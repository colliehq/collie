import json
import os
import sys
import threading
import time

import pytest

from harness.agent_runners import (
    CodexExecRunner,
    ProcessOutcome,
    RecoveryRequiredError,
    RunnerSnapshot,
    SubprocessRunner,
)


THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"


def _jsonl(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _complete(thread=True, text="done", input_tokens=10, output_tokens=2):
    events = []
    if thread:
        events.append({"type": "thread.started", "thread_id": THREAD})
    events += [
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"id": "item_1", "type": "agent_message", "text": text}},
        {"type": "turn.completed",
         "usage": {"input_tokens": input_tokens, "cached_input_tokens": 4,
                   "output_tokens": output_tokens, "reasoning_output_tokens": 1}},
    ]
    return ProcessOutcome(stdout=_jsonl(*events), exit_code=0)


class FakeProcess:
    pid = 43210


class FakeProcessRunner:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def run(self, argv, *, cwd, stdin_text, timeout_s, on_process):
        self.calls.append({"argv": tuple(argv), "cwd": cwd, "stdin": stdin_text,
                           "timeout": timeout_s})
        on_process(FakeProcess())
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        return outcome


class Snapshots:
    def __init__(self, *digests):
        self.digests = list(digests)

    def __call__(self, _workspace):
        digest = self.digests.pop(0)
        return {"tree_digest": digest, "snapshot_complete": True}


def test_start_is_stdin_safe_bounded_and_serializable(tmp_path):
    prompt = "fix the race --dangerously-bypass-approvals-and-sandbox"
    process = FakeProcessRunner(_complete(text="race fixed"))
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("before", "after"),
                             default_timeout_s=37)

    snapshot = runner.start(prompt, str(tmp_path))

    call = process.calls[0]
    assert prompt == call["stdin"]
    assert prompt not in call["argv"]
    assert call["argv"][-1] == "-"
    assert ("--sandbox", "workspace-write") == (
        call["argv"][call["argv"].index("--sandbox")],
        call["argv"][call["argv"].index("--sandbox") + 1])
    assert "danger-full-access" not in call["argv"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in call["argv"]
    assert call["cwd"] == os.path.realpath(str(tmp_path))
    assert call["timeout"] == 37

    assert snapshot.thread_id == THREAD
    assert snapshot.cursor == 4
    assert [event.cursor for event in snapshot.events] == [1, 2, 3, 4]
    assert snapshot.usage == {"input_tokens": 10, "cached_input_tokens": 4,
                              "output_tokens": 2, "reasoning_output_tokens": 1}
    assert snapshot.final_output == "race fixed"
    assert snapshot.settled is True
    assert snapshot.mutated is True
    assert snapshot.recovery_required is False
    assert RunnerSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_resume_uses_exact_thread_and_accumulates_cursor_events_usage(tmp_path):
    process = FakeProcessRunner(
        _complete(text="first", input_tokens=10, output_tokens=2),
        _complete(thread=False, text="second", input_tokens=7, output_tokens=3),
    )
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("a", "b", "b", "c"))

    first = runner.start("first turn", str(tmp_path), timeout_s=11)
    second = runner.resume(first, "continue privately", timeout_s=22)

    call = process.calls[1]
    assert call["argv"][:3] == ("codex", "exec", "resume")
    assert "--json" in call["argv"]
    assert 'sandbox_mode="workspace-write"' in call["argv"]
    assert call["argv"][-2:] == (THREAD, "-")
    assert "continue privately" not in call["argv"]
    assert call["stdin"] == "continue privately"
    assert call["timeout"] == 22
    assert second.thread_id == THREAD
    assert second.cursor == 7
    assert [event.cursor for event in second.events] == list(range(1, 8))
    assert second.usage == {"input_tokens": 17, "cached_input_tokens": 8,
                            "output_tokens": 5, "reasoning_output_tokens": 2}
    assert second.final_output == "second"
    assert second.invocation == 2
    assert second.settled is True


def test_abnormal_mutating_turn_requires_manual_recovery_and_cannot_resume(tmp_path):
    interrupted = ProcessOutcome(
        stdout=_jsonl(
            {"type": "thread.started", "thread_id": THREAD},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "file_change"}},
        ), exit_code=-9, timed_out=True)
    process = FakeProcessRunner(interrupted, _complete(thread=False))
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("clean", "partial"))

    snapshot = runner.start("make a change", str(tmp_path), timeout_s=5)

    assert snapshot.settled is False
    assert snapshot.timed_out is True
    assert snapshot.mutated is True
    assert snapshot.recovery_required is True
    assert "wall timeout" in snapshot.error
    with pytest.raises(RecoveryRequiredError, match="partial workspace mutation"):
        runner.resume(snapshot, "try that again")
    assert len(process.calls) == 1


def test_abnormal_non_mutating_turn_can_resume_same_thread(tmp_path):
    interrupted = ProcessOutcome(
        stdout=_jsonl({"type": "thread.started", "thread_id": THREAD},
                      {"type": "turn.started"}),
        stderr="rate limited", exit_code=1)
    process = FakeProcessRunner(interrupted, _complete(thread=False, text="recovered"))
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("same", "same", "same", "new"))

    failed = runner.start("begin", str(tmp_path))
    assert failed.recovery_required is False
    assert failed.error == "rate limited"

    recovered = runner.resume(failed, "continue")
    assert recovered.settled is True
    assert recovered.final_output == "recovered"


def test_invalid_protocol_after_mutation_is_recovery_required(tmp_path):
    process = FakeProcessRunner(ProcessOutcome(
        stdout=("not-json\n" + _jsonl(
            {"type": "thread.started", "thread_id": THREAD},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        )), exit_code=0))
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("before", "after"))

    snapshot = runner.start("edit", str(tmp_path))

    assert snapshot.settled is False
    assert snapshot.recovery_required is True
    assert snapshot.events[0].type == "protocol.invalid_json"
    assert "invalid" in snapshot.error


def test_completed_stream_without_thread_id_is_not_treated_as_settled(tmp_path):
    process = FakeProcessRunner(_complete(thread=False))
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("same", "same"))

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.settled is False
    assert snapshot.recovery_required is False
    assert "inconsistent JSONL" in snapshot.error


def test_event_tail_is_bounded_but_cursor_remains_monotonic(tmp_path):
    process = FakeProcessRunner(_complete())
    runner = CodexExecRunner(process_runner=process, max_events=2,
                             snapshotter=Snapshots("a", "a"))

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.cursor == 4
    assert [event.cursor for event in snapshot.events] == [3, 4]


def test_cancel_current_kills_active_tree_and_marks_snapshot(monkeypatch, tmp_path):
    entered = threading.Event()
    released = threading.Event()
    killed = []

    class BlockingRunner:
        def run(self, argv, *, cwd, stdin_text, timeout_s, on_process):
            proc = FakeProcess()
            on_process(proc)
            entered.set()
            assert released.wait(3)
            return ProcessOutcome(exit_code=-9)

    def kill_tree(proc):
        killed.append(proc)
        released.set()

    monkeypatch.setattr("harness.agent_runners.plat.kill_tree", kill_tree)
    runner = CodexExecRunner(process_runner=BlockingRunner(),
                             snapshotter=lambda _p: {
                                 "tree_digest": "same", "snapshot_complete": True})
    result = []
    worker = threading.Thread(
        target=lambda: result.append(runner.start("wait", str(tmp_path))), daemon=True)
    worker.start()
    assert entered.wait(2)

    assert runner.cancel_current() is True
    worker.join(3)

    assert not worker.is_alive()
    assert len(killed) == 1
    assert result[0].cancelled is True
    assert result[0].settled is False
    assert result[0].recovery_required is False
    assert runner.cancel_current() is False


def test_process_exception_after_observed_mutation_requires_recovery(tmp_path):
    marker = tmp_path / "marker.txt"
    marker.write_text("before", encoding="utf-8")

    class RaisingRunner:
        def run(self, argv, *, cwd, stdin_text, timeout_s, on_process):
            on_process(FakeProcess())
            marker.write_text("after", encoding="utf-8")
            raise OSError("transport vanished")

    def marker_snapshot(_workspace):
        return {"tree_digest": marker.read_text(encoding="utf-8"),
                "snapshot_complete": True}

    runner = CodexExecRunner(process_runner=RaisingRunner(),
                             snapshotter=marker_snapshot)
    snapshot = runner.start("mutate", str(tmp_path))

    assert snapshot.recovery_required is True
    assert snapshot.mutated is True
    assert "transport vanished" in snapshot.error


def test_thread_id_and_prompt_validation_happen_before_resume_process(tmp_path):
    process = FakeProcessRunner(_complete())
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("a", "a"))
    unsafe = RunnerSnapshot(runner="codex-exec", workspace=os.path.realpath(str(tmp_path)),
                            thread_id="--last")

    with pytest.raises(ValueError, match="safe Codex thread id"):
        runner.resume(unsafe, "continue")
    with pytest.raises(ValueError, match="NUL"):
        runner.start("bad\x00prompt", str(tmp_path))
    assert process.calls == []


def test_default_process_transport_enforces_wall_timeout_without_duplicate_output(tmp_path):
    started = time.monotonic()
    outcome = SubprocessRunner().run(
        [sys.executable, "-c", "import time; print('ONE', flush=True); time.sleep(10)"],
        cwd=str(tmp_path), stdin_text="not on argv", timeout_s=0.15,
        on_process=lambda _proc: None)

    assert time.monotonic() - started < 5
    assert outcome.timed_out is True
    assert outcome.stdout.count("ONE") == 1


def test_default_transport_holds_target_behind_registration_gate(tmp_path):
    marker = tmp_path / "target-started"
    prompt_copy = tmp_path / "prompt.txt"
    prompt = "private prompt --never-on-argv"
    script = (
        "import sys; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('started'); "
        f"Path({str(prompt_copy)!r}).write_text(sys.stdin.read())"
    )
    observed = []

    def register(_proc):
        # A direct Popen(target) can run this immediate write while registration
        # is sleeping.  The trusted gate cannot spawn it until we return.
        time.sleep(.15)
        observed.append(marker.exists())
        return True

    outcome = SubprocessRunner().run(
        [sys.executable, "-c", script], cwd=str(tmp_path),
        stdin_text=prompt, timeout_s=5, on_process=register)

    assert observed == [False]
    assert outcome.exit_code == 0
    assert marker.exists()
    assert prompt_copy.read_text(encoding="utf-8") == prompt


def test_default_transport_cancel_at_registration_never_starts_target(tmp_path):
    marker = tmp_path / "must-not-start"
    script = (
        "from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('escaped')"
    )

    outcome = SubprocessRunner().run(
        [sys.executable, "-c", script], cwd=str(tmp_path), stdin_text="stop",
        timeout_s=5, on_process=lambda _proc: False)

    assert outcome.cancelled is True
    assert not marker.exists()


def test_cancel_during_launch_waits_for_gate_and_prevents_release(monkeypatch, tmp_path):
    launch_entered = threading.Event()
    permit_registration = threading.Event()
    target_started = threading.Event()
    killed = []

    class DelayedRegistrationRunner:
        def run(self, argv, *, cwd, stdin_text, timeout_s, on_process):
            from harness.agent_runners import plat
            launch_entered.set()
            assert permit_registration.wait(3)
            proc = FakeProcess()
            allowed = on_process(proc)
            if allowed:
                target_started.set()
            else:
                plat.kill_tree(proc)
            return ProcessOutcome(exit_code=-9, cancelled=not allowed)

    monkeypatch.setattr(
        "harness.agent_runners.plat.kill_tree", lambda proc: killed.append(proc))
    runner = CodexExecRunner(
        process_runner=DelayedRegistrationRunner(),
        snapshotter=lambda _p: {"tree_digest": "same", "snapshot_complete": True})
    snapshots = []
    worker = threading.Thread(
        target=lambda: snapshots.append(runner.start("work", str(tmp_path))),
        daemon=True)
    worker.start()
    assert launch_entered.wait(2)

    cancelled = []
    canceller = threading.Thread(
        target=lambda: cancelled.append(runner.cancel_current()), daemon=True)
    canceller.start()
    time.sleep(.05)
    assert canceller.is_alive(), "cancel waits until the launch gate is owned"
    permit_registration.set()
    canceller.join(3)
    worker.join(3)

    assert cancelled == [True]
    assert len(killed) == 1
    assert not target_started.is_set()
    assert snapshots[0].cancelled is True
    assert snapshots[0].settled is False


def test_cancel_reports_false_when_job_extinction_is_unconfirmed(tmp_path):
    calls = []

    class Owner:
        def terminate_and_wait(self, *, timeout_s):
            calls.append(timeout_s)
            return False

    proc = FakeProcess()
    proc._collie_kill_job = Owner()
    runner = CodexExecRunner(
        process_runner=FakeProcessRunner(),
        snapshotter=lambda _p: {"tree_digest": "same", "snapshot_complete": True})
    with runner._active_condition:
        runner._active_process = proc
        runner._starting = False

    assert runner.cancel_current() is False
    assert len(calls) == 1
    assert calls[0] > 0


def test_windows_job_termination_waits_for_zero_active_processes(monkeypatch):
    from harness import plat

    job = object.__new__(plat._KillOnCloseJob)
    job._lock = threading.RLock()
    job._handle = 123
    job._extinct = False
    active = iter((2, 1, 0))
    queries = []

    def active_processes():
        value = next(active)
        queries.append(value)
        if value == 0:
            job._extinct = True
        return value

    class Kernel:
        def TerminateJobObject(self, handle, exit_code):
            assert handle == 123 and exit_code == 9
            return True

    job._active_processes_locked = active_processes
    job._kernel = Kernel()

    assert job.terminate_and_wait(exit_code=9, timeout_s=1) is True
    assert queries == [2, 1, 0]
