from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.normalized_prime_pi import (
    API,
    AUTH_SENTINEL,
    FORBIDDEN_AUTH_ENV,
    MODEL,
    PI_COMMIT,
    PI_NORMALIZED_SYSTEM_PROMPT,
    PI_VERSION,
    PRIME_COMMIT,
    PRIME_VERSION,
    PROVIDER,
    prepare_pi,
    prepare_prime,
    run_pi,
    run_prime,
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("factory,name", [(prepare_prime, "prime"), (prepare_pi, "pi")])
def test_generates_fresh_keyless_openai_sidecar_config(tmp_path, factory, name):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launch = factory(
        tmp_path / name,
        endpoint="http://127.0.0.1:43117/v1/",
        workspace=workspace,
        prompt="Fix the frozen fixture.",
        inherited_env={
            "PATH": "/trusted/bin",
            "ANTHROPIC_API_KEY": "must-not-cross",
            "OPENAI_API_KEY": "must-not-cross",
            "UNRELATED_SECRET": "must-not-cross",
        },
    )

    assert launch.harness == name
    assert launch.cwd == workspace.resolve()
    assert launch.prompt_transport == "argv"
    assert launch.provider == PROVIDER
    assert launch.model == MODEL
    assert launch.thinking == "high"
    assert launch.config_dir.parent == launch.root
    assert launch.session_dir.parent == launch.root
    assert launch.home_dir.parent == launch.root
    assert launch.session_dir.is_dir() and not any(launch.session_dir.iterdir())
    assert not (launch.config_dir / "auth.json").exists()

    config = _read(launch.models_path)
    assert set(config["providers"]) == {PROVIDER}
    provider = config["providers"][PROVIDER]
    assert provider["baseUrl"] == "http://127.0.0.1:43117/v1"
    assert provider["api"] == API
    assert provider["apiKey"] == AUTH_SENTINEL
    assert provider["apiKey"] == "subscription-sidecar-internal-only-v1"
    assert provider["authHeader"] is True
    assert [model["id"] for model in provider["models"]] == [MODEL]
    assert provider["models"][0]["thinkingLevelMap"] == {"high": "high"}
    assert provider["models"][0]["cost"] == {
        "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
    }
    settings = _read(launch.settings_path)
    assert settings["retry"] == {
        "enabled": False,
        "maxRetries": 0,
        "baseDelayMs": 0,
        "provider": {"maxRetries": 0},
    }

    assert launch.env["PATH"] == "/trusted/bin"
    assert launch.env["HOME"] == str(launch.home_dir)
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["PI_OFFLINE"] == "1"
    assert launch.env["PI_SKIP_VERSION_CHECK"] == "1"
    assert not FORBIDDEN_AUTH_ENV.intersection(launch.env)
    assert "UNRELATED_SECRET" not in launch.env


def test_prime_pin_and_exact_isolated_argv(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    launch = prepare_prime(
        tmp_path / "prime-state",
        endpoint="http://localhost:8199/v1",
        workspace=workspace,
        prompt="Make the requested change.",
        executable="/pinned/prime-agent/prime-agent.sh",
        inherited_env={"PATH": "/usr/bin"},
    )

    assert (launch.pin.version, launch.pin.commit) == (PRIME_VERSION, PRIME_COMMIT)
    assert (PRIME_VERSION, PRIME_COMMIT) == (
        "0.7.2", "0987c1ba7637cbcb99afe9efe1180b838a0aa958"
    )
    assert launch.argv == (
        "/pinned/prime-agent/prime-agent.sh", "--dist", "--mode", "json",
        "--offline", "--no-session", "--no-extensions", "--no-context-files",
        "--no-skills", "--no-prompt-templates", "--no-themes",
        "--provider", PROVIDER, "--model", MODEL,
        "--models", f"{PROVIDER}/{MODEL}", "--thinking", "high",
        "--cwd", str(workspace.resolve()), "--", "Make the requested change.",
    )
    assert launch.env["PRIME_AGENT_CODING_AGENT_DIR"] == str(launch.config_dir)
    assert launch.env["PRIME_AGENT_SESSION_DIR"] == str(launch.session_dir)
    assert launch.env["PRIME_AGENT_TELEMETRY"] == "0"
    assert launch.env["PRIME_AGENT_KERNEL_PYTHON"] == "/opt/prime-kernel/bin/python"
    settings = _read(launch.settings_path)
    assert settings["telemetry"] == {"enabled": False}


def test_accepts_container_inference_service_endpoint(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    launch = prepare_prime(
        tmp_path / "state", endpoint="http://inference:43117/v1",
        workspace=workspace, prompt="Run test.", inherited_env={},
    )
    provider = _read(launch.models_path)["providers"][PROVIDER]
    assert provider["baseUrl"] == "http://inference:43117/v1"
    assert provider["apiKey"] == "subscription-sidecar-internal-only-v1"
    assert provider["authHeader"] is True


def test_pi_pin_and_exact_isolated_argv_preserves_native_tools(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    launch = prepare_pi(
        tmp_path / "pi-state",
        endpoint="http://[::1]:8199/v1",
        workspace=workspace,
        prompt="Make the requested change.",
        executable="/pinned/pi",
        inherited_env={"PATH": "/usr/bin"},
    )

    assert (launch.pin.version, launch.pin.commit) == (PI_VERSION, PI_COMMIT)
    assert (PI_VERSION, PI_COMMIT) == (
        "0.84.1", "53fa77ccd8a279eb87e92294ef3687b03ff80112"
    )
    assert launch.argv == (
        "/pinned/pi", "--mode", "json", "--offline", "--no-session",
        "--no-extensions", "--no-context-files", "--no-skills",
        "--no-prompt-templates", "--no-themes", "--no-approve",
        "--system-prompt", PI_NORMALIZED_SYSTEM_PROMPT
        + "\nCurrent working directory: " + str(workspace.resolve()),
        "--provider", PROVIDER, "--model", MODEL,
        "--models", f"{PROVIDER}/{MODEL}", "--thinking", "high",
        "Make the requested change.",
    )
    # No --no-tools/--no-builtin-tools: Pi keeps read/bash/edit/write.
    assert "--no-tools" not in launch.argv
    assert "--no-builtin-tools" not in launch.argv
    assert launch.env["PI_CODING_AGENT_DIR"] == str(launch.config_dir)
    assert launch.env["PI_CODING_AGENT_SESSION_DIR"] == str(launch.session_dir)
    assert launch.env["PI_TELEMETRY"] == "0"
    settings = _read(launch.settings_path)
    assert settings["defaultProjectTrust"] == "never"
    assert settings["enableInstallTelemetry"] is False
    assert settings["packages"] == []


def test_prime_keeps_its_native_ipython_tool(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    launch = prepare_prime(
        tmp_path / "state", endpoint="http://127.0.0.1:9/v1",
        workspace=workspace, prompt="Inspect and edit.", inherited_env={},
    )
    assert "--no-tools" not in launch.argv
    assert "--no-builtin-tools" not in launch.argv


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com/v1",
        "http://user:password@127.0.0.1:8000/v1",
        "http://127.0.0.1/v1",
        "file:///tmp/sidecar",
        "http://127.0.0.1:8000/v1?token=x",
    ],
)
def test_rejects_non_loopback_or_ambiguous_sidecar_endpoints(tmp_path, endpoint):
    workspace = tmp_path / "work"
    workspace.mkdir()
    with pytest.raises(ValueError):
        prepare_pi(
            tmp_path / "state", endpoint=endpoint, workspace=workspace,
            prompt="Run test.", inherited_env={},
        )


def test_refuses_to_reuse_nonempty_state_or_option_shaped_prompt(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "foreign").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError):
        prepare_prime(
            state, endpoint="http://127.0.0.1:8000/v1", workspace=workspace,
            prompt="Run test.", inherited_env={},
        )
    with pytest.raises(ValueError):
        prepare_pi(
            tmp_path / "fresh", endpoint="http://127.0.0.1:8000/v1",
            workspace=workspace, prompt="--provider anthropic", inherited_env={},
        )


@pytest.mark.parametrize(
    "factory,run,name,events,expected",
    [
        (
            prepare_prime,
            run_prime,
            "prime",
            [
                {"type": "agent_start"},
                {"type": "tool_execution_start", "toolCallId": "a", "toolName": "ipython",
                 "args": {"code": "from pathlib import Path; Path('x').write_text('ok')"}},
                {"type": "tool_execution_end", "toolCallId": "a", "toolName": "ipython", "isError": False},
                {"type": "agent_end", "messages": [{"content": "secret prompt echo"}]},
            ],
            {"ipython": 1},
        ),
        (
            prepare_pi,
            run_pi,
            "pi",
            [
                {"type": "agent_start"},
                {"type": "tool_execution_start", "toolCallId": "a", "toolName": "read"},
                {"type": "tool_execution_end", "toolCallId": "a", "toolName": "read", "isError": False},
                {"type": "tool_execution_start", "toolCallId": "b", "toolName": "bash"},
                {"type": "tool_execution_end", "toolCallId": "b", "toolName": "bash", "isError": True},
                {"type": "agent_end", "messages": []},
            ],
            {"bash": 0, "edit": 0, "read": 1, "write": 0},
        ),
    ],
)
def test_run_api_returns_prompt_free_native_tool_evidence(
    tmp_path, factory, run, name, events, expected
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    launch = factory(
        tmp_path / name, endpoint="http://127.0.0.1:8080/v1",
        workspace=workspace, prompt="TOP SECRET PROMPT", inherited_env={"PATH": "/bin"},
    )
    seen = {}

    def execute(argv, *, cwd, env, timeout_s):
        seen.update(argv=argv, cwd=cwd, env=env, timeout_s=timeout_s)
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )

    result = run(launch, timeout_s=17, executor=execute)
    assert result["outcome"] == "completed"
    assert result["returncode"] == 0
    assert result["safe_error_category"] == ""
    assert result["native_tool_success_counts"] == expected
    assert result["native_tool_success_total"] == 1
    assert result["tool_evidence"]["native_tool_calls"] == (1 if name == "prime" else 2)
    assert result["tool_evidence"]["native_edit_calls"] == (1 if name == "prime" else 0)
    assert result["tool_evidence"]["terminal_observed"] is True
    assert result["tool_evidence"]["terminal_observed"] == result["agent_end_observed"]
    assert result["agent_end_observed"] is True
    assert result["runtime"]["expected_version"] == launch.pin.version
    assert result["runtime"]["source_commit"] == launch.pin.commit
    assert result["runtime"]["system_prompt_profile"] == (
        "pi-native-coding-minus-self-documentation-v1"
        if name == "pi" else "prime-native-default"
    )
    assert result["raw_output_persisted"] is False
    assert result["prompt_persisted"] is False
    assert "TOP SECRET" not in json.dumps(result)
    assert seen == {
        "argv": launch.argv, "cwd": launch.cwd, "env": launch.env, "timeout_s": 17.0,
    }


def test_run_api_classifies_failure_without_returning_stderr(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    launch = prepare_pi(
        tmp_path / "pi", endpoint="http://127.0.0.1:8080/v1",
        workspace=workspace, prompt="secret prompt", inherited_env={},
    )

    def execute(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1, stdout="secret prompt echoed",
            stderr="ECONNREFUSED contains another secret",
        )

    result = run_pi(launch, executor=execute)
    assert result["outcome"] == "invalid"
    assert result["returncode"] == 1
    assert result["safe_error_category"] == "sidecar_unavailable"
    assert result["tool_evidence"]["terminal_observed"] is False
    assert result["tool_evidence"]["terminal_observed"] == result["agent_end_observed"]
    assert "secret" not in json.dumps(result).lower()


def test_run_api_rejects_cross_harness_launch(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    launch = prepare_pi(
        tmp_path / "pi", endpoint="http://127.0.0.1:8080/v1",
        workspace=workspace, prompt="test", inherited_env={},
    )
    with pytest.raises(ValueError):
        run_prime(launch, executor=lambda *_args, **_kwargs: None)
