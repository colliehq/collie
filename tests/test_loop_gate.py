"""The gate wired into the run — end to end, through the real loop.

test_gate.py proves the decisions; this proves the WIRING, which is where the mistakes
that actually hurt would live: a secret rendered into an approval prompt, a refused call
that leaves an unpaired tool_use behind, an unattended run that treats silence as consent.
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ScriptProvider  # noqa: E402

from harness.cli import make_harness  # noqa: E402
from harness.gate import Gate, Mode, Outcome  # noqa: E402
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
