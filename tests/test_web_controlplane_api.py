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
    assert 'id="refreshTools"' in page and 'api("/api/comfy/refresh"' in page

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

    code, denied = _json(base + "/api/comfy/refresh", method="POST", body={})
    assert code == 403 and denied["error"] == "forbidden"
    monkeypatch.setattr(comfy_integration, "refresh_connections", lambda: {
        "ok": True, "refreshed": [{"server": "comfy-cloud", "tools": 41}],
        "errors": [], "status": expected,
    })
    code, refreshed = _json(base + "/api/comfy/refresh?token=" + token,
                            method="POST", body={})
    assert code == 200 and refreshed["refreshed"][0]["tools"] == 41

    monkeypatch.setattr(
        comfy_integration, "refresh_connections",
        lambda: (_ for _ in ()).throw(RuntimeError("refresh failed")))
    code, failed = _json(base + "/api/comfy/refresh?token=" + token,
                         method="POST", body={})
    assert code == 409 and failed["error"] == "refresh failed"


def test_live_copilot_surface_and_control_plane_require_audio_consent(web_server, monkeypatch):
    from harness import avatar_rehearsal

    base, token, _state = web_server
    monkeypatch.setattr(avatar_rehearsal.mcpclient, "server_has_tool",
                        lambda _server, _tool: False)

    with urllib.request.urlopen(base + "/live", timeout=8) as response:
        page = response.read().decode("utf-8")
    assert "Live Copilot" in page and 'id="handoff"' in page
    with urllib.request.urlopen(base + "/live-capsule", timeout=8) as response:
        capsule = response.read().decode("utf-8")
    assert "COLLIE · LIVE CAPSULE" in capsule and 'name="collie-token"' in capsule

    code, denied = _json(base + "/api/live-copilot")
    assert code == 403 and denied["error"] == "forbidden"
    code, idle = _json(base + "/api/live-copilot?token=" + token)
    assert code == 200 and idle["active"] is False

    code, refused = _json(base + "/api/live-copilot/start?token=" + token, "POST", {
        "consent": False, "listen": True, "board_edit": True})
    assert code == 409 and "consent" in refused["error"]
    code, started = _json(base + "/api/live-copilot/start?token=" + token, "POST", {
        "consent": True, "listen": True, "understand": True, "observe_apps": True,
        "board_edit": True, "context": ""})
    assert code == 201 and started["active"] and started["board_edit"]
    assert started["context"] == "" and started["observe_apps"]

    code, event = _json(base + "/api/live-copilot/event?token=" + token, "POST", {
        "source": "other", "text": "Can you take the next task?"})
    assert code == 201 and event["source"] == "other"
    code, handoff = _json(base + "/api/live-copilot/handoff?token=" + token, "POST", {
        "app": "Chrome", "title": "System design board", "pid": 42, "hwnd": 9001})
    assert code == 201 and handoff["pending"] is True
    assert handoff["app"] == "chrome" and handoff["title"] == "System design board"
    assert handoff["pid"] == 42 and handoff["hwnd"] == 9001

    code, avatar = _json(base + "/api/live-copilot/avatar/start?token=" + token,
                         "POST", {"scenario": "Explain the architecture"})
    assert code == 201 and avatar["simulation"] is True
    assert avatar["avatar"]["active"] is True
    code, avatar_stop = _json(base + "/api/live-copilot/avatar/stop?token=" + token,
                              "POST", {})
    assert code == 200 and avatar_stop["avatar"]["active"] is False

    code, stopped = _json(base + "/api/live-copilot/stop?token=" + token, "POST", {})
    assert code == 200 and not stopped["active"]
    assert not stopped["listen"] and not stopped["board_edit"]


@pytest.mark.parametrize("body", [
    {"listen": "false"}, {"observe_screen": 1}, {"understand": "false"},
    {"observe_ui": None}, {"share_transcript": "true"},
])
def test_live_start_rejects_non_boolean_authority(web_server, monkeypatch, body):
    from harness import live_copilot

    monkeypatch.setattr(live_copilot, "capabilities", lambda: {})
    base, token, state = web_server
    code, refused = _json(base + "/api/live-copilot/start?token=" + token, "POST", body)
    assert code == 409 and "boolean" in refused["error"]
    assert live_copilot.LiveSessionStore(state).snapshot()["active"] is False


def test_live_permissions_cannot_enable_listening_with_a_string(web_server, monkeypatch):
    from harness import live_copilot

    monkeypatch.setattr(live_copilot, "capabilities", lambda: {})
    base, token, state = web_server
    code, _ = _json(base + "/api/live-copilot/start?token=" + token, "POST", {"listen": False})
    assert code == 201
    code, refused = _json(base + "/api/live-copilot/permissions?token=" + token,
                         "POST", {"listen": "false"})
    assert code == 409 and "boolean" in refused["error"]
    current = live_copilot.LiveSessionStore(state).snapshot()
    assert current["listen"] is False and current["consent_at_ms"] == 0


def test_live_review_download_is_authenticated_and_session_bound(web_server, monkeypatch):
    from harness import live_copilot

    monkeypatch.setattr(live_copilot, "capabilities", lambda: {})
    base, token, state = web_server
    store = live_copilot.LiveSessionStore(state)
    session = store.start(listen=False)["session_id"]
    store.add_note(text="Prepare the launch notes.")
    store.add_event(source="you", text="Private transcript")
    store.stop()
    url = base + "/api/live-copilot/export?session=" + session
    assert _json(url)[0] == 403
    assert _json(url + "&token=" + token + "&events=false")[0] == 400
    with urllib.request.urlopen(url + "&token=" + token, timeout=8) as response:
        assert response.headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert response.headers["Content-Disposition"] == 'attachment; filename="%s.md"' % session
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        content = response.read().decode("utf-8")
    assert "Prepare the launch notes." in content and "Private transcript" not in content
    with urllib.request.urlopen(url + "&token=" + token + "&events=1&lang=zh", timeout=8) as response:
        content = response.read().decode("utf-8")
    assert "会话回顾" in content and "Private transcript" in content
    store.start(listen=False)
    assert _json(url + "&token=" + token)[0] == 409


def test_mcp_login_thread_warms_cache_and_publishes_failure(web_server, monkeypatch):
    from harness import mcpclient, webapp

    base, token, _ = web_server
    config = {"srv": {"url": "https://example.test/mcp"}}
    monkeypatch.setattr(mcpclient, "_load_config", lambda: config)
    webapp._MCP_LOGIN_BUSY.clear()
    webapp._MCP_LOGIN_ERR.clear()

    completed = threading.Event()
    calls = []

    monkeypatch.setattr(mcpclient, "login", lambda name, cfg: calls.append(("login", name, cfg)))

    def refresh(name):
        calls.append(("refresh", name))
        completed.set()
        return [{"name": "read"}]

    monkeypatch.setattr(mcpclient, "refresh_server", refresh)
    code, started = _json(
        base + "/api/mcp?token=" + token, method="POST",
        body={"action": "login", "name": "srv"})
    assert code == 200 and started == {"ok": True, "started": True}
    assert completed.wait(3)
    assert calls == [("login", "srv", config["srv"]), ("refresh", "srv")]
    assert "srv" not in webapp._MCP_LOGIN_BUSY
    assert "srv" not in webapp._MCP_LOGIN_ERR

    failed = threading.Event()

    def reject(_name, _cfg):
        failed.set()
        raise RuntimeError("oauth secret must-not-leak")

    monkeypatch.setattr(mcpclient, "login", reject)
    code, started = _json(
        base + "/api/mcp?token=" + token, method="POST",
        body={"action": "login", "name": "srv"})
    assert code == 200 and started["started"] is True
    assert failed.wait(3)
    for _ in range(100):
        if "srv" not in webapp._MCP_LOGIN_BUSY:
            break
        threading.Event().wait(.01)
    assert "srv" not in webapp._MCP_LOGIN_BUSY
    assert "RuntimeError" in webapp._MCP_LOGIN_ERR["srv"]
    webapp._MCP_LOGIN_ERR.clear()


def test_mcp_recommendation_is_authenticated_local_first_and_consent_bounded(
        web_server, monkeypatch):
    from harness import mcp_discovery, settings

    base, token, _ = web_server
    calls = []

    def recommend(goal, include_registry=False, refresh=False, max_results=5):
        calls.append((goal, include_registry, refresh, max_results))
        return {"recognized": True, "recommendations": [], "raw_goal_shared": False,
                "registry_searched": include_registry}

    monkeypatch.setattr(mcp_discovery, "recommend", recommend)
    monkeypatch.setattr(settings, "all_values", lambda: {"MCP_DISCOVERY": "off"})
    updates = []
    monkeypatch.setattr(settings, "update", lambda values: updates.append(values) or values)
    monkeypatch.setattr(settings, "apply", lambda: None)

    payload = {"goal": "Schedule ACME secret calendar meetings"}
    code, denied = _json(base + "/api/mcp/recommend", "POST", payload)
    assert code == 403 and denied["error"] == "forbidden"

    code, local = _json(base + "/api/mcp/recommend?token=" + token, "POST", payload)
    assert code == 200 and local["raw_goal_shared"] is False
    assert calls == [(payload["goal"], False, False, 5)]

    public_payload = dict(payload, search_registry=True)
    code, consent = _json(base + "/api/mcp/recommend?token=" + token, "POST", public_payload)
    assert code == 409 and consent["consent_required"] is True
    assert consent["raw_goal_shared"] is False and "calendar" in consent["generic_terms"]
    assert "ACME" not in json.dumps(consent)

    public_payload["confirm_public_search"] = True
    code, public = _json(base + "/api/mcp/recommend?token=" + token, "POST", public_payload)
    assert code == 200 and public["registry_searched"] is True
    assert updates == [{"MCP_DISCOVERY": "on"}]
    assert calls[-1] == (payload["goal"], True, False, 5)


def test_mcp_registry_candidate_requires_exact_confirmation_and_connects_in_background(
        web_server, monkeypatch):
    from harness import mcpclient, webapp

    base, token, _ = web_server
    candidate = {"id": "registry:io.example/calendar@1.0.0", "label": "Calendar",
                 "trust_level": "community_unreviewed"}
    cfg = {"url": "https://mcp.example.test/mcp"}
    completed = threading.Event()
    calls = []
    monkeypatch.setattr(mcpclient, "prepare_registry_candidate",
                        lambda cid: (candidate, "registry-calendar-abc", cfg))

    def connect(cid):
        calls.append(cid); completed.set(); return candidate, "registry-calendar-abc", cfg, []

    monkeypatch.setattr(mcpclient, "connect_registry_candidate", connect)
    webapp._MCP_LOGIN_BUSY.clear(); webapp._MCP_LOGIN_ERR.clear()

    body = {"action": "connect_candidate", "candidate_id": candidate["id"]}
    code, refused = _json(base + "/api/mcp?token=" + token, "POST", body)
    assert code == 400 and "confirmed=true" in refused["error"]
    assert calls == []

    body["confirmed"] = True
    code, started = _json(base + "/api/mcp?token=" + token, "POST", body)
    assert code == 200 and started["started"] is True
    assert started["candidate"]["trust_level"] == "community_unreviewed"
    assert completed.wait(3) and calls == [candidate["id"]]


def test_nowplaying_poll_is_cheap_unless_system_media_is_requested(web_server, monkeypatch):
    from harness import desktop

    base, _, _ = web_server
    system_calls = []
    monkeypatch.setattr(desktop, "playing_here", lambda: {"track": {
        "title": "Local track", "uploader": "Collie", "duration": 42,
    }})
    monkeypatch.setattr(
        desktop, "nowplaying",
        lambda: system_calls.append(True) or {"title": "System track"})

    code, cheap = _json(base + "/api/desktop/nowplaying")
    assert code == 200 and cheap["track"] is None
    assert cheap["collie"]["title"] == "Local track" and cheap["collie"]["stoppable"]
    assert system_calls == []

    code, full = _json(base + "/api/desktop/nowplaying?system=1")
    assert code == 200 and full["track"] == {"title": "System track"}
    assert system_calls == [True]


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


def test_ide_context_handoff_is_authenticated_bounded_and_one_shot(web_server):
    from harness import webapp

    base, token, _ = web_server
    body = {"items": [{"kind": "selection", "path": "src/app.ts", "startLine": 7,
                       "endLine": 9, "content": "const value = 1;"}]}
    code, denied = _json(base + "/api/ide/context", "POST", body)
    assert code == 403 and denied["error"] == "forbidden"
    code, saved = _json(base + "/api/ide/context?token=" + token, "POST", body)
    assert code == 200 and len(saved["id"]) == 16
    assert webapp.Handler._ide_context_take(saved["id"]) == body["items"]
    assert webapp.Handler._ide_context_take(saved["id"]) is None

    # Over the limit is refused, not quietly shortened.  Truncating here handed
    # the model a file that stopped mid-function while the editor showed the
    # whole thing, and nobody was told which 6000 characters went missing.
    code, refused = _json(base + "/api/ide/context?token=" + token, "POST", {
        "items": [{"path": "too-large.ts", "content": "x" * 70_000}]})
    assert code == 413
    assert "nothing was truncated" in refused["error"] and "id" not in refused

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
                                        "detail": {"prompt": secret}},
                       "supervisor": {"state": "running", "fresh": True,
                                      "detail": {"prompt": secret}}},
        "services": {"web": {"ok": True, "detail": secret}},
        "credentials": [{"name": "codex-oauth", "state": "ok", "token": secret}],
        "queues": {"notifications": {"pending": 1, "payload": secret}},
        "supervisor": {"installed": True},
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
    assert health["supervisor"]["running"] is True
    assert health["supervisor"]["status"] == "running"
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
                "rule_offer": "", "effect": "", "action": "",
                "authorization_basis": "", "grant_options": [], "state": "pending",
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

    # The editor-area project map is the only additional reviewed IDE document.  It receives the
    # same exact per-process check; this does not become a wildcard for the other HTML surfaces.
    map_embedded = headers("/map?ide=1&vscode_embed=" + secret)
    assert map_embedded.get("X-Frame-Options") is None
    map_csp = map_embedded.get("Content-Security-Policy", "")
    assert "frame-ancestors vscode-webview: https://*.vscode-cdn.net" in map_csp

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


def test_procedural_memory_api_requires_auth_and_explicit_review(web_server):
    import time
    from harness.procedure_memory import ProcedureMemory

    base, token, state = web_server
    project = str(state / "repo")
    (state / "repo").mkdir()
    with ProcedureMemory(str(state / "procedural-memory.db")) as store:
        now = time.time()
        for session, offset in (("a", 0), ("b", 100)):
            store.observe(session=session, project=project, app="browser",
                          action="browser_navigate", object_kind="web",
                          object_ref="https://example.test/private?q=secret",
                          observed_at=now + offset)
            store.observe(session=session, project=project, app="terminal",
                          action="shell", object_kind="command",
                          object_ref="pytest --token secret", observed_at=now + offset + 1)

    code, denied = _json(base + "/api/procedures")
    assert code == 403 and denied["error"] == "forbidden"
    code, found = _json(base + "/api/procedures/discover?token=" + token, "POST",
                        {"project": project, "min_support": 2})
    assert code == 200 and found["candidates"]
    candidate_id = max(found["candidates"], key=lambda row: len(row["sequence"]))["candidate_id"]
    code, refused = _json(base + "/api/procedures/review?token=" + token, "POST",
                          {"id": candidate_id, "action": "accept"})
    assert code == 409 and "confirmed=true" in refused["error"]
    code, accepted = _json(base + "/api/procedures/review?token=" + token, "POST", {
        "id": candidate_id, "action": "accept", "confirm": True})
    assert code == 200 and accepted["candidate"]["status"] == "accepted"
    code, snapshot = _json(base + "/api/procedures?token=" + token)
    assert code == 200 and snapshot["guarantees"]["raw_sync"] is False
    assert snapshot["workflows"][0]["authority_scope"] == "none"
    assert "secret" not in json.dumps(snapshot["events"])


def test_personal_intelligence_api_has_one_time_consent_and_local_compression(web_server):
    import time

    base, token, _state = web_server
    code, denied = _json(base + "/api/personal")
    assert code == 403 and denied["error"] == "forbidden"
    code, refused = _json(base + "/api/procedures/privacy?token=" + token, "POST", {
        "observation_mode": "personal"})
    assert code in (400, 409) and "consent" in refused["error"]
    code, enabled = _json(base + "/api/procedures/privacy?token=" + token, "POST", {
        "observation_mode": "personal", "consent": True})
    assert code == 200 and enabled["privacy"]["observation_mode"] == "personal"

    code, refused = _json(base + "/api/personal/source?token=" + token, "POST", {
        "source_id": "browser_history", "enabled": True,
        "permission_state": "granted"})
    assert code in (400, 409) and "consent" in refused["error"]
    code, connected = _json(base + "/api/personal/source?token=" + token, "POST", {
        "source_id": "browser_history", "enabled": True,
        "permission_state": "granted", "confirm": True,
        "scopes": ["origin", "time_bucket", "count"]})
    assert code == 200 and connected["source"]["enabled"] is True
    code, ingested = _json(base + "/api/personal/history?token=" + token, "POST", {
        "items": [{"url": "https://shop.example/orders/secret?token=x",
                   "lastVisitTime": int(time.time() * 1000), "visit_count": 3}]})
    assert code == 200 and ingested["raw_stored"] is False
    code, snapshot = _json(base + "/api/personal?token=" + token)
    assert code == 200
    wire = json.dumps(snapshot)
    assert "https://shop.example" in wire
    assert "orders/secret" not in wire and "token=x" not in wire
    assert snapshot["guarantees"]["browser_history_can_create_order"] is False

    code, email = _json(base + "/api/personal/source?token=" + token, "POST", {
        "source_id": "primary_email", "kind": "email", "enabled": True,
        "permission_state": "granted", "confirm": True, "scopes": ["orders"]})
    assert code == 200 and email["source"]["kind"] == "email"
    code, event = _json(base + "/api/personal/event?token=" + token, "POST", {
        "event": {"event_type": "order_shipment", "title": "Package",
                  "status": "shipped", "expected_at": int(time.time()) - 30,
                  "source_id": "primary_email", "source_kind": "email",
                  "source_ref": "message-9", "evidence_digest": "c" * 64,
                  "confidence": 0.93}})
    assert code == 200 and event["event"]["source_ref_digest"] != "message-9"
    code, snapshot = _json(base + "/api/personal?token=" + token)
    assert code == 200 and snapshot["reminders"][0]["state"] == "possibly_delayed"
    assert snapshot["reminders"][0]["authority_scope"] == "notify_only"


def test_authority_grant_revoke_api_is_authenticated_and_exact(web_server):
    from harness.authority import AuthorityStore, GrantScope

    base, token, state = web_server
    store = AuthorityStore(str(state / "authority.db"))
    grant = store.add(scope=GrantScope.PROJECT, action="publish", project="collie")
    store.close()
    code, denied = _json(base + "/api/security/authority/revoke", "POST",
                         {"id": grant.id, "confirmed": True})
    assert code == 403 and denied["error"] == "forbidden"
    code, removed = _json(base + "/api/security/authority/revoke?token=" + token, "POST",
                          {"id": grant.id, "confirmed": True})
    assert code == 200 and removed["removed"] is True


def test_online_control_surface_is_authenticated_and_token_free(web_server):
    base, token, _state = web_server
    code, denied = _json(base + "/api/online")
    assert code == 403 and denied["error"] == "forbidden"
    code, value = _json(base + "/api/online?token=" + token)
    assert code == 200 and value["mode"] == "local" and value["cloud_llm_default"] == "off"
    assert "access_token" not in json.dumps(value) and "refresh_token" not in json.dumps(value)
    code, denied = _json(base + "/api/online/action", "POST", {"action": "logout", "confirmed": True})
    assert code == 403
    code, local = _json(base + "/api/online/action?token=" + token, "POST",
                        {"action": "logout", "confirmed": True})
    assert code == 200 and local["mode"] == "local"


def test_online_first_project_action_is_authenticated_and_bounded(web_server, monkeypatch):
    from harness import onlinecontrol

    base, token, _state = web_server
    seen = {}

    def fake_create(path, **values):
        seen.update(values)
        return {"ok": True, "project": {"project_id": "p1", "name": values["name"]}}

    monkeypatch.setattr(onlinecontrol, "create_project", fake_create)
    body = {"action": "create_project", "name": "Shared work",
            "local_project": "collie", "memory_data_class": "sealed"}
    code, denied = _json(base + "/api/online/action", "POST", body)
    assert code == 403 and denied["error"] == "forbidden"
    code, value = _json(base + "/api/online/action?token=" + token, "POST", body)
    assert code == 200 and value["project"]["project_id"] == "p1"
    assert seen == {"name": "Shared work", "local_project": "collie",
                    "memory_data_class": "sealed", "cwd": ""}


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
    # Simulate a process launched without a model override as well as clearing its current env.
    # settings remembers hard overrides at import, including a mock model supplied by the runner.
    monkeypatch.setattr(settings, "_HARD_ENV", settings._HARD_ENV - {"COLLIE_MODEL"})
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
                  "/api/mission/specialist/cancel", "System activity", "/api/doctor",
                  "/api/recovery-center", "/api/automations", "/api/memory/claims",
                  "/api/budgets", "/api/security"):
        assert value in page
    assert "confirmed: true" in page and "PRIVATE" not in page
    assert "Auto — Collie chooses per task" in page
    assert "entry.auto ? { auto: true }" in page


def test_health_probes_the_port_this_server_is_listening_on(web_server, monkeypatch):
    """A server that moved off 8787 (it was taken) must not report itself unreachable."""
    from harness import ops
    base, token, _ = web_server
    port = int(base.rsplit(":", 1)[1])
    probed = []
    real_urlopen = ops.urllib.request.urlopen

    def recording(url, *args, **kwargs):
        target = url if isinstance(url, str) else url.full_url
        probed.append(target)
        if target.startswith("http://127.0.0.1:8787/"):
            raise OSError("nothing listens on the default port in this test")
        return real_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(ops.urllib.request, "urlopen", recording)
    code, report = _json(base + "/api/healthz?token=" + token)
    assert code == 200
    assert "http://127.0.0.1:%d/api/ver" % port in probed
    assert report["services"]["web"]["ok"] is True
