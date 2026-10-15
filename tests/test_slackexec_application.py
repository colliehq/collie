"""Explicit CLI child assembly remains behind release and stdin/owner setup."""
import os
import sys
from types import SimpleNamespace

import pytest

from harness import application_bootstrap, application_ports, slackexec


@pytest.fixture
def plumbing(monkeypatch):
    events = []
    monkeypatch.setattr(slackexec, "_read_release", lambda: events.append("release") or True)
    # Keep the fake descriptors local to this module. Patching the real os.dup2
    # would also alter pytest's capture machinery and obscure the child order.
    monkeypatch.setattr(slackexec, "sys", SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: 0),
                                                        argv=sys.argv, stderr=sys.stderr))
    monkeypatch.setattr(slackexec, "os", SimpleNamespace(
        name=os.name, environ=os.environ, devnull=os.devnull, O_RDONLY=os.O_RDONLY,
        open=lambda *args: 99, dup=lambda *args: 100,
        dup2=lambda *args: events.append("stdin-devnull"), close=lambda *args: None,
        set_inheritable=lambda *args: None))
    monkeypatch.setattr(slackexec, "threading", SimpleNamespace(
        Thread=lambda **kwargs: SimpleNamespace(start=lambda: None)))
    monkeypatch.setenv("COLLIE_PROCESS_OWNER", "fixture-before")
    return events


def test_fresh_cli_assembles_after_release_and_owned_stdin(plumbing, monkeypatch):
    def missing(name):
        assert name == "cli"
        raise application_ports.ServiceUnavailable("no fresh-process binding")
    monkeypatch.setattr(application_ports, "cli_factory", missing)
    def assemble():
        assert plumbing == ["release", "stdin-devnull"]
        assert os.environ["COLLIE_PROCESS_OWNER"] == "slackexec"
        plumbing.append("assemble")
        return SimpleNamespace(main=lambda args: 23 if args == ["run", "fixture"] else 99)
    monkeypatch.setattr(application_bootstrap, "bootstrap_application", assemble)
    assert slackexec.main([sys.executable, "-m", "harness.cli", "run", "fixture"]) == 23
    assert plumbing == ["release", "stdin-devnull", "assemble"]


def test_registered_cli_binding_is_preserved(plumbing, monkeypatch):
    app = SimpleNamespace(main=lambda args: 17)
    monkeypatch.setattr(application_ports, "cli_factory", lambda name: app)
    def forbidden():
        raise AssertionError("registered application must not be replaced")
    monkeypatch.setattr(application_bootstrap, "bootstrap_application", forbidden)
    assert slackexec.main([sys.executable, "-m", "harness.cli", "run", "fixture"]) == 17
    assert plumbing == ["release", "stdin-devnull"]


def test_unreleased_cli_never_resolves_or_assembles_application(plumbing, monkeypatch):
    monkeypatch.setattr(slackexec, "_read_release", lambda: False)
    def forbidden(*args):
        raise AssertionError("an unreleased task cannot assemble its application")
    monkeypatch.setattr(application_ports, "cli_factory", forbidden)
    monkeypatch.setattr(application_bootstrap, "bootstrap_application", forbidden)
    assert slackexec.main([sys.executable, "-m", "harness.cli", "run", "fixture"]) == 75
    assert plumbing == []
