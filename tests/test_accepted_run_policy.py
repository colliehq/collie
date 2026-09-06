"""Default settings and permissions are part of the accepted request too."""
import copy
from dataclasses import asdict

import pytest

from harness import capability_policy, cli, settings
from harness.providers import AnthropicProvider, OpenAICompatProvider, Completion, ToolCall, Usage
from harness.recorder import RunResult, run_outcome
from harness.tools import Tool


def test_an_unset_generation_limit_replays_the_provider_default(monkeypatch):
    accepted = settings.RunLimits(source="frozen")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-placeholder")
    monkeypatch.setenv("UNIT_TEST_PROVIDER_KEY", "unit-test-placeholder")
    monkeypatch.setenv("COLLIE_MAX_TOKENS", "512")
    monkeypatch.setenv("COLLIE_TEMPERATURE", "0.9")
    anthropic = AnthropicProvider()
    compatible = OpenAICompatProvider("http://127.0.0.1:1", "UNIT_TEST_PROVIDER_KEY", "test")
    assert anthropic.max_tokens == compatible.max_tokens == 512
    cli._apply_generation_limits(anthropic, accepted)
    cli._apply_generation_limits(compatible, accepted)
    assert anthropic.max_tokens == 8192
    assert compatible.max_tokens == 4096 and compatible.temperature == 0.2
    zero = settings.RunLimits(temperature=0.0, source="frozen")
    cli._apply_generation_limits(compatible, zero)
    assert compatible.temperature == 0.0


def test_a_partial_or_noncanonical_frozen_budget_cannot_become_unlimited():
    payload = settings.RunLimits(max_total_tokens=3000, source="frozen").payload()
    examples = []
    missing = copy.deepcopy(payload); missing["values"].pop("MAX_TOTAL_TOKENS"); examples.append(missing)
    no_digest = copy.deepcopy(payload); no_digest.pop("digest"); examples.append(no_digest)
    examples.append(dict(payload, digest=""))
    junk = copy.deepcopy(payload); junk["values"]["MAX_TOTAL_TOKENS"] = "inf"; examples.append(junk)
    for broken in examples:
        with pytest.raises(ValueError):
            settings.limits_from_payload(broken)


def test_saved_budget_is_present_in_serialized_results_and_durable_outcomes():
    result = RunResult(budget_exhausted=True, budget_limits={"MAX_TOTAL_TOKENS":"3000", "source":"frozen"})
    assert asdict(result)["budget_limits"] == result.budget_limits
    assert run_outcome(result)["budget_limits"] == result.budget_limits


def test_real_harness_tools_receive_the_accepted_capability_policy(tmp_path, monkeypatch):
    panel = {key:"off" for key in capability_policy.KEYS}
    monkeypatch.setattr(settings,"get",lambda key,default=None:panel.get(key,default))
    frozen = capability_policy.from_payload(capability_policy.freeze())
    panel["SCREEN_CAPTURE"] = "on"
    seen = []

    class Probe(Tool):
        name, tier, description = "policy_probe", "always", "Read this task's policy"
        schema = {"type":"object", "properties":{}}
        def run(self, args, ctx):
            seen.append(capability_policy.allowed("SCREEN_CAPTURE",ctx))
            return "policy inspected"

    class Provider:
        name, model, reports_cache = "mock", "mock", False
        calls = 0
        def complete(self, system, messages, tool_schemas, on_text=None):
            self.calls += 1
            if self.calls == 1:
                return Completion(tool_calls=[ToolCall("probe-1","policy_probe",{})], usage=Usage(10,10))
            return Completion(text="done", usage=Usage(10,10))

    monkeypatch.setattr(cli,"DATA",str(tmp_path/"data"))
    monkeypatch.setattr(cli,"make_provider",lambda *a,**kw:Provider())
    harness = cli.make_harness(str(tmp_path),provider="mock",embed="bm25",capabilities=frozen)
    harness.registry.register(Probe()); harness.registry.activate(["policy_probe"])
    try:
        result = harness.run("capability-policy", "Inspect the policy with policy_probe", consolidate=False)
        assert not result.error, result.error
        assert seen == [False], "another request's grant must not arm this accepted task"
    finally:
        harness.memory.close(); harness.recorder.close()


def test_native_pack_freezes_one_policy_and_budget_for_all_candidates(tmp_path, monkeypatch):
    from harness import pack, catalog
    from harness.providers import MockProvider
    panel = {key:"off" for key in capability_policy.KEYS}
    monkeypatch.setattr(settings,"get",lambda key,default=None:panel.get(key,default))
    monkeypatch.setattr(settings,"_HARD_ENV",set())
    monkeypatch.setattr(cli,"DATA",str(tmp_path/"state"))
    monkeypatch.setattr(catalog,"preflight",lambda roster:[])
    project = tmp_path/"project"; project.mkdir(); (project/"README.md").write_text("fixture")
    snapshots = []
    real_make = cli.make_harness
    def make(*args,**kwargs):
        snapshots.append((kwargs["limits"].max_total_tokens,dict(kwargs["capabilities"])))
        harness = real_make(*args,embed="bm25",**kwargs)
        original_run = harness.run
        def run(*a,**kw):
            panel.update(MAX_TOTAL_TOKENS="999999", SCREEN_CAPTURE="on")
            return original_run(*a,**kw)
        harness.run = run
        return harness
    monkeypatch.setattr(cli,"make_harness",make)
    monkeypatch.setattr(cli,"make_provider",lambda *a,**kw:MockProvider())
    accepted = settings.RunLimits(max_total_tokens=100000,source="frozen")
    result = pack.run_pack("Hello",str(project),n=2,provider="mock",limits=accepted)
    assert len(result["attempts"]) == 2, result
    assert len(snapshots) == 2 and snapshots[0] == snapshots[1]
    assert snapshots[0][0] == 100000 and not snapshots[0][1]["SCREEN_CAPTURE"]
