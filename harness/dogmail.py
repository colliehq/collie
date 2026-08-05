"""An address of a dog's own, and the ability to wait for a letter.

Why this exists. Every service on the internet proves you are a person by mailing you something —
a verification link, an invite, a code. A dog without an address must borrow its owner's inbox,
which means a human reads the mail and a human clicks the link, which is exactly the interruption
that makes an agent an assistant rather than a colleague. `wait_for` is therefore the point of this
module; `list` and `read` are conveniences around it.

Why the relay cannot read it. Mail for every user's dogs passes through one hosted Worker. A design
where that operator can read verification codes contradicts the thing collie is — so the Worker
seals each message to the receiving dog's public key the moment it arrives and stores only
ciphertext. What it keeps is unreadable to it, by construction rather than by policy.

Be precise about the limits, because "end-to-end encrypted" would be a lie here:
  · SMTP is a cleartext protocol. The message exists in plaintext in Worker memory for the instant
    between delivery and sealing. The promise is that it is never STORED in the clear.
  · The relay sees metadata: which address received something, when, and how big.
  · The private key lives on this machine. A compromised machine means that dog's mail is readable;
    what the encryption buys is that a compromised RELAY is not.

Identity, and what binds a dog to its address:

  handle "daming"   claimed once, by proving control of a real mailbox (a code is mailed there),
                    and bound from then on to a handle key. Only that key can create addresses
                    under `*.daming@…`, which is what stops someone else claiming your dog's name.
        │
        ├── dog "rowan" — its own keypair, generated on ITS machine and never moved. Registered
        └── dog "juno"    with a tag the handle key makes; retiring one is a revocation, not an
                          address left behind in a stranger's account records.

Authentication carries no bearer token: a token on disk is a token that can be copied. Every
request is stamped with an HMAC over a key derived from X25519(dog_private, relay_public) — the
relay can recompute it because it is a party to that exchange, and nobody else can.

    K_auth = HKDF(X25519(dog_priv, relay_pub), salt=address, info="collie-mail-auth")
    mac    = HMAC(K_auth, method ‖ path ‖ ts ‖ nonce)

KNOWN LIMIT of doing it with key agreement rather than signatures: the relay operator, holding its
own private key, could register a different key for an address it hosts — i.e. redirect future mail
for an address, though never read what has already been delivered. Closing that needs a signature
scheme (Ed25519) so the handle's authority is checkable without the relay being a party to it.
Written down here rather than left as an assumption.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import e2e

RELAY = os.environ.get("COLLIE_MAIL_RELAY", "https://mail.collie.run")
DOMAIN = os.environ.get("COLLIE_MAIL_DOMAIN", "collie.run")
STORE = os.path.expanduser("~/.collie/mail.json")

INFO_AUTH = b"collie-mail-auth"
INFO_SEAL = b"collie-mail-seal"
INFO_CERT = b"collie-mail-cert"
SKEW = 120                      # seconds a request stamp may be off before the relay refuses it


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def ub64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ---------------------------------------------------------------- the store

def load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def save(d: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
    try:
        from . import plat
        plat.chmod_private(tmp)          # private keys live in here
    except Exception:
        pass
    os.replace(tmp, STORE)


def address_for(dog: str, handle: str) -> str:
    """`rowan.daming@collie.run` — flat, so one MX and one catch-all Worker serve every user and
    adding a dog is a row rather than a DNS change."""
    return "%s.%s@%s" % (dog.strip().lower(), handle.strip().lower(), DOMAIN)


# ---------------------------------------------------------------- keys and envelopes

def _derive(private: bytes, peer_public: bytes, salt: bytes, info: bytes) -> bytes:
    return e2e._hkdf(e2e.shared_secret(private, peer_public), salt, info)


def auth_key(dog_priv: bytes, relay_pub: bytes, address: str) -> bytes:
    return _derive(dog_priv, relay_pub, address.encode("utf-8"), INFO_AUTH)


def cert_tag(handle_priv: bytes, relay_pub: bytes, address: str, dog_pub: bytes) -> bytes:
    """The handle's authority over one address, in a form the relay can check.

    Keyed by the handle↔relay agreement, so a claim is only accepted for an address whose handle
    key made this tag — that is what stops one user creating a dog under another's handle.
    """
    k = _derive(handle_priv, relay_pub, b"handle", INFO_CERT)
    return _mac(k, e2e.lp(address) + e2e.lp(dog_pub))


def _mac(key: bytes, message: bytes) -> bytes:
    c = e2e._crypto()
    m = c["hmac"].HMAC(key, c["hashes"].SHA256())
    m.update(message)
    return m.finalize()


def seal_to_dog(dog_pub: bytes, plaintext: bytes) -> dict:
    """What the Worker does on delivery, mirrored here so the tests exercise the real path.

    Ephemeral-static: a throwaway keypair per message, so the sender needs no long-term identity
    and nothing links two messages to one another.
    """
    eph_priv, eph_pub = e2e.keypair()
    key = _derive(eph_priv, dog_pub, b"", INFO_SEAL)
    env = e2e.seal(key, plaintext, e2e.lp(eph_pub))
    env["epk"] = b64(eph_pub)
    return env


def open_from_relay(dog_priv: bytes, env: dict) -> bytes:
    eph_pub = ub64(env["epk"])
    key = _derive(dog_priv, eph_pub, b"", INFO_SEAL)
    return e2e.open_(key, env, e2e.lp(eph_pub))


# ---------------------------------------------------------------- the relay

def _post(path: str, payload: dict, headers: dict = None, relay: str = "") -> dict:
    url = (relay or RELAY).rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=dict({"content-type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return dict(json.loads(body or "{}"), ok=False, status=e.code)
        except ValueError:
            return {"ok": False, "status": e.code, "error": body[:200]}


def _get(path: str, headers: dict = None, relay: str = "") -> dict:
    url = (relay or RELAY).rstrip("/") + path
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.read().decode("utf-8", "replace")[:200]}


def relay_public(relay: str = "") -> bytes:
    """The relay's X25519 public key. Cached after the first fetch — but note what trusting it on
    first use means: whoever answers this endpoint becomes the party every auth key is derived
    against. Pin it in the store rather than fetching it fresh each time."""
    st = load()
    if st.get("relay_pub"):
        return ub64(st["relay_pub"])
    d = _get("/pubkey", relay=relay)
    if not d.get("pub"):
        raise RuntimeError("relay did not publish a public key: %s" % json.dumps(d)[:200])
    st["relay_pub"] = d["pub"]
    save(st)
    return ub64(d["pub"])


def _signed_headers(dog: dict, method: str, path: str, relay_pub: bytes) -> dict:
    ts = str(int(time.time()))
    nonce = b64(os.urandom(12))
    k = auth_key(ub64(dog["priv"]), relay_pub, dog["address"])
    mac = _mac(k, e2e.lp(method) + e2e.lp(path) + e2e.lp(ts) + e2e.lp(nonce))
    return {"x-collie-addr": dog["address"], "x-collie-ts": ts,
            "x-collie-nonce": nonce, "x-collie-mac": b64(mac)}


# ---------------------------------------------------------------- claiming

def claim_handle(handle: str, email: str, relay: str = "") -> dict:
    """Step one, once per person: prove you control a real mailbox, and bind the handle to a key."""
    st = load()
    priv, pub = e2e.keypair()
    st.setdefault("handle", {})
    st["handle"].update({"name": handle, "priv": b64(priv), "pub": b64(pub), "verified": False})
    save(st)
    return _post("/handle/claim", {"handle": handle, "pub": b64(pub), "email": email}, relay=relay)


def verify_handle(code: str, relay: str = "") -> dict:
    st = load()
    h = st.get("handle") or {}
    if not h.get("name"):
        return {"ok": False, "error": "no handle claimed on this machine yet"}
    d = _post("/handle/verify", {"handle": h["name"], "code": code, "pub": h["pub"]}, relay=relay)
    if d.get("ok"):
        h["verified"] = True
        save(st)
    return d


def claim_dog(name: str, relay: str = "") -> dict:
    """Give one dog an address. Its key is made HERE and never leaves."""
    st = load()
    h = st.get("handle") or {}
    if not h.get("verified"):
        return {"ok": False, "error": "claim and verify a handle first (collie mail claim)"}
    dogs = st.setdefault("dogs", {})
    if name in dogs and dogs[name].get("address"):
        return {"ok": True, "address": dogs[name]["address"], "note": "already had one"}
    rp = relay_public(relay)
    priv, pub = e2e.keypair()
    address = address_for(name, h["name"])
    tag = cert_tag(ub64(h["priv"]), rp, address, pub)
    d = _post("/dog/claim", {"address": address, "pub": b64(pub), "handle": h["name"],
                             "cert": b64(tag)}, relay=relay)
    if not d.get("ok"):
        return d
    dogs[name] = {"address": address, "priv": b64(priv), "pub": b64(pub), "cursor": 0}
    save(st)
    return {"ok": True, "address": address}


# ---------------------------------------------------------------- reading

def _dog(name: str = "") -> dict:
    st = load()
    dogs = st.get("dogs") or {}
    if name:
        return dogs.get(name) or {}
    return (list(dogs.values()) or [{}])[0]


def fetch(name: str = "", since: int = None, relay: str = "") -> list:
    """Everything waiting, decrypted here. The relay hands over ciphertext and a delivery time."""
    dog = _dog(name)
    if not dog.get("address"):
        return []
    rp = relay_public(relay)
    cursor = dog.get("cursor", 0) if since is None else since
    path = "/mail?since=%d" % cursor
    d = _get(path, headers=_signed_headers(dog, "GET", path, rp), relay=relay)
    out = []
    for m in d.get("messages") or []:
        try:
            raw = open_from_relay(ub64(dog["priv"]), m["env"])
        except Exception as e:
            out.append({"at": m.get("at"), "error": "could not open: %s" % type(e).__name__})
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            msg = {"raw": raw.decode("utf-8", "replace")}
        msg["at"] = m.get("at")
        out.append(msg)
    if out:
        st = load()
        for k, v in (st.get("dogs") or {}).items():
            if v.get("address") == dog["address"]:
                v["cursor"] = max([m.get("at") or 0 for m in d.get("messages") or []] + [cursor])
        save(st)
    return out


def wait_for(name: str = "", subject: str = "", sender: str = "", timeout: int = 180,
             poll: float = 5.0, relay: str = "") -> dict:
    """Block until a matching letter arrives, or the time runs out.

    This is the one that changes what an agent can finish on its own: a signup that ends in "check
    your email" stops being a handover to a human.
    """
    deadline = time.time() + max(1, int(timeout))
    subject, sender = (subject or "").lower(), (sender or "").lower()
    while time.time() < deadline:
        for m in fetch(name, relay=relay):
            if subject and subject not in (m.get("subject") or "").lower():
                continue
            if sender and sender not in (m.get("from") or "").lower():
                continue
            return m
        time.sleep(min(poll, max(0.5, deadline - time.time())))
    return {}
