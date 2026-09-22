"""The evidence card for a check that ran out of time.

``test_verification_timeout_receipt`` pins the receipt: a check killed at its
deadline reports ``timed_out`` and no exit code, and the conversation's own
sentence says it ran out of time.  This file pins the other half of the same
ending — the gate card and the timeline row a person actually looks at.

Both read the receipt through ``settleGate``, which asked ``passed``, then the
exit-zero "inconclusive" case, and then gave up and painted red.  So the surface
said "✗ Check failed" with the command beside it, while the sentence two inches
away in the conversation said the check never finished.  The live row was worse:
it printed the missing exit code verbatim, as "· exit null".

Nothing about the verdict is softened here — a timed-out check is still not a
pass, and it is still not verified.  It simply gets the caution styling the
other uncertified endings already have, and says which deadline it hit.
"""
import json

import pytest

from test_web_ui_run_status import (  # noqa: F401  (pytest fixtures)
    TRANSCRIPTS, _Fixture, browser, server, ui,
)

TIMED_OUT_EVIDENCE = {
    "command": "cargo test --all", "executed": True, "cancelled": False,
    "timed_out": True, "timeout_s": 900, "exit_code": None,
    "command_passed": False, "passed": False, "ran_after_last_edit": False,
    "freshness": "timed_out", "process_tree_terminated": True, "source": "user",
    "output": "running 412 tests\n\n(check timed out after 900s)",
}
TIMED_OUT_ERROR = ("required check ran out of time: cargo test --all was stopped "
                   "after 900s without finishing, so it judged nothing")


def _stream(events):
    """One staged SSE run, delivered the way the fixture server delivers one."""
    return "".join("event: %s\ndata: %s\n\n" % (kind, json.dumps(data))
                   for kind, data in events)


def _route_timed_out_run(page):
    """A real run whose required check is killed at its deadline."""
    done = {"session": "s-slow", "run": "r-slow", "answer": "Reworked the parser.",
            "error": TIMED_OUT_ERROR, "canceled": False, "stop_reason": "error",
            "completed": False, "edited": True, "verified": False,
            "turns": 3, "model_calls": 4, "cost_usd": 0.01,
            "verification_evidence": TIMED_OUT_EVIDENCE}
    page.route("**/api/stream?*", lambda route: route.fulfill(
        content_type="text/event-stream", body=_stream([
            ("start", {"session": "s-slow", "run": "r-slow", "model": "mock",
                       "prior_turns": 0}),
            ("edit", {"path": "src/parser.rs", "old": "a\n", "new": "b\n"}),
            ("verification_started", {"command": "cargo test --all", "run": "r-slow"}),
            ("verification_evidence", {"evidence": TIMED_OUT_EVIDENCE}),
            ("done", done),
        ])))


def test_a_live_check_that_ran_out_of_time_is_not_painted_as_a_failure(ui):
    _route_timed_out_run(ui.page)
    ui.ask("Rework the parser")

    assert ui.gate_state() == "timeout", "a deadline is its own answer, not a red check"
    status = ui.page.inner_text("#gateStatus")
    assert "Check ran out of time" in status
    assert "Check failed" not in status, status
    sub = ui.page.inner_text("#gateSub")
    assert "cargo test --all" in sub
    assert "stopped after 900s without finishing" in sub
    # The conversation still says plainly that nothing was certified.
    assert "ran out of time" in ui.log_text()
    assert not ui.errors, ui.errors


def test_the_timeline_row_says_it_never_finished_instead_of_exit_null(ui):
    _route_timed_out_run(ui.page)
    ui.ask("Rework the parser")
    if ui.page.query_selector("#tabTimeline"):
        ui.page.click("#tabTimeline")
    timeline = ui.page.inner_text("#timeline")

    assert "proposed check ran out of time" in timeline
    assert "proposed check failed" not in timeline, timeline
    assert "exit null" not in timeline, timeline
    assert "stopped after 900s without finishing" in timeline


def test_the_timed_out_headline_is_amber_caution_not_failure_red(ui):
    """It shares the styling of the other uncertified endings, not of a red check."""
    _route_timed_out_run(ui.page)
    ui.ask("Rework the parser")

    colors = ui.page.evaluate("""() => {
        const probe = (v) => {
          const el = document.createElement('span');
          el.style.color = 'var(--' + v + ')';
          document.body.appendChild(el);
          const c = getComputedStyle(el).color; el.remove(); return c;
        };
        return {status: getComputedStyle(document.getElementById('gateStatus')).color,
                amber: probe('amber'), coral: probe('coral')};
    }""")

    assert colors["status"] == colors["amber"], colors
    assert colors["status"] != colors["coral"], "a deadline is not a failed-check verdict"


def test_a_reopened_thread_restates_the_timed_out_check_from_its_receipt(ui, monkeypatch):
    """The durable receipt carries the fact, so reopening must not re-flatten it."""
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [
            {"role": "user", "content": "Rework the parser"},
            {"role": "assistant", "content": "Reworked the parser."},
        ],
        "run_receipts": [{"run": "r-slow", "stop_reason": "error", "completed": False,
                          "edited": True, "verified": False, "canceled": False,
                          "error": TIMED_OUT_ERROR,
                          "verification_evidence": TIMED_OUT_EVIDENCE,
                          "decision": {"intent": "build", "verification": "required"}}],
    })
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function(
        "() => document.getElementById('gate').getAttribute('data-state') === 'timeout'",
        timeout=8000)

    assert "Check ran out of time" in ui.page.inner_text("#gateStatus")
    assert "cargo test --all" in ui.page.inner_text("#gateSub")
    assert not _Fixture.stream_requests, "reopening a thread must not execute a turn"


def test_a_check_that_really_failed_is_still_reported_as_a_failure(ui):
    """The control: the repair must not turn every unmet check into a softer word."""
    ui.ask("Break the release build")

    assert ui.gate_state() == "fail"
    assert "Check failed" in ui.page.inner_text("#gateStatus")
    assert "required check failed: pytest -q (exit 1)" in ui.log_text()


@pytest.mark.parametrize("lang,headline,row", [
    ("zh", "检查超时中止", "建议的检查超时中止"),
    ("zh-tw", "檢查逾時中止", "建議的檢查逾時中止"),
])
def test_the_timed_out_check_speaks_the_reader_s_language(server, browser, lang,
                                                          headline, row):
    """A Chinese reader got the English sentence beside translated neighbours."""
    from test_web_ui_run_status import TOKEN, _reset_fixture_state, await_run

    _reset_fixture_state()
    _Fixture.lang = lang
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        # The language arrives with the settings fetch and is applied asynchronously.
        # Waiting for the applied language, rather than for a fixed delay, is what
        # keeps this about the wording instead of about the network.
        page.wait_for_function(
            "() => document.documentElement.lang === %r" % lang, timeout=8000)
        _route_timed_out_run(page)
        page.fill("#input", "Rework the parser")
        page.press("#input", "Enter")
        await_run(page)

        assert page.get_attribute("#gate", "data-state") == "timeout"
        assert page.inner_text("#gateStatus") == headline
        sub = page.inner_text("#gateSub")
        assert "cargo test --all" in sub, "the command itself is not translated away"
        assert "ran out of time" not in sub and "stopped after" not in sub, sub
        assert row in page.inner_text("#timeline")
        assert errors == [], "JS errors: %r" % errors
    finally:
        _Fixture.lang = "en"
        context.close()
