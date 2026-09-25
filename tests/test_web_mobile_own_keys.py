"""Words from the server index the phone's label tables; prototype names must not leak through.

The real mobile.html in Chromium (the fixture shared with test_web_ui_run_status). The page's own
functions are called with state words that exist on every JavaScript object -- ``constructor``,
``toString``, ``__proto__`` -- in English and Chinese. First-party state vocabularies never send
these; the point is that no future or hostile value can turn a label into ``function Object()``.
"""
import pytest

from test_web_ui_run_status import browser, phone, server  # noqa: F401  (pytest fixtures)

PROTOTYPE_WORDS = ["constructor", "toString", "__proto__", "hasOwnProperty", "valueOf"]


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_state_labels_and_translations_ignore_prototype_names(phone, lang):
    page = phone.page
    rows = page.evaluate("""([words, lang]) => {
        UI_LANG = lang;
        return words.map(w => ({
          word: w,
          label: String(mobileStateLabel(w)),
          t: String(t(w)),
          verb: String(mobileCheckRow({executed: true, command_passed: false, passed: false,
                                       freshness: w, exit_code: 1}).verb),
        }));
    }""", [PROTOTYPE_WORDS, lang])
    for row in rows:
        for key in ("label", "t", "verb"):
            assert "function" not in row[key] and "[object" not in row[key], row
    labels = {row["word"]: row["label"] for row in rows}
    assert labels["constructor"] == "constructor" and labels["toString"] == "toString"
    assert labels["__proto__"] == "  proto  "            # underscores read as spaces, as for any word


def test_known_states_are_unchanged(phone):
    page = phone.page
    assert page.evaluate("() => { UI_LANG = 'en'; return mobileStateLabel('needs_you'); }") == "Needs You"
    assert page.evaluate("() => mobileCheckRow({executed: true, command_passed: false, passed: false, "
                         "exit_code: 1}).verb") == "check failed"
