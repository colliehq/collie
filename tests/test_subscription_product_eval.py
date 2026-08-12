import json
import subprocess
from types import SimpleNamespace


def test_codex_predictor_uses_stdin_subscription_cli_and_scrubs_metered_routes(monkeypatch, tmp_path):
    from harness import swe

    seen = {}

    def fake_run(cmd, workdir, extra_env=None, timeout=0, stdin_text=None):
        seen.update(cmd=cmd, workdir=workdir, extra_env=extra_env,
                    timeout=timeout, stdin_text=stdin_text)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swe, "_run_cli", fake_run)
    issue = "first line\nsecond line"
    swe.predict_codex(str(tmp_path), issue, model="gpt-test", timeout=17)

    assert seen["cmd"][0:5] == [
        "codex", "--sandbox", "workspace-write", "--ask-for-approval", "never"]
    assert "exec" in seen["cmd"] and "--json" in seen["cmd"]
    assert "--ephemeral" in seen["cmd"]
    assert "--ignore-user-config" in seen["cmd"]
    assert "--ignore-rules" in seen["cmd"]
    assert seen["cmd"][-1] == "-"
    assert issue not in seen["cmd"]
    assert seen["stdin_text"].endswith(issue)
    assert seen["extra_env"]["OPENAI_API_KEY"] is None
    assert seen["extra_env"]["OPENAI_AUTH_TOKEN"] is None
    assert seen["extra_env"]["OPENAI_ACCESS_TOKEN"] is None
    assert seen["extra_env"]["OPENAI_BASE_URL"] is None
    assert seen["extra_env"]["OPENAI_PROJECT_ID"] is None
    assert seen["extra_env"]["CODEX_API_KEY"] is None
    assert seen["extra_env"]["CODEX_AUTH_TOKEN"] is None
    assert seen["extra_env"]["CODEX_BASE_URL"] is None
    assert seen["extra_env"]["AZURE_OPENAI_AD_TOKEN"] is None
    assert seen["extra_env"]["ANTHROPIC_API_KEY"] is None
    assert seen["extra_env"]["CODEX_PERMISSION_PROFILE"] is None
    assert seen["extra_env"]["CODEX_THREAD_ID"] is None


def test_claude_predictor_is_headless_but_never_bypasses_host_permissions(monkeypatch, tmp_path):
    from harness import swe

    seen = {}

    def fake_run(cmd, workdir, extra_env=None, timeout=0, stdin_text=None):
        seen.update(cmd=cmd, extra_env=extra_env, stdin_text=stdin_text)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(swe, "_run_cli", fake_run)
    swe.predict_claude_code(str(tmp_path), "fix this", model="opus", max_turns=7)

    assert "bypassPermissions" not in seen["cmd"]
    assert "acceptEdits" in seen["cmd"]
    assert "--safe-mode" in seen["cmd"]
    assert "--no-session-persistence" in seen["cmd"]
    tools_index = seen["cmd"].index("--tools")
    assert seen["cmd"][tools_index + 1] == "Read,Edit,Write,Grep,Glob"
    allowed_index = seen["cmd"].index("--allowedTools")
    assert seen["cmd"][allowed_index + 1] == "Read,Edit,Write,Grep,Glob"
    turns_index = seen["cmd"].index("--max-turns")
    assert seen["cmd"][turns_index + 1] == "7"
    assert seen["extra_env"]["ANTHROPIC_API_KEY"] is None
    assert seen["extra_env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None
    assert seen["stdin_text"].endswith("fix this")


def test_claude_cli_provider_resolves_windows_shim(monkeypatch):
    from harness import providers

    seen = {}
    monkeypatch.setattr(providers.shutil, "which", lambda *a, **k: "C:/bin/claude.cmd")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-subscription-run")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-use-official-login-store")

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        seen["env"] = kwargs.get("env")
        system_path = cmd[cmd.index("--system-prompt-file") + 1]
        with open(system_path, encoding="utf-8") as system_file:
            seen["system"] = system_file.read()
        return subprocess.CompletedProcess(cmd, 0, json.dumps({
            "result": '{"answer":"done"}', "usage": {}}), "")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    provider = providers.ClaudeCliProvider("opus")
    provider.subscription_only = True
    completion = provider.complete(
        "system contract", [{"role": "user", "content": "do the work"}], [])

    assert seen["cmd"][0] == "C:/bin/claude.cmd"
    assert "system contract" not in seen["cmd"]
    assert "do the work" not in seen["cmd"]
    assert "--safe-mode" in seen["cmd"]
    assert "--no-session-persistence" in seen["cmd"]
    assert seen["system"] == "system contract"
    assert "do the work" in seen["input"]
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]
    assert completion.text == "done"


def test_registry_retain_removes_irrelevant_and_activated_tools():
    from harness.tools import Tool, ToolRegistry

    class Keep(Tool):
        name, tier = "keep", "always"

    class Drop(Tool):
        name, tier = "drop", "deferred"

    registry = ToolRegistry()
    registry.register(Keep())
    registry.register(Drop())
    registry.activate(["drop"])

    assert registry.retain(["keep"]) == ["keep"]
    assert registry.names() == ["keep"]
    assert registry.active_schemas()[0]["name"] == "keep"
    assert registry.deferred_names() == []


def test_codex_usage_parser_takes_last_aggregate_event_without_inventing_cost():
    from bench.paired_eval import _codex_usage

    stdout = "\n".join([
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2}}),
        "not json",
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 25, "cached_input_tokens": 12, "output_tokens": 7,
            "total_tokens": 44}}),
    ])

    assert _codex_usage(stdout) == {
        "input_tokens": 25,
        "cache_read": 12,
        "output_tokens": 7,
        "total_tokens": 44,
    }
    assert _codex_usage("{}\n") == {}


def test_codex_nonzero_exit_is_an_error_not_a_benchmark_loss(monkeypatch, tmp_path):
    from bench import paired_eval
    from harness import swe

    cli = subprocess.CompletedProcess(["codex"], 7, "quota exhausted", "")
    monkeypatch.setattr(swe, "predict_codex", lambda *a, **k: cli)
    monkeypatch.setattr(swe, "make_patch", lambda workdir: "")

    row = paired_eval.run_codex({"problem_statement": "fix it"}, str(tmp_path), "gpt-x", 1)

    assert row["harness"] == "codex"
    assert row["patch_bytes"] == 0
    assert "codex exited 7" in row["error"]
    assert row["usage"] == {}


def test_codex_zero_exit_with_read_only_write_denial_is_adapter_error(monkeypatch, tmp_path):
    from bench import paired_eval
    from harness import swe

    stdout = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message",
                 "text": "Writing is blocked by read-only sandbox."},
    })
    cli = subprocess.CompletedProcess(["codex"], 0, stdout, "")
    monkeypatch.setattr(swe, "predict_codex", lambda *a, **k: cli)
    monkeypatch.setattr(swe, "make_patch", lambda workdir: "")

    row = paired_eval.run_codex({"problem_statement": "fix it"}, str(tmp_path), "gpt-x", 1)

    assert row["patch_bytes"] == 0
    assert row["error"] == "codex workspace was read-only"


def test_claude_zero_exit_permission_refusal_is_adapter_error(monkeypatch, tmp_path):
    from bench import paired_eval
    from harness import swe

    cli = subprocess.CompletedProcess(["claude"], 0, json.dumps({
        "result": "Please approve the write and I can apply it.", "usage": {}}), "")
    monkeypatch.setattr(swe, "predict_claude_code", lambda *a, **k: cli)
    monkeypatch.setattr(swe, "make_patch", lambda workdir: "")

    row = paired_eval.run_claude({"problem_statement": "fix it"}, str(tmp_path), "opus", 1)

    assert row["patch_bytes"] == 0
    assert row["error"] == "claude adapter denied workspace editing"


def test_collie_benchmark_profile_has_no_ambient_rules_skills_shell_or_network(
        monkeypatch, tmp_path):
    from harness import cli, swe

    seen = {}

    class Registry:
        def retain(self, names):
            seen["tools"] = tuple(names)

    class Closable:
        def close(self):
            pass

    composer = SimpleNamespace(auto_prefetch=True, include_project_rules=True,
                               include_skills=True)
    harness = SimpleNamespace(registry=Registry(), composer=composer, memory=Closable(),
                              recorder=Closable(), provider=SimpleNamespace(subscription_only=False))

    def run(project, prompt, consolidate=False):
        seen.update(project=project, prompt=prompt, consolidate=consolidate)
        return SimpleNamespace()

    harness.run = run
    def make_harness(*args, **kwargs):
        seen["make_harness"] = kwargs
        return harness

    monkeypatch.setattr(cli, "make_harness", make_harness)
    # Ambient product customizations must not mutate the frozen benchmark profile.
    for name, value in {
            "COLLIE_PROVIDER": "metered-provider", "COLLIE_MODEL": "different-model",
            "COLLIE_MAX_TURNS": "99", "COLLIE_E2E_IMAGE": "some-image",
            "COLLIE_PLAN_FIRST": "1", "COLLIE_CRITIC": "1", "COLLIE_V1_PROMPT": "1",
            "COLLIE_TRACE_PATH": "1", "COLLIE_LEAN_PROMPT": "1",
            "COLLIE_CODE_SEARCH": "1", "COLLIE_SWE_LANG": "rust",
    }.items():
        monkeypatch.setenv(name, value)

    swe.predict_collie(str(tmp_path), "fix clamp", provider="claude-cli", model="opus",
                       max_turns=6, benchmark_safe=True)

    assert seen["tools"] == ("read_file", "write_file", "edit_file", "grep", "glob")
    assert composer.auto_prefetch is False
    assert composer.include_project_rules is False
    assert composer.include_skills is False
    assert harness.provider.subscription_only is True
    assert harness.max_turns == 6
    assert seen["make_harness"]["provider"] == "claude-cli"
    assert seen["make_harness"]["model"] == "opus"
    assert "`code_search`" not in seen["prompt"]
    assert "`run_in_env`" not in seen["prompt"]
    assert "external hidden grader" in seen["prompt"]


def test_subscription_smoke_hidden_grader_rejects_fixture_and_accepts_fix(tmp_path):
    from bench.subscription_smoke import FIXTURE, grade, prepare_fixture

    prepare_fixture(str(tmp_path))
    assert grade(str(tmp_path))["resolved"] is False
    (tmp_path / "clamp.py").write_text(
        "def clamp(value, lower, upper):\n"
        "    if lower > upper: raise ValueError('inverted')\n"
        "    return min(upper, max(value, lower))\n",
        encoding="utf-8",
    )
    assert FIXTURE.is_dir()
    assert grade(str(tmp_path))["resolved"] is True
