"""The desktop's update notice: when it may contact the feed, what it claims, and what Install runs.

Nothing here reaches GitHub or runs an installer. The feed is a staged ``checker``, the install is a
staged ``spawn`` whose child exits with a chosen code, and every file lives in a temporary directory
named through the module's environment overrides.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import types

import pytest

from harness import update, update_notice


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_UPDATE_STATUS", str(tmp_path / "update-status.json"))
    monkeypatch.setenv("COLLIE_UPDATE_INSTALL_LOG", str(tmp_path / "logs" / "update-install.log"))
    monkeypatch.setenv("COLLIE_UPDATE_JOURNAL", str(tmp_path / "update-journal.json"))
    monkeypatch.delenv("COLLIE_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update_notice, "__version__", "0.29.1")
    monkeypatch.setattr(update_notice, "_checking", {"active": False})
    monkeypatch.setattr(update_notice, "_child", {"proc": None})
    monkeypatch.setattr(update, "install_kind", lambda: "setup")
    monkeypatch.setattr(update_notice, "_one_press_supported", lambda kind: kind == "setup")
    monkeypatch.setattr(update_notice, "auto_enabled", lambda: False)
    return tmp_path


def _feed(latest="0.30.0", calls=None):
    def checker(channel):
        if calls is not None:
            calls.append(channel)
        return {"current": "0.29.1", "latest": latest, "newer": True, "channel": "stable",
                "prerelease": False, "kind": "setup", "notes": "Better updates.\nSecond line.",
                "url": "https://example.test/v" + latest, "assets": {}, "digests": {}}
    return checker


def _offline(channel):
    raise OSError("the network is unreachable")


# ---------------------------------------------------------------- when the feed may be contacted

def test_reading_the_notice_never_contacts_the_feed(state, monkeypatch):
    monkeypatch.setattr(update, "check", lambda *_a, **_k: pytest.fail("status() reached the feed"))
    value = update_notice.status()
    assert value["current"] == "0.29.1"
    assert value["latest"] is None and value["newer"] is False and value["checked_at"] is None
    assert value["error"] == ""


def test_automatic_checks_are_off_unless_the_person_turned_them_on(state):
    started = []
    assert update_notice.maybe_background_check(start=started.append) is False
    assert started == []


def test_an_enabled_automatic_check_runs_once_a_day_at_most(state, monkeypatch):
    monkeypatch.setattr(update_notice, "auto_enabled", lambda: True)
    started = []
    assert update_notice.maybe_background_check(now=1000.0, start=started.append) is True
    # The claim is taken before the thread runs, so a second poll in the same moment starts nothing.
    assert update_notice.maybe_background_check(now=1000.0, start=started.append) is False
    assert len(started) == 1
    calls = []
    monkeypatch.setattr(update, "check", _feed(calls=calls))
    monkeypatch.setattr(update_notice.time, "time", lambda: 1000.0)   # the thread reads the clock
    started[0]()
    assert calls == [None] and update_notice._checking["active"] is False
    assert update_notice.maybe_background_check(now=1000.0 + 3600, start=started.append) is False
    assert update_notice.maybe_background_check(
        now=1000.0 + update_notice.CHECK_INTERVAL_S + 1, start=started.append) is True


def test_a_check_stamped_in_the_future_does_not_block_automatic_checks(state, monkeypatch):
    monkeypatch.setattr(update_notice, "auto_enabled", lambda: True)
    update_notice.check_now(force=True, now=9_000_000.0, checker=_feed())    # clock later set back
    assert update_notice.maybe_background_check(now=1000.0, start=lambda fn: None) is True


def test_a_failed_automatic_check_backs_off_before_retrying(state, monkeypatch):
    monkeypatch.setattr(update_notice, "auto_enabled", lambda: True)
    update_notice.check_now(force=True, now=5000.0, checker=_offline)
    started = []
    assert update_notice.maybe_background_check(now=5000.0 + 60, start=started.append) is False
    assert update_notice.maybe_background_check(
        now=5000.0 + update_notice.ERROR_BACKOFF_S + 1, start=started.append) is True


def test_a_second_press_reuses_the_check_that_just_finished(state):
    calls = []
    update_notice.check_now(now=100.0, checker=_feed(calls=calls))
    update_notice.check_now(now=110.0, checker=_feed(calls=calls))
    assert calls == [None]
    update_notice.check_now(now=100.0 + update_notice.MANUAL_REUSE_S + 1, checker=_feed(calls=calls))
    assert len(calls) == 2
    update_notice.check_now(now=200.0, force=True, checker=_feed(calls=calls))
    assert len(calls) == 3


# ---------------------------------------------------------------- what the notice claims

def test_a_failed_check_is_reported_as_a_failure_not_as_up_to_date(state):
    value = update_notice.check_now(now=100.0, checker=_offline)
    assert value["newer"] is False
    assert value["checked_at"] is None                       # nothing was learned
    assert "network is unreachable" in value["error"]
    assert value["error_at"] == 100.0
    assert value["checking"] is False


def test_a_failure_after_a_success_keeps_the_last_answer_and_says_it_failed(state):
    update_notice.check_now(now=100.0, checker=_feed())
    value = update_notice.check_now(now=200.0, checker=_offline)
    assert value["latest"] == "0.30.0" and value["newer"] is True
    assert value["checked_at"] == 100.0 and value["error_at"] == 200.0 and value["error"]


def test_a_finished_check_is_not_reported_as_still_checking(state):
    value = update_notice.check_now(now=100.0, checker=_feed())
    assert value["checking"] is False
    assert value["newer"] is True and value["one_press"] is True
    assert value["notes"].startswith("Better updates.")
    assert value["command"] == "collie update --channel stable --yes"


def test_the_notice_follows_the_running_version_not_the_one_that_checked(state, monkeypatch):
    update_notice.check_now(now=100.0, checker=_feed())
    monkeypatch.setattr(update_notice, "__version__", "0.30.0")    # the update was installed
    value = update_notice.status(now=200.0)
    assert value["current"] == "0.30.0" and value["newer"] is False
    assert value["notes"] == "" and value["one_press"] is False
    # A check made by the older copy does not count as this copy's daily check.
    monkeypatch.setattr(update_notice, "auto_enabled", lambda: True)
    assert update_notice.maybe_background_check(now=200.0, start=lambda fn: None) is True


def test_a_remote_viewer_sees_the_notice_but_is_not_offered_install(state):
    update_notice.check_now(now=100.0, checker=_feed())
    value = update_notice.status(local=False)
    assert value["newer"] is True and value["one_press"] is False and value["local"] is False


def test_install_kinds_without_one_press_get_the_command(state, monkeypatch):
    monkeypatch.setattr(update, "install_kind", lambda: "pip")
    update_notice.check_now(now=100.0, checker=_feed())
    value = update_notice.status()
    assert value["newer"] is True and value["one_press"] is False and value["kind"] == "pip"


def test_one_press_is_only_claimed_for_a_windows_installer_copy(monkeypatch):
    monkeypatch.setattr("harness.plat.is_windows", lambda: True)
    assert update_notice._one_press_supported("setup") is True
    for kind in ("app", "brew", "pip"):
        assert update_notice._one_press_supported(kind) is False
    monkeypatch.setattr("harness.plat.is_windows", lambda: False)
    assert update_notice._one_press_supported("setup") is False


# ---------------------------------------------------------------- what Install runs

class _Child:
    def __init__(self, code=0, pid=4242):
        self.code, self.pid, self.polled = code, pid, None

    def poll(self):
        return self.polled

    def wait(self):
        self.polled = self.code
        return self.code


def _install(state, *, code=0, expect="0.30.0", now=300.0, **kwargs):
    seen, watchers = {}, []

    def spawn(argv, **popen):
        seen["argv"], seen["popen"] = argv, popen
        popen["stdout"].write(b"downloading\nupdate failed: digest mismatch\n" if code else b"handed off\n")
        return _Child(code)

    value = update_notice.start_install(expect, spawn=spawn, watch=watchers.append,
                                        python="python-under-test", now=now, **kwargs)
    return value, seen, watchers


def test_install_runs_the_verified_cli_bound_to_the_version_shown(state, monkeypatch):
    monkeypatch.setenv("COLLIE_PROCESS_OWNER", "slackexec")
    update_notice.check_now(now=100.0, checker=_feed())
    value, seen, watchers = _install(state)
    assert seen["argv"] == ["python-under-test", "-m", "harness.cli", "update", "--channel", "stable",
                            "--yes", "--expect", "0.30.0"]
    assert seen["popen"]["stdin"] is subprocess.DEVNULL
    assert seen["popen"]["stderr"] is subprocess.STDOUT
    assert "COLLIE_PROCESS_OWNER" not in seen["popen"]["env"]
    assert value["install"]["state"] == "running" and value["install"]["target"] == "0.30.0"
    watchers[0]()                                            # the child exits 0: handed off
    after = update_notice.status()
    assert after["install"]["state"] == "handed_off" and after["install"]["exit_code"] == 0


def test_a_failed_install_reports_the_cli_reason_and_its_log(state):
    update_notice.check_now(now=100.0, checker=_feed())
    _value, _seen, watchers = _install(state, code=1)
    watchers[0]()
    after = update_notice.status(now=400.0)
    assert after["install"]["state"] == "failed"
    assert after["install"]["detail"] == "update failed: digest mismatch"
    assert "digest mismatch" in after["install"]["log_tail"]


@pytest.mark.parametrize("expect", ["0.30.1", "", "0.29.1"])
def test_install_refuses_a_version_other_than_the_newer_one_shown(state, expect):
    update_notice.check_now(now=100.0, checker=_feed())
    with pytest.raises(update_notice.UpdateRefused) as refused:
        _install(state, expect=expect)
    assert refused.value.status == 409


def test_install_refuses_without_a_newer_release(state):
    update_notice.check_now(now=100.0, checker=_feed(latest="0.29.1"))
    with pytest.raises(update_notice.UpdateRefused):
        _install(state, expect="0.29.1")


def test_install_refuses_a_remote_request_and_other_install_kinds(state, monkeypatch):
    update_notice.check_now(now=100.0, checker=_feed())
    with pytest.raises(update_notice.UpdateRefused) as remote:
        _install(state, local=False)
    assert remote.value.status == 403
    monkeypatch.setattr(update, "install_kind", lambda: "app")
    with pytest.raises(update_notice.UpdateRefused) as kind:
        _install(state)
    assert kind.value.status == 400


def test_install_refuses_a_second_press_while_one_is_running_or_waiting(state):
    update_notice.check_now(now=100.0, checker=_feed())
    _value, _seen, watchers = _install(state)
    with pytest.raises(update_notice.UpdateRefused, match="already being installed"):
        _install(state)
    watchers[0]()                                            # handed off, server not yet closed
    with pytest.raises(update_notice.UpdateRefused, match="waiting to run"):
        _install(state)


# ---------------------------------------------------------------- after the server was replaced

def _started_by_an_earlier_server(state, *, target="0.30.0", journal=None, started=None,
                                  recorded="handed_off"):
    update_notice.check_now(now=100.0, checker=_feed(latest=target))
    update_notice._merge({"install": {"state": recorded, "target": target, "server_pid": -7,
                                      "started_at": time.time() if started is None else started,
                                      "from_version": "0.29.1", "log": str(state / "missing.log")}})
    if journal is not None:
        update._write_update_journal(journal)


def test_a_restarted_server_on_the_target_version_reports_installed(state, monkeypatch):
    _started_by_an_earlier_server(state)
    monkeypatch.setattr(update_notice, "__version__", "0.30.0")
    assert update_notice.status()["install"]["state"] == "installed"


def test_a_restarted_server_still_on_the_old_version_does_not_claim_success(state, monkeypatch):
    boot = state / "collie-update.log"
    boot.write_text("[collie-update] installer exit code: 5\n[collie-update] installer FAILED\n")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(state))
    _started_by_an_earlier_server(state, journal={"schema": 1, "state": "pending_startup"})
    view = update_notice.status()["install"]
    assert view["state"] == "unconfirmed"
    assert "installer FAILED" in view["installer_log_tail"]


def test_a_journal_failure_is_reported_as_the_install_failing(state):
    _started_by_an_earlier_server(state, journal={"schema": 1, "state": "install_failed",
                                                  "last_error": "installer exited 2"})
    view = update_notice.status()["install"]
    assert view["state"] == "failed" and view["detail"] == "installer exited 2"


# ---------------------------------------------------------------- the CLI side of the contract

def _cli_args(**kw):
    base = {"channel": None, "yes": True, "expect": ""}
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.mark.parametrize("latest,newer", [("0.30.1", True), ("0.29.1", False)])
def test_cli_expect_refuses_anything_but_the_confirmed_newer_release(monkeypatch, latest, newer):
    from harness import cli
    monkeypatch.setattr(update, "check", lambda channel: {
        "current": "0.29.1", "latest": latest, "newer": newer, "channel": "stable",
        "kind": "setup", "notes": "", "url": "", "assets": {"Collie-Setup.exe": "x"}, "digests": {}})
    monkeypatch.setattr(update, "_download", lambda *a, **k: pytest.fail("downloaded"))
    monkeypatch.setattr(update, "apply_windows", lambda *a, **k: pytest.fail("installed"))
    assert cli.cmd_update(_cli_args(expect="0.30.0")) == 3


def test_cli_passes_the_release_version_into_the_update_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_UPDATE_JOURNAL", str(tmp_path / "journal.json"))
    artifact = tmp_path / "Collie-Setup.exe"
    artifact.write_bytes(b"installer")
    monkeypatch.setattr(update, "verify_digest", lambda *a: (True, "digest ok"))
    monkeypatch.setattr(update, "verify_windows_authenticode", lambda *a: (True, "signed"))
    monkeypatch.setattr(update, "_install_root", lambda: "")
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    ok, _why = update.apply_windows(str(artifact), "sha256:x", on_note=lambda _m: None,
                                    target_version="0.30.0")
    assert ok
    journal = json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))
    assert journal["target_version"] == "0.30.0"
    assert journal["state"] == "pending_startup"


def test_an_outcome_is_news_for_a_day_then_the_card_returns_to_the_last_check(state):
    _started_by_an_earlier_server(state, journal={"schema": 1, "state": "install_failed",
                                                  "last_error": "installer exited 2"},
                                  started=time.time() - update_notice.OUTCOME_TTL_S - 5)
    value = update_notice.status()
    assert value["install"] == {"state": "none"}
    assert value["newer"] is True and value["one_press"] is True     # offered again


def test_a_failure_is_cleared_once_this_copy_reached_the_target_another_way(state, monkeypatch):
    _started_by_an_earlier_server(state, recorded="failed")
    assert update_notice.status()["install"]["state"] == "failed"
    monkeypatch.setattr(update_notice, "__version__", "0.30.0")     # e.g. `collie update --yes`
    assert update_notice.status()["install"]["state"] == "installed"
    monkeypatch.setattr(update_notice, "__version__", "0.31.0")     # and later past it
    assert update_notice.status()["install"] == {"state": "none"}


@pytest.mark.parametrize("kind,command", [
    ("setup", "collie update --channel stable --yes"), ("pip", "collie update --channel stable --yes"),
    ("brew", "brew upgrade collie"), ("app", "")])
def test_the_command_shown_is_one_this_install_can_run(state, monkeypatch, kind, command):
    monkeypatch.setattr(update, "install_kind", lambda: kind)
    assert update_notice.status()["command"] == command


# --- from the independent review ------------------------------------------------------------

def test_a_handoff_the_installer_never_acted_on_stops_blocking_a_retry(state):
    update_notice.check_now(now=100.0, checker=_feed())
    _value, _seen, watchers = _install(state)          # started at 300
    watchers[0]()                                       # handed off (finished_at is wall time)
    install = update_notice._read()["install"]
    install["finished_at"] = 300.0
    update_notice._merge({"install": install})
    soon = update_notice.status(now=300.0 + 30)["install"]
    assert soon["state"] == "handed_off"                # still waiting for Setup to close us
    late = update_notice.status(now=300.0 + update_notice.HANDOFF_WAIT_S + 1)["install"]
    assert late["state"] == "unconfirmed"               # Setup never closed this server
    value, seen, _watchers = _install(state, now=300.0 + update_notice.HANDOFF_WAIT_S + 1)
    assert value["install"]["state"] == "running" and seen["argv"][-1] == "0.30.0"


def test_a_download_still_running_from_a_gone_server_is_not_started_twice(state, monkeypatch):
    update_notice.check_now(now=100.0, checker=_feed())
    update_notice._merge({"install": {"state": "running", "target": "0.30.0", "server_pid": -7,
                                      "pid": 4321, "started_at": 250.0, "log": "x"}})
    monkeypatch.setattr(update_notice, "_pid_alive", lambda pid: pid == 4321)
    with pytest.raises(update_notice.UpdateRefused, match="already being installed"):
        _install(state)                                 # clock 300: 50s old and alive
    update_notice._merge({"install": {"state": "running", "target": "0.30.0", "server_pid": -7,
                                      "pid": 4321, "started_at": 300.0 - update_notice.RUNNING_STALE_S - 1,
                                      "log": "x"}})
    value, _seen, _w = _install(state)                  # far too old: a reused pid, not a download
    assert value["install"]["state"] == "running"


def test_a_finished_watcher_does_not_drop_a_newer_childs_handle(state):
    update_notice.check_now(now=100.0, checker=_feed())
    _value, _seen, first = _install(state)
    newer = object()
    update_notice._child["proc"] = newer                # as if another install replaced it
    first[0]()
    assert update_notice._child["proc"] is newer


def test_pid_alive_reads_without_signalling():
    import os as _os
    assert update_notice._pid_alive(_os.getpid()) is True
    assert update_notice._pid_alive(0) is False and update_notice._pid_alive("x") is False


def test_the_cli_installs_one_update_at_a_time(monkeypatch, tmp_path, capsys):
    from harness import cli, supervisor
    monkeypatch.setenv("USERPROFILE", str(tmp_path)); monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(update, "check", lambda channel: {
        "current": "0.29.1", "latest": "0.30.0", "newer": True, "channel": "stable",
        "kind": "setup", "notes": "", "url": "", "assets": {"Collie-Setup.exe": "x"}, "digests": {}})
    monkeypatch.setattr(update, "_download", lambda *a, **k: pytest.fail("downloaded"))
    held = supervisor.InstanceLock(str(tmp_path / ".collie" / "update.lock"))
    try:
        assert cli.cmd_update(_cli_args()) == 4
    finally:
        held.close()
    assert "Another Collie update is already running" in capsys.readouterr().err
