"""The driver's own wait, checked against runs that finish at awkward moments.

`await_run` in tests/test_web_ui_run_status.py is what every web-UI suite uses to hand control
back to Python once a staged run is over. It used to wait for the state pill to *become* live and
then to stop being live. Both halves are real, and both are observations made after the send has
already happened: Playwright's first poll can land after a fast run has already started *and*
finished, and then the wait for "live" describes a state that no longer exists. The run that
completed perfectly on screen is reported as a timeout.

That is not a hypothesis about scheduling here. Every run in this file is staged so that the
awkward moment is the only moment there is:

  * the finished-first runs hand control back to Python only after the answer is on screen and the
    pill has gone quiet, so the old wait's first half is unsatisfiable by construction;
  * the still-going run is held open by the fixture server's own event, so a driver that reports
    it finished is wrong about a run that is demonstrably not finished;
  * the stale runs have really completed, once, and must not be spent twice.

No sleeps and no repeated attempts: each case is decided by what the browser did, not by how the
host was feeling. Against the old driver the first case times out while the page shows a finished
run; the rest hold for both drivers and are here so that "wait for this send's own run" cannot be
mistaken for "return as soon as the page looks idle".
"""
import json

import pytest

from test_web_ui_run_status import (  # noqa: F401  (pytest fixtures)
    TOKEN, _Fixture, _reset_fixture_state, await_run, browser, server, ui,
)

playwright_api = pytest.importorskip("playwright.sync_api")
PWTimeout = playwright_api.TimeoutError

# A check that really ran and really passed, so the finished-first runs can be asked the same
# questions (gate state, the command beside it) that a timed-out suite never got to ask.
PASSED_EVIDENCE = {"command": "pytest -q tests/test_index.py", "executed": True,
                   "cancelled": False, "timed_out": False, "exit_code": 0,
                   "command_passed": True, "passed": True, "ran_after_last_edit": True,
                   "freshness": "fresh", "source": "user", "output": "1 passed"}
ANSWER = "Rebuilt the index in one breath."


def _stream(events):
    """One staged SSE run, delivered the way the fixture server delivers one."""
    return "".join("event: %s\ndata: %s\n\n" % (kind, json.dumps(data))
                   for kind, data in events)


def _route_complete_run(page):
    """A whole run — `start`, an edit, its check and `done` — already in the response body.

    This is how tests/test_web_verification_timeout_ui.py stages a run, and it is the shape that
    exposed the race: there is no beat of "running" to wait for, because nothing about the run is
    still outstanding by the time the browser has read the response.
    """
    page.route("**/api/stream?*", lambda route: route.fulfill(
        content_type="text/event-stream", body=_stream([
            ("start", {"session": "s-fast", "run": "r-fast", "model": "mock", "prior_turns": 0}),
            ("edit", {"path": "src/index.rs", "old": "a\n", "new": "b\n"}),
            ("verification_started", {"command": PASSED_EVIDENCE["command"], "run": "r-fast"}),
            ("verification_evidence", {"evidence": PASSED_EVIDENCE}),
            ("done", {"session": "s-fast", "run": "r-fast", "answer": ANSWER, "error": "",
                      "canceled": False, "stop_reason": "completed", "completed": True,
                      "edited": True, "verified": True, "model": "mock", "turns": 1,
                      "model_calls": 1, "cost_usd": 0.01,
                      "verification_evidence": PASSED_EVIDENCE}),
        ])))


# The page itself decides when the send is over: the unique answer is on screen and the pill has
# stopped saying "live". Returning only then is what makes "the run finished before Playwright
# looked again" a fact of this test rather than a matter of luck.
_SEND_AND_LET_IT_FINISH = """
async (answer) => {
  const log = document.getElementById('log');
  const pill = document.getElementById('statePill');
  document.getElementById('send').click();          // the real send, on the real button
  const over = () => log.innerText.includes(answer) && !pill.classList.contains('live');
  await new Promise((resolve, reject) => {
    const deadline = Date.now() + 15000;
    const look = () => {
      if (over()) return resolve();
      if (Date.now() > deadline) return reject(new Error('the staged run never finished'));
      setTimeout(look, 5);
    };
    look();
  });
}
"""


def _send_and_let_it_finish(page, text=ANSWER):
    """Send for real, and come back only once that run is over on screen."""
    page.fill("#input", "Rebuild the index")
    page.evaluate(_SEND_AND_LET_IT_FINISH, text)
    assert not page.evaluate(
        "() => document.getElementById('statePill').classList.contains('live')"), \
        "the premise of this file: the run is over before the driver looks"


def _the_run_really_finished(page):
    """What the page shows about the run the driver is being asked to notice."""
    assert ANSWER in page.inner_text("#log"), "the answer is on screen"
    assert page.get_attribute("#gate", "data-state") == "pass", "the check settled"


# ------------------------------------------------- the run that finished too quickly
def test_a_run_that_finished_before_the_first_look_is_still_observed(ui):
    """The failure the driver actually produced: a timeout reported over a completed run."""
    _route_complete_run(ui.page)
    _send_and_let_it_finish(ui.page)

    await_run(ui.page)          # the unchanged wait path, on a run that is already over

    # The assertions a timed-out driver never reached.
    _the_run_really_finished(ui.page)
    assert ui.page.inner_text("#gateSub").startswith(PASSED_EVIDENCE["command"])
    assert not ui.errors, ui.errors


def test_the_live_then_idle_wait_the_driver_used_misses_that_same_run(ui):
    """Why it failed, stated as a test: the state it waited for had already been and gone.

    This is the old driver's wait, spelled out here rather than imported, so the diagnosis keeps
    holding after the driver is repaired — and so nobody has to rerun anything to see it.
    """
    _route_complete_run(ui.page)
    _send_and_let_it_finish(ui.page)
    _the_run_really_finished(ui.page)

    live = "() => document.getElementById('statePill').classList.contains('live')"
    with pytest.raises(PWTimeout):
        ui.page.wait_for_function(live, timeout=8000)

    # Still true afterwards: the run this wait gave up on is the run that completed.
    _the_run_really_finished(ui.page)
    assert not ui.errors, ui.errors


# ------------------------------------------------- runs that must NOT satisfy the wait
def test_an_idle_page_that_never_ran_anything_is_not_accepted(ui):
    """A page that starts idle is not a finished run, however idle it looks."""
    assert not ui.page.evaluate(
        "() => document.getElementById('statePill').classList.contains('live')")
    with pytest.raises(PWTimeout):
        await_run(ui.page)
    assert _Fixture.stream_requests == [], "nothing was ever sent, so nothing can have finished"


def test_the_previous_run_s_completion_does_not_answer_for_the_next_send(ui):
    """One completion, one wait: an earlier receipt is spent, not reusable."""
    ui.ask("Read README.md and tell me what this tool does")
    assert "It reads a CSV" in ui.log_text(), "the first run really did finish"

    with pytest.raises(PWTimeout):
        await_run(ui.page)          # no second send happened; the first one's ending is spent

    # ...and the driver is not left broken by having been told to wait for nothing.
    ui.ask("Fix the parser off-by-one")
    assert "Fixed the off-by-one." in ui.log_text()
    assert len(_Fixture.stream_requests) == 2


def test_a_run_that_is_still_going_is_waited_for_not_reported_finished(ui):
    """Held open by the fixture's own event: reporting this one finished would be a lie."""
    page = ui.page
    page.fill("#input", "Hold queue fixture")
    page.press("#input", "Enter")
    page.wait_for_function(
        "() => document.getElementById('statePill').classList.contains('live')", timeout=8000)

    with pytest.raises(PWTimeout):
        await_run(page)             # the run is still open on the server; there is no ending yet

    assert not _Fixture.queue_release.is_set(), "the run was held for the whole wait"
    _Fixture.queue_release.set()    # now it ends, for real

    await_run(page)                 # and the ending that does arrive is the one we waited for
    assert "Partial work" in ui.log_text()
    assert not page.evaluate(
        "() => document.getElementById('statePill').classList.contains('live')")


def test_a_finished_run_cannot_stand_in_for_a_later_one_that_is_still_going(ui):
    """The two failure modes together: one run over, the next still open."""
    page = ui.page
    ui.ask("Read README.md and tell me what this tool does")

    page.fill("#input", "Hold queue fixture")
    page.press("#input", "Enter")
    with pytest.raises(PWTimeout):
        await_run(page)

    assert not _Fixture.queue_release.is_set()
    assert len(_Fixture.stream_requests) == 2
    _Fixture.queue_release.set()


# ------------------------------------------------- the shapes other suites call it in
def test_a_direct_post_click_caller_still_observes_an_ordinary_run(ui):
    """tests/test_web_verification_timeout_ui.py and the IME suites call it exactly like this."""
    page = ui.page
    page.fill("#input", "Bundle the release assets")
    page.press("#input", "Enter")
    await_run(page)

    assert "Bundled the release assets." in ui.log_text()
    assert page.get_attribute("#gate", "data-state") == "inconclusive"


def test_a_page_opened_by_a_suite_of_its_own_is_driven_the_same_way(server, browser):
    """The context other suites build by hand, finishing before the driver's first look."""
    _reset_fixture_state()
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        page.wait_for_timeout(400)      # identity + model probes settle, as in the `ui` fixture
        _route_complete_run(page)
        _send_and_let_it_finish(page)

        await_run(page)

        _the_run_really_finished(page)
        assert errors == [], "JS errors: %r" % errors
    finally:
        context.close()
