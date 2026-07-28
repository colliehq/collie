"""Collie Remote — the desktop side of "control your desktop Collie from your phone".

Topology (see docs design): the desktop is behind NAT, so it dials *out* to a public relay
(a Cloudflare Worker at collie.run) over one WebSocket; the phone talks to the same relay over
HTTPS; the relay multiplexes the phone's HTTP requests onto the agent WS.

This module is the relay *client*. The elegant part: it is just a **local client of our own
127.0.0.1 web server** — it replays each incoming request to http://127.0.0.1:<port>/... with the
per-process CSRF TOKEN injected. So webapp.py's `_host_ok` (Host is loopback) and `_authed` (token
present) are satisfied untouched, and the local security model keeps holding. The local TOKEN never
leaves the machine; the phone authenticates one layer up, at the relay.

E2E (opt-in, per device): a phone can pair with an X25519 public key plus an HMAC over the transcript
keyed by the pairing code. The relay cannot check that tag — it does not know the code — so it forwards
it here; we verify it, answer with our own key and tag, and derive a per-device key. From then on that
device's requests arrive sealed and its responses go back sealed, so a HOSTED relay routes bytes it
cannot read. Plaintext clients keep working unchanged; see harness/e2e.py and relay/E2E_DESIGN.md.
"""
from __future__ import annotations

import base64
import http.client
import json
import threading
import time
import urllib.parse

from .wsclient import WebSocketClient, WebSocketClosed

# hop-by-hop / connection-management headers we must NOT forward to the local server (RFC 7230 §6.1),
# plus Host (we set our own loopback Host) and content-length (http.client recomputes from body).
_DROP_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
}

# read the local response in modest chunks so an SSE stream is relayed frame-by-frame as it is
# produced (http.client's read(n) returns whatever has arrived), not buffered until the run ends.
_CHUNK = 2048


class RelayClient:
    def __init__(self, relay_url: str, identity, paircode: str,
                 local_host: str, local_port: int, local_token: str, logf=None):
        self.relay_url = relay_url.rstrip("/")
        self.identity = identity          # harness.remote_identity.Identity (durable device store)
        self.room = identity.room
        self.agent_key = identity.agent_key
        self.paircode = paircode
        self.local_host = local_host
        self.local_port = local_port
        self.local_token = local_token
        self._log = logf or (lambda *a: None)
        self._ws: WebSocketClient | None = None
        self._stop = False
        self.connected = threading.Event()   # set once the agent socket is actually up
        self.last_error = None               # why the last attempt failed, for the CLI to report
        self.on_pair = None                  # set by Remote: rotate the paircode after a device pairs (one-shot)
        # A device waiting on a human. The relay holds the pairing until _reply_pair answers, so this
        # is at most one at a time — a second request while one is pending would be exactly the
        # confusion the number on both screens exists to prevent.
        self.pending_pair = None             # {id, num, device_id, name, at, ws}
        self.approved_devices = set(identity.approved_ids())
        # E2E state. The keypair is per process: a restart re-pairs any E2E device, which is the
        # honest tradeoff until the device store persists K_dev (E2E_DESIGN.md §7).
        self._e2e_keys = self._make_e2e_keys()   # (private, public), advertised in `hello`
        self._e2e_devices = {}               # device_id -> K_dev
        self._e2e_seq = {}                   # (device_id, rid) -> next outbound seq

    # ------------------------------------------------------------------ lifecycle
    def run_forever(self):
        """Connect, serve, and reconnect with exponential backoff until stop()."""
        backoff = 1.0
        while not self._stop:
            try:
                self._connect_and_serve()
                self.last_error = None
                backoff = 1.0
            except WebSocketClosed:
                self.connected.clear()
                self._log("relay: connection closed")
            except Exception as e:                       # noqa: BLE001 — keep the loop alive
                self.last_error = e
                self._log("relay: error: %s" % e)
            if self._stop:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def stop(self):
        self._stop = True
        if self._ws:
            self._ws.close()

    def refresh_devices(self):
        """Push the current paired-device hash set to the relay (after a kick), so a kicked device
        loses access immediately without waiting for a reconnect."""
        ws = self._ws
        if ws is not None:
            try:
                ws.send_text(json.dumps({"t": "devices", "devices": self.identity.device_hashes()}))
            except Exception:
                pass

    def refresh_paircode(self):
        """Tell the relay the pairing code rotated, so the old code stops working right away."""
        ws = self._ws
        if ws is not None:
            try:
                ws.send_text(json.dumps({"t": "paircode", "paircode": self.paircode}))
            except Exception:
                pass

    def _reply_pair(self, ws, rid, ok, error=""):
        try:
            ws.send_text(json.dumps({"t": "pair_decision", "id": rid, "ok": bool(ok),
                                     "error": error}))
        except Exception:
            pass

    def _connect_and_serve(self):
        q = urllib.parse.urlencode({"room": self.room, "key": self.agent_key})
        url = "%s/relay/agent?%s" % (self.relay_url, q)
        self._log("relay: connecting %s (room=%s)" % (self.relay_url, self.room))
        ws = WebSocketClient.connect(url)
        self._ws = ws
        self.connected.set()
        # announce ourselves. The desktop is the source of truth for pairing: we hand the relay the
        # AGENTKEY (proves we own this room), the current pairing code (for NEW devices), and the set
        # of already-paired device-token hashes (so RETURNING phones validate without re-pairing —
        # this is what makes pairing survive a desktop restart / 24h offline).
        ws.send_text(json.dumps({
            "t": "hello", "v": 1, "room": self.room, "agentKey": self.agent_key,
            "e2ePub": self.e2e_public_b64(),
            "paircode": self.paircode, "devices": self.identity.device_hashes(),
            # Tell the relay we can answer pair_request, so it will hold a new device until this
            # desktop says yes. Declared rather than assumed: a relay that demanded approval from
            # every desktop would lock out every install that predates this message.
            "approve": True,
        }))
        self._log("relay: connected")
        stop_ka = self._start_keepalive(ws)
        try:
            self._pending_bodies: dict = {}              # id -> bytearray, for requests with a body
            while not self._stop:
                kind, data = ws.recv_message()
                if kind != "text":
                    continue
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                self._dispatch(ws, msg)
        finally:
            stop_ka.set()
            ws.close()
            self._ws = None

    def _start_keepalive(self, ws) -> threading.Event:
        stop = threading.Event()

        def beat():
            while not stop.wait(20.0):
                try:
                    ws.send_ping()
                except Exception:
                    return
        threading.Thread(target=beat, name="relay-keepalive", daemon=True).start()
        return stop

    # ------------------------------------------------------------------ frame dispatch
    def _dispatch(self, ws, msg: dict):
        t = msg.get("t")
        if t == "req":
            if msg.get("hasBody"):
                self._pending_bodies[msg["id"]] = {"msg": msg, "buf": bytearray()}
            else:
                self._spawn(ws, msg, b"")
        elif t == "body":
            slot = self._pending_bodies.get(msg.get("id"))
            if slot is not None:
                slot["buf"] += base64.b64decode(msg.get("data", ""))
        elif t == "body_end":
            slot = self._pending_bodies.pop(msg.get("id"), None)
            if slot is not None:
                self._spawn(ws, slot["msg"], bytes(slot["buf"]))
        elif t == "pair_request":
            # A phone got the code right; the relay is holding it until this desktop agrees. Auto-
            # approve a device that was approved before (re-pairing after a reinstall is not a new
            # decision), otherwise park it for the control panel and say so on the console — someone
            # running headless still needs to know why their phone is waiting.
            did = str(msg.get("device_id") or "")
            if did and did in self.approved_devices:
                self._reply_pair(ws, msg.get("id"), True)
                self._log("relay: %s re-paired (already approved)" % (msg.get("name") or did[:8]))
                return
            self.pending_pair = {"id": msg.get("id"), "num": str(msg.get("num") or ""),
                                 "device_id": did, "name": str(msg.get("name") or ""),
                                 "at": __import__("time").time(), "ws": ws}
            self._log("relay: %s wants to pair · code %s · approve it at /remote"
                      % (msg.get("name") or "a device", msg.get("num")))
        elif t == "e2e_pair":
            self._e2e_handshake(ws, msg)
        elif t == "device_added":
            # a client completed pairing → the relay minted its session token and tells us the client's
            # stable device_id + the token HASH (never the token). Keyed by device_id, so the SAME
            # client re-pairing updates its row instead of duplicating. Re-push the deduped hash set.
            self.identity.add_or_update(msg.get("device_id", ""), msg.get("hash", ""), msg.get("name", ""))
            self.refresh_devices()
            self._log("relay: device paired (%s)" % (msg.get("name") or msg.get("device_id", "")[:8]))
            # ONE-SHOT pairing code: a code that just paired a device must not pair a second one. Rotate
            # it now so a leaked link (room#code) is spent the instant it's used — the panel live-updates
            # to the new code, and re-opening the old link lands on "link expired". Already-paired devices
            # keep working (they authenticate by session token, not the code).
            if self.on_pair:
                try:
                    self.on_pair()
                except Exception:
                    pass
        # ping/pong are handled at the WS control-frame layer (wsclient auto-pongs)

    # ------------------------------------------------------------------ E2E
    @staticmethod
    def _make_e2e_keys():
        """An X25519 keypair for this process, or None when the crypto extra is missing (in which case
        no public key is advertised and phones simply pair in plaintext)."""
        try:
            from . import e2e
            return e2e.keypair() if e2e.available() else None
        except Exception:                                          # pragma: no cover
            return None

    def e2e_public_b64(self):
        import base64
        return base64.b64encode(self._e2e_keys[1]).decode("ascii") if self._e2e_keys else ""

    def _e2e_handshake(self, ws, msg: dict):
        """A phone offered its public key. Verify its tag against the pairing code, answer with ours.

        The relay forwarded this and cannot forge the tag, so a swapped key fails here — which is the
        entire reason the pairing code is shown on this machine's screen rather than sent over the wire.
        """
        import base64
        rid = msg.get("id")
        device_id = str(msg.get("device_id") or "")
        try:
            from . import e2e
        except Exception as exc:                                   # pragma: no cover
            return self._e2e_refuse(ws, rid, "e2e unavailable: %s" % exc)
        if not e2e.available():
            # never fall back to plaintext for a client that ASKED for E2E
            return self._e2e_refuse(ws, rid, "desktop lacks the crypto extra (collie-harness[remote])")
        try:
            phone_pub = base64.b64decode(msg.get("pub") or "")
            phone_confirm = base64.b64decode(msg.get("confirm") or "")
            if len(phone_pub) != 32:
                raise ValueError("public key must be 32 bytes")
            if self._e2e_keys is None:
                return self._e2e_refuse(ws, rid, "desktop has no E2E keypair")
            priv, pub = self._e2e_keys
            if not e2e.verify_confirm(self.paircode, self.room, pub, phone_pub,
                                      e2e.SIDE_PHONE, phone_confirm):
                # either the code is wrong or the relay tampered with a key; both mean stop
                return self._e2e_refuse(ws, rid, "confirm tag mismatch")
            k_dev = e2e.device_key(e2e.shared_secret(priv, phone_pub), self.room)
            self._e2e_devices[device_id] = k_dev
            ws.send_text(json.dumps({
                "t": "e2e_pair_result", "id": rid, "ok": True,
                "pub": base64.b64encode(pub).decode("ascii"),
                "confirm": base64.b64encode(
                    e2e.confirm_tag(self.paircode, self.room, pub, phone_pub,
                                    e2e.SIDE_DESKTOP)).decode("ascii"),
            }))
            self._log("relay: E2E paired (%s)" % (device_id[:8] or "device"))
        except Exception as exc:                                   # noqa: BLE001
            self._e2e_refuse(ws, rid, str(exc))

    def _e2e_refuse(self, ws, rid, why: str):
        self._log("relay: E2E handshake refused — %s" % why)
        try:
            ws.send_text(json.dumps({"t": "e2e_pair_result", "id": rid, "ok": False, "error": why}))
        except Exception:
            pass

    def _e2e_key_for(self, req: dict):
        """(K_sess, session) for a sealed request, or (None, None) when this one is plaintext."""
        if not req.get("enc"):
            return None, None
        from . import e2e
        session = str(req.get("session") or "s1")
        cid = str(req.get("cid") or req.get("id"))
        # one paired device at a time in v1: the sealed frame proves which key opens it
        for k_dev in self._e2e_devices.values():
            try:
                key = e2e.session_key(k_dev, session)
                e2e.open_request(key, json.loads(req["enc"]), room=self.room,
                                 frame_id=cid, session=session, seq=int(req.get("seq") or 0))
                return key, session
            except Exception:
                continue
        return None, None

    def _spawn(self, ws, req: dict, body: bytes):
        # one thread per request → a long-lived SSE stream never blocks the sidebar's polls,
        # mirroring webapp.py's ThreadingHTTPServer rationale.
        threading.Thread(target=self._handle, args=(ws, req, body),
                         name="relay-req-%s" % req.get("id"), daemon=True).start()

    # ------------------------------------------------------------------ replay to local server
    def _handle(self, ws, req: dict, body: bytes):
        rid = req.get("id")
        try:
            key, session = self._e2e_key_for(req)
            cid = str(req.get("cid") or rid)
            if req.get("enc") and key is None:
                # a sealed frame we cannot open is not something to guess at
                raise ValueError("no paired E2E key opens this frame")
            if key is not None:
                from . import e2e
                envelope = e2e.open_request(key, json.loads(req["enc"]), room=self.room,
                                            frame_id=cid, session=session,
                                            seq=int(req.get("seq") or 0))
                method = (envelope.get("method") or "GET").upper()
                path = self._inject_token(envelope.get("path") or "/")
                headers = {k: v for k, v in (envelope.get("headers") or {}).items()
                           if k.lower() not in _DROP_HEADERS}
                body = envelope.get("body") or b""
            else:
                method = (req.get("method") or "GET").upper()
                path = self._inject_token(req.get("path") or "/")
                headers = {k: v for k, v in (req.get("headers") or {}).items()
                           if k.lower() not in _DROP_HEADERS}

            # generous timeout: an SSE run can have long quiet gaps (e.g. a slow bash tool call)
            # between frames; a short timeout would sever the phone's stream mid-run.
            headers["X-Collie-Relay"] = "1"   # tag as relay-replayed so the server withholds the raw CSRF token from pages
            conn = http.client.HTTPConnection(self.local_host, self.local_port, timeout=3600)
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()

            resp_headers = {k: v for k, v in resp.getheaders() if k.lower() not in _DROP_HEADERS}
            if key is not None:
                from . import e2e
                head = json.dumps({"status": resp.status, "headers": resp_headers}).encode()
                ws.send_text(json.dumps({
                    "t": "res", "id": rid, "status": 200,
                    "headers": {"content-type": "application/octet-stream"},
                    "enc": json.dumps(e2e.seal_chunk(key, head, room=self.room, frame_id=cid,
                                                     session=session, seq=0)), "seq": 0}))
            else:
                ws.send_text(json.dumps({"t": "res", "id": rid, "status": resp.status,
                                         "headers": resp_headers}))
            seq = 1
            while True:
                # read1(): return as soon as ANY bytes are available, instead of blocking until the
                # full buffer fills. Essential for SSE — a long-lived stream (/api/mirror) or a slow
                # token trickle must forward frame-by-frame, not stall until 2 KB accumulates.
                chunk = resp.read1(_CHUNK)
                if not chunk:
                    break
                if key is not None:
                    from . import e2e
                    ws.send_text(json.dumps({
                        "t": "chunk", "id": rid, "seq": seq,
                        "enc": json.dumps(e2e.seal_chunk(key, chunk, room=self.room, frame_id=cid,
                                                         session=session, seq=seq))}))
                    seq += 1
                else:
                    ws.send_text(json.dumps({"t": "chunk", "id": rid,
                                             "data": base64.b64encode(chunk).decode("ascii")}))
            ws.send_text(json.dumps({"t": "end", "id": rid}))
            conn.close()
        except WebSocketClosed:
            raise
        except Exception as e:                            # noqa: BLE001
            try:
                ws.send_text(json.dumps({"t": "err", "id": rid, "msg": str(e)}))
            except Exception:
                pass

    def _inject_token(self, path: str) -> str:
        """Force the local CSRF token onto the query string so `_authed` passes; the phone never
        sees or supplies it. Overrides any client-supplied token."""
        parts = urllib.parse.urlsplit(path)
        q = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        q["token"] = self.local_token
        return urllib.parse.urlunsplit(("", "", parts.path or "/",
                                        urllib.parse.urlencode(q), parts.fragment))


class RemoteState:
    """Owns the RelayClient lifecycle so the running web server (webapp.py) can show status, kick
    devices, rotate the code, and toggle remote on/off from the desktop control panel. One per
    `collie web [--remote]` process; webapp.REMOTE points at it."""

    def __init__(self, relay_url: str, local_port: int, local_token: str, logf=None):
        from . import remote_identity
        self.relay_url = relay_url.rstrip("/")
        self.web_base = self.relay_url.replace("wss://", "https://").replace("ws://", "http://")
        self.local_port = local_port
        self.local_token = local_token
        self._log = logf or (lambda *a: None)
        self.identity = remote_identity.load_or_create()
        self.paircode = None
        self._paircode_at = 0.0
        self.client: RelayClient | None = None
        self._thread = None
        self.enabled = False

    def wait_connected(self, timeout=8.0):
        """True once the agent socket is up. The CLI waits on this before advertising a pairing link:
        printing a link (and a QR) while the relay is unreachable sends the user's phone to whatever
        else answers on that hostname — which, for a relay behind a marketing site, is a 405."""
        client = self.client
        if client is None:
            return False
        return client.connected.wait(timeout)

    def last_error(self):
        client = self.client
        return client.last_error if client else None

    def link(self):
        self._maybe_expire()
        if not self.paircode:
            return None
        return "%s/r/%s#%s" % (self.web_base, self.identity.room, self.paircode)

    def start(self):
        import threading
        from . import remote_identity
        if self.enabled:
            return
        self.paircode = remote_identity.gen_paircode()
        self.client = RelayClient(self.relay_url, self.identity, self.paircode,
                                  "127.0.0.1", self.local_port, self.local_token, self._log)
        self.client.on_pair = self.rotate_code       # one-shot: rotate the code the moment a device pairs
        self._thread = threading.Thread(target=self.client.run_forever, name="collie-relay", daemon=True)
        self._thread.start()
        self.enabled = True

    def stop(self):
        if self.client:
            self.client.stop()
        self.enabled = False

    # The LAN pairing secret has expired after 180s since it existed; the relay code never did. It
    # was only ever invalidated by being USED, so a code shown on screen at 9am still paired a phone
    # at 9pm — and it is written in plain text in a URL, which means a screenshot, a screen share or
    # the phone's own history is enough. What one scan buys is every /api/* on the desktop: run
    # commands, read and write files, drive the logged-in browser. That is too much to leave lying
    # around indefinitely, so the two paths now expire the same way.
    CODE_TTL = 180

    def code_age(self):
        import time
        return time.time() - (self._paircode_at or 0)

    def _maybe_expire(self):
        """Rotate a code that has gone stale. Called wherever the code is read or shown, so the
        window is real rather than nominal: an unattended pairing screen refreshes itself."""
        if self.enabled and self.paircode and self.code_age() > self.CODE_TTL:
            self.rotate_code()
            self._log("relay: pairing code expired after %ds — a fresh one is on the pairing screen"
                      % self.CODE_TTL)
            return True
        return False

    def decide_pair(self, allow: bool) -> bool:
        """Answer the phone that is waiting. Approving also remembers the device, so the same phone
        re-pairing later — after a reinstall, or after the code rotated — is not asked about again."""
        cl = self.client
        p = getattr(cl, "pending_pair", None) if cl else None
        if not p:
            return False
        cl._reply_pair(p["ws"], p["id"], allow)
        if allow and p.get("device_id"):
            cl.approved_devices.add(p["device_id"])
        self._log("relay: %s %s" % (p.get("name") or "device", "approved" if allow else "denied"))
        cl.pending_pair = None
        return True

    def rotate_code(self):
        import time
        from . import remote_identity
        self.paircode = remote_identity.gen_paircode()
        self._paircode_at = time.time()
        if self.client:
            self.client.paircode = self.paircode
            self.client.refresh_paircode()
        return self.paircode

    def forget(self, device_id: str) -> bool:
        ok = self.identity.forget_device(device_id)
        if ok and self.client:
            # Drop it from the in-memory approved set too — else a kicked (or compromised) device
            # replaying its stable device_id with a live pairing code is auto-approved with NO human
            # number-match prompt until the desktop restarts, defeating the whole point of the kick.
            self.client.approved_devices.discard(device_id)
            if self.enabled:
                self.client.refresh_devices()   # push the shrunk hash set → kicked device 401s at once
        return ok

    def rename(self, device_id: str, name: str) -> bool:
        return self.identity.rename(device_id, name)

    def status(self) -> dict:
        self._maybe_expire()      # the control panel is a read of the code, so it expires here too
        return {
            "code_age": int(self.code_age()) if self.paircode else 0,
            "code_ttl": self.CODE_TTL,
            "enabled": self.enabled,
            "connected": bool(self.client and self.client._ws is not None and self.enabled),
            "relay": self.relay_url,
            "room": self.identity.room,
            "link": self.link(),
            "paircode": self.paircode,
            "devices": self.identity.devices(),
        }
