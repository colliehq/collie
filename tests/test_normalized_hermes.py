import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
import yaml

from bench.normalized_hermes import (
    API_KEY_SENTINEL,
    MODEL,
    PROVIDER_ID,
    TRACE_SCHEMA,
    HermesIsolationError,
    HermesTraceError,
    build_hermes_launch,
    parse_hermes_output,
    parse_hermes_trace,
    read_hermes_state,
    render_hermes_config,
    run_hermes,
)


def test_config_is_subscription_transport_only_and_keeps_native_coding_tools():
    config_text = render_hermes_config("http://sdk-transport:8765/v1", max_turns=27)
    config = yaml.safe_load(config_text)

    assert config["model"] == {
        "default": MODEL,
        "provider": PROVIDER_ID,
        "api_mode": "chat_completions",
    }
    assert config["providers"]["normalized-sdk"] == {
        "api": "http://sdk-transport:8765/v1",
        "transport": "chat_completions",
        "default_model": MODEL,
        "api_key": API_KEY_SENTINEL,
        "discover_models": False,
    }
    assert config["fallback_providers"] == []
    assert config["platform_toolsets"]["cli"] == ["terminal", "file"]
    assert config["agent"]["max_turns"] == 27
    disabled = set(config["agent"]["disabled_toolsets"])
    assert {"memory", "skills", "skills_hub", "delegation", "web", "browser"} <= disabled
    assert not {"terminal", "file"} & disabled
    assert config["memory"]["memory_enabled"] is False
    assert config["memory"]["user_profile_enabled"] is False
    assert config["mcp_servers"] == {}
    assert config["delegation"]["orchestrator_enabled"] is False
    assert config["compression"]["enabled"] is False
    assert config["telemetry"]["shared_metrics"]["enabled"] is False
    assert config["hooks"] == {}


@pytest.mark.parametrize("endpoint", [
    "sdk-transport:8765/v1",
    "ftp://sdk-transport/v1",
    "http://user:pass@sdk-transport/v1",
    "http://sdk-transport/v1?target=evil",
    "http://sdk-transport/v2",
])
def test_config_rejects_ambiguous_or_credentialed_endpoint(endpoint):
    with pytest.raises(HermesIsolationError):
        render_hermes_config(endpoint)


def test_launch_creates_fresh_home_exact_argv_and_scrubs_billing_routes(tmp_path):
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    home = tmp_path / "hermes-home"
    ambient = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "must-not-cross",
        "OPENAI_API_KEY": "must-not-cross",
        "CLAUDE_CODE_OAUTH_TOKEN": "must-not-cross",
        "HTTPS_PROXY": "http://must-not-cross",
        "NODE_OPTIONS": "--require=must-not-cross",
        "PYTHONPATH": "/must-not-cross",
        "SSL_CERT_FILE": "/must-not-cross",
    }
    launch = build_hermes_launch(
        home,
        workspace,
        "http://sdk-transport:8765/v1",
        "Fix the fixture and run its tests.",
        hermes_executable="/opt/hermes/.venv/bin/hermes",
        max_turns=11,
        source_env=ambient,
    )

    assert launch.argv == (
        "/opt/hermes/.venv/bin/hermes", "-z", "Fix the fixture and run its tests.",
        "--provider", PROVIDER_ID, "--model", MODEL,
        "--toolsets", "terminal,file", "--ignore-rules",
    )
    assert "--max-turns" not in launch.argv
    assert "--ignore-user-config" not in launch.argv
    assert launch.api_key_sentinel == API_KEY_SENTINEL == (
        "subscription-sidecar-internal-only-v1")
    assert launch.env["HERMES_HOME"] == str(home.resolve())
    assert launch.env["HOME"].startswith(str(home.resolve()))
    assert launch.env["HERMES_INFERENCE_PROVIDER"] == PROVIDER_ID
    assert launch.env["HERMES_INFERENCE_MODEL"] == MODEL
    assert set(ambient) & set(launch.env) == {"PATH", "LANG"}
    assert launch.config_sha256 == hashlib.sha256(
        launch.config_path.read_bytes()).hexdigest()
    assert (home / ".env").read_text(encoding="utf-8") == ""
    assert list((home / "plugins").iterdir()) == []
    assert list((home / "skills").iterdir()) == []


def test_launch_refuses_reusing_nonempty_profile(tmp_path):
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "auth.json").write_text("secret", encoding="utf-8")
    with pytest.raises(HermesIsolationError, match="absent or empty"):
        build_hermes_launch(home, workspace, "http://sdk:8765/v1", "work")


def _valid_trace():
    rows = [
        {"event": "run_start", "schema": TRACE_SCHEMA, "model": MODEL},
        {"event": "sdk_request", "request_id": "r1", "model": MODEL,
         "route": "claude_agent_sdk_subscription", "system_sha256": "a" * 64},
        {"event": "sdk_response", "request_id": "r1", "model": MODEL,
         "api_key_source": "none",
         "foreign_surfaces": {"tools": [], "skills": [], "plugins": [],
                              "agents": {}, "mcp_servers": {}},
         "usage": {"input_tokens": 100, "output_tokens": 12,
                   "cache_read_input_tokens": 80,
                   "cache_creation_input_tokens": 4}},
        {"event": "tool_call", "call_id": "t1", "name": "read_file"},
        {"event": "tool_result", "call_id": "t1", "success": True},
        {"event": "sdk_request", "request_id": "r2", "model": MODEL,
         "route": "claude_agent_sdk_subscription"},
        {"event": "sdk_response", "request_id": "r2", "model": MODEL,
         "api_key_source": "none", "foreign_surfaces": {},
         "usage": {"input_tokens": 150, "output_tokens": 20}},
        {"event": "tool_call", "call_id": "t2", "name": "patch"},
        {"event": "tool_result", "call_id": "t2", "success": True},
        {"event": "final", "text": "Implemented and tested."},
        {"event": "run_end", "status": "completed", "exit_code": 0},
    ]
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def test_trace_admission_counts_physical_calls_tools_usage_and_terminal():
    trace = _valid_trace()
    summary = parse_hermes_trace(trace)
    assert summary.admitted
    assert summary.physical_requests == summary.completed_requests == 2
    assert summary.tool_calls == summary.completed_tools == 2
    assert summary.native_edit_calls == 1
    assert summary.native_tool_names == ("read_file", "patch")
    assert summary.local_tool_observed
    assert summary.usage == {
        "input_tokens": 250,
        "output_tokens": 32,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 4,
    }
    assert summary.terminal_status == "completed"
    assert summary.terminal_exit_code == 0
    assert summary.final_text == "Implemented and tested."
    assert summary.trace_sha256 == hashlib.sha256(trace.encode()).hexdigest()
    assert summary.errors == ()


@pytest.mark.parametrize(("replacement", "expected_error"), [
    ({"event": "tool_call", "call_id": "t1", "name": "delegate_task"},
     "foreign_or_disabled_tool:delegate_task"),
    ({"event": "sdk_response", "request_id": "r1", "model": MODEL,
      "api_key_source": "ANTHROPIC_API_KEY", "foreign_surfaces": {}},
     "sdk_api_key_source_invalid"),
    ({"event": "sdk_response", "request_id": "r1", "model": MODEL,
      "api_key_source": "none", "foreign_surfaces": {"tools": ["Bash"]}},
     "sdk_foreign_surface_present"),
    ({"event": "sdk_request", "request_id": "r1", "model": MODEL,
      "route": "anthropic_api"}, "sdk_request_rerouted"),
])
def test_trace_fails_closed_for_foreign_routes_credentials_and_tools(replacement, expected_error):
    rows = [json.loads(line) for line in _valid_trace().splitlines()]
    target_event = replacement["event"]
    target_id = replacement.get("request_id") or replacement.get("call_id")
    for index, row in enumerate(rows):
        row_id = row.get("request_id") or row.get("call_id")
        if row["event"] == target_event and row_id == target_id:
            rows[index] = replacement
            break
    summary = parse_hermes_trace("\n".join(json.dumps(row) for row in rows))
    assert not summary.admitted
    assert expected_error in summary.errors


def test_trace_rejects_unpaired_calls_and_nonfinite_json():
    rows = [json.loads(line) for line in _valid_trace().splitlines()]
    rows = [row for row in rows if row.get("event") not in {"tool_call", "tool_result"}]
    rows.insert(-2, {"event": "tool_result", "call_id": "missing", "success": True})
    summary = parse_hermes_trace("\n".join(json.dumps(row) for row in rows))
    assert not summary.admitted
    assert "tool_result_unpaired" in summary.errors
    with pytest.raises(HermesTraceError, match="non-finite"):
        parse_hermes_trace('{"event":"run_start","value":NaN}\n')


def test_trace_allows_valid_zero_tool_completion_for_formal_ranking():
    rows = [json.loads(line) for line in _valid_trace().splitlines()]
    rows = [row for row in rows if row.get("event") not in {"tool_call", "tool_result"}]
    summary = parse_hermes_trace("\n".join(json.dumps(row) for row in rows))

    assert summary.admitted
    assert summary.tool_calls == 0
    assert summary.native_edit_calls == 0
    assert not summary.local_tool_observed


def test_oneshot_output_contract_is_plain_final_and_fail_closed():
    output = parse_hermes_output("  Implemented and tested.\n", "", 0)
    assert output.ok
    assert output.final_text == "Implemented and tested."

    failed = parse_hermes_output("", "Traceback (most recent call last): boom", 1)
    assert not failed.ok
    assert failed.errors == (
        "hermes_nonzero_exit", "hermes_final_output_missing", "hermes_traceback")


def test_native_state_allows_zero_tools_for_valid_unresolved_result(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    db = sqlite3.connect(home / "state.db")
    db.executescript("""
        CREATE TABLE sessions (
            id TEXT, model TEXT, started_at REAL, tool_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            api_call_count INTEGER
        );
        CREATE TABLE messages (
            id INTEGER, session_id TEXT, role TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT
        );
    """)
    db.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
        ("s1", MODEL, 1.0, 0, 25, 5, 0, 0, 1))
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
               (1, "s1", "assistant", None, None, None))
    db.commit()
    db.close()

    summary = read_hermes_state(home)
    assert summary.admitted
    assert summary.tool_calls == 0
    assert summary.native_edit_calls == 0
    assert summary.physical_requests == 1


def test_run_hermes_returns_redacted_stable_worker_receipt(tmp_path, monkeypatch):
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    home = tmp_path / "hermes-home"
    secret_prompt = "prompt-that-must-never-be-returned-or-persisted"

    def fake_run(argv, **kwargs):
        assert secret_prompt in argv
        db = sqlite3.connect(home / "state.db")
        db.executescript("""
            CREATE TABLE sessions (
                id TEXT, model TEXT, started_at REAL, tool_call_count INTEGER,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                api_call_count INTEGER
            );
            CREATE TABLE messages (
                id INTEGER, session_id TEXT, role TEXT, tool_call_id TEXT,
                tool_calls TEXT, tool_name TEXT
            );
        """)
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", MODEL, 1.0, 2, 250, 32, 80, 4, 2))
        calls = [
            {"id": "t1", "type": "function", "function": {"name": "read_file"}},
            {"id": "t2", "type": "function", "function": {"name": "patch"}},
        ]
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                   (1, "s1", "assistant", None, json.dumps(calls), None))
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                   (2, "s1", "tool", "t1", None, "read_file"))
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                   (3, "s1", "tool", "t2", None, "patch"))
        db.commit()
        db.close()
        return SimpleNamespace(
            returncode=0, stdout="Implemented and tested.\n", stderr="")

    monkeypatch.setattr("bench.normalized_hermes.subprocess.run", fake_run)
    receipt = run_hermes(
        home,
        workspace,
        "http://sdk-transport:8765/v1",
        secret_prompt,
        max_turns=11,
        wall_seconds=30,
        source_env={"PATH": "/usr/bin"},
    )

    assert receipt["worker_outcome"] == "candidate"
    assert receipt["error_category"] == ""
    assert receipt["returncode"] == 0
    assert receipt["tool_evidence"] == {
        "native_tool_calls": 2,
        "native_edit_calls": 1,
        "terminal_observed": True,
    }
    assert receipt["runtime"] == {
        "product": "hermes-agent",
        "version": "0.15.2",
        "entrypoint": "hermes -z",
        "provider": PROVIDER_ID,
        "model": MODEL,
        "transport": "openai_chat_completions",
        "profile": "ephemeral",
    }
    assert secret_prompt not in json.dumps(receipt, sort_keys=True)
    assert "Implemented and tested." not in json.dumps(receipt, sort_keys=True)
