import base64
import json
import os
import pytest

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


def test_notification_health_uses_backlog_age_and_dead_retry_is_explicit(tmp_path):
    with OpsStore(str(tmp_path / "ops.db")) as store:
        nid = store.enqueue("notice", "old", "private body", now=10)
        health = store.notification_health(now=400, stale_after_s=300)
        assert health["live"] == health["due"] == 1
        assert health["oldest_pending_age_s"] == 390
        assert health["stale"] is True
        item = store.claim(now=400)[0]
        assert item["notification_id"] == nid
        assert store.failed(nid, "private transport detail", max_attempts=1, now=400) == "dead"
        assert store.retry_dead(nid, now=401) == 1
        row = store.db.execute(
            "SELECT state,attempts,last_error FROM notifications WHERE notification_id=?", (nid,)
        ).fetchone()
        assert dict(row) == {"state": "pending", "attempts": 0, "last_error": ""}


def test_unresolved_dedupe_key_coalesces_for_entire_incident(tmp_path):
    with OpsStore(str(tmp_path / "ops.db")) as store:
        first = store.enqueue(
            "worker_dead", "old title", "old detail", dedupe_key="worker-dead:web",
            cooldown_s=1, now=10)
        second = store.enqueue(
            "worker_dead", "current title", "current detail", severity="error",
            payload={"worker": "web"}, dedupe_key="worker-dead:web",
            cooldown_s=1, now=10_000)
        assert second == first
        row = store.db.execute(
            "SELECT count(*) AS n,title,body,severity,payload_json FROM notifications "
            "WHERE dedupe_key='worker-dead:web'").fetchone()
        assert row["n"] == 1
        assert (row["title"], row["body"], row["severity"]) == (
            "current title", "current detail", "error")
        assert json.loads(row["payload_json"]) == {"worker": "web"}


def test_health_alerts_never_alert_about_their_own_backlog(tmp_path):
    report = {
        "workers": {"web": {"fresh": False, "state": "dead"}},
        "services": {}, "credentials": [],
        "queues": {"slack": {}, "notifications": {
            "stale": True, "live": 238, "dead": 5,
            "oldest_pending_age_s": 86_400,
        }},
    }
    with OpsStore(str(tmp_path / "ops.db")) as store:
        enqueue_health_alerts(store, report, now=10)
        enqueue_health_alerts(store, report, now=100_000)
        rows = list(store.db.execute(
            "SELECT kind,dedupe_key FROM notifications ORDER BY created_at"))
        assert [row["kind"] for row in rows] == ["worker_dead"]
        assert rows[0]["dedupe_key"] == "worker-dead:web"


def test_notification_maintenance_dismisses_disabled_health_and_bounds_history(tmp_path):
    with OpsStore(str(tmp_path / "ops.db")) as store:
        for now in (20, 30, 40):
            nid = store.enqueue(
                "completion", "done", "detail", dedupe_key="same-completion",
                cooldown_s=0, now=now)
            assert store.claim(limit=1, now=now)[0]["notification_id"] == nid
            assert store.delivered(nid, now=now)
        store.enqueue(
            "worker_dead", "health", "detail", dedupe_key="worker-dead:web", now=41)
        explicit = store.enqueue(
            "automation_needs_you", "explicit", "detail",
            dedupe_key="automation:run-1:needs_you", now=42)

        result = store.maintain_notifications(
            delivery_enabled=False, now=50, delivered_retention_s=1_000,
            keep_delivered=50)
        assert result["health_dismissed"] == 1
        assert result["delivered_pruned"] == 2
        assert store.db.execute(
            "SELECT count(*) FROM notifications WHERE state='delivered'").fetchone()[0] == 1
        pending = list(store.db.execute(
            "SELECT notification_id,kind FROM notifications WHERE state='pending'"))
        assert [(row["notification_id"], row["kind"]) for row in pending] == [
            (explicit, "automation_needs_you")]


def test_stale_backlog_and_pump_make_health_degraded(tmp_path):
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.enqueue("notice", "old", "private body", now=10)
        store.beat("notification-pump", "running", ttl=20, now=10)
        report = aggregate_health(store, now=400, probe_services=False)
        assert report["status"] == "degraded"
        assert report["issues"] == ["notification_delivery_stalled"]
        assert "private body" not in json.dumps(report)


def test_outbox_strict_json_and_corrupt_payload_are_fail_closed(tmp_path):
    with OpsStore(str(tmp_path / "ops.db")) as store:
        with pytest.raises((TypeError, ValueError), match="compliant|Out of range"):
            store.enqueue("notice", "bad", "body", payload={"usage": float("nan")})
        with pytest.raises(ValueError, match="payload must be an object"):
            store.enqueue("notice", "bad", "body", payload=[])
        with pytest.raises(ValueError, match="claim limit"):
            store.claim(limit=True)

        nid = store.enqueue("notice", "corrupt", "must not send", payload={"ok": True},
                            now=10)
        store.db.execute(
            "UPDATE notifications SET payload_json=? WHERE notification_id=?",
            ('{"authority":NaN}', nid))
        store.db.commit()
        sent = []
        assert store.deliver_once(lambda item: sent.append(item) or True, now=10) == {
            "sent": 0, "retried": 0, "dead": 0}
        row = store.db.execute(
            "SELECT state,last_error FROM notifications WHERE notification_id=?", (nid,)
        ).fetchone()
        assert row["state"] == "dead"
        assert "invalid durable notification payload" in row["last_error"]
        assert sent == []


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


# --- only the login the configured provider reads can make Collie unhealthy -------------------

def _creds(tmp_path, *, claude=None, codex=None):
    claude_path, codex_path = tmp_path / "claude.json", tmp_path / "codex.json"
    if claude is not None:
        claude_path.write_text(json.dumps({"claudeAiOauth": claude}), encoding="utf-8")
    if codex is not None:
        codex_path.write_text(json.dumps({"tokens": codex}), encoding="utf-8")
    return str(claude_path), str(codex_path)


@pytest.mark.parametrize("provider,needed", [
    ("codex-oauth", {"codex-oauth"}), ("anthropic-oauth", {"claude-oauth"}),
    ("claude-agent-sdk", set()), ("claude-cli", set()), ("anthropic", set()), ("", set()),
])
def test_credentials_are_marked_needed_only_for_the_provider_that_reads_them(tmp_path, provider, needed):
    claude, codex = _creds(tmp_path)
    rows = credential_health(now=1000, claude_path=claude, codex_path=codex, provider=provider)
    assert {row["name"] for row in rows} == {"claude-oauth", "codex-oauth"}   # both still listed
    assert {row["name"] for row in rows if row["needed"]} == needed


def test_an_unused_missing_login_neither_degrades_health_nor_raises_an_alert(tmp_path, monkeypatch):
    import functools
    from harness import ops
    claude, codex = _creds(tmp_path, codex={"access_token": _jwt(10_000), "refresh_token": "r"})
    monkeypatch.setenv("COLLIE_PROVIDER", "codex-oauth")
    monkeypatch.setattr(ops, "credential_health", functools.partial(
        ops.credential_health, claude_path=claude, codex_path=codex))
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.beat("worker:web", "running", {}, ttl=10, now=100)
        report = aggregate_health(store, desired_workers=["web"], state_dir=str(tmp_path),
                                  now=105, probe_services=False)
        assert report["ok"] is True and report["status"] == "ok"
        assert any(row["name"] == "claude-oauth" and row["state"] == "missing"
                   for row in report["credentials"])
        enqueue_health_alerts(store, report, now=105)
        kinds = [row["kind"] for row in store.db.execute("SELECT kind FROM notifications")]
        assert "credential_expiry" not in kinds


def test_the_login_the_provider_reads_still_degrades_and_alerts_when_missing(tmp_path, monkeypatch):
    import functools
    from harness import ops
    claude, codex = _creds(tmp_path)
    monkeypatch.setenv("COLLIE_PROVIDER", "codex-oauth")
    monkeypatch.setattr(ops, "credential_health", functools.partial(
        ops.credential_health, claude_path=claude, codex_path=codex))
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.beat("worker:web", "running", {}, ttl=10, now=100)
        report = aggregate_health(store, desired_workers=["web"], state_dir=str(tmp_path),
                                  now=105, probe_services=False)
        assert report["ok"] is False and report["status"] == "degraded"
        enqueue_health_alerts(store, report, now=105)
        rows = list(store.db.execute("SELECT kind, body FROM notifications"))
        assert [row[1] for row in rows if row[0] == "credential_expiry"] == ["codex-oauth is missing"]
