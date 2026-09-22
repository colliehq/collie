"""Does a worker actually behave the way its spec claims?  This is what answers that.

Every other module in the selection layer trades in *declarations*:
``HarnessSpec.caps`` says a runner can resume, ``RunnerProbe`` says it is logged
in, ``runner_select`` picks between them on that basis.  None of it is evidence.
A capability that has never been exercised on this host is a claim somebody
typed, and the failure mode is quiet: the selector offers a resumable worker, the
resume silently starts a new thread, and the receipt still says "resumed".

So this module runs the claims against the real thing and writes down what
happened.  It is deliberately two audiences at once:

* ``tests/test_runner_conformance.py`` runs the columns that need no model and no
  credential on every push.  Those columns must be green on a machine with
  neither CLI installed, which is why "not installed" is a ``SKIP`` with a reason
  and never a failure.
* ``collie runners compat --live`` runs the same table plus the columns that cost
  real tokens, and writes a report an operator reads and
  :func:`harness.runner_registry.apply_compat_report` folds back into every
  future probe — that is the loop that turns a declared capability into a
  verified one, or takes it away.

Three rules shape the code:

**Three states, never two.**  ``PASS`` / ``FAIL`` / ``SKIP(reason)``, plus
``UNVERIFIED`` for a live column nobody ran.  ``UNVERIFIED`` is not ``SKIP``: a
skip says "this cannot apply here" (no CLI, wrong phase), while unverified says
"this could have been checked and was not", and the registry downgrades
capabilities for the second but not the first.  Collapsing them would let an
unrun matrix read as a clean bill of health.  A live column whose prerequisite
column failed is the second kind (:class:`PrerequisiteFailed`): it does not run,
it does not spend a turn, and the capability it speaks for is withdrawn.

**One cell cannot take the matrix down.**  Every check runs inside its own
try/except and turns an exception into a ``FAIL`` with a redacted summary.  A
conformance run happens precisely when something is already odd about the host,
and a traceback that hides the other nine columns is the least useful possible
output.

**The report is publishable.**  It carries versions, statuses, timings and error
summaries — never a prompt, a token, an email address or a home directory path.
The report is the artefact people paste into an issue, so anything a receipt is
not allowed to contain must not be here either (see :func:`_scrub`).

The checks that need no model do not need the CLIs either: ``framing``,
``double_control`` and ``cancel`` drive the real runner objects through a
scripted transport, so they exercise the argv, the parser and the process-tree
owner without a ``codex`` or ``claude`` binary anywhere on the host.
"""
from __future__ import annotations

import json
import base64
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable

from . import __version__, plat, runner_env, runner_registry
from . import runner_specs, subscription_guard
from .agent_runners import ProcessOutcome, SubprocessRunner, _display_path
from .runner_env import BillingOverrideError
from .runner_specs import (
    BILLING_CLASSES,
    BILLING_MODE_OF,
    CANONICAL_TYPES,
    CURRENT_PHASE,
    HarnessSpec,
    RunnerProbe,
    redact_text,
    snapshot_to_run_result,
    usage_to_collie,
)

SCHEMA = "collie-runner-compat/1"

# The three states a cell can be in, plus the fourth that only exists because a
# live column can go unrun.  `runner_registry._DOWNGRADING` reads FAIL and
# UNVERIFIED; it must not read SKIP, which is why they are different words.
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
UNVERIFIED = "UNVERIFIED"

# Login states `RunnerProbe.login` is allowed to hold (catalog.probe_auth's five,
# plus "n/a" for the in-process harness that has no login of its own).
LOGIN_STATES = ("ok", "missing-key", "not-logged-in", "expired", "unknown", "n/a")

# --- the live fixture -------------------------------------------------------
# Kept in one place because none of it may reach the report: the prompt is the
# only text in this module a model ever sees, and a report that quotes it starts
# leaking task text the moment somebody parameterises the fixture.
_FIXTURE_FILE = "hello.py"
_FIXTURE_BODY = 'def hello():\n    return "hello"\n'
_ONE_TURN_MARKER = "# collie-compat"
_RESUME_MARKER = "# collie-compat-2"
_ONE_TURN_PROMPT = ("Append the single line `%s` to the end of %s. "
                    "Change nothing else, then stop." % (_ONE_TURN_MARKER, _FIXTURE_FILE))
_RESUME_PROMPT = ("Change that last line of %s to read `%s` instead. "
                  "Change nothing else, then stop." % (_FIXTURE_FILE, _RESUME_MARKER))
# Sent to a sleeping stand-in binary, never to a model.
_CANCEL_PROMPT = "collie conformance: cancel probe (no model is contacted)"

# --- decoys -----------------------------------------------------------------
# `env_hygiene` poisons a *copy* of this process's environment with these before
# building a child environment, so the check proves the stripping happened rather
# than that the developer's shell happened to be clean.  Names are the merge of
# `subscription_guard`'s forbidden inventory and `FORBIDDEN_AUTH_ENV`
# (bench/normalized_prime_pi.py:78-87); the values are recognisable markers so a
# leak into `env_receipt` is detectable by substring.
_DECOY_MARK = "collie-conformance-decoy"
_DECOY_ENV: dict[str, str] = {
    "ANTHROPIC_API_KEY": _DECOY_MARK + "-anthropic",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/" + _DECOY_MARK,
    "CLAUDE_CODE_OAUTH_TOKEN": _DECOY_MARK + "-claude-oauth",
    "OPENAI_API_KEY": _DECOY_MARK + "-openai",
    "CODEX_API_KEY": _DECOY_MARK + "-codex",
    "AWS_SECRET_ACCESS_KEY": _DECOY_MARK + "-aws",
    "GITHUB_TOKEN": _DECOY_MARK + "-github",
    "GOOGLE_APPLICATION_CREDENTIALS": _DECOY_MARK + "-google",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "NODE_OPTIONS": "--require=" + _DECOY_MARK + ".js",
    "NODE_EXTRA_CA_CERTS": _DECOY_MARK + ".pem",
    "LD_PRELOAD": _DECOY_MARK + ".so",
    "CODEX_THREAD_ID": _DECOY_MARK + "-thread",
    "CLAUDECODE": "1",
}

# Names that match a forbidden prefix and are nevertheless allowed to reach a
# child, with the reason recorded next to the exception rather than in a comment
# three files away.  Mirrors `runner_env._POLICY_PASSTHROUGH` /
# `_BILLING_ROUTE_EXEMPT`: CODEX_HOME selects which login file the CLI reads, and
# Collie's own probe honours the same variable, so stripping it would make the
# receipt's billing evidence describe a different account than the one that ran.
_PREFIX_EXEMPT = frozenset({"CODEX_HOME"})

# Words that name a second control plane.  A worker Collie drives must not be
# started with a goal list, a scheduler, a daemon or a delegation surface of its
# own: two planners on one workspace is the failure J1 exists to prevent.
_SECOND_PLANE_WORDS = ("goal", "thread", "schedule", "cron", "daemon",
                       "delegat", "kanban", "supervisor")

# Argv spellings that hand a worker more authority than the gate ever reviewed.
_FORBIDDEN_ARGV_WORDS = ("dangerously", "bypasspermissions", "yolo",
                         "--no-sandbox", "--full-auto")

# What a receipt, a log or this report must never contain.  The first pattern is
# the one `tests/test_runner_specs.py` asserts against a receipt; the second is
# here because `codex login status` prints the account's email address and this
# report is the artefact people paste into issues.
_SECRET_SCAN = re.compile(r"sk-[A-Za-z0-9_-]{4,}|sess-[A-Za-z0-9_-]{4,}"
                          r"|Bearer\s+[A-Za-z0-9._~+/=-]+|\beyJ[A-Za-z0-9_-]{6,}"
                          r"|\bghp_[A-Za-z0-9]{8,}|\bAKIA[0-9A-Z]{8,}")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*"
                    r"\.[A-Za-z]{2,}\b")

# One cell's error summary.  Long enough to name the assertion that failed,
# short enough that a table of them is still a table.
_DETAIL_LIMIT = 600

# Later-phase adapters are visible before they are selectable.  This table is
# deliberately small: it proves that the binary on PATH exposes the vendor's
# documented *programmatic* entry point.  It is not a code-signing mechanism and
# does not turn a phase-3 declaration into an enabled runner; the ordinary
# framing, isolation, billing and live-turn columns must still pass after an
# implementation is admitted.
_ADMISSION_MARKERS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = MappingProxyType({
    "pi-rpc": (("--help",), ("--mode", "rpc")),
    "prime-rpc": (("--help",), ("--mode", "rpc")),
    "hermes-gateway": (("--help",), ("--tui", "acp")),
    "hermes-acp": (("acp", "--help"), ("acp",)),
})


class SkipCheck(Exception):
    """This column cannot apply here, and that is not a failure.

    Raised with the reason a human needs: ``not installed: claude-code``,
    ``live checks disabled``.  The reason is rendered inside the cell as
    ``SKIP(reason)`` and collected into the report's ``unverified_reasons``.
    """


class PrerequisiteFailed(Exception):
    """The column this one builds on did not pass, so this one did not run.

    ``UNVERIFIED``, not ``SKIP``, and the difference is the whole point: a skip
    says "this cannot apply here" and leaves the declared capability alone, while
    a prerequisite failure means the capability *could* have been checked on this
    host and was not — the registry must take it away rather than let an unrun
    column read as evidence.  It is not a ``FAIL`` either: the column asserts
    nothing about its own runner here, and a second red cell carrying the first
    cell's error only buries the failure that actually happened.
    """


class CheckFailure(AssertionError):
    """A conformance assertion did not hold.

    An ``AssertionError`` subclass on purpose: the checks read like assertions
    and `pytest` reports one of these the way it reports a failed ``assert``,
    without this module having to use the ``assert`` statement — which ``python
    -O`` removes, and a conformance engine that silently checks nothing under
    ``-O`` would be worse than none.
    """


def _expect(condition: Any, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _scrub(text: Any, limit: int = _DETAIL_LIMIT) -> str:
    """Everything written into a report goes through here.

    ``redact_text`` masks credential-shaped substrings; the email pass exists
    because an account address is not credential-shaped and is exactly what a
    ``codex login status`` line contains.  Home directories are folded to ``~``
    by the callers that carry paths (:func:`_display_path`), so a report can be
    pasted into an issue without also publishing the operator's user name.
    """
    value = redact_text(text, limit * 4)
    value = _EMAIL.sub("[redacted-email]", value)
    value = " ".join(value.split())
    return value[:limit]


def _assert_publishable(value: Any, what: str) -> None:
    """Refuse to emit anything that looks like a credential or an address."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = repr(value)
    match = _SECRET_SCAN.search(encoded) or _EMAIL.search(encoded)
    _expect(match is None,
            "%s contains something credential- or address-shaped (%r)"
            % (what, (match.group(0)[:12] + "…") if match else ""))


# --- versions ---------------------------------------------------------------
_VERSION_NUMBERS = re.compile(r"(\d+(?:\.\d+)*)")


def _version_tuple(value: str) -> tuple[int, ...]:
    """``"claude-code 2.1.221 (Claude Code)"`` -> ``(2, 1, 221)``; ``()`` if unreadable.

    CLIs print their version inside a sentence and the sentence changes between
    releases, so the first dotted number wins rather than the whole string being
    parsed.  An empty tuple means "cannot compare", which the caller turns into a
    ``SKIP`` — claiming a version check passed on a string nobody could read is
    the failure this function exists to avoid.
    """
    match = _VERSION_NUMBERS.search(str(value or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _version_at_least(found: str, minimum: str) -> bool | None:
    left, right = _version_tuple(found), _version_tuple(minimum)
    if not left or not right:
        return None
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) >= right + (0,) * (width - len(right))


# --- process helpers --------------------------------------------------------
_ENV_ECHO = ("import json,os,sys;"
             "sys.stdout.write(json.dumps({k: v for k, v in os.environ.items()}))")

_PROBE_CHILD = ("import json,sys;"
                "from harness import runner_registry;"
                "sys.stdout.write(json.dumps(runner_registry.probe(sys.argv[1]).to_dict()))")

# Sleeps until it is killed, after announcing itself.  Stands in for an agent CLI
# in the `cancel` column: the process tree, the Job object and the start gate are
# all real, only the model is absent.
_SLEEPER = ("import os,sys,time;"
            "open(sys.argv[1], 'w').write(str(os.getpid()));"
            "time.sleep(float(sys.argv[2]))")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _capture(argv: list[str], *, env: Mapping[str, str] | None = None,
             cwd: str | None = None, timeout_s: float = 30.0) -> tuple[str, str, str]:
    """Run a short, read-only command; return ``(stdout, stderr, launch error)``.

    The two streams stay apart because one caller parses stdout as JSON and a
    Python warning on stderr would otherwise corrupt it, while the other reads
    ``--help`` text that some CLIs print to stderr.

    Never raises: a CLI that will not answer ``--help`` is an observation to
    record, not a reason to lose the other columns.
    """
    try:
        completed = subprocess.run(
            argv, cwd=cwd, env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            **plat.no_window_kwargs())
    except Exception as exc:
        return "", "", "%s: %s" % (type(exc).__name__, exc)
    return (completed.stdout or ""), (completed.stderr or ""), ""


def _capture_cli(resolved: str, args: Iterable[str], *, env: Mapping[str, str],
                 timeout_s: float = 30.0) -> tuple[str, str, str]:
    """Read a CLI, including an npm ``.cmd`` shim on Windows.

    ``CreateProcess`` cannot execute batch shims directly.  Python 3.14 also
    quotes a list-valued ``cmd /c`` argument in a way ``cmd.exe`` treats as
    literal backslashes, so this one Windows-only branch supplies the exact
    command line to a fixed ``COMSPEC`` executable.  ``shell=True`` is never
    used, and both the resolved path and arguments come from the closed adapter
    manifest rather than task text.
    """
    argv = [resolved, *[str(arg) for arg in args]]
    if os.name != "nt" or os.path.splitext(resolved)[1].lower() not in (".cmd", ".bat"):
        return _capture(argv, env=env, timeout_s=timeout_s)
    command = os.environ.get("COMSPEC") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    command_line = ('"%s" /d /s /c "%s"'
                    % (command, subprocess.list2cmdline(argv)))
    try:
        completed = subprocess.run(
            command_line, executable=command, env=dict(env),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            **plat.no_window_kwargs())
    except Exception as exc:
        return "", "", "%s: %s" % (type(exc).__name__, exc)
    return completed.stdout or "", completed.stderr or "", ""


def _gated_output(argv: list[str], env: Mapping[str, str], cwd: str,
                  timeout_s: float = 60.0) -> ProcessOutcome:
    """Run ``argv`` through the *production* transport, not a test double.

    ``env_hygiene`` uses this so its answer describes the environment a real
    worker would see: the same start gate, the same Job/process-group ownership
    and the same ``env=`` plumbing ``CodexExecRunner`` uses.
    """
    return SubprocessRunner().run(argv, cwd=cwd, stdin_text="",
                                  timeout_s=timeout_s,
                                  on_process=lambda proc: True, env=env)


class _StaticSnapshotter:
    """A workspace digest that never changes — or changes exactly once.

    The offline checks are about argv and parsing, not about mutation detection,
    and a real ``workspace_snapshot`` over a temp directory would make ``mutated``
    depend on what the OS wrote there between two calls.
    """

    def __init__(self, *digests: str):
        self._digests = list(digests) or ["d0"]

    def __call__(self, _workspace: str) -> dict[str, Any]:
        digest = self._digests[0] if len(self._digests) == 1 else self._digests.pop(0)
        return {"tree_digest": digest, "snapshot_complete": True}


class _ScriptedTransport:
    """A ``ProcessRunner`` that answers from a script instead of a CLI.

    ``builder(argv, stdin_text)`` returns the :class:`ProcessOutcome` the runner
    should see, so a check can hand back deliberately malformed output (framing)
    or simply record the argv it was asked to launch (double_control).  It is not
    a ``SubprocessRunner`` subclass, which is what makes the runners skip
    ``shutil.which`` and lets these columns run on a host with neither CLI.
    """

    def __init__(self, builder: Callable[[tuple[str, ...], str], ProcessOutcome]):
        self._builder = builder
        self.calls: list[dict[str, Any]] = []

    def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
        self.calls.append({"argv": tuple(argv), "cwd": cwd, "stdin": stdin_text,
                           "timeout_s": timeout_s, "env": dict(env or {})})
        on_process(_FakeProcess())
        return self._builder(tuple(argv), stdin_text)


class _FakeProcess:
    """Enough of a ``Popen`` for the runners' registration bookkeeping."""

    pid = 0


class _ScriptedAppServerTransport:
    """Minimal App Server peer for argv/control-plane conformance."""

    def __init__(self, thread_id: str = "thr_collie_compat"):
        from collections import deque
        self.thread_id = thread_id
        self.sent: list[dict[str, Any]] = []
        self.incoming: Any = deque()
        self._returncode = 0

    @property
    def returncode(self):
        return self._returncode

    @property
    def stderr(self):
        return ""

    def send(self, message):
        row = dict(message)
        self.sent.append(row)
        method = row.get("method")
        if method == "initialize":
            self.incoming.append({"id": row["id"], "result": {
                "userAgent": "collie-conformance", "platformFamily": "test"}})
        elif method in ("thread/start", "thread/resume"):
            self.incoming.append({"id": row["id"], "result": {
                "thread": {"id": self.thread_id}}})
        elif method == "turn/start":
            self.incoming.append({"id": row["id"], "result": {
                "turn": {"id": "turn_compat", "status": "inProgress"}}})
            self.incoming.append({"method": "turn/started", "params": {
                "threadId": self.thread_id,
                "turn": {"id": "turn_compat", "status": "inProgress"}}})
            self.incoming.append({"method": "item/completed", "params": {
                "threadId": self.thread_id, "turnId": "turn_compat",
                "item": {"id": "message_compat", "type": "agentMessage",
                         "text": "done", "phase": "final_answer"}}})
            self.incoming.append({"method": "turn/completed", "params": {
                "threadId": self.thread_id,
                "turn": {"id": "turn_compat", "status": "completed"}}})

    def receive(self, timeout_s):
        if not self.incoming:
            raise TimeoutError("scripted App Server has no queued message")
        return self.incoming.popleft()

    def terminate(self, timeout_s=5.0):
        return True

    def close(self):
        return True


class _ScriptedPiTransport:
    """Minimal Pi RPC peer for offline argv/control-plane checks."""

    def __init__(self, argv: Iterable[str]):
        from collections import deque
        self.argv = tuple(argv)
        self.sent: list[dict[str, Any]] = []
        self.incoming: Any = deque()
        self.returncode = 0
        self.stderr = ""
        self.session_id = "pi_collie_compat"
        for flag in ("--session-id", "--fork"):
            if flag in self.argv and self.argv.index(flag) + 1 < len(self.argv):
                self.session_id = self.argv[self.argv.index(flag) + 1]

    def send(self, message):
        row = dict(message)
        self.sent.append(row)
        kind = row.get("type")
        if kind == "get_state":
            data = {"sessionId": self.session_id}
        elif kind == "prompt":
            self.incoming.append({"type": "response", "id": row["id"],
                                  "command": "prompt", "success": True})
            self.incoming.append({"type": "agent_settled"})
            return
        elif kind == "get_session_stats":
            data = {"tokens": {"input": 2, "output": 1}}
        elif kind == "get_last_assistant_text":
            data = {"text": "done"}
        else:
            data = {}
        self.incoming.append({"type": "response", "id": row.get("id"),
                              "command": kind, "success": True, "data": data})

    def receive(self, timeout_s):
        if not self.incoming:
            raise TimeoutError("scripted Pi peer has no queued message")
        return self.incoming.popleft()

    def terminate(self, timeout_s=5.0):
        return True

    def close(self):
        return True


_APP_SERVER_CANCEL_PEER = r"""
import json
import os
import sys
import time

marker = sys.argv[1]
thread_id = "thr_collie_cancel"

def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    row = json.loads(line)
    method = row.get("method")
    if method == "initialize":
        emit({"id": row["id"], "result": {"userAgent": "compat-peer"}})
    elif method in ("thread/start", "thread/resume"):
        emit({"id": row["id"], "result": {"thread": {"id": thread_id}}})
    elif method == "turn/start":
        emit({"id": row["id"], "result": {"turn": {"id": "turn_cancel"}}})
        emit({"method": "turn/started", "params": {
            "threadId": thread_id, "turn": {"id": "turn_cancel", "status": "inProgress"}}})
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    elif method == "turn/interrupt":
        # Deliberately do not acknowledge: Collie must escalate to its process
        # owner and prove extinction instead of trusting a native cancel reply.
        time.sleep(180)
"""


_PI_CANCEL_PEER = r"""
import json
import os
import sys
import time

marker = sys.argv[1]

def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    row = json.loads(line)
    kind = row.get("type")
    if kind == "get_state":
        emit({"type": "response", "id": row["id"], "command": kind,
              "success": True, "data": {"sessionId": "pi_cancel_compat"}})
    elif kind == "prompt":
        emit({"type": "response", "id": row["id"], "command": kind,
              "success": True})
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    elif kind == "abort":
        # Ignore native abort. Collie must escalate and prove tree extinction.
        time.sleep(180)
"""


class _SleepTransport:
    """Real process ownership, absent CLI.

    The runner builds its argv as usual and this transport launches a sleeping
    Python child in its place, through the genuine :class:`SubprocessRunner`.
    Everything ``cancel_current`` depends on — the start gate, the Job object or
    process group, the registration latch, ``_terminate_owned_process`` — is the
    production code path; only the thing being killed is free.
    """

    def __init__(self, marker: str, seconds: float = 120.0):
        self.marker = marker
        self.seconds = float(seconds)
        self.inner = SubprocessRunner()
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
        self.calls.append(tuple(argv))
        return self.inner.run(
            [sys.executable, "-c", _SLEEPER, self.marker, "%.1f" % self.seconds],
            cwd=cwd, stdin_text=stdin_text, timeout_s=timeout_s,
            on_process=on_process, env=env)


def _instrument(runner: Any, transport: Any, snapshotter: Any = None) -> Any:
    """Point a registry-built runner at a scripted transport.

    Both runner classes take ``process_runner``/``snapshotter`` as constructor
    seams, but ``runner_registry.make_runner`` deliberately does not forward
    them: production has exactly one transport.  Replacing the attributes here
    keeps the registry as the single place that knows how to build a runner for a
    key, instead of this module growing its own copy of that table.
    """
    runner.process_runner = transport
    if snapshotter is not None:
        runner.snapshotter = snapshotter
    return runner


# --- the fixture workspace --------------------------------------------------
def _make_fixture(root: str, key: str) -> tuple[str, str]:
    """A throwaway git repo with one file in it.  Returns ``(path, note)``."""
    prefix = "collie-compat-%s-" % key.replace("/", "-")
    if plat.is_windows():
        # Python 3.13+ mkdtemp installs an owner-only ACL on Windows. Codex's
        # restricted token cannot read that fixture even when it can edit an
        # ordinary project. This directory contains only synthetic public test
        # code: create it with the scratch parent's inherited permissions.
        # Never change ACLs on an existing workspace or on the scratch parent.
        path = os.path.join(root, prefix + uuid.uuid4().hex)
        os.mkdir(path)
    else:
        path = tempfile.mkdtemp(prefix=prefix, dir=root)
    with open(os.path.join(path, _FIXTURE_FILE), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_FIXTURE_BODY)
    return path, _git_init(path)


def _git_init(path: str) -> str:
    """Best effort: a worker that expects a repo gets one; a host without git still runs."""
    git = shutil.which("git")
    if not git:
        return "no git on PATH: the fixture is a plain directory"
    env, _ = runner_env.child_env("native")
    identity = ["-c", "user.email=collie@localhost", "-c", "user.name=collie"]
    for argv in (["init", "-q"], ["add", "-A"],
                 identity + ["commit", "-qm", "conformance fixture"]):
        _out, _err, error = _capture([git] + argv, env=env, cwd=path, timeout_s=60.0)
        if error:
            return "git %s failed: %s" % (argv[0], error)
    return ""


# --- context ----------------------------------------------------------------
@dataclass
class CheckContext:
    """Everything one runner's row of checks shares.

    ``state`` is what makes the live columns affordable: ``one_turn`` leaves its
    snapshot and its runner there, ``resume`` continues that same thread and
    ``usage`` reads the tokens both of them accumulated.  Three columns, one
    billable turn each, instead of three fresh sessions.

    That sharing is also a dependency, so ``outcomes`` records what each column
    ended up saying (:func:`_cell` fills it in).  A dependent column reads it
    through :func:`_needs` instead of inferring from the leftovers in ``state``:
    a snapshot is left behind whether or not the turn it came from was any good,
    and believing one that was not is how a failed first turn used to buy a
    second model call and then a fabricated usage failure.
    """

    spec: HarnessSpec
    probe: RunnerProbe
    live: bool = False
    docker: bool = False
    scratch_root: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    # check name -> (status, detail), in the order the columns ran.
    outcomes: dict[str, tuple[str, str]] = field(default_factory=dict)
    _temp_dirs: list[str] = field(default_factory=list)

    def workspace(self, suffix: str = "ws") -> str:
        """A fresh directory the offline checks can point a runner at."""
        path = tempfile.mkdtemp(prefix="collie-compat-%s-%s-" % (self.spec.key, suffix),
                                dir=self.scratch_root or None)
        self._temp_dirs.append(path)
        return path

    def fixture(self) -> tuple[str, str]:
        path, note = _make_fixture(self.scratch_root or tempfile.gettempdir(), self.spec.key)
        self._temp_dirs.append(path)
        return path, note

    def close(self) -> None:
        for path in self._temp_dirs:
            try:
                plat.rmtree(path)
            except Exception:
                pass            # a leftover temp directory is not worth a failed run
        self._temp_dirs.clear()


def _needs(ctx: CheckContext, name: str, why: str) -> None:
    """Stop a dependent column when the one it builds on did not pass.

    The reason carries the prerequisite's own detail, so the operator reading the
    dependent cell sees the failure that actually happened rather than a second,
    derived one.  A prerequisite that was skipped skips this column too — "no CLI
    installed" is still not a fact about resuming — while a prerequisite that
    failed or went unverified leaves this column ``UNVERIFIED``, which is what
    makes the registry withdraw the capability instead of leaving it declared.

    A prerequisite that was not part of this run at all (``collie runners compat
    --checks resume``) says nothing either way; the column falls through to its
    own guards.
    """
    status, detail = ctx.outcomes.get(name, ("", ""))
    if not status or status == PASS:
        return
    reason = "%s (%s %s: %s)" % (why, name, status, _scrub(detail, 240) or "no detail")
    if status == SKIP:
        raise SkipCheck(reason)
    raise PrerequisiteFailed(reason)


# ---------------------------------------------------------------------------
# The checks.  Each one returns the detail string its cell should carry (what was
# observed, not what was asserted), raises :class:`CheckFailure` for a FAIL, or
# raises :class:`SkipCheck` with a reason.
# ---------------------------------------------------------------------------
def check_probe(ctx: CheckContext) -> str:
    """The probe row is well-formed, honest about absence, and reads no login."""
    probe, spec = ctx.probe, ctx.spec
    _expect(probe.key == spec.key,
            "probe reports key %r for spec %r" % (probe.key, spec.key))
    _expect(probe.login in LOGIN_STATES,
            "probe.login %r is not one of %s" % (probe.login, ", ".join(LOGIN_STATES)))
    _expect(probe.billing_class in BILLING_CLASSES,
            "probe.billing_class %r is not a declared class" % probe.billing_class)
    _expect(probe.ttl_s > 0, "probe.ttl_s must be positive")
    if not probe.installed:
        _expect(probe.detail.strip(),
                "an uninstalled runner must say why: probe.detail is empty")
    round_trip = RunnerProbe.from_dict(probe.to_dict())
    _expect(round_trip.to_dict() == probe.to_dict(), "RunnerProbe does not round-trip")
    _assert_publishable(probe.to_dict(), "the probe row for %s" % spec.key)

    # The second half: probe again in a child whose `~` is an empty directory.
    # A probe that still reported "ok" there would have been reading something
    # other than the login it claims to describe.
    empty_home = ctx.workspace("home")
    env, _receipt = runner_env.child_env("native", home=empty_home)
    stdout, stderr, error = _capture([sys.executable, "-c", _PROBE_CHILD, spec.key],
                                     env=env, cwd=_REPO_ROOT, timeout_s=90.0)
    _expect(not error, "probing %s against an empty HOME failed: %s" % (spec.key, error))
    try:
        blank = json.loads(stdout.strip() or "{}")
    except (ValueError, json.JSONDecodeError):
        raise CheckFailure("probing %s against an empty HOME printed no probe row: %s"
                           % (spec.key, _scrub(stdout or stderr))) from None
    _expect(isinstance(blank, dict) and blank.get("key") == spec.key,
            "the empty-HOME probe returned %s" % _scrub(stdout or stderr))
    if spec.kind != "native":
        _expect(blank.get("login") != "ok",
                "the empty-HOME probe still reported login=ok, so it read a credential "
                "store outside the home it was given")
    return "installed=%s version=%s login=%s empty-HOME login=%s" % (
        probe.installed, probe.version or "-", probe.login, blank.get("login"))


def check_admission(ctx: CheckContext) -> str:
    """Corroborate a declared adapter's CLI and documented protocol surface.

    Current-phase runners already have a production probe and implementation, so
    their admission evidence is the spec/probe pair.  A future runner is never
    constructed here: only ``--version`` and ``--help`` are read, with stdin
    closed and the same stripped child environment a real launch would receive.
    That makes the check cheap enough for CI while keeping "binary found" very
    different from "adapter enabled".
    """
    spec, probe = ctx.spec, ctx.probe
    if spec.kind == "native":
        return "native control row; adapter admission does not apply"
    if spec.phase <= CURRENT_PHASE:
        _expect(spec.caps.protocol.strip(), "%s declares no protocol" % spec.key)
        if not probe.installed:
            raise SkipCheck("not installed: %s" % spec.key)
        _expect(probe.version.strip(), "%s is installed but reports no version" % spec.key)
        return "current phase; protocol=%s version=%s" % (spec.caps.protocol, probe.version)

    resolved = shutil.which(spec.binary) if spec.binary else ""
    if not resolved:
        raise SkipCheck("phase %d admission: binary not installed: %s"
                        % (spec.phase, spec.binary or spec.key))
    env, _receipt = runner_env.child_env(spec.env_policy)
    declared_version = list(spec.version_argv or (spec.binary, "--version"))
    version_args = declared_version[1:] if declared_version else ["--version"]
    out, err, error = _capture_cli(
        resolved, version_args, env=env, timeout_s=30.0)
    if error:
        raise CheckFailure("phase %d admission could not read version: %s"
                           % (spec.phase, _scrub(error, 160)))
    version = (out or err).strip().splitlines()
    _expect(bool(version), "%s is installed but its version command was silent" % spec.key)

    admission = _ADMISSION_MARKERS.get(spec.key)
    if admission is None:
        raise SkipCheck("phase %d admission has no reviewed protocol fingerprint for %s"
                        % (spec.phase, spec.key))
    help_argv, markers = admission
    help_out, help_err, help_error = _capture_cli(
        resolved, help_argv, env=env, timeout_s=30.0)
    if help_error:
        raise CheckFailure("phase %d admission could not read protocol help: %s"
                           % (spec.phase, _scrub(help_error, 160)))
    help_text = (help_out + "\n" + help_err).lower()
    missing = [marker for marker in markers if marker.lower() not in help_text]
    _expect(not missing,
            "%s does not expose the reviewed %s surface; missing help marker(s): %s"
            % (spec.key, spec.caps.protocol, ", ".join(missing)))
    return ("phase %d candidate only; binary=%s version=%s; protocol markers=%s; "
            "selection remains disabled" %
            (spec.phase, os.path.basename(resolved), _scrub(version[0], 80),
             ",".join(markers)))


def check_env_hygiene(ctx: CheckContext) -> str:
    """A worker's environment is the allowlist and nothing else — proven in a real child."""
    spec = ctx.spec
    policy = spec.env_policy
    family = spec.credential_family

    # A *copy* of this process's environment, poisoned.  Never os.environ itself:
    # a check that mutates the process it runs in is a bug waiting for the next
    # thread, and `child_env(environ=…)` exists exactly so it does not have to.
    parent = dict(os.environ)
    parent.update(_DECOY_ENV)

    env, receipt = runner_env.child_env(policy, environ=parent)
    allowed = set(runner_env.allowlist(policy)) | {"NO_COLOR"}
    unexpected = sorted(set(env) - allowed)
    _expect(not unexpected,
            "child_env(%r) passed names that are not on its allowlist: %s"
            % (policy, ", ".join(unexpected)))

    forbidden = set(subscription_guard._FORBIDDEN_ENV_NAMES) | set(_DECOY_ENV)
    prefixes = tuple(subscription_guard._FORBIDDEN_ENV_PREFIXES.get(
        spec.guard_alias or "", ())) or ("ANTHROPIC_", "CLAUDE_", "OPENAI_",
                                         "CODEX_", "AZURE_OPENAI_")
    leaked = sorted(name for name in env
                    if name in forbidden
                    or (name.startswith(prefixes) and name not in _PREFIX_EXEMPT))
    _expect(not leaked,
            "child_env(%r) would hand a %s worker: %s" % (policy, family or "external",
                                                          ", ".join(leaked)))

    _expect(set(receipt) == {"allowed", "stripped"},
            "env_receipt has keys %s" % sorted(receipt))
    _expect(receipt["allowed"] == sorted(env),
            "env_receipt.allowed does not describe the environment that was built")
    missed = sorted(name for name in _DECOY_ENV if name not in receipt["stripped"])
    _expect(not missed, "env_receipt.stripped does not record %s" % ", ".join(missed))
    encoded = json.dumps(receipt)
    _expect(_DECOY_MARK not in encoded,
            "env_receipt carries a value, not just key names")
    _assert_publishable(receipt, "the env receipt for %s" % spec.key)

    # The start-time refusal, from both directions.
    refused = False
    try:
        runner_env.assert_no_billing_override(parent, family)
    except BillingOverrideError as exc:
        refused = True
        _expect(_DECOY_MARK not in str(exc),
                "the billing-override refusal quotes a value; it may name only variables")
    _expect(refused,
            "assert_no_billing_override accepted a parent environment holding %s"
            % ", ".join(sorted(_DECOY_ENV)))
    runner_env.assert_no_billing_override({}, family)   # a clean shell must start

    # And now the same environment, observed from inside a real gated child.
    workspace = ctx.workspace("env")
    outcome = _gated_output([sys.executable, "-c", _ENV_ECHO], env, workspace)
    _expect(outcome.exit_code == 0,
            "the environment echo child exited %s: %s"
            % (outcome.exit_code, _scrub(outcome.stderr)))
    try:
        observed = json.loads((outcome.stdout or "").strip() or "{}")
    except (ValueError, json.JSONDecodeError):
        raise CheckFailure("the environment echo child printed %s"
                           % _scrub(outcome.stdout)) from None
    names = {str(name).upper() for name in observed}
    child_leaked = sorted(name for name in names
                          if name in forbidden
                          or (name.startswith(prefixes) and name not in _PREFIX_EXEMPT))
    _expect(not child_leaked,
            "a real child process received %s" % ", ".join(child_leaked))
    _expect(_DECOY_MARK not in json.dumps(observed),
            "a decoy value reached the child under a different name")
    return "policy=%s allowed=%d stripped=%d child_env=%d names" % (
        policy, len(receipt["allowed"]), len(receipt["stripped"]), len(names))


def check_handshake(ctx: CheckContext) -> str:
    """The CLI on this host is one this layer was written against."""
    spec, probe = ctx.spec, ctx.probe
    if spec.kind == "native":
        _expect(probe.version == __version__,
                "the native harness reports version %r, not %r"
                % (probe.version, __version__))
        return "in-process harness %s; no CLI handshake exists" % __version__
    if not probe.installed:
        raise SkipCheck("not installed: %s" % spec.key)
    _expect(probe.version.strip(),
            "%s is installed but would not report a version: %s"
            % (spec.key, probe.detail or "(no detail)"))

    notes = ["version=%s" % probe.version]
    if spec.min_version:
        verdict = _version_at_least(probe.version, spec.min_version)
        if verdict is None:
            raise SkipCheck("cannot compare %r with the pinned minimum %s"
                            % (_scrub(probe.version, 80), spec.min_version))
        _expect(verdict, "%s %s is older than the pinned minimum %s"
                         % (spec.key, probe.version, spec.min_version))
        notes.append("min=%s ok" % spec.min_version)

    # The unverified flags §G.4 names, settled the cheap way: `--help` lists what
    # the build accepts, and reading it costs no tokens and no login.  These are
    # recorded, never asserted — the runners deliberately pass none of them, so a
    # build that drops one must not turn this column red.
    env, _receipt = runner_env.child_env(spec.env_policy)
    executable = probe.executable_path or spec.binary
    if spec.key == "claude-code":
        out, err, error = _capture([executable, "--help"], env=env, timeout_s=60.0)
        notes.append(_flag_note(out + err, error, ("--max-turns", "--setting-sources")))
    elif spec.key == "codex-exec":
        out, err, error = _capture([executable, "exec", "resume", "--help"],
                                   env=env, timeout_s=60.0)
        notes.append(_flag_note(out + err, error,
                                ("--ignore-user-config", "--ignore-rules")))
    return "; ".join(part for part in notes if part)


def _flag_note(help_text: str, error: str, flags: Iterable[str]) -> str:
    if error:
        return "could not read --help: %s" % _scrub(error, 120)
    seen = [flag for flag in flags if flag in help_text]
    missing = [flag for flag in flags if flag not in help_text]
    parts = []
    if seen:
        parts.append("accepts " + ", ".join(seen))
    if missing:
        parts.append("no " + ", ".join(missing) + " in --help")
    return "; ".join(parts)


def check_framing(ctx: CheckContext) -> str:
    """Chaotic output produces a reported error, never an exception and never a lie."""
    spec = ctx.spec
    if spec.kind == "native":
        raise SkipCheck("the native harness has no external frame transport")

    if spec.key in ("codex-app-server", "pi-rpc"):
        # Both adapters use the same owned bounded LFJSONL transport.  Protocol
        # correlation is exercised by their runner-specific scripted peers;
        # this column answers the byte-framing question once at the shared seam.
        return _check_rpc_stdio_framing(ctx)

    workspace = ctx.workspace("frames")
    observations: list[str] = []
    for case, builder, expected in _framing_cases(spec.key):
        transport = _ScriptedTransport(builder)
        runner = _instrument(runner_registry.make_runner(spec.key, timeout_s=60.0),
                             transport, _StaticSnapshotter("d0"))
        # A crash here is the failure this column exists to catch, so the
        # exception is allowed to propagate into the cell's FAIL.
        snapshot = runner.start(_CANCEL_PROMPT, workspace)

        _expect(snapshot.runner == spec.key,
                "%s: the snapshot claims runner %r" % (case, snapshot.runner))
        if expected == "settled":
            _expect(snapshot.settled,
                    "%s: a well-formed stream in this shape did not settle: %s"
                    % (case, _scrub(snapshot.error, 200)))
            observations.append("%s=settled" % case)
            continue
        _expect(not snapshot.settled,
                "%s: malformed output was reported as a settled turn" % case)
        _expect(snapshot.error.strip(),
                "%s: the turn failed without saying why" % case)
        if expected == "error_event":
            # A stream that produced *something* must also produce the event that
            # phase 2 translates into `runner.error`; a stream that produced
            # nothing at all has nothing to report an event about, and the error
            # on the snapshot is the whole answer.
            kinds = {event.type for event in snapshot.events}
            _expect("protocol.invalid_json" in kinds,
                    "%s: no protocol error event was recorded; got %s"
                    % (case, ", ".join(sorted(kinds)) or "(none)"))
        observations.append("%s=%s" % (case, expected))

    # The native event name above is what phase 2's translator maps onto the
    # canonical vocabulary; asserting the target exists keeps the two ends of that
    # mapping from drifting apart before the translator is written.
    _expect("runner.error" in CANONICAL_TYPES,
            "runner.error is missing from CANONICAL_TYPES")
    observations.append("LF-only framing keeps literal U+2028/U+2029 inside JSON strings")
    return "; ".join(observations)


def _check_rpc_stdio_framing(ctx: CheckContext) -> str:
    """Exercise the real bounded stdio reader against byte-level JSONL cases."""
    from .codex_app_server_runner import AppServerStdioTransport

    valid = json.dumps({"method": "turn/started", "params": {
        "text": "first\u2028second"}}, ensure_ascii=False)
    cases = (
        ("lf", (valid + "\n").encode("utf-8"), True),
        ("crlf", (valid + "\r\n").encode("utf-8"), True),
        ("chunked", (valid + "\n").encode("utf-8"), True),
        ("u2028", (valid + "\n").encode("utf-8"), True),
        ("no_trailing_lf", valid.encode("utf-8"), False),
        ("invalid_json", b"{not json\n", False),
        ("not_an_object", b"[1,2,3]\n", False),
        ("nul_and_noise", b"\x00\x01binary noise\n", False),
        ("empty", b"", False),
    )
    observations: list[str] = []
    env, _receipt = runner_env.child_env("native")
    workspace = ctx.workspace("appserver-frames")
    for name, payload, should_pass in cases:
        wire = base64.b64encode(payload).decode("ascii")
        split = max(1, len(payload) // 2) if name == "chunked" else len(payload)
        script = (
            "import base64,sys,time;"
            "b=base64.b64decode(sys.argv[1]);n=int(sys.argv[2]);"
            "sys.stdout.buffer.write(b[:n]);sys.stdout.buffer.flush();"
            "time.sleep(0.03);sys.stdout.buffer.write(b[n:]);sys.stdout.buffer.flush()"
        )
        transport = AppServerStdioTransport(
            [sys.executable, "-u", "-c", script, wire, str(split)],
            cwd=workspace, env=env, max_wire_chars=65_536)
        passed = False
        try:
            message = transport.receive(5.0)
            passed = isinstance(message, dict)
        except (runner_specs.RunnerProtocolError, EOFError):
            passed = False
        finally:
            transport.close()
        _expect(passed == should_pass,
                "%s: strict %s JSONL %s unexpectedly" %
                (name, ctx.spec.key, "passed" if passed else "failed"))
        observations.append("%s=%s" %
                            (name, "settled" if passed else "error_event"))
    observations.append("LF-only framing keeps literal U+2028/U+2029 inside JSON strings")
    return "; ".join(observations)


def _framing_cases(key: str):
    """``(case name, outcome builder, expectation)`` for one dialect.

    The expectation is ``"settled"`` (this shape is legal and must parse),
    ``"error_event"`` (illegal: report it as a runner error *and* record the
    event) or ``"error"`` (illegal and empty: there is nothing to raise an event
    about, so only the reported error is required).
    """
    if key == "codex-exec":
        thread = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        events = [
            {"type": "thread.started", "thread_id": thread},
            {"type": "turn.started"},
            {"type": "item.completed",
             "item": {"id": "item_1", "type": "agent_message", "text": "done"}},
            {"type": "turn.completed",
             "usage": {"input_tokens": 10, "cached_input_tokens": 4,
                       "output_tokens": 2, "reasoning_output_tokens": 1}},
        ]
        lines = [json.dumps(event) for event in events]
        whole = "\n".join(lines) + "\n"

        def out(text: str) -> Callable[..., ProcessOutcome]:
            return lambda argv, stdin: ProcessOutcome(stdout=text, exit_code=0)

        def chunked(_argv, _stdin) -> ProcessOutcome:
            # The phase-1 dialects parse a complete buffer, so arbitrary chunk
            # boundaries are reassembled before the parser sees them.  Feeding
            # them anyway is what proves the boundary is not load-bearing; the
            # streaming case belongs to `runner_jsonrpc` in phase 3.
            size = 7
            pieces = [whole[i:i + size] for i in range(0, len(whole), size)]
            return ProcessOutcome(stdout="".join(pieces), exit_code=0)

        # ensure_ascii=False on purpose: this frame is only interesting if the
        # separator reaches the parser as a literal U+2028 rather than as the
        # six characters an escaped encoding would print.
        u2028 = json.dumps({"type": "item.completed",
                            "item": {"id": "i", "type": "agent_message",
                                     "text": "first\u2028second"}},
                           ensure_ascii=False)
        return [
            ("lf", out(whole), "settled"),
            ("crlf", out("\r\n".join(lines) + "\r\n"), "settled"),
            ("no_trailing_lf", out("\n".join(lines)), "settled"),
            ("chunked", chunked, "settled"),
            ("invalid_json",
             out("\n".join(lines[:1] + ["{not json"] + lines[1:])), "error_event"),
            # Codex JSONL is LF-delimited. Literal U+2028/U+2029 inside a JSON
            # string are data, not record boundaries (the same strict framing
            # rule used by Prime/Pi RPC), so this stream remains valid.
            ("u2028", out("\n".join([lines[0], u2028] + lines[1:])), "settled"),
            ("not_an_object", out(lines[0] + "\n[1,2,3]\n"), "error_event"),
            ("nul_and_noise",
             out(lines[0] + "\n\x00\x01binary noise\n"), "error_event"),
            ("empty", out(""), "error"),
        ]

    if key == "claude-code":
        def result(argv: tuple[str, ...]) -> dict[str, Any]:
            session = ""
            for index, token in enumerate(argv):
                if token in ("--session-id", "--resume") and index + 1 < len(argv):
                    session = argv[index + 1]
            return {"type": "result", "subtype": "success", "is_error": False,
                    "result": "done", "session_id": session,
                    "usage": {"input_tokens": 11, "output_tokens": 3,
                              "cache_read_input_tokens": 1,
                              "cache_creation_input_tokens": 2},
                    "total_cost_usd": 0.0123}

        def whole(argv, _stdin) -> ProcessOutcome:
            return ProcessOutcome(stdout=json.dumps(result(argv)) + "\n", exit_code=0)

        def crlf(argv, _stdin) -> ProcessOutcome:
            return ProcessOutcome(stdout="\r\n" + json.dumps(result(argv)) + "\r\n",
                                  exit_code=0)

        def no_lf(argv, _stdin) -> ProcessOutcome:
            return ProcessOutcome(stdout=json.dumps(result(argv)), exit_code=0)

        def chunked(argv, _stdin) -> ProcessOutcome:
            text = json.dumps(result(argv))
            return ProcessOutcome(stdout="".join(text[i:i + 5]
                                                 for i in range(0, len(text), 5)),
                                  exit_code=0)

        def u2028(argv, _stdin) -> ProcessOutcome:
            payload = result(argv)
            payload["result"] = "line one\u2028line two"
            return ProcessOutcome(stdout=json.dumps(payload, ensure_ascii=False),
                                  exit_code=0)

        def doubled(argv, _stdin) -> ProcessOutcome:
            text = json.dumps(result(argv))
            return ProcessOutcome(stdout=text + "\n" + text + "\n", exit_code=0)

        def out(text: str):
            return lambda argv, stdin: ProcessOutcome(stdout=text, exit_code=0)

        return [
            ("lf", whole, "settled"),
            ("crlf", crlf, "settled"),
            ("no_trailing_lf", no_lf, "settled"),
            ("chunked", chunked, "settled"),
            # U+2028 is an ordinary character inside a JSON string, so a
            # single-object dialect keeps it: this case proves the record
            # boundary question is dialect-specific, not universal.
            ("u2028", u2028, "settled"),
            ("two_objects", doubled, "error_event"),
            ("invalid_json", out("{not json\n"), "error_event"),
            ("not_an_object", out("[1,2,3]\n"), "error_event"),
            ("nul_and_noise", out("\x00\x01binary noise\n"), "error_event"),
            ("empty", out(""), "error"),
        ]

    if key == "codex-sdk":
        thread = "thr_codex_sdk_compat"
        records = [
            {"type": "thread.started", "thread_id": thread},
            {"type": "collie.sdk.result", "status": "completed",
             "thread_id": thread, "final_output": "done",
             "usage": {"input_tokens": 3, "output_tokens": 1}},
        ]
        lines = [json.dumps(record) for record in records]

        def out(text: str):
            return lambda argv, stdin: ProcessOutcome(stdout=text, exit_code=0)

        whole = "\n".join(lines) + "\n"
        u2028_records = [dict(records[0]), dict(records[1])]
        u2028_records[1]["final_output"] = "first\u2028second"
        return [
            ("lf", out(whole), "settled"),
            ("crlf", out("\r\n".join(lines) + "\r\n"), "settled"),
            ("no_trailing_lf", out("\n".join(lines)), "settled"),
            ("chunked", out("".join(whole[i:i + 5]
                                      for i in range(0, len(whole), 5))), "settled"),
            ("u2028", out("\n".join(json.dumps(item, ensure_ascii=False)
                                      for item in u2028_records)), "settled"),
            ("invalid_json", out(lines[0] + "\n{not json\n" + lines[1]),
             "error_event"),
            ("not_an_object", out(lines[0] + "\n[1,2,3]\n" + lines[1]),
             "error_event"),
            ("nul_and_noise", out(lines[0] + "\n\x00\x01binary noise\n" + lines[1]),
             "error_event"),
            ("empty", out(""), "error"),
        ]

    raise SkipCheck("no frame dialect is implemented for %s in this phase" % key)


def check_double_control(ctx: CheckContext) -> str:
    """Nothing Collie starts is allowed to bring its own planner, scheduler or daemon."""
    spec = ctx.spec
    caps = spec.caps
    if spec.kind == "native":
        _expect(not caps.native_goal and not caps.native_scheduler,
                "the native harness must not declare a second control plane")
        return ("collie is the control plane; there is no second one to disable "
                "(native_goal=False, native_scheduler=False)")

    if spec.key == "codex-app-server":
        return _check_app_server_double_control(ctx)
    if spec.key == "pi-rpc":
        return _check_pi_double_control(ctx)

    _expect(not caps.native_goal,
            "%s declares native_goal: phase 1 has no way to prove a goals surface "
            "is disabled, so it may not be offered" % spec.key)
    _expect(not caps.native_scheduler,
            "%s declares native_scheduler: a worker that can schedule its own work "
            "outlives the slice that started it" % spec.key)

    workspace = ctx.workspace("argv")
    transport = _ScriptedTransport(
        lambda argv, _stdin: _settled_outcome(spec.key, argv))
    runner = _instrument(runner_registry.make_runner(spec.key, timeout_s=60.0),
                         transport, _StaticSnapshotter("d0"))
    snapshot = runner.start(_CANCEL_PROMPT, workspace)
    start_argv = transport.calls[-1]["argv"]
    _check_argv(spec.key, start_argv, resume=False)

    # Resume is the half that drifts: `codex exec resume` and `claude --resume`
    # take different flags than their start counterparts, and a hardening flag
    # dropped there would put the *second* turn back on the host's configuration.
    resumed = ""
    try:
        runner.resume(snapshot, _CANCEL_PROMPT)
        _check_argv(spec.key, transport.calls[-1]["argv"], resume=True)
        resumed = "resume argv checked"
    except CheckFailure:
        raise
    except Exception as exc:
        resumed = "resume argv not reachable offline: %s" % _scrub(exc, 120)
    return "start argv checked (%d tokens); %s; confinement=%s" % (
        len(start_argv), resumed, caps.confinement)


def _check_app_server_double_control(ctx: CheckContext) -> str:
    """Prove App Server's goal surface exists but is never invoked by Collie."""
    from .codex_app_server_runner import CodexAppServerRunner

    workspace = ctx.workspace("appserver-argv")
    transports: list[_ScriptedAppServerTransport] = []
    launches: list[tuple[str, ...]] = []

    def factory(argv, _cwd, _env):
        launches.append(tuple(argv))
        transport = _ScriptedAppServerTransport()
        transports.append(transport)
        return transport

    runner = CodexAppServerRunner(
        executable="codex", default_timeout_s=60.0,
        transport_factory=factory, snapshotter=_StaticSnapshotter("d0"))
    first = runner.start(_CANCEL_PROMPT, workspace)
    second = runner.resume(first, _CANCEL_PROMPT)
    _expect(first.settled and second.settled,
            "scripted App Server start/resume did not settle")
    for argv in launches:
        _check_argv(ctx.spec.key, argv, resume=False)
    methods = [str(row.get("method") or "")
               for transport in transports for row in transport.sent]
    _expect(not any(method.startswith("thread/goal/") for method in methods),
            "App Server runner invoked its native goal control plane")
    allowed = {"initialize", "initialized", "thread/start", "thread/resume",
               "turn/start"}
    unexpected = sorted({method for method in methods if method and method not in allowed})
    _expect(not unexpected, "App Server runner invoked unexpected methods: %s"
            % ", ".join(unexpected))
    for transport in transports:
        starts = [row for row in transport.sent
                  if row.get("method") in ("thread/start", "thread/resume")]
        _expect(starts and starts[0].get("params", {}).get("sandbox") == "workspace-write",
                "App Server thread start/resume did not pin workspace-write")
        _expect(starts[0].get("params", {}).get("approvalPolicy") == "on-request",
                "App Server thread start/resume did not pin on-request approvals")
    return ("start argv checked; resume argv checked; native thread/goal unused; "
            "host extensions disabled; confinement=workspace-write")


def _check_pi_double_control(ctx: CheckContext) -> str:
    """Drive start/resume through Pi's real adapter and inspect both launches."""
    from .pi_rpc_runner import PiRpcRunner

    workspace = ctx.workspace("pi-argv")
    launches: list[tuple[str, ...]] = []
    transports: list[_ScriptedPiTransport] = []

    def factory(argv, _cwd, _env):
        launches.append(tuple(argv))
        peer = _ScriptedPiTransport(argv)
        transports.append(peer)
        return peer

    runner = PiRpcRunner(default_timeout_s=60.0, transport_factory=factory,
                         snapshotter=_StaticSnapshotter("d0"))
    first = runner.start(_CANCEL_PROMPT, workspace)
    second = runner.resume(first, _CANCEL_PROMPT)
    _expect(first.settled and second.settled,
            "scripted Pi start/resume did not settle")
    _expect(len(launches) == 2, "Pi did not produce start and resume launch lines")
    _check_argv(ctx.spec.key, launches[0], resume=False)
    _check_argv(ctx.spec.key, launches[1], resume=True)
    sent_types = {str(row.get("type") or "")
                  for peer in transports for row in peer.sent}
    forbidden = sorted(sent_types.intersection(
        {"set_model", "spawn", "schedule", "cron", "daemon"}))
    _expect(not forbidden, "Pi invoked a second control plane: %s" %
            ", ".join(forbidden))
    return ("start argv checked; resume argv checked; extensions/skills/context "
            "disabled; shell absent; confinement=tools-allowlist")


def _settled_outcome(key: str, argv: tuple[str, ...]) -> ProcessOutcome:
    """A minimal well-formed reply in ``key``'s dialect, so a turn can settle."""
    cases = dict((name, builder) for name, builder, _expected in _framing_cases(key))
    return cases["lf"](argv, "")


def _check_argv(key: str, argv: tuple[str, ...], *, resume: bool) -> None:
    """Assert one launch line disables every surface Collie must own itself."""
    flags = [token for token in argv[1:]
             if token.startswith("-") and token not in ("-", "-c")]
    overrides = [argv[index + 1] for index, token in enumerate(argv)
                 if token == "-c" and index + 1 < len(argv)]
    # Values (a workspace path, a session uuid, the prompt sentinel) are excluded
    # on purpose: a repository that happens to live in `~/goals` must not fail a
    # check about what the *worker* was told it may do.
    inspected = flags + overrides
    lowered = [token.lower() for token in inspected]
    for token in lowered:
        for word in _SECOND_PLANE_WORDS:
            if word in token:
                raise CheckFailure(
                    "%s launch line names a second control plane: %r" % (key, token))
        for word in _FORBIDDEN_ARGV_WORDS:
            if word in token:
                raise CheckFailure(
                    "%s launch line asks for unreviewed authority: %r" % (key, token))

    joined = " ".join(inspected)
    if key == "codex-exec":
        for required in ("--ignore-user-config", "--ignore-rules"):
            _expect(required in flags,
                    "codex %s argv is missing %s: the host's own configuration "
                    "would reach the worker" % ("resume" if resume else "start", required))
        _expect("--strict-config" in flags,
                "codex %s argv is missing --strict-config" %
                ("resume" if resume else "start"))
        _expect('web_search="disabled"' in overrides,
                "codex argv does not disable web_search")
        # The design's `--ask-for-approval never` is spelled as a `-c` override
        # because 0.149.0 rejects the flag outright; either spelling satisfies
        # the requirement, which is that exec fails closed rather than blocking.
        _expect('approval_policy="never"' in overrides
                or ("--ask-for-approval" in flags and "never" in argv),
                "codex argv does not pin approval_policy to never")
        _expect("--ephemeral" not in flags,
                "codex argv passes --ephemeral, which discards the thread resume needs")
        if resume:
            _expect('sandbox_mode="workspace-write"' in overrides,
                    "codex resume argv does not re-pin the sandbox")
        else:
            _expect("--sandbox" in flags and "workspace-write" in argv,
                    "codex start argv does not pin --sandbox workspace-write")
    elif key == "codex-app-server":
        _expect(len(argv) >= 4 and argv[1:4] ==
                ("app-server", "--stdio", "--strict-config"),
                "App Server argv does not pin local stdio + strict config")
        required = {
            "mcp_servers={}", "plugins={}", 'web_search="disabled"',
            "project_doc_max_bytes=0", "features.hooks=false",
            "features.memories=false", "features.multi_agent=false",
            "features.apps=false",
        }
        missing = sorted(required - set(overrides))
        _expect(not missing, "App Server argv leaves host surfaces enabled: %s"
                % ", ".join(missing))
    elif key == "codex-sdk":
        _expect(len(argv) == 2 and os.path.basename(argv[1]) == "codex_sdk_worker.py",
                "Codex SDK does not launch the pinned isolated sidecar")
    elif key == "claude-code":
        _expect("--strict-mcp-config" in flags,
                "claude argv does not exclude the user's MCP servers")
        _expect("--safe-mode" in flags and "--no-chrome" in flags,
                "claude argv does not disable host customizations/Chrome")
        _expect("--disable-slash-commands" in flags,
                "claude argv does not disable skills and slash commands")
        _expect("--prompt-suggestions" in flags and
                argv[argv.index("--prompt-suggestions") + 1] == "false",
                "claude argv does not disable prompt suggestions")
        _expect("--permission-mode" in argv
                and argv[argv.index("--permission-mode") + 1] == "acceptEdits",
                "claude argv does not pin --permission-mode acceptEdits")
        _expect("--max-turns" not in flags,
                "claude argv passes --max-turns, whose semantics are unverified")
        tools = argv[argv.index("--tools") + 1] if "--tools" in argv else ""
        for banned in ("Bash", "Task", "Agent", "WebFetch", "WebSearch"):
            _expect(banned not in tools,
                    "claude tool allowlist contains %s, which has no approval channel "
                    "back into Collie's gate" % banned)
        if resume:
            _expect("--resume" in flags and "--session-id" not in flags,
                    "claude resume argv does not continue the pinned session")
            _expect("--fork-session" not in flags,
                    "claude resume argv forks the session the snapshot names")
        _expect("bypasspermissions" not in joined.lower(),
                "claude argv asks for bypassPermissions")
    elif key == "pi-rpc":
        required = {"--mode", "--tools", "--no-extensions", "--no-skills",
                    "--no-prompt-templates", "--no-context-files", "--no-approve"}
        missing = sorted(required - set(flags))
        _expect(not missing, "Pi argv leaves host surfaces enabled: %s" %
                ", ".join(missing))
        _expect(argv[argv.index("--mode") + 1] == "rpc",
                "Pi launch does not pin RPC mode")
        tools = argv[argv.index("--tools") + 1].split(",")
        _expect("bash" not in tools and "shell" not in tools,
                "Pi tool allowlist enables an unreviewed shell")
        _expect(set(tools) == {"read", "edit", "write", "grep", "find", "ls"},
                "Pi tool allowlist drifted: %s" % ",".join(tools))
        _expect("--session-id" in argv and "--fork" not in argv,
                "Pi start/resume must address the exact session without forking")


def check_billing(ctx: CheckContext) -> str:
    """Which account pays is a declared class with a mode that agrees with it."""
    spec, probe = ctx.spec, ctx.probe
    _expect(set(BILLING_MODE_OF) == set(BILLING_CLASSES),
            "BILLING_MODE_OF does not cover every billing class")
    _expect(probe.billing_class in BILLING_CLASSES,
            "probe.billing_class %r is not declared" % probe.billing_class)
    _expect(BILLING_MODE_OF[probe.billing_class] == probe.billing_mode,
            "billing_class=%s but billing_mode=%s" % (probe.billing_class,
                                                      probe.billing_mode))
    _expect(not probe.overage_attested,
            "overage_attested is an operator attestation and must never be set by a probe")
    if spec.guard_alias:
        _expect(spec.guard_alias in subscription_guard._FORBIDDEN_ENV_PREFIXES,
                "guard alias %r has no environment policy in subscription_guard"
                % spec.guard_alias)
    _assert_publishable(probe.billing_evidence,
                        "the billing evidence for %s" % spec.key)
    note = ""
    if probe.billing_class == "unknown":
        # Not a failure: without --live nothing may claim to know which plan pays,
        # and H5 refuses an unknown route under --no-paid-overage on its own.
        note = "; unknown until `collie runners probe %s --live` says otherwise" % spec.key
    return "class=%s mode=%s guard=%s%s" % (probe.billing_class, probe.billing_mode,
                                            spec.guard_alias or "-", note)


def check_cancel(ctx: CheckContext) -> str:
    """``cancel_current()`` kills the owned tree within five seconds, or says it could not."""
    spec = ctx.spec
    if spec.kind == "native":
        raise SkipCheck("the native harness cancels through its own gate, not a "
                        "process tree it owns")
    if spec.key == "codex-app-server":
        return _check_app_server_cancel(ctx)
    if spec.key == "pi-rpc":
        return _check_pi_cancel(ctx)

    workspace = ctx.workspace("cancel")
    marker = os.path.join(workspace, "child.pid")
    transport = _SleepTransport(marker, seconds=180.0)
    runner = _instrument(runner_registry.make_runner(spec.key, timeout_s=120.0),
                         transport, _StaticSnapshotter("d0"))

    outcome: dict[str, Any] = {}

    def turn() -> None:
        try:
            outcome["snapshot"] = runner.start(_CANCEL_PROMPT, workspace)
        except BaseException as exc:            # reported below, never swallowed
            outcome["error"] = exc

    worker = threading.Thread(target=turn, name="collie-compat-cancel", daemon=True)
    worker.start()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if os.path.isfile(marker) or "error" in outcome:
            break
        time.sleep(0.05)
    if "error" in outcome:
        raise outcome["error"]
    _expect(os.path.isfile(marker),
            "the stand-in worker never started, so cancellation was not exercised")

    began = time.monotonic()
    confirmed = runner.cancel_current()
    elapsed = time.monotonic() - began
    worker.join(30.0)

    _expect(confirmed,
            "cancel_current() returned False: process-tree extinction was not confirmed")
    _expect(elapsed <= 5.0,
            "cancel_current() took %.1fs; the contract is five seconds" % elapsed)
    _expect(not worker.is_alive(), "the cancelled turn never returned")
    snapshot = outcome.get("snapshot")
    _expect(snapshot is not None, "the cancelled turn produced no snapshot")
    _expect(snapshot.cancelled,
            "the snapshot of a cancelled turn does not say it was cancelled")
    _expect(not snapshot.settled, "a cancelled turn was reported as settled")
    return "cancelled in %.2fs; tree extinction confirmed" % elapsed


def _check_app_server_cancel(ctx: CheckContext) -> str:
    """Run a real owned stdio peer that ignores interrupt, then escalate."""
    from .codex_app_server_runner import AppServerStdioTransport, CodexAppServerRunner

    workspace = ctx.workspace("appserver-cancel")
    marker = os.path.join(workspace, "child.pid")

    def factory(_argv, cwd, env):
        return AppServerStdioTransport(
            [sys.executable, "-u", "-c", _APP_SERVER_CANCEL_PEER, marker],
            cwd=cwd, env=env)

    runner = CodexAppServerRunner(
        executable="codex", default_timeout_s=120.0,
        transport_factory=factory, snapshotter=_StaticSnapshotter("d0"))
    outcome: dict[str, Any] = {}

    def turn():
        try:
            outcome["snapshot"] = runner.start(_CANCEL_PROMPT, workspace)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=turn, name="collie-appserver-cancel", daemon=True)
    worker.start()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not os.path.isfile(marker):
        if "error" in outcome:
            raise outcome["error"]
        time.sleep(.03)
    _expect(os.path.isfile(marker), "the App Server cancel peer never reached turn/start")
    began = time.monotonic()
    confirmed = runner.cancel_current()
    elapsed = time.monotonic() - began
    worker.join(15.0)
    _expect(confirmed, "App Server cancel did not confirm process-tree extinction")
    _expect(elapsed <= 5.0, "App Server cancel took %.1fs" % elapsed)
    _expect(not worker.is_alive(), "App Server turn remained alive after cancel")
    snapshot = outcome.get("snapshot")
    _expect(snapshot is not None and snapshot.cancelled,
            "cancelled App Server turn produced no cancelled snapshot")
    _expect(not snapshot.settled, "cancelled App Server turn was reported settled")
    return "cancelled in %.2fs; native interrupt escalated; tree extinction confirmed" % elapsed


def _check_pi_cancel(ctx: CheckContext) -> str:
    """A Pi peer that ignores abort must be killed by the owned stdio tree."""
    from .codex_app_server_runner import AppServerStdioTransport
    from .pi_rpc_runner import PiRpcRunner

    workspace = ctx.workspace("pi-cancel")
    marker = os.path.join(workspace, "child.pid")

    def factory(_argv, cwd, env):
        return AppServerStdioTransport(
            [sys.executable, "-u", "-c", _PI_CANCEL_PEER, marker],
            cwd=cwd, env=env)

    runner = PiRpcRunner(default_timeout_s=120.0, transport_factory=factory,
                         snapshotter=_StaticSnapshotter("d0"))
    outcome: dict[str, Any] = {}

    def turn():
        try:
            outcome["snapshot"] = runner.start(_CANCEL_PROMPT, workspace)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=turn, name="collie-pi-cancel", daemon=True)
    worker.start()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not os.path.isfile(marker):
        if "error" in outcome:
            raise outcome["error"]
        time.sleep(.03)
    _expect(os.path.isfile(marker), "the Pi cancel peer never received prompt")
    began = time.monotonic()
    confirmed = runner.cancel_current()
    elapsed = time.monotonic() - began
    worker.join(15.0)
    _expect(confirmed, "Pi cancel did not confirm process-tree extinction")
    _expect(elapsed <= 5.0, "Pi cancel took %.1fs" % elapsed)
    _expect(not worker.is_alive(), "Pi turn remained alive after cancel")
    snapshot = outcome.get("snapshot")
    _expect(snapshot is not None and snapshot.cancelled,
            "cancelled Pi turn produced no cancelled snapshot")
    _expect(not snapshot.settled, "cancelled Pi turn was reported settled")
    return "cancelled in %.2fs; native abort escalated; tree extinction confirmed" % elapsed


def check_one_turn(ctx: CheckContext) -> str:
    """One real turn, verified by reading the file afterwards rather than believing the runner."""
    spec, probe = ctx.spec, ctx.probe
    if spec.kind == "native":
        raise SkipCheck("the native harness is exercised by the rest of the test suite")
    if not probe.installed:
        raise SkipCheck("not installed: %s" % spec.key)
    if not probe.usable():
        raise SkipCheck("not usable: %s" % (_scrub(probe.detail, 120) or probe.login))

    workspace, fixture_note = ctx.fixture()
    runner = runner_registry.make_runner(spec.key, timeout_s=600.0)
    snapshot = runner.start(_ONE_TURN_PROMPT, workspace)
    ctx.state.update({"runner": runner, "snapshot": snapshot, "workspace": workspace})

    _expect(snapshot.settled,
            "the turn did not settle: %s" % (_scrub(snapshot.error, 200) or "(no error)"))
    # Past this line a turn really completed, so its tokens are evidence `usage`
    # may read even if an assertion below fails: a settled turn whose edit never
    # landed is a bad turn, not an unmeasured one.
    ctx.state["completed"] = snapshot
    _expect(snapshot.mutated, "the turn settled without changing the workspace")
    with open(os.path.join(workspace, _FIXTURE_FILE), "r", encoding="utf-8") as handle:
        body = handle.read()
    # The host reads the file.  The runner's own account of what it did is not
    # evidence, which is the whole reason Collie verifies after the fact.
    _expect(_ONE_TURN_MARKER in body,
            "the runner reported success but %s does not contain the marker" % _FIXTURE_FILE)
    result = snapshot_to_run_result(snapshot, spec, probe)
    _expect(result.harness == spec.key,
            "the run would be recorded under harness=%r" % result.harness)
    _expect(not result.verified,
            "snapshot_to_run_result must never mark a run verified")
    return "settled, workspace mutated, marker present, locator=%s%s" % (
        "yes" if snapshot.thread_id else "MISSING",
        "; " + fixture_note if fixture_note else "")


def check_resume(ctx: CheckContext) -> str:
    """A second turn continues the first thread instead of quietly starting a new one."""
    spec = ctx.spec
    # Only a first turn that passed *every* one_turn assertion is a thread worth
    # continuing.  Resuming a thread whose turn never settled spends a second
    # model call to rediscover the first one's error, and resuming one whose edit
    # never landed asks the runner to change a line that is not there.
    _needs(ctx, "one_turn", "no verified first turn to resume")
    first = ctx.state.get("snapshot")
    runner = ctx.state.get("runner")
    workspace = ctx.state.get("workspace")
    if first is None or runner is None:
        raise SkipCheck("one_turn produced no thread to resume")
    if not spec.caps.session_resume:
        raise SkipCheck("%s does not declare session_resume" % spec.key)

    second = runner.resume(first, _RESUME_PROMPT)
    ctx.state["snapshot"] = second

    _expect(second.settled,
            "the resumed turn did not settle: %s" % (_scrub(second.error, 200) or "(none)"))
    # Settled, so this turn's cumulative counters supersede the first turn's for
    # `usage`.  Before it settles they do not: a resume that never completed must
    # not be allowed to overwrite the usable evidence the first turn left.
    ctx.state["completed"] = second
    _expect(second.thread_id == first.thread_id,
            "the resumed turn reports locator %r, not the one the snapshot named"
            % (second.thread_id or ""))
    _expect(second.cursor > first.cursor,
            "the event cursor did not advance (%d -> %d)" % (first.cursor, second.cursor))
    _expect(second.invocation == 2,
            "the resumed turn is invocation %d, not 2" % second.invocation)
    with open(os.path.join(workspace, _FIXTURE_FILE), "r", encoding="utf-8") as handle:
        body = handle.read()
    _expect(_RESUME_MARKER in body,
            "the resumed turn did not change %s" % _FIXTURE_FILE)
    return "same locator, cursor %d -> %d, invocation=2" % (first.cursor, second.cursor)


def check_usage(ctx: CheckContext) -> str:
    """Token counts match what the spec claims, and absence is None rather than zero."""
    spec = ctx.spec
    # The *completed* turn, not merely the most recent one.  Reading a snapshot
    # whose turn never settled turns somebody else's failure into "a completed
    # turn reported 0 output tokens", which is a claim about the runner's usage
    # reporting that nothing in this run supports.
    snapshot = ctx.state.get("completed")
    if snapshot is None:
        _needs(ctx, "one_turn", "no completed turn to read usage from")
        raise SkipCheck("one_turn produced no turn to read usage from")

    usage = usage_to_collie(spec.key, dict(snapshot.usage or {}))
    _expect(usage.known == bool(spec.caps.usage_tokens),
            "usage_known=%s but caps.usage_tokens=%s" % (usage.known,
                                                         spec.caps.usage_tokens))
    if not usage.known:
        for name in ("input_tokens", "output_tokens", "cache_read", "cache_creation"):
            _expect(getattr(usage, name) is None,
                    "unknown usage reported %s=%r; an unmeasured run must not average "
                    "into the dashboard as a free one" % (name, getattr(usage, name)))
        return "usage unknown, every field None (not 0)"

    _expect(usage.output_tokens and usage.output_tokens > 0,
            "a completed turn reported %r output tokens" % usage.output_tokens)
    # Collie's `input` is the *uncached* count, so a resumed turn whose whole
    # prompt was served from cache legitimately reports input=0.  What must not
    # happen is both halves being zero.
    _expect((usage.input_tokens or 0) > 0 or (usage.cache_read or 0) > 0,
            "a completed turn reported neither input nor cached input tokens")
    if spec.caps.usage_cost:
        _expect(usage.cost_usd_reported is not None,
                "%s declares usage_cost but reported no cost" % spec.key)
    return "input=%s output=%s cache_read=%s cost_reported=%s source=%s" % (
        usage.input_tokens, usage.output_tokens, usage.cache_read,
        usage.cost_usd_reported, usage.source or "-")


# --- the table --------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    """One column: what it asserts, whether it costs money, and how to run it."""

    name: str
    live: bool
    asserts: str
    run: Callable[[CheckContext], str]


_CHECKS: tuple[Check, ...] = (
    Check("admission", False,
          "the binary exposes the reviewed vendor programmatic interface; this "
          "does not by itself enable a later-phase adapter",
          check_admission),
    Check("probe", False,
          "the probe row is well-formed, explains absence, and reads no login it "
          "was not pointed at",
          check_probe),
    Check("env_hygiene", False,
          "a real child process receives the allowlist and nothing else; the "
          "receipt records key names only",
          check_env_hygiene),
    Check("handshake", False,
          "the installed CLI is at or above the version this layer was written "
          "against",
          check_handshake),
    Check("framing", False,
          "chaotic output becomes a reported runner error instead of a crash or a "
          "false success",
          check_framing),
    Check("double_control", False,
          "the launch line disables every planner, scheduler and daemon surface "
          "the worker has of its own",
          check_double_control),
    Check("billing", False,
          "the billing class is one of the declared five and its mode agrees",
          check_billing),
    Check("cancel", False,
          "cancel_current() confirms process-tree extinction within five seconds",
          check_cancel),
    Check("one_turn", True,
          "one real turn settles and the host can see the edit on disk",
          check_one_turn),
    Check("resume", True,
          "a second turn continues the same locator with a monotonic cursor",
          check_resume),
    Check("usage", True,
          "reported tokens match the declared capability; unknown stays None",
          check_usage),
)

# Order matters twice over: the offline columns run first so a report from a
# broken host still says something, and `one_turn` runs before `resume` and
# `usage`, which read the thread it leaves behind.
CHECKS: Mapping[str, Check] = MappingProxyType({check.name: check for check in _CHECKS})
CHECK_NAMES: tuple[str, ...] = tuple(check.name for check in _CHECKS)
LIVE_CHECK_NAMES: tuple[str, ...] = tuple(check.name for check in _CHECKS if check.live)
OFFLINE_CHECK_NAMES: tuple[str, ...] = tuple(check.name for check in _CHECKS
                                             if not check.live)


# --- the matrix -------------------------------------------------------------
def _cell(check: Check, ctx: CheckContext) -> dict[str, Any]:
    """Run one check.  Returns a cell; never raises for an ordinary failure."""
    began = time.monotonic()
    status, detail = PASS, ""
    try:
        detail = check.run(ctx) or ""
    except SkipCheck as exc:
        status, detail = SKIP, str(exc)
    except PrerequisiteFailed as exc:
        status, detail = UNVERIFIED, str(exc)
    except BillingOverrideError as exc:
        # The parent shell would re-bill or re-route this worker.  That is a fact
        # about the host, not about the runner, and it is the same refusal a real
        # run would hit — so it is a skip with the variable names in it.
        status, detail = SKIP, "parent environment blocks this worker: %s" % exc
    except CheckFailure as exc:
        status, detail = FAIL, str(exc)
    except Exception as exc:
        status, detail = FAIL, "%s: %s" % (type(exc).__name__, exc)
    cell = {"status": status, "detail": _scrub(detail),
            "duration_ms": int((time.monotonic() - began) * 1000)}
    # What a later column in this row is allowed to build on (see `_needs`).
    ctx.outcomes[check.name] = (status, cell["detail"])
    return cell


def _row(key: str, *, live: bool, docker: bool, scratch_root: str,
         names: tuple[str, ...]) -> dict[str, Any]:
    """One runner's whole row, including the reason a row could not be filled in."""
    spec = runner_registry.SPECS.get(key)
    if spec is None:
        return {"runner": key, "label": key, "error": "unknown runner",
                "checks": {name: {"status": SKIP, "detail": "unknown runner: %s" % key,
                                  "duration_ms": 0} for name in names}}
    try:
        probe = runner_registry.probe(key)
    except Exception as exc:    # probe() is documented never to raise; belt and braces
        probe = RunnerProbe(key=key, installed=False,
                            detail="could not probe: %s: %s" % (type(exc).__name__, exc))

    ctx = CheckContext(spec=spec, probe=probe, live=live, docker=docker,
                       scratch_root=scratch_root)
    checks: dict[str, dict[str, Any]] = {}
    try:
        for name in names:
            check = CHECKS[name]
            if docker and spec.kind != "native":
                checks[name] = {"status": SKIP, "duration_ms": 0,
                                "detail": "the Docker runtime arrives in phase 3"}
            elif spec.phase > CURRENT_PHASE and name != "admission":
                checks[name] = {"status": SKIP, "duration_ms": 0,
                                "detail": "%s in this phase: %s arrives in phase %d"
                                          % (runner_specs.NOT_IMPLEMENTED_PREFIX,
                                             key, spec.phase)}
            elif check.live and spec.kind == "native":
                # SKIP, not UNVERIFIED, and the difference matters: the registry
                # downgrades a capability for every UNVERIFIED column, so leaving
                # the control row unverified would end with a compat report
                # telling the selector that Collie's own harness cannot resume.
                checks[name] = {"status": SKIP, "duration_ms": 0,
                                "detail": "the native harness is the control row; "
                                          "its turns are covered by the rest of the "
                                          "test suite, not by this matrix"}
            elif check.live and not live:
                checks[name] = {"status": UNVERIFIED, "duration_ms": 0,
                                "detail": "live checks are disabled; pass --live "
                                          "(this one spends real tokens)"}
            else:
                checks[name] = _cell(check, ctx)
    finally:
        ctx.close()

    return {
        "runner": key,
        "label": spec.label,
        "kind": spec.kind,
        "phase": spec.phase,
        "protocol": spec.caps.protocol,
        "credential_family": spec.credential_family,
        "installed": probe.installed,
        # Deliberately not `billing_evidence` or `executable_path`: one can quote
        # an account address, the other carries the operator's user name, and
        # neither adds anything the class and the version do not.
        "version": probe.version,
        "login": probe.login,
        "billing_class": probe.billing_class,
        "billing_mode": probe.billing_mode,
        "compat_before": probe.compat,
        "notes": [_scrub(note, 240) for note in spec.notes],
        "checks": checks,
    }


def run_matrix(runners: Iterable[str] | None = None, *, live: bool = False,
               docker: bool = False, workspace: str | None = None,
               checks: Iterable[str] | None = None) -> dict[str, Any]:
    """Run the conformance matrix and return the report.

    ``runners`` names the rows (default: every key in
    :data:`harness.runner_registry.SPECS`).  ``live`` additionally runs the
    columns that spend real tokens; without it they are recorded ``UNVERIFIED``
    with the reason, because "we did not look" and "we looked and it was fine"
    must not be the same cell.  ``docker`` is declared here and skips every
    external row: the container runtime arrives in phase 3, and quietly running
    the host matrix instead would put a PASS in the report for a claim nobody
    tested.  ``workspace`` is the scratch *root* every fixture is created under —
    a fresh directory per runner, so a caller may point this at a fast disk
    without a worker ever touching a directory it was not given.

    Never raises for a runner or a check that misbehaves: a cell becomes ``FAIL``
    with a redacted summary and the rest of the table still runs.  The return
    value is JSON-serializable and is exactly what :func:`write_report` writes and
    :func:`harness.runner_registry.apply_compat_report` reads back.
    """
    keys = list(runner_registry.SPECS) if runners is None else [str(k) for k in runners]
    if checks is None:
        names: tuple[str, ...] = CHECK_NAMES
    else:
        wanted = {str(name) for name in checks}
        unknown = sorted(wanted - set(CHECK_NAMES))
        if unknown:
            raise ValueError("unknown conformance check(s): %s" % ", ".join(unknown))
        names = tuple(name for name in CHECK_NAMES if name in wanted)

    scratch_root = workspace or tempfile.gettempdir()
    if not os.path.isdir(scratch_root):
        raise ValueError("workspace does not exist or is not a directory: %s"
                         % _display_path(scratch_root))

    began = time.time()
    rows: dict[str, Any] = {}
    for key in keys:
        rows[key] = _row(key, live=live, docker=docker, scratch_root=scratch_root,
                         names=names)

    totals = {PASS: 0, FAIL: 0, SKIP: 0, UNVERIFIED: 0}
    unverified: dict[str, str] = {}
    failures: dict[str, str] = {}
    for key, row in rows.items():
        for name, cell in row["checks"].items():
            status = cell["status"]
            totals[status] = totals.get(status, 0) + 1
            reason = cell.get("detail") or "(no reason recorded)"
            if status == FAIL:
                failures["%s.%s" % (key, name)] = reason
            elif status in (SKIP, UNVERIFIED):
                # Both go in one list on purpose: the operator's question is
                # "what does this report *not* tell me", and the status prefix
                # keeps the two answers distinguishable inside it.
                unverified["%s.%s" % (key, name)] = "%s: %s" % (status, reason)

    report = {
        "schema": SCHEMA,
        "collie_version": __version__,
        "generated_at": began,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(began)),
        "date": time.strftime("%Y-%m-%d", time.gmtime(began)),
        # `runner_registry._report_is_windows` reads `os_name` first; the rest is
        # for the human.  `platform.node()` is deliberately absent — a hostname is
        # often a person's name and says nothing a report reader needs.
        "os_name": os.name,
        "os": plat.os_label(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "live": bool(live),
        "docker": bool(docker),
        "checks": list(names),
        "check_asserts": {name: CHECKS[name].asserts for name in names},
        "runners": rows,
        "totals": totals,
        "failures": failures,
        "unverified_reasons": unverified,
        "duration_ms": int((time.time() - began) * 1000),
    }
    _assert_publishable(report, "the compat report")
    return report


# --- report output ----------------------------------------------------------
_STATUS_ORDER = (PASS, FAIL, SKIP, UNVERIFIED)


def _cell_text(cell: Mapping[str, Any]) -> str:
    status = str(cell.get("status") or "")
    detail = str(cell.get("detail") or "")
    if status in (SKIP, UNVERIFIED) and detail:
        return "%s(%s)" % (status, detail.split(";")[0][:60])
    return status


def render_markdown(report: Mapping[str, Any]) -> str:
    """The same report, for a person.

    A table of statuses, then one section per runner with the observation each
    cell recorded.  Everything here came through :func:`_scrub` on the way into
    the report, so this function only formats — it must never reach back for
    something the JSON deliberately left out.
    """
    names = [str(name) for name in report.get("checks") or CHECK_NAMES]
    rows = dict(report.get("runners") or {})
    lines: list[str] = []
    lines.append("# Runner conformance — %s" % report.get("date", ""))
    lines.append("")
    lines.append("Collie %s · %s %s (%s) · Python %s · %s%s"
                 % (report.get("collie_version", "?"), report.get("os", "?"),
                    report.get("os_release", ""), report.get("machine", ""),
                    report.get("python_version", "?"),
                    "live" if report.get("live") else "offline only",
                    " · docker" if report.get("docker") else ""))
    lines.append("")
    totals = dict(report.get("totals") or {})
    lines.append("Totals: " + " · ".join("%s %d" % (status, totals.get(status, 0))
                                         for status in _STATUS_ORDER))
    lines.append("")
    lines.append("| runner | version | login | billing | " + " | ".join(names) + " |")
    lines.append("|---|---|---|---|" + "---|" * len(names))
    for key, row in rows.items():
        cells = dict(row.get("checks") or {})
        values = [_cell_text(cells.get(name) or {"status": ""}) for name in names]
        lines.append("| `%s` | %s | %s | %s | %s |"
                     % (key, row.get("version") or "-", row.get("login") or "-",
                        row.get("billing_class") or "-", " | ".join(values)))
    lines.append("")
    failures = dict(report.get("failures") or {})
    if failures:
        lines.append("## Failures")
        lines.append("")
        for where, reason in failures.items():
            lines.append("- `%s` — %s" % (where, reason))
        lines.append("")
    unverified = dict(report.get("unverified_reasons") or {})
    if unverified:
        # The most-read section of the report: what it does *not* say.  A
        # capability listed here is one `runner_registry.apply_compat_report`
        # takes away until somebody runs the column that would prove it.
        lines.append("## Not established by this run")
        lines.append("")
        for where, reason in unverified.items():
            lines.append("- `%s` — %s" % (where, reason))
        lines.append("")
    lines.append("## What each column asserts")
    lines.append("")
    asserts = dict(report.get("check_asserts") or {})
    for name in names:
        lines.append("- **%s** — %s" % (name, asserts.get(name, CHECKS[name].asserts
                                                          if name in CHECKS else "")))
    lines.append("")
    for key, row in rows.items():
        lines.append("## %s (`%s`)" % (row.get("label") or key, key))
        lines.append("")
        lines.append("phase %s · %s · protocol `%s` · credential family `%s`"
                     % (row.get("phase", "?"), row.get("kind", "?"),
                        row.get("protocol", "?"), row.get("credential_family") or "-"))
        lines.append("")
        for name in names:
            cell = dict((row.get("checks") or {}).get(name) or {})
            detail = cell.get("detail") or ""
            lines.append("- **%s** %s (%d ms)%s"
                         % (name, cell.get("status", "-"), int(cell.get("duration_ms") or 0),
                            " — " + detail if detail else ""))
        notes = list(row.get("notes") or [])
        if notes:
            lines.append("")
            lines.append("Known limits declared by the spec:")
            for note in notes:
                lines.append("- %s" % note)
        lines.append("")
    lines.append("_No prompt text, token, account address or home directory path is "
                 "recorded in this report._")
    lines.append("")
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], path: str) -> tuple[str, str]:
    """Write ``<path>.json`` and the same name as ``.md``; return both paths.

    Two files because they have two readers: the JSON is what
    ``runner_registry.apply_compat_report`` folds back into every probe, and the
    Markdown is what goes into ``docs/runners.md`` and into issues.  Writing only
    the machine half is how a report ends up unread; writing only the human half
    is how a capability stays unverified forever.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("report path must be a non-empty string")
    base = path
    for suffix in (".json", ".md"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    directory = os.path.dirname(os.path.abspath(base))
    if directory:
        os.makedirs(directory, exist_ok=True)
    _assert_publishable(report, "the compat report")

    json_path = base + ".json"
    md_path = base + ".md"
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_markdown(report))
    return json_path, md_path


__all__ = [
    "CHECKS", "CHECK_NAMES", "Check", "CheckContext", "CheckFailure", "FAIL",
    "LIVE_CHECK_NAMES", "OFFLINE_CHECK_NAMES", "PASS", "PrerequisiteFailed", "SCHEMA",
    "SKIP", "SkipCheck", "UNVERIFIED", "render_markdown", "run_matrix", "write_report",
]
