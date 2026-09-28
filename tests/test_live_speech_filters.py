"""Live keeps real short speech but not recognizer filler or Collie's own echo.

Everything here drives LiveSessionStore with injected transcribers; no audio device, model, or
real Collie state is touched.
"""
from pathlib import Path
import time

import pytest

from harness import live_copilot as live


def _drain(store, deadline=3.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end and store.snapshot()["audio"]["pending"]:
        time.sleep(.01)
    assert store.snapshot()["audio"]["pending"] == 0


def _audit(store):
    with store._transaction():
        return store._read()["audit"]


@pytest.mark.parametrize("text", [
    "", ".", "。", "…?!", "🎵", "The.", "um", "Uh, um.", "hmm?", "and the",
])
def test_filler_and_punctuation_are_not_meaningful(text):
    assert not live._meaningful_transcript(text)


def test_recognizer_special_tags_leave_nothing_meaningful():
    from harness.sensevoice import _SPECIAL_TOKEN
    assert not live._meaningful_transcript(_SPECIAL_TOKEN.sub("", "<|nospeech|><|EMO_UNKNOWN|>"))


@pytest.mark.parametrize("text", [
    "stop", "Yes", "Explain?", "清空", "好", "はい", "네", "the 3rd one", "Oh, the queue is full",
])
def test_short_real_speech_is_kept(text):
    assert live._meaningful_transcript(text)


@pytest.mark.parametrize("source", ["microphone", "capsule"])
def test_filler_transcript_is_dropped_and_audited(tmp_path, source):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=True, consent=True, understand=False, observe_apps=False)
    store.ingest_audio(session_id=session["session_id"], source=source, seq=0,
                       mime_type="audio/webm", data=b"quiet-webm",
                       transcriber=lambda *_a, **_k: {"text": "The.", "segments": []})
    _drain(store)
    texts = [row["text"] for row in store.snapshot()["events"]]
    assert "The." not in texts
    assert _audit(store)[-1]["action"] == "speech_fragment_suppressed"
    assert "source=%s" % source in _audit(store)[-1]["detail"]


def test_filler_segments_are_dropped_but_real_segments_kept(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    session = store.start(listen=True, consent=True, understand=False, observe_apps=False)
    store.ingest_audio(
        session_id=session["session_id"], source="system", seq=0, mime_type="audio/webm",
        data=b"meeting", transcriber=lambda *_a, **_k: {"segments": [
            {"speaker": "A", "text": "Um."},
            {"speaker": "B", "text": "Which region serves the European tenants?"}]})
    _drain(store)
    speech = [row for row in store.snapshot()["events"] if row["source"] == "other"]
    assert [(row["speaker"], row["text"]) for row in speech] == [
        ("B", "Which region serves the European tenants?")]


def test_native_continuous_recognition_drops_low_confidence_but_keeps_short_commands():
    source = (Path(__file__).parents[1] / "harness" / "wallpaper" / "Program.cs").read_text(
        encoding="utf-8")
    live_speech = source.split("static void ResumeLiveSpeech()", 1)[1].split(
        "static void ConfigureLiveSpeech", 1)[0]
    assert "e.Result.Confidence" in live_speech and "confidence < 0.55f" in live_speech
    # A confident "stop" or "cancel" is a command: length alone must not drop it here. What is
    # only filler ("the", "uh") is the server's to drop (_meaningful_transcript).
    assert "spoken.Length <" not in live_speech and "tinyLatin" not in live_speech
    # The filter sits before the transcript is posted, not after.
    assert live_speech.index("confidence < 0.55f") < live_speech.index("live-native-transcript")
