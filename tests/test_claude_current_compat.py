"""Compatibility checks against the *installed* Claude Agent SDK and its CLI.

Every other Claude test in this suite states Collie's contract against a hand
written stand-in for the SDK.  That is deliberate — the provider must keep
working with no optional dependency present — but it means a newer SDK can
change a field name, a message shape, or a CLI flag without a single test
turning red.  This file closes that gap: it imports the real
``claude_agent_sdk`` (skipping when it is absent) and drives Collie's worker
through the SDK's own control protocol, message parser and command builder.

Nothing here spawns the CLI, opens a socket, or issues a model request.  The
only transport used is an in-process script (:class:`_ScriptedTransport`),
which is the SDK's documented extension point for exactly this purpose.

Audited against claude-agent-sdk 0.2.157 / Claude Code 2.1.277-2.1.278.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import typing
from pathlib import Path

import pytest


sdk = pytest.importorskip("claude_agent_sdk",
                          reason="the optional Claude Agent SDK is not installed")

from claude_agent_sdk._errors import CLIConnectionError  # noqa: E402
from claude_agent_sdk._internal.transport import Transport  # noqa: E402
from claude_agent_sdk._internal.transport.subprocess_cli import (  # noqa: E402
    SubprocessCLITransport,
)


ROOT = Path(__file__).resolve().parents[1]


def _worker_module():
    """Import ``harness/claude_agent_worker.py`` the way the worker runs it.

    The worker is executed as a script, never imported as ``harness.*``, so it
    is loaded here by path.  Importing it as part of the package would shadow
    the installed ``claude_agent_sdk`` with Collie's sibling transport module
    of the same name — the exact confusion the worker's own sys.path surgery
    exists to prevent.
    """
    spec = importlib.util.spec_from_file_location(
        "collie_claude_agent_worker_compat", ROOT / "harness" / "claude_agent_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _worker_module()


def _version_tuple(text: str) -> tuple:
    parts = []
    for chunk in str(text).split(".")[:3]:
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


# --- the installed pair ----------------------------------------------------

def test_installed_sdk_is_within_the_audited_range():
    """0.2.157 is what this audit read; 0.3 may move anything it relies on."""
    version = _version_tuple(sdk.__version__)
    assert (0, 2, 157) <= version < (0, 3, 0), (
        "claude-agent-sdk %s is outside the audited range; re-run the "
        "compatibility audit before shipping it" % sdk.__version__)


def test_installed_sdk_ships_the_claude_code_cli_generation_collie_audited():
    from claude_agent_sdk import _cli_version

    assert _version_tuple(_cli_version.__cli_version__) >= (2, 1, 277)


def test_query_and_options_are_still_the_public_entry_points():
    assert "query" in sdk.__all__ and "ClaudeAgentOptions" in sdk.__all__
    assert callable(sdk.query)


# --- ClaudeAgentOptions ----------------------------------------------------

def _request(**extra):
    request = {"protocol": 1, "model": "claude-opus-4-8", "system_prompt": "SYS",
               "prompt": "hello", "effort": "high"}
    request.update(extra)
    return request


def _structured_request(**extra):
    extra.setdefault("response_tools", ["read_file", "write_file"])
    return _request(protocol=3, response_format="structured", **extra)


def test_every_option_collie_sets_still_exists_on_the_installed_dataclass():
    """A silently dropped kwarg would be a silently re-opened surface."""
    import dataclasses

    fields = {field.name for field in dataclasses.fields(sdk.ClaudeAgentOptions)}
    required = {"model", "fallback_model", "system_prompt", "setting_sources",
                "tools", "allowed_tools", "mcp_servers", "strict_mcp_config",
                "skills", "plugins", "agents", "max_turns", "extra_args", "env",
                "effort", "output_format", "include_partial_messages"}

    assert required <= fields, sorted(required - fields)


def test_options_keep_every_capability_surface_closed():
    options = worker._build_options(sdk, _request())

    assert options.tools == [] and options.allowed_tools == []
    assert options.skills == [] and options.plugins == [] and options.agents == {}
    assert options.mcp_servers == {} and options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.max_turns == 1 and options.fallback_model is None
    # Nothing Collie never sets may default to something that opens a surface.
    assert options.permission_mode is None and options.can_use_tool is None
    assert options.hooks is None and options.sandbox is None
    assert options.add_dirs == [] and options.settings is None
    assert options.session_store is None and options.betas == []
    assert options.continue_conversation is False and options.resume is None
    assert options.fork_session is False
    assert options.max_budget_usd is None and options.task_budget is None


def test_requested_model_and_effort_are_passed_through_unchanged():
    options = worker._build_options(sdk, _request(effort="xhigh"))

    assert options.model == "claude-opus-4-8"
    assert options.effort == "xhigh"
    assert options.effort in typing.get_args(sdk.types.EffortLevel)


def test_auto_effort_spellings_never_reach_the_cli():
    for spelling in ("", "default", "auto", "provider-default", "AUTO"):
        options = worker._build_options(sdk, _request(effort=spelling))
        assert options.effort is None


def test_structured_options_pin_the_schema_and_disable_native_retries():
    options = worker._build_options(sdk, _structured_request())

    assert options.output_format["type"] == "json_schema"
    assert options.include_partial_messages is True
    assert options.env["MAX_STRUCTURED_OUTPUT_RETRIES"] == "0"
    assert options.env["CLAUDE_CODE_MAX_RETRIES"] == "0"
    # The plain path must stay byte-identical to a pre-structured worker.
    plain = worker._build_options(sdk, _request())
    assert plain.output_format is None
    assert plain.include_partial_messages is False
    assert "MAX_STRUCTURED_OUTPUT_RETRIES" not in plain.env


# --- the command the installed SDK actually builds -------------------------

def _command(request) -> list:
    transport = SubprocessCLITransport(
        prompt="hello", options=worker._build_options(sdk, request))
    transport._cli_path = "C:/collie/fake/claude.exe"
    return transport._build_command()


def test_plain_command_carries_no_tool_skill_or_host_configuration():
    cmd = _command(_request())
    joined = " ".join(cmd)

    assert cmd[1:4] == ["--output-format", "stream-json", "--verbose"]
    assert cmd[cmd.index("--tools") + 1] == ""          # every built-in tool off
    assert "--setting-sources=" in cmd                   # no user/project settings
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
    assert cmd[cmd.index("--effort") + 1] == "high"
    for absent in ("--allowedTools", "--mcp-config", "--plugin-dir", "--add-dir",
                   "--permission-mode", "--fallback-model", "--settings",
                   "--continue", "--session-mirror", "--fork-session",
                   "--dangerously-skip-permissions"):
        assert absent not in joined


def test_plain_command_carries_the_three_hardening_flags():
    cmd = _command(_request())

    assert "--safe-mode" in cmd
    assert "--no-session-persistence" in cmd
    assert "--disable-slash-commands" in cmd


def test_structured_command_sends_collies_own_schema():
    cmd = _command(_structured_request())
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])

    assert "--include-partial-messages" in cmd
    assert schema == worker._response_schema(["read_file", "write_file"])
    alternatives = schema["properties"]["response"]["anyOf"]
    assert {"answer"} in [set(alt["properties"]) for alt in alternatives]
    assert all(alt["additionalProperties"] is False for alt in alternatives)
    # A batch the request did not enable is not offered by the schema.
    assert not any("reads" in alt["properties"] for alt in alternatives)


def test_structured_read_batch_is_offered_only_when_the_request_enables_it():
    cmd = _command(_structured_request(response_tools=["read_file"],
                                       response_read_batch=True))
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    alternatives = schema["properties"]["response"]["anyOf"]
    batch = next(alt for alt in alternatives if "reads" in alt["properties"])

    assert batch["properties"]["reads"]["maxItems"] == worker._READ_BATCH_MAX
    assert batch["properties"]["reads"]["items"]["additionalProperties"] is False


def test_empty_skill_allowlist_never_injects_a_skill_rule():
    """``skills=[]`` must not turn into ``--allowedTools Skill(...)``."""
    transport = SubprocessCLITransport(
        prompt="hello", options=worker._build_options(sdk, _request()))
    allowed, sources = transport._apply_skills_defaults()

    assert allowed == []
    # None would make the SDK default to ["user", "project"]; [] keeps the
    # user's own settings out of this worker.
    assert sources == []


# --- Windows CLI discovery -------------------------------------------------

def test_batch_launchers_are_refused_by_the_installed_transport(monkeypatch):
    """npm's claude.cmd shim is not a supported CLI for this SDK generation."""
    from claude_agent_sdk._internal.transport import subprocess_cli
    monkeypatch.setattr(subprocess_cli.platform, "system", lambda: "Windows")
    for shim in (r"C:\\Users\\x\\AppData\\Roaming\\npm\\claude.CMD",
                 r"C:\\tools\\claude.bat",
                 r"C:\\tools\\claude.cmd\\..\\claude.exe"):
        with pytest.raises(CLIConnectionError):
            SubprocessCLITransport._reject_windows_batch_cli(shim)

    SubprocessCLITransport._reject_windows_batch_cli(r"C:\\Program Files\\claude.exe")


def test_a_native_executable_is_what_discovery_prefers():
    assert SubprocessCLITransport._is_windows_native_exe("C:/x/claude.exe")
    assert not SubprocessCLITransport._is_windows_native_exe("C:/x/claude.CMD")
    assert not SubprocessCLITransport._is_windows_native_exe("C:/x/claude")


def test_the_installed_sdk_bundles_a_cli_or_collie_must_stage_one():
    """Collie's Windows install depends on the bundled native executable.

    An sdist install of the SDK carries no ``_bundled/claude*`` binary, and on
    a machine whose only ``claude`` is npm's ``claude.cmd`` shim the transport
    then refuses to start at all (previous test).  The installer stages the
    pinned native CLI for exactly this reason; this test states the dependency
    so a packaging change that drops it fails here rather than at first use.
    """
    bundled = Path(sdk.__file__).parent / "_bundled"
    names = {"claude.exe", "claude"}
    staged = [path for path in bundled.glob("*") if path.name in names]

    if not staged:
        pytest.skip("no bundled CLI in this environment; the installer stages it")
    assert staged[0].is_file() and staged[0].stat().st_size > 0
    transport = SubprocessCLITransport(prompt="hi", options=sdk.ClaudeAgentOptions())
    assert transport._find_bundled_cli() == str(staged[0])


# --- error / usage vocabulary ----------------------------------------------

def test_provider_error_categories_match_the_installed_sdk_literal():
    documented = set(typing.get_args(sdk.types.AssistantMessageError))

    assert set(worker._PROVIDER_ERRORS) == documented - {"unknown"}
    # "unknown" classifies nothing, so it must stay off the reported path.
    assert "unknown" not in worker._PROVIDER_ERRORS


def test_rate_limit_windows_collie_reads_are_all_declared_by_the_sdk():
    declared = set(typing.get_args(sdk.types.RateLimitType))
    read_by_collie = {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"}

    assert read_by_collie <= declared


def test_post_result_error_is_still_recognised():
    from claude_agent_sdk._errors import ResultError

    error = ResultError("Claude Code returned an error result: rate limit",
                        data={"is_error": True}, exit_code=1)

    assert worker._is_post_result_error(error)
    assert not worker._is_post_result_error(RuntimeError("some other failure"))


def test_runner_effort_vocabulary_matches_the_installed_sdk():
    from harness import claude_code_runner

    assert set(claude_code_runner.EFFORT_LEVELS) == set(
        typing.get_args(sdk.types.EffortLevel))


# --- end to end through the SDK's own pipeline -----------------------------

class _ScriptedTransport(Transport):
    """An in-process CLI stand-in: real control protocol, no subprocess.

    ``write`` is where the SDK's initialize request arrives; answering it is
    what makes the rest of the scripted stream flow, so the handshake this
    transport exercises is the SDK's, not a re-implementation of it.
    """

    def __init__(self, script):
        self._script = list(script)
        self.written = []
        self._ready = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        self._ready = True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if not line.strip():
                continue
            message = json.loads(line)
            self.written.append(message)
            if (message.get("type") == "control_request"
                    and message["request"].get("subtype") == "initialize"):
                await self._queue.put({
                    "type": "control_response",
                    "response": {"subtype": "success",
                                 "request_id": message["request_id"],
                                 "response": {}},
                })
                for scripted in self._script:
                    await self._queue.put(scripted)
                await self._queue.put(None)

    async def read_messages(self):
        while True:
            message = await self._queue.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        pass


def _sdk_with(script):
    """A module-shaped shim that routes Collie's query through the real SDK."""
    transport = _ScriptedTransport(script)

    class _Shim:
        ClaudeAgentOptions = sdk.ClaudeAgentOptions
        transport_used = transport

        @staticmethod
        def query(*, prompt, options):
            return sdk.query(prompt=prompt, options=options, transport=transport)

    return _Shim


def _init(tools=(), model="claude-opus-4-8"):
    """The init frame Claude Code 2.1.x emits, in its own spelling."""
    return {"type": "system", "subtype": "init", "cwd": "C:/w",
            "session_id": "s", "tools": list(tools), "mcp_servers": [],
            "model": model, "permissionMode": "default", "slash_commands": [],
            "apiKeySource": "none", "agents": [], "skills": [], "plugins": [],
            "uuid": "u-init", "claude_code_version": "2.1.278"}


def _usage(output=7):
    return {"input_tokens": 11, "output_tokens": output,
            "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1}


def _result(**extra):
    payload = {"type": "result", "subtype": "success", "duration_ms": 5,
               "duration_api_ms": 4, "is_error": False, "num_turns": 1,
               "session_id": "s", "usage": _usage(), "result": "ok",
               "uuid": "u-result"}
    payload.update(extra)
    return payload


def _assistant(blocks, message_id="msg_1", **extra):
    payload = {"type": "assistant", "session_id": "s", "uuid": "u-a",
               "message": {"id": message_id, "role": "assistant",
                           "model": "claude-opus-4-8", "content": blocks,
                           "usage": _usage()}}
    payload.update(extra)
    return payload


def _run(script, request):
    return asyncio.run(worker._query(request, _sdk_with(script)))


def test_plain_turn_survives_the_installed_parser_and_control_protocol():
    result = _run([_init(), _assistant([{"type": "text", "text": "hello "},
                                        {"type": "text", "text": "world"}]),
                   _result()], _request())

    assert result == {"ok": True, "text": "hello world", "usage": _usage(),
                      "api_key_source": "none"}


def test_thinking_blocks_are_dropped_not_answered_with():
    result = _run([_init(),
                   _assistant([{"type": "thinking", "thinking": "hmm",
                                "signature": "sig"},
                               {"type": "text", "text": "answer"}]),
                   _result()], _request())

    assert result["text"] == "answer"


def test_a_foreign_tool_call_fails_the_plain_turn():
    with pytest.raises(RuntimeError, match="foreign tool use"):
        _run([_init(), _assistant([{"type": "tool_use", "id": "t1",
                                    "name": "Bash", "input": {}}]),
              _result()], _request())


def test_an_init_that_exposes_a_surface_fails_before_any_answer():
    with pytest.raises(RuntimeError, match="non-empty tools surface"):
        _run([_init(tools=["Bash"]), _assistant([{"type": "text", "text": "hi"}]),
              _result()], _request())


def test_an_init_naming_another_model_fails_the_turn():
    with pytest.raises(RuntimeError, match="frozen route"):
        _run([_init(model="claude-sonnet-5"),
              _assistant([{"type": "text", "text": "hi"}]), _result()], _request())


def test_provider_rejection_reports_its_category_and_reset():
    reset = 1_900_000_000
    script = [
        _init(),
        {"type": "rate_limit_event", "uuid": "u-r", "session_id": "s",
         "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour",
                             "resetsAt": reset}},
        _assistant([{"type": "text", "text": "quota prose"}], error="rate_limit"),
        _result(is_error=True),
    ]

    result = _run(script, _request())

    assert result["ok"] is False
    assert result["provider_error"] == "rate_limit"
    assert result["retry_at"] == reset
    assert result["usage"] == _usage()
    assert "text" not in result and "quota prose" not in json.dumps(result)


def _structured_script(tool_input, *, is_error=False, structured_output=None):
    return [
        _init(tools=["StructuredOutput"]),
        {"type": "stream_event", "uuid": "u-s", "session_id": "s",
         "event": {"type": "message_start",
                   "message": {"id": "msg_1", "usage": _usage(output=1)}}},
        _assistant([{"type": "tool_use", "id": "call_1",
                     "name": "StructuredOutput", "input": tool_input}]),
        {"type": "user", "session_id": "s", "uuid": "u-u",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call_1",
              "content": ("refused" if is_error
                          else "Structured output provided successfully"),
              "is_error": is_error}]}},
        _result(num_turns=2,
                structured_output=structured_output
                if structured_output is not None else tool_input),
    ]


def test_structured_turn_returns_collies_canonical_envelope():
    envelope = {"response": {"tool": "read_file", "args": {"path": "a.py"}}}

    result = _run(_structured_script(envelope), _structured_request())

    assert json.loads(result["text"]) == {"tool": "read_file",
                                          "args": {"path": "a.py"}}
    assert result["response_format"] == "structured"
    assert result["response_tools"] == ["read_file", "write_file"]
    assert result["usage"] == _usage()


def test_structured_answer_outside_the_allowlist_is_refused():
    envelope = {"response": {"tool": "run_shell", "args": {}}}

    with pytest.raises(RuntimeError, match="outside the allowlist"):
        _run(_structured_script(envelope), _structured_request())


def test_structured_output_must_match_the_formatter_input():
    envelope = {"response": {"answer": "one"}}

    with pytest.raises(RuntimeError, match="did not match the formatter input"):
        _run(_structured_script(envelope,
                                structured_output={"response": {"answer": "two"}}),
             _structured_request())


def test_formatter_refusal_stops_the_stream_before_a_second_response():
    envelope = {"response": {"answer": "  "}}
    script = _structured_script(envelope, is_error=True)
    # A native repair turn the CLI would have started next; reaching it at all
    # would mean Collie paid for a second model response.
    script.append(_assistant([{"type": "text", "text": "repair"}],
                             message_id="msg_2"))

    with pytest.raises(worker._StructuredContractRejected) as excinfo:
        _run(script, _structured_request())

    # Request side is measured; the response side is reported as unmeasured
    # rather than guessed from the refused response.
    assert excinfo.value.usage == {"input_tokens": 11, "output_tokens": 0,
                                   "cache_read_input_tokens": 3,
                                   "cache_creation_input_tokens": 1}
    assert excinfo.value.api_key_source == "none"


def test_structured_mode_sends_the_empty_skill_allowlist_over_initialize():
    shim = _sdk_with(_structured_script({"response": {"answer": "ok"}}))
    asyncio.run(worker._query(_structured_request(), shim))
    initialize = next(message for message in shim.transport_used.written
                      if message.get("type") == "control_request")

    assert initialize["request"]["skills"] == []
    assert initialize["request"]["hooks"] is None
    assert "agents" not in initialize["request"]


# --- the CLI runner's own Windows launch path ------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe launch semantics")
def test_runner_argv_survives_an_npm_batch_launcher_unchanged(tmp_path):
    """The claude-code runner spawns the CLI itself, not through the SDK.

    ``npm install -g @anthropic-ai/claude-code`` puts a ``claude.cmd`` shim on
    PATH, so ``shutil.which("claude")`` resolves to a batch file and Windows
    routes the spawn through cmd.exe, which re-parses the command line.  The
    SDK refuses that hop outright because *its* argv can carry caller-supplied
    session titles.  This runner's argv carries only values it generates and
    validates itself, so the hop is kept — but only as long as every argument
    survives it byte for byte.  A new flag whose value carries a cmd.exe
    metacharacter would fail here rather than in a paid run.
    """
    import subprocess

    from harness import claude_code_runner

    runner = claude_code_runner.ClaudeCodeRunner(
        executable="claude", model="claude-opus-4-8", effort="high", speed="fast",
        max_budget_usd=2.5, process_runner=object())
    argv = runner._argv("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", resume=False)[1:]

    echo = tmp_path / "echo_argv.py"
    echo.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
                    encoding="utf-8")
    shim = tmp_path / "claude.cmd"
    shim.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, echo),
                    encoding="utf-8", newline="")

    direct = subprocess.run([sys.executable, str(echo)] + argv,
                            capture_output=True, text=True, timeout=120)
    through_shim = subprocess.run([str(shim)] + argv,
                                  capture_output=True, text=True, timeout=120)

    assert direct.returncode == 0 and through_shim.returncode == 0
    assert json.loads(through_shim.stdout) == argv
    assert json.loads(direct.stdout) == argv


@pytest.mark.skipif(sys.platform != "win32", reason="Windows worker launch")
def test_worker_runs_under_the_isolated_interpreter_flag():
    """``python -I`` must still be able to import the installed SDK."""
    import subprocess

    probe = subprocess.run(
        [sys.executable, "-I", "-c",
         "import claude_agent_sdk; print(claude_agent_sdk.__version__)"],
        capture_output=True, text=True, timeout=120)

    assert probe.returncode == 0, probe.stderr[-400:]
    assert probe.stdout.strip() == sdk.__version__
