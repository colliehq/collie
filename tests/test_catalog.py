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
check("static catalog non-empty", len(ents) > 10)
check("every entry has provider+model+auth+price", all(
    e.get("provider") and e.get("model") and e.get("auth") and "price_in" in e for e in ents))
check("codex-oauth terra present", "codex-oauth:gpt-5.6-terra" in by_id)
check("anthropic-oauth opus present", "anthropic-oauth:claude-opus-4-8" in by_id)

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

# ---- ordering: authed-ok before un-authed; subscription kind ranks first -------------
first_unauthed = next((i for i, e in enumerate(ents) if e["auth"] != "ok"), len(ents))
last_authed = max((i for i, e in enumerate(ents) if e["auth"] == "ok"), default=-1)
check("all authed entries sort before un-authed", last_authed < first_unauthed)

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
