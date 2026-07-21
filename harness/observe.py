"""Independent-channel world observation — the substrate the world done-checks
read ground truth through.

The code gate's ground truth is a process exit code (loop.py:97-106). A world
done-check's ground truth is a fresh READ of the real world — but it must come
back through a DIFFERENT channel than the one that acted, or the app's own
"Published!" toast (returned through the logged-in browser bridge, i.e. the
acting path) could vouch for a publish that never became public.

So this module deliberately observes with NO browser session: a cookieless
stdlib `urllib` GET. That is the whole point — the logged-in bridge holds the
credentials and does the acting; verification uses a plain, credential-free
fetch, so "the listing is visible to a logged-out stranger" is what gets
asserted, not "the site told my own session it worked".

Injection note: observed HTML is untrusted, but host code here only runs
deterministic predicates (substring / regex) over it and NEVER feeds it to a
model, so the fetched page cannot carry instructions into the agent. Verifying
in host code is itself a containment property.

Three outcomes, mapped onto the verifier's four-state verdict:
  predicate -> True   an Observation(ok=True)  is emitted -> VERIFIED-eligible
  predicate -> False  an Observation(ok=False) is emitted -> FAILED (refuted)
  predicate -> None   NO Observation is emitted            -> INCONCLUSIVE
  fetch error         NO Observation is emitted            -> INCONCLUSIVE (fail-closed)

`None` is the honest "I could not tell" (a login wall, an ambiguous page): it
must never be laundered into FAILED (that would falsely report the outcome
wrong) nor into VERIFIED (that would be a Manus-style claimed success). It
routes to the plan's needs_you.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

from .verifier import ListingVerifier, Mutation, Observation, Verdict

_UA = "collie-donecheck/0.1 (+independent-verification; no-session)"


def fetch_loggedout(url: str, timeout: float = 10.0):
    """Cookieless GET through the independent channel. Returns (status, text) on
    any HTTP response (we OBSERVED, whatever the status), or None on a transport
    error (we could NOT observe — fail-closed to INCONCLUSIVE).

    A fresh OpenerDirector with no HTTPCookieProcessor means zero session state:
    this fetch carries none of the bridge's logged-in cookies by construction.
    """
    opener = urllib.request.build_opener()  # no cookie processor == no session
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(2_000_000)  # cap; a listing page fits easily
            enc = r.headers.get_content_charset() or "utf-8"
            return r.status, body.decode(enc, "replace")
    except urllib.error.HTTPError as e:  # a real HTTP response (404/403/500…) — we DID observe
        try:
            return e.code, (e.read(500_000).decode("utf-8", "replace"))
        except Exception:
            return e.code, ""
    except Exception:  # DNS/timeout/refused/TLS — we could NOT observe
        return None


_PRICE_RE = re.compile(r"[¥$€£]\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*)\s?(?:元|USD|CNY|RMB)")


def _parse_price(text: str):
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def listing_predicate(expect_title: str, price_max=None):
    """Author-once done-check for a published listing. Returns a predicate
    (html) -> True | False | None:

      True   the title is present AND (no price cap, or a parsed price <= cap)
      False  the title is present but the parsed price exceeds the cap (refuted)
      None   the title is NOT present — could be a login wall / wrong page, so we
             cannot tell the outcome from a logged-out fetch (-> INCONCLUSIVE),
             which is the honest answer, not a FAILED.
    """
    needle = (expect_title or "").strip().lower()

    def pred(html: str):
        hay = html.lower()
        if needle and needle not in hay:
            return None  # can't confirm we even reached the listing
        if price_max is None:
            return True
        price = _parse_price(html)
        if price is None:
            return None  # title seen but no readable price — ambiguous
        return price <= float(price_max)

    return pred


def donecheck_listing(url: str, expect_title: str, price_max=None,
                      at: float = 0.0, publish_at: float = 0.0,
                      fetch=fetch_loggedout) -> Verdict:
    """Run the post-publish done-check for a listing through the independent
    channel and return the verifier's verdict, with the observation attached as
    receipt evidence.

    `publish_at`/`at`: the mutation and observation order keys (turn or ts) so
    freshness holds — the observation must post-date the publish. `fetch` is
    injectable so tests can drive it against a local fixture server (real
    sockets) without hitting a live marketplace.
    """
    mut = [Mutation(at=publish_at, kind="publish", reversible=False,
                    detail=f"publish listing {url}")]
    got = fetch(url)
    obs = []
    if got is not None:
        status, text = got
        if status in (404, 410):
            # definitively absent — a logged-out stranger cannot see it. Refuting,
            # not merely unobservable (distinct from a login wall / transport error).
            obs = [Observation(channel="logged-out-fetch", at=at, ok=False,
                               asserted=True, detail=f"GET {url} -> {status} (not visible)")]
        else:
            verdict = listing_predicate(expect_title, price_max)(text)
            if verdict is not None:
                price = _parse_price(text)
                obs = [Observation(
                    channel="logged-out-fetch", at=at, ok=bool(verdict), asserted=True,
                    detail=f"GET {url} -> {status}; title=seen"
                           f"{'' if price is None else f'; price={price}'}")]
            # verdict is None -> no observation -> INCONCLUSIVE (wall / ambiguous)
    return ListingVerifier().verdict(mut, obs)
