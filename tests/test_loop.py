"""The run itself: what happens to an error, a truncation, a steer, a repair —
and to a tool result on its way to becoming a conversation turn.

Split out of test_core.py — a pure move; no assertion was changed. Stdlib-only, no Opus, fast.
    python tests/test_loop.py     (exit 0 = all pass)
"""
import inspect, io, json, os, re, sys, tempfile, time, types, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ctx, _Skip, _RecordingMemory, _ScriptProvider, run_module  # noqa: E402,F401

import contextlib
import inspect, io, json, os, re, sys, tempfile, time, types, warnings

# ------------------------------------------------------------------ loop repro-gate
def test_is_repro_cmd():
    from harness.loop import _is_repro_cmd as R
    yes = ['python -c "assert f()==2"', "python3 repro.py", "py -c 'print(1)'",
           # heredoc / stdin repros — the common self-contained form; unrecognized before, a passing
           # one couldn't reset a stale failure flag so the gate nagged about a phantom failure
           "python 2>&1 <<'EOF'\nimport traceback\nprint('ok')\nEOF", "python3 - <<EOF\nprint(1)\nEOF",
           "cd /x && python <<'PY'\nassert 1==1\nPY",
           "python -m pytest -q", "python -m unittest", "python -m nose",
           "npm test", "go test ./...", "cargo test"]
    no = ["python setup.py test", "pytest --collect-only",
           'ln -sf "$(command -v python3)" /usr/bin/py', "echo python is great"]
    for c in yes: assert R("bash", {"command": c}), "should be repro: %r" % c
    for c in no: assert not R("bash", {"command": c}), "should NOT be repro: %r" % c

def test_repro_failed_by_exit_not_traceback():
    # a reproduction FAILS only if it exited nonzero / the tool errored — never because the output
    # merely contains "Traceback" (a passing repro that tests error handling prints it and exits 0).
    from harness.loop import _repro_failed as F
    assert F("[exit 1]\nTraceback (most recent call last):\nValueError")   # real uncaught -> nonzero
    assert F("[exit 2]\nAssertionError")                                   # assert-mode failure
    assert F("ERROR: edit_file requires string 'old_string'")             # tool-level error
    assert not F("caught it:\nTraceback (most recent call last):\n ...\nALL PASS\n")  # caught, exit 0
    assert not F("imported traceback module; result correct\n")           # word in data, exit 0
    assert not F("42\nverified\n")                                        # clean pass

def test_loop_error_not_answer_not_memory():
    """THE #4 regression lock: an error completion must NOT become res.answer nor enter memory
    (v0.17.0/5328c6a's answer-recovery fallback reintroduced this leak). Fails on that code."""
    from harness.cli import make_harness
    from harness.providers import Completion, Usage
    h = make_harness(os.getcwd(), provider="mock", project="err_leak", embed="hash")
    h.max_turns = 3
    h.memory = _RecordingMemory()
    h.provider = _ScriptProvider([Completion(text="ERROR(deepseek): HTTP 500: boom",
                                             stop_reason="error", error_status=500, error_detail="boom")])
    res = h.run("err_leak", "do it")
    assert res.error and "ERROR(" not in (res.answer or ""), "error must not leak into answer: %r" % res.answer
    assert not any("ERROR(" in m for m in h.memory.remembered), "error must never be consolidated to memory"

def test_loop_retry_transient_then_success():
    """#5 regression lock: a retryable transport error retries (bounded) and recovers — no error,
    answer set, kind='retry' rows logged, nothing appended to the thread on the failed attempts."""
    from unittest.mock import patch
    from harness.cli import make_harness
    from harness.providers import Completion, Usage
    h = make_harness(os.getcwd(), provider="mock", project="retry_ok", embed="hash")
    h.max_turns = 2; h.max_retries = 3; h.retry_base = 1
    err = Completion(text="overloaded", stop_reason="error", error_status=529, error_detail="overloaded_error")
    ok = Completion(text="all good", stop_reason="end_turn", usage=Usage(input_tokens=5))
    h.provider = _ScriptProvider([err, err, ok])
    slept = []
    with patch("time.sleep", lambda s: slept.append(s)):
        res = h.run("retry_ok", "go")
    assert res.error == "" and res.answer == "all good", (res.error, res.answer)
    assert res.model_calls == 3, "physical retry attempts must be budget-visible"
    assert len(slept) == 2, "two retries -> two backoff sleeps: %s" % slept
    assert not any("ERROR(" in m.get("content", "") for m in res.messages if isinstance(m.get("content"), str))


def _contract_error(input_tokens=7, output_tokens=2):
    from harness.providers import Completion, Usage
    return Completion(
        text="ERROR(sidecar): rejected assistant text is intentionally absent",
        stop_reason="error", error_status=422,
        error_code="response_contract_error",
        error_detail=("{\"error\":{\"code\":\"response_contract_error\","
                      "\"message\":\"assistant response was not bridgeable\"}}"),
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens))


def test_loop_repairs_one_response_contract_error_without_backoff():
    from unittest.mock import patch
    from harness.cli import make_harness
    from harness.providers import Completion, Usage

    seen = {}
    def corrected(messages):
        seen["messages"] = messages
        return Completion(text="fixed", stop_reason="end_turn",
                          usage=Usage(input_tokens=5, output_tokens=1))

    h = make_harness(os.getcwd(), provider="mock", project="contract_ok", embed="hash")
    h.max_turns = 1
    h.max_retries = 0
    h.max_contract_repairs = 1
    h.memory = _RecordingMemory()
    p = _ScriptProvider([_contract_error(), corrected])
    h.provider = p

    with patch("time.sleep") as sleep:
        res = h.run("contract_ok", "go")

    assert res.error == "" and res.answer == "fixed"
    assert p.calls == 2 and res.model_calls == 2
    assert res.contract_repairs == 1
    assert res.input_tokens == 12 and res.output_tokens == 3
    assert "previous response could not be parsed" in seen["messages"][-1]["content"]
    durable = json.dumps(res.messages)
    assert "previous response could not be parsed" not in durable
    assert "rejected assistant text" not in durable
    assert not any("response_contract_error" in item for item in h.memory.remembered)
    row = h.recorder.db.execute(
        "SELECT COUNT(*) n FROM turns WHERE run_id=? AND kind='format_repair'",
        (res.run_id,)).fetchone()
    assert row["n"] == 1
    sleep.assert_not_called()


def test_loop_response_contract_repair_is_bounded_and_content_free():
    from unittest.mock import patch
    from harness.cli import make_harness

    h = make_harness(os.getcwd(), provider="mock", project="contract_bounded", embed="hash")
    h.max_turns = 3
    h.max_retries = 3
    h.max_contract_repairs = 1
    p = _ScriptProvider([_contract_error(), _contract_error(),
                         lambda _messages: (_ for _ in ()).throw(
                             AssertionError("third request must not be sent"))])
    h.provider = p

    with patch("time.sleep") as sleep:
        res = h.run("contract_bounded", "go")

    assert p.calls == 2 and res.model_calls == 2
    assert res.contract_repairs == 1
    assert res.error.startswith("protocol:")
    assert "rejected assistant text" not in res.error
    assert not res.answer
    sleep.assert_not_called()


def test_loop_contract_repair_respects_model_call_cap():
    from unittest.mock import patch
    from harness.cli import make_harness

    h = make_harness(os.getcwd(), provider="mock", project="contract_cap", embed="hash")
    h.max_turns = 5
    h.max_model_calls = 1
    h.max_contract_repairs = 1
    p = _ScriptProvider([_contract_error(),
                         lambda _messages: (_ for _ in ()).throw(
                             AssertionError("request beyond cap"))])
    h.provider = p

    with patch("time.sleep") as sleep:
        res = h.run("contract_cap", "go")

    assert p.calls == 1 and res.model_calls == 1
    assert res.contract_repairs == 0
    assert res.error.startswith("protocol:")
    assert not res.answer
    sleep.assert_not_called()


def test_loop_contract_repair_respects_local_token_budget():
    from unittest.mock import patch
    from harness.cli import make_harness

    h = make_harness(os.getcwd(), provider="mock", project="contract_token_cap", embed="hash")
    h.max_turns = 3
    p = _ScriptProvider([_contract_error(input_tokens=7, output_tokens=2)])
    h.provider = p

    with patch.dict(os.environ, {"COLLIE_MAX_TOTAL_TOKENS": "9"}), patch("time.sleep") as sleep:
        res = h.run("contract_token_cap", "go")

    assert p.calls == 1 and res.model_calls == 1
    assert res.contract_repairs == 0 and res.error.startswith("protocol:")
    sleep.assert_not_called()


def test_loop_contract_repair_respects_shared_budget():
    from harness.cli import make_harness

    class SharedBudget:
        def __init__(self):
            self.spent = 0
        def account(self, _model, usage):
            self.spent += usage.input_tokens + usage.output_tokens
        def exceeded(self):
            return self.spent >= 9

    h = make_harness(os.getcwd(), provider="mock", project="contract_shared_cap", embed="hash")
    h.max_turns = 3
    h.shared_budget = SharedBudget()
    p = _ScriptProvider([_contract_error(input_tokens=7, output_tokens=2)])
    h.provider = p

    res = h.run("contract_shared_cap", "go")

    assert p.calls == 1 and res.model_calls == 1
    assert res.contract_repairs == 0 and res.error.startswith("protocol:")


def test_loop_does_not_repair_an_unrelated_http_422():
    from harness.cli import make_harness
    from harness.providers import Completion

    h = make_harness(os.getcwd(), provider="mock", project="generic_422", embed="hash")
    h.max_turns = 3
    p = _ScriptProvider([Completion(
        text="validation failed", stop_reason="error", error_status=422,
        error_detail="ordinary validation failure")])
    h.provider = p

    res = h.run("generic_422", "go")

    assert p.calls == 1 and res.contract_repairs == 0
    assert res.error.startswith("terminal:")


def test_transport_retry_and_contract_repair_use_separate_bounded_policies():
    from unittest.mock import patch
    from harness.cli import make_harness
    from harness.providers import Completion

    overloaded = Completion(text="overloaded", stop_reason="error", error_status=529,
                            error_detail="overloaded_error")
    h = make_harness(os.getcwd(), provider="mock", project="retry_then_contract", embed="hash")
    h.max_turns = 1
    h.max_retries = 1
    h.retry_base = 1
    h.max_contract_repairs = 1
    p = _ScriptProvider([overloaded, _contract_error(), Completion(text="done")])
    h.provider = p

    slept = []
    with patch("time.sleep", lambda seconds: slept.append(seconds)):
        res = h.run("retry_then_contract", "go")

    assert res.answer == "done" and not res.error
    assert p.calls == 3 and res.model_calls == 3
    assert res.contract_repairs == 1 and slept == [1]
    rows = h.recorder.db.execute(
        "SELECT kind, COUNT(*) n FROM turns WHERE run_id=? "
        "AND kind IN ('retry','format_repair') GROUP BY kind", (res.run_id,)).fetchall()
    assert {row["kind"]: row["n"] for row in rows} == {"retry": 1, "format_repair": 1}


def test_loop_model_call_cap_stops_before_retry_or_synthesis():
    """A nested overnight slice must never send request N+1 after N was reserved."""
    from unittest.mock import patch
    from harness.cli import make_harness
    from harness.providers import Completion
    h = make_harness(os.getcwd(), provider="mock", project="call_cap", embed="hash")
    h.max_turns = 5
    h.max_model_calls = 1
    h.max_retries = 3
    h.retry_base = 1
    p = _ScriptProvider([
        Completion(text="overloaded", stop_reason="error", error_status=529,
                   error_detail="overloaded_error"),
        Completion(text="must not be sent", stop_reason="end_turn"),
    ])
    h.provider = p

    with patch("time.sleep") as sleep:
        res = h.run("call_cap", "go")

    assert p.calls == 1
    assert res.model_calls == 1
    assert not res.answer
    sleep.assert_not_called()

def test_loop_terminal_fails_fast():
    from harness.cli import make_harness
    from harness.providers import Completion
    h = make_harness(os.getcwd(), provider="mock", project="term", embed="hash")
    h.max_turns = 3
    p = _ScriptProvider([Completion(text="no", stop_reason="error", error_status=402,
                                    error_detail="Insufficient Balance")])
    h.provider = p
    res = h.run("term", "go")
    assert p.calls == 1, "terminal error must not retry: %d calls" % p.calls
    assert res.error.startswith("terminal:"), res.error

# --- the tool/loop seam ------------------------------------------------------------------------
# Every edit_file test in this file calls `EditFileTool().run(...)` and stops at the tool boundary:
# it asserts the string that came back and that the file is untouched, and then never asks what
# READS that string. The seam where a tool result becomes a conversation turn had no test at all,
# which is where three of the four defects found while reviewing codex/foundation were living.
# These are written against behaviour rather than structure, so they still hold if dispatch later
# moves behind a registry — which is exactly the change that broke it there.

def _tool_result_for(res, call_id):
    return next((m for m in res.messages
                 if m.get("role") == "tool" and m.get("tool_call_id") == call_id), None)

def test_a_recoverable_tool_error_does_not_end_the_run():
    """A stale `old_string` is the commonest slip there is. The model has to get it back and answer.

    Measured on codex/foundation before this existed: the same call raised out of the run and
    killed it, with the whole suite green.
    """
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall, Usage
    d = tempfile.mkdtemp(); p = os.path.join(d, "f.py")
    open(p, "w", encoding="utf-8").write("def f():\n    return 1\n")
    h = make_harness(d, provider="mock", project="toolseam", embed="hash")
    h.max_turns = 3
    h.memory = _RecordingMemory()
    call = ToolCall("tc-1", "edit_file",
                    {"path": "f.py", "old_string": "return 2", "new_string": "return 3"})
    h.provider = _ScriptProvider([
        Completion(tool_calls=[call], stop_reason="tool_use"),
        Completion(text="the file said otherwise, so I looked again",
                   stop_reason="end_turn", usage=Usage(input_tokens=5)),
    ])
    res = h.run("toolseam", "edit it")
    assert res.error == "", "a rejected edit is not a failed run: %r" % res.error
    assert res.answer == "the file said otherwise, so I looked again"
    assert res.tool_calls == 1
    result = _tool_result_for(res, "tc-1")
    assert result is not None, "an unpaired tool_use 400s the provider on the next turn"
    assert "old_string not found" in result["content"], result["content"]
    assert open(p, encoding="utf-8").read() == "def f():\n    return 1\n", "refused, so unchanged"

def test_a_tool_that_raises_does_not_end_the_run_either():
    """A tool exception is the loop's problem to contain, not the run's to die of — and the
    tool_use still has to be paired, or the NEXT provider call is the thing that fails."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall, Usage
    from harness.tools import Tool

    class Detonating(Tool):
        name, tier = "detonate", "always"
        description = "raises"
        schema = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            raise RuntimeError("the disk fell off")

    d = tempfile.mkdtemp()
    h = make_harness(d, provider="mock", project="toolraise", embed="hash")
    h.registry.register(Detonating())
    h.max_turns = 3
    h.memory = _RecordingMemory()
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall("tc-2", "detonate", {})], stop_reason="tool_use"),
        Completion(text="noted, doing it another way", stop_reason="end_turn",
                   usage=Usage(input_tokens=5)),
    ])
    res = h.run("toolraise", "go")
    assert res.error == "", res.error
    assert res.answer == "noted, doing it another way"
    result = _tool_result_for(res, "tc-2")
    assert result is not None and "the disk fell off" in result["content"], result
    assert result["content"].startswith("ERROR"), "the model needs to see it as a failure"

def test_an_unknown_tool_name_is_answerable_not_fatal():
    """A model naming a tool that does not exist is a mistake it can recover from in one turn."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall, Usage
    d = tempfile.mkdtemp()
    h = make_harness(d, provider="mock", project="toolmissing", embed="hash")
    h.max_turns = 3
    h.memory = _RecordingMemory()
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall("tc-3", "no_such_tool", {})], stop_reason="tool_use"),
        Completion(text="used a real one instead", stop_reason="end_turn",
                   usage=Usage(input_tokens=5)),
    ])
    res = h.run("toolmissing", "go")
    assert res.error == "" and res.answer == "used a real one instead"
    result = _tool_result_for(res, "tc-3")
    assert result is not None and "no such tool" in result["content"], result

def test_loop_overflow_recovers():
    """#9: a context-overflow error triggers a one-shot shrink+retry; the run recovers instead of
    dying. Exactly one kind='overflow' turn."""
    from harness.cli import make_harness
    from harness.providers import Completion, Usage
    h = make_harness(os.getcwd(), provider="mock", project="ovf", embed="hash")
    h.max_turns = 3
    ovf = Completion(text="prompt is too long: 300000 tokens > 200000 maximum", stop_reason="error",
                     error_status=400, error_detail="prompt is too long: 300000 tokens > 200000 maximum")
    ok = Completion(text="recovered", stop_reason="end_turn", usage=Usage(input_tokens=5))
    p = _ScriptProvider([ovf, ok])
    h.provider = p
    res = h.run("ovf", "go")
    assert res.error == "" and res.answer == "recovered", (res.error, res.answer)
    rows = h.recorder.db.execute("SELECT COUNT(*) c FROM turns WHERE run_id=? AND kind='overflow'",
                                 (res.run_id,)).fetchone()
    assert rows["c"] == 1, "exactly one overflow-recovery turn: %s" % rows["c"]

def test_loop_overflow_exactly_once():
    from harness.cli import make_harness
    from harness.providers import Completion
    h = make_harness(os.getcwd(), provider="mock", project="ovf2", embed="hash")
    h.max_turns = 4
    ovf = Completion(text="maximum context length exceeded", stop_reason="error",
                     error_status=400, error_detail="maximum context length is 65536 tokens")
    p = _ScriptProvider([ovf])   # always overflow
    h.provider = p
    res = h.run("ovf2", "go")
    assert res.error.startswith("overflow:"), res.error
    assert p.calls == 2, "recover ONCE then give up (1 original + 1 retry): %d" % p.calls

def test_loop_overflow_env_off():
    from harness.cli import make_harness
    from harness.providers import Completion
    old = os.environ.get("COLLIE_OVERFLOW_RECOVERY")
    os.environ["COLLIE_OVERFLOW_RECOVERY"] = "0"
    try:
        h = make_harness(os.getcwd(), provider="mock", project="ovf_off", embed="hash")
        h.max_turns = 3
        p = _ScriptProvider([Completion(text="prompt is too long", stop_reason="error",
                                        error_status=400, error_detail="prompt is too long")])
        h.provider = p
        res = h.run("ovf_off", "go")
        assert p.calls == 1, "recovery OFF -> no retry: %d" % p.calls
        assert res.error, "overflow with recovery off must fail"
    finally:
        if old is None: os.environ.pop("COLLIE_OVERFLOW_RECOVERY", None)
        else: os.environ["COLLIE_OVERFLOW_RECOVERY"] = old

def test_loop_fails_truncated_toolcalls():
    """#1: a stop_reason='length' turn with tool calls must execute NONE of them; each gets a
    'not executed' result, the file is untouched, pairing holds, and did_edit stays False."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall, AnthropicProvider, Usage
    d = tempfile.mkdtemp(); fp = os.path.join(d, "t.py")
    open(fp, "w").write("x = 1\n")
    h = make_harness(d, provider="mock", project="trunc", embed="hash")
    h.max_turns = 2
    trunc = Completion(tool_calls=[ToolCall("c1", "edit_file", {"path": fp, "old_string": "x = 1", "new_string": ""})],
                       stop_reason="length")
    ok = Completion(text="ok", stop_reason="end_turn", usage=Usage(input_tokens=3))
    h.provider = _ScriptProvider([trunc, ok])
    res = h.run("trunc", "fix")
    assert open(fp).read() == "x = 1\n", "truncated edit must NOT be executed"
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert tool_msgs and "not executed" in tool_msgs[0]["content"], "must tell the model it was truncated"
    an = AnthropicProvider.__new__(AnthropicProvider)._to_anthropic(res.messages)
    seen = set()
    for m in an:
        c = m["content"]
        if isinstance(c, list):
            for b in c:
                if b.get("type") == "tool_use": seen.add(b["id"])
                if b.get("type") == "tool_result":
                    assert b["tool_use_id"] in seen, "orphaned tool_result after truncation guard"

def test_loop_truncated_answer_marker_and_bound():
    """#1: a truncated plain answer gets a marker and is NOT consolidated; an every-turn-length run
    hits the trunc_rounds bound instead of spinning."""
    from harness.cli import make_harness
    from harness.providers import Completion, Usage
    h = make_harness(os.getcwd(), provider="mock", project="trunc2", embed="hash")
    h.max_turns = 5
    h.memory = _RecordingMemory()
    h.provider = _ScriptProvider([Completion(text="partial ans", stop_reason="length", usage=Usage(input_tokens=3))])
    res = h.run("trunc2", "explain")
    assert "truncated at output-token limit" in (res.answer or ""), res.answer
    assert not h.memory.remembered, "a length-stopped answer must not be consolidated to memory"

def test_loop_truncation_escalates_max_tokens():
    # the fix for the "output-limit truncation loop": retrying at the SAME output ceiling truncates
    # forever, so each length-stop doubles the cap (bounded) to give the retry real room to finish.
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall, Usage
    d = tempfile.mkdtemp(); fp = os.path.join(d, "t.py"); open(fp, "w").write("x = 1\n")
    h = make_harness(d, provider="mock", project="esc", embed="hash")
    h.max_turns = 6
    trunc = Completion(tool_calls=[ToolCall("c", "edit_file", {"path": fp, "old_string": "x = 1", "new_string": "x = 2"})],
                       stop_reason="length")
    prov = _ScriptProvider([trunc, trunc, trunc, Completion(text="ok", stop_reason="end_turn", usage=Usage(input_tokens=3))])
    assert prov.max_tokens == 4096
    h.provider = prov
    h.run("esc", "fix")
    assert prov.max_tokens > 4096, "each length-stop must escalate the output ceiling, got %d" % prov.max_tokens

def test_judge_error_completion_neutral():
    from harness.judge import judge_quality
    from harness.providers import Completion
    class P:
        def complete(self, s, m, t, on_text=None):
            return Completion(text="ERROR(x): HTTP 429 too many requests", stop_reason="error")
    q = judge_quality(P(), "task", "some answer", True)
    assert q == 5.0, "an errored judge call must be neutral 5.0, not read '429' as a 10: %s" % q

# ==================== Batch D: arg-repair layer (#7) · steering queue (#13) ====================

def test_repair_args_schema_coercion():
    from harness.tools import repair_args
    plan_schema = {"type": "object", "properties": {"todos": {"type": "array"}}, "required": ["todos"]}
    out, notes = repair_args({"todos": '[{"content":"x"}]'}, plan_schema)
    assert out["todos"] == [{"content": "x"}] and notes == ["json_str:todos"], (out, notes)
    # a declared STRING field must NOT be json-parsed (key safety invariant)
    wf = {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}
    out2, n2 = repair_args({"content": '["x"]'}, wf)
    assert out2["content"] == '["x"]' and n2 == [], "string field must stay a string"
    # unparseable + type-mismatch strings left untouched
    out3, n3 = repair_args({"todos": "not json"}, plan_schema)
    assert out3["todos"] == "not json" and n3 == [], "unparseable array field left for the tool's error"

def test_repair_args_alias():
    from harness.tools import repair_args
    ef = {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"},
          "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}
    out, notes = repair_args({"file_path": "f.py", "old_string": "a", "new_string": "b"}, ef)
    assert out["path"] == "f.py" and "file_path" not in out and "alias:file_path->path" in notes
    # both present -> untouched (never overwrite)
    out2, n2 = repair_args({"path": "keep", "file_path": "x", "old_string": "a", "new_string": "b"}, ef)
    assert out2["path"] == "keep" and n2 == []

def test_repair_args_identity():
    from harness.tools import repair_args
    ef = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    a = {"path": "f.py"}
    out, notes = repair_args(a, ef)
    assert out is a and notes == [], "well-formed args must pass through by identity, no churn"
    assert repair_args("not a dict", ef) == ({}, ["non_dict"])

def test_loop_repair_end_to_end():
    """#7 regression lock: a string-wrapped array arg is repaired before dispatch (today it errors
    'must be an array'), the raw session copy is preserved (replay fidelity), pairing intact."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    h = make_harness(os.getcwd(), provider="mock", project="repair", embed="hash")
    h.max_turns = 2
    tc = ToolCall("c1", "plan", {"todos": '[{"content":"a","status":"completed"}]'})
    h.provider = _ScriptProvider([Completion(tool_calls=[tc], stop_reason="tool_use"),
                                  Completion(text="done", stop_reason="end_turn")])
    res = h.run("repair", "make a plan")
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert tool_msgs and not tool_msgs[0]["content"].startswith("ERROR"), "repaired plan must not error: %r" % tool_msgs[0]["content"]
    assert res.arg_repairs == 1 and tool_msgs[0].get("repairs") == ["json_str:todos"]
    # replay fidelity: the SAVED assistant tool_call keeps the model's RAW string-wrapped arg
    asst = [m for m in res.messages if m.get("role") == "assistant" and m.get("tool_calls")]
    raw = asst[0]["tool_calls"][0]
    raw_args = raw.args if hasattr(raw, "args") else raw["args"]
    assert raw_args["todos"] == '[{"content":"a","status":"completed"}]', "raw args must be preserved for replay"

def test_malformed_args_sentinel():
    """#7: a provider sentinel for malformed JSON args must yield an actionable 'not valid JSON'
    error, NOT a misleading 'missing required arg'."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    h = make_harness(os.getcwd(), provider="mock", project="malformed", embed="hash")
    h.max_turns = 2
    tc = ToolCall("c1", "edit_file", {"_malformed_args": '{"path": "f.py", "old'})
    h.provider = _ScriptProvider([Completion(tool_calls=[tc], stop_reason="tool_use"),
                                  Completion(text="ok", stop_reason="end_turn")])
    res = h.run("malformed", "edit")
    tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
    assert "not valid JSON" in tool_msgs[0]["content"], tool_msgs[0]["content"]
    assert "missing required" not in tool_msgs[0]["content"], "must not misdiagnose as missing arg"

def test_steering_injected_at_safe_point():
    """#13 regression lock: mid-run steering appears as a user message after a tool result and
    before the final answer; steer_count and a kind='steer' turn row are set."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    h = make_harness(os.getcwd(), provider="mock", project="steer", embed="hash")
    h.max_turns = 4
    calls = {"n": 0}
    def steer():
        calls["n"] += 1
        return ["actually check utils.py"] if calls["n"] == 2 else []   # fire on the 2nd drain (turn 1)
    h.steering = steer
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall("t0", "bash", {"command": "ls"})], stop_reason="tool_use"),
        Completion(tool_calls=[ToolCall("t1", "bash", {"command": "pwd"})], stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn")])
    res = h.run("steer", "poke")
    roles = [m.get("role") for m in res.messages]
    contents = [m.get("content") for m in res.messages]
    assert "actually check utils.py" in contents, "steer text must be injected"
    idx = contents.index("actually check utils.py")
    assert res.messages[idx]["role"] == "user" and "tool" in roles[:idx], "steer must land after a tool msg"
    assert res.steer_count == 1
    rows = h.recorder.db.execute("SELECT COUNT(*) c FROM turns WHERE run_id=? AND kind='steer'",
                                 (res.run_id,)).fetchone()
    assert rows["c"] == 1

def test_steering_default_none_identical():
    """The benchmark path (steering unset) must be byte-identical to steering wired but idle, and
    the None path must never invoke a callback."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    def build():
        h = make_harness(os.getcwd(), provider="mock", project="steer_id", embed="hash")
        h.max_turns = 3
        h.provider = _ScriptProvider([
            Completion(tool_calls=[ToolCall("t0", "bash", {"command": "ls"})], stop_reason="tool_use"),
            Completion(text="done", stop_reason="end_turn")])
        return h
    h1 = build()                     # steering never set
    r1 = h1.run("steer_id", "go")
    h2 = build()
    called = {"n": 0}
    h2.steering = lambda: (called.__setitem__("n", called["n"] + 1), [])[1]   # wired but idle
    r2 = h2.run("steer_id", "go")
    assert r1.steer_count == 0 and r2.steer_count == 0
    assert [m.get("content") for m in r1.messages] == [m.get("content") for m in r2.messages], "benchmark path must be byte-identical"

def test_web_steer_registry():
    """Web transport for #13: /api/steer pushes onto a per-session queue that the run's h.steering
    drains. push before open (no active run) and after close must both fail; between, it queues."""
    from harness.webapp import Handler
    import queue
    sid = "web-steer-test"
    assert Handler._steer_push(sid, "before") is False, "no active run -> not queued"
    q = Handler._steer_open(sid)
    assert Handler._steer_push(sid, "one") is True
    assert Handler._steer_push(sid, "two") is True
    drained = []
    while True:
        try: drained.append(q.get_nowait())
        except queue.Empty: break
    assert drained == ["one", "two"], drained
    Handler._steer_close(sid)
    assert Handler._steer_push(sid, "after") is False, "run over -> not queued"

def test_steering_callable_raises_safe():
    from harness.cli import make_harness
    from harness.providers import Completion
    h = make_harness(os.getcwd(), provider="mock", project="steer_raise", embed="hash")
    h.max_turns = 2
    h.steering = lambda: 1 / 0        # a broken callback must not crash the run
    h.provider = _ScriptProvider([Completion(text="ok", stop_reason="end_turn")])
    res = h.run("steer_raise", "go")
    assert res.answer == "ok" and not res.error

def test_stdin_feed():
    from harness.tui import _StdinFeed
    feed = _StdinFeed(io.StringIO("look at utils\n/exit\n"))
    feed._t.join(timeout=2)           # let the pump finish reading the fake stream
    steer = feed.drain()
    assert steer == ["look at utils"], "drain returns non-slash lines as steering: %s" % steer
    # the slash line + EOF sentinel are re-queued for the REPL prompt to consume
    assert feed.readline_blocking() == "/exit", "slash command deferred to the prompt, not injected"
    assert feed.readline_blocking() is None, "EOF sentinel preserved"
    assert feed.tty is False, "a StringIO is not a tty -> steering wiring skipped in run_tui"

def test_running_out_of_turns_is_not_reported_as_done():
    """A run cut off mid-task must not answer with the word "done".

    Measured on a real task with a two-turn budget: six runs out of six ended with the loop's
    placeholder, `(done — see the edits/tools above)`, having made an edit and never run a single
    check — in the verify-gated mode too, because running out of turns leaves the loop from outside
    the gate. The cost ceiling had always appended a "stopped" note; the turn ceiling appended
    nothing, so the two endings were indistinguishable to a reader and one of them lied.
    """
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    # a provider that never stops asking for tools -> the loop can only end by exhausting turns
    always_tool = Completion(text="", stop_reason="tool_use",
                             tool_calls=[ToolCall("t1", "bash", {"command": "echo hi"})])
    h = make_harness(os.getcwd(), provider="mock", project="exhaust", embed="hash")
    h.max_turns = 2
    h.provider = _ScriptProvider([always_tool, always_tool,
                                  Completion(text="", stop_reason="end_turn")])
    res = h.run("exhaust", "do something that cannot finish in two turns")
    ans = (res.answer or "") + " " + (res.error or "")
    assert "ran out of turns" in ans, \
        "an exhausted run must say so; got %r" % ans[:160]
    assert not re.match(r"^\(done\b", (res.answer or "").strip()), \
        "an exhausted run must not open with 'done': %r" % (res.answer or "")[:80]

# ------------------------------------------------------------------ loop: answer recovery + no orphan
def test_loop_recovers_answer_and_no_orphan():
    from harness.cli import make_harness
    from harness.providers import AnthropicProvider
    h = make_harness(os.getcwd(), provider="mock", project="loop1")
    h.max_turns = 1                       # 1 turn -> mock's turn0 is a tool call -> loop exhausts
    res = h.run("loop1", "list files")
    assert (res.answer or "").strip(), "loop must NOT return an empty answer when it exhausts on a tool call"
    # the saved thread must be a VALID sequence — every tool_result preceded by its tool_use
    an = AnthropicProvider.__new__(AnthropicProvider)._to_anthropic(res.messages)
    seen = set()
    for m in an:
        c = m["content"]
        if isinstance(c, list):
            for b in c:
                if b.get("type") == "tool_use": seen.add(b["id"])
                if b.get("type") == "tool_result":
                    assert b["tool_use_id"] in seen, "orphaned tool_use -> provider 400 on --continue"

def test_budget_stops_early():
    # a tiny token ceiling must break the loop early and annotate the answer, without a synthesis turn
    from harness.cli import make_harness
    os.environ["COLLIE_MAX_TOTAL_TOKENS"] = "100"
    try:
        h = make_harness(os.getcwd(), provider="mock", project="budget"); h.max_turns = 10
        res = h.run("budget", "list files and summarize each")
        assert res.turns < 10, "budget ceiling must stop the loop early (got %d turns)" % res.turns
        assert "budget ceiling reached" in (res.answer or ""), "answer must note the budget stop"
    finally:
        os.environ.pop("COLLIE_MAX_TOTAL_TOKENS", None)

def test_budget_off_by_default():
    # 0 / unset ceiling must NOT stop early
    from harness import loop as L
    assert L._budget_exceeded("claude-opus-4-8", None) is False
    os.environ.pop("COLLIE_MAX_COST", None); os.environ.pop("COLLIE_MAX_TOTAL_TOKENS", None)
    class T:  # minimal total-usage stand-in
        input_tokens = 10**9; output_tokens = 10**9
    assert L._budget_exceeded("claude-opus-4-8", T()) is False, "no ceiling set -> never exceeded"

def test_subscription_loop_ignores_list_price_cost_cap_but_keeps_token_cap(monkeypatch):
    from harness import loop as L

    class T:
        input_tokens = 1_000_000
        output_tokens = 0
        cache_read = 0
        cache_creation = 0

    monkeypatch.setenv("COLLIE_MAX_COST", "0.01")
    monkeypatch.delenv("COLLIE_MAX_TOTAL_TOKENS", raising=False)
    assert L._budget_exceeded("claude-opus-4-8", T(), subscription_only=False) is True
    assert L._budget_exceeded("claude-opus-4-8", T(), subscription_only=True) is False

    monkeypatch.setenv("COLLIE_MAX_TOTAL_TOKENS", "100")
    assert L._budget_exceeded("claude-opus-4-8", T(), subscription_only=True) is True

def test_loop_whiteflag_rescue_and_restore():
    """sphinx-10435 regression lock: a model that edits, REVERTS itself, then insists on
    finishing must (a) get one ROLLBACK_NUDGE rescue turn, and (b) when it still finishes with
    an empty tree, have the last non-empty edit state mechanically restored — an empty patch
    is a guaranteed zero, a restored partial fix can still score."""
    import subprocess, tempfile
    from harness.cli import make_harness
    from harness.providers import Completion, Usage, ToolCall
    wd = tempfile.mkdtemp(prefix="whiteflag_")
    subprocess.run(["git", "init", "-q", wd], check=True)
    subprocess.run(["git", "-C", wd, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", wd, "config", "user.name", "t"], check=True)
    p = os.path.join(wd, "f.py")
    open(p, "w").write("x = 1\n")
    subprocess.run(["git", "-C", wd, "add", "-A"], check=True)
    subprocess.run(["git", "-C", wd, "commit", "-qm", "base"], check=True)

    nudged = []
    def _finish(messages):
        nudged.append(any("ZERO net changes" in str(m.get("content", "")) for m in messages))
        return Completion(text="done — no change needed", stop_reason="end_turn", usage=Usage())
    edit = Completion(tool_calls=[ToolCall("c1", "edit_file",
                      {"path": p, "old_string": "x = 1", "new_string": "x = 2"})],
                      usage=Usage(), stop_reason="tool_use")
    revert = Completion(tool_calls=[ToolCall("c2", "edit_file",
                        {"path": p, "old_string": "x = 2", "new_string": "x = 1"})],
                        usage=Usage(), stop_reason="tool_use")
    h = make_harness(wd, provider="mock", project="whiteflag", embed="hash")
    h.max_turns = 8
    h.memory = _RecordingMemory()
    h.force_edit = True
    h.self_verify = False                      # isolate the white-flag path from verify gates
    h.provider = _ScriptProvider([edit, revert, _finish, _finish])
    res = h.run("whiteflag", "fix the bug in f.py")
    # finish attempt #1 eats the advisory COVERAGE_NUDGE, #2 the ROLLBACK_NUDGE, #3 lands:
    # edit + revert + 3 finish attempts = 5 completions, and only the LAST carries the rescue.
    assert h.provider.calls == 5, "expected coverage+rescue turns (5 completions), got %d" % h.provider.calls
    assert nudged[0] is False and nudged[-1] is True, \
        "last finish must carry ROLLBACK_NUDGE, first must not: %r" % nudged
    assert "x = 2" in open(p).read(), "empty tree at finish must be restored to the last edit"
    assert not res.error

def test_verify_gate_is_not_python_only():
    """The finish-gate must see evidence in whatever language the repo is written in.

    Both repro regexes matched only `python`/`py`, so on a Go or JS repo the gate saw NO evidence
    whatever the agent ran: it nagged for verify_max rounds with a `python3 -c` instruction that
    could not be satisfied, then let the agent finish anyway. SWE-bench Pro is 280 go / 266 python
    / 165 js / 20 ts, and on its flipt instance Collie shipped a patch whose test package did not
    even COMPILE. Necessary but not sufficient: a build must count as evidence, and must NOT count
    as a correctness assertion.
    """
    from harness.loop import _is_repro_cmd, _is_asserting_cmd
    for cmd in ("go build ./...", "go vet ./...", "cargo check",
                "npx tsc --noEmit", "node --check src/a.js",
                "go test -run '^TestXxVerify$' ./internal/config"):
        assert _is_repro_cmd("bash", {"command": cmd}), "not counted as evidence: %s" % cmd
    # A real suite run is stronger executable evidence and is exactly what the default nudge asks
    # for. Discovery-only modes must not claim a pass.
    for cmd in ("go test ./...", "npm test", "pytest -q", "cargo test"):
        assert _is_repro_cmd("bash", {"command": cmd}), "suite run missed: %s" % cmd
        assert _is_asserting_cmd(cmd), "suite not counted as asserting: %s" % cmd
    assert not _is_repro_cmd("bash", {"command": "pytest --collect-only"})
    # building proves it compiles, never that it is correct
    assert not _is_asserting_cmd("go build ./...")
    assert not _is_asserting_cmd("npx tsc --noEmit")
    assert not _is_asserting_cmd("python -c \"print('assert')\"")
    assert not _is_asserting_cmd("python -c \"print('&& pytest')\"")
    assert _is_repro_cmd("bash", {"command": "python -c \"print('&& pytest')\""})
    assert not _is_asserting_cmd("echo pytest")
    for cmd in ("go test -run '^TestX$' ./p", "python3 -c 'assert a == b'",
                 "npx jest test/foo.test.ts"):
        assert _is_asserting_cmd(cmd), "assertion not recognised: %s" % cmd

def test_verify_nudge_names_the_repos_own_toolchain():
    """A Go agent told to run `python3 -c` is being told to verify nothing."""
    from harness import swe
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "go.mod"), "w").close()
        assert swe.detect_language(d) == "go"
        n = swe._swe_assert_verify_nudge("go")
        assert "go build" in n and "python3" not in n
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "package.json"), "w").close()
        open(os.path.join(d, "tsconfig.json"), "w").close()
        assert swe.detect_language(d) == "ts"
        assert "tsc" in swe._swe_assert_verify_nudge("ts")
    # python keeps the tuned wording verbatim — rewording it would silently re-run that experiment
    assert swe._swe_assert_verify_nudge("python") == swe._SWE_ASSERT_VERIFY_NUDGE
    # an unknown language must not silently fall back to python
    assert "python3" not in swe._swe_assert_verify_nudge("")

if __name__ == "__main__":                 # LAST, always: a guard with definitions after it
    sys.exit(run_module(globals(), "LOOP"))  # silently skips every one of them.
