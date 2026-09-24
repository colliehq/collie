"""Owned, cancellable subprocesses for the built-in tools.

A built-in tool that shells out (`bash`, `grep`) owns a whole process TREE, not one
process: the shell, whatever it launched, and anything those backgrounded. Three things
follow, and all three used to be missing at this boundary:

  • **Stop has to mean stop.** ``Popen.communicate(timeout=…)`` cannot be interrupted, so
    a Stop pressed one second into ``pytest -x`` did nothing at all — the command kept
    compiling, writing files and burning the machine until its own timeout (up to 600s)
    expired. Cancellation here is cooperative and host-owned: the surface hands the loop a
    callback, the loop hands it to the tool through ``ToolCtx.cancelled``, and the wait
    loop polls it every ``CANCEL_POLL_S`` alongside the deadline.

  • **A cancelled command must never read like a finished one.** Killing the shell is not
    evidence that its descendants stopped; a backgrounded test runner can keep editing the
    working tree after we have "cancelled" it. So termination is *proved* where the OS lets
    us prove it (Windows Job accounting, a dedicated POSIX process group polled to ESRCH)
    and reported as unconfirmed where it does not — as DATA (``Outcome.effect_uncertain``)
    and not only as prose, because the host has to fence such a turn, not just read it.

  • **Ownership must exist before the command does.** Assigning a Job to a shell that is
    already running is a race: between ``Popen`` returning and ``AssignProcessToJobObject``,
    an arbitrary command can spawn — or an MSYS shell re-parent — descendants that the Job
    then never contains, while Job accounting would still happily report the (empty) tree
    extinct. On Windows the command is therefore launched by a fixed, trusted bootstrap that
    blocks in ``stdin.readline()`` and does nothing at all until Collie explicitly releases
    it; the Job is attached to that bootstrap first, so ordinary descendants inherit the Job.
    A program launched indirectly by an outside system service is not one of those descendants.
    If ownership cannot be established the command is
    never released and never runs. POSIX needs no such gate: ``start_new_session`` is applied
    in the forked child before ``exec``, so the group exists before the shell's first
    instruction.

Ownership deliberately reuses ``plat``'s helpers rather than growing a second dialect of
process killing: ``new_group_kwargs`` for the POSIX group, ``attach_kill_on_close_job`` for
the Windows kernel Job, ``no_window_kwargs`` so nothing flashes a console, ``kill_tree`` as
the taskkill belt, and ``release_without_terminating`` for the one case that must NOT kill.
Nothing here ever kills by name or by scanning the process table, so a cancel can only ever
reach the tree this call created — never another Collie run. There is no global registry of
live processes either: each call owns exactly what it started, for exactly as long as it
runs.
"""

from __future__ import annotations

import base64
import codecs
from collections import deque
import json
import locale
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

from . import plat

# Stop → kill latency, and the cost of asking. 50ms is imperceptible to a human pressing
# Stop and is nothing next to the shell startup it runs beside.
CANCEL_POLL_S = .05
# Bounded wait for the pipes to close once the tree has been killed. Never unbounded: a
# descendant we could not reap must not be able to wedge the agent loop.
DRAIN_S = 5.0
# Capture both ends of noisy commands without letting output grow with runtime.
# Tools may persist this captured text, and explicitly report any omitted middle.
OUTPUT_CAPTURE_CHARS = 1_048_576
OUTPUT_HEAD_CHARS = 32_768
PIPE_READ_CHARS = 65_536


# What a command prints on Windows is not in one encoding. Git, Git Bash's own tools, node and
# rg write UTF-8; Python children and legacy console programs write the ANSI code page. Read in
# the ANSI code page (the text-mode default), everything UTF-8 -- `git log`, `cat` of a source
# file -- reached the model garbled wherever that code page is not UTF-8 (936, the Chinese
# default; 1252). So each line is read as UTF-8 when it is valid UTF-8, which text in another
# code page almost never is, and in the ANSI code page otherwise. Where the code page already
# is UTF-8 (65001) nothing changes.
OUTPUT_CODEC = "collie_utf8_else_ansi"
_LONG_LINE_BYTES = 65_536


def _ansi() -> str:
    # The code page children write in -- what subprocess's text mode would use. Not
    # getpreferredencoding(): under UTF-8 mode that says utf-8 for this process alone.
    getencoding = getattr(locale, "getencoding", None)          # 3.11+
    return getencoding() if getencoding else locale.getpreferredencoding(False)


def _decode_line(raw: bytes, errors: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(_ansi(), errors)


class _Utf8ElseAnsiDecoder(codecs.IncrementalDecoder):
    """Decide per line. A line longer than _LONG_LINE_BYTES is released in pieces cut on a
    UTF-8 character boundary, so one enormous line keeps the reader's memory bound."""

    def __init__(self, errors="replace"):
        super().__init__(errors)
        self._pending = b""

    def decode(self, data, final=False):
        buf = self._pending + bytes(data)
        out, start = [], 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            out.append(_decode_line(buf[start:nl + 1], self.errors))
            start = nl + 1
        rest = buf[start:]
        if final and rest:
            out.append(_decode_line(rest, self.errors))
            rest = b""
        elif len(rest) >= _LONG_LINE_BYTES:
            utf8 = codecs.getincrementaldecoder("utf-8")()
            try:
                out.append(utf8.decode(rest))
                rest = utf8.getstate()[0]           # an incomplete character waits for the rest
            except UnicodeDecodeError:
                out.append(rest.decode(_ansi(), self.errors))
                rest = b""
        self._pending = rest
        return "".join(out)

    def reset(self):
        self._pending = b""

    def getstate(self):
        return self._pending, 0

    def setstate(self, state):
        self._pending = state[0]


class _AnsiEncoder(codecs.IncrementalEncoder):
    def encode(self, text, final=False):
        return text.encode(_ansi(), self.errors)


def _codec_search(name):
    if name != OUTPUT_CODEC:
        return None
    return codecs.CodecInfo(
        name=OUTPUT_CODEC,
        encode=lambda text, errors="strict": (text.encode(_ansi(), errors), len(text)),
        decode=lambda raw, errors="strict": (
            _Utf8ElseAnsiDecoder(errors).decode(raw, final=True), len(raw)),
        incrementalencoder=_AnsiEncoder, incrementaldecoder=_Utf8ElseAnsiDecoder)


codecs.register(_codec_search)


def _output_encoding():
    """The pipes' encoding for a command: None (the platform default) unless Windows's ANSI
    code page is something other than UTF-8."""
    if not plat.is_windows():
        return None
    try:
        if codecs.lookup(_ansi()).name == "utf-8":
            return None
    except LookupError:
        pass
    return OUTPUT_CODEC

OK = "ok"
TIMEOUT = "timeout"
CANCELED = "canceled"
PRELAUNCH_CANCELED = "prelaunch_canceled"
LAUNCH_ERROR = "launch_error"
# Released, then lost: Collie could not finish handing the command to its owned tree, so the
# command MAY be running. Counted as executed on purpose — the honest half of the guarantee
# that a prelaunch failure is the only thing allowed to say "it did not run".
HANDOVER_ERROR = "handover_error"

# CreateProcess flag; the bootstrap is spawned windowless and spawns the command the same
# way, so a windowless Collie (pythonw: the Slack dog, the wallpaper, the desktop app) still
# never throws a black console box per shell step.
_CREATE_NO_WINDOW = 0x08000000

# The trusted Windows bootstrap, in full. It is fixed code — it never interpolates the
# command, it only forwards JSON it is handed on stdin — and its single reason to exist is
# ORDER: it is already assigned to the Job before the release line arrives, so the command
# cannot create anything outside that Job. Two consequences worth stating:
#   • stdin closed without a release line ⇒ it runs NOTHING. That is the fail-closed belt
#     under ownership failure: even if our kill were to lose the race, no command runs.
#   • it records, in a file only Collie names, whether the command was ever created. That is
#     the only honest way to distinguish "the program does not exist" from "something went
#     wrong after we let it go" — the second must never be reported as "it did not run".
_BOOTSTRAP_SRC = r'''
import json, os, subprocess, sys

def _record(path, payload):
    try:
        with open(path, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
    except Exception:
        pass                    # Collie treats an unreadable record as "unknown", never as "no"

line = sys.stdin.readline()     # blocks until Collie has proved it owns this process tree
try:
    req = json.loads(line) if line.strip() else None
except Exception:
    req = None
if not req:
    os._exit(0)                 # released nothing: the command was never created
status = req.get("status") or ""
try:
    child = subprocess.Popen(
        req["argv"], shell=bool(req.get("shell")), cwd=(req.get("cwd") or None),
        stdin=subprocess.DEVNULL, creationflags=0x08000000)
except BaseException as exc:
    _record(status, {"launched": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    sys.stderr.write("could not start the command: %s: %s\n" % (type(exc).__name__, exc))
    sys.stderr.flush()
    os._exit(126)
_record(status, {"launched": True, "pid": child.pid})
rc = child.wait()
# Windows exit codes are unsigned DWORDs; os._exit takes a C int. Converting to the signed
# equivalent round-trips exactly (0xC0000005 → -1073741819 → 0xC0000005) instead of raising
# OverflowError and turning a crashed command into a bootstrap crash.
os._exit(rc - 0x100000000 if rc >= 0x80000000 else rc)
'''


def _bootstrap_argv():
    """``python -I -c …`` for the bootstrap, as ONE argument with no quoting hazards.

    Base64 keeps the program on a single line: a multi-line ``-c`` argument would carry raw
    newlines through ``list2cmdline``, which only quotes on spaces and tabs, and the command
    line would arrive at CreateProcess split. ``-I`` (isolated) is the same choice the SDK
    worker makes: a tool call runs in the model's workspace, and PYTHONPATH / user site /
    ``sitecustomize`` from that workspace must not get a say in Collie's own launcher.
    """
    blob = base64.b64encode(_BOOTSTRAP_SRC.encode("utf-8")).decode("ascii")
    # A Windows venv python.exe can be a redirector that spawns the real
    # interpreter before Job assignment. Start that interpreter directly.
    executable = (getattr(sys, "_base_executable", None) or sys.executable
                  if plat.is_windows() else sys.executable)
    return [executable, "-I", "-c",
            'import base64;exec(base64.b64decode("%s").decode("utf-8"))' % blob]


def cancel_check(ctx):
    """The host's cancellation callback for this tool call, or None.

    Read with getattr on purpose: embedders and the many small test contexts pass a plain
    object with `cwd`/`project` and nothing else, and a tool must keep working for them.
    """
    cb = getattr(ctx, "cancelled", None)
    return cb if callable(cb) else None


def is_cancelled(cancelled) -> bool:
    """Ask the host whether to stop. A callback that RAISES an ordinary exception is a
    broken surface, not a stop request — swallowing it keeps a running command from dying
    on someone else's bug. KeyboardInterrupt/SystemExit are deliberately not caught: those
    are the interpreter telling us to unwind, and the caller cleans up its tree on the way."""
    if cancelled is None:
        return False
    try:
        return bool(cancelled())
    except Exception:
        return False


def mark_effect_uncertain(ctx) -> bool:
    """Tell the HOST — as data, not prose — that this call's effects are not known to be over.

    The message already says so, but a message is only advice to the model; the loop needs a
    flag it can fence a turn on (finalization must not treat a turn containing an action of
    unknown extent as cleanly finished). Host-only and set-only: a tool marks it, no model
    argument can. Best effort by design — a context that cannot carry the field is one of the
    small test/embedder doubles, and refusing to run there would be a much worse trade.
    """
    try:
        setattr(ctx, "tool_effect_uncertain", True)
        return True
    except Exception:
        return False


@dataclass
class Outcome:
    """What actually happened, separated from how a tool wants to word it."""
    # ok | timeout | canceled | prelaunch_canceled | launch_error | handover_error
    status: str
    returncode: "int | None" = None
    stdout: str = ""
    stderr: str = ""
    elapsed_s: float = 0.0
    # PROVED whole-tree extinction: Job accounting says zero, or the POSIX group answered
    # ESRCH, or nothing was ever launched. Never set from the mere fact that a kill was
    # delivered — and NOT implied by a normal exit either: a command that finished after
    # backgrounding a server exits 0 with its tree very much alive (see background_detached).
    tree_terminated: bool = True
    # The foreground command finished and left descendants running ON PURPOSE, and we handed
    # those descendants over cleanly: they are no longer tied to this Collie process. This is
    # a success, not a failed kill — the difference matters to every consumer of the two.
    background_detached: bool = False
    # We could do NEITHER: a tree the command deliberately left running could not be handed
    # over, so it may die when Collie exits (or may not). A known failure, not mere ignorance
    # — a host that cannot see into a shared process group simply has nothing to report, and
    # must not spend the fence on every ordinary command.
    release_failed: bool = False
    detail: str = ""            # why it could not be confirmed, or the launch error
    stdout_omitted_chars: int = 0
    stderr_omitted_chars: int = 0

    @property
    def executed(self) -> bool:
        return self.status not in (PRELAUNCH_CANCELED, LAUNCH_ERROR)

    @property
    def effect_uncertain(self) -> bool:
        """True when this call may still be having effects nobody can account for.

        Two shapes: an interrupted command whose tree could not be proved gone, and a
        finished command whose deliberate survivors could not be released safely (we may
        have kept them alive, we may be about to kill them at exit — we do not know which).
        A clean cancellation and an ordinary success are both certain, and neither sets it;
        neither does a foreground success that merely cannot see into someone else's group.
        """
        if not self.executed:
            return False
        if self.status in (CANCELED, TIMEOUT, HANDOVER_ERROR):
            return not self.tree_terminated
        return bool(self.release_failed)


class _Reader(threading.Thread):
    """Drain bounded line fragments, keeping the beginning and most recent output.

    Both a giant line and many tiny lines have a fixed memory ceiling. Newline
    reads still make ordinary progress output available before the process exits.
    """

    def __init__(self, stream):
        super().__init__(daemon=True, name="collie-tool-pipe")
        self._stream = stream
        self._head = ""
        self._tail = deque()
        self._tail_chars = 0
        self._total_chars = 0
        self._lock = threading.Lock()

    def _append(self, chunk):
        with self._lock:
            self._total_chars += len(chunk)
            head_room = OUTPUT_HEAD_CHARS - len(self._head)
            if head_room > 0:
                self._head += chunk[:head_room]
                chunk = chunk[head_room:]
            if not chunk:
                return
            self._tail.append(chunk)
            self._tail_chars += len(chunk)
            tail_limit = OUTPUT_CAPTURE_CHARS - OUTPUT_HEAD_CHARS
            while self._tail_chars > tail_limit:
                excess = self._tail_chars - tail_limit
                first = self._tail.popleft()
                if len(first) > excess:
                    self._tail.appendleft(first[excess:])
                    self._tail_chars -= excess
                else:
                    self._tail_chars -= len(first)

    def run(self):
        try:
            while True:
                chunk = self._stream.readline(PIPE_READ_CHARS)
                if not chunk:
                    break
                self._append(chunk)
        except Exception:
            pass                      # a pipe closed under us is an end, not a failure

    def text(self) -> str:
        with self._lock:
            omitted = self._total_chars - len(self._head) - self._tail_chars
            marker = ("\n[... %d output characters omitted from the middle ...]\n" % omitted
                      if omitted else "")
            return self._head + marker + "".join(self._tail)

    @property
    def omitted_chars(self) -> int:
        with self._lock:
            return self._total_chars - len(self._head) - self._tail_chars


def _kill_owned_group(pgid: int, timeout_s: float, reap=None):
    """SIGKILL a process group WE created and poll it until ESRCH.

    Delivery is not extinction: the returning caller is about to tell the model whether the
    command really stopped, and a still-draining test runner writing files would make that
    a lie. Only the group started by this call is ever signalled.

    Signal before reaping our direct child: an unreaped group leader can be the last
    member preventing pgid reuse. Never send another destructive signal after reaping.
    Reap before probing because our own zombie can otherwise keep the group present.
    Darwin also returns EPERM to SIGKILL for a zombie-only group, then ESRCH after
    reaping. A denied initial signal therefore still permits this cleanup and bounded
    probe when a reaper is available. Only ESRCH confirms extinction.
    """
    import signal
    initial_error = ""
    try:
        os.killpg(int(pgid), getattr(signal, "SIGKILL", 9))
    except ProcessLookupError:
        if reap is not None:
            reap()
        return True, ""
    except OSError as e:
        initial_error = "%s: %s" % (type(e).__name__, e)
        if reap is None:
            # Without a reaper there is no further child cleanup we can perform here.
            return False, initial_error
    if reap is not None:
        reap()
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    probe_error = initial_error
    while True:
        try:
            os.killpg(int(pgid), 0)
            probe_error = ""
        except ProcessLookupError:
            return True, ""
        except PermissionError as e:
            # Darwin can briefly return EPERM while a successfully killed,
            # leaderless group is disappearing. Keep waiting for ESRCH; neither
            # EPERM nor a successful signal is itself proof of termination.
            probe_error = "%s: %s" % (type(e).__name__, e)
        if time.monotonic() >= deadline:
            return False, (probe_error or initial_error or
                           "process group still had members %.0fs after SIGKILL" % timeout_s)
        time.sleep(.01)


class _NotStarted(Exception):
    """The run ended during startup — usually because nothing was ever released to run.

    ``HANDOVER_ERROR`` is the exception to the name and the reason this carries the
    termination facts too: there, the command may genuinely have started before Collie lost
    its grip on it, and reporting "not executed" would be the one lie that matters.
    """

    def __init__(self, status, detail, tree_terminated=True):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.tree_terminated = bool(tree_terminated)


class _Bootstrap:
    """Collie's side of the trusted Windows launcher: the release, and what it recorded."""

    def __init__(self, status_path: str):
        self.status_path = status_path
        self._record = None

    def release(self, proc, argv, use_shell: bool, cwd: str) -> None:
        """Hand the command over. After this call the command is RUNNING, or about to be.

        ASCII-only on the wire (``ensure_ascii``) so a command or path with non-Latin
        characters cannot depend on the launcher's stdin encoding matching ours.
        """
        payload = json.dumps({"argv": argv, "shell": bool(use_shell), "cwd": cwd or "",
                              "status": self.status_path}, ensure_ascii=True)
        proc.stdin.write(payload + "\n")
        proc.stdin.flush()
        proc.stdin.close()        # EOF: the command inherits no writable channel back to us

    def launched(self):
        """True / False / None — did the user's command ever become a process?

        None (no record, unreadable record) is the important value: it means we do NOT know,
        and a caller may not turn it into "it did not run". Only the bootstrap's own explicit
        report that CreateProcess failed is evidence of nothing having happened.
        """
        if self._record is None:
            try:
                with open(self.status_path, encoding="utf-8") as fh:
                    self._record = json.loads(fh.read() or "{}")
            except Exception:
                self._record = {}
        got = self._record.get("launched")
        return got if isinstance(got, bool) else None

    def error(self) -> str:
        return str((self._record or {}).get("error") or "")

    def cleanup(self) -> None:
        try:
            os.unlink(self.status_path)
        except Exception:
            pass                  # a temp file we could not remove is not worth a failure


class _Owner:
    """Kernel-backed ownership of one tool subprocess and its descendants."""

    def __init__(self, proc, group_kwargs, job=None, detail=""):
        self.proc = proc
        self.job = job
        self.pgid = 0
        self.detail = detail
        self._extinct = False
        if plat.is_windows():
            if self.job is None and not self.detail:
                # Not reachable through run_owned (ownership failure refuses to release the
                # command at all); kept so a direct caller degrades instead of crashing.
                self.detail = "no Windows Job Object was created"
        elif group_kwargs.get("start_new_session"):
            # start_new_session makes the child its own group leader, so pid == pgid.
            # Captured NOW: once the shell exits, getpgid(pid) can no longer find the
            # group its still-running background members are in.
            self.pgid = int(proc.pid)
        else:
            # A Slack/Mission worker deliberately shares Collie's group (plat decides
            # this, not us). killpg would then signal Collie ITSELF, so the direct child
            # is the only thing we may safely kill here — and we say so.
            self.detail = ("subprocess shares Collie's process group (COLLIE_PROCESS_OWNER=%s), "
                           "so only the direct child is ours to kill"
                           % (os.environ.get("COLLIE_PROCESS_OWNER", "") or "unset"))

    # -- termination ------------------------------------------------------- #
    def terminate(self, timeout_s: float = DRAIN_S):
        """Kill the owned tree; return (confirmed_extinct, detail)."""
        if self._extinct:
            return True, ""
        if self.job is not None:
            confirmed, detail = self._terminate_job(timeout_s)
        elif plat.is_windows():
            confirmed, detail = self._terminate_windows_taskkill(timeout_s)
        elif self.pgid > 1:
            confirmed, detail = _kill_owned_group(
                self.pgid, timeout_s, reap=lambda: self._reap_direct(timeout_s))
        else:
            confirmed, detail = self._terminate_direct_only(timeout_s)
        self._extinct = bool(confirmed)
        if detail and self.detail and detail != self.detail:
            detail = self.detail + "; " + detail
        return confirmed, (detail or self.detail)

    def _terminate_job(self, timeout_s):
        try:
            if self.job.terminate_and_wait(timeout_s=timeout_s):
                self._reap_direct(timeout_s)
                return True, ""
        except Exception as e:
            return False, "Job termination failed (%s: %s)" % (type(e).__name__, e)
        # Belt: the PID cannot be recycled while our Popen still holds the process handle,
        # so taskkill /T here can only ever reach this call's own tree.
        plat.kill_tree(self.proc)
        deadline = time.monotonic() + 2.0
        while True:
            try:
                if self.job.active_processes() == 0:
                    self._reap_direct(timeout_s)
                    return True, ""
            except Exception as e:
                return False, "Job accounting unavailable (%s: %s)" % (type(e).__name__, e)
            if time.monotonic() >= deadline:
                return False, "Windows Job still reported live processes after termination"
            time.sleep(.01)

    def _terminate_windows_taskkill(self, timeout_s):
        plat.kill_tree(self.proc)                 # handle still open ⇒ no PID reuse hazard
        direct = self._reap_direct(timeout_s)
        if not direct:
            return False, "the command's own process did not exit after taskkill"
        # taskkill /T reports delivery, never extinction, and without a Job there is no
        # kernel accounting to ask. Descendants a shell re-parented stay unproven.
        return False, ("no Windows Job Object, so descendants could not be proven gone"
                       if not self.detail else "")

    def _terminate_direct_only(self, timeout_s):
        try:
            self.proc.kill()
        except Exception as e:
            if self.proc.poll() is None:
                return False, "could not kill the command (%s: %s)" % (type(e).__name__, e)
        # Never confirmed: whatever this command started is in a group we are not allowed to
        # signal, so its descendants are outside what this call can prove anything about.
        if self._reap_direct(timeout_s):
            return False, ""
        return False, "the command did not exit after kill()"

    def _reap_direct(self, timeout_s) -> bool:
        try:
            self.proc.wait(timeout=max(.1, float(timeout_s)))
            return True
        except Exception:
            return False

    # -- letting go -------------------------------------------------------- #
    def release_after_success(self):
        """The FOREGROUND command finished normally. Return (extinct, detached, failed, detail).

        Only reachable from a clean completion, and that restriction is the whole design:
        `server &` is a documented use of the bash tool, so a call that ends normally must
        not kill what the model deliberately left running — and must not quietly tie it to
        Collie's own lifetime either, which is what holding a KILL_ON_JOB_CLOSE handle open
        would do (the server would die the moment `collie run` returned).

        The three answers are deliberately distinct. Extinct = the whole tree is gone.
        Detached = it deliberately is not, and the survivors are now free of Collie. Failed =
        neither, and someone has to be told. Anything else (a group we may not inspect) is
        plain ignorance about processes that were never ours, and reports nothing.

        Cancellations, timeouts and exceptions never come here; they go through terminate()
        and discard(), which keep KILL_ON_JOB_CLOSE to the end.
        """
        job, self.job = self.job, None
        if job is not None:
            try:
                live = job.active_processes()
            except Exception as e:
                # Not knowing what is in the Job means not knowing whether closing it would
                # kill something, so the handle stays open (and shut at process exit).
                return False, False, True, (
                    "the kernel Job that owns anything this command left running could not be "
                    "queried (%s: %s), so it was not handed over" % (type(e).__name__, e))
            if live == 0:
                try:
                    job.close(timeout_s=1.0)      # empty Job: a plain CloseHandle
                except Exception as e:
                    return True, False, False, "Job handle could not be closed (%s: %s)" % (
                        type(e).__name__, e)
                return True, False, False, ""
            try:
                job.release_without_terminating()
            except Exception as e:
                # Deliberately do NOT close the handle: closing it now would kill exactly the
                # processes the model meant to keep. Leaking it keeps them alive until Collie
                # exits — and the caller reports that as an uncertain effect rather than
                # claiming a background start that may not survive.
                return False, False, True, (
                    "%d process(es) the command left running could not be released from "
                    "Collie's kernel Job (%s: %s), so they will be killed when Collie exits"
                    % (live, type(e).__name__, e))
            return False, True, False, ""
        if self.pgid > 1:
            try:
                os.killpg(int(self.pgid), 0)      # the group we made; never Collie's own
            except ProcessLookupError:
                return True, False, False, ""
            except Exception:
                return False, False, False, ""    # unknowable, but nothing was killed either
            # POSIX survivors are already free of us: no kernel object ties them to Collie,
            # so a group that still answers is a background start that already succeeded.
            return False, True, False, ""
        # A shared process group (COLLIE_PROCESS_OWNER) holds Collie's own processes too, so
        # there is no tree here that is ours to account for — and an ordinary success must not
        # spend the host's uncertainty fence on that.
        return False, False, False, ""

    def discard(self) -> None:
        """Give up the handle on a path that must never spare survivors.

        Cancellation/timeout/interrupt only. If the Job is empty the handle is closed
        normally; if terminate() could not empty it, the handle is dropped WITH
        KILL_ON_JOB_CLOSE intact, so the kernel makes a last attempt when Collie exits.
        """
        job, self.job = self.job, None
        if job is None:
            return
        try:
            if job.active_processes() == 0:
                job.close(timeout_s=1.0)
        except Exception:
            pass


def _start_owned(argv, *, use_shell, cwd, env, capture_stderr, cancelled):
    """Start the command inside proven ownership. Returns (proc, owner, bootstrap|None)."""
    stdio = dict(stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
                 text=True, errors="replace")
    encoding = _output_encoding()
    if encoding:
        stdio["encoding"] = encoding
    if not plat.is_windows():
        group_kwargs = plat.new_group_kwargs()
        try:
            proc = subprocess.Popen(argv, shell=use_shell, cwd=cwd, env=env,
                                    **stdio, **group_kwargs, **plat.no_window_kwargs())
        except Exception as e:
            raise _NotStarted(LAUNCH_ERROR, str(e))
        return proc, _Owner(proc, group_kwargs), None

    # Windows: ownership BEFORE the command exists (see the module docstring).
    fd, status_path = tempfile.mkstemp(prefix="collie-tool-", suffix=".json")
    os.close(fd)
    boot = _Bootstrap(status_path)
    try:
        proc = subprocess.Popen(_bootstrap_argv(), stdin=subprocess.PIPE, cwd=cwd, env=env,
                                **stdio, **plat.no_window_kwargs())
    except Exception as e:
        boot.cleanup()
        raise _NotStarted(LAUNCH_ERROR,
                          "the command was NOT executed: its process-tree launcher could not "
                          "be started (%s: %s)" % (type(e).__name__, e))
    job = None
    retired = False
    try:
        try:
            job = plat.attach_kill_on_close_job(proc)
            if job is None:
                raise RuntimeError("no Windows Job Object was created")
        except Exception as e:
            raise _NotStarted(
                LAUNCH_ERROR,
                "the command was NOT executed: its process tree could not be owned (%s: %s), "
                "and running it unowned would mean a Stop could not reliably reach what it "
                "starts" % (type(e).__name__, e))
        # Last honest "nothing ran": ownership is proved and the command has still not been
        # let go, so a Stop that arrived during launcher startup costs the user nothing.
        if is_cancelled(cancelled):
            raise _NotStarted(PRELAUNCH_CANCELED,
                              "cancelled before the command was released to run")
        owner = _Owner(proc, {}, job=job)
        try:
            boot.release(proc, argv, use_shell, cwd)
        except Exception as e:
            launched = boot.launched()            # read the record BEFORE cleanup unlinks it
            confirmed = _abandon(proc, boot, job)
            retired = True
            why = "%s: %s" % (type(e).__name__, e)
            if launched is False:
                # The trusted launcher recorded that it never created the process. That is
                # the only evidence which permits "it did not run".
                raise _NotStarted(LAUNCH_ERROR,
                                  "the command was NOT executed: it could not be released to "
                                  "its owned process tree (%s)" % why)
            # It may well have started. Claiming otherwise is the one lie that matters here.
            raise _NotStarted(HANDOVER_ERROR,
                              "handover to the owned process tree failed (%s)" % why,
                              tree_terminated=confirmed)
    except BaseException:
        # Including KeyboardInterrupt out of is_cancelled: a bootstrap left blocked in
        # readline() with nobody to release or reap it is a stray process, and this is the
        # only window where one could exist.
        if not retired:
            _abandon(proc, boot, job)
        raise
    return proc, owner, boot


def _abandon(proc, boot, job=None) -> bool:
    """Retire a bootstrap that will never be released; return whether the tree is proved gone.

    Closing stdin is the primary act, not the kill: with no release line the bootstrap runs
    nothing and exits by itself, so the user's command cannot run even if every kill below
    fails. The Job (when we got one) is then terminated and closed with KILL_ON_JOB_CLOSE
    intact — this path may never spare anything.
    """
    confirmed = False
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        pass
    if job is not None:
        try:
            confirmed = bool(job.terminate_and_wait(timeout_s=2.0))
        except Exception:
            pass
    try:
        plat.kill_tree(proc)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    if job is not None:
        try:
            job.close(timeout_s=1.0)                 # KILL_ON_JOB_CLOSE stays on, by design
            confirmed = True
        except Exception:
            pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    boot.cleanup()
    return confirmed


def run_owned(argv, *, use_shell: bool, cwd: str, timeout_s: float, cancelled=None,
              env=None, capture_stderr: bool = True,
              poll_s: float = CANCEL_POLL_S) -> Outcome:
    """Run one owned subprocess to completion, a deadline, or a cancellation.

    The wait is a poll loop rather than ``communicate(timeout=…)`` because that call cannot
    be woken: everything about honouring Stop follows from being able to look at the host's
    flag between two sleeps. Output is drained by threads, so partial output is already in
    hand when the tree is killed — and so the timeout path never re-buffers what it already
    read (the old code called ``communicate`` a second time to drain).
    """
    t0 = time.monotonic()
    # Prelaunch: the ONLY window where "nothing ran" is a fact rather than a hope. On Windows
    # it stays a fact until the bootstrap is released, which is a little later than Popen.
    if is_cancelled(cancelled):
        return Outcome(PRELAUNCH_CANCELED, elapsed_s=0.0, tree_terminated=True,
                       detail="cancelled before the process was created")
    try:
        proc, owner, boot = _start_owned(argv, use_shell=use_shell, cwd=cwd, env=env,
                                         capture_stderr=capture_stderr, cancelled=cancelled)
    except _NotStarted as e:
        return Outcome(e.status, elapsed_s=time.monotonic() - t0,
                       tree_terminated=e.tree_terminated, detail=e.detail)
    readers = [_Reader(proc.stdout)]
    if capture_stderr and proc.stderr is not None:
        readers.append(_Reader(proc.stderr))
    for r in readers:
        r.start()
    status, confirmed, detached, failed, detail = OK, True, False, False, ""
    try:
        status = _wait(proc, readers, t0, timeout_s, cancelled, poll_s)
        if status in (CANCELED, TIMEOUT):
            confirmed, detail = owner.terminate()
            # One shared budget, not one per pipe: a tree we could not reap must not be able
            # to keep the agent loop waiting twice over.
            drain_until = time.monotonic() + DRAIN_S
            for r in readers:                     # pipes close once every writer is gone
                r.join(max(.0, drain_until - time.monotonic()))
    except BaseException:
        # KeyboardInterrupt (or anything else unwinding this thread) must not leave a shell
        # and its children running against the working tree with nobody left to reap them.
        owner.terminate()
        for r in readers:
            r.join(1.0)
        _close_pipes(proc, readers)
        owner.discard()                           # never the detaching release: this is a kill
        if boot is not None:
            boot.cleanup()
        raise
    _close_pipes(proc, readers)
    if status == OK:
        confirmed, detached, failed, detail = owner.release_after_success()
    else:
        owner.discard()
    launched = boot.launched() if boot is not None else None
    if boot is not None:
        boot.cleanup()
    if status == OK and launched is False:
        # The trusted launcher reports it never created the process — the same fact a POSIX
        # Popen raises. Anything less explicit (no record at all) is NOT this case and must
        # keep its executed status.
        return Outcome(LAUNCH_ERROR, elapsed_s=time.monotonic() - t0, tree_terminated=True,
                       stderr=readers[1].text() if len(readers) > 1 else "",
                       detail=boot.error() or "the command could not be started")
    return Outcome(status, returncode=proc.returncode,
                   stdout=readers[0].text(),
                   stderr=readers[1].text() if len(readers) > 1 else "",
                   elapsed_s=time.monotonic() - t0,
                   tree_terminated=confirmed, background_detached=detached,
                   release_failed=failed, detail=detail,
                   stdout_omitted_chars=readers[0].omitted_chars,
                   stderr_omitted_chars=readers[1].omitted_chars if len(readers) > 1 else 0)


def _wait(proc, readers, t0, timeout_s, cancelled, poll_s) -> str:
    """Honor Stop before handing off any background survivors as a normal exit."""
    deadline = t0 + max(0.0, float(timeout_s))
    while True:
        if is_cancelled(cancelled):
            return CANCELED
        # "Finished" needs BOTH: an exited shell whose pipe is still held by a backgrounded
        # grandchild is exactly the wedge the timeout exists to break, so it stays a timeout.
        if proc.poll() is not None and not any(r.is_alive() for r in readers):
            return OK
        now = time.monotonic()
        if now >= deadline:
            return TIMEOUT
        time.sleep(min(poll_s, max(.0, deadline - now)))


def own_gated_process(proc, group_kwargs):
    """Own a process whose trusted stdin gate has not released any user code.

    The caller must launch a real interpreter directly (not a Windows venv
    redirector), and keep the gate closed until this function returns.
    """
    job = plat.attach_kill_on_close_job(proc) if plat.is_windows() else None
    if plat.is_windows() and job is None:
        raise RuntimeError("could not establish Windows process-tree ownership")
    return _Owner(proc, group_kwargs, job=job)


def _close_pipes(proc, readers) -> None:
    try:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:
        pass
    if any(r.is_alive() for r in readers):
        return           # a blocked reader owns the stream; let GC close it, never yank it
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
