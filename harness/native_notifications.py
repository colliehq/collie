"""Native, privacy-aware meeting reminder delivery.

The browser Meeting Notes page is an interaction surface, not a daemon.  This module keeps a
small polling worker inside the long-lived Collie host so reminders can reach the operating system
after that page closes.  It never starts a recording and never sends calendar data off-device.

Delivery is deliberately best-effort.  A claimed reminder is written to a bounded local receipt
log whether the platform notifier accepted it or not; failures are visible through ``status()``
instead of crashing the web server.  Native reminders are opt-in in ReminderStore preferences.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time


_LOCK = threading.RLock()
_SERVICE = None
_MAX_TEXT = 500


def _text(value, limit=_MAX_TEXT):
    return str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")[:limit]


def platform_backend() -> str:
    if os.name == "nt":
        return "windows-balloon"
    if sys.platform == "darwin" and shutil.which("osascript"):
        return "macos-notification-center"
    if shutil.which("notify-send"):
        return "freedesktop-notify"
    return "unavailable"


def available() -> bool:
    return platform_backend() != "unavailable"


def _spawn(argv, *, env=None):
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(argv, env=env, **kwargs)


def notify(title: str, body: str) -> dict:
    """Ask the OS to display one notification without interpolating text into shell code."""
    title, body = _text(title, 120), _text(body, 400)
    backend = platform_backend()
    try:
        if backend == "windows-balloon":
            # Windows PowerShell is present on every supported Windows release.  User text travels
            # through environment variables, never through the command string, so quotes/backticks
            # cannot become PowerShell code.  The hidden helper lives only long enough for the
            # shell notification area to accept the balloon.
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.BalloonTipTitle=$env:COLLIE_NOTIFY_TITLE;"
                "$n.BalloonTipText=$env:COLLIE_NOTIFY_BODY;"
                "$n.Visible=$true;$n.ShowBalloonTip(5000);Start-Sleep -Seconds 6;$n.Dispose()")
            env = os.environ.copy()
            env["COLLIE_NOTIFY_TITLE"], env["COLLIE_NOTIFY_BODY"] = title, body
            exe = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
            _spawn([exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                    "-Command", script], env=env)
        elif backend == "macos-notification-center":
            script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
            _spawn(["osascript", "-e", script, title, body])
        elif backend == "freedesktop-notify":
            _spawn(["notify-send", "--app-name=Collie", title, body])
        else:
            return {"ok": False, "backend": backend, "error": "native notifications unavailable"}
        return {"ok": True, "backend": backend, "error": ""}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "backend": backend,
                "error": "%s: %s" % (type(exc).__name__, _text(exc, 240))}


class MeetingReminderService:
    def __init__(self, store=None, *, interval=20.0, receipt_path=None):
        from .meeting_reminders import ReminderStore
        self.store = store or ReminderStore()
        root = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
        self.receipt_path = os.path.abspath(
            receipt_path or os.path.join(root, "native-meeting-notifications.json"))
        os.makedirs(os.path.dirname(self.receipt_path) or ".", exist_ok=True)
        self.interval = max(5.0, min(300.0, float(interval)))
        self._stop = threading.Event()
        self._thread = None
        self.last_tick = 0.0
        self.last_error = ""

    def _receipts(self):
        try:
            with open(self.receipt_path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    def _record(self, reminder, result):
        with _LOCK:
            rows = self._receipts()
            rows.append({"id": _text(reminder.get("id"), 180),
                         "phase": _text(reminder.get("phase"), 20),
                         "at": time.time(), "ok": bool(result.get("ok")),
                         "backend": _text(result.get("backend"), 60),
                         "error": _text(result.get("error"), 240)})
            rows = rows[-200:]
            tmp = "%s.%d.tmp" % (self.receipt_path, os.getpid())
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(rows, handle, ensure_ascii=False, indent=2)
                    handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
                os.replace(tmp, self.receipt_path)
            finally:
                try:
                    os.remove(tmp)
                except FileNotFoundError:
                    pass

    def tick(self, *, now=None) -> list[dict]:
        self.last_tick = time.time()
        try:
            snapshot = self.store.snapshot(start_at=(now or self.last_tick) - 86400,
                                           end_at=(now or self.last_tick) + 86400)
            if not snapshot.get("preferences", {}).get("native_enabled", False):
                self.last_error = ""
                return []
            rows = self.store.claim_due(now=now, limit=10, channel="native")
            out = []
            for reminder in rows:
                result = notify(reminder.get("notification_title") or "Collie",
                                reminder.get("notification_body") or "Meeting reminder")
                self._record(reminder, result)
                out.append(dict(result, id=reminder.get("id"), phase=reminder.get("phase")))
            self.last_error = next((x["error"] for x in out if not x["ok"]), "")
            return out
        except Exception as exc:  # a background convenience may never take the host down
            self.last_error = "%s: %s" % (type(exc).__name__, _text(exc, 240))
            return []

    def _run(self):
        while not self._stop.wait(self.interval):
            self.tick()

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="collie-meeting-reminders",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        return True

    def status(self):
        receipts = self._receipts()
        return {"available": available(), "backend": platform_backend(),
                "running": bool(self._thread and self._thread.is_alive()),
                "last_tick": self.last_tick, "last_error": self.last_error,
                "recent": receipts[-10:]}


def ensure_started() -> MeetingReminderService:
    global _SERVICE
    with _LOCK:
        from .meeting_reminders import ReminderStore
        wanted = ReminderStore()
        if (_SERVICE is None or
                os.path.normcase(_SERVICE.store.path) != os.path.normcase(wanted.path)):
            if _SERVICE is not None:
                _SERVICE.stop()
            _SERVICE = MeetingReminderService(store=wanted)
        _SERVICE.start()
        return _SERVICE


def service() -> MeetingReminderService:
    return ensure_started()
