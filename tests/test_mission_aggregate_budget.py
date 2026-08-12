import sqlite3
import threading
import time

import pytest

from harness.actions import ActionStore
from harness.jobs import Capability, NEEDS_YOU
from harness.mission import MissionStore, StepTimedOut, create_mission, world_leash
from harness.mission import MissionDriver
from harness.verifier import VERIFIED, Verdict


def _create_child(store, mission_id, parent_id, leash):
    return create_mission(
        store, mission_id, mission_id, leash=leash,
        case={"_parent_mission_id": parent_id}, lane="specialist",
        external_run_id="run_" + mission_id)


@pytest.mark.parametrize("field", ["max_model_cost_usd", "spend_max_usd"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_world_leash_rejects_nonfinite_money_bounds(field, value):
    with pytest.raises(ValueError, match="finite"):
        world_leash(**{field: value})


@pytest.mark.parametrize(
    ("bounds", "root_charge", "child_charge", "reason"),
    [
        ({"max_model_tokens": 5}, {"input_tokens": 4}, {"cache_tokens": 1},
         "mission model-token budget exhausted"),
        ({"max_model_cost_usd": 0.5}, {"cost_usd": 0.4}, {"cost_usd": 0.1},
         "mission model-cost budget exhausted"),
        ({"max_active_wall_seconds": 1}, {"wall_ms": 900}, {"wall_ms": 100},
         "mission active wall-time budget exhausted"),
        ({"max_retries": 2}, {"retries": 1}, {"retries": 1},
         "mission retry budget exhausted"),
    ],
)
def test_root_budget_aggregates_own_and_descendant_runtime(
        tmp_path, bounds, root_charge, child_charge, reason):
    store = MissionStore(str(tmp_path / "missions.db"))
    leash = world_leash(max_storage_bytes=10_000_000, **bounds)
    create_mission(store, "root", "root", leash=leash)
    _create_child(store, "child", "root", leash)
    _create_child(store, "grandchild", "child", leash)
    root_token = store.claim_run("root")
    grandchild_token = store.claim_run("grandchild")

    assert store.account_runtime("root", root_token, **root_charge)
    assert store.account_runtime("grandchild", grandchild_token, **child_charge)
    aggregate = store.aggregate_runtime("root")
    assert aggregate["input_tokens"] == root_charge.get("input_tokens", 0)
    assert aggregate["cache_tokens"] == child_charge.get("cache_tokens", 0)
    assert aggregate["active_wall_ms"] == (
        root_charge.get("wall_ms", 0) + child_charge.get("wall_ms", 0))
    assert aggregate["retry_count"] == (
        root_charge.get("retries", 0) + child_charge.get("retries", 0))
    assert aggregate["model_cost_usd"] == pytest.approx(
        root_charge.get("cost_usd", 0) + child_charge.get("cost_usd", 0))
    assert store.aggregate_runtime("child")["model_cost_usd"] == pytest.approx(
        child_charge.get("cost_usd", 0))
    assert store.budget_reason("root") == reason
    assert store.budget_reason("grandchild") == "ancestor root: " + reason
    store.close()


def test_active_step_timeout_uses_tightest_ancestor_allowance(tmp_path):
    store = MissionStore(str(tmp_path / "active.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    root_leash = world_leash(
        max_active_wall_seconds=1, max_step_seconds=10,
        max_storage_bytes=10_000_000)
    child_leash = world_leash(
        max_active_wall_seconds=5, max_step_seconds=10,
        max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=root_leash)
    _create_child(store, "child", "root", child_leash)
    root_token = store.claim_run("root")
    child_token = store.claim_run("child")
    assert store.account_runtime("root", root_token, wall_ms=250)
    assert store.account_runtime("child", child_token, wall_ms=500)

    assert store.remaining_active_wall_seconds("child") == pytest.approx(0.25)
    driver = MissionDriver(store, actions, lambda *_args: {})
    assert driver._active_step_timeout("child", child_leash) == pytest.approx(0.25)
    actions.close()
    store.close()


def test_stale_worker_charges_unreported_inflight_time_before_safe_requeue(tmp_path):
    store = MissionStore(str(tmp_path / "stale-active.db"))
    leash = world_leash(
        max_active_wall_seconds=1, max_step_seconds=2,
        max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    token = store.claim_run("root")
    assert token
    with store._lock:
        store.db.execute(
            "UPDATE missions SET lease_until=101 WHERE mission_id='root'")
        store.db.execute(
            "UPDATE mission_runtime SET active_phase='deciding',active_since=100 "
            "WHERE mission_id='root'")
        store.db.commit()

    assert store.recover_stale_runs(now=102) == 1
    assert store.runtime("root")["active_wall_ms"] == 2000
    assert store.get("root").state == NEEDS_YOU
    assert "active wall-time budget exhausted" in store.get("root").result
    store.close()


def test_stale_worker_excludes_lease_grace_and_sleep_after_last_heartbeat(tmp_path):
    store = MissionStore(str(tmp_path / "stale-sleep.db"))
    leash = world_leash(
        max_active_wall_seconds=100, max_step_seconds=90,
        max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    token = store.claim_run("root")
    assert token
    with store._lock:
        store.db.execute(
            "UPDATE missions SET lease_until=200,updated_at=105 WHERE mission_id='root'")
        store.db.execute(
            "UPDATE mission_runtime SET active_phase='deciding',active_since=100 "
            "WHERE mission_id='root'")
        store.db.commit()

    assert store.recover_stale_runs(now=10_000) == 1
    # Five observed seconds plus at most one 20-second heartbeat interval;
    # the hours until recovery are not active work.
    assert store.runtime("root")["active_wall_ms"] == 25_000
    assert store.get("root").state == "queued"
    store.close()


def test_campaign_active_slot_is_atomic_across_sibling_missions(tmp_path):
    path = str(tmp_path / "active-slot.db")
    setup = MissionStore(path)
    leash = world_leash(max_active_wall_seconds=1,
                        max_storage_bytes=10_000_000)
    create_mission(setup, "root", "root", leash=leash)
    _create_child(setup, "left", "root", leash)
    _create_child(setup, "right", "root", leash)
    setup.close()

    left, right = MissionStore(path), MissionStore(path)
    tokens = {"left": left.claim_run("left"), "right": right.claim_run("right")}
    barrier = threading.Barrier(2)
    results = {}

    def claim(store, mission_id):
        barrier.wait()
        results[mission_id] = store.claim_active_slot(
            mission_id, tokens[mission_id], lease_s=5)

    threads = [threading.Thread(target=claim, args=(left, "left")),
               threading.Thread(target=claim, args=(right, "right"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    winners = [mid for mid, (_resource, token) in results.items() if token]
    assert len(winners) == 1
    loser = "right" if winners[0] == "left" else "left"
    assert results[loser] == (None, None)
    winner_store = left if winners[0] == "left" else right
    resource, active_token = results[winners[0]]
    assert winner_store.release_resource(resource, winners[0], active_token)
    loser_store = right if loser == "right" else left
    assert loser_store.claim_active_slot(loser, tokens[loser], lease_s=5)[1]
    left.close()
    right.close()


def test_bounded_call_rechecks_budget_after_waiting_for_campaign_slot(tmp_path):
    store = MissionStore(str(tmp_path / "active-recheck.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    leash = world_leash(max_active_wall_seconds=1,
                        max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    _create_child(store, "left", "root", leash)
    _create_child(store, "right", "root", leash)
    left_token = store.claim_run("left")
    right_token = store.claim_run("right")
    resource, slot = store.claim_active_slot("left", left_token, lease_s=5)
    assert slot
    stale_timeout = store.remaining_active_wall_seconds("right")
    assert stale_timeout == pytest.approx(1.0)
    assert store.account_runtime("left", left_token, wall_ms=1000)
    assert store.release_resource(resource, "left", slot)
    called = []
    driver = MissionDriver(store, actions, lambda *_args: {})

    outcome = driver._bounded_call(
        lambda: called.append(True), stale_timeout,
        mission_id="right", run_token=right_token)

    assert called == []
    assert isinstance(outcome.error, StepTimedOut)
    assert "active wall-time budget exhausted" in str(outcome.error)
    actions.close()
    store.close()


def test_bounded_call_charges_by_active_slot_after_concurrent_cancel(tmp_path):
    store = MissionStore(str(tmp_path / "cancelled-active.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    leash = world_leash(max_active_wall_seconds=1,
                        max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    token = store.claim_run("root")
    driver = MissionDriver(store, actions, lambda *_args: {})

    def cancel_while_inflight():
        assert store.cancel("root")
        time.sleep(0.003)
        return "finished"

    outcome = driver._bounded_call(
        cancel_while_inflight, 0.5,
        mission_id="root", run_token=token)

    assert outcome.value == "finished"
    assert store.runtime("root")["active_wall_ms"] >= 1
    assert not store.active_resources("root")
    actions.close()
    store.close()


def test_unconfirmed_timeout_charges_once_and_retains_campaign_fence(tmp_path):
    store = MissionStore(str(tmp_path / "timeout-active.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    leash = world_leash(max_active_wall_seconds=1,
                        max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    token = store.claim_run("root")
    driver = MissionDriver(store, actions, lambda *_args: {})
    release = threading.Event()

    outcome = driver._bounded_call(
        lambda: release.wait(1), 0.01,
        mission_id="root", run_token=token)

    assert outcome.timed_out and not outcome.cancelled
    charged = store.runtime("root")["active_wall_ms"]
    assert charged >= 1
    resources = store.active_resources("root")
    assert len(resources) == 1
    # The original token was rotated as part of the one-shot settlement, so a
    # late retry cannot double-charge the same boundary.
    assert not store.settle_active_slot(
        resources[0]["resource"], "root", resources[0]["token"],
        100, release=True)
    assert store.runtime("root")["active_wall_ms"] == charged
    store.schedule_wait("root", 1)
    assert store.finish_run("root", token, "waiting", "retry later")
    with store._lock:
        store.db.execute(
            "UPDATE mission_resource_leases SET lease_until=0 "
            "WHERE mission_id='root' AND token LIKE 'settled:%'")
        store.db.commit()
    claimed = store.claim_due_wait(int(time.time()) + 1)
    assert claimed and claimed[0] == "root"
    resource, fresh_slot = store.claim_active_slot("root", claimed[1], lease_s=5)
    assert fresh_slot, "an expired settled fence must not livelock the same Mission"
    assert store.release_resource(resource, "root", fresh_slot)
    release.set()
    actions.close()
    store.close()


def test_parent_lineage_is_immutable_and_legacy_specialist_is_backfilled(tmp_path):
    path = str(tmp_path / "missions.db")
    store = MissionStore(path)
    leash = world_leash(max_model_tokens=5, max_storage_bytes=10_000_000)
    create_mission(store, "root-a", "root a", leash=leash)
    create_mission(store, "root-b", "root b", leash=leash)
    _create_child(store, "child", "root-a", leash)

    changed_case = dict(store.get("child").case)
    changed_case["_parent_mission_id"] = "root-b"
    store.set_case("child", changed_case)
    child_token = store.claim_run("child")
    assert store.account_runtime("child", child_token, input_tokens=2)
    assert store.aggregate_runtime("root-a")["input_tokens"] == 2
    assert store.aggregate_runtime("root-b")["input_tokens"] == 0
    assert store.runtime("child")["parent_mission_id"] == "root-a"
    store.close()

    # Recreate the pre-column shape. Migration trusts the original case once,
    # restores the immutable column, and also recognizes legacy rows whose lane
    # had defaulted to "mission" before specialist metadata existed.
    db = sqlite3.connect(path)
    db.execute("UPDATE missions SET case_json=? WHERE mission_id='child'",
               ('{"_parent_mission_id":"root-a"}',))
    db.execute("UPDATE mission_runtime SET lane='mission' WHERE mission_id='child'")
    db.execute("DROP INDEX mission_runtime_parent")
    db.execute("ALTER TABLE mission_runtime DROP COLUMN parent_mission_id")
    db.commit()
    db.close()
    reopened = MissionStore(path)
    assert reopened.runtime("child")["parent_mission_id"] == "root-a"
    assert reopened.runtime("child")["lane"] == "specialist"
    reopened.close()


def test_corrupt_specialist_lineage_fails_closed(tmp_path):
    store = MissionStore(str(tmp_path / "missions.db"))
    leash = world_leash(max_total_steps=5)
    create_mission(store, "root", "root", leash=leash)
    _create_child(store, "child", "root", leash)
    with store._lock:
        store.db.execute(
            "UPDATE mission_runtime SET parent_mission_id='' WHERE mission_id='child'")
        store.db.commit()
    assert store.budget_reason("child") == \
        "specialist budget lineage is missing its parent"
    assert store.reserve_decision("child", leash) is False
    store.close()


def test_total_steps_is_atomic_across_root_and_siblings(tmp_path):
    path = str(tmp_path / "missions.db")
    setup = MissionStore(path)
    leash = world_leash(max_total_steps=2, max_storage_bytes=10_000_000)
    create_mission(setup, "root", "root", leash=leash)
    _create_child(setup, "left", "root", leash)
    _create_child(setup, "right", "root", leash)
    assert setup.reserve_decision("root", leash)
    setup.close()

    left, right = MissionStore(path), MissionStore(path)
    barrier = threading.Barrier(2)
    results = {}

    def reserve(store, mission_id):
        barrier.wait()
        results[mission_id] = store.reserve_decision(mission_id, leash)

    threads = [
        threading.Thread(target=reserve, args=(left, "left")),
        threading.Thread(target=reserve, args=(right, "right")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results.values()) == [False, True]
    count = left.db.execute(
        "SELECT COUNT(*) n FROM mission_events WHERE kind='decision'"
    ).fetchone()["n"]
    assert count == 2
    assert left.aggregate_runtime("root")["model_calls"] == 2
    assert left.aggregate_runtime("root")["turns"] == 2
    left.close()
    right.close()


def test_model_call_budget_is_independent_and_defaults_are_durable(tmp_path):
    store = MissionStore(str(tmp_path / "calls.db"))
    leash = world_leash(
        max_total_steps=10, max_model_calls=1, max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    assert store.reserve_decision("root", leash)
    assert store.runtime("root")["model_calls"] == 1
    assert store.runtime("root")["turns"] == 1
    assert store.budget_reason("root") == "mission model-call budget exhausted"
    assert store.reserve_decision("root", leash) is False
    store.close()


def test_model_call_counter_repairs_partial_column_migration(tmp_path):
    path = str(tmp_path / "partial-calls.db")
    store = MissionStore(path)
    leash = world_leash(max_total_steps=10, max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    assert store.reserve_decision("root", leash)
    assert store.reserve_decision("root", leash)
    store.close()

    # Represents a prior process which successfully added the columns but died
    # before it could backfill decision receipts or record any migration marker.
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE mission_runtime DROP COLUMN model_calls")
    db.execute("ALTER TABLE mission_runtime DROP COLUMN turns")
    db.execute(
        "ALTER TABLE mission_runtime ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0")
    db.execute(
        "ALTER TABLE mission_runtime ADD COLUMN turns INTEGER NOT NULL DEFAULT 0")
    db.commit()
    db.close()

    reopened = MissionStore(path)
    assert reopened.runtime("root")["model_calls"] == 2
    assert reopened.runtime("root")["turns"] == 2
    reopened.close()
    again = MissionStore(path)
    assert again.runtime("root")["model_calls"] == 2
    assert again.runtime("root")["turns"] == 2
    again.close()


def test_physical_request_reservation_is_crash_safe_and_owner_fenced(tmp_path):
    path = str(tmp_path / "physical.db")
    store = MissionStore(path)
    leash = world_leash(max_total_steps=10, max_model_calls=2,
                        max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    token = store.claim_run("root")
    assert token
    assert store.reserve_model_request(
        "root", token, "request-1", provider="anthropic-oauth", model="opus")
    assert store.runtime("root")["model_calls"] == 1
    store.close()

    reopened = MissionStore(path)
    assert reopened.runtime("root")["model_calls"] == 1
    assert reopened.reserve_model_request("root", token, "request-1")
    assert reopened.runtime("root")["model_calls"] == 1
    reopened.cancel("root", "stop")
    assert reopened.reserve_model_request("root", token, "request-1") is False
    assert reopened.reserve_model_request("root", token, "request-2") is False
    reopened.close()


def test_planning_turn_does_not_become_physical_call_after_reopen(tmp_path):
    path = str(tmp_path / "planning.db")
    store = MissionStore(path)
    leash = world_leash(max_total_steps=10, max_model_calls=3,
                        max_storage_bytes=10_000_000)
    create_mission(store, "root", "root", leash=leash)
    token = store.claim_run("root")
    assert store.reserve_decision(
        "root", leash, token, count_model_call=False)
    assert store.runtime("root")["model_calls"] == 0
    store.close()

    reopened = MissionStore(path)
    assert reopened.runtime("root")["model_calls"] == 0
    assert reopened.runtime("root")["turns"] == 1
    reopened.close()


@pytest.mark.parametrize(
    ("bound", "reason_fragment"),
    [
        ({"max_irreversible_actions": 2, "actions_per_hour": 10},
         "irreversible-action budget exhausted"),
        ({"max_irreversible_actions": 10, "actions_per_hour": 2},
         "external-action rate limit reached"),
    ],
)
def test_irreversible_quotas_are_atomic_across_root_and_siblings_and_refund(
        tmp_path, bound, reason_fragment):
    path = str(tmp_path / (reason_fragment.split()[0] + ".db"))
    setup = MissionStore(path)
    leash = world_leash(max_total_steps=20, max_storage_bytes=10_000_000, **bound)
    create_mission(setup, "root", "root", leash=leash)
    _create_child(setup, "left", "root", leash)
    _create_child(setup, "right", "root", leash)
    root_token = setup.claim_run("root")
    root_reserved = setup.reserve_action(
        "root", "root-action", True, leash, "social.publish", {}, root_token)
    assert root_reserved[0]
    setup.close()

    left, right = MissionStore(path), MissionStore(path)
    tokens = {"left": left.claim_run("left"), "right": right.claim_run("right")}
    barrier = threading.Barrier(2)
    results = {}

    def reserve(store, mission_id):
        barrier.wait()
        results[mission_id] = store.reserve_action(
            mission_id, mission_id + "-action", True, leash,
            "social.publish", {"mission": mission_id}, tokens[mission_id])

    threads = [
        threading.Thread(target=reserve, args=(left, "left")),
        threading.Thread(target=reserve, args=(right, "right")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    winners = [mid for mid, result in results.items() if result[0]]
    losers = [mid for mid, result in results.items() if not result[0]]
    assert len(winners) == len(losers) == 1
    loser_result = results[losers[0]]
    assert reason_fragment in loser_result[1]
    if "rate limit" in reason_fragment:
        assert loser_result[2] > int(time.time())

    stores = {"left": left, "right": right}
    winner = winners[0]
    loser = losers[0]
    assert stores[winner].release_action_key(
        winner, winner + "-action", tokens[winner])
    retried = stores[loser].reserve_action(
        loser, loser + "-retry", True, leash, "social.publish",
        {"mission": loser, "retry": True}, tokens[loser])
    assert retried[0], retried
    left.close()
    right.close()


def test_irreversible_semantic_key_is_atomic_across_campaign_and_releasable(
        tmp_path):
    path = str(tmp_path / "semantic-keys.db")
    setup = MissionStore(path)
    leash = world_leash(
        max_total_steps=20, max_irreversible_actions=20,
        actions_per_hour=20, max_storage_bytes=10_000_000)
    create_mission(setup, "root", "root", leash=leash)
    _create_child(setup, "left", "root", leash)
    _create_child(setup, "right", "root", leash)
    setup.close()

    left, right = MissionStore(path), MissionStore(path)
    tokens = {"left": left.claim_run("left"), "right": right.claim_run("right")}
    barrier = threading.Barrier(2)
    results = {}

    def reserve(store, mission_id):
        barrier.wait()
        results[mission_id] = store.reserve_action(
            mission_id, "publish:release-42", True, leash,
            "social.publish", {"release": 42}, tokens[mission_id])

    threads = [
        threading.Thread(target=reserve, args=(left, "left")),
        threading.Thread(target=reserve, args=(right, "right")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    winners = [mid for mid, result in results.items() if result[0]]
    losers = [mid for mid, result in results.items() if not result[0]]
    assert len(winners) == len(losers) == 1
    assert "duplicate external action blocked" in results[losers[0]][1]
    assert left.db.execute(
        "SELECT COUNT(*) n FROM mission_action_keys WHERE action_key=?",
        ("publish:release-42",)).fetchone()["n"] == 1

    # The fence belongs to this root campaign, not to the key globally.
    create_mission(left, "other-root", "independent", leash=leash)
    other_token = left.claim_run("other-root")
    assert left.reserve_action(
        "other-root", "publish:release-42", True, leash,
        "social.publish", {"release": 42}, other_token)[0]

    stores = {"left": left, "right": right}
    winner, loser = winners[0], losers[0]
    assert stores[winner].release_action_key(
        winner, "publish:release-42", tokens[winner])
    retried = stores[loser].reserve_action(
        loser, "publish:release-42", True, leash,
        "social.publish", {"release": 42}, tokens[loser])
    assert retried[0], retried
    left.close()
    right.close()


def test_confirmed_action_rechecks_aggregate_budget_before_firing(tmp_path):
    store = MissionStore(str(tmp_path / "missions.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    fired = []
    capability = Capability(
        "social.publish",
        execute=lambda _record: fired.append(True) or {"ok": True},
        verify=lambda _record, _result: Verdict(VERIFIED, "published"),
        reversible=False, risk="publish", semantic_args=("target",))
    decision = {"action": "social.publish", "args": {"target": "one"}}
    driver = MissionDriver(
        store, actions, lambda *_args: decision, capabilities=[capability])
    leash = world_leash(
        may=["social.publish"], autonomous=False, max_model_tokens=1,
        max_total_steps=10, max_storage_bytes=10_000_000)
    create_mission(store, "root", "publish once", leash=leash)
    _create_child(store, "child", "root", leash)

    assert driver.advance("root") == NEEDS_YOU
    _name, nonce = store.last_parked("root")
    child_token = store.claim_run("child")
    assert store.account_runtime("child", child_token, input_tokens=1)
    assert driver.confirm_and_resume("root", nonce) == NEEDS_YOU
    assert fired == []
    assert "model-token budget exhausted" in store.get("root").result
    store.close()
    actions.close()
