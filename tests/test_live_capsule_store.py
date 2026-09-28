"""Server side of the push-to-talk capsule: per-clip receipts, previews, intent, dictation.

Transcribers, the model and the desktop are injected; no audio device, model or window is used.
"""
import time

import pytest

from harness import live_copilot as live


def _wait(predicate, deadline=3.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    return predicate()


def _receipts(store):
    return store.snapshot()["audio"].get("capsule_results") or []


def test_capsule_final_has_its_own_event_kind(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, consent=False, observe_apps=False)
    store.ingest_audio(
        session_id=session["session_id"], source="capsule", seq=0,
        mime_type="audio/webm", data=b"final-audio",
        transcriber=lambda *_a, **_k: {"text": "clear the board", "segments": []})
    rows = _wait(lambda: [row for row in store.snapshot()["events"]
                          if row.get("kind") == "capsule_speech"])
    assert rows[-1]["source"] == "you" and rows[-1]["text"] == "clear the board"
    assert not [row for row in store.snapshot()["events"] if row.get("kind") == "speech"]
    assert store.snapshot()["audio"]["pending"] == 0


def test_capsule_receipt_joins_segments_and_records_silence_for_its_own_seq(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, consent=False, observe_apps=False)
    results = [{"segments": [{"text": "Design a GPU queue."}, {"text": "保持租户公平。"}]},
               {"text": "The."}]
    for seq, result in enumerate(results):
        store.ingest_audio(session_id=session["session_id"], source="capsule", seq=seq,
                           mime_type="audio/wav", data=bytes([seq + 1]),
                           transcriber=lambda *a, _result=result, **k: _result)
    receipts = _wait(lambda: len(_receipts(store)) == 2 and _receipts(store))
    assert {row["seq"]: row["text"] for row in receipts} == {
        0: "Design a GPU queue. 保持租户公平。", 1: ""}
    rows = [row for row in store.snapshot()["events"] if row["kind"] == "capsule_speech"]
    assert [row["text"] for row in rows] == ["Design a GPU queue. 保持租户公平。"]


def test_capsule_receipt_carries_a_decode_error(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, consent=False, observe_apps=False)

    def broken(*_args, **_kwargs):
        raise RuntimeError("ffmpeg could not decode the live audio")
    store.ingest_audio(session_id=session["session_id"], source="capsule", seq=0,
                       mime_type="audio/webm", data=b"bad", transcriber=broken)
    receipts = _wait(lambda: _receipts(store))
    assert receipts[0]["seq"] == 0 and receipts[0]["text"] == ""
    assert "ffmpeg could not decode" in receipts[0]["error"]


def test_continuous_speech_gets_no_capsule_receipt(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=True, consent=True, observe_apps=False)
    store.ingest_audio(session_id=session["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"room",
                       transcriber=lambda *_a, **_k: {"text": "Talking about lunch."})
    assert _wait(lambda: [row for row in store.snapshot()["events"] if row["kind"] == "speech"])
    assert _receipts(store) == []


def test_hands_free_voice_does_not_answer_a_capsule_command(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, consent=False, observe_apps=False, voice_dialogue=True)
    store.ingest_audio(session_id=session["session_id"], source="capsule", seq=0,
                       mime_type="audio/webm", data=b"cmd",
                       transcriber=lambda *_a, **_k: {"text": "open the release checklist"})
    assert _wait(lambda: _receipts(store))
    asked = []
    assert live.run_voice_dialogue_once(tmp_path, analyzer=lambda p: asked.append(p) or "Sure.") \
        is False
    assert asked == []


def test_a_continuous_copy_of_a_capsule_command_is_not_conversation(tmp_path):
    """The native continuous recognizer heard the command before the capsule clip was decoded."""
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=True, consent=True, understand=False, observe_apps=False,
                          voice_dialogue=True)
    store.add_event(source="you", kind="speech", text="Open my project notes",
                    session_id=session["session_id"])
    store.ingest_audio(session_id=session["session_id"], source="capsule", seq=0,
                       mime_type="audio/webm", data=b"c",
                       transcriber=lambda *_a, **_k: {"text": "Open my project notes."})
    assert _wait(lambda: _receipts(store))
    mine = [(e["kind"], e["text"]) for e in store.snapshot()["events"] if e["source"] == "you"]
    assert mine == [("capsule_speech", "Open my project notes.")]
    assert _receipts(store) == [{"seq": 0, "text": "Open my project notes.", "error": ""}]
    asked = []
    assert live.run_voice_dialogue_once(tmp_path, analyzer=lambda p: asked.append(p) or "OK.") \
        is False
    assert asked == []


def test_a_capsule_command_leaves_unrelated_conversation_alone(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=True, consent=True, understand=False, observe_apps=False)
    store.add_event(source="you", kind="speech", text="We should ship on Friday",
                    session_id=session["session_id"])
    store.ingest_audio(session_id=session["session_id"], source="capsule", seq=0,
                       mime_type="audio/webm", data=b"c",
                       transcriber=lambda *_a, **_k: {"text": "Open my project notes."})
    assert _wait(lambda: _receipts(store))
    assert [e["kind"] for e in store.snapshot()["events"] if e["source"] == "you"] == [
        "speech", "capsule_speech"]


# --- Without SenseVoice: the Windows recognizer's text is the clip -------------------------------

def test_native_capsule_text_becomes_the_clips_receipt(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, consent=False, observe_apps=False)
    sid = session["session_id"]
    assert store.ingest_capsule_text(session_id=sid, seq=0, text="Open my notes") == {
        "ok": True, "seq": 0}
    assert _receipts(store) == [{"seq": 0, "text": "Open my notes", "error": ""}]
    assert store.snapshot()["events"][-1]["kind"] == "capsule_speech"
    assert store.snapshot()["audio"]["capsule_seq"] == 0
    # A resent request is the same recording, not a second command.
    assert store.ingest_capsule_text(session_id=sid, seq=0, text="Open my notes")["duplicate"]
    assert len(_receipts(store)) == 1
    assert [e["kind"] for e in store.snapshot()["events"]].count("capsule_speech") == 1


def test_native_capsule_text_follows_the_audio_sequence_rules(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    sid = store.start(listen=False, consent=False, observe_apps=False)["session_id"]
    store.ingest_audio(session_id=sid, source="capsule", seq=0, mime_type="audio/webm",
                       data=b"a", transcriber=lambda *_a, **_k: {"text": "first command"})
    assert _wait(lambda: _receipts(store))
    with pytest.raises(live.LiveCopilotError, match="already accepted"):
        store.ingest_capsule_text(session_id=sid, seq=0, text="something else")
    with pytest.raises(live.LiveCopilotError, match="gap: expected 1"):
        store.ingest_capsule_text(session_id=sid, seq=2, text="skipped one")
    store.ingest_capsule_text(session_id=sid, seq=1, text="second command")
    assert [row["text"] for row in _receipts(store)] == ["first command", "second command"]


@pytest.mark.parametrize("heard", ["", "Uh, um."])
def test_native_silence_or_filler_gets_an_empty_receipt(tmp_path, heard):
    store = live.LiveSessionStore(tmp_path)
    sid = store.start(listen=False, consent=False, observe_apps=False)["session_id"]
    store.ingest_capsule_text(session_id=sid, seq=0, text=heard)
    assert _receipts(store) == [{"seq": 0, "text": "", "error": ""}]
    assert "capsule_speech" not in [e["kind"] for e in store.snapshot()["events"]]


def test_native_capsule_text_needs_the_current_session(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    sid = store.start(listen=False, consent=False, observe_apps=False)["session_id"]
    store.stop()
    with pytest.raises(live.LiveCopilotError, match="no longer active"):
        store.ingest_capsule_text(session_id=sid, seq=0, text="Open my notes")
    with pytest.raises(live.LiveCopilotError, match="4,000"):
        live.LiveSessionStore(tmp_path).ingest_capsule_text(session_id=sid, seq=0, text="x" * 4001)


def test_capsule_text_http_route(tmp_path, monkeypatch):
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from harness import webapp

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    store = live.LiveSessionStore(str(tmp_path))
    sid = store.start(listen=False, consent=False, observe_apps=False)["session_id"]
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/live-copilot/capsule-text?token=%s" % (
                server.server_port, webapp.TOKEN),
            data=json.dumps({"session_id": sid, "seq": 0, "text": "Open my notes"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert _receipts(live.LiveSessionStore(str(tmp_path)))[0]["text"] == "Open my notes"


# --- Live text while the person is still talking ------------------------------------------------

def test_capsule_preview_is_ephemeral_and_does_not_touch_the_log(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, consent=False, observe_apps=False)
    before = store.snapshot()

    def transcribe(path, **kwargs):
        with open(path, "rb") as handle:
            assert handle.read() == b"cumulative-webm"
        assert kwargs == {"mime_type": "audio/webm", "language": ""}
        return {"text": "帮我清空一下画板。", "segments": []}

    result = store.preview_audio(session_id=session["session_id"],
                                 mime_type="audio/webm;codecs=opus", data=b"cumulative-webm",
                                 transcriber=transcribe)
    assert result == {"ok": True, "text": "帮我清空一下画板。", "final": False,
                      "engine": "SenseVoice", "language": "auto"}
    after = store.snapshot()
    assert after["events"] == before["events"]
    assert after["audio"].get("capsule_seq") is None and after["audio"]["pending"] == 0
    assert not list((tmp_path / "live-audio").rglob("capsule-preview-*"))


def test_capsule_preview_drops_filler(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, consent=False, observe_apps=False)
    result = store.preview_audio(
        session_id=session["session_id"], mime_type="audio/webm", data=b"quiet-webm",
        transcriber=lambda *_a, **_k: {"text": "The.", "segments": []})
    assert result["text"] == ""


@pytest.mark.parametrize("replacement", [False, True])
def test_capsule_preview_cannot_return_after_authority_ends(tmp_path, replacement):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, observe_apps=False)

    def decode(path, **kwargs):
        store.stop()
        if replacement:
            store.start(listen=False, observe_apps=False)
        return {"text": "old session text"}
    with pytest.raises(live.LiveCopilotError, match="authority"):
        store.preview_audio(session_id=session["session_id"], mime_type="audio/wav",
                            data=b"audio", transcriber=decode)
    assert not list((tmp_path / "live-audio").rglob("capsule-preview-*"))


def test_capsule_preview_refuses_bad_input(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=False, observe_apps=False)
    with pytest.raises(live.LiveCopilotError, match="empty"):
        store.preview_audio(session_id=session["session_id"], mime_type="audio/webm", data=b"")
    with pytest.raises(live.LiveCopilotError, match="unsupported"):
        store.preview_audio(session_id=session["session_id"], mime_type="video/mp4", data=b"x")
    with pytest.raises(live.LiveCopilotError, match="authority"):
        store.preview_audio(session_id="live-other", mime_type="audio/webm", data=b"x")


@pytest.mark.parametrize("entry", ["preview", "microphone", "system", "capsule"])
def test_every_default_live_audio_entry_uses_the_speech_check(tmp_path, monkeypatch, entry):
    from harness import sensevoice
    seen = []

    def decode(path, **kwargs):
        seen.append(kwargs)
        return {"text": "", "segments": [],
                "speech_evidence": {"accepted": False, "reason": "no_speech"}}
    monkeypatch.setattr(sensevoice, "transcribe", decode)
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=True, consent=True, understand=False, observe_apps=False)
    if entry == "preview":
        assert store.preview_audio(session_id=session["session_id"], mime_type="audio/wav",
                                   data=b"noise")["text"] == ""
    else:
        store.ingest_audio(session_id=session["session_id"], source=entry, seq=0,
                           mime_type="audio/wav", data=b"noise")
        assert _wait(lambda: store.snapshot()["audio"]["pending"] == 0)
    assert seen and seen[0]["speech_gate"] is True
    assert not [e for e in store.snapshot()["events"] if e["kind"] in {"speech", "capsule_speech"}]


def test_audio_preview_http_route(tmp_path, monkeypatch):
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from harness import sensevoice, webapp

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sensevoice, "transcribe",
                        lambda path, **kwargs: {"text": "draw a queue", "segments": []})
    session = live.LiveSessionStore(str(tmp_path)).start(listen=False, observe_apps=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(session_id):
        url = "http://127.0.0.1:%d/api/live-copilot/audio-preview?token=%s&session=%s" % (
            server.server_port, webapp.TOKEN, session_id)
        request = urllib.request.Request(url, data=b"cumulative",
                                         headers={"Content-Type": "audio/webm"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())
    try:
        assert post(session["session_id"])["text"] == "draw a queue"
        with pytest.raises(urllib.error.HTTPError) as refused:
            post("live-gone")
        assert refused.value.code == 409
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert live.LiveSessionStore(str(tmp_path)).snapshot()["events"][-1]["kind"] == "session"


# --- Command or dictation ------------------------------------------------------------------------

@pytest.fixture
def desktop(monkeypatch):
    """A fake captured window: what has focus in it, and what gets typed."""
    from harness import native, native_input
    state = {"elements": [{"type": "Edit", "name": "Prompt", "focused": True, "enabled": True}],
             "focused": [], "typed": [], "focus_ok": True}
    monkeypatch.setattr(native, "tree", lambda **_kwargs: {"ok": True,
                                                            "elements": state["elements"]})
    monkeypatch.setattr(native_input, "focus_window", lambda **kwargs: state["focused"].append(
        kwargs) or {"ok": state["focus_ok"]})
    monkeypatch.setattr(native_input, "type_text",
                        lambda text: state["typed"].append(text) or {"ok": True})
    monkeypatch.setattr(native_input, "press", lambda *a, **k: pytest.fail("pressed a key"))
    return state


def test_dictation_types_only_into_the_captured_edit_and_never_submits(tmp_path, desktop):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Notes", listen=False, consent=False, observe_ui=True)
    handoff = store.request_handoff(app="chrome", title="Notes", pid=42, hwnd=9001)

    result = store.dictate(text="whole truth and although", handoff_id=handoff["id"])

    assert desktop["focused"] == [{"hwnd": 9001, "pid": 42}]
    assert desktop["typed"] == ["whole truth and although"]
    assert result["submitted"] is False and result["control"] == "Edit"
    after = store.snapshot()
    assert after["handoff"]["pending"] is False
    assert after["events"][-1]["kind"] == "dictation"
    assert after["events"][-1]["text"] == "whole truth and although"


@pytest.mark.parametrize("elements", [
    [{"type": "Pane", "name": "Excalidraw", "focused": True}],
    [{"type": "Edit", "name": "Search", "focused": False}],
    [{"type": "Edit", "name": "Locked", "focused": True, "enabled": False}],
])
def test_dictation_refuses_a_target_without_a_focused_editable_field(tmp_path, desktop, elements):
    desktop["elements"] = elements
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Board", listen=False, consent=False, observe_ui=True)
    handoff = store.request_handoff(app="chrome", title="Board", pid=42, hwnd=9001)
    with pytest.raises(live.LiveCopilotError, match="focused editable"):
        store.dictate(text="clear the board", handoff_id=handoff["id"])
    assert desktop["typed"] == []
    assert store.snapshot()["handoff"]["pending"] is True


def test_dictation_needs_the_current_handoff_and_a_window(tmp_path, desktop):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Notes", listen=False, consent=False, observe_ui=True)
    old = store.request_handoff(app="chrome", title="Notes", pid=42, hwnd=9001)
    store.request_handoff(app="code", title="main.py", pid=43, hwnd=9002)
    with pytest.raises(live.LiveCopilotError, match="no longer current"):
        store.dictate(text="hello", handoff_id=old["id"])
    windowless = store.request_handoff(app="code", title="", pid=0, hwnd=0)
    with pytest.raises(live.LiveCopilotError, match="no writable window"):
        store.dictate(text="hello", handoff_id=windowless["id"])
    desktop["focus_ok"] = False
    current = store.request_handoff(app="code", title="main.py", pid=43, hwnd=9002)
    with pytest.raises(live.LiveCopilotError, match="could not bring back"):
        store.dictate(text="hello", handoff_id=current["id"])
    assert desktop["typed"] == []


@pytest.mark.parametrize("mode,confidence,expected", [
    ("dictation", 0.77, "clarify"), ("dictation", 0.91, "dictation"),
    ("command", 0.77, "clarify"), ("command", 0.95, "command"),
    ("clarify", 0.99, "clarify"), ("unknown", 0.99, "clarify"),
])
def test_model_routing_requires_clear_intent(monkeypatch, mode, confidence, expected):
    import json
    from types import SimpleNamespace
    from harness import cancellation, providers, settings

    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None: {
        "PROVIDER": "fixture", "MODEL": "gpt-6-astra", "INTERACTIVE_SPEED": "fast",
    }.get(key, default))
    monkeypatch.setattr(providers, "provider_capabilities",
                        lambda *_args, **_kwargs: {"speed_tiers": ["fast"]})
    made = []
    monkeypatch.setattr(providers, "make_provider",
                        lambda *args, **kwargs: made.append((args, kwargs)) or object())
    monkeypatch.setattr(cancellation, "complete", lambda *_args, **_kwargs: SimpleNamespace(
        stop_reason="end_turn", error_detail="",
        text=json.dumps({"mode": mode, "confidence": confidence, "reason": "context"})))

    result = live.classify_capsule_payload({"utterance": "literal words"})

    assert result["mode"] == expected and result["model"] == "gpt-6-astra"
    assert made[0][1]["effort"] == "low"


def test_classifier_errors_are_reported_not_guessed(monkeypatch):
    from types import SimpleNamespace
    from harness import cancellation, providers, settings

    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: {"PROVIDER": "fixture"}.get(key, default))
    monkeypatch.setattr(providers, "provider_capabilities", lambda *a, **k: {})
    monkeypatch.setattr(providers, "make_provider", lambda *a, **k: object())
    monkeypatch.setattr(cancellation, "complete", lambda *a, **k: SimpleNamespace(
        stop_reason="end_turn", error_detail="", text="I think it is a command"))
    with pytest.raises(live.LiveCopilotError, match="invalid JSON"):
        live.classify_capsule_payload({"utterance": "open notes"})
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    with pytest.raises(live.LiveCopilotError, match="real model provider"):
        live.classify_capsule_payload({"utterance": "open notes"})


def test_intent_uses_the_captured_target_and_a_deadline(tmp_path, monkeypatch):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Writing release notes", listen=False, consent=False, observe_apps=False)
    hand = store.request_handoff(app="notepad", title="notes.txt")
    seen = {}

    def classify(payload, *, cancelled=None):
        seen.update(payload=payload, cancelled=cancelled)
        return {"mode": "dictation", "confidence": .95, "reason": "literal text"}
    monkeypatch.setattr(live, "classify_capsule_payload", classify)
    result = store.classify_capsule_intent(text="ship it on Friday", handoff_id=hand["id"])
    assert result["mode"] == "dictation"
    assert seen["payload"]["utterance"] == "ship it on Friday"
    assert seen["payload"]["captured_target"]["app"] == "notepad"
    assert seen["payload"]["live_context"] == "Writing release notes"
    assert callable(seen["cancelled"]) and seen["cancelled"]() is False


def test_intent_and_dictate_http_routes_reach_the_store(tmp_path, monkeypatch, desktop):
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from harness import webapp

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(live, "classify_capsule_payload",
                        lambda payload, **_k: {"mode": "dictation", "confidence": .9})
    store = live.LiveSessionStore(str(tmp_path))
    store.start(context="Notes", listen=False, consent=False, observe_ui=True)
    hand = store.request_handoff(app="notepad", title="notes.txt", pid=5, hwnd=77)
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
        body = {"handoff_id": hand["id"], "text": "ship it Friday"}
        assert post("/api/live-copilot/intent", body) == (200, {"mode": "dictation",
                                                                "confidence": .9})
        status, typed = post("/api/live-copilot/dictate", body)
        assert status == 201 and typed["submitted"] is False
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert desktop["typed"] == ["ship it Friday"]


def test_context_change_during_intent_classification_rejects_result(monkeypatch, tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Backend review", listen=False, consent=False, observe_apps=False)
    hand = store.request_handoff(app="fixture")

    def delayed(_payload, **_kwargs):
        store.request_handoff(app="different")
        return {"mode": "command", "confidence": 1}
    monkeypatch.setattr(live, "classify_capsule_payload", delayed)
    with pytest.raises(live.LiveCopilotError, match="changed"):
        store.classify_capsule_intent(text="Explain this", handoff_id=hand["id"])
