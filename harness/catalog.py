"""Model catalog — one flat list of runnable (provider, model) entries the UI picks from.

Replaces "choose a provider dropdown, then TYPE a model id" with a single choice. Each
entry is already bound to how it's reached (API key / OAuth subscription / local) and what
it costs, carrying a live auth badge — so a switch can never produce an invalid
(provider, model) pair, and you see up front whether you can actually run it.

Three sources, merged + deduped by (provider, model):
  1. STATIC  — curated presets per provider (always present, offline-safe)
  2. LIVE    — a provider's model endpoint queried with your CURRENT auth
               (codex-oauth /models, OpenAI-compatible /v1/models, ollama /api/tags, …)
  3. CUSTOM  — the user's openai-compat base_url + model (from settings)

Grounding: make_provider(provider, model) is unchanged — an entry just carries both.
Prices are registered into costs.PRICES so $/instance receipts are correct for every
catalog model (fixes the "no price for gpt-5.6-terra" $0 misprice).
"""
from __future__ import annotations
import json
import os
import shutil
import time
import urllib.request
from dataclasses import dataclass, field

from .providers import OPENAI_COMPAT_PRESETS

# provider -> (base_url, api-key env, default model). Reuse the preset table where it exists;
# add the non-compat providers so auth-probing + discovery have one source of truth.
_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY",
    "moonshot": "MOONSHOT_API_KEY", "zhipu": "ZHIPU_API_KEY",
    "qwen": "DASHSCOPE_API_KEY", "dashscope": "DASHSCOPE_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# ---- prices ($/1M tokens: input, cached_input, output) ------------------------------------
# Login-backed entries bill at the EQUIVALENT metered rate in receipts so budgets stay
# conservative.  That accounting must not be read as a billing claim: in particular,
# anthropic-oauth is an experimental raw direct route whose availability and billing are
# established only by runtime admission, not by this catalog.  Registered into costs.PRICES on
# import (idempotent).
PRICES = {
    # (input, cache-read, output) per 1M tokens. Opus 4.8 sat at the old 15/75 Opus-3-era rates,
    # which overstated every Opus receipt by 3x; both Opus 5 and 4.8 are 5/25.
    "claude-opus-5":    (5.0, 0.5, 25.0),
    "claude-opus-4-8":  (5.0, 0.5, 25.0),
    "claude-sonnet-5":  (3.0, 0.3, 15.0),
    "claude-haiku-4-5": (0.8, 0.08, 4.0),
    "claude-fable-5":   (15.0, 1.5, 75.0),
    "gpt-5.6-sol":      (5.0, 0.5, 30.0),
    "gpt-5.6-terra":    (2.5, 0.25, 15.0),
    "gpt-5.6-luna":     (1.0, 0.10, 6.0),
    "gpt-4o-mini":      (0.15, 0.075, 0.60),
    "deepseek-chat":    (0.27, 0.07, 1.10),
    "deepseek-reasoner": (0.55, 0.14, 2.19),
    "gemini-2.5-pro":   (1.25, 0.31, 10.0),
    "gemini-2.5-flash": (0.30, 0.075, 2.50),
    "llama-3.3-70b-versatile": (0.59, 0.0, 0.79),
    "glm-4-flash":      (0.0, 0.0, 0.0),
}


def _register_prices():
    from . import costs
    for k, v in PRICES.items():
        costs.PRICES.setdefault(k, v)


_register_prices()


@dataclass
class ModelEntry:
    provider: str
    model: str
    label: str
    via: str                    # human "how it's reached": Claude subscription / API key / local …
    kind: str                   # subscription | metered | local
    tags: list = field(default_factory=list)
    source: str = "static"      # static | live | custom
    price: tuple = (0.0, 0.0, 0.0)

    @property
    def id(self) -> str:
        return "%s:%s" % (self.provider, self.model)

    def to_dict(self, auth: str) -> dict:
        pin, _pc, pout = self.price
        return {"id": self.id, "provider": self.provider, "model": self.model,
                "label": self.label, "via": self.via, "kind": self.kind,
                "tags": self.tags, "source": self.source,
                "price_in": pin, "price_out": pout, "auth": auth}


# ---- STATIC curated catalog ---------------------------------------------------------------
def _static() -> list:
    P = PRICES
    return [
        # Claude direct is experimental; keep its route visibly distinct from the metered API.
        ModelEntry("anthropic-oauth", "claude-opus-5", "Claude Opus 5",
                   "Claude direct (experimental)", "subscription", ["coding", "frontier"], price=P["claude-opus-5"]),
        ModelEntry("anthropic-oauth", "claude-opus-4-8", "Claude Opus 4.8",
                   "Claude direct (experimental)", "subscription", ["coding", "frontier"], price=P["claude-opus-4-8"]),
        ModelEntry("anthropic-oauth", "claude-sonnet-5", "Claude Sonnet 5",
                   "Claude direct (experimental)", "subscription", ["coding", "fast"], price=P["claude-sonnet-5"]),
        ModelEntry("anthropic", "claude-opus-5", "Claude Opus 5",
                   "Anthropic API key", "metered", ["coding", "frontier"], price=P["claude-opus-5"]),
        ModelEntry("anthropic", "claude-opus-4-8", "Claude Opus 4.8",
                   "Anthropic API key", "metered", ["coding", "frontier"], price=P["claude-opus-4-8"]),
        ModelEntry("anthropic", "claude-sonnet-5", "Claude Sonnet 5",
                   "Anthropic API key", "metered", ["coding", "fast"], price=P["claude-sonnet-5"]),
        ModelEntry("anthropic", "claude-haiku-4-5-20251001", "Claude Haiku 4.5",
                   "Anthropic API key", "metered", ["fast", "cheap"], price=P["claude-haiku-4-5"]),
        # GPT-5.6 via ChatGPT Codex subscription ($0 marginal) then metered OpenAI API.
        ModelEntry("codex-oauth", "gpt-5.6-terra", "GPT-5.6 Terra",
                   "ChatGPT subscription", "subscription", ["coding", "reasoning"], price=P["gpt-5.6-terra"]),
        ModelEntry("codex-oauth", "gpt-5.6-sol", "GPT-5.6 Sol",
                   "ChatGPT subscription", "subscription", ["coding", "frontier"], price=P["gpt-5.6-sol"]),
        ModelEntry("codex-oauth", "gpt-5.6-luna", "GPT-5.6 Luna",
                   "ChatGPT subscription", "subscription", ["fast", "cheap"], price=P["gpt-5.6-luna"]),
        ModelEntry("openai", "gpt-5.6-terra", "GPT-5.6 Terra",
                   "OpenAI API key", "metered", ["coding", "reasoning"], price=P["gpt-5.6-terra"]),
        ModelEntry("openai", "gpt-4o-mini", "GPT-4o mini",
                   "OpenAI API key", "metered", ["fast", "cheap"], price=P["gpt-4o-mini"]),
        # DeepSeek — the cheap strong default.
        ModelEntry("deepseek", "deepseek-chat", "DeepSeek Chat",
                   "DeepSeek API key", "metered", ["coding", "cheap"], price=P["deepseek-chat"]),
        ModelEntry("deepseek", "deepseek-reasoner", "DeepSeek Reasoner",
                   "DeepSeek API key", "metered", ["reasoning"], price=P["deepseek-reasoner"]),
        # Others.
        ModelEntry("gemini", "gemini-2.5-pro", "Gemini 2.5 Pro",
                   "Google API key", "metered", ["frontier"], price=P["gemini-2.5-pro"]),
        ModelEntry("gemini", "gemini-2.5-flash", "Gemini 2.5 Flash",
                   "Google API key", "metered", ["fast", "cheap"], price=P["gemini-2.5-flash"]),
        ModelEntry("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B (Groq)",
                   "Groq API key", "metered", ["fast"], price=P["llama-3.3-70b-versatile"]),
        # OpenRouter — one key, hundreds of models (turn on Discover to list them all).
        ModelEntry("openrouter", "deepseek/deepseek-chat", "DeepSeek v3 (OpenRouter)",
                   "OpenRouter API key", "metered", ["cheap", "gateway"]),
        # Qwen / DashScope — the cloud Qwen (distinct from any local ollama qwen).
        ModelEntry("qwen", "qwen2.5-coder-32b-instruct", "Qwen2.5 Coder 32B",
                   "DashScope API key", "metered", ["coding"]),
        ModelEntry("claude-cli", "sonnet", "Claude CLI (logged-in)",
                   "your claude CLI", "subscription", ["coding"], price=P["claude-sonnet-5"]),
        # No `mock` row. It answers from canned text, which is indistinguishable from a model that
        # has gone wrong, and it sat in the picker between real models where one tap would silently
        # replace every future answer with a fixture. Tests still reach it through COLLIE_PROVIDER
        # and probe_auth() still knows it; it is simply not something to offer a person.
    ]


# ---- auth probing -------------------------------------------------------------------------
def probe_auth(provider: str) -> str:
    """'ok' | 'missing-key' | 'not-logged-in' | 'expired' | 'unknown'. Cheap + no network except
    the ollama liveness ping (which is localhost + 0.3s)."""
    if provider == "mock":
        return "ok"
    if provider == "anthropic-oauth":
        # not os.path.exists(~/.claude/.credentials.json): macOS keeps the same credentials in the
        # Keychain and writes no file, so a file check reported every logged-in Mac as logged-out.
        from .providers import _read_oauth_token, claude_oauth_expired
        if not _read_oauth_token():
            return "not-logged-in"
        # Present but stale is a THIRD state, and the only one of the three that looks fine right
        # up until the request fails. Collie does not refresh this token (providers.
        # OAUTH_EXPIRED_HINT says why), so a run can start against a credential that cannot work.
        return "expired" if claude_oauth_expired() else "ok"
    if provider == "codex-oauth":
        return "ok" if os.path.exists(
            os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                         "auth.json")) else "not-logged-in"
    if provider == "claude-cli":
        return "ok" if shutil.which("claude") else "not-logged-in"
    if provider == "ollama":
        try:
            urllib.request.urlopen("http://localhost:11434/api/tags", timeout=0.3).read()
            return "ok"
        except Exception:
            return "not-logged-in"
    env = _KEY_ENV.get(provider)
    if env:
        return "ok" if os.environ.get(env) else "missing-key"
    if provider in OPENAI_COMPAT_PRESETS:            # openai-compat / openrouter / moonshot / …
        _b, keyenv, _d = OPENAI_COMPAT_PRESETS[provider]
        return "ok" if os.environ.get(keyenv) else "missing-key"
    return "unknown"


_LOGIN_HINT = {
    "anthropic-oauth": "run `claude` once to log in",
    "codex-oauth": "run `codex login` (ChatGPT account)",
    "claude-cli": "install the claude CLI and log in",
    "ollama": "start ollama (http://localhost:11434)",
}


def auth_problem(provider: str) -> str:
    """One line saying why ``provider`` cannot be used right now, or "" when it can."""
    status = probe_auth(provider)
    if status == "ok":
        return ""
    if status == "expired":
        from .providers import OAUTH_EXPIRED_HINT
        return "%s: %s" % (provider, OAUTH_EXPIRED_HINT)
    if status == "missing-key":
        env = _KEY_ENV.get(provider)
        if not env and provider in OPENAI_COMPAT_PRESETS:
            env = OPENAI_COMPAT_PRESETS[provider][1]
        return "%s: set %s" % (provider, env or "its API key")
    if status == "not-logged-in":
        return "%s: %s" % (provider, _LOGIN_HINT.get(provider, "not logged in"))
    return "%s: unknown provider" % provider


def preflight(members) -> list:
    """Problems that would sink a multi-provider run, one line each; [] when every member is usable.

    ``members`` is an iterable of provider names or ``(provider, model)`` pairs. This spends no
    completion — it is the same cheap probe the model picker uses — so a roster whose third member
    has a stale subscription token says so NOW, by name, instead of after two members have already
    run and the third burns its attempts on 401s. Order is preserved and duplicates collapse, since
    a roster naming one provider twice should not report it twice.
    """
    problems, seen = [], set()
    for member in members or ():
        provider = member[0] if isinstance(member, (tuple, list)) else member
        if not provider or provider in seen:
            continue
        seen.add(provider)
        problem = auth_problem(provider)
        if problem:
            problems.append(problem)
    return problems


# ---- live discovery -----------------------------------------------------------------------
_disc_cache = {}                                     # provider -> (expiry_ts, [model_ids])
_DISC_TTL = 300


def _http_json(url: str, headers: dict, timeout: float = 4.0):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def discover(provider: str) -> list:
    """Model ids the provider reports for the CURRENT auth. [] on any failure (offline-safe).
    Cached for _DISC_TTL so the picker never blocks on a slow endpoint twice."""
    now = time.time()
    hit = _disc_cache.get(provider)
    if hit and hit[0] > now:
        return hit[1]
    ids = []
    try:
        if provider == "codex-oauth":
            from .codex_oauth import _fresh_access_token, BASE_URL
            access, acct = _fresh_access_token()
            d = _http_json(BASE_URL.rstrip("/") + "/models?client_version=1.0.0",
                           {"authorization": "Bearer " + access, "ChatGPT-Account-Id": acct,
                            "originator": "codex_cli_rs", "user-agent": "codex_cli_rs/0.0.0 (collie)",
                            "accept": "application/json"})
            ids = [m["slug"] for m in d.get("models", []) if m.get("slug")]
        elif provider == "ollama":
            d = _http_json("http://localhost:11434/api/tags", {}, timeout=1.0)
            ids = [m["name"] for m in d.get("models", []) if m.get("name")]
        elif provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            if key:
                d = _http_json("https://api.anthropic.com/v1/models",
                               {"x-api-key": key, "anthropic-version": "2023-06-01"})
                ids = [m["id"] for m in d.get("data", []) if m.get("id")]
        elif provider == "anthropic-oauth":
            # The subscription path had NO discovery branch at all — only the API-key one existed,
            # and it needs ANTHROPIC_API_KEY, which a subscription user does not have. So the picker
            # showed the hand-written list forever: Opus 5 shipped and Collie went on offering 4.8,
            # with no way to notice. The same endpoint answers a Bearer token.
            from . import providers as _p
            tok = _p._read_oauth_token()
            if tok:
                d = _http_json("https://api.anthropic.com/v1/models?limit=40",
                               {"authorization": "Bearer " + tok,
                                "anthropic-version": "2023-06-01",
                                "anthropic-beta": _p._CC_BETAS,
                                "user-agent": "claude-code/%s (external, cli)" % _p._claude_version(),
                                "x-app": "cli"})
                ids = [m["id"] for m in d.get("data", []) if m.get("id")]
        elif provider in OPENAI_COMPAT_PRESETS or provider in _KEY_ENV:
            base, keyenv, _d = OPENAI_COMPAT_PRESETS.get(
                provider, ("https://api.openai.com/v1", _KEY_ENV.get(provider, ""), ""))
            key = os.environ.get(keyenv, "")
            if key:
                d = _http_json(base.rstrip("/") + "/models", {"authorization": "Bearer " + key})
                ids = [m.get("id") for m in d.get("data", []) if m.get("id")]
    except Exception:
        ids = []
    ids = [i for i in ids if i]
    _disc_cache[provider] = (now + _DISC_TTL, ids)
    return ids


def _label_for(provider: str, model: str) -> str:
    return model


def _via_kind(provider: str) -> tuple:
    if provider in ("anthropic-oauth", "codex-oauth", "claude-cli"):
        return {"anthropic-oauth": "Claude direct (experimental)", "codex-oauth": "ChatGPT subscription",
                "claude-cli": "your claude CLI"}[provider], "subscription"
    if provider in ("ollama", "mock"):
        return "local", "local"
    return "API key", "metered"


# ---- the merged catalog -------------------------------------------------------------------
def list_entries(discover_live: bool = False, custom: dict | None = None) -> list:
    """The full catalog as a list of dicts (each with an `auth` badge + price). Static always;
    live discovery only for providers whose auth probes 'ok'; plus one custom openai-compat
    entry if the settings carry a base_url+model. Deduped by (provider, model)."""
    from . import costs
    entries, seen = [], set()
    auth_cache = {}
    unauthed_shown = set()          # providers we've already shown one "get started" row for

    def _auth(p):
        if p not in auth_cache:
            auth_cache[p] = probe_auth(p)
        return auth_cache[p]

    def _add(e: ModelEntry, collapse_unauthed: bool = False):
        if e.id in seen:
            return
        a = _auth(e.provider)
        if collapse_unauthed and a != "ok":
            # a provider you have no key/login for gets ONE representative row (so you can see
            # it exists + how to enable it), not its whole curated list greyed out.
            if e.provider in unauthed_shown:
                return
            unauthed_shown.add(e.provider)
        seen.add(e.id)
        entries.append(e.to_dict(a))

    for e in _static():
        _add(e, collapse_unauthed=True)

    # `mock` is not offered (see _static), but a machine already ON it must still see what it is on
    # — otherwise the picker shows a current id matching no row, which reads as "nothing selected"
    # on the very setup whose whole problem is that it is answering from fixtures.
    from . import settings as _settings
    if _settings.get("PROVIDER", "") == "mock":
        # The id has to be the one `current` reports, or the row it describes shows no tick and the
        # picker still looks like it has lost track of itself. PROVIDER and MODEL are separate knobs,
        # so a machine pinned to mock usually keeps whatever MODEL name was already saved.
        # all_values(), not get("MODEL", ""): `current` in /api/models is built from all_values, which
        # falls back to the SCHEMA default rather than to empty. Reading MODEL a second way produced a
        # row id of mock:mock against a current of mock:claude-opus-4-8 — the same "nothing selected"
        # this row exists to prevent.
        _add(ModelEntry("mock", _settings.all_values().get("MODEL") or "mock",
                        "Mock (offline) — canned replies, not a model",
                        "local, canned", "local", ["testing"]))

    # LOCAL discovery (Ollama) is a cheap localhost call and the ONLY way to know which models
    # you've pulled — always include it, no toggle. NETWORK (cloud) discovery stays opt-in below.
    if _auth("ollama") == "ok":
        for mid in discover("ollama"):
            _add(ModelEntry("ollama", mid, mid, "local (Ollama)", "local", source="live"))

    if discover_live:
        # the full per-provider model list for every cloud we're actually authed for — this is
        # how DeepSeek/Qwen/OpenRouter/… surface their whole catalog without hardcoding it.
        providers = sorted({e["provider"] for e in entries} |
                           {"openrouter", "openai", "codex-oauth", "deepseek", "qwen"})
        for p in providers:
            if p == "ollama" or _auth(p) != "ok":
                continue
            for mid in discover(p):
                if "%s:%s" % (p, mid) in seen:
                    continue
                via, kind = _via_kind(p)
                _add(ModelEntry(p, mid, _label_for(p, mid), via, kind,
                                source="live", price=costs.price_for(mid)))

    if custom and custom.get("base_url") and custom.get("model"):
        _add(ModelEntry("openai-compat", custom["model"], custom["model"],
                        "custom endpoint", "metered", source="custom",
                        price=costs.price_for(custom["model"])))

    # stable, useful order: authed first, then subscription > metered > local, then label
    kind_rank = {"subscription": 0, "metered": 1, "local": 2}
    entries.sort(key=lambda e: (e["auth"] != "ok", kind_rank.get(e["kind"], 3), e["label"]))
    return entries


def resolve(entry_id: str) -> tuple:
    """'provider:model' -> (provider, model). Provider names carry no ':', so split once."""
    provider, _, model = (entry_id or "").partition(":")
    return provider, (model or None)
