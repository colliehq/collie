"""Hard rules, soft ranking and request building for harness selection.

Everything here is a pure-function test: no CLI is executed, no probe is taken,
no file is read.  That is the point of ``runner_select.decide`` — the same
request plus the same evidence must produce the same decision on any machine,
including one replaying a receipt.
"""
from __future__ import annotations

import argparse
import json
import os
import re

from harness import runner_select
from harness.runner_specs import (
    HarnessRequest,
    HarnessSpec,
    RunnerCapabilities,
    RunnerProbe,
)


NOW = 1_700_000_000.0

_ALL_TOOLS = frozenset({"code", "bash", "browser", "desktop", "mcp",
                        "web_search", "email", "slack"})


class _Settings:
    """Stand-in for the settings module: only ``get`` is used."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)


def _collie_spec(**over) -> HarnessSpec:
    caps = RunnerCapabilities(
        protocol="collie-native", tools=_ALL_TOOLS, session_resume=True, steer=True,
        cancel="native+process-tree", usage_tokens=True, usage_cost=True,
        approval_round_trip=True, request_gate=True, needs_git_workspace=False,
        windows_native=True, confinement="none")
    base = dict(key="collie", label="Collie's own harness", kind="native",
                credential_family="", caps=caps, env_policy="native", phase=1)
    base.update(over)
    return HarnessSpec(**base)


def _claude_spec(**over) -> HarnessSpec:
    caps = RunnerCapabilities(
        protocol="claude-print-json", tools=frozenset({"code"}), session_resume=True,
        usage_tokens=True, usage_cost=True, confinement="tools-allowlist",
        windows_native=True)
    base = dict(key="claude-code", label="Claude Code", kind="external",
                binary="claude", credential_family="claude", caps=caps,
                env_policy="claude", guard_alias="claude-code", min_version="2.1.0",
                phase=1)
    base.update(over)
    return HarnessSpec(**base)


def _codex_spec(**over) -> HarnessSpec:
    caps = RunnerCapabilities(
        protocol="codex-exec-jsonl", tools=frozenset({"code", "bash"}),
        session_resume=True, usage_tokens=True, confinement="workspace-write",
        windows_native=True)
    base = dict(key="codex-exec", label="Codex CLI", kind="external", binary="codex",
                credential_family="codex", caps=caps, env_policy="codex",
                guard_alias="codex-cli", min_version="0.149.0", phase=1)
    base.update(over)
    return HarnessSpec(**base)


def _probe(key, **over) -> RunnerProbe:
    base = dict(key=key, installed=True, login="ok", version="9.9.9",
                billing_class="subscription_allowance", billing_mode="subscription",
                overage_attested=True,
                billing_evidence={"source": "claude auth status", "plan": "max",
                                  "observed_at": NOW - 60},
                probed_at=NOW)
    if key == "collie":
        base["login"] = "n/a"
    base.update(over)
    return RunnerProbe(**base)


def _specs(*specs) -> dict:
    return {spec.key: spec for spec in specs}


def _probes(*probes) -> dict:
    return {probe.key: probe for probe in probes}


def _req(**over) -> HarnessRequest:
    base = dict(surface="run", intent="build", route_kind="code",
                needs=frozenset({"code", "bash"}), workspace="current",
                workspace_is_git=True, provider="anthropic-oauth",
                provider_family="claude", os_name="posix", configured="collie",
                pool=("collie",), phase=1)
    base.update(over)
    return HarnessRequest(**base)


def _decide(req, specs=None, probes=None, **kw):
    specs = specs if specs is not None else _specs(_collie_spec(), _claude_spec(),
                                                   _codex_spec())
    probes = probes if probes is not None else _probes(_probe("collie"),
                                                       _probe("claude-code"),
                                                       _probe("codex-exec"))
    kw.setdefault("now", NOW)
    return runner_select.decide(req, specs, probes, **kw)


# --- H1..H11 ----------------------------------------------------------------
def test_h1_surface_locks_collie():
    # repl re-decides the route every turn; phase 1 only lets `run` delegate.
    decision = _decide(_req(surface="repl", pin="claude-code"))
    assert decision.runner == ""
    assert decision.rejected["claude-code"].startswith("H1:")


def test_h1_allows_run_surface():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code"))
    assert decision.runner == "claude-code"
    assert decision.rejected == {}


def test_h2_plan_intent_locks_collie():
    decision = _decide(_req(intent="plan", needs=frozenset({"code"}),
                            pin="claude-code"))
    assert decision.rejected["claude-code"].startswith("H2:")


def test_h2_chat_route_locks_collie():
    decision = _decide(_req(route_kind="chat", needs=frozenset({"code"}),
                            pin="claude-code"))
    assert decision.rejected["claude-code"].startswith("H2:")


def test_h3_probe_unusable_is_rejected():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code"),
                       probes=_probes(_probe("collie"),
                                      _probe("claude-code", installed=False,
                                             detail="not installed")))
    assert decision.rejected["claude-code"] == "H3: not installed"
    assert decision.runner == ""


def test_h3_phase_gate_rejects_future_runner():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code"),
                       specs=_specs(_collie_spec(), _claude_spec(phase=3)))
    assert "phase 3" in decision.rejected["claude-code"]


def test_h3_min_version_rejects_old_binary():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code"),
                       probes=_probes(_probe("collie"),
                                      _probe("claude-code", version="1.9.0")))
    assert decision.rejected["claude-code"].startswith("H3: version 1.9.0")


def test_h3_never_applies_the_login_check_to_collie():
    # An unusable-looking collie probe must not take the default path away:
    # collie is in-process, and its provider credential is the guard's business.
    decision = _decide(_req(), probes=_probes(_probe("collie", installed=False,
                                                     login="missing-key")))
    assert decision.runner == "collie"


def test_h4_needs_beyond_declared_tools_rejected():
    decision = _decide(_req(needs=frozenset({"code", "browser"}), pin="claude-code"))
    assert decision.rejected["claude-code"] == "H4: does not provide browser"


def test_h5_unknown_billing_rejected_under_no_paid_overage():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code",
                            no_paid_overage=True),
                       probes=_probes(_probe("collie"),
                                      _probe("claude-code", billing_class="unknown",
                                             billing_mode="unconfigured")))
    assert decision.rejected["claude-code"].startswith("H5: billing class unknown")


def test_h5_subscription_only_requires_route_evidence_not_overage_attestation():
    without_attestation = _decide(
        _req(needs=frozenset({"code"}), pin="claude-code", subscription_only=True),
        probes=_probes(_probe("collie"), _probe("claude-code", overage_attested=False)))
    assert without_attestation.runner == "claude-code"

    without_evidence = _decide(
        _req(needs=frozenset({"code"}), pin="claude-code", subscription_only=True),
        probes=_probes(_probe("collie"), _probe("claude-code", billing_evidence={})))
    assert "no billing evidence" in without_evidence.rejected["claude-code"]

    no_paid_without_attestation = _decide(
        _req(needs=frozenset({"code"}), pin="claude-code", no_paid_overage=True),
        probes=_probes(_probe("collie"), _probe("claude-code", overage_attested=False)))
    assert "attestation" in no_paid_without_attestation.rejected["claude-code"]


def test_h5_codex_evidence_expires():
    stale = _decide(_req(needs=frozenset({"code", "bash"}), pin="codex-exec",
                         no_paid_overage=True),
                    probes=_probes(_probe("collie"),
                                   _probe("codex-exec",
                                          billing_evidence={"source": "file:~/.codex/auth.json",
                                                            "observed_at": NOW - 3600})))
    assert "older than 15 minutes" in stale.rejected["codex-exec"]

    fresh = _decide(_req(needs=frozenset({"code", "bash"}), pin="codex-exec",
                         no_paid_overage=True),
                    probes=_probes(_probe("collie"),
                                   _probe("codex-exec",
                                          billing_evidence={"source": "file:~/.codex/auth.json",
                                                            "observed_at": NOW - 60})))
    assert fresh.runner == "codex-exec"

    malformed_time = _decide(
        _req(needs=frozenset({"code", "bash"}), pin="codex-exec",
             no_paid_overage=True),
        probes=_probes(_probe("collie"), _probe(
            "codex-exec", billing_evidence={"source": "login", "observed_at": float("nan")},
            probed_at=0)))
    assert "older than" in malformed_time.rejected["codex-exec"]


def test_selection_rejects_non_finite_clock():
    import pytest

    with pytest.raises(ValueError, match="selection time"):
        _decide(_req(), now=float("nan"))


def test_h5_token_budget_needs_measurable_usage():
    caps = _claude_spec().caps.to_dict()
    caps["usage_tokens"] = False
    spec = _claude_spec(caps=RunnerCapabilities.from_dict(caps))
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code",
                            max_total_tokens=50_000),
                       specs=_specs(_collie_spec(), spec))
    assert "token budget" in decision.rejected["claude-code"]


def test_h6_empty_workspace_rejects_external():
    decision = _decide(_req(needs=frozenset({"code"}), workspace="", pin="claude-code"))
    assert decision.rejected["claude-code"].startswith("H6:")


def test_h6_non_git_pack_rejected_but_run_only_warns():
    packed = _decide(_req(surface="pack", phase=2, needs=frozenset({"code"}),
                          workspace="isolated", workspace_is_git=False,
                          pin="claude-code"))
    assert "git workspace" in packed.rejected["claude-code"]

    ran = _decide(_req(needs=frozenset({"code"}), workspace_is_git=False,
                       pin="claude-code"))
    assert ran.runner == "claude-code"
    assert any("not a git repository" in line for line in ran.reasons)


def test_h7_is_skipped_when_no_signals_are_supplied():
    class _Signals:
        auth_status = "expired"
        recent_429 = 0
        cooldown_until = None
        quota = None
        history = None
        mission_budget = None
        rate_limit = None

        def to_dict(self):
            return {"auth_status": self.auth_status}

    req = _req(needs=frozenset({"code"}), pin="claude-code")
    assert _decide(req).runner == "claude-code"                     # phase 1: no signals
    blocked = _decide(req, signals=_Signals())
    assert blocked.rejected["claude-code"] == "H7: auth status is expired"


def test_h7_cooldown_rejects_auto_but_not_a_named_runner():
    class _Signals:
        auth_status = "ok"
        recent_429 = 5
        cooldown_until = NOW + 300
        quota = None
        history = None
        mission_budget = None
        rate_limit = None

        def to_dict(self):
            return {"recent_429": self.recent_429}

    auto = _decide(_req(needs=frozenset({"code"}), configured="auto",
                        pool=("claude-code", "collie")), signals=_Signals())
    assert auto.runner == "collie"
    assert auto.rejected["claude-code"].startswith("H7:")

    pinned = _decide(_req(needs=frozenset({"code"}), pin="claude-code"),
                     signals=_Signals())
    assert pinned.runner == "claude-code"


def test_h7_signal_set_is_route_specific_in_auto_selection():
    from harness.runner_signals import (QuotaSnapshot, RateLimitWindow,
                                        RouteSignals, SignalSet)

    signals = SignalSet({
        "codex-exec": RouteSignals(
            runner="codex-exec", auth_status="ok",
            quota=QuotaSnapshot(primary=RateLimitWindow(95), observed_at=NOW)),
        "collie": RouteSignals(runner="collie", auth_status="ok"),
    })
    decision = _decide(
        _req(needs=frozenset({"code"}), configured="auto",
             pool=("codex-exec", "collie")), signals=signals)

    assert decision.runner == "collie"
    assert "quota is 95% used" in decision.rejected["codex-exec"]
    assert decision.signals_digest == signals.digest()


def test_h7_malformed_duck_typed_signals_degrade_to_unknown_without_crashing():
    class _Window:
        used_percent = "not-a-percent"

    class _Quota:
        primary = _Window()
        secondary = None
        rate_limit_reached_type = ""

    class _History:
        runs = "many"
        verified = object()

    class _RateLimit:
        observed_at = "yesterday-ish"

    class _Signals:
        auth_status = "ok"
        recent_429 = "several"
        cooldown_until = "later"
        quota_guard = "most"
        quota = _Quota()
        history = _History()
        rate_limit = _RateLimit()
        mission_budget = None

        def digest(self):
            raise ValueError("observer broke")

    decision = _decide(
        _req(needs=frozenset({"code"}), configured="auto",
             pool=("claude-code", "collie")), signals=_Signals())

    assert decision.runner in {"claude-code", "collie"}
    assert len(decision.signals_digest) == 64
    assert any("quota: unknown" in score.soft_reasons
               for score in decision.candidates if score.eligible)


def test_h7_untrusted_signal_numbers_are_clamped_to_safe_scoring_ranges():
    class _Window:
        used_percent = 999999

    class _Quota:
        primary = _Window()
        secondary = None
        rate_limit_reached_type = ""

    class _History:
        runs = 10
        verified = 10_000

    class _Signals:
        auth_status = "ok"
        recent_429 = -20
        cooldown_until = float("inf")
        quota_guard = -1
        quota = _Quota()
        history = _History()
        rate_limit = None
        mission_budget = None

        def to_dict(self):
            return {}

    decision = _decide(
        _req(needs=frozenset({"code"}), configured="auto",
             pool=("claude-code", "collie")), signals=_Signals())

    assert decision.runner == "collie"
    assert "quota is 100% used (guard 90%)" in decision.rejected["claude-code"]


def test_h8_overnight_locks_collie():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code",
                            overnight=True))
    assert decision.rejected["claude-code"].startswith("H8: overnight")


def test_h8_mission_slice_requires_session_resume():
    caps = _codex_spec().caps.to_dict()
    caps["session_resume"] = False
    spec = _codex_spec(caps=RunnerCapabilities.from_dict(caps))
    decision = _decide(_req(surface="mission-code", phase=2, workspace="mission",
                            pin="codex-exec"),
                       specs=_specs(_collie_spec(), spec))
    assert "session_resume" in decision.rejected["codex-exec"]


def test_h9_bash_without_confinement_or_round_trip_rejected():
    caps = _codex_spec().caps.to_dict()
    caps["confinement"] = "none"
    spec = _codex_spec(caps=RunnerCapabilities.from_dict(caps))
    decision = _decide(_req(pin="codex-exec"), specs=_specs(_collie_spec(), spec))
    assert decision.rejected["codex-exec"].startswith("H9:")


def test_h9_interactive_approval_rejects_external():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code",
                            gate_mode="interactive", has_approver=True))
    assert "interactive approval" in decision.rejected["claude-code"]


def test_h10_windows_unverified_rejects_auto_and_warns_a_pin():
    caps = _claude_spec().caps.to_dict()
    caps["windows_native"] = None
    spec = _claude_spec(caps=RunnerCapabilities.from_dict(caps))
    specs = _specs(_collie_spec(), spec)

    auto = _decide(_req(needs=frozenset({"code"}), os_name="nt", configured="auto",
                        pool=("claude-code", "collie")), specs=specs)
    assert auto.runner == "collie"
    assert auto.rejected["claude-code"].startswith("H10:")

    pinned = _decide(_req(needs=frozenset({"code"}), os_name="nt", pin="claude-code"),
                     specs=specs)
    assert pinned.runner == "claude-code"
    assert any("unverified on this host" in line for line in pinned.reasons)


def test_h10_windows_known_bad_rejects_even_a_pin():
    caps = _claude_spec().caps.to_dict()
    caps["windows_native"] = False
    spec = _claude_spec(caps=RunnerCapabilities.from_dict(caps))
    decision = _decide(_req(needs=frozenset({"code"}), os_name="nt", pin="claude-code"),
                       specs=_specs(_collie_spec(), spec))
    assert decision.rejected["claude-code"] == "H10: not supported natively on Windows"


def test_h11_double_control_must_be_proven_off():
    caps = _codex_spec().caps.to_dict()
    caps["native_goal"] = True
    spec = _codex_spec(caps=RunnerCapabilities.from_dict(caps))
    specs = _specs(_collie_spec(), spec)

    unproven = _decide(_req(pin="codex-exec"), specs=specs)
    assert unproven.rejected["codex-exec"].startswith("H11:")

    proven = _decide(_req(pin="codex-exec"), specs=specs,
                     probes=_probes(_probe("collie"),
                                    _probe("codex-exec",
                                           capabilities={"double_control": "PASS"})))
    assert proven.runner == "codex-exec"


# --- D.2 sources ------------------------------------------------------------
def test_source_user_for_an_explicit_runner():
    assert _decide(_req(needs=frozenset({"code"}), pin="claude-code")).source == "user"


def test_source_roster_for_a_pack_member():
    decision = _decide(_req(surface="pack", phase=2, needs=frozenset({"code"}),
                            workspace="isolated", pin="claude-code"))
    assert decision.source == "roster"


def test_source_configured_for_the_runner_setting():
    decision = _decide(_req(needs=frozenset({"code"}), configured="claude-code"))
    assert decision.source == "configured"
    assert decision.runner == "claude-code"


def test_source_task_policy_when_auto_ranks_candidates():
    decision = _decide(_req(needs=frozenset({"code"}), configured="auto",
                            pool=("claude-code", "collie")))
    assert decision.source == "task-policy"


def test_source_safety_default_when_only_collie_survives():
    decision = _decide(_req(needs=frozenset({"code", "browser"}), configured="auto",
                            pool=("claude-code", "collie")))
    assert decision.runner == "collie"
    assert decision.source == "safety-default"


# --- fail-closed vs fall back ----------------------------------------------
def test_pin_unavailable_is_error_not_fallback():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code"),
                       probes=_probes(_probe("collie"),
                                      _probe("claude-code", installed=False,
                                             detail="not installed")))
    assert decision.runner == ""
    assert decision.fallback_chain == ()
    assert "never swapped" in decision.error
    assert "claude-code" in decision.error


def test_configured_unavailable_is_error_too():
    decision = _decide(_req(needs=frozenset({"code"}), configured="claude-code"),
                       probes=_probes(_probe("collie"),
                                      _probe("claude-code", login="expired",
                                             detail="login is expired")))
    assert decision.runner == ""
    assert decision.source == "configured"
    assert decision.error


def test_auto_empty_never_overrides_hard_billing_rules():
    # Everything, Collie included, is rejected. Auto ranks only eligible
    # routes; it cannot waive the user's no-paid-overage promise.
    decision = _decide(_req(needs=frozenset({"code"}), configured="auto",
                            pool=("codex-exec", "collie"), no_paid_overage=True),
                       probes=_probes(_probe("collie", billing_class="unknown"),
                                      _probe("codex-exec", billing_class="unknown")))
    assert decision.runner == ""
    assert decision.source == "safety-default"
    assert "codex-exec" in decision.rejected
    assert "collie" in decision.rejected
    assert "no eligible worker" in decision.error


def test_fallback_chain_same_family_same_billing():
    decision = _decide(_req(needs=frozenset({"code"}), configured="auto",
                            pool=("claude-code", "codex-exec", "collie")))
    assert decision.runner == "claude-code"
    assert decision.credential_family == "claude"
    # collie runs on the same Claude subscription here; codex-exec is a different
    # payer entirely and must never be a silent substitute.
    assert "collie" in decision.fallback_chain
    assert "codex-exec" not in decision.fallback_chain
    assert any("fallback chain" in line for line in decision.reasons)


def test_pin_never_gets_a_fallback_chain():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code"))
    assert decision.fallback_chain == ()


# --- D.4 soft ranking -------------------------------------------------------
def test_soft_sort_follows_pool_order():
    caps = _claude_spec().caps.to_dict()
    caps["steer"] = True
    spec = _claude_spec(caps=RunnerCapabilities.from_dict(caps))
    specs = _specs(_collie_spec(), spec)

    external_first = _decide(_req(needs=frozenset({"code"}), configured="auto",
                                  pool=("claude-code", "collie")), specs=specs)
    assert external_first.runner == "claude-code"

    collie_first = _decide(_req(needs=frozenset({"code"}), configured="auto",
                                pool=("collie", "claude-code")), specs=specs)
    assert collie_first.runner == "collie"


def test_unknown_quota_scores_zero_and_says_so():
    decision = _decide(_req(needs=frozenset({"code"}), configured="auto",
                            pool=("claude-code", "collie")))
    chosen = [item for item in decision.candidates if item.key == decision.runner][0]
    assert chosen.terms["headroom"] == 0.0
    assert "quota: unknown" in decision.reasons


def test_history_term_uses_laplace_smoothing():
    class _History:
        runs = 8
        success = 6
        verified = 5
        errors = 2
        retry_429 = 0
        avg_cost_usd = None
        avg_wall_ms = 0

    class _Signals:
        auth_status = "ok"
        recent_429 = 0
        cooldown_until = None
        quota = None
        history = _History()
        mission_budget = None
        rate_limit = None

        def to_dict(self):
            return {"history": {"runs": 8, "verified": 5}}

    decision = _decide(_req(needs=frozenset({"code"}), configured="auto",
                            pool=("claude-code", "collie")), signals=_Signals())
    chosen = [item for item in decision.candidates if item.key == decision.runner][0]
    assert abs(chosen.terms["history"] - (5 + 1) / (8 + 2)) < 1e-9
    assert decision.signals_digest


def test_weights_sum_to_one():
    assert abs(sum(runner_select._WEIGHTS.values()) - 1.0) < 1e-9


def test_single_candidate_is_not_scored():
    # RUNNER=collie must not pay for a ranking it cannot use.
    decision = _decide(_req())
    assert decision.candidates[0].terms == {}


# --- shape, purity, explanation ---------------------------------------------
def test_decision_is_json_serializable():
    decision = _decide(_req(needs=frozenset({"code"}), configured="auto",
                            pool=("claude-code", "collie")))
    encoded = json.dumps(decision.to_dict(), ensure_ascii=False)
    assert json.loads(encoded)["runner"] == decision.runner
    # Same secret shapes tests/test_runner_specs.py greps for, as word-anchored
    # patterns — a plain "sk-" substring matches the word "ta-sk-policy" itself.
    secrets = re.compile(r"\b(?:sk|sess)-[A-Za-z0-9_-]{4,}|\bghp_[A-Za-z0-9]{8,}"
                         r"|\bAKIA[0-9A-Z]{8,}|\beyJ[A-Za-z0-9_-]{6,}"
                         r"|(?i:bearer\s+\S+)")
    assert secrets.search(encoded) is None


def test_decide_is_a_pure_function():
    req = _req(needs=frozenset({"code"}), configured="auto",
               pool=("claude-code", "collie"))
    first = _decide(req).to_dict()
    second = _decide(req).to_dict()
    assert first == second


def test_reason_names_family_and_billing():
    decision = _decide(_req(needs=frozenset({"code"}), pin="claude-code"))
    assert decision.reasons[0] == ("runner: claude-code (user); family=claude "
                                   "billing=subscription_allowance")


def test_explain_lists_reasons_scores_and_errors():
    ranked = runner_select.explain(_decide(_req(needs=frozenset({"code"}),
                                                configured="auto",
                                                pool=("claude-code", "collie"))))
    assert any(line.startswith("runner: ") for line in ranked)
    assert any(line.startswith("runner scores: ") for line in ranked)

    refused = runner_select.explain(
        _decide(_req(needs=frozenset({"code"}), pin="claude-code"),
                probes=_probes(_probe("collie"),
                               _probe("claude-code", installed=False,
                                      detail="not installed"))))
    assert any(line.startswith("runner error: ") for line in refused)


# --- request_from_run -------------------------------------------------------
class _Decision:
    """The fields runner_select reads off router.RunDecision."""

    provider = "anthropic-oauth"
    model = "claude-sonnet-5"
    intent = "build"
    route_kind = "code"
    workspace = "current"
    strategy = "single"


def _args(**over):
    base = dict(runner=None, web_search=False, mode=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_default_runner_collie_skips_probe(tmp_path):
    """The untouched configuration must cost exactly nothing.

    ``candidates()`` returning only ``collie`` is what lets ``cmd_run`` skip
    ``probe_all`` entirely, so no external CLI is ever executed on the default
    path.  ``decide`` then works with an empty probe map.
    """
    req = runner_select.request_from_run(
        _args(), _Decision(), _Settings({"RUNNER": "collie", "RUNNER_POOL": "collie"}),
        cwd=str(tmp_path), has_approver=False)
    assert req.candidates() == ("collie",)
    assert req.pin == ""

    # An empty probe map is enough: nothing external is looked at, let alone run.
    decision = runner_select.decide(req, _specs(_collie_spec()), {}, now=NOW)
    assert decision.runner == "collie"
    assert decision.source == "configured"


def test_request_from_run_pins_the_cli_flag(tmp_path):
    req = runner_select.request_from_run(
        _args(runner="claude-code"), _Decision(), _Settings({"RUNNER": "collie"}),
        cwd=str(tmp_path), has_approver=True)
    assert req.pin == "claude-code"
    assert req.candidates() == ("claude-code",)
    assert req.has_approver is True


def test_request_from_run_auto_opens_the_pool(tmp_path):
    req = runner_select.request_from_run(
        _args(runner="auto"), _Decision(),
        _Settings({"RUNNER": "collie", "RUNNER_POOL": "claude-code, codex-exec"}),
        cwd=str(tmp_path))
    assert req.pin == ""
    assert req.configured == "auto"
    assert req.pool == ("claude-code", "codex-exec")
    assert req.candidates() == ("claude-code", "codex-exec", "collie")


def test_request_from_run_carries_needs_budgets_and_gate(tmp_path):
    (tmp_path / ".git").mkdir()
    args = _args(web_search=True, mode="interactive")
    req = runner_select.request_from_run(
        args, _Decision(),
        _Settings({"MAX_COST": "2.50", "MAX_TOTAL_TOKENS": "60000"}),
        cwd=str(tmp_path), has_approver=True)
    # No "bash": the host runs the verification command after the worker stops,
    # so a build only needs an editor.  Requiring a shell unconditionally used to
    # make H4 reject `claude-code` for every possible `collie run` invocation.
    assert req.needs == frozenset({"code", "web_search"})
    assert req.workspace_is_git is True
    assert req.max_cost_usd == 2.5
    assert req.max_total_tokens == 60000
    assert req.gate_mode == "interactive"
    assert req.surface == "run"
    assert req.provider_family == "claude"
    assert req.os_name == os.name


def test_test_intent_is_the_only_one_that_demands_a_shell(tmp_path):
    """`test` means "run the checks"; everything else is file work.

    The distinction decides whether a shell-less worker such as `claude-code` is
    eligible at all, so it is asserted from the same entry point `collie run`
    uses rather than by constructing needs by hand.
    """
    (tmp_path / ".git").mkdir()
    build = _Decision()
    build.intent = "build"
    assert "bash" not in runner_select.request_from_run(
        _args(), build, _Settings(), cwd=str(tmp_path)).needs

    checks = _Decision()
    checks.intent = "test"
    assert "bash" in runner_select.request_from_run(
        _args(), checks, _Settings(), cwd=str(tmp_path)).needs


def test_claude_code_is_selectable_for_an_ordinary_build(tmp_path):
    """The shape `collie run --runner claude-code` really produces must pass H4."""
    (tmp_path / ".git").mkdir()
    req = runner_select.request_from_run(
        _args(runner="claude-code"), _Decision(), _Settings(),
        cwd=str(tmp_path), has_approver=True)
    decision = _decide(req)
    assert decision.runner == "claude-code", decision.rejected
    assert decision.error == ""


def test_request_from_run_gate_mode_follows_a_read_only_intent(tmp_path):
    decision = _Decision()
    decision.intent = "plan"
    req = runner_select.request_from_run(_args(mode="project"), decision,
                                         _Settings(), cwd=str(tmp_path))
    assert req.gate_mode == "plan"
    assert req.intent == "plan"


def test_request_from_run_treats_zero_budgets_as_no_limit(tmp_path):
    req = runner_select.request_from_run(
        _args(), _Decision(), _Settings({"MAX_COST": "0", "MAX_TOTAL_TOKENS": "0"}),
        cwd=str(tmp_path))
    assert req.max_cost_usd is None
    assert req.max_total_tokens is None


def test_request_from_web_pins_worker_and_uses_same_selector_contract(tmp_path):
    (tmp_path / ".git").mkdir()
    req = runner_select.request_from_surface(
        "web", "claude-code", _Decision(),
        _Settings({"RUNNER": "collie", "RUNNER_POOL": "codex-exec,collie"}),
        cwd=str(tmp_path), has_approver=True)
    assert req.surface == "web"
    assert req.pin == "claude-code"
    assert req.candidates() == ("claude-code",)
    assert req.needs == frozenset({"code"})
    assert _decide(req).runner == "claude-code"


def test_request_from_mission_carries_durable_and_billing_boundaries(tmp_path):
    (tmp_path / ".git").mkdir()
    req = runner_select.request_from_surface(
        "mission-code", "auto", _Decision(),
        _Settings({"RUNNER_POOL": "claude-code,collie"}), cwd=str(tmp_path),
        no_paid_overage=True, subscription_only=True)
    assert req.workspace == "mission"
    assert req.configured == "auto" and req.pin == ""
    assert req.no_paid_overage is True and req.subscription_only is True


def test_request_builder_treats_non_finite_budget_settings_as_unset(tmp_path):
    req = runner_select.request_from_surface(
        "web", "", _Decision(),
        _Settings({"MAX_COST": "Infinity", "MAX_TOTAL_TOKENS": "NaN"}),
        cwd=str(tmp_path))

    assert req.max_cost_usd is None
    assert req.max_total_tokens is None


def test_request_from_pack_records_the_private_git_workspace(tmp_path):
    """Pack copies any source tree, then creates a private git baseline for the worker.

    Eligibility must describe that guaranteed candidate workspace.  Looking for
    ``.git`` in the source would reject an ordinary folder even though the worker
    never receives that folder directly.
    """
    req = runner_select.request_from_surface(
        "pack", "codex-exec", _Decision(), _Settings(), cwd=str(tmp_path))
    assert req.workspace == "isolated"
    assert req.workspace_is_git is True
    assert req.surface == "pack"
