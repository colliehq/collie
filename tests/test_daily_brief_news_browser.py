"""News on the real /brief page, in Chromium, against the real server.

Only the feed transport is staged (``daily_brief_news.fetch_feed``), and the lazy
background refresh is recorded instead of started, so nothing leaves the machine and no
thread outlives a test.  Everything else -- parsing, storage, the brief builder, the
route and the page -- is the real code.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from harness import daily_brief_news as news

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

WIRE = "https://feeds.example.com/rss"
BROKEN = "https://down.example.net/feed"
DOCS = {WIRE: b"""<rss><channel>
  <item><title>AI systems update</title><link>https://example.com/ai</link>
    <description>&lt;b&gt;Useful&lt;/b&gt; architecture</description>
    <pubDate>Wed, 23 Sep 2026 10:00:00 GMT</pubDate></item>
  <item><title>Sports today</title><link>https://example.com/sports</link></item>
  <item><title>&lt;img src=x onerror=alert(1)&gt; AI markup</title><link>https://example.com/x</link></item>
  <item><title>AI quoting &amp;lt;img src=y onerror=alert(2)&amp;gt; as text</title><link>https://example.com/y</link></item>
</channel></rss>"""}


def staged(url):
    if url not in DOCS:
        raise news.NewsError("The feed answered HTTP 503")
    return DOCS[url]


@pytest.fixture
def server(tmp_path, monkeypatch):
    from harness import webapp
    from harness.httpserver import ThreadingHTTPServer
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(news, "fetch_feed", staged)
    monkeypatch.setattr(news, "start_refresh", lambda root, **_: True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1], webapp
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def call(server, path, body=None):
    base, webapp = server
    req = urllib.request.Request(base + path + "?token=" + webapp.TOKEN,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read())


def saved(server):
    return call(server, "/api/brief")[1]["news"]["settings"]


@pytest.fixture
def page(server):
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(locale="en-US", viewport={"width": 1000, "height": 900})
        page = context.new_page()
        errors, dialogs = [], []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        page.add_init_script("try{localStorage.setItem('collie-brief-lang','en')}catch(e){}")
        page.goto(server[0] + "/brief")
        expect(page.locator("#stampFresh")).to_contain_text("collected")
        yield page
        assert not errors and not dialogs
        context.close()
        browser.close()


def open_settings(page):
    page.locator("#newsOptions summary").click()


def test_news_is_off_until_a_feed_is_saved_then_shows_filtered_headlines(page, server):
    expect(page.locator("#newsEmpty")).to_contain_text("Off.")
    expect(page.locator("#newsCheck")).to_be_disabled()
    open_settings(page)
    page.fill("#newsFeeds", WIRE + "\n" + BROKEN)
    page.fill("#newsTopics", "AI")
    page.click("#newsSave")
    expect(page.locator("#newsNotice")).to_contain_text("Some feeds could not be read")
    news_list = page.locator("#newsList")
    link = news_list.get_by_role("link", name="AI systems update")
    expect(link).to_have_attribute("href", "https://example.com/ai")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    expect(news_list).to_contain_text("feeds.example.com")
    expect(news_list).to_contain_text("Useful architecture")
    expect(news_list).not_to_contain_text("Sports today")                  # not a topic
    # Markup in a title is dropped when the feed is read; markup a title spells out as
    # text stays text on this page. Neither ever becomes an element.
    expect(news_list.get_by_role("link", name="AI markup", exact=True)).to_have_count(1)
    expect(news_list).to_contain_text("AI quoting <img src=y onerror=alert(2)> as text")
    assert news_list.locator("img").count() == 0
    status = page.locator("#feedStatus")
    expect(status).to_contain_text("down.example.net · not read yet · The feed answered HTTP 503")
    expect(status.locator("li.bad")).to_have_count(1)
    # Nothing about a headline reaches Needs you.
    expect(page.locator("#attentionList")).not_to_contain_text("AI systems update")
    assert saved(server)["feeds"] == [WIRE, BROKEN] and saved(server)["topics"] == ["AI"]


def test_a_refused_feed_address_is_explained_and_nothing_is_saved(page, server):
    open_settings(page)
    page.fill("#newsFeeds", "http://feeds.example.com/rss")
    page.click("#newsSave")
    expect(page.locator("#newsNotice")).to_contain_text("https://")
    expect(page.locator("#newsFeeds")).to_have_value("http://feeds.example.com/rss")
    assert saved(server)["feeds"] == []


def test_a_half_typed_edit_survives_a_refresh(page, server):
    open_settings(page)
    page.fill("#newsTopics", "design, 人工智能")
    page.select_option("#newsEvery", "360")
    page.click("#refresh")
    expect(page.locator("#stampFresh")).to_contain_text("collected")
    expect(page.locator("#newsTopics")).to_have_value("design, 人工智能")
    expect(page.locator("#newsEvery")).to_have_value("360")
    expect(page.locator("#newsRevert")).to_be_visible()
    page.click("#newsRevert")
    expect(page.locator("#newsTopics")).to_have_value("")
    expect(page.locator("#newsEvery")).to_have_value("60")


def test_a_stale_window_keeps_its_edit_and_replaces_only_on_a_second_save(page, server):
    open_settings(page)
    page.fill("#newsTopics", "Mine")
    other = saved(server)
    assert call(server, "/api/brief/news", {"action": "settings", "settings": dict(
        other, topics=["Other window"])})[0] == 200
    with page.expect_response(lambda r: "/api/brief/news?" in r.url) as answer:
        page.click("#newsSave")
    assert answer.value.status == 409
    expect(page.locator("#newsNotice")).to_contain_text("changed in another window")
    expect(page.locator("#newsTopics")).to_have_value("Mine")
    assert saved(server)["topics"] == ["Other window"]            # nothing was forced
    page.click("#newsSave")
    expect(page.locator("#newsNotice")).to_have_text("Saved.")
    assert saved(server)["topics"] == ["Mine"]


def test_check_now_reads_the_feeds_again(page, server):
    call(server, "/api/brief/news", {"action": "settings", "settings": dict(
        saved(server), feeds=[WIRE])})
    page.click("#refresh")
    expect(page.locator("#newsList")).to_contain_text("Sports today")
    DOCS[WIRE] = DOCS[WIRE].replace(b"Sports today", b"Evening sports")
    try:
        with news.NewsStore(server[1]._state_root())._connect() as db:
            db.execute("UPDATE feeds SET checked_at=0")        # a minute has passed
        open_settings(page)
        page.click("#newsCheck")
        expect(page.locator("#newsNotice")).to_have_text("Checked.")
        expect(page.locator("#newsList")).to_contain_text("Evening sports")
    finally:
        DOCS[WIRE] = DOCS[WIRE].replace(b"Evening sports", b"Sports today")


def test_the_news_panel_speaks_chinese_and_leaves_headlines_alone(page, server):
    call(server, "/api/brief/news", {"action": "settings", "settings": dict(
        saved(server), feeds=[WIRE])})
    page.click("#lang")
    expect(page.locator("#newsHead")).to_have_text("新闻")
    expect(page.locator("#newsSave")).to_have_text("保存订阅源")
    expect(page.locator("#newsList")).to_contain_text("AI systems update")
    expect(page.locator("#feedStatus")).to_contain_text("读取于")
