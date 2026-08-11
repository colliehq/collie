"""execute_code — programmatic tool calling (Hermes' per-token lever, on brand for collie).

The model writes a short Python script that drives collie's OWN tools over a localhost RPC
and PRINTS only what matters. Ten greps/reads/code_search calls collapse into ONE summarized
turn instead of ten tool messages in the context window — the single biggest structural lever
on tokens-per-task, exactly the axis collie competes on.

Design (lean, keyless): while the script runs, collie stands up an ephemeral 127.0.0.1 HTTP
server whose requests are brokered back through the live Harness tool dispatcher
(POST /tool {name,args} -> {result}). The child process
gets a tiny preamble defining read_file/grep/glob/code_search/bash/web_search/tool() helpers
that POST to it. Only the child's STDOUT (capped) returns to the model. The server binds to
loopback on an ephemeral port and is torn down when the script exits. Same trust level as the
existing bash tool (arbitrary model-authored code), but far cheaper on context.
"""
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .tools import Tool

_PREAMBLE = r'''
import json as _json, os as _os, sys as _sys, urllib.request as _u
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
            req = json.loads(self.rfile.read(n) or b"{}")
            name = req.get("name", "")
            # Never dispatch against the registry here.  That used to bypass the Harness
            # permission gate, audit ledger, lifecycle hooks and durability fence.  The
            # callback is installed only while Harness is executing this execute_code call;
            # direct/embedded use without a broker therefore fails closed.  Even forbidden
            # recursion is routed to the broker so its denial gets the same durable receipts.
            broker = getattr(self.server, "collie_tool_broker", None)
            if broker is None:
                out = "ERROR: execute_code inner tool broker is unavailable"
            else:
                out = broker(name, req.get("args", {}) or {})
            body = json.dumps({"result": out if isinstance(out, str) else str(out)}).encode()
        except Exception as e:
            body = json.dumps({"result": "ERROR(rpc): %s" % e}).encode()
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
        "bash(command), code_search(query), web_search(query), or tool(name, **args). ONLY what "
        "you print() returns to you (capped ~6000 chars), so print a tight summary, not raw dumps. "
        "Args: code (Python source), optional timeout seconds (default 60).")
    schema = {"type": "object", "properties": {
        "code": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["code"]}

    def __init__(self, registry):
        self._registry = registry

    def run(self, args, ctx):
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
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                f.write(_PREAMBLE + "\n" + code)
                path = f.name
            env = dict(os.environ, COLLIE_RPC_PORT=str(port), COLLIE_RPC_TOKEN=token)
            # Do NOT prepend the untrusted repo (ctx.cwd) to PYTHONPATH — that puts it ahead of the
            # stdlib on sys.path, so a repo file named json.py/urllib.py would shadow and RUN in
            # place of the stdlib module on any import. The preamble appends cwd AFTER the stdlib.
            try:
                from . import plat as _plat
                p = subprocess.run([sys.executable, path], cwd=ctx.cwd, env=env,
                                   capture_output=True, text=True, **_plat.no_window_kwargs(),
                                   encoding="utf-8", errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired:
                return "ERROR: execute_code timed out after %ds" % timeout
            except Exception as e:               # bad interpreter, exec failure, etc. — never escape
                return "ERROR(execute_code): %s" % e
            out = (p.stdout or "")[:6000]
            if p.returncode != 0:
                return out + "\n[exit %d] %s" % (p.returncode, (p.stderr or "").strip()[-1500:])
            if not out.strip():
                tail = (p.stderr or "").strip()[-600:]
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
