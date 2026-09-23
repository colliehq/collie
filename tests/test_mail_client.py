import hashlib
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from harness import dogmail as mail


def identity(tmp_path, monkeypatch):
    mail.save({"handle": {"name": "owner", "verified": True, "priv": mail.b64(b"handle")},
               "dogs": {"collie": {"address": "collie.owner@example.test", "priv": mail.b64(b"private")}}}, tmp_path)
    monkeypatch.setattr(mail, "relay_public", lambda *a, **k: b"public")
    monkeypatch.setattr(mail, "_signed_headers", lambda *a: {"x-test-path": a[2]})


def test_claim_retry_keeps_pending_key_after_response_loss(tmp_path, monkeypatch):
    identity(tmp_path, monkeypatch)
    monkeypatch.setattr(mail.e2e, "keypair", lambda: (b"new-private", b"new-public"))
    monkeypatch.setattr(mail, "cert_tag", lambda *a: b"tag")
    payloads = []
    def post(path, payload, **kwargs):
        payloads.append(payload)
        if len(payloads) == 1:
            raise TimeoutError("relay accepted, response lost")
        return {"ok": True}
    monkeypatch.setattr(mail, "_post", post)
    with pytest.raises(TimeoutError):
        mail.claim_dog("new", state_dir=tmp_path)
    pending = mail.load(tmp_path)["dogs"]["new"]
    assert pending["pending"] and pending["priv"] == mail.b64(b"new-private")
    assert mail._dog("new", state_dir=tmp_path) == {}
    monkeypatch.setattr(mail.e2e, "keypair", lambda: pytest.fail("must reuse reserved key"))
    assert mail.claim_dog("new", state_dir=tmp_path)["address"] == "new.owner@collie.run"
    assert payloads[0] == payloads[1]
    assert not mail.load(tmp_path)["dogs"]["new"].get("pending")


def test_resending_handle_code_does_not_replace_private_key(tmp_path, monkeypatch):
    payloads = []
    monkeypatch.setattr(mail, "_post", lambda path, payload, **kw: payloads.append(payload) or {"ok": True})
    mail.claim_handle("owner", "owner@example.test", state_dir=tmp_path)
    key = mail.load(tmp_path)["handle"]["priv"]
    mail.claim_handle("owner", "owner@example.test", state_dir=tmp_path)
    assert mail.load(tmp_path)["handle"]["priv"] == key
    assert payloads[0] == payloads[1]


def test_send_digest_matches_exact_utf8_wire_and_success_is_submission(tmp_path, monkeypatch):
    identity(tmp_path, monkeypatch)
    def request(path, *, body=None, headers=None, relay=""):
        assert path == "/send?sha256=" + hashlib.sha256(body).hexdigest()
        assert headers["x-test-path"] == path
        assert json.loads(body)["text"] == "明天继续。"
        return {"ok": True, "status": "sent", "receipt": "<receipt@example.test>"}
    monkeypatch.setattr(mail, "_request", request)
    result = mail.send("collie", {"id": "reply-12345", "destination": "owner@example.test", "text": "明天继续。"}, state_dir=tmp_path)
    assert result["status"] == "submitted" and result["provider_message_id"]
    assert "delivered" not in result


@pytest.mark.parametrize("response,unknown", [
    ({"ok": False, "status": "unknown"}, True),
    ({"ok": False, "status": "failed", "http_status": 502}, False),
    ({"ok": False, "http_status": 403}, False),
    ({"ok": False, "http_status": 503}, True),
    ({"ok": True, "status": "sent", "receipt": ""}, True),
])
def test_receipt_never_guesses_success(response, unknown):
    with pytest.raises(mail.MailRelayError) as caught:
        mail._receipt(response)
    assert caught.value.delivery_unknown is unknown


def test_page_resume_preserves_cursor_and_decryption_failure(tmp_path, monkeypatch):
    identity(tmp_path, monkeypatch)
    monkeypatch.setattr(mail, "open_from_relay", lambda key, env: json.dumps(env).encode())
    paths = []
    def get(path, **kwargs):
        paths.append(path)
        return {"ok": True, "messages": [{"id": "old", "at": 9, "env": {"text": "old"}},
                {"id": "new", "at": 12, "env": {"text": "new"}}], "more": True, "next_cursor": "next-page"}
    monkeypatch.setattr(mail, "_get", get)
    result = mail.fetch_page("collie", cursor={"page": "page-one", "since": 10}, state_dir=tmp_path)
    assert [m["id"] for m in result["messages"]] == ["new"]
    assert result["cursor"] == {"page": "next-page", "since": 10}
    assert paths == ["/mail-page?cursor=page-one"]
    assert mail.load(tmp_path)["dogs"]["collie"].get("cursor") is None
    monkeypatch.setattr(mail, "open_from_relay", lambda *args: b"invalid")
    assert mail.fetch_page("collie", state_dir=tmp_path)["messages"][0]["error"]


def test_page_without_progress_does_not_consume_messages(tmp_path, monkeypatch):
    identity(tmp_path, monkeypatch)
    monkeypatch.setattr(mail, "_get", lambda *a, **k: {"ok": True, "messages": [], "more": True, "next_cursor": "same"})
    with pytest.raises(mail.MailRelayError, match="cursor was kept"):
        mail.fetch_page("collie", cursor={"page": "same"}, state_dir=tmp_path)


def test_http_failure_hides_provider_body_and_rejects_redirects(monkeypatch):
    error = urllib.error.HTTPError("https://example.test", 403, "forbidden", {}, io.BytesIO(b'{"error":"private credential"}'))
    def opening(*args, **kwargs):
        raise error
    monkeypatch.setattr(mail.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=opening))
    result = mail._get("/mail", relay="https://example.test")
    assert result["http_status"] == 403 and "private credential" not in json.dumps(result)
    assert mail._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test") is None


@pytest.mark.parametrize("origin", ["http://example.test", "https://user:password@example.test", "https://example.test/?token=secret"])
def test_relay_origin_refuses_unprotected_or_credential_urls(origin):
    with pytest.raises(ValueError):
        mail._relay_url(origin)


def test_lost_verification_response_recovers_same_reserved_identity(tmp_path, monkeypatch):
    calls = []
    def claim(path, body, **kwargs):
        calls.append(body)
        return {"ok": True, "verified": len(calls) > 1, "sent": len(calls) == 1}
    monkeypatch.setattr(mail, "_post", claim)
    mail.claim_handle("owner", "owner@example.test", state_dir=tmp_path)
    pending = mail.load(tmp_path)["handle"]
    assert pending["verified"] is False
    result = mail.claim_handle("owner", "owner@example.test", state_dir=tmp_path)
    recovered = mail.load(tmp_path)["handle"]
    assert result["verified"] and recovered["verified"]
    assert recovered["priv"] == pending["priv"] and calls[0] == calls[1]


def test_rate_limit_guidance_preserves_only_bounded_numeric_fields(monkeypatch):
    payload = {"error": "private body", "retry_after": 60, "attempts_left": "private body"}
    def opening(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.test", 429, "limited", {},
                                     io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(mail.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=opening))
    result = mail._get("/mail", relay="https://example.test")
    assert result["retry_after"] == 60 and "60 seconds" in result["error"]
    assert "private body" not in json.dumps(result) and "attempts_left" not in result
