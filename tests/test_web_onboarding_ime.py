"""In the optional naming dialog, Enter and Escape belong to the keyboard until the name is chosen.

Naming a companion in Chinese, Japanese or Korean means typing latin letters into an *open
composition* and then pressing Enter to accept the candidate the keyboard offers — or Escape to
throw that candidate list away.  The naming dialog read both keys unconditionally: Enter meant
"Continue" and Escape meant "Skip for now".  So the very first key of the very first name saved
`小yun` — the half-typed romanisation — as the companion's permanent name and closed onboarding,
or, for Escape, wrote the `collie-name-onboard-skip` flag and shut the dialog for good, before a
single character had been chosen.

These tests drive the real harness/webui/index.html in Chromium against the same staged HTTP
fixture as test_web_ui_run_status, with the identity and settings endpoints answered by Playwright
interception, and open a genuine composition through CDP `Input.imeSetComposition`.  The keydown
they assert on is therefore a trusted Enter/Escape with `isComposing` true — the event an IME
actually delivers.  What they check is what a person sees across the page/request/storage
boundary: nothing was POSTed, no skip flag was written, the dialog is still up with the text still
in the box, and the *next* deliberate Enter, Escape, Continue or Skip does exactly what it says.

Coverage and its limit.  CDP composition is Chromium's own IME path, not a physical Windows/macOS
IME, and no real setting is written — `/api/settings` is intercepted.  Blink's ordering (keydown,
then compositionend) is one of two orderings in the wild: keyboards that end the composition first
and report only the legacy `keyCode` 229 cannot be reproduced through CDP, so that ordering, and
the boundary of the Escape guard, are covered by explicitly synthetic events.
"""
import json
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser, _Fixture   # noqa: F401

PREFIX = "小"                     # what is already committed when the composition opens
PINYIN = "yun"                    # what the keyboard shows while it waits for a choice
HANZI = "云"                      # what the person actually meant


class Naming:
    """The naming dialog of a machine that has never been named, plus what left the page."""

    def __init__(self, ui):
        self.ui = ui
        self.page = ui.page
        self.posts = []           # every /api/settings body the page POSTed, in order
        self.saved = []           # the names the fixture has accepted so far
        self.refuse = None        # (status, body) to answer the next save with, if set

    def _identity(self, route):
        route.fulfill(json={"name": self.saved[-1] if self.saved else "Collie",
                            "machine": "fixture", "avatar": "",
                            "name_source": "explicit" if self.saved else "default",
                            "name_editable": True})

    def _settings(self, route):
        if route.request.method != "POST":
            route.continue_()
            return
        body = route.request.post_data_json
        self.posts.append(body)
        if self.refuse is not None:
            status, payload = self.refuse
            self.refuse = None
            route.fulfill(status=status, json=payload)
            return
        if "COMPANION_NAME" in body:
            self.saved.append(body["COMPANION_NAME"])
        route.fulfill(json={"ok": True})

    def open(self):
        """Explicitly preview naming; ordinary first-run no longer requires a name."""
        self.page.evaluate("() => localStorage.removeItem('collie-name-onboard-skip')")
        self.page.route("**/api/whoami", self._identity)
        self.page.route("**/api/settings?*", self._settings)
        url = self.page.url
        self.page.goto(url + ("&" if "?" in url else "?") + "preview=identity", wait_until="load")
        expect(self.overlay).to_be_visible()
        expect(self.field).to_be_focused()
        return self

    @property
    def overlay(self):
        return self.page.locator("#nameOverlay")

    @property
    def field(self):
        return self.page.locator("#nameInput")

    def skipped(self):
        """The persisted "don't ask again" flag, as the next page load would read it."""
        return self.page.evaluate("() => localStorage.getItem('collie-name-onboard-skip')")

    def compose(self, prefix, text):
        """Open a real Chromium composition of `text` after `prefix`, the way a keyboard does."""
        self.field.fill(prefix)
        self.page.evaluate("""() => { const el = document.querySelector('#nameInput'); el.focus();
                                     el.setSelectionRange(el.value.length, el.value.length); }""")
        cdp = self.page.context.new_cdp_session(self.page)
        cdp.send("Input.imeSetComposition", {"text": text, "selectionStart": len(text),
                                             "selectionEnd": len(text)})
        expect(self.field).to_have_value(prefix + text)
        return cdp

    def commit(self, cdp, prefix, text):
        """The candidate the person picks: the composition ends with real characters in the box."""
        cdp.send("Input.insertText", {"text": text})
        expect(self.field).to_have_value(prefix + text)


@pytest.fixture
def naming(ui):                                              # noqa: F811
    return Naming(ui).open()


def watch_avatar_previews(page):
    """Every avatar preview the page asked for, so "the preview still follows the text" is checked."""
    seen = []
    page.on("request", lambda r: urlparse(r.url).path == "/api/avatar.png" and seen.append(r.url))
    return seen


# ------------------------------- the two keys the keyboard takes back

def test_the_enter_that_picks_a_candidate_does_not_save_the_half_typed_name(naming):
    page = naming.page
    previews = watch_avatar_previews(page)
    cdp = naming.compose(PREFIX, PINYIN)

    page.keyboard.press("Enter")                     # trusted Enter, isComposing = true
    page.wait_for_timeout(300)

    assert naming.posts == [], "the romanisation was saved before a character was chosen"
    assert naming.field.input_value() == PREFIX + PINYIN, "the box was emptied or split mid-word"
    expect(naming.overlay).to_be_visible()
    assert naming.skipped() is None
    # Unchanged by the guard: the preview still follows what is in the box, composing or not.
    assert any("preview=%E5%B0%8Fyun" in url for url in previews), previews

    naming.commit(cdp, PREFIX, HANZI)
    page.keyboard.press("Enter")                     # now an ordinary Enter: Continue
    expect(naming.overlay).not_to_be_visible()

    assert naming.posts == [{"COMPANION_NAME": PREFIX + HANZI}], "the chosen name, saved once"
    assert naming.skipped() is None, "continuing is not skipping"


def test_the_escape_that_closes_the_candidate_list_does_not_skip_naming(naming):
    page = naming.page
    cdp = naming.compose(PREFIX, PINYIN)

    page.keyboard.press("Escape")                    # trusted Escape, isComposing = true
    page.wait_for_timeout(300)

    assert naming.skipped() is None, "a candidate list was dismissed and naming was skipped for good"
    expect(naming.overlay).to_be_visible()
    assert naming.field.input_value() == PREFIX + PINYIN
    assert naming.posts == []

    naming.commit(cdp, PREFIX, HANZI)
    page.keyboard.press("Escape")                    # now a deliberate Escape: skip for now
    expect(naming.overlay).not_to_be_visible()

    assert naming.skipped() == "1", "the guard outlived the composition"
    assert naming.posts == [], "skipping saves nothing"


@pytest.mark.parametrize("key", ["Enter", "Escape"])
def test_the_keydown_the_dialog_refused_really_was_a_composing_key(naming, key):
    """The guard is about the browser's composition state, not about a flag we invented."""
    page = naming.page
    naming.compose(PREFIX, PINYIN)
    page.evaluate("""() => { window.nameImeSeen = [];
        document.querySelector('#nameInput').addEventListener('keydown', e =>
            window.nameImeSeen.push({key: e.key, trusted: e.isTrusted,
                                     composing: e.isComposing, legacy: e.keyCode})); }""")

    page.keyboard.press(key)
    page.wait_for_timeout(250)

    seen = page.evaluate("window.nameImeSeen")
    assert [e for e in seen if e["key"] == key and e["trusted"] and e["composing"]], seen
    assert naming.posts == [], seen
    assert naming.skipped() is None, seen
    expect(naming.overlay).to_be_visible()


# ------------------------------- the controls the dialog still owes the person

def test_the_continue_button_saves_the_committed_name_exactly_once(naming):
    cdp = naming.compose(PREFIX, PINYIN)
    naming.commit(cdp, PREFIX, HANZI)

    naming.page.click("#nameContinue")
    expect(naming.overlay).not_to_be_visible()

    assert naming.posts == [{"COMPANION_NAME": PREFIX + HANZI}]
    assert naming.skipped() is None


def test_the_skip_button_still_skips_for_now(naming):
    naming.page.click("#nameSkip")
    expect(naming.overlay).not_to_be_visible()

    assert naming.skipped() == "1"
    assert naming.posts == []


def test_an_ordinary_escape_with_no_composition_still_skips(naming):
    naming.field.fill("Rowan")
    naming.field.focus()

    naming.page.keyboard.press("Escape")
    expect(naming.overlay).not_to_be_visible()

    assert naming.skipped() == "1"
    assert naming.posts == []


# ------------------------------- refusals keep the text, and the person can fix it

def test_a_name_the_page_rejects_keeps_the_dialog_open_and_the_text_editable(naming):
    page = naming.page
    naming.field.fill("bad<name>")
    naming.field.focus()

    page.keyboard.press("Enter")
    page.wait_for_timeout(250)

    assert naming.posts == [], "a name with markup must not reach the settings endpoint"
    expect(naming.overlay).to_be_visible()
    assert naming.field.get_attribute("aria-invalid") == "true"
    assert "32 characters or fewer" in page.inner_text("#nameError")
    assert naming.field.input_value() == "bad<name>", "the text the person typed was taken away"

    naming.field.fill("Juniper")                     # still editable, and the retry goes through
    page.keyboard.press("Enter")
    expect(naming.overlay).not_to_be_visible()
    assert naming.posts == [{"COMPANION_NAME": "Juniper"}]


def test_a_refused_save_keeps_the_name_in_the_box_and_lets_the_person_retry(naming):
    page = naming.page
    naming.refuse = (409, {"error": "that name is already taken on this machine"})
    naming.field.fill("Juniper")
    naming.field.focus()

    page.keyboard.press("Enter")
    page.wait_for_function("() => document.getElementById('nameError').textContent.length > 0")

    assert "already taken" in page.inner_text("#nameError"), "the server's reason must be shown"
    expect(naming.overlay).to_be_visible()
    assert naming.field.input_value() == "Juniper", "a failed save must not eat the name"
    assert naming.field.is_editable()
    expect(page.locator("#nameContinue")).to_be_enabled()

    page.keyboard.press("Enter")                     # the same name, deliberately, once more
    expect(naming.overlay).not_to_be_visible()
    assert naming.posts == [{"COMPANION_NAME": "Juniper"}] * 2
    assert naming.skipped() is None


# ------------------------------- the other ordering, and the edge of the guard

LEGACY = """() => {
  const el = document.querySelector('#nameInput');
  el.focus();
  const probe = new KeyboardEvent('keydown', {key: 'Enter', keyCode: 229,
                                              bubbles: true, cancelable: true});
  if (probe.keyCode !== 229) return 'unsupported';
  // WebKit / older keyboards order it the other way round: the composition ends first, so the
  // confirming keydown reports isComposing = false and only the legacy keyCode 229 identifies it.
  el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
  el.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true, data: 'x'}));
  el.dispatchEvent(probe);
  return 'dispatched';
}"""


def test_a_confirming_enter_that_arrives_after_compositionend_is_still_not_a_save(naming):
    """Explicitly synthetic: this event sequence is not reachable through Chromium's own IME."""
    page = naming.page
    naming.field.fill(PREFIX + PINYIN)
    if page.evaluate(LEGACY) == "unsupported":
        pytest.skip("this browser does not carry the legacy keyCode on a synthetic keydown")
    page.wait_for_timeout(250)

    assert naming.posts == [], "a keyCode 229 Enter is the keyboard confirming, not the person saving"
    expect(naming.overlay).to_be_visible()
    assert naming.field.input_value() == PREFIX + PINYIN

    naming.field.fill(PREFIX + HANZI)                # the person then continues, deliberately
    page.keyboard.press("Enter")
    expect(naming.overlay).not_to_be_visible()
    assert naming.posts == [{"COMPANION_NAME": PREFIX + HANZI}]


def test_escape_on_another_control_of_the_dialog_still_skips(naming):
    """The guard stops at the name field: nothing else in this dialog can be composing.

    Synthetic on purpose — a real composition ends when focus leaves the field, so this is the
    stale-state case, checked so the guard cannot quietly disable Escape for the rest of the dialog.
    """
    page = naming.page
    page.evaluate("""() => { const el = document.querySelector('#nameInput'); el.focus();
                            el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
                            document.getElementById('nameSkip').focus(); }""")

    page.keyboard.press("Escape")
    expect(naming.overlay).not_to_be_visible()

    assert naming.skipped() == "1"
    assert naming.posts == []


def test_reopening_the_dialog_starts_with_no_leftover_composition_state(naming):
    """A composition abandoned by closing the dialog must not deaden the next Enter."""
    page = naming.page
    naming.compose(PREFIX, PINYIN)
    page.click("#nameSkip")                          # closed mid-composition, by mouse
    expect(naming.overlay).not_to_be_visible()

    naming.open()                                    # a later visit, flag cleared
    naming.field.fill("Juniper")
    naming.field.focus()
    page.keyboard.press("Enter")
    expect(naming.overlay).not_to_be_visible()

    assert naming.posts == [{"COMPANION_NAME": "Juniper"}], json.dumps(naming.posts)
