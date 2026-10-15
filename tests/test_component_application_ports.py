"""Application callbacks preserve kernel independence and legacy callback identity."""
import pytest

from harness import application_ports
from harness.run_options import normalize_run_options


def test_missing_factory_does_not_invent_executor():
    with pytest.raises(application_ports.ServiceUnavailable):
        application_ports.cli_factory("not-installed-test-executor")


def test_resolver_observes_replaced_host_without_importing_frontend():
    state = {"host": object()}
    application_ports.register("test-host", lambda: state["host"])
    try:
        assert application_ports.resolve("test-host") is state["host"]
        state["host"] = object()
        assert application_ports.resolve("test-host") is state["host"]
    finally:
        application_ports.unregister("test-host")


def test_pure_accepted_options_are_not_frontend_specific():
    assert normalize_run_options(" TEST ", "THOROUGH", "required") == {
        "intent": "test", "quality": "thorough", "verification": "required"}
    with pytest.raises(ValueError):
        normalize_run_options(verification="skip")
