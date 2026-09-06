"""Review/export stays local, selective, and bound to the displayed session."""
from pathlib import Path

import pytest

from harness import live_copilot as live


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "capabilities", lambda: {})
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Customer launch", listen=False)
    value = store._read()
    value.update({"summary": "Prepare the launch checklist.",
                  "notes": [{"text": "Confirm the support rota."}],
                  "suggestions": [{"text": "Check the rollout.", "dismissed": False},
                                  {"text": "Dismissed private cue", "dismissed": True}],
                  "work": [{"goal": "Draft release notes", "mission_id": "msn_123",
                            "state": "queued"}],
                  "avatar": {"conversation_url": "https://example.invalid/?token=PRIVATE"},
                  "audio": {"last_error": "PRIVATE_AUDIO_METADATA", "pending": 0}})
    store._write(value)
    return store


def test_review_exports_summary_notes_and_recorded_task_state_without_raw_log(store, monkeypatch):
    session = store.snapshot()["session_id"]
    store.add_event(source="you", kind="speech", text="Private raw transcript")
    store.stop()
    before = Path(store.path).read_bytes()
    monkeypatch.setattr(live, "capabilities", lambda: pytest.fail("export must not probe providers"))
    exported = store.export_markdown(session_id=session)
    assert exported["filename"] == session + ".md"
    content = exported["content"]
    assert "Prepare the launch checklist." in content
    assert "Confirm the support rota." in content and "Check the rollout." in content
    assert "Last recorded status: queued" in content and "Draft release notes" in content
    assert "Status: Ended" in content and "UTC" in content
    assert "Dismissed private cue" not in content and "Private raw transcript" not in content
    assert "PRIVATE" not in content and "example.invalid" not in content
    assert Path(store.path).read_bytes() == before


def test_full_log_export_uses_all_retained_events_not_the_ui_slice(store):
    value = store._read()
    value["events"] = [{"text": "Signal %d" % i, "source": "you", "at_ms": 1_000 + i}
                       for i in range(300)]
    store._write(value)
    assert len(store.snapshot()["events"]) == 240
    content = store.export_markdown(session_id=value["session_id"], include_events=True)["content"]
    assert "Signal 0\n" in content and "Signal 299\n" in content
    assert "may have been trimmed" in content


def test_old_page_cannot_export_a_replacement_session(store):
    old = store.snapshot()["session_id"]
    store.start(listen=False, context="New private session")
    with pytest.raises(live.LiveCopilotError, match="no longer available"):
        store.export_markdown(session_id=old, include_events=True)


def test_empty_store_has_no_review(tmp_path):
    with pytest.raises(live.LiveCopilotError, match="no longer available"):
        live.LiveSessionStore(tmp_path).export_markdown(session_id="")


def test_review_supports_chinese_and_escapes_executable_markup(store):
    value = store._read()
    value["summary"] = '<script>alert(1)</script> [open](javascript:alert(1))'
    store._write(value)
    content = store.export_markdown(session_id=value["session_id"], language="zh-CN")["content"]
    assert "会话回顾" in content and "摘要（AI 生成）" in content
    assert "<script>" not in content and "[open](javascript:" not in content
    assert "&lt;script&gt;" in content


def test_raw_context_requires_a_boolean(store):
    with pytest.raises(live.LiveCopilotError, match="boolean"):
        store.export_markdown(session_id=store.snapshot()["session_id"], include_events="false")
