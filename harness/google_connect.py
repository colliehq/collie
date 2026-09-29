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
import codecs
import datetime as _dt
import email.errors
import email.headerregistry
import email.message
import email.policy
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import unicodedata
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
EXCHANGE_GRACE = 3 * HTTP_TIMEOUT + 5           # a code exchange + account lookup, after the deadline
MAX_RESPONSE_BYTES = 12 * 1024 * 1024         # a long thread in format=full can be several MB
MAX_SEARCH_RESULTS = 50
MAX_QUERY_CHARS = 1000
MAX_EVENTS = 250
MAX_THREAD_MESSAGES = 100
MAX_DRAFT_BODY = 200_000
MAX_REFERENCES = 20
USER_AGENT = "Collie-Google/1.0 (+https://github.com/colliehq/collie)"

RECONNECT_HINT = "Run `collie google connect` (or press Connect in Settings → Connections)."
_WHAT = {GMAIL_READ: "read your Gmail", GMAIL_COMPOSE: "write Gmail drafts",
         CALENDAR_READ: "read your Google Calendar"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\.apps\.googleusercontent\.com\Z")
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})\Z")
_DRAFT_ID = re.compile(r"r-?\d{1,20}\Z")
_HEX_ID = re.compile(r"[0-9a-fA-F]{1,16}\Z")


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
    dead = ""                  # a check that failed in a way only reconnecting fixes
    if check and client:
        # A cached access token proves nothing about the sign-in behind it: Google may have
        # ended it since. Only a refresh asks.
        _drop_cached(state_dir)
        try:
            _access_token(state_dir=state_dir)
        except NeedsReconnect as exc:
            dead = str(exc)
        except NotConnected:
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
    elif dead or rec.get("needs_reconnect") or (rec.get("client_id")
                                                and rec["client_id"] != client["client_id"]):
        out.update(state="needs_reconnect", message=dead or _reconnect_message(rec, client))
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
        self.claimed, self.closed, self.error = False, False, None

    def claim(self):
        """Take the one redirect this sign-in accepts; refused once the deadline has closed it."""
        with self.lock:
            if self.claimed or self.closed:
                return False
            self.claimed = True
            return True

    def close(self):
        """The deadline passed: accept no redirect from now on. True when one is already being
        exchanged, which the caller then waits for instead of reporting that nothing was saved."""
        with self.lock:
            self.closed = True
            return self.claimed

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
        replaced = _separate_grant_token(self.state_dir, account, self.client["client_id"])
        _save_connection(refresh, granted, account=account, client_id=self.client["client_id"],
                         state_dir=self.state_dir)
        if replaced and replaced != refresh:
            _revoke(replaced)                            # best effort: the new sign-in is saved
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
        if verdict == "state_mismatch":
            # Any local process can reach this port, so a wrong state must not decide the
            # sign-in. It is refused on its own and the wait continues for Google's redirect.
            return self._reply(400, failure_page("state_mismatch"), "text/html; charset=utf-8")
        if not flow.claim():
            return self._reply(409, "This sign-in has already finished or expired. You can close "
                                    "this tab; to connect again, run `collie google connect`.")
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


class _CallbackServer(ThreadingHTTPServer):
    """The loopback listener, which no other socket may share.

    http.server sets SO_REUSEADDR, and on Windows that lets a second socket bind the same
    127.0.0.1:port and take the redirect (with its authorization code) instead. Reuse is off, and
    on Windows SO_EXCLUSIVEADDRUSE refuses even a later socket that asks for SO_REUSEADDR itself.
    """
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def connect(open_browser=webbrowser.open, *, timeout=300, announce=None, login_hint="",
            state_dir=None) -> dict:
    """Connect Google: open the consent page, wait for the loopback redirect, store the grant.

    ``open_browser(url)`` shows Google's page; ``announce(url)`` (optional) is handed the same
    address first, for a caller that must show it itself when no browser opens. Blocks until the
    redirect arrives or ``timeout`` seconds pass. Returns ``status()``; raises GoogleError with
    ``code`` in denied / no_scopes / timeout / error / storage, or NotConfigured. A redirect whose
    ``state`` does not match is refused on its own and does not end the wait.
    """
    client = client_config(state_dir=state_dir)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
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
        if not finished and flow.close():
            # The code arrived in time and its exchange is still running: its outcome, not the
            # clock, decides what to report. The exchange makes at most two bounded requests.
            finished = flow.event.wait(EXCHANGE_GRACE)
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


def _revoke(token):
    """Ask Google to revoke the grant behind ``token`` (the token travels in the POST body).
    True when Google confirmed it; never raises."""
    try:
        status_code, _body = _send("POST", REVOKE_URI, headers=_FORM, data=_form({"token": token}))
    except Exception:
        return False
    return status_code == 200


def _separate_grant_token(state_dir, account, client_id):
    """The stored refresh token when a new sign-in replaces a *different* grant, else "".

    Google revokes grants, not single tokens: revoking one token ends every token that account
    holds for the project. So the old token is only worth revoking when it belongs to another
    account or another Google Cloud project (the number before the first "-" of a client id).
    Replacing the same account's own grant just forgets the old token; revoking it would end the
    sign-in that has just replaced it. An old record with no known account is left alone too.
    """
    path = _conn_path(state_dir)
    old = _read_record(path)
    if not old:
        return ""
    old_account, new_account = (old.get("account") or "").lower(), (account or "").lower()
    old_project = (old.get("client_id") or "").split("-", 1)[0]
    new_project = (client_id or "").split("-", 1)[0]
    other_account = bool(old_account and new_account and old_account != new_account)
    other_project = bool(old_project.isdigit() and new_project.isdigit()
                         and old_project != new_project)
    if not (other_account or other_project):
        return ""
    try:
        return _backend_for(old).open(old["sealed"], path)
    except Exception:
        return ""


def disconnect(*, state_dir=None) -> dict:
    """Revoke the grant at Google, then delete it here whatever Google answered.

    Returns ``{"removed": bool, "revoked": bool, "message": str}``.
    """
    _import_plaintext(state_dir)
    path = _conn_path(state_dir)
    rec = _read_record(path)
    if not rec:
        return {"removed": False, "revoked": False, "message": "Google wasn't connected."}
    try:
        refresh = _backend_for(rec).open(rec["sealed"], path)
    except Exception:
        refresh = ""
    revoked = _revoke(refresh) if refresh else False
    _remove_connection(path)
    if revoked:
        message = ("Disconnected. Google has revoked Collie's access, and the connection is "
                   "deleted from this computer.")
    else:
        message = ("Disconnected on this computer. Google didn't confirm that access was revoked; "
                   "to be sure, remove Collie at %s." % PERMISSIONS_PAGE)
    return {"removed": True, "revoked": revoked, "message": message}


# ------------------------------------------------------------------------------------ API

def _check_id(value, what="id"):
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError("%s must be a Gmail id (letters, digits, - and _)" % what)
    return value


def _api(method, url, *, need, params=None, body=None, state_dir=None):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = None if body is None else json.dumps(body).encode("utf-8")
    for attempt in (0, 1):
        token = _access_token(need, state_dir=state_dir)
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        status_code, raw = _send(method, url, headers=headers, data=data)
        if status_code == 401 and attempt == 0:
            _drop_cached(state_dir)             # revoked or expired early: refresh once, retry
            continue
        break
    if 200 <= status_code < 300:
        return _loads(raw)
    message, reasons = _google_error(raw)
    if status_code == 403 and (reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                                          "insufficient_scope"}
                               or "insufficient authentication scopes" in message.lower()):
        raise MissingScope(need)
    if status_code == 429 or status_code >= 500:
        raise GoogleAPIError(status_code, "Google is busy right now (HTTP %d%s). Try again in a "
                                          "minute." % (status_code, (": " + message) if message else ""))
    raise GoogleAPIError(status_code, "Google answered HTTP %d: %s" % (
        status_code, message or "no detail"))


def _headers(payload):
    out = {}
    for row in (payload or {}).get("headers") or []:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            out.setdefault(row["name"].lower(), str(row.get("value") or ""))
    return out


def _summary(msg):
    head = _headers(msg.get("payload"))
    labels = [str(x) for x in msg.get("labelIds") or []]
    try:
        stamp = int(msg.get("internalDate") or 0) // 1000
    except (TypeError, ValueError):
        stamp = 0
    return {"id": str(msg.get("id") or ""), "thread_id": str(msg.get("threadId") or ""),
            "from": head.get("from", "")[:1000], "to": head.get("to", "")[:2000],
            "subject": head.get("subject", "")[:1000], "date": head.get("date", "")[:200],
            "snippet": html.unescape(str(msg.get("snippet") or ""))[:1000],
            "labels": labels, "unread": "UNREAD" in labels, "timestamp": stamp}


def gmail_search(query: str, max_results: int = 20, *, state_dir=None) -> list:
    """Messages matching a Gmail search (the same syntax as the search box), newest first.

    Returns ``[{id, thread_id, from, to, subject, date, snippet, labels, unread, timestamp}]``;
    ``max_results`` is clamped to 1..50. Needs gmail.readonly.
    """
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise ValueError("query must be a string of at most %d characters" % MAX_QUERY_CHARS)
    count = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    listing = _api("GET", GMAIL_API + "/users/me/messages", need=GMAIL_READ, state_dir=state_dir,
                   params={"q": query, "maxResults": count})
    rows = []
    for item in (listing.get("messages") or [])[:count]:
        mid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(mid, str) or not _ID.match(mid):
            continue
        msg = _api("GET", GMAIL_API + "/users/me/messages/" + mid, need=GMAIL_READ,
                   state_dir=state_dir, params=[("format", "metadata")] + [
                       ("metadataHeaders", h) for h in ("From", "To", "Subject", "Date")])
        rows.append(_summary(msg))
    return rows


# A sender's HTML is hostile input, and the stdlib html.parser is not safe on it everywhere Collie
# runs: the Windows installer embeds CPython 3.12.10, whose parser is quadratic on malformed markup
# (CVE-2025-6069; 24 KB of "<a " takes ~10 s there). So the fallback never uses it. Comments and
# script/style blocks go first with str.find (so a large stylesheet does not use up the budget), at
# most MAX_HTML_CHARS of what is left is read, and the reading is one pass of a regex whose
# alternatives cannot overlap, checked against HTML_TIME_BUDGET as it goes.
MAX_HTML_CHARS = 64 * 1024
HTML_TIME_BUDGET = 0.25                                  # seconds
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_DROPPED_BLOCK = re.compile(r"<(script|style|head|title|noscript|template)(?=[\s/>]|$)")
_HTML_TOKEN = re.compile(r"<[^<>]*>|[^<]+|<")
_TAG_NAME = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9]*)")
_BREAKS = {"br", "p", "div", "li", "tr", "table", "ul", "ol", "blockquote", "section", "article",
           "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "hr"}


def _drop_invisible(markup):
    """Markup without comments and script/style/head/title blocks, in linear time."""
    low = markup.translate(_ASCII_LOWER)                 # same length as markup, unlike .lower()
    out, i, end = [], 0, len(markup)
    while i < end:
        j = markup.find("<", i)
        if j < 0:
            out.append(markup[i:])
            break
        out.append(markup[i:j])
        if low.startswith("<!--", j):
            k = low.find("-->", j + 4)
            i = end if k < 0 else k + 3
            continue
        block = _DROPPED_BLOCK.match(low, j)
        if block:
            k = low.find("</" + block.group(1), block.end())
            k = -1 if k < 0 else low.find(">", k)
            i = end if k < 0 else k + 1                  # unclosed: the rest is inside it
            continue
        out.append("<")
        i = j + 1
    return "".join(out)


def _html_text(markup, budget=None):
    """(text, complete) from an HTML body. ``complete`` is False when the size cap or the time
    budget stopped the reading early."""
    deadline = time.monotonic() + (HTML_TIME_BUDGET if budget is None else budget)
    visible = _drop_invisible(markup)
    complete = len(visible) <= MAX_HTML_CHARS
    parts = []
    for count, match in enumerate(_HTML_TOKEN.finditer(visible, 0, MAX_HTML_CHARS)):
        if count % 64 == 0 and time.monotonic() >= deadline:
            return "".join(parts), False
        token = match.group(0)
        if len(token) > 1 and token[0] == "<":
            name = _TAG_NAME.match(token)
            if name and name.group(1).lower() in _BREAKS:
                parts.append("\n")
            continue                                     # any other tag, doctype or CDATA marker
        parts.append(html.unescape(token))
    return "".join(parts), complete


def _tidy(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _leaves(part, depth=0, out=None):
    out = [] if out is None else out
    if depth > 20 or len(out) > 200 or not isinstance(part, dict):
        return out
    children = part.get("parts")
    if isinstance(children, list) and children:
        for child in children:
            _leaves(child, depth + 1, out)
    else:
        out.append(part)
    return out


_NOT_MAIL_CHARSETS = {"idna", "punycode", "undefined", "unicode-escape", "raw-unicode-escape"}


def _decode_part(part):
    data = ((part.get("body") or {}).get("data")) or ""
    if not isinstance(data, str) or not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""
    ctype = _headers(part).get("content-type", "")
    match = re.search(r"""charset\s*=\s*["']?([A-Za-z0-9._:-]+)""", ctype, re.I)
    # The sender names the charset. Python also knows codecs that are not mail charsets at all:
    # some raise UnicodeError (idna, undefined), others "decode" to nonsense (punycode, escapes).
    try:
        charset = codecs.lookup(match.group(1)).name if match else "utf-8"
    except LookupError:
        charset = "utf-8"
    if charset in _NOT_MAIL_CHARSETS:
        charset = "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, ValueError):                    # ValueError includes UnicodeError
        return raw.decode("utf-8", errors="replace")


def _message_body(payload, cap):
    """(plain text, truncated): text/plain parts, else the HTML part stripped to text."""
    leaves = [p for p in _leaves(payload) if not p.get("filename")
              and "attachment" not in _headers(p).get("content-disposition", "").lower()]
    plain = [_decode_part(p) for p in leaves if str(p.get("mimeType", "")).lower() == "text/plain"]
    plain = [t for t in plain if t.strip()]
    complete = True
    if plain:
        text = _tidy("\n\n".join(plain))
    else:
        markup = [_decode_part(p) for p in leaves if str(p.get("mimeType", "")).lower() == "text/html"]
        text, complete = _html_text("\n".join(markup))
        text = _tidy(text)
    if len(text) > cap:
        return text[:cap].rstrip(), True
    return text, not complete


def gmail_thread(thread_id: str, *, max_body_chars: int = 20000, state_dir=None) -> dict:
    """One conversation with readable bodies.

    Returns ``{thread_id, subject, messages: [{id, thread_id, from, to, cc, subject, date,
    snippet, labels, unread, timestamp, rfc_message_id, in_reply_to, references, is_draft, body,
    body_truncated}]}``. Bodies prefer text/plain, fall back to HTML stripped to text, skip
    attachments and are capped at ``max_body_chars`` (200..100000). Needs gmail.readonly.
    """
    tid = _check_id(thread_id, "thread_id")
    cap = max(200, min(int(max_body_chars), 100_000))
    data = _api("GET", GMAIL_API + "/users/me/threads/" + tid, need=GMAIL_READ,
                state_dir=state_dir, params={"format": "full"})
    messages = []
    for msg in (data.get("messages") or [])[:MAX_THREAD_MESSAGES]:
        if not isinstance(msg, dict):
            continue
        row = _summary(msg)
        head = _headers(msg.get("payload"))
        body, truncated = _message_body(msg.get("payload") or {}, cap)
        row.update(cc=head.get("cc", "")[:2000], rfc_message_id=head.get("message-id", "")[:998],
                   in_reply_to=head.get("in-reply-to", "")[:998],
                   references=head.get("references", "")[:8000],
                   is_draft="DRAFT" in row["labels"], body=body, body_truncated=truncated)
        messages.append(row)
    return {"thread_id": str(data.get("id") or tid),
            "subject": messages[0]["subject"] if messages else "", "messages": messages}


def _header_value(value, field, limit):
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("%s must be text of at most %d characters" % (field, limit))
    # CR and LF are not the only line breaks: NEL, VT, FF and U+2028/2029 are too, to some
    # reader somewhere. No control character or line separator belongs in a header value.
    if any(c != "\t" and unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in value):
        raise ValueError("%s must be a single line without control characters" % field)
    return value.strip()


_ADDRESS = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_NAME_FORBIDDEN = set('@＠﹫<>,;:"\\()[]')


def _one_recipient(value):
    """``value`` as exactly one address, optionally with a display name, or ValueError.

    A reply goes to one person. Lists, groups and names that carry an address of their own
    ('"boss@corp.com" <attacker@evil.com>') are refused, so what the person sees is who gets it.
    """
    name, addr = "", value.strip()
    if addr.endswith(">") and "<" in addr:
        cut = addr.rindex("<")
        name, addr = addr[:cut].strip(), addr[cut + 1:-1].strip()
        if len(name) >= 2 and name[0] == name[-1] == '"':
            name = name[1:-1].strip()
    local = addr.split("@", 1)[0]
    if not _ADDRESS.fullmatch(addr) or len(addr) > 254 or len(local) > 64:
        raise ValueError("to must be one email address, like ana@example.com or "
                         "Ana <ana@example.com>")
    if len(name) > 200 or "=?" in name or any(c in _NAME_FORBIDDEN for c in name) or \
            not all(c.isprintable() for c in name):
        raise ValueError("to has a display name Collie will not write: it may not contain an "
                         "address, quotes, brackets or separators")
    return email.headerregistry.Address(display_name=name, addr_spec=addr)


def _angle(message_id):
    message_id = message_id.strip()
    if not message_id:
        return ""
    if not (message_id.startswith("<") and message_id.endswith(">")):
        message_id = "<" + message_id.strip("<>") + ">"
    if re.search(r"\s", message_id):
        raise ValueError("a Message-ID cannot contain spaces")
    return message_id


_MESSAGE_ID = re.compile(r"<[^<>\s]{1,994}>")


def _reply_context(tid, state_dir):
    """(Message-ID, References, Subject) of the last message in the thread that is not a draft.

    These headers were written by whoever sent the mail, so they are cleaned rather than
    trusted or refused: a Message-ID that is not one is left out (Gmail still threads by
    threadId), only well-formed ids are kept from References (the last MAX_REFERENCES), and the
    subject is made one line of at most 998 characters.
    """
    data = _api("GET", GMAIL_API + "/users/me/threads/" + tid, need=GMAIL_READ,
                state_dir=state_dir, params=[("format", "metadata")] + [
                    ("metadataHeaders", h) for h in ("Message-ID", "References", "Subject")])
    last = {}
    for msg in data.get("messages") or []:
        if isinstance(msg, dict) and "DRAFT" not in (msg.get("labelIds") or []):
            last = msg
    head = _headers(last.get("payload"))
    first = _headers(((data.get("messages") or [{}])[0] or {}).get("payload"))
    found_id = head.get("message-id", "").strip()
    found_id = found_id if _MESSAGE_ID.fullmatch(found_id) else ""
    refs = _MESSAGE_ID.findall(head.get("references", ""))[-MAX_REFERENCES:]
    subject = head.get("subject", "") or first.get("subject", "")
    subject = re.sub(r"\s+", " ", "".join(c if c.isprintable() else " " for c in subject))
    return found_id, " ".join(refs), subject.strip()[:998]


def gmail_create_draft(thread_id: str, to: str, subject: str, body: str, in_reply_to: str = "",
                       references: str = "", *, state_dir=None) -> dict:
    """Save a reply draft in ``thread_id``. It is never sent: the person opens it and presses Send.

    Follows Gmail's threading rules (threadId on the draft, RFC 2822 In-Reply-To/References,
    matching Subject). ``in_reply_to``/``references`` are the Message-ID headers of the message
    being answered; when ``in_reply_to`` is empty they are read from the thread's last message.
    Returns ``{draft_id, message_id, thread_id, open_url, thread_url}``. Needs gmail.compose
    (and gmail.readonly when the reply headers have to be looked up).
    """
    tid = _check_id(thread_id, "thread_id")
    to = _header_value(to, "to", 2000)
    if not to:
        raise ValueError("to is required")
    recipient = _one_recipient(to)
    subject = _header_value(subject, "subject", 998)
    in_reply_to = _angle(_header_value(in_reply_to, "in_reply_to", 998))
    references = _header_value(references, "references", 8000)
    if not isinstance(body, str) or len(body) > MAX_DRAFT_BODY:
        raise ValueError("body must be text of at most %d characters" % MAX_DRAFT_BODY)
    _access_token(GMAIL_COMPOSE, state_dir=state_dir)       # scope refusal before any lookup
    if not in_reply_to or not subject:
        found_id, found_refs, found_subject = _reply_context(tid, state_dir)
        if not in_reply_to:
            in_reply_to = found_id
            references = references or found_refs
        subject = subject or found_subject
    refs = []
    for item in references.split() + ([in_reply_to] if in_reply_to else []):
        item = _angle(item)
        if item and item not in refs:
            refs.append(item)
    refs = refs[-MAX_REFERENCES:]
    if in_reply_to and not re.match(r"(?i)\s*re\s*:", subject):
        subject = "Re: " + subject if subject else "Re:"
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    try:
        msg["To"] = recipient
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if refs:
            msg["References"] = " ".join(refs)
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    except (ValueError, TypeError, email.errors.MessageError) as exc:
        raise ValueError("the draft could not be written as an email: %s" % exc) from None
    made = _api("POST", GMAIL_API + "/users/me/drafts", need=GMAIL_COMPOSE, state_dir=state_dir,
                body={"message": {"raw": raw, "threadId": tid}})
    message = made.get("message") if isinstance(made.get("message"), dict) else {}
    draft_id = str(made.get("id") or "")
    thread = str(message.get("threadId") or tid)
    account = (_read_record(_conn_path(state_dir)) or {}).get("account") or ""
    return {"draft_id": draft_id, "message_id": str(message.get("id") or ""), "thread_id": thread,
            "open_url": draft_open_url(thread, draft_id, account),
            "thread_url": _gmail_base(account) + "#all/" + thread}


def gmail_draft_exists(draft_id: str, *, state_dir=None) -> bool:
    """Is this still a draft? ``False`` once it was sent or deleted: Gmail answers 404 then.

    Reads one draft's id and nothing of its content (``format=minimal``). Any other failure is
    raised, never taken as "gone". Needs gmail.compose, the permission the draft was made with.
    """
    did = _check_id(draft_id, "draft_id")
    try:
        _api("GET", GMAIL_API + "/users/me/drafts/" + did, need=GMAIL_COMPOSE,
             params={"format": "minimal"}, state_dir=state_dir)
    except GoogleAPIError as exc:
        if getattr(exc, "status", None) == 404:
            return False
        raise
    return True


_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_GMAIL_URL_ALPHABET = "BCDFGHJKLMNPQRSTVWXZbcdfghjklmnpqrstvwxz"


def _compose_token(plain):
    """Gmail's web-UI id encoding: base64 of the text (without "thread-"), read as a base-64
    number and written in Gmail's 40-consonant alphabet. Matches the published vectors of
    github.com/GoodMeasuresLLC/gmail_compose_encoder and decodes with ArsenalRecon's
    GmailURLDecoder; Google does not document it."""
    digits = base64.b64encode(plain.replace("thread-", "").encode("utf-8")).decode("ascii").rstrip("=")
    number = 0
    for ch in digits:
        number = number * 64 + _B64.index(ch)
    out = []
    while number:
        number, rest = divmod(number, 40)
        out.append(_GMAIL_URL_ALPHABET[rest])
    return "".join(reversed(out))


def _gmail_base(account):
    # ``?authuser=<address>`` selects the signed-in account by address. ``/mail/u/<address>/``
    # does not: Gmail answers it with "Temporary Error (404)" (seen 2026-09-29 on a real draft).
    if account and "@" in account:
        return "https://mail.google.com/mail/?authuser=%s" % urllib.parse.quote(account, safe="@.+-_")
    return "https://mail.google.com/mail/u/0/"


def draft_open_url(thread_id: str, draft_id: str, account: str = "") -> str:
    """A Gmail web address that opens this reply draft for editing.

    ``?authuser=<account>`` picks the right signed-in account; ``#all?compose=<token>`` opens the
    draft, the token being ``thread-f:<decimal thread id>+msg-a:<draft id>`` in Gmail's URL
    encoding (see _compose_token). For an id of any other shape this falls back to the thread,
    where Gmail shows the draft inline.
    """
    base = _gmail_base(account)
    if isinstance(thread_id, str) and _HEX_ID.match(thread_id) and \
            isinstance(draft_id, str) and _DRAFT_ID.match(draft_id):
        return base + "#all?compose=" + _compose_token(
            "thread-f:%d+msg-a:%s" % (int(thread_id, 16), draft_id))
    return base + "#all/" + str(thread_id)


def _rfc3339(value, name):
    if isinstance(value, _dt.datetime):
        aware = value if value.tzinfo else value.astimezone()
        return aware.isoformat(timespec="seconds")
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day).astimezone().isoformat(
            timespec="seconds")
    if isinstance(value, str) and _RFC3339.match(value.strip()):
        return value.strip()
    raise ValueError("%s must be a datetime or an RFC 3339 timestamp such as "
                     "2026-09-30T09:00:00-07:00" % name)


def calendar_events(time_min=None, time_max=None, max_results: int = 50, *,
                    calendar_id: str = "primary", state_dir=None) -> list:
    """Events between ``time_min`` and ``time_max`` (datetimes or RFC 3339 strings; default now
    to seven days from now), recurring events expanded, in start order, cancelled ones left out.

    Returns ``[{id, summary, start, end, all_day, location, attendees_count, html_link}]`` where
    start/end are RFC 3339 for timed events and YYYY-MM-DD for all-day ones. Needs
    calendar.readonly.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    start = _rfc3339(now if time_min is None else time_min, "time_min")
    end = _rfc3339(now + _dt.timedelta(days=7) if time_max is None else time_max, "time_max")
    if not isinstance(calendar_id, str) or not calendar_id or len(calendar_id) > 320 or \
            re.search(r"[/?#\s\\]", calendar_id):
        raise ValueError("calendar_id must be 'primary' or a calendar address")
    count = max(1, min(int(max_results), MAX_EVENTS))
    data = _api("GET", CALENDAR_API + "/calendars/%s/events" % urllib.parse.quote(
        calendar_id, safe="@.+-_"), need=CALENDAR_READ, state_dir=state_dir, params={
        "timeMin": start, "timeMax": end, "singleEvents": "true", "orderBy": "startTime",
        "maxResults": count})
    rows = []
    for ev in (data.get("items") or [])[:count]:
        if not isinstance(ev, dict) or ev.get("status") == "cancelled":
            continue
        when, until = ev.get("start") or {}, ev.get("end") or {}
        all_day = "dateTime" not in when and "date" in when
        people = [a for a in ev.get("attendees") or [] if isinstance(a, dict) and not a.get("resource")]
        rows.append({"id": str(ev.get("id") or ""), "summary": str(ev.get("summary") or "")[:500],
                     "start": str(when.get("dateTime") or when.get("date") or ""),
                     "end": str(until.get("dateTime") or until.get("date") or ""),
                     "all_day": all_day, "location": str(ev.get("location") or "")[:500],
                     "attendees_count": len(people), "html_link": str(ev.get("htmlLink") or "")})
    return rows


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
