"""Read a Mission's full coding report from its bound durable session."""
from __future__ import annotations

import os

from . import sessions


def read_report(mission, state_dir):
    if mission is None:
        return {"error": "No such Mission"}
    case = mission.case or {}
    delivery = case.get("code_delivery") or {}
    if not isinstance(delivery, dict):
        return {"error": "This Mission has no coding report"}
    profile = case.get("code_profile") or {}
    sid = str(case.get("code_session_id") or profile.get("session_id") or "")
    result = {"mission_id":mission.mission_id, "session_id":sid,
              "state":mission.state, "answer":str(delivery.get("answer") or ""),
              "source":"mission_preview", "complete":False,
              "verification":case.get("code_verification") or {}, "notice":""}
    if not sid:
        result["notice"] = "This Mission has no bound coding session; only its saved preview is available."
        return result
    checked = sessions.load_checked(sid, directory=os.path.join(state_dir, "mission-code-sessions"))
    if checked.get("status") != "ok":
        result["notice"] = "The coding transcript is missing or unreadable; only the saved preview is available."
        return result
    journal = checked["session"]
    receipts = [row for row in journal.get("run_receipts", [])
                if row.get("kind") == "mission_code_slice"]
    # Do not follow a corrupt case's pointer into a different Mission's work.
    if not receipts or any(row.get("mission_id") != mission.mission_id or
                            row.get("session_id") != sid for row in receipts):
        return {"error": "The coding transcript is not bound to this Mission"}
    answer = journal.get("last_answer")
    if not isinstance(answer, str) or not answer:
        result["notice"] = "The coding transcript has no completed report yet; showing the saved preview."
        return result
    result.update(answer=answer, source="session_transcript", complete=True)
    return result
