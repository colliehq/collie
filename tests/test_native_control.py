"""The generic desktop hand: window binding, coordinate conversion and bounded scripts."""
from __future__ import annotations

import os
import sys

import pytest

from harness import native
from harness import native_input
from harness import risk
from harness.gate import Gate, Mode


def test_driver_can_attach_by_hwnd_and_use_the_full_uia_action_set():
    src = native._DRIVER_PS
    assert "$AE::FromHandle([IntPtr]$Hwnd)" in src
    for pattern in ("InvokePattern", "TogglePattern", "SelectionItemPattern",
                    "SelectionPattern", "ExpandCollapsePattern", "ScrollItemPattern",
                    "ScrollPattern", "RangeValuePattern", "TextPattern", "GridPattern",
                    "GridItemPattern", "TablePattern", "TableItemPattern", "WindowPattern",
                    "TransformPattern", "DockPattern", "MultipleViewPattern",
                    "VirtualizedItemPattern"):
        assert pattern in src


def test_native_layers_include_msaa_and_allowlisted_win32_messages():
    src = native_input._MSAA_PS
    assert "AccessibleObjectFromWindow" in src
    assert "IAccessible" in src
    assert "accDoDefaultAction" in src
    assert native_input.WM_SETTEXT == 0x000C
    assert native_input.WM_GETTEXT == 0x000D
    assert native_input.BM_CLICK == 0x00F5


def test_invoke_prefers_native_semantics_before_keyboard_or_mouse(monkeypatch):
    monkeypatch.setattr(native, "_target", lambda *a, **k: ({"hwnd": 10}, ""))
    monkeypatch.setattr(native, "_run", lambda *a, **k: {
        "ok": False, "needs_coordinate": True, "needs_native": True,
        "target": {"native_hwnd": 20, "focusable": True, "type": "Button",
                   "rect": {"x": 1, "y": 2, "w": 3, "h": 4}},
        "error": "no UIA pattern",
    })
    monkeypatch.setattr(native_input, "control_action", lambda *a, **k: {
        "ok": True, "action": "click", "method": "msaa.IAccessible", "layer": "msaa"})
    monkeypatch.setattr(native_input, "press", lambda *a, **k: pytest.fail("keyboard must not run"))
    monkeypatch.setattr(native_input, "click", lambda *a, **k: pytest.fail("mouse must not run"))
    out = native.invoke(hwnd=10, index=0)
    assert out["ok"] and out["layer"] == "msaa"


def test_invoke_uses_keyboard_before_mouse(monkeypatch):
    monkeypatch.setattr(native, "_target", lambda *a, **k: ({"hwnd": 10}, ""))

    def fake_run(_action, **kwargs):
        if kwargs.get("operation") == "focus":
            return {"ok": True}
        return {"ok": False, "needs_coordinate": True,
                "target": {"native_hwnd": 0, "focusable": True, "type": "Button",
                           "rect": {"x": 1, "y": 2, "w": 3, "h": 4}},
                "error": "no UIA pattern"}

    monkeypatch.setattr(native, "_run", fake_run)
    monkeypatch.setattr(native, "focus", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(native_input, "press", lambda key: {"ok": True, "key": key})
    monkeypatch.setattr(native_input, "click", lambda *a, **k: pytest.fail("mouse must not run"))
    out = native.invoke(hwnd=10, index=0)
    assert out["ok"] and out["layer"] == "keyboard"


def test_type_prefers_wm_settext_before_keyboard(monkeypatch):
    monkeypatch.setattr(native, "_target", lambda *a, **k: ({"hwnd": 10}, ""))
    monkeypatch.setattr(native, "_run", lambda *a, **k: {
        "ok": False, "needs_native": True, "needs_keyboard": True,
        "target": {"native_hwnd": 20}, "error": "no ValuePattern"})
    monkeypatch.setattr(native_input, "control_set_text", lambda *a, **k: {
        "ok": True, "action": "set_text", "method": "win32.WM_SETTEXT", "layer": "win32"})
    monkeypatch.setattr(native, "focus", lambda *a, **k: pytest.fail("keyboard must not run"))
    out = native.set_value("hello", hwnd=10, index=0)
    assert out["ok"] and out["layer"] == "win32"


@pytest.mark.parametrize("tool,args,target", [
    ("desktop_click", {"match": "WeChat", "index": 3}, "wechat"),
    ("desktop_type", {"hwnd": 1234, "text": "secret"}, "hwnd:1234"),
    ("desktop_key", {"pid": 77, "key": "Enter"}, "pid:77"),
    ("desktop_launch", {"target": r"C:\Apps\WeChat.lnk"}, r"c:\apps\wechat.lnk"),
    ("desktop_clipboard", {"action": "set", "text": "x"}, "windows-clipboard"),
])
def test_real_desktop_schemas_produce_scoped_permission_targets(tool, args, target):
    assert risk.target_for(tool, args) == target


def test_one_app_standing_rule_allows_that_app_not_another(tmp_path):
    gate = Gate(cwd=tmp_path, mode=Mode.PROJECT)
    gate.session_rules.add(("desktop_script", "wechat"))
    allowed = gate.evaluate("desktop_script", {"match": "WeChat", "steps": []})
    denied = gate.evaluate("desktop_script", {"match": "Notepad", "steps": []})
    assert allowed.allowed and allowed.target == "wechat"
    assert not denied.allowed and denied.needs_user and denied.target == "notepad"


def test_window_relative_point_scales_a_downsampled_screenshot(monkeypatch):
    monkeypatch.setattr(native, "_target", lambda *a, **k: (
        {"hwnd": 9, "rect": {"x": -100, "y": 50, "w": 2000, "h": 1000}}, ""))
    point, error = native._screen_point({
        "match": "game", "x": 250, "y": 125,
        "image_width": 1000, "image_height": 500,
    })
    assert error == ""
    assert point == (400.0, 300.0)


def test_screen_point_does_not_need_a_window(monkeypatch):
    monkeypatch.setattr(native, "_target", lambda *a, **k: pytest.fail("must not resolve a window"))
    assert native._screen_point({"space": "screen", "x": -20, "y": 40}) == ((-20.0, 40.0), "")


def test_bound_script_cannot_escape_to_another_window_or_the_whole_screen():
    out = native.run_script([{"action": "focus", "match": "Notepad"}],
                            defaults={"match": "WeChat"})
    assert out["ok"] is False
    assert "overrides bound match" in out["error"]

    out = native.run_script([{"action": "mouse", "space": "screen", "x": 1, "y": 1}],
                            defaults={"match": "WeChat"})
    assert out["ok"] is False
    assert "escapes" in out["error"]


def test_script_stops_at_first_failed_postcondition(monkeypatch):
    calls = []

    def fake_wait(args):
        calls.append(args.get("kind"))
        return {"ok": False, "error": "not there"}

    monkeypatch.setattr(native, "wait_for", fake_wait)
    out = native.run_script([
        {"action": "wait_for", "kind": "window"},
        {"action": "wait", "ms": 1},
    ])
    assert out["ok"] is False
    assert out["stopped_at"] == 1
    assert out["ran"] == 1
    assert calls == ["window"]


def test_named_keys_are_resolved_without_touching_the_os():
    assert native_input.key_code("ArrowDown") == 0x28
    assert native_input.key_code("Ctrl") == 0x11
    assert native_input.key_code("F12") == 0x7B
    assert native_input.key_code("a") == ord("A")
    with pytest.raises(ValueError):
        native_input.key_code("not-a-real-key")


@pytest.mark.skipif(sys.platform != "win32", reason="live read-only Win32 discovery")
def test_win32_discovery_returns_stable_handles_and_bounds():
    rows = native_input.windows()
    assert rows
    assert all(isinstance(row["hwnd"], int) and row["hwnd"] > 0 for row in rows)
    assert all(set(("x", "y", "w", "h")) <= set(row["rect"]) for row in rows)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only registry surface")
def test_windows_registers_the_complete_desktop_surface(monkeypatch):
    from harness.tools import ToolRegistry

    monkeypatch.setenv("COLLIE_DESKTOP_CONTROL", "1")
    registry = ToolRegistry()
    native.register_native(registry)
    expected = {
        "desktop_apps", "desktop_inspect", "desktop_click", "desktop_type",
        "desktop_read", "desktop_range", "desktop_mouse", "desktop_drag",
        "desktop_uia", "desktop_win32", "desktop_key", "desktop_window",
        "desktop_clipboard", "desktop_wait",
        "desktop_script", "desktop_launch", "desktop_focus",
    }
    assert expected <= set(registry.names())
