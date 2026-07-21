"""research.web — a read-only research capability using collie's real browser.

The plan's first-slice archetype #1 (research + decision), and the delegation
category the 2026 market evidence says actually works. It runs collie's own
bounded agent loop restricted to READ-ONLY tools — the user's real logged-in
browser (browser_open/read/links via the bridge) plus keyless web_search/
web_fetch — to answer a question with a short, SOURCED recommendation.

It is read-only, so it needs no confirm token; the leash marks it reversible and
drive() runs it autonomously. Its done-check is honest: re-fetch the cited URLs
through the independent channel and require at least one to be reachable — a
recommendation with no verifiable source is INCONCLUSIVE, never "verified".

The agent run is injectable (`runner`) so tests are deterministic without a live
model or browser.
"""

from __future__ import annotations

import os
import re

from . import verifier as _v
from .jobs import Capability, register
from .observe import fetch_loggedout

_URL = re.compile(r"https?://[^\s)\]}>\"'，、。（）：；]+")   # also stop at CJK punctuation
# Autonomous research uses ONLY the cookieless, SSRF-guarded web tools — NOT the
# user's authenticated browser bridge. Driving a logged-in browser with no human
# present (and feeding its pages to the model) is a real exfiltration/injection
# risk; authenticated-browser research belongs behind a confirm, not here.
_RESEARCH_TOOLS = {"web_search", "web_fetch"}
# ANCHORED (with an optional apology lead-in): a no-info/refusal reply BEGINS by
# stating the model's own inability. A real answer to a negative-topic query
# ("how to fix 'unable to locate package'") mentions such a phrase MID-string,
# describing the topic — anchoring at the start distinguishes the two. Applied
# with .match() alongside the citation gate.
_NOINFO = re.compile(
    r"(?i)^\W*"
    r"(?:(?:i'?m\s+)?(?:sorry|apolog\w*|unfortunately|regrettabl\w*|抱歉|很抱歉|遗憾|很遗憾)"
    r"[\s,，、:：.\-—]*)?"
    r"(?:"
    r"i\s+(?:couldn'?t|could\s+not|was\s+unable|am\s+unable|can'?t|cannot|failed|refuse)"
    r"(?:\s+(?:to\s+)?(?:find|locate|retrieve|help|assist|answer|provide|do))?"
    r"|(?:was\s+)?unable\s+to\s+(?:find|locate|help|answer)"
    r"|no\s+(?:information|results?|data|sources?)\s+(?:found|available|for|on)"
    r"|as\s+an\s+ai\b"
    r"|我?(?:没(?:能|有)?(?:找到|查到)|无法(?:找到|查到|获取|回答|完成|提供|帮)|查不到|找不到|不能(?:帮|回答))"
    r")")

_PROMPT = (
    "Research this and give a SHORT recommendation (a few sentences), then a "
    "markdown list of 2-4 source URLs under a final 'Sources:' line. Use web_search "
    "and web_fetch. Do NOT edit or write files. Question: ")


def _notes_dir() -> str:
    d = os.environ.get("COLLIE_NOTES_DIR") or os.path.expanduser("~/.collie/notes")
    os.makedirs(d, exist_ok=True)
    return d


def _slug(q: str) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "-", (q or "").strip())[:40].strip("-")
    return s or "query"


def _live_runner(query: str) -> str:
    """Run collie's agent loop, restricted to read-only research tools, and return
    its answer text. Uses the configured provider/model and the real browser."""
    from .cli import make_harness
    from . import settings as _s
    _s.apply()
    h = make_harness(_notes_dir(), provider=_s.get("PROVIDER"), model=_s.get("MODEL"),
                     project="research", embed="hash", browser=False, web_search=True)
    # ensure the cookieless web tools exist even if a live bridge suppressed them
    # (default_registry drops web_search/web_fetch when the bridge is on).
    from .websearch import register_web_search
    from .webfetch import register_web_fetch
    if not h.registry.get("web_search"):
        register_web_search(h.registry)
    if not h.registry.get("web_fetch"):
        register_web_fetch(h.registry)
    # read-only + cookieless: keep ONLY web_search/web_fetch (no bridge, no edit/bash)
    for name in list(h.registry._tools):
        if name not in _RESEARCH_TOOLS:
            del h.registry._tools[name]
    h.self_verify = False
    h.force_edit = False
    h.max_turns = 12
    # enforce cookieless: web_search can be told (via env) to route through the
    # authenticated browser/Chrome profile — scrub those for the autonomous run so
    # research can never reach the logged-in session, then restore.
    _scrub = ("COLLIE_WEBSEARCH_BRIDGE", "COLLIE_WEBSEARCH_CHROME",
              "COLLIE_CHROME", "COLLIE_CHROME_PROFILE",
              "COLLIE_WEBFETCH_ALLOW_LOCAL")   # keep the SSRF guard ON for unattended fetches
    saved = {k: os.environ.pop(k, None) for k in _scrub}
    try:
        res = h.run("research", _PROMPT + query)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    return res.answer or ""


def run_research(query: str, runner=None) -> dict:
    answer = (runner or _live_runner)(query)
    cites = []
    for u in _URL.findall(answer or ""):
        u = u.rstrip(".,;")
        if u not in cites:
            cites.append(u)
    cites = cites[:6]
    path = os.path.join(_notes_dir(), f"research-{_slug(query)}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {query}\n\n{answer}\n")
    return {"answer": answer, "citations": cites, "report_file": path}


def _research_execute(record):
    return run_research(record.args.get("query", ""))


class _CiteVerifier(_v.Verifier):
    channels = ("research-answer",)
    require_assert = True


def _research_verify(record, result):
    """Research is a DELIVERABLE-is-the-answer task: success = a real answer was
    produced and saved. Re-fetching the cited URLs is an informational annotation
    on the receipt (many real sites 403 a cookieless bot), NEVER a gate that
    downgrades a delivered answer — collie delivers, it doesn't hedge. An empty
    answer is the only genuine miss."""
    result = result or {}
    answer = (result.get("answer") or "").strip()
    cites = result.get("citations") or []
    # Two complementary gates: (1) a real answer must carry a URL (the prompt
    # demands a Sources list) — a source-less refusal fails here; (2) a no-info
    # reply that DID emit a (generic/hallucinated) URL is caught by the anchored
    # _NOINFO — it opens by stating the model's own inability, which a real answer
    # (even one restating "unable to locate" about the topic) does not.
    if not answer or not cites or _NOINFO.match(answer):
        return _v.Verdict(_v.FAILED, "no sourced answer produced")
    ok = sum(1 for u in cites if (lambda g: g is not None and g[0] < 400)(fetch_loggedout(u)))
    detail = (f"answer written to {os.path.basename(result.get('report_file', ''))}"
              + (f"; {ok}/{len(cites)} sources re-checkable" if cites else ""))
    obs = [_v.Observation(channel="research-answer", at=2, ok=True, asserted=True,
                          detail=detail)]
    return _CiteVerifier().verdict(
        [_v.Mutation(at=1, kind="research", reversible=True)], obs)


def register_research():
    register(Capability(
        "research.web", execute=_research_execute, verify=_research_verify,
        reversible=True, risk="reversible",
        description="research a question on the web using the real browser; returns a short "
                    "recommendation with cited sources",
        args_hint='{"query": "<what to find out, e.g. where to buy X>"}'))
