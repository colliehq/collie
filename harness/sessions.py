"""Local session persistence — save/load a conversation THREAD so `collie run --continue`,
`--resume <id>`, and `collie repl` carry the full back-and-forth across separate CLI invocations.
This is the continuity every interactive harness has; collie's version is plain local JSON files
(data/sessions/<id>.json) — no server, no account, on brand. The composer's own history elision
keeps a long thread from bloating the prefix, so sessions can grow safely.
"""
import ast
import json
import os
import time


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


def _dir():
    # COLLIE_SESSIONS_DIR lets tests (and throwaway runs) write to a temp store instead of the
    # user's real data/sessions/ — so a mock-provider test suite never floods the Map's run list.
    d = os.environ.get("COLLIE_SESSIONS_DIR")
    if not d:
        from .cli import DATA
        d = os.path.join(DATA, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def _path(sid):
    """Map a session id to its JSON file, SAFELY. The web routes (/api/delete, /api/rename,
    /api/session, /api/stream?session=) feed `sid` straight from the URL, so an id like
    "../../etc/foo" or an absolute "/etc/cron.d/x" must not escape data/sessions/ — a CSRF GET
    from any web page the user has open could otherwise read/write/delete arbitrary *.json files.
    os.path.basename() collapses both traversal and absolute paths to a bare name; the realpath
    check is belt-and-suspenders. Returns None for anything that isn't a plain id."""
    name = os.path.basename(str(sid))
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        return None
    d = _dir()
    p = os.path.join(d, name + ".json")
    if os.path.dirname(os.path.realpath(p)) != os.path.realpath(d):
        return None
    return p


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


def save(sid, messages, project="demo", cwd="", answer=""):
    p = _path(sid)
    if not p:
        return sid
    _atomic_dump({"id": sid, "project": project, "cwd": cwd, "updated": time.time(),
                  "messages": _msgs_out(messages), "last_answer": answer}, p)
    return sid


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
    existing = load(sid) or {}
    messages = list(existing.get("messages") or [])
    messages.append({"role": "user", "content": user_text})
    messages.append({"role": "assistant", "content": answer})
    return save(sid, messages, project=existing.get("project") or project,
                cwd=existing.get("cwd") or cwd, answer=answer)


def _atomic_dump(obj, p):
    # write to a temp file then os.replace() so a concurrent reader never sees a truncated file and
    # two near-simultaneous writers to the same session id can't interleave into corruption. The temp
    # name MUST be unique per writer: under ThreadingHTTPServer two threads saving the same session id
    # share a pid, so a pid-only name collided and corrupted the file the comment claims to protect.
    tmp = "%s.%d.%s.tmp" % (p, os.getpid(), os.urandom(6).hex())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, default=str)
    os.replace(tmp, p)


def load(sid):
    p = _path(sid)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        s["messages"] = _msgs_in(s.get("messages"))
        return s
    except Exception:
        return None


def delete(sid):
    p = _path(sid)
    if not p:
        return False
    try:
        os.remove(p)
        return True
    except OSError:
        return False


def set_title(sid, title):
    """Pin a human title override (shown in the sidebar instead of the first message)."""
    s = load(sid)
    if not s:
        return False
    s["title"] = (title or "").strip()[:80]
    p = _path(sid)
    if not p:
        return False
    # load() rebuilt tool_calls into ToolCall objects; re-serialize them (default=str would turn
    # them back into repr strings and reintroduce the 'str has no attribute id' crash).
    s["messages"] = _msgs_out(s.get("messages"))
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
