"""Which surface a reload lands on, checked in a real browser against the real index.html.

Today and New task share the root address, so opening the task canvas used to be a choice the page
forgot the moment it was reloaded: the composer still held the unsent draft, but the canvas behind
it had gone back to the Today dashboard, and the only way back to the task was to press New task
again. These tests pin the four things that decide where a reload lands — an explicit New task, an
explicit Today, a first visit with nothing chosen, and a session deep link — plus the back/forward
behaviour that has to agree with them, and the same question asked of a page that is still loading:
a press made before the script arrived is later than the address, and has to be answered like any
other press.

Everything is asserted through what a person can see (the header, the canvas, the composer) and
what the page asks the server for. The fixture server, browser and staged sessions are the ones
from test_web_ui_run_status.
"""
import os
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_web_mission_updates_ui import stage as stage_mission
from test_web_ui_run_status import TOKEN, WEBUI, _Fixture, browser, server, ui  # noqa: F401

DRAFT = "Draft I typed before reloading the page"
# Only the sources nothing but the Today dashboard reads. The queue and approvals pollers keep the
# sidebar honest on every surface, so their requests say nothing about which view opened.
TODAY_SOURCES = ("/api/personal", "/api/procedures", "/api/meetings/schedule")


class Visit:
    """A freshly loaded page plus the API paths it asked for."""

    def __init__(self, page, paths, errors):
        self.page = page
        self.paths = paths
        self.errors = errors

    def today_requests(self):
        return [p for p in self.paths if p.startswith(TODAY_SOURCES)]


def visit(server, browser, query=""):
    """Open the surface in its own context, recording every API path it requests."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    paths, errors = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: paths.append(urlsplit(r.url).path))
    page.goto(server + "/?token=" + TOKEN + query, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(400)
    return Visit(page, paths, errors), context


def is_task_canvas(page):
    """The task canvas is showing: the welcome card is up and the dashboard is gone."""
    return page.evaluate("() => !!document.getElementById('welcome') && "
                         "!document.getElementById('todayDashboard')")


def is_today(page):
    return page.evaluate("() => !!document.getElementById('todayDashboard')")


def reload(page):
    page.reload(wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(400)


# ---------------------------------------------------------------- the reported problem
def test_new_task_and_its_unsent_draft_are_both_still_there_after_a_reload(ui):
    page = ui.page
    page.locator("#newChat").click()
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)          # the draft is stored on input, not on a timer
    before = len(_Fixture.stream_requests)

    reload(page)

    expect(page.locator("#pageTitle")).to_have_text("New task")
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert is_task_canvas(page), "the reload should land back on the task canvas, not Today"
    # Coming back to the canvas is not starting anything: no run, and no session in the address.
    assert len(_Fixture.stream_requests) == before
    assert "session=" not in page.url


def test_the_restored_new_task_can_still_send_and_becomes_that_session(ui):
    """The restored canvas is a working one, not a picture of one."""
    page = ui.page
    page.locator("#newChat").click()
    reload(page)
    assert is_task_canvas(page)

    ui.ask("Read README.md and tell me what this tool does")

    assert "session=s-read" in page.url
    assert "view=new" not in page.url, "a named session identifies the view by itself"
    expect(page.locator("#log")).to_contain_text("It reads a CSV and prints per-category totals.")


def test_an_explicit_today_click_sends_the_next_reload_back_to_today(ui):
    page = ui.page
    page.locator("#newChat").click()
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)
    page.locator("#navHome").click()
    page.wait_for_timeout(250)
    expect(page.locator("#pageTitle")).to_have_text("Today")

    reload(page)

    expect(page.locator("#pageTitle")).to_have_text("Today")
    assert is_today(page)
    # Draft scope is unchanged by this: Today and New task have always shared the one unsent root
    # draft, so the text is still in the composer — it is the *view* that now obeys the last click.
    expect(page.locator("#input")).to_have_value(DRAFT)
    page.locator("#newChat").click()
    expect(page.locator("#input")).to_have_value(DRAFT)


def test_drafts_from_different_threads_are_not_mixed_by_a_restored_view(ui):
    page = ui.page
    page.locator(".thread").first.click()          # an existing session
    page.wait_for_timeout(300)
    session_draft = "Follow-up meant only for this thread"
    page.fill("#input", session_draft)
    page.wait_for_timeout(150)

    page.locator("#newChat").click()
    expect(page.locator("#input")).to_have_value("")
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)

    reload(page)
    expect(page.locator("#pageTitle")).to_have_text("New task")
    expect(page.locator("#input")).to_have_value(DRAFT)

    page.locator(".thread").first.click()
    page.wait_for_timeout(300)
    expect(page.locator("#input")).to_have_value(session_draft)


# ---------------------------------------------------------------- first visit and deep links
def test_a_first_visit_still_opens_today_and_loads_its_sources(server, browser):
    seen, context = visit(server, browser)
    try:
        expect(seen.page.locator("#pageTitle")).to_have_text("Today")
        assert is_today(seen.page)
        assert seen.today_requests(), "a first visit should still gather Today"
        assert seen.errors == [], "JS errors: %r" % seen.errors
    finally:
        context.close()


def test_the_new_task_address_opens_the_canvas_without_gathering_today(server, browser):
    seen, context = visit(server, browser, "&view=new")
    try:
        expect(seen.page.locator("#pageTitle")).to_have_text("New task")
        assert is_task_canvas(seen.page)
        assert seen.today_requests() == [], \
            "Today's sources were fetched for a page that never shows them: %r" % seen.today_requests()
        assert seen.errors == [], "JS errors: %r" % seen.errors
    finally:
        context.close()


def test_a_session_deep_link_still_opens_that_session(server, browser):
    seen, context = visit(server, browser, "&session=s-cap")
    try:
        expect(seen.page.locator("#pageTitle")).to_have_text(
            "Migrate every module to the new config loader")
        expect(seen.page.locator("#log")).to_contain_text(
            "I converted the first two modules and listed the rest.")
        assert "session=s-cap" in seen.page.url
        assert seen.errors == [], "JS errors: %r" % seen.errors
    finally:
        context.close()


def test_a_session_deep_link_wins_over_a_stale_new_task_marker(server, browser):
    """Both keys in one address is a contradiction; the named session is the stronger claim."""
    seen, context = visit(server, browser, "&session=s-cap&view=new")
    try:
        expect(seen.page.locator("#log")).to_contain_text(
            "I converted the first two modules and listed the rest.")
        assert "session=s-cap" in seen.page.url
        reload(seen.page)
        expect(seen.page.locator("#log")).to_contain_text(
            "I converted the first two modules and listed the rest.")
        assert seen.errors == [], "JS errors: %r" % seen.errors
    finally:
        context.close()


def test_the_address_keeps_the_parameters_it_arrived_with(ui):
    """Switching views must not drop the token or the embedding flag from the address."""
    page = ui.page
    page.goto(page.url.split("?")[0] + "?token=" + TOKEN + "&vscode_embed=1", wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(300)
    page.locator("#newChat").click()
    page.wait_for_timeout(150)
    assert "token=" + TOKEN in page.url and "vscode_embed=1" in page.url
    reload(page)
    expect(page.locator("#pageTitle")).to_have_text("New task")
    assert "vscode_embed=1" in page.url
    # The token belongs to the page's own address, not to links it renders.
    assert page.evaluate("""() => [...document.querySelectorAll('a[href]')]
        .filter(a => /token=/.test(a.getAttribute('href'))).length""") == 0


# ---------------------------------------------------------------- pressed before the script ran
# The sidebar is painted from the top of a long document; this page's script is at the bottom of it.
# On a slow first load there is a real moment where New task is on screen with no handler behind it,
# and the press a person makes there used to vanish — leaving them on Today, with pressing the
# button a second time as the only way through. The server below hands over the markup, waits, and
# only then delivers the script, which is that moment held still.
#
# The address the page arrived at is a request from before that press. The press is later, and it is
# the same press the two buttons answer once the page is alive, so it has to be answered the same
# way — including when the address named a session or a mission. These tests hold the boot replies
# for that named thread open until after the choice has been made, so a late reply that repainted
# the surface the visitor had just left would be caught here rather than by a person.
class _Split(_Fixture):
    gate = threading.Event()             # releases the page's own script
    late = threading.Event()             # releases the boot replies held behind the press
    held = ()                            # path prefixes whose replies wait for `late`

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(WEBUI, "index.html"), "rb") as fh:
                body = fh.read()
            body = body.replace(b'<meta charset="utf-8">',
                                b'<meta charset="utf-8">\n<meta name="collie-token" content="%s">\n'
                                % TOKEN.encode(), 1)
            cut = body.rindex(b"<script>", 0, body.index(b'"use strict"'))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:cut]); self.wfile.flush()
            _Split.gate.wait(15)
            self.wfile.write(body[cut:]); self.wfile.flush()
            return
        if _Split.held and path.startswith(_Split.held):
            _Split.late.wait(15)
        return _Fixture.do_GET(self)


class HalfLoaded:
    """A page stopped between its markup and its script, released a step at a time."""

    def __init__(self, page, errors, paths):
        self.page = page
        self.errors = errors
        self.paths = paths

    def finish(self):
        """Deliver the script and let the page boot.

        `data-surface` is the page's own mark that its script has run; the composer cannot stand in
        for it, because a mission canvas is a boot that hides the composer on purpose.
        """
        _Split.gate.set()
        self.page.wait_for_load_state("load")
        self.page.wait_for_selector("body[data-surface]", timeout=8000)
        self.page.wait_for_timeout(500)

    def settle(self):
        """Let the replies that were held open land, behind everything the visitor did."""
        _Split.late.set()
        self.page.wait_for_timeout(800)

    def messages(self):
        return self.page.locator("#log .msg").count()


@pytest.fixture
def half_loaded(browser):                                          # noqa: F811
    """Opens such a page — at any address, with any boot replies held open."""
    _Split.gate.clear(); _Split.late.clear(); _Split.held = ()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Split)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    address = "http://127.0.0.1:%d" % httpd.server_address[1]
    contexts = []

    def open_page(query="", hold=(), prepare=None):
        _Split.held = tuple(hold)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        contexts.append(context)
        page = context.new_page()
        errors, paths = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: paths.append(urlsplit(r.url).path))
        if prepare:
            prepare(page)
        page.goto(address + "/?token=" + TOKEN + query, wait_until="commit")
        page.wait_for_selector("#newChat", state="visible", timeout=8000)
        return HalfLoaded(page, errors, paths)

    try:
        yield open_page
    finally:
        _Split.gate.set(); _Split.late.set()
        for context in contexts:
            context.close()
        httpd.shutdown()


def test_new_task_pressed_before_the_script_arrived_is_still_honoured(half_loaded):
    early = half_loaded()
    page = early.page
    before = len(_Fixture.stream_requests)
    page.locator("#newChat").click()

    early.finish()

    expect(page.locator("#pageTitle")).to_have_text("New task")
    assert is_task_canvas(page), "the press was dropped and the visitor was left on Today"
    assert "view=new" in page.url, "the honoured press has to survive the next reload too"
    # Honouring it is not starting anything, and Today's sources stay unread for a page that never
    # shows them.
    assert len(_Fixture.stream_requests) == before
    assert [p for p in early.paths if p.startswith(TODAY_SOURCES)] == []
    assert early.errors == [], "JS errors: %r" % early.errors


def test_today_pressed_before_the_script_arrived_still_lands_on_today(half_loaded):
    """The reverse press, from the same dead moment: Today is a choice, not just the default."""
    early = half_loaded()
    page = early.page
    page.locator("#navHome").click()

    early.finish()

    expect(page.locator("#pageTitle")).to_have_text("Today")
    assert is_today(page)
    assert "view=new" not in page.url
    assert early.errors == [], "JS errors: %r" % early.errors


def test_new_task_pressed_before_the_script_leaves_a_session_deep_link(half_loaded):
    """The press is newer than the address that opened the page, so the thread is left behind.

    The saved thread's own replies are held open across the press and released afterwards, which is
    the shape of the problem: an answer to a question the visitor has already walked away from.
    """
    early = half_loaded("&session=s-cap&vscode_embed=1",
                        hold=("/api/session/s-cap", "/api/sessions"))
    page = early.page
    before = len(_Fixture.stream_requests)
    page.locator("#newChat").click()
    page.fill("#input", DRAFT)                    # typed in the same pre-script moment

    early.finish()
    early.settle()

    expect(page.locator("#pageTitle")).to_have_text("New task")
    assert is_task_canvas(page), "the visitor was left in the thread they had just walked out of"
    assert early.messages() == 0, "the old transcript was painted over the canvas they asked for"
    expect(page.locator("#log")).not_to_contain_text(
        "I converted the first two modules and listed the rest.")
    # The address has to agree with the surface: the thread is no longer the one being shown, and
    # the canvas that is showing survives the next reload.
    assert "session=" not in page.url
    assert "view=new" in page.url
    assert "token=" + TOKEN in page.url and "vscode_embed=1" in page.url
    # The words they typed belong to the view they chose, and choosing it starts nothing.
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert len(_Fixture.stream_requests) == before
    assert early.errors == [], "JS errors: %r" % early.errors

    # Those words are the root's, not the abandoned thread's: opening that thread from the sidebar
    # shows its own empty composer, and coming back shows them again.
    page.locator(".thread").filter(has_text="Migrate every module").first.click()
    page.wait_for_timeout(400)
    expect(page.locator("#input")).to_have_value("")
    page.locator("#newChat").click()
    expect(page.locator("#input")).to_have_value(DRAFT)


@pytest.mark.parametrize("latest", ["New words typed before the page finishes loading", ""])
def test_early_root_edit_wins_over_a_previously_saved_root_draft(half_loaded, latest):
    early = half_loaded()
    page = early.page
    early.finish()
    early.settle()
    page.fill("#input", "Older root draft saved through the real composer")
    _Split.gate.clear()
    page.goto(page.url.split("?")[0] + "?token=" + TOKEN + "&session=s-cap",
              wait_until="commit")
    page.wait_for_selector("#newChat", state="visible")
    page.locator("#newChat").click()
    page.fill("#input", "Temporary text")
    page.fill("#input", latest)
    early.finish()
    early.settle()
    expect(page.locator("#pageTitle")).to_have_text("New task")
    expect(page.locator("#input")).to_have_value(latest)
    assert early.errors == []
    # The latest edit is persisted, so a second load must not resurrect the older draft either.
    reload(page)
    expect(page.locator("#input")).to_have_value(latest)


def test_today_pressed_before_the_script_clears_a_new_task_address(half_loaded):
    """Today pressed over `view=new`: the marker the press contradicts has to go with it."""
    early = half_loaded("&view=new")
    page = early.page
    page.locator("#navHome").click()

    early.finish()

    expect(page.locator("#pageTitle")).to_have_text("Today")
    assert is_today(page), "the press lost to the address it arrived with"
    assert "view=new" not in page.url, "a reload would send them straight back to the task canvas"
    assert "token=" + TOKEN in page.url
    assert early.errors == [], "JS errors: %r" % early.errors


def test_the_last_of_several_presses_before_the_script_is_the_one_answered(half_loaded):
    """Pressing twice in that dead moment is not a vote: the last press is what they asked for."""
    early = half_loaded("&session=s-cap")
    page = early.page
    page.locator("#newChat").click()
    page.locator("#navHome").click()
    page.locator("#newChat").click()

    early.finish()

    expect(page.locator("#pageTitle")).to_have_text("New task")
    assert is_task_canvas(page)
    assert "view=new" in page.url and "session=" not in page.url

    other = half_loaded()
    other.page.locator("#newChat").click()
    other.page.locator("#navHome").click()

    other.finish()

    expect(other.page.locator("#pageTitle")).to_have_text("Today")
    assert is_today(other.page)
    assert "view=new" not in other.page.url
    assert early.errors == [] and other.errors == [], \
        "JS errors: %r %r" % (early.errors, other.errors)


def test_a_session_deep_link_still_opens_when_no_press_beats_it(half_loaded):
    """Nothing was pressed, so nothing is newer than the address: the deep link is still the word."""
    early = half_loaded("&session=s-cap", hold=("/api/session/s-cap", "/api/sessions"))
    page = early.page

    early.finish()
    early.settle()

    expect(page.locator("#pageTitle")).to_have_text(
        "Migrate every module to the new config loader")
    expect(page.locator("#log")).to_contain_text(
        "I converted the first two modules and listed the rest.")
    assert "session=s-cap" in page.url and "view=new" not in page.url
    assert early.errors == [], "JS errors: %r" % early.errors


def test_a_mission_deep_link_still_opens_when_no_press_beats_it(half_loaded):
    """The same for a mission, which paints a canvas of its own and hides the composer."""
    early = half_loaded("&mission=mission-ui",
                        prepare=lambda page: stage_mission(SimpleNamespace(page=page)))
    page = early.page

    early.finish()

    expect(page.locator("#pageTitle")).to_have_text("Refine the category filter")
    expect(page.locator("#composer")).to_be_hidden()
    assert "mission=mission-ui" in page.url and "session=" not in page.url
    assert early.errors == [], "JS errors: %r" % early.errors


# ---------------------------------------------------------------- back / forward
def test_back_and_forward_show_the_views_their_addresses_name(ui):
    page = ui.page
    root = page.url.split("?")[0] + "?token=" + TOKEN
    page.goto(root, wait_until="load")                       # entry 1: Today
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(300)
    page.goto(root + "&session=s-read", wait_until="load")   # entry 2: a session
    page.wait_for_timeout(400)

    page.locator("#newChat").click()                         # entry 2 becomes New task
    page.wait_for_timeout(200)
    expect(page.locator("#pageTitle")).to_have_text("New task")

    page.go_back()
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(400)
    expect(page.locator("#pageTitle")).to_have_text("Today")
    assert is_today(page)

    page.go_forward()
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(400)
    expect(page.locator("#pageTitle")).to_have_text("New task")
    assert is_task_canvas(page)
