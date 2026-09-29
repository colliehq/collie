"""`collie google connect | status | disconnect`, over the fake Google."""
import pytest

from harness import cli, google_connect as gc
from _google_fakes import REFRESH, connected, make_env, refresh_ok


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield make_env(tmp_path, monkeypatch)
    gc._reset_cache()


def test_google_is_a_top_level_command():
    assert "google" in cli.CMDS


def test_cli_status_reports_the_state_and_never_the_token(env, capsys):
    assert cli.main(["google", "status"]) == 1
    assert "not connected" in capsys.readouterr().out.lower()
    connected()
    refresh_ok(env)
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/profile", {"emailAddress": "owner@example.com"})
    assert cli.main(["google", "status"]) == 0
    out = capsys.readouterr().out
    assert "owner@example.com" in out and "connected" in out.lower()
    assert "never sends" in out.lower()
    assert REFRESH not in out and "FAKE-client-secret" not in out


def test_cli_status_says_how_to_reconnect(env, capsys):
    connected()
    env["fake"].on("POST", gc.TOKEN_URI, {"error": "invalid_grant"}, status=400)
    assert cli.main(["google", "status"]) == 1
    assert "collie google connect" in capsys.readouterr().out


def test_cli_connect_prints_the_link_and_the_result(env, monkeypatch, capsys):
    def fake_connect(open_browser=None, timeout=300, announce=None, **kw):
        announce("https://accounts.google.com/o/oauth2/v2/auth?client_id=x")
        connected()
        return gc.status()
    monkeypatch.setattr(gc, "connect", fake_connect)
    assert cli.main(["google", "connect"]) == 0
    out = capsys.readouterr().out
    assert "https://accounts.google.com/" in out and "owner@example.com" in out


def test_cli_connect_failure_exits_nonzero(env, monkeypatch, capsys):
    def refused(**kw):
        raise gc.GoogleError("denied", "You chose not to give Collie access. Nothing was saved.")
    monkeypatch.setattr(gc, "connect", refused)
    assert cli.main(["google", "connect"]) == 1
    assert "Nothing was saved" in capsys.readouterr().out


def test_cli_disconnect_revokes_and_forgets(env, capsys):
    connected()
    env["fake"].on("POST", gc.REVOKE_URI, {})
    assert cli.main(["google", "disconnect"]) == 0
    out = capsys.readouterr().out
    assert "disconnected" in out.lower()
    assert gc.status()["state"] == "not_connected"


def _uninstall_home(env, monkeypatch, tmp_path):
    import types
    import urllib.parse
    from harness import plat
    from _google_fakes import ALL, CLIENT_ID
    home = tmp_path / "home"
    cdir = home / ".collie"
    cdir.mkdir(parents=True)
    gc._save_connection(REFRESH, ALL.split(), account="owner@example.com", client_id=CLIENT_ID,
                        state_dir=str(cdir))
    env["fake"].on("POST", gc.REVOKE_URI, {})
    monkeypatch.setattr(cli.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.setattr(cli, "_collie_procs", lambda: [])
    monkeypatch.setattr(plat, "is_macos", lambda: False)

    def revoked():
        return [dict(urllib.parse.parse_qsl(c["data"].decode()))["token"]
                for c in env["fake"].calls if c["url"] == gc.REVOKE_URI]
    return cdir, revoked, lambda yes: cli.cmd_uninstall(
        types.SimpleNamespace(yes=yes, keep_config=False))


def test_uninstall_dry_run_names_the_google_connection_and_touches_nothing(env, monkeypatch,
                                                                            tmp_path, capsys):
    cdir, revoked, uninstall = _uninstall_home(env, monkeypatch, tmp_path)
    assert uninstall(False) == 0
    assert "Google" in capsys.readouterr().out
    assert revoked() == [] and env["backend"].secrets


def test_uninstall_revokes_google_and_removes_the_sealed_sign_in(env, monkeypatch, tmp_path,
                                                                 capsys):
    # Deleting ~/.collie alone would leave the grant live at Google and, on macOS, the
    # Keychain item behind.
    cdir, revoked, uninstall = _uninstall_home(env, monkeypatch, tmp_path)
    assert uninstall(True) == 0
    assert revoked() == [REFRESH]
    assert not env["backend"].secrets and not cdir.exists()
    assert REFRESH not in capsys.readouterr().out
