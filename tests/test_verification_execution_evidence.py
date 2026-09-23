"""Only a call the host actually DISPATCHED may become verification evidence.

`_account_tool_outcome` used to run its reproduction accounting off the result TEXT of every
call the loop handled, dispatched or not.  A `bash python -m pytest -q` the permission gate
refused came back as `DENIED: ...`, which `_repro_failed` reads as "did not start with ERROR
or [exit", i.e. as a PASS — so an edit followed by a refused test run finished `verified=True`
and satisfied `verification="required"` with `require_assert` on.  The same shape reaches the
accounting from a lifecycle-hook rejection, a revoked `execute_code` parent, a malformed call
and an unknown tool: host explanations about a call that never ran, read as observations of
the workspace.

These tests pin the boundary rather than the wording: what counts is whether `Tool.run` was
reached, which the host knows at the execution boundary and never has to infer from text.
Controls below keep the other direction honest — a check that really runs still verifies, a
check that really fails still blocks, and a reread is still not a check.
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from _util import _ScriptProvider  # noqa: E402

from harness import cli  # noqa: E402
from harness.gate import Gate, Mode, Outcome  # noqa: E402
from harness.hooks import HookResult  # noqa: E402
from harness.providers import Completion, ToolCall  # noqa: E402
from harness.tools import Tool  # noqa: E402

CHECK = "python -m pytest -q"


def _harness(tmp_path, monkeypatch, *, gate=None, required=True, project="verif-evidence"):
    """A real Harness over a tiny workspace, with state kept inside tmp_path."""
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    h = cli.make_harness(str(ws), provider="mock", project=project, embed="hash",
                         gate=gate(ws) if callable(gate) else gate)
    h.max_turns = 8
    if required:
        cli.configure_run_options(h, verification="required")
    return h


def _bash_spy(h, result="3 passed in 0.1s"):
    """Replace the bash tool's run with a recorder, so 'did it dispatch?' is observable."""
    ran = []

    def run(args, ctx):
        ran.append(args.get("command", ""))
        return result(args) if callable(result) else result
    h.registry.get("bash").run = run
    return ran


def _edit(cid="e1", old="VALUE = 1", new="VALUE = 2"):
    return Completion(tool_calls=[ToolCall(cid, "edit_file", {
        "path": "app.py", "old_string": old, "new_string": new})])


def _bash(cid, command=CHECK):
    return Completion(tool_calls=[ToolCall(cid, "bash", {"command": command})])


def _run(h, script, task="Bump VALUE to 2."):
    """Run the scripted conversation, returning (result, hints seen on each request)."""
    hints = []

    def watch(messages):
        hints.append([m.get("content", "") for m in messages
                      if m.get("kind") == "verification_state"])

    items = []
    for item in script:
        # A scripted item may be a Completion or a callable(messages) -> Completion, so a test
        # can act (e.g. request cancellation) at a precise point in the conversation.
        items.append((lambda it: lambda m: (watch(m), it(m) if callable(it) else it)[1])(item))
    h.provider = _ScriptProvider(items)
    try:
        return h.run("evidence", task, consolidate=False), hints
    finally:
        h.memory.close()
        h.recorder.close()


def _denying_gate(answer):
    """An interactive gate whose scripted human refuses bash/run_in_env."""
    def build(cwd):
        return Gate(cwd, mode=Mode.INTERACTIVE)

    def approve(name, args, decision):
        return (answer if name in ("bash", "run_in_env") else Outcome.ALLOW_ONCE).value
    return build, approve


# -- the defect: a refused check is not a check ------------------------------------------
@pytest.mark.parametrize("answer", [Outcome.REJECT_ONCE, Outcome.REJECT_ALWAYS])
def test_denied_check_cannot_satisfy_required_verification(tmp_path, monkeypatch, answer):
    build, approve = _denying_gate(answer)
    h = _harness(tmp_path, monkeypatch, gate=build)
    h.approve = approve
    ran = _bash_spy(h)
    res, _ = _run(h, [_edit(), _bash("b1"), _bash("b2"),
                      Completion(text="Bumped VALUE; I could not run the suite."),
                      Completion(text="Bumped VALUE; I could not run the suite."),
                      Completion(text="Bumped VALUE; I could not run the suite.")])

    assert ran == [], "the refused command must never reach Tool.run"
    assert res.denied_calls >= 1, "the gate must still have refused the call"
    assert res.verified is False, "a call that never ran verified the edit"
    assert res.error and "verification required" in res.error, (
        "Required accepted a finish with no executed check: %r" % res.error)


def test_denied_check_does_not_fabricate_a_pass_over_a_real_failure(tmp_path, monkeypatch):
    """The real check ran and FAILED; a later refused rerun must not overwrite that."""
    # Allow the first bash, refuse the rerun, at the gate's own boundary.
    seen = {"bash": 0}

    def approver(name, args, decision):
        if name != "bash":
            return Outcome.ALLOW_ONCE.value
        seen["bash"] += 1
        return (Outcome.ALLOW_ONCE if seen["bash"] == 1 else Outcome.REJECT_ALWAYS).value

    h = _harness(tmp_path, monkeypatch, gate=lambda ws: Gate(ws, mode=Mode.INTERACTIVE))
    h.approve = approver
    ran = _bash_spy(h, result="[exit 1]\n1 failed, 0 passed")
    res, _ = _run(h, [_edit(), _bash("b1"), _bash("b2"),
                      Completion(text="Done, could not rerun."),
                      Completion(text="Done, could not rerun."),
                      Completion(text="Done, could not rerun.")])

    assert ran == [CHECK], "exactly one dispatch was authorized"
    assert res.verified is False, "a refused rerun turned a real failure into a pass"
    assert res.error and "verification required" in res.error


def test_denied_check_does_not_invalidate_a_real_passing_check(tmp_path, monkeypatch):
    """The other direction: a refused call is not evidence of failure either."""
    seen = {"bash": 0}

    def approver(name, args, decision):
        if name != "bash":
            return Outcome.ALLOW_ONCE.value
        seen["bash"] += 1
        return (Outcome.ALLOW_ONCE if seen["bash"] == 1 else Outcome.REJECT_ALWAYS).value

    h = _harness(tmp_path, monkeypatch, gate=lambda ws: Gate(ws, mode=Mode.INTERACTIVE))
    h.approve = approver
    ran = _bash_spy(h, result="1 passed in 0.1s")
    res, _ = _run(h, [_edit(),
                      _bash("b1", "python -m pytest -q -k value"),
                      _bash("b2"),
                      Completion(text="Bumped VALUE; the focused test passes.")])

    assert ran == ["python -m pytest -q -k value"]
    assert res.verified is True, "a refused extra call erased a real passing check"
    assert not res.error


def test_unexecuted_call_is_not_advertised_as_ran_and_failed(tmp_path, monkeypatch):
    """The host must not describe a call it refused as output the model can go read."""
    build, approve = _denying_gate(Outcome.REJECT_ALWAYS)
    h = _harness(tmp_path, monkeypatch, gate=build)
    h.approve = approve
    _bash_spy(h)
    res, hints = _run(h, [_edit(), _bash("b1"),
                          Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                          Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE.")])

    after = [body for req in hints[2:] for body in req]
    assert after, "the edit is still unverified, so the state hint must keep riding requests"
    for body in after:
        assert "NOT RUN yet" in body, "hint claimed a state the host never observed: %r" % body
        assert "DID NOT PASS" not in body
        assert "ran without asserting" not in body
    assert res.verified is False


def test_hook_denied_check_is_not_verification(tmp_path, monkeypatch):
    """A PreToolUse rejection is the same non-dispatch as a permission refusal."""
    class Hooks:
        pending = ()

        def dispatch(self, event, payload, subject=""):
            if event == "PreToolUse" and payload.get("tool_name") == "bash":
                return HookResult(allowed=False, reason="policy forbids the test runner")
            return HookResult(allowed=True)

    h = _harness(tmp_path, monkeypatch)
    h.hooks = Hooks()
    ran = _bash_spy(h)
    res, _ = _run(h, [_edit(), _bash("b1"), _bash("b2"),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                      Completion(text="Bumped VALUE.")])

    assert ran == []
    assert res.verified is False
    assert res.error and "verification required" in res.error


def test_execute_code_inner_denied_check_is_not_verification(tmp_path, monkeypatch):
    """Inner RPC calls share the accounting, so they share the dispatch requirement."""
    class FakeExec(Tool):
        name, tier, risk = "execute_code", "always", "external"
        schema = {"type": "object", "properties": {"code": {"type": "string"}}}

        def run(self, args, ctx):
            return str(ctx.tool_broker("bash", {"command": CHECK}))

    build, approve = _denying_gate(Outcome.REJECT_ALWAYS)
    h = _harness(tmp_path, monkeypatch, gate=build)
    h.approve = approve
    h.registry.register(FakeExec())
    ran = _bash_spy(h)
    res, _ = _run(h, [_edit(),
                      Completion(tool_calls=[ToolCall("x1", "execute_code", {"code": "run"})]),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE.")])

    assert ran == [], "the inner call was refused and must not have dispatched"
    assert res.verified is False, "an inner refused check verified the edit"
    assert res.error and "verification required" in res.error


def test_execute_code_inner_executed_check_still_verifies(tmp_path, monkeypatch):
    """Control for the test above: a dispatched inner check is real evidence, as before."""
    class FakeExec(Tool):
        name, tier, risk = "execute_code", "always", "external"
        schema = {"type": "object", "properties": {"code": {"type": "string"}}}

        def run(self, args, ctx):
            return str(ctx.tool_broker("bash", {"command": CHECK}))

    h = _harness(tmp_path, monkeypatch, gate=lambda ws: Gate(ws, mode=Mode.INTERACTIVE))
    h.approve = lambda *a: Outcome.ALLOW_ALWAYS.value
    h.registry.register(FakeExec())
    ran = _bash_spy(h, result="2 passed in 0.1s")
    res, _ = _run(h, [_edit(),
                      Completion(tool_calls=[ToolCall("x1", "execute_code", {"code": "run"})]),
                      Completion(text="Bumped VALUE; the suite passes.")])

    assert ran == [CHECK]
    assert res.verified is True, "a really-dispatched inner check must still verify"
    assert not res.error


def test_denied_run_in_env_is_not_a_fresh_observation(tmp_path, monkeypatch):
    """run_in_env's receipt path already fails closed; it must also not claim a run happened."""
    build, approve = _denying_gate(Outcome.REJECT_ALWAYS)
    h = _harness(tmp_path, monkeypatch, gate=build)
    h.approve = approve
    if h.registry.get("run_in_env") is None:
        class FakeEnv(Tool):
            name, tier, risk = "run_in_env", "always", "external"
            schema = {"type": "object", "properties": {"command": {"type": "string"}}}

            def run(self, args, ctx):   # pragma: no cover - must never be reached here
                raise AssertionError("run_in_env dispatched despite refusal")
        h.registry.register(FakeEnv())
    res, hints = _run(h, [_edit(),
                          Completion(tool_calls=[ToolCall("r1", "run_in_env",
                                                          {"command": CHECK})]),
                          Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                          Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE.")])

    assert res.verified is False
    for body in [b for req in hints[2:] for b in req]:
        assert "NOT RUN yet" in body, "a refused run_in_env was reported as a run: %r" % body


# -- controls: the fix may only ever withhold verification --------------------------------
def test_executed_passing_check_still_verifies(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    ran = _bash_spy(h, result="3 passed in 0.1s")
    res, _ = _run(h, [_edit(), _bash("b1"), Completion(text="Bumped VALUE; suite green.")])

    assert ran == [CHECK]
    assert res.verified is True
    assert not res.error


def test_executed_failing_check_still_blocks(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    ran = _bash_spy(h, result="[exit 1]\n1 failed")
    res, _ = _run(h, [_edit(), _bash("b1"),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                      Completion(text="Bumped VALUE.")])

    assert ran, "the failing check really ran"
    assert res.verified is False
    assert res.error and "verification required" in res.error


def test_check_that_crashes_at_dispatch_still_counts_as_failure(tmp_path, monkeypatch):
    """`Tool.run` was entered, so its ERROR is an outcome, not a non-dispatch."""
    h = _harness(tmp_path, monkeypatch)

    def boom(args, ctx):
        raise RuntimeError("runner exploded")
    h.registry.get("bash").run = boom
    res, _ = _run(h, [_edit(), _bash("b1"),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                      Completion(text="Bumped VALUE.")])

    assert res.verified is False
    assert res.error and "verification required" in res.error


def test_canceled_check_is_not_verification(tmp_path, monkeypatch):
    """The cancel path fills the call's result in without dispatching it; same rule."""
    h = _harness(tmp_path, monkeypatch)
    ran = _bash_spy(h)
    stop = {"now": False}
    h.cancelled = lambda: stop["now"]

    def arm(_m):
        stop["now"] = True          # cancel lands between the reply and the dispatch
        return _bash("b1")
    res, _ = _run(h, [_edit(), arm, Completion(text="Bumped VALUE.")])

    assert ran == [], "the canceled command must never reach Tool.run"
    assert res.canceled is True
    assert res.verified is False, "a canceled check verified the edit"


def test_reread_is_still_not_verification(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    _bash_spy(h)
    res, _ = _run(h, [_edit(),
                      Completion(tool_calls=[ToolCall("r1", "read_file", {"path": "app.py"})]),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                      Completion(text="Bumped VALUE."), Completion(text="Bumped VALUE."),
                      Completion(text="Bumped VALUE.")])

    assert res.verified is False
    assert res.error and "verification required" in res.error
