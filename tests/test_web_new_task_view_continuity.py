"""Which surface a reload lands on, checked in a real browser against the real index.html.

Today and New task share the root address, so opening the task canvas used to be a choice the page
forgot the moment it was reloaded: the composer still held the unsent draft, but the canvas behind
it had gone back to the Today dashboard, and the only way back to the task was to press New task
again. These tests pin the four things that decide where a reload lands — an explicit New task, an
explicit Today, a first visit with nothing chosen, and a session deep link — plus the back/forward
behaviour that has to agree with them.

Everything is asserted through what a person can see (the header, the canvas, the composer) and
what the page asks the server for. The fixture server, browser and staged sessions are the ones
from test_web_ui_run_status.
"""
from urllib.parse import urlsplit

from playwright.sync_api import expect

from test_web_ui_run_status import TOKEN, _Fixture, browser, server, ui  # noqa: F401

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
