"""The opt-in daily *email* of the Daily Brief: one message a local day, or none.

The brief itself is a local surface.  This module is the only place that turns it
into mail, and it exists to make three promises that the rest of the surface
cannot make on its own.

**Consent is explicit and separate.**  Seeing the brief in the app is a desktop
default; being emailed it is not.  Nothing here is on until :func:`configure` is
called with ``enabled: true``, and enabling it never enables a connection, never
adds credentials, never touches a provider account and never turns on a
connection's general ``auto_reply`` switch -- that switch is about replying to
people who wrote in, and it is deliberately *not* a prerequisite here, because
this preference is its own consent and nothing else's.

**The destination is frozen, never inferred.**  There is no recipient field.  The
brief goes to the owner address of the connection the user picked, as it stood at
opt-in; only ``imap`` and ``collie_mail`` connections qualify, never voice or SMS.
If that account's owner address, kind or connection later changes, the schedule
*pauses and says so* instead of quietly forwarding a private summary to whatever
address is configured now.

**One email per local date, or an explicit skip.**  The job key is
``(profile, connection, local date)`` and is deliberately separate from
``brief["id"]``, which names the *content* and changes whenever anything moves.
A day is a local day: 23- and 25-hour days are handled by the zone, and a machine
that cannot resolve the zone fails closed rather than sending yesterday's date.
The send window is the configured morning time plus a bounded grace; an app that
was off all day skips that date, visibly, and never floods a backlog.

Durability order, which is the whole crash story::

    ledger job (frozen subject+text)  ->  comms outbox row  ->  provider

Every step is idempotent on the *stored* payload: a restart re-uses the outbox row
that already exists and never rebuilds different text under the same id.  The
outbox state is the authority on what happened -- ``submitted`` means the provider
accepted the message, which is not the same as delivery, and ``unknown`` is never
resent.  Results are marked ``auto_eligible: False`` so the channel delivery lane
leaves them alone; the scheduler sends only its own pending draft.

Mounting (the parent owns the routes)::

    # GET  /api/brief/preferences -> daily_brief_schedule.preferences(root)
    # POST /api/brief/preferences -> daily_brief_schedule.configure(root, body)
    # from the existing channel pump / periodic tick:
    #     daily_brief_schedule.tick(root)      # a no-op until someone opted in

``ScheduleError`` is a ``ValueError``, so an existing ``except ValueError -> 400``
arm already turns a bad request into a visible message.  Nothing here reads a
mailbox, a credential or a message body: it renders sources that are already
connected, and asks :mod:`communications` for the rest.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import time

from . import communications as comms, daily_brief, sessions

SCHEMA = "collie.daily_brief.schedule/1"
STATE_SCHEMA = "collie.daily_brief.schedule.state/1"

#: Mail only.  A brief read aloud down a phone line is not a thing we will do.
KINDS = ("imap", "collie_mail")
LANGUAGES = ("en", "zh")
FIELDS = ("enabled", "connection", "timezone", "at", "language", "grace_minutes")

DEFAULT_AT = "07:30"
DEFAULT_GRACE_MINUTES = 240               # "this morning", not "some time today"
MIN_GRACE_MINUTES, MAX_GRACE_MINUTES = 15, 12 * 60
MAX_ATTEMPTS = 3                          # per job, ever; a failure waits for a person
JOB_RETENTION = 30
CORRUPT_KEPT = 3
SUBJECT_BYTES = 512
TEXT_BYTES = comms.MAX_TEXT_BYTES         # 64 KiB, the transport's own ceiling
MAX_STATE_BYTES = 4 * 1024 * 1024

#: Job states this module owns.  Everything else it reports comes from the outbox.
OPEN_STATES = ("preparing", "queued", "sending")
#: Outbox states that have settled for good.  A settled day may be corrected to
#: one of these -- by the provider, or by a person resolving it -- and to nothing
#: else: a day that is already closed here is never re-opened, so no correction
#: can become a second email.
SETTLED_STATES = ("submitted", "failed", "unknown", "cancelled")

_AT_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_REPAIR = ("the daily email settings could not be read; they were left untouched "
           "and nothing was sent")

_DEFAULTS = {"enabled": False, "connection": "", "kind": "", "owner_digest": "",
             "destination_masked": "", "timezone": "", "at": DEFAULT_AT,
             "language": "en", "grace_minutes": DEFAULT_GRACE_MINUTES,
             "updated": 0.0, "opted_in_at": 0.0}


class ScheduleError(ValueError):
    """A refusal a person can act on.  Never carries an address or a body."""


# ---------------------------------------------------------------- small helpers


def _digest(value):
    return hashlib.sha256(("collie.brief.owner/1|" + str(value)).encode("utf-8")).hexdigest()[:32]


def _clip(value, limit, mark="\n… (truncated)"):
    """Bounded UTF-8, cut on a character boundary and marked when it was cut."""
    text = str(value or "")
    if len(text.encode("utf-8")) <= limit:
        return text
    room = limit - len(mark.encode("utf-8"))
    cut = text.encode("utf-8")[:room].decode("utf-8", "ignore")
    return cut + mark


def _zone(name):
    """The IANA zone, or a refusal.  A brief dated the wrong day is worse than none."""
    key = str(name or "").strip()
    if not key or len(key) > 64 or not daily_brief._ZONE_RE.fullmatch(key):
        raise ScheduleError("choose a time zone like Europe/Berlin or Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(key)
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        if daily_brief._looks_like_missing_tzdata():
            raise ScheduleError(
                "this machine has no time zone database, so a local morning cannot be "
                "worked out; install the 'tzdata' package, then set this up again") from None
        raise ScheduleError("unknown time zone %r" % key[:40]) from None


def _slot(wall, zone, at):
    """``(local date, the instant the email is due)`` for the day ``wall`` falls in.

    The date comes from the zone, so a 23- or 25-hour day is still exactly one day
    and one email.  On the spring-forward hour the configured time does not exist;
    the standard library resolves it to the instant that hour would have been, which
    keeps the window a real window instead of skipping the day.
    """
    hour, minute = int(at[:2]), int(at[3:])
    local = _dt.datetime.fromtimestamp(float(wall), zone)
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local.strftime("%Y-%m-%d"), due.timestamp()


def _service(root, service=None):
    if service is not None:
        return service
    from .channel_service import ChannelService
    return ChannelService(root)


# ---------------------------------------------------------------- the ledger


def _path(root, profile):
    if not daily_brief._PROFILE_RE.fullmatch(str(profile or "")):
        raise ScheduleError("invalid brief profile name")
    folder = os.path.join(os.path.abspath(str(root)), "daily-brief-schedule")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "%s.json" % profile)


def _blank():
    return {"schema": STATE_SCHEMA, "prefs": dict(_DEFAULTS), "paused": {}, "jobs": []}


def _load(path):
    """``(state, error)``.  Unreadable settings are ``(None, why)`` -- never defaults.

    Failing closed here is the point: silently treating a damaged file as "off" is
    survivable, but silently treating it as *some other configuration* is not, and a
    read must never overwrite the evidence.  :func:`configure` is the only repair,
    because that is the one moment a person is asking for a specific state.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return _blank(), ""
    except OSError as exc:
        return None, "the daily email settings could not be read (%s)" % type(exc).__name__
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        value = None
    if (len(raw) > MAX_STATE_BYTES or not isinstance(value, dict)
            or value.get("schema") != STATE_SCHEMA
            or not isinstance(value.get("prefs"), dict)
            or not isinstance(value.get("paused", {}), dict)
            or not isinstance(value.get("jobs"), list)
            or not all(isinstance(job, dict) and isinstance(job.get("id"), str)
                       for job in value["jobs"])):
        return None, _REPAIR
    state = _blank()
    for key, default in _DEFAULTS.items():
        got = value["prefs"].get(key, default)
        if isinstance(default, bool):
            ok = isinstance(got, bool)
        elif isinstance(default, float):
            ok = isinstance(got, (int, float)) and not isinstance(got, bool)
        elif isinstance(default, int):
            ok = isinstance(got, int) and not isinstance(got, bool)
        else:
            ok = isinstance(got, str)
        if not ok:
            return None, _REPAIR
        state["prefs"][key] = got
    # A pause is a reason and a time, and only ever those two: whatever else a
    # file carries under that key is not carried back out into a surface.
    paused = value.get("paused") or {}
    reason, at = str(paused.get("reason") or "")[:300], paused.get("at")
    state["paused"] = {"reason": reason,
                       "at": float(at) if isinstance(at, (int, float))
                       and not isinstance(at, bool) else 0.0} if reason else {}
    state["jobs"] = [dict(job) for job in value["jobs"][-JOB_RETENTION:]]
    return state, ""


def _save(path, state):
    """Atomic, bounded.  Settled jobs keep their evidence, not the person's day."""
    jobs = []
    for job in state["jobs"][-JOB_RETENTION:]:
        if job.get("state") not in OPEN_STATES:
            job = {k: v for k, v in job.items() if k not in ("text", "subject")}
        jobs.append(job)
    state["jobs"] = jobs
    sessions._atomic_dump(state, path)


def _preserve(path):
    """Keep a damaged file beside itself, bounded, before an explicit rewrite."""
    folder, name = os.path.dirname(path), os.path.basename(path)
    try:
        os.replace(path, "%s.corrupt-%d" % (path, int(time.time())))
        kept = sorted(f for f in os.listdir(folder)
                      if f.startswith(name + ".corrupt-"))
        for stale in kept[:-CORRUPT_KEPT]:
            os.remove(os.path.join(folder, stale))
    except OSError:
        pass


def _job_id(profile, connection, date):
    """Names the *send*, not the content: one per profile, connection and local day."""
    return "daily-brief-email:%s:%s:%s" % (profile, connection, date)


def _result_id(profile, connection, date):
    """The outbox idempotency key.  Ids are ASCII and become no file name but ours."""
    return "daily-brief-%s-%s" % (date, _digest("%s|%s|%s" % (profile, connection, date))[:12])


def _thread_key(job_id):
    """The conversation a reply to this day's brief belongs to.

    Derived from the job id, so it names exactly one profile, connection and local
    day and is re-derived identically after a crash or a repaired ledger.  The
    prefix is distinct from the ``mail-``/``sms-`` keys :mod:`channel_service`
    mints for messages that thread against nothing, so a key of this shape can
    only ever have been reached by an incoming message whose ``References`` or
    ``In-Reply-To`` matched this outbound brief.  It is a plain token by
    ``communications`` rules: ASCII, single line, far inside the length limit.
    """
    return "daily-brief-thread-%s" % hashlib.sha256(
        ("collie.brief.thread/1|" + str(job_id)).encode("utf-8")).hexdigest()[:32]


def _find(state, job_id):
    for job in state["jobs"]:
        if job.get("id") == job_id:
            return job
    return None


# ---------------------------------------------------------------- connection


def _owner(service, prefs):
    """``(row, refusal)``.  The frozen destination, or a reason to pause.

    The address itself is never stored here; only a digest of it is, so this can
    tell "same account as at opt-in" from "somebody re-pointed the connection"
    without keeping a second copy of the user's mail address on disk.
    """
    try:
        row = service.connection(prefs["connection"])
    except Exception:                                 # noqa: BLE001 - reported, not raised
        return None, ("the email connection this brief was set up for is no longer "
                      "there; choose an account again")
    if row.get("kind") not in KINDS or row.get("kind") != prefs["kind"]:
        return None, ("this connection is not the email account the brief was set up "
                      "for any more; set the daily email up again")
    if _digest(row.get("owner") or "") != prefs["owner_digest"]:
        return None, ("this account's owner address changed, so the daily brief was "
                      "paused instead of being sent somewhere new; set it up again")
    return row, ""


def _ready(row):
    """Transient reasons not to send *now*.  None of these settle the day."""
    if not row.get("enabled"):
        return "the email connection is paused"
    if row.get("settings_incomplete"):
        return "this connection's settings were interrupted while saving"
    if row["kind"] != "collie_mail" and not row.get("has_credentials"):
        return "connect this account again before the brief can be emailed"
    return ""


def _message_id(row, job_id):
    """A stable Message-ID for this day's brief, so replies and retries thread."""
    config = row.get("config") or {}
    domain = str(config.get("address") or config.get("sender") or "mail@collie.run")
    return "<collie-brief-%s@%s>" % (hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:40],
                                     domain.split("@")[-1] or "collie.run")


# ---------------------------------------------------------------- rendering


def _render(root, prefs, profile, wall):
    """The exact payload this day's email will carry, frozen once and stored.

    Renders from the same collector the surface uses -- already-connected sources
    and the user's existing brief preferences -- and takes the plain-text half of
    ``render_email``, because that is what the current transports actually send.
    """
    from . import daily_brief_web
    payload = daily_brief_web.read(root, timezone=prefs["timezone"],
                                   language=prefs["language"], profile=profile, now=wall)
    brief, email = payload["brief"], payload["email"]
    zone = brief.get("timezone") or {}
    if zone.get("degraded") or zone.get("resolved") != prefs["timezone"]:
        raise ScheduleError("today's brief could not be built in %s, so nothing was sent"
                            % prefs["timezone"][:40])
    if not email.get("available"):
        raise ScheduleError("today's brief could not be rendered as an email")
    # A subject is one line by contract; only the body carries the cut marker.
    return brief, _clip(email["subject"], SUBJECT_BYTES, "…"), _clip(email["text"], TEXT_BYTES)


# ---------------------------------------------------------------- public views


def _public_job(job):
    """A job as a surface may show it: what happened, never the person's morning."""
    if not job:
        return None
    return {"id": job.get("id"), "date": job.get("date"), "state": job.get("state"),
            "connection": job.get("connection"), "result_id": job.get("result_id"),
            "brief_id": job.get("brief_id"), "attempts": int(job.get("attempts") or 0),
            "detail": str(job.get("detail") or "")[:300],
            "text_bytes": int(job.get("text_bytes") or 0),
            "created": job.get("created"), "updated": job.get("updated"),
            # Provider acceptance is not receipt, and this never claims otherwise.
            "submission_known": job.get("state") == "submitted",
            "delivery_known": False}


def _choices(service):
    """Connections that could carry a brief, masked.  A listing changes nothing."""
    rows = []
    try:
        for row in service.overview()["connections"]:
            if row.get("kind") in KINDS:
                rows.append({"id": row.get("id"), "kind": row.get("kind"),
                             "destination_masked": comms.mask_address(row.get("owner"), "email"),
                             "enabled": bool(row.get("enabled")),
                             "ready": not _ready(row), "reason": _ready(row)})
    except Exception:                                 # noqa: BLE001 - a listing, never a blocker
        return []
    return rows


def _public(state, profile, *, service=None, notices=()):
    prefs = state["prefs"]
    out = {"schema": SCHEMA, "profile": profile, "available": True, "error": "",
           "enabled": bool(prefs["enabled"]), "connection": prefs["connection"],
           "kind": prefs["kind"], "destination_masked": prefs["destination_masked"],
           "timezone": prefs["timezone"], "at": prefs["at"], "language": prefs["language"],
           "grace_minutes": int(prefs["grace_minutes"]),
           "opted_in_at": prefs["opted_in_at"], "updated": prefs["updated"],
           "needs_reconfigure": bool(state["paused"]),
           "paused_reason": str((state["paused"] or {}).get("reason") or "")[:300],
           "defaults": {"at": DEFAULT_AT, "grace_minutes": DEFAULT_GRACE_MINUTES,
                        "grace_range": [MIN_GRACE_MINUTES, MAX_GRACE_MINUTES]},
           "languages": list(LANGUAGES), "kinds": list(KINDS),
           "jobs": [_public_job(job) for job in state["jobs"][-7:]],
           "notices": list(notices)}
    out["last"] = out["jobs"][-1] if out["jobs"] else None
    if service is not None:
        out["connections"] = _choices(service)
    return out


def preferences(root, *, profile="default", service=None):
    """What the daily email is set to, safe to hand to any authenticated surface.

    No address, no subject line and no message body: a masked destination, the
    schedule, and what the last few days' jobs did.
    """
    path = _path(root, profile)
    with sessions._locked(path):
        state, error = _load(path)
    if state is None:
        return {"schema": SCHEMA, "profile": profile, "available": False, "enabled": False,
                "error": error, "needs_reconfigure": True, "jobs": [], "last": None,
                "defaults": {"at": DEFAULT_AT, "grace_minutes": DEFAULT_GRACE_MINUTES,
                             "grace_range": [MIN_GRACE_MINUTES, MAX_GRACE_MINUTES]},
                "languages": list(LANGUAGES), "kinds": list(KINDS), "notices": []}
    return _public(state, profile, service=_service(root, service))


# ---------------------------------------------------------------- configure


def _validate(body, prefs):
    """Strict: every field is checked, unknown fields are refused, nothing coerced."""
    out = dict(prefs)
    if "timezone" in body or not out["timezone"]:
        zone = body.get("timezone")
        if not isinstance(zone, str) or not zone.strip():
            raise ScheduleError("choose the time zone your morning happens in")
        _zone(zone)                                   # resolves now, or refuses now
        out["timezone"] = zone.strip()
    if "at" in body:
        at = body.get("at")
        if not isinstance(at, str) or not _AT_RE.fullmatch(at.strip()):
            raise ScheduleError("send time must look like 07:30, on a 24-hour clock")
        out["at"] = at.strip()
    if "language" in body:
        language = body.get("language")
        if language not in LANGUAGES:
            raise ScheduleError("language must be one of: %s" % ", ".join(LANGUAGES))
        out["language"] = language
    if "grace_minutes" in body:
        grace = body.get("grace_minutes")
        if isinstance(grace, bool) or not isinstance(grace, int):
            raise ScheduleError("the missed-window cutoff must be a whole number of minutes")
        if not MIN_GRACE_MINUTES <= grace <= MAX_GRACE_MINUTES:
            raise ScheduleError("the missed-window cutoff must be between %d minutes and "
                                "%d hours" % (MIN_GRACE_MINUTES, MAX_GRACE_MINUTES // 60))
        out["grace_minutes"] = grace
    return out


def configure(root, body, *, profile="default", now=None, service=None):
    """Turn the daily email on or off.  Enabling freezes the destination.

    ``body`` is ``{"enabled": bool}`` plus, when enabling, ``connection`` and
    ``timezone`` (IANA), and optionally ``at`` (``"HH:MM"``), ``language``
    (``en``/``zh``) and ``grace_minutes``.  There is no recipient field: the brief
    goes to the chosen connection's owner address and nowhere else.

    This enables nothing but this preference.  A paused connection stays paused, a
    provider account is never created or touched, and the connection's own
    ``auto_reply`` switch is neither read nor changed.
    """
    wall = float(now) if now is not None else time.time()
    if not isinstance(body, dict):
        raise ScheduleError("expected a JSON object")
    unknown = sorted(set(body) - set(FIELDS) - {"profile"})
    if unknown:
        raise ScheduleError("unknown setting(s): %s" % ", ".join(unknown[:5]))
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ScheduleError("enabled must be true or false")
    service = _service(root, service)
    path = _path(root, profile)
    with sessions._locked(path):
        state, error = _load(path)
        notices = []
        if state is None:
            # The one moment repair is honest: a person is stating what they want.
            _preserve(path)
            state, notices = _blank(), ["the previous daily email settings could not be "
                                        "read; they were set aside and replaced"]
        if not enabled:
            state["prefs"]["enabled"] = False
            state["prefs"]["updated"] = wall
            state["paused"] = {}
            _save(path, state)
            return _public(state, profile, service=service, notices=notices)

        connection = body.get("connection", state["prefs"]["connection"])
        if not isinstance(connection, str) or not connection.strip():
            raise ScheduleError("choose the email account the brief should be sent from")
        prefs = _validate(body, state["prefs"])
        row = _connection(service, connection.strip())
        prefs.update(enabled=True, connection=row["id"], kind=row["kind"],
                     owner_digest=_digest(row["owner"]),
                     destination_masked=comms.mask_address(row["owner"], "email"),
                     updated=wall)
        if not prefs["opted_in_at"] or prefs["owner_digest"] != state["prefs"]["owner_digest"]:
            prefs["opted_in_at"] = wall
        state["prefs"], state["paused"] = prefs, {}
        _save(path, state)
        blocked = _ready(row)
        if blocked:
            notices.append("The daily email is on, but %s, so nothing will be sent until "
                           "that is fixed." % blocked)
        return _public(state, profile, service=service, notices=notices)


def _connection(service, connection):
    try:
        row = service.connection(connection)
    except Exception:                                 # noqa: BLE001 - reported, not raised
        raise ScheduleError("that connection was not found; connect an email account "
                            "first") from None
    if row.get("kind") not in KINDS:
        raise ScheduleError("the daily brief can only be emailed; choose an email "
                            "connection, not a phone one")
    if not row.get("owner"):
        raise ScheduleError("this connection has no owner address to send the brief to")
    return row


# ---------------------------------------------------------------- tick


def _report(profile, state, detail="", *, job=None, date="", next_at=None,
            needs=False, sent=False):
    return {"schema": SCHEMA, "profile": profile, "state": state, "detail": detail,
            "date": date, "sent": bool(sent), "needs_reconfigure": bool(needs),
            "next_at": next_at, "job": _public_job(job),
            # Accepted by a provider is not delivered to a person; say so everywhere.
            "delivery_known": False}


def _edit(path, mutate):
    """One locked read-modify-write of the ledger.  Corruption stops the pass."""
    with sessions._locked(path):
        state, error = _load(path)
        if state is None:
            raise ScheduleError(error)
        result = mutate(state)
        _save(path, state)
        return result


def _peek(path, job_id):
    """The stored job, read-only."""
    with sessions._locked(path):
        state, _error = _load(path)
    return _find(state, job_id) if state else None


def _stored(service, connection, result_id, *, private=False):
    """This day's outbox row, or ``None``.  A read: it starts no send and no provider.

    Safe to call while the ledger lock is held.  The outbox has its own lock and
    never takes this module's, so the only order that ever occurs is ledger then
    outbox, and there is no second order to deadlock against.
    """
    if not connection or not result_id:
        return None
    try:
        return comms.get_result(connection, result_id, directory=service.directory,
                                include_private=private)
    except (comms.CommsError, OSError, ValueError):   # a read never settles a day
        return None


def _reconcile(job, service, wall):
    """Let the outbox correct a day this module already settled.  ``True`` if it did.

    The ledger records what a pass *observed*; the outbox is what actually happened,
    and it keeps moving after the pass ends -- a person resolves an ``unknown`` from
    the account's sent folder, discards the draft, or the provider answered while the
    morning window was closing.  A stale ``unknown`` on a brief that demonstrably went
    out is a lie a surface then repeats all day.

    The only states this will ever write are settled ones, which is what keeps it
    safe: it can close a day the outbox already closed, but it can never re-open one,
    so no correction turns into a second email -- and a draft a person put back to
    ``pending`` with a manual retry is deliberately left for that person to send.
    """
    if not job:
        return False
    actual = str((_stored(service, job.get("connection"), job.get("result_id")) or {})
                 .get("state") or "")
    if actual not in SETTLED_STATES or actual == job.get("state"):
        return False
    job.update(state=actual, detail=_SETTLED.get(actual, "today's brief is %s" % actual),
               updated=wall)
    return True


def tick(root, now=None, *, profile="default", service=None):
    """One bounded pass.  At most one email, for today only, or a stated reason.

    Safe to call from any pump as often as you like: it is a cheap read until
    someone opts in, and concurrent or repeated calls cannot produce a second
    email -- the ledger, the outbox id and the send claim each refuse a duplicate
    independently.  It starts no model and no tool.
    """
    wall = float(now) if now is not None else time.time()
    path = _path(root, profile)

    with sessions._locked(path):
        state, error = _load(path)
        if state is None:
            return _report(profile, "error", error)
        prefs = state["prefs"]
        if not prefs["enabled"]:
            # Switched off: this reads one small file and stops.  No connection is
            # opened, no outbox is touched and no provider is asked anything.
            return _report(profile, "disabled", "the daily email is switched off")
        if state["paused"]:
            return _report(profile, "needs_reconfigure",
                           str(state["paused"].get("reason") or "")[:300], needs=True)
        service = _service(root, service)
        try:
            zone = _zone(prefs["timezone"])
        except ScheduleError as exc:
            return _report(profile, "error", str(exc))
        date, due = _slot(wall, zone, prefs["at"])
        window_end = due + int(prefs["grace_minutes"]) * 60
        job_id = _job_id(profile, prefs["connection"], date)
        job = _find(state, job_id)

        if job is not None and job["state"] not in OPEN_STATES:
            if _reconcile(job, service, wall):
                _save(path, state)
            return _report(profile, job["state"], job.get("detail") or "", job=job, date=date)
        if wall < due:
            return _report(profile, "waiting", "today's brief is due at %s" % prefs["at"],
                           date=date, next_at=due, job=job)

        row, refusal = _owner(service, prefs)
        if refusal:
            # A changed owner or channel never redirects a private brief silently.
            state["paused"] = {"reason": refusal, "at": wall}
            _save(path, state)
            return _report(profile, "needs_reconfigure", refusal, needs=True, date=date, job=job)

        if wall >= window_end:
            # Past the morning: skip this date explicitly.  No backfill, ever.
            if job is None:
                job = {"id": job_id, "date": date, "connection": prefs["connection"],
                       "result_id": "", "state": "skipped", "attempts": 0,
                       "detail": ("Collie was not running during this morning's send "
                                  "window, so today's brief was not emailed"),
                       "created": wall, "updated": wall}
                state["jobs"].append(job)
            elif not _reconcile(job, service, wall):
                # The clock closes the window; it does not decide what happened.  A
                # brief the provider accepted while the window was closing is settled
                # by the outbox first, so "expired" is only ever said about a draft
                # that really is still sitting there unsent.
                job.update(state="expired", updated=wall,
                           detail=("the morning window closed before this could be "
                                   "sent; the draft is in the outbox"))
            _save(path, state)
            return _report(profile, job["state"], job["detail"], job=job, date=date)

        blocked = _ready(row)
        if blocked:
            # Transient: nothing is settled and nothing claims to have been sent.
            return _report(profile, "blocked", blocked, date=date, job=job)

        if job is None:
            try:
                brief, subject, text = _render(root, prefs, profile, wall)
            except (ScheduleError, daily_brief.BriefError, OSError) as exc:
                return _report(profile, "error", str(exc)[:300], date=date)
            if brief["date"] != date:
                return _report(profile, "error", "the brief was built for a different "
                               "day than the schedule; nothing was sent", date=date)
            job = {"id": job_id, "date": date, "connection": prefs["connection"],
                   "result_id": _result_id(profile, prefs["connection"], date),
                   # The consent this payload was frozen under, so the pass that
                   # finally calls a provider can prove it still holds.
                   "kind": prefs["kind"], "owner_digest": prefs["owner_digest"],
                   "state": "preparing", "detail": "", "attempts": 0,
                   "brief_id": brief["id"], "brief_date": brief["date"],
                   "message_id": _message_id(row, job_id), "subject": subject, "text": text,
                   "text_bytes": len(text.encode("utf-8")), "created": wall, "updated": wall}
            state["jobs"].append(job)
            _save(path, state)                        # ledger first, always
        frozen = dict(job)

    # Outside the ledger lock: the outbox and the provider own their own locking,
    # and a slow provider must not block a settings read.
    try:
        result = _outbox(service, prefs, row, frozen)
    except (comms.CommsError, ScheduleError) as exc:
        detail = str(exc)[:300]
        _edit(path, lambda st: (_find(st, job_id) or {}).update(
            state="error", detail=detail, updated=wall))
        return _report(profile, "error", detail, date=date)

    adopted = result.pop("reused_payload", None)
    if adopted:
        # A draft that was already on disk is what a person will receive, so the
        # ledger describes *that*, not the render this pass happened to make.
        _edit(path, lambda st: (_find(st, job_id) or {}).update(adopted))
        frozen.update(adopted)

    if result["state"] != "pending" or frozen["attempts"] >= MAX_ATTEMPTS:
        # Already claimed, already settled, or out of attempts: report what the
        # outbox says and re-send nothing.  ``unknown`` is never retried.
        name, detail = _settle(path, job_id, result, wall)
        return _report(profile, name, detail, date=date, job=_peek(path, job_id))

    attempts, refused = _edit(path, lambda st: _claim(st, job_id, wall, frozen))
    if refused:
        return _report(profile, "blocked", refused, date=date, job=_peek(path, job_id))
    if attempts is None:
        return _report(profile, "sending", "another pass is already sending today's "
                       "brief", date=date)
    try:
        sent = service.send(frozen["connection"], frozen["result_id"]) or {}
    except comms.StateConflict:
        # Claimed, cancelled or settled by somebody else between the read and the
        # claim.  Which of those it was is in the outbox; this reports that rather
        # than guessing, and re-sends nothing either way.
        current = _stored(service, frozen["connection"], frozen["result_id"])
        name, detail = _settle(path, job_id, current or {"state": "sending"}, wall)
        return _report(profile, name, detail, date=date, job=_peek(path, job_id))
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        # Raised before the outbox was claimed, so the draft is still pending and
        # the next pass inside the window may try again.
        detail = str(exc)[:300] if isinstance(exc, (ValueError, comms.CommsError)) else \
            "the brief could not be handed to the mail account"
        _edit(path, lambda st: (_find(st, job_id) or {}).update(
            state="queued", detail=detail, updated=wall))
        return _report(profile, "blocked", detail, date=date)

    # The outbox, not the return value, is what this reports: it is the record that
    # survived the call, and a settled state there is the only evidence there is.
    current = _stored(service, frozen["connection"], frozen["result_id"]) or sent
    name, detail = _settle(path, job_id, current, wall)
    return _report(profile, name, detail, date=date, sent=name == "submitted",
                   job=_peek(path, job_id))


def _adopt(existing, job, row):
    """Prove a row that is already under this id is *this* brief, for *this* account.

    The result id is derived from the profile, connection and date, so a ledger that
    was repaired -- or a job trimmed by retention -- re-derives the same id for the
    same morning and finds a draft waiting.  Re-using it is the point; treating
    whatever is there as today's brief is not.  A row that is not the scheduler's
    own, not this job's, or addressed anywhere other than the frozen owner is
    somebody else's message, and this module will not hand it to a provider.

    Returns the ledger corrections needed when the stored draft is an older render
    than this pass produced: the stored one is what will actually be sent, so that
    is what the record has to say.
    """
    if existing.get("compacted"):                     # settled long ago, text gone
        return {}
    metadata = existing.get("metadata") or {}
    if (metadata.get("source") != "daily_brief" or metadata.get("job_id") != job["id"]
            or metadata.get("auto_eligible") is not False):
        raise ScheduleError("a different message is already stored under today's brief "
                            "id, so nothing was sent")
    if existing.get("destination") != row.get("owner"):
        raise ScheduleError("the draft already stored for today was addressed to "
                            "another account, so nothing was sent")
    if metadata.get("brief_id") == job.get("brief_id"):
        return {}
    return {"brief_id": str(metadata.get("brief_id") or "")[:120],
            "brief_date": str(metadata.get("brief_date") or "")[:10],
            "text_bytes": int(existing.get("text_bytes") or 0),
            "detail": "today's brief was already prepared and is sent as it was stored"}


def _outbox(service, prefs, row, job):
    """The durable draft for this day, created once from the frozen payload.

    Re-entered after a crash this returns the row that already exists; it never
    rebuilds the text under the same id, so what a person eventually receives is
    what the ledger says was prepared.
    """
    existing = comms.get_result(job["connection"], job["result_id"],
                                directory=service.directory, include_private=True)
    if existing is not None:
        # The private read is for proof, not for reporting: only the state and the
        # size of what is already stored ever leave this function.
        adopted = _adopt(existing, job, row)
        out = {"state": existing.get("state") or "unknown"}
        if adopted:
            out["reused_payload"] = adopted
        return out
    comms.create_result(
        job["connection"], job["result_id"], destination=row["owner"],
        text=job["text"], subject=job["subject"],
        # Named here, not left blank: a reply to this brief carries the outbound
        # Message-ID (or the one the provider assigned) in its references, and
        # ``ChannelService._thread`` can only follow that back to this thread when
        # the outbound row actually has a key.  Without it a "show me the first
        # item" starts a conversation about nothing.
        thread_key=_thread_key(job["id"]),
        metadata={"message_id": job["message_id"], "speak": False,
                  # Never the channel pump's to send: this is the scheduler's draft.
                  "auto_eligible": False, "source": "daily_brief",
                  "job_id": job["id"], "brief_id": job.get("brief_id", ""),
                  "brief_date": job.get("brief_date", ""), "language": prefs["language"]},
        directory=service.directory)
    return {"state": (comms.get_result(job["connection"], job["result_id"],
                                       directory=service.directory)
                      or {}).get("state") or "pending"}


def _fence(state, frozen, wall):
    """Is the consent this draft was frozen under still the consent on disk?  ``""`` if so.

    Everything between rendering the brief and handing it to a provider happens with
    no lock held, which is right -- a slow provider must not block a settings read --
    but it means a person can switch the daily email off, or point it at another
    account, while a brief they agreed to yesterday is halfway out.  The settings
    read at the top of the pass is a *snapshot*, and a snapshot is not consent at the
    moment of sending, so it is checked again here, inside the ledger lock, in the
    same write that spends the attempt.

    Nothing is undone when this refuses: the frozen draft stays pending in the
    outbox, so switching the setting back on inside the same window sends that one
    message rather than building a second.
    """
    prefs = state["prefs"]
    if not prefs["enabled"] or state["paused"]:
        return ("the daily email was switched off while today's brief was being "
                "prepared, so nothing was sent")
    moved = ("the daily email settings changed while today's brief was being "
             "prepared, so nothing was sent to the account it was prepared for")
    if prefs["connection"] != frozen.get("connection"):
        return moved
    for key in ("kind", "owner_digest"):
        if frozen.get(key) and prefs[key] != frozen[key]:
            return moved
    try:
        zone = _zone(prefs["timezone"])
    except ScheduleError as exc:
        return str(exc)
    date, due = _slot(wall, zone, prefs["at"])
    if (date != frozen.get("date") or wall < due
            or wall >= due + int(prefs["grace_minutes"]) * 60):
        return ("today's send window moved or closed before this brief went out, so "
                "nothing was sent")
    return ""


def _claim(state, job_id, wall, frozen):
    """Spend one attempt, durably, before the provider is called.

    ``(attempts, "")`` on success; ``(None, "")`` when another pass already holds the
    day, and ``(None, why)`` when consent, the destination or the window moved
    underneath the prepared draft.
    """
    job = _find(state, job_id)
    if job is None or job["state"] not in OPEN_STATES:
        return None, ""
    refusal = _fence(state, frozen, wall)
    if refusal:
        job.update(state="queued", detail=refusal, updated=wall)
        return None, refusal
    attempts = int(job.get("attempts") or 0) + 1
    job.update(state="sending", attempts=attempts, updated=wall, detail="")
    return attempts, ""


_SETTLED = {
    "submitted": "the mail account accepted today's brief; delivery is not confirmed",
    "failed": "the mail account refused today's brief; it is kept for a manual retry",
    "unknown": "it is not known whether today's brief was sent; it will not be re-sent",
    "cancelled": "today's brief was cancelled before it was sent",
    "sending": "today's brief is being sent",
    "pending": "today's brief is prepared and waiting",
}


def _settle(path, job_id, result, wall):
    """Record what the outbox says, verbatim.  This module never upgrades a state."""
    name = str((result or {}).get("state") or "unknown")
    detail = _SETTLED.get(name, "today's brief is %s" % name)
    # A draft still pending is not a settled day: it stays open for the rest of
    # the window rather than being recorded as anything that happened.
    stored = "queued" if name == "pending" else name
    _edit(path, lambda st: (_find(st, job_id) or {}).update(
        state=stored, detail=detail, updated=wall))
    return stored, detail
