"""Resuming Live with the attached board, and changing what a session is about.

Only LiveSessionStore state, the tool, and the HTTP routes are exercised; no browser, board, or
real Collie state is touched.
"""
import json
import threading
import time
from pathlib import Path

import pytest

from harness import live_copilot as live


def _attach(store, tab_id=9):
    with store._transaction():
        value = store._read()
        value["board"] = {"url": "https://excalidraw.com/", "title": "Architecture",
                          "service": "excalidraw", "service_name": "Excalidraw",
                          "tab_id": tab_id}
        value["work"] = [{"mission_id": "m-1", "goal": "Preserve this"}]
        value["notes"] = [{"id": "note-1", "kind": "note", "text": "Queue sizing"}]
        store._write(value)


def test_resume_requires_fresh_recording_consent_and_retains_board(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    first = store.start(context="Interview prep", listen=False, consent=False,
                        observe_apps=False, board_edit=True)
    _attach(store)
    store.stop()
    stopped_bytes = Path(store.path).read_bytes()

    with pytest.raises(live.LiveCopilotError, match="consent"):
        store.resume(context="ComfyUI review", listen=True, consent=False)
    assert Path(store.path).read_bytes() == stopped_bytes

    resumed = store.resume(context="ComfyUI review", listen=True, consent=True,
                           board_edit=True, max_duration_minutes=180)
    assert resumed["active"] and resumed["session_id"] != first["session_id"]
    assert resumed["board"]["tab_id"] == 9 and resumed["work"][0]["mission_id"] == "m-1"
    assert resumed["notes"][0]["text"] == "Queue sizing"
    assert resumed["board_edit"] is True and resumed["listen"] is True
    assert resumed["context"] == "ComfyUI review"
    assert resumed["events"][-1]["kind"] == "session" and resumed["suggestions"] == []
    assert resumed["expires_at_ms"] - resumed["started_at_ms"] == 180 * 60_000
    with store._transaction():
        assert store._read()["audit"][-1]["action"] == "session_resumed"


def test_resume_keeps_the_previous_context_when_none_is_given(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Design review", listen=False, observe_apps=False)
    store.stop()
    assert store.resume()["context"] == "Design review"


def test_resume_refuses_while_a_session_is_active(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(listen=False, observe_apps=False)
    with pytest.raises(live.LiveCopilotError, match="already active"):
        store.resume()
    with pytest.raises(live.LiveCopilotError, match="boolean"):
        live.LiveSessionStore(tmp_path / "other").resume(listen="yes")


def test_context_change_is_a_hard_reset_but_keeps_the_work_surface(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Dota coaching", listen=False, consent=False,
                observe_apps=False, board_edit=True)
    store.add_event(source="you", kind="speech", text="What item next?")
    _attach(store, tab_id=7)
    with store._transaction():
        value = store._read()
        value["summary"] = "Old Dota summary"
        value["suggestions"] = [{"id": "old", "kind": "answer", "text": "Buy wards"}]
        value["handoff"] = {"id": "old-handoff", "pending": True}
        value["voice_playback"] = {"active": True, "cue_id": "old", "started_at_ms": 1,
                                   "ended_at_ms": 0}
        before_audio_epoch = value["audio"]["listen_epoch"]
        before_model_epoch = int(value["analysis"].get("permission_epoch") or 0)
        store._write(value)

    changed = store.update_context("ComfyUI backend system-design review")

    assert changed["context"] == "ComfyUI backend system-design review"
    assert changed["summary"] == "" and changed["suggestions"] == []
    assert changed["handoff"] is None and changed["board"]["tab_id"] == 7
    assert changed["voice_playback"]["active"] is False
    assert [row["kind"] for row in changed["events"]] == ["context_reset"]
    assert "Dota" not in changed["events"][0]["text"]
    assert changed["audio"]["listen_epoch"] == before_audio_epoch + 1
    assert changed["analysis"]["permission_epoch"] == before_model_epoch + 1
    with store._transaction():
        audit = store._read()["audit"][-1]
    assert audit["action"] == "context_changed" and "Dota coaching" in audit["detail"]


def test_context_change_needs_an_active_session_and_text(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    with pytest.raises(live.LiveCopilotError, match="start a live session"):
        store.update_context("anything")
    store.start(listen=False, observe_apps=False)
    with pytest.raises(live.LiveCopilotError, match="required"):
        store.update_context("   ")


def test_speech_decoded_under_the_old_context_does_not_land_after_the_change(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(context="Old topic", listen=True, consent=True,
                          understand=False, observe_apps=False)
    started, release = threading.Event(), threading.Event()

    def slow(path, **_kwargs):
        started.set()
        release.wait(5)
        return {"text": "This belongs to the old topic entirely."}
    store.ingest_audio(session_id=session["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"old", transcriber=slow)
    assert started.wait(5)
    store.update_context("New topic")
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and store.snapshot()["audio"]["pending"]:
        time.sleep(.01)
    assert [row["kind"] for row in store.snapshot()["events"]] == ["context_reset"]


def test_tool_can_resume_and_change_context(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    tool = live.LiveCopilotTool()
    assert {"resume", "context"} <= set(tool.schema["properties"]["action"]["enum"])
    store = live.LiveSessionStore(str(tmp_path))
    store.start(context="Before", listen=False, observe_apps=False)
    _attach(store)
    store.stop()
    resumed = json.loads(tool.run({"action": "resume", "context": "After"}, None))
    assert resumed["active"] and resumed["board"]["tab_id"] == 9
    assert resumed["started_from"] == "natural_language"
    changed = json.loads(tool.run({"action": "context", "text": "Later still"}, None))
    assert changed["context"] == "Later still"


def test_resume_and_context_http_routes(tmp_path, monkeypatch):
    import urllib.request
    from http.server import ThreadingHTTPServer
    from harness import webapp

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    store = live.LiveSessionStore(str(tmp_path))
    store.start(context="Before", listen=False, observe_apps=False)
    _attach(store)
    store.stop()
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, body):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s?token=%s" % (server.server_port, path, webapp.TOKEN),
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    try:
        status, resumed = post("/api/live-copilot/resume", {"context": "After", "listen": False})
        assert status == 201 and resumed["board"]["tab_id"] == 9
        assert resumed["started_from"] == "live_ui"
        status, changed = post("/api/live-copilot/context", {"context": "Later"})
        assert status == 200 and changed["context"] == "Later"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_live_page_resumes_when_the_ended_session_had_a_board():
    source = (Path(__file__).parents[1] / "harness" / "webui" / "live.html").read_text(
        encoding="utf-8")
    start = source.index('document.getElementById("start").onclick=async function()')
    flow = source[start:source.index('document.getElementById("stop").onclick', start)]
    assert 'STATE.session_id&&STATE.board?"/api/live-copilot/resume":"/api/live-copilot/start"' \
        in flow
    assert flow.index("var s=await api(endpoint") < flow.index("await beginCapture()")