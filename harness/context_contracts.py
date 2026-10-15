"""Lightweight context-composition contract; no optional prompt sources."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol
from .model_contracts import est_tokens

@dataclass
class ComposeMeta:
    prefix_tokens: int = 0
    section_tokens: dict = field(default_factory=dict)
    prefetched: int = 0
    prefetched_ids: list = field(default_factory=list)
    elide_from: int = 0      # message index below which old tool outputs were stubbed this build;
                             # the loop compares it turn-to-turn to attribute cache misses to 'elide'
                             # (composer stays stateless — it only reports, never remembers)
    # What the compaction projection did this build: {"active", "cutoff", "compacted", "kept",
    # "generation"}. Content-free — the summary text itself never rides in meta.
    compaction: dict = field(default_factory=dict)
    # The pre-elision message list `elide_from` indexes into. It is the PROJECTION, not
    # session["messages"], once compaction is active; the loop's cache-miss attribution needs
    # the same list the indices came from or it reads the wrong window.
    pre_elision: list = field(default_factory=list)

class TokenBudgeter:
    def __init__(self, prefix_ceiling: int = 6000):
        self.prefix_ceiling = prefix_ceiling

    def report(self, sections: dict) -> dict:
        return {k: est_tokens(v) for k, v in sections.items()}


class ComposerPort(Protocol):
    """Context construction supplied by the application, not selected by the loop."""

    budgeter: TokenBudgeter

    def build(self, session: dict, user_msg, cwd: str, project: str,
              mode: str = "act") -> tuple[str, list, ComposeMeta]: ...
