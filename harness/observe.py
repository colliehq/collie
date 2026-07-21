"""Independent-channel world observation — the substrate the world done-checks
read ground truth through.

The code gate's ground truth is a process exit code (loop.py:97-106). A world
done-check's ground truth is a fresh READ of the real world — but it must come
back through a DIFFERENT channel than the one that acted, or the app's own
"Published!" toast (returned through the logged-in browser bridge, i.e. the
acting path) could vouch for a publish that never became public.

So this module deliberately observes with NO browser session: a cookieless
GET routed through webfetch's SSRF-hardened opener (scheme allowlist, DNS-pinned
per-hop revalidation, no file://data://ftp://, no auto-redirect into internal
hosts). The logged-in bridge holds the credentials and does the acting;
verification uses a plain, credential-free, egress-guarded fetch, so "the
listing is visible to a logged-out stranger" is what gets asserted.

Injection note: observed HTML is untrusted, but host code here only runs
deterministic predicates (substring / regex) over it and NEVER feeds it to a
model, so the fetched page cannot carry instructions into the agent.

Outcomes map onto the verifier's four-state verdict:
  predicate -> True   Observation(ok=True)  -> VERIFIED-eligible
  predicate -> False  Observation(ok=False) -> FAILED (refuted)
  predicate -> None   no Observation         -> INCONCLUSIVE  (login wall /
                      ambiguous / unreadable price — honest "can't tell")
  fetch refused/error no Observation         -> INCONCLUSIVE  (fail-closed)
"""

from __future__ import annotations

import re
import urllib.error

from . import webfetch as _wf
from .verifier import INCONCLUSIVE, ListingVerifier, Mutation, Observation, Verdict


def fetch_loggedout(url: str, timeout: float = 10.0):
    """Cookieless, SSRF-guarded GET through the independent channel. Returns
    (status, text) on any HTTP response (we OBSERVED, whatever the status), or
    None when we could NOT observe — an SSRF refusal (non-http(s) scheme,
    loopback/private/link-local host), a transport error, or a redirect into a
    blocked target. None routes to INCONCLUSIVE, never a forged pass.

    Reuses harness.webfetch._open_pinned so the exact, already-audited egress
    defense applies: no FileHandler/DataHandler/FTPHandler, per-hop DNS pinning,
    manual redirect revalidation. A bare urllib opener would let
    file:///etc/passwd or a metadata-IP redirect forge a VERIFIED observation.
    """
    try:
        _final, _ctype, body = _wf._open_pinned(url, timeout)
        return 200, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:            # a real HTTP response — we DID observe
        try:
            return e.code, e.read(500_000).decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except ValueError:                             # SSRF refusal / non-http(s) scheme
        return None
    except Exception:                              # DNS/timeout/refused/TLS — could NOT observe
        return None


# strip non-visible regions so a login wall that echoes the title only in <head>
# metadata (og:title) cannot satisfy a body-title match.
_HEAD = re.compile(r"(?is)<head\b.*?</head>")
_DROP = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"\s+")
_LOGINWALL = re.compile(
    r"(?i)\b(log ?in to (view|continue|see|reply)|sign ?in to (view|see|continue)"
    r"|create an account to|you must be logged in|please log ?in)\b")
# structured price first (JSON-LD / OpenGraph / microdata) — robust vs a random
# currency token elsewhere on the page (a phone plan, a shipping fee, an ad).
_STRUCT_PRICE = re.compile(
    r'(?is)(?:"price"\s*:\s*"?|property="product:price:amount"\s+content="'
    r'|property="og:price:amount"\s+content="|itemprop="price"[^>]*content=")'
    r'(\d[\d,]*(?:\.\d+)?)')
_CURRENCY = re.compile(r"[¥$€£]\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*)\s?(?:元|USD|CNY|RMB)")


def _visible(html: str) -> str:
    return _DROP.sub(" ", _HEAD.sub(" ", html or ""))


def _extract_price(html: str, visible_text: str):
    """Prefer structured data; fall back to a single unambiguous currency token
    in the visible body. Returns a float, or None when it cannot be located with
    confidence (which routes to INCONCLUSIVE, not a fabricated pass/fail)."""
    m = _STRUCT_PRICE.search(html or "")
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    hits = _CURRENCY.findall(visible_text or "")
    vals = []
    for a, b in hits:
        raw = (a or b).replace(",", "")
        try:
            vals.append(float(raw))
        except ValueError:
            pass
    # exactly one currency-tagged number in the visible body -> trust it; more
    # than one is ambiguous (shipping/promo/unrelated) -> None (needs_you).
    if len(vals) == 1:
        return vals[0]
    return None


def listing_predicate(expect_title: str, price_max=None):
    """Author-once done-check for a published listing. (html) -> True|False|None:

      True   the title is visible in the BODY and (no cap, or a confidently
             located price <= cap)
      False  the title is visible but a located price exceeds the cap (refuted)
      None   title not visible in body / a login wall / price not locatable —
             a logged-out fetch cannot tell the outcome (-> INCONCLUSIVE)
    """
    needle = (expect_title or "").strip().lower()

    def pred(html: str):
        vis = _visible(html)
        body_text = _WS.sub(" ", _TAGS.sub(" ", vis)).lower()
        if needle and needle not in body_text:
            return None                       # not confirmed we even reached the listing
        if _LOGINWALL.search(body_text) and price_max is None:
            return None                       # weak signal + wall -> can't tell
        if price_max is None:
            return True
        price = _extract_price(html, vis)
        if price is None:
            return None                       # title seen but price ambiguous
        return price <= float(price_max)

    return pred


def donecheck_listing(url: str, expect_title: str, price_max=None,
                      at: float = None, publish_at: float = None,
                      fetch=fetch_loggedout) -> Verdict:
    """Post-publish done-check for a listing through the independent channel.

    `publish_at`/`at` are the mutation/observation order keys (freshness: the
    observation must post-date the publish). They are REQUIRED — an un-timestamped
    call fails closed to INCONCLUSIVE rather than silently satisfying freshness.
    `fetch` is injectable so tests drive it against a local fixture server.
    """
    if at is None or publish_at is None:
        return Verdict(INCONCLUSIVE, "missing publish/observation timestamps (fail-closed)")
    mut = [Mutation(at=publish_at, kind="publish", reversible=False,
                    detail=f"publish listing {url}")]
    got = fetch(url)
    obs = []
    if got is not None:
        status, text = got
        if status in (404, 410):
            obs = [Observation(channel="logged-out-fetch", at=at, ok=False, asserted=True,
                               detail=f"GET {url} -> {status} (not visible to a stranger)")]
        else:
            verdict = listing_predicate(expect_title, price_max)(text)
            if verdict is not None:
                price = _extract_price(text, _visible(text))
                obs = [Observation(
                    channel="logged-out-fetch", at=at, ok=bool(verdict), asserted=True,
                    detail=f"GET {url} -> {status}; title=seen"
                           f"{'' if price is None else f'; price={price}'}")]
            # verdict None -> no observation -> INCONCLUSIVE (wall / ambiguous)
    return ListingVerifier().verdict(mut, obs)
