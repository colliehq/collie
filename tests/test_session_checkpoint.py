from harness import sessions

import pytest


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


def test_transcript_save_can_preserve_an_external_replay_fence(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.checkpoint(
        "external", [], run_id="worker-1", state="external_action",
        detail={"runner": "codex-exec"})

    sessions.save(
        "external", [{"role": "assistant", "content": "useful partial result"}],
        answer="useful partial result", preserve_active=True)

    assert sessions.load("external")["last_answer"] == "useful partial result"
    state = sessions.recovery_state("external")
    assert state["recovery_required"] is True
    assert state["detail"]["runner"] == "codex-exec"


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


@pytest.mark.parametrize("writer", ["save", "checkpoint"])
def test_all_session_writers_preserve_an_unreadable_journal(
        monkeypatch, tmp_path, writer):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    path = tmp_path / "torn.json"
    original = b'{"id":"torn","messages":['
    path.write_bytes(original)

    with pytest.raises(ValueError, match="unreadable"):
        getattr(sessions, writer)(
            "torn", [{"role": "user", "content": "new"}])

    assert path.read_bytes() == original


def test_semantically_corrupt_journal_is_visible_as_recovery_required(
        monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    path = tmp_path / "semantic.json"
    path.write_text(
        '{"id":"semantic","messages":[],"active_run":"torn"}',
        encoding="utf-8")

    state = sessions.recovery_state("semantic")

    assert state["state"] == "invalid"
    assert state["recovery_required"] is True
    assert state["auto_resumable"] is False
    assert sessions.active_runs()[0]["session_id"] == "semantic"
    assert sessions.load("semantic") is None


def test_non_finite_journal_is_invalid_and_is_never_rewritten(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    path = tmp_path / "nonfinite.json"
    original = b'{"id":"nonfinite","messages":[],"active_run":' \
               b'{"state":"external_action","updated":NaN}}'
    path.write_bytes(original)

    state = sessions.recovery_state("nonfinite")

    assert state["state"] == "invalid"
    assert state["recovery_required"] is True
    assert sessions.load_checked("nonfinite")["status"] == "invalid"
    assert sessions.append_run_receipt("nonfinite", {"ok": True}) is False
    assert path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("title", []), ("cwd", {}), ("last_answer", 17), ("updated", "yesterday"),
])
def test_malformed_sidebar_fields_become_visible_recovery_items(
        monkeypatch, tmp_path, field, value):
    import json

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    path = tmp_path / "bad-sidebar.json"
    path.write_text(json.dumps({
        "id": "bad-sidebar", "messages": [], field: value,
    }), encoding="utf-8")

    state = sessions.recovery_state("bad-sidebar")

    assert state["state"] == "invalid"
    assert state["recovery_required"] is True
    assert sessions.active_runs()[0]["session_id"] == "bad-sidebar"
    # A corrupt row cannot take down the whole chat picker.
    assert sessions.recent(10)[0]["id"] == "bad-sidebar"


def test_session_writer_rejects_non_finite_values_without_partial_file(
        monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="Out of range float values"):
        sessions.append_exchange("strict-json", "question", {"value": float("nan")})

    assert not (tmp_path / "strict-json.json").exists()
