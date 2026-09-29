"""The morning-report controls on the real /brief page, in headless Chromium, on the real server.

The page is the real page and the routes are the real routes; the mail account is a fake
adapter and "Send me one now" is staged (``daily_brief_schedule.send_now``), so no report
is built and nothing leaves the machine.
"""
import json
import threading
import urllib.request

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

OWNER = "owner@example.test"


class Adapter:
    def validate_config(self, config):
        return dict(config)


@pytest.fixture
def server(tmp_path, monkeypatch):
    from harness import daily_brief_news, settings, webapp
    from harness.channel_service import ChannelService
    from harness.httpserver import ThreadingHTTPServer
    root = tmp_path / "state"
    monkeypatch.setenv("COLLIE_STATE_DIR", str(root))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setattr(settings, "_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(daily_brief_news, "start_refresh", lambda root, **_: True)
    monkeypatch.setattr(ChannelService, "_adapter", staticmethod(lambda kind: Adapter()))
    ChannelService(str(root)).configure("mail", kind="imap",
                                        config={"address": "collie@example.test"}, owner=OWNER,
                                        credentials={"password": "private-password"},
                                        workspace=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1], webapp
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def call(server, path, body):
    base, webapp = server
    req = urllib.request.Request(base + path + "?token=" + webapp.TOKEN,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read())


@pytest.fixture
def page(server):
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(locale="en-US", viewport={"width": 1000, "height": 1400})
        page = context.new_page()
        errors, dialogs = [], []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
        page.add_init_script("try{localStorage.setItem('collie-brief-lang','en')}catch(e){}")
        page.dialogs = dialogs
        yield page
        assert not errors
        context.close()
        browser.close()


def test_the_report_is_chosen_saved_and_sent_on_request(page, server, monkeypatch):
    from harness import daily_brief_schedule
    sent = []

    def staged(root, **_kwargs):
        sent.append(root)
        return {"state": "submitted", "sent": True, "detail": "", "delivery_known": False}

    monkeypatch.setattr(daily_brief_schedule, "send_now", staged)
    page.goto(server[0] + "/brief")
    # Nothing is saved yet: there is nowhere to send one.
    expect(page.locator("#schedNow")).to_be_disabled()
    expect(page.locator("#schedNowNote")).to_contain_text("Save an email account above first")

    page.locator("#schedOptions > summary").click()
    page.select_option("#schedConn", "mail")
    expect(page.locator("#schedDraftsRow")).to_be_hidden()          # only the report writes drafts
    page.select_option("#schedContent", "morning_report")
    expect(page.locator("#schedDraftsRow")).to_be_visible()
    page.uncheck("#schedDrafts")
    page.click("#schedSave")
    expect(page.locator("#schedNotice")).to_contain_text("Saved and on")
    facts = page.locator("#schedFacts")
    expect(facts).to_contain_text("The morning report, as a designed email")
    expect(facts).to_contain_text("o***@example.test")
    saved = call(server, "/api/brief/preferences", {"enabled": True, "connection": "mail",
                                                    "timezone": "UTC"})
    assert saved["report"] is True and saved["gmail_drafts"] is False

    expect(page.locator("#schedNow")).to_be_enabled()
    expect(page.locator("#schedNowNote")).to_contain_text("o***@example.test")
    page.click("#schedNow")
    expect(page.locator("#schedNotice")).to_contain_text("Sent.")
    assert len(sent) == 1
    assert page.dialogs and "o***@example.test" in page.dialogs[0]
    assert OWNER not in page.content()


def test_the_controls_speak_chinese(page, server):
    call(server, "/api/brief/preferences", {"enabled": True, "connection": "mail",
                                            "timezone": "UTC", "report": True})
    page.goto(server[0] + "/brief")
    page.click("#lang")
    page.locator("#schedOptions > summary").click()
    expect(page.locator("label[for=schedContent]")).to_have_text("发送内容")
    expect(page.locator("#schedNow")).to_have_text("现在发我一份")
    expect(page.locator("#schedFacts")).to_contain_text("晨报（排好版的邮件）")
    expect(page.locator("#schedDraftsRow")).to_contain_text("在 Gmail 里写好回复草稿")
