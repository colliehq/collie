"""Live session authority and asynchronous result boundaries (offline)."""
import json
from pathlib import Path

import pytest

from harness import live_copilot as live


PERMISSIONS = (
    "listen", "understand", "observe_apps", "observe_ui", "observe_input",
    "observe_screen", "voice_dialogue", "board_edit", "consent",
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "capabilities", lambda: {})
    return live.LiveSessionStore(tmp_path)


@pytest.mark.parametrize("field", PERMISSIONS)
def test_start_rejects_string_permissions_without_replacing_session(store, field):
    current = store.start(listen=False)
    before = Path(store.path).read_bytes()
    with pytest.raises(live.LiveCopilotError, match="boolean"):
        store.start(**{**{"listen": False, "consent": True}, field: "false"})
    assert Path(store.path).read_bytes() == before
    assert store.snapshot()["session_id"] == current["session_id"]


@pytest.mark.parametrize("field", PERMISSIONS)
def test_permission_update_rejects_strings_atomically(store, field):
    store.start(listen=False, understand=False)
    before = Path(store.path).read_bytes()
    with pytest.raises(live.LiveCopilotError, match="boolean"):
        store.update_permissions(**{**{"understand": True}, field: "false"})
    assert Path(store.path).read_bytes() == before


@pytest.mark.parametrize("invalid", [0, 1, [], {}, "true"])
def test_listening_requires_a_boolean_not_a_truthy_value(store, invalid):
    store.start(listen=False)
    with pytest.raises(live.LiveCopilotError, match="boolean"):
        store.update_permissions(listen=invalid)
    assert store.snapshot()["listen"] is False


@pytest.mark.parametrize("field", ("active",) + PERMISSIONS[:-1])
def test_corrupt_durable_permissions_fail_closed(store, field):
    store.start(listen=False)
    value = json.loads(Path(store.path).read_text(encoding="utf-8"))
    value[field] = "false"
    with open(store.path, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    before = Path(store.path).read_bytes()
    with pytest.raises(live.LiveCopilotError, match="boolean"):
        store.snapshot()
    assert Path(store.path).read_bytes() == before


def test_valid_permission_updates_preserve_consent_requirement(store):
    store.start(listen=False, understand=False)
    with pytest.raises(live.LiveCopilotError, match="consent"):
        store.update_permissions(listen=True)
    enabled = store.update_permissions(listen=True, consent=True, observe_screen=True)
    assert enabled["listen"] and enabled["consent_at_ms"] and enabled["observe_screen"]
    disabled = store.update_permissions(listen=False, observe_screen=False)
    assert not disabled["listen"] and not disabled["observe_screen"]


def test_audio_completion_cannot_append_into_a_replacement_session(store, tmp_path, monkeypatch):
    original = store.start(listen=True, consent=True)
    audio = tmp_path / "chunk.webm"
    audio.write_bytes(b"test audio")
    add_event = store.add_event
    replacement = []

    def switch_session_then_append(**kwargs):
        # Switch at the exact boundary after transcription has updated its old session's
        # pending count, but before the transcript is appended under a new lock.
        store.stop()
        replacement.append(store.start(listen=False)["session_id"])
        return add_event(**kwargs)

    monkeypatch.setattr(store, "add_event", switch_session_then_append)
    store._transcribe_audio(original["session_id"], "microphone", str(audio), "audio/webm",
                            lambda *_args, **_kwargs: {"text": "old private transcript"})
    current = store.snapshot()
    assert current["session_id"] == replacement[0] != original["session_id"]
    assert all(row.get("text") != "old private transcript" for row in current["events"])
    assert current["audio"]["pending"] == 0
    assert not audio.exists()


def test_dialogue_completion_is_discarded_when_voice_dialogue_is_disabled(store):
    store.start(listen=False, observe_apps=False, voice_dialogue=True)
    store.add_event(source="you", kind="speech", text="What should I do next?")

    def analyze(_payload):
        store.update_permissions(voice_dialogue=False)
        return "A late answer that must not be spoken."

    assert live.run_voice_dialogue_once(store.root, analyzer=analyze) is False
    current = store.snapshot()
    assert current["suggestions"] == []
    assert current["analysis"]["dialogue_inflight"] is False


def test_stop_clears_both_analysis_lanes(store):
    store.start(listen=False, voice_dialogue=True)
    store.add_event(source="you", kind="speech", text="Can you help?")

    def analyze(_payload):
        store.stop()
        return "Too late."

    assert live.run_voice_dialogue_once(store.root, analyzer=analyze) is False
    current = store.snapshot()
    assert current["analysis"]["inflight"] is False
    assert current["analysis"]["dialogue_inflight"] is False
    assert current["suggestions"] == []


def test_long_multilingual_session_stays_readable_and_keeps_newest_context(store):
    store.start(listen=False)
    value = store._read()
    value["events"] = [
        {"id": "evt-%d" % index, "source": "you", "kind": "speech", "text": "中" * 4_000}
        for index in range(live.MAX_EVENTS)
    ]
    store._write(value)
    assert Path(store.path).stat().st_size <= live.MAX_STATE_BYTES
    current = store._read()
    assert 0 < len(current["events"]) < live.MAX_EVENTS
    assert current["events"][-1]["id"] == "evt-%d" % (live.MAX_EVENTS - 1)
    store.add_event(source="typed", text="The session still accepts new context.")
    assert store.snapshot()["events"][-1]["text"] == "The session still accepts new context."


def test_oversized_non_event_state_does_not_destroy_last_readable_state(store):
    store.start(listen=False)
    before = Path(store.path).read_bytes()
    value = store._read()
    value["summary"] = "x" * live.MAX_STATE_BYTES
    with pytest.raises(live.LiveCopilotError, match="limit"):
        store._write(value)
    assert Path(store.path).read_bytes() == before
    assert not list(Path(store.root).glob("*.tmp"))
