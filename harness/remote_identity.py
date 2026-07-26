"""Persistent identity + paired-device store for Collie Remote (the desktop side).

The desktop — not the ephemeral relay/DO — is the source of truth for "who is paired". That's what
makes pairing durable: the phone pairs once, and it keeps working even after the desktop was off for
a day, because on every reconnect the desktop re-registers its paired-device set to the relay.

Stored at ~/.collie/remote.json (honouring $COLLIE_STATE_DIR), 0600:
  {
    "device_id": "<stable random>",       # this desktop's identity
    "room":      "<stable slug>",          # stable relay room → phone URL is bookmarkable
    "agent_key": "<stable secret>",        # proves this desktop owns the room (AGENTKEY)
    "devices": { "<sha256(token)>": {"name":..,"paired_at":..,"last_seen":..} }
  }
Session tokens themselves are NEVER stored — only their SHA-256, so the file leaking can't grant
access. The plaintext token lives only in the phone's cookie.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time


def _state_dir() -> str:
    return os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")


def _path() -> str:
    return os.path.join(_state_dir(), "remote.json")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Crockford-ish base32 (no I/L/O/U) — ~40 bits at n=8, human-typable. Only used to add a NEW device.
_PAIR_ALPHA = "23456789ABCDEFGHJKMNPQRSTVWXYZ"


def gen_paircode(n: int = 8) -> str:
    return "".join(secrets.choice(_PAIR_ALPHA) for _ in range(n))


class Identity:
    def __init__(self, data: dict, path: str):
        self._d = data
        self._path = path

    @property
    def device_id(self) -> str: return self._d["device_id"]
    @property
    def room(self) -> str: return self._d["room"]
    @property
    def agent_key(self) -> str: return self._d["agent_key"]

    # devices are keyed by a client-supplied stable device_id (localStorage / Keychain), NOT by the
    # session-token hash — so the SAME client re-pairing UPDATES its entry (new token) instead of
    # spawning a duplicate. entry = {name, token_sha, paired_at, last_seen}.

    def device_hashes(self) -> list[str]:
        """Current valid session-token hashes (one per device). The desktop re-registers this set to
        the relay on connect; a re-paired device's OLD hash is gone because it was replaced in place."""
        return [v.get("token_sha") for v in self._d.get("devices", {}).values() if v.get("token_sha")]

    def add_or_update(self, device_id: str, token_sha: str, name: str = ""):
        """Pair or re-pair `device_id`. Re-pairing keeps the entry (and its custom name) and just
        swaps in the fresh token hash — so no duplicate row appears."""
        if not device_id:
            device_id = token_sha            # legacy client with no device_id → key by hash
        devs = self._d.setdefault("devices", {})
        now = int(time.time())
        e = devs.get(device_id)
        if e:
            e["token_sha"] = token_sha
            e["last_seen"] = now
            if name and not e.get("name"):
                e["name"] = name
        else:
            devs[device_id] = {"name": name or "device", "token_sha": token_sha,
                               "paired_at": now, "last_seen": now}
        self._save()

    def rename(self, device_id: str, name: str) -> bool:
        e = self._d.get("devices", {}).get(device_id)
        if e is None:
            return False
        e["name"] = name
        self._save()
        return True

    def forget_device(self, device_id: str) -> bool:
        devs = self._d.setdefault("devices", {})
        if device_id in devs:
            del devs[device_id]
            self._save()
            return True
        return False

    def forget_all(self):
        self._d["devices"] = {}
        self._save()

    def devices(self) -> list:
        """List of {device_id, name, paired_at, last_seen} (token_sha omitted — never leaves here)."""
        return [{"device_id": k, "name": v.get("name", "device"),
                 "paired_at": v.get("paired_at"), "last_seen": v.get("last_seen")}
                for k, v in self._d.get("devices", {}).items()]

    def _save(self):
        _atomic_write(self._path, self._d)


def load_or_create() -> Identity:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            data = None
    if not data or "device_id" not in data:
        data = {
            "device_id": secrets.token_urlsafe(12),
            # a stable, unguessable room slug (~72 bits) → the phone URL never changes for this desktop
            "room": secrets.token_urlsafe(12),
            "agent_key": secrets.token_urlsafe(32),
            "devices": {},
        }
        _atomic_write(path, data)
    else:
        # migrate v0 device entries (keyed by token hash, no token_sha field) → keep them valid by
        # setting token_sha = the key, so already-paired devices survive the upgrade without re-pairing.
        changed = False
        for k, v in (data.get("devices") or {}).items():
            if isinstance(v, dict) and "token_sha" not in v:
                v["token_sha"] = k
                changed = True
        if changed:
            _atomic_write(path, data)
    return Identity(data, path)


def _atomic_write(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp, 0o600)      # best-effort on Windows; a real no-op there but harmless
    except OSError:
        pass
    os.replace(tmp, path)
