"""The morning report as an email: one table layout, inline styles, and nothing that can run.

The renderer takes a saved report and returns the subject, the HTML, the plain text and the
inline avatar.  These tests pin what must never change: every string is escaped, only https
links become links, empty sections are not drawn, capped sections say how many more there are,
the dog is an inline (CID) image a mail client shows without fetching anything, and the header
wears the morning's weather.
"""
import base64
import copy
import datetime as dt
import re

import pytest

from harness import morning_report_email as mail

NOW = dt.datetime(2026, 9, 29, 14, 2, tzinfo=dt.timezone.utc).timestamp()   # 07:02 PDT, Tuesday
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def tiny_avatar(monkeypatch):
    from harness import avatar
    monkeypatch.setattr(avatar, "png", lambda name, size=512, source="", plate=True: PNG)


def item(title, detail="", **kw):
    return dict({"title": title, "detail": detail, "signal_ids": ["s-1"], "link": "",
                 "sources": ["github"], "when": None}, **kw)


def report(**over):
    base = {
        "schema": "collie.morning_report/1", "date": "2026-09-29", "generated_at": NOW,
        "language": "en", "timezone": "America/Los_Angeles", "utc_offset_minutes": -420,
        "profile": {"name": "Daming", "companion": "Rowan"},
        "weather": {"state": "ok", "temp_c": 11.4, "code": 0, "is_day": 1, "stale": False},
        "greeting": "Good morning, Daming!", "headline": "Four quick ones and you're clear.",
        "summary": "Collie 0.31.0 shipped overnight.", "things_today": 4,
        "sections": {
            "wins": {"items": [item("Collie 0.31.0 is out.", "3 downloads already.",
                                    link="https://github.com/colliehq/collie/releases/tag/v0.31.0")],
                     "more": 0},
            "yours": {"items": [item("Pay the Azure bill", "Keeps your subscription running.",
                                     sources=["gmail"], when=NOW - 2 * 3600 + 360)], "more": 2},
            "ready": {"items": [dict(item("MyNextRide · take your phone number off the listing"),
                                     sources=["gmail"],
                                     draft={"state": "created", "draft_id": "d1",
                                            "open_url": "https://mail.google.com/mail/#drafts?compose=d1",
                                            "to": "support@mynextride.example", "subject": "Re: x",
                                            "body": "Hi"})], "more": 0},
            "projects": {"items": [{"project": "AgentGalaxy", "line": "29 changes waiting",
                                    "signal_ids": ["s-2"], "link": "", "sources": ["local"],
                                    "when": None}], "more": 0},
            "reads": {"items": [{"title": "OpenAI pauses its biggest training runs",
                                 "why_you_care": "It's the ground your papers stand on.",
                                 "signal_ids": ["s-3"], "link": "https://example.com/a",
                                 "sources": ["news"], "when": None, "publisher": "wired.com"}],
                      "more": 0}},
        "signals": [], "counters": {},
        "provenance": {"read_at": NOW, "composer": {"mode": "model"},
                       "sources": [{"name": "github", "label": "GitHub", "state": "ok", "reason": "",
                                    "stats": {"repos": 20}},
                                   {"name": "local", "label": "Projects on this computer",
                                    "state": "ok", "reason": "", "stats": {"repos": 53}},
                                   {"name": "gmail", "label": "Gmail", "state": "unavailable",
                                    "reason": "Google isn't connected yet", "stats": {}}],
                       "drafts": {}},
    }
    base.update(over)
    return base


def test_the_email_has_the_design_sections_in_order_and_a_subject():
    out = mail.render(report())
    html = out["html"]
    order = [html.index(label) for label in ("Good morning, Daming!", "While you slept",
                                             "Quick ones for you", "Ready to go", "Your projects",
                                             "Good reads with your coffee")]
    assert order == sorted(order)
    assert "Tuesday · 29 September · 11° clear" in html
    assert "4 things today" in html
    assert out["subject"].startswith("Four quick ones and you're clear.")
    assert "Have a great Tuesday!" in html and "Rowan, your Collie" in html


def test_every_string_is_escaped():
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(1)>'
    sections = copy.deepcopy(report()["sections"])
    sections["yours"]["items"][0].update(title=evil, detail=evil)
    sections["reads"]["items"][0].update(title=evil, why_you_care=evil, publisher=evil)
    sections["projects"]["items"][0].update(project=evil, line=evil)
    html = mail.render(report(sections=sections, greeting=evil, summary=evil,
                              profile={"name": evil, "companion": evil}))["html"]
    assert "<script" not in html.lower() and "onerror=alert" not in html.replace("onerror=alert(1)&gt;", "")
    assert "&lt;script&gt;" in html
    assert not re.search(r"<img[^>]*src=x", html)


def test_only_https_links_become_links():
    sections = copy.deepcopy(report()["sections"])
    sections["wins"]["items"][0]["link"] = "javascript:alert(1)"
    sections["reads"]["items"][0]["link"] = "http://example.com/plain"
    sections["ready"]["items"][0]["draft"]["open_url"] = "data:text/html,hi"
    out = mail.render(report(sections=sections))
    hrefs = re.findall(r'href="([^"]*)"', out["html"])
    assert all(href.startswith("https://") for href in hrefs)
    assert "javascript:" not in out["html"] and "http://example.com" not in out["html"]
    assert "OpenAI pauses its biggest training runs" in out["html"]        # still shown, unlinked
    assert "Review &amp; send" not in out["html"]
    assert "http://" not in out["text"] and "javascript:" not in out["text"]


def test_a_created_draft_gets_a_review_button_and_links_are_kept():
    html = mail.render(report())["html"]
    assert 'href="https://mail.google.com/mail/#drafts?compose=d1"' in html
    assert "Review &amp; send" in html
    assert 'href="https://github.com/colliehq/collie/releases/tag/v0.31.0"' in html


def test_empty_sections_are_not_drawn_and_capped_ones_say_how_many_more():
    sections = copy.deepcopy(report()["sections"])
    sections["wins"] = {"items": [], "more": 0}
    sections["reads"] = {"items": [], "more": 0}
    html = mail.render(report(sections=sections))["html"]
    assert "While you slept" not in html and "Good reads with your coffee" not in html
    assert "Quick ones for you" in html and "+ 2 more" in html


def test_a_morning_with_nothing_to_do_has_no_dots():
    empty = {name: {"items": [], "more": 0} for name in ("wins", "yours", "ready", "projects", "reads")}
    out = mail.render(report(sections=empty, things_today=0, headline="Nothing needs you this morning."))
    assert "things today" not in out["html"] and "All clear today" in out["html"]
    assert "While you slept" not in out["text"]


def test_the_avatar_is_an_inline_cid_image_by_default():
    out = mail.render(report())
    assert 'src="cid:%s"' % mail.AVATAR_CID in out["html"]
    part = out["inline"][0]
    assert part["cid"] == mail.AVATAR_CID and part["content_type"] == "image/png"
    assert part["data"] == PNG and part["filename"].endswith(".png")
    assert "data:image" not in out["html"]


def test_a_browser_preview_embeds_the_avatar_instead():
    out = mail.render(report(), avatar="data")
    assert "data:image/png;base64," in out["html"] and out["inline"] == []
    assert "cid:" not in out["html"]


def test_nothing_in_the_email_can_run_or_fetch():
    html = mail.render(report())["html"].lower()
    for banned in ("<script", "<link", "@import", "fonts.googleapis", "<style", "javascript:",
                   "url(http", "<iframe", "<form"):
        assert banned not in html, banned
    assert html.count("<table") >= 2 and 'role="presentation"' in html


@pytest.mark.parametrize("weather, colour, text", [
    ({"state": "ok", "code": 0, "is_day": 1, "temp_c": 11}, "#58aaff", "#14213d"),
    ({"state": "ok", "code": 63, "is_day": 1, "temp_c": 12}, "#3d4f66", "#f4f7ff"),
    ({"state": "ok", "code": 0, "is_day": 0, "temp_c": 9}, "#070b1f", "#f4f7ff"),
    ({"state": "ok", "code": 75, "is_day": 1, "temp_c": -2}, "#b9cbe0", "#14213d"),
    ({"state": "off"}, "#58aaff", "#14213d"),
])
def test_the_header_wears_the_mornings_weather(weather, colour, text):
    html = mail.render(report(weather=weather))["html"]
    header = html[html.index("linear-gradient"):html.index("Good morning")]
    assert colour in header and text in header
    if weather.get("state") != "ok":
        assert "°" not in html.split("Good morning")[0]


def test_the_provenance_line_says_what_was_read_and_what_was_not():
    out = mail.render(report())
    assert "Read GitHub and 53 local repos at 7:02 AM." in out["html"]
    assert "Not read: Gmail (Google isn&#x27;t connected yet)." in out["html"]
    assert "Not read: Gmail (Google isn't connected yet)." in out["text"]


def test_items_show_where_they_came_from_and_when():
    html = mail.render(report())["html"]
    assert "Gmail · 5:08 AM" in html


def test_the_plain_text_carries_the_same_report():
    text = mail.render(report())["text"]
    for words in ("Good morning, Daming!", "WHILE YOU SLEPT", "- Collie 0.31.0 is out.",
                  "QUICK ONES FOR YOU", "READY TO GO", "Review & send: https://mail.google.com/",
                  "YOUR PROJECTS", "AgentGalaxy: 29 changes waiting", "You'll care:",
                  "https://example.com/a", "Rowan, your Collie"):
        assert words in text, words
    assert "<b>" not in text and "<td" not in text


def test_chinese_reports_get_chinese_labels():
    out = mail.render(report(language="zh", greeting="早上好，Daming！"))
    html = out["html"]
    for words in ("你睡着的时候", "几件小事等你", "都准备好了", "你的项目", "配咖啡读一读",
                  "今天 4 件事", "你会在意：", "9月29日 星期二"):
        assert words in html, words
    assert "While you slept" not in html


def test_the_dots_and_the_label_count_what_is_listed_out_of_the_total():
    html = mail.render(report(things_today=3, things_total=5))["html"]
    header = html[:html.index("While you slept")]
    assert "3 of 5 things today" in header
    assert header.count("border-radius:50%;background:rgba(20,33,61,.16)") == 3
    assert "3 of 5 things today" in mail.render(report(things_today=3, things_total=5))["text"]
    zh = mail.render(report(language="zh", things_today=3, things_total=5))["html"]
    assert "今天 3 件事（共 5 件）" in zh


def test_when_everything_is_listed_the_label_is_just_the_count():
    html = mail.render(report(things_today=4, things_total=4))["html"]
    assert "4 things today" in html and " of 4 " not in html
