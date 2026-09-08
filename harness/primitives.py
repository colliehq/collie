"""Neutral primitives — the small, domain-agnostic action set a mission draws on.

This is the answer to "don't template every errand": instead of marketplace.* /
dentist.* / refund.* capabilities, there is ONE generic set the model composes
toward any goal. Selling a car, booking a table, chasing a refund all use the
SAME five — only the args (which the model fills) differ:

  research   (read)        gather facts from the web toward a question
  compose    (read)        turn facts into text (a listing, a reply, an email)
  observe    (read)        re-observe the world (logged-out fetch for evidence, or
                           an authed browser read to poll an inbox)
  web.submit (IRREVERSIBLE) fill + submit a non-commerce form (publish a listing)
  web.send   (IRREVERSIBLE) send a message (a reply, a negotiation, an email)

Risk is fixed by PRIMITIVE, not by errand (plan §5.1): the irreversible ones are
inherently gated — the leash parks them for confirm unless the mission is
pre-authorized within bounds. The reversible reads run freely under the leash.

TWO registrations behind ONE surface:
  - register_primitives(stub=True)  — canned bodies, no I/O. The container tests
    and the safe default use these.
  - register_primitives(stub=False, actuator=, provider=, research_runner=) — the
    REAL bodies: research runs collie's browser research (research.py), compose
    calls the model, observe re-fetches through webfetch / drives the browser,
    web.submit/web.send drive a BrowserActuator (webact.py) and the submit is
    verified by an INDEPENDENT logged-out re-fetch (observe.py). Every dependency
    is injectable, so the real bodies are tested with fakes + a localhost fixture;
    with no browser available they degrade to a clean 'no browser' verdict, never a
    crash. The primitive NAMES / risk tiers / mission / leash never change.

Nothing here evades detection; it automates the user's own actions on the user's
own account, gated the same way every other action is.
"""

from __future__ import annotations

import json
import hashlib
import inspect
import fnmatch
import math
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from .jobs import Capability, get_capability, register
from .verifier import FAILED, INCONCLUSIVE, VERIFIED, Observation, Verdict


def _int(v):
    try:
        return int(str(v).split()[0])
    except (ValueError, TypeError, IndexError):
        return None


# ══════════════════════════ STUB bodies (canned, no I/O) ═══════════════════════
def _stub_research(rec):
    q = (rec.args or {}).get("query") or (rec.args or {}).get("goal") or ""
    return {"case": {"researched": True}, "query": q,
            "found": f"(stub) gathered facts for {q!r}"}


def _stub_compose(rec):
    a = rec.args or {}
    facts = a.get("facts") or a.get("about") or a.get("query") or ""
    # ``text`` is an already-finished literal. ``instruction`` asks the
    # composer to create the deliverable. Keeping those meanings separate
    # prevents "write a post about ..." from being stored as the post itself.
    text = a.get("text") or (
        f"(stub) composed text for {a.get('instruction')!r} about {facts!r}"
        if a.get("instruction") else f"(stub) composed text about {facts!r}")
    return {"case": {"composed": True}, "text": text}


def _stub_observe(rec):
    a = rec.args or {}
    case = a.get("_case") or {}
    n = (_int(a.get("observe_count")) or _int(case.get("observe_count")) or 0) + 1
    present = n >= 2
    return {"case": {"observe_count": n, "signal": present},
            "present": present, "detail": f"(stub) observation #{n}, signal={present}"}


def _stub_verification_fill(rec):
    return {"case": {"verification_code_filled": True}, "filled": True,
            "source": "connected_verification_inbox"}


def _verification_fill_verify(rec, result):
    if (result or {}).get("filled"):
        return Verdict(VERIFIED, "fresh matching verification code filled from connected inbox")
    return Verdict(FAILED, (result or {}).get("error") or "verification code was not filled")


def _verification_field(snapshot, requested=""):
    text = str((snapshot or {}).get("snapshot") or "")
    requested = str(requested or "").strip()
    hits = []
    for line in text.splitlines():
        match = re.search(r"\[([^\]]+)\]\s+(?:textbox|input|combobox)\s+\"([^\"]+)\"", line, re.I)
        if not match:
            continue
        label = match.group(2)
        if requested:
            match_label = requested.casefold() in label.casefold()
        else:
            match_label = bool(re.search(
                r"verification|security code|one[ -]?time|\botp\b|验证码|驗證碼|校验码|確認碼",
                label, re.I))
        if match_label:
            hits.append(match.group(1))
    return hits[0] if len(hits) == 1 else ""


def _real_verification_fill(actuator, otp_reader=None):
    def execute(rec):
        args = rec.args or {}
        service = str(args.get("service") or "").strip()
        if not service:
            return {"filled": False, "error": "expected service name is required"}
        act = _space_actuator(actuator, getattr(rec, "job_id", ""))
        if act is None or not hasattr(act, "snapshot") or not hasattr(act, "type_ref"):
            return {"filled": False, "error": "connected browser is unavailable"}
        target = act.snapshot() or {}
        ref = _verification_field(target, args.get("field"))
        if not ref:
            return {"filled": False, "error": "verification-code field is missing or ambiguous"}
        reader = otp_reader
        if reader is None:
            from .workidentity import take_verification_code
            reader = take_verification_code
        code = ""
        try:
            reader_args = {"max_age_seconds": args.get("max_age_seconds", 600)}
            if args.get("channel"):
                reader_args["channel"] = args.get("channel")
            code, meta = reader(service, **reader_args)
            act.type_ref(ref, code, submit=False)
            return {"case": {"verification_code_filled": True}, "filled": True,
                    "source": meta.get("source", "connected_verification_inbox"),
                    "account": meta.get("account", ""),
                    "received_at": int(meta.get("received_at") or 0)}
        except Exception as exc:
            return {"filled": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            code = ""  # make the intended lifetime explicit; never return or persist it
    return execute


def _read_verify(rec, result):
    return Verdict(VERIFIED, (result or {}).get("detail") or "observation recorded")


def _stub_web_submit(rec):
    a = rec.args or {}
    ref = a.get("what") or a.get("title") or "submission"
    url = "https://example.invalid/item/STUB-" + str(ref).lower().replace(" ", "-")[:40]
    return {"case": {"submitted": True, "url": url}, "url": url, "what": ref}


def _stub_submit_verify(rec, result):
    if (result or {}).get("url"):
        return Verdict(VERIFIED, "submitted; live per (stub) re-fetch")
    return Verdict(FAILED, "submit produced no confirmation")


def _stub_web_send(rec):
    a = rec.args or {}
    return {"case": {"sent": True, "last_sent_to": a.get("to")},
            "to": a.get("to"), "text": a.get("text"), "sent": True}


def _stub_send_verify(rec, result):
    if (result or {}).get("sent"):
        return Verdict(VERIFIED, "message sent (stub)")
    return Verdict(FAILED, "message not sent")


# ══════════════════════════ REAL bodies (injectable deps) ═════════════════════
def _get_provider():
    try:
        from . import settings as _s
        _s.apply()
        name = _s.get("PROVIDER") or "mock"
        if name == "mock":
            return None
        from .providers import make_provider
        return make_provider(name, _s.get("MODEL"))
    except Exception:
        return None


def _real_research(runner=None):
    def execute(rec):
        from .research import run_research
        q = (rec.args or {}).get("query") or (rec.args or {}).get("goal") or ""
        out = run_research(q, runner=runner)
        ans = out.get("answer", "")
        return {"case": {"researched": True, "research": ans[:600]},
                "answer": ans, "citations": out.get("citations", []),
                "report_file": out.get("report_file", "")}
    return execute


def _real_research_verify(rec, result):
    from .research import _research_verify
    return _research_verify(rec, result)


_COMPOSE_REQUEST_OPEN = re.compile(
    r"^\s*(?:please\s+)?(?:write|create|draft|produce|generate|compose|prepare|rewrite)\b",
    re.I,
)
_COMPOSE_REQUEST_CUE = re.compile(
    r"\b(?:copy|post|email|message|reply|caption|title|body|platform[- ]specific|"
    r"publication[- ]ready|ready[- ]to[- ](?:use|publish)|must include|should be|"
    r"do not (?:claim|invent|include))\b",
    re.I,
)
_COMPOSE_REQUEST_ZH = re.compile(
    r"^\s*(?:请|帮我)?(?:写|撰写|起草|生成|创作|准备).{0,80}"
    r"(?:文案|帖子|邮件|消息|回复|标题|正文|可直接发布)",
)


def _compose_request_like(text):
    """Recognise a writing request misplaced in ``args.text``.

    ``text`` is normally a final literal, but a model can ignore the schema and
    put "Write/Create ... copy" there.  The predicate intentionally requires a
    writing verb *and* a meta-writing cue so legitimate slogans such as
    "Create faster with VocalCode" remain literal copy.
    """
    value = str(text or "").strip()
    return bool(
        (_COMPOSE_REQUEST_OPEN.search(value) and _COMPOSE_REQUEST_CUE.search(value))
        or _COMPOSE_REQUEST_ZH.search(value)
    )


def _real_compose(provider=None):
    def execute(rec):
        a = rec.args or {}
        facts = a.get("facts") or a.get("about") or a.get("_case") or a.get("query") or ""
        instruction = str(a.get("instruction") or "").strip()
        prov = provider or _get_provider()
        # ``text`` is already-final copy. Generation requests belong in
        # ``instruction`` so the result cannot silently echo a writing request.
        text = str(a.get("text") or "").strip()
        if not instruction and _compose_request_like(text):
            instruction, text = text, ""
        should_generate = bool(instruction) or not text
        if should_generate and prov is not None:
            sys = ("Create the final, ready-to-use text for the user's errand. Follow the "
                   "instruction precisely, use only the supplied facts, and stay honest. "
                   "Return the deliverable itself in plain text with no planning notes, "
                   "placeholders, or preamble.")
            payload = {"facts": facts}
            if instruction:
                payload["instruction"] = instruction
            if text:
                payload["draft"] = text
            try:
                comp = prov.complete(
                    sys, [{"role": "user", "content":
                           json.dumps(payload, ensure_ascii=False)[:6000]}], [])
                if getattr(comp, "stop_reason", "") != "error":
                    text = (getattr(comp, "text", "") or "").strip()
            except Exception:
                text = ""
        if instruction and not text:
            return {"case": {"composed": False}, "text": "",
                    "error": "composer could not produce the requested deliverable"}
        if not text:                     # no model / empty -> a plain factual fallback
            text = facts if isinstance(facts, str) else json.dumps(facts, ensure_ascii=False)
        return {"case": {"composed": True, "draft": text}, "text": text}
    return execute


def _compose_verify(rec, result):
    text = str((result or {}).get("text") or "").strip()
    args = rec.args or {}
    instruction = str((args.get("instruction") or "")).strip()
    misplaced = str((args.get("text") or "")).strip()
    if not instruction and _compose_request_like(misplaced):
        instruction = misplaced
    if instruction and text == instruction:
        return Verdict(FAILED, "composer echoed the instruction instead of producing final text")
    if instruction and _compose_request_like(text):
        return Verdict(FAILED, "composer returned another writing request instead of final text")
    if text:
        return Verdict(VERIFIED, "text composed")
    return Verdict(FAILED, "nothing composed")


def _real_observe(actuator=None, fetch=None):
    def execute(rec):
        a = rec.args or {}
        case = a.get("_case") or {}
        n = (_int(a.get("observe_count")) or _int(case.get("observe_count")) or 0) + 1
        url = a.get("url") or a.get("target") or ""
        expect = (a.get("expect") or "").strip()
        authed = bool(a.get("authed") or a.get("inbox"))
        text, how = "", ""
        if authed:
            # poll an authed page (e.g. the message inbox) via the logged-in browser
            act = _space_actuator(actuator, getattr(rec, "job_id", ""))
            if act is None:
                return {"case": {"observe_count": n}, "present": None,
                        "detail": "no browser to read the authed page"}
            try:
                act.open(url)
                scope_error = _actuator_scope_error(act, a, url)
                if scope_error:
                    return {"case": {"observe_count": n}, "present": None,
                            "detail": scope_error}
                text, how = act.read(4000), "authed-browser-read"
            except Exception as e:
                return {"case": {"observe_count": n}, "present": None,
                        "detail": f"authed read failed: {e}"}
        else:
            # independent, logged-out channel (the evidence path)
            from .observe import fetch_loggedout
            got = (fetch or fetch_loggedout)(url)
            if got is None:
                return {"case": {"observe_count": n}, "present": None,
                        "detail": f"could not observe {url} (SSRF/transport)"}
            _status, text = got
            how = "logged-out-fetch"
        present = (expect.lower() in (text or "").lower()) if expect else bool((text or "").strip())
        return {"case": {"observe_count": n, "signal": present},
                "present": present, "channel": how,
                "detail": f"{how}: {'found' if present else 'not found'} "
                          f"{('%r' % expect) if expect else ''} in {url}".strip()}
    return execute


def _real_web_submit(actuator=None):
    def execute(rec):
        a = rec.args or {}
        url = a.get("url") or ""
        fields = a.get("fields") or {}
        submit_sel = a.get("submit") or a.get("submit_selector") or ""
        act = _space_actuator(actuator, getattr(rec, "job_id", ""))
        if act is None:
            return {"submitted": False, "error": "no browser available (start `collie browser-bridge` and connect the extension)"}
        try:
            act.open(url)
            scope_error = _actuator_scope_error(act, a, url)
            if scope_error:
                return {"submitted": False, "error": scope_error}
            for sel, text in (fields.items() if isinstance(fields, dict) else []):
                act.type(sel, text)
            result_url = act.click(submit_sel) if submit_sel else act.current_url()
        except Exception as e:
            return {"submitted": False, "error": f"submit failed: {type(e).__name__}: {e}"}
        return {"case": {"submitted": True, "url": result_url or url},
                "submitted": True, "url": result_url or url, "published_at": time.time(),
                "expect_title": a.get("expect_title") or a.get("title") or ""}
    return execute


def _real_submit_verify(rec, result):
    r = result or {}
    if not r.get("submitted") or not r.get("url"):
        return Verdict(FAILED, r.get("error") or "submit did not complete")
    # INDEPENDENT channel: a logged-out re-fetch must show the listing (observe.py).
    from .observe import donecheck_listing
    now = time.time()
    return donecheck_listing(r["url"], r.get("expect_title") or "",
                             at=now, publish_at=r.get("published_at") or (now - 1))


def _real_web_send(actuator=None):
    def execute(rec):
        a = rec.args or {}
        url = a.get("url") or ""
        text = a.get("text") or ""
        msg_sel = a.get("selector") or a.get("message_selector") or ""
        send_sel = a.get("send") or a.get("send_selector") or ""
        act = _space_actuator(actuator, getattr(rec, "job_id", ""))
        if act is None:
            return {"sent": False, "error": "no browser available"}
        try:
            if url:
                act.open(url)
                scope_error = _actuator_scope_error(act, a, url)
                if scope_error:
                    return {"sent": False, "error": scope_error}
            if msg_sel:
                act.type(msg_sel, text)
            if send_sel:
                act.click(send_sel)
        except Exception as e:
            return {"sent": False, "error": f"send failed: {type(e).__name__}: {e}"}
        try:
            page = act.read(4000) or ""
            form = _actuator_form(act, _mission_space(getattr(rec, "job_id", "")))
        except Exception:
            page, form = "", []
        want = str(a.get("success_text") or "").strip()
        composer_still_has_text = bool(text and any(
            str(f.get("value") or "").strip() == str(text).strip() for f in form))
        failure = re.search(r"\b(error|failed|could not|couldn't|rate limit|try again)\b",
                            page, re.I)
        confirmed = bool(not failure and ((want and want.casefold() in page.casefold()) or
                                          (text and text in page and
                                           not composer_still_has_text)))
        return {"case": ({"sent": True, "last_sent_to": a.get("to") or url}
                         if confirmed else {}),
                "sent": True, "confirmed": confirmed,
                "to": a.get("to") or url, "text": text}
    return execute


def _real_send_verify(rec, result):
    r = result or {}
    if not r.get("sent"):
        return Verdict(FAILED, r.get("error") or "message not sent")
    if not r.get("confirmed"):
        return Verdict(INCONCLUSIVE,
                       "send click fired but a fresh thread/composer read did not confirm delivery")
    # This proves the outgoing bubble/composer state, not that the recipient read it.
    return Verdict(VERIFIED, "fresh thread state confirms message sent (not read)")


def _live_actuator():
    from .webact import get_actuator
    return get_actuator()


def _actuator_scope_error(act, args, requested_url=""):
    """Validate the actual post-navigation origin before any read/type/click."""
    try:
        landed = urlsplit(str(act.current_url() or ""))
        requested = urlsplit(str(requested_url or ""))
    except Exception:
        return "browser target identity is unavailable"
    host = (landed.hostname or "").lower()
    allowed = (((args or {}).get("_leash") or {}).get("allowed_domains") or [])
    if allowed:
        ok = any(fnmatch.fnmatchcase(host, str(p).lower()) for p in allowed)
    else:
        first = (requested.hostname or "").lower()
        ok = bool(host and first and (host == first or host.endswith("." + first)))
    return "browser redirect left the Mission domain boundary" if not ok else ""


# ── browse: run the agent loop with the browser tools to DO a web task ───────
# This is the bridge between the durable/gated mission and the browser agent loop
# that actually drives obfuscated, dynamic sites (Facebook Marketplace). `browse`
# fills/navigates (reversible, stops before any irreversible submit); `browse.submit`
# is the single gated click that publishes/sends.
def _browse_dir():
    import os
    d = os.environ.get("COLLIE_NOTES_DIR") or os.path.expanduser("~/.collie/browse")
    os.makedirs(d, exist_ok=True)
    return d


def _mission_space(job_id):
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(job_id or "standalone"))
    return ("mission-" + safe)[:40]


class _BoundBrowserTool:
    """Delegate a browser tool while pinning it to a Mission tab and narrowing args."""
    def __init__(self, inner, space, kind, name="", boundary=None):
        self.inner, self.space, self.kind = inner, space, kind
        self.boundary = boundary or {"domains": [], "first_host": ""}
        self.name, self.tier = getattr(inner, "name", name), getattr(inner, "tier", "always")
        self.description = getattr(inner, "description", "Mission-scoped browser tool")
        schema = getattr(inner, "schema", {}) or {}
        props = dict(schema.get("properties") or {})
        props.pop("space", None); props.pop("adopt", None); props.pop("submit", None)
        self.schema = dict(schema)
        self.schema["properties"] = props

    def provider_schema(self):
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}

    def run(self, args, ctx):
        from .browserbridge import browser_space, space_identity
        clean = dict(args or {})
        clean.pop("space", None); clean.pop("adopt", None)
        if self.kind == "type":
            clean["submit"] = False
        domains = self.boundary.get("domains") or []
        first = self.boundary.get("first_host") or ""

        def allowed(host):
            host = (host or "").lower()
            if not host:
                return True
            if domains:
                return any(fnmatch.fnmatchcase(host, str(p).lower()) for p in domains)
            return not first or host == first or host.endswith("." + first)

        # A previous JS navigation/redirect cannot grant the child authority on a
        # new origin.  Refuse before read/type and suppress any off-scope result.
        current = space_identity(self.space) or {}
        current_host = urlsplit(str(current.get("url") or "")).hostname or ""
        if current_host and not allowed(current_host):
            return "ERROR(browser): live page left the Mission domain boundary"
        if self.kind == "open":
            u = urlsplit(str(clean.get("url") or ""))
            if u.scheme not in ("http", "https") or not u.netloc:
                return "ERROR(browser): Mission browse only opens http(s) pages"
            host = (u.hostname or "").lower()
            if domains and not allowed(host):
                return "ERROR(browser): target domain is outside Mission leash"
            if not domains and first and host != first and not host.endswith("." + first):
                return "ERROR(browser): reversible browse cannot leave its first site"
            # GET endpoints can themselves be consequential.  Activation,
            # unsubscribe, logout and destructive links belong at an outer gated
            # capability, not inside reversible browsing.
            if re.search(r"(?:^|[/?&=])(?:log-?out|sign-?out|unsubscribe|delete|remove|"
                         r"deactivate|activate|verify|confirm)(?:[/?&=]|$)",
                         u.path + "?" + u.query, re.I):
                return "ERROR(browser): consequential navigation requires an outer Mission gate"
            if not first:
                self.boundary["first_host"] = host
                first = host
        with browser_space(self.space):
            out = self.inner.run(clean, ctx)
        landed = space_identity(self.space) or {}
        landed_host = urlsplit(str(landed.get("url") or "")).hostname or ""
        if landed_host and not allowed(landed_host):
            return "ERROR(browser): redirect/navigation left the Mission domain boundary"
        return out


def _restrict_browse_child(h, space, allowed_domains=None):
    """Positive authority list: nothing desktop/MCP/filesystem can survive."""
    allow = {"browser_open", "browser_read", "browser_snapshot", "browser_fields",
             "browser_links", "browser_type", "browser_pick", "browser_advance"}
    for name in list(h.registry._tools):
        if name not in allow:
            h.registry._tools.pop(name, None)
    boundary = {"domains": list(allowed_domains or []), "first_host": ""}
    for name in list(h.registry._tools):
        kind = ("type" if name == "browser_type" else
                "open" if name == "browser_open" else
                "advance" if name == "browser_advance" else "read")
        h.registry._tools[name] = _BoundBrowserTool(
            h.registry._tools[name], space, kind, name, boundary)
    return boundary


def _live_browse(goal, space="mission-standalone", allowed_domains=None):
    import os
    os.environ.setdefault("COLLIE_BROWSER_BRIDGE", "1")   # drive the user's real browser via the bridge
    from .cli import make_harness
    from .browserbridge import space_identity
    from . import settings as _s
    _s.apply()
    provider = _s.get("PROVIDER")
    # Browser manipulation is an execution subtask, not an open-ended architecture problem.  An
    # explicit medium effort keeps the same configured/default model but prevents provider-default
    # deep reasoning from turning two form fields into a ten-minute run.  Both axes remain
    # independently overridable for unusually hard sites.
    browser_model = os.environ.get("COLLIE_BROWSE_MODEL") or _s.get("MODEL")
    browser_effort = os.environ.get("COLLIE_BROWSE_EFFORT") or "medium"
    h = make_harness(_browse_dir(), provider=provider, model=browser_model,
                     project="browse", embed="hash", effort=browser_effort)
    # Prompt text is not an authority boundary.  Keep a positive list, wrap every
    # survivor in this Mission's isolated browser space, and force type.submit off.
    boundary = _restrict_browse_child(h, space, allowed_domains)
    h.self_verify = False
    try:
        h.force_edit = False
    except Exception:
        pass
    # A Mission can issue another bounded browse step after receiving a diagnostic.  Letting one
    # child consume 35 model turns instead made a reversible two-field fill monopolize its entire
    # 600-second watchdog.  Eighteen is ample for multi-step forms while giving the outer planner a
    # timely chance to repair or choose a different route.
    try:
        h.max_turns = max(4, min(35, int(os.environ.get("COLLIE_BROWSE_TURNS", "18"))))
    except (TypeError, ValueError):
        h.max_turns = 18
    prompt = (goal.strip() + "\n\n"
              "Act ONLY through the available reversible browser tools (browser_open / browser_snapshot / "
              "browser_fields / browser_type with a snapshot `ref` or `label` / browser_pick / "
              "browser_advance with an exact snapshot `ref` / browser_links / browser_read). "
              "browser_advance may open menus, choose a non-final step, follow sign-in navigation, "
              "or focus an editor; it refuses final submit/publish/account-creation, CAPTCHA, consent, "
              "commerce, and destructive controls. Enter, script, and upload are unavailable; if a "
              "consequential action is needed, stop and report its exact button so the outer Mission "
              "can gate it. The form is DYNAMIC: picking a "
              "value can REVEAL or CHANGE other fields (e.g. after Vehicle type, Make becomes a dropdown "
              "and Mileage/Body-style/Condition appear).\n"
              "WORKFLOW — repeat until complete:\n"
              "  1. call browser_fields to list the CURRENT fields (label, kind text/richtext/dropdown, value); "
              "if a rich editor is missing, call browser_snapshot and use its exact textbox ref;\n"
              "  2. fill every empty one — browser_type(ref-or-label,text) for text/richtext, browser_pick(label,option) "
              "for dropdowns;\n"
              "  3. call browser_fields AGAIN to catch fields that just appeared or didn't take;\n"
              "  4. keep going until EVERY field the listing needs is filled — fill ALL of them "
              "(vehicle type, year, make, model, mileage, price, description, condition, …), do NOT stop "
              "after the first one or two.\n"
              "EFFICIENCY: for one or two known fields, take one fresh field/snapshot read, one fill pass, "
              "and one verification read. Do not re-read unchanged state. If the same field fails twice, "
              "stop and report the exact failure instead of looping.\n"
              "CRITICAL: do NOT click any IRREVERSIBLE button (Publish, Post, Send, Pay, Place order, "
              "Next-to-publish) — fill everything up to that point and STOP, then report each field you "
              "filled and its final value.")
    res = h.run("browse", prompt)
    try:
        h.memory.close(); h.recorder.close()
    except Exception:
        pass
    answer = res.answer or res.error or ""
    ident = space_identity(space) or {}
    final_host = (urlsplit(str(ident.get("url") or "")).hostname or "").lower()
    first_host = str(boundary.get("first_host") or "").lower()
    domains = boundary.get("domains") or []
    if domains:
        in_scope = not final_host or any(
            fnmatch.fnmatchcase(final_host, str(pattern).lower()) for pattern in domains)
    else:
        in_scope = (not final_host or not first_host or final_host == first_host or
                    final_host.endswith("." + first_host))
    return {"_browse_answer": answer,
            "_scope_error": "" if in_scope else
                "browse ended outside its single-action domain boundary (%s -> %s)" %
                (first_host or "unknown", final_host or "unknown")}


# Independent form re-read (the verify's ground truth): after the acting agent
# stops, snapshot the page's fields straight from the DOM — text/textarea via
# el.value, dropdowns via their label text (which carries the picked value). This
# is a FRESH read, not the agent's self-report, so it can refute a "done" that
# didn't actually fill the form.
_FORM_SNAPSHOT = (
    "JSON.stringify([...document.querySelectorAll('input,textarea,[role=combobox],[contenteditable]')].map(e=>{"
    "var l=e.closest('label');var lab=l?(l.innerText||'').trim().split('\\n')[0]:(e.getAttribute('aria-label')||e.getAttribute('data-testid')||e.getAttribute('role')||e.tagName);"
    "var val=e.getAttribute('role')==='combobox'?(l?(l.innerText||'').replace(/\\n/g,' ').trim():''):(e.value||e.innerText||'');"
    "var meta=[lab,e.type,e.name,e.id,e.autocomplete,e.getAttribute('aria-label')].join(' ');"
    "var sensitive=e.type==='password'||e.type==='email'||e.type==='tel'||/(pass(word|code)?|secret|token|api.?key|captcha|recaptcha|csrf|authenticity|oauth|session.?redirect|cancel.?redirect|redirect.?uri|login.?csrf|page.?instance|sid.?string|control.?id|referer|otp|one.?time|verification.?code|cvv|cvc|card.?number|ssn|social.?security|e.?mail|phone|mobile|street.?address|postal|zip.?code|birth|dob|user.?name)/i.test(meta);"
    "return {label:lab,value:sensitive?'[redacted]':val,sensitive:!!sensitive,filled:!!val};}).filter(x=>x.label&&x.filled))")


_SENSITIVE_FIELD = re.compile(
    r"pass(word|code)?|secret|token|api.?key|captcha|recaptcha|csrf|authenticity|oauth|"
    r"session.?redirect|cancel.?redirect|redirect.?uri|login.?csrf|page.?instance|"
    r"sid.?string|control.?id|referer|"
    r"otp|one.?time|verification.?code|"
    r"cvv|cvc|card.?number|ssn|social.?security|e.?mail|phone|mobile|"
    r"street.?address|postal|zip.?code|birth|dob|user.?name", re.I)


def _sanitize_form(form):
    """Never persist browser credentials/PII in Mission case, events or snapshots."""
    out = []
    for item in form if isinstance(form, list) else []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")[:160]
        raw = item.get("value")
        filled = bool(item.get("filled", raw not in (None, "")))
        sensitive = bool(item.get("sensitive") or _SENSITIVE_FIELD.search(label))
        if not label or not filled:
            continue
        out.append({"label": label,
                    "value": "[redacted]" if sensitive else str(raw or "")[:1000],
                    **({"sensitive": True} if sensitive else {})})
    return out


def _read_form_state(space=""):
    from . import browserbridge as _bb
    try:
        r = _bb._call({"action": "form_snapshot", "space": space} if space else
                      {"action": "form_snapshot"})
        data = r.get("data", r) if isinstance(r, dict) else None
        fields = data.get("fields") if isinstance(data, dict) else []
        actions = data.get("actions") if isinstance(data, dict) else []
        safe_actions = [{"label": str(a.get("label") or "")[:80],
                         "disabled": bool(a.get("disabled"))}
                        for a in actions if isinstance(a, dict) and a.get("label")]
        return _sanitize_form(fields or []), safe_actions[:20]
    except Exception:
        return [], []


def _read_form(space=""):
    return _read_form_state(space)[0]


def _actuator_form(act, space):
    if act is not None and hasattr(act, "form_snapshot"):
        try:
            data = act.form_snapshot() or {}
            return _sanitize_form(data.get("fields") or [])
        except Exception:
            return []
    if act is not None and hasattr(act, "eval"):
        try:
            data = act.eval(_FORM_SNAPSHOT)
            return _sanitize_form(json.loads(data) if isinstance(data, str) else (data or []))
        except Exception:
            return []
    return _read_form(space)


def _locks_current_page(args):
    """True only for an explicit read-only inspection that forbids navigation.

    Domain pinning prevents cross-site drift, but an OAuth child can still guess
    a different URL on the same host.  When the caller says CURRENT page and no
    navigation/reload/open, bind the reversible step to the exact starting URL.
    """
    if not _explicit_read_only_browse(args or {}):
        return False
    goal = str((args or {}).get("goal") or (args or {}).get("task") or "")
    current = bool(re.search(r"(?i)\bcurrent\b|当前|本页", goal))
    no_nav = bool(re.search(
        r"(?i)\bwithout\s+(?:any\s+)?(?:navigating|navigation|reloading|reload|opening)\b|"
        r"\bdo\s+not\s+(?:navigate|reload|open)\b|"
        r"\bno\s+(?:navigation|reload)\b|不要.{0,30}(?:导航|刷新|打开)|禁止.{0,20}(?:导航|刷新)",
        goal))
    return current and no_nav


def _safe_page_name(url):
    """Describe browser drift without persisting OAuth/query credentials."""
    parsed = urlsplit(str(url or ""))
    return (parsed.hostname or "") + (parsed.path or "/")


def _redacted_page_url(url):
    """Persist page identity without query credentials, OAuth state, or fragments."""
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return "%s://%s%s" % (parsed.scheme, parsed.netloc, parsed.path or "/")


def _page_url_digest(url):
    """Opaque exact-URL binding used for TOCTOU without storing the URL itself."""
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()


def _real_browse(runner=None, form_reader=None):
    def execute(rec):
        from .browserbridge import space_identity
        args = rec.args or {}
        goal = args.get("goal") or args.get("task") or ""
        space = _mission_space(getattr(rec, "job_id", ""))
        domains = (args.get("_leash") or {}).get("allowed_domains") or []
        lock_current = _locks_current_page(args)
        start_ident = (space_identity(space) or {}) if lock_current and runner is None else {}
        start_url = str(start_ident.get("url") or "")
        # The restricted browser child receives only this goal, never the outer Mission case.  A
        # planner instruction such as "use the prepared copy from the case" therefore forces the
        # child to invent the missing body.  Refuse before touching the browser unless every
        # expected value is literally embedded in the self-contained payload.
        if not _explicit_read_only_browse(args):
            expect = args.get("expect") or {}
            flat_goal = re.sub(r"\s+", " ", str(goal)).strip().casefold()
            missing = [str(k) for k, v in expect.items()
                       if re.sub(r"\s+", " ", str(v)).strip().casefold() not in flat_goal]
            case_ref = re.search(
                r"(?i)\b(?:from|in|use)\s+(?:the\s+)?(?:case(?:\s+draft)?|draft|context|"
                r"previous\s+(?:message|result)|above)\b|"
                r"(?:case|草稿|上下文|上文).{0,20}(?:copy|text|body|文案|正文)", str(goal))
            if missing or case_ref:
                reason = ("browse payload is not self-contained: embed the complete exact value for "
                          + (", ".join(missing) if missing else "every referenced case/draft field")
                          + " in args.goal and args.expect")
                return {"case": {"browsed": False, "browse_result": reason[:600]},
                        "result": reason, "form": [], "form_actions": [], "page": {},
                        "contract_error": reason}
        try:
            from .workidentity import resolve_references
            goal = resolve_references(goal)
        except RuntimeError as exc:
            reason = "connected work identity reference could not be resolved: %s" % exc
            return {"case": {"browsed": False, "browse_result": reason[:600]},
                    "result": reason, "form": [], "form_actions": [], "page": {},
                    "contract_error": reason}
        raw_out = runner(goal) if runner else _live_browse(goal, space, domains)
        scope_error = ""
        if isinstance(raw_out, dict) and "_browse_answer" in raw_out:
            out = raw_out.get("_browse_answer") or ""
            scope_error = str(raw_out.get("_scope_error") or "")
        else:
            out = raw_out
        # Child summaries are durable case/event material.  Defense in depth for
        # a child that ignored the prompt and echoed signup/contact credentials.
        out = str(out or "")
        out = re.sub(r"(?i)((?:password|passcode|secret|token|otp|e-?mail|phone|"
                     r"card(?: number)?)\s*(?:is|=|:)\s*)\S+", r"\1[redacted]", out)
        out = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
                     "[redacted-email]", out, flags=re.I)
        if form_reader:
            form, form_actions = _sanitize_form(form_reader()), []
        else:
            form, form_actions = _read_form_state(space)
        # Page identity is evidence too.  A platform/site is not an HTML form
        # field, and treating it as one produced impossible contracts such as
        # expect={platform: Twitter/X}.  Keep only origin-level identity and a
        # bounded title; query strings/fragments may carry credentials or PII.
        ident = (space_identity(space) or {}) if runner is None else {}
        end_url = str(ident.get("url") or "")
        if lock_current and start_url and end_url and end_url != start_url:
            locked_error = (
                "read-only current-page browse navigated away from its locked URL "
                "(%s -> %s)" % (_safe_page_name(start_url), _safe_page_name(end_url)))
            scope_error = scope_error or locked_error
        parsed = urlsplit(str(ident.get("url") or ""))
        page = {"host": (parsed.hostname or "").lower(),
                "title": str(ident.get("title") or "")[:160]}
        return {"case": {"browsed": True, "browse_result": (out or "")[:600]},
                "result": out, "form": form, "form_actions": form_actions,
                "page": page, **({"scope_error": scope_error} if scope_error else {})}
    return execute


def _explicit_read_only_browse(args):
    """Recognize only an unmistakable no-write inspection request.

    The explicit boolean is the primary contract.  The narrow language fallback
    exists because planners can omit an optional JSON field even while spelling
    out "inspect; do not change or submit anything" in the goal.  Requiring both
    a read verb and a no-write clause keeps ordinary failed form fills outside
    this path.
    """
    a = args or {}
    # An explicit false is just as meaningful as true. Falling through to the
    # heuristic let a failed form fill masquerade as a verified inspection.
    if "read_only" in a:
        return a.get("read_only") is True
    goal = str(a.get("goal") or a.get("task") or "")
    read_intent = bool(re.search(
        r"(?i)\b(inspect|review|check|identify|read|observe|audit|look\s+at)\b|"
        r"查看|检查|核实|审查|识别", goal))
    no_write = bool(re.search(
        r"(?i)\bread[- ]only\b|\bwithout\s+(?:making\s+)?(?:changes?|changing|"
        r"submitting|posting|publishing|sending|editing|filling|clicking)\b|"
        r"\bdo\s+not\s+(?:register|message|change|create|submit|post|publish|send|"
        r"edit|fill|click)\b|只读|不要.{0,80}(?:修改|提交|发布|注册|发送|创建|填写|点击)",
        goal))
    # Planners naturally produce composite clauses such as "without navigating,
    # reloading, opening, clicking, typing, or submitting".  The old expression
    # only recognized the first word after ``without`` and therefore treated
    # semantic page expectations as form fields whenever ``read_only`` was
    # omitted.  Require an actual mutation verb later in the same bounded clause;
    # "without navigating" alone is deliberately not enough, because a caller
    # could still intend to fill the current page.
    composite_no_write = bool(re.search(
        r"(?is)\bwithout\b[^.\n]{0,180}\b(?:submitting|posting|publishing|sending|"
        r"editing|filling|clicking|typing|changing|creating|registering)\b",
        goal))
    return read_intent and (no_write or composite_no_write)


def _browse_verify(rec, result):
    """Done-check by an INDEPENDENT re-read of the form, not the agent's self-report.
    If the caller passed `expect` ({label: value}), assert each value is actually
    present in the re-read form (differential); otherwise confirm the form is
    substantially filled. A 'done' over an empty form is refuted here."""
    r = result or {}
    form = r.get("form") or []
    expect = (rec.args or {}).get("expect") or {}
    read_only = _explicit_read_only_browse(rec.args or {})
    if r.get("scope_error"):
        return Verdict(FAILED, str(r.get("scope_error"))[:300])
    if r.get("contract_error"):
        return Verdict(FAILED, str(r.get("contract_error"))[:300])
    if not r.get("result") and not form:
        return Verdict(FAILED, "browse produced no result")

    # A deliberate inspection/navigation action has no form to fill.  It still
    # needs independent evidence: the bridge re-read of the live page identity.
    # Without the explicit flag, an empty form remains inconclusive so a failed
    # fill cannot disguise itself as successful browsing.
    # ``expect`` has form-fill semantics.  A planner can still attach semantic
    # inspection goals such as {account: "authenticated identity"}; those are
    # not labels/values that should suddenly turn an explicit no-write read
    # into a failed form submission.  Explicit read-only intent wins, and the
    # independent evidence remains the freshly reread page origin below.
    if read_only:
        page = r.get("page") or {}
        host = str(page.get("host") or "").strip().lower()
        title = str(page.get("title") or "").strip()
        if not host:
            return Verdict(INCONCLUSIVE,
                           "read-only browse returned no independently confirmed page")
        ev = Observation(channel="browser-page-reread", at=1, ok=True, asserted=True,
                         detail=(host + ((" · " + title) if title else "")))
        return Verdict(VERIFIED, "independently confirmed read-only browse on " + host, (ev,))

    def _norm(s):                                    # ignore $, commas, spacing ("9500" == "$9,500")
        return re.sub(r"[^a-z0-9]", "", str(s).lower())

    def _present(label, val):
        lab, v = str(label).lower(), _norm(val)
        return bool(v) and any(lab in str(f.get("label", "")).lower() and v in _norm(f.get("value", ""))
                               for f in form)

    def _value_present(val):
        v = _norm(val)
        return bool(v) and any(v in _norm(f.get("value", "")) for f in form)

    def _value_exact(val):
        v = _norm(val)
        return bool(v) and any(v == _norm(f.get("value", "")) for f in form)

    def _page_present(val):
        page = r.get("page") or {}
        wanted = _norm(val)
        actual = _norm("%s %s" % (page.get("host", ""), page.get("title", "")))
        if wanted and wanted in actual:
            return True
        # Twitter and X are one platform but neither spelling is a substring of
        # the other.  The evidence remains the independently read x.com host.
        return wanted in ("x", "twitter", "twitterx") and (
            str(page.get("host") or "").lower() == "x.com" or
            str(page.get("host") or "").lower().endswith(".x.com"))

    def _expected_present(label, val):
        key = re.sub(r"[^a-z0-9_]", "", str(label).lower())
        try:
            from .workidentity import is_reference
            identity_reference = is_reference(val)
        except Exception:
            identity_reference = False
        if identity_reference:
            # The trusted capability resolved the reference before acting.  The
            # independent DOM reread intentionally redacts the value; prove that
            # the intended sensitive field is filled without re-exposing it.
            lab = str(label).lower()
            return any(lab in str(f.get("label", "")).lower() and
                       bool(f.get("sensitive")) and f.get("value") == "[redacted]"
                       for f in form)
        if key in ("platform", "site", "origin"):
            return _page_present(val)
        # Rich editors expose unstable accessibility labels/data-testid values
        # (e.g. tweetTextarea_0).  Semantic *_text/body/content expectations are
        # verified against the fresh value of every filled editor, not a guessed
        # label, while ordinary form fields retain strict label+value matching.
        if key in ("text", "title", "body", "content", "message", "tweet_text", "post_text") or key.endswith("_text"):
            # A prefix proves only that the child started the requested copy.  It does not prove the
            # rest was preserved rather than invented (including links).  Rich/social payloads are
            # externally consequential, so verify the complete normalized value exactly.
            return _value_exact(val)
        return _present(label, val)

    if expect:
        missing = [k for k, v in expect.items() if not _expected_present(k, v)]
        if missing:
            return Verdict(FAILED, "form fields NOT confirmed filled: " + ", ".join(missing))
        final_actions = [a for a in (r.get("form_actions") or [])
                         if str(a.get("label") or "").lower() in
                         ("post", "publish", "send", "submit", "save", "next", "continue")]
        if final_actions and not any(not a.get("disabled") for a in final_actions):
            return Verdict(FAILED, "form is filled but final action remains disabled: " +
                           ", ".join(str(a.get("label")) for a in final_actions))
        ev = Observation(channel="form-reread", at=1, ok=True, asserted=True,
                         detail="; ".join("%s=%s" % (k, v) for k, v in expect.items()))
        return Verdict(VERIFIED, "independently confirmed %d field(s) filled" % len(expect), (ev,))
    # no expected values -> at least confirm the form is substantially filled
    if len(form) >= 3:
        return Verdict(VERIFIED, "form re-read shows %d filled field(s)" % len(form))
    return Verdict(INCONCLUSIVE,
                   "could not confirm the form was filled (re-read found %d field(s))" % len(form))


def _space_actuator(actuator, job_id):
    act = actuator or _live_actuator()
    if act is not None and hasattr(act, "for_space"):
        act = act.for_space(_mission_space(job_id))
    return act


_FINAL_BUTTON_EQUIVALENTS = (
    # The browser tree exposes the page's locale, while the planning model may describe the same
    # final action in the user's language, in English, or as a bilingual label ("保存 / Save").
    # Keep this deliberately limited to common final-action verbs.  `_find_button` still requires
    # one unique enabled live button, so translation never turns a vague label into a guessed click.
    frozenset(("save", "save changes", "保存", "保存更改", "guardar", "enregistrer",
               "speichern", "salva", "opslaan", "zapisz", "сохранить", "저장", "kaydet",
               "lưu", "บันทึก", "simpan")),
    frozenset(("publish", "发布", "發佈", "publier", "veröffentlichen", "publicar",
               "pubblica", "publiceren", "opublikuj", "опубликовать", "公開", "게시",
               "yayınla")),
    frozenset(("post", "发帖", "發文", "投稿", "게시하기")),
    frozenset(("send", "发送", "傳送", "envoyer", "senden", "enviar", "invia",
               "verzenden", "wyślij", "отправить", "送信", "보내기", "gönder")),
    frozenset(("submit", "提交", "送出", "soumettre", "absenden", "enviar",
               "invia", "indienen", "prześlij", "отправить", "送信", "제출")),
)


def _button_labels(button):
    raw = str(button or "").strip().casefold()
    # A bilingual description is not normally the DOM's literal accessible name.  Treat each side
    # as a semantic hint, but never as permission to match arbitrary substrings.
    parts = {p.strip() for p in re.split(r"\s*(?:/|｜)\s*", raw) if p.strip()}
    exact = {raw} if raw else set()
    semantic = set(parts)
    seeds = set(parts)
    for group in _FINAL_BUTTON_EQUIVALENTS:
        if seeds.intersection(group):
            semantic.update(group)
    return exact, semantic


def _find_button(snapshot, button, include_disabled=False):
    exact, semantic = _button_labels(button)
    hits = []
    for line in str((snapshot or {}).get("snapshot") or "").splitlines():
        m = re.search(r"\[([^\]]+)\]\s+(button|link|menuitem)\s+\"([^\"]+)\"", line)
        label = m.group(3).strip().casefold() if m else ""
        if m and label in semantic:
            if re.search(r"×\s*[2-9]\d*|identical siblings", line, re.I):
                return None
            hits.append({"ref": m.group(1), "role": m.group(2),
                         "label": label,
                         "line": line.strip(),
                         "disabled": bool(re.search(r"\(disabled\)|\[disabled\]|aria-disabled", line, re.I))})
    buttons = [h for h in hits if h["role"] in ("button", "menuitem")]
    exact_buttons = [h for h in buttons if h["label"] in exact]
    if exact_buttons:
        enabled = [h for h in exact_buttons if not h["disabled"]]
        if len(enabled) == 1:
            return enabled[0]
        if include_disabled and len(exact_buttons) == 1:
            return exact_buttons[0]
        return None
    if buttons:
        enabled = [h for h in buttons if not h["disabled"]]
        if len(enabled) == 1:
            return enabled[0]
        if include_disabled and len(buttons) == 1:
            return buttons[0]
        return None
    exact_links = [h for h in hits if h["role"] == "link" and h["label"] in exact
                   and not h["disabled"]]
    if exact_links:
        return exact_links[0] if len(exact_links) == 1 else None
    links = [h for h in hits if h["role"] == "link" and not h["disabled"]]
    return links[0] if len(links) == 1 else None


def _browse_target_snapshot(actuator):
    def snap(args, job_id):
        button = (args or {}).get("button") or (args or {}).get("text") or "Publish"
        if re.search(r"\b(pay|purchase|buy|checkout|place\s+order)\b",
                     str(button), re.I):
            raise RuntimeError("commerce requires a dedicated pay capability with a bound amount")
        act = _space_actuator(actuator, job_id)
        if act is None or not hasattr(act, "page_identity") or not hasattr(act, "snapshot"):
            raise RuntimeError("cannot snapshot the browser target")
        if hasattr(act, "show"):
            act.show()
        ident = act.page_identity() or {}
        tree, target = {}, None
        # GitHub and some other consent pages intentionally render the final
        # control disabled for a short safety delay.  A one-shot snapshot made
        # a valid, already verified target look missing and sent the Mission
        # back through planning.  Re-read only when the exact unique target is
        # present-but-disabled; ambiguity and true absence still fail at once.
        for attempt in range(4):
            tree = act.snapshot() or {}
            target = _find_button(tree, button)
            if target:
                break
            pending = _find_button(tree, button, include_disabled=True)
            if not pending or not pending.get("disabled") or attempt >= 3:
                break
            time.sleep(1)
        full_url = tree.get("url") or ident.get("url")
        if not full_url or not target:
            raise RuntimeError("target page/button is missing or ambiguous; prepare the page again")
        u = urlsplit(str(full_url or ""))
        form = _actuator_form(act, _mission_space(job_id))
        form_json = json.dumps(form, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return {"space": _mission_space(job_id), "tab_id": ident.get("tab_id"),
                "title": ident.get("title") or "", "url": _redacted_page_url(full_url),
                "url_digest": _page_url_digest(full_url),
                "origin": "%s://%s" % (u.scheme, u.netloc),
                "button": str(button), "ref": target["ref"],
                "target": target["line"],
                "form_digest": hashlib.sha256(form_json.encode("utf-8")).hexdigest(),
                "form": form[:20]}
    return snap


def _browse_target_unchanged(actuator):
    def unchanged(rec):
        old = rec.snapshot or {}
        act = _space_actuator(actuator, getattr(rec, "job_id", ""))
        if act is None:
            return False
        if hasattr(act, "show"):
            act.show()
        ident = act.page_identity() or {}
        tree = act.snapshot() or {}
        target = _find_button(tree, old.get("button"))
        full_url = tree.get("url") or ident.get("url")
        form = _actuator_form(act, _mission_space(getattr(rec, "job_id", "")))
        form_json = json.dumps(form, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        form_digest = hashlib.sha256(form_json.encode("utf-8")).hexdigest()
        url_matches = (_page_url_digest(full_url) == old.get("url_digest")
                       if old.get("url_digest") else full_url == old.get("url"))
        return bool(target and ident.get("tab_id") == old.get("tab_id") and
                    url_matches and
                    target.get("line") == old.get("target") and
                    target.get("ref") == old.get("ref") and
                    form_digest == old.get("form_digest"))
    return unchanged


def _real_browse_submit(actuator=None):
    def execute(rec):
        button = (rec.args or {}).get("button") or (rec.args or {}).get("text") or "Publish"
        act = _space_actuator(actuator, getattr(rec, "job_id", ""))
        if act is None:
            return {"submitted": False, "error": "no browser available"}
        try:
            ref = (getattr(rec, "snapshot", None) or {}).get("ref")
            if not ref or not hasattr(act, "click_ref"):
                return {"submitted": False, "error": "approved button identity is missing"}
            if hasattr(act, "trusted_click_ref"):
                act.trusted_click_ref(ref)
            else:
                act.click_ref(ref)
        except Exception as e:
            return {"submitted": False, "error": "publish click failed: %s: %s" % (type(e).__name__, e)}
        old = getattr(rec, "snapshot", None) or {}
        success_text = str((rec.args or {}).get("success_text") or "").strip()
        success_url = str((rec.args or {}).get("success_url_contains") or "").strip()
        confirmed, new_url, last_error = False, "", ""
        # A trusted click can return before an OAuth redirect or SPA success
        # state lands.  Re-observe the page for a short bounded window; never
        # click again.  This converts a real success from "inconclusive" without
        # weakening the evidence requirement.
        for attempt in range(5):
            try:
                ident = act.page_identity() or {}
                tree = act.snapshot() or {}
            except Exception as e:
                last_error = "clicked, but fresh postcondition read failed: %s" % e
                tree, ident = {}, {}
            new_url = str(tree.get("url") or ident.get("url") or "")
            page = "\n".join((str(ident.get("title") or ""),
                               str(tree.get("snapshot") or "")))
            failure = re.search(r"\b(error|required|could not|couldn't|failed|captcha|"
                                r"rate limit|try again|something went wrong)\b", page, re.I)
            explicit = ((success_text and success_text.casefold() in page.casefold()) or
                        (success_url and success_url in new_url))
            marker = re.search(r"\b(published|posted|sent successfully|your post is live|"
                               r"view post|successfully published)\b", page, re.I)
            target_gone = _find_button(tree, old.get("button")) is None
            navigated = bool(new_url and (
                _page_url_digest(new_url) != old.get("url_digest")
                if old.get("url_digest") else new_url != str(old.get("url") or "")))
            permalink = re.search(
                r"/(?:posts?|status|items?|listings?|p|reels?|videos?|updates?)/[^/?#]+",
                urlsplit(new_url).path, re.I) if new_url else None
            confirmed = bool(not failure and (((explicit or marker) and
                                                (navigated or target_gone)) or
                                               (permalink and navigated and target_gone)))
            if confirmed or attempt >= 4:
                break
            if hasattr(act, "wait"):
                act.wait(0.75)
            else:
                time.sleep(0.75)
        return {"case": {"published": True} if confirmed else {},
                "submitted": True, "confirmed": confirmed, "button": button,
                "target": _redacted_page_url(new_url),
                "error": last_error if not confirmed and last_error else "",
                "postcondition":
                    ("fresh success state observed" if confirmed else
                     "click fired; no fresh publication evidence")}
    return execute


def _browse_submit_verify(rec, result):
    r = result or {}
    if not r.get("submitted"):
        return Verdict(FAILED, r.get("error") or "publish click did not fire")
    if not r.get("confirmed"):
        return Verdict(INCONCLUSIVE,
                       r.get("error") or "click fired but publication was not independently observed")
    return Verdict(VERIFIED, "fresh page state confirms %r completed" % r.get("button"))


def _stub_browse(rec):
    goal = (rec.args or {}).get("goal") or ""
    # a canned re-read so the (real) _browse_verify has a form to check against
    form = [{"label": "Make", "value": "Toyota"}, {"label": "Model", "value": "Corolla"},
            {"label": "Price", "value": "$9,500"}]
    return {"case": {"browsed": True}, "result": "(stub) filled the form for: " + goal[:60],
            "form": form}


def _stub_browse_submit(rec):
    return {"case": {"published": True}, "submitted": True, "confirmed": True,
            "button": (rec.args or {}).get("button") or "Publish"}


# ── code: coding is a capability like any other — run collie's coding agent ───
# The delegate's positioning is a human-delegate; coding is ONE function under it.
# `code` runs a filesystem-confined read/edit/search loop. General command execution
# stays unavailable inside Mission; a real edit therefore hands off as INCONCLUSIVE
# unless an injected, separately sandboxed runner supplies executed verification.
class _BoundCodeTool:
    """Confine every path-bearing code tool to one approved real workspace."""
    def __init__(self, inner, root, path_key="path", default_path=None):
        self.inner, self.root = inner, os.path.realpath(root)
        self.path_key, self.default_path = path_key, default_path
        self.name, self.tier = inner.name, getattr(inner, "tier", "always")
        self.description = getattr(inner, "description", "Mission-scoped code tool")
        self.schema = getattr(inner, "schema", {}) or {}

    def provider_schema(self):
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}

    def run(self, args, ctx):
        clean = dict(args or {})
        raw = clean.get(self.path_key, self.default_path)
        if raw is None:
            return "ERROR(code): path is required"
        try:
            raw = str(raw)
            candidate = os.path.realpath(raw if os.path.isabs(raw)
                                         else os.path.join(self.root, raw))
            if os.path.commonpath([self.root, candidate]) != self.root:
                return "ERROR(code): path is outside the approved Mission workspace"
        except (OSError, ValueError):
            return "ERROR(code): invalid or cross-volume path"
        clean[self.path_key] = candidate
        return self.inner.run(clean, ctx)


def _restrict_code_child(h, root):
    # `glob` can traverse directory symlinks and general shell/execute tools can
    # escape any path wrapper. code_search already provides safe repo discovery.
    # This is the whole generic tool contract; the only other hand a Mission code
    # slice gets is the fixed-command host check registered after this pass (see
    # `harness.code_check`), which is not a generic tool and takes no arguments.
    allow = {"read_file", "write_file", "edit_file", "grep",
             "plan", "undo", "code_search"}
    for name in list(h.registry._tools):
        if name not in allow:
            h.registry._tools.pop(name, None)
    for name in ("read_file", "write_file", "edit_file"):
        if name in h.registry._tools:
            h.registry._tools[name] = _BoundCodeTool(h.registry._tools[name], root)
    if "grep" in h.registry._tools:
        h.registry._tools["grep"] = _BoundCodeTool(
            h.registry._tools["grep"], root, default_path=".")


def _code_session_id(mission_id, workspace):
    material = str(mission_id or workspace or "mission-code")
    return "mission-code-" + hashlib.sha256(
        material.encode("utf-8", "replace")).hexdigest()[:24]


def _optional_nonnegative_int(value, name):
    """Parse an optional durable authority value without truncation or NaN."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("%s must be a non-negative integer" % name)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("%s must be a non-negative integer" % name)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a non-negative integer" % name) from None
    if isinstance(value, str) and str(parsed) != value.strip():
        raise ValueError("%s must be a non-negative integer" % name)
    if parsed < 0:
        raise ValueError("%s must be a non-negative integer" % name)
    return parsed


# ── how many logical turns one durable code slice may take ──────────────────
# Three inputs that mean three different things, and used to collapse into one:
#
#   absent (None/"")  nobody chose.  Use the configured scheduling slice, which
#                     is what every pre-existing caller relied on.
#   explicit 0        the dedicated ordinary-code profile's own choice, and the
#                     same value the native loop uses for interactive work: no
#                     arbitrary turn ceiling.  Chopping one small feature into
#                     24-turn fragments costs a resumed prompt, a re-read of the
#                     repository and a full host check every time it resumes;
#                     that is where a real 5-slice, 113-model-call run went.
#   explicit positive bounded scheduling on purpose (overnight uses 3), clamped
#                     to a sane range.
#
# "Unlimited" is unlimited in LOGICAL TURNS only.  Real work stays bounded by
# four independent things that all outlive this function: the Mission
# model-call ledger (every turn costs at least one call, and the loop breaks on
# that ceiling), the Mission's per-step wall-clock leash enforced by the driver
# watchdog against a killable worker process, Stop, and the Mission's own
# active wall-time budget.  An unlimited slice therefore REQUIRES a positive
# model-call budget: with nothing counting down, "unlimited" would be literally
# unbounded, so the configured slice is used instead.
#
# Timeout responsiveness, for the record: when a slice outlives the step leash
# the driver cancels the worker's process tree and fences the Mission as
# recovery_required rather than pretending it checkpointed.  That is a real,
# visible cost of an unlimited slice on a very slow repository, and it is the
# reason a positive `slice_turns` remains available and remains what overnight
# uses.
_DEFAULT_CODE_SLICE_TURNS = 24
_MAX_CODE_SLICE_TURNS = 50
# How much of a coding run's final report the Mission case keeps.  The case is
# reloaded, compacted and handed to a planner constantly, so it cannot hold an
# unbounded report; the durable session journal holds the complete text and the
# delivery record says explicitly which of the two is whole.
CODE_DELIVERY_ANSWER_CHARS = 4000


def _code_slice_turn_cap(slice_turns, model_call_limit):
    """Resolve one slice's logical turn ceiling. Returns (cap, mode); 0 = unlimited."""
    try:
        configured = int(os.environ.get(
            "COLLIE_CODE_SLICE_TURNS",
            os.environ.get("COLLIE_CODE_TURNS", str(_DEFAULT_CODE_SLICE_TURNS))))
    except (TypeError, ValueError, OverflowError):
        configured = _DEFAULT_CODE_SLICE_TURNS
    default_cap = max(1, min(_MAX_CODE_SLICE_TURNS, configured))
    if slice_turns is None or slice_turns == "" or isinstance(slice_turns, bool):
        return default_cap, "configured"
    try:
        requested = int(slice_turns)
    except (TypeError, ValueError, OverflowError):
        # Non-finite or unparseable authority is not an instruction to run
        # forever; it is a broken value, and the configured slice is the safe
        # reading of it.
        return default_cap, "configured"
    if requested > 0:
        return max(1, min(_MAX_CODE_SLICE_TURNS, requested)), "bounded"
    if requested < 0:
        return default_cap, "configured"
    if model_call_limit is None or int(model_call_limit) <= 0:
        return default_cap, "configured_without_call_budget"
    return 0, "unlimited"


def _default_code_verifier(workspace, result, command="", baseline_digest="",
                           timeout_seconds=300, *, patch_attributed=False,
                           agent_post_tree_digest="", cancelled=None, on_event=None):
    """Run one exact, pre-authorized host check and bind it to current bytes.

    ``cancelled`` is this Mission's own stop predicate.  It is handed to the
    verifier so a Stop pressed while the repository command is running kills that
    owned process tree and yields cancelled evidence, instead of leaving a child
    writing files after the Mission has already moved on.
    """
    command = str(command or "").strip()
    if not command:
        return {"verified": bool(getattr(result, "verified", False)),
                "detail": "no host verification command configured", "evidence": None}
    from .verification import run_verification_command
    try:
        parsed_timeout = _optional_nonnegative_int(
            timeout_seconds, "verify_timeout_seconds")
    except ValueError as exc:
        return {"verified": False, "detail": str(exc), "evidence": None}
    evidence = run_verification_command(
        command, workspace, timeout=max(1, min(3600, parsed_timeout or 300)),
        source="mission_code_profile", after_last_edit=True,
        cancelled=cancelled, on_event=on_event)
    return _bind_check_evidence(
        evidence, baseline_digest=baseline_digest,
        patch_attributed=patch_attributed,
        agent_post_tree_digest=agent_post_tree_digest)


def _bind_check_evidence(evidence, baseline_digest="", *, patch_attributed=False,
                         agent_post_tree_digest=""):
    """Decide one host check's verdict from its receipt and the agent boundary.

    Shared by the check the host runs after the slice and by an in-slice receipt
    the host reuses, so "when is a green command a verified Mission patch" has
    exactly one implementation and cannot drift between the two paths.
    """
    evidence = dict(evidence or {})
    if evidence.get("cancelled"):
        # A stop is never a verdict about the code.  Report it as its own state
        # so nothing downstream can read "not failed" as "fine".
        evidence["patch_attributed"] = bool(patch_attributed)
        evidence["agent_post_tree_digest"] = str(agent_post_tree_digest or "")
        evidence["agent_boundary_matches"] = bool(
            agent_post_tree_digest and
            str(evidence.get("tree_digest") or "") == str(agent_post_tree_digest))
        return {"verified": False, "cancelled": True,
                "detail": "host verification was cancelled before it could finish",
                "evidence": evidence}
    # The verifier is allowed to execute repository code and can therefore
    # create files of its own (for example __pycache__, coverage data, or build
    # output).  Those bytes are part of the physical workspace boundary, but
    # they are not evidence that the coding agent produced a patch.  Bind the
    # check to the snapshot captured immediately after the agent loop, and use
    # only durable agent/reconciliation provenance for patch attribution.
    agent_boundary = str(agent_post_tree_digest or "")
    boundary_matches = bool(
        agent_boundary and str(evidence.get("tree_digest") or "") == agent_boundary)
    evidence["agent_post_tree_digest"] = agent_boundary
    evidence["agent_boundary_matches"] = boundary_matches
    # Provenance is about bytes that still exist, not whether an agent changed
    # something at any earlier point in the Mission.  A later slice may cleanly
    # revert the prior patch to the original baseline; verifier/build artifacts
    # created after this boundary must not keep that historical mutation alive.
    agent_differs_from_baseline = bool(
        baseline_digest and agent_boundary and agent_boundary != baseline_digest)
    current_patch_attributed = bool(
        patch_attributed and agent_differs_from_baseline)
    evidence["agent_differs_from_baseline"] = agent_differs_from_baseline
    evidence["patch_attributed"] = current_patch_attributed
    verified = bool(
        evidence.get("passed") and boundary_matches and current_patch_attributed)
    detail = ("configured host check passed against the current Mission patch" if verified else
              "check passed but the Mission produced no attributed patch"
              if evidence.get("passed") and boundary_matches else
              "workspace changed between the agent boundary and host verification"
              if evidence.get("passed") and not boundary_matches else
              "configured check failed (exit %s)" % evidence.get("exit_code"))
    return {"verified": verified, "detail": detail, "evidence": evidence}


def _acquire_code_session_lease(sid, cwd, mission_id):
    """Take this durable code session's execution lease, or refuse without effect.

    The lease is acquired BEFORE the journal is loaded and released only after the
    last receipt, so no second executor — another Mission worker, a `collie` CLI
    turn on the same session, a second daemon — can interleave a conversation into
    one transcript or edit the same workspace behind our snapshots.  Refusing here
    is safe: nothing has been read, written or launched yet.
    """
    from . import session_owner
    try:
        lease = session_owner.try_acquire(
            sid, label="mission-code",
            meta={"mission_id": str(mission_id or "")[:120], "cwd": str(cwd)[:400]})
    except (OSError, ValueError) as exc:
        return {"answer": "durable code session lease could not be taken: %s: %s"
                          % (type(exc).__name__, exc),
                "verified": False, "continue_needed": False,
                "recovery_required": False,
                "needs_human": True, "session_id": sid}
    if lease is None:
        return {"answer": "another executor already owns this durable code session; "
                          "no edit was attempted",
                "verified": False, "continue_needed": False,
                # Nothing was read, written or launched, so this is an ordinary
                # refusal and must never be escalated as an uncertain effect.
                "recovery_required": False,
                "needs_human": True, "session_id": sid}
    return lease


def _live_code(goal, workspace=None, mission_id=None, host_verifier=None,
               execution_profile=None, worker_profile=None, verify_command="", session_id="",
               baseline_tree_digest="", expected_tree_digest="", slice_turns=None,
               verify_timeout_seconds=None, max_session_storage_bytes=None,
               max_model_calls=None, runs_db="", mission_store_path="",
               mission_run_token="", cancelled=None, on_event=None):
    """Run one durable code slice while holding its session execution lease.

    The lease is taken inside the slice (only once the workspace and authority are
    known to be valid, so a refused configuration never touches the session store)
    and released here, after the final receipt and host-check evidence exist.
    """
    holder = {}
    try:
        return _live_code_slice(
            goal, workspace, mission_id=mission_id, host_verifier=host_verifier,
            execution_profile=execution_profile, worker_profile=worker_profile,
            verify_command=verify_command, session_id=session_id,
            baseline_tree_digest=baseline_tree_digest,
            expected_tree_digest=expected_tree_digest, slice_turns=slice_turns,
            verify_timeout_seconds=verify_timeout_seconds,
            max_session_storage_bytes=max_session_storage_bytes,
            max_model_calls=max_model_calls, runs_db=runs_db,
            mission_store_path=mission_store_path,
            mission_run_token=mission_run_token, cancelled=cancelled,
            on_event=on_event, _lease_holder=holder)
    finally:
        lease = holder.get("lease")
        if lease is not None:
            try:
                lease.release()
            except Exception:
                pass


def _live_code_slice(goal, workspace=None, mission_id=None, host_verifier=None,
                     execution_profile=None, worker_profile=None, verify_command="",
                     session_id="", baseline_tree_digest="", expected_tree_digest="",
                     slice_turns=None, verify_timeout_seconds=None,
                     max_session_storage_bytes=None, max_model_calls=None, runs_db="",
                     mission_store_path="", mission_run_token="", cancelled=None,
                     on_event=None, _lease_holder=None):
    import os
    from . import sessions
    from .cli import (make_harness, _RunnerShim, _paths, _worker_history_note,
                      _worker_model, _worker_provider)
    from . import settings as _s
    cwd = os.path.realpath(os.path.abspath(workspace or os.getcwd()))
    roots = [os.path.realpath(os.path.abspath(p)) for p in
             (os.environ.get("COLLIE_MISSION_CODE_ROOTS") or "").split(os.pathsep) if p]
    try:
        approved = any(os.path.commonpath([cwd, root]) == root for root in roots)
    except ValueError:
        approved = False
    if not roots or not approved:
        return {"answer": "Mission code is disabled for this workspace; add an approved root to "
                          "COLLIE_MISSION_CODE_ROOTS and explicitly allow the code capability.",
                "verified": False, "needs_human": True, "configuration_error": True}
    if not os.path.isdir(cwd):
        return {"answer": "approved code workspace does not exist", "verified": False,
                "needs_human": True, "configuration_error": True}
    profile = dict(execution_profile or {})
    provider = str(profile.get("provider") or _s.get("PROVIDER") or "").strip()
    model = str(profile.get("model") or _s.get("MODEL") or "").strip() or None
    if profile:
        if profile.get("allow_provider_fallback") is not False:
            return {"answer": "frozen code execution profile permits provider fallback",
                    "verified": False}
        subscription = provider in (
            "anthropic-oauth", "claude-sub", "claude-cli", "cli",
            "claude-agent-sdk", "claude-sdk",
            "codex-oauth", "codex-sub", "codex")
        if profile.get("subscription_only") and not subscription:
            return {"answer": "frozen subscription code route is invalid", "verified": False}
        if profile.get("subscription_only") and profile.get("billing_mode") != "subscription":
            return {"answer": "frozen subscription billing mode is inconsistent",
                    "verified": False, "needs_human": True}
        if (profile.get("profile") == "overnight" and
                provider != "claude-agent-sdk"):
            return {"answer": (
                        "overnight code requires the official Claude Agent SDK "
                        "with Collie's system prompt"),
                    "verified": False, "needs_human": True}
    project = "mission-code-" + hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:12]
    sid = str(session_id or _code_session_id(mission_id, cwd))
    if (len(sid) > 128 or not sid or
            not all(ch.isalnum() or ch in "-_." for ch in sid)):
        return {"answer": "durable code session id is invalid", "verified": False,
                "needs_human": True}
    try:
        session_limit = _optional_nonnegative_int(
            max_session_storage_bytes, "max_session_storage_bytes") or 0
        verified_timeout = _optional_nonnegative_int(
            verify_timeout_seconds, "verify_timeout_seconds")
        model_call_limit = _optional_nonnegative_int(
            max_model_calls, "max_model_calls")
    except ValueError as exc:
        return {"answer": "invalid Mission code authority: " + str(exc),
                "verified": False, "continue_needed": False,
                "needs_human": True, "session_id": sid}
    if model_call_limit == 0:
        return {"answer": "Mission model-request budget is exhausted",
                "verified": False, "continue_needed": False,
                "needs_human": True, "session_id": sid}
    lease = _acquire_code_session_lease(sid, cwd, mission_id)
    if isinstance(lease, dict):
        # A refusal here proves no journal read, no baseline receipt and no model
        # or tool call happened, so it is an ordinary stop and never recovery.
        return lease
    if isinstance(_lease_holder, dict):
        _lease_holder["lease"] = lease
    checked = sessions.load_checked(sid)
    if checked.get("status") == "invalid":
        return {
            "answer": "durable code session is corrupt or unreadable; inspect it before retrying",
            "verified": False, "continue_needed": False, "recovery_required": True,
            "session_id": sid,
        }
    saved = checked.get("session") or {}
    saved_cwd = str(saved.get("cwd") or "")
    if saved_cwd and os.path.realpath(os.path.abspath(saved_cwd)) != cwd:
        return {
            "answer": "durable code session belongs to a different workspace",
            "verified": False, "continue_needed": False, "recovery_required": True,
            "session_id": sid,
        }
    recovery = sessions.recovery_state(sid)
    if recovery and recovery.get("recovery_required"):
        return {
            "answer": recovery.get("reason") or "code session requires recovery inspection",
            "verified": False, "continue_needed": False, "recovery_required": True,
            "session_id": sid,
        }
    history = saved.get("messages") or None
    from .verification import workspace_snapshot
    prior_receipts = list(saved.get("run_receipts") or [])
    for receipt in prior_receipts:
        if not isinstance(receipt, dict):
            return {"answer": "durable code receipt is malformed", "verified": False,
                    "continue_needed": False, "recovery_required": True,
                    "session_id": sid}
        receipt_sid = str(receipt.get("session_id") or "")
        if receipt_sid and receipt_sid != sid:
            return {"answer": "durable code receipt identity does not match this Mission",
                    "verified": False, "continue_needed": False,
                    "recovery_required": True, "session_id": sid}
        receipt_mid = str(receipt.get("mission_id") or "")
        if mission_id and receipt_mid and receipt_mid != str(mission_id):
            return {"answer": "durable code receipt belongs to a different Mission",
                    "verified": False, "continue_needed": False,
                    "recovery_required": True, "session_id": sid}
    baseline_digest = str(baseline_tree_digest or "")
    for receipt in prior_receipts:
        if (not baseline_digest and isinstance(receipt, dict) and
                receipt.get("baseline_tree_digest")):
            baseline_digest = str(receipt["baseline_tree_digest"])
            break
    if not baseline_digest:
        baseline_digest = str(workspace_snapshot(cwd).get("tree_digest") or "")
    receipt_baselines = {
        str(receipt.get("baseline_tree_digest") or "") for receipt in prior_receipts
        if receipt.get("kind") in (
            "mission_code_baseline", "mission_code_slice",
            "mission_code_reconciled") and
        receipt.get("baseline_tree_digest")}
    if receipt_baselines and receipt_baselines != {baseline_digest}:
        return {"answer": "durable code baseline does not match this Mission",
                "verified": False, "continue_needed": False,
                "recovery_required": True, "session_id": sid}
    if not any(isinstance(receipt, dict) and
               receipt.get("kind") == "mission_code_baseline"
               for receipt in prior_receipts):
        # Persist this before the first possible edit.  If the worker dies after
        # changing files but before its final slice receipt, restart still knows
        # which bytes belonged to the user and which belong to this Mission.
        try:
            baseline_persisted = sessions.append_run_receipt(sid, {
                "kind": "mission_code_baseline",
                "mission_id": str(mission_id or ""),
                "session_id": sid,
                "baseline_tree_digest": baseline_digest,
            }, limit=128)
        except Exception:
            baseline_persisted = False
        if not baseline_persisted:
            return {
                "answer": "could not durably persist the pre-edit code baseline",
                "verified": False, "continue_needed": False,
                "recovery_required": True, "session_id": sid,
                "baseline_tree_digest": baseline_digest,
            }
        reloaded = sessions.load_checked(sid)
        if reloaded.get("status") != "ok":
            return {
                "answer": "durable code baseline could not be read back safely",
                "verified": False, "continue_needed": False,
                "recovery_required": True, "session_id": sid,
                "baseline_tree_digest": baseline_digest,
            }
        prior_receipts = list(
            (reloaded.get("session") or {}).get("run_receipts") or [])
    # Reconstruct the last Collie-owned byte boundary from durable receipts.  A
    # worker can finish its slice receipt and die before Mission folds the case;
    # that completed, contiguous receipt is safe to adopt.  Any other change is
    # external drift and must be inspected instead of silently attributed to us.
    #
    # Physical ownership and patch provenance are deliberately separate.  A
    # host verifier may create cache/build files which must be included in the
    # next slice's expected byte boundary, but those files must never become
    # evidence that the coding agent changed the project.  Only new-format
    # slice receipts with an explicit pre-verifier mutation, or a human-approved
    # completed reconciliation, establish patch provenance.  Legacy receipts
    # are physically replayable but provenance-ambiguous and therefore fail
    # closed for completion.
    patch_attributed = False
    provenance_expected = str(baseline_digest or "")
    for receipt in prior_receipts:
        if not isinstance(receipt, dict) or receipt.get("kind") not in (
                "mission_code_slice", "mission_code_reconciled"):
            continue
        before = str(receipt.get("pre_tree_digest") or "")
        after = str(receipt.get("post_tree_digest") or "")
        if not before or not after or before != provenance_expected:
            continue
        if receipt.get("kind") == "mission_code_slice":
            agent_after = str(receipt.get("agent_post_tree_digest") or "")
            if (receipt.get("snapshot_complete") is True and
                    receipt.get("agent_snapshot_complete") is True and
                    agent_after):
                # This is state, not an ever-fired event.  In particular, a
                # later receipt with ``patch_attributed: false`` records that
                # its agent restored the original baseline.  Replaying older
                # ``agent_mutated`` events must not resurrect that patch after
                # a verifier artifact advances the physical receipt chain.
                patch_attributed = bool(
                    receipt.get("patch_attributed") is True and
                    receipt.get("verifier_mutated") is False and
                    agent_after != baseline_digest)
            else:
                # Legacy/partial receipts are physically replayable below but
                # cannot establish completion-grade patch provenance.
                patch_attributed = False
        elif (receipt.get("kind") == "mission_code_reconciled" and
              receipt.get("resolution") == "completed" and before != after):
            patch_attributed = True
        provenance_expected = after
    expected_digest = str(expected_tree_digest or baseline_digest or "")
    for receipt in prior_receipts:
        if not isinstance(receipt, dict) or receipt.get("kind") not in (
                "mission_code_slice", "mission_code_reconciled"):
            continue
        if receipt.get("kind") == "mission_code_reconciled" and (
                receipt.get("resolution") != "completed" or
                receipt.get("snapshot_complete") is not True):
            return {
                "answer": "durable code reconciliation receipt is malformed",
                "verified": False, "continue_needed": False,
                "recovery_required": True, "session_id": sid,
            }
        before = str(receipt.get("pre_tree_digest") or "")
        after = str(receipt.get("post_tree_digest") or "")
        if before and after and before == expected_digest:
            expected_digest = after
    before_session_bytes = sessions.storage_bytes(sid)
    if session_limit and before_session_bytes >= session_limit:
        return {
            "answer": "durable code session storage budget is exhausted",
            "verified": False, "continue_needed": False, "needs_human": True,
            "session_id": sid, "baseline_tree_digest": baseline_digest,
            "_external_storage_bytes": before_session_bytes,
        }
    pre_slice = workspace_snapshot(cwd)
    if (not pre_slice.get("snapshot_complete") or not expected_digest or
            str(pre_slice.get("tree_digest") or "") != expected_digest):
        return {
            "answer": (
                "workspace bytes changed outside the last completed Collie code slice; "
                "inspect and reconcile ownership before continuing"),
            "verified": False, "continue_needed": False,
            "recovery_required": True, "needs_human": False,
            "session_id": sid, "baseline_tree_digest": baseline_digest,
            "expected_tree_digest": expected_digest,
            "post_tree_digest": str(pre_slice.get("tree_digest") or ""),
            "_external_storage_bytes": before_session_bytes,
        }
    worker_decision = None
    worker_request = None
    external_worker = False
    worker_receipt = None
    worker_spec = None
    worker_model = ""
    if isinstance(worker_profile, dict) and worker_profile:
        from .runner_select import refresh_frozen_worker_profile
        from .runner_specs import HarnessRequest
        try:
            worker_decision = refresh_frozen_worker_profile(
                worker_profile, cwd=cwd, runs_db=str(runs_db or ""))
            worker_request = HarnessRequest.from_dict(worker_profile.get("request") or {})
        except ValueError as exc:
            return {"answer": str(exc), "verified": False, "continue_needed": False,
                    "needs_human": True, "session_id": sid}
        external_worker = worker_decision.runner != "collie"
        if external_worker:
            from . import runner_registry
            worker_spec = runner_registry.SPECS.get(worker_decision.runner)
            worker_model = _worker_model(
                "", type("MissionRoute", (), {"model": model})(),
                worker_request, worker_spec)
    if external_worker:
        h = _RunnerShim(str(runs_db or "") or _paths()[1])
    else:
        h = make_harness(cwd, provider=provider, model=model,
                         project=project, embed="hash", rerank="off", distill="off",
                         web_search=False, code_search=True, exec_code=False,
                         subscription_only=bool(profile.get("subscription_only")))
    # The surface already owns this session's execution lease (acquired above,
    # before the journal was read).  Hand it to the harness rather than letting a
    # nested run take a second one; per the native host contract the harness
    # validates the same sid/root and never releases a supplied lease.
    h.run_owner = lease
    if callable(cancelled) and getattr(h, "cancelled", None) is None:
        h.cancelled = cancelled
    request_store = None
    external_request_id = ""
    if mission_store_path and mission_run_token:
        from .mission import MissionStore
        request_store = MissionStore(str(mission_store_path))

        def reserve_request(purpose="code_agent"):
            request_id = "req_" + secrets.token_hex(16)
            ok = request_store.reserve_model_request(
                str(mission_id or ""), str(mission_run_token), request_id,
                provider=(_worker_provider(worker_decision) if external_worker else
                          getattr(h.provider, "name", "")),
                model=((worker_model or worker_decision.runner) if external_worker else
                       getattr(h.provider, "model", "")), purpose=purpose)
            return request_id if ok else None

        if external_worker:
            external_request_id = reserve_request("external_code_slice") or ""
            if not external_request_id:
                request_store.close()
                h.memory.close(); h.recorder.close()
                return {"answer": "Mission model-request budget is exhausted",
                        "verified": False, "continue_needed": False,
                        "needs_human": True, "session_id": sid}
        else:
            h.provider.request_gate = reserve_request
            h.provider.request_complete = request_store.complete_model_request
    elif profile.get("profile") == "overnight" and max_model_calls not in (None, ""):
        return {"answer": "overnight code model-request authority is missing",
                "verified": False, "needs_human": True}
    if profile.get("subscription_only") and not external_worker:
        # Claude CLI normally permits an API-key fallback.  Overnight code does
        # not: its frozen billing route is part of Mission authority.
        h.provider.subscription_only = True
    # Positive authority list: a capability advertised as reversible cannot load
    # browser/desktop/MCP hands or a general shell behind Mission's outer gate.
    if not external_worker:
        _restrict_code_child(h, cwd)
    # This child still has no shell, no browser, no network and no MCP.  What it
    # now has is one hand: the exact command the user pre-authorized for this
    # workspace, run by the host, with its real output handed back.  Without it
    # the loop could not see its own test failures until the slice was over, so
    # every repair cost a whole new slice — a resumed prompt, a re-read of the
    # repository and another full check.  It is added AFTER the restriction pass
    # so the allow-list stays a statement about generic tools.
    check_receipts = []
    check_tool = None
    if not external_worker:
        h.self_verify = False   # the generic nudge is about a bash tool that is not here
        if str(verify_command or "").strip():
            from .code_check import VerificationCommandTool
            check_tool = VerificationCommandTool(
                verify_command, cwd, timeout_seconds=(verified_timeout or 300),
                receipts=check_receipts, on_event=on_event)
            h.registry._tools[check_tool.name] = check_tool
    if profile.get("profile") == "overnight" and not external_worker:
        # Let Mission's durable wait/backoff own transport retries.  Sleeping and
        # retrying inside a killable slice obscures the runnable-boundary auth
        # recheck and can consume several subscription requests before the
        # campaign call leash is folded.
        h.max_retries = 0
        h.critic = False
    turn_cap, turn_mode = _code_slice_turn_cap(slice_turns, model_call_limit)
    h.max_turns = turn_cap if not external_worker else None
    if model_call_limit is not None:
        h.max_model_calls = model_call_limit
        # A zero budget means exhausted, and is refused far above before any
        # session or workspace is touched; it must never arrive here and be
        # read as "no ceiling".  ``h.max_turns == 0`` is unlimited logical
        # turns, so it is left alone: the model-call ledger is what counts it
        # down, and min(0, N) would silently make it unlimited-with-a-number.
        if h.max_model_calls and h.max_turns and not external_worker:
            h.max_turns = min(h.max_turns, h.max_model_calls)
    if not external_worker:
        h.durable_session_id = sid
        h.checkpoint_scope = "session:" + sid
    prompt = str(goal or "")
    if history:
        prompt = ("Continue the same coding task from its durable checkpoint. Inspect the "
                  "current workspace before editing; do not repeat completed work.\n\n"
                  "Original goal: " + prompt)
        if prior_receipts:
            last_check = prior_receipts[-1].get("verification") \
                if isinstance(prior_receipts[-1], dict) else None
            last_check = last_check if isinstance(last_check, dict) else {}
            last_evidence = last_check.get("evidence") \
                if isinstance(last_check.get("evidence"), dict) else {}
            feedback = str(last_check.get("detail") or "").strip()
            output = str(last_evidence.get("output") or "").strip()
            if feedback or output:
                prompt += ("\n\nHost verification after the previous slice (ground truth; "
                           "repair this before finishing):\n" +
                           (feedback + "\n" if feedback else "") + output[-2500:])
    if check_tool is not None:
        # Say it in the prompt rather than through the generic self-verify nudge,
        # which talks about a bash tool this loop does not have.
        prompt += (
            "\n\nYou can check your own work in this run. The `run_verification` tool "
            "runs exactly `%s` in this workspace and gives you its real output. It "
            "takes no arguments. Run it after you have made your changes; if it "
            "fails, read the output, fix the cause and run it again. The same check "
            "is run by the host after you stop and is what decides whether this task "
            "is complete, so finishing on an untested guess only costs another round. "
            "If you cannot make it pass, say so plainly and say what is wrong."
            % str(verify_command or "").strip())
    try:
        if external_worker:
            from . import runner_slice
            resume_from = None
            for prior_receipt in reversed(prior_receipts):
                section = prior_receipt.get("runner") \
                    if isinstance(prior_receipt, dict) else None
                if isinstance(section, dict) and \
                        section.get("runner") == worker_decision.runner:
                    resume_from = section.get("native_session") or None
                    if resume_from:
                        break
            res = runner_slice.run_adhoc(
                worker_decision, prompt, cwd,
                timeout_s=(worker_spec.default_timeout_s if worker_spec else None),
                history_note=(None if resume_from else _worker_history_note(history)),
                resume_from=resume_from, model=worker_model,
                provider=_worker_provider(worker_decision),
                task_id="code:" + str(mission_id or project), recorder=h.recorder)
            worker_receipt = runner_slice.receipt_of(res)
            if external_request_id and request_store is not None:
                request_store.complete_model_request(
                    external_request_id, "completed" if not res.error else "failed")
        else:
            res = h.run("code:" + str(mission_id or project), prompt, history=history)
    except Exception:
        # The durable baseline was written before entering the model/tool loop.
        # Close local stores before propagating so the process wrapper can turn
        # this outcome-uncertain boundary into Mission recovery state.
        if request_store is not None and external_request_id:
            try:
                request_store.complete_model_request(external_request_id, "failed")
            except Exception:
                pass
        for store in (getattr(h, "memory", None), getattr(h, "recorder", None),
                      request_store):
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        raise
    # A Mission always has a token leash. Declared runner capability is not
    # enough: if this particular invocation omitted usage, treating None as zero
    # would authorize another slice against an unmeasurable budget. Mark the
    # slice as needing attention before its transcript is committed.
    worker_billing = str(getattr(worker_decision, "billing_class", "") or "")
    safe_worker_billing = external_worker and worker_billing in (
        "subscription_allowance", "local")
    raw_usage = {
        "input_tokens": getattr(res, "input_tokens", None),
        "output_tokens": getattr(res, "output_tokens", None),
        "cache_tokens": (
            None if (getattr(res, "cache_read", None) is None or
                     getattr(res, "cache_creation", None) is None)
            else int(getattr(res, "cache_read", 0) or 0) +
                 int(getattr(res, "cache_creation", 0) or 0)),
    }
    missing_usage = [key for key, value in raw_usage.items() if value is None]
    if (external_worker and not safe_worker_billing and
            getattr(res, "cost_usd", None) is None):
        missing_usage.append("cost_usd")
    usage_error = ""
    if external_worker and missing_usage:
        usage_error = (
            "external worker did not report usage required by the Mission leash: " +
            ", ".join(missing_usage))
        res.error = ((str(getattr(res, "error", "") or "") + "; ")
                     if getattr(res, "error", "") else "") + usage_error
        res.success = False
    # Freeze the exact agent-owned boundary before transcript persistence or
    # the host verifier can touch the workspace.  The final post-slice snapshot
    # below remains the physical continuation boundary; this one alone decides
    # whether this slice contributed patch provenance.
    agent_post_slice = workspace_snapshot(cwd)
    agent_snapshot_complete = bool(
        pre_slice.get("snapshot_complete") and
        agent_post_slice.get("snapshot_complete"))
    agent_mutated = bool(
        agent_snapshot_complete and
        pre_slice.get("tree_digest") != agent_post_slice.get("tree_digest"))
    # An in-slice check runs project code INSIDE the agent boundary, so unlike
    # the end-of-slice verifier its build output lands where a naive pre/post
    # comparison would read it as the agent's patch.  Attribute only the
    # intervals between the checks; the intervals across them belong to the
    # check.  ``None`` means the digests cannot answer it, and then the
    # conservative pre/post answer stands.
    from .code_check import agent_mutated_outside_checks, check_window_mutated
    if check_receipts:
        outside = agent_mutated_outside_checks(
            pre_slice.get("tree_digest"), check_receipts,
            agent_post_slice.get("tree_digest"))
        if outside is not None and agent_snapshot_complete:
            agent_mutated = outside
    # ``patch_attributed`` reconstructed above means a prior slice introduced
    # agent-owned bytes.  It must not remain sticky after a later agent slice
    # restores the exact original baseline.  This check deliberately uses the
    # pre-verifier boundary: build/test artifacts created next may change the
    # physical continuation digest, but can never resurrect a reverted patch.
    agent_post_digest = str(agent_post_slice.get("tree_digest") or "")
    patch_attributed = bool(
        (patch_attributed or agent_mutated) and agent_snapshot_complete and
        baseline_digest and agent_post_digest != baseline_digest)
    # Same rule the end-of-slice verifier already obeys: a check that rewrote a
    # represented project byte makes this slice's ownership unprovable rather
    # than being laundered into it.
    checks_mutated = check_window_mutated(check_receipts) if check_receipts else False
    if checks_mutated:
        patch_attributed = False
    transcript_persisted = True
    transcript_error = ""
    try:
        if external_worker:
            sessions.append_exchange(sid, prompt, runner_slice.transcript_text(res),
                                     project=project, cwd=cwd)
        else:
            sessions.save(sid, res.messages, project=project, cwd=cwd,
                          answer=res.answer or "")
    except Exception as persist_exc:
        from .runner_specs import redact_text
        transcript_persisted = False
        transcript_error = "code transcript could not be persisted: " + redact_text(
            "%s: %s" % (type(persist_exc).__name__, persist_exc), 500)
        res.error = ((str(getattr(res, "error", "") or "") + "; ")
                     if getattr(res, "error", "") else "") + transcript_error
        res.success = False
    # The worker's own stop state is resolved BEFORE the host check, because it
    # decides whether that check may run at all.  A cancelled or errored slice has
    # no settled workspace to certify; starting the repository command anyway
    # would spend real time on evidence nobody may use and risks turning somebody
    # else's Stop into a green-looking receipt.
    error_text = str(getattr(res, "error", "") or "")
    if not error_text and str(getattr(res, "answer", "") or "").startswith("ERROR("):
        error_text = str(getattr(res, "answer", "") or "")
    run_cancelled = bool(getattr(res, "canceled", False) or
                         getattr(res, "cancelled", False))
    if not run_cancelled and callable(cancelled):
        try:
            run_cancelled = bool(cancelled())
        except Exception:
            # A broken predicate is a caller bug, not a user Stop.  Keep going;
            # the verifier's own watcher reports the fault in its evidence.
            run_cancelled = False
    stop_reason = str(getattr(res, "stop_reason", "") or "")
    boundary_uncertain = bool(external_worker and (
        worker_receipt is None or worker_receipt.recovery_required))
    skip_reason = ("the code worker was cancelled" if run_cancelled else
                   "the external worker requires recovery" if boundary_uncertain else
                   "the code worker stopped with an error" if error_text else "")
    # If the loop ran the same command itself and nothing has moved since, the
    # host already owns a receipt for exactly these bytes.  Running it a second
    # time would cost the user the suite's whole wall-clock for an answer that
    # is on disk.  Every condition for standing in is checked in
    # ``reusable_receipt``, and the decisive one is that the tree digest before
    # AND after that check is still the tree that exists now.
    reused = None
    if (host_verifier is None and not skip_reason and check_receipts and
            str(verify_command or "").strip()):
        from .code_check import reusable_receipt
        reused = reusable_receipt(
            check_receipts, verify_command,
            str(agent_post_slice.get("tree_digest") or ""))
    if host_verifier is not None:
        verification = host_verifier(cwd, res)
    elif reused is not None:
        verification = _bind_check_evidence(
            reused, baseline_digest=baseline_digest,
            patch_attributed=patch_attributed,
            agent_post_tree_digest=str(agent_post_slice.get("tree_digest") or ""))
        verification["reused_in_slice_check"] = True
    elif skip_reason:
        verification = {"verified": False, "skipped": True,
                        "detail": "host verification skipped: " + skip_reason,
                        "evidence": None}
    elif not str(verify_command or "").strip():
        verification = _default_code_verifier(
            cwd, res, verify_command, baseline_digest=baseline_digest,
            timeout_seconds=verified_timeout or 300,
            patch_attributed=patch_attributed,
            agent_post_tree_digest=str(agent_post_slice.get("tree_digest") or ""))
    else:
        # A repository check runs project code and can write files.  Fence that
        # possible effect durably before the first command byte, and retire the
        # fence only on evidence that nothing is still running.
        from .verification import close_check_boundary, open_check_boundary
        boundary = open_check_boundary(
            sid, [], project=project, cwd=cwd, command=verify_command,
            surface="mission-code")
        if boundary.get("preexisting"):
            boundary_uncertain = True
            verification = {
                "verified": False, "skipped": True, "evidence": None,
                "detail": ("host verification skipped: an earlier unreconciled "
                           "boundary still fences this code session")}
        elif boundary.get("error"):
            boundary_uncertain = True
            verification = {"verified": False, "skipped": True, "evidence": None,
                            "detail": "host verification skipped: " +
                                      str(boundary.get("error"))}
        else:
            verification = _default_code_verifier(
                cwd, res, verify_command, baseline_digest=baseline_digest,
                timeout_seconds=verified_timeout or 300,
                patch_attributed=patch_attributed,
                agent_post_tree_digest=str(agent_post_slice.get("tree_digest") or ""),
                cancelled=(cancelled if callable(cancelled) else None),
                on_event=on_event)
            evidence = verification.get("evidence") if isinstance(
                verification, dict) else None
            closed = close_check_boundary(boundary, evidence)
            if isinstance(verification, dict):
                verification["check_boundary"] = {
                    key: closed.get(key) for key in
                    ("retired", "fenced", "detail", "error")}
            if closed.get("fenced") or closed.get("error"):
                boundary_uncertain = True
                if isinstance(verification, dict):
                    verification["verified"] = False
    if isinstance(verification, bool):
        verification = {"verified": verification}
    verification = dict(verification) if isinstance(verification, dict) else {}
    check_evidence = verification.get("evidence") if isinstance(
        verification.get("evidence"), dict) else {}
    # A Stop that lands while the host check is running stops the slice too: the
    # check never finished, so the slice has no settled outcome to continue from.
    if verification.get("cancelled") or check_evidence.get("cancelled"):
        run_cancelled = True
    verified = bool(verification.get("verified"))
    if boundary_uncertain:
        verified = False
        verification["verified"] = False
        verification["recovery_required"] = True
    if usage_error:
        verified = False
        verification["verified"] = False
        verification["usage_guard"] = usage_error
    if not transcript_persisted:
        verified = False
        verification["verified"] = False
        verification["transcript_guard"] = transcript_error
    if run_cancelled:
        verified = False
        verification["verified"] = False
        verification["cancelled"] = True
    transient = False
    retry_at = 0
    if error_text:
        from .providers import classify_error, provider_retry_at
        transient = classify_error(error_text) == "retryable"
        if transient:
            retry_at = provider_retry_at(getattr(res, "retry_at", 0))
    post_slice = workspace_snapshot(cwd)
    slice_snapshot_complete = bool(agent_snapshot_complete and
                                   post_slice.get("snapshot_complete"))
    verifier_mutated = bool(
        slice_snapshot_complete and
        agent_post_slice.get("tree_digest") != post_slice.get("tree_digest"))
    # A verifier that changes any represented project byte invalidates prior
    # agent ownership for continuation. Without per-path provenance, retaining
    # the bool would let a verifier overwrite the agent's source in slice N and
    # have that replacement laundered as an agent patch in slice N+1. Common
    # untracked Python cache artifacts are excluded by workspace_snapshot, so
    # ordinary py_compile/pytest startup does not cause a false taint.
    patch_attributed = bool(patch_attributed and not verifier_mutated)
    # Public/receipt mutation attribution is the agent-side delta only.  The
    # physical post_tree_digest still includes verifier bytes so restart/drift
    # checks bind the exact workspace that actually exists.
    slice_mutated = agent_mutated
    session_recovery = sessions.recovery_state(sid)
    journal_uncertain = bool(session_recovery and
                             session_recovery.get("recovery_required"))
    recovery_required = bool(journal_uncertain or not slice_snapshot_complete or
                             not transcript_persisted or boundary_uncertain)
    needs_human = bool((usage_error or (error_text and not transient)) and
                       not verified and not recovery_required)
    # A cancelled slice is a settled stop, not a scheduling yield: continuing it
    # automatically would replay work the user asked to stop.
    continue_needed = bool(not verified and not recovery_required and not needs_human and
                           not run_cancelled and
                           (getattr(res, "turns_exhausted", False) or transient or
                            profile.get("profile") == "overnight"))
    reported_cost = getattr(res, "cost_usd", None)
    no_marginal_charge = bool(
        safe_worker_billing or (not external_worker and profile.get("subscription_only")))
    marginal_cost = 0.0 if no_marginal_charge else (
        None if reported_cost is None else float(reported_cost))
    equivalent_cost = None if reported_cost is None else float(reported_cost)
    usage = {
        "known": not missing_usage,
        "input_tokens": (None if raw_usage["input_tokens"] is None else
                         int(raw_usage["input_tokens"] or 0)),
        "output_tokens": (None if raw_usage["output_tokens"] is None else
                          int(raw_usage["output_tokens"] or 0)),
        "cache_tokens": raw_usage["cache_tokens"],
        # Mission's cost leash is a charge leash.  Equivalent API value remains
        # visible separately instead of falsely stopping a flat subscription.
        "cost_usd": marginal_cost,
        "equivalent_cost_usd": equivalent_cost,
    }
    accounted_usage = {
        "input_tokens": int(raw_usage["input_tokens"] or 0),
        "output_tokens": int(raw_usage["output_tokens"] or 0),
        "cache_tokens": int(raw_usage["cache_tokens"] or 0),
        "cost_usd": float(marginal_cost or 0.0),
    }
    receipt = {
        "kind": "mission_code_slice", "mission_id": str(mission_id or ""),
        "session_id": sid, "baseline_tree_digest": baseline_digest,
        "pre_tree_digest": str(pre_slice.get("tree_digest") or ""),
        "agent_post_tree_digest": str(agent_post_slice.get("tree_digest") or ""),
        "post_tree_digest": str(post_slice.get("tree_digest") or ""),
        "snapshot_complete": slice_snapshot_complete,
        "agent_snapshot_complete": agent_snapshot_complete,
        "agent_mutated": agent_mutated,
        "verifier_mutated": verifier_mutated,
        "patch_attributed": patch_attributed,
        "turns": int(getattr(res, "turns", 0) or 0),
        "turns_exhausted": bool(getattr(res, "turns_exhausted", False)),
        "slice_turn_cap": turn_cap,
        "slice_turn_mode": turn_mode,
        "in_slice_checks": len(check_receipts),
        "in_slice_check_mutated": checks_mutated,
        "reused_in_slice_check": bool(verification.get("reused_in_slice_check")),
        "cancelled": run_cancelled,
        "stop_reason": stop_reason,
        "verified": verified, "continue_needed": continue_needed,
        "verification": verification,
        "transcript_persisted": transcript_persisted,
        "transcript_error": transcript_error,
        "usage": usage,
        "runner": worker_receipt.to_dict() if worker_receipt is not None else None,
        "worker_decision": (worker_decision.to_dict()
                            if worker_decision is not None else None),
    }
    receipt_error = ""
    try:
        receipt_persisted = sessions.append_run_receipt(sid, receipt, limit=128)
    except Exception as persist_exc:
        from .runner_specs import redact_text
        receipt_persisted = False
        receipt_error = "code ownership receipt could not be persisted: " + redact_text(
            "%s: %s" % (type(persist_exc).__name__, persist_exc), 500)
    if not receipt_persisted:
        # The workspace may already contain edits. Without the post-slice WAL
        # receipt those bytes have uncertain ownership and must be reconciled;
        # never report verification success or silently start another slice.
        verified = False
        continue_needed = False
        recovery_required = True
        needs_human = False
    session_bytes = sessions.storage_bytes(sid)
    try:
        h.settle_run_memory(res, verified, verification.get("evidence"),
                            source="mission_code_verification")
        h.recorder.finish_run(res)
    except Exception:
        pass
    for store in (getattr(h, "memory", None), getattr(h, "recorder", None),
                  request_store):
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    public_answer = str(getattr(res, "answer", "") or "")
    if not transcript_persisted:
        public_answer += (("\n\n" if public_answer else "") + transcript_error)
    if not receipt_persisted:
        ownership_error = (receipt_error or
                           "code slice completed but its ownership receipt was not "
                           "durably persisted")
        public_answer += (("\n\n" if public_answer else "") + ownership_error)
    public_answer = public_answer or str(getattr(res, "error", "") or "")
    return {
        "answer": public_answer,
        "verified": verified,
        "continue_needed": continue_needed, "session_id": sid,
        "turns_exhausted": bool(getattr(res, "turns_exhausted", False)),
        "turns": int(getattr(res, "turns", 0) or 0),
        "slice_turn_cap": turn_cap,
        "slice_turn_mode": turn_mode,
        "in_slice_checks": len(check_receipts),
        "reused_in_slice_check": bool(verification.get("reused_in_slice_check")),
        "model_calls": int(getattr(res, "model_calls", 0) or
                           getattr(res, "turns", 0) or 0),
        "_model_calls_reserved": bool(request_store is not None),
        "_usage": accounted_usage,
        "_usage_known": usage["known"],
        "equivalent_cost_usd": usage["equivalent_cost_usd"],
        "baseline_tree_digest": baseline_digest,
        "expected_tree_digest": expected_digest,
        "agent_post_tree_digest": str(agent_post_slice.get("tree_digest") or ""),
        "post_tree_digest": str(post_slice.get("tree_digest") or ""),
        "verification": verification,
        "error": error_text[:1000],
        "receipt_error": receipt_error,
        "transient": transient,
        "retry_after_seconds": 60 if transient else 0,
        "retry_at": retry_at,
        "recovery_required": recovery_required,
        "needs_human": needs_human,
        "slice_mutated": slice_mutated,
        "verifier_mutated": verifier_mutated,
        "patch_attributed": patch_attributed,
        "cancelled": run_cancelled,
        "stop_reason": stop_reason,
        "runner": worker_receipt.to_dict() if worker_receipt is not None else None,
        "_external_storage_bytes": session_bytes,
    }


def _real_code(runner=None):
    def execute(rec):
        goal = (rec.args or {}).get("goal") or (rec.args or {}).get("task") or ""
        ws = (rec.args or {}).get("workspace") or (rec.args or {}).get("cwd")
        case = (rec.args or {}).get("_case") or {}
        execution_profile = case.get("execution_profile") or {}
        code_profile = case.get("code_profile") or {}
        active_runner = runner
        if active_runner is None:
            out = _live_code(
                goal, ws, mission_id=getattr(rec, "job_id", ""),
                execution_profile=execution_profile,
                worker_profile=case.get("worker_profile") or {},
                verify_command=code_profile.get("verify_command") or "",
                session_id=(code_profile.get("session_id") or
                            case.get("code_session_id") or ""),
                baseline_tree_digest=(case.get("code_baseline_tree_digest") or ""),
                expected_tree_digest=(case.get("code_expected_tree_digest") or ""),
                slice_turns=code_profile.get("slice_turns"),
                verify_timeout_seconds=code_profile.get("verify_timeout_seconds"),
                max_session_storage_bytes=code_profile.get(
                    "max_session_storage_bytes"),
                max_model_calls=(rec.args or {}).get("_model_call_budget"))
        else:
            # Keep the long-standing injected ``runner(goal)`` seam while
            # allowing Mission-aware runners to opt into durable identity.
            try:
                sig = inspect.signature(active_runner)
                params = sig.parameters
                accepts_context = (any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()) or
                    any(name in params for name in (
                        "workspace", "mission_id", "execution_profile",
                        "worker_profile",
                        "verify_command", "max_wall_seconds", "session_id",
                        "baseline_tree_digest", "slice_turns",
                        "expected_tree_digest",
                        "max_model_calls",
                        "mission_store_path", "mission_run_token",
                        "verify_timeout_seconds", "max_session_storage_bytes")))
            except (TypeError, ValueError):
                accepts_context = False
            if accepts_context:
                context = {
                    "workspace": ws, "mission_id": getattr(rec, "job_id", ""),
                    "execution_profile": execution_profile,
                    "worker_profile": case.get("worker_profile") or {},
                    "verify_command": code_profile.get("verify_command") or "",
                    "session_id": (code_profile.get("session_id") or
                                   case.get("code_session_id") or ""),
                    "baseline_tree_digest": str(
                        case.get("code_baseline_tree_digest") or ""),
                    "expected_tree_digest": str(
                        case.get("code_expected_tree_digest") or ""),
                    "slice_turns": code_profile.get("slice_turns"),
                    "verify_timeout_seconds": code_profile.get(
                        "verify_timeout_seconds"),
                    "max_session_storage_bytes": code_profile.get(
                        "max_session_storage_bytes"),
                    "max_model_calls": (rec.args or {}).get("_model_call_budget"),
                    "mission_store_path": str(
                        getattr(rec, "_mission_store_path", "") or ""),
                    "mission_run_token": str(
                        getattr(rec, "_mission_run_token", "") or ""),
                    # Finish and kill inside the process runner just before the
                    # outer watchdog only as a last-resort fallback.  The outer
                    # owner fires first so timeout becomes recovery_required
                    # instead of an ordinary reversible failure/retry loop.
                    "max_wall_seconds": max(1.0, float(
                        ((rec.args or {}).get("_leash") or {}).get(
                            "max_step_seconds", 600)) + 30.0),
                }
                accepts_kwargs = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values())
                kwargs = context if accepts_kwargs else {
                    key: value for key, value in context.items()
                    if key in sig.parameters}
                out = active_runner(goal, **kwargs)
            else:
                out = active_runner(goal)
        if isinstance(out, str):
            out = {"answer": out, "verified": False}
        pending = bool(out.get("continue_needed"))
        verification = out.get("verification") if isinstance(
            out.get("verification"), dict) else {}
        evidence = verification.get("evidence") if isinstance(
            verification.get("evidence"), dict) else {}
        answer = str(out.get("answer") or "")
        # Mutation/outcome facts are the difference between "a patch exists that
        # nobody checked" and "the run changed nothing at all".  They must reach
        # the done-check and the Mission case: reshaping the worker's result into
        # a bare ``result`` string is how a read-only survey came to be reported
        # as "code edited but not executed-verified".
        mutation_reported = ("slice_mutated" in out or "patch_attributed" in out)
        # The case is a working set that has to stay loadable and survive
        # compaction, so the copy of the answer kept here is capped.  That cap
        # is stated rather than implied: ``code_stop_report`` used to advertise
        # this field as "the complete answer", so a long report looked like it
        # was preserved somewhere it was not.  The session journal below is the
        # copy that is always whole.
        answer_kept = answer[:CODE_DELIVERY_ANSWER_CHARS]
        delivery = {
            "answer": answer_kept,
            "answer_chars": len(answer),
            "answer_chars_kept": len(answer_kept),
            "answer_truncated": len(answer_kept) < len(answer),
            # Where the complete text lives when this copy is short of it.
            "answer_source": "durable coding session journal",
            "session_id": str(out.get("session_id") or "")[:120],
            "verified": bool(out.get("verified")),
            "mutation_reported": mutation_reported,
            "slice_mutated": bool(out.get("slice_mutated")),
            "patch_attributed": bool(out.get("patch_attributed")),
            # The agent-owned boundary of THIS slice.  ``patch_attributed`` is
            # cumulative and cannot answer "did the last slice do anything";
            # comparing this digest with the one the dispatch recorded can.
            "agent_post_tree_digest": str(out.get("agent_post_tree_digest") or "")[:128],
            "continue_needed": pending,
            "turns": int(out.get("turns", 0) or 0),
            "turns_exhausted": bool(out.get("turns_exhausted")),
            "slice_turn_cap": int(out.get("slice_turn_cap", 0) or 0),
            "slice_turn_mode": str(out.get("slice_turn_mode") or "")[:40],
            "in_slice_checks": int(out.get("in_slice_checks", 0) or 0),
            "cancelled": bool(out.get("cancelled")),
            "stop_reason": str(out.get("stop_reason") or "")[:80],
            "error": str(out.get("error") or "")[:500],
            "verification_detail": str(verification.get("detail") or "")[:500],
            "at": int(time.time()),
        }
        case_update = {
            "coded": True, "code_verified": bool(out.get("verified")),
            "code_pending": pending,
            "code_session_id": str(out.get("session_id") or ""),
            "code_recovery_required": bool(out.get("recovery_required")),
            # The user's actual deliverable survives case compaction and restart.
            "code_delivery": delivery,
        }
        if out.get("post_tree_digest") and not out.get("recovery_required"):
            case_update["code_expected_tree_digest"] = str(
                out.get("post_tree_digest") or "")
        if verification:
            # Bounded host evidence is durable Mission state.  The goal verifier
            # rechecks its workspace digest after restart instead of trusting the
            # coding model's final answer.
            case_update["code_verification"] = verification
            case_update["code_baseline_tree_digest"] = str(
                out.get("baseline_tree_digest") or
                evidence.get("baseline_tree_digest") or "")
        result = {
            "case": case_update,
            "answer": answer,
            "result": answer, "verified": bool(out.get("verified")),
            "continue_needed": pending, "session_id": out.get("session_id", ""),
            "recovery_required": bool(out.get("recovery_required")),
            "needs_human": bool(out.get("needs_human")),
            "configuration_error": bool(out.get("configuration_error")),
            "transient": bool(out.get("transient")),
            "retry_after_seconds": int(out.get("retry_after_seconds", 0) or 0),
            "retry_at": out.get("retry_at", 0),
            "turns_exhausted": bool(out.get("turns_exhausted")),
            "turns": int(out.get("turns", 0) or 0),
            "cancelled": bool(out.get("cancelled")),
            "stop_reason": str(out.get("stop_reason") or ""),
            "model_calls": int(out.get("model_calls", 0) or 0),
            "_model_calls_reserved": bool(out.get("_model_calls_reserved")),
            "_usage": dict(out.get("_usage") or {}),
            "_usage_known": bool(out.get("_usage_known", True)),
            "equivalent_cost_usd": (
                None if out.get("equivalent_cost_usd") is None else
                float(out.get("equivalent_cost_usd") or 0.0)),
            "runner": out.get("runner") if isinstance(out.get("runner"), dict) else None,
            "verification": out.get("verification"),
            "error": out.get("error", ""),
            "_external_storage_bytes": int(
                out.get("_external_storage_bytes", 0) or 0),
        }
        if mutation_reported:
            # Absent keys mean "this runner reported nothing", which must stay
            # distinguishable from a runner that reported "nothing changed".
            result["slice_mutated"] = bool(out.get("slice_mutated"))
            result["patch_attributed"] = bool(out.get("patch_attributed"))
            result["verifier_mutated"] = bool(out.get("verifier_mutated"))
            result["agent_post_tree_digest"] = str(
                out.get("agent_post_tree_digest") or "")
            result["post_tree_digest"] = str(out.get("post_tree_digest") or "")
            result["baseline_tree_digest"] = str(
                out.get("baseline_tree_digest") or "")
        return result
    return execute


def _code_resource(rec):
    """Serialize edits to the same canonical workspace across Missions/processes."""
    ws = (rec.args or {}).get("workspace") or (rec.args or {}).get("cwd") or os.getcwd()
    root = os.path.realpath(os.path.abspath(str(ws)))
    return "code-workspace:" + hashlib.sha256(root.encode("utf-8")).hexdigest()


def _code_verify(rec, result):
    """Done-check = collie's OWN executed verification (a repro that fails on the
    broken code, an edit that flips it, a re-run that passes). Verified only when the
    coding loop reported that gate green; an edit without it is INCONCLUSIVE, not done."""
    r = result or {}
    if not isinstance(r, dict):
        return Verdict(FAILED, "coding task returned an invalid result")
    if r.get("recovery_required"):
        return Verdict(FAILED, "code worker stopped at an outcome-uncertain edit boundary")
    if r.get("configuration_error"):
        return Verdict(INCONCLUSIVE, str(r.get("answer") or r.get("result") or
                                         "coding setup is incomplete")[:700])
    if r.get("verified") is True:
        return Verdict(VERIFIED, "Mission patch passed the configured fresh host check")
    if r.get("cancelled"):
        # A stop is a settled outcome with no verdict about the code; it must not
        # be reported as a checkpointed yield that will be resumed on its own.
        return Verdict(INCONCLUSIVE,
                       "the coding run was cancelled before it produced verified work")
    if r.get("continue_needed") and r.get("session_id"):
        return Verdict(VERIFIED, "bounded code slice durably checkpointed; continuing automatically")
    if r.get("error"):
        return Verdict(FAILED, str(r["error"])[:700])
    answer = str(r.get("answer") or r.get("result") or "")
    if answer:
        verification = r.get("verification") or {}
        detail = verification.get("detail") if isinstance(verification, dict) else ""
        if r.get("slice_mutated") or r.get("patch_attributed"):
            return Verdict(INCONCLUSIVE, "A patch was produced but has no fresh verification. " +
                           str(detail or "Run a check against the changed workspace.")[:500])
        if "slice_mutated" in r or "patch_attributed" in r:
            # The runner explicitly reported that nothing changed.  Saying "code
            # edited" here is the false claim that made a read-only survey look
            # like an unverified patch.
            return Verdict(INCONCLUSIVE,
                           "The coding run changed no file in the workspace, so its answer is a "
                           "report rather than a patch: " + answer[:500])
        return Verdict(INCONCLUSIVE, "Coding work returned a result without completion-grade "
                       "evidence: " + answer[:500])
    return Verdict(FAILED, "coding task produced no result")


def _stub_code(rec):
    goal = (rec.args or {}).get("goal") or ""
    return {"case": {"coded": True, "code_verified": True},
            "result": "(stub) fixed: " + goal[:50], "verified": True}


def _semantic_web_submit(args):
    """Canonical executor inputs; aliases/verification hints cannot split a key."""
    a = args or {}
    return {"url": a.get("url") or "", "fields": a.get("fields") or {},
            "submit": a.get("submit") or a.get("submit_selector") or ""}


def _semantic_web_send(args):
    a = args or {}
    # `to` is display/case metadata only; the executor binds the actual thread by
    # URL + selectors. Letting `to` split the key could resend on that same thread.
    return {"url": a.get("url") or "", "text": a.get("text") or "",
            "selector": a.get("selector") or a.get("message_selector") or "",
            "send": a.get("send") or a.get("send_selector") or ""}


def _semantic_browse_submit(args):
    a = args or {}
    return {"button": a.get("button") or a.get("text") or "Publish"}


# ══════════════════════════ registration ═════════════════════════════════════
def register_primitives(stub: bool = True, actuator=None, provider=None,
                        research_runner=None, browse_runner=None, code_runner=None,
                        otp_reader=None):
    """Register the neutral primitive set. `stub=True` wires the canned bodies
    (container tests / safe default). `stub=False` wires the REAL bodies; deps are
    injectable (actuator/provider/research_runner/browse_runner/code_runner) for
    tests, and fall back to live ones when omitted."""
    if stub:
        research_exec, research_verify = _stub_research, _read_verify
        compose_exec, compose_verify = _stub_compose, _read_verify
        observe_exec, observe_verify = _stub_observe, _read_verify
        submit_exec, submit_verify = _stub_web_submit, _stub_submit_verify
        send_exec, send_verify = _stub_web_send, _stub_send_verify
        browse_exec, browse_verify = _stub_browse, _browse_verify
        bsubmit_exec, bsubmit_verify = _stub_browse_submit, _browse_submit_verify
        code_exec, code_verify = _stub_code, _code_verify
        verification_exec, verification_verify = _stub_verification_fill, _verification_fill_verify
        browser_resource = code_resource = bsubmit_snapshot = bsubmit_unchanged = None
    else:
        research_exec, research_verify = _real_research(research_runner), _real_research_verify
        compose_exec, compose_verify = _real_compose(provider), _compose_verify
        observe_exec, observe_verify = _real_observe(actuator), _read_verify
        submit_exec, submit_verify = _real_web_submit(actuator), _real_submit_verify
        send_exec, send_verify = _real_web_send(actuator), _real_send_verify
        browse_exec, browse_verify = _real_browse(browse_runner), _browse_verify
        bsubmit_exec, bsubmit_verify = _real_browse_submit(actuator), _browse_submit_verify
        code_exec, code_verify = _real_code(code_runner), _code_verify
        verification_exec = _real_verification_fill(actuator, otp_reader)
        verification_verify = _verification_fill_verify
        browser_resource = "browser-profile"
        bsubmit_snapshot = _browse_target_snapshot(actuator)
        bsubmit_unchanged = _browse_target_unchanged(actuator)
        code_resource = _code_resource

    register(Capability(
        name="research", execute=research_exec, verify=research_verify, reversible=True,
        risk="read", description="Gather facts from the web toward a question.",
        args_hint='{"query"}'))
    register(Capability(
        name="compose", execute=compose_exec, verify=compose_verify, reversible=True,
        risk="read", description=("Create final ready-to-use copy. Put a generation request in "
                                  "instruction; use text only for already-final literal copy."),
        args_hint='{"facts","instruction","text (final literal only)"}'))
    register(Capability(
        name="observe", execute=observe_exec, verify=observe_verify, reversible=True,
        risk="read", resource=browser_resource,
        description="Re-observe the world (logged-out fetch for evidence, "
        "or authed browser read to poll an inbox).",
        args_hint='{"url","expect","authed"}'))
    register(Capability(
        name="verification.fill", execute=verification_exec, verify=verification_verify,
        reversible=True, risk="read", resource=browser_resource,
        description=("Read one fresh service-matching code from a connected verification inbox "
                     "and fill the current code field internally. The code is never returned to "
                     "the model, Mission case, event log, or receipt."),
        args_hint='{"service":"Product Hunt","channel":"email|sms","field":"Verification code","max_age_seconds":600}'))
    register(Capability(
        name="web.submit", execute=submit_exec, verify=submit_verify, reversible=False,
        risk="publish", resource=browser_resource,
        description="Fill and submit a non-commerce form (for example, publish a listing).",
        args_hint='{"url","fields","submit","expect_title"}',
        semantic_args=_semantic_web_submit))
    register(Capability(
        name="web.send", execute=send_exec, verify=send_verify, reversible=False,
        risk="send", resource=browser_resource,
        description="Send a message (reply / negotiate / email).",
        args_hint='{"url","selector","text","send","success_text"}',
        semantic_args=_semantic_web_send))
    register(Capability(
        name="browse", execute=browse_exec, verify=browse_verify, reversible=True, risk="read",
        resource=browser_resource,
        description="Do a task on a website by driving the real browser adaptively (fill a form, "
        "navigate, act) — handles dynamic/obfuscated sites like Facebook Marketplace. Fills up to the "
        "final submit then STOPS (reversible). The browser child cannot see the Mission case: embed "
        "every complete exact field value in goal AND expect; never reference a prior/case draft. "
        "Pass `expect` using exact visible field labels. For a "
        "rich-text editor use content/body/post_text and provide its entire final value; platform/site is checked against the live page "
        "origin. For inspection/navigation with no form changes pass read_only=true (an explicit "
        "inspect + do-not-change/submit goal is also recognized fail-closed). The outcome is verified "
        "by an INDEPENDENT re-read, not the agent's say-so.",
        args_hint='{"goal": "fill Make exactly Toyota, Model exactly Corolla, Year exactly 2015, Price exactly 9500", '
                  '"expect": {"Make":"Toyota","Model":"Corolla","Year":"2015","Price":"9500"}, '
                  '"read_only": false}'))
    register(Capability(
        name="browse.submit", execute=bsubmit_exec, verify=bsubmit_verify, reversible=False,
        risk="publish", snapshot=bsubmit_snapshot, unchanged=bsubmit_unchanged,
        resource=browser_resource,
        description="Click one exact snapshotted final CONSEQUENTIAL button (Publish / Post / "
        "Create account / Authorize app) after `browse` has prepared the page. Gated and "
        "independently verified; commerce is refused and uses a dedicated pay capability.",
        args_hint='{"button": "Authorize app", "success_url_contains": "producthunt.com"}',
        semantic_args=_semantic_browse_submit))
    register(Capability(
        name="code", execute=code_exec, verify=code_verify, reversible=True, risk="code",
        resource=code_resource,
        description="Read / write / refactor code inside one explicitly approved workspace using "
        "a filesystem-confined child. Mission grants no shell; unverified edits hand off for review.",
        args_hint='{"goal": "fix the null-pointer in parser.py", "workspace": "/path/to/repo"}'))
    return [get_capability(name) for name in
            ("research", "compose", "observe", "verification.fill", "web.submit", "web.send",
             "browse", "browse.submit", "code")]
