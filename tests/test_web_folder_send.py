"""Send checks the folder it was aimed at, then starts one request — there.

Send used to be refused here and to ask for a *Use folder* click first.  That click performs a
read-only ``/api/verification`` look at the local disk, not an authorization, so Send performs it
as part of the send already pressed and waits while it is open.  Everything asking about the same
unchanged folder — a second Send, the optional button — joins that one look; any change to what
was submitted (words, attachments, manual context, thread, or the folder the draft is aimed at)
ends the wait instead, leaving the newer draft whole.  A submission is its content, not its shape
(one file swapped for another is another message), and its destination is the *choice*, not the
text: confirming a second folder mid-look, or leaving one and returning, is a change, while the
server resolving the path that was asked about is not.  Asserted from what the fixture server (the
one `test_web_draft_folder` drives, real directories behind it) was actually asked: the count and
`cwd`/`q`/`session` of every `/api/stream`, the bodies of every `/api/upload`, never the page.
"""
import base64
import os
import struct
import zlib

import pytest
from playwright.sync_api import expect

from test_web_draft_folder import (           # noqa: F401  (pytest fixtures by name)
    DRAFT, ROOTS, _Fixture, _FolderFixture, browser, folder_server, ui)

EDITED = DRAFT + " and update the README while you are there"


def _png(red, green, blue):
    """A real 1x1 PNG of one colour — two of these are the same size and different bytes."""
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes([0, red, green, blue]))) + chunk(b"IEND", b""))


FIRST_PNG, SECOND_PNG = _png(255, 0, 0), _png(0, 0, 255)


def streams():
    return _FolderFixture.stream_queries


def uploaded():   # the base64 payload of every attachment the page handed to the server
    return [row.get("data") for row in _Fixture.uploads]


def attach(page, name, data):
    before = page.locator("#attachStrip .thumb").count()
    page.locator("#fileInput").set_input_files(
        {"name": name, "mimeType": "image/png", "buffer": data})
    expect(page.locator("#attachStrip .thumb")).to_have_count(before + 1)


def swap_attachment(page, name, data):   # one file out, another in: same count, other bytes
    page.locator("#attachStrip .thumb .rm").first.click()
    expect(page.locator("#attachStrip .thumb")).to_have_count(0)
    attach(page, name, data)


def checks_for(path):
    return [row for row in _FolderFixture.verification_requests if row["cwd"] == path]


def settle(page, ms=700):
    page.wait_for_timeout(ms)


def sent(page, timeout=8000):   # the composer empties only when a request actually left
    expect(page.locator("#input")).to_have_value("", timeout=timeout)


def send_during_check(ui, folder, delay=1.5, then=300, files=()):
    """One Send on DRAFT (plus `files`) left waiting on a `delay`-second look at `folder`."""
    _FolderFixture.verification_delay = delay
    ui.select_folder(folder, confirm=False)
    for name, data in files:
        attach(ui.page, name, data)
    ui.send(DRAFT)
    settle(ui.page, then)


@pytest.mark.parametrize("suffix", ["", os.sep + "."])
def test_one_send_on_a_typed_folder_starts_exactly_one_request_there(ui, suffix):
    """With `suffix` the server answers under the resolved name: its own answer, not a move."""
    page = ui.page
    ui.select_folder(ROOTS["b"] + suffix, confirm=False)   # typed, never confirmed by hand
    assert ui.unchecked()
    ui.send(DRAFT)
    sent(page)
    settle(page)
    assert len(streams()) == 1, "one Send started %d requests" % len(streams())
    assert streams()[0].get("cwd") == ROOTS["b"], "the request did not run where it was aimed"
    assert streams()[0].get("q") == DRAFT
    assert checks_for(ROOTS["b"] + suffix), "the folder was never actually checked"
    assert ui.field() == ROOTS["b"]
    assert not ui.unchecked()


def test_use_folder_stays_an_optional_way_to_check_first(ui):
    """A Send after the explicit control starts one request and asks no second question."""
    page = ui.page
    ui.select_folder(ROOTS["b"])                     # Use folder, by hand
    expect(page.locator("#taskWorkspacePending")).to_be_hidden(timeout=6000)
    ui.send(DRAFT)
    sent(page)
    settle(page)
    assert len(streams()) == 1
    assert streams()[0].get("cwd") == ROOTS["b"]
    assert len(checks_for(ROOTS["b"])) == 1, "Send asked the question the click had answered"


def test_a_slow_check_holds_the_send_visibly_and_then_starts_it(ui):
    page = ui.page
    send_during_check(ui, ROOTS["b"], then=400)
    assert streams() == [], "started before the folder was known to exist"
    assert ui.unchecked(), "the wait was invisible on the collapsed summary"
    assert "checking" in page.inner_text("#taskWorkspacePending").lower()
    assert page.locator("dialog[open]").count() == 0, "a modal was put in front of the composer"
    assert not page.is_disabled("#input"), "the composer was taken away during the check"
    expect(page.locator("#input")).to_have_value(DRAFT)
    sent(page)                                        # released by the answer, not by a click
    settle(page)
    assert len(streams()) == 1
    assert streams()[0].get("cwd") == ROOTS["b"]


def test_a_second_send_while_the_check_is_open_does_not_duplicate_the_request(ui):
    """Enter, Enter, and the button twice over, all before one answer comes back."""
    page = ui.page
    _FolderFixture.verification_delay = 1.5
    ui.select_folder(ROOTS["b"], confirm=False)
    page.fill("#input", DRAFT)
    for _ in range(2):
        page.press("#input", "Enter")
        page.click("#send")
        settle(page, 120)
    sent(page)
    settle(page, 900)
    assert len(streams()) == 1, "four Sends became %d requests" % len(streams())
    assert streams()[0].get("cwd") == ROOTS["b"]
    assert len(checks_for(ROOTS["b"])) == 1, "each Send asked the same question again"


def test_use_folder_on_the_same_folder_mid_send_joins_that_check_and_does_not_swallow_it(ui):
    """Reproduced independently: the button pressed on the unchanged folder, draft untouched, asks
    the question already open — it joins that look rather than replacing it with one of its own,
    which would settle the Send's look as stale and swallow the Send."""
    page = ui.page
    send_during_check(ui, ROOTS["b"], then=300)
    assert checks_for(ROOTS["b"]), "the Send never asked about the folder it was aimed at"
    page.click("#taskFolderUse")                      # same field, same draft: a repeated check
    sent(page)                                        # released by the shared answer
    settle(page, 900)
    assert len(streams()) == 1, "the repeated check swallowed the pending Send"
    assert streams()[0].get("cwd") == ROOTS["b"]
    assert streams()[0].get("q") == DRAFT
    assert len(checks_for(ROOTS["b"])) == 1, "the unchanged folder was asked about twice"


def test_a_send_pressed_during_the_buttons_check_joins_it_and_starts_once(ui):
    """The other order: the button asks first and Send is pressed while that look is open."""
    page = ui.page
    _FolderFixture.verification_delay = 1.5
    ui.select_folder(ROOTS["b"])                      # Use folder, answered slowly
    settle(page, 300)
    ui.send(DRAFT)
    assert streams() == [], "started before the folder was known to exist"
    sent(page)
    settle(page, 900)
    assert len(streams()) == 1, "one Send started %d requests" % len(streams())
    assert streams()[0].get("cwd") == ROOTS["b"]
    assert streams()[0].get("q") == DRAFT
    assert len(checks_for(ROOTS["b"])) == 1, "Send asked the open look's question again"


def test_a_failed_check_keeps_the_draft_and_the_folder_and_never_falls_back(ui):
    page = ui.page
    absent = os.path.join(ROOTS["b"], "never-created")
    ui.select_folder(absent, confirm=False)
    ui.send(DRAFT)
    settle(page, 1200)
    assert streams() == [], "a task was started although the folder is not there"
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert ui.field() == absent, "the chosen folder was replaced"
    assert ui.folder() == absent, "the summary moved the task to another project"
    expect(page.locator("#taskWorkspaceStatus")).to_contain_text("does not exist")
    assert page.get_attribute("#taskWorkspace", "open") is not None, "the reason was hidden"
    assert page.locator("dialog[open]").count() == 0

    os.makedirs(absent)                               # the same draft, one Send, now it exists
    ui.send()
    sent(page)
    settle(page)
    assert len(streams()) == 1
    assert streams()[0].get("cwd") == absent
    assert streams()[0].get("q") == DRAFT


def test_an_answer_for_a_folder_the_person_moved_off_starts_nothing(ui):
    page = ui.page
    send_during_check(ui, ROOTS["a"])
    ui.select_folder(ROOTS["b"], confirm=False)       # a newer choice, mid-look
    settle(page, 2000)
    assert streams() == [], "the stale answer started the task in the folder left behind"
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert ui.field() == ROOTS["b"]
    assert ui.unchecked(), "the newer folder was presented as checked"
    _FolderFixture.verification_delay = 0
    ui.send()
    sent(page)
    settle(page)
    assert len(streams()) == 1
    assert streams()[0].get("cwd") == ROOTS["b"], "the run took the abandoned folder"


def test_confirming_another_folder_mid_check_does_not_send_the_older_draft(ui):
    """Reproduced independently: a Send waits on a slow look at A, the person picks B and confirms
    it by hand, answered first. A's answer is about a folder nobody aims at now, and B's
    confirmation is nobody's instruction to start this draft there."""
    page = ui.page
    send_during_check(ui, ROOTS["a"], delay=2.0, then=350)
    assert checks_for(ROOTS["a"]), "the Send never asked about the folder it was aimed at"
    _FolderFixture.verification_delay = 0
    ui.select_folder(ROOTS["b"], confirm=True)        # Use folder on B, answered while A is open
    expect(page.locator("#taskWorkspacePending")).to_be_hidden(timeout=6000)
    assert ui.folder() == ROOTS["b"]
    settle(page, 2400)                                # A's answer lands in this changed world
    assert streams() == [], "the older Send ran in the folder that was confirmed after it"
    expect(page.locator("#input")).to_have_value(DRAFT), "the draft was cleared by a send nobody made"
    ui.send()                                         # one explicit Send, where it now points
    sent(page)
    settle(page)
    assert len(streams()) == 1, "the re-Send started %d requests" % len(streams())
    assert streams()[0].get("cwd") == ROOTS["b"]
    assert streams()[0].get("q") == DRAFT


def test_a_send_after_changing_folders_supersedes_the_open_wait_and_runs_once(ui):
    """Not a duplicate of the waiting Send: the old wait goes, this one runs where it points."""
    page = ui.page
    send_during_check(ui, ROOTS["a"], delay=1.2, then=250)
    ui.select_folder(ROOTS["b"], confirm=False)
    ui.send()                                         # while the look at A is still open
    sent(page)
    settle(page, 1600)
    assert len(streams()) == 1, "the change turned one message into %d requests" % len(streams())
    assert streams()[0].get("cwd") == ROOTS["b"], "the superseded wait chose the folder"
    assert streams()[0].get("q") == DRAFT


def test_leaving_a_folder_and_returning_to_it_still_ends_the_older_wait(ui):
    """Same path when the answer lands, but chosen again after that Send: the wait is over."""
    page = ui.page
    send_during_check(ui, ROOTS["a"], then=250)
    ui.select_folder(ROOTS["b"], confirm=False)
    ui.select_folder(ROOTS["a"], confirm=False)       # back to the same path, a new choice
    settle(page, 2200)
    assert streams() == [], "an answer from before the re-aim released the send"
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert ui.field() == ROOTS["a"]
    _FolderFixture.verification_delay = 0
    ui.send()
    sent(page)
    settle(page)
    assert len(streams()) == 1
    assert streams()[0].get("cwd") == ROOTS["a"]
    assert streams()[0].get("q") == DRAFT


def test_words_typed_while_the_check_is_open_are_never_sent_or_cleared(ui):
    page = ui.page
    send_during_check(ui, ROOTS["b"])
    page.fill("#input", EDITED)                       # the submission is no longer that one
    settle(page, 2000)
    assert streams() == [], "the words as they were when Send was pressed went out anyway"
    expect(page.locator("#input")).to_have_value(EDITED), "a newer draft was cleared"
    assert not ui.unchecked(), "the finished check was thrown away with the send"
    ui.send()                                         # the check it produced is still good
    sent(page)
    settle(page)
    assert len(streams()) == 1
    assert streams()[0].get("cwd") == ROOTS["b"]
    assert streams()[0].get("q") == EDITED
    assert len(checks_for(ROOTS["b"])) == 1


def test_swapping_one_attachment_for_another_mid_check_cancels_that_send(ui):
    """The gap a count comparison leaves open: one file out, one file in, same shape."""
    page = ui.page
    send_during_check(ui, ROOTS["b"], files=[("first.png", FIRST_PNG)])
    swap_attachment(page, "second.png", SECOND_PNG)   # same count, other bytes
    settle(page, 2200)                                # the answer lands in a changed composer
    assert streams() == [], "the stale answer started the send with the file it replaced"
    assert uploaded() == [], "the replaced attachment was uploaded for a send nobody made"
    expect(page.locator("#input")).to_have_value(DRAFT), "a newer draft was cleared"
    expect(page.locator("#attachStrip .thumb")).to_have_count(1), "the new attachment was dropped"
    ui.send()                                         # one explicit Send, the message as it is now
    sent(page)
    settle(page)
    assert len(streams()) == 1, "one Send started %d requests" % len(streams())
    assert streams()[0].get("cwd") == ROOTS["b"]
    assert streams()[0].get("q") == DRAFT
    assert uploaded() == [base64.b64encode(SECOND_PNG).decode()], \
        "the request did not carry exactly the attachment the composer was holding"
    assert len(checks_for(ROOTS["b"])) == 1, "the same folder was asked about twice"


def test_a_send_after_the_swap_supersedes_the_open_wait_and_runs_once(ui):
    """Not a duplicate once the message changed: one submission runs, with the new file."""
    page = ui.page
    send_during_check(ui, ROOTS["b"], then=250, files=[("first.png", FIRST_PNG)])
    swap_attachment(page, "second.png", SECOND_PNG)
    ui.send()                                         # while the first look is still open
    assert not page.is_disabled("#input"), "the composer was taken away during the check"
    sent(page)
    settle(page, 900)
    assert len(streams()) == 1, "the swap turned one message into %d requests" % len(streams())
    assert streams()[0].get("cwd") == ROOTS["b"]
    assert streams()[0].get("q") == DRAFT
    assert uploaded() == [base64.b64encode(SECOND_PNG).decode()], \
        "the superseded send went out with the attachment it was made under"
    assert len(checks_for(ROOTS["b"])) == 1, "superseding the wait asked the folder question again"


def test_removing_the_only_attachment_mid_check_is_also_a_change(ui):
    page = ui.page
    send_during_check(ui, ROOTS["b"], files=[("first.png", FIRST_PNG)])
    page.locator("#attachStrip .thumb .rm").first.click()
    expect(page.locator("#attachStrip .thumb")).to_have_count(0)
    settle(page, 2200)
    assert streams() == [], "the answer started a send whose attachment had been taken away"
    assert uploaded() == []
    expect(page.locator("#input")).to_have_value(DRAFT)
    ui.send()
    sent(page)
    settle(page)
    assert len(streams()) == 1
    assert streams()[0].get("q") == DRAFT
    assert uploaded() == [], "a removed attachment was still uploaded"


def test_a_thread_opened_while_the_check_is_open_is_not_where_the_draft_lands(ui):
    """Switching context mid-look: the pending send is dropped, the thread keeps its own accepted
    workspace, and the draft is still waiting where it was left."""
    page = ui.page
    send_during_check(ui, ROOTS["b"], then=250)
    ui.open_thread("Read README.md", "s-read")
    settle(page, 2200)
    assert streams() == [], "the pending send followed the person into a thread"
    expect(page.locator("#input")).to_have_value("")
    assert ui.folder() == ROOTS["a"], "the draft's folder was pushed onto the conversation"
    ui.send("Continue from where you stopped")        # a follow-up is unchanged by any of this
    sent(page)
    settle(page, 900)
    assert len(streams()) == 1
    assert streams()[0].get("cwd") == ROOTS["a"]
    assert streams()[0].get("session") == "s-read"

    ui.new_task()
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert ui.field() == ROOTS["b"], "the draft lost the folder it was written for"


def test_a_busy_thread_still_queues_a_follow_up_rather_than_starting_a_run(ui):
    """A running worker owns the composer's Enter: no `/api/stream` for the follow-up."""
    page = ui.page
    ui.select_folder(ROOTS["b"], confirm=False)
    ui.send("Hold queue fixture")                     # the fixture keeps this run open
    sent(page)
    page.wait_for_function("() => document.getElementById('send').classList.contains('stop') && "
                           "!document.getElementById('input').disabled", timeout=8000)
    assert len(streams()) == 1
    page.fill("#input", "One more thing once you are free")
    page.press("#input", "Enter")
    settle(page, 900)
    assert len(streams()) == 1, "a follow-up opened a second run"
    assert _FolderFixture.queue_posts, "the follow-up was not queued"


def test_a_slash_command_is_not_a_task_and_is_not_held_by_the_check(ui):
    page = ui.page
    ui.select_folder(ROOTS["b"], confirm=False)
    ui.send("/model")
    settle(page, 500)
    assert streams() == [], "a picker command was sent as a task"
    expect(page.locator("#input")).to_have_value("")
    assert ui.field() == ROOTS["b"], "the command disturbed the draft's folder"
