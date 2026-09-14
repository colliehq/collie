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


def accepted_limits(entry):
    """The budget and generation ceilings this queued request was accepted under.

    A request typed into the Web composer freezes MAX_COST / MAX_TOTAL_TOKENS /
    MAX_TURNS / MAX_TOKENS / TEMPERATURE at acceptance, and the Web queue replays that
    snapshot when its turn comes. ``/next`` is the other door onto the SAME durable
    queue, so it has to replay it too: without this the entry ran under whatever the
    Settings panel happened to say afterwards, which is wrong in both directions — a
    ceiling raised while the request waited let it spend past what was authorized, and
    one lowered while it waited aborted the answer the person was waiting for with
    "stopped: budget ceiling reached".

    ``None`` inside the entry means it made no claim about its limits (it was accepted
    before they were frozen) and the current settings are the honest answer. A payload
    this build cannot read is a refusal instead: the request keeps its place in the
    queue rather than running under a guessed number.
    """
    from . import settings
    frozen = (entry.get("config") or {}).get("frozen") or {}
    try:
        return settings.enforce_pinned(settings.limits_from_payload(frozen.get("limits")))
    except ValueError as exc:
        raise QueueError(
            "this request recorded the budget it was accepted under, and this build "
            "cannot replay it (%s); it was kept pending — re-send it to run under the "
            "current limits" % exc) from exc


def accepted_capabilities(entry):
    """The sensitive grants this queued request was accepted under.

    The Web composer freezes DESKTOP_CONTROL / SCREEN_CAPTURE / MCP_MANAGE /
    MCP_DISCOVERY at acceptance and the managed stream replays that payload when the
    request's turn comes. ``/next`` claims the SAME durable entry, so it replays it too:
    a toggle switched on while the request waited must not arm a task nobody accepted
    with that authority. Revocation needs no replay — ``capability_policy.allowed``
    requires the live setting as well, so a grant turned off meanwhile stays off.

    ``None`` inside the entry means it made no claim (accepted before policies were
    frozen) and the current snapshot is the honest answer, which is exactly what
    ``from_payload`` returns for it. A payload this build cannot read is a refusal: the
    request keeps its place in the queue rather than running under guessed permissions.
    """
    from . import capability_policy
    frozen = (entry.get("config") or {}).get("frozen") or {}
    try:
        return capability_policy.from_payload(frozen.get("capabilities"))
    except ValueError as exc:
        raise QueueError(
            "this request recorded the capability settings it was accepted under, and "
            "this build cannot replay them (%s); it was kept pending — re-send it to "
            "run under the current settings" % exc) from exc


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
    accepted_limits(entry)          # refuse an unreplayable budget before the entry is claimed
    accepted_capabilities(entry)    # ... and unreplayable grants, for the same reason


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
    axes = tuple(axis for axis in (config.get("explicit_axes") or "").split(",")
                 if axis and axis != "none")
    # The same rule the Web queue applies to the same durable entry: the composer
    # sends effort=auto whenever its Reasoning-effort group was left alone, so that
    # value is the absence of a choice and must not outrank the default frozen at
    # acceptance.  Where a person pressed Start cannot decide how much reasoning
    # they pay for.
    effort = str(config.get("effort") or "").strip()
    if not effort or (effort.lower() in ("auto", "default") and "effort" not in axes):
        from . import settings
        effort = (frozen.get("reasoning_effort") or
                  settings.get("REASONING_EFFORT", "auto") or "auto")
    return resolve_run_decision(
        entry["text"], provider=provider,
        model=(frozen["model"] or None) if "model" in frozen else model,
        effort=effort,
        speed=config.get("speed") or "standard", route_kind=config.get("route_kind") or None,
        intent=config.get("intent") or "build", quality=config.get("quality") or "balanced",
        verification=config.get("verification") or "auto", workspace="current", strategy="single",
        explicit_axes=axes, history=history, receipts=receipts)


def notice(session):
    try:
        count = len(task_inbox.list_entries(session, states=("pending",)))
        return ("%d request(s) still waiting · /queue to inspect · /next to continue" % count) if count else ""
    except (task_inbox.InboxError, OSError) as exc:
        return "Pending requests could not be read: %s" % exc
