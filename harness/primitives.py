"""Neutral primitives — the small, domain-agnostic action set a mission draws on.

This is the answer to "don't template every errand": instead of marketplace.* /
dentist.* / refund.* capabilities, there is ONE generic set the model composes
toward any goal. Selling a car, booking a table, chasing a refund all use the
SAME five — only the args (which the model fills) differ:

  research   (read)        gather facts from the web toward a question
  compose    (read)        turn facts into text (a listing, a reply, an email)
  observe    (read)        re-observe the world (logged-out fetch for evidence, or
                           an authed browser read to poll an inbox)
  web.submit (IRREVERSIBLE) fill + submit a form (publish a listing, place an order)
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
import re
import time

from .jobs import Capability, register
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
    text = a.get("text") or f"(stub) composed text about {facts!r}"
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


def _real_compose(provider=None):
    def execute(rec):
        a = rec.args or {}
        facts = a.get("facts") or a.get("about") or a.get("_case") or a.get("query") or ""
        prov = provider or _get_provider()
        text = a.get("text") or ""
        if not text and prov is not None:
            sys = ("Write a concise, HONEST piece of text for the user's errand from these "
                   "facts — a marketplace listing, a reply, or an email as appropriate. "
                   "Plain text only, no preamble.")
            try:
                comp = prov.complete(
                    sys, [{"role": "user", "content": json.dumps(facts, ensure_ascii=False)[:3000]}], [])
                if getattr(comp, "stop_reason", "") != "error":
                    text = (getattr(comp, "text", "") or "").strip()
            except Exception:
                text = ""
        if not text:                     # no model / empty -> a plain factual fallback
            text = facts if isinstance(facts, str) else json.dumps(facts, ensure_ascii=False)
        return {"case": {"composed": True, "draft": text}, "text": text}
    return execute


def _compose_verify(rec, result):
    if (result or {}).get("text"):
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
            act = actuator or _live_actuator()
            if act is None:
                return {"case": {"observe_count": n}, "present": None,
                        "detail": "no browser to read the authed page"}
            try:
                act.open(url)
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
        act = actuator or _live_actuator()
        if act is None:
            return {"submitted": False, "error": "no browser available (start `collie browser-bridge` and connect the extension)"}
        try:
            act.open(url)
            for sel, text in (fields.items() if isinstance(fields, dict) else []):
                act.type(sel, text)
            result_url = act.click(submit_sel) if submit_sel else act.current_url()
        except Exception as e:
            return {"submitted": False, "error": f"submit failed: {type(e).__name__}: {e}"}
        return {"case": {"submitted": True, "url": result_url or url},
                "submitted": True, "url": result_url or url, "published_at": time.time(),
                "expect_title": a.get("expect_title") or a.get("title") or "",
                "price_max": a.get("price_floor")}
    return execute


def _real_submit_verify(rec, result):
    r = result or {}
    if not r.get("submitted") or not r.get("url"):
        return Verdict(FAILED, r.get("error") or "submit did not complete")
    # INDEPENDENT channel: a logged-out re-fetch must show the listing (observe.py).
    from .observe import donecheck_listing
    now = time.time()
    return donecheck_listing(r["url"], r.get("expect_title") or "",
                             price_max=r.get("price_max"),
                             at=now, publish_at=r.get("published_at") or (now - 1))


def _real_web_send(actuator=None):
    def execute(rec):
        a = rec.args or {}
        url = a.get("url") or ""
        text = a.get("text") or ""
        msg_sel = a.get("selector") or a.get("message_selector") or ""
        send_sel = a.get("send") or a.get("send_selector") or ""
        act = actuator or _live_actuator()
        if act is None:
            return {"sent": False, "error": "no browser available"}
        try:
            if url:
                act.open(url)
            if msg_sel:
                act.type(msg_sel, text)
            if send_sel:
                act.click(send_sel)
        except Exception as e:
            return {"sent": False, "error": f"send failed: {type(e).__name__}: {e}"}
        return {"case": {"sent": True, "last_sent_to": a.get("to") or url},
                "sent": True, "to": a.get("to") or url, "text": text}
    return execute


def _real_send_verify(rec, result):
    r = result or {}
    if not r.get("sent"):
        return Verdict(FAILED, r.get("error") or "message not sent")
    # A DM has no logged-out channel to confirm receipt independently; the send is
    # the deliverable and it was gated by a human confirm before firing. Honest:
    # verified as SENT, not as READ.
    return Verdict(VERIFIED, "message sent (acting channel; no independent read-receipt)")


def _live_actuator():
    from .webact import get_actuator
    return get_actuator()


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


def _live_browse(goal):
    import os
    os.environ.setdefault("COLLIE_BROWSER_BRIDGE", "1")   # drive the user's real browser via the bridge
    from .cli import make_harness
    from . import settings as _s
    _s.apply()
    h = make_harness(_browse_dir(), provider=_s.get("PROVIDER"), model=_s.get("MODEL"),
                     project="browse", embed="hash")
    for name in ("edit_file", "write_file", "bash", "run_in_env"):   # act ONLY through the browser
        h.registry._tools.pop(name, None)
    h.self_verify = False
    try:
        h.force_edit = False
    except Exception:
        pass
    h.max_turns = int(os.environ.get("COLLIE_BROWSE_TURNS", "35"))
    prompt = (goal.strip() + "\n\n"
              "Act ONLY through the browser_* tools (browser_open / browser_fields / browser_type with a "
              "`label` / browser_pick / browser_click / browser_read). The form is DYNAMIC: picking a "
              "value can REVEAL or CHANGE other fields (e.g. after Vehicle type, Make becomes a dropdown "
              "and Mileage/Body-style/Condition appear).\n"
              "WORKFLOW — repeat until complete:\n"
              "  1. call browser_fields to list the CURRENT fields (label, kind text/dropdown, value);\n"
              "  2. fill every empty one — browser_type(label,text) for text, browser_pick(label,option) "
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
    "JSON.stringify([...document.querySelectorAll('input,textarea,[role=combobox]')].map(e=>{"
    "var l=e.closest('label');var lab=l?(l.innerText||'').trim().split('\\n')[0]:(e.getAttribute('aria-label')||'');"
    "var val=e.getAttribute('role')==='combobox'?(l?(l.innerText||'').replace(/\\n/g,' ').trim():''):(e.value||'');"
    "return {label:lab,value:val};}).filter(x=>x.label&&x.value&&x.value!==x.label))")


def _read_form():
    from . import browserbridge as _bb
    try:
        r = _bb._call({"action": "eval", "expr": _FORM_SNAPSHOT})
        data = r.get("data", {}).get("value") if isinstance(r, dict) else None
        return json.loads(data) if isinstance(data, str) else (data or [])
    except Exception:
        return []


def _real_browse(runner=None, form_reader=None):
    def execute(rec):
        goal = (rec.args or {}).get("goal") or (rec.args or {}).get("task") or ""
        out = (runner or _live_browse)(goal)
        form = (form_reader or _read_form)()          # INDEPENDENT re-read for the done-check
        return {"case": {"browsed": True, "browse_result": (out or "")[:600]},
                "result": out, "form": form}
    return execute


def _browse_verify(rec, result):
    """Done-check by an INDEPENDENT re-read of the form, not the agent's self-report.
    If the caller passed `expect` ({label: value}), assert each value is actually
    present in the re-read form (differential); otherwise confirm the form is
    substantially filled. A 'done' over an empty form is refuted here."""
    r = result or {}
    form = r.get("form") or []
    expect = (rec.args or {}).get("expect") or {}
    if not r.get("result") and not form:
        return Verdict(FAILED, "browse produced no result")

    def _norm(s):                                    # ignore $, commas, spacing ("9500" == "$9,500")
        return re.sub(r"[^a-z0-9]", "", str(s).lower())

    def _present(label, val):
        lab, v = str(label).lower(), _norm(val)
        return bool(v) and any(lab in str(f.get("label", "")).lower() and v in _norm(f.get("value", ""))
                               for f in form)

    if expect:
        missing = [k for k, v in expect.items() if not _present(k, v)]
        if missing:
            return Verdict(FAILED, "form fields NOT confirmed filled: " + ", ".join(missing))
        ev = Observation(channel="form-reread", at=1, ok=True, asserted=True,
                         detail="; ".join("%s=%s" % (k, v) for k, v in expect.items()))
        return Verdict(VERIFIED, "independently confirmed %d field(s) filled" % len(expect), (ev,))
    # no expected values -> at least confirm the form is substantially filled
    if len(form) >= 3:
        return Verdict(VERIFIED, "form re-read shows %d filled field(s)" % len(form))
    return Verdict(INCONCLUSIVE,
                   "could not confirm the form was filled (re-read found %d field(s))" % len(form))


def _real_browse_submit(actuator=None):
    def execute(rec):
        button = (rec.args or {}).get("button") or (rec.args or {}).get("text") or "Publish"
        act = actuator or _live_actuator()
        if act is None:
            return {"submitted": False, "error": "no browser available"}
        try:
            act.click_text(button)
        except Exception as e:
            return {"submitted": False, "error": "publish click failed: %s: %s" % (type(e).__name__, e)}
        return {"case": {"published": True}, "submitted": True, "button": button}
    return execute


def _browse_submit_verify(rec, result):
    r = result or {}
    if not r.get("submitted"):
        return Verdict(FAILED, r.get("error") or "publish click did not fire")
    return Verdict(VERIFIED, "clicked %r (human-confirmed)" % r.get("button"))


def _stub_browse(rec):
    goal = (rec.args or {}).get("goal") or ""
    # a canned re-read so the (real) _browse_verify has a form to check against
    form = [{"label": "Make", "value": "Toyota"}, {"label": "Model", "value": "Corolla"},
            {"label": "Price", "value": "$9,500"}]
    return {"case": {"browsed": True}, "result": "(stub) filled the form for: " + goal[:60],
            "form": form}


def _stub_browse_submit(rec):
    return {"case": {"published": True}, "submitted": True,
            "button": (rec.args or {}).get("button") or "Publish"}


# ── code: coding is a capability like any other — run collie's coding agent ───
# The delegate's positioning is a human-delegate; coding is ONE function under it.
# `code` runs collie's own coding loop (read/edit/search/run) with its executed-
# verification gate, so a mission can compose a coding step with web/world steps.
def _live_code(goal, workspace=None):
    import os
    from .cli import make_harness
    from . import settings as _s
    _s.apply()
    cwd = workspace or os.getcwd()
    h = make_harness(cwd, provider=_s.get("PROVIDER"), model=_s.get("MODEL"),
                     project="code", embed="hash", code_search=True, exec_code=True)
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
    else:
        research_exec, research_verify = _real_research(research_runner), _real_research_verify
        compose_exec, compose_verify = _real_compose(provider), _compose_verify
        observe_exec, observe_verify = _real_observe(actuator), _read_verify
        submit_exec, submit_verify = _real_web_submit(actuator), _real_submit_verify
        send_exec, send_verify = _real_web_send(actuator), _real_send_verify
        browse_exec, browse_verify = _real_browse(browse_runner), _browse_verify
        bsubmit_exec, bsubmit_verify = _real_browse_submit(actuator), _browse_submit_verify
        code_exec, code_verify = _real_code(code_runner), _code_verify

    register(Capability(
        name="research", execute=research_exec, verify=research_verify, reversible=True,
        risk="read", description="Gather facts from the web toward a question.",
        args_hint='{"query"}'))
    register(Capability(
        name="compose", execute=compose_exec, verify=compose_verify, reversible=True,
        risk="read", description="Turn facts into text (listing / reply / email).",
        args_hint='{"facts","text"}'))
    register(Capability(
        name="observe", execute=observe_exec, verify=observe_verify, reversible=True,
        risk="read", description="Re-observe the world (logged-out fetch for evidence, "
        "or authed browser read to poll an inbox).",
        args_hint='{"url","expect","authed"}'))
    register(Capability(
        name="web.submit", execute=submit_exec, verify=submit_verify, reversible=False,
        risk="publish", description="Fill and submit a form (publish / place an order).",
        args_hint='{"url","fields","submit","expect_title"}'))
    register(Capability(
        name="web.send", execute=send_exec, verify=send_verify, reversible=False,
        risk="send", description="Send a message (reply / negotiate / email).",
        args_hint='{"url","selector","text","send"}'))
    register(Capability(
        name="browse", execute=browse_exec, verify=browse_verify, reversible=True, risk="read",
        description="Do a task on a website by driving the real browser adaptively (fill a form, "
        "navigate, act) — handles dynamic/obfuscated sites like Facebook Marketplace. Fills up to the "
        "final submit then STOPS (reversible). Pass `expect` (the field->value map you intend to fill) "
        "so the outcome is verified by an INDEPENDENT re-read of the form, not the agent's say-so.",
        args_hint='{"goal": "fill a Marketplace vehicle listing for a 2015 Corolla, $9500", '
                  '"expect": {"Make":"Toyota","Model":"Corolla","Year":"2015","Price":"9500"}}'))
    register(Capability(
        name="browse.submit", execute=bsubmit_exec, verify=bsubmit_verify, reversible=False,
        risk="publish", description="Click the final IRREVERSIBLE button (Publish / Post / Place "
        "order) after `browse` has filled the form. Gated — parks for your confirm.",
        args_hint='{"button": "Publish"}'))
    register(Capability(
        name="code", execute=code_exec, verify=code_verify, reversible=True, risk="code",
        description="Write / fix / refactor code in a workspace by running collie's coding agent "
        "(read/edit/search/run) with its executed-verification gate (a repro that fails on the broken "
        "code, an edit that flips it, a re-run that passes). Reversible (version control). Use for the "
        "coding step of an errand.",
        args_hint='{"goal": "fix the null-pointer in parser.py", "workspace": "/path/to/repo"}'))
