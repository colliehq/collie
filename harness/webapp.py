"""collie web GUI — a local, stdlib-only Server-Sent-Events front end for the harness.

    python -m harness.webapp            # serve on :8787 and open a browser
    COLLIE_PROVIDER=anthropic python -m harness.webapp --port 9000

Why stdlib-only: collie ships no web framework. `http.server.ThreadingHTTPServer` gives us
concurrent request handling (the long-lived SSE stream in one thread never blocks the sidebar's
`/api/sessions` poll in another), and `webbrowser.open` pops the UI — zero pip installs.

The run itself streams LIVE: we set `h.emit` to a callback that writes each harness event
(tool / edit / repro / receipt) straight onto the open SSE socket as it happens, so the browser
paints the verification gate flipping fail -> pass in real time instead of waiting for one blob.
Because `Harness.run` is synchronous and calls `emit` on THIS handler thread, no queue is needed —
the callback writes to `self.wfile` directly. `Harness._emit` already swallows callback
exceptions, so a client that closes mid-run can never crash the run.

Routes:
    GET /                       -> webui/index.html (the whole UI, one self-contained file)
    GET /api/sessions           -> sessions.recent()           (sidebar thread list)
    GET /api/session/<id>       -> sessions.load(id)           (click a thread to reload it)
    GET /api/stream?session=&q= -> text/event-stream           (one run, streamed live)
"""
from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "webui", "index.html")
# logo ships INSIDE the package (webui/logo.svg) so a pip-installed wheel serves it; the repo's
# assets/ copy is the fallback for an editable checkout that predates the move.
LOGO_SVG = os.path.join(HERE, "webui", "logo.svg")
if not os.path.exists(LOGO_SVG):
    LOGO_SVG = os.path.join(os.path.dirname(HERE), "assets", "collie-logo.svg")

# CSRF guard. `collie web` binds 127.0.0.1, but that does NOT stop a *drive-by* request: any web
# page the user has open can fire `new Image().src="http://127.0.0.1:8787/api/stream?q=rm -rf …"`,
# and the agent (which runs bash + edits files) would execute it server-side — the attacker never
# needs to read the response. Defence: a per-process secret minted at import, injected into the
# served HTML (same-origin, so cross-site JS can't read it), and required on every state-changing /
# code-executing route. Same-origin requests from our own page carry it; cross-site ones can't.
TOKEN = os.urandom(16).hex()
# Extra Host values `_host_ok` accepts, populated only by `collie web --lan` with this machine's own
# addresses. Empty by default: loopback-only, exactly as before.
LAN_HOSTS = set()
# Pairing: a phone never receives TOKEN over the network. It reads a one-shot secret off a code shown
# on THIS machine's screen and trades it at /api/pair. Secrets are 8 bytes (64 bits — unguessable),
# live for _PAIR_TTL seconds, and are burned on first use.
_PAIR_TTL = 180
_PAIR_LOCK = threading.Lock()
_PAIR_LIVE = {}                      # secret(hex) -> expiry timestamp
_PAIR_FAILS = []                     # timestamps of failed redemptions, for a crude rate limit

# /api/desktop/audio proxies IP+time-locked CDN audio so playback is same-origin (Web Audio analyser
# works, Range/seek forwards). It fetches an arbitrary URL, so it is an SSRF surface: only these CDN
# hosts are allowed, and only over https. Kept module-level so it is unit-testable.
_AUDIO_OK_HOSTS = ("googlevideo.com", "bilivideo.com", "bilivideo.cn", "akamaized.net", "hdslb.com")

# MCP OAuth runs in a thread: it opens a browser and waits for the redirect, which can take a minute
# of human time — far too long to hold a request open. The panel starts it and then watches the auth
# state, so what it shows is the real outcome. Failures are kept here because otherwise a login that
# quietly failed is indistinguishable from one the user simply has not finished.
_MCP_LOGIN_ERR = {}                  # server name -> last login error
_MCP_LOGIN_BUSY = set()              # server names with a login in flight


def _web_plan_scope(session):
    """The browser and the model's PlanTool must address the exact same artifact."""
    return "web:" + str(session or "").strip()


def _review_findings(answer):
    """Turn a read-only Review answer into selectable, structured findings.

    Prefer a JSON ``findings`` block when the model supplied one, while accepting
    ordinary review bullets as a useful fallback.  This is deliberately an
    artifact parser, not another model call: Review remains read-only and its
    handoff stays deterministic/auditable.
    """
    text = str(answer or "")
    candidates = []
    for raw in re.findall(r"```(?:json)?\s*(.*?)```", text, re.I | re.S):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            parsed = parsed.get("findings")
        if isinstance(parsed, list):
            candidates = parsed
            break
    if not candidates:
        # [high] path/to/file.py:42 - explanation
        pattern = re.compile(
            r"^\s*(?:[-*]\s*)?(?:\[(critical|high|medium|low|info)\]\s*)?"
            r"(?:`?([^`:\n]+\.[A-Za-z0-9_+-]+)`?)(?::(\d+))?\s*[-:–—]\s*(.+)$",
            re.I)
        for line in text.splitlines():
            match = pattern.match(line)
            if match:
                candidates.append({"severity": match.group(1) or "medium",
                                   "path": match.group(2).strip(),
                                   "line": match.group(3), "message": match.group(4).strip()})
    findings = []
    for item in candidates[:100]:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or item.get("description") or "").strip()[:4000]
        if not message:
            continue
        severity = str(item.get("severity") or "medium").strip().lower()
        if severity not in ("critical", "high", "medium", "low", "info"):
            severity = "medium"
        path = str(item.get("path") or item.get("file") or "").strip()[:500]
        try:
            line = int(item.get("line")) if item.get("line") not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        key = "%s\0%s\0%s\0%s" % (path, line or "", severity, message)
        findings.append({"id": "finding-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12],
                         "path": path, "line": line, "severity": severity,
                         "message": message})
    return findings


def _latest_review_artifact(session):
    from . import sessions
    saved = sessions.load(str(session or "").strip()) or {}
    for receipt in reversed(saved.get("run_receipts") or []):
        findings = receipt.get("review_findings") if isinstance(receipt, dict) else None
        if isinstance(findings, list):
            return {"session": str(session), "run": receipt.get("run"),
                    "readonly": True, "findings": findings}
    return {"session": str(session), "run": None, "readonly": True, "findings": []}


def _plan_build_prompt(artifact):
    lines = ["Implement the user-approved plan below. Treat it as the execution brief, inspect the "
             "current workspace before editing, and verify the result."]
    if artifact.get("title"):
        lines.append("\nPlan: " + artifact["title"])
    if artifact.get("files"):
        lines.append("Files in scope:\n" + "\n".join("- " + x for x in artifact["files"]))
    if artifact.get("risks"):
        lines.append("Risks to handle:\n" + "\n".join("- " + x for x in artifact["risks"]))
    if artifact.get("todos"):
        lines.append("Tasks:\n" + "\n".join(
            "- [%s] %s (%s)" % ("x" if t.get("status") == "completed" else " ",
                                 t.get("content", ""), t.get("id", ""))
            for t in artifact["todos"]))
    if artifact.get("checks"):
        lines.append("Checks:\n" + "\n".join("- " + c.get("command", "")
                                               for c in artifact["checks"]))
    return "\n\n".join(lines)


def _review_build_prompt(findings):
    lines = ["Fix only the review findings selected by the user. Inspect each location before "
             "editing and run relevant checks after the fixes."]
    for finding in findings:
        loc = finding.get("path") or "(unspecified file)"
        if finding.get("line") is not None:
            loc += ":" + str(finding["line"])
        lines.append("- [%s] %s — %s" % (finding.get("severity", "medium"), loc,
                                          finding.get("message", "")))
    return "\n".join(lines)


def _state_root():
    from .controlplane import state_dir
    return state_dir()


def _public_recovery(row, session_id=""):
    """Recovery metadata safe for an operations surface (never transcript/tool args)."""
    row = row if isinstance(row, dict) else {}
    return {k: v for k, v in {
        "session_id": session_id or row.get("session_id"), "run_id": row.get("run_id"),
        "turn": row.get("turn"), "state": row.get("state"), "updated": row.get("updated"),
        "recovery_required": bool(row.get("recovery_required")),
        "auto_resumable": bool(row.get("auto_resumable")), "reason": row.get("reason"),
    }.items() if v is not None}


def _public_task_run(row):
    """Specialist lifecycle without task, result, leash, resources or workspace content."""
    row = row if isinstance(row, dict) else {}
    keys = ("run_id", "parent_run_id", "root_run_id", "mission_id", "depth", "role",
            "status", "background", "cancel_requested", "cancel_ack_at", "progress_seq",
            "progress_at", "input_tokens", "output_tokens", "model_cost_usd",
            "active_wall_ms", "retry_count", "created_at", "updated_at")
    return {key: row.get(key) for key in keys if row.get(key) is not None}


def _public_tree(value):
    value = value if isinstance(value, dict) else {}
    root = value.get("root")
    return {"root": _public_task_run(root) if isinstance(root, dict) else None,
            "flat": [_public_task_run(row) for row in (value.get("flat") or [])
                     if isinstance(row, dict)]}


def _public_activity(raw):
    """Allowlisted Activity response; raw control-plane rows contain private work content."""
    raw = raw if isinstance(raw, dict) else {}
    missions = [{k: row.get(k) for k in ("mission_id", "state", "updated_at", "lane")
                 if row.get(k) is not None}
                for row in (raw.get("missions") or []) if isinstance(row, dict)]
    automations = [{k: row.get(k) for k in
                    ("execution_id", "automation_id", "state", "attempts", "created_at",
                     "updated_at", "started_at", "finished_at") if row.get(k) is not None}
                   for row in (raw.get("automations") or []) if isinstance(row, dict)]
    notifications = [{k: row.get(k) for k in
                      ("notification_id", "run_id", "kind", "state", "created_at", "acked_at")
                      if row.get(k) is not None}
                     for row in (raw.get("notifications") or []) if isinstance(row, dict)]
    return {
        "at": raw.get("at"),
        "sessions": [_public_recovery(row) for row in (raw.get("sessions") or [])],
        "missions": missions,
        "task_runs": [_public_task_run(row) for row in (raw.get("task_runs") or [])],
        "automations": automations, "notifications": notifications,
        # Lane failures are useful; exception strings are not guaranteed to be
        # content-free, so expose presence without reflecting arbitrary text.
        "errors": {str(k): "unavailable" for k in (raw.get("errors") or {})},
    }


def _public_health(raw):
    raw = raw if isinstance(raw, dict) else {}
    workers = {}
    for name, row in (raw.get("workers") or {}).items():
        row = row if isinstance(row, dict) else {}
        workers[str(name)] = {k: row.get(k) for k in ("state", "fresh", "age_s", "pid")
                              if row.get(k) is not None}
    heartbeats = {}
    for name, row in (raw.get("heartbeats") or {}).items():
        row = row if isinstance(row, dict) else {}
        heartbeats[str(name)] = {k: row.get(k) for k in
                                 ("state", "fresh", "age_s", "pid", "updated_at", "expires_at")
                                 if row.get(k) is not None}
    work = raw.get("work") if isinstance(raw.get("work"), dict) else {}
    recovery = [{k: row.get(k) for k in
                 ("kind", "session_id", "run_id", "parent_run_id", "execution_id",
                  "automation_id", "state", "status", "role", "reason")
                 if row.get(k) is not None}
                for row in (work.get("recovery_required") or []) if isinstance(row, dict)]
    supervisor = raw.get("supervisor") if isinstance(raw.get("supervisor"), dict) else {}
    services_raw = raw.get("services") if isinstance(raw.get("services"), dict) else {}
    services = {}
    if isinstance(services_raw.get("web"), dict):
        services["web"] = {"ok": bool(services_raw["web"].get("ok"))}
    if isinstance(services_raw.get("browser"), dict):
        browser = services_raw["browser"]
        services["browser"] = {
            "ok": bool(browser.get("ok")),
            "extension_connected": bool(browser.get("extension_connected")),
            "last_poll_secs_ago": browser.get("last_poll_secs_ago"),
        }
    credentials = [{k: row.get(k) for k in
                    ("name", "state", "expires_at", "seconds_remaining",
                     "refresh_available", "refresh_owner", "action")
                    if row.get(k) is not None}
                   for row in (raw.get("credentials") or []) if isinstance(row, dict)]
    queues_raw = raw.get("queues") if isinstance(raw.get("queues"), dict) else {}
    queues = {}
    for name in ("slack", "notifications"):
        row = queues_raw.get(name)
        if isinstance(row, dict):
            # Queue health is counts only. Never forward a future payload/error field.
            queues[name] = {str(k): value for k, value in row.items()
                            if isinstance(value, (int, float, bool)) and not isinstance(value, str)}
    return {
        "ok": bool(raw.get("ok")), "status": raw.get("status") or "unknown", "at": raw.get("at"),
        "workers": workers, "heartbeats": heartbeats,
        "services": services, "credentials": credentials, "queues": queues,
        "supervisor": {k: supervisor.get(k) for k in
                       ("installed", "enabled", "running", "status", "mode", "task_name",
                        "last_result") if supervisor.get(k) is not None},
        "work": {"interactive_active": work.get("interactive_active", 0),
                 "missions_active": work.get("missions_active", 0),
                 "task_runs_active": work.get("task_runs_active", 0),
                 "automations_active": work.get("automations_active", 0),
                 "recovery_required": recovery},
        "activity_errors": {str(k): "unavailable" for k in (raw.get("activity_errors") or {})},
    }


def _public_specialist(value):
    value = value if isinstance(value, dict) else {}
    out = {}
    if isinstance(value.get("run"), dict): out["run"] = _public_task_run(value["run"])
    if isinstance(value.get("tree"), dict): out["tree"] = _public_tree(value["tree"])
    if isinstance(value.get("events"), list):
        out["events"] = [{k: event.get(k) for k in ("event_id", "kind", "at")
                          if event.get(k) is not None}
                         for event in value["events"] if isinstance(event, dict)]
    for key in ("mission_id", "run_id", "available", "attached", "queued", "message_id", "error"):
        if value.get(key) is not None: out[key] = value.get(key)
    return out

# A Web/Desktop process is already Collie's long-lived local process, so it also
# wakes due Missions. The ticker owns no plan: each pass only finds durable rows
# and calls the Mission driver, which asks Collie for the next action. SQL run
# tokens make this safe alongside `collie jobs daemon` and manual Check now.
_MISSION_TICK_LOCK = threading.Lock()
_MISSION_TICK_THREAD = None
_MISSION_TICK_ERROR = ""


def start_mission_ticker(interval=30.0):
    """Start one process-local Mission wake loop (idempotent)."""
    global _MISSION_TICK_THREAD
    with _MISSION_TICK_LOCK:
        if _MISSION_TICK_THREAD and _MISSION_TICK_THREAD.is_alive():
            return _MISSION_TICK_THREAD

        def _loop():
            global _MISSION_TICK_ERROR
            while True:
                svc = None
                try:
                    from . import settings
                    from .missionweb import MissionService
                    settings.apply()
                    if (settings.get("PROVIDER") or "") in ("", "mock"):
                        raise RuntimeError("configure a real provider to run Missions")
                    svc = MissionService()
                    svc.tick()
                    _MISSION_TICK_ERROR = ""
                except Exception as e:
                    # No provider/network is recoverable: leave durable rows intact
                    # and try again. The Web request/status surface stays available.
                    _MISSION_TICK_ERROR = "%s: %s" % (type(e).__name__, e)
                finally:
                    if svc is not None:
                        try:
                            svc.close()
                        except Exception:
                            pass
                time.sleep(max(1.0, float(interval)))

        _MISSION_TICK_THREAD = threading.Thread(
            target=_loop, name="collie-mission-ticker", daemon=True)
        _MISSION_TICK_THREAD.start()
        return _MISSION_TICK_THREAD


def _audio_host_ok(target):
    """True only for an https URL whose host is one of _AUDIO_OK_HOSTS — matched EXACTLY or as a
    DOTTED subdomain. 'evilgooglevideo.com' must NOT pass (a bare endswith would let it through)."""
    if not (target or "").startswith("https://"):
        return False
    host = (urllib.parse.urlparse(target).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _AUDIO_OK_HOSTS)


def _pair_mint():
    """A fresh pairing secret. Also expires stale ones, so the dict can't grow."""
    import time as _time
    secret = os.urandom(8).hex()
    now = _time.time()
    with _PAIR_LOCK:
        for old, expiry in list(_PAIR_LIVE.items()):
            if expiry <= now:
                _PAIR_LIVE.pop(old, None)
        if len(_PAIR_LIVE) > 8:                      # only the newest few screens can be live
            for old in sorted(_PAIR_LIVE, key=_PAIR_LIVE.get)[:-8]:
                _PAIR_LIVE.pop(old, None)
        _PAIR_LIVE[secret] = now + _PAIR_TTL
    return secret


def _pair_kdf(secret_hex, label, nonce_hex):
    """HMAC-SHA256(secret, "collie-pair-v1|<label>|<nonce>") — one derivation for proofs and keys."""
    import hashlib
    import hmac
    key = bytes.fromhex(secret_hex)
    msg = ("collie-pair-v1|%s|%s" % (label, nonce_hex)).encode("ascii")
    return hmac.new(key, msg, hashlib.sha256).digest()


def _pair_redeem(secret):
    """(ok, detail). Constant-time compare, one shot, TTL, and a 10-per-minute failure ceiling.

    Kept for the plain path and for tests; the wire protocol uses `_pair_prove` so the secret itself
    never travels."""
    import hmac
    import time as _time
    now = _time.time()
    with _PAIR_LOCK:
        _PAIR_FAILS[:] = [t for t in _PAIR_FAILS if now - t < 60]
        if len(_PAIR_FAILS) >= 10:
            return False, "too many pairing attempts, wait a minute"
        match = None
        for live, expiry in list(_PAIR_LIVE.items()):
            if expiry <= now:
                _PAIR_LIVE.pop(live, None)
                continue
            if hmac.compare_digest(live, secret or ""):
                match = live
        if match is None:
            _PAIR_FAILS.append(now)
            return False, "unknown or expired pairing code"
        _PAIR_LIVE.pop(match, None)                  # burn it: one code, one pairing
    return True, "ok"


def _pair_prove(nonce_hex, proof_hex):
    """Challenge–response redemption: the client proves it knows a live secret without sending it.

    Why not just POST the secret: pairing happens over plain HTTP on a LAN, so anyone able to
    ARP-spoof the server would collect the secret and pair themselves. Here the client sends a fresh
    nonce plus HMAC(secret, "client"|nonce); the server answers with HMAC(secret, "server"|nonce) —
    which proves it is the real collie, since an impostor cannot compute it — and returns the token
    XORed with HMAC(secret, "token"|nonce), so a passive listener (and an active impostor) get
    nothing usable. The secret is burned either way.

    Returns (ok, detail_or_payload).
    """
    import hmac
    import time as _time
    if len(nonce_hex or "") < 16 or len(proof_hex or "") != 64:
        return False, "malformed pairing challenge"
    try:
        bytes.fromhex(nonce_hex)
        bytes.fromhex(proof_hex)
    except ValueError:
        return False, "malformed pairing challenge"

    now = _time.time()
    with _PAIR_LOCK:
        _PAIR_FAILS[:] = [t for t in _PAIR_FAILS if now - t < 60]
        if len(_PAIR_FAILS) >= 10:
            return False, "too many pairing attempts, wait a minute"
        match = None
        for live, expiry in list(_PAIR_LIVE.items()):
            if expiry <= now:
                _PAIR_LIVE.pop(live, None)
                continue
            expected = _pair_kdf(live, "client", nonce_hex).hex()
            if hmac.compare_digest(expected, proof_hex):
                match = live
        if match is None:
            _PAIR_FAILS.append(now)
            return False, "unknown or expired pairing code"
        _PAIR_LIVE.pop(match, None)

    raw = bytes.fromhex(TOKEN)
    stream = _pair_kdf(match, "token", nonce_hex)
    sealed = bytes(a ^ b for a, b in zip(raw, stream)).hex()
    return True, {"server_proof": _pair_kdf(match, "server", nonce_hex).hex(),
                  "sealed_token": sealed}


def _esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _intent_summary(r):
    """One line describing what the desktop just did, for the transcript."""
    name = r.get("arg") or r.get("query") or ""
    if r.get("ok") is False:
        return "Couldn\u2019t do that%s%s" % (": " + name if name else "",
                                               " \u2014 " + r["error"] if r.get("error") else "")
    # Every action the router can return — music/app/focus/quit/windows/system/project/stop/agent.
    # A missing one falls through to "Done", which tells the reader nothing about what happened to
    # their machine; that is worth less than no entry at all.
    action = r.get("action")
    if action == "app":
        return "Opened %s." % (name or "it")
    if action == "focus":
        return "Switched to %s." % (name or "it")
    if action == "quit":
        return "Quit %s." % (name or "it")
    if action == "windows":
        return "Arranged the windows%s." % (" for " + name if name else "")
    if action == "system":
        return "%s." % (name or "Done").capitalize()
    if action == "project":
        return "Opened the project %s." % name if name else "Opened the project."
    if action == "stop":
        return "Stopped the music." if r.get("stopped_audio") else "Stopped."
    return "Done."


def _play_summary(r):
    if not r.get("ok"):
        return "Couldn\u2019t find that%s" % (" \u2014 " + r["error"] if r.get("error") else ".")
    who = r.get("uploader") or ""
    line = "\u25b6 Playing %s%s." % (r.get("title") or "it", " \u2014 " + who if who else "")
    # Say where the off switch is, now, while they are looking. Anything the agent starts and leaves
    # running must be stoppable without asking the agent a second time — and a control nobody can
    # find is not a control.
    if r.get("menubar"):
        line += " Click \u266a in the menu bar to stop."
    elif r.get("stoppable"):
        line += " Say \u201cstop the music\u201d, or use the stop button in Collie."
    return line


def _relay_qr_page(link, room, code, ttl=0):
    """The pairing screen when Collie Remote is on: a plain QR of the relay link.

    Deliberately a standard QR rather than collie's own ring code. The ring can only be read by
    collie, which is fine once the app is installed and useless before — a phone camera pointed at
    it reports nothing, and the person has no way to tell whether the code is broken or they are.
    A URL in a normal QR is read by every camera, and the app reads the same URL, so one symbol
    serves someone who has collie and someone who does not.
    """
    from . import qr
    svg = qr.svg(link, quiet=2, scale=6, dark="#0F0E19").decode("utf-8")
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair a phone — Collie</title>
<style>
 :root{color-scheme:light dark;--bg:#f5f7fd;--ink:#141a2e;--mut:#5a638a;--card:#ffffff;
       --line:rgba(40,55,110,.14)}
 @media (prefers-color-scheme:dark){:root{--bg:#0b0e18;--ink:#eef1ff;--mut:#98a1c8;
       --card:#141a2b;--line:rgba(255,255,255,.12)}}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
      justify-content:center;gap:18px;background:var(--bg);color:var(--ink);
      font-family:system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif;padding:32px 20px}
 h1{margin:0;font-size:21px;font-weight:650;letter-spacing:-.01em}
 p{margin:0;color:var(--mut);font-size:14.5px;line-height:1.6;max-width:34rem;text-align:center}
 /* ALWAYS light, never var(--card): a camera needs dark modules on a light quiet zone. Following
    the theme here painted a near-black symbol on a near-black card in dark mode — the page looked
    fine and simply could not be scanned. */
 .card{background:#fff;border:1px solid var(--line);border-radius:22px;padding:26px;
       box-shadow:0 18px 50px rgba(20,30,70,.10);display:grid;place-items:center}
 .card svg{display:block;width:min(62vw,300px);height:auto}
 code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px;
      color:var(--mut);word-break:break-all;text-align:center;max-width:34rem}
 .note{font-size:12.5px;color:var(--mut)}
 .note b{color:var(--ink);font-weight:600}
 .ask{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:22px 26px;
      display:grid;gap:10px;place-items:center;box-shadow:0 18px 50px rgba(20,30,70,.14)}
 .ask[hidden]{display:none}
 .who{font-size:15.5px;font-weight:600}
 .num{font-size:40px;font-weight:700;letter-spacing:.14em;font-variant-numeric:tabular-nums}
 .row{display:flex;gap:10px;margin-top:4px}
 .row button{font:inherit;font-size:14.5px;font-weight:600;border-radius:11px;padding:9px 20px;
             border:1px solid var(--line);cursor:pointer}
 .yes{background:#12a150;border-color:#12a150;color:#fff}
 .no{background:transparent;color:var(--mut)}
</style></head><body>
<h1>Point your phone camera here</h1>
<p>Any camera works — you do not need the app first. Scanning opens Collie on your phone
   and pairs it with this computer.</p>
<div class="card" id="card">%(svg)s</div>
<code id="link">%(link)s</code>
<p class="note">The code is <b>one-shot</b> and expires after <b>%(ttl)s seconds</b>; this page keeps
   showing a live one. Room <b>%(room)s</b>.</p>

<div class="ask" id="ask" hidden>
  <div class="who" id="who"></div>
  <div class="num" id="num"></div>
  <p>Check this number matches the one on the phone, then let it in.</p>
  <div class="row">
    <button class="no"  id="deny">Not me</button>
    <button class="yes" id="allow">Allow</button>
  </div>
</div>
<script>
// The approval prompt belongs HERE, not only in the control panel: whoever just scanned is looking
// at this page. Polling rather than a socket because the page is trivial and the window is short.
(function(){
  var tok = new URLSearchParams(location.search).get("token") || "";
  var q = function(p){ return p + (tok ? "?token=" + encodeURIComponent(tok) : ""); };
  var ask = document.getElementById("ask"), cur = null;
  function show(p){
    cur = p;
    document.getElementById("who").textContent = (p.name || "A device") + " wants to pair";
    document.getElementById("num").textContent = p.num || "";
    ask.hidden = false;
  }
  function hide(){ cur = null; ask.hidden = true; }
  function decide(yes){
    if (!cur) return;
    fetch(q("/api/remote/" + (yes ? "approve" : "deny")), {method:"POST"})
      .then(function(){ hide(); }).catch(hide);
  }
  document.getElementById("allow").onclick = function(){ decide(true); };
  document.getElementById("deny").onclick  = function(){ decide(false); };
  setInterval(function(){
    fetch(q("/api/remote/pending")).then(function(r){ return r.json(); }).then(function(j){
      if (j && j.pending) { if (!cur || cur.num !== j.pending.num) show(j.pending); }
      else if (cur) hide();
    }).catch(function(){});
  }, 1200);

  // The code expires, so a page left open would otherwise be showing a symbol that no longer pairs
  // anything — and the phone would report a failure that looks like the feature is broken.
  var code = %(code)s;
  setInterval(function(){
    fetch(q("/api/remote/status")).then(function(r){ return r.json(); }).then(function(j){
      if (!j || !j.paircode || j.paircode === code) return;
      code = j.paircode;
      document.getElementById("link").textContent = j.link || "";
      fetch(q("/api/remote/qr.svg")).then(function(r){ return r.text(); })
        .then(function(s){ document.getElementById("card").innerHTML = s; }).catch(function(){});
    }).catch(function(){});
  }, 3000);
})();
</script>
</body></html>""" % {"svg": svg, "link": _esc(link), "room": _esc(room),
                     "ttl": ttl or 180, "code": json.dumps(code or "")}


def _pair_advertised_host():
    """The address the phone should dial: this machine's LAN IP under --lan, else loopback."""
    for host in sorted(LAN_HOSTS):
        return host
    return "127.0.0.1"
# Non-secret per-process id. Injected into served HTML and returned by /api/ver so a long-lived
# desktop/wallpaper page can detect a server restart and auto-reload itself (picking up the fresh
# token + latest front-end/behaviour). Safe to expose: it's not a credential.
BOOT = os.urandom(8).hex()

# Set by `collie web --remote` (cli._cmd_web_remote) to a harness.remote.RemoteState. Powers the
# desktop control panel at /remote and the local-only /api/remote/* routes. None in plain `collie web`
# until the panel's "开启远程" toggle lazily creates one via _ensure_remote().
REMOTE = None

# WHICH DOG this server speaks for. `collie web --name Rowan`.
#
# The pack made a machine the wrong unit. One laptop can run several dogs — that is what the kennel
# is for, and they work in different repositories — so a phone that has paired with "your Mac" cannot
# say which of them it is about to task. Slack solved this with one app per dog; here it is one
# server per dog, and this is the name it answers to.
DOG_NAME = ""


def whoami() -> dict:
    """Who is on the other end of this connection — for a phone that has paired with several.

    Deliberately does NOT report an autonomy. Autonomy is enforced by `collie slack` when it spawns
    a run (AUTONOMY_MODE -> the gate's mode); this server spawns runs on its own terms and would be
    stating a limit it does not keep. A sentence in a greeting that nothing enforces is the exact
    defect that was just taken out of the Slack side, and it is not worth re-introducing here for
    the sake of a fuller-looking payload.
    """
    from . import settings, slackbot
    name = DOG_NAME
    source = "explicit" if name else ""
    if not name:
        name = settings.get("COMPANION_NAME", "") or ""
        if name:
            source = "environment" if settings.pinned("COMPANION_NAME") else "settings"
    if not name:
        # Unnamed is a real answer, not a guess. With one dog in the kennel the choice is obvious;
        # with several, picking one would be indistinguishable from picking the wrong one, and the
        # phone can fall back to the machine label — which is what it shows today.
        try:
            dogs = list(slackbot.load_kennel())
            name = dogs[0] if len(dogs) == 1 else ""
            if name:
                source = "kennel"
        except Exception:
            name = ""
    if not source:
        source = "default"
    # The URL changes with the effective name, while the endpoint itself also refuses durable
    # caching. Both matter: already-open surfaces can poll whoami and swap the dog immediately,
    # while a browser can never reuse the old coat after a rename.
    avatar_key = hashlib.sha256(("collie-ui-avatar-v1\0" + (name or "Collie").strip().lower())
                                .encode("utf-8")).hexdigest()[:12]
    from . import __version__ as ver
    return {"name": name, "machine": slackbot.machine_label(), "os": slackbot.os_label(),
            "fingerprint": slackbot.fingerprint(), "repo": os.getcwd(), "version": ver,
            "name_source": source,
            "name_editable": source not in ("explicit", "environment"),
            "avatar": "/api/avatar.png?v=" + avatar_key}


def _ensure_remote(port):
    """Lazily build the RemoteState so ANY `collie web` (incl. the desktop app) can turn remote on
    from the /remote panel — no `--remote` flag, no separate process, no second port. The relay URL
    comes from $COLLIE_RELAY (default wss://collie.run)."""
    global REMOTE
    if REMOTE is None:
        from .remote import RemoteState
        relay = os.environ.get("COLLIE_RELAY", "wss://collie.run")
        REMOTE = RemoteState(relay, port, TOKEN)
    return REMOTE


def _provider() -> str:
    """The configured provider, or "" when nothing is configured.

    Deliberately NOT "mock". mock answers from canned fixtures, and a fixture is indistinguishable
    from a model that has gone wrong — so a default that conjures one turns every momentary "the
    setting did not arrive" into confident nonsense. It did: see the settings._load() latch this
    shipped alongside. settings.apply() runs per query and lands a saved Provider in the env; if
    that ever comes back empty the honest move is to say so, not to answer anyway. Callers that
    RUN a model refuse on "" (_serve_stream); callers that merely display it show it as unset.
    mock stays reachable — by NAME only: COLLIE_PROVIDER=mock, or PROVIDER=mock in the panel."""
    return os.environ.get("COLLIE_PROVIDER", "")


def _perm(item) -> dict:
    """One parked approval, as the browser needs it.

    `body` is the argument preview the loop produced from PRE-redaction arguments — the
    gate runs before `_redact.restore`, so a secret the model only ever saw as a
    placeholder stays a placeholder on its way to the screen. Nothing here goes looking
    for the real value to make a nicer card.

    `rule_offer` is empty for calls that cannot carry a standing rule, and the UI must
    hide "always" when it is — an "always" button that silently means "just this once"
    lies to the person clicking it.
    """
    return {"id": item.id, "tool": item.tool, "body": item.body, "title": item.title,
            "target": item.target, "risk": item.risk, "rule_offer": item.rule_offer,
            "state": item.state}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 (the default) closes the connection when the handler returns, which is exactly
    # what we want for SSE: after the `done` event we return and the socket closes cleanly. The
    # client listens for `done` and calls es.close(), so there is no reconnect storm.
    server_version = "collie-web/0.1"

    # ------------------------------------------------------------------ live event bus
    # A run streams its events to the client that started it (via _serve_stream). We ALSO fan the
    # structural events (start/tool/edit/repro/done — never the token firehose) out to a process-wide
    # bus so OTHER open pages — the Map and the main page's mini-map — render Collie's work in real
    # time. Subscribers are plain queues; a run publishes, each /api/live connection drains its queue.
    _live_lock = threading.Lock()
    _live_subs: list = []

    # session-scoped MIRROR bus. _live (above) carries structural events for ALL runs (the Map);
    # this carries the FULL stream — INCLUDING tokens — for ONE session, so a second window (a phone
    # + the desktop) can mirror a run token-by-token in real time. sid -> list[queue].
    _mirror_lock = threading.Lock()
    _mirror_subs: dict = {}
    # What a late subscriber missed. sid -> [(kind, data)], structural events only, dropped when the
    # run ends because from that moment the saved thread is the better record.
    _mirror_backlog: dict = {}
    _MIRROR_BACKLOG = 60

    # attached-image store: the composer POSTs an image to /api/upload, gets an id back, then the
    # next /api/stream?imgs=<id> references it. Kept in memory (a run consumes it right away), bounded
    # so a burst of screenshots can't grow unbounded.
    _img_lock = threading.Lock()
    _imgs: dict = {}
    _img_order: list = []

    # mid-run steering: while a run streams, the user can send more text (Claude-Code style). It's
    # queued here per session; the run's loop drains it at the next turn boundary (loop._drain_steering)
    # and injects it as a user message. Bounded so a jammed run can't grow the queue without limit.
    _steer_lock = threading.Lock()
    _steer_runs: dict = {}          # sid -> queue.Queue[str], present only while a run is in flight

    # What is running, across every window and none.
    #
    # A run outlives the socket that started it, so "is anything happening?" cannot be answered from
    # the page that asked — and a second thread cannot be started with any confidence while the first
    # one's state lives only in one tab's `running` variable. This is the process's own answer, and
    # the only thing that makes more than one conversation at a time honest rather than hopeful.
    # sid -> {state, started, ask, cwd, turns, verified, error, ended}
    _runs_lock = threading.Lock()
    _runs: dict = {}
    _cancel_events: dict = {}       # sid -> (run id, Event), never exposed in JSON snapshots
    _RUNS_KEEP = 30                 # finished runs stay listable so the list can show a verdict

    @classmethod
    def _run_begin(cls, sid, ask, cwd):
        with cls._runs_lock:
            current = cls._runs.get(sid)
            if current is not None and current.get("ended") is None:
                return None
            run_id = os.urandom(8).hex()
            cancel = threading.Event()
            cls._runs[sid] = {"session": sid, "state": "running", "started": time.time(),
                              "ask": (ask or "")[:120], "cwd": cwd, "turns": 0,
                              "verified": None, "error": "", "ended": None,
                              "run": run_id, "cancel_requested": None}
            cls._cancel_events[sid] = (run_id, cancel)
            return run_id

    @classmethod
    def _run_mark(cls, sid, **kw):
        with cls._runs_lock:
            r = cls._runs.get(sid)
            if r is not None:
                r.update(kw)

    @classmethod
    def _run_end(cls, sid, res=None, error="", canceled=False, run_id=None):
        with cls._runs_lock:
            r = cls._runs.get(sid)
            # First verdict wins. The success path records the real one and the `finally` guard runs
            # straight after it; without this the guard would overwrite every good result with its
            # own catch-all and every finished run would read as failed.
            if r is None or r["ended"] or (run_id is not None and r.get("run") != run_id):
                return
            entry = cls._cancel_events.get(sid)
            was_canceled = bool(canceled or getattr(res, "canceled", False)
                                or (entry and entry[0] == r.get("run") and entry[1].is_set()))
            r["state"] = ("canceled" if was_canceled else
                          ("failed" if (error or getattr(res, "error", "")) else "done"))
            r["error"] = ("canceled by user" if was_canceled else
                          (error or getattr(res, "error", "") or "")[:200])
            r["ended"] = time.time()
            if res is not None:
                r["turns"] = getattr(res, "turns", 0) or 0
                r["verified"] = False if was_canceled else bool(getattr(res, "verified", False))
            # keep the most recent finished runs, drop the rest — a verdict nobody looked at within
            # thirty runs is not one the sidebar should still be offering
            done = sorted([x for x in cls._runs.values() if x["ended"]], key=lambda x: x["ended"])
            for old in done[:-cls._RUNS_KEEP]:
                cls._runs.pop(old["session"], None)
                cls._cancel_events.pop(old["session"], None)

    @classmethod
    def _run_cancel(cls, sid, run_id=None):
        with cls._runs_lock:
            r = cls._runs.get(sid)
            if r is None:
                return {"ok": False, "status": "not_running", "session": sid}
            if run_id and run_id != r.get("run"):
                return {"ok": False, "status": "run_mismatch", "session": sid,
                        "run": r.get("run")}
            if r.get("ended") is not None:
                status = "already_canceled" if r.get("state") == "canceled" else "not_running"
                return {"ok": status == "already_canceled", "status": status,
                        "session": sid, "run": r.get("run")}
            entry = cls._cancel_events.get(sid)
            if entry is None or entry[0] != r.get("run"):
                return {"ok": False, "status": "not_running", "session": sid}
            already = entry[1].is_set()
            entry[1].set()
            r["state"] = "canceling"
            r["cancel_requested"] = r.get("cancel_requested") or time.time()
            out = {"ok": True, "status": "already_requested" if already else "cancel_requested",
                   "session": sid, "run": r.get("run")}
        # An approval can be parked indefinitely. Orphaning it wakes that waiter; the loop then
        # observes the cancel flag before executing the next tool.
        cls._inbox_close(sid)
        return out

    @classmethod
    def _run_cancelled(cls, sid, run_id):
        with cls._runs_lock:
            entry = cls._cancel_events.get(sid)
            return bool(entry and entry[0] == run_id and entry[1].is_set())

    @classmethod
    def _runs_snapshot(cls):
        with cls._runs_lock:
            return sorted((dict(r) for r in cls._runs.values()),
                          key=lambda r: (r["state"] not in ("running", "canceling"),
                                         -(r["ended"] or r["started"])))

    @classmethod
    def _steer_open(cls, sid):
        q: queue.Queue = queue.Queue(maxsize=64)
        with cls._steer_lock:
            cls._steer_runs[sid] = q
        return q

    @classmethod
    def _steer_close(cls, sid):
        with cls._steer_lock:
            cls._steer_runs.pop(sid, None)

    @classmethod
    def _steer_push(cls, sid, text):
        """Queue a steer for an in-flight run. False if the session has no active run (already done)."""
        with cls._steer_lock:
            q = cls._steer_runs.get(sid)
        if q is None:
            return False
        try:
            q.put_nowait(text)
            return True
        except queue.Full:
            return False

    # -- approvals: the session's Inbox, present only while a run is in flight ----
    _inbox_lock = threading.Lock()
    _inbox_runs: dict = {}

    @classmethod
    def _inbox_open(cls, sid, store):
        with cls._inbox_lock:
            old = cls._inbox_runs.get(sid)
            cls._inbox_runs[sid] = store
        if old is not None and old is not store:
            # A previous run for this session left questions nobody can act on any more.
            try:
                old.resolve_session(sid)
                old.close()
            except Exception:
                pass

    @classmethod
    def _inbox_close(cls, sid):
        with cls._inbox_lock:
            store = cls._inbox_runs.pop(sid, None)
        if store is not None:
            try:
                # An approval whose run has ended can never be meaningfully granted; leaving
                # it pending would show a decision that no longer does anything.
                store.resolve_session(sid)
                store.close()
            except Exception:
                pass

    @classmethod
    def _inbox_answer(cls, sid, item_id, resolution):
        """Answer a parked question. False when there is no live run, the item is unknown,
        or somebody else got there first — the loser is told nothing happened."""
        with cls._inbox_lock:
            store = cls._inbox_runs.get(sid)
        if store is None:
            return False
        resolved = bool(store.resolve(item_id, resolution))
        if resolved:
            event = {"id": item_id, "answer": resolution}
            cls._mirror_pub(sid, "permission_resolved", event)
            cls._live_pub("permission_resolved", dict(event, session=sid))
        return resolved

    @classmethod
    def _inbox_pending(cls, sid):
        with cls._inbox_lock:
            store = cls._inbox_runs.get(sid)
        return [_perm(i) for i in store.pending(sid)] if store is not None else []

    @classmethod
    def _inbox_pending_all(cls):
        """Snapshot every live run's still-actionable approval.

        ``/api/live`` is intentionally a live bus, not a durable queue.  A page opened after an
        approval was published therefore needs this read before its global Needs You indicator can
        be truthful.  Keep the snapshot tied to the in-memory run stores: ``_inbox_close`` removes
        the store and orphans its questions as soon as the run can no longer act on an answer.
        """
        with cls._inbox_lock:
            stores = list(cls._inbox_runs.items())
        out = []
        for sid, store in stores:
            try:
                out.extend(dict(_perm(item), session=sid) for item in store.pending(sid))
            except Exception:
                # One closing run must not hide the other runs' decisions.  A following refresh
                # will observe the stable post-close snapshot.
                continue
        return out

    @classmethod
    def _notify_waiting(cls, sid, item):
        """Push the question to a paired phone.

        Run-finished notices are rate-limited by NOTIFY_AFTER_MS, because a run that took
        two seconds does not need to buzz anybody. This one is not throttled and never
        should be: the run is STOPPED until it is answered, so the notification is the
        only thing that will ever restart it. A silent parked approval is a run that
        appears to have hung.
        """
        if REMOTE is None:
            return
        try:
            REMOTE.notify("Collie needs your approval",
                          ("%s — %s" % (item.tool, item.body))[:180],
                          session=sid, thread=sid)
        except Exception:
            pass                      # never fail a run over a notification

    @classmethod
    def _img_put(cls, media_type, data):
        iid = os.urandom(8).hex()
        with cls._img_lock:
            cls._imgs[iid] = (media_type, data)
            cls._img_order.append(iid)
            while len(cls._img_order) > 24:
                cls._imgs.pop(cls._img_order.pop(0), None)
        return iid

    @classmethod
    def _img_get(cls, iid):
        with cls._img_lock:
            return cls._imgs.get(iid)

    @classmethod
    def _live_pub(cls, kind, data):
        with cls._live_lock:
            subs = list(cls._live_subs)
        for q in subs:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                pass          # a stalled listener drops frames rather than blocking the run

    # A run that outlives the person's attention is the whole reason the phone exists. Short runs are
    # not worth a buzz — you are still looking at the screen — so this only fires past a threshold, or
    # when the run failed, which is worth knowing however fast it happened.
    # Configurable because the right answer differs per person: 0 notifies for every run, a very
    # large number for none but failures.
    try:
        NOTIFY_AFTER_MS = int(os.environ.get("COLLIE_NOTIFY_AFTER_MS") or 45_000)
    except ValueError:
        NOTIFY_AFTER_MS = 45_000

    @staticmethod
    def _record_command(sid, said, answer):
        """Write a fast-path command into the conversation it was typed in.

        The intent router is an optimisation — instant and free where a model call is neither — but
        it is not a different place for things to happen. A chat that cannot show you the thing you
        just asked for is one you stop believing.
        """
        said = (said or "").strip()
        if not said or not answer:
            return None
        try:
            from . import sessions            # imported per-use here, as everywhere else in this file
            # No session yet means this command is the first thing said in a new chat. Start one, and
            # hand the id back so the client continues in it — otherwise the very first thing a
            # person does is the one thing the history cannot show them.
            sid = str(sid or "").strip() or sessions.new_id()
            sessions.append_exchange(sid, said, answer, cwd=os.getcwd())
            return sid
        except Exception:
            return None                 # the command already happened; logging it is not worth failing

    @staticmethod
    def _notify_done(sid, res, wall_ms=None):
        if REMOTE is None:
            return
        failed = bool(getattr(res, "error", None))
        if not failed and (wall_ms or 0) < Handler.NOTIFY_AFTER_MS:
            return
        answer = (getattr(res, "answer", "") or "").strip().replace("\n", " ")
        try:
            REMOTE.notify(
                "Run failed" if failed else "Run finished",
                (getattr(res, "error", "") or "")[:200] if failed
                else (answer[:180] or "No answer text."),
                session=sid, thread=sid)
        except Exception:
            pass                      # a notification is never worth failing a finished run over

    def _serve_live(self):
        """GET /api/live -> an SSE feed of every run's structural events, for live map rendering."""
        q: queue.Queue = queue.Queue(maxsize=256)
        with Handler._live_lock:
            Handler._live_subs.append(q)
        self._sse_open()
        try:
            self._sse("live_hello", {"cwd": os.getcwd()})
            while True:
                try:
                    kind, data = q.get(timeout=15)
                    self._sse(kind, data)
                except queue.Empty:
                    self._sse("ping", {})     # keep-alive so proxies/browsers hold the connection
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            with Handler._live_lock:
                try:
                    Handler._live_subs.remove(q)
                except ValueError:
                    pass

    @classmethod
    def _mirror_pub(cls, sid, kind, data):
        """Fan one event of session `sid`'s run to every window mirroring that session."""
        with cls._mirror_lock:
            subs = list(cls._mirror_subs.get(sid, ()))
            # Keep a short tail so a window that joins mid-run is not shown a blank screen under a
            # note saying work is happening. Structural events only: the token firehose would blow
            # the buffer in one paragraph, and what a late joiner needs is what Collie DID, not the
            # prose it was in the middle of.
            if kind != "token":
                buf = cls._mirror_backlog.setdefault(sid, [])
                buf.append((kind, data))
                if len(buf) > cls._MIRROR_BACKLOG:
                    del buf[:-cls._MIRROR_BACKLOG]
            if kind == "done":
                cls._mirror_backlog.pop(sid, None)   # the thread on disk is the record now
        for q in subs:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                pass

    def _serve_mirror(self, sid):
        """GET /api/mirror?session=<sid> -> SSE feed of that session's live run (tokens + structural),
        so another open window mirrors it. The window that STARTED the run renders from its own
        /api/stream; every other window renders from here."""
        if not sid:
            return self._send_json({"error": "session required"}, 400)
        q: queue.Queue = queue.Queue(maxsize=1024)
        with Handler._mirror_lock:
            Handler._mirror_subs.setdefault(sid, []).append(q)
        self._sse_open()
        try:
            self._sse("mirror_hello", {"session": sid})
            # Hand over what already happened before this window arrived, marked as a replay so the
            # page can render it as history rather than as things happening right now.
            pending_ids = {str(item.get("id") or "") for item in Handler._inbox_pending(sid)}
            with Handler._mirror_lock:
                past = [event for event in Handler._mirror_backlog.get(sid, ())
                        if event[0] != "permission" or
                        str((event[1] or {}).get("id") or "") in pending_ids]
            if past:
                self._sse("mirror_replay", {"session": sid, "count": len(past)})
                for kind, data in past:
                    self._sse(kind, data)
                self._sse("mirror_live", {"session": sid})
            while True:
                try:
                    kind, data = q.get(timeout=15)
                    self._sse(kind, data)
                except queue.Empty:
                    self._sse("ping", {})
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            with Handler._mirror_lock:
                subs = Handler._mirror_subs.get(sid)
                if subs is not None:
                    try:
                        subs.remove(q)
                    except ValueError:
                        pass
                    if not subs:
                        Handler._mirror_subs.pop(sid, None)

    # ------------------------------------------------------------------ helpers
    def end_headers(self):
        """Security defaults for every response, including errors, JSON, media, and SSE."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy",
                         "camera=(self), microphone=(self), display-capture=(self), "
                         "geolocation=(), payment=(), usb=(), browsing-topics=()")
        # Ambient intentionally embeds the same-origin wallpaper/map.  The sole exception is an
        # authenticated index response requested by Collie's VS Code webview; XFO cannot express
        # that narrow scheme/host allowlist, so CSP frame-ancestors owns that one response.
        if not getattr(self, "_vscode_embed", False):
            self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    @staticmethod
    def _html_csp(body: bytes, vscode_embed: bool = False) -> str:
        """Authorize this exact document's inline scripts without allowing arbitrary inline JS."""
        scripts = re.findall(
            br"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", body,
            flags=re.IGNORECASE | re.DOTALL)
        hashes = []
        for script in scripts:
            # The HTML tokenizer normalizes CRLF/CR to LF before CSP checks the inline text.
            # Hash that parsed representation, not the checkout's platform line endings.
            normalized = script.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            digest = base64.b64encode(hashlib.sha256(normalized).digest()).decode("ascii")
            value = "'sha256-%s'" % digest
            if value not in hashes:
                hashes.append(value)
        script_src = "script-src 'self'" + ((" " + " ".join(hashes)) if hashes else "")
        frame_ancestors = ("vscode-webview: https://*.vscode-cdn.net"
                           if vscode_embed else "'self'")
        return "; ".join((
            "default-src 'self'",
            script_src,
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "media-src 'self' data: blob:",
            "connect-src 'self' https://ipapi.co https://api.open-meteo.com",
            "frame-src 'self'",
            "frame-ancestors " + frame_ancestors,
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ))

    def _send_html(self, body: bytes, code: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if ctype.lower().startswith("text/html"):
            self.send_header("Content-Security-Policy", self._html_csp(
                body, vscode_embed=bool(getattr(self, "_vscode_embed", False))))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send_html(body, code, "application/json; charset=utf-8")

    def _read_json(self, maxlen: int = 8192):
        """Read + parse a JSON POST body, or None on any problem (missing/oversize/parse)."""
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > maxlen:
            return None
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        # Connection: close (NOT keep-alive) — this is a one-shot stream. Without a Content-Length or
        # chunked terminator, a keep-alive SSE connection never signals end-of-response, so after the
        # `done` frame it LINGERS open until something (a proxy, the browser) drops it — which the
        # client sees as EventSource.onerror -> "stream interrupted". Closing after the handler returns
        # gives a clean EOF right after `done`. close_connection makes BaseHTTPRequestHandler honor it.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")   # defeat any reverse-proxy buffering
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, data) -> None:
        """Write one SSE frame and flush it onto the wire immediately. When a heartbeat thread is
        active (_serve_stream), a per-request lock serializes writes so a ping can't interleave
        mid-frame with the run's token/tool events and corrupt the stream."""
        payload = "event: %s\ndata: %s\n\n" % (
            event, json.dumps(data, ensure_ascii=False, default=str))
        buf = payload.encode("utf-8")
        lock = getattr(self, "_wlock", None)
        if lock is not None:
            with lock:
                self.wfile.write(buf)
                self.wfile.flush()
        else:
            self.wfile.write(buf)
            self.wfile.flush()

    def log_message(self, fmt, *args):   # keep the terminal quiet; SSE is the real feedback
        pass

    # ------------------------------------------------------------------ routing
    def do_GET(self):
        # BaseHTTPRequestHandler may reuse one handler for multiple HTTP/1.1 requests.  Never let
        # an authenticated embed response relax headers on a later request over that connection.
        self._vscode_embed = False
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, 403)
        if not self._peer_ok(parsed):
            return self._send_json({"error": "pairing required"}, 403)
        try:
            if path in ("/", "/index.html"):
                self._vscode_embed = self._vscode_embed_ok(parsed)
                return self._serve_index()
            if path == "/pair":
                return self._serve_pair_page()
            if path in ("/logo.svg", "/favicon.ico", "/favicon.svg"):
                return self._serve_logo()
            if path == "/map":
                return self._serve_static("map.html", "text/html; charset=utf-8")
            if path == "/wallpaper":
                return self._serve_static("wallpaper.html", "text/html; charset=utf-8")
            if path == "/ambient":
                return self._serve_static("ambient.html", "text/html; charset=utf-8")
            if path == "/remote":
                return self._serve_static("remote.html", "text/html; charset=utf-8")
            if path == "/m":                          # mobile client (served to phones via the relay)
                return self._serve_static("mobile.html", "text/html; charset=utf-8")
            if path == "/map/three.min.js":
                return self._serve_static("three.min.js", "application/javascript; charset=utf-8")
            if path == "/api/ver":
                # non-secret per-process id; a long-lived desktop page polls this and reloads when it changes
                return self._send_html(BOOT.encode(), 200, "text/plain; charset=utf-8")
            if path == "/api/whoami":
                # Behind the same pairing gate as everything else: which dog this is, and which
                # repository it is standing in, is not public.
                return self._send_json(whoami())
            if path == "/api/avatar.png":
                return self._serve_avatar(parsed)
            if path == "/api/tree":
                return self._serve_tree(urllib.parse.parse_qs(parsed.query))
            if path == "/api/repos":
                return self._serve_repos()
            if path == "/api/session_map":
                return self._serve_session_map(urllib.parse.parse_qs(parsed.query))
            if path == "/api/file":
                return self._serve_file(urllib.parse.parse_qs(parsed.query))
            if path == "/api/live":
                return self._serve_live()
            if path == "/api/sessions":
                return self._serve_sessions(urllib.parse.parse_qs(parsed.query))
            if path == "/api/checkpoints":
                return self._serve_checkpoints()
            if path == "/api/worktrees":
                # Isolation nobody can see becomes disk nobody reclaims. Listing them with what each
                # holds is what makes "clean up" a decision rather than a gamble.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from . import worktree as _wt
                return self._send_json({"worktrees": _wt.listing(os.getcwd())})
            if path == "/api/runs":
                # What is running right now, and how the last few finished. The page cannot know this
                # on its own: a run outlives the socket that started it and may have been started from
                # another window entirely (the phone, a second tab).
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                return self._send_json({"runs": Handler._runs_snapshot()})
            if path == "/api/approvals":
                # The live bus only carries new events.  This authenticated snapshot restores the
                # cross-session Needs You queue after a refresh or an SSE reconnect.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                return self._send_json({"approvals": Handler._inbox_pending_all()})
            if path == "/api/library":
                # Installed extension metadata is operational state: keep it behind the same
                # process token as Activity and Settings.  ExtensionStore already returns an
                # allowlisted view (digest/trust/scopes/component counts, never package paths).
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from .extensions import ExtensionError, ExtensionStore
                try:
                    return self._send_json({"extensions": ExtensionStore(_state_root()).list()})
                except ExtensionError as exc:
                    return self._send_json({"error": str(exc)}, 409)
            if path in ("/api/activity", "/api/healthz", "/api/recovery", "/api/hooks") or \
                    path.startswith("/api/recovery/"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                if path == "/api/activity":
                    from .controlplane import activity
                    return self._send_json(_public_activity(activity(_state_root(), limit=250)))
                if path == "/api/healthz":
                    from .controlplane import health
                    return self._send_json(_public_health(health(_state_root())))
                if path == "/api/hooks":
                    from .hooks import HookManager
                    manager = HookManager(os.getcwd())
                    return self._send_json({"active": manager.active, "events": manager.events(),
                                            "pending": [{"path": str(x.get("path") or ""),
                                                         "sha256": str(x.get("sha256") or "")}
                                                        for x in manager.pending],
                                            "trust_changes_allowed": False})
                from . import sessions
                session_dir = os.path.join(_state_root(), "sessions")
                if path == "/api/recovery":
                    return self._send_json({"sessions": [
                        _public_recovery(row) for row in
                        sessions.active_runs(limit=250, directory=session_dir)]})
                sid = urllib.parse.unquote(path[len("/api/recovery/"):]).strip()
                if not sid:
                    return self._send_json({"error": "session required"}, 400)
                state = sessions.recovery_state(sid, directory=session_dir)
                if state is None:
                    return self._send_json({"error": "no active recovery state"}, 404)
                return self._send_json(_public_recovery(state, sid))
            if path in ("/api/mission/run-tree", "/api/mission/specialist"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                query = urllib.parse.parse_qs(parsed.query)
                from .missionweb import MissionService
                svc = MissionService()
                try:
                    if path == "/api/mission/run-tree":
                        mid = str(query.get("id", [""])[0] or "").strip()
                        if not mid:
                            return self._send_json({"error": "id required"}, 400)
                        value = svc.inspect_run_tree(mid)
                        if isinstance(value.get("tree"), dict):
                            value = dict(value); value["tree"] = _public_tree(value["tree"])
                        value.pop("path", None)
                    else:
                        run_id = str(query.get("run_id", [""])[0] or "").strip()
                        if not run_id:
                            return self._send_json({"error": "run_id required"}, 400)
                        value = _public_specialist(svc.inspect_specialist(run_id))
                    return self._send_json(value, 404 if value.get("error") else 200)
                finally:
                    svc.close()
            if path in ("/api/plan", "/api/review"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                sid = (urllib.parse.parse_qs(parsed.query).get("session", [""])[0] or "").strip()
                if not sid:
                    return self._send_json({"error": "session required"}, 400)
                if path == "/api/plan":
                    from .plantool import PlanArtifactStore
                    artifact = PlanArtifactStore().get(_web_plan_scope(sid))
                    return self._send_json({"session": sid, "artifact": artifact,
                                            "can_approve": not artifact.get("approved")})
                return self._send_json(_latest_review_artifact(sid))
            if path == "/api/checkpoints":
                return self._serve_checkpoints()
            if path == "/api/settings":
                from . import settings
                vals = settings.all_values()
                # Make the ambient-desktop toggle tell the TRUTH: it's ON iff the logon autostart file
                # actually exists (the main installer never creates it — onboarding or this toggle do),
                # so the switch can never disagree with what's really running. Windows-only feature.
                try:
                    from . import plat
                    if plat.is_windows():
                        from . import wallpaper as _wp
                        vals["WALLPAPER"] = "on" if os.path.exists(_wp._startup_vbs()) else "off"
                except Exception:
                    pass
                return self._send_json({"schema": settings.SCHEMA, "values": vals,
                                        "identity": whoami()})
            if path == "/api/work-identities":
                from .workidentity import public_connections
                return self._send_json({"connections": public_connections(_state_root())})
            if path == "/api/run-capabilities":
                # Static provider/model truth for the run setup.  In particular the
                # UI hides Fast unless this exact pair has a known same-model wire
                # contract and billing premium.
                from . import settings
                from .providers import provider_capabilities
                settings.apply()
                name = _provider()
                model = settings.get("MODEL", "") or None
                return self._send_json(provider_capabilities(name, model))
            if path == "/api/verification":
                from .verification import detect_verification_commands
                return self._send_json({"cwd": os.getcwd(),
                                        "candidates": detect_verification_commands(os.getcwd())})
            if path == "/api/mcp":
                # The MCP control plane: what is configured and what state it is really in. Read-only
                # and deliberately out-of-band — when a bad server is what is breaking collie, you
                # cannot ask collie to fix it, so seeing and switching them has to work without it.
                try:
                    from . import mcpclient
                    have = {x.get("name") for x in mcpclient.status()}
                    return self._send_json({"servers": mcpclient.status(),
                                            "config": mcpclient._CONFIG,
                                            "errors": dict(_MCP_LOGIN_ERR),
                                            # The services you can connect without knowing anything.
                                            # Adding one used to mean already having its URL, which
                                            # is a strange thing to demand of the screen whose job is
                                            # to tell you what exists.
                                            "catalog": [dict(v, name=k) for k, v in
                                                        mcpclient.CATALOG.items() if k not in have]})
                except Exception as e:
                    return self._send_json({"servers": [], "error": str(e)})
            if path == "/api/models":
                # The model-picker catalog: one flat list of runnable (provider, model) entries
                # with auth badge + price. ?discover=1 also queries each authed provider's model
                # endpoint (codex/openai/ollama/…) — slower, so it's opt-in from the UI.
                from . import catalog, settings
                q = urllib.parse.parse_qs(parsed.query)
                live = q.get("discover", ["0"])[0] in ("1", "true", "on")
                vals = settings.all_values()
                custom = {"base_url": os.environ.get("COLLIE_COMPAT_BASE", ""),
                          "model": vals.get("MODEL", "")} if vals.get("PROVIDER") == "openai-compat" else None
                entries = catalog.list_entries(discover_live=live, custom=custom)
                current = "%s:%s" % (vals.get("PROVIDER", ""), vals.get("MODEL", ""))
                # A COLLIE_PROVIDER/COLLIE_MODEL set before we started outranks anything the picker
                # writes. Say so here rather than letting every selection appear to be ignored.
                pin = [k for k in ("PROVIDER", "MODEL") if settings.pinned(k)]
                out = {"entries": entries, "current": current}
                if pin:
                    out["pinned"] = pin
                    out["pinned_note"] = (
                        "This collie was started with %s set in its environment, which outranks the "
                        "picker — choosing a model here will not change what runs. Restart collie "
                        "without it." % ", ".join("COLLIE_" + k for k in pin))
                return self._send_json(out)
            if path == "/api/browser/status":
                # onboarding "connect your browser": is the bridge up, has the extension connected,
                # where's the extension folder, and which Chromium browsers are installed.
                import shutil
                from . import browserbridge as bb
                ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_ext")
                health = {}
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/health" % bb._port(), timeout=1.5) as r:
                        health = json.loads(r.read())
                except Exception:
                    health = {}

                def _found(cmd, paths, mac=()):
                    # The Windows paths just miss elsewhere, and `which` does not rescue macOS
                    # either: browsers there are .app bundles and are never on PATH. So this
                    # answered "no browsers installed" on every Mac, however many were installed.
                    for p in tuple(paths) + (tuple(mac) if plat.is_macos() else ()):
                        if p and os.path.exists(os.path.expandvars(p)):
                            return True
                    return bool(shutil.which(cmd))
                browsers = []
                if _found("chrome", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                                     r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                                     r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
                          mac=["/Applications/Google Chrome.app"]):
                    browsers.append("Chrome")
                if _found("msedge", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                                     r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
                          mac=["/Applications/Microsoft Edge.app"]):
                    browsers.append("Edge")
                return self._send_json({"bridge_running": bool(health),
                                        "extension_connected": bool(health.get("extension_connected")),
                                        "ext_version": health.get("extension_version"),
                                        "ext_path": ext, "browsers": browsers})
            if path == "/api/record/status":
                from . import record as rec
                st = rec._load()
                on = bool(st and rec._alive(st.get("pid")))
                return self._send_json({"recording": on, "out": (st or {}).get("out"),
                                        "since": (st or {}).get("started"),
                                        "window": (st or {}).get("window")})
            if path == "/api/record/sources":
                # everything the record panel needs to populate its pickers
                from . import record as rec
                cams, mics = [], []
                try:
                    cams, mics = rec.list_capture_devices()
                except Exception:
                    pass
                mons = []
                try:
                    mons = [{"w": w, "h": h, "x": x, "y": y} for (x, y, w, h) in rec._monitors()]
                except Exception:
                    pass
                return self._send_json({"windows": rec.list_windows(), "cameras": cams,
                                        "microphones": mics, "monitors": mons})
            if path == "/api/record/list":
                from . import record as rec
                return self._send_json({"recordings": rec.list_recordings()})
            if path == "/api/desktop/config":
                from . import desktop as dt
                return self._send_json(dt.load_config())
            if path == "/api/desktop/sys":
                from . import desktop as dt
                return self._send_json(dt.sysinfo())
            if path == "/api/desktop/nowplaying":
                from . import desktop as dt
                # Two different questions, and conflating them would be wrong. `track` is whatever
                # the SYSTEM is playing (Spotify, Music — read-only, we can only send media keys).
                # `collie` is what THIS process started and can therefore actually stop, which is the
                # one a stop button may be offered for.
                mine = dt.playing_here().get("track")
                return self._send_json({
                    "track": dt.nowplaying(),
                    # `ok` so this object is the same shape /api/desktop/play returns — one type on
                    # the client for "what is playing", rather than two that differ by one field.
                    "collie": ({"ok": True, "title": mine.get("title"),
                                "uploader": mine.get("uploader"),
                                "duration": mine.get("duration"), "stoppable": True}
                               if mine else None)})
            if path == "/api/desktop/projects":
                from . import desktop as dt
                return self._send_json({"projects": dt.projects()})
            if path == "/api/desktop/resolve":
                from . import desktop as dt
                qs = urllib.parse.parse_qs(parsed.query)
                return self._send_json(dt.resolve((qs.get("q") or [""])[0]))
            if path == "/api/desktop/lyrics":
                from . import desktop as dt
                qs = urllib.parse.parse_qs(parsed.query)
                return self._send_json(dt.lyrics((qs.get("q") or [""])[0], (qs.get("a") or [""])[0],
                                                 (qs.get("d") or ["0"])[0], (qs.get("t") or [""])[0]))
            if path == "/api/desktop/resolve_audio":
                from . import desktop as dt
                import base64
                qs = urllib.parse.parse_qs(parsed.query)
                _excl = [x for x in ((qs.get("exclude") or [""])[0]).split(",") if x]
                info = dt.resolve_audio((qs.get("q") or [""])[0], (qs.get("artist") or [""])[0],
                                        (qs.get("title") or [""])[0], (qs.get("region") or [""])[0], _excl)
                if info.get("ok") and info.get("url"):
                    info["src"] = "/api/desktop/audio?u=" + urllib.parse.quote(
                        base64.urlsafe_b64encode(info["url"].encode()).decode())
                    info.pop("url", None)          # play through the same-origin proxy (enables the analyser)
                return self._send_json(info)
            if path == "/api/desktop/audio":
                # stream-proxy the (IP+time-locked) googlevideo audio so playback is same-origin
                # (Web Audio analyser works) and Range/seek is forwarded. Host-locked against SSRF.
                import base64
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    target = base64.urlsafe_b64decode((qs.get("u") or [""])[0]).decode("utf-8")
                except Exception:
                    return self._send_json({"error": "bad url"}, 400)
                if not _audio_host_ok(target):
                    return self._send_json({"error": "forbidden host"}, 403)
                hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                rng = self.headers.get("Range")
                if rng:
                    hdrs["Range"] = rng
                try:
                    # do NOT follow redirects — a 30x could send us to an unvalidated (internal) host
                    class _NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, *a, **k):
                            return None
                    up = urllib.request.build_opener(_NoRedirect).open(
                        urllib.request.Request(target, headers=hdrs), timeout=25)
                except Exception as e:
                    return self._send_json({"error": str(e)}, 502)
                try:
                    self.send_response(getattr(up, "status", 200) or 200)
                    for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                        v = up.headers.get(h)
                        if v:
                            self.send_header(h, v)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while True:
                        chunk = up.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                except Exception:
                    pass
                finally:
                    try:
                        up.close()
                    except Exception:
                        pass
                return
            if path == "/api/desktop/icon":
                from . import desktop as dt
                qs = urllib.parse.parse_qs(parsed.query)
                png = dt.icon_png((qs.get("path") or [""])[0])
                if not png:
                    return self._send_json({"error": "no icon"}, 404)
                try:
                    with open(png, "rb") as f:
                        return self._send_html(f.read(), 200, "image/png")
                except Exception:
                    return self._send_json({"error": "read"}, 404)
            if path.startswith("/api/delete/"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from . import sessions
                return self._send_json({"ok": sessions.delete(urllib.parse.unquote(path[len("/api/delete/"):]))})
            if path.startswith("/api/rename/"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from . import sessions
                sid = urllib.parse.unquote(path[len("/api/rename/"):])
                title = urllib.parse.parse_qs(parsed.query).get("title", [""])[0]
                return self._send_json({"ok": sessions.set_title(sid, title)})
            if path.startswith("/api/session/"):
                return self._serve_session(path[len("/api/session/"):])
            if path == "/api/mission":                    # delegate: one mission's live status
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from .missionweb import MissionService
                mid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                svc = MissionService()
                try:
                    if not mid:
                        return self._send_json({"error": "id required"}, 400)
                    out = svc.status(mid)
                    return self._send_json(out, 404 if out.get("error") else 200)
                finally:
                    svc.close()
            if path == "/api/missions":                   # delegate: the mission list (sidebar)
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from .missionweb import MissionService
                svc = MissionService()
                try:
                    return self._send_json({"missions": svc.missions()})
                finally:
                    svc.close()
            if path == "/api/stream":
                if not self._authed(parsed):
                    # SSE clients read errors as events; but headers aren't sent yet, so a plain
                    # 403 is fine and the drive-by never starts the agent.
                    return self._send_json({"error": "forbidden"}, 403)
                return self._serve_stream(urllib.parse.parse_qs(parsed.query))
            if path == "/api/mirror":                 # live token-by-token mirror of one session's run
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                return self._serve_mirror(urllib.parse.parse_qs(parsed.query).get("session", [""])[0].strip())
            if path == "/api/remote/qr.svg":
                # The pairing code expires, so the symbol on screen has to be able to catch up. The
                # page re-fetches this when the code rotates; rendering server-side means the page
                # needs no QR encoder of its own, and the symbol always matches the live link.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                link = REMOTE.link() if REMOTE else ""
                if not link:
                    return self._send_json({"error": "remote not available"}, 503)
                from . import qr as _qr
                svg = _qr.svg(link, quiet=2, scale=6, dark="#0F0E19")
                self.send_response(200)
                self.send_header("content-type", "image/svg+xml; charset=utf-8")
                self.send_header("cache-control", "no-store")
                self.send_header("content-length", str(len(svg)))
                self.end_headers()
                return self.wfile.write(svg)
            if path == "/api/remote/pending":        # a phone waiting on a human — GET, it's a read
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                # Polled by BOTH the pairing screen and the control panel: whoever just scanned is
                # looking at the pairing screen, not at a panel they would have to go and find.
                cl = REMOTE.client if REMOTE else None
                p = getattr(cl, "pending_pair", None) if cl else None
                if not p:
                    return self._send_json({"pending": None})
                return self._send_json({"pending": {
                    "id": p.get("id"), "num": p.get("num"), "name": p.get("name"),
                    "device_id": p.get("device_id"), "age": int(time.time() - p.get("at", 0))}})
            if path == "/api/remote/status":         # desktop control panel: pairing + device list
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                if REMOTE is None:
                    return self._send_json({"available": False})
                return self._send_json(dict(available=True, **REMOTE.status()))
            if path == "/api/remote/qr":             # SVG QR of the pairing link (stdlib encoder)
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                return self._serve_remote_qr()
            self._send_html(b"not found", 404, "text/plain; charset=utf-8")
        except BrokenPipeError:
            pass
        except Exception as e:                       # never take the server down on one bad request
            try:
                self._send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        self._vscode_embed = False
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, 403)
        if not self._peer_ok(parsed):
            return self._send_json({"error": "pairing required"}, 403)
        try:
            if path == "/api/pair":
                return self._serve_pair_exchange()
            if path == "/api/run/cancel":
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(4096)
                if body is None:
                    return self._send_json({"error": "expected JSON object"}, 400)
                sid = str(body.get("session") or "").strip()
                if not sid:
                    return self._send_json({"error": "session required"}, 400)
                out = Handler._run_cancel(sid, str(body.get("run") or "").strip() or None)
                code = 200 if out.get("ok") else (409 if out.get("status") == "run_mismatch" else 404)
                return self._send_json(out, code)
            if path == "/api/library/action":
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(8192)
                if body is None:
                    return self._send_json({"error": "expected JSON object"}, 400)
                action = str(body.get("action") or "").strip().lower()
                ext_id = str(body.get("id") or "").strip()
                version = str(body.get("version") or "").strip()
                if action not in ("enable", "disable", "rollback", "uninstall"):
                    return self._send_json({"error": "unknown Library action"}, 400)
                if not ext_id or len(ext_id) > 128:
                    return self._send_json({"error": "extension id required"}, 400)
                if version and len(version) > 128:
                    return self._send_json({"error": "invalid extension version"}, 400)
                if "approve" in body and not isinstance(body.get("approve"), bool):
                    return self._send_json({"error": "approve must be true or false"}, 400)
                approve = body.get("approve") is True
                from .extensions import ExtensionError, ExtensionStore
                store = ExtensionStore(_state_root())
                try:
                    if action == "enable":
                        result = store.enable(ext_id, version, approve=approve)
                    elif action == "disable":
                        result = store.disable(ext_id)
                    elif action == "rollback":
                        if version:
                            return self._send_json(
                                {"error": "rollback chooses the previous installed version"}, 400)
                        result = store.rollback(ext_id, approve=approve)
                    else:
                        # Deliberately never force removal from the web surface: an active package
                        # must first be disabled, making the state transition visible and reversible.
                        result = store.uninstall(ext_id, version, force=False)
                except ExtensionError as exc:
                    return self._send_json({"error": str(exc)}, 409)
                return self._send_json({"ok": True, "action": action, "extension": result})
            if path in ("/api/plan", "/api/plan/approve", "/api/review/handoff"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(131072)
                if body is None:
                    return self._send_json({"error": "expected JSON object"}, 400)
                sid = str(body.get("session") or "").strip()
                if not sid:
                    return self._send_json({"error": "session required"}, 400)
                if path == "/api/review/handoff":
                    artifact = _latest_review_artifact(sid)
                    wanted = {str(x) for x in (body.get("finding_ids") or [])}
                    selected = [x for x in artifact["findings"] if x.get("id") in wanted]
                    if not selected:
                        return self._send_json({"error": "select at least one current finding"}, 400)
                    return self._send_json({"ok": True, "handoff": {
                        "session": sid, "intent": "build", "source": "review",
                        "finding_ids": [x["id"] for x in selected],
                        "prompt": _review_build_prompt(selected)}})
                from .plantool import PlanArtifactStore, RevisionConflict
                store = PlanArtifactStore()
                if "revision" not in body:
                    return self._send_json({"error": "revision required for safe update"}, 400)
                try:
                    if path == "/api/plan":
                        # Approval is its own explicit, auditable user action.
                        patch = {k: body[k] for k in
                                 ("title", "files", "risks", "checks", "todos") if k in body}
                        # Any edit invalidates the previous approval. The next Build
                        # handoff must approve the exact newly-saved revision.
                        patch["approved"] = False
                        artifact = store.update(_web_plan_scope(sid), patch,
                                                expected_revision=body["revision"], actor="user")
                        return self._send_json({"ok": True, "session": sid,
                                                "artifact": artifact})
                    artifact = store.update(_web_plan_scope(sid), {"approved": True},
                                            expected_revision=body["revision"], actor="user")
                except RevisionConflict as exc:
                    return self._send_json({"error": str(exc), "conflict": True}, 409)
                except (TypeError, ValueError) as exc:
                    return self._send_json({"error": str(exc)}, 400)
                return self._send_json({"ok": True, "artifact": artifact,
                                        "can_start": True, "handoff": {
                                            "session": sid, "intent": "build", "source": "plan",
                                            "plan_revision": artifact["revision"],
                                            "prompt": _plan_build_prompt(artifact)}})
            if path == "/api/recovery/reconcile":
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(8192)
                if body is None:
                    return self._send_json({"error": "expected JSON object"}, 400)
                if body.get("confirmed") is not True:
                    return self._send_json({"error": "explicit confirmed=true is required"}, 400)
                sid = str(body.get("session") or "").strip()
                resolution = str(body.get("resolution") or "").strip()
                if not sid:
                    return self._send_json({"error": "session required"}, 400)
                from . import sessions
                try:
                    state = sessions.reconcile_recovery(
                        sid, resolution, note=str(body.get("note") or "")[:1000], confirmed=True,
                        directory=os.path.join(_state_root(), "sessions"))
                except KeyError:
                    return self._send_json({"error": "no such session"}, 404)
                except ValueError as exc:
                    return self._send_json({"error": str(exc)}, 409)
                return self._send_json({"ok": True, "session": sid,
                                        "state": _public_recovery(state, sid) if state else None})
            if path in ("/api/automation/webhook", "/api/automations/webhook"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(131072)
                if body is None:
                    return self._send_json({"error": "expected JSON object"}, 400)
                aid = str(body.get("automation_id") or "").strip()
                payload = body.get("payload")
                if not aid or not isinstance(payload, dict):
                    return self._send_json({"error": "automation_id and object payload required"}, 400)
                from .automations import (AutomationQueueFull, AutomationStore, PermissionDenied,
                                          TriggerEngine)
                try:
                    with AutomationStore(os.path.join(_state_root(), "automations.db")) as store:
                        eid = TriggerEngine(store).ingest_webhook(
                            aid, payload, authenticated=True,
                            delivery_id=str(body.get("delivery_id") or "")[:200])
                except KeyError as exc:
                    return self._send_json({"error": str(exc)}, 404)
                except PermissionDenied as exc:
                    return self._send_json({"error": str(exc)}, 403)
                except AutomationQueueFull as exc:
                    return self._send_json({"error": str(exc)}, 429)
                except ValueError as exc:
                    return self._send_json({"error": str(exc)}, 400)
                return self._send_json({"ok": True, "accepted": bool(eid),
                                        "execution_id": eid})
            if path in ("/api/mission/specialist/steer", "/api/mission/specialist/cancel"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(8192)
                if body is None:
                    return self._send_json({"error": "expected JSON object"}, 400)
                run_id = str(body.get("run_id") or "").strip()
                if not run_id:
                    return self._send_json({"error": "run_id required"}, 400)
                from .missionweb import MissionService
                svc = MissionService()
                try:
                    if path.endswith("/steer"):
                        text = str(body.get("text") or "").strip()
                        if not text:
                            return self._send_json({"error": "text required"}, 400)
                        value = svc.steer_specialist(run_id, text[:4000],
                                                     str(body.get("sender_run_id") or "")[:100])
                    else:
                        value = svc.cancel_specialist(
                            run_id, str(body.get("sender_run_id") or "")[:100])
                    value = _public_specialist(value)
                    return self._send_json(value, 404 if value.get("error") else 200)
                finally:
                    svc.close()
            if path == "/api/checkpoint/restore":
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                return self._serve_checkpoint_restore()
            if path == "/api/mcp":
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 8192:
                    return self._send_json({"error": "bad body"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"error": "bad json"}, 400)
                if not isinstance(body, dict):
                    return self._send_json({"error": "expected object"}, 400)
                from . import mcpclient
                action = str(body.get("action") or "")
                name = str(body.get("name") or "")
                if action in ("enable", "disable"):
                    if not mcpclient.set_enabled(name, action == "enable"):
                        return self._send_json({"error": "no such server"}, 404)
                    return self._send_json({"ok": True, "servers": mcpclient.status(),
                                            "note": "takes effect on the next collie run"})
                if action == "add":
                    # One field, not a form of them: an https:// URL means remote, anything else is
                    # the stdio command line — the same rule `collie mcp add` uses, so the two ways
                    # in cannot disagree about what you typed.
                    target = str(body.get("target") or "").strip()
                    if not target:
                        return self._send_json({"error": "need a URL or a command"}, 400)
                    if target.startswith(("http://", "https://")):
                        cfg = {"url": target}
                    else:
                        parts = target.split()
                        cfg = {"command": parts[0]}
                        if len(parts) > 1:
                            cfg["args"] = parts[1:]
                    err = mcpclient.add_server(name, cfg, replace=False)
                    if err:
                        return self._send_json({"error": err}, 400)
                    return self._send_json({"ok": True, "servers": mcpclient.status(),
                                            "note": "takes effect on the next collie run"})
                if action == "remove":
                    if not mcpclient.remove_server(name):
                        return self._send_json({"error": "no such server"}, 404)
                    _MCP_LOGIN_ERR.pop(name, None)
                    return self._send_json({"ok": True, "servers": mcpclient.status()})
                if action == "logout":
                    toks = mcpclient._load_tokens()
                    had = toks.pop(name, None) is not None
                    mcpclient._save_tokens(toks)
                    _MCP_LOGIN_ERR.pop(name, None)
                    return self._send_json({"ok": True, "had_token": had,
                                            "servers": mcpclient.status()})
                if action == "connect":
                    # One press, for a service whose address we already know. Add it and go straight
                    # into the browser handshake — the two used to be separate steps with a form in
                    # between, and the form asked for the very thing the catalog exists to supply.
                    hit = mcpclient.known(name)
                    if not hit:
                        return self._send_json({"error": "not a known service — use Add with a URL"}, 400)
                    name = hit["name"]
                    if (hit.get("byo_client")
                            and not (mcpclient._load_config().get(name) or {}).get("client_id")):
                        # Refuse before adding it. Otherwise the press adds a server, the handshake
                        # dies on "no client_id", and the panel shows a service that looks one
                        # Sign-in away from working and never will be.
                        return self._send_json(
                            {"error": mcpclient.byo_client_help(name, hit["label"], hit["url"])}, 400)
                    if name not in mcpclient._load_config():
                        err = mcpclient.add_server(name, {"url": hit["url"]}, replace=False)
                        if err:
                            return self._send_json({"error": err}, 400)
                    action = "login"                      # fall through to the browser handshake
                if action == "login":
                    cfg = mcpclient._load_config().get(name)
                    if not cfg:
                        return self._send_json({"error": "no such server"}, 404)
                    if name in _MCP_LOGIN_BUSY:
                        return self._send_json({"ok": True, "busy": True})
                    _MCP_LOGIN_ERR.pop(name, None)
                    _MCP_LOGIN_BUSY.add(name)

                    def _run_login(nm=name, c=cfg):
                        try:
                            mcpclient.login(nm, c)
                            # Warm the tool cache while authorized, so the panel can show a real
                            # count instead of "unknown" right after a successful login.
                            try:
                                conn = mcpclient._get_conn(nm, c)
                                tools = [{"name": t.get("name"), "description": t.get("description", ""),
                                          "inputSchema": t.get("inputSchema") or t.get("input_schema")}
                                         for t in conn.list_tools() if t.get("name")]
                                cache = mcpclient._read_cache()
                                cache[nm] = {"hash": mcpclient._cfg_hash(c), "tools": tools}
                                mcpclient._write_cache(cache)
                            except Exception:
                                pass
                        except Exception as exc:
                            _MCP_LOGIN_ERR[nm] = "%s: %s" % (type(exc).__name__, exc)
                        finally:
                            _MCP_LOGIN_BUSY.discard(nm)

                    threading.Thread(target=_run_login, daemon=True).start()
                    return self._send_json({"ok": True, "started": True})
                return self._send_json({"error": "unknown action"}, 400)
            if path.startswith("/api/remote/"):      # desktop control panel actions (local only)
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                action = path[len("/api/remote/"):]
                if action == "enable":                # lazily create the relay client if needed
                    rs = _ensure_remote(self.server.server_address[1])
                    rs.start()
                    from . import settings as _s
                    _s.update({"REMOTE": "on"})        # persist → auto-starts on next launch
                    return self._send_json(dict(ok=True, **rs.status()))
                if REMOTE is None:
                    return self._send_json({"error": "remote not available"}, 503)
                if action in ("approve", "deny"):
                    ok = REMOTE.decide_pair(action == "approve")
                    return self._send_json({"ok": bool(ok)})
                if action == "disable":
                    REMOTE.stop()
                    from . import settings as _s
                    _s.update({"REMOTE": "off"})       # persist the off state too
                    return self._send_json(dict(ok=True, **REMOTE.status()))
                if action == "rotate":
                    return self._send_json({"ok": True, "paircode": REMOTE.rotate_code(), "link": REMOTE.link()})
                if action == "forget":
                    body = self._read_json(4096) or {}
                    return self._send_json({"ok": REMOTE.forget(body.get("device_id", ""))})
                if action == "rename":
                    body = self._read_json(4096) or {}
                    name = (body.get("name") or "").strip()[:60]
                    return self._send_json({"ok": REMOTE.rename(body.get("device_id", ""), name)})
                return self._send_json({"error": "unknown action"}, 404)
            if path == "/api/settings":
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 65536:                       # config is tiny; reject junk/oversize
                    return self._send_json({"error": "bad body"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"error": "bad json"}, 400)
                if not isinstance(body, dict):
                    return self._send_json({"error": "expected object"}, 400)
                from . import settings
                # prev_wp must reflect what's REALLY running (the logon .vbs), not settings.json — the
                # GET side reports WALLPAPER that way too, so the toggle the user flipped agrees. Reading
                # settings.get() here let a pre-existing-autostart user's "off" flip no-op (vbs stayed).
                prev_wp = False
                try:
                    from . import plat, wallpaper as _wp
                    prev_wp = plat.is_windows() and os.path.exists(_wp._startup_vbs())
                except Exception:
                    pass
                # MERGE, don't replace: a partial POST (e.g. the onboarding ambient step sending only
                # {WALLPAPER}) must NOT wipe PROVIDER/MODEL/LANG. update() loads + merges + saves; a
                # full modal payload merges to the same result as a replace.
                try:
                    saved = settings.update(body)
                except ValueError as exc:
                    return self._send_json({"error": str(exc)}, 400)
                settings.apply()                              # take effect for the next query now
                # Ambient-desktop autostart is USER-controlled: toggling WALLPAPER creates/removes the
                # logon launcher — install() also starts it now, uninstall() stops it. Only on a change.
                if "WALLPAPER" in body:
                    want_wp = str(body.get("WALLPAPER") or "").lower() in ("on", "1", "true")
                    if want_wp != prev_wp:
                        try:
                            from . import wallpaper as wp
                            wp.install() if want_wp else wp.uninstall()
                        except Exception as exc:
                            # The desktop toggle controls a real OS integration. If that operation
                            # fails, restore the persisted value and report the failure; claiming
                            # success here leaves Settings disagreeing with what starts at logon.
                            settings.update({"WALLPAPER": "on" if prev_wp else "off"})
                            settings.apply()
                            return self._send_json({"ok": False,
                                                    "error": "could not apply ambient desktop: %s" % exc,
                                                    "values": settings.all_values()}, 500)
                return self._send_json({"ok": True, "values": settings.all_values(), "saved": saved})
            if path == "/api/work-identities":
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(4096)
                if body is None:
                    return self._send_json({"error": "expected JSON object"}, 400)
                action = str(body.get("action") or "").strip().lower()
                connection = str(body.get("connection") or "google_voice").strip().lower()
                if connection != "google_voice" or action not in ("connect", "disconnect"):
                    return self._send_json({"error": "unknown work-identity action"}, 400)
                from .workidentity import connect_google_voice, disconnect_google_voice
                try:
                    result = (connect_google_voice(body.get("last4", ""), _state_root())
                              if action == "connect" else
                              disconnect_google_voice(_state_root()))
                except (RuntimeError, ValueError) as exc:
                    return self._send_json({"error": str(exc)}, 409)
                return self._send_json({"ok": True, "connection": result})
            if path == "/api/browser/start":
                # onboarding "connect your browser": bring the localhost bridge up (windowless), so the
                # extension has something to poll. Returns the extension folder for the Load-unpacked step.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from . import browserbridge as bb
                ok = bb.start_background()
                ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_ext")
                return self._send_json({"ok": bool(ok), "ext_path": ext})
            if path in ("/api/record/start", "/api/record/stop", "/api/record/play",
                        "/api/record/reveal", "/api/record/delete"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from . import record as rec
                if path.endswith("/stop"):
                    return self._send_json({"ok": True, "message": rec.stop()})
                body = {}
                try:
                    n = int(self.headers.get("content-length") or 0)
                    if 0 < n <= 8192:
                        body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") or {}
                except Exception:
                    body = {}
                if path.endswith("/play"):
                    return self._send_json({"ok": rec.play(body.get("name") or "")})
                if path.endswith("/reveal"):
                    return self._send_json({"ok": rec.reveal()})
                if path.endswith("/delete"):
                    return self._send_json({"ok": rec.delete_recording(body.get("name") or "")})
                try:
                    msg = rec.start(no_cam=bool(body.get("no_cam")), no_mic=bool(body.get("no_mic")),
                                    sysaudio=body.get("sys_audio") or None,
                                    webcam=body.get("webcam") or None, mic=body.get("mic") or None,
                                    position=body.get("position") or "bl",
                                    window=body.get("window") or None, region=body.get("region") or None,
                                    monitor=body.get("monitor") or None,
                                    countdown=int(body.get("countdown") or 0))
                except Exception as e:
                    return self._send_json({"error": str(e)}, 400)
                return self._send_json({"ok": msg.startswith("recording"), "message": msg})
            if path.startswith("/api/desktop/"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from . import desktop as dt
                action = path[len("/api/desktop/"):]
                body = {}
                try:
                    n = int(self.headers.get("content-length") or 0)
                    if 0 < n <= 65536:
                        body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") or {}
                except Exception:
                    body = {}
                if action == "config":
                    return self._send_json(dt.save_config(body))
                if action == "launch":
                    return self._send_json({"ok": dt.launch(body.get("target") or "")})
                if action == "media":
                    return self._send_json({"ok": dt.media(body.get("cmd") or "")})
                if action == "open":
                    return self._send_json({"ok": dt.open_project(body.get("root") or "")})
                if action == "reveal":
                    # macOS only: collie sits above the Finder icons, so it eats the click that
                    # used to reveal the desktop. This is that gesture, given back.
                    try:
                        from . import desktop_mac
                        ok = desktop_mac.reveal_desktop(bool(body.get("show", True)))
                    except Exception:
                        ok = False
                    return self._send_json({"ok": ok})
                if action == "play":
                    # Play it HERE, on the computer. The existing music path resolves a stream and
                    # hands the URL to the caller's own audio element, which a phone does not have —
                    # so "play Cruel Summer" found the track and then nothing happened.
                    r = dt.play_here(
                        body.get("q") or body.get("query") or "",
                        artist=body.get("artist") or "", title=body.get("title") or "",
                        region=body.get("region") or "")
                    sid = Handler._record_command(body.get("session"), body.get("said"),
                                                  _play_summary(r))
                    if sid:
                        r["session"] = sid
                    return self._send_json(r)
                if action == "stopaudio":
                    return self._send_json(dt.stop_here())
                if action == "intent":
                    # Routes to app/system/project/stop/music, and to `agent` for everything else.
                    # `music` is still in the reply so an older page keeps working unchanged.
                    r = dt.desktop_intent(body.get("text") or "")
                    if r.get("action") == "music":
                        m = dt.music_intent(body.get("text") or "")
                        r.update({k: v for k, v in m.items() if k != "action"})
                    r["music"] = r.get("action") == "music" and bool(r.get("query") or r.get("arg"))
                    if r["music"] and not r.get("query"):
                        r["query"] = r.get("arg") or ""
                    # "stop the music" used to be a message TO the caller: the router returned
                    # action=stop and the web page paused its own <audio>. Now that the desktop plays
                    # music itself there was nothing on this machine that could stop it — no button
                    # anywhere, and the words did nothing. If something is playing here, stop it.
                    if r.get("action") == "stop" and dt.playing_here().get("track"):
                        dt.stop_here()
                        r["stopped_audio"] = True
                    # A command carried out here is still something that happened in a conversation.
                    # Music is recorded by /play instead, once it knows what it actually started.
                    if r.get("action") not in ("agent", "music"):
                        sid = Handler._record_command(body.get("session"), body.get("text"),
                                                      _intent_summary(r))
                        if sid:
                            r["session"] = sid
                    return self._send_json(r)
                return self._send_json({"error": "unknown action"}, 404)
            if path == "/api/model":
                # Model picker's one-click switch: merge PROVIDER+MODEL into settings (never
                # clobbers other keys) and apply, so the next run uses the chosen model.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 4096:
                    return self._send_json({"error": "bad body"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"error": "bad json"}, 400)
                from . import settings, catalog
                if (body or {}).get("auto") is True:
                    # Unpin only the model. Provider/auth stays exactly where the
                    # user put it; the task router chooses within that provider.
                    provider = settings.get("PROVIDER", "") or _provider()
                    if not provider:
                        return self._send_json({"error": "choose a provider before Auto"}, 400)
                    settings.update({"MODEL": ""})
                    settings.apply()
                    out = {"ok": True, "provider": provider, "model": "", "auto": True}
                    if settings.pinned("MODEL"):
                        out.update(ok=False, pinned=["MODEL"], error=(
                            "Saved Auto, but COLLIE_MODEL is set in this collie's environment and "
                            "outranks it. Restart collie without COLLIE_MODEL."))
                    return self._send_json(out)
                provider, model = catalog.resolve((body or {}).get("id", ""))
                if not provider:
                    return self._send_json({"error": "bad model id"}, 400)
                partial = {"PROVIDER": provider}
                if model:
                    partial["MODEL"] = model
                settings.update(partial)
                settings.apply()
                out = {"ok": True, "provider": provider, "model": model or ""}
                # The write went through; whether it CHANGES anything is a different question.
                # Reporting plain success while a hard-set env var keeps serving another provider is
                # how "the picker doesn't work" stayed a mystery instead of becoming a message.
                pin = [k for k in ("PROVIDER", "MODEL") if settings.pinned(k)]
                if pin:
                    out["ok"] = False
                    out["pinned"] = pin
                    out["error"] = (
                        "Saved, but %s is set in this collie's environment and outranks it — runs will "
                        "keep using %s. Restart collie without it."
                        % (", ".join("COLLIE_" + k for k in pin),
                           settings.get("PROVIDER", "") or "the pinned provider"))
                return self._send_json(out)
            if path in ("/api/mission", "/api/mission/run", "/api/mission/confirm",
                        "/api/mission/pause", "/api/mission/resume", "/api/mission/cancel",
                        "/api/mission/continue", "/api/mission/accept", "/api/mission/check",
                        "/api/mission/reconcile", "/api/mission/tick"):
                # The NL front door: start/gate/carry a delegate mission from the chat.
                # CSRF-gated like every state-changing route — a mission runs the model
                # and can fire (gated) real-world actions, so a drive-by must never start one.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(8192)
                if body is None:
                    return self._send_json({"error": "bad body"}, 400)
                from . import settings
                settings.apply()                          # run on the Settings-panel provider
                from .missionweb import MissionService
                svc = MissionService()
                try:
                    if path == "/api/mission":
                        goal = (body.get("goal") or "").strip()
                        if not goal:
                            return self._send_json({"error": "goal required"}, 400)
                        bounds = {k: body[k] for k in
                                  ("allowed_domains", "actions_per_hour",
                                   "max_irreversible_actions", "max_total_steps",
                                   "spend_max_usd") if body.get(k) is not None}
                        try:
                            autonomy = body.get("autonomous") if "autonomous" in body else None
                            if autonomy is not None and not isinstance(autonomy, bool):
                                return self._send_json(
                                    {"error": "autonomous must be a boolean when supplied"}, 400)
                            created = svc.start(goal, autonomous=autonomy, **bounds)
                        except ValueError as e:
                            return self._send_json({"error": str(e)}, 400)
                        return self._send_json(created, 201)
                    mid = (body.get("id") or "").strip()
                    if not mid and path != "/api/mission/tick":
                        return self._send_json({"error": "id required"}, 400)
                    if path == "/api/mission/confirm":
                        nonce = (body.get("nonce") or "").strip()
                        if not nonce:
                            return self._send_json({"error": "nonce required"}, 400)
                        out = svc.confirm(mid, nonce)
                    elif path == "/api/mission/run":
                        out = svc.run(mid)
                    elif path == "/api/mission/pause":
                        out = svc.pause(mid)
                    elif path == "/api/mission/resume":
                        out = svc.resume(mid)
                    elif path == "/api/mission/cancel":
                        out = svc.cancel(mid)
                    elif path == "/api/mission/accept":
                        out = svc.accept(mid)
                    elif path == "/api/mission/continue":
                        out = svc.continue_after_human(mid, body.get("note") or "")
                    elif path == "/api/mission/reconcile":
                        out = svc.reconcile(mid, body.get("note") or "")
                    elif path == "/api/mission/check":
                        out = svc.check(mid)
                    else:
                        out = svc.tick(mid or None)             # daemon/debug global tick
                    code = 404 if out.get("error") == "unknown mission" else \
                        (409 if out.get("error") else 200)
                    return self._send_json(out, code)
                finally:
                    svc.close()
            if path == "/api/route":
                # The classifying "head": type a message (chat/code/mission) so the UI
                # routes it. CSRF-gated (it runs the model). Model down -> 503, honestly.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(8192)
                if body is None:
                    return self._send_json({"error": "bad body"}, 400)
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send_json({"error": "text required"}, 400)
                from . import settings
                settings.apply()
                from .router import classify, ModelUnavailable, DEFAULT_ROUTER_MODEL
                from .providers import make_provider
                try:
                    # the router runs on every message's critical path. Default it to
                    # Sonnet (fast + capable; all of haiku/sonnet/opus scored 28/28 on the
                    # battery, so this trades only latency), overridable up (opus) or down
                    # (haiku) via COLLIE_ROUTER_MODEL. Only anthropic providers take a claude
                    # model id; others keep their own default.
                    _name = _provider()
                    if not _name:      # same rule as the run path: never route on a fixture
                        return self._send_json({"error": "model_unavailable",
                                                "detail": "no model configured"}, 503)
                    _rmodel = os.environ.get("COLLIE_ROUTER_MODEL") or (
                        DEFAULT_ROUTER_MODEL if _name in ("anthropic-oauth", "anthropic") else None)
                    # Classification is tiny and reversible.  Keep it at the
                    # lowest supported effort regardless of the execution run's
                    # configured depth; the resolved task gets its own decision.
                    prov = make_provider(_name, _rmodel, effort="low")
                    return self._send_json(classify(text, prov))
                except ModelUnavailable as e:
                    return self._send_json({"error": "model_unavailable", "detail": str(e)}, 503)
            if path == "/api/write":
                # code-editor write-back: verify (compile + relevant tests) then write, or reject.
                # CSRF-gated like every state-changing route; a whole file can be up to ~2MB.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 2_000_000:
                    return self._send_json({"ok": False, "stage": "guard", "error": "bad body size"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"ok": False, "stage": "guard", "error": "bad json"}, 400)
                rel = (body or {}).get("path")
                content = (body or {}).get("content")
                if not isinstance(rel, str) or not isinstance(content, str):
                    return self._send_json({"ok": False, "stage": "guard",
                                            "error": "need path + content strings"}, 400)
                from . import webedit
                res = webedit.write_checked(os.getcwd(), rel, content)
                return self._send_json(res, 200 if res.get("ok") else 200)
            if path == "/api/upload":
                # stash an attached image; the next /api/stream?imgs=<id> references it. CSRF-gated;
                # base64 up to ~16MB (a big screenshot). Returns {id}.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 16_000_000:
                    return self._send_json({"error": "bad body size"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"error": "bad json"}, 400)
                mt = (body or {}).get("media_type") or "image/png"
                data = (body or {}).get("data") or ""
                if not isinstance(data, str) or not data or not str(mt).startswith("image/"):
                    return self._send_json({"error": "need image data + media_type"}, 400)
                return self._send_json({"id": Handler._img_put(mt, data)})
            if path == "/api/steer":
                # mid-run steering: queue user text for the session's in-flight run. The loop injects
                # it as a user message at the next turn boundary. CSRF-gated; tiny body.
                # {queued:false} means no active run — the client falls back to starting a new turn.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 65536:
                    return self._send_json({"queued": False, "error": "bad body"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"queued": False, "error": "bad json"}, 400)
                sid = (body or {}).get("session") or ""
                text = ((body or {}).get("q") or "").strip()
                if not sid or not text:
                    return self._send_json({"queued": False, "error": "need session + q"}, 400)
                return self._send_json({"queued": Handler._steer_push(sid, text[:4000])})
            if path == "/api/approve":
                # Answer a parked approval. Same CSRF gate and tiny body as /api/steer.
                # {resolved:false} means the run ended, the item is unknown, or another
                # surface answered first — never an error, because a lost race is not a fault.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 4096:
                    return self._send_json({"resolved": False, "error": "bad body"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"resolved": False, "error": "bad json"}, 400)
                sid = (body or {}).get("session") or ""
                item = (body or {}).get("id") or ""
                answer = str((body or {}).get("answer") or "")
                # An unrecognised answer is a refusal, decided here rather than trusted from
                # the wire: inbox.outcome_of maps anything it does not know to reject, so a
                # malformed or replayed body can never become consent.
                from .inbox import R_ALLOW, R_ALWAYS, R_DENY, R_NEVER
                if answer not in (R_ALLOW, R_ALWAYS, R_DENY, R_NEVER):
                    answer = R_DENY
                if not sid or not item:
                    return self._send_json({"resolved": False, "error": "need session + id"}, 400)
                return self._send_json({"resolved": Handler._inbox_answer(sid, item, answer)})
            self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    # ------------------------------------------------------------------ handlers
    def _serve_logo(self):
        try:
            with open(LOGO_SVG, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("content-type", "image/svg+xml")
            self.send_header("cache-control", "max-age=86400")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_html(b"logo missing", 404, "text/plain; charset=utf-8")

    def _serve_avatar(self, parsed=None):
        """This dog's transparent first-party face, drawn once on the desktop.

        The phone needs a picture per dog to make a switcher worth looking at, and the obvious
        shortcut — port the derivation (sha256 of the name -> coat, plate) into Swift — is two
        implementations of one identity, which drift and then show the same dog two different
        colours on two screens. One generator, served. An unnamed server gets the deterministic
        generic Collie coat, still without the external app-icon plate.
        """
        name = whoami().get("name") or "Collie"
        qs = urllib.parse.parse_qs(parsed.query if parsed is not None else "")
        preview = (qs.get("preview") or [""])[0]
        if preview:
            if not self._authed(parsed):
                return self._send_json({"error": "forbidden"}, 403)
            try:
                from .settings import normalize_companion_name
                name = normalize_companion_name(preview, allow_empty=False)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
        body = b""
        try:
            from . import avatar
            # 256 px is still >3x the largest first-party display size at 1x and keeps a 2x/3x
            # screen crisp. The library's external icon default remains 512 px; using it here made
            # the pure-Python first render several seconds slower for pixels no Collie UI displays.
            body = avatar.png(name, size=256, plate=False)
        except Exception:
            body = b""
        if not body:
            return self._send_html(b"no avatar", 404, "text/plain; charset=utf-8")
        self.send_response(200)
        self.send_header("content-type", "image/png")
        # whoami's URL is versioned too, but no-store makes direct/hard-coded callers just as safe:
        # a rename must not leave yesterday's coat on a phone or ambient desktop.
        self.send_header("cache-control", "private, no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self):
        try:
            with open(INDEX_HTML, "rb") as f:
                html = f.read()
            # inject the CSRF secret so same-origin JS can read it (cross-site JS can't reach it).
            # Robust anchor: prefer the charset meta, else the <head>/doctype, else prepend — a
            # silent no-op would give JS an empty token and 403 every /api/* call (whole app dead).
            #
            # LOOPBACK ONLY. Under `--lan` this page is otherwise a token dispenser for the whole
            # network, and the token runs bash. A non-loopback client that already has a token got
            # past _peer_ok, so it needs no second copy; one that doesn't gets the page tokenless.
            # ...and NOT to a relay-replayed request: the relay injects ?token= server-side, so the
            # phone never needs (or should get) the raw token embedded in the page it receives.
            token = self._embed_token()
            meta = ('<meta name="collie-token" content="%s">\n' % token).encode()
            for anchor in (b'<meta charset="utf-8">', b'<head>', b'<!doctype html>', b'<!DOCTYPE html>'):
                if anchor in html:
                    html = html.replace(anchor, anchor + b"\n" + meta, 1)
                    break
            else:
                html = meta + html            # no known anchor — prepend so the token is never missing
            self._send_html(html)
        except FileNotFoundError:
            self._send_html(b"index.html missing next to webapp.py", 500,
                            "text/plain; charset=utf-8")

    # ------------------------------------------------------------------ Map view (galaxy)
    def _serve_static(self, name, ctype):
        """Serve a file from webui/ (three.min.js verbatim). HTML files (map.html) get the CSRF token
        injected like the index so same-origin JS — e.g. the Map's code-editor Commit — can read it."""
        try:
            with open(os.path.join(HERE, "webui", name), "rb") as f:
                data = f.read()
            if name.endswith(".html"):
                # same rule as the index: embed the CSRF token only for a DIRECT loopback page load,
                # never for a relay-replayed request (the phone gets ?token= injected server-side).
                tok = self._embed_token()
                meta = ('<meta name="collie-token" content="%s">\n<meta name="collie-boot" content="%s">\n' % (tok, BOOT)).encode()
                for anchor in (b'<meta charset="utf-8">', b'<head>', b'<!doctype html>', b'<!DOCTYPE html>'):
                    if anchor in data:
                        data = data.replace(anchor, anchor + b"\n" + meta, 1)
                        break
                else:
                    data = meta + data
            self._send_html(data, 200, ctype)
        except FileNotFoundError:
            self._send_html(("missing %s" % name).encode(), 404, "text/plain; charset=utf-8")

    @staticmethod
    def _default_repo():
        """The project to show when the request names none.

        NOT simply the server's cwd. The web server is spawned without a cwd of its own, so from a
        shortcut launch it inherits Explorer's — and the map dutifully drew C:\\Windows\\System32,
        nebulae labelled DRIVERSTORE and SPOOL. When the cwd is not itself a project, use the one the
        user most recently worked in, which the session records remember.
        """
        from . import codemap
        cwd = os.getcwd()
        if codemap.git_root(cwd) == cwd:
            return cwd
        try:
            from . import sessions as _sess
            for s in (_sess.recent(50) or []):
                root = codemap.git_root((s or {}).get("cwd") or "") if isinstance(s, dict) else None
                if root:
                    return root
        except Exception:
            pass
        for r in (Handler._REPOS_CACHE.get("repos") or []):
            if r.get("root"):
                return r["root"]
        return cwd                                 # nothing better to offer; at least it is honest

    _TREE_CACHE: dict = {}
    def _serve_tree(self, qs=None):
        """GET /api/tree[?repo=ABS] -> a project's code galaxy (files with loc/defs/names/imports).
        `repo` picks any project the server has discovered; default = the last project worked in.
        Cached on the dir's mtime so repeated Map loads don't re-walk the tree."""
        from . import codemap
        cwd = Handler._default_repo()
        repo = ((qs or {}).get("repo", [""])[0] or "").strip()
        if repo:
            home = os.path.realpath(os.path.expanduser("~"))
            cand = os.path.realpath(os.path.expanduser(repo))
            # Never map an arbitrary path the request names — but "under the home directory" was the
            # wrong way to say that. Projects legitimately live on C:\workspace or /srv, and once
            # discovery started returning them this guard rejected every one, silently mapping cwd
            # instead: the picker offered nine projects and eight of them showed a tenth. The rule
            # that actually expresses the intent is that the server must have DISCOVERED the repo —
            # that list comes from its own cwd, runs and sessions, never from the caller.
            known = {os.path.realpath(r.get("root") or "")
                     for r in (Handler._REPOS_CACHE.get("repos") or [])}
            allowed = cand in known or cand == home or cand.startswith(home + os.sep)
            if allowed and codemap.git_root(cand) == cand:
                cwd = cand
        try:
            key = (cwd, os.path.getmtime(cwd))
        except OSError:
            key = (cwd, 0)
        if key not in Handler._TREE_CACHE:
            if len(Handler._TREE_CACHE) > 8:
                Handler._TREE_CACHE.clear()            # bounded LRU-ish; keep a few repos warm
            Handler._TREE_CACHE[key] = codemap.build_tree(cwd)
        self._send_json({"cwd": cwd, "repo": os.path.basename(cwd), "files": Handler._TREE_CACHE[key]})

    _REPOS_CACHE: dict = {}
    _REPOS_SCAN: dict = {}          # the ONE in-flight scan {box, thread}, hoisted to class scope so a
    _REPOS_LOCK = None              # slow walk caches when it eventually finishes and never re-spawns
    REPOS_BUDGET_S = 8.0

    def _serve_repos(self):
        """GET /api/repos -> git projects under the user's home, one galaxy each.

        Bounded by a deadline, because a directory walk can BLOCK rather than merely be slow: a
        macOS media library full of cloud placeholders never returns from os.walk at all. That hung
        this endpoint forever — a phone screen spinning with no timeout of its own, and a server
        thread that never came back. Names known to do it are pruned in codemap, but the guarantee
        has to be structural: an answer arrives either way.

        ONE scan, ever: the box + thread live at class scope. A blocking walk is joined with a budget
        and reported `partial` — but the SAME thread is reused on every later poll (no per-request
        thread leak), and its box is persistent, so if it eventually returns the result is cached then.
        """
        from . import codemap
        import threading as _th

        if Handler._REPOS_LOCK is None:
            Handler._REPOS_LOCK = _th.Lock()

        if "repos" not in Handler._REPOS_CACHE:
            with Handler._REPOS_LOCK:
                scan = Handler._REPOS_SCAN
                if scan.get("thread") is None:            # start the single scan exactly once
                    home = os.path.expanduser("~")
                    # Seed with where work has ACTUALLY happened: this server's own directory and
                    # every cwd a run was started in. Walking the home directory alone misses the
                    # usual Windows layout completely — projects on C:\workspace, a home holding
                    # nothing but AppData — which is how the star-map came to open on a list of
                    # collie's own temp worktrees with the real repository nowhere in it.
                    seeds = [os.getcwd()]
                    try:
                        for r in (Handler._runs_snapshot() or []):
                            cwd = (r or {}).get("cwd")
                            if cwd:
                                seeds.append(cwd)
                    except Exception:
                        pass                              # no runs yet must not cost the scan
                    try:
                        from . import sessions as _sess
                        for s in (_sess.recent(50) or []):
                            cwd = (s or {}).get("cwd") if isinstance(s, dict) else None
                            if cwd:
                                seeds.append(cwd)
                    except Exception:
                        pass
                    box = {}
                    scan["box"] = box

                    def _run(b=box, seeds=seeds):
                        try:
                            b["repos"] = codemap.discover_repos(home, extra=seeds)
                        except Exception:
                            b["repos"] = []

                    t = _th.Thread(target=_run, name="collie-repos-scan", daemon=True)
                    scan["thread"] = t
                    t.start()
                t = scan["thread"]
            t.join(Handler.REPOS_BUDGET_S)               # join outside the lock — concurrent polls wait together
            box = Handler._REPOS_SCAN.get("box") or {}
            if "repos" not in box:
                return self._send_json({"cwd": os.getcwd(), "repos": [], "partial": True})
            Handler._REPOS_CACHE["repos"] = box["repos"]
        # `cwd` here is what the picker labels its default entry, so it must be the project /api/tree
        # would actually serve — not os.getcwd(), which would label the default "System32" while the
        # map drew something else.
        self._send_json({"cwd": Handler._default_repo(), "repos": Handler._REPOS_CACHE["repos"]})

    def _serve_session_map(self, qs):
        """GET /api/session_map?id=SID -> the files THAT run touched, grouped by repo (nebulae), plus
        a probe replaying the touches. Single-repo runs light one nebula; cross-repo runs span many."""
        from . import codemap, sessions
        sid = (qs.get("id", [""])[0] or "").strip()
        s = sessions.load(urllib.parse.unquote(sid)) if sid else None
        if not s:
            return self._send_json({"error": "no such session"}, 404)
        self._send_json(codemap.session_map(s, os.getcwd()))

    def _serve_file(self, qs):
        """GET /api/file?path=REL (under cwd) or ?abs=ABS (a repo file elsewhere under home) -> a
        file's source for the code sidebar. Both are guarded (no traversal, home-scoped, source ext)."""
        from . import codemap
        ab = (qs.get("abs", [""])[0] or "").strip()
        if ab:
            src = codemap.read_abs(ab)
            key = ab
        else:
            key = (qs.get("path", [""])[0] or "").strip()
            src = codemap.read_source(os.getcwd(), key)
        if src is None:
            return self._send_json({"error": "not found"}, 404)
        self._send_json({"path": key, "source": src})

    def _host_ok(self) -> bool:
        """Anti-DNS-rebinding: serve only requests whose Host header is a loopback name. Binding
        127.0.0.1 alone doesn't stop rebinding — a page on attacker.com can lower its TTL and
        re-resolve attacker.com -> 127.0.0.1, becoming same-origin with this server and reading the
        CSRF token out of the served HTML. Rejecting a non-loopback Host closes that: the browser
        always sends the navigated hostname (attacker.com) as Host, which never matches loopback.

        `--lan` widens this by exactly the machine's own addresses (LAN_HOSTS), because a phone on the
        same Wi-Fi necessarily sends `Host: 192.168.x.y:8787`. Still a closed set, never "any host"."""
        h = (self.headers.get("Host", "") or "").strip()
        host = h.rsplit(":", 1)[0].strip("[]").lower() if h else ""
        return host in ("", "127.0.0.1", "localhost", "::1", "collie.localhost") or host in LAN_HOSTS

    def _peer_is_loopback(self) -> bool:
        peer = (self.client_address[0] if self.client_address else "") or ""
        return peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or peer.startswith("127.")

    def _is_relay(self) -> bool:
        # the relay client replays a phone's request from 127.0.0.1 (so it looks loopback) but tags it
        # with this header — used to withhold the embedded CSRF token from pages sent to a phone.
        try:
            return (self.headers.get("X-Collie-Relay") or "") == "1"
        except Exception:
            return False

    def _vscode_embed_ok(self, parsed) -> bool:
        """Authorize only the main document inside Collie's VS Code webview.

        Loopback is not sufficient here: any site can try to frame localhost.  The extension mints
        one per-process secret, supplies it in the child URL, and passes the same value to the web
        process through its environment.  A minimum length prevents an accidentally configured
        human word from turning this narrowly-scoped exception into a guessable frame bypass.
        """
        expected = str(os.environ.get("COLLIE_VSCODE_EMBED_TOKEN") or "")
        values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("vscode_embed", [])
        if len(expected) < 32 or len(expected) > 512 or len(values) != 1:
            return False
        got = str(values[0] or "")
        return len(got) == len(expected) and hmac.compare_digest(got, expected)

    def _embed_token(self) -> str:
        """The CSRF token to bake into a served HTML page — but ONLY for a direct loopback page load.
        A non-loopback client got past _peer_ok with a token already, so it needs no second copy; and a
        relay-replayed request (a phone) must NEVER get the raw token — the relay injects ?token=
        server-side instead. Both cases fall through to '' (a tokenless page)."""
        return TOKEN if (self._peer_is_loopback() and not self._is_relay()) else ""

    def _peer_ok(self, parsed) -> bool:
        """Everything a NON-loopback client asks for must carry the token.

        Why the peer address and not the route: `/` embeds the token for same-origin JS, so leaving
        it ungated under `--lan` handed the token — and therefore `bash` on this machine — to anyone
        on the Wi-Fi. Gating by peer keeps the local browser untouched (it is loopback, so nothing
        changes for it) while a phone must present a token it can only obtain by pairing.

        `/api/pair` is the one pre-token route: it trades a one-shot secret, shown as a code on THIS
        machine's screen, for the token. That is the whole "you must physically see the screen" step.
        """
        if self._peer_is_loopback() or parsed.path == "/api/pair":
            return True
        return self._authed(parsed)

    def _serve_pair_exchange(self):
        """POST /api/pair {"nonce","proof"} -> {"server_proof","sealed_token"}.

        One shot, short-lived, rate-limited, and the secret never appears on the wire — see
        `_pair_prove` for why that matters on a LAN."""
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4096:
            return self._send_json({"error": "bad body"}, 400)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._send_json({"error": "bad json"}, 400)
        nonce = (body.get("nonce") or "").strip()
        proof = (body.get("proof") or "").strip()
        ok, detail = _pair_prove(nonce, proof)
        if not ok:
            return self._send_json({"error": detail}, 403)
        detail.update({"cwd": os.getcwd(), "provider": _provider()})
        return self._send_json(detail)

    def _serve_pair_page(self):
        """The pairing screen: shows the collie pair code for a phone camera to read.

        Loopback only — the page carries a live pairing secret, so serving it to the network would
        undo the handshake it exists to protect."""
        if not self._peer_is_loopback():
            return self._send_json({"error": "pairing page is loopback-only"}, 403)
        from . import paircode
        port = self.server.server_address[1]

        # With Collie Remote on, the phone is going to reach us THROUGH the relay, so the code has to
        # carry the room + relay pair code rather than a LAN address it cannot route to. Same symbol,
        # different payload type.
        # Expire before showing. Checking here rather than only on a timer is what makes the window
        # real: a pairing screen left open overnight refreshes its code the moment it is reloaded,
        # instead of displaying one that has been valid — and readable over someone's shoulder, or
        # in a screenshot — for hours.
        if REMOTE and REMOTE.enabled:
            REMOTE._maybe_expire()
        remote = REMOTE if (REMOTE and REMOTE.enabled and REMOTE.paircode) else None
        try:
            if remote is not None:
                # A STANDARD QR of the relay link, not the collie ring code.
                #
                # The ring code is unreadable by anything but collie — which was the point when the
                # only reader was the app. But a phone that has not got the app yet, or has an older
                # build, points its camera at the ring and gets nothing at all, with no clue why.
                # The relay link is a URL; a plain QR of it is read by every camera on earth, opens
                # the phone client, and the app scans the same URL when it is installed. One symbol,
                # both audiences. The ring stays available for in-app scanning, where it is faster.
                link = remote.link() or ""
                if link:
                    html = _relay_qr_page(link, remote.identity.room, remote.paircode,
                                          getattr(remote, "CODE_TTL", 180))
                    return self._send_html(html.encode("utf-8"), 200)
                payload = paircode.relay_payload_bytes(remote.identity.room, remote.paircode)
                target, ttl = "the relay", 0
            else:
                secret = _pair_mint()
                host = _pair_advertised_host()
                payload = paircode.payload_bytes(host, port, secret)
                target, ttl = "%s:%d" % (host, port), _PAIR_TTL
        except Exception as e:
            return self._send_html(("cannot build a pair code: %s" % e).encode(), 500,
                                   "text/plain; charset=utf-8")
        html = paircode.page(payload, host=target, port=port, ttl=ttl)
        self._send_html(html.encode("utf-8"))

    def _authed(self, parsed) -> bool:
        """State-changing / code-executing routes require the per-process token (query param).
        Same-origin page JS supplies it; a drive-by cross-site request cannot read it."""
        import hmac
        got = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        return hmac.compare_digest(got, TOKEN)     # constant-time compare

    def _serve_remote_qr(self):
        """Render the current pairing link as an SVG QR. Transparent background + light modules so it
        sits on the dark control panel; 404 if there is no link yet.

        Uses collie's own stdlib encoder rather than segno: an optional dependency meant this returned
        "pip install …" on a plain install, exactly when someone is first trying to pair a phone."""
        link = REMOTE.link() if REMOTE else None
        if not link:
            return self._send_json({"error": "no pairing link"}, 404)
        try:
            from . import qr
            svg = qr.svg(link, dark="#c9d1e6")
        except ValueError as e:                  # link longer than the encoder's 106-byte ceiling
            return self._send_json({"error": str(e)}, 500)
        except Exception as e:
            return self._send_json({"error": str(e)}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(svg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(svg)
        except BrokenPipeError:
            pass

    def _serve_sessions(self, qs=None):
        from . import sessions
        # ?n= (the Map asks for more so it can surface older runs that actually edited code, sorting
        # edited-first client-side); the main composer uses the default.
        try:
            n = max(1, min(80, int(((qs or {}).get("n", ["20"])[0]) or 20)))
        except ValueError:
            n = 20
        self._send_json({"sessions": sessions.recent(n)})

    def _serve_checkpoints(self):
        """List the snapshots that exist RIGHT NOW in this repo, plus why there are none.

        The reason matters as much as the list: a user who sees an empty list assumes nothing has
        happened yet, when the truth may be that this folder is not a git repo and no run has ever
        been protected. Those two states must not look alike.
        """
        from . import checkpoints as ckpt
        cwd = getattr(self.server, "cwd", None) or os.getcwd()
        ok, why = ckpt.available(cwd)
        if not ok:
            return self._send_json({"available": False, "reason": why, "checkpoints": []})
        try:
            items = [c.as_dict() for c in ckpt.history(cwd)]
        except ckpt.CheckpointError as e:
            return self._send_json({"available": False, "reason": str(e), "checkpoints": []})
        return self._send_json({"available": True, "reason": "", "checkpoints": items})

    def _serve_checkpoint_restore(self):
        """Rewind the working tree. DESTRUCTIVE, so it is POST + authed, and it reports exactly
        what happened — including whether untracked files could be rewound, which older snapshots
        cannot do."""
        from . import checkpoints as ckpt
        body = self._read_json() or {}
        ref = (body.get("ref") or "").strip()
        if not ref:
            return self._send_json({"error": "which checkpoint? pass ref"}, 400)
        cwd = getattr(self.server, "cwd", None) or os.getcwd()
        try:
            cp = ckpt.Checkpoint(ref=ref, session=str(body.get("session") or ""),
                                 n=int(body.get("n") or 0))
            return self._send_json({"ok": True, "result": ckpt.restore(cwd, cp)})
        except ckpt.CheckpointError as e:
            return self._send_json({"ok": False, "error": str(e)}, 409)
        except Exception as e:
            return self._send_json({"ok": False,
                                    "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def _serve_session(self, sid: str):
        from . import sessions
        s = sessions.load(urllib.parse.unquote(sid))
        if not s:
            return self._send_json({"error": "no such session"}, 404)
        # load() rehydrates tool_calls into ToolCall dataclasses; JSON's default=str would emit them
        # as repr strings ("ToolCall(id=…, name=…, args=…)"), which the Map's replay can't parse.
        # Normalize to plain {id, name, args} dicts so /api/session carries structured tool calls.
        for m in s.get("messages", []):
            tcs = m.get("tool_calls")
            if tcs:
                m["tool_calls"] = [
                    tc if isinstance(tc, dict) else
                    {"id": getattr(tc, "id", None), "name": getattr(tc, "name", None),
                     "args": getattr(tc, "args", None)}
                    for tc in tcs]
        self._send_json(s)

    def _serve_stream(self, qs):
        from .cli import (configure_run_options, default_gate, make_harness,
                          normalize_run_options)
        from . import sessions, settings
        settings.apply()   # a Settings-panel save takes effect on the next query, no restart

        q = (qs.get("q", [""])[0] or "").strip()
        sid = (qs.get("session", [""])[0] or "").strip() or sessions.new_id()
        # attached images: /api/stream?imgs=<id>,<id> references what the composer POSTed to /api/upload.
        # With images the user_msg becomes a multimodal list (text + image blocks) the provider layer
        # reshapes into each vendor's vision format.
        img_ids = [i for i in (qs.get("imgs", [""])[0] or "").split(",") if i]
        imgs = [Handler._img_get(i) for i in img_ids]
        imgs = [im for im in imgs if im]
        user_msg = q
        if imgs:
            user_msg = ([{"type": "text", "text": q}] if q else []) + \
                       [{"type": "image", "media_type": mt, "data": data} for (mt, data) in imgs]
        self._sse_open()
        if not q and not imgs:
            self._sse("done", {"session": sid, "answer": "", "error": "empty message"})
            return

        # Run controls are independent axes.  Keep the old `mode=` values as a compatibility
        # adapter, but canonicalize immediately so no later branch can accidentally treat a
        # workspace choice as a quality/verification choice again.
        legacy_mode = (qs.get("mode", ["normal"])[0] or "normal").strip().lower()
        if legacy_mode not in ("normal", "herding", "isolated", "pack"):
            self._sse("done", {"session": sid, "answer": "",
                               "error": "unknown legacy run mode: " + legacy_mode})
            return
        try:
            requested_opts = normalize_run_options(
                qs.get("intent", ["build"])[0],
                qs.get("quality", ["thorough" if legacy_mode == "herding" else "balanced"])[0],
                qs.get("verification", ["required" if legacy_mode == "herding" else "auto"])[0],
            )
        except ValueError as e:
            self._sse("done", {"session": sid, "answer": "", "error": str(e)})
            return
        strategy = (qs.get("strategy", ["pack" if legacy_mode == "pack" else "single"])[0]
                    or "single").strip().lower()
        workspace = (qs.get("workspace", ["isolated" if (
            legacy_mode == "isolated" or qs.get("isolate", ["0"])[0] in ("1", "true", "on")
        ) else "current"])[0] or "current").strip().lower()
        if strategy not in ("single", "pack"):
            self._sse("done", {"session": sid, "answer": "",
                               "error": "strategy must be single or pack"})
            return
        if workspace not in ("current", "isolated"):
            self._sse("done", {"session": sid, "answer": "",
                               "error": "workspace must be current or isolated"})
            return
        # No provider -> refuse here, in one legible frame. Everything below assumes a model: the
        # old default answered from mock's fixtures, and the run path would otherwise hand "" down
        # to make_provider to fail deeper and less clearly. The SSE headers are already committed,
        # so this goes out as a clean `done{error}` like every other pre-flight refusal.
        prov = _provider()
        if not prov:
            self._sse("done", {"session": sid, "answer": "", "error":
                               "no model configured — open Settings and choose a Provider "
                               "(a saved one did not reach this run)"})
            return

        # A transcript stopped at a model/turn boundary is safe to continue.  One stopped while an
        # external tool may have fired is not: loading and replaying that suffix can duplicate an
        # irreversible effect.  Recovery reconciliation is a separate, explicit API action.
        if qs.get("session", [""])[0]:
            recovery = sessions.recovery_state(sid)
            if recovery and recovery.get("recovery_required"):
                self._sse("done", {
                    "session": sid, "answer": "", "error": recovery.get("reason") or
                    "recovery required before this session can continue",
                    "recovery_required": True, "recovery": recovery,
                })
                return

        # seed the full prior thread so the web UI has the same --continue continuity the CLI has
        prior = sessions.load(sid) if qs.get("session", [""])[0] else None
        history = (prior or {}).get("messages") or []
        cwd = os.getcwd()

        # New clients send a non-empty sentinel ("none") when every axis is Auto. Older clients
        # predate Auto and expect any supplied query field to be literal, so preserve that contract.
        from .router import parse_explicit_axes, resolve_run_decision
        if "explicit_axes" in qs:
            explicit_axes = parse_explicit_axes(qs.get("explicit_axes", ["none"])[0])
        else:
            explicit_axes = parse_explicit_axes(
                [a for a in ("intent", "quality", "verification", "workspace", "strategy",
                             "effort", "speed") if a in qs])
        if legacy_mode == "herding":
            explicit_axes = parse_explicit_axes(list(explicit_axes) + ["quality", "verification"])
        elif legacy_mode == "isolated":
            explicit_axes = parse_explicit_axes(list(explicit_axes) + ["workspace"])
        elif legacy_mode == "pack":
            explicit_axes = parse_explicit_axes(list(explicit_axes) + ["strategy"])

        effort_request = qs.get("effort", [settings.get("REASONING_EFFORT", "auto") or "auto"])[0]
        speed_request = qs.get("speed", ["standard"])[0]
        configured_model = settings.get("MODEL", "") or None
        try:
            decision = resolve_run_decision(
                q, provider=prov, model=configured_model, effort=effort_request,
                speed=speed_request, route_kind=qs.get("route_kind", [""])[0],
                intent=requested_opts["intent"], quality=requested_opts["quality"],
                verification=requested_opts["verification"], workspace=workspace,
                strategy=strategy, explicit_axes=explicit_axes, history=history)
        except ValueError as e:
            self._sse("done", {"session": sid, "answer": "", "error": str(e)})
            return
        run_opts = {"intent": decision.intent, "quality": decision.quality,
                    "verification": decision.verification}
        strategy, workspace = decision.strategy, decision.workspace

        # Show/edit this proposal in the UI, then carry the exact command into Test/Required
        # evidence. API clients that omit it get the same deterministic detector.
        from .verification import detect_verification_commands
        verify_command = (qs.get("verify_command", [""])[0] or "").strip()
        verify_source = ((qs.get("verify_source", [""])[0] or "").strip()[:160]
                         if verify_command else "")
        if verify_command and not verify_source:
            verify_source = "user"
        detected_checks = detect_verification_commands(cwd)
        if not verify_command and detected_checks:
            verify_command = detected_checks[0]["command"]
            verify_source = detected_checks[0]["source"]

        readonly = run_opts["intent"] in ("plan", "review")
        if readonly:
            label = run_opts["intent"].title()
            if strategy != "single":
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "%s is read-only; Pack is only available for Build" % label})
                return
            if workspace != "current":
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "%s uses the current read-only workspace; isolation is only available for Build" % label})
                return
            if run_opts["verification"] != "auto":
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "%s does not edit code; Required verification is only available for Build" % label})
                return
        if run_opts["intent"] == "test":
            if strategy != "single" or workspace != "current":
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "Test is read-only and runs once in the current workspace"})
                return
            if not verify_command:
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "Test needs a detected or explicit verification command"})
                return
            test_gate = default_gate(cwd, mode="test", commands=[verify_command])
            if not test_gate.evaluate("bash", {"command": verify_command}).allowed:
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "Test command must be one allowlisted command without shell chaining"})
                return
        if strategy == "pack" and workspace == "isolated":
            self._sse("done", {"session": sid, "answer": "",
                               "error": "Pack already creates isolated attempts; choose Current workspace"})
            return
        if strategy == "pack" and run_opts["intent"] != "build":
            self._sse("done", {"session": sid, "answer": "",
                               "error": "Pack is only available for Build"})
            return

        pack_n, pack_check, pack_apply = 3, None, False
        if strategy == "pack":
            try:
                pack_n = int(qs.get("n", ["3"])[0] or 3)
            except ValueError:
                pack_n = 0
            if pack_n < 2 or pack_n > 6:
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "Pack attempts must be a whole number from 2 to 6"})
                return
            pack_check = (qs.get("check", [""])[0] or "").strip() or None
            if not pack_check:
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "Pack needs an executed check command to select a winner"})
                return
            pack_apply = qs.get("apply", ["0"])[0] in ("1", "on", "true")

        # ?isolate=1 — run this one in its own git worktree, on its own branch.
        #
        # Opt-in, and it has to stay opt-in: the ordinary expectation is that Collie edits the tree
        # you are looking at, and quietly moving those edits somewhere else would be the worst kind
        # of surprise. Ask for it when you want two runs on the same repo not to collide, or a
        # result you can read as a diff before it touches anything.
        #
        # If the directory is not a repository this REFUSES rather than falling back: falling back to
        # the shared tree is exactly the collision that was asked to be avoided, and saying nothing
        # about it is how you find out afterwards.
        wt_info = None
        if workspace == "isolated":
            from . import worktree as _wt
            wt_info = _wt.prepare(cwd, sid, label=q)
            if not wt_info["ok"]:
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "could not isolate this run: " + wt_info["error"]})
                return
            cwd = wt_info["dir"]

        run_id = Handler._run_begin(sid, q, cwd)
        if run_id is None:
            self._sse("done", {"session": sid, "answer": "",
                               "error": "this session already has an active run"})
            return

        def _tx(kind, data):
            # A run belongs to the server, not the initiating socket. Every lifecycle path uses this
            # guarded writer, including pack, so closing a window never strands a running registry row.
            try:
                self._sse(kind, data)
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                pass

        decision_payload = decision.to_dict()
        if verify_command:
            decision_payload["verification_proposal"] = {
                "command": verify_command, "source": verify_source,
            }
        start_d = {"session": sid, "run": run_id, "provider": prov, "cwd": cwd,
                   "prior_turns": sum(1 for m in history if m.get("role") == "user"),
                   "intent": run_opts["intent"], "quality": run_opts["quality"],
                   "verification": run_opts["verification"], "workspace": workspace,
                   "strategy": strategy, "model": decision.model,
                   "effort": decision.effort, "speed": decision.speed,
                   "decision": decision_payload}
        if wt_info:
            start_d["branch"] = wt_info["branch"]
            start_d["isolated"] = True
        _tx("start", start_d)
        Handler._live_pub("start", start_d)   # let open Maps enter live mode for this run
        Handler._mirror_pub(sid, "start", start_d)   # + any window mirroring this session

        # PACK mode (🎯 best-of-N): run the task N times in isolation, pick the winner by what
        # actually PASSES. No token stream — each attempt runs silently; we push a `pack_attempt`
        # event as each one lands, then a `done` carrying the winning answer + why it won.
        if strategy == "pack":
            from . import pack as _pack
            _tx("pack_start", {"n": pack_n, "check": pack_check,
                               "apply": pack_apply, "decision": decision_payload})

            def _emit(i, rec):
                _tx("pack_attempt", {
                    "idx": rec.get("idx", i), "verified": bool(rec.get("verified")),
                    "turns": rec.get("turns", 0), "error": (rec.get("error") or "")[:120],
                    "cost_usd": rec.get("cost_usd", 0.0), "check_pass": rec.get("check_pass"),
                    "provider": rec.get("provider"), "model": rec.get("model"),
                    "effort": rec.get("effort"), "speed": rec.get("speed"),
                    "verification_evidence": rec.get("verification_evidence")})
            try:
                pr = _pack.run_pack(user_msg, cwd, n=pack_n, check=pack_check, provider=prov,
                                    model=decision.model, effort=decision.effort,
                                    speed=decision.speed, apply=pack_apply, emit=_emit,
                                    cancel=lambda: Handler._run_cancelled(sid, run_id),
                                    quality=run_opts["quality"],
                                    verification=run_opts["verification"],
                                    # Pack has no single foreground approval card.  Give every
                                    # candidate the normal project gate anyway: local repo work is
                                    # allowed, anything external fails closed instead of running
                                    # ungated merely because this is a multi-candidate strategy.
                                    gate_factory=lambda attempt_cwd: default_gate(attempt_cwd),
                                    history=history)
            except Exception as e:
                error = "pack failed: %s: %s" % (type(e).__name__, e)
                try:
                    sessions.append_exchange(sid, user_msg, error, project="web", cwd=cwd)
                except Exception:
                    pass
                Handler._run_end(sid, error=error, run_id=run_id)
                done_d = {"session": sid, "run": run_id, "answer": "", "error": error,
                          "pack": True, "decision": decision_payload,
                          "model": decision.model, "effort": decision.effort,
                          "speed": decision.speed}
                try:
                    sessions.append_run_receipt(sid, {
                        "run": run_id, "decision": decision_payload,
                        "error": error, "pack": True,
                    })
                except Exception:
                    pass
                Handler._mirror_pub(sid, "done", done_d)
                Handler._live_pub("done", {"session": sid, "run": run_id, "error": error})
                _tx("done", done_d)
                return
            win = pr.get("winner")
            ans = pr.get("answer", "") if win is not None else ""
            canceled = bool(pr.get("canceled") or Handler._run_cancelled(sid, run_id))
            error = ("canceled by user" if canceled else
                     (("apply failed — " + pr.get("apply_error", ""))
                      if pack_apply and pr.get("apply_error") else
                      (None if win is not None else
                       ("no winner — " + pr.get("reason", "nothing passed")))))
            saved_answer = ("_[stopped by user]_" if canceled else
                            ((ans + "\n\n_[%s]_" % error) if ans and error else
                             (ans or error or "")))
            try:
                sessions.append_exchange(sid, user_msg, saved_answer, project="web", cwd=cwd)
            except Exception as e:
                error = error or "could not save pack history: %s: %s" % (type(e).__name__, e)
            turns = 0
            winner_rec = None
            if win is not None and 0 <= win < len(pr.get("attempts") or []):
                winner_rec = pr["attempts"][win]
                turns = winner_rec.get("turns", 0) or 0
            Handler._run_mark(sid, turns=turns,
                              verified=bool(winner_rec and winner_rec.get("verified")))
            Handler._run_end(sid, error=error or "", canceled=canceled, run_id=run_id)
            done_d = {
                "session": sid, "run": run_id, "answer": ans, "error": error,
                "canceled": canceled,
                "pack": True, "winner": win, "reason": pr.get("reason", ""),
                "applied": pr.get("applied", False), "attempts": pr.get("attempts", []),
                "n": pr.get("n"), "cost_usd": pr.get("total_cost_usd", 0.0),
                "model": (winner_rec or {}).get("model") or decision.model,
                "effort": decision.effort,
                "speed": decision.speed,
                "actual_speed": (winner_rec or {}).get("speed") or decision.speed,
                "decision": decision_payload,
                "verification_evidence": (winner_rec or {}).get("verification_evidence"),
                "subscription": prov in ("anthropic-oauth", "claude-cli", "codex-oauth",
                                           "codex-sub", "codex")}
            try:
                sessions.append_run_receipt(sid, {
                    "run": run_id, "decision": decision_payload, "pack": True,
                    "winner": win, "reason": pr.get("reason", ""),
                    "error": error or "", "model": done_d["model"],
                    "actual_speed": done_d["actual_speed"],
                    "verification_evidence": done_d["verification_evidence"],
                })
            except Exception:
                pass
            Handler._mirror_pub(sid, "done", done_d)
            Handler._live_pub("done", {"session": sid, "run": run_id, "turns": turns,
                                        "canceled": canceled})
            _tx("done", done_d)
            return

        h = None
        try:
            # build INSIDE the try: make_harness -> AnthropicOAuth can raise on a missing token
            # (the advertised real path), and the SSE headers are already committed — so a
            # provider error must arrive as a clean `done{error}` frame, not an escaped 500.
            # `prov` (not _provider()) — it is the provider this request already resolved and
            # reported in the frames above; re-reading it here could answer differently.
            gate_mode = (run_opts["intent"] if run_opts["intent"] in
                         ("plan", "review", "test") else None)
            h = make_harness(cwd, provider=prov, model=decision.model,
                             effort=decision.effort, speed=decision.speed,
                             project="web",
                             code_search=True, web_search=True, exec_code=True, delegate=True,
                             gate=default_gate(
                                 cwd, mode=gate_mode,
                                 commands=[verify_command] if gate_mode == "test" else None))
            configure_run_options(h, **run_opts)
            h.cancelled = lambda: Handler._run_cancelled(sid, run_id)
            h.checkpoint_scope = "web:" + sid
            # Desktop/live-wallpaper persona: collie here is the user's on-desktop assistant with a real
            # shell + the user's logged-in browser. Nudge it to ACT on local/system questions (time, tz,
            # hardware, status, location) via bash/powershell.exe instead of refusing for "lack of a tool".
            try:
                h.composer.identity = (
                    "You are collie, a focused coding agent running as the user's live desktop assistant. "
                    "Use tools to gather facts before answering; be concise and correct. "
                    "You have a real shell (bash) and, on this machine (WSL under Windows), can call "
                    "powershell.exe to reach the Windows host. For anything about the local machine — "
                    "current time, timezone, hardware/spec, OS, battery or status, network or approximate "
                    "location — just RUN the command (date, timedatectl, `powershell.exe Get-ComputerInfo`, "
                    "`powershell.exe Get-TimeZone`, `curl -s ipinfo.io`, etc.) rather than saying you lack "
                    "permission. You also drive the user's real logged-in browser via the browser_* tools. "
                    "Do NOT preface your work with what you are about to do (no 'let me check', no 'I'll look "
                    "into it') — just do it, then give the result directly and concisely."
                )
                if run_opts["intent"] == "plan":
                    h.composer.identity += (
                        " For this Plan run, write the final editable artifact through the plan tool "
                        "with title, files, risks, checks, and todos before answering. Do not approve it."
                    )
                elif run_opts["intent"] == "review":
                    h.composer.identity += (
                        " For this Review run, end with one fenced JSON object shaped as "
                        "{\"findings\":[{\"path\":\"...\",\"line\":1,\"severity\":\"high|medium|low\","
                        "\"message\":\"...\"}]}. Report an empty findings array when there are no issues."
                    )
            except Exception:
                pass
            # every structural event hits BOTH the starting client's socket and the live bus (so the
            # Map / mini-map render it in real time); the token firehose stays client-only.
            # The run does not belong to the socket that started it.
            #
            # `self._sse` was called FIRST and unguarded here, so the moment the browser went away —
            # closed tab, switched thread, phone locked — the next emit raised BrokenPipeError, which
            # travelled out of h.run() into the handler below and ended the run. A comment in the web
            # UI said the opposite ("a dropped SSE connection does NOT stop the run") and built a
            # whole reconnect path on top of that belief; what it reconnected to was a corpse.
            #
            # Writing to the departed client is the only part allowed to fail. The live bus and the
            # session mirror always get the event, which is what lets a window re-attach later, and
            # what makes leaving a run to start another one possible at all.
            h.emit = lambda kind, d: (_tx(kind, d), Handler._live_pub(kind, d),
                                      Handler._mirror_pub(sid, kind, d))
            h.stream_cb = lambda piece: (_tx("token", {"t": piece}),
                                         Handler._mirror_pub(sid, "token", {"t": piece}))  # real token streaming
            # Approvals. The card is driven by the Inbox ITEM, not by a separate event type:
            # one record means a question answered on the phone closes the card in the browser
            # and the other way round, and whoever answers first is the one that counts.
            # It rides the mirror as well as the socket, so re-attaching a window after a
            # dropped connection re-delivers a still-pending question instead of losing it.
            from .inbox import VIS_INLINE, InboxStore, inbox_approver
            def _permission_new(it):
                payload = _perm(it)
                _tx("permission", payload)
                Handler._mirror_pub(sid, "permission", payload)
                Handler._live_pub("permission", dict(payload, session=sid))
                Handler._notify_waiting(sid, it)
            inbox = InboxStore(on_new=_permission_new)
            Handler._inbox_open(sid, inbox)
            h.approve = inbox_approver(inbox, sid, visibility=VIS_INLINE)
            # mid-run steering: register a per-session queue; the loop drains it at each turn boundary.
            # POST /api/steer pushes onto it. Text typed while Collie works becomes the next user turn.
            steer_q = Handler._steer_open(sid)
            def _drain_steer():
                out = []
                while True:
                    try:
                        out.append(steer_q.get_nowait())
                    except queue.Empty:
                        break
                return out
            h.steering = _drain_steer
            # HEARTBEAT: h.run is synchronous, so during a silent gap (a slow tool, then the next
            # turn's time-to-first-token) NO bytes cross the SSE socket. On flaky forwarders (WSL2
            # localhost, some proxies) an idle connection gets dropped mid-run -> the browser shows
            # "stream interrupted" even though the run finished and the answer was saved. A daemon
            # thread pings every 10s (serialized with the run's writes via self._wlock) to keep the
            # connection warm. It stops the instant the socket breaks or the run ends.
            self._wlock = threading.Lock()
            stop_hb = threading.Event()

            def _heartbeat():
                while not stop_hb.wait(10):
                    try:
                        self._sse("ping", {})
                    except Exception:
                        break                  # socket gone — stop pinging (the run's own writes will end it)
            hb = threading.Thread(target=_heartbeat, daemon=True)
            hb.start()
            try:
                res = h.run("web", user_msg, consolidate=True, history=history)
            finally:
                stop_hb.set()                  # end the heartbeat before we send `done`
            canceled = bool(getattr(res, "canceled", False)
                            or Handler._run_cancelled(sid, run_id))
            verification_evidence = None
            should_check = (run_opts["intent"] == "test" or
                            run_opts["verification"] == "required")
            if should_check and verify_command and not canceled:
                from .verification import run_verification_command
                verification_evidence = run_verification_command(
                    verify_command, cwd, source=verify_source or "detected",
                    after_last_edit=True)
                evidence_event = {"session": sid, "run": run_id,
                                  "evidence": verification_evidence}
                _tx("verification_evidence", evidence_event)
                Handler._live_pub("verification_evidence", evidence_event)
                Handler._mirror_pub(sid, "verification_evidence", evidence_event)
                if run_opts["intent"] == "test":
                    res.verified = bool(verification_evidence["passed"])
                elif not verification_evidence["passed"]:
                    res.verified = False
                    check_error = "required check failed: %s (exit %s)" % (
                        verify_command, verification_evidence.get("exit_code"))
                    res.error = ((res.error + "; ") if res.error else "") + check_error
            sessions.save(sid, res.messages, project="web", cwd=cwd, answer=res.answer or "")
            Handler._live_pub("done", {"session": sid, "run": run_id,
                                        "turns": res.turns, "canceled": canceled})
            actual_speed = getattr(getattr(h, "provider", None), "actual_speed", decision.speed)
            done_d = {
                "session": sid, "run": run_id, "answer": res.answer or "", "error": res.error,
                "canceled": canceled,
                "model": res.model, "prefix_tokens": res.prefix_tokens,
                "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                "total_tokens": res.total_tokens, "turns": res.turns,
                "max_turns": getattr(h, "max_turns", None),
                "tool_calls": res.tool_calls, "wall_ms": res.wall_ms,
                "cost_usd": res.cost_usd,
                "effort": decision.effort, "speed": decision.speed,
                "actual_speed": actual_speed, "decision": decision_payload,
                "verification_evidence": verification_evidence,
                # flat-subscription paths draw a fixed bucket, so the real charge is $0 — cost_usd is
                # only a per-token ESTIMATE of what it'd cost on the metered API. Flag it so the UI
                # doesn't present the estimate as a real charge.
                "subscription": prov in ("anthropic-oauth", "claude-cli", "codex-oauth",
                                           "codex-sub", "codex")}
            review_findings = (_review_findings(res.answer or "")
                               if run_opts["intent"] == "review" else None)
            if review_findings is not None:
                done_d["review_findings"] = review_findings
            # An isolated run's result is a branch, not a claim. Say which one, and what is on it,
            # so the next step is `git diff` rather than "did anything happen?".
            if wt_info:
                from . import worktree as _wt
                st = _wt.status(wt_info["dir"])
                done_d["branch"] = wt_info["branch"]
                done_d["isolated"] = True
                done_d["changed"] = len(st["files"])
                done_d["worktree"] = wt_info["dir"]

            try:
                sessions.append_run_receipt(sid, {
                    "run": run_id, "decision": decision_payload,
                    "model": res.model, "effort": decision.effort,
                    "requested_speed": decision.speed, "actual_speed": actual_speed,
                    "verified": bool(getattr(res, "verified", False)),
                    "verification_evidence": verification_evidence,
                    "error": res.error or "", "canceled": canceled,
                    "review_findings": review_findings,
                })
            except Exception:
                pass

            # Record the verdict BEFORE trying to tell the client, and let telling it fail. A run
            # whose browser had gone away finished its work, saved its answer, and was then filed as
            # a failure — because this frame went straight to a dead socket and the BrokenPipeError
            # jumped over the line that stored the result. The work was real; only the audience left.
            Handler._run_end(sid, res, canceled=canceled, run_id=run_id)
            Handler._mirror_pub(sid, "done", done_d)   # mirroring windows see the run finish too
            if not canceled:
                Handler._notify_done(sid, res, wall_ms=res.wall_ms)
            _tx("done", done_d)
        except BrokenPipeError:
            # Only reachable now from a write outside h.emit; the run's own emits swallow it.
            Handler._run_end(sid, error="client went away", run_id=run_id)
        except Exception as e:
            error = "%s: %s" % (type(e).__name__, e)
            Handler._run_end(sid, error=error, run_id=run_id)
            _tx("done", {"session": sid, "run": run_id, "answer": "", "error": error,
                         "canceled": Handler._run_cancelled(sid, run_id),
                         "model": decision.model, "effort": decision.effort,
                         "speed": decision.speed, "decision": decision_payload})
            try:
                sessions.append_run_receipt(sid, {
                    "run": run_id, "decision": decision_payload, "error": error,
                    "canceled": Handler._run_cancelled(sid, run_id),
                })
            except Exception:
                pass
            # A run that CRASHED is the one most worth being told about, and it never reaches the
            # success path above — so notify from here too.
            try:
                if REMOTE is not None:
                    REMOTE.notify("Run failed", "%s: %s" % (type(e).__name__, e),
                                  session=sid, thread=sid)
            except Exception:
                pass
        finally:
            # Whatever happened, nothing may stay listed as running — a sidebar that shows a dot
            # forever is worse than one that shows nothing, because it is believed.
            Handler._run_end(sid, error="ended without a verdict", run_id=run_id)
            Handler._steer_close(sid)      # run over: reject further steers for this session
            Handler._inbox_close(sid)      # and close any question its run can no longer act on
            if h is not None:
                try:
                    h.memory.close(); h.recorder.close()
                except Exception:
                    pass


def bind_server(port=8787):
    """Bind the local GUI server on 127.0.0.1, scanning a few ports if the preferred one is busy.
    Returns (httpd, actual_port). Used by `collie web --remote`, which needs the httpd + chosen port
    up front (to serve in a background thread while the relay client runs), and which always wants
    loopback — the relay client replays a phone's requests to 127.0.0.1. main() has its own inline
    bind because `--lan` can widen it to 0.0.0.0; the two are otherwise the same."""
    ThreadingHTTPServer.allow_reuse_address = True
    for cand in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            return httpd, cand
        except OSError as e:
            if e.errno in (98, 48, 10048):     # in use: Linux 98 / macOS 48 / Windows 10048
                continue
            raise
    raise OSError("ports %d–%d are all in use" % (port, port + 11))


def main(argv=None, on_bound=None):
    """Serve the web UI. `on_bound(port)` fires once the socket is up, with the port ACTUALLY
    bound — which is not always the one asked for, since a busy port makes this scan forward.
    A caller that needs to point something at the server (the native app window) has no other
    way to learn where it landed."""
    argv = list(sys.argv[1:] if argv is None else argv)
    port = 8787
    open_browser = True
    lan = False
    want_qr = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2; continue
        if a == "--no-open":
            open_browser = False; i += 1; continue
        if a == "--lan":
            lan = True; i += 1; continue
        if a == "--qr":
            want_qr = True; i += 1; continue
        if a == "--name" and i + 1 < len(argv):
            global DOG_NAME
            DOG_NAME = argv[i + 1]; i += 2; continue
        i += 1

    if not os.path.exists(INDEX_HTML):
        print("warning: %s not found — GET / will 500 until it exists" % INDEX_HTML)

    # Bind gracefully: if the port is taken (a stale `collie web`, or the user re-launching),
    # try the next few ports instead of crashing with a raw traceback. allow_reuse_address so a
    # just-closed server's TIME_WAIT socket doesn't block an immediate restart.
    ThreadingHTTPServer.allow_reuse_address = True
    requested = port
    httpd = None
    # Default: loopback only — nothing on the network can even connect. `--lan` is the opt-in a phone
    # needs (CollieIOS talks straight to this server), and it also teaches _host_ok this machine's own
    # addresses, since a phone necessarily sends the LAN IP as Host.
    bind = "0.0.0.0" if lan else "127.0.0.1"
    lan_ips = _own_ipv4() if lan else []
    LAN_HOSTS.update(lan_ips)
    for cand in range(requested, requested + 12):
        try:
            httpd = ThreadingHTTPServer((bind, cand), Handler)
            port = cand
            break
        except OSError as e:
            # address already in use / access denied → try the next port. Linux errno 98, macOS 48;
            # on Windows this arrives as PermissionError(errno=13)/EADDRINUSE with the real code in
            # .winerror (10048 WSAEADDRINUSE, 10013 WSAEACCES exclusive), so match winerror too — else
            # the server crashed on a busy port on Windows and `collie app` pointed at a dead port.
            if e.errno in (98, 48) or getattr(e, "winerror", None) in (10048, 10013):
                continue
            raise
    if httpd is None:
        print("error: ports %d–%d are all in use. Is `collie web` already running? "
              "Open http://127.0.0.1:%d/ , or pass --port <free port>." % (requested, requested + 11, requested))
        return 1
    start_mission_ticker()
    # a nicer local URL than a bare loopback IP: browsers resolve any *.localhost name to the
    # loopback address per RFC 6761 (zero setup, no /etc/hosts), so collie.localhost:PORT works
    # out of the box while the server still binds 127.0.0.1. VS Code parses the 127.0.0.1 line below.
    # Install the player reaper HERE: signal handlers can only be set from the main thread, and the
    # request that starts music runs on an HTTP worker — so doing it there silently did nothing and
    # the music outlived collie with no way left to stop it.
    try:
        from . import desktop as _dt
        _dt._install_reaper()
    except Exception:
        pass

    if on_bound:
        try:
            on_bound(port)
        except Exception:
            pass                       # a caller's bookkeeping must never take the server down
    url = "http://collie.localhost:%d/" % port
    ip_url = "http://127.0.0.1:%d/" % port
    note = "" if port == requested else "  (%d was busy → auto-picked %d)" % (requested, port)
    # print BOTH: the pretty one for humans, the 127.0.0.1 one so the VS Code extension's regex finds a port.
    print("collie web · %s · provider=%s · Ctrl-C to stop%s"
          % (url, _provider() or "(not configured)", note), flush=True)
    print("            %s" % ip_url, flush=True)
    # Remote is a first-class, Collie-managed capability: if the user turned it on (Settings/panel),
    # it starts automatically whenever the web server runs — no separate process, no --remote flag.
    try:
        from . import settings as _settings
        if _settings.get("REMOTE") == "on":
            _ensure_remote(port).start()
            print("collie remote · on (setting) · panel %s remote" % url, flush=True)
    except Exception as e:                       # never let remote block the normal web server
        print("collie remote: auto-start failed: %s" % e, flush=True)

    if lan:
        for ip in lan_ips:
            print("            http://%s:%d/   ← this device is reachable on your network" % (ip, port),
                  flush=True)
        print("  [lan] network clients get NOTHING until they pair: every route needs the token, and "
              "the token is only handed to loopback. Pair by showing the code below to the app.",
              flush=True)
        from . import plat
        if plat.is_macos() and _macos_firewall_on():
            print("  [lan] macOS's firewall is ON, so it will silently drop these incoming "
                  "connections until you allow python: System Settings → Network → Firewall → "
                  "Options, or turn the firewall off while you pair.", flush=True)
        _print_pair_hint(lan_ips[0] if lan_ips else "127.0.0.1", port, want_qr)
    if open_browser:
        # open the browser a beat after the server is actually accepting connections
        threading.Timer(0.6, lambda: _open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


def _print_pair_hint(ip, port, want_qr):
    """Tell the user how to pair a phone. The pairing CODE is never printed with a token in it: it
    carries a one-shot secret that /api/pair trades for the token, so a photo of your terminal (or a
    screen share) is worth nothing a minute later.

    Default is the collie pair code, drawn on the /pair screen — a private format no generic scanner
    reads. `--qr` is the fallback for when a camera can't manage the ring code; it encodes the same
    one-shot secret as a collie:// URL, which only CollieIOS can act on."""
    print("\n  pair the phone app (CollieIOS): open  http://127.0.0.1:%d/pair" % port, flush=True)
    if not want_qr:
        return
    secret = _pair_mint()
    pair_url = "collie://pair?h=%s&p=%d&s=%s" % (ip, port, secret)
    try:
        from . import qr
        code = qr.ansi(pair_url)
    except Exception as e:                       # a fallback code is a convenience, never a blocker
        print("  [qr] unavailable (%s); use the /pair screen" % e, flush=True)
        return
    print("\n  fallback code (valid %ds, one use):\n" % _PAIR_TTL, flush=True)
    print(code, flush=True)


def _macos_firewall_on():
    """True when macOS's application firewall is enabled — it drops inbound connections to an
    unlisted python, which looks exactly like `--lan` not working. Best-effort; never raises."""
    try:
        import subprocess
        out = subprocess.run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"],
                             capture_output=True, text=True, timeout=4).stdout
        return "State = 1" in out or "enabled" in out.lower()
    except Exception:
        return False


def _own_ipv4():
    """This machine's own LAN IPv4 addresses, for `--lan`'s Host allow-list. The UDP-connect trick
    gets the address the default route would use without sending a packet; hostname resolution adds
    any others. No third-party deps, and a failure just yields fewer allowed hosts."""
    import socket
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    return sorted({ip for ip in ips if ip and not ip.startswith("127.")})


def _open(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
