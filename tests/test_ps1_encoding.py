"""PowerShell scripts Collie writes to disk must read back intact in Windows PowerShell 5.1.

5.1 decodes a .ps1 without a byte-order mark in the ANSI code page. The update script was written
as UTF-8 without one, and its em dash (E2 80 94) ends in 0x94, a closing curly quote in code page
1252, which PowerShell treats as the end of a string. The script failed to parse, so on Western
Windows no installer update since 0.20.31 ever started Setup. A development machine running with
the UTF-8 code page (65001) cannot show this, so the read is simulated here: the bytes written
are decoded the way 5.1 decodes them, and PowerShell's own parser judges the result.
"""
import ast
import os
import subprocess
import sys

import pytest

from harness import native, plat, screenshot, update

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOM = b"\xef\xbb\xbf"


def _as_windows_powershell_reads(data, codepage):
    """The text Windows PowerShell 5.1 sees in a .ps1 holding these bytes."""
    if data.startswith(BOM):
        return data[len(BOM):].decode("utf-8")
    return data.decode(codepage, errors="replace")


def _parse_errors(text, tmp_path):
    src = tmp_path / "seen.ps1"
    src.write_bytes(BOM + text.encode("utf-8"))      # hand PowerShell exactly `text`
    ps = ("$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
          "'%s', [ref]$t, [ref]$e); $e | ForEach-Object { $_.Message }" % src)
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120)
    return [l for l in out.stdout.splitlines() if l.strip()]


def _bootstrap():
    return update._BOOTSTRAP.format(
        pid=1, exe=r"C:\Users\José\AppData\Local\Temp\collie-update-x\Collie-Setup.exe",
        root=r"C:\Users\José\AppData\Local\Programs\Collie", log=r"C:\t\collie-update.log",
        restarts="\n".join(update._restart_script(p, r"C:\Users\José\Collie")
                           for p in ("supervisor", "wallpaper", "slack:slack-rowan.pyw",
                                     "bridge", "window")))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell's parser")
def test_the_old_way_of_writing_it_does_not_parse_on_western_windows(tmp_path):
    old = _bootstrap().replace(" - not restarting", " \u2014 not restarting").encode("utf-8")
    assert _parse_errors(_as_windows_powershell_reads(old, "cp1252"), tmp_path), \
        "the simulation must reproduce the failure, or it proves nothing"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell's parser")
@pytest.mark.parametrize("codepage", ["cp1252", "cp1250", "cp936"])
def test_the_update_script_parses_and_keeps_its_paths_in_every_code_page(tmp_path, codepage):
    path = plat.write_ps1(str(tmp_path / "collie-update.ps1"), _bootstrap())
    seen = _as_windows_powershell_reads(open(path, "rb").read(), codepage)
    assert _parse_errors(seen, tmp_path) == []
    assert r"C:\Users\José\AppData\Local\Programs\Collie\python\pythonw.exe" in seen


def test_write_ps1_marks_utf8_and_leaves_an_identical_file_alone(tmp_path):
    p = str(tmp_path / "x.ps1")
    plat.write_ps1(p, "'é'\n")
    assert open(p, "rb").read() == BOM + "'é'\n".encode("utf-8")
    os.utime(p, (1, 1))
    plat.write_ps1(p, "'é'\n")
    assert os.stat(p).st_mtime == 1, "an unchanged script must not be rewritten"
    plat.write_ps1(p, "'e'\n")
    assert open(p, "rb").read() == BOM + b"'e'\n"


def test_the_drivers_are_written_with_the_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    assert open(screenshot._script_path(), "rb").read().startswith(BOM)
    assert open(native._ensure_driver(), "rb").read().startswith(BOM)


def test_no_module_writes_a_ps1_any_other_way():
    """Every .ps1 goes through plat.write_ps1: a text-mode write has no mark."""
    offenders = []
    for name in sorted(os.listdir(os.path.join(ROOT, "harness"))):
        if not name.endswith(".py") or name == "plat.py":
            continue
        src = open(os.path.join(ROOT, "harness", name), encoding="utf-8").read()
        if ".ps1" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open"
                    and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    and "w" in str(node.args[1].value)):
                seg = ast.get_source_segment(src, node) or ""
                window = src[max(0, src.find(seg) - 400):src.find(seg) + len(seg)]
                if ".ps1" in window or "_DRIVER" in seg:
                    offenders.append("%s: %s" % (name, seg))
    assert offenders == []
