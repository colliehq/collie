"""Session-scoped spoken cue relay for an already-running Windows native shell.

New native builds speak cues directly. This bounded helper lets an in-progress Live session gain
the same behavior without restarting the user's foreground application: it briefly pauses local
recognition, speaks one prioritized cue through a prewarmed local neural voice, then resumes the
same session. Windows SAPI is retained only as an availability fallback.
"""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import tempfile
import time

from . import plat
from .live_copilot import LiveCopilotError, LiveSessionStore


def _play_wave(path: str, gain: float = 1.7) -> bool:
    player = shutil.which("ffplay")
    if not player:
        return False
    played = subprocess.run(
        [player, "-nodisp", "-autoexit", "-loglevel", "error",
         "-af", "volume=%.2f" % gain, path],
        timeout=35, check=False, capture_output=True, **plat.no_window_kwargs())
    return played.returncode == 0


class LocalNeuralVoice:
    def __init__(self, voice: str | None = None):
        self.voice = voice or os.environ.get("COLLIE_LIVE_VOICE", "zf_xiaoxiao")
        self.pipeline = None
        self.failed = False

    def prewarm(self) -> bool:
        if self.pipeline is not None:
            return True
        if self.failed:
            return False
        try:
            from kokoro import KPipeline
            self.pipeline = KPipeline(lang_code="z", repo_id="hexgrad/Kokoro-82M")
            # The first G2P/model call has one-time setup work. Pay it before the first real cue.
            list(self.pipeline("好。", voice=self.voice, speed=1.14))
            return True
        except Exception:
            self.pipeline = None
            self.failed = True
            return False

    def speak(self, text: str) -> bool:
        if not self.prewarm():
            return _speak_sapi(text, "zh-CN")
        fd, wave_path = tempfile.mkstemp(prefix="collie-live-neural-", suffix=".wav")
        os.close(fd)
        try:
            import numpy as np
            import soundfile as sf
            parts = [audio for _graphemes, _phonemes, audio in self.pipeline(
                str(text)[:260], voice=self.voice, speed=1.14)]
            if not parts:
                return False
            sf.write(wave_path, np.concatenate(parts), 24_000)
            return _play_wave(wave_path, 1.7)
        except Exception:
            return _speak_sapi(text, "zh-CN")
        finally:
            try:
                os.remove(wave_path)
            except OSError:
                pass


def _speak_sapi(text: str, language: str = "zh-CN") -> bool:
    if os.name != "nt":
        return False
    payload = base64.b64encode(str(text)[:260].encode("utf-8")).decode("ascii")
    culture = "zh-CN" if str(language).casefold().startswith("zh") else "en-US"
    fd, wave_path = tempfile.mkstemp(prefix="collie-live-voice-", suffix=".wav")
    os.close(fd)
    path_payload = base64.b64encode(wave_path.encode("utf-8")).decode("ascii")
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$t=[System.Text.Encoding]::UTF8.GetString("
        "[System.Convert]::FromBase64String('%s'));"
        "$p=[System.Text.Encoding]::UTF8.GetString("
        "[System.Convert]::FromBase64String('%s'));"
        "$v=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v.Rate=2;$v.Volume=100;"
        "try{$v.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet,"
        "[System.Speech.Synthesis.VoiceAge]::NotSet,0,"
        "[System.Globalization.CultureInfo]::GetCultureInfo('%s'))}catch{};"
        "$v.SetOutputToWaveFile($p);$v.Speak($t);"
        "$v.SetOutputToDefaultAudioDevice();$v.Dispose()"
    ) % (payload, path_payload, culture)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            timeout=35, check=False, capture_output=True, **plat.no_window_kwargs())
        if result.returncode != 0:
            return False
        return _play_wave(wave_path, 2.8)
    finally:
        try:
            os.remove(wave_path)
        except OSError:
            pass


def _speak(text: str, language: str = "zh-CN") -> bool:
    return LocalNeuralVoice().speak(text)


def run(session_id: str, poll_seconds: float = 0.8) -> int:
    store = LiveSessionStore()
    voice = LocalNeuralVoice()
    voice.prewarm()
    initial = store.snapshot()
    existing = (initial.get("suggestions") or []) if initial.get("session_id") == session_id else []
    # Attaching the relay to an in-progress session must not replay stale advice.
    spoken: set[str] = {str(row.get("id") or "") for row in existing}
    spoken_text: set[str] = {
        " ".join(str(row.get("text") or "").split()).casefold()
        for row in existing if row.get("text")
    }
    last_spoken_at = 0.0
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
        fresh = [row for row in rows
                 if " ".join(str(row.get("text") or "").split()).casefold()
                 not in spoken_text]
        if not fresh:
            continue
        latest = (value.get("events") or [{}])[-1]
        direct_answer = (fresh[0].get("lane") == "dialogue" or
                         (fresh[0].get("kind") == "answer" and
                          latest.get("source") == "you" and latest.get("kind") == "speech"))
        now = time.monotonic()
        if not direct_answer and now - last_spoken_at < 18.0:
            continue
        cue = " ".join(str(fresh[0].get("text") or "").split())[:260]
        if not cue:
            continue
        spoken_text.add(cue.casefold())
        pause_token = ""
        try:
            pause_token = store.pause_listening_for_voice(session_id=session_id)
            if pause_token:
                time.sleep(1.8)
            current = store.snapshot()
            if current.get("active") and current.get("session_id") == session_id:
                if voice.speak(cue):
                    last_spoken_at = time.monotonic()
        finally:
            if pause_token:
                try:
                    store.resume_listening_after_voice(session_id=session_id, token=pause_token)
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
