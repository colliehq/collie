"""A dog's own address (harness/dogmail.py), against a relay that behaves like the real one.

The claims worth pinning are the security ones, so they are tested the way an attacker would try
them: seal a message the way the Worker does and open it here; check that a different dog's key
cannot; check that the request stamp cannot be replayed or lifted onto another path; check that a
claim under someone else's handle is refused.

    python3 tests/test_dogmail.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

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

    tmp = tempfile.mkdtemp(prefix="collie_mail_")
    dm.STORE = os.path.join(tmp, "mail.json")
    dm.DOMAIN = "collie.run"

    relay_priv, relay_pub = e2e.keypair()
    state = {"handles": {}, "dogs": {}, "mail": [], "seen": set(), "codes": {}}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("content-length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/pubkey":
                return self._json({"pub": dm.b64(relay_pub)})
            if self.path.startswith("/mail"):
                addr = self.headers.get("x-collie-addr", "")
                dog = state["dogs"].get(addr)
                if not dog:
                    return self._json({"ok": False, "error": "unknown address"}, 404)
                ts, nonce = self.headers.get("x-collie-ts", ""), self.headers.get("x-collie-nonce", "")
                if abs(int(time.time()) - int(ts or 0)) > dm.SKEW:
                    return self._json({"ok": False, "error": "stale"}, 401)
                if nonce in state["seen"]:
                    return self._json({"ok": False, "error": "replay"}, 401)
                want = dm._mac(dm.auth_key(relay_priv, dm.ub64(dog["pub"]), addr),
                               e2e.lp("GET") + e2e.lp(self.path) + e2e.lp(ts) + e2e.lp(nonce))
                import hmac as _h
                if not _h.compare_digest(dm.b64(want), self.headers.get("x-collie-mac", "")):
                    return self._json({"ok": False, "error": "bad mac"}, 401)
                state["seen"].add(nonce)
                return self._json({"ok": True, "messages": [m for m in state["mail"]
                                                            if m["to"] == addr]})
            return self._json({"ok": False}, 404)

        def do_POST(self):
            d = self._body()
            if self.path == "/handle/claim":
                state["codes"][d["handle"]] = "123456"
                state["handles"][d["handle"]] = {"pub": d["pub"], "verified": False}
                return self._json({"ok": True, "sent": True})
            if self.path == "/handle/verify":
                h = state["handles"].get(d.get("handle"))
                if not h or d.get("code") != state["codes"].get(d.get("handle")):
                    return self._json({"ok": False, "error": "bad code"}, 401)
                h["verified"] = True
                return self._json({"ok": True})
            if self.path == "/dog/claim":
                h = state["handles"].get(d.get("handle") or "")
                if not h or not h["verified"]:
                    return self._json({"ok": False, "error": "handle not verified"}, 403)
                want = dm.cert_tag(relay_priv, dm.ub64(h["pub"]), d["address"], dm.ub64(d["pub"]))
                import hmac as _h
                if not _h.compare_digest(dm.b64(want), d.get("cert", "")):
                    return self._json({"ok": False, "error": "cert does not match this handle"}, 403)
                if d["address"] in state["dogs"]:
                    return self._json({"ok": False, "error": "taken"}, 409)
                state["dogs"][d["address"]] = {"pub": d["pub"]}
                return self._json({"ok": True})
            return self._json({"ok": False}, 404)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    relay = "http://127.0.0.1:%d" % srv.server_address[1]

    try:
        # ---- the envelope: only the addressed dog can open it ------------------------------
        dog_priv, dog_pub = e2e.keypair()
        env = dm.seal_to_dog(dog_pub, b'{"subject":"verify your email","from":"noreply@x.com"}')
        check(dm.open_from_relay(dog_priv, env).startswith(b'{"subject"'),
              "the dog opens what the relay sealed to it")
        other_priv, _ = e2e.keypair()
        try:
            dm.open_from_relay(other_priv, env)
            opened = True
        except Exception:
            opened = False
        check(not opened, "another dog's key cannot open it")
        check("epk" in env and "ct" in env and b"verify" not in dm.ub64(env["ct"]),
              "and the stored bytes carry no plaintext")

        # ---- claiming ------------------------------------------------------------------
        check(dm.claim_dog("rowan", relay=relay).get("ok") is False,
              "no dog before a verified handle — an address is an identity, not a free string")
        dm.claim_handle("daming", "wudaming00@gmail.com", relay=relay)
        check(dm.verify_handle("000000", relay=relay).get("ok") is not True,
              "a wrong code does not verify a handle")
        check(dm.verify_handle("123456", relay=relay).get("ok") is True, "the mailed code does")
        got = dm.claim_dog("rowan", relay=relay)
        check(got.get("address") == "rowan.daming@collie.run",
              "the address is <dog>.<handle>@domain — one zone, no per-user DNS (%s)" % got)

        # someone else's handle cannot mint an address under it
        forged = dm._post("/dog/claim", {"address": "rowan.someone@collie.run",
                                         "pub": dm.b64(dog_pub), "handle": "daming",
                                         "cert": dm.b64(b"\x00" * 32)}, relay=relay)
        check(forged.get("ok") is False, "a claim under a handle you do not hold is refused")

        # ---- reading, and the stamp that authorises it ------------------------------------
        me = dm._dog("rowan")
        state["mail"].append({"to": me["address"], "at": int(time.time()),
                              "env": dm.seal_to_dog(dm.ub64(me["pub"]),
                                                    json.dumps({"subject": "Verify your email",
                                                                "from": "noreply@stripe.com",
                                                                "text": "code 993214"}).encode())})
        msgs = dm.fetch("rowan", since=0, relay=relay)
        check(len(msgs) == 1 and msgs[0].get("subject") == "Verify your email",
              "a letter arrives, decrypted on this machine")
        check("993214" in json.dumps(msgs[0]), "with its body intact")

        rp = dm.relay_public(relay)
        h1 = dm._signed_headers(me, "GET", "/mail?since=0", rp)
        first = dm._get("/mail?since=0", headers=h1, relay=relay)
        check(first.get("ok") is True, "a fresh stamp is accepted")
        again = dm._get("/mail?since=0", headers=h1, relay=relay)
        check(again.get("ok") is False, "the same stamp twice is a replay and is refused")
        h2 = dm._signed_headers(me, "GET", "/mail?since=0", rp)
        moved = dm._get("/mail?since=999", headers=h2, relay=relay)
        check(moved.get("ok") is False, "a stamp cannot be lifted onto a different path")

        # ---- wait_for: the reason the module exists ---------------------------------------
        def deliver_later():
            time.sleep(1.0)
            state["mail"].append({"to": me["address"], "at": int(time.time()) + 1,
                                  "env": dm.seal_to_dog(dm.ub64(me["pub"]),
                                                        json.dumps({"subject": "Confirm your Notion account",
                                                                    "from": "team@notion.so"}).encode())})

        threading.Thread(target=deliver_later, daemon=True).start()
        hit = dm.wait_for("rowan", subject="confirm", timeout=15, poll=0.5, relay=relay)
        check(hit.get("from") == "team@notion.so", "wait_for returns the letter it was waiting for")
        miss = dm.wait_for("rowan", subject="nothing like this", timeout=2, poll=0.5, relay=relay)
        check(miss == {}, "and gives up empty-handed rather than hanging")
    finally:
        srv.shutdown()

    print("\n  " + ("%d FAILED" % len(fails) if fails else "dog mail: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
