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

    # A designed email exactly as the client puts it on the wire: the stored outbox form of
    # the page and the dog, through the client's own request builder.
    from harness import mail_messages
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
    page = '<p>Good morning — 早上好!</p><img alt="" src="cid:collie-avatar">'
    stored = mail_messages.encode_inline([{"cid": "collie-avatar", "filename": "collie.png",
                                           "content_type": "image/png", "data": png}], page)
    send_body = dm._send_payload({"id": "report-2026-09-29-abc", "destination": "owner@example.test",
                                  "subject": "Two quick ones · Tue 29 Sep",
                                  "text": "Good morning — 早上好!\n", "html": page,
                                  "inline": stored}, design=True)

    tmp = tempfile.mkdtemp(prefix="collie_wire_")
    fx, out = os.path.join(tmp, "fx.json"), os.path.join(tmp, "out.json")
    with open(fx, "w", encoding="utf-8") as f:
        json.dump({"relay_priv": dm.b64(relay_priv), "dog_pub": dm.b64(dog_pub),
                   "handle_pub": dm.b64(handle_pub), "address": address, "ts": ts,
                   "nonce": nonce, "method": method, "path": path, "plaintext": plaintext,
                   "send_body": send_body, "owner": "owner@example.test"}, f,
                  ensure_ascii=False)

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

    # 4. a designed email: the client's body, the Worker's validation and raw message,
    #    read back by Python's own mail parser
    import email as email_pkg
    import email.policy
    check(got.get("send_error") is None,
          "the Worker accepts the HTML body the client builds (%s)" % got.get("send_error"))
    raw = got.get("legacy_raw") or ""
    message = email_pkg.message_from_string(raw, policy=email.policy.default)
    check(message.get_content_type() == "multipart/alternative" and not message.defects,
          "the legacy raw message parses as one clean multipart/alternative")
    parts = message.get_payload() if message.is_multipart() else []
    plain = parts[0] if parts else None
    related = parts[1] if len(parts) > 1 else None
    check(plain is not None and plain.get_content_type() == "text/plain"
          and plain.get_content() == send_body["text"],
          "its first alternative is the plain text, byte for byte")
    inner = related.get_payload() if related is not None and related.is_multipart() else []
    check(related is not None and related.get_content_type() == "multipart/related"
          and len(inner) == 2 and inner[0].get_content_type() == "text/html"
          and inner[0].get_content() == page,
          "its second is multipart/related holding the page, unchanged")
    check(len(inner) == 2 and inner[1]["Content-ID"] == "<collie-avatar>"
          and inner[1].get_payload(decode=True) == png
          and inner[1].get_content_disposition() == "inline",
          "and the dog, inline under the content id the page names, bit for bit")
    check(message["To"] == "owner@example.test" and message["Cc"] is None
          and message["Bcc"] is None and message["Auto-Submitted"] == "auto-replied",
          "to the owner and nobody else, marked as an automatic message")
    built = got.get("structured") or {}
    attachment = (built.get("attachments") or [{}])[0]
    check(built.get("html") == page and built.get("text") == send_body["text"]
          and attachment.get("contentId") == "collie-avatar"
          and attachment.get("disposition") == "inline"
          and dm.ub64(attachment.get("content") or "") == png,
          "and the structured builder gets the same page and the same image")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "dog mail wire: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
