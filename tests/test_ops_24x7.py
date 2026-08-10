import base64
import json
import os

from harness.ops import (NotificationPump, OpsStore, OutboxFull, RotatingLog,
                         aggregate_health, credential_health, enqueue_health_alerts,
                         remote_notification_sender)


def _jwt(exp):
    enc = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return "%s.%s.x" % (enc({"alg": "none"}), enc({"exp": exp}))


def test_heartbeats_and_aggregate_health_are_safe(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "queue-dog.json").write_text(json.dumps({
        "items": [{"state": "waiting", "text": "secret task", "user": "U1"}],
        "next_id": 2, "receipts": [], "dead_letters": [{"text": "dead secret"}],
    }), encoding="utf-8")
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.beat("worker:web", "running", {"mode": "test"}, ttl=10, now=100)
        report = aggregate_health(store, desired_workers=["web", "jobd"],
                                  state_dir=str(state), now=105, probe_services=False)
        assert report["workers"]["web"]["fresh"] is True
        assert report["workers"]["jobd"]["state"] == "missing"
        assert report["queues"]["slack"]["waiting"] == 1
        assert report["queues"]["slack"]["dead_letters"] == 1
        assert "secret task" not in json.dumps(report)
        queued = enqueue_health_alerts(store, report, backlog_warning=1, now=105)
        assert queued
        kinds = {row["kind"] for row in store.db.execute(
            "SELECT kind FROM notifications")}
        assert {"worker_dead", "queue_backlog", "dead_letters"} <= kinds


def test_outbox_retry_lease_dead_letter_capacity_and_pump(tmp_path):
    with OpsStore(str(tmp_path / "ops.db"), outbox_cap=1, dead_letter_cap=2) as store:
        nid = store.enqueue("notice", "hello", "body", now=10)
        first = store.claim(now=10, lease_s=2)
        assert first[0]["notification_id"] == nid and first[0]["attempts"] == 1
        # A crashed sender's expired lease is reclaimed rather than left delivering forever.
        second = store.claim(now=13, lease_s=2)
        assert second[0]["attempts"] == 2
        assert store.failed(nid, "offline", max_attempts=2, now=13) == "dead"

        pending = store.enqueue("notice", "second", "body", now=14)
        # Live capacity overflow is represented as a dead letter, never reported delivered.
        overflow = store.enqueue("notice", "overflow", "body", now=15)
        rows = {r["notification_id"]: r["state"] for r in store.db.execute(
            "SELECT notification_id,state FROM notifications")}
        assert rows[pending] == "pending" and rows[overflow] == "dead"
        try:
            store.enqueue("notice", "no room", "body", now=16)
        except OutboxFull:
            pass
        else:
            raise AssertionError("full outbox + DLQ must fail visibly")

        pump = NotificationPump(store, lambda item: item["notification_id"] == pending)
        assert pump.step()["sent"] == 1
        assert store.notification_stats()["delivered"] == 1


def test_credential_health_exposes_metadata_not_tokens(tmp_path):
    claude = tmp_path / "claude.json"
    codex = tmp_path / "codex.json"
    claude.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "claude-secret", "refreshToken": "claude-refresh",
        "expiresAt": 1_010_000,
    }}), encoding="utf-8")
    codex.write_text(json.dumps({"tokens": {
        "access_token": _jwt(900), "refresh_token": "codex-refresh",
    }}), encoding="utf-8")
    result = credential_health(now=1000, claude_path=str(claude), codex_path=str(codex))
    wire = json.dumps(result)
    assert all(value not in wire for value in (
        "claude-secret", "claude-refresh", "codex-refresh", _jwt(900)))
    assert result[0]["state"] == "expiring"
    assert result[0]["refresh_owner"] == "claude-code"
    assert result[1]["refresh_owner"] == "collie-codex-owner"


def test_rotating_log_is_bounded(tmp_path):
    path = tmp_path / "worker.log"
    log = RotatingLog(str(path), max_bytes=1024, backups=2)
    for _ in range(30):
        log.write("x" * 100)
    log.close()
    assert path.exists() and (tmp_path / "worker.log.1").exists()
    assert len(list(tmp_path.glob("worker.log*"))) <= 3


def test_remote_notification_survives_disconnect_and_drains_after_reconnect(tmp_path):
    class Remote:
        connected = False
        delivered = []

        def notify(self, title, body, **metadata):
            if not self.connected:
                return False
            self.delivered.append((title, body, metadata))
            return True

    remote = Remote()
    with OpsStore(str(tmp_path / "ops.db")) as store:
        nid = store.enqueue("completion", "finished", "result ready", now=10)
        first = store.deliver_once(remote_notification_sender(remote), now=10)
        assert first == {"sent": 0, "retried": 1, "dead": 0}
        assert store.db.execute(
            "SELECT state FROM notifications WHERE notification_id=?", (nid,)
        ).fetchone()["state"] == "pending"
        remote.connected = True
        second = store.deliver_once(remote_notification_sender(remote), now=16)
        assert second["sent"] == 1 and remote.delivered
        assert store.db.execute(
            "SELECT state FROM notifications WHERE notification_id=?", (nid,)
        ).fetchone()["state"] == "delivered"
