"""Local session persistence — save/load a conversation THREAD so `collie run --continue`,
`--resume <id>`, and `collie repl` carry the full back-and-forth across separate CLI invocations.
This is the continuity every interactive harness has; collie's version is plain local JSON files
(data/sessions/<id>.json) — no server, no account, on brand. The composer's own history elision
keeps a long thread from bloating the prefix, so sessions can grow safely.
"""
import ast
import contextlib
import json
import os
import threading
import time


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


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
            s = json.load(f)
        return s if isinstance(s, dict) else None
    except Exception:
        return None


def _merge_messages(old, new):
    """Merge two histories that grew from a common prefix, preserving both completed exchanges."""
    old, new = list(old or []), list(new or [])
    common = 0
    while common < min(len(old), len(new)) and old[common] == new[common]:
        common += 1
    if common == len(old):
        return new
    if common == len(new):
        return old
    merged = old + new[common:]
    # A retry may submit the identical suffix after another writer already committed it.
    if new[common:] and len(old) >= len(new) - common and old[-(len(new) - common):] == new[common:]:
        return old
    return merged


def save(sid, messages, project="demo", cwd="", answer=""):
    p = _path(sid)
    if not p:
        return sid
    incoming = _msgs_out(messages)
    with _locked(p):
        old = _load_raw(p) or {}
        obj = {"id": sid, "project": old.get("project") or project,
               "cwd": old.get("cwd") or cwd, "updated": time.time(),
               "messages": _merge_messages(old.get("messages"), incoming),
               "last_answer": answer or old.get("last_answer", "")}
        if old.get("title"):
            obj["title"] = old["title"]
        # Run receipts are orthogonal to the conversational transcript.  Preserve
        # them across the final transcript save without retaining an in-flight
        # ``active_run`` checkpoint, which save() intentionally closes.
        if old.get("run_receipts"):
            obj["run_receipts"] = old["run_receipts"]
        _atomic_dump(obj, p)
    return sid


def append_run_receipt(sid, receipt, limit=40):
    """Persist a compact, structured execution/verification receipt on a thread."""
    p = _path(sid)
    if not p or not isinstance(receipt, dict):
        return False
    with _locked(p):
        obj = _load_raw(p) or {"id": sid, "messages": []}
        rows = list(obj.get("run_receipts") or [])
        rows.append(dict(receipt))
        obj["run_receipts"] = rows[-max(1, int(limit or 40)):]
        obj["updated"] = time.time()
        _atomic_dump(obj, p)
    return True


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
        old = _load_raw(p) or {}
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
        raw = _load_raw(p) or {}
    active = raw.get("active_run")
    if not isinstance(active, dict):
        return None
    out = dict(active)
    state = out.get("state") or "unknown"
    uncertain = state in ("executing_tool", "external_action")
    out["recovery_required"] = uncertain
    out["auto_resumable"] = not uncertain and state not in ("terminal", "canceled")
    if uncertain:
        out["reason"] = ("the process stopped while a tool was executing; inspect the outside "
                         "world before retrying so an irreversible effect is not duplicated")
    return out


def active_runs(limit=100, directory=None):
    """List durable in-flight/recovery sessions for Activity and health views."""
    d = _dir(directory)
    rows = []
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        sid = name[:-5]
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
        raw = _load_raw(p) or {}
        active = raw.get("active_run")
        if not isinstance(active, dict) or active.get("state") not in (
                "executing_tool", "external_action"):
            raise ValueError("session is not awaiting recovery reconciliation")
        if resolution == "cancel":
            raw.pop("active_run", None)
        else:
            detail = active.get("detail") if isinstance(active.get("detail"), dict) else {}
            call_id = detail.get("tool_call_id")
            name = detail.get("tool_name") or "tool"
            messages = list(raw.get("messages") or [])
            if call_id:
                outcome = ("the user inspected the external system and confirmed the action completed"
                           if resolution == "completed" else
                           "the user inspected the external system and confirmed the action did not fire")
                if note:
                    outcome += ": " + str(note)[:1000]
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
        existing = _load_raw(p) or {}
        messages = list(existing.get("messages") or [])
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
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, default=str)
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
    if s is not None:
        s["messages"] = _msgs_in(s.get("messages"))
    return s


def delete(sid):
    p = _path(sid)
    if not p:
        return False
    with _locked(p):
        try:
            os.remove(p)
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


def recent(n=10):
    d = _dir()
    files = [f for f in os.listdir(d) if f.endswith(".json")]
    files.sort(key=lambda f: _mtime(os.path.join(d, f)), reverse=True)
    out = []
    for f in files[:n]:
        s = load(f[:-5]) or {}
        msgs = s.get("messages", [])
        turns = sum(1 for m in msgs if m.get("role") == "user")
        # the thread's TITLE is the first user message (what a person recognizes it by), not the
        # model's answer, which tends to be a generic lead-in that reads poorly as a sidebar label.
        title = (s.get("title") or "").strip()
        if not title:
            for m in msgs:
                if m.get("role") != "user":
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
        out.append({"id": f[:-5], "turns": turns, "title": title[:72], "cwd": s.get("cwd") or "",
                    "last": (s.get("last_answer") or "")[:60], "edits": n_edit, "touches": n_touch})
    return out
