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


def test_no_plugins_configured_is_silent(monkeypatch):
    """The default path must not pay for, or complain about, a feature nobody is using."""
    monkeypatch.delenv("COLLIE_PROVIDER_PLUGINS", raising=False)
    found, errors = providers._plugin_providers()
    assert found == {} and errors == []
