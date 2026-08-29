import copy
import time
import uuid

import pytest

from harness.online import OnlineClient, OnlineError, OnlineStore, generate_device_key
from harness.online_node import MissionLease, OnlineNode, detect_capabilities


def connected(store):
    store.connect(base_url="https://api.collie.test", user_id="u", workspace_id="w",
                  device_id="d", device_name="Laptop", access_token="a", refresh_token="r",
                  access_expires_at=9_999_999_999, refresh_expires_at=9_999_999_999)


def authorized_mission(node, *, mission_id="m1", goal="do it", payload=None,
                       issuer_device_id="d", target_device_id="d"):
    now = int(time.time())
    values = {"goal": goal, "payload": payload or {}, "required_capabilities": [],
              "fallback": "wait", "data_class": "cloud_indexed", "cloud_budget_tokens": 0}
    intent = {"version": 1, "kind": "mission", "authorization_id": uuid.uuid4().hex,
              "mission_id": mission_id, "workspace_id": "w", "project_id": "p",
              "target_device_id": target_device_id, **values,
              "issuer_device_id": issuer_device_id, "issued_at": now, "expires_at": now + 3600}
    return {"id": mission_id, "project_id": "p", **values,
            "authorization": node._sign(intent), "lease_id": "lease", "fencing_token": 4}


def test_commit_key_survives_a_reissued_fencing_token(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    node = OnlineNode(store, capabilities=["test"])
    first = MissionLease(node, {"id": "m1", "lease_id": "l1", "fencing_token": 1})
    second = MissionLease(node, {"id": "m1", "lease_id": "l2", "fencing_token": 2})
    assert node.node_id == "w:d"
    assert first.commit_key("send-report") == second.commit_key("send-report")
    assert first.commit_key("send-report") != first.commit_key("publish-report")
    store.close()


def test_run_once_checkpoints_then_completes_under_same_lease(tmp_path, monkeypatch):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    node = OnlineNode(store, capabilities=["test"])
    mission = authorized_mission(node)
    calls = []
    def request(self, method, path, body=None, authenticated=True):
        calls.append((path, body))
        if path.endswith("heartbeat"):
            return {"node": {"id": "d"}}
        if path.endswith("claim"):
            return {"mission": mission}
        if path.endswith("checkpoint"):
            return {"mission": {"id": "m1", "lease_id": "lease", "fencing_token": 4}}
        if path.endswith("complete"):
            return {"mission": {"id": "m1", "state": "completed"}}
        raise AssertionError(path)
    monkeypatch.setattr(OnlineClient, "_request", request)
    result = node.run_once(lambda mission, lease: {"key": lease.commit_key("step")})
    assert result["state"] == "completed"
    assert [x[0].rsplit("/", 1)[-1] for x in calls] == ["heartbeat", "claim", "checkpoint", "complete"]
    assert calls[-1][1]["fencing_token"] == 4
    store.close()


def test_endpoint_authorization_rejects_unsigned_tampered_and_replayed_work(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    node = OnlineNode(store)
    with pytest.raises(OnlineError, match="no endpoint authorization"):
        node._verify_authorization({"id": "unsigned", "project_id": "p", "goal": "do it",
                                    "payload": {}, "required_capabilities": [], "fallback": "wait",
                                    "data_class": "cloud_indexed", "cloud_budget_tokens": 0})
    tampered = authorized_mission(node, mission_id="tampered")
    tampered["goal"] = "run attacker command"
    with pytest.raises(OnlineError, match="changed after endpoint authorization"):
        node._verify_authorization(tampered)
    wrong_endpoint = authorized_mission(node, mission_id="wrong-endpoint", target_device_id="other")
    with pytest.raises(OnlineError, match="targets another execution endpoint"):
        node._verify_authorization(wrong_endpoint)
    accepted = authorized_mission(node, mission_id="once")
    node._verify_authorization(accepted)
    with pytest.raises(OnlineError, match="already consumed"):
        node._verify_authorization(accepted)
    store.close()


def test_cross_device_handoff_requires_an_out_of_band_local_trust_pin(tmp_path):
    receiving_store = OnlineStore(str(tmp_path / "receiving" / "online.db")); connected(receiving_store)
    issuer_store = OnlineStore(str(tmp_path / "issuer" / "online.db"))
    issuer_store.connect(base_url="https://api.collie.test", user_id="u", workspace_id="w",
                         device_id="other", device_name="Phone", access_token="a", refresh_token="r",
                         access_expires_at=9_999_999_999, refresh_expires_at=9_999_999_999)
    receiver, issuer = OnlineNode(receiving_store), OnlineNode(issuer_store)
    mission = authorized_mission(issuer, mission_id="from-phone", issuer_device_id="other")
    with pytest.raises(OnlineError, match="not trusted"):
        receiver._verify_authorization(mission)
    key = generate_device_key(str(tmp_path / "issuer" / "online-device-key.json"))
    receiver.trust_device("other", key["public_key"], name="Phone")
    receiver._verify_authorization(copy.deepcopy(mission))
    assert any(row["device_id"] == "other" and row["source"] == "local_pin"
               for row in receiver.trusted_devices())
    issuer_store.close(); receiving_store.close()


def test_signed_schedule_only_accepts_aligned_fresh_occurrences(tmp_path, monkeypatch):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    node = OnlineNode(store)
    captured = {}
    def request(self, method, path, body=None, authenticated=True):
        captured.update(body or {})
        return {"schedule": {"id": body["schedule_id"], "next_run_at": body["next_run_at"]}}
    monkeypatch.setattr(OnlineClient, "_request", request)
    start = int(time.time())
    node.schedule(project_id="p", name="Daily report", next_run_at=start,
                  cadence="daily", goal="write report")
    occurrence = {"id": "%s:%s" % (captured["schedule_id"], start),
                  "schedule_id": captured["schedule_id"], "scheduled_for": start,
                  "project_id": "p", **captured["mission"],
                  "authorization": captured["authorization"]}
    node._verify_authorization(occurrence)
    forged = copy.deepcopy(occurrence)
    forged["id"] = "%s:%s" % (captured["schedule_id"], start + 1)
    forged["scheduled_for"] = start + 1
    with pytest.raises(OnlineError, match="outside its signed bounds"):
        node._verify_authorization(forged)
    store.close()


def test_lost_lease_never_reports_completion(tmp_path, monkeypatch):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    node = OnlineNode(store)
    lease = MissionLease(node, {"id": "m1", "lease_id": "l", "fencing_token": 1},
                         lost=True, last_error="stale")
    try:
        lease.finish({"ok": True})
        assert False
    except Exception as exc:
        assert "refusing completion" in str(exc)
    store.close()


def test_detected_capabilities_are_stable_and_include_local_runtime():
    caps = detect_capabilities(["custom", "custom"])
    assert caps == sorted(set(caps)) and "collie" in caps and "custom" in caps
