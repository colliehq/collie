"""Built-in delegate capabilities — real, registered, executable end to end.

The spine (verifier/observe/actions/jobs) is capability-agnostic; this registers
concrete capabilities so `collie jobs` actually does work and verifies it, not
only in tests. The first built-in is deliberately SAFE and REVERSIBLE — a note
append to a sandboxed file — so the full chain (propose -> leash -> execute ->
independent re-read done-check -> receipt) runs live without any risky external
side effect. Irreversible capabilities (send/publish/pay) are intentionally NOT
shipped here; they belong behind explicit authority + the confirm token.

The done-check follows the module's own rule: verify by RE-READING the file from
disk (an independent read), never by trusting the write call's own return value.
"""

from __future__ import annotations

import os

from . import verifier as _v
from .jobs import Capability, register


def notes_dir() -> str:
    d = (os.environ.get("COLLIE_NOTES_DIR")
         or os.path.join(os.environ.get("COLLIE_STATE_DIR")
                         or os.path.expanduser("~/.collie"), "notes"))
    os.makedirs(d, exist_ok=True)
    return d


def _safe_path(name) -> str:
    # basename only — never let args steer the write outside the sandbox dir
    base = os.path.basename(str(name or "notes.txt")) or "notes.txt"
    return os.path.join(notes_dir(), base)


def _note_text(record) -> str:
    # guard the RAW value: str(None) == "None" would be written & pass — so a
    # None/non-str text must collapse to empty, not the literal "None".
    raw = record.args.get("text")
    return raw if isinstance(raw, str) else ""


def _note_execute(record):
    p = _safe_path(record.args.get("file"))
    text = _note_text(record)
    if not text.strip():
        return {"path": p, "skipped": "empty note"}   # write nothing; verify will FAIL it
    with open(p, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    return {"path": p}


class _FileReread(_v.Verifier):
    channels = ("file-reread",)
    require_assert = True


def _note_verify(record, result):
    """Independent post-check: re-open the file and assert the text landed."""
    p = _safe_path(record.args.get("file"))
    text = _note_text(record)
    if not text.strip():
        # `"" in content` is ALWAYS True — an empty (or None) note would fabricate
        # a done_verified for a write that recorded nothing. Fail it honestly.
        return _v.Verdict(_v.FAILED, "empty note — nothing to record")
    obs = []
    try:
        with open(p, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        pass                                    # couldn't observe -> INCONCLUSIVE
    else:
        present = text in content
        obs = [_v.Observation(channel="file-reread", at=2, ok=present, asserted=True,
                              detail=f"reread {os.path.basename(p)}: "
                                     f"{'present' if present else 'absent'}")]
    return _FileReread().verdict(
        [_v.Mutation(at=1, kind="note.append", reversible=True)], obs)


def _note_list_execute(record):
    """Read back notes: one file's content if `file` given, else a listing of all
    note files with a short preview. Always delivers."""
    d = notes_dir()
    fname = record.args.get("file")
    if fname:
        try:
            content = open(_safe_path(fname), encoding="utf-8").read()
        except OSError:
            content = None                          # unreadable/nonexistent -> distinct from empty
        return {"file": os.path.basename(_safe_path(fname)), "content": content}
    files = []
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        try:
            lines = open(p, encoding="utf-8").read().splitlines()
        except OSError:
            lines = []
        files.append({"file": f, "lines": len(lines), "preview": " · ".join(lines[:3])[:160]})
    return {"files": files}


def _note_list_verify(record, result):
    """A listing IS the deliverable. But a single-file read that FAILED (file
    missing/unreadable -> content is None) must not report "read <file>" — that
    would fabricate a read that never happened."""
    result = result or {}
    if "content" in result:                         # single-file read branch
        if result["content"] is None:
            return _v.Verdict(_v.FAILED, f"could not read {result.get('file')}")
        return _v.Verdict(_v.VERIFIED, f"read {result.get('file')}")
    n = len(result.get("files") or [])
    return _v.Verdict(_v.VERIFIED, f"listed {n} note file(s)")


def register_builtins():
    """Idempotent: register the shipped capabilities into the jobs registry."""
    register(Capability(
        "note.append", execute=_note_execute, verify=_note_verify,
        reversible=True, risk="reversible",
        description="append a line to a note/to-do file in the user's notes dir",
        args_hint='{"file": "<filename e.g. todo.txt>", "text": "<the note line>"}'))
    register(Capability(
        "note.list", execute=_note_list_execute, verify=_note_list_verify,
        reversible=True, risk="reversible",
        description="read back the user's notes/todos (all files, or one file's content)",
        args_hint='{"file": "<optional filename; omit to list all>"}'))
    from .research import register_research
    register_research()
    from .everyday import register_everyday
    register_everyday()
