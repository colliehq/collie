import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace


NOW = dt.datetime(2026, 8, 12, 20, 0, tzinfo=dt.timezone.utc)


def _guard(provider):
    value = {"schema_version": 1, "provider": provider, "verdict": "allow"}
    if provider == "codex-cli":
        value["account_evidence"] = {
            "credits_remaining": 0, "auto_reload": False,
            "observed_at_utc": "2026-08-12T20:00:00Z",
            "expires_at_utc": "2026-08-12T20:15:00Z",
        }
    return value


def _canonical_sha(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def test_current_schedule_has_admission_then_counterbalanced_configurable_ranking():
    from bench.current_product_rank import canonical_plan

    admission = canonical_plan(99, admission=True)
    plan = canonical_plan(3)

    assert [(row["arm"], row["position"]) for row in admission] == [
        ("collie", 1), ("codex", 2)]
    assert len(plan) == 12
    assert len({row["run_id"] for row in plan}) == 12
    assert all(row["attempt"] == 1 and row["phase"] == "ranking" for row in plan)
    assert {arm: sum(row["position"] == 1 and row["arm"] == arm for row in plan)
            for arm in ("collie", "codex")} == {"collie": 3, "codex": 3}


def test_shared_prompt_is_byte_identical_for_both_workers(monkeypatch, tmp_path):
    from bench import current_product_worker as worker
    from harness import swe

    seen = {}
    credential = tmp_path / "claude.json"
    credential.write_text("opaque", encoding="utf-8")
    task = {
        "run_id": "run-collie", "task_id": "task", "task_sha256": "a" * 64,
        "prompt": "public issue", "delivered_prompt": "EXACT\npublic issue",
        "delivered_prompt_sha256": hashlib.sha256(
            b"EXACT\npublic issue").hexdigest(),
        "claude_credential_source": str(credential),
    }

    def fake_predict(*args, **kwargs):
        seen.update(kwargs)
        request_id = kwargs["request_gate"]("claude_agent_sdk")
        kwargs["request_complete"](request_id, "completed")
        return SimpleNamespace(error="", turns=1, input_tokens=2, output_tokens=3,
                               turns_exhausted=False, cost_usd=0.01)

    monkeypatch.setattr(swe, "predict_collie", fake_predict)
    monkeypatch.setattr(worker, "_collect_patch", lambda workspace: ("diff --git a/x b/x\n", ""))
    row = worker.run_collie(task, tmp_path, tmp_path / "run", tmp_path / "state", 4)

    assert row["worker_outcome"] == "candidate"
    assert seen["provider"] == "claude-agent-sdk"
    assert seen["model"] == "claude-opus-4-8"
    assert seen["complete_prompt"] == task["delivered_prompt"]
    assert seen["benchmark_effort"] == "high"
    assert len(row["request_evidence"]) == 1
    assert row["request_evidence"][0]["outcome"] == "completed"
    assert (tmp_path / "run" / "requests" /
            row["request_evidence"][0]["request_id"] / "reservation.json").is_file()


def test_collie_request_without_settlement_is_invalid(monkeypatch, tmp_path):
    from bench import current_product_worker as worker
    from harness import swe

    credential = tmp_path / "claude.json"
    credential.write_text("opaque", encoding="utf-8")
    task = {"run_id": "run", "prompt": "p", "delivered_prompt": "p",
            "claude_credential_source": str(credential)}

    def fake_predict(*args, **kwargs):
        kwargs["request_gate"]("claude_agent_sdk")
        return SimpleNamespace(error="", input_tokens=1, output_tokens=1,
                               turns_exhausted=False)

    monkeypatch.setattr(swe, "predict_collie", fake_predict)
    monkeypatch.setattr(worker, "_collect_patch", lambda workspace: ("", ""))
    row = worker.run_collie(task, tmp_path, tmp_path / "run", tmp_path / "state", 2)

    assert row["worker_outcome"] == "invalid_infrastructure"
    assert row["error_code"] == "model_request_settlement_incomplete"


def test_codex_trace_rejects_foreign_surfaces_and_rerouting():
    from bench.current_product_worker import _codex_trace_verdict

    terminal, error = _codex_trace_verdict(json.dumps({
        "type": "item.completed", "item": {"type": "web_search"}}), "gpt-5.6-sol")
    assert terminal == ""
    assert error == "codex_forbidden_surface_observed"

    terminal, error = _codex_trace_verdict(json.dumps({
        "type": "turn.completed", "model": "gpt-other"}), "gpt-5.6-sol")
    assert error == "codex_model_rerouted"

    terminal, error = _codex_trace_verdict("\n".join([
        json.dumps({"type": "thread.started", "thread_id": "abc"}),
        json.dumps({"type": "turn.completed", "model": "gpt-5.6-sol"}),
    ]), "gpt-5.6-sol")
    assert (terminal, error) == ("completed", "")


def test_codex_predictor_freezes_capability_surface_and_high_effort(monkeypatch, tmp_path):
    from harness import swe

    seen = {}

    def fake_run(cmd, workdir, extra_env=None, timeout=0, stdin_text=None):
        seen.update(cmd=cmd, extra_env=extra_env, stdin_text=stdin_text)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swe, "_run_cli", fake_run)
    swe.predict_codex(str(tmp_path), "ignored", model="gpt-5.6-sol",
                      complete_prompt="BYTE IDENTICAL")

    assert seen["stdin_text"] == "BYTE IDENTICAL"
    assert "--ephemeral" in seen["cmd"]
    assert "--ignore-user-config" in seen["cmd"]
    assert "--ignore-rules" in seen["cmd"]
    assert seen["cmd"].count("--disable") >= 20
    for expected in (
            'web_search="disabled"', "tools.web_search=false",
            'cli_auth_credentials_store="file"', 'model_reasoning_effort="high"'):
        assert expected in seen["cmd"]
    assert seen["extra_env"]["OPENAI_API_KEY"] is None
    assert seen["extra_env"]["CODEX_BASE_URL"] is None


def test_codex_guard_allows_only_exact_trusted_isolated_home(tmp_path):
    from harness.subscription_guard import SubscriptionGuardError, check_subscription_guard

    home = str((tmp_path / "codex-home").resolve())
    evidence = {"credits_remaining": 0, "auto_reload": False,
                "observed_at_utc": "2026-08-12T20:00:00Z"}
    runner = lambda argv: subprocess.CompletedProcess(argv, 0, "", "Logged in using ChatGPT")

    receipt = check_subscription_guard(
        "codex-cli", account_evidence=evidence, environ={"CODEX_HOME": home},
        runner=runner, expected_codex_home=home, now_utc=NOW)

    encoded = json.dumps(receipt)
    assert receipt["verdict"] == "allow"
    assert receipt["environment"]["isolated_codex_home"].startswith("sha256:")
    assert home not in encoded

    try:
        check_subscription_guard(
            "codex-cli", account_evidence=evidence,
            environ={"CODEX_HOME": home + "-other"}, runner=runner,
            expected_codex_home=home, now_utc=NOW)
    except SubscriptionGuardError as exc:
        assert exc.reason == "codex_home_mismatch"
    else:
        raise AssertionError("mismatched CODEX_HOME was admitted")


def test_summary_is_honest_product_comparison_and_never_publishable():
    from bench.current_product_rank import canonical_plan, summarize

    plan = canonical_plan(1)
    suite = "f" * 64
    rows = [{**row, "suite_sha256": suite, "status": "valid_unresolved",
             "resolved": False, "duration_ms": 10} for row in plan]
    result = summarize(plan, rows, suite)

    assert result["publishable"] is False
    assert result["comparison_label"] == (
        "subscription_native_product_comparison_not_harness_only")
    assert result["ranking_withheld"] is False
    assert all(item["rank"] == 1 for item in result["ranking"])

    rows[0]["status"] = "invalid_infrastructure"
    invalid = summarize(plan, rows, suite)
    assert invalid["ranking_withheld"] is True
    assert invalid["ranking"] is None


def test_summary_withholds_ranking_until_post_run_billing_check():
    from bench.current_product_rank import canonical_plan, summarize

    plan = canonical_plan(1)
    suite = "e" * 64
    rows = [{**row, "suite_sha256": suite, "status": "valid_resolved",
             "resolved": True, "duration_ms": 10} for row in plan]

    result = summarize(plan, rows, suite, require_post_run_billing=True)

    assert result["scores"] is not None
    assert result["ranking"] is None
    assert result["ranking_withheld"] is True
    assert result["billing_post_run_verified"] is False
    assert result["ranking_withheld_reason"] == "post_run_billing_ui_recheck_pending"


def test_evidence_timestamp_must_be_recent_and_after_benchmark(monkeypatch):
    from bench import current_product_rank as rank

    now = dt.datetime(2026, 8, 12, 21, 0, tzinfo=dt.timezone.utc)

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(rank.dt, "datetime", FrozenDateTime)
    assert rank._parse_recent_evidence_timestamp(
        "2026-08-12T20:59:00Z", label="launch") == "2026-08-12T20:59:00Z"

    for value, not_before in (
        ("2026-08-12T20:44:59Z", None),
        ("2026-08-12T21:02:00Z", None),
        ("2026-08-12T20:59:00Z", now),
    ):
        try:
            rank._parse_recent_evidence_timestamp(
                value, label="evidence", not_before=not_before)
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid evidence timestamp was accepted")


def test_worker_input_binds_guard_and_delivered_prompt():
    from bench.current_product_worker import _validate_task

    guard = _guard("claude-agent-sdk")
    prompt = "exact prompt"
    task = {
        "run_id": "r", "task_id": "t", "task_sha256": "a" * 64,
        "prompt": "issue", "model": "claude-opus-4-8", "wall_seconds": 60,
        "guard_receipt": guard, "guard_receipt_sha256": _canonical_sha(guard),
        "delivered_prompt": prompt,
        "delivered_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "claude_credential_source": "/input/claude-credentials.json",
    }
    _validate_task(task, "collie")

    task["delivered_prompt"] = "changed"
    try:
        _validate_task(task, "collie")
    except RuntimeError as exc:
        assert "delivered-prompt" in str(exc)
    else:
        raise AssertionError("tampered prompt was accepted")


def test_child_codex_evidence_strips_parent_receipt_expiry_field():
    from bench.current_product_rank import _worker_codex_evidence

    guard = _guard("codex-cli")
    child = _worker_codex_evidence(guard)

    assert child == {
        "credits_remaining": 0,
        "auto_reload": False,
        "observed_at_utc": "2026-08-12T20:00:00Z",
    }


def test_launch_guard_ignores_only_trusted_parent_codex_metadata(monkeypatch, tmp_path):
    from bench import current_product_rank as rank

    auth = tmp_path / "auth.json"
    auth.write_text("opaque", encoding="utf-8")
    observed = "2026-08-12T20:00:00Z"
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex_desktop")
    monkeypatch.setenv("CODEX_PERMISSION_PROFILE", "disabled")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    seen = []

    def fake_guard(provider, **kwargs):
        seen.append((provider, dict(kwargs.get("environ") or {})))
        return {"provider": provider, "verdict": "allow"}

    monkeypatch.setattr(rank, "check_subscription_guard", fake_guard)
    rank._guard_receipts({
        "credits_remaining": 0, "auto_reload": False,
        "observed_at_utc": observed,
    }, auth)

    codex_environment = next(env for provider, env in seen if provider == "codex-cli")
    assert not any(name in codex_environment for name in (
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_PERMISSION_PROFILE", "CODEX_THREAD_ID"))

    monkeypatch.setenv("OPENAI_API_KEY", "must-be-seen-by-guard")
    seen.clear()
    rank._guard_receipts({
        "credits_remaining": 0, "auto_reload": False,
        "observed_at_utc": observed,
    }, auth)
    codex_environment = next(env for provider, env in seen if provider == "codex-cli")
    assert "OPENAI_API_KEY" in codex_environment


def test_slot_codex_guard_reuses_launch_billing_receipt_but_rechecks_login(tmp_path):
    from harness.subscription_guard import check_subscription_guard

    home = str((tmp_path / "codex-home").resolve())
    launch = _guard("codex-cli")
    launch["auth"] = {"status": "authenticated", "method": "ChatGPT"}
    calls = []

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "Logged in using ChatGPT")

    receipt = check_subscription_guard(
        "codex-cli", codex_launch_receipt=launch,
        environ={"CODEX_HOME": home}, expected_codex_home=home,
        runner=runner, now_utc=NOW + dt.timedelta(hours=5))

    assert calls == [("codex", "login", "status")]
    assert receipt["verdict"] == "allow"
    assert receipt["account_evidence"]["post_run_ui_recheck_required"] is True
