"""What the desktop knows about a newer Collie, and the one press that installs it.

`collie update` already checks the release feed, verifies the download (the published digest plus
Authenticode on Windows, Gatekeeper plus the signing team on macOS) and hands the Windows installer
to a bootstrap that outlives Collie. None of that was visible on the desktop, so an installed copy
stayed at whatever version it was installed at. Measured 2026-09-23: the developer's own machine
was running 0.24.0 while 0.29.1 was published -- five releases it had never heard of.

This module is the desktop's side of the same updater:

* A check contacts api.github.com. The privacy policy promises that Collie only uses a network
  destination for a feature the person turned on, so a check happens only when they press "Check
  for updates", or after they enable ``UPDATE_CHECK`` (off by default) -- and then at most once per
  ``CHECK_INTERVAL_S``. A failed check is reported as a failure with the time it happened; it never
  turns into "up to date".
* The last answer is kept in ``~/.collie/update-status.json``: opening Settings reads the file, not
  the network, and a restarted server still knows what it saw and what it was installing.
* Installing runs ``collie update --yes`` in a child process instead of calling ``apply_windows``
  inside the web server. On Windows the installer closes every process under the install root --
  this server included -- and the CLI already hands the installer to a PowerShell bootstrap that
  waits for it and restarts what was running. Measured on the scheduled-task install: the task's
  job has no kill-on-close limit and a child keeps running after the task's process exits, so the
  handoff survives the server being closed. The child is bound to the version the person was shown
  (``--expect``), so a release published between reading the notice and pressing Install is not
  silently installed in its place.

One-press install is offered only where it has been exercised end to end: a Windows installer
("setup") copy. Every other install kind is shown the exact command instead.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from . import __version__

SCHEMA = 1
CHECK_INTERVAL_S = 20 * 3600       # automatic checks: at most about once a day
ERROR_BACKOFF_S = 3600             # after a failed automatic check, wait before trying again
MANUAL_REUSE_S = 30                # a second press within this window answers from the last check
OUTCOME_TTL_S = 24 * 3600          # how long "updated" / "not installed" stays on the card
_NOTES_MAX = 4000
_LOG_TAIL_MAX = 1600

_lock = threading.RLock()          # guards the status file and the in-process install record
_check_lock = threading.Lock()     # one feed request at a time; a concurrent caller waits for it
_claim_lock = threading.Lock()     # makes "is an automatic check running? then start one" atomic
_checking = {"active": False}
_child = {"proc": None}            # the install this process started, while its watcher runs


class UpdateRefused(Exception):
    """An install request that must not proceed. ``status`` is the HTTP status to answer with."""

    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def status_path(path=None):
    return os.path.abspath(path or os.environ.get("COLLIE_UPDATE_STATUS")
                           or os.path.expanduser("~/.collie/update-status.json"))


def install_log_path():
    return os.path.abspath(os.environ.get("COLLIE_UPDATE_INSTALL_LOG")
                           or os.path.expanduser("~/.collie/logs/update-install.log"))


def _read(path=None):
    try:
        with open(status_path(path), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write(value, path=None):
    path = status_path(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _merge(patch, path=None):
    with _lock:
        value = _read(path)
        value.update(patch)
        value["schema"] = SCHEMA
        _write(value, path)
        return value


def auto_enabled():
    from . import settings
    return str(settings.get("UPDATE_CHECK", "off") or "off").strip().lower() in ("on", "1", "true")


def _newer(latest, current):
    from .update import _release_version
    candidate = _release_version(latest)
    installed = _release_version(current)
    return bool(candidate and (not installed or candidate > installed))


def check_now(channel=None, *, force=False, now=None, checker=None, path=None, local=True):
    """Ask the release feed now and remember the answer (or the failure).

    ``force`` is False for a person pressing the button: a second press inside ``MANUAL_REUSE_S``
    answers from the check that just finished instead of spending another anonymous GitHub request
    (the feed allows 60 an hour per address).
    """
    from . import update
    checker = checker or update.check
    with _check_lock:
        clock = float(time.time() if now is None else now)
        previous = _read(path)
        last = float(previous.get("checked_at") or 0)
        reuse = (not force and last and clock - last < MANUAL_REUSE_S
                 and previous.get("checked_version") == __version__
                 and not previous.get("error"))
        if not reuse:
            _checking["active"] = True
            try:
                info = checker(channel)
            except Exception as exc:
                detail = ("%s: %s" % (type(exc).__name__, exc)).strip()[:300]
                _merge({"error": detail, "error_at": clock, "attempted_at": clock}, path)
            else:
                _merge({"checked_at": clock, "attempted_at": clock, "error": "", "error_at": 0,
                        "checked_version": __version__,
                        "channel": str(info.get("channel") or channel or "stable"),
                        "latest": str(info.get("latest") or ""),
                        "url": str(info.get("url") or ""),
                        "notes": str(info.get("notes") or "")[:_NOTES_MAX],
                        "prerelease": info.get("prerelease") is True}, path)
            finally:
                _checking["active"] = False
    return status(now=clock, path=path, local=local)


def maybe_background_check(*, now=None, path=None, start=None):
    """Start one automatic check when the person enabled them and the last one is old enough.

    Returns True when a check was started. Never touches the network when UPDATE_CHECK is off.
    """
    if not auto_enabled():
        return False
    clock = float(time.time() if now is None else now)
    with _claim_lock:                # two polls arriving together must not start two checks
        if _checking["active"]:
            return False
        value = _read(path)
        # A timestamp ahead of the clock (the clock was set back since) counts as old, not as a
        # wait that lasts until the clock catches up again.
        checked_age = clock - float(value.get("checked_at") or 0)
        if (float(value.get("checked_at") or 0) and value.get("checked_version") == __version__
                and 0 <= checked_age < CHECK_INTERVAL_S):
            return False
        error_age = clock - float(value.get("error_at") or 0)
        if value.get("error") and 0 <= error_age < ERROR_BACKOFF_S:
            return False
        _checking["active"] = True   # claimed before the thread exists

    def run():
        try:
            check_now(force=True, path=path)
        except Exception:
            pass
        finally:
            _checking["active"] = False

    (start or (lambda fn: threading.Thread(target=fn, name="collie-update-check",
                                           daemon=True).start()))(run)
    return True


def _one_press_supported(kind):
    from . import plat
    return kind == "setup" and plat.is_windows()


def _log_tail(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _LOG_TAIL_MAX))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _install_view(value, journal, clock):
    """The install as the person should read it, from this process's watcher or the journal."""
    record = value.get("install") if isinstance(value.get("install"), dict) else {}
    if not record:
        return {"state": "none"}
    out = {k: record.get(k) for k in ("state", "target", "started_at", "finished_at", "exit_code",
                                       "detail", "from_version")
           if record.get(k) not in (None, "")}
    state = str(record.get("state") or "")
    target = str(record.get("target") or "")
    started = float(record.get("started_at") or 0)
    same_process = record.get("server_pid") == os.getpid()
    live = same_process and state in ("running", "handed_off")
    if not live and (clock - started > OUTCOME_TTL_S or clock < started - 60):
        # An outcome is news for a day. After that the notice goes back to what the last check
        # found; an old failure is not left standing over a copy that was since updated anyway.
        return {"state": "none"}
    if not live and target and not _newer(target, __version__):
        # This copy is at (or past) the version that install was for, however it got there.
        return dict(out, state="installed") if target == __version__ else {"state": "none"}
    if state in ("running", "handed_off") and not same_process:
        # The server that started it is gone -- on Windows that is the installer closing it. What
        # happened next is in the update journal the CLI and the supervisor write.
        if journal.get("state") == "install_failed":
            out["state"] = "failed"
            out["detail"] = str(journal.get("last_error") or "the installer reported a failure")
        elif journal.get("state") in ("startup_failed", "rollback_required"):
            out["state"] = "failed"
            out["detail"] = str(journal.get("last_error") or "the new version failed to start")
        else:
            out["state"] = "unconfirmed"
    elif state == "running" and same_process and _child["proc"] is None:
        out["state"] = "unconfirmed"      # the watcher is gone without recording an exit
    if out.get("state") == "failed":
        tail = _log_tail(str(record.get("log") or ""))
        if tail:
            out["log_tail"] = tail
    if out.get("state") in ("failed", "unconfirmed"):
        # After the handoff only the bootstrap knows what the installer did, and it says so in
        # its own transcript (update.apply_windows writes it there).
        import tempfile
        boot = os.path.join(tempfile.gettempdir(), "collie-update.log")
        tail = _log_tail(boot)
        if tail:
            out["installer_log"] = boot
            out["installer_log_tail"] = tail
    if record.get("log"):
        out["log"] = str(record.get("log"))
    return out


def status(*, now=None, path=None, local=True):
    """Everything the Settings panel shows. Reads files only; never contacts the feed."""
    from . import update
    clock = float(time.time() if now is None else now)
    value = _read(path)
    kind = update.install_kind()
    channel = str(value.get("channel") or os.environ.get("COLLIE_UPDATE_CHANNEL") or "stable")
    latest = str(value.get("latest") or "")
    checked_at = float(value.get("checked_at") or 0)
    newer = bool(latest) and _newer(latest, __version__)
    journal = update._read_update_journal()
    out = {
        "current": __version__, "kind": kind, "channel": channel, "auto": auto_enabled(),
        "local": bool(local),
        "checking": bool(_checking["active"]),
        "checked_at": checked_at or None,
        "checked_age_s": (max(0.0, clock - checked_at) if checked_at else None),
        "latest": latest or None, "newer": newer,
        "url": value.get("url") or None,
        "notes": (value.get("notes") or "") if newer else "",
        "error": value.get("error") or "",
        "error_at": value.get("error_at") or None,
        # What this install kind can actually run: the macOS app puts no `collie` command on PATH
        # (it is updated from the release page), and a Homebrew copy is upgraded by brew.
        "command": ("" if kind == "app" else "brew upgrade collie" if kind == "brew"
                    else "collie update --channel %s --yes" % channel),
        "one_press": bool(newer and local and _one_press_supported(kind)),
        "install": _install_view(value, journal, clock),
    }
    if journal:
        out["last_update"] = {k: journal.get(k) for k in ("state", "previous_version",
                                                           "target_version", "updated_at",
                                                           "healthy_at", "last_error")
                              if journal.get(k) not in (None, "")}
    return out


def start_install(expect_version, *, local=True, path=None, spawn=None, now=None,
                  python=None, watch=None):
    """Launch ``collie update --yes --expect <version>`` for the release the person was shown."""
    from . import plat, update
    clock = float(time.time() if now is None else now)
    if not local:
        raise UpdateRefused("updates can only be installed from this computer", 403)
    kind = update.install_kind()
    if not _one_press_supported(kind):
        raise UpdateRefused("this copy of Collie is updated with a command: collie update --yes", 400)
    expect = str(expect_version or "").strip().lstrip("vV")
    with _lock:
        value = _read(path)
        latest = str(value.get("latest") or "")
        if not expect or expect != latest or not _newer(latest, __version__):
            raise UpdateRefused("the release shown is no longer the latest; check again first")
        record = value.get("install") if isinstance(value.get("install"), dict) else {}
        if _child["proc"] is not None and _child["proc"].poll() is None:
            raise UpdateRefused("an update is already being installed")
        if record.get("state") == "handed_off" and record.get("server_pid") == os.getpid():
            # The installer is waiting for Collie to close; a second press would queue another.
            raise UpdateRefused("the installer is already waiting to run; Collie will restart")
        channel = str(value.get("channel") or "stable")
        log = install_log_path()
        os.makedirs(os.path.dirname(log), exist_ok=True)
        argv = [python or sys.executable, "-m", "harness.cli", "update", "--channel", channel,
                "--yes", "--expect", expect]
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        env.pop("COLLIE_PROCESS_OWNER", None)
        with open(log, "wb") as handle:
            handle.write(("[collie] installing %s over %s\n" % (expect, __version__)).encode("utf-8"))
            handle.flush()
            proc = (spawn or subprocess.Popen)(
                argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                cwd=os.path.expanduser("~"), env=env, **plat.no_window_kwargs())
        _child["proc"] = proc
        record = {"state": "running", "target": expect, "from_version": __version__,
                  "started_at": clock, "pid": getattr(proc, "pid", None),
                  "server_pid": os.getpid(), "log": log}
        _merge({"install": record}, path)

    def wait():
        try:
            code = proc.wait()
        except Exception as exc:          # pragma: no cover - Popen.wait does not raise here
            code, detail = -1, str(exc)
        else:
            detail = ""
        finished = {"exit_code": code, "finished_at": time.time()}
        if code == 0:
            # On Windows the CLI has handed the installer to its bootstrap, which closes this
            # server next. "handed_off" is the honest word: the install has not happened yet.
            finished["state"] = "handed_off"
        else:
            finished["state"] = "failed"
            finished["detail"] = detail or _last_line(_log_tail(log)) or "exit code %s" % code
        with _lock:
            current = _read(path)
            install = current.get("install") if isinstance(current.get("install"), dict) else {}
            if install.get("pid") == getattr(proc, "pid", None):
                install.update(finished)
                _merge({"install": install}, path)
            _child["proc"] = None

    (watch or (lambda fn: threading.Thread(target=fn, name="collie-update-install",
                                           daemon=True).start()))(wait)
    return status(now=clock, path=path, local=local)


def _last_line(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""
