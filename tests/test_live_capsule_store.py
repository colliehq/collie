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
