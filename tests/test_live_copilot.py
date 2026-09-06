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
    assert value["observe_ui"] is True and value["observe_input"] is True
    assert value["observe_screen"] is False and value["voice_dialogue"] is False
    assert value["consent_version"] == "not-required"
    assert value["events"][-1]["kind"] == "session"


def test_live_ui_never_uses_screen_sharing_for_audio_capture():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "harness" / "webui" / "live.html").read_text(
        encoding="utf-8")
    assert 'async function captureSources()' in source
    assert 'getDisplayMedia' not in source
    assert 'id="systemAudio"' not in source
    start = source.index('document.getElementById("start").onclick=async function()')
    start_flow = source[start:source.index('document.getElementById("stop").onclick', start)]
    assert start_flow.index('var s=await api("/api/live-copilot/start"') < \
        start_flow.index('await beginCapture()')


def test_live_defaults_to_local_sensevoice_not_a_cloud_transcriber(tmp_path, monkeypatch):
    from harness.live_copilot import LiveSessionStore, capabilities

    monkeypatch.setattr("harness.sensevoice.availability", lambda: {
        "available": True, "engine": "SenseVoice · local", "model_dir": "C:/model"})
    seen = []

    def local_transcribe(path, **kwargs):
        seen.append((path, kwargs))
        return {"text": "Collie 能看到当前浏览器。", "segments": []}

    monkeypatch.setattr("harness.sensevoice.transcribe", local_transcribe)
    assert capabilities()["speech_ready"] is True
    assert capabilities()["speech_engine"] == "SenseVoice · local"
    store = LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, observe_apps=False)
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"local-audio")
    deadline = time.time() + 3
    while time.time() < deadline and not any(
            event.get("text") == "Collie 能看到当前浏览器。"
            for event in store.snapshot()["events"]):
        time.sleep(.02)
    assert seen and seen[0][1]["language"] == ""
    assert any(event.get("text") == "Collie 能看到当前浏览器。"
               for event in store.snapshot()["events"])


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


def test_direct_voice_and_visual_observation_are_session_scoped(tmp_path):
    from harness.live_copilot import LiveSessionStore

    store = LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, observe_screen=True,
                          voice_dialogue=True)
    assert started["observe_screen"] is True
    assert started["voice_dialogue"] is True
    stopped = store.stop()
    assert stopped["observe_screen"] is False
    assert stopped["voice_dialogue"] is False


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
    while time.time() < deadline and not any(
            row.get("source") == "other" for row in store.snapshot()["events"]):
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


def test_voice_dialogue_lane_answers_without_waiting_for_visual_analyzer(tmp_path):
    from harness.live_copilot import LiveSessionStore, run_voice_dialogue_once

    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, observe_apps=False, voice_dialogue=True)
    spoken = store.add_event(source="you", kind="speech", text="我现在出什么？")
    seen = []

    def answer(payload):
        seen.append(payload)
        return "先补微光披风，留钱买真眼。"

    assert run_voice_dialogue_once(tmp_path, analyzer=answer) is True
    value = store.snapshot()
    cue = value["suggestions"][-1]
    assert cue["lane"] == "dialogue" and cue["kind"] == "answer"
    assert cue["source_event_id"] == spoken["id"]
    assert seen[0]["newest_speech"] == "我现在出什么？"
    assert run_voice_dialogue_once(tmp_path, analyzer=answer) is False


def test_voice_dialogue_prompt_adapts_to_interview_and_dota_contexts():
    from harness.live_copilot import _dialogue_system_prompt

    interview = _dialogue_system_prompt({
        "optional_context": "System-design interview about a ticketing service.",
        "newest_speech": "How do you prevent oversell?",
    })
    assert "general work session" in interview
    assert "Dota 2 support" not in interview

    dota = _dialogue_system_prompt({
        "recent_events": [{"app": "dota2", "title": "Dota 2", "text": "lane"}],
        "newest_speech": "现在出什么？",
    })
    assert "playing Dota 2 as a support" in dota
    assert "fog-of-war" in dota


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


def test_environment_observation_logs_window_metadata_without_input_content(tmp_path):
    from harness.live_copilot import LiveCopilotRuntime, LiveSessionStore

    class Source:
        def foreground_app(self):
            return "Figma.exe"

    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False, observe_apps=True,
                observe_ui=False, observe_input=False)
    runtime = LiveCopilotRuntime(tmp_path, analyzer=lambda _payload: {})
    runtime.activity_source = Source()
    runtime._foreground_window = lambda: {
        "app": "figma", "title": "Architecture board", "pid": 42, "hwnd": 9001}
    assert runtime.tick() is False
    value = store.snapshot()
    row = next(row for row in value["events"] if row["kind"] == "window")
    assert row["text"] == "Opened figma · Architecture board."
    assert row["title"] == "Architecture board"
    assert "keys" not in row
    handoff = store.request_handoff()
    assert handoff["pending"] and handoff["app"] == "figma"


def test_browser_observation_is_bounded_to_host_and_opt_in_title(monkeypatch, tmp_path):
    from harness import browserbridge
    from harness.live_copilot import LiveCopilotRuntime, LiveSessionStore

    monkeypatch.setattr(browserbridge, "live_tab_context", lambda **_kwargs: {
        "app": "chrome", "host": "maker.tavus.io",
        "title": "Collie System Design Interviewer — Tavus",
        "url": "https://maker.tavus.io/pals/pf14a58a0511?secret=never-recorded",
        "page_text": "also never recorded",
    })
    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False, observe_apps=True,
                observe_ui=True, observe_input=False)
    runtime = LiveCopilotRuntime(tmp_path, analyzer=lambda _payload: {})
    runtime.activity_source = None
    assert runtime.tick() is False
    row = store.snapshot()["events"][-1]
    assert row["kind"] == "browser_tab" and row["app"] == "chrome"
    assert row["title"] == "Collie System Design Interviewer — Tavus"
    assert row["text"] == "Browser tab: maker.tavus.io · Collie System Design Interviewer — Tavus."
    assert "secret" not in json.dumps(row) and "page_text" not in row
    before = len(store.snapshot()["events"])
    runtime.tick()
    assert len(store.snapshot()["events"]) == before

    private_store = LiveSessionStore(tmp_path / "host-only")
    private_store.start(listen=False, consent=False, understand=False, observe_apps=True,
                        observe_ui=False, observe_input=False)
    private_runtime = LiveCopilotRuntime(tmp_path / "host-only", analyzer=lambda _payload: {})
    private_runtime.activity_source = None
    private_runtime.tick()
    private_row = private_store.snapshot()["events"][-1]
    assert private_row["kind"] == "browser_tab" and private_row["title"] == ""
    assert private_row["text"] == "Browser tab: maker.tavus.io."


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
    assert (started["observe_apps"] and started["observe_ui"] and
            started["observe_input"] and started["understand"])
    assert not started["listen"] and "Ctrl+Alt+Space" in started["next"]
    stopped = json.loads(tool.run({"action": "stop"}, None))
    assert not stopped["active"]


def test_start_provenance_is_persisted_and_explicit_end_phrase_stops_capture(tmp_path):
    from harness.live_copilot import LiveSessionStore

    store = LiveSessionStore(tmp_path)
    started = store.start(listen=False, consent=False, started_from="natural_language",
                          max_duration_minutes=45)
    assert started["started_from"] == "natural_language"
    assert started["expires_at_ms"] > started["started_at_ms"]

    store.add_event(source="you", kind="speech", text="行了，结束吧。")
    stopped = store.snapshot()
    assert stopped["active"] is False
    assert stopped["stop_reason"] == "explicit_stop_phrase"
    assert stopped["stopped_from"] == "live_event"
    assert stopped["events"][-1]["text"] == "行了，结束吧。"


def test_repeated_interface_snapshots_are_coalesced(tmp_path):
    from harness.live_copilot import LiveSessionStore

    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False)
    first = store.add_event(source="system", kind="interface", app="chrome",
                            title="Board", text="Interface in chrome: Canvas")
    second = store.add_event(source="system", kind="interface", app="chrome",
                             title="Board", text="Interface in chrome: Canvas")
    value = store.snapshot()
    assert len(value["events"]) == 2
    assert second["id"] == first["id"] and second["repeat_count"] == 2


def test_avatar_rehearsal_simulates_without_tavus_credentials(monkeypatch, tmp_path):
    from harness import avatar_rehearsal
    from harness.avatar_rehearsal import AvatarRehearsalService
    from harness.live_copilot import LiveSessionStore

    monkeypatch.setattr(avatar_rehearsal.mcpclient, "server_has_tool",
                        lambda _server, _tool: False)
    store = LiveSessionStore(tmp_path)
    store.start(context="Explain a restaurant discovery design", listen=False, consent=False)
    service = AvatarRehearsalService(tmp_path)
    started = service.start()
    assert started["simulation"] is True
    assert started["avatar"]["active"] is True
    assert "AI 排练化身" in started["script"]
    stopped = service.stop()
    assert stopped["avatar"]["active"] is False


def test_avatar_rehearsal_uses_narrow_mcp_service(monkeypatch, tmp_path):
    from harness import avatar_rehearsal
    from harness.avatar_rehearsal import AvatarRehearsalService
    from harness.live_copilot import LiveSessionStore

    monkeypatch.setattr(avatar_rehearsal.mcpclient, "server_has_tool",
                        lambda _server, tool: tool in {"avatar_start", "avatar_stop"})
    calls = []

    def call(server, tool, arguments, timeout=None):
        calls.append((server, tool, arguments, timeout))
        if tool == "avatar_start":
            return {"structuredContent": {
                "provider": "tavus", "conversation_id": "conversation_123456",
                "join_url": "https://tavus.daily.co/rehearsal?t=short-lived-token",
                "disclosed_ai": True}}
        return {"structuredContent": {"ended": True}}

    LiveSessionStore(tmp_path).start(listen=False, consent=False)
    service = AvatarRehearsalService(
        tmp_path, mcp_call=call, allowed_join_hosts=["daily.co"])
    started = service.start(scenario="Explain the cache path")
    assert started["simulation"] is False
    assert "short-lived-token" in started["join_url"]
    assert calls[0][0:2] == ("avatar", "avatar_start")
    assert "AI 排练化身" in calls[0][2]["script"]
    assert calls[0][2]["max_live_seconds"] == 600
    service.stop()
    assert calls[-1][0:2] == ("avatar", "avatar_stop")
    assert calls[-1][2]["conversation_id"] == "conversation_123456"


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
    runtime._foreground_window = lambda: {
        "app": "figma", "title": "Design", "pid": 42, "hwnd": 9001}
    runtime.last_ui_poll_ms = 0
    runtime.tick()
    text = store.snapshot()["events"][-1]["text"]
    assert "focused Edit: Search layers" in text and "Button: Share" in text
    assert "private query" not in text and "secret" not in text


def test_input_observation_is_throttled_metadata_not_a_keylogger(tmp_path):
    from harness.live_copilot import LiveCopilotRuntime, LiveSessionStore

    class Source:
        def foreground_app(self):
            return "Figma.exe"

        def idle_seconds(self):
            return 0.05

    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False, observe_apps=False,
                observe_ui=False, observe_input=True)
    runtime = LiveCopilotRuntime(tmp_path, analyzer=lambda _payload: {})
    runtime.activity_source = Source()
    runtime._foreground_window = lambda: {
        "app": "figma", "title": "Private draft", "pid": 42, "hwnd": 9001}
    runtime.last_input_at_ms = 1
    runtime.tick()
    row = store.snapshot()["events"][-1]
    assert row["kind"] == "interaction" and "content not recorded" in row["text"]
    assert "key" not in row and "value" not in row
    before = len(store.snapshot()["events"])
    runtime.tick()
    assert len(store.snapshot()["events"]) == before


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


def test_delayed_native_transcript_cannot_cross_live_sessions(tmp_path):
    from harness.live_copilot import LiveCopilotError, LiveSessionStore

    store = LiveSessionStore(tmp_path)
    old = store.start(listen=False, consent=False)
    store.stop()
    current = store.start(listen=False, consent=False)
    with pytest.raises(LiveCopilotError, match="different session"):
        store.add_event(source="you", kind="speech", text="stale phrase",
                        session_id=old["session_id"])
    assert current["session_id"] != old["session_id"]
    assert all(row.get("text") != "stale phrase" for row in store.snapshot()["events"])


def test_default_registry_exposes_live_copilot_not_a_meeting_adapter(monkeypatch):
    from harness.tools import default_registry

    monkeypatch.setenv("COLLIE_BROWSER_BRIDGE", "0")
    names = default_registry().names()
    assert "live_copilot" in names
    assert "interview_assist" not in names
