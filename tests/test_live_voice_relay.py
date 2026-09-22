"""A temporary TTS mute cannot restore a superseded listening permission."""
import pytest

from harness import live_copilot as live, live_voice_relay as relay


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "capabilities", lambda: {})
    store = live.LiveSessionStore(tmp_path)
    store.start(listen=True, consent=True)
    return store


def test_owned_pause_resumes_once_without_creating_consent(store):
    before = store.snapshot()
    session = before["session_id"]
    token = store.pause_listening_for_voice(session_id=session)
    assert token and not store.snapshot()["listen"]
    assert not store.resume_listening_after_voice(session_id=session, token="wrong")
    assert store.resume_listening_after_voice(session_id=session, token=token)
    assert store.snapshot()["listen"]
    assert store.snapshot()["consent_at_ms"] == before["consent_at_ms"]
    assert not store.resume_listening_after_voice(session_id=session, token=token)


@pytest.mark.parametrize("interruption", ["disable", "stop", "replace", "toggle"])
def test_explicit_choice_or_session_change_supersedes_pause(store, interruption):
    session = store.snapshot()["session_id"]
    token = store.pause_listening_for_voice(session_id=session)
    if interruption == "disable":
        store.update_permissions(listen=False)
    elif interruption == "stop":
        store.stop()
    elif interruption == "replace":
        store.start(listen=False)
    else:
        store.update_permissions(listen=True)
        store.update_permissions(listen=False)
    assert not store.resume_listening_after_voice(session_id=session, token=token)
    assert not store.snapshot()["listen"]


def test_stale_relay_cannot_pause_replacement_session(store):
    old = store.snapshot()["session_id"]
    store.start(listen=True, consent=True)
    assert not store.pause_listening_for_voice(session_id=old)
    assert store.snapshot()["listen"]


def test_pausing_never_enables_an_unconsented_listener(store):
    session = store.start(listen=False)["session_id"]
    assert not store.pause_listening_for_voice(session_id=session)
    assert not store.resume_listening_after_voice(session_id=session, token="")
    assert not store.snapshot()["listen"] and not store.snapshot()["consent_at_ms"]


def test_relay_respects_user_disabling_listening_during_playback(store, monkeypatch):
    session = store.snapshot()["session_id"]
    original_snapshot = store.snapshot
    snapshots = 0
    playback_done = False
    spoken = []

    def snapshot():
        nonlocal snapshots, playback_done
        snapshots += 1
        if snapshots == 2:
            value = store._read()
            value["suggestions"] = [{"id": "fresh", "kind": "answer", "lane": "dialogue",
                                      "urgency": "now", "text": "A useful answer."}]
            store._write(value)
        elif playback_done:
            # The next loop runs after playback and its finally block have completed.
            assert not original_snapshot()["listen"]
            playback_done = False
            store.stop()
        return original_snapshot()

    class Voice:
        def prewarm(self):
            return True

        def speak(self, text):
            nonlocal playback_done
            spoken.append(text)
            assert not original_snapshot()["listen"]
            store.update_permissions(listen=False)
            playback_done = True
            return True

    monkeypatch.setattr(store, "snapshot", snapshot)
    monkeypatch.setattr(relay, "LiveSessionStore", lambda: store)
    monkeypatch.setattr(relay, "LocalNeuralVoice", Voice)
    monkeypatch.setattr(relay.time, "sleep", lambda _: None)
    assert relay.run(session) == 0
    assert spoken == ["A useful answer."]
