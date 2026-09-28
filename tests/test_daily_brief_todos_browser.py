"""The to-do list on the real /brief page, in Chromium, against the real server.

Real HTML and script, real HTTP handlers and a to-do store under ``tmp_path``.  The
only thing staged is *when* an already-accepted answer reaches the page, to show what
happens to words typed while a save is on its way.  No network, no model, no user state.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect


@pytest.fixture
def server(tmp_path, monkeypatch):
    from harness import webapp
    from harness.httpserver import ThreadingHTTPServer
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
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


def listed(server):
    return call(server, "/api/brief")[1]["todos"]["items"]


@pytest.fixture
def page(server):
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(locale="en-US", timezone_id="UTC",
                                      viewport={"width": 1000, "height": 900})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.add_init_script("try{localStorage.setItem('collie-brief-lang','en')}catch(e){}")
        page.goto(server[0] + "/brief")
        expect(page.locator("#stampFresh")).to_contain_text("collected")
        yield page
        assert not errors
        context.close()
        browser.close()


def today(page):
    return page.evaluate("""() => { const d = new Date();
      return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }""")


def row(page, title):
    return page.locator("#todoList li", has_text=title)


class DelayedAck:
    """Let the real server accept one save, and hold its answer until ``release``.

    The route stays registered and passes every later request straight through:
    removing it while the page's next request is in flight leaves that request
    paused in Chromium's interception layer, and the page never hears back.
    """

    def __init__(self, page, path):
        self.page, self.held, self.armed = page, [], True
        page.route("**" + path + "?*", self.capture)

    def capture(self, route):
        if not self.armed:
            return route.continue_()
        self.armed = False
        response = route.fetch()
        assert response.ok, response.text()
        self.held.append((route, response))

    def wait(self):
        deadline = time.monotonic() + 10
        while not self.held and time.monotonic() < deadline:
            self.page.wait_for_timeout(10)
        assert len(self.held) == 1, "the real server must have accepted the first save"

    def release(self):
        route, response = self.held.pop()
        payload = response.json()
        route.fulfill(response=response)
        expect(self.page.locator("#todoNotice")).to_have_text("Saved.")
        expect(self.page.locator("#todoSave")).to_be_enabled()
        return payload


def test_add_edit_finish_reopen_and_delete_a_todo(page, server):
    page.fill("#todoTitle", "Write the plan")
    page.fill("#todoDue", "2026-12-01")
    page.click("#todoSave")
    expect(row(page, "Write the plan")).to_contain_text("Due 2026-12-01")
    expect(page.locator("#todoTitle")).to_have_value("")

    row(page, "Write the plan").get_by_role("button", name="Edit to-do: Write the plan").click()
    expect(page.locator("#todoSave")).to_have_text("Save to-do")
    page.fill("#todoTitle", "Review the plan")
    page.click("#todoSave")
    expect(row(page, "Review the plan")).to_have_count(1)
    expect(page.locator("#todoSave")).to_have_text("Add to-do")

    page.get_by_role("checkbox", name="Finished: Review the plan").check()
    expect(row(page, "Review the plan")).to_have_count(0)      # finished ones are tucked away
    page.check("#todoShowDone")
    page.get_by_role("checkbox", name="Finished: Review the plan").uncheck()
    page.reload()
    expect(row(page, "Review the plan")).to_have_count(1)
    assert [(t["title"], t["done"]) for t in listed(server)] == [("Review the plan", False)]

    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Delete to-do: Review the plan").click()
    expect(page.locator("#todoList li")).to_have_count(0)
    expect(page.locator("#todoEmpty")).to_be_visible()
    assert listed(server) == []


def test_late_work_needs_you_and_its_row_opens_the_todo_on_this_page(page, server):
    call(server, "/api/brief/todos", {"action": "save",
                                      "todo": {"title": "Renew the lease", "due": "2020-01-01"}})
    call(server, "/api/brief/todos", {"action": "save",
                                      "todo": {"title": "Due today", "due": today(page)}})
    page.click("#refresh")
    attention = page.locator("#attentionList")
    expect(attention).to_contain_text("Renew the lease")
    expect(attention).to_contain_text("Past its due date.")
    expect(attention).to_contain_text("Due today.")
    expect(row(page, "Renew the lease")).to_contain_text("Late · due 2020-01-01")
    before = page.url
    attention.locator("li", has_text="Renew the lease").get_by_role("link", name="Open To-dos").click()
    expect(row(page, "Renew the lease")).to_have_class("todo found")
    expect(page.get_by_role("checkbox", name="Finished: Renew the lease")).to_be_focused()
    assert page.url == before                                  # no reload, nothing lost
    # Ticking it off here is what takes it out of Needs you -- and puts it under done today.
    page.get_by_role("checkbox", name="Finished: Renew the lease").check()
    expect(attention).not_to_contain_text("Renew the lease")
    expect(page.locator("#progressList")).to_contain_text("Renew the lease")


def test_a_link_to_one_todo_opens_the_page_at_it(page, server):
    saved = call(server, "/api/brief/todos", {"action": "save",
                                              "todo": {"title": "Linked to-do"}})[1]["todo"]
    page.goto(server[0] + "/brief?todo=" + saved["id"])
    expect(row(page, "Linked to-do")).to_have_class("todo found")
    page.goto(server[0] + "/brief?todo=gone")
    expect(page.locator("#todoNotice")).to_have_text("That to-do is no longer on your list.")


def test_an_edit_from_a_stale_window_is_refused_and_the_draft_is_kept(page, server):
    saved = call(server, "/api/brief/todos", {"action": "save",
                                              "todo": {"title": "Shared to-do"}})[1]["todo"]
    page.click("#refresh")
    row(page, "Shared to-do").get_by_role("button", name="Edit to-do: Shared to-do").click()
    # Another window edits it first.
    assert call(server, "/api/brief/todos", {"action": "save",
                                             "todo": dict(saved, title="Other window")})[0] == 200
    page.fill("#todoTitle", "My words")
    with page.expect_response(lambda r: "/api/brief/todos?" in r.url) as answer:
        page.click("#todoSave")
    assert answer.value.status == 409
    expect(page.locator("#todoNotice")).to_contain_text("changed elsewhere first")
    expect(page.locator("#todoTitle")).to_have_value("My words")
    # Nothing was forced: the other window's words stand, and the list shows them.
    assert [t["title"] for t in listed(server)] == ["Other window"]
    expect(row(page, "Other window")).to_have_count(1)
    # A second, deliberate save replaces it, on the same row.
    page.click("#todoSave")
    expect(row(page, "My words")).to_have_count(1)
    saved_now = listed(server)
    assert [(t["id"], t["title"]) for t in saved_now] == [(saved["id"], "My words")]


def test_a_tick_from_a_stale_window_changes_nothing_and_says_so(page, server):
    saved = call(server, "/api/brief/todos", {"action": "save",
                                              "todo": {"title": "Shared to-do"}})[1]["todo"]
    page.click("#refresh")
    expect(row(page, "Shared to-do")).to_have_count(1)
    call(server, "/api/brief/todos", {"action": "save", "todo": dict(saved, title="Renamed")})
    page.get_by_role("checkbox", name="Finished: Shared to-do").check()
    expect(page.locator("#todoNotice")).to_contain_text("changed in another window")
    expect(row(page, "Renamed")).to_have_count(1)
    assert [(t["title"], t["done"]) for t in listed(server)] == [("Renamed", False)]


@pytest.mark.parametrize("editing", [False, True], ids=["create", "edit"])
def test_words_typed_while_a_save_is_on_its_way_are_kept(page, server, editing):
    if editing:
        initial = call(server, "/api/brief/todos", {"action": "save",
                                                    "todo": {"title": "Existing"}})[1]["todo"]
        page.click("#refresh")
        row(page, "Existing").get_by_role("button", name="Edit to-do: Existing").click()
    page.fill("#todoTitle", "Submitted words")
    page.fill("#todoDue", "2026-12-01")
    gate = DelayedAck(page, "/api/brief/todos")
    page.click("#todoSave")
    gate.wait()
    page.fill("#todoTitle", "Newer words")
    page.fill("#todoDue", "2026-12-02")
    first = gate.release()["todo"]
    expect(page.locator("#todoTitle")).to_have_value("Newer words")
    expect(page.locator("#todoDue")).to_have_value("2026-12-02")
    expect(page.locator("#todoSave")).to_have_text("Save to-do" if editing else "Add to-do")
    with page.expect_response(lambda r: "/api/brief/todos?" in r.url) as answer:
        page.click("#todoSave")
    assert answer.value.status == 200, answer.value.text()
    second = answer.value.json()["todo"]
    expect(page.locator("#todoTitle")).to_have_value("")
    saved = sorted((t["title"], t["due"]) for t in listed(server))
    if editing:
        assert first["id"] == second["id"] == initial["id"]
        assert second["revision"] > first["revision"]
        assert saved == [("Newer words", "2026-12-02")]
    else:
        assert first["id"] != second["id"]
        assert saved == [("Newer words", "2026-12-02"), ("Submitted words", "2026-12-01")]


def test_a_refused_save_keeps_the_latest_draft(page):
    held = []
    page.route("**/api/brief/todos?*", lambda route: held.append(route))
    page.fill("#todoTitle", "Submitted")
    page.click("#todoSave")
    expect(page.locator("#todoSave")).to_be_disabled()
    page.fill("#todoTitle", "Typed after pressing save")
    held.pop().fulfill(status=400, json={"error": "A to-do can be at most 300 characters"})
    expect(page.locator("#todoNotice")).to_have_text("A to-do can be at most 300 characters")
    expect(page.locator("#todoTitle")).to_have_value("Typed after pressing save")


def test_the_list_speaks_chinese_with_the_rest_of_the_page(page, server):
    call(server, "/api/brief/todos", {"action": "save",
                                      "todo": {"title": "Call the bank", "due": "2020-01-01"}})
    page.click("#lang")
    expect(page.locator("#todoHead")).to_have_text("你的待办")
    expect(page.locator("#todoSave")).to_have_text("添加待办")
    expect(row(page, "Call the bank")).to_contain_text("已逾期 · 截止 2020-01-01")
    expect(page.locator("#attentionList")).to_contain_text("已过截止日期。")
    expect(page.locator("#attentionList")).to_contain_text("Call the bank")   # their words stay theirs
