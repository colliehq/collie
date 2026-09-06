"""One running conversation's consent must not arm another conversation."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness import capability_policy, native, screenshot, settings
from harness.mcpclient import _mcp_manage_on, _mcp_discovery_on
from harness.tools import ToolCtx, EnableCapabilityTool


@pytest.fixture
def panel(monkeypatch):
    values = {key:"off" for key in capability_policy.KEYS}
    monkeypatch.setattr(settings, "get", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(settings, "update", lambda updates: values.update(updates))
    monkeypatch.setattr(settings, "apply", lambda: None)
    return values


@pytest.mark.parametrize("capability,key,probe", [
    ("screen_capture", "SCREEN_CAPTURE", screenshot._enabled),
    ("desktop_control", "DESKTOP_CONTROL", native._dc_enabled),
    ("mcp_manage", "MCP_MANAGE", _mcp_manage_on),
    ("mcp_discovery", "MCP_DISCOVERY", _mcp_discovery_on),
])
def test_grant_is_scoped_across_threads_and_global_revocation_still_works(panel, capability, key, probe):
    first, other = ToolCtx(".", "first", None), ToolCtx(".", "other", None)
    assert not probe(first) and not probe(other)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(EnableCapabilityTool().run, {"capability":capability}, first).result()
        assert "enabled" in result and not result.startswith("ERROR")
        assert pool.submit(probe, first).result() is True
        assert pool.submit(probe, other).result() is False
    assert probe(ToolCtx(".", "future", None)) is True
    panel[key] = "off"
    assert probe(first) is False


def test_other_running_context_cannot_capture_pixels_after_a_sibling_grant(panel, monkeypatch):
    first, other = ToolCtx(".", "first", None), ToolCtx(".", "other", None)
    calls = []
    monkeypatch.setattr(screenshot, "capture", lambda **kw: calls.append(kw) or
                        {"ok":False, "error":"synthetic capture did not return pixels"})
    EnableCapabilityTool().run({"capability":"screen_capture"}, first)
    assert "OFF" in screenshot.ScreenshotTool().run({}, other)
    assert calls == []
    assert "synthetic capture" in screenshot.ScreenshotTool().run({}, first)
    assert len(calls) == 1


def test_failed_persistence_does_not_claim_or_broadcast_a_grant(panel, monkeypatch):
    def fail(_updates):
        raise OSError("disk unavailable")
    monkeypatch.setattr(settings, "update", fail)
    ctx = ToolCtx(".", "request", None)
    result = EnableCapabilityTool().run({"capability":"screen_capture"}, ctx)
    assert result.startswith("ERROR") and "not enabled" in result
    assert not screenshot._enabled(ctx)
    assert panel["SCREEN_CAPTURE"] == "off"
