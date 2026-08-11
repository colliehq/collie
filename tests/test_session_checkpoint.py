from harness import sessions


def test_inflight_model_boundary_is_auto_resumable(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.checkpoint("s1", [{"role": "user", "content": "go"}],
                        run_id="r1", turn=2, state="calling_model")
    state = sessions.recovery_state("s1")
    assert state["auto_resumable"] is True
    assert state["recovery_required"] is False
    assert sessions.load("s1")["messages"][0]["content"] == "go"
    assert sessions.active_runs()[0]["session_id"] == "s1"


def test_interrupted_tool_fails_closed_until_reconciled(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.checkpoint("s2", [{"role": "user", "content": "publish"}],
                        run_id="r2", turn=3, state="executing_tool",
                        detail={"tool_name": "browser_click", "tool_call_id": "c9"})
    state = sessions.recovery_state("s2")
    assert state["recovery_required"] is True
    assert state["auto_resumable"] is False
    assert "not duplicated" in state["reason"]


def test_terminal_checkpoint_clears_active_run(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.checkpoint("s3", [], run_id="r3", state="tool_complete")
    assert sessions.recovery_state("s3") is not None
    sessions.checkpoint("s3", [{"role": "assistant", "content": "done"}],
                        run_id="r3", state="terminal", terminal=True)
    assert sessions.recovery_state("s3") is None


def test_uncertain_tool_requires_explicit_reconciliation(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "name": "publish", "args": {"id": 4}}]}]
    sessions.checkpoint("s4", messages, run_id="r4", state="executing_tool",
                        detail={"tool_name": "publish", "tool_call_id": "c1"})
    try:
        sessions.reconcile_recovery("s4", "completed")
        assert False, "confirmation must be mandatory"
    except ValueError as exc:
        assert "confirmed=True" in str(exc)
    state = sessions.reconcile_recovery(
        "s4", "completed", note="receipt 42 exists", confirmed=True)
    assert state["auto_resumable"] is True
    loaded = sessions.load("s4")
    assert loaded["messages"][-1]["tool_call_id"] == "c1"
    assert "receipt 42" in loaded["messages"][-1]["content"]


def test_reconciliation_does_not_duplicate_an_already_paired_parent_call(
        monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c0", "name": "execute_code", "args": {"code": "..."}}]},
        {"role": "tool", "tool_call_id": "c0", "name": "execute_code",
         "content": "ERROR: inner external effect may still be running"},
    ]
    sessions.checkpoint(
        "paired", messages, run_id="run-paired", state="external_action",
        detail={"tool_name": "execute_code", "tool_call_id": "c0"})

    sessions.reconcile_recovery(
        "paired", "completed", note="external receipt inspected", confirmed=True)
    loaded = sessions.load("paired")
    paired = [m for m in loaded["messages"]
              if m.get("role") == "tool" and m.get("tool_call_id") == "c0"]
    assert len(paired) == 1
    assert loaded["messages"][-1]["role"] == "user"
    assert "external receipt inspected" in loaded["messages"][-1]["content"]
