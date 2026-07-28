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

import json
import os
import queue
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
        return "Stopped."
    return "Done."


def _play_summary(r):
    if not r.get("ok"):
        return "Couldn\u2019t find that%s" % (" \u2014 " + r["error"] if r.get("error") else ".")
    who = r.get("uploader") or ""
    return "\u25b6 Playing %s%s." % (r.get("title") or "it", " \u2014 " + who if who else "")


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
    """Zero-config stays mock ($0 local dev): settings.apply() runs per query, so a Provider
    saved in the Settings panel (default choice: anthropic API) lands in the env and wins here;
    only a fresh install with nothing saved falls through to mock."""
    return os.environ.get("COLLIE_PROVIDER", "mock")


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
    def _send_html(self, body: bytes, code: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, 403)
        if not self._peer_ok(parsed):
            return self._send_json({"error": "pairing required"}, 403)
        try:
            if path in ("/", "/index.html"):
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
                return self._send_json({"schema": settings.SCHEMA, "values": vals})
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
                return self._send_json({"entries": entries, "current": current})
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
                return self._send_json({"track": dt.nowplaying()})
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
            # Missions are disabled (the router rewrites mission->chat). Enforce that HERE too — not
            # only in router+UI — so the endpoints can't be driven directly. Delete to re-enable.
            if path in ("/api/mission", "/api/missions"):
                return self._send_json({"error": "missions are disabled"}, 404)
            if path == "/api/mission":                    # delegate: one mission's live status
                from .missionweb import MissionService
                mid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                svc = MissionService()
                try:
                    return self._send_json(svc.status(mid) if mid else {"error": "id required"})
                finally:
                    svc.close()
            if path == "/api/missions":                   # delegate: the mission list (sidebar)
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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, 403)
        if not self._peer_ok(parsed):
            return self._send_json({"error": "pairing required"}, 403)
        try:
            if path == "/api/pair":
                return self._serve_pair_exchange()
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
                saved = settings.update(body)
                settings.apply()                              # take effect for the next query now
                # Ambient-desktop autostart is USER-controlled: toggling WALLPAPER creates/removes the
                # logon launcher — install() also starts it now, uninstall() stops it. Only on a change.
                if "WALLPAPER" in body:
                    want_wp = str(body.get("WALLPAPER") or "").lower() in ("on", "1", "true")
                    if want_wp != prev_wp:
                        try:
                            from . import wallpaper as wp
                            wp.install() if want_wp else wp.uninstall()
                        except Exception:
                            pass
                return self._send_json({"ok": True, "values": settings.all_values(), "saved": saved})
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
                provider, model = catalog.resolve((body or {}).get("id", ""))
                if not provider:
                    return self._send_json({"error": "bad model id"}, 400)
                partial = {"PROVIDER": provider}
                if model:
                    partial["MODEL"] = model
                settings.update(partial)
                settings.apply()
                return self._send_json({"ok": True, "provider": provider, "model": model or ""})
            if path in ("/api/mission", "/api/mission/confirm", "/api/mission/resume",
                        "/api/mission/tick"):
                # Missions are disabled (the router rewrites mission->chat). Enforce it server-side so
                # a CSRF-token holder still can't start a durable model-driven campaign the product
                # says is impossible. Delete this guard (and the router rewrite) to re-enable.
                return self._send_json({"error": "missions are disabled"}, 404)
            if path in ("/api/_mission_disabled",):
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
                        bounds = {}
                        if body.get("price_floor") is not None:
                            bounds["price_floor"] = body["price_floor"]
                        return self._send_json(svc.start(
                            goal, autonomous=bool(body.get("autonomous")), **bounds))
                    mid = (body.get("id") or "").strip()
                    if not mid:
                        return self._send_json({"error": "id required"}, 400)
                    if path == "/api/mission/confirm":
                        return self._send_json(svc.confirm(mid, (body.get("nonce") or "").strip()))
                    if path == "/api/mission/resume":
                        return self._send_json(svc.resume(mid))
                    return self._send_json(svc.tick(mid))     # /api/mission/tick
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
                    _name = os.environ.get("COLLIE_PROVIDER", "mock")
                    _rmodel = os.environ.get("COLLIE_ROUTER_MODEL") or (
                        DEFAULT_ROUTER_MODEL if _name in ("anthropic-oauth", "anthropic") else None)
                    prov = make_provider(_name, _rmodel)
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

    _TREE_CACHE: dict = {}
    def _serve_tree(self, qs=None):
        """GET /api/tree[?repo=ABS] -> a project's code galaxy (files with loc/defs/names/imports).
        `repo` (must be a git repo under the user's home) picks any discovered project; default = the
        server's cwd. Cached on the dir's mtime so repeated Map loads don't re-walk the tree."""
        from . import codemap
        cwd = os.getcwd()
        repo = ((qs or {}).get("repo", [""])[0] or "").strip()
        if repo:
            home = os.path.realpath(os.path.expanduser("~"))
            cand = os.path.realpath(os.path.expanduser(repo))
            # only a real git repo under home may be mapped (never an arbitrary path)
            if (cand == home or cand.startswith(home + os.sep)) and codemap.git_root(cand) == cand:
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
    def _serve_repos(self):
        """GET /api/repos -> git projects under the user's home, one galaxy each."""
        from . import codemap
        home = os.path.expanduser("~")
        if "repos" not in Handler._REPOS_CACHE:
            Handler._REPOS_CACHE["repos"] = codemap.discover_repos(home)
        self._send_json({"cwd": os.getcwd(), "repos": Handler._REPOS_CACHE["repos"]})

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
        from .cli import make_harness
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

        # seed the full prior thread so the web UI has the same --continue continuity the CLI has
        prior = sessions.load(sid) if qs.get("session", [""])[0] else None
        history = (prior or {}).get("messages") or []
        cwd = os.getcwd()
        start_d = {"session": sid, "provider": _provider(), "cwd": cwd,
                   "prior_turns": sum(1 for m in history if m.get("role") == "user")}
        self._sse("start", start_d)
        Handler._live_pub("start", start_d)   # let open Maps enter live mode for this run
        Handler._mirror_pub(sid, "start", start_d)   # + any window mirroring this session

        # PACK mode (🎯 best-of-N): run the task N times in isolation, pick the winner by what
        # actually PASSES. No token stream — each attempt runs silently; we push a `pack_attempt`
        # event as each one lands, then a `done` carrying the winning answer + why it won.
        if (qs.get("mode", ["normal"])[0]) == "pack":
            from . import pack as _pack
            try:
                n = int(qs.get("n", ["3"])[0] or 3)
            except ValueError:
                n = 3
            check = (qs.get("check", [""])[0] or "").strip() or None
            apply_ = qs.get("apply", ["0"])[0] in ("1", "on", "true")
            self._sse("pack_start", {"n": max(1, min(8, n)), "check": check or "", "apply": apply_})

            def _emit(i, rec):
                self._sse("pack_attempt", {
                    "idx": rec.get("idx", i), "verified": bool(rec.get("verified")),
                    "turns": rec.get("turns", 0), "error": (rec.get("error") or "")[:120],
                    "cost_usd": rec.get("cost_usd", 0.0), "check_pass": rec.get("check_pass")})
            try:
                pr = _pack.run_pack(q, cwd, n=n, check=check, provider=_provider(),
                                    apply=apply_, emit=_emit)
            except BrokenPipeError:
                return
            except Exception as e:
                self._sse("done", {"session": sid, "answer": "", "error": "pack failed: %s" % e})
                return
            win = pr.get("winner")
            ans = pr.get("answer", "") if win is not None else ""
            if ans:
                sessions.save(sid, [{"role": "user", "content": q},
                                    {"role": "assistant", "content": ans}],
                              project="web", cwd=cwd, answer=ans)
            self._sse("done", {
                "session": sid, "answer": ans,
                "error": None if win is not None else ("no winner — " + pr.get("reason", "nothing passed")),
                "pack": True, "winner": win, "reason": pr.get("reason", ""),
                "applied": pr.get("applied", False), "attempts": pr.get("attempts", []),
                "n": pr.get("n"), "cost_usd": pr.get("total_cost_usd", 0.0),
                "subscription": _provider() in ("anthropic-oauth", "claude-cli")})
            return

        h = None
        try:
            # build INSIDE the try: make_harness -> AnthropicOAuth can raise on a missing token
            # (the advertised real path), and the SSE headers are already committed — so a
            # provider error must arrive as a clean `done{error}` frame, not an escaped 500.
            h = make_harness(cwd, provider=_provider(), project="web",
                             code_search=True, web_search=True, exec_code=True, delegate=True)
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
            except Exception:
                pass
            # run mode: "herding" (🐕 Extreme Herding) pushes harder — more turns + the executed
            # assert-verify gate on (won't finish until a reproduction prints green).
            # Turn ceilings sit well above typical need: session 10e8 (2026-07-14) exhausted the old
            # 10-turn normal cap mid info-hunt and shipped its half-way plan as the "answer".
            # Subscription providers (anthropic-oauth / claude-cli) make extra turns cost $0.
            if (qs.get("mode", ["normal"])[0]) == "herding":
                h.max_turns = max(h.max_turns, 48)   # never shallower than 48; a bigger panel value wins
                h.verify_gate = True
                h.require_assert = True
                if hasattr(h, "verify_max"):
                    h.verify_max = 4
            elif not settings.get("MAX_TURNS"):
                h.max_turns = 40                     # raised default; the Settings panel / env still wins
            # every structural event hits BOTH the starting client's socket and the live bus (so the
            # Map / mini-map render it in real time); the token firehose stays client-only.
            h.emit = lambda kind, d: (self._sse(kind, d), Handler._live_pub(kind, d),
                                      Handler._mirror_pub(sid, kind, d))
            h.stream_cb = lambda piece: (self._sse("token", {"t": piece}),
                                         Handler._mirror_pub(sid, "token", {"t": piece}))  # real token streaming
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
            sessions.save(sid, res.messages, project="web", cwd=cwd, answer=res.answer or "")
            Handler._live_pub("done", {"session": sid, "turns": res.turns})   # live map: run finished
            done_d = {
                "session": sid, "answer": res.answer or "", "error": res.error,
                "model": res.model, "prefix_tokens": res.prefix_tokens,
                "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                "total_tokens": res.total_tokens, "turns": res.turns,
                "max_turns": getattr(h, "max_turns", None),
                "tool_calls": res.tool_calls, "wall_ms": res.wall_ms,
                "cost_usd": res.cost_usd,
                # flat-subscription paths draw a fixed bucket, so the real charge is $0 — cost_usd is
                # only a per-token ESTIMATE of what it'd cost on the metered API. Flag it so the UI
                # doesn't present the estimate as a real charge.
                "subscription": _provider() in ("anthropic-oauth", "claude-cli")}
            self._sse("done", done_d)
            Handler._mirror_pub(sid, "done", done_d)   # mirroring windows see the run finish too
            Handler._notify_done(sid, res, wall_ms=res.wall_ms)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "%s: %s" % (type(e).__name__, e)})
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
            Handler._steer_close(sid)      # run over: reject further steers for this session
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


def main(argv=None):
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
            if e.errno in (98, 48):        # address already in use (Linux 98 / macOS 48)
                continue
            raise
    if httpd is None:
        print("error: ports %d–%d are all in use. Is `collie web` already running? "
              "Open http://127.0.0.1:%d/ , or pass --port <free port>." % (requested, requested + 11, requested))
        return 1
    # a nicer local URL than a bare loopback IP: browsers resolve any *.localhost name to the
    # loopback address per RFC 6761 (zero setup, no /etc/hosts), so collie.localhost:PORT works
    # out of the box while the server still binds 127.0.0.1. VS Code parses the 127.0.0.1 line below.
    url = "http://collie.localhost:%d/" % port
    ip_url = "http://127.0.0.1:%d/" % port
    note = "" if port == requested else "  (%d was busy → auto-picked %d)" % (requested, port)
    # print BOTH: the pretty one for humans, the 127.0.0.1 one so the VS Code extension's regex finds a port.
    print("collie web · %s · provider=%s · Ctrl-C to stop%s" % (url, _provider(), note), flush=True)
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
