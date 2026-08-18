"""Offline tests for the model catalog — merge/dedup/resolve/auth/price/ordering."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="cat_")   # no codex login -> not-logged-in
os.environ.pop("OPENAI_API_KEY", None)                       # ensure openai probes missing-key

from harness import catalog, costs

ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# ---- static catalog + shape ----------------------------------------------------------
ents = catalog.list_entries(discover_live=False)
by_id = {e["id"]: e for e in ents}
check("static catalog has one representative for each unavailable provider", len(ents) >= 8)
check("every entry has provider+model+auth+price", all(
    e.get("provider") and e.get("model") and e.get("auth") and "price_in" in e for e in ents))
check("codex-oauth terra present", "codex-oauth:gpt-5.6-terra" in by_id)
check("anthropic-oauth representative present", any(
    e["provider"] == "anthropic-oauth" for e in ents))

# ---- dedup: no duplicate ids ---------------------------------------------------------
ids = [e["id"] for e in ents]
check("no duplicate (provider,model)", len(ids) == len(set(ids)))

# ---- resolve round-trips (incl. models whose id contains a colon, e.g. ollama tags) --
check("resolve simple", catalog.resolve("codex-oauth:gpt-5.6-terra") == ("codex-oauth", "gpt-5.6-terra"))
check("resolve colon-in-model", catalog.resolve("ollama:qwen2.5-coder:7b") == ("ollama", "qwen2.5-coder:7b"))
check("resolve empty", catalog.resolve("") == ("", None))

# ---- auth probing --------------------------------------------------------------------
check("codex-oauth not-logged-in (fake CODEX_HOME)", catalog.probe_auth("codex-oauth") == "not-logged-in")
check("openai missing-key (env unset)", catalog.probe_auth("openai") == "missing-key")
check("mock ok", catalog.probe_auth("mock") == "ok")

# ---- price registration into costs ---------------------------------------------------
check("terra priced", costs.price_for("gpt-5.6-terra") == (2.5, 0.25, 15.0))
check("luna priced", costs.price_for("gpt-5.6-luna") == (1.0, 0.10, 6.0))
check("opus still matches via substring", costs.price_for("claude-opus-4-8") == (15.0, 1.5, 75.0))
check("deepseek-reasoner beats deepseek", costs.price_for("deepseek-reasoner") == (0.55, 0.14, 2.19))

# ---- ordering: within a rank, authed-ok before un-authed; subscription kind ranks first ----
# The invariant is now per-rank rather than global. `rank` exists so a provider can be pinned below
# the everyday list — deliberately, by the plugin that supplies it — and that pin has to hold even
# while the thing is perfectly usable. Ranking auth first meant such a provider sank only while it
# was broken and sprang back up among the ordinary choices the moment it started working. Inside
# one rank the original rule is unchanged: nothing usable is ever buried under something that is not.
by_rank = {}
for i, e in enumerate(ents):
    by_rank.setdefault(e.get("rank", 0), []).append(e)
check("within each rank, authed entries sort before un-authed",
      all(max((j for j, e in enumerate(group) if e["auth"] == "ok"), default=-1)
          < next((j for j, e in enumerate(group) if e["auth"] != "ok"), len(group))
          for group in by_rank.values()))
check("ranks come out in ascending order",
      [e.get("rank", 0) for e in ents] == sorted(e.get("rank", 0) for e in ents))

# ---- entry dict is JSON-serializable (webapp sends it over the wire) ------------------
import json
json.dumps({"entries": ents})
check("catalog JSON-serializable", True)

print("\n%s" % ("ALL PASS" if ok else "SOME FAILED"))


def test_catalog_checks_pass():
    """Gate for a bare `pytest` run. The checks above execute at import (script style, the way
    run_all.sh drives this file); this just reports their verdict to a collector."""
    assert ok, "see the FAIL lines in captured stdout"


# Script mode only. At module level this SystemExit escaped during pytest's COLLECTION, which
# pytest reports as an INTERNALERROR and which aborts the whole session — so one script-style
# file took down every other test in tests/. Under a collector we hand the verdict to
# test_catalog_checks_pass instead.
if __name__ == "__main__":
    raise SystemExit(0 if ok else 1)


def _entry(**kw):
    from harness.catalog import ModelEntry
    base = dict(provider="p", model="m", label="L", via="v", kind="metered")
    base.update(kw)
    return ModelEntry(**base)


def test_only_the_curated_list_stays_outside_more_models():
    """The fold is curated-vs-discovered, not a hand-written "latest models" set.

    A hand-written one is what goes stale: this catalog once offered three Claude models for a
    machine that could serve ten. Anything the maintained list names is an everyday choice;
    anything only live discovery turns up is the long tail.
    """
    assert _entry(source="static").tier == "main"
    assert _entry(source="live").tier == "more"
    assert _entry(source="custom").tier == "main", "a hand-configured endpoint is deliberate"


def test_local_models_are_never_folded_away():
    """They are on this machine because somebody pulled them — the opposite of a long tail."""
    assert _entry(source="live", kind="local").tier == "main"


def test_rank_outranks_auth_when_ordering():
    """Something pinned below the everyday list belongs there whether or not it currently works.

    Ranking auth first meant a pinned provider sank only while it was unusable and sprang back up
    among the ordinary choices the moment it started working — the opposite of pinning it.
    """
    from harness import catalog
    rows = [catalog.ModelEntry("plug", "m1", "Pinned", "v", "subscription", rank=100).to_dict("ok"),
            catalog.ModelEntry("api", "m2", "Ordinary", "v", "metered").to_dict("missing-key")]
    kind_rank = {"subscription": 0, "metered": 1, "local": 2}
    rows.sort(key=lambda e: (e.get("rank", 0), e["auth"] != "ok",
                             kind_rank.get(e["kind"], 3), e["label"]))
    assert [r["label"] for r in rows] == ["Ordinary", "Pinned"], \
        "the usable-but-pinned row must still sort below an unusable ordinary one"


def test_a_plugin_with_no_catalog_block_contributes_no_rows(monkeypatch):
    from harness import catalog
    monkeypatch.setattr(catalog, "_plugin_info", lambda: {"x": {"label": "X"}})
    assert catalog._plugin_entries() == []


def test_plugin_rows_carry_the_plugins_via_kind_and_rank(monkeypatch):
    from harness import catalog
    monkeypatch.setattr(catalog, "_plugin_info", lambda: {
        "x": {"catalog": [{"model": "m", "label": "X"}], "via": "Some Relay",
              "kind": "subscription", "rank": 100}})
    (e,) = catalog._plugin_entries()
    assert (e.provider, e.model, e.via, e.kind, e.rank) == ("x", "m", "Some Relay", "subscription", 100)


def test_an_unstated_plugin_kind_is_metered(monkeypatch):
    """Never present something as free without being told that it is."""
    from harness import catalog
    monkeypatch.setattr(catalog, "_plugin_info",
                        lambda: {"x": {"catalog": [{"model": "m"}]}})
    assert catalog._plugin_entries()[0].kind == "metered"


def test_a_fallback_is_a_step_down_the_family_ladder(monkeypatch):
    from harness import catalog
    served = ["claude-opus-5", "claude-sonnet-5", "claude-sonnet-4-6",
              "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"]
    monkeypatch.setattr(catalog, "discover", lambda p: served)
    assert catalog.fallback_model("x", "claude-opus-5") == "claude-sonnet-5", \
        "the curated current-generation id must win over a dated snapshot"
    assert catalog.fallback_model("x", "claude-sonnet-5") == "claude-haiku-4-5-20251001"
    assert catalog.fallback_model("x", "claude-haiku-4-5-20251001") == "", "bottom of the ladder"


def test_no_fallback_outside_a_family_we_know_how_to_walk(monkeypatch):
    """Guessing a step for an unfamiliar family would just be a different way to fail."""
    from harness import catalog
    monkeypatch.setattr(catalog, "discover", lambda p: ["gpt-5.6-terra", "gpt-5.6-luna"])
    assert catalog.fallback_model("x", "gpt-5.6-terra") == ""


def test_a_fallback_never_leaves_the_provider(monkeypatch):
    """Same plan, so the only question is capacity. Crossing providers can move the bill from a
    flat plan onto a metered key, which is not a mid-turn decision to make for someone."""
    from harness import catalog
    monkeypatch.setattr(catalog, "discover", lambda p: [])
    monkeypatch.setattr(catalog, "_static", lambda: [
        catalog.ModelEntry("mine", "claude-sonnet-5", "S", "v", "subscription"),
        catalog.ModelEntry("other", "claude-haiku-4-5", "H", "v", "metered"),
    ])
    assert catalog.fallback_model("mine", "claude-opus-5") == "claude-sonnet-5"
    assert catalog.fallback_model("mine", "claude-sonnet-5") == "", \
        "the haiku on another provider must not be offered"
