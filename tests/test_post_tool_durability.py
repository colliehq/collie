"""A receipt the host could not write is still a host failure — and the tool already ran.

test_loop_durability.py covers the PRE-action fence: the call never starts, so the honest
report is "nothing happened".  This file covers the other side of the same tool: the fence
was written, the tool was dispatched for real, and only the completed-result checkpoint
failed.  Nothing can un-run that call, so the run must stop where it is, keep the output it
really got, and describe the call as it actually ended — completed-but-unsaved when it
returned, started-and-maybe-partial when it raised — never as skipped.
"""
import json
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ScriptProvider  # noqa: E402

from harness.cli import make_harness  # noqa: E402
from harness.gate import Decision  # noqa: E402
from harness.providers import Completion, ToolCall  # noqa: E402
from harness.recorder import provider_wait_state, run_stop_reason  # noqa: E402
from harness.tools import Tool  # noqa: E402


class _AllowGate:
    def evaluate(self, tool_name, args, tool=None):
        return Decision(True, "test allow", risk="read")


class _DenyGate:
    def evaluate(self, tool_name, args, tool=None):
        return Decision(False, "test refuses this call", risk="write")

    def apply_outcome(self, outcome, tool_name, target, decision=None):
        pass


def _h(tmp_path, **kw):
    h = make_harness(str(tmp_path), provider="mock", project="post_tool_test",
                     embed="hash", gate=None, **kw)
    h.max_turns = 4
    h.durable_session_id = "durable-session"
    return h


def _script(first, second):
    """Turn 0 makes `first`; a run that keeps going makes the DISTINCT call `second`,
    so a second landed file is visible proof the host kept working after the fault."""
    return _batch_script([first], second)


def _batch_script(batch, later):
    """One assistant message proposing every call in `batch`, then a distinct later call."""
    return [Completion(text="", tool_calls=[ToolCall("c%d" % i, *spec)
                                            for i, spec in enumerate(batch)]),
            Completion(text="", tool_calls=[ToolCall("c9", *later)]),
            Completion(text="all done", stop_reason="end_turn")]


def _raiser(nm, ran):
    """A tool that enters run() and raises before touching anything: dispatched, no effect."""
    class Boom(Tool):
        name, tier = nm, "always"
        schema = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            ran.append(args)
            raise RuntimeError("refused by the outside world before any change")
    return Boom()


def _write(path, content):
    return ("write_file", {"path": str(path), "content": content})


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


def _post_tool_fails_once(counter):
    """One injected failure on a completed top-level tool result; the store then heals."""
    def fails(state, detail):
        if state != "tool_complete":
            return False
        counter["n"] += 1
        return counter["n"] == 1
    return fails


def _assert_says_it_ran(error, tool="write_file"):
    """The call returned normally, so the report may say so — and must not send the
    reader off to undo or repeat an effect the host knows completed."""
    low = (error or "").lower()
    assert tool in low, error
    assert "completed" in low, "the report does not say the call finished: %s" % error
    assert ("not be saved" in low or "could not save" in low or "persist" in low), error
    assert "do not replay" in low and "roll its effect back" in low, error
    assert "not executed" not in low, (
        "a completed effect was described as never having run: %s" % error)
    assert "recovery inspection" not in low, (
        "a fenced, completed call was given recovery uncertainty it does not have: %s"
        % error)


# -- top-level: the effect landed, the receipt did not ----------------------------
def test_post_tool_checkpoint_failure_stops_the_run_and_keeps_the_real_output(tmp_path):
    """A real file write completes, then the ONE injected failure hits its completed-result
    checkpoint and the store heals.  The tool cannot be un-run, so: no further model turn,
    no second call, the file stays exactly as written, and the transcript keeps the output
    the tool really returned."""
    landed, later = tmp_path / "landed.txt", tmp_path / "later.txt"
    ckpts = []
    h = _h(tmp_path)
    h._session_checkpoint = _checkpoint_stub(ckpts, _post_tool_fails_once({"n": 0}))
    h.provider = _ScriptProvider(_script(_write(landed, "landed"), _write(later, "later")))

    res = h.run("post_tool", "do it", consolidate=False)

    assert landed.read_text() == "landed", "a completed write was rolled back or replayed"
    assert not later.exists(), "the run kept working after it could not save a result"
    assert h.provider.calls == 1, (
        "the model was called again after a completed result could not be saved: %d"
        % h.provider.calls)
    out = _results(res)[0]["content"]
    assert out.startswith("wrote ") and "landed.txt" in out, (
        "the tool's real output was replaced or discarded: %r" % out)
    assert _pairing_is_complete(res), "the executed call was left unpaired"
    assert res.host_error is True, "a store outage was reported as a model/tool error"
    assert res.stop_reason == "error" and res.success is False
    _assert_says_it_ran(res.error)
    states = [c[0] for c in ckpts]
    assert states.count("executing_tool") == 1 and states.count("tool_complete") == 1, states
    assert states[-1] == "terminal", (
        "finalization did not reach an ordinary terminal save: %s" % states)


def test_post_tool_failure_after_a_raising_tool_does_not_promise_an_effect(tmp_path):
    """Dispatch is not completion: this tool enters run() and raises before changing
    anything, and the SAME lost receipt follows.  The host knows only that the call was
    started, so the report must say that much and no more — no claim that an effect
    landed and must be preserved — while still keeping the real ERROR output and refusing
    a blind replay."""
    ran = []
    h = _h(tmp_path)
    h._session_checkpoint = _checkpoint_stub([], _post_tool_fails_once({"n": 0}))
    h.registry.register(_raiser("explode", ran))
    h.provider = _ScriptProvider(_script(("explode", {}),
                                         _write(tmp_path / "later.txt", "later")))

    res = h.run("post_tool", "do it", consolidate=False)

    assert ran, "the tool under test never reached run(), so nothing was dispatched"
    assert not (tmp_path / "later.txt").exists(), (
        "the run kept working after it could not save a result")
    assert h.provider.calls == 1, (
        "the model was called again after a lost receipt: %d" % h.provider.calls)
    out = _results(res)[0]["content"]
    assert out.startswith("ERROR: tool explode failed"), (
        "the tool's real failure output was replaced or discarded: %r" % out)
    assert _pairing_is_complete(res), "the attempted call was left unpaired"
    assert res.host_error is True and res.success is False
    low = res.error.lower()
    assert "explode" in low and "started" in low, res.error
    assert "may have taken effect" in low, (
        "a call that raised before doing anything was not described as uncertain: %s"
        % res.error)
    assert "did run, so do not replay" not in low, (
        "a tool that raised before writing was reported as a landed effect: %s" % res.error)
    assert "not executed" not in low, (
        "a call that really was dispatched was reported as never started: %s" % res.error)
    assert "blindly" in low and "check the session store" in low, (
        "the report dropped its no-blind-retry guidance: %s" % res.error)


def test_multi_call_batch_stops_after_the_lost_receipt_and_closes_the_rest_honestly(tmp_path):
    """Three calls in ONE assistant message.  The first really writes its file, its
    completed-result checkpoint fails, and the store heals.  Calls two and three were
    never started, so they must not run and must not be closed with the running call's
    unknown-effect wording — the thread has to state exactly which of the three happened."""
    first, second, third = (tmp_path / "one.txt", tmp_path / "two.txt",
                            tmp_path / "three.txt")
    ckpts = []
    h = _h(tmp_path)
    h._session_checkpoint = _checkpoint_stub(ckpts, _post_tool_fails_once({"n": 0}))
    h.provider = _ScriptProvider(_batch_script(
        [_write(first, "1"), _write(second, "2"), _write(third, "3")],
        _write(tmp_path / "later.txt", "later")))

    res = h.run("post_tool", "do it", consolidate=False)

    assert first.read_text() == "1", "the completed write was rolled back or replayed"
    assert not second.exists() and not third.exists(), (
        "later calls in the batch ran after the host lost the first one's receipt")
    assert not (tmp_path / "later.txt").exists() and h.provider.calls == 1
    assert _pairing_is_complete(res), "unpaired tool_use blocks would 400 the next turn"
    by_id = {m["tool_call_id"]: str(m["content"]) for m in _results(res)}
    assert by_id["c0"].startswith("wrote "), (
        "the executed call's real output was lost: %r" % by_id["c0"])
    for cid in ("c1", "c2"):
        assert by_id[cid].startswith("CANCELED: run stopped before execution"), (
            "a call that never started was closed as %r" % by_id[cid])
        assert "UNKNOWN" not in by_id[cid] and "INTERRUPTED" not in by_id[cid], (
            "a never-started call was given the running call's uncertainty: %r"
            % by_id[cid])
    assert [c[0] for c in ckpts].count("executing_tool") == 1, (
        "a later call in the batch was still fenced for execution: %s"
        % [c[0] for c in ckpts])
    assert res.host_error is True and res.success is False
    _assert_says_it_ran(res.error)


def test_post_tool_failure_for_a_call_that_never_ran_does_not_claim_an_effect(tmp_path):
    """The same lost write, but the call was refused by the gate and never dispatched.
    The honest half of the message is the other one: nothing executed."""
    h = _h(tmp_path)
    h.gate = _DenyGate()
    h._session_checkpoint = _checkpoint_stub([], _post_tool_fails_once({"n": 0}))
    h.provider = _ScriptProvider(_script(_write(tmp_path / "a.txt", "a"),
                                         _write(tmp_path / "b.txt", "b")))

    res = h.run("post_tool", "do it", consolidate=False)

    assert not (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()
    assert h.provider.calls == 1
    assert res.host_error is True and res.success is False
    low = res.error.lower()
    assert "not executed" in low, res.error
    assert "did run" not in low, "a denied call was reported as having executed: %s" % res.error


# -- the same fault against the real session writer -------------------------------
def test_healed_final_save_keeps_the_tool_output_and_invents_no_uncertainty(tmp_path,
                                                                            monkeypatch):
    """Only the post-tool write fails; the REAL writer handles everything else, so the
    terminal save lands.  The saved thread must contain the executed call's real result,
    and must not be fenced for recovery — the host knows exactly what happened."""
    from harness import sessions

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    landed = tmp_path / "landed.txt"
    h = _h(tmp_path)
    real = h._session_checkpoint
    counter = {"n": 0}

    def only_post_tool_fails(messages, run_id, turn, state, detail=None, terminal=False):
        if state == "tool_complete":
            counter["n"] += 1
            if counter["n"] == 1:
                return False
        return real(messages, run_id, turn, state, detail, terminal)

    h._session_checkpoint = only_post_tool_fails
    h.provider = _ScriptProvider(_script(_write(landed, "landed"),
                                         _write(tmp_path / "later.txt", "later")))

    res = h.run("post_tool", "do it", consolidate=False)

    loaded = sessions.load_checked("durable-session")
    assert loaded["status"] == "ok", loaded
    saved = loaded["session"]
    assert not saved.get("active_run"), (
        "a run whose final save succeeded left a recovery fence behind: %r"
        % (saved.get("active_run"),))
    tool_msgs = [m for m in saved["messages"] if m.get("role") == "tool"]
    assert any(str(m.get("content", "")).startswith("wrote ") for m in tool_msgs), (
        "the executed tool's output did not survive to the saved thread: %r" % tool_msgs)
    assert not any("RECOVERY" in str(m.get("content", "")) for m in saved["messages"]), (
        "the resume view invented uncertainty about a call that finished")
    assert landed.read_text() == "landed"
    assert res.host_error is True and res.success is False
    assert res.stop_reason == "error", (
        "a healed store turned a stopped run back into an ordinary one")
    _assert_says_it_ran(res.error)
    assert "final transcript save failed" not in res.error, (
        "the final save succeeded; only the mid-run receipt was lost: %s" % res.error)
    low = res.error.lower()
    assert "final save" in low and "latest results" in low, (
        "the store is healthy and holds everything, but the error still reads as if "
        "this run's results were lost: %s" % res.error)
    assert "no recovery fence" in low, (
        "nothing is left to reconcile, yet the report does not say so: %s" % res.error)
    assert ".;" not in res.error, (
        "the appended explanation collides with the fault's punctuation: %s" % res.error)


def test_when_the_final_save_also_fails_the_pre_action_fence_stays_on_disk(tmp_path,
                                                                           monkeypatch):
    """The store never heals: both the post-tool write and the terminal write fail.  The
    last thing that DID reach disk is this call's pre-action fence, and it must still be
    there, naming the call whose effect now has to be reconciled by hand."""
    from harness import sessions

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    landed = tmp_path / "landed.txt"
    h = _h(tmp_path)
    real = h._session_checkpoint

    def post_tool_and_terminal_fail(messages, run_id, turn, state, detail=None,
                                    terminal=False):
        if state in ("tool_complete", "terminal") or terminal:
            return False
        return real(messages, run_id, turn, state, detail, terminal)

    h._session_checkpoint = post_tool_and_terminal_fail
    h.provider = _ScriptProvider(_script(_write(landed, "landed"),
                                         _write(tmp_path / "later.txt", "later")))

    res = h.run("post_tool", "do it", consolidate=False)

    loaded = sessions.load_checked("durable-session")
    assert loaded["status"] == "ok", loaded
    active = loaded["session"].get("active_run") or {}
    assert active.get("state") == "executing_tool", (
        "the pre-action recovery fence was lost or overwritten: %r" % (active,))
    assert active.get("detail", {}).get("tool_call_id") == "c0", active
    assert active.get("detail", {}).get("tool_name") == "write_file", active
    assert landed.read_text() == "landed", "the landed effect was disturbed"
    assert res.host_error is True and res.success is False
    _assert_says_it_ran(res.error)
    assert "final transcript save failed" in res.error, (
        "the lost terminal save was not reported: %s" % res.error)
    low = res.error.lower()
    assert "latest results" not in low and "no recovery fence" not in low, (
        "the store never healed, but the run claims its results are safely stored: %s"
        % res.error)
    # In-memory the run still hands back the real result it got, unpaired by nothing.
    assert _results(res)[0]["content"].startswith("wrote ")
    assert _pairing_is_complete(res)


# -- inner execute_code calls go through the same boundary ------------------------
def test_inner_post_tool_failure_stops_a_second_distinct_inner_call(tmp_path):
    """Inside one execute_code script: the first inner write lands for real, its completed
    -result checkpoint fails once, then the store heals.  A healed store is not permission
    to run the script's SECOND, different call — but the first call's output is real
    evidence and has to come back."""
    first, second = tmp_path / "inner_first.txt", tmp_path / "inner_second.txt"
    counter = {"n": 0}

    def fails(state, detail):
        if not (detail.get("internal") and detail.get("inner_complete")):
            return False
        counter["n"] += 1
        return counter["n"] == 1

    h = _h(tmp_path, exec_code=True)
    h.gate = _AllowGate()
    h._session_checkpoint = _checkpoint_stub([], fails)
    h.provider = _ScriptProvider(_script(
        ("execute_code", {"code":
            'print("FIRST:", tool("write_file", path=%s, content="A"))\n'
            'print("SECOND:", tool("write_file", path=%s, content="B"))'
            % (json.dumps(str(first)), json.dumps(str(second)))}),
        _write(tmp_path / "later.txt", "later")))

    res = h.run("post_tool", "do it", consolidate=False)

    out = _results(res)[0]["content"]
    assert first.read_text() == "A", "the inner call that really ran was rolled back: %s" % out
    assert not second.exists(), (
        "a second inner call ran after the host lost the first one's receipt: %s" % out)
    assert not (tmp_path / "later.txt").exists(), "a later turn's tool executed"
    assert h.provider.calls == 1, (
        "the model was called again after the inner durability stop: %d" % h.provider.calls)
    answered = {ln.split(":", 1)[0]: ln for ln in out.splitlines()
                if ln.startswith(("FIRST:", "SECOND:"))}
    assert "wrote " in answered.get("FIRST", ""), (
        "the completed inner call's real output was lost: %s" % out)
    denial = answered.get("SECOND", "")
    assert "DENIED" in denial, out
    # This line ends up in the script's stdout, the parent result and any resume view,
    # so it must not contradict the top-level report of a call that did execute.
    assert "not be fenced" not in denial and "cannot be fenced" not in denial, (
        "a post-execution stop was explained as a call that never got a fence: %s" % denial)
    assert ("durab" in denial.lower() or "persist" in denial.lower()), denial
    assert "no further tool will be started" in denial, denial
    assert res.host_error is True and res.success is False
    _assert_says_it_ran(res.error)
    assert _pairing_is_complete(res), "the parent execute_code call was left unpaired"


# -- turn 0: the same outage, classified the same way -----------------------------
def test_initial_request_checkpoint_failure_is_a_host_error(tmp_path):
    """The store is already down when the accepted request is journaled.  It is the same
    host failure the mid-run and terminal paths record; only the timing differs, so the
    field surfaces route on must not differ either."""
    h = _h(tmp_path)
    h._session_checkpoint = _checkpoint_stub(
        [], lambda state, detail: state == "turn_boundary")
    h.provider = _ScriptProvider([Completion(text="all done", stop_reason="end_turn")])

    res = h.run("post_tool", "do it", consolidate=False)

    assert h.provider.calls == 0, "the model was called despite an unpersisted request"
    assert "could not be persisted" in res.error, res.error
    assert res.host_error is True, (
        "the same store outage is a host error mid-run but not at turn 0")
    assert res.stop_reason == "error" and res.success is False
    # Classification is all that changed: a leftover quota reset stops being read as a
    # wait, and cancellation still outranks the error it was already outranking.
    res.retry_at = int(time.time()) + 600
    assert provider_wait_state(res) is None, "an unwritable store was dressed up as a wait"
    res.canceled = True
    assert run_stop_reason(res) == "canceled", "host classification displaced cancellation"


if __name__ == "__main__":
    from _util import run_module
    run_module(globals(), "post_tool_durability")
