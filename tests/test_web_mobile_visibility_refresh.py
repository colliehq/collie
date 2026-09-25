"""A phone left open while the desktop changes model or worker re-reads run capabilities on return.

The real mobile.html in Chromium (the fixture shared with test_web_ui_run_status), with
``/api/run-capabilities`` answered by interception. Page visibility is staged by redefining
``document.visibilityState`` and dispatching the event the browser itself would send.
"""
from test_web_ui_run_status import browser, phone, server  # noqa: F401  (pytest fixtures)


def _set_visibility(page, state):
    page.evaluate("""s => { Object.defineProperty(document, 'visibilityState',
                                                  {value: s, configurable: true});
                           document.dispatchEvent(new Event('visibilitychange')); }""", state)


def test_returning_to_the_page_rereads_what_the_next_run_can_use(phone):
    """A model changed on the desktop while the phone was put away shows up on return."""
    page = phone.page
    answers = [{"speed_tiers": ["standard"], "reasoning_efforts": [], "workers": []},
               {"speed_tiers": ["standard", "fast"], "reasoning_efforts": [], "workers": []}]
    reads = []

    def caps(route):
        reads.append(1)
        route.fulfill(json=answers[min(len(reads) - 1, 1)])

    page.route("**/api/run-capabilities*", caps)
    page.evaluate("() => refreshRunCapabilities()")
    page.wait_for_function("() => document.getElementById('mFastOption').hidden === true")
    _set_visibility(page, "hidden")
    assert len(reads) == 1                        # going away reads nothing
    _set_visibility(page, "visible")
    page.wait_for_function("() => document.getElementById('mFastOption').hidden === false")
    assert len(reads) == 2
