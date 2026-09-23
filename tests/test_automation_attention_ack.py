"""A finished automation run that needs a person must be answerable — exactly once, by name.

An execution that ends ``needs_you`` used to pin the health badge red forever: the only offered
action was navigation, and nothing anywhere could say "I looked at this".  The answer is a durable
acknowledgement of one exact execution.  It is deliberately *not* "the newest run wins": two daily
runs can strand two different pieces of work, and a later success proves nothing about an earlier
incident, so every execution is acknowledged on its own and a new one arrives unreviewed.

Acknowledging is a receipt, not a resolution: state, error, result receipt and saved conversation
stay exactly as the run left them, and a run that has not finished cannot be acknowledged at all.
"""
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


def _store(tmp_path):
    from harness.automations import AutomationStore
    return AutomationStore(str(tmp_path / "automations.db"))


def _spec(automation_id="daily-review"):
    from harness.automations import AutomationSpec
    return AutomationSpec.from_dict({
        "automation_id": automation_id, "task": "review this repository",
        "trigger": {"provider": "timer", "every_s": 3600},
        "workspace": {"mode": "isolated"}, "permissions": {}, "enabled": True})


def _needs_you(store, spec, event_id, *, now, error="budget ceiling reached", result=None):
    """Drive one execution through the real claim/finish path to a terminal needs_you."""
    from harness.automations import NEEDS_YOU
    execution_id = store.enqueue(spec, event_id, {"kind": "timer"}, now=now)
    row = store.claim(now=now)
    assert row and row["execution_id"] == execution_id
    assert store.finish(execution_id, NEEDS_YOU,
                        result if result is not None else {"outcome": "needs_you"},
                        error, lease_token=row["lease_token"], now=now) is True
    return execution_id


def _health(root, monkeypatch):
    from harness.controlplane import health
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    return health(str(root), probe_services=False)


def _automation_rows(report):
    return [row for row in report["work"]["recovery_required"] if row["kind"] == "automation"]


def test_terminal_needs_you_is_permanent_attention_until_that_run_is_reviewed(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    store = _store(tmp_path)
    spec = store.upsert(_spec())
    first = _needs_you(store, spec, "day-1", now=1000.0)
    store.close()

    report = _health(tmp_path, monkeypatch)
    assert [row["execution_id"] for row in _automation_rows(report)] == [first]
    assert report["status"] == "needs_you" and report["ok"] is False

    # A later successful run does not answer for the earlier incident: it stays in the lane.
    store = _store(tmp_path)
    from harness.automations import SUCCEEDED
    later = store.enqueue(spec, "day-2", {"kind": "timer"}, now=2000.0)
    row = store.claim(now=2000.0)
    assert store.finish(later, SUCCEEDED, {}, "", lease_token=row["lease_token"], now=2000.0)
    store.close()
    assert [row["execution_id"] for row in
            _automation_rows(_health(tmp_path, monkeypatch))] == [first]

    # Only the explicit acknowledgement of that exact execution clears it.
    store = _store(tmp_path)
    reviewed = store.mark_attention_reviewed(first, now=3000.0)
    store.close()
    assert reviewed["execution_id"] == first and reviewed["attention_reviewed_at"] == 3000.0
    report = _health(tmp_path, monkeypatch)
    assert _automation_rows(report) == [] and report["work"]["recovery_required"] == []
    assert report["status"] != "needs_you"


def test_review_is_durable_idempotent_and_keeps_the_original_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    store = _store(tmp_path)
    spec = store.upsert(_spec())
    receipt = {"outcome": "needs_you", "session_saved": True, "session_id": "auto-1"}
    execution_id = _needs_you(store, spec, "day-1", now=1000.0,
                              error="permission denied for publish", result=receipt)
    store.mark_attention_reviewed(execution_id, now=3000.0)
    again = store.mark_attention_reviewed(execution_id, now=9000.0)
    store.close()
    # Idempotent: the second acknowledgement keeps the first receipt rather than rewriting it.
    assert again["attention_reviewed_at"] == 3000.0

    # Durable across a reopen (restart/reload), with the raw history untouched.
    reopened = _store(tmp_path)
    row = [item for item in reopened.executions()
           if item["execution_id"] == execution_id][0]
    reopened.close()
    assert row["state"] == "needs_you" and row["attention_reviewed_at"] == 3000.0
    assert row["last_error"] == "permission denied for publish"
    assert json.loads(row["result_json"]) == receipt
    assert _automation_rows(_health(tmp_path, monkeypatch)) == []

    # Execution history keeps the run, its error and its saved conversation link.
    from harness.controlcenter import automation_snapshot
    public = automation_snapshot(str(tmp_path))["executions"][0]
    assert public["state"] == "needs_you" and public["attention_reviewed_at"] == 3000.0
    assert public["session_id"] == "auto-1"
    assert "review this repository" not in json.dumps(public)


def test_review_refuses_unfinished_runs_and_never_touches_another_execution(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    store = _store(tmp_path)
    spec = store.upsert(_spec())
    pending = store.enqueue(spec, "queued", {"kind": "timer"}, now=1000.0)
    with pytest.raises(ValueError):
        store.mark_attention_reviewed(pending)
    claimed = store.claim(now=1000.0)
    assert claimed["execution_id"] == pending           # claimed/running is still not reviewable
    with pytest.raises(ValueError):
        store.mark_attention_reviewed(pending)
    with pytest.raises(KeyError):
        store.mark_attention_reviewed("no-such-execution")

    # Two stranded incidents on a second automation, since the claimed run above holds its own.
    # Acknowledging the older one leaves the newer one asking.
    other = store.upsert(_spec("weekly-review"))
    old = _needs_you(store, other, "day-1", now=1050.0)
    new = _needs_you(store, other, "day-2", now=1100.0)
    store.mark_attention_reviewed(old, now=1200.0)
    store.close()
    assert [row["execution_id"] for row in
            _automation_rows(_health(tmp_path, monkeypatch))] == [new]

    reopened = _store(tmp_path)
    rows = {item["execution_id"]: item for item in reopened.executions()}
    reopened.close()
    assert rows[new]["attention_reviewed_at"] == 0 and rows[new]["state"] == "needs_you"
    assert rows[pending]["state"] == "running" and rows[pending]["attention_reviewed_at"] == 0


def test_concurrent_review_of_an_old_run_cannot_clear_a_newer_one(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    store = _store(tmp_path)
    spec = store.upsert(_spec())
    old = _needs_you(store, spec, "day-1", now=1000.0)
    new = _needs_you(store, spec, "day-2", now=2000.0)
    start, errors = threading.Barrier(4), []

    def review(execution_id, at):
        try:
            start.wait(10)
            store.mark_attention_reviewed(execution_id, now=at)
        except Exception as exc:                        # pragma: no cover - reported below
            errors.append("%s: %s" % (type(exc).__name__, exc))

    threads = [threading.Thread(target=review, args=(old, 5000.0 + index))
               for index in range(3)]
    for thread in threads:
        thread.start()
    start.wait(10)
    for thread in threads:
        thread.join(10)
    store.close()
    assert errors == []

    reopened = _store(tmp_path)
    rows = {item["execution_id"]: item for item in reopened.executions()}
    reopened.close()
    assert rows[old]["attention_reviewed_at"] > 0
    assert rows[new]["attention_reviewed_at"] == 0
    assert [row["execution_id"] for row in
            _automation_rows(_health(tmp_path, monkeypatch))] == [new]


def test_an_older_database_opens_and_reviews_without_losing_rows(tmp_path):
    """The pre-migration schema: no attention column, rows already written."""
    path = str(tmp_path / "automations.db")
    legacy = sqlite3.connect(path)
    legacy.executescript("""
      CREATE TABLE automations(automation_id TEXT PRIMARY KEY, spec_json TEXT NOT NULL,
        enabled INTEGER NOT NULL, updated_at REAL NOT NULL);
      CREATE TABLE trigger_state(automation_id TEXT PRIMARY KEY,
        cursor_json TEXT NOT NULL DEFAULT '{}', last_error TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL);
      CREATE TABLE executions(execution_id TEXT PRIMARY KEY, automation_id TEXT NOT NULL,
        event_id TEXT NOT NULL, request_json TEXT NOT NULL, state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        lease_token TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
        started_at REAL NOT NULL DEFAULT 0, finished_at REAL NOT NULL DEFAULT 0,
        result_json TEXT NOT NULL DEFAULT '{}', last_error TEXT NOT NULL DEFAULT '',
        UNIQUE(automation_id,event_id));
      CREATE TABLE usage(execution_id TEXT PRIMARY KEY, model_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0, actions INTEGER NOT NULL DEFAULT 0,
        wall_s REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
      CREATE TABLE audit(audit_id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
        automation_id TEXT NOT NULL, execution_id TEXT NOT NULL DEFAULT '', event TEXT NOT NULL,
        decision TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}');
    """)
    legacy.execute(
        "INSERT INTO executions(execution_id,automation_id,event_id,request_json,state,"
        "created_at,updated_at,finished_at,result_json,last_error) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("exec-old", "daily-review", "day-0", "{}", "needs_you", 10.0, 10.0, 10.0,
         '{"outcome": "needs_you"}', "stopped before finishing"))
    legacy.commit(); legacy.close()

    store = _store(tmp_path)
    row = store.executions()[0]
    assert row["state"] == "needs_you" and row["attention_reviewed_at"] == 0
    assert row["last_error"] == "stopped before finishing"
    assert store.mark_attention_reviewed("exec-old", now=50.0)["attention_reviewed_at"] == 50.0
    store.close()


# ------------------------------------------------------------------ authenticated HTTP surface
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


def _post(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_review_api_is_authenticated_exact_and_leaks_nothing(web_server, monkeypatch):
    base, token, state = web_server
    store = _store(state)
    spec = store.upsert(_spec())
    execution_id = _needs_you(store, spec, "day-1", now=1000.0,
                              result={"outcome": "needs_you", "answer": "private model answer"})
    pending = store.enqueue(spec, "day-2", {"kind": "timer"}, now=2000.0)
    store.close()

    code, denied = _post(base + "/api/automations/review", {"execution_id": execution_id})
    assert code == 403 and denied["error"] == "forbidden"
    code, missing = _post(base + "/api/automations/review?token=" + token,
                          {"execution_id": "no-such-execution"})
    assert code == 404
    code, refused = _post(base + "/api/automations/review?token=" + token,
                          {"execution_id": pending})
    assert code == 400 and "needs_you" in refused["error"]
    code, empty = _post(base + "/api/automations/review?token=" + token, {})
    assert code == 400

    code, reviewed = _post(base + "/api/automations/review?token=" + token,
                           {"execution_id": execution_id})
    assert code == 200 and reviewed["ok"] is True
    assert reviewed["reviewed"]["execution_id"] == execution_id
    assert reviewed["reviewed"]["state"] == "needs_you"
    serialized = json.dumps(reviewed)
    assert "private model answer" not in serialized and "review this repository" not in serialized

    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    assert _automation_rows(_health(state, monkeypatch)) == []
    # The unfinished run is untouched by a neighbour's acknowledgement.
    reopened = _store(state)
    rows = {item["execution_id"]: item for item in reopened.executions()}
    reopened.close()
    assert rows[pending]["state"] == "pending" and rows[pending]["attention_reviewed_at"] == 0


def test_recovery_snapshot_offers_review_next_to_inspect(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    monkeypatch.setattr("harness.supervisor.remote_notifications_enabled", lambda *_a, **_k: False)
    store = _store(tmp_path)
    spec = store.upsert(_spec())
    execution_id = _needs_you(store, spec, "day-1", now=1000.0)
    store.close()

    from harness.controlcenter import automation_review_execution, recovery_snapshot
    items = [row for row in recovery_snapshot(str(tmp_path))["items"]
             if row["kind"] == "automation"]
    assert len(items) == 1 and items[0]["identity"] == execution_id
    assert items[0]["actions"] == ["inspect_automation", "review_automation"]

    assert automation_review_execution(execution_id, str(tmp_path))["ok"] is True
    assert [row for row in recovery_snapshot(str(tmp_path))["items"]
            if row["kind"] == "automation"] == []
