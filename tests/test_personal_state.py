"""Local notes/calendar contract: isolated storage, tools, CLI and token-gated Web API."""

import json
import os
import sqlite3
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from harness import cli, webapp
from harness.personal_state import PersonalState, default_path, parse_time
from harness.personal_tools import CalendarReadTool, CalendarSaveTool, NoteSaveTool, NotesReadTool


def _request(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_hq_store_is_isolated_from_other_personal_db(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    # A separate experimental database may already exist on a developer machine. HQ must neither
    # open nor migrate it without an explicit import action.
    sqlite3.connect(tmp_path / "personal.db").close()
    assert default_path() == str(tmp_path / "collie-personal.db")
    with PersonalState() as state:
        assert state.notes() == []
    assert (tmp_path / "collie-personal.db").exists()


def test_note_and_calendar_crud(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    with PersonalState() as state:
        note = state.add_note("First line\nDetails", pinned=True)
        assert note["title"] == "First line" and note["pinned"] == 1
        assert state.find_note("first line")["id"] == note["id"]
        note = state.append_note(note["id"], "More")
        assert note["body"].endswith("More")
        note = state.update_note(note["id"], title="Plan", pinned=False)
        assert note["title"] == "Plan" and note["pinned"] == 0
        assert state.notes(query="more")[0]["id"] == note["id"]

        event = state.add_event("Interview", "2026-08-27T10:00:00-07:00",
                                end_at="2026-08-27T11:00:00-07:00",
                                kind="interview", location="Zoom")
        assert event["start_at"] == parse_time("2026-08-27T10:00:00-07:00")
        assert state.events(since="2026-08-27", until="2026-08-27")[0]["id"] == event["id"]
        event = state.update_event(event["id"], location="Office", kind="meeting")
        assert event["location"] == "Office" and event["kind"] == "meeting"
        assert state.delete_event(event["id"])["id"] == event["id"]
        assert state.delete_note(note["id"])["id"] == note["id"]


def test_personal_tools_require_person_facing_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    worker = types.SimpleNamespace(gate=None)
    owner = types.SimpleNamespace(gate=object())
    assert NotesReadTool().run({}, worker).startswith("ERROR:")
    assert CalendarSaveTool().run(
        {"title": "must not write", "start": "2026-08-27"}, worker).startswith("ERROR:")

    created = json.loads(NoteSaveTool().run(
        {"action": "create", "title": "Ideas", "text": "Ship local first"}, owner))
    nid = created["note"]["id"]
    assert json.loads(NotesReadTool().run({"query": "local"}, owner))["notes"][0]["id"] == nid
    event = json.loads(CalendarSaveTool().run(
        {"action": "create", "title": "Call", "start": "2026-08-28T09:00:00-07:00"},
        owner))["event"]
    assert json.loads(CalendarReadTool().run({"id": event["id"]}, owner))["event"]["title"] == "Call"


def test_personal_cli_and_capability_share_structured_store(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_NOTES_DIR", str(tmp_path / "flat-notes"))
    args = types.SimpleNamespace(action="add", value="CLI body", title="CLI note", text=None,
                                 query="", limit=50, pinned=False, as_json=True)
    assert cli.cmd_notes(args) == 0
    assert "CLI note" in capsys.readouterr().out

    from harness.capabilities import _note_execute
    record = types.SimpleNamespace(args={"file": "delegated.txt", "text": "job result"})
    _note_execute(record)
    with PersonalState() as state:
        assert state.find_note("delegated")["body"] == "job result"
        assert state.notes(query="CLI body")[0]["title"] == "CLI note"


def test_personal_web_api_is_token_gated_and_persistent(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    token = "?token=" + webapp.TOKEN
    try:
        with urllib.request.urlopen(root + "/personal", timeout=5) as response:
            page = response.read().decode()
        assert "Stored only on this computer" in page
        assert "/api/state/notes" in page and "/api/state/events" in page
        assert 'meta name="collie-token"' in page

        code, _ = _request(root + "/api/state/notes")
        assert code == 403
        code, created = _request(root + "/api/state/note" + token, "POST",
                                 {"title": "Web note", "text": "private and local"})
        assert code == 201
        nid = created["note"]["id"]
        code, found = _request(root + "/api/state/notes?id=" + nid + "&token=" + webapp.TOKEN)
        assert code == 200 and found["note"]["body"] == "private and local"

        code, created = _request(root + "/api/state/event" + token, "POST",
                                 {"title": "Demo", "start": "2026-08-29T15:00:00-07:00"})
        assert code == 201
        eid = created["event"]["id"]
        code, found = _request(root + "/api/state/events?id=" + eid + "&token=" + webapp.TOKEN)
        assert code == 200 and found["event"]["title"] == "Demo"
        code, missing = _request(root + "/api/state/events?id=missing&token=" + webapp.TOKEN)
        assert code == 404 and missing["event"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
