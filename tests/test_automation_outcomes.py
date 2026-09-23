"""What an unattended automation is allowed to CALL a finished run.

Every case below drives the real child-process body (`_run_collie_request`) in-process
against the real `Harness`/`RunResult`, with the same environment ceilings
`DefaultCollieRunner` exports for its child (`COLLIE_MAX_TURNS`,
`COLLIE_MAX_TOTAL_TOKENS`).  Nobody watches an automation run, so "succeeded" is the
one word a reader trusts without opening the transcript: it has to mean the host saw
the task finish AND the thread it points at can be read back.
"""
import json
import os

import pytest

from harness import automations, providers, sessions
from harness.automations import FAILED, NEEDS_YOU, SUCCEEDED


def _isolate(tmp_path, monkeypatch, **env):
    """Point session/state storage at this test's temp dir, then apply budget ceilings.

    COLLIE_SESSIONS_DIR is not optional: `sessions._dir` otherwise falls back to
    `cli.DATA`, which is computed at import time, so under the full suite (where another
    module already imported `harness.cli`) COLLIE_STATE_DIR alone arrives too late and
    these runs could write outside their temporary test directory.
    """
    state = tmp_path / "state"
    (state / "notes").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "data" / "sessions"))
    monkeypatch.setenv("COLLIE_NOTES_DIR", str(state / "notes"))
    monkeypatch.setenv("COLLIE_EMBED", "bm25")
    from harness import cli
    monkeypatch.setattr(cli, "DATA", str(state / "data"))
    for key in ("COLLIE_MAX_TURNS", "COLLIE_MAX_TOTAL_TOKENS", "COLLIE_MAX_COST"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "a.py").write_text("# TODO: fix\n", encoding="utf-8")
    return workspace


def _request(tmp_path, **overrides):
    workspace = _workspace(tmp_path)
    request = {
        "automation_id": "outcomes", "execution_id": "exec-1",
        "task": "list every todo in this workspace",
        "resolved_workspace": str(workspace),
        "execution": {"provider": "mock", "allow_mock": True, "mode": "project",
                      "project": "outcomes"},
        "budget": {"max_turns": 50, "max_actions": 8, "max_wall_s": 60,
                   "max_model_tokens": 200000},
        "permissions": {"tools": ["grep"], "read_roots": [str(workspace)]},
        "context": {},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(request.get(key), dict):
            request[key] = dict(request[key], **value)
        else:
            request[key] = value
    return request


def _loop_forever(monkeypatch):
    """Make the model keep calling a tool, so only the host's turn ceiling can stop it."""
    from harness import cli

    real = cli.make_harness

    def patched(*args, **kwargs):
        harness = real(*args, **kwargs)

        def complete(system, messages, tool_schemas, on_text=None):
            return providers.Completion(
                tool_calls=[providers.ToolCall("call-%d" % len(messages), "grep",
                                               {"pattern": "TODO", "path": "."})],
                usage=providers.Usage(input_tokens=10, output_tokens=5),
                stop_reason="tool_use")

        harness.provider.complete = complete
        return harness

    monkeypatch.setattr(cli, "make_harness", patched)


class _LegacyResult:
    """A third-party/adapter result from before RunResult grew stop bookkeeping."""

    def __init__(self, answer="legacy answer"):
        self.answer, self.error, self.messages = answer, "", [
            {"role": "user", "content": "task"}, {"role": "assistant", "content": answer}]


def _fixed_result(monkeypatch, result):
    from harness import cli

    real = cli.make_harness

    def patched(*args, **kwargs):
        harness = real(*args, **kwargs)
        harness.run = lambda *a, **kw: result
        return harness

    monkeypatch.setattr(cli, "make_harness", patched)


def test_a_run_stopped_at_its_token_budget_is_not_reported_as_success(tmp_path, monkeypatch):
    """The summary says "stopped at budget"; the status must not say the opposite."""
    _isolate(tmp_path, monkeypatch, COLLIE_MAX_TOTAL_TOKENS=1)
    value = automations._run_collie_request(
        _request(tmp_path, budget={"max_model_tokens": 1}))

    assert value["status"] == NEEDS_YOU, value
    assert value["stop_reason"] == "budget_limit", value
    assert "budget" in value["error"], value
    # The partial work still has to reach the person who now has to decide about it.
    assert "budget" in value["summary"], value
    assert value["session_saved"] is True
    assert sessions.load(value["session_id"]), "the partial thread must remain resumable"


def test_a_run_that_ran_out_of_turns_is_not_reported_as_success(tmp_path, monkeypatch):
    """`turns_exhausted` carries no error text, and that is not the same as finishing."""
    _isolate(tmp_path, monkeypatch, COLLIE_MAX_TURNS=2)
    _loop_forever(monkeypatch)
    value = automations._run_collie_request(_request(tmp_path, budget={"max_turns": 2}))

    assert value["status"] == NEEDS_YOU, value
    assert value["stop_reason"] == "turn_limit", value
    assert "UNFINISHED" in value["summary"], value
    assert value["session_saved"] is True
    assert sessions.load(value["session_id"])


def test_an_answer_cut_off_at_the_output_limit_is_not_reported_as_success(tmp_path, monkeypatch):
    """A reply the provider truncated mid-sentence is a partial deliverable, not a result."""
    from harness import cli

    _isolate(tmp_path, monkeypatch)
    real = cli.make_harness

    def patched(*args, **kwargs):
        harness = real(*args, **kwargs)
        harness.provider.complete = lambda system, messages, schemas, on_text=None: (
            providers.Completion(text="the todo list begins with",
                                 usage=providers.Usage(input_tokens=10, output_tokens=5),
                                 stop_reason="length"))
        return harness

    monkeypatch.setattr(cli, "make_harness", patched)
    value = automations._run_collie_request(_request(tmp_path))

    assert value["status"] == NEEDS_YOU, value
    assert value["stop_reason"] == "output_limit", value
    assert "the todo list begins with" in value["summary"], value
    assert value["session_saved"] is True


def test_an_ordinary_finished_run_still_succeeds(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    value = automations._run_collie_request(_request(tmp_path))

    assert value["status"] == SUCCEEDED, value
    assert value["error"] == "" and value["stop_reason"] == "completed"
    assert value["session_saved"] is True
    assert "TODO" in value["summary"], value
    assert sessions.load(value["session_id"])


def test_a_host_error_is_still_a_failure_not_a_review_request(tmp_path, monkeypatch):
    from harness.recorder import RunResult

    _isolate(tmp_path, monkeypatch)
    result = RunResult(answer="partial", error="provider transport died",
                       messages=[{"role": "user", "content": "task"}])
    _fixed_result(monkeypatch, result)
    value = automations._run_collie_request(_request(tmp_path))

    assert value["status"] == FAILED, value
    assert value["error"] == "provider transport died"
    assert value["stop_reason"] == "error"


def test_the_action_budget_cancel_path_keeps_its_existing_verdict(tmp_path, monkeypatch):
    """`_LimitedTool` exhaustion already asked for a person; that wording stays."""
    _isolate(tmp_path, monkeypatch)
    _loop_forever(monkeypatch)
    value = automations._run_collie_request(_request(tmp_path, budget={"max_actions": 2}))

    assert value["status"] == NEEDS_YOU, value
    assert value["error"] == "automation wall/action budget exhausted"
    assert value["stop_reason"] == "canceled", value
    assert value["session_saved"] is True


def test_a_result_object_without_stop_bookkeeping_is_still_a_success(tmp_path, monkeypatch):
    """Absent fields are a legacy contract, not evidence that a run stopped early.

    `RunResult.success` defaults to False, so reading it instead of the canonical stop
    reason would turn every such adapter's finished run into a false failure.
    """
    _isolate(tmp_path, monkeypatch)
    _fixed_result(monkeypatch, _LegacyResult())
    value = automations._run_collie_request(_request(tmp_path))

    assert value["status"] == SUCCEEDED, value
    assert value["stop_reason"] == "completed"
    assert value["summary"] == "legacy answer"
    assert value["session_saved"] is True


def test_a_transcript_that_did_not_persist_cannot_be_called_a_success(tmp_path, monkeypatch):
    """No durable thread means the next continued run has nothing to resume."""
    _isolate(tmp_path, monkeypatch)

    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sessions, "save", refuse)
    value = automations._run_collie_request(_request(tmp_path))

    assert value["status"] == NEEDS_YOU, value
    assert value["session_saved"] is False, value
    assert "transcript" in value["error"] and "disk full" in value["error"], value
    # "completed" would claim the whole pipeline finished when only the model work did.
    assert value["stop_reason"] != "completed", value
    # The run's own work is evidence a person needs; losing it is a second failure.
    assert "TODO" in value["summary"], value


def test_a_silently_unwritten_transcript_is_reported_too(tmp_path, monkeypatch):
    """`sessions.save` returns the id it was handed even when it wrote no file at all.

    An id the session store refuses to map to a path is that silent no-op, so the
    receipt is only allowed to claim a resumable thread after reading one back.
    """
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(sessions, "new_id", lambda: "not/a/storable/id")
    value = automations._run_collie_request(_request(tmp_path))

    assert value["status"] != SUCCEEDED, value
    assert value["session_saved"] is False, value
    assert "transcript did not persist" in value["error"], value


def test_a_boundary_outcome_is_terminal_and_never_silently_re_runs(tmp_path):
    """NEEDS_YOU parks the execution: no second attempt, no duplicated tool effects."""
    from harness.automations import (AutomationExecutor, AutomationStore, TriggerEngine,
                                     WorkspaceAllocator)
    from harness.ops import OpsStore

    attempts = []
    with AutomationStore(str(tmp_path / "automations.db")) as store, \
            OpsStore(str(tmp_path / "ops.db")) as notifications:
        value = {
            "id": "boundary", "task": "inspect and report",
            "trigger": {"provider": "timer", "every_s": 10, "fire_immediately": True},
            "workspace": {"mode": "isolated"},
            "permissions": {"read_roots": [str(tmp_path)], "tools": ["grep"]},
            "notifications": ["needs_you", "success"],
            "budget": {"max_retries": 2},
        }
        store.upsert(value, now=1)
        TriggerEngine(store).tick(1)

        def runner(request, guard):
            attempts.append(request["execution_id"])
            return {"status": NEEDS_YOU, "stop_reason": "turn_limit",
                    "error": "automation stopped at its turn limit before finishing",
                    "summary": "(ran out of turns - UNFINISHED)",
                    "session_id": "sess-1", "session_saved": True}

        executor = AutomationExecutor(
            store, runner, workspace_allocator=WorkspaceAllocator(str(tmp_path / "ws")),
            notification_store=notifications)
        assert executor.step(now=2) == NEEDS_YOU
        assert executor.step(now=3) == "idle", "a parked boundary must not be re-leased"
        assert len(attempts) == 1

        row = store.executions("boundary")[0]
        assert row["state"] == NEEDS_YOU and row["attempts"] == 1
        stored = json.loads(row["result_json"])
        assert stored["stop_reason"] == "turn_limit" and stored["session_id"] == "sess-1"
        assert notifications.notification_stats()["pending"] == 1


def test_a_finished_run_that_never_set_success_is_not_a_failure(tmp_path, monkeypatch):
    """`RunResult.success` defaults to False, so it is not a verdict the adapter may read."""
    from harness.recorder import RunResult

    _isolate(tmp_path, monkeypatch)
    _fixed_result(monkeypatch, RunResult(
        answer="done", messages=[{"role": "user", "content": "task"}]))
    value = automations._run_collie_request(_request(tmp_path))

    assert value["status"] == SUCCEEDED, value
    assert value["stop_reason"] == "completed" and value["summary"] == "done"


def test_the_adapter_reads_the_canonical_host_stop_reason(monkeypatch):
    """Outcome mapping must not become a second, drifting judgment of the same facts.

    Asserted behaviourally: the host's helper is made to answer with a reason the adapter
    cannot possibly have derived itself, and the receipt must still carry it.
    """
    from harness import recorder

    monkeypatch.setattr(recorder, "run_stop_reason", lambda result: "invented_ceiling")
    stop, status, error = automations._automation_outcome(
        _LegacyResult(), {"cancelled": False}, "")

    assert stop == "invented_ceiling", "the adapter re-derived its own stop reason"
    assert status == NEEDS_YOU and "invented ceiling" in error


def test_the_full_suite_cannot_be_what_isolates_this_file(tmp_path, monkeypatch):
    """Importing `harness.cli` first freezes `cli.DATA`; isolation must survive that.

    This is the regression itself: `sessions._dir` falls back to `cli.DATA`, which is
    evaluated once at import time, so a fixture that sets only COLLIE_STATE_DIR writes
    the user's real session store under any suite that imported cli earlier.
    """
    import harness.cli  # noqa: F401  — the frozen-DATA precondition, imported BEFORE isolation

    _isolate(tmp_path, monkeypatch)
    value = automations._run_collie_request(_request(tmp_path))

    written = sessions._dir()
    assert os.path.realpath(written).startswith(os.path.realpath(str(tmp_path))), written
    assert os.path.exists(os.path.join(written, value["session_id"] + ".json"))


def test_a_continued_automation_keeps_its_prior_thread_and_gains_this_answer(
        tmp_path, monkeypatch):
    """`continued` is the one policy with history to lose, and the only reader of the save."""
    _isolate(tmp_path, monkeypatch)
    prior = [{"role": "user", "content": "earlier task"},
             {"role": "assistant", "content": "earlier answer"}]
    sessions.save("automation-continued-1", prior, project="outcomes",
                  cwd=str(_workspace(tmp_path)), answer="earlier answer")

    value = automations._run_collie_request(_request(tmp_path, context={
        "policy": "continued", "session_id": "automation-continued-1"}))

    assert value["status"] == SUCCEEDED and value["session_saved"] is True, value
    assert value["session_id"] == "automation-continued-1"
    saved = sessions.load("automation-continued-1")
    texts = [str(m.get("content") or "") for m in saved["messages"]]
    assert "earlier task" in texts and "earlier answer" in texts, texts
    assert saved["last_answer"] == value["summary"], "this run's answer must reach the thread"


@pytest.mark.parametrize("canceled", [False, True])
def test_a_cancelled_run_that_also_failed_keeps_the_host_error(canceled):
    """A budget cancel is why it stopped; the underlying error is still what to act on.

    The two conditions only coincide inside the child, so this drives the mapping directly
    (the cancel path's end-to-end wiring is covered by the action-budget case above).
    """
    from harness.recorder import RunResult

    stop, status, error = automations._automation_outcome(
        RunResult(answer="partial", error="provider transport died", canceled=canceled),
        {"cancelled": True}, "")

    assert stop == "canceled" and status == NEEDS_YOU
    assert "budget exhausted" in error and "provider transport died" in error, error


@pytest.mark.parametrize("reported_status", [SUCCEEDED, "running"])
def test_final_budget_check_parks_custom_runner_receipts_even_with_bad_status(
        tmp_path, reported_status):
    """A spent budget is terminal even if a custom runner returns an invalid verdict."""
    from harness.automations import (AutomationExecutor, AutomationStore, TriggerEngine,
                                     WorkspaceAllocator)

    attempts = []
    with AutomationStore(str(tmp_path / "automations.db")) as store:
        store.upsert({
            "id": "custom-budget", "task": "inspect and report",
            "trigger": {"provider": "timer", "every_s": 10, "fire_immediately": True},
            "workspace": {"mode": "isolated"},
            "permissions": {"read_roots": [str(tmp_path)], "tools": ["grep"]},
            "budget": {"max_model_tokens": 100, "max_retries": 2},
        }, now=1)
        TriggerEngine(store).tick(1)

        def runner(request, guard):
            attempts.append(request["execution_id"])
            store.add_usage(request["execution_id"], model_tokens=101)
            return {"status": reported_status, "summary": "partial finding",
                    "session_id": "partial-session", "stop_reason": "turn_limit"}

        executor = AutomationExecutor(
            store, runner, workspace_allocator=WorkspaceAllocator(str(tmp_path / "ws")))
        assert executor.step(now=2) == NEEDS_YOU
        assert executor.step(now=3) == "idle"
        row = store.executions("custom-budget")[0]
        receipt = json.loads(row["result_json"])
        assert receipt["status"] == row["state"] == NEEDS_YOU
        assert receipt["summary"] == "partial finding"
        assert receipt["session_id"] == "partial-session"
        assert receipt["stop_reason"] == "turn_limit"
        assert "max_model_tokens" in row["last_error"]
        if reported_status == "running":
            assert "invalid status running" in row["last_error"]
        assert store.usage(row["execution_id"])["model_tokens"] == 101
        assert len(attempts) == row["attempts"] == 1


def test_a_save_failure_is_redacted_before_it_reaches_the_ledger(tmp_path, monkeypatch):
    """The text lands in the audit ledger and a notification body, so it is not raw."""
    _isolate(tmp_path, monkeypatch)
    secret = "sk-ant-api03-" + "z" * 40

    def refuse(*_args, **_kwargs):
        raise OSError("keyring rejected api_key=%s" % secret)

    monkeypatch.setattr(sessions, "save", refuse)
    value = automations._run_collie_request(_request(tmp_path))

    assert value["session_saved"] is False and "transcript save failed" in value["error"]
    assert secret not in value["error"], value["error"]
    assert "{{SECRET:" in value["error"], value["error"]


def test_crossing_the_budget_while_accounting_a_finished_child_keeps_its_receipt(
        tmp_path, monkeypatch):
    """End-to-end: real executor, real `DefaultCollieRunner`, only the child is a stand-in.

    `guard.consume` of a finished child's usage — and then the executor's own
    `guard.check` — both raise `BudgetExceeded` after the work is done. Before this fix
    `step` replaced the whole result with {}, so the durable receipt and the `needs_you`
    notification carried no partial answer, no resumable session and no stop reason: the
    evidence a person needs in order to decide about the cap it just hit.
    """
    from harness.automations import (AutomationExecutor, AutomationStore, DefaultCollieRunner,
                                     TriggerEngine, WorkspaceAllocator)
    from harness.ops import OpsStore

    child = {"status": SUCCEEDED, "error": "", "stop_reason": "completed",
             "session_saved": True, "session_id": "sess-over-budget",
             "summary": "found 3 todos; the third is in a.py", "model": "mock",
             "total_tokens": 9000, "cost_usd": 0.0, "tool_calls": 1}

    class _Child:
        returncode = 0

        def __init__(self, argv, **kwargs):
            self.argv = argv

        def poll(self):
            return 0

        def wait(self, timeout=None):
            result_path = self.argv[self.argv.index("--result") + 1]
            with open(result_path, "w", encoding="utf-8") as fh:
                json.dump(child, fh)
            return 0

    killed = []
    monkeypatch.setattr("harness.automations.subprocess.Popen",
                        lambda argv, **kwargs: _Child(argv, **kwargs))
    monkeypatch.setattr("harness.plat.kill_tree", lambda proc: killed.append(proc))
    with AutomationStore(str(tmp_path / "automations.db")) as store, \
            OpsStore(str(tmp_path / "ops.db")) as notifications:
        store.upsert({
            "id": "overspend", "task": "inspect and report",
            "trigger": {"provider": "timer", "every_s": 10, "fire_immediately": True},
            "workspace": {"mode": "isolated"},
            "permissions": {"read_roots": [str(tmp_path)], "tools": ["grep"]},
            "execution": {"provider": "mock", "allow_mock": True},
            "notifications": ["needs_you"],
            "budget": {"max_model_tokens": 100, "max_wall_s": 30, "max_retries": 2},
        }, now=1)
        TriggerEngine(store).tick(1)

        executor = AutomationExecutor(
            store, DefaultCollieRunner(),
            workspace_allocator=WorkspaceAllocator(str(tmp_path / "ws")),
            notification_store=notifications)
        assert executor.step(now=2) == NEEDS_YOU
        assert executor.step(now=3) == "idle", "an exhausted budget must not be replayed"

        row = store.executions("overspend")[0]
        stored = json.loads(row["result_json"])
        assert row["state"] == NEEDS_YOU and row["attempts"] == 1
        assert stored["summary"] == child["summary"], stored
        assert stored["session_id"] == "sess-over-budget" and stored["session_saved"] is True
        assert stored["stop_reason"] == "completed", "the child's own stop reason stands"
        assert "max_model_tokens" in row["last_error"], row["last_error"]
        assert row["last_error"].count("max_model_tokens") == 1, "reported once, not twice"
        # The spend really happened, so the ledger has to carry it, not just the verdict.
        assert store.usage(row["execution_id"])["model_tokens"] == 9000
        assert notifications.notification_stats()["pending"] == 1
    assert not killed


def test_a_budget_crossing_still_reports_a_partial_child_truthfully(tmp_path, monkeypatch):
    """The same seam when the child itself stopped early: both reasons survive, once each."""
    from harness.automations import (AutomationStore, BudgetGuard, DefaultCollieRunner,
                                     TriggerEngine)

    child = {"status": NEEDS_YOU, "error": "automation stopped at its turn limit",
             "stop_reason": "turn_limit", "session_saved": True, "session_id": "sess-partial",
             "summary": "(ran out of turns - UNFINISHED)", "total_tokens": 9000}

    class _Child:
        returncode = 0

        def __init__(self, argv, **kwargs):
            self.argv = argv

        def poll(self):
            return 0

        def wait(self, timeout=None):
            with open(self.argv[self.argv.index("--result") + 1], "w", encoding="utf-8") as fh:
                json.dump(child, fh)
            return 0

    monkeypatch.setattr("harness.automations.subprocess.Popen",
                        lambda argv, **kwargs: _Child(argv, **kwargs))
    with AutomationStore(str(tmp_path / "automations.db")) as store:
        store.upsert({
            "id": "partial", "task": "inspect and report",
            "trigger": {"provider": "timer", "every_s": 10, "fire_immediately": True},
            "workspace": {"mode": "isolated"},
            "permissions": {"read_roots": [str(tmp_path)], "tools": ["grep"]},
            "execution": {"provider": "mock", "allow_mock": True},
            "budget": {"max_model_tokens": 100, "max_wall_s": 30},
        }, now=1)
        TriggerEngine(store).tick(1)
        request = json.loads(store.executions()[0]["request_json"])
        request["resolved_workspace"] = str(tmp_path)
        guard = BudgetGuard(store, request["execution_id"], request["budget"], request=request)
        value = DefaultCollieRunner()(request, guard)

    assert value["status"] == NEEDS_YOU and value["stop_reason"] == "turn_limit", value
    assert value["summary"] == child["summary"] and value["session_id"] == "sess-partial"
    assert "turn limit" in value["error"] and "max_model_tokens" in value["error"], value


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
