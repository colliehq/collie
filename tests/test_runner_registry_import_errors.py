"""Lazy runner loading must distinguish a missing adapter from its dependency."""
import pytest

from harness import runner_registry


def _missing_adapter(monkeypatch, error):
    monkeypatch.setattr(runner_registry, "_ADAPTERS", {"FixtureAdapter": ("fixture_adapter", None)})

    def load(name, package):
        assert (name, package) == (".fixture_adapter", "harness")
        raise error

    monkeypatch.setattr(runner_registry.importlib, "import_module", load)


def test_missing_adapter_reports_incomplete_unified_runtime(monkeypatch):
    missing = ModuleNotFoundError("synthetic missing adapter", name="harness.fixture_adapter")
    _missing_adapter(monkeypatch, missing)
    with pytest.raises(runner_registry.RunnerUnavailableError, match="runtime is incomplete") as caught:
        runner_registry._adapter("FixtureAdapter")
    assert caught.value.__cause__ is missing
    assert "harness.fixture_adapter" in str(caught.value)
    assert "component" not in str(caught.value)


@pytest.mark.parametrize("dependency", ["fixture_vendor_sdk", "harness.fixture_helper"])
def test_adapter_dependency_failure_preserves_original_error(monkeypatch, dependency):
    missing = ModuleNotFoundError("synthetic missing dependency", name=dependency)
    _missing_adapter(monkeypatch, missing)
    with pytest.raises(ModuleNotFoundError) as caught:
        runner_registry._adapter("FixtureAdapter")
    assert caught.value is missing
    assert caught.value.name == dependency
