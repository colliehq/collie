"""A check killed at its deadline is not a check that failed.

``run_verification_command`` kills the owned process tree when the command
overruns ``timeout``.  What it recorded afterwards, though, was a receipt with
``exit_code: None`` and nothing at all to say that a deadline had been hit — the
only trace was a sentence appended to the captured output.  Every surface that
reads a receipt asks ``passed``, then ``command_passed``, and a timed-out check
answers no to both, so it fell through to the bottom of the ladder::

    required check failed: pytest -q (exit None)

That is the same mistake the inconclusive receipt already fixed, one rung
further down: the command did not fail, it never finished, and the only thing
the surface knows about the code afterwards is nothing.  Worse, the receipt also
claimed ``freshness: "fresh"`` and ``ran_after_last_edit: True`` whenever the
killed check happened to leave the tree untouched — a freshness binding for a
run that was cut off mid-flight.  Cancellation was already excluded from that
claim for exactly this reason; a deadline was not.

Downstream that costs real work.  The in-slice coding agent is handed
"VERIFICATION FAILED: the configured check exited None. Read the output below,
fix the cause, and run this check again", so it spends a repair round hunting a
broken test nobody has evidence of, instead of narrowing the check or saying it
needs longer than the Mission allows.

The check is run for real here — a script that outlives its own deadline — so
the trigger is the product's own timeout path rather than a hand-written
receipt.
"""
import json
import sys
from pathlib import Path

import pytest

from harness import cli, code_check, sessions, verification

from test_verification_cancel import (  # the real CLI/Web endings, already wired
    PYTHON, _WebHarness, _cli_args, _answered, _pin_cli, _spaces, _web_isolate,
)
from test_verification_inconclusive_receipt import _failing_check


CHECK_TIMEOUT_S = 2


def _overrunning_check(workspace):
    """A check that announces itself, then outlives any deadline it is given."""
    (workspace / "src.txt").write_text("source", encoding="utf-8")
    (workspace / "slow_check.py").write_text(
        "import time\n"
        "print('collecting 412 tests', flush=True)\n"
        "time.sleep(600)\n", encoding="utf-8")
    return '"%s" slow_check.py' % PYTHON


def _pin_check_timeout(monkeypatch, seconds=CHECK_TIMEOUT_S):
    """Give the host check a test-sized deadline without faking the check itself.

    The CLI and Web endings do not pass ``timeout`` at all, so the product's own
    300s default would make a real timeout untestable.  Only the deadline is
    replaced; the command, the process tree and the receipt are the real ones.
    """
    real = verification.run_verification_command

    def with_deadline(command, cwd, **kw):
        kw["timeout"] = seconds
        return real(command, cwd, **kw)

    monkeypatch.setattr(verification, "run_verification_command", with_deadline)


# --------------------------------------------------------------------------- #
# the receipt itself
# --------------------------------------------------------------------------- #
def test_a_check_killed_at_its_deadline_is_not_a_failed_check(tmp_path):
    """The real trigger: no exit code, no verdict, and no freshness binding."""
    workspace, _ = _spaces(tmp_path, "slow")
    evidence = verification.run_verification_command(
        _overrunning_check(workspace), str(workspace), timeout=CHECK_TIMEOUT_S)

    assert evidence["executed"] is True and evidence["cancelled"] is False
    assert evidence["timed_out"] is True, "the receipt records the deadline it hit"
    assert evidence["timeout_s"] == CHECK_TIMEOUT_S
    assert evidence["exit_code"] is None, "a killed check reports no exit code"
    assert evidence["command_passed"] is False and evidence["passed"] is False
    assert evidence["process_tree_terminated"] is True
    # The tree is untouched here, and that is exactly the trap: an unfinished run
    # may not borrow a clean workspace as proof that it covered one.
    assert evidence["freshness"] == "timed_out"
    assert evidence["ran_after_last_edit"] is False
    assert "check timed out after %ds" % CHECK_TIMEOUT_S in evidence["output"]

    assert verification.check_result_state(evidence) == "timed_out"
    reason = verification.check_result_reason(evidence, "pytest -q")
    assert "ran out of time" in reason
    assert "stopped after %ss without finishing" % CHECK_TIMEOUT_S in reason
    assert "failed" not in reason, reason
    assert "None" not in reason, reason


def test_a_real_failure_is_still_reported_as_a_failure(tmp_path):
    """The repair must not soften a red check into an excuse about time."""
    workspace, _ = _spaces(tmp_path, "red")
    evidence = verification.run_verification_command(
        _failing_check(workspace), str(workspace), timeout=120)

    assert evidence["timed_out"] is False
    assert verification.check_result_state(evidence) == "failed"
    assert verification.check_result_reason(evidence, "pytest -q") == (
        "required check failed: pytest -q (exit 1)")


def test_a_green_check_keeps_its_pass_and_its_freshness(tmp_path):
    """The control: an ordinary check that finishes is untouched by any of this."""
    workspace, _ = _spaces(tmp_path, "green")
    (workspace / "ok_check.py").write_text("print('2 passed')\n", encoding="utf-8")
    evidence = verification.run_verification_command(
        '"%s" ok_check.py' % PYTHON, str(workspace), timeout=120)

    assert evidence["timed_out"] is False and evidence["exit_code"] == 0
    assert evidence["passed"] is True and evidence["freshness"] == "fresh"
    assert evidence["ran_after_last_edit"] is True
    assert verification.check_result_state(evidence) == "passed"
    assert verification.check_result_reason(evidence, "pytest -q") == ""


@pytest.mark.parametrize("evidence,state", [
    # A deadline reached on a check that never started is still "did not run".
    ({"executed": False, "timed_out": True, "passed": False}, "not_run"),
    # A stop the user asked for stays the user's answer, not the clock's.
    ({"executed": True, "cancelled": True, "timed_out": True}, "cancelled"),
    ({"executed": True, "passed": True, "command_passed": True}, "passed"),
    ({"executed": True, "passed": False, "command_passed": True}, "inconclusive"),
])
def test_the_other_receipt_states_keep_their_own_names(evidence, state):
    assert verification.check_result_state(evidence) == state


def test_a_receipt_with_no_recorded_deadline_still_reads_honestly():
    """Older receipts carry no ``timeout_s``; the sentence must not print one."""
    reason = verification.check_result_reason(
        {"executed": True, "timed_out": True, "passed": False}, "make test")
    assert reason == ("required check ran out of time: make test was stopped "
                      "before it finished, so it judged nothing")


# --------------------------------------------------------------------------- #
# what the in-slice coding agent is told
# --------------------------------------------------------------------------- #
def test_the_coding_agent_is_told_the_check_never_finished(tmp_path):
    """The agent must not spend a repair round on a failure nobody observed."""
    workspace, _ = _spaces(tmp_path, "slice")
    evidence = verification.run_verification_command(
        _overrunning_check(workspace), str(workspace), timeout=CHECK_TIMEOUT_S)

    rendered = code_check.render_check_result(evidence)

    assert rendered.startswith("VERIFICATION TIMED OUT:")
    assert "stopped after %ss" % CHECK_TIMEOUT_S in rendered
    assert "VERIFICATION FAILED" not in rendered, rendered
    assert "exited None" not in rendered, rendered
    # It still sees the real partial output, and is pointed at the real choice.
    assert "collecting 412 tests" in rendered
    assert "needs longer" in rendered


def test_a_timed_out_receipt_is_never_reused_in_place_of_the_host_check(tmp_path):
    """Reuse is for answers the host already holds; this receipt holds none."""
    workspace, _ = _spaces(tmp_path, "reuse")
    command = _overrunning_check(workspace)
    evidence = verification.run_verification_command(
        command, str(workspace), timeout=CHECK_TIMEOUT_S, source=code_check.CHECK_SOURCE)
    evidence["invoked_by"] = code_check.IN_SLICE_INVOKER

    assert code_check.reusable_receipt(
        [evidence], command, evidence["post_tree_digest"]) is None


# --------------------------------------------------------------------------- #
# the CLI ending
# --------------------------------------------------------------------------- #
def test_cli_run_reports_a_timed_out_check_in_its_own_words(monkeypatch, tmp_path,
                                                            capsys):
    workspace, _ = _spaces(tmp_path, "cli-slow")
    command = _overrunning_check(workspace)
    _pin_cli(monkeypatch, tmp_path, _answered)
    _pin_check_timeout(monkeypatch)

    assert cli.cmd_run(_cli_args(tmp_path, cwd=str(workspace),
                                 verify_command=command)) == 1
    payload = json.loads(capsys.readouterr().out.strip())

    evidence = payload["verification_evidence"]
    assert evidence["timed_out"] is True and evidence["passed"] is False
    assert evidence["ran_after_last_edit"] is False
    assert "required check failed" not in payload["error"]
    assert "(exit None)" not in payload["error"]
    assert "ran out of time" in payload["error"]
    # The work the run did produce is still the answer, and nothing is verified.
    assert payload["answer"].startswith("rewrote the tokenizer")
    saved = sessions.load(payload["session"])
    assert saved["run_receipts"][-1]["verified"] is False
    assert saved["run_receipts"][-1]["verification_evidence"]["timed_out"] is True
    # The check's own fence retires: its tree was proved extinct by the kill.
    assert sessions.recovery_state(payload["session"]) is None


# --------------------------------------------------------------------------- #
# the Web ending
# --------------------------------------------------------------------------- #
def test_web_run_says_the_check_ran_out_of_time_rather_than_failed(monkeypatch,
                                                                   tmp_path):
    from harness import webapp

    _web_isolate(monkeypatch, tmp_path)
    workspace, _ = _spaces(tmp_path, "web-slow")
    command = _overrunning_check(workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(cli, "make_harness",
                        lambda *a, **kw: _WebHarness(kw.get("gate")))
    _pin_check_timeout(monkeypatch)

    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    webapp.Handler._serve_stream(fake, {
        "q": ["fix the parser"], "session": ["web-timeout"], "intent": ["build"],
        "quality": ["balanced"], "verification": ["required"],
        "verify_command": [command], "verify_source": ["user"],
        "workspace": ["current"], "strategy": ["single"]})

    done = next(data for kind, data in events if kind == "done")
    evidence = done["verification_evidence"]
    assert evidence["timed_out"] is True and evidence["passed"] is False
    assert evidence["freshness"] == "timed_out"
    assert "required check failed" not in (done["error"] or "")
    assert "(exit None)" not in (done["error"] or "")
    assert "ran out of time" in done["error"]
    # A deadline is not a Stop: the run itself was never cancelled.
    assert done["canceled"] is False
    saved = sessions.load("web-timeout")
    assert saved["run_receipts"][-1]["verified"] is False
    # ...and the fact travels with the receipt, so a reopened thread can say it too.
    assert saved["run_receipts"][-1]["verification_evidence"]["timed_out"] is True
