"""Native subscription exhaustion preserves finished work and its reset time."""
import json
import time

import pytest

from harness.agent_runners import ProcessOutcome, RunnerSnapshot
from harness.runner_specs import HarnessSpec, RunnerProbe, snapshot_to_run_result
from test_claude_code_runner import SESSION, OTHER_SESSION, FakeProcessRunner, _result, _runner


def _stream(reset, prefix=(), **terminal):
    return [*prefix, {"type": "rate_limit_event", "session_id": SESSION,
                     "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour",
                                         "resetsAt": reset}},
            {"type": "assistant", "session_id": SESSION, "error": "rate_limit",
             "message": {"content": [{"type": "text", "text": "quota exhausted"}]}},
            _result(is_error=True, subtype="error_during_execution", **terminal)]


def _outcome(events, **overrides):
    return ProcessOutcome(stdout="\n".join(json.dumps(x) for x in events),
                          exit_code=overrides.pop("exit_code", 1), **overrides)


def _edit():
    return [{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "write-1", "name": "Edit", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "write-1", "content": "updated"}]}}]


def test_completed_file_edit_can_resume_after_quota_reset(tmp_path):
    reset = int(time.time()) + 18000
    process = FakeProcessRunner(_outcome(_stream(reset, _edit())),
                               _outcome([_result()], exit_code=0))
    runner = _runner(process, digests=("old", "new", "new", "new"))
    snap = runner.start("fix", str(tmp_path))
    assert snap.mutated and snap.retry_at == reset and not snap.settled
    assert not snap.recovery_required
    assert "HTTP 429" in snap.error
    from harness.providers import classify_error
    assert classify_error(snap.error) == "retryable"
    restored = RunnerSnapshot.from_dict(snap.to_dict())
    assert restored == snap
    result = snapshot_to_run_result(restored, HarnessSpec("claude-code", "Claude Code", "external"),
                                    RunnerProbe(key="claude-code", installed=True))
    assert result.retry_at == reset and not result.verified
    resumed = runner.resume(restored, "continue")
    assert resumed.settled and resumed.retry_at == 0
    argv = process.calls[1]["argv"]
    assert argv[argv.index("--resume") + 1] == SESSION


@pytest.mark.parametrize("scenario", ["unfinished", "failed", "unknown", "duplicate", "unmatched"])
@pytest.mark.parametrize("mutated", [True, False])
def test_quota_cannot_clear_uncertain_tool_effects(tmp_path, scenario, mutated):
    prefix = _edit()
    if scenario == "unfinished":
        prefix.pop()
    elif scenario == "failed":
        prefix[1]["message"]["content"][0]["is_error"] = True
    elif scenario == "unknown":
        prefix[0]["message"]["content"][0]["name"] = "Bash"
    elif scenario == "duplicate":
        prefix.insert(1, prefix[0])
    else:
        prefix[1]["message"]["content"][0]["tool_use_id"] = "other"
    process = FakeProcessRunner(_outcome(_stream(int(time.time()) + 18000, prefix)))
    snap = _runner(process, digests=("old", "new" if mutated else "old")).start("fix", str(tmp_path))
    assert snap.recovery_required and not snap.settled


@pytest.mark.parametrize("scenario", ["cancel", "timeout", "truncated", "mismatch", "missing_id",
                                     "signal_exit", "prose_only", "auth", "stale", "bool",
                                     "string", "allowed", "unknown_window", "tail"])
def test_invalid_or_unrelated_quota_data_cannot_schedule_a_resume(tmp_path, scenario):
    events = _stream(int(time.time()) + 18000, _edit())
    overrides = {}
    if scenario in ("cancel", "timeout", "truncated"):
        overrides[{"cancel": "cancelled", "timeout": "timed_out",
                   "truncated": "output_truncated"}[scenario]] = True
    elif scenario == "mismatch":
        events[-1]["session_id"] = OTHER_SESSION
    elif scenario == "missing_id":
        events[-1].pop("session_id")
    elif scenario == "signal_exit":
        overrides["exit_code"] = -15
    elif scenario == "prose_only":
        events.pop(-3)
    elif scenario == "auth":
        events[-2]["error"] = "authentication_failed"
    elif scenario in ("stale", "bool", "string"):
        events[-3]["rate_limit_info"]["resetsAt"] = {
            "stale": int(time.time()) - 1, "bool": True,
            "string": str(int(time.time()) + 18000)}[scenario]
    elif scenario == "allowed":
        events[-3]["rate_limit_info"]["status"] = "allowed_warning"
    elif scenario == "unknown_window":
        events[-3]["rate_limit_info"]["rateLimitType"] = "overage"
    else:
        events.append(events[-3])
    snap = _runner(FakeProcessRunner(_outcome(events, **overrides)),
                   digests=("old", "new")).start("fix", str(tmp_path))
    assert snap.retry_at == 0 and snap.recovery_required


def test_persisted_quota_receipts_do_not_reschedule_an_unrelated_later_error(tmp_path):
    reset = int(time.time()) + 18000
    process = FakeProcessRunner(_outcome(_stream(reset)),
                               _outcome([_result(is_error=True, result="authentication failed")]))
    runner = _runner(process, digests=("old", "old", "old", "old"))
    first = runner.start("fix", str(tmp_path))
    assert first.retry_at == reset
    second = runner.resume(first, "continue")
    assert second.retry_at == 0
    assert any(event.type == "rate_limit_event" for event in second.events)
