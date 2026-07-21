"""Leash — the per-job grant of authority the executor enforces (plan §5.1).

A leash is a small JSON on a Job. Deterministic host code evaluates a proposed
action against it and returns allow / ask / deny. This is the authority half of
the delegate; the verifier is the evidence half.

Three tiers, not five (the plan's simplification): see (free) / do-reversible
(free within the leash) / irreversible (send, publish, pay, delete — needs a
confirm token unless the leash pre-authorizes with bounds).

Backward-compat rule: a job with NO leash, or a leash without a `may` key, is
UNENFORCED (allow) — leash metadata is optional, and enforcement kicks in only
once a real allowlist is declared. A declared-but-empty `may` denies everything
(explicit lockdown). Production jobs should always carry a `may`.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

ALLOW = "allow"
ASK = "ask"      # permitted, but requires a confirm token (irreversible)
DENY = "deny"

# capability risk tiers that require confirm/pre-auth
_IRREVERSIBLE = {"irreversible", "send", "publish", "pay", "delete"}


@dataclass
class Decision:
    decision: str
    reason: str

    @property
    def denied(self) -> bool:
        return self.decision == DENY


def evaluate(leash: dict, capability: str, cap_risk: str = "irreversible",
             spend_usd: float = 0.0, now_iso: str = None) -> Decision:
    """Evaluate one proposed action against a job's leash dict.

    - unenforced (no leash / no `may`) -> ALLOW
    - expired                          -> DENY
    - capability not matched by `may`  -> DENY  (glob match, e.g. "listing.*")
    - spend over spend_max_usd         -> DENY
    - irreversible risk                -> DENY / ALLOW / ASK per leash.irreversible
    - otherwise                        -> ALLOW
    """
    if not leash or "may" not in leash:
        return Decision(ALLOW, "no leash configured (unenforced)")

    expires = leash.get("expires") or ""
    if expires and now_iso and now_iso > expires:
        return Decision(DENY, f"leash expired at {expires}")

    patterns = leash.get("may") or []
    if not any(fnmatch.fnmatchcase(capability, p) for p in patterns):
        return Decision(DENY, f"{capability!r} not permitted by leash.may")

    cap = leash.get("spend_max_usd")
    if cap is not None and spend_usd > float(cap):
        return Decision(DENY, f"spend ${spend_usd} exceeds leash cap ${cap}")

    if cap_risk in _IRREVERSIBLE:
        mode = leash.get("irreversible", "confirm")
        if mode == "deny":
            return Decision(DENY, "irreversible actions denied by leash")
        if mode == "allow":
            return Decision(ALLOW, "irreversible pre-authorized by leash")
        return Decision(ASK, "irreversible action requires a confirm token")

    return Decision(ALLOW, "within leash")
