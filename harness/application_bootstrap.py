"""Explicit full-application assembly for Collie's unified runtime."""
from __future__ import annotations

import importlib

from . import application_ports, paths


FACTORY_NAMES = (
    "_RunnerShim", "_paths", "_worker_history_note", "_worker_model", "_worker_provider",
    "_worker_session", "apply_accepted_capabilities", "apply_accepted_limits",
    "apply_turn_decision", "configure_host_verification", "configure_run_options",
    "configured_model_for", "default_gate", "make_harness", "normalize_run_options",
    "owned_turn_state", "recovery_fence_lifted", "recovery_notice", "resolve_turn_decision",
    "skipped_verification_evidence", "stopped_before_verification", "turn_decision_receipt",
    "turn_receipt_fence",
)


def bind_cli_application(application):
    """Bind live attributes, preserving existing CLI monkeypatch/embedding seams."""
    missing = [name for name in FACTORY_NAMES if not callable(getattr(application, name, None))]
    if missing or not hasattr(application, "DATA"):
        raise application_ports.ServiceUnavailable(
            "the application is missing required bindings: " + ", ".join(missing or ["DATA"]))
    for name in FACTORY_NAMES:
        application_ports.register(
            "factory:" + name, lambda name=name: getattr(application, name))
    application_ports.register("factory:cli", lambda: application)
    paths.set_data_root_resolver(lambda: application.DATA)
    return application


def bootstrap_application():
    """Assemble a Web/desktop entrypoint without a kernel-to-CLI dependency."""
    try:
        application = application_ports.cli_factory("cli")
    except application_ports.ServiceUnavailable:
        application = importlib.import_module("harness.cli")
    return bind_cli_application(application)
