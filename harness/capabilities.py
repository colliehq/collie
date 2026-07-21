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
    d = os.environ.get("COLLIE_NOTES_DIR") or os.path.expanduser("~/.collie/notes")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_path(name) -> str:
    # basename only — never let args steer the write outside the sandbox dir
    base = os.path.basename(str(name or "notes.txt")) or "notes.txt"
    return os.path.join(notes_dir(), base)


def _note_execute(record):
    p = _safe_path(record.args.get("file"))
    text = str(record.args.get("text", ""))
    with open(p, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    return {"path": p}


class _FileReread(_v.Verifier):
    channels = ("file-reread",)
    require_assert = True


def _note_verify(record, result):
    """Independent post-check: re-open the file and assert the text landed."""
    p = _safe_path(record.args.get("file"))
    text = str(record.args.get("text", ""))
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


def register_builtins():
    """Idempotent: register the shipped capabilities into the jobs registry."""
    register(Capability(
        "note.append", execute=_note_execute, verify=_note_verify,
        reversible=True, risk="reversible",
        description="append a line to a note/to-do file in the user's notes dir",
        args_hint='{"file": "<filename e.g. todo.txt>", "text": "<the note line>"}'))
    from .research import register_research
    register_research()
