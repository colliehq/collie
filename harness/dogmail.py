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
import hashlib
import json
import os
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import e2e

RELAY = os.environ.get("COLLIE_MAIL_RELAY", "https://mail.collie.run")
DOMAIN = os.environ.get("COLLIE_MAIL_DOMAIN", "collie.run")
_DEFAULT_STORE = os.path.expanduser("~/.collie/mail.json")
STORE = _DEFAULT_STORE       # legacy callers/tests may supply an explicit store

INFO_AUTH = b"collie-mail-auth"
INFO_SEAL = b"collie-mail-seal"
INFO_CERT = b"collie-mail-cert"
SKEW = 120                      # seconds a request stamp may be off before the relay refuses it


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def ub64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ---------------------------------------------------------------- the store

def _store_path(state_dir=None):
    if state_dir is not None:
        return os.path.join(os.path.abspath(os.path.expanduser(state_dir)), "mail.json")
    if STORE != _DEFAULT_STORE:
        return STORE
    from .controlplane import state_dir as active_state_dir
    return os.path.join(active_state_dir(), "mail.json")


def load(state_dir=None) -> dict:
    try:
        with open(_store_path(state_dir), encoding="utf-8") as f:
            raw = f.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return {}
    try:
        value = json.loads(raw)
        if (len(raw) > 1024 * 1024 or not isinstance(value, dict)
                or not isinstance(value.get("dogs", {}), dict)
                or not isinstance(value.get("handle", {}), dict)):
            raise ValueError("invalid mail state")
        return value
    except ValueError:
        raise ValueError("mail identity store needs repair; existing keys were kept") from None


def save(d: dict, state_dir=None) -> None:
    from . import sessions
    path = _store_path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sessions._locked(path):
        load(state_dir)  # Never replace an unreadable key store with a fresh identity.
        sessions._atomic_dump(d, path)
        from . import plat
        plat.chmod_private(path)


def _change(update, state_dir=None):
    from . import sessions
    path = _store_path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sessions._locked(path):
        data = load(state_dir)
        result = update(data)
        save(data, state_dir)
        return result


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

# urllib's default User-Agent ("Python-urllib/3.x") is refused by Cloudflare's bot protection with
# `error code: 1010` — the relay never sees the request. Found the hard way: the same URL answered
# from PowerShell and not from here. A client that identifies itself is also the polite thing.
UA = "collie-mail/1.0 (+https://collie.run)"
MAX_RESPONSE = 12 * 1024 * 1024


class MailRelayError(RuntimeError):
    """A bounded, credential-free failure with an explicit delivery boundary."""

    def __init__(self, message, *, delivery_unknown=False):
        super().__init__(message)
        self.delivery_unknown = delivery_unknown


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _relay_url(relay=""):
    value = (relay or RELAY).rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
    if ((parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("mail relay must be an HTTPS origin (or literal loopback) without credentials")
    return value


def _request(path, *, body=None, headers=None, relay=""):
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("invalid mail relay path")
    request = urllib.request.Request(_relay_url(relay) + path, data=body,
                                    headers=dict({"user-agent": UA, "content-type": "application/json"},
                                                 **(headers or {})))
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise MailRelayError("mail relay response exceeded the size limit", delivery_unknown=body is not None)
            value = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(value, dict):
                raise ValueError("mail relay response must be an object")
            return value
    except urllib.error.HTTPError as exc:
        # Status is useful; error pages can contain reflected secrets or private mail.
        raw = exc.read(16 * 1024 + 1)
        try:
            value = json.loads(raw) if len(raw) <= 16 * 1024 else {}
        except (ValueError, UnicodeDecodeError):
            value = {}
        clean = {"ok": False, "http_status": exc.code, "error": "mail relay refused the request"}
        if isinstance(value, dict):
            for key in ("id", "status", "receipt", "duplicate", "at", "updated"):
                if key in value:
                    clean[key] = value[key]
        return clean


def _post(path: str, payload: dict, headers: dict = None, relay: str = "") -> dict:
    return _request(path, body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    headers=headers, relay=relay)


def _get(path: str, headers: dict = None, relay: str = "") -> dict:
    return _request(path, headers=headers, relay=relay)


def relay_public(relay: str = "", *, state_dir=None) -> bytes:
    """The relay's X25519 public key. Cached after the first fetch — but note what trusting it on
    first use means: whoever answers this endpoint becomes the party every auth key is derived
    against. Pin it in the store rather than fetching it fresh each time."""
    st = load(state_dir)
    origin = _relay_url(relay)
    if st.get("relay_origin") and st["relay_origin"] != origin:
        raise ValueError("this mail identity is pinned to a different relay")
    if st.get("relay_pub"):
        return ub64(st["relay_pub"])
    d = _get("/pubkey", relay=relay)
    if not d.get("pub"):
        raise RuntimeError("relay did not publish a public key: %s" % json.dumps(d)[:200])
    def pin(current):
        # Another connection may have pinned the same relay while this request
        # was in flight. Never restore a stale snapshot over newer identities.
        existing = current.get("relay_pub")
        if existing and existing != d["pub"]:
            raise ValueError("mail relay public key changed; the existing pin was kept")
        current["relay_pub"] = d["pub"]
        current["relay_origin"] = origin
        return ub64(d["pub"])
    return _change(pin, state_dir)


def _signed_headers(dog: dict, method: str, path: str, relay_pub: bytes) -> dict:
    ts = str(int(time.time()))
    nonce = b64(os.urandom(12))
    k = auth_key(ub64(dog["priv"]), relay_pub, dog["address"])
    mac = _mac(k, e2e.lp(method) + e2e.lp(path) + e2e.lp(ts) + e2e.lp(nonce))
    return {"x-collie-addr": dog["address"], "x-collie-ts": ts,
            "x-collie-nonce": nonce, "x-collie-mac": b64(mac)}


# ---------------------------------------------------------------- claiming

def claim_handle(handle: str, email: str, relay: str = "", *, state_dir=None) -> dict:
    """Step one, once per person: prove you control a real mailbox, and bind the handle to a key."""
    from . import sessions
    handle = handle.strip().lower() if isinstance(handle, str) else ""
    email = email.strip().lower() if isinstance(email, str) else ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]", handle):
        raise ValueError("mail handle must have 3–32 letters, numbers or internal hyphens")
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email) or len(email) > 254:
        raise ValueError("enter one valid email address")
    with sessions._locked(_store_path(state_dir) + ".claim"):
        def reserve(current):
            old = current.get("handle") or {}
            if old.get("verified"):
                return None
            if old.get("priv") and (old.get("name") != handle or old.get("email", email) != email):
                raise ValueError("a mail identity is awaiting verification; finish that claim before changing it")
            if not old.get("priv"):
                priv, pub = e2e.keypair()
                old = {"name": handle, "priv": b64(priv), "pub": b64(pub), "verified": False}
            old["email"] = email
            current["handle"] = old
            return dict(old)
        identity = _change(reserve, state_dir)
        if identity is None:
            return {"ok": False, "error": "this device already has a verified mail identity"}
        return _post("/handle/claim", {"handle": handle, "pub": identity["pub"], "email": email}, relay=relay)


def verify_handle(code: str, relay: str = "", *, state_dir=None) -> dict:
    st = load(state_dir)
    h = st.get("handle") or {}
    if not h.get("name"):
        return {"ok": False, "error": "no handle claimed on this machine yet"}
    d = _post("/handle/verify", {"handle": h["name"], "code": code, "pub": h["pub"]}, relay=relay)
    if d.get("ok"):
        def mark(current):
            latest = current.get("handle") or {}
            if latest.get("pub") != h["pub"] or latest.get("name") != h["name"]:
                raise ValueError("mail identity changed during verification; existing keys were kept")
            latest["verified"] = True
        _change(mark, state_dir)
    return d


def claim_dog(name: str, relay: str = "", *, state_dir=None) -> dict:
    """Give one dog an address. Its key is made HERE and never leaves."""
    from . import sessions
    name = name.strip().lower() if isinstance(name, str) else ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}[a-z0-9]|[a-z0-9]", name):
        raise ValueError("mailbox name must have 1–32 letters, numbers or internal hyphens")
    with sessions._locked(_store_path(state_dir) + ".claim"):
        st = load(state_dir)
        h = st.get("handle") or {}
        if not h.get("verified"):
            return {"ok": False, "error": "claim and verify a handle first"}
        old = (st.get("dogs") or {}).get(name) or {}
        if old.get("address") and not old.get("pending"):
            return {"ok": True, "address": old["address"], "note": "already had one"}
        rp = relay_public(relay, state_dir=state_dir)
        def reserve(current):
            dogs = current.setdefault("dogs", {})
            if name not in dogs:
                priv, pub = e2e.keypair()
                dogs[name] = {"address": address_for(name, h["name"]), "priv": b64(priv),
                              "pub": b64(pub), "cursor": 0, "pending": True}
            return dict(dogs[name])
        dog = _change(reserve, state_dir)
        # A timeout or crash after the relay accepts no longer loses the only key.
        tag = cert_tag(ub64(h["priv"]), rp, dog["address"], ub64(dog["pub"]))
        response = _post("/dog/claim", {"address": dog["address"], "pub": dog["pub"],
                         "handle": h["name"], "cert": b64(tag)}, relay=relay)
        if response.get("ok"):
            def finish(current):
                latest = current["dogs"][name]
                if latest.get("pub") != dog["pub"]:
                    raise ValueError("mailbox identity changed during creation; existing keys were kept")
                latest.pop("pending", None)
            _change(finish, state_dir)
            return dict(response, address=dog["address"])
        return response


# ---------------------------------------------------------------- reading

def capabilities(relay=""):
    result = _get("/capabilities", relay=relay)
    if result.get("ok") is not True or (result.get("paging") or {}).get("endpoint") != "/mail-page":
        raise MailRelayError("this mail relay needs an update for desktop connections")
    return result


def _mail_request(name, method, path, *, payload=None, relay="", state_dir=None):
    dog = _dog(name, state_dir=state_dir)
    if not dog.get("address"):
        raise MailRelayError("create or finish connecting a Collie Mail address first")
    public = relay_public(relay, state_dir=state_dir)
    headers = _signed_headers(dog, method, path, public)
    return (_post(path, payload, headers=headers, relay=relay) if method == "POST"
            else _get(path, headers=headers, relay=relay))


def _opened(dog, item):
    identity = item.get("id")
    if not isinstance(identity, str) or not identity or len(identity) > 200:
        raise MailRelayError("mail relay returned an invalid message identifier")
    timestamp = item.get("at")
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0:
        raise MailRelayError("mail relay returned an invalid receipt time")
    try:
        raw = open_from_relay(ub64(dog["priv"]), item["env"])
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("mail content must be an object")
    except Exception:
        value = {"error": "received mail could not be decrypted or decoded"}
    return dict(value, id=identity, at=timestamp)


def fetch_page(name="", *, cursor=None, relay="", state_dir=None):
    """One bounded replayable page. Caller commits the cursor after durable intake.

    A completed scan starts again on the next poll: KV is eventually consistent,
    so a time watermark would silently lose late-visible mail. The channel store
    deduplicates stable ids. ``since`` is only an explicit initial-history choice.
    """
    cursor = cursor or {}
    if (not isinstance(cursor, dict) or set(cursor) - {"page", "since"}
            or not isinstance(cursor.get("page", ""), str) or len(cursor.get("page", "")) > 4096
            or not isinstance(cursor.get("since", 0), (int, float))
            or not math.isfinite(cursor.get("since", 0)) or cursor.get("since", 0) < 0):
        raise ValueError("invalid Collie Mail cursor")
    path = "/mail-page"
    if cursor.get("page"):
        path += "?" + urllib.parse.urlencode({"cursor": cursor["page"]})
    result = _mail_request(name, "GET", path, relay=relay, state_dir=state_dir)
    if result.get("ok") is not True:
        raise MailRelayError("mail could not be retrieved; check the relay and mailbox connection")
    rows = result.get("messages")
    more, page = result.get("more"), result.get("next_cursor")
    if (not isinstance(rows, list) or len(rows) > 50 or type(more) is not bool
            or (more and (not isinstance(page, str) or not page or len(page) > 4096
                         or page == cursor.get("page")))):
        raise MailRelayError("mail relay returned an invalid page; the cursor was kept")
    dog = _dog(name, state_dir=state_dir)
    messages = []
    for item in rows:
        if not isinstance(item, dict):
            raise MailRelayError("mail relay returned an invalid message")
        opened = _opened(dog, item)
        if opened["at"] >= cursor.get("since", 0):
            messages.append(opened)
    return {"messages": messages, "cursor": {"page": page if more else "", "since": cursor.get("since", 0)},
            "more": more}


def anchor(name="", *, relay="", state_dir=None):
    # Verify actual authenticated access; knowing the relay's public key does not.
    baseline = int(time.time())
    fetch_page(name, cursor={"since": baseline}, relay=relay, state_dir=state_dir)
    return {"cursor": {"page": "", "since": baseline}, "note": "Only mail received from now will be imported"}


def probe(name="", *, relay="", state_dir=None):
    info = capabilities(relay)
    anchor(name, relay=relay, state_dir=state_dir)
    return {"receive": True, "send": bool((info.get("send") or {}).get("configured")),
            "detail": "Inbox access verified. Outgoing mail still depends on the provider's sender permissions."}


def _receipt(result):
    if (result.get("ok") is True and result.get("status") == "sent"
            and isinstance(result.get("receipt"), str) and result["receipt"]):
        return {"status": "submitted", "provider_message_id": result["receipt"],
                "duplicate": bool(result.get("duplicate"))}
    code = result.get("http_status")
    unknown = result.get("status") != "failed" and code not in {400, 401, 403, 404, 409, 413, 429, 501}
    raise MailRelayError("mail submission is uncertain; check its receipt before retrying" if unknown
                         else "mail relay refused this message; check the owner address and connection",
                         delivery_unknown=unknown)


def send(name, result, *, relay="", state_dir=None):
    """Submit once with a body-bound auth stamp and stable result id."""
    metadata = result.get("metadata") or {}
    references = result.get("references") or metadata.get("references") or []
    references = references if isinstance(references, str) else " ".join(references)
    payload = {"id": result.get("id"), "to": result.get("destination"), "text": result.get("text"),
               "subject": result.get("subject") or "Collie result", "in_reply_to": result.get("in_reply_to") or "",
               "references": references}
    # The same serialization as _post: the digest covers the EXACT wire bytes.
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 96 * 1024:
        raise MailRelayError("outgoing mail exceeds the relay's message limit")
    path = "/send?sha256=" + hashlib.sha256(body).hexdigest()
    try:
        response = _mail_request(name, "POST", path, payload=payload, relay=relay, state_dir=state_dir)
        return _receipt(response)
    except MailRelayError:
        raise
    except Exception:
        raise MailRelayError("mail submission is uncertain; check its receipt before retrying",
                             delivery_unknown=True) from None


def send_status(name, result_id, *, relay="", state_dir=None):
    if not isinstance(result_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", result_id):
        raise ValueError("invalid mail result id")
    path = "/send-status?" + urllib.parse.urlencode({"id": result_id})
    return _receipt(_mail_request(name, "GET", path, relay=relay, state_dir=state_dir))

def _dog(name: str = "", *, state_dir=None) -> dict:
    st = load(state_dir)
    dogs = st.get("dogs") or {}
    if name:
        dog = dogs.get(name) or {}
        return {} if dog.get("pending") else dog
    return next((dog for dog in dogs.values() if not dog.get("pending")), {})


def fetch(name: str = "", since: int = None, relay: str = "", *, state_dir=None,
          advance_cursor: bool = True) -> list:
    """Everything waiting, decrypted here. The relay hands over ciphertext and a delivery time."""
    dog = _dog(name, state_dir=state_dir)
    if not dog.get("address"):
        return []
    rp = relay_public(relay, state_dir=state_dir)
    cursor = dog.get("cursor", 0) if since is None else since
    path = "/mail?since=%d" % cursor
    d = _get(path, headers=_signed_headers(dog, "GET", path, rp), relay=relay)
    if d.get("ok") is False:
        raise RuntimeError("mail could not be retrieved (status %s)" % d.get("status", "unknown"))
    out = []
    for m in d.get("messages") or []:
        message_id = m.get("id") or hashlib.sha256(json.dumps(
            m.get("env"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            raw = open_from_relay(ub64(dog["priv"]), m["env"])
        except Exception as e:
            out.append({"id": message_id, "at": m.get("at"), "error": "could not open: %s" % type(e).__name__})
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
            if not isinstance(msg, dict):
                raise ValueError("mail payload must be an object")
        except Exception:
            msg = {"raw": raw.decode("utf-8", "replace")}
        msg["at"] = m.get("at")
        # Old relays lack message ids; hashing the sealed envelope is stable
        # across fetches and never exposes message contents in a task id.
        msg["id"] = message_id
        out.append(msg)
    if out and advance_cursor and not any(m.get("error") for m in out):
        def advance(current):
            for v in (current.get("dogs") or {}).values():
                if v.get("address") == dog["address"]:
                    v["cursor"] = max([m.get("at") or 0 for m in d.get("messages") or []]
                                      + [cursor, v.get("cursor", 0)])
        _change(advance, state_dir)
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
