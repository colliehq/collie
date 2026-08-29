import json
import os
import time

import pytest

from harness.audit import AuditLog
from harness.procedure_memory import ProcedureMemory


def _repeat(store, project, session, at):
    store.observe(session=session, project=project, app="browser",
                  action="browser_navigate", object_kind="web",
                  object_ref="https://example.com/private?q=secret", observed_at=at)
    store.observe(session=session, project=project, app="filesystem",
                  action="read_file", object_kind="file",
                  object_ref=os.path.join(project, "CHANGELOG.md"), observed_at=at + 1)
    store.observe(session=session, project=project, app="terminal",
                  action="shell", object_kind="command",
                  object_ref="pytest -q --token secret", result="allowed", observed_at=at + 2)


def test_raw_observations_are_structured_device_only_and_content_free(tmp_path):
    path = tmp_path / "procedures.db"
    with ProcedureMemory(str(path)) as store:
        event_id = store.observe_tool(
            "browser_type",
            {"text": "my private message", "password": "hunter2",
             "url": "https://mail.example.test/inbox?token=secret"},
            session="s1", project=str(tmp_path), result="once",
            metadata={"body": "private body", "risk": "off_machine"})
        row = store.list_events()[0]

    assert event_id
    assert row["data_class"] == "device_only"
    assert row["object_ref"] == "https://mail.example.test"
    serialized = json.dumps(row)
    assert "my private message" not in serialized
    assert "hunter2" not in serialized
    assert "private body" not in serialized
    assert row["metadata"]["body"] == "[12 chars]"


def test_privacy_pause_and_sensitive_apps_are_skipped(tmp_path):
    with ProcedureMemory(str(tmp_path / "procedures.db")) as store:
        assert store.observe(session="s", app="1Password", action="open") is None
        assert store.observe(session="s", app="browser", action="open",
                             metadata={"incognito": True}) is None
        store.update_privacy(paused=True)
        assert store.observe(session="s", app="terminal", action="shell") is None
        store.update_privacy(paused=False, exclude_app="music")
        assert store.observe(session="s", app="Music Player", action="play") is None
        assert store.list_events() == []


def test_repeated_sequences_become_reviewable_zero_authority_workflows(tmp_path):
    project = str(tmp_path / "repo")
    os.makedirs(project)
    with ProcedureMemory(str(tmp_path / "procedures.db")) as store:
        now = time.time()
        _repeat(store, project, "session-a", now)
        _repeat(store, project, "session-b", now + 100)
        assert store.list_candidates(status="proposed", project=project)
        candidates = store.discover(project=project)

        assert candidates
        candidate = max(candidates, key=lambda row: len(row["sequence"]))
        assert candidate["support"] == 2
        assert candidate["status"] == "proposed"
        assert candidate["data_class"] == "sealed"
        with pytest.raises(ValueError, match="confirmed=true"):
            store.review(candidate["candidate_id"], "accept")

        accepted = store.review(candidate["candidate_id"], "accept", confirmed=True)
        workflows = store.list_workflows(project=project)

    assert accepted["status"] == "accepted"
    assert len(workflows) == 1
    assert workflows[0]["authority_scope"] == "none"
    assert workflows[0]["data_class"] == "sealed"


def test_dismissal_survives_rediscovery_and_raw_events_can_be_purged(tmp_path):
    project = str(tmp_path / "repo")
    os.makedirs(project)
    with ProcedureMemory(str(tmp_path / "procedures.db")) as store:
        now = time.time()
        _repeat(store, project, "a", now)
        _repeat(store, project, "b", now + 100)
        candidate = max(store.discover(project=project),
                        key=lambda row: len(row["sequence"]))
        store.review(candidate["candidate_id"], "dismiss", confirmed=True)
        store.discover(project=project)
        assert store.get_candidate(candidate["candidate_id"])["status"] == "dismissed"
        with pytest.raises(ValueError, match="confirmed=true"):
            store.purge_events()
        assert store.purge_events(confirmed=True) == 6
        assert store.list_events() == []


def test_audit_log_teaches_only_actions_that_passed_the_gate(tmp_path):
    audit = AuditLog(str(tmp_path / "audit.db"))
    audit.record(session="s", cwd=str(tmp_path), tool="browser_type",
                 target="https://example.test/form?private=1", stage="denied",
                 outcome="deny", reason="user declined", args={"text": "denied private text"})
    audit.record(session="s", cwd=str(tmp_path), tool="browser_type",
                 target="https://example.test/form?private=1", stage="approved",
                 outcome="once", reason="user approved", args={"text": "approved private text"})
    audit.close()

    with ProcedureMemory(str(tmp_path / "procedural-memory.db")) as store:
        rows = store.list_events()
    assert len(rows) == 1
    assert rows[0]["object_ref"] == "https://example.test"
    serialized = json.dumps(rows)
    assert "denied private text" not in serialized
    assert "approved private text" not in serialized
