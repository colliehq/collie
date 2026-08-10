"""Authenticated Web operations/control-plane contracts."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def web_server(monkeypatch, tmp_path):
    from harness import webapp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN, state
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def _json(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_activity_health_and_hooks_are_authenticated_and_content_safe(
        web_server, monkeypatch):
    from harness import controlplane

    base, token, state = web_server
    secret = "PRIVATE-PROMPT-CONTENT"
    monkeypatch.setattr(controlplane, "activity", lambda *_args, **_kwargs: {
        "at": 1, "sessions": [{"session_id": "s1", "state": "external_action",
                                "recovery_required": True, "detail": {"args": secret}}],
        "missions": [{"mission_id": "m1", "state": "running", "goal": secret,
                      "result": secret, "lane": "mission"}],
        "task_runs": [{"run_id": "r1", "role": "reader", "status": "running",
                       "task": secret, "result": secret, "leash": {"secret": secret}}],
        "automations": [{"execution_id": "e1", "automation_id": "a1", "state": "pending",
                         "request_json": secret, "result_json": secret}],
        "notifications": [{"notification_id": 1, "run_id": "r1", "kind": "progress",
                           "state": "queued", "payload": {"text": secret}}],
        "errors": {"missions": secret}})
    monkeypatch.setattr(controlplane, "health", lambda *_args, **_kwargs: {
        "ok": True, "status": "ok", "at": 1,
        "workers": {"web": {"state": "running", "fresh": True, "detail": {"task": secret}}},
        "heartbeats": {"worker:web": {"state": "running", "fresh": True,
                                        "detail": {"prompt": secret}}},
        "services": {"web": {"ok": True, "detail": secret}},
        "credentials": [{"name": "codex-oauth", "state": "ok", "token": secret}],
        "queues": {"notifications": {"pending": 1, "payload": secret}},
        "supervisor": {"installed": False},
        "work": {"interactive_active": 1, "missions_active": 1, "task_runs_active": 1,
        "automations_active": 1, "recovery_required": []},
        "activity_errors": {"task_runs": secret}})

    for path in ("/api/activity", "/api/healthz", "/api/hooks"):
        code, _ = _json(base + path)
        assert code == 403
    code, activity = _json(base + "/api/activity?token=" + token)
    assert code == 200 and activity["task_runs"][0]["role"] == "reader"
    assert secret not in json.dumps(activity)
    code, health = _json(base + "/api/healthz?token=" + token)
    assert code == 200 and health["workers"]["web"]["fresh"] is True
    assert secret not in json.dumps(health)

    # Hook status is inspect-only. Unreviewed exact bytes stay pending.
    hooks = state / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
    code, status = _json(base + "/api/hooks?token=" + token)
    assert code == 200 and status["trust_changes_allowed"] is False
    assert status["pending"] and status["pending"][0]["sha256"]


def test_recovery_list_detail_and_explicit_reconcile(web_server):
    from harness import sessions

    base, token, state = web_server
    sessions.checkpoint("uncertain", [{"role": "user", "content": "private action"}],
                        run_id="run-1", state="external_action",
                        detail={"tool_name": "publish", "tool_call_id": "call-1",
                                "args": {"secret": "not-on-wire"}})
    code, _ = _json(base + "/api/recovery")
    assert code == 403
    code, listing = _json(base + "/api/recovery?token=" + token)
    assert code == 200 and listing["sessions"][0]["session_id"] == "uncertain"
    assert "not-on-wire" not in json.dumps(listing)
    code, detail = _json(base + "/api/recovery/uncertain?token=" + token)
    assert code == 200 and detail["recovery_required"] is True

    code, refused = _json(base + "/api/recovery/reconcile?token=" + token, "POST", {
        "session": "uncertain", "resolution": "not_fired"})
    assert code == 400 and "confirmed" in refused["error"]
    code, _ = _json(base + "/api/recovery/reconcile", "POST", {
        "session": "uncertain", "resolution": "not_fired", "confirmed": True})
    assert code == 403
    code, reconciled = _json(base + "/api/recovery/reconcile?token=" + token, "POST", {
        "session": "uncertain", "resolution": "not_fired", "confirmed": True})
    assert code == 200 and reconciled["state"]["auto_resumable"] is True
    assert sessions.recovery_state("uncertain")["state"] == "turn_boundary"


def test_authenticated_automation_webhook_only_persists_allowlisted_fields(web_server):
    from harness.automations import AutomationStore

    base, token, state = web_server
    with AutomationStore(str(state / "automations.db")) as store:
        store.upsert({
            "automation_id": "deploy-hook", "task": "check deployment",
            "trigger": {"provider": "webhook", "persist_fields": ["event", "project"]},
            "workspace": {"mode": "isolated"},
            "permissions": {"webhook_ingest": True},
        })
    payload = {"automation_id": "deploy-hook", "delivery_id": "delivery-1",
               "payload": {"event": "deploy", "project": "collie", "secret": "DROP-ME"}}
    code, _ = _json(base + "/api/automation/webhook", "POST", payload)
    assert code == 403
    code, accepted = _json(base + "/api/automation/webhook?token=" + token, "POST", payload)
    assert code == 200 and accepted["accepted"] is True
    with AutomationStore(str(state / "automations.db")) as store:
        persisted = store.executions()[0]["request_json"]
    assert "collie" in persisted and "DROP-ME" not in persisted


def test_specialist_tree_inspect_steer_cancel_and_no_task_leak(web_server, tmp_path):
    from harness.missionweb import MissionService

    base, token, state = web_server
    repo = tmp_path / "repo"; repo.mkdir()
    svc = MissionService(state_dir=str(state), decider=lambda *_: {}, stub=True)
    mission = svc.start("PRIVATE-MISSION-GOAL", may=["research"])
    root = svc.create_run_tree(mission["mission_id"], [
        {"kind": "file", "id": str(repo), "mode": "write"}], workspace=str(repo))["root"]
    child = svc.spawn_specialist(mission["mission_id"], "reader", "PRIVATE-SPECIALIST-TASK",
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))
    svc.close()

    code, _ = _json(base + "/api/mission/run-tree?id=" + mission["mission_id"])
    assert code == 403
    code, tree = _json(base + "/api/mission/run-tree?id=" + mission["mission_id"] + "&token=" + token)
    assert code == 200 and tree["tree"]["root"]["run_id"] == root["run_id"]
    code, specialist = _json(base + "/api/mission/specialist?run_id=" + child["run_id"] + "&token=" + token)
    assert code == 200 and specialist["run"]["role"] == "reader"
    assert "PRIVATE-" not in json.dumps(tree) + json.dumps(specialist)

    code, denied = _json(base + "/api/mission/specialist/steer", "POST", {
        "run_id": child["run_id"], "text": "focus"})
    assert code == 403
    code, steered = _json(base + "/api/mission/specialist/steer?token=" + token, "POST", {
        "run_id": child["run_id"], "text": "focus"})
    assert code == 200 and steered["queued"] is True
    code, cancelled = _json(base + "/api/mission/specialist/cancel?token=" + token, "POST", {
        "run_id": child["run_id"]})
    assert code == 200 and cancelled["run"]["status"] in ("cancel_requested", "cancelled")


def test_model_picker_auto_unpins_model_without_switching_provider(web_server, monkeypatch, tmp_path):
    from harness import settings

    base, token, _ = web_server
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_PATH", str(settings_path))
    monkeypatch.setattr(settings, "_cache", {"mtime": None, "data": {}})
    monkeypatch.setenv("COLLIE_PROVIDER", "codex-oauth")
    monkeypatch.delenv("COLLIE_MODEL", raising=False)
    settings.update({"PROVIDER": "codex-oauth", "MODEL": "gpt-5.6-sol"})

    code, result = _json(base + "/api/model?token=" + token, "POST", {"auto": True})
    assert code == 200 and result == {
        "ok": True, "provider": "codex-oauth", "model": "", "auto": True}
    saved = json.loads(settings_path.read_text("utf-8"))
    assert saved["PROVIDER"] == "codex-oauth" and saved.get("MODEL", "") == ""


def test_activity_ui_and_auto_model_contracts():
    page = (Path(__file__).parents[1] / "harness" / "webui" / "index.html").read_text("utf-8")
    for value in ("activityPanel", "/api/activity", "/api/healthz", "/api/hooks",
                  "/api/recovery/reconcile", "/api/mission/specialist/steer",
                  "/api/mission/specialist/cancel"):
        assert value in page
    assert "confirmed: true" in page and "PRIVATE" not in page
    assert "Auto — Collie chooses per task" in page
    assert "entry.auto ? { auto: true }" in page
