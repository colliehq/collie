"""The gate wired into the run — end to end, through the real loop.

test_gate.py proves the decisions; this proves the WIRING, which is where the mistakes
that actually hurt would live: a secret rendered into an approval prompt, a refused call
that leaves an unpaired tool_use behind, an unattended run that treats silence as consent.
"""
import json
import os
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ScriptProvider  # noqa: E402

from harness.cli import make_harness  # noqa: E402
from harness.gate import Decision, Gate, Mode, Outcome  # noqa: E402
from harness.providers import Completion, ToolCall  # noqa: E402
from harness.tools import Tool  # noqa: E402


def _spy(nm, sink, ret="ok"):
    """A real Tool subclass — the registry builds provider schemas off these, so a bare
    duck-typed stand-in never reaches the model and the test would pass vacuously."""
    class Spy(Tool):
        name, tier = nm, "always"
        schema = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            sink.append(args if ret == "ok" else ret)
            return ret
    return Spy()


def _h(tmp_path, gate=None, approve=None, project="gate_test"):
    h = make_harness(str(tmp_path), provider="mock", project=project, embed="hash", gate=gate)
    h.max_turns = 3
    h.approve = approve
    return h


def _calls(*specs):
    """A completion proposing tool calls, then one that finishes."""
    tcs = [ToolCall("c%d" % i, name, args) for i, (name, args) in enumerate(specs)]
    return [Completion(text="", tool_calls=tcs), Completion(text="done", stop_reason="end_turn")]


def _results(h):
    return [m for m in (h._last_messages or []) if m.get("role") == "tool"]


def _run(h, task="do it"):
    res = h.run("gate_test", task, consolidate=False)
    h._last_messages = res.messages or []
    return res


class _RecordingGate:
    """Small deterministic gate for proving execute_code cannot hide an inner call."""
    def __init__(self):
        self.seen = []

    def evaluate(self, tool_name, args, tool=None):
        self.seen.append((tool_name, args))
        return Decision(True, "test allow", risk="read")


def _exec_code_h(tmp_path, gate):
    h = make_harness(str(tmp_path), provider="mock", project="gate_test", embed="hash",
                     gate=gate, exec_code=True)
    h.max_turns = 3
    return h


# -- the gate actually stops things -----------------------------------------
def test_external_call_is_refused_when_nobody_can_approve(tmp_path):
    """The headless case, and the one that matters most: no approver means no consent,
    so an off-machine action does NOT run just because nobody objected."""
    ran = []
    h = _h(tmp_path, gate=Gate(cwd=tmp_path))
    h.provider = _ScriptProvider(_calls(("browser_click", {"ref": "e1"})))

    h.registry.register(_spy('browser_click', ran))
    res = _run(h)
    assert not ran, "an external call ran with nobody to approve it"
    out = _results(h)[0]["content"]
    assert out.startswith("DENIED:"), out
    assert res.denied_calls == 1


def test_denied_call_still_pairs_its_tool_use(tmp_path):
    """An unpaired tool_use 400s the provider on the next turn and on --continue. The
    refusal has to come back AS the result, not as a dropped call."""
    h = _h(tmp_path, gate=Gate(cwd=tmp_path))
    h.provider = _ScriptProvider(_calls(("browser_click", {"ref": "e1"}),
                                        ("read_file", {"path": "nope.txt"})))
    _run(h)
    msgs = h._last_messages
    proposed = [tc.id for m in msgs if m.get("role") == "assistant"
                for tc in (m.get("tool_calls") or [])]
    answered = [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"]
    assert proposed and sorted(proposed) == sorted(answered), (
        "every proposed call must have a result: proposed=%s answered=%s" % (proposed, answered))


def test_project_mode_lets_ordinary_coding_through_untouched(tmp_path):
    """The trade this whole design rests on: writing and running inside the directory you
    launched collie in never asks. If this ever starts prompting, the gate is not shippable."""
    asked = []
    h = _h(tmp_path, gate=Gate(cwd=tmp_path),
           approve=lambda *a: asked.append(a) or Outcome.ALLOW_ONCE.value)
    h.provider = _ScriptProvider(_calls(("write_file", {"path": "a.py", "content": "x = 1"}),
                                        ("read_file", {"path": "a.py"})))
    _run(h)
    assert not asked, "project mode asked about work inside its own directory: %s" % asked
    assert (tmp_path / "a.py").read_text() == "x = 1"


# -- the approval path ------------------------------------------------------
def test_approval_lets_the_call_run(tmp_path):
    ran = []
    h = _h(tmp_path, gate=Gate(cwd=tmp_path),
           approve=lambda *a: Outcome.ALLOW_ONCE.value)
    h.provider = _ScriptProvider(_calls(("browser_click", {"ref": "e1"})))

    h.registry.register(_spy('browser_click', ran))
    _run(h)
    assert ran == [{"ref": "e1"}]


def test_the_approver_never_sees_a_restored_secret(tmp_path, monkeypatch):
    """THE one to never let regress.

    `_redact.restore` swaps {{SECRET:…}} back to the real credential one line before
    tool.run. Authorization happens BEFORE that, so an approval prompt — and anything it
    feeds: an audit row, a notification pushed to a phone — sees the placeholder. If this
    ordering ever flips, collie starts printing the user's keys on screen in the name of
    asking permission.
    """
    seen = {}

    def approver(tool_name, args, decision):
        seen.update(args)
        return Outcome.ALLOW_ONCE.value

    REAL = "sk-live-REAL-CREDENTIAL"
    h = _h(tmp_path, gate=Gate(cwd=tmp_path), approve=approver)
    # run() keeps a vault that is already set (getattr(self, "_secret_vault", {})), so
    # seeding it here is the same state a run reaches after redacting a real secret. The
    # vault is keyed by the placeholder's 8-hex id, not by the whole placeholder.
    h._secret_vault = {"deadbeef": REAL}

    got = []
    h.registry.register(_spy("browser_type", got))
    h.provider = _ScriptProvider(_calls(("browser_type", {"text": "{{SECRET:deadbeef}}"})))
    _run(h)

    assert seen, "the approver was never consulted — this test would pass vacuously"
    assert REAL not in repr(seen), (
        "the real credential reached the approval prompt: %r" % seen)
    assert seen["text"] == "{{SECRET:deadbeef}}"
    assert got and got[0]["text"] == REAL, (
        "the TOOL still needs the real value — only the approval path sees the placeholder")


def test_a_broken_gate_is_a_closed_gate(tmp_path):
    """A gate that raises must refuse, not wave things through."""
    class Exploding:
        def evaluate(self, *a, **kw):
            raise RuntimeError("boom")

    ran = []
    h = _h(tmp_path, gate=Exploding())
    h.provider = _ScriptProvider(_calls(("read_file", {"path": "x"})))

    h.registry.register(_spy('read_file', ran))
    _run(h)
    assert not ran
    assert _results(h)[0]["content"].startswith("DENIED:")


def test_an_unparseable_answer_is_not_consent(tmp_path):
    ran = []
    h = _h(tmp_path, gate=Gate(cwd=tmp_path), approve=lambda *a: "sure why not")
    h.provider = _ScriptProvider(_calls(("browser_click", {"ref": "e1"})))

    h.registry.register(_spy('browser_click', ran))
    _run(h)
    assert not ran


def test_an_exploding_approver_denies(tmp_path):
    def approver(*a):
        raise RuntimeError("the surface went away")

    ran = []
    h = _h(tmp_path, gate=Gate(cwd=tmp_path), approve=approver)
    h.provider = _ScriptProvider(_calls(("browser_click", {"ref": "e1"})))

    h.registry.register(_spy('browser_click', ran))
    _run(h)
    assert not ran


def test_a_broken_audit_ledger_blocks_an_approved_consequential_call(tmp_path):
    class BrokenAudit:
        def record(self, **_kwargs):
            raise OSError("audit disk unavailable")

    ran = []
    h = _h(tmp_path, gate=Gate(cwd=tmp_path),
           approve=lambda *a: Outcome.ALLOW_ONCE.value)
    h.audit = BrokenAudit()
    h.provider = _ScriptProvider(_calls(("browser_click", {"ref": "e1"})))
    h.registry.register(_spy("browser_click", ran))

    _run(h)
    assert not ran
    assert "audit ledger is unavailable" in _results(h)[0]["content"]


def test_durable_tool_does_not_run_when_pre_action_checkpoint_fails(tmp_path):
    ran = []
    h = _h(tmp_path, gate=None)
    h.durable_session_id = "durable-session"
    h._session_checkpoint = lambda *args, **kwargs: False
    h.provider = _ScriptProvider(_calls(("write_file", {
        "path": "should-not-exist.txt", "content": "unsafe",
    })))
    h.registry.register(_spy("write_file", ran))

    _run(h)
    assert not ran
    assert "durability checkpoint failed" in _results(h)[0]["content"]


def test_execute_code_denied_inner_call_uses_harness_gate(tmp_path):
    gate = Gate(cwd=tmp_path)
    h = _exec_code_h(tmp_path, gate)
    events = []
    h.emit = lambda kind, data: events.append((kind, data))
    outside = tmp_path.parent / (tmp_path.name + "-blocked.txt")
    h.provider = _ScriptProvider(_calls(("execute_code", {"code":
        ('print(tool("write_file", path=%s, content="must not land"))\n'
         'print(bash("python -c \\\"assert True\\\""))') %
        json.dumps(str(outside))})))

    res = _run(h)

    assert not outside.exists()
    assert "DENIED:" in _results(h)[0]["content"]
    inner_events = [data for kind, data in events
                    if kind == "tool" and data.get("internal")]
    assert [event["name"] for event in inner_events] == ["write_file", "bash"]
    assert inner_events[0]["ok"] is False
    assert res.denied_calls == 1 and res.tool_calls == 3
    assert res.verified is False, "a denied write is not an edit that later checks can verify"


def test_execute_code_allowed_read_uses_normal_checkpoint_and_recorder_path(tmp_path):
    (tmp_path / "note.txt").write_text("brokered-content", encoding="utf-8")
    gate = _RecordingGate()
    h = _exec_code_h(tmp_path, gate)
    checkpoints = []

    def checkpoint(_messages, _run_id, _turn, state, detail=None, terminal=False):
        checkpoints.append((state, dict(detail or {}), terminal))
        return True

    h._session_checkpoint = checkpoint
    h.provider = _ScriptProvider(_calls(("execute_code", {
        "code": 'print(read_file("note.txt"))'})))

    res = _run(h)

    tool_messages = _results(h)
    assert len(tool_messages) == 1 and tool_messages[0]["name"] == "execute_code"
    assert "brokered-content" in tool_messages[0]["content"]
    assert [name for name, _args in gate.seen] == ["execute_code", "read_file"]
    inner = [(state, detail) for state, detail, _terminal in checkpoints
             if detail.get("inner_tool_name") == "read_file" and detail.get("internal")]
    assert [state for state, _detail in inner] == ["executing_tool", "executing_tool"]
    assert {detail["tool_name"] for _state, detail in inner} == {"execute_code"}
    assert {detail["tool_call_id"] for _state, detail in inner} == {"c0"}
    assert res.tool_calls == 2


def test_execute_code_inner_authorization_re_redacts_restored_secrets(tmp_path):
    seen = {}
    received = []
    # Deliberately not vendor-shaped: only exact vault-value replacement can catch this after
    # execute_code restores the placeholder and sends the bare value back over RPC.
    real = "opaque-credential-value-1234567890"

    def approver(_tool_name, args, _decision):
        seen.update(args)
        return Outcome.ALLOW_ONCE.value

    h = _exec_code_h(tmp_path, Gate(cwd=tmp_path))
    h.approve = approver
    h._secret_vault = {"deadbeef": real}
    h.registry.register(_spy("browser_type", received))
    h.provider = _ScriptProvider(_calls(("execute_code", {
        "code": ('print(tool("browser_type", '
                 'text="{{SECRET:deadbeef}}"))'),
    })))

    _run(h)

    assert seen["text"] == "{{SECRET:deadbeef}}"
    assert real not in repr(seen)
    assert received and received[0]["text"] == real


def test_execute_code_redacts_structured_inner_results_before_hooks(tmp_path):
    real = "opaque-structured-secret-value-1234567890"
    seen = []

    class StructuredSecret(Tool):
        name, tier, risk = "structured_secret", "always", "read"
        schema = {"type": "object", "properties": {}}

        def run(self, _args, _ctx):
            return {real: {"nested": [real]}}

    class HookResult:
        allowed, reason = True, ""
        receipts, additional_context = (), ()

    class Hooks:
        def dispatch(self, event, payload, subject=""):
            if event == "PostToolUse" and payload.get("tool_name") == "structured_secret":
                seen.append(payload.get("tool_response"))
            return HookResult()

    h = _exec_code_h(tmp_path, _RecordingGate())
    h._secret_vault = {"deadbeef": real}
    h.hooks = Hooks()
    h.registry.register(StructuredSecret())
    h.provider = _ScriptProvider(_calls(("execute_code", {
        "code": 'print(tool("structured_secret"))',
    })))

    _run(h)

    assert seen == [{"{{SECRET:deadbeef}}": {
        "nested": ["{{SECRET:deadbeef}}"]}}]
    assert real not in repr(seen)


def test_execute_code_memory_calls_are_joined_to_the_parent_lifecycle(tmp_path):
    audits = []

    class Audit:
        def record(self, **entry):
            audits.append(entry)

    h = _exec_code_h(tmp_path, _RecordingGate())
    h.audit = Audit()
    h.provider = _ScriptProvider(_calls(("execute_code", {
        "code": 'print(tool("remember", text="must stay quarantined"))',
    })))

    res = _run(h)

    assert h.memory.list_claims(status="proposed", project="gate_test") == []
    assert "memory tools cannot run inside execute_code" in _results(h)[0]["content"]
    assert res.denied_calls == 1
    assert any(item["tool"] == "remember" and item["stage"] == "denied"
               for item in audits)


def test_execute_code_nested_amplification_denials_are_audited(tmp_path):
    audits = []

    class Audit:
        def record(self, **entry):
            audits.append(entry)

    h = _exec_code_h(tmp_path, _RecordingGate())
    h.audit = Audit()
    h.provider = _ScriptProvider(_calls(("execute_code", {
        "code": ('print(tool("execute_code", code="print(1)"))\n'
                 'print(tool("delegate", task="nested"))'),
    })))

    res = _run(h)

    content = _results(h)[0]["content"]
    assert content.count("cannot be called from inside execute_code") == 2
    denied = [(item["tool"], item["stage"], item["outcome"]) for item in audits
              if item["tool"] in ("execute_code", "delegate")]
    assert denied == [
        ("execute_code", "denied", "refused"),
        ("delegate", "denied", "refused"),
    ]
    assert res.denied_calls == 2


def test_execute_code_late_approval_cannot_fire_or_mint_a_rule(tmp_path):
    approval_started = threading.Event()
    release = threading.Event()
    received = []

    def approver(_name, _args, _decision):
        approval_started.set()
        release.wait(5)
        return Outcome.ALLOW_ALWAYS.value

    gate = Gate(cwd=tmp_path)
    h = _exec_code_h(tmp_path, gate)
    h.approve = approver
    h.registry.register(_spy("browser_type", received))
    h.provider = _ScriptProvider(_calls(("execute_code", {
        "timeout": 1,
        "code": 'print(tool("browser_type", text="must not type late"))',
    })))
    holder = {}
    worker = threading.Thread(target=lambda: holder.setdefault("result", _run(h)), daemon=True)
    worker.start()
    assert approval_started.wait(3)
    time.sleep(1.4)  # let the parent subprocess deadline revoke the invocation
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert received == []
    assert gate.session_rules == set(), "late Allow must not widen future authority"


def test_execute_code_timeout_with_running_inner_tool_keeps_recovery_fence(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class SlowTool(Tool):
        name, tier, risk = "slow_external", "always", "external"
        schema = {"type": "object", "properties": {}}

        def run(self, _args, _ctx):
            started.set()
            release.wait(5)
            return "slow effect finished"

    h = _exec_code_h(tmp_path, _RecordingGate())
    h.registry.register(SlowTool())
    checkpoints = []
    h._session_checkpoint = lambda _m, _r, _t, state, detail=None, terminal=False: (
        checkpoints.append((state, dict(detail or {}), terminal)) or True)
    h.provider = _ScriptProvider(_calls(("execute_code", {
        "timeout": 1, "code": 'print(tool("slow_external"))'})))

    try:
        res = _run(h)
        assert started.is_set()
        assert "recovery inspection is required" in res.error
        assert checkpoints[-1][0] == "external_action"
        assert checkpoints[-1][2] is False
        assert checkpoints[-1][1]["tool_call_id"] == "c0"
        closed_tool_calls = res.tool_calls
        closed_checkpoint = checkpoints[-1]
    finally:
        release.set()
    # Let the late handler unwind after the real effect returns. It must not
    # mutate the already-closed run or overwrite the parent recovery fence.
    time.sleep(0.5)
    assert res.tool_calls == closed_tool_calls
    assert checkpoints[-1] == closed_checkpoint


# -- back-compat ------------------------------------------------------------
def test_no_gate_means_no_change(tmp_path):
    """Benchmarks, `pack` and the delegate child build harnesses through the same
    constructor. With gate=None the path must be exactly what it was."""
    ran = []
    h = _h(tmp_path, gate=None)
    h.provider = _ScriptProvider(_calls(("browser_click", {"ref": "e1"})))

    h.registry.register(_spy('browser_click', ran))
    res = _run(h)
    assert ran == [{"ref": "e1"}]
    assert res.denied_calls == 0


def test_authorization_happens_before_any_execution(tmp_path):
    """When the model proposes several calls, the human decides on all of them before the
    first one happens — otherwise the third is refused after the first two already went
    through irreversibly."""
    order = []

    def approver(tool_name, args, decision):
        order.append("ask:" + tool_name)
        return Outcome.ALLOW_ONCE.value

    h = _h(tmp_path, gate=Gate(cwd=tmp_path), approve=approver)

    for nm in ("browser_click", "browser_type"):
        h.registry.register(_spy(nm, order, ret="run:" + nm))
    h.provider = _ScriptProvider(_calls(("browser_click", {"ref": "a"}),
                                        ("browser_type", {"text": "b"})))
    _run(h)
    assert order == ["ask:browser_click", "ask:browser_type",
                     "run:browser_click", "run:browser_type"], order
