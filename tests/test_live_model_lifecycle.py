"""Live model work has an owner and stops at the session's permission boundary."""
import threading
import time
from types import SimpleNamespace

import pytest

from harness import live_copilot as live


def setup_runtime(path, analyzer=None):
    store = live.LiveSessionStore(path)
    store.start(listen=False, consent=False, understand=True, observe_apps=False,
                observe_ui=False, observe_input=False, observe_screen=False)
    store.add_event(source="typed", text="Review the CSV filter design.")
    runtime = live.LiveCopilotRuntime(path, analyzer=analyzer, debounce_ms=0, min_interval_ms=0)
    runtime.activity_source = None
    return store, runtime


def test_no_observation_permission_does_not_read_foreground_window(tmp_path, monkeypatch):
    _, runtime = setup_runtime(tmp_path, lambda _: {"summary": "Typed context only."})
    def forbidden():
        raise AssertionError("foreground observation was not authorized")
    monkeypatch.setattr(runtime, "_foreground_window", forbidden)
    assert runtime.tick() is True


def test_live_owner_cannot_be_replaced_when_a_request_exceeds_old_timeout(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def analyze(_):
        calls.append(True); entered.set(); assert release.wait(5)
        return {"summary": "Only one result."}
    store, first = setup_runtime(tmp_path, analyze)
    second = live.LiveCopilotRuntime(tmp_path, analyzer=analyze, debounce_ms=0, min_interval_ms=0)
    second.activity_source = None
    worker = threading.Thread(target=first.tick)
    worker.start()
    try:
        assert entered.wait(2)
        with store._transaction():
            value = store._read()
            value["analysis"]["claimed_at_ms"] = 1
            store._write(value)
        assert second.tick() is False
        assert len(calls) == 1
    finally:
        release.set(); worker.join(3)
    assert store.snapshot()["summary"] == "Only one result."


def test_dead_owner_can_be_recovered_immediately_without_waiting_for_timeout(tmp_path):
    store, runtime = setup_runtime(tmp_path, lambda _: {"summary": "Recovered now."})
    with store._transaction():
        value = store._read()
        value["analysis"].update(inflight=True, owner="dead-owner", claimed_at_ms=live._now_ms(), nonce="old")
        store._write(value)
    assert runtime.tick() is True
    assert store.snapshot()["summary"] == "Recovered now."


def test_saved_note_triggers_fresh_understanding_and_is_in_model_context(tmp_path):
    payloads = []
    def analyze(payload):
        payloads.append(payload)
        return {"summary": "Updated understanding."}
    store, runtime = setup_runtime(tmp_path, analyze)
    assert runtime.tick() is True
    assert runtime.tick() is False
    text = "Keep the original API compatible."
    note = store.add_note(text=text, session_id=store.snapshot()["session_id"])
    assert runtime.tick() is True
    assert payloads[-1]["user_notes"][-1]["text"] == text
    assert payloads[-1]["events"][-1]["text"] == text
    assert store.snapshot()["notes"][-1]["id"] == note["id"]


def test_oversize_or_stale_session_note_is_rejected_without_changing_state(tmp_path):
    from pathlib import Path
    store, _ = setup_runtime(tmp_path)
    before = Path(store.path).read_bytes()
    with pytest.raises(live.LiveCopilotError, match="nothing was saved"):
        store.add_note(text="x" * 2001)
    with pytest.raises(live.LiveCopilotError, match="different Live session"):
        store.add_note(text="old session note", session_id="live-old")
    assert Path(store.path).read_bytes() == before


def test_old_result_cannot_overwrite_new_permission_epoch(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def slow(_):
        entered.set(); assert release.wait(5)
        return {"summary": "Old private result."}
    store, first = setup_runtime(tmp_path, slow)
    worker = threading.Thread(target=first.tick)
    worker.start()
    try:
        assert entered.wait(2)
        store.update_permissions(understand=False)
        store.update_permissions(understand=True)
        second = live.LiveCopilotRuntime(tmp_path, analyzer=lambda _: {"summary": "Fresh result."},
                                         debounce_ms=0, min_interval_ms=0)
        second.activity_source = None
        assert second.tick() is True
    finally:
        release.set(); worker.join(3)
    assert store.snapshot()["summary"] == "Fresh result."


def test_stopping_one_live_session_cancels_only_its_model_request(tmp_path, monkeypatch):
    from harness import providers, settings

    class SharedProvider(providers.ModelProvider):
        def __init__(self):
            self.pending = {}; self.canceled = []; self.finish = threading.Event()
        def complete(self, system, messages, schemas, on_text=None):
            scope = self.current_request_scope()
            assert scope
            event = threading.Event(); self.pending[scope] = event
            deadline = time.monotonic() + 5
            while not event.is_set() and not self.finish.is_set() and time.monotonic() < deadline:
                time.sleep(.005)
            return SimpleNamespace(stop_reason="error" if event.is_set() else "end_turn",
                                   error_detail="canceled", text='{"summary":"Other session finished."}')
        def cancel_for(self, scope):
            def cancel():
                self.canceled.append(scope)
                if scope in self.pending: self.pending[scope].set()
            return cancel

    provider = SharedProvider()
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None: {
        "PROVIDER": "claude-agent-sdk", "MODEL": "fixture", "INTERACTIVE_SPEED": "standard"}.get(key, default))
    monkeypatch.setattr(providers, "make_provider", lambda *a, **k: provider)
    monkeypatch.setattr(providers, "provider_capabilities", lambda *a, **k: {"speed_tiers": ["standard"]})
    a, first = setup_runtime(tmp_path / "a")
    b, second = setup_runtime(tmp_path / "b")
    t1 = threading.Thread(target=first.tick); t2 = threading.Thread(target=second.tick)
    t1.start(); t2.start()
    try:
        deadline = time.monotonic() + 2
        while len(provider.pending) != 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(provider.pending) == 2
        a.stop()
        t1.join(2)
        assert not t1.is_alive() and t2.is_alive()
        assert len(set(provider.canceled)) == 1
        assert a.snapshot()["summary"] == ""
    finally:
        provider.finish.set(); t1.join(3); t2.join(3)
    assert b.snapshot()["summary"] == "Other session finished."
