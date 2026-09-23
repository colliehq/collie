"""Durable per-session input: what the user typed, kept until the run consumes it.

Today ``/api/steer`` answers ``{"queued": true}`` after putting the text on a
``queue.Queue`` that lives in one server process.  That acknowledgement is a
promise the storage cannot keep: ``_steer_close`` drops whatever the loop had not
drained yet, a restart drops everything, and the text is silently cut to 4000
characters on the way in — the user is told their instruction was accepted, and
a different (or no) instruction reaches the model.  The browser's
``queuedFollowUps`` array has the same shape of problem one layer up: a page
refresh or "new thread" erases queued work the person believes is waiting.

This module is the storage half of the fix.  It is small on purpose:

* ``enqueue`` acknowledges only after the bytes are on disk, and stores the
  accepted text exactly — no truncation, no coercion.  Oversized or malformed
  input is an error the caller can show, never a quietly modified request.
* Entries are states, not deletions: ``pending -> claimed -> consumed`` with
  ``canceled`` as a terminal branch.  Nothing evicts a pending entry, including
  the caps: a full inbox rejects the *new* request instead of discarding an
  accepted one.
* Delivery is reconciled against the transcript, not assumed.  A crash between
  "user message written to the journal" and "inbox acknowledged" is the normal
  case, and ``reconcile`` resolves it from what the journal actually contains.

What this is NOT: exactly-once *execution*.  Nothing here can undo a tool call.
The guarantee is exactly-once *insertion* of an accepted instruction into a
session journal at a boundary the executor controls, plus a truthful record of
what happened to every accepted instruction.

Storage lives in ``<sessions>/inbox/<id>.json`` — the real sessions directory
(``sessions._dir``), so there is no second environment convention to configure,
but in a subdirectory so ``sessions.recent()`` / ``latest()`` / ``active_runs()``
(which list ``*.json`` in the sessions directory itself) never mistake an inbox
for a conversation.  Transactions reuse ``sessions._locked`` and
``sessions._atomic_dump``: the same short cross-process read/modify/write the
journal uses, which is exactly right here because every operation is a few
kilobytes.  Crucially, these are *short* transactions — the run lease
(``session_owner``) is what an executor holds for the length of a run, so the
inbox stays writable while the model is thinking.

Scheduling is not our business: no daemon, no polling loop, no provider calls.
``pending_sessions()`` and ``list_entries()`` are enough for the caller to decide
when to wake a run.

Integration contract for the drain site
---------------------------------------
The executor appends the journal message; this module never does.  Exactly one
append per claimed entry, and the append happens *before* the acknowledgement::

    entries = task_inbox.claim(sid, lease, modes=("steer",), limit=16)
    for entry in entries:
        session["messages"].append(task_inbox.journal_message(entry))  # once
        sessions.checkpoint(sid, session["messages"], ...)             # durable
        task_inbox.ack(sid, lease, entry["id"])                        # then ack

``Harness.steering`` is a callback that *returns text* and whose caller appends
(``loop.Harness._drain_steering`` → ``session["messages"].append(...)`` at
``loop.py:1242`` and ``:2181``).  A callback wired to the sequence above must
therefore return ``[]``, or the same instruction is inserted twice.  Only one of
the two may append.  See ``DRAIN_CONTRACT``.

Deleting the conversation
-------------------------
Acceptance and deletion are two writers to two different files — this store and
the journal — so "is anything waiting?" answered before the journal is removed is
a read that another process can invalidate one instruction later.  Rechecking is
not a fix; it moves the window.  ``begin_close`` closes it by putting the
conversation's own lifecycle *in this store*, under the same transaction that
accepts input::

    with task_inbox.closing(sid, lease, discard=flag) as closure:  # one transaction
        if sessions.delete(sid):                                   # journal removed
            closure.completed()                                    # -> "closed"

Every acceptance loads this file first, so an enqueue is either ordered before
the mark (and ``begin_close`` sees it waiting and refuses, or cancels it when the
person asked to discard) or after it (and is refused, storing nothing).  There is
no third interleaving: both writers hold the same cross-process lock on the same
path.

Which mark the close ends on is never decided by ``delete``'s answer — that call
can remove the journal and then fail at its own bookkeeping.  It is decided by
the journal: whether one existed when the close began, and whether one exists
now.  See ``_close_outcome`` and ``closing``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import time

from . import session_owner, sessions
from .session_owner import OwnershipRequired

INBOX_SUBDIR = "inbox"
VERSION = 1

MODES = ("steer", "follow_up")
STATES = ("pending", "claimed", "consumed", "canceled")
OPEN_STATES = ("pending", "claimed")
# The conversation's own lifecycle, as opposed to one request's.  Absent is the
# normal state: this id is open for input (including ids no journal exists for
# yet — the first request of a new conversation is accepted before it runs).
LIFECYCLE_STATES = ("closing", "closed")

# Caps are explicit and enforced at accept time, because the alternative to an
# error is dropping something a person was told had been accepted.
MAX_ID_LEN = 128
MAX_CLIENT_LEN = 64
MAX_TEXT_BYTES = 128 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_CONFIG_BYTES = 16 * 1024
MAX_JSON_DEPTH = 8
MAX_PENDING = 64                 # pending + claimed entries
MAX_PENDING_BYTES = 1024 * 1024  # summed payload bytes of pending + claimed
MAX_TERMINAL_RETAINED = 200      # finished entries kept in full before compaction
MAX_TERMINAL_BYTES = 1024 * 1024  # ...and their summed payload, whichever binds first
MAX_TOMBSTONES = 2000            # id+digest receipts kept after compaction
MAX_REVISIONS = 10

# Every cap above bounds what we *write*; this one bounds what we agree to
# *read*.  A store this module produced cannot exceed roughly 1 MiB of open
# payload + 1 MiB of retained terminal payload + ~0.5 MiB of tombstones, before
# JSON escaping.  A file past this ceiling was not written by us, and parsing it
# to find that out would mean allocating whatever an attacker (or a filesystem
# that filled a file with garbage) chose.
MAX_STORE_BYTES = 8 * 1024 * 1024
_MAX_TIMESTAMP = 1e13            # ~year 318857; a real clock never reaches it
_MAX_COUNTER = 1 << 40           # attempts / revision / pid sanity ceiling

_UNSET = object()

DRAIN_CONTRACT = """claim -> append journal_message once -> checkpoint -> ack.

The executor owns the append.  `journal_message(entry)` builds the message;
whoever calls it must be the only code path that inserts it.  A `Harness.steering`
callback returns text for display/gating only and must return [] once the drain
site appends `journal_message` itself, because `_drain_steering`'s caller also
appends what it is given.  `ack` runs only after the transcript containing that
message is durable; a crash before it is repaired by `reconcile`, a crash after
an early ack is not repairable by anything here."""


class InboxError(Exception):
    """Base for every refusal this module makes."""


class InvalidRequest(InboxError, ValueError):
    """The request could not be accepted as written (shape, size, or value)."""


class IdConflict(InboxError):
    """This id is already taken by a different payload."""


class InboxFull(InboxError):
    """Accepting this would exceed an explicit cap. Nothing was evicted."""


class StateConflict(InboxError):
    """The entry is no longer in a state where this operation is honest."""


class UnknownEntry(InboxError, KeyError):
    """No entry (and no tombstone) with that id in this session."""


class SessionClosed(InboxError):
    """This conversation is being deleted, or already was.  Nothing was stored.

    Deliberately *not* raised for a session id that merely has no journal: the
    first request of a new conversation is accepted before anything runs, so
    "no transcript" is a normal state.  This is raised only when a delete under
    the run lease recorded that this id is going away.
    """

    def __init__(self, message, *, state="closed"):
        super().__init__(message)
        self.state = state


class CloseBlocked(InboxError):
    """Accepted requests stand between this conversation and its deletion.

    ``entries`` is every open request, as the caller may show them.
    ``unwithdrawable`` names the ones ``cancel`` cannot take back (a claim is the
    executor's, not ours), which is a different refusal: the person can withdraw
    the first kind and has to settle the second.
    """

    def __init__(self, message, entries=(), *, unwithdrawable=()):
        super().__init__(message)
        self.entries = list(entries)
        self.unwithdrawable = list(unwithdrawable)


class StoreCorrupt(InboxError):
    """The inbox file exists but is not a readable inbox.

    Never repaired automatically: a torn store is evidence about accepted user
    input, and resetting it would turn "we lost your instruction" into "you never
    sent one".
    """


# ---------------------------------------------------------------- validation

def _check_id(value, field="id", limit=MAX_ID_LEN):
    if not isinstance(value, str) or not value:
        raise InvalidRequest("%s must be a non-empty string" % field)
    if len(value) > limit:
        raise InvalidRequest("%s is %d characters; the limit is %d"
                             % (field, len(value), limit))
    if value in (".", ".."):
        raise InvalidRequest("%s is reserved" % field)
    # ASCII-only, unlike sessions._path's Unicode-aware isalnum(): these ids
    # travel through URLs, HTTP replies and log lines, and a homoglyph pair that
    # renders identically must not be two different instructions.
    if not all(c.isascii() and (c.isalnum() or c in "-_.") for c in value):
        raise InvalidRequest("%s may use only letters, digits, '-', '_' and '.'" % field)
    return value


def _check_text(text):
    if not isinstance(text, str):
        raise InvalidRequest("text must be a string, not %s" % type(text).__name__)
    if not text.strip():
        raise InvalidRequest("text must not be empty")
    if "\x00" in text:
        raise InvalidRequest("text must not contain NUL")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        # Unpaired surrogates survive in a Python str but cannot round-trip
        # through UTF-8 JSON; accepting one would mean acknowledging a request
        # we could not store or replay faithfully.
        raise InvalidRequest("text contains unpaired surrogate characters") from None
    if size > MAX_TEXT_BYTES:
        raise InvalidRequest("text is %d bytes; the limit is %d" % (size, MAX_TEXT_BYTES))
    return size


def _check_json(value, *, field, limit, depth=0):
    """Accept only plain JSON values, and measure them honestly.

    The parent owns what these fields *mean* (which attachment ids are real,
    which run settings are legal).  This module owns only that they are JSON,
    bounded, and stored unmodified: no ``default=str`` stringification of an
    object the caller did not intend to send.
    """
    if value is None:
        return None, 0
    if not isinstance(value, dict):
        raise InvalidRequest("%s must be a JSON object" % field)
    _walk_json(value, field=field, depth=0)
    try:
        blob = json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise InvalidRequest("%s is not serializable JSON: %s" % (field, exc)) from None
    if len(blob) > limit:
        raise InvalidRequest("%s is %d bytes; the limit is %d" % (field, len(blob), limit))
    return json.loads(blob.decode("utf-8")), len(blob)


def _walk_json(value, *, field, depth):
    if depth > MAX_JSON_DEPTH:
        raise InvalidRequest("%s is nested deeper than %d levels" % (field, MAX_JSON_DEPTH))
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and "\x00" in value:
            raise InvalidRequest("%s must not contain NUL" % field)
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidRequest("%s must not contain NaN or Infinity" % field)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidRequest("%s keys must be strings" % field)
            _walk_json(item, field=field, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _walk_json(item, field=field, depth=depth + 1)
        return
    raise InvalidRequest("%s may not contain %s values" % (field, type(value).__name__))


def _check_mode(mode):
    if mode not in MODES:
        raise InvalidRequest("mode must be one of %s" % ", ".join(MODES))
    return mode


def _digest(mode, text, metadata, config):
    """Identity of a request's *content*, so a duplicate id can be judged."""
    payload = json.dumps({"mode": mode, "text": text, "metadata": metadata,
                          "config": config},
                         ensure_ascii=False, allow_nan=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- storage

def _root(directory=None):
    """The resolved sessions root every path and lease check in a call agrees on."""
    return session_owner.sessions_root(directory)


def store_path(session, directory=None, *, root=None):
    """The inbox file for one session (validated exactly like its journal)."""
    return session_owner.sidecar_path(session, INBOX_SUBDIR, ".json", directory, root=root)


def _blank(session):
    return {"version": VERSION, "session": session, "updated": 0.0,
            "next_seq": 1, "entries": [], "tombstones": [], "lifecycle": None}


def _load(path, session):
    """Read and fully validate the store, or refuse to operate on it.

    Bounded twice on purpose: by the file size before opening, and by the read
    itself, because the file can grow between the two.  Everything after this
    point assumes a store that *this module could have written* — that is what
    ``_validate_store`` establishes, and it is why every operation loads through
    here instead of trusting the last writer.
    """
    if not os.path.exists(path):
        return _blank(session)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise StoreCorrupt("inbox for %s is unreadable: %s" % (session, exc)) from None
    if size > MAX_STORE_BYTES:
        raise StoreCorrupt("inbox for %s is %d bytes; nothing this module writes "
                           "exceeds %d" % (session, size, MAX_STORE_BYTES))
    try:
        with open(path, encoding="utf-8") as fh:
            blob = fh.read(MAX_STORE_BYTES + 1)
        if len(blob) > MAX_STORE_BYTES:
            raise ValueError("larger than %d characters" % MAX_STORE_BYTES)
        # RecursionError: json's C parser raises it on a deeply nested document,
        # and a crashed load must be a refusal like every other unreadable store.
        doc = json.loads(blob, parse_constant=sessions._reject_json_constant)
    except (OSError, ValueError, RecursionError) as exc:
        raise StoreCorrupt("inbox for %s is unreadable: %s" % (session, exc)) from None
    return _validate_store(doc, session)


def _corrupt(session, why, *args):
    return StoreCorrupt("inbox for %s %s" % (session, why % args if args else why))


def _is_digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _check_stored_time(value, session, what):
    """Timestamps are read back, compared and passed to float(): bound them.

    A JSON integer has no width limit, so ``{"updated": 10**400}`` parses fine and
    then raises OverflowError somewhere far away (``pending_sessions`` sorts on
    ``float(updated)``).  Non-finite literals are already refused by the parser;
    this refuses the finite-but-absurd ones too.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _corrupt(session, "has a malformed %s timestamp" % what)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise _corrupt(session, "has an out-of-range %s timestamp" % what) from None
    if not math.isfinite(number) or number < 0 or number > _MAX_TIMESTAMP:
        raise _corrupt(session, "has an out-of-range %s timestamp" % what)
    return number


def _check_stored_int(value, session, what, *, low=0, high=_MAX_COUNTER):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _corrupt(session, "has a malformed %s" % what)
    return value


def _validate_store(doc, session):
    """Validate the whole durability-bearing structure before any operation.

    The rule is the same one ``sessions._validate_raw`` follows: a syntactically
    valid JSON object can still be torn — or forged — in exactly the fields that
    decide what happens to a person's accepted instruction, and every writer must
    reject it identically rather than let a fast path repair it into something
    plausible.  What "malformed" means here is narrow and checkable: *this module
    could not have written this*.

    Concretely that rules out several things the earlier shape-only check let
    through: two entries sharing an id (so ``_find`` and the caps disagree about
    which one exists), a ``bytes`` field that lies about the payload it describes
    (the caps are summed from it, so a forged negative made the byte cap
    unenforceable), a ``digest`` that does not match the text it certifies (which
    is what decides whether a retried id is a duplicate or an ``IdConflict``), a
    payload that could never have been accepted by ``enqueue``, a ``claim`` or
    ``delivery`` record contradicting the entry's own state, and a ``next_seq``
    behind an entry that already exists (which would hand a fresh entry a
    sequence number an older one already owns).

    Payload fields go through the *same* ``_check_text`` / ``_check_json`` the
    accept path uses, so there is one definition of storable and no gap between
    "we would refuse to accept this" and "we are happy to read it back".
    """
    if not isinstance(doc, dict):
        raise _corrupt(session, "is not an object")
    if doc.get("version") != VERSION:
        raise StoreCorrupt("inbox for %s has unsupported version %r"
                           % (session, doc.get("version")))
    if doc.get("session") != session:
        # Same discipline as sessions._validate_raw's identity check: an inbox
        # whose contents belong to another session must never be served here.
        raise StoreCorrupt("inbox file for %s claims session %r"
                           % (session, doc.get("session")))
    _check_stored_time(doc.get("updated", 0.0), session, "store")
    entries, tombs = doc.get("entries"), doc.get("tombstones")
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise _corrupt(session, "has malformed entries")
    if not isinstance(tombs, list) or not all(isinstance(t, dict) for t in tombs):
        raise _corrupt(session, "has malformed tombstones")
    if len(entries) > max(1024, MAX_PENDING + MAX_TERMINAL_RETAINED + 64):
        raise _corrupt(session, "holds %d entries; more than compaction allows",
                       len(entries))
    if len(tombs) > MAX_TOMBSTONES + 64:
        raise _corrupt(session, "holds %d tombstones; more than compaction allows",
                       len(tombs))
    next_seq = doc.get("next_seq")
    if (isinstance(next_seq, bool) or not isinstance(next_seq, int)
            or not 1 <= next_seq <= _MAX_COUNTER):
        raise _corrupt(session, "has a malformed sequence")
    _validate_lifecycle(doc.get("lifecycle"), session)
    seqs, ids = set(), set()
    for entry in entries:
        _validate_entry(entry, session, next_seq, seqs, ids)
    for tomb in tombs:
        _validate_tombstone(tomb, session, next_seq, ids)
    return doc


def _validate_lifecycle(record, session):
    """Structure only, on purpose.

    The mark is checked like every other field — state, owner, pid, timestamps —
    but an *open entry sitting next to it* is deliberately not corruption.
    ``StoreCorrupt`` is never repaired automatically, so rejecting that
    combination would answer "a request may have been stranded by a delete" with
    "you may no longer read this store", hiding the evidence a person needs.
    """
    if record is None:
        return None
    if not isinstance(record, dict):
        raise _corrupt(session, "has a malformed lifecycle record")
    if record.get("state") not in LIFECYCLE_STATES:
        raise _corrupt(session, "has a lifecycle record in state %r", record.get("state"))
    holder = record.get("owner")
    if not isinstance(holder, str) or not 0 < len(holder) <= 128:
        # The owner is what later tells an abandoned close from a live one.
        raise _corrupt(session, "has a lifecycle record closed by nobody nameable")
    _check_stored_int(record.get("pid", 0), session, "lifecycle pid")
    _check_stored_time(record.get("at"), session, "lifecycle")
    if not isinstance(record.get("journal", True), bool):
        raise _corrupt(session, "has a lifecycle record with a malformed journal fact")
    for field in ("reason", "note"):
        if not isinstance(record.get(field, ""), str):
            raise _corrupt(session, "has a lifecycle record with a malformed %s" % field)
    return record


# Every field the code below (and edit/cancel/ack/_compact) indexes directly. A
# store missing one of them is not a store we wrote, and finding out by
# KeyError halfway through a transaction is the wrong way to learn it.
_ENTRY_FIELDS = ("id", "seq", "mode", "state", "text", "metadata", "config", "client",
                 "digest", "message_id", "bytes", "revision", "created", "updated",
                 "attempts", "claim", "delivery", "revisions")
_TOMBSTONE_FIELDS = ("id", "digest", "state", "seq", "at")


def _validate_entry(entry, session, next_seq, seqs, ids):
    missing = [field for field in _ENTRY_FIELDS if field not in entry]
    if missing:
        raise _corrupt(session, "has an entry missing %s", ", ".join(missing))
    seq = entry.get("seq")
    if (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0
            or seq >= next_seq):
        # seq >= next_seq is the "next_seq is behind" case: accepting it would
        # let the next enqueue reuse a sequence number that is already spent.
        raise _corrupt(session, "has an entry with a malformed sequence")
    if seq in seqs:
        raise _corrupt(session, "has two entries with sequence %d", seq)
    seqs.add(seq)
    eid = entry.get("id")
    try:
        _check_id(eid)
        message_id = _check_id(entry.get("message_id"), "message_id")
    except InvalidRequest as exc:
        raise _corrupt(session, "has an entry with an unusable id: %s", exc) from None
    if eid in ids:
        raise _corrupt(session, "has two records for id %r", eid)
    ids.add(eid)
    if entry.get("session") not in (None, session):
        raise _corrupt(session, "has an entry belonging to %r", entry.get("session"))
    state, mode = entry.get("state"), entry.get("mode")
    if state not in STATES:
        raise _corrupt(session, "has an entry in state %r", state)
    if mode not in MODES:
        raise _corrupt(session, "has an entry with mode %r", mode)
    client = entry.get("client")
    if not isinstance(client, str) or len(client) > MAX_CLIENT_LEN:
        raise _corrupt(session, "has an entry with a malformed client")

    # The payload, judged by exactly the rules that would have accepted it.
    try:
        size = _check_text(entry.get("text"))
        metadata, meta_size = _check_json(entry.get("metadata"), field="metadata",
                                          limit=MAX_METADATA_BYTES)
        config, config_size = _check_json(entry.get("config"), field="config",
                                          limit=MAX_CONFIG_BYTES)
    except InvalidRequest as exc:
        raise _corrupt(session, "holds entry %s, which could never have been "
                       "accepted: %s", eid, exc) from None
    total = size + meta_size + config_size
    stored = entry.get("bytes")
    if isinstance(stored, bool) or not isinstance(stored, int) or stored != total:
        # The caps are summed from this field. Anything but the measured size of
        # the payload sitting right here is either a tear or a forgery, and both
        # answers are "refuse", not "assume the smaller number".
        raise _corrupt(session, "declares %r payload bytes for entry %s but holds %d",
                       stored, eid, total)
    if not _is_digest(entry.get("digest")):
        raise _corrupt(session, "has an entry (%s) with a malformed digest", eid)
    if entry["digest"] != _digest(mode, entry["text"], metadata, config):
        # The digest is what decides whether a retried id is the same request or
        # an IdConflict; a digest that does not certify this payload would let a
        # confirmed instruction be answered as a duplicate of a different one.
        raise _corrupt(session, "has an entry (%s) whose digest does not match "
                       "its payload", eid)
    _check_stored_time(entry.get("created"), session, "entry")
    _check_stored_time(entry.get("updated"), session, "entry")
    _check_stored_int(entry.get("attempts", 0), session, "attempt count")
    _check_stored_int(entry.get("revision", 0), session, "revision count")

    claim = entry.get("claim")
    if claim is not None:
        if state != "claimed":
            raise _corrupt(session, "has a %s entry (%s) carrying a claim", state, eid)
        if not isinstance(claim, dict):
            raise _corrupt(session, "has an entry (%s) with a malformed claim", eid)
        holder = claim.get("owner")
        if not isinstance(holder, str) or not 0 < len(holder) <= 128:
            raise _corrupt(session, "has an entry (%s) claimed by nobody nameable", eid)
        _check_stored_int(claim.get("pid", 0), session, "claim pid")
        _check_stored_time(claim.get("at"), session, "claim")
    elif state == "claimed":
        raise _corrupt(session, "has a claimed entry (%s) with no claim record", eid)

    delivery = entry.get("delivery")
    if delivery is not None:
        if state != "consumed":
            raise _corrupt(session, "has a %s entry (%s) carrying a delivery record",
                           state, eid)
        if not isinstance(delivery, dict):
            raise _corrupt(session, "has an entry (%s) with a malformed delivery", eid)
        if delivery.get("message_id") != message_id:
            # reconcile matches the journal against message_id. A delivery naming
            # a different id claims this entry reached the transcript under a name
            # nothing there can carry.
            raise _corrupt(session, "has entry %s delivered as %r but journaled as %r",
                           eid, delivery.get("message_id"), message_id)
        _check_stored_time(delivery.get("at"), session, "delivery")
        if not isinstance(delivery.get("recovered"), bool):
            raise _corrupt(session, "has an entry (%s) with a malformed delivery", eid)
    elif state == "consumed":
        raise _corrupt(session, "has a consumed entry (%s) with no delivery record", eid)

    revisions = entry.get("revisions")
    if revisions is not None:
        if not isinstance(revisions, list) or len(revisions) > MAX_REVISIONS:
            raise _corrupt(session, "has an entry (%s) with malformed revisions", eid)
        for revision in revisions:
            if not isinstance(revision, dict) or not _is_digest(revision.get("digest")):
                raise _corrupt(session, "has an entry (%s) with a malformed revision", eid)
            _check_stored_time(revision.get("at"), session, "revision")

    for field in ("canceled", "released"):
        record = entry.get(field)
        if record is None:
            continue
        if not isinstance(record, dict):
            raise _corrupt(session, "has an entry (%s) with a malformed %s record",
                           eid, field)
        _check_stored_time(record.get("at"), session, field)
        if not isinstance(record.get("reason", ""), str):
            raise _corrupt(session, "has an entry (%s) with a malformed %s reason",
                           eid, field)
    if entry.get("canceled") is not None and state != "canceled":
        raise _corrupt(session, "has a %s entry (%s) recorded as canceled", state, eid)


def _validate_tombstone(tomb, session, next_seq, ids):
    """Tombstones outlive their entries, so they carry the whole dedup contract."""
    missing = [field for field in _TOMBSTONE_FIELDS if field not in tomb]
    if missing:
        raise _corrupt(session, "has a tombstone missing %s", ", ".join(missing))
    try:
        tid = _check_id(tomb.get("id"))
    except InvalidRequest as exc:
        raise _corrupt(session, "has a tombstone with an unusable id: %s", exc) from None
    if tid in ids:
        raise _corrupt(session, "has two records for id %r", tid)
    ids.add(tid)
    if not _is_digest(tomb.get("digest")):
        raise _corrupt(session, "has a tombstone (%s) with a malformed digest", tid)
    if tomb.get("state") not in ("consumed", "canceled"):
        raise _corrupt(session, "has a tombstone (%s) in state %r", tid,
                       tomb.get("state"))
    seq = tomb.get("seq")
    if (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0
            or seq >= next_seq):
        raise _corrupt(session, "has a tombstone (%s) with a malformed sequence", tid)
    _check_stored_time(tomb.get("at"), session, "tombstone")


def _save(doc, path):
    doc["updated"] = time.time()
    sessions._atomic_dump(doc, path)


def _find(doc, entry_id):
    for entry in doc["entries"]:
        if entry.get("id") == entry_id:
            return entry
    return None


def _tombstone(doc, entry_id):
    for tomb in doc["tombstones"]:
        if tomb.get("id") == entry_id:
            return tomb
    return None


def _payload_bytes(entry):
    """The entry's payload size — validated against the payload on every load.

    ``_validate_entry`` refuses any store whose ``bytes`` disagrees with the text
    and JSON actually sitting in the entry, so summing this field for the caps is
    summing measured sizes, not a number the last writer asserted.
    """
    return int(entry.get("bytes") or 0)


def _compact(doc):
    """Bound the store without ever touching an open entry.

    Finished entries beyond the retention window collapse to an id+digest
    tombstone, which is what enqueue's idempotency check actually needs.  Pending
    and claimed entries are never compacted: they are the ones a person is still
    waiting on.

    Retention is bounded by count *and* by bytes: 200 finished entries carrying a
    128 KiB instruction each would be a 25 MiB store, which is both a silly thing
    to keep and more than ``_load`` will agree to read back.
    """
    terminal = sorted((e for e in doc["entries"] if e["state"] in ("consumed", "canceled")),
                      key=lambda e: e["seq"])
    excess = max(0, len(terminal) - MAX_TERMINAL_RETAINED)
    kept = sum(_payload_bytes(e) for e in terminal[excess:])
    while excess < len(terminal) and kept > MAX_TERMINAL_BYTES:
        kept -= _payload_bytes(terminal[excess])
        excess += 1
    if excess <= 0:
        return
    dropped = {id(e) for e in terminal[:excess]}
    for entry in terminal[:excess]:
        doc["tombstones"].append({"id": entry["id"], "digest": entry["digest"],
                                  "state": entry["state"], "seq": entry["seq"],
                                  "at": time.time()})
    doc["entries"] = [e for e in doc["entries"] if id(e) not in dropped]
    if len(doc["tombstones"]) > MAX_TOMBSTONES:
        doc["tombstones"] = doc["tombstones"][-MAX_TOMBSTONES:]


def _public(entry):
    """A defensive copy; callers never hold a reference into the store."""
    out = json.loads(json.dumps(entry, ensure_ascii=False, allow_nan=False))
    return out


# ----------------------------------------------------------- conversation life

def _journal_exists(session, root):
    """Does a transcript file exist for this id right now?

    Existence, not validity: a torn journal is a conversation that needs
    inspection, not one that was deleted, and treating the two alike would let a
    bad parse silence a person's input.
    """
    path = sessions._path(session, root)
    return bool(path) and os.path.exists(path)


def _closer_still_holds_the_lease(session, record, root):
    """Is the close recorded here still being carried out?

    A close is authoritative only while the process doing it is alive, and the
    run lease answers that without a timeout guess: the kernel releases it when
    its holder dies.  Two facts, in the order that cannot give a false negative:

    * A *different* owner id in the lease's identity record means the lock was
      taken again after our closer had it, so that closer is gone.  Identity, not
      busyness — refusing input merely because *somebody* holds the lease would
      make steering a live run impossible.
    * Otherwise ask the OS.  ``probe_busy`` never waits; ``None`` means it could
      not tell, which is no evidence that nobody is deleting this conversation,
      so it counts as held.
    """
    holder = record.get("owner")
    try:
        current = (session_owner.describe(session, root) or {}).get("owner") or {}
    except (ValueError, OSError):
        current = {}
    live = current.get("owner")
    if isinstance(live, str) and live and holder and live != holder:
        return False
    return session_owner.probe_busy(session, root) is not False


def _close_outcome(session, record, root):
    """Did this close actually remove a conversation?  Ask the disk, not the caller.

    Two facts decide it, and only two, because they are the only ones that
    separate *removed* from *never existed*:

    * ``journal`` in the mark — whether a transcript was there when the close
      began, recorded before anything could unlink it;
    * whether a transcript is there now.

    Present now means nothing was removed, whatever the delete returned, so the
    conversation is open.  Absent now, and present at the start, means the
    transcript is genuinely gone: closed, regardless of which step afterwards
    failed.  Absent in both means this id never named a conversation — the first
    request of a new one must still be accepted, so it stays open.
    """
    if _journal_exists(session, root):
        return "open"
    return "closed" if record.get("journal", True) else "open"


def _settle_lifecycle(doc, session, root):
    """The effective state of this conversation, with a dead mark cleared.

    Mutates ``doc`` when the record no longer describes reality; the caller is
    inside the transaction and decides whether that is worth a write.  A closer
    that did not survive leaves the question to ``_close_outcome``, which reads
    the journal it was removing.
    """
    record = doc.get("lifecycle")
    if not isinstance(record, dict):
        return "open"
    state = record.get("state")
    if state == "closing":
        if _closer_still_holds_the_lease(session, record, root):
            return "closing"
        if _close_outcome(session, record, root) == "open":
            doc["lifecycle"] = None
            return "open"
        doc["lifecycle"] = dict(record, state="closed", at=time.time(),
                                note="closer did not survive; the journal is gone")
        return "closed"
    if state == "closed":
        if _journal_exists(session, root):
            # The id names a conversation again — something re-created it under
            # the same name.  Refusing its input would be refusing a transcript
            # that exists.
            doc["lifecycle"] = None
            return "open"
        return "closed"
    return "open"


def _refuse_if_closed(doc, session, root):
    state = _settle_lifecycle(doc, session, root)
    if state == "closing":
        raise SessionClosed(
            "conversation %s is being deleted right now; this request was not stored "
            "— send it to a new conversation" % session, state=state)
    if state == "closed":
        raise SessionClosed(
            "conversation %s was deleted; this request was not stored — send it to a "
            "new conversation" % session, state=state)
    return state


def lifecycle(session, *, directory=None):
    """Whether this id is open, being deleted, or deleted — and what backs it.

    ``state`` is settled the same way every writer settles it, so a surface and
    an acceptance never disagree.  ``journal`` separates the two things that both
    look like "no conversation": ``missing`` with state ``open`` is an id nothing
    has created yet, while ``closed`` is one a delete finished on.
    """
    root = _root(directory)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        state = _settle_lifecycle(doc, session, root)
        record = doc.get("lifecycle")
    return {"session": session, "state": state,
            "record": dict(record) if isinstance(record, dict) else None,
            "journal": sessions.load_checked(session, root)["status"]}


def begin_close(session, owner, *, discard=False, reason="", directory=None):
    """Stop accepting input for this conversation, so its journal can be removed.

    One transaction does all of it: read what is waiting, decide, withdraw it if
    the person asked to, and record the mark.  That is the whole point — an
    acceptance racing this either lands first and is *seen* here, or lands after
    and is refused by ``enqueue``, which reads the mark under the same lock.

    Refusals, both of which leave the store exactly as it was:

    * open requests and no ``discard`` — the person is waiting on them, and a
      delete is not consent to drop them;
    * ``discard`` with a *claimed* request — cancelling is refused once an
      executor owns it, and the caller settles that against the journal (which
      is about to be deleted) before trying again.

    Requires the run lease, which the delete already holds for its own reason:
    it proves nothing is executing this conversation.  Accepting input never
    asks for the lease, so only *deletion* is made exclusive with acceptance.
    """
    root = _root(directory)
    owner = _require_owner(owner, session, root)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        open_entries = [e for e in doc["entries"] if e["state"] in OPEN_STATES]
        if open_entries and not discard:
            raise CloseBlocked("%d accepted request(s) are still waiting"
                               % len(open_entries), [_public(e) for e in open_entries])
        stuck = [e for e in open_entries if e["state"] != "pending"]
        if stuck:
            raise CloseBlocked(
                "%d accepted request(s) are claimed by an executor and cannot be "
                "withdrawn" % len(stuck), [_public(e) for e in open_entries],
                unwithdrawable=[e["id"] for e in stuck])
        now = time.time()
        canceled = []
        for entry in open_entries:
            entry.update(state="canceled", updated=now,
                         canceled={"at": now, "reason": str(reason or "session deleted")[:500]})
            canceled.append(entry["id"])
        # ``journal`` is the fact that later distinguishes a conversation this
        # close removed from an id that never had one.  It has to be taken here,
        # under the lock, before anything can unlink it: afterwards nobody can
        # tell the two apart, and a delete's own return value is not evidence —
        # it can report failure for bookkeeping that ran *after* the removal.
        doc["lifecycle"] = {"state": "closing", "owner": owner.owner_id,
                            "pid": os.getpid(), "at": now,
                            "journal": _journal_exists(session, root),
                            "reason": str(reason or "")[:200]}
        _compact(doc)
        _save(doc, path)
        return {"session": session, "canceled": canceled,
                "lifecycle": dict(doc["lifecycle"])}


def finish_close(session, owner, *, directory=None):
    """Record that the journal is gone.  Only the lease that began the close may.

    ``closed`` is the durable half of the fix: it survives the process, so a
    restarted server refuses a late acknowledgement instead of storing a request
    nothing can ever run.
    """
    root = _root(directory)
    owner = _require_owner(owner, session, root)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        record = doc.get("lifecycle") or {}
        if record.get("owner") != owner.owner_id or record.get("state") not in LIFECYCLE_STATES:
            raise StateConflict("conversation %s is not being closed by this lease"
                                % session)
        if record.get("state") == "closed":
            return dict(record)                      # idempotent
        record = dict(record, state="closed", at=time.time())
        doc["lifecycle"] = record
        _save(doc, path)
        return dict(record)


def abort_close(session, owner, *, reason="", directory=None):
    """Take the mark back: the journal is still here, so the conversation is open.

    What this does *not* undo is a discard.  A request cancelled on the way into
    a close stays cancelled, with its reason, because cancellation is the record
    the person was shown — silently resurrecting it would be inventing an
    instruction they were told had been withdrawn.
    """
    root = _root(directory)
    owner = _require_owner(owner, session, root)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        record = doc.get("lifecycle") or {}
        if record.get("owner") != owner.owner_id or record.get("state") not in LIFECYCLE_STATES:
            raise StateConflict("conversation %s is not being closed by this lease"
                                % session)
        doc["lifecycle"] = None
        _save(doc, path)
        return {"session": session, "state": "open",
                "reason": str(reason or "")[:200]}


class _Closure:
    """The handle ``closing`` yields, and the receipt it leaves behind.

    ``completed()`` is what the caller *believes* happened; ``outcome`` is what
    the disk says, filled in on the way out and the only one a route may report.
    ``settled`` is False when neither mark could be written, which is not a
    silent failure: the ``closing`` mark is still there and the next reader
    settles it from the journal the same way.
    """

    __slots__ = ("session", "canceled", "done", "outcome", "journal", "settled", "error")

    def __init__(self, session, canceled):
        self.session = session
        self.canceled = list(canceled)
        self.done = False
        self.outcome = "closing"
        self.journal = None
        self.settled = False
        self.error = None

    def completed(self):
        """The journal is gone; make the close permanent on exit."""
        self.done = True

    @property
    def removed(self):
        """Is the conversation actually gone?  True only with no journal left."""
        return self.outcome == "closed"


@contextlib.contextmanager
def closing(session, owner, *, discard=False, reason="", directory=None):
    """Hold a conversation closed for exactly as long as its deletion takes.

    The mark is settled on *every* way out — return, ``False`` from the delete,
    or an exception — and always from ``_close_outcome``, never from what the
    body reported.  A delete that removes the journal and then fails at its own
    bookkeeping reports failure for work done after the transcript was gone;
    believing it would clear the tombstone and reopen a conversation nothing can
    run.

    Durability:

    * ``closing`` is on disk before the journal is touched, and is authoritative
      only while the lease that wrote it is held.  A process killed mid-delete
      cannot silence a conversation: the next writer sees the lease is gone and
      settles the mark by the same rule.
    * ``closed`` is permanent for as long as no journal exists under that id.
      That receipt is what a restarted process reads, and it is why a delete
      deliberately leaves ``<sessions>/inbox/<id>.json`` behind.
    * A genuinely failed delete — journal still present — rolls the mark back;
      requests discarded on the way in stay cancelled.
    * If neither mark can be written, ``closing`` stands and is settled the same
      way once the lease drops.  All three failure orders converge.
    """
    root = _root(directory)
    started = begin_close(session, owner, discard=discard, reason=reason,
                          directory=directory)
    closure = _Closure(session, started["canceled"])
    try:
        yield closure
    except BaseException:
        _settle_closure(closure, started, owner, root, directory, "delete failed")
        raise
    _settle_closure(closure, started, owner, root, directory,
                    reason or "delete did not happen")


def _settle_closure(closure, started, owner, root, directory, reason):
    """Write the mark the journal calls for, and record what was really done."""
    record = started["lifecycle"]
    closure.journal = _journal_exists(closure.session, root)
    closure.outcome = _close_outcome(closure.session, record, root)
    try:
        if closure.outcome == "closed":
            finish_close(closure.session, owner, directory=directory)
        else:
            abort_close(closure.session, owner, reason=reason, directory=directory)
        closure.settled = True
    except (InboxError, OSError) as exc:
        # Not swallowed: the caller gets the reason, and the ``closing`` mark that
        # is still on disk is settled from the journal by the next reader.
        closure.settled, closure.error = False, exc


# -------------------------------------------------------------- accepting


def enqueue(session, entry_id, text, *, mode="steer", metadata=None, config=None,
            client="", directory=None):
    """Durably accept one instruction.  Returns only after it is on disk.

    ``entry_id`` is the caller's idempotency key (a browser can retry a POST it
    never saw the answer to).  Re-sending the same id with the same payload is a
    no-op that returns the stored entry with ``duplicate=True``; the same id with
    a different payload is an ``IdConflict``, because silently keeping either
    version would mean the user's confirmed input became a different request.

    ``mode`` is frozen at acceptance.  A ``follow_up`` never becomes a mid-run
    ``steer`` on its own: those are different instructions to the model (one
    interrupts the current turn, one starts from a finished state), and a queued
    follow-up that quietly turned into a steer would rewrite the request the
    person is watching.

    ``metadata`` carries bounded references (attachment ids, IDE context chips)
    and ``config`` the run settings snapshot a follow-up should run under.  Both
    are stored verbatim as JSON; their meaning is the caller's.

    The entry's ``message_id`` — the name it will carry in the journal, and the
    only name ``reconcile`` can recover it by — is assigned here, before any
    delivery, and equals ``entry_id``.  Nothing later may choose a different one:
    an id invented at ack time would name a message no transcript contains.
    """
    _check_id(entry_id)
    if client:
        _check_id(client, "client", MAX_CLIENT_LEN)
    _check_mode(mode)
    size = _check_text(text)
    metadata, meta_size = _check_json(metadata, field="metadata", limit=MAX_METADATA_BYTES)
    config, config_size = _check_json(config, field="config", limit=MAX_CONFIG_BYTES)
    total = size + meta_size + config_size
    digest = _digest(mode, text, metadata, config)
    root = _root(directory)
    path = store_path(session, root=root)

    with sessions._locked(path):
        doc = _load(path, session)
        existing = _find(doc, entry_id)
        if existing is not None:
            if existing["digest"] != digest:
                # Includes the case worth naming: the id was accepted, then
                # *edited*, and this is a retry of the original POST. Answering
                # "duplicate, accepted" would confirm a request the store no
                # longer holds. The revision tells the caller which it is.
                raise IdConflict(
                    "entry %s already exists with a different payload%s"
                    % (entry_id,
                       " (it was edited; revision %d)" % existing.get("revision", 0)
                       if existing.get("revision") else ""))
            # duplicate=True means "an entry with this id holds exactly this
            # payload right now" — not "nothing ever changed". `revision` says
            # whether it was edited (possibly back to this text) since; a caller
            # that needs the stricter answer passes expected_digest to `edit`.
            out = _public(existing)
            out["duplicate"] = True
            return out
        tomb = _tombstone(doc, entry_id)
        if tomb is not None:
            if tomb.get("digest") != digest:
                raise IdConflict("entry %s was already used for a different payload"
                                 % entry_id)
            return {"id": entry_id, "session": session, "state": tomb.get("state"),
                    "digest": digest, "duplicate": True, "compacted": True}
        # Only now, for the same reason the duplicate check comes first: a retry
        # of a request this conversation already holds is answered with the
        # record that exists — including the cancellation a delete gave it —
        # rather than turned into "you never sent that".  What a closing or
        # closed conversation refuses is *new* input, which is the thing that
        # would otherwise be acknowledged into a conversation being removed.
        _refuse_if_closed(doc, session, root)
        # Caps are checked after the duplicate check on purpose: a retry of an
        # already-accepted request must not start failing because the inbox
        # filled up afterwards.
        open_entries = [e for e in doc["entries"] if e["state"] in OPEN_STATES]
        if len(open_entries) >= MAX_PENDING:
            raise InboxFull("%d requests are already waiting (limit %d); "
                            "nothing was discarded" % (len(open_entries), MAX_PENDING))
        used = sum(_payload_bytes(e) for e in open_entries)
        if used + total > MAX_PENDING_BYTES:
            raise InboxFull("waiting requests would use %d bytes (limit %d); "
                            "nothing was discarded" % (used + total, MAX_PENDING_BYTES))
        now = time.time()
        entry = {"id": entry_id, "session": session, "seq": doc["next_seq"],
                 "mode": mode, "state": "pending", "text": text,
                 "metadata": metadata, "config": config,
                 "client": str(client or ""), "digest": digest,
                 "message_id": entry_id, "bytes": total, "revision": 0,
                 "created": now, "updated": now, "attempts": 0,
                 "claim": None, "delivery": None, "revisions": []}
        doc["next_seq"] += 1
        doc["entries"].append(entry)
        _compact(doc)
        _save(doc, path)
        out = _public(entry)
    out["duplicate"] = False
    return out


def edit(session, entry_id, *, text=_UNSET, metadata=_UNSET, config=_UNSET,
         expected_digest="", directory=None):
    """Revise an accepted request that has not been handed to an executor yet.

    Refused once the entry is ``claimed``: at that point the executor may already
    have read the text it is about to insert, and letting an edit land would mean
    the model received one request while the UI shows another.  ``mode`` is never
    editable.  Pass ``expected_digest`` to make the edit a compare-and-set when
    two surfaces might be editing at once.
    """
    if text is _UNSET and metadata is _UNSET and config is _UNSET:
        raise InvalidRequest("edit requires text, metadata or config")
    root = _root(directory)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        entry = _find(doc, entry_id)
        if entry is None:
            raise UnknownEntry(entry_id)
        if entry["state"] != "pending":
            raise StateConflict("entry %s is %s and can no longer be edited"
                                % (entry_id, entry["state"]))
        if expected_digest and expected_digest != entry["digest"]:
            raise StateConflict("entry %s changed since it was read" % entry_id)
        new_text = entry["text"] if text is _UNSET else text
        size = _check_text(new_text)
        # Re-validating the untouched fields costs nothing and keeps one code
        # path deciding what is storable and how many bytes it takes.
        new_meta, meta_size = _check_json(
            entry["metadata"] if metadata is _UNSET else metadata,
            field="metadata", limit=MAX_METADATA_BYTES)
        new_config, config_size = _check_json(
            entry["config"] if config is _UNSET else config,
            field="config", limit=MAX_CONFIG_BYTES)
        total = size + meta_size + config_size
        others = sum(_payload_bytes(e) for e in doc["entries"]
                     if e["state"] in OPEN_STATES and e["id"] != entry_id)
        if others + total > MAX_PENDING_BYTES:
            raise InboxFull("the edited request would exceed %d pending bytes"
                            % MAX_PENDING_BYTES)
        entry["revisions"] = (entry.get("revisions") or [])[-(MAX_REVISIONS - 1):] + [
            {"digest": entry["digest"], "at": time.time()}]
        entry.update(text=new_text, metadata=new_meta, config=new_config, bytes=total,
                     revision=entry.get("revision", 0) + 1, updated=time.time(),
                     digest=_digest(entry["mode"], new_text, new_meta, new_config))
        _save(doc, path)
        return _public(entry)


def cancel(session, entry_id, *, reason="", directory=None):
    """Withdraw a request that has not been claimed.  Idempotent.

    A canceled entry stays in the store as its own tombstone: the id is spent, so
    a retried POST cannot resurrect it and a later id reuse with different text
    is a conflict rather than a surprise.  Cancelling one request never disturbs
    the others — the remaining pending entries stay exactly as accepted.
    """
    root = _root(directory)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        entry = _find(doc, entry_id)
        if entry is None:
            tomb = _tombstone(doc, entry_id)
            if tomb is not None and tomb.get("state") == "canceled":
                return {"id": entry_id, "session": session, "state": "canceled",
                        "compacted": True}
            raise UnknownEntry(entry_id)
        if entry["state"] == "canceled":
            return _public(entry)
        if entry["state"] != "pending":
            raise StateConflict("entry %s is %s and can no longer be canceled"
                                % (entry_id, entry["state"]))
        entry.update(state="canceled", updated=time.time(),
                     canceled={"at": time.time(), "reason": str(reason or "")[:500]})
        _compact(doc)
        _save(doc, path)
        return _public(entry)


def purge(session, entry_id, *, directory=None):
    """Remove a finished entry, leaving an id+digest tombstone behind.

    Explicit removal is allowed only for ``consumed``/``canceled`` entries.  A
    pending or claimed request is something a person is still waiting on; the
    answer to "delete it" is ``cancel``, which is visible in the record.
    """
    root = _root(directory)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        entry = _find(doc, entry_id)
        if entry is None:
            if _tombstone(doc, entry_id) is not None:
                return {"id": entry_id, "session": session, "purged": True}
            raise UnknownEntry(entry_id)
        if entry["state"] in OPEN_STATES:
            raise StateConflict("entry %s is %s; cancel it before removing it"
                                % (entry_id, entry["state"]))
        doc["entries"] = [e for e in doc["entries"] if e["id"] != entry_id]
        doc["tombstones"].append({"id": entry_id, "digest": entry["digest"],
                                  "state": entry["state"], "seq": entry["seq"],
                                  "at": time.time()})
        if len(doc["tombstones"]) > MAX_TOMBSTONES:
            doc["tombstones"] = doc["tombstones"][-MAX_TOMBSTONES:]
        _save(doc, path)
        return {"id": entry_id, "session": session, "purged": True,
                "state": entry["state"]}


# --------------------------------------------------------------- delivery


def claim(session, owner, *, limit=8, modes=MODES, after_seq=0, directory=None):
    """Hand pending entries to the one executor that holds the run lease.

    Claiming is not delivery.  It records *which* executor is responsible for
    inserting these instructions, so that a later ``reconcile`` — run by whoever
    holds the lease next — can tell "in flight" from "abandoned" without a
    timeout.  Requires a live ``session_owner`` lease for this exact session:
    without it there is nothing to stop a second surface from claiming the same
    entry and inserting it twice.

    ``after_seq`` claims only entries accepted *after* a boundary the caller
    established (a run start, an ownership acquisition).  It is applied inside
    this transaction rather than by the caller for two reasons: an entry the
    caller must not deliver is never marked claimed even briefly, and a backlog
    of older pending entries larger than ``limit`` cannot hide a newer one behind
    it — which would either starve the new instruction or turn every claim into a
    claim/release cycle over rows nobody is going to run.
    """
    root = _root(directory)
    owner = _require_owner(owner, session, root)
    wanted = tuple(_check_mode(m) for m in modes)
    limit = max(1, int(limit))
    if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
        raise InvalidRequest("after_seq must be a non-negative integer")
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        ready = sorted((e for e in doc["entries"]
                        if e["state"] == "pending" and e["mode"] in wanted
                        and e["seq"] > after_seq),
                       key=lambda e: e["seq"])[:limit]
        if not ready:
            return []
        now = time.time()
        for entry in ready:
            entry.update(state="claimed", updated=now,
                         attempts=int(entry.get("attempts") or 0) + 1,
                         claim={"owner": owner.owner_id, "pid": os.getpid(), "at": now})
        _save(doc, path)
        return [_public(e) for e in ready]


def ack(session, owner, entry_id, *, message_id="", directory=None):
    """Record that a claimed entry is now in the session journal.

    Call this *after* the message is durably in the transcript.  The other order
    — ack first, then write — is the one failure this module cannot repair: the
    inbox would say "delivered" about a message no journal contains.  In this
    order a crash leaves a claimed entry that ``reconcile`` resolves from the
    journal itself.

    ``message_id`` is optional and may only *confirm* the id assigned at
    acceptance (``entry["message_id"]``, which equals the entry id).  It is not a
    choice made here: ``journal_message`` stamped the entry's id on the message
    that is now in the transcript, and that is the only name ``reconcile`` can
    find after a crash.  Recording a different one would leave the inbox claiming
    delivery under an id no journal contains — so a mismatch is refused rather
    than stored, and the caller learns its id before it can act on it.
    """
    root = _root(directory)
    owner = _require_owner(owner, session, root)
    if message_id:
        _check_id(message_id, "message_id")
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        entry = _find(doc, entry_id)
        if entry is None:
            raise UnknownEntry(entry_id)
        mid = entry.get("message_id") or entry_id
        if message_id and message_id != mid:
            raise InvalidRequest(
                "entry %s is journaled as %r; ack cannot record it as %r"
                % (entry_id, mid, message_id))
        if entry["state"] == "consumed":
            if (entry.get("delivery") or {}).get("message_id") != mid:
                raise StateConflict("entry %s was already delivered as %r"
                                    % (entry_id, (entry.get("delivery") or {}).get("message_id")))
            return _public(entry)
        if entry["state"] != "claimed":
            raise StateConflict("entry %s is %s, not claimed" % (entry_id, entry["state"]))
        held_by = (entry.get("claim") or {}).get("owner")
        if held_by != owner.owner_id:
            raise OwnershipRequired("entry %s is claimed by %s" % (entry_id, held_by))
        entry.update(state="consumed", updated=time.time(), claim=None,
                     delivery={"message_id": mid, "at": time.time(), "recovered": False})
        _compact(doc)
        _save(doc, path)
        return _public(entry)


def release_claims(session, owner, *, entry_ids=None, reason="", directory=None):
    """Return this executor's undelivered claims to ``pending``.

    The end-of-run call: cancellation, an error, a budget stop or a clean finish
    all leave the same obligation — anything accepted but not inserted is still
    the user's outstanding request, and must be waiting when the next run starts.
    Only this owner's claims are touched; a claim belonging to a *previous* owner
    is ``reconcile``'s business.
    """
    root = _root(directory)
    owner = _require_owner(owner, session, root)
    if entry_ids is not None:
        entry_ids = {_check_id(e) for e in entry_ids}
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        released = []
        for entry in doc["entries"]:
            if entry["state"] != "claimed":
                continue
            if (entry.get("claim") or {}).get("owner") != owner.owner_id:
                continue
            if entry_ids is not None and entry["id"] not in entry_ids:
                continue
            entry.update(state="pending", claim=None, updated=time.time(),
                         released={"at": time.time(), "reason": str(reason or "")[:200]})
            released.append(entry["id"])
        if released:
            _save(doc, path)
        return released


def reconcile(session, owner, delivered_ids=(), *, directory=None):
    """Settle the crash window between journal insertion and acknowledgement.

    Sequence the executor implements:

        1. insert the user message into the journal, stamped ``inbox_id``
        2. ``ack`` the entry

    A process that dies between them leaves a ``claimed`` entry whose text is
    already in the transcript.  Re-delivering it would repeat the user's
    instruction to the model; dropping it would lose one that never ran.  Only
    the journal can decide, so the caller passes the ``inbox_id`` values it found
    in the persisted transcript and this function marks exactly those consumed,
    returning every other abandoned claim to ``pending``.

    Requires the run lease, which is what makes reassignment safe: holding it
    proves no other executor is running, so a claim under a different owner id
    belongs to a process that is gone.  A claim held by *this* owner is left
    alone — it is in flight right now, not abandoned.  No timeout is involved at
    any point.
    """
    root = _root(directory)
    owner = _require_owner(owner, session, root)
    delivered = {str(x) for x in (delivered_ids or ())}
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        result = {"consumed": [], "released": [], "unknown": [], "conflicts": []}
        seen = set()
        now = time.time()
        for entry in doc["entries"]:
            mid = (entry.get("delivery") or {}).get("message_id") or entry.get("message_id")
            if mid in delivered:
                seen.add(mid)
                if entry["state"] in OPEN_STATES:
                    entry.update(state="consumed", claim=None, updated=now,
                                 delivery={"message_id": mid, "at": now, "recovered": True})
                    result["consumed"].append(entry["id"])
                elif entry["state"] == "canceled":
                    # Cancel is refused once claimed, so this should be
                    # unreachable; report it rather than silently choosing.
                    result["conflicts"].append(entry["id"])
                continue
            if (entry["state"] == "claimed"
                    and (entry.get("claim") or {}).get("owner") != owner.owner_id):
                entry.update(state="pending", claim=None, updated=now,
                             released={"at": now, "reason": "owner did not survive"})
                result["released"].append(entry["id"])
        for tomb in doc["tombstones"]:
            if tomb.get("id") in delivered:
                seen.add(tomb.get("id"))
        result["unknown"] = sorted(delivered - seen)
        if result["consumed"] or result["released"]:
            _compact(doc)
            _save(doc, path)
        return result


def _require_owner(owner, session, root):
    """The single gate on every mutating delivery call.

    A lease is authority over one session id *in one sessions root*, held by one
    live process.  Checking only the id was a real hole: session ids are unique
    per directory, not globally, so a lease taken over ``A/run-7`` — in a test
    fixture, a second store, or after ``COLLIE_SESSIONS_DIR`` changed — would
    silently authorise claiming and acking a different person's ``B/run-7``.
    ``root`` is the store this call is actually about to write.
    """
    if not isinstance(owner, session_owner.SessionOwner):
        raise OwnershipRequired(
            "this operation requires a session_owner lease for %s" % session)
    return owner.assert_held(session, root=root)


# ------------------------------------------------------------------ reading


def get(session, entry_id, *, directory=None):
    """One entry, or None.  A compacted entry returns its tombstone record."""
    root = _root(directory)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        entry = _find(doc, entry_id)
        if entry is not None:
            return _public(entry)
        tomb = _tombstone(doc, entry_id)
        return dict(tomb, compacted=True, session=session) if tomb else None


def list_entries(session, *, states=None, modes=None, limit=200, include_open=False,
                 directory=None):
    """Accepted entries in acceptance order, surviving any restart.

    ``limit`` cuts the oldest-first listing, which is the right shape for a page
    of history but the wrong one for work still waiting: a session that has
    filled its retained history (``MAX_TERMINAL_RETAINED``) spends the whole
    budget on finished entries, and a request accepted afterwards falls off the
    end of the listing while the store still holds it as ``pending``.

    ``include_open`` is for the surfaces that answer "what is queued": entries
    in ``OPEN_STATES`` that the cut would have dropped are added back, still in
    acceptance order.  The result stays bounded — the store already refuses more
    than ``MAX_PENDING`` open entries, and that cap is applied here too rather
    than trusted — so this adds at most ``MAX_PENDING`` rows to ``limit`` and
    never trades away history to make room.
    """
    if states is not None:
        states = {s for s in states}
        unknown = states - set(STATES)
        if unknown:
            raise InvalidRequest("unknown state(s): %s" % ", ".join(sorted(unknown)))
    if modes is not None:
        modes = {_check_mode(m) for m in modes}
    root = _root(directory)
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        rows = [e for e in doc["entries"]
                if (states is None or e["state"] in states)
                and (modes is None or e["mode"] in modes)]
    rows.sort(key=lambda e: e["seq"])
    kept = rows[:max(0, int(limit))]
    if include_open and len(kept) < len(rows):
        dropped_open = [e for e in rows[len(kept):] if e["state"] in OPEN_STATES]
        if dropped_open:
            kept = sorted(kept + dropped_open[:MAX_PENDING], key=lambda e: e["seq"])
    return [_public(e) for e in kept]


def status(session, *, directory=None):
    """Counts, byte usage, caps — and whether a real journal backs this inbox.

    ``journal`` is the honest edge case: an inbox may legitimately exist before
    any transcript (the first message of a brand-new session is accepted before
    the run starts), so "missing" is not an error.  It does mean this id is not
    yet an executable session, and a caller listing work to resume should treat
    "missing" and "invalid" differently from "ok" rather than launching either.

    ``lifecycle`` is the other half of that question and the one that separates a
    session id nothing has created yet from one a delete finished on: ``open``,
    ``closing`` or ``closed``.
    """
    root = _root(directory)
    path = store_path(session, root=root)
    exists = os.path.exists(path)
    with sessions._locked(path):
        doc = _load(path, session)
        life = _settle_lifecycle(doc, session, root)
    counts = {state: 0 for state in STATES}
    for entry in doc["entries"]:
        counts[entry["state"]] += 1
    open_entries = [e for e in doc["entries"] if e["state"] in OPEN_STATES]
    return {
        "session": session, "exists": exists, "counts": counts, "lifecycle": life,
        "open_bytes": sum(_payload_bytes(e) for e in open_entries),
        "tombstones": len(doc["tombstones"]), "next_seq": doc["next_seq"],
        "updated": doc.get("updated") or 0.0,
        "journal": sessions.load_checked(session, root)["status"],
        "limits": {"max_pending": MAX_PENDING, "max_pending_bytes": MAX_PENDING_BYTES,
                   "max_text_bytes": MAX_TEXT_BYTES},
    }


def pending_sessions(*, directory=None, limit=200):
    """Sessions with input still waiting, for whoever schedules runs.

    A listing, not a loop: this module starts nothing.  Unreadable inbox files
    are reported with ``error`` instead of being skipped, so a store that needs
    inspection cannot disappear from view.
    """
    root = _root(directory)
    base = session_owner.sidecar_dir(INBOX_SUBDIR, root=root)
    if not os.path.isdir(base):
        return []
    rows = []
    for name in sorted(os.listdir(base)):
        if not name.endswith(".json"):
            continue
        sid = name[: -len(".json")]
        if not sessions._path(sid, directory=base):
            continue                       # not an id we could have written
        try:
            # Completed inboxes are common. Do not decode their entire backing
            # conversations just to learn that no input is waiting.
            path = store_path(sid, root=root)
            with sessions._locked(path):
                doc = _load(path, sid)
            if not any(entry["state"] in ("pending", "claimed") for entry in doc["entries"]):
                continue
            row = status(sid, directory=root)
        except InboxError as exc:
            rows.append({"session": sid, "error": str(exc)})
            continue
        if row["counts"]["pending"] or row["counts"]["claimed"]:
            rows.append({"session": sid, "pending": row["counts"]["pending"],
                         "claimed": row["counts"]["claimed"],
                         "journal": row["journal"], "updated": row["updated"]})
    rows.sort(key=lambda r: float(r.get("updated") or 0), reverse=True)
    return rows[:max(0, int(limit))]


#: Defaults for :func:`pending_sessions_window`.  ``SCAN`` is what bounds the work:
#: it is the number of inbox files a windowed read is allowed to open, whatever the
#: store holds.
WINDOW_LIMIT = 50
WINDOW_SCAN_LIMIT = 100


def pending_sessions_window(*, directory=None, limit=WINDOW_LIMIT,
                            scan_limit=WINDOW_SCAN_LIMIT):
    """A *bounded* listing of sessions with input waiting, for read-only surfaces.

    :func:`pending_sessions` answers "all of it", and the schedulers that decide what
    to run next need exactly that.  A display surface does not: the Daily Brief shows
    at most ``limit`` rows, and paying for every inbox in the store to produce them
    makes opening the brief slower the longer the person has used the product.

    So this opens at most ``scan_limit`` inbox files and stops early once ``limit``
    rows are found.  What that costs in exchange is coverage, and the return value is
    a dict rather than a list precisely so the shortfall cannot be lost:

    ``sessions``
        the rows, newest-touched first, same fields as :func:`pending_sessions` minus
        ``journal`` — a transcript's status cannot be had without decoding it, and
        that is the cost this function exists to avoid.  Unreadable inboxes still
        appear, with ``error`` and ``unreadable``, because a store that needs looking
        at must not vanish from a listing.
    ``examined`` / ``total`` / ``unexamined``
        how many candidate inboxes were opened, how many the store holds, and the
        difference.
    ``truncated``
        ``True`` when anything was left unopened *or* unreported.  A caller that
        cannot show this must not claim the store is quiet.

    Which files make the window is decided by file modification time, newest first,
    because that is the only ordering available without opening them; it is a
    preference for recent work, not a guarantee, and ``truncated`` is what says so.
    """
    limit, scan_limit = max(0, int(limit)), max(0, int(scan_limit))
    root = _root(directory)
    base = session_owner.sidecar_dir(INBOX_SUBDIR, root=root)
    empty = {"sessions": [], "truncated": False, "examined": 0, "total": 0,
             "unexamined": 0, "limit": limit, "scan_limit": scan_limit}
    if not os.path.isdir(base):
        return empty
    candidates = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                sid = entry.name[: -len(".json")]
                if not sessions._path(sid, directory=base):
                    continue               # not an id we could have written
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    mtime = 0.0            # still a candidate; just undatable
                candidates.append((mtime, sid))
    except OSError as exc:
        raise StoreCorrupt("inbox directory is unreadable: %s" % exc) from None
    # Newest first, and by id for the ties a coarse filesystem clock produces, so the
    # same store always yields the same window.
    candidates.sort(key=lambda row: (-row[0], row[1]))

    rows, examined = [], 0
    for mtime, sid in candidates[:scan_limit]:
        if len(rows) >= limit:
            break
        examined += 1
        try:
            path = store_path(sid, root=root)
            with sessions._locked(path):
                doc = _load(path, sid)
                life = _settle_lifecycle(doc, sid, root)
        except InboxError as exc:
            rows.append({"session": sid, "error": str(exc), "unreadable": True,
                         "updated": mtime})
            continue
        counts = {state: 0 for state in STATES}
        for entry in doc["entries"]:
            counts[entry["state"]] += 1
        if not (counts["pending"] or counts["claimed"]):
            continue
        rows.append({"session": sid, "pending": counts["pending"],
                     "claimed": counts["claimed"], "lifecycle": life,
                     "updated": doc.get("updated") or 0.0})
    rows.sort(key=lambda row: float(row.get("updated") or 0), reverse=True)
    rows = rows[:limit]
    return {"sessions": rows, "truncated": examined < len(candidates),
            "examined": examined, "total": len(candidates),
            "unexamined": max(0, len(candidates) - examined),
            "limit": limit, "scan_limit": scan_limit}


def journal_message(entry):
    """The journal message an executor must insert for a consumed entry.

    Kept here so the shape stays with the record it comes from.  ``source`` /
    ``kind`` matter: ``compaction._is_user`` treats ``source == "user"`` as the
    person's own instruction (anything else is host text that a summary may
    discard), so a steer inserted without them can be compacted away as harness
    chatter.  ``inbox_id`` is what ``reconcile`` matches against after a crash.
    """
    return {"role": "user", "content": entry["text"], "source": "user",
            "kind": entry["mode"], "inbox_id": entry.get("message_id") or entry["id"]}


def delivered_ids(messages):
    """Extract the ``inbox_id`` values a persisted transcript actually contains."""
    out = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        mid = message.get("inbox_id")
        if isinstance(mid, str) and mid:
            out.append(mid)
    return out
