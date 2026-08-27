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


def _raw_json(url, raw):
    request = urllib.request.Request(
        url, data=raw.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_comfy_surface_and_control_plane_are_authenticated(web_server, monkeypatch):
    from harness import comfy_integration

    base, token, _ = web_server
    expected = {
        "cloud": {"configured": False, "connected": False},
        "local": {"reachable": False, "mcp_installed": False},
        "links": {},
    }
    monkeypatch.setattr(comfy_integration, "snapshot", lambda: expected)

    with urllib.request.urlopen(base + "/comfy", timeout=8) as response:
        page = response.read().decode("utf-8")
    assert "Comfy × Collie" in page and "OFFICIAL MCP INTEGRATION" in page

    code, denied = _json(base + "/api/comfy")
    assert code == 403 and denied["error"] == "forbidden"
    code, status = _json(base + "/api/comfy?token=" + token)
    assert code == 200 and status == expected

    code, denied = _json(base + "/api/comfy/local?token=" + token,
                         method="POST", body={"confirmed": False})
    assert code == 409 and "confirmed=true" in denied["error"]
    monkeypatch.setattr(comfy_integration, "add_local_connection",
                        lambda: {"ok": True, "server": "comfy-local"})
    code, added = _json(base + "/api/comfy/local?token=" + token,
                        method="POST", body={"confirmed": True})
    assert code == 200 and added["server"] == "comfy-local"


def test_browser_extension_bridge_auth_and_cors_are_narrow(web_server, monkeypatch):
    from harness import browserbridge

    base, web_token, _ = web_server
    bridge_secret = "bridge-secret-for-test"
    extension_origin = "chrome-extension://" + ("a" * 32)
    monkeypatch.setattr(browserbridge, "token", lambda: bridge_secret)

    preflight = urllib.request.Request(
        base + "/api/browser/bridge-auth", method="OPTIONS",
        headers={"Origin": extension_origin,
                 "Access-Control-Request-Method": "GET",
                 "Access-Control-Request-Headers": "authorization"})
    with urllib.request.urlopen(preflight, timeout=8) as response:
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == extension_origin

    request = urllib.request.Request(
        base + "/api/browser/bridge-auth",
        headers={"Origin": extension_origin,
                 "Authorization": "Bearer " + bridge_secret})
    with urllib.request.urlopen(request, timeout=8) as response:
        result = json.loads(response.read())
        assert response.headers["Access-Control-Allow-Origin"] == extension_origin
    assert result["token"] == web_token
    assert result["site_access"] in ("all_except_sensitive", "ask_every_site", "all_sites")

    denied = urllib.request.Request(
        base + "/api/browser/bridge-auth",
        headers={"Origin": extension_origin, "Authorization": "Bearer wrong"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(denied, timeout=8)
    assert exc.value.code == 403

    web_page = urllib.request.Request(
        base + "/api/browser/bridge-auth", method="OPTIONS",
        headers={"Origin": "https://attacker.example"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(web_page, timeout=8)
    assert exc.value.code == 403


def test_http_json_boundary_rejects_nonstandard_numbers_and_non_objects(web_server):
    from harness import webapp

    base, token, _ = web_server
    for raw in ('{"session":"s","q":NaN}',
                '{"session":"s","q":Infinity}', '[]'):
        code, result = _raw_json(base + "/api/steer?token=" + token, raw)
        assert code == 400
        assert result["queued"] is False

    class Sink:
        _send_json = webapp.Handler._send_json

        def _send_html(self, body, code, ctype):
            self.body, self.code, self.ctype = body, code, ctype

    with pytest.raises(ValueError):
        Sink()._send_json({"invalid": float("nan")})


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


def test_pending_approvals_snapshot_is_authenticated_and_only_lists_live_items(
        web_server, tmp_path):
    from harness import webapp
    from harness.inbox import InboxStore, R_DENY

    base, token, _ = web_server
    store = InboxStore(str(tmp_path / "live-inbox.db"))
    first = store.add("session-a", tool="browser_click", title="Publish release?",
                      body="button: Publish v1.4.0", target="https://example.test/release",
                      risk="external write", rule_offer="")
    resolved = store.add("session-a", tool="browser_read", title="Read status?")
    store.resolve(resolved.id, R_DENY)
    webapp.Handler._inbox_open("session-a", store)
    try:
        code, denied = _json(base + "/api/approvals")
        assert code == 403 and denied["error"] == "forbidden"

        code, snapshot = _json(base + "/api/approvals?token=" + token)
        assert code == 200
        assert snapshot == {"approvals": [{
            "id": first.id, "session": "session-a", "tool": "browser_click",
            "body": "button: Publish v1.4.0", "title": "Publish release?",
            "target": "https://example.test/release", "risk": "external write",
            "rule_offer": "", "state": "pending",
        }]}
    finally:
        webapp.Handler._inbox_close("session-a")


def test_library_snapshot_and_lifecycle_actions_are_authenticated_and_explicit(
        web_server, monkeypatch):
    from harness.extensions import ExtensionStore

    base, token, _ = web_server
    row = {
        "id": "example.release", "name": "Release helper", "publisher": "Example",
        "description": "Reviewable release assets", "enabled": False,
        "active_version": "", "versions": [{
            "version": "1.2.0", "digest": "a" * 64, "scope_hash": "b" * 64,
            "trust_state": "unreviewed", "approved": False, "revoked": False,
            "integrity_ok": True,
        }],
        "permissions": {"network": ["api.example.test"], "host_hooks": False},
        "components": {"skills": 1, "hooks": 0, "connections": 1,
                       "templates": 0, "assets": 0},
    }
    calls = []
    monkeypatch.setattr(ExtensionStore, "list", lambda self: [row])
    monkeypatch.setattr(ExtensionStore, "enable", lambda self, ext_id, version="", approve=False:
                        calls.append(("enable", ext_id, version, approve)) or
                        dict(row, enabled=True, active_version=version or "1.2.0"))
    monkeypatch.setattr(ExtensionStore, "disable", lambda self, ext_id:
                        calls.append(("disable", ext_id)) or row)
    monkeypatch.setattr(ExtensionStore, "rollback", lambda self, ext_id, approve=False:
                        calls.append(("rollback", ext_id, approve)) or row)
    monkeypatch.setattr(ExtensionStore, "uninstall",
                        lambda self, ext_id, version="", force=False:
                        calls.append(("uninstall", ext_id, version, force)) or
                        {"id": ext_id, "removed_versions": [version or "1.2.0"]})

    code, denied = _json(base + "/api/library")
    assert code == 403 and denied["error"] == "forbidden"
    code, listing = _json(base + "/api/library?token=" + token)
    assert code == 200 and listing["extensions"] == [row]
    assert listing["builtins"] and listing["summary"]["builtins"] == len(listing["builtins"])
    assert set(listing["add_actions"]) == {
        "create_skill", "import_package", "add_connection", "record_workflow"}
    assert isinstance(listing["skills"], list)
    assert isinstance(listing["connections"], list)
    assert isinstance(listing["workflows"], list)

    code, denied = _json(base + "/api/library/action", "POST", {
        "action": "enable", "id": row["id"], "version": "1.2.0", "approve": True})
    assert code == 403 and denied["error"] == "forbidden"
    code, invalid = _json(base + "/api/library/action?token=" + token, "POST", {
        "action": "enable", "id": row["id"], "approve": "yes"})
    assert code == 400 and "approve" in invalid["error"]

    code, enabled = _json(base + "/api/library/action?token=" + token, "POST", {
        "action": "enable", "id": row["id"], "version": "1.2.0", "approve": True})
    assert code == 200 and enabled["extension"]["enabled"] is True
    code, removed = _json(base + "/api/library/action?token=" + token, "POST", {
        "action": "uninstall", "id": row["id"], "version": "1.2.0"})
    assert code == 200 and removed["extension"]["removed_versions"] == ["1.2.0"]
    assert calls == [
        ("enable", "example.release", "1.2.0", True),
        ("uninstall", "example.release", "1.2.0", False),
    ]


def test_library_can_create_a_skill_without_overwriting_existing_bytes(web_server):
    base, token, state = web_server
    payload = {
        "name": "Release Checklist",
        "description": "Use when preparing a reviewed release.",
        "instructions": "Inspect the current version, run tests, and report the exact evidence.",
    }
    code, denied = _json(base + "/api/library/skill", "POST", payload)
    assert code == 403 and denied["error"] == "forbidden"

    code, created = _json(base + "/api/library/skill?token=" + token, "POST", payload)
    assert code == 200 and created["skill"]["name"] == "release-checklist"
    path = state / "skills" / "release-checklist" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "name: release-checklist" in text
    assert "Use when preparing a reviewed release." in text
    assert "run tests" in text

    code, duplicate = _json(base + "/api/library/skill?token=" + token, "POST", payload)
    assert code == 409 and "already exists" in duplicate["error"]
    assert path.read_text(encoding="utf-8") == text


def test_library_package_import_requires_previewed_digest_and_confirmation(
        web_server, monkeypatch):
    from harness import capability_library

    base, token, _ = web_server
    digest = "d" * 64
    calls = []
    monkeypatch.setattr(capability_library, "preview_package", lambda root, source: {
        "id": "example.safe", "name": "Safe helper", "publisher": "Example",
        "version": "1.0.0", "digest": digest, "permissions": {"network": []},
        "components": {"skills": ["skills/safe/SKILL.md"]},
    })
    monkeypatch.setattr(capability_library, "install_package",
                        lambda root, source, digest, confirmed=False:
                        calls.append((source, digest, confirmed)) or {
                            "id": "example.safe", "enabled": True, "active_version": "1.0.0"})

    code, preview = _json(base + "/api/library/package/preview?token=" + token, "POST", {
        "source": "C:/reviewed/package"})
    assert code == 200 and preview["preview"]["digest"] == digest

    code, invalid = _json(base + "/api/library/package/install?token=" + token, "POST", {
        "source": "C:/reviewed/package", "digest": digest, "confirmed": "yes"})
    assert code == 400 and "confirmed" in invalid["error"]
    code, installed = _json(base + "/api/library/package/install?token=" + token, "POST", {
        "source": "C:/reviewed/package", "digest": digest, "confirmed": True})
    assert code == 200 and installed["extension"]["enabled"] is True
    assert calls == [("C:/reviewed/package", digest, True)]


def test_loopback_page_can_refresh_a_rotated_process_token(web_server):
    base, token, _ = web_server
    code, value = _json(base + "/api/session-token")
    assert code == 200 and value["token"] == token and value["boot"]


def test_vscode_embed_headers_require_the_exact_high_entropy_process_token(
        web_server, monkeypatch):
    base, _, _ = web_server
    secret = "vscode-test-" + "a" * 52
    monkeypatch.setenv("COLLIE_VSCODE_EMBED_TOKEN", secret)

    def headers(path):
        with urllib.request.urlopen(base + path, timeout=8) as response:
            response.read()
            return response.headers

    normal = headers("/")
    assert normal.get("X-Frame-Options") == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in normal.get("Content-Security-Policy", "")

    wrong = headers("/?vscode_embed=wrong")
    assert wrong.get("X-Frame-Options") == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in wrong.get("Content-Security-Policy", "")

    embedded = headers("/?vscode_embed=" + secret)
    assert embedded.get("X-Frame-Options") is None
    csp = embedded.get("Content-Security-Policy", "")
    assert "frame-ancestors vscode-webview: https://*.vscode-cdn.net" in csp
    assert "frame-ancestors 'self'" not in csp

    # The token grants no general header bypass: every non-index document remains same-origin.
    remote = headers("/remote?vscode_embed=" + secret)
    assert remote.get("X-Frame-Options") == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in remote.get("Content-Security-Policy", "")

    monkeypatch.setenv("COLLIE_VSCODE_EMBED_TOKEN", "short")
    short = headers("/?vscode_embed=short")
    assert short.get("X-Frame-Options") == "SAMEORIGIN"


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


def test_control_center_automation_and_memory_workflows_are_authenticated(web_server):
    from harness.memory import SqliteMemory

    base, token, state = web_server
    spec = {"automation_id": "daily-review", "task": "review repository",
            "trigger": {"provider": "timer", "every_s": 60, "fire_immediately": True},
            "workspace": {"mode": "isolated"}, "permissions": {}, "enabled": False}
    code, denied = _json(base + "/api/automations/upsert", "POST", {"spec": spec})
    assert code == 403 and denied["error"] == "forbidden"
    code, saved = _json(base + "/api/automations/upsert?token=" + token, "POST", {"spec": spec})
    assert code == 200 and saved["spec"]["automation_id"] == "daily-review"
    code, listing = _json(base + "/api/automations?token=" + token)
    assert code == 200 and listing["specs"][0]["task"] == "review repository"
    code, preview = _json(base + "/api/automations/preview?token=" + token, "POST",
                          {"automation_id": "daily-review"})
    assert code == 200 and preview["persisted"] is False
    code, refused = _json(base + "/api/automations/run?token=" + token, "POST",
                          {"automation_id": "daily-review"})
    assert code == 400 and "confirmed=true" in refused["error"]
    code, queued = _json(base + "/api/automations/run?token=" + token, "POST",
                         {"automation_id": "daily-review", "confirmed": True})
    assert code == 200 and queued["queued"] is True

    data = state / "data"; data.mkdir()
    memory = SqliteMemory(str(data / "memory.db"), embedder=None)
    claim_id = memory.propose("private proposed claim", project="demo", evidence="receipt")
    memory.close()
    code, denied = _json(base + "/api/memory/claims")
    assert code == 403
    code, claims = _json(base + "/api/memory/claims?status=proposed&token=" + token)
    assert code == 200 and claims["claims"][0]["id"] == claim_id
    code, refused = _json(base + "/api/memory/review?token=" + token, "POST",
                          {"memory_id": claim_id, "action": "attest"})
    assert code == 400 and "confirmed=true" in refused["error"]
    code, reviewed = _json(base + "/api/memory/review?token=" + token, "POST", {
        "memory_id": claim_id, "action": "attest", "note": "checked", "confirmed": True})
    assert code == 200 and reviewed["claim"]["status"] == "attested"


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
                  "/api/mission/specialist/cancel", "Control Center", "/api/doctor",
                  "/api/recovery-center", "/api/automations", "/api/memory/claims",
                  "/api/budgets", "/api/security"):
        assert value in page
    assert "confirmed: true" in page and "PRIVATE" not in page
    assert "Auto — Collie chooses per task" in page
    assert "entry.auto ? { auto: true }" in page
