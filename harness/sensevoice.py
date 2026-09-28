"""Local SenseVoice transcription for privacy-preserving Live sessions.

The recognizer uses a user-local SenseVoice model: ``%LOCALAPPDATA%\\Collie\\models\\sensevoice``,
the directory named by ``COLLIE_SENSEVOICE_MODEL_DIR``, or a locally installed VocalCode model.
Nothing is bundled or downloaded here. Audio is converted to 16 kHz mono in the private Live
staging folder, decoded in this process, and the temporary PCM file is removed before the caller
receives a transcript. Nothing here opens a network connection.

Live audio can also pass an optional speech check first: a Silero voice-activity model (MIT) found
as ``silero_vad.onnx`` in one of those model directories, as VocalCode's own
``models\\speech-gate\\silero-v5.onnx``, or at ``COLLIE_SPEECH_VAD_MODEL``. Without it Live
transcribes exactly as before and reports the check as off.
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
_VAD_LOCK = threading.RLock()
_SPECIAL_TOKEN = re.compile(r"<\|[^|]*\|>")
_MODEL_ENV = "COLLIE_SENSEVOICE_MODEL_DIR"
_LANGUAGE_ENV = "COLLIE_SENSEVOICE_LANGUAGE"
_VAD_ENV = "COLLIE_SPEECH_VAD_MODEL"
# Peak-to-peak floor below which decoded PCM carries no speech at all. Silence sent into SenseVoice
# can come back as a hallucinated token in any language; a text blacklist cannot tell that from a
# real short reply, so reject it acoustically. Quiet but varying speech stays well above this.
_FLAT_PEAK_TO_PEAK = 0.0002


def _model_candidates() -> tuple[Path, ...]:
    candidates = []
    configured = os.environ.get(_MODEL_ENV, "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        # A Collie-owned, user-local place for the model. Nothing is bundled into the installer.
        candidates.append(Path(local_app_data) / "Collie" / "models" / "sensevoice")
        # VocalCode's model manager already verifies this same signed artifact. Reusing it avoids
        # a duplicate 229 MiB download while keeping the model on the user's machine.
        candidates.append(Path(local_app_data) / "VocalCode" / "models" / "sensevoice")
    return tuple(candidates)


def speech_gate_model_path() -> Path | None:
    """Return the optional local Silero speech-detection model, or None when there is none."""
    configured = os.environ.get(_VAD_ENV, "").strip()
    if configured:
        candidates = [Path(configured).expanduser()]
    else:
        candidates = [directory / "silero_vad.onnx" for directory in _model_candidates()]
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            # VocalCode installs the same MIT-licensed model next to its SenseVoice model.
            candidates.append(Path(local_app_data) / "VocalCode" / "models" / "speech-gate" /
                              "silero-v5.onnx")
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 100_000:
                return path
        except OSError:
            continue
    return None


@lru_cache(maxsize=1)
def _speech_detector(model: str):
    import sherpa_onnx
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = model
    config.silero_vad.threshold = .5
    # Keep real short acknowledgements. This is acoustic evidence, not a word blacklist.
    config.silero_vad.min_speech_duration = .096
    config.silero_vad.min_silence_duration = .2
    config.silero_vad.max_speech_duration = 60
    config.sample_rate = 16000
    config.num_threads = 1
    return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=120)


def speech_evidence(samples, sample_rate: int) -> dict:
    """Say whether decoded 16 kHz PCM contains speech, without trimming or keeping any audio.

    Raises SenseVoiceError when no speech-detection model is installed; transcription treats that
    as "check unavailable" rather than as a failure.
    """
    import numpy as np
    if sample_rate != 16000:
        raise SenseVoiceError("speech detection expects decoded 16 kHz PCM")
    samples = np.asarray(samples, dtype="float32")
    if not np.isfinite(samples).all():
        raise SenseVoiceError("audio contains non-finite samples")
    # Ignore DC offset; a non-zero constant is still silence.
    centered = samples - samples.mean() if len(samples) else samples
    rms = float(np.sqrt(np.mean(centered ** 2))) if len(samples) else 0.
    evidence = {"engine": "silero-vad", "audio_ms": round(len(samples) * 1000 / sample_rate),
                "ac_rms": round(rms, 7), "speech_ms": 0, "speech_regions": 0,
                "accepted": False, "reason": "no_speech"}
    if not len(samples) or float(np.ptp(centered)) < _FLAT_PEAK_TO_PEAK:
        evidence["reason"] = "flat_audio"
        return evidence
    model = speech_gate_model_path()
    if model is None:
        raise SenseVoiceError("the local speech detection model is unavailable; place "
                              "silero_vad.onnx next to the SenseVoice model or set %s" % _VAD_ENV)
    speech_samples = 0
    with _VAD_LOCK:
        detector = _speech_detector(str(model))
        # The detector is shared, so every call starts and ends with no history from another clip.
        detector.reset()
        try:
            for offset in range(0, len(centered), 512):
                frame = centered[offset:offset + 512]
                if len(frame) < 512:
                    frame = np.pad(frame, (0, 512 - len(frame)))
                detector.accept_waveform(frame)
                # Drain as we go so a long push-to-talk clip never outgrows the detector buffer.
                while not detector.empty():
                    segment = detector.front
                    speech_samples += max(0, min(len(segment.samples),
                                                 len(samples) - segment.start))
                    evidence["speech_regions"] += 1
                    detector.pop()
            detector.flush()
            while not detector.empty():
                segment = detector.front
                speech_samples += max(0, min(len(segment.samples), len(samples) - segment.start))
                evidence["speech_regions"] += 1
                detector.pop()
        finally:
            detector.reset()
    evidence["speech_ms"] = round(speech_samples * 1000 / sample_rate)
    evidence["accepted"] = speech_samples > 0
    if evidence["accepted"]:
        evidence["reason"] = "speech_detected"
    return evidence


def _gate(samples, sample_rate: int) -> dict:
    """Run the optional speech check; an absent or failing detector never blocks recognition."""
    if speech_gate_model_path() is None:
        return {"engine": "none", "accepted": True, "reason": "gate_unavailable"}
    try:
        return speech_evidence(samples, sample_rate)
    except Exception as exc:
        return {"engine": "silero-vad", "accepted": True, "reason": "gate_error",
                "error": ("%s: %s" % (type(exc).__name__, exc))[:240]}


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
    """Readiness that names each missing piece; the speech check is reported but optional."""
    paths = model_paths()
    missing = []
    if not paths:
        missing.append("SenseVoice model")
    if not ffmpeg_path():
        missing.append("ffmpeg")
    for module in ("sherpa_onnx", "soundfile", "numpy"):
        if not importlib.util.find_spec(module):
            missing.append(module)
    gate = speech_gate_model_path()
    return {
        "available": not missing,
        "missing": missing,
        "speech_gate_available": gate is not None,
        "speech_gate_model": str(gate) if gate else "",
        "optional_missing": [] if gate else ["Silero speech detection model"],
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
    value = (requested or os.environ.get(_LANGUAGE_ENV, "auto")).strip().casefold()
    # Live and capsule speech can switch languages within one utterance. Default to SenseVoice's
    # multilingual route; a caller or COLLIE_SENSEVOICE_LANGUAGE can still pin one language.
    return value if value in {"zh", "en", "ja", "ko", "yue", "auto"} else "auto"


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


def transcribe_live(path: str, *, mime_type: str = "", language: str = "") -> dict:
    """The one entry point for Live audio: continuous chunks and capsule clips."""
    return transcribe(path, mime_type=mime_type, language=language, speech_gate=True)


def transcribe(path: str, *, mime_type: str = "", language: str = "",
               speech_gate: bool = False) -> dict:
    """Return a normalized local transcript in the same shape as Collie's cloud adapter.

    Flat audio never reaches the recognizer. With ``speech_gate`` the clip must also contain
    detected speech when the optional detector is installed; the result then carries
    ``speech_evidence`` saying what the check decided.
    """
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
        if float(samples.max()) - float(samples.min()) < _FLAT_PEAK_TO_PEAK:
            result = {"text": "", "segments": []}
            if speech_gate:
                result["speech_evidence"] = {"engine": "level", "accepted": False,
                                             "reason": "flat_audio"}
            return result
        evidence = _gate(samples, sample_rate) if speech_gate else None
        if evidence is not None and not evidence.get("accepted"):
            return {"text": "", "segments": [], "speech_evidence": evidence}
        with _DECODE_LOCK:
            recognizer = _recognizer(_language(language))
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
            text = _SPECIAL_TOKEN.sub("", str(stream.result.text or "")).strip()
        result = {"text": text, "segments": []}
        if evidence is not None:
            result["speech_evidence"] = evidence
        return result
    finally:
        try:
            os.remove(temporary_wav)
        except OSError:
            pass
