import io
import json
import os
import shutil
import subprocess

import pytest

from harness.automations import (AutomationExecutor, AutomationSpec, AutomationStore,
                                 BudgetExceeded, DefaultCollieRunner,
                                 FilePredicateTrigger, NEEDS_YOU, PENDING,
                                 PagePredicateTrigger, PermissionDenied,
                                 PermissionPolicy, SUCCEEDED, TriggerEngine,
                                 TriggerRegistry, WorkspaceAllocator, _LimitedTool,
                                 _unscopable_unattended_tool)
from harness.ops import OpsStore


def _spec(aid, trigger, root, **extra):
    value = {
        "id": aid, "task": "inspect and report", "trigger": trigger,
        "workspace": {"mode": "isolated"},
        "permissions": {"read_roots": [str(root)]},
    }
    value.update(extra)
    return value


def test_policy_validation_requires_explicit_continuation_and_current_workspace(tmp_path):
    with pytest.raises(ValueError, match="session_id"):
        AutomationSpec.from_dict(_spec(
            "bad", {"provider": "timer", "every_s": 10}, tmp_path,
            context={"policy": "continued"}))
    with pytest.raises(ValueError, match="current_workspace"):
        AutomationSpec.from_dict(_spec(
            "bad2", {"provider": "timer", "every_s": 10}, tmp_path,
            workspace={"mode": "current"}))


def test_timer_is_durable_idempotent_and_context_policy_is_snapshotted(tmp_path):
    with AutomationStore(str(tmp_path / "automations.db")) as store:
        store.upsert(_spec("clock", {
            "provider": "timer", "every_s": 10, "fire_immediately": True,
        }, tmp_path, context={"policy": "continued", "session_id": "daily-thread"}), now=100)
        engine = TriggerEngine(store)
        first = engine.tick(100)
        assert len(first) == 1 and engine.tick(101) == []
        request = json.loads(store.executions()[0]["request_json"])
        assert request["context"] == {"policy": "continued", "session_id": "daily-thread"}
        assert engine.tick(135) and len(store.executions()) == 2  # one catch-up, not four runs
        audit = store.audit_log("clock")
        assert any(row["event"] == "configuration" for row in audit)


def test_file_trigger_is_edge_based_and_permission_bounded(tmp_path):
    watched = tmp_path / "ready.txt"
    registry = TriggerRegistry()
    provider = registry.get("file")
    spec = AutomationSpec.from_dict(_spec("file-watch", {
        "provider": "file", "path": str(watched),
        "predicate": {"type": "contains", "value": "READY"},
    }, tmp_path))
    cursor = provider.evaluate(spec, {}, 1).cursor
    watched.write_text("not yet", encoding="utf-8")
    check = provider.evaluate(spec, cursor, 2)
    assert not check.fired
    watched.write_text("READY", encoding="utf-8")
    fired = provider.evaluate(spec, check.cursor, 3)
    assert fired.fired and "READY" not in json.dumps(fired.event)
    assert not provider.evaluate(spec, fired.cursor, 4).fired

    denied = AutomationSpec.from_dict(_spec("denied", {
        "provider": "file", "path": str(tmp_path.parent / "outside.txt")}, tmp_path))
    with pytest.raises(PermissionDenied):
        provider.evaluate(denied, {}, 1)


def test_page_predicate_requires_host_allowlist_and_never_persists_body(tmp_path):
    class Response(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): self.close()

    provider = PagePredicateTrigger(opener=lambda request, timeout: Response(b"release READY"))
    value = _spec("page", {
        "provider": "page", "url": "https://example.com/status",
        "predicate": {"type": "contains", "value": "READY"},
        "fire_on_initial": True,
    }, tmp_path)
    value["permissions"]["network_hosts"] = ["example.com"]
    spec = AutomationSpec.from_dict(value)
    result = provider.evaluate(spec, {}, 1)
    assert result.fired and "release READY" not in json.dumps(result.event)

    value["permissions"]["network_hosts"] = []
    with pytest.raises(PermissionDenied):
        provider.evaluate(AutomationSpec.from_dict(value), {}, 1)


def test_webhook_requires_authenticated_permission_and_persists_allowlisted_fields(tmp_path):
    with AutomationStore(str(tmp_path / "automations.db")) as store:
        value = _spec("hook", {
            "provider": "webhook", "predicate": {"field": "event", "equals": "deploy"},
            "persist_fields": ["event", "project"],
        }, tmp_path)
        value["permissions"]["webhook_ingest"] = True
        store.upsert(value, now=1)
        engine = TriggerEngine(store)
        with pytest.raises(PermissionDenied):
            engine.ingest_webhook("hook", {"event": "deploy"}, authenticated=False, now=2)
        assert engine.ingest_webhook(
            "hook", {"event": "other", "secret": "never"}, authenticated=True, now=3) is None
        eid = engine.ingest_webhook(
            "hook", {"event": "deploy", "project": "collie", "secret": "never"},
            authenticated=True, delivery_id="delivery-1", now=4)
        assert eid
        persisted = store.executions()[0]["request_json"]
        assert "collie" in persisted and "never" not in persisted
        assert engine.ingest_webhook(
            "hook", {"event": "deploy"}, authenticated=True,
            delivery_id="delivery-1", now=5) is None


def test_executor_isolated_workspace_budget_permission_audit_and_notifications(tmp_path):
    automation_db = tmp_path / "automations.db"
    ops_db = tmp_path / "ops.db"
    with AutomationStore(str(automation_db)) as store, OpsStore(str(ops_db)) as notifications:
        value = _spec("run", {
            "provider": "timer", "every_s": 10, "fire_immediately": True,
        }, tmp_path, notifications=["start", "success"],
            budget={"max_model_tokens": 10, "max_actions": 2, "max_wall_s": 60,
                    "max_cost_usd": 1, "max_runs_per_day": 5, "max_retries": 0})
        value["permissions"].update({"tools": ["read_file"],
                                     "write_roots": [str(tmp_path)]})
        store.upsert(value, now=10)
        TriggerEngine(store).tick(10)

        def runner(request, guard):
            assert os.path.isdir(request["resolved_workspace"])
            assert guard.authority.tool("read_file") == "read_file"
            with pytest.raises(PermissionDenied):
                guard.authority.tool("shell")
            guard.consume(model_tokens=5, actions=1)
            return {"status": "succeeded", "summary": "done"}

        executor = AutomationExecutor(
            store, runner, workspace_allocator=WorkspaceAllocator(str(tmp_path / "workspaces")),
            notification_store=notifications)
        assert executor.step(now=11) == SUCCEEDED
        assert store.executions()[0]["state"] == SUCCEEDED
        decisions = [row["decision"] for row in store.audit_log("run")
                     if row["event"] == "permission"]
        assert {"allowed", "denied"} <= set(decisions)
        assert notifications.notification_stats()["pending"] == 2


def test_budget_exhaustion_and_crash_recovery_park_external_writes(tmp_path):
    with AutomationStore(str(tmp_path / "automations.db")) as store:
        value = _spec("external", {
            "provider": "timer", "every_s": 10, "fire_immediately": True,
        }, tmp_path, budget={"max_model_tokens": 1, "max_actions": 1, "max_wall_s": 60,
                            "max_cost_usd": 1, "max_runs_per_day": 5, "max_retries": 2})
        value["permissions"]["external_writes"] = True
        store.upsert(value, now=1)
        TriggerEngine(store).tick(1)
        claimed = store.claim(now=2, lease_s=1)
        assert claimed
        assert store.recover_expired(now=4) == 1
        assert store.executions()[0]["state"] == NEEDS_YOU

    guard_store = AutomationStore(str(tmp_path / "budget.db"))
    try:
        guard_store.upsert(_spec("budget", {
            "provider": "timer", "every_s": 1, "fire_immediately": True,
        }, tmp_path, budget={"max_model_tokens": 1}), now=1)
        TriggerEngine(guard_store).tick(1)
        request = json.loads(guard_store.executions()[0]["request_json"])
        from harness.automations import BudgetGuard
        guard = BudgetGuard(guard_store, request["execution_id"], request["budget"],
                            request=request)
        with pytest.raises(BudgetExceeded):
            guard.consume(model_tokens=2)
    finally:
        guard_store.close()


def test_default_tool_wrapper_denies_out_of_root_and_unsafe_ambient_tools(tmp_path):
    class Tool:
        name = "read_file"
        description = "read"
        tier = "safe"
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        called = 0

        def provider_schema(self):
            return self.schema

        def run(self, args, ctx):
            self.called += 1
            return "secret"

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    tool = Tool()
    with AutomationStore(str(tmp_path / "authority.db")) as store:
        wrapped = _LimitedTool(
            tool, float("inf"), {"actions": 0, "cancelled": False}, 3,
            PermissionPolicy.from_dict({"read_roots": [str(workspace)]}),
            str(workspace), store, "safe", "exec-1")
        result = wrapped.run({"path": str(outside)}, None)
        assert "permission denied" in result.lower()
        assert tool.called == 0
        denied = [row for row in store.audit_log("safe")
                  if row["event"] == "permission" and row["decision"] == "denied"]
        assert denied

    assert _unscopable_unattended_tool("bash")
    assert _unscopable_unattended_tool("browser_click")
    assert _unscopable_unattended_tool("mcp__github__write")
    assert not _unscopable_unattended_tool("read_file")


def test_default_runner_hard_wall_kills_child_and_preserves_budget_type(tmp_path, monkeypatch):
    class HungProcess:
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("automation", timeout)

    killed = []
    monkeypatch.setattr("harness.automations.subprocess.Popen", lambda *a, **k: HungProcess())
    monkeypatch.setattr("harness.plat.kill_tree", lambda proc: killed.append(proc))
    with AutomationStore(str(tmp_path / "automations.db")) as store:
        store.upsert(_spec("wall", {
            "provider": "timer", "every_s": 1, "fire_immediately": True,
        }, tmp_path, execution={"provider": "mock", "allow_mock": True},
            budget={"max_wall_s": .2}), now=1)
        TriggerEngine(store).tick(1)
        request = json.loads(store.executions()[0]["request_json"])
        request["resolved_workspace"] = str(tmp_path)
        from harness.automations import BudgetGuard
        guard = BudgetGuard(store, request["execution_id"], request["budget"], request=request)
        with pytest.raises(BudgetExceeded, match="hard wall-time"):
            DefaultCollieRunner()(request, guard)
        assert killed


def test_isolated_source_workspace_is_a_real_git_worktree(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git is unavailable")
    source = tmp_path / "source"
    source.mkdir()
    for args in (("init",), ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "Collie Test")):
        subprocess.run(["git", *args], cwd=source, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (source / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "base"], cwd=source, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    request = {
        "execution_id": "exec-worktree", "automation_id": "isolated",
        "workspace": {"mode": "isolated", "source": str(source)},
        "permissions": {"read_roots": [str(source)], "write_roots": [str(source)]},
    }
    prepared = WorkspaceAllocator(str(tmp_path / "empty-workspaces")).prepare(request)
    try:
        assert os.path.isdir(os.path.join(prepared, ".git")) or os.path.isfile(
            os.path.join(prepared, ".git"))
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=prepared, check=True,
            capture_output=True, text=True).stdout.strip()
        assert branch.startswith("collie/") and request["workspace_branch"] == branch
        assert os.path.realpath(prepared) != os.path.realpath(source)
    finally:
        from harness import worktree
        worktree.release(prepared, force=True)
