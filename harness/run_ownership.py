"""The seam between the native surfaces and the durable execution storage.

Three storage modules landed before this one: ``session_owner`` (exactly one
executor per saved session), ``task_inbox`` (instructions that outlive the
process that accepted them) and ``input_assets`` (their attachments).  This
module is deliberately thin — it starts nothing, schedules nothing and owns no
state — but it is the ONE place ``loop.py``, ``cli.py`` and ``tui.py`` reach
those modules from, because the parts that must not drift between surfaces are
exactly the parts that are easy to get subtly different:

* a lease is taken BEFORE the journal is read and released only after the final
  save, and it is always released — which is why the entry point is a context
  manager rather than an acquire/release pair repeated over dozens of early
  returns;
* a supplied lease is validated (same session id, same sessions root, this
  process) and never released by the code that was handed it;
* a claimed entry becomes a journal message exactly once, and the delivery
  acknowledgement happens after that message is durable, never before;
* a storage failure is reported, never swallowed — the volatile steer queue's
  habit of dropping accepted text on an exception is the bug this whole stack
  exists to end.

``verification_row``/``context_message`` are the other half: the durable record
of a host-executed check lives in ``run_receipts``, where the next model turn
cannot see it, so a resumed conversation had no idea whether the project's tests
had passed, failed or been stopped.  The projection built here is host-authored
(``source="harness"``), bounded, deduplicated by content digest, and carries only
fields the host itself recorded — a model sentence claiming a green test run can
never become one of these rows.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os

from . import input_assets, session_owner, sessions, task_inbox, verification
from .session_owner import OwnershipRequired, SessionBusy   # re-exported for callers

# Bounds. Every one of these limits what a *model turn* has to carry, not what
# the user may accept: the inbox itself refuses oversized input at acceptance
# time and never truncates it afterwards.
MAX_STEER_CLAIM = 8              # durable steers adopted at one safe boundary
RECEIPT_SCAN = 20                # newest receipts examined for host evidence
MAX_COMMAND_CHARS = 400          # of the recorded check command, in the projection
MAX_FIELD_CHARS = 64             # freshness/source/timestamp fields
CONTEXT_KIND = "verification_context"
CONTEXT_SOURCE = "harness"


class DurableInputError(RuntimeError):
    """The durable state behind a person's accepted input could not be established.

    Raised instead of guessing.  Every raise site leaves the store exactly as it
    was — claimed stays claimed, pending stays pending — because the two wrong
    answers are symmetrical and both silent: settling a claim from something that
    is not the journal marks a request delivered that no transcript contains, and
    reopening a claim we cannot verify lets a person edit an instruction the model
    has already been given.
    """

    def __init__(self, message, *, session="", action=""):
        super().__init__(message)
        self.session = session
        self.action = action or "durable_input"


class OwnershipRefused(RuntimeError):
    """This process may not execute this session right now.

    ``busy`` distinguishes "someone else is running it" (retryable, and the
    honest answer is to attach or enqueue durable input instead) from "the lease
    you handed me is not authority over this session" (a wiring error).
    """

    def __init__(self, message, *, session="", busy=False, owner=None):
        super().__init__(message)
        self.session = session
        self.busy = bool(busy)
        self.owner = dict(owner or {})


# ------------------------------------------------------------------ ownership

def validate(lease, session, *, directory=None):
    """Accept a caller-supplied lease as authority over ``session``, or refuse.

    The three facts checked here are the three ways a lease can look valid and
    not be: it belongs to another session id, it was taken against another
    sessions root (ids are unique per directory, not globally), or it was
    inherited by a forked child that does not own the run.  A run with no durable
    session id must not carry one at all — that is precisely the read-only
    delegate child, which has no journal of its own and must never be handed the
    parent's authority.
    """
    if not isinstance(lease, session_owner.SessionOwner):
        raise OwnershipRefused(
            "run_owner must be a session_owner.SessionOwner lease, not %s"
            % type(lease).__name__, session=session)
    if not session:
        raise OwnershipRefused(
            "a run with no durable session id must not hold an execution lease "
            "(this lease is for %s)" % lease.session, session=lease.session)
    try:
        return lease.assert_held(session, root=session_owner.sessions_root(directory))
    except OwnershipRequired as exc:
        raise OwnershipRefused(str(exc), session=session) from None


@contextlib.contextmanager
def hold(session, *, label="", meta=None, directory=None, existing=None):
    """Own ``session`` for the length of the block.  The one entry point.

    ``existing`` is a lease the caller already holds (the Web execution manager
    takes it before loading the journal and keeps it through its own final
    save): it is validated and yielded, and it is NOT released here, because
    releasing someone else's lease at the end of a nested block is how a run
    loses ownership while it is still running.

    A falsy ``session`` yields ``None``: a benchmark or embedded Harness with no
    durable conversation has nothing to serialize, and inventing a lease for it
    would make an anonymous run fail because an unrelated one is in flight.
    """
    if existing is not None:
        yield validate(existing, session, directory=directory)
        return
    if not session:
        yield None
        return
    try:
        lease = session_owner.acquire(session, label=label, meta=meta,
                                      directory=directory)
    except SessionBusy as exc:
        raise OwnershipRefused(str(exc), session=session, busy=True,
                               owner=exc.owner) from None
    except (ValueError, OSError) as exc:
        # An unusable id or an unwritable runtime directory. Refusing is the only
        # honest answer: without the lock there is nothing preventing a second
        # executor from appending to the same transcript.
        raise OwnershipRefused(
            "cannot take the execution lease for %s: %s" % (session, exc),
            session=session) from None
    try:
        yield lease
    finally:
        lease.release()


# ------------------------------------------------------- durable session state

def session_state(session, lease, *, cwd="", directory=None):
    """The conversation as it stands on disk right now, read under the run lease.

    An interactive surface loads a session once and then sits at a prompt where a
    person may think for minutes.  Meanwhile another surface can execute the same
    conversation to completion.  The history in memory is history by then, and a
    turn started from it asks the model about a thread that no longer exists and
    saves messages over the ones the other run wrote.  So execution authority
    re-derives state here, under the lease that makes it stable, and a fence or a
    workspace move that appeared in the meantime refuses the turn instead of
    being run over.

    Returns ``messages``/``receipts``/``cwd``/``recovery`` plus a ``refusal``
    string; a non-empty refusal means this turn must not run.
    """
    validate(lease, session, directory=directory)
    root = lease.root
    empty = {"messages": [], "receipts": [], "cwd": cwd, "recovery": None,
             "refusal": ""}
    checked = sessions.load_checked(session, root)
    status = checked.get("status")
    if status == "invalid":
        return dict(empty, refusal="session journal requires inspection: %s"
                    % (checked.get("reason") or "unreadable"))
    recovery = sessions.recovery_state(session, root)
    if recovery and recovery.get("recovery_required"):
        return dict(empty, recovery=recovery,
                    refusal=(recovery.get("reason")
                             or "an uninspected effect blocks this thread"))
    stored = checked.get("session") or {}
    saved_cwd = stored.get("cwd") or ""
    if cwd and saved_cwd and not _same_path(saved_cwd, cwd):
        # The journal, not this process, says where the conversation lives.
        # Executing it against the directory this window happens to hold would
        # edit an unrelated tree under the authority of somebody else's thread.
        return dict(empty, refusal=(
            "this conversation belongs to %s, but this surface is running in %s; "
            "reopen it there (or /resume it) rather than executing it here"
            % (saved_cwd, cwd)))
    return {"messages": list(stored.get("messages") or []),
            "receipts": list(stored.get("run_receipts") or []),
            "cwd": saved_cwd or cwd, "recovery": recovery, "refusal": ""}


def _same_path(left, right):
    return (os.path.normcase(os.path.abspath(os.path.expanduser(left)))
            == os.path.normcase(os.path.abspath(os.path.expanduser(right))))


# --------------------------------------------------------------- durable input

def durable_messages(session, lease, *, directory=None):
    """The transcript on disk — the only evidence a delivery may be settled from.

    In-memory messages are not evidence: the list a surface is holding may carry
    text no checkpoint ever accepted, and settling a claim from it marks an
    accepted request consumed with no journal row behind it.  A journal that
    cannot be read is not "empty" either; it is unknown, and unknown settles
    nothing.
    """
    validate(lease, session, directory=directory)
    checked = sessions.load_checked(session, lease.root)
    status = checked.get("status")
    if status == "ok":
        return list((checked.get("session") or {}).get("messages") or [])
    if status == "missing":
        return []          # no transcript at all: nothing has been delivered yet
    raise DurableInputError(
        "the transcript for %s could not be read (%s), so no accepted request may "
        "be settled from it: %s" % (session, status, checked.get("reason") or ""),
        session=session, action="journal")


def reconcile(session, lease, *, directory=None):
    """Settle claims left behind by an executor that did not survive.

    The crash window is one line wide — the message is in the journal, the inbox
    has not been told — so the journal, and only the journal, decides.
    """
    delivered = sorted(set(task_inbox.delivered_ids(
        durable_messages(session, lease, directory=directory))))
    return task_inbox.reconcile(session, lease, delivered, directory=directory)


def settle_and_release(session, lease, *, reason="", directory=None):
    """Reconcile from the journal, then return only what is genuinely undelivered.

    Releasing blind is the bug this replaces: an entry whose message reached the
    journal one line before the crash would go back to ``pending``, where a
    person can edit or re-send an instruction the model already has.  Reconcile
    first, so everything the transcript proves was delivered is ``consumed``, and
    only then hand the remainder back.  If the journal cannot be read nothing is
    released at all — a claim left standing is recoverable, a claim wrongly
    reopened is not.
    """
    settled = reconcile(session, lease, directory=directory)
    released = task_inbox.release_claims(session, lease, reason=reason,
                                         directory=directory)
    return {"reconciled": settled, "released": released}


def sequence_floor(session, lease, *, directory=None):
    """The highest sequence number the inbox has already handed out.

    This is a run's chronology boundary.  Mid-run steering is consumed strictly
    ABOVE it, so an instruction accepted before this run started cannot be
    appended after — and therefore cannot override — the newer request the person
    just sent.  It is read before any model work, under the lease, so nothing
    accepted while the provider is being set up is missed.
    """
    validate(lease, session, directory=directory)
    row = task_inbox.status(session, directory=lease.root)
    return max(0, int(row.get("next_seq") or 1) - 1)


def claim_steer(session, lease, *, limit=MAX_STEER_CLAIM, after_seq=0):
    """Take mid-run instructions for this run only.  A follow-up is a new turn.

    ``after_seq`` filtering happens inside the store's lock, so older pending
    entries are never claimed-and-released in a loop: they stay untouched and
    visible, and a fresh steer behind a backlog larger than ``limit`` is still
    the one this run picks up.
    """
    return task_inbox.claim(session, lease, modes=("steer",), limit=limit,
                            after_seq=after_seq)


# ------------------------------------------- the initial request, as accepted

def claimed_entry(session, lease, entry, *, directory=None):
    """Validate a surface's claimed entry against the store, and return the store's.

    Identity and shape are not enough.  What decides whether this run may deliver
    the request is the durable record under the lease we hold: it must still be
    claimed, claimed by *us*, and hold the payload the caller thinks it holds.  A
    consumed entry is refused outright — it is already in a transcript, and
    executing it again repeats an instruction the person gave once.
    """
    validate(lease, session, directory=directory)

    def refuse(why):
        return DurableInputError("accepted request %s %s" % (
            (isinstance(entry, dict) and entry.get("id")) or "?", why),
            session=session, action="input_entry")

    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) \
            or not entry["id"]:
        raise DurableInputError(
            "input_entry must be a claimed task_inbox entry, not %r" % (entry,),
            session=session, action="input_entry")
    if entry.get("session") not in (None, "", session):
        raise refuse("belongs to session %r, not %s" % (entry.get("session"), session))
    stored = task_inbox.get(session, entry["id"], directory=lease.root)
    if stored is None:
        raise refuse("is not in this session's inbox")
    if stored.get("compacted"):
        raise refuse("is already finished (%s) and cannot be executed again"
                     % stored.get("state"))
    state = stored.get("state")
    if state == "consumed":
        raise refuse("was already delivered as %r; it must not be executed twice"
                     % (stored.get("delivery") or {}).get("message_id"))
    if state != "claimed":
        raise refuse("is %s, not claimed by this run" % state)
    holder = (stored.get("claim") or {}).get("owner")
    if holder != lease.owner_id:
        raise refuse("is claimed by %s, not by this executor" % holder)
    for field in ("digest", "text", "mode"):
        if field in entry and entry[field] != stored.get(field):
            raise refuse("changed since it was claimed (%s differs)" % field)
    return stored


def initial_request_content(session, stored, *, content, authority, directory=None):
    """Check that this run is about to execute the request that was accepted.

    Returns the model-facing content built through the official ``input_assets``
    path.  The two comparisons are different promises: ``content`` is what the
    model reads, so it must be the accepted request expanded by the host rather
    than a bundle assembled somewhere else, and ``authority`` is the sentence
    that mints tool grants, so it must be the person's own words exactly — an
    attached file must never widen what they authorised.
    """
    expected = entry_content(session, stored, directory=directory)
    accepted = stored["text"]
    if authority:
        if authority != accepted:
            raise DurableInputError(
                "accepted request %s grants authority for different text than it "
                "was accepted with" % stored["id"], session=session,
                action="input_entry")
    elif expected != accepted:
        raise DurableInputError(
            "accepted request %s carries attachments, so its verbatim text must be "
            "passed as the authority message" % stored["id"], session=session,
            action="input_entry")
    if content != expected:
        raise DurableInputError(
            "accepted request %s would be delivered as different content than it "
            "was accepted as" % stored["id"], session=session, action="input_entry")
    return expected


def entry_content(session, entry, *, directory=None):
    """The model-facing content for one accepted entry, attachments included.

    Raises ``input_assets.AssetError`` when the bundle a person attached cannot
    be read back.  That is a stop, not a warning: delivering the text of a
    request whose screenshot is missing sends the model a different request from
    the one that was accepted.
    """
    reference = (entry.get("metadata") or {}).get("assets")
    if not reference:
        return entry["text"]
    bundle = input_assets.load(session, reference, directory=directory)
    return input_assets.model_message(entry["text"], bundle)


def release(session, lease, *, entry_ids=None, reason=""):
    """Return this executor's undelivered claims to the queue people can see."""
    return task_inbox.release_claims(session, lease, entry_ids=entry_ids,
                                     reason=reason)


# ------------------------------------------- host verification, projected once

def _bounded_text(value, limit=MAX_FIELD_CHARS):
    text = " ".join(str(value or "").split())
    return text[:limit]


def _exit_code(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -(1 << 31) < value < (1 << 31) else None


def _outcome(evidence):
    """What this receipt established, classified by ``verification`` itself.

    Deriving the word here from ``passed`` alone collapsed two outcomes the host
    records separately into ``failed``: a check killed at its deadline (no exit
    code at all) and one that exited 0 but could not be bound to the files that
    exist now.  Neither is a broken test, and announcing them as FAILED sent the
    next model turn to repair a failure no receipt establishes.  So the single
    authoritative classifier the CLI and Web surfaces already read decides, and
    the projection cannot disagree with them about the same receipt.

    Two things it does not delegate: the ``canceled`` spelling this projection
    has always emitted (the ``outcome`` field is durable and is keyed off by
    callers), and a receipt with no ``executed`` flag at all — the shared
    classifier reads only an explicit ``False``, while a legacy receipt that
    predates the flag carries no evidence that anything ran.
    """
    if not evidence.get("executed"):
        return "not_run"
    state = verification.check_result_state(evidence)
    return "canceled" if state == "cancelled" else state


def _reason(evidence, outcome):
    """Why this check settled nothing, in the host's own words — or ''.

    A recorded ``skipped_reason`` is the receipt's own sentence and always wins.
    The two ambiguous outcomes have no such field, so their explanation comes
    from the same ``verification`` helper the CLI reports them with.  The command
    is already carried on its own line under its own bound, so it is cleared out
    of the evidence the sentence is built from rather than repeated here, where a
    long command line would crowd out the explanation itself.
    """
    recorded = _bounded_text(evidence.get("skipped_reason"), 160)
    if recorded or outcome not in ("timed_out", "inconclusive"):
        return recorded
    return _bounded_text(verification.check_result_reason(
        dict(evidence, command=""), command=""), 160)


def _row(evidence):
    """The bounded, host-recorded facts about one check — or None if there are none."""
    if not isinstance(evidence, dict):
        return None
    command = " ".join(str(evidence.get("command") or "").split())
    if not command:
        # Nothing actionable: a receipt with no command cannot tell the next turn
        # what was checked, and "something failed somewhere" is not evidence.
        return None
    outcome = _outcome(evidence)
    row = {
        "outcome": outcome,
        "command": command[:MAX_COMMAND_CHARS],
        "command_truncated": len(command) > MAX_COMMAND_CHARS,
        "exit_code": _exit_code(evidence.get("exit_code")),
        "freshness": _bounded_text(evidence.get("freshness")),
        "source": _bounded_text(evidence.get("source")),
        "recorded": _bounded_text(evidence.get("timestamp")),
        "reason": _reason(evidence, outcome),
    }
    row["digest"] = hashlib.sha256(json.dumps(
        row, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
    return row


def verification_row(session, lease, *, directory=None):
    """The newest host verification receipt for this session, bounded — or None.

    Reading the receipts requires the run lease for this exact session, which is
    the same authority the run itself is executing under: a projection of one
    conversation's evidence must never be assembled while owning another's.
    """
    validate(lease, session, directory=directory)
    checked = sessions.load_checked(session, lease.root)
    if checked.get("status") != "ok":
        return None
    receipts = (checked.get("session") or {}).get("run_receipts") or []
    for receipt in reversed(receipts[-RECEIPT_SCAN:]):
        if not isinstance(receipt, dict):
            continue
        row = _row(receipt.get("verification_evidence"))
        if row is not None:
            return row
    return None


# One sentence per outcome.  The two ambiguous ones say what they are NOT as
# well as what they are: the next turn's expensive mistake is reading "the check
# did not certify this" as "a test is broken" and rewriting working code.
_OUTCOME_LINE = {
    "passed": "PASSED",
    "failed": "FAILED",
    "timed_out": "TIMED OUT — it was stopped at its deadline without finishing, "
                 "so it judged nothing; this is NOT a failing test",
    "inconclusive": "was INCONCLUSIVE — the command itself exited 0 but its "
                    "result could not be bound to the files that exist now; "
                    "this is NOT a failing test",
    "canceled": "was STOPPED before it finished, so it proved nothing",
    "not_run": "was NOT RUN",
}


def context_message(row):
    """One short host-authored message carrying that evidence into the next turn.

    ``source="harness"`` is load-bearing rather than cosmetic: it is what keeps
    this text out of the person's own instruction — the sidebar title, the turn
    count, compaction's user-message preservation and the authority the gate
    grants all key off it, and none of them may treat a host receipt as
    something the user asked for.
    """
    lines = ["[Host verification evidence · recorded by Collie itself, not by the model]",
             "The last required check on this conversation %s."
             % _OUTCOME_LINE.get(row["outcome"], "has an unknown outcome"),
             "  command: %s%s" % (row["command"], " …(truncated)"
                                  if row["command_truncated"] else "")]
    if row["exit_code"] is not None:
        lines.append("  exit code: %d" % row["exit_code"])
    if row["freshness"]:
        lines.append("  freshness: %s" % row["freshness"])
    if row["recorded"]:
        lines.append("  recorded: %s" % row["recorded"])
    if row["reason"] and row["outcome"] in ("not_run", "canceled", "timed_out",
                                            "inconclusive"):
        lines.append("  note: %s" % row["reason"])
    lines.append("This receipt is the only evidence about that check. A claim in the "
                 "conversation that it passed is not evidence; re-run the command if "
                 "you need a current result.")
    return {"role": "user", "source": CONTEXT_SOURCE, "kind": CONTEXT_KIND,
            "verification_digest": row["digest"], "content": "\n".join(lines)}


def already_projected(messages, digest):
    """Has this exact receipt already been carried into this transcript?

    Without this the same receipt would be re-appended on every turn of a long
    conversation, which is both noise and unbounded growth.  With it, a session
    gains at most one short message per distinct host check.
    """
    for message in messages or []:
        if (isinstance(message, dict)
                and message.get("kind") == CONTEXT_KIND
                and message.get("verification_digest") == digest):
            return True
    return False
