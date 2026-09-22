"""execute_code — programmatic tool calling (Hermes' per-token lever, on brand for collie).

The model writes a short Python script that drives collie's OWN tools over a localhost RPC
and PRINTS only what matters. Ten greps/reads/code_search calls collapse into ONE summarized
turn instead of ten tool messages in the context window — the single biggest structural lever
on tokens-per-task, exactly the axis collie competes on.

Design (lean, keyless): while the script runs, collie stands up an ephemeral 127.0.0.1 HTTP
server whose requests are brokered back through the live Harness tool dispatcher
(POST /tool {name,args} -> {result}). The child process gets a tiny preamble defining
read_file/grep/glob/code_search/bash/web_search/tool() helpers that POST to it. Only the child's
STDOUT (capped) returns to the model. The server binds to loopback on an ephemeral port and is
torn down when the script exits.

Security boundary: this is arbitrary model-authored Python with the SAME host, filesystem, and
network authority as the existing bash tool. Calls made through ``tool()`` are brokered; direct
Python I/O (``open``, ``socket``, ``subprocess``, ctypes, etc.) is not. Process-tree ownership,
isolated interpreter startup, and bounded output below contain lifetime/import/output hazards;
they are deliberately not described as an OS sandbox.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from .httpserver import ThreadingHTTPServer

from .tools import Tool

_PREAMBLE = r'''
import json as _json, os as _os, sys as _sys, urllib.request as _u
# The host does not release this gate until the process has been placed in its process-tree
# owner. User code therefore cannot win the Windows Popen -> AssignProcessToJobObject race.
if _sys.stdin.buffer.read(1) != b"G":
    raise SystemExit("execute_code start gate was not released")
_PORT = _os.environ["COLLIE_RPC_PORT"]
_TOKEN = _os.environ.get("COLLIE_RPC_TOKEN", "")
def tool(name, **args):
    """Call any collie tool by name; returns its string output."""
    _b = _json.dumps({"name": name, "args": args}).encode()
    _r = _u.Request("http://127.0.0.1:%s/tool" % _PORT, data=_b,
                    headers={"content-type": "application/json",
                             "x-collie-rpc-token": _TOKEN})
    with _u.urlopen(_r, timeout=180) as _resp:
        return _json.loads(_resp.read())["result"]
def read_file(path, **kw):     return tool("read_file", path=path, **kw)
def grep(pattern, **kw):       return tool("grep", pattern=pattern, **kw)
def glob(pattern, **kw):       return tool("glob", pattern=pattern, **kw)
def bash(command, **kw):       return tool("bash", command=command, **kw)
def code_search(query, **kw):  return tool("code_search", query=query, **kw)
def web_search(query, **kw):   return tool("web_search", query=query, **kw)
# Repo-local imports are allowed, but appended AFTER the stdlib so a repo file named
# e.g. json.py / urllib.py can never shadow (and execute in place of) a stdlib module.
_sys.path.append(_os.getcwd())
# ---- user code ----
'''


class _BoundedCapture:
    """Drain a pipe continuously while retaining only a fixed-size head or tail.

    A model can print forever. ``communicate(capture_output=True)`` retains all of that output
    before Collie applies its display cap, allowing a tiny response to consume the host's memory.
    Reader threads keep both OS pipes flowing, while this object bounds resident capture memory.
    """

    def __init__(self, limit: int, keep_tail: bool = False):
        self.limit = max(0, int(limit))
        self.keep_tail = keep_tail
        self.data = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if not self.limit:
            return
        if self.keep_tail:
            self.data.extend(chunk)
            if len(self.data) > self.limit:
                del self.data[:-self.limit]
        elif len(self.data) < self.limit:
            self.data.extend(chunk[:self.limit - len(self.data)])

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


def _drain_pipe(pipe, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = pipe.read(64 * 1024)
            if not chunk:
                return
            capture.add(chunk)
    except (OSError, ValueError):
        # Cleanup may close a pipe after a failed spawn/kill. The process result is still useful.
        return
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


class _ProcessTree:
    """Reuse the common owned-tree proof; scripts always reap their descendants."""

    def __init__(self, child):
        from . import tool_process
        self.owner = tool_process.own_gated_process(
            child, {"start_new_session": os.name != "nt"})
        self.detail = ""

    def terminate_and_wait(self):
        confirmed, self.detail = self.owner.terminate()
        return confirmed

    def close(self):
        self.owner.discard()


def _child_env(port: int, token: str) -> dict:
    """Build an interpreter environment without PYTHON* startup/import injection."""
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("PYTHON")}
    env.update(COLLIE_RPC_PORT=str(port), COLLIE_RPC_TOKEN=token)
    return env


def _isolated_python_executable() -> str:
    """Return a Windows interpreter that cannot outrun Job assignment via a venv launcher.

    Windows virtual environments use a tiny redirector executable.  Starting that redirector and
    assigning it to a Job leaves a race in which it can spawn the real interpreter before the
    redirector is assigned; the stdin gate then protects a process outside Collie's Job.  CPython
    exposes the trusted base interpreter specifically as ``sys._base_executable``.  execute_code is
    isolated and needs no venv packages, so launching the base interpreter closes that race.
    """
    if os.name == "nt":
        base = getattr(sys, "_base_executable", None)
        if isinstance(base, str) and base:
            return base
    return sys.executable


def _rpc_host_ok(host):
    """Accept only loopback Host headers: this RPC exposes collie's real tools (bash/edit),
    so a non-loopback Host (DNS-rebinding) or a forged remote request must be refused."""
    h = (host or "").rsplit(":", 1)[0].strip("[]").lower()
    return h in ("", "127.0.0.1", "localhost", "::1")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _deny(self):
        body = b'{"result": "ERROR(rpc): forbidden"}'
        try:
            self.send_response(403)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def do_POST(self):
        # Loopback + ephemeral port is NOT authentication. Require a loopback Host (defeats
        # DNS-rebinding) and the per-invocation token (defeats other local processes) before
        # dispatching to bash/write_file/edit_file.
        if not _rpc_host_ok(self.headers.get("Host", "")):
            return self._deny()
        want = getattr(self.server, "collie_token", "")
        got = self.headers.get("x-collie-rpc-token", "")
        if not want or not hmac.compare_digest(got, want):
            return self._deny()
        try:
            n = int(self.headers.get("content-length", 0) or 0)
            if n <= 0 or n > 1_048_576:
                raise ValueError("RPC body must be 1..1048576 bytes")

            def reject_constant(value):
                raise ValueError("non-finite JSON number is forbidden: %s" % value)

            req = json.loads(self.rfile.read(n), parse_constant=reject_constant)
            if not isinstance(req, dict):
                raise ValueError("RPC body must be a JSON object")
            name = req.get("name", "")
            args = req.get("args", {})
            if not isinstance(name, str) or not name or not isinstance(args, dict):
                raise ValueError("RPC name must be a string and args must be an object")
            # Never dispatch against the registry here.  That used to bypass the Harness
            # permission gate, audit ledger, lifecycle hooks and durability fence.  The
            # callback is installed only while Harness is executing this execute_code call;
            # direct/embedded use without a broker therefore fails closed.  Even forbidden
            # recursion is routed to the broker so its denial gets the same durable receipts.
            broker = getattr(self.server, "collie_tool_broker", None)
            if broker is None:
                out = "ERROR: execute_code inner tool broker is unavailable"
            else:
                out = broker(name, args)
            body = json.dumps({"result": out if isinstance(out, str) else str(out)},
                              allow_nan=False).encode()
        except Exception as e:
            body = json.dumps({"result": "ERROR(rpc): %s" % e}, allow_nan=False).encode()
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # The child is killed on timeout and may close its socket while an inner
            # tool is finishing. The Harness recovery fence already records that
            # ambiguity; a loopback response write failure adds no useful signal.
            pass


class ExecuteCodeTool(Tool):
    name, tier = "execute_code", "always"
    description = (
        "Run a short Python script that drives collie's tools programmatically and PRINTS a "
        "summary — use it to do heavy exploration (many read/grep/glob/code_search/web_search "
        "calls, loops, filtering, counting) in ONE turn instead of many tool round-trips. In the "
        "script you may call: read_file(path), grep(pattern, path=...), glob(pattern), "
        "bash(command), code_search(query), web_search(query), or tool(name, **args). Calls through "
        "these helpers are brokered, but direct Python file/network/process I/O is NOT sandboxed "
        "and has the same host authority as bash. ONLY what you print() returns to you (capped "
        "~6000 chars), so print a tight summary, not raw dumps. "
        "Args: code (Python source), optional timeout seconds (default 60).")
    schema = {"type": "object", "properties": {
        "code": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["code"]}

    def __init__(self, registry):
        self._registry = registry

    def run(self, args, ctx):
        from . import tool_process
        cancelled = tool_process.cancel_check(ctx)
        if tool_process.is_cancelled(cancelled):
            return "ERROR: execute_code canceled before execution"
        code = args.get("code") or ""
        if not code.strip():
            return "ERROR: empty code"
        timeout = max(1, min(300, int(args.get("timeout", 60))))
        token = secrets.token_hex(16)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv.collie_tool_broker = getattr(ctx, "tool_broker", None)
        srv.collie_token = token
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        path = None
        proc = None
        owner = None
        readers = []
        stdout_capture = _BoundedCapture(24 * 1024)
        stderr_capture = _BoundedCapture(16 * 1024, keep_tail=True)
        timed_out = False
        stopped = False
        released = False
        tree_terminated = False
        run_error = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                f.write(_PREAMBLE + "\n" + code)
                path = f.name
            # Do NOT prepend the untrusted repo (ctx.cwd) to PYTHONPATH — that puts it ahead of the
            # stdlib on sys.path, so a repo file named json.py/urllib.py would shadow and RUN in
            # place of the stdlib module on any import. -I ignores PYTHON* and excludes the script
            # directory; the preamble appends cwd only AFTER trusted stdlib/site paths are ready.
            try:
                from . import plat as _plat
                spawn_kw = _plat.no_window_kwargs()
                if os.name != "nt":
                    # Deliberately do not use plat.new_group_kwargs(): execute_code owns a nested
                    # tree even when its caller is a Slack process owner, and must reap it without
                    # killing the whole task after a normal script return.
                    spawn_kw["start_new_session"] = True
                proc = subprocess.Popen(
                    [_isolated_python_executable(), "-I", "-u", path], cwd=ctx.cwd,
                    env=_child_env(port, token), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, **spawn_kw)
                # User code is blocked on the preamble gate while the Windows Job Object is
                # assigned. Assignment failure stays fail-closed: the byte is never released.
                owner = _ProcessTree(proc)
                for pipe, capture in ((proc.stdout, stdout_capture),
                                      (proc.stderr, stderr_capture)):
                    reader = threading.Thread(target=_drain_pipe, args=(pipe, capture), daemon=True)
                    reader.start()
                    readers.append(reader)
                if tool_process.is_cancelled(cancelled):
                    stopped = True
                else:
                    # From the first release byte onward, user code may have run.
                    released = True
                    proc.stdin.write(b"G")
                    proc.stdin.close()
                    deadline = time.monotonic() + timeout
                    while True:
                        if tool_process.is_cancelled(cancelled):
                            stopped = True
                            break
                        if proc.poll() is not None:
                            break
                        if time.monotonic() >= deadline:
                            timed_out = True
                            break
                        time.sleep(.05)
            except Exception as e:               # bad interpreter, exec failure, etc. — never escape
                run_error = e
            finally:
                revoke = getattr(getattr(srv, "collie_tool_broker", None), "revoke", None)
                if callable(revoke):
                    revoke()
                # Reap descendants even when the direct script returned successfully or raised.
                # This runs before output-drain joins, so inherited pipe handles cannot wedge us.
                if owner is not None:
                    tree_terminated = owner.terminate_and_wait()
                    if released and not tree_terminated:
                        tool_process.mark_effect_uncertain(ctx)
                elif proc is not None:
                    try:
                        proc.kill()
                        proc.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                for reader in readers:
                    reader.join(timeout=5)
                quiesce = getattr(getattr(srv, "collie_tool_broker", None), "quiesce", None)
                if callable(quiesce):
                    quiesce()
                if proc is not None:
                    for pipe in (proc.stdin, proc.stdout, proc.stderr):
                        try:
                            if pipe is not proc.stdin and any(r.is_alive() for r in readers):
                                continue       # a blocked reader owns the pipe; close can block too
                            if pipe is not None:
                                pipe.close()
                        except (OSError, ValueError):
                            pass
                if owner is not None:
                    owner.close()

            partial = stdout_capture.text()[:6000]
            capture = getattr(getattr(srv, "collie_tool_broker", None), "captured_results", None)
            if (stopped or timed_out or not tree_terminated) and callable(capture):
                inner_results = capture()
                if inner_results:
                    partial += "\nPartial tool results:\n" + json.dumps(inner_results, ensure_ascii=False)[:6000]
            if released and not tree_terminated:
                # Say WHY. The owner knows whether the group answered a signal, outlived
                # SIGKILL, or could not be signalled at all, and that distinction is the
                # whole of what a recovery inspection has to start from; dropping it left
                # the reader (and CI) with an unfalsifiable "could not be confirmed".
                reason = getattr(owner, "detail", "") or ""
                return ("ERROR: execute_code process-tree termination could not be confirmed; "
                        "recovery inspection is required." +
                        (" (%s)" % reason if reason else "") + "\n" + partial)
            if stopped:
                return ("ERROR: execute_code canceled by the user; " +
                        ("partial output:\n" + partial if released else
                         "the script was not executed"))
            if run_error is not None:
                return "ERROR(execute_code): %s" % run_error
            if timed_out:
                return "ERROR: execute_code timed out after %ds\n%s" % (timeout, partial)
            out = stdout_capture.text()[:6000]
            err = stderr_capture.text()
            if proc.returncode != 0:
                return out + "\n[exit %d] %s" % (proc.returncode, err.strip()[-1500:])
            if not out.strip():
                tail = err.strip()[-600:]
                return ("(script ran OK but produced no stdout — remember to print() what you need)"
                        + ("\n[stderr] " + tail if tail else ""))
            return out
        finally:
            revoke = getattr(getattr(srv, "collie_tool_broker", None), "revoke", None)
            if callable(revoke):
                revoke()
            srv.shutdown()
            srv.server_close()          # shutdown() stops serve_forever but LEAKS the listen socket
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def register_execute_code(registry):
    registry.register(ExecuteCodeTool(registry))
    return True
