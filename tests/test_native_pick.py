"""Element selection in the UI Automation driver, against a real window.

aid used to stop at its first match and ignore occurrence and name. On chrome://extensions,
aid=removeButton with occurrence=1 therefore removed the first extension in the list, not the
second. An index was also used as-is even when the name passed with it no longer matched.
"""
import ctypes
import os
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation")

# WPF, not WinForms: under Windows PowerShell a WinForms button reaches UI Automation only as an
# HWND-proxy Pane, with its handle for an AutomationId and no Invoke pattern.
_FORM = r'''
Add-Type -AssemblyName PresentationFramework
$w = New-Object System.Windows.Window
$w.Title = "%(title)s"; $w.Width = 360; $w.Height = 260
$panel = New-Object System.Windows.Controls.StackPanel
function Add-Button($aid, $text, $label) {
  $b = New-Object System.Windows.Controls.Button
  $b.Content = $text; $b.Margin = "8"
  [System.Windows.Automation.AutomationProperties]::SetAutomationId($b, $aid)
  $b.Tag = $label
  $b.add_Click({ param($s, $e) $w.Title = "clicked:" + $s.Tag })
  [void]$panel.Children.Add($b)
}
Add-Button "removeButton" "Remove" "first"
Add-Button "removeButton" "Remove" "second"
Add-Button "cancelButton" "Cancel" "cancel"
$w.Content = $panel
[void]$w.ShowDialog()
'''


def _title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


@pytest.fixture
def form(tmp_path):
    from harness import native
    title = "collie-pick-%s" % uuid.uuid4().hex[:8]
    script = tmp_path / "form.ps1"
    script.write_text(_FORM % {"title": title}, encoding="utf-8")
    proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-File", str(script)], creationflags=0x08000000)
    try:
        hwnd = 0
        deadline = time.time() + 30
        while time.time() < deadline and not hwnd:
            rows = native.windows(title).get("windows") or []
            hwnd = next((r["hwnd"] for r in rows if r.get("title") == title), 0)
            if not hwnd:
                time.sleep(0.3)
        if not hwnd:
            pytest.skip("the test window did not appear (no interactive desktop?)")
        yield native, hwnd
    finally:
        proc.kill()
        proc.wait(10)


def _clicked(hwnd, want, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _title(hwnd) == want:
            return True
        time.sleep(0.1)
    return False


def test_occurrence_counts_elements_that_share_an_automation_id(form):
    native, hwnd = form
    out = native.invoke(hwnd=hwnd, aid="removeButton", occurrence=1,
                        keyboard_fallback=False, coordinate_fallback=False)
    assert out.get("ok"), out
    assert _clicked(hwnd, "clicked:second"), _title(hwnd)

    out = native.invoke(hwnd=hwnd, aid="removeButton", occurrence=0,
                        keyboard_fallback=False, coordinate_fallback=False)
    assert out.get("ok"), out
    assert _clicked(hwnd, "clicked:first"), _title(hwnd)


def test_an_occurrence_past_the_matches_picks_nothing(form):
    native, hwnd = form
    before = _title(hwnd)
    out = native.invoke(hwnd=hwnd, aid="removeButton", occurrence=2,
                        keyboard_fallback=False, coordinate_fallback=False)
    assert not out.get("ok")
    assert "2 element(s) match" in out.get("error", ""), out
    time.sleep(0.5)
    assert _title(hwnd) == before


def test_aid_and_name_must_both_hold(form):
    native, hwnd = form
    out = native.invoke(hwnd=hwnd, aid="removeButton", name="Cancel",
                        keyboard_fallback=False, coordinate_fallback=False)
    assert not out.get("ok"), out


def test_an_index_whose_element_no_longer_matches_the_name_is_refused(form):
    native, hwnd = form
    rows = native.tree(hwnd=hwnd, max=100)["elements"]
    cancel = next(r["index"] for r in rows if r["name"] == "Cancel")
    remove = next(r["index"] for r in rows if r["name"] == "Remove")

    out = native.invoke(hwnd=hwnd, index=cancel, name="Remove", control_type="Button",
                        keyboard_fallback=False, coordinate_fallback=False)
    assert not out.get("ok"), out
    assert "is now a Button named 'Cancel'" in out.get("error", ""), out

    out = native.invoke(hwnd=hwnd, index=remove, name="Remove", control_type="Button",
                        keyboard_fallback=False, coordinate_fallback=False)
    assert out.get("ok"), out
    assert _title(hwnd).startswith("clicked:"), _title(hwnd)


def test_the_tool_schema_says_how_index_and_occurrence_select():
    from harness import native
    from harness.tools import ToolRegistry
    reg = ToolRegistry()
    native._register_windows(reg)
    props = reg.get("desktop_click").schema["properties"]
    assert "counting from 0" in props["occurrence"]["description"]
    assert "control_type" in props["index"]["description"]
