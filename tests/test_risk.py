"""Risk classification — the table is the policy, so the tests guard the table.

The load-bearing one is test_every_registered_tool_is_classified: it walks the LIVE
registry, so a new tool cannot reach users without someone deciding what it can reach.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from harness import risk as R
from harness.risk import RiskClass


def _live_registry():
    from harness.tools import default_registry
    reg = default_registry(code_search=True, web_search=True, exec_code=True, delegate=True)
    for mod, fn in (("harness.browserbridge", "register_browser_bridge"),
                    ("harness.native", "register_native"),
                    ("harness.websearch", "register_web_search"),
                    ("harness.webfetch", "register_web_fetch"),
                    ("harness.mcpclient", "register_mcp_management")):
        try:
            __import__(mod)
            getattr(sys.modules[mod], fn)(reg)
        except Exception:
            pass          # platform-gated (desktop on Linux, bridge without a browser)
    return reg


def test_every_registered_tool_is_classified():
    """No tool may fall through to the EXTERNAL default. Falling through is safe but
    silent, and silence is how a tool ships without anyone deciding about it.

    Covered means the table OR a glob rule — an MCP server's tools are classified by the
    `mcp__*` rule, and this machine may well have live ones registered.
    """
    reg = _live_registry()
    unclassified = [n for n in reg.names() if not R.is_classified(n)]
    assert not unclassified, (
        "these registered tools have no risk decision — classify them:\n  "
        + "\n  ".join(sorted(unclassified)))


def test_a_live_mcp_tool_counts_as_classified():
    """Guards the guard: if is_classified stopped honouring patterns, the test above would
    start failing on any machine with an MCP server configured — and the tempting fix
    would be to loosen it."""
    assert R.is_classified("mcp__slack__slack_send_message")
    assert not R.is_classified("some_tool_nobody_has_classified")


def _every_tool_class_name():
    """Every Tool subclass name in the package, whether or not this machine/env
    registers it. Many tools are gated (desktop_* by platform, run_in_env by
    COLLIE_E2E_IMAGE, browser_* by a live bridge), so "not in the live registry"
    does NOT mean "gone" — only "not here"."""
    import importlib
    import inspect
    import pkgutil

    import harness
    from harness.tools import Tool

    names = set()
    for m in pkgutil.iter_modules(harness.__path__):
        try:
            mod = importlib.import_module("harness." + m.name)
        except Exception:
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, Tool) and obj is not Tool and getattr(obj, "name", ""):
                names.add(obj.name)
        # desktop_* classes are defined INSIDE register_native's platform branches,
        # so a module-level scan cannot see them; read them off the source instead.
        src = inspect.getsource(mod) if m.name == "native" else ""
        for line in src.splitlines():
            line = line.strip()
            if line.startswith("name, tier = ") or line.startswith("name = "):
                for quote in ('"', "'"):
                    if quote in line:
                        names.add(line.split(quote)[1])
                        break
    return names


def test_table_has_no_stale_entries():
    """The reverse guard: an entry for a tool that no longer exists is dead policy
    that reads as coverage."""
    known = _every_tool_class_name()
    stale = [n for n in R._BASE if n not in known]
    assert not stale, "risk._BASE names tools that no longer exist: %s" % sorted(stale)


def test_unknown_tool_defaults_to_external():
    """Fail closed. collie's tool set is open (MCP, enable_capability, plugins)."""
    assert R.classify("something_nobody_classified") is RiskClass.EXTERNAL


def test_mcp_tools_default_to_external():
    assert R.classify("mcp__notion__create_page") is RiskClass.EXTERNAL
    assert R.classify("mcp__fs__read_file") is RiskClass.EXTERNAL


def test_override_wins_over_table():
    """A user who trusts a server can relax it; that is the only way down."""
    assert R.classify("bash") is RiskClass.EXEC
    assert R.classify("bash", overrides=lambda n: RiskClass.READ) is RiskClass.READ


def test_tool_self_declaration_cannot_override_the_table():
    """A plugin declaring itself harmless must not beat a decision made in the table."""
    class Sneaky:
        risk = "read"
    assert R.classify("bash", Sneaky()) is RiskClass.EXEC


def test_tool_self_declaration_used_only_when_unknown():
    class Plugin:
        risk = "read"
    assert R.classify("some_plugin_tool", Plugin()) is RiskClass.READ

    class Garbage:
        risk = "harmless"
    assert R.classify("other_plugin_tool", Garbage()) is RiskClass.EXTERNAL


@pytest.mark.parametrize("name", [
    "browser_click", "browser_type", "browser_press", "browser_upload",
    "browser_eval", "browser_script", "desktop_click", "desktop_type",
    "enable_capability", "delegate",
])
def test_reaching_off_machine_is_external(name):
    assert R.classify(name) is RiskClass.EXTERNAL


@pytest.mark.parametrize("name", [
    "browser_read", "browser_snapshot", "browser_links", "browser_screenshot",
    "read_file", "grep", "glob", "web_fetch", "desktop_read",
])
def test_observing_is_read(name):
    assert R.classify(name) is RiskClass.READ


def test_is_consequential():
    assert not R.is_consequential(RiskClass.READ)
    for r in (RiskClass.WRITE_LOCAL, RiskClass.EXEC, RiskClass.EXTERNAL):
        assert R.is_consequential(r)


# -- targets ----------------------------------------------------------------
def test_origin_of():
    assert R.origin_of("https://Mail.Google.com/mail/u/0#inbox") == "https://mail.google.com"
    assert R.origin_of("http://localhost:5173/app") == "http://localhost:5173"
    assert R.origin_of("not a url") == ""
    assert R.origin_of("") == ""


def test_browser_open_target_is_its_destination_not_the_current_page():
    """Navigation authorizes where it is GOING. Using the current origin would let a
    rule for the safe page you are on authorize a jump to anywhere."""
    t = R.target_for("browser_open", {"url": "https://evil.example/x"},
                     origin_lookup=lambda: "http://localhost:5173")
    assert t == "https://evil.example"


def test_browser_action_target_is_the_live_origin():
    t = R.target_for("browser_click", {"ref": "e3"},
                     origin_lookup=lambda: "https://github.com/foo/bar")
    assert t == "https://github.com"


def test_browser_target_is_none_without_a_live_lookup():
    """No lookup -> no target -> no standing rule -> asked every time. Never a guess."""
    assert R.target_for("browser_click", {"ref": "e3"}, origin_lookup=None) is None


def test_browser_target_is_none_when_lookup_fails():
    def boom():
        raise RuntimeError("bridge down")
    assert R.target_for("browser_click", {"ref": "e3"}, origin_lookup=boom) is None


@pytest.mark.parametrize("name", sorted(R.NO_STANDING_RULE))
def test_never_a_standing_rule(name):
    """Arbitrary code in a logged-in page — or on this machine — is asked every time."""
    assert R.target_for(name, {"url": "https://x.test", "expr": "1"},
                        origin_lookup=lambda: "https://x.test") is None


def test_desktop_target_is_the_app():
    assert R.target_for("desktop_click", {"app": "Slack.exe"}, None) == "slack.exe"
    assert R.target_for("desktop_click", {}, None) is None
