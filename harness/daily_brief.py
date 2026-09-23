"""One honest daily brief, built from named source payloads and nothing else.

Today already reads eight local endpoints and paints them into a dashboard.  That
renderer is the only thing that knows how to turn those payloads into a *day*, and it
lives in JavaScript inside one HTML file, so email and mobile cannot reuse a single
word of it.  Building a second brief somewhere else would be the real bug: two
renderers drift, and the moment they drift the quiet one starts lying.

So this module is the shared factual skeleton.  ``build()`` takes the payloads the
caller already fetched, keyed by source name, and returns one JSON-safe snapshot.
Every surface -- desktop Today, an opt-in email digest, a phone -- renders that same
snapshot.  No model is involved in producing it: a brief that says "3 things need you"
must be able to point at the three rows, and a sentence a model wrote cannot be
pointed at.

What this module refuses to do
------------------------------
* **Invent a day.**  Counts are ``len()`` of real entries.  A source that could not
  be read is ``unavailable`` and is never rendered as an empty, clear day; a source
  that was never configured is ``absent`` and produces a setup state, not tasks.
  There is no "No events today" row -- an empty agenda is an empty list plus a notice,
  because a placeholder row gets counted by the next person who writes a loop.
* **Obey its inputs.**  Titles, agendas, locations and mail-derived text are data.
  They are whitespace-collapsed, control-stripped, length-bounded here, and HTML
  escaped again in the renderers.  Nothing in a payload can become an instruction, a
  link target, or an action.
* **Act.**  Suggestions carry a prompt for a composer and ``requires_user_intent``.
  Nothing here sends, opens, cancels or completes anything, and there is no transport
  of any kind in this file.
* **Touch source stores.**  Payloads are read-only inputs and are never mutated.  The
  only thing this module ever writes is its own small feedback/seen file.

Messages, and what a message is allowed to do here
--------------------------------------------------
The ``communications`` payload is the local record of a connected inbox: messages that
already arrived and replies that were already drafted.  It is the one source whose rows
were written by *strangers*, so it is the most tightly bounded one:

* Only a **subject** (or a fixed, module-written fallback) is ever rendered.  No body,
  no sender or destination address, no attachment name and nothing about a credential
  is read out of the payload, and the collector that builds it does not put them there.
* Severity comes from the **stored state** and from nothing else.  "URGENT" in a
  subject line buys a message exactly as much rank as any other subject, because
  otherwise anyone who can reach the mailbox can rank themselves first in your morning.
* Messages the owner never allowed, and mail the transport marked automatic or bulk,
  are counted in a notice and given **no row**.  A rejected message is settled; it
  keeps its record in Communications and makes no claim on the day.
* ``submitted`` means the provider accepted the message.  That is not delivery, and
  the brief says so rather than reporting a reply as received.

Nothing in this module polls a mailbox, opens a connection, sends, retries, accepts or
approves anything: it reads rows that were already written and renders them.

Staleness, and why hiding is not completing
-------------------------------------------
The failure that makes a daily brief worthless is repeating yesterday's obligations
verbatim forever.  Two mechanisms answer it, and neither one silently drops work:

* ``seen`` bookkeeping gives every item ``first_seen_date``/``days_seen``.  An
  unchanged non-decision item older than ``STALE_DAYS`` is marked ``stale`` and loses
  its claim on the Top-3 slots -- it stays in the brief, it just stops crowding out
  what changed today.
* Optional user feedback (``dismiss`` / ``snooze``) suppresses an item *in the brief*
  while its content fingerprint is unchanged.  A changed status or changed evidence
  re-computes the fingerprint and the item comes back.  Approvals can never be
  suppressed at all (``UNSUPPRESSIBLE_KINDS``).

Hiding an item from the brief is not cancelling, completing, or acknowledging the
underlying task.  The task keeps its own row in Missions, Needs You and the thread
list, and the brief's own ``counts["attention_total"]`` still counts it.

Two identities
--------------
``brief["id"]`` names *this content*: it digests the headline, counts, coverage and
every rendered row's fingerprint, so two briefs that would read differently to a person
never share an id.  ``brief["reply"]["day_key"]`` names *the day* and does not move.
Sending is deduped on the second one; deduping on the first would mail the reader again
every time a mission changed state.

Timezones
---------
Day boundaries are local-midnight to next local-midnight, so a DST day is 23 or 25
hours long.  The greeting and ``timezone["utc_offset_minutes"]`` follow the reader's
current local time; each timed row additionally carries the offset in force at its own
instant, because one offset cannot describe a 23-hour day.  ``timezone`` accepts an IANA key or a ``tzinfo`` instance.  An invalid
key, or a valid key on a machine with no tzdata (a stock Windows CPython), does not
raise and does not silently pretend to be UTC: it falls back in a documented order
and sets ``timezone["degraded"]`` with a ``reason`` the renderers print.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import html
import json
import os
import re
import time
import urllib.parse

from . import statelock

SCHEMA = "collie.daily_brief/1"
STATE_SCHEMA = "collie.daily_brief.state/1"

#: Source name -> the local endpoint Today already fetches for it.  Callers that have
#: the payloads by other means (a phone talking to the same host, a test) may pass
#: anything they like; this mapping exists so the fetch list lives in one place.
PAYLOAD_ENDPOINTS = {
    "personal": "/api/personal",
    "missions": "/api/missions",
    "approvals": "/api/approvals",
    "procedures": "/api/procedures?limit=20",
    "runs": "/api/runs",
    "meetings": "/api/meetings/schedule",
    "task_inbox": "/api/task-inbox/pending",
    "communications": "/api/channels",
}
SOURCE_NAMES = tuple(PAYLOAD_ENDPOINTS)

TOP_LIMIT = 3
AGENDA_LIMIT = 12
LIST_LIMIT = 20
MAX_ROWS = 200
STALE_DAYS = 3
TITLE_LIMIT = 140
DETAIL_LIMIT = 200

#: A decision the app is blocking on is never hidden by a brief preference.
UNSUPPRESSIBLE_KINDS = frozenset({"approval"})

FEEDBACK_TTL_DAYS = 45
FEEDBACK_LIMIT = 500
SEEN_TTL_DAYS = 30
SEEN_LIMIT = 1000
BRIEF_LOG_LIMIT = 60

TERMINAL_STATUSES = frozenset({"done", "delivered", "cancelled", "canceled", "paid"})
_ATTENTION_MISSION_STATES = frozenset({
    "needs_you", "review_required", "recovery_required", "failed"})
_PROGRESS_MISSION_STATES = frozenset({"running", "waiting", "queued"})
_DONE_MISSION_STATES = frozenset({"done", "completed", "succeeded"})

#: ``/api/task-inbox/pending`` annotates each row with ``quota_resume.status``.  Only
#: these values mean the server will start the work by itself.  ``stalled``, ``stopped``
#: and ``unknown`` mean the opposite -- nothing starts this without a person -- so
#: reading "a wait exists" as "it is handled" is how stranded work leaves the brief and
#: the day then reads clear.
_CARRIED_WAIT_STATES = frozenset({"scheduled", "queued", "starting"})
#: How many connections, and how many rows inside one connection, a brief will look at.
#: A mailbox is unbounded and a brief is not; whatever falls outside these is reported
#: as partial coverage (see ``coverage["partial"]``) rather than quietly left out.
COMMS_CONNECTION_LIMIT = 16
COMMS_ROW_LIMIT = 60

#: Outgoing states that are still owed to a person.  ``unknown`` is deliberately here:
#: the store uses it for "we never learned whether this was sent", and it is never
#: retried by itself, so nobody finds out unless the brief says so.
_ATTENTION_RESULT_STATES = frozenset({"failed", "unknown"})
#: ``webapp._run_end`` publishes ``stop_reason`` beside ``state`` precisely because a
#: run stopped by an error or a turn/budget cap is terminal but *not* finished.  Listing
#: those under "Done today" is how a person ends up asking for the same work twice.
_UNFINISHED_STOP_REASONS = frozenset({"error", "turn_limit", "budget_limit"})

_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_ZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9+_-]*(?:/[A-Za-z0-9+._-]+){0,2}")
_ID_RE = re.compile(r"[a-z_]+:[0-9a-f]{16}")
#: Local UI routes a brief row may deep-link to.  First path segment only; anything
#: else (and every protocol-relative, traversing or scheme-bearing string) is dropped.
#: These are the paths ``webapp.Handler`` actually serves.  Missions, Needs You, Today,
#: sessions and the Library are *panes* of the single-page app at ``/``, not routes:
#: a row that linked to ``/missions`` produced a 404, so they are reached the way the
#: page itself reaches them, through ``/?mission=`` and ``/?session=``.
_LOCAL_ROUTES = frozenset({"", "meetings", "communications", "studio", "ambient", "live"})
_LOCAL_PATH_RE = re.compile(r"/[A-Za-z0-9._~/-]*(?:\?[A-Za-z0-9._~%=&+-]*)?")


def _deep_link(param="", value=""):
    """A link into the single-page app: ``/`` alone, or the pane the page understands."""
    native = _text(value, 80)
    if not param or not native:
        return "/"
    # `quote` leaves "." alone, and `safe_href` drops any link containing "..".  An id
    # with a dot in it would therefore silently lose its link, so encode those too.
    return "/?%s=%s" % (param, urllib.parse.quote(native, safe="").replace(".", "%2E"))


class BriefError(ValueError):
    """The caller asked for something this module cannot honestly produce."""


# ---------------------------------------------------------------- normalisation


def _text(value, limit=TITLE_LIMIT):
    """Collapse to one bounded, control-character-free line.  Never trusted after."""
    raw = value if isinstance(value, str) else ("" if value is None else str(value))
    clean = "".join(ch for ch in raw if ch == " " or ch == "\t" or ch >= " ")
    return " ".join(clean.split())[:max(0, int(limit))]


def _finite(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _stamp(value):
    """Seconds since the epoch, or ``None``.  Rejects 0, negatives and absurd years."""
    number = _finite(value, 0.0)
    if number <= 0 or number > 4102444800.0:  # 2100-01-01, well past any real row
        return None
    return number


def _rows(payload, key, limit=MAX_ROWS):
    """Bounded list-of-dicts out of an untrusted payload.  Missing/odd shapes -> []."""
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [row for row in value[:limit] if isinstance(row, dict)]


def _digest(*parts):
    return hashlib.sha256("\u001f".join(str(part) for part in parts).encode(
        "utf-8", "replace")).hexdigest()[:16]


def _item_id(kind, *parts):
    return "%s:%s" % (kind, _digest(kind, *parts))


def _fingerprint(*parts):
    """Content identity: the thing that decides whether a dismissal still applies."""
    return _digest("fp", *parts)


def safe_href(value, *, external=False):
    """Return a link a renderer may emit, or ``""``.

    Internal rows get local UI routes only.  ``external=True`` (news, which the caller
    supplies with its own sources) additionally allows a plain ``https`` URL with a
    hostname and no embedded credentials.  Everything else -- ``javascript:``,
    ``data:``, ``//host``, backslashes, traversal, control characters -- is dropped
    rather than escaped, because a link is an action target and not text.
    """
    if not isinstance(value, str):
        return ""
    raw = _text(value, 400)
    # A URL that changed under whitespace/control normalisation was never a clean URL,
    # and one that survived normalisation may still hold an interior space: `urlsplit`
    # happily reports a hostname for "https://exa mple.com".
    if not raw or raw != value or any(ch.isspace() for ch in raw):
        return ""
    if raw.startswith("//") or "\\" in raw or ".." in raw:
        return ""
    if raw.startswith("/"):
        if not _LOCAL_PATH_RE.fullmatch(raw):
            return ""
        segment = raw[1:].split("?", 1)[0].split("/", 1)[0]
        return raw if segment in _LOCAL_ROUTES else ""
    if not external:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return raw


# ---------------------------------------------------------------- timezone


def _fixed(minutes, name=None):
    delta = _dt.timedelta(minutes=int(minutes))
    return _dt.timezone(delta, name or ("UTC%+03d:%02d" % (
        int(minutes) // 60, abs(int(minutes)) % 60)))


def _resolve_zone(timezone, utc_offset_minutes, fallback):
    """(tzinfo, meta).  Never raises for a bad zone; degrades and says so."""
    meta = {"requested": timezone if isinstance(timezone, str) else _text(
        getattr(timezone, "key", "") or str(timezone or ""), 64),
        "resolved": "", "kind": "", "degraded": False, "reason": ""}
    if isinstance(timezone, _dt.tzinfo):
        meta.update(resolved=meta["requested"] or "provided", kind="provided")
        return timezone, meta
    key = _text(timezone, 64)
    if not key:
        meta.update(degraded=True, reason="no timezone supplied")
    elif not _ZONE_RE.fullmatch(key):
        meta.update(degraded=True, reason="invalid timezone name")
    else:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(key)
        except Exception as exc:                      # tzdata absent, or unknown key
            meta.update(degraded=True, reason=(
                "time zone database unavailable on this machine"
                if type(exc).__name__ == "ZoneInfoNotFoundError" and
                _looks_like_missing_tzdata() else "unknown timezone"))
        else:
            meta.update(resolved=key, kind="zoneinfo")
            return zone, meta
    if utc_offset_minutes is not None:
        minutes = _finite(utc_offset_minutes, 0.0)
        if -840 <= minutes <= 840:
            zone = _fixed(round(minutes))
            meta.update(resolved=str(zone), kind="fixed-offset")
            return zone, meta
        meta["reason"] += "; supplied utc offset out of range"
    if fallback == "system":
        zone = _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc
        meta.update(resolved=_text(zone.tzname(_dt.datetime.now()) or "system", 40),
                    kind="system")
        return zone, meta
    meta.update(resolved="UTC", kind="utc")
    return _dt.timezone.utc, meta


def _looks_like_missing_tzdata():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("UTC")
    except Exception:
        return True
    return False


def _day_window(now, zone):
    """Local midnight to the next local midnight.  23h and 25h days are correct."""
    local = _dt.datetime.fromtimestamp(now, zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start, nxt


# ---------------------------------------------------------------- feedback state


def _state_path(state_dir, profile):
    if not _PROFILE_RE.fullmatch(str(profile or "")):
        raise BriefError("invalid brief profile name")
    root = os.path.join(os.path.abspath(str(state_dir)), "daily-brief")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "%s.json" % profile)


def _blank_state():
    return {"schema": STATE_SCHEMA, "feedback": {}, "seen": {}, "briefs": []}


def _load_state(path):
    """Read the store.  A corrupt file is preserved beside itself, never overwritten."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return _blank_state(), ""
    except OSError as exc:
        return _blank_state(), "brief preferences could not be read (%s)" % type(exc).__name__
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        value = None
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        kept = "%s.corrupt-%d" % (path, int(time.time()))
        try:
            os.replace(path, kept)
        except OSError:
            pass
        return _blank_state(), "unreadable brief preferences were set aside; none were applied"
    state = _blank_state()
    for key in ("feedback", "seen"):
        section = value.get(key)
        if isinstance(section, dict):
            state[key] = {str(name)[:120]: dict(entry) for name, entry in
                          list(section.items())[:max(FEEDBACK_LIMIT, SEEN_LIMIT)]
                          if isinstance(entry, dict)}
    log = value.get("briefs")
    if isinstance(log, list):
        state["briefs"] = [dict(row) for row in log[-BRIEF_LOG_LIMIT:] if isinstance(row, dict)]
    return state, ""


def _prune(state, now):
    feedback = {}
    for name, entry in state["feedback"].items():
        mode = str(entry.get("mode") or "")
        until = _stamp(entry.get("until"))
        at = _stamp(entry.get("at")) or now
        if mode == "snooze" and (until is None or until < now - 86400):
            continue
        if now - at > FEEDBACK_TTL_DAYS * 86400:
            continue
        feedback[name] = entry
    state["feedback"] = dict(sorted(
        feedback.items(), key=lambda kv: _stamp(kv[1].get("at")) or 0.0)[-FEEDBACK_LIMIT:])
    seen = {name: entry for name, entry in state["seen"].items()
            if now - (_stamp(entry.get("last_at")) or now) <= SEEN_TTL_DAYS * 86400}
    state["seen"] = dict(sorted(
        seen.items(), key=lambda kv: _stamp(kv[1].get("last_at")) or 0.0)[-SEEN_LIMIT:])
    state["briefs"] = state["briefs"][-BRIEF_LOG_LIMIT:]
    return state


def _write_state(path, state):
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _edit_state(state_dir, profile, mutate, *, now=None):
    """Read-modify-write the profile store under the cross-process state lock."""
    path = _state_path(state_dir, profile)
    wall = _finite(now, time.time()) if now is not None else time.time()
    with statelock.transaction(path):
        state, notice = _load_state(path)
        result = mutate(state)
        _write_state(path, _prune(state, wall))
    return result, notice


def dismiss(item_id, *, state_dir, profile="default", fingerprint="", note="", now=None):
    """Hide one item from future briefs while its content is unchanged.

    This is a brief-rendering preference and nothing else.  It does not cancel,
    complete or acknowledge the underlying task, it does not touch the store the item
    came from, and it cannot hide a pending approval (see ``UNSUPPRESSIBLE_KINDS``);
    Needs You, Missions and the thread list are unaffected either way.
    """
    return _feedback(item_id, "dismiss", None, state_dir, profile, fingerprint, note, now)


def snooze(item_id, until, *, state_dir, profile="default", fingerprint="", note="",
           now=None):
    """Hide one item until ``until`` (epoch seconds), then let it come back."""
    deadline = _stamp(until)
    if deadline is None:
        raise BriefError("snooze needs an absolute future timestamp")
    return _feedback(item_id, "snooze", deadline, state_dir, profile, fingerprint, note, now)


def restore(item_id, *, state_dir, profile="default", now=None):
    """Drop any suppression for ``item_id``."""
    key = _text(item_id, 120)

    def mutate(state):
        return {"ok": bool(state["feedback"].pop(key, None)), "item_id": key}

    return _edit_state(state_dir, profile, mutate, now=now)[0]


def _feedback(item_id, mode, until, state_dir, profile, fingerprint, note, now):
    key = _text(item_id, 120)
    if not _ID_RE.fullmatch(key):
        raise BriefError("invalid brief item id")
    if key.split(":", 1)[0] in UNSUPPRESSIBLE_KINDS:
        return {"ok": False, "item_id": key, "refused": "approvals cannot be hidden"}
    wall = _finite(now, time.time()) if now is not None else time.time()

    def mutate(state):
        state["feedback"][key] = {"mode": mode, "until": until, "at": wall,
                                  "fingerprint": _text(fingerprint, 40),
                                  "note": _text(note, 120)}
        return {"ok": True, "item_id": key, "mode": mode, "until": until}

    return _edit_state(state_dir, profile, mutate, now=wall)[0]


def feedback_state(*, state_dir, profile="default"):
    """Current suppressions, for a settings screen.  Read-only."""
    state, _ = _load_state(_state_path(state_dir, profile))
    return {"feedback": state["feedback"], "briefs": state["briefs"]}


def _suppressed(entry, item, now, day_start):
    """Does a stored preference still apply to this item, as it is right now?"""
    if not entry:
        return ""
    stored = _text(entry.get("fingerprint"), 40)
    if stored and stored != item["fingerprint"]:
        return ""                     # status or evidence moved: let it resurface
    mode = str(entry.get("mode") or "")
    if mode == "snooze":
        until = _stamp(entry.get("until"))
        return "snooze" if until and until > now else ""
    if mode != "dismiss":
        return ""
    if not stored and (_stamp(entry.get("at")) or 0.0) < day_start:
        # A client that sent no fingerprint recorded nothing that can be re-checked when
        # the item changes, so the promise "changed work comes back" cannot be kept for
        # it.  An unanchored dismissal therefore lasts for its own local day and no
        # longer -- never the 45-day TTL, which would hide a mission that has since
        # failed.  (A snooze is already bounded by its own deadline.)
        return ""
    return "dismiss"


# ---------------------------------------------------------------- item building


def _item(kind, *, native, title, detail="", when=None, status="", tone="attention",
          source="", href="", severity=5, evidence=None, dedupe="", fallback="",
          generated=()):
    """One row.  ``generated`` names the fields whose words this module wrote itself.

    That distinction is the whole of the localisation rule.  A title that came out of a
    payload is the source's own sentence -- a person's subject line, a mission they
    named -- and is rendered exactly as it was stored, in whatever language it was
    written.  Only the strings this file authored ("Coming up soon.", "Ongoing task")
    may be swapped for another language, so ``title`` is marked generated only when the
    payload had nothing and ``fallback`` (or the bare kind) supplied the words instead.
    """
    authored = {name for name in generated if name in ("title", "detail")}
    title = _text(title)
    if not title:
        if _text(fallback):
            title, authored = _text(fallback), authored | {"title"}
        elif _text(detail):
            title = _text(detail)
            if "detail" in authored:
                authored = authored | {"title"}
        else:
            title, authored = kind.replace("_", " "), authored | {"title"}
    item = {
        "id": _item_id(kind, source, native),
        "kind": kind,
        "title": title,
        "detail": _text(detail, DETAIL_LIMIT),
        "when": when,
        "status": _text(status, 40),
        "tone": tone,
        "severity": int(severity),
        "source": source,
        "source_ref": {"label": _text(native, 80), "href": safe_href(href)},
        "evidence": {k: _text(v, 80) for k, v in sorted((evidence or {}).items())},
        "dedupe_key": _text(dedupe, 120) or "",
        "generated": sorted(authored),
    }
    item["fingerprint"] = _fingerprint(
        item["id"], item["status"], "" if when is None else int(when),
        item["title"], item["detail"], json.dumps(item["evidence"], sort_keys=True))
    return item


def _collect(sources, window):
    """``(items, personal, communications report)``, from the payloads only.

    No row is ever synthesised: every entry below is something a store already wrote.
    """
    day_start, day_end = window
    personal = sources["personal"]["payload"]
    missions = sources["missions"]["payload"]
    approvals = sources["approvals"]["payload"]
    procedures = sources["procedures"]["payload"]
    runs = sources["runs"]["payload"]
    meetings = sources["meetings"]["payload"]
    inbox = sources["task_inbox"]["payload"]
    out = []

    for row in _rows(approvals, "approvals"):
        native = _text(row.get("id"), 80) or _text(row.get("tool"), 80)
        out.append(_item("approval", native=native, source="approvals", severity=0,
                         title=_text(row.get("title")) or _text(row.get("tool")),
                         fallback="A decision is waiting", generated=("detail",),
                         detail="Collie is paused until you answer.",
                         status="pending", href=_deep_link("session", row.get("session")),
                         evidence={"tool": row.get("tool", ""),
                                   "session": row.get("session", "")}))

    for row in _rows(personal, "reminders"):
        state = _text(row.get("state"), 32)
        if state == "upcoming":
            continue                      # a dashboard state, not a thing to raise today
        severity = {"overdue": 1, "possibly_delayed": 2, "soon": 3}.get(state, 4)
        out.append(_item("reminder", native=_text(row.get("event_id"), 80),
                         source="personal", severity=severity,
                         title=row.get("title"), when=_stamp(row.get("at")),
                         status=state, href="/", generated=("detail",),
                         dedupe="personal:" + _text(row.get("event_id"), 80)
                         if row.get("event_id") else "",
                         detail={"overdue": "Past its expected time.",
                                 "possibly_delayed": "May be delayed.",
                                 "soon": "Coming up soon."}.get(state, ""),
                         evidence={"event_type": row.get("event_type", ""),
                                   "status": row.get("status", ""),
                                   "authority": row.get("authority_scope", "notify_only")}))

    for row in _rows(missions, "missions"):
        state = _text(row.get("state"), 32)
        native = _text(row.get("mission_id"), 80)
        link = _deep_link("mission", native)
        if state in _ATTENTION_MISSION_STATES:
            out.append(_item("mission", native=native, source="missions", severity=1,
                             title=row.get("title"), status=state, href=link,
                             detail=row.get("current") or "", dedupe="mission:" + native,
                             when=_stamp(row.get("updated_at")),
                             evidence={"next": row.get("next", "")}))
        elif state in _PROGRESS_MISSION_STATES:
            out.append(_item("mission", native=native, source="missions", severity=7,
                             tone="progress", title=row.get("title"), status=state,
                             href=link, detail=row.get("current") or "",
                             dedupe="mission:" + native,
                             when=_stamp(row.get("updated_at"))))
        elif state in _DONE_MISSION_STATES:
            when = _stamp(row.get("updated_at"))
            if when is not None and day_start <= when < day_end:
                out.append(_item("mission", native=native, source="missions", severity=9,
                                 tone="done", title=row.get("title"), status=state,
                                 href=link, when=when, dedupe="mission:" + native))

    scheduler = missions.get("scheduler") if isinstance(missions, dict) else None
    if isinstance(scheduler, dict) and scheduler.get("blocked"):
        # The reason is the scheduler's own words; only the fallback is ours.
        reason = _text(scheduler.get("reason") or scheduler.get("detail"), DETAIL_LIMIT)
        out.append(_item("scheduler", native="mission-scheduler", source="missions",
                         severity=1, title="Automatic task progress is blocked",
                         status="blocked", href="/",
                         generated=("title",) if reason else ("title", "detail"),
                         detail=reason or "Saved tasks are kept."))

    for row in _rows(inbox, "sessions"):
        if row.get("owner_busy") is True:
            continue                       # the server is carrying it; not a demand
        session = _text(row.get("session"), 80)
        # A scheduled wait is only evidence that the server is carrying this if the wait
        # itself says so.  `stalled`, `stopped` and `unknown` are the opposite claim.
        wait = row.get("scheduled_wait") if isinstance(
            row.get("scheduled_wait"), dict) else {}
        progress = _text(wait.get("progress"), 32) if wait else ""
        carried = progress in _CARRIED_WAIT_STATES
        stranded = bool(wait) and not carried
        needs_you = (bool(row.get("needs_you") or row.get("blocked") or
                          row.get("error")) or stranded) and not carried
        out.append(_item("queued", native=session, source="task_inbox",
                         severity=2 if needs_you else 8,
                         tone="attention" if needs_you else "progress",
                         title=row.get("title"), fallback="Ongoing task",
                         status="needs_you" if needs_you else "queued",
                         href=_deep_link("session", session), dedupe="session:" + session,
                         detail=_text(row.get("error"), DETAIL_LIMIT) or (
                             _text(wait.get("reason"), DETAIL_LIMIT) if stranded else ""),
                         evidence={"pending": row.get("pending", ""), "wait": progress}))

    for row in _rows(runs, "runs"):
        state = _text(row.get("state"), 32)
        session = _text(row.get("session"), 80)
        link = _deep_link("session", session)
        if state in ("running", "canceling"):
            out.append(_item("run", native=session, source="runs", severity=8,
                             tone="progress", title=row.get("ask"),
                             fallback="Collie is working",
                             status=state, href=link, dedupe="session:" + session))
            continue
        ended = _stamp(row.get("ended"))
        if ended is None or not (day_start <= ended < day_end):
            continue
        # Terminal is not the same as finished.  A failed run, or one cut off by a turn
        # or budget cap, still owes an answer. Cancelled work is neither a new
        # failure nor a completed task to celebrate.
        stop_reason = _text(row.get("stop_reason"), 32)
        if state in {"canceled", "cancelled"} or stop_reason in {"canceled", "cancelled"}:
            continue
        unfinished = state == "failed" or stop_reason in _UNFINISHED_STOP_REASONS
        out.append(_item("run", native=session, source="runs",
                         severity=2 if unfinished else 9,
                         tone="attention" if unfinished else "done",
                         title=row.get("ask"),
                         fallback="A task stopped before finishing" if unfinished
                         else "A task finished",
                         status=state or "done", when=ended, href=link,
                         dedupe="session:" + session,
                         detail=_text(row.get("error"), DETAIL_LIMIT) if unfinished else "",
                         evidence={"verified": row.get("verified", ""),
                                   "stop_reason": row.get("stop_reason", "")}))

    # --- the agenda: only rows that carry a real timestamp inside the local day ---
    for row in _rows(meetings, "events"):
        start = _stamp(row.get("start_at"))
        end = _stamp(row.get("end_at")) or start
        if start is None or end is None or end < day_start or start >= day_end:
            continue
        sensitive = bool(row.get("sensitive"))
        out.append(_item("meeting", native=_text(row.get("id"), 80), source="meetings",
                         tone="agenda", severity=4, when=start, status="scheduled",
                         title="Private event" if sensitive else row.get("title"),
                         fallback="Scheduled meeting",
                         generated=("title",) if sensitive else (),
                         detail="" if sensitive else _text(row.get("location"), 80),
                         href="/meetings",
                         dedupe="when:%d|%s" % (int(start // 60),
                                                _text(row.get("title"), 60).casefold()),
                         evidence={"source": row.get("source", ""),
                                   "all_day": row.get("all_day", "")}))
        if sensitive:
            out[-1]["sensitive"] = True

    reminded = {_text(row.get("event_id"), 80) for row in _rows(personal, "reminders")
                if row.get("event_id") and row.get("state") != "upcoming"}
    for row in _rows(personal, "events"):
        status = _text(row.get("status"), 32)
        when = _stamp(row.get("expected_at")) or _stamp(row.get("due_at")) or \
            _stamp(row.get("starts_at"))
        if when is None:
            continue
        native = _text(row.get("event_id"), 80)
        if native and native in reminded:
            continue  # One obligation: hiding its reminder must also hide the agenda copy.
        if status in {"canceled", "cancelled"}:
            continue
        if status in TERMINAL_STATUSES:
            # A due date is not evidence of when something was finished. Older
            # paid/delivered notices must not turn into today's achievements.
            completed_at = _stamp(row.get("completed_at"))
            if completed_at is not None and day_start <= completed_at < day_end:
                out.append(_item("event", native=native, source="personal", tone="done",
                                 severity=9, title=row.get("title"), status=status,
                                 when=completed_at, href="/"))
            continue
        if not (day_start <= when < day_end):
            continue
        out.append(_item("event", native=native, source="personal", tone="agenda",
                         severity=5, title=row.get("title"), status=status or "scheduled",
                         when=when, href="/",
                         dedupe="when:%d|%s" % (int(when // 60),
                                                _text(row.get("title"), 60).casefold()),
                         evidence={"event_type": row.get("event_type", ""),
                                   "confidence": row.get("confidence", "")}))

    for row in _rows(procedures, "candidates"):
        if _text(row.get("status"), 32) != "proposed":
            continue
        out.append(_item("procedure", native=_text(row.get("candidate_id") or row.get("id"), 80),
                         source="procedures", tone="progress", severity=10,
                         title=row.get("title"), fallback="A routine worth reviewing",
                         detail=row.get("summary") or "", status="proposed",
                         href="/"))
    messages, comms_report = _communication_items(
        sources["communications"]["payload"], window)
    out += messages
    # The connected-source list is read from /api/personal and only from there.  The
    # personal snapshot nested inside /api/procedures is the same data by a longer
    # route; trusting two paths to it is how the two start disagreeing.
    return out, personal if isinstance(personal, dict) else {}, comms_report


def _communication_items(payload, window):
    """``(items, report)`` for messages already received and replies already written.

    Every row here is a *stored local record*.  Nothing in this function knows how to
    reach a provider, and no field it reads can make it reach one.

    The four facts it is willing to state, and the reason each one is a row:

    * a **pending message from a sender the owner allowed** is work waiting on a person,
      and nothing moves it without them -- ``ready_for_review``;
    * a **pending reply** is a draft the app already wrote and will not send by itself --
      ``ready_to_send``, and it says so when the connection is paused;
    * a **failed** or **unknown** send is the quiet failure this whole surface exists to
      catch: the store never retries ``unknown`` by itself, so nobody learns otherwise;
    * an **accepted** message became a task, which Missions/Needs You/the session list
      already show -- so it is filed under the same ``session:`` dedupe key at the lowest
      rank and collapses into that row instead of doubling it.

    Everything else is deliberately quiet.  A rejected message is settled, and mail the
    transport marked automatic, bulk or from a sender outside the allow list is counted
    in ``report["quiet"]`` and given no row at all: an unknown stranger cannot put a
    line in somebody's morning just by sending it.
    """
    day_start, day_end = window
    out = []
    report = {"quiet": 0, "partial": 0, "unreadable": 0, "connections": 0}
    supplied = payload.get("connections") if isinstance(payload, dict) else None
    if (isinstance(payload, dict) and payload.get("truncated")) or (
            isinstance(supplied, list) and len(supplied) > COMMS_CONNECTION_LIMIT):
        report["partial"] += 1               # more connections than were looked at
    for conn in _rows(payload, "connections", COMMS_CONNECTION_LIMIT):
        cid = _text(conn.get("id"), 80)
        report["connections"] += 1
        if conn.get("unreadable") or conn.get("error"):
            # Unreadable is never empty.  No row can be made from a store that would
            # not open, and the count becomes partial coverage rather than a clear day.
            report["unreadable"] += 1
            continue
        events = _rows(conn, "events", COMMS_ROW_LIMIT)
        results = _rows(conn, "results", COMMS_ROW_LIMIT)
        if conn.get("truncated") or len(conn.get("events") or []) > COMMS_ROW_LIMIT \
                or len(conn.get("results") or []) > COMMS_ROW_LIMIT:
            report["partial"] += 1
        # Pausing a connection stops new mail arriving.  It does not undo the messages
        # and drafts already stored, and hiding those would be the actual dishonesty.
        paused = conn.get("enabled") is False

        for row in events:
            native = "%s|%s" % (cid, _text(row.get("id"), 200))
            state = _text(row.get("state"), 32)
            when = _stamp(row.get("received")) or _stamp(row.get("recorded"))
            acceptance = row.get("acceptance") if isinstance(
                row.get("acceptance"), dict) else {}
            session = _text(acceptance.get("session"), 80)
            if state == "accepted":
                out.append(_item("message", native=native, source="communications",
                                 tone="progress", severity=9, title=row.get("subject"),
                                 fallback="A message Collie is working on",
                                 detail="Collie is working on this message.",
                                 generated=("detail",), status="accepted", when=when,
                                 href="/communications",
                                 dedupe="session:" + session if session else "",
                                 evidence={"connection": cid}))
                continue
            if state != "pending":
                continue
            if not row.get("sender_allowed") or row.get("automatic"):
                report["quiet"] += 1
                continue
            # Severity is this state and nothing else.  A subject line cannot buy rank:
            # if it could, the ranking would belong to whoever sends the most mail.
            out.append(_item("message", native=native, source="communications",
                             severity=4, title=row.get("subject"),
                             fallback="A message is waiting for you",
                             detail="Waiting for you to review it in Communications.",
                             generated=("detail",), status="ready_for_review", when=when,
                             href="/communications", evidence={"connection": cid}))

        for row in results:
            native = "%s|%s" % (cid, _text(row.get("id"), 200))
            state = _text(row.get("state"), 32)
            when = _stamp(row.get("updated")) or _stamp(row.get("created"))
            session = _text(row.get("session"), 80)
            common = {"native": native, "source": "communications", "when": when,
                      "title": row.get("subject"), "href": "/communications",
                      "generated": ("detail",), "evidence": {"connection": cid}}
            if state == "pending":
                out.append(_item("reply", severity=3, status="ready_to_send",
                                 fallback="A reply is waiting to be sent",
                                 detail="Ready to send. This connection is paused, so "
                                 "nothing will go out until you resume it." if paused
                                 else "Ready to send from Communications.",
                                 # An answer that still has to go out is the live half of
                                 # this exchange; the message it replies to has been dealt
                                 # with.  Same key, better rank: the draft is what shows.
                                 dedupe="session:" + session if session else "", **common))
            elif state in _ATTENTION_RESULT_STATES:
                out.append(_item("reply", severity=2, status=state,
                                 fallback="A reply could not be sent" if state == "failed"
                                 else "A reply may not have been sent",
                                 detail="It could not be sent. Open Communications to "
                                 "retry or edit it." if state == "failed" else
                                 "It is not known whether this was sent. It will not be "
                                 "retried by itself.", **common))
            elif state == "sending":
                out.append(_item("reply", severity=8, tone="progress", status=state,
                                 fallback="A reply is being sent",
                                 detail="Being sent now.", **common))
            elif state == "submitted" and when is not None and day_start <= when < day_end:
                # Handed over is not delivered, and this is the only place a person
                # would otherwise read "sent" and stop wondering.
                out.append(_item("reply", severity=9, tone="done", status=state,
                                 fallback="A reply was handed to the provider",
                                 detail="The service accepted it. Delivery is not "
                                 "confirmed.", **common))
    return out, report


def _news_items(news):
    kept, dropped = [], 0
    for row in (news or [])[:MAX_ROWS]:
        if not isinstance(row, dict):
            dropped += 1
            continue
        href = safe_href(row.get("url"), external=True)
        source = _text(row.get("source"), 60)
        title = _text(row.get("title"))
        if not (href and source and title):
            dropped += 1                  # unsourced or unlinkable: not publishable
            continue
        item = _item("news", native=href, source="news", tone="news", severity=12,
                     title=title, detail=row.get("summary") or "",
                     when=_stamp(row.get("published_at")),
                     evidence={"publisher": source})
        item["source_ref"] = {"label": source, "href": href}
        kept.append(item)
    return kept[:LIST_LIMIT], dropped


# ---------------------------------------------------------------- build


def build(payloads, *, now=None, timezone="UTC", language="en", state_dir=None,
          profile="default", remember=True, news=None, top_limit=TOP_LIMIT,
          utc_offset_minutes=None, fallback_timezone="utc"):
    """Return one deterministic, evidence-backed brief snapshot.

    ``payloads`` is a mapping of source name -> the JSON body that source returned.
    Recognised names are :data:`SOURCE_NAMES`; :data:`PAYLOAD_ENDPOINTS` says where
    each one comes from.  A value may be

    * a payload dict                      -> ``ok``
    * ``{"__unavailable": True, ...}`` or a dict carrying ``error``, or any non-dict
                                          -> ``unavailable`` (never rendered as clear)
    * missing or ``None``                 -> ``absent`` (never configured)

    ``now`` is epoch seconds (default: wall clock).  ``timezone`` is an IANA key or a
    ``tzinfo``; see the module docstring for the degradation rules, and pass
    ``utc_offset_minutes`` (what a browser or phone actually knows) as the preferred
    fallback.  ``state_dir`` enables the optional feedback/seen store; without it the
    brief is a pure function of its inputs and nothing is written anywhere.

    The result is JSON-safe and is the only input the renderers take.
    """
    wall = _finite(now, time.time()) if now is not None else time.time()
    zone, tzmeta = _resolve_zone(timezone, utc_offset_minutes, str(fallback_timezone or "utc"))
    start_local, end_local = _day_window(wall, zone)
    # The greeting is about the moment the brief is read; `start_local` is local
    # *midnight*, whose hour is 0 on every day there has ever been, so greeting off it
    # wished every reader a good evening -- at 07:00, in a morning summary.
    now_local = _dt.datetime.fromtimestamp(wall, zone)
    day_start, day_end = start_local.timestamp(), end_local.timestamp()
    tzmeta.update(day_start=day_start, day_end=day_end,
                  day_hours=round((day_end - day_start) / 3600.0, 2),
                  utc_offset_minutes=int((now_local.utcoffset() or
                                          _dt.timedelta()).total_seconds() // 60))
    language = _text(language, 16).lower() or "en"
    top_limit = max(1, min(int(top_limit), 10))
    date_key = start_local.strftime("%Y-%m-%d")

    supplied = payloads if isinstance(payloads, dict) else {}
    sources, notices = {}, []
    for name in SOURCE_NAMES:
        payload = supplied.get(name)
        if payload is None:
            state, detail = "absent", "not configured"
        elif not isinstance(payload, dict):
            state, detail = "unavailable", "malformed payload"
        elif payload.get("__unavailable") or payload.get("error"):
            state, detail = "unavailable", _text(payload.get("error"), 120) or "unavailable"
        else:
            state, detail = "ok", ""
        sources[name] = {"name": name, "state": state, "detail": detail,
                         "endpoint": PAYLOAD_ENDPOINTS[name],
                         "payload": payload if isinstance(payload, dict) else {}}
    unknown = sorted(set(supplied) - set(SOURCE_NAMES) - {"news"})
    if unknown:
        notices.append("Ignored unknown source payloads: %s." % ", ".join(unknown[:5]))

    items, personal_block, comms_report = _collect(sources, (day_start, day_end))
    news_items, news_dropped = _news_items(news)
    if news_dropped:
        notices.append("%d news item(s) were dropped for missing a source or a safe link."
                       % news_dropped)

    # De-duplicate: one underlying thing produces one row, highest severity wins.
    by_key, ordered = {}, []
    for item in sorted(items, key=lambda row: (row["severity"], row["id"])):
        key = item["dedupe_key"] or item["id"]
        if key in by_key:
            keep = by_key[key]
            keep["evidence"].setdefault("also_in", item["source"])
            continue
        by_key[key] = item
        ordered.append(item)

    # Seen bookkeeping and user suppression.
    state_notice = ""
    store = {"feedback": {}, "seen": {}}
    if state_dir is not None:
        store, state_notice = _load_state(_state_path(state_dir, profile))
        if state_notice:
            notices.append(state_notice)
    # One offset cannot describe a DST day: on a spring-forward day the offset at local
    # midnight is an hour off every afternoon row, so a 14:00 meeting rendered as 13:00.
    # Each timed row therefore carries the offset in force at *its own* instant.
    for item in ordered + news_items:
        item["when_offset_minutes"] = tzmeta["utc_offset_minutes"] \
            if item.get("when") is None else int((
                _dt.datetime.fromtimestamp(float(item["when"]), zone).utcoffset() or
                _dt.timedelta()).total_seconds() // 60)

    suppressed = []
    for item in ordered:
        seen = store["seen"].get(item["id"]) if isinstance(store["seen"], dict) else None
        first_date = _text((seen or {}).get("first_date"), 10) or date_key
        changed = bool(seen) and _text((seen or {}).get("fingerprint"), 40) != item["fingerprint"]
        days = int(_finite((seen or {}).get("days"), 0))
        if seen and _text((seen or {}).get("last_date"), 10) != date_key:
            days += 1
        item["first_seen_date"] = first_date
        item["days_seen"] = max(1, days if seen else 1)
        item["changed_since_last_brief"] = changed
        item["stale"] = bool(item["days_seen"] >= STALE_DAYS and not changed and
                             item["kind"] not in UNSUPPRESSIBLE_KINDS)
        hidden = _suppressed(store["feedback"].get(item["id"]), item, wall, day_start) \
            if item["kind"] not in UNSUPPRESSIBLE_KINDS else ""
        item["suppressed"] = hidden
        if hidden:
            suppressed.append(item)
    visible = [item for item in ordered if not item["suppressed"]]

    attention = [row for row in visible if row["tone"] == "attention"]
    agenda = sorted([row for row in visible if row["tone"] == "agenda"],
                    key=lambda row: (row["when"] or day_start, row["title"]))[:AGENDA_LIMIT]
    progress = [row for row in visible if row["tone"] == "progress"][:LIST_LIMIT]
    completed = sorted([row for row in visible if row["tone"] == "done"],
                       key=lambda row: (row["when"] or day_start, row["title"]))[:LIST_LIMIT]
    attention.sort(key=lambda row: (row["stale"], row["severity"],
                                    row["when"] or day_end, row["title"]))
    top = attention[:top_limit]

    ok_names = [name for name, row in sources.items() if row["state"] == "ok"]
    unavailable = [name for name, row in sources.items() if row["state"] == "unavailable"]
    absent = [name for name, row in sources.items() if row["state"] == "absent"]
    complete = not unavailable
    # Read, but not all of it.  A mailbox is unbounded and a brief is not, so a
    # connection with more rows than the collector reads -- or one whose store would not
    # open at all -- leaves a hole this brief knows about.  Saying "partly read" is the
    # only honest option: "unavailable" would throw away the rows that *were* read, and
    # silence would let a day with an unread pending message report as clear.
    partial = ["communications"] if (comms_report["partial"] or
                                     comms_report["unreadable"]) else []
    configured = bool(ok_names)
    connected = [row for row in _rows(personal_block, "sources")
                 if row.get("enabled") and _text(row.get("permission_state"), 32) == "granted"]
    has_calendar = bool([row for row in _rows(sources["meetings"]["payload"], "events")]) or \
        any(_text(row.get("kind"), 32) == "calendar" for row in connected)

    if unavailable:
        notices.append("Could not read: %s. Nothing missing has been assumed clear."
                       % ", ".join(sorted(unavailable)))
    # An empty agenda is only honest if something could have filled it.  The old guard
    # required the meetings source to be `ok`, so the commonest case of all -- a profile
    # where the calendar endpoint was never wired up at all -- produced a silent empty
    # day.  The one state that must stay quiet here is `unavailable`: "could not be read"
    # is already said above, and "not connected" would be a second, wrong explanation.
    if not agenda and not has_calendar and sources["meetings"]["state"] != "unavailable":
        notices.append("No calendar is connected, so the agenda is not a claim about your day.")
    if suppressed:
        notices.append("%d item(s) are hidden by your own brief preferences. Hiding an item "
                       "does not cancel or complete it." % len(suppressed))
    for key in ("unreadable", "partial", "quiet"):
        if comms_report[key]:
            notices.append(_COMMS_NOTICES[key][
                1 if language.startswith("zh") else 0] % comms_report[key])

    counts = {"attention": len(attention), "attention_total": len(
        [row for row in ordered if row["tone"] == "attention"]),
        "agenda": len(agenda), "completed": len(completed), "progress": len(progress),
        "news": len(news_items), "suppressed": len(suppressed),
        "sources_ok": len(ok_names), "sources_unavailable": len(unavailable),
        "messages_quiet": comms_report["quiet"],
        "connections": comms_report["connections"]}
    # Partial coverage cannot be a clear day either: the rows that were not read are
    # exactly the ones nobody has looked at.
    all_clear = bool(complete and configured and not attention and not partial)

    setup = None
    if not configured:
        # "Nothing is connected" and "everything we asked failed" are opposite facts and
        # need opposite words.  A fresh profile whose endpoints all answered with empty
        # bodies is `ok`, not `absent`, and never reaches here at all.
        setup = {"reason": "no source responded" if unavailable else
                 "no sources are configured",
                 "steps": [{"id": "connect-calendar", "title": "Connect a calendar",
                            "href": "/library"},
                           {"id": "review-sources", "title": "Review connected sources",
                            "href": "/library"}],
                 "absent": sorted(absent), "unavailable": sorted(unavailable)}

    brief = {
        "schema": SCHEMA,
        "id": "",
        "date": date_key,
        "generated_at": wall,
        "language": language,
        "profile": _text(profile, 64),
        "timezone": tzmeta,
        "greeting": _greeting(now_local.hour),
        "headline": "",
        "all_clear": all_clear,
        "sources": [{k: v for k, v in row.items() if k != "payload"}
                    for row in sources.values()],
        "coverage": {"ok": sorted(ok_names), "unavailable": sorted(unavailable),
                     "absent": sorted(absent), "complete": complete,
                     "partial": partial, "configured": configured},
        "top": top,
        "attention": attention,
        "agenda": agenda,
        "progress": progress,
        "completed": completed,
        "news": news_items,
        "news_enabled": news is not None,
        "suppressed": [{"id": row["id"], "mode": row["suppressed"], "kind": row["kind"],
                        "title": row["title"]}
                       for row in suppressed],
        "setup": setup,
        "counts": counts,
        "notices": notices,
        "suggestions": [],
    }
    brief["headline"] = _headline(brief)
    if language.startswith("zh"):
        brief["greeting"] = {"Good morning.": "早上好。", "Good afternoon.": "下午好。",
                             "Good evening.": "晚上好。"}[brief["greeting"]]
        translated = {"No calendar is connected, so the agenda is not a claim about your day.":
                      "尚未连接日历，因此空白日程不代表你今天没有安排。"}
        brief["notices"] = [translated.get(note, note) for note in brief["notices"]]
        # Only the words this module wrote.  A subject line, a mission title and an
        # error a store reported are the source's own text and stay exactly as stored,
        # which is also why this runs after every fingerprint and sort key is fixed:
        # hiding an item must survive the reader switching languages.
        _localize(ordered + news_items, language)
        for hidden, item in zip(brief["suppressed"], suppressed):
            hidden["title"] = item["title"]
    brief["suggestions"] = _suggestions(brief, has_calendar)
    if language.startswith("zh"):
        titles = {
            "Review what Collie is waiting on": "看看哪些决定需要你",
            "Check what slipped": "核对可能逾期的事项",
            "Retry the sources that failed": "检查未能读取的来源",
            "Plan around today's schedule": "围绕今天的日程安排工作",
            "Connect a calendar": "连接日历",
            "Deal with what keeps coming back": "处理反复出现的事项",
            "Keep today realistic": "选一件今天值得做的事",
        }
        for suggestion in brief["suggestions"]:
            suggestion["title"] = titles.get(suggestion["title"], suggestion["title"])
    # The id names *this content*, not "the brief for this date".  Digesting only the
    # date and the item ids meant a mission going needs_you -> failed, an approval
    # arriving, a source falling over or a row being hidden all produced the same id --
    # so the history log deduped two different briefs into one, and two different emails
    # would have carried the same `X-Collie-Brief-Id`.  Every rendered section, the
    # sentence above them and the coverage they were computed under go into it.
    brief["id"] = "brief-%s-%s" % (date_key, _digest(
        SCHEMA, date_key, brief["profile"], language, tzmeta["resolved"], brief["greeting"], brief["headline"],
        json.dumps(counts, sort_keys=True), json.dumps(brief["coverage"], sort_keys=True),
        json.dumps(brief["setup"], sort_keys=True),
        *[row["fingerprint"] for row in
          top + agenda + progress + completed + news_items],
        *["%s=%s" % (row["id"], row["suppressed"]) for row in suppressed]))
    # ...which is exactly why it cannot also be the at-most-one-email-per-day key: a
    # changing id would send a second email every time anything moved.  `day_key` is
    # that separate, stable job key -- one profile, one local day, one send.
    brief["reply"] = {"brief_id": brief["id"], "date": date_key,
                      "subject_tag": "[collie %s]" % brief["id"],
                      "day_key": "daily-brief:%s:%s" % (brief["profile"], date_key),
                      "sources": sorted(ok_names)}

    if state_dir is not None and remember:
        _remember(state_dir, profile, brief, ordered, date_key, wall)
    return brief


def _remember(state_dir, profile, brief, items, date_key, wall):
    def mutate(state):
        for item in items:
            entry = state["seen"].get(item["id"]) if isinstance(state["seen"], dict) else None
            first = _text((entry or {}).get("first_date"), 10) or date_key
            days = item["days_seen"]
            state["seen"][item["id"]] = {"first_date": first, "last_date": date_key,
                                         "fingerprint": item["fingerprint"],
                                         "days": days, "last_at": wall}
        log = [row for row in state["briefs"] if row.get("id") != brief["id"]]
        log.append({"id": brief["id"], "date": date_key, "at": wall,
                    "sources": brief["coverage"]["ok"],
                    "counts": dict(brief["counts"])})
        state["briefs"] = log
        return True

    try:
        _edit_state(state_dir, profile, mutate, now=wall)
    except (OSError, statelock.StateLockTimeout) as exc:
        brief["notices"].append("Brief history was not saved (%s)." % type(exc).__name__)


#: Strings this module writes itself, and nothing else.  A row's ``generated`` list says
#: which of its fields are in here; a title that came out of a payload is never looked
#: up, because translating a person's own subject line or mission name would be a
#: rewrite of their words rather than a translation of ours.
_GENERATED = {"zh": {
    # decisions, reminders and the scheduler
    "A decision is waiting": "有一项决定在等你",
    "Collie is paused until you answer.": "Collie 已暂停，等待你的答复。",
    "Past its expected time.": "已超过预计时间。",
    "May be delayed.": "可能会延迟。",
    "Coming up soon.": "即将到来。",
    "Automatic task progress is blocked": "任务的自动推进已被阻止",
    "Saved tasks are kept.": "已保存的任务仍然保留。",
    # tasks, runs and the calendar
    "Ongoing task": "进行中的任务",
    "Collie is working": "Collie 正在处理",
    "A task stopped before finishing": "一项任务未完成就停止了",
    "A task finished": "一项任务已完成",
    "Private event": "私密日程",
    "Scheduled meeting": "已安排的会议",
    "A routine worth reviewing": "一个值得确认的流程",
    # messages and replies
    "A message is waiting for you": "有一条消息在等你查看",
    "Waiting for you to review it in Communications.": "等待你在“通讯”中查看。",
    "A message Collie is working on": "Collie 正在处理的一条消息",
    "Collie is working on this message.": "Collie 正在处理这条消息。",
    "A reply is waiting to be sent": "有一条回复等待发送",
    "Ready to send from Communications.": "可在“通讯”中发送。",
    "Ready to send. This connection is paused, so nothing will go out until you "
    "resume it.": "已可发送。该连接已暂停，恢复之前不会发出。",
    "A reply could not be sent": "有一条回复未能发送",
    "It could not be sent. Open Communications to retry or edit it.":
        "未能发送。请在“通讯”中重试或修改。",
    "A reply may not have been sent": "一条回复是否已发送尚不清楚",
    "It is not known whether this was sent. It will not be retried by itself.":
        "无法确认这条回复是否已发送，系统不会自动重试。",
    "A reply is being sent": "正在发送一条回复",
    "Being sent now.": "正在发送。",
    "A reply was handed to the provider": "一条回复已交给服务商",
    "The service accepted it. Delivery is not confirmed.":
        "服务商已接收，但这不等于已送达。",
    # the bare kind, used only when a row carried no words at all
    "approval": "待决定", "reminder": "提醒", "mission": "任务", "scheduler": "调度",
    "queued": "排队中", "run": "运行", "meeting": "会议", "event": "事项",
    "procedure": "流程", "message": "消息", "reply": "回复", "news": "新闻",
}}

#: Notices about the communications source.  Each takes one count, and each says what
#: was *not* covered -- never "everything is fine".
_COMMS_NOTICES = {
    "quiet": ("%d message(s) are from senders you have not allowed, or arrived "
              "automatically. They are kept in Communications and are not counted as "
              "needing you.",
              "有 %d 条消息来自未被允许的发件人或为自动邮件。它们仍保留在“通讯”中，"
              "但不计为需要你处理的事项。"),
    "partial": ("%d connection(s) hold more messages than one brief reads, so only the "
                "most recent were checked. Nothing older has been assumed clear.",
                "有 %d 个连接的消息数量超出单份简报的读取范围，只检查了最近的部分，"
                "更早的内容未被判定为无事。"),
    "unreadable": ("%d message connection(s) could not be read. Nothing in them has "
                   "been assumed clear.",
                   "有 %d 个消息连接无法读取，其中的内容未被判定为无事。"),
}


def _localize(rows, language):
    """Swap module-written strings for another language, in place.  Payload text is
    left exactly as the source stored it."""
    key = str(language or "en").lower()
    table = _GENERATED.get(key) or _GENERATED.get(key.split("-")[0])
    if not table:
        return
    for item in rows:
        for field in item.get("generated") or ():
            item[field] = table.get(item.get(field), item.get(field))


def _greeting(hour):
    if hour < 5:
        return "Good evening."
    if hour < 12:
        return "Good morning."
    if hour < 18:
        return "Good afternoon."
    return "Good evening."


def _headline(brief):
    counts, coverage = brief["counts"], brief["coverage"]
    # Read in full, read in part and not read at all are three different claims about
    # the day, and only the first one may end in "nothing needs you".
    partial = bool(coverage.get("partial"))
    if str(brief.get("language") or "").startswith("zh"):
        if brief["setup"]:
            return ("暂时无法读取来源，还不能判断今天的情况。" if brief["setup"]["reason"] == "no source responded"
                    else "还没有连接来源，暂时没有可汇总的内容。")
        tail = "" if coverage["complete"] else " 部分来源未能读取，尚不能判断其中的事项。"
        if not tail and partial:
            tail = " 部分内容未被读取，尚不能判断其中的事项。"
        if counts["attention"]:
            return "%d 件事需要你关注。" % counts["attention"] + tail
        if not coverage["complete"]:
            return "部分来源未能读取，尚不能判断其中的事项。"
        if partial:
            return "部分内容未被读取，尚不能判断其中的事项。"
        if counts["agenda"]:
            return "今天有 %d 项日程，目前没有需要你决定的事。" % counts["agenda"]
        if counts["progress"]:
            return "工作正在推进，目前没有需要你决定的事。"
        return "已读取的来源中，目前没有需要你决定的事。"
    if brief["setup"]:
        if brief["setup"]["reason"] == "no source responded":
            return "No source could be read, so nothing has been assumed clear."
        return "No sources are connected yet, so there is nothing to report."
    tail = "" if coverage["complete"] else \
        " Some sources could not be read, so nothing else has been assumed clear."
    if not tail and partial:
        tail = " Some sources were only read in part, so nothing else has been assumed clear."
    if counts["attention"]:
        return ("%d thing%s need%s your attention." % (
            counts["attention"], "" if counts["attention"] == 1 else "s",
            "s" if counts["attention"] == 1 else "")) + tail
    if not coverage["complete"]:
        return "Some sources could not be read. Nothing has been assumed clear."
    if partial:
        return "Some sources were only read in part. Nothing has been assumed clear."
    if counts["agenda"]:
        return "%d item%s on your calendar today. Nothing needs a decision." % (
            counts["agenda"], "" if counts["agenda"] == 1 else "s")
    if counts["progress"]:
        return "Work is in progress. Nothing needs your decision."
    return "Nothing needs a decision right now."


def _suggestions(brief, has_calendar):
    """One to three concrete next steps, each derived from a row above.

    Every suggestion is a *prompt* for a composer plus the evidence it came from.
    Nothing here runs: ``requires_user_intent`` is always true, and the caller must
    treat these as drafts a person chooses, never as a plan to execute.
    """
    out = []

    def add(key, title, prompt, evidence):
        if len(out) < 3:
            out.append({"id": "suggestion:%s" % key, "title": title, "prompt": prompt,
                        "evidence": evidence, "requires_user_intent": True})

    approvals = [row for row in brief["top"] if row["kind"] == "approval"]
    overdue = [row for row in brief["attention"] if row["status"] in
               ("overdue", "possibly_delayed")]
    if approvals:
        add("review-decisions", "Review what Collie is waiting on",
            "Walk me through the decisions waiting for me and what each one changes. "
            "Do not act on any of them until I say so.",
            [row["id"] for row in approvals])
    if overdue:
        add("triage-overdue", "Check what slipped",
            "Check the items that look late, tell me what the source actually says, and "
            "ask me before sending, buying, cancelling or changing anything.",
            [row["id"] for row in overdue[:3]])
    if not brief["coverage"]["complete"]:
        add("retry-sources", "Retry the sources that failed",
            "Some local sources did not answer. Help me check why, without assuming the "
            "missing ones were empty.", brief["coverage"]["unavailable"])
    if brief["agenda"]:
        add("plan-around-agenda", "Plan around today's schedule",
            "Here is my schedule for today. Help me plan realistic work around it and "
            "keep some open time.", [row["id"] for row in brief["agenda"][:3]])
    elif not has_calendar:
        add("connect-calendar", "Connect a calendar",
            "Help me connect a calendar so Today and the daily brief can show real plans "
            "instead of guessing. Do not connect anything without my approval.", [])
    stale = [row for row in brief["attention"] if row["stale"]]
    if stale:
        add("close-stale", "Deal with what keeps coming back",
            "These items have been in my brief unchanged for days. Help me decide for each "
            "one: do it, reschedule it, or drop it.", [row["id"] for row in stale[:3]])
    if not out:
        add("shortest-plan", "Keep today realistic",
            "Nothing is urgent. Help me pick one worthwhile thing for today without "
            "filling the whole day.", [])
    return out


# ---------------------------------------------------------------- renderers

#: Renderer strings.  Unknown languages fall back to English rather than to a key.
_LABELS = {
    "en": {"attention": "Needs you", "agenda": "Today", "progress": "In progress",
           "completed": "Done today", "news": "News", "suggestions": "Suggested next",
           "notices": "Notes", "setup": "Set up", "sources": "Sources",
           "hidden": "hidden by you", "stale": "unchanged since",
           "nothing": "Nothing here.", "subject": "Daily brief"},
    "zh": {"attention": "需要你", "agenda": "今天", "progress": "进行中",
           "completed": "今日完成", "news": "新闻", "suggestions": "建议",
           "notices": "说明", "setup": "设置", "sources": "来源",
           "hidden": "已被你隐藏", "stale": "未变化，始于",
           "nothing": "暂无内容。", "subject": "每日简报"},
}


def _labels(language):
    key = str(language or "en").lower()
    return _LABELS.get(key) or _LABELS.get(key.split("-")[0]) or _LABELS["en"]


def _label(brief, key, bilingual):
    primary = _labels(brief.get("language"))[key]
    if not bilingual:
        return primary
    english = _LABELS["en"][key]
    return primary if primary == english else "%s · %s" % (english, primary)


def _when_text(item, brief):
    when = item.get("when")
    if not when:
        return ""
    minutes = item.get("when_offset_minutes")
    if minutes is None:                       # a snapshot from before per-row offsets
        minutes = brief["timezone"].get("utc_offset_minutes") or 0
    local = _dt.datetime.fromtimestamp(float(when), _fixed(int(_finite(minutes, 0.0))))
    return local.strftime("%H:%M")


def _line(item, brief):
    """One item as plain text.  All strings here came through ``_text`` already."""
    bits = [_when_text(item, brief), item["title"]]
    detail = item.get("detail") or item.get("status")
    if detail:
        bits.append("(%s)" % detail)
    if item.get("stale"):
        bits.append("[%s %s]" % (_labels(brief.get("language"))["stale"],
                                 item.get("first_seen_date", "")))
    return " · ".join(bit for bit in bits if bit)


def render_text(brief, *, bilingual=False):
    """The brief as plain text.  Same snapshot, same facts as the HTML renderer."""
    _check(brief)
    lines = ["%s %s" % (brief["greeting"], brief["headline"]),
             "%s · %s" % (brief["date"], brief["timezone"].get("resolved") or "UTC")]
    tz = brief["timezone"]
    if tz.get("degraded"):
        lines.append("Times shown in %s — %s (%s requested)." % (
            tz.get("resolved") or "UTC", tz.get("reason") or "timezone unavailable",
            tz.get("requested") or "none"))
    if brief.get("setup"):
        lines += ["", _label(brief, "setup", bilingual) + ":"]
        lines += ["  - %s" % step["title"] for step in brief["setup"]["steps"]]
    for key, rows in (("attention", brief["top"]), ("agenda", brief["agenda"]),
                      ("progress", brief["progress"]), ("completed", brief["completed"])):
        if not rows:
            continue
        lines += ["", "%s (%d):" % (_label(brief, key, bilingual), brief["counts"][key])]
        lines += ["  - %s" % _line(row, brief) for row in rows]
    hidden = len(brief["top"]) < brief["counts"]["attention"]
    if hidden:
        lines.append("  … %d more in Today." % (brief["counts"]["attention"] - len(brief["top"])))
    if brief["news"]:
        lines += ["", "%s:" % _label(brief, "news", bilingual)]
        lines += ["  - %s (%s) %s" % (row["title"], row["source_ref"]["label"],
                                      row["source_ref"]["href"]) for row in brief["news"]]
    if brief["suggestions"]:
        lines += ["", "%s:" % _label(brief, "suggestions", bilingual)]
        lines += ["  - %s" % row["title"] for row in brief["suggestions"]]
    if brief["notices"]:
        lines += ["", "%s:" % _label(brief, "notices", bilingual)]
        lines += ["  - %s" % notice for notice in brief["notices"]]
    lines += ["", "%s: %s" % (_label(brief, "sources", bilingual),
                              ", ".join(brief["coverage"]["ok"]) or "none")]
    return "\n".join(lines).strip() + "\n"


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _html_rows(brief, key, rows, bilingual):
    if not rows:
        return ""
    out = ['<h2 style="font:600 13px sans-serif;margin:18px 0 6px">%s (%d)</h2><ul>'
           % (_esc(_label(brief, key, bilingual)), brief["counts"][key])]
    for row in rows:
        href = row["source_ref"]["href"]
        title = _esc(row["title"])
        # Local app routes are useful on the desktop, but become broken links
        # inside an email client. Only already-validated public URLs can be links.
        if href.startswith("https://"):
            title = '<a href="%s">%s</a>' % (_esc(href), title)
        meta = " · ".join(_esc(bit) for bit in
                          (_when_text(row, brief), row.get("detail") or row.get("status"))
                          if bit)
        out.append('<li style="margin:4px 0">%s%s</li>' % (
            title, ' <span style="color:#6b7280">%s</span>' % meta if meta else ""))
    out.append("</ul>")
    return "".join(out)


def render_email(brief, *, bilingual=False):
    """``{"subject", "text", "html", "headers"}`` for an opt-in digest.

    Built from the same snapshot as :func:`render_text`, so the two cannot disagree.
    Every untrusted string is HTML-escaped and every link has already been through
    :func:`safe_href`.  This function does not send anything and knows no transport;
    the caller owns consent, quiet hours and at-most-once-per-day.
    """
    _check(brief)
    text = render_text(brief, bilingual=bilingual)
    parts = ['<div style="font:14px/1.5 -apple-system,Segoe UI,sans-serif;color:#111">',
             '<p style="font-size:12px;color:#6b7280;margin:0">%s · %s</p>' % (
                 _esc(brief["date"]), _esc(brief["timezone"].get("resolved") or "UTC")),
             '<h1 style="font-size:19px;margin:6px 0 2px">%s</h1>' % _esc(brief["greeting"]),
             '<p style="margin:0 0 4px">%s</p>' % _esc(brief["headline"])]
    tz = brief["timezone"]
    if tz.get("degraded"):
        parts.append('<p style="color:#b45309;font-size:12px">%s</p>' % _esc(
            "Times shown in %s — %s." % (tz.get("resolved") or "UTC",
                                         tz.get("reason") or "timezone unavailable")))
    if brief.get("setup"):
        parts.append('<h2 style="font:600 13px sans-serif;margin:18px 0 6px">%s</h2><ul>'
                     % _esc(_label(brief, "setup", bilingual)))
        for step in brief["setup"]["steps"]:
            parts.append("<li>%s</li>" % _esc(step["title"]))
        parts.append("</ul>")
    for key, rows in (("attention", brief["top"]), ("agenda", brief["agenda"]),
                      ("progress", brief["progress"]), ("completed", brief["completed"]),
                      ("news", brief["news"])):
        parts.append(_html_rows(brief, key, rows, bilingual))
    if brief["suggestions"]:
        parts.append('<h2 style="font:600 13px sans-serif;margin:18px 0 6px">%s</h2><ul>'
                     % _esc(_label(brief, "suggestions", bilingual)))
        for row in brief["suggestions"]:
            parts.append("<li>%s</li>" % _esc(row["title"]))
        parts.append("</ul>")
    if brief["notices"]:
        parts.append('<ul style="color:#6b7280;font-size:12px;margin-top:18px">')
        for notice in brief["notices"]:
            parts.append("<li>%s</li>" % _esc(notice))
        parts.append("</ul>")
    parts.append('<p style="color:#6b7280;font-size:12px">%s: %s</p></div>' % (
        _esc(_label(brief, "sources", bilingual)),
        _esc(", ".join(brief["coverage"]["ok"]) or "none")))
    subject = "%s · %s · %s" % (_labels(brief.get("language"))["subject"], brief["date"],
                                brief["headline"])
    return {"subject": _text(subject, 160), "text": text, "html": "".join(parts),
            "headers": {"X-Collie-Brief-Id": brief["id"], "X-Collie-Brief-Date": brief["date"]}}


def _check(brief):
    if not isinstance(brief, dict) or brief.get("schema") != SCHEMA:
        raise BriefError("not a daily brief snapshot")
