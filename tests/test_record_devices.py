"""Device names come back exactly as ffmpeg printed them, whatever the Windows code page.

dshow hands ffmpeg UTF-16 names and ffmpeg prints them as UTF-8. Read in the code page (936 on
Chinese Windows), a localized microphone name came back garbled, and a name that is not the
device's own is one dshow cannot open.
"""
import locale
import subprocess
import sys

from harness import record

_FAKE = (
    "import sys\n"
    "sys.stderr.buffer.write(("
    "'[in#0 @ 0] \"HD 摄像头\" (video)\\n'"
    "'[in#0 @ 0] \"麦克风 (Realtek(R) Audio)\" (audio)\\n').encode('utf-8'))\n"
    "sys.exit(1)\n")


def test_localized_device_names_survive_a_chinese_code_page(tmp_path, monkeypatch):
    fake = tmp_path / "fake_ffmpeg.py"
    fake.write_text(_FAKE, encoding="utf-8")
    real_run = subprocess.run

    def run(argv, *a, **kw):                  # the real decode path, a stand-in ffmpeg
        return real_run([sys.executable, str(fake)], *a, **kw)

    monkeypatch.setattr(locale, "getencoding", lambda: "cp936")
    monkeypatch.setattr(record, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(record.subprocess, "run", run)
    cams, mics = record.list_dshow_devices()
    assert cams == ["HD 摄像头"]
    assert mics == ["麦克风 (Realtek(R) Audio)"]
