"""Who paid for a finished Web run, as the terminal `done` frame reports it.

The desktop run card prints "plan" instead of a currency amount when `done`
carries `subscription: true`, because a subscription route's cost number is an
API-equivalent estimate rather than an observed bill.  Native and pack runs
built that flag from a hand-written five-name tuple that never listed
`claude-agent-sdk` — the official Claude Agent SDK route, the one a Claude plan
login actually runs on — nor the `claude-sdk`/`claude-sub`/`cli` aliases.  A run
on a checked subscription login therefore came back `subscription: false` and the
card showed an estimate as if it were the charge.

What is pinned here is route CLASSIFICATION only, from a provider name through
the canonical `runner_registry.collie_billing_class`: paid, local and unknown
routes stay unlabeled, the flag never turns a cost estimate into a claimed $0
bill, and the route that actually ran wins over the one Settings held when the
request was accepted.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness import cli, runner_registry, runner_select, sessions, settings, webapp
from harness.recorder import RunResult
from harness.runner_specs import HarnessDecision, RunnerProbe

# Every name `providers.make_provider` routes to a Claude/Codex plan login.
PLAN_ROUTES = ["claude-agent-sdk", "claude-sdk", "anthropic-oauth", "claude-sub",
               "claude-cli", "cli", "codex-oauth", "codex-sub", "codex"]
# Negative controls: an API key pays for the first three, the host pays for the
# local pair, and a name nothing classifies stays unknown rather than becoming a plan.
METERED_ROUTES = ["anthropic", "openai", "deepseek"]
LOCAL_ROUTES = ["mock", "ollama"]
UNKNOWN_ROUTES = ["some-plugin-brain"]


def _isolate(monkeypatch, tmp_path, provider):
    """One Web run, on `provider`, with its own state and no live anything."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: provider)
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(webapp.Handler, "_notify_done", lambda *a, **kw: None)
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear()
        webapp.Handler._cancel_events.clear()


class _FakeHarness:
    """Collie's native harness as the Web surface uses it — no model, no provider call."""

    def __init__(self, gate=None, *, provider_name=None, result_provider=""):
        self.gate = gate
        self.composer = SimpleNamespace(identity="")
        self.memory = self.recorder = SimpleNamespace(close=lambda: None)
        self.mode = "act"; self.force_edit = True; self.max_turns = 20
        self._max_turns_hard_cap = None
        self.self_verify = False; self.verify_max = 2
        self.verify_gate = False; self.require_assert = False
        self._result_provider = result_provider
        if provider_name is not None:
            # The resolved provider OBJECT, which is what `actual_speed` already reads.
            self.provider = SimpleNamespace(name=provider_name, actual_speed="standard",
                                            model="mock-planner-v1")

    def run(self, task_id, message, history=None, **kwargs):
        return RunResult(
            task_id=task_id, harness="collie", provider=self._result_provider,
            model="mock-planner-v1", input_tokens=10, output_tokens=5, total_tokens=15,
            turns=1, wall_ms=12, success=True, verified=False, cost_usd=0.42,
            answer="done", error="",
            messages=[{"role": "user", "content": message},
                      {"role": "assistant", "content": "done"}])


def _run_native(monkeypatch, tmp_path, provider, *, session="sub-native",
                harness_provider=None, result_provider=None):
    """Drive one native Web turn and return its terminal `done` payload."""
    _isolate(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(
        cli, "make_harness",
        lambda *a, **kw: _FakeHarness(
            kw.get("gate"),
            provider_name=(provider if harness_provider is None else harness_provider),
            result_provider=(provider if result_provider is None else result_provider)))
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    webapp.Handler._serve_stream(fake, {
        "q": ["summarise the module"], "session": [session],
        "intent": ["build"], "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"],
    })
    done = next(data for kind, data in reversed(events) if kind == "done")
    assert not done.get("error"), done
    return done


def _run_pack(monkeypatch, tmp_path, provider, attempts, *, session="sub-pack"):
    """Drive one pack Web turn over a staged pack result and return its `done`."""
    from harness import pack as _pack

    _isolate(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(cli, "make_harness", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("a staged pack must not construct a harness")))
    monkeypatch.setattr(_pack, "run_pack", lambda *a, **kw: {
        "winner": 0, "answer": "winner", "reason": "verified", "applied": False,
        "attempts": attempts, "n": len(attempts), "total_cost_usd": 0.42,
        "canceled": False, "apply_error": ""})
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    webapp.Handler._serve_stream(fake, {
        "q": ["summarise the module"], "session": [session], "strategy": ["pack"],
        "intent": ["build"], "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "n": ["2"], "check": ["python -m pytest -q"],
    })
    done = next(data for kind, data in reversed(events) if kind == "done")
    assert not done.get("error"), done
    return done


# --------------------------------------------------------------------------- #
# native single runs

@pytest.mark.parametrize("provider", PLAN_ROUTES)
def test_native_done_labels_every_accepted_plan_route(monkeypatch, tmp_path, provider):
    """Including the SDK route and its aliases, which the old tuple silently missed."""
    done = _run_native(monkeypatch, tmp_path, provider)

    assert done["subscription"] is True
    # The estimate is still reported, and still an estimate: the flag says "do not
    # print this as the bill", never "the marginal charge was observed to be $0".
    assert done["cost_usd"] == 0.42


@pytest.mark.parametrize("provider", METERED_ROUTES + LOCAL_ROUTES + UNKNOWN_ROUTES)
def test_native_done_leaves_paid_local_and_unknown_routes_unlabeled(monkeypatch, tmp_path,
                                                                    provider):
    done = _run_native(monkeypatch, tmp_path, provider)

    assert done["subscription"] is False
    assert done["cost_usd"] == 0.42


def test_the_three_unlabeled_route_classes_stay_distinct():
    """"not a plan route" must not collapse metered, local and unknown into one thing."""
    assert runner_registry.collie_billing_class("anthropic") == "api_metered"
    assert runner_registry.collie_billing_class("ollama") == "local"
    assert runner_registry.collie_billing_class("") == "unknown"
    assert runner_registry.collie_billing_class("claude-agent-sdk") == "subscription_allowance"
    assert webapp._subscription_route("") is False


def test_native_done_follows_the_route_that_actually_ran(monkeypatch, tmp_path):
    """A run that resolved to a metered backend is not a plan run because Settings said so."""
    done = _run_native(monkeypatch, tmp_path, "claude-agent-sdk",
                       harness_provider="anthropic", result_provider="anthropic")

    assert done["subscription"] is False


def test_native_done_reads_the_resolved_provider_object_when_the_result_has_none(
        monkeypatch, tmp_path):
    """The result's own record first, then the harness's provider, then the request's."""
    from_object = _run_native(monkeypatch, tmp_path, "anthropic",
                              harness_provider="claude-agent-sdk", result_provider="")
    from_request = _run_native(monkeypatch, tmp_path, "claude-agent-sdk",
                               harness_provider=None, result_provider="",
                               session="sub-native-request")

    assert from_object["subscription"] is True
    assert from_request["subscription"] is True


def test_native_label_is_a_name_lookup_not_a_credential_read(monkeypatch, tmp_path):
    """Classification reads the canonical table with a provider NAME, and nothing else.

    No login file, no auth-status subprocess, no network preflight is allowed to
    appear in terminal assembly just to decide what the cost tile should say.
    """
    monkeypatch.setattr(runner_registry, "_claude_login_state",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("terminal assembly must not inspect a login")))
    asked = []
    canonical = runner_registry.collie_billing_class

    def spy(provider):
        asked.append(provider)
        return canonical(provider)

    monkeypatch.setattr(runner_registry, "collie_billing_class", spy)

    done = _run_native(monkeypatch, tmp_path, "claude-agent-sdk")

    assert done["subscription"] is True
    assert asked == ["claude-agent-sdk"]


# --------------------------------------------------------------------------- #
# pack runs

@pytest.mark.parametrize("provider,expected", [("claude-agent-sdk", True),
                                               ("claude-sdk", True),
                                               ("codex-sub", True),
                                               ("anthropic", False),
                                               ("mock", False)])
def test_pack_done_classifies_the_route_its_candidates_ran_on(monkeypatch, tmp_path,
                                                              provider, expected):
    done = _run_pack(monkeypatch, tmp_path, provider,
                     [{"idx": 0, "turns": 2, "verified": True, "provider": provider,
                       "cost_usd": 0.2},
                      {"idx": 1, "turns": 1, "verified": False, "provider": provider,
                       "cost_usd": 0.22}])

    assert done["pack"] is True
    assert done["subscription"] is expected
    assert done["cost_usd"] == 0.42


def test_pack_done_follows_the_winning_attempt_not_the_configured_provider(monkeypatch,
                                                                          tmp_path):
    """A roster attempt records the backend it ran on; the winner's is the run's route."""
    done = _run_pack(monkeypatch, tmp_path, "claude-agent-sdk",
                     [{"idx": 0, "turns": 2, "verified": True, "provider": "anthropic"}])

    assert done["subscription"] is False


@pytest.mark.parametrize("billing_class,expected", [("subscription_allowance", True),
                                                    ("api_metered", False),
                                                    ("unknown", False)])
def test_pack_done_on_an_external_worker_keeps_the_selector_billing_evidence(
        monkeypatch, tmp_path, billing_class, expected):
    """An external pack spends the worker's route, which the selector already evidenced."""
    probe = RunnerProbe(
        key="codex-exec", installed=True, executable_path="C:/bin/codex.exe",
        version="99.0.0", login="ok", billing_class=billing_class,
        billing_mode="subscription", probed_at=1_800_000_000.0)
    decision = HarnessDecision(
        runner="codex-exec", source="user", credential_family="codex",
        billing_class=billing_class, billing_mode="subscription", reasons=(),
        rejected={}, candidates=(), fallback_chain=(), probe=probe.to_dict(),
        probe_digest="digest")
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": probe})
    monkeypatch.setattr(runner_select, "decide", lambda *a, **kw: decision)

    # The configured Brain is a plan route; the worker is what actually spends.
    done = _run_pack(monkeypatch, tmp_path, "claude-agent-sdk",
                     [{"idx": 0, "turns": 1, "verified": True, "provider": "",
                       "runner": "codex-exec"}],
                     session="sub-pack-worker")

    assert done["subscription"] is expected


def test_pack_history_and_receipt_still_land_beside_the_label(monkeypatch, tmp_path):
    """The classification change must not disturb the rest of the terminal contract."""
    done = _run_pack(monkeypatch, tmp_path, "claude-agent-sdk",
                     [{"idx": 0, "turns": 2, "verified": True,
                       "provider": "claude-agent-sdk"}],
                     session="sub-pack-durable")

    assert done["answer"] == "winner"
    saved = sessions.load("sub-pack-durable")
    assert saved["messages"][-1]["content"] == "winner"
    assert saved["run_receipts"][-1]["pack"] is True
