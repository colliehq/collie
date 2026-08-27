from harness.jobs import DONE_VERIFIED, NEEDS_YOU
from harness.mission import Mission, completion_contract
from harness.missionweb import _mission_summary


def test_verified_completion_requires_persisted_positive_evidence():
    mission = Mission("m1", "ship it", state=DONE_VERIFIED, result="done")
    without = completion_contract(mission).to_dict()
    with_evidence = completion_contract(mission, [{
        "kind": "goal_verification",
        "payload": {"evidence": [{"channel": "pytest", "at": 1.0,
                                    "ok": True, "asserted": False,
                                    "detail": "all checks passed"}]},
    }]).to_dict()

    assert tuple(without) == ("status", "summary", "evidence", "artifacts",
                              "next_action")
    assert without["status"] == "accepted"
    assert with_evidence["status"] == "verified"
    assert with_evidence["evidence"][0]["channel"] == "pytest"


def test_mission_summary_projects_real_artifacts_not_activity_labels():
    mission = Mission("m2", "build", state=NEEDS_YOU, result="review",
                      case={"artifact_refs": [{"path": "dist/report.html"}],
                            "artifacts": ["dist/app.zip"]})
    summary = _mission_summary(
        mission, [], [], {}, None, None,
        activity=[{"status": "completed", "summary": "Ran unit tests"}],
        events=[])

    assert summary["completion"]["artifacts"] == [
        "dist/report.html", "dist/app.zip"]
    assert "Ran unit tests" not in summary["completion"]["artifacts"]
