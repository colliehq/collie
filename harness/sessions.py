"""Local session persistence — save/load a conversation THREAD so `collie run --continue`,
`--resume <id>`, and `collie repl` carry the full back-and-forth across separate CLI invocations.
This is the continuity every interactive harness has; collie's version is plain local JSON files
(data/sessions/<id>.json) — no server, no account, on brand. The composer's own history elision
keeps a long thread from bloating the prefix, so sessions can grow safely.
"""
import ast
import contextlib
import json
import math
import os
import threading
import time


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


def _parse_legacy_toolcall(s, ToolCall):
    """Recover a ToolCall from a legacy repr string ("ToolCall(id=…, name=…, args=…)").
    Uses ast.literal_eval on each argument (never eval) so a hand-edited/corrupt session
    file can't smuggle in executable code. Raises on anything that isn't a ToolCall literal."""
    node = ast.parse(s.strip(), mode="eval").body
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "ToolCall"):
        raise ValueError("not a ToolCall literal")
    pos = [ast.literal_eval(a) for a in node.args]
    kw = {k.arg: ast.literal_eval(k.value) for k in node.keywords}
    return ToolCall(*pos, **kw)


def _dir(directory=None):
    # COLLIE_SESSIONS_DIR lets tests (and throwaway runs) write to a temp store instead of the
    # user's real data/sessions/ — so a mock-provider test suite never floods the Map's run list.
    d = directory or os.environ.get("COLLIE_SESSIONS_DIR")
    if not d:
        from .cli import DATA
        d = os.path.join(DATA, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def _path(sid, directory=None):
    """Map a session id to its JSON file, SAFELY. The web routes (/api/delete, /api/rename,
    /api/session, /api/stream?session=) feed `sid` straight from the URL, so an id like
    "../../etc/foo" or an absolute "/etc/cron.d/x" must not escape data/sessions/ — a CSRF GET
    from any web page the user has open could otherwise read/write/delete arbitrary *.json files.
    Reject traversal rather than normalising it: collapsing ``../../victim`` to ``victim`` stays
    inside the directory, but gives the hostile id authority over a different, valid session.
    Returns None for anything that isn't a short, plain id."""
    if not isinstance(sid, str):
        return None
    name = sid
    if (not name or len(name) > 128 or name in (".", "..")
            or any(c in "/\\\x00:" for c in name)
            or not all(c.isalnum() or c in "-_." for c in name)):
        return None
    d = _dir(directory)
    p = os.path.join(d, name + ".json")
    if os.path.dirname(os.path.realpath(p)) != os.path.realpath(d):
        return None
    return p


def store_root(state_dir=None):
    """Resolve the journal store shared by execution, recovery and ownership.

    A request for another installation's state reads its data/sessions store.
    The current installation also honors explicit data/session overrides and
    the source-checkout data location, exactly as normal session writers do.
    """
    if state_dir is None:
        return _dir()
    requested = os.path.realpath(os.path.expanduser(state_dir))
    current = os.path.realpath(os.environ.get("COLLIE_STATE_DIR") or
                               os.path.expanduser("~/.collie"))
    if os.path.normcase(requested) == os.path.normcase(current):
        return _dir()
    # An explicitly configured legacy/custom session store can itself identify
    # the requested installation. Never reuse an override from an unrelated root.
    override = os.environ.get("COLLIE_SESSIONS_DIR")
    if override:
        parent = os.path.normcase(os.path.dirname(os.path.realpath(os.path.expanduser(override))))
        if parent in (os.path.normcase(requested), os.path.normcase(os.path.join(requested, "data"))):
            return _dir()
    return os.path.join(requested, "data", "sessions")


@contextlib.contextmanager
def _locked(p):
    """Serialize a session's complete read/modify/write transaction across threads and processes."""
    with _LOCKS_GUARD:
        local = _LOCKS.setdefault(os.path.realpath(p), threading.RLock())
    with local:
        lock_path = p + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "a+b")
        acquired = False
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                if os.path.getsize(lock_path) == 0:
                    fh.write(b"\0"); fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    fh.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


def _msgs_out(messages):
    """Serialize messages for disk. tool_calls hold ToolCall dataclasses that DON'T JSON-serialize;
    the old `default=str` turned each into its repr string, so on reload `_to_anthropic` did `tc.id`
    on a STR and crashed ('str' object has no attribute 'id') on any continued tool-using session.
    Convert them to plain dicts so they round-trip."""
    out = []
    for m in messages or []:
        tcs = m.get("tool_calls")
        if tcs:
            m = dict(m)
            m["tool_calls"] = [tc if isinstance(tc, dict) else
                               {"id": getattr(tc, "id", None), "name": getattr(tc, "name", None),
                                "args": getattr(tc, "args", {})} for tc in tcs]
        out.append(m)
    return out


def _msgs_in(messages):
    """Rebuild ToolCall objects from the on-disk form so seeded history behaves like a live run."""
    from .providers import ToolCall
    out = []
    for m in messages or []:
        tcs = m.get("tool_calls")
        if tcs:
            m = dict(m); rebuilt = []
            for tc in tcs:
                if isinstance(tc, dict):
                    rebuilt.append(ToolCall(tc.get("id"), tc.get("name"), tc.get("args") or {}))
                elif isinstance(tc, str):
                    # legacy repr string ("ToolCall(id=…, name=…, args=…)") — recover via a
                    # safe AST parse (no eval); drop if it won't parse (better than crashing).
                    try:
                        rebuilt.append(_parse_legacy_toolcall(tc, ToolCall))
                    except Exception:
                        if os.environ.get("COLLIE_DEBUG"):
                            print("[sessions] dropped unparseable legacy tool_call:", tc[:120])
                elif tc is not None:
                    rebuilt.append(tc)
            m["tool_calls"] = rebuilt
        out.append(m)
    return out


def _load_raw(p):
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f, parse_constant=_reject_json_constant)
        return s if isinstance(s, dict) else None
    except Exception:
        return None


def _validate_raw(raw, sid):
    """Validate the recovery-bearing structure before any read/modify/write.

    A syntactically valid JSON object can still be torn in exactly the fields
    that fence replay.  Every writer uses this same validator so a fast path
    cannot repair that evidence into a deceptively clean session.
    """
    if not isinstance(raw, dict):
        raise ValueError("session journal is unreadable")
    if raw.get("id") not in (None, sid):
        raise ValueError("session identity does not match its filename")
    for field in ("project", "cwd", "title", "last_answer"):
        if field in raw and not isinstance(raw.get(field), str):
            raise ValueError("session %s is malformed" % field)
    if "updated" in raw:
        updated = raw.get("updated")
        if (isinstance(updated, bool) or not isinstance(updated, (int, float)) or
                not math.isfinite(float(updated)) or float(updated) < 0):
            raise ValueError("session timestamp is malformed")
    messages = raw.get("messages", [])
    if not isinstance(messages, list) or not all(
            isinstance(item, dict) for item in messages):
        raise ValueError("session messages are malformed")
    if "active_run" in raw:
        active = raw.get("active_run")
        if not isinstance(active, dict):
            raise ValueError("session active_run is malformed")
        valid_states = {
            "turn_boundary", "calling_model", "model_complete",
            "executing_tool", "tool_complete", "external_action",
            "terminal", "canceled",
        }
        state = active.get("state")
        if not isinstance(state, str) or state not in valid_states:
            raise ValueError("session active_run state is malformed")
        if not isinstance(active.get("detail", {}), dict):
            raise ValueError("session active_run detail is malformed")
        if not isinstance(active.get("run_id", ""), str):
            raise ValueError("session active_run identity is malformed")
        turn = active.get("turn", 0)
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            raise ValueError("session active_run turn is malformed")
        updated = active.get("updated", 0)
        if (isinstance(updated, bool) or not isinstance(updated, (int, float)) or
                not math.isfinite(float(updated)) or float(updated) < 0):
            raise ValueError("session active_run timestamp is malformed")
    if "run_receipts" in raw:
        receipts = raw.get("run_receipts")
        if not isinstance(receipts, list) or not all(
                isinstance(item, dict) for item in receipts):
            raise ValueError("session run_receipts are malformed")
    return raw


def _message_json(message):
    """Compare JSON representations without deleting fields from either record."""
    try:
        return json.dumps(message, ensure_ascii=False,
                          sort_keys=True, default=str, allow_nan=False)
    except Exception:
        return None


def _same_message(stored, live):
    """Recognize a loaded prefix without discarding new incoming information.

    Only the stored side may pass through the legacy reader. The incoming side
    is already serialized by the caller; projecting it again would discard new
    tool metadata or false/zero arguments. A match keeps the original stored
    record, including information the legacy reader did not expose to the run.
    """
    if stored == live:
        return True
    right = _message_json(live)
    if right is None:
        return False
    if _message_json(stored) == right:
        return True
    try:
        loaded = _msgs_out(_msgs_in([stored]))[0]
    except Exception:
        return False
    return _message_json(loaded) == right


def _merge_messages(old, new):
    """Merge two histories that grew from a common prefix, preserving both completed exchanges.

    The prefix scan decides how much of ``old`` and ``new`` is the SAME
    conversation.  Getting that wrong is not a cosmetic error: a false divergence
    at index i makes this append the whole live history onto the stored one, so a
    resumed thread duplicates its own past — and duplicates it again at every
    checkpoint after that, feeding the model the same tool calls and results over
    and over.  That is why the comparison is ``_same_message`` and not ``==``.

    Where the two only match after the reader's projection, the STORED record is
    the one kept: the incoming copy has already lost whatever the reader dropped
    (an unparseable legacy tool_call, an unknown key on a tool_call dict), and a
    merge is not the place to normalise a durable record away.
    """
    old, new = list(old or []), list(new or [])
    common = 0
    while common < min(len(old), len(new)) and _same_message(old[common], new[common]):
        common += 1
    if common == len(old):
        return old + new[common:]
    if common == len(new):
        return old
    merged = old + new[common:]
    # A retry may submit the identical suffix after another writer already committed it.
    tail = new[common:]
    if tail and len(old) >= len(tail) and all(
            _same_message(a, b) for a, b in zip(old[-len(tail):], tail)):
        return old
    return merged


def resolve_cwd(session=None, requested=None, fallback=None):
    """Resolve the execution root before creating tools, gates or project memory.

    An explicit directory wins. Otherwise a resumed thread belongs to its saved
    workspace, even when the terminal was opened somewhere else. A missing saved
    workspace is an actionable error, never a reason to run in an unrelated tree.
    """
    saved = (session or {}).get("cwd") or ""
    path = os.path.abspath(os.path.expanduser(requested or saved or fallback or os.getcwd()))
    if not os.path.isdir(path):
        label = "session workspace" if saved and not requested else "workspace"
        raise ValueError("%s does not exist or is not a directory: %s; "
                         "use --cwd to select its current location" % (label, path))
    return path


def relocate(sid, cwd):
    """Remember an explicitly selected workspace without rewriting its transcript."""
    path = resolve_cwd(requested=cwd)
    p = _path(sid)
    if not p or not os.path.exists(p):
        raise ValueError("no such session: %s" % sid)
    with _locked(p):
        raw = _validate_raw(_load_raw(p), sid)
        old_cwd = raw.get("cwd") or ""
        if old_cwd and os.path.normcase(os.path.abspath(old_cwd)) == os.path.normcase(path):
            return path
        now = time.time()
        workspace = {"mode": "local", "path": path}
        handoffs = list(raw.get("handoffs") or [])
        handoffs.append({"at": now, "target": "directory", "previous_cwd": old_cwd,
                         "previous_workspace": raw.get("workspace") or {},
                         "workspace": workspace})
        raw.update(cwd=path, workspace=workspace, handoffs=handoffs[-50:], updated=now)
        _atomic_dump(raw, p)
    return path


def save(sid, messages, project="demo", cwd="", answer="",
         preserve_active=False):
    """Persist the conversation. Never clears a boundary nobody has inspected.

    ``save`` closes an in-flight ``active_run`` because a finished turn has no
    checkpoint to keep.  An UNCERTAIN boundary is different evidence: the tool
    may have already changed the outside world, and only ``checkpoint(terminal)``
    (the run's own clean ending) or an explicit ``reconcile_recovery`` may retire
    it.  Every surface saves the transcript after a run, so this fence lives here
    rather than in each caller.  ``preserve_active`` additionally keeps a
    still-certain checkpoint, which external-worker callers use before their
    receipt lands.
    """
    p = _path(sid)
    if not p:
        return sid
    incoming = _msgs_out(messages)
    with _locked(p):
        old = _validate_raw(_load_raw(p), sid) if os.path.exists(p) else {}
        obj = {"id": sid, "project": old.get("project") or project,
               "cwd": old.get("cwd") or cwd, "updated": time.time(),
               "messages": _merge_messages(old.get("messages"), incoming),
               "last_answer": answer or old.get("last_answer", "")}
        if old.get("title"):
            obj["title"] = old["title"]
        for field in ("forked_from", "fork_index", "lineage", "workspace", "handoffs",
                      "context_compaction"):
            if field in old:
                obj[field] = old[field]
        # Run receipts are orthogonal to the conversational transcript.  Preserve
        # them across the final transcript save without retaining an in-flight
        # ``active_run`` checkpoint, which save() intentionally closes.
        if old.get("run_receipts"):
            obj["run_receipts"] = old["run_receipts"]
        active = old.get("active_run")
        if isinstance(active, dict) and (preserve_active or _recovery_required(active)):
            # An external worker may have returned useful text while still
            # requiring reconciliation (or while its receipt failed to land).
            # Saving that text must not erase the pre-launch replay fence.
            # The same holds for a native run stopped while a tool was running:
            # writing its partial transcript must not make the unknown effect
            # look replay-safe on the next resume.
            obj["active_run"] = active
        _atomic_dump(obj, p)
    return sid


def append_run_receipt(sid, receipt, limit=40, directory=None):
    """Persist a compact, structured execution/verification receipt on a thread."""
    p = _path(sid, directory)
    if not p or not isinstance(receipt, dict):
        return False
    with _locked(p):
        if os.path.exists(p):
            # Never turn an unreadable/torn journal into a fresh-looking one.
            # Recovery callers use this return value as a publication fence.
            try:
                obj = _validate_raw(_load_raw(p), sid)
            except ValueError:
                return False
        else:
            obj = {"id": sid, "messages": []}
        existing = obj.get("run_receipts", [])
        rows = list(existing)
        rows.append(dict(receipt))
        obj["run_receipts"] = rows[-max(1, int(limit or 40)):]
        obj["updated"] = time.time()
        _atomic_dump(obj, p)
    return True


def bind_isolated_workspace(sid, info, cwd=""):
    """Record a prepared workspace before dispatch, under the caller's run lease."""
    p = _path(sid)
    if not p or not info.get("ok") or not os.path.isdir(info.get("dir") or ""):
        raise ValueError("isolated workspace is unavailable")
    with _locked(p):
        raw = _validate_raw(_load_raw(p), sid) if os.path.exists(p) else {"id": sid, "messages": []}
        workspace = {"mode": "isolated", "path": info["dir"], "branch": info["branch"],
                     "origin": info["root"], "base_commit": info.get("base_commit") or ""}
        raw.update(cwd=info["dir"], workspace=workspace, updated=time.time())
        raw["handoffs"] = (list(raw.get("handoffs") or []) + [
            {"at": raw["updated"], "target": "isolated", "workspace": workspace,
             "from": cwd}])[-50:]
        _atomic_dump(raw, p)
    return workspace


def checkpoint(sid, messages, project="demo", cwd="", run_id="", turn=0,
               state="turn_boundary", detail=None, terminal=False):
    """Continuously persist an in-flight run at replay-safe boundaries.

    ``save`` remains the public conversation operation.  This variant also
    records where execution was when the process disappeared.  A model call is
    safe to retry; an interrupted tool may have changed the outside world and is
    therefore marked ``recovery_required`` on the next read instead of replayed.
    """
    p = _path(sid)
    if not p:
        return sid
    incoming = _msgs_out(messages)
    with _locked(p):
        old = _validate_raw(_load_raw(p), sid) if os.path.exists(p) else {}
        obj = dict(old)
        obj.update({"id": sid, "project": old.get("project") or project,
                    "cwd": old.get("cwd") or cwd, "updated": time.time(),
                    "messages": _merge_messages(old.get("messages"), incoming)})
        if terminal:
            obj.pop("active_run", None)
        else:
            obj["active_run"] = {
                "run_id": str(run_id or ""), "turn": max(0, int(turn or 0)),
                "state": str(state or "turn_boundary"),
                "detail": detail if isinstance(detail, dict) else {},
                "updated": time.time(),
            }
        _atomic_dump(obj, p)
    return sid


def recovery_state(sid, directory=None):
    """Describe whether an interrupted session may be resumed automatically."""
    p = _path(sid, directory)
    if not p or not os.path.exists(p):
        return None
    with _locked(p):
        try:
            raw = _validate_raw(_load_raw(p), sid)
        except ValueError as exc:
            return {
                "state": "invalid", "updated": _mtime(p),
                "recovery_required": True, "auto_resumable": False,
                "reason": "session journal requires inspection: %s" % exc,
            }
    active = raw.get("active_run")
    if not isinstance(active, dict):
        return None
    out = dict(active)
    state = out.get("state") or "unknown"
    uncertain = _recovery_required(active)
    out["recovery_required"] = uncertain
    out["auto_resumable"] = not uncertain and state not in ("terminal", "canceled")
    if uncertain:
        out["reason"] = ("the process stopped while a tool was executing; inspect the outside "
                         "world before retrying so an irreversible effect is not duplicated")
    return out


def _recovery_required(active):
    """One definition of 'a human must look before this thread moves again'."""
    if not isinstance(active, dict):
        return False
    return (active.get("state") in ("executing_tool", "external_action")
            and not _replay_safe_read(active))


def replay_safe_boundary(state, detail):
    """Is this in-flight boundary a host-attested built-in read?

    Exposed for the execution loop, which decides at the end of an interrupted
    run whether the boundary it stopped at still deserves auto-resume.  Sharing
    this predicate keeps that decision identical to the one recovery/resume use.
    """
    return _replay_safe_read({"state": state,
                              "detail": detail if isinstance(detail, dict) else {}})


def _replay_safe_read(active):
    """Only a host-attested built-in read can bypass effect reconciliation.

    Tool names and MCP read-only hints alone are not authority. The execution
    loop records this flag from the actual built-in implementation, before the
    call starts. An inner read cannot make its enclosing script replay-safe.
    """
    detail = active.get("detail") or {}
    return (active.get("state") == "executing_tool"
            and detail.get("replay_safe") is True
            and not detail.get("internal")
            and detail.get("tool_name") in {
                "read_file", "glob", "grep", "memory_search", "delegate"})


def _pending_calls(messages):
    """Find unanswered calls without replaying any of them."""
    pending = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    pending[call["id"]] = call
        elif msg.get("role") == "tool":
            pending.pop(msg.get("tool_call_id"), None)
    return list(pending.values())


def _resume_messages(raw):
    """Pair interrupted safe calls in the resume view; preserve the disk journal.

    A model/tool boundary can interrupt a batch before its remaining calls run.
    Providers reject these orphaned calls. Backfilling observations lets the
    agent decide what still needs doing; loading a thread never executes tools.
    The next normal checkpoint persists this augmented, prefix-compatible view.
    """
    messages = list(raw.get("messages") or [])
    active = raw.get("active_run") or {}
    state = active.get("state")
    safe_read = _replay_safe_read(active)
    if state not in ("turn_boundary", "calling_model", "model_complete", "tool_complete") \
            and not safe_read:
        return messages
    detail = active.get("detail") or {}
    for call in _pending_calls(messages):
        interrupted = safe_read and call["id"] == detail.get("tool_call_id")
        content = ("RECOVERY: this read was interrupted and its result is unavailable. "
                   "Run the read again if its output is still needed." if interrupted else
                   "RECOVERY: this queued tool call did not execute before the run was "
                   "interrupted. Reassess whether it is still needed before requesting it again.")
        messages.append({"role": "tool", "tool_call_id": call["id"],
                         "name": call.get("name") or "tool", "content": content})
    return messages


def resume_after_interrupt(sid, fallback=None):
    """Rebuild an interactive surface's live thread after Ctrl-C ended a turn.

    A REPL/TUI holds the PRE-turn history in memory.  Continuing from it after an
    interrupt deletes everything the turn actually did from the conversation and
    invites the agent to ask for the same edits again.  The durable journal is
    the only record that saw those actions, so recover from it — and report
    plainly when the thread is fenced on an effect nobody has inspected yet,
    because then no next turn may run at all.
    """
    previous = list(fallback or [])
    recovery = recovery_state(sid)
    checked = load_checked(sid)
    blocked = bool(recovery and recovery.get("recovery_required"))
    reason = (recovery or {}).get("reason", "") if blocked else ""
    if checked.get("status") == "invalid":
        blocked = True
        reason = reason or checked.get("reason") or "session journal requires inspection"
    messages = previous
    if checked.get("status") == "ok":
        durable = (checked.get("session") or {}).get("messages") or []
        # Only ever move forward: a journal that is somehow shorter than what
        # this process already holds is not a reason to forget the difference.
        # A fenced thread still recovers its messages — they are what the user
        # is shown and reconciles against; ``blocked`` is what stops the turn.
        if len(durable) >= len(previous):
            messages = durable
    return {"messages": messages, "recovery": recovery,
            "blocked": blocked,
            "reason": reason or ("session journal requires inspection" if blocked else "")}


def active_runs(limit=100, directory=None):
    """List durable in-flight/recovery sessions for Activity and health views."""
    d = _dir(directory)
    rows = []
    for metadata in _indexed_rows(directory=d):
        if metadata.get("_has_active") is False:
            continue
        sid = metadata["id"]
        state = recovery_state(sid, directory)
        if state:
            state = dict(state); state["session_id"] = sid
            rows.append(state)
    rows.sort(key=lambda x: float(x.get("updated") or 0), reverse=True)
    return rows[:max(0, int(limit))]


def reconcile_recovery(sid, resolution, note="", confirmed=False, directory=None):
    """Resolve an uncertain in-flight tool boundary after explicit inspection.

    ``completed`` records a synthetic tool result saying the effect was observed;
    ``not_fired`` records that no effect was found and lets the model choose a
    retry; ``cancel`` closes the active run.  No branch silently replays a tool.
    """
    if not confirmed:
        raise ValueError("recovery reconciliation requires confirmed=True")
    if resolution not in ("completed", "not_fired", "cancel"):
        raise ValueError("resolution must be completed, not_fired, or cancel")
    p = _path(sid, directory)
    if not p or not os.path.exists(p):
        raise KeyError("no such session")
    with _locked(p):
        raw = _validate_raw(_load_raw(p), sid)
        active = raw.get("active_run")
        if not isinstance(active, dict) or active.get("state") not in (
                "executing_tool", "external_action"):
            raise ValueError("session is not awaiting recovery reconciliation")
        detail = active.get("detail") if isinstance(active.get("detail"), dict) else {}
        if detail.get("operation") == "workspace_handoff" and resolution == "completed":
            previous = raw.get("workspace") or {}
            if (previous.get("mode") != "isolated" or
                    detail.get("source") != previous.get("path") or
                    detail.get("destination") != previous.get("origin")):
                raise ValueError("handoff recovery does not match this conversation's workspace")
            destination = resolve_cwd(requested=detail.get("destination"))
            workspace = {"mode": "local", "path": destination,
                         "from_branch": previous.get("branch") or "",
                         "reconciled": "completed"}
            raw.update(cwd=destination, workspace=workspace)
            raw["handoffs"] = (list(raw.get("handoffs") or []) + [{"at": time.time(),
                "target": "local", "workspace": workspace, "previous_workspace": previous,
                "reconciled": "completed"}])[-50:]
        call_id = detail.get("tool_call_id")
        name = detail.get("tool_name") or "tool"
        messages = list(raw.get("messages") or [])
        if resolution == "cancel":
            for call in _pending_calls(messages):
                content = ("RECOVERY: the user canceled recovery. This action's result is "
                           "unknown; inspect its effects before considering a retry."
                           if call["id"] == call_id else
                           "RECOVERY: this queued tool call was canceled before execution.")
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "name": call.get("name") or "tool", "content": content})
            raw["messages"] = messages
            raw.pop("active_run", None)
        else:
            if call_id:
                outcome = ("the user inspected the external system and confirmed the action completed"
                           if resolution == "completed" else
                           "the user inspected the external system and confirmed the action did not fire")
                if note:
                    outcome += ": " + str(note)[:1000]
                already_paired = any(
                    msg.get("role") == "tool" and msg.get("tool_call_id") == call_id
                    for msg in messages if isinstance(msg, dict))
                if already_paired:
                    messages.append({"role": "user", "content": "RECOVERY: " + outcome,
                                     "source": "harness", "kind": "recovery_notice"})
                else:
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "name": name, "content": "RECOVERY: " + outcome})
                raw["messages"] = messages
            active = dict(active)
            active.update(state="turn_boundary", updated=time.time(),
                          detail={"reconciled": resolution, "note": str(note)[:1000]})
            raw["active_run"] = active
        raw["updated"] = time.time()
        _atomic_dump(raw, p)
    return recovery_state(sid, directory)


def append_exchange(sid, user_text, answer, project="web", cwd=""):
    """Add one question-and-answer to a session without running a model.

    A command the desktop carried out itself — "open Xcode", "play Cruel Summer" — is still something
    that happened in a conversation, and a conversation that cannot remember it is one people will not
    trust. The fast path is an optimisation, not a different place for things to happen, so what it
    does is written where everything else is.

    Creates the session when it does not exist yet, so the first thing said in a new chat can be a
    command.
    """
    if not sid:
        return sid
    p = _path(sid)
    if not p:
        return sid
    with _locked(p):
        if os.path.exists(p):
            existing = _load_raw(p)
            # A torn or structurally invalid journal is recovery evidence.  Do
            # not turn it into a fresh-looking conversation merely because a
            # fast-path command or external worker finished successfully.
            if not isinstance(existing, dict):
                raise ValueError("cannot append to an unreadable session journal")
        else:
            existing = {}
        existing = _validate_raw(existing, sid)
        raw_messages = existing.get("messages", [])
        messages = list(raw_messages)
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": answer})
        obj = dict(existing)
        obj.update({"id": sid, "project": existing.get("project") or project,
                    "cwd": existing.get("cwd") or cwd, "updated": time.time(),
                    "messages": messages, "last_answer": answer})
        _atomic_dump(obj, p)
    return sid


def _atomic_dump(obj, p):
    # write to a temp file then os.replace() so a concurrent reader never sees a truncated file and
    # two near-simultaneous writers to the same session id can't interleave into corruption. The temp
    # name MUST be unique per writer: under ThreadingHTTPServer two threads saving the same session id
    # share a pid, so a pid-only name collided and corrupted the file the comment claims to protect.
    tmp = "%s.%d.%s.tmp" % (p, os.getpid(), os.urandom(6).hex())
    # Encode once to use CPython's fast encoder and avoid per-token file writes.
    # This allocates a temporary string the size of the journal. Encoding before
    # opening the file also keeps serialization failures from leaving a partial
    # temp file. Flush, fsync and atomic replacement remain the durability boundary.
    blob = json.dumps(obj, ensure_ascii=False, default=str, allow_nan=False)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)       # conversations/tool output are private user data
        except OSError:
            pass
        # Antivirus/indexers and another Python process can briefly hold the
        # destination without delete sharing on Windows. The inter-process lock
        # serializes our writers but cannot control those readers; bounded retry
        # keeps a transient WinError 5 from killing a durable checkpoint.
        for attempt in range(7):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt >= 6:
                    raise
                time.sleep(.01 * (2 ** attempt))
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def load(sid):
    p = _path(sid)
    if not p or not os.path.exists(p):
        return None
    with _locked(p):
        s = _load_raw(p)
    try:
        s = _validate_raw(s, sid)
        s["messages"] = _msgs_in(_resume_messages(s))
        return s
    except Exception:
        return None


def load_checked(sid, directory=None):
    """Load a durable session while distinguishing missing from corrupt state.

    The legacy ``load`` API intentionally returns ``None`` for both.  Mission
    workers need a fail-closed answer: treating a truncated later-slice journal
    as a new conversation can repeat edits against an already-mutated tree.
    """
    p = _path(sid, directory)
    if not p or not os.path.exists(p):
        return {"status": "missing", "session": None}
    try:
        with _locked(p):
            with open(p, encoding="utf-8") as fh:
                raw = json.load(fh, parse_constant=_reject_json_constant)
        raw = _validate_raw(raw, sid)
        messages = _resume_messages(raw)
        raw["messages"] = _msgs_in(messages)
        return {"status": "ok", "session": raw}
    except Exception as exc:
        return {"status": "invalid", "session": None,
                "reason": "%s: %s" % (type(exc).__name__, exc)}


def storage_bytes(sid, directory=None):
    """Return the exact durable session-file size without exposing its path."""
    p = _path(sid, directory)
    if not p:
        return 0
    try:
        return max(0, int(os.path.getsize(p)))
    except OSError:
        return 0


def delete(sid):
    p = _path(sid)
    if not p:
        return False
    with _locked(p):
        try:
            os.remove(p)
            from . import session_index
            session_index.forget(os.path.dirname(p), sid)
            return True
        except OSError:
            return False


def set_title(sid, title):
    """Pin a human title override (shown in the sidebar instead of the first message)."""
    p = _path(sid)
    if not p:
        return False
    with _locked(p):
        s = _load_raw(p)
        if not s:
            return False
        s["title"] = (title or "").strip()[:80]
        s["updated"] = time.time()
        _atomic_dump(s, p)
    return True


def _mtime(path):
    # a *.json can be deleted between listdir and here (concurrent delete / rewrite); a missing
    # file sorts oldest instead of raising FileNotFoundError and breaking the whole sidebar.
    try:
        return os.path.getmtime(path)
    except OSError:
        return float("-inf")


def latest():
    """Most recently updated session id, or None."""
    d = _dir()
    files = [f for f in os.listdir(d) if f.endswith(".json")]
    if not files:
        return None
    newest = max(files, key=lambda f: _mtime(os.path.join(d, f)))
    return newest[:-5]


def _recent_row(sid, s, mtime):
    msgs = s.get("messages", [])
    turns = sum(1 for m in msgs if m.get("role") == "user" and m.get("source") != "harness")
    # the thread's TITLE is the first user message (what a person recognizes it by), not the
    # model's answer, which tends to be a generic lead-in that reads poorly as a sidebar label.
    title = (s.get("title") or "").strip()
    if not title:
        for m in msgs:
            if m.get("role") != "user" or m.get("source") == "harness":
                continue
            c = m.get("content")
            if isinstance(c, list):        # multimodal (attached image) -> title from text blocks
                c = " ".join(b.get("text", "") for b in c
                             if isinstance(b, dict) and b.get("type") == "text") or "[image]"
            if isinstance(c, str) and c.strip():
                title = " ".join(c.split()); break
    # cheap edit/touch counts so the Map's run picker can flag (and sort) the runs that actually
    # changed code — the ones worth a diff — instead of burying them under chatty Q&A runs.
    # DISTINCT files, not tool calls. Counting calls made a run that read one file eleven times
    # read as "·11" beside a run that changed eleven files, and the map's landing view believed
    # it: it opened on a run whose whole footprint was two stars. What the picker promises is
    # how much of the codebase the run is about, so that is what it has to count.
    touched, edited = set(), set()
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            name = (getattr(tc, "name", None) or (tc.get("name") if isinstance(tc, dict) else "") or "").lower()
            args = getattr(tc, "args", None) or (tc.get("args") if isinstance(tc, dict) else {}) or {}
            p = args.get("path") or args.get("file_path") or args.get("file")
            if p:
                touched.add(str(p))
                if any(k in name for k in ("edit", "write", "create")):
                    edited.add(str(p))
    n_edit, n_touch = len(edited), len(touched)
    # `cwd` is where the run happened, and it is the only DURABLE record of where this user keeps
    # code: the web server is spawned without a cwd of its own, so on a shortcut launch it
    # inherits whatever Explorer hands it, and the in-memory run list is empty at startup. The
    # star-map's project discovery seeds from these.
    return {"id": sid, "turns": turns, "title": title[:72], "cwd": s.get("cwd") or "",
                "updated": float(s.get("updated") or mtime),
                "last": (s.get("last_answer") or "")[:60], "edits": n_edit, "touches": n_touch,
                "forked_from": s.get("forked_from") or "",
                "workspace": s.get("workspace") if isinstance(s.get("workspace"), dict) else {},
                "_has_active": "active_run" in s,
                "_fork_index": s.get("fork_index", 0), "_custom_title": s.get("title") or ""}


def _indexed_rows(n=None, directory=None):
    from . import session_index

    d = _dir(directory)
    files = []
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        sid = name[:-5]
        path = _path(sid, d)
        if not path:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        files.append((sid, path, stat))
    files.sort(key=lambda item: item[2].st_mtime_ns, reverse=True)
    selected = files if n is None else files[:max(0, int(n))]
    stamps = {sid: session_index.fingerprint(stat) for sid, _, stat in selected}
    cached = session_index.read(d, stamps) if selected else {}
    out, updates = [], []
    for sid, path, stat in selected:
        row = cached.get(sid)
        if row is None:
            # Only immutable display metadata is cached. Recovery and resume
            # continue to validate the full journal on every authoritative read.
            with _locked(path):
                try:
                    before = os.stat(path)
                except OSError:
                    continue
                raw = _load_raw(path)
                try:
                    data = _validate_raw(raw, sid)
                    data["messages"] = _msgs_in(data.get("messages", []))
                    row = _recent_row(sid, data, before.st_mtime)
                except (ValueError, TypeError, AttributeError):
                    row = _recent_row(sid, {}, before.st_mtime)
                    row["_has_active"] = True  # unreadable journals must remain visible in recovery
                try:
                    after = os.stat(path)
                except OSError:
                    continue
                stamp = session_index.fingerprint(before)
                if stamp == session_index.fingerprint(after):
                    updates.append((sid, stamp, row))
        out.append(row)
    session_index.write(d, updates)
    return out


def recent(n=10):
    return [{key: value for key, value in row.items() if not key.startswith("_")}
            for row in _indexed_rows(n)]


def timeline(sid):
    """Return a bounded message timeline, child forks, and workspace handoffs."""
    p = _path(sid)
    if not p or not os.path.exists(p):
        raise KeyError("no such session")
    with _locked(p):
        raw = _validate_raw(_load_raw(p), sid)
    nodes = []
    for index, message in enumerate(raw.get("messages") or []):
        role = str(message.get("role") or "")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
        nodes.append({"index": index, "role": role,
                      "source": message.get("source") or "",
                      "kind": message.get("kind") or "",
                      "summary": " ".join(str(content or "").split())[:240],
                      "tool_calls": len(message.get("tool_calls") or [])})
    children = []
    for child in _indexed_rows():
        if child["id"] != sid and child.get("forked_from") == sid:
            children.append({"id": child["id"], "fork_index": child.get("_fork_index", 0),
                             "title": child.get("_custom_title") or "", "updated": child.get("updated", 0)})
    children.sort(key=lambda x: float(x.get("updated") or 0))
    return {"id": sid, "title": raw.get("title") or "", "nodes": nodes, "children": children,
            "forked_from": raw.get("forked_from") or "", "fork_index": raw.get("fork_index"),
            "workspace": raw.get("workspace") if isinstance(raw.get("workspace"), dict) else {},
            "handoffs": list(raw.get("handoffs") or [])[-50:]}


def fork(sid, at_index, *, child_id="", title=""):
    """Create a new durable session from one exact message boundary."""
    source_path = _path(sid)
    if not source_path or not os.path.exists(source_path):
        raise KeyError("no such session")
    with _locked(source_path):
        source = _validate_raw(_load_raw(source_path), sid)
    messages = list(source.get("messages") or [])
    if (isinstance(at_index, bool) or not isinstance(at_index, int) or
            at_index < 0 or at_index > len(messages)):
        raise ValueError("fork index must be a message boundary")
    child_id = child_id or new_id()
    target = _path(child_id)
    if not target:
        raise ValueError("invalid child session id")
    if os.path.exists(target):
        raise ValueError("child session already exists")
    now = time.time()
    child = {"id": child_id, "project": source.get("project") or "web",
             "cwd": source.get("cwd") or "", "updated": now,
             "messages": messages[:at_index], "last_answer": "",
             "title": (title or ((source.get("title") or sid) + " · fork"))[:80],
             "forked_from": sid, "fork_index": at_index,
             "lineage": list(source.get("lineage") or [])[-30:] + [sid],
             "workspace": dict(source.get("workspace") or {})}
    with _locked(target):
        _atomic_dump(child, target)
    return {"id": child_id, "forked_from": sid, "fork_index": at_index,
            "messages": at_index, "cwd": child["cwd"], "title": child["title"]}


def handoff(sid, target, *, confirm=False, remove_isolated=False):
    """Move a stopped conversation's workspace under the same execution lease."""
    from . import session_owner
    lease = session_owner.try_acquire(sid, label="workspace-handoff")
    if lease is None:
        raise ValueError("this conversation is running; stop it before moving its workspace")
    try:
        return _handoff_owned(sid, target, confirm=confirm, remove_isolated=remove_isolated)
    finally:
        lease.release()


def _handoff_owned(sid, target, *, confirm=False, remove_isolated=False):
    """Move a workspace while the caller owns the execution lease, without blocking readers."""
    p = _path(sid)
    if not p or not os.path.exists(p):
        raise KeyError("no such session")
    target = str(target or "").lower()
    if target not in {"isolated", "local"}:
        raise ValueError("handoff target must be isolated or local")
    from . import worktree
    with _locked(p):
        raw = _validate_raw(_load_raw(p), sid)
    workspace = dict(raw.get("workspace") or {})
    previous_workspace, previous_cwd = dict(workspace), raw.get("cwd")
    previous_active = raw.get("active_run")
    if _recovery_required(previous_active):
        raise ValueError("inspect the interrupted operation before moving its workspace")
    old_path = ""
    if target == "isolated":
        if workspace.get("mode") == "isolated" and os.path.isdir(workspace.get("path") or ""):
            return {"ok": True, "session": sid, "workspace": workspace, "existing": True}
        result = worktree.prepare(raw.get("cwd") or os.getcwd(), sid, label=raw.get("title") or sid)
        if not result.get("ok"):
            raise ValueError(result.get("error") or "could not create isolated workspace")
        workspace = {"mode": "isolated", "path": result["dir"], "branch": result["branch"],
                     "origin": result["root"], "base_commit": result.get("base_commit") or ""}
    else:
        if workspace.get("mode") != "isolated":
            return {"ok": True, "session": sid, "workspace": workspace, "existing": True}
        def before_apply():
            with _locked(p):
                latest = _validate_raw(_load_raw(p), sid)
                if latest.get("workspace") != previous_workspace or latest.get("cwd") != previous_cwd:
                    raise ValueError("workspace changed before handoff; reload this conversation")
                latest["active_run"] = {"run_id": "workspace-handoff", "turn": 0,
                    "state": "external_action", "updated": time.time(),
                    "detail": {"operation": "workspace_handoff", "source": workspace.get("path"),
                               "destination": workspace.get("origin")}}
                _atomic_dump(latest, p)
        result = worktree.handoff_to_local(
            workspace.get("path") or "", workspace.get("origin") or "",
            workspace.get("base_commit") or "", confirm=confirm, before_apply=before_apply)
        if not result.get("ok"):
            raise ValueError(result.get("error") or "handoff failed")
        old_path = workspace.get("path") or ""
        workspace = {"mode": "local", "path": workspace.get("origin") or "",
                     "from_branch": workspace.get("branch") or "", "applied_files": result.get("files") or []}
    with _locked(p):
        # A title/receipt may have changed while Git ran. Preserve that metadata.
        raw = _validate_raw(_load_raw(p), sid)
        if dict(raw.get("workspace") or {}) != previous_workspace or raw.get("cwd") != previous_cwd:
            raise ValueError("workspace changed during handoff; inspect before retrying")
        if target == "local":
            if previous_active is None:
                raw.pop("active_run", None)
            else:
                raw["active_run"] = previous_active
        now = time.time()
        raw.update(cwd=workspace["path"], workspace=workspace, updated=now)
        raw["handoffs"] = (list(raw.get("handoffs") or []) +
                           [{"at": now, "target": target, "workspace": workspace}])[-50:]
        _atomic_dump(raw, p)
    # A requested cleanup happens only after the new durable location is committed.
    if remove_isolated and old_path:
        released = worktree.release(old_path, force=True)
        workspace["isolated_removed"] = bool(released.get("ok"))
    return {"ok": True, "session": sid, "workspace": workspace}
