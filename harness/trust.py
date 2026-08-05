"""Which directories the user has decided to trust.

A repository can ship things that widen what collie may do without asking: command
prefixes in `.collie/allow.toml`, skills in `.collie/skills/`. Cloning a repo must not
grant them. So a repo-provided allowance is INERT until the user trusts that exact
directory, and the decision is recorded here — user-local, keyed by canonical path.

Trust follows the PATH, not a snapshot of the contents. Once you have said you trust
`~/src/collie`, later edits to its config are accepted; you are trusting the project and
the people who can write to it, which is the thing a person can actually reason about.
Re-confirming on every file change would train the reflex this exists to avoid.

This replaces `COLLIE_TRUST_REPO_SKILLS=1`, which was a single global switch: turning it
on for a project you wrote turned it on for every repo you would ever clone.

The inviolable rule, same as the risk overrides: **nothing but the user writes this
file.** Not a skill, not a repo config, not a persona, not the model. A project may
declare what it wants; only the person decides whether to believe it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def state_dir() -> str:
    d = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    os.makedirs(d, exist_ok=True)
    return d


def canonical(path) -> str:
    """One spelling per directory, so `~/src/x`, `./x` and a symlink to it cannot become
    three different trust decisions — or worse, one bypass of another."""
    return str(Path(path).expanduser().resolve())


class TrustStore:
    def __init__(self, path: str = None):
        self.path = path or os.path.join(state_dir(), "trust.json")

    def _load(self) -> set:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return set()
        vals = data.get("trusted") if isinstance(data, dict) else None
        if not isinstance(vals, list):
            return set()
        return {str(v) for v in vals if isinstance(v, str) and v}

    def _save(self, values: set) -> None:
        from . import plat
        tmp = self.path + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"trusted": sorted(values)}, f, indent=2)
            f.write("\n")
        try:
            plat.chmod_private(tmp)       # owner-only where the OS has a notion of it
        except Exception:
            pass
        os.replace(tmp, self.path)        # atomic: never a half-written trust file

    def is_trusted(self, workspace) -> bool:
        return canonical(workspace) in self._load()

    def list(self) -> list:
        return sorted(self._load())

    def set(self, workspace, trusted: bool = True) -> str:
        c = canonical(workspace)
        vals = self._load()
        vals.add(c) if trusted else vals.discard(c)
        self._save(vals)
        return c


def repo_allowed_commands(cwd, store: TrustStore = None) -> list:
    """Command prefixes this repo asks to have auto-run — [] unless the user trusts it.

    Reading the file is deliberately done ONLY after the trust check, so an untrusted
    repo's config is never even parsed. Cheap, and it keeps a malformed or hostile file
    from being a surface at all.

    Format (`.collie/allow.toml`, minimal on purpose — no toml dependency):

        allow = ["pytest", "npm test", "cargo build"]

    Each entry still has to survive gate._command_allowed, so it can only ever
    auto-run a command whose argv it is an exact prefix of, and never one carrying
    shell operators.
    """
    store = store or TrustStore()
    if not store.is_trusted(cwd):
        return []
    p = os.path.join(canonical(cwd), ".collie", "allow.toml")
    try:
        with open(p, "r", encoding="utf-8") as f:
            text = f.read(64_000)
    except OSError:
        return []
    return _parse_allow(text)


def _parse_allow(text: str) -> list:
    """Pull `allow = [...]` out of a tiny TOML file without taking a dependency.

    Only string entries on the `allow` key are honoured; anything else in the file is
    ignored rather than guessed at. A parse failure yields nothing, which fails closed.
    """
    out, collecting, buf = [], False, ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not collecting:
            if line.replace(" ", "").startswith("allow=["):
                collecting = True
                buf = line.split("[", 1)[1]
            else:
                continue
        else:
            buf += " " + line
        if "]" in buf:
            buf = buf.split("]", 1)[0]
            break
    if not collecting:
        return out
    for part in buf.split(","):
        part = part.strip()
        if len(part) >= 2 and part[0] in "\"'" and part[-1] == part[0]:
            val = part[1:-1].strip()
            if val:
                out.append(val)
    return out
