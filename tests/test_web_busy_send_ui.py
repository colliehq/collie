"""A send this window was not allowed to start must not cost the person their words.

`web_tasks.serve_managed_stream` refuses a stream whose session lease is already held,
and it does so *before* it reads the journal: no run row, nothing transmitted, nothing
written.  Only the browser had been wrong about the state of the thread — a tab opened
seconds before its first `/api/runs` poll, a second window or phone on the same
conversation, or the gap after a dropped stream, where `es.onerror` puts the composer
back into "Send" while `recoverAfterInterrupt` is still saying "Collie is still
working…".

The page used to answer that with an error and an emptied composer.  The optimistic
user bubble was then wiped by the next re-render of the real transcript, and the text
existed nowhere: not in the composer, not in the durable inbox, not in the journal.

So the refusal is routed to the durable queue instead — exactly where the same
keystroke goes one second later, once the window knows a run is live.  Nothing is
replayed by this: the refused stream never reached an effect.  And the promise is only
as strong as the storage, so a save that fails leaves the draft, attachments and all,
in the composer where the person can see it.
"""
import base64
import pytest

from playwright.sync_api import expect
from test_web_ui_run_status import ui, server, browser, _Fixture   # noqa: F401

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "ScLbtAAAAABJRU5ErkJggg==")

ASK = "Busy fixture — use the tests directory, not the whole tree"


def open_read_thread(page):
    page.locator(".thread").filter(has_text="Read README.md").first.click()
    expect(page).to_have_url(__import__("re").compile("session=s-read"))
    page.wait_for_timeout(200)


def attach(page):
    page.locator("#fileInput").set_input_files(
        {"name": "shot.png", "mimeType": "image/png", "buffer": PNG})
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)


def send(page, text):
    page.fill("#input", text)
    page.press("#input", "Enter")


def test_a_refused_start_becomes_a_pending_request_instead_of_lost_words(ui):
    page = ui.page
    open_read_thread(page)
    send(page, ASK)

    row = page.locator(".task-queue-row")
    expect(row).to_have_count(1)
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    expect(page.locator("#input")).to_have_value("")

    post = _Fixture.queue_posts[-1]
    assert post["text"] == ASK, post
    assert post["mode"] == "follow_up", "a refused start is the next turn, not a mid-turn steer"
    assert post["session"] == "s-read"
    # It is queued once, not queued and started.
    assert len([p for p in _Fixture.queue_posts if p["text"] == ASK]) == 1
    assert not _Fixture.queue_starts

    # No orphan user bubble claiming a turn that never happened.
    assert page.locator(".msg.user").filter(has_text=ASK).count() == 0
    # And the window is usable again rather than stuck in a run it never had.
    expect(page.locator("#send")).not_to_have_class(__import__("re").compile(r"\bstop\b"))


def test_a_refused_start_keeps_the_attachment_with_the_request(ui):
    page = ui.page
    open_read_thread(page)
    attach(page)
    send(page, ASK)

    expect(page.locator(".task-queue-row")).to_have_count(1)
    expect(page.locator("#attachStrip .thumb")).to_have_count(0)
    assert _Fixture.uploads, "the image must be uploaded for the queued request"
    # The refused stream uploaded it once; the queued request references its own
    # upload, which is the id the entry is stored against.
    assert _Fixture.queue_posts[-1]["images"] == ["upload-%d" % len(_Fixture.uploads)]


def test_a_failed_save_leaves_the_draft_and_its_attachment_in_the_composer(ui):
    page = ui.page
    _Fixture.queue_fail_once = True
    open_read_thread(page)
    attach(page)
    send(page, ASK)

    expect(page.locator("#taskQueueNotice")).to_contain_text("Your draft is kept")
    expect(page.locator("#input")).to_have_value(ASK)
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    assert page.locator(".task-queue-row").count() == 0


NEWER = "Actually — check the installer instead."


def test_a_newer_draft_typed_while_the_refusal_travels_is_not_overwritten(ui):
    """The refusal can take seconds. Words typed in the meantime are the person's
    current intent: the refused request goes to the queue, the new draft stays put."""
    page = ui.page
    _Fixture.busy_delay = 1.2
    open_read_thread(page)
    send(page, ASK)
    page.wait_for_timeout(150)
    page.fill("#input", NEWER)          # typed while the busy answer is still in flight

    row = page.locator(".task-queue-row")
    expect(row).to_have_count(1, timeout=5000)
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    # The newer draft was never submitted, so it is neither queued nor cleared by the ACK.
    expect(page.locator("#input")).to_have_value(NEWER)
    assert [p["text"] for p in _Fixture.queue_posts] == [ASK], _Fixture.queue_posts


def open_cap_thread(page):
    page.locator(".thread").filter(has_text="Migrate every module").first.click()
    expect(page).to_have_url(__import__("re").compile("session=s-cap"))
    page.wait_for_timeout(250)


def retained_rows(page):
    return page.locator(".task-queue-row.retained")


def test_a_refused_request_is_queued_against_the_thread_it_was_sent_from(ui):
    """Switching threads while the refusal travels must not lose the request, must not
    re-aim it, and must not touch the composer of the thread the person moved to."""
    page = ui.page
    _Fixture.busy_delay = 1.2
    open_read_thread(page)
    send(page, ASK)
    page.wait_for_timeout(150)
    open_cap_thread(page)
    page.wait_for_timeout(1800)

    posts = [p for p in _Fixture.queue_posts if p["text"] == ASK]
    assert len(posts) == 1, _Fixture.queue_posts
    assert posts[0]["session"] == "s-read", "queued against the thread it was sent from"
    assert posts[0]["mode"] == "follow_up"
    assert not _Fixture.queue_starts, "a refused start never starts a run elsewhere"
    # The thread the person switched to keeps its own empty composer and its own panel.
    expect(page.locator("#input")).to_have_value("")
    assert retained_rows(page).count() == 0
    assert page.locator(".task-queue-row").count() == 0, "s-cap shows no row for s-read's request"
    # Going back shows it where it belongs.
    open_read_thread(page)
    expect(page.locator(".task-queue-row")).to_have_count(1)
    expect(page.locator(".task-queue-text")).to_have_text(ASK)


def test_an_accepted_start_after_a_thread_switch_does_not_start_a_second_run(ui):
    """The detached listener exists for refusals. An accepted start is the server's run:
    it is left alone, never queued and never launched again."""
    page = ui.page
    _Fixture.start_delay = 1.2
    open_read_thread(page)
    send(page, "Accepted fixture — this one is allowed to run")
    page.wait_for_timeout(150)
    open_cap_thread(page)
    page.wait_for_timeout(1800)

    streams = [r for r in _Fixture.stream_requests if r["q"].startswith("Accepted fixture")]
    assert len(streams) == 1, streams
    assert not _Fixture.queue_posts, "an accepted send is not also queued"
    assert retained_rows(page).count() == 0, "an accepted send is not held back for review"
    expect(page.locator("#input")).to_have_value("")


def test_a_newer_thread_draft_and_attachment_survive_the_refusal_landing(ui):
    """The person left, and started writing something else with a file attached. The
    refusal that lands afterwards belongs to the old thread and must not touch any of it."""
    page = ui.page
    _Fixture.busy_delay = 1.5
    open_read_thread(page)
    send(page, ASK)
    page.wait_for_timeout(150)
    open_cap_thread(page)
    attach(page)
    page.fill("#input", NEWER)
    page.wait_for_timeout(1800)

    expect(page.locator("#input")).to_have_value(NEWER)
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    assert [p["text"] for p in _Fixture.queue_posts] == [ASK]
    assert _Fixture.queue_posts[0]["session"] == "s-read"


def test_the_refused_request_keeps_the_settings_it_was_sent_under(ui):
    """Run settings changed after the send are not the settings it was made under."""
    page = ui.page
    _Fixture.busy_delay = 1.4
    open_read_thread(page)
    page.evaluate("document.getElementById('runIntent').value = 'review'")
    send(page, ASK)
    page.wait_for_timeout(150)
    open_cap_thread(page)
    page.evaluate("document.getElementById('runIntent').value = 'build'")
    page.wait_for_timeout(1800)

    posts = [p for p in _Fixture.queue_posts if p["text"] == ASK]
    assert len(posts) == 1, _Fixture.queue_posts
    assert posts[0]["config"]["intent"] == "review", posts[0]["config"]


def test_a_failed_save_after_a_switch_is_retained_for_review_and_survives_return_and_reload(ui):
    """Nowhere durable would take it and the composer is another thread's. The request is
    kept whole — text, settings, attachment — under its own thread, visible and resendable,
    and it is never resent on its own."""
    page = ui.page
    _Fixture.busy_delay = 1.2
    _Fixture.queue_fail_once = True
    open_read_thread(page)
    attach(page)
    send(page, ASK)
    page.wait_for_timeout(150)
    open_cap_thread(page)
    page.wait_for_timeout(1800)

    # Not in the thread the person is reading.
    assert retained_rows(page).count() == 0
    open_read_thread(page)
    expect(retained_rows(page)).to_have_count(1)
    expect(retained_rows(page).locator(".task-queue-text")).to_have_text(ASK)
    expect(retained_rows(page).locator(".task-queue-meta")).to_contain_text("Save not confirmed")
    assert page.locator(".task-queue-row:not(.retained)").count() == 0, "not shown as queued"

    # A reload does not throw it away.
    page.reload(wait_until="load")
    page.wait_for_selector("#input")
    page.wait_for_timeout(600)
    expect(retained_rows(page)).to_have_count(1)
    expect(retained_rows(page).locator(".task-queue-text")).to_have_text(ASK)

    # Nothing was resent by itself; the resend is the person's.
    assert len([p for p in _Fixture.queue_posts if p["text"] == ASK]) == 1
    retained_rows(page).locator("button").first.click()
    expect(page.locator(".task-queue-row:not(.retained)")).to_have_count(1, timeout=5000)
    expect(retained_rows(page)).to_have_count(0)
    posts = [p for p in _Fixture.queue_posts if p["text"] == ASK]
    assert len(posts) == 2, posts
    assert posts[1]["session"] == "s-read"
    assert posts[1]["id"] == posts[0]["id"], "a resend carries the first attempt's identity"


def test_a_failed_save_behind_a_newer_draft_is_retained_not_dropped(ui):
    """The composer is not free (a newer draft) and the queue refused: the request still
    has to exist somewhere the person can see, without disturbing the newer draft."""
    page = ui.page
    _Fixture.busy_delay = 1.2
    _Fixture.queue_fail_once = True
    open_read_thread(page)
    send(page, ASK)
    page.wait_for_timeout(150)
    page.fill("#input", NEWER)
    page.wait_for_timeout(1800)

    expect(page.locator("#input")).to_have_value(NEWER)
    expect(retained_rows(page)).to_have_count(1)
    expect(retained_rows(page).locator(".task-queue-text")).to_have_text(ASK)


# --- fourth pass: what the third pass still lost -----------------------------------

def test_two_sends_of_the_same_words_are_two_requests_not_one(ui):
    """Same sentence, different attachments: two people's-worth of intent. Matching a
    retained record on its text alone merged them and threw one away."""
    page = ui.page
    _Fixture.queue_fail_all = True
    _Fixture.busy_delay = 0.9
    open_read_thread(page)
    attach(page)
    send(page, ASK)                       # first submission: with a file
    page.wait_for_timeout(150)
    open_cap_thread(page)
    page.wait_for_timeout(1400)

    open_read_thread(page)
    send(page, ASK)                       # second submission: same words, no file
    page.wait_for_timeout(150)
    open_cap_thread(page)
    page.wait_for_timeout(1400)

    open_read_thread(page)
    expect(retained_rows(page)).to_have_count(2)
    metas = retained_rows(page).locator(".task-queue-meta").all_inner_texts()
    assert sum("Attachments saved" in m for m in metas) == 1, metas


OVERFLOW = """
(() => {
  const real = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    if (key.indexOf('collie.retained') === 0 &&
        JSON.parse(value).some(row => row.images && row.images.length))
      throw new Error('QuotaExceededError');
    return real.call(this, key, value);
  };
})();
"""


def test_attachments_lost_to_storage_block_the_resend_until_they_are_back(ui):
    """The row says "reattach before sending". "Send again" used to post anyway, with
    an empty image list — the file silently gone from a request that says it has one."""
    page = ui.page
    page.add_init_script(OVERFLOW)        # survives the reload below
    page.evaluate(OVERFLOW)              # also affect the write before that reload
    _Fixture.queue_fail_once = True
    _Fixture.busy_delay = 0.9
    open_read_thread(page)
    attach(page)
    send(page, ASK)
    page.wait_for_timeout(150)
    open_cap_thread(page)
    page.wait_for_timeout(1500)
    open_read_thread(page)
    expect(retained_rows(page)).to_have_count(1)

    # The window still holds the file, so this window could still send it. What the storage
    # took is text and settings only — which is what a reload gets back.
    page.reload(wait_until="load")
    page.wait_for_selector("#input")
    page.wait_for_timeout(600)
    row = retained_rows(page)
    expect(row).to_have_count(1)
    expect(row.locator(".task-queue-meta")).to_contain_text("reattach before sending")
    # Sending is blocked, not quietly stripped of the file the row says it is waiting for.
    expect(row.locator("button").first).to_be_disabled()

    # The practical way out: put the file back, from the row, without touching the composer.
    page.fill("#input", NEWER)
    row.locator("button", has_text="Reattach files").click()
    page.locator("#retainFileInput").set_input_files(
        {"name": "shot.png", "mimeType": "image/png", "buffer": PNG})
    expect(retained_rows(page).locator(".task-queue-meta")).to_contain_text("Attachments saved")
    expect(page.locator("#input")).to_have_value(NEWER)
    expect(page.locator("#attachStrip .thumb")).to_have_count(0)

    before = len(_Fixture.uploads)
    retained_rows(page).locator("button").first.click()
    expect(page.locator(".task-queue-row:not(.retained)")).to_have_count(1, timeout=5000)
    post = [p for p in _Fixture.queue_posts if p["text"] == ASK][-1]
    assert post["images"], "the resend carries the file it said it was waiting for"
    assert len(_Fixture.uploads) > before
    assert post["id"] == _Fixture.queue_posts[0]["id"], "still the same inbox request"


def test_a_reload_while_a_send_is_unanswered_keeps_it_as_unconfirmed(ui):
    """The page went away before the server said anything. What it did is unknown, so the
    words are kept and shown as unconfirmed — and nothing is resent on their behalf."""
    page = ui.page
    _Fixture.busy_delay = 4.0
    open_read_thread(page)
    send(page, ASK)
    page.wait_for_timeout(400)            # still unanswered
    page.reload(wait_until="load")
    page.wait_for_selector("#input")
    page.wait_for_timeout(800)

    row = retained_rows(page)
    expect(row).to_have_count(1)
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    expect(row.locator(".task-queue-meta")).to_contain_text("Not confirmed")
    assert not _Fixture.queue_posts, "an unanswered send is never resent by itself"
    assert not _Fixture.queue_starts


def test_a_terminal_frame_that_proves_nothing_is_not_treated_as_a_start(ui):
    """Only a `start` says the server took the submission. A malformed terminal frame, or an
    error raised before the run exists, used to be read as "it started" and discarded."""
    page = ui.page
    _Fixture.busy_delay = 0.9
    for ask in ("Malformed fixture — unreadable terminal frame",
                "Prestart error fixture — the workspace vanished"):
        open_read_thread(page)
        send(page, ask)
        page.wait_for_timeout(150)
        open_cap_thread(page)
        page.wait_for_timeout(1500)

    open_read_thread(page)
    rows = retained_rows(page)
    expect(rows).to_have_count(2)
    expect(rows.locator(".task-queue-meta").first).to_contain_text("Not confirmed")
    assert not _Fixture.queue_posts, "nothing uncertain is resent"


def test_leaving_during_the_upload_phase_keeps_a_send_that_never_left(ui):
    """Before the EventSource opens, nothing has reached the server at all. The navigation
    that cancelled the launch used to drop the words with it."""
    page = ui.page
    _Fixture.upload_delay = 1.2
    open_read_thread(page)
    attach(page)
    send(page, ASK)
    page.wait_for_timeout(150)            # still uploading
    open_cap_thread(page)
    page.wait_for_timeout(1800)

    assert not [r for r in _Fixture.stream_requests if r["q"] == ASK], "no stream was opened"
    assert retained_rows(page).count() == 0, "not in the thread the person moved to"
    open_read_thread(page)
    row = retained_rows(page)
    expect(row).to_have_count(1)
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    expect(row.locator(".task-queue-meta")).to_contain_text("Not sent")


def test_a_new_task_that_never_left_is_restorable_instead_of_dropped(ui):
    """A send made before any thread exists has no inbox to be queued into. It still has to
    exist somewhere: the row offers it back to the composer it came from."""
    page = ui.page
    _Fixture.upload_delay = 1.2
    attach(page)
    page.evaluate("document.getElementById('runIntent').value = 'review'")
    send(page, ASK)                       # no session yet: this is a new task
    page.wait_for_timeout(150)
    open_cap_thread(page)                 # cancels the launch before the stream opens
    page.wait_for_timeout(1800)

    assert not [r for r in _Fixture.stream_requests if r["q"] == ASK]
    page.locator("#newChat").click()
    page.wait_for_timeout(400)
    page.evaluate("document.getElementById('runIntent').value = 'build'")
    row = retained_rows(page)
    expect(row).to_have_count(1)
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    row.locator("button", has_text="Put in composer").click()
    expect(page.locator("#input")).to_have_value(ASK)
    expect(page.locator("#runIntent")).to_have_value("review")
    expect(retained_rows(page)).to_have_count(0)


def test_reload_during_queue_save_keeps_original_payload_and_retry_identity(ui):
    page = ui.page
    _Fixture.queue_ack.clear()
    _Fixture.queue_fail_all = True
    open_read_thread(page)
    attach(page)
    send(page, ASK)
    expect(page.locator("#taskQueueNotice")).to_contain_text("Saving request")
    assert _Fixture.queue_seen.wait(3)
    request_id = _Fixture.queue_posts[0]["id"]
    page.fill("#input", NEWER)
    page.reload(wait_until="load")
    _Fixture.queue_ack.set()
    row = retained_rows(page)
    expect(row).to_have_count(1)
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    expect(row.locator(".task-queue-meta")).to_contain_text("Attachments saved")
    expect(page.locator("#input")).to_have_value(NEWER)
    page.wait_for_timeout(200)
    assert len(_Fixture.queue_posts) == 1, "an unanswered save is not replayed on reload"
    _Fixture.queue_fail_all = False
    row.locator("button").first.click()
    expect(retained_rows(page)).to_have_count(0)
    assert _Fixture.queue_posts[-1]["id"] == request_id
    assert _Fixture.queue_posts[-1]["images"]
    expect(page.locator("#input")).to_have_value(NEWER)


def test_save_failure_after_a_newer_draft_keeps_the_original_request(ui):
    page = ui.page
    _Fixture.queue_ack.clear()
    _Fixture.queue_fail_once = True
    open_read_thread(page)
    send(page, ASK)
    expect(page.locator("#taskQueueNotice")).to_contain_text("Saving request")
    assert _Fixture.queue_seen.wait(3)
    page.fill("#input", NEWER)
    _Fixture.queue_ack.set()
    expect(retained_rows(page)).to_have_count(1)
    expect(retained_rows(page).locator(".task-queue-text")).to_have_text(ASK)
    expect(page.locator("#input")).to_have_value(NEWER)


@pytest.mark.parametrize("ask", ["Malformed fixture — unreadable response",
                                 "Prestart error fixture — workspace missing"])
def test_unconfirmed_terminal_in_the_current_thread_keeps_the_request(ui, ask):
    page = ui.page
    open_read_thread(page)
    send(page, ask)
    expect(retained_rows(page)).to_have_count(1)
    expect(retained_rows(page).locator(".task-queue-text")).to_have_text(ask)
    expect(page.locator("#send")).not_to_have_class(__import__("re").compile(r"\bstop\b"))
    assert not _Fixture.queue_posts


def test_upload_failure_keeps_a_newer_draft_and_the_original_attachment(ui):
    page = ui.page
    _Fixture.upload_delay = 0.8
    _Fixture.upload_fail_once = True
    open_read_thread(page)
    attach(page)
    send(page, ASK)
    page.wait_for_timeout(100)
    page.fill("#input", NEWER)
    expect(retained_rows(page)).to_have_count(1)
    expect(retained_rows(page).locator(".task-queue-meta")).to_contain_text("Attachments saved")
    expect(page.locator("#input")).to_have_value(NEWER)
    assert not _Fixture.stream_requests


def test_inbox_receipt_after_reload_retires_uncertainty_without_a_resend(ui):
    page = ui.page
    _Fixture.queue_ack.clear()
    open_read_thread(page)
    send(page, ASK)
    expect(page.locator("#taskQueueNotice")).to_contain_text("Saving request")
    assert _Fixture.queue_seen.wait(3)
    page.fill("#input", NEWER)
    page.reload(wait_until="load")
    expect(retained_rows(page)).to_have_count(1)
    _Fixture.queue_ack.set()
    expect(retained_rows(page)).to_have_count(0, timeout=6000)
    expect(page.locator(".task-queue-row:not(.retained)")).to_have_count(1)
    assert len(_Fixture.queue_posts) == 1
    expect(page.locator("#input")).to_have_value(NEWER)


@pytest.mark.parametrize("existing_thread", [False, True])
def test_attachment_only_submission_survives_navigation_and_reload(ui, existing_thread):
    page = ui.page
    _Fixture.upload_delay = 1.0
    if existing_thread:
        open_read_thread(page)
    attach(page)
    page.press("#input", "Enter")
    page.wait_for_timeout(100)
    open_cap_thread(page)
    page.wait_for_timeout(1200)
    if existing_thread:
        open_read_thread(page)
    else:
        page.locator("#newChat").click()
    expect(retained_rows(page)).to_have_count(1)
    page.reload(wait_until="load")
    row = retained_rows(page)
    expect(row).to_have_count(1)
    expect(row.locator(".task-queue-meta")).to_contain_text("Attachments saved")
    row.get_by_role("button", name="Put in composer").click()
    expect(retained_rows(page)).to_have_count(0)
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    expect(page.locator("#input")).to_have_value("")
    assert not _Fixture.stream_requests
    assert not _Fixture.queue_posts
