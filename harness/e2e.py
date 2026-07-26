"""End-to-end encryption for Collie Remote — the crypto half, no I/O.

Why: a self-hosted relay is your own Worker, so it may see plaintext. A *hosted* relay carries other
people's dev sessions, and "the operator can read your code and commands" is not acceptable. So the
hosted relay must be a zero-knowledge pipe: it routes `room`/`id`/`session` and forwards sealed bytes.
See relay/E2E_DESIGN.md for the threat model.

Primitives: X25519 · HKDF-SHA256 · AES-256-GCM · HMAC-SHA256, via `cryptography` (the `[remote]`
extra). No hand-rolled crypto.

WIRE SPEC v1 — the Swift half (CollieIOS/Pairing/E2E.swift) must agree byte for byte, and
tests/test_e2e.py emits vectors that the Swift checker verifies:

  handshake        both sides make an X25519 keypair and swap public keys THROUGH the relay, which
                   could tamper with them. The pairing code — shown on the desktop's screen, typed or
                   scanned into the phone, never sent to the relay — authenticates that swap:

                     transcript = LP("collie-e2e-v1") ‖ LP(room) ‖ LP(pub_desktop) ‖ LP(pub_phone)
                     confirm(side) = HMAC-SHA256(paircode, transcript ‖ side)      side ∈ {b"D", b"P"}

                   where LP(x) = 4-byte big-endian length ‖ x. The doc sketches plain concatenation;
                   length-prefixing removes the ambiguity where a crafted `room` could imitate the
                   start of a public key and produce a colliding transcript. The relay does not know
                   the paircode, so it cannot forge a tag: a swapped key fails verification → abort.

  keys             S      = X25519(own_private, peer_public)
                   K_dev  = HKDF-SHA256(ikm=S,     salt=room,  info="collie-remote-device",  L=32)
                   K_sess = HKDF-SHA256(ikm=K_dev, salt=b"",   info="collie-remote-session" ‖ LP(sid))

  frames           seal   = AES-256-GCM(K_sess, nonce=12 random bytes, plaintext, aad)
                   aad    = LP(room) ‖ LP(id) ‖ LP(session) ‖ LP(direction) ‖ 8-byte BE seq
                   direction ∈ {"c2s", "s2c"} so a frame cannot be reflected back, and `seq` binds a
                   frame to its position so the relay cannot drop, reorder or replay one silently.
"""
from __future__ import annotations

import base64
import json
import os
import struct

LABEL_TRANSCRIPT = b"collie-e2e-v1"
INFO_DEVICE = b"collie-remote-device"
INFO_SESSION = b"collie-remote-session"
SIDE_DESKTOP = b"D"
SIDE_PHONE = b"P"
NONCE_BYTES = 12
KEY_BYTES = 32


class E2EUnavailable(RuntimeError):
    """`cryptography` is not installed. On a HOSTED relay this must be fatal, never a plaintext
    fallback — degrading silently would hand the operator exactly what E2E exists to withhold."""


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes, hmac
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey, X25519PublicKey)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat, PrivateFormat, NoEncryption)
    except ImportError as e:                                       # pragma: no cover
        raise E2EUnavailable(
            "collie remote E2E needs the `cryptography` package: pip install 'collie-harness[remote]'"
        ) from e
    return dict(hashes=hashes, hmac=hmac, X25519PrivateKey=X25519PrivateKey,
                X25519PublicKey=X25519PublicKey, AESGCM=AESGCM, HKDF=HKDF,
                Encoding=Encoding, PublicFormat=PublicFormat, PrivateFormat=PrivateFormat,
                NoEncryption=NoEncryption)


def available() -> bool:
    try:
        _crypto()
        return True
    except E2EUnavailable:
        return False


# ---------------------------------------------------------------- framing helpers

def lp(value) -> bytes:
    """Length-prefix a field: 4-byte big-endian length ‖ bytes. Every transcript/AAD field goes
    through this, so no field's contents can be mistaken for the next field's."""
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return struct.pack(">I", len(raw)) + raw


def transcript(room: str, pub_desktop: bytes, pub_phone: bytes) -> bytes:
    return lp(LABEL_TRANSCRIPT) + lp(room) + lp(pub_desktop) + lp(pub_phone)


def confirm_tag(paircode: str, room: str, pub_desktop: bytes, pub_phone: bytes, side: bytes) -> bytes:
    """HMAC over the transcript, keyed by the pairing code — the out-of-band secret the relay lacks."""
    c = _crypto()
    if side not in (SIDE_DESKTOP, SIDE_PHONE):
        raise ValueError("side must be b'D' or b'P'")
    mac = c["hmac"].HMAC(paircode.encode("utf-8"), c["hashes"].SHA256())
    mac.update(transcript(room, pub_desktop, pub_phone) + side)
    return mac.finalize()


def verify_confirm(paircode: str, room: str, pub_desktop: bytes, pub_phone: bytes,
                   side: bytes, tag: bytes) -> bool:
    """Constant-time check of the peer's tag. False here means ABORT — the relay tampered with a key."""
    import hmac as _hmac
    return _hmac.compare_digest(confirm_tag(paircode, room, pub_desktop, pub_phone, side), tag)


# ---------------------------------------------------------------- keys

def keypair():
    """(private_bytes, public_bytes) — raw 32-byte X25519, the form that goes on the wire."""
    c = _crypto()
    priv = c["X25519PrivateKey"].generate()
    return (priv.private_bytes(c["Encoding"].Raw, c["PrivateFormat"].Raw, c["NoEncryption"]()),
            priv.public_key().public_bytes(c["Encoding"].Raw, c["PublicFormat"].Raw))


def shared_secret(private_bytes: bytes, peer_public: bytes) -> bytes:
    c = _crypto()
    priv = c["X25519PrivateKey"].from_private_bytes(private_bytes)
    return priv.exchange(c["X25519PublicKey"].from_public_bytes(peer_public))


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = KEY_BYTES) -> bytes:
    c = _crypto()
    return c["HKDF"](algorithm=c["hashes"].SHA256(), length=length, salt=salt,
                     info=info).derive(ikm)


def device_key(shared: bytes, room: str) -> bytes:
    """The long-term per-(device, room) key. Never leaves the device; the relay never sees it."""
    return _hkdf(shared, room.encode("utf-8"), INFO_DEVICE)


def session_key(k_dev: bytes, session_id: str) -> bytes:
    """One key per collie session, so a compromised session key does not expose the others."""
    return _hkdf(k_dev, b"", INFO_SESSION + lp(session_id))


# ---------------------------------------------------------------- frames

def aad(room: str, frame_id, session: str, direction: str, seq: int) -> bytes:
    """Bind a sealed frame to its exact context. Without this the relay could replay a frame into a
    different request, or swap two frames' order, without breaking authentication."""
    if direction not in ("c2s", "s2c"):
        raise ValueError("direction must be 'c2s' or 's2c'")
    return (lp(room) + lp(str(frame_id)) + lp(session) + lp(direction)
            + struct.pack(">Q", int(seq)))


def seal(key: bytes, plaintext: bytes, associated: bytes) -> dict:
    """{"n": nonce_b64, "ct": ciphertext_b64}. A fresh random nonce per frame — GCM forbids reuse."""
    c = _crypto()
    nonce = os.urandom(NONCE_BYTES)
    ct = c["AESGCM"](key).encrypt(nonce, plaintext, associated)
    return {"n": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii")}


def open_(key: bytes, enc: dict, associated: bytes) -> bytes:
    """Raises `cryptography.exceptions.InvalidTag` on any tampering — including a wrong AAD, which is
    how a replayed or reordered frame is caught."""
    c = _crypto()
    nonce = base64.b64decode(enc["n"])
    ct = base64.b64decode(enc["ct"])
    return c["AESGCM"](key).decrypt(nonce, ct, associated)


# ---------------------------------------------------------------- envelopes

def seal_request(key: bytes, *, room: str, frame_id, session: str, seq: int,
                 method: str, path: str, headers: dict, body: bytes = b"") -> dict:
    """The phone's request, sealed. The relay sees only room/id/session/seq and the ciphertext — not
    the path, not the headers, not the body."""
    envelope = {"method": method, "path": path, "headers": headers or {},
                "body_b64": base64.b64encode(body or b"").decode("ascii")}
    return seal(key, json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
                aad(room, frame_id, session, "c2s", seq))


def open_request(key: bytes, enc: dict, *, room: str, frame_id, session: str, seq: int) -> dict:
    envelope = json.loads(open_(key, enc, aad(room, frame_id, session, "c2s", seq)))
    envelope["body"] = base64.b64decode(envelope.pop("body_b64", "") or "")
    return envelope


def seal_chunk(key: bytes, payload: bytes, *, room: str, frame_id, session: str, seq: int) -> dict:
    """One response/SSE chunk, sealed. Sealing per chunk (not per response) is what keeps a run
    streaming frame-by-frame while still being end-to-end encrypted."""
    return seal(key, payload, aad(room, frame_id, session, "s2c", seq))


def open_chunk(key: bytes, enc: dict, *, room: str, frame_id, session: str, seq: int) -> bytes:
    return open_(key, enc, aad(room, frame_id, session, "s2c", seq))
