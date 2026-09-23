"""The model picker has to be readable in the language the page is already speaking.

On a page whose `html lang` is `zh` — Chinese sidebar, Chinese composer, Chinese run controls — the
one dialog that decides which model the next message runs on still said "Choose a model", "Switches
immediately and applies to your next message", "Discover live", "METERED API", "Billed by the
connected provider", "Ready", "Price unavailable", "2 models available" and "↑ ↓ to browse · Enter
to select", with an English search placeholder.  Only the synthetic Auto row was translated.  So the
capability, the billing route and the price of every model — the facts the choice is actually made
on — were unreadable to the person making it.

What is translated is the page's own words.  A provider name, a model id, a tag and anything the
backend wrote (a refusal reason, a provider error) are identifiers and are shown exactly as they
arrived in every language: these tests assert the numbers and names survive the translation, and
that a backend detail is never replaced by a generic translated sentence.

The late-language case is the hard one.  `/api/settings` answers over the network, so the language
normally lands *after* the picker has drawn its rows — and the picker may be open with a search
half-typed.  Relabelling may not cost the person anything: these tests hold the settings answer
back, open the picker, type, move the caret, walk the highlight, tick live discovery, and then
demand that the labels change while the text, the caret, the focus, the highlight, the checkbox and
the current selection do not — and that no catalog refetch and no `/api/model` POST left the page.

Everything here drives the real harness/webui/index.html in Chromium against the staged HTTP fixture
shared with test_web_ui_run_status, with the catalog and the model-change endpoint answered by
Playwright interception over invented entries — no provider is contacted, no key, account or setting
is touched, and no live discovery reaches anything.  The composition test uses Chromium's own CDP
IME path (`Input.imeSetComposition`), which is not a physical OS IME and says nothing about Safari,
Firefox or a real keyboard driver.
"""
import re

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser   # noqa: F401
from test_web_queue_locale import hold_settings

# An invented catalog that covers every label the picker draws from an entry: all three groups, an
# authenticated and an unauthenticated route, a priced model, an unpriced one, a local one and a
# plan login whose price is deliberately not guessed.
ENTRIES = [
    {"provider": "anthropic-oauth", "model": "plan-sonnet", "id": "anthropic-oauth:plan-sonnet",
     "label": "Plan Sonnet", "auth": "ok", "kind": "subscription", "via": "plan login"},
    {"provider": "anthropic", "model": "mock", "id": "anthropic:mock", "label": "Mock",
     "auth": "ok", "kind": "metered", "via": "api", "price_in": 3, "price_out": 15},
    {"provider": "openai", "model": "keyless", "id": "openai:keyless", "label": "Keyless Metered",
     "auth": "missing-key", "kind": "metered", "via": "api"},
    {"provider": "ollama", "model": "llama-local", "id": "ollama:llama-local", "label": "Local Llama",
     "auth": "ok", "kind": "local", "via": "local"},
]
CURRENT = "anthropic:mock"
OPTION_COUNT = len(ENTRIES) + 1          # the page adds the synthetic Auto row for the current provider

# (LANG, title, caption, placeholder, close, discovery, list, footer, auto row, "共 N 个模型可用")
LOCALES = {
    "zh": {
        "title": "选择模型",
        "caption": "立即切换，并从你的下一条消息开始生效。",
        "placeholder": "搜索模型、提供方或能力",
        "close": "关闭模型选择器",
        "discover": "实时发现",
        "list": "可用模型",
        "foot": "↑ ↓ 浏览 · Enter 选择",
        "groups": ["登录账号", "按量计费 API", "本地"],
        "captions": ["可用性与计费取决于所选路由", "由已连接的提供方计费", "在这台电脑上运行"],
        "ready": "就绪",
        "needs_key": "需要 API key",
        "no_price": "价格不可用",
        "per_m": "$3.00/$15.00 每百万 token",
        "local_price": "本地 · $0",
        "plan_price": "订阅登录 · 计费未验证",
        "count": "共 5 个模型可用",
        "one_match": "1 个匹配的模型", "many_match": "5 个匹配的模型",
        "auto": "自动 — Collie 按任务选择",
        "trigger": "当前模型 Mock。切换模型。",
        "switched": "已切换 · 下一条消息生效",
        "no_match": "没有匹配该搜索的模型。",
    },
    "zh-tw": {
        "title": "選擇模型",
        "caption": "立即切換，並從你的下一則訊息開始生效。",
        "placeholder": "搜尋模型、供應商或能力",
        "close": "關閉模型選擇器",
        "discover": "即時探索",
        "list": "可用模型",
        "foot": "↑ ↓ 瀏覽 · Enter 選取",
        "groups": ["登入帳號", "按量計費 API", "本機"],
        "captions": ["可用性與計費取決於所選路由", "由已連線的供應商計費", "在這台電腦上執行"],
        "ready": "就緒",
        "needs_key": "需要 API key",
        "no_price": "價格無法取得",
        "per_m": "$3.00/$15.00 每百萬 token",
        "local_price": "本機 · $0",
        "plan_price": "訂閱登入 · 計費未驗證",
        "count": "共 5 個模型可用",
        "one_match": "1 個相符的模型", "many_match": "5 個相符的模型",
        "auto": "自動 — Collie 依任務選擇",
        "trigger": "目前模型 Mock。切換模型。",
        "switched": "已切換 · 下一則訊息生效",
        "no_match": "沒有符合該搜尋的模型。",
    },
}
ENGLISH = {
    "title": "Choose a model",
    "caption": "Switches immediately and applies to your next message.",
    "placeholder": "Search models, providers, or capabilities",
    "close": "close model picker",
    "discover": "Discover live",
    "list": "Available models",
    "foot": "↑ ↓ to browse · Enter to select",
    "groups": ["Login-backed", "Metered API", "Local"],
    "captions": ["Availability and billing depend on the selected route",
                 "Billed by the connected provider", "Runs on this machine"],
    "ready": "Ready",
    "needs_key": "API key required",
    "no_price": "Price unavailable",
    "per_m": "$3.00/$15.00 per M",
    "local_price": "Local · $0",
    "plan_price": "Plan login · billing unverified",
    "count": "5 models available",
    "one_match": "1 matching model",
    "many_match": "5 matching models",
    "auto": "Auto — Collie chooses per task",
    "trigger": "Current model Mock. Switch model.",
    "switched": "Switched · applies to your next message",
    "no_match": "No models match that search.",
}

# What the status line says while a catalog read is in flight, in every language the picker speaks.
# Opening redraws the rows from the catalog the page already holds and only *then* asks for a fresh
# one, so the overlay, the five rows and the focused field are all in place while the count is still
# one of these sentences — a one-shot read of the count could land there instead of on the count.
# Leaving this set is the state the count belongs to, and over an unchanged list it is the only thing
# the answer changes, so it is also the only signal that the answer has landed and been drawn.
IN_FLIGHT = re.compile("|".join(re.escape(sentence) for sentence in [
    "Loading available models…", "Discovering authenticated providers…",
    "正在加载可用模型…", "正在发现已登录的提供方…",
    "正在載入可用模型…", "正在探索已登入的供應商…"]))


class Picker:
    """The picker over an invented catalog, plus every model change and catalog read it caused."""

    def __init__(self, ui):
        self.ui = ui
        self.page = ui.page
        self.posts = []            # every /api/model body the page POSTed, in order
        self.catalog_reads = []    # every /api/models URL the page asked for, in order
        self.refuse = None         # (status, body) to answer the next switch with, if set

    def _catalog(self, route):
        self.catalog_reads.append(route.request.url)
        route.fulfill(json={"current": CURRENT, "entries": ENTRIES})

    def _change(self, route):
        self.posts.append(route.request.post_data_json)
        if self.refuse is not None:
            status, payload = self.refuse
            self.refuse = None
            route.fulfill(status=status, json=payload)
            return
        body = route.request.post_data_json
        route.fulfill(json={"ok": True, "provider": "anthropic", "model": body.get("id", ":mock").split(":", 1)[1]})

    def install(self):
        self.page.route("**/api/models*", self._catalog)
        self.page.route("**/api/model?*", self._change)
        return self

    def reload(self):
        self.page.reload(wait_until="load")
        self.page.wait_for_selector("#input", timeout=8000)
        return self

    def open(self):
        self.page.keyboard.press("Control+k")
        expect(self.overlay).to_be_visible()
        expect(self.page.locator(".model-option")).to_have_count(OPTION_COUNT)
        expect(self.field).to_be_focused()
        return self.settled()

    def settled(self):
        """Wait until no catalog read is still in flight, so the count speaks for an answered one.

        The rows are no barrier: opening draws them from the catalog the page already has, before
        the read that opening itself causes has answered.
        """
        expect(self.status).not_to_have_text(IN_FLIGHT)
        return self

    @property
    def overlay(self):
        return self.page.locator("#modelOverlay")

    @property
    def field(self):
        return self.page.locator("#modelSearch")

    @property
    def status(self):
        return self.page.locator("#modelStatus")

    def option(self, model_id):
        return self.page.locator('.model-option[data-model-id="%s"]' % model_id)

    def row_text(self, model_id):
        return self.option(model_id).inner_text()

    def labels(self):
        return self.page.eval_on_selector_all(
            ".model-option", "els => els.map(el => el.querySelector('.model-option-label').textContent)")

    def active(self):
        """The highlighted option, as the list also reports it to a screen reader."""
        return self.page.evaluate("""() => {
          const on = document.querySelector('.model-option.is-keyboard-active');
          return {id: on ? on.dataset.modelId : '',
                  described: document.getElementById('modelList').getAttribute('aria-activedescendant')};
        }""")

    def chrome(self):
        """Everything the dialog says about itself, read the way a person or a reader sees it."""
        page = self.page
        return {
            "title": page.inner_text("#modelTitle"),
            "caption": page.inner_text("#modelCaption"),
            "placeholder": page.get_attribute("#modelSearch", "placeholder"),
            "close": page.get_attribute("#modelClose", "aria-label"),
            "close_title": page.get_attribute("#modelClose", "title"),
            "discover": page.inner_text(".model-discover"),
            "list": page.get_attribute("#modelList", "aria-label"),
            "foot": page.inner_text(".model-foot-note"),
            "groups": page.eval_on_selector_all(
                ".model-group-title", "els => els.map(el => el.firstChild.textContent.trim())"),
            "captions": page.eval_on_selector_all(
                ".model-group-title span", "els => els.map(el => el.textContent)"),
            "status": page.inner_text("#modelStatus"),
            "trigger": page.get_attribute("#modelTrigger", "aria-label"),
        }


@pytest.fixture
def picker(ui):                                              # noqa: F811
    return Picker(ui).install()


def in_language(picker, lang):
    """A normal load whose saved language is `lang` — settings still answer over the network."""
    picker.page.route("**/api/settings**", lambda route: _with_lang(route, lang))
    return picker.reload().open()


def _with_lang(route, lang):
    response = route.fetch()
    data = response.json()
    data.setdefault("values", {})["LANG"] = lang
    route.fulfill(response=response, json=data)


def watch_requests(page):
    seen = []
    page.on("request", lambda request: seen.append(request.method + " " + request.url))
    return seen


# ------------------------------------------------------------------ what the dialog says

@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_the_whole_picker_speaks_the_saved_language(picker, lang):
    words = LOCALES[lang]
    in_language(picker, lang)
    page = picker.page
    expect(page.locator("html")).to_have_attribute("lang", lang)

    chrome = picker.chrome()
    assert chrome["title"] == words["title"]
    assert chrome["caption"] == words["caption"]
    assert chrome["placeholder"] == words["placeholder"]
    assert chrome["close"] == words["close"] and chrome["close_title"] == words["close"]
    assert words["discover"] in chrome["discover"]
    assert chrome["list"] == words["list"]
    assert chrome["foot"] == words["foot"]
    assert chrome["groups"] == words["groups"], chrome["groups"]
    assert chrome["captions"] == words["captions"], chrome["captions"]
    assert chrome["status"] == words["count"], chrome["status"]
    assert chrome["trigger"] == words["trigger"]

    # Capability, billing route and price — the facts the choice is made on.
    assert words["ready"] in picker.row_text("anthropic:mock")
    assert words["per_m"] in picker.row_text("anthropic:mock")
    assert words["needs_key"] in picker.row_text("openai:keyless")
    assert words["no_price"] in picker.row_text("openai:keyless")
    assert words["local_price"] in picker.row_text("ollama:llama-local")
    assert words["plan_price"] in picker.row_text("anthropic-oauth:plan-sonnet")
    assert words["auto"] in picker.row_text("anthropic:")

    # No English is left anywhere the page wrote a sentence of its own.
    visible = page.inner_text("#modelOverlay")
    for leftover in ["Choose a model", "Switches immediately", "Discover live", "Metered API",
                     "Billed by the connected provider", "Ready", "Price unavailable",
                     "models available", "to browse", "Runs on this machine", "Login-backed"]:
        assert leftover not in visible, leftover + " is still English in " + lang


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_provider_and_model_identifiers_are_never_translated(picker, lang):
    in_language(picker, lang)
    # The names the backend gave, the ids, the routes and the numbers all read the same as in English.
    # Group order, then catalog order: the synthetic Auto row joins the group of the current route.
    assert picker.labels() == ["Plan Sonnet", LOCALES[lang]["auto"], "Mock", "Keyless Metered", "Local Llama"]
    assert "api · mock" in picker.row_text("anthropic:mock")
    assert "local · llama-local" in picker.row_text("ollama:llama-local")
    assert "plan login · plan-sonnet" in picker.row_text("anthropic-oauth:plan-sonnet")
    assert picker.page.text_content("#modelTriggerLabel") == "Mock"


def test_english_keeps_the_words_it_had(picker):
    in_language(picker, "en")
    chrome = picker.chrome()
    for key in ["title", "caption", "placeholder", "close", "discover", "list", "foot",
                "groups", "captions", "trigger"]:
        assert chrome[key] == ENGLISH[key] or (key == "discover" and ENGLISH[key] in chrome[key]), key
    assert chrome["status"] == ENGLISH["count"]
    assert ENGLISH["ready"] in picker.row_text("anthropic:mock")
    assert ENGLISH["per_m"] in picker.row_text("anthropic:mock")
    assert ENGLISH["no_price"] in picker.row_text("openai:keyless")


@pytest.mark.parametrize("lang,expected", [("en", ENGLISH), ("zh", LOCALES["zh"]),
                                           ("zh-tw", LOCALES["zh-tw"])])
def test_counts_read_naturally_for_one_and_for_many(picker, lang, expected):
    in_language(picker, lang)
    assert picker.status.inner_text() == expected["count"]
    picker.field.fill("mock")                      # exactly one entry matches
    expect(picker.page.locator(".model-option")).to_have_count(1)
    assert picker.status.inner_text() == expected["one_match"], "a single model is not '1 models'"
    picker.field.fill("no-such-model")
    assert expected["no_match"] in picker.page.inner_text("#modelList")


# ------------------------------------------------------------------ a language that lands late

def prepare_open_picker(picker, query="o", caret=1):
    """The picker open, mid-search, with live discovery ticked and the highlight moved."""
    page = picker.page
    release = hold_settings(page)
    picker.reload().open()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    assert picker.chrome()["title"] == "Choose a model", "the fixture must actually hold the language"

    # Ticking live discovery reloads the catalog (intercepted — no provider is contacted). That
    # answer redraws the list on its own, so it has to land *before* the highlight is placed, or the
    # test would be reading a race rather than the effect of the language. Waiting for the answer
    # itself — the response, then the status line it settles — says that it has, which a wait for
    # rows that were already on screen cannot.
    with page.expect_response("**/api/models*"):
        page.check("#modelDiscover")
    assert "discover=1" in picker.catalog_reads[-1], picker.catalog_reads
    expect(page.locator(".model-option")).to_have_count(OPTION_COUNT)
    picker.settled()
    picker.field.fill(query)
    page.keyboard.press("ArrowDown")
    page.evaluate("""caret => { const el = document.getElementById('modelSearch');
      el.setSelectionRange(caret, caret); el.focus(); }""", caret)
    return release


def field_state(page):
    return page.evaluate("""() => {
      const el = document.getElementById('modelSearch');
      return {value: el.value, start: el.selectionStart, end: el.selectionEnd,
              focused: document.activeElement === el, editable: !el.disabled && !el.readOnly};
    }""")


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_a_late_language_relabels_the_open_picker_without_touching_the_search(picker, lang):
    words = LOCALES[lang]
    page = picker.page
    release = prepare_open_picker(picker)
    before_field, before_active = field_state(page), picker.active()
    before_labels = picker.labels()
    reads, requests = len(picker.catalog_reads), watch_requests(page)

    release(lang)
    expect(page.locator("html")).to_have_attribute("lang", lang)
    expect(page.locator("#modelTitle")).to_have_text(words["title"])
    page.wait_for_timeout(400)

    # The labels moved language…
    chrome = picker.chrome()
    assert chrome["caption"] == words["caption"]
    assert chrome["placeholder"] == words["placeholder"]
    assert chrome["close"] == words["close"]
    assert chrome["foot"] == words["foot"]
    assert words["discover"] in chrome["discover"]
    assert chrome["groups"] == words["groups"]
    assert words["ready"] in picker.row_text("anthropic:mock")
    assert words["no_price"] in picker.row_text("openai:keyless")
    assert chrome["status"] == words["many_match"], "the count follows the search and the language"
    assert words["auto"] in picker.row_text("anthropic:")

    # …and nothing the person was in the middle of did.
    assert field_state(page) == before_field, "the search text, caret or focus was disturbed"
    assert picker.active() == before_active, "the keyboard highlight moved"
    after_labels = picker.labels()
    assert len(after_labels) == len(before_labels), "rows appeared or vanished"
    assert ([label for label in after_labels if label != words["auto"]]
            == [label for label in before_labels if label != ENGLISH["auto"]]), "the rows lost their identity"
    assert page.is_checked("#modelDiscover"), "live discovery was un-ticked by a language"
    expect(picker.overlay).to_be_visible()

    # Nothing was refetched, discovered, selected or posted because a language turned up.
    assert picker.posts == [], picker.posts
    assert len(picker.catalog_reads) == reads, picker.catalog_reads
    assert not [row for row in requests if "/api/model" in row], requests
    assert not [row for row in requests if not row.startswith("GET ")], requests


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_the_keys_still_do_what_they_say_after_a_late_language(picker, lang):
    page = picker.page
    release = prepare_open_picker(picker)
    release(lang)
    expect(page.locator("#modelTitle")).to_have_text(LOCALES[lang]["title"])

    # Escape is still an ordinary Escape…
    page.keyboard.press("Escape")
    expect(picker.overlay).not_to_be_visible()
    assert picker.posts == []

    # …and the next deliberate Enter still switches, in the new language, to the highlighted model.
    picker.open()
    page.keyboard.press("ArrowDown")
    chosen = picker.active()["id"]
    page.keyboard.press("Enter")
    expect(picker.status).to_have_text(LOCALES[lang]["switched"])
    assert len(picker.posts) == 1 and picker.posts[0].get("id", "") in (chosen, ""), picker.posts
    expect(picker.overlay).not_to_be_visible()


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_a_late_language_does_not_break_an_open_composition(picker, lang):
    """The one state no snapshot can carry: an IME composition lives on the live input node."""
    page = picker.page
    release = prepare_open_picker(picker, query="", caret=0)
    picker.field.fill("")
    picker.field.focus()
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.imeSetComposition", {"text": "yun", "selectionStart": 3, "selectionEnd": 3})
    expect(picker.field).to_have_value("yun")
    composing_node = page.evaluate("() => (window.__node = document.getElementById('modelSearch')) && true")

    release(lang)
    expect(page.locator("#modelTitle")).to_have_text(LOCALES[lang]["title"])
    page.wait_for_timeout(300)

    assert composing_node
    assert page.evaluate("() => window.__node === document.getElementById('modelSearch')"), \
        "the search input was replaced, which would throw the composition away"
    assert picker.field.input_value() == "yun"
    assert page.evaluate("() => document.activeElement === document.getElementById('modelSearch')")

    # The composing keys are still the keyboard's, and the candidate still commits into this field.
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    assert picker.posts == [], "a composing Enter switched the model after the language changed"
    expect(picker.overlay).to_be_visible()
    cdp.send("Input.insertText", {"text": "云"})
    expect(picker.field).to_have_value("云")
    assert LOCALES[lang]["no_match"] in page.inner_text("#modelList")


# ------------------------------------------------------------------ what the backend said

@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_a_backend_refusal_keeps_its_own_words_through_a_language_change(picker, lang):
    detail = "anthropic-oauth: plan-sonnet is not available on this plan"
    page = picker.page
    release = prepare_open_picker(picker, query="plan")
    picker.refuse = (403, {"ok": False, "error": detail})
    page.keyboard.press("Enter")
    expect(picker.status).to_have_text(detail)
    assert page.get_attribute("#modelStatus", "class").endswith("err")
    assert len(picker.posts) == 1, picker.posts

    release(lang)
    expect(page.locator("#modelTitle")).to_have_text(LOCALES[lang]["title"])
    page.wait_for_timeout(400)

    # The reason is the server's, not a sentence the page made up about it.
    assert picker.status.inner_text() == detail, "the backend reason was overwritten"
    assert page.get_attribute("#modelStatus", "class").endswith("err")
    assert picker.posts == [picker.posts[0]], "the refused switch was retried"


# ------------------------------------------------------------------ what the count counts

def test_the_count_is_never_a_catalog_read_that_has_not_answered(picker):
    """The count says how many models are available, so it may only speak for an answered read.

    Opening the picker redraws the rows from the catalog the page already holds and only then asks
    for a fresh one, so the whole dialog — overlay, five rows, focused search field — is in place
    while the new read is still in flight and the line where the count goes says the list is
    loading. A one-shot read of the count taken at that moment reads the loading sentence, which is
    what macOS CI caught (run 35799412332): the count itself was right, the read was early. Here
    the read that opening causes is held open, so that window is the test's to look at rather than
    the fixture's to decide, and the count has to wait for the answer.
    """
    page = picker.page
    page.route("**/api/settings**", lambda route: _with_lang(route, "en"))
    picker.reload()
    expect(page.locator(".model-option")).to_have_count(OPTION_COUNT)   # the load-time read has drawn

    held = []
    page.route("**/api/models*", lambda route: held.append(route))      # the open-time read is held
    page.keyboard.press("Control+k")
    expect(picker.overlay).to_be_visible()
    expect(page.locator(".model-option")).to_have_count(OPTION_COUNT)
    expect(picker.field).to_be_focused()
    assert held, "the fixture must actually hold the read that opening caused"
    mid_load = picker.status.inner_text()
    assert mid_load != ENGLISH["count"], "the count spoke for a read that had not answered"
    assert IN_FLIGHT.search(mid_load), \
        "the line says a read is in flight, and `settled()` waits for exactly that: " + mid_load

    for route in held[:]:
        route.fulfill(json={"current": CURRENT, "entries": ENTRIES})
        held.remove(route)
    picker.settled()
    assert picker.status.inner_text() == ENGLISH["count"]
    picker.field.fill("mock")                      # and the filtered count is the answer's too
    expect(page.locator(".model-option")).to_have_count(1)
    assert picker.status.inner_text() == ENGLISH["one_match"]


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_a_catalog_that_cannot_be_read_says_so_in_the_readers_language(picker, ui, lang):  # noqa: F811
    page = picker.page
    page.route("**/api/settings**", lambda route: _with_lang(route, lang))
    page.route("**/api/models*", lambda route: route.fulfill(status=503, json={"error": "down"}))
    picker.reload()
    page.keyboard.press("Control+k")
    expect(picker.overlay).to_be_visible()
    expect(picker.status).to_have_text({"zh": "无法加载模型列表。",
                                        "zh-tw": "無法載入模型清單。"}[lang])
    assert page.get_attribute("#modelStatus", "class").endswith("err")
