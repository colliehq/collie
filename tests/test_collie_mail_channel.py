import pytest

from harness import communications as comms, dogmail
from harness.channel_service import ChannelService


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    dogmail.save({"handle": {"name": "owner", "verified": True}, "dogs": {
        "collie": {"address": "collie.owner@collie.run", "priv": "fixture-key"}}}, tmp_path)
    service = ChannelService(tmp_path)
    service.configure("relay", kind="collie_mail", config={"mailbox": "collie"}, owner="owner@example.test")
    return service


def test_new_only_anchor_then_replayable_pages_are_durable(host, monkeypatch):
    calls = []
    monkeypatch.setattr(dogmail, "anchor", lambda *a, **k: {"cursor": {"page": "", "since": 10}, "note": "From now"})
    def page(*a, cursor=None, **k):
        calls.append(cursor)
        return {"messages": [{"id": "incoming", "at": 12, "from": "owner@example.test",
                              "to": "collie.owner@collie.run", "text": "Please continue", "subject": "Request"}],
                "cursor": {"page": "next", "since": 10}, "more": True}
    monkeypatch.setattr(dogmail, "fetch_page", page)
    assert host.poll("relay")["anchored"]
    assert not calls
    assert host.poll("relay")["received"] == 1
    assert calls == [{"page": "", "since": 10}]
    assert host.poll("relay")["received"] == 0
    assert len(host.events("relay")) == 1
    assert host._row("relay")["cursor"]["page"] == "next"


def test_relay_response_loss_is_resolved_by_reading_receipt_without_resend(host, monkeypatch):
    sends = []
    def send(name, payload, **kwargs):
        sends.append(payload["id"])
        raise dogmail.MailRelayError("network lost", delivery_unknown=True)
    monkeypatch.setattr(dogmail, "send", send)
    host.prepare_reply("relay", "reply-12345", text="Finished result")
    assert host.send("relay", "reply-12345")["state"] == "unknown"
    monkeypatch.setattr(dogmail, "send_status", lambda *a, **k: {"status": "submitted", "provider_message_id": "<actual@example.test>"})
    assert host.check_receipt("relay", "reply-12345")["status"] == "submitted"
    row = comms.get_result("relay", "reply-12345", include_private=True, directory=host.directory)
    assert row["state"] == "submitted" and not row["delivery_known"]
    assert row["outcome_detail"]["provider_message_id"] == "<actual@example.test>"
    assert sends == ["reply-12345"]


PAGE = '<p>Good morning!</p><img alt="" src="cid:collie-avatar">'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def html_reply(host, result_id="report-12345"):
    comms.create_result("relay", result_id, destination="owner@example.test",
                        text="Good morning!", subject="Two quick ones",
                        html=PAGE, inline=[{"cid": "collie-avatar", "filename": "collie.png",
                                            "content_type": "image/png", "data": PNG}],
                        metadata={"message_id": "<r@collie.run>", "auto_eligible": False},
                        directory=host.directory)
    return result_id


def relay(monkeypatch, *, caps):
    """A relay that answers /capabilities with ``caps`` and records every /send body."""
    import json as json_mod
    posted, asked = [], []

    def get(path, headers=None, relay=""):
        asked.append(path)
        return caps

    def request(name, method, path, *, payload=None, relay="", state_dir=None):
        body = json_mod.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        assert path == "/send?sha256=" + __import__("hashlib").sha256(body).hexdigest()
        posted.append(payload)
        return {"ok": True, "status": "sent", "receipt": "cf-%d" % len(posted)}

    monkeypatch.setattr(dogmail, "_get", get)
    monkeypatch.setattr(dogmail, "_mail_request", request)
    return posted, asked


NEW_RELAY = {"ok": True, "send": {"configured": True, "formats": ["text", "html"],
                                  "max_body_bytes": 655360}}
OLD_RELAY = {"ok": True, "send": {"configured": True, "max_body_bytes": 98304}}


def test_a_relay_that_carries_html_gets_the_page_and_the_dog(host, monkeypatch):
    posted, asked = relay(monkeypatch, caps=NEW_RELAY)
    result_id = html_reply(host)
    assert host.send("relay", result_id)["state"] == "submitted"
    assert asked == ["/capabilities"]
    body = posted[0]
    assert body["html"] == PAGE and body["text"] == "Good morning!"
    assert [part["cid"] for part in body["inline"]] == ["collie-avatar"]
    assert body["to"] == "owner@example.test" and "cc" not in body
    detail = comms.get_result("relay", result_id, include_private=True,
                              directory=host.directory)["outcome_detail"]
    assert "plain text" not in (detail.get("detail") or "")


def test_a_relay_that_cannot_carry_html_gets_the_text_and_the_reason_is_kept(host, monkeypatch):
    posted, _ = relay(monkeypatch, caps=OLD_RELAY)
    result_id = html_reply(host)
    assert host.send("relay", result_id)["state"] == "submitted"
    assert "html" not in posted[0] and "inline" not in posted[0]
    assert posted[0]["text"] == "Good morning!"
    row = comms.get_result("relay", result_id, include_private=True, directory=host.directory)
    assert "plain text" in row["outcome_detail"]["detail"]
    assert "does not carry HTML" in row["outcome_detail"]["detail"]


def test_a_page_too_big_for_the_relay_goes_as_text_and_says_so(host, monkeypatch):
    posted, _ = relay(monkeypatch, caps={"ok": True, "send": {"formats": ["text", "html"],
                                                              "max_body_bytes": 200}})
    result_id = html_reply(host)
    assert host.send("relay", result_id)["state"] == "submitted"
    assert "html" not in posted[0]
    row = comms.get_result("relay", result_id, include_private=True, directory=host.directory)
    assert "larger than" in row["outcome_detail"]["detail"]


def test_a_text_reply_is_the_same_request_it_always_was(host, monkeypatch):
    posted, asked = relay(monkeypatch, caps=NEW_RELAY)
    host.prepare_reply("relay", "reply-12345", text="Finished result")
    assert host.send("relay", "reply-12345")["state"] == "submitted"
    assert asked == []                                   # no capability question for plain text
    assert set(posted[0]) == {"id", "to", "text", "subject", "in_reply_to", "references"}


def test_probe_checks_authenticated_inbox_instead_of_cached_public_key(host, monkeypatch):
    monkeypatch.setattr(dogmail, "probe", lambda *a, **k: (_ for _ in ()).throw(dogmail.MailRelayError("mailbox unavailable")))
    with pytest.raises(dogmail.MailRelayError):
        host.probe("relay")
    assert host.connection("relay")["status"] != "connected"
