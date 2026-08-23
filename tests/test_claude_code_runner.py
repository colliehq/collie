"""What `claude -p` is allowed to be started with, and what comes back.

Nothing here runs Claude Code.  Every test drives a fake process transport, in
the shape `tests/test_agent_runners.py` established, because the properties worth
locking are the argv, the environment and the parsing — and all three are decided
before a real CLI would have printed its first byte.

The argv test is written as an exact equality on purpose.  A flag added by
accident (`--dangerously-skip-permissions` copied from the benchmark adapter, a
`--permission-mode bypassPermissions` pasted from a blog post) is precisely the
kind of change that keeps every other test green while removing the boundary the
selection layer's H9 rule assumes.
"""
import json
import os

import pytest

from harness import runner_specs
from harness.agent_runners import ProcessOutcome, RecoveryRequiredError, RunnerSnapshot
from harness.claude_code_runner import CAPABILITIES, ClaudeCodeRunner
from harness.runner_env import BillingOverrideError


SESSION = "0199a213-81c0-7800-8aa1-bbab2a035a53"
OTHER_SESSION = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
TOOLS = "Read,Edit,Write,Grep,Glob"

# A parent environment that is realistic on Windows and contains nothing this
# runner is allowed to hand down.  CLAUDECODE/CLAUDE_CODE_ENTRYPOINT are what the
# real one looks like when Collie itself was started from inside Claude Code.
PARENT = {
    "PATH": "C:\\tools;C:\\Windows\\System32",
    "PATHEXT": ".COM;.EXE;.CMD",
    "SYSTEMROOT": "C:\\Windows",
    "USERPROFILE": "C:\\Users\\dev",
    "TEMP": "C:\\Temp",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "NODE_OPTIONS": "--require=/tmp/evil.js",
    "HTTPS_PROXY": "http://127.0.0.1:8888",
    "COLLIE_STATE_DIR": "C:\\Users\\dev\\.collie",
}


class FakeProcess:
    pid = 51515


class FakeProcessRunner:
    """Records the launch request and returns canned outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
        self.calls.append({"argv": tuple(argv), "cwd": cwd, "stdin": stdin_text,
                           "timeout": timeout_s, "env": dict(env or {})})
        on_process(FakeProcess())
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Snapshots:
    def __init__(self, *digests):
        self.digests = list(digests)

    def __call__(self, _workspace):
        return {"tree_digest": self.digests.pop(0), "snapshot_complete": True}


def _result(**overrides):
    """One `--output-format json` result object."""
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "added the line",
        "session_id": SESSION,
        "num_turns": 3,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 900, "output_tokens": 120,
                  "cache_read_input_tokens": 4_000,
                  "cache_creation_input_tokens": 250},
    }
    payload.update(overrides)
    return payload


def _outcome(*, exit_code=0, stderr="", **overrides):
    return ProcessOutcome(stdout=json.dumps(_result(**overrides)),
                          stderr=stderr, exit_code=exit_code)


def _runner(process, *, digests=("before", "before"), **kwargs):
    kwargs.setdefault("environ", PARENT)
    return ClaudeCodeRunner(process_runner=process, snapshotter=Snapshots(*digests),
                            session_ids=lambda: SESSION, **kwargs)


def test_argv_exact_no_bypass_no_max_turns(tmp_path):
    process = FakeProcessRunner(_outcome())
    runner = _runner(process, default_timeout_s=42)

    runner.start("add a line to README", str(tmp_path))

    assert process.calls[0]["argv"] == (
        "claude", "-p",
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--tools", TOOLS,
        "--allowedTools", TOOLS,
        "--safe-mode",
        "--strict-mcp-config",
        "--session-id", SESSION,
    )
    assert process.calls[0]["timeout"] == 42
    assert process.calls[0]["cwd"] == os.path.realpath(str(tmp_path))
    flat = " ".join(process.calls[0]["argv"]).lower()
    for banned in ("bypasspermissions", "--dangerously", "--max-turns",
                   "--no-session-persistence", "--permission-prompt-tool"):
        assert banned not in flat


def test_argv_carries_model_and_budget_when_set(tmp_path):
    process = FakeProcessRunner(_outcome())
    runner = _runner(process, model="opus", max_budget_usd=2.5)

    runner.start("go", str(tmp_path))

    argv = process.calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    # Formatted without exponent notation; a "2.5e+00" would be a silent no-op.
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5"


def test_prompt_on_stdin(tmp_path):
    # A prompt is untrusted text of unbounded length: on Windows it would blow the
    # command-line limit, and on any platform it would be visible in `ps`.
    prompt = "fix the race --dangerously-skip-permissions\nand explain why"
    process = FakeProcessRunner(_outcome())

    _runner(process).start(prompt, str(tmp_path))

    assert process.calls[0]["stdin"] == prompt
    assert prompt not in process.calls[0]["argv"]
    assert "--dangerously-skip-permissions" not in process.calls[0]["argv"]


def test_resume_uses_same_uuid(tmp_path):
    process = FakeProcessRunner(_outcome(), _outcome(result="and again"))
    runner = _runner(process, digests=("a", "a", "a", "a"))

    first = runner.start("step one", str(tmp_path))
    second = runner.resume(first, "step two")

    assert first.thread_id == SESSION
    resumed = process.calls[1]["argv"]
    assert "--session-id" not in resumed
    assert resumed[resumed.index("--resume") + 1] == SESSION
    # --fork-session would start a sibling thread and orphan the work.
    assert "--fork-session" not in resumed
    assert second.thread_id == SESSION
    assert second.invocation == 2
    assert second.cursor == 2


def test_result_parse_max_turn_and_is_error(tmp_path):
    # The turn ceiling is Claude's own (there is no --max-turns to raise), and it
    # exits non-zero for it, so the message must not read like a crash.
    exhausted = FakeProcessRunner(
        ProcessOutcome(stdout=json.dumps(_result(subtype="error_max_turns",
                                                 is_error=True, result="")),
                       exit_code=1))
    snap = _runner(exhausted).start("go", str(tmp_path))
    assert snap.settled is False
    assert "turn limit" in snap.error
    assert "error_max_turns" in snap.error

    failed = FakeProcessRunner(
        ProcessOutcome(stdout=json.dumps(_result(is_error=True,
                                                 subtype="error_during_execution",
                                                 result="tool use failed")),
                       exit_code=0))
    snap = _runner(failed).start("go", str(tmp_path))
    assert snap.settled is False
    assert snap.error == "tool use failed"


def test_successful_result_is_settled_and_answers(tmp_path):
    process = FakeProcessRunner(_outcome())

    snap = _runner(process, digests=("before", "after")).start("go", str(tmp_path))

    assert snap.settled is True
    assert snap.error == ""
    assert snap.final_output == "added the line"
    assert snap.thread_id == SESSION
    assert snap.exit_code == 0
    assert snap.invocation == 1
    assert snap.mutated is True and snap.mutation_check_complete is True
    assert snap.workspace_digest == "after"
    # Settling cleanly is not verification: only the host verifier may claim that.
    assert snap.recovery_required is False
    assert RunnerSnapshot.from_dict(snap.to_dict()).thread_id == SESSION


def test_usage_stays_in_claude_dialect_for_usage_to_collie(tmp_path):
    process = FakeProcessRunner(_outcome())

    snap = _runner(process).start("go", str(tmp_path))

    # Raw Claude field names, because usage_to_collie() is what translates them —
    # translating here as well would drop the cache counters and the cost.
    usage = runner_specs.usage_to_collie("claude-code", snap.usage)
    assert usage.known is True
    assert (usage.input_tokens, usage.output_tokens) == (900, 120)
    assert (usage.cache_read, usage.cache_creation) == (4_000, 250)
    assert usage.cost_usd_reported == pytest.approx(0.0123)
    assert usage.source == "result.usage"


def test_cost_survives_a_persisted_snapshot(tmp_path):
    # RunnerSnapshot.from_dict int-coerces usage values, so a sub-dollar float
    # comes back as 0 — which would read as "this run was free".
    process = FakeProcessRunner(_outcome())
    snap = _runner(process).start("go", str(tmp_path))

    reloaded = RunnerSnapshot.from_dict(json.loads(json.dumps(snap.to_dict())))

    assert reloaded.usage["cost_micro_usd"] == 12_300
    assert reloaded.usage["input_tokens"] == 900


def test_usage_accumulates_tokens_but_not_session_cost(tmp_path):
    process = FakeProcessRunner(
        _outcome(),
        ProcessOutcome(stdout=json.dumps(_result(total_cost_usd=0.02)), exit_code=0))
    runner = _runner(process, digests=("a", "a", "a", "a"))

    second = runner.resume(runner.start("one", str(tmp_path)), "two")

    assert second.usage["input_tokens"] == 1_800
    # Claude reports the session's cost, not the turn's: summing would invent money.
    assert second.usage["total_cost_usd"] == pytest.approx(0.02)


def test_env_has_no_anthropic_or_claude_vars(tmp_path):
    process = FakeProcessRunner(_outcome())
    runner = _runner(process)

    runner.start("go", str(tmp_path))

    env = process.calls[0]["env"]
    assert env, "the child environment must be built, not inherited"
    leaked = [name for name in env
              if name.startswith("ANTHROPIC_") or name.startswith("CLAUDE_")
              or name == "CLAUDECODE"]
    assert leaked == []
    assert "NODE_OPTIONS" not in env and "HTTPS_PROXY" not in env
    # …while the names the CLI genuinely needs to start are still there.
    assert env["PATH"] == PARENT["PATH"]
    assert env["PATHEXT"] == PARENT["PATHEXT"]
    assert env["NO_COLOR"] == "1"
    # Names only in the receipt — never a value.
    assert "CLAUDECODE" in runner.last_env_receipt["stripped"]
    assert "NODE_OPTIONS" in runner.last_env_receipt["stripped"]
    assert "PATH" in runner.last_env_receipt["allowed"]
    assert PARENT["NODE_OPTIONS"] not in json.dumps(runner.last_env_receipt)


def test_forbidden_env_rejected_before_start(tmp_path):
    process = FakeProcessRunner(_outcome())
    runner = _runner(process, environ=dict(PARENT, ANTHROPIC_API_KEY="sk-ant-secret"))

    with pytest.raises(BillingOverrideError) as caught:
        runner.start("go", str(tmp_path))

    # Refused, not degraded: the run would have been billed to the API account
    # while the receipt still said "subscription".
    assert process.calls == []
    assert "ANTHROPIC_API_KEY" in str(caught.value)
    assert "sk-ant-secret" not in str(caught.value)


def test_session_id_mismatch_is_a_protocol_error(tmp_path):
    process = FakeProcessRunner(_outcome(session_id=OTHER_SESSION))

    snap = _runner(process).start("go", str(tmp_path))

    assert snap.settled is False
    assert "unparseable or inconsistent" in snap.error
    # Keep the id that actually holds the transcript: it is the only resumable one.
    assert snap.thread_id == OTHER_SESSION


def test_unparseable_stdout_is_a_protocol_error(tmp_path):
    process = FakeProcessRunner(
        ProcessOutcome(stdout="Welcome to Claude Code!\n{\"result\": \"hi\"}",
                       stderr="", exit_code=0))

    snap = _runner(process).start("go", str(tmp_path))

    # Deliberately strict: scavenging JSON out of surrounding noise would mean
    # parsing text the model itself wrote.
    assert snap.settled is False
    assert snap.events[-1].type == "protocol.invalid_json"


def test_error_text_and_events_are_redacted(tmp_path):
    process = FakeProcessRunner(
        ProcessOutcome(stdout=json.dumps(_result(result="token sk-ant-abc123456789")),
                       stderr="authorization: Bearer sk-ant-abc123456789",
                       exit_code=3))

    snap = _runner(process).start("go", str(tmp_path))

    assert snap.settled is False
    assert "sk-ant-abc123456789" not in snap.error
    assert "sk-ant-abc123456789" not in json.dumps(snap.events[-1].to_dict())


def test_transport_failure_keeps_the_locator_and_flags_recovery(tmp_path):
    process = FakeProcessRunner(FileNotFoundError("claude is not on PATH"))

    snap = _runner(process, digests=("before", "after")).start("go", str(tmp_path))

    assert snap.settled is False
    assert snap.exit_code is None
    assert "FileNotFoundError" in snap.error
    # The workspace changed under a turn that did not report back: replaying it
    # is exactly what must not happen automatically.
    assert snap.mutated is True and snap.recovery_required is True
    # Pinned up front, so a killed start is still resumable.
    assert snap.thread_id == SESSION
    with pytest.raises(RecoveryRequiredError):
        _runner(FakeProcessRunner(_outcome())).resume(snap, "again")


def test_resume_refuses_a_foreign_or_unsafe_locator(tmp_path):
    runner = _runner(FakeProcessRunner(_outcome()))
    good = RunnerSnapshot(runner="claude-code",
                          workspace=os.path.realpath(str(tmp_path)),
                          thread_id=SESSION)

    with pytest.raises(ValueError):
        runner.resume(RunnerSnapshot.from_dict(dict(good.to_dict(), runner="codex-exec")), "x")
    with pytest.raises(ValueError):
        runner.resume(RunnerSnapshot.from_dict(
            dict(good.to_dict(), thread_id="--resume; rm -rf /")), "x")


def test_shell_tools_are_refused():
    # claude-code declares confinement=tools-allowlist with no shell, and the
    # selector hands it work on that basis.
    for tool in ("Bash", "bash", "WebFetch", "Task"):
        with pytest.raises(ValueError):
            ClaudeCodeRunner(tools=("Read", tool), environ=PARENT)
    for bad in ("--model", "mcp__server__tool", "Read,Bash"):
        with pytest.raises(ValueError):
            ClaudeCodeRunner(tools=(bad,), environ=PARENT)
    assert "bash" not in {name.lower() for name in CAPABILITIES.tools}


def test_probe_is_metadata_only(monkeypatch, tmp_path):
    calls = []

    def fake_which(name):
        return "C:\\npm\\claude.cmd" if name == "claude" else None

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return type("R", (), {"returncode": 0, "stdout": "2.1.221 (Claude Code)\n",
                              "stderr": ""})()

    monkeypatch.setattr("harness.claude_code_runner.shutil.which", fake_which)
    # `_cli_version` is shared with CodexExecRunner, so the version call itself
    # lives in agent_runners; patch it where it actually runs.
    monkeypatch.setattr("harness.agent_runners.subprocess.run", fake_run)

    probe = _runner(FakeProcessRunner()).probe(now=1_700_000_000.0)

    assert probe.installed is True
    assert probe.version == "2.1.221 (Claude Code)"
    assert probe.probed_at == 1_700_000_000.0
    # The npm shim is resolved through which(): CreateProcess ignores PATHEXT.
    assert probe.executable_path.endswith("claude.cmd")
    # One read-only command, and it is not `claude auth status` — deciding the
    # login is a live probe and belongs to runner_registry.
    assert calls == [("C:\\npm\\claude.cmd", "--version")]
    # Login and billing are live probes and belong to runner_registry, so this
    # probe alone is deliberately not usable().
    assert probe.login == "unknown" and probe.usable() is False
    assert probe.capabilities["confinement"] == "tools-allowlist"
    assert probe.capabilities["approval_round_trip"] is False


def test_probe_reports_a_missing_cli_instead_of_raising(monkeypatch):
    monkeypatch.setattr("harness.claude_code_runner.shutil.which", lambda _name: None)

    probe = _runner(FakeProcessRunner()).probe()

    assert probe.installed is False and probe.usable() is False
    assert "not on PATH" in probe.detail


def test_probe_reports_a_broken_cli_without_a_version(monkeypatch):
    monkeypatch.setattr("harness.claude_code_runner.shutil.which",
                        lambda _name: "/usr/local/bin/claude")

    def boom(_argv, **_kwargs):
        raise OSError("Exec format error")

    monkeypatch.setattr("harness.agent_runners.subprocess.run", boom)

    probe = _runner(FakeProcessRunner()).probe()

    assert probe.installed is True and probe.version == ""
    assert "Exec format error" in probe.detail


def test_cancel_for_delegates_to_cancel_current():
    runner = _runner(FakeProcessRunner())
    seen = []
    runner.cancel_current = lambda: (seen.append(True), True)[1]

    # Same shape as CodexExecRunner.cancel_for: the key is accepted and ignored,
    # because this runner owns at most one turn and the registry cancels by key.
    assert runner.cancel_for("claude-code") is True
    assert runner.cancel_for() is True
    assert seen == [True, True]


def test_cancel_with_nothing_running_is_not_a_kill():
    # False here means "there was nothing to cancel", which callers must not
    # confuse with extinction proof.
    assert _runner(FakeProcessRunner()).cancel_current() is False


def test_snapshot_projects_onto_a_run_result(tmp_path):
    process = FakeProcessRunner(_outcome())
    snap = _runner(process, digests=("before", "after")).start("go", str(tmp_path))
    spec = runner_specs.HarnessSpec(key="claude-code", label="Claude Code",
                                    kind="external", credential_family="claude")
    probe = runner_specs.RunnerProbe(key="claude-code", installed=True)

    row = runner_specs.snapshot_to_run_result(snap, spec, probe, model="opus")

    assert row.harness == "claude-code"
    assert row.success is True
    assert row.verified is False
    assert (row.input_tokens, row.output_tokens) == (900, 120)
    assert "sk-" not in json.dumps(row.__dict__, default=str)
