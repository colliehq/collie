"""Short-lived isolation for several agents working the same task at once.

pack runs N attempts at one task and picks the winner by what PASSES. That only means anything if
the N are independent samples. They were not: every attempt ran under the same `project`, the loop
consolidates its answer by default, and the composer auto-prefetches from memory — so attempt 2's
prompt arrived carrying attempt 1's conclusion, and best-of-N quietly degraded into serial
refinement. Measured, not theorised: attempt 1's system prompt contained
`RELEVANT MEMORY (auto-recalled): - Task 'pack0' -> <attempt 0's answer>`.

The fix is deliberately NOT "give each agent its own memory". Shared long-term memory is the
point — every member should start from the same accumulated knowledge about this repo, or the
members differ for a reason that has nothing to do with the model behind them. What must not be
shared is what a member writes WHILE it is running.

So reads fall through to the real store and writes land in an in-process overlay that dies with
the agent. There is nothing to clean up afterwards, nothing to forget, and no member can read
another's notes. It also makes the members safe to run concurrently, which is the point of giving
each one a different model.

A consequence worth stating: a losing attempt's answer never becomes a durable fact. That is the
intended reading — a candidate that was not selected is not something Collie learned.
"""
from __future__ import annotations

from .memory import SqliteMemory

# One fixed bucket inside the overlay. The caller's project name is deliberately ignored on the
# way in (see ScratchMemory.recall) so the agent can be given a unique project — which is what
# isolates its checkpoint/undo stack — without that name also cutting it off from shared memory.
_SCRATCH = "scratch"


class ScratchMemory:
    """Reads the shared store, writes to a throwaway one. Same surface as SqliteMemory.

    ``read_project`` is the project every read is scoped to, regardless of the project the caller
    passes. That indirection is the whole trick: ``ctx.project`` can be a per-agent name for
    checkpoint isolation while recall still sees the team's common baseline.
    """

    def __init__(self, base: SqliteMemory, read_project: str):
        self.base = base
        self.read_project = read_project
        # BM25-only on purpose: this holds a handful of notes from one run, and sharing the real
        # embedder across concurrently running agents would put several threads on one ONNX session.
        self._own = SqliteMemory(":memory:")

    def __getattr__(self, name):
        # Anything not overridden below is a read of the real store (embed_model, rebuild_fts, …).
        return getattr(self.base, name)

    def recall(self, query: str, project: str = "global", k: int = 8, pool: int = 50,
               statuses=None, *, allowed_scopes=None) -> list:
        logical_project = str(project or "global")
        if allowed_scopes is None:
            # ``scratch`` is the legacy scope used by direct overlay writes;
            # newer writes retain the caller's logical scope.  The shared
            # store is intentionally mapped to ``read_project``.
            own_scopes = tuple(dict.fromkeys(
                (logical_project, _SCRATCH, "global")))
            shared_scopes = tuple(dict.fromkeys(
                (str(self.read_project), "global")))
        else:
            own_scopes = SqliteMemory._allowed_scopes(logical_project, allowed_scopes)
            # Explicit logical authority follows the adapter's project mapping
            # without exposing the physical shared project to Pack callers.
            shared_scopes = tuple(dict.fromkeys(
                str(self.read_project) if scope == logical_project else scope
                for scope in own_scopes))
        mine = self._own.recall(
            query, project=_SCRATCH, k=k, pool=pool, statuses=statuses,
            allowed_scopes=own_scopes)
        shared = self.base.recall(
            query, project=self.read_project, k=k, pool=pool, statuses=statuses,
            allowed_scopes=shared_scopes)
        seen = {hit.get("text") for hit in mine}
        return (mine + [h for h in shared if h.get("text") not in seen])[:k]

    def remember(self, text: str, keys: str = "", project: str = "global", **kw):
        kw.setdefault("scope", project)
        return self._own.remember(text, keys=keys, project=_SCRATCH, **kw)

    @staticmethod
    def claim_boundary(project: str) -> dict[str, str]:
        """Map a caller's logical claim boundary to this overlay's storage row."""
        return {"project": _SCRATCH, "scope": str(project or "global")}

    # Lifecycle methods must be explicit.  Letting __getattr__ forward them to
    # ``base`` would turn a Pack candidate's proposal (or promotion of its local
    # integer id) into a write against shared durable memory — exactly the
    # cross-candidate contamination this adapter exists to prevent.
    def propose(self, text: str, keys: str = "", project: str = "global", **kw):
        kw.setdefault("scope", project)
        return self._own.propose(text, keys=keys, project=_SCRATCH, **kw)

    def promote(self, memory_id: int, status: str = "active", **kw) -> bool:
        return self._own.promote(memory_id, status=status, **kw)

    def reject(self, memory_id: int, **kw) -> bool:
        return self._own.reject(memory_id, **kw)

    def invalidate(self, memory_id: int, **kw) -> bool:
        return self._own.invalidate(memory_id, **kw)

    promote_memory = promote
    reject_memory = reject
    invalidate_memory = invalidate

    def get_claim(self, memory_id: int):
        return self._own.get_claim(memory_id)

    def list_claims(self, status: str | None = None, project: str | None = None,
                    limit: int = 100, *, allowed_scopes=None) -> list[dict]:
        if project is None and allowed_scopes is None:
            # The overlay contains no shared rows, so this is the same local
            # admin listing the underlying memory API has always exposed.
            return self._own.list_claims(status=status, limit=limit)
        if allowed_scopes is None:
            logical_project = str(project or "global")
            allowed_scopes = tuple(dict.fromkeys(
                (logical_project, _SCRATCH, "global")))
        return self._own.list_claims(
            status=status, project=_SCRATCH, limit=limit,
            allowed_scopes=allowed_scopes)

    def core_blocks(self, scopes: list) -> list:
        # The composer builds scopes as [f"project:{project}", "global"]; rewrite the project scope
        # to the shared one for the same reason recall ignores the caller's project.
        shared_scopes = ["project:%s" % self.read_project if str(s).startswith("project:") else s
                         for s in scopes]
        return list(self.base.core_blocks(shared_scopes)) + list(self._own.core_blocks(scopes))

    def set_block(self, scope: str, label: str, value: str, char_limit: int = 1500) -> None:
        self._own.set_block(scope, label, value, char_limit)

    def count(self, project: str | None = None) -> int:
        return self.base.count(self.read_project) + self._own.count()

    def close(self) -> None:
        # Closes the wrapped store too: in the one wiring that builds this (isolate_harness), the
        # harness owns the base and its caller releases everything through `h.memory.close()`.
        try:
            self._own.close()
        finally:
            self.base.close()


def isolate_harness(harness, read_project: str) -> None:
    """Give one already-built harness an ephemeral write layer, in place.

    Call between ``make_harness`` and ``run``. Build the harness with a per-agent ``project`` (that
    is what separates the undo stacks, which are keyed by project and also live in a process-global
    dict); this then reconnects its reads to the shared ``read_project``.
    """
    scratch = ScratchMemory(harness.memory, read_project)
    harness.memory = scratch
    # The composer captured the original at construction; the tool ctx is rebuilt from
    # harness.memory on every run, so it needs no fixing.
    if getattr(harness, "composer", None) is not None:
        harness.composer.memory = scratch
