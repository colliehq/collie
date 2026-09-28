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
