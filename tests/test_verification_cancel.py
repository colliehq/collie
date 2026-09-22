"""Stopping a host verification check, and proving what it left behind.

The required check is the last thing that runs on a Mission, and it is the one
step that owns real OS processes. Three claims are tested here with real
processes wherever the platform allows:

  * a Stop reaches the check itself — the owned tree dies promptly, the run is
    reported as canceled, and no exit code produced around that stop is ever
    laundered into a pass;
  * the cancellation is scoped to exactly one request — a sibling check running
    at the same moment finishes untouched;
  * whatever happened is durable — a check arms an effect boundary before its
    first byte and only retires it on evidence, so a crash mid-check leaves a
    thread a human has to look at rather than one that quietly resumes.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from harness import cli, sessions, verification


PYTHON = Path(sys.executable).as_posix().replace('"', '\\"')


def _wait_for(predicate, timeout=15.0, interval=0.02):
    """Poll a condition instead of guessing a sleep long enough to hide a bug."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _stayed_absent(path, since, hold_s, margin=1.5):
    """Prove a killed descendant never got to its LATER write.

    The negative claim needs real time to pass — but only the descendant's own
    delay plus a margin, measured from the moment it announced itself, not an
    arbitrary sleep bolted onto the end of the test.
    """
    deadline = since + float(hold_s) + float(margin)
    while time.monotonic() < deadline:
        if path.exists():
            return False
        time.sleep(0.05)
    return not path.exists()


def _spaces(tmp_path, name):
    """A workspace whose bytes stay clean, plus a marker dir outside it.

    Markers must not live in the checked workspace: writing one there is a real
    edit during the check, and the freshness receipt would correctly refuse to
    call the result fresh. The subject here is cancellation, not freshness.
    """
    workspace = tmp_path / name
    markers = tmp_path / (name + "-markers")
    workspace.mkdir(parents=True, exist_ok=True)
    markers.mkdir(parents=True, exist_ok=True)
    return workspace, markers


def _grandchild_launcher(workspace, ready, late, hold_s=1.5):
    """A check that spawns a descendant which writes again LATER.

    This is the shape that makes "we sent a kill" different from "it stopped":
    the direct command is only a launcher, and the process that would edit the
    workspace after the receipt is its child.
    """
    child = (
        "import time\n"
        "from pathlib import Path\n"
        "Path(%r).write_text('ready')\n"
        "time.sleep(%r)\n"
        "Path(%r).write_text('late')\n" % (str(ready), float(hold_s), str(late)))
    launcher = workspace / "launcher.py"
    launcher.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', %r])\n"
        "print('launched', flush=True)\n"
        "time.sleep(120)\n" % child,
        encoding="utf-8")
    return '"%s" launcher.py' % PYTHON


# --------------------------------------------------------------------------- #
# a Stop reaches the check
# --------------------------------------------------------------------------- #
def test_cancel_between_registration_and_release_never_runs_the_command(tmp_path):
    """A stop that lands after the handle is published still runs nothing.

    ``on_process`` hands the caller a live, owned tree before one instruction
    byte is released. A Stop pressed inside that window used to be read only
    afterwards; the predicate is re-read on this side of the latch instead, so
    the window cannot execute anything.
    """
    workspace, markers = _spaces(tmp_path, "gap")
    marker = markers / "ran"
    (workspace / "check.py").write_text(
        "from pathlib import Path\nPath(%r).write_text('ran')\n" % str(marker),
        encoding="utf-8")
    stop = threading.Event()

    def register(proc):
        # The caller now holds the handle; the user presses Stop right here.
        stop.set()
        return True

    evidence = verification.run_verification_command(
        '"%s" check.py' % PYTHON, str(workspace), timeout=30, source="test",
        on_process=register, cancelled=stop.is_set)

    assert not marker.exists(), "a cancelled check must not execute one command byte"
    assert evidence["executed"] is False
    assert evidence["cancelled"] is True
    assert evidence["cancel_reason"] == "before_start"
    assert evidence["passed"] is False and evidence["command_passed"] is False
    assert evidence["process_tree_terminated"] is True
    assert evidence["freshness"] == "not_run"


@pytest.mark.parametrize("python_command", ["absolute", "shell-default"])
def test_stop_during_the_check_kills_the_tree_before_it_can_write_again(tmp_path, python_command):
    """Web Stop during a running check: the descendant never gets its later write."""
    workspace, markers = _spaces(tmp_path, "stop")
    ready, late = markers / "ready", markers / "late"
    command = _grandchild_launcher(workspace, ready, late, hold_s=1.5)
    if python_command == "shell-default":
        command = "python launcher.py"
    events = []
    ready_at = []

    def user_pressed_stop():
        # The stop arrives once the descendant is genuinely alive, which is the
        # only moment at which killing "just the shell" would be a silent bug.
        if ready.exists():
            ready_at.append(time.monotonic())
            return True
        return False

    evidence = verification.run_verification_command(
        command, str(workspace), timeout=120, source="test",
        cancelled=user_pressed_stop,
        on_event=lambda kind, data: events.append((kind, data)))

    assert ready.exists(), "the descendant must have started before the stop"
    assert evidence["executed"] is True
    assert evidence["cancelled"] is True
    assert evidence["cancel_reason"] == "during_execution"
    assert evidence["passed"] is False
    assert evidence["ran_after_last_edit"] is False
    assert evidence["freshness"] == "cancelled"
    assert evidence["process_tree_terminated"] is True
    assert "stopped by user request" in evidence["output"]
    # The check finished far sooner than its 120s timeout: a Stop is not a wait.
    assert evidence["duration_ms"] < 60_000
    # UI contract: the surface was told a check was running, then that it ended.
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "verification_started" and kinds[-1] == "verification_finished"
    assert "verification_canceling" in kinds
    assert events[-1][1]["cancelled"] is True and events[-1][1]["passed"] is False
    # ...and the descendant's later write never lands.
    assert _stayed_absent(late, ready_at[0], 1.5), (
        "a cancelled verifier's descendant kept writing after its receipt")


def test_a_stop_does_not_reach_a_sibling_check_running_at_the_same_time(tmp_path):
    """Cancellation is scoped to one request, not to "verification" in general."""
    stopped_ws, stopped_markers = _spaces(tmp_path, "victim")
    sibling_ws, sibling_markers = _spaces(tmp_path, "sibling")
    ready = stopped_markers / "ready"
    late = stopped_markers / "late"
    sibling_done = sibling_markers / "done"
    stopped_command = _grandchild_launcher(stopped_ws, ready, late, hold_s=1.5)
    # The sibling starts working only once the victim's tree is alive, so it is
    # demonstrably running at the moment the other tree is killed.
    (sibling_ws / "check.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "deadline = time.monotonic() + 15\n"
        "while not Path(%r).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        "time.sleep(0.5)\n"
        "Path(%r).write_text('done')\n"
        "raise SystemExit(0)\n" % (str(ready), str(sibling_done)),
        encoding="utf-8")
    results = {}
    ready_at = []

    def victim_stopped():
        if ready.exists():
            ready_at.append(time.monotonic())
            return True
        return False

    def run(key, command, cwd, cancelled):
        results[key] = verification.run_verification_command(
            command, cwd, timeout=120, source="test", cancelled=cancelled)

    threads = [
        threading.Thread(target=run, args=(
            "stopped", stopped_command, str(stopped_ws), victim_stopped)),
        threading.Thread(target=run, args=(
            "sibling", '"%s" check.py' % PYTHON, str(sibling_ws), lambda: False)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads)

    assert results["stopped"]["cancelled"] is True
    assert results["stopped"]["passed"] is False
    assert results["sibling"]["cancelled"] is False
    assert results["sibling"]["exit_code"] == 0
    assert results["sibling"]["command_passed"] is True
    assert results["sibling"]["passed"] is True, results["sibling"]["output"]
    assert sibling_done.exists(), "an unrelated check must survive another's Stop"
    assert _stayed_absent(late, ready_at[0], 1.5)


def test_a_normal_check_still_passes_and_reports_no_cancellation(tmp_path):
    """The stop machinery must not weaken the ordinary Required path."""
    workspace, markers = _spaces(tmp_path, "green")
    marker = markers / "ran"
    (workspace / "check.py").write_text(
        "from pathlib import Path\nPath(%r).write_text('ran')\n" % str(marker),
        encoding="utf-8")
    events = []

    evidence = verification.run_verification_command(
        '"%s" check.py' % PYTHON, str(workspace), timeout=60, source="test",
        cancelled=lambda: False,
        on_event=lambda kind, data: events.append(kind))

    assert marker.exists()
    assert evidence["exit_code"] == 0 and evidence["command_passed"] is True
    assert evidence["passed"] is True and evidence["freshness"] == "fresh"
    assert evidence["cancelled"] is False and evidence["cancel_reason"] == ""
    assert evidence["cancel_probe_error"] == ""
    assert evidence["process_tree_terminated"] is True
    assert events == ["verification_started", "verification_finished"]


def test_a_broken_stop_predicate_kills_nothing_and_leaks_no_thread(tmp_path):
    """A caller's bug is not evidence that the user pressed Stop."""
    workspace, markers = _spaces(tmp_path, "broken")
    marker = markers / "ran"
    (workspace / "check.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(0.4)\n"
        "Path(%r).write_text('ran')\n" % str(marker),
        encoding="utf-8")
    before = {t.name for t in threading.enumerate()}

    def broken():
        raise RuntimeError("cancel probe exploded")

    evidence = verification.run_verification_command(
        '"%s" check.py' % PYTHON, str(workspace), timeout=60, source="test",
        cancelled=broken)

    assert marker.exists(), "a raising predicate must not terminate the check"
    assert evidence["cancelled"] is False
    assert evidence["command_passed"] is True and evidence["passed"] is True
    assert "cancel probe exploded" in evidence["cancel_probe_error"]
    assert "cancellation check failed" in evidence["output"]
    assert _wait_for(lambda: not [
        t for t in threading.enumerate()
        if t.name == "collie-verifier-cancel" and t.name not in before], timeout=5)


def test_keyboard_interrupt_becomes_evidence_instead_of_a_traceback(monkeypatch,
                                                                    tmp_path):
    """Ctrl-C during the check owes the user a receipt, not a stack trace."""
    from harness import plat

    terminated = []

    class FakeJob:
        def terminate_and_wait(self, timeout_s):
            terminated.append(timeout_s)
            return True

        def close(self):
            pass

    class FakeProcess:
        pid = 4242
        returncode = None

        def communicate(self, input=None, timeout=None):
            raise KeyboardInterrupt()

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: {
        "commit": "abc", "working_tree": "clean", "dirty_files": [],
        "tree_digest": "same", "snapshot_complete": True, "snapshot_kind": "git"})
    monkeypatch.setattr(verification.subprocess, "Popen",
                        lambda *a, **kw: FakeProcess())
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda proc: FakeJob())

    evidence = verification.run_verification_command(
        "pytest -q", str(tmp_path), timeout=300, source="test")

    assert evidence["cancelled"] is True
    assert evidence["cancel_reason"] == "keyboard_interrupt"
    assert evidence["passed"] is False and evidence["ran_after_last_edit"] is False
    assert evidence["freshness"] == "cancelled"
    assert evidence["process_tree_terminated"] is True
    assert terminated, "the interrupted tree must still be proved gone"
    assert "interrupted by user" in evidence["output"]


def test_an_on_process_holder_that_cancels_gets_no_green_check(monkeypatch, tmp_path):
    """An exit-zero race after a caller's cancel is still a stop, not a pass."""
    from harness import plat

    class FakeJob:
        def terminate_and_wait(self, timeout_s):
            return True

        def close(self):
            pass

    class FakeProcess:
        pid = 4243
        returncode = None

        def communicate(self, input=None, timeout=None):
            # The caller stops the tree; the command's exit status races in as 0.
            assert verification.cancel_verification_process(self) is True
            self.returncode = 0
            return ("all green", None)

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: {
        "commit": "abc", "working_tree": "clean", "dirty_files": [],
        "tree_digest": "same", "snapshot_complete": True, "snapshot_kind": "git"})
    monkeypatch.setattr(verification.subprocess, "Popen",
                        lambda *a, **kw: FakeProcess())
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda proc: FakeJob())

    evidence = verification.run_verification_command(
        "pytest -q", str(tmp_path), timeout=300, source="test",
        on_process=lambda proc: True)

    assert evidence["command_passed"] is True, "exit zero stays visible as itself"
    assert evidence["cancelled"] is True
    assert evidence["cancel_reason"] == "caller_cancelled_process"
    assert evidence["passed"] is False, "a cancelled check cannot certify anything"
    assert evidence["ran_after_last_edit"] is False


# --------------------------------------------------------------------------- #
# the durable effect boundary
# --------------------------------------------------------------------------- #
def _session(tmp_path, sid, monkeypatch):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    sessions.save(sid, [{"role": "user", "content": "fix the parser"}],
                  project="checks", cwd=str(tmp_path))
    return sid


def test_the_boundary_is_armed_before_the_check_and_names_the_verification(
        tmp_path, monkeypatch):
    sid = _session(tmp_path, "fence-arm", monkeypatch)
    boundary = verification.open_check_boundary(
        sid, [{"role": "user", "content": "fix the parser"}], project="checks",
        cwd=str(tmp_path), run_id="run-1", command="pytest -q", surface="cli")

    assert boundary["armed"] is True and boundary["error"] == ""
    state = sessions.recovery_state(sid)
    assert state["recovery_required"] is True
    assert state["state"] == "external_action"
    assert state["detail"]["tool_name"] == "verification"
    assert state["detail"]["command"] == "pytest -q"
    assert state["run_id"] == "run-1"


def test_a_journal_that_cannot_hold_the_boundary_refuses_to_start_the_check(
        tmp_path, monkeypatch):
    """No durable fence, no host command: an unfenced check is the failure mode."""
    sid = _session(tmp_path, "fence-fail", monkeypatch)

    def boom(*a, **kw):
        raise OSError("journal is read-only")

    monkeypatch.setattr(sessions, "checkpoint", boom)
    boundary = verification.open_check_boundary(
        sid, [], project="checks", cwd=str(tmp_path), run_id="run-1",
        command="pytest -q")

    assert boundary["armed"] is False
    assert "could not be persisted" in boundary["error"]
    assert "journal is read-only" in boundary["error"]


def test_a_session_id_with_no_durable_file_is_not_a_fence(tmp_path, monkeypatch):
    """checkpoint() is a no-op for an unusable id; believing it would be the bug."""
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    boundary = verification.open_check_boundary(
        "../escape", [], project="checks", cwd=str(tmp_path), run_id="run-1",
        command="pytest -q")

    assert boundary["armed"] is False
    assert "did not become durable" in boundary["error"]


def test_an_older_uncertain_boundary_is_never_overwritten_or_retired(
        tmp_path, monkeypatch):
    """The check does not get to close somebody else's unreconciled effect."""
    sid = _session(tmp_path, "fence-older", monkeypatch)
    sessions.checkpoint(sid, [], project="checks", cwd=str(tmp_path),
                        run_id="worker-1", state="external_action",
                        detail={"tool_name": "browser_click"})

    boundary = verification.open_check_boundary(
        sid, [], project="checks", cwd=str(tmp_path), run_id="run-1",
        command="pytest -q")
    assert boundary["armed"] is False and boundary["preexisting"] is True
    assert boundary["error"] == ""
    assert sessions.recovery_state(sid)["detail"]["tool_name"] == "browser_click"

    closed = verification.close_check_boundary(boundary, {
        "executed": True, "process_tree_terminated": True})
    assert closed["retired"] is False
    assert "earlier unreconciled boundary" in closed["detail"]
    still = sessions.recovery_state(sid)
    assert still["recovery_required"] is True
    assert still["detail"]["tool_name"] == "browser_click"


@pytest.mark.parametrize("evidence,retired", [
    ({"executed": False}, True),
    ({"executed": True, "process_tree_terminated": True}, True),
    ({"executed": True, "process_tree_terminated": False}, False),
    ({"executed": True}, False),
    ({}, False),
    ({"executed": 0}, False),
    ({"executed": "false", "process_tree_terminated": True}, False),
])
def test_only_non_execution_or_confirmed_extinction_retires_the_boundary(
        tmp_path, monkeypatch, evidence, retired):
    sid = _session(tmp_path, "fence-verdict", monkeypatch)
    boundary = verification.open_check_boundary(
        sid, [], project="checks", cwd=str(tmp_path), run_id="run-1",
        command="pytest -q")
    assert boundary["armed"] is True

    closed = verification.close_check_boundary(boundary, evidence)

    assert closed["retired"] is retired
    assert closed["fenced"] is (not retired)
    assert bool(sessions.recovery_state(sid)) is (not retired)
    if retired and evidence.get("executed"):
        # Termination is not the same claim as "nothing happened".
        assert "may have changed files" in closed["detail"]


def test_a_boundary_that_cannot_be_cleared_stays_a_fence_and_says_so(
        tmp_path, monkeypatch):
    sid = _session(tmp_path, "fence-stuck", monkeypatch)
    boundary = verification.open_check_boundary(
        sid, [], project="checks", cwd=str(tmp_path), run_id="run-1",
        command="pytest -q")

    def boom(*a, **kw):
        raise OSError("disk went away")

    monkeypatch.setattr(sessions, "checkpoint", boom)
    closed = verification.close_check_boundary(
        boundary, {"executed": True, "process_tree_terminated": True})

    assert closed["retired"] is False and closed["fenced"] is True
    assert "could not be cleared" in closed["error"]
    assert sessions.recovery_state(sid)["recovery_required"] is True


# --------------------------------------------------------------------------- #
# the CLI ending
# --------------------------------------------------------------------------- #
def _cli_args(tmp_path, **over):
    import argparse

    base = dict(task="fix the parser", cwd=str(tmp_path), provider="mock", model=None,
                project="checks", mode=None, persona=None, goal=None, resume=None,
                cont=False, stream_json=False, json=True, print=False,
                web_search=False, intent="build", quality="balanced",
                verification="required", effort=None, speed=None,
                verify_command="pytest -q", runner=None)
    base.update(over)
    return argparse.Namespace(**base)


def _pin_cli(monkeypatch, tmp_path, result):
    """Route `collie run` at a fake native harness, so only the ending is under test."""
    import argparse

    from harness import router, runner_registry, runner_slice, settings
    from harness.router import RunDecision
    from harness.runner_specs import RunnerProbe

    state = tmp_path / "state"
    (state / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "data" / "memory.db"), str(state / "data" / "runs.db"),
        str(state / "data" / "dashboard.html"), str(state / "data" / "sandbox")))
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: RunDecision(
        provider="mock", model="mock-coder-v1", effort="default", speed="standard",
        billing_multiplier=1.0, intent="build", quality="balanced",
        verification="required", workspace="current", strategy="single",
        route_kind="code", complexity="simple"))
    monkeypatch.setattr(runner_registry, "probe_all", lambda keys=None, **kw: {
        "collie": RunnerProbe(
            key="collie", installed=True, executable_path="", version="0.test",
            login="n/a", billing_class="local", billing_mode="local",
            probed_at=1_800_000_000.0,
            capabilities=runner_registry.COLLIE_CAPABILITIES.to_dict())})
    monkeypatch.setattr(runner_slice, "run_adhoc", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("the native path must not build a worker")))
    monkeypatch.setattr(cli, "default_gate", lambda *a, **k: None)
    monkeypatch.setattr(cli, "configure_run_options", lambda h, **k: None)
    monkeypatch.setattr(cli, "dash", type("D", (), {"build": staticmethod(
        lambda *a, **k: None)})())

    class FakeHarness:
        def __init__(self):
            self.memory = self.recorder = type(
                "Closer", (), {"close": lambda self: None,
                               "finish_run": lambda self, res: None,
                               "set_block": lambda self, *a, **k: None})()
            self.provider = argparse.Namespace(actual_speed="standard")
            self.checkpoint_scope = ""
            self.settled = []

        def settle_run_memory(self, res, passed, evidence, source=""):
            self.settled.append((passed, source))
            return {"promoted": 0, "rejected": 0}

        def run(self, task_id, task, history=None, **kw):
            return result(task_id, task)

    built = []
    monkeypatch.setattr(cli, "make_harness",
                        lambda *a, **kw: built.append(FakeHarness()) or built[-1])
    return built


def _answered(task_id, task):
    from harness.recorder import RunResult

    return RunResult(task_id=task_id, harness="collie", model="mock-coder-v1",
                     answer="rewrote the tokenizer",
                     messages=[{"role": "user", "content": task},
                               {"role": "assistant", "content": "rewrote the tokenizer"}])


def test_cli_ctrl_c_during_the_check_keeps_the_answer_and_the_receipt(
        monkeypatch, tmp_path, capsys):
    """A stop during verification is a canceled task, not a lost one."""
    built = _pin_cli(monkeypatch, tmp_path, _answered)
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda command, cwd, **kw: {
                            "command": command, "exit_code": None,
                            "command_passed": False, "passed": False,
                            "executed": True, "cancelled": True,
                            "cancel_reason": "keyboard_interrupt",
                            "process_tree_terminated": True,
                            "freshness": "cancelled", "output": "interrupted",
                            "source": "user"})

    code = cli.cmd_run(_cli_args(tmp_path))
    payload = json.loads(capsys.readouterr().out.strip())

    assert code == 1
    assert payload["canceled"] is True and payload["stop_reason"] == "canceled"
    assert payload["completed"] is False
    # The work the agent actually did survives the stop.
    assert payload["answer"] == "rewrote the tokenizer"
    assert "stopped by user during the required check" in payload["error"]
    assert "required check failed" not in payload["error"]
    assert payload["verification_evidence"]["cancelled"] is True
    assert payload["verification_evidence"]["passed"] is False
    # A cancelled check never settles memory as a verified outcome.
    assert built[0].settled == []
    saved = sessions.load(payload["session"])
    assert saved["run_receipts"][-1]["verified"] is False
    assert saved["run_receipts"][-1]["canceled"] is True
    assert saved["last_answer"].startswith("rewrote the tokenizer")
    # A confirmed-extinct tree lets the check's own fence retire.
    assert sessions.recovery_state(payload["session"]) is None


def test_cli_check_whose_tree_survives_stays_fenced_across_save_and_resume(
        monkeypatch, tmp_path, capsys):
    """An unconfirmed process tree is exactly what a human has to look at."""
    _pin_cli(monkeypatch, tmp_path, _answered)
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda command, cwd, **kw: {
                            "command": command, "exit_code": 0,
                            "command_passed": True, "passed": False,
                            "executed": True, "cancelled": False,
                            "process_tree_terminated": False,
                            "freshness": "process_tree_cleanup_failed",
                            "output": "could not terminate verification process tree",
                            "source": "user"})

    cli.cmd_run(_cli_args(tmp_path))
    payload = json.loads(capsys.readouterr().out.strip())
    sid = payload["session"]

    assert payload["recovery_required"] is True
    assert payload["recovery"]["detail"]["tool_name"] == "verification"
    assert payload["verification_evidence"]["passed"] is False
    # The transcript save must not make the unknown effect look replay-safe.
    state = sessions.recovery_state(sid)
    assert state["recovery_required"] is True
    assert state["detail"]["tool_name"] == "verification"
    assert sessions.load(sid)["last_answer"].startswith("rewrote the tokenizer")

    # ...and the next --continue refuses this thread before routing anything.
    monkeypatch.setattr(cli, "make_harness", lambda *a, **kw: pytest.fail(
        "a fenced thread started another run"))
    assert cli.cmd_run(_cli_args(tmp_path, cont=True)) == 2
    refusal = json.loads(capsys.readouterr().out.strip())
    assert refusal["recovery_required"] is True


def test_cli_refuses_to_launch_a_check_it_cannot_fence(monkeypatch, tmp_path, capsys):
    """Pre-check journal failure means the command does not run at all."""
    _pin_cli(monkeypatch, tmp_path, _answered)
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda *a, **kw: pytest.fail("an unfenced check was launched"))
    real_checkpoint = sessions.checkpoint

    def selective(sid, messages, **kw):
        if (kw.get("detail") or {}).get("tool_name") == "verification":
            raise OSError("journal is read-only")
        return real_checkpoint(sid, messages, **kw)

    monkeypatch.setattr(sessions, "checkpoint", selective)

    code = cli.cmd_run(_cli_args(tmp_path))
    payload = json.loads(capsys.readouterr().out.strip())

    assert code == 1
    assert payload["verification_evidence"]["executed"] is False
    assert payload["verification_evidence"]["passed"] is False
    assert "journal is read-only" in payload["verification_evidence"]["output"]
    assert "journal is read-only" in payload["error"]
    # The answer is still the run's own, and the thread is not silently fenced.
    assert payload["answer"] == "rewrote the tokenizer"


def test_cli_normal_required_check_passes_and_leaves_no_fence(monkeypatch, tmp_path,
                                                              capsys):
    """The whole boundary machinery must be invisible on the ordinary green path."""
    built = _pin_cli(monkeypatch, tmp_path, _answered)
    seen = []
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda command, cwd, **kw: seen.append(command) or {
                            "command": command, "exit_code": 0,
                            "command_passed": True, "passed": True,
                            "executed": True, "cancelled": False,
                            "process_tree_terminated": True,
                            "ran_after_last_edit": True, "freshness": "fresh",
                            "output": "2 passed", "source": "user"})

    assert cli.cmd_run(_cli_args(tmp_path)) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    assert seen == ["pytest -q"]
    assert payload["verification_evidence"]["passed"] is True
    assert payload["completed"] is True and payload["canceled"] is False
    assert payload["recovery_required"] is False
    assert built[0].settled == [(True, "cli_verification")]
    assert sessions.recovery_state(payload["session"]) is None


def test_cli_real_check_is_fenced_while_it_runs_and_retired_afterwards(
        monkeypatch, tmp_path, capsys):
    """One real host process, end to end: fenced before, proven and retired after."""
    workspace, markers = _spaces(tmp_path, "cli-real")
    fence_seen = markers / "fence-seen.json"
    # The check reads the session journal from inside itself, which is the only
    # honest way to show the fence exists WHILE the command is running.
    (workspace / "check.py").write_text(
        "import json, glob, os\n"
        "from pathlib import Path\n"
        "rows = []\n"
        "for path in glob.glob(os.path.join(%r, '*.json')):\n"
        "    with open(path, encoding='utf-8') as fh:\n"
        "        rows.append(json.load(fh).get('active_run'))\n"
        "Path(%r).write_text(json.dumps(rows))\n"
        % (str(tmp_path / "state" / "sessions"), str(fence_seen)),
        encoding="utf-8")
    _pin_cli(monkeypatch, tmp_path, _answered)

    assert cli.cmd_run(_cli_args(
        tmp_path, cwd=str(workspace),
        verify_command='"%s" check.py' % PYTHON)) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["verification_evidence"]["passed"] is True
    assert payload["verification_evidence"]["process_tree_terminated"] is True
    assert payload["verification_evidence"]["cancelled"] is False
    assert "may have changed files" in payload["verification_evidence"]["effect_boundary"]
    live = [row for row in json.loads(fence_seen.read_text(encoding="utf-8")) if row]
    assert live, "the check must have been fenced while it was running"
    assert live[0]["state"] == "external_action"
    assert live[0]["detail"]["tool_name"] == "verification"
    # A confirmed-extinct tree retires it again.
    assert payload["recovery_required"] is False
    assert sessions.recovery_state(payload["session"]) is None


# --------------------------------------------------------------------------- #
# the Web ending
# --------------------------------------------------------------------------- #
def _web_isolate(monkeypatch, tmp_path):
    from harness import settings, webapp

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(webapp.Handler, "_notify_done", lambda *a, **kw: None)
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear()
        webapp.Handler._cancel_events.clear()


class _WebHarness:
    def __init__(self, gate):
        from types import SimpleNamespace

        closer = SimpleNamespace(close=lambda: None, finish_run=lambda res: None,
                                 set_block=lambda *a, **k: None)
        self.gate = gate
        self.composer = SimpleNamespace(identity="")
        self.memory = self.recorder = closer
        self.provider = SimpleNamespace(name="mock", model="mock-1")
        self.mode, self.force_edit, self.max_turns = "act", True, 20
        self._max_turns_hard_cap = None
        self.self_verify, self.verify_max = False, 2
        self.verify_gate, self.require_assert = False, False
        self.checkpoint_scope = ""
        self.settled = []

    def settle_run_memory(self, res, passed, evidence, source=""):
        self.settled.append((passed, source))
        return {"promoted": 0, "rejected": 0}

    def run(self, task_id, message, history=None, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(
            answer="rewrote the tokenizer", error="", model="mock-1",
            prefix_tokens=0, input_tokens=0, output_tokens=0, total_tokens=0,
            turns=1, tool_calls=1, wall_ms=1, cost_usd=0.0, verified=False,
            canceled=False, turns_exhausted=False, budget_exhausted=False,
            stop_reason="completed", edited=True, model_calls=1,
            parent_run_id=None, success=True,
            messages=[{"role": "user", "content": message},
                      {"role": "assistant", "content": "rewrote the tokenizer"}])


def test_web_stop_during_the_required_check_ends_canceled_not_green(monkeypatch,
                                                                    tmp_path):
    """Stop pressed while the check runs: it reaches the tree, and nothing passes."""
    from harness import webapp

    _web_isolate(monkeypatch, tmp_path)
    workspace, markers = _spaces(tmp_path, "web")
    ready, late = markers / "ready", markers / "late"
    command = _grandchild_launcher(workspace, ready, late, hold_s=1.5)
    # The Web run's workspace is the process cwd; the check must run in it.
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(cli, "make_harness",
                        lambda *a, **kw: _WebHarness(kw.get("gate")))

    # The Stop press is a separate request, exactly as it is in the browser:
    # POST /api/cancel calls _run_cancel on whatever run this session has.
    pressed = threading.Event()
    ready_at = []

    def press_stop_once_the_check_is_alive():
        if not _wait_for(ready.exists, timeout=60):
            return
        ready_at.append(time.monotonic())
        with webapp.Handler._runs_lock:
            row = dict(webapp.Handler._runs.get("web-check-stop") or {})
        if row.get("run"):
            webapp.Handler._run_cancel("web-check-stop", row["run"])
            pressed.set()
    stopper = threading.Thread(target=press_stop_once_the_check_is_alive, daemon=True)
    stopper.start()

    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    webapp.Handler._serve_stream(fake, {
        "q": ["fix the parser"], "session": ["web-check-stop"], "intent": ["build"],
        "quality": ["balanced"], "verification": ["required"],
        "verify_command": [command], "verify_source": ["user"],
        "workspace": ["current"], "strategy": ["single"]})

    stopper.join(timeout=30)
    assert pressed.is_set(), "the Stop press never reached a live run"
    done = next(data for kind, data in events if kind == "done")
    evidence = done["verification_evidence"]
    assert ready.exists(), "the check must really have been running"
    assert evidence["executed"] is True and evidence["cancelled"] is True
    assert evidence["passed"] is False
    assert evidence["process_tree_terminated"] is True
    assert done["canceled"] is True
    assert done["completed"] is False
    assert done["stop_reason"] == "canceled"
    assert "required check failed" not in (done["error"] or "")
    # The agent's own work is not thrown away by the stop.
    assert done["answer"] == "rewrote the tokenizer"
    kinds = [kind for kind, _ in events]
    assert "verification_started" in kinds, "the UI is told a check is running"
    saved = sessions.load("web-check-stop")
    assert saved["run_receipts"][-1]["verified"] is False
    assert saved["run_receipts"][-1]["canceled"] is True
    assert _stayed_absent(late, ready_at[0], 1.5), "the killed tree kept writing"
