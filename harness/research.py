"""research.web — research + decide, driving the user's real browser.

The plan's first-slice archetype #1 (research + decision). It runs collie's own
bounded agent loop with the FULL browser tool set against the user's real
logged-in Chrome (open/read/links + type/click/console/eval via the bridge) plus
keyless web_search/web_fetch — so it can actually USE interactive sites (fill a
finder form, click through) to produce a short, SOURCED answer. Only the local
file/shell mutators are dropped, so the browser is the sole actuator.

Its done-check is structural: the answer must declare findings (not NOFINDINGS),
carry a real cited URL, and follow the mandated Sources: format; cited-URL
re-fetch is an informational annotation, never a gate.

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
# Research uses the user's REAL browser with its FULL tool set — open/read/links
# AND type/click/console/eval — so it can actually USE interactive sites (type a
# zip into VSP's finder, click through, work a form), plus cookieless web_search/
# web_fetch. The browser is the only actuator: we drop just the LOCAL mutators
# (edit/write/bash/run_in_env) so a run can't touch the filesystem or shell.
# web_fetch stays SSRF-guarded (loopback/metadata blocked) for the urllib path.
_RESEARCH_DROP = {"edit_file", "write_file", "bash", "run_in_env"}
# ANCHORED (with an optional apology lead-in): a no-info/refusal reply BEGINS by
# stating the model's own inability. A real answer to a negative-topic query
# ("how to fix 'unable to locate package'") mentions such a phrase MID-string,
# describing the topic — anchoring at the start distinguishes the two. Applied
# with .match() alongside the citation gate.
_NOINFO = re.compile(
    r"(?i)^(?:[^\n]{0,80}?[,，:：]\s*)?\W*"          # skip an optional short lead-in clause
    r"(?:(?:i'?m\s+)?(?:sorry|apolog\w*|unfortunately|regrettabl\w*|抱歉|很抱歉|遗憾|很遗憾)"
    r"[\s,，、:：.\-—]*)?"
    r"(?:"
    # first-person branch: object is REQUIRED (no trailing ?) — "I can't go wrong"
    # / "I couldn't be happier" are real content, not refusals; only "I can't FIND"
    # etc. count.
    r"i\s+(?:couldn'?t|could\s+not|was\s+unable|am\s+unable|was\s+not\s+able|can'?t|cannot|failed|refuse)"
    r"\s+(?:to\s+)?(?:find|locate|retrieve|determine|help|assist|answer|provide)"
    r"|(?:was\s+)?unable\s+to\s+(?:find|locate|determine|help|answer)"
    r"|no\s+(?:information|results?|data|sources?|findings?|luck)\b"
    r"|nothing\s+(?:turned\s+up|found|came\s+up)"
    r"|as\s+an\s+ai\b"
    r"|我?(?:没(?:能|有)?(?:找到|查到)|无法(?:找到|查到|获取|回答|完成|提供|帮)|查不到|找不到|不能(?:帮|回答))"
    r")")

_PROMPT = (
    "Research this and give the MOST USEFUL answer you can, grounded in real sources. "
    "Give a SHORT recommendation (a few sentences). If you cannot fully resolve it — "
    "you don't know the user's exact location, or real-time data (today's "
    "availability, live prices) isn't on the open web — do NOT give up: still provide "
    "the best actionable guidance (the right official tool/directory to use, the major "
    "relevant options, and how to get the rest), and say what you'd need (e.g. a city "
    "or zip). Then a markdown list of 2-4 real source URLs under a final line starting "
    "'Sources:'. Use the browser (browser_open/browser_read/browser_links) and "
    "web_search/web_fetch. Do NOT edit or write files. Reply with "
    "EXACTLY the single token NOFINDINGS only if the open web has nothing relevant at "
    "all. Question: ")


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
    # browser=None -> default_registry auto-enables the real browser bridge when it
    # is live (that is the whole point). web_search/web_fetch are force-registered
    # below so they exist as a fallback even when the bridge suppresses them.
    h = make_harness(_notes_dir(), provider=_s.get("PROVIDER"), model=_s.get("MODEL"),
                     project="research", embed="hash", web_search=True)
    from .websearch import register_web_search
    from .webfetch import register_web_fetch
    if not h.registry.get("web_search"):
        register_web_search(h.registry)
    if not h.registry.get("web_fetch"):
        register_web_fetch(h.registry)
    # keep the FULL browser + web tools; drop only local file/shell mutators so a
    # research run acts ONLY through the browser (never the filesystem).
    for name in list(h.registry._tools):
        if name in _RESEARCH_DROP:
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
    # STRUCTURAL gates first (robust, not phrase-matching): a real answer declares
    # findings (not NOFINDINGS), carries a URL, AND follows the mandated format with
    # a "Sources:" section — a prose non-finding with a bare URL ("Nothing turned
    # up. https://x") has no Sources block. _NOINFO is a narrowed phrase backstop.
    if (not answer or answer.strip().upper().startswith("NOFINDINGS")
            or not cites
            or not re.search(r"(?im)^\s*(?:sources?|来源|参考(?:资料|链接)?)\s*[:：]", answer)
            or _NOINFO.match(answer)):
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
