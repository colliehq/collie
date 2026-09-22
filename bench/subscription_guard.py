"""Backward-compatible import surface for the packaged subscription guard.

The production implementation lives in :mod:`harness.subscription_guard` so it
is included in Collie's wheel.  Keep this wrapper for benchmark scripts and
downstream callers that imported the original module path.
"""
from harness import subscription_guard as _implementation
from harness.subscription_guard import (
    CODEX_EVIDENCE_MAX_AGE_SECONDS,
    RECEIPT_FORMAT,
    SCHEMA_VERSION,
    SubscriptionGuardError,
    check_subscription_guard,
)

# Historical tests and callers may patch these module objects through the old
# path.  They are the same objects used by the packaged implementation, so that
# behavior remains compatible without duplicating any guard logic.
shutil = _implementation.shutil
subprocess = _implementation.subprocess

__all__ = list(_implementation.__all__)
