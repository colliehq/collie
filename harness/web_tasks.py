"""Web input that outlives the socket it was typed on, and the run that owns it.

The Web surface used to accept a person's words into process memory: ``/api/steer``
pushed onto a ``queue.Queue`` that a finishing run threw away, sliced the text at
4000 characters on the way in, and answered ``{"queued": true}`` regardless;
attachments lived in a bounded dict that a later upload could evict, so a run
could start with the request but without the screenshot it was about.  Every one
of those is the same bug: an acknowledgement the storage could not keep.

This module is the Web half of the fix.  ``task_inbox`` owns durability,
``input_assets`` owns the attachment bytes and ``session_owner`` owns the right
to execute a session; what lives here is the surface's own three jobs:

* **accept** — validate the request, snapshot the attachment bytes it refers to,
  freeze the run settings it was typed under, and only then acknowledge it;
* **own** — take the OS lease *before* the journal is read and hold it through
  the run, the host check and the final save, so no second process can write the
  same session and no stale client socket decides when the work stops;
* **schedule** — after a run that actually completed, hand the still-held lease
  to the next accepted follow-up on a detached event sink, so the next turn does
  not depend on a browser tab still being open.

What this module deliberately does not do: decide that something ran.  Delivery
is always reconciled against the journal (``task_inbox.reconcile``), never
assumed from the fact that a run was started, and never inferred from the words
a model produced.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from . import input_assets, session_owner, sessions, task_inbox

# The documented ceiling for one accepted request.  It has to hold the text
# (``task_inbox.MAX_TEXT_BYTES``, 128 KiB) plus inline IDE context
# (``input_assets.MAX_CONTEXT_CHARS``, 64k characters, up to 4 bytes each) plus
# the small config object, with room for JSON escaping.  Images are referenced by
# upload id, so they are not counted here.  Anything larger is refused with 413
# rather than trimmed to fit: a request nobody agreed to shorten must not be
# accepted in a shortened form.
MAX_BODY_BYTES = 2_000_000

# The same limit for the compatibility IDE-context upload, which posts context
# objects inline and used to silently truncate them.
MAX_IDE_BODY_BYTES = 1_000_000

BUSY_ERROR = "this session already has an active run"

# Exactly the fields the composer's run configuration carries.  Unknown keys are
# refused rather than dropped: a setting silently ignored at acceptance would run
# the follow-up under something other than what the person selected.
CONFIG_KEYS = ("intent", "quality", "verification", "workspace", "strategy",
               "effort", "speed", "runner", "explicit_axes", "verify_command",
               "verify_source", "n", "check", "apply", "route_kind")
AXES = ("intent", "quality", "verification", "workspace", "strategy", "effort", "speed")
_ENUMS = {"workspace": ("current", "isolated"), "strategy": ("single", "pack"),
          "speed": ("standard", "fast"),
          "effort": ("auto", "low", "medium", "high", "xhigh", "max")}
_ENUM_DEFAULTS = {"workspace": "current", "strategy": "single", "speed": "standard",
                  "effort": "auto"}


class WebInputError(Exception):
    """A refusal with the HTTP status that describes it honestly."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = int(status)


# ------------------------------------------------------------------ validation

def check_session_id(value):
    """The session this request addresses, validated exactly like its journal.

    An inbox may legitimately exist before any transcript does — the first
    message of a new conversation is accepted before the run starts — so this
    checks that the id is *usable*, not that a journal already exists.
    """
    sid = str(value or "").strip()
    if not sid or not sessions._path(sid):
        raise WebInputError("session must be a plain conversation id", 400)
    return sid


def check_entry_id(value, field="id"):
    """The caller's idempotency key.  ``task_inbox`` is the authority; this only
    turns its refusal into an actionable 400 before any storage is touched."""
    try:
        return task_inbox._check_id(str(value or ""), field)
    except task_inbox.InvalidRequest as exc:
        raise WebInputError(str(exc), 400) from None


def check_mode(value):
    mode = str(value or "steer").strip().lower()
    if mode not in task_inbox.MODES:
        raise WebInputError("mode must be steer or follow_up", 400)
    return mode


def check_text(value):
    """Accept the text exactly as written, or refuse it — never a shortened copy."""
    if not isinstance(value, str):
        raise WebInputError("text must be a string", 400)
    if not value.strip():
        raise WebInputError("text must not be empty", 400)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise WebInputError("text contains characters that cannot be stored", 400) from None
    if size > task_inbox.MAX_TEXT_BYTES:
        raise WebInputError(
            "the request is %d bytes; the limit is %d and nothing was truncated"
            % (size, task_inbox.MAX_TEXT_BYTES), 413)
    if "\x00" in value:
        raise WebInputError("text must not contain NUL", 400)
    return value


def semantic_config(raw):
    """The run configuration the *person* chose, normalised, with nothing frozen.

    This is the half of an accepted config that a retry can be compared against.
    It contains only what the request itself said; the machine settings that were
    current at acceptance are added by ``freeze_config`` and deliberately kept
    out of here, because a retry sent after the Settings panel moved on is still
    the same request and must not be answered as a different one.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise WebInputError("config must be a JSON object", 400)
    unknown = sorted(set(raw) - set(CONFIG_KEYS) - {"frozen"})
    if unknown:
        raise WebInputError("unknown config field(s): %s" % ", ".join(unknown), 400)
    from .cli import normalize_run_options
    try:
        out = normalize_run_options(raw.get("intent") or "build",
                                    raw.get("quality") or "balanced",
                                    raw.get("verification") or "auto")
    except ValueError as exc:
        raise WebInputError(str(exc), 400) from None
    for key, allowed in _ENUMS.items():
        value = raw.get(key)
        value = _ENUM_DEFAULTS[key] if value in (None, "") else value
        if not isinstance(value, str) or value.strip().lower() not in allowed:
            raise WebInputError("%s must be one of %s" % (key, ", ".join(allowed)), 400)
        out[key] = value.strip().lower()
    for key, limit in (("runner", 64), ("route_kind", 32), ("verify_source", 160),
                       ("verify_command", 2000), ("check", 2000)):
        value = raw.get(key)
        if value in (None, ""):
            out[key] = ""
            continue
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            raise WebInputError("%s must be a string of at most %d characters"
                                % (key, limit), 400)
        out[key] = value.strip()
    axes = raw.get("explicit_axes")
    if isinstance(axes, (list, tuple)):
        axes = ",".join(str(a) for a in axes)
    axes = str(axes or "none").strip().lower()
    for axis in [a for a in axes.split(",") if a]:
        if axis not in AXES and axis not in ("none", "runner"):
            raise WebInputError("explicit_axes may only name %s" % ", ".join(AXES), 400)
    out["explicit_axes"] = axes or "none"
    attempts = raw.get("n", 3)
    if isinstance(attempts, str) and attempts.strip():
        try:
            attempts = int(attempts.strip())
        except ValueError:
            raise WebInputError("n must be a whole number from 2 to 6", 400) from None
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 2 <= attempts <= 6:
        raise WebInputError("n must be a whole number from 2 to 6", 400)
    out["n"] = attempts
    if raw.get("apply") not in (None, True, False):
        raise WebInputError("apply must be true or false", 400)
    out["apply"] = bool(raw.get("apply"))
    if out["strategy"] == "pack" and not out["check"]:
        raise WebInputError("Pack needs an executed check command to select a winner", 400)
    if (out["intent"] == "test" or out["verification"] == "required") and not out["verify_command"]:
        raise WebInputError("Test or Required verification needs a check command", 400)
    return out


def freeze_config(raw, *, provider, model="", interactive_speed="", reasoning_effort="",
                  runner_settings=None, limits=None, capabilities=None):
    """Pin every setting this request was typed under, at acceptance.

    A follow-up accepted while the composer said "Plan / thorough / Required"
    must run that way when its turn comes, even though the panel has moved on.
    The snapshot is resolved locally by the managed stream; nothing here writes
    ``Settings`` or the process environment, because replaying a snapshot by
    mutating global state would change every *other* run in this process too —
    and a request accepted under a snapshot must not have that snapshot rewritten
    afterwards, which is why ``accept`` never re-freezes an id it already holds.

    ``provider`` is frozen for a different reason: it decides who pays.  The
    managed stream refuses a run whose configured provider changed since
    acceptance rather than quietly charging a different account.

    ``limits`` is ``settings.freeze_limits()`` — the budget and generation
    ceilings (MAX_COST, MAX_TOTAL_TOKENS, MAX_TURNS, MAX_TOKENS, TEMPERATURE)
    the person had configured when they pressed send.  Those used to be read
    live at every turn boundary, which meant a cap raised or lowered while a
    request sat in the queue silently became the cap it ran under.  It carries
    its own version and digest so a build that cannot replay it says so and
    leaves the request waiting instead of guessing.  ``None`` means this entry
    made no claim about its limits (an entry accepted before they were frozen),
    and the managed stream then uses the current ones — an explicit absence,
    not an empty snapshot.
    """
    out = semantic_config(raw)
    out["frozen"] = {"provider": str(provider or ""), "model": str(model or ""),
                     "interactive_speed": str(interactive_speed or ""),
                     "reasoning_effort": str(reasoning_effort or "")}
    if runner_settings is not None:
        out["frozen"]["runner_settings"] = dict(runner_settings)
    if limits is not None:
        out["frozen"]["limits"] = json.loads(json.dumps(limits))   # plain, storable, detached
    if capabilities is not None:
        out["frozen"]["capabilities"] = json.loads(json.dumps(capabilities))
    return out


def stream_query(session, config):
    """Rebuild the run's query from its frozen config, for the managed stream.

    Deliberately not the text: the request itself travels as the claimed inbox
    entry, so there is exactly one authority for what the person asked.
    """
    cfg = config if isinstance(config, dict) else {}
    out = {"session": [session]}
    for key in ("intent", "quality", "verification", "workspace", "strategy", "effort",
                "speed", "runner", "verify_command", "verify_source", "check", "route_kind"):
        value = cfg.get(key)
        if value not in (None, ""):
            out[key] = [str(value)]
    if cfg.get("n"):
        out["n"] = [str(cfg["n"])]
    if cfg.get("apply"):
        out["apply"] = ["1"]
    if cfg:
        # Always explicit, so a frozen "all Auto" cannot be re-read as "every
        # supplied field was a deliberate choice" by the axis fallback.
        out["explicit_axes"] = [str(cfg.get("explicit_axes") or "none")]
    return out


# ------------------------------------------------------------------ attachments

def resolve_images(ids, lookup):
    """Turn upload ids into the bytes they name, or refuse.

    A missing id is an error, never an omission: running the request without the
    screenshot it is about produces a confident answer to a different question.
    """
    out = []
    ids = list(ids or ())
    if len(ids) > input_assets.MAX_IMAGES:
        raise WebInputError("attach at most %d images" % input_assets.MAX_IMAGES, 400)
    for value in ids:
        if not isinstance(value, str) or not value:
            raise WebInputError("image references must be upload ids", 400)
        stored = lookup(value)
        if not stored:
            raise WebInputError(
                "an attached image is no longer available; re-attach it and send again",
                409)
        media_type, data = stored
        out.append({"media_type": str(media_type or ""), "data": data})
    return out


def resolve_contexts(items, lookup):
    """Validate IDE context objects (or peek at already-uploaded ids) exactly.

    ``lookup`` peeks; it never consumes.  Taking the record before validation is
    how a rejected request used to destroy the context it was rejected for, and
    how a retried POST lost the attachment the first attempt had already read.
    """
    raw = []
    for item in list(items or ()):
        if isinstance(item, str):
            stored = lookup(item)
            if not stored:
                raise WebInputError(
                    "attached editor context is no longer available; re-attach it", 409)
            raw.extend(stored)
        else:
            raw.append(item)
    try:
        return input_assets.validate_contexts(raw)
    except input_assets.AssetError as exc:
        raise WebInputError(str(exc), 413 if "exceed" in str(exc) else 400) from None


def _asset_status(message):
    return 413 if ("exceed" in message or "storage limit" in message) else 400


def snapshot_assets(session, images, contexts):
    """Persist the exact bytes before anything is acknowledged."""
    if not images and not contexts:
        return None
    try:
        return input_assets.save(session, images=list(images or []),
                                 contexts=list(contexts or []))
    except input_assets.AssetError as exc:
        raise WebInputError(str(exc), _asset_status(str(exc))) from None
    except OSError as exc:
        raise WebInputError("attachments could not be stored, so the request was "
                            "not accepted: %s" % (exc.strerror or "write failed"),
                            503) from None


def asset_reference(images, contexts):
    """The reference ``snapshot_assets`` *would* produce, computed without storing.

    Attachment bytes are content-addressed, so the same screenshot re-uploaded
    under a new id resolves to the same digest.  That is what lets a retry whose
    volatile upload ids changed be recognised as the same request, and it costs
    no write: a retry that turns out to be a conflict must not leave a stray
    bundle behind in the conversation's attachment quota.
    """
    images, contexts = list(images or []), list(contexts or [])
    if not images and not contexts:
        return None
    try:
        return input_assets.reference_of(images=images, contexts=contexts)
    except input_assets.AssetError as exc:
        raise WebInputError(str(exc), _asset_status(str(exc))) from None


def load_bundle(session, entry):
    """The attachments an accepted request refers to, or an error before any run."""
    reference = (entry.get("metadata") or {}).get("assets")
    if not reference:
        return None
    return input_assets.load(session, reference)


# ------------------------------------------------------------------- accepting

def request_fingerprint(session, *, entry_id, text, mode, config, images, contexts,
                        client):
    """A private digest of the request body exactly as it was sent.

    Stored (as a hash, never the payload) so that a client which never saw the
    answer to its POST can re-send the *identical* body and be recognised — even
    after a restart has emptied the upload cache the body's image ids refer to.
    Without it, an acknowledgement lost in the network turns into "an attached
    image is no longer available" for a request that was in fact accepted.

    It is deliberately not the entry's identity: ``task_inbox`` decides that from
    the payload it stored.  This only ever answers "is this the same body again",
    and a retry that fails it still gets the full field-by-field comparison.
    """
    payload = json.dumps(
        {"session": session, "id": entry_id, "text": text, "mode": mode,
         "config": config, "images": list(images or []),
         "contexts": list(contexts or []), "client": client},
        ensure_ascii=False, allow_nan=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def existing_entry(session, entry_id):
    """What this conversation already holds under an id, if anything.

    Read before a retry is validated against the volatile upload cache, because
    the whole point is that an already-accepted request must not start failing
    because a restart emptied a cache it no longer depends on.
    """
    try:
        return task_inbox.get(session, entry_id)
    except task_inbox.StoreCorrupt as exc:
        raise WebInputError(str(exc), 409) from None
    except task_inbox.InboxError:
        return None


def _conflict(entry_id, what):
    return WebInputError(
        "request %s was already accepted with a different %s; send this one with a "
        "new id (nothing was changed or discarded)" % (entry_id, what), 409)


def match_accepted(existing, *, entry_id, text, mode, config, fingerprint,
                   resolve_assets):
    """Is this POST a retry of the request this id already holds?

    Returns the stored entry (marked ``duplicate``) when every part of the
    request matches what was accepted, and raises 409 when any of it does not.
    Never re-freezes and never rewrites: the entry keeps the settings snapshot it
    was accepted under, and the answer to a retry is the request that exists.

    The comparison is on what the person supplied — text, mode, the configuration
    they chose, and the attachment *content* — because the machine settings
    frozen at acceptance are allowed to have moved on since.  Attachments are
    compared by content digest, so re-uploading the same screenshot under a new
    id is a retry while a different screenshot under the same id is a conflict.
    """
    if existing.get("compacted"):
        # Only an id, a digest and a state survive compaction, so "is this the
        # same request?" has no answer here.  Fall through to task_inbox, whose
        # tombstone rule refuses anything it cannot certify.
        return None
    if str(existing.get("mode") or "") != mode:
        raise _conflict(entry_id, "mode")
    if existing.get("text") != text:
        raise _conflict(entry_id, "message")
    stored_config = {key: value for key, value in (existing.get("config") or {}).items()
                     if key != "frozen"}
    if stored_config != semantic_config(config):
        raise _conflict(entry_id, "run configuration")
    metadata = existing.get("metadata") or {}
    stored_reference = metadata.get("assets") or None
    stored_fingerprint = (metadata.get("request") or {}).get("fingerprint")
    if (fingerprint and stored_fingerprint == fingerprint
            and not existing.get("revision")):
        # The identical body, byte for byte, from a client that never saw the
        # answer.  Nothing volatile is looked up: the upload ids in it may have
        # expired long ago, and the request they described is already stored.
        return dict(existing, duplicate=True)
    images, contexts = resolve_assets()
    if asset_reference(images, contexts) != stored_reference:
        raise _conflict(entry_id, "attachment")
    return dict(existing, duplicate=True)


def accept(session, *, entry_id, text, mode="steer", config=None, images=(),
           contexts=(), client="web", fingerprint=""):
    """Durably accept one request.  Returns only after it is on disk.

    Attachments are snapshotted first: the entry may only reference bytes that
    are already durable, so an accepted request can never be executed without
    the screenshot or file selection it was about.
    """
    reference = snapshot_assets(session, images, contexts)
    metadata = {}
    if reference:
        metadata["assets"] = reference
    if fingerprint:
        # Bookkeeping, not payload: it is derived from this exact request, so it
        # is identical on every retry of it and cannot make one look different.
        metadata["request"] = {"fingerprint": fingerprint}
    metadata = metadata or None
    try:
        return task_inbox.enqueue(session, entry_id, text, mode=mode, metadata=metadata,
                                  config=config, client=client)
    except task_inbox.IdConflict as exc:
        raise WebInputError(str(exc), 409) from None
    except task_inbox.InboxFull as exc:
        raise WebInputError(str(exc), 429) from None
    except task_inbox.InvalidRequest as exc:
        raise WebInputError(str(exc),
                            413 if "the limit is" in str(exc) else 400) from None
    except task_inbox.StoreCorrupt as exc:
        raise WebInputError(str(exc), 409) from None
    except OSError as exc:
        # The one answer that must never be "accepted": we do not know whether
        # the bytes landed, so we do not claim they did.
        raise WebInputError("this request could not be stored and was not accepted: %s"
                            % (exc.strerror or "write failed"), 503) from None


def public_entry(entry):
    """One accepted request, as a surface may show it.

    The text is returned exactly as accepted.  Redaction belongs at the model and
    journal boundary, where what leaves the machine is decided; an inbox that
    showed the person something other than what they typed would be lying about
    what is queued.
    """
    if not isinstance(entry, dict):
        return None
    out = {key: entry.get(key) for key in
           ("id", "session", "seq", "mode", "state", "text", "client", "digest",
            "message_id", "bytes", "revision", "created", "updated", "attempts")}
    out["config"] = entry.get("config") or {}
    reference = (entry.get("metadata") or {}).get("assets")
    out["assets"] = ({"images": reference.get("images"), "contexts": reference.get("contexts"),
                      "bytes": reference.get("bytes")}
                     if isinstance(reference, dict) else None)
    claim = entry.get("claim")
    if isinstance(claim, dict):
        out["claim"] = {"at": claim.get("at"), "pid": claim.get("pid")}
    delivery = entry.get("delivery")
    if isinstance(delivery, dict):
        out["delivery"] = {"at": delivery.get("at"),
                           "recovered": bool(delivery.get("recovered"))}
    for key in ("canceled", "released"):
        if isinstance(entry.get(key), dict):
            out[key] = {"at": entry[key].get("at"), "reason": entry[key].get("reason")}
    for key in ("duplicate", "compacted"):
        if key in entry:
            out[key] = bool(entry[key])
    return out


def list_public(session, *, limit=200):
    return [public_entry(row) for row in task_inbox.list_entries(session, limit=limit)]


# -------------------------------------------------------------------- delivery

def journal_ids(session):
    """The ``inbox_id`` values the persisted transcript actually contains.

    ``None`` means the journal could not be read.  That is not "nothing was
    delivered": reconciling against an unreadable journal would return delivered
    requests to pending and send them a second time.
    """
    checked = sessions.load_checked(session)
    if checked["status"] == "invalid":
        return None
    return task_inbox.delivered_ids((checked["session"] or {}).get("messages") or [])


def reconcile_open_claims(session, owner):
    """Settle claims left by a process that did not survive, before claiming more.

    Only runs when a claim exists, so an ordinary run never pays for a journal
    read it has no use for.
    """
    if not task_inbox.list_entries(session, states=("claimed",), limit=1):
        return None
    delivered = journal_ids(session)
    if delivered is None:
        return None
    return task_inbox.reconcile(session, owner, delivered)


def inbox_floor(session):
    """The sequence number every request accepted from now on will be above.

    Read once at the start of a turn, under the lease, so "steering typed during
    this run" has a definition that does not depend on when the run got around to
    asking.  A steer left pending by an earlier, canceled turn is older than this
    floor: appending it *after* a newer manual request would answer the person
    with the instruction they had already moved on from.
    """
    try:
        return max(0, int(task_inbox.status(session)["next_seq"]) - 1)
    except (task_inbox.InboxError, KeyError, TypeError, ValueError):
        # No usable floor is not "no floor": consuming nothing is the safe
        # answer, and older steering stays visible for an explicit Start.
        return None


def _store_filters_by_seq():
    """Does this build's ``task_inbox.claim`` apply the floor itself?

    Asked of the signature rather than by catching ``TypeError`` from the call: a
    ``TypeError`` raised *inside* claiming is a bug, and swallowing it into a
    fallback would hide it behind behaviour that looks almost right.
    """
    import inspect
    try:
        return "after_seq" in inspect.signature(task_inbox.claim).parameters
    except (TypeError, ValueError):
        return False


def _claim(session, owner, *, limit, modes, after_seq=0):
    """``task_inbox.claim`` with the chronological floor applied.

    The filter belongs in the store — one lock, one decision — and the foundation
    grows it as ``claim(after_seq=...)``.  Until that lands here, the floor is
    still honoured: anything older is handed straight back to pending, which is
    where it was and where an explicit Start can still find it.
    """
    after_seq = max(0, int(after_seq or 0))
    if _store_filters_by_seq():
        return list(task_inbox.claim(session, owner, limit=limit, modes=modes,
                                     after_seq=after_seq))
    rows = list(task_inbox.claim(session, owner, limit=limit, modes=modes))
    older = [row["id"] for row in rows if int(row.get("seq") or 0) <= after_seq]
    if older:
        release_undelivered(
            session, owner, older,
            "accepted before this run started; waiting for an explicit Start")
        rows = [row for row in rows if int(row.get("seq") or 0) > after_seq]
    return rows


def claim_next(session, owner, modes=task_inbox.MODES, *, after_seq=0):
    """The earliest waiting request, handed to the executor that holds the lease."""
    rows = _claim(session, owner, limit=1, modes=modes, after_seq=after_seq)
    return rows[0] if rows else None


def claim_steer(session, owner, *, limit=8, after_seq=0):
    """Steering accepted after this turn began, and nothing older than that."""
    if after_seq is None:
        return []
    return _claim(session, owner, limit=limit, modes=("steer",), after_seq=after_seq)


def settle_run_claims(session, owner, entry_ids, *,
                      reason="the run ended before this request was delivered"):
    """Close the crash window, then return anything undelivered to pending.

    Order matters and is the whole point: the journal decides what was delivered
    (a crash between insertion and acknowledgement is the normal case), and only
    what it does *not* contain goes back to waiting.  Releasing first would
    re-deliver an instruction the model already received.
    """
    out = {"consumed": [], "released": [], "unknown": [], "conflicts": []}
    ids = [i for i in (entry_ids or []) if i]
    if not ids and not task_inbox.list_entries(session, states=("claimed",), limit=1):
        return out                # nothing outstanding; no journal read to pay for
    delivered = journal_ids(session)
    if delivered is None:
        # The journal cannot be read, so "was this delivered?" has no answer.
        # Leave every claim exactly where it is: returning them to pending would
        # send instructions again that the model may already have received.
        out["unreadable_journal"] = True
        return out
    try:
        out.update(task_inbox.reconcile(session, owner, delivered))
    except task_inbox.InboxError as exc:
        # Reported, not swallowed: an inbox that could not be reconciled is one
        # whose delivery state is now unknown, and the caller decides what may
        # still be started on top of that (nothing).
        out["error"] = "queued input could not be reconciled: %s" % exc
        return out
    if not ids:
        return out
    try:
        released = task_inbox.release_claims(session, owner, entry_ids=ids, reason=reason)
    except task_inbox.InboxError as exc:
        released = []
        out["error"] = "undelivered requests could not be returned to waiting: %s" % exc
    out["released"] = sorted(set(out.get("released") or []) | set(released))
    return out


def append_exchange_with_input(session, user_text, answer, entries, *, project="web", cwd=""):
    """One journal write: the request, the steering already journalled, the answer.

    Used by the external-worker path, whose protocol has no durable proof that a
    mid-run message was consumed.  What *is* provable is what the person said and
    that Collie wrote it down before offering it to the worker, so that is what
    the transcript records — in the order it happened, and never as a trailing
    unanswered user message that the next turn would answer twice.

    ``user_text=None`` means the request is already in the journal (it was
    write-ahead stamped before the worker was launched); the answer is then
    appended to what is there rather than a second copy of the question.  Entries
    already carrying an ``inbox_id`` in the transcript are likewise skipped, so
    the write-ahead path and this one can never double-insert the same steer.
    """
    checked = sessions.load_checked(session)
    if checked["status"] == "invalid":
        raise ValueError("cannot append to an unreadable session journal")
    messages = list((checked["session"] or {}).get("messages") or [])
    have = set(task_inbox.delivered_ids(messages))
    if user_text is not None:
        messages.append({"role": "user", "content": user_text})
    messages.extend(task_inbox.journal_message(entry) for entry in entries
                    if entry["id"] not in have)
    messages.append({"role": "assistant", "content": answer})
    sessions.save(session, messages, project=project, cwd=cwd, answer=answer,
                  preserve_active=True)
    return session


def journal_before_transport(session, owner, entries, *, base_messages, run_id,
                             project="web", cwd="", detail=None):
    """Write the accepted requests down *before* anything can transmit them.

    The failure this closes is small and expensive: an instruction handed to a
    worker and only saved when the run ends is, if the process dies in between,
    an effectful message that comes back as "pending" and gets sent a second
    time.  So the order is inverted — journal (stamped with the inbox id, under
    the held lease, keeping the run's recovery fence armed), acknowledge, and
    only then let the caller hand the text to the transport.

    Returns the messages now believed to be in the journal.  Raises on any
    durability failure, and the caller must then not transmit: an instruction
    that was never written down and never sent is one the person can still see,
    correct and re-send, which is the only recoverable state available here.
    """
    owner.assert_held(session)      # writing this journal is an owner's right only
    messages = list(base_messages or [])
    have = set(task_inbox.delivered_ids(messages))
    fresh = [entry for entry in entries if entry["id"] not in have]
    if fresh:
        messages.extend(task_inbox.journal_message(entry) for entry in fresh)
        sessions.checkpoint(session, messages, project=project, cwd=cwd, run_id=run_id,
                            state="external_action",
                            detail=detail if isinstance(detail, dict) else {})
    return messages


def ack_delivered(session, owner, entries):
    """Acknowledge entries whose text is now durably in the transcript.

    ``unconfirmed`` is the honest part: an external worker's protocol cannot
    prove it *acted* on a specific message, and nothing here claims it did.  The
    acknowledgement is only about insertion, which is what stops the request from
    being sent a second time as an unknown effect.
    """
    out = {"consumed": [], "unsettled": [], "unconfirmed": [e["id"] for e in entries]}
    for entry in entries:
        try:
            task_inbox.ack(session, owner, entry["id"])
            out["consumed"].append(entry["id"])
        except task_inbox.InboxError:
            out["unsettled"].append(entry["id"])
    return out


def harness_accepts_input_entry(harness):
    """Does this harness implement the durable-input handshake?

    Probed before assignment, because setting an attribute on a Python object
    always "works": a harness that does not read ``input_entry`` would run the
    text as an ordinary message and never stamp ``inbox_id`` on it, leaving the
    inbox unable to tell delivered from lost.  ``steering_after_seq`` is part of
    the same handshake: a loop that claims accepted steering without honouring
    the run's chronological floor can append a stale instruction from a canceled
    turn after a newer one.  Refusing is the only honest answer available here.
    """
    return (hasattr(harness, "input_entry") and hasattr(harness, "run_owner")
            and hasattr(harness, "steering_after_seq"))


# ------------------------------------------------------------------ scheduling

class DetachedSink:
    """Where a scheduled run's stream goes when nobody has a socket open.

    ``_run_stream`` writes to the client through exactly three attributes
    (``_sse_open``, ``_sse``, ``_wlock``) and mirrors every structural event onto
    the process-wide live bus and the session mirror.  So a scheduled turn needs
    no client at all: it publishes normally, a window that opens later re-attaches
    to the mirror, and nothing is written to a socket whose owner walked away.
    """

    KEEP = 60

    def __init__(self, session):
        self.session = session
        self._wlock = threading.Lock()
        self.events = []            # event kinds, bounded — evidence, not a transcript
        self.done = None
        self.finished = threading.Event()

    def _sse_open(self):
        return None

    def _sse(self, kind, data):
        with self._wlock:
            if kind == "done":
                self.done = data
            if kind not in ("token", "ping"):
                self.events.append(kind)
                del self.events[:-self.KEEP]


_SCHEDULED_LOCK = threading.Lock()
_SCHEDULED: dict = {}

_QUEUE_ERROR_LOCK = threading.Lock()
_QUEUE_ERRORS: dict = {}          # session -> one bounded, structured last failure
_MAX_QUEUE_ERRORS = 64


def note_queue_error(session, message, *, entry_id="", kind="queue"):
    """Publish a failure that happened where no one was watching.

    A detached turn has no socket and, when its bookkeeping is what broke, no
    ``done`` frame either.  Swallowing that leaves accepted input sitting in a
    state nobody can explain, so the last failure per session is kept here (a
    short string, never the request's text), mirrored to any open window, and
    returned by the inbox endpoint the surface already polls.
    """
    record = {"session": session, "error": str(message or "")[:400],
              "at": time.time(), "kind": str(kind or "queue")[:32]}
    if entry_id:
        record["entry"] = str(entry_id)[:128]
    with _QUEUE_ERROR_LOCK:
        _QUEUE_ERRORS[session] = record
        if len(_QUEUE_ERRORS) > _MAX_QUEUE_ERRORS:
            for stale in sorted(_QUEUE_ERRORS.values(),
                                key=lambda row: row["at"])[:-_MAX_QUEUE_ERRORS]:
                _QUEUE_ERRORS.pop(stale["session"], None)
    try:
        from .webapp import Handler
        Handler._mirror_pub(session, "queue_error", dict(record))
        Handler._live_pub("queue_error", dict(record))
    except Exception:
        # Publication is the second copy of this fact; the record above is the
        # one the inbox endpoint reads, and it is already stored.
        pass
    return record


def queue_error(session):
    with _QUEUE_ERROR_LOCK:
        record = _QUEUE_ERRORS.get(session)
        return dict(record) if record else None


def clear_queue_error(session):
    with _QUEUE_ERROR_LOCK:
        _QUEUE_ERRORS.pop(session, None)


def owner_busy(session):
    """Display snapshot; execution still requires taking the session lease."""
    return session_owner.probe_busy(session)

def scheduled(session):
    """The detached run this process started for a session, if it is still going."""
    with _SCHEDULED_LOCK:
        return _SCHEDULED.get(session)


def schedule(session, owner, entry):
    """Run one claimed request on a detached sink, keeping the lease held.

    The lease is *handed over*, not re-acquired: between releasing and taking it
    again another surface could win it, and the request the person is watching
    would sit waiting behind work they did not ask for.
    """
    sink = DetachedSink(session)
    thread = threading.Thread(target=_run_detached, args=(sink, session, owner, entry),
                              name="collie-web-input-%s" % str(session)[:16], daemon=True)
    with _SCHEDULED_LOCK:
        _SCHEDULED[session] = (thread, sink)
    try:
        thread.start()
    except BaseException:
        with _SCHEDULED_LOCK:
            if _SCHEDULED.get(session) == (thread, sink):
                _SCHEDULED.pop(session, None)
        raise
    return thread, sink


def _run_detached(sink, session, owner, entry):
    try:
        serve_managed_stream(sink, stream_query(session, entry.get("config")),
                             owner=owner, entry=entry)
        if sink.done and sink.done.get("error"):
            # A normal preflight refusal has a done frame, not an exception,
            # and may happen before any run registry row exists. Keep that
            # reason visible to the browser which clicked Send next.
            note_queue_error(session, sink.done["error"],
                             entry_id=(entry or {}).get("id") or "",
                             kind="scheduled_run")
    except Exception as exc:
        # A detached turn has nobody to raise at, but "nothing happened, silently"
        # is the one outcome a person cannot act on.  Publish the failure, end the
        # registry row so no window shows a dot forever, and — only if the lease
        # is somehow still ours — settle from the journal, which is the only thing
        # that can say whether the request reached the model before this broke.
        error = "the queued turn failed before it could report: %s: %s" % (
            type(exc).__name__, str(exc)[:200])
        note_queue_error(session, error, entry_id=(entry or {}).get("id") or "",
                         kind="scheduled_run")
        try:
            from .webapp import Handler
            Handler._run_end(session, error=error)
            Handler._mirror_pub(session, "done", {"session": session, "answer": "",
                                                  "error": error, "canceled": False})
        except Exception:
            pass
        successor = scheduled(session)
        if (owner is not None and getattr(owner, "held", False)
                and (successor is None or successor[1] is sink)):
            # The lease is still this turn's own.  If a *successor* had been
            # handed it before this failure surfaced, it would be running on it
            # right now, and releasing it here would hand its conversation to
            # whoever asked next.
            try:
                settle_run_claims(session, owner,
                                  [entry["id"]] if entry else [],
                                  reason="the queued turn failed before it ran")
            except Exception:
                pass
            owner.release()
    finally:
        sink.finished.set()
        with _SCHEDULED_LOCK:
            current = _SCHEDULED.get(session)
            if current is not None and current[1] is sink:
                _SCHEDULED.pop(session, None)


def _await_run_id(session, timeout=1.0):
    """The run id of a just-scheduled turn, if it exists yet.  Never waits long."""
    from .webapp import Handler
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        with Handler._runs_lock:
            row = Handler._runs.get(session)
            if row is not None and row.get("ended") is None and row.get("run"):
                return row["run"]
        if time.time() >= deadline:
            return ""
        time.sleep(0.01)


def start_pending(session, *, label="web-start"):
    """Explicitly run the earliest waiting request, while the session is idle.

    Refuses a busy or recovery-fenced session rather than queueing behind it, and
    takes the *earliest* pending entry whatever its mode — including a steer an
    interrupted run never delivered, which becomes this turn's initial request
    with its original kind intact.
    """
    try:
        lease = session_owner.try_acquire(session, label=label)
    except ValueError as exc:
        raise WebInputError(str(exc), 400) from None
    if lease is None:
        raise WebInputError("this conversation is running; stop it or wait for it "
                            "to finish", 409)
    handed = False
    try:
        state = sessions.recovery_state(session)
        if state and state.get("recovery_required"):
            raise WebInputError(state.get("reason") or
                                "this conversation needs recovery before it can continue", 409)
        try:
            reconcile_open_claims(session, lease)
            entry = claim_next(session, lease, task_inbox.MODES)
        except task_inbox.StoreCorrupt as exc:
            raise WebInputError(str(exc), 409) from None
        if entry is None:
            return {"session": session, "started": False, "reason": "nothing_pending"}
        schedule(session, lease, entry)
        handed = True
        out = {"session": session, "started": True, "entry": public_entry(entry)}
        run_id = _await_run_id(session)
        if run_id:
            out["run"] = run_id
        return out
    finally:
        if not handed:
            lease.release()


# ------------------------------------------------------------- execution owner

def serve_managed_stream(handler, qs, *, owner=None, entry=None):
    """Own the session for one whole turn, then hand the lease to the next one.

    The lease is taken **before** the journal is read, because everything the run
    decides — history, recovery state, saved workspace — is read from a store a
    second executor could otherwise be writing at the same moment.  It is held
    through the run, the host verification command and the final save, and only
    released after all of them, so a finished run's transcript can never be
    overwritten by a run that started while it was still saving.

    The client's socket owns none of that.  It is a place events are written to,
    and it may go away at any point without changing what runs or what is stored.
    """
    from . import webapp
    sid = (qs.get("session", [""])[0] or "").strip() or sessions.new_id()
    lease = owner
    if lease is None:
        try:
            lease = session_owner.try_acquire(sid, label="web")
        except ValueError as exc:
            handler._sse_open()
            handler._sse("done", {"session": sid, "answer": "", "error": str(exc)})
            return None
        if lease is None:
            handler._sse_open()
            handler._sse("done", {"session": sid, "answer": "", "error": BUSY_ERROR,
                                  "busy": True, "owner": session_owner.describe(sid)["owner"]})
            return None
    handed = False
    try:
        # A turn is starting for this conversation, so the last queue failure is
        # no longer the newest thing that happened to it.  If this one fails too,
        # it will say so itself.
        clear_queue_error(sid)
        handler._managed_session = sid
        handler._run_owner = lease
        handler._input_entry = entry
        handler._input_bundle = None
        handler._input_handed = []
        handler._stream_outcome = None
        handler._run_config_frozen = (((entry or {}).get("config") or {}).get("frozen")
                                      if entry else None)
        # The chronological floor for this turn's steering, read now — under the
        # lease and before any of the run's slow setup — so "typed during this
        # run" means what a person would mean by it.  A queued turn's own request
        # is the floor: steering accepted before it belongs to an earlier turn.
        handler._steer_floor = (int(entry["seq"]) if entry and entry.get("seq") is not None
                                else inbox_floor(sid))
        if entry is None:
            try:
                reconcile_open_claims(sid, lease)
            except task_inbox.InboxError:
                pass          # an inbox needing inspection must not block a new run
        else:
            try:
                handler._input_bundle = load_bundle(sid, entry)
            except input_assets.AssetError as exc:
                # Before the provider, before any tool: the request stays visible
                # and correctable instead of running without what it referred to.
                release_undelivered(sid, lease, [entry["id"]], "attachments could not be loaded")
                handler._sse_open()
                handler._sse("done", {"session": sid, "answer": "", "error": str(exc),
                                      "input_id": entry["id"]})
                return None
        webapp.Handler._run_stream(handler, qs)
    finally:
        try:
            handed = _settle_and_schedule(handler, sid, lease, entry)
        except Exception:
            # Whatever went wrong in bookkeeping, the lease must not leak: a held
            # lease nobody is using locks the conversation for this process's life.
            handed = False
        if not handed:
            lease.release()
    return None


def release_undelivered(session, owner, ids, reason):
    """Return claimed requests to waiting, with the reason a person can read."""
    try:
        task_inbox.release_claims(session, owner, entry_ids=ids, reason=reason)
    except task_inbox.InboxError:
        pass


def _settle_and_schedule(handler, sid, lease, entry):
    """Reconcile this run's claims, then schedule at most one follow-up turn.

    Settlement is a precondition, not a courtesy.  If what this turn delivered
    cannot be established — an unreadable journal, a store that refuses to be
    written — then the next request's history is unknown too, and starting work
    on top of it would be building on a guess.  In that case nothing is started,
    the failure is published where the surface can show it, and every accepted
    request stays exactly where it is, waiting for an explicit Start.
    """
    outcome = getattr(handler, "_stream_outcome", None) or {}
    claimed = [entry["id"]] if entry else []
    claimed += [row["id"] for row in getattr(handler, "_input_handed", None) or []]
    try:
        settlement = settle_run_claims(sid, lease, claimed)
    except Exception as exc:
        note_queue_error(sid, "queued input could not be settled after this run, so "
                              "nothing further was started: %s: %s"
                              % (type(exc).__name__, str(exc)[:200]),
                         entry_id=(entry or {}).get("id") or "", kind="settlement")
        return False
    if settlement.get("unreadable_journal"):
        note_queue_error(sid, "this conversation's transcript could not be read, so "
                              "what this run delivered is unknown; queued requests are "
                              "still waiting and nothing was started",
                         entry_id=(entry or {}).get("id") or "", kind="settlement")
        return False
    if settlement.get("error"):
        note_queue_error(sid, settlement["error"] + "; nothing further was started",
                         entry_id=(entry or {}).get("id") or "", kind="settlement")
        return False
    if settlement.get("conflicts"):
        note_queue_error(sid, "the inbox and the transcript disagree about %d request(s); "
                              "nothing was started" % len(settlement["conflicts"]),
                         kind="settlement")
        return False
    if not outcome.get("auto_next"):
        # Stop, error, a turn/budget cap and an open recovery fence all leave
        # accepted input waiting on an explicit Start.  Nothing here starts work
        # a person did not ask to continue.
        return False
    try:
        readable = journal_ids(sid) is not None
    except Exception:
        readable = False
    if not readable:
        # A turn that claimed nothing settles trivially, which says nothing about
        # the transcript the *next* turn would be built on.  Reading it is the
        # cheapest part of starting a run, and running one against a history
        # nobody could read is the most expensive thing that can go wrong.
        note_queue_error(sid, "this conversation's transcript could not be read, so no "
                              "further turn was started; queued requests are waiting",
                         entry_id=(entry or {}).get("id") or "", kind="settlement")
        return False
    try:
        nxt = claim_next(sid, lease, ("follow_up",))
    except Exception as exc:
        note_queue_error(sid, "the next queued request could not be claimed: %s: %s"
                              % (type(exc).__name__, str(exc)[:200]), kind="settlement")
        return False
    if nxt is None:
        return False
    if entry is not None and nxt["id"] == entry["id"]:
        # The run reported success but never consumed the request it was given.
        # Re-running it would loop; leave it waiting and visible instead.
        release_undelivered(sid, lease, [nxt["id"]], "the run finished without delivering it")
        return False
    try:
        schedule(sid, lease, nxt)
        return True
    except Exception:
        release_undelivered(sid, lease, [nxt["id"]], "the next turn could not be started")
        return False


def terminal_outcome(res=None, *, canceled=False, error="", recovery_required=False,
                     completed=None):
    """What actually happened, read from the run's own result.

    ``auto_next`` is the only thing scheduling looks at, and it is true for
    exactly one shape of ending: a run that completed normally.  A cancel, an
    error, an exhausted turn or budget cap, and an open recovery fence are all
    endings after which continuing on the person's behalf would be a guess.
    """
    from .recorder import run_outcome
    if res is not None:
        row = dict(run_outcome(res))
    else:
        row = {"stop_reason": "error" if error else "completed",
               "completed": bool(completed), "canceled": bool(canceled)}
    if completed is not None:
        row["completed"] = bool(completed) and row.get("stop_reason") in ("completed", None, "")
    row["canceled"] = bool(row.get("canceled") or canceled)
    row["error"] = str(error or "")
    row["recovery_required"] = bool(recovery_required)
    row["auto_next"] = bool(row.get("completed") and not row["canceled"]
                            and not row["error"] and not recovery_required)
    return row
