"""In the model picker, Enter, Escape and the arrows belong to the keyboard until a character is chosen.

Searching for a model in Chinese, Japanese or Korean means typing latin letters into an *open
composition* and then pressing Enter to accept the candidate the keyboard offers — or Escape to
throw that candidate list away, or the arrows to walk it.  The picker read all three keys
unconditionally: Enter switched to whatever model the half-typed romanisation happened to match
(`yun` matched a model whose name starts "Yun", and the page POSTed that switch to /api/model),
Escape closed the whole dialog mid-word, and the arrows moved the option highlight underneath the
candidate list.  So the first key of the first search changed the model the next task would run on,
before a single character had been chosen.

These tests drive the real harness/webui/index.html in Chromium against the same staged HTTP
fixture as test_web_ui_run_status, with the model catalog and the model-change endpoint answered by
Playwright interception over invented fixture entries, and open a genuine composition through CDP
`Input.imeSetComposition`.  The keydown they assert on is therefore a trusted Enter/Escape/Arrow
with `isComposing` true — the event an IME actually delivers.  What they check is what a person
sees across the page/request/focus/dialog boundary: no /api/model request left the page, the
highlight has not moved, the dialog is still up with the search still editable, and the *next*
deliberate Enter, Escape, arrow, click or Ctrl+K does exactly what it says.

Coverage and its limit.  CDP composition is Chromium's own IME path, not a physical Windows/macOS
IME, and no real model, credential or setting is touched — /api/models and /api/model are
intercepted and the entries are invented.  Blink's ordering (keydown, then compositionend) is one
of the two orderings in the wild: keyboards that end the composition first and report only the
legacy `keyCode` 229 cannot be reproduced through CDP, so that ordering, and the boundary of the
guard, are covered by explicitly synthetic events.  Nothing here is evidence about Safari, Firefox
or a physical OS IME.
"""
import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser   # noqa: F401

PINYIN = "yun"                    # what the keyboard shows while it waits for a choice
HANZI = "云"                      # what the person actually meant
ROMANIZED = "Yun mock option"     # the invented entry the half-typed romanisation matches
CLOUD = "云 mock option"          # the invented entry the chosen character matches
PLAIN = "Ordinary mock option"    # a third invented entry, so the arrows have somewhere to go

ENTRIES = [
    {"provider": "anthropic", "model": "romanized", "id": "anthropic:romanized", "label": ROMANIZED,
     "auth": "ok", "kind": "metered", "via": "fixture"},
    {"provider": "anthropic", "model": "cloud", "id": "anthropic:cloud", "label": CLOUD,
     "auth": "ok", "kind": "metered", "via": "fixture"},
    {"provider": "anthropic", "model": "plain", "id": "anthropic:plain", "label": PLAIN,
     "auth": "ok", "kind": "local", "via": "fixture"},
]


class Picker:
    """The model picker over an invented catalog, plus every model change that left the page."""

    def __init__(self, ui):
        self.ui = ui
        self.page = ui.page
        self.posts = []           # every /api/model body the page POSTed, in order
        self.refuse = None        # (status, body) to answer the next switch with, if set

    def _catalog(self, route):
        route.fulfill(json={"current": "anthropic:mock", "entries": ENTRIES})

    def _change(self, route):
        body = route.request.post_data_json
        self.posts.append(body)
        if self.refuse is not None:
            status, payload = self.refuse
            self.refuse = None
            route.fulfill(status=status, json=payload)
            return
        route.fulfill(json={"ok": True, "provider": "anthropic", "model": body["id"].split(":", 1)[1]})

    def open(self):
        """Open the picker the way the shortcut does, and wait for the invented catalog to land."""
        self.page.route("**/api/models*", self._catalog)
        self.page.route("**/api/model?*", self._change)
        self.page.keyboard.press("Control+k")
        expect(self.overlay).to_be_visible()
        expect(self.page.locator(".model-option-label", has_text=CLOUD)).to_have_count(1)
        expect(self.field).to_be_focused()
        return self

    @property
    def overlay(self):
        return self.page.locator("#modelOverlay")

    @property
    def field(self):
        return self.page.locator("#modelSearch")

    def labels(self):
        """The options the list is offering right now, in order."""
        return self.page.eval_on_selector_all(
            ".model-option", "els => els.map(el => el.querySelector('.model-option-label').textContent)")

    def active(self):
        """The option the keyboard highlight is on, as `aria-activedescendant` also reports it."""
        return self.page.eval_on_selector_all(
            ".model-option.is-keyboard-active", "els => els.map(el => el.dataset.modelId)")

    def compose(self, text):
        """Open a real Chromium composition of `text` in the search box, the way a keyboard does."""
        self.field.fill("")
        self.field.focus()
        cdp = self.page.context.new_cdp_session(self.page)
        cdp.send("Input.imeSetComposition", {"text": text, "selectionStart": len(text),
                                             "selectionEnd": len(text)})
        expect(self.field).to_have_value(text)
        return cdp

    def commit(self, cdp, text):
        """The candidate the person picks: the composition ends with real characters in the box."""
        cdp.send("Input.insertText", {"text": text})
        expect(self.field).to_have_value(text)


@pytest.fixture
def picker(ui):                                              # noqa: F811
    return Picker(ui).open()


# ------------------------------- the three keys the keyboard takes back

def test_the_enter_that_picks_a_candidate_does_not_switch_the_model_or_close_the_picker(picker):
    page = picker.page
    cdp = picker.compose(PINYIN)
    assert picker.labels() == [ROMANIZED], "the list must still follow the text as it is composed"

    page.keyboard.press("Enter")                     # trusted Enter, isComposing = true
    page.wait_for_timeout(300)

    assert picker.posts == [], "the model matching the unfinished romanisation was switched to"
    expect(picker.overlay).to_be_visible()
    assert picker.field.input_value() == PINYIN, "the search was emptied or split mid-word"
    assert picker.field.is_editable()

    picker.commit(cdp, HANZI)                        # the candidate replaces "yun": "云"
    assert picker.labels() == [CLOUD]
    page.keyboard.press("Enter")                     # now an ordinary Enter: switch
    expect(picker.overlay).not_to_be_visible()

    assert picker.posts == [{"id": "anthropic:cloud"}], "the chosen model, switched to once"


def test_the_escape_that_closes_the_candidate_list_does_not_close_the_picker(picker):
    page = picker.page
    picker.compose(PINYIN)

    page.keyboard.press("Escape")                    # trusted Escape, isComposing = true
    page.wait_for_timeout(300)

    expect(picker.overlay).to_be_visible(), "a candidate list was dismissed and the picker closed"
    assert picker.posts == []
    expect(picker.field).to_be_focused()
    assert picker.field.is_editable(), "the search the person was typing in was taken away"

    picker.field.fill(HANZI)                         # the person types on, then leaves deliberately
    picker.field.focus()
    page.keyboard.press("Escape")                    # now an ordinary Escape: close
    expect(picker.overlay).not_to_be_visible()
    assert picker.posts == [], "closing the picker switches nothing"


def test_the_arrows_that_walk_the_candidate_list_do_not_move_the_option_highlight(picker):
    page = picker.page
    cdp = picker.compose("mock")                     # all three invented entries match
    assert picker.labels() == [ROMANIZED, CLOUD, PLAIN]
    before = picker.active()
    assert before == ["anthropic:romanized"], before

    page.keyboard.press("ArrowDown")                 # trusted arrows, isComposing = true
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowUp")
    page.wait_for_timeout(250)

    assert picker.active() == before, "the candidate list was walked and the highlight moved with it"
    assert picker.posts == []
    assert picker.field.input_value() == "mock"

    picker.commit(cdp, "mock")                       # composition over, same text in the box
    page.keyboard.press("ArrowDown")                 # now ordinary navigation
    assert picker.active() == ["anthropic:cloud"], "the guard outlived the composition"
    page.keyboard.press("ArrowUp")
    assert picker.active() == ["anthropic:romanized"]

    page.keyboard.press("Enter")
    expect(picker.overlay).not_to_be_visible()
    assert picker.posts == [{"id": "anthropic:romanized"}], "the highlighted model, switched to once"


@pytest.mark.parametrize("key", ["Enter", "Escape", "ArrowDown"])
def test_the_keydown_the_picker_refused_really_was_a_composing_key(picker, key):
    """The guard is about the browser's composition state, not about a flag we invented."""
    page = picker.page
    picker.compose(PINYIN)
    page.evaluate("""() => { window.modelImeSeen = [];
        document.querySelector('#modelSearch').addEventListener('keydown', e =>
            window.modelImeSeen.push({key: e.key, trusted: e.isTrusted,
                                      composing: e.isComposing, legacy: e.keyCode})); }""")

    page.keyboard.press(key)
    page.wait_for_timeout(250)

    seen = page.evaluate("window.modelImeSeen")
    assert [e for e in seen if e["key"] == key and e["trusted"] and e["composing"]], seen
    assert picker.posts == [], seen
    expect(picker.overlay).to_be_visible()


# ------------------------------- the controls the picker still owes the person

def test_an_explicit_click_on_an_option_still_switches_even_mid_composition(picker):
    """The guard is about borrowed keystrokes; a click is nobody's candidate list."""
    page = picker.page
    picker.compose(PINYIN)
    page.keyboard.press("Enter")
    page.wait_for_timeout(250)
    assert picker.posts == []

    page.click(".model-option[data-model-id='anthropic:romanized']")
    expect(picker.overlay).not_to_be_visible()

    assert picker.posts == [{"id": "anthropic:romanized"}], "an explicit choice must still be made"


def test_the_close_button_still_closes_after_a_composition(picker):
    picker.compose(PINYIN)
    picker.page.keyboard.press("Escape")
    picker.page.wait_for_timeout(250)
    expect(picker.overlay).to_be_visible()

    picker.page.click("#modelClose")
    expect(picker.overlay).not_to_be_visible()
    assert picker.posts == []


def test_escape_then_ctrl_k_closes_and_reopens_a_picker_that_still_switches(picker):
    page = picker.page
    picker.compose(PINYIN)
    page.keyboard.press("Escape")                    # the candidate list only
    page.wait_for_timeout(250)
    expect(picker.overlay).to_be_visible()

    picker.field.fill("")
    picker.field.focus()
    page.keyboard.press("Escape")                    # the picker
    expect(picker.overlay).not_to_be_visible()

    picker.open()                                    # Ctrl+K, the same shortcut as before
    assert picker.field.input_value() == "", "a reopened picker starts from a clean search"
    picker.field.fill(HANZI)
    picker.field.focus()
    page.keyboard.press("Enter")
    expect(picker.overlay).not_to_be_visible()

    assert picker.posts == [{"id": "anthropic:cloud"}]


def test_a_composition_abandoned_by_closing_does_not_deaden_the_next_enter(picker):
    """A composition thrown away with the dialog must not leave the guard armed."""
    page = picker.page
    picker.compose(PINYIN)
    page.click("#modelClose")                        # closed mid-composition, by mouse
    expect(picker.overlay).not_to_be_visible()

    picker.open()
    picker.field.fill(HANZI)
    picker.field.focus()
    page.keyboard.press("Enter")
    expect(picker.overlay).not_to_be_visible()

    assert picker.posts == [{"id": "anthropic:cloud"}], picker.posts


# ------------------------------- a switch the page refuses

def test_a_refused_switch_keeps_the_picker_open_and_editable_and_retries_exactly_once(picker):
    page = picker.page
    picker.refuse = (409, {"error": "that model is not available on this account"})
    picker.field.fill(HANZI)
    picker.field.focus()

    page.keyboard.press("Enter")
    page.wait_for_function("() => document.getElementById('modelStatus').classList.contains('err')")

    assert "not available" in page.inner_text("#modelStatus"), "the page's reason must be shown"
    expect(picker.overlay).to_be_visible()
    assert picker.field.is_editable(), "a failed switch must not leave the search disabled"
    assert picker.field.input_value() == HANZI, "a failed switch must not eat the search"
    assert picker.posts == [{"id": "anthropic:cloud"}], "one attempt, one request"

    picker.field.focus()
    page.keyboard.press("Enter")                     # the same model, deliberately, once more
    expect(picker.overlay).not_to_be_visible()
    assert picker.posts == [{"id": "anthropic:cloud"}] * 2, "the retry must not double-POST"


# ------------------------------- the other ordering, and the edge of the guard

LEGACY = """() => {
  const el = document.querySelector('#modelSearch');
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


def test_a_confirming_enter_that_arrives_after_compositionend_is_still_not_a_switch(picker):
    """Explicitly synthetic: this event sequence is not reachable through Chromium's own IME."""
    page = picker.page
    picker.field.fill(PINYIN)
    if page.evaluate(LEGACY) == "unsupported":
        pytest.skip("this browser does not carry the legacy keyCode on a synthetic keydown")
    page.wait_for_timeout(250)

    assert picker.posts == [], "a keyCode 229 Enter is the keyboard confirming, not a model switch"
    expect(picker.overlay).to_be_visible()
    assert picker.field.input_value() == PINYIN

    picker.field.fill(HANZI)                         # the person then switches, deliberately
    picker.field.focus()
    page.keyboard.press("Enter")
    expect(picker.overlay).not_to_be_visible()
    assert picker.posts == [{"id": "anthropic:cloud"}], "the guard must not outlive the composition"


def test_escape_on_another_control_of_the_picker_still_closes_it(picker):
    """The guard stops at the search box: nothing else in this dialog can be composing.

    Synthetic on purpose — a real composition ends when focus leaves the field, so this is the
    stale-state case, checked so the guard cannot quietly disable Escape for the rest of the dialog.
    """
    page = picker.page
    page.evaluate("""() => { const el = document.querySelector('#modelSearch'); el.focus();
                            el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
                            document.getElementById('modelClose').focus(); }""")

    page.keyboard.press("Escape")
    expect(picker.overlay).not_to_be_visible()
    assert picker.posts == []
