"""What Windows PowerShell prints reaches Collie as UTF-8, whatever the console code page.

PowerShell writes to a pipe in [Console]::OutputEncoding, which starts as the OEM code page
(936 on Chinese Windows, 437 on English). Collie reads these pipes as UTF-8, so on a Chinese
Windows every window title, UI Automation name and media title with Chinese in it came back as
debris -- the desktop tools could not find "微信" by its name.
"""
import base64
import subprocess

import pytest

from harness import native, native_input, plat, screenshot

_LINE = "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)"


def test_every_script_whose_output_is_read_sets_utf8_output():
    assert plat.PS_UTF8_OUTPUT.strip() == _LINE
    import inspect
    from harness import update
    # update.running_parts reads process command lines to know what to start again
    assert "PS_UTF8_OUTPUT" in inspect.getsource(update.running_parts)
    for name, src in (("native UIA driver", native._DRIVER_PS),
                      ("MSAA driver", native_input._MSAA_PS),
                      ("capture script", screenshot._CAPTURE_PS)):
        head = src.split(_LINE)[0] if _LINE in src else None
        assert head is not None, "%s never sets UTF-8 output" % name
        # before anything that can print: only a param() block may come first
        assert "Write-" not in head and "ConvertTo-Json" not in head, name


@pytest.mark.skipif(not plat.is_windows(), reason="Windows PowerShell")
def test_a_chinese_title_survives_a_936_console(monkeypatch):
    real_run = subprocess.run

    def under_936(argv, *a, **kw):
        # Run the same script in a hidden console switched to code page 936, as on a Chinese
        # Windows; -EncodedCommand carries it through cmd.exe untouched.
        script = argv[argv.index("-Command") + 1]
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return real_run('cmd /c "chcp 936 >nul && powershell -NoProfile -EncodedCommand %s"'
                        % enc, *a, **kw)

    monkeypatch.setattr(native.subprocess, "run", under_936)
    assert native._ps('Write-Output "微信 - 窗口标题"', timeout=60) == "微信 - 窗口标题"
