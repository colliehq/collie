"""plat.ask_allow_deny + remote's desktop pairing prompt.

Every way this fails is silent. Misreading osascript's reply turns "nobody was at the screen" into a
refusal, which would break pairing on any unattended machine; answering the wrong request would let a
stale click approve a device the person never saw; and blocking on the socket thread would stall the
run that is streaming over it. So all three are pinned here rather than trusted.

    python3 tests/test_pairprompt.py
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import plat                                            # noqa: E402
from harness import remote as remote_mod                            # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


class Reply(object):
    """Stands in for the osascript process."""

    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def with_osascript(stdout, returncode=0):
    """Run ask_allow_deny against a canned osascript reply, on macOS's branch."""
    real_run, real_mac = subprocess.run, plat.is_macos
    subprocess.run = lambda *a, **k: Reply(stdout, returncode)
    plat.is_macos = lambda: True
    try:
        return plat.ask_allow_deny("t", "m", timeout=1)
    finally:
        subprocess.run, plat.is_macos = real_run, real_mac


def main():
    # These are the exact strings osascript produces.
    check(with_osascript("button returned:Allow, gave up:false\n") is True,
          "Allow is read as allow")
    check(with_osascript("button returned:Not me, gave up:false\n") is False,
          "Not me is read as deny")
    # The one that matters: an unattended machine must come back undecided, so the pairing falls
    # through to the card on /remote instead of being refused outright.
    check(with_osascript("button returned:, gave up:true\n") is None,
          "a timeout is UNDECIDED, never a refusal")
    check(with_osascript("", 1) is None, "a cancelled or unavailable dialog is undecided too")
    check(with_osascript("something unexpected\n") is False,
          "an unrecognised reply is not treated as approval")

    # A device name arrives over the network; it must not be able to close the AppleScript string.
    hostile = 'evil" & (do shell script "touch /tmp/collie-pwned") & "'
    literal = plat._as_str(hostile)
    check(literal.startswith('"') and literal.endswith('"'), "the name is a quoted literal")
    check('\\"' in literal and 'do shell script' in literal,
          "its quotes are escaped rather than stripped, so the text survives but cannot break out")
    check(literal.count('"') - literal.count('\\"') == 2,
          "exactly two unescaped quotes — the opening and closing ones")

    # Asking must not block the socket thread, and must answer the request it was asked about.
    client = remote_mod.RelayClient.__new__(remote_mod.RelayClient)
    client.approved_devices = set()
    client.pending_pair = None
    replies = []
    client._reply_pair = lambda ws, rid, ok, error="": replies.append((rid, ok))
    client._log = lambda *a, **k: None

    gate = threading.Event()
    real_ask = plat.ask_allow_deny
    plat.ask_allow_deny = lambda *a, **k: (gate.wait(5), True)[1]

    pending = {"id": "req-1", "num": "1234", "device_id": "dev-1", "name": "iPhone", "ws": object()}
    client.pending_pair = pending
    t0 = time.time()
    client._ask_on_screen(pending)
    check(time.time() - t0 < 0.5, "asking returns at once — it never blocks the relay socket")
    gate.set()
    for _ in range(50):
        if replies:
            break
        time.sleep(0.1)
    check(replies == [("req-1", True)], "the answer goes back for that request")
    check("dev-1" in client.approved_devices, "and an approved device is remembered")
    check(client.pending_pair is None, "the card clears with it")

    # A dialog answered after the web card already handled a DIFFERENT request must do nothing.
    replies.clear()
    client.approved_devices.clear()
    gate2 = threading.Event()
    plat.ask_allow_deny = lambda *a, **k: (gate2.wait(5), True)[1]
    stale = {"id": "req-2", "num": "5555", "device_id": "dev-2", "name": "iPad", "ws": object()}
    client.pending_pair = stale
    client._ask_on_screen(stale)
    client.pending_pair = {"id": "req-3", "num": "9999", "device_id": "dev-3",
                           "name": "someone else", "ws": object()}
    gate2.set()
    time.sleep(0.6)
    check(replies == [], "a late click does not answer the request that replaced it")
    check("dev-2" not in client.approved_devices and "dev-3" not in client.approved_devices,
          "and approves nobody")

    plat.ask_allow_deny = real_ask

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "pair prompt: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
