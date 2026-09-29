"""Collie's own Google connection: the sign-in, where the refresh token lives, and its states.

No network, no real browser and no real account: Google's endpoints are a recorded-shape fake
installed in place of ``google_connect._send``, and "the browser" is a thread that requests the
loopback redirect the way Google's would.
"""
import base64
import hashlib
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from harness import google_connect as gc
from _google_fakes import (ACCESS, ALL, CAL, COMPOSE, READ, REFRESH, make_env, refresh_ok,
                           state_files)
from _google_fakes import connected as _connected_as


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield make_env(tmp_path, monkeypatch)
    gc._reset_cache()


def _connected(env, scopes=ALL, account="owner@example.com"):
    _connected_as(scopes, account)


_refresh_ok = refresh_ok
_state_files = state_files


# ------------------------------------------------------------------------ client configuration

def test_client_resolution_prefers_env_then_user_file_then_packaged(env, monkeypatch, tmp_path):
    user = gc.client_config()
    assert user["source"] == "user" and user["client_id"].startswith("123-fake")

    other = tmp_path / "env-client.json"
    other.write_text(json.dumps({"installed": {"client_id": "env-id.apps.googleusercontent.com",
                                               "client_secret": "s"}}), encoding="utf-8")
    monkeypatch.setenv("COLLIE_GOOGLE_OAUTH_CLIENT", str(other))
    assert gc.client_config()["client_id"] == "env-id.apps.googleusercontent.com"
    assert gc.client_config()["source"] == "env"

    monkeypatch.delenv("COLLIE_GOOGLE_OAUTH_CLIENT")
    os.remove(env["state"] / "google-oauth-client.json")
    packaged = tmp_path / "packaged.json"
    packaged.write_text(json.dumps({"installed": {"client_id": "pkg.apps.googleusercontent.com",
                                                  "client_secret": "s"}}), encoding="utf-8")
    monkeypatch.setattr(gc, "PACKAGED_CLIENT", str(packaged))
    assert gc.client_config()["source"] == "packaged"


def test_no_client_means_not_configured_and_says_so(env):
    os.remove(env["state"] / "google-oauth-client.json")
    with pytest.raises(gc.NotConfigured):
        gc.client_config()
    st = gc.status()
    assert st["state"] == "not_configured"
    assert "OAuth" in st["message"] or "set up" in st["message"]


def test_a_malformed_client_file_is_not_configured_not_a_crash(env):
    (env["state"] / "google-oauth-client.json").write_text('{"web": {}}', encoding="utf-8")
    assert gc.status()["state"] == "not_configured"


# ------------------------------------------------------------------------ PKCE / URL / callback

def test_pkce_challenge_is_s256_of_the_verifier():
    verifier, challenge = gc._pkce()
    assert 43 <= len(verifier) <= 128
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expect.decode()
    assert gc._pkce()[0] != verifier


def test_auth_url_requests_exactly_the_three_scopes_offline_with_pkce():
    url = gc._auth_url("cid", "http://127.0.0.1:5555", "st4te", "chall", login_hint="")
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == "accounts.google.com"
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert set(q["scope"].split()) == {READ, COMPOSE, CAL} and len(q["scope"].split()) == 3
    assert q["code_challenge"] == "chall" and q["code_challenge_method"] == "S256"
    assert q["state"] == "st4te" and q["response_type"] == "code"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["redirect_uri"] == "http://127.0.0.1:5555"
    assert "include_granted_scopes" not in q and "client_secret" not in q


@pytest.mark.parametrize("peer,host,query,verdict", [
    ("127.0.0.1", "127.0.0.1:5555", "state=good&code=abc", "code"),
    ("127.0.0.1", "127.0.0.1:5555", "state=bad&code=abc", "state_mismatch"),
    ("127.0.0.1", "127.0.0.1:5555", "code=abc", "state_mismatch"),
    ("127.0.0.1", "127.0.0.1:5555", "state=good&error=access_denied", "denied"),
    ("127.0.0.1", "127.0.0.1:5555", "state=good&error=server_error", "error"),
    ("127.0.0.1", "127.0.0.1:5555", "", "ignore"),
    ("192.168.1.9", "127.0.0.1:5555", "state=good&code=abc", "forbidden"),
    ("127.0.0.1", "evil.example:5555", "state=good&code=abc", "forbidden"),
    ("127.0.0.1", "localhost:5555", "state=good&code=abc", "forbidden"),
])
def test_callback_is_judged_on_peer_host_and_state(peer, host, query, verdict):
    got = gc._judge_callback("/?" + query, peer=peer, host=host, port=5555, state="good")
    assert got[0] == verdict


def test_favicon_and_stray_paths_do_not_end_the_wait():
    assert gc._judge_callback("/favicon.ico", peer="127.0.0.1", host="127.0.0.1:5555",
                              port=5555, state="good")[0] == "ignore"


def _browser(env, respond):
    """A fake browser: on open, request the loopback redirect as Google would, in a thread."""
    seen = {}

    def open_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen["auth"] = q
        target = respond(q)

        def go():
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(target, timeout=15) as r:
                    seen["status"], seen["page"] = r.status, r.read().decode("utf-8")
                    seen["headers"] = dict(r.headers)
            except urllib.error.HTTPError as exc:
                seen["status"], seen["page"] = exc.code, exc.read().decode("utf-8")
        seen["thread"] = threading.Thread(target=go, daemon=True)
        seen["thread"].start()
        return True
    return open_browser, seen


def _exchange(env, scope=ALL, refresh=REFRESH, check=None):
    def token(method, url, headers, data):
        form = dict(urllib.parse.parse_qsl(data.decode()))
        if check:
            check(form)
        if form.get("grant_type") == "authorization_code":
            return 200, json.dumps({"access_token": ACCESS, "expires_in": 3599,
                                    "refresh_token": refresh, "scope": scope,
                                    "token_type": "Bearer"}).encode()
        return 200, json.dumps({"access_token": ACCESS, "expires_in": 3599,
                                "scope": scope}).encode()
    env["fake"].on("POST", gc.TOKEN_URI, fn=token)
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/profile", {"emailAddress": "owner@example.com"})
    env["fake"].on("GET", gc.CALENDAR_API + "/calendars/primary", {"id": "owner@example.com"})


def test_connect_verifies_pkce_stores_sealed_token_and_serves_success(env, capsys):
    forms = []
    _exchange(env, check=forms.append)
    browser, seen = _browser(env, lambda q: q["redirect_uri"] + "/?" + urllib.parse.urlencode(
        {"state": q["state"], "code": "4/auth-code", "scope": ALL}))
    st = gc.connect(open_browser=browser, timeout=20)
    seen["thread"].join(10)

    assert st["state"] == "connected" and st["account"] == "owner@example.com"
    assert set(st["granted_scopes"]) == {READ, COMPOSE, CAL}
    form = forms[0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "4/auth-code"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == seen["auth"]["code_challenge"]
    assert form["redirect_uri"] == seen["auth"]["redirect_uri"]
    assert urllib.parse.urlsplit(form["redirect_uri"]).hostname == "127.0.0.1"
    assert seen["status"] == 200 and "You're connected" in seen["page"]
    assert "owner@example.com" in seen["page"]
    assert "default-src 'none'" in seen["headers"].get("Content-Security-Policy", "")
    # sealed by the backend, and not sitting in any file in the state directory
    assert env["backend"].secrets and REFRESH in env["backend"].secrets.values()
    for name, blob in _state_files(env["state"]).items():
        assert REFRESH.encode() not in blob, name
        assert ACCESS.encode() not in blob, name
    out = capsys.readouterr()
    assert REFRESH not in out.out + out.err and ACCESS not in out.out + out.err
    assert "FAKE-client-secret" not in out.out + out.err


def _get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=15) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_a_mismatched_state_is_refused_without_ending_the_sign_in(env):
    # Any local process can reach the loopback port. A wrong state is answered 400 and ignored;
    # the real redirect that follows still completes the sign-in.
    forms = []
    _exchange(env, check=forms.append)
    seen = {}

    def browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

        def go():
            seen["forged"] = _get(q["redirect_uri"] + "/?" + urllib.parse.urlencode(
                {"state": "forged", "code": "4/forged-code"}))
            seen["real"] = _get(q["redirect_uri"] + "/?" + urllib.parse.urlencode(
                {"state": q["state"], "code": "4/auth-code"}))
        seen["thread"] = threading.Thread(target=go, daemon=True)
        seen["thread"].start()
    st = gc.connect(open_browser=browser, timeout=20)
    seen["thread"].join(10)
    assert seen["forged"][0] == 400 and "didn't match" in seen["forged"][1]
    assert seen["real"][0] == 200 and st["state"] == "connected"
    assert [f["code"] for f in forms] == ["4/auth-code"]      # the forged code was never exchanged


def test_a_forged_callback_alone_ends_in_a_timeout_and_stores_nothing(env):
    _exchange(env)
    browser, seen = _browser(env, lambda q: q["redirect_uri"] + "/?" + urllib.parse.urlencode(
        {"state": "forged", "code": "4/auth-code"}))
    with pytest.raises(gc.GoogleError) as info:
        gc.connect(open_browser=browser, timeout=2)
    seen["thread"].join(10)
    assert info.value.code == "timeout"
    assert seen["status"] == 400 and "didn't match" in seen["page"]
    assert not env["fake"].urls(gc.TOKEN_URI)
    assert gc.status()["state"] == "not_connected" and not env["backend"].secrets


def test_the_callback_port_cannot_be_shared(env):
    import socket
    assert gc._CallbackServer.allow_reuse_address is False
    _exchange(env)
    seen = {}

    def browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        port = urllib.parse.urlsplit(q["redirect_uri"]).port
        rival = socket.socket()
        rival.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rival.bind(("127.0.0.1", port))
            seen["bound"] = True
        except OSError:
            seen["bound"] = False
        finally:
            rival.close()

        def go():
            seen["real"] = _get(q["redirect_uri"] + "/?" + urllib.parse.urlencode(
                {"state": q["state"], "code": "4/auth-code"}))
        seen["thread"] = threading.Thread(target=go, daemon=True)
        seen["thread"].start()
    gc.connect(open_browser=browser, timeout=20)
    seen["thread"].join(10)
    assert seen["bound"] is False


def test_connect_reports_a_denied_consent(env):
    _exchange(env)
    browser, seen = _browser(env, lambda q: q["redirect_uri"] + "/?" + urllib.parse.urlencode(
        {"state": q["state"], "error": "access_denied"}))
    with pytest.raises(gc.GoogleError) as info:
        gc.connect(open_browser=browser, timeout=20)
    seen["thread"].join(10)
    assert info.value.code == "denied"
    assert "nothing was connected" in seen["page"].lower()
    assert gc.status()["state"] == "not_connected"


def test_connect_times_out_cleanly(env):
    _exchange(env)
    with pytest.raises(gc.GoogleError) as info:
        gc.connect(open_browser=lambda url: True, timeout=1)
    assert info.value.code == "timeout"


def test_a_code_that_arrives_before_the_deadline_is_waited_for(env):
    # The exchange outlives the timeout. connect() must not report "Nothing was saved" while it
    # is still saving.
    import time as _time
    _exchange(env)
    fast = env["fake"].routes[("POST", gc.TOKEN_URI)][2]

    def slow(method, url, headers, data):
        _time.sleep(2)
        return fast(method, url, headers, data)
    env["fake"].on("POST", gc.TOKEN_URI, fn=slow)
    browser, seen = _browser(env, lambda q: q["redirect_uri"] + "/?" + urllib.parse.urlencode(
        {"state": q["state"], "code": "4/auth-code"}))
    st = gc.connect(open_browser=browser, timeout=1)
    seen["thread"].join(10)
    assert st["state"] == "connected" and seen["status"] == 200


def test_a_redirect_after_the_deadline_is_not_exchanged():
    flow = gc._Flow(client={}, state="s", verifier="v", redirect_uri="http://127.0.0.1:1",
                    port=1, state_dir=None)
    assert flow.close() is False          # nothing in flight when the deadline passed
    assert flow.claim() is False          # so a late redirect cannot start an exchange
    late = gc._Flow(client={}, state="s", verifier="v", redirect_uri="http://127.0.0.1:1",
                    port=1, state_dir=None)
    assert late.claim() is True and late.close() is True


def test_connect_announces_the_url_when_the_browser_cannot_open(env):
    _exchange(env)
    announced = []
    with pytest.raises(gc.GoogleError):
        gc.connect(open_browser=lambda url: False, timeout=1, announce=announced.append)
    assert announced and announced[0].startswith("https://accounts.google.com/")


def test_partial_consent_records_what_google_actually_granted(env):
    _exchange(env, scope=READ + " " + CAL)
    browser, seen = _browser(env, lambda q: q["redirect_uri"] + "/?" + urllib.parse.urlencode(
        {"state": q["state"], "code": "c", "scope": READ + " " + CAL}))
    st = gc.connect(open_browser=browser, timeout=20)
    seen["thread"].join(10)
    assert st["state"] == "missing_scope"
    assert st["missing_scopes"] == [COMPOSE]
    assert st["can"] == {"gmail_read": True, "gmail_drafts": False, "calendar_read": True}
    assert "drafts" in st["message"] and "collie google connect" in st["message"]
    assert "Not allowed" in seen["page"] or "not allowed" in seen["page"]


def test_consent_with_nothing_granted_is_not_stored(env):
    _exchange(env, scope="openid")
    browser, seen = _browser(env, lambda q: q["redirect_uri"] + "/?" + urllib.parse.urlencode(
        {"state": q["state"], "code": "c"}))
    with pytest.raises(gc.GoogleError) as info:
        gc.connect(open_browser=browser, timeout=20)
    seen["thread"].join(10)
    assert info.value.code == "no_scopes"
    assert gc.status()["state"] == "not_connected" and not env["backend"].secrets


def test_account_falls_back_to_calendar_when_gmail_was_not_granted(env):
    _exchange(env, scope=CAL)
    browser, seen = _browser(env, lambda q: q["redirect_uri"] + "/?" + urllib.parse.urlencode(
        {"state": q["state"], "code": "c"}))
    st = gc.connect(open_browser=browser, timeout=20)
    seen["thread"].join(10)
    assert st["account"] == "owner@example.com"
    assert not env["fake"].urls(gc.GMAIL_API)


# ------------------------------------------------------------------------ plaintext import

def test_plaintext_token_is_imported_then_deleted(env):
    plain = env["state"] / "google-token.json"
    plain.write_text(json.dumps({"refresh_token": REFRESH, "scope": ALL, "obtained_at": 1790705135,
                                 "client_id": "123-fake.apps.googleusercontent.com"}),
                     encoding="utf-8")
    st = gc.status()
    assert st["state"] == "connected"
    assert not plain.exists()
    assert REFRESH in env["backend"].secrets.values()
    for name, blob in _state_files(env["state"]).items():
        assert REFRESH.encode() not in blob, name


def test_plaintext_is_kept_when_sealing_fails(env):
    plain = env["state"] / "google-token.json"
    plain.write_text(json.dumps({"refresh_token": REFRESH, "scope": ALL}), encoding="utf-8")
    env["backend"].fail = True
    st = gc.status()
    assert plain.exists()                      # never delete the only copy
    assert st["state"] == "not_connected" and "could not" in st["message"].lower()


def test_plaintext_left_over_after_a_connection_is_just_deleted(env):
    _connected(env)
    plain = env["state"] / "google-token.json"
    plain.write_text(json.dumps({"refresh_token": "1//other-stale"}), encoding="utf-8")
    assert gc.status()["state"] == "connected"
    assert not plain.exists()
    assert REFRESH in env["backend"].secrets.values()


# ------------------------------------------------------------------------ refresh + states

def test_access_token_is_refreshed_once_and_cached(env):
    _connected(env)
    _refresh_ok(env)
    assert gc._access_token() == ACCESS
    assert gc._access_token() == ACCESS
    assert len(env["fake"].urls(gc.TOKEN_URI)) == 1
    form = dict(urllib.parse.parse_qsl(env["fake"].calls[0]["data"].decode()))
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == REFRESH
    assert REFRESH not in env["fake"].calls[0]["url"]          # never in a URL


def test_an_expiring_access_token_is_refreshed_again(env):
    _connected(env)
    _refresh_ok(env, expires_in=30)            # inside the 60 s margin
    gc._access_token()
    gc._access_token()
    assert len(env["fake"].urls(gc.TOKEN_URI)) == 2


def test_invalid_grant_means_needs_reconnect_and_persists(env):
    _connected(env)
    env["fake"].on("POST", gc.TOKEN_URI, {"error": "invalid_grant",
                                         "error_description": "Token has been expired or revoked."},
                   status=400)
    with pytest.raises(gc.NeedsReconnect) as info:
        gc._access_token()
    assert "collie google connect" in str(info.value)
    gc._reset_cache()
    st = gc.status()
    assert st["state"] == "needs_reconnect"
    assert "collie google connect" in st["message"] and "Settings" in st["message"]
    calls = len(env["fake"].calls)
    with pytest.raises(gc.NeedsReconnect):
        gc._access_token()
    assert len(env["fake"].calls) == calls       # no pointless refresh once it is known dead


def test_status_check_turns_a_revoked_grant_into_needs_reconnect(env):
    _connected(env)
    env["fake"].on("POST", gc.TOKEN_URI, {"error": "invalid_grant"}, status=400)
    assert gc.status()["state"] == "connected"               # no network without check
    assert gc.status(check=True)["state"] == "needs_reconnect"


def test_a_sign_in_that_cannot_be_unsealed_reads_as_needs_reconnect(env, monkeypatch):
    _connected(env)

    def refused(sealed, path):
        raise OSError("the store refused")
    monkeypatch.setattr(env["backend"], "open", refused)
    st = gc.status(check=True)
    assert st["state"] == "needs_reconnect"
    assert "unseal" in st["message"] and "collie google connect" in st["message"]
    assert not env["fake"].calls


def test_a_different_client_needs_reconnect(env):
    gc._save_connection(REFRESH, ALL.split(), account="a@example.com",
                        client_id="999-other.apps.googleusercontent.com")
    st = gc.status()
    assert st["state"] == "needs_reconnect"


def test_status_distinguishes_every_state(env):
    assert gc.status()["state"] == "not_connected"
    _connected(env, scopes=READ + " " + CAL)
    assert gc.status()["state"] == "missing_scope"
    _connected(env)
    st = gc.status()
    assert st["state"] == "connected" and st["account"] == "owner@example.com"
    assert st["missing_scopes"] == [] and st["storage"] == "memory"
    assert REFRESH not in json.dumps(st)


def test_network_failure_is_a_clear_error_not_a_reconnect(env, monkeypatch):
    _connected(env)

    def down(*a, **k):
        raise gc.GoogleError("network", "Could not reach Google: timed out")
    monkeypatch.setattr(gc, "_send", down)
    with pytest.raises(gc.GoogleError) as info:
        gc._access_token()
    assert info.value.code == "network"
    assert gc.status()["state"] == "connected"


# ------------------------------------------------------------------------ disconnect

def test_disconnect_revokes_with_the_token_in_the_body_then_forgets(env):
    _connected(env)
    env["fake"].on("POST", gc.REVOKE_URI, {})
    out = gc.disconnect()
    assert out["revoked"] is True and out["removed"] is True
    call = [c for c in env["fake"].calls if c["url"].startswith(gc.REVOKE_URI)][0]
    assert REFRESH not in call["url"]
    assert dict(urllib.parse.parse_qsl(call["data"].decode()))["token"] == REFRESH
    assert gc.status()["state"] == "not_connected"
    assert not env["backend"].secrets
    assert not (env["state"] / "google-connection.json").exists()


def test_disconnect_forgets_locally_even_when_revoke_fails(env):
    _connected(env)
    env["fake"].on("POST", gc.REVOKE_URI, {"error": "invalid_token"}, status=400)
    out = gc.disconnect()
    assert out["removed"] is True and out["revoked"] is False
    assert "myaccount.google.com" in out["message"]
    assert gc.status()["state"] == "not_connected"


def test_disconnect_when_not_connected_is_a_no_op(env):
    out = gc.disconnect()
    assert out["removed"] is False and not env["fake"].calls


# ------------------------------------------------------------------------ real secret storage

@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_backend_round_trips_and_never_writes_the_token(tmp_path):
    backend = gc._DpapiBackend()
    path = str(tmp_path / "google-connection.json")
    sealed = backend.seal(REFRESH, path)
    assert REFRESH not in json.dumps(sealed)
    assert backend.open(sealed, path) == REFRESH


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_file_backend_is_owner_only_and_outside_the_json(tmp_path):
    backend = gc._FileBackend()
    path = str(tmp_path / "google-connection.json")
    sealed = backend.seal(REFRESH, path)
    secret_path = tmp_path / sealed["file"]
    assert (os.stat(secret_path).st_mode & 0o777) == 0o600
    assert REFRESH not in json.dumps(sealed)
    assert backend.open(sealed, path) == REFRESH
    backend.erase(sealed, path)
    assert not secret_path.exists()


def test_keychain_backend_never_puts_the_token_on_a_command_line(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0
        stdout = REFRESH + "\n"
        stderr = ""

    def run(args, **kw):
        calls.append((args, kw))
        return Done()
    monkeypatch.setattr(gc.subprocess, "run", run)
    backend = gc._KeychainBackend()
    path = str(tmp_path / "google-connection.json")
    sealed = backend.seal(REFRESH, path)
    assert backend.open(sealed, path) == REFRESH
    backend.erase(sealed, path)
    for args, kw in calls:
        assert all(REFRESH not in a for a in args)
    assert any(REFRESH in (kw.get("input") or "") for _a, kw in calls)
    assert all(kw.get("errors") for _a, kw in calls if kw.get("text"))


def test_keychain_backend_refuses_characters_it_cannot_quote(tmp_path):
    with pytest.raises(gc.GoogleError):
        gc._KeychainBackend().seal('bad" token', str(tmp_path / "x.json"))


def test_platform_backend_is_chosen_by_os(monkeypatch):
    from harness import plat
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "is_macos", lambda: False)
    assert gc._secret_backend().name == "dpapi"
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(plat, "is_macos", lambda: True)
    assert gc._secret_backend().name == "keychain"
    monkeypatch.setattr(plat, "is_macos", lambda: False)
    assert gc._secret_backend().name == "file"
