import time
import json

import pytest


def test_live_session_accepts_no_task_and_observes_without_recording_consent(tmp_path):
    from harness.live_copilot import LiveSessionStore

    store = LiveSessionStore(tmp_path)
    value = store.start(context="", listen=False, understand=True,
                        observe_apps=True, consent=False)
    assert value["active"] is True
    assert value["context"] == ""
    assert value["observe_apps"] is True
    assert value["consent_version"] == "not-required"


def test_audio_listening_requires_consent_and_clears_authority_on_stop(tmp_path):
    from harness.live_copilot import LiveCopilotError, LiveSessionStore

    store = LiveSessionStore(tmp_path)
    with pytest.raises(LiveCopilotError, match="consent"):
        store.start(listen=True, consent=False)
    started = store.start(listen=True, consent=True)
    assert started["listen"] and started["consent_at_ms"]
    stopped = store.stop()
    assert not stopped["active"] and not stopped["listen"] and not stopped["understand"]

    store.start(listen=False, consent=False)
    with pytest.raises(LiveCopilotError, match="consent"):
        store.update_permissions(listen=True)
    enabled = store.update_permissions(listen=True, consent=True)
    assert enabled["listen"] and enabled["consent_at_ms"]


def test_native_audio_chunks_become_bounded_speaker_events_and_are_deleted(tmp_path):
    from harness.live_copilot import LiveSessionStore

    store = LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, observe_apps=False)

    def transcribe(path, **_kwargs):
        assert open(path, "rb").read() == b"audio"
        return {"text": "", "segments": [{"speaker": "Speaker A", "text": "Please compare them."}]}

    queued = store.ingest_audio(session_id=started["session_id"], source="system", seq=0,
                                mime_type="audio/webm", data=b"audio", transcriber=transcribe)
    assert queued["queued"]
    deadline = time.time() + 3
    while time.time() < deadline and not store.snapshot()["events"]:
        time.sleep(.02)
    value = store.snapshot()
    assert value["events"][-1]["source"] == "other"
    assert value["events"][-1]["speaker"] == "Speaker A"
    assert value["audio"]["pending"] == 0
    assert not list((tmp_path / "live-audio").rglob("*.webm"))


def test_runtime_continuously_understands_events_but_never_starts_work(tmp_path):
    from harness.live_copilot import LiveCopilotRuntime, LiveSessionStore

    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, observe_apps=False)
    store.add_event(source="you", text="We need to choose storage.")
    calls = []

    def analyze(payload):
        calls.append(payload)
        return {"summary": "The team is choosing storage.", "suggestions": [
            {"kind": "question", "urgency": "now", "text": "Ask about consistency."},
            {"kind": "made-up", "urgency": "now", "text": "discard"},
        ]}

    runtime = LiveCopilotRuntime(tmp_path, analyzer=analyze, debounce_ms=0, min_interval_ms=0)
    runtime.activity_source = None
    assert runtime.tick() is True
    value = store.snapshot()
    assert value["summary"] == "The team is choosing storage."
    assert [row["text"] for row in value["suggestions"]] == ["Ask about consistency."]
    assert value["work"] == []
    assert calls[0]["events"][-1]["source"] == "you"
    assert runtime.tick() is False


def test_late_understanding_result_is_dropped_after_session_stops(tmp_path):
    from harness.live_copilot import LiveCopilotRuntime, LiveSessionStore

    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, observe_apps=False)
    store.add_event(source="you", text="This session is about to end.")

    def analyze(_payload):
        store.stop()
        return {"summary": "late private summary", "suggestions": [
            {"kind": "action", "urgency": "now", "text": "late action"},
        ]}

    runtime = LiveCopilotRuntime(tmp_path, analyzer=analyze, debounce_ms=0, min_interval_ms=0)
    runtime.activity_source = None
    assert runtime.tick() is False
    value = store.snapshot()
    assert value["active"] is False
    assert value["summary"] == "" and value["suggestions"] == []


def test_environment_observation_is_app_name_only_and_handoff_freezes_it(tmp_path):
    from harness.live_copilot import LiveCopilotRuntime, LiveSessionStore

    class Source:
        def foreground_app(self):
            return "Figma.exe"

    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False, observe_apps=True)
    runtime = LiveCopilotRuntime(tmp_path, analyzer=lambda _payload: {})
    runtime.activity_source = Source()
    assert runtime.tick() is False
    value = store.snapshot()
    assert value["events"][-1]["text"] == "Foreground app changed to figma."
    assert "title" not in value["events"][-1] and "keys" not in value["events"][-1]
    handoff = store.request_handoff()
    assert handoff["pending"] and handoff["app"] == "figma"


def test_capsule_handoff_freezes_exact_window_and_bounded_semantics(monkeypatch, tmp_path):
    from harness import native
    from harness.live_copilot import LiveSessionStore

    monkeypatch.setattr(native, "tree", lambda **kwargs: {
        "ok": True, "elements": [
            {"type": "Edit", "name": "Architecture notes", "value": "private draft",
             "keys": "secret", "focused": True},
            {"type": "Button", "name": "Add service"},
        ]})
    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False, observe_apps=True,
                observe_ui=True)
    handoff = store.request_handoff(app="Chrome", title="System design board",
                                    pid=42, hwnd=9001)
    assert handoff["app"] == "chrome" and handoff["title"] == "System design board"
    assert handoff["pid"] == 42 and handoff["hwnd"] == 9001
    assert handoff["context_event_id"]
    text = store.snapshot()["events"][-1]["text"]
    assert "focused Edit: Architecture notes" in text and "Button: Add service" in text
    assert "private draft" not in text and "secret" not in text


def test_natural_language_tool_starts_useful_live_defaults_and_can_stop(monkeypatch,
                                                                       tmp_path):
    from harness import live_copilot

    real = live_copilot.LiveSessionStore
    monkeypatch.setattr(live_copilot, "LiveSessionStore", lambda: real(tmp_path))
    tool = live_copilot.LiveCopilotTool()
    started = json.loads(tool.run({"action": "start", "context": "System design interview"},
                                  None))
    assert started["active"] and started["started_from"] == "natural_language"
    assert started["observe_apps"] and started["observe_ui"] and started["understand"]
    assert not started["listen"] and "Ctrl+Alt+Space" in started["next"]
    stopped = json.loads(tool.run({"action": "stop"}, None))
    assert not stopped["active"]


def test_opt_in_semantic_ui_observation_drops_values_and_keys(monkeypatch, tmp_path):
    from harness import native
    from harness.live_copilot import LiveCopilotRuntime, LiveSessionStore

    class Source:
        def foreground_app(self):
            return "Figma.exe"

    monkeypatch.setattr(native, "foreground_pid", lambda: 42)
    monkeypatch.setattr(native, "tree", lambda **_kwargs: {
        "ok": True, "elements": [
            {"type": "Edit", "name": "Search layers", "value": "private query",
             "focused": True, "keys": "secret"},
            {"type": "Button", "name": "Share"},
        ]})
    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False, observe_apps=False,
                observe_ui=True)
    runtime = LiveCopilotRuntime(tmp_path, analyzer=lambda _payload: {})
    runtime.activity_source = Source()
    runtime.last_ui_poll_ms = 0
    runtime.tick()
    text = store.snapshot()["events"][-1]["text"]
    assert "focused Edit: Search layers" in text and "Button: Share" in text
    assert "private query" not in text and "secret" not in text


def test_explicit_handoff_can_create_durable_mission(monkeypatch, tmp_path):
    from harness import missionweb
    from harness.live_copilot import LiveSessionStore

    seen = {}

    class Service:
        def start(self, goal, **kwargs):
            seen.update(goal=goal, kwargs=kwargs)
            return {"mission_id": "msn_live", "state": "queued"}

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(missionweb, "MissionService", Service)
    store = LiveSessionStore(tmp_path)
    store.start(context="Customer launch", listen=False, consent=False, observe_apps=False)
    row = store.start_work(text="Check the rollout risks")
    assert row["mission_id"] == "msn_live" and row["state"] == "queued"
    assert seen["kwargs"]["case"]["source"] == "live_copilot"
    assert seen["kwargs"]["case"]["live_context"] == "Customer launch"
    assert seen["closed"] is True


def test_stopped_session_refuses_new_context(tmp_path):
    from harness.live_copilot import LiveCopilotError, LiveSessionStore

    store = LiveSessionStore(tmp_path)
    started = store.start(listen=False, consent=False)
    store.stop()
    with pytest.raises(LiveCopilotError, match="no live session"):
        store.add_event(source="typed", text="late")
    with pytest.raises(LiveCopilotError, match="authority"):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                           mime_type="audio/webm", data=b"late")


def test_default_registry_exposes_live_copilot_not_a_meeting_adapter(monkeypatch):
    from harness.tools import default_registry

    monkeypatch.setenv("COLLIE_BROWSER_BRIDGE", "0")
    names = default_registry().names()
    assert "live_copilot" in names
    assert "interview_assist" not in names
