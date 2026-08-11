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
import fnmatch
import os
import re
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
             "browser_links", "browser_type", "browser_pick"}
    for name in list(h.registry._tools):
        if name not in allow:
            h.registry._tools.pop(name, None)
    boundary = {"domains": list(allowed_domains or []), "first_host": ""}
    for name in list(h.registry._tools):
        kind = "type" if name == "browser_type" else "open" if name == "browser_open" else "read"
        h.registry._tools[name] = _BoundBrowserTool(
            h.registry._tools[name], space, kind, name, boundary)


def _live_browse(goal, space="mission-standalone", allowed_domains=None):
    import os
    os.environ.setdefault("COLLIE_BROWSER_BRIDGE", "1")   # drive the user's real browser via the bridge
    from .cli import make_harness
    from . import settings as _s
    _s.apply()
    h = make_harness(_browse_dir(), provider=_s.get("PROVIDER"), model=_s.get("MODEL"),
                     project="browse", embed="hash")
    # Prompt text is not an authority boundary.  Keep a positive list, wrap every
    # survivor in this Mission's isolated browser space, and force type.submit off.
    _restrict_browse_child(h, space, allowed_domains)
    h.self_verify = False
    try:
        h.force_edit = False
    except Exception:
        pass
    h.max_turns = int(os.environ.get("COLLIE_BROWSE_TURNS", "35"))
    prompt = (goal.strip() + "\n\n"
              "Act ONLY through the available reversible browser tools (browser_open / browser_snapshot / "
              "browser_fields / browser_type with a snapshot `ref` or `label` / browser_pick / "
              "browser_links / browser_read). Generic click, Enter, script, "
              "and upload are intentionally unavailable; if one is needed, stop and report the exact "
              "button/action so the outer Mission can gate it. The form is DYNAMIC: picking a "
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
              "CRITICAL: do NOT click any IRREVERSIBLE button (Publish, Post, Send, Pay, Place order, "
              "Next-to-publish) — fill everything up to that point and STOP, then report each field you "
              "filled and its final value.")
    res = h.run("browse", prompt)
    try:
        h.memory.close(); h.recorder.close()
    except Exception:
        pass
    return res.answer or res.error or ""


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
    "var sensitive=e.type==='password'||e.type==='email'||e.type==='tel'||/(pass(word|code)?|secret|token|api.?key|otp|one.?time|verification.?code|cvv|cvc|card.?number|ssn|social.?security|e.?mail|phone|mobile|street.?address|postal|zip.?code|birth|dob|user.?name)/i.test(meta);"
    "return {label:lab,value:sensitive?'[redacted]':val,sensitive:!!sensitive,filled:!!val};}).filter(x=>x.label&&x.filled))")


_SENSITIVE_FIELD = re.compile(
    r"pass(word|code)?|secret|token|api.?key|otp|one.?time|verification.?code|"
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


def _real_browse(runner=None, form_reader=None):
    def execute(rec):
        from .browserbridge import space_identity
        goal = (rec.args or {}).get("goal") or (rec.args or {}).get("task") or ""
        space = _mission_space(getattr(rec, "job_id", ""))
        domains = ((rec.args or {}).get("_leash") or {}).get("allowed_domains") or []
        out = runner(goal) if runner else _live_browse(goal, space, domains)
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
        parsed = urlsplit(str(ident.get("url") or ""))
        page = {"host": (parsed.hostname or "").lower(),
                "title": str(ident.get("title") or "")[:160]}
        return {"case": {"browsed": True, "browse_result": (out or "")[:600]},
                "result": out, "form": form, "form_actions": form_actions,
                "page": page}
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
    if a.get("read_only") is True:
        return True
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
    return read_intent and no_write


def _browse_verify(rec, result):
    """Done-check by an INDEPENDENT re-read of the form, not the agent's self-report.
    If the caller passed `expect` ({label: value}), assert each value is actually
    present in the re-read form (differential); otherwise confirm the form is
    substantially filled. A 'done' over an empty form is refuted here."""
    r = result or {}
    form = r.get("form") or []
    expect = (rec.args or {}).get("expect") or {}
    read_only = _explicit_read_only_browse(rec.args or {})
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
        if key in ("platform", "site", "origin"):
            return _page_present(val)
        # Rich editors expose unstable accessibility labels/data-testid values
        # (e.g. tweetTextarea_0).  Semantic *_text/body/content expectations are
        # verified against the fresh value of every filled editor, not a guessed
        # label, while ordinary form fields retain strict label+value matching.
        if key in ("text", "body", "content", "message", "tweet_text", "post_text") or key.endswith("_text"):
            return _value_present(val)
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


def _find_button(snapshot, button):
    wanted = str(button or "").strip().casefold()
    hits = []
    for line in str((snapshot or {}).get("snapshot") or "").splitlines():
        m = re.search(r"\[([^\]]+)\]\s+(button|link|menuitem)\s+\"([^\"]+)\"", line)
        if m and m.group(3).strip().casefold() == wanted:
            if re.search(r"×\s*[2-9]\d*|identical siblings", line, re.I):
                return None
            hits.append({"ref": m.group(1), "role": m.group(2),
                         "line": line.strip(),
                         "disabled": bool(re.search(r"\(disabled\)|\[disabled\]|aria-disabled", line, re.I))})
    buttons = [h for h in hits if h["role"] in ("button", "menuitem")]
    enabled = [h for h in buttons if not h["disabled"]]
    if buttons:
        return enabled[0] if len(enabled) == 1 else None
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
        ident = act.page_identity() or {}
        tree = act.snapshot() or {}
        target = _find_button(tree, button)
        full_url = tree.get("url") or ident.get("url")
        if not full_url or not target:
            raise RuntimeError("target page/button is missing or ambiguous; prepare the page again")
        u = urlsplit(str(full_url or ""))
        form = _actuator_form(act, _mission_space(job_id))
        form_json = json.dumps(form, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return {"space": _mission_space(job_id), "tab_id": ident.get("tab_id"),
                "title": ident.get("title") or "", "url": full_url,
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
        ident = act.page_identity() or {}
        tree = act.snapshot() or {}
        target = _find_button(tree, old.get("button"))
        full_url = tree.get("url") or ident.get("url")
        form = _actuator_form(act, _mission_space(getattr(rec, "job_id", "")))
        form_json = json.dumps(form, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        form_digest = hashlib.sha256(form_json.encode("utf-8")).hexdigest()
        return bool(target and ident.get("tab_id") == old.get("tab_id") and
                    full_url == old.get("url") and
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
            act.click_ref(ref)
        except Exception as e:
            return {"submitted": False, "error": "publish click failed: %s: %s" % (type(e).__name__, e)}
        old = getattr(rec, "snapshot", None) or {}
        try:
            ident = act.page_identity() or {}
            tree = act.snapshot() or {}
        except Exception as e:
            return {"submitted": True, "confirmed": False, "button": button,
                    "error": "clicked, but fresh postcondition read failed: %s" % e}
        new_url = str(tree.get("url") or ident.get("url") or "")
        page = "\n".join((str(ident.get("title") or ""),
                           str(tree.get("snapshot") or "")))
        failure = re.search(r"\b(error|required|could not|couldn't|failed|captcha|"
                            r"rate limit|try again|something went wrong)\b", page, re.I)
        success_text = str((rec.args or {}).get("success_text") or "").strip()
        success_url = str((rec.args or {}).get("success_url_contains") or "").strip()
        explicit = ((success_text and success_text.casefold() in page.casefold()) or
                    (success_url and success_url in new_url))
        marker = re.search(r"\b(published|posted|sent successfully|your post is live|"
                           r"view post|successfully published)\b", page, re.I)
        target_gone = _find_button(tree, old.get("button")) is None
        navigated = bool(new_url and new_url != str(old.get("url") or ""))
        permalink = re.search(
            r"/(?:posts?|status|items?|listings?|p|reels?|videos?|updates?)/[^/?#]+",
            urlsplit(new_url).path, re.I) if new_url else None
        confirmed = bool(not failure and (((explicit or marker) and
                                            (navigated or target_gone)) or
                                           (permalink and navigated and target_gone)))
        return {"case": {"published": True} if confirmed else {},
                "submitted": True, "confirmed": confirmed, "button": button,
                "target": new_url, "postcondition":
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


def _live_code(goal, workspace=None):
    import os
    from .cli import make_harness
    from . import settings as _s
    _s.apply()
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
                "verified": False}
    if not os.path.isdir(cwd):
        return {"answer": "approved code workspace does not exist", "verified": False}
    project = "mission-code-" + hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:12]
    h = make_harness(cwd, provider=_s.get("PROVIDER"), model=_s.get("MODEL"),
                     project=project, embed="hash", code_search=True, exec_code=False)
    # Positive authority list: a capability advertised as reversible cannot load
    # browser/desktop/MCP hands or a general shell behind Mission's outer gate.
    _restrict_code_child(h, cwd)
    h.max_turns = int(os.environ.get("COLLIE_CODE_TURNS", "30"))
    res = h.run("code", goal)
    verified = bool(getattr(res, "verified", False))
    try:
        h.memory.close(); h.recorder.close()
    except Exception:
        pass
    return {"answer": res.answer or res.error or "", "verified": verified}


def _real_code(runner=None):
    def execute(rec):
        goal = (rec.args or {}).get("goal") or (rec.args or {}).get("task") or ""
        ws = (rec.args or {}).get("workspace") or (rec.args or {}).get("cwd")
        out = (runner or (lambda g: _live_code(g, ws)))(goal)
        if isinstance(out, str):
            out = {"answer": out, "verified": False}
        return {"case": {"coded": True, "code_verified": bool(out.get("verified"))},
                "result": out.get("answer", ""), "verified": bool(out.get("verified"))}
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
    if r.get("verified"):
        return Verdict(VERIFIED, "code change executed-verified (repro RED->GREEN)")
    if r.get("result"):
        return Verdict(INCONCLUSIVE, "code edited but not executed-verified — a human should check")
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
                        research_runner=None, browse_runner=None, code_runner=None):
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
        "final submit then STOPS (reversible). Pass `expect` using exact visible field labels. For a "
        "rich-text editor use content/body/post_text; platform/site is checked against the live page "
        "origin. For inspection/navigation with no form changes pass read_only=true (an explicit "
        "inspect + do-not-change/submit goal is also recognized fail-closed). The outcome is verified "
        "by an INDEPENDENT re-read, not the agent's say-so.",
        args_hint='{"goal": "fill a Marketplace vehicle listing for a 2015 Corolla, $9500", '
                  '"expect": {"Make":"Toyota","Model":"Corolla","Year":"2015","Price":"9500"}, '
                  '"read_only": false}'))
    register(Capability(
        name="browse.submit", execute=bsubmit_exec, verify=bsubmit_verify, reversible=False,
        risk="publish", snapshot=bsubmit_snapshot, unchanged=bsubmit_unchanged,
        resource=browser_resource,
        description="Click one exact snapshotted final IRREVERSIBLE button (Publish / Post) "
        "after `browse` has filled the form. Gated — parks for your confirm.",
        args_hint='{"button": "Publish"}', semantic_args=_semantic_browse_submit))
    register(Capability(
        name="code", execute=code_exec, verify=code_verify, reversible=True, risk="code",
        resource=code_resource,
        description="Read / write / refactor code inside one explicitly approved workspace using "
        "a filesystem-confined child. Mission grants no shell; unverified edits hand off for review.",
        args_hint='{"goal": "fix the null-pointer in parser.py", "workspace": "/path/to/repo"}'))
    return [get_capability(name) for name in
            ("research", "compose", "observe", "web.submit", "web.send",
             "browse", "browse.submit", "code")]
