"""Lease-based Collie presence client.

The Cloudflare relay is the outside observer: a machine that loses power cannot announce that it
went offline, so each healthy dog renews a short server-side lease instead.  This client only owns
the outbound WebSocket and its lifecycle; the relay owns expiry and the public online/offline view.

Authentication stays in the WebSocket ``Authorization`` header.  In particular, the bearer token
is never put in the URL, a wire message, or a log line.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping

from .wsclient import WebSocketClient, WebSocketClosed


_OK_WORDS = {"ok", "healthy", "online", "ready", "up", "available", "busy", "running"}
_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]*$")


def normalize_health(value) -> str:
    """Collapse a local health result to the deliberately small wire vocabulary.

    Integrations may return a bool, a status string, or a mapping of component checks.  Unknown
    values fail closed to ``degraded``: a green dot should mean we positively know the listener and
    worker are usable, not merely that their process still exists.
    """
    if isinstance(value, str):
        return "ok" if value.strip().lower() in _OK_WORDS else "degraded"
    if isinstance(value, bool):
        return "ok" if value else "degraded"
    if isinstance(value, Mapping):
        checks = [item for item in value.values() if isinstance(item, bool)]
        if checks and not all(checks):
            return "degraded"
        for key in ("health", "status"):
            if key in value:
                return normalize_health(value[key])
        return "ok" if checks and all(checks) else "degraded"
    return "degraded"


class PresenceClient:
    """Maintain one dog's authenticated presence lease in a background-safe loop.

    ``health_fn`` is sampled for *every* heartbeat.  This is important: a Slack socket can remain
    connected after its worker thread has died, and that dog must be reported as degraded.

    ``connect`` and ``waiter`` are injectable so tests do not need a network or real sleeps.
    ``connected`` is a :class:`threading.Event`; ``last_error`` is a sanitized string or ``None``.
    """

    HEARTBEAT_S = 20.0
    BACKOFF_INITIAL_S = 1.0
    BACKOFF_MAX_S = 30.0
    CONNECT_TIMEOUT_S = 20.0

    def __init__(
        self,
        base_url: str,
        pack: str,
        dog: str,
        token: str,
        *,
        session: str | None = None,
        health="ok",
        health_fn: Callable[[], object] | None = None,
        heartbeat_s: float = HEARTBEAT_S,
        backoff_initial_s: float = BACKOFF_INITIAL_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        connect=None,
        waiter: Callable[[float], bool] | None = None,
        random_fn: Callable[[], float] | None = None,
        clock: Callable[[], float] | None = None,
        logf=None,
    ):
        self.base_url = self._validate_base_url(base_url)
        self.pack = self._identity("pack", pack, 128)
        self.dog = self._identity("dog", dog, 80)
        self.session = self._identity("session", session or uuid.uuid4().hex, 128, 8)
        if (not isinstance(token, str) or not token or len(token) > 512 or
                any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token)):
            # The connector writes headers directly onto the HTTP upgrade. Restrict credentials to
            # the relay's visible-ASCII bearer grammar so a malformed kennel value cannot inject a
            # second handshake header (the enrolled random token is base64url and already fits).
            raise ValueError("presence token must be a visible-ASCII bearer value")
        if heartbeat_s <= 0 or backoff_initial_s <= 0 or backoff_max_s <= 0:
            raise ValueError("presence timing values must be positive")
        if backoff_initial_s > backoff_max_s:
            raise ValueError("initial presence backoff exceeds its maximum")

        self._token = token
        self._health = health
        self._health_fn = health_fn
        self.heartbeat_s = float(heartbeat_s)
        self.backoff_initial_s = float(backoff_initial_s)
        self.backoff_max_s = float(backoff_max_s)
        self.connect_timeout_s = float(connect_timeout_s)
        self._connect = connect or WebSocketClient.connect
        self._waiter = waiter
        self._random = random_fn or random.random
        self._clock = clock or time.time
        self._log = logf or (lambda *_args: None)

        self.connected = threading.Event()
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._heartbeat_lock = threading.Lock()
        self._ws = None
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._connection_generation = 0

    @staticmethod
    def _identity(label: str, value, maximum: int, minimum: int = 1) -> str:
        text = str(value or "").strip()
        if not (minimum <= len(text) <= maximum) or not _WIRE_ID.fullmatch(text):
            raise ValueError("invalid presence %s" % label)
        return text

    @staticmethod
    def _validate_base_url(url: str) -> str:
        text = str(url or "").strip().rstrip("/")
        parsed = urllib.parse.urlsplit(text)
        if parsed.scheme != "wss" or not parsed.hostname:
            raise ValueError("presence requires a wss:// relay URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("presence relay URL cannot contain credentials or a fragment")
        return text

    @property
    def url(self) -> str:
        """WebSocket URL with routing identity, but never authentication material."""
        parsed = urllib.parse.urlsplit(self.base_url)
        path = parsed.path.rstrip("/")
        if not path.endswith("/presence/ws"):
            path += "/presence/ws"
        query = [(key, value) for key, value in urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True
        ) if key not in {"pack", "dog", "session"}]
        query.extend((("pack", self.pack), ("dog", self.dog), ("session", self.session)))
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path,
                                        urllib.parse.urlencode(query), ""))

    def start(self) -> threading.Thread:
        """Start the reconnect loop once and return its daemon thread."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._thread
            if self._stop.is_set():
                raise RuntimeError("a stopped PresenceClient cannot be restarted")
            self._thread = threading.Thread(
                target=self.run_forever,
                name="collie-presence-%s" % self.dog,
                daemon=True,
            )
            self._thread.start()
            return self._thread

    def run_forever(self):
        """Connect, heartbeat, and redial with capped exponential backoff until stopped."""
        backoff = self.backoff_initial_s
        while not self._stop.is_set():
            generation = self._connection_generation
            try:
                self._connect_and_serve()
            except Exception as exc:  # noqa: BLE001 - a presence loop must stay alive
                if not self._stop.is_set():
                    self._record_error(exc)
            finally:
                self.connected.clear()
                with self._lock:
                    self._ws = None

            if self._stop.is_set():
                break
            # A completed handshake is a recovery boundary. Without this reset, a daemon that has
            # survived a few unrelated network changes eventually waits the full 30 seconds after
            # every later disconnect forever, even after hours of healthy service.
            recovered = self._connection_generation != generation
            if recovered:
                backoff = self.backoff_initial_s
            # A little downward-only jitter prevents a rebooted fleet from redialing in lockstep,
            # while preserving a hard upper bound suitable for tests and operations.
            jitter = 0.8 + 0.2 * min(1.0, max(0.0, float(self._random())))
            delay = min(backoff, self.backoff_max_s) * jitter
            if self._wait(delay):
                break
            if not recovered:
                backoff = min(backoff * 2.0, self.backoff_max_s)

    def stop(self, *, send_bye: bool = True, join_timeout: float = 2.0):
        """Stop promptly, best-effort announce ``bye``, and close the active socket.

        A crash, power loss, or severed network cannot send ``bye``; the relay's lease expiry is the
        authoritative fallback for those cases.
        """
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws is not None:
            if send_bye:
                try:
                    # Do not let a periodic heartbeat overtake bye on another thread. The server
                    # correctly fences it, but ordering it here avoids turning a clean stop into a
                    # protocol error and makes graceful removal immediate.
                    with self._heartbeat_lock:
                        ws.send_text(self._encode("bye", health=self._sample_health()))
                except Exception:
                    pass
            try:
                ws.close()
            except Exception:
                pass
        self.connected.clear()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0.0, join_timeout))

    def update_health(self, health):
        """Update the fallback health used when no ``health_fn`` is configured."""
        with self._lock:
            self._health = health

    def heartbeat_now(self) -> bool:
        """Renew the active lease immediately; return false while disconnected or on send failure."""
        with self._lock:
            ws = self._ws
        if ws is None or not self.connected.is_set() or self._stop.is_set():
            return False
        try:
            self._send_heartbeat(ws)
            return True
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            try:
                ws.close()
            except Exception:
                pass
            return False

    def _wait(self, delay: float) -> bool:
        if self._waiter is not None:
            return bool(self._waiter(delay)) or self._stop.is_set()
        return self._stop.wait(delay)

    def _connect_and_serve(self):
        # The URL contains routing identity only.  Bearer auth is deliberately header-only.
        ws = self._connect(
            self.url,
            headers={"Authorization": "Bearer " + self._token},
            timeout=self.connect_timeout_s,
        )
        if self._stop.is_set():
            ws.close()
            return
        with self._lock:
            self._ws = ws

        beat_stop = threading.Event()
        beat_error = []
        try:
            ws.send_text(self._encode("hello", health=self._sample_health()))
            self._send_heartbeat(ws)  # establish the first lease without waiting one interval
            self.last_error = None
            self.connected.set()
            self._connection_generation += 1
            self._safe_log("presence: connected (pack=%s dog=%s)" % (self.pack, self.dog))

            def beat():
                while not beat_stop.wait(self.heartbeat_s):
                    if self._stop.is_set():
                        return
                    try:
                        self._send_heartbeat(ws)
                    except Exception as exc:  # noqa: BLE001
                        beat_error.append(exc)
                        try:
                            ws.close()  # wake recv_message so the outer loop can redial
                        except Exception:
                            pass
                        return

            threading.Thread(
                target=beat,
                name="collie-presence-heartbeat-%s" % self.dog,
                daemon=True,
            ).start()

            while not self._stop.is_set():
                kind, data = ws.recv_message()
                if kind != "text":
                    continue
                try:
                    message = json.loads(data)
                except (TypeError, ValueError):
                    continue
                if message.get("t") == "ping":
                    ws.send_text(self._encode("pong"))
                elif message.get("t") == "error":
                    raise RuntimeError("presence relay rejected connection: %s"
                                       % message.get("error", "unknown error"))
        except WebSocketClosed:
            if beat_error:
                raise beat_error[0]
            raise
        finally:
            beat_stop.set()
            self.connected.clear()
            try:
                ws.close()
            except Exception:
                pass
            with self._lock:
                if self._ws is ws:
                    self._ws = None

    def _sample_health(self) -> str:
        try:
            if self._health_fn is not None:
                value = self._health_fn()
            else:
                with self._lock:
                    value = self._health
            return normalize_health(value)
        except Exception as exc:  # noqa: BLE001 - failed checks are themselves degraded health
            self._record_error(RuntimeError("presence health check failed: %s" % exc))
            return "degraded"

    def _send_heartbeat(self, ws):
        # heartbeat_now() (Slack socket edges) and the periodic heartbeat thread can run together.
        # Allocate the sequence and put it on the wire under one lock; otherwise seq=N+1 can arrive
        # before seq=N and the relay will correctly fence this client as a replay.
        with self._heartbeat_lock:
            with self._lock:
                self._seq += 1
                seq = self._seq
            ws.send_text(self._encode("heartbeat", seq=seq, health=self._sample_health()))

    def _encode(self, kind: str, **extra) -> str:
        message = {
            "t": kind,
            "v": 1,
            "pack": self.pack,
            "dog": self.dog,
            "session": self.session,
            "ts": int(self._clock()),
        }
        message.update(extra)
        return json.dumps(message, ensure_ascii=False, separators=(",", ":"))

    def _record_error(self, exc):
        message = str(exc) or type(exc).__name__
        # A connector or health callback might carelessly echo its inputs in an exception.  Keep
        # both the public status and logs safe even in that case.
        message = message.replace(self._token, "[redacted]")
        self.last_error = message
        self._safe_log("presence: %s" % message)

    def _safe_log(self, message: str):
        # Defence in depth for custom log functions: no call site should contain the token, and this
        # final replacement guarantees it even if a future one accidentally does.
        self._log(str(message).replace(self._token, "[redacted]"))


__all__ = ["PresenceClient", "normalize_health"]
