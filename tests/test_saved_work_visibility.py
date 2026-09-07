import json

from harness import sessions, session_owner, task_inbox, webapp
from test_web_task_inbox import web, _get, _post, _call, CONFIG


def test_waiting_input_stays_discoverable_without_live_run_registry(web):
    base, token, state = web
    sessions.save("paused-task", [{"role":"user", "content":"Build the report"}], cwd=str(state.parent))
    for i in range(2):
        code, data = _post(base, token, "/api/task-inbox", {
            "session":"paused-task", "id":"next-%d"%i, "text":"Add section %d"%i, "config":CONFIG})
        assert code == 200 and data["accepted"]
    code, _ = _call(base + "/api/task-inbox/pending")
    assert code == 403
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear()
    code, data = _get(base, token, "/api/task-inbox/pending")
    assert code == 200
    row = data["sessions"][0]
    assert row["session"] == "paused-task" and row["pending"] == 2
    assert row["owner_busy"] is False and row["title"] == "Build the report"
    assert all(entry["state"] == "pending" for entry in task_inbox.list_entries("paused-task"))
    lease = session_owner.try_acquire("paused-task", label="other-process")
    try:
        assert _get(base, token, "/api/task-inbox/pending")[1]["sessions"][0]["owner_busy"] is True
    finally:
        lease.release()


def test_scheduler_failure_is_reported_and_can_clear(web, monkeypatch):
    base, token, _ = web
    monkeypatch.setattr(webapp, "_MISSION_TICK_ERROR", "Model connection unavailable")
    monkeypatch.setattr(webapp, "_MISSION_TICK_STARTED", 123.0)
    monkeypatch.setattr(webapp, "_MISSION_TICK_COMPLETED", 124.0)
    code, data = _get(base, token, "/api/missions")
    assert code == 200 and data["scheduler"]["last_error"] == "Model connection unavailable"
    assert data["scheduler"]["last_completed_at"] == 124.0
    monkeypatch.setattr(webapp, "_MISSION_TICK_ERROR", "")
    assert _get(base, token, "/api/missions")[1]["scheduler"]["last_error"] == ""
