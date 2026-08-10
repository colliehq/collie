"""Hosted remote v2: relay-blind pairing, fail-closed requests and authenticated records."""
import base64
import json
import secrets
import time

from harness import e2e
from harness.remote import RelayClient, RemoteState


class Identity:
    room = "room-v2-test"
    agent_key = "agent-key"

    def __init__(self):
        self._d = {"devices": {}}
        self.saved = 0

    def approved_ids(self): return set()
    def approved_devices(self): return set()
    def device_hashes(self): return [x.get("token_sha") for x in self._d["devices"].values()]
    def device_keys(self): return {k: v["k_dev"] for k, v in self._d["devices"].items() if v.get("k_dev")}
    def add_or_update(self, did, token_hash, name):
        self._d["devices"][did] = {"token_sha": token_hash, "name": name}
    def set_device_key(self, did, key): self._d["devices"][did]["k_dev"] = key
    def forget_device(self, did): return self._d["devices"].pop(did, None) is not None
    def _save(self): self.saved += 1


class WS:
    def __init__(self): self.messages = []
    def send_text(self, value): self.messages.append(json.loads(value))


def client(secret=None, identity=None):
    return RelayClient("wss://relay.test", identity or Identity(), secret or secrets.token_urlsafe(32),
                       "127.0.0.1", 8787, "local-token")


def test_pairing_secret_never_enters_relay_frames_and_is_one_shot():
    secret = secrets.token_urlsafe(32)
    c = client(secret)
    ws = WS()
    c._ask_on_screen = lambda pending: None
    rotations = []
    def rotate():
        rotations.append(True)
        c.pairing_secret = secrets.token_urlsafe(32)
    c.on_pair = rotate

    phone_private, phone_public = e2e.keypair()
    desktop_public = c._e2e_keys[1]
    proof = e2e.confirm_tag(secret, c.room, desktop_public, phone_public, e2e.SIDE_PHONE)
    c._dispatch(ws, {
        "t": "pair_request", "id": "request-1", "device_id": "phone-1", "name": "iPhone",
        "pub": base64.b64encode(phone_public).decode(),
        "confirm": base64.b64encode(proof).decode(),
    })

    ready = ws.messages[-1]
    assert ready["t"] == "pair_ready"
    assert secret not in json.dumps(ready) and "paircode" not in ready
    assert rotations == [True]
    assert c.pending_pair["id"] == "request-1"
    returned_tag = base64.b64decode(ready["confirm"])
    assert e2e.verify_confirm(secret, c.room, desktop_public, phone_public,
                              e2e.SIDE_DESKTOP, returned_tag)
    assert ready["num"] == "%04d" % (int.from_bytes(returned_tag[:4], "big") % 10000)

    # Human approval only stages K_dev; it becomes durable after the relay has minted a token and
    # acknowledges device_added.
    c._reply_pair(ws, "request-1", True)
    assert "phone-1" not in c._e2e_devices
    token_hash = "a" * 64
    c._dispatch(ws, {"t": "device_added", "id": "store-1", "pair_id": "request-1",
                     "device_id": "phone-1", "hash": token_hash, "name": "iPhone"})
    assert "phone-1" in c._e2e_devices
    assert ws.messages[-1] == {"t": "device_stored", "id": "store-1",
                               "hash": token_hash, "ok": True}
    shared = e2e.shared_secret(phone_private, desktop_public)
    assert c._e2e_devices["phone-1"] == e2e.device_key(shared, c.room)


def test_unbound_device_registration_is_rejected_without_mutating_identity():
    identity = Identity()
    c = client(identity=identity)
    ws = WS()

    c._dispatch(ws, {"t": "device_added", "id": "forged", "pair_id": "not-approved",
                     "device_id": "attacker", "hash": "b" * 64, "name": "attacker"})

    assert identity._d["devices"] == {}
    assert c._e2e_devices == {}
    assert ws.messages == [{"t": "device_stored", "id": "forged",
                            "hash": "b" * 64, "ok": False}]


def test_device_registration_persistence_failure_restores_previous_device_row():
    class FailingIdentity(Identity):
        def _save(self):
            raise OSError("disk full")

    identity = FailingIdentity()
    old_key = base64.b64encode(b"o" * 32).decode()
    identity._d["devices"]["phone"] = {
        "token_sha": "c" * 64, "name": "old phone", "k_dev": old_key,
    }
    c = client(identity=identity)
    c._approved_pair_keys["phone"] = {
        "key": b"k" * 32, "pair_id": "approved-pair", "approved_at": time.time(),
    }
    ws = WS()

    c._dispatch(ws, {"t": "device_added", "id": "store-fail", "pair_id": "approved-pair",
                     "device_id": "phone", "hash": "d" * 64, "name": "new phone"})

    assert identity._d["devices"]["phone"] == {
        "token_sha": "c" * 64, "name": "old phone", "k_dev": old_key,
    }
    assert c._e2e_devices["phone"] == b"o" * 32
    assert ws.messages[-1]["t"] == "device_stored" and ws.messages[-1]["ok"] is False


def test_qr_secret_is_high_entropy_and_fragment_only():
    state = RemoteState.__new__(RemoteState)
    state.enabled = False
    state.web_base = "https://relay.example"
    state.identity = Identity()
    state.pairing_secret = secrets.token_urlsafe(32)
    state.paircode = "0000"
    state._paircode_at = 0
    desktop = client(identity=state.identity)
    state.client = desktop

    from urllib.parse import urlsplit
    url = state.link()
    parts = urlsplit(url)
    assert parts.query == "" and state.pairing_secret not in url.split("#", 1)[0]
    assert parts.fragment.startswith("pair=")
    encoded = parts.fragment.split("=", 1)[1]
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded))
    assert payload["v"] == 2 and payload["secret"] == state.pairing_secret
    raw_secret = payload["secret"].replace("-", "+").replace("_", "/")
    raw_secret += "=" * ((4 - len(raw_secret) % 4) % 4)
    assert len(base64.b64decode(raw_secret)) == 32


def test_desktop_attempt_limit_burns_the_secret():
    c = client("still-high-entropy-but-wrong-proof")
    ws = WS()
    rotated = []
    c.on_pair = lambda: rotated.append(True)
    _, phone_public = e2e.keypair()
    bad = base64.b64encode(b"x" * 32).decode()
    for index in range(c.PAIR_MAX):
        c._dispatch(ws, {"t": "pair_request", "id": str(index), "device_id": "attacker",
                         "pub": base64.b64encode(phone_public).decode(), "confirm": bad})
    assert all(message["t"] == "pair_invalid" for message in ws.messages)
    assert c.pairing_secret == ""
    assert rotated == [True]


def test_expired_pairing_secret_is_rejected_at_authentication_boundary():
    secret = secrets.token_urlsafe(32)
    c = client(secret)
    c.pairing_expires_at = 0
    ws = WS()
    rotations = []

    def rotate():
        rotations.append(True)
        c.pairing_secret = secrets.token_urlsafe(32)
        c.pairing_expires_at = 10**12

    c.on_pair = rotate
    _, phone_public = e2e.keypair()
    proof = e2e.confirm_tag(secret, c.room, c._e2e_keys[1], phone_public, e2e.SIDE_PHONE)
    c._dispatch(ws, {
        "t": "pair_request", "id": "expired", "device_id": "phone",
        "pub": base64.b64encode(phone_public).decode(),
        "confirm": base64.b64encode(proof).decode(),
    })

    assert ws.messages == [{"t": "pair_invalid", "id": "expired",
                            "error": "pairing proof refused"}]
    assert c.pending_pair is None
    assert rotations == [True]


def test_plaintext_requests_fail_closed_without_execution():
    c = client()
    ws = WS()
    spawned = []
    c._spawn = lambda *args: spawned.append(args)
    c._dispatch(ws, {"t": "req", "id": 7, "method": "POST", "path": "/api/stream?q=secret"})
    c._dispatch(ws, {"t": "body", "id": 7, "data": "c2VjcmV0"})
    assert spawned == []
    assert [message["t"] for message in ws.messages] == ["err", "err"]


def test_response_records_include_authenticated_terminal_and_exact_sequences():
    c = client()
    ws = WS()
    key = e2e.session_key(b"k" * 32, "s1")
    seq = c._send_head(ws, 9, key, "phone-rid", "s1", 201, {"content-type": "text/plain"})
    seq = c._send_data(ws, 9, key, "phone-rid", "s1", seq, b"hello")
    c._send_terminal(ws, 9, key, "phone-rid", "s1", seq, ok=True, last_data_seq=1)

    assert [message["seq"] for message in ws.messages] == [0, 1, 2]
    opened = []
    for message in ws.messages:
        payload = e2e.open_chunk(key, json.loads(message["enc"]), room=c.room,
                                 frame_id="phone-rid", session="s1", seq=message["seq"])
        opened.append(json.loads(payload))
    assert opened[0]["kind"] == "head" and opened[0]["status"] == 201
    assert opened[1] == {"kind": "data", "data_b64": base64.b64encode(b"hello").decode()}
    assert opened[2] == {"kind": "terminal", "ok": True, "last_data_seq": 1}


def test_non_idempotent_request_ids_are_persistently_one_shot():
    identity = Identity()
    c = client(identity=identity)
    assert c._claim_request("phone", "rid", "POST")
    assert not c._claim_request("phone", "rid", "POST")
    # The persisted ledger, not only an in-memory set, enforces the duplicate after a fresh client.
    restarted = client(identity=identity)
    assert not restarted._claim_request("phone", "rid", "DELETE")
    assert restarted._claim_request("phone", "get-rid", "GET", "/api/sessions")
    assert restarted._claim_request("phone", "get-rid", "GET", "/api/sessions")
    assert restarted._claim_request("phone", "stream-rid", "GET", "/api/stream?q=fix")
    assert not restarted._claim_request("phone", "stream-rid", "GET", "/api/stream?q=fix")
    assert identity.saved >= 1


def test_replay_ledger_refuses_capacity_instead_of_evicting_live_claims():
    identity = Identity()
    c = client(identity=identity)
    c.REPLAY_MAX = 2
    assert c._claim_request("phone", "first", "POST")
    assert c._claim_request("phone", "second", "POST")
    assert not c._claim_request("phone", "third", "POST")
    assert not c._claim_request("phone", "first", "POST")
    assert [row["c"] for row in identity._d["remote_v2_seen"]] == ["first", "second"]


def test_relay_url_requires_tls_except_for_exact_loopback_origins():
    identity = Identity()
    for unsafe in (
        "ws://relay.example", "wss://user:pass@relay.example",
        "wss://relay.example/path", "wss://relay.example?debug=1",
        "wss://relay.example#fragment",
    ):
        try:
            RelayClient(unsafe, identity, secrets.token_urlsafe(32),
                        "127.0.0.1", 8787, "local-token")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe relay URL was accepted: %s" % unsafe)

    assert client(identity=identity).relay_url == "wss://relay.test"
    local = RelayClient("ws://127.0.0.1:8788/", identity, secrets.token_urlsafe(32),
                        "127.0.0.1", 8787, "local-token")
    assert local.relay_url == "ws://127.0.0.1:8788"


def test_device_revoke_is_idempotently_acknowledged_only_after_durable_delete():
    identity = Identity()
    identity._d["devices"]["phone"] = {"token_sha": "hash", "name": "iPhone"}
    c = client(identity=identity)
    ws = WS()

    c._dispatch(ws, {"t": "device_revoke", "id": "one", "hash": "hash"})
    c._dispatch(ws, {"t": "device_revoke", "id": "retry", "hash": "hash"})

    assert [message["ok"] for message in ws.messages] == [True, True]
    assert identity._d["devices"] == {}


def test_duplicate_request_preserves_authenticated_409_outcome():
    c = client()
    ws = WS()
    key = e2e.session_key(b"k" * 32, "s1")
    c._e2e_key_for = lambda request: (
        key, "s1", "phone", {"method": "POST", "path": "/api/stream", "headers": {}, "body": b""})
    c._claim_request = lambda *_: False

    c._handle(ws, {"t": "req", "id": 9, "cid": "phone-rid", "session": "s1"}, b"")

    encrypted = [message for message in ws.messages if message["t"] != "end"]
    opened = []
    for message in encrypted:
        payload = e2e.open_chunk(key, json.loads(message["enc"]), room=c.room,
                                 frame_id="phone-rid", session="s1", seq=message["seq"])
        opened.append(json.loads(payload))
    assert opened[0]["kind"] == "head" and opened[0]["status"] == 409
    assert opened[-1] == {"kind": "terminal", "ok": True, "last_data_seq": 1}
