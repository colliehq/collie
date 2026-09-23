"""The Daily Brief surface: collect the eight sources, build one brief, serve it.

:mod:`daily_brief` is deliberately transport-free -- it takes payloads and returns a
snapshot.  This module reads Today's local stores and the communication inbox,
using the public module functions behind the ``/api/...`` handlers, and hands
the results to ``daily_brief.build``.

Two rules shape everything here.

**Never call our own HTTP server.**  Today fetches its endpoints from the browser;
a server-side collector that did the same would re-enter its own handler, re-do auth,
and deadlock a single-threaded server.  The endpoints in
``daily_brief.PAYLOAD_ENDPOINTS`` are kept only as provenance labels.

**A source that fails is unavailable, not empty.**  Each collector runs inside its own
guard.  One broken store costs its own row a state of ``__unavailable`` and a reason;
it never raises, and it never lets the brief render as a clear day.  That is the whole
point: the product failure this surface exists to avoid is a morning summary that
repeats an old notification as if it were the present.  So every source carries
``collected_at``, every item carries the timestamp its source reported, and nothing
here upgrades "the last email I saw said it failed" into "it is failing".

Mounting (the parent owns ``webapp.py``; this is the whole contract)::

    # GET /api/brief?timezone=Asia/Shanghai&utc_offset_minutes=480&language=zh
    return self._send_json(daily_brief_web.read(
        _state_root(), timezone=query.get("timezone", [""])[0],
        utc_offset_minutes=query.get("utc_offset_minutes", [None])[0],
        language=query.get("language", ["en"])[0]))
    # POST /api/brief  {"action": "dismiss"|"snooze"|"restore"|"preview", ...}
    result = daily_brief_web.perform(_state_root(), body)    # ok:False -> 409 + error

Both are authenticated reads of local state and take no query beyond the above.
``BriefError`` is a ``ValueError``, so the existing ``except ValueError -> 400`` arm
already turns a malformed request into a visible message.  ``GET /brief`` serves
``webui/daily_brief.html``, which is static and fetches only ``/api/brief``.

Root isolation is not optional.  Every store is opened under the ``root`` the caller
passed.  Two of Today's sources -- running runs and pending approvals -- live in the
web server's *process* memory rather than on disk, so they are readable only from
inside the running app and only when ``root`` is that app's own state directory;
anywhere else they are explicitly unavailable with a reason, and never silently empty.

**The messages collector reads, and only reads.**  ``communications`` asks
:class:`~harness.channel_service.ChannelService` for the connections this root already
has and for the events and results each one has already stored.  It never polls a
mailbox, opens a connection, probes, sends, retries, accepts or rejects anything, and
it never touches a credential: those are all separate methods on the same object, and
none of them is called from here.  What it hands to the builder is a deliberately
narrow projection -- state, subject, timestamps and two flags -- because the full row
carries the message body, the sender's address and attachment names, and a brief has
no use for any of them.
"""
from __future__ import annotations

import os
import sys
import time

from . import daily_brief

SCHEMA = "collie.daily_brief.web/1"

BriefError = daily_brief.BriefError

#: What ``perform`` will act on.  Anything else is refused by name.
ACTIONS = ("dismiss", "snooze", "restore", "preview")
SNOOZE_MAX_MINUTES = 60 * 24 * 7
INBOX_LIMIT = 50
PROCEDURE_LIMIT = 20
#: How much of the message stores one brief reads.  Whatever is past these is reported
#: as partial coverage by the builder, never dropped in silence.
CONNECTION_LIMIT = daily_brief.COMMS_CONNECTION_LIMIT
MESSAGE_LIMIT = daily_brief.COMMS_ROW_LIMIT
SUBJECT_LIMIT = 200


def _reason(source, exc):
    """A visible, private-data-free reason one source could not be read.

    Store exception text carries paths, mailbox names and occasionally a row of user
    content.  The class name plus the source name is enough for a person to act on and
    cannot leak what was inside.
    """
    return "%s could not be read (%s)" % (source, type(exc).__name__)


def _live_root():
    from .controlplane import state_dir
    return os.path.realpath(os.path.abspath(state_dir()))


def _is_live_root(root):
    """Is ``root`` the state directory this very process is serving?"""
    try:
        return os.path.realpath(os.path.abspath(str(root))) == _live_root()
    except (OSError, ValueError):
        return False


def _handler():
    """The web server's request handler class, if the server is loaded in-process.

    Deliberately ``sys.modules`` and not an import: the runs and approvals registries
    are per-process state that only exists when this code runs *inside* the serving
    process.  Importing ``webapp`` from a CLI or a test would produce a second, empty
    registry and a brief that claims nothing is running.
    """
    module = sys.modules.get("%s.webapp" % __package__)
    return getattr(module, "Handler", None) if module is not None else None


# ---------------------------------------------------------------- collectors


def _personal(root, live, now):
    from .controlcenter import personal_snapshot
    return personal_snapshot(root)


def _procedures(root, live, now):
    from .controlcenter import procedure_snapshot
    return procedure_snapshot(root, event_limit=PROCEDURE_LIMIT)


def _missions(root, live, now):
    from .missionweb import MissionService
    service = MissionService(state_dir=root)
    try:
        payload = {"missions": service.missions()}
    finally:
        service.close()
    handler = _handler()
    if live and handler is not None:
        module = sys.modules.get("%s.webapp" % __package__)
        status = getattr(module, "mission_ticker_status", None)
        if callable(status):
            payload["scheduler"] = status()
    return payload


def _meetings(root, live, now):
    """Yesterday through the next month, the window ``/api/meetings/schedule`` uses.

    Stated rather than defaulted: the store's own default window is relative to the
    wall clock, and a brief built for a supplied instant must read the same days it
    renders, not the days the machine happens to be standing in.
    """
    from .meeting_reminders import ReminderStore
    store = ReminderStore(os.path.join(str(root), "meeting-reminders.json"))
    return store.snapshot(start_at=now - 86400, end_at=now + 31 * 86400)


def _task_inbox(root, live, now):
    from . import sessions, task_inbox
    rows = task_inbox.pending_sessions(directory=sessions.store_root(root), limit=INBOX_LIMIT)
    if live and rows:
        # Only inside the serving process, and only for its own root: these three
        # helpers read the ambient state directory, so borrowing them for another
        # profile would mix two people's sessions together.
        from . import sessions, web_tasks
        try:
            titles = {row["id"]: row for row in sessions.recent(80)}
        except Exception:                             # a listing, never a blocker
            titles = {}
        for row in rows:
            session = str(row.get("session") or "")
            try:
                row["owner_busy"] = web_tasks.owner_busy(session)
                row["scheduled_wait"] = web_tasks.scheduled_wait(session)
            except Exception:
                pass
            row["title"] = row.get("title") or (titles.get(session) or {}).get("title") or ""
    return {"sessions": rows}


def _runs(root, live, now):
    handler = _handler()
    if not live or handler is None:
        raise _Unwired("what is running right now is only visible inside the running "
                       "Collie app, for its own profile")
    return {"runs": handler._runs_snapshot()}


def _approvals(root, live, now):
    handler = _handler()
    if not live or handler is None:
        raise _Unwired("decisions waiting on you are only visible inside the running "
                       "Collie app, for its own profile")
    return {"approvals": handler._inbox_pending_all()}


def _message_row(event):
    """One received message, reduced to what a brief is allowed to say about it.

    The row this comes from holds the body, the sender's address, the recipient, the
    attachment file names and the raw-message reference.  None of them is copied: a
    brief needs to know that a message is waiting, not what is in it or who sent it.
    ``sender_allowed`` and ``automatic`` are the two flags the builder ranks on, and
    both were decided at intake by the connection's own policy -- never here, and never
    from the subject text.
    """
    acceptance = event.get("acceptance") if isinstance(event.get("acceptance"), dict) else {}
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return {"id": str(event.get("id") or "")[:200],
            "state": str(event.get("state") or "")[:32],
            "subject": str(event.get("subject") or "")[:SUBJECT_LIMIT],
            "sender_allowed": bool(event.get("sender_allowed")),
            "automatic": bool(metadata.get("automatic")),
            "received": event.get("received"), "recorded": event.get("recorded"),
            "acceptance": {"state": str(acceptance.get("state") or "")[:32],
                           "session": str(acceptance.get("session") or "")[:96]}}


def _reply_row(result):
    """One outgoing reply: its state and when it moved, never its text or destination."""
    return {"id": str(result.get("id") or "")[:200],
            "state": str(result.get("state") or "")[:32],
            "subject": str(result.get("subject") or "")[:SUBJECT_LIMIT],
            "session": str(result.get("session") or "")[:96],
            "created": result.get("created"), "updated": result.get("updated"),
            "attempts": result.get("attempts")}


def _communications(root, live, now):
    """Connections, received messages and prepared replies already stored under ``root``.

    Reading only.  ``overview``, ``events`` and ``results`` are the three methods used;
    ``poll``, ``probe``, ``send``, ``retry``, ``accept`` and the credential store are
    not reachable from this function, and a paused or disconnected connection is read
    exactly like any other -- pausing stops new mail arriving, it does not settle the
    drafts and messages already sitting here.

    One connection that will not open costs itself: it is marked ``unreadable`` and the
    others still render, which the builder turns into partial coverage rather than into
    a clear day.  No exception text is carried over -- store errors quote paths, mailbox
    names and occasionally a row of somebody's mail.
    """
    from .channel_service import ChannelService
    service = ChannelService(root)
    rows = service.overview().get("connections") or []
    payload = {"connections": [], "truncated": len(rows) > CONNECTION_LIMIT}
    for row in rows[:CONNECTION_LIMIT]:
        connection = str(row.get("id") or "")[:96]
        entry = {"id": connection, "kind": str(row.get("kind") or "")[:32],
                 "enabled": row.get("enabled") is not False,
                 "status": str(row.get("status") or "")[:32]}
        try:
            events = service.events(connection, limit=MESSAGE_LIMIT, newest=True)
            results = service.results(connection, limit=MESSAGE_LIMIT, newest=True)
        except Exception:                             # noqa: BLE001 - counted, not raised
            entry["unreadable"] = True
        else:
            entry["events"] = [_message_row(event) for event in events]
            entry["results"] = [_reply_row(result) for result in results]
            # Both listings take the newest window.  Filling it means there is older
            # work this brief did not look at, which is partial coverage and not silence.
            entry["truncated"] = (len(events) >= MESSAGE_LIMIT or
                                  len(results) >= MESSAGE_LIMIT)
        payload["connections"].append(entry)
    return payload


class _Unwired(RuntimeError):
    """This source exists, but not from here.  Carries its own explanation."""


COLLECTORS = {"personal": _personal, "missions": _missions, "approvals": _approvals,
              "procedures": _procedures, "runs": _runs, "meetings": _meetings,
              "task_inbox": _task_inbox, "communications": _communications}


def collect(root, *, now=None):
    """``(payloads, report)`` for all eight sources.  Never raises for one bad source.

    ``payloads`` is exactly what :func:`daily_brief.build` wants.  ``report`` records,
    per source, when it was read and why it could not be -- the freshness evidence the
    surface shows instead of implying that an old notice is the current state.
    """
    root = str(root)
    live = _is_live_root(root)
    wall = float(now) if now is not None else time.time()
    payloads, report = {}, []
    for name in daily_brief.SOURCE_NAMES:
        started = time.time()
        try:
            payload = COLLECTORS[name](root, live, wall)
            if not isinstance(payload, dict):
                raise TypeError("payload")
            state, reason = "ok", ""
        except _Unwired as exc:
            payload = {"__unavailable": True, "error": str(exc)}
            state, reason = "unavailable", str(exc)
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            reason = _reason(name, exc)
            payload = {"__unavailable": True, "error": reason}
            state, reason = "unavailable", reason
        finished = time.time()
        payloads[name] = payload
        report.append({"name": name, "state": state, "reason": reason,
                       "endpoint": daily_brief.PAYLOAD_ENDPOINTS[name],
                       "collected_at": finished,
                       "took_ms": max(0, int((finished - started) * 1000))})
    if now is not None:
        for row in report:
            row["collected_at"] = float(now)
    return payloads, report


# ---------------------------------------------------------------- read / perform


def _language(value):
    language = str(value or "en").strip().lower()[:16]
    return "zh" if language.startswith("zh") else "en"


def _offset(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        return None
    return minutes if -840 <= minutes <= 840 else None


def _brief(root, *, timezone, utc_offset_minutes, language, profile, now=None,
           remember=True):
    payloads, report = collect(root, now=now)
    brief = daily_brief.build(
        payloads, now=now, timezone=timezone, language=language, state_dir=root,
        profile=profile, remember=remember, utc_offset_minutes=_offset(utc_offset_minutes),
        fallback_timezone="utc")
    return brief, report


def read(root, *, timezone="UTC", utc_offset_minutes=None, language="en",
         profile="default", now=None):
    """The whole Daily Brief surface in one authenticated GET.

    Builds today's brief from live stores, and returns it with the per-source freshness
    report, the user's current hide/snooze preferences, and a preview of the email
    rendering of this same snapshot.  Opening the brief runs no model and no tool, and
    reaches no mailbox or provider: it is a read of eight local stores plus one pure
    function.
    """
    language = _language(language)
    brief, report = _brief(root, timezone=timezone, utc_offset_minutes=utc_offset_minutes,
                           language=language, profile=profile, now=now)
    try:
        state = daily_brief.feedback_state(state_dir=root, profile=profile)
    except (OSError, BriefError):
        state = {"feedback": {}, "briefs": []}
    hidden = _hidden(state.get("feedback"), brief["suppressed"])
    return {"schema": SCHEMA, "brief": brief, "sources": report,
            "email": _preview(brief, language),
            "feedback": {"hidden": hidden, "count": len(hidden)},
            "profile": profile,
            "unsuppressible": sorted(daily_brief.UNSUPPRESSIBLE_KINDS),
            "collected_at": max([row["collected_at"] for row in report] or [time.time()])}


def _hidden(feedback, suppressed):
    """Only rows actually hidden in this snapshot, with their current display names."""
    rows = []
    active = {row["id"]: row for row in suppressed}
    for item_id, entry in sorted((feedback or {}).items()):
        if not isinstance(entry, dict) or item_id not in active:
            continue
        rows.append({"item_id": str(item_id)[:120], "mode": str(entry.get("mode") or "")[:16],
                     "title": active[item_id]["title"],
                     "until": entry.get("until"), "at": entry.get("at"),
                     "note": str(entry.get("note") or "")[:120]})
    return rows[:daily_brief.FEEDBACK_LIMIT]


def _preview(brief, language):
    """The daily email, rendered from this snapshot.  Nothing is sent from here."""
    try:
        email = daily_brief.render_email(brief, bilingual=language == "zh")
    except BriefError:
        return {"available": False, "subject": "", "text": ""}
    return {"available": True, "subject": email["subject"], "text": email["text"],
            "sendable": False,
            "reason": "This is a preview. Morning email is controlled separately in Daily Brief settings."}


def _item_id(body):
    value = body.get("item_id") or body.get("item")
    if not isinstance(value, str) or not value.strip():
        raise BriefError("this action needs an item id")
    return value.strip()


def _minutes(body):
    value = body.get("minutes")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BriefError("snooze needs a number of minutes")
    minutes = int(value)
    if not 1 <= minutes <= SNOOZE_MAX_MINUTES:
        raise BriefError("snooze must be between 1 minute and 7 days")
    return minutes


def perform(root, body, *, profile="default", now=None):
    """Hide, snooze or restore one brief row -- or preview the email text.

    Nothing here sends, completes, cancels or acknowledges anything.  Hiding a row is a
    preference about *this brief only*: the task keeps its place in Missions, Needs You
    and the session list, it still counts in ``counts["attention_total"]``, and it comes
    back by itself as soon as its content changes.  Pending approvals cannot be hidden
    at all, and this refuses rather than pretending to.
    """
    if not isinstance(body, dict):
        raise BriefError("expected a JSON object")
    action = body.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise BriefError("unknown brief action")
    profile = str(body.get("profile") or profile)

    if action == "preview":
        language = _language(body.get("language"))
        brief, report = _brief(root, timezone=body.get("timezone", "UTC"),
                               utc_offset_minutes=body.get("utc_offset_minutes"),
                               language=language, profile=profile, now=now, remember=False)
        return {"ok": True, "action": action, "brief_id": brief["id"], "sources": report,
                "email": _preview(brief, language)}

    if action == "restore":
        result = daily_brief.restore(_item_id(body), state_dir=root, profile=profile, now=now)
        return {"ok": True, "action": action, **result}

    item_id = _item_id(body)
    fingerprint = body.get("fingerprint")
    fingerprint = fingerprint if isinstance(fingerprint, str) else ""
    if action == "dismiss":
        result = daily_brief.dismiss(item_id, state_dir=root, profile=profile,
                                     fingerprint=fingerprint, now=now)
    else:
        wall = float(now) if now is not None else time.time()
        result = daily_brief.snooze(item_id, wall + _minutes(body) * 60, state_dir=root,
                                    profile=profile, fingerprint=fingerprint, now=now)
    if result.get("refused"):
        # Visible refusal, not a silent no-op: the person pressed a button.
        return {"ok": False, "action": action, "item_id": result.get("item_id", ""),
                "error": result["refused"]}
    return {"ok": True, "action": action, **result}
