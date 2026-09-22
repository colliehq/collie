"""The live columns depend on each other, and a dependency must not be a rumour.

`one_turn`, `resume` and `usage` share one billable thread, so `resume` and
`usage` read what `one_turn` left in `CheckContext.state`.  The failure this file
pins down was observed against a real `claude` 2.1.228, whose default model the
API rejected with a 400: `one_turn` failed, `resume` spent a *second* model call
to rediscover the same 400, and `usage` then read the snapshot of a turn that
never completed and reported "a completed turn reported 0 output tokens" — a
claim about the runner's usage reporting that nothing in the run supported.

Every case here drives `run_matrix` with a recording stand-in runner, so the
assertions are about what was actually invoked and what the matrix said, not
about the internals of a check:

* an unsettled first turn buys no second call, and manufactures no usage failure;
* a *settled* first turn whose edit never landed is also not resumable — the
  prerequisite is the whole column, not `snapshot.settled`;
* a good first turn followed by a failed resume keeps the first turn's usage
  evidence instead of reading the counters of a turn that did not finish;
* the ordinary two-turn path still runs both turns and still checks the locator,
  the cursor and the file on disk;
* and a blocked column ends UNVERIFIED, not SKIP, so `apply_compat_report`
  withdraws the capability instead of leaving it declared.

No credential, no CLI and no model: `runner_registry.probe` and
`runner_registry.make_runner` are both injected.
"""
import os

import pytest

from harness import runner_compat, runner_registry
from harness.agent_runners import RunnerSnapshot
from harness.runner_compat import FAIL, LIVE_CHECK_NAMES, PASS, SKIP, UNVERIFIED
from harness.runner_specs import RunnerProbe

KEY = "claude-code"
API_400 = ("API Error: 400 Claude Code 2.1.228 does not support this model; "
           "version 2.1.251 or newer is required.")

# What `claude -p --output-format stream-json` reports for a turn that finished.
FIRST_USAGE = {"input_tokens": 120, "output_tokens": 45,
               "cache_read_input_tokens": 8, "total_cost_usd": 0.0031}
SECOND_USAGE = {"input_tokens": 140, "output_tokens": 90,
                "cache_read_input_tokens": 120, "total_cost_usd": 0.0074}


class Turn:
    """One scripted turn: did it settle, did it edit the fixture, what did it cost."""

    def __init__(self, *, settled=True, marker="", usage=None, error=""):
        self.settled = settled
        self.marker = marker
        self.usage = dict(usage or {})
        self.error = error


class RecordingRunner:
    """A `claude-code` stand-in that records its calls and edits the fixture itself.

    The edit is real (the check reads the file back off disk, which is the point
    of the column), the thread locator and cursor advance the way the adapter's
    do, and nothing else about the runner is simulated: every other collaborator
    in the path is the shipped one.
    """

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []             # ("start"|"resume", workspace)

    def _apply(self, turn, workspace, *, cursor, invocation):
        if turn.marker:
            path = os.path.join(workspace, runner_compat._FIXTURE_FILE)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n%s\n" % turn.marker)
        return RunnerSnapshot(
            runner=KEY, workspace=workspace, thread_id="thr-compat-1",
            cursor=cursor, usage=turn.usage, settled=turn.settled,
            mutated=bool(turn.marker), mutation_check_complete=True,
            error=turn.error, exit_code=0 if turn.settled else 1,
            invocation=invocation, started_at=1.0, finished_at=2.0,
        )

    def _next(self):
        if not self.turns:
            raise AssertionError("the matrix asked for a turn the script does not have")
        return self.turns.pop(0)

    def start(self, _prompt, workspace):
        self.calls.append(("start", workspace))
        return self._apply(self._next(), workspace, cursor=4, invocation=1)

    def resume(self, snapshot, _prompt):
        self.calls.append(("resume", snapshot.workspace))
        return self._apply(self._next(), snapshot.workspace, cursor=9, invocation=2)


def _resume_marker_turn(**kwargs):
    """A resumed turn that rewrites the last line the way `_RESUME_PROMPT` asks."""
    return Turn(marker=runner_compat._RESUME_MARKER, **kwargs)


INSTALLED = RunnerProbe(key=KEY, installed=True, version="2.1.228 (Claude Code)",
                        login="ok", billing_class="max", billing_mode="subscription")
ABSENT = RunnerProbe(key=KEY, installed=False, detail="claude is not on PATH")


@pytest.fixture
def drive(monkeypatch, tmp_path):
    """Run the live matrix for `claude-code` against an injected runner and probe."""
    def run(runner, checks=LIVE_CHECK_NAMES, probe=INSTALLED):
        monkeypatch.setattr(runner_registry, "probe", lambda key, **kw: probe)
        monkeypatch.setattr(runner_registry, "make_runner", lambda key, **kw: runner)
        return runner_compat.run_matrix([KEY], live=True, checks=list(checks),
                                        workspace=str(tmp_path))

    return run


def _cells(report):
    return report["runners"][KEY]["checks"]


# --- the observed failure ---------------------------------------------------
def test_unsettled_first_turn_buys_no_second_model_call(drive):
    """The 400 that killed `one_turn` must not be paid for twice."""
    runner = RecordingRunner(Turn(settled=False, error=API_400),
                             _resume_marker_turn(usage=SECOND_USAGE))
    cells = _cells(drive(runner))

    assert [call[0] for call in runner.calls] == ["start"]
    assert cells["one_turn"]["status"] == FAIL
    assert "did not settle" in cells["one_turn"]["detail"]
    assert "400" in cells["one_turn"]["detail"]

    assert cells["resume"]["status"] == UNVERIFIED, cells["resume"]
    assert "one_turn FAIL" in cells["resume"]["detail"]
    assert "400" in cells["resume"]["detail"]


def test_an_uncompleted_turn_is_not_reported_as_usage_of_zero(drive):
    """`usage` speaks for the runner's counters, not for somebody else's failure."""
    runner = RecordingRunner(Turn(settled=False, error=API_400))
    cells = _cells(drive(runner))

    assert cells["usage"]["status"] == UNVERIFIED, cells["usage"]
    assert "no completed turn" in cells["usage"]["detail"]
    assert "0 output tokens" not in cells["usage"]["detail"]
    # The failure an operator has to act on is still exactly one row.
    assert cells["one_turn"]["status"] == FAIL


def test_the_primary_failure_is_the_only_failure_in_the_report(drive):
    report = drive(RecordingRunner(Turn(settled=False, error=API_400)))

    assert list(report["failures"]) == ["%s.one_turn" % KEY]
    assert "400" in report["failures"]["%s.one_turn" % KEY]
    assert report["totals"][FAIL] == 1
    assert report["totals"][UNVERIFIED] == 2
    for name in ("resume", "usage"):
        where = "%s.%s" % (KEY, name)
        assert report["unverified_reasons"][where].startswith(UNVERIFIED)
        assert where in runner_compat.render_markdown(report)


# --- the prerequisite is the column, not one of its assertions ---------------
def test_a_settled_turn_whose_edit_never_landed_is_not_resumed(drive):
    """`snapshot.settled` is not the bar: resume asks it to change a line that is not there."""
    runner = RecordingRunner(Turn(marker="# not-the-marker", usage=FIRST_USAGE),
                             _resume_marker_turn(usage=SECOND_USAGE))
    cells = _cells(drive(runner))

    assert [call[0] for call in runner.calls] == ["start"]
    assert cells["one_turn"]["status"] == FAIL
    assert "marker" in cells["one_turn"]["detail"]
    assert cells["resume"]["status"] == UNVERIFIED
    assert "no verified first turn" in cells["resume"]["detail"]
    # The turn itself completed, so its counters are evidence and are still read.
    assert cells["usage"]["status"] == PASS, cells["usage"]["detail"]
    assert "output=45" in cells["usage"]["detail"]


def test_a_failed_resume_keeps_the_first_turns_usage_evidence(drive):
    """A resume that never finished must neither be believed nor erase what was."""
    runner = RecordingRunner(Turn(marker=runner_compat._ONE_TURN_MARKER,
                                  usage=FIRST_USAGE),
                             Turn(settled=False, error=API_400, usage={}))
    cells = _cells(drive(runner))

    assert [call[0] for call in runner.calls] == ["start", "resume"]
    assert cells["one_turn"]["status"] == PASS, cells["one_turn"]["detail"]
    # The resume really was attempted and really did fail: that stays a FAIL.
    assert cells["resume"]["status"] == FAIL
    assert "did not settle" in cells["resume"]["detail"]
    assert cells["usage"]["status"] == PASS, cells["usage"]["detail"]
    assert "output=45" in cells["usage"]["detail"]
    assert "cost_reported=0.0031" in cells["usage"]["detail"]


# --- the path that must not change ------------------------------------------
def test_the_ordinary_two_turn_path_still_runs_both_turns(drive):
    runner = RecordingRunner(Turn(marker=runner_compat._ONE_TURN_MARKER,
                                  usage=FIRST_USAGE),
                             _resume_marker_turn(usage=SECOND_USAGE))
    cells = _cells(drive(runner))

    assert [call[0] for call in runner.calls] == ["start", "resume"]
    for name in LIVE_CHECK_NAMES:
        assert cells[name]["status"] == PASS, (name, cells[name]["detail"])
    assert "locator=yes" in cells["one_turn"]["detail"]
    assert "cursor 4 -> 9" in cells["resume"]["detail"]
    assert "invocation=2" in cells["resume"]["detail"]
    # `usage` reads the resumed turn's cumulative counters, not the first turn's.
    assert "output=90" in cells["usage"]["detail"]
    assert "cache_read=120" in cells["usage"]["detail"]


def test_a_skipped_prerequisite_skips_rather_than_unverifies(drive):
    """"No CLI on this host" is not evidence about resuming, and must not downgrade."""
    runner = RecordingRunner()
    cells = _cells(drive(runner, probe=ABSENT))

    assert runner.calls == []
    for name in LIVE_CHECK_NAMES:
        assert cells[name]["status"] == SKIP, cells[name]
        assert "not installed" in cells[name]["detail"]


def test_a_prerequisite_outside_this_run_leaves_the_column_to_its_own_guards(drive):
    """`--checks resume` says nothing about one_turn, so it is not treated as failed."""
    runner = RecordingRunner()
    cells = _cells(drive(runner, checks=["resume", "usage"]))

    assert runner.calls == []
    for name in ("resume", "usage"):
        assert cells[name]["status"] == SKIP, cells[name]
        assert "one_turn produced no" in cells[name]["detail"]


# --- the closing half of the loop -------------------------------------------
def test_a_blocked_column_withdraws_the_capability_it_speaks_for(drive, tmp_path,
                                                                 monkeypatch):
    """UNVERIFIED, not SKIP: the registry must not read a blocked column as verified."""
    saved = dict(runner_registry._COMPAT)
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    report = drive(RecordingRunner(Turn(settled=False, error=API_400)))
    json_path, _md = runner_compat.write_report(report, str(tmp_path / "compat.json"))
    try:
        applied = runner_registry.apply_compat_report(json_path)
        downgraded = set(applied[KEY])
        # one_turn failed; resume and usage were blocked by it.  All three
        # capabilities go, and none of them may survive as a declaration.
        assert {"session_create", "session_resume",
                "usage_tokens", "usage_cost"} <= downgraded
        caps = runner_registry._capabilities_for(runner_registry.SPECS[KEY])
        assert caps["session_resume"] is False and caps["usage_tokens"] is False
        assert runner_registry.compat_status(KEY) == "unverified"
    finally:
        runner_registry._COMPAT.clear()
        runner_registry._COMPAT.update(saved)
        runner_registry.reset_cache()
