"""Which fact wins when one check receipt carries two of them, on the task surface.

A receipt is not a boolean.  ``harness.verification.check_result_state`` reads
one in a fixed order — did not run, stopped, ran out of time, passed, exited 0
without being bindable, failed — because the later words are only meaningful
once the earlier ones are ruled out.  A check killed at its deadline judged
nothing, whatever ``passed`` still says beside it, and a run's ``verified``
boolean is a summary of a receipt, not evidence that can outlive one.

The desktop gate asked those questions in the order the flags happened to
appear: ``passed`` first, then the deadline.  So a receipt that said
``timed_out: true`` AND carried a stale ``passed: true`` from an earlier attempt
came out the other side as "✓ Check passed", two inches from a conversation
sentence saying the required check ran out of time.  The same hole was open one
step further back: the live evidence handler set the window's own
``runVerified`` straight from ``passed`` before it classified the deadline, and
a ``verified`` flag on the terminal frame could re-green a check that had
explicitly never run.

Every run below is a real SSE stream into the real page, or a real reopened
thread, and every assertion is on what a person sees.  Nothing here re-implements
the rule: the expected word comes from the Python classifier the receipt was
written by, so the two cannot drift apart quietly.
"""
import pytest

from harness.verification import check_result_state

from test_web_ui_run_status import (  # noqa: F401  (pytest fixtures)
    TRANSCRIPTS, _Fixture, browser, server, ui,
)
from test_web_verification_timeout_ui import TIMED_OUT_EVIDENCE, _stream

# The gate word the card shows for each state the classifier can return. This is deliberately not
# the whole contract: `not_run` has no card of its own, because "the check never ran" is reported
# through what the run promised — a required check missing, or edits nobody confirmed — so those
# endings are asserted case by case below instead of through this table.
GATE_FOR_STATE = {"cancelled": "stopped", "timed_out": "timeout", "passed": "pass",
                  "inconclusive": "inconclusive", "failed": "fail"}

# The originally contradictory receipt: killed at its deadline, still carrying the pass flag of an
# attempt that finished earlier. Both facts are in one dict, exactly as the parent observed it.
STALE_PASS_TIMEOUT = dict(TIMED_OUT_EVIDENCE, passed=True)
# Stopped by the person, with the same stale flag left behind.
STALE_PASS_CANCELLED = {"command": "python -m unittest", "executed": True, "cancelled": True,
                        "timed_out": False, "exit_code": None, "command_passed": False,
                        "passed": True, "source": "user"}
# Never started at all — and the run still claims it was verified.
NEVER_RAN = {"command": "pytest -q", "executed": False, "cancelled": False, "timed_out": False,
             "passed": True, "freshness": "not_run",
             "skipped_reason": "the run ended before the check started"}
# The positive control: a check that really did run and really did pass.
GREEN = {"command": "pytest -q", "executed": True, "cancelled": False, "timed_out": False,
         "exit_code": 0, "command_passed": True, "passed": True, "ran_after_last_edit": True,
         "freshness": "fresh", "source": "user"}
# Older receipts, from before the surrounding flags existed. They must keep working unchanged.
LEGACY_PASS = {"command": "make test", "passed": True}
LEGACY_FAIL = {"command": "make test", "passed": False, "exit_code": 2}
# Exit 0, unbindable, and its `freshness` word is one nothing here has a sentence for. The reader
# still gets a sentence.
ODD_FRESHNESS = {"command": "npm run build", "executed": True, "cancelled": False,
                 "timed_out": False, "exit_code": 0, "command_passed": True, "passed": False,
                 "freshness": "constructor", "source": "user"}


def _route(page, events):
    """Stage one SSE run the way the real server delivers one: events, then close."""
    page.route("**/api/stream?*", lambda route: route.fulfill(
        content_type="text/event-stream", body=_stream(events)))


ANSWER = "Reworked the parser."


def _await_settled(page):
    """Wait for the staged run to be over, on its own terminal frame.

    A page-routed stream is delivered in one piece, so the "running" pill can come and go
    between two polls; the answer the terminal frame renders is the signal that cannot be
    missed, and the gate is settled in the same handler that writes it.
    """
    page.wait_for_function("(a) => document.getElementById('log').innerText.includes(a)",
                           arg=ANSWER, timeout=8000)
    page.wait_for_function(
        "() => !document.getElementById('statePill').classList.contains('live')", timeout=8000)
    page.wait_for_timeout(150)


def _run(ui, evidence, done_extra=None, live_evidence=True, frame_receipt=True,
         ask="Update the parser"):
    """One real run whose check produced `evidence`, asked through the real composer."""
    done = {"session": "s-prec", "run": "r-prec", "answer": ANSWER,
            "canceled": False, "completed": False, "edited": True, "turns": 2,
            "model_calls": 2, "cost_usd": 0.0, "stop_reason": "error"}
    done.update(done_extra or {})
    if frame_receipt and evidence is not None:
        done["verification_evidence"] = evidence
    events = [("start", {"session": "s-prec", "run": "r-prec", "model": "mock",
                         "prior_turns": 0}),
              ("edit", {"path": "src/parser.rs", "old": "a\n", "new": "b\n"})]
    if evidence is not None and live_evidence:
        events += [("verification_started", {"command": evidence.get("command", ""),
                                             "run": "r-prec"}),
                   ("verification_evidence", {"evidence": evidence})]
    # The run receipt carries the same `verified` boolean the terminal frame does; it arrives
    # first, the way the runner emits it.
    events += [("receipt", {"turns": 2, "tool_calls": 1, "wall_ms": 900, "total_tokens": 700,
                            "prefix_tokens": 300, "cost_usd": 0.0,
                            "verified": bool(done.get("verified"))}),
               ("done", done)]
    _route(ui.page, events)
    ui.page.fill("#input", ask)
    ui.page.press("#input", "Enter")
    _await_settled(ui.page)


def _reopen(ui):
    """Open the saved thread and wait for its transcript, not for the answer we expect."""
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function(
        "(a) => document.getElementById('log').innerText.includes(a)", arg=ANSWER, timeout=8000)
    ui.page.wait_for_timeout(200)


def _timeline(ui):
    if ui.page.query_selector("#tabTimeline"):
        ui.page.click("#tabTimeline")
    return ui.page.inner_text("#timeline")


def _headline(ui):
    return ui.page.inner_text("#gateStatus")


# --------------------------------------------------------------- the contradiction itself

@pytest.mark.parametrize("frame_receipt", [True, False])
def test_a_deadline_outranks_a_stale_pass_flag_on_the_same_receipt(ui, frame_receipt):
    """The reported defect: gate `pass` on a receipt whose check never finished.

    Run twice: once with the receipt repeated on the terminal frame, once with the frame
    carrying only its own `verified: true`. The frame's boolean is younger than the receipt
    but it is not newer evidence, and a check that ran out of time certifies nothing either way.
    """
    assert check_result_state(STALE_PASS_TIMEOUT) == "timed_out", "contract, not this file"
    _run(ui, STALE_PASS_TIMEOUT, done_extra={"verified": True,
                                             "error": "required check ran out of time"},
         frame_receipt=frame_receipt)

    assert ui.gate_state() == GATE_FOR_STATE["timed_out"]
    assert "Check ran out of time" in _headline(ui)
    assert "Check passed" not in _headline(ui), _headline(ui)
    sub = ui.page.inner_text("#gateSub")
    assert "cargo test --all" in sub and "stopped after 900s without finishing" in sub, sub
    assert "proposed check ran out of time" in _timeline(ui)
    assert "proposed check passed" not in _timeline(ui), _timeline(ui)


def test_a_stopped_check_outranks_a_stale_pass_flag(ui):
    """Cancellation keeps its own priority: stopped is not a pass and not a failure."""
    assert check_result_state(STALE_PASS_CANCELLED) == "cancelled"
    _run(ui, STALE_PASS_CANCELLED, done_extra={"verified": True, "canceled": True,
                                               "stop_reason": "canceled"})

    assert ui.gate_state() == GATE_FOR_STATE["cancelled"]
    assert "Check stopped" in _headline(ui)
    assert "Check passed" not in _headline(ui) and "Check failed" not in _headline(ui)
    assert "python -m unittest" in ui.page.inner_text("#gateSub")


def test_a_check_that_never_ran_is_not_certified_by_the_frames_verified_flag(ui):
    """`executed: false` outranks everything, including the run's own summary boolean."""
    assert check_result_state(NEVER_RAN) == "not_run"
    _run(ui, NEVER_RAN, done_extra={"verified": True, "stop_reason": "completed",
                                    "completed": True, "error": ""})

    # No check ran, and files did change: that is "unconfirmed", stated plainly. What it may
    # never be is green.
    assert ui.gate_state() == "unverified", "a check that never ran cannot certify edits"
    assert "Check passed" not in _headline(ui), _headline(ui)
    assert "Changed work, not checked" in _headline(ui)
    assert "Check was not run" in _timeline(ui)


# --------------------------------------------------------------------- the durable path

def test_a_reopened_thread_reads_the_timed_out_receipt_over_its_verified_flag(ui, monkeypatch):
    """Reopening a finished thread re-states the receipt, not the boolean stored beside it."""
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [{"role": "user", "content": "Rework the parser"},
                     {"role": "assistant", "content": "Reworked the parser."}],
        "run_receipts": [{"run": "r-old", "stop_reason": "error", "completed": False,
                          "edited": True, "verified": True, "canceled": False,
                          "error": "required check ran out of time",
                          "verification_evidence": STALE_PASS_TIMEOUT,
                          "decision": {"intent": "build", "verification": "required"}}],
    })
    _reopen(ui)

    assert ui.gate_state() == GATE_FOR_STATE["timed_out"]
    assert "Check ran out of time" in _headline(ui)
    assert "cargo test --all" in ui.page.inner_text("#gateSub")
    assert not _Fixture.stream_requests, "reopening a thread must not execute a turn"


def test_a_reopened_required_check_that_never_ran_says_exactly_that(ui, monkeypatch):
    """The promise the run made is what is missing, so that is what the card reports."""
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [{"role": "user", "content": "Rework the parser"},
                     {"role": "assistant", "content": "Reworked the parser."}],
        "run_receipts": [{"run": "r-skip", "stop_reason": "error", "completed": False,
                          "edited": True, "verified": True, "canceled": False,
                          "error": "required check did not run: pytest -q",
                          "verification_evidence": NEVER_RAN,
                          "decision": {"intent": "build", "verification": "required"}}],
    })
    _reopen(ui)

    assert ui.gate_state() == "missing", "the promise the run made is what is missing"
    assert "Required check did not run" in _headline(ui)
    assert "Check passed" not in _headline(ui), _headline(ui)
    assert not _Fixture.stream_requests


# ------------------------------------------------------------------------ the controls

def test_a_check_that_really_passed_is_still_green(ui):
    """The positive control: none of this may make a real pass harder to report."""
    assert check_result_state(GREEN) == "passed"
    _run(ui, GREEN, done_extra={"verified": True, "stop_reason": "completed",
                                "completed": True, "error": ""})

    assert ui.gate_state() == GATE_FOR_STATE["passed"]
    assert "Check passed" in _headline(ui)
    assert "pytest -q" in ui.page.inner_text("#gateSub")
    assert "proposed check passed" in _timeline(ui)


def test_a_check_that_really_failed_is_still_red(ui):
    """The negative control, driven by the fixture server's own failing run."""
    assert check_result_state({"command": "pytest -q", "executed": True, "exit_code": 1,
                               "command_passed": False, "passed": False}) == "failed"
    ui.ask("Break the release build")

    assert ui.gate_state() == GATE_FOR_STATE["failed"]
    assert "Check failed" in _headline(ui)
    assert "required check failed: pytest -q (exit 1)" in ui.log_text()


def test_an_exit_zero_check_that_covers_nothing_keeps_its_own_answer(ui):
    """The uncertified ending stays its own word — neither collapsed into pass nor into fail."""
    ui.ask("Bundle the release assets")

    assert ui.gate_state() == GATE_FOR_STATE["inconclusive"]
    assert "did not certify this result" in _headline(ui)
    sub = ui.page.inner_text("#gateSub")
    assert "npm run build" in sub and "the workspace changed while it was running" in sub, sub


def test_an_unrecognised_freshness_word_still_produces_a_sentence(ui):
    """A receipt word this page has no phrasing for must not leak the template at the reader.

    `freshness` is data from the run: looking the reason up by it found something for words
    this file never wrote, and the sentence reached the card with its slot unfilled.
    """
    assert check_result_state(ODD_FRESHNESS) == "inconclusive"
    _run(ui, ODD_FRESHNESS, done_extra={"error": "required check did not certify this result"})

    assert ui.gate_state() == "inconclusive"
    sub = ui.page.inner_text("#gateSub")
    assert "{why}" not in sub, sub
    assert "its result could not be bound to the files that exist now" in sub, sub
    assert "npm run build" in sub, sub


# --------------------------------------------------------- what must NOT change underneath

@pytest.mark.parametrize("evidence,state", [(LEGACY_PASS, "passed"), (LEGACY_FAIL, "failed")])
def test_a_legacy_receipt_without_the_newer_flags_reads_as_it_always_did(ui, evidence, state):
    """Receipts written before `executed`/`timed_out`/`cancelled` existed still settle."""
    assert check_result_state(evidence) == state
    _run(ui, evidence, done_extra={"verified": state == "passed"})

    assert ui.gate_state() == GATE_FOR_STATE[state]
    assert "make test" in ui.page.inner_text("#gateSub")


def test_a_run_with_no_check_receipt_still_trusts_its_verified_flag(ui):
    """With nothing contradicting it, the backend's own signal is all there is, and it stands."""
    _run(ui, None, done_extra={"verified": True, "stop_reason": "completed",
                               "completed": True, "error": ""})

    assert ui.gate_state() == "pass"
    assert "Check passed" in _headline(ui)


def test_a_read_only_answer_still_shows_no_card_at_all(ui):
    """Nothing was changed and nothing was checked: the quiet ending stays quiet."""
    ui.ask("Read README.md and tell me what this tool does")

    assert ui.gate_state() == "idle"
    assert not ui.gate_visible(), "an informational answer has nothing to report here"
