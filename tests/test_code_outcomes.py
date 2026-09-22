"""A coding slice's answer, verification and continuation are separate facts."""
from harness import primitives
from harness.mission import MissionStore
from harness.verifier import FAILED, INCONCLUSIVE, VERIFIED


def test_live_answer_with_patch_is_unverified_work_not_missing_result():
    verdict = primitives._code_verify(None, {
        "answer": "Updated the implementation.", "verified": False,
        "slice_mutated": True, "patch_attributed": True,
        "verification": {"detail": "no host verification command configured"},
    })
    assert verdict.status == INCONCLUSIVE
    assert "patch was produced" in verdict.reason
    assert "no host verification command" in verdict.reason


def test_workspace_configuration_problem_keeps_its_actual_explanation(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLIE_MISSION_CODE_ROOTS", raising=False)
    result = primitives._live_code("inspect", str(tmp_path))
    verdict = primitives._code_verify(None, result)
    assert verdict.status == INCONCLUSIVE
    assert verdict.reason == result["answer"]
    assert "COLLIE_MISSION_CODE_ROOTS" in verdict.reason


def test_error_receipt_keeps_the_error_instead_of_saying_no_result():
    verdict = primitives._code_verify(None, {
        "answer": "partial work", "error": "required host check timed out", "verified": False})
    assert verdict.status == FAILED and verdict.reason == "required host check timed out"


def test_durable_slice_yield_is_in_progress_in_activity(tmp_path):
    store = MissionStore(str(tmp_path / "mission.db"))
    try:
        result = {"answer": "partial work", "verified": False,
                  "continue_needed": True, "session_id": "slice-1"}
        verdict = primitives._code_verify(None, result)
        assert verdict.status == VERIFIED  # checkpoint accepted, task not yet complete
        store.record_event("mission-1", "proposed", "code", "p", {
            "args": {"goal": "finish the implementation"}})
        store.record_event("mission-1", "result", "code", "r", {
            "verdict": verdict.status, "reason": verdict.reason, "result": result})
        entries = store.activity_ledger("mission-1")
        assert entries[-1]["status"] == "in_progress"
    finally:
        store.close()
