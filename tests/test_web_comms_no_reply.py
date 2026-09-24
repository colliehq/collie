""""No reply needed" end to end: the real inbox page, the real web handler and the real store.

A mail connection with one accepted message is created through ``ChannelService`` in a temporary
state directory (the adapter is staged, so no mail server is contacted). Chromium opens the real
``/communications`` page over loopback, the person presses *No reply needed* and types a reason into
the browser prompt, and the test then reads the durable event back from disk.
"""
import threading
from http.server import ThreadingHTTPServer

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from harness import communications as comms  # noqa: E402
from harness.channel_service import ChannelService  # noqa: E402


class _Adapter:
    def validate_config(self, config):
        return dict(config)


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    from harness import webapp

    root = tmp_path / "state"
    monkeypatch.setenv("COLLIE_STATE_DIR", str(root))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setattr(ChannelService, "_adapter", staticmethod(lambda kind: _Adapter()))
    host = ChannelService(str(root))
    host.configure("mail", kind="imap", config={"address": "collie@example.test"},
                   owner="owner@example.test", credentials={"password": "p"},
                   workspace=str(tmp_path), history="all")
    host.ingest("mail", {"event_id": "req-1", "sender": "owner@example.test",
                         "recipient": "collie@example.test", "text": "Please summarise the report.",
                         "subject": "Report summary", "message_id": "<req-1@example.test>",
                         "in_reply_to": [], "references": [], "attachments": []})
    host.accept("mail", "req-1", start=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], host
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


def _open_message(page, base):
    page.goto(base + "/communications", wait_until="load")
    page.locator("#connections .connection").first.click()
    page.locator("#messages .message", has_text="Report summary").click()
    expect(page.locator("#detail h2")).to_have_text("Report summary")


def test_no_reply_needed_records_the_reason_and_stops_being_owed(inbox, chromium):
    base, host = inbox
    context = chromium.new_context(viewport={"width": 1200, "height": 850})
    errors = []
    try:
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        _open_message(page, base)
        prompts = []

        def answer(dialog):
            prompts.append(dialog.message)
            dialog.accept("Answered on the phone")

        page.once("dialog", answer)
        page.get_by_role("button", name="No reply needed").click()
        expect(page.locator("#notice")).to_contain_text("Marked as no reply needed.")
        assert prompts and "The task itself is not changed." in prompts[0]
        event = comms.get_event("mail", "req-1", directory=host.directory)
        assert event["awaiting_reply"] is False
        assert event["settlement"]["disposition"] == "closed"
        assert event["settlement"]["reason"] == "Answered on the phone"
        # The page now says so, and no longer offers either reply action.
        expect(page.locator("#detail")).to_contain_text("No reply needed · Answered on the phone")
        expect(page.get_by_role("button", name="No reply needed")).to_have_count(0)
        expect(page.get_by_role("button", name="Write reviewed reply")).to_have_count(0)
    finally:
        context.close()
    assert errors == []


def test_cancelling_the_prompt_leaves_the_message_owed(inbox, chromium):
    base, host = inbox
    context = chromium.new_context()
    try:
        page = context.new_page()
        _open_message(page, base)
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.get_by_role("button", name="No reply needed").click()
        expect(page.get_by_role("button", name="No reply needed")).to_be_enabled()
        assert comms.get_event("mail", "req-1", directory=host.directory)["awaiting_reply"] is True
    finally:
        context.close()
