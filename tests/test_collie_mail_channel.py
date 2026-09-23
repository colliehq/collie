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


def test_probe_checks_authenticated_inbox_instead_of_cached_public_key(host, monkeypatch):
    monkeypatch.setattr(dogmail, "probe", lambda *a, **k: (_ for _ in ()).throw(dogmail.MailRelayError("mailbox unavailable")))
    with pytest.raises(dogmail.MailRelayError):
        host.probe("relay")
    assert host.connection("relay")["status"] != "connected"
