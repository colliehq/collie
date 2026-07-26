"""harness.e2e — the crypto behind a zero-knowledge relay.

The properties worth locking are the ones a plausible-looking implementation gets wrong quietly:

  • the pairing code actually authenticates the key swap — a relay that substitutes a public key must
    fail the confirm tag, because that swap is the whole attack E2E exists to stop
  • the AAD really binds a frame to its place — a frame replayed into another request, reordered, or
    reflected back in the other direction must fail to open, not merely "look odd"
  • keys are derived deterministically from documented labels, so the Swift half can agree
  • nothing falls back to plaintext when `cryptography` is missing

It also writes cross-language vectors (E2E_VECTORS env var) for Tests/E2ECheck on the iOS side: two
implementations of one wire format is exactly where silent mismatches live.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import e2e                                            # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


ROOM = "r7f3a91c2e8d40b56"
PAIRCODE = "K7QW-3M2X"
SESSION = "20260726-041500-aa11"


def test_transcript_is_unambiguous():
    """Length-prefixing is the point: no field's bytes can be read as the next field's."""
    a = e2e.transcript("ab", b"\x01" * 32, b"\x02" * 32)
    b = e2e.transcript("a", b"b" + b"\x01" * 32, b"\x02" * 32)
    check(a != b, "a crafted room cannot collide with a public key in the transcript")
    check(e2e.lp("") == b"\x00\x00\x00\x00", "LP of empty is a bare length")
    check(e2e.lp(b"xy") == b"\x00\x00\x00\x02xy", "LP prefixes a 4-byte big-endian length")


def test_paircode_authenticates_the_key_swap():
    desktop_priv, desktop_pub = e2e.keypair()
    phone_priv, phone_pub = e2e.keypair()

    tag_d = e2e.confirm_tag(PAIRCODE, ROOM, desktop_pub, phone_pub, e2e.SIDE_DESKTOP)
    tag_p = e2e.confirm_tag(PAIRCODE, ROOM, desktop_pub, phone_pub, e2e.SIDE_PHONE)
    check(tag_d != tag_p, "the two sides' tags differ (a tag cannot be reflected back)")
    check(e2e.verify_confirm(PAIRCODE, ROOM, desktop_pub, phone_pub, e2e.SIDE_DESKTOP, tag_d),
          "an untampered transcript verifies")

    # the attack: the relay swaps in its own key and forwards it
    _, evil_pub = e2e.keypair()
    check(not e2e.verify_confirm(PAIRCODE, ROOM, evil_pub, phone_pub, e2e.SIDE_DESKTOP, tag_d),
          "a SWAPPED desktop key fails the tag — MITM detected")
    check(not e2e.verify_confirm(PAIRCODE, ROOM, desktop_pub, evil_pub, e2e.SIDE_DESKTOP, tag_d),
          "a swapped phone key fails the tag too")
    check(not e2e.verify_confirm("WRONG-CODE", ROOM, desktop_pub, phone_pub, e2e.SIDE_DESKTOP, tag_d),
          "a guessed pairing code fails")
    check(not e2e.verify_confirm(PAIRCODE, "other-room", desktop_pub, phone_pub,
                                 e2e.SIDE_DESKTOP, tag_d),
          "the tag is bound to the room")

    # and both sides land on the same key material
    s_d = e2e.shared_secret(desktop_priv, phone_pub)
    s_p = e2e.shared_secret(phone_priv, desktop_pub)
    check(s_d == s_p, "X25519 agrees on both sides")
    check(e2e.device_key(s_d, ROOM) == e2e.device_key(s_p, ROOM), "K_dev matches on both sides")
    check(e2e.device_key(s_d, ROOM) != e2e.device_key(s_d, "other"),
          "K_dev is bound to the room (a second desktop is a different key)")
    k_dev = e2e.device_key(s_d, ROOM)
    check(e2e.session_key(k_dev, SESSION) != e2e.session_key(k_dev, "other-session"),
          "K_sess differs per session")
    check(len(k_dev) == 32 and len(e2e.session_key(k_dev, SESSION)) == 32, "keys are 32 bytes")


def test_frames_are_bound_to_their_place():
    key = e2e.session_key(b"\x11" * 32, SESSION)
    payload = b'event: token\ndata: {"t":"hello "}\n\n'
    sealed = e2e.seal_chunk(key, payload, room=ROOM, frame_id=7, session=SESSION, seq=3)

    got = e2e.open_chunk(key, sealed, room=ROOM, frame_id=7, session=SESSION, seq=3)
    check(got == payload, "a chunk opens with the right context")

    from cryptography.exceptions import InvalidTag

    def refuses(msg, **kw):
        args = dict(room=ROOM, frame_id=7, session=SESSION, seq=3)
        args.update(kw)
        try:
            e2e.open_chunk(key, sealed, **args)
            check(False, msg)
        except InvalidTag:
            check(True, msg)

    refuses("a frame replayed at another seq is refused", seq=4)
    refuses("a frame replayed into another request id is refused", frame_id=8)
    refuses("a frame replayed into another session is refused", session="20260726-999999-zz99")
    refuses("a frame from another room is refused", room="r-other")

    # reflected direction: seal one way, try to open the other
    try:
        e2e.open_(key, sealed, e2e.aad(ROOM, 7, SESSION, "c2s", 3))
        check(False, "a server→client frame cannot be reflected as client→server")
    except InvalidTag:
        check(True, "a server→client frame cannot be reflected as client→server")

    # a different key must fail
    try:
        e2e.open_chunk(e2e.session_key(b"\x22" * 32, SESSION), sealed,
                       room=ROOM, frame_id=7, session=SESSION, seq=3)
        check(False, "another device's key cannot open the frame")
    except InvalidTag:
        check(True, "another device's key cannot open the frame")

    # tampered ciphertext
    bad = dict(sealed)
    raw = bytearray(base64.b64decode(bad["ct"]))
    raw[0] ^= 0x01
    bad["ct"] = base64.b64encode(bytes(raw)).decode()
    try:
        e2e.open_chunk(key, bad, room=ROOM, frame_id=7, session=SESSION, seq=3)
        check(False, "a flipped ciphertext bit is refused")
    except InvalidTag:
        check(True, "a flipped ciphertext bit is refused")


def test_nonces_never_repeat():
    key = e2e.session_key(b"\x33" * 32, SESSION)
    nonces = {e2e.seal_chunk(key, b"x", room=ROOM, frame_id=1, session=SESSION, seq=i)["n"]
              for i in range(500)}
    check(len(nonces) == 500, "500 seals produced 500 distinct nonces (GCM forbids reuse)")


def test_request_envelope_hides_everything_routable():
    key = e2e.session_key(b"\x44" * 32, SESSION)
    sealed = e2e.seal_request(key, room=ROOM, frame_id=1, session=SESSION, seq=0,
                              method="POST", path="/api/stream?q=secret+question",
                              headers={"Accept": "text/event-stream"}, body=b'{"x":1}')
    blob = json.dumps(sealed)
    check("secret" not in blob and "api/stream" not in blob and "Accept" not in blob,
          "the sealed frame leaks neither path, query, headers nor body")
    opened = e2e.open_request(key, sealed, room=ROOM, frame_id=1, session=SESSION, seq=0)
    check(opened["method"] == "POST" and opened["path"] == "/api/stream?q=secret+question"
          and opened["headers"]["Accept"] == "text/event-stream" and opened["body"] == b'{"x":1}',
          "the desktop recovers the request exactly")


def test_no_silent_plaintext_fallback():
    check(isinstance(e2e.E2EUnavailable("x"), RuntimeError),
          "a missing crypto library raises, so a hosted relay cannot fall back to plaintext")
    check(e2e.available(), "cryptography is installed in this environment")


def write_vectors(path):
    """Emit vectors for the Swift half. Fixed keys/nonces so both sides must agree byte for byte."""
    desktop_priv = bytes(range(32))
    phone_priv = bytes(range(32, 64))
    c = e2e._crypto()
    dpriv = c["X25519PrivateKey"].from_private_bytes(desktop_priv)
    ppriv = c["X25519PrivateKey"].from_private_bytes(phone_priv)
    dpub = dpriv.public_key().public_bytes(c["Encoding"].Raw, c["PublicFormat"].Raw)
    ppub = ppriv.public_key().public_bytes(c["Encoding"].Raw, c["PublicFormat"].Raw)
    shared = e2e.shared_secret(desktop_priv, ppub)
    k_dev = e2e.device_key(shared, ROOM)
    k_sess = e2e.session_key(k_dev, SESSION)

    b64 = lambda b: base64.b64encode(b).decode("ascii")
    payload = b'event: token\ndata: {"t":"the quick brown fox "}\n\n'
    nonce = bytes(range(12))
    ct = c["AESGCM"](k_sess).encrypt(nonce, payload,
                                     e2e.aad(ROOM, 7, SESSION, "s2c", 3))
    vectors = {
        "room": ROOM, "paircode": PAIRCODE, "session": SESSION,
        "desktop_private": b64(desktop_priv), "desktop_public": b64(dpub),
        "phone_private": b64(phone_priv), "phone_public": b64(ppub),
        "shared": b64(shared), "k_dev": b64(k_dev), "k_sess": b64(k_sess),
        "transcript": b64(e2e.transcript(ROOM, dpub, ppub)),
        "confirm_desktop": b64(e2e.confirm_tag(PAIRCODE, ROOM, dpub, ppub, e2e.SIDE_DESKTOP)),
        "confirm_phone": b64(e2e.confirm_tag(PAIRCODE, ROOM, dpub, ppub, e2e.SIDE_PHONE)),
        "aad_s2c_7_3": b64(e2e.aad(ROOM, 7, SESSION, "s2c", 3)),
        "chunk_plaintext": b64(payload),
        "chunk_nonce": b64(nonce),
        "chunk_sealed": b64(ct),
    }
    with open(path, "w") as f:
        json.dump(vectors, f, indent=2)
    print("  wrote cross-language vectors -> %s" % path)


def main():
    if not e2e.available():
        print("  SKIP harness.e2e needs `cryptography` (pip install 'collie-harness[remote]')")
        return
    test_transcript_is_unambiguous()
    test_paircode_authenticates_the_key_swap()
    test_frames_are_bound_to_their_place()
    test_nonces_never_repeat()
    test_request_envelope_hides_everything_routable()
    test_no_silent_plaintext_fallback()
    out = os.environ.get("E2E_VECTORS")
    if out:
        write_vectors(out)
    if _fails:
        print("\n%d FAILED" % len(_fails))
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
