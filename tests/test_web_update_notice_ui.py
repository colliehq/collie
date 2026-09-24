"""The update notice in Settings → General, driven in Chromium against staged update answers.

The real ``harness/webui/index.html`` runs over the fixture server shared with
test_web_ui_run_status; ``/api/settings`` and ``/api/update*`` are answered by Playwright
interception. No release feed, installer, account or saved setting is touched. The restart after an
install is staged as the server becoming unreachable and then answering 403, which is what a new
server process with a new page token does.
"""
import copy

import pytest
from playwright.sync_api import expect

from harness import settings
from test_web_ui_run_status import ui, server, browser  # noqa: F401
from test_web_settings_capabilities import Settings, SCHEMA

UPDATE_ROW = [row for row in settings.SCHEMA if row["key"] == "UPDATE_CHECK"]

NEWER = {"current": "0.29.1", "kind": "setup", "channel": "stable", "auto": False, "local": True,
         "checking": False, "checked_at": 1000.0, "checked_age_s": 120.0, "latest": "0.30.0",
         "newer": True, "url": "https://example.test/releases/v0.30.0",
         "notes": "Faster starts.\n<img src=x onerror=\"window.__updateXss=1\">",
         "error": "", "error_at": None, "command": "collie update --channel stable --yes",
         "one_press": True, "install": {"state": "none"}}


def answer(**changes):
    value = copy.deepcopy(NEWER)
    value.update(changes)
    return value


class Updates(Settings):
    """A page whose settings schema carries UPDATE_CHECK and whose update routes are staged."""

    def __init__(self, ui, first):
        super().__init__(ui)
        self.values["UPDATE_CHECK"] = "off"
        self.gets = []            # answers for successive GET /api/update, the last one repeats
        self.get_count = 0
        self.first = first
        self.post_bodies = []     # (action, body)
        self.post_answers = {}    # action -> (status, body)

    def _settings(self, route):
        if route.request.method != "POST":
            route.fulfill(json={"schema": SCHEMA + UPDATE_ROW, "values": dict(self.values)})
            return
        super()._settings(route)

    def _update(self, route):
        url = route.request.url
        if route.request.method == "POST":
            action = url.split("/api/update/", 1)[1].split("?", 1)[0]
            self.post_bodies.append((action, route.request.post_data_json))
            status, body = self.post_answers[action]
            route.fulfill(status=status, json=body)
            return
        self.get_count += 1
        step = self.gets.pop(0) if self.gets else self.first
        if step == "down":
            route.abort("connectionrefused")
        elif isinstance(step, tuple):
            route.fulfill(status=step[0], json=step[1])
        else:
            route.fulfill(json=step)

    def open(self):
        self.page.route("**/api/update*", self._update)
        self.page.route("**/api/update/*", self._update)
        return super().open()

    def general(self):
        self.page.click("#settingsBtn")
        expect(self.page.locator("#setOverlay.open")).to_have_count(1)
        self.page.click('.set-nav[data-cat="overview"]')
        expect(self.box).to_be_visible()
        return self

    @property
    def box(self):
        return self.page.locator("#updateBox")

    @property
    def line(self):
        return self.page.locator("#updateBox .upd-line")


def test_a_newer_release_is_visible_before_settings_is_opened(ui):
    s = Updates(ui, NEWER).open()
    expect(ui.page.locator("#settingsBtn .upd-dot")).to_have_count(1)
    expect(ui.page.locator("#settingsBtn")).to_have_attribute("aria-label", "Settings · Update available")
    s.general()
    expect(s.line).to_have_text("Collie 0.30.0 is available.")
    expect(s.box.locator(".upd-name")).to_have_text("Collie 0.29.1")
    s.box.locator("summary", has_text="What's new in 0.30.0").click()
    # Release notes are text: markup in them is shown, never parsed.
    expect(s.box.locator(".upd-notes")).to_contain_text('<img src=x onerror="window.__updateXss=1">')
    assert not ui.page.evaluate("() => !!window.__updateXss")
    expect(s.box.locator("a", has_text="Release page")).to_have_attribute(
        "href", "https://example.test/releases/v0.30.0")


def test_install_names_the_version_shown_and_rides_out_the_restart(ui):
    s = Updates(ui, NEWER).open()
    s.general()
    s.post_answers["install"] = (200, answer(install={"state": "running", "target": "0.30.0"}))
    handed = answer(install={"state": "handed_off", "target": "0.30.0"})
    s.gets = [handed, "down", "down", (403, {"error": "forbidden"})]
    s.box.get_by_role("button", name="Install 0.30.0 and restart").click()
    assert s.post_bodies == [("install", {"version": "0.30.0"})]
    expect(s.line).to_have_text("Downloading and verifying Collie 0.30.0…")
    expect(s.box.get_by_role("button", name="Check for updates")).to_be_disabled()
    expect(s.line).to_have_text("Verified. Collie will close, install 0.30.0 and start again by itself.",
                                timeout=6000)
    expect(s.line).to_have_text("Installing… Collie is restarting.", timeout=6000)
    # The new server answers the old page token with 403: the page reloads onto it.
    ui.page.wait_for_function("() => !window.__stillTheSamePage", timeout=12000)


def test_a_failed_check_never_reads_as_up_to_date(ui):
    offline = answer(checked_at=None, checked_age_s=None, latest=None, newer=False, notes="",
                     one_press=False, error="URLError: the network is unreachable", error_at=2000.0)
    s = Updates(ui, offline).open()
    expect(ui.page.locator("#settingsBtn .upd-dot")).to_have_count(0)
    s.general()
    expect(s.line).to_have_text("Could not check for updates: URLError: the network is unreachable")
    expect(s.box.locator(".upd-badge")).to_have_count(0)
    expect(s.box.get_by_role("button", name="Install 0.30.0 and restart")).to_have_count(0)
    s.post_answers["check"] = (200, answer(latest="0.29.1", newer=False, notes="", one_press=False,
                                           checked_at=3000.0, checked_age_s=4.0))
    s.box.get_by_role("button", name="Check for updates").click()
    expect(s.line).to_have_text("You have the latest version. · checked just now")
    expect(s.box.locator(".upd-badge")).to_have_text("up to date")
    assert s.post_bodies == [("check", {})]


def test_a_copy_without_one_press_is_given_the_exact_command(ui):
    s = Updates(ui, answer(kind="pip", one_press=False)).open()
    s.general()
    expect(s.box.get_by_role("button", name="Install 0.30.0 and restart")).to_have_count(0)
    expect(s.box.locator(".upd-cmd")).to_have_text("collie update --channel stable --yes")


def test_a_phone_is_told_where_updates_are_installed(ui):
    s = Updates(ui, answer(one_press=False, local=False)).open()
    s.general()
    expect(s.box).to_contain_text("Updates are installed from Collie on this computer.")
    expect(s.box.locator(".upd-cmd")).to_have_count(0)


def test_a_refused_install_shows_why_and_the_release_now_current(ui):
    s = Updates(ui, NEWER).open()
    s.general()
    s.post_answers["install"] = (409, {"error": "the release shown is no longer the latest; check again first",
                                       "update": answer(latest="0.30.1")})
    s.box.get_by_role("button", name="Install 0.30.0 and restart").click()
    expect(s.line).to_have_text("the release shown is no longer the latest; check again first")
    expect(s.box.get_by_role("button", name="Install 0.30.1 and restart")).to_be_visible()
    assert s.page.evaluate("() => !!window.__stillTheSamePage")


def test_turning_on_automatic_checks_saves_and_rereads_the_notice(ui):
    s = Updates(ui, answer(latest=None, newer=False, checked_at=None, checked_age_s=None,
                           one_press=False, notes="")).open()
    s.general()
    expect(s.line).to_have_text("Not checked yet. Checking asks api.github.com for the latest release.")
    # The server starts its first automatic check when the next read arrives after the save.
    s.gets = [answer(latest=None, newer=False, checked_at=None, checked_age_s=None,
                     one_press=False, notes="", auto=True, checking=True)]
    ui.page.locator('.set-row[data-key="UPDATE_CHECK"] .set-toggle').click()
    expect(s.status).to_have_class("set-status ok")
    assert s.posts == [{"UPDATE_CHECK": "on"}]
    expect(s.line).to_have_text("Checking for updates…")


def test_a_refused_install_reads_in_chinese_too(ui):
    s = Updates(ui, NEWER)
    s.values["LANG"] = "zh"
    s.open()
    s.general()
    s.post_answers["install"] = (409, {"error": "the release shown is no longer the latest; check again first",
                                       "update": answer(latest="0.30.1")})
    s.box.get_by_role("button", name="安装 0.30.0 并重启").click()
    expect(s.line).to_have_text("显示的版本已不是最新版，请先重新检查")
    expect(s.box.get_by_role("button", name="安装 0.30.1 并重启")).to_be_visible()


def test_the_mac_app_is_pointed_at_the_release_page_not_a_missing_command(ui):
    s = Updates(ui, answer(kind="app", one_press=False, command="")).open()
    s.general()
    expect(s.box).to_contain_text("Download it from the release page and replace Collie in Applications.")
    expect(s.box.locator(".upd-cmd")).to_have_count(0)


def test_a_second_window_follows_an_install_started_elsewhere(ui):
    handed = answer(install={"state": "handed_off", "target": "0.30.0"})
    s = Updates(ui, handed)
    s.gets = [handed, handed, "down", (403, {"error": "forbidden"})]
    s.open()
    # No click here: the page saw an install in progress and waits for the restart by itself.
    ui.page.wait_for_function("() => !window.__stillTheSamePage", timeout=15000)
