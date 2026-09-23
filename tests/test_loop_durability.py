"""A host that cannot persist must not keep spending, and must not report success.

test_loop_gate.py proves the pre-action fence refuses to RUN the tool.  This file proves
the other half of the same promise: once the host has admitted it cannot write the
crash-recovery journal, the run stops paying the provider, says so in its own result, and
still hands back a transcript the next turn can legally continue from.
"""
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ScriptProvider  # noqa: E402

from harness.cli import make_harness  # noqa: E402
from harness.gate import Decision, Outcome  # noqa: E402
from harness.providers import Completion, ToolCall  # noqa: E402
from harness.tools import Tool  # noqa: E402


def _spy(nm, sink, ret="ok"):
    class Spy(Tool):
        name, tier = nm, "always"
        schema = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            sink.append(args)
            return ret
    return Spy()


class _AllowGate:
    def evaluate(self, tool_name, args, tool=None):
        return Decision(True, "test allow", risk="read")


class _AskGate:
    """Every call is a question, so work the run should not have done is visible as a
    prompt the person had to answer — the cost this fence exists to stop."""

    def evaluate(self, tool_name, args, tool=None):
        target = args.get("path") if isinstance(args, dict) else None
        return Decision(False, "test asks", needs_user=True, risk="write",
                        target=str(target or ""))

    def apply_outcome(self, outcome, tool_name, target, decision=None):
        pass


def _h(tmp_path, **kw):
    h = make_harness(str(tmp_path), provider="mock", project="durability_test",
                     embed="hash", gate=None, **kw)
    h.max_turns = 4
    h.durable_session_id = "durable-session"
    return h


def _calls(*specs):
    tcs = [ToolCall("c%d" % i, name, args) for i, (name, args) in enumerate(specs)]
    return [Completion(text="", tool_calls=tcs),
            Completion(text="", tool_calls=[ToolCall("c9", specs[0][0], specs[0][1])]),
            Completion(text="all done", stop_reason="end_turn")]


def _results(res):
    return [m for m in (res.messages or []) if m.get("role") == "tool"]


def _pairing_is_complete(res):
    """Every tool_use the provider emitted has exactly one paired result."""
    proposed = [tc.id for m in (res.messages or []) if m.get("role") == "assistant"
                for tc in (m.get("tool_calls") or [])]
    answered = [m.get("tool_call_id") for m in _results(res)]
    return sorted(proposed) == sorted(answered)


def _checkpoint_stub(sink, fails):
    """Record every checkpoint; fail exactly the ones `fails(state, detail)` selects."""
    def checkpoint(messages, run_id, turn, state, detail=None, terminal=False):
        ok = not fails(state, dict(detail or {}))
        sink.append((state, dict(detail or {}), bool(terminal), ok))
        return ok
    return checkpoint


# -- a mid-run pre-action checkpoint failure ends the run --------------------
def test_pre_action_checkpoint_failure_stops_model_spend_and_fails_the_run(tmp_path):
    """The initial checkpoint SUCCEEDS (the run was legitimately allowed to start); the
    fence then fails before the first tool.  The host cannot promise recovery any more,
    so it must not buy another turn, and must not call the model's cheerful "all done" a
    successful run."""
    ran = []
    ckpts = []
    h = _h(tmp_path)
    h._session_checkpoint = _checkpoint_stub(
        ckpts, lambda state, detail: state == "executing_tool")
    h.provider = _ScriptProvider(_calls(("write_note", {"text": "unsafe"})))
    h.registry.register(_spy("write_note", ran))

    res = h.run("durability", "do it", consolidate=False)

    assert not ran, "a tool ran after its recovery fence was refused"
    assert h.provider.calls == 1, (
        "the model was called again after the host admitted it cannot persist: %d calls"
        % h.provider.calls)
    assert res.success is False, "a host that cannot persist reported a successful run"
    assert "durab" in res.error.lower() or "persist" in res.error.lower(), res.error
    assert res.stop_reason == "error"
    out = _results(res)[0]["content"]
    assert "durability checkpoint failed" in out
    assert _pairing_is_complete(res), "the refused call was left unpaired"


def test_checkpoint_failure_keeps_completed_effects_and_pairs_the_rest(tmp_path):
    """Three top-level calls, the fence fails before the second.  The first call's real
    effect stands (it happened, and pretending otherwise is its own lie), the second and
    third never run, and all three still carry a result so the thread stays continuable."""
    first, second, third = [], [], []
    ckpts = []
    seen = {"executing": 0}

    def fails(state, detail):
        if state != "executing_tool" or detail.get("inner_complete"):
            return False
        seen["executing"] += 1
        return seen["executing"] >= 2

    h = _h(tmp_path)
    h._session_checkpoint = _checkpoint_stub(ckpts, fails)
    h.provider = _ScriptProvider(_calls(("t_first", {}), ("t_second", {}), ("t_third", {})))
    h.registry.register(_spy("t_first", first))
    h.registry.register(_spy("t_second", second))
    h.registry.register(_spy("t_third", third))

    res = h.run("durability", "do it", consolidate=False)

    assert first, "an already-completed effect was rolled back by the durability stop"
    assert not second and not third, "tools ran past the failed recovery fence"
    assert res.success is False and res.error
    assert _pairing_is_complete(res), "unpaired tool_use blocks would 400 the next turn"
    assert h.provider.calls == 1


def test_inner_execute_code_checkpoint_failure_stops_the_run(tmp_path):
    """The broker's inner calls pass through the same fence.  A refusal there is a HOST
    durability fault too: the parent's result may come back looking ordinary, so the run
    has to stop on the fence rather than on how the script chose to report it."""
    ckpts = []
    h = _h(tmp_path, exec_code=True)
    h.gate = _AllowGate()
    h._session_checkpoint = _checkpoint_stub(
        ckpts, lambda state, detail: bool(detail.get("internal"))
        and not detail.get("inner_complete"))
    target = tmp_path / "inner.txt"
    h.provider = _ScriptProvider(_calls(("execute_code", {"code":
        'print(tool("write_file", path=%s, content="landed"))' % json.dumps(str(target))})))

    res = h.run("durability", "do it", consolidate=False)

    assert not target.exists(), "an inner tool ran without its recovery fence"
    assert h.provider.calls == 1, (
        "the model kept being called after an inner durability fault: %d" % h.provider.calls)
    assert res.success is False
    assert "durab" in res.error.lower() or "persist" in res.error.lower(), res.error
    assert _pairing_is_complete(res)


def test_terminal_checkpoint_failure_is_reported_as_a_failed_run(tmp_path):
    """Nothing went wrong until the very last write.  The answer is real, but the host
    could not persist the thread it just claimed to have finished, so the receipt says
    error rather than completed."""
    ckpts = []
    h = _h(tmp_path)
    h._session_checkpoint = _checkpoint_stub(
        ckpts, lambda state, detail: state == "terminal")
    h.provider = _ScriptProvider([Completion(text="all done", stop_reason="end_turn")])

    res = h.run("durability", "just answer", consolidate=False)

    assert res.answer == "all done", "the model's real answer was discarded"
    assert res.success is False, "an unsaved transcript was reported as a completed run"
    assert res.host_error is True
    # Wording is asserted precisely in the real-writer test below; here only that the
    # failure names the save it lost, rather than being an unattributed error string.
    assert "save failed" in res.error.lower(), res.error
    assert res.stop_reason == "error"


def test_inner_durability_stop_latches_against_a_healed_store(tmp_path):
    """Only the FIRST inner checkpoint fails; the store then heals.

    The run has already recorded that it stopped without executing that call, so a later
    inner call from the same script must not be prepared, approved, audited or executed.
    A healed store is not permission to resume work the run said it had abandoned — and
    the person must not be asked to approve it.
    """
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    internal = {"seen": 0}

    def fails(state, detail):
        if not detail.get("internal") or detail.get("inner_complete"):
            return False
        internal["seen"] += 1
        return internal["seen"] == 1          # heals immediately afterwards

    prompts = []

    def approver(tool_name, args, decision):
        prompts.append(tool_name)
        return Outcome.ALLOW_ONCE.value

    h = _h(tmp_path, exec_code=True)
    h.gate = _AskGate()
    h.approve = approver
    h._session_checkpoint = _checkpoint_stub([], fails)
    h.provider = _ScriptProvider(_calls(("execute_code", {"code":
        'print("FIRST:", tool("write_file", path=%s, content="A"))\n'
        'print("SECOND:", tool("write_file", path=%s, content="B"))'
        % (json.dumps(str(first)), json.dumps(str(second)))})))

    res = h.run("durability", "do it", consolidate=False)

    out = _results(res)[0]["content"]
    assert not first.exists(), "the fenced inner call executed anyway"
    assert not second.exists(), (
        "a later inner call executed for real after the run reported it had stopped: %s"
        % out)
    assert prompts.count("write_file") == 1, (
        "the user was asked to approve work the stopped run had already refused: %r"
        % (prompts,))
    answered = [ln for ln in out.splitlines() if ln.startswith("SECOND:")]
    assert answered and "DENIED" in answered[0], out
    assert ("durab" in answered[0].lower() or "persist" in answered[0].lower()), out
    assert h.provider.calls == 1, (
        "the model was called again after the durability stop: %d" % h.provider.calls)
    assert res.success is False
    assert _pairing_is_complete(res), "the parent execute_code call was left unpaired"


def test_terminal_save_failure_does_not_claim_the_thread_is_lost(tmp_path, monkeypatch):
    """The REAL checkpoint writer runs for every state except the terminal one.

    Earlier checkpoints are therefore genuinely on disk — `sessions.checkpoint` merges into
    the existing file — so the thread loads fine minus the last turn.  The error has to say
    that, and must not tell the person to blindly rerun a turn whose effects it cannot see.
    """
    from harness import sessions

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    h = _h(tmp_path)
    real = h._session_checkpoint

    def only_terminal_fails(messages, run_id, turn, state, detail=None, terminal=False):
        if terminal or state == "terminal":
            return False
        return real(messages, run_id, turn, state, detail, terminal)

    h._session_checkpoint = only_terminal_fails
    h.provider = _ScriptProvider([Completion(text="all done", stop_reason="end_turn")])

    res = h.run("durability", "remember this request", consolidate=False)

    loaded = sessions.load_checked("durable-session")
    assert loaded["status"] == "ok", loaded
    texts = [str(m.get("content")) for m in loaded["session"]["messages"]]
    assert any("remember this request" in t for t in texts), (
        "no earlier checkpoint survived, so the claim under test is untested: %r" % texts)
    assert "cannot be resumed" not in res.error, (
        "the thread IS on disk up to the previous checkpoint: %s" % res.error)
    low = res.error.lower()
    assert "final" in low and ("save" in low or "persist" in low), res.error
    assert "check" in low, (
        "the error must send the person to the saved state, not to a blind retry: %s"
        % res.error)
    assert res.host_error is True
    assert res.stop_reason == "error" and res.success is False


def test_mid_run_durability_fault_is_recorded_as_a_host_error(tmp_path):
    """The store breaks before a tool and heals before finalization, so the terminal write
    succeeds.  The run still failed because of the HOST, and every surface that routes on
    `host_error` must see that rather than reading it as a model or tool failure."""
    ran = []
    seen = {"executing": 0}

    def fails(state, detail):
        if state != "executing_tool":
            return False
        seen["executing"] += 1
        return seen["executing"] == 1         # the terminal write later succeeds

    h = _h(tmp_path)
    h._session_checkpoint = _checkpoint_stub([], fails)
    h.provider = _ScriptProvider(_calls(("write_note", {"text": "unsafe"})))
    h.registry.register(_spy("write_note", ran))

    res = h.run("durability", "do it", consolidate=False)

    assert not ran, "a tool ran after its recovery fence was refused"
    assert res.host_error is True, (
        "a store outage was reported as an ordinary model/tool error")
    assert res.stop_reason == "error" and res.success is False


if __name__ == "__main__":
    from _util import run_module
    run_module(globals(), "loop_durability")
