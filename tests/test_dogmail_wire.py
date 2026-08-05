"""Do the Python client and the JavaScript Worker actually agree on the bytes?

Everything else about dog mail is tested against a fake relay written in Python — which proves the
client agrees with ITSELF. This is the check that the real other half, `relay/mail_worker.js`,
derives the same keys, frames the same MAC input, and produces an envelope this side can open.
A protocol implemented twice and verified once is a protocol with a fork in it.

Skipped (not failed) when node is missing: the JS half cannot be run, and pretending otherwise
would be the kind of green that means nothing.

    python3 tests/test_dogmail_wire.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def main():
    from harness import dogmail as dm
    from harness import e2e
    if not e2e.available():
        print("  (cryptography not installed — skipping)")
        return 0
    node = shutil.which("node")
    if not node:
        print("  (node not found — skipping the cross-implementation check)")
        return 0

    relay_priv, relay_pub = e2e.keypair()
    dog_priv, dog_pub = e2e.keypair()
    handle_priv, handle_pub = e2e.keypair()
    address = "rowan.daming@collie.run"
    ts, nonce, method, path = "1770000000", dm.b64(b"0123456789ab"), "GET", "/mail?since=0"
    plaintext = '{"subject":"Verify your email","from":"noreply@stripe.com"}'

    tmp = tempfile.mkdtemp(prefix="collie_wire_")
    fx, out = os.path.join(tmp, "fx.json"), os.path.join(tmp, "out.json")
    with open(fx, "w", encoding="utf-8") as f:
        json.dump({"relay_priv": dm.b64(relay_priv), "dog_pub": dm.b64(dog_pub),
                   "handle_pub": dm.b64(handle_pub), "address": address, "ts": ts,
                   "nonce": nonce, "method": method, "path": path, "plaintext": plaintext}, f)

    r = subprocess.run([node, os.path.join(ROOT, "tests", "mail_crossimpl_test.js"), fx, out],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print("  node failed:\n" + (r.stderr or r.stdout)[-1500:])
        return 1
    got = json.load(open(out, encoding="utf-8"))

    # 1. the stamp: the client makes it, the Worker must expect exactly it
    mine = dm._mac(dm.auth_key(dog_priv, relay_pub, address),
                   e2e.lp(method) + e2e.lp(path) + e2e.lp(ts) + e2e.lp(nonce))
    check(dm.b64(mine) == got["mac"],
          "the request stamp is byte-identical on both sides (key, framing, HMAC)")

    # 2. the handle's authority over an address
    mine_cert = dm.cert_tag(handle_priv, relay_pub, address, dog_pub)
    check(dm.b64(mine_cert) == got["cert"], "and so is the handle's claim tag")

    # 3. an envelope sealed by the Worker's code, opened by the client's
    opened = dm.open_from_relay(dog_priv, got["sealed"])
    check(opened.decode("utf-8") == plaintext,
          "a message sealed by the Worker opens on this machine, unchanged")

    other_priv, _ = e2e.keypair()
    try:
        dm.open_from_relay(other_priv, got["sealed"])
        leaked = True
    except Exception:
        leaked = False
    check(not leaked, "and stays shut for any other key")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "dog mail wire: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
