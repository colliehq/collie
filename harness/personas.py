"""Personas — a role packaged as one file: what to be, what to reach, and how much to ask.

collie already has skills (things it can be told HOW to do). A persona is the other axis:
who it is for this session, and the tools and the permission mode that go with that.

The part worth having is `mode`. "Fix this bug in my repo" and "go and do this for me on
these websites" want genuinely different defaults — the first should not stop to ask about
`pytest`, the second should stop before it clicks anything. Today that is a flag people
have to know about; as a persona field it travels with the job description, which is where
someone actually forms the intent.

Format — the same lenient frontmatter as SKILL.md, then the prompt:

    ---
    name: ops
    description: investigate incidents and produce a written record
    mode: interactive
    tools: read_file, grep, bash, browser_read
    ---
    You are... (the identity text, appended to collie's own)

**A persona can only narrow.** `tools` is an allowlist filtered against what is already
registered, `mode` is clamped so a persona can pick a stricter one than the user's but
never a laxer one, and there is deliberately no field for risk overrides or workspace
trust. A file that could relax the gate would be a way to smuggle permission in as
configuration — and personas are exactly the kind of file people copy from the internet.
"""

from __future__ import annotations

import os

from .gate import Mode
from .skills import _parse_frontmatter

# Strict -> permissive. A persona may move DOWN this list, never up.
_ORDER = [Mode.PLAN, Mode.INTERACTIVE, Mode.PROJECT, Mode.AUTO]


class Persona:
    def __init__(self, name, description="", mode=None, tools=None, prompt="", path=""):
        self.name = name
        self.description = description
        self.mode = mode
        self.tools = tools or []
        self.prompt = prompt
        self.path = path

    def __repr__(self):
        return "Persona(%r, mode=%s, tools=%d)" % (self.name, self.mode, len(self.tools))

    # -- the two things a persona is allowed to do ---------------------------
    def effective_mode(self, user_mode: Mode) -> Mode:
        """The stricter of the persona's mode and the user's.

        Only ever narrowing. Otherwise a persona file — the kind of thing people copy from
        a gist — could hand itself `auto`, and the gate would be configuration rather than
        a decision.
        """
        if self.mode is None:
            return user_mode
        try:
            return _ORDER[min(_ORDER.index(self.mode), _ORDER.index(user_mode))]
        except ValueError:
            return user_mode

    def apply_tools(self, registry) -> int:
        """Restrict the registry to this persona's tools. Returns how many were removed.

        Filtered against what is REGISTERED: a persona naming a tool that does not exist
        gets nothing new, and one naming none at all leaves the registry alone rather than
        silently disarming collie.
        """
        if not self.tools:
            return 0
        keep = set(self.tools)
        drop = [n for n in registry.names() if n not in keep]
        for n in drop:
            registry._tools.pop(n, None)
            registry._activated.discard(n)
        return len(drop)


def persona_dirs(cwd: str) -> list:
    return [os.path.join(cwd, ".collie", "personas"),
            os.path.expanduser("~/.collie/personas")]


def parse(text: str, path: str = "") -> Persona:
    fm = _parse_frontmatter(text)
    body = text.split("---", 2)[2] if text.startswith("---") and text.count("---") >= 2 else text
    mode = None
    raw = (fm.get("mode") or "").strip().lower()
    if raw:
        try:
            mode = Mode(raw)
        except ValueError:
            mode = None                 # an unknown mode is ignored, never guessed at
    tools = [t.strip() for t in (fm.get("tools") or "").replace(",", " ").split() if t.strip()]
    return Persona(name=(fm.get("name") or os.path.basename(path).split(".")[0] or "").strip(),
                   description=(fm.get("description") or "").strip(),
                   mode=mode, tools=tools, prompt=body.strip(), path=path)


def discover(cwd: str) -> list:
    """Personas from the project then the user's own; first wins on a duplicate name."""
    seen, out = set(), []
    for base in persona_dirs(cwd):
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(base, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    persona = parse(f.read(200_000), p)
            except (OSError, UnicodeDecodeError):
                continue
            if persona.name and persona.name not in seen:
                seen.add(persona.name)
                out.append(persona)
    return out


def load(name: str, cwd: str):
    for p in discover(cwd):
        if p.name == name:
            return p
    return None
