import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _snapshot():
    return {
        "commit": "abc",
        "working_tree": "clean",
        "dirty_files": [],
        "tree_digest": "same-bytes",
        "snapshot_complete": True,
        "snapshot_kind": "git",
    }


def test_verification_timeout_kills_process_tree_and_drains_bounded_output(monkeypatch, tmp_path):
    from harness import plat, verification

    calls = {"communicate": 0, "killed": [], "job_closed": 0,
             "job_terminated": 0}

    class FakeJob:
        def terminate_and_wait(self, timeout_s):
            assert timeout_s == 5.0
            calls["job_terminated"] += 1
            return True

        def close(self):
            calls["job_closed"] += 1

    class FakeProcess:
        pid = 321
        returncode = None
        stdout = None

        def communicate(self, input=None, timeout=None):
            calls["communicate"] += 1
            if calls["communicate"] == 1:
                request = __import__("json").loads(input)
                assert request["shell"] in (True, False)
                raise subprocess.TimeoutExpired("check", timeout, output="first-" + "x" * 5000)
            assert input is None
            self.returncode = -9
            return ("drained-" + "y" * 5000, None)

    def fake_popen(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        calls["process"] = FakeProcess()
        return calls["process"]

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(verification.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "new_group_kwargs", lambda: {})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {"creationflags": 123})
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda proc: FakeJob())
    monkeypatch.setattr(plat, "kill_tree", lambda proc: calls["killed"].append(proc))

    evidence = verification.run_verification_command("check", str(tmp_path), timeout=7)

    assert "start_new_session" not in calls["kwargs"]
    assert calls["kwargs"]["creationflags"] == 123
    assert calls["args"][0][:3] == [sys.executable, "-I", "-c"]
    assert calls["kwargs"]["stdin"] == subprocess.PIPE
    assert calls["killed"] == []
    assert calls["job_terminated"] == 1
    assert calls["job_closed"] == 1
    assert calls["communicate"] == 2
    assert evidence["exit_code"] is None
    assert evidence["command_passed"] is False
    assert evidence["passed"] is False
    assert evidence["executed"] is True
    assert evidence["output"].endswith("\n(check timed out after 7s)")
    assert "drained-" not in evidence["output"], "only the bounded tail should survive"
    assert len(evidence["output"]) <= 3500 + len("\n(check timed out after 7s)")


def test_verification_success_uses_popen_and_preserves_receipt_semantics(monkeypatch, tmp_path):
    from harness import plat, verification

    events = []

    class FakeJob:
        def terminate_and_wait(self, timeout_s):
            events.append("tree-extinct")
            return True

        def close(self):
            events.append("job-closed")

    class FakeProcess:
        pid = 987
        returncode = 0

        def communicate(self, input=None, timeout=None):
            assert timeout == 11
            assert events == ["gate-created", "job-attached", "cancel-registered"]
            request = __import__("json").loads(input)
            assert request["shell"] in (True, False)
            events.append("target-released")
            return ("z" * 5000, None)

    def popen(*args, **kwargs):
        assert args[0][:3] == [sys.executable, "-I", "-c"]
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.PIPE
        events.append("gate-created")
        return FakeProcess()

    def attach(proc):
        assert events == ["gate-created"]
        events.append("job-attached")
        return FakeJob()

    def register(proc):
        assert events == ["gate-created", "job-attached"]
        assert isinstance(proc._collie_verification_job, FakeJob)
        events.append("cancel-registered")
        return True

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(verification.subprocess, "Popen", popen)
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "attach_kill_on_close_job", attach)

    evidence = verification.run_verification_command(
        "check", str(tmp_path), timeout=11, source="test", after_last_edit=True,
        on_process=register)

    assert evidence["exit_code"] == 0
    assert evidence["command_passed"] is True
    assert evidence["passed"] is True
    assert evidence["freshness"] == "fresh"
    assert evidence["ran_after_last_edit"] is True
    assert evidence["source"] == "test"
    assert evidence["output"] == "z" * 4000
    assert events == ["gate-created", "job-attached", "cancel-registered",
                      "target-released", "tree-extinct", "job-closed"]


def test_verification_success_reaps_owned_posix_process_group(monkeypatch, tmp_path):
    from harness import plat, verification

    killed = []

    class FakeProcess:
        pid = 777
        returncode = 0

        def communicate(self, input=None, timeout=None):
            assert input is not None
            return ("ok", None)

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(verification.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(plat, "new_group_kwargs", lambda: {"start_new_session": True})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda proc: None)
    def killpg(pgid, sig):
        killed.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError()
    monkeypatch.setattr(verification.os, "killpg", killpg, raising=False)

    evidence = verification.run_verification_command("check", str(tmp_path), timeout=11)

    assert killed == [(777, getattr(signal, "SIGKILL", 9)), (777, 0)]
    assert evidence["exit_code"] == 0
    assert evidence["command_passed"] is True
    assert evidence["passed"] is True


def test_verification_success_fails_closed_when_posix_group_cannot_be_reaped(
        monkeypatch, tmp_path):
    from harness import plat, verification

    class FakeProcess:
        pid = 778
        returncode = 0

        def communicate(self, input=None, timeout=None):
            assert input is not None
            return ("ok", None)

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(verification.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(plat, "new_group_kwargs", lambda: {"start_new_session": True})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda proc: None)
    monkeypatch.setattr(
        verification.os, "killpg",
        lambda pgid, sig: (_ for _ in ()).throw(PermissionError("not owned")),
        raising=False)

    evidence = verification.run_verification_command("check", str(tmp_path), timeout=11)

    assert evidence["exit_code"] == 0
    assert evidence["command_passed"] is True
    assert evidence["passed"] is False
    assert evidence["freshness"] == "process_tree_cleanup_failed"
    assert "could not terminate verification process tree" in evidence["output"]


def test_verification_refuses_unowned_posix_process_group(monkeypatch, tmp_path):
    from harness import plat, verification

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(plat, "new_group_kwargs", lambda: {})
    monkeypatch.setattr(
        verification.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an unowned verifier must not start")))

    evidence = verification.run_verification_command("check", str(tmp_path), timeout=11)

    assert evidence["executed"] is False
    assert evidence["command_passed"] is False
    assert evidence["passed"] is False
    assert "independent verification process-tree ownership" in evidence["output"]


def test_verification_job_assignment_failure_is_fail_closed(monkeypatch, tmp_path):
    from harness import plat, verification

    killed = []

    class FakeProcess:
        pid = 654
        returncode = None

        def communicate(self, input=None, timeout=None):
            raise AssertionError("the repository command gate must never be released")

    process = FakeProcess()
    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(verification.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(
        plat, "attach_kill_on_close_job",
        lambda proc: (_ for _ in ()).throw(OSError("nested job refused")))
    monkeypatch.setattr(plat, "kill_tree", lambda proc: killed.append(proc))

    evidence = verification.run_verification_command("check", str(tmp_path), timeout=11)

    assert killed == [process]
    assert evidence["command_passed"] is False
    assert evidence["passed"] is False
    assert "could not establish verification process-tree ownership" in evidence["output"]


def test_verification_cancel_before_release_never_starts_target_and_confirms_job(
        monkeypatch, tmp_path):
    from harness import plat, verification

    calls = {"released": 0, "terminated": 0, "closed": 0}

    class FakeJob:
        def terminate_and_wait(self, timeout_s):
            calls["terminated"] += 1
            return True

        def close(self):
            calls["closed"] += 1

    class FakeProcess:
        pid = 655
        returncode = None

        def communicate(self, *args, **kwargs):
            calls["released"] += 1
            raise AssertionError("cancelled trusted gate must not be released")

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(
        verification.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda proc: FakeJob())

    evidence = verification.run_verification_command(
        "check", str(tmp_path), timeout=11, on_process=lambda proc: False)

    assert calls == {"released": 0, "terminated": 1, "closed": 1}
    assert evidence["executed"] is False
    assert evidence["cancelled"] is True
    assert evidence["process_tree_terminated"] is True
    assert evidence["passed"] is False


def test_registered_verifier_handle_cancels_started_tree_with_extinction_proof(
        monkeypatch, tmp_path):
    from harness import plat, verification

    calls = {"terminated": 0, "closed": 0, "released": 0}

    class FakeJob:
        def terminate_and_wait(self, timeout_s):
            calls["terminated"] += 1
            return True

        def close(self):
            calls["closed"] += 1

    class FakeProcess:
        pid = 657
        returncode = None

        def communicate(self, input=None, timeout=None):
            assert input is not None
            calls["released"] += 1
            # This stands in for another thread using the handle published by
            # on_process while communicate() is waiting on the real command.
            assert verification.cancel_verification_process(self) is True
            self.returncode = -9
            return ("cancelled", None)

    registered = []
    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(
        verification.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda proc: FakeJob())

    evidence = verification.run_verification_command(
        "check", str(tmp_path), timeout=11,
        on_process=lambda proc: registered.append(proc) or True)

    assert len(registered) == 1
    assert calls == {"terminated": 1, "closed": 1, "released": 1}
    assert evidence["executed"] is True
    assert evidence["exit_code"] == -9
    assert evidence["process_tree_terminated"] is True
    assert evidence["passed"] is False


def test_verification_timeout_fails_closed_without_job_extinction_proof(
        monkeypatch, tmp_path):
    from harness import plat, verification

    class FakeJob:
        def terminate_and_wait(self, timeout_s):
            return False

        def close(self):
            raise RuntimeError("job still active")

    class FakeProcess:
        pid = 656
        returncode = None
        stdout = None
        calls = 0

        def communicate(self, input=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("check", timeout, output="partial")
            return ("partial", None)

    monkeypatch.setattr(verification, "_git_snapshot", lambda cwd: _snapshot())
    monkeypatch.setattr(
        verification.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda proc: FakeJob())

    evidence = verification.run_verification_command("check", str(tmp_path), timeout=1)

    assert evidence["executed"] is True
    assert evidence["process_tree_terminated"] is False
    assert evidence["freshness"] == "process_tree_cleanup_failed"
    assert evidence["passed"] is False
    assert "could not terminate verification process tree" in evidence["output"]


def test_verification_timeout_prevents_grandchild_from_surviving(tmp_path):
    from harness.verification import run_verification_command

    ready = tmp_path / "grandchild-ready"
    survived = tmp_path / "grandchild-survived"
    child_code = (
        "import time; from pathlib import Path; "
        f"Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(1.5); "
        f"Path({str(survived)!r}).write_text('survived')"
    )
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"ready = {str(ready)!r}\n"
        "from pathlib import Path\n"
        "deadline = time.monotonic() + 0.8\n"
        "while not Path(ready).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "print('parent-ready', flush=True)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    python = Path(sys.executable).as_posix().replace('"', '\\"')

    evidence = run_verification_command(
        f'"{python}" launcher.py', str(tmp_path), timeout=1, source="test")

    assert ready.exists(), "the descendant must have started before the timeout"
    assert evidence["exit_code"] is None
    assert evidence["command_passed"] is False
    assert "check timed out after 1s" in evidence["output"]
    time.sleep(1)
    assert not survived.exists(), "the verification timeout must kill descendants, not just the shell"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_verification_success_prevents_background_descendant_from_surviving(tmp_path):
    from harness.verification import run_verification_command

    workspace = tmp_path / "workspace"
    markers = tmp_path / "markers"
    workspace.mkdir()
    markers.mkdir()
    ready = markers / "background-ready"
    survived = markers / "background-survived"
    child_code = (
        "import time; from pathlib import Path; "
        f"Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(1.5); "
        f"Path({str(survived)!r}).write_text('survived')"
    )
    launcher = workspace / "launcher.py"
    launcher.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"ready = {str(ready)!r}\n"
        "from pathlib import Path\n"
        "deadline = time.monotonic() + 0.8\n"
        "while not Path(ready).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "raise SystemExit(0 if Path(ready).exists() else 2)\n",
        encoding="utf-8",
    )
    python = Path(sys.executable).as_posix().replace('"', '\\"')

    evidence = run_verification_command(
        f'"{python}" launcher.py', str(workspace), timeout=5, source="test")

    assert ready.exists(), "the background descendant must start before its parent exits"
    assert evidence["exit_code"] == 0
    assert evidence["command_passed"] is True
    assert evidence["passed"] is True
    time.sleep(2)
    assert not survived.exists(), (
        "a background verifier descendant must not edit after an exit-zero receipt")
