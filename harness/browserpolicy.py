"""Persistent browser *site access* policy.

Site access answers only "may Collie navigate to this origin without another
prompt?"  It deliberately does not authorize clicks, typing, uploads, sends,
payments, deletes, or any other action once the page is open.  Those remain in
the normal per-action gate.
"""
from __future__ import annotations

import fnmatch
import re
import urllib.parse


# A narrow built-in safety net.  This is intentionally an ask-list, not a
# block-list: false positives cost one confirmation, while false negatives are
# still protected by the action-time gate.  Users can extend it in Settings.
_SENSITIVE_SUFFIXES = (
    "paypal.com", "venmo.com", "cash.app", "wise.com", "revolut.com",
    "stripe.com", "coinbase.com", "binance.com", "kraken.com",
    "gemini.com", "robinhood.com", "webull.com", "fidelity.com",
    "schwab.com", "etrade.com", "interactivebrokers.com",
    "bankofamerica.com", "chase.com", "wellsfargo.com", "citi.com",
    "capitalone.com", "americanexpress.com", "discover.com",
)
_SENSITIVE_LABELS = re.compile(
    r"(?:^|[-.])(bank(?:ing)?|creditunion|broker(?:age)?|crypto|wallet|payments?)(?:[-.]|$)",
    re.IGNORECASE,
)


def hostname(value: str) -> str:
    """Normalized hostname from an origin/URL, or an empty string."""
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        return (parsed.hostname or "").strip(".").lower()
    except (TypeError, ValueError):
        return ""


def _extra_patterns(value) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.split(",")
    return tuple(str(item or "").strip().lower() for item in (value or ())
                 if str(item or "").strip())


def is_sensitive_site(value: str, extra=()) -> bool:
    """Whether an origin should keep the navigation confirmation."""
    host = hostname(value)
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        return False
    if host.endswith(".bank") or _SENSITIVE_LABELS.search(host):
        return True
    if any(host == suffix or host.endswith("." + suffix)
           for suffix in _SENSITIVE_SUFFIXES):
        return True
    for pattern in _extra_patterns(extra):
        # A plain domain includes its subdomains; wildcard entries use normal
        # shell-style matching (for example *.finance.example).
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatchcase(host, pattern):
                return True
        elif host == pattern or host.endswith("." + pattern):
            return True
    return False


def navigation_allowed_without_prompt(policy: str, target: str, extra=()) -> bool:
    """Apply the persistent navigation policy; action authorization is separate."""
    policy = str(policy or "ask_every_site").strip().lower()
    if policy == "all_sites":
        return bool(hostname(target))
    if policy == "all_except_sensitive":
        return bool(hostname(target)) and not is_sensitive_site(target, extra)
    return False
