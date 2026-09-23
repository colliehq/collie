"""The end-of-run summary turn must tell the model the run is OVER.

Measured on a real 50-turn run (Opus, high effort, 50 successful read_file calls, host
stop_reason ``turn_limit``, 51 settled requests): the loop ended by exhausting its turns, made
its one final no-tools completion — and reused the working system prompt and history verbatim.
Nothing in that request said execution had ended or what the turn was for, so the model did the
only thing the thread suggested: it emitted its next tool call. The answer delivered to the user
was the single line ``read_file(node_0f4c32741e99ac23c8261209.json)``, followed by the host's
"ran out of turns" notice. The host was honest; the report was worthless.

These tests drive the REAL Harness loop with an offline provider that reproduces that behaviour
conditionally: while tool schemas are offered it keeps reading files, and on the no-tools turn it
summarizes only if the request actually tells it the run has stopped and no further action is
possible. Before the fix the loop sends no such instruction, the provider falls back to writing
its next tool call as prose, and the contract assertions below fail.

    python -m pytest tests/test_final_summary_contract.py -q
"""
import os
import re
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.loop import final_summary_instruction                       # noqa: E402
from harness.providers import Completion, ToolCall, Usage                # noqa: E402

_TURN_USAGE = Usage(input_tokens=400, output_tokens=40)   # so the token ceiling is reachable

# What the summary turn has to convey, matched as CONCEPTS rather than as the shipped wording —
# rephrasing the instruction should not break these tests, dropping its meaning should.
_SAYS_ENDED = re.compile(
    r"execution has ended|ended execution|(?:run|task|it)\s+(?:has\s+)?(?:ended|stopped)"
    r"|no (?:further|more) turn", re.I)
_SAYS_NO_ACTION = re.compile(
    r"no tools?\b[^.]*available|nothing you write will be executed|cannot .{0,40}\brun\b"
    r"|discarded, not run", re.I)


def _host_ended_the_run(messages):
    """True when some host-authored message in the request says the run is over AND that no
    further action is possible. Both halves matter: 'you are out of turns' alone still reads
    like a working thread to a model that has one completion left."""
    for m in messages:
        if m.get("role") != "user":
            continue
        body = m.get("content")
        if not isinstance(body, str):
            continue
        if _SAYS_ENDED.search(body) and _SAYS_NO_ACTION.search(body):
            return True
    return False


class _KeepsReadingModel:
    """Offline stand-in for the model in the measured run.

    Given tools, it reads one more file every turn and never volunteers to finish, so the loop
    can only end by exhausting its turn cap. Given NO tools, it behaves the way the real run
    did: if the request never says execution is over it writes out the tool call it wanted next
    (the defect), otherwise it writes the short report the host asked for.
    """
    name, model, max_tokens, reports_cache = "offline-longturn", "offline-longturn", 4096, False

    def __init__(self, targets, summary=None):
        self.targets = list(targets)
        self.summary = summary or "Read %d files; no fix was written yet."
        self.reads = 0
        self.tool_schema_counts = []        # per complete() call
        self.final_request = None           # (system, messages) of the no-tools turn
        self.told_run_ended = None

    def _next_target(self):
        return self.targets[min(self.reads, len(self.targets) - 1)]

    def complete(self, system, messages, tool_schemas, on_text=None):
        self.tool_schema_counts.append(len(tool_schemas or []))
        if tool_schemas:
            self.reads += 1
            return Completion(stop_reason="tool_use", usage=_TURN_USAGE, tool_calls=[
                ToolCall("call-%d" % self.reads, "read_file",
                         {"path": self._next_target()})])
        self.final_request = (system, list(messages))
        self.told_run_ended = _host_ended_the_run(messages)
        if self.told_run_ended:
            return Completion(text=self.summary % self.reads, stop_reason="end_turn",
                              usage=_TURN_USAGE)
        return Completion(text="read_file(%s)" % self._next_target(), stop_reason="end_turn",
                          usage=_TURN_USAGE)


def _workspace(tmp_path, n=6):
    names = []
    for i in range(n):
        name = "node_%02d.json" % i
        (tmp_path / name).write_text('{"id": %d}\n' % i, encoding="utf-8")
        names.append(name)
    return names


def _harness(tmp_path, provider, turns, project="final_summary"):
    from harness.cli import make_harness
    h = make_harness(str(tmp_path), provider="mock", project=project, embed="bm25")
    h.provider = provider
    h.max_turns = h.turn_target = turns
    h.force_edit = False
    h.self_verify = False
    return h


# --------------------------------------------------------------- the end-to-end contract

def test_a_turn_exhausted_run_reports_instead_of_emitting_its_next_tool_call(tmp_path):
    """The whole defect, end to end: exhaust the turns, then read what the user is handed."""
    provider = _KeepsReadingModel(_workspace(tmp_path))
    h = _harness(tmp_path, provider, turns=4)
    res = h.run("exhaust", "inspect every node file and report what they contain",
                consolidate=False)

    assert provider.reads == 4, "the loop must actually spend its turn budget on tools"
    assert provider.told_run_ended, \
        "the final no-tools turn never learned the run had stopped; request was:\n%s" % (
            (provider.final_request or ("", [{}]))[1][-1].get("content", "")[:400])
    answer = (res.answer or "").strip()
    assert not answer.startswith("read_file("), \
        "the user was handed the model's next tool call as the answer: %r" % answer[:120]
    assert provider.summary % provider.reads in answer, \
        "the model's summary must survive into the answer: %r" % answer[:200]
    # the host's own honesty note still rides along, unchanged by this repair
    assert "ran out of turns" in answer and "NOT finished" in answer
    assert res.turns_exhausted is True
    assert not any(m.get("kind") == "final_summary" for m in res.messages), \
        "the summary instruction belongs to the request projection, not the durable transcript"


def test_the_summary_turn_offers_no_tools_and_names_the_limit_that_stopped_the_run(tmp_path):
    """Inspect the request the loop actually built for the summary turn."""
    provider = _KeepsReadingModel(_workspace(tmp_path))
    h = _harness(tmp_path, provider, turns=3)
    h.run("inspect", "inspect every node file", consolidate=False)

    assert provider.tool_schema_counts[:3] == [provider.tool_schema_counts[0]] * 3
    assert provider.tool_schema_counts[0] > 0, "working turns must be offered tools"
    assert provider.tool_schema_counts[-1] == 0, "the summary turn must offer NO tools"

    system, messages = provider.final_request
    instruction = messages[-1]
    assert instruction["role"] == "user" and instruction.get("source") == "harness", \
        "the instruction must be host-authored, not attributed to the user"
    body = instruction["content"]
    assert str(h.max_turns) in body, "name the turn cap that actually stopped the run: %r" % body
    assert system, "the summary turn still carries the composed system prompt"
    # ...and it must not invite the model to claim or promise anything it cannot back
    assert re.search(r"\bverif", body, re.I), "forbid invented verification"
    assert re.search(r"\bpermission\b|\bapproval\b", body, re.I), "forbid a new permission ask"
    assert re.search(r"(do not|don't|never)[^.]{0,80}\bsaved\b", body, re.I), \
        "the durable save runs AFTER this turn and can fail, so claiming it must be " \
        "forbidden outright: %r" % body


def test_the_instruction_only_blames_the_turn_limit_when_the_turn_limit_is_what_stopped(tmp_path):
    """A spin-break or guard stop reaches the same branch; it must not borrow the turn story."""
    limited = final_summary_instruction(True, 50)
    guarded = final_summary_instruction(False, 50)

    assert "50" in limited and re.search(r"\bturn", limited, re.I)
    assert "50" not in guarded, "a guard stop must not quote a turn count it did not reach"
    assert not re.search(r"used all .* turns", guarded, re.I)
    for text in (limited, guarded):
        assert _SAYS_ENDED.search(text) and _SAYS_NO_ACTION.search(text), \
            "every variant states the stop and that nothing further will run: %r" % text
    # an unknown cap still gets a truthful turn-limit sentence, just without a number
    assert re.search(r"\bturn limit\b", final_summary_instruction(True, 0), re.I)


# --------------------------------------------------------------- what the summary turn may NOT do

@pytest.mark.parametrize("prose", ["", "I will write must_not_be_written.txt now."])
def test_a_summary_turn_that_answers_with_tool_calls_neither_runs_nor_ships_them(tmp_path, prose):
    """The model refuses to summarize and asks for one more read. Nothing may dispatch it, and
    the call must not be rendered to the user as the final answer."""
    names = _workspace(tmp_path)
    marker = tmp_path / "must_not_be_written.txt"

    class _RefusesToSummarize(_KeepsReadingModel):
        def complete(self, system, messages, tool_schemas, on_text=None):
            if not tool_schemas:
                self.tool_schema_counts.append(0)
                self.final_request = (system, list(messages))
                return Completion(text=prose, stop_reason="tool_use", tool_calls=[
                    ToolCall("late-1", "write_file",
                             {"path": marker.name, "content": "executed"})])
            return super().complete(system, messages, tool_schemas, on_text)

    provider = _RefusesToSummarize(names)
    h = _harness(tmp_path, provider, turns=3)
    res = h.run("refuse", "inspect every node file", consolidate=False)

    assert not marker.exists(), "a tool call from the summary turn must never be executed"
    answer = (res.answer or "")
    assert "write_file" not in answer and "late-1" not in answer, \
        "a tool call must not be shown to the user as the answer: %r" % answer[:160]
    assert not prose or prose not in answer, "an unexecuted action promise is not a final report"
    assert "UNFINISHED" in answer and "ran out of turns" in answer, \
        "fall back to the truthful unfinished text: %r" % answer[:160]
    assert len(provider.tool_schema_counts) == 4, "and no retry turn is bought to argue with it"
    assert not [m for m in res.messages if m.get("role") == "tool"][3:], \
        "only the three in-loop tool results exist; the summary turn added none"


def test_a_failed_summary_turn_stays_an_error_and_is_not_retried(tmp_path):
    """Unchanged invariant, re-pinned on the branch this repair touches."""
    class _Broken(_KeepsReadingModel):
        def complete(self, system, messages, tool_schemas, on_text=None):
            if not tool_schemas:
                self.tool_schema_counts.append(0)
                return Completion(text="upstream exploded", stop_reason="error",
                                  error_detail="500")
            return super().complete(system, messages, tool_schemas, on_text)

    provider = _Broken(_workspace(tmp_path))
    h = _harness(tmp_path, provider, turns=3)
    res = h.run("broken", "inspect every node file", consolidate=False)

    assert "upstream exploded" in (res.error or ""), "the failure stays terminal"
    assert "upstream exploded" not in (res.answer or ""), \
        "a failed synthesis must not become the report"
    assert "UNFINISHED" in (res.answer or "")
    assert len(provider.tool_schema_counts) == 4, "one summary attempt, no repair round"


def test_the_summary_turn_costs_exactly_one_accounted_request(tmp_path):
    """The repair adds a message, not a call: the receipt is still turns + 1."""
    provider = _KeepsReadingModel(_workspace(tmp_path))
    h = _harness(tmp_path, provider, turns=4)
    res = h.run("count", "inspect every node file", consolidate=False)

    assert len(provider.tool_schema_counts) == 5, "4 working turns + 1 summary turn"
    assert res.model_calls == 5, "receipt must match what was issued: %r" % res.model_calls
    assert res.turns == 4


def test_a_budget_exhausted_run_still_buys_no_summary_turn(tmp_path, monkeypatch):
    """The budget guards sit in front of this branch and must keep doing so."""
    provider = _KeepsReadingModel(_workspace(tmp_path))
    h = _harness(tmp_path, provider, turns=4)
    monkeypatch.setenv("COLLIE_MAX_TOTAL_TOKENS", "100")  # one working turn spends past this
    res = h.run("broke", "inspect every node file", consolidate=False)

    assert res.turns < 4, "the token ceiling must stop the loop early (got %d)" % res.turns
    assert provider.final_request is None, "no summary request may be issued past the ceiling"
    assert 0 not in provider.tool_schema_counts, "every request made was a working turn"
    assert "budget" in (res.answer or "").lower(), "the answer names the ceiling: %r" % res.answer
