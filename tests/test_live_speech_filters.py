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


# --- The same speech heard twice, and Collie hearing itself ------------------------------------

@pytest.fixture
def clock(monkeypatch):
    now = [3_000_000]
    monkeypatch.setattr(live, "_now_ms", lambda: now[0])
    return now


def _speech(store):
    return [row for row in store.snapshot()["events"] if row.get("kind") == "speech"]


def test_system_audio_wins_when_microphone_echo_arrives_first(clock, tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Backend review", listen=False, consent=False, observe_apps=False)
    microphone = store.add_event(
        source="you", kind="speech",
        text="How would you fence a retry after the worker lease expires?")
    clock[0] += 900
    system = store.add_event(
        source="other", speaker="Anupreet", kind="speech",
        text="How would you fence a retry after a worker lease expires?")

    speech = _speech(store)
    assert [row["id"] for row in speech] == [system["id"]]
    assert speech[0]["source"] == "other" and speech[0]["speaker"] == "Anupreet"
    assert microphone["id"] not in {row["id"] for row in store.snapshot()["events"]}
    assert _audit(store)[-1]["action"] == "speech_echo_reattributed"


def test_microphone_echo_is_suppressed_when_system_audio_arrives_first(clock, tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Backend review", listen=False, consent=False, observe_apps=False)
    canonical = store.add_event(
        source="other", speaker="Anupreet", kind="speech",
        text="Explain how your cache key changes when a custom node version changes.")
    clock[0] += 2_200
    duplicate = store.add_event(
        source="you", kind="speech",
        text="Explain how the cache key changes when a custom node version changes.")

    assert [row["id"] for row in _speech(store)] == [canonical["id"]]
    assert duplicate["ignored"] and duplicate["duplicate_of"] == canonical["id"]
    assert duplicate["reason"] == "system_audio_duplicate"
    assert _audit(store)[-1]["action"] == "speech_echo_suppressed"


def test_echo_filter_keeps_a_real_answer_and_a_late_repetition(clock, tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Backend review", listen=False, consent=False, observe_apps=False)
    store.add_event(source="other", kind="speech",
                    text="Would you store the queue in Redis or Postgres?")
    clock[0] += 700
    answer = store.add_event(
        source="you", kind="speech",
        text="I would start with Postgres because transactional claiming simplifies correctness.")
    clock[0] += 12_500
    repeated = store.add_event(source="you", kind="speech",
                               text="Would you store the queue in Redis or Postgres?")

    assert not answer.get("ignored") and not repeated.get("ignored")
    assert len(_speech(store)) == 3


def test_short_speech_is_never_treated_as_an_echo(clock, tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Review", listen=False, consent=False, observe_apps=False)
    store.add_event(source="other", kind="speech", text="Yes.")
    clock[0] += 300
    mine = store.add_event(source="you", kind="speech", text="Yes.")
    assert not mine.get("ignored") and len(_speech(store)) == 2


def test_same_source_exact_repeat_is_one_event_moved_to_the_end(clock, tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(context="Review", listen=False, consent=False, observe_apps=False)
    first = store.add_event(source="you", kind="speech", text="Let me check the retry budget first.")
    clock[0] += 400
    store.add_event(source="system", kind="window", app="code", text="Foreground app changed.")
    clock[0] += 600
    again = store.add_event(source="you", kind="speech", text="Let me check the retry budget first.")

    assert again["id"] == first["id"] and again["repeat_count"] == 2
    assert store.snapshot()["events"][-1]["id"] == first["id"]
    assert len(_speech(store)) == 1


def _with_cue(store, text, cue_id="cue-fence"):
    with store._transaction():
        value = store._read()
        value["suggestions"] = [{"id": cue_id, "lane": "dialogue", "kind": "answer",
                                 "urgency": "now", "text": text}]
        store._write(value)


def test_recent_collie_voice_is_discarded_but_the_window_expires(clock, tmp_path):
    store = live.LiveSessionStore(tmp_path)
    state = store.start(context="Backend review", listen=False, consent=False,
                        observe_apps=False, voice_dialogue=True)
    asked = store.add_event(source="you", kind="speech", text="Collie, explain fencing tokens.")
    _with_cue(store, "Use a monotonic fencing token and reject every stale commit atomically.")
    store.set_voice_playback(session_id=state["session_id"], cue_id="cue-fence", speaking=True)
    clock[0] += 4_000
    loopback = store.add_event(source="other", kind="speech",
                               text="monotonic fencing token and reject every stale commit")
    playback = store.set_voice_playback(session_id=state["session_id"], cue_id="cue-fence",
                                        speaking=False)
    assert playback["active"] is False and playback["ended_at_ms"] == clock[0]
    clock[0] += 15_100
    real = store.add_event(
        source="you", kind="speech",
        text="Use a monotonic fencing token and reject every stale commit atomically.")

    assert loopback["ignored"] and loopback["reason"] == "recent_collie_voice"
    assert not real.get("ignored")
    assert [row["id"] for row in _speech(store)] == [asked["id"], real["id"]]


def test_a_short_fragment_of_a_long_spoken_cue_is_still_collie(clock, tmp_path):
    store = live.LiveSessionStore(tmp_path)
    state = store.start(context="Review", listen=False, consent=False, observe_apps=False,
                        voice_dialogue=True)
    _with_cue(store, (
        "Start with Postgres and a transactional claim on each job row; add a fencing token so a "
        "worker whose lease expired cannot commit, and move to Redis streams only if throughput "
        "numbers show the database is the bottleneck."))
    store.set_voice_playback(session_id=state["session_id"], cue_id="cue-fence", speaking=True)
    clock[0] += 3_000
    tail = store.add_event(source="you", kind="speech",
                           text="a fencing token so a worker whose lease expire cannot commit")
    reply = store.add_event(source="you", kind="speech",
                            text="I would use Postgres because the claim is transactional")
    assert tail["ignored"] and tail["reason"] == "recent_collie_voice"
    assert not reply.get("ignored")


def _after_cue(clock, tmp_path, cue):
    """A session in which Collie has just finished speaking ``cue``."""
    store = live.LiveSessionStore(tmp_path)
    state = store.start(context="Review", listen=True, consent=True, understand=False,
                        observe_apps=False, voice_dialogue=True)
    _with_cue(store, cue)
    store.set_voice_playback(session_id=state["session_id"], cue_id="cue-fence", speaking=True)
    clock[0] += 2_000
    store.set_voice_playback(session_id=state["session_id"], cue_id="cue-fence", speaking=False)
    clock[0] += 1_000
    return store, state


@pytest.mark.parametrize("cue", ["Send the invoice to Alex",
                                 "Send the invoice to Alex and Maria today"])
@pytest.mark.parametrize("said", [
    "No, don't send the invoice to Alex until Maria approves it",
    "Wait, before you send the invoice to Alex, fix the total",
    "Send the invoice to Alex now",
    "No, send the invoice to Alex",
])
def test_the_person_repeating_part_of_collies_cue_with_their_own_words_is_kept(clock, tmp_path,
                                                                               cue, said):
    store, _ = _after_cue(clock, tmp_path, cue)
    row = store.add_event(source="you", kind="speech", text=said)
    assert not row.get("ignored")
    assert [event["text"] for event in _speech(store)] == [said]


def test_a_capsule_command_is_never_treated_as_collies_voice(clock, tmp_path):
    # Opening the capsule cuts Collie's voice first, so a capsule clip cannot contain its echo.
    store, state = _after_cue(clock, tmp_path, "Open the quarterly report now")
    store.ingest_audio(session_id=state["session_id"], source="capsule", seq=0,
                       mime_type="audio/webm", data=b"clip",
                       transcriber=lambda *_a, **_k: {"text": "Open the quarterly report now."})
    _drain(store)
    receipts = store.snapshot()["audio"]["capsule_results"]
    assert receipts == [{"seq": 0, "text": "Open the quarterly report now.", "error": ""}]
    assert [e["kind"] for e in store.snapshot()["events"]][-1] == "capsule_speech"


@pytest.mark.parametrize("transcript,echo", [
    ("Use a monotonic fencing token and reject every stale commit atomically.", True),
    ("monotonic fencing token and reject every stale commit", True),
    ("fencing token and reject every stale commit atomically please", False),   # a word added
    ("reject every stale commit", True),
    ("reject stale", False),                                                     # too short
    ("先用 Postgres 做事务认领", False),
])
def test_collie_echo_is_one_directional(transcript, echo):
    cue = "Use a monotonic fencing token and reject every stale commit atomically."
    assert live._collie_echo(transcript, cue) is echo


def test_a_chinese_fragment_of_collies_cue_is_collie():
    cue = "先用 Postgres 做事务认领，再加 fencing token 防止过期 worker 提交。"
    assert live._collie_echo("再加fencing token防止过期worker提交", cue) is True
    assert live._collie_echo("我觉得用 Postgres 就行，事务更简单", cue) is False


def test_voice_playback_is_bound_to_the_session_and_a_current_cue(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    state = store.start(context="Review", listen=False, consent=False, observe_apps=False)
    _with_cue(store, "Check the lease first.")
    with pytest.raises(live.LiveCopilotError, match="no longer available"):
        store.set_voice_playback(session_id=state["session_id"], cue_id="gone", speaking=True)
    with pytest.raises(live.LiveCopilotError, match="different live session"):
        store.set_voice_playback(session_id="live-other", cue_id="cue-fence", speaking=True)
    with pytest.raises(live.LiveCopilotError, match="boolean"):
        store.set_voice_playback(session_id=state["session_id"], cue_id="cue-fence",
                                 speaking="true")
    started = store.set_voice_playback(session_id=state["session_id"], cue_id="cue-fence",
                                       speaking=True)
    assert started["active"] and store.snapshot()["voice_playback"]["cue_id"] == "cue-fence"
    # Ending a different cue does not end this one.
    store.set_voice_playback(session_id=state["session_id"], cue_id="other", speaking=False)
    assert store.snapshot()["voice_playback"]["active"] is True
    # A new session never inherits the old playback window.
    assert store.start(listen=False, observe_apps=False)["voice_playback"]["active"] is False


def test_voice_state_http_route_records_playback(tmp_path, monkeypatch):
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from harness import webapp

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    store = live.LiveSessionStore(str(tmp_path))
    state = store.start(context="Review", listen=False, consent=False, observe_apps=False)
    _with_cue(store, "Check the lease first.")
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d/api/live-copilot/voice-state?token=%s" % (
            server.server_port, webapp.TOKEN)
        body = json.dumps({"session_id": state["session_id"], "cue_id": "cue-fence",
                           "speaking": True}).encode()
        request = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.loads(response.read())["active"] is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert live.LiveSessionStore(str(tmp_path)).snapshot()["voice_playback"]["cue_id"] == "cue-fence"


def test_native_voice_reports_its_lifetime_and_the_capsule_barges_in():
    root = Path(__file__).parents[1] / "harness"
    native = (root / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    ended = native.split("static void PostLiveVoiceEnded(", 1)[1].split("static void", 1)[0]
    assert '\\"live-native-voice-state\\"' in ended and '\\"speaking\\":false' in ended
    stop_voice = native.split("static void StopLiveVoice()", 1)[1].split("static void", 1)[0]
    assert "if (wasSpeaking) PostLiveVoiceEnded(session, cue);" in stop_voice
    speak = native.split("static void SpeakLiveCue(", 1)[1].split("static void", 1)[0]
    assert speak.count("PostLiveVoiceEnded(session, cueId);") == 2   # finished, failed to start
    capsule_open = native.split("void OpenLiveCapsule", 1)[1].split("try", 1)[0]
    assert "StopLiveVoice();" in capsule_open
    page = (root / "webui" / "index.html").read_text(encoding="utf-8")
    assert 'data.type === "live-native-voice-state"' in page
    dispatch = page.split("function dispatchLiveVoice(", 1)[1].split("function syncLiveNav", 1)[0]
    # The server knows Collie is about to speak before the native voice starts.
    assert "postLiveVoiceState(session, cueId, true).then(" in dispatch
    assert dispatch.index("postLiveVoiceState(session, cueId, true).then(") < \
        dispatch.index("window.chrome.webview.postMessage(payload)")
