"""Collie Remote — the desktop side of "control your desktop Collie from your phone".

Topology (see docs design): the desktop is behind NAT, so it dials *out* to a public relay
(a Cloudflare Worker at collie.run) over one WebSocket; the phone talks to the same relay over
HTTPS; the relay multiplexes the phone's HTTP requests onto the agent WS.

This module is the relay *client*. The elegant part: it is just a **local client of our own
127.0.0.1 web server** — it replays each incoming request to http://127.0.0.1:<port>/... with the
per-process CSRF TOKEN injected. So webapp.py's `_host_ok` (Host is loopback) and `_authed` (token
present) are satisfied untouched, and the local security model keeps holding. The local TOKEN never
leaves the machine; the phone authenticates one layer up, at the relay.

Hosted-remote protocol v2 is fail-closed E2E.  A QR fragment carries a 256-bit secret and this
desktop's X25519 public key; URL fragments never reach the relay.  The phone sends only an HMAC proof
over the key transcript, which this process verifies before a human approves the device.  Every API
request then uses one fixed outer endpoint and an encrypted inner method/path/query/body.  Plaintext
requests from a hosted relay are deliberately rejected; see harness/e2e.py and relay/E2E_DESIGN.md.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
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


def _validated_relay_url(relay_url: str) -> str:
    """Return a normalized relay origin, rejecting network-visible plaintext transports."""
    value = str(relay_url or "").strip()
    parts = urllib.parse.urlsplit(value)
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError("invalid Collie relay URL") from exc
    host = (parts.hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme not in {"wss", "ws"} or not host:
        raise ValueError("Collie relay URL must be a WebSocket origin")
    if parts.scheme == "ws" and not loopback:
        raise ValueError("hosted Collie remote requires wss:// (ws:// is loopback-only)")
    if (parts.username is not None or parts.password is not None or
            parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("Collie relay URL must be an origin without credentials, path, query, or fragment")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


class RelayClient:
    PAIR_WINDOW_S = 10 * 60
    PAIR_MAX = 5
    PAIR_PENDING_S = 3 * 60
    PAIR_SECRET_TTL_S = 3 * 60
    REPLAY_TTL_S = 30 * 24 * 60 * 60
    REPLAY_MAX = 8192

    def __init__(self, relay_url: str, identity, pairing_secret: str,
                 local_host: str, local_port: int, local_token: str, logf=None,
                 pairing_expires_at=None):
        self.relay_url = _validated_relay_url(relay_url)
        self.identity = identity          # harness.remote_identity.Identity (durable device store)
        self.room = identity.room
        self.agent_key = identity.agent_key
        # This secret is intentionally desktop-only.  It appears in the QR URL *fragment* and is
        # never placed in a WebSocket hello or HTTP body visible to the relay.
        self.pairing_secret = pairing_secret
        self.pairing_expires_at = (float(pairing_expires_at) if pairing_expires_at is not None
                                   else time.time() + self.PAIR_SECRET_TTL_S)
        self.local_host = local_host
        self.local_port = local_port
        self.local_token = local_token
        self._log = logf or (lambda *a: None)
        self._ws: WebSocketClient | None = None
        self._stop = False
        self._wake = threading.Event()
        self.connected = threading.Event()   # set once the agent socket is actually up
        self.last_error = None               # why the last attempt failed, for the CLI to report
        self.on_pair = None                  # set by Remote: rotate after a valid proof (one-shot)
        # A device waiting on a human. The relay holds the pairing until _reply_pair answers, so this
        # is at most one at a time — a second request while one is pending would be exactly the
        # confusion the number on both screens exists to prevent.
        self.pending_pair = None             # verified proof waiting on the human
        self.approved_devices = set(identity.approved_ids())
        self._pair_failures = []
        # Human-approved, one-shot pairing material. The request id binds a later device_added to
        # the exact number-match decision; neither a stale nor invented relay message can consume it.
        self._approved_pair_keys = {}        # device_id -> {key, pair_id, approved_at}
        # Hosted mode has no plaintext fallback.  Starting remote without the crypto extra is an
        # actionable configuration error, not permission to expose prompts to the operator.
        self._e2e_keys = self._make_e2e_keys()
        if self._e2e_keys is None:
            raise RuntimeError(
                "Collie hosted remote requires E2E support; install 'collie-harness[remote]'")
        # K_dev per device, RELOADED from the device store. The keypair above is per process, but the
        # session keys are not: a restart used to leave every encrypted phone unable to talk to this
        # desktop at all, which is the opposite of what E2E_DESIGN.md §7 promises.
        self._e2e_devices = self._load_device_keys()
        self._replay_lock = threading.Lock()
        # A relay peer is untrusted input. Bound concurrent local replays so a phone bug or a
        # compromised relay cannot turn one desktop into an unbounded thread/connection factory.
        self._inflight_limit = max(1, min(256, int(os.environ.get(
            "COLLIE_REMOTE_MAX_INFLIGHT", "32"))))
        self._inflight_gate = threading.BoundedSemaphore(self._inflight_limit)
        self._inflight_lock = threading.Lock()
        self._inflight_count = 0

    def _heartbeat(self, state: str, **detail):
        try:
            from .ops import heartbeat
            detail.update({"connected": self.connected.is_set(),
                           "inflight": self._inflight_count,
                           "inflight_limit": self._inflight_limit})
            heartbeat("remote", state, detail, ttl=max(75.0, self.PONG_GRACE_S + 10))
        except Exception:
            pass

    # ------------------------------------------------------------------ lifecycle
    def run_forever(self):
        """Connect, serve, and reconnect with exponential backoff until stop()."""
        backoff = 1.0
        while not self._stop:
            try:
                self._heartbeat("connecting")
                self._connect_and_serve()
                self.last_error = None
                backoff = 1.0
            except WebSocketClosed:
                self.connected.clear()
                self._log("relay: connection closed")
                self._heartbeat("disconnected", reason="connection closed")
            except Exception as e:                       # noqa: BLE001 — keep the loop alive
                self.connected.clear()
                self.last_error = e
                self._log("relay: error: %s" % e)
                self._heartbeat("retrying", error="%s: %s" % (type(e).__name__, e),
                                retry_in_s=backoff)
            if self._stop:
                break
            self._wake.wait(backoff)
            self._wake.clear()
            backoff = min(backoff * 2, 30.0)
        self._heartbeat("stopped")

    def stop(self):
        self._stop = True
        self._wake.set()
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

    def _ask_on_screen(self, pending):
        """Put the pairing question in front of whoever is at this computer.

        The card on /remote only asks someone who happens to have that page open, which at the moment
        a phone scans is nobody. A device asking for the run of this machine has to interrupt — once,
        at pairing — or the check is decoration.

        On its own thread: this blocks on a human, and the socket it arrived on is the same one every
        run streams over.
        """
        def run():
            from . import plat
            answer = plat.ask_allow_deny(
                "Collie — a device wants to pair",
                "%s is asking to control this computer.\n\nNumber shown on it: %s\n\n"
                "Only allow it if that number matches, and if it is your device."
                % (pending.get("name") or "A device", pending.get("num") or "?"))
            if answer is None:
                return                       # headless, dismissed, or timed out — leave it to the card
            # The card on /remote may have answered while the dialog was up. Only decide if THIS
            # request is still the one waiting, or a stale click would answer someone else's.
            cur = self.pending_pair
            if not cur or cur.get("id") != pending.get("id"):
                return
            try:
                self._reply_pair(pending["ws"], pending["id"], answer)
                if answer and pending.get("device_id"):
                    self.approved_devices.add(pending["device_id"])
                self.pending_pair = None
                self._log("relay: %s %s (from the desktop prompt)"
                          % (pending.get("name") or "device", "approved" if answer else "denied"))
            except Exception:
                pass

        threading.Thread(target=run, name="collie-pair-prompt", daemon=True).start()

    def notify(self, title, body, session="", thread="collie"):
        """Ask the relay to push a notice to every phone paired with this desktop.

        Through the relay rather than straight to Apple: the APNs key is a credential for the whole
        app, and putting it on every user's machine would mean shipping it. The relay already holds
        the devices, and it is the piece that stays up when this machine sleeps.

        Best effort by design — a run must never fail because a notification could not be sent.
        """
        ws = self._ws
        if ws is None:
            return False
        try:
            # Notification text can contain run output.  APNs delivery requires the relay to see the
            # final alert payload, so hosted mode deliberately sends a generic notice instead of
            # leaking caller-supplied title/body outside the E2E channel.
            ws.send_text(json.dumps({"t": "notify", "session": str(session or ""),
                                     "thread": str(thread or "collie")}))
            return True
        except Exception:
            return False

    def _reply_pair(self, ws, rid, ok, error=""):
        pending = self.pending_pair
        if pending and pending.get("id") == rid:
            did = pending.get("device_id") or ""
            if ok and did:
                self._approved_pair_keys[did] = {
                    "key": pending["k_dev"], "pair_id": str(rid), "approved_at": time.time(),
                }
            elif did:
                self._approved_pair_keys.pop(did, None)
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
        self._heartbeat("connected")
        # The relay gets routing/authentication metadata only.  In particular, the QR secret is not
        # in hello: it stays in a URL fragment between the desktop screen and the phone camera.
        ws.send_text(json.dumps({
            "t": "hello", "v": 2, "room": self.room, "agentKey": self.agent_key,
            "devices": self.identity.device_hashes(),
            "approve": True, "e2eRequired": True,
        }))
        self._log("relay: connected")
        stop_ka = self._start_keepalive(ws)
        try:
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
            self.connected.clear()
            self._heartbeat("disconnected")

    KEEPALIVE_S = 20.0
    # Two missed replies, not one: a single slow round trip over a phone network is not a dead
    # socket, and tearing the connection down for it would reconnect constantly.
    PONG_GRACE_S = 55.0

    def _start_keepalive(self, ws) -> threading.Event:
        """Ping, and REQUIRE an answer.

        Pinging alone proves nothing: a socket stays writable long after the far end has stopped
        treating it as this room's agent, so every ping succeeds, nothing raises, and the desktop
        reports itself connected while the relay answers "desktop offline" to the phone. That state
        is invisible from here and lasts until something restarts the client by hand.

        The far end's PONG is the only evidence anyone is listening. Without one for long enough,
        close the socket — the run loop reconnects, which is the whole point.
        """
        import time as _time
        stop = threading.Event()

        def beat():
            ws.last_pong = _time.time()          # a fresh socket has not gone quiet yet
            while not stop.wait(self.KEEPALIVE_S):
                if _time.time() - getattr(ws, "last_pong", 0.0) > self.PONG_GRACE_S:
                    self._log("relay: no reply to keepalive — reconnecting")
                    self._heartbeat("disconnected", reason="keepalive timeout")
                    try:
                        ws.close()               # wakes recv_message() → the run loop redials
                    except Exception:
                        pass
                    return
                self._heartbeat("connected")
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
            # Hosted v2 is E2E-only.  Accepting the old method/path/body fields would make a relay
            # downgrade silently and expose exactly the prompts and code E2E is meant to protect.
            if not msg.get("enc"):
                self._protocol_error(ws, msg.get("id"), "hosted requests must be sealed")
            else:
                self._spawn(ws, msg, b"")
        elif t in ("body", "body_end"):
            self._protocol_error(ws, msg.get("id"), "plaintext body frames are disabled")
        elif t == "pair_request":
            self._begin_pair(ws, msg)
        elif t == "device_added":
            # The untrusted relay may only finish the exact pairing a human just approved. Persist
            # both token hash and K_dev before ACKing; the relay withholds the bearer until this ACK.
            ack_id = str(msg.get("id") or "")
            pair_id = str(msg.get("pair_id") or "")
            did = str(msg.get("device_id") or "")
            token_hash = str(msg.get("hash") or "")
            staged = self._approved_pair_keys.get(did)
            valid = (
                bool(ack_id) and len(ack_id) <= 128 and bool(did) and len(did) <= 128 and
                bool(pair_id) and isinstance(staged, dict) and
                hmac.compare_digest(str(staged.get("pair_id") or ""), pair_id) and
                0 <= time.time() - float(staged.get("approved_at") or 0) <= self.PAIR_PENDING_S and
                re.fullmatch(r"[0-9a-f]{64}", token_hash) is not None and
                isinstance(staged.get("key"), bytes) and len(staged["key"]) == 32
            )
            ok = False
            old_entry = None
            had_old_entry = False
            if valid:
                devices = self.identity._d.setdefault("devices", {})  # noqa: SLF001
                had_old_entry = did in devices
                if had_old_entry:
                    old_entry = dict(devices[did])
                try:
                    k_dev = staged["key"]
                    encoded_key = base64.b64encode(k_dev).decode("ascii")
                    name = str(msg.get("name") or "")[:60]
                    now = int(time.time())
                    if had_old_entry:
                        stored = dict(old_entry)
                        stored.update({"token_sha": token_hash, "last_seen": now,
                                       "k_dev": encoded_key})
                        if name and not stored.get("name"):
                            stored["name"] = name
                    else:
                        stored = {"name": name or "device", "token_sha": token_hash,
                                  "paired_at": now, "last_seen": now, "k_dev": encoded_key}
                    devices[did] = stored
                    # One atomic file replacement contains hash + K_dev. A crash cannot persist the
                    # bearer allowlist without the decryption key or vice versa.
                    self.identity._save()                             # noqa: SLF001
                    self._e2e_devices[did] = k_dev
                    self._approved_pair_keys.pop(did, None)
                    self.approved_devices.add(did)
                    ok = True
                except Exception as exc:                              # noqa: BLE001
                    devices = self.identity._d.setdefault("devices", {})  # noqa: SLF001
                    if had_old_entry:
                        devices[did] = old_entry
                    else:
                        devices.pop(did, None)
                    try:
                        self.identity._save()                         # noqa: SLF001
                    except Exception:
                        pass
                    self._log("relay: could not persist paired device: %s" % exc)
            try:
                ws.send_text(json.dumps({"t": "device_stored", "id": ack_id,
                                         "hash": token_hash, "ok": ok}))
            except Exception:
                pass
            if ok:
                self.refresh_devices()
                self._log("relay: device paired (%s)" %
                          (msg.get("name") or msg.get("device_id", "")[:8]))
            else:
                self._log("relay: rejected unbound device registration")
        elif t == "device_revoke":
            token_hash = str(msg.get("hash") or "")
            ok = self._revoke_hash(token_hash)
            try:
                # The relay must not report success to the phone until the durable desktop store
                # confirms deletion.  The id makes retries/reconnect reconciliation idempotent.
                ws.send_text(json.dumps({"t": "device_revoked", "id": msg.get("id"),
                                         "hash": token_hash, "ok": bool(ok)}))
            except Exception:
                pass
        # ping/pong are handled at the WS control-frame layer (wsclient auto-pongs)

    @staticmethod
    def _protocol_error(ws, rid, message):
        try:
            ws.send_text(json.dumps({"t": "err", "id": rid, "msg": message}))
        except Exception:
            pass

    # ------------------------------------------------------------------ E2E
    @staticmethod
    def _make_e2e_keys():
        """An X25519 keypair for this process; hosted startup fails if crypto is unavailable."""
        try:
            from . import e2e
            return e2e.keypair() if e2e.available() else None
        except Exception:                                          # pragma: no cover
            return None

    def e2e_public_b64(self):
        import base64
        return base64.b64encode(self._e2e_keys[1]).decode("ascii") if self._e2e_keys else ""

    def _begin_pair(self, ws, msg: dict):
        """Verify a relay-blind phone proof, burn the QR secret, then ask the human.

        `msg` contains only public keys, an HMAC proof and display metadata.  The high-entropy secret
        that authenticates the transcript came from the QR fragment and never traverses the relay.
        """
        rid = msg.get("id")
        device_id = str(msg.get("device_id") or "")
        now = time.time()
        if not self.pairing_secret or now >= self.pairing_expires_at:
            # Enforce expiry at the authentication boundary, not only when a UI happens to poll the
            # pairing status.  A screenshot of an unattended QR therefore expires on schedule.
            self.pairing_secret = ""
            self.pairing_expires_at = 0.0
            if self.on_pair:
                try:
                    self.on_pair()
                except Exception:
                    pass
            return self._pair_refuse(ws, rid, "pairing secret expired")
        self._pair_failures = [t for t in self._pair_failures if now - t < self.PAIR_WINDOW_S]
        if len(self._pair_failures) >= self.PAIR_MAX:
            return self._pair_refuse(ws, rid, "pairing temporarily locked")
        if self.pending_pair and now - self.pending_pair.get("at", 0) < self.PAIR_PENDING_S:
            return self._pair_refuse(ws, rid, "another pairing request is awaiting approval")
        if self.pending_pair:
            self._approved_pair_keys.pop(self.pending_pair.get("device_id") or "", None)
            self.pending_pair = None
        try:
            from . import e2e
            if not rid or not device_id or not self.pairing_secret:
                raise ValueError("invalid pairing request")
            phone_pub = base64.b64decode(msg.get("pub") or "", validate=True)
            phone_confirm = base64.b64decode(msg.get("confirm") or "", validate=True)
            if len(phone_pub) != 32 or len(phone_confirm) != 32:
                raise ValueError("invalid pairing proof")
            priv, pub = self._e2e_keys
            secret = self.pairing_secret
            if not e2e.verify_confirm(secret, self.room, pub, phone_pub,
                                      e2e.SIDE_PHONE, phone_confirm):
                raise ValueError("invalid pairing proof")
            k_dev = e2e.device_key(e2e.shared_secret(priv, phone_pub), self.room)
            desktop_confirm = e2e.confirm_tag(
                secret, self.room, pub, phone_pub, e2e.SIDE_DESKTOP)
            num = "%04d" % (int.from_bytes(desktop_confirm[:4], "big") % 10000)
            self._pair_failures = []

            # Burn before acknowledging proof validity.  A second request signed by the same QR
            # secret fails even if the relay races it before the human answers the first one.
            self.pairing_secret = ""
            self.pairing_expires_at = 0.0
            if self.on_pair:
                try:
                    self.on_pair()
                except Exception as rotate_error:                    # noqa: BLE001
                    self._log("relay: valid proof consumed; could not prepare next QR: %s"
                              % rotate_error)

            self.pending_pair = {
                "id": rid, "num": num, "device_id": device_id,
                "name": str(msg.get("name") or "device")[:60], "at": now, "ws": ws,
                "k_dev": k_dev,
            }
            ws.send_text(json.dumps({
                "t": "pair_ready", "id": rid, "num": num,
                "pub": base64.b64encode(pub).decode("ascii"),
                "confirm": base64.b64encode(desktop_confirm).decode("ascii"),
            }))
            self._log("relay: %s wants to pair · verification %s · approve it at /remote"
                      % (self.pending_pair["name"], num))
            self._ask_on_screen(self.pending_pair)
        except Exception as exc:                                   # noqa: BLE001
            self._pair_failures.append(now)
            if len(self._pair_failures) >= self.PAIR_MAX:
                # A captured QR is useless after the limit as well as after a success.
                self.pairing_secret = ""
                self.pairing_expires_at = 0.0
                if self.on_pair:
                    try:
                        self.on_pair()
                    except Exception:
                        pass
            self._pair_refuse(ws, rid, str(exc))

    def _load_device_keys(self) -> dict:
        """K_dev for every device that paired with encryption, from the last run of this desktop."""
        out = {}
        try:
            for device_id, b64 in (self.identity.device_keys() or {}).items():
                try:
                    out[device_id] = base64.b64decode(b64)
                except Exception:
                    continue               # a corrupt entry re-pairs; it must not stop the others
        except Exception:
            pass
        return out

    def _pair_refuse(self, ws, rid, why: str):
        self._log("relay: pairing proof refused — %s" % why)
        try:
            # Keep the response deliberately generic.  The relay needs state, not an oracle about
            # which field of an authentication proof was wrong.
            ws.send_text(json.dumps({"t": "pair_invalid", "id": rid,
                                     "error": "pairing proof refused"}))
        except Exception:
            pass

    def _revoke_hash(self, token_hash: str) -> bool:
        """Durably forget the device whose bearer-token hash the relay authenticated.

        Missing is success: the relay may replay a pending tombstone after either side reconnects.
        Persistence failure is not success, and the in-memory deletion is rolled back so a restart
        cannot silently resurrect a device that the phone was told had been revoked.
        """
        if not token_hash:
            return False
        device_id = ""
        entry = None
        try:
            for did, entry in (self.identity._d.get("devices") or {}).items():  # noqa: SLF001
                if hmac.compare_digest(str(entry.get("token_sha") or ""), token_hash):
                    device_id = did
                    entry = dict(entry)
                    break
        except Exception:
            return False
        if not device_id:
            return True
        try:
            removed = self.identity.forget_device(device_id)
        except Exception:
            if entry is not None:
                self.identity._d.setdefault("devices", {})[device_id] = entry  # noqa: SLF001
            return False
        if not removed:
            return False
        self._e2e_devices.pop(device_id, None)
        self._approved_pair_keys.pop(device_id, None)
        self.approved_devices.discard(device_id)
        self.refresh_devices()
        self._log("relay: device revoked (%s)" % device_id[:8])
        return True

    def _e2e_key_for(self, req: dict):
        """Return `(K_sess, session, device_id, envelope)` for a valid sealed request."""
        if not req.get("enc") or int(req.get("seq", -1)) != 0:
            return None, None, None, None
        from . import e2e
        session = str(req.get("session") or "")
        cid = str(req.get("cid") or "")
        if not session or len(session) > 256 or not cid or len(cid) > 128:
            return None, None, None, None
        try:
            enc = json.loads(req["enc"])
            if not isinstance(enc, dict):
                return None, None, None, None
        except (TypeError, ValueError):
            return None, None, None, None
        # No device id is exposed outside the ciphertext.  The AEAD tag identifies the one K_dev
        # that can open the request; the number of devices is intentionally small and bounded by the
        # human-managed device list.
        candidates = list(self._e2e_devices.items())
        for device_id, k_dev in candidates:
            try:
                key = e2e.session_key(k_dev, session)
                envelope = e2e.open_request(key, enc, room=self.room,
                                            frame_id=cid, session=session, seq=0)
                return key, session, device_id, envelope
            except Exception:
                continue
        return None, None, None, None

    def _claim_request(self, device_id: str, cid: str, method: str, path: str = "") -> bool:
        """Persistently reject duplicate state-changing or run-starting request ids.

        This is the desktop's last line of defence if a relay or mobile network replays a ciphertext.
        Ordinary GET/HEAD/OPTIONS remain retryable, but GET `/api/stream` starts work and is therefore
        accepted exactly once just like POST/PATCH/PUT/DELETE. Claims last thirty days across restarts.
        """
        safe_path = urllib.parse.urlsplit(path or "").path
        if method in ("GET", "HEAD", "OPTIONS") and safe_path != "/api/stream":
            return True
        now = int(time.time())
        with self._replay_lock:
            data = getattr(self.identity, "_d", {})       # noqa: SLF001 — same private durable file
            original = data.get("remote_v2_seen")
            seen = [x for x in (original or [])
                    if isinstance(x, dict) and now - int(x.get("t") or 0) < self.REPLAY_TTL_S]
            if any(x.get("d") == device_id and x.get("c") == cid for x in seen):
                return False
            # Never evict an unexpired claim: doing so would make the oldest state change replayable.
            # Capacity exhaustion rejects new work until records expire, preserving fail-closed E2E.
            if len(seen) >= self.REPLAY_MAX:
                return False
            seen.append({"d": device_id, "c": cid, "t": now})
            data["remote_v2_seen"] = seen
            try:
                self.identity._save()                     # noqa: SLF001
            except Exception:
                # Fail closed: executing without recording would make a dropped reply replayable.
                if original is None:
                    data.pop("remote_v2_seen", None)
                else:
                    data["remote_v2_seen"] = original
                return False
        return True

    def _spawn(self, ws, req: dict, body: bytes):
        # one thread per request → a long-lived SSE stream never blocks the sidebar's polls,
        # mirroring webapp.py's ThreadingHTTPServer rationale.
        if not self._inflight_gate.acquire(blocking=False):
            self._protocol_error(ws, req.get("id"),
                                 "desktop request capacity reached; retry later")
            self._heartbeat("overloaded")
            return False
        with self._inflight_lock:
            self._inflight_count += 1
        threading.Thread(target=self._run_request, args=(ws, req, body),
                         name="relay-req-%s" % req.get("id"), daemon=True).start()
        return True

    def _run_request(self, ws, req: dict, body: bytes):
        try:
            self._handle(ws, req, body)
        finally:
            with self._inflight_lock:
                self._inflight_count = max(0, self._inflight_count - 1)
            self._inflight_gate.release()

    # ------------------------------------------------------------------ replay to local server
    def _handle(self, ws, req: dict, body: bytes):
        rid = req.get("id")
        key = session = cid = None
        next_seq = 0
        last_data_seq = 0
        conn = None
        try:
            key, session, device_id, envelope = self._e2e_key_for(req)
            cid = str(req.get("cid") or rid)
            if key is None or envelope is None:
                raise ValueError("sealed request could not be authenticated; re-pair this device")

            method = str(envelope.get("method") or "GET").upper()
            raw_path = str(envelope.get("path") or "/")
            parsed = urllib.parse.urlsplit(raw_path)
            if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
                raise ValueError("invalid inner request method")
            if (len(raw_path) > 8192 or parsed.scheme or parsed.netloc or parsed.fragment or
                    not raw_path.startswith("/")):
                raise ValueError("invalid inner request path")
            path = self._inject_token(raw_path)
            raw_headers = envelope.get("headers") or {}
            if not isinstance(raw_headers, dict) or len(raw_headers) > 128:
                raise ValueError("invalid inner request headers")
            headers = {str(k): str(v) for k, v in raw_headers.items()
                       if str(k).lower() not in _DROP_HEADERS}
            body = envelope.get("body") or b""

            if not self._claim_request(device_id, cid, method, parsed.path):
                next_seq = self._send_head(
                    ws, rid, key, cid, session, 409, {"content-type": "application/json"})
                next_seq = self._send_data(
                    ws, rid, key, cid, session, next_seq,
                    json.dumps({"error": "duplicate_request",
                                "message": "This operation was already accepted; reopen its session "
                                           "instead of running it again."}).encode())
                # The encrypted stream itself completed cleanly; 409 is the authenticated HTTP
                # result.  Marking the terminal as failed would mask that status on native clients
                # and turn the useful "already accepted" outcome into a generic protocol error.
                self._send_terminal(ws, rid, key, cid, session, next_seq, ok=True,
                                    last_data_seq=next_seq - 1)
                ws.send_text(json.dumps({"t": "end", "id": rid}))
                return

            # generous timeout: an SSE run can have long quiet gaps (e.g. a slow bash tool call)
            # between frames; a short timeout would sever the phone's stream mid-run.
            headers["X-Collie-Relay"] = "1"   # tag as relay-replayed so the server withholds the raw CSRF token from pages
            conn = http.client.HTTPConnection(self.local_host, self.local_port, timeout=3600)
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()

            resp_headers = {k: v for k, v in resp.getheaders() if k.lower() not in _DROP_HEADERS}
            next_seq = self._send_head(ws, rid, key, cid, session, resp.status, resp_headers)
            while True:
                # read1(): return as soon as ANY bytes are available, instead of blocking until the
                # full buffer fills. Essential for SSE — a long-lived stream (/api/mirror) or a slow
                # token trickle must forward frame-by-frame, not stall until 2 KB accumulates.
                chunk = resp.read1(_CHUNK)
                if not chunk:
                    break
                last_data_seq = next_seq
                next_seq = self._send_data(ws, rid, key, cid, session, next_seq, chunk)
            self._send_terminal(ws, rid, key, cid, session, next_seq, ok=True,
                                last_data_seq=last_data_seq)
            ws.send_text(json.dumps({"t": "end", "id": rid}))
        except WebSocketClosed:
            raise
        except Exception as e:                            # noqa: BLE001
            try:
                if key is not None and cid is not None and session is not None:
                    if next_seq == 0:
                        next_seq = self._send_head(
                            ws, rid, key, cid, session, 502,
                            {"content-type": "application/json"})
                    self._send_terminal(ws, rid, key, cid, session, next_seq, ok=False,
                                        last_data_seq=last_data_seq,
                                        error="desktop_request_failed")
                    ws.send_text(json.dumps({"t": "end", "id": rid}))
                else:
                    ws.send_text(json.dumps({"t": "err", "id": rid,
                                             "msg": "sealed request authentication failed"}))
            except Exception:
                pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _send_record(self, ws, kind, rid, key, cid, session, seq, record):
        from . import e2e
        payload = json.dumps(record, separators=(",", ":")).encode("utf-8")
        msg = {"t": kind, "id": rid, "seq": seq,
               "enc": json.dumps(e2e.seal_chunk(
                   key, payload, room=self.room, frame_id=cid, session=session, seq=seq),
                   separators=(",", ":"))}
        if kind == "res":
            msg.update({"status": 200, "headers": {"content-type": "application/octet-stream"}})
        ws.send_text(json.dumps(msg, separators=(",", ":")))

    def _send_head(self, ws, rid, key, cid, session, status, headers):
        self._send_record(ws, "res", rid, key, cid, session, 0,
                          {"kind": "head", "status": int(status), "headers": headers})
        return 1

    def _send_data(self, ws, rid, key, cid, session, seq, payload):
        self._send_record(ws, "chunk", rid, key, cid, session, seq,
                          {"kind": "data", "data_b64": base64.b64encode(payload).decode("ascii")})
        return seq + 1

    def _send_terminal(self, ws, rid, key, cid, session, seq, *, ok,
                       last_data_seq=0, error=""):
        record = {"kind": "terminal", "ok": bool(ok), "last_data_seq": int(last_data_seq)}
        if error:
            record["error"] = error
        self._send_record(ws, "chunk", rid, key, cid, session, seq, record)

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
        self.relay_url = _validated_relay_url(relay_url)
        relay_parts = urllib.parse.urlsplit(self.relay_url)
        web_scheme = "https" if relay_parts.scheme == "wss" else "http"
        self.web_base = urllib.parse.urlunsplit((web_scheme, relay_parts.netloc, "", "", ""))
        self.local_port = local_port
        self.local_token = local_token
        self._log = logf or (lambda *a: None)
        self.identity = remote_identity.load_or_create()
        self.paircode = None
        self.pairing_secret = None
        self._paircode_at = 0.0
        self.client: RelayClient | None = None
        self._thread = None
        self.enabled = False
        self._notification_store = None
        self._notification_pump = None

    def _start_notification_pump(self):
        """Drain the shared durable outbox while the relay is available.

        A false return from ``notify`` is a retry, never success. Keeping this pump attached to the
        RemoteState (rather than the supervisor) ensures there is only one relay socket owner and
        that queued alerts resume automatically after that socket reconnects.
        """
        if self._notification_pump is not None:
            return
        try:
            from .ops import (NotificationPump, OpsStore, remote_notification_sender)
            self._notification_store = OpsStore()
            self._notification_pump = NotificationPump(
                self._notification_store, remote_notification_sender(self), interval_s=5).start()
        except Exception as exc:
            self._notification_store = self._notification_pump = None
            self._log("notification outbox: could not start: %s" % exc)

    def drain_notifications(self):
        """Synchronously attempt one outbox batch (also useful to authenticated status surfaces)."""
        pump = self._notification_pump
        return pump.step() if pump is not None else {"sent": 0, "retried": 0, "dead": 0}

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
        if not self.pairing_secret or not self.client:
            return None
        payload = json.dumps({
            "v": 2,
            "room": self.identity.room,
            "secret": self.pairing_secret,
            "desktop_pub": self.client.e2e_public_b64(),
        }, separators=(",", ":")).encode("utf-8")
        fragment = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return "%s/r/%s#pair=%s" % (self.web_base, self.identity.room, fragment)

    def start(self):
        import threading
        if self.enabled:
            return
        self._new_pairing_secret()
        self.client = RelayClient(self.relay_url, self.identity, self.pairing_secret,
                                  "127.0.0.1", self.local_port, self.local_token, self._log,
                                  pairing_expires_at=self._paircode_at + self.CODE_TTL)
        self.client.on_pair = self.rotate_code       # one-shot: rotate when a proof validates
        self._thread = threading.Thread(target=self.client.run_forever, name="collie-relay", daemon=True)
        self._thread.start()
        self.enabled = True
        self._start_notification_pump()

    def stop(self):
        if self._notification_pump:
            self._notification_pump.stop()
            self._notification_pump = None
        if self._notification_store:
            try:
                self._notification_store.close()
            except Exception:
                pass
            self._notification_store = None
        if self.client:
            self.client.stop()
        self.enabled = False

    # QR secrets expire quickly as well as being one-shot.  They have 256 bits of entropy, but a
    # screenshot is still a bearer of the pairing proof until it expires.
    CODE_TTL = 180

    def code_age(self):
        import time
        return time.time() - (self._paircode_at or 0)

    def _maybe_expire(self):
        """Rotate a code that has gone stale. Called wherever the code is read or shown, so the
        window is real rather than nominal: an unattended pairing screen refreshes itself."""
        if self.enabled and self.pairing_secret and self.code_age() > self.CODE_TTL:
            self.rotate_code()
            self._log("relay: pairing code expired after %ds — a fresh one is on the pairing screen"
                      % self.CODE_TTL)
            return True
        return False

    def decide_pair(self, allow: bool) -> bool:
        """Answer the verified phone waiting on this exact four-digit transcript fingerprint."""
        cl = self.client
        p = getattr(cl, "pending_pair", None) if cl else None
        if not p:
            return False
        cl._reply_pair(p["ws"], p["id"], allow)
        self._log("relay: %s %s" % (p.get("name") or "device", "approved" if allow else "denied"))
        cl.pending_pair = None
        return True

    def notify(self, title, body, session="", thread="collie") -> bool:
        """Push a notice to the paired phones. Silent no-op when remote is off or nothing is paired —
        the caller is a run finishing, and a run must not care whether anyone is listening."""
        cl = self.client
        if not self.enabled or cl is None:
            return False
        try:
            return bool(cl.notify(title, body, session=session, thread=thread))
        except Exception:
            return False

    def rotate_code(self):
        self._new_pairing_secret()
        if self.client:
            self.client.pairing_secret = self.pairing_secret
            self.client.pairing_expires_at = self._paircode_at + self.CODE_TTL
        return self.paircode

    def _new_pairing_secret(self):
        self.pairing_secret = secrets.token_urlsafe(32)
        self._paircode_at = time.time()
        # A local visual fingerprint for the desktop panel.  It is not accepted by the relay and is
        # not enough to pair; the QR carries the full secret.  The per-request comparison number is
        # derived later from both public keys and shown on both devices.
        digest = hashlib.sha256((self.identity.room + "\0" + self.pairing_secret).encode()).digest()
        self.paircode = "%04d" % (int.from_bytes(digest[:4], "big") % 10000)

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
            "protocol": 2,
            "pairing": "qr_only",
            "code_age": int(self.code_age()) if self.pairing_secret else 0,
            "code_ttl": self.CODE_TTL,
            "enabled": self.enabled,
            "connected": bool(self.client and self.client._ws is not None and self.enabled),
            "inflight": int(getattr(self.client, "_inflight_count", 0) or 0),
            "inflight_limit": int(getattr(self.client, "_inflight_limit", 0) or 0),
            "relay": self.relay_url,
            "room": self.identity.room,
            "link": self.link(),
            "paircode": self.paircode,
            "devices": self.identity.devices(),
        }
