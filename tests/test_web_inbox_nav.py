"""The Inbox entry in the desktop sidebar: what its badge counts and where it leads.

The real ``harness/webui/index.html`` runs in Chromium over the fixture server shared with
test_web_ui_run_status; ``/api/channels`` is answered by Playwright interception with invented
connection counts. No mailbox, phone provider or saved setting is touched.
"""
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser  # noqa: F401


def _connections(*rows):
    return {"connections": [
        {"id": cid, "kind": "imap", "auto_reply": auto,
         "counts": {"events": {"pending": pending}, "outbox": {"pending": drafts, "unknown": unknown}}}
        for cid, auto, pending, drafts, unknown in rows]}


def _load(ui, answer):
    page = ui.page
    page.route("**/api/channels*", lambda route: route.fulfill(json=answer))
    page.reload(wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    return page


def test_the_badge_counts_what_waits_on_the_person(ui):
    # mail: 2 to decide + 3 drafts to review + 1 delivery to check; sms sends its own drafts,
    # so only its 1 message waiting for a decision counts.
    page = _load(ui, _connections(("mail", False, 2, 3, 1), ("sms", True, 1, 5, 0)))
    expect(page.locator("#inboxCount")).to_have_text("7")
    expect(page.locator("#inboxCount")).to_be_visible()
    expect(page.locator("#navInbox")).to_have_attribute("aria-label", "Inbox · 7 waiting")


def test_nothing_waiting_shows_no_badge(ui):
    page = _load(ui, _connections(("mail", False, 0, 0, 0)))
    expect(page.locator("#navInbox")).to_be_visible()
    expect(page.locator("#inboxCount")).to_be_hidden()
    expect(page.locator("#navInbox")).to_have_attribute("aria-label", "Inbox")


def test_the_entry_opens_the_inbox_inside_the_shell(ui):
    page = _load(ui, _connections(("mail", False, 1, 0, 0)))
    page.locator("#navInbox").click()
    expect(page.locator("#surfacePanel")).to_be_visible()
    expect(page.locator("#surfaceFrame")).to_have_attribute("src", "/communications?embedded=1")
    expect(page.locator("#pageTitle")).to_have_text("Inbox")
    expect(page.locator("#navInbox")).to_have_class("side-nav-item on")


def test_the_embedded_inbox_opens_a_task_in_the_shell_and_ignores_bad_ids(ui):
    page = _load(ui, _connections(("mail", False, 1, 0, 0)))
    page.locator("#navInbox").click()
    expect(page.locator("#surfacePanel")).to_be_visible()
    asked = []
    page.on("request", lambda request: asked.append(request.url) if "/api/session/" in request.url else None)
    page.evaluate("() => window.postMessage({type: 'collie:open-session', session: '../../etc'}, location.origin)")
    page.wait_for_timeout(200)
    expect(page.locator("#surfacePanel")).to_be_visible()
    assert asked == []
    with page.expect_request("**/api/session/sess-from-inbox"):
        page.evaluate("() => window.postMessage({type: 'collie:open-session', session: 'sess-from-inbox'}, location.origin)")
    expect(page.locator("#surfacePanel")).to_be_hidden()
