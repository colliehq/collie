"""Stop, at the boundary where a tool owns a real process tree.

Everything here runs REAL bounded subprocesses on the host OS, because the bug this covers
only exists there: `Popen.communicate(timeout=…)` cannot be woken, so pressing Stop one
second into a 600-second `bash` call used to change nothing at all — the command kept
compiling, writing files and holding the machine until its own deadline expired.

Three properties are load-bearing and each is asserted against an observable effect, never
against a message alone:

  1. A cancelled command STOPS, and so does everything it started. The test child schedules
     a filesystem write for several seconds in the future; if the tree really died, that
     file never appears. A `p.kill()` that reaps only the shell fails this.
  2. A cancellation is never mistaken for a result. The output is ERROR-prefixed and says it
     is partial, so the finish gate (loop._repro_failed) reads an interrupted `pytest` as a
     failed check rather than a passing one.
  3. Nothing that used to work changed: the timeout message, the spill file, exit codes, and
     tool contexts that have never heard of cancellation at all.

    python -m pytest tests/test_tool_cancellation.py -q
"""
import os
import subprocess
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import plat                                    # noqa: E402
from harness import tool_process as P                       # noqa: E402
from harness.tools import BashTool, GrepTool, ToolCtx       # noqa: E402


# A child that outlives its parent shell unless the whole tree is killed: it schedules a
# write 4s out, prints a line first (so partial output exists), and only then raises the
# `started` flag the test's Stop is armed on — so cancellation always lands with the shell
# running, its grandchild pending, and one line already through the pipe.
_BG_CMD = ("(sleep 4; touch done) & echo running; sleep .3; touch started; sleep 30")
_CHILD_GRACE = 5.5          # comfortably past the child's 4s write


def _ctx(cwd, cancelled=None):
    """A tool context shaped like the ones surfaces really pass."""
    ns = types.SimpleNamespace(cwd=str(cwd), project="cancel-test")
    if cancelled is not None:
        ns.cancelled = cancelled
    return ns


def _need_shell():
    if not plat.has_posix_shell():
        pytest.skip("no POSIX shell on this host (cmd.exe fallback cannot background a child)")


def _need_windows():
    if not plat.is_windows():
        pytest.skip("Windows Job Object ownership (the POSIX group is established at fork)")


def _fake_outcome(status, **kw):
    """Drive a tool's message mapping from a decided outcome, no subprocess involved."""
    def _run_owned(*a, **k):
        return P.Outcome(status, **kw)
    return _run_owned


def test_stop_wins_over_foreground_exit_before_background_release():
    proc = types.SimpleNamespace(poll=lambda: 0)
    assert P._wait(proc, [], time.monotonic(), 10, lambda: True, .01) == P.CANCELED


@pytest.mark.skipif(os.name != "nt", reason="Windows venv launcher")
def test_trusted_bootstrap_bypasses_the_venv_redirector(monkeypatch):
    monkeypatch.setattr(P.sys, "executable", r"C:\venv\Scripts\python.exe")
    monkeypatch.setattr(P.sys, "_base_executable", r"C:\Python312\python.exe")
    assert P._bootstrap_argv()[0] == r"C:\Python312\python.exe"


def test_execute_code_stop_reaps_a_delayed_descendant_and_keeps_partial_output(tmp_path):
    from harness.progtool import register_execute_code
    from harness.tools import default_registry
    registry = default_registry(web_search=False)
    register_execute_code(registry)
    delayed = "import time; time.sleep(2); open('late', 'w').write('bad')"
    code = ("import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', %r], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print('kept partial output', flush=True)\n"
            "open('ready', 'w').write('ready')\n"
            "time.sleep(30)" % delayed)
    ctx = _ctx(tmp_path, cancelled=lambda: (tmp_path / "ready").exists())
    started = time.monotonic()
    out = registry.get("execute_code").run({"code": code, "timeout": 60}, ctx)
    assert time.monotonic() - started < 5
    assert out.startswith("ERROR") and "canceled" in out and "kept partial output" in out
    assert not getattr(ctx, "tool_effect_uncertain", False)
    time.sleep(2.2)
    assert not (tmp_path / "late").exists()


def test_execute_code_already_canceled_never_executes_script(tmp_path):
    from harness.progtool import register_execute_code
    from harness.tools import default_registry
    registry = default_registry(web_search=False)
    register_execute_code(registry)
    out = registry.get("execute_code").run(
        {"code": "open('ran', 'w').write('bad')"}, _ctx(tmp_path, cancelled=lambda: True))
    assert "before execution" in out and not (tmp_path / "ran").exists()


# ── 1. a running command, and the child it started ───────────────────────────
def test_cancel_stops_the_running_command_and_its_child(tmp_path):
    """The whole point: Stop ends the tree, promptly, without losing what it printed."""
    _need_shell()
    started, done = tmp_path / "started", tmp_path / "done"
    ctx = _ctx(tmp_path, cancelled=lambda: started.exists())

    t0 = time.monotonic()
    out = BashTool().run({"command": _BG_CMD, "timeout_s": 600}, ctx)
    elapsed = time.monotonic() - t0

    # promptness: the command asks for 30s and the tool allows 600s; Stop must not wait for
    # either. The budget is generous enough for a loaded CI box and still proves the point.
    assert elapsed < 6, "cancellation must return promptly, took %.1fs" % elapsed
    assert out.startswith("ERROR"), out[:200]
    assert "canceled" in out.lower() and "PARTIAL" in out, out[:300]
    assert "running" in out, "partial pre-cancel output must survive: %r" % out[:300]
    # the delayed effect: a shell-only kill leaves this child alive and it lands 4s later.
    time.sleep(_CHILD_GRACE)
    assert not done.exists(), \
        "a cancelled command's child kept running and wrote to the working dir"
    # and the message must not have claimed a clean stop while leaving a tree behind
    if "WARNING" not in out:
        assert not done.exists()


def test_cancel_before_launch_does_not_run_the_command(tmp_path):
    """Cancelled before Popen is the ONE case where 'it never ran' is a fact, and the only
    case allowed to say so. Nothing may be created on disk."""
    marker = tmp_path / "ran"
    out = BashTool().run({"command": "touch ran; echo hi", "timeout_s": 30},
                         _ctx(tmp_path, cancelled=lambda: True))
    assert out.startswith("ERROR") and "NOT executed" in out, out[:200]
    time.sleep(.5)
    assert not marker.exists(), "a pre-launch cancellation must not execute the command"


def test_cancel_mid_command_beats_a_long_deadline(tmp_path):
    """Cancellation is polled, not waited on: a Stop that arrives 0.3s in returns then."""
    _need_shell()
    deadline = time.monotonic() + .3
    t0 = time.monotonic()
    out = BashTool().run({"command": "sleep 30", "timeout_s": 600},
                         _ctx(tmp_path, cancelled=lambda: time.monotonic() > deadline))
    elapsed = time.monotonic() - t0
    assert elapsed < 5, "Stop must not wait out the command, took %.1fs" % elapsed
    assert out.startswith("ERROR") and "canceled" in out.lower(), out[:200]


def test_keyboard_interrupt_kills_the_tree_before_it_propagates(tmp_path):
    """Ctrl-C unwinds this thread; the shell and its children are not the interpreter's to
    clean up. They must be reaped on the way out, and the exception must still reach the
    caller unchanged."""
    _need_shell()
    started, done = tmp_path / "started", tmp_path / "done"

    def _stop():
        if started.exists():
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        BashTool().run({"command": _BG_CMD, "timeout_s": 600}, _ctx(tmp_path, cancelled=_stop))
    time.sleep(_CHILD_GRACE)
    assert not done.exists(), "KeyboardInterrupt left the command's child running"


# ── 2. a cancellation is not a result ────────────────────────────────────────
def test_a_cancelled_command_never_reads_as_a_passing_check(tmp_path, monkeypatch):
    """The finish gate decides whether a post-edit reproduction passed by reading the tool's
    output (loop._repro_failed). A cancelled `pytest` that came back looking neutral would
    be counted as a check that ran and did not fail — verification theater built out of a
    user pressing Stop."""
    from harness import tools as T
    from harness.loop import _repro_failed

    monkeypatch.setattr(T._proc, "run_owned",
                        _fake_outcome(P.CANCELED, stdout="collected 41 items",
                                      elapsed_s=2.0, tree_terminated=True))
    out = BashTool().run({"command": "python -m pytest -q"}, _ctx(tmp_path))
    assert _repro_failed(out), "a cancelled repro must count as FAILED, not passed: %r" % out[:200]

    monkeypatch.setattr(T._proc, "run_owned",
                        _fake_outcome(P.PRELAUNCH_CANCELED, tree_terminated=True))
    out = BashTool().run({"command": "python -m pytest -q"}, _ctx(tmp_path))
    assert _repro_failed(out), out[:200]


def test_an_unconfirmed_kill_is_never_described_as_a_clean_stop(tmp_path, monkeypatch):
    """When the OS would not prove the tree is gone, the first line has to say so. The next
    thing a model does with 'it stopped' is re-run the command — on top of a tree that may
    still be writing files."""
    from harness import tools as T
    monkeypatch.setattr(T._proc, "run_owned",
                        _fake_outcome(P.CANCELED, stdout="half a build",
                                      elapsed_s=3.0, tree_terminated=False,
                                      detail="no Windows Job Object"))
    out = BashTool().run({"command": "make -j8"}, _ctx(tmp_path))
    assert out.startswith("ERROR") and "WARNING" in out, out[:300]
    assert "still be RUNNING" in out and "no Windows Job Object" in out, out[:300]
    assert "do not re-run" in out, out[:300]


# ── 3. grep: an unfinished search says nothing about what exists ─────────────
def test_grep_cancel_before_launch_is_not_a_no_match(tmp_path):
    (tmp_path / "a.txt").write_text("needle here\n", encoding="utf-8")
    out = GrepTool().run({"pattern": "needle", "path": "."},
                         _ctx(tmp_path, cancelled=lambda: True))
    assert out.startswith("ERROR") and "NOTHING" in out, out[:200]
    assert "no matches" not in out, out[:200]
    # control: the same search, uncancelled, still works exactly as before
    ok = GrepTool().run({"pattern": "needle", "path": "."}, _ctx(tmp_path))
    assert "needle here" in ok, ok[:200]
    assert GrepTool().run({"pattern": "zzz", "path": "."}, _ctx(tmp_path)) == "(no matches)"


def test_grep_cancel_keeps_partial_matches_but_marks_them_partial(tmp_path, monkeypatch):
    from harness import tools as T
    monkeypatch.setattr(T._proc, "run_owned",
                        _fake_outcome(P.CANCELED, stdout="src/a.py:1:hit\n",
                                      elapsed_s=1.0, tree_terminated=True))
    out = GrepTool().run({"pattern": "hit", "path": "."}, _ctx(tmp_path))
    assert out.startswith("ERROR"), out[:200]
    assert "src/a.py:1:hit" in out, "partial matches are still worth having: %r" % out[:200]
    assert "PARTIAL" in out and "NOTHING" in out, out[:200]
    assert "no matches" not in out


# ── 4. the owned-process helper itself ───────────────────────────────────────
def test_run_owned_cancels_a_real_tree_and_reports_extinction(tmp_path):
    """grep's exact configuration (stderr discarded), driven directly: the helper returns a
    cancelled outcome with partial stdout, and the tree it owned is gone."""
    _need_shell()
    started, done = tmp_path / "started", tmp_path / "done"
    argv, use_shell = plat.shell_argv(_BG_CMD)
    r = P.run_owned(argv, use_shell=use_shell, cwd=str(tmp_path), timeout_s=600,
                    capture_stderr=False, cancelled=lambda: started.exists())
    assert r.status == P.CANCELED and r.executed
    assert "running" in r.stdout, r.stdout[:200]
    assert r.elapsed_s < 6, r.elapsed_s
    time.sleep(_CHILD_GRACE)
    assert not done.exists()
    # On both supported ownership models (POSIX group polled to ESRCH, Windows Job
    # accounting) extinction is provable; if a host ever cannot, it must say why.
    assert r.tree_terminated or r.detail, "an unconfirmed kill must carry its reason"


def test_run_owned_prelaunch_cancel_creates_no_process(tmp_path):
    argv, use_shell = plat.shell_argv("touch ran")
    r = P.run_owned(argv, use_shell=use_shell, cwd=str(tmp_path), timeout_s=30,
                    cancelled=lambda: True)
    assert r.status == P.PRELAUNCH_CANCELED and not r.executed and r.tree_terminated
    time.sleep(.5)
    assert not (tmp_path / "ran").exists()


def test_run_owned_reports_a_launch_failure_instead_of_raising(tmp_path):
    r = P.run_owned(["definitely-not-a-real-program-xyz"], use_shell=False,
                    cwd=str(tmp_path), timeout_s=5)
    assert r.status == P.LAUNCH_ERROR and not r.executed and r.detail


def test_owned_group_kill_reaps_the_child_before_it_claims_extinction(monkeypatch):
    """The POSIX ownership contract, asserted from any host (this suite also runs on Windows,
    where there is no process group to exercise for real).

    A SIGKILLed child nobody has waited on is a zombie, and a zombie is still a MEMBER of its
    process group — so `killpg(pgid, 0)` keeps succeeding and the extinction poll would run to
    its deadline and report a clean cancellation as unconfirmed. Reaping our own child first is
    what makes the group answer honestly.
    """
    calls = []
    zombie = {"present": True}

    def _killpg(pgid, sig):
        calls.append(("killpg", pgid, sig))
        if sig == 0 and not zombie["present"]:
            raise ProcessLookupError                 # the group is genuinely empty now
        return None

    def _reap():
        calls.append(("reap",))
        zombie["present"] = False

    monkeypatch.setattr(P.os, "killpg", _killpg, raising=False)
    confirmed, detail = P._kill_owned_group(4242, 1.0, reap=_reap)

    assert confirmed and not detail, detail
    assert calls[0][0] == "killpg" and calls[0][2] != 0, calls
    assert calls[1] == ("reap",), "the direct child must be reaped before the poll: %r" % calls


def test_is_cancelled_survives_a_broken_host_but_not_a_real_interrupt():
    """A surface whose callback throws is a bug in the surface — it must not kill a running
    command. KeyboardInterrupt is the opposite: the interpreter is unwinding, and swallowing
    it would strand the tree."""
    def _boom():
        raise RuntimeError("surface bug")

    assert P.is_cancelled(_boom) is False
    assert P.is_cancelled(None) is False
    assert P.is_cancelled(lambda: True) is True

    def _interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        P.is_cancelled(_interrupt)


# ── 5. nothing that worked before changed ────────────────────────────────────
def test_contexts_without_cancellation_still_run(tmp_path):
    """Backwards compatibility, both shapes: the many test/embedder contexts that carry only
    cwd+project, and a ToolCtx built without the new field."""
    assert P.cancel_check(types.SimpleNamespace(cwd=".", project="p")) is None
    assert P.cancel_check(types.SimpleNamespace(cwd=".", cancelled="not callable")) is None
    assert ToolCtx(cwd=".", project="p", memory=None).cancelled is None

    bare = types.SimpleNamespace(cwd=str(tmp_path), project="p")
    assert BashTool().run({"command": "echo ok"}, bare).strip() == "ok"
    real = ToolCtx(cwd=str(tmp_path), project="p", memory=None)
    assert BashTool().run({"command": "echo ok"}, real).strip() == "ok"


def test_timeout_still_kills_spills_and_does_not_duplicate_its_buffer(tmp_path):
    """The timeout path keeps its wording, its kill and its spill file — and the output is
    read ONCE now (it used to be drained a second time after the kill), so a large pre-kill
    buffer cannot come back doubled."""
    _need_shell()
    out = BashTool().run({"command": "seq 1 40000; sleep 30", "timeout_s": 2}, _ctx(tmp_path))
    assert out.startswith("ERROR: command timed out after 2s (killed)"), out[:160]
    import re
    m = re.search(r"saved to (\S+)", out)
    assert m, "a timed-out command's pre-kill output must still spill: %r" % out[:200]
    full = open(m.group(1), encoding="utf-8").read()
    assert "\n40000" in full, "the spill must hold the whole pre-kill output"
    assert full.count("\n12345\n") == 1, "the pre-kill buffer must not be captured twice"


def test_timeout_kills_the_process_group_fast(tmp_path):
    """A backgrounded grandchild holding the stdout pipe must not wedge the drain."""
    _need_shell()
    t0 = time.monotonic()
    out = BashTool().run({"command": "(sleep 30 &) ; sleep 30", "timeout_s": 2}, _ctx(tmp_path))
    assert time.monotonic() - t0 < 12, "the tree kill must be prompt"
    assert "timed out" in out


def test_exit_code_and_stderr_still_surface(tmp_path):
    out = BashTool().run({"command": "echo oops; echo bad 1>&2; exit 3", "timeout_s": 30},
                         _ctx(tmp_path))
    assert out.startswith("[exit 3]") and "oops" in out and "[stderr] bad" in out, out[:200]


def test_a_deliberately_backgrounded_process_outlives_a_finished_call(tmp_path):
    """`server &` is a documented use of this tool. Owning the tree must not turn every
    completed call into a kill of whatever the model meant to leave running."""
    _need_shell()
    done = tmp_path / "done"
    out = BashTool().run(
        {"command": "(sleep 2; touch done) >/dev/null 2>&1 & echo hi", "timeout_s": 30},
        _ctx(tmp_path))
    assert out.strip() == "hi", out[:200]
    time.sleep(4)
    assert done.exists(), "a backgrounded child must survive the call that started it"


# ── 6. the wiring: a surface's Stop reaches inside a running tool ────────────
def test_the_loop_hands_its_stop_callback_to_the_tool(tmp_path):
    """End to end through the real loop: the harness runs one bash call, the surface's Stop
    flips while the command is running, and the run ends in seconds instead of waiting out
    the tool's own deadline — with the command's pending child never landing."""
    _need_shell()
    from _util import _ScriptProvider
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall

    started, done = tmp_path / "started", tmp_path / "done"
    h = make_harness(str(tmp_path), provider="mock", project="cancel-wiring", embed="hash")
    h.max_turns = 2
    h.self_verify = False
    h.cancelled = lambda: started.exists()
    h.provider = _ScriptProvider([
        Completion(text="", tool_calls=[ToolCall("c1", "bash", {"command": _BG_CMD,
                                                                "timeout_s": 600})]),
        Completion(text="done", stop_reason="end_turn")])

    t0 = time.monotonic()
    res = h.run("cancel-wiring", "run it", consolidate=False)
    elapsed = time.monotonic() - t0

    assert elapsed < 20, "Stop must reach INSIDE the running tool, took %.1fs" % elapsed
    assert "cancel" in (res.error or "").lower(), res.error
    time.sleep(_CHILD_GRACE)
    assert not done.exists(), "the loop's Stop must end the tool's whole process tree"


# ── 7. ownership exists BEFORE the command does (Windows) ────────────────────
def test_ownership_is_established_before_the_command_is_released(tmp_path, monkeypatch):
    """The order is the guarantee, and it is asserted as an order.

    Attaching a Job to a shell that is already running leaves a window in which an arbitrary
    command can spawn — or an MSYS shell re-parent — descendants the Job never contains,
    while Job accounting would still report that (empty) tree extinct. So the process the Job
    is attached to must be one that provably cannot have started anything yet: a fixed
    bootstrap blocked in stdin.readline().
    """
    _need_windows()
    seq = []
    real_attach = plat.attach_kill_on_close_job
    real_release = P._Bootstrap.release

    def _attach(proc, name=None):
        seq.append("attach")
        return real_attach(proc, name=name)

    def _release(self, proc, argv, use_shell, cwd):
        seq.append("release")
        return real_release(self, proc, argv, use_shell, cwd)

    monkeypatch.setattr(P.plat, "attach_kill_on_close_job", _attach)
    monkeypatch.setattr(P._Bootstrap, "release", _release)
    argv, use_shell = plat.shell_argv("echo owned")
    r = P.run_owned(argv, use_shell=use_shell, cwd=str(tmp_path), timeout_s=30)
    assert r.status == P.OK and "owned" in r.stdout, r
    assert seq == ["attach", "release"], seq


def test_a_cancel_during_startup_never_runs_the_command(tmp_path):
    """Stop pressed while the launcher is starting: ownership is already proved, the command
    has still not been released, so 'it did not run' stays a FACT rather than a hope — and
    the filesystem has to agree."""
    _need_windows()
    calls = []

    def _stop():                       # False for run_owned's own prelaunch check, then True
        calls.append(1)
        return len(calls) > 1

    argv, use_shell = plat.shell_argv("touch ran; echo hi")
    r = P.run_owned(argv, use_shell=use_shell, cwd=str(tmp_path), timeout_s=30, cancelled=_stop)
    assert r.status == P.PRELAUNCH_CANCELED and not r.executed, r
    assert r.tree_terminated and not r.effect_uncertain, r
    time.sleep(1.0)
    assert not (tmp_path / "ran").exists(), "a startup cancellation executed the command"


def test_a_command_that_cannot_be_owned_is_never_executed(tmp_path):
    """Ownership failure fails CLOSED. Running unowned would mean a later Stop could not
    reliably reach what the command started, and 'stopped' would be a claim we cannot make —
    so the command is not released at all, and the report says exactly that."""
    _need_windows()
    original = plat.attach_kill_on_close_job

    def _boom(proc, name=None):
        raise OSError("simulated Job assignment failure")

    P.plat.attach_kill_on_close_job = _boom
    try:
        argv, use_shell = plat.shell_argv("touch ran; echo hi")
        r = P.run_owned(argv, use_shell=use_shell, cwd=str(tmp_path), timeout_s=30)
    finally:
        P.plat.attach_kill_on_close_job = original
    assert r.status == P.LAUNCH_ERROR and not r.executed, r
    assert "NOT executed" in r.detail and "simulated Job assignment failure" in r.detail, r.detail
    time.sleep(1.0)
    assert not (tmp_path / "ran").exists(), "an unowned command was executed anyway"
    # and the tool says so rather than inventing a shell error
    out = BashTool().run({"command": "echo hi"}, _ctx(tmp_path))
    assert out.strip() == "hi", "ownership must work again once the failure is removed: %r" % out


def test_an_interrupt_during_startup_leaves_no_stray_launcher(tmp_path, monkeypatch):
    """A Ctrl-C between owning the launcher and releasing the command: the launcher is
    blocked in readline() with nobody left to release or reap it, so it must be retired on
    the way out — and the command it was holding must still never run."""
    _need_windows()
    started = []
    real_popen = subprocess.Popen

    def _popen(*a, **k):
        proc = real_popen(*a, **k)
        started.append(proc)
        return proc

    calls = []
    real_is_cancelled = P.is_cancelled

    def _is_cancelled(cb):
        calls.append(1)
        if len(calls) > 1:                     # the post-ownership check, mid-startup
            raise KeyboardInterrupt
        return real_is_cancelled(cb)

    monkeypatch.setattr(P.subprocess, "Popen", _popen)
    monkeypatch.setattr(P, "is_cancelled", _is_cancelled)
    argv, use_shell = plat.shell_argv("touch ran; echo hi")
    with pytest.raises(KeyboardInterrupt):
        P.run_owned(argv, use_shell=use_shell, cwd=str(tmp_path), timeout_s=30,
                    cancelled=lambda: False)
    assert started, "no launcher was started"
    assert started[0].poll() is not None, "the process-tree launcher was left running"
    time.sleep(1.0)
    assert not (tmp_path / "ran").exists()


def test_the_launcher_leaves_no_temp_records_behind(tmp_path):
    """The bootstrap's launch record is Collie's, not the user's: it must not accumulate."""
    _need_windows()
    import glob
    import tempfile as _tf
    pattern = os.path.join(_tf.gettempdir(), "collie-tool-*.json")
    before = set(glob.glob(pattern))
    argv, use_shell = plat.shell_argv("echo hi")
    for _ in range(3):
        P.run_owned(argv, use_shell=use_shell, cwd=str(tmp_path), timeout_s=30)
    assert not (set(glob.glob(pattern)) - before)


# ── 8. a background server must survive the run that started it ──────────────
_BG_HELPER = '''
import sys
sys.path.insert(0, %(root)r)
from harness import plat, tool_process as P
argv, use_shell = plat.shell_argv(%(cmd)r)
r = P.run_owned(argv, use_shell=use_shell, cwd=%(cwd)r, timeout_s=30)
print(r.status, r.background_detached, r.effect_uncertain, r.tree_terminated)
'''


def test_a_backgrounded_child_outlives_the_collie_process_that_started_it(tmp_path):
    """The regression this guards is subtle and total: owning the tree with a
    KILL_ON_JOB_CLOSE Job makes a successfully started background server die the moment
    `collie run` returns and the handle closes — silently breaking the one workflow the bash
    tool's own description recommends ("for a command that never returns … background it").

    So the test uses a FRESH process: a helper that runs one bash-shaped call, backgrounds a
    child with a delayed write, and exits. The child's file must land after the helper is
    gone. Nothing outside tmp_path is touched.
    """
    _need_shell()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = _BG_HELPER % {"root": root, "cwd": str(tmp_path),
                        "cmd": "(sleep 3; touch done) >/dev/null 2>&1 & echo hi"}
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, timeout=60)
    exited = time.monotonic() - t0
    assert p.returncode == 0, p.stderr[-500:]
    status, detached, uncertain, extinct = p.stdout.split()
    assert status == "ok" and detached == "True", p.stdout
    # a deliberate background start is a SUCCESS with a live tree, not a failed kill
    assert extinct == "False" and uncertain == "False", p.stdout
    assert exited < 3, "the call must not wait for what it backgrounded (%.1fs)" % exited
    assert not (tmp_path / "done").exists(), "the child was supposed to still be sleeping"
    time.sleep(5)
    assert (tmp_path / "done").exists(), \
        "a backgrounded child was killed when the Collie process that started it exited"


def test_a_background_release_that_fails_is_reported_not_claimed(tmp_path, monkeypatch):
    """If the survivors cannot be handed over cleanly we do NOT quietly kill them, and we do
    NOT pretend the background start is safe: the handle is dropped with KILL_ON_JOB_CLOSE
    intact (they die at Collie exit at the latest) and the call reports an uncertain effect."""
    _need_windows()
    _need_shell()
    real_attach = plat.attach_kill_on_close_job

    class _NoRelease:
        """The real Job, with only the detaching release broken."""

        def __init__(self, job):
            self._job = job

        def __getattr__(self, name):
            return getattr(self._job, name)

        def release_without_terminating(self):
            raise OSError("simulated SetInformationJobObject failure")

    monkeypatch.setattr(P.plat, "attach_kill_on_close_job",
                        lambda proc, name=None: _NoRelease(real_attach(proc, name=name)))
    ctx = _ctx(tmp_path)
    out = BashTool().run(
        {"command": "(sleep 2; touch done) >/dev/null 2>&1 & echo hi", "timeout_s": 30}, ctx)
    assert "WARNING" in out and "hi" in out, out[:300]
    assert "simulated SetInformationJobObject failure" in out, out[:300]
    assert getattr(ctx, "tool_effect_uncertain", False) is True, "the host must be told"
    # the survivors were not killed to make the failure tidy
    time.sleep(4)
    assert (tmp_path / "done").exists(), \
        "a failed release must not turn into a kill of what the model backgrounded"


# ── 9. release semantics, asserted without a kernel ──────────────────────────
class _FakeJob:
    """A Windows Job's observable surface, for the two paths that must never be confused."""

    def __init__(self, live=0, release_error=None):
        self.live = live
        self.release_error = release_error
        self.closed = self.released = self.terminated = False

    def active_processes(self):
        return self.live

    def close(self, timeout_s=5.0):
        self.closed = True

    def release_without_terminating(self):
        if self.release_error:
            raise self.release_error
        self.released = True

    def terminate_and_wait(self, exit_code=1, timeout_s=5.0):
        self.terminated, self.live = True, 0
        return True


def _owner_with(job):
    proc = types.SimpleNamespace(pid=4242, returncode=0, poll=lambda: 0,
                                 wait=lambda timeout=None: 0, kill=lambda: None)
    owner = P._Owner(proc, {}, job=job)
    owner.job = job                       # also exercised on POSIX, where plat makes no Job
    return owner


def test_a_finished_call_with_nothing_left_closes_its_job():
    job = _FakeJob(live=0)
    extinct, detached, failed, detail = _owner_with(job).release_after_success()
    assert (extinct, detached, failed, detail) == (True, False, False, "")
    assert job.closed and not job.released


def test_a_finished_call_with_survivors_detaches_them_instead_of_closing():
    job = _FakeJob(live=2)
    extinct, detached, failed, detail = _owner_with(job).release_after_success()
    assert (extinct, detached, failed) == (False, True, False), detail
    assert job.released and not job.closed and not job.terminated
    # foreground completion is NOT proof of whole-tree extinction, and that is not a failure
    assert not P.Outcome(P.OK, tree_terminated=extinct, background_detached=detached,
                         release_failed=failed).effect_uncertain


def test_a_failed_detach_neither_closes_the_job_nor_claims_success():
    job = _FakeJob(live=3, release_error=OSError("nope"))
    extinct, detached, failed, detail = _owner_with(job).release_after_success()
    assert (extinct, detached, failed) == (False, False, True)
    assert not job.closed and not job.terminated, "closing would kill the survivors"
    assert "3 process(es)" in detail and "killed when Collie exits" in detail, detail
    assert P.Outcome(P.OK, tree_terminated=extinct, background_detached=detached,
                     release_failed=failed, detail=detail).effect_uncertain


def test_a_success_in_a_shared_process_group_is_just_a_success():
    """Under COLLIE_PROCESS_OWNER the child shares Collie's own group, so there is no tree
    of ours to account for. Ignorance about processes that were never ours must not spend the
    host's uncertainty fence — otherwise every ordinary command in a Slack/Mission worker
    would come back flagged and the flag would mean nothing."""
    owner = _owner_with(None)
    owner.pgid = 0
    extinct, detached, failed, detail = owner.release_after_success()
    assert (extinct, detached, failed, detail) == (False, False, False, "")
    assert not P.Outcome(P.OK, tree_terminated=extinct, background_detached=detached,
                         release_failed=failed).effect_uncertain


def test_cancellation_can_never_reach_the_detaching_release():
    """The single most dangerous confusion in this file: a cancel path that 'released'
    survivors would be a Stop that leaves the tree running and says it stopped it."""
    job = _FakeJob(live=1)
    owner = _owner_with(job)
    confirmed, _ = owner.terminate()
    owner.discard()
    assert confirmed and job.terminated
    assert not job.released, "cancellation must never detach anything"


def test_an_unkillable_tree_keeps_its_kill_on_close_handle():
    """discard() on a tree terminate() could not empty: the handle is dropped WITHOUT the
    detaching release, so the kernel still makes a last attempt when Collie exits."""
    class _Stubborn(_FakeJob):
        def terminate_and_wait(self, exit_code=1, timeout_s=5.0):
            self.terminated = True
            return False

    job = _Stubborn(live=1)
    owner = _owner_with(job)
    monkey = []
    owner.proc = types.SimpleNamespace(pid=4242, returncode=None, poll=lambda: None,
                                       wait=lambda timeout=None: monkey.append(1) or 0,
                                       kill=lambda: None)
    confirmed, detail = owner.terminate()
    owner.discard()
    assert not confirmed and detail, detail
    assert not job.released and not job.closed


# ── 10. the uncertainty marker the host fences on ────────────────────────────
def test_an_unconfirmed_stop_marks_the_context_for_the_host(tmp_path, monkeypatch):
    """Wording is advice to the model; the flag is a fact for the loop. A turn whose command
    may still be running must be fenceable without anyone parsing English."""
    from harness import tools as T
    monkeypatch.setattr(T._proc, "run_owned",
                        _fake_outcome(P.CANCELED, stdout="half a build", elapsed_s=3.0,
                                      tree_terminated=False, detail="Job accounting unavailable"))
    ctx = _ctx(tmp_path)
    out = BashTool().run({"command": "make -j8"}, ctx)
    assert "WARNING" in out
    assert getattr(ctx, "tool_effect_uncertain", False) is True

    grep_ctx = _ctx(tmp_path)
    GrepTool().run({"pattern": "x", "path": "."}, grep_ctx)
    assert getattr(grep_ctx, "tool_effect_uncertain", False) is True


def test_a_clean_stop_and_an_ordinary_success_leave_the_marker_alone(tmp_path, monkeypatch):
    """The fence has to mean something: a proved kill and a plain command are both certain."""
    from harness import tools as T
    monkeypatch.setattr(T._proc, "run_owned",
                        _fake_outcome(P.CANCELED, stdout="x", elapsed_s=1.0,
                                      tree_terminated=True))
    ctx = _ctx(tmp_path)
    BashTool().run({"command": "sleep 1"}, ctx)
    assert getattr(ctx, "tool_effect_uncertain", False) is False

    monkeypatch.undo()                                       # a REAL command, start to finish
    plain = ToolCtx(cwd=str(tmp_path), project="p", memory=None)
    assert BashTool().run({"command": "echo ok"}, plain).strip() == "ok"
    assert plain.tool_effect_uncertain is False

    bg = _ctx(tmp_path)
    monkeypatch.setattr(T._proc, "run_owned",
                        _fake_outcome(P.OK, stdout="started", returncode=0,
                                      tree_terminated=False, background_detached=True))
    out = BashTool().run({"command": "server &"}, bg)
    assert out.strip() == "started", out
    assert getattr(bg, "tool_effect_uncertain", False) is False


def test_marking_a_context_that_cannot_carry_the_flag_is_not_an_error():
    """Embedders and test doubles pass whatever they like; a tool may not die on that."""
    class _Frozen:
        __slots__ = ()

    assert P.mark_effect_uncertain(_Frozen()) is False
    ok = types.SimpleNamespace(cwd=".")
    assert P.mark_effect_uncertain(ok) is True and ok.tool_effect_uncertain is True


def test_a_lost_handover_reports_the_command_as_executed(tmp_path, monkeypatch):
    """The bootstrap failing AFTER the command was released says nothing about whether the
    command ran. Only a failure BEFORE the release may claim it did not."""
    from harness import tools as T
    monkeypatch.setattr(T._proc, "run_owned",
                        _fake_outcome(P.HANDOVER_ERROR, stdout="partial", elapsed_s=.2,
                                      tree_terminated=False, detail="BrokenPipeError"))
    ctx = _ctx(tmp_path)
    out = BashTool().run({"command": "python -m pytest -q"}, ctx)
    assert out.startswith("ERROR") and "NOT executed" not in out, out[:300]
    assert "may have started" in out and "WARNING" in out, out[:300]
    assert getattr(ctx, "tool_effect_uncertain", False) is True
    from harness.loop import _repro_failed
    assert _repro_failed(out)
    assert P.Outcome(P.HANDOVER_ERROR, tree_terminated=False).executed


def test_plat_refuses_to_release_a_named_job():
    """Named Jobs are durable Mission cancellation receipts: another process is supposed to
    be able to terminate the tree through them. Detaching one would break that, so the narrow
    background-release operation is unavailable there — close()/terminate() are untouched."""
    _need_windows()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            **plat.no_window_kwargs())
    job = plat.attach_kill_on_close_job(proc, name="collie-test-named-job-%d" % os.getpid())
    try:
        with pytest.raises(RuntimeError):
            job.release_without_terminating()
        assert job.terminate_and_wait(timeout_s=5.0)     # strong semantics still work
    finally:
        try:
            job.close(timeout_s=5.0)
        except Exception:
            pass
        plat.kill_tree(proc)
        proc.wait(timeout=10)


if __name__ == "__main__":                                   # standalone runner parity
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
