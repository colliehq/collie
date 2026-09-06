from types import SimpleNamespace

from harness import sessions
from harness.mission_delivery import read_report
from test_web_task_inbox import web, _get


def prepared(tmp_path, monkeypatch):
    directory = tmp_path / "mission-code-sessions"
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    text = ("Complete implementation detail.\n" * 200) + "FINAL-DETAIL-PRESERVED"
    sessions.save("code-report", [{"role":"assistant", "content":text}], answer=text)
    sessions.append_run_receipt("code-report", {"kind":"mission_code_slice",
        "mission_id":"mission-report", "session_id":"code-report", "transcript_persisted":True})
    mission = SimpleNamespace(mission_id="mission-report", state="done_verified", case={
        "code_session_id":"code-report", "code_delivery":{"answer":text[:4000]}})
    return mission, text


def test_full_delivery_outlives_the_bounded_mission_preview(tmp_path, monkeypatch):
    mission, text = prepared(tmp_path, monkeypatch)
    result = read_report(mission, str(tmp_path))
    assert result["answer"] == text and result["complete"]
    assert result["source"] == "session_transcript"


def test_missing_transcript_keeps_preview_with_an_explicit_notice(tmp_path, monkeypatch):
    mission, text = prepared(tmp_path, monkeypatch)
    mission.case["code_session_id"] = "missing-report"
    result = read_report(mission, str(tmp_path))
    assert result["answer"] == text[:4000] and not result["complete"]
    assert "missing" in result["notice"]


def test_another_missions_transcript_is_never_served(tmp_path, monkeypatch):
    mission, text = prepared(tmp_path, monkeypatch)
    mission.mission_id = "other-mission"
    result = read_report(mission, str(tmp_path))
    assert result.get("error") and "answer" not in result


def test_http_delivery_reads_the_bound_report_without_starting_a_model(web, monkeypatch):
    from harness import missionweb
    base, token, state = web
    mission, text = prepared(state, monkeypatch)
    service = SimpleNamespace(store=SimpleNamespace(get=lambda mid: mission if mid==mission.mission_id else None),
                              _state_dir=str(state), close=lambda:None)
    monkeypatch.setattr(missionweb, "MissionService", lambda:service)
    code, report = _get(base, token, "/api/mission/delivery?id=mission-report")
    assert code == 200 and report["answer"] == text
    code, missing = _get(base, token, "/api/mission/delivery?id=unknown")
    assert code == 404
