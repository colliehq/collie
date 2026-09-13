"""Contract tests for harness/runner_specs.py.

Three properties are load-bearing and everything else here supports them:
nothing credential-shaped survives serialization, unreported usage stays None
instead of decaying into a flattering zero, and the canonical event vocabulary is
a closed set.
"""
import json
import re

import pytest

from harness import runner_specs
from harness.agent_runners import RunnerSnapshot
from harness.runner_specs import (
    BILLING_CLASSES,
    BILLING_MODE_OF,
    CANONICAL_TYPES,
    CURRENT_PHASE,
    EXTERNAL_ALLOWED_SURFACES,
    SURFACES,
    ApprovalDecision,
    ApprovalRequest,
    CandidateScore,
    CanonicalEvent,
    HarnessDecision,
    HarnessRequest,
    HarnessSpec,
    NativeSessionRef,
    RunnerCapabilities,
    RunnerError,
    RunnerProbe,
    RunnerProtocolError,
    RunnerReceipt,
    RunnerUsage,
    equivalent_cost_usd,
    family_of_provider,
    redact_text,
    snapshot_to_run_result,
    stable_digest,
    usage_to_collie,
)


# The exact patterns the design forbids from any serialized runner artifact.
SECRET_RE = re.compile(r"sk-|sess-|Bearer |eyJ|ghp_|AKIA")

SECRETS = (
    "sk-ant-api03-Ab3xQ9mZ0PlKjHgFdSaW",
    "sess-0123456789abcdefghij",
    "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
    "ghp_ABCDEFGHIJKLMNOP1234567890",
    "AKIAIOSFODNN7EXAMPLE",
    "Authorization: Bearer sk-live-9f8e7d6c5b4a3210",
)


def _codex_caps():
    return RunnerCapabilities(
        protocol="codex-exec-jsonl",
        protocol_version="0.149.0",
        session_resume=True,
        cancel="process-tree",
        usage_tokens=True,
        confinement="workspace-write",
        tools=frozenset({"code", "bash"}),
        windows_native=True,
    )


def _codex_spec():
    return HarnessSpec(
        key="codex-exec",
        label="Codex (external)",
        kind="external",
        binary="codex",
        version_argv=("codex", "--version"),
        min_version="0.149.0",
        credential_family="codex",
        caps=_codex_caps(),
        env_policy="codex",
        guard_alias="codex-cli",
        phase=1,
        notes=("codex exec resume --ignore-user-config: unverified",),
    )


def _codex_probe(**over):
    values = dict(
        key="codex-exec",
        installed=True,
        executable_path="C:/npm/codex.CMD",
        version="0.149.0",
        login="ok",
        billing_class="subscription_allowance",
        billing_mode="subscription",
        billing_evidence={"source": "codex login status", "plan": "chatgpt"},
        capabilities=_codex_caps().to_dict(),
        compat="verified 2026-08-21",
        probed_at=1_700_000_000.0,
    )
    values.update(over)
    return RunnerProbe(**values)


# --- redaction --------------------------------------------------------------
def test_redact_text_masks_every_forbidden_pattern():
    for secret in SECRETS:
        cleaned = redact_text("runner said: %s <- there" % secret)
        assert not SECRET_RE.search(cleaned), (secret, cleaned)
        assert "runner said:" in cleaned          # only the secret is removed


def test_redact_text_is_idempotent_and_keeps_ordinary_words():
    once = redact_text("task-id risk-free basket sk-ant-api03-Ab3xQ9mZ0PlKjHgF")
    assert redact_text(once) == once
    # 'sk'/'sess' inside ordinary words are not token prefixes and must survive.
    assert "task-id risk-free basket" in once


def test_redact_text_keeps_the_authorization_header_name_but_not_its_value():
    cleaned = redact_text("Authorization: Bearer sk-live-9f8e7d6c5b4a3210\nnext line")
    assert cleaned.startswith("Authorization: [redacted]")
    assert "next line" in cleaned
    assert not SECRET_RE.search(cleaned)


@pytest.mark.parametrize("raw, forbidden", [
    ('{"api_key": "ordinarylookingsecret123456"}', "ordinarylookingsecret123456"),
    ("PASSWORD=correct-horse-battery-staple", "correct-horse-battery-staple"),
    ("tool --token abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
    ("Cookie: session=abcdefghijklmnop", "session=abcdefghijklmnop"),
    ("postgres://alice:longdatabasepassword@db.internal/app",
     "alice:longdatabasepassword"),
    ("xox" + "b-1234567890-abcdefghijklmnop",
     "xox" + "b-1234567890-abcdefghijklmnop"),
    ("rk_live_1234567890abcdefghijkl", "rk_live_1234567890abcdefghijkl"),
])
def test_redact_text_masks_structural_credentials_without_vendor_prefix(raw, forbidden):
    cleaned = redact_text("runner echoed " + raw)
    assert forbidden not in cleaned
    assert "[redacted]" in cleaned


def test_redact_text_does_not_mask_short_settings_or_normal_code():
    raw = 'password = get_password()\napi_key="dev"\nhttps://example.com/path'
    assert redact_text(raw) == raw


# --- serialization is credential-free ---------------------------------------
def test_roundtrip_no_secret_keys():
    """Every type round-trips through dict, and nothing runner-supplied leaks."""
    poison = " | ".join(SECRETS)

    probe = _codex_probe(
        detail="codex login status failed: %s" % poison,
        billing_evidence={"source": "codex login status", "raw": poison},
    )
    spec = _codex_spec()
    caps = _codex_caps()
    session = NativeSessionRef(
        runner="codex-exec", workspace="C:/repo", locator="0199a213-81c0-7800-8aa1-bbab2a035a53",
        protocol_version="0.149.0", created_at=1_700_000_000.0, workspace_digest="abc123",
    )
    event = CanonicalEvent(
        cursor=7, type="tool.completed", native_type="item.completed",
        payload={"command": "curl -H '%s'" % poison, "nested": [{"env": poison}]},
        at=1_700_000_001.0, runner="codex-exec",
    )
    request = ApprovalRequest(
        approval_id="ap-1", runner="codex-exec", kind="command",
        command="curl -H 'Authorization: Bearer %s'" % SECRETS[0],
        cwd="C:/repo", paths=("src/a.py",), raw={"argv": ["curl", poison]},
    )
    approval = ApprovalDecision(
        approval_id="ap-1", outcome="reject_once",
        reason="cmdsafety denied; runner echoed %s" % poison, decided_by="cmdsafety-deny",
    )
    usage = usage_to_collie("codex-exec", {"input_tokens": 100, "cached_input_tokens": 40,
                                           "output_tokens": 20, "cache_write_input_tokens": 5})
    score = CandidateScore(key="codex-exec", eligible=True, hard_reasons=(), score=0.72,
                           terms={"pool_order": 1.0, "fit": 0.5}, soft_reasons=("pool_order: 1",))
    decision = HarnessDecision(
        runner="codex-exec", source="user", credential_family="codex",
        billing_class="subscription_allowance", billing_mode="subscription",
        reasons=("runner: codex-exec (user); family=codex billing=subscription_allowance",),
        rejected={"claude-code": "H4: needs bash"}, candidates=(score,),
        fallback_chain=(), probe=probe.to_dict(), probe_digest=stable_digest([probe.to_dict()]),
    )
    harness_request = HarnessRequest(
        surface="run", intent="build", route_kind="code", needs=frozenset({"code", "bash"}),
        workspace="current", workspace_path="C:/repo", workspace_is_git=True,
        provider="codex-oauth", model="gpt-5.6-terra", provider_family="codex",
        pin="codex-exec", configured="auto", pool=("codex-exec", "collie"),
    )
    receipt = RunnerReceipt(
        runner="codex-exec", runner_version="0.149.0", runner_protocol="codex-exec-jsonl",
        runner_protocol_version="0.149.0", billing_class="subscription_allowance",
        billing_mode="subscription", credential_family="codex",
        decision=decision.to_dict(), native_session=session.to_dict(),
        usage=usage.to_dict(), usage_known=usage.known,
        cost_usd_reported=None, cost_usd_equivalent=0.000_5, model="gpt-5.6-terra",
        settled=True, recovery_required=False, mutated=True,
        events_digest=stable_digest([event.to_dict()]), event_count=1,
        approvals=(approval.to_dict(),),
        env_receipt={"allowed": ["PATH", "APPDATA"], "stripped": ["OPENAI_API_KEY"]},
        error="codex exited 1: %s" % poison,
    )

    for value in (caps, spec, probe, session, event, request, approval, usage,
                  score, decision, harness_request, receipt):
        encoded = json.dumps(value.to_dict(), ensure_ascii=False, sort_keys=True)
        assert not SECRET_RE.search(encoded), "%s leaked a credential: %s" % (
            type(value).__name__, encoded)
        restored = type(value).from_dict(value.to_dict())
        assert restored == value, type(value).__name__
        assert restored.to_dict() == value.to_dict()


def test_env_receipt_and_evidence_carry_names_not_values():
    probe = _codex_probe(billing_evidence={"source": "file:~/.codex/auth.json",
                                           "expires_at": 1_700_003_600})
    # Evidence points AT the credential file and its expiry; it never quotes one.
    assert probe.to_dict()["billing_evidence"] == {
        "source": "file:~/.codex/auth.json", "expires_at": 1_700_003_600}
    assert "token" not in json.dumps(probe.to_dict()["billing_evidence"])


def test_probe_availability_does_not_conflate_installed_with_runnable():
    declared = RunnerProbe(
        key="future", installed=True, login="ok",
        detail="not implemented in this phase: future arrives later")
    assert declared.availability() == "declared-not-runnable"
    assert declared.to_dict()["runnable"] is False

    unverified = RunnerProbe(
        key="codex-exec", installed=True, login="ok", compat="unverified")
    assert unverified.availability() == "runnable-unverified"
    assert unverified.to_dict()["compat_verified"] is False

    verified = RunnerProbe(
        key="codex-exec", installed=True, login="ok", compat="verified 2026-08-25")
    assert verified.availability() == "runnable-verified"
    assert verified.to_dict()["compat_verified"] is True


def test_wire_booleans_are_strict_at_authority_boundaries():
    caps = RunnerCapabilities.from_dict({
        "protocol": "x", "session_create": "false", "streaming": "false",
        "steer": "false", "approval_round_trip": "false",
        "needs_git_workspace": "false", "windows_native": "false",
    })
    assert caps.session_create is False
    assert caps.streaming is caps.steer is caps.approval_round_trip is False
    assert caps.needs_git_workspace is False and caps.windows_native is False

    probe = RunnerProbe.from_dict({
        "key": "x", "installed": "false", "overage_attested": "false",
        "capabilities": {"steer": "false", "approval_round_trip": "false"},
    })
    assert probe.installed is probe.overage_attested is False
    assert probe.capabilities["steer"] is False
    assert probe.capabilities["approval_round_trip"] is False

    request = HarnessRequest.from_dict({
        "workspace_is_git": "false", "no_paid_overage": "false",
        "subscription_only": "false", "overnight": "false",
        "has_approver": "false",
    })
    assert request.workspace_is_git is False
    assert request.no_paid_overage is request.subscription_only is False
    assert request.overnight is request.has_approver is False


def test_receipt_wire_state_and_usage_fail_closed_on_malformed_values():
    receipt = RunnerReceipt.from_dict({
        "runner": "codex-exec",
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "usage_known": "false", "settled": "false",
        "recovery_required": "false", "mutated": "false",
        "cost_usd_reported": float("nan"),
        "cost_usd_equivalent": float("inf"),
        "event_count": float("nan"),
    })
    assert receipt.usage_known is False
    assert receipt.settled is receipt.recovery_required is False
    assert receipt.mutated is False
    assert receipt.cost_usd_reported is receipt.cost_usd_equivalent is None
    assert receipt.event_count == 0

    inconsistent = RunnerReceipt.from_dict({
        "settled": True, "recovery_required": True,
        "usage": {"input_tokens": float("inf"), "output_tokens": 1},
        "usage_known": True,
    })
    assert inconsistent.settled is False
    assert inconsistent.recovery_required is True
    assert inconsistent.usage_known is False


def test_redacted_nested_payload_is_strict_json_even_with_non_finite_numbers():
    value = runner_specs.redact_value({
        "nan": float("nan"), "positive_inf": float("inf"),
        "nested": [float("-inf"), 1],
    })

    assert value == {
        "nan": "[non-finite]", "positive_inf": "[non-finite]",
        "nested": ["[non-finite]", 1],
    }
    assert json.dumps(value, allow_nan=False)


def test_redaction_covers_mapping_keys_and_wire_parsers_are_total():
    secret = "sk-secretkey123456789"
    value = runner_specs.redact_value({secret: {"safe": 1}})

    assert secret not in json.dumps(value)
    assert "[redacted]" in value
    assert RunnerUsage.from_dict("not-a-mapping").known is False

    cyclic = []
    cyclic.append(cyclic)
    assert runner_specs.redact_value(cyclic) == {
        "truncated": True, "reason": "cycle"}


# --- usage ------------------------------------------------------------------
def test_usage_to_collie_codex_cached_subtraction():
    """Codex counts cached bytes inside input_tokens; Collie's input is UNCACHED."""
    usage = usage_to_collie("codex-exec", {
        "input_tokens": 1_000, "cached_input_tokens": 400, "output_tokens": 120,
        "reasoning_output_tokens": 30, "cache_write_input_tokens": 55,
    })
    assert usage.input_tokens == 600            # 1000 - 400, not 1000
    assert usage.cache_read == 400
    assert usage.cache_creation == 55
    assert usage.output_tokens == 120           # already includes reasoning
    assert usage.reasoning_tokens == 30
    assert usage.source == "turn.completed"
    assert usage.known is True
    # The invariant the cache ledger relies on: full input = uncached + read + write.
    assert usage.input_tokens + usage.cache_read == 1_000


def test_usage_to_collie_codex_never_reports_negative_input():
    usage = usage_to_collie("codex-exec", {"input_tokens": 10, "cached_input_tokens": 40,
                                           "output_tokens": 1})
    assert usage.input_tokens == 0


def test_usage_to_collie_codex_accepts_the_token_usage_wrapper():
    usage = usage_to_collie("codex-exec", {"token_usage": {
        "input_tokens": 90, "cached_input_tokens": 10, "output_tokens": 5}})
    assert (usage.input_tokens, usage.cache_read, usage.output_tokens) == (80, 10, 5)


def test_usage_to_collie_claude_maps_same_names_and_reported_cost():
    usage = usage_to_collie("claude-code", {
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 700, "output_tokens": 90,
                  "cache_read_input_tokens": 300, "cache_creation_input_tokens": 40},
    })
    assert usage.input_tokens == 700            # already uncached — no subtraction
    assert usage.cache_read == 300
    assert usage.cache_creation == 40
    assert usage.cost_usd_reported == pytest.approx(0.0123)
    assert usage.source == "result.usage"


def test_unknown_usage_is_none_not_zero():
    """A runner with no usage channel must not look like a run that cost nothing."""
    for key, raw in (("claude-code", {}), ("codex-exec", {}), ("prime-rpc", {"tokens": {"input": 5}}),
                     ("no-such-runner", {"input_tokens": 5, "output_tokens": 5})):
        usage = usage_to_collie(key, raw)
        assert usage.input_tokens is None, key
        assert usage.output_tokens is None, key
        assert usage.cache_read is None, key
        assert usage.cache_creation is None, key
        assert usage.cost_usd_reported is None, key
        assert usage.known is False, key
        assert usage.to_collie_usage() is None, key


def test_known_usage_converts_to_provider_usage():
    usage = usage_to_collie("codex-exec", {"input_tokens": 100, "cached_input_tokens": 40,
                                           "output_tokens": 20})
    collie = usage.to_collie_usage()
    assert (collie.input_tokens, collie.output_tokens, collie.cache_read) == (60, 20, 40)
    # cache_creation was not reported; 0 is correct only because tokens ARE known.
    assert collie.cache_creation == 0


def test_output_alone_is_not_known_usage():
    assert RunnerUsage(output_tokens=10).known is False
    assert RunnerUsage(input_tokens=10).known is False
    assert RunnerUsage(input_tokens=0, output_tokens=0).known is True


def test_equivalent_cost_is_none_when_usage_or_price_is_unknown():
    known = usage_to_collie("codex-exec", {"input_tokens": 100, "cached_input_tokens": 40,
                                           "output_tokens": 20})
    assert equivalent_cost_usd("", known) is None
    assert equivalent_cost_usd("no-such-model-zzz", known) is None
    assert equivalent_cost_usd("sonnet", RunnerUsage()) is None
    assert equivalent_cost_usd("sonnet", known) > 0


# --- billing ----------------------------------------------------------------
def test_billing_mode_of_covers_all_classes():
    assert set(BILLING_MODE_OF) == set(BILLING_CLASSES)
    assert len(BILLING_CLASSES) == 5
    # The four values Mission already persists (missionweb._billing_mode).
    assert set(BILLING_MODE_OF.values()) <= {"subscription", "metered", "local", "unconfigured"}
    assert BILLING_MODE_OF["unknown"] == "unconfigured"
    assert BILLING_MODE_OF["paid_overage"] == "metered"


def test_family_of_provider_groups_by_who_pays():
    for provider in ("claude-agent-sdk", "claude-sdk", "anthropic-oauth", "claude-sub",
                     "claude-cli", "cli", "CLAUDE-CLI"):
        assert family_of_provider(provider) == "claude", provider
    for provider in ("codex-oauth", "codex-sub", "codex"):
        assert family_of_provider(provider) == "codex", provider
    assert family_of_provider("ollama") == "local"
    assert family_of_provider("mock") == "local"
    assert family_of_provider("") == ""
    # An API key is a different payer than the subscription of the same vendor.
    assert family_of_provider("anthropic") == "api:anthropic"
    assert family_of_provider("deepseek") == "api:deepseek"


# --- probes & phases --------------------------------------------------------
def test_probe_usable_requires_install_login_and_implementation():
    assert _codex_probe().usable() is True
    assert _codex_probe(installed=False).usable() is False
    assert _codex_probe(login="expired").usable() is False
    assert _codex_probe(login="not-logged-in").usable() is False
    # collie itself has no separate login
    assert _codex_probe(login="n/a").usable() is True
    assert _codex_probe(detail="not implemented in this phase").usable() is False
    assert _codex_probe().to_dict()["usable"] is True


def test_surface_gate_is_phase_scoped_and_host_owned_surfaces_are_wired():
    assert CURRENT_PHASE == 2
    assert EXTERNAL_ALLOWED_SURFACES[1] == ("run", "web", "pack", "mission-code")
    for phase, allowed in EXTERNAL_ALLOWED_SURFACES.items():
        assert set(allowed) <= set(SURFACES), phase
        assert set(EXTERNAL_ALLOWED_SURFACES[1]) <= set(allowed), phase


# --- canonical events -------------------------------------------------------
def test_canonical_types_whitelist_is_closed():
    assert CANONICAL_TYPES == frozenset({
        "session.started", "session.resumed", "session.state",
        "turn.started", "turn.yielded", "turn.failed", "turn.cancelled",
        "tool.started", "tool.completed", "file.changed", "message.completed",
        "approval.requested", "approval.resolved",
        "interaction.requested", "interaction.resolved", "message.accepted",
        "context.compacted",
        "usage.updated", "rate_limit.reached", "auth.required", "runner.error"})
    # deliberately not part of the vocabulary: Codex item.* and a separate
    # protocol error type (framing failures are runner.error like everything else)
    assert not any(t.startswith("item.") for t in CANONICAL_TYPES)
    assert "protocol.error" not in CANONICAL_TYPES


def test_every_canonical_type_constructs_and_unknown_ones_raise():
    for name in sorted(CANONICAL_TYPES):
        event = CanonicalEvent(cursor=1, type=name, native_type="x", payload={},
                               at=0.0, runner="codex-exec")
        assert event.type == name
    for bogus in ("item.completed", "protocol.error", "", "turn.done"):
        with pytest.raises(RunnerProtocolError):
            CanonicalEvent(cursor=1, type=bogus, native_type=bogus, payload={},
                           at=0.0, runner="codex-exec")


def test_event_payload_is_bounded():
    event = CanonicalEvent(cursor=1, type="tool.completed", native_type="item.completed",
                           payload={"out": "x" * 50_000}, at=0.0, runner="codex-exec")
    encoded = json.dumps(event.to_dict())
    assert len(encoded) < 20_000
    assert event.payload.get("truncated") is True


# --- request candidates -----------------------------------------------------
def _request(**over):
    values = dict(surface="run", intent="build", route_kind="code",
                  needs=frozenset({"code"}), workspace="current")
    values.update(over)
    return HarnessRequest(**values)


def test_candidates_default_is_collie_only():
    assert _request().candidates() == ("collie",)


def test_candidates_pin_beats_configured_and_pool():
    req = _request(pin="claude-code", configured="auto", pool=("codex-exec", "collie"))
    assert req.candidates() == ("claude-code",)


def test_candidates_explicit_configured_narrows_to_one():
    assert _request(configured="codex-exec", pool=("collie",)).candidates() == ("codex-exec",)


def test_candidates_auto_uses_pool_order_and_always_keeps_collie():
    req = _request(configured="auto", pool=("codex-exec", "claude-code"))
    assert req.candidates() == ("codex-exec", "claude-code", "collie")
    # pool order is the operator's preference order, and duplicates collapse
    req = _request(configured="auto", pool=("collie", "codex-exec", "codex-exec", ""))
    assert req.candidates() == ("collie", "codex-exec")


# --- snapshot projection ----------------------------------------------------
def _snapshot(**over):
    values = dict(runner="codex-exec", workspace="C:/repo", thread_id="t-1",
                  usage={"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20,
                         "cache_write_input_tokens": 5},
                  settled=True, invocation=1, started_at=100.0, finished_at=102.5,
                  final_output="done", mutated=True, mutation_check_complete=True)
    values.update(over)
    return RunnerSnapshot(**values)


def test_snapshot_to_run_result_uses_runner_key_and_normalized_usage():
    res = snapshot_to_run_result(_snapshot(), _codex_spec(), _codex_probe(),
                                 model="gpt-5.6-terra")
    assert res.harness == "codex-exec"
    assert res.provider == "codex"
    assert (res.input_tokens, res.cache_read, res.cache_creation) == (60, 40, 5)
    assert res.output_tokens == 20
    assert res.total_tokens == 125
    assert res.wall_ms == 2_500
    assert res.turns == 1
    assert res.success is True
    assert res.answer == "done"
    assert res.messages == []
    assert res.cost_usd > 0


def test_snapshot_to_run_result_settled_is_never_verified():
    res = snapshot_to_run_result(_snapshot(settled=True), _codex_spec(), _codex_probe())
    assert res.success is True
    # Only the host verifier that runs after the runner exits may set this.
    assert res.verified is False


def test_snapshot_to_run_result_leaves_unknown_usage_null_not_zero():
    spec = HarnessSpec(key="claude-code", label="Claude Code", kind="external",
                       credential_family="claude",
                       caps=RunnerCapabilities(protocol="claude-print-json"))
    probe = RunnerProbe(key="claude-code", installed=True, login="ok")
    res = snapshot_to_run_result(RunnerSnapshot(runner="claude-code", workspace="C:/repo"),
                                 spec, probe, model="sonnet")
    for field_name in ("input_tokens", "output_tokens", "cache_read", "cache_creation",
                       "total_tokens", "cost_usd"):
        assert getattr(res, field_name) is None, field_name


def test_snapshot_to_run_result_redacts_the_error_and_fails_the_run():
    snap = _snapshot(settled=False, error="auth failed: %s" % SECRETS[0], final_output="")
    res = snapshot_to_run_result(snap, _codex_spec(), _codex_probe())
    assert not SECRET_RE.search(res.error)
    assert res.success is False


def test_snapshot_to_run_result_carries_the_stop_into_the_run_result():
    """A stopped worker turn must not be projected as a finished one.

    ``RunnerSnapshot.cancelled`` is the only record that a stop happened; the
    error string is not.  An App Server ``turn/completed`` that races
    ``turn/interrupt`` leaves a snapshot with ``cancelled=True`` and no error at
    all, which ``run_stop_reason`` used to read as "completed" — the one verdict
    a stopped run may never be reported under.
    """
    from harness.recorder import run_outcome, run_stop_reason

    clean_terminal = _snapshot(settled=False, cancelled=True, error="",
                               exit_code=0, final_output="partial work")
    assert clean_terminal.terminal_state == "cancelled"
    res = snapshot_to_run_result(clean_terminal, _codex_spec(), _codex_probe())
    assert res.canceled is True
    assert res.stop_reason == "canceled"
    assert res.success is False
    assert run_stop_reason(res) == "canceled"
    assert run_outcome(res)["completed"] is False

    # The ordinary process-tree cancel does carry an error, and it too has to
    # read as a stop rather than as an unexplained failure.
    killed = _snapshot(settled=False, cancelled=True, exit_code=None,
                       error="Codex turn was cancelled")
    killed_res = snapshot_to_run_result(killed, _codex_spec(), _codex_probe())
    assert (killed_res.canceled, run_stop_reason(killed_res)) == (True, "canceled")

    # A turn nobody stopped is untouched: the verdict stays with run_stop_reason.
    settled = snapshot_to_run_result(_snapshot(), _codex_spec(), _codex_probe())
    assert (settled.canceled, settled.stop_reason) == (False, "")
    assert run_stop_reason(settled) == "completed"
    failed = snapshot_to_run_result(_snapshot(settled=False, error="boom"),
                                    _codex_spec(), _codex_probe())
    assert (failed.canceled, run_stop_reason(failed)) == (False, "error")


def test_snapshot_to_run_result_refuses_a_mismatched_probe_or_decision():
    with pytest.raises(RunnerError):
        snapshot_to_run_result(_snapshot(), _codex_spec(),
                               _codex_probe(key="claude-code"))
    other = HarnessDecision(
        runner="claude-code", source="user", credential_family="claude",
        billing_class="subscription_allowance", billing_mode="subscription",
        reasons=(), rejected={}, candidates=(), fallback_chain=(), probe={}, probe_digest="")
    with pytest.raises(RunnerError):
        snapshot_to_run_result(_snapshot(), _codex_spec(), _codex_probe(), decision=other)


# --- digest -----------------------------------------------------------------
def test_stable_digest_ignores_key_order_and_changes_with_content():
    assert stable_digest({"a": 1, "b": 2}) == stable_digest({"b": 2, "a": 1})
    assert stable_digest({"a": 1}) != stable_digest({"a": 2})
    assert len(stable_digest({"a": 1})) == 64
