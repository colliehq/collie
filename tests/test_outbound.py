import json

import pytest

from harness.outbound import SignedWebhookClient, verify_signature


class Response:
    status = 202
    def read(self, limit):
        return b"accepted"


def test_signed_webhook_is_idempotent_bounded_and_verifiable(monkeypatch):
    monkeypatch.setattr("harness.outbound.socket.getaddrinfo",
                        lambda *_a, **_k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        return Response()

    secret = b"x" * 32
    client = SignedWebhookClient(url="https://hooks.example.test/collie",
                                 secret=secret,
                                 allowed_hosts=frozenset({"hooks.example.test"}),
                                 opener=opener)
    receipt = client.deliver("mission.completed", {"ok": True},
                             event_id="evt-1", timestamp=1000, nonce="nonce")
    request = captured["request"]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["idempotency-key"] == "evt-1"
    assert verify_signature(secret, request.data, 1000, "nonce",
                            headers["x-collie-signature"], now=1001)
    assert json.loads(request.data)["payload"] == {"ok": True}
    assert receipt.status == 202 and receipt.event_id == "evt-1"


def test_webhook_rechecks_dns_before_delivery(monkeypatch):
    answers = iter([
        [(2, 1, 6, "", ("93.184.216.34", 443))],
        [(2, 1, 6, "", ("127.0.0.1", 443))],
    ])
    monkeypatch.setattr("harness.outbound.socket.getaddrinfo",
                        lambda *_a, **_k: next(answers))
    client = SignedWebhookClient(url="https://hooks.example.test/collie",
                                 secret=b"x" * 32,
                                 allowed_hosts=frozenset({"hooks.example.test"}),
                                 opener=lambda *_a, **_k: Response())

    with pytest.raises(ValueError, match="non-public"):
        client.deliver("mission.completed", {"ok": True})
