"""Conversational tools for the owner's local notes and calendar."""
from __future__ import annotations

import json
import time

from .personal_state import PersonalState, format_time, parse_time
from .tools import Tool

__all__ = ["NotesReadTool", "NoteSaveTool", "CalendarReadTool", "CalendarSaveTool",
           "register_personal"]


def _allowed(ctx) -> bool:
    # Benchmarks, Pack attempts and embedded worker runs have no person-facing gate.  They may see
    # the schemas in the shared registry, but cannot read or mutate the owner's personal database.
    return getattr(ctx, "gate", None) is not None


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _event_view(event: dict) -> dict:
    out = dict(event)
    out["start"] = format_time(event.get("start_at"), all_day=bool(event.get("all_day")))
    out["end"] = format_time(event.get("end_at"), all_day=bool(event.get("all_day")))
    return out


class NotesReadTool(Tool):
    name = "notes_read"
    description = ("Read the owner's local notes. Supply id for one exact note, or query to search title/body. "
                   "Returns most recently updated notes first, with pinned notes first.")
    schema = {"type": "object", "properties": {
        "id": {"type": "string"}, "query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}}

    def run(self, args, ctx):
        if not _allowed(ctx):
            return "ERROR: personal notes are unavailable in benchmarks or worker runs"
        try:
            with PersonalState() as state:
                if args.get("id"):
                    note = state.note(str(args["id"]))
                    return _json({"note": note}) if note else "ERROR: unknown note"
                return _json({"notes": state.notes(query=str(args.get("query") or ""),
                                                    limit=int(args.get("limit") or 30))})
        except Exception as exc:
            return "ERROR reading notes: %s" % exc


class NoteSaveTool(Tool):
    name = "note_save"
    description = ("Create, append to, update, pin/unpin, or delete one local personal note. "
                   "action=create needs text; append needs id (or append_to title) and text; "
                   "update/delete need id. Nothing is synced to a cloud service.")
    schema = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["create", "append", "update", "delete"]},
        "id": {"type": "string"}, "append_to": {"type": "string"},
        "title": {"type": "string"}, "text": {"type": "string"},
        "pinned": {"type": "boolean"}, "project_id": {"type": "string"},
        "goal_id": {"type": "string"}}}

    def run(self, args, ctx):
        if not _allowed(ctx):
            return "ERROR: personal notes are unavailable in benchmarks or worker runs"
        action = str(args.get("action") or "create").lower()
        try:
            with PersonalState() as state:
                note_id = str(args.get("id") or "").strip()
                if action == "create":
                    note = state.add_note(str(args.get("text") or ""),
                                          title=str(args.get("title") or ""),
                                          project_id=str(args.get("project_id") or ""),
                                          goal_id=str(args.get("goal_id") or ""),
                                          pinned=bool(args.get("pinned")), source="collie")
                    return _json({"ok": True, "created": True, "note": note})
                if not note_id and args.get("append_to"):
                    target = state.find_note(str(args.get("append_to") or ""))
                    note_id = str((target or {}).get("id") or "")
                if action == "append":
                    note = state.append_note(note_id, str(args.get("text") or ""), source="collie")
                    return _json({"ok": True, "note": note}) if note else "ERROR: unknown note"
                if action == "update":
                    fields = {}
                    if "title" in args: fields["title"] = args.get("title")
                    if "text" in args: fields["body"] = args.get("text")
                    if "pinned" in args: fields["pinned"] = args.get("pinned")
                    if "project_id" in args: fields["project_id"] = args.get("project_id")
                    if "goal_id" in args: fields["goal_id"] = args.get("goal_id")
                    note = state.update_note(note_id, **fields)
                    return _json({"ok": True, "note": note}) if note else "ERROR: unknown note"
                if action == "delete":
                    note = state.delete_note(note_id)
                    return _json({"ok": True, "deleted": note_id}) if note else "ERROR: unknown note"
                return "ERROR: unknown note action %r" % action
        except Exception as exc:
            return "ERROR saving note: %s" % exc


class CalendarReadTool(Tool):
    name = "calendar_read"
    description = ("Read the owner's local calendar. By default returns the next 14 days. "
                   "Use since/until as ISO-8601 dates or date-times, or query to search.")
    schema = {"type": "object", "properties": {
        "id": {"type": "string"}, "since": {"type": "string"},
        "until": {"type": "string"}, "days": {"type": "integer"},
        "query": {"type": "string"}, "limit": {"type": "integer"}}}

    def run(self, args, ctx):
        if not _allowed(ctx):
            return "ERROR: personal calendar is unavailable in benchmarks or worker runs"
        try:
            with PersonalState() as state:
                if args.get("id"):
                    event = state.event(str(args["id"]))
                    return _json({"event": _event_view(event)}) if event else "ERROR: unknown event"
                since = args.get("since")
                until = args.get("until")
                if since in (None, "") and until in (None, ""):
                    since = int(time.time()) - 3600
                    until = int(time.time()) + max(1, int(args.get("days") or 14)) * 86400
                events = state.events(since=since, until=until,
                                      query=str(args.get("query") or ""),
                                      limit=int(args.get("limit") or 50))
                return _json({"events": [_event_view(event) for event in events]})
        except Exception as exc:
            return "ERROR reading calendar: %s" % exc


class CalendarSaveTool(Tool):
    name = "calendar_save"
    description = ("Create, update, or delete one event in the owner's local calendar. "
                   "create needs title and start; date/time values are ISO-8601 or epoch seconds. "
                   "Naive values use this computer's timezone. Nothing is sent to a cloud service.")
    schema = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["create", "update", "delete"]},
        "id": {"type": "string"}, "title": {"type": "string"},
        "start": {"type": "string"}, "end": {"type": "string"},
        "all_day": {"type": "boolean"}, "kind": {"type": "string"},
        "location": {"type": "string"}, "notes": {"type": "string"},
        "project_id": {"type": "string"}, "goal_id": {"type": "string"},
        "dedupe": {"type": "boolean"}}}

    def run(self, args, ctx):
        if not _allowed(ctx):
            return "ERROR: personal calendar is unavailable in benchmarks or worker runs"
        action = str(args.get("action") or "create").lower()
        try:
            with PersonalState() as state:
                event_id = str(args.get("id") or "").strip()
                if action == "delete":
                    event = state.delete_event(event_id)
                    return _json({"ok": True, "deleted": event_id}) if event else "ERROR: unknown event"
                fields = {}
                mapping = {"start": "start_at", "end": "end_at"}
                for key in ("title", "start", "end", "all_day", "kind", "location", "notes",
                            "project_id", "goal_id"):
                    if key in args:
                        fields[mapping.get(key, key)] = args.get(key)
                if action == "update":
                    event = state.update_event(event_id, **fields)
                    return _json({"ok": True, "event": _event_view(event)}) if event else "ERROR: unknown event"
                if action != "create":
                    return "ERROR: unknown calendar action %r" % action
                title, start = str(args.get("title") or "").strip(), args.get("start")
                if args.get("dedupe") and title and start not in (None, ""):
                    stamp = parse_time(start)
                    existing = next((row for row in state.events(since=stamp, until=stamp, limit=100)
                                     if str(row.get("title") or "").casefold() == title.casefold()
                                     and int(row.get("start_at") or 0) == stamp), None)
                    if existing:
                        return _json({"ok": True, "created": False, "event": _event_view(existing)})
                event = state.add_event(title, start, end_at=args.get("end"),
                                        all_day=bool(args.get("all_day")),
                                        kind=str(args.get("kind") or "meeting"),
                                        location=str(args.get("location") or ""),
                                        notes=str(args.get("notes") or ""),
                                        project_id=str(args.get("project_id") or ""),
                                        goal_id=str(args.get("goal_id") or ""))
                return _json({"ok": True, "created": True, "event": _event_view(event)})
        except Exception as exc:
            return "ERROR saving calendar: %s" % exc


def register_personal(registry) -> None:
    for tool in (NotesReadTool(), NoteSaveTool(), CalendarReadTool(), CalendarSaveTool()):
        registry.register(tool)
