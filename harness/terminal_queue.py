"""Terminal controls for accepted input left waiting after a stopped run."""
import contextlib

from . import run_ownership, task_inbox


class QueueError(ValueError):
    pass


def handle_command(line, session, write):
    """Read/cancel accepted input without taking execution ownership."""
    if line != "/queue" and not line.startswith("/queue "):
        return False
    parts = line.split(None, 2)
    try:
        if len(parts) == 3 and parts[1] == "remove":
            row = task_inbox.cancel(session, parts[2], reason="removed in terminal")
            write("Removed %s (%s)." % (row["id"], row["state"]))
        elif len(parts) == 3 and parts[1] == "show":
            row = task_inbox.get(session, parts[2])
            if not row:
                raise QueueError("no accepted request with that id")
            write("%s · %s · %s\n%s" % (row["id"], row["mode"], row["state"], row["text"]))
        elif len(parts) == 1:
            rows = task_inbox.list_entries(session, states=task_inbox.OPEN_STATES)
            if not rows:
                write("No pending requests.")
            for row in rows:
                text = row["text"].replace("\n", " ")
                preview = text[:160] + ("…" if len(text) > 160 else "")
                write("%s · %s · %s\n  %s" % (row["id"], row["mode"], row["state"], preview))
            if rows:
                write("/next sends the earliest pending request. /queue show <id> shows it in full; /queue remove <id> removes it.")
        else:
            write("Use /queue, /queue show <id>, /queue remove <id>, or /next.")
    except (task_inbox.InboxError, OSError, QueueError) as exc:
        write("Queue: %s" % exc)
    return True


def _supported(entry):
    config = entry.get("config") or {}
    if (config.get("strategy", "single") != "single"
            or config.get("workspace", "current") != "current"
            or config.get("runner", "") not in ("", "collie")
            or config.get("verification", "auto") == "required"
            or config.get("intent") == "test"):
        raise QueueError("this request uses a separate workspace, external worker, Pack or a required host check; start it from the Web queue to keep those settings")
    from . import settings
    frozen = config.get("frozen") or {}
    previous = frozen.get("runner_settings")
    if isinstance(previous, dict) and any(
            previous.get(key) != (settings.get(key, "collie") or "collie")
            for key in ("RUNNER", "RUNNER_POOL")):
        raise QueueError("worker settings changed since acceptance; the request was kept pending")
    if isinstance(previous, dict) and previous.get("RUNNER", "collie") != "collie":
        raise QueueError("this request selected another worker; start it from the Web queue")


@contextlib.contextmanager
def claimed_next(session, owner, requested=False):
    if not requested:
        yield None
        return
    try:
        run_ownership.reconcile(session, owner)
        pending = task_inbox.list_entries(session, states=("pending",), limit=1)
        if not pending:
            raise QueueError("No pending request to send. Use /queue to inspect the conversation.")
        _supported(pending[0])
        claimed = task_inbox.claim(session, owner, limit=1)
        if not claimed:
            raise QueueError("the request changed before it could be started; use /queue to refresh")
    except (task_inbox.InboxError, OSError, run_ownership.DurableInputError) as exc:
        raise QueueError(str(exc)) from exc
    try:
        # Recheck the claimed record because edit/cancel is allowed while a
        # terminal is choosing. Execution authority comes from this record.
        _supported(claimed[0])
        yield claimed[0]
    finally:
        try:
            run_ownership.settle_and_release(session, owner, reason="terminal queue turn ended")
        except (task_inbox.InboxError, OSError, run_ownership.DurableInputError) as exc:
            raise QueueError("accepted input could not be settled: %s" % exc) from exc


def decision(entry, provider, model, history, receipts):
    """Keep the accepted run options, including an explicitly empty Auto model."""
    from .cli import resolve_turn_decision
    from .router import resolve_run_decision
    config = entry.get("config") or {}
    if not config:
        return resolve_turn_decision(entry["text"], provider, configured_model=model,
                                     history=history, receipts=receipts)
    frozen = config.get("frozen") or {}
    if frozen.get("provider") and frozen["provider"] != provider:
        raise QueueError("this request was accepted for %s; reopen that provider or use the Web queue" % frozen["provider"])
    return resolve_run_decision(
        entry["text"], provider=provider,
        model=(frozen["model"] or None) if "model" in frozen else model,
        effort=config.get("effort") or frozen.get("reasoning_effort") or "auto",
        speed=config.get("speed") or "standard", route_kind=config.get("route_kind") or None,
        intent=config.get("intent") or "build", quality=config.get("quality") or "balanced",
        verification=config.get("verification") or "auto", workspace="current", strategy="single",
        explicit_axes=tuple(axis for axis in (config.get("explicit_axes") or "").split(",")
                            if axis and axis != "none"), history=history, receipts=receipts)


def notice(session):
    try:
        count = len(task_inbox.list_entries(session, states=("pending",)))
        return ("%d request(s) still waiting · /queue to inspect · /next to continue" % count) if count else ""
    except (task_inbox.InboxError, OSError) as exc:
        return "Pending requests could not be read: %s" % exc
