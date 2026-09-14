"""What the run setup says about the model, after a Brains & routing save in Settings.

Settings is the other control that changes which model runs: the Provider select and the Model
field write `PROVIDER`/`MODEL` through `/api/settings`, and the acknowledgement reports the values
that are now in force. `/api/run-capabilities` is read per provider/model — which speed tiers that
route reports, which reasoning efforts it accepts, which worker is its default, and the sentence
under the Fast item that explains why Fast is or is not offered — so the answer the page is holding
describes *a* pair, and it stops describing anything the moment a different pair is saved.

The picker's `selectModel` and first-run onboarding both adopt an accepted save through
`adoptCurrentModel`, which re-reads those capabilities. An accepted Settings save only reloaded the
model *catalog*, so the run setup kept the previous pair's tiers, efforts, default worker and Fast
sentence: someone who moved from a route without a Fast tier to one that reports it saw no Fast
item, and someone moving the other way was still offered one. Reopening the picker or reloading the
page fixed it — the manual step the save is supposed to make unnecessary.

The saves that change no model may not claim a new pair's capabilities either: a refusal, and the
acknowledgement that reports an environment-held value the panel could not overwrite. And a
capability read is not instantaneous — the page starts one at load — so an answer that was asked
for while the previous pair was current may not land on top of the pair now in force.

Everything here drives the real `harness/webui/index.html` in Chromium against the staged HTTP
fixture shared with test_web_ui_run_status, with `/api/settings`, `/api/models` and
`/api/run-capabilities` answered by Playwright interception over an invented settings schema and the
invented catalog rows shared with test_web_model_picker_locale. The staged backend keeps its own
settings values, merges an accepted POST into them, and answers both the catalog's `current` and the
capability read from *those* values, which is the real server's contract. No provider, account, key,
saved setting or live discovery is touched: `ollama:llama-local` is an invented row and the tiers
below are staged descriptions, not claims about Ollama. Nothing waits by sleeping — when an answer
that must change nothing is released, the test then makes a *later* request over a different
endpoint and waits for its visible result, so the released answer is known to have arrived first.
"""
import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser   # noqa: F401
from test_web_model_picker_locale import ENTRIES

# The two knobs under Brains & routing, rendered from the schema the panel is given, exactly as the
# product's own schema types them: the provider is a select, the model is a free-text lock.
SCHEMA = [
    {"group": "Model", "key": "PROVIDER", "label": "Provider", "type": "select",
     "default": "anthropic",
     "options": [{"value": "anthropic", "label": "Anthropic"},
                 {"value": "ollama", "label": "Ollama (local)"}]},
    {"group": "Model", "key": "MODEL", "label": "Model", "type": "text", "default": "",
     "hint": "Optional exact model lock."},
    {"group": "General", "key": "LANG", "label": "Language", "type": "select", "default": "en",
     "options": [{"value": "en", "label": "English"}]},
]
START = {"LANG": "en", "PROVIDER": "anthropic", "MODEL": "mock"}

# Staged descriptions of three endpoints, keyed by the pair that is current when the read is made.
BOOTED = "Capabilities of the pair this page booted on"
BY_PROVIDER = "Capabilities of the provider you selected in Settings"
BY_MODEL = "Capabilities of the model you typed in Settings"
STALE_WORKER = "worker-from-the-older-read"      # only ever sent in an answer that must not apply


def caps(note, tiers=("standard",), worker="collie", efforts=()):
    return {"speed_tiers": list(tiers), "interactive_speed_default": "standard",
            "reasoning_efforts": list(efforts), "worker_default": worker, "workers": [],
            "fast_note": note}


BEFORE = caps(BOOTED)
PROVIDER_CAPS = caps(BY_PROVIDER, tiers=("standard", "fast"), worker="local-worker",
                     efforts=("low", "medium", "high"))
MODEL_CAPS = caps(BY_MODEL, tiers=("standard", "fast"), worker="thinking-worker",
                  efforts=("low", "medium", "high"))
CAPABILITIES = {"anthropic:mock": BEFORE,          # what the page boots on
                "ollama:mock": PROVIDER_CAPS,      # the provider save, model lock left alone
                "anthropic:claude-thinker": MODEL_CAPS}   # the model save, provider left alone


class Settings:
    """One configured page whose settings, catalog and capability endpoints are staged."""

    def __init__(self, ui):
        self.page = ui.page
        self.values = dict(START)     # what the staged backend has actually saved
        self.pinned = {}              # key -> value an environment variable holds against the panel
        self.posts = []               # every /api/settings body the page POSTed, in order
        self.reads = []               # every /api/run-capabilities read, in order
        self.held = []                # reads caught before they may answer
        self.hold_first = False       # catch the read the page starts at load
        self.refuse = None            # (status, body) for the next save, if set
        self.entries = list(ENTRIES)

    # -- staged endpoints ---------------------------------------------------
    def current(self):
        return "%s:%s" % (self.values.get("PROVIDER", ""), self.values.get("MODEL", ""))

    def _caps(self, route):
        self.reads.append(self.current())
        if self.hold_first and len(self.reads) == 1:
            self.held.append(route)
            return
        route.fulfill(json=CAPABILITIES.get(self.current(), BEFORE))

    def _catalog(self, route):
        route.fulfill(json={"current": self.current(), "entries": self.entries})

    def _settings(self, route):
        if route.request.method != "POST":
            route.fulfill(json={"schema": SCHEMA, "values": dict(self.values)})
            return
        body = route.request.post_data_json or {}
        self.posts.append(body)
        if self.refuse is not None:
            status, payload = self.refuse
            self.refuse = None
            route.fulfill(status=status, json=payload)
            return
        # The real endpoint merges the partial write into what it holds and reports every effective
        # value back, so a knob an environment variable outranks comes back unchanged.
        saved = {}
        for key, value in body.items():
            if key in self.pinned:
                continue
            self.values[key] = value
            saved[key] = value
        route.fulfill(json={"ok": True, "values": dict(self.values), "saved": saved})

    def open(self):
        page = self.page
        page.route("**/api/run-capabilities", self._caps)
        page.route("**/api/models*", self._catalog)
        page.route("**/api/settings*", self._settings)
        page.goto(page.url, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        if not self.hold_first:
            expect(self.note).to_have_text(BOOTED)   # the pair the page booted on, as read
        assert not page.is_visible("#obOverlay.open"), "the staged machine is configured"
        page.evaluate("() => { window.__stillTheSamePage = true; }")
        return self

    def release(self, answer):
        """Answer the load-time read that a later save has already overtaken."""
        self.held.pop(0).fulfill(json=answer)
        return self

    def barrier(self, name):
        """Make a later request over another endpoint and wait for what it draws.

        Anything released before this returns reached the page ahead of the catalog answer now on
        screen, so "it changed nothing" is an observation rather than a guess.
        """
        sentinel = {"provider": "ollama", "model": name, "id": "ollama:" + name, "label": name,
                    "auth": "ok", "kind": "local", "via": "local"}
        self.entries = list(ENTRIES) + [sentinel]
        self.page.keyboard.press("Control+k")
        expect(self.page.locator("#modelOverlay")).to_be_visible()
        expect(self.page.locator('.model-option[data-model-id="%s"]' % sentinel["id"])).to_have_count(1)
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#modelOverlay")).not_to_be_visible()
        return self

    # -- what a person sees -------------------------------------------------
    @property
    def note(self):
        return self.page.locator("#runFastHelp")

    @property
    def worker_desc(self):
        return self.page.locator("#savedWorkerDesc")

    @property
    def status(self):
        return self.page.locator("#setStatus")

    @property
    def trigger(self):
        return self.page.locator("#modelTriggerLabel")

    def fast_offered(self):
        return self.page.evaluate("() => !document.getElementById('runFastItem').hidden")

    def high_effort_offered(self):
        return self.page.evaluate(
            "() => !document.querySelector('.mode-item[data-axis=\"effort\"][data-val=\"high\"]').disabled")

    def reloaded(self):
        return not self.page.evaluate("() => !!window.__stillTheSamePage")

    # -- what a person does -------------------------------------------------
    def open_panel(self):
        page = self.page
        page.click("#settingsBtn")
        expect(page.locator("#setOverlay.open")).to_have_count(1)
        page.click('.set-nav[data-cat="brains"]')
        expect(page.locator("#set_PROVIDER")).to_be_visible()
        return self

    def close_panel(self):
        self.page.click("#setClose")
        expect(self.page.locator("#setOverlay.open")).to_have_count(0)
        return self

    def choose_provider(self, provider):
        self.page.select_option("#set_PROVIDER", provider)
        return self

    def type_model(self, model):
        """Write the model lock; a text setting commits on its change, or on its own debounce."""
        self.page.fill("#set_MODEL", model)
        return self


def test_saving_a_provider_in_settings_shows_the_capabilities_of_what_it_saved(ui):
    """The bug: an accepted Provider save left the previous route's capabilities on screen."""
    s = Settings(ui).open()
    ui.page.fill("#input", "a half-written message")
    assert not s.fast_offered() and not s.high_effort_offered()
    s.open_panel()
    before_reads = len(s.reads)

    s.choose_provider("ollama")
    expect(s.status).to_have_class("set-status ok")

    expect(s.note).to_have_text(BY_PROVIDER)               # the sentence under Fast is per-pair…
    expect(s.worker_desc).to_contain_text("local-worker")  # …and so is the default worker beside it
    assert s.fast_offered()                                # the tier this route reports is offered
    assert s.high_effort_offered()                         # and the efforts it accepts
    assert s.reads[before_reads:] == ["ollama:mock"], s.reads   # asked once, about the saved pair
    assert s.posts == [{"PROVIDER": "ollama"}], s.posts    # and written exactly once
    assert ui.page.input_value("#input") == "a half-written message"
    assert not s.reloaded()


def test_saving_a_model_lock_in_settings_refreshes_the_same_capabilities(ui):
    """The other knob on the same pane, committed the way a text setting commits: on leaving it."""
    s = Settings(ui).open()
    s.open_panel()
    before_reads = len(s.reads)

    s.type_model("claude-thinker")
    expect(s.status).to_have_class("set-status ok")

    expect(s.note).to_have_text(BY_MODEL)
    expect(s.worker_desc).to_contain_text("thinking-worker")
    assert s.fast_offered() and s.high_effort_offered()
    assert s.reads[before_reads:] == ["anthropic:claude-thinker"], s.reads
    assert s.posts == [{"MODEL": "claude-thinker"}], s.posts
    expect(s.trigger).to_have_text("claude-thinker")       # the pair now in force, in the trigger
    assert not s.reloaded()


def test_a_refused_settings_save_neither_adopts_nor_re_reads_capabilities(ui):
    """A save the backend turned down changes no model, so it may not claim its capabilities."""
    s = Settings(ui).open()
    s.open_panel()
    before_reads, before_trigger = len(s.reads), s.trigger.text_content()

    s.refuse = (500, {"ok": False, "error": "settings.json is read-only."})
    s.choose_provider("ollama")

    expect(s.status).to_have_text("settings.json is read-only.")
    expect(s.note).to_have_text(BOOTED)                    # still describing the pair in force
    assert not s.fast_offered() and not s.high_effort_offered()
    assert s.reads[before_reads:] == [], s.reads           # nothing re-read off a failed save
    expect(s.trigger).to_have_text(before_trigger)
    assert s.posts == [{"PROVIDER": "ollama"}], s.posts


def test_an_environment_held_provider_is_reported_and_never_adopted(ui):
    """COLLIE_PROVIDER outranks the panel: the acknowledgement says so, and nothing may pretend."""
    s = Settings(ui).open()
    s.pinned["PROVIDER"] = "anthropic"                     # what the environment is holding
    s.open_panel()
    before_reads, before_trigger = len(s.reads), s.trigger.text_content()

    s.choose_provider("ollama")

    expect(s.status).to_contain_text("environment variable")
    expect(s.status).to_contain_text("PROVIDER = anthropic")
    expect(s.note).to_have_text(BOOTED)                    # the pair that is really in force
    assert not s.fast_offered() and not s.high_effort_offered()
    assert s.reads[before_reads:] == [], s.reads
    expect(s.trigger).to_have_text(before_trigger)
    assert s.posts == [{"PROVIDER": "ollama"}], s.posts


def test_an_older_capability_answer_does_not_overwrite_the_one_the_save_asked_for(ui):
    """The read that was already in flight describes the pair that used to be current."""
    s = Settings(ui)
    s.hold_first = True
    s.open()
    assert len(s.held) == 1, s.reads

    s.open_panel()
    s.choose_provider("ollama")
    expect(s.status).to_have_class("set-status ok")
    expect(s.note).to_have_text(BY_PROVIDER)
    s.close_panel()

    # …and now the load-time read finally answers, about the pair nobody is on any more.
    s.release(caps(BOOTED, worker=STALE_WORKER))
    s.barrier("after-the-older-answer")

    expect(s.note).to_have_text(BY_PROVIDER)
    expect(s.worker_desc).not_to_contain_text(STALE_WORKER)
    assert s.fast_offered() and s.high_effort_offered()
    assert s.posts == [{"PROVIDER": "ollama"}], s.posts
