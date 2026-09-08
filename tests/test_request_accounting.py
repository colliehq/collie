"""model_calls is a receipt for requests that were physically ISSUED.

``Completion.request_count`` documents physical provider requests, so an adapter that failed
before issuing one reports 0 — the Claude Agent SDK returns exactly that when its request gate
denies the reservation, before any worker is spawned. The loop used to clamp every count to at
least 1 in four places (main turn, compaction summary, critic, end-of-run synthesis), so a run
that made 48 requests and was then DENIED its final synthesis reported 49 model calls.

These tests pin the arithmetic on the real denied-gate path, and just as importantly what a
denial must NOT do: become an answer, count as usage, or spend a critic repair round arguing
with an error string. The error itself stays terminal — a refused reservation is still a
failure, it is just not a request.

    python -m pytest tests/test_request_accounting.py -q
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _util import _ScriptProvider                                        # noqa: E402
from harness import compaction, loop                                     # noqa: E402
from harness.claude_agent_sdk import ClaudeAgentSdkProvider              # noqa: E402
from harness.providers import (Completion, ToolCall, Usage,              # noqa: E402
                               issued_requests, request_count_of)


class _GatedSdkProvider(ClaudeAgentSdkProvider):
    """The shipped SDK adapter with only the process spawn stubbed.

    Everything the fix depends on — the subscription request gate, the denial branch, the
    request_count it reports — is production code from harness/claude_agent_sdk.py. The gate
    grants ``allow`` reservations and denies every one after that, which is what a run does
    when it reaches the user's request cap mid-run.
    """

    def __init__(self, replies=(), allow=0):
        super().__init__(subscription_only=True)
        self._replies = list(replies)
        self.allow = allow
        self.reservations = []          # True per granted reservation, False per denial
        self.spawned = 0
        self.request_gate = self._gate
        self.request_complete = lambda request_id, status: None

    def _gate(self, kind):
        granted = sum(self.reservations) < self.allow
        self.reservations.append(granted)
        return ("req-%d" % len(self.reservations)) if granted else ""

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.spawned += 1
        reply = self._replies[min(self.spawned - 1, len(self._replies) - 1)]
        return {"ok": True, "text": reply, "api_key_source": "none",
                "usage": {"input_tokens": 5, "output_tokens": 2}}


def _harness(cwd, project, provider, turns):
    from harness.cli import make_harness
    h = make_harness(cwd, provider="mock", project=project, embed="hash")
    h.provider = provider
    h.max_turns = h.turn_target = turns
    h.force_edit = False
    h.self_verify = False
    return h


def _write(path, content="done\n"):
    """A tool call in the SDK's own JSON envelope — the reply a worker would have returned."""
    return '{"tool":"write_file","args":{"path":%s,"content":%s}}' % (
        json.dumps(path), json.dumps(content))


def _tool_output(res):
    return [m for m in res.messages if m.get("role") == "tool"]


# --------------------------------------------------------------------------- the provider seam

def test_a_denied_reservation_reports_no_request_and_never_spawns_a_worker():
    provider = _GatedSdkProvider(['{"answer":"done"}'], allow=0)
    denied = provider.complete("s", [{"role": "user", "content": "u"}], [])
    assert denied.stop_reason == "error"
    assert "reservation denied" in denied.error_detail
    assert provider.spawned == 0, "the gate refused before anything could be issued"
    assert denied.request_count == 0
    assert issued_requests(denied) == 0, "what every ledger in the loop must add"


@pytest.mark.parametrize("value,expected", [
    (0, 0),                      # the honest no-spawn report, preserved
    (1, 1),
    (5, 5),                      # a repairing adapter's real attempts, counted in full
    (2.0, 2),                    # integral float: same rule bounded_int already used
    (None, 1), (True, 1), (False, 1), ("2", 1), (2.5, 1), (-1, 1),
    (float("nan"), 1), (float("inf"), 1),
])
def test_only_a_readable_count_is_believed(value, expected):
    """Unreadable metadata keeps the conservative 1: an adapter that ran and then failed to
    describe itself is likelier to have spent a request than to have spent none."""
    assert issued_requests(Completion(request_count=value)) == expected
    assert request_count_of(value) == expected


def test_a_completion_that_carries_no_count_at_all_still_costs_one():
    assert issued_requests(types.SimpleNamespace(text="from a foreign adapter")) == 1


def test_the_summary_ceiling_still_refuses_an_absurd_count():
    """The documented compaction bound is unchanged: past it, fall back to one request."""
    cap = compaction.MAX_SUMMARY_REQUESTS
    assert issued_requests(Completion(request_count=cap), maximum=cap) == cap
    assert issued_requests(Completion(request_count=cap + 1), maximum=cap) == 1


# --------------------------------------------------------------------------- the main loop

def test_the_turn_a_gate_denied_is_not_added_to_the_run(tmp_path):
    """Turn 0 is reserved and issued; turn 1's reservation is denied. One request, one call."""
    provider = _GatedSdkProvider([_write(str(tmp_path / "result.txt"))], allow=1)
    h = _harness(str(tmp_path), "denied_turn", provider, turns=4)

    res = h.run("denied_turn", "write the result", consolidate=False)

    assert provider.reservations == [True, False]
    assert provider.spawned == 1
    assert res.model_calls == 1, "the denied turn never reached the provider"
    assert res.error and "reservation denied" in res.error
    assert not res.answer, "a refused reservation is not an answer"
    assert (tmp_path / "result.txt").read_text() == "done\n"


def test_a_multi_request_adapter_is_still_counted_in_full(tmp_path):
    """The clamp is gone, not the counting: a format-repairing adapter's 3 requests are 3."""
    path = str(tmp_path / "result.txt")
    provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall("w1", "write_file", {"path": path, "content": "x\n"})],
                   stop_reason="tool_use", usage=Usage(input_tokens=4), request_count=3),
        Completion(text="finished", stop_reason="end_turn", usage=Usage(input_tokens=4)),
    ])
    h = _harness(str(tmp_path), "multi_request", provider, turns=4)

    res = h.run("multi_request", "write the result", consolidate=False)

    assert res.answer == "finished"
    assert provider.calls == 2 and res.model_calls == 4


# --------------------------------------------------------------------------- final synthesis

def test_a_denied_final_synthesis_keeps_the_work_and_bills_nothing(tmp_path):
    """The reported bug, end to end: every turn is issued, the run runs out of turns, and the
    synthesis reservation is denied. The receipt must show the requests that happened — and the
    tool work already done must survive, without the run claiming it finished."""
    path = tmp_path / "result.txt"
    provider = _GatedSdkProvider([_write(str(path), "first\n"), _write(str(path), "second\n")],
                                 allow=2)
    h = _harness(str(tmp_path), "denied_synthesis", provider, turns=2)

    res = h.run("denied_synthesis", "keep working", consolidate=False)

    assert provider.reservations == [True, True, False], "the synthesis asked once, was refused"
    assert provider.spawned == 2
    assert res.model_calls == 2, "48 requests must not be reported as 49"
    assert res.answer == "(ran out of turns — UNFINISHED; see the edits/tools above)"
    assert res.error and "reservation denied" in res.error
    assert path.read_text() == "second\n", "executed tool progress survives the denial"
    assert len(_tool_output(res)) == 2
    assert res.input_tokens == 10 and res.output_tokens == 4, (
        "the two issued requests still cost their real tokens")


def test_a_denied_synthesis_is_not_retried_into_a_repair_loop(tmp_path):
    """A denial is terminal, not an invitation: no second attempt, no invented success."""
    provider = _GatedSdkProvider([_write(str(tmp_path / "r.txt"))], allow=1)
    h = _harness(str(tmp_path), "denied_no_retry", provider, turns=1)
    h.max_retries = 3
    h.max_contract_repairs = 2

    res = h.run("denied_no_retry", "keep working", consolidate=False)

    assert provider.reservations == [True, False]
    assert res.contract_repairs == 0
    assert res.model_calls == 1
    assert "done —" not in (res.answer or ""), "a refused synthesis must not read as done"
    assert res.answer.startswith("(ran out of turns")


# --------------------------------------------------------------------------- critic

def test_a_denied_critic_review_costs_nothing_and_opens_no_repair_round(tmp_path, monkeypatch):
    """The reviewer never reviewed. Its error text is not a finding to argue with, and the
    reservation it was refused is not a model call."""
    monkeypatch.setattr(loop, "_tree_diff", lambda _cwd: "diff --git a/r.txt b/r.txt\n")
    path = str(tmp_path / "r.txt")
    main = _ScriptProvider([
        Completion(tool_calls=[ToolCall("w1", "write_file", {"path": path, "content": "x\n"})],
                   stop_reason="tool_use"),
        Completion(text="finished", stop_reason="end_turn"),
    ])
    critic = _GatedSdkProvider(['CORRECT'], allow=0)
    h = _harness(str(tmp_path), "denied_critic", main, turns=4)
    h.critic = True
    h.critic_max = 2
    h.critic_provider = critic

    res = h.run("denied_critic", "write the result", consolidate=False)

    assert critic.reservations == [False], "the review was attempted exactly once"
    assert critic.spawned == 0
    assert main.calls == 2 and res.model_calls == 2, "the refused review is not a model call"
    assert res.answer == "finished"
    assert not any(m.get("kind") == "review_feedback" for m in res.messages), (
        "a denial must not be fed back as a review objection"
    )


def test_a_reviewer_that_did_answer_is_still_billed_and_still_blocks(tmp_path, monkeypatch):
    """Control for the test above: the critic path itself is unchanged when it really runs."""
    monkeypatch.setattr(loop, "_tree_diff", lambda _cwd: "diff --git a/r.txt b/r.txt\n")
    path = str(tmp_path / "r.txt")
    main = _ScriptProvider([
        Completion(tool_calls=[ToolCall("w1", "write_file", {"path": path, "content": "x\n"})],
                   stop_reason="tool_use"),
        Completion(text="finished", stop_reason="end_turn"),
        Completion(text="fixed and finished", stop_reason="end_turn"),
    ])
    critic = _ScriptProvider([Completion(text="it misses the empty-input case",
                                         stop_reason="end_turn",
                                         usage=Usage(input_tokens=6), request_count=2),
                              Completion(text="CORRECT", stop_reason="end_turn")])
    h = _harness(str(tmp_path), "billed_critic", main, turns=4)
    h.critic = True
    h.critic_max = 1
    h.critic_provider = critic

    res = h.run("billed_critic", "write the result", consolidate=False)

    assert any(m.get("kind") == "review_feedback" for m in res.messages)
    assert res.model_calls == main.calls + 2, "the review's two real requests are billed"
    assert res.answer == "fixed and finished"
