"""Local SenseVoice transcription for privacy-preserving Live sessions.

The recognizer reuses a locally installed VocalCode SenseVoice model when it is available. Audio is
converted to 16 kHz mono in the private Live staging folder, decoded in this process, and the
temporary PCM file is removed before the caller receives a transcript. Nothing here opens a
network connection.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

from . import plat


class SenseVoiceError(RuntimeError):
    pass


_DECODE_LOCK = threading.RLock()
_SPECIAL_TOKEN = re.compile(r"<\|[^|]*\|>")
_MODEL_ENV = "COLLIE_SENSEVOICE_MODEL_DIR"
_LANGUAGE_ENV = "COLLIE_SENSEVOICE_LANGUAGE"


def _model_candidates() -> tuple[Path, ...]:
    candidates = []
    configured = os.environ.get(_MODEL_ENV, "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        # VocalCode's model manager already verifies this same signed artifact. Reusing it avoids
        # a duplicate 229 MiB download while keeping the model on the user's machine.
        candidates.append(Path(local_app_data) / "VocalCode" / "models" / "sensevoice")
    return tuple(candidates)


def model_paths() -> tuple[Path, Path] | None:
    for directory in _model_candidates():
        model = directory / "model.int8.onnx"
        tokens = directory / "tokens.txt"
        if model.is_file() and tokens.is_file() and model.stat().st_size > 1_000_000 and \
                tokens.stat().st_size > 1_000:
            return model, tokens
    return None


def ffmpeg_path() -> str:
    return os.environ.get("COLLIE_FFMPEG", "").strip() or shutil.which("ffmpeg") or ""


def availability() -> dict:
    paths = model_paths()
    return {
        "available": bool(paths and ffmpeg_path() and
                          importlib.util.find_spec("sherpa_onnx") and
                          importlib.util.find_spec("soundfile")),
        "model_dir": str(paths[0].parent) if paths else "",
        "engine": "SenseVoice · local" if paths else "SenseVoice · unavailable",
    }


@lru_cache(maxsize=2)
def _recognizer(language: str):
    paths = model_paths()
    if not paths:
        raise SenseVoiceError(
            "SenseVoice model is unavailable; set %s to its local model directory" % _MODEL_ENV)
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise SenseVoiceError("the local sherpa-onnx runtime is not installed") from exc
    model, tokens = paths
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model), tokens=str(tokens), num_threads=2, use_itn=True,
        language=language, debug=False)


def _language(requested: str) -> str:
    value = (requested or os.environ.get(_LANGUAGE_ENV, "zh")).strip().casefold()
    # SenseVoice's Chinese route also recognizes mixed Mandarin/English well; use auto only when
    # explicitly requested so a Chinese Live session does not lose its intended normalization.
    return value if value in {"zh", "en", "ja", "ko", "yue", "auto"} else "zh"


def _pcm_wav(path: str) -> str:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise SenseVoiceError("ffmpeg is required to decode browser audio chunks")
    directory = os.path.dirname(os.path.abspath(path))
    descriptor, output = tempfile.mkstemp(prefix="sensevoice-", suffix=".wav", dir=directory)
    os.close(descriptor)
    try:
        # -nostdin prevents a background worker from ever consuming desktop-process input.
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
             "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", output],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=20, check=False, **plat.no_window_kwargs())
    except OSError as exc:
        try:
            os.remove(output)
        except OSError:
            pass
        raise SenseVoiceError("could not start ffmpeg: %s" % exc) from exc
    except subprocess.TimeoutExpired as exc:
        try:
            os.remove(output)
        except OSError:
            pass
        raise SenseVoiceError("ffmpeg timed out decoding the live audio chunk") from exc
    if result.returncode:
        try:
            os.remove(output)
        except OSError:
            pass
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SenseVoiceError("ffmpeg could not decode the live audio%s" %
                              (": " + detail[-1][:240] if detail else ""))
    return output


def transcribe(path: str, *, mime_type: str = "", language: str = "") -> dict:
    """Return a normalized local transcript in the same shape as Collie's cloud adapter."""
    if not availability().get("available"):
        raise SenseVoiceError("local SenseVoice is not ready")
    temporary_wav = _pcm_wav(path)
    try:
        try:
            import soundfile as sf
        except ImportError as exc:
            raise SenseVoiceError("the local soundfile runtime is not installed") from exc
        samples, sample_rate = sf.read(temporary_wav, dtype="float32", always_2d=False)
        if getattr(samples, "ndim", 1) > 1:
            samples = samples.mean(axis=1)
        if len(samples) == 0:
            return {"text": "", "segments": []}
        with _DECODE_LOCK:
            recognizer = _recognizer(_language(language))
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
            text = _SPECIAL_TOKEN.sub("", str(stream.result.text or "")).strip()
        return {"text": text, "segments": []}
    finally:
        try:
            os.remove(temporary_wav)
        except OSError:
            pass
