"""Token-gated Web API for local notes and calendar state.

Only the local personal layer lives here. There are intentionally no account, sync, or
cloud-execution routes in this module.
"""
from __future__ import annotations

import time

from .personal_state import PersonalState, format_time


def _event_view(event: dict) -> dict:
    out = dict(event)
    out["start"] = format_time(event.get("start_at"), all_day=bool(event.get("all_day")))
    out["end"] = format_time(event.get("end_at"), all_day=bool(event.get("all_day")))
    return out


def _bad(handler, message: str, code: int = 400):
    handler._send_json({"error": message}, code)
    return True


def handle_get(handler, path: str, parsed, qs: dict) -> bool:
    if path not in ("/api/state/notes", "/api/state/events"):
        return False
    if not handler._authed(parsed):
        handler._send_json({"error": "forbidden"}, 403)
        return True
    try:
        with PersonalState() as state:
            if path == "/api/state/notes":
                note_id = str((qs.get("id") or [""])[0] or "").strip()
                if note_id:
                    note = state.note(note_id)
                    handler._send_json({"note": note}, 200 if note else 404)
                else:
                    query = str((qs.get("q") or [""])[0] or "").strip()
                    limit = max(1, min(500, int((qs.get("limit") or ["100"])[0] or 100)))
                    handler._send_json({"notes": state.notes(query=query, limit=limit)})
                return True
            event_id = str((qs.get("id") or [""])[0] or "").strip()
            if event_id:
                event = state.event(event_id)
                handler._send_json({"event": _event_view(event) if event else None},
                                   200 if event else 404)
                return True
            now = int(time.time())
            since = (qs.get("since") or [now - 7 * 86400])[0]
            until = (qs.get("until") or [now + 60 * 86400])[0]
            query = str((qs.get("q") or [""])[0] or "").strip()
            events = state.events(since=since, until=until, query=query, limit=200)
            handler._send_json({"events": [_event_view(event) for event in events], "now": now})
            return True
    except (TypeError, ValueError) as exc:
        return _bad(handler, str(exc))
    except Exception as exc:
        handler._send_json({"error": "personal state unavailable: %s" % exc,
                            "unavailable": True}, 503)
        return True


def handle_post(handler, path: str, parsed) -> bool:
    if path not in ("/api/state/note", "/api/state/event"):
        return False
    if not handler._authed(parsed):
        handler._send_json({"error": "forbidden"}, 403)
        return True
    body = handler._read_json(65536)
    if body is None:
        return _bad(handler, "bad body")
    try:
        with PersonalState() as state:
            action = str(body.get("action") or "add").strip().lower()
            if path == "/api/state/note":
                note_id = str(body.get("note_id") or body.get("id") or "").strip()
                if action == "delete":
                    old = state.delete_note(note_id)
                    if not old:
                        return _bad(handler, "unknown note", 404)
                    handler._send_json({"ok": True, "deleted": note_id})
                    return True
                if action == "append":
                    if not note_id and body.get("append_to"):
                        target = state.find_note(str(body.get("append_to") or ""))
                        note_id = str((target or {}).get("id") or "")
                    note = state.append_note(note_id, str(body.get("text") or ""))
                    if not note:
                        return _bad(handler, "unknown note", 404)
                    handler._send_json({"ok": True, "note": note})
                    return True
                if action == "update":
                    fields = {}
                    if "title" in body: fields["title"] = body.get("title")
                    if "text" in body: fields["body"] = body.get("text")
                    if "body" in body: fields["body"] = body.get("body")
                    for key in ("pinned", "project_id", "goal_id"):
                        if key in body: fields[key] = body.get(key)
                    note = state.update_note(note_id, **fields)
                    if not note:
                        return _bad(handler, "unknown note", 404)
                    handler._send_json({"ok": True, "note": note})
                    return True
                if action not in ("add", "create"):
                    return _bad(handler, "unknown note action")
                note = state.add_note(str(body.get("text") or body.get("body") or ""),
                                      title=str(body.get("title") or ""),
                                      project_id=str(body.get("project_id") or ""),
                                      goal_id=str(body.get("goal_id") or ""),
                                      pinned=bool(body.get("pinned")))
                handler._send_json({"ok": True, "created": True, "note": note}, 201)
                return True

            event_id = str(body.get("event_id") or body.get("id") or "").strip()
            if action == "delete":
                old = state.delete_event(event_id)
                if not old:
                    return _bad(handler, "unknown event", 404)
                handler._send_json({"ok": True, "deleted": event_id})
                return True
            fields = {}
            aliases = {"start": "start_at", "end": "end_at"}
            for key in ("title", "start", "start_at", "end", "end_at", "all_day", "kind",
                        "location", "notes", "project_id", "goal_id", "external_ref"):
                if key in body:
                    fields[aliases.get(key, key)] = body.get(key)
            if action == "update":
                event = state.update_event(event_id, **fields)
                if not event:
                    return _bad(handler, "unknown event", 404)
                handler._send_json({"ok": True, "event": _event_view(event)})
                return True
            if action not in ("add", "create"):
                return _bad(handler, "unknown event action")
            title = str(body.get("title") or "").strip()
            start = body.get("start_at") if "start_at" in body else body.get("start")
            event = state.add_event(title, start,
                                    end_at=body.get("end_at") if "end_at" in body else body.get("end"),
                                    all_day=bool(body.get("all_day")),
                                    kind=str(body.get("kind") or "meeting"),
                                    location=str(body.get("location") or ""),
                                    notes=str(body.get("notes") or ""),
                                    project_id=str(body.get("project_id") or ""),
                                    goal_id=str(body.get("goal_id") or ""),
                                    external_ref=str(body.get("external_ref") or ""))
            handler._send_json({"ok": True, "created": True, "event": _event_view(event)}, 201)
            return True
    except (TypeError, ValueError) as exc:
        return _bad(handler, str(exc))
    except Exception as exc:
        handler._send_json({"error": "personal state unavailable: %s" % exc,
                            "unavailable": True}, 503)
        return True
