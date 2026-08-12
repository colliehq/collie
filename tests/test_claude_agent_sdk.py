import asyncio
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from harness.claude_agent_sdk import ClaudeAgentSdkProvider, _sanitized_worker_env
from harness import claude_agent_worker as sdk_worker
from harness.claude_agent_worker import _SDK_ENV, _build_options, _query
from harness.providers import make_provider, provider_default_model


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Sdk:
    ClaudeAgentOptions = _Options


def test_sdk_options_remove_every_foreign_agent_surface():
    options = _build_options(_Sdk, {
        "model": "opus", "system_prompt": "COLLIE SYSTEM", "effort": "high",
    })
    assert options.system_prompt == "COLLIE SYSTEM"
    assert options.setting_sources == []
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.skills == []
    assert options.plugins == []
    assert options.agents == {}
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.max_turns == 1
    assert options.fallback_model is None
    assert options.extra_args == {
        "safe-mode": None,
        "no-session-persistence": None,
        "disable-slash-commands": None,
    }
    assert options.env == _SDK_ENV


def test_worker_environment_drops_auth_provider_proxy_and_tls_but_keeps_windows_paths():
    env = _sanitized_worker_env({
        "PATH": "bin", "ProgramFiles": r"C:\\Program Files",
        "ProgramFiles(x86)": r"C:\\Program Files (x86)", "ProgramW6432": "pf64",
        "ANTHROPIC_API_KEY": "secret", "CLAUDE_CODE_OAUTH_TOKEN": "secret",
        "HTTP_PROXY": "http://proxy", "HTTPS_PROXY": "http://proxy",
        "SSL_CERT_FILE": "cert", "REQUESTS_CA_BUNDLE": "cert",
        "OPENAI_API_KEY": "secret", "NODE_OPTIONS": "--require evil.js",
    })
    assert env["PATH"] == "bin"
    assert env["ProgramFiles"] == r"C:\\Program Files"
    assert env["ProgramFiles(x86)"] == r"C:\\Program Files (x86)"
    assert env["ProgramW6432"] == "pf64"
    assert not any(key in env for key in (
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HTTP_PROXY", "HTTPS_PROXY",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "OPENAI_API_KEY", "NODE_OPTIONS"))


def test_parent_death_group_kill_refuses_changed_or_reused_group(monkeypatch):
    killed = []
    monkeypatch.setattr(sdk_worker.os, "getpgrp", lambda: 7001, raising=False)
    monkeypatch.setattr(sdk_worker.os, "killpg",
                        lambda group, sig: killed.append((group, sig)), raising=False)

    with pytest.raises(RuntimeError, match="ownership changed"):
        sdk_worker._kill_own_process_group(7002)

    assert killed == []


def test_parent_death_argv_keeps_windows_job_path_unchanged(monkeypatch):
    monkeypatch.setattr(sdk_worker.os, "name", "nt")
    assert sdk_worker._arm_parent_death_from_argv([]) is None
    with pytest.raises(RuntimeError, match="Windows worker received POSIX"):
        sdk_worker._arm_parent_death_from_argv([
            "--collie-parent-death-fd", "7", "--collie-parent-pid", "42"])


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "fork"),
                    reason="POSIX process-group crash contract")
def test_posix_parent_pipe_crash_kills_worker_and_spawned_runtime_tree():
    """An abrupt creator death closes the exact-lifetime pipe and reaps its group."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = r'''
import os, subprocess, sys, time
sys.path.insert(0, sys.argv[3])
from harness.claude_agent_worker import _arm_posix_parent_death
read_fd, parent_pid = int(sys.argv[1]), int(sys.argv[2])
_arm_posix_parent_death(read_fd, parent_pid)
runtime = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(os.getpid(), runtime.pid, flush=True)
time.sleep(60)
'''
    supervisor = r'''
import os, subprocess, sys, time
read_fd, write_fd = os.pipe()
worker = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], str(read_fd), str(os.getpid()), sys.argv[2]],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    start_new_session=True, pass_fds=(read_fd,))
os.close(read_fd)
line = worker.stdout.readline()
print(line.strip(), flush=True)
time.sleep(60)
'''
    owner = subprocess.Popen(
        [sys.executable, "-c", supervisor, target, project_root],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    worker_pid = runtime_pid = 0
    try:
        line = owner.stdout.readline().strip()
        worker_pid, runtime_pid = (int(value) for value in line.split())
        assert worker_pid > 1 and runtime_pid > 1
        os.kill(worker_pid, 0)
        os.kill(runtime_pid, 0)

        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                os.killpg(worker_pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                pytest.fail("SDK worker process group survived its exact parent")
            time.sleep(.02)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=5)
        if worker_pid:
            try:
                os.killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _messages(*assistant_ids, init=None, api_source="none"):
    init_data = {
        "tools": [], "skills": [], "plugins": [], "agents": {}, "slash_commands": [],
        "mcp_servers": [], "model": "opus", "apiKeySource": api_source,
    }
    if init:
        init_data.update(init)
    values = [{"type": "system", "subtype": "init", "data": init_data}]
    values += [{"type": "assistant", "id": message_id,
                "content": [{"type": "text", "text": '{"answer":"ok"}'}]}
               for message_id in assistant_ids]
    values.append({"type": "result", "num_turns": 1, "is_error": False,
                   "usage": {"input_tokens": 3, "output_tokens": 2}})
    return values


class _QuerySdk(_Sdk):
    def __init__(self, messages):
        self.messages = messages
        self.options = None
        self.prompt = None

    async def query(self, *, prompt, options):
        self.prompt, self.options = prompt, options
        for message in self.messages:
            yield message


def _run_query(messages):
    sdk = _QuerySdk(messages)
    result = asyncio.run(_query({
        "model": "opus", "system_prompt": "collie", "prompt": "next", "effort": "default",
    }, sdk))
    return result, sdk


def test_worker_accepts_exactly_one_assistant_and_reports_usage():
    result, sdk = _run_query(_messages("assistant-1"))
    assert result == {"ok": True, "text": '{"answer":"ok"}', "usage": {
        "input_tokens": 3, "output_tokens": 2,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }, "api_key_source": "none"}
    assert sdk.prompt == "next"
    assert sdk.options.system_prompt == "collie"


def test_worker_accepts_thinking_and_text_fragments_with_one_message_id():
    messages = _messages("unused")
    messages[1:2] = [
        {"type": "assistant", "message_id": "msg-one",
         "content": [{"type": "thinking", "thinking": "private"}]},
        {"type": "assistant", "message_id": "msg-one",
         "content": [{"type": "text", "text": '{"answer":"ok"}'}]},
    ]

    result, _sdk = _run_query(messages)

    assert result["text"] == '{"answer":"ok"}'


@pytest.mark.parametrize("messages,match", [
    (_messages("a", "b"), "more than one Assistant message"),
    (_messages(""), "missing an id"),
    (_messages("a", init={"tools": ["Read"]}), "non-empty tools"),
    (_messages("a", api_source="ANTHROPIC_API_KEY"), "disallowed API key source"),
    (_messages("a", api_source="subscription"), "disallowed API key source"),
])
def test_worker_fails_closed_on_foreign_harness_or_multiple_assistant_ids(messages, match):
    with pytest.raises(RuntimeError, match=match):
        _run_query(messages)


def test_worker_requires_an_explicit_unambiguous_api_key_source():
    missing = _messages("a")
    del missing[0]["data"]["apiKeySource"]
    with pytest.raises(RuntimeError, match="did not attest an API key source"):
        _run_query(missing)

    ambiguous = _messages("a", init={"api_key_source": "none"})
    with pytest.raises(RuntimeError, match="ambiguous API key sources"):
        _run_query(ambiguous)

    non_string = _messages("a", api_source=None)
    with pytest.raises(RuntimeError, match="invalid API key source"):
        _run_query(non_string)


def test_worker_rejects_duplicate_result_messages():
    messages = _messages("a")
    messages.append({"type": "result", "num_turns": 1, "is_error": False,
                     "usage": {}})
    with pytest.raises(RuntimeError, match="more than one result message"):
        _run_query(messages)


class _Provider(ClaudeAgentSdkProvider):
    def __init__(self, response, **kwargs):
        super().__init__(**kwargs)
        self.response = response
        self.spawned = 0

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.spawned += 1
        self.request = request
        self.cancel_scope = cancel_scope
        return self.response


def test_provider_uses_collie_prompt_and_parses_tool_protocol():
    provider = _Provider({"ok": True, "text": '{"tool":"grep","args":{"pattern":"x"}}',
                          "usage": {"input_tokens": 7, "output_tokens": 4},
                          "api_key_source": "none"})
    completion = provider.complete("COLLIE SYSTEM", [{"role": "user", "content": "fix it"}], [
        {"name": "grep", "description": "search", "input_schema": {
            "type": "object", "properties": {"pattern": {"type": "string"}}}},
    ])
    assert completion.stop_reason == "tool_use"
    assert completion.request_count == 1
    assert completion.tool_calls[0].name == "grep"
    assert completion.api_key_source == "none"
    assert provider.request["system_prompt"] == "COLLIE SYSTEM"
    assert "# Tools the executor can run:" in provider.request["prompt"]


def test_provider_preserves_caller_owned_json_schema_for_mission_planner():
    plan = '{"action":"code","args":{"goal":"fix it"},"reason":"next"}'
    provider = _Provider({"ok": True, "text": plan, "usage": {},
                          "api_key_source": "none"})

    completion = provider.complete(
        "Return an action-shaped JSON object.",
        [{"role": "user", "content": "choose the next action"}], [])

    assert completion.stop_reason == "end_turn"
    assert completion.text == plan
    assert completion.request_count == 1
    assert "# RESPONSE FORMAT (strict):" not in provider.request["prompt"]
    assert "Return an action-shaped JSON object." == provider.request["system_prompt"]


def test_subscription_authority_fails_before_spawn_and_reserves_exactly_once():
    response = {"ok": True, "text": '{"answer":"done"}', "usage": {},
                "api_key_source": "none"}
    provider = _Provider(response, subscription_only=True)
    denied = provider.complete("s", [{"role": "user", "content": "u"}], [])
    assert denied.stop_reason == "error"
    assert denied.request_count == 0
    assert provider.spawned == 0

    events = []
    provider.request_gate = lambda kind: events.append(("reserve", kind)) or "req-1"
    provider.request_complete = lambda request_id, status: events.append(
        ("complete", request_id, status))
    result = provider.complete("s", [{"role": "user", "content": "u"}], [])
    assert result.text == "done"
    assert provider.spawned == 1
    assert events == [("reserve", "claude_agent_sdk"),
                      ("complete", "req-1", "completed")]


def test_mission_request_scope_reaches_the_physical_worker():
    provider = _Provider({"ok": True, "text": '{"answer":"done"}', "usage": {},
                          "api_key_source": "none"}, subscription_only=True)
    with provider.request_authority(
            lambda _kind: "req-1", lambda *_args: None,
            request_scope="msn_scoped"):
        result = provider.complete("s", [{"role": "user", "content": "u"}], [])

    assert result.text == "done"
    assert provider.cancel_scope == "msn_scoped"


def test_worker_is_owned_before_prompt_release_and_cancel_is_scoped(monkeypatch):
    from harness import claude_agent_sdk as transport
    from harness import plat

    entered = threading.Event()
    released = threading.Event()
    events = []

    class Proc:
        pid = 4242
        returncode = None

        def communicate(self, input=None, timeout=None):
            events.append(("communicate", input))
            entered.set()
            released.wait(2)
            self.returncode = -9
            return None, None

        def wait(self, timeout=None):
            released.wait(timeout)
            if not released.is_set():
                raise TimeoutError
            self.returncode = -9
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9
            released.set()

    class Owner:
        def close(self):
            events.append(("owner-close", None))

    proc = Proc()
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(plat, "new_group_kwargs",
                        lambda: {"start_new_session": True})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(
        plat, "attach_kill_on_close_job",
        lambda child: events.append(("attach", child.pid)) or Owner())

    def terminate(child, timeout_s=5.0):
        events.append(("terminate", child.pid))
        child.returncode = -9
        released.set()
        return True

    monkeypatch.setattr(transport, "_terminate_worker_tree", terminate)
    provider = ClaudeAgentSdkProvider()
    errors = []
    thread = threading.Thread(
        target=lambda: _capture_error(
            errors, lambda: provider._run_worker(
                {"protocol": 1}, cancel_scope="msn_one")),
        daemon=True)
    thread.start()
    assert entered.wait(1)
    assert provider.cancel_for("msn_other")() is False
    assert provider.cancel_for("msn_one")() is True
    thread.join(2)

    assert not thread.is_alive()
    assert events.index(("attach", 4242)) < next(
        i for i, event in enumerate(events) if event[0] == "communicate")
    assert any(event[0] == "terminate" for event in events)
    assert any(event[0] == "owner-close" for event in events)
    assert errors


def test_scoped_cancel_fences_reserved_call_during_prompt_gap(monkeypatch):
    from harness import claude_agent_sdk as transport

    prompt_entered = threading.Event()
    prompt_release = threading.Event()
    popen_calls = []
    completions = []
    authority_events = []

    provider = ClaudeAgentSdkProvider(subscription_only=True)

    def blocking_prompt(_messages):
        prompt_entered.set()
        assert prompt_release.wait(2)
        return "prompt"

    def forbidden_spawn(*_args, **_kwargs):
        popen_calls.append(True)
        raise AssertionError("a cancelled pending call crossed the worker boundary")

    monkeypatch.setattr(provider, "_plain_prompt", blocking_prompt)
    monkeypatch.setattr(transport.subprocess, "Popen", forbidden_spawn)

    def invoke():
        with provider.request_authority(
                lambda purpose: authority_events.append(
                    ("reserve", purpose)) or "req-gap",
                lambda request_id, status: authority_events.append(
                    ("complete", request_id, status)),
                request_scope="msn_gap"):
            completions.append(provider.complete(
                "system", [{"role": "user", "content": "work"}], []))

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    assert prompt_entered.wait(1)
    assert authority_events == [("reserve", "claude_agent_sdk")]

    # The unrelated Mission remains isolated, while cancellation of the
    # reserved-but-not-yet-started call proves process-freedom immediately.
    assert provider.cancel_for("msn_other")() is False
    started = time.monotonic()
    assert provider.cancel_for("msn_gap")() is True
    assert time.monotonic() - started < 0.5
    assert popen_calls == []

    # Once cancel_for has returned, releasing prompt construction must consume
    # the pending tombstone instead of starting the physical SDK worker.
    prompt_release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert popen_calls == []
    assert len(completions) == 1
    assert completions[0].stop_reason == "error"
    assert "cancelled" in completions[0].error_detail
    assert authority_events == [
        ("reserve", "claude_agent_sdk"),
        ("complete", "req-gap", "error"),
    ]
    assert provider._active_runs == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor regression")
def test_scoped_cancel_before_start_does_not_create_parent_death_pipe(monkeypatch):
    from harness import claude_agent_sdk as transport

    prompt_entered = threading.Event()
    prompt_release = threading.Event()
    pipe_calls = []
    provider = ClaudeAgentSdkProvider(subscription_only=True)

    def blocking_prompt(_messages):
        prompt_entered.set()
        assert prompt_release.wait(2)
        return "prompt"

    monkeypatch.setattr(provider, "_plain_prompt", blocking_prompt)
    monkeypatch.setattr(
        transport.os, "pipe",
        lambda: pipe_calls.append(True) or (_ for _ in ()).throw(
            AssertionError("cancelled pending call created an ownership pipe")))

    result = []

    def invoke():
        with provider.request_authority(
                lambda _purpose: "req-posix-gap", lambda *_args: None,
                request_scope="msn_posix_gap"):
            result.append(provider.complete(
                "system", [{"role": "user", "content": "work"}], []))

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    assert prompt_entered.wait(1)
    assert provider.cancel_for("msn_posix_gap")() is True
    prompt_release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert pipe_calls == []
    assert result[0].stop_reason == "error"
    assert "cancelled" in result[0].error_detail
    assert provider._active_runs == {}


def test_posix_parent_death_pipe_is_closed_when_spawn_fails(monkeypatch):
    from harness import claude_agent_sdk as transport
    from harness import plat

    closed = []
    fake_fds = (501, 502)
    real_close = transport.os.close

    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(plat, "new_group_kwargs",
                        lambda: {"start_new_session": True})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(transport.os, "pipe", lambda: fake_fds)
    monkeypatch.setattr(
        transport.os, "close",
        lambda fd: closed.append(fd) if fd in fake_fds else real_close(fd))
    monkeypatch.setattr(
        transport.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")))

    provider = ClaudeAgentSdkProvider()
    with pytest.raises(OSError, match="spawn failed"):
        provider._run_worker({"protocol": 1}, cancel_scope="msn_spawn_fail")

    assert sorted(closed) == sorted(fake_fds)
    assert provider._active_runs == {}


def test_parent_death_fd_close_error_still_retires_worker_state(monkeypatch):
    from harness import claude_agent_sdk as transport
    from harness import plat

    fake_fds = (601, 602)
    close_attempts = []
    real_close = transport.os.close

    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(plat, "new_group_kwargs",
                        lambda: {"start_new_session": True})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(transport.os, "pipe", lambda: fake_fds)

    def close_once(fd):
        if fd not in fake_fds:
            return real_close(fd)
        close_attempts.append(fd)
        if fd == fake_fds[1]:
            raise InterruptedError("injected close EINTR")

    monkeypatch.setattr(transport.os, "close", close_once)
    monkeypatch.setattr(
        transport.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")))

    provider = ClaudeAgentSdkProvider()
    with pytest.raises(OSError, match="spawn failed"):
        provider._run_worker({"protocol": 1}, cancel_scope="msn_close_eintr")

    # The original spawn failure remains primary, but both descriptors were
    # attempted exactly once and the call can no longer be observed/cancelled.
    assert sorted(close_attempts) == sorted(fake_fds)
    assert provider._active_runs == {}


def test_parent_death_read_close_eintr_is_not_retried(monkeypatch):
    from harness import claude_agent_sdk as transport
    from harness import plat

    fake_fds = (701, 702)
    close_attempts = []
    real_close = transport.os.close

    class Proc:
        pid = 4243
        returncode = -1

        def communicate(self, input=None, timeout=None):
            return None, None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(plat, "new_group_kwargs",
                        lambda: {"start_new_session": True})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(plat, "attach_kill_on_close_job", lambda _proc: None)
    monkeypatch.setattr(transport, "_terminate_worker_tree", lambda _proc: True)
    monkeypatch.setattr(transport.os, "pipe", lambda: fake_fds)
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *_a, **_k: Proc())

    def close_once(fd):
        if fd not in fake_fds:
            return real_close(fd)
        close_attempts.append(fd)
        if fd == fake_fds[0]:
            raise InterruptedError("injected read close EINTR")

    monkeypatch.setattr(transport.os, "close", close_once)

    provider = ClaudeAgentSdkProvider()
    with pytest.raises(RuntimeError, match="descriptor cleanup failed"):
        provider._run_worker({"protocol": 1}, cancel_scope="msn_read_eintr")

    assert close_attempts.count(fake_fds[0]) == 1
    assert close_attempts.count(fake_fds[1]) == 1
    assert provider._active_runs == {}


def _capture_error(target, fn):
    try:
        fn()
    except BaseException as exc:
        target.append(exc)


@pytest.mark.parametrize("source", [None, "oauth", "subscription", "ANTHROPIC_API_KEY"])
def test_provider_rejects_missing_or_unreviewed_worker_auth_attestation(source):
    response = {"ok": True, "text": '{"answer":"done"}', "usage": {}}
    if source is not None:
        response["api_key_source"] = source
    provider = _Provider(response)

    completion = provider.complete("s", [{"role": "user", "content": "u"}], [])

    assert completion.stop_reason == "error"
    assert "auth attestation" in completion.error_detail


def test_factory_is_lazy_and_threads_subscription_only():
    assert provider_default_model("claude-agent-sdk") == "claude-opus-4-8"
    provider = make_provider("claude-agent-sdk", subscription_only=True)
    assert isinstance(provider, ClaudeAgentSdkProvider)
    assert provider.subscription_only is True
