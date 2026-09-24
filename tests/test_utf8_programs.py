"""Programs that print UTF-8 are read as UTF-8, whatever the Windows code page.

Read in the code page (936 on Chinese Windows, 1252 on Western), their non-ASCII output came
back garbled: ffmpeg's dshow device names (dshow hands ffmpeg UTF-16 names, ffmpeg prints them
as UTF-8, and a name that is not the device's own is one dshow cannot open), and headless
Chrome's --dump-dom of a web search.
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


def test_web_search_results_keep_their_text_under_a_western_code_page(tmp_path, monkeypatch):
    # Headless Chrome's --dump-dom prints UTF-8; read in 1252 every non-ASCII result was garbled.
    from harness import websearch
    fake = tmp_path / "fake_chrome.py"
    fake.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(('<li class=\"b_algo\"><h2><a href=\"https://x.test/\">"
        "Café 中文 — résumé</a></h2><p>naïve 解析器</p></li>').encode('utf-8'))\n",
        encoding="utf-8")
    real_run = subprocess.run

    def run(argv, *a, **kw):
        return real_run([sys.executable, str(fake)], *a, **kw)

    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("COLLIE_CHROME_PROFILE", str(tmp_path / "profile"))
    got = websearch._chrome_search("q", 3, "chrome")
    assert got and got[0]["title"] == "Café 中文 — résumé" and got[0]["snippet"] == "naïve 解析器"
