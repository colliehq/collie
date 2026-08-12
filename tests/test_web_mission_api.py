"""HTTP regression for the explicit Mission control surface."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from harness import settings, webapp
from harness.mission import MissionStore


def _allow_claude_subscription(provider, *, account_evidence=None, environ=None,
                               model="", require_direct_probe=True):
    if provider != "claude-agent-sdk" or account_evidence is not None:
        raise RuntimeError("unreviewed subscription route")
    assert isinstance(environ, dict)
    return {
        "format": "collie-subscription-guard-v1",
        "schema_version": 1,
        "provider": provider,
        "verdict": "allow",
    }


def _request(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_mission_api_is_authed_persistent_and_manageable(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    # Creation and management are model-free. The provider is intentionally absent
    # to prove these endpoints do not initialize one just to read/update state.
    monkeypatch.delenv("COLLIE_PROVIDER", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    token = "?token=" + webapp.TOKEN
    try:
        code, _ = _request(root + "/api/missions")
        assert code == 403

        code, created = _request(
            root + "/api/mission" + token, "POST", {"goal": "watch replies"})
        assert code == 201 and created["state"] == "queued"
        mid = created["mission_id"]

        code, bad_mode = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "do not coerce strings into authority", "autonomous": "false"})
        assert code == 400 and "boolean" in bad_mode["error"]

        code, status = _request(root + "/api/mission?id=" + mid + "&token=" + webapp.TOKEN)
        assert code == 200 and status["mission_id"] == mid
        assert set(status["controls"]) == {"run", "pause", "cancel"}
        assert status["report"]["format_version"] == 1

        code, _ = _request(root + "/api/mission/report?id=" + mid)
        assert code == 403
        code, report = _request(
            root + "/api/mission/report?id=" + mid + "&token=" + webapp.TOKEN)
        assert code == 200 and report["mission_id"] == mid
        assert "case" not in report and "markdown" in report

        code, paused = _request(
            root + "/api/mission/pause" + token, "POST", {"id": mid})
        assert code == 200 and paused["state"] == "paused"
        code, resumed = _request(
            root + "/api/mission/resume" + token, "POST", {"id": mid})
        assert code == 200 and resumed["state"] == "queued"
        code, cancelled = _request(
            root + "/api/mission/cancel" + token, "POST", {"id": mid})
        assert code == 200 and cancelled["state"] == "cancelled"

        code, listed = _request(root + "/api/missions" + token)
        assert code == 200 and listed["missions"][0]["mission_id"] == mid

        code, uncertain = _request(
            root + "/api/mission" + token, "POST", {"goal": "uncertain external action"})
        umid = uncertain["mission_id"]
        store = MissionStore(str(tmp_path / "jobs.db"))
        assert store.claim_run(umid, lease_s=-1)
        store.close()
        code, ticked = _request(root + "/api/mission/tick" + token, "POST", {})
        assert code == 200 and ticked["recovered"] == 1
        code, recovery = _request(
            root + "/api/mission?id=" + umid + "&token=" + webapp.TOKEN)
        assert recovery["state"] == "recovery_required"
        assert set(recovery["controls"]) == {"reconcile", "cancel"}
        code, refused = _request(
            root + "/api/mission/continue" + token, "POST", {"id": umid})
        assert code == 409 and refused["state"] == "recovery_required"
        code, reconciled = _request(
            root + "/api/mission/reconcile" + token, "POST",
            {"id": umid, "note": "inspected target and receipts"})
        assert code == 200 and reconciled["state"] == "queued"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_web_ui_keeps_mission_out_of_the_model_router():
    html = open(webapp.INDEX_HTML, encoding="utf-8").read()
    send_pos = html.index("function send()")
    command_pos = html.index("handleMissionCommand(q)", send_pos)
    steer_pos = html.index("if (running)", send_pos)
    assert command_pos < steer_pos, "mission control must be intercepted before steering"
    assert 'd.kind === "mission"' not in html
    assert 'missionPost("/api/mission/cancel"' in html
    assert 'missionPost("/api/mission/pause"' in html
    assert 'missionPost("/api/mission/reconcile"' in html
    assert 't("Progress report")' in html
    assert 'missionCopyReport(report, copyReport)' in html
    assert 'missionDownloadReport(report)' in html
    assert 'if (action === "report") { showMission(mid, true); return true; }' in html
    handler_pos = html.index("function handleMissionCommand")
    malformed_guard = html.index('if (/^start$/i.test(raw))', handler_pos)
    start_call = html.index("_startMissionCard(goal, autonomous, bounds)", malformed_guard)
    assert malformed_guard < start_call
    assert 'if (/^(?:list|ls|help)\\s+/i.test(raw))' in html


def test_mission_api_validates_and_atomically_binds_overnight_code_profile(
        monkeypatch, tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_PROVIDER", "claude-agent-sdk")
    monkeypatch.setenv("COLLIE_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(
        settings, "_HARD_ENV", settings._HARD_ENV | {"COLLIE_PROVIDER", "COLLIE_MODEL"})
    monkeypatch.setattr(
        "harness.subscription_guard.check_subscription_guard",
        _allow_claude_subscription)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    token = "?token=" + webapp.TOKEN
    try:
        code, invalid = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "must not persist", "code": True, "overnight": True,
             "workspace": str(tmp_path / "missing"), "no_paid_overage": True})
        assert code == 400 and "does not exist" in invalid["error"]
        code, listed = _request(root + "/api/missions" + token)
        assert code == 200 and listed["missions"] == []

        code, created = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "make the suite green", "code": True, "overnight": True,
             "workspace": str(repo), "verify_command": "python -m pytest -q",
             "provider": "claude-agent-sdk", "model": "claude-opus-4-8",
             "no_paid_overage": True})
        assert code == 201 and created["state"] == "queued"
        assert created["case"]["_isolated_workspace"] == str(repo.resolve())
        assert created["case"]["execution_profile"]["provider"] == "claude-agent-sdk"
        assert created["case"]["execution_profile"]["model"] == "claude-opus-4-8"
        assert created["case"]["execution_profile"]["subscription_only"] is True

        code, invalid_type = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "reject coercion", "code": "true"})
        assert code == 400 and "boolean" in invalid_type["error"]
        code, invalid_path_type = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "reject empty collection coercion", "code": True,
             "workspace": []})
        assert code == 400 and "workspace must be a string" in invalid_path_type["error"]
        code, invalid_provider_type = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "reject provider coercion", "provider": []})
        assert code == 400 and "provider must be a string" in invalid_provider_type["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_mission_api_refuses_metered_overnight_fallback(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLLIE_PROVIDER", "anthropic")
    monkeypatch.setenv("COLLIE_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(
        settings, "_HARD_ENV", settings._HARD_ENV | {"COLLIE_PROVIDER", "COLLIE_MODEL"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    token = "?token=" + webapp.TOKEN
    try:
        code, refused = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "never charge API usage", "code": True, "overnight": True,
             "workspace": str(repo), "no_paid_overage": True})
        assert code == 400 and "official Claude Agent SDK" in refused["error"]
        code, listed = _request(root + "/api/missions" + token)
        assert code == 200 and listed["missions"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
