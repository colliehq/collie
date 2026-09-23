"""Durable waits for an already accepted follow-up, past a provider quota reset.

A run that dies on a quota error leaves the conversation in a specific, common
state: the person already accepted the *next* request, that request is sitting
pending in the durable inbox with its own frozen budget, and the provider has
said in machine metadata exactly when it will answer again.  Until now the only
thing that could act on that pair of facts was a person coming back to the tab
and pressing Start.

What this module adds is one durable record per session — "this one accepted
request is waiting for the reset at T" — and a bounded, server-owned pass that,
once T is reached, admits *that exact request* through the ordinary scheduling
path.  What it deliberately is not:

  * It does not continue the interrupted request.  That turn's spent budget is
    spent; its tools, its input and its unfinished work are not replayed, and no
    synthetic "continue" request is ever created.  The thing that starts is the
    distinct, separately accepted request that was already queued behind it,
    under the budget frozen when *it* was accepted.
  * It owns no policy.  Recovery fences, cancellation, capability and provider
    policy, the session lease and the inbox/journal reconciliation are all read
    again, live, by the normal path at admission time — this module only decides
    *when* to ask, never *whether* the answer may be yes.
  * It is not a retry loop.  A wait is admitted at most once; only a failure in
    this module's own bookkeeping is retried, a bounded number of times, and
    what stopped it is stored where a person can read it.
  * It does not expire.  A laptop that slept through the reset has not lost its
    place: nothing here turns "you were away" into "press Start again".  The
    authority that may refuse — the fence, the lease, the inbox — is re-read
    when the pass finally runs, which is the only reading that could be right.

Two failures decide the shape of everything below.

**A claimant can die.**  Moving a record to ``admitting`` is what stops two
passes racing toward the same admission, and a process killed one instruction
later would strand the record there forever if nothing could tell "in flight"
from "gone".  Nothing here guesses from a timeout.  The evidence is the same
pair the rest of the project already trusts: the session's OS run lease (the
kernel drops it when the holder dies — ``session_owner.probe_busy``) and the
inbox's own record of what happened to the bound entry.  An unreadable answer
from either is never read as "dead"; it is read as "not now".

**A successor can be newer.**  A record is identified by a ``nonce`` that
changes every time the wait is (re)written, and every state transition is a
compare-and-set on it.  So a stalled claimant that wakes up and retires "its"
wait cannot delete the wait a newer failing run recorded a second ago, and a
recovered record cannot be retired by the zombie it was recovered from.

The record lives in its own sessions sidecar directory, so a pass reads exactly
the sessions that have a wait and never walks unrelated user files.  A browser
tab owns none of it: the record is written before the failing run's stream
returns, and a webserver started ten minutes later finds it and catches up.

Everything here addresses the sessions store the process is configured for.
There is no ``directory=`` override on any call that writes, claims a lease or
executes: an override that reached the sidecar but not the lease and the inbox
would write bookkeeping in one store about work admitted in another.
"""
import bisect
import json
import os
import threading
import time
import uuid

from . import session_owner, sessions

SUBDIR = "quota_waits"
VERSION = 2
STATES = ("waiting", "admitting", "retired")

# A wait is a promise about one upcoming provider reset, not a standing job.
MAX_ATTEMPTS = 4                  # bookkeeping failures before the record retires
RETRY_BACKOFF = (30.0, 120.0, 600.0)
BUSY_BACKOFF = 60.0               # a conversation that was busy is asked again later
CLAIM_GRACE = 90.0                # a claim younger than this is assumed in flight
TICK_INTERVAL = 30.0              # how often the pass looks for due waits
MAX_DUE_PER_PASS = 32             # admissions attempted per pass
MAX_SCAN = 4096                   # records *examined* per pass, so sorting is fair
_MAX_BYTES = 64 * 1024


def _path(session):
    return session_owner.sidecar_path(session, SUBDIR, ".json")


def _finite(value):
    """A real, readable seconds value — never a bool, a string, a NaN or an int
    too big to be one.  The range is checked by comparison, which is total: a
    JSON integer of a thousand digits answers it without being converted to a
    float first (``math.isfinite`` would raise ``OverflowError`` on it, out of a
    reader whose whole contract is not to raise), and NaN and both infinities
    compare false against every bound.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 <= value < 2 ** 53


def _valid(record, session):
    """Accept only a record this module could have written.

    Strict about types rather than coercing them: a JSON ``true`` is not the
    epoch 1, ``"2000000000"`` is not a time, and a NaN compares false against
    every deadline, which would make a wait either immortal or instantly due.
    """
    if not isinstance(record, dict) or record.get("version") != VERSION:
        return None
    if record.get("session") != session:
        return None
    if record.get("state") not in STATES:
        return None
    retry_at = record.get("retry_at")
    if type(retry_at) is not int or not 0 < retry_at < 2 ** 53:
        return None
    for key in ("entry", "run", "reason", "state", "provider", "nonce"):
        value = record.get(key, "")
        if not isinstance(value, str) or len(value) > 400:
            return None
    if not record.get("nonce") or not record.get("entry"):
        return None                # a wait with nothing bound cannot admit anything
    for key in ("attempts", "deferrals"):
        value = record.get(key, 0)
        if type(value) is not int or not 0 <= value <= 1 << 20:
            return None
    for key in ("at", "next_attempt_at", "claimed_at"):
        if not _finite(record.get(key, 0.0)):
            return None
    claimed_by = record.get("claimed_by", 0)
    if type(claimed_by) is not int or not 0 <= claimed_by <= 1 << 31:
        return None
    if record["state"] == "admitting" and not record.get("claimed_at"):
        return None
    return record


def read(session):
    """The durable wait for one session, or None.  Never raises for a bad file."""
    try:
        path = _path(session)
    except ValueError:
        return None
    try:
        with sessions._locked(path):
            return _read_locked(path, session)
    except OSError:
        return None


def _read_locked(path, session):
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return _valid(record, session)


def _write_locked(path, record):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, allow_nan=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def record(session, *, retry_at, entry_id, run_id="", provider=""):
    """Persist "the accepted request ``entry_id`` waits for the reset at T".

    Written before the failing turn's stream returns, so a restart of the
    webserver — or a browser that closes the instant the error lands — changes
    nothing about whether the queued request is picked up.  The entry is bound
    here and never re-chosen later: if *that* request is withdrawn, the wait is
    over, because silently starting whatever else happens to be queued would be
    running something the person did not schedule.

    A wait already recorded for the same entry and a *later* reset is left
    alone: the newest provider statement about when it will answer is the one to
    believe, and re-recording an earlier one would admit while the door is still
    shut.  Anything else — a different entry, a claim in flight from a run that
    has plainly ended, since this call is made by the run that replaced it — is
    replaced, with a fresh nonce that retires the old holder's authority.
    """
    if type(retry_at) is not int or not 0 < retry_at < 2 ** 53:
        return None
    entry_id = str(entry_id or "")[:400]
    if not entry_id:
        return None
    path = _path(session)
    now = time.time()
    row = {"version": VERSION, "session": session, "retry_at": retry_at,
           "entry": entry_id, "run": str(run_id or "")[:128],
           "provider": str(provider or "")[:64], "state": "waiting",
           "at": now, "next_attempt_at": float(retry_at), "attempts": 0,
           "deferrals": 0, "reason": "", "claimed_at": 0.0, "claimed_by": 0,
           "nonce": uuid.uuid4().hex}
    with sessions._locked(path):
        existing = _read_locked(path, session)
        if (existing is not None and existing["state"] == "waiting"
                and existing["entry"] == entry_id
                and int(existing["retry_at"]) >= retry_at):
            return existing
        _write_locked(path, row)
    return row


def _transition(session, nonce, mutate, *, expect=None):
    """Compare-and-set one record: ``mutate`` runs only on the nonce it was for.

    Every write after the first goes through here.  Without the nonce check a
    pass that stalled — a long GC pause, a suspended laptop, a retried tick —
    could come back and retire, defer or delete a record that a newer failing
    run had since written, silently cancelling a wait nobody asked to cancel.
    """
    try:
        path = _path(session)
    except ValueError:
        return None
    try:
        with sessions._locked(path):
            row = _read_locked(path, session)
            if row is None or (nonce is not None and row.get("nonce") != nonce):
                return None
            if expect is not None and row["state"] not in expect:
                return None
            out = mutate(row, path)
            return dict(out) if isinstance(out, dict) else out
    except OSError:
        return None


def retire(session, reason, *, nonce=None, state="retired"):
    """Stop waiting, and leave why behind where it can be inspected.

    The record is kept rather than deleted for everything a person might want to
    ask about later ("why did my queued request not start?"); only a wait whose
    request actually *started* is cleared, because its answer is the transcript.
    """
    def _apply(row, path):
        if state == "admitted":
            try:
                os.remove(path)
            except OSError:
                pass
            return dict(row, state="retired", reason=str(reason or "")[:400])
        row.update(state="retired", reason=str(reason or "")[:400],
                   next_attempt_at=0.0, claimed_at=0.0, claimed_by=0)
        _write_locked(path, row)
        return row

    return _transition(session, nonce, _apply)


def clear(session, *, nonce=None, reason=""):
    """Forget the wait entirely — a newer run has superseded it.

    Called when any run takes this conversation's lease.  Whatever that run
    does next, including being canceled a second later, it is newer than this
    wait and the person is present for it; a timer must not start queued work
    behind a decision they have since made.
    """
    def _apply(row, path):
        try:
            os.remove(path)
        except OSError:
            return None
        return dict(row, state="cleared", reason=str(reason or "")[:400])

    return _transition(session, nonce, _apply)


WITHDRAWN_REASON = "the accepted request this was waiting for was withdrawn"


def withdraw(session, entry_id, *, reason=WITHDRAWN_REASON):
    """Retire this entry's waiting schedule, retaining its reason and nonce guard.

    An admitting claim owns its transition and revalidates under the session lease.
    """
    entry_id = str(entry_id or "")
    if not entry_id:
        return None
    row = read(session)
    if row is None or row["state"] != "waiting" or row["entry"] != entry_id:
        return None
    return retire(session, reason, nonce=row["nonce"])


def _bound_withdrawn(row):
    """Check other cancellation paths too; a missing/unreadable entry proves nothing."""
    from . import task_inbox
    try:
        entry = task_inbox.get(row["session"], row["entry"])
    except Exception:
        return False
    return bool(entry is not None and entry.get("state") == "canceled")


def defer(session, reason, *, nonce, now=None):
    """Put a claimed wait back, for a reason a later pass could resolve.

    Deferring is not failing and is never counted against the wait: a
    conversation that was busy at the reset is exactly the case this exists for.
    """
    now = time.time() if now is None else float(now)

    def _apply(row, path):
        row.update(state="waiting", deferrals=int(row.get("deferrals") or 0) + 1,
                   next_attempt_at=now + BUSY_BACKOFF, claimed_at=0.0, claimed_by=0,
                   reason=str(reason or "")[:400], nonce=uuid.uuid4().hex)
        _write_locked(path, row)
        return row

    return _transition(session, nonce, _apply, expect=("admitting",))


def failed(session, reason, *, nonce, now=None):
    """A claimed wait whose own bookkeeping failed: back off, then give up.

    Bounded, and the bound is about this module's failures only — the store
    would not write, the scheduler raised.  A refusal that a person owns is not
    routed here; it retires the wait with what refused it.
    """
    now = time.time() if now is None else float(now)

    def _apply(row, path):
        attempts = int(row.get("attempts") or 0) + 1
        row.update(attempts=attempts, reason=str(reason or "")[:400],
                   claimed_at=0.0, claimed_by=0, nonce=uuid.uuid4().hex)
        if attempts >= MAX_ATTEMPTS:
            row.update(state="retired", next_attempt_at=0.0)
        else:
            delay = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
            row.update(state="waiting", next_attempt_at=now + delay)
        _write_locked(path, row)
        return row

    return _transition(session, nonce, _apply, expect=("admitting",))


def claim(session, *, now=None):
    """Take exclusive responsibility for admitting this wait, or return None.

    The claim is the compare-and-set that makes a duplicate tick — or a second
    webserver process sharing the store — harmless: the file lock serialises the
    read/modify/write, and only the pass that moves the record out of ``waiting``
    goes on to admit.  The session lease still guards execution itself; this
    keeps two passes from *racing toward* it and double-counting attempts.
    """
    now = time.time() if now is None else float(now)

    def _apply(row, path):
        if now < int(row["retry_at"]) or now < float(row.get("next_attempt_at") or 0):
            return None
        row.update(state="admitting", claimed_at=now, claimed_by=os.getpid(),
                   nonce=uuid.uuid4().hex)
        _write_locked(path, row)
        return row

    return _transition(session, None, _apply, expect=("waiting",))


# ------------------------------------------------------------------- recovery

def claim_evidence(row, *, now=None):
    """Is the claimant of an ``admitting`` record gone, and what did it leave?

    Returns one of ``"in_flight"`` (assume alive; leave it alone), ``"unknown"``
    (nothing here could be read, which is never evidence of death),
    ``"abandoned"`` (nobody is executing and the bound request is still waiting),
    ``"delivered"`` (it reached an executor and the transcript owns the answer)
    or ``"gone"`` (the bound request is no longer there to start).

    Two independent sources, neither of them a timeout.  The session's run lease
    is an OS byte lock, so a claimant that was killed has already lost it; and
    the inbox says what became of the exact entry this wait is bound to.  The
    short grace covers the only window where a live claimant holds no lease —
    between taking the claim and acquiring the session.
    """
    from . import task_inbox
    now = time.time() if now is None else float(now)
    if now - float(row.get("claimed_at") or 0) < CLAIM_GRACE:
        return "in_flight"
    busy = session_owner.probe_busy(row["session"])
    if busy is None:
        return "unknown"           # the OS could not answer; that is not "dead"
    if busy:
        return "in_flight"
    try:
        entry = task_inbox.get(row["session"], row["entry"])
    except Exception:
        return "unknown"           # an inbox needing inspection is not a timer's job
    if entry is None:
        return "gone"
    state = entry.get("state")
    if state in ("pending", "claimed"):
        # `claimed` with no live lease is an abandoned claim; the admission path
        # reconciles it against the journal before it claims anything itself.
        return "abandoned"
    if state == "consumed":
        return "delivered"
    return "gone"


def recover(row, *, now=None):
    """Return a dead claimant's record to ``waiting``, or retire what it finished.

    The new nonce is the point: whatever the old claimant does if it turns out to
    be a zombie after all, it can no longer retire, defer or delete this record.
    """
    now = time.time() if now is None else float(now)
    verdict = claim_evidence(row, now=now)
    if verdict in ("in_flight", "unknown"):
        return None
    if verdict == "delivered":
        return retire(row["session"], "this request was already delivered",
                      nonce=row["nonce"])
    if verdict == "gone":
        return retire(row["session"], "the accepted request this was waiting for is "
                                      "no longer queued", nonce=row["nonce"])

    def _apply(record_row, path):
        record_row.update(state="waiting", next_attempt_at=now, claimed_at=0.0,
                          claimed_by=0, nonce=uuid.uuid4().hex,
                          reason="the process that was starting this stopped before "
                                 "it could; picked up again")
        _write_locked(path, record_row)
        return record_row

    return _transition(row["session"], row["nonce"], _apply, expect=("admitting",))


# ------------------------------------------------------------------- the scan

_SCAN_LOCK = threading.Lock()
_SCAN_CURSOR = ""                 # process-local: where the next bounded scan resumes


def _session_ids(*, advance=True):
    """At most ``MAX_SCAN`` ids, resuming where the last scan stopped.

    A pass must be bounded, and the listing is lexicographic while a session id
    says nothing about time — so a fixed "first N" window would let a few
    thousand waits whose ids sort early hide a wait that is due right now, every
    pass, forever.  The window therefore rotates: each scan starts after the id
    the previous one ended on and wraps, so every record is examined within
    ``ceil(total / MAX_SCAN)`` passes and a due one is reached on one of them.
    The cursor is process-local, which is all it needs to be — it orders this
    process's passes; correctness against another process is the record's own
    lock and nonce, not this.
    """
    global _SCAN_CURSOR
    try:
        base = session_owner.sidecar_dir(SUBDIR)
    except (ValueError, OSError):
        return []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    ids = []
    for name in names:
        if not name.endswith(".json"):
            continue
        sid = name[: -len(".json")]
        if not sessions._path(sid, directory=base):
            continue                   # not an id this module could have written
        ids.append(sid)
    if len(ids) <= MAX_SCAN:
        return ids                     # everything fits: nothing to be fair about
    with _SCAN_LOCK:
        start = bisect.bisect_right(ids, _SCAN_CURSOR)
        window = (ids[start:] + ids[:start])[:MAX_SCAN]
        if advance and window:
            _SCAN_CURSOR = window[-1]
    return window


def records(*, advance=True):
    """Every readable record in this scan's window, whatever its state.

    One tick reads this *once* and reuses the rows: two scans advancing the
    cursor in the same pass would hand the recovery half one window and the
    admission half the next, so a due record could be seen only by the half that
    cannot start it.
    """
    rows = []
    for sid in _session_ids(advance=advance):
        row = read(sid)
        if row is not None:
            rows.append(row)
    return rows


def waiting(*, now=None, limit=MAX_DUE_PER_PASS, due_only=False, rows=None):
    """Live waits, earliest reset first — sorted before anything is dropped.

    The bound is on how many admissions one pass attempts; which records a pass
    is allowed to *see* is bounded too, but fairly (see ``_session_ids``), so a
    due wait is never starved by ids that sort before it.
    """
    now = time.time() if now is None else float(now)
    rows = [row for row in (records() if rows is None else rows)
            if row["state"] == "waiting"]
    if due_only:
        rows = [row for row in rows
                if now >= int(row["retry_at"])
                and now >= float(row.get("next_attempt_at") or 0)]
    rows.sort(key=lambda row: (int(row["retry_at"]),
                               float(row.get("next_attempt_at") or 0),
                               row["session"]))
    limit = max(1, int(limit))
    return rows[:limit]


# ------------------------------------------------------------------ the pass

def tick(*, now=None, limit=MAX_DUE_PER_PASS):
    """One bounded pass: recover stranded claims, admit every due wait.

    Never raises.  A pass that cannot read something does nothing about it and
    says so in what it returns, which is the whole difference between "not yet"
    and "never".
    """
    from . import web_tasks
    now = time.time() if now is None else float(now)
    out, live = [], []
    for row in records():              # one window per pass, read once (see records)
        if row["state"] == "admitting":
            recovered = recover(row, now=now)
            if recovered is not None and recovered["state"] == "waiting":
                out.append({"session": row["session"], "started": False,
                            "reason": "recovered"})
                row = recovered        # due again in this same pass, not the next
        live.append(row)
    for row in waiting(now=now, limit=limit, due_only=True, rows=live):
        held = claim(row["session"], now=now)
        if held is None:
            continue                       # someone else took it, or it moved on
        try:
            out.append(web_tasks.admit_after_quota_reset(held))
        except Exception as exc:
            failed(row["session"], "the queued request could not be started: %s: %s"
                   % (type(exc).__name__, str(exc)[:200]), nonce=held["nonce"], now=now)
            out.append({"session": row["session"], "started": False, "reason": "error"})
    return out


def _next_try(row):
    """The earliest this wait may be admitted — the pair ``claim`` enforces."""
    return max(float(row["retry_at"]), float(row.get("next_attempt_at") or 0))


def _ticker_progress(now):
    """A healthy bounded pass owns its backlog, however old the reset time is."""
    info = ticker_status()
    if not info.get("running"):
        return "stalled"
    interval = float(info.get("interval") or TICK_INTERVAL)
    beat = float(info.get("last_at") or 0.0)
    started = float(info.get("started_at") or 0.0)
    if started > beat and 0 <= now - started <= interval:
        return "queued"            # the first pass is running, within its startup grace
    if beat <= 0 or now - beat > 2 * interval:
        return "unknown"
    return "queued"


def _waiting_progress(row, now):
    if now <= _next_try(row):
        return "scheduled"
    return _ticker_progress(now)


def _admitting_progress(row, now):
    """Is an admission in flight?  The same evidence ``recover`` acts on, read
    without writing: the claim's grace first, then the session lease and the
    bound entry.  An answer nothing could read stays ``unknown`` — it is not a
    claim of progress, and not a verdict that the claimant is gone.
    """
    if now - float(row.get("claimed_at") or 0) < CLAIM_GRACE:
        return "starting"
    try:
        verdict = claim_evidence(row, now=now)
    except Exception:
        verdict = "unknown"
    if verdict == "in_flight":
        return "starting"
    if verdict == "abandoned":
        # tick() recovers this exact claim before admitting due work.
        return _ticker_progress(now)
    if verdict in ("delivered", "gone"):
        return None                # this schedule says nothing about other queued entries
    return "unknown" if verdict == "unknown" else "stalled"


def status(session, *, now=None):
    """The sanitized view a surface may show: what is scheduled, when it is next
    tried, and whether this host can still vouch for it.

    Deliberately not the record.  A pid, a claim time, a nonce and this module's
    own retry counters answer no question a person asked.  ``next_attempt_at`` is
    published because ``retry_at`` alone is not the next start once a backoff has
    moved it, and ``progress`` because it is the one field a surface cannot work
    out for itself — it is decided here, from this process's clock, the record's
    own backoff and the liveness evidence above, never from a browser's clock:

      ``scheduled``  the next try is ahead
      ``queued``     the healthy ticker will attempt admission or claim recovery
      ``starting``   an admission is in flight
      ``unknown``    nothing here could be read; visible, never called progress
      ``stalled``    nothing is going to start this without a person
      ``stopped``    this module gave up on its own bookkeeping
    """
    row = read(session)
    if row is None:
        return None
    now = time.time() if now is None else float(now)
    if row["state"] != "admitting" and _bound_withdrawn(row):
        # An abandoned schedule must not imply other queued requests will start.
        # In-flight admissions are projected separately by _admitting_progress.
        return None
    if row["state"] == "waiting":
        progress = _waiting_progress(row, now)
    elif row["state"] == "admitting":
        progress = _admitting_progress(row, now)
        if progress is None:
            return None
    elif (row["state"] == "retired"
            and int(row.get("attempts") or 0) >= MAX_ATTEMPTS):
        # A wait that retired because it got its answer — the request ran, or was
        # withdrawn, or a fence took over — is not news: the queue row already
        # shows that.  A wait that retired because *this module's* bookkeeping
        # kept failing is: the scheduled start silently stopped being scheduled,
        # and only saying so keeps the queue row honest.  The request is still
        # pending and still startable by hand, so this is a label, not an alarm.
        progress = "stopped"
    else:
        return None
    return {"entry": row["entry"], "state": row["state"], "progress": progress,
            "retry_at": int(row["retry_at"]), "reason": row.get("reason") or "",
            "next_attempt_at": 0 if progress == "stopped" else int(_next_try(row))}


# ------------------------------------------------------------------ the ticker

_TICK_LOCK = threading.Lock()
_TICK_THREAD = None
_TICK_STOP = None                 # the stop event of the thread above, never shared
# `interval` and `last_at` are the pass's own account of itself: how often it
# intends to look, and when it last finished looking.  `status` reads both rather
# than assuming a healthy timer — a stopped or undated pass is not progress.
_TICK_STATUS = {"passes": 0, "started": 0, "last_error": "", "last_at": 0.0,
                "interval": 0.0, "started_at": 0.0}


def ticker_status():
    with _TICK_LOCK:
        return dict(_TICK_STATUS,
                    running=bool(_TICK_THREAD and _TICK_THREAD.is_alive()))


def start_ticker(interval=TICK_INTERVAL):
    """Start the one process-local pass over due waits (idempotent).

    The server owns this timer because the server is what can act on it.  A tab
    that is closed, a laptop that sleeps and a stream that was abandoned mid-run
    all leave the durable record exactly where it was.

    Each thread gets its *own* stop event.  A single module-level event shared
    across starts is how a stopped-but-not-yet-joined thread comes back to life:
    the next start clears the flag its predecessor is still reading, and two
    passes run against one store.
    """
    global _TICK_THREAD, _TICK_STOP
    with _TICK_LOCK:
        if _TICK_THREAD is not None and _TICK_THREAD.is_alive():
            return _TICK_THREAD
        stop = threading.Event()
        _TICK_STATUS["interval"] = max(1.0, float(interval))
        _TICK_STATUS["started_at"] = time.time()

        def _loop():
            while not stop.is_set():
                try:
                    results = tick()
                    with _TICK_LOCK:
                        _TICK_STATUS["passes"] += 1
                        _TICK_STATUS["last_at"] = time.time()
                        _TICK_STATUS["started"] += sum(
                            1 for row in results if row.get("started"))
                        _TICK_STATUS["last_error"] = ""
                except Exception as exc:          # a pass must never end the thread
                    with _TICK_LOCK:
                        _TICK_STATUS["last_error"] = "%s: %s" % (type(exc).__name__,
                                                                 str(exc)[:200])
                stop.wait(max(1.0, float(interval)))

        _TICK_THREAD = threading.Thread(target=_loop, name="collie-quota-resume",
                                        daemon=True)
        _TICK_STOP = stop
        _TICK_THREAD.start()
        return _TICK_THREAD


def stop_ticker(timeout=5.0):
    """Ask the pass to finish and wait for it.  True only if it really ended.

    A join that timed out is reported as a failure and the thread keeps its
    identity here, so a later start finds it still alive and refuses to run a
    second pass beside it.  Claiming "stopped" for a thread that is still
    running would be the one answer a shutdown path cannot recover from.
    """
    global _TICK_THREAD, _TICK_STOP
    with _TICK_LOCK:
        thread, stop = _TICK_THREAD, _TICK_STOP
    if stop is not None:
        stop.set()
    if thread is None:
        return True
    thread.join(timeout=max(0.0, float(timeout)))
    if thread.is_alive():
        return False
    with _TICK_LOCK:
        if _TICK_THREAD is thread:
            _TICK_THREAD, _TICK_STOP = None, None
    return True
