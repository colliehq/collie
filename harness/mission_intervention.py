"""Quietly abandon optional routes without turning a refusal into authority."""
from __future__ import annotations

import hashlib
import json


def repeated_without_progress(events: list[dict], kind: str, name: str, identity: str) -> bool:
    """A/B/A deferrals are still a repeat if no work happened between them."""
    for event in reversed(events):
        pair = (event.get("kind"), event.get("name"))
        if event.get("kind") == "result" or pair in {
                ("coverage", "updated"), ("followup", "checked"),
                ("authorization", "standing_resolved"), ("authorization", "resolved_reused"),
                ("intervention", "optional_skipped")}:
            return False
        if pair == (kind, name) and event.get("nonce") == identity:
            return True
    return False


def optional_skip(args: dict, reason: str, coverage: list[dict]) -> dict | None:
    """Require an explicit optional classification; non-blocking is not optional.

    A declared coverage contract is stronger than a planner's label. Only an
    exact, non-required branch can be skipped when that contract is present.
    This returns a record, never an authorization or an execution instruction.
    """
    if args.get("optional") is not True or args.get("blocking"):
        return None
    summary = str(args.get("summary") or "").strip()[:1000]
    explanation = str(reason or args.get("reason") or "").strip()[:1000]
    branch = str(args.get("campaign_branch") or args.get("branch") or "").strip()[:180]
    if not summary or not explanation:
        return None
    if coverage:
        matches = [row for row in coverage
                   if str(row.get("branch") or "").casefold() == branch.casefold()]
        if len(matches) != 1 or matches[0].get("required", True):
            return None
    identity = {"branch": branch, "summary": summary,
                "domain": str(args.get("domain") or "")[:253],
                "kind": str(args.get("kind") or "optional")[:80]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    return {"id": "skip_" + digest, **identity, "reason": explanation}
