"""Default settings and permissions are part of the accepted request too."""
import copy
from dataclasses import asdict

import pytest

from harness import capability_policy, cli, settings
from harness.providers import AnthropicProvider, OpenAICompatProvider, Completion, ToolCall, Usage
from harness.recorder import RunResult, run_outcome
from harness.tools import ReadFileTool, Tool


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


def test_a_queued_request_with_no_output_cap_can_start_on_the_codex_subscription(
        tmp_path, monkeypatch):
    """The panel leaves "Max output tokens" empty by default, so that is what most accepted
    requests froze, and replaying it means restoring the provider's OWN default: the request
    was authorized without an output cap, and the COLLIE_MAX_TOKENS its provider read at
    construction may be a cap another tab saved while it waited.  A provider that cannot name
    that default is refused rather than guessed at — so one that owns ``max_tokens`` without
    declaring it makes every durable request on it unstartable, which is what the ChatGPT
    Codex subscription did: the Web queue and ``/next`` both died inside ``make_harness``
    with "provider does not declare its default max_tokens", before a single token was sent.
    """
    from harness import codex_oauth
    auth = tmp_path / "codex"
    auth.mkdir()
    (auth / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(codex_oauth, "_auth_path", lambda: str(auth / "auth.json"))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(settings, "_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    monkeypatch.setattr(settings, "_HARD_ENV", set())
    for key in settings.LIMIT_KEYS:
        monkeypatch.delenv("COLLIE_" + key, raising=False)
    accepted = settings.RunLimits(source="frozen")    # accepted with no output cap at all
    monkeypatch.setenv("COLLIE_MAX_TOKENS", "512")    # ... then another tab saved one
    harness = cli.make_harness(str(tmp_path), provider="codex-oauth", embed="bm25",
                               limits=accepted)
    try:
        assert harness.provider.max_tokens == codex_oauth.CodexOAuthProvider.default_max_tokens
        assert harness.limits_applied["MAX_TOKENS"] == 16384
        assert not harness.limits_not_applicable
        # `/next` replays the same snapshot onto the long-lived terminal harness, every turn.
        monkeypatch.setenv("COLLIE_MAX_TOKENS", "64")
        cli.apply_accepted_limits(harness, accepted)
        assert harness.provider.max_tokens == 16384, "a later save is not this request's cap"
        # ... and a line typed after it is measured against the settings as they are now.
        cli.apply_accepted_limits(harness, None)
        assert harness.provider.max_tokens == 64
    finally:
        harness.memory.close()
        harness.recorder.close()


def test_no_provider_owns_a_generation_knob_it_cannot_name_a_default_for():
    """The contract the refusal above rests on, checked against the real provider sources.

    ``cli._apply_generation_limits`` can only replay an accepted "unset" by restoring a
    declared default, so owning ``self.max_tokens``/``self.temperature`` without declaring
    ``default_*`` is not a style problem: it silently strands every queued request on that
    provider while live typing keeps working, which is exactly why it survived unnoticed.
    Discovered from the source (any ``*Provider`` in the package) rather than a hand-kept
    list, and resolved through the class so an inherited default counts.
    """
    import ast
    import importlib
    import pathlib

    candidates = []
    for path in sorted(pathlib.Path(cli.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef) or not node.name.endswith("Provider"):
                continue
            assigned = {target.attr for stmt in ast.walk(node)
                        if isinstance(stmt, ast.Assign)
                        for target in stmt.targets
                        if isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name) and target.value.id == "self"}
            for knob in ("max_tokens", "temperature"):
                if knob in assigned:
                    candidates.append((path.stem, node.name, knob))
    assert ("codex_oauth", "CodexOAuthProvider", "max_tokens") in candidates, candidates
    undeclared = [
        "%s.%s has no default_%s" % (name, knob, knob) for module, name, knob in candidates
        if not hasattr(getattr(importlib.import_module("harness." + module), name),
                       "default_" + knob)]
    assert undeclared == [], undeclared


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


def test_a_delegated_subtask_investigates_under_the_parents_accepted_policy(
        tmp_path, monkeypatch):
    """Delegation owns a fresh conversation, never a fresh permission grant.

    ``delegate`` runs a CHILD harness inside the parent's turn.  The child used to
    build its own ToolCtx, whose default is a fresh read of the Settings panel — so a
    capability switched on while a queued request waited was correctly refused to the
    parent's tools and quietly handed to the child's, which is the one place an
    accepted policy could be stepped around without anybody accepting anything.  The
    parent's spending ceilings already travel down this path (``shared_budget`` carries
    ``_active_limits``); the capability policy has to travel with them.

    Read through the SHIPPED ``read_file`` tool — the real object the child registry
    inherits — so what is asserted is what a capability check inside a child tool call
    would actually see, not a value the test arranged for itself.  Fully offline: mock
    provider, temp data dir, and one temp file.
    """
    from harness import delegate                      # noqa: F401  (exercised via the tool)

    panel = {key: "off" for key in capability_policy.KEYS}
    monkeypatch.setattr(settings, "get", lambda key, default=None: panel.get(key, default))
    accepted = capability_policy.from_payload(capability_policy.freeze())
    panel["SCREEN_CAPTURE"] = "on"      # ... another tab enables it while the request waits
    note = tmp_path / "note.txt"
    note.write_text("evidence", encoding="utf-8")

    seen = []
    original = ReadFileTool.run

    def watched(self, args, ctx):
        seen.append(capability_policy.allowed("SCREEN_CAPTURE", ctx))
        return original(self, args, ctx)

    monkeypatch.setattr(ReadFileTool, "run", watched)

    class Provider:
        """Parent reads a file then delegates; the child reads the same file."""
        name, model, reports_cache = "mock", "mock", False
        calls = 0

        def complete(self, system, messages, tool_schemas, on_text=None):
            self.calls += 1
            if self.calls == 1:
                return Completion(tool_calls=[
                    ToolCall("parent-read", "read_file", {"path": "note.txt"}),
                    ToolCall("parent-delegate", "delegate",
                             {"task": "read note.txt and report what it says"}),
                ], usage=Usage(10, 10))
            if self.calls == 2:
                return Completion(
                    tool_calls=[ToolCall("child-read", "read_file", {"path": "note.txt"})],
                    usage=Usage(10, 10))
            return Completion(text="done", usage=Usage(10, 10))

    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "make_provider", lambda *a, **kw: Provider())
    harness = cli.make_harness(str(tmp_path), provider="mock", embed="bm25",
                               delegate=True, capabilities=accepted)
    try:
        result = harness.run("delegated-policy", "Investigate the note", consolidate=False)
        assert not result.error, result.error
    finally:
        harness.memory.close(); harness.recorder.close()

    assert len(seen) == 2, "the parent and its child each made one real tool call: %r" % (seen,)
    assert seen == [False, False], (
        "a capability enabled after this request was accepted must not reach the "
        "subtask it delegates either: %r" % (seen,))


def test_a_delegated_subtask_still_holds_what_the_parent_was_accepted_with(
        tmp_path, monkeypatch):
    """The other direction: inheriting a policy must still GRANT what it holds.

    A child that is simply never given anything satisfies the refusal above too, and
    would quietly break every subtask of a run that legitimately holds a capability.
    """
    panel = {key: "off" for key in capability_policy.KEYS}
    panel["SCREEN_CAPTURE"] = "on"                    # accepted with it on ...
    monkeypatch.setattr(settings, "get", lambda key, default=None: panel.get(key, default))
    accepted = capability_policy.from_payload(capability_policy.freeze())
    note = tmp_path / "note.txt"                      # ... and nobody touched the panel since
    note.write_text("evidence", encoding="utf-8")

    seen = []
    original = ReadFileTool.run

    def watched(self, args, ctx):
        seen.append(capability_policy.allowed("SCREEN_CAPTURE", ctx))
        return original(self, args, ctx)

    monkeypatch.setattr(ReadFileTool, "run", watched)

    class Provider:
        name, model, reports_cache = "mock", "mock", False
        calls = 0

        def complete(self, system, messages, tool_schemas, on_text=None):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    tool_calls=[ToolCall("parent-delegate", "delegate",
                                         {"task": "read note.txt"})],
                    usage=Usage(10, 10))
            if self.calls == 2:
                return Completion(
                    tool_calls=[ToolCall("child-read", "read_file", {"path": "note.txt"})],
                    usage=Usage(10, 10))
            return Completion(text="done", usage=Usage(10, 10))

    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "make_provider", lambda *a, **kw: Provider())
    harness = cli.make_harness(str(tmp_path), provider="mock", embed="bm25",
                               delegate=True, capabilities=accepted)
    try:
        result = harness.run("delegated-grant", "Investigate the note", consolidate=False)
        assert not result.error, result.error
    finally:
        harness.memory.close(); harness.recorder.close()

    assert seen == [True], "an accepted grant that is still enabled must reach the subtask"

    # And revocation stays a standing gate rather than a value the child replays.
    panel["SCREEN_CAPTURE"] = "off"
    assert capability_policy.allowed("SCREEN_CAPTURE",
                                     _StubCtx(dict(accepted))) is False


class _StubCtx:
    def __init__(self, capabilities):
        self.capabilities = capabilities


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
