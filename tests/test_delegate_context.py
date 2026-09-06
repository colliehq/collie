"""Child investigations share the parent's execution limits and authenticated authority."""
import json
import os
import threading

import pytest

from harness import cli
from harness.gate import Gate, Mode
from harness.providers import Completion, ToolCall, Usage
from harness.recorder import root_run_filter
from _util import _ScriptProvider


def response(text="", name=None, args=None, call_id="c"):
    return Completion(text=text, usage=Usage(input_tokens=100, output_tokens=20),
                      tool_calls=[ToolCall(call_id, name, args or {})] if name else [],
                      stop_reason="tool_use" if name else "end_turn")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    for key in ("COLLIE_SUBAGENT", "COLLIE_MAX_TURNS", "COLLIE_MAX_TOTAL_TOKENS", "COLLIE_MAX_COST"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "fact.txt").write_text("supported finding", encoding="utf-8")
    h = cli.make_harness(str(tmp_path), provider="mock", project="delegate-test", embed="hash",
                         delegate=True, gate=Gate(cwd=tmp_path, mode=Mode.PROJECT))
    h._events = []
    h.emit = lambda event: h._events.append(event)
    yield h
    h.memory.close()
    h.recorder.close()


def delegated_result(result):
    return json.loads(next(msg["content"] for msg in result.messages
                           if msg.get("role") == "tool" and msg.get("name") == "delegate"))


def test_child_keeps_provider_authority_and_reports_aggregate_usage(agent, monkeypatch):
    h = agent
    authority_inputs = []
    begin = h.gate.begin_request
    def tracked(message, **kwargs):
        authority_inputs.append(message)
        return begin(message, **kwargs)
    monkeypatch.setattr(h.gate, "begin_request", tracked)
    # No factory supports this arbitrary name. Delegation must retain the actual
    # resolved provider, instead of reopening settings and a different account.
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "inspect fact.txt"}),
        response(name="read_file", args={"path": "fact.txt"}, call_id="r"),
        response("The evidence supports the finding."),
        response("Investigation complete."),
    ], name="already-resolved-custom-provider", model="already-resolved-model")
    before = dict(os.environ)
    result = h.run("parent", "Investigate the issue; do not change files.", consolidate=False)
    assert not result.error and result.success
    assert result.model_calls == h.provider.calls == 4
    assert result.total_tokens == 480
    assert authority_inputs == ["Investigate the issue; do not change files."]
    assert dict(os.environ) == before
    child = delegated_result(result)
    assert child["status"] == "completed" and child["model_calls"] == 2
    assert len(result.messages) == 4  # child exploration stays out of parent context
    rows = h.recorder.db.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
    assert len(rows) == 2 and rows[1]["parent_run_id"] == rows[0]["run_id"]
    assert rows[1]["total_tokens"] == 240
    counted = h.recorder.db.execute("SELECT sum(total_tokens) FROM runs WHERE " +
                                   root_run_filter(h.recorder.db)).fetchone()[0]
    assert counted == 480  # child telemetry must not double total spending


def test_child_receives_original_user_request_beside_model_authored_subtask(agent):
    h = agent
    def inspect_child_prompt(messages):
        prompt = json.loads(messages[0]["content"])
        assert prompt["user_request"] == "Reject non-ISO dates."
        assert prompt["delegated_subtask"] == "Accept non-ISO dates."
        return response("The proposed subtask contradicts the user's requirement.")
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "Accept non-ISO dates."}),
        inspect_child_prompt, response("I will preserve strict ISO date validation."),
    ])
    result = h.run("parent", "Reject non-ISO dates.", consolidate=False)
    assert "contradicts" in delegated_result(result)["answer"]


def test_child_does_not_write_into_parent_durable_conversation(agent, monkeypatch, tmp_path):
    from harness import sessions
    h = agent
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    h.checkpoint_scope = "session:parent-thread"
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "inspect fact.txt"}),
        response(name="read_file", args={"path": "fact.txt"}, call_id="child-read"),
        response("Private child finding."), response("Public parent result."),
    ])
    result = h.run("parent", "Inspect this workspace.", consolidate=False)
    durable = sessions.load("parent-thread")["messages"]
    assert durable == result.messages
    assert len(durable) == 4
    assert not any(m.get("tool_call_id") == "child-read" for m in durable)
    assert sessions.recovery_state("parent-thread") is None


def test_child_cannot_write_or_nest_even_if_model_requests_it(agent):
    h = agent
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "write forbidden.txt then delegate again"}),
        response(name="write_file", args={"path": "forbidden.txt", "content": "bad"}, call_id="w"),
        response(name="delegate", args={"task": "nested"}, call_id="nested"),
        response("I can only inspect; the parent must make any changes."),
        response("Inspection finished."),
    ])
    result = h.run("parent", "inspect the project", consolidate=False)
    assert not os.path.exists(os.path.join(h.cwd, "forbidden.txt"))
    assert result.model_calls == 5
    assert delegated_result(result)["tool_calls"] == 2
    assert len(h.recorder.db.execute("SELECT run_id FROM runs").fetchall()) == 2


def test_child_requests_count_against_parent_physical_call_limit(agent):
    h = agent
    h.max_model_calls = 2
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "inspect fact.txt"}),
        response(name="read_file", args={"path": "fact.txt"}, call_id="r"),
        response("This completion must not be requested."),
    ])
    result = h.run("parent", "investigate", consolidate=False)
    assert h.provider.calls == result.model_calls == 2
    assert result.stop_reason == "budget_limit" and not result.success
    assert delegated_result(result)["status"] == "budget_limit"


def test_child_spending_includes_already_spent_parent_tokens(agent, monkeypatch):
    monkeypatch.setenv("COLLIE_MAX_TOTAL_TOKENS", "200")
    h = agent
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "inspect fact.txt"}),
        response(name="read_file", args={"path": "fact.txt"}, call_id="r"),
        response("Do not spend a third request."),
    ])
    result = h.run("parent", "investigate", consolidate=False)
    assert h.provider.calls == result.model_calls == 2
    assert result.total_tokens == 240 and result.budget_exhausted
    assert delegated_result(result)["status"] == "budget_limit"


def test_cancel_reaches_child_before_another_request(agent):
    h = agent
    canceled = threading.Event()
    h.cancelled = canceled.is_set
    def child_step(messages):
        canceled.set()
        return response(name="read_file", args={"path": "fact.txt"}, call_id="r")
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "inspect fact.txt"}), child_step,
        response("Must not request this."),
    ])
    result = h.run("parent", "investigate", consolidate=False)
    assert result.canceled and result.stop_reason == "canceled"
    assert h.provider.calls == result.model_calls == 2
    assert delegated_result(result)["status"] == "canceled"


def test_shared_budget_observes_child_usage_exactly_once(agent):
    observed = []
    class Budget:
        def account(self, model, usage):
            observed.append(usage.input_tokens + usage.output_tokens)
        def exceeded(self):
            return False
    h = agent
    h.shared_budget = Budget()
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "explain what is known"}),
        response("A scoped finding."), response("Complete."),
    ])
    result = h.run("parent", "investigate", consolidate=False)
    assert result.total_tokens == 360
    assert observed == [120, 120, 120]


def test_explicit_child_turn_limit_is_reported_as_unfinished(agent):
    h = agent
    h.provider = _ScriptProvider([
        response(name="delegate", args={"task": "inspect", "max_turns": 1}),
        response(name="read_file", args={"path": "fact.txt"}, call_id="r"),
        response("A partial investigation."), response("Parent continued."),
    ])
    result = h.run("parent", "investigate", consolidate=False)
    assert delegated_result(result)["status"] == "turn_limit"
    assert result.answer == "Parent continued."
