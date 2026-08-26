import base64
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from harness import agent_runners, runner_env
from harness.agent_runners import (
    CodexExecRunner,
    ProcessOutcome,
    RecoveryRequiredError,
    RunnerSnapshot,
    SubprocessRunner,
)


THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"

# Prefixes `assert_no_billing_override` refuses to start a Codex worker against.
_CODEX_BILLING_PREFIXES = ("OPENAI_", "CODEX_", "AZURE_OPENAI_")


@pytest.fixture(autouse=True)
def _shell_without_codex_routing(monkeypatch):
    """Run every case as if the developer's shell exported no Codex overrides.

    `CodexExecRunner` refuses to start when the parent environment could re-bill
    or re-route the turn.  That refusal is the point, and it is asserted head-on
    by `test_billing_override_in_parent_env_refuses_to_start` — but leaving it
    ambient would make this whole module go red on any machine that happens to
    export OPENAI_API_KEY, which says nothing about the code under test.
    """
    for name in list(os.environ):
        upper = name.upper()
        if upper == "CODEX_HOME":  # selects the login file, not the payer
            continue
        if upper.startswith(_CODEX_BILLING_PREFIXES):
            monkeypatch.delenv(name, raising=False)


def _jwt(exp):
    """A structurally valid JWT whose only readable claim is ``exp``."""
    def chunk(value):
        raw = json.dumps(value).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.%s" % (chunk({"alg": "none"}), chunk({"exp": int(exp)}), "signature")


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

    def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
        self.calls.append({"argv": tuple(argv), "cwd": cwd, "stdin": stdin_text,
                           "timeout": timeout_s, "env": env})
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


def test_snapshot_deserialization_is_bounded_and_fail_closed(tmp_path):
    raw = {
        "runner": "codex-exec",
        "workspace": str(tmp_path),
        "thread_id": THREAD,
        "cursor": float("nan"),
        "events": [
            {"cursor": index, "type": "message", "payload": {"text": "ok"},
             "at": float("inf")}
            for index in range(10_005)
        ],
        "usage": {"input_tokens": float("inf"), "bool_is_not_usage": True,
                  "cost": 0.125},
        # JSON corruption must not turn truthy strings into permission/state facts.
        "settled": "false",
        "recovery_required": "false",
        "mutated": "false",
        "final_output": "Authorization: Bearer secret-token-123456789",
    }

    snapshot = RunnerSnapshot.from_dict(raw)

    assert snapshot.cursor == 0
    assert len(snapshot.events) == 10_000
    assert snapshot.events[0].cursor == 5
    assert snapshot.events[-1].at == 0.0
    assert snapshot.usage == {"cost": 0.125}
    assert snapshot.settled is snapshot.recovery_required is snapshot.mutated is False
    assert "secret-token-123456789" not in snapshot.final_output


def test_snapshot_without_workspace_does_not_default_to_process_cwd():
    assert RunnerSnapshot.from_dict({"runner": "codex-exec"}).workspace == ""


def test_event_type_and_mapping_keys_cannot_leak_credentials():
    secret = "sk-eventsecret123456789"
    event = agent_runners.RunnerEvent.from_dict({
        "type": secret, "payload": {secret: "safe"},
    })

    encoded = json.dumps(event.to_dict())
    assert secret not in encoded
    assert "[redacted]" in encoded


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


def test_non_standard_json_number_is_a_protocol_error(tmp_path):
    process = FakeProcessRunner(ProcessOutcome(
        stdout=_jsonl(
            {"type": "thread.started", "thread_id": THREAD},
        ) + '{"type":"turn.completed","usage":{"input_tokens":NaN}}\n',
        exit_code=0))
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("same", "same"))

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.settled is False
    assert snapshot.events[-1].type == "protocol.invalid_json"
    # The literal survives only as diagnostic text; no non-standard numeric
    # value survives into the serialized snapshot.
    assert json.dumps(snapshot.to_dict(), allow_nan=False)


def test_excessively_nested_json_becomes_protocol_evidence_not_an_exception(tmp_path):
    nested = '{"type":"message","payload":' + ("[" * 2000) + "0" + ("]" * 2000) + "}\n"
    runner = CodexExecRunner(
        process_runner=FakeProcessRunner(ProcessOutcome(stdout=nested, exit_code=0)),
        snapshotter=Snapshots("same", "same"))

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.settled is False
    assert snapshot.events[-1].type == "protocol.invalid_json"


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
        def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
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
        def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
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
        # The transport starts a trusted ownership gate and then the target.
        # Leave enough time for both interpreters to initialize on cold Windows
        # hosts before testing timeout capture/deduplication itself.
        cwd=str(tmp_path), stdin_text="not on argv", timeout_s=1.0,
        on_process=lambda _proc: None)

    assert time.monotonic() - started < 5
    assert outcome.timed_out is True
    assert outcome.stdout.count("ONE") == 1


def test_default_transport_delivers_complete_stdout_records_before_exit(tmp_path):
    first = threading.Event()
    records = []
    outcomes = []

    def consume(record):
        records.append(record)
        if "FIRST" in record:
            first.set()

    worker = threading.Thread(target=lambda: outcomes.append(
        SubprocessRunner().run(
            [sys.executable, "-c",
             "import time; print('FIRST', flush=True); time.sleep(1); "
             "print('SECOND', flush=True)"],
            cwd=str(tmp_path), stdin_text="private", timeout_s=5,
            on_process=lambda _proc: True, on_stdout=consume)), daemon=True)
    worker.start()

    assert first.wait(3), "the first flushed record was buffered until process exit"
    assert worker.is_alive(), "the callback must run while the target is still active"
    worker.join(5)

    assert not worker.is_alive()
    assert outcomes[0].exit_code == 0
    assert "".join(records) == outcomes[0].stdout
    assert [record.strip() for record in records] == ["FIRST", "SECOND"]


def test_streaming_transport_bounds_captured_output_and_skips_partial_record(tmp_path):
    records = []
    outcome = SubprocessRunner(max_output_chars=65_536).run(
        [sys.executable, "-c", "print('X' * 70000, flush=True)"],
        cwd=str(tmp_path), stdin_text="private", timeout_s=5,
        on_process=lambda _proc: True, on_stdout=records.append)

    assert outcome.exit_code == 0
    assert outcome.output_truncated is True
    assert len(outcome.stdout) == 65_536
    assert records == []


def test_non_streaming_transport_is_bounded_too(tmp_path):
    outcome = SubprocessRunner(max_output_chars=65_536).run(
        [sys.executable, "-c", "print('Y' * 70000, flush=True)"],
        cwd=str(tmp_path), stdin_text="private", timeout_s=5,
        on_process=lambda _proc: True)

    assert outcome.exit_code == 0
    assert outcome.output_truncated is True
    assert len(outcome.stdout) == 65_536


def test_transport_never_uses_unbounded_readline_for_one_huge_record():
    class SizedStream:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def readline(self, size=-1):
            assert 0 < size <= 65_536
            return self.chunks.pop(0) if self.chunks else ""

    class Sink:
        def write(self, _value):
            return None

        def flush(self):
            return None

        def close(self):
            return None

    class Proc:
        def __init__(self):
            self.stdout = SizedStream(["Z" * 65_536, "Z" * 4_464 + "\n"])
            self.stderr = SizedStream([])
            self.stdin = Sink()
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    records = []
    outcome = SubprocessRunner(max_output_chars=65_536)._streaming_communicate(
        Proc(), "private", 5, records.append)

    assert outcome.output_truncated is True
    assert len(outcome.stdout) == 65_536
    assert records == []


def test_codex_parser_keeps_constant_event_tail_but_aggregates_whole_stream(tmp_path):
    rows = ([{"type": "thread.started", "thread_id": THREAD}] +
            [{"type": "turn.started", "seq": i} for i in range(5_000)] +
            [{"type": "item.completed", "item": {
                "type": "agent_message", "text": "bounded"}},
             {"type": "turn.completed", "usage": {"input_tokens": 7}}])
    runner = CodexExecRunner(
        process_runner=FakeProcessRunner(ProcessOutcome(
            stdout=_jsonl(*rows), exit_code=0)),
        snapshotter=Snapshots("same", "same"), max_events=3)

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.settled is True
    assert snapshot.cursor == len(rows)
    assert len(snapshot.events) == 3
    assert snapshot.thread_id == THREAD
    assert snapshot.final_output == "bounded"
    assert snapshot.usage["input_tokens"] == 7


def test_codex_rejects_oversized_single_event_before_json_materialization(tmp_path):
    rows = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "S" * 2_000}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ]
    runner = CodexExecRunner(
        process_runner=FakeProcessRunner(ProcessOutcome(
            stdout=_jsonl(*rows), exit_code=0)),
        snapshotter=Snapshots("same", "same"), max_event_chars=1_024)

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.settled is False
    assert snapshot.final_output == ""
    assert any(event.type == "protocol.event_too_large"
               for event in snapshot.events)


def test_codex_rejects_duplicate_terminal_events(tmp_path):
    complete = _complete()
    stdout = complete.stdout + _jsonl(
        {"type": "turn.completed", "usage": {"input_tokens": 1}})
    runner = CodexExecRunner(
        process_runner=FakeProcessRunner(ProcessOutcome(stdout=stdout, exit_code=0)),
        snapshotter=Snapshots("same", "same"))

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.settled is False
    assert "inconsistent JSONL" in snapshot.error


def test_codex_rejects_records_after_terminal_event(tmp_path):
    complete = _complete()
    stdout = complete.stdout + _jsonl({"type": "item.completed", "item": {
        "type": "agent_message", "text": "too late"}})
    runner = CodexExecRunner(
        process_runner=FakeProcessRunner(ProcessOutcome(stdout=stdout, exit_code=0)),
        snapshotter=Snapshots("same", "same"))

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.settled is False
    assert snapshot.final_output == "too late"
    assert "inconsistent JSONL" in snapshot.error


def test_codex_runner_projects_jsonl_records_live_without_changing_snapshot(tmp_path):
    outcome = _complete(text="streamed")
    projected = []

    class StreamingTransport:
        def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None,
                on_stdout=None):
            on_process(FakeProcess())
            for record in outcome.stdout.splitlines(keepends=True):
                on_stdout(record)
            return outcome

    runner = CodexExecRunner(
        process_runner=StreamingTransport(),
        snapshotter=Snapshots("same", "same"), event_callback=projected.append)
    snapshot = runner.start("inspect", str(tmp_path))

    assert [event.type for event in projected] == [
        "thread.started", "turn.started", "item.completed", "turn.completed"]
    assert [event.cursor for event in projected] == [1, 2, 3, 4]
    assert [event.type for event in snapshot.events] == [event.type for event in projected]
    assert snapshot.settled is True and snapshot.final_output == "streamed"


def test_codex_jsonl_uses_only_lf_not_unicode_line_separators(tmp_path):
    records = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "item_1", "type": "agent_message",
            "text": "before\u2028middle\u2029after"}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 1, "output_tokens": 2}},
    ]
    stdout = "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n"
    outcome = ProcessOutcome(stdout=stdout, exit_code=0)
    projected = []

    class LfTransport:
        def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None,
                on_stdout=None):
            on_process(FakeProcess())
            for raw in stdout.split("\n")[:-1]:
                on_stdout(raw + "\n")
            return outcome

    runner = CodexExecRunner(
        process_runner=LfTransport(), snapshotter=Snapshots("same", "same"),
        event_callback=projected.append)
    snapshot = runner.start("inspect", str(tmp_path))

    assert [event.type for event in projected] == [row["type"] for row in records]
    assert [event.type for event in snapshot.events] == [row["type"] for row in records]
    assert snapshot.final_output == "before\u2028middle\u2029after"
    assert snapshot.settled is True


def test_codex_snapshot_redacts_native_events_final_output_and_stderr(tmp_path):
    process = FakeProcessRunner(ProcessOutcome(
        stdout=_complete(text="credential ghp_1234567890abcdef").stdout,
        stderr="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        exit_code=1))
    runner = CodexExecRunner(
        process_runner=process, snapshotter=Snapshots("same", "same"))

    snapshot = runner.start("inspect", str(tmp_path))
    serialized = json.dumps(snapshot.to_dict())

    assert "ghp_1234567890abcdef" not in serialized
    assert "eyJhbGciOiJIUzI1NiJ9" not in serialized
    assert "Bearer " not in serialized
    assert "[redacted]" in serialized


def test_codex_refuses_to_settle_when_transport_truncated_output(tmp_path):
    complete = _complete(text="looks complete")
    process = FakeProcessRunner(ProcessOutcome(
        stdout=complete.stdout, exit_code=0, output_truncated=True))
    runner = CodexExecRunner(
        process_runner=process, snapshotter=Snapshots("same", "same"))

    snapshot = runner.start("inspect", str(tmp_path))

    assert snapshot.settled is False
    assert "bounded capture limit" in snapshot.error


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
        def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
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


def test_run_passes_env_to_popen(tmp_path, monkeypatch):
    """The child gets exactly the mapping we hand the transport — and only then.

    The environment is set on the *ownership gate*, which spawns the target
    without an env of its own, so proving it on the target proves it for both.
    ``env=None`` has to keep inheriting, because that is what every caller
    predating the selection layer relies on.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-key-must-not-leak")
    dump = tmp_path / "env.json"
    script = ("import json, os; "
              "open(%r, 'w', encoding='utf-8').write(json.dumps(dict(os.environ)))"
              % str(dump))

    inherited = SubprocessRunner().run(
        [sys.executable, "-c", script], cwd=str(tmp_path), stdin_text="ignored",
        timeout_s=30, on_process=lambda _proc: None)
    assert inherited.exit_code == 0
    assert json.loads(dump.read_text(encoding="utf-8")).get(
        "OPENAI_API_KEY") == "sk-parent-key-must-not-leak"

    env, receipt = runner_env.child_env("codex", extra={"COLLIE_RUNNER_TEST": "kept"})
    isolated = SubprocessRunner().run(
        [sys.executable, "-c", script], cwd=str(tmp_path), stdin_text="ignored",
        timeout_s=30, on_process=lambda _proc: None, env=env)

    assert isolated.exit_code == 0
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert seen.get("COLLIE_RUNNER_TEST") == "kept"
    assert "OPENAI_API_KEY" not in seen
    assert "OPENAI_API_KEY" in receipt["stripped"]
    # Names only: a receipt that quoted the value would be the leak it documents.
    assert "sk-parent-key-must-not-leak" not in json.dumps(receipt)


def test_start_argv_hardened_no_ephemeral(tmp_path):
    """The host's ~/.codex/config.toml must not reach a delegated worker."""
    process = FakeProcessRunner(_complete())
    runner = CodexExecRunner(process_runner=process, snapshotter=Snapshots("a", "a"))

    runner.start("harden me", str(tmp_path))

    argv = process.calls[0]["argv"]
    for flag in ("--ignore-user-config", "--ignore-rules", "--strict-config"):
        assert flag in argv, flag
    # Approval policy rides on -c, never on --ask-for-approval: `codex exec`
    # 0.149.0 rejects that flag ("unexpected argument") and would exit 2 before
    # reading the prompt.  Verified against the real CLI on 2026-08-22.
    assert "--ask-for-approval" not in argv
    assert 'approval_policy="never"' in argv
    assert 'web_search="disabled"' in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    # --ephemeral would discard the thread that `resume` is addressed to.
    assert "--ephemeral" not in argv
    assert argv[-1] == "-"


def test_start_argv_sets_windows_sandbox_level(tmp_path, monkeypatch):
    """--ignore-user-config drops [windows] sandbox, which defaults to Disabled.

    With no platform sandbox to enforce the boundary, Codex rejects every patch
    ("writing is blocked by read-only sandbox") *and still exits 0*, so the only
    symptom is a turn that changed nothing.  Measured on 0.149.0 / Windows 11.
    """
    monkeypatch.setattr(agent_runners.plat, "is_windows", lambda: True)
    process = FakeProcessRunner(_complete())
    runner = CodexExecRunner(process_runner=process, snapshotter=Snapshots("a", "a"))

    runner.start("harden me", str(tmp_path))

    argv = process.calls[0]["argv"]
    assert 'windows.sandbox="unelevated"' in argv
    # The private desktop cannot be created from the start gate (CREATE_NO_WINDOW,
    # no console), and Codex degrades to refusing writes rather than erroring.
    assert "windows.sandbox_private_desktop=false" in argv

    monkeypatch.setattr(agent_runners.plat, "is_windows", lambda: False)
    posix = FakeProcessRunner(_complete())
    CodexExecRunner(process_runner=posix, snapshotter=Snapshots("a", "a")).start(
        "harden me", str(tmp_path))
    # Seatbelt/bwrap are unconditional on POSIX; forcing a Windows-only key
    # there would trip --strict-config.
    assert not [a for a in posix.calls[0]["argv"] if "windows.sandbox" in a]


def test_policy_refused_turn_is_not_a_clean_settle(tmp_path):
    """A turn that changed nothing because policy refused every write is a failure.

    Codex logs the refusal on stderr and still exits 0 with `turn.completed`, so
    a naive reading reports success for a turn that accomplished nothing -- and a
    Mission slice would bank it as progress.  Reproduced against the real CLI on
    Windows 11 / 0.149.0 on 2026-08-22.
    """
    outcome = _complete(text="I could not modify hello.py")
    refused = ProcessOutcome(
        stdout=outcome.stdout,
        stderr=("2026-08-22T23:06:36Z ERROR codex_core::tools::router: error=patch "
                "rejected: writing is blocked by read-only sandbox; rejected by "
                "user approval settings\n"),
        exit_code=0)
    process = FakeProcessRunner(refused)
    # Same digest before and after: the workspace really is untouched.
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("same", "same"))

    snapshot = runner.start("edit the file", str(tmp_path))

    assert snapshot.settled is False
    assert snapshot.mutated is False
    assert "refused every write" in snapshot.error
    assert "read-only sandbox" in snapshot.error
    # Nothing was written, so there is nothing to reconcile before a retry.
    assert snapshot.recovery_required is False


def test_ordinary_tool_error_still_settles(tmp_path):
    """Only Codex's own policy wording counts; recoverable tool errors do not.

    `apply_patch verification failed` is a routine miss that the model retries in
    the same turn, so treating it as a refusal would fail healthy runs.
    """
    outcome = _complete(text="fixed it on the second try")
    noisy = ProcessOutcome(
        stdout=outcome.stdout,
        stderr=("ERROR codex_core::tools::router: error=apply_patch verification "
                "failed: Failed to find expected lines\n"),
        exit_code=0)
    runner = CodexExecRunner(process_runner=FakeProcessRunner(noisy),
                             snapshotter=Snapshots("before", "after"))

    snapshot = runner.start("edit the file", str(tmp_path))

    assert snapshot.settled is True
    assert snapshot.error == ""


def test_resume_argv_keeps_sandbox_override(tmp_path):
    """`exec resume` has no --sandbox flag, so both bounds ride on -c."""
    process = FakeProcessRunner(_complete(), _complete(thread=False))
    runner = CodexExecRunner(process_runner=process,
                             snapshotter=Snapshots("a", "a", "a", "a"))

    first = runner.start("first", str(tmp_path))
    runner.resume(first, "second")

    argv = process.calls[1]["argv"]
    assert argv[:4] == ("codex", "exec", "resume", "--json")
    assert 'sandbox_mode="workspace-write"' in argv
    assert 'web_search="disabled"' in argv
    assert 'approval_policy="never"' in argv
    # Host-config isolation must survive the resume too, or a thread silently
    # regains the user's MCP servers half way through.  Verified accepted by
    # `codex exec resume` 0.149.0 on 2026-08-22.
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--strict-config" in argv
    assert "--ephemeral" not in argv
    assert argv[-2:] == (THREAD, "-")


def test_start_uses_isolated_child_env_and_records_names_only(tmp_path, monkeypatch):
    """The worker reads its own ~/.codex login, never the shell's credential.

    ANTHROPIC_API_KEY rather than OPENAI_API_KEY: an OpenAI key in the parent is
    a *billing route* for this family and refuses the launch outright (see
    `test_billing_override_in_parent_env_refuses_to_start`).  A foreign vendor's
    key cannot misbill a Codex run, so stripping it silently is the whole answer
    — and stripping is what this case is about.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-the-worker")
    process = FakeProcessRunner(_complete())
    runner = CodexExecRunner(process_runner=process, snapshotter=Snapshots("a", "a"))

    runner.start("go", str(tmp_path))

    env = process.calls[0]["env"]
    assert env is not None and "ANTHROPIC_API_KEY" not in env
    assert env.get("NO_COLOR") == "1"
    assert "PATH" in env
    assert "ANTHROPIC_API_KEY" in runner.last_env_receipt["stripped"]
    assert "sk-not-for-the-worker" not in json.dumps(runner.last_env_receipt)


def test_billing_override_in_parent_env_refuses_to_start(tmp_path, monkeypatch):
    """Refuse before a process exists, not after an unattributable run."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://proxy.invalid/v1")
    process = FakeProcessRunner(_complete())
    runner = CodexExecRunner(process_runner=process, snapshotter=Snapshots("a", "a"))

    with pytest.raises(runner_env.BillingOverrideError, match="OPENAI_BASE_URL"):
        runner.start("go", str(tmp_path))

    assert process.calls == []


def test_cancel_for_delegates(tmp_path):
    """The registry cancels by key; this runner owns one turn, so key is noise."""
    runner = CodexExecRunner(process_runner=FakeProcessRunner(),
                             snapshotter=lambda _p: {"tree_digest": "same",
                                                     "snapshot_complete": True})
    calls = []
    runner.cancel_current = lambda: (calls.append("cancelled"), True)[1]

    assert runner.cancel_for("codex-exec") is True
    assert runner.cancel_for() is True
    assert calls == ["cancelled", "cancelled"]


def test_probe_reads_metadata_only(tmp_path, monkeypatch):
    """which + one --version + the shape of auth.json.  No token, no network."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    secret = _jwt(time.time() + 3600)
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": secret, "refresh_token": "rt-secret-value",
                   "id_token": "id-secret-value"},
        "OPENAI_API_KEY": "sk-secret-value",
    }), encoding="utf-8")
    fake_cli = str(tmp_path / "codex.cmd")
    monkeypatch.setattr(agent_runners.shutil, "which",
                        lambda name: fake_cli if name == "codex" else None)

    spawned = []

    def fake_run(argv, **kwargs):
        spawned.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "codex-cli 0.149.0\n", "")

    monkeypatch.setattr(agent_runners.subprocess, "run", fake_run)

    probe = CodexExecRunner().probe(codex_home=str(codex_home))

    # `codex login status` is a live probe (it can hit the network); the registry
    # owns it.  --version is the only child this path is allowed to start.
    assert spawned == [(fake_cli, "--version")]
    assert probe.installed is True
    assert probe.executable_path == fake_cli
    assert probe.version == "codex-cli 0.149.0"
    assert probe.login == "ok"
    assert probe.usable() is True
    # An OAuth login is not evidence of *which* plan pays; --live decides that.
    assert probe.billing_class == "unknown"
    assert probe.billing_evidence["login_kind"] == "chatgpt"
    assert probe.billing_evidence["expires_at"] > time.time()
    serialized = json.dumps(probe.to_dict())
    for value in (secret, "rt-secret-value", "id-secret-value", "sk-secret-value"):
        assert value not in serialized


def test_probe_reports_missing_and_expired_logins(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runners.shutil, "which", lambda _name: None)
    absent = CodexExecRunner().probe(codex_home=str(tmp_path / "nope"))
    assert absent.installed is False and absent.usable() is False
    assert "not installed" in absent.detail

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": _jwt(time.time() - 60)}}),
        encoding="utf-8")
    monkeypatch.setattr(agent_runners.shutil, "which", lambda _name: str(tmp_path / "codex"))
    monkeypatch.setattr(agent_runners, "_cli_version", lambda _exe: ("codex-cli 0.149.0", ""))

    # No refresh token, so the next turn would stop at a login prompt with the
    # prompt already delivered.  A refresh token makes the same file usable.
    expired = CodexExecRunner().probe(codex_home=str(codex_home))
    assert expired.login == "expired" and expired.usable() is False

    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": _jwt(time.time() - 60),
                               "refresh_token": "rt"}}), encoding="utf-8")
    assert CodexExecRunner().probe(codex_home=str(codex_home)).login == "ok"


def test_probe_rejects_nonstandard_numbers_in_auth_metadata(tmp_path, monkeypatch):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        '{"tokens":{"access_token":"x"},"metadata":NaN}', encoding="utf-8")
    monkeypatch.setattr(agent_runners.shutil, "which", lambda _name: str(tmp_path / "codex"))
    monkeypatch.setattr(agent_runners, "_cli_version", lambda _exe: ("codex-cli 0.149.0", ""))

    probe = CodexExecRunner().probe(codex_home=str(codex_home))

    assert probe.login == "unknown"
    assert probe.billing_evidence["login_kind"] == "unreadable"
    assert probe.usable() is False


def test_usage_reads_token_usage_alias_and_cache_writes(tmp_path):
    """Codex 0.149.0 may spell the block `token_usage`; cache writes are billed."""
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "turn.started"},
        {"type": "turn.completed",
         "token_usage": {"input_tokens": 9, "cached_input_tokens": 3,
                         "output_tokens": 4, "reasoning_output_tokens": 2,
                         "cache_write_input_tokens": 7}},
    ]
    process = FakeProcessRunner(ProcessOutcome(stdout=_jsonl(*events), exit_code=0))
    runner = CodexExecRunner(process_runner=process, snapshotter=Snapshots("a", "a"))

    snapshot = runner.start("count me", str(tmp_path))

    assert snapshot.usage == {"input_tokens": 9, "cached_input_tokens": 3,
                              "output_tokens": 4, "reasoning_output_tokens": 2,
                              "cache_write_input_tokens": 7}
    assert snapshot.settled is True
