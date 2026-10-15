"""Application assembly and namespace compatibility without external services."""
from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from harness import application_bootstrap, application_ports, paths


@pytest.fixture
def isolated_bindings(monkeypatch):
    monkeypatch.setattr(application_ports, "_bindings", {})
    previous = paths.set_data_root_resolver(None)
    try:
        yield
    finally:
        paths.set_data_root_resolver(previous)


def fake_application(data):
    application = SimpleNamespace(DATA=str(data))
    for name in application_bootstrap.FACTORY_NAMES:
        setattr(application, name, lambda *args, **kwargs: None)
    application._paths = lambda: paths.runtime_paths(application.DATA, create=False)
    return application


def test_bound_factories_and_journals_observe_current_cli_data(tmp_path, monkeypatch,
                                                            isolated_bindings):
    from harness import sessions

    monkeypatch.delenv("COLLIE_SESSIONS_DIR", raising=False)
    application = fake_application(tmp_path / "first")
    application_bootstrap.bind_cli_application(application)
    replacement = lambda: "replacement"
    application.make_harness = replacement
    assert application_ports.cli_factory("make_harness") is replacement

    sessions.save("first-journal", [{"role": "user", "content": "synthetic"}])
    assert (tmp_path / "first" / "sessions" / "first-journal.json").is_file()
    application.DATA = str(tmp_path / "second")
    assert paths.data_dir() == application.DATA
    assert application_ports.cli_factory("_paths")()[1] == str(tmp_path / "second" / "runs.db")
    sessions.save("second-journal", [{"role": "user", "content": "synthetic"}])
    assert (tmp_path / "second" / "sessions" / "second-journal.json").is_file()
    assert not (tmp_path / "first" / "sessions" / "second-journal.json").exists()
    explicit = tmp_path / "explicit"
    assert paths.sessions_dir(str(explicit), create=False) == str(explicit)


@pytest.mark.parametrize("entrypoint", ["main", "bind_server"])
def test_independent_web_entrypoint_bootstraps_before_binding(tmp_path, monkeypatch,
                                                            isolated_bindings, entrypoint):
    from harness import plat, webapp

    application = fake_application(tmp_path / "data")
    original_import = application_bootstrap.importlib.import_module
    monkeypatch.setattr(application_bootstrap.importlib, "import_module",
                        lambda name: application if name == "harness.cli" else original_import(name))
    monkeypatch.setattr(plat, "make_output_safe", lambda: None)

    class StopBeforeSocket(RuntimeError):
        pass

    def server(*args, **kwargs):
        assert application_ports.cli_factory("make_harness") is application.make_harness
        assert paths.data_dir() == application.DATA
        raise StopBeforeSocket()

    monkeypatch.setattr(webapp, "CollieHTTPServer", server)
    with pytest.raises(StopBeforeSocket):
        if entrypoint == "main":
            webapp.main(["--no-open"])
        else:
            webapp.bind_server()


def test_missing_execution_binding_cannot_take_a_lease_or_claim(monkeypatch, isolated_bindings):
    from harness import web_tasks

    application_ports.register("web_surface", lambda: SimpleNamespace(Handler=object))

    def forbidden(*args, **kwargs):
        raise AssertionError("readiness must be checked before lease/claim")

    monkeypatch.setattr(web_tasks.session_owner, "try_acquire", forbidden)
    monkeypatch.setattr(web_tasks, "claim_next", forbidden)
    with pytest.raises(web_tasks.WebInputError) as caught:
        web_tasks.start_pending("synthetic-pending")
    assert "make_harness" in str(caught.value)


@pytest.mark.parametrize("missing", ["harness.embeddings", "vendor_dependency"])
def test_compat_cli_does_not_hide_missing_runtime_or_vendor_modules(monkeypatch, missing):
    from harness import core_cli

    def missing_module(*args, **kwargs):
        raise ModuleNotFoundError("synthetic missing dependency", name=missing)

    monkeypatch.setattr(core_cli.importlib, "import_module", missing_module)
    with pytest.raises(ModuleNotFoundError, match="synthetic missing") as caught:
        core_cli.main(["--help"])
    assert caught.value.name == missing


def test_compat_cli_reports_one_distribution_without_loading_application(monkeypatch, capsys):
    import json
    from harness import __version__, core_cli

    requested = []

    def metadata(name):
        requested.append(name)
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(core_cli.importlib.metadata, "version", metadata)
    monkeypatch.setattr(core_cli.importlib, "import_module",
                        lambda *args: pytest.fail("diagnostics cannot assemble an application"))
    assert core_cli.main(["components"]) == 0
    rows = json.loads(capsys.readouterr().out)["components"]
    assert rows == [{"distribution": "collie-harness", "installed": False, "version": None,
                     "source_version": __version__, "functional_verification": "not_run"}]
    assert requested == ["collie-harness"]


def test_compat_cli_keeps_task_service_and_full_application_argv(monkeypatch):
    from harness import core_cli, evidence_bridge

    seen = []
    monkeypatch.setattr(evidence_bridge, "main", lambda args: seen.append(args) or 17)
    assert core_cli.main(["task", "serve", "--config", "synthetic.json"]) == 17
    assert seen == [["--config", "synthetic.json"]]
    original = sys.argv
    app = SimpleNamespace(main=lambda: seen.append(list(sys.argv)) or 23)
    monkeypatch.setattr(core_cli.importlib, "import_module", lambda name: app)
    assert core_cli.main(["run", "synthetic"]) == 23
    assert seen[-1][1:] == ["run", "synthetic"]
    assert sys.argv is original


def test_flat_runtime_keeps_one_canonical_package_without_namespace_addons(tmp_path):
    root = Path(__file__).resolve().parents[1]
    isolated = tmp_path / "namespace"
    addon = isolated / "addon" / "harness"
    addon.mkdir(parents=True)
    (addon / "component_probe.py").write_text("VALUE = 17\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("CODEX_", "COLLIE_", "ANTHROPIC_", "OPENAI_", "CLAUDE_"))
           and not any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))}
    env.update(HOME=str(isolated), USERPROFILE=str(isolated),
               COLLIE_STATE_DIR=str(isolated / "state"),
               COLLIE_DATA_DIR=str(isolated / "data"))
    code = """
import importlib.abc, pathlib, socket, sys
sys.path[:0] = [sys.argv[1], str(pathlib.Path(sys.argv[2]) / 'addon')]
class NoFeatures(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('harness.cli', 'harness.providers', 'harness.tools'):
            raise ImportError('feature unavailable: ' + fullname)
        return None
sys.meta_path.insert(0, NoFeatures())
def deny(*args, **kwargs):
    raise AssertionError('network forbidden in namespace fixture')
socket.socket.connect = deny
socket.create_connection = deny
import harness, importlib.util
assert list(harness.__path__) == [str(pathlib.Path(sys.argv[1]) / 'harness')]
assert importlib.util.find_spec('harness.component_probe') is None
assert not (pathlib.Path(sys.argv[1]) / 'components').exists()
print('one canonical package resolved without a component manifest')
"""
    completed = subprocess.run([sys.executable, "-I", "-c", code, str(root), str(isolated)],
                               cwd=str(isolated), env=env, capture_output=True,
                               text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert "one canonical package resolved without a component manifest" in completed.stdout
