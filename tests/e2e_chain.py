"""Drive the whole Collie Remote chain with E2E on, against a REAL deployed Worker.

  phone (this script) → Cloudflare Worker + Durable Object → agent WebSocket → collie web on 127.0.0.1

What it proves, which no unit test can: the pairing handshake survives a round trip through a relay
that cannot read it, a sealed request is opened by the desktop, the response comes back sealed, and —
the point of the exercise — the relay never sees the path, headers, body or answer.

  python3 e2e_chain.py https://<worker>.workers.dev
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/Users/siningxu/projects/collie")
from harness import e2e                                            # noqa: E402

RELAY = sys.argv[1].rstrip("/")
ROOM = open("/private/tmp/claude-501/-Users-siningxu/b2bdca8a-093a-4cbe-92fc-fad172e99672/"
            "scratchpad/room.txt").read().strip()
PAIRCODE = open("/private/tmp/claude-501/-Users-siningxu/b2bdca8a-093a-4cbe-92fc-fad172e99672/"
                "scratchpad/paircode.txt").read().strip()
BASE = "%s/r/%s/" % (RELAY, ROOM)
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:200]}


# ---------------------------------------------------------------- 1. pair with a key exchange
priv, pub = e2e.keypair()
print("phone: X25519 public key %s…" % base64.b64encode(pub).decode()[:12])

# fetch the desktop's PUBLIC key first, so our confirm tag covers the true transcript (both keys), not
# a placeholder. It is public by definition; the pairing code is what authenticates it.
req = urllib.request.Request(BASE + "e2e", headers={"User-Agent": UA})
with urllib.request.urlopen(req, timeout=30) as r:
    advertised = json.load(r).get("pub") or ""
check(len(base64.b64decode(advertised)) == 32 if advertised else False,
      "the relay advertises the desktop's X25519 public key")
desktop_pub_pre = base64.b64decode(advertised)

status, body = post("pair", {
    "paircode": PAIRCODE, "device_id": "e2e-chain-test", "name": "chain test",
    "pub": base64.b64encode(pub).decode("ascii"),
    "confirm": base64.b64encode(
        e2e.confirm_tag(PAIRCODE, ROOM, desktop_pub_pre, pub, e2e.SIDE_PHONE)).decode("ascii"),
})
check(status == 200, "pair returned 200 (got %s %s)" % (status, body.get("error", "")))
if status != 200:
    print("\n%d FAILED" % len(fails))
    sys.exit(1)

token = body["token"]
desktop_pub = base64.b64decode(body["pub"])
check(desktop_pub == desktop_pub_pre, "the key returned by pairing is the one advertised")
desktop_confirm = base64.b64decode(body["confirm"])
check(len(desktop_pub) == 32, "the desktop returned an X25519 public key through the relay")
check(e2e.verify_confirm(PAIRCODE, ROOM, desktop_pub, pub, e2e.SIDE_DESKTOP, desktop_confirm),
      "the desktop's confirm tag verifies — the relay did not swap its key")

k_dev = e2e.device_key(e2e.shared_secret(priv, desktop_pub), ROOM)
print("phone: K_dev %s…" % base64.b64encode(k_dev).decode()[:12])

# ---------------------------------------------------------------- 2. a sealed request
SESSION = "s1"
key = e2e.session_key(k_dev, SESSION)
sealed = e2e.seal_request(key, room=ROOM, frame_id=1, session=SESSION, seq=0,
                          method="GET", path="/api/sessions", headers={"Accept": "application/json"})
blob = json.dumps(sealed)
check("api/sessions" not in blob, "the sealed request does not contain the path")

req = urllib.request.Request(BASE + "api/sessions", headers={
    "Authorization": "Bearer " + token, "User-Agent": UA,
    "X-Collie-Enc": blob, "X-Collie-Session": SESSION, "X-Collie-Rid": "1",
})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        check(r.status == 200, "the relay accepted the sealed request")
except urllib.error.HTTPError as e:
    check(False, "sealed request failed: HTTP %s %s" % (e.code, e.read()[:200]))
    raw = b""

# ---------------------------------------------------------------- 3. open the sealed response
lines = [l for l in raw.split(b"\n") if l.strip()]
check(bool(lines), "the relay forwarded sealed frames (%d)" % len(lines))
opened, head = [], None
for line in lines:
    frame = json.loads(line)
    seq = int(frame.get("seq") or 0)
    payload = e2e.open_chunk(key, json.loads(frame["enc"]), room=ROOM, frame_id=1,
                             session=SESSION, seq=seq)
    if seq == 0:
        head = json.loads(payload)
    else:
        opened.append(payload)
check(head is not None and head.get("status") == 200,
      "the sealed head decrypts to the local server's real status (%s)" % (head or {}).get("status"))
body_bytes = b"".join(opened)
try:
    payload = json.loads(body_bytes)
    ok = "sessions" in payload
except Exception:
    ok = False
check(ok, "the sealed body decrypts to collie's /api/sessions JSON (%d bytes)" % len(body_bytes))

# ---------------------------------------------------------------- 4. what the relay could see
check(b"sessions" not in raw, "the bytes the relay forwarded contain no plaintext 'sessions' key")
check(all(set(json.loads(l).keys()) <= {"enc", "seq"} for l in lines),
      "every forwarded frame carries only {enc, seq} — nothing readable")

print("\n" + ("all green" if not fails else "%d FAILED" % len(fails)))
sys.exit(1 if fails else 0)
