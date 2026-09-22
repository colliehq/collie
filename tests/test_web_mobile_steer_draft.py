"""On the phone, words stay in the composer until the server says it took them.

Steering a live run from the phone emptied the box the instant the POST left the browser. When the
server refused (`403`, `503`), when the request never came back, or when it answered without a
receipt, all the person saw was a red "Steer not delivered" banner and an empty composer: the
sentence they had typed — often the careful one, in a language that takes a keyboard round-trip per
character — was gone, with nothing to retry but their memory.

These tests drive the real mobile.html in Chromium against the same staged HTTP fixture as
test_web_ui_run_status. The run is the fixture's held stream, so the composer is genuinely in
steering mode, and `/api/steer` is answered by this file, one request at a time, whenever a test
decides to answer it — which is how the window *while a POST is pending* becomes observable at all.

What they assert is what a person sees: the typed text (its spacing and line breaks included) is
still in the box and still editable, one Enter is one POST, an accepted steer clears exactly the
draft that was accepted and nothing newer, and a late answer from an abandoned chat neither empties
nor banners the chat that replaced it. Nothing is ever re-POSTed without the person pressing send.

Feedback is owned the same way, and two steers can be in flight at once: the tests below hold both
POSTs open and answer them in either order. An older receipt must not take back a newer request's
failure banner, and an older failure that lost the race must still be reported — as the sentence it
belongs to, never as if it were the latest attempt.

Coverage and its limit. This is real Chromium and a real HTTP round-trip against a mock backend: no
inference runs, and no physical phone or OS keyboard is involved, so nothing here is evidence about
a real Android/iOS IME. A refusal carries an HTTP status and is therefore proof of non-delivery; an
aborted request and a receipt-less answer are not, and are only ever reported as unconfirmed here.
"""
import json
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import phone, server, browser, _Fixture   # noqa: F401

DRAFT = "  请保留这条尚未接收的要求\n第二行  "   # what is in the box, spacing and line break included
SENT = "请保留这条尚未接收的要求\n第二行"        # what a POST may carry: trimmed, nothing else changed
LATER = DRAFT + "还有一句"                       # what the person adds while the answer is still out
UNPROVEN = "may have arrived"                    # the banner for a request that never came back
FIRST = "先检查配置"                             # two steers sent before either is answered:
SECOND = "再保留第二条要求"                       # the answers may come back in the other order


class Steer:
    """Every POST to /api/steer, held until this test answers it."""

    def __init__(self, page):
        self.page = page
        self.bodies = []
        self._held = []
        page.route("**/api/steer**", self._catch)

    def _catch(self, route):
        self.bodies.append(route.request.post_data_json)
        self._held.append(route)

    def wait_for_post(self, count=1, timeout=8000):
        """Let the page run until it has POSTed `count` times (the requests stay unanswered)."""
        waited = 0
        while len(self.bodies) < count and waited < timeout:
            self.page.wait_for_timeout(50)
            waited += 50
        assert len(self.bodies) >= count, "expected %d POST(s), saw %r" % (count, self.bodies)

    def _take(self, which):
        """The `which`-th POST the page made (0 is the first), which must still be unanswered."""
        route = self._held[which]
        assert route is not None, "POST %d has already been answered" % which
        self._held[which] = None
        return route

    def answer(self, status, payload, which=0):
        self.raw(status, json.dumps(payload), which)

    def raw(self, status, body, which=0):
        """Answer one held POST with exactly this body, parseable or not."""
        self._take(which).fulfill(status=status, content_type="application/json", body=body)

    def accept(self, which=0):
        self.answer(200, {"queued": True}, which)

    def abort(self, which=0):
        self._take(which).abort()

    def drain(self):
        for i, route in enumerate(self._held):
            if route is not None:
                self._take(i).abort()


def posted(page, path):
    """Every body the page POSTs to `path`, counted even if a route never answers it."""
    seen = []
    page.on("request", lambda r: r.method == "POST" and urlparse(r.url).path == path
            and seen.append(json.loads(r.post_data or "{}")))
    return seen


@pytest.fixture
def steer(phone):
    """A phone inside a live steerable run, with /api/steer under this test's control."""
    control = Steer(phone.page)
    page = phone.page
    page.locator("#input").fill("Hold queue fixture")
    page.locator("#input").press("Enter")
    expect(page.locator("#input")).to_have_attribute("placeholder", "Steer while Collie works…")
    yield control
    control.drain()
    _Fixture.queue_release.set()
    page.wait_for_timeout(100)


def send(page, text):
    page.locator("#input").fill(text)
    page.locator("#input").press("Enter")


def send_both(steer, page):
    """Two steers typed one after the other, both POSTed, neither answered yet."""
    send(page, FIRST)
    steer.wait_for_post(1)
    send(page, SECOND)
    steer.wait_for_post(2)
    assert steer.bodies == [{"session": "s-read", "q": FIRST}, {"session": "s-read", "q": SECOND}]


@pytest.mark.parametrize("status", [403, 503])
def test_a_refused_steer_keeps_every_typed_character_and_posts_once(steer, phone, status):
    page = phone.page
    send(page, DRAFT)
    steer.wait_for_post()
    expect(page.locator("#input")).to_have_value(DRAFT)     # nothing is given up while it is pending
    steer.answer(status, {"error": "steer refused by fixture"})
    expect(page.locator(".banner")).to_contain_text("Steer not delivered")
    expect(page.locator("#input")).to_have_value(DRAFT)     # a refusal is not a reason to lose words
    assert page.locator("#input").is_editable()
    assert steer.bodies == [{"session": "s-read", "q": SENT}]
    assert not _Fixture.queue_entries, "a refused steer is queued nowhere"
    page.wait_for_timeout(600)
    assert steer.bodies == [{"session": "s-read", "q": SENT}], "a refusal is never replayed by itself"


def test_a_steer_that_never_came_back_keeps_the_words_without_claiming_non_delivery(steer, phone):
    page = phone.page
    send(page, DRAFT)
    steer.wait_for_post()
    steer.abort()                                           # the connection drops mid-request
    expect(page.locator(".banner")).to_contain_text(UNPROVEN)
    assert "Steer not delivered" not in page.inner_text(".banner"), \
        "an unanswered POST may already have been accepted upstream"
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert steer.bodies == [{"session": "s-read", "q": SENT}]
    page.wait_for_timeout(600)
    assert len(steer.bodies) == 1, "an ambiguous failure is never resent on its own"


@pytest.mark.parametrize("body", ['{"queued": tr', '{"ok": true}'])
def test_an_answer_without_a_receipt_keeps_the_draft(steer, phone, body):
    page = phone.page
    send(page, DRAFT)
    steer.wait_for_post()
    steer.raw(200, body)                                    # unreadable, or readable but silent
    expect(page.locator(".banner")).to_contain_text(UNPROVEN)
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert steer.bodies == [{"session": "s-read", "q": SENT}]
    assert UNPROVEN in page.inner_text("#log"), "an unread answer is reported as unread"


def test_two_enters_on_one_pending_draft_send_one_post_and_keep_the_controls(steer, phone):
    page = phone.page
    all_posts = posted(page, "/api/steer")
    send(page, DRAFT)
    steer.wait_for_post()
    expect(page.locator("#input")).to_have_value(DRAFT)
    page.locator("#input").press("Enter")                   # the slip: pressed again while waiting
    page.wait_for_timeout(400)
    assert steer.bodies == [{"session": "s-read", "q": SENT}], "one draft, one request"
    assert len(all_posts) == 1
    expect(page.locator("#send")).to_have_attribute("aria-label", "Stop run")
    assert page.locator("#send").is_enabled(), "waiting must not take the stop button away"
    assert page.locator("#input").is_editable()
    steer.accept()
    expect(page.locator("#input")).to_have_value("")
    assert len(steer.bodies) == 1


def test_an_accepted_steer_clears_exactly_that_draft(steer, phone):
    page = phone.page
    send(page, DRAFT)
    steer.wait_for_post()
    steer.accept()
    expect(page.locator("#input")).to_have_value("")
    expect(page.locator("#log")).to_contain_text("请保留这条尚未接收的要求")
    assert page.locator(".banner").count() == 0
    assert steer.bodies == [{"session": "s-read", "q": SENT}]


@pytest.mark.parametrize("outcome", ["accepted", "refused"])
def test_typing_while_a_steer_is_pending_survives_its_answer(steer, phone, outcome):
    page = phone.page
    send(page, DRAFT)
    steer.wait_for_post()
    expect(page.locator("#input")).to_have_value(DRAFT)
    page.locator("#input").fill(LATER)                      # the person keeps writing meanwhile
    if outcome == "accepted":
        steer.accept()
    else:
        steer.answer(503, {"error": "steer refused by fixture"})
    page.wait_for_timeout(400)
    expect(page.locator("#input")).to_have_value(LATER)     # an answer owns the old draft, not this
    assert steer.bodies == [{"session": "s-read", "q": SENT}], "newer typing is nobody's request yet"


@pytest.mark.parametrize("outcome", ["accepted", "refused"])
def test_a_late_answer_does_not_touch_the_chat_that_replaced_it(steer, phone, outcome):
    page = phone.page
    send(page, DRAFT)
    steer.wait_for_post()
    page.locator("#newBtn").click()                         # a new chat while the answer is out
    fresh = "新话题的草稿"
    page.locator("#input").fill(fresh)
    if outcome == "accepted":
        steer.accept()
    else:
        steer.answer(403, {"error": "steer refused by fixture"})
    page.wait_for_timeout(500)
    expect(page.locator("#input")).to_have_value(fresh)
    assert page.locator(".banner").count() == 0, "a stale answer banners the chat it belonged to"
    assert page.locator("#log .empty").count() == 1, "the new chat is still the new chat"
    assert page.locator("#input").is_editable()
    assert len(steer.bodies) == 1


def test_an_explicit_retry_after_a_refusal_posts_exactly_once_more(steer, phone):
    page = phone.page
    send(page, DRAFT)
    steer.wait_for_post()
    steer.answer(503, {"error": "steer refused by fixture"})
    expect(page.locator(".banner")).to_contain_text("Steer not delivered")
    expect(page.locator("#input")).to_have_value(DRAFT)
    page.locator("#input").press("Enter")                   # the person decides to send it again
    steer.wait_for_post(2)
    steer.accept(which=1)                                   # the second POST, the one they just made
    expect(page.locator("#input")).to_have_value("")
    assert steer.bodies == [{"session": "s-read", "q": SENT}] * 2, "the retry is the person's, once"
    page.wait_for_timeout(600)
    assert len(steer.bodies) == 2


def test_an_older_receipt_cannot_erase_the_newer_steers_failure(steer, phone):
    page = phone.page
    send_both(steer, page)
    steer.answer(503, {"error": "steer refused by fixture"}, which=1)   # the newer one is refused
    expect(page.locator(".banner")).to_have_text("Steer not delivered")
    steer.accept(which=0)                                   # the older one is acknowledged afterwards
    page.wait_for_timeout(400)
    expect(page.locator(".banner")).to_have_text("Steer not delivered")  # still the refused one's banner
    expect(page.locator("#input")).to_have_value(SECOND)    # and still the refused words, editable
    assert page.locator("#input").is_editable()
    assert FIRST in page.inner_text("#log"), "the steer that was taken is logged as taken"
    assert len(steer.bodies) == 2, "neither answer causes a resend"


def test_a_failure_that_lost_the_race_names_its_own_sentence(steer, phone):
    page = phone.page
    send_both(steer, page)
    steer.accept(which=1)                                   # the newer steer is taken and clears its draft
    expect(page.locator("#input")).to_have_value("")
    assert page.locator(".banner").count() == 0
    steer.answer(403, {"error": "steer refused by fixture"}, which=0)   # the older one was refused all along
    banner = page.locator(".banner")
    expect(banner).to_contain_text("Earlier steer not delivered")
    expect(banner).to_contain_text(FIRST)                   # which sentence did not arrive
    assert SECOND not in page.inner_text(".banner"), "the steer that was taken is not the one reported"
    assert "your words are kept" not in page.inner_text(".banner"), \
        "that draft is no longer the one in the box, so the banner must not promise it is"
    expect(page.locator("#input")).to_have_value("")        # nothing is typed back into the composer
    page.wait_for_timeout(400)
    assert len(steer.bodies) == 2, "a late refusal is never replayed by itself"


def test_an_older_unproven_failure_leaves_the_newer_refusal_on_screen(steer, phone):
    page = phone.page
    send_both(steer, page)
    steer.answer(503, {"error": "steer refused by fixture"}, which=1)
    expect(page.locator(".banner")).to_have_text("Steer not delivered")
    steer.abort(which=0)                                    # the older request never comes back at all
    page.wait_for_timeout(400)
    expect(page.locator(".banner")).to_have_text("Steer not delivered")
    assert page.locator(".banner").count() == 1, "two complaints do not stack into two banners"
    log = page.inner_text("#log")
    assert UNPROVEN in log and FIRST in log, "the older failure is still recorded, not hidden"


def test_a_newer_receipt_still_clears_the_older_complaint(steer, phone):
    page = phone.page
    send_both(steer, page)
    steer.answer(503, {"error": "steer refused by fixture"}, which=0)   # the older one is refused first
    expect(page.locator(".banner")).to_have_text("Steer not delivered")
    steer.accept(which=1)                                   # then the newer one is taken
    expect(page.locator("#input")).to_have_value("")
    page.wait_for_timeout(200)
    assert page.locator(".banner").count() == 0, "a stale complaint does not outlive a later receipt"
    assert FIRST in page.inner_text("#log"), "the refused sentence stays readable in the transcript"


def test_an_earlier_failure_after_a_new_chat_stays_out_of_it(steer, phone):
    page = phone.page
    send_both(steer, page)
    steer.accept(which=1)
    page.locator("#newBtn").click()                         # the person moves on while an answer is out
    fresh = "新话题的草稿"
    page.locator("#input").fill(fresh)
    steer.answer(403, {"error": "steer refused by fixture"}, which=0)
    page.wait_for_timeout(500)
    expect(page.locator("#input")).to_have_value(fresh)
    assert page.locator(".banner").count() == 0, "an abandoned chat's failure belongs to that chat"
    assert page.locator("#log .empty").count() == 1, "the new chat is still the new chat"
    assert len(steer.bodies) == 2
