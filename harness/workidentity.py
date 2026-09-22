"""First-party work-identity connections with explicit operational authority.

Connection records contain only public metadata.  Google credentials stay in the
user's Chrome profile; Collie Mail private keys stay in ``mail.json``; and one-time
codes live only in the stack frame that moves one fresh code into the already-open
verification form.
"""
from __future__ import annotations

import json
import os
import re
import time


VOICE_SPACE = "connection.google_voice"
VOICE_ORIGIN = "https://voice.google.com"
MAIL_SPACE = "connection.collie_mail"
_REFERENCE = re.compile(r"\{\{work_identity:([a-z0-9_]+):(account)\}\}")


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


def _mail_public(state_dir=None):
    """Discover the already-provisioned dog mailbox without copying its keys.

    Dog Mail predates the Connections panel and owns its own encrypted identity
    store.  Treating only ``work-identities.json`` as authoritative made a real
    ``@collie.run`` mailbox invisible to Mission, which in turn sent the planner
    down unnecessary consumer-email signup flows.
    """
    path = os.path.join(_root(state_dir), "mail.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, ValueError):
        data = {}
    dogs = data.get("dogs") if isinstance(data, dict) else {}
    rows = []
    for name, dog in sorted((dogs or {}).items()):
        if not isinstance(dog, dict):
            continue
        address = str(dog.get("address") or "").strip()
        if address:
            rows.append({"name": str(name), "address": address})
    account = rows[0]["address"] if rows else ""
    return {
        "id": "collie_mail",
        "label": "Collie Mail",
        "connected": bool(rows),
        "account": account,
        "accounts": rows,
        "scopes": (["mail.receive", "signup.email_use",
                    "verification_code.read_and_fill"] if rows else []),
        "verified_at": int(os.path.getmtime(path)) if rows else 0,
        "description": ("Collie's encrypted @collie.run work mailbox for account signup, "
                        "verification mail, and routine service messages."),
    }


def public_connections(state_dir=None):
    row = (_load(state_dir).get("google_voice") or {})
    connected = bool(row.get("connected"))
    voice = {
        "id": "google_voice",
        "label": "Google Voice",
        "connected": connected,
        "account": ("•••-•••-%s" % row.get("last4")) if connected else "",
        "scopes": list(row.get("scopes") or []),
        "verified_at": int(row.get("verified_at") or 0),
        "description": "Collie's assigned work line for messages, calls, voicemail, codes, and routine Voice operation.",
    }
    return [_mail_public(state_dir), voice]


def connected_context(state_dir=None):
    """Privacy-safe facts that the Mission planner may actually rely on."""
    return {
        "connections": [
            {**{key: row.get(key) for key in
                ("id", "label", "scopes", "description")
                if row.get(key) not in (None, "", [])},
             **({"reference": "{{work_identity:%s:account}}" % row["id"]}
                if row["id"] == "collie_mail" else {})}
            for row in public_connections(state_dir) if row.get("connected")
        ]
    }


def is_reference(value) -> bool:
    """True only for the small, non-secret reference grammar Mission may persist."""
    return bool(_REFERENCE.fullmatch(str(value or "").strip()))


def resolve_references(value, state_dir=None):
    """Resolve public work-identity metadata at the last responsible moment.

    The reference is safe in Mission state.  Its value exists only in the
    capability stack frame and the browser child prompt, then is redacted from
    all returned evidence.
    """
    text = str(value or "")
    rows = {row["id"]: row for row in public_connections(state_dir)
            if row.get("connected")}

    def replace(match):
        connection, field = match.groups()
        row = rows.get(connection) or {}
        resolved = str(row.get(field) or "")
        if not resolved:
            raise RuntimeError("connected work identity %s is unavailable" % connection)
        return resolved

    return _REFERENCE.sub(replace, text)


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
        return next(row for row in public_connections(state_dir)
                    if row["id"] == "google_voice")
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
    return next(row for row in public_connections(state_dir)
                if row["id"] == "google_voice")


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


def _mail_code(message, service=""):
    """Extract a likely OTP without ever returning or persisting the message."""
    message = message if isinstance(message, dict) else {}
    subject = str(message.get("subject") or "")
    sender = str(message.get("from") or "")
    body = str(message.get("text") or message.get("raw") or "")
    haystack = "\n".join((subject, sender, body))
    service = str(service or "").strip().casefold()
    if service and service not in haystack.casefold():
        # A platform may omit its display name from the subject, so this lowers
        # confidence rather than rejecting the only fresh code-bearing message.
        service_match = False
    else:
        service_match = True
    contextual = re.search(
        r"(?:verification|security|one[ -]?time|otp|code|验证码|驗證碼|校验码|確認碼)"
        r"[^0-9]{0,40}([0-9]{4,8})(?![0-9])", haystack, re.I)
    if contextual:
        return contextual.group(1), service_match
    standalone = re.findall(r"(?<![0-9])([0-9]{6})(?![0-9])", haystack)
    return (standalone[0], service_match) if len(set(standalone)) == 1 else ("", False)


def take_collie_mail_code(service, max_age_seconds=600, state_dir=None):
    """Wait briefly for one fresh code in the provisioned ``@collie.run`` inbox."""
    mail = _mail_public(state_dir)
    if not mail.get("connected"):
        raise RuntimeError("Collie Mail verification inbox is not connected")
    # dogmail uses the active state directory.  Mission and the installed runtime
    # share that directory; tests may point it elsewhere through COLLIE_STATE_DIR.
    from . import dogmail
    deadline = time.time() + min(180, max(10, int(max_age_seconds or 600)))
    best = None
    while time.time() < deadline:
        for message in dogmail.fetch():
            received = int(message.get("at") or 0)
            if received and received < int(time.time()) - max(60, int(max_age_seconds or 600)):
                continue
            code, service_match = _mail_code(message, service)
            if code and service_match:
                return code, {"source": "collie_mail", "account": mail["account"],
                              "received_at": received}
            if code and best is None:
                best = (code, received)
        if best:
            code, received = best
            return code, {"source": "collie_mail", "account": mail["account"],
                          "received_at": received}
        time.sleep(min(5.0, max(0.25, deadline - time.time())))
    raise RuntimeError("no fresh matching verification code found in Collie Mail")


def take_verification_code(service, max_age_seconds=600, state_dir=None, channel=""):
    """Route a requested code to the connected inbox used by the current form."""
    channel = str(channel or "").strip().lower()
    if channel in ("email", "mail", "collie_mail"):
        return take_collie_mail_code(service, max_age_seconds, state_dir)
    if channel in ("sms", "phone", "voice", "google_voice"):
        return take_google_voice_code(service, max_age_seconds, state_dir)
    # Preserve the historical Voice default for old decisions.  New Mission
    # prompts require an explicit channel so an email signup never scans SMS.
    if (_load(state_dir).get("google_voice") or {}).get("connected"):
        return take_google_voice_code(service, max_age_seconds, state_dir)
    return take_collie_mail_code(service, max_age_seconds, state_dir)
