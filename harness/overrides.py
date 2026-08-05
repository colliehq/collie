"""User-local risk overrides — the only way DOWN the risk ladder.

Mostly this exists for MCP. Every MCP tool is `external` by default (risk.py), because a
server's `create_page` and `delete_database` are indistinguishable from their names, and
a wrong guess there is somebody's data. That default is right and it is also annoying: a
user who has read a server and trusts its read-only tools should be able to say so once
instead of approving `mcp__fs__read_file` forever.

Rules match tool names by glob and the most specific wins, so `mcp__fs__*` can be relaxed
while `mcp__fs__write_file` stays external.

**INVIOLABLE: nothing but the user writes this file.** Not a skill, not an MCP server's
own metadata, not a persona, not the model, not a repo config. A package may declare what
tools it would like; only the person decides how far to believe it. If this store ever
becomes writable by something collie loaded, every other guarantee in the gate is
decorative — the thing being gated could simply reclassify itself as harmless.

That is why there is no tool for this, and why the CLI is the only writer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .risk import RiskClass


def _state_dir() -> str:
    d = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    os.makedirs(d, exist_ok=True)
    return d


def _as_risk(value):
    """A RiskClass from either a RiskClass or its string name, or None.

    `RiskClass(str(value))` is the obvious spelling and it is wrong: RiskClass subclasses
    str+Enum, and on current Pythons `str(RiskClass.READ)` is "RiskClass.READ", not "read"
    — so passing the enum in raised while passing the string worked.
    """
    if isinstance(value, RiskClass):
        return value
    try:
        return RiskClass(str(value))
    except ValueError:
        return None


@dataclass
class Rule:
    pattern: str
    risk: RiskClass


def _specificity(pattern: str) -> int:
    """More literal characters = more specific; an exact name beats any glob."""
    literal = sum(1 for c in pattern if c not in "*?[]")
    return literal + (0 if any(c in pattern for c in "*?[") else 1000)


class RiskOverrideStore:
    def __init__(self, path: str = None):
        self.path = path or os.path.join(_state_dir(), "risk_overrides.json")

    def _load(self) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        raw = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        out = []
        for r in raw:
            try:
                risk = _as_risk(r["risk"])
            except (KeyError, TypeError):
                continue
            if risk is None:
                continue      # skip a malformed rule rather than fail the whole store —
                              # one bad line must not silently drop every other override
            out.append(Rule(str(r["pattern"]), risk))
        return out

    def _save(self, rules: list) -> None:
        from . import plat
        tmp = self.path + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rules": [{"pattern": r.pattern, "risk": r.risk.value}
                                 for r in rules]}, f, indent=2)
            f.write("\n")
        try:
            plat.chmod_private(tmp)
        except Exception:
            pass
        os.replace(tmp, self.path)

    def list(self) -> list:
        return sorted(self._load(), key=lambda r: (-_specificity(r.pattern), r.pattern))

    def set(self, pattern: str, risk) -> None:
        risk = _as_risk(risk)
        if risk is None:
            raise ValueError("unknown risk class: %r" % (risk,))
        rules = [r for r in self._load() if r.pattern != pattern]
        rules.append(Rule(pattern, risk))
        self._save(rules)

    def unset(self, pattern: str) -> bool:
        rules = self._load()
        kept = [r for r in rules if r.pattern != pattern]
        if len(kept) == len(rules):
            return False
        self._save(kept)
        return True

    def lookup(self, tool_name: str):
        """The most specific matching rule's class, or None to defer to the table."""
        best, best_score = None, -1
        for r in self._load():
            if fnmatchcase(tool_name, r.pattern):
                score = _specificity(r.pattern)
                if score > best_score:
                    best, best_score = r.risk, score
        return best

    def resolver(self):
        """A `RiskOverrides` callable for the gate. Reads on every call so a rule added
        with `collie risk --set` applies to a run already in flight, and — more to the
        point — so REVOKING one takes effect immediately rather than at next launch."""
        return self.lookup
