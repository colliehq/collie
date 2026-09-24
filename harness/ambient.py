"""Opt-in, content-free observation of activity outside AI tool calls.

The sensor deliberately does less than typical activity trackers: no window titles,
keystrokes, screenshots, clipboard, browser history, document paths, or command lines.
On Windows it observes only the foreground executable name, idle boundaries, and a
small allowlist of power/session event IDs whose XML payload is never retained.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from . import plat
from .procedure_memory import AMBIENT_PROJECT, ProcedureMemory


_NOISE_APPS = frozenset({
    "", "collie", "python", "pythonw", "lockapp", "logonui", "credentialui",
    "consent", "searchhost", "shellexperiencehost", "startmenuexperiencehost",
})
_APP_CHARS = re.compile(r"[^a-z0-9._-]+")
_SYSTEM_ACTIONS = {
    ("microsoft-windows-kernel-power", 42): "sleep",
    ("microsoft-windows-power-troubleshooter", 1): "resume",
    ("eventlog", 6005): "system_start",
    ("eventlog", 6006): "system_stop",
}


def state_path(root=None):
    root = os.path.abspath(root or os.environ.get("COLLIE_STATE_DIR")
                           or os.path.expanduser("~/.collie"))
    return os.path.join(root, "procedural-memory.db")


def supported():
    return os.name == "nt"


def _app_name(path):
    name = os.path.splitext(os.path.basename(str(path or "")))[0].lower()
    return _APP_CHARS.sub("-", name).strip("-")[:80]


class WindowsActivitySource:
    """Read application identity and idle time without asking for window content."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def foreground_app(self):
        if os.name != "nt":
            return ""
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (
            ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel32.QueryFullProcessImageNameW.argtypes = (
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(size)):
                return ""
            return _app_name(buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    def idle_seconds(self):
        if os.name != "nt":
            return 0.0

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return elapsed_ms / 1000.0

    def system_signals(self, *, since_seconds=300):
        """Return only provider/id/time from an allowlist; discard EventData and messages."""
        if os.name != "nt":
            return []
        query = ("*[System[(EventID=1 or EventID=42 or EventID=6005 or "
                 "EventID=6006)]]")
        try:
            result = subprocess.run(
                ["wevtutil.exe", "qe", "System", "/q:" + query,
                 "/rd:true", "/c:16", "/f:xml"],
                capture_output=True, text=True, errors="replace", timeout=8,
                **plat.no_window_kwargs())
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        text = re.sub(r"<\?xml[^>]*\?>", "", result.stdout)
        try:
            root = ET.fromstring("<Events>" + text + "</Events>")
        except ET.ParseError:
            return []
        cutoff = time.time() - max(60, int(since_seconds))
        out = []
        for event in root:
            system = next((node for node in event if node.tag.endswith("System")), None)
            if system is None:
                continue
            provider = next((node for node in system if node.tag.endswith("Provider")), None)
            event_id = next((node for node in system if node.tag.endswith("EventID")), None)
            created = next((node for node in system if node.tag.endswith("TimeCreated")), None)
            try:
                provider_name = str(provider.attrib.get("Name") or "").lower()
                numeric_id = int(event_id.text)
                stamp = str(created.attrib.get("SystemTime") or "").replace("Z", "+00:00")
                observed_at = dt.datetime.fromisoformat(stamp).timestamp()
            except (AttributeError, TypeError, ValueError):
                continue
            action = _SYSTEM_ACTIONS.get((provider_name, numeric_id))
            if action and observed_at >= cutoff:
                out.append({"action": action, "observed_at": observed_at})
        return out


def _duration_bucket(seconds):
    seconds = max(0, int(seconds))
    quantum = 15 if seconds < 300 else 60
    return min(8 * 3600, int(round(seconds / quantum) * quantum))


class AmbientObserver:
    def __init__(self, path=None, *, source=None):
        self.store = ProcedureMemory(path or state_path())
        self.source = source or WindowsActivitySource()
        self.current_app = ""
        self.current_since = 0.0
        self.last_active_at = 0.0
        self.session = ""
        self.last_system_poll = 0.0

    def _new_session(self):
        self.session = "ambient_" + uuid.uuid4().hex

    def _drop_current(self):
        self.current_app = ""
        self.current_since = 0.0

    def _record_current(self, ended_at, sample_seconds):
        app, started = self.current_app, self.current_since
        self._drop_current()
        duration = _duration_bucket(float(ended_at) - float(started or ended_at))
        if not app or duration < max(2, int(sample_seconds)):
            return None
        return self.store.observe(
            session=self.session, project=AMBIENT_PROJECT, app=app, action="focus",
            object_kind="tool", object_ref="", result="observed",
            metadata={"duration_s": duration, "ambient": True},
            source="ambient-app", observed_at=started)

    def _poll_system(self, now):
        if now - self.last_system_poll < 60:
            return 0
        self.last_system_poll = now
        changed = 0
        for signal in self.source.system_signals(since_seconds=300):
            exists = self.store.db.execute(
                """SELECT 1 FROM procedure_events WHERE source='ambient-system'
                   AND action=? AND observed_at=? LIMIT 1""",
                (signal["action"], signal["observed_at"])).fetchone()
            if exists:
                continue
            if not self.session:
                self._new_session()
            changed += int(bool(self.store.observe(
                session=self.session, project=AMBIENT_PROJECT, app="system",
                action=signal["action"], object_kind="tool", result="observed",
                metadata={"ambient": True}, source="ambient-system",
                observed_at=signal["observed_at"])))
        return changed

    def tick(self, now=None):
        now = float(time.time() if now is None else now)
        privacy = self.store.settings(refresh=True)
        mode = str(privacy.get("observation_mode") or "off")
        if mode == "off" or not privacy.get("ambient_enabled") or privacy.get("paused"):
            self._drop_current()
            return {"enabled": mode != "off" and bool(privacy.get("ambient_enabled")),
                    "observed": 0, "app": "", "idle": False}
        sample_seconds = int(privacy.get("ambient_sample_seconds") or 5)
        idle_limit = max(60, int(privacy.get("ambient_idle_seconds") or 300))
        idle = float(self.source.idle_seconds())
        observed = self._poll_system(now)
        if idle >= idle_limit:
            if self.current_app:
                observed += int(bool(self._record_current(now - idle, sample_seconds)))
            self.session = ""
            return {"enabled": True, "observed": observed, "app": "", "idle": True}
        app = _app_name(self.source.foreground_app())
        if app in _NOISE_APPS:
            if self.current_app:
                observed += int(bool(self._record_current(now, sample_seconds)))
            return {"enabled": True, "observed": observed, "app": "", "idle": False}
        if not self.session or (self.last_active_at and now - self.last_active_at >= idle_limit):
            self._new_session()
        self.last_active_at = now
        if app != self.current_app:
            if self.current_app:
                observed += int(bool(self._record_current(now, sample_seconds)))
            self.current_app, self.current_since = app, now
        return {"enabled": True, "observed": observed, "app": app, "idle": False}

    def close(self):
        try:
            privacy = self.store.settings(refresh=True)
            if privacy.get("ambient_enabled") and self.current_app:
                self._record_current(time.time(), privacy.get("ambient_sample_seconds") or 5)
        finally:
            self.store.close()

    def run(self, *, interval=None, once=False):
        try:
            while True:
                value = self.tick()
                if once:
                    return value
                settings = self.store.settings(refresh=True)
                delay = int(interval or settings.get("ambient_sample_seconds") or 5)
                time.sleep(max(2, min(delay, 300)))
        finally:
            self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m harness.ambient")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--interval", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    path = state_path(args.state_dir)
    if args.once:
        observer = AmbientObserver(path)
        print(json.dumps(observer.run(once=True), ensure_ascii=False))
        return 0
    from .supervisor import AlreadyRunning, InstanceLock
    root = os.path.dirname(path)
    try:
        lock = InstanceLock(os.path.join(root, "ambient.lock"), what="The Collie ambient observer")
    except AlreadyRunning as exc:
        print("collie ambient: %s; this copy is exiting." % exc, file=sys.stderr)
        return 3
    try:
        AmbientObserver(path).run(interval=args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
