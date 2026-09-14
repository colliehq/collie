"""ONE accepted request, more than forty productive model/tool cycles.

The existing coverage around the turn boundary proves two narrower things:
``tests/test_run_options.py`` proves the *configuration* (balanced leaves
``max_turns`` at 0 and only sets the advisory ``turn_target`` 40), and
``tests/test_loop.py::test_zero_turn_cap_means_unlimited_not_zero_turns``
proves a single model turn is not truncated to zero.  Neither drives the loop
across the boundary, and
``tests/test_session_resume.py::test_default_run_continues_past_fifty_tool_steps``
does so only by re-reading ONE file with byte-identical arguments 65 times,
which is exactly the shape a "you already did that" heuristic is entitled to
cut short — so it cannot distinguish a turn-limit bug from repeat suppression.

What is exercised here is the product path a person actually gets: a request
accepted into the durable queue (frozen budget + capabilities), replayed
through ``terminal_queue``/``cli.apply_*`` the way ``collie repl``'s ``/next``
replays it, and then run to a declared terminal count of 47 tool cycles plus
one final answering turn.  Every cycle does DIFFERENT incremental work (step N
of a survey, recorded in a temp-dir ledger), so reaching 48 turns cannot be
confused with a loop-detection escape, and the ledger is independent physical
evidence of what was actually executed.

Offline throughout: a scripted provider is the ONLY replaced seam, plus one
test-owned harmless tool that writes into ``tmp_path``.  Sessions root, DATA,
settings file and workspace are all temporary.

NOTE on counting: ``_Scripted.calls`` below counts ``complete()`` invocations
observed in this process.  It is a test-side observation, NOT a number any
provider reported about physical API requests; ``res.model_calls`` is the
loop's own ledger, which for this offline stand-in has nothing richer to read.

    python -m pytest -q tests/test_single_run_turn_boundary.py
"""
import json
import os

import pytest

from harness import (cli, run_ownership, sessions, settings, task_inbox,
                     terminal_queue, web_tasks)
from harness.providers import Completion, ToolCall
from harness.tools import Tool

BOUNDARY = 40            # the advisory balanced turn_target under test
TOOL_CYCLES = 47         # declared terminal count: productive tool turns...
FINAL_TURNS = 1          # ...plus the one turn that answers
STEER_AT_CALL = 41       # a late instruction, deliberately PAST the boundary


class _Scripted:
    """Drives the loop with a fixed script and refuses to be called past its end.

    Strictness is the point: the shared ``_util._ScriptProvider`` repeats its
    last entry forever, which would hide exactly the failure these tests are
    looking for — a model call made after the run should have stopped.
    """
    reports_cache = False
    name = "deepseek"
    model = "deepseek-chat"
    max_tokens = 4096

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def complete(self, system, messages, tool_schemas, on_text=None):
        if self.calls >= len(self._script):
            raise AssertionError(
                "provider called %d times for a %d-entry script: work continued "
                "past the point this run had to stop" % (self.calls + 1, len(self._script)))
        item = self._script[self.calls]
        self.calls += 1
        return item(messages) if callable(item) else item


class _SurveyStep(Tool):
    """Harmless test-owned tool: record one DISTINCT step of a survey.

    Distinct arguments and a growing result string per call are what keep this
    a long piece of real work rather than a repeated no-op.
    """
    name = "survey_step"
    description = "Record one numbered survey step in the workspace ledger."
    tier = "always"
    schema = {"type": "object",
              "properties": {"step": {"type": "integer"}, "note": {"type": "string"}},
              "required": ["step", "note"]}

    def __init__(self, on_step=None):
        self.on_step = on_step

    def run(self, args, ctx):
        step, note = int(args["step"]), str(args["note"])
        path = os.path.join(ctx.cwd, "ledger.txt")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%d\t%s\n" % (step, note))
        if self.on_step is not None:
            self.on_step(step)
        return "recorded step %d (%s); ledger now has %d entries" % (
            step, note, len(_ledger(ctx.cwd)))


def _ledger(cwd):
    path = os.path.join(cwd, "ledger.txt")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [line.split("\t") for line in handle.read().splitlines() if line]


def _step_call(index, note="survey"):
    """One model turn that asks for one distinct unit of work."""
    return Completion(
        text="", stop_reason="tool_use",
        tool_calls=[ToolCall("c%d" % index, "survey_step",
                             {"step": index, "note": "%s-%d" % (note, index)})])


@pytest.fixture
def store(tmp_path, monkeypatch):
    """One installation's worth of isolated state: sessions, DATA, settings."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    monkeypatch.setattr(settings, "_HARD_ENV", set())
    for key in settings.LIMIT_KEYS:
        monkeypatch.delenv("COLLIE_" + key, raising=False)
    return str(directory)


def _panel(**values):
    with open(settings._PATH, "w", encoding="utf-8") as handle:
        json.dump(values, handle)
    settings._cache["mtime"] = -1.0


def _accept(sid, entry_id, text, *, cwd, quality="balanced"):
    """Accept one request exactly as the web composer does, frozen settings and all."""
    frozen = web_tasks.freeze_config(
        {"intent": "build", "quality": quality, "verification": "auto"},
        provider="mock", model="", limits=settings.freeze_limits())
    sessions.save(sid, [{"role": "user", "content": "earlier turn"}], cwd=cwd)
    return task_inbox.enqueue(sid, entry_id, text, mode="follow_up", config=frozen)


def _run_accepted(tmp_path, sid, provider, tools=(), events=None, cancelled=None):
    """Replay an accepted request the way ``collie repl``'s ``/next`` does.

    ``terminal_queue`` + ``cli.apply_turn_decision`` + ``cli.apply_accepted_*``
    is the real routing; only the provider is swapped, and deliberately AFTER
    the product code has finished configuring the harness, so nothing here can
    quietly widen or narrow what the accepted request was authorized to do.
    """
    h = cli.make_harness(str(tmp_path), provider="mock", project="boundary", embed="hash")
    h.checkpoint_scope = "session:" + sid        # exactly what cmd_repl sets
    for tool in tools:
        h.registry.register(tool)
    if events is not None:
        h.emit = lambda kind, data: events.append((kind, data))
    h.cancelled = cancelled
    observed = {}
    try:
        with run_ownership.hold(sid, label="single-run-boundary") as lease, \
                terminal_queue.claimed_next(sid, lease, requested=True) as queued:
            decision = terminal_queue.decision(queued, "mock", "", [], [])
            cli.apply_turn_decision(h, decision, None)
            cli.apply_accepted_limits(h, terminal_queue.accepted_limits(queued))
            cli.apply_accepted_capabilities(h, terminal_queue.accepted_capabilities(queued))
            observed["max_turns"] = h.max_turns
            observed["turn_target"] = h.turn_target
            h.provider = provider           # the one replaced seam
            h.run_owner, h.input_entry = lease, queued
            try:
                res = h.run("repl", run_ownership.entry_content(sid, queued),
                            consolidate=False, history=[],
                            authority_msg=queued["text"])
            finally:
                h.run_owner = h.input_entry = None
    finally:
        h.memory.close()
        h.recorder.close()
    return res, observed


def _record(case, res, provider, cwd, **extra):
    """Append what this case actually observed, when a diagnostic run asks for it.

    ``COLLIE_BOUNDARY_EVIDENCE=<path>`` makes the suite double as the diagnostic:
    the same assertions, plus a durable NDJSON line of the numbers behind them.
    Unset (the normal case) this costs one ``environ`` lookup and writes nothing.
    """
    path = os.environ.get("COLLIE_BOUNDARY_EVIDENCE")
    if not path:
        return
    row = dict(extra, case=case, turns=res.turns, tool_calls=res.tool_calls,
               stop_reason=res.stop_reason, completed=bool(res.success),
               turns_exhausted=bool(res.turns_exhausted),
               budget_exhausted=bool(res.budget_exhausted),
               canceled=bool(res.canceled), steer_count=res.steer_count,
               model_calls=res.model_calls,
               # An offline observation of complete() invocations in this
               # process — NOT a provider-reported physical API request count.
               observed_complete_calls=provider.calls,
               ledger_steps=[int(row[0]) for row in _ledger(cwd)],
               answer=(res.answer or "")[:200], error=(res.error or "")[:200])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
def test_accepted_request_runs_forty_seven_tool_cycles_then_one_final_turn(
        store, tmp_path):
    """The headline: ONE accepted request, 47 distinct tool cycles, then done.

    The advisory balanced target is 40.  Nothing may stop this run there: it
    must reach the declared terminal count, report ``completed``, and leave 47
    ledger rows behind as evidence that the work really happened.
    """
    sid = "long-accepted"
    _panel()                                     # no ceiling of any kind
    _accept(sid, "survey", "survey every module and report", cwd=str(tmp_path))

    def late_instruction(_messages):
        """A real instruction, accepted into the durable inbox mid-run.

        Enqueued synchronously inside the 41st model call, so it is on disk
        before the loop reaches the NEXT turn boundary — deterministic without
        any sleeping, and with no receipt invented by this test.
        """
        task_inbox.enqueue(sid, "late-steer", "from here on, mark the steps revised",
                           mode="steer")
        return _step_call(STEER_AT_CALL)

    script = [_step_call(i) for i in range(1, STEER_AT_CALL)]
    script.append(late_instruction)
    def revised_step(index):
        def answer(messages):
            # Changing the fixture's output on a timer would not prove delivery
            # to the model. Require the actual next request to contain the steer.
            assert any(
                message.get("role") == "user"
                and "from here on, mark the steps revised" in str(message.get("content", ""))
                for message in messages
            ), "late steer was consumed but absent from the provider request"
            return _step_call(index, note="revised")
        return answer

    script += [revised_step(i) for i in range(STEER_AT_CALL + 1, TOOL_CYCLES + 1)]
    script.append(Completion(text="Surveyed all %d modules." % TOOL_CYCLES,
                             stop_reason="end_turn"))
    provider = _Scripted(script)
    events = []

    res, observed = _run_accepted(tmp_path, sid, provider,
                                  tools=[_SurveyStep()], events=events)
    _record("long_accepted_run", res, provider, str(tmp_path), **observed)

    # The accepted request was routed as "balanced": 40 is a convergence target,
    # and the hard cap it was accepted under is genuinely absent.
    assert (observed["turn_target"], observed["max_turns"]) == (BOUNDARY, 0), observed
    assert res.budget_limits["MAX_TURNS"] == "0", res.budget_limits
    assert res.budget_limits["source"] == "frozen", res.budget_limits

    # The declared terminal count, reached and reported honestly.
    assert res.turns == TOOL_CYCLES + FINAL_TURNS, (res.turns, res.error)
    assert res.tool_calls == TOOL_CYCLES, (res.tool_calls, res.error)
    assert not res.turns_exhausted and not res.budget_exhausted
    assert res.stop_reason == "completed" and res.success
    assert res.answer == "Surveyed all %d modules." % TOOL_CYCLES
    assert "ran out of turns" not in res.answer

    # ...and no model or tool work beyond it. `_Scripted` raises rather than
    # repeating, so an extra call would have surfaced as an error above.
    assert provider.calls == TOOL_CYCLES + FINAL_TURNS, provider.calls
    assert res.model_calls == TOOL_CYCLES + FINAL_TURNS, res.model_calls

    # Physical evidence: 47 DIFFERENT steps, in order, executed once each.
    ledger = _ledger(str(tmp_path))
    assert [int(row[0]) for row in ledger] == list(range(1, TOOL_CYCLES + 1))
    assert len({row[1] for row in ledger}) == TOOL_CYCLES

    # The late instruction crossed the real inbox boundary, past turn 40, and
    # changed the work that followed.
    assert res.steer_count == 1, res.input_failures
    assert task_inbox.get(sid, "late-steer")["state"] == "consumed"
    assert any(m.get("inbox_id") == "late-steer" for m in res.messages), \
        "the accepted instruction must be in the transcript it was acknowledged against"
    assert [row[1] for row in ledger].count("survey-1") == 1
    assert all(row[1].startswith("revised-") for row in ledger[STEER_AT_CALL:])

    # The product's receipt event agrees with the result. This assertion reads
    # the emitted event; it does not claim a separate durable receipt readback.
    receipt = [data for kind, data in events if kind == "receipt"]
    assert len(receipt) == 1 and receipt[0]["completed"] is True
    assert receipt[0]["turns"] == TOOL_CYCLES + FINAL_TURNS
    assert receipt[0]["tool_calls"] == TOOL_CYCLES
    assert sum(1 for kind, _ in events if kind == "tool") == TOOL_CYCLES


# --------------------------------------------------------------------------- #
def test_accepted_hard_cap_stops_at_its_declared_boundary_without_completing(
        store, tmp_path):
    """The paired control: an explicit cap is a real, and honestly reported, stop.

    The cap is set ABOVE 40 on purpose.  A run that stopped at 40 here would be
    obeying something nobody declared; a run that reported ``completed`` at 44
    would be claiming a finished task it never finished.
    """
    sid = "capped-accepted"
    cap = 44
    _panel(MAX_TURNS=str(cap))
    _accept(sid, "capped", "survey every module and report", cwd=str(tmp_path))

    # More work is offered than the cap allows, plus the loop's final
    # no-tools synthesis turn, which must not execute any tool.
    script = [_step_call(i) for i in range(1, cap + 20)]
    script[cap] = Completion(text="Only %d of the steps were done." % cap,
                             stop_reason="end_turn")
    provider = _Scripted(script)

    res, observed = _run_accepted(tmp_path, sid, provider, tools=[_SurveyStep()])
    _record("explicit_hard_cap", res, provider, str(tmp_path), **observed)

    assert (observed["turn_target"], observed["max_turns"]) == (BOUNDARY, cap), observed
    assert res.budget_limits["MAX_TURNS"] == str(cap), res.budget_limits
    assert res.turns == cap and res.tool_calls == cap, (res.turns, res.tool_calls)
    assert res.turns_exhausted
    assert res.stop_reason == "turn_limit" and not res.success
    assert "_[stopped: ran out of turns (%d)" % cap in res.answer, res.answer
    # Exactly the capped amount of work happened — the cap stopped it, and the
    # one extra call is the loop's own summary turn, which ran no tool.
    assert [int(row[0]) for row in _ledger(str(tmp_path))] == list(range(1, cap + 1))
    assert provider.calls == cap + 1, provider.calls


# --------------------------------------------------------------------------- #
def test_stop_near_forty_is_honored_before_the_next_work_request(store, tmp_path):
    """Stop pressed just short of 40 ends the run at that boundary.

    "Honored" is measured as the absence of work: no further model call, no
    further tool execution, and a result that says canceled rather than done.
    """
    sid = "stopped-accepted"
    stop_after = 38
    _panel()
    _accept(sid, "stoppable", "survey every module and report", cwd=str(tmp_path))

    stopped = {"flag": False}
    tool = _SurveyStep(on_step=lambda step: stopped.__setitem__(
        "flag", stopped["flag"] or step >= stop_after))
    script = [_step_call(i) for i in range(1, stop_after + 20)]
    provider = _Scripted(script)
    events = []

    res, observed = _run_accepted(tmp_path, sid, provider, tools=[tool], events=events,
                                  cancelled=lambda: stopped["flag"])
    _record("stop_near_forty", res, provider, str(tmp_path), **observed)

    assert res.canceled and res.stop_reason == "canceled" and not res.success
    assert res.turns == stop_after and res.tool_calls == stop_after
    assert not res.turns_exhausted, "a stop is not a turn limit"
    assert res.answer.endswith("_[stopped by user]_"), res.answer
    # Nothing was requested after the stop: the run ended at the boundary that
    # followed the work already in flight.
    assert provider.calls == stop_after, provider.calls
    assert [int(row[0]) for row in _ledger(str(tmp_path))] == list(range(1, stop_after + 1))
    assert [data.get("at") for kind, data in events if kind == "canceled"] == ["turn_boundary"]


# --------------------------------------------------------------------------- #
def test_balanced_target_does_not_erase_a_smaller_accepted_turn_cap(store, tmp_path):
    """The advisory 40 must not overwrite a smaller number somebody declared.

    ``tests/test_run_options.py`` already checks that the configuration keeps
    both fields; what is checked here is the loop's behaviour and the sentence
    the person reads, which must quote the cap they set and not the target.
    """
    sid = "small-cap-accepted"
    cap = 5
    _panel(MAX_TURNS=str(cap))
    _accept(sid, "small", "survey every module and report", cwd=str(tmp_path))

    script = [_step_call(i) for i in range(1, BOUNDARY + 5)]
    script[cap] = Completion(text="Partial survey.", stop_reason="end_turn")
    provider = _Scripted(script)

    res, observed = _run_accepted(tmp_path, sid, provider, tools=[_SurveyStep()])
    _record("advisory_target_vs_smaller_cap", res, provider, str(tmp_path), **observed)

    assert observed["turn_target"] == BOUNDARY, "balanced still targets 40"
    assert observed["max_turns"] == cap, "the accepted cap survives configuration"
    assert res.turns == cap and res.turns_exhausted
    assert res.stop_reason == "turn_limit" and not res.success
    assert "ran out of turns (%d)" % cap in res.answer, res.answer
    assert "(%d)" % BOUNDARY not in res.answer, res.answer
    assert len(_ledger(str(tmp_path))) == cap
