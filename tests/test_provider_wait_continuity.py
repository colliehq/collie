"""A run stopped on a provider quota reset stays a wait — across the receipt and back.

`loop.py` already learns the reset: an upstream rate-limit error carrying a validated
future timestamp sets `RunResult.retry_at` and emits `provider_wait`, and Mission persists
its own durable timer from it. Ordinary CLI and web runs went through `recorder.run_outcome`
instead, which dropped the field — so the shared terminal frame and the durable run receipt
both lost the one fact that explains the ending, and every surface reading them could only
say "failed" with an HTTP body attached.

These tests exercise the real boundary — `run_outcome` into `sessions.append_run_receipt`
and back out of `sessions.load` — rather than the dataclass alone, because losing the field
on the way to disk is exactly the bug. They also pin the two things a wait must never
become: a claim that something is scheduled, and a disguise for an ending that was really
a cancellation, a spent budget, or a host-side failure recorded after the run.

Offline: no provider, no network, no clock beyond an injected `now`.
"""
import json
import math
import os
import time

import pytest

from harness import sessions
from harness.recorder import (PROVIDER_WAIT_HORIZON, RunResult, note_host_error,
                              provider_wait_at, provider_wait_state, run_outcome,
                              run_stop_reason)

NOW = 1_800_000_000          # 2027-01-15 UTC — fixed, so nothing here depends on the wall clock
RESET = NOW + 3600


def waited(**kw):
    """A run that ended exactly the way `loop.py` ends one on an attested quota reset."""
    fields = {"retry_at": RESET,
              "error": "retryable: [provider quota reset at 2027-01-15 09:00:00 UTC] "
                       "HTTP 429 rate limit"}
    fields.update(kw)
    return RunResult(**fields)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    os.makedirs(tmp_path / "sessions", exist_ok=True)
    return str(tmp_path / "sessions")


# --------------------------------------------------------------- the value itself
def test_only_a_real_epoch_grants_a_wait():
    """What decides whether a person is told to wait is not allowed to be almost-a-number.

    `True` is not 1, a float is not an epoch, "1800003600" is a string, and an epoch old
    enough to predate the product cannot be a live quota window.
    """
    assert provider_wait_at(RESET, now=NOW) == RESET
    for bad in (True, False, None, "1800003600", 1800003600.0, float("nan"),
                float("inf"), -float("inf"), [RESET], 0, -1, 1, 1_000_000_000):
        assert provider_wait_at(bad, now=NOW) == 0, bad
    # Beyond the horizon the upstream validator uses, an "eventual" reset is not a wait.
    assert provider_wait_at(NOW + PROVIDER_WAIT_HORIZON, now=NOW) == NOW + PROVIDER_WAIT_HORIZON
    assert provider_wait_at(NOW + PROVIDER_WAIT_HORIZON + 1, now=NOW) == 0


def test_a_reset_already_reached_reads_as_ready_not_as_absent():
    """The durable receipt is read back later — that is the whole point of writing it.

    `providers.provider_retry_at` refuses a past reset, and must: it gates whether to stop
    calling. A receipt is the other reading, and dropping it would make a thread reopened
    the next morning say "failed" about a limit that has since lifted.
    """
    from harness.providers import provider_retry_at
    assert provider_retry_at(RESET, now=RESET + 60) == 0
    assert provider_wait_at(RESET, now=RESET + 60) == RESET


# ------------------------------------------------- the shared outcome boundary
def test_run_outcome_carries_the_wait_and_says_nothing_finished():
    out = run_outcome(waited(), now=NOW)
    assert out["provider_wait"] is True and out["retry_at"] == RESET
    assert out["completed"] is False and out["stop_reason"] == "error"
    assert out["canceled"] is False


def test_a_run_that_did_not_wait_carries_no_wait_keys():
    """Absent, not false: a legacy receipt and an ordinary ending must read the same."""
    for res in (RunResult(answer="done"),
                RunResult(error="boom"),                       # an error with no reset
                waited(retry_at=0),
                waited(retry_at=True),                         # a bool never grants one
                waited(retry_at=1_500_000_000)):               # an epoch below the live floor
        out = run_outcome(res, now=NOW)
        assert "provider_wait" not in out and "retry_at" not in out, res


def test_every_other_verdict_outranks_the_wait():
    """A reset left on the result explains nothing when something else ended the run."""
    for res in (waited(canceled=True),
                waited(budget_exhausted=True, error=""),
                waited(turns_exhausted=True, error=""),
                waited(error="", stop_reason="output_limit"),
                waited(error="", stop_reason="")):             # completed
        out = run_outcome(res, now=NOW)
        assert "provider_wait" not in out, (run_stop_reason(res), out)
    # ...and the surviving verdict is still the real one.
    assert run_outcome(waited(canceled=True), now=NOW)["stop_reason"] == "canceled"
    assert run_outcome(waited(budget_exhausted=True, error=""),
                       now=NOW)["stop_reason"] == "budget_limit"


def test_a_recovery_fence_outranks_the_wait_in_every_row_that_composes_one():
    """A tool may already have fired outside the process. "Come back at 14:00" is not that.

    `RunResult` cannot answer this on its own — the fence lives in the session journal and
    in the worker's settlement report — so it is an argument, and each host passes what it
    knows. These are the rows that reach a person: the durable receipt, the live `done`
    frame and the scheduler's view.
    """
    assert provider_wait_state(waited(), now=NOW, recovery_required=True) is None
    fenced = run_outcome(waited(), now=NOW, recovery_required=True)
    assert "provider_wait" not in fenced and "retry_at" not in fenced
    assert fenced["stop_reason"] == "error", "the ending itself is unchanged"
    # The fence is the caller's key to write; `run_outcome` only stops claiming a wait.
    assert "recovery_required" not in fenced
    assert run_outcome(waited(), now=NOW)["provider_wait"] is True, "unfenced is still a wait"


def test_the_scheduler_row_drops_the_wait_when_the_thread_is_fenced():
    from harness.web_tasks import terminal_outcome
    soon = int(time.time()) + 3600
    row = terminal_outcome(waited(retry_at=soon), recovery_required=True)
    assert "provider_wait" not in row and "retry_at" not in row
    assert row["recovery_required"] is True and row["auto_next"] is False
    assert terminal_outcome(waited(retry_at=soon))["provider_wait"] is True


def test_every_host_composition_site_passes_its_fence_in():
    """The blocker was one call site out of several. Pin all of them at the source.

    A `run_outcome(res)` with no fence argument in a host that composes a durable receipt
    or a terminal frame is exactly how a fenced thread starts reading as "just early"
    again, and a unit test of the function alone would not see it.
    """
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("webapp.py", "web_tasks.py"):
        text = open(os.path.join(here, "harness", name), encoding="utf-8").read()
        bare = re.findall(r"run_outcome\(res\)|run_outcome\(res,\s*now=", text)
        assert not bare, "%s composes an outcome without its recovery fence: %r" % (name, bare)
        assert "recovery_required=" in text
    cli = open(os.path.join(here, "harness", "cli.py"), encoding="utf-8").read()
    assert "run_outcome(res, recovery_required=receipt_fenced)" in cli, "durable CLI receipt"
    # The printed verdict passes the CONSERVATIVE reading, not the bare fact: `fenced` is
    # what the row states about the thread, `wait_fenced` is what it may still claim about
    # the clock, and an unreadable journal separates the two.
    assert "run_outcome(res, recovery_required=wait_fenced)" in cli, "the printed CLI verdict"
    assert "wait_fenced = fenced or not recovery_known" in cli, "unknown withholds the wait"
    assert not re.search(r"run_outcome\(res\)", cli)


def test_the_cli_reads_its_fence_from_the_journal_for_its_own_runs(store):
    """Where `recovery_required` actually comes from in the CLI, for the durable receipt.

    An in-process run's interrupted tool is already on disk when the receipt is composed,
    so the journal answers. An EXTERNAL worker's replay fence is armed before launch and
    retired after the save, so the journal would call every external run fenced while the
    fence is still its own; that branch reports nothing and the caller uses the worker's
    settlement report instead.
    """
    from harness.cli import _session_fenced

    sid = "fenced-thread"
    sessions.checkpoint(sid, [{"role": "user", "content": "rotate the keys"}],
                        run_id="r-1", state="executing_tool",
                        detail={"tool": "run_shell", "args": {"cmd": "./rotate.sh"}})
    assert _session_fenced(sessions, sid) is True
    assert _session_fenced(sessions, sid, external=True) is False, "not the journal's to say"

    sessions.checkpoint(sid, [{"role": "user", "content": "rotate the keys"}],
                        run_id="r-1", terminal=True)
    assert _session_fenced(sessions, sid) is False

    class Refuses:
        def recovery_state(self, sid):
            raise OSError("journal unreadable")
    assert _session_fenced(Refuses(), sid) is False, "an unreadable journal claims nothing"


def test_a_host_failure_after_the_run_is_not_an_ordinary_wait():
    """The transcript would not save. That is not something waiting an hour fixes.

    `note_host_error` is the one door every post-run failure goes through — a required
    check's verdict, a receipt or transcript that would not persist, a boundary that would
    not close — so the wait is withdrawn structurally, not by sniffing the error string.
    """
    res = waited()
    assert run_outcome(res, now=NOW)["provider_wait"] is True
    note_host_error(res, "session transcript could not be persisted: OSError: disk full")
    out = run_outcome(res, now=NOW)
    assert "provider_wait" not in out and "retry_at" not in out
    assert out["stop_reason"] == "error"
    assert "disk full" in res.error and "rate limit" in res.error, "both facts are kept"
    assert res.retry_at == RESET, "the reset stays on the result; only the reading is withdrawn"


def test_note_host_error_appends_the_way_the_hosts_always_did():
    res = RunResult()
    assert note_host_error(res, "") == ""
    note_host_error(res, "first")
    note_host_error(res, "second")
    assert res.error == "first; second" and res.host_error is True


def test_the_cli_and_web_hosts_route_their_post_run_failures_through_it():
    """A new append site that forgets this would silently re-open the disguise."""
    import re
    raw = re.compile(r'\.error = \(\(res\.error \+ "; "\) if res\.error else ""\)')
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("cli.py", "webapp.py"):
        text = open(os.path.join(here, "harness", name), encoding="utf-8").read()
        assert not raw.search(text), "%s appends a host error without retiring the wait" % name
        assert "note_host_error" in text


# --------------------------------------------- persistence and reload, for real
def test_the_wait_survives_the_receipt_and_is_re_read_against_the_clock(store):
    sid = "wait-thread"
    sessions.save(sid, [{"role": "user", "content": "migrate the loader"}])
    receipt = {**run_outcome(waited(), now=NOW), "run": "r-wait", "error": waited().error,
               "decision": {"intent": "build", "verification": "auto"}}
    assert sessions.append_run_receipt(sid, receipt) is True

    loaded = sessions.load(sid)
    row = loaded["run_receipts"][-1]
    assert row["provider_wait"] is True and row["retry_at"] == RESET
    assert type(row["retry_at"]) is int, "a JSON round trip must not soften the type"

    # The same durable row, read at two different moments, is a wait and then a readiness.
    assert provider_wait_at(row["retry_at"], now=NOW) == RESET
    assert provider_wait_at(row["retry_at"], now=RESET + 1) == RESET
    assert provider_wait_at(row["retry_at"], now=NOW - 10 * 86400) == 0, "not yet plausible"


def test_a_legacy_receipt_without_the_field_is_simply_not_a_wait(store):
    """Receipts written before this existed stay readable and stay silent."""
    sid = "old-thread"
    sessions.save(sid, [{"role": "user", "content": "old run"}])
    sessions.append_run_receipt(sid, {"run": "r-old", "stop_reason": "error",
                                      "completed": False, "canceled": False,
                                      "error": "provider error"})
    row = sessions.load(sid)["run_receipts"][-1]
    assert "provider_wait" not in row and "retry_at" not in row


def test_a_hostile_receipt_value_cannot_grant_a_wait_on_reload(store):
    """Anything that reached the file by another route is re-validated on the way out."""
    sid = "hostile-thread"
    sessions.save(sid, [{"role": "user", "content": "x"}])
    for value in (True, "1800003600", 1800003600.5, 1, None):
        sessions.append_run_receipt(sid, {"run": "r", "stop_reason": "error",
                                          "provider_wait": True, "retry_at": value})
        row = sessions.load(sid)["run_receipts"][-1]
        assert provider_wait_at(row.get("retry_at"), now=NOW) == 0, value


# ------------------------------------------------------- nothing is scheduled
def test_a_wait_never_schedules_the_next_turn():
    """`auto_next` is the only thing the web scheduler reads, and a wait is not a finish."""
    from harness.web_tasks import terminal_outcome
    # `terminal_outcome` reads the wall clock, as the scheduler does; the reset is placed
    # relative to it rather than to this module's fixed NOW.
    soon = int(time.time()) + 3600
    row = terminal_outcome(waited(retry_at=soon))
    assert row["provider_wait"] is True and row["retry_at"] == soon
    assert row["auto_next"] is False and row["completed"] is False
    # A fenced wait is still fenced; a wait is never a reason to lift that.
    assert terminal_outcome(waited(retry_at=soon), recovery_required=True)["auto_next"] is False


def test_recording_a_wait_issues_no_provider_call(tmp_path):
    """The receipt path is bookkeeping. It may not reach for the provider to 'check'.

    A quota reset in the future is precisely when a hidden probe would be most expensive,
    so the whole terminal path runs with the provider factory booby-trapped.
    """
    import harness.providers as providers
    from harness.recorder import Recorder

    calls = []
    original = providers.make_provider
    providers.make_provider = lambda *a, **k: calls.append((a, k))
    try:
        rec = Recorder(str(tmp_path / "runs.db"))
        res = waited(run_id=rec.start_run("t", "collie", "m", "anthropic"))
        rec.finish_run(res)
        out = run_outcome(res, now=NOW)
        rec.close()
    finally:
        providers.make_provider = original

    assert calls == [], "a recorded wait must not construct a provider"
    assert out["provider_wait"] is True


def test_the_telemetry_row_still_records_the_run_as_stopped(tmp_path):
    """`runs.db` keeps its own vocabulary: a wait is an error ending there, not a success."""
    from harness.recorder import Recorder
    rec = Recorder(str(tmp_path / "runs.db"))
    res = waited(run_id=rec.start_run("t", "collie", "m", "anthropic"))
    rec.finish_run(res)
    row = rec.db.execute("SELECT stop_reason, success FROM runs WHERE run_id=?",
                         (res.run_id,)).fetchone()
    rec.close()
    assert row["stop_reason"] == "error" and not row["success"]


def test_provider_wait_state_is_a_plain_typed_block():
    assert provider_wait_state(waited(), now=NOW) == {"retry_at": RESET}
    assert provider_wait_state(waited(canceled=True), now=NOW) is None
    assert provider_wait_state(RunResult(), now=NOW) is None


# ------------------------------------ the terminal surfaces' own durable turn receipts
#
# TUI and REPL turns compose `turn_decision_receipt` BEFORE `sessions.save`, and append it
# durably right after.  The journal they later re-read for the next turn is already settled
# at that earlier moment (the execution loop writes its terminal or `external_action`
# checkpoint before `run()` returns), so the fence is readable where the row is built --
# which is the only place that can keep a quota error over an open effect from reopening in
# the web UI as an ordinary "waiting for quota".  These exercise that real boundary.


def _decision():
    """A real routed decision, from the router the surfaces actually call."""
    from harness.cli import resolve_turn_decision
    return resolve_turn_decision("rotate the deploy keys", "mock")


def _thread(scope=""):
    """A stand-in that resolves its durable thread id with the LOOP's own derivation."""
    from harness.loop import Harness

    class Thread:
        _durable_session_id = Harness._durable_session_id

        def __init__(self, s):
            self.checkpoint_scope = s
            self.provider = None
    return Thread(scope)


def _fence_the_thread(sid):
    """Stop this thread exactly where a run that died with a tool in flight stops."""
    sessions.checkpoint(sid, [{"role": "user", "content": "rotate the deploy keys"}],
                        run_id="r-9", state="external_action",
                        detail={"tool_name": "run_shell", "error": "HTTP 429"})
    assert sessions.recovery_state(sid)["recovery_required"] is True


def _settle_the_thread(sid):
    sessions.checkpoint(sid, [{"role": "user", "content": "rotate the deploy keys"}],
                        run_id="r-9", terminal=True)
    assert sessions.recovery_state(sid) is None


def _turn(sid, res, journal=None):
    """One terminal turn's receipt path: compose, save the transcript, append, reload."""
    from harness.cli import turn_decision_receipt, turn_receipt_fence
    h = _thread("session:" + sid if sid else "")
    receipt = turn_decision_receipt(
        _decision(), res, None,
        recovery_required=turn_receipt_fence(h, journal, sid))
    sessions.save(sid, res.messages or [], answer=res.answer or "")
    sessions.append_run_receipt(sid, receipt)
    return receipt, (sessions.load(sid) or {}).get("run_receipts", [])[-1]


def test_a_terminal_turn_reads_its_fence_from_the_thread_the_loop_journaled_to(store):
    """Real journal states, read through the id derivation the loop itself uses."""
    from harness.cli import turn_receipt_fence

    sid = "repl-thread"
    _fence_the_thread(sid)
    assert turn_receipt_fence(_thread("session:" + sid)) is True
    assert turn_receipt_fence(_thread(""), sessions, sid) is True, "explicit thread, same read"

    # An interrupted host-attested built-in READ is auto-resumable, and demanding inspection
    # for it would strand an ordinary wait. The journal's own predicate decides, not this test.
    sessions.checkpoint(sid, [{"role": "user", "content": "rotate the deploy keys"}],
                        run_id="r-9", state="executing_tool",
                        detail={"tool_name": "read_file", "replay_safe": True})
    assert turn_receipt_fence(_thread("session:" + sid)) is False

    _settle_the_thread(sid)
    assert turn_receipt_fence(_thread("session:" + sid)) is False
    # ACP: no durable thread, so there is no fence to miss and nothing changes for it.
    assert turn_receipt_fence(_thread("")) is False


def test_a_repl_turn_ending_on_quota_over_an_open_effect_does_not_reopen_as_a_wait(store):
    """The reviewer's case, end to end: fenced journal, quota error, durable receipt.

    Before this, the row went to disk saying `provider_wait` with no fence beside it, and a
    web reopen read it as "waiting for quota" while an uninspected effect was still open.
    """
    sid = "fenced-turn"
    _fence_the_thread(sid)
    soon = int(time.time()) + 3600
    receipt, reopened = _turn(sid, waited(retry_at=soon, messages=[
        {"role": "user", "content": "rotate the deploy keys"}]))

    for row in (receipt, reopened):
        assert "provider_wait" not in row and "retry_at" not in row
        assert row["stop_reason"] == "error" and row["completed"] is False
    assert "rate limit" in reopened["error"], "the provider's own words are still there"
    assert "recovery_required" not in reopened, "that key stays the host's to write"
    # Reading a fence may not clear one, and the transcript save must keep it.
    assert sessions.recovery_state(sid)["recovery_required"] is True


def test_a_clean_quota_wait_on_a_settled_thread_still_reopens_as_a_wait(store):
    """The compatibility half: nothing is withheld from a run that really is just early."""
    sid = "clean-turn"
    _settle_the_thread(sid)
    soon = int(time.time()) + 3600
    receipt, reopened = _turn(sid, waited(retry_at=soon))
    for row in (receipt, reopened):
        assert row["provider_wait"] is True and row["retry_at"] == soon
    assert sessions.recovery_state(sid) is None, "no fence was invented by asking"


def test_an_unreadable_journal_withholds_the_wait_without_inventing_an_effect(store):
    """Not knowing is not evidence that the clock is the only problem -- nor that it isn't."""
    sid = "unreadable-turn"
    _settle_the_thread(sid)

    class Refuses:
        asked = 0

        def recovery_state(self, sid):
            Refuses.asked += 1
            raise OSError("journal unreadable")

    soon = int(time.time()) + 3600
    receipt, reopened = _turn(sid, waited(retry_at=soon), journal=Refuses())
    assert Refuses.asked == 1
    for row in (receipt, reopened):
        assert "provider_wait" not in row and "retry_at" not in row
        assert "recovery_required" not in row
    # Nothing was claimed in return: the thread on disk is as unfenced as it was.
    assert sessions.recovery_state(sid) is None


def test_cancellation_and_a_host_failure_keep_their_own_verdict_in_a_turn_receipt(store):
    """Precedence is unchanged by the fence argument, on a thread with no fence at all."""
    sid = "precedence-turn"
    soon = int(time.time()) + 3600

    _settle_the_thread(sid)
    _, canceled = _turn(sid, waited(retry_at=soon, canceled=True))
    assert canceled["canceled"] is True and "provider_wait" not in canceled

    _settle_the_thread(sid)
    res = waited(retry_at=soon)
    note_host_error(res, "session transcript could not be persisted: OSError: disk full")
    _, failed = _turn(sid, res)
    assert "provider_wait" not in failed and "retry_at" not in failed
    assert "disk full" in failed["error"] and "rate limit" in failed["error"]


def test_an_ordinary_completed_turn_receipt_is_what_it_always_was(store):
    """Every turn that is not a wait must look exactly as it did before the fence existed."""
    from harness.cli import turn_decision_receipt

    sid = "ordinary-turn"
    _settle_the_thread(sid)
    res = RunResult(answer="done", stop_reason="completed", success=True, edited=True,
                    model="mock-planner-v1",
                    messages=[{"role": "assistant", "content": "done"}])
    receipt, reopened = _turn(sid, res)
    assert turn_decision_receipt(_decision(), res, None) == receipt, "the fence changed nothing"
    assert reopened["completed"] is True and reopened["stop_reason"] == "completed"
    assert "provider_wait" not in reopened and "recovery_required" not in reopened
    # A FENCED completed turn is still a completed turn: the fence only withdraws a wait.
    _fence_the_thread(sid)
    assert _turn(sid, res)[1]["completed"] is True


def test_every_terminal_receipt_call_site_passes_its_fence():
    """Structural, not textual: the three surfaces' actual call nodes are read as syntax.

    The earlier grep guards match strings; this walks each module's AST, so a rename or a
    reformat cannot make a fence-less call site pass by accident.
    """
    import ast

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    found = {}
    for name in ("tui.py", "cli.py", "acp_agent.py"):
        tree = ast.parse(open(os.path.join(here, "harness", name), encoding="utf-8").read())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "turn_decision_receipt"]
        assert calls, "%s no longer composes a turn receipt -- update this test" % name
        found[name] = len(calls)
        for call in calls:
            fence = [k for k in call.keywords if k.arg == "recovery_required"]
            assert fence, "%s:%d composes a turn receipt without a fence" % (name, call.lineno)
            assert isinstance(fence[0].value, ast.Call) and \
                fence[0].value.func.id == "turn_receipt_fence", \
                "%s:%d must read the fence, not assume one" % (name, call.lineno)
    assert found == {"tui.py": 1, "cli.py": 1, "acp_agent.py": 1}


# ---------------------------------------------- an UNKNOWN fence is not a clear one
# `turn_receipt_fence` already withholds the wait when the journal will not answer. The
# ordinary CLI and Web run paths read the same journal at their own moments and turned
# every failure of that read into "unfenced", which is a different claim: the row then
# went out saying "nothing is wrong except the clock" about a thread whose boundary
# nobody could see. A read can fail on its own -- a lock held, a truncated write, a file
# briefly unreadable -- and the very next save can succeed, so a failed write is no
# rescue. These drive the REAL run paths with reads refusing and every write working,
# and read the verdict where a person meets it: the printed JSON, the durable receipt,
# the streamed `done` frame and the scheduler's row.


class _RefusesToRead:
    """Refuses `recovery_state`; every other journal call is the real one.

    Armed on demand, because a read that answered a moment ago is exactly the read that
    can fail now: the Web surface consults the journal BEFORE it starts a run (to refuse
    continuing a fenced thread), and the failure this guards is the read that composes
    the finished run's own verdict.
    """

    def __init__(self, monkeypatch, module=sessions):
        self.reads = 0
        self._monkeypatch, self._module = monkeypatch, module

    def arm(self):
        self._monkeypatch.setattr(self._module, "recovery_state", self._refuse)
        return self

    def _refuse(self, *a, **kw):
        self.reads += 1
        raise OSError("journal unreadable")


def _passing_check(monkeypatch, arm=None):
    """A required check that passes, optionally arming a journal failure as it returns."""
    from harness import verification

    def check(command, cwd, **kw):
        if arm is not None:
            arm.arm()
        return {"command": command, "exit_code": 0, "passed": True,
                "command_passed": True, "output": "1 passed",
                "executed": True, "process_tree_terminated": True,
                "freshness": "fresh", "source": "user"}
    monkeypatch.setattr(verification, "run_verification_command", check)


def _cli_quota_run(monkeypatch, tmp_path, capsys, soon):
    """One real `collie run` that ends on an attested quota reset. Returns (payload, receipt)."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from harness import cli as cli_mod
    from test_interruption_lifecycle import _cli_run_args, _pin_native_cli

    _passing_check(monkeypatch)

    def run(h, task_id, task, history):
        return waited(retry_at=soon, task_id=task_id, harness="collie",
                      model="mock-coder-v1",
                      messages=[{"role": "user", "content": task}])

    _pin_native_cli(monkeypatch, tmp_path, run)
    assert cli_mod.cmd_run(_cli_run_args(tmp_path)) == 1        # it ended on an error
    payload = json.loads(capsys.readouterr().out.strip())
    saved = sessions.load(payload["session"]) or {}
    return payload, (saved.get("run_receipts") or [])[-1]


def test_the_cli_withholds_the_wait_when_the_journal_will_not_answer(monkeypatch, tmp_path,
                                                                    capsys):
    """Printed verdict and durable receipt, over a journal that refuses every read.

    The saves below all SUCCEED -- that is the point. A host error would withdraw the
    wait by itself; an unreadable fence with a healthy disk is the case where nothing
    else does it.
    """
    refuses = _RefusesToRead(monkeypatch).arm()
    soon = int(time.time()) + 3600
    payload, receipt = _cli_quota_run(monkeypatch, tmp_path, capsys, soon)

    assert refuses.reads >= 2, "both the receipt and the printed verdict asked"
    for row in (payload, receipt):
        assert "provider_wait" not in row and "retry_at" not in row, row
        assert row["stop_reason"] == "error" and row["completed"] is False
    # Nothing was claimed in exchange: no fence is asserted, and none is offered to
    # reconcile, because no evidence of one was ever read.
    assert payload["recovery_required"] is False and payload["recovery"] is None
    assert "rate limit" in payload["error"], "the provider's own words survive"
    assert "recovery_required" not in receipt, "that key stays the host's to write"
    # The transcript really did persist -- a write failure is not what saved this verdict.
    assert (sessions.load(payload["session"]) or {}).get("messages")


def test_a_cli_run_on_a_readable_thread_still_reports_its_wait(monkeypatch, tmp_path,
                                                               capsys):
    """The control: same run, same writes, a journal that answers -- the wait is intact."""
    soon = int(time.time()) + 3600
    payload, receipt = _cli_quota_run(monkeypatch, tmp_path, capsys, soon)

    for row in (payload, receipt):
        assert row["provider_wait"] is True and row["retry_at"] == soon
    assert payload["recovery_required"] is False and payload["recovery"] is None
    assert sessions.recovery_state(payload["session"]) is None, "no fence was invented"


def _web_run(monkeypatch, tmp_path, result, session, refuses=None, after_check=False):
    """One real `_serve_stream` run. Returns (done frame, scheduler row, durable receipt).

    ``refuses`` is armed as the run returns, so the journal answers the pre-run gate and
    then refuses the read the verdict is built from.  ``after_check`` arms it one step
    later instead, as the required check returns: the check boundary reads the journal
    too, and a failure there is a HOST error that withdraws the wait by its own door --
    which is not the case under test here.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_interruption_lifecycle import _WebHarness, _web_isolate
    from harness import cli as _cli, webapp

    _web_isolate(monkeypatch, tmp_path)
    _passing_check(monkeypatch, arm=refuses if after_check else None)
    arm_at_run = refuses if (refuses and not after_check) else None
    monkeypatch.setattr(_cli, "make_harness",
                        lambda *a, **kw: _WebHarness(
                            kw.get("gate"), lambda msg: result,
                            before_return=(lambda h: arm_at_run.arm()) if arm_at_run else None))
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    webapp.Handler._serve_stream(fake, {
        "q": ["rotate the deploy keys"], "session": [session], "intent": ["build"],
        "quality": ["balanced"], "verification": ["required"],
        "verify_command": ["pytest -q"], "verify_source": ["user"],
        "workspace": ["current"], "strategy": ["single"]})
    done = next(data for kind, data in events if kind == "done")
    saved = sessions.load(session) or {}
    return done, fake._stream_outcome, (saved.get("run_receipts") or [])[-1]


def _web_quota_result(soon, **over):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_interruption_lifecycle import _web_result

    values = dict(error="retryable: [provider quota reset] HTTP 429 rate limit",
                  stop_reason="error", success=False, retry_at=soon)
    values.update(over)
    return _web_result("rotate the deploy keys", **values)


def test_the_web_stream_withholds_the_wait_when_the_journal_will_not_answer(monkeypatch,
                                                                           tmp_path):
    """The `done` frame the browser renders, and the receipt a reopen reads back."""
    soon = int(time.time()) + 3600
    result = _web_quota_result(soon)
    refuses = _RefusesToRead(monkeypatch)
    done, scheduled, receipt = _web_run(monkeypatch, tmp_path, result, "web-unknown",
                                        refuses=refuses)

    assert refuses.reads >= 1
    for row in (done, scheduled, receipt):
        assert "provider_wait" not in row and "retry_at" not in row, row
        assert row["stop_reason"] == "error"
    assert done["recovery_required"] is False and done["recovery"] is None
    assert "rate limit" in done["error"]
    assert (sessions.load("web-unknown") or {}).get("messages"), "the save went through"


def test_a_web_run_on_a_readable_thread_still_reports_its_wait(monkeypatch, tmp_path):
    """The control, through the same stream: a settled thread reads as the wait it is."""
    soon = int(time.time()) + 3600
    done, scheduled, receipt = _web_run(
        monkeypatch, tmp_path, _web_quota_result(soon), "web-clean")

    for row in (done, scheduled, receipt):
        assert row["provider_wait"] is True and row["retry_at"] == soon
    assert done["recovery_required"] is False
    assert scheduled["auto_next"] is False, "an error never schedules the next item"


def test_an_unreadable_journal_does_not_start_the_next_queued_item(monkeypatch, tmp_path):
    """Scheduling is a claim too: `auto_next` says this thread ended clean and free.

    A completed run whose fence cannot be read has no error to stop the scheduler, so
    this is the one place where the unknown state would otherwise hand the next request
    to a thread that may have an effect open.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_interruption_lifecycle import _web_result

    done, scheduled, _ = _web_run(
        monkeypatch, tmp_path, _web_result("rotate the deploy keys"), "web-auto-clean")
    assert scheduled["completed"] is True and scheduled["auto_next"] is True

    done, scheduled, _ = _web_run(
        monkeypatch, tmp_path, _web_result("rotate the deploy keys"), "web-auto-unknown",
        refuses=_RefusesToRead(monkeypatch), after_check=True)
    assert scheduled["completed"] is True, "the run itself still finished"
    assert scheduled["auto_next"] is False, "unknown is not a green light"
    assert scheduled["recovery_required"] is False, "and no fence was invented either"
    assert done["recovery_required"] is False


def test_the_cli_fence_reading_separates_the_fact_from_the_reading(monkeypatch, tmp_path,
                                                                  store):
    """The value the two CLI rows are built from, against the real journal states."""
    from harness.cli import _session_fence_reading, _session_fenced

    sid = "reading-thread"
    sessions.checkpoint(sid, [{"role": "user", "content": "rotate the keys"}],
                        run_id="r-1", state="executing_tool",
                        detail={"tool": "run_shell", "args": {"cmd": "./rotate.sh"}})
    assert _session_fence_reading(sessions, sid) == (True, True)
    # An external worker owns its own fence; the journal is not the one being asked, so
    # it neither reports one nor withholds anything on its behalf.
    assert _session_fence_reading(sessions, sid, external=True) == (False, True)

    sessions.checkpoint(sid, [{"role": "user", "content": "rotate the keys"}],
                        run_id="r-1", terminal=True)
    assert _session_fence_reading(sessions, sid) == (False, True)

    class Refuses:
        def recovery_state(self, sid):
            raise OSError("journal unreadable")

    assert _session_fence_reading(Refuses(), sid) == (False, False)
    # The fact alone is unchanged for callers that WRITE it down.
    assert _session_fenced(Refuses(), sid) is False
