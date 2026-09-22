"""Least-authority A2A envelopes and live-session supervision controls."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .runner_specs import QueuedTurnMessage, redact_text, redact_value


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class DelegationScope:
    """Authority a child agent may use; omission means denial."""

    tools: frozenset[str] = frozenset()
    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    network_hosts: frozenset[str] = frozenset()
    max_cost_usd: float = 0.0
    max_tokens: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.max_cost_usd, bool):
            raise ValueError("delegation max_cost_usd must be finite and non-negative")
        cost = float(self.max_cost_usd)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("delegation max_cost_usd must be finite and non-negative")
        if (isinstance(self.max_tokens, bool)
                or not isinstance(self.max_tokens, int) or self.max_tokens < 0):
            raise ValueError("delegation max_tokens must be non-negative")
        object.__setattr__(self, "max_cost_usd", cost)
        object.__setattr__(self, "max_tokens", int(self.max_tokens))
        object.__setattr__(self, "tools", frozenset(str(x) for x in self.tools if str(x)))
        object.__setattr__(self, "network_hosts", frozenset(
            str(x).encode("idna").decode("ascii").lower().rstrip(".")
            for x in self.network_hosts if str(x)))
        object.__setattr__(self, "read_roots", self._roots(self.read_roots))
        object.__setattr__(self, "write_roots", self._roots(self.write_roots))

    @staticmethod
    def _roots(values: tuple[str, ...]) -> tuple[str, ...]:
        roots = []
        for value in values:
            value = str(value or "")
            if not value or "\x00" in value:
                raise ValueError("delegation resource root must be a non-empty path")
            roots.append(os.path.realpath(os.path.abspath(value)))
        return tuple(dict.fromkeys(roots))

    def is_subset_of(self, parent: "DelegationScope") -> bool:
        def covered(child: str, parents: tuple[str, ...]) -> bool:
            for root in parents:
                try:
                    if os.path.commonpath([child, root]) == root:
                        return True
                except ValueError:
                    continue
            return False
        return bool(
            self.tools <= parent.tools and
            self.network_hosts <= parent.network_hosts and
            self.max_cost_usd <= parent.max_cost_usd and
            self.max_tokens <= parent.max_tokens and
            all(covered(root, parent.read_roots + parent.write_roots)
                for root in self.read_roots) and
            all(covered(root, parent.write_roots) for root in self.write_roots))

    def to_dict(self) -> dict[str, Any]:
        return {"tools": sorted(self.tools), "read_roots": list(self.read_roots),
                "write_roots": list(self.write_roots),
                "network_hosts": sorted(self.network_hosts),
                "max_cost_usd": self.max_cost_usd,
                "max_tokens": self.max_tokens}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DelegationScope":
        if not isinstance(value, dict):
            raise TypeError("delegation scope must be an object")
        return cls(tools=frozenset(str(item) for item in value.get("tools") or ()),
                   read_roots=tuple(str(item) for item in
                                    value.get("read_roots") or ()),
                   write_roots=tuple(str(item) for item in
                                     value.get("write_roots") or ()),
                   network_hosts=frozenset(str(item) for item in
                                           value.get("network_hosts") or ()),
                   max_cost_usd=value.get("max_cost_usd", 0.0),
                   max_tokens=value.get("max_tokens", 0))


@dataclass(frozen=True)
class DelegationEnvelope:
    version: int
    delegation_id: str
    parent_id: str
    issuer: str
    audience: str
    objective: str
    scope: DelegationScope
    issued_at: int
    expires_at: int
    nonce: str
    signature: str = ""

    @classmethod
    def issue(cls, *, secret: bytes, parent_id: str, issuer: str,
              audience: str, objective: str, scope: DelegationScope,
              ttl_s: int = 900, now: int | None = None,
              delegation_id: str = "") -> "DelegationEnvelope":
        if len(secret) < 32:
            raise ValueError("delegation signing secret must contain at least 32 bytes")
        if not str(parent_id or "").strip() or not str(audience or "").strip():
            raise ValueError("delegation parent_id and audience must be non-empty")
        if not str(objective or "").strip():
            raise ValueError("delegation objective must be non-empty")
        now = int(time.time()) if now is None else int(now)
        ttl_s = int(ttl_s)
        if ttl_s < 1 or ttl_s > 86_400:
            raise ValueError("delegation ttl_s must be between 1 and 86400")
        value = cls(version=1,
                    delegation_id=str(delegation_id or secrets.token_urlsafe(18)),
                    parent_id=str(parent_id or ""), issuer=str(issuer or "collie"),
                    audience=str(audience or ""),
                    objective=redact_text(objective, 8_000), scope=scope,
                    issued_at=now, expires_at=now + ttl_s,
                    nonce=secrets.token_hex(16))
        return cls(**{**value.__dict__, "signature": value._sign(secret)})

    def _unsigned(self) -> dict[str, Any]:
        return {"version": self.version, "delegation_id": self.delegation_id,
                "parent_id": self.parent_id, "issuer": self.issuer,
                "audience": self.audience, "objective": self.objective,
                "scope": self.scope.to_dict(), "issued_at": self.issued_at,
                "expires_at": self.expires_at, "nonce": self.nonce}

    def _sign(self, secret: bytes) -> str:
        return "v1=" + hmac.new(secret, _canonical(self._unsigned()),
                                 hashlib.sha256).hexdigest()

    def verify(self, *, secret: bytes, audience: str,
               parent_scope: DelegationScope, now: int | None = None,
               consume_nonce: Callable[[str, int], bool] | None = None) -> bool:
        now = int(time.time()) if now is None else int(now)
        if len(secret) < 32 or self.version != 1 or self.audience != audience:
            return False
        if self.issued_at > now + 60 or now >= self.expires_at:
            return False
        if not hmac.compare_digest(self.signature, self._sign(secret)):
            return False
        if not self.scope.is_subset_of(parent_scope):
            return False
        if consume_nonce is None:
            return False
        try:
            return bool(consume_nonce(self.nonce, self.expires_at))
        except Exception:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {**self._unsigned(), "signature": self.signature}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DelegationEnvelope":
        if not isinstance(value, dict):
            raise TypeError("delegation envelope must be an object")
        for name in ("version", "issued_at", "expires_at"):
            if isinstance(value.get(name), bool) or not isinstance(value.get(name), int):
                raise ValueError("delegation %s must be an integer" % name)
        strings = {}
        for name in ("delegation_id", "parent_id", "issuer", "audience",
                     "objective", "nonce", "signature"):
            item = value.get(name)
            if not isinstance(item, str) or not item.strip() or "\x00" in item:
                raise ValueError("delegation %s must be a non-empty string" % name)
            strings[name] = item
        return cls(version=value["version"], scope=DelegationScope.from_dict(
                       value.get("scope")), issued_at=value["issued_at"],
                   expires_at=value["expires_at"], **strings)


class LiveSessionSupervisor:
    """Bounded voice/UI control channel independent of audio transport.

    Voice clients submit only supervision intents here.  Audio, transcripts and
    credentials are deliberately outside this object; runner_slice can consume
    ``drain_turn_messages`` and ``cancelled`` directly.
    """

    def __init__(self, session_id: str, *, lease_s: float = 90.0,
                 max_messages: int = 64):
        self.session_id = str(session_id or "")
        if not self.session_id:
            raise ValueError("live session id must be non-empty")
        lease_s = float(lease_s)
        if not math.isfinite(lease_s) or lease_s < 5 or lease_s > 3600:
            raise ValueError("live session lease_s must be between 5 and 3600")
        if (isinstance(max_messages, bool) or not isinstance(max_messages, int)
                or max_messages < 1 or max_messages > 256):
            raise ValueError("live session max_messages must be between 1 and 256")
        self.lease_s = lease_s
        self.max_messages = max_messages
        self._lock = threading.Lock()
        self._messages: list[QueuedTurnMessage] = []
        self._cancelled = False
        self._paused = False
        self._heartbeat_at = time.monotonic()
        self._sequence = 0

    def heartbeat(self) -> None:
        with self._lock:
            self._heartbeat_at = time.monotonic()

    def submit(self, action: str, text: str = "", *, message_id: str = "") -> dict:
        action = str(action or "").lower()
        with self._lock:
            self._heartbeat_at = time.monotonic()
            self._sequence += 1
            if action == "cancel":
                self._cancelled = True
            elif action == "pause":
                self._paused = True
            elif action == "resume":
                self._paused = False
            elif action in ("steer", "follow_up"):
                if len(self._messages) >= self.max_messages:
                    raise RuntimeError("live supervision queue is full")
                self._messages.append(QueuedTurnMessage(
                    message_id=str(message_id or "%s:%d" %
                                   (self.session_id, self._sequence)),
                    mode=action, text=text, created_at=time.time()))
            else:
                raise ValueError("unsupported live supervision action")
            return self._state_locked()

    def cancelled(self) -> bool:
        with self._lock:
            return bool(self._cancelled or
                        time.monotonic() - self._heartbeat_at > self.lease_s)

    def drain_turn_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._paused:
                return []
            rows = [item.to_dict() for item in self._messages]
            self._messages.clear()
            return rows

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> dict[str, Any]:
        age = max(0.0, time.monotonic() - self._heartbeat_at)
        return redact_value({"session_id": self.session_id,
                             "cancelled": self._cancelled,
                             "paused": self._paused,
                             "queued": len(self._messages),
                             "heartbeat_age_s": round(age, 3),
                             "lease_expired": age > self.lease_s,
                             "sequence": self._sequence})


__all__ = ["DelegationEnvelope", "DelegationScope", "LiveSessionSupervisor"]
