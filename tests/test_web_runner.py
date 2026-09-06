"""Web worker-axis wiring without launching a real CLI or model."""
from __future__ import annotations

import json

from harness import cli, runner_registry, runner_slice, sessions, settings, webapp, worktree
from harness.recorder import RunResult
from harness.router import RunDecision
from harness.runner_specs import RunnerProbe, RunnerReceipt


def _decision(**over):
    values = dict(
        provider="codex-oauth", model="gpt-5.6-sol", effort="high",
        speed="standard", billing_multiplier=1.0, intent="build",
        quality="balanced", verification="auto", workspace="current",
        strategy="single", route_kind="code", complexity="simple")
    values.update(over)
    return RunDecision(**values)


def _probe(key="codex-exec", **over):
    values = dict(
        key=key, installed=True, executable_path="C:/bin/codex.exe",
        version="99.0.0", login="ok", billing_class="subscription_allowance",
        billing_mode="subscription", probed_at=1_800_000_000.0)
    values.update(over)
    return RunnerProbe(**values)


def _receipt(locator="thread-web", recovery=False, runner="codex-exec"):
    protocol = ("codex-appserver-jsonrpc" if runner == "codex-app-server"
                else "codex-exec-jsonl")
    return RunnerReceipt.from_dict({
        "runner": runner, "runner_version": "99.0.0",
        "runner_protocol": protocol,
        "billing_class": "subscription_allowance", "billing_mode": "subscription",
        "credential_family": "codex", "usage_known": True,
        "usage": {"input_tokens": 20, "output_tokens": 10},
        "settled": not recovery, "recovery_required": recovery, "mutated": True,
        "native_session": {"runner": runner, "locator": locator,
                           "workspace": "C:/workspace"},
    })


def _handler(events):
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    return fake


def _isolate(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "codex-oauth")
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(webapp.Handler, "_notify_done", lambda *a, **kw: None)
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear()
        webapp.Handler._cancel_events.clear()


def test_web_external_worker_streams_identity_saves_history_and_receipt(monkeypatch,
                                                                        tmp_path):
    from harness import router

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe()})
    monkeypatch.setattr(cli, "make_harness", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("external Web run must not construct Collie's native harness")))
    seen = {}

    def fake_run(decision, task, workspace, **kwargs):
        seen.update(kwargs)
        seen.update(decision=decision, task=task, workspace=workspace)
        kwargs["emit"]("runner", {"event": "native", "runner": "codex-exec",
                                    "cursor": 1, "type": "turn.completed"})
        receipt = _receipt()
        kwargs["emit"]("receipt", receipt.to_dict())
        result = RunResult(
            task_id="web", harness="codex-exec", provider="codex-oauth",
            model="gpt-5.6-sol", input_tokens=20, output_tokens=10,
            total_tokens=30, turns=1, wall_ms=25, success=True,
            verified=False, cost_usd=None, answer="worker answer", error="",
            messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, receipt)
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)
    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix the parser"], "session": ["web-worker"],
        "runner": ["codex-exec"], "intent": ["build"],
        "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"],
    })

    start = next(data for kind, data in events if kind == "start")
    done = next(data for kind, data in events if kind == "done")
    assert start["decision"]["runner"]["runner"] == "codex-exec"
    assert start["worker_capabilities"] == {
        "streaming": True,
        "steer": False,
        "approval_round_trip": False,
        "cancel": "process-tree",
    }
    assert start["run_plan"]["worker"]["key"] == "codex-exec"
    assert start["run_plan"]["worker"]["model"] == "gpt-5.6-sol"
    assert start["run_plan"]["worker"]["model_source"] == "resolved"
    assert start["run_plan"]["task"]["intent"] == "build"
    assert start["run_plan"]["capabilities"]["steer"] is False
    assert start["run_plan"]["id"].startswith("plan-")
    active = next(row for row in webapp.Handler._runs_snapshot()
                  if row["session"] == "web-worker")
    assert active["runner"] == "codex-exec"
    assert active["can_steer"] is False
    assert any(kind == "runner" for kind, _ in events)
    assert any(kind == "receipt" for kind, _ in events)
    assert done["runner"]["runner"] == "codex-exec"
    assert done["input_tokens"] == 20 and done["cost_usd"] is None
    assert seen["model"] == "gpt-5.6-sol"
    assert seen["provider"] == "codex"
    assert callable(seen["cancelled"])

    saved = sessions.load("web-worker")
    assert [m["content"] for m in saved["messages"]] == [
        "fix the parser", "worker answer"]
    assert saved["run_receipts"][-1]["runner"]["native_session"]["locator"] == \
        "thread-web"
    assert saved["run_receipts"][-1]["decision"]["run_plan"] == start["run_plan"]
    assert sessions.recovery_state("web-worker") is None


def test_web_resume_reads_saved_project_from_another_server_directory(monkeypatch, tmp_path):
    from harness import router

    _isolate(monkeypatch, tmp_path)
    original = tmp_path / "original-project"
    original.mkdir()
    (original / "fact.txt").write_text("saved project", encoding="utf-8")
    (tmp_path / "fact.txt").write_text("server project", encoding="utf-8")
    sessions.save("resume-project", [{"role": "user", "content": "remember"}],
                  cwd=str(original))
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe()})

    def run(decision, task, workspace, **kwargs):
        from pathlib import Path
        return RunResult(task_id="web", success=True, stop_reason="completed",
                         answer=(Path(workspace) / "fact.txt").read_text(encoding="utf-8"))

    monkeypatch.setattr(runner_slice, "run_adhoc", run)
    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["read fact.txt"], "session": ["resume-project"], "runner": ["codex-exec"],
    })
    done = next(data for kind, data in events if kind == "done")
    assert done["answer"] == "saved project"
    assert sessions.load("resume-project")["cwd"] == str(original)


def test_web_resume_missing_project_refuses_before_starting_worker(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sessions.save("missing-project", [{"role": "user", "content": "remember"}],
                  cwd=str(tmp_path / "removed"))
    monkeypatch.setattr(runner_slice, "run_adhoc", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not run in the server project")))
    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["continue"], "session": ["missing-project"], "runner": ["codex-exec"],
    })
    assert not any(kind == "start" for kind, _ in events)
    done = next(data for kind, data in events if kind == "done")
    assert done["workspace_missing"] is True
    assert "session workspace" in done["error"]


def test_web_app_server_wires_gate_inbox_and_steering(monkeypatch, tmp_path):
    from harness import router

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all", lambda keys=None, **kw: {
        "codex-app-server": _probe(key="codex-app-server")})
    seen = {}

    def fake_run(decision, task, workspace, **kwargs):
        seen.update(kwargs)
        seen["approval_answer"] = kwargs["approval_callback"](
            "command", {"itemId": "cmd_1", "command": "python -m pytest"})
        receipt = _receipt(runner="codex-app-server")
        result = RunResult(
            task_id="web", harness="codex-app-server", provider="codex",
            model="gpt-5.6-sol", turns=1, success=True,
            answer="worker answer", messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, receipt)
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)
    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix it"], "session": ["app-server-web"],
        "runner": ["codex-app-server"], "intent": ["build"],
        "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"],
    })

    start = next(data for kind, data in events if kind == "start")
    assert start["worker_capabilities"]["approval_round_trip"] is True
    assert start["worker_capabilities"]["steer"] is True
    assert seen["approval_answer"] == "accept"
    assert callable(seen["approval_callback"])
    assert callable(seen["steering"])


def test_web_external_worker_success_survives_runs_db_telemetry_failure(monkeypatch,
                                                                        tmp_path):
    from harness import recorder, router

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe()})

    def fake_run(*_args, **_kwargs):
        result = RunResult(
            task_id="web", harness="codex-exec", provider="codex",
            model="gpt-5.6-sol", input_tokens=2, output_tokens=1,
            total_tokens=3, turns=1, success=True, answer="real result", messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, _receipt("telemetry-thread"))
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)
    monkeypatch.setattr(
        recorder.Recorder, "finish_run",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("runs.db locked")))
    events = []

    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix it"], "session": ["telemetry-failure"],
        "runner": ["codex-exec"], "intent": ["build"],
        "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"],
    })

    done = [data for kind, data in events if kind == "done"][-1]
    assert done["answer"] == "real result" and not done["error"]
    assert sessions.load("telemetry-failure")["last_answer"] == "real result"


def test_web_external_worker_fails_visibly_when_payer_receipt_cannot_persist(
        monkeypatch, tmp_path):
    from harness import router

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe()})

    def fake_run(*_args, **_kwargs):
        result = RunResult(
            task_id="web", harness="codex-exec", provider="codex",
            model="gpt-5.6-sol", input_tokens=2, output_tokens=1,
            total_tokens=3, turns=1, success=True, answer="useful result", messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, _receipt("lost-receipt"))
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)
    monkeypatch.setattr(sessions, "append_run_receipt", lambda *_a, **_kw: False)
    events = []

    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix it"], "session": ["receipt-failure"],
        "runner": ["codex-exec"], "intent": ["build"],
        "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"],
    })

    done = [data for kind, data in events if kind == "done"][-1]
    assert done["answer"] == "useful result"
    assert "receipt could not be persisted" in done["error"]
    row = next(item for item in webapp.Handler._runs_snapshot()
               if item["session"] == "receipt-failure")
    assert row["state"] == "failed"
    assert "Worker error" in sessions.load("receipt-failure")["last_answer"]
    assert sessions.recovery_state("receipt-failure")["recovery_required"] is True


def test_web_external_worker_uncertain_outcome_keeps_recovery_fence(
        monkeypatch, tmp_path):
    from harness import router

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe()})

    def fake_run(*_args, **_kwargs):
        result = RunResult(
            task_id="web", harness="codex-exec", provider="codex",
            model="gpt-5.6-sol", input_tokens=2, output_tokens=1,
            total_tokens=3, turns=1, success=False, answer="partial answer",
            error="worker stopped after editing", messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR,
                _receipt("uncertain-thread", recovery=True))
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)
    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix it"], "session": ["uncertain-worker"],
        "runner": ["codex-exec"], "intent": ["build"],
        "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"],
    })

    done = [data for kind, data in events if kind == "done"][-1]
    assert done["answer"] == "partial answer"
    assert done["runner"]["recovery_required"] is True
    assert sessions.recovery_state("uncertain-worker")["recovery_required"] is True


def test_active_run_feature_is_explicit_and_closes_with_run(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sid = "feature-contract"
    run_id = webapp.Handler._run_begin(sid, "work", str(tmp_path))

    assert webapp.Handler._run_feature(sid, "can_steer") == (False, "unsupported")
    webapp.Handler._run_mark(sid, can_steer=True)
    assert webapp.Handler._run_feature(sid, "can_steer") == (True, "available")

    webapp.Handler._run_end(sid, error="test finished", run_id=run_id)
    assert webapp.Handler._run_feature(sid, "can_steer") == (False, "not_running")


def test_run_plan_is_content_addressed_and_records_effective_limits():
    decision = _decision().to_dict()
    decision["runner"] = {
        "runner": "codex-exec", "source": "user", "billing_mode": "subscription",
        "reasons": ["operator pinned codex-exec"],
    }
    caps = {"streaming": True, "steer": False, "approval_round_trip": False,
            "cancel": "process-tree"}

    one = webapp._build_run_plan(
        decision, caps, workspace="current", strategy="single",
        verify_command="python -m pytest -q", verify_source="pyproject.toml")
    two = webapp._build_run_plan(
        decision, dict(reversed(list(caps.items()))), workspace="current",
        strategy="single", verify_command="python -m pytest -q",
        verify_source="pyproject.toml")

    assert one == two
    assert one["verification"] == {
        "mode": "auto", "command": "python -m pytest -q", "source": "pyproject.toml"}
    assert one["worker"]["billing_mode"] == "subscription"
    assert one["worker"]["model"] == ""
    assert one["worker"]["model_source"] == "worker-default"
    assert len(one["limitations"]) == 2
    assert webapp._build_run_plan(
        decision, caps, workspace="isolated", strategy="single")["id"] != one["id"]


def test_web_unavailable_pinned_worker_refuses_before_run_registry(monkeypatch,
                                                                   tmp_path):
    from harness import router

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all", lambda keys=None, **kw: {
        "codex-exec": _probe(installed=False, executable_path="", version="",
                              login="unknown", detail="codex is not on PATH")})
    monkeypatch.setattr(runner_slice, "run_adhoc", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("a refused worker must never start")))

    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix it"], "runner": ["codex-exec"],
        "intent": ["build"], "quality": ["balanced"],
        "verification": ["auto"], "workspace": ["current"],
        "strategy": ["single"],
    })
    assert events[-1][0] == "done"
    assert "codex is not on PATH" in events[-1][1]["error"]
    assert not any(kind == "start" for kind, _ in events)
    assert webapp.Handler._runs_snapshot() == []


def test_preflight_refusal_discards_unused_isolated_worktree(monkeypatch, tmp_path):
    from harness import router

    _isolate(monkeypatch, tmp_path)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / ".git").mkdir()
    monkeypatch.setattr(
        router, "resolve_run_decision",
        lambda *a, **kw: _decision(workspace="isolated"))
    monkeypatch.setattr(worktree, "prepare", lambda *a, **kw: {
        "ok": True, "dir": str(isolated), "branch": "collie/test",
        "root": str(tmp_path), "kind": "worktree", "error": ""})
    released = []
    monkeypatch.setattr(
        worktree, "release",
        lambda path, force=False: released.append((path, force)) or {"ok": True})
    monkeypatch.setattr(runner_registry, "probe_all", lambda keys=None, **kw: {
        "codex-exec": _probe(installed=False, executable_path="", version="",
                              login="unknown", detail="codex is not on PATH")})

    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix it"], "runner": ["codex-exec"],
        "intent": ["build"], "quality": ["balanced"],
        "verification": ["auto"], "workspace": ["isolated"],
        "strategy": ["single"],
    })

    assert events[-1][0] == "done" and "not on PATH" in events[-1][1]["error"]
    assert released == [(str(isolated), True)]
    assert webapp.Handler._runs_snapshot() == []


def test_preflight_exception_discards_worktree_and_closes_sse(monkeypatch, tmp_path):
    """Probe crashes happen before ownership; they cannot strand the tree/stream."""
    from harness import router

    _isolate(monkeypatch, tmp_path)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.setattr(
        router, "resolve_run_decision",
        lambda *a, **kw: _decision(workspace="isolated"))
    monkeypatch.setattr(worktree, "prepare", lambda *a, **kw: {
        "ok": True, "dir": str(isolated), "branch": "collie/test",
        "root": str(tmp_path), "kind": "worktree", "error": ""})
    released = []
    monkeypatch.setattr(
        worktree, "release",
        lambda path, force=False: released.append((path, force)) or {"ok": True})

    def broken_probe(*_args, **_kwargs):
        raise RuntimeError("probe leaked sk-super-secret")

    monkeypatch.setattr(runner_registry, "probe_all", broken_probe)
    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix it"], "runner": ["codex-exec"],
        "intent": ["build"], "quality": ["balanced"],
        "verification": ["auto"], "workspace": ["isolated"],
        "strategy": ["single"],
    })

    assert events[-1][0] == "done"
    assert "worker preflight failed" in events[-1][1]["error"]
    assert "sk-super-secret" not in events[-1][1]["error"]
    assert "[redacted]" in events[-1][1]["error"]
    assert released == [(str(isolated), True)]
    assert webapp.Handler._runs_snapshot() == []


def test_external_worker_failure_closes_initiator_mirror_and_global_activity(
        monkeypatch, tmp_path):
    from harness import router

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe()})
    monkeypatch.setattr(
        runner_slice, "run_adhoc",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("worker crashed with sk-super-secret")))
    mirrored = []
    live = []
    monkeypatch.setattr(
        webapp.Handler, "_mirror_pub",
        classmethod(lambda _cls, sid, kind, data: mirrored.append((sid, kind, data))))
    monkeypatch.setattr(
        webapp.Handler, "_live_pub",
        classmethod(lambda _cls, kind, data: live.append((kind, data))))

    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix it"], "session": ["web-failure"],
        "runner": ["codex-exec"], "intent": ["build"],
        "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"],
    })

    terminal = [data for kind, data in events if kind == "done"]
    assert len(terminal) == 1 and "worker crashed" in terminal[0]["error"]
    assert "sk-super-secret" not in terminal[0]["error"]
    assert "[redacted]" in terminal[0]["error"]
    assert any(sid == "web-failure" and kind == "done"
               for sid, kind, _data in mirrored)
    assert any(kind == "done" and data["session"] == "web-failure"
               and "worker crashed" in data["error"] for kind, data in live)
    row = next(item for item in webapp.Handler._runs_snapshot()
               if item["session"] == "web-failure")
    assert row["state"] == "failed" and "worker crashed" in row["error"]
    saved = sessions.load("web-failure")
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]
    assert "worker crashed" in saved["messages"][-1]["content"]
    assert "sk-super-secret" not in json.dumps(saved)


def test_external_worker_error_does_not_launch_a_fresh_required_check(monkeypatch, tmp_path):
    from harness import router, verification

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw:
                        _decision(verification="required"))
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe()})
    monkeypatch.setattr(runner_slice, "run_adhoc", lambda *a, **kw:
                        RunResult(task_id="web", error="worker transport failed", answer="partial work"))
    monkeypatch.setattr(verification, "run_verification_command", lambda *a, **kw:
                        (_ for _ in ()).throw(AssertionError("failed work cannot start a check")))
    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix and check"], "session": ["failed-check"], "runner": ["codex-exec"],
        "verification": ["required"], "verify_command": ["python -m unittest"],
    })
    evidence = next(data["evidence"] for kind, data in events if kind == "verification_evidence")
    assert evidence["executed"] is False
    done = next(data for kind, data in events if kind == "done")
    assert done["answer"] == "partial work"
    assert "worker transport failed" in done["error"]
    assert "required check failed" not in done["error"]


def test_run_capability_payload_shape_exposes_worker_truth():
    """The UI contract names availability explicitly; installed is not enough."""
    row = _probe().to_dict()
    assert row["runnable"] is True
    assert row["availability"] == "runnable-unverified"
    assert json.loads(json.dumps(row))["login"] == "ok"


def test_public_web_errors_redact_credentials_before_json_sse_or_history():
    error = webapp._public_error(
        RuntimeError("request failed Authorization: Bearer sk-super-secret"),
        prefix="pack failed: ")

    assert error.startswith("pack failed: RuntimeError: request failed")
    assert "sk-super-secret" not in error
    assert "Bearer " not in error
    assert "[redacted]" in error
