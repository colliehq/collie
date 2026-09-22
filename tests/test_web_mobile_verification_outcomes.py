"""On the phone, "not passed" was printed as "check failed".

The mobile receipt row asked one question of a check receipt — ``ev.passed`` — and painted the
answer in two colours.  So five different endings arrived at the same red sentence:

* the check the user stopped (``cancelled``),
* the check killed at its deadline (``timed_out``, no exit code at all),
* the check that never ran (``executed: false``),
* the check that exited 0 but whose result could not be bound to the files on disk
  (``command_passed: true``, ``passed: false``),
* and the one check that really did fail.

Only the last of those is a failure.  ``harness.verification.check_result_state`` has told these
apart for a while, and index.html already shows them apart; the phone — often the only screen a
result is read on — still said "check failed" with the command beside it, sending a reader hunting
for a broken test nothing here has evidence of.  A stopped check could even carry ``passed: true``
and would then have certified a workspace nobody finished checking.

These tests drive the real mobile.html in Chromium against the same staged HTTP/SSE fixture as
test_web_ui_run_status, over both entries that share ``onEvent``: a run this phone started, and a
run mirrored from another screen.  What they assert is what a person sees — the verb on the row,
its colour, and the factual detail beside it — plus one parity check that runs the page's own
classifier against the Python contract it is supposed to agree with.

Nothing is softened.  A failed check is still red and still says "check failed"; a passed check is
untouched; the run-error sentence the `done` frame already carried is left exactly as it was.  The
four uncertified endings simply stop claiming a verdict they never had, and print the deadline they
hit rather than "exit null".

Coverage and its limit.  Real Chromium, real HTTP and real SSE against a mock backend: no provider
and no check ever runs here, one browser engine, and nothing below is a statement about speed.
"""
import json
from contextlib import contextmanager

import pytest

from harness.verification import check_result_state

from test_web_ui_run_status import (  # noqa: F401  (pytest fixtures)
    TOKEN, _Fixture, _reset_fixture_state, browser, phone, server, mark_run, await_run,
)

# ---------------------------------------------------------------- staged receipts

# Killed at its deadline: no exit code, and a limit worth naming.
TIMED_OUT = {"command": "cargo test --all", "executed": True, "cancelled": False,
             "timed_out": True, "timeout_s": 900, "exit_code": None,
             "command_passed": False, "passed": False, "freshness": "timed_out",
             "output": "running 412 tests\n\n(check timed out after 900s)"}
TIMED_OUT_ERROR = ("required check ran out of time: cargo test --all was stopped after 900s "
                   "without finishing, so it judged nothing")
# The same ending without a recorded limit: still not a failure, still nothing to print as a number.
TIMED_OUT_NO_LIMIT = dict(TIMED_OUT, timeout_s=None)
# Stopped mid-run, with a stale pass riding along. It did not finish, so it certifies nothing.
STOPPED_BUT_PASSED = {"command": "pytest -q", "executed": True, "cancelled": True,
                      "passed": True, "command_passed": True, "exit_code": 0}
# Older evidence: a command and a bare verdict, nothing else. Must stay exactly what it says.
LEGACY_GREEN = {"command": "make check", "passed": True, "exit_code": 0}
LEGACY_RED = {"command": "make check", "passed": False, "exit_code": 2}


def _sse(events):
    return "".join("event: %s\ndata: %s\n\n" % (kind, json.dumps(data))
                   for kind, data in events)


def _run_frames(evidence, error="", canceled=False, session="s-evi", run="r-evi"):
    return [
        ("start", {"session": session, "run": run, "model": "mock", "prior_turns": 0}),
        ("verification_evidence", {"evidence": evidence}),
        ("done", {"session": session, "run": run, "answer": "Reworked the parser.",
                  "error": error, "canceled": canceled, "completed": not error,
                  "edited": True, "verified": False, "turns": 2,
                  "verification_evidence": evidence}),
    ]


def route_stream(page, frames):
    """Stage one SSE run for the next /api/stream this page opens."""
    page.route("**/api/stream?*", lambda route: route.fulfill(
        content_type="text/event-stream", body=_sse(frames)))


def route_mirror(page, frames):
    """Stage the run another screen is executing, as this phone's mirror will receive it."""
    page.route("**/api/mirror?*", lambda route: route.fulfill(
        content_type="text/event-stream", body=_sse(frames)))


# ---------------------------------------------------------------- driving the phone

def ask(phone, text):
    """Send one message from the phone composer and wait for the run to end."""
    phone.page.fill("#input", text)
    token = mark_run(phone.page)
    phone.page.press("#input", "Enter")
    # A fulfilled SSE response can finish between two Playwright calls. Observe
    # this send's actual start/end edges instead of sampling the transient Stop
    # button after the run may already have ended.
    await_run(phone.page, token)


def open_thread(phone, title):
    """Open a saved chat from the drawer — the entry that subscribes this phone to the mirror."""
    page = phone.page
    page.click("#menuBtn")
    page.wait_for_selector("#threads .thread", timeout=8000)
    page.locator("#threads .thread", has_text=title).first.click()
    page.wait_for_timeout(150)


def steps(page):
    """Every step row currently on the page: its verb, its detail, and its tone class."""
    return page.eval_on_selector_all(".step", """els => els.map(el => ({
        verb: el.querySelector('.v').textContent,
        detail: el.lastElementChild.textContent,
        cls: el.className,
        color: getComputedStyle(el.querySelector('.v')).color}))""")


def check_row(page, verb):
    """The one step row for a check receipt, found by the verb a reader would see."""
    found = [row for row in steps(page) if row["verb"] == verb]
    assert len(found) == 1, "expected exactly one %r row, saw %r" % (verb, steps(page))
    return found[0]


def palette(page):
    """What --bad, --warn and --brand actually resolve to on this page."""
    return page.evaluate("""() => {
        const probe = (name) => {
          const el = document.createElement('span');
          el.style.color = 'var(--' + name + ')';
          document.body.appendChild(el);
          const c = getComputedStyle(el).color; el.remove(); return c;
        };
        return {bad: probe('bad'), warn: probe('warn'), brand: probe('brand')};
    }""")


@contextmanager
def phone_in(server, browser, lang):
    """A phone client whose reader's language is `lang`."""
    _reset_fixture_state()
    _Fixture.lang = lang
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/m?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        page.wait_for_timeout(250)

        class _P:
            pass
        client = _P()
        client.page, client.errors = page, errors
        yield client
        assert errors == [], "JS errors: %r" % errors
    finally:
        _Fixture.lang = "en"
        context.close()


# ---------------------------------------------------------------- the two controls

def test_a_check_that_really_failed_is_still_red_and_still_says_failed(phone):
    """The negative control: nothing here may soften a real failure."""
    ask(phone, "Break the release build")
    row = check_row(phone.page, "check failed")

    assert "bad" in row["cls"].split(), row
    assert row["color"] == palette(phone.page)["bad"], row
    assert "pytest -q" in row["detail"], row
    assert "exit 1" in row["detail"], "the exit code it really reported is still shown"
    # And the run's own error sentence is untouched.
    assert "required check failed: pytest -q (exit 1)" in phone.page.inner_text("#log")


def test_a_check_that_passed_keeps_the_row_it_already_had(phone):
    """The positive control: a green check is not dragged into the caution styling."""
    ask(phone, "Fix the parser")
    row = check_row(phone.page, "check passed")
    colors = palette(phone.page)

    assert row["cls"].split() == ["step"], "a pass carries no tone class, exactly as before"
    assert row["detail"] == "pytest -q tests/test_parser.py"
    assert row["color"] == colors["brand"] and row["color"] not in (colors["bad"], colors["warn"])
    assert "check failed" not in phone.page.inner_text("#log")


# ---------------------------------------------------------------- the four uncertified endings

def test_a_check_stopped_by_the_user_is_not_reported_as_a_failed_check(phone):
    ask(phone, "Cancel the required check")
    text = phone.page.inner_text("#log")
    row = check_row(phone.page, "check stopped")

    assert "warn" in row["cls"].split() and "bad" not in row["cls"].split(), row
    assert row["color"] == palette(phone.page)["warn"], row
    assert "python -m unittest" in row["detail"], "the command is still named"
    assert "stopped before it finished" in row["detail"], row
    assert "check failed" not in text, text
    assert "check passed" not in text, "a stopped check certifies nothing either"


def test_a_stopped_check_never_certifies_the_workspace_even_carrying_a_stale_pass(phone):
    """`cancelled` outranks `passed`, exactly as check_result_state orders them."""
    route_stream(phone.page, _run_frames(STOPPED_BUT_PASSED, canceled=True))
    ask(phone, "Rework the parser")
    text = phone.page.inner_text("#log")

    assert "check passed" not in text, "a killed check may never certify the workspace"
    row = check_row(phone.page, "check stopped")
    assert "warn" in row["cls"].split() and "bad" not in row["cls"].split(), row


def test_a_check_that_ran_out_of_time_names_its_deadline_instead_of_failing(phone):
    route_stream(phone.page, _run_frames(TIMED_OUT, error=TIMED_OUT_ERROR))
    ask(phone, "Rework the parser")
    text = phone.page.inner_text("#log")
    row = check_row(phone.page, "check ran out of time")

    assert "warn" in row["cls"].split() and "bad" not in row["cls"].split(), row
    assert row["color"] == palette(phone.page)["warn"], row
    assert "cargo test --all" in row["detail"]
    assert "stopped after 900s without finishing" in row["detail"], row
    assert "check failed" not in text, text
    assert "exit None" not in text and "exit null" not in text, text
    # The run-error sentence the `done` frame already carried is left exactly as it was.
    assert TIMED_OUT_ERROR in text


def test_a_deadline_with_no_recorded_limit_prints_no_number_and_no_null(phone):
    route_stream(phone.page, _run_frames(TIMED_OUT_NO_LIMIT, error=TIMED_OUT_ERROR))
    ask(phone, "Rework the parser")
    row = check_row(phone.page, "check ran out of time")

    assert row["detail"] == "cargo test --all · stopped before it finished", row
    assert "None" not in row["detail"] and "null" not in row["detail"], row


def test_a_check_that_never_ran_says_so_and_keeps_the_reason(phone):
    ask(phone, "Stop before checking")
    text = phone.page.inner_text("#log")
    row = check_row(phone.page, "check did not run")

    assert "warn" in row["cls"].split() and "bad" not in row["cls"].split(), row
    assert "pytest -q" in row["detail"], "the command it would have run is still named"
    assert "The user stopped the run." in row["detail"], row
    assert "check failed" not in text and "check passed" not in text, text


def test_exit_zero_that_could_not_be_bound_to_these_files_is_neither_pass_nor_fail(phone):
    ask(phone, "Bundle the release assets")
    text = phone.page.inner_text("#log")
    row = check_row(phone.page, "check did not certify this result")

    assert "warn" in row["cls"].split() and "bad" not in row["cls"].split(), row
    assert "npm run build" in row["detail"]
    assert "the workspace changed while it was running" in row["detail"], row
    assert "check failed" not in text and "check passed" not in text, text


# ---------------------------------------------------------------- older evidence

@pytest.mark.parametrize("evidence,verb,tone", [
    (LEGACY_GREEN, "check passed", []),
    (LEGACY_RED, "check failed", ["bad"]),
])
def test_evidence_that_carries_only_a_verdict_is_read_exactly_as_it_reads(phone, evidence, verb,
                                                                         tone):
    """No new state is invented for receipts that predate these fields, in either direction."""
    route_stream(phone.page, _run_frames(evidence))
    ask(phone, "Rework the parser")
    row = check_row(phone.page, verb)

    assert row["cls"].split() == ["step"] + tone, row
    assert row["detail"].startswith("make check"), row


def test_the_page_classifier_agrees_with_the_python_receipt_contract(phone):
    """One order of precedence, checked against harness.verification, not restated by hand."""
    receipts = [
        {}, {"passed": True}, {"passed": False}, {"command_passed": True, "passed": False},
        {"timed_out": True, "passed": False}, {"timed_out": True, "passed": True},
        {"cancelled": True, "passed": True}, {"cancelled": True, "timed_out": True},
        {"executed": False, "passed": True}, {"executed": False, "cancelled": True},
        {"executed": False, "timed_out": True}, {"command_passed": True, "cancelled": True},
        LEGACY_GREEN, LEGACY_RED, TIMED_OUT, STOPPED_BUT_PASSED,
    ]
    page_states = phone.page.evaluate(
        "rows => rows.map(r => mobileCheckState(r))", receipts)

    assert page_states == [check_result_state(r) for r in receipts], list(
        zip(receipts, page_states))


# ---------------------------------------------------------------- the mirrored entry

def test_a_mirrored_run_reports_the_same_ending_as_a_run_this_phone_started(phone):
    """Live and mirror share onEvent, so the fix must arrive on both without a second rule."""
    route_mirror(phone.page, _run_frames(TIMED_OUT, error=TIMED_OUT_ERROR, session="s-read",
                                         run="r-mirror"))
    open_thread(phone, "Read README.md")
    phone.page.wait_for_function(
        "() => [...document.querySelectorAll('.step .v')]"
        "        .some(v => v.textContent === 'check ran out of time')", timeout=8000)
    row = check_row(phone.page, "check ran out of time")
    text = phone.page.inner_text("#log")

    assert "warn" in row["cls"].split() and "bad" not in row["cls"].split(), row
    assert "stopped after 900s without finishing" in row["detail"], row
    assert "check failed" not in text, text
    assert "exit None" not in text and "exit null" not in text, text


# ---------------------------------------------------------------- the reader's language

@pytest.mark.parametrize("lang,timed_out,limit,inconclusive,why", [
    ("zh", "检查超时中止", "运行 900 秒未完成，已中止", "检查尚未确认当前结果",
     "它运行期间工作区发生了变化"),
    ("zh-tw", "檢查逾時中止", "執行 900 秒未完成，已中止", "檢查尚未確認目前結果",
     "它執行期間工作區發生了變化"),
])
def test_the_uncertified_endings_speak_the_readers_language(server, browser, lang, timed_out,
                                                            limit, inconclusive, why):
    with phone_in(server, browser, lang) as client:
        route_stream(client.page, _run_frames(TIMED_OUT, error=TIMED_OUT_ERROR))
        ask(client, "Rework the parser")
        row = check_row(client.page, timed_out)
        assert limit in row["detail"], row
        assert "cargo test --all" in row["detail"], "the command is never translated"

    with phone_in(server, browser, lang) as client:
        ask(client, "Bundle the release assets")
        row = check_row(client.page, inconclusive)
        assert why in row["detail"], row
        assert "npm run build" in row["detail"], row
