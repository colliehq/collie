"""Ad-hoc slice tests: what `run_adhoc` does with a worker's outcome.

Every runner here is a fake object with the three methods of the ``AgentRunner``
protocol, injected by monkeypatching ``runner_registry.make_runner``.  No CLI is
executed, nothing is probed on the host and no model is called — the questions
this file asks (may we fall back? is settled verified? who cancels?) are all
about the slice's own decisions, and answering them against a real Codex would
make the test depend on which account the machine happens to be logged into.
"""
from __future__ import annotations

import json
import os
import re
import threading

import pytest

from harness import runner_registry, runner_slice
from harness.agent_runners import RunnerEvent, RunnerSnapshot
from harness.runner_specs import HarnessDecision, PendingInteraction, RunInput, RunnerProbe


# The §C inventory, with a left boundary on the two short prefixes: a decision
# reason legitimately says "(task-policy)", and "ta*sk-*policy" is not a key.
SECRETS = re.compile(r"(?<![A-Za-z0-9])(?:sk-|sess-)|Bearer |eyJ|ghp_|AKIA")


# --- fixtures ---------------------------------------------------------------
def _probe(key: str, **over) -> RunnerProbe:
    values = dict(key=key, installed=True, executable_path="/usr/bin/" + key,
                  version="9.9.9", login="ok",
                  billing_class="subscription_allowance",
                  billing_mode="subscription", probed_at=1_700_000_000.0)
    values.update(over)
    return RunnerProbe(**values)


def _decision(runner: str = "codex-exec", *, fallback=(), **over) -> HarnessDecision:
    values = dict(
        runner=runner, source="task-policy", credential_family="codex",
        billing_class="subscription_allowance", billing_mode="subscription",
        reasons=("runner: %s (task-policy); family=codex "
                 "billing=subscription_allowance" % runner,),
        rejected={}, candidates=(), fallback_chain=tuple(fallback),
        probe=_probe(runner).to_dict(), probe_digest="d" * 8)
    values.update(over)
    return HarnessDecision(**values)


def _snapshot(runner: str, workspace: str, **over) -> RunnerSnapshot:
    """A settled turn, in the shape `CodexExecRunner._invoke` returns."""
    values = dict(
        runner=runner, workspace=os.path.realpath(os.path.abspath(workspace)),
        thread_id="0198f0aa-1111-7000-8000-0000000000aa", cursor=1,
        events=(RunnerEvent(cursor=1, type="turn.completed",
                            payload={"usage": {"input_tokens": 100}}, at=1.0),),
        usage={"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30},
        settled=True, exit_code=0, mutated=True, mutation_check_complete=True,
        final_output="done", invocation=1, started_at=1_000.0, finished_at=1_002.0)
    values.update(over)
    return RunnerSnapshot(**values)


class _FakeRunner:
    """The three methods `run_adhoc` is allowed to call, and nothing else."""

    def __init__(self, key: str, *, result=None, raises=None):
        self.key = key
        self._result = result
        self._raises = raises
        self.starts: list[tuple[str, str]] = []
        self.resumes: list[tuple[str, str]] = []
        self.cancels = 0
        self.last_env_receipt = {"allowed": ["PATH", "HOME"], "stripped": ["ANTHROPIC_API_KEY"]}

    def start(self, prompt, workspace, *, timeout_s=None):
        self.starts.append((prompt, workspace))
        if self._raises is not None:
            raise self._raises
        return self._result

    def resume(self, snapshot, prompt, *, timeout_s=None):
        self.resumes.append((snapshot.thread_id, prompt))
        if self._raises is not None:
            raise self._raises
        return self._result

    def cancel_current(self):
        self.cancels += 1
        return True


class _Factory:
    """Stands in for ``runner_registry.make_runner`` and records every build."""

    def __init__(self, runners: dict):
        self.runners = runners
        self.built: list[str] = []
        self.kwargs: list[dict] = []

    def __call__(self, key, *, model="", speed="standard", effort="auto",
                 timeout_s=None, env_policy=""):
        self.built.append(key)
        self.kwargs.append({"model": model, "speed": speed, "effort": effort})
        runner = self.runners.get(key)
        if runner is None:
            raise AssertionError("test built an unexpected runner: %s" % key)
        if isinstance(runner, Exception):
            raise runner
        return runner


@pytest.fixture(autouse=True)
def _no_host_probing(monkeypatch):
    """A probe of a fallback worker must never shell out during a unit test."""
    monkeypatch.setattr(runner_registry, "probe",
                        lambda key, **kw: _probe(key), raising=True)


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


def _install(monkeypatch, **runners) -> _Factory:
    factory = _Factory({key.replace("_", "-"): value for key, value in runners.items()})
    monkeypatch.setattr(runner_registry, "make_runner", factory, raising=True)
    return factory


# --- the happy path ---------------------------------------------------------
def test_run_result_harness_is_runner_key(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    res = runner_slice.run_adhoc(_decision(), "add a line", workspace, model="gpt-5")

    assert res.harness == "codex-exec"        # runs.db groups by this column
    assert res.provider == "codex"            # the spec's credential family
    assert res.answer == "done"
    assert res.error == ""
    assert res.turns == 1
    assert runner.starts and runner.starts[0][0] == "add a line"
    assert runner_slice.receipt_of(res).runner == "codex-exec"


def test_read_only_decision_reaches_the_worker_factory_and_receipt(monkeypatch,workspace):
    runner = _FakeRunner("claude-code",result=_snapshot("claude-code",workspace,mutated=False))
    calls = []
    monkeypatch.setattr(runner_registry,"make_runner",lambda key,**kw:calls.append((key,kw)) or runner)
    decision = _decision("claude-code",credential_family="claude",read_only=True)
    result = runner_slice.run_adhoc(decision,"Explain the earlier patch",workspace)
    assert calls[0][1]["read_only"] is True
    assert runner_slice.receipt_of(result).decision["read_only"] is True


def test_structured_input_reaches_multimodal_runner_without_losing_images(
        monkeypatch, workspace):
    runner = _FakeRunner("codex-sdk", result=_snapshot("codex-sdk", workspace))
    _install(monkeypatch, codex_sdk=runner)
    structured = RunInput("inspect this", image_urls=("data:image/png;base64,AA==",))

    runner_slice.run_adhoc(_decision(runner="codex-sdk"), structured, workspace,
                           history_note="continue the review")

    prompt = runner.starts[0][0]
    assert isinstance(prompt, RunInput)
    assert prompt.image_urls == structured.image_urls
    assert "continue the review" in prompt.text and "inspect this" in prompt.text


def test_settled_is_not_verified(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    res = runner_slice.run_adhoc(_decision(), "add a line", workspace)
    receipt = runner_slice.receipt_of(res)

    assert res.success is True
    assert res.verified is False              # only the host verifier may set it
    assert receipt.settled is True
    assert "verified" not in receipt.to_dict()


def test_receipt_carries_handshake_and_unresolved_interaction(monkeypatch, workspace):
    interaction = PendingInteraction(
        interaction_id="ask-1", runner="codex-exec", kind="user_input",
        prompt="Choose a branch")
    snapshot = _snapshot("codex-exec", workspace, settled=True,
                         pending_interactions=(interaction,))
    runner = _FakeRunner("codex-exec", result=snapshot)
    _install(monkeypatch, codex_exec=runner)

    receipt = runner_slice.receipt_of(
        runner_slice.run_adhoc(_decision(), "t", workspace))

    assert receipt.settled is False
    assert receipt.interactions[0]["interaction_id"] == "ask-1"
    assert receipt.capability_handshake["runner"] == "codex-exec"
    assert receipt.capability_handshake["capabilities"]["protocol"] == \
        "codex-exec-jsonl"


def test_bidirectional_runner_receives_surface_approval_callback(monkeypatch, workspace):
    class _ApprovalRunner(_FakeRunner):
        def set_approval_callback(self, callback):
            self.approval_callback = callback

    runner = _ApprovalRunner(
        "codex-app-server",
        result=_snapshot("codex-app-server", workspace))
    _install(monkeypatch, codex_app_server=runner)
    callback = lambda kind, params: "decline"

    runner_slice.run_adhoc(
        _decision(runner="codex-app-server"), "t", workspace,
        approval_callback=callback)

    assert runner.approval_callback is callback


def test_steer_waits_through_runner_launch_race(monkeypatch, workspace):
    delivered = threading.Event()

    class _SteerRunner(_FakeRunner):
        active = False

        def start(self, prompt, root, *, timeout_s=None):
            self.starts.append((prompt, root))
            self.active = True
            assert delivered.wait(2.0), "queued steer was lost before launch"
            return self._result

        def steer_current(self, prompt):
            if not self.active:
                return False
            self.steered = prompt
            delivered.set()
            return True

    runner = _SteerRunner(
        "codex-app-server",
        result=_snapshot("codex-app-server", workspace))
    _install(monkeypatch, codex_app_server=runner)
    queued = ["use the smaller fix"]

    def drain():
        rows = list(queued)
        queued.clear()
        return rows

    result = runner_slice.run_adhoc(
        _decision(runner="codex-app-server"), "t", workspace,
        steering=drain)

    assert result.success is True
    assert runner.steered == "use the smaller fix"


def test_usage_is_translated_and_cost_needs_a_price(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    receipt = runner_slice.receipt_of(
        runner_slice.run_adhoc(_decision(), "t", workspace, model="gpt-5"))

    assert receipt.usage_known is True
    assert receipt.usage["input_tokens"] == 80      # 100 total - 20 cached
    assert receipt.usage["cache_read"] == 20
    assert receipt.cost_usd_reported is None        # codex reports no dollars


def test_unknown_usage_is_none_not_zero(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec",
                         result=_snapshot("codex-exec", workspace, usage={}))
    _install(monkeypatch, codex_exec=runner)

    res = runner_slice.run_adhoc(_decision(), "t", workspace)
    receipt = runner_slice.receipt_of(res)

    assert receipt.usage_known is False
    assert res.input_tokens is None and res.output_tokens is None
    assert res.total_tokens is None and res.cost_usd is None


def test_external_result_is_persisted_for_dashboard_and_route_health(monkeypatch,
                                                                      workspace):
    from harness.recorder import Recorder

    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)
    recorder = Recorder(os.path.join(workspace, "runs.db"))
    try:
        res = runner_slice.run_adhoc(
            _decision(), "t", workspace, model="gpt-5", task_id="adhoc",
            recorder=recorder)
        row = recorder.db.execute(
            "SELECT harness,task_id,input_tokens,verified,success FROM runs "
            "WHERE run_id=?", (res.run_id,)).fetchone()
    finally:
        recorder.close()

    assert res.run_id > 0
    assert tuple(row) == ("codex-exec", "adhoc", 80, 0, 1)


# --- fallback ---------------------------------------------------------------
def test_start_time_failure_uses_fallback_and_records_from(monkeypatch, workspace):
    """A CLI that is not installed never saw the prompt, so the chain continues."""
    missing = _FakeRunner("codex-exec",
                          raises=FileNotFoundError("Codex CLI is not installed"))
    second = _FakeRunner("claude-code", result=_snapshot("claude-code", workspace))
    factory = _install(monkeypatch, codex_exec=missing, claude_code=second)

    res = runner_slice.run_adhoc(
        _decision(fallback=("claude-code",)), "add a line", workspace)
    receipt = runner_slice.receipt_of(res)

    assert factory.built == ["codex-exec", "claude-code"]
    assert res.harness == "claude-code"
    assert receipt.runner == "claude-code"
    assert receipt.fallback_from == "codex-exec"
    assert receipt.credential_family == "claude"    # the worker that actually ran
    assert res.error == ""


def test_fallback_is_not_attempted_without_a_chain(monkeypatch, workspace):
    """An explicit `--runner` has an empty chain: a pin fails, it never migrates."""
    missing = _FakeRunner("codex-exec", raises=FileNotFoundError("not installed"))
    factory = _install(monkeypatch, codex_exec=missing)

    res = runner_slice.run_adhoc(_decision(source="user"), "t", workspace)

    assert factory.built == ["codex-exec"]
    assert res.harness == "codex-exec"
    assert "not installed" in res.error
    assert res.success is False
    assert runner_slice.receipt_of(res).fallback_from == ""


def test_post_start_failure_never_falls_back(monkeypatch, workspace):
    """The child answered — whatever it left behind belongs to that worker."""
    failed = _FakeRunner("codex-exec", result=_snapshot(
        "codex-exec", workspace, settled=False, exit_code=1, mutated=False,
        error="Codex exited with status 1", final_output=""))
    other = _FakeRunner("claude-code", result=_snapshot("claude-code", workspace))
    factory = _install(monkeypatch, codex_exec=failed, claude_code=other)

    res = runner_slice.run_adhoc(
        _decision(fallback=("claude-code",)), "t", workspace)

    assert factory.built == ["codex-exec"]      # the second worker was never built
    assert other.starts == []
    assert res.harness == "codex-exec"
    assert res.error == "Codex exited with status 1"
    assert runner_slice.receipt_of(res).fallback_from == ""


def test_timeout_never_falls_back_even_with_no_events(monkeypatch, workspace):
    """A silent timeout looks like "nothing happened" and is not."""
    timed_out = _FakeRunner("codex-exec", result=_snapshot(
        "codex-exec", workspace, settled=False, timed_out=True, exit_code=None,
        cursor=0, events=(), error="Codex turn exceeded its 900.0s wall timeout"))
    other = _FakeRunner("claude-code", result=_snapshot("claude-code", workspace))
    factory = _install(monkeypatch, codex_exec=timed_out, claude_code=other)

    res = runner_slice.run_adhoc(_decision(fallback=("claude-code",)), "t", workspace)

    assert factory.built == ["codex-exec"]
    assert "wall timeout" in res.error


def test_mutated_workspace_never_falls_back(monkeypatch, workspace):
    """Half-applied edits make a second worker a race, not a retry."""
    dirty = _FakeRunner("codex-exec", result=_snapshot(
        "codex-exec", workspace, settled=False, exit_code=None, cursor=0, events=(),
        mutated=True, recovery_required=True, error="transport died"))
    other = _FakeRunner("claude-code", result=_snapshot("claude-code", workspace))
    factory = _install(monkeypatch, codex_exec=dirty, claude_code=other)

    res = runner_slice.run_adhoc(_decision(fallback=("claude-code",)), "t", workspace)
    receipt = runner_slice.receipt_of(res)

    assert factory.built == ["codex-exec"]
    assert receipt.recovery_required is True
    assert receipt.mutated is True


def test_transport_failure_before_a_child_falls_back(monkeypatch, workspace):
    """No events, no exit status, clean workspace: the prompt was never sent."""
    stillborn = _FakeRunner("codex-exec", result=_snapshot(
        "codex-exec", workspace, settled=False, exit_code=None, cursor=0, events=(),
        thread_id="", mutated=False, mutation_check_complete=True,
        error="OSError: could not spawn"))
    second = _FakeRunner("claude-code", result=_snapshot("claude-code", workspace))
    factory = _install(monkeypatch, codex_exec=stillborn, claude_code=second)

    res = runner_slice.run_adhoc(_decision(fallback=("claude-code",)), "t", workspace)

    assert factory.built == ["codex-exec", "claude-code"]
    assert res.harness == "claude-code"
    assert runner_slice.receipt_of(res).fallback_from == "codex-exec"


def test_unexpected_start_exception_requires_recovery_and_never_falls_back(
        monkeypatch, workspace):
    first = _FakeRunner("codex-exec", raises=RuntimeError("parser crashed"))
    second = _FakeRunner("claude-code", result=_snapshot("claude-code", workspace))
    factory = _install(monkeypatch, codex_exec=first, claude_code=second)

    res = runner_slice.run_adhoc(
        _decision(fallback=("claude-code",)), "edit", workspace)
    receipt = runner_slice.receipt_of(res)

    assert factory.built == ["codex-exec"]
    assert receipt.recovery_required is True
    assert receipt.settled is False
    assert receipt.mutated is None


def test_exhausted_chain_reports_the_last_failure(monkeypatch, workspace):
    first = _FakeRunner("codex-exec", raises=FileNotFoundError("codex missing"))
    second = _FakeRunner("claude-code", raises=FileNotFoundError("claude missing"))
    factory = _install(monkeypatch, codex_exec=first, claude_code=second)

    res = runner_slice.run_adhoc(_decision(fallback=("claude-code",)), "t", workspace)

    assert factory.built == ["codex-exec", "claude-code"]
    assert res.harness == "claude-code"
    assert "claude missing" in res.error
    assert runner_slice.receipt_of(res).fallback_from == "codex-exec"


# --- cancellation -----------------------------------------------------------
def test_cancelled_watcher_calls_cancel_current(monkeypatch, workspace):
    """The watcher asks the runner to stop; it never touches a process itself."""
    cancelled = threading.Event()
    stopped = threading.Event()

    class _Blocking(_FakeRunner):
        def start(self, prompt, ws, *, timeout_s=None):
            self.starts.append((prompt, ws))
            cancelled.set()                       # the user presses cancel now
            assert stopped.wait(10.0), "the watcher never asked us to stop"
            return _snapshot(self.key, ws, settled=False, cancelled=True,
                             exit_code=None, error="Codex turn was cancelled")

        def cancel_current(self):
            self.cancels += 1
            stopped.set()
            return True

    runner = _Blocking("codex-exec")
    other = _FakeRunner("claude-code", result=_snapshot("claude-code", workspace))
    factory = _install(monkeypatch, codex_exec=runner, claude_code=other)

    res = runner_slice.run_adhoc(_decision(fallback=("claude-code",)), "t", workspace,
                                 cancelled=cancelled.is_set)

    assert runner.cancels >= 1
    # A user cancel is not a start-time failure: falling back here would launch a
    # second worker moments after the user asked for none.
    assert factory.built == ["codex-exec"]
    assert "cancelled" in res.error


def test_cancel_before_start_skips_the_launch(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    res = runner_slice.run_adhoc(_decision(), "t", workspace, cancelled=lambda: True)

    assert runner.starts == []                    # nothing was paid for
    assert res.success is False
    assert "cancelled before the worker started" in res.error


def test_a_broken_cancel_predicate_does_not_kill_the_run(monkeypatch, workspace):
    def explode():
        raise RuntimeError("the request table is gone")

    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    res = runner_slice.run_adhoc(_decision(), "t", workspace, cancelled=explode)

    assert res.success is True
    assert runner.cancels == 0


# --- resume -----------------------------------------------------------------
def test_resume_from_locator_continues_the_thread(monkeypatch, workspace):
    locator = "0198f0aa-1111-7000-8000-0000000000aa"
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    res = runner_slice.run_adhoc(_decision(), "keep going", workspace,
                                 resume_from=locator)

    assert runner.starts == []
    assert runner.resumes == [(locator, "keep going")]
    assert runner_slice.receipt_of(res).native_session["locator"] == locator


def test_resume_accepts_a_native_session_dict(monkeypatch, workspace):
    locator = "0198f0aa-1111-7000-8000-0000000000aa"
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    receipt_section = {"runner": "codex-exec", "workspace": workspace,
                       "locator": locator, "protocol_version": "",
                       "created_at": 1.0, "workspace_digest": ""}
    runner_slice.run_adhoc(_decision(), "keep going", workspace,
                           resume_from=receipt_section)

    assert runner.resumes == [(locator, "keep going")]


def test_resume_refuses_a_locator_from_another_workspace(monkeypatch, workspace, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)
    receipt_section = {
        "runner": "codex-exec", "workspace": str(other),
        "locator": "0198f0aa-1111-7000-8000-0000000000aa",
    }

    with pytest.raises(ValueError, match="different workspace"):
        runner_slice.run_adhoc(
            _decision(), "keep going", workspace, resume_from=receipt_section)

    assert runner.starts == [] and runner.resumes == []


def test_resume_refuses_a_snapshot_from_another_runner(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)
    foreign = _snapshot("claude-code", workspace)

    with pytest.raises(ValueError, match="claude-code, not codex-exec"):
        runner_slice.run_adhoc(_decision(), "keep going", workspace,
                               resume_from=foreign)

    assert runner.starts == [] and runner.resumes == []


def test_resume_never_falls_back(monkeypatch, workspace):
    """A locator is one worker's private session id; nobody else can continue it."""
    runner = _FakeRunner("codex-exec", raises=FileNotFoundError("codex missing"))
    other = _FakeRunner("claude-code", result=_snapshot("claude-code", workspace))
    factory = _install(monkeypatch, codex_exec=runner, claude_code=other)

    res = runner_slice.run_adhoc(_decision(fallback=("claude-code",)), "t", workspace,
                                 resume_from="0198f0aa-1111-7000-8000-0000000000aa")

    assert factory.built == ["codex-exec"]
    assert "codex missing" in res.error


# --- emit and receipt -------------------------------------------------------
def test_emit_reports_the_decision_and_the_receipt(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)
    seen: list[tuple[str, dict]] = []

    runner_slice.run_adhoc(_decision(), "t", workspace,
                           emit=lambda kind, payload: seen.append((kind, payload)))

    kinds = [kind for kind, _ in seen]
    assert kinds[0] == "decision" and kinds[-1] == "receipt"
    assert dict(seen[0][1])["runner"] == "codex-exec"
    assert "runner" in kinds                      # native events replayed
    assert dict(seen[-1][1])["usage_known"] is True


def test_transcript_text_keeps_answer_and_terminal_error():
    class Result:
        answer = "worker produced a patch"
        error = "required check failed"

    text = runner_slice.transcript_text(Result())

    assert text.startswith("worker produced a patch")
    assert "Worker error" in text and "required check failed" in text


def test_live_native_event_is_not_replayed_a_second_time(monkeypatch, workspace):
    class _Streaming(_FakeRunner):
        def set_event_callback(self, callback):
            self.callback = callback

        def start(self, prompt, ws, *, timeout_s=None):
            snapshot = _snapshot(self.key, ws)
            self.callback(snapshot.events[0])
            return snapshot

    runner = _Streaming("codex-exec")
    _install(monkeypatch, codex_exec=runner)
    seen = []

    runner_slice.run_adhoc(
        _decision(), "t", workspace,
        emit=lambda kind, payload: seen.append((kind, payload)))

    native = [payload for kind, payload in seen
              if kind == "runner" and payload.get("event") == "native"]
    assert len(native) == 1
    assert native[0]["cursor"] == 1 and native[0]["live"] is True


def test_live_native_event_projection_is_bounded(monkeypatch, workspace):
    events = tuple(
        RunnerEvent(cursor=index, type="item.updated",
                    payload={"index": index}, at=1.0)
        for index in range(1, 206))
    snapshot = _snapshot(
        "codex-exec", workspace, cursor=len(events), events=events)

    class _Streaming(_FakeRunner):
        def set_event_callback(self, callback):
            self.callback = callback

        def start(self, prompt, ws, *, timeout_s=None):
            for event in events:
                self.callback(event)
            return snapshot

    runner = _Streaming("codex-exec")
    _install(monkeypatch, codex_exec=runner)
    seen = []

    runner_slice.run_adhoc(
        _decision(), "t", workspace,
        emit=lambda kind, payload: seen.append((kind, payload)))

    native = [payload for kind, payload in seen
              if kind == "runner" and payload.get("event") == "native"]
    omitted = [payload for kind, payload in seen
               if kind == "runner" and payload.get("event") == "native-events-omitted"]
    assert len(native) == runner_slice._MAX_REPLAYED_EVENTS
    assert len(omitted) == 1
    assert omitted[0]["omitted_after"] == runner_slice._MAX_REPLAYED_EVENTS


def test_a_failing_emitter_never_fails_the_run(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    def emit(kind, payload):
        raise RuntimeError("the stream consumer hung up")

    assert runner_slice.run_adhoc(_decision(), "t", workspace, emit=emit).success is True


def test_receipt_carries_no_credentials(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot(
        "codex-exec", workspace,
        error="auth failed: Bearer sk-ant-oat01-secret",
        settled=False, exit_code=1))
    _install(monkeypatch, codex_exec=runner)

    receipt = runner_slice.receipt_of(runner_slice.run_adhoc(_decision(), "t", workspace))
    text = json.dumps(receipt.to_dict())

    assert not SECRETS.search(text), text
    assert receipt.env_receipt == {"allowed": ["PATH", "HOME"],
                                   "stripped": ["ANTHROPIC_API_KEY"]}
    assert receipt.events_digest and len(receipt.events_digest) == 64


def test_incomplete_mutation_check_reports_mutated_none(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", raises=FileNotFoundError("codex missing"))
    _install(monkeypatch, codex_exec=runner)

    receipt = runner_slice.receipt_of(runner_slice.run_adhoc(_decision(), "t", workspace))

    # Nobody compared the workspace, so "unchanged" would be a claim, not a fact.
    assert receipt.mutated is None
    assert receipt.settled is False
    assert receipt.decision["runner"] == "codex-exec"


def test_history_note_is_prefixed_to_the_prompt(monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)

    runner_slice.run_adhoc(_decision(), "add a line", workspace,
                           history_note="you already created hello.py")

    prompt = runner.starts[0][0]
    assert prompt.startswith("Earlier in this session:\nyou already created hello.py")
    assert prompt.endswith("Task:\nadd a line")


def test_external_worker_prompt_and_history_never_receive_pasted_credentials(
        monkeypatch, workspace):
    runner = _FakeRunner("codex-exec", result=_snapshot("codex-exec", workspace))
    _install(monkeypatch, codex_exec=runner)
    secret = "sk-" + "a" * 32

    runner_slice.run_adhoc(
        _decision(), "use api_key=" + secret, workspace,
        history_note="prior Authorization: Bearer " + secret)

    prompt = runner.starts[0][0]
    assert secret not in prompt
    assert "Bearer " not in prompt
    assert "[redacted]" in prompt


# --- refusals ---------------------------------------------------------------
def test_a_failed_decision_is_refused_not_run(monkeypatch, workspace):
    _install(monkeypatch)

    with pytest.raises(Exception) as excinfo:
        runner_slice.run_adhoc(_decision(runner="", error="H3: codex not installed"),
                               "t", workspace)
    assert "H3" in str(excinfo.value)


def test_collie_is_not_an_external_worker(monkeypatch, workspace):
    _install(monkeypatch)

    with pytest.raises(Exception, match="not an external worker"):
        runner_slice.run_adhoc(_decision(runner="collie"), "t", workspace)


def test_missing_workspace_is_refused_before_a_worker_is_built(monkeypatch, tmp_path):
    factory = _install(monkeypatch)

    with pytest.raises(ValueError, match="does not exist"):
        runner_slice.run_adhoc(_decision(), "t", str(tmp_path / "nope"))
    assert factory.built == []


def test_empty_task_is_refused(monkeypatch, workspace):
    factory = _install(monkeypatch)

    with pytest.raises(ValueError, match="non-empty"):
        runner_slice.run_adhoc(_decision(), "   ", workspace)
    assert factory.built == []

def test_receipt_names_the_model_claude_actually_used():
    """Claude's terminal stream-json result has no top-level model, only a breakdown.

    A single run lists more than one -- a small model for internal steps and the
    one that answered -- so the receipt takes the most expensive entry and its
    canonical name.  Measured against claude 2.1.221 on 2026-08-22.
    """
    from harness.runner_slice import _dominant_model

    breakdown = {
        "claude-haiku-4-5-20251001": {"costUSD": 0.000632,
                                      "canonicalModel": "claude-haiku-4-5"},
        "claude-opus-5[1m]": {"costUSD": 0.0446825,
                              "canonicalModel": "claude-opus-5"},
    }
    assert _dominant_model(breakdown) == "claude-opus-5"
    # No breakdown is not an excuse to invent one.
    assert _dominant_model(None) == ""
    assert _dominant_model({}) == ""
    # Without a cost the raw key is still better than claiming nothing ran.
    assert _dominant_model({"some-model": {"canonicalModel": "some-model"}}) == "some-model"
    # Non-finite JSON numbers cannot pin the first entry as the apparent winner.
    assert _dominant_model({
        "poison": {"costUSD": float("nan")},
        "real": {"costUSD": 0.5, "canonicalModel": "real-model"},
    }) == "real-model"
