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
"""

from __future__ import annotations

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
            "next_seq": 1, "entries": [], "tombstones": []}


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
    seqs, ids = set(), set()
    for entry in entries:
        _validate_entry(entry, session, next_seq, seqs, ids)
    for tomb in tombs:
        _validate_tombstone(tomb, session, next_seq, ids)
    return doc


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


def claim(session, owner, *, limit=8, modes=MODES, directory=None):
    """Hand pending entries to the one executor that holds the run lease.

    Claiming is not delivery.  It records *which* executor is responsible for
    inserting these instructions, so that a later ``reconcile`` — run by whoever
    holds the lease next — can tell "in flight" from "abandoned" without a
    timeout.  Requires a live ``session_owner`` lease for this exact session:
    without it there is nothing to stop a second surface from claiming the same
    entry and inserting it twice.
    """
    root = _root(directory)
    owner = _require_owner(owner, session, root)
    wanted = tuple(_check_mode(m) for m in modes)
    limit = max(1, int(limit))
    path = store_path(session, root=root)
    with sessions._locked(path):
        doc = _load(path, session)
        ready = sorted((e for e in doc["entries"]
                        if e["state"] == "pending" and e["mode"] in wanted),
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


def list_entries(session, *, states=None, modes=None, limit=200, directory=None):
    """Accepted entries in acceptance order, surviving any restart."""
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
    return [_public(e) for e in rows[:max(0, int(limit))]]


def status(session, *, directory=None):
    """Counts, byte usage, caps — and whether a real journal backs this inbox.

    ``journal`` is the honest edge case: an inbox may legitimately exist before
    any transcript (the first message of a brand-new session is accepted before
    the run starts), so "missing" is not an error.  It does mean this id is not
    yet an executable session, and a caller listing work to resume should treat
    "missing" and "invalid" differently from "ok" rather than launching either.
    """
    root = _root(directory)
    path = store_path(session, root=root)
    exists = os.path.exists(path)
    with sessions._locked(path):
        doc = _load(path, session)
    counts = {state: 0 for state in STATES}
    for entry in doc["entries"]:
        counts[entry["state"]] += 1
    open_entries = [e for e in doc["entries"] if e["state"] in OPEN_STATES]
    return {
        "session": session, "exists": exists, "counts": counts,
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
