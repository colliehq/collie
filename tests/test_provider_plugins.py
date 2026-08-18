"""Out-of-tree provider plugins (harness.providers._plugin_providers / make_provider).

The hook exists so a provider can live in a separate repo. The three properties that matter are
tested here: a plugin name resolves, a plugin can NOT shadow a built-in, and a plugin that fails to
import says so instead of vanishing into "unknown provider".
"""
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import providers  # noqa: E402


@pytest.fixture
def env_plugins_only(monkeypatch):
    """Ignore pip-installed plugins for the duration of a test.

    Entry-point discovery finding an installed plugin is the feature, not a failure — but it makes
    any assertion about "what discovery returns" a fact about the machine running the suite. These
    tests are about the code, so the installed set is stubbed away and only COLLIE_PROVIDER_PLUGINS
    speaks.
    """
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **kw: [])


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """A throwaway importable directory on sys.path, cleaned out of the module cache after."""
    monkeypatch.syspath_prepend(str(tmp_path))
    made = []

    def write(mod_name, source):
        (tmp_path / (mod_name + ".py")).write_text(textwrap.dedent(source), encoding="utf-8")
        made.append(mod_name)
        return mod_name

    yield write
    for m in made:
        sys.modules.pop(m, None)


def test_plugin_provider_resolves(plugin_dir, monkeypatch):
    mod = plugin_dir("collie_plugin_ok", """
        class Fake:
            name = "fake-relay"
            def __init__(self, model): self.model = model or "default-model"
            def complete(self, system, messages, tool_schemas, on_text=None): ...
        COLLIE_PROVIDERS = {"fake-relay": lambda model: Fake(model)}
    """)
    monkeypatch.setenv("COLLIE_PROVIDER_PLUGINS", mod)
    p = providers.make_provider("fake-relay", model="m1")
    assert p.name == "fake-relay" and p.model == "m1"
    # the factory gets None through when no model is named, so the plugin picks its own default
    assert providers.make_provider("fake-relay").model == "default-model"


def test_plugin_cannot_shadow_a_builtin(plugin_dir, monkeypatch):
    """A plugin claiming `mock` must not displace the real one — built-ins win by construction."""
    mod = plugin_dir("collie_plugin_shadow", """
        class Impostor:
            name = "mock"
            def __init__(self, model): pass
        COLLIE_PROVIDERS = {"mock": lambda model: Impostor(model)}
    """)
    monkeypatch.setenv("COLLIE_PROVIDER_PLUGINS", mod)
    assert isinstance(providers.make_provider("mock"), providers.MockProvider)


def test_broken_plugin_is_reported_not_swallowed(plugin_dir, monkeypatch):
    """The failure mode this hook must not have: a plugin that blew up on import leaving the user
    with a bare 'unknown provider' and no idea the plugin was even involved."""
    mod = plugin_dir("collie_plugin_broken", """
        raise RuntimeError("boom while importing")
        COLLIE_PROVIDERS = {}
    """)
    monkeypatch.setenv("COLLIE_PROVIDER_PLUGINS", mod)
    with pytest.raises(ValueError) as e:
        providers.make_provider("whatever-relay")
    assert "plugin load errors" in str(e.value)
    assert "boom while importing" in str(e.value)


def test_broken_plugin_does_not_break_other_providers(plugin_dir, monkeypatch):
    mod = plugin_dir("collie_plugin_broken2", "raise ImportError('no such dep')")
    monkeypatch.setenv("COLLIE_PROVIDER_PLUGINS", mod)
    assert isinstance(providers.make_provider("mock"), providers.MockProvider)


def test_unknown_provider_lists_what_is_known(monkeypatch):
    monkeypatch.delenv("COLLIE_PROVIDER_PLUGINS", raising=False)
    with pytest.raises(ValueError) as e:
        providers.make_provider("nope")
    msg = str(e.value)
    assert "unknown provider: nope" in msg
    assert "deepseek" in msg and "anthropic" in msg      # the built-in catalogue is offered


def test_multiple_plugin_modules_are_merged(plugin_dir, monkeypatch):
    a = plugin_dir("collie_plugin_a", """
        COLLIE_PROVIDERS = {"relay-a": lambda model: type("A", (), {"name": "relay-a"})()}
    """)
    b = plugin_dir("collie_plugin_b", """
        COLLIE_PROVIDERS = {"relay-b": lambda model: type("B", (), {"name": "relay-b"})()}
    """)
    monkeypatch.setenv("COLLIE_PROVIDER_PLUGINS", a + os.pathsep + b)
    assert providers.make_provider("relay-a").name == "relay-a"
    assert providers.make_provider("relay-b").name == "relay-b"


def test_no_plugins_configured_is_silent(env_plugins_only, monkeypatch):
    """The default path must not pay for, or complain about, a feature nobody is using."""
    monkeypatch.delenv("COLLIE_PROVIDER_PLUGINS", raising=False)
    found, errors = providers._plugin_providers()
    assert found == {} and errors == []


def test_plugin_can_offer_itself_and_ask_for_setup(env_plugins_only, plugin_dir, monkeypatch):
    """COLLIE_PROVIDER_INFO is what puts a plugin in the `collie init` list and lets it ask."""
    mod = plugin_dir("collie_plugin_menu", """
        def _setup():
            return True
        COLLIE_PROVIDERS = {"menu-relay": lambda model: type("M", (), {"name": "menu-relay"})()}
        COLLIE_PROVIDER_INFO = {"menu-relay": {"label": "Menu relay — needs a code",
                                               "setup": _setup}}
    """)
    monkeypatch.setenv("COLLIE_PROVIDER_PLUGINS", mod)
    menu = {v: (label, setup) for v, label, setup in providers.plugin_provider_menu()}
    assert "menu-relay" in menu
    label, setup = menu["menu-relay"]
    assert label == "Menu relay — needs a code"
    assert setup() is True


def test_a_plugin_without_info_stays_usable_but_unlisted(env_plugins_only, plugin_dir, monkeypatch):
    """The original contract is unchanged: COLLIE_PROVIDERS alone means usable, not advertised."""
    mod = plugin_dir("collie_plugin_quiet", """
        COLLIE_PROVIDERS = {"quiet-relay": lambda model: type("Q", (), {"name": "quiet-relay"})()}
    """)
    monkeypatch.setenv("COLLIE_PROVIDER_PLUGINS", mod)
    assert providers.make_provider("quiet-relay").name == "quiet-relay"
    assert providers.plugin_provider_menu() == []


def test_broken_info_costs_a_menu_row_not_a_run(env_plugins_only, plugin_dir, monkeypatch):
    """A plugin whose info is the wrong shape must not take the provider down with it."""
    mod = plugin_dir("collie_plugin_badinfo", """
        COLLIE_PROVIDERS = {"odd-relay": lambda model: type("O", (), {"name": "odd-relay"})()}
        COLLIE_PROVIDER_INFO = {"odd-relay": "not a dict"}
    """)
    monkeypatch.setenv("COLLIE_PROVIDER_PLUGINS", mod)
    assert providers.plugin_provider_menu() == []
    assert providers.make_provider("odd-relay").name == "odd-relay"
