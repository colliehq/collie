"""Acoustic rejection of flat or speechless audio must precede multilingual recognition.

The Silero speech check is optional: without its model, Live keeps transcribing exactly as before
and readiness says the check is off. Nothing here opens a microphone or loads a real model.
"""
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

np = pytest.importorskip("numpy")

from harness import sensevoice


@pytest.mark.parametrize('floor,ceiling,expected', [
    (0.0, 0.0, ''), (-0.00001, 0.00001, ''), (0.2, 0.2, ''),
    (-0.003, 0.003, 'quiet speech'),
])
def test_flat_pcm_never_reaches_decoder(monkeypatch, tmp_path, floor, ceiling, expected):
    pcm = tmp_path / 'decoded.wav'
    pcm.write_bytes(b'fixture')

    class Samples:
        def __len__(self): return 32000
        def min(self): return floor
        def max(self): return ceiling
    monkeypatch.setattr(sensevoice, 'availability', lambda: {'available': True})
    monkeypatch.setattr(sensevoice, '_pcm_wav', lambda _path: str(pcm))
    monkeypatch.setitem(sys.modules, 'soundfile', SimpleNamespace(
        read=lambda *a, **kw: (Samples(), 16000)))
    calls = []
    stream = SimpleNamespace(accept_waveform=lambda *a: None,
                             result=SimpleNamespace(text='quiet speech'))
    recognizer = SimpleNamespace(create_stream=lambda: stream,
                                 decode_stream=lambda s: calls.append(s))
    monkeypatch.setattr(sensevoice, '_recognizer', lambda _language: recognizer)
    assert sensevoice.transcribe('fixture')['text'] == expected
    assert bool(calls) is bool(expected)
    assert not pcm.exists()


def _decoded(monkeypatch, tmp_path, samples):
    pcm = tmp_path / 'decoded.wav'
    pcm.write_bytes(b'fixture')
    monkeypatch.setattr(sensevoice, 'availability', lambda: {'available': True})
    monkeypatch.setattr(sensevoice, '_pcm_wav', lambda _: str(pcm))
    monkeypatch.setitem(sys.modules, 'soundfile', SimpleNamespace(
        read=lambda *a, **kw: (samples, 16000)))
    return pcm


def test_no_speech_evidence_skips_asr_and_keeps_reason(monkeypatch, tmp_path):
    pcm = _decoded(monkeypatch, tmp_path, np.sin(np.arange(16000) * .1).astype('float32'))
    evidence = {'accepted': False, 'reason': 'no_speech', 'speech_ms': 0}
    monkeypatch.setattr(sensevoice, 'speech_gate_model_path', lambda: Path('model.onnx'))
    monkeypatch.setattr(sensevoice, 'speech_evidence', lambda *a: evidence)
    monkeypatch.setattr(sensevoice, '_recognizer', lambda *a: pytest.fail('Noise reached ASR'))
    result = sensevoice.transcribe('fixture', speech_gate=True)
    assert result['text'] == '' and result['speech_evidence'] == evidence
    assert not pcm.exists()


def test_live_entry_point_uses_the_gate(monkeypatch):
    seen = []
    monkeypatch.setattr(sensevoice, 'transcribe',
                        lambda path, **kwargs: seen.append(kwargs) or {'text': ''})
    sensevoice.transcribe_live('clip.webm', mime_type='audio/webm')
    assert seen == [{'mime_type': 'audio/webm', 'language': '', 'speech_gate': True}]


def test_missing_vad_model_keeps_transcribing_as_before(monkeypatch, tmp_path):
    """The speech check is an improvement, never a new requirement for Live."""
    _decoded(monkeypatch, tmp_path, np.sin(np.arange(16000) * .1).astype('float32'))
    monkeypatch.setenv('COLLIE_SPEECH_VAD_MODEL', str(tmp_path / 'absent.onnx'))
    stream = SimpleNamespace(accept_waveform=lambda *a: None,
                             result=SimpleNamespace(text='<|en|>real words'))
    recognizer = SimpleNamespace(create_stream=lambda: stream, decode_stream=lambda s: None)
    monkeypatch.setattr(sensevoice, '_recognizer', lambda _language: recognizer)
    result = sensevoice.transcribe('fixture', speech_gate=True)
    assert result['text'] == 'real words'
    assert result['speech_evidence']['reason'] == 'gate_unavailable'
    assert result['speech_evidence']['accepted'] is True


def test_a_failing_vad_falls_back_to_recognition(monkeypatch, tmp_path):
    _decoded(monkeypatch, tmp_path, np.sin(np.arange(16000) * .1).astype('float32'))

    def broken(*_args):
        raise RuntimeError('onnx session failed')
    monkeypatch.setattr(sensevoice, 'speech_gate_model_path', lambda: Path('model.onnx'))
    monkeypatch.setattr(sensevoice, 'speech_evidence', broken)
    stream = SimpleNamespace(accept_waveform=lambda *a: None,
                             result=SimpleNamespace(text='still heard'))
    recognizer = SimpleNamespace(create_stream=lambda: stream, decode_stream=lambda s: None)
    monkeypatch.setattr(sensevoice, '_recognizer', lambda _language: recognizer)
    result = sensevoice.transcribe('fixture', speech_gate=True)
    assert result['text'] == 'still heard'
    assert result['speech_evidence']['reason'] == 'gate_error'


def test_vad_resets_history_after_success_and_exception(monkeypatch):
    class Detector:
        resets = 0
        frames = []
        fail = False
        def reset(self): self.resets += 1; self.frames = []
        def accept_waveform(self, frame):
            self.frames.append(frame)
            if self.fail: raise RuntimeError('inference failed')
        def flush(self): pass
        def empty(self): return True
    detector = Detector()
    monkeypatch.setattr(sensevoice, 'speech_gate_model_path', lambda: Path('model.onnx'))
    monkeypatch.setattr(sensevoice, '_speech_detector', lambda _: detector)
    samples = np.sin(np.arange(2000) * .1).astype('float32') * .01
    assert not sensevoice.speech_evidence(samples, 16000)['accepted']
    assert detector.resets == 2 and detector.frames == []
    detector.fail = True
    with pytest.raises(RuntimeError, match='inference failed'):
        sensevoice.speech_evidence(samples, 16000)
    assert detector.resets == 4 and detector.frames == []


def test_vad_counts_only_detected_speech(monkeypatch):
    class Detector:
        def __init__(self): self.queue, self.emitted = [], False
        def reset(self): self.queue, self.emitted = [], False
        def accept_waveform(self, frame):
            if not self.emitted and np.abs(frame).max() > .1:
                self.emitted = True
                self.queue.append(SimpleNamespace(start=512, samples=[0.0] * 1600))
        def flush(self): pass
        def empty(self): return not self.queue
        @property
        def front(self): return self.queue[0]
        def pop(self): self.queue.pop(0)
    monkeypatch.setattr(sensevoice, 'speech_gate_model_path', lambda: Path('model.onnx'))
    monkeypatch.setattr(sensevoice, '_speech_detector', lambda _: Detector())
    loud = np.sin(np.arange(16000) * .3).astype('float32') * .5
    evidence = sensevoice.speech_evidence(loud, 16000)
    assert evidence['accepted'] and evidence['reason'] == 'speech_detected'
    assert evidence['speech_ms'] == 100 and evidence['speech_regions'] == 1


def test_constant_offset_is_flat_audio_without_running_the_model(monkeypatch):
    monkeypatch.setattr(sensevoice, '_speech_detector',
                        lambda _: pytest.fail('flat audio reached the speech detector'))
    monkeypatch.setattr(sensevoice, 'speech_gate_model_path', lambda: Path('model.onnx'))
    evidence = sensevoice.speech_evidence(np.full(16000, .3, dtype='float32'), 16000)
    assert evidence['accepted'] is False and evidence['reason'] == 'flat_audio'


def test_explicit_vad_call_without_a_model_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv('COLLIE_SPEECH_VAD_MODEL', str(tmp_path / 'missing-vad-model.onnx'))
    with pytest.raises(sensevoice.SenseVoiceError, match='unavailable'):
        sensevoice.speech_evidence(np.sin(np.arange(16000) * .1).astype('float32'), 16000)


def test_readiness_lists_what_is_missing_and_vad_stays_optional(monkeypatch, tmp_path):
    monkeypatch.setattr(sensevoice, 'model_paths',
                        lambda: (tmp_path / 'model.onnx', tmp_path / 'tokens.txt'))
    monkeypatch.setattr(sensevoice, 'ffmpeg_path', lambda: 'ffmpeg')
    monkeypatch.setattr(sensevoice.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setenv('COLLIE_SPEECH_VAD_MODEL', str(tmp_path / 'absent.onnx'))
    state = sensevoice.availability()
    assert state['available'] is True and state['missing'] == []
    assert state['speech_gate_available'] is False
    assert state['optional_missing'] == ['Silero speech detection model']
    from harness.live_copilot import capabilities
    caps = capabilities()
    assert caps['speech_ready'] is True and caps['speech_gate_ready'] is False
    assert caps['speech_missing'] == []


def test_readiness_names_each_missing_requirement(monkeypatch, tmp_path):
    monkeypatch.setattr(sensevoice, 'model_paths', lambda: None)
    monkeypatch.setattr(sensevoice, 'ffmpeg_path', lambda: '')
    monkeypatch.setattr(sensevoice.importlib.util, 'find_spec',
                        lambda name: None if name == 'sherpa_onnx' else object())
    state = sensevoice.availability()
    assert state['available'] is False
    assert state['missing'] == ['SenseVoice model', 'ffmpeg', 'sherpa_onnx']
    from harness.live_copilot import capabilities
    monkeypatch.setattr(sensevoice, 'availability', lambda: state)
    caps = capabilities()
    assert caps['speech_ready'] is False
    assert caps['speech_missing'] == ['SenseVoice model', 'ffmpeg', 'sherpa_onnx']


def test_collie_owned_models_do_not_require_vocalcode(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.delenv('COLLIE_SENSEVOICE_MODEL_DIR', raising=False)
    monkeypatch.delenv('COLLIE_SPEECH_VAD_MODEL', raising=False)
    model = tmp_path / 'Collie/models/sensevoice'
    model.mkdir(parents=True)
    (model / 'model.int8.onnx').write_bytes(b'0' * 1_000_001)
    (model / 'tokens.txt').write_bytes(b'0' * 1_001)
    (model / 'silero_vad.onnx').write_bytes(b'0' * 100_001)
    assert sensevoice.model_paths()[0].parent == model
    assert sensevoice.speech_gate_model_path() == model / 'silero_vad.onnx'


def test_vad_is_reused_from_a_vocalcode_install(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.delenv('COLLIE_SENSEVOICE_MODEL_DIR', raising=False)
    monkeypatch.delenv('COLLIE_SPEECH_VAD_MODEL', raising=False)
    assert sensevoice.speech_gate_model_path() is None
    gate = tmp_path / 'VocalCode/models/speech-gate'
    gate.mkdir(parents=True)
    (gate / 'silero-v5.onnx').write_bytes(b'0' * 100_001)
    assert sensevoice.speech_gate_model_path() == gate / 'silero-v5.onnx'
    # A truncated download is not a model.
    (gate / 'silero-v5.onnx').write_bytes(b'0' * 10)
    assert sensevoice.speech_gate_model_path() is None


@pytest.mark.parametrize('requested,expected', [
    ('', 'auto'), ('zh', 'zh'), ('EN', 'en'), ('klingon', 'auto')])
def test_default_language_is_automatic(monkeypatch, requested, expected):
    monkeypatch.delenv('COLLIE_SENSEVOICE_LANGUAGE', raising=False)
    assert sensevoice._language(requested) == expected
