"""A modal's focus move belongs to the dialog that is up *now*, not to the one that scheduled it.

Every dialog on this page hands its focus move to the next tick: `dialogOpened` waits a tick before
focusing the field it wants to start on, and `dialogClosed` waits a tick before giving the focus back
to whatever opened it.  Those two callbacks used to fire unconditionally.  A tick is long enough for
the person to have moved on — Escape and then Ctrl+K again is a single flick of the fingers — and the
late callback then landed on the wrong page: the *closing* dialog's restore pulled the caret out of a
picker that had already reopened (the reopened dialog was still on screen, with its search field now
dead), and a late *open* callback pulled the caret into a dialog that was gone.

So the callbacks are owned by a lifecycle: scheduling a new one cancels the one still waiting, and
any that runs anyway re-checks, at the moment it runs, that its dialog is still the visible one.

The delay here is injected, not waited out.  These tests replace `window.setTimeout` for the length
of one step, keep the real zero-delay callback the page scheduled, and release it by hand at the
moment they choose — so the ordering under test is exact and nothing sleeps.  `clearTimeout` is
honoured by the harness, which is how a cancelled callback is distinguished from one that ran and
declined.  This is fault-injected *timer ordering* inside one Chromium page: it says nothing about
how a real OS scheduler, a slow machine, another browser engine or any external provider would
interleave, and no test here focuses anything itself to paper over a focus the page lost.

Everything drives the real harness/webui/index.html in Chromium against the staged HTTP fixture
shared with test_web_ui_run_status, with the catalog and the model-change endpoint answered by
Playwright interception over invented entries: no provider is contacted and no account, key or
setting is touched.
"""
import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser   # noqa: F401
from test_web_model_picker_locale import Picker

# Hold the real zero-delay dialog-focus callbacks the page schedules while armed, and honour a
# cancellation of one.  Nothing else about time is touched: every other timer, zero-delay or not,
# keeps running normally, so polling, the switch animation and the rest of the page behave exactly as
# they always do.  The callback is recognised by its own source — the shared lifecycle's
# `runDialogFocus`, or the individual closures the page scheduled before it had one — so that the
# same tests can be pointed at either version of the page and an unrelated next-tick task (a queue
# reload, a permissions refresh) is never caught in its place.
FOCUS_CALLBACK = r"/runDialogFocus|querySelector\(initial\)|target\.isConnected|modelSearch\.focus/"
HOLD = """() => {
  const set = window.setTimeout.bind(window), clear = window.clearTimeout.bind(window);
  const held = new Map(), cancelled = [];
  let arm = 0;
  window.setTimeout = function (fn, ms, ...args) {
    if (arm > 0 && Number(ms || 0) === 0 && FOCUS_CALLBACK.test(String(fn))) {
      arm -= 1;
      const id = set(() => {}, 100000);          // a real, live id the page may cancel
      held.set(id, () => fn(...args));
      return id;
    }
    return set(fn, ms, ...args);
  };
  window.clearTimeout = function (id) {
    if (held.has(id)) { held.delete(id); cancelled.push(id); }
    clear(id);
  };
  window.__hold = {
    arm: (n) => { arm = n; },
    held: () => held.size,
    cancelled: () => cancelled.length,
    release: () => {
      const pending = Array.from(held.values());
      held.clear();
      for (const fn of pending) fn();
    },
  };
}""".replace("FOCUS_CALLBACK", FOCUS_CALLBACK)


class Hold:
    """The page's own delayed focus callbacks, caught one step at a time."""

    def __init__(self, page):
        self.page = page
        page.evaluate(HOLD)

    def arm(self, count=1):
        self.page.evaluate("n => window.__hold.arm(n)", count)
        return self

    @property
    def held(self):
        return self.page.evaluate("() => window.__hold.held()")

    @property
    def cancelled(self):
        return self.page.evaluate("() => window.__hold.cancelled()")

    def release(self):
        self.page.evaluate("() => window.__hold.release()")


def focused(page):
    return page.evaluate("() => document.activeElement ? document.activeElement.id : ''")


def composer(picker, text=""):
    """The person is in the composer, mid-draft, when the dialog is opened over them."""
    page = picker.page
    page.locator("#input").focus()
    if text:
        page.keyboard.type(text)
    assert focused(page) == "input"
    return page


@pytest.fixture
def picker(ui):                                              # noqa: F811
    return Picker(ui).install().reload()


# ------------------------------------------------------- a closing dialog does not outlive its close

def test_a_held_close_restore_does_not_pull_focus_out_of_a_reopened_picker(picker):
    page = composer(picker, "half a message")
    picker.open()
    expect(picker.field).to_be_focused()

    hold = Hold(page).arm(1)
    page.keyboard.press("Escape")                    # its restore is caught, still unfired
    assert hold.held == 1, "instrument: the close did not schedule a delayed focus"

    picker.open()                                    # reopened before the restore ever ran
    expect(picker.field).to_be_focused()
    assert (hold.held, hold.cancelled) == (0, 1), "the superseded restore was neither cancelled nor kept"

    hold.release()                                   # and if it had already fired, it must decline
    assert focused(page) == "modelSearch", "the old close pulled focus out of the reopened picker"
    expect(picker.overlay).to_be_visible()
    assert page.input_value("#input") == "half a message"
    assert picker.posts == [], picker.posts


def test_an_ordinary_close_still_gives_the_caret_back_to_the_composer(picker):
    page = composer(picker, "half a message")
    picker.open()

    hold = Hold(page).arm(1)
    page.keyboard.press("Escape")
    expect(picker.overlay).not_to_be_visible()
    assert hold.held == 1
    assert focused(page) != "input", "instrument: the restore ran early, so nothing was under test"

    hold.release()                                   # the only dialog is gone: the restore is right
    assert focused(page) == "input"
    assert page.input_value("#input") == "half a message"
    assert page.evaluate("() => document.getElementById('input').selectionStart") == len("half a message")


def test_a_close_restore_held_across_another_dialog_does_not_steal_from_it(picker):
    """Two different modals: the settings restore may not land inside the picker that came up."""
    page = composer(picker)
    page.click("#settingsBtn")
    expect(page.locator("#setOverlay.open")).to_have_count(1)
    expect(page.locator("#setClose")).to_be_focused()

    hold = Hold(page).arm(1)
    page.keyboard.press("Escape")
    expect(page.locator("#setOverlay.open")).to_have_count(0)
    assert hold.held == 1

    picker.open()                                    # a *different* dialog now owns the focus
    expect(picker.field).to_be_focused()
    hold.release()
    assert focused(page) == "modelSearch", "the settings restore reached into the model picker"
    expect(picker.overlay).to_be_visible()


def test_settings_closed_on_its_own_still_restores_the_control_that_opened_it(picker):
    page = composer(picker)
    page.click("#settingsBtn")
    expect(page.locator("#setClose")).to_be_focused()

    hold = Hold(page).arm(1)
    page.keyboard.press("Escape")
    assert hold.held == 1
    hold.release()
    assert focused(page) == "settingsBtn"


# -------------------------------------------------------- an opening dialog does not outlive its open

def test_a_held_open_focus_does_not_reach_into_a_dialog_that_is_already_closed(picker):
    page = composer(picker, "half a message")

    hold = Hold(page).arm(1)
    page.keyboard.press("Control+k")                 # the picker's initial focus is caught
    expect(picker.overlay).to_be_visible()
    assert hold.held == 1, "instrument: the open did not schedule a delayed focus"

    page.keyboard.press("Escape")                    # closed again before it ever ran
    expect(picker.overlay).not_to_be_visible()
    hold.release()                                   # anything of that open still waiting

    assert focused(page) == "input", "a closed dialog took the caret"
    assert picker.field.evaluate("el => el === document.activeElement") is False
    assert page.input_value("#input") == "half a message"
    assert picker.posts == [], picker.posts


def test_a_held_open_focus_does_not_reach_past_the_dialog_that_came_up_after_it(picker):
    """Settings is still on screen under the picker — being visible is not the same as being current."""
    page = composer(picker)

    hold = Hold(page).arm(1)
    page.click("#settingsBtn")                       # settings is up, its initial focus is caught
    expect(page.locator("#setOverlay.open")).to_have_count(1)
    assert hold.held == 1, "instrument: the open did not schedule a delayed focus"
    assert focused(page) == "settingsBtn"

    page.keyboard.press("Control+k")                 # the picker opens over it and takes the caret
    expect(picker.overlay).to_be_visible()
    expect(picker.field).to_be_focused()

    hold.release()
    assert focused(page) == "modelSearch", "the older dialog's opening focus reached over the newer one"
    expect(page.locator("#setOverlay.open")).to_have_count(1)


def test_a_second_open_hotkey_does_not_make_the_dialog_its_own_return_focus(picker):
    """Ctrl+K pressed again while the picker is up must not forget where the person came from."""
    page = composer(picker, "half a message")
    picker.open()
    picker.field.type("mock")

    page.keyboard.press("Control+k")                 # the same dialog, opened over itself
    expect(picker.overlay).to_be_visible()
    expect(picker.field).to_be_focused()

    page.keyboard.press("Escape")
    expect(picker.overlay).not_to_be_visible()
    expect(page.locator("#input")).to_be_focused()
    assert page.input_value("#input") == "half a message"
    assert picker.posts == [], picker.posts


def test_the_tab_trap_and_the_search_draft_survive_a_released_stale_callback(picker):
    page = composer(picker)
    picker.open()
    picker.field.type("loc")
    expect(page.locator(".model-option")).to_have_count(1)

    hold = Hold(page).arm(1)
    page.keyboard.press("Escape")
    picker.open()
    hold.release()

    # The reopened dialog is intact: its own search draft, its highlight and the Tab trap that keeps
    # the caret inside it are all still the reopened dialog's.
    assert focused(page) == "modelSearch"
    assert picker.field.input_value() == "", "a reopen starts on an empty search"
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.getElementById('modelOverlay').contains(document.activeElement)")
