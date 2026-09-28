"""`collie record` never destroys a recording: not an existing file, not a partial capture, not a
good export replaced by a failed one, and a stop that did not stop is reported.

ffmpeg, the process table and the clock are faked; nothing records the screen.
"""
import os
from types import SimpleNamespace

import pytest

from harness import record


@pytest.fixture
def private_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(record, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(record, "STATE", str(state_dir / "record.json"))
    monkeypatch.setattr(record.time, "sleep", lambda _seconds: None)
    return state_dir


def _startable(monkeypatch):
    monkeypatch.setattr(record, "_require_capture_os", lambda: None)
    monkeypatch.setattr(record, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(record, "list_capture_devices", lambda: ([], []))
    monkeypatch.setattr(record, "resolve_region", lambda **_kwargs: None)
    monkeypatch.setattr(record, "resolve_screen", lambda **_kwargs: 0)
    monkeypatch.setattr(record, "_kill", lambda pid: None)


def test_capture_command_never_overwrites_an_output():
    args = record._build_cmd("ffmpeg", "C:/out/clip.ts", 30, None, None, None, None, None)
    assert "-n" in args and "-y" not in args


def test_start_refuses_an_output_that_already_exists(private_state, tmp_path, monkeypatch):
    _startable(monkeypatch)
    out = tmp_path / "existing.ts"
    out.write_bytes(b"an earlier recording")
    monkeypatch.setattr(record.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("ffmpeg started over an existing file"))
    with pytest.raises(FileExistsError, match="already exists"):
        record.start(out=str(out), no_cam=True, no_mic=True)
    assert out.read_bytes() == b"an earlier recording"


def test_a_capture_that_never_got_going_keeps_what_it_wrote(private_state, tmp_path, monkeypatch):
    _startable(monkeypatch)
    out = tmp_path / "partial.ts"

    class Died:
        pid = 4321

        def __init__(self, *args, **kwargs):
            out.write_bytes(b"x" * 100)       # a few packets, then the device failed

        def poll(self):
            return 1
    monkeypatch.setattr(record.subprocess, "Popen", Died)
    message = record.start(out=str(out), no_cam=True, no_mic=True)
    assert "didn't start" in message
    assert out.read_bytes() == b"x" * 100
    assert not os.path.exists(record.STATE)


def _post(tmp_path, monkeypatch, returncode, written, earlier=True):
    src = tmp_path / "clip.ts"
    src.write_bytes(b"t" * 20_000)
    dst = tmp_path / "clip.mp4"
    if earlier:
        dst.write_bytes(b"the export that already worked")
    monkeypatch.setattr(record, "_ffmpeg", lambda: "ffmpeg")
    seen = []

    def run(args, **_kwargs):
        target = args[-1]
        seen.append(target)
        with open(target, "wb") as handle:
            handle.write(written)
        return SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"")
    monkeypatch.setattr(record.subprocess, "run", run)
    result = record._postprocess(str(src), False, False, False, 240, 40, "bl", True)
    return result, src, dst, seen


def test_a_failed_export_leaves_the_previous_one_and_no_partial_file(tmp_path, monkeypatch):
    result, _src, dst, seen = _post(tmp_path, monkeypatch, 1, b"p" * 50_000)
    assert result is None
    assert dst.read_bytes() == b"the export that already worked"
    assert seen and seen[0] != str(dst)                      # ffmpeg wrote somewhere else
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4", "clip.ts"]


def test_a_successful_export_appears_only_at_the_end(tmp_path, monkeypatch):
    result, _src, dst, _seen = _post(tmp_path, monkeypatch, 0, b"n" * 50_000, earlier=False)
    assert result == str(dst)
    assert dst.read_bytes() == b"n" * 50_000
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4", "clip.ts"]


def test_a_successful_export_never_replaces_an_earlier_one(tmp_path, monkeypatch):
    """Recording twice with --out clip.ts: the first take's clip.mp4 survives the second."""
    result, _src, dst, _seen = _post(tmp_path, monkeypatch, 0, b"n" * 50_000)
    assert dst.read_bytes() == b"the export that already worked"
    assert result == str(tmp_path / "clip-1.mp4")
    assert (tmp_path / "clip-1.mp4").read_bytes() == b"n" * 50_000
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip-1.mp4", "clip.mp4", "clip.ts"]


def test_start_refuses_when_the_export_would_land_on_an_existing_file(private_state, tmp_path,
                                                                      monkeypatch):
    _startable(monkeypatch)
    (tmp_path / "talk.mp4").write_bytes(b"the first take")
    monkeypatch.setattr(record.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("ffmpeg started although talk.mp4 exists"))
    with pytest.raises(FileExistsError, match="talk.mp4"):
        record.start(out=str(tmp_path / "talk.ts"), no_cam=True, no_mic=True)
    assert (tmp_path / "talk.mp4").read_bytes() == b"the first take"


def test_a_tiny_export_is_not_success(tmp_path, monkeypatch):
    result, _src, dst, _seen = _post(tmp_path, monkeypatch, 0, b"tiny")
    assert result is None and dst.read_bytes() == b"the export that already worked"


def test_a_recorder_that_will_not_die_is_reported_and_kept(private_state, tmp_path, monkeypatch):
    out = tmp_path / "running.ts"
    out.write_bytes(b"r" * 40_000)
    record._save({"pid": 777, "out": str(out), "started": 0})
    monkeypatch.setattr(record, "_alive", lambda pid: True)
    killed = []
    monkeypatch.setattr(record, "_kill", killed.append)
    monkeypatch.setattr(record, "_postprocess",
                        lambda *a, **k: pytest.fail("exported a file that is still being written"))
    message = record.stop()
    assert killed == [777]
    assert "could not stop" in message and "777" in message
    assert os.path.exists(record.STATE)                     # `collie record stop` can retry
    assert out.read_bytes() == b"r" * 40_000
