"""What the phone's run options say about the route, after the desktop has changed it.

`/api/run-capabilities` is read per provider/model: which speed tiers that route reports, which
reasoning efforts it accepts, which worker is its default, and what each worker row is labelled
with. So the answer the phone is holding describes *a* route, and it stops describing anything the
moment someone saves a different provider or model on the desktop.

mobile.html read it once, in the bootstrap `.finally(...)`, and never again. A phone left open on
the sofa kept offering the old route's options for as long as it stayed open: a Fast item that the
new route does not have, reasoning efforts it does not accept, and worker rows labelled from a
probe that has since been replaced. Nothing on the phone corrected it short of a reload.

The reads that a fix adds bring the other half of the same staleness with them: a capability read
is not instantaneous, so a read started at the older boundary can land *after* a newer one has
already been applied. Applied unconditionally — in the fulfilled path or the rejected one — it puts
the old route's tiers back over the new route's, which is the original bug with extra steps.

Everything here drives the real `harness/webui/mobile.html` in Chromium against the staged HTTP
fixture shared with test_web_ui_run_status, with `/api/run-capabilities` answered by Playwright
interception over invented capability payloads. No provider, account, key, setting or live
discovery is touched: the tiers, efforts and worker rows below are staged descriptions, not claims
about any real route. The boundary under test is the one a person reaches with a real gesture here:
opening the run options. Nothing waits by sleeping — a held answer is released and then a *later*
request over the same endpoint is made and waited for, so the released answer is known to have been
delivered to the page first.
"""
import threading

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import server, browser, TOKEN   # noqa: F401

# Two staged descriptions of the same endpoint: the route the phone booted on, and the route the
# desktop saves while the phone sits open. "fast" here is a staged tier on an invented row.
BEFORE = {"speed_tiers": ["standard", "fast"], "interactive_speed_default": "fast",
          "reasoning_efforts": ["low", "medium", "high"], "worker_default": "collie",
          "workers": [{"key": "codex-exec", "probe": {"runnable": True,
                                                      "availability": "route-before",
                                                      "detail": "before"}}],
          "worker_signals": {}}
AFTER = {"speed_tiers": ["standard"], "interactive_speed_default": "standard",
         "reasoning_efforts": ["low"], "worker_default": "collie",
         "workers": [{"key": "codex-exec", "probe": {"runnable": True,
                                                     "availability": "route-after",
                                                     "detail": "after"}}],
         "worker_signals": {}}
SAME_ROUTE_REREAD = {"speed_tiers": ["standard", "fast"], "interactive_speed_default": "fast",
                     "reasoning_efforts": ["low", "medium", "high"], "worker_default": "collie",
                     "workers": [{"key": "codex-exec",
                                  "probe": {"runnable": True, "availability": "route-before-again",
                                            "detail": "before"}}],
                     "worker_signals": {}}
STALE = {"speed_tiers": ["standard", "fast"], "interactive_speed_default": "fast",
         "reasoning_efforts": ["low", "medium", "high"], "worker_default": "collie",
         "workers": [{"key": "codex-exec", "probe": {"runnable": True,
                                                     "availability": "route-stale",
                                                     "detail": "stale"}}],
         "worker_signals": {}}


# Installed before the page's own scripts run, so it is already watching when the bootstrap read
# lands. It records, from outside, every state the run options are drawn in — the worker row's text
# and availability, whether Fast is offered, whether a high effort is accepted. A stale answer that
# is applied and then painted over by a later read still leaves its mark here.
_RECORDER_JS = r"""
(() => {
  if (window.__capLog) return;
  window.__capLog = [];
  function snap() {
    var row = document.querySelector('#mRunner option[value="codex-exec"]'),
        fast = document.getElementById("mFastOption"),
        high = document.querySelector('#mEffort option[value="high"]');
    if (!row || !fast || !high) return;
    var now = {label: row.textContent, note: row.title || "", fast: !fast.hidden,
               available: row.dataset.available || "", high: !high.disabled},
        last = window.__capLog[window.__capLog.length - 1];
    if (last && JSON.stringify(last) === JSON.stringify(now)) return;
    window.__capLog.push(now);
  }
  function watch() {
    if (!document.getElementById("mRunner")) return false;
    snap();
    var opts = {subtree: true, childList: true, characterData: true, attributes: true};
    new MutationObserver(snap).observe(document.getElementById("mRunner"), opts);
    new MutationObserver(snap).observe(document.getElementById("mEffort"), opts);
    new MutationObserver(snap).observe(document.getElementById("mFastOption"), {attributes: true});
    return true;
  }
  if (!watch()) document.addEventListener("DOMContentLoaded", watch);
})();
"""


class Phone:
    """One phone page, with every capability read recorded and holdable."""

    def __init__(self, page, errors):
        self.page = page
        self.errors = errors
        self.lock = threading.Lock()
        self.reads = 0           # every /api/run-capabilities the page asked for
        self.held = []           # reads caught before they may answer
        self.hold_next = False
        self.answer = BEFORE
        self.fail_next = False   # answer the next read the way an unreachable backend does

    def _caps(self, route):
        with self.lock:
            self.reads += 1
            if self.hold_next:
                self.hold_next = False
                self.held.append(route)
                return
            fail, answer = self.fail_next, self.answer
            self.fail_next = False
        if fail:
            route.abort("failed")
        else:
            route.fulfill(json=answer)

    def open(self, base):
        self.page.route("**/api/run-capabilities*", self._caps)
        self.page.add_init_script(_RECORDER_JS)
        self.page.goto(base + "/m?token=" + TOKEN, wait_until="load")
        self.page.wait_for_selector("#input", timeout=8000)
        self.page.wait_for_function("() => window.runCapabilities !== null", timeout=8000)
        return self

    def release(self, answer):
        """Answer a read that a later read has already overtaken."""
        route = self.held.pop(0)
        if answer is None:
            route.abort("failed")
        else:
            route.fulfill(json=answer)
        return self

    def shown(self):
        """Every distinct state the run options have been drawn in, in order."""
        return self.page.evaluate("() => window.__capLog")

    def shown_since(self, name):
        """...from the moment the answer carrying `name` was applied."""
        log = self.shown()
        for i, row in enumerate(log):
            if name in (row["label"] or ""):
                return log[i:]
        raise AssertionError("%r was never drawn; the page showed %r" % (name, log))

    def barrier(self):
        """Make a later read and wait for what it draws.

        Anything released before this returns was delivered to the page ahead of the answer that is
        now on screen, so "it changed nothing" is an observation rather than a guess. The barrier
        answer repaints the panel itself, so what a late answer did is read off the recorder above
        rather than off the panel as it stands afterwards.
        """
        sentinel = dict(AFTER)
        sentinel["workers"] = [{"key": "codex-exec",
                                "probe": {"runnable": True, "availability": "route-barrier",
                                          "detail": "barrier"}}]
        self.answer = sentinel
        if self.run_options_open():
            self.close_run_options()
        self.open_run_options()
        self.wait_for_route("route-barrier", timeout=8000)
        self.close_run_options()
        self.answer = AFTER
        return self

    def wait_for_reads(self, count, timeout=8000):
        """Wait until the page has asked for the route `count` times in total.

        The wait is spent inside the page, which is what dispatches the interception callbacks that
        do the counting; a plain sleep here would never see a request arrive.
        """
        waited = 0
        while waited < timeout:
            with self.lock:
                if self.reads >= count:
                    return self
            self.page.wait_for_timeout(25)
            waited += 25
        raise AssertionError("the page made %d capability reads, wanted %d" % (self.reads, count))

    def wait_for_route(self, name, timeout=5000):
        """Wait for the worker row the staged answer carries — the last thing a read applies."""
        expect(self.page.locator('#mRunner option[value="codex-exec"]')).to_contain_text(
            name, timeout=timeout)
        return self

    # -- what a person sees -------------------------------------------------
    def fast_offered(self):
        return self.page.evaluate("() => !document.getElementById('mFastOption').hidden")

    def speed(self):
        return self.page.evaluate("() => document.getElementById('mSpeed').value")

    def speed_is_explicit(self):
        return self.page.evaluate("() => !!(window.runExplicitAxes || {}).speed")

    def effort_enabled(self, value):
        return self.page.evaluate(
            "v => !document.querySelector('#mEffort option[value=\"' + v + '\"]').disabled", value)

    def worker_label(self):
        return self.page.inner_text('#mRunner option[value="codex-exec"]')

    def worker_available(self):
        return self.page.evaluate(
            "() => document.querySelector('#mRunner option[value=\"codex-exec\"]')"
            ".dataset.available")

    def worker_note(self):
        return self.page.get_attribute('#mRunner option[value="codex-exec"]', "title") or ""

    def run_options_open(self):
        return self.page.evaluate("() => document.getElementById('runSetup').open")

    def summary(self):
        return self.page.inner_text("#runSummary")

    # -- what a person does -------------------------------------------------
    def open_run_options(self):
        reads = self.reads
        self.page.click("#runSetup > summary")
        expect(self.page.locator("#runSetup")).to_have_attribute("open", "")
        # The details element queues its toggle event after changing `open`.
        # Wait for this gesture's request before changing the staged answer or
        # failure flag, otherwise a slow host can consume it for the prior open.
        self.wait_for_reads(reads + 1)
        return self

    def close_run_options(self):
        self.page.click("#runSetup > summary")
        expect(self.page.locator("#runSetup")).not_to_have_attribute("open", "")
        return self

    def choose_fast(self):
        """Pick the Fast tier the way the run options offer it."""
        self.page.select_option("#mSpeed", "fast")
        assert self.speed() == "fast" and self.speed_is_explicit()
        return self

    def choose_effort(self, value):
        self.page.select_option("#mEffort", value)
        assert self.page.evaluate("() => document.getElementById('mEffort').value") == value
        return self

@pytest.fixture
def phone(server, browser):
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    client = Phone(page, errors).open(server)
    yield client
    assert errors == [], "JS errors: %r" % errors
    context.close()


# ------------------------------- the route actually changing

def test_opening_run_options_shows_the_route_that_is_current_not_the_one_the_page_booted_on(phone):
    phone.open_run_options()
    assert phone.fast_offered(), "the booted route offers Fast"
    assert phone.effort_enabled("high")
    assert "route-before" in phone.worker_label()
    phone.close_run_options()

    phone.answer = AFTER                      # the desktop saves a different provider/model

    phone.open_run_options()
    phone.wait_for_route("route-after")        # a stale page keeps showing the old row instead

    assert not phone.fast_offered(), "Fast is still offered for a route that does not have it"
    assert not phone.effort_enabled("high"), "an effort the current route does not accept"


def test_a_fast_selection_made_for_the_old_route_is_not_left_pointing_at_a_tier_that_is_gone(phone):
    """The existing policy for a route without Fast, now reached by a refresh instead of a reload."""
    phone.open_run_options()
    phone.choose_fast()
    assert "Fast" in phone.summary()

    phone.answer = AFTER
    phone.close_run_options()
    phone.open_run_options()
    phone.wait_for_route("route-after")

    assert not phone.fast_offered()
    assert phone.speed() == "standard", "left selecting a tier the current route does not have"
    assert not phone.speed_is_explicit(), "the dropped selection is not still claimed as explicit"
    assert "Fast" not in phone.summary()


def test_an_explicit_selection_the_current_route_still_accepts_is_kept_across_a_refresh(phone):
    phone.open_run_options()
    phone.choose_effort("low")
    phone.choose_fast()

    phone.answer = SAME_ROUTE_REREAD          # the same capabilities, read again
    phone.close_run_options()
    phone.open_run_options()
    phone.wait_for_route("route-before-again")

    assert phone.page.evaluate("() => document.getElementById('mEffort').value") == "low"
    assert phone.speed() == "fast" and phone.speed_is_explicit()
    assert "Fast" in phone.summary()


# ------------------------------- answers that arrive out of order

def test_a_slow_answer_for_the_old_route_does_not_replace_the_route_that_is_current(phone):
    reads = phone.reads
    phone.hold_next = True
    phone.open_run_options()                  # this read is caught before it may answer
    phone.wait_for_reads(reads + 1)

    phone.answer = AFTER
    phone.close_run_options()
    phone.open_run_options()                  # a newer read, answered for the current route
    phone.wait_for_route("route-after", timeout=8000)

    phone.release(STALE)                      # the held answer lands late
    phone.barrier()                           # ...and is known to have been delivered by now

    since = phone.shown_since("route-after")
    assert not any(row["fast"] for row in since),         "a late answer for the old route put Fast back: %r" % since
    assert not any(row["high"] for row in since),         "and put back an effort the current route refuses: %r" % since
    assert not any("route-stale" in row["label"] for row in since),         "the old route's worker row was drawn again: %r" % since


def test_a_slow_failure_for_the_old_route_does_not_wipe_the_route_that_is_current(phone):
    reads = phone.reads
    phone.hold_next = True
    phone.open_run_options()
    phone.wait_for_reads(reads + 1)

    phone.answer = AFTER
    phone.close_run_options()
    phone.open_run_options()
    phone.wait_for_route("route-after", timeout=8000)

    phone.release(None)                       # the held read fails, late
    phone.barrier()

    assert phone.worker_available() == "true", \
        "a late failure disabled a worker the current route reports as runnable"
    assert "Capabilities unavailable" not in phone.worker_label()


def test_a_read_that_fails_now_still_falls_back_the_way_it_always_did(phone):
    """The failure policy itself is unchanged, just reachable at a boundary other than a reload.

    What it always did, and still does: forget the capabilities, take Fast off the list, and mark
    the two external workers unavailable. It does not rewrite a speed the person chose — that is
    the fulfilled path's job, when a route that really lacks Fast has actually answered — so the
    selection is left alone here rather than dropped on a read that told us nothing.
    """
    phone.open_run_options()
    phone.choose_fast()

    phone.fail_next = True
    phone.close_run_options()
    phone.open_run_options()

    phone.page.wait_for_function(
        "() => document.querySelector('#mRunner option[value=\"codex-exec\"]')"
        "        .dataset.available === 'false'", timeout=8000)
    assert not phone.fast_offered()
    assert "Capabilities unavailable" in phone.worker_note()
    assert phone.page.evaluate("() => window.runCapabilities") == {}
    assert phone.speed() == "fast" and phone.speed_is_explicit(), \
        "a read that failed discarded a choice it had no answer about"


# ------------------------------- and no new background traffic

def test_the_page_does_not_start_polling_the_route_on_its_own(phone):
    before = phone.reads
    phone.page.wait_for_timeout(3000)
    assert phone.reads == before, "the route is being polled: %d reads while nobody touched it" % (
        phone.reads - before)


def test_one_open_gesture_is_one_read(phone):
    before = phone.reads
    phone.open_run_options()
    phone.page.wait_for_timeout(400)
    assert phone.reads == before + 1, "opening the panel reads the route more than once"
    phone.close_run_options()
    phone.page.wait_for_timeout(400)
    assert phone.reads == before + 1, "closing the panel reads the route too"
