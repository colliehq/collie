"""Cancel one physical request without canceling another caller of its provider."""
import threading
from concurrent.futures import ThreadPoolExecutor

from harness.cancellation import complete
from harness.providers import Completion, ModelProvider


class ScopedProvider(ModelProvider):
    def __init__(self):
        self.active = {}
        self.seen = {}
        self.lock = threading.Lock()
        self.ready = threading.Barrier(3)

    def complete(self, system, messages, schemas, on_text=None):
        scope = self.current_request_scope()
        done = threading.Event()
        with self.lock:
            self.active[scope] = done
            self.seen[scope] = self.current_request_authority()
        self.ready.wait(timeout=3)
        released = done.wait(timeout=3)
        with self.lock:
            self.active.pop(scope, None)
        return Completion(text=scope if released else "timed out")

    def cancel_for(self, scope):
        with self.lock:
            event = self.active.get(scope)
        if event:
            event.set()


def test_cancel_is_scoped_and_preserves_each_callers_budget_callbacks():
    provider = ScopedProvider()
    stop_a, stop_b = threading.Event(), threading.Event()
    gate_a, settled_a, gate_b, settled_b = (object() for _ in range(4))

    def call(scope, stop, gate, settled):
        with provider.request_authority(gate, settled, request_scope=scope):
            return complete(provider, "", [], [], cancelled=stop.is_set)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(call, "a", stop_a, gate_a, settled_a)
        b = pool.submit(call, "b", stop_b, gate_b, settled_b)
        provider.ready.wait(timeout=3)
        stop_a.set()
        assert a.result(timeout=1).text == "a"
        assert not b.done()
        stop_b.set()
        assert b.result(timeout=1).text == "b"
    assert provider.seen == {"a": (gate_a, settled_a), "b": (gate_b, settled_b)}
    assert provider.current_request_authority() == (None, None)
    assert provider.current_request_scope() == ""


def test_provider_wide_cancel_is_never_used_as_a_scoped_fallback():
    class LegacyProvider:
        def cancel_current(self):
            raise AssertionError("would cancel unrelated work")

        def complete(self, *a, **kw):
            return Completion(text="normal transport behavior")

    assert complete(LegacyProvider(), "", [], [], cancelled=lambda: True).text == \
        "normal transport behavior"


def test_cancel_received_during_completion_keeps_partial_text_and_does_not_execute_tools(
        monkeypatch, tmp_path):
    from harness import cli
    from harness.providers import ToolCall
    from _util import _ScriptProvider

    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    h = cli.make_harness(str(tmp_path), provider="mock", embed="hash")
    stop = threading.Event()
    h.cancelled = stop.is_set

    class Provider(_ScriptProvider):
        def complete(self, *a, **kw):
            stop.set()
            return Completion(text="Inspection found the root cause.", tool_calls=[
                ToolCall("late-write", "write_file", {"path": "forbidden.txt", "content": "late"})])

    h.provider = Provider([])
    try:
        result = h.run("cancel", "inspect the project", consolidate=False)
        assert result.stop_reason == "canceled" and result.canceled
        assert "Inspection found the root cause." in result.answer
        assert result.tool_calls == 0 and not (tmp_path / "forbidden.txt").exists()
        assert result.model_calls == 1
    finally:
        h.memory.close()
        h.recorder.close()
