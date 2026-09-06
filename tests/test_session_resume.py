"""Resume actual interrupted tool batches without replaying their effects."""
import json

import pytest

from harness import sessions


def batch():
    return [{"role": "user", "content": "inspect and update"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "read-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "write-2", "name": "write_file", "args": {
                    "path": "b.txt", "content": "never replay blindly"}},
            ]}]


@pytest.mark.parametrize("loader", [sessions.load, sessions.load_checked])
def test_builtin_read_resume_backfills_batch_without_changing_journal(
        tmp_path, monkeypatch, loader):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.checkpoint("read", batch(), state="executing_tool", detail={
        "tool_name": "read_file", "tool_call_id": "read-1", "replay_safe": True})
    before = (tmp_path / "read.json").read_bytes()
    assert sessions.recovery_state("read")["auto_resumable"] is True
    loaded = loader("read")
    if loader is sessions.load_checked:
        loaded = loaded["session"]
    results = loaded["messages"][-2:]
    assert [row["tool_call_id"] for row in results] == ["read-1", "write-2"]
    assert "read was interrupted" in results[0]["content"]
    assert "did not execute" in results[1]["content"]
    assert (tmp_path / "read.json").read_bytes() == before
    assert not (tmp_path / "b.txt").exists()
    # Persisting the resume view must not duplicate the completed transcript.
    sessions.checkpoint("read", loaded["messages"], state="calling_model")
    assert len(sessions.load("read")["messages"]) == 4


@pytest.mark.parametrize("detail,state", [
    ({"tool_name": "read_file"}, "executing_tool"),
    ({"tool_name": "read_file", "replay_safe": "true"}, "executing_tool"),
    ({"tool_name": "read_file", "replay_safe": 1}, "executing_tool"),
    ({"tool_name": "write_file", "replay_safe": True}, "executing_tool"),
    ({"tool_name": "mcp__remote__read", "replay_safe": True}, "executing_tool"),
    ({"tool_name": "read_file", "replay_safe": True, "internal": True}, "executing_tool"),
    ({"tool_name": "read_file", "replay_safe": True}, "external_action"),
])
def test_unknown_or_effectful_calls_still_require_reconciliation(
        tmp_path, monkeypatch, detail, state):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.checkpoint("uncertain", batch(), state=state, detail=detail)
    assert sessions.recovery_state("uncertain")["recovery_required"] is True
    assert len(sessions.load("uncertain")["messages"]) == 2


def test_crash_between_calls_only_backfills_unexecuted_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    messages = batch() + [{"role": "tool", "name": "read_file",
                           "tool_call_id": "read-1", "content": "actual result"}]
    sessions.checkpoint("between", messages, state="tool_complete")
    loaded = sessions.load("between")["messages"]
    assert loaded[-2] == messages[-1]
    assert loaded[-1]["tool_call_id"] == "write-2"
    assert "did not execute" in loaded[-1]["content"]


@pytest.mark.parametrize("resolution", ["completed", "not_fired", "cancel"])
def test_reconciliation_closes_entire_interrupted_batch(tmp_path, monkeypatch, resolution):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.checkpoint("reconcile", batch(), state="executing_tool", detail={
        "tool_name": "read_file", "tool_call_id": "read-1"})
    sessions.reconcile_recovery("reconcile", resolution, confirmed=True)
    loaded = sessions.load("reconcile")["messages"]
    assert [row.get("tool_call_id") for row in loaded[2:]] == ["read-1", "write-2"]
    if resolution == "cancel":
        assert "unknown" in loaded[2]["content"]
        assert "before execution" in loaded[3]["content"]


def test_loop_attests_builtin_read_before_interruption(tmp_path, monkeypatch):
    """Ctrl-C inside a built-in read ends the run without fencing the thread.

    The contract changed deliberately: an interrupt is a stop, so run() returns a
    canceled result instead of unwinding past every surface's bookkeeping. What
    it must NOT change is the attestation — the boundary stays the auto-resumable
    ``executing_tool`` a host-attested read earns, never a generic external fence.
    """
    from harness import cli
    from harness.providers import Completion, ToolCall
    from harness.tools import ReadFileTool
    from _util import _ScriptProvider

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    h = cli.make_harness(str(tmp_path), provider="mock", project="resume-read", embed="hash")
    h.durable_session_id = "killed-read"
    h.provider = _ScriptProvider([Completion(tool_calls=[
        ToolCall("r1", "read_file", {"path": "a.txt"})], stop_reason="tool_use")])
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(ReadFileTool, "run", interrupt)
    try:
        res = h.run("test", "inspect a.txt", consolidate=False)
        assert res.canceled and res.stop_reason == "canceled" and not res.success
        assert "interrupted by user" in res.error
        state = sessions.recovery_state(h.durable_session_id)
        assert state["state"] == "executing_tool"
        assert state["auto_resumable"] is True
        assert state["recovery_required"] is False
        # The killed read is closed honestly in the transcript rather than left
        # as an orphan tool_use, and it is not described as having produced data.
        saved = sessions.load(h.durable_session_id)["messages"]
        closures = [m for m in saved if m.get("tool_call_id") == "r1"]
        assert len(closures) == 1 and closures[0]["role"] == "tool"
        assert "stopped before returning a result" in closures[0]["content"]
        assert saved[-1] == {"role": "assistant", "content": "_[stopped by user]_"}
    finally:
        h.memory.close()
        h.recorder.close()


def test_default_run_continues_past_fifty_tool_steps(tmp_path, monkeypatch):
    from harness import cli
    from harness.providers import Completion, ToolCall
    from _util import _ScriptProvider

    monkeypatch.delenv("COLLIE_MAX_TURNS", raising=False)
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    (tmp_path / "fact.txt").write_text("the required evidence", encoding="utf-8")
    h = cli.make_harness(str(tmp_path), provider="mock", project="long-run", embed="hash")
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall(str(i), "read_file", {"path": "fact.txt"})],
                   stop_reason="tool_use") for i in range(65)
    ] + [Completion(text="Completed all 65 evidence steps.")])
    try:
        res = h.run("long", "inspect the evidence thoroughly", consolidate=False)
        assert res.turns == 66 and res.tool_calls == 65
        assert not res.error and not res.turns_exhausted
        assert res.answer == "Completed all 65 evidence steps."
    finally:
        h.memory.close()
        h.recorder.close()
