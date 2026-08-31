"""Session-scoped spoken cue relay for an already-running Windows native shell.

New native builds speak cues directly.  This bounded helper lets an in-progress Live session gain
the same behavior without restarting the user's foreground application: it briefly pauses local
recognition, speaks one prioritized cue through Windows SAPI, then resumes the same session.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import time

from . import plat
from .live_copilot import LiveCopilotError, LiveSessionStore


def _speak(text: str, language: str = "zh-CN") -> None:
    if os.name != "nt":
        return
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$v=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v.Rate=2;$v.Volume=92;"
        "try{$v.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet,"
        "[System.Speech.Synthesis.VoiceAge]::NotSet,0,"
        "[System.Globalization.CultureInfo]::GetCultureInfo($args[1]))}catch{};"
        "$v.Speak($args[0]);$v.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script, "--",
         str(text)[:260], language],
        timeout=35, check=False, capture_output=True, **plat.no_window_kwargs())


def run(session_id: str, poll_seconds: float = 0.8) -> int:
    store = LiveSessionStore()
    spoken: set[str] = set()
    rank = {"now": 0, "soon": 1, "later": 2}
    kinds = {"answer": 0, "risk": 1, "action": 2, "question": 3, "note": 4}
    while True:
        value = store.snapshot()
        if not value.get("active") or value.get("session_id") != session_id:
            return 0
        rows = [row for row in value.get("suggestions") or []
                if not row.get("dismissed") and row.get("id") not in spoken
                and row.get("urgency") != "later"]
        rows.sort(key=lambda row: (rank.get(row.get("urgency"), 9),
                                   kinds.get(row.get("kind"), 9),
                                   -int(row.get("created_at_ms") or 0)))
        if not rows:
            time.sleep(max(.25, poll_seconds))
            continue
        spoken.update(str(row.get("id") or "") for row in rows)
        cue = " ".join(str(rows[0].get("text") or "").split())[:260]
        if not cue:
            continue
        was_listening = bool(value.get("listen"))
        try:
            if was_listening:
                store.update_permissions(listen=False)
                time.sleep(1.8)
            current = store.snapshot()
            if current.get("active") and current.get("session_id") == session_id:
                _speak(cue, "zh-CN")
        finally:
            current = store.snapshot()
            if (was_listening and current.get("active") and
                    current.get("session_id") == session_id):
                try:
                    store.update_permissions(listen=True, consent=True)
                except LiveCopilotError:
                    pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--poll", type=float, default=.8)
    args = parser.parse_args(argv)
    return run(args.session, args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
