"""Run the mail relay's Node regressions from pytest, so CI actually sees them.

`tests/mail_relay_test.mjs` executes the real `relay/mail_worker.js` — its fetch handler, its email
handler and its MailDelivery Durable Object — against in-memory KV/DO/send bindings with genuine
X25519 stamps. That suite is where "exactly one outbound send per request id" and "an ambiguous
provider failure is never retried" are actually proven.

A .mjs file is invisible to pytest, and the project's gate runs the complete collected pytest suite
rather than a hand-maintained list. So this wrapper exists purely to make the Node checks part of
that collection: a Node failure becomes a pytest failure, with Node's own output attached, on all
three OSes CI runs.

Skipped — not silently passed — when Node is missing or too old. A green tick for a suite that was
never executed is worse than a visible skip.

    python tests/test_mail_relay.py
"""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE = os.path.join(ROOT, "tests", "mail_relay_test.mjs")
MIN_NODE = 20          # the Worker uses top-level await and WebCrypto X25519


def _node():
    """The node binary, if there is one new enough to load the Worker module."""
    exe = shutil.which("node")
    if not exe:
        return None, "node is not installed"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        major = int(out.stdout.strip().lstrip("v").split(".")[0])
    except Exception as exc:                                  # noqa: BLE001 - reported, not raised
        return None, "could not read the node version: %s" % exc
    if major < MIN_NODE:
        return None, "node %d is older than the required %d" % (major, MIN_NODE)
    return exe, ""


def test_mail_relay_worker():
    """The Worker's send ledger, bounded intake and paginated reads, run for real under Node."""
    exe, why = _node()
    if not exe:
        pytest.skip(why)
    run = subprocess.run([exe, SUITE], capture_output=True, text=True, cwd=ROOT, timeout=600)
    output = (run.stdout or "") + (run.stderr or "")
    assert run.returncode == 0, "mail relay Node suite failed:\n" + output[-8000:]
    # A suite that exits 0 without running anything would otherwise read as a pass.
    assert "mail relay: all green" in output, "the Node suite did not report a result:\n" + output[-4000:]


def main():
    exe, why = _node()
    if not exe:
        print("  (%s — skipping the mail relay Node suite)" % why)
        return 0
    return subprocess.run([exe, SUITE], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
