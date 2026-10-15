"""Contracts remain useful without installing or constructing feature adapters."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_old_exports_keep_one_class_and_receipt_identity():
    from harness import model_contracts as model, providers
    from harness import tool_contracts as tool, tools
    from harness import context, context_contracts

    for name in ("ToolCall", "Usage", "Completion", "ModelProvider", "content_text",
                 "issued_requests", "classify_error", "_error_completion"):
        assert getattr(providers, name) is getattr(model, name)
    for name in ("Tool", "ToolCtx", "ToolRegistry", "ExecReceipt", "ToolResult",
                 "exec_receipt", "repair_args"):
        assert getattr(tools, name) is getattr(tool, name)
    assert context.ComposeMeta is context_contracts.ComposeMeta
    assert context.TokenBudgeter is context_contracts.TokenBudgeter

    receipt = tool.ExecReceipt("bash", "call", "synthetic command", edit_rc=0)
    result = tools.ToolResult("synthetic result", receipt)
    assert tool.exec_receipt(result) is receipt
    assert tools.exec_receipt(result) is receipt
    assert tool.exec_receipt({"receipt": receipt}) is None


def test_request_authority_stays_bound_to_one_execution_context():
    from concurrent.futures import ThreadPoolExecutor
    from harness.model_contracts import ModelProvider

    provider, another = ModelProvider(), ModelProvider()
    gate = object()

    def worker():
        assert provider.current_request_authority() == (None, None)
        with provider.request_authority(gate, request_scope="worker"):
            assert provider.current_request_authority() == (gate, None)
            assert provider.current_request_scope() == "worker"
            assert another.current_request_scope() == ""
        assert provider.current_request_scope() == ""

    with provider.request_authority(gate, request_scope="caller"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(worker).result()
        assert provider.current_request_scope() == "caller"
    assert provider.current_request_authority() == (None, None)


def test_replay_safe_check_uses_exact_implementation_not_names_or_subclasses():
    from harness.tool_contracts import replay_safe_builtin
    from harness.tools import ReadFileTool

    class Subclass(ReadFileTool):
        pass

    class NamedLikeARead:
        name = "read_file"
        readOnlyHint = True

    assert replay_safe_builtin(ReadFileTool()) is True
    assert replay_safe_builtin(Subclass()) is False
    assert replay_safe_builtin(NamedLikeARead()) is False


def test_resolving_default_data_never_rescues_or_moves_legacy_files(tmp_path, monkeypatch):
    from harness.paths import data_dir, runtime_paths

    for key in ("COLLIE_DATA_DIR", "COLLIE_STATE_DIR"):
        monkeypatch.delenv(key, raising=False)
    user = tmp_path / "user"
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("HOME", str(user))
    installed = tmp_path / "installed"
    legacy = installed / "data"
    legacy.mkdir(parents=True)
    marker = legacy / "synthetic.txt"
    marker.write_text("retained", encoding="utf-8")

    target = Path(data_dir(str(installed)))
    assert target == user / ".collie" / "data"
    assert marker.read_text(encoding="utf-8") == "retained"
    assert not target.exists()
    runtime_paths(str(target), create=False)
    assert not target.exists()


def test_kernel_can_run_and_read_a_journal_with_features_unavailable(tmp_path):
    """An import blocker catches hidden dependencies; this is a mock-only task."""
    root = Path(__file__).resolve().parents[1]
    isolated = tmp_path / "isolation"
    isolated.mkdir()
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith(("CODEX_", "COLLIE_", "ANTHROPIC_", "OPENAI_", "CLAUDE_",
                             "AWS_", "AZURE_", "GOOGLE_", "GEMINI_", "OLLAMA_")):
            continue
        if any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            continue
        env[key] = value
    env.update(HOME=str(isolated), USERPROFILE=str(isolated),
               COLLIE_STATE_DIR=str(isolated / "state"),
               COLLIE_DATA_DIR=str(isolated / "data"),
               COLLIE_SESSIONS_DIR=str(isolated / "sessions"),
               COLLIE_EMBED="bm25", COLLIE_BROWSER_BRIDGE="0")
    code = textwrap.dedent("""
        import importlib.abc, json, pathlib, socket, sys
        sys.path.insert(0, sys.argv[1])
        forbidden = (
            'harness.providers', 'harness.tools', 'harness.memory', 'harness.cli',
            'harness.context', 'harness.native', 'harness.browserbridge',
            'harness.mcpclient', 'harness.live_copilot', 'harness.agent_runners',
            'harness.claude_agent_sdk', 'harness.codex_oauth', 'harness.delegate',
            'harness.runner_registry', 'harness.codex_app_server_runner',
        )
        class NoFeatures(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + '.')
                       for name in forbidden):
                    raise ImportError('feature unavailable: ' + fullname)
                return None
        sys.meta_path.insert(0, NoFeatures())
        def deny_network(*args, **kwargs):
            raise AssertionError('network is forbidden in this fixture')
        socket.create_connection = deny_network
        socket.socket.connect = deny_network
        from harness.model_contracts import ModelProvider, Completion
        from harness.tool_contracts import ToolRegistry
        from harness.context_contracts import ComposeMeta, TokenBudgeter
        from harness.recorder import Recorder
        from harness.loop import Harness
        from harness import sessions
        class Canned(ModelProvider):
            name = 'mock'
            model = 'mock'
            def complete(self, system, messages, tool_schemas, on_text=None):
                return Completion(text='Synthetic task completed.')
        class Composer:
            budgeter = TokenBudgeter()
            def build(self, session, user_msg, cwd, project, mode='act'):
                return 'Synthetic system', session['messages'], ComposeMeta()
        class Memory:
            pass
        harness = Harness(Canned(), Memory(), ToolRegistry(), Composer(),
                          Recorder(':memory:'), cwd=sys.argv[2], self_verify=False)
        result = harness.run('contract-fixture', 'synthetic task')
        assert not result.error, result.error
        assert result.answer == 'Synthetic task completed.'
        sessions.save('contract-journal', result.messages, cwd=sys.argv[2])
        loaded = sessions.load_checked('contract-journal')
        assert loaded['status'] == 'ok', loaded['status']
        assert not any(name in sys.modules for name in forbidden)
        print(json.dumps({'answer': result.answer, 'journal': loaded['status']}))
    """)
    completed = subprocess.run([sys.executable, "-I", "-c", code, str(root), str(isolated)],
                               cwd=str(isolated), env=env, capture_output=True,
                               text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert '"journal": "ok"' in completed.stdout
