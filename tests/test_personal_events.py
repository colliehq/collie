import json
import time

import pytest

from harness.personal_events import PersonalEventStore


def _connected(store):
    return store.configure_source(
        "browser_history", enabled=True, permission_state="granted",
        scopes=["origin", "time_bucket", "count"], consent_version="v1",
        confirmed=True)


def _email_connected(store):
    return store.configure_source(
        "primary_email", kind="email", enabled=True, permission_state="granted",
        scopes=["orders", "calendar"], consent_version="v1", confirmed=True)


def test_history_is_reduced_to_bounded_origin_summaries_without_raw_urls(tmp_path):
    now = int(time.time())
    path = tmp_path / "personal.db"
    with PersonalEventStore(str(path)) as store:
        _connected(store)
        result = store.ingest_browser_history([
            {"url": "https://shop.example/private/order/123?token=secret",
             "lastVisitTime": now * 1000, "visit_count": 4, "typed_count": 1},
            {"origin": "https://docs.example", "last_visit_at": now,
             "visit_count": 2, "typed_count": 0},
            {"origin": "https://login.bank.example", "last_visit_at": now,
             "visit_count": 100, "typed_count": 1},
        ], now=now)
        patterns = store.browsing_patterns()
        wire = json.dumps(store.snapshot())
    assert result["raw_stored"] is False and result["raw_uploaded"] is False
    assert {row["origin"] for row in patterns} == {
        "https://shop.example", "https://docs.example"}
    assert "order/123" not in wire and "token=secret" not in wire
    durable = b"".join(candidate.read_bytes() for candidate in tmp_path.iterdir()
                       if candidate.is_file())
    assert b"order/123" not in durable and b"token=secret" not in durable


def test_history_cannot_claim_a_purchase_and_events_need_evidence(tmp_path):
    with PersonalEventStore(str(tmp_path / "personal.db")) as store:
        _connected(store)
        with pytest.raises(ValueError, match="not evidence"):
            store.upsert_event({"event_type": "order_shipment", "title": "A parcel",
                                "source_kind": "browser_history", "confidence": 0.9})
        _email_connected(store)
        with pytest.raises(ValueError, match="source evidence"):
            store.upsert_event({"event_type": "order_shipment", "title": "A parcel",
                                "source_id": "primary_email", "source_kind": "email",
                                "confidence": 0.9})
        with pytest.raises(ValueError, match="hexadecimal digest"):
            store.upsert_event({"event_type": "order_shipment", "title": "A parcel",
                                "source_id": "primary_email", "source_kind": "email",
                                "source_ref": "message-1", "evidence_digest": "raw email body",
                                "confidence": 0.9})


def test_evidenced_shipment_becomes_notify_only_delay_reminder(tmp_path):
    now = int(time.time())
    with PersonalEventStore(str(tmp_path / "personal.db")) as store:
        _email_connected(store)
        event = store.upsert_event({
            "event_type": "order_shipment", "title": "Camera delivery",
            "status": "shipped", "expected_at": now - 60,
            "source_id": "primary_email", "source_kind": "email",
            "source_ref": "message-42",
            "evidence_digest": "a" * 64, "confidence": 0.94,
            "details": {"carrier": "Example Express", "private_body": "never"},
        })
        reminders = store.reminders(now=now)
    assert event["source_ref_digest"] != "message-42"
    assert event["details"] == {"carrier": "Example Express"}
    assert reminders[0]["state"] == "possibly_delayed"
    assert reminders[0]["authority_scope"] == "notify_only"


def test_disconnect_can_delete_all_browser_summaries(tmp_path):
    now = int(time.time())
    with PersonalEventStore(str(tmp_path / "personal.db")) as store:
        _connected(store)
        store.ingest_browser_history([
            {"origin": "https://example.test", "last_visit_at": now}], now=now)
        source = store.configure_source(
            "browser_history", enabled=False, permission_state="revoked", purge=True)
        assert source["enabled"] is False
        assert store.browsing_patterns() == []


def test_native_personal_reminder_is_idempotent_and_has_no_action_authority(tmp_path,
                                                                            monkeypatch):
    from harness import native_notifications
    from harness.native_notifications import PersonalReminderService
    from harness.procedure_memory import ProcedureMemory

    now = int(time.time())
    with ProcedureMemory(str(tmp_path / "procedural-memory.db")) as procedures:
        procedures.update_privacy(observation_mode="personal", consent=True)
    store = PersonalEventStore(str(tmp_path / "personal-intelligence.db"))
    _email_connected(store)
    store.upsert_event({
        "event_type": "bill_due", "title": "Utility bill", "status": "due",
        "due_at": now - 1, "source_id": "primary_email",
        "source_kind": "email", "source_ref": "message-7",
        "evidence_digest": "b" * 64, "confidence": 0.9,
    })
    seen = []
    monkeypatch.setattr(native_notifications, "notify", lambda title, body:
                        seen.append((title, body)) or
                        {"ok": True, "backend": "test", "error": ""})
    service = PersonalReminderService(store, interval=60)
    try:
        first = service.tick(now=now)
        second = service.tick(now=now + 1)
        assert len(first) == 1 and second == []
        assert seen == [("Collie reminder", "Utility bill is overdue.")]
        assert store.reminders(now=now)[0]["authority_scope"] == "notify_only"
    finally:
        store.close()
