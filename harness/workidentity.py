"""First-party work-identity connections with explicit operational authority.

Connection records contain only public metadata.  Google credentials stay in the
user's Chrome profile and one-time codes live only in the stack frame that moves
one fresh code from Voice into the already-open verification form.
"""
from __future__ import annotations

import json
import os
import time


VOICE_SPACE = "connection.google_voice"
VOICE_ORIGIN = "https://voice.google.com"


def _root(state_dir=None):
    if state_dir:
        return os.path.abspath(os.path.expanduser(state_dir))
    from .controlplane import state_dir as current_state_dir
    return current_state_dir()


def _path(state_dir=None):
    return os.path.join(_root(state_dir), "work-identities.json")


def _load(state_dir=None):
    try:
        with open(_path(state_dir), encoding="utf-8") as fh:
            data = json.load(fh) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data, state_dir=None):
    path = _path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        from . import plat
        plat.chmod_private(tmp)
    except Exception:
        pass
    os.replace(tmp, path)


def public_connections(state_dir=None):
    row = (_load(state_dir).get("google_voice") or {})
    connected = bool(row.get("connected"))
    return [{
        "id": "google_voice",
        "label": "Google Voice",
        "connected": connected,
        "account": ("•••-•••-%s" % row.get("last4")) if connected else "",
        "scopes": list(row.get("scopes") or []),
        "verified_at": int(row.get("verified_at") or 0),
        "description": "Collie's assigned work line for messages, calls, voicemail, codes, and routine Voice operation.",
    }]


def _bridge_data(result):
    from .browserbridge import _data
    data = _data(result)
    if data is None:
        error = result.get("error") if isinstance(result, dict) else "browser bridge unavailable"
        raise RuntimeError(error or "browser bridge command failed")
    return data


def connect_google_voice(expected_last4="", state_dir=None):
    """Attach the one open Voice tab and persist only its masked identity."""
    expected_last4 = "".join(ch for ch in str(expected_last4 or "") if ch.isdigit())[-4:]
    from . import browserbridge as bb
    attached = _bridge_data(bb._call({
        "action": "attach", "space": VOICE_SPACE, "origin": VOICE_ORIGIN}, timeout=10))
    try:
        identity = _bridge_data(bb._call({
            "action": "voice_identity", "space": VOICE_SPACE}, timeout=10))
        actual = str(identity.get("last4") or "")
        if len(actual) != 4:
            raise RuntimeError("Google Voice number was not visible on the connected page")
        if expected_last4 and actual != expected_last4:
            raise RuntimeError("the open Google Voice number does not match the requested ending")
        data = _load(state_dir)
        data["google_voice"] = {
            "connected": True, "last4": actual, "origin": VOICE_ORIGIN,
            "space": VOICE_SPACE, "scopes": [
                "voice.messages.read", "voice.messages.send",
                "voice.calls.place_receive", "voice.voicemail.read",
                "voice.settings.routine", "verification_code.read_and_fill",
            ],
            "verified_at": int(time.time()),
        }
        _save(data, state_dir)
        return public_connections(state_dir)[0]
    except Exception:
        if attached.get("attached"):
            bb._call({"action": "release", "space": VOICE_SPACE}, timeout=5)
        raise


def disconnect_google_voice(state_dir=None):
    from . import browserbridge as bb
    try:
        bb._call({"action": "release", "space": VOICE_SPACE}, timeout=5)
    except Exception:
        pass
    data = _load(state_dir)
    data.pop("google_voice", None)
    _save(data, state_dir)
    return public_connections(state_dir)[0]


def take_google_voice_code(service, max_age_seconds=600, state_dir=None):
    """Return one fresh matching code transiently; callers must never persist it."""
    row = _load(state_dir).get("google_voice") or {}
    if not row.get("connected"):
        raise RuntimeError("Google Voice verification-code connection is not connected")
    service = str(service or "").strip()
    if not service or len(service) > 100:
        raise RuntimeError("the expected service name is required")
    from . import browserbridge as bb
    data = _bridge_data(bb._call({
        "action": "google_voice_otp", "space": VOICE_SPACE,
        "service": service, "max_age_seconds": max(60, min(900, int(max_age_seconds))),
    }, timeout=10))
    code = str(data.pop("code", "") or "")
    if not code:
        raise RuntimeError(data.get("error") or "no fresh matching verification code found")
    return code, {"source": "google_voice", "account": "•••-•••-%s" % row.get("last4"),
                  "received_at": int(data.get("received_at") or 0)}
