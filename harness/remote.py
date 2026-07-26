"""Collie Remote — the desktop side of "control your desktop Collie from your phone".

Topology (see docs design): the desktop is behind NAT, so it dials *out* to a public relay
(a Cloudflare Worker at collie.run) over one WebSocket; the phone talks to the same relay over
HTTPS; the relay multiplexes the phone's HTTP requests onto the agent WS.

This module is the relay *client*. The elegant part: it is just a **local client of our own
127.0.0.1 web server** — it replays each incoming request to http://127.0.0.1:<port>/... with the
per-process CSRF TOKEN injected. So webapp.py's `_host_ok` (Host is loopback) and `_authed` (token
present) are satisfied untouched, and the local security model keeps holding. The local TOKEN never
leaves the machine; the phone authenticates one layer up, at the relay.

v0 scope: no E2E, plaintext pairing code, single implicit session. Concurrency, streaming (SSE),
reconnect, and per-request isolation are here from the start because they're load-bearing.
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

    # ------------------------------------------------------------------ lifecycle
    def run_forever(self):
        """Connect, serve, and reconnect with exponential backoff until stop()."""
        backoff = 1.0
        while not self._stop:
            try:
                self._connect_and_serve()
                backoff = 1.0
            except WebSocketClosed:
                self._log("relay: connection closed")
            except Exception as e:                       # noqa: BLE001 — keep the loop alive
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

    def _connect_and_serve(self):
        q = urllib.parse.urlencode({"room": self.room, "key": self.agent_key})
        url = "%s/relay/agent?%s" % (self.relay_url, q)
        self._log("relay: connecting %s (room=%s)" % (self.relay_url, self.room))
        ws = WebSocketClient.connect(url)
        self._ws = ws
        # announce ourselves. The desktop is the source of truth for pairing: we hand the relay the
        # AGENTKEY (proves we own this room), the current pairing code (for NEW devices), and the set
        # of already-paired device-token hashes (so RETURNING phones validate without re-pairing —
        # this is what makes pairing survive a desktop restart / 24h offline).
        ws.send_text(json.dumps({
            "t": "hello", "v": 1, "room": self.room, "agentKey": self.agent_key,
            "paircode": self.paircode, "devices": self.identity.device_hashes(),
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
        elif t == "device_added":
            # a client completed pairing → the relay minted its session token and tells us the client's
            # stable device_id + the token HASH (never the token). Keyed by device_id, so the SAME
            # client re-pairing updates its row instead of duplicating. Re-push the deduped hash set.
            self.identity.add_or_update(msg.get("device_id", ""), msg.get("hash", ""), msg.get("name", ""))
            self.refresh_devices()
            self._log("relay: device paired (%s)" % (msg.get("name") or msg.get("device_id", "")[:8]))
        # ping/pong are handled at the WS control-frame layer (wsclient auto-pongs)

    def _spawn(self, ws, req: dict, body: bytes):
        # one thread per request → a long-lived SSE stream never blocks the sidebar's polls,
        # mirroring webapp.py's ThreadingHTTPServer rationale.
        threading.Thread(target=self._handle, args=(ws, req, body),
                         name="relay-req-%s" % req.get("id"), daemon=True).start()

    # ------------------------------------------------------------------ replay to local server
    def _handle(self, ws, req: dict, body: bytes):
        rid = req.get("id")
        try:
            method = (req.get("method") or "GET").upper()
            path = self._inject_token(req.get("path") or "/")
            headers = {k: v for k, v in (req.get("headers") or {}).items()
                       if k.lower() not in _DROP_HEADERS}

            # generous timeout: an SSE run can have long quiet gaps (e.g. a slow bash tool call)
            # between frames; a short timeout would sever the phone's stream mid-run.
            conn = http.client.HTTPConnection(self.local_host, self.local_port, timeout=3600)
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()

            resp_headers = {k: v for k, v in resp.getheaders() if k.lower() not in _DROP_HEADERS}
            ws.send_text(json.dumps({"t": "res", "id": rid, "status": resp.status,
                                     "headers": resp_headers}))
            while True:
                # read1(): return as soon as ANY bytes are available, instead of blocking until the
                # full buffer fills. Essential for SSE — a long-lived stream (/api/mirror) or a slow
                # token trickle must forward frame-by-frame, not stall until 2 KB accumulates.
                chunk = resp.read1(_CHUNK)
                if not chunk:
                    break
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
        self.client: RelayClient | None = None
        self._thread = None
        self.enabled = False

    def link(self):
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
        self._thread = threading.Thread(target=self.client.run_forever, name="collie-relay", daemon=True)
        self._thread.start()
        self.enabled = True

    def stop(self):
        if self.client:
            self.client.stop()
        self.enabled = False

    def rotate_code(self):
        from . import remote_identity
        self.paircode = remote_identity.gen_paircode()
        if self.client:
            self.client.paircode = self.paircode
            self.client.refresh_paircode()
        return self.paircode

    def forget(self, device_id: str) -> bool:
        ok = self.identity.forget_device(device_id)
        if ok and self.client and self.enabled:
            self.client.refresh_devices()     # push the shrunk hash set → kicked device 401s at once
        return ok

    def rename(self, device_id: str, name: str) -> bool:
        return self.identity.rename(device_id, name)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": bool(self.client and self.client._ws is not None and self.enabled),
            "relay": self.relay_url,
            "room": self.identity.room,
            "link": self.link(),
            "paircode": self.paircode,
            "devices": self.identity.devices(),
        }
