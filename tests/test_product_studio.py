import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def _http_json(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_native_reminder_channel_and_background_delivery(tmp_path, monkeypatch):
    from harness.meeting_reminders import ReminderStore
    from harness import native_notifications

    now = time.time()
    store = ReminderStore(str(tmp_path / "reminders.json"))
    event = store.save_event(title="Planning", start_at=now + 60, end_at=now + 3600)
    prefs = store.update_preferences({"native_enabled": True, "hide_titles": True})
    assert prefs["native_enabled"] is True
    browser = store.claim_due(now=now, channel="browser")
    native = store.claim_due(now=now, channel="native")
    assert browser and native and browser[0]["id"] == native[0]["id"]
    assert not store.claim_due(now=now, channel="native")

    # A fresh event proves the service calls the platform backend and keeps a receipt.
    store.save_event(title="Review", start_at=now + 90, end_at=now + 3600)
    seen = []
    monkeypatch.setattr(native_notifications, "notify", lambda title, body:
                        seen.append((title, body)) or {"ok": True, "backend": "test", "error": ""})
    svc = native_notifications.MeetingReminderService(
        store, interval=60, receipt_path=str(tmp_path / "receipts.json"))
    delivered = svc.tick(now=now)
    assert delivered and seen
    assert "Planning" not in seen[0][1]  # hidden-title privacy preference
    assert json.loads((tmp_path / "receipts.json").read_text(encoding="utf-8"))[-1]["ok"]


def test_workflow_capture_evaluate_and_approve_skill(tmp_path, monkeypatch):
    from harness.workflow_capture import WorkflowStore

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    project = tmp_path / "repo"; project.mkdir()
    store = WorkflowStore()
    row = store.start("Release helper", description="Repeat a checked release", session="s1")
    assert store.capture_session_event("s1", "tool", {"name": "bash", "command": "token=sk-secret"})
    store.capture_session_event("s1", "edit", {"path": "src/app.py"})
    store.capture_session_event("s1", "verification_evidence", {"command": "pytest -q"})
    draft = store.stop(row["id"])
    assert "sk-secret" not in draft["draft"]
    events = store.get(row["id"])["events"]
    evaluation = store.evaluate(row["id"], [{
        "name": "prior-run", "event_kinds": [x["kind"] for x in events], "passed": True}])
    assert evaluation["eligible"] is True
    approved = store.approve(row["id"], cwd=str(project), confirm=True)
    skill = Path(approved["installed_path"])
    assert skill.is_file() and project in skill.parents
    assert store.replay_plan(row["id"])["dry_run"] is True


def test_migration_center_dry_run_conflicts_and_sync(tmp_path, monkeypatch):
    from harness import migration_center as mc

    state = tmp_path / "state"; source = tmp_path / "codex"
    (source / "skills" / "review").mkdir(parents=True)
    (source / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: review code\n---\n", encoding="utf-8")
    (source / "config.toml").write_text("model='x'\n", encoding="utf-8")
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    original = mc._sources
    monkeypatch.setattr(mc, "_sources", lambda: {"codex": {
        "root": source, "instructions": ["AGENTS.md"], "settings": ["config.toml"],
        "skills": ["skills"], "sessions": ["sessions"], "hooks": ["hooks"]}})
    center = mc.MigrationCenter()
    plan = center.plan("codex", kinds=["settings", "skills"], keep_synced=True)
    assert len(plan["items"]) == 2 and all(not x["conflict"] for x in plan["items"])
    result = center.apply(plan["id"], confirm=True)
    assert result["status"] == "applied"
    installed = state / "skills" / "imported-codex-review" / "SKILL.md"
    assert installed.is_file()
    installed.write_text("different\n", encoding="utf-8")
    conflict = center.sync("codex", confirm=True)
    assert conflict["status"] == "needs_review" and "skills/review/SKILL.md" in conflict["conflicts"]
    monkeypatch.setattr(mc, "_sources", original)


def test_plan_graph_leases_dependencies_and_file_conflicts(tmp_path, monkeypatch):
    from harness.plantool import PlanArtifactStore

    monkeypatch.setenv("COLLIE_PLAN_DIR", str(tmp_path / "plans"))
    # plantool's module-level directory is intentionally configurable at import; patch for this test.
    import harness.plantool as plantool
    monkeypatch.setattr(plantool, "_DIR", str(tmp_path / "plans"))
    store = PlanArtifactStore()
    artifact = store.update("scope", {"todos": [
        {"id": "a", "content": "first", "files": ["src/app.py"]},
        {"id": "b", "content": "second", "depends_on": ["a"], "files": ["src/app.py"]},
        {"id": "c", "content": "parallel", "files": ["src/app.py"]},
    ]})
    graph = store.graph("scope")
    assert next(x for x in graph["nodes"] if x["id"] == "a")["ready"]
    assert not next(x for x in graph["nodes"] if x["id"] == "b")["ready"]
    claim = store.claim("scope", "a", "worker-a", expected_revision=artifact["revision"])
    with pytest.raises(ValueError, match="file conflict"):
        store.claim("scope", "c", "worker-c")
    with pytest.raises(ValueError, match="token"):
        store.release("scope", "a", "wrong", completed=True)
    store.release("scope", "a", claim["claim_token"], completed=True, evidence="tests passed")
    assert next(x for x in store.graph("scope")["nodes"] if x["id"] == "b")["ready"]


def test_anchored_annotations_create_rework_and_resolve(tmp_path):
    from harness.annotations import AnnotationStore

    store = AnnotationStore(str(tmp_path / "annotations.db"))
    try:
        row = store.create("file", "working-tree", {"path": "src/app.py", "line": 12,
                           "end_line": 14, "context": "def run"}, "Handle the empty result")
        handoff = store.rework([row["annotation_id"]])
        assert "src/app.py:12-14" in handoff["prompt"]
        assert handoff["intent"] == "build"
        store.resolve(row["annotation_id"], action="resolved", resolution="covered by test")
        assert store.list(status="open") == []
    finally:
        store.close()


def test_session_time_travel_and_safe_worktree_handoff(tmp_path, monkeypatch):
    from harness import sessions

    session_dir = tmp_path / "sessions"; monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(session_dir))
    repo = tmp_path / "repo"; repo.mkdir(); _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com"); _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8"); _git(repo, "add", "a.txt"); _git(repo, "commit", "-m", "base")
    sessions.save("parent", [{"role": "user", "content": "one"},
                              {"role": "assistant", "content": "two"}], cwd=str(repo))
    child = sessions.fork("parent", 1, child_id="child", title="alternate")
    assert child["messages"] == 1 and sessions.timeline("parent")["children"][0]["id"] == "child"
    isolated = sessions.handoff("child", "isolated")["workspace"]
    Path(isolated["path"], "a.txt").write_text("changed\n", encoding="utf-8")
    local = sessions.handoff("child", "local", confirm=True)["workspace"]
    assert "a.txt" in local["applied_files"]
    assert (repo / "a.txt").read_text(encoding="utf-8") == "changed\n"


def test_studio_web_contracts_are_authenticated_and_actionable(tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer
    from harness import plantool, sessions, webapp

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(plantool, "_DIR", str(tmp_path / "plans"))
    sessions.save("studio-session", [{"role": "user", "content": "plan"}], cwd=str(tmp_path))
    seeded = plantool.PlanArtifactStore().update("web:studio-session", {"todos": [
        {"id": "one", "content": "First", "files": ["a.py"]}]})
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]; token = webapp.TOKEN
    try:
        with urllib.request.urlopen(base + "/studio", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert "Workflow → Skill" in page and token in page
        code, denied = _http_json(base + "/api/plan/claim", "POST", {
            "session": "studio-session", "task_id": "one", "owner": "worker"})
        assert code == 403 and denied["error"] == "forbidden"
        code, graph = _http_json(base + "/api/plan/graph?session=studio-session&token=" + token)
        assert code == 200 and graph["nodes"][0]["ready"] is True
        code, claimed = _http_json(base + "/api/plan/claim?token=" + token, "POST", {
            "session": "studio-session", "task_id": "one", "owner": "worker",
            "revision": seeded["revision"]})
        assert code == 200 and claimed["result"]["claim_token"]
        code, created = _http_json(base + "/api/annotations/create?token=" + token, "POST", {
            "kind": "session", "artifact_id": "studio-session",
            "anchor": {"session_id": "studio-session", "message_index": 0},
            "body": "Clarify this request"})
        assert code == 200 and created["result"]["status"] == "open"
        code, timeline = _http_json(base + "/api/session/timeline?session=studio-session&token=" + token)
        assert code == 200 and timeline["nodes"][0]["summary"] == "plan"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
