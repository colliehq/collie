"""Focused contracts for Mission-scoped, durable coding slices.

These tests intentionally exercise only public seams between the code primitive,
session persistence, host verification, and the Mission driver.  The real model
provider and real command runner are replaced with deterministic fakes.
"""
from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from harness import sessions
from harness.actions import ActionStore
from harness.codeworker import CodeSliceProcessRunner
from harness.jobs import (Capability, DONE_VERIFIED, NEEDS_YOU,
                          RECOVERY_REQUIRED, WAITING)
from harness.mission import (_compact_case_storage, MissionDriver, MissionStore,
                             ModelDecider, create_mission, world_leash)
from harness.missionweb import MissionService
from harness.providers import Completion, ModelProvider, Usage
from harness.primitives import _code_verify, _live_code, _real_code
from harness.recorder import RunResult
from harness.verification import workspace_snapshot
from harness.verifier import (CodeWorkspaceGoalVerifier, MissionGoalVerifier,
                              VERIFIED, INCONCLUSIVE)


class _Closer:
    def close(self):
        pass


class _Recorder(_Closer):
    def finish_run(self, _result):
        pass


class _FakeHarness:
    def __init__(self, result, seen):
        self._result = result
        self._seen = seen
        self.registry = SimpleNamespace(_tools={})
        self.provider = SimpleNamespace(subscription_only=False)
        self.memory = _Closer()
        self.recorder = _Recorder()
        self.max_turns = 0
        self.self_verify = True
        self.durable_session_id = ""
        self.checkpoint_scope = ""
        self.project = "mission-code-test"
        self.cwd = ""

    def run(self, task_id, prompt, history=None):
        self._seen.append({
            "task_id": task_id,
            "prompt": prompt,
            "history": history,
            "durable_session_id": self.durable_session_id,
            "max_turns": self.max_turns,
            "self_verify": self.self_verify,
        })
        return self._result

    def settle_run_memory(self, *_args, **_kwargs):
        pass


def _result(*, answer, messages, exhausted, verified=False, turns=3,
            input_tokens=11, output_tokens=7, cache_read=5,
            cache_creation=2, cost_usd=.25, error=""):
    return RunResult(
        task_id="code",
        answer=answer,
        messages=messages,
        turns_exhausted=exhausted,
        verified=verified,
        turns=turns,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_creation=cache_creation,
        cost_usd=cost_usd,
        error=error,
    )


def _install_fake_harnesses(monkeypatch, results):
    seen = []
    pending = list(results)

    def make_harness(*_args, **_kwargs):
        assert pending, "the code worker started more slices than the test supplied"
        return _FakeHarness(pending.pop(0), seen)

    monkeypatch.setattr("harness.cli.make_harness", make_harness)
    return seen


def test_live_code_resumes_one_stable_mission_session_and_reports_slice_usage(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("COLLIE_CODE_SLICE_TURNS", "9")

    first_messages = [
        {"role": "user", "content": "implement the retry"},
        {"role": "assistant", "content": "edited retry.py; more work remains"},
    ]
    final_messages = first_messages + [
        {"role": "user", "content": "continue from the durable checkpoint"},
        {"role": "assistant", "content": "done"},
    ]
    seen = _install_fake_harnesses(monkeypatch, [
        _result(answer="partial", messages=first_messages, exhausted=True,
                turns=9, input_tokens=101, output_tokens=31, cache_read=17,
                cache_creation=3, cost_usd=.41),
        _result(answer="done", messages=final_messages, exhausted=False,
                turns=4, input_tokens=52, output_tokens=13, cache_read=8,
                cache_creation=2, cost_usd=.19),
    ])
    verification_calls = []

    def host_verifier(root, result):
        verification_calls.append((root, result.answer))
        return ({"verified": False, "detail": "retry assertion still fails",
                 "evidence": {"output": "expected 2 but got 1"}}
                if len(verification_calls) == 1 else True)

    first = _live_code(
        "implement the retry", str(workspace), mission_id="overnight-42",
        host_verifier=host_verifier)
    second = _live_code(
        "implement the retry", str(workspace), mission_id="overnight-42",
        host_verifier=host_verifier)

    assert first["verified"] is False
    assert first["continue_needed"] is True
    assert first["_usage"] == {
        "input_tokens": 101,
        "output_tokens": 31,
        "cache_tokens": 20,
        "cost_usd": .41,
    }
    assert first["model_calls"] == 9
    assert first["turns"] == 9
    assert second["verified"] is True
    assert second["continue_needed"] is False
    assert second["model_calls"] == 4
    assert second["turns"] == 4

    assert seen[0]["history"] in (None, [])
    assert seen[1]["history"] == first_messages
    assert "retry assertion still fails" in seen[1]["prompt"]
    assert "expected 2 but got 1" in seen[1]["prompt"]
    assert seen[0]["durable_session_id"] == seen[1]["durable_session_id"]
    assert seen[0]["durable_session_id"].startswith("mission-code-")
    assert len(seen[0]["durable_session_id"]) <= 128
    assert seen[0]["max_turns"] == 9
    assert verification_calls == [
        (os.path.realpath(str(workspace)), "partial"),
        (os.path.realpath(str(workspace)), "done"),
    ]


def test_exhausted_last_turn_finishes_when_host_verification_passes(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    result = _result(
        answer="turn budget ended immediately after the fix",
        messages=[{"role": "assistant", "content": "fix applied"}],
        exhausted=True,
    )
    _install_fake_harnesses(monkeypatch, [result])
    calls = []

    def verifier(root, run_result):
        calls.append((root, run_result))
        return {"verified": True, "detail": "targeted check passed"}

    out = _live_code(
        "fix the bug", str(workspace), mission_id="last-turn-edit",
        host_verifier=verifier)

    assert len(calls) == 1, "host verification must run even when turns are exhausted"
    assert calls[0][1] is result
    assert out["verified"] is True
    assert out["continue_needed"] is False


def test_verifier_side_effects_never_become_agent_patch_provenance(
        tmp_path, monkeypatch):
    """A stable verifier artifact on slice two is not a Mission code patch.

    ``py_compile`` creates ``__pycache__`` on its first run. That cache is
    ignored as an untracked verifier artifact and can never be laundered into
    proof that the coding agent edited the project.
    """
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    _install_fake_harnesses(monkeypatch, [
        _result(answer="inspected", messages=[{
            "role": "assistant", "content": "no edit needed"}], exhausted=True),
        _result(answer="inspected again", messages=[{
            "role": "assistant", "content": "still no edit"}], exhausted=True),
    ])
    profile = {
        "profile": "overnight", "provider": "claude-agent-sdk",
        "model": "claude-opus-4-8", "subscription_only": True,
        "billing_mode": "subscription", "allow_provider_fallback": False,
    }

    first = _live_code(
        "inspect without editing", str(workspace), mission_id="verifier-provenance",
        execution_profile=profile, verify_command="python -m py_compile sample.py",
        baseline_tree_digest=baseline, expected_tree_digest=baseline)
    second = _live_code(
        "inspect without editing", str(workspace), mission_id="verifier-provenance",
        execution_profile=profile, verify_command="python -m py_compile sample.py",
        baseline_tree_digest=baseline,
        expected_tree_digest=first["post_tree_digest"])

    assert (workspace / "__pycache__").is_dir()
    assert first["verified"] is False
    assert second["verified"] is False
    assert first["agent_post_tree_digest"] == baseline
    assert first["post_tree_digest"] == baseline
    assert first["slice_mutated"] is False
    assert first["verifier_mutated"] is False
    assert second["slice_mutated"] is False
    assert second["patch_attributed"] is False
    assert second["verification"]["evidence"]["passed"] is True
    assert second["verification"]["evidence"]["patch_attributed"] is False

    saved = sessions.load(first["session_id"])
    slices = [row for row in saved["run_receipts"]
              if row.get("kind") == "mission_code_slice"]
    assert len(slices) == 2
    assert slices[0]["agent_post_tree_digest"] == baseline
    assert slices[0]["post_tree_digest"] == baseline
    assert slices[0]["agent_mutated"] is False
    assert slices[0]["verifier_mutated"] is False
    assert all(row["patch_attributed"] is False for row in slices)


def test_verifier_overwrite_cannot_be_laundered_as_agent_provenance(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "sample.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "verify.py").write_text(
        "from pathlib import Path\n"
        "p = Path('sample.py')\n"
        "p.write_text('VALUE = 3\\n', encoding='utf-8')\n"
        "assert p.read_text(encoding='utf-8') == 'VALUE = 3\\n'\n",
        encoding="utf-8")
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))

    results = [
        _result(answer="patched", messages=[{
            "role": "assistant", "content": "set value to two"}], exhausted=True),
        _result(answer="checked", messages=[{
            "role": "assistant", "content": "made no change"}], exhausted=False),
    ]
    mutations = [
        lambda: target.write_text("VALUE = 2\n", encoding="utf-8"),
        lambda: None,
    ]

    def make_harness(*_args, **_kwargs):
        harness = _FakeHarness(results.pop(0), [])
        mutation = mutations.pop(0)
        original_run = harness.run

        def run(*args, **kwargs):
            mutation()
            return original_run(*args, **kwargs)

        harness.run = run
        return harness

    monkeypatch.setattr("harness.cli.make_harness", make_harness)
    first = _live_code(
        "set the value to two", str(workspace), mission_id="verifier-overwrite",
        baseline_tree_digest=baseline, expected_tree_digest=baseline,
        verify_command="python verify.py")
    second = _live_code(
        "set the value to two", str(workspace), mission_id="verifier-overwrite",
        baseline_tree_digest=baseline,
        expected_tree_digest=first["post_tree_digest"],
        verify_command="python verify.py")

    assert first["verified"] is False
    assert first["verifier_mutated"] is True
    assert first["patch_attributed"] is False
    assert second["verification"]["evidence"]["passed"] is True
    assert second["patch_attributed"] is False
    assert second["verified"] is False
    assert target.read_text(encoding="utf-8") == "VALUE = 3\n"


def test_later_agent_revert_clears_historical_patch_before_verifier_artifact(
        tmp_path, monkeypatch):
    """A reverted historical edit cannot be revived by verifier-owned bytes."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "sample.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "verify.py").write_text(
        "from pathlib import Path\n"
        "assert Path('sample.py').read_text(encoding='utf-8') == 'VALUE = 1\\n'\n"
        "Path('build-artifact.txt').write_text('generated', encoding='utf-8')\n",
        encoding="utf-8")
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))

    results = [
        _result(answer="patched", messages=[{
            "role": "assistant", "content": "set value to two"}], exhausted=True),
        _result(answer="reverted", messages=[{
            "role": "assistant", "content": "restored original value"}], exhausted=False),
        _result(answer="checked again", messages=[{
            "role": "assistant", "content": "original value remains"}], exhausted=False),
    ]
    mutations = [
        lambda: target.write_text("VALUE = 2\n", encoding="utf-8"),
        lambda: target.write_text("VALUE = 1\n", encoding="utf-8"),
        lambda: None,
    ]

    def make_harness(*_args, **_kwargs):
        result = results.pop(0)
        mutation = mutations.pop(0)
        harness = _FakeHarness(result, [])
        original_run = harness.run

        def run(*args, **kwargs):
            mutation()
            return original_run(*args, **kwargs)

        harness.run = run
        return harness

    monkeypatch.setattr("harness.cli.make_harness", make_harness)
    first = _live_code(
        "change then reconsider", str(workspace), mission_id="reverted-patch",
        baseline_tree_digest=baseline, expected_tree_digest=baseline,
        host_verifier=lambda *_args: {"verified": False})

    assert first["patch_attributed"] is True
    assert first["agent_post_tree_digest"] != baseline

    second = _live_code(
        "restore the original", str(workspace), mission_id="reverted-patch",
        baseline_tree_digest=baseline,
        expected_tree_digest=first["post_tree_digest"],
        verify_command="python verify.py")

    assert second["agent_post_tree_digest"] == baseline
    assert second["post_tree_digest"] != baseline
    assert (workspace / "build-artifact.txt").is_file()
    assert second["verifier_mutated"] is True
    assert second["patch_attributed"] is False
    assert second["verification"]["evidence"]["patch_attributed"] is False
    assert second["verified"] is False

    # On the next slice the same verifier leaves its existing artifact
    # unchanged, so its host evidence is fresh and passing.  Replaying the first
    # slice's historical mutation must still not resurrect patch provenance.
    third = _live_code(
        "confirm the original", str(workspace), mission_id="reverted-patch",
        baseline_tree_digest=baseline,
        expected_tree_digest=second["post_tree_digest"],
        verify_command="python verify.py")

    assert third["verification"]["evidence"]["passed"] is True
    assert third["patch_attributed"] is False
    assert third["verification"]["evidence"]["patch_attributed"] is False
    assert third["verified"] is False


def test_code_verify_accepts_a_durably_checkpointed_yield():
    verdict = _code_verify(None, {
        "result": "slice stopped at its bounded turn budget",
        "verified": False,
        "continue_needed": True,
        "session_id": "mission-code-deadbeef",
    })

    assert verdict.status == VERIFIED
    assert "continu" in verdict.reason.lower() or "checkpoint" in verdict.reason.lower()


def test_real_code_passes_mission_identity_to_new_runners_and_keeps_legacy_runner():
    record = SimpleNamespace(
        args={"goal": "finish parser", "workspace": "/bound/repo"},
        job_id="mission-abc",
    )
    observed = {}

    def mission_runner(goal, *, workspace=None, mission_id=None):
        observed.update(goal=goal, workspace=workspace, mission_id=mission_id)
        return {"answer": "slice saved", "verified": False,
                "continue_needed": True, "session_id": "mission-code-abc"}

    modern = _real_code(mission_runner)(record)
    assert observed == {
        "goal": "finish parser",
        "workspace": "/bound/repo",
        "mission_id": "mission-abc",
    }
    assert modern["continue_needed"] is True
    assert modern["case"]["code_pending"] is True

    legacy_calls = []
    legacy = _real_code(
        lambda goal: legacy_calls.append(goal) or
        {"answer": "legacy complete", "verified": True})(record)
    assert legacy_calls == ["finish parser"]
    assert legacy["verified"] is True


def test_case_compaction_preserves_overnight_execution_and_code_profiles():
    execution_profile = {
        "name": "overnight",
        "max_elapsed_seconds": 12 * 60 * 60,
        "max_active_wall_seconds": 12 * 60 * 60,
    }
    code_profile = {
        "slice_turns": 12,
        "verify_command": "python -m pytest -q",
        "workspace": "C:/bound/repo",
    }
    compacted = _compact_case_storage({
        "execution_profile": execution_profile,
        "code_profile": code_profile,
        "code_expected_tree_digest": "f" * 64,
        "code_recovery_required": True,
        "old_research_blob": "x" * 20_000,
    }, max_chars=2_000)

    assert compacted["execution_profile"] == execution_profile
    assert compacted["code_profile"] == code_profile
    assert compacted["code_expected_tree_digest"] == "f" * 64
    assert compacted["code_recovery_required"] is True


def test_overnight_voluntary_answer_keeps_running_until_host_gate_is_green(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    seen = _install_fake_harnesses(monkeypatch, [_result(
        answer="I think this is done", messages=[{"role": "assistant", "content": "done"}],
        exhausted=False)])

    out = _live_code(
        "finish it", str(workspace), mission_id="voluntary-stop",
        execution_profile={"profile": "overnight", "provider": "claude-agent-sdk",
                           "model": "claude-opus-4-8", "subscription_only": True,
                           "billing_mode": "subscription",
                           "allow_provider_fallback": False},
        host_verifier=lambda *_: {"verified": False, "detail": "still red"})

    assert out["recovery_required"] is False
    assert out["continue_needed"] is True
    assert seen[0]["self_verify"] is False


def test_completed_slice_advances_owned_digest_and_external_drift_is_fenced(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "value.py"
    target.write_text("value = 1\n", encoding="utf-8")
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    made = []

    class EditingHarness(_FakeHarness):
        def run(self, task_id, prompt, history=None):
            target.write_text("value = 2\n", encoding="utf-8")
            return super().run(task_id, prompt, history)

    result = _result(
        answer="slice", messages=[{"role": "assistant", "content": "edited"}],
        exhausted=True)

    def make_harness(*_args, **_kwargs):
        harness = EditingHarness(result, made)
        return harness

    monkeypatch.setattr("harness.cli.make_harness", make_harness)
    first = _live_code(
        "change it", str(workspace), mission_id="owned-chain",
        baseline_tree_digest=baseline, expected_tree_digest=baseline,
        host_verifier=lambda *_: False)
    assert first["continue_needed"] is True
    assert first["post_tree_digest"] != baseline

    # A byte change not connected to the completed receipt chain is never
    # laundered into Collie's patch, even if a verifier would now be green.
    target.write_text("value = 999\n", encoding="utf-8")
    second = _live_code(
        "change it", str(workspace), mission_id="owned-chain",
        baseline_tree_digest=baseline,
        expected_tree_digest=first["post_tree_digest"],
        host_verifier=lambda *_: True)
    assert second["recovery_required"] is True
    assert second["verified"] is False
    assert len(made) == 1, "external drift must be detected before another model call"


def test_transient_provider_error_after_safe_edit_continues_from_checkpoint(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "retry.py"
    target.write_text("ready = False\n", encoding="utf-8")
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))

    class EditingHarness(_FakeHarness):
        def run(self, task_id, prompt, history=None):
            target.write_text("ready = True\n", encoding="utf-8")
            return super().run(task_id, prompt, history)

    result = _result(
        answer="ERROR(provider): 429 rate limit", error="429 rate limit",
        messages=[{"role": "assistant", "content": "edit checkpointed"}],
        exhausted=False)
    monkeypatch.setattr(
        "harness.cli.make_harness",
        lambda *_args, **_kwargs: EditingHarness(result, []))

    out = _live_code(
        "finish safely", str(workspace), mission_id="transient-edit",
        baseline_tree_digest=baseline, expected_tree_digest=baseline,
        execution_profile={"profile": "overnight", "provider": "claude-agent-sdk",
                           "model": "claude-opus-4-8", "subscription_only": True,
                           "billing_mode": "subscription",
                           "allow_provider_fallback": False},
        host_verifier=lambda *_: False)

    assert out["transient"] is True
    assert out["slice_mutated"] is True
    assert out["recovery_required"] is False
    assert out["continue_needed"] is True


def test_overnight_code_constructs_agent_sdk_provider_as_subscription_only(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    captured = {}
    result = _result(
        answer="waiting", messages=[{"role": "assistant", "content": "waiting"}],
        exhausted=True)

    def make_harness(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeHarness(result, [])

    monkeypatch.setattr("harness.cli.make_harness", make_harness)

    out = _live_code(
        "continue", str(workspace), mission_id="subscription-constructor",
        execution_profile={"profile": "overnight", "provider": "claude-agent-sdk",
                           "model": "claude-opus-4-8", "subscription_only": True,
                           "billing_mode": "subscription",
                           "allow_provider_fallback": False},
        host_verifier=lambda *_: False)

    assert captured["subscription_only"] is True
    assert out["continue_needed"] is True


@pytest.mark.parametrize("contents", ["{", "[]", '{"messages":"not-a-list"}'])
def test_existing_corrupt_code_session_fails_closed_before_model_call(
        tmp_path, monkeypatch, contents):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "mission-code-corrupt"
    (session_dir / (sid + ".json")).write_text(contents, encoding="utf-8")
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(session_dir))
    monkeypatch.setattr(
        "harness.cli.make_harness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("model must not run with corrupt durable state")))

    out = _live_code("continue", str(workspace), mission_id="corrupt", session_id=sid)

    assert out["recovery_required"] is True


@pytest.mark.parametrize("broken", [
    {"active_run": "corrupt"},
    {"active_run": {"run_id": "r", "turn": 0, "state": "mystery", "detail": {}}},
    {"active_run": {"run_id": "r", "turn": 0, "state": "calling_model",
                    "detail": "corrupt"}},
    {"run_receipts": "corrupt"},
])
def test_semantically_corrupt_code_session_fails_closed_before_model_call(
        monkeypatch, tmp_path, broken):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(workspace))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    sid = "mission-code-semantic-corrupt"
    path = sessions._path(sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"id": sid, "cwd": str(workspace), "messages": []}
    payload.update(broken)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    out = _live_code("continue", str(workspace), mission_id="corrupt", session_id=sid)

    assert out["recovery_required"] is True
    assert "corrupt" in out["answer"]
    assert out["continue_needed"] is False


def test_code_session_workspace_mismatch_fails_closed(tmp_path, monkeypatch):
    from harness import sessions

    workspace = tmp_path / "repo"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(session_dir))
    sid = "mission-code-wrong-workspace"
    sessions.save(sid, [], cwd=str(other))
    monkeypatch.setattr(
        "harness.cli.make_harness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("model must not run in a mismatched workspace")))

    out = _live_code("continue", str(workspace), mission_id="wrong", session_id=sid)

    assert out["recovery_required"] is True
    assert "different workspace" in out["answer"]


def test_mission_driver_yields_immediately_for_a_continuing_code_slice(tmp_path):
    store = MissionStore(str(tmp_path / "missions.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    decisions = []

    def decider(_goal, _case, _capabilities):
        decisions.append(time.time())
        if len(decisions) > 1:
            raise AssertionError("continuing slice must yield before another model decision")
        return {"action": "slice.code", "args": {"goal": "keep coding"}}

    cap = Capability(
        "slice.code",
        execute=lambda _record: {
            "case": {"coded": True, "code_pending": True},
            "result": "durable slice checkpointed",
            "verified": False,
            "continue_needed": True,
            "session_id": "mission-code-1234",
            "_usage": {
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_tokens": 3,
                "cost_usd": .01,
            },
            "model_calls": 5,
            "turns": 5,
        },
        verify=_code_verify,
        reversible=True,
        risk="read",
    )
    create_mission(
        store, "overnight", "finish a large implementation",
        leash=world_leash(may=["slice.code"], autonomous=True))
    before = int(time.time())

    state = MissionDriver(store, actions, decider, [cap]).advance("overnight")

    assert state == WAITING
    assert len(decisions) == 1
    wait = store.next_wait("overnight")
    assert wait is not None
    assert before + 1 <= wait["fire_at"] <= int(time.time()) + 1
    mission = store.get("overnight")
    assert mission.case["coded"] is True
    assert mission.case["code_pending"] is True
    assert mission.case["slice.code"]["continue_needed"] is True
    runtime = store.runtime("overnight")
    assert runtime["input_tokens"] >= 12
    assert runtime["output_tokens"] >= 4
    assert runtime["cache_tokens"] >= 3
    store.close()
    actions.close()


def test_code_goal_verifier_rechecks_current_bytes_and_rejects_stale_receipt(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "fix.py"
    target.write_text("broken = True\n", encoding="utf-8")
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    target.write_text("broken = False\n", encoding="utf-8")
    post = workspace_snapshot(str(workspace))
    evidence = {
        "timestamp": time.time(), "passed": True, "command_passed": True,
        "ran_after_last_edit": True, "post_tree_digest": post["tree_digest"],
        "post_snapshot_complete": post["snapshot_complete"],
        "command": "python verify.py", "source": "mission_code_profile",
    }
    mission = SimpleNamespace(case={
        "code_profile": {"verify_command": "python verify.py"},
        "code_verified": True, "_isolated_workspace": str(workspace),
        "code_baseline_tree_digest": baseline,
        "code_verification": {"verified": True, "evidence": evidence},
    })

    verdict = CodeWorkspaceGoalVerifier().verify_mission(mission)
    assert verdict.status == VERIFIED
    assert verdict.evidence[0].channel == "host-verification-command"

    target.write_text("broken = 'changed after verification'\n", encoding="utf-8")
    assert CodeWorkspaceGoalVerifier().verify_mission(mission).status == INCONCLUSIVE


def test_default_mission_goal_verifier_routes_code_and_campaign(tmp_path):
    store = MissionStore(str(tmp_path / "missions.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    verifier = MissionGoalVerifier(store, actions)
    code = SimpleNamespace(case={"code_profile": {}, "code_verified": False})
    campaign = SimpleNamespace(mission_id="missing", case={})
    assert "code" in verifier.verify_mission(code).reason
    assert "coverage" in verifier.verify_mission(campaign).reason
    store.close()
    actions.close()


def test_subscription_decider_tracks_equivalent_value_but_charges_zero():
    class Provider:
        model = "claude-opus-4-8"
        subscription_only = True

        def complete(self, *_args, **_kwargs):
            return Completion(
                text='{"action":"needs_human","args":{"summary":"done"}}',
                request_count=2,
                usage=Usage(input_tokens=1_000_000, output_tokens=0))

    decision = ModelDecider(Provider())("goal", {}, [])
    assert decision["_cost_usd"] == 0.0
    assert decision["_equivalent_cost_usd"] == 5.0
    assert decision["_model_calls"] == 2
    assert MissionDriver._usage_from_decision(decision)["model_calls"] == 1


def test_only_transport_aware_provider_switches_decider_to_physical_gate():
    class LegacyProvider:
        def complete(self, *_args, **_kwargs):
            return Completion(text='{"action":"needs_human","args":{}}')

    class GatedProvider(LegacyProvider):
        supports_request_gate = True

    assert ModelDecider(LegacyProvider()).supports_request_gate is False
    assert ModelDecider(GatedProvider()).supports_request_gate is True


def test_transport_reserved_decision_is_not_postcharged_again():
    class Provider:
        model = "claude-opus-4-8"
        subscription_only = True
        supports_request_gate = True

        def complete(self, *_args, **_kwargs):
            return Completion(
                text='{"action":"needs_human","args":{"summary":"done"}}',
                request_count=1, usage=Usage(input_tokens=3))

    reserved = []
    decision = ModelDecider(Provider())(
        "goal", {}, [], request_gate=lambda purpose: reserved.append(purpose) or "r1",
        request_complete=lambda *_args: None)

    assert reserved == ["mission_decider"]
    assert decision["_model_calls_reserved"] is True
    assert MissionDriver._usage_from_decision(decision)["model_calls"] == 0


def test_transport_reservation_is_context_bound_at_the_physical_provider_call():
    calls = []

    class Provider(ModelProvider):
        model = "claude-opus-4-8"
        subscription_only = True
        supports_request_gate = True

        def complete(self, *_args, **_kwargs):
            gate, complete = self.current_request_authority()
            calls.append(("scope", self.current_request_scope()))
            request_id = gate("physical_transport")
            calls.append(request_id)
            complete(request_id, "completed")
            return Completion(
                text='{"action":"needs_human","args":{"summary":"done"}}',
                usage=Usage(input_tokens=3))

    completed = []
    decision = ModelDecider(Provider())(
        "goal", {}, [], request_gate=lambda purpose: calls.append(purpose) or "r1",
        request_complete=lambda *args: completed.append(args),
        request_scope="mission-one")

    assert calls == [("scope", "mission-one"), "physical_transport", "r1"]
    assert completed == [("r1", "completed")]
    assert decision["_model_calls_reserved"] is True


def test_durable_code_mission_closes_from_host_evidence_not_campaign_receipts(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "fix.py"
    target.write_text("broken = True\n", encoding="utf-8")
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    decisions = iter([
        {"action": "code", "args": {"goal": "fix it"}},
        {"action": "done", "reason": "implementation is ready"},
    ])

    def runner(_goal, *, workspace=None, **_context):
        target.write_text("broken = False\n", encoding="utf-8")
        post = workspace_snapshot(workspace)
        return {
            "answer": "fixed", "verified": True, "session_id": "mission-code-e2e",
            "baseline_tree_digest": baseline,
            "verification": {"verified": True, "evidence": {
                "timestamp": time.time(), "passed": True, "command_passed": True,
                "ran_after_last_edit": True,
                "post_tree_digest": post["tree_digest"],
                "post_snapshot_complete": post["snapshot_complete"],
                "command": "python verify.py", "source": "mission_code_profile",
            }},
        }

    store = MissionStore(str(tmp_path / "missions.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                     reversible=True, risk="read")
    create_mission(
        store, "code-e2e", "fix it and prove it",
        case={"_isolated_workspace": str(workspace),
              "code_profile": {"verify_command": "python verify.py"}},
        leash=world_leash(may=["code"], autonomous=True,
                          workspace_mode="isolated"))
    driver = MissionDriver(
        store, actions, lambda *_args: next(decisions), [cap],
        goal_verifier=MissionGoalVerifier(store, actions))

    assert driver.advance("code-e2e") == DONE_VERIFIED
    mission = store.get("code-e2e")
    assert mission.case["code_verified"] is True
    assert "current Mission patch" in mission.result
    store.close()
    actions.close()


def test_fake_clock_eleven_hour_code_run_survives_repeated_daemon_restarts(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "long_fix.py"
    target.write_text("done = False\n", encoding="utf-8")
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    mission_path = str(tmp_path / "missions.db")
    action_path = str(tmp_path / "actions.db")
    clock = [1_900_000_000.0]
    monkeypatch.setattr("harness.mission.time.time", lambda: clock[0])
    calls = []

    def runner(_goal, *, mission_id=None, workspace=None, **_context):
        calls.append((mission_id, workspace))
        if len(calls) < 12:
            return {"answer": "checkpoint", "verified": False,
                    "continue_needed": True, "session_id": "mission-code-long",
                    "turns_exhausted": True, "turns": 3, "model_calls": 3}
        target.write_text("done = True\n", encoding="utf-8")
        post = workspace_snapshot(workspace)
        return {
            "answer": "complete", "verified": True,
            "session_id": "mission-code-long", "baseline_tree_digest": baseline,
            "verification": {"verified": True, "evidence": {
                "timestamp": clock[0], "passed": True, "command_passed": True,
                "ran_after_last_edit": True,
                "post_tree_digest": post["tree_digest"],
                "post_snapshot_complete": post["snapshot_complete"],
                "command": "python verify.py", "source": "mission_code_profile",
            }},
        }

    def open_driver():
        store = MissionStore(mission_path)
        actions = ActionStore(action_path)

        def decide(_goal, case, _capabilities):
            return ({"action": "done", "reason": "host gate passed"}
                    if case.get("code_verified") else
                    {"action": "code", "args": {"goal": "finish long fix"}})

        cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                         reversible=True, risk="read")
        driver = MissionDriver(
            store, actions, decide, [cap],
            goal_verifier=MissionGoalVerifier(store, actions))
        return store, actions, driver

    store, actions, driver = open_driver()
    create_mission(
        store, "long-code", "finish while unattended",
        case={"_isolated_workspace": str(workspace),
              "code_profile": {"verify_command": "python verify.py"}},
        leash=world_leash(
            may=["code"], autonomous=True, workspace_mode="isolated",
            max_total_steps=100, max_model_calls=200,
            max_elapsed_seconds=12 * 60 * 60,
            max_active_wall_seconds=12 * 60 * 60))

    for hour in range(12):
        assert driver.tick_missions(now=int(clock[0]), max_workers=1) == 1
        state = store.get("long-code").state
        if hour < 11:
            assert state == WAITING
        if hour in (2, 5, 8):
            store.close()
            actions.close()
            store, actions, driver = open_driver()
        clock[0] += 60 * 60

    assert store.get("long-code").state == DONE_VERIFIED
    assert len(calls) == 12
    assert {mission_id for mission_id, _workspace in calls} == {"long-code"}
    assert {os.path.realpath(path) for _mission_id, path in calls} == {
        os.path.realpath(str(workspace))}
    runtime = store.runtime("long-code")
    assert runtime["model_calls"] >= 12 + 11 * 3
    assert runtime["model_cost_usd"] == 0.0
    store.close()
    actions.close()


def test_code_slice_process_runs_from_an_uninstalled_source_checkout(tmp_path, monkeypatch):
    workspace = tmp_path / "isolated-repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    out = CodeSliceProcessRunner()(
        "Inspect the workspace and report what remains.", workspace=str(workspace),
        mission_id="process-boundary", execution_profile={
            "provider": "mock", "model": "mock-planner-v1",
            "billing_mode": "local", "subscription_only": False,
            "allow_provider_fallback": False,
        }, max_wall_seconds=30)

    assert out["session_id"].startswith("mission-code-")
    assert isinstance(out["verified"], bool)
    assert out["turns"] >= 1


def test_code_slice_process_searches_its_bound_workspace(tmp_path):
    """The real worker must retain enough non-secret OS context for grep.

    On Windows this covers the sanitized child environment locating Git Bash;
    without ProgramFiles the POSIX-quoted pattern ran under cmd.exe and silently
    reported no matches even though every cwd/workspace binding was correct.
    """
    if os.name == "nt":
        from harness import plat
        if not plat.has_posix_shell():
            pytest.skip("Windows cross-process search requires an installed POSIX shell")
    workspace = tmp_path / "isolated-search-repo"
    workspace.mkdir()
    (workspace / "marker.py").write_text(
        "# TODO: cross_process_workspace_marker\n", encoding="utf-8")
    runner = CodeSliceProcessRunner(
        session_dir=str(tmp_path / "sessions"),
        worker_dir=str(tmp_path / "workers"))

    out = runner(
        "Find the TODO in the bound workspace and report it.",
        workspace=str(workspace), mission_id="process-search-boundary",
        execution_profile={
            "provider": "mock", "model": "mock-planner-v1",
            "billing_mode": "local", "subscription_only": False,
            "allow_provider_fallback": False,
        }, max_wall_seconds=30)

    assert "marker.py" in out["answer"], out["answer"]
    assert "cross_process_workspace_marker" in out["answer"], out["answer"]


def test_code_process_cancellation_is_scoped_to_the_target_mission(tmp_path, monkeypatch):
    killed = []
    both_started = threading.Event()
    release = threading.Event()

    class Proc:
        next_pid = 100

        def __init__(self, *_args, **_kwargs):
            self.pid = Proc.next_pid
            Proc.next_pid += 1
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            both_started.wait(2)
            release.wait(2)
            self.returncode = 0

    monkeypatch.setattr("harness.plat.kill_tree", lambda proc: killed.append(proc.pid))
    runner = CodeSliceProcessRunner(popen=Proc)
    workspaces = []
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        workspaces.append(root)
    errors = []

    def run(mid, root):
        try:
            runner("work", workspace=str(root), mission_id=mid)
        except RuntimeError:
            # Fake processes intentionally write no result receipt.
            pass
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=("a", workspaces[0])),
               threading.Thread(target=run, args=("b", workspaces[1]))]
    for thread in threads:
        thread.start()
    deadline = time.time() + 2
    while len(runner._procs) < 2 and time.time() < deadline:
        time.sleep(.01)
    target_pid = runner._procs["a"].pid
    both_started.set()
    runner.cancel_current("a")
    release.set()
    for thread in threads:
        thread.join(3)

    assert errors == []
    assert killed == [target_pid]


def test_code_process_cancel_during_popen_waits_for_gated_startup(tmp_path, monkeypatch):
    spawned = threading.Event()
    let_popen_return = threading.Event()
    killed = []
    observed = {}

    class Proc:
        pid = 321

        def __init__(self, argv, **_kwargs):
            self.returncode = None
            observed["gate"] = argv[argv.index("--start-gate") + 1]
            spawned.set()
            let_popen_return.wait(3)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                raise TimeoutError("fake process was not terminated")
            return self.returncode

    def kill(proc):
        killed.append(proc.pid)
        proc.returncode = -9

    monkeypatch.setattr("harness.plat.kill_tree", kill)
    runner = CodeSliceProcessRunner(
        popen=Proc, session_dir=str(tmp_path / "sessions"),
        worker_dir=str(tmp_path / "workers"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    run_errors = []
    cancel_result = []

    def run():
        try:
            runner("work", workspace=str(workspace), mission_id="race")
        except Exception as exc:
            run_errors.append(exc)

    run_thread = threading.Thread(target=run)
    run_thread.start()
    assert spawned.wait(2)
    cancel_thread = threading.Thread(
        target=lambda: cancel_result.append(runner.cancel_current("race")))
    cancel_thread.start()
    time.sleep(.05)

    assert cancel_thread.is_alive(), "startup intent must not look like safe process absence"
    assert not os.path.exists(observed["gate"]), "the child must remain behind its start gate"
    let_popen_return.set()
    run_thread.join(3)
    cancel_thread.join(3)

    assert cancel_result == [True]
    assert killed == [321]
    assert len(run_errors) == 1 and "cancelled during startup" in str(run_errors[0])
    assert not runner.has_owned_worker("race")


def test_dead_local_code_process_retires_its_matching_receipt(tmp_path):
    class DeadProc:
        pid = 654
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    runner = CodeSliceProcessRunner(
        popen=lambda *_args, **_kwargs: DeadProc(),
        session_dir=str(tmp_path / "sessions"), worker_dir=str(tmp_path / "workers"))
    token = "dead-owner-token"
    path = runner._write_worker_receipt("dead", {
        "version": 2, "mission_id": "dead", "pid": 654, "pgid": 0,
        "process_identity": "", "job_name": "", "request_path": "request",
        "token": token,
    })
    runner._procs["dead"] = DeadProc()
    runner._jobs["dead"] = None
    runner._owners["dead"] = {"path": path, "token": token, "pgid": 0}

    assert runner.cancel_current("dead", include_persisted=False) is True
    assert not os.path.exists(path)
    assert runner.has_owned_worker("dead") is False


def test_dead_or_reused_posix_leader_never_authorizes_orphan_group_kill(
        tmp_path, monkeypatch):
    runner = CodeSliceProcessRunner(
        popen=lambda *_args, **_kwargs: None,
        session_dir=str(tmp_path / "sessions"), worker_dir=str(tmp_path / "workers"))
    token = "posix-owner-token"
    path = runner._write_worker_receipt("orphan", {
        "version": 2, "mission_id": "orphan", "pid": 777, "pgid": 777,
        "process_identity": "linux:boot:123", "job_name": "",
        "request_path": "unique-request", "token": token,
    })
    monkeypatch.setattr("harness.codeworker._is_windows", lambda: False)
    monkeypatch.setattr(runner, "_process_matches", lambda *_args: False)
    monkeypatch.setattr(runner, "_posix_group_extinct", lambda _pgid: False)
    signalled = []
    monkeypatch.setattr("harness.codeworker.os.killpg",
                        lambda pgid, sig: signalled.append((pgid, sig)), raising=False)

    assert runner.cancel_persisted("orphan") is False
    assert os.path.exists(path), "an unprovable orphan group must remain fenced"
    assert signalled == []

    monkeypatch.setattr(runner, "_posix_group_extinct", lambda _pgid: True)
    assert runner.cancel_persisted("orphan") is True
    assert not os.path.exists(path)
    assert signalled == []


def test_persisted_windows_worker_requires_named_job_extinction_proof(
        tmp_path, monkeypatch):
    runner = CodeSliceProcessRunner(
        popen=lambda *_args, **_kwargs: None,
        session_dir=str(tmp_path / "sessions"), worker_dir=str(tmp_path / "workers"))
    token = "windows-owner-token"
    path = runner._write_worker_receipt("windows", {
        "version": 2, "mission_id": "windows", "pid": 778, "pgid": 0,
        "process_identity": "", "job_name": "Local\\CollieMissionCode-generation",
        "request_path": "unique-request", "token": token,
    })
    monkeypatch.setattr("harness.codeworker._is_windows", lambda: True)
    outcomes = iter((False, True))
    names = []

    def terminate_and_wait(name):
        names.append(name)
        return next(outcomes)

    monkeypatch.setattr(
        "harness.plat.terminate_named_job_and_wait", terminate_and_wait)

    assert runner.cancel_persisted("windows") is False
    assert os.path.exists(path), "delivery without extinction proof must keep the fence"
    assert runner.cancel_persisted("windows") is True
    assert not os.path.exists(path)
    assert names == ["Local\\CollieMissionCode-generation"] * 2


def test_unversioned_workspace_snapshot_binds_file_content_not_only_metadata(tmp_path):
    target = tmp_path / "same-size.py"
    target.write_text("value = 1\n", encoding="utf-8")
    first = workspace_snapshot(str(tmp_path))
    original_times = (target.stat().st_atime_ns, target.stat().st_mtime_ns)

    target.write_text("value = 2\n", encoding="utf-8")
    os.utime(target, ns=original_times)
    second = workspace_snapshot(str(tmp_path))

    assert first["snapshot_kind"] == "filesystem"
    assert first["snapshot_complete"] is True
    assert second["snapshot_complete"] is True
    assert first["tree_digest"] != second["tree_digest"]


def test_live_code_persists_the_original_baseline_before_a_crashing_edit(
        tmp_path, monkeypatch):
    from harness import sessions

    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "fix.py"
    target.write_text("broken = True\n", encoding="utf-8")
    before = workspace_snapshot(str(workspace))["tree_digest"]
    session_dir = tmp_path / "sessions"
    session_id = "mission-code-crash-baseline"
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(workspace))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(session_dir))

    class CrashingHarness(_FakeHarness):
        def run(self, *_args, **_kwargs):
            target.write_text("broken = False\n", encoding="utf-8")
            raise RuntimeError("simulated provider crash after edit")

    monkeypatch.setattr(
        "harness.cli.make_harness",
        lambda *_args, **_kwargs: CrashingHarness(None, []))

    with pytest.raises(RuntimeError, match="after edit"):
        _live_code(
            "fix it", str(workspace), mission_id="baseline-crash",
            session_id=session_id)

    saved = sessions.load(session_id)
    anchors = [row for row in saved["run_receipts"]
               if row.get("kind") == "mission_code_baseline"]
    assert len(anchors) == 1
    assert anchors[0]["baseline_tree_digest"] == before
    assert workspace_snapshot(str(workspace))["tree_digest"] != before


def test_live_code_never_starts_edit_when_baseline_receipt_cannot_persist(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "fix.py").write_text("broken = True\n", encoding="utf-8")
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(workspace))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sessions, "append_run_receipt", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        "harness.cli.make_harness",
        lambda *_a, **_kw: pytest.fail("model must not start without a durable baseline"))

    out = _live_code(
        "fix it", str(workspace), mission_id="baseline-write-failed",
        session_id="mission-code-baseline-write-failed")

    assert out["recovery_required"] is True
    assert out["verified"] is False
    assert "baseline" in out["answer"]


def test_live_code_fails_closed_when_post_edit_receipt_cannot_persist(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(workspace))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    session_id = "mission-code-slice-write-failed"
    _install_fake_harnesses(monkeypatch, [_result(
        answer="done", messages=[{"role": "assistant", "content": "done"}],
        exhausted=False, verified=True)])
    append = sessions.append_run_receipt

    def fail_slice_receipt(sid, receipt, **kwargs):
        if receipt.get("kind") == "mission_code_slice":
            return False
        return append(sid, receipt, **kwargs)

    monkeypatch.setattr(sessions, "append_run_receipt", fail_slice_receipt)

    out = _live_code(
        "fix it", str(workspace), mission_id="slice-write-failed",
        session_id=session_id, host_verifier=lambda *_a: {"verified": True})

    assert out["recovery_required"] is True
    assert out["verified"] is False
    assert out["continue_needed"] is False
    assert "ownership receipt" in out["answer"]
    saved = sessions.load(session_id)
    assert not any(row.get("kind") == "mission_code_slice"
                   for row in saved.get("run_receipts") or [])


def test_completed_reconcile_adopts_crashed_edit_and_next_slice_runs(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "fix.py"
    target.write_text("broken = True\n", encoding="utf-8")
    state = tmp_path / "state"
    session_dir = state / "mission-code-sessions"
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(workspace))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(session_dir))
    service = MissionService(
        state_dir=str(state), provider="anthropic-oauth",
        model="claude-opus-4-8", decider=lambda *_args: {}, stub=True)
    created = service.start(
        "finish the interrupted edit", code=True, workspace=str(workspace))
    mid = created["mission_id"]
    before_case = service.store.get(mid).case
    before = before_case["code_expected_tree_digest"]
    sid = before_case["code_profile"]["session_id"]

    class CrashingHarness(_FakeHarness):
        def run(self, *_args, **_kwargs):
            target.write_text("broken = False\n", encoding="utf-8")
            raise RuntimeError("simulated crash after the edit")

    monkeypatch.setattr(
        "harness.cli.make_harness",
        lambda *_args, **_kwargs: CrashingHarness(None, []))
    with pytest.raises(RuntimeError, match="after the edit"):
        _live_code(
            "fix it", str(workspace), mission_id=mid, session_id=sid,
            execution_profile=before_case["execution_profile"],
            baseline_tree_digest=before_case["code_baseline_tree_digest"],
            expected_tree_digest=before)
    after = workspace_snapshot(str(workspace))["tree_digest"]
    assert after != before
    service.store.set_state(mid, RECOVERY_REQUIRED, "worker crashed after editing")

    reconciled = service.reconcile(
        mid, "the intended edit is present", code_resolution="completed")

    assert reconciled["state"] == "queued"
    adopted = service.store.get(mid).case
    assert adopted["code_expected_tree_digest"] == after
    assert adopted["code_recovery_required"] is False
    checked = sessions.load_checked(sid, directory=str(session_dir))
    receipts = checked["session"]["run_receipts"]
    assert any(
        row.get("kind") == "mission_code_reconciled" and
        row.get("pre_tree_digest") == before and
        row.get("post_tree_digest") == after and
        row.get("resolution") == "completed"
        for row in receipts)

    # Deliberately provide the stale Mission boundary: the durable reconciled
    # receipt must advance it contiguously before the next model slice starts.
    seen = _install_fake_harnesses(monkeypatch, [_result(
        answer="continued safely", messages=[
            {"role": "assistant", "content": "continued safely"}],
        exhausted=False, verified=True, turns=1)])
    out = _live_code(
        "finish it", str(workspace), mission_id=mid, session_id=sid,
        execution_profile=before_case["execution_profile"],
        baseline_tree_digest=before_case["code_baseline_tree_digest"],
        expected_tree_digest=before,
        host_verifier=lambda *_args: {"verified": True})

    assert len(seen) == 1
    assert out["expected_tree_digest"] == after
    assert out["recovery_required"] is False
    service.close()


def test_not_fired_reconcile_refuses_workspace_drift(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "fix.py"
    target.write_text("broken = True\n", encoding="utf-8")
    state = tmp_path / "state"
    service = MissionService(
        state_dir=str(state), provider="anthropic-oauth",
        model="claude-opus-4-8", decider=lambda *_args: {}, stub=True)
    created = service.start("inspect the edit", code=True, workspace=str(workspace))
    mid = created["mission_id"]
    expected = service.store.get(mid).case["code_expected_tree_digest"]
    target.write_text("broken = False\n", encoding="utf-8")
    service.store.set_state(mid, RECOVERY_REQUIRED, "worker outcome uncertain")

    refused = service.reconcile(
        mid, "the edit supposedly did not fire", code_resolution="not_fired")

    assert refused["state"] == RECOVERY_REQUIRED
    assert "original expected digest" in refused["error"]
    assert service.store.get(mid).case["code_expected_tree_digest"] == expected
    service.close()


def test_code_reconcile_fails_closed_on_malformed_session(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "fix.py").write_text("broken = True\n", encoding="utf-8")
    state = tmp_path / "state"
    service = MissionService(
        state_dir=str(state), provider="anthropic-oauth",
        model="claude-opus-4-8", decider=lambda *_args: {}, stub=True)
    created = service.start("recover carefully", code=True, workspace=str(workspace))
    mid = created["mission_id"]
    case = service.store.get(mid).case
    sid = case["code_profile"]["session_id"]
    session_dir = state / "mission-code-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / (sid + ".json")).write_text(
        json.dumps({"id": sid, "messages": "not-a-list"}), encoding="utf-8")
    service.store.set_state(mid, RECOVERY_REQUIRED, "journal may be torn")

    refused = service.reconcile(
        mid, "trust the bytes", code_resolution="completed")

    assert refused["state"] == RECOVERY_REQUIRED
    assert "corrupt or unreadable" in refused["error"]
    assert service.store.get(mid).case["code_expected_tree_digest"] == \
        case["code_expected_tree_digest"]
    service.close()


def test_raising_code_worker_after_edit_enters_recovery_without_replay(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "fix.py"
    target.write_text("broken = True\n", encoding="utf-8")
    calls = []

    def runner(_goal, **_context):
        calls.append(True)
        target.write_text("broken = False\n", encoding="utf-8")
        raise RuntimeError("worker disappeared")

    store = MissionStore(str(tmp_path / "missions.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                     reversible=True, risk="read")
    create_mission(
        store, "crashing-code", "fix without blind replay",
        case={"_isolated_workspace": str(workspace),
              "code_profile": {"verify_command": "python verify.py"}},
        leash=world_leash(may=["code"], autonomous=True,
                          workspace_mode="isolated"))
    driver = MissionDriver(
        store, actions,
        lambda *_args: {"action": "code", "args": {"goal": "fix"}}, [cap])

    assert driver.advance("crashing-code") == RECOVERY_REQUIRED
    assert calls == [True]
    assert target.read_text(encoding="utf-8") == "broken = False\n"
    receipts = actions.receipts()
    assert len(receipts) == 1 and receipts[0]["verdict"] == "failed"
    store.close()
    actions.close()


def test_structured_uncertain_code_result_enters_recovery_without_replay(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def runner(_goal, **_context):
        calls.append(True)
        return {
            "answer": "transport stopped after a write",
            "verified": False,
            "session_id": "mission-code-uncertain",
            "recovery_required": True,
            "error": "workspace outcome uncertain",
        }

    store = MissionStore(str(tmp_path / "missions.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                     reversible=True, risk="read")
    create_mission(
        store, "uncertain-code", "inspect before continuing",
        case={"_isolated_workspace": str(workspace), "code_profile": {}},
        leash=world_leash(may=["code"], autonomous=True,
                          workspace_mode="isolated"))
    driver = MissionDriver(
        store, actions,
        lambda *_args: {"action": "code", "args": {"goal": "fix"}}, [cap])

    assert driver.advance("uncertain-code") == RECOVERY_REQUIRED
    assert calls == [True]
    assert store.get("uncertain-code").case["code_recovery_required"] is True
    store.close()
    actions.close()


def test_transient_code_failure_yields_for_backoff_instead_of_hot_looping(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def runner(_goal, **_context):
        calls.append(True)
        return {
            "answer": "rate limit 429",
            "verified": False,
            "continue_needed": True,
            "transient": True,
            "retry_after_seconds": 60,
            "session_id": "mission-code-rate-limit",
            "turns": 1,
            "model_calls": 1,
        }

    store = MissionStore(str(tmp_path / "missions.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                     reversible=True, risk="read")
    create_mission(
        store, "rate-limited-code", "wait and continue",
        case={"_isolated_workspace": str(workspace), "code_profile": {}},
        leash=world_leash(may=["code"], autonomous=True,
                          workspace_mode="isolated"))
    driver = MissionDriver(
        store, actions,
        lambda *_args: {"action": "code", "args": {"goal": "fix"}}, [cap])
    before = int(time.time())

    assert driver.advance("rate-limited-code") == WAITING
    assert calls == [True]
    wait = store.next_wait("rate-limited-code")
    assert before + 60 <= wait["fire_at"] <= int(time.time()) + 60
    assert store.runtime("rate-limited-code")["retry_count"] == 1
    store.close()
    actions.close()


def test_unknown_safe_provider_error_requires_human_and_does_not_continue(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    _install_fake_harnesses(monkeypatch, [
        _result(answer="", messages=[], exhausted=False,
                error="unrecognized provider protocol failure")])

    out = _live_code(
        "fix it", str(workspace), mission_id="unknown-provider-error",
        host_verifier=lambda *_args: False)

    assert out["needs_human"] is True
    assert out["continue_needed"] is False
    assert out["recovery_required"] is False


def test_external_code_session_storage_counts_against_mission_budget(tmp_path):
    store = MissionStore(str(tmp_path / "missions.db"))
    create_mission(
        store, "storage-code", "bounded transcript",
        leash=world_leash(max_storage_bytes=1_000))
    database_bytes = store.refresh_storage("storage-code")
    store.set_external_storage("storage-code", 1_000)

    runtime = store.runtime("storage-code")
    aggregate = store.aggregate_runtime("storage-code")
    assert runtime["external_storage_bytes"] == 1_000
    assert aggregate["storage_bytes"] == database_bytes + 1_000
    assert store.budget_reason("storage-code") == \
        "mission durable-storage budget exhausted"
    store.close()
