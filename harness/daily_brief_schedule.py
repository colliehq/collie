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
leaves them alone; the scheduler sends only its own pending draft, and when a
morning ends with that draft still unsent it withdraws it rather than leaving a
brief nobody will ever send sitting in the outbox for good.

Mounting (the parent owns the routes)::

    # GET  /api/brief/preferences -> daily_brief_schedule.preferences(root)
    # POST /api/brief/preferences -> daily_brief_schedule.configure(root, body)
    # from the existing channel pump / periodic tick:
    #     daily_brief_schedule.tick(root)      # a no-op until someone opted in

**The morning report can be the morning email instead.**  With ``report: true``
the same slot -- same consent, destination, window, job key, outbox and failure
rules -- carries the morning report (:mod:`morning_report`) as a designed HTML
email with its plain text beside it.  Because the job key is the day, a morning
gets the report *or* the brief, never both: switching after that morning's email
went changes tomorrow, not today.  The report takes minutes to build (a model call,
Gmail, GitHub), so it is built outside every lock, under a lease written to the
ledger first: a second pass that arrives while it is being built waits, a build
that fails is tried again later, spaced (``BUILD_RETRY_S``, at most ``MAX_BUILDS``
builds a day), and when none succeeds -- or the next try would miss the window --
that morning's plain brief goes instead, in the same job and outbox id.  Once built
the rendered email is frozen in the ledger exactly as the brief's text is.  A
restart sends the frozen report; it never builds a second one.

**Send me one now** (:func:`send_now`) is a real send of a freshly built report to
the same frozen destination, on an explicit request: its own job (never the
morning's slot), at most one on its way and ``NOW_PER_DAY`` a local day.

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

from . import communications as comms, daily_brief, mail_messages, sessions

SCHEMA = "collie.daily_brief.schedule/1"
STATE_SCHEMA = "collie.daily_brief.schedule.state/1"

#: Mail only.  A brief read aloud down a phone line is not a thing we will do.
KINDS = ("imap", "collie_mail")
LANGUAGES = ("en", "zh")
FIELDS = ("enabled", "connection", "timezone", "at", "language", "grace_minutes", "report",
          "gmail_drafts")
#: What a morning's email carries.  One of these per local day, never both.
CONTENTS = ("brief", "morning_report")

#: A report being built holds the day this long; a pass that finds it held waits.
BUILD_LEASE_S = 10 * 60
#: Builds of one morning's report, ever.  After that the morning gets the plain brief.
MAX_BUILDS = 3
#: How long to wait after the first and after the second failed build: spaced, so a bug
#: that fails every time costs at most MAX_BUILDS builds a day, never one a tick.
BUILD_RETRY_S = (10 * 60, 20 * 60)
#: "Send me one now": at most this many a local day, and one on its way at a time.
NOW_PER_DAY = 3
#: A requested send still open after this long was abandoned by a process that died.
NOW_STALE_S = 20 * 60
DRAFTS_SETTING = "REPORT_GMAIL_DRAFTS"

DEFAULT_AT = "07:30"
DEFAULT_GRACE_MINUTES = 240               # "this morning", not "some time today"
MIN_GRACE_MINUTES, MAX_GRACE_MINUTES = 15, 12 * 60
MAX_ATTEMPTS = 3                          # per job, ever; a failure waits for a person
#: Who the outbox records as having withdrawn an abandoned day's draft.
DISCARD_ACTOR = "daily-brief-schedule"
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
             "updated": 0.0, "opted_in_at": 0.0, "report": False}


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
            job = {k: v for k, v in job.items()
                   if k not in ("text", "subject", "html", "inline", "build_token")}
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


def _request_ids(profile, connection, date, number):
    """``(job id, outbox id)`` for the ``number``-th report sent on request that day.

    Never the morning's own ids: a report asked for at six is a different send from the
    one the schedule makes at half past seven, and neither may stand in for the other.
    """
    job_id = "morning-report-request:%s:%s:%s:%d" % (profile, connection, date, number)
    return job_id, "report-request-%s-%d-%s" % (date, number, _digest(job_id)[:12])


def _find(state, job_id):
    for job in state["jobs"]:
        if job.get("id") == job_id:
            return job
    return None


def _prepared(job):
    """Has this job's email been rendered and frozen?  A report being built has not."""
    return isinstance(job, dict) and "text" in job


def _new_job(job_id, result_id, date, prefs, row, wall, *, trigger="schedule"):
    return {"id": job_id, "date": date, "connection": prefs["connection"],
            "result_id": result_id,
            # The consent this payload is frozen under, so the pass that finally calls a
            # provider can prove it still holds.
            "kind": prefs["kind"], "owner_digest": prefs["owner_digest"],
            "state": "preparing", "detail": "", "attempts": 0, "trigger": trigger,
            "message_id": _message_id(row, job_id), "created": wall, "updated": wall}


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


def _brief_payload(root, prefs, profile, wall, date):
    brief, subject, text = _render(root, prefs, profile, wall)
    if brief["date"] != date:
        raise ScheduleError("the brief was built for a different day than the schedule; "
                            "nothing was sent")
    return {"content": "brief", "brief_id": brief["id"], "brief_date": brief["date"],
            "subject": subject, "text": text, "text_bytes": len(text.encode("utf-8"))}


def _designed(report, rendered):
    """``(html, stored inline images, why not)`` -- the designed email, or none and why.

    The page and the dog are checked by the rules the composer and the outbox apply, so
    a report that is frozen is a report that can be sent.  A page too large for email is
    tried once more without the dog; failing that the plain text goes alone, which a
    reader can still use, and the reason is kept with the day.
    """
    from . import morning_report_email
    problem = ""
    for avatar in ("cid", "none"):
        if avatar == "none":
            try:
                rendered = morning_report_email.render(report, avatar="none")
            except Exception:                         # noqa: BLE001 - a fallback of a fallback
                break
        html = rendered.get("html") or ""
        try:
            mail_messages.check_html(html)
            return html, mail_messages.encode_inline(rendered.get("inline") or [], html), ""
        except mail_messages.MailFormatError as exc:
            # The first refusal is the one that describes the designed email as written.
            problem = problem or str(exc)
    return "", [], ("the designed report could not be sent as an email (%s), so its plain "
                    "text was sent" % (problem or "it could not be rendered"))[:300]


def _report_payload(root, prefs, wall, date):
    """Build this morning's report and render the email it becomes.  Minutes, not seconds.

    Built in the schedule's own zone and language, so the report is dated the day it is
    sent and written in the language the person chose for the email.  Reply drafts are
    made per the ``REPORT_GMAIL_DRAFTS`` setting; nothing here can send one.
    """
    from . import morning_report, morning_report_email
    profile = morning_report.load_profile(root)
    profile.update(zone=_zone(prefs["timezone"]), timezone=prefs["timezone"],
                   language=prefs["language"])
    report = morning_report.build(state_dir=root, now=wall, drafts=True, profile=profile)
    if not isinstance(report, dict) or report.get("date") != date:
        raise ScheduleError("it was built for a different day than the schedule")
    rendered = morning_report_email.render(report)
    subject = _clip(rendered.get("subject") or "", SUBJECT_BYTES, "…")
    text = _clip(rendered.get("text") or "", TEXT_BYTES)
    if not subject.strip() or not text.strip():
        raise ScheduleError("it rendered an empty email")
    html, inline, why = _designed(report, rendered)
    content = hashlib.sha256("\n".join((subject, text, html)).encode("utf-8")).hexdigest()[:16]
    return {"content": "morning_report", "brief_id": "report-%s-%s" % (date, content),
            "brief_date": date, "subject": subject, "text": text,
            "text_bytes": len(text.encode("utf-8")), "html": html, "inline": inline,
            "html_bytes": len(html.encode("utf-8")), "format": "html" if html else "text",
            "format_detail": why}


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
            # What the day carries and how it went out: the morning report or the brief,
            # scheduled or asked for, as a designed email or as its plain text -- and why.
            "content": job.get("content") or "brief",
            "trigger": job.get("trigger") or "schedule",
            "format": str(job.get("format")
                          or ("text" if (job.get("content") or "brief") == "brief" else "")),
            "format_detail": str(job.get("format_detail") or "")[:300],
            "html_bytes": int(job.get("html_bytes") or 0),
            # Provider acceptance is not receipt, and this never claims otherwise.
            "submission_known": job.get("state") == "submitted",
            "delivery_known": False}


def _drafts():
    """``(on, locked)`` for the reply-drafts switch the morning report obeys.

    ``locked`` means an environment variable the person set holds it, so saving a new
    value here would change nothing -- which is refused rather than pretended.
    """
    from . import settings
    value = str(settings.get(DRAFTS_SETTING, "on") or "on").strip().lower()
    return value in ("on", "1", "true", "yes"), not settings.owns(DRAFTS_SETTING)


def _write_drafts(value):
    from . import settings
    on, locked = _drafts()
    if value == on:
        return
    if locked:
        raise ScheduleError("writing reply drafts is set by the COLLIE_%s environment variable, "
                            "so it cannot be changed here" % DRAFTS_SETTING)
    settings.update({DRAFTS_SETTING: "on" if value else "off"})
    settings.apply()                                  # this process reads the new value too


def _bool_field(body, key, what):
    value = body.get(key)
    if not isinstance(value, bool):
        raise ScheduleError("%s must be true or false" % what)
    return value


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
    drafts, locked = _drafts()
    out = {"schema": SCHEMA, "profile": profile, "available": True, "error": "",
           "enabled": bool(prefs["enabled"]), "connection": prefs["connection"],
           "kind": prefs["kind"], "destination_masked": prefs["destination_masked"],
           "timezone": prefs["timezone"], "at": prefs["at"], "language": prefs["language"],
           "grace_minutes": int(prefs["grace_minutes"]),
           "opted_in_at": prefs["opted_in_at"], "updated": prefs["updated"],
           "report": bool(prefs["report"]),
           "content": "morning_report" if prefs["report"] else "brief",
           "gmail_drafts": drafts, "gmail_drafts_locked": locked,
           "needs_reconfigure": bool(state["paused"]),
           "paused_reason": str((state["paused"] or {}).get("reason") or "")[:300],
           "defaults": {"at": DEFAULT_AT, "grace_minutes": DEFAULT_GRACE_MINUTES,
                        "grace_range": [MIN_GRACE_MINUTES, MAX_GRACE_MINUTES]},
           "languages": list(LANGUAGES), "kinds": list(KINDS), "contents": list(CONTENTS),
           "jobs": [_public_job(job) for job in state["jobs"][-7:]],
           "notices": list(notices)}
    scheduled = [job for job in state["jobs"] if (job.get("trigger") or "schedule") == "schedule"]
    asked = [job for job in state["jobs"] if job.get("trigger") == "request"]
    # "Last email" is the morning's own; a report sent on request is shown on its own line.
    out["last"] = _public_job(scheduled[-1]) if scheduled else None
    out["requests"] = {"per_day": NOW_PER_DAY, "last": _public_job(asked[-1]) if asked else None,
                       # Only a saved account can be sent one: there is nowhere else to send it.
                       "available": bool(prefs["connection"] and prefs["owner_digest"]
                                         and not state["paused"])}
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
    if "report" in body:
        out["report"] = _bool_field(body, "report", "sending the morning report")
    return out


def configure(root, body, *, profile="default", now=None, service=None):
    """Turn the daily email on or off.  Enabling freezes the destination.

    ``body`` is ``{"enabled": bool}`` plus, when enabling, ``connection`` and
    ``timezone`` (IANA), and optionally ``at`` (``"HH:MM"``), ``language``
    (``en``/``zh``) and ``grace_minutes``.  There is no recipient field: the brief
    goes to the chosen connection's owner address and nowhere else, and no profile
    either: the profile is the caller's to state, and a body naming a different one
    is refused rather than quietly saved somewhere the person is not looking.

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
    if "profile" in body and body["profile"] != profile:
        # The caller, not the body, says which profile is being written: the page
        # has no profile picker and the matching read takes no profile either, so a
        # body that names a different one would save a person's settings where they
        # would never see them again.  Naming the one already being written is fine.
        raise ScheduleError("this request cannot save the daily email to another profile")
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ScheduleError("enabled must be true or false")
    drafts = _bool_field(body, "gmail_drafts", "writing reply drafts in Gmail") \
        if "gmail_drafts" in body else None
    report = _bool_field(body, "report", "sending the morning report") \
        if "report" in body else None
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
            if drafts is not None:
                _write_drafts(drafts)
            state["prefs"]["enabled"] = False
            if report is not None:
                state["prefs"]["report"] = report
            state["prefs"]["updated"] = wall
            state["paused"] = {}
            _save(path, state)
            return _public(state, profile, service=service, notices=notices)

        connection = body.get("connection", state["prefs"]["connection"])
        if not isinstance(connection, str) or not connection.strip():
            raise ScheduleError("choose the email account the brief should be sent from")
        prefs = _validate(body, state["prefs"])
        row = _connection(service, connection.strip())
        if drafts is not None:
            # A global setting (the report's own builds read it too), so it is written only
            # once everything else in this request has been accepted.
            _write_drafts(drafts)
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
    if actual == "cancelled" and job.get("discarded"):
        # This module withdrew that draft itself when the day was abandoned.  The
        # reason the day ended is the record; re-reading it back off the outbox
        # would overwrite it with the consequence.
        return False
    job.update(state=actual, detail=_SETTLED.get(actual, "today's brief is %s" % actual),
               updated=wall)
    return True


def _discard(service, job):
    """Withdraw this job's own unsent draft.  ``discarded``, ``kept`` or ``""``.

    A day that is over will never be emailed, and the draft it prepared should not
    outlive it: left there it is a brief the person is shown forever, and enough of
    them eventually fill the outbox against messages that could still be sent.

    What it will not touch is anything that is not demonstrably this job's own
    unsent draft.  A row that is ``sending`` may be in a provider's hands already, a
    ``submitted``, ``failed`` or ``unknown`` one is the only evidence of what
    happened, and a row that is not the scheduler's, or is another job's, was never
    ours.  If the outbox refuses -- because a pass claimed it between the read and
    the write, or it is simply gone -- that is reported as ``kept``: this claims no
    cancellation it did not actually make.
    """
    stored = _stored(service, job.get("connection"), job.get("result_id"), private=True)
    if not stored or stored.get("compacted"):
        return ""
    metadata = stored.get("metadata") or {}
    if (metadata.get("source") != "daily_brief" or metadata.get("job_id") != job.get("id")
            or metadata.get("auto_eligible") is not False):
        return ""
    if stored.get("state") != "pending":
        return "kept"
    try:
        out = comms.cancel_result(job["connection"], job["result_id"], actor=DISCARD_ACTOR,
                                  expected_digest=stored.get("digest"),
                                  directory=service.directory)
    except (comms.CommsError, OSError, ValueError):
        return "kept"
    return "discarded" if (out or {}).get("state") == "cancelled" else "kept"


def _abandon(job, service, wall, reason):
    """Close an open job for good, and take its unsent draft out of the outbox with it.

    The outbox settles the day first: a brief the provider accepted while the window
    was closing is ``submitted``, not abandoned, and nothing of it is withdrawn.
    """
    if _reconcile(job, service, wall):
        return False
    fate = _discard(service, job)
    job.update(state="expired", updated=wall,
               detail=reason + {"discarded": "; the unsent draft was discarded",
                                "kept": "; the draft is in the outbox"}.get(fate, ""))
    if fate == "discarded":
        job["discarded"] = True
    return fate == "discarded"


def _sweep(state, service, wall, today):
    """Abandon every open job left over from a local day that is already over.

    The pass that closes today's window handles today; this is the rest of the story
    -- a machine that was off from the send window until tomorrow, or a person who
    switched the daily email off and left a prepared brief behind.  Neither will ever
    be sent, and without this neither is ever cleared up.
    """
    changed = False
    for job in state["jobs"]:
        if job.get("state") in OPEN_STATES and str(job.get("date") or "") < today:
            _abandon(job, service, wall, "this morning was over before the brief was sent")
            changed = True
    return changed


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
        if any(job.get("state") in OPEN_STATES for job in state["jobs"]):
            # Days that ended are cleared up before anything else is decided, and
            # whether the daily email is still on has no bearing on it: a draft
            # nobody will ever send does not become sendable by being forgotten.
            try:
                so_far = _slot(wall, _zone(prefs["timezone"]), prefs["at"])[0]
            except (ScheduleError, ValueError):        # unreadable clock, nothing to do
                so_far = ""
            if so_far:
                service = _service(root, service)
                if _sweep(state, service, wall, so_far):
                    _save(path, state)
        if not prefs["enabled"]:
            # Switched off with nothing left over: this reads one small file and
            # stops.  No connection is opened, no outbox is touched and no provider
            # is asked anything.
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
                       "content": "morning_report" if prefs["report"] else "brief",
                       "detail": ("Collie was not running during this morning's send "
                                  "window, so today's brief was not emailed"),
                       "created": wall, "updated": wall}
                state["jobs"].append(job)
            else:
                # The clock closes the window; it does not decide what happened.  A
                # brief the provider accepted while the window was closing is settled
                # by the outbox first, so "expired" is only ever said about a draft
                # that really is still sitting there unsent -- and that draft is
                # withdrawn with the day, never left pending for a morning that has
                # already passed.
                _abandon(job, service, wall,
                         "the morning window closed before this could be sent")
            _save(path, state)
            return _report(profile, job["state"], job["detail"], job=job, date=date)

        blocked = _ready(row)
        if blocked:
            # Transient: nothing is settled and nothing claims to have been sent.
            return _report(profile, "blocked", blocked, date=date, job=job)

        token = fallback = ""
        if not _prepared(job):
            if prefs["report"]:
                # The report is built outside this lock, under a lease written first:
                # a pass that finds the day already being built leaves it alone.
                job, waiting, fallback = _lease(
                    state, job, prefs, row, job_id,
                    _result_id(profile, prefs["connection"], date), date, wall)
                _save(path, state)
                if waiting:
                    return _report(profile, job["state"], waiting, date=date, job=job)
                token = "" if fallback else job["build_token"]
            if not token:
                try:
                    payload = _brief_payload(root, prefs, profile, wall, date)
                except (ScheduleError, daily_brief.BriefError, OSError) as exc:
                    return _report(profile, "error", str(exc)[:300], date=date)
                if job is None:
                    job = _new_job(job_id, _result_id(profile, prefs["connection"], date),
                                   date, prefs, row, wall)
                    state["jobs"].append(job)
                # A report that was still being built becomes the brief the person
                # switched to -- or the brief a morning whose report could not be built
                # gets instead: nothing of the report was frozen, so none of it can go.
                job.pop("build_token", None)
                job.update(payload, detail=fallback, updated=wall)
                if fallback:
                    job.update(format="text", format_detail=fallback)
                _save(path, state)                    # ledger first, always
        frozen = dict(job)

    if token:
        frozen, answer = _build(root, path, profile, prefs, job_id, token, date, wall,
                                window_end=window_end)
        if answer is not None:
            return answer
    return _deliver(path, profile, service, prefs, row, frozen, job_id, date, wall)


def _instead(cause):
    return ("the morning report could not be built (%s), so today's Daily Brief was sent "
            "instead" % cause)[:300]


def _lease(state, job, prefs, row, job_id, result_id, date, wall, *, trigger="schedule"):
    """``(job, "", "")`` holding a fresh build lease, ``(job, why not now, "")``, or
    ``(job, "", why the brief goes instead)``.

    The lease is the only thing that stops two passes -- a pump and a restarted app, or
    two ticks a minute apart around a slow model -- from building the same morning twice.
    A lease that runs out belonged to a pass that died or failed; the next pass takes it,
    up to ``MAX_BUILDS`` builds for the day.  After that the morning is not lost: a
    scheduled day gets the plain brief instead.
    """
    if job is None:
        job = _new_job(job_id, result_id, date, prefs, row, wall, trigger=trigger)
        job.update(content="morning_report", builds=0, build_started=0.0)
        state["jobs"].append(job)
    if wall < float(job.get("build_started") or 0.0) + BUILD_LEASE_S:
        return job, str(job.get("detail") or "today's morning report is being built"), ""
    builds = int(job.get("builds") or 0)
    if builds >= MAX_BUILDS:
        # The last build died without saying why (a crash holds no exception).
        job.pop("build_token", None)
        return job, "", _instead(job.get("build_error") or "its last build did not finish")
    job.update(content="morning_report", builds=builds + 1, build_started=wall,
               build_token=os.urandom(8).hex(), detail="today's morning report is being built",
               updated=wall)
    return job, "", ""


def _build(root, path, profile, prefs, job_id, token, date, wall, *, window_end=None):
    """Build the report with no lock held, then freeze it into the job holding ``token``.

    ``(frozen job, None)`` when it is ready to send, or ``(None, report)`` when there is
    nothing to send this pass.  ``window_end`` is the scheduled morning's: a failed build
    is tried again later, spaced (``BUILD_RETRY_S``), and when builds run out -- or the
    next try would miss the window -- that morning's plain brief is frozen instead, in the
    same job and outbox id.  Without it (a report asked for on request) a failed build
    ends the request: it was a report that was asked for, not the brief.
    """
    try:
        payload = _report_payload(root, prefs, wall, date)
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        cause = str(exc)[:200] if isinstance(exc, ScheduleError) else type(exc).__name__
        job, instead = _edit(path, lambda st: _build_failed(
            st, job_id, token, wall, cause, isinstance(exc, ScheduleError), window_end))
        if not instead:
            return None, _report(profile, "error", str((job or {}).get("detail") or cause),
                                 date=date, job=job)
        try:
            payload = _brief_payload(root, prefs, profile, wall, date)
        except (ScheduleError, daily_brief.BriefError, OSError) as brief_exc:
            return None, _report(profile, "error", "%s; the Daily Brief could not be rendered "
                                 "either (%s)" % (_instead(cause), str(brief_exc)[:120]),
                                 date=date, job=job)
        payload.update(format="text", format_detail=_instead(cause))

    def freeze(state):
        job = _find(state, job_id)
        if (job is None or job.get("build_token") != token or _prepared(job)
                or job.get("state") != "preparing"):
            return None
        job.pop("build_token", None)
        job.update(payload, detail=payload.get("format_detail") if payload.get(
            "content") == "brief" else "", updated=wall)
        return dict(job)

    frozen = _edit(path, freeze)
    if frozen is None:
        # Another pass settled or re-prepared the day while this one was building.
        current = _peek(path, job_id)
        return None, _report(profile, (current or {}).get("state") or "error",
                             "today's email was prepared by another pass", date=date,
                             job=current)
    return frozen, None


def _build_failed(state, job_id, token, wall, cause, final, window_end):
    """Record a failed build.  ``(job, True)`` when the brief should go instead now."""
    job = _find(state, job_id)
    if job is None or job.get("build_token") != token or _prepared(job):
        return (dict(job) if job else None), False
    job.update(build_error=cause, updated=wall)
    if window_end is None:
        job.pop("build_token", None)
        job.update(state="error", detail="the morning report could not be built (%s), so "
                                         "nothing was sent" % cause)
        return dict(job), False
    builds = int(job.get("builds") or 0)
    retry_at = wall + BUILD_RETRY_S[min(max(builds, 1), len(BUILD_RETRY_S)) - 1]
    if final or builds >= MAX_BUILDS or retry_at >= window_end:
        # The token stays so the brief can be frozen into this very job.
        job.update(detail=_instead(cause))
        return dict(job), True
    # The next build waits: the lease is moved to end when the retry is due, so a failure
    # that repeats costs one build per spacing, not one per tick of the pump.
    job.update(build_started=retry_at - BUILD_LEASE_S,
               detail="the morning report could not be built (%s); it will be tried again in "
                      "%d minutes" % (cause, (retry_at - wall) // 60))
    return dict(job), False


def _deliver(path, profile, service, prefs, row, frozen, job_id, date, wall):
    """Ledger -> outbox -> provider for one frozen email.  The same for every kind of day.

    Outside the ledger lock: the outbox and the provider own their own locking, and a
    slow provider must not block a settings read.
    """
    try:
        result = _outbox(service, prefs, row, frozen)
    except (comms.CommsError, ScheduleError, mail_messages.MailFormatError) as exc:
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
    # survived the call, and a settled state there is the only evidence there is.  The
    # private read is for one fact only: whether the transport sent the page or, like a
    # Collie Mail relay that carries no HTML yet, only the plain text.
    current = _stored(service, frozen["connection"], frozen["result_id"], private=True) or sent
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
    metadata = {"message_id": job["message_id"], "speak": False,
                # Never the channel pump's to send: this is the scheduler's draft.
                "auto_eligible": False, "source": "daily_brief",
                "job_id": job["id"], "brief_id": job.get("brief_id", ""),
                "brief_date": job.get("brief_date", ""), "language": prefs["language"]}
    design = {}
    if job.get("content") == "morning_report":
        # What a reply to this email is about, for daily_brief_reply: the report (which
        # a "mute <project>" line may answer), not the plain brief.
        metadata["content"] = "morning_report"
        if job.get("trigger") == "request":
            metadata["trigger"] = "request"
        if job.get("html"):
            design = {"html": job["html"],
                      "inline": mail_messages.decode_inline(job.get("inline") or [], job["html"])}
    comms.create_result(
        job["connection"], job["result_id"], destination=row["owner"],
        text=job["text"], subject=job["subject"],
        # Named here, not left blank: a reply to this brief carries the outbound
        # Message-ID (or the one the provider assigned) in its references, and
        # ``ChannelService._thread`` can only follow that back to this thread when
        # the outbound row actually has a key.  Without it a "show me the first
        # item" starts a conversation about nothing.
        thread_key=_thread_key(job["id"]),
        metadata=metadata, directory=service.directory, **design)
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


def _fence_request(state, frozen):
    """:func:`_fence` for a report sent on request: the same destination, and no window.

    Asked for with a click, so the morning's window has no say -- but the account it was
    asked for must still be the account on disk, unpaused, when the attempt is spent.
    """
    prefs = state["prefs"]
    if state["paused"]:
        return ("the daily email settings were paused while the report was being prepared, "
                "so nothing was sent")
    if prefs["connection"] != frozen.get("connection") or any(
            frozen.get(key) and prefs[key] != frozen[key] for key in ("kind", "owner_digest")):
        return ("the email account changed while the report was being prepared, so nothing "
                "was sent to the account it was prepared for")
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
    refusal = (_fence_request(state, frozen) if frozen.get("trigger") == "request"
               else _fence(state, frozen, wall))
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
    shape = {}
    if isinstance(result, dict) and result.get("format") and name == "submitted":
        # What reached the provider: the designed email, or -- when the transport said
        # so -- its plain text alone, with the transport's reason.
        note = str((result.get("outcome_detail") or {}).get("detail") or "")
        shape = ({"format": "text", "format_detail": note[:300]}
                 if note.startswith("sent as plain text") else {"format": result["format"]})
    _edit(path, lambda st: (_find(st, job_id) or {}).update(
        state=stored, detail=detail, updated=wall, **shape))
    return stored, detail


# ---------------------------------------------------------------- send me one now


def send_now(root, now=None, *, profile="default", service=None):
    """Build a morning report now and send it to the saved account.  A real email.

    Called from an explicit click ("Send me one now"), never from a timer.  It goes where
    the scheduled email goes -- the owner address of the connection saved in these
    settings, checked against the digest frozen at opt-in -- and nowhere else; with no
    saved account there is nowhere to send it, and it is refused.  Whether the daily
    email is switched on does not matter: the click is its own consent for one message.

    It is its own job, never the morning's: a report asked for at six does not stand in
    for the one the schedule sends at half past seven.  At most one is on its way at a
    time and ``NOW_PER_DAY`` go in a local day.  Refusals raise :class:`ScheduleError`
    (nothing was built or sent); otherwise the answer is the same report a tick gives.
    """
    wall = float(now) if now is not None else time.time()
    path = _path(root, profile)
    service = _service(root, service)
    with sessions._locked(path):
        state, error = _load(path)
        if state is None:
            raise ScheduleError(error)
        prefs = state["prefs"]
        if not prefs["connection"] or not prefs["owner_digest"]:
            raise ScheduleError("choose an email account and save these settings first; "
                                "there is nowhere to send a report yet")
        if state["paused"]:
            raise ScheduleError("%s Choose an account and save again." % str(
                state["paused"].get("reason") or "the daily email needs setting up again")[:300])
        date = _slot(wall, _zone(prefs["timezone"]), prefs["at"])[0]
        for job in state["jobs"]:
            if job.get("trigger") != "request" or job.get("state") not in OPEN_STATES:
                continue
            if _reconcile(job, service, wall):
                continue
            if wall - float(job.get("updated") or job.get("created") or 0.0) < NOW_STALE_S:
                raise ScheduleError("a report you asked for is already on its way; wait for it "
                                    "before asking for another")
            # Left open by a process that died; it will never finish now.
            _abandon(job, service, wall, "the report asked for was not finished")
        today = [job for job in state["jobs"]
                 if job.get("trigger") == "request" and job.get("date") == date]
        if len(today) >= NOW_PER_DAY:
            _save(path, state)
            raise ScheduleError("at most %d reports on request a day; the next one can be sent "
                                "tomorrow" % NOW_PER_DAY)
        row, refusal = _owner(service, prefs)
        blocked = refusal or _ready(row)
        if blocked:
            _save(path, state)
            raise ScheduleError(blocked)
        job_id, result_id = _request_ids(profile, prefs["connection"], date, len(today) + 1)
        job, _waiting, _instead_of = _lease(state, None, prefs, row, job_id, result_id, date,
                                            wall, trigger="request")
        _save(path, state)
        token, snapshot = job["build_token"], dict(prefs)

    frozen, answer = _build(root, path, profile, snapshot, job_id, token, date, wall)
    if answer is not None:
        return answer
    return _deliver(path, profile, service, snapshot, row, frozen, job_id, date, wall)
