"""Shared fakes for the Google connection tests: no network, no platform secret store."""
import json
import os

from harness import google_connect as gc

READ, COMPOSE, CAL = gc.GMAIL_READ, gc.GMAIL_COMPOSE, gc.CALENDAR_READ
ALL = " ".join((COMPOSE, READ, CAL))
REFRESH = "1//0gFAKE-refresh-token_value.for-tests"
ACCESS = "ya29.FAKE-access-token"
CLIENT_ID = "123-fake.apps.googleusercontent.com"
CLIENT_SECRET = "FAKE-client-secret"


class MemoryBackend:
    """Stands in for DPAPI / Keychain so most tests do not depend on the platform."""
    name = "memory"

    def __init__(self):
        self.secrets = {}
        self.fail = False

    def seal(self, secret, path):
        if self.fail:
            raise OSError("sealing refused")
        self.secrets[os.path.abspath(path)] = secret
        return {"ref": "memory"}

    def open(self, sealed, path):
        return self.secrets[os.path.abspath(path)]

    def erase(self, sealed, path):
        self.secrets.pop(os.path.abspath(path), None)


class FakeGoogle:
    """Google's endpoints by URL prefix; the longest matching prefix answers."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def on(self, method, prefix, body=None, status=200, fn=None):
        self.routes[(method, prefix)] = (status, body, fn)

    def __call__(self, method, url, headers=None, data=None, timeout=20):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "data": data, "timeout": timeout})
        hits = [k for k in self.routes if k[0] == method and url.startswith(k[1])]
        if not hits:
            raise AssertionError("unexpected request %s %s" % (method, url))
        status, body, fn = self.routes[max(hits, key=lambda k: len(k[1]))]
        if fn is not None:
            return fn(method, url, headers or {}, data)
        return status, json.dumps(body).encode("utf-8")

    def urls(self, prefix=""):
        return [c["url"] for c in self.calls if c["url"].startswith(prefix)]


def make_env(tmp_path, monkeypatch):
    """A private state dir with a fake OAuth client, a memory secret store and fake Google."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.delenv("COLLIE_GOOGLE_OAUTH_CLIENT", raising=False)
    monkeypatch.setattr(gc, "PACKAGED_CLIENT", str(tmp_path / "packaged-absent.json"))
    backend = MemoryBackend()
    monkeypatch.setattr(gc, "_secret_backend", lambda: backend)
    fake = FakeGoogle()
    monkeypatch.setattr(gc, "_send", fake)
    gc._reset_cache()
    (state / "google-oauth-client.json").write_text(json.dumps({"installed": {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token"}}), encoding="utf-8")
    return {"state": state, "backend": backend, "fake": fake, "tmp": tmp_path}


def connected(scopes=ALL, account="owner@example.com"):
    gc._save_connection(REFRESH, scopes.split(), account=account, client_id=CLIENT_ID)


def refresh_ok(env, scope=ALL, expires_in=3599, token=ACCESS):
    env["fake"].on("POST", gc.TOKEN_URI, {"access_token": token, "expires_in": expires_in,
                                         "scope": scope, "token_type": "Bearer"})


def state_files(state):
    out = {}
    for root, _dirs, files in os.walk(state):
        for name in files:
            with open(os.path.join(root, name), "rb") as handle:
                out[name] = handle.read()
    return out
