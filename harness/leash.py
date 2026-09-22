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
import math
from dataclasses import dataclass

ALLOW = "allow"
ASK = "ask"      # permitted, but requires a confirm token (irreversible)
DENY = "deny"

# capability risk tiers that require confirm/pre-auth
_IRREVERSIBLE = {"irreversible", "send", "publish", "pay", "delete"}
_IRREVERSIBLE_MODES = {"allow", "confirm", "deny"}


@dataclass
class Decision:
    decision: str
    reason: str

    @property
    def denied(self) -> bool:
        return self.decision == DENY


def validate(leash: dict | None) -> dict:
    """Validate the fields that grant Job action authority.

    Unknown domain-specific bounds are retained, but the generic authority
    fields must have exact JSON types.  In particular, JSON booleans are not
    numbers and a non-finite spend cap can never mean "unlimited".
    """
    if leash is None:
        return {}
    if not isinstance(leash, dict):
        raise ValueError("leash must be a JSON object")
    if "may" in leash:
        may = leash["may"]
        if not isinstance(may, list) or any(
                not isinstance(item, str) or not item for item in may):
            raise ValueError("leash.may must be a list of non-empty strings")
    if "irreversible" in leash:
        mode = leash["irreversible"]
        if not isinstance(mode, str) or mode not in _IRREVERSIBLE_MODES:
            raise ValueError("leash.irreversible must be allow, confirm, or deny")
    if "spend_max_usd" in leash and leash["spend_max_usd"] is not None:
        cap = leash["spend_max_usd"]
        if (isinstance(cap, bool) or not isinstance(cap, (int, float)) or
                not math.isfinite(cap) or cap < 0):
            raise ValueError("leash.spend_max_usd must be a finite non-negative number")
    if "expires" in leash and not isinstance(leash["expires"], str):
        raise ValueError("leash.expires must be a string")
    return dict(leash)


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
    try:
        leash = validate(leash)
    except (TypeError, ValueError) as exc:
        return Decision(DENY, "invalid leash authority: %s" % exc)
    if (isinstance(spend_usd, bool) or not isinstance(spend_usd, (int, float)) or
            not math.isfinite(spend_usd) or spend_usd < 0):
        return Decision(DENY, "invalid requested spend")
    if not isinstance(capability, str) or not capability:
        return Decision(DENY, "invalid capability")
    if not leash or "may" not in leash:
        return Decision(ALLOW, "no leash configured (unenforced)")

    expires = leash.get("expires") or ""
    if expires and now_iso and now_iso > expires:
        return Decision(DENY, f"leash expired at {expires}")

    patterns = leash.get("may") or []
    if not any(fnmatch.fnmatchcase(capability, p) for p in patterns):
        return Decision(DENY, f"{capability!r} not permitted by leash.may")

    cap = leash.get("spend_max_usd")
    if cap is not None and spend_usd > cap:
        return Decision(DENY, f"spend ${spend_usd} exceeds leash cap ${cap}")

    if cap_risk in _IRREVERSIBLE:
        mode = leash.get("irreversible", "confirm")
        if mode == "deny":
            return Decision(DENY, "irreversible actions denied by leash")
        if mode == "allow":
            return Decision(ALLOW, "irreversible pre-authorized by leash")
        return Decision(ASK, "irreversible action requires a confirm token")

    return Decision(ALLOW, "within leash")
