"""Durable Mission worker profiles and external code slices."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from harness import (cli, runner_registry, runner_select, runner_signals,
                     runner_slice, sessions, settings)
from harness.missionweb import MissionService, _worker_profile_digest
from harness.primitives import _live_code, _real_code
from harness.recorder import RunResult
from harness.runner_specs import RunnerProbe, RunnerReceipt


def _probe(key="codex-exec", **over):
    now = time.time()
    values = dict(
        key=key, installed=True, executable_path="C:/bin/codex.exe",
        version="99.0.0", login="ok",
        billing_class="subscription_allowance", billing_mode="subscription",
        billing_evidence={"source": "test account status", "observed_at": now},
        # A live vendor status proves the subscription route, but cannot prove
        # that the operator disabled provider-side paid overage.
        overage_attested=False, probed_at=now)
    values.update(over)
    return RunnerProbe(**values)


def _route():
    return SimpleNamespace(
        intent="build", route_kind="code", workspace="mission", strategy="single",
        provider="codex-oauth", model="gpt-5.6-sol")


def _profile(workspace):
    request = runner_select.request_from_surface(
        "mission-code", "codex-exec", _route(),
        {"RUNNER": "collie", "RUNNER_POOL": "collie"}, cwd=str(workspace))
    decision = runner_select.decide(
        request, runner_registry.SPECS, {"codex-exec": _probe()})
    assert decision.runner == "codex-exec", decision.error
    return runner_select.freeze_worker_profile(request, decision)


def _receipt(workspace):
    return RunnerReceipt.from_dict({
        "runner": "codex-exec", "runner_version": "99.0.0",
        "runner_protocol": "codex-exec-jsonl",
        "billing_class": "subscription_allowance", "billing_mode": "subscription",
        "credential_family": "codex", "settled": True, "mutated": True,
        "usage_known": True, "usage": {"input_tokens": 40, "output_tokens": 12},
        "native_session": {"runner": "codex-exec", "locator": "mission-thread",
                           "workspace": str(workspace)},
    })


def test_mission_start_freezes_external_worker_and_rejects_overnight(monkeypatch,
                                                                     tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {key: _probe(key) for key in (keys or ())})
    service = MissionService(
        base=str(tmp_path / "svc"), provider="codex-oauth", model="gpt-5.6-sol",
        decider=lambda *_: {}, stub=True)
    try:
        created = service.start(
            "finish the parser", code=True, workspace=str(workspace),
            runner="codex-exec")
        mission = service.store.get(created["mission_id"])
        profile = mission.case["worker_profile"]
        assert profile["decision"]["runner"] == "codex-exec"
        assert profile["decision"]["billing_class"] == "subscription_allowance"
        assert profile["request"]["surface"] == "mission-code"
        assert mission.leash["worker_profile_sha256"] == _worker_profile_digest(profile)

        no_paid = service.start(
            "bounded external parser work", code=True, workspace=str(workspace),
            runner="codex-exec", no_paid_overage=True)
        no_paid_mission = service.store.get(no_paid["mission_id"])
        no_paid_profile = no_paid_mission.case["worker_profile"]
        assert no_paid_profile["decision"]["probe"][
            "overage_attested"] is True
        refreshed = runner_select.refresh_frozen_worker_profile(
            no_paid_profile, cwd=str(workspace), specs=runner_registry.SPECS,
            probe_all=lambda keys=None, **kw: {
                key: _probe(key, overage_attested=False) for key in (keys or ())})
        assert refreshed.runner == "codex-exec"
        assert refreshed.probe["overage_attested"] is True

        with pytest.raises(ValueError, match="stays on Collie"):
            service.start(
                "unsafe overnight", code=True, workspace=str(workspace), overnight=True,
                no_paid_overage=True, verify_command="python verify.py",
                provider="claude-agent-sdk", model="claude-opus-4-8",
                runner="codex-exec")
    finally:
        service.close()


def test_external_mission_slice_keeps_host_verification_and_worker_receipt(monkeypatch,
                                                                           tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {key: _probe(key) for key in (keys or ())})
    profile = _profile(workspace)
    receipt = _receipt(workspace)
    seen = {}

    def fake_run(decision, task, root, **kwargs):
        seen.update(kwargs)
        seen.update(decision=decision, task=task, root=root)
        seen["runs_db"] = kwargs["recorder"].db.execute(
            "PRAGMA database_list").fetchone()["file"]
        (workspace / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = RunResult(
            task_id="code:external", harness="codex-exec", provider="codex-oauth",
            model="gpt-5.6-sol", input_tokens=40, output_tokens=12,
            total_tokens=52, turns=1, success=True, answer="updated the value",
            error="", cost_usd=0.02, messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, receipt)
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)
    out = _live_code(
        "update the value", str(workspace), mission_id="external-worker",
        worker_profile=profile,
        execution_profile={
            "version": 1, "profile": "durable-code", "provider": "codex-oauth",
            "model": "gpt-5.6-sol", "billing_mode": "metered",
            "subscription_only": False, "allow_provider_fallback": False,
        },
        runs_db=str(state / "mission-runs.db"),
        host_verifier=lambda _root, _result: {
            "verified": True, "detail": "host assertion passed"})

    assert out["verified"] is True
    assert out["runner"]["runner"] == "codex-exec"
    assert out["slice_mutated"] is True
    assert out["_usage"]["cost_usd"] == 0.0
    assert out["equivalent_cost_usd"] == 0.02
    assert seen["model"] == "gpt-5.6-sol"
    assert seen["provider"] == "codex"
    assert seen["runs_db"] == str(state / "mission-runs.db")
    saved = sessions.load(out["session_id"])
    code_receipts = [row for row in saved["run_receipts"]
                     if row.get("kind") == "mission_code_slice"]
    assert code_receipts[-1]["runner"]["native_session"]["locator"] == \
        "mission-thread"
    assert [m["content"] for m in saved["messages"]][-2:] == [
        "update the value", "updated the value"]


@pytest.mark.parametrize("provider_error", ["", "HTTP 429: subscription rate limit"])
def test_external_mission_slice_fails_closed_when_usage_is_unknown(monkeypatch,
                                                                    tmp_path, provider_error):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    target = workspace / "value.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {key: _probe(key) for key in (keys or ())})
    profile = _profile(workspace)
    unknown_receipt = RunnerReceipt.from_dict({
        "runner": "codex-exec", "runner_version": "99.0.0",
        "runner_protocol": "codex-exec-jsonl",
        "billing_class": "subscription_allowance", "billing_mode": "subscription",
        "credential_family": "codex", "settled": True, "mutated": True,
        "usage_known": False, "usage": {"known": False},
        "native_session": {"runner": "codex-exec", "locator": "unknown-usage",
                           "workspace": str(workspace)},
    })

    def fake_run(*args, **kwargs):
        target.write_text("VALUE = 2\n", encoding="utf-8")
        result = RunResult(
            task_id="code:external", harness="codex-exec", provider="codex",
            model="", input_tokens=None, output_tokens=None, total_tokens=None,
            cache_read=None, cache_creation=None, cost_usd=None, turns=1,
            success=not provider_error, answer="updated the value", error=provider_error,
            retry_at=int(time.time()) + 18000 if provider_error else 0, messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, unknown_receipt)
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)
    out = _live_code(
        "update the value", str(workspace), mission_id="unknown-usage-worker",
        worker_profile=profile,
        execution_profile={
            "version": 1, "profile": "durable-code", "provider": "codex-oauth",
            "model": "gpt-5.6-sol", "billing_mode": "subscription",
            "subscription_only": True, "allow_provider_fallback": False,
        },
        host_verifier=lambda _root, _result: {
            "verified": True, "detail": "host assertion passed"})

    assert out["verified"] is False
    assert out["needs_human"] is True
    assert out["continue_needed"] is False
    assert out["_usage_known"] is False
    assert "did not report usage" in out["error"]
    saved = sessions.load(out["session_id"])
    receipt = [row for row in saved["run_receipts"]
               if row.get("kind") == "mission_code_slice"][-1]
    assert receipt["usage"]["known"] is False
    assert receipt["usage"]["input_tokens"] is None
    assert "did not report usage" in receipt["verification"]["usage_guard"]
    assert "Worker error" in saved["messages"][-1]["content"]
    assert "did not report usage" in saved["messages"][-1]["content"]


@pytest.mark.parametrize("requires_recovery", [False, True])
def test_external_quota_wait_preserves_the_workers_recovery_boundary(monkeypatch, tmp_path,
                                                                    requires_recovery):
    from dataclasses import replace
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {key: _probe(key) for key in (keys or ())})
    receipt = replace(_receipt(workspace), settled=False, recovery_required=requires_recovery)
    reset = int(time.time()) + 18000

    def fake_run(*args, **kwargs):
        (workspace / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = RunResult(task_id="quota", input_tokens=40, output_tokens=12,
                           error="HTTP 429: rate limit", retry_at=reset, messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, receipt)
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)
    out = _live_code(
        "update the value", str(workspace), mission_id="quota-worker",
        worker_profile=_profile(workspace),
        execution_profile={"version": 1, "profile": "durable-code", "provider": "codex-oauth",
                           "model": "gpt-5.6-sol", "billing_mode": "subscription",
                           "subscription_only": True, "allow_provider_fallback": False})
    assert out["retry_at"] == reset and not out["verified"]
    assert out["recovery_required"] is requires_recovery
    assert out["continue_needed"] is (not requires_recovery)


def test_worker_profile_refresh_rejects_a_changed_billing_route(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    profile = _profile(workspace)

    def probes(keys=None, **_kwargs):
        return {key: _probe(key, billing_class="api_metered",
                            billing_mode="metered") for key in (keys or ())}

    with pytest.raises(ValueError, match="billing route changed"):
        runner_select.refresh_frozen_worker_profile(
            profile, cwd=str(workspace), specs=runner_registry.SPECS,
            probe_all=probes)


def test_code_capability_keeps_unknown_external_usage_visible():
    def worker(_goal, **_kwargs):
        return {
            "answer": "worker stopped safely", "verified": False,
            "needs_human": True, "_usage_known": False,
            "_usage": {"input_tokens": 0, "output_tokens": 0,
                       "cache_tokens": 0, "cost_usd": 0.0},
            "equivalent_cost_usd": None,
            "runner": {"runner": "codex-exec", "usage_known": False},
        }

    result = _real_code(worker)(SimpleNamespace(
        args={"goal": "do work", "_case": {}}, job_id="msn_unknown"))

    assert result["_usage_known"] is False
    assert result["equivalent_cost_usd"] is None
    assert result["runner"]["runner"] == "codex-exec"


def test_auto_worker_profile_refresh_rechecks_live_quota(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    profile = _profile(workspace)
    # The frozen decision names Codex, but the original authority was Auto.
    profile["request"].update(pin="", configured="auto", pool=["codex-exec"])
    seen = {}

    def probes(keys=None, **_kwargs):
        return {
            key: (_probe(key) if key == "codex-exec" else RunnerProbe(
                key="collie", installed=True, login="n/a",
                billing_class="subscription_allowance", billing_mode="subscription"))
            for key in (keys or ())
        }

    def signals(request, live_probes, *, runs_db=""):
        seen.update(request=request, probes=live_probes, runs_db=runs_db)
        return runner_signals.SignalSet({
            "codex-exec": runner_signals.RouteSignals(
                runner="codex-exec", auth_status="ok",
                quota=runner_signals.QuotaSnapshot(
                    primary=runner_signals.RateLimitWindow(used_percent=95),
                    observed_at=time.time())),
            "collie": runner_signals.RouteSignals(
                runner="collie", auth_status="ok"),
        })

    with pytest.raises(ValueError, match="changed identity"):
        runner_select.refresh_frozen_worker_profile(
            profile, cwd=str(workspace), specs=runner_registry.SPECS,
            probe_all=probes, runs_db=str(tmp_path / "runs.db"),
            signal_loader=signals)
    assert seen["runs_db"].endswith("runs.db")
    assert seen["request"].configured == "auto" and not seen["request"].pin
