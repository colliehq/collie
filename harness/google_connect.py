"""Collie's own Google connection: read Gmail, write Gmail drafts, read Google Calendar.

This is a first-party connector built on Collie's registered Google OAuth app (a *Desktop app*
client), not an MCP server: the morning report needs a handful of reads and one kind of write,
and a local connector with three fixed scopes is easier to reason about than a general tool
surface.

The sign-in is Google's installed-app flow (RFC 8252): a one-shot HTTP server on 127.0.0.1 and
a random port catches the redirect, PKCE (S256) binds the code to this process and ``state``
binds the redirect to this sign-in. Exactly three scopes are requested; the person can untick
any of them on Google's page, so what Google actually granted is recorded and every call checks
for its own scope first.

Where the refresh token lives: never in a plaintext JSON file. On Windows it is sealed with DPAPI
for the signed-in user (``CryptProtectData``), on macOS it is a login-Keychain item, and on other
systems it is an owner-only (0600) file beside the metadata. The metadata file
(``~/.collie/google-connection.json``) holds the account, the granted scopes and the sealed blob,
never the token itself. Access tokens are only ever held in memory.

Collie never sends mail from here. The module has no call that sends a message or a draft, and
tests/test_google_api.py fails if an endpoint that does ever appears in this file.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import plat, sessions
from .controlplane import state_dir as _state_dir
from .httpserver import ThreadingHTTPServer

GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
CALENDAR_READ = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES = (GMAIL_READ, GMAIL_COMPOSE, CALENDAR_READ)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
PERMISSIONS_PAGE = "https://myaccount.google.com/permissions"

_HERE = os.path.dirname(os.path.abspath(__file__))
#: Written at release time from the GOOGLE_OAUTH_CLIENT_JSON secret; .gitignored, never committed.
PACKAGED_CLIENT = os.path.join(_HERE, "google_oauth_client.json")
_LOGO = os.path.join(_HERE, "webui", "logo.svg")
USER_CLIENT_FILE = "google-oauth-client.json"
CONNECTION_FILE = "google-connection.json"
PLAINTEXT_FILE = "google-token.json"          # what the first prototype left behind

HTTP_TIMEOUT = 20
MAX_RESPONSE_BYTES = 12 * 1024 * 1024         # a long thread in format=full can be several MB
USER_AGENT = "Collie-Google/1.0 (+https://github.com/colliehq/collie)"

RECONNECT_HINT = "Run `collie google connect` (or press Connect in Settings → Connections)."
_WHAT = {GMAIL_READ: "read your Gmail", GMAIL_COMPOSE: "write Gmail drafts",
         CALENDAR_READ: "read your Google Calendar"}
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\.apps\.googleusercontent\.com\Z")


# ------------------------------------------------------------------------------------ errors

class GoogleError(RuntimeError):
    """A refusal or failure, with a stable ``code`` and a message fit to show a person.

    Messages never contain a token, the client secret or mail content.
    """
    default_code = "error"

    def __init__(self, code_or_message, message=None):
        if message is None:
            code, message = self.default_code, code_or_message
        else:
            code = code_or_message
        super().__init__(message)
        self.code = code


class NotConfigured(GoogleError):
    """No usable OAuth client: Collie's Google app is not set up on this machine."""
    default_code = "not_configured"


class NotConnected(GoogleError):
    default_code = "not_connected"


class NeedsReconnect(GoogleError):
    """The refresh token stopped working (expired, revoked, or a different OAuth client)."""
    default_code = "needs_reconnect"


class MissingScope(GoogleError):
    default_code = "missing_scope"

    def __init__(self, scope, message=None):
        super().__init__("missing_scope", message or (
            "Google didn't allow Collie to %s. Run `collie google connect` again and tick that "
            "box on Google's page (or press Connect in Settings → Connections)."
            % _WHAT.get(scope, scope)))
        self.scope = scope


class GoogleAPIError(GoogleError):
    default_code = "api_error"

    def __init__(self, status, message):
        super().__init__("api_error", message)
        self.status = status


# ------------------------------------------------------------------------------------ client

def _parse_client(path):
    with open(path, encoding="utf-8") as handle:
        raw = handle.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("the file is too large to be an OAuth client")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ValueError("the file is not JSON") from None
    inner = doc.get("installed") if isinstance(doc, dict) else None
    if not isinstance(inner, dict):
        raise ValueError('expected a Desktop-app client ({"installed": {...}})')
    client_id, secret = inner.get("client_id"), inner.get("client_secret")
    if not isinstance(client_id, str) or not _CLIENT_ID.match(client_id):
        raise ValueError("client_id is missing or is not a Google OAuth client id")
    if not isinstance(secret, str) or not secret or len(secret) > 512 or any(c.isspace() for c in secret):
        raise ValueError("client_secret is missing or malformed")
    return {"client_id": client_id, "client_secret": secret}


def client_config(*, state_dir=None) -> dict:
    """The OAuth client to use: ``COLLIE_GOOGLE_OAUTH_CLIENT`` (a path), then
    ``~/.collie/google-oauth-client.json``, then the file packaged with this release.

    Returns ``{"client_id", "client_secret", "source", "path"}``. The secret of a Desktop-app
    client is not confidential by Google's own definition, but it is still never printed.
    Raises NotConfigured.
    """
    explicit = os.environ.get("COLLIE_GOOGLE_OAUTH_CLIENT", "").strip()
    if explicit:
        path = os.path.abspath(os.path.expanduser(explicit))
        try:
            return dict(_parse_client(path), source="env", path=path)
        except (OSError, ValueError) as exc:
            raise NotConfigured("COLLIE_GOOGLE_OAUTH_CLIENT does not name a usable Google OAuth "
                                "client (%s)." % _plain_os_error(exc)) from None
    problems = []
    for source, path in (("user", os.path.join(_state_dir(state_dir), USER_CLIENT_FILE)),
                         ("packaged", PACKAGED_CLIENT)):
        if not os.path.isfile(path):
            continue
        try:
            return dict(_parse_client(path), source=source, path=path)
        except (OSError, ValueError) as exc:
            problems.append("%s: %s" % (os.path.basename(path), _plain_os_error(exc)))
    raise NotConfigured(
        "Collie's Google app isn't set up on this computer: no OAuth client was found%s. An "
        "official Collie build includes it; for a source checkout, save the Desktop-app client "
        "JSON as ~/.collie/%s." % ((" (" + "; ".join(problems) + ")") if problems else "",
                                  USER_CLIENT_FILE))


def _plain_os_error(exc):
    if isinstance(exc, FileNotFoundError):
        return "file not found"
    return str(exc) if isinstance(exc, ValueError) else exc.__class__.__name__


# ------------------------------------------------------------------------------------ HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib copies request headers onto a redirect, Authorization included. Google's APIs do not
    # redirect, so a redirect is an error rather than a reason to hand a bearer token elsewhere.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _send(method, url, headers=None, data=None, timeout=HTTP_TIMEOUT):
    """The one place a request leaves Collie for Google. Returns ``(status, body bytes)``.

    HTTP error statuses are returned, not raised; a failure to reach Google at all raises
    GoogleError("network"). Replaced wholesale by the tests.
    """
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    req.add_header("User-Agent", USER_AGENT)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise GoogleAPIError(resp.status, "Google's answer was larger than Collie reads "
                                                  "(%d MB)." % (MAX_RESPONSE_BYTES // 2 ** 20))
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(64 * 1024)
        except Exception:
            body = b""
        return exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise GoogleError("network", "Could not reach Google (%s). Check the internet connection "
                                     "and try again." % reason) from None


def _loads(body):
    try:
        value = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _google_error(body):
    """(message, reasons) from either Google error shape; never includes request data."""
    doc = _loads(body)
    err = doc.get("error")
    reasons = set()
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("status") or "")
        for key in ("details", "errors"):
            for row in err.get(key) or []:
                if isinstance(row, dict) and row.get("reason"):
                    reasons.add(str(row["reason"]))
        if err.get("status"):
            reasons.add(str(err["status"]))
    else:
        message = str(doc.get("error_description") or err or "")
        if err:
            reasons.add(str(err))
    return message[:300], reasons


def _form(fields):
    return urllib.parse.urlencode(fields).encode("ascii")


_FORM = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}


# ------------------------------------------------------------------------------------ secrets

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR if protect else ctypes.c_void_p,
                   ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                   ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    src, _src_buf = blob(data)
    entropy, _ent_buf = blob(b"collie-google-connection-v1")
    out = _Blob()
    label = "Collie Google connection" if protect else None
    if not fn(ctypes.byref(src), label, ctypes.byref(entropy), None, None, 0x01, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI refused the Google connection")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


class _DpapiBackend:
    """Windows: sealed for this Windows user with DPAPI; the blob sits in the metadata file."""
    name = "dpapi"

    def seal(self, secret, path):
        return {"blob": base64.b64encode(_dpapi(secret.encode("utf-8"), True)).decode("ascii")}

    def open(self, sealed, path):
        return _dpapi(base64.b64decode(sealed["blob"]), False).decode("utf-8")

    def erase(self, sealed, path):
        pass                                   # the blob goes with the metadata file


_KEYCHAIN_SAFE = re.compile(r"[A-Za-z0-9._~+/=-]{1,4096}\Z")


class _KeychainBackend:
    """macOS: a generic-password item in the login Keychain.

    ``security add-generic-password -w`` takes the password as an argument, which any process
    can read from the process list. ``security -i`` reads the same command from stdin instead.
    """
    name = "keychain"
    service = "Collie Google connection"

    def _account(self, path):
        return "refresh-token-" + hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]

    def seal(self, secret, path):
        if not _KEYCHAIN_SAFE.match(secret or ""):
            raise GoogleError("storage", "Google returned a sign-in Collie cannot store safely.")
        account = self._account(path)
        command = 'add-generic-password -U -s "%s" -a "%s" -w "%s"\n' % (
            self.service, account, secret)
        done = subprocess.run(["security", "-i"], input=command, capture_output=True, text=True,
                              errors="replace", timeout=20)
        if done.returncode != 0:
            raise GoogleError("storage", "The macOS Keychain refused to store the Google connection.")
        return {"service": self.service, "account": account}

    def open(self, sealed, path):
        done = subprocess.run(["security", "find-generic-password", "-s", sealed["service"],
                               "-a", sealed["account"], "-w"], capture_output=True, text=True,
                              errors="replace", timeout=20)
        if done.returncode != 0 or not done.stdout.strip():
            raise GoogleError("storage", "The Google connection is missing from the macOS Keychain.")
        return done.stdout.strip()

    def erase(self, sealed, path):
        subprocess.run(["security", "delete-generic-password", "-s", sealed["service"],
                        "-a", sealed["account"]], capture_output=True, text=True,
                       errors="replace", timeout=20)


class _FileBackend:
    """Anywhere else: an owner-only (0600) file next to the metadata, not inside it."""
    name = "file"

    def _secret_path(self, path):
        return os.path.splitext(path)[0] + ".secret"

    def seal(self, secret, path):
        target = self._secret_path(path)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret)
        plat.chmod_private(target)
        return {"file": os.path.basename(target)}

    def open(self, sealed, path):
        with open(os.path.join(os.path.dirname(path), sealed["file"]), encoding="utf-8") as handle:
            return handle.read(8192).strip()

    def erase(self, sealed, path):
        try:
            os.remove(os.path.join(os.path.dirname(path), sealed["file"]))
        except FileNotFoundError:
            pass


_BACKENDS = {"dpapi": _DpapiBackend, "keychain": _KeychainBackend, "file": _FileBackend}


def _secret_backend():
    if plat.is_windows():
        return _DpapiBackend()
    if plat.is_macos():
        return _KeychainBackend()
    return _FileBackend()


def _backend_for(record):
    current = _secret_backend()
    stored = record.get("storage")
    if stored == current.name or stored not in _BACKENDS:
        return current
    return _BACKENDS[stored]()


# ------------------------------------------------------------------------------------ records

_LOCK = threading.RLock()
_CACHE = {}          # connection path -> {"token", "expires_at"}; access tokens live only here


def _reset_cache():
    with _LOCK:
        _CACHE.clear()


def _conn_path(state_dir=None):
    return os.path.join(_state_dir(state_dir), CONNECTION_FILE)


def _read_record(path):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read(256 * 1024 + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("version") != 1 or not isinstance(rec.get("sealed"), dict):
        return None
    rec["scopes"] = sorted({s for s in rec.get("scopes") or [] if isinstance(s, str)})
    return rec


def _save_connection(refresh_token, scopes, *, account="", client_id="", imported=False,
                     connected_at=None, state_dir=None):
    """Seal ``refresh_token`` and record the connection. Verifies the seal opens again before
    anything is written, so a store that cannot give the token back never replaces a good one."""
    if not isinstance(refresh_token, str) or not refresh_token or len(refresh_token) > 4096:
        raise GoogleError("storage", "Google returned a sign-in Collie cannot store.")
    path = _conn_path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backend = _secret_backend()
    with sessions._locked(path):
        old = _read_record(path)
        try:
            sealed = backend.seal(refresh_token, path)
            if backend.open(sealed, path) != refresh_token:
                raise GoogleError("storage", "the sealed sign-in did not read back")
        except GoogleError:
            raise
        except Exception as exc:
            raise GoogleError("storage", "Collie could not store the Google connection securely "
                                         "(%s)." % exc.__class__.__name__) from None
        record = {"version": 1, "account": str(account or "")[:320],
                  "scopes": sorted({s for s in scopes if isinstance(s, str)}),
                  "client_id": str(client_id or ""),
                  "connected_at": int(connected_at or time.time()), "imported": bool(imported),
                  "storage": backend.name, "sealed": sealed,
                  "needs_reconnect": False, "reconnect_reason": ""}
        sessions._atomic_dump(record, path)
        plat.chmod_private(path)
        if old and old.get("storage") != backend.name:
            try:
                _backend_for(old).erase(old["sealed"], path)
            except Exception:
                pass
    with _LOCK:
        _CACHE.pop(path, None)
    return record


def _update_record(path, **changes):
    with sessions._locked(path):
        rec = _read_record(path)
        if not rec:
            return None
        rec.update(changes)
        sessions._atomic_dump(rec, path)
        plat.chmod_private(path)
        return rec


def _remove_connection(path):
    with sessions._locked(path):
        rec = _read_record(path)
        if rec:
            try:
                _backend_for(rec).erase(rec["sealed"], path)
            except Exception:
                pass
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    with _LOCK:
        _CACHE.pop(path, None)
    return rec is not None


def _import_plaintext(state_dir=None):
    """Move the prototype's plaintext ``google-token.json`` into sealed storage, then delete it.

    Returns "" or a sentence saying why the import could not happen. The plaintext is deleted
    only after the sealed copy has been read back, never before.
    """
    plain = os.path.join(_state_dir(state_dir), PLAINTEXT_FILE)
    if not os.path.isfile(plain):
        return ""
    path = _conn_path(state_dir)
    if _read_record(path):
        _unlink(plain)                          # a newer connection already exists
        return ""
    try:
        with open(plain, encoding="utf-8") as handle:
            data = json.loads(handle.read(64 * 1024))
        refresh = data.get("refresh_token") if isinstance(data, dict) else None
        if not isinstance(refresh, str) or not refresh:
            raise ValueError("no refresh token in it")
    except (OSError, ValueError) as exc:
        return ("Collie could not read the old %s (%s); run `collie google connect` to connect "
                "again." % (PLAINTEXT_FILE, _plain_os_error(exc)))
    scope = data.get("scope")
    scopes = scope.split() if isinstance(scope, str) and scope.strip() else list(SCOPES)
    obtained = data.get("obtained_at")
    try:
        _save_connection(refresh, scopes, client_id=str(data.get("client_id") or ""),
                         imported=True, state_dir=state_dir,
                         connected_at=obtained if isinstance(obtained, int) else None)
    except GoogleError as exc:
        return ("Collie could not move the saved Google sign-in into secure storage, so it left "
                "%s where it is: %s" % (PLAINTEXT_FILE, exc))
    _unlink(plain)
    return ""


def _unlink(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------------------------ tokens

def _reconnect_message(rec, client=None):
    if client and rec.get("client_id") and rec["client_id"] != client["client_id"]:
        return ("Google needs you to connect again: Collie's Google app changed since this "
                "account was connected. " + RECONNECT_HINT)
    return ("Google needs you to connect again: the saved sign-in has expired or was revoked "
            "(while Collie's Google app is in testing, Google ends every sign-in after 7 days). "
            + RECONNECT_HINT)


def _refresh(client, refresh_token, path):
    status, body = _send("POST", TOKEN_URI, headers=_FORM, data=_form({
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client["client_id"], "client_secret": client["client_secret"]}))
    payload = _loads(body)
    if status == 200 and isinstance(payload.get("access_token"), str):
        try:
            expires_in = max(0, int(payload.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        return payload["access_token"], expires_in, str(payload.get("scope") or "")
    message, reasons = _google_error(body)
    if "invalid_grant" in reasons:
        rec = _update_record(path, needs_reconnect=True, reconnect_reason="invalid_grant") or {}
        raise NeedsReconnect(_reconnect_message(rec))
    if reasons & {"invalid_client", "unauthorized_client"}:
        raise GoogleError("client_rejected", "Google refused Collie's OAuth client (%s). Update "
                                             "Collie, or check ~/.collie/%s."
                          % (sorted(reasons)[0], USER_CLIENT_FILE))
    raise GoogleAPIError(status, "Google's sign-in service answered HTTP %d%s. Try again in a "
                                 "minute." % (status, (": " + message) if message else ""))


def _access_token(need=None, *, state_dir=None):
    """A valid access token, refreshed on demand and cached in memory only.

    Raises NotConfigured, NotConnected, NeedsReconnect, MissingScope (checked before any request
    when the stored grant already lacks ``need``) or GoogleError("network").
    """
    _import_plaintext(state_dir)
    path = _conn_path(state_dir)
    rec = _read_record(path)
    if not rec:
        raise NotConnected("Google isn't connected. " + RECONNECT_HINT)
    client = client_config(state_dir=state_dir)
    if rec.get("needs_reconnect") or (rec.get("client_id") and rec["client_id"] != client["client_id"]):
        raise NeedsReconnect(_reconnect_message(rec, client))
    if need and need not in rec["scopes"]:
        raise MissingScope(need)
    with _LOCK:
        hit = _CACHE.get(path)
        if hit and hit["expires_at"] - 60 > time.time():
            return hit["token"]
        try:
            refresh = _backend_for(rec).open(rec["sealed"], path)
        except GoogleError:
            raise
        except Exception as exc:
            raise NeedsReconnect("Collie could not unseal the saved Google connection (%s). %s"
                                 % (exc.__class__.__name__, RECONNECT_HINT)) from None
        token, expires_in, scope = _refresh(client, refresh, path)
        _CACHE[path] = {"token": token, "expires_at": time.time() + expires_in}
    granted = sorted(set(scope.split())) if scope else rec["scopes"]
    changes = {}
    if granted != rec["scopes"]:
        changes["scopes"] = granted
    if not rec.get("account"):
        account = _fetch_account(token, granted)
        if account:
            changes["account"] = account
    if changes:
        _update_record(path, **changes)
    if need and need not in granted:
        raise MissingScope(need)
    return token


def _drop_cached(state_dir=None):
    with _LOCK:
        _CACHE.pop(_conn_path(state_dir), None)


def _fetch_account(token, granted):
    """The account's address: Gmail's profile, or the primary calendar's id. Best effort."""
    auth = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    try:
        if GMAIL_READ in granted or GMAIL_COMPOSE in granted:
            status, body = _send("GET", GMAIL_API + "/users/me/profile", headers=auth)
            value = _loads(body).get("emailAddress") if status == 200 else ""
        elif CALENDAR_READ in granted:
            status, body = _send("GET", CALENDAR_API + "/calendars/primary", headers=auth)
            value = _loads(body).get("id") if status == 200 else ""
        else:
            value = ""
    except GoogleError:
        return ""
    return value if isinstance(value, str) and "@" in value and len(value) <= 320 else ""


def _email_from_id_token(id_token):
    """The email claim of an id_token that came straight from Google's token endpoint over TLS
    (display only; nothing is authorised on it)."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        value = claims.get("email")
    except Exception:
        return ""
    return value if isinstance(value, str) and "@" in value else ""


# ------------------------------------------------------------------------------------ status

def status(*, check=False, state_dir=None) -> dict:
    """Where the connection stands, without secrets.

    ``state`` is one of ``not_configured`` (no OAuth client), ``not_connected``, ``connected``,
    ``missing_scope`` (connected, but the person unticked something) or ``needs_reconnect``.
    With ``check=True`` it also proves the sign-in still works by refreshing an access token,
    which is the only way to learn that Google has ended it.
    """
    out = {"state": "not_connected", "message": "", "account": "", "granted_scopes": [],
           "missing_scopes": list(SCOPES), "can": _can(()), "client_source": "",
           "storage": "", "connected_at": None}
    problem = _import_plaintext(state_dir)
    path = _conn_path(state_dir)
    try:
        client = client_config(state_dir=state_dir)
        out["client_source"] = client["source"]
    except NotConfigured as exc:
        client, not_configured = None, str(exc)
    if check and client:
        try:
            _access_token(state_dir=state_dir)
        except (NeedsReconnect, NotConnected):
            pass
        except GoogleError as exc:
            out["error"] = str(exc)
    rec = _read_record(path)
    if rec:
        granted = rec["scopes"]
        out.update(account=rec.get("account") or "", granted_scopes=granted,
                   missing_scopes=[s for s in SCOPES if s not in granted], can=_can(granted),
                   storage=rec.get("storage") or "", connected_at=rec.get("connected_at"))
    if client is None:
        out.update(state="not_configured", message=not_configured)
    elif not rec:
        out["message"] = problem or ("Google isn't connected. " + RECONNECT_HINT)
    elif rec.get("needs_reconnect") or (rec.get("client_id") and rec["client_id"] != client["client_id"]):
        out.update(state="needs_reconnect", message=_reconnect_message(rec, client))
    elif out["missing_scopes"]:
        who = out["account"] or "your Google account"
        out.update(state="missing_scope", message=(
            "Connected as %s, but Google didn't allow Collie to %s. Run `collie google connect` "
            "again and tick every box to fix it." % (
                who, " or ".join(_WHAT[s] for s in out["missing_scopes"]))))
    else:
        out.update(state="connected", message="Connected to Google as %s." % (
            out["account"] or "your Google account"))
    return out


def _can(granted):
    return {"gmail_read": GMAIL_READ in granted, "gmail_drafts": GMAIL_COMPOSE in granted,
            "calendar_read": CALENDAR_READ in granted}


# ------------------------------------------------------------------------------------ sign-in

def _pkce():
    """(code_verifier, code_challenge) for PKCE S256 (RFC 7636)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _auth_url(client_id, redirect_uri, state, challenge, login_hint=""):
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(SCOPES), "code_challenge": challenge,
              "code_challenge_method": "S256", "state": state, "access_type": "offline",
              "prompt": "consent"}
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URI + "?" + urllib.parse.urlencode(params)


def _judge_callback(path, *, peer, host, port, state):
    """What a request to the loopback server is: ``code``, ``denied``, ``error``,
    ``state_mismatch``, ``forbidden`` (not loopback, or a Host that is not ours) or ``ignore``
    (a favicon or anything else that is not Google's redirect)."""
    if peer != "127.0.0.1" or (host or "").strip().lower() != "127.0.0.1:%d" % port:
        return "forbidden", {}
    parts = urllib.parse.urlsplit(path or "/")
    if parts.path not in ("", "/"):
        return "ignore", {}
    query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
    if not {"code", "error", "state"} & set(query):
        return "ignore", {}
    if not hmac.compare_digest(query.get("state", "").encode("utf-8"), state.encode("utf-8")):
        return "state_mismatch", {}
    if query.get("error"):
        return ("denied" if query["error"] == "access_denied" else "error"), {
            "error": query["error"][:120]}
    if query.get("code"):
        return "code", {"code": query["code"]}
    return "ignore", {}


class _Flow:
    def __init__(self, *, client, state, verifier, redirect_uri, port, state_dir):
        self.client, self.state, self.verifier = client, state, verifier
        self.redirect_uri, self.port, self.state_dir = redirect_uri, port, state_dir
        self.lock, self.event = threading.Lock(), threading.Event()
        self.claimed, self.error = False, None

    def claim(self):
        with self.lock:
            if self.claimed:
                return False
            self.claimed = True
            return True

    def finish(self, code):
        """Exchange the code, record the grant and return the success page."""
        status_code, body = _send("POST", TOKEN_URI, headers=_FORM, data=_form({
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client["client_id"], "client_secret": self.client["client_secret"],
            "code_verifier": self.verifier}))
        payload = _loads(body)
        if status_code != 200 or not isinstance(payload.get("access_token"), str):
            message, reasons = _google_error(body)
            raise GoogleError("error", "Google would not finish the sign-in (%s). %s" % (
                ", ".join(sorted(reasons)) or "HTTP %d" % status_code, RECONNECT_HINT))
        granted = sorted(set(str(payload.get("scope") or "").split()))
        if not any(s in granted for s in SCOPES):
            raise GoogleError("no_scopes", "Every permission was left unticked on Google's page, "
                                           "so there is nothing Collie could use. Nothing was "
                                           "saved. " + RECONNECT_HINT)
        refresh = payload.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise GoogleError("error", "Google did not give Collie a lasting sign-in (no refresh "
                                       "token). Nothing was saved. " + RECONNECT_HINT)
        token = payload["access_token"]
        account = (_email_from_id_token(payload.get("id_token") or "")
                   or _fetch_account(token, granted))
        _save_connection(refresh, granted, account=account, client_id=self.client["client_id"],
                         state_dir=self.state_dir)
        try:
            expires_in = max(0, int(payload.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        with _LOCK:
            _CACHE[_conn_path(self.state_dir)] = {"token": token,
                                                  "expires_at": time.time() + expires_in}
        return success_page(granted, account=account)


_CALLBACK_ERRORS = {
    "denied": "You chose not to give Collie access to Google. Nothing was saved. " + RECONNECT_HINT,
    "state_mismatch": ("The answer from Google didn't match the sign-in Collie started, so Collie "
                       "ignored it. Nothing was saved. " + RECONNECT_HINT),
}

_PAGE_HEADERS = (
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    timeout = 30                                # an idle pre-connected socket cannot pin a thread

    def log_message(self, *args):               # the request line carries the authorization code
        pass

    def log_error(self, *args):
        pass

    def _reply(self, status_code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in _PAGE_HEADERS:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        flow = self.server.flow
        verdict, info = _judge_callback(self.path, peer=self.client_address[0],
                                        host=self.headers.get("Host", ""), port=flow.port,
                                        state=flow.state)
        if verdict == "forbidden":
            return self._reply(403, "forbidden")
        if verdict == "ignore":
            return self._reply(404, "not found")
        if not flow.claim():
            return self._reply(409, "This sign-in was already handled. You can close this tab.")
        status_code = 200
        try:
            if verdict == "code":
                page = flow.finish(info["code"])
            elif verdict in _CALLBACK_ERRORS:
                raise GoogleError(verdict, _CALLBACK_ERRORS[verdict])
            else:
                raise GoogleError("error", "Google reported a problem (%s). Nothing was saved. %s"
                                  % (info.get("error") or "unknown", RECONNECT_HINT))
        except GoogleError as exc:
            flow.error, status_code = exc, 400
            page = failure_page(exc.code, detail="" if exc.code in _CALLBACK_ERRORS else str(exc))
        except Exception as exc:                # never leave the browser tab hanging
            flow.error, status_code = GoogleError("error", "Collie hit an unexpected problem "
                                                  "(%s). Nothing was saved." % exc.__class__.__name__), 500
            page = failure_page("error", detail=str(flow.error))
        try:
            self._reply(status_code, page, "text/html; charset=utf-8")
        finally:
            flow.event.set()


def connect(open_browser=webbrowser.open, *, timeout=300, announce=None, login_hint="",
            state_dir=None) -> dict:
    """Connect Google: open the consent page, wait for the loopback redirect, store the grant.

    ``open_browser(url)`` shows Google's page; ``announce(url)`` (optional) is handed the same
    address first, for a caller that must show it itself when no browser opens. Blocks until the
    redirect arrives or ``timeout`` seconds pass. Returns ``status()``; raises GoogleError with
    ``code`` in denied / state_mismatch / no_scopes / timeout / error / storage, or NotConfigured.
    """
    client = client_config(state_dir=state_dir)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    redirect_uri = "http://127.0.0.1:%d" % port
    flow = server.flow = _Flow(client=client, state=state, verifier=verifier,
                               redirect_uri=redirect_uri, port=port, state_dir=state_dir)
    url = _auth_url(client["client_id"], redirect_uri, state, challenge, login_hint)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                              name="collie-google-callback", daemon=True)
    worker.start()
    try:
        if announce:
            try:
                announce(url)
            except Exception:
                pass
        try:
            open_browser(url)
        except Exception:
            pass
        finished = flow.event.wait(max(1, int(timeout or 300)))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
    if not finished:
        raise GoogleError("timeout", "Nothing came back from Google within %d seconds, so the "
                                     "sign-in was abandoned. Nothing was saved. %s"
                          % (max(1, int(timeout or 300)), RECONNECT_HINT))
    if flow.error:
        raise flow.error
    return status(state_dir=state_dir)


def disconnect(*, state_dir=None) -> dict:
    """Revoke the grant at Google, then delete it here whatever Google answered.

    Returns ``{"removed": bool, "revoked": bool, "message": str}``.
    """
    _import_plaintext(state_dir)
    path = _conn_path(state_dir)
    rec = _read_record(path)
    if not rec:
        return {"removed": False, "revoked": False, "message": "Google wasn't connected."}
    revoked = False
    try:
        refresh = _backend_for(rec).open(rec["sealed"], path)
    except Exception:
        refresh = ""
    if refresh:
        try:
            status_code, _body = _send("POST", REVOKE_URI, headers=_FORM,
                                       data=_form({"token": refresh}))
            revoked = status_code == 200
        except GoogleError:
            revoked = False
    _remove_connection(path)
    if revoked:
        message = ("Disconnected. Google has revoked Collie's access, and the connection is "
                   "deleted from this computer.")
    else:
        message = ("Disconnected on this computer. Google didn't confirm that access was revoked; "
                   "to be sure, remove Collie at %s." % PERMISSIONS_PAGE)
    return {"removed": True, "revoked": revoked, "message": message}


# ------------------------------------------------------------------------------------ pages

_PAGE_CSS = """
:root{
  --sky:linear-gradient(170deg,#58aaff 0%,#8fcaff 40%,#d6ecff 68%,#ffe2ad 100%);
  --card:#ffffff;--ink:#14213d;--mut:#5b6682;--line:#e4e9f3;--code:#f3f6fb;
  --ok:#1fa971;--okbg:#e3f7ee;--warn:#c2410c;--warnbg:#fff1e6;
  --sun:linear-gradient(100deg,#ffb547,#ff8a3d);--shadow:0 24px 60px rgba(40,90,160,.22);
  --round:ui-rounded,"SF Pro Rounded","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  --ground:#ffe2ad;color-scheme:light dark;
}
@media (prefers-color-scheme: dark){
  :root{
    --sky:linear-gradient(170deg,#0c1c40 0%,#16306a 45%,#29407a 75%,#4a3a5e 100%);
    --card:#172036;--ink:#eef3ff;--mut:#a3aecb;--line:#2a3552;--code:#0f1729;
    --ok:#3ccf92;--okbg:rgba(60,207,146,.15);--warn:#ffb27a;--warnbg:rgba(255,138,61,.15);
    --shadow:0 24px 60px rgba(0,0,0,.5);--ground:#4a3a5e;
  }
}
*{box-sizing:border-box}
html{background:var(--ground)}
body{margin:0;min-height:100vh;background:var(--sky);color:var(--ink);
  font:500 16px/1.55 var(--round);display:flex;justify-content:center;align-items:flex-start;
  padding:72px 16px 48px}
.card{width:100%;max-width:560px;background:var(--card);border-radius:22px;box-shadow:var(--shadow);
  padding:0 32px 28px;position:relative}
.dog{width:96px;height:96px;border-radius:50%;background:#fff;border:5px solid var(--card);
  box-shadow:0 10px 28px rgba(20,50,100,.2);display:flex;align-items:center;justify-content:center;
  margin:-48px auto 6px;overflow:hidden}
.dog img{width:74px;height:74px;display:block}
.badge{width:34px;height:34px;border-radius:50%;margin:-8px auto 0;display:flex;align-items:center;
  justify-content:center;font:800 18px/1 var(--round);color:#fff;background:#1fa971;
  box-shadow:0 0 0 5px var(--card)}
.badge.bad{background:var(--sun)}
h1{font:800 34px/1.1 var(--round);letter-spacing:-.02em;text-align:center;margin:14px 0 6px}
.lead{text-align:center;color:var(--mut);margin:0 auto 6px;max-width:42ch}
.lead b{color:var(--ink);font-weight:750;overflow-wrap:anywhere}
h2{font:800 13px/1.3 var(--round);letter-spacing:.09em;text-transform:uppercase;color:var(--mut);
  margin:26px 0 10px}
ul{list-style:none;margin:0;padding:0;display:grid;gap:12px}
li{display:grid;grid-template-columns:26px 1fr;gap:12px;align-items:start}
.tick{width:26px;height:26px;border-radius:50%;background:var(--okbg);color:var(--ok);
  font:800 14px/26px var(--round);text-align:center;font-style:normal}
.tick.no,.tick.again{background:var(--warnbg);color:var(--warn)}
li b{font-weight:750}
li .sub{display:block;color:var(--mut);font-size:15px}
.next{margin:0;background:var(--sun);color:#fff;border-radius:16px;padding:14px 18px;
  font:700 16px/1.45 var(--round);box-shadow:0 10px 26px rgba(255,138,61,.35)}
.fine li{font-size:15px;color:var(--mut)}
.fine .tick{width:22px;height:22px;font-size:12px;line-height:22px;margin-top:1px}
code{font:600 .9em ui-monospace,"Cascadia Mono",Consolas,monospace;background:var(--code);
  border:1px solid var(--line);border-radius:7px;padding:1px 6px;color:var(--ink);white-space:nowrap}
.detail{margin:14px 0 0;padding:12px 14px;border-radius:14px;background:var(--code);
  border:1px solid var(--line);color:var(--mut);font-size:14px;overflow-wrap:anywhere}
.close{text-align:center;margin:28px 0 0;font-weight:700;color:var(--mut)}
@media (max-width:480px){.card{padding:0 20px 22px}h1{font-size:28px}body{padding-top:64px}}
"""


def _dog_name():
    try:
        from . import settings
        return str(settings.get("COMPANION_NAME", "") or "").strip()
    except Exception:
        return ""


def _avatar_uri(dog_name):
    """The dog as an inline SVG: this Collie's own coat when it has a name, else the logo."""
    try:
        if dog_name:
            from . import avatar
            svg = avatar.svg(dog_name, plate=False)
        else:
            with open(_LOGO, encoding="utf-8") as handle:
                svg = handle.read(256 * 1024)
    except Exception:
        return ""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _page(title, badge, heading, lead, sections, dog_name):
    esc = html.escape
    avatar = _avatar_uri(dog_name)
    dog = ('<div class="dog"><img alt="" src="%s"></div>' % esc(avatar)) if avatar else \
        '<div class="dog"></div>'
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"color-scheme\" content=\"light dark\">"
            "<title>" + esc(title, quote=False) + "</title><style>" + _PAGE_CSS + "</style></head><body>"
            "<main class=\"card\">" + dog + badge + "<h1>" + heading + "</h1>"
            "<p class=\"lead\">" + lead + "</p>" + sections +
            "<p class=\"close\">You can close this tab.</p></main></body></html>\n")


def _item(ok, title, sub=""):
    return ('<li><i class="tick%s" aria-hidden="true">%s</i><div><b>%s</b>%s</div></li>' % (
        "" if ok else " no", "✓" if ok else "!", title,
        ('<span class="sub">%s</span>' % sub) if sub else ""))


def _note(text):
    return '<li><i class="tick" aria-hidden="true">✓</i><div>%s</div></li>' % text


def success_page(granted_scopes, account: str = "", dog_name=None) -> str:
    """The page after a successful sign-in: who is connected and exactly what Google allowed."""
    esc = html.escape
    granted = set(granted_scopes or ())
    name = _dog_name() if dog_name is None else str(dog_name or "")
    read, drafts, cal = GMAIL_READ in granted, GMAIL_COMPOSE in granted, CALENDAR_READ in granted
    fix = "To allow it, run <code>collie google connect</code> again and tick that box."
    items = []
    if read and drafts:
        items.append(_item(True, "Gmail — read your mail and write drafts",
                           "Collie never sends. Drafts wait in Gmail until you press Send."))
    elif read:
        items.append(_item(True, "Gmail — read your mail"))
        items.append(_item(False, "Gmail drafts — not allowed",
                           "Collie can read your mail but can't prepare replies for you. " + fix))
    elif drafts:
        items.append(_item(True, "Gmail — write drafts",
                           "Collie never sends. Drafts wait in Gmail until you press Send."))
        items.append(_item(False, "Reading your mail — not allowed",
                           "Your morning report can't include your inbox. " + fix))
    else:
        items.append(_item(False, "Gmail — not allowed",
                           "Your morning report can't include your inbox or prepare replies. "
                           + fix))
    if cal:
        items.append(_item(True, "Calendar — read your events"))
    else:
        items.append(_item(False, "Calendar — not allowed",
                           "Your morning report won't show your day's events. " + fix))
    who = esc(account, quote=False) if account else "your Google account"
    signed = (" — %s, your Collie" % esc(name, quote=False)) if name else ""
    sections = (
        "<h2>What Collie can do</h2><ul>" + "".join(items) + "</ul>"
        "<h2>What happens next</h2><p class=\"next\">☀️ Your first morning report "
        "arrives tomorrow morning — by email and on your desktop." + signed + "</p>"
        "<h2>Your privacy</h2><ul class=\"fine\">"
        + _note("This connection is stored only on this computer.")
        + _note("Disconnect anytime with <code>collie google disconnect</code> or in "
                "Settings → Connections.")
        + "</ul>")
    return _page("Collie · Google connected", '<div class="badge">✓</div>',
                 "You're connected!", "Collie is now connected to <b>%s</b>." % who,
                 sections, name)


_FAILURES = {
    "denied": ("No problem — nothing was connected",
               "You chose not to give Collie access, so nothing was saved."),
    "state_mismatch": ("This sign-in didn't match",
                       "The answer that came back from Google didn't match the sign-in Collie "
                       "started, so Collie ignored it to keep your account safe. Nothing was "
                       "saved."),
    "no_scopes": ("Nothing was allowed",
                  "Every box on Google's page was left unticked, so there's nothing Collie could "
                  "use. Nothing was saved."),
}


def failure_page(reason: str, detail: str = "", dog_name=None) -> str:
    """The page when the sign-in did not complete: what happened and how to try again."""
    esc = html.escape
    name = _dog_name() if dog_name is None else str(dog_name or "")
    heading, lead = _FAILURES.get(reason, (
        "Something went wrong", "Collie couldn't finish connecting to Google. Nothing was saved."))
    sections = ""
    detail = (detail or "").replace(RECONNECT_HINT, "").strip()   # the page says it below
    if detail:
        sections += '<p class="detail">' + esc(detail[:600], quote=False) + "</p>"
    sections += ('<h2>Try again</h2><ul><li><i class="tick again" aria-hidden="true">↻</i>'
                 "<div><b>Run <code>collie google connect</code></b><span class=\"sub\">or press "
                 "Connect in Settings → Connections.</span></div></li></ul>")
    return _page("Collie · Google not connected", '<div class="badge bad">!</div>',
                 esc(heading, quote=False), esc(lead, quote=False), sections, name)
