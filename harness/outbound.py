"""Signed, bounded outbound webhook delivery for integrations and A2A hand-offs."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request


MAX_BODY_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


class OutboundWebhookError(RuntimeError):
    pass


def _secret(value: bytes | bytearray) -> bytes:
    raw = bytes(value)
    if len(raw) < 32:
        raise ValueError("webhook signing secret must contain at least 32 bytes")
    return raw


def _body(payload: dict[str, Any]) -> bytes:
    if not isinstance(payload, dict):
        raise TypeError("webhook payload must be an object")
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("webhook payload is not safe JSON") from exc
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("webhook payload exceeds %d bytes" % MAX_BODY_BYTES)
    return raw


def signature(secret: bytes | bytearray, body: bytes, timestamp: int,
              nonce: str) -> str:
    digest = hashlib.sha256(body).hexdigest()
    statement = "v1:%d:%s:%s" % (int(timestamp), str(nonce), digest)
    return "v1=" + hmac.new(_secret(secret), statement.encode("ascii"),
                             hashlib.sha256).hexdigest()


def verify_signature(secret: bytes | bytearray, body: bytes, timestamp: int,
                     nonce: str, supplied: str, *, now: float | None = None,
                     max_age_s: int = 300) -> bool:
    now = time.time() if now is None else float(now)
    if max_age_s <= 0 or abs(now - int(timestamp)) > int(max_age_s):
        return False
    if not isinstance(supplied, str) or not supplied.startswith("v1="):
        return False
    try:
        expected = signature(secret, body, int(timestamp), str(nonce))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, supplied)


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OutboundWebhookError("could not resolve webhook host") from exc
    addresses = tuple(dict.fromkeys(str(row[4][0]) for row in rows))
    if not addresses:
        raise OutboundWebhookError("webhook host resolved to no address")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise ValueError("outbound webhook host resolves to a non-public address")
    return addresses


def _validate_target(url: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username \
            or parsed.password or parsed.fragment:
        raise ValueError("outbound webhook target must be a credential-free HTTPS URL")
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if not allowed_hosts or host not in allowed_hosts:
        raise ValueError("outbound webhook host is not in the exact allowlist")
    _public_addresses(host, parsed.port or 443)
    return parsed.geturl()


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    """Connect to the vetted IP while verifying TLS for the allowlisted host."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


@dataclass(frozen=True)
class DeliveryReceipt:
    event_id: str
    status: int
    body_sha256: str
    response_preview: str
    delivered_at: float

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "status": self.status,
                "body_sha256": self.body_sha256,
                "response_preview": self.response_preview,
                "delivered_at": self.delivered_at}


class SignedWebhookClient:
    """Deliver one idempotent event without exposing the signing secret."""

    def __init__(self, *, url: str, secret: bytes | bytearray,
                 allowed_hosts: frozenset[str], timeout_s: float = 15.0,
                 opener: Callable[..., Any] | None = None):
        normalized_hosts = frozenset(
            str(item).encode("idna").decode("ascii").lower().rstrip(".")
            for item in allowed_hosts)
        self.url = _validate_target(url, normalized_hosts)
        self.allowed_hosts = normalized_hosts
        self._secret = _secret(secret)
        if isinstance(timeout_s, bool):
            raise ValueError("webhook timeout_s must be between 0.1 and 60")
        self.timeout_s = float(timeout_s)
        if not (self.timeout_s == self.timeout_s and
                .1 <= self.timeout_s <= 60.0):
            raise ValueError("webhook timeout_s must be between 0.1 and 60")
        self._open = opener

    def _open_pinned(self, request: Request) -> Any:
        parsed = urlsplit(self.url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        addresses = _public_addresses(host, port)
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = dict(request.header_items())
        last_error: Exception | None = None
        for address in addresses:
            connection = _PinnedHttpsConnection(host, port, address, self.timeout_s)
            try:
                connection.request("POST", path, body=request.data, headers=headers)
                response = connection.getresponse()
                # Keep the connection alive until the caller has read the bounded
                # response; attach it so close() remains reachable.
                response._collie_connection = connection
                return response
            except Exception as exc:
                last_error = exc
                connection.close()
        raise OutboundWebhookError("outbound webhook TLS connection failed") from last_error

    def deliver(self, event: str, payload: dict[str, Any], *,
                event_id: str = "", timestamp: int | None = None,
                nonce: str = "") -> DeliveryReceipt:
        event = str(event or "").strip()
        if not event or len(event) > 120:
            raise ValueError("webhook event name must be 1..120 characters")
        event_id = str(event_id or secrets.token_urlsafe(18))[:200]
        timestamp = int(time.time()) if timestamp is None else int(timestamp)
        nonce = str(nonce or secrets.token_hex(16))[:128]
        body = _body({"event": event, "event_id": event_id,
                      "payload": payload, "sent_at": timestamp})
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Collie-Webhook/1",
            "Idempotency-Key": event_id,
            "X-Collie-Timestamp": str(timestamp),
            "X-Collie-Nonce": nonce,
            "X-Collie-Signature": signature(self._secret, body, timestamp, nonce),
        }
        request = Request(self.url, data=body, headers=headers, method="POST")
        # Resolve immediately before connection as well as at construction. The
        # default connection is pinned to one of these vetted addresses, which
        # closes the DNS-rebinding gap between policy check and socket connect.
        _validate_target(self.url, self.allowed_hosts)
        response = None
        try:
            response = (self._open(request, timeout=self.timeout_s)
                        if self._open is not None else self._open_pinned(request))
            status = int(getattr(response, "status", 0) or response.getcode())
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except OutboundWebhookError:
            raise
        except Exception as exc:
            raise OutboundWebhookError("outbound webhook delivery failed: %s" %
                                       type(exc).__name__) from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
                connection = getattr(response, "_collie_connection", None)
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
        if len(raw) > MAX_RESPONSE_BYTES:
            raw = raw[:MAX_RESPONSE_BYTES]
        if status < 200 or status >= 300:
            raise OutboundWebhookError("outbound webhook returned status %d" % status)
        return DeliveryReceipt(
            event_id=event_id, status=status,
            body_sha256=hashlib.sha256(body).hexdigest(),
            response_preview=raw.decode("utf-8", "replace")[:2_000],
            delivered_at=time.time())


__all__ = ["DeliveryReceipt", "OutboundWebhookError", "SignedWebhookClient",
           "signature", "verify_signature"]
