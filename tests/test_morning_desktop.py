"""The morning report on the desktop: when the wallpaper shows it, and what it offers.

`morning_desktop` answers GET /api/report/today for the wallpaper from the saved snapshot
(~/.collie/morning-report/latest.json). These drive it directly with a fixture report and a
fixed clock; the route tests go through the real server. Nothing here reaches Gmail or GitHub:
the checks the progress pass would make are replaced by ones that fail the test if they run.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from harness import morning_desktop as md
from _morning_fixture import (AZURE_LINK, DAY, DRAFT_ANA, PR_LINK, at, empty_report, report,
                              rewrite, save)


@pytest.fixture
def root(tmp_path, monkeypatch):
    from harness import desktop
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_DESKTOP_MORNING_REPORT", "on")
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(tmp_path / "desktop.json"))
    # No check may reach Google or GitHub unless a test says what it answers.
    for name in ("_google_account", "_draft_exists", "_pr_merged"):
        if hasattr(md, name):
            monkeypatch.setattr(md, name, _never)
    monkeypatch.setattr(md, "_spawn", lambda _fn: None)
    return str(state)


def _never(*_args, **_kwargs):
    raise AssertionError("this check should not have run")


def keys_of(today):
    return {item["title"]: item["key"] for item in today["items"]}


# ---------------------------------------------------------------- when it shows


def test_it_shows_on_the_reports_morning_with_the_reports_own_words(root):
    save(root, report())
    today = md.today(root, now=at(8))
    assert today["ok"] is True and today["show"] is True and today["why"] == ""
    assert today["date"] == DAY and today["until"] == at(12)
    assert today["sentence"] == "Five quick ones and you're clear."
    assert today["summary"].startswith("Collie 0.31.0 shipped overnight")
    assert (today["done"], today["total"], today["all_done"]) == (0, 5, False)
    assert today["dots_label"] == "0 of 5 done"
    assert today["by"] == {"companion": "Rowan", "words": "caught up at 6:38 AM"}
    assert [item["section"] for item in today["items"]] == ["yours"] * 3 + ["ready"] * 2
    assert all(item["state"] == "open" for item in today["items"])
    assert len(set(keys_of(today).values())) == 5


def test_it_does_not_show_after_noon_on_another_day_or_without_a_report(root):
    assert md.today(root, now=at(8))["why"] == "no_report"
    save(root, report())
    assert md.today(root, now=at(11, 59))["show"] is True
    after = md.today(root, now=at(12))
    assert (after["show"], after["why"]) == (False, "after_noon")
    assert after["recheck_s"] >= 12 * 3600 - 60          # nothing to ask until tomorrow
    tomorrow = md.today(root, now=at(8, day="2026-09-30"))
    assert (tomorrow["show"], tomorrow["why"]) == (False, "not_today")
    assert "sentence" not in after and "items" not in tomorrow


def test_either_switch_turns_it_off(root, monkeypatch, tmp_path):
    from harness import desktop
    save(root, report())
    assert desktop.morning_enabled() is True
    monkeypatch.setenv("COLLIE_DESKTOP_MORNING_REPORT", "off")
    assert md.today(root, now=at(8))["why"] == "setting_off"
    monkeypatch.setenv("COLLIE_DESKTOP_MORNING_REPORT", "on")
    (tmp_path / "desktop.json").write_text(json.dumps({"widgets": {"morning": {"on": False}}}),
                                           encoding="utf-8")
    assert desktop.morning_enabled() is False
    assert md.today(root, now=at(8))["why"] == "widget_off"
    assert desktop.load_config()["widgets"]["morning"] == {"on": False}
    # A desktop.json that cannot be read keeps the default: the scene asks no one new.
    (tmp_path / "desktop.json").write_text('{"widgets": {"morning": {"on": false},}}',
                                           encoding="utf-8")
    assert desktop.morning_enabled() is True


def test_the_setting_is_declared_on_by_default_in_the_desktop_panel():
    from harness import desktop, settings
    row = next(s for s in settings.SCHEMA if s["key"] == "DESKTOP_MORNING_REPORT")
    assert row["group"] == "Desktop" and row["type"] == "bool" and row["default"] == "on"
    assert row.get("label_zh") and row.get("hint_zh")
    page = (Path(__file__).resolve().parents[1] / "harness/webui/index.html").read_text(
        encoding="utf-8")
    devices = page.split('{ id: "devices"', 1)[1].split("}", 1)[0]
    assert '"DESKTOP_MORNING_REPORT"' in devices
    assert desktop.DEFAULT_CONFIG["widgets"]["morning"] == {"on": True}


def test_dismissing_hides_it_for_the_rest_of_that_day_only(root):
    save(root, report())
    assert md.act(root, {"action": "dismiss"}, now=at(8))["today"]["show"] is False
    assert md.today(root, now=at(9))["why"] == "dismissed"
    save(root, report("2026-09-30"))
    assert md.today(root, now=at(8, day="2026-09-30"))["show"] is True
    for body in ({"action": "fly"}, [], {"action": "dismiss", "reason": 7}):
        with pytest.raises(ValueError):
            md.act(root, body, now=at(8, day="2026-09-30"))


def test_there_is_nothing_to_dismiss_without_todays_report(root):
    with pytest.raises(ValueError):
        md.act(root, {"action": "dismiss"}, now=at(8))


# ---------------------------------------------------------------- the pills


def test_pills_group_the_drafts_then_follow_the_report_and_link_less_items_open_the_report(root):
    save(root, report())
    today = md.today(root, now=at(8))
    keys = keys_of(today)
    assert today["pills"] == [
        {"label": "Review 2 drafts", "href": DRAFT_ANA, "kind": "draft", "primary": True,
         "keys": [keys["Reply to Ana about Thursday"], keys["Reply to Ben about the invoice"]]},
        {"label": "Review Ana's pull request", "href": PR_LINK, "kind": "link", "primary": False,
         "keys": [keys["Review Ana's pull request"]]},
        {"label": "Answer Collie's question", "href": "/report", "kind": "report",
         "primary": False, "keys": [keys["Answer Collie's question"]]},
    ]


def test_a_pill_only_ever_links_to_https_or_the_report_page(root):
    rep = report()
    rep["sections"]["yours"]["items"][0]["link"] = "http://github.com/colliehq/collie/pull/31"
    rep["sections"]["yours"]["items"][2]["link"] = "javascript:alert(1)"
    for item in rep["sections"]["ready"]["items"]:
        item["draft"]["open_url"] = "https://user:secret@mail.example/#drafts"
    rewrite(root, rep)
    today = md.today(root, now=at(8))
    assert all(p["href"].startswith("https://") or p["href"] == "/report" for p in today["pills"])
    assert today["pills"][0]["href"] == "https://mail.google.com/mail/u/0/#drafts"
    assert today["pills"][1] == dict(today["pills"][1], href="/report", kind="report")
    links = {item["title"]: item["link"] for item in today["items"]}
    assert links["Review Ana's pull request"] == "" and links["Pay the Azure invoice"] == ""
    assert all(link == "" or link.startswith("https://") for link in links.values())


def test_one_draft_is_offered_as_itself_and_long_titles_are_shortened(root):
    rep = report()
    rep["sections"]["ready"]["items"] = rep["sections"]["ready"]["items"][:1]
    rep["sections"]["yours"]["items"][0]["title"] = \
        "Review the enormous refactor of the report builder"
    rewrite(root, rep)
    today = md.today(root, now=at(8))
    pills = today["pills"]
    assert pills[0]["label"] == "Review the draft" and pills[0]["href"] == DRAFT_ANA
    assert len(pills[1]["label"]) <= md.PILL_LABEL and pills[1]["label"].endswith("…")
    assert today["total"] == 4                                 # the dots count what is listed


def test_a_morning_with_nothing_to_do_offers_the_report(root):
    save(root, empty_report())
    today = md.today(root, now=at(8))
    assert today["show"] is True and today["total"] == 0 and today["all_done"] is False
    assert today["sentence"] == "Nothing needs you this morning." and today["dots_label"] == ""
    assert today["pills"] == [{"label": "Open report", "href": "/report", "kind": "report",
                               "primary": True, "keys": []}]


def test_a_chinese_report_keeps_its_language(root):
    save(root, report(language="zh"))
    today = md.today(root, now=at(8))
    assert today["pills"][0]["label"] == "看看 2 封草稿"
    assert today["dots_label"] == "完成 0 / 5" and today["by"]["words"] == "06:38 整理好"


# ---------------------------------------------------------------- the report page


def test_the_report_page_is_the_email_with_links_that_leave_the_collie_window(root):
    save(root, report())
    status, html = md.page(root)
    assert status == 200
    assert "Five quick ones and you&#x27;re clear." in html
    assert "<script" not in html.lower() and "cid:" not in html
    assert "data:image/png;base64," in html                    # the dog, without a request
    links = [chunk.split(">", 1)[0] for chunk in html.split("<a ")[1:]]
    assert links and all('target="_blank"' in link and 'rel="noopener noreferrer"' in link
                         and 'href="https://' in link for link in links)
    assert md.page(root, DAY)[0] == 200 and md.page(root, "2026-09-01")[0] == 404
    missing = md.page(str(Path(root) / "nowhere"))
    assert missing[0] == 404 and "collie report build" in missing[1]


# ---------------------------------------------------------------- the routes


@pytest.fixture
def web(root, monkeypatch):
    from harness import webapp
    monkeypatch.setattr(md, "_now", lambda: at(8))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def call(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=8) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_the_routes_need_the_token(web, root):
    base, token = web
    save(root, report())
    assert call(base + "/api/report/today")[0] == 403
    assert call(base + "/api/report/today?token=wrong")[0] == 403
    assert call(base + "/api/report/today", "POST", {"action": "dismiss"})[0] == 403
    assert call(base + "/report")[0] == 403
    assert md.today(root, now=at(8))["show"] is True             # the refused POST did nothing
    code, _headers, body = call(base + "/api/report/today?token=" + token)
    today = json.loads(body)
    assert code == 200 and today["show"] is True and len(today["pills"]) == 3
    code, headers, body = call(base + "/report?token=" + token)
    assert code == 200 and headers["Content-Type"].startswith("text/html")
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert b"Five quick ones" in body
    assert call(base + "/api/report/today?token=" + token, "POST", {"action": "fly"})[0] == 400
    code, _headers, body = call(base + "/api/report/today?token=" + token, "POST",
                                {"action": "dismiss"})
    assert code == 200 and json.loads(body)["today"]["why"] == "dismissed"
