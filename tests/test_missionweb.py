"""Pin MissionService (harness.missionweb) — the NL-front-door service behind
`collie web`'s mission commands. Deterministic ($0): a scripted decider stands in
for the model, so this tests the goal-in / status-out marshalling and the
confirm/resume plumbing, not the model.

Run: python tests/test_missionweb.py   (exit 0 = all green)
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.jobs import (clear_registry, NEEDS_YOU, WAITING, DONE_ACCEPTED,
                          PAUSED, CANCELLED, QUEUED, RECONCILING,
                          RECOVERY_REQUIRED)  # noqa: E402
from harness.missionweb import MissionService  # noqa: E402
from harness.verifier import Verdict, VERIFIED  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


class Scripted:
    def __init__(self, decisions):
        self.decisions, self.i = list(decisions), 0

    def __call__(self, goal, case, primitives):
        if self.i >= len(self.decisions):
            return {"action": "done", "reason": "end"}
        d = self.decisions[self.i]
        self.i += 1
        return d


R = {"action": "research", "args": {"query": "price"}, "reason": "price"}
C = {"action": "compose", "args": {"facts": "car"}, "reason": "draft"}
P = {"action": "web.submit", "args": {"what": "listing"}, "reason": "publish"}
H = {"action": "needs_human", "args": {"summary": "buyer ready"}, "reason": "hand off"}
W = {"action": "wait", "args": {"seconds": 3600}, "reason": "later"}


def _svc(decisions):
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    clear_registry()
    # a scripted decider is a controlled scenario -> force the canned stub primitives
    # (independent of whatever real provider the host env has configured).
    return MissionService(base=p, decider=Scripted(decisions), stub=True)


def test_start_gate_confirm_handoff():
    print("test_start_gate_confirm_handoff")
    svc = _svc([R, C, P, H])
    st = svc.start("sell my car", autonomous=False)
    check(st["state"] == QUEUED, "start persists and returns the id before model work")
    st = svc.run(st["mission_id"])

    check(st["state"] == NEEDS_YOU, f"publish should park (needs_you), got {st['state']}")
    check(st["case"].get("researched") and st["case"].get("composed"),
          "reversible steps ran and show in the returned case")
    check("_case" not in st["case"], "the injected _case context is stripped from the UI payload")
    check(st["inbox"] and st["inbox"]["capability"] == "web.submit",
          "a Confirm item is surfaced for the parked publish")
    check(st["needs_human"] is False, "a gated confirm is not a hand-off")

    mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    st2 = svc.confirm(mid, nonce)
    check(st2["state"] == NEEDS_YOU and st2["needs_human"] is True,
          "after confirm+publish it hands off to the human")
    check(st2["case"].get("submitted") is True, "publish fired after confirm")
    check(any(r["capability"] == "web.submit" and r["fired"] for r in st2["receipts"]),
          "the publish receipt is attributed to this mission")

    st3 = svc.accept(mid)
    check(st3["state"] == DONE_ACCEPTED, "accept explicitly takes over the hand-off")
    svc.close()


def test_bad_confirm_is_soft_error():
    print("test_bad_confirm_is_soft_error")
    svc = _svc([R, C, P, H])
    st = svc.start("sell my car", autonomous=False)
    st = svc.run(st["mission_id"])
    out = svc.confirm(st["mission_id"], "not-a-real-nonce")
    check("error" in out and st["state"] == NEEDS_YOU,
          "a bad nonce returns a soft error, not a crash, and leaves the mission parked")
    svc.close()


def test_missions_listing():
    print("test_missions_listing")
    svc = _svc([R, C, P, H])
    svc.start("sell my car", autonomous=True)
    ms = svc.missions()
    check(len(ms) == 1 and ms[0]["goal"] == "sell my car", "the mission is listed for the UI")
    svc.close()


def test_pause_resume_check_and_cancel():
    print("test_pause_resume_check_and_cancel")
    svc = _svc([W, H])
    st = svc.start("watch for a reply", autonomous=True)
    st = svc.run(st["mission_id"]); mid = st["mission_id"]
    check(st["state"] == WAITING and "check" in st["controls"], "waiting is manageable")
    check(svc.pause(mid)["state"] == PAUSED, "pause is durable")
    check(svc.tick(now=10**11).get("advanced") == 0,
          "daemon does not consume a paused wake")
    check(svc.resume(mid)["state"] == WAITING, "resume restores waiting")
    check(svc.check(mid)["state"] == NEEDS_YOU, "check now wakes only this mission")
    check(svc.cancel(mid)["state"] == CANCELLED, "cancel is terminal")
    check(svc.cancel(mid)["state"] == CANCELLED, "cancel is idempotent")
    svc.close()


def test_wrong_mission_nonce_and_cancelled_nonce_are_refused():
    print("test_wrong_mission_nonce_and_cancelled_nonce_are_refused")
    svc = _svc([P])
    one = svc.start("publish one", autonomous=False)
    one = svc.run(one["mission_id"])
    nonce = one["inbox"]["nonce"]
    two = svc.start("publish two", autonomous=False)
    bad = svc.confirm(two["mission_id"], nonce)
    check("error" in bad and svc.actions.get(nonce).state == "pending",
          "a nonce cannot be confirmed through another mission id")
    killed = svc.cancel(one["mission_id"])
    check(killed["state"] == CANCELLED and svc.actions.get(nonce).state == "refused",
          "cancel revokes the parked action")
    check("error" in svc.confirm(one["mission_id"], nonce),
          "a cancelled nonce remains unusable")
    svc.close()


def test_read_surfaces_do_not_require_a_provider():
    print("test_read_surfaces_do_not_require_a_provider")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    svc = MissionService(base=p, provider="")
    st = svc.start("persist only")
    check(st["state"] == QUEUED and len(svc.missions()) == 1,
          "create/status/list work without constructing a model provider")
    svc.close()


def test_human_assist_can_continue_without_ending_the_mission():
    print("test_human_assist_can_continue_without_ending_the_mission")
    svc = _svc([H, {"action": "done", "reason": "finished after MFA"}])
    st = svc.start("finish signup")
    st = svc.run(st["mission_id"]); mid = st["mission_id"]
    check(st["needs_human"] and "continue" in st["controls"],
          "a temporary human hand-off offers continue separately from accept")
    resumed = svc.continue_after_human(mid, "MFA completed")
    check(resumed["state"] == QUEUED and
          resumed["case"]["human_updates"][-1]["note"] == "MFA completed",
          "human completion note is durable and returns control to Collie")
    reported = svc.run(mid)
    check(reported["state"] == NEEDS_YOU and reported["needs_human"],
          "Collie continues after the human assist but its done self-report awaits review")
    check(svc.accept(mid)["state"] == DONE_ACCEPTED,
          "the user explicitly accepts reported completion")
    svc.close()


def test_mock_provider_never_fakes_durable_progress():
    print("test_mock_provider_never_fakes_durable_progress")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    svc = MissionService(base=p, provider="mock")
    created = svc.start("real-world task")
    out = svc.run(created["mission_id"])
    check(out["state"] == QUEUED and "error" in out,
          "the canned mock provider cannot advance a durable real-world mission")
    svc.close()


def test_refused_parked_action_does_not_deadlock_the_mission():
    print("test_refused_parked_action_does_not_deadlock_the_mission")
    svc = _svc([P, H])
    st = svc.start("publish safely")
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    assert svc.actions.refuse(nonce, "approval expired")
    repaired = svc.status(mid)
    check(repaired["inbox"] is None and repaired["needs_human"],
          "refused/expired parked action becomes a replannable hand-off")
    continued = svc.continue_after_human(mid, "re-prepare a fresh target")
    check(continued["state"] == QUEUED,
          "stale awaiting row no longer makes continue/accept impossible")
    svc.close()


def test_reconcile_wrong_state_has_no_side_effects():
    print("test_reconcile_wrong_state_has_no_side_effects")
    svc = _svc([P])
    st = svc.start("publish safely")
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    before = svc.actions.get(nonce)
    out = svc.reconcile(mid, "this is not a recovery state")
    after = svc.actions.get(nonce)
    check("error" in out and out["state"] == NEEDS_YOU,
          "reconcile is rejected outside recovery_required")
    check(before.state == after.state == "pending" and svc.status(mid)["inbox"],
          "a rejected reconcile does not revoke or detach the pending action")
    svc.close()


def test_reconcile_fences_cleanup_before_requeue():
    print("test_reconcile_fences_cleanup_before_requeue")
    svc = _svc([])
    st = svc.start("recover safely"); mid = st["mission_id"]
    nonce = svc.actions.propose(
        "web.submit", {"what": "old"}, job_id=mid, leash_id=mid)
    svc.store.set_state(mid, RECOVERY_REQUIRED, "inspect first")
    seen = []
    original = svc.actions.refuse

    def inspect_fence(*args, **kwargs):
        seen.append(svc.store.get(mid).state)
        return original(*args, **kwargs)

    svc.actions.refuse = inspect_fence
    out = svc.reconcile(mid, "receipts checked")
    check(seen == [RECONCILING] and out["state"] == QUEUED and
          svc.actions.get(nonce).state == "refused",
          "cross-database cleanup runs behind a persistent non-runnable fence")
    svc.close()


def test_stale_reconciler_cannot_revoke_a_fresh_action():
    print("test_stale_reconciler_cannot_revoke_a_fresh_action")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    one = MissionService(base=p, decider=Scripted([]), stub=True)
    two = MissionService(base=p, decider=Scripted([]), stub=True)
    st = one.start("recover without crossing generations"); mid = st["mission_id"]
    old = one.actions.propose(
        "web.submit", {"what": "old"}, job_id=mid, leash_id=mid)
    one.store.set_state(mid, RECOVERY_REQUIRED, "old run crashed")

    entered, release = threading.Event(), threading.Event()
    original_begin = one.store.begin_reconcile

    def stalled_begin(mission_id, note="", lease_s=300):
        token = original_begin(mission_id, note, lease_s=0)
        entered.set(); release.wait(3)
        return token

    one.store.begin_reconcile = stalled_begin
    stale_result = []
    t = threading.Thread(
        target=lambda: stale_result.append(one.reconcile(mid, "old inspection")))
    t.start(); check(entered.wait(1), "first reconciler acquired its cleanup lease")
    winner = two.reconcile(mid, "take over expired cleanup")
    check(winner["state"] == QUEUED and two.actions.get(old).state == "refused",
          "replacement owner safely completed the old cleanup")

    leash = two.store.get(mid).leash
    fresh_run = two.store.claim_run(mid)
    ok, _why, _retry = two.store.reserve_action(
        mid, "fresh-key", True, leash, "web.submit", {"fresh": True}, fresh_run)
    fresh = two.actions.propose(
        "web.submit", {"what": "fresh"}, job_id=mid, leash_id=mid)
    bound = two.store.bind_action_key(mid, "fresh-key", fresh, fresh_run)
    parked = two.store.park_for_confirm(
        mid, fresh_run, "web.submit", fresh, "fresh confirmation")
    release.set(); t.join(3)
    key = two.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, fresh)).fetchone()
    check(ok and bound and parked and stale_result and "error" in stale_result[0] and
          two.actions.get(fresh).state == "pending" and key is not None and
          two.store.last_parked(mid)[1] == fresh,
          "expired cleanup owner cannot revoke or detach a post-recovery action")
    one.close(); two.close()


def test_reconcile_resolves_old_inbox_but_preserves_executed_key():
    print("test_reconcile_resolves_old_inbox_but_preserves_executed_key")
    svc = _svc([P])
    st = svc.start("publish exactly once")
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    svc.actions.confirm(nonce)
    svc.actions.execute(
        nonce, side_effect_fn=lambda _r: {"submitted": True},
        donecheck_fn=lambda _r, _x: Verdict(VERIFIED, "done"))
    svc.store.set_state(mid, RECOVERY_REQUIRED, "crashed before folding receipt")
    out = svc.reconcile(mid, "receipt and external target inspected")
    row = svc.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, nonce)).fetchone()
    check(out["state"] == QUEUED and svc.store.last_parked(mid)[1] is None and
          row is not None,
          "reconcile retires the stale inbox while retaining executed idempotency")
    reported = svc.run(mid)
    check(reported["state"] == NEEDS_YOU and not reported["action_in_flight"] and
          "accept" in reported["controls"],
          "old executed inbox cannot permanently suppress completion review")
    svc.close()


def test_reconcile_clears_only_unmaterialized_reserved_keys():
    print("test_reconcile_clears_only_unmaterialized_reserved_keys")
    svc = _svc([])
    st = svc.start("recover reservation crash", max_irreversible_actions=1)
    mid = st["mission_id"]
    leash = svc.store.get(mid).leash
    old_run = svc.store.claim_run(mid)
    ok, _why, _retry = svc.store.reserve_action(
        mid, "orphan-key", True, leash, "web.submit", {"old": True}, old_run)
    with svc.store._lock:
        svc.store.db.execute(
            "UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
            (RECOVERY_REQUIRED, mid))
        svc.store.db.commit()
    out = svc.reconcile(mid, "no matching action or external effect exists")
    fresh_run = svc.store.claim_run(mid)
    again, _why2, _retry2 = svc.store.reserve_action(
        mid, "orphan-key", True, leash, "web.submit", {"new": True}, fresh_run)
    check(ok and out["state"] == QUEUED and again,
          "an empty reservation returns both its key and max-action quota")
    svc.close()


def test_reconcile_releases_a_previously_refused_materialized_key():
    print("test_reconcile_releases_a_previously_refused_materialized_key")
    svc = _svc([])
    st = svc.start("retry an action proven not fired"); mid = st["mission_id"]
    leash = svc.store.get(mid).leash
    old_run = svc.store.claim_run(mid)
    ok, _why, _retry = svc.store.reserve_action(
        mid, "refused-key", True, leash, "web.submit", {"old": True}, old_run)
    nonce = svc.actions.propose(
        "web.submit", {"what": "old"}, job_id=mid, leash_id=mid)
    bound = svc.store.bind_action_key(mid, "refused-key", nonce, old_run)
    refused = svc.actions.refuse(nonce, "old worker lost ownership before firing")
    with svc.store._lock:
        svc.store.db.execute(
            "UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
            (RECOVERY_REQUIRED, mid))
        svc.store.db.commit()
    out = svc.reconcile(mid, "refusal proves the action never fired")
    key = svc.store.db.execute(
        "SELECT 1 FROM mission_action_keys WHERE mission_id=? AND action_key=?",
        (mid, "refused-key")).fetchone()
    fresh_run = svc.store.claim_run(mid)
    retried, _why2, _retry2 = svc.store.reserve_action(
        mid, "refused-key", True, leash, "web.submit", {"new": True}, fresh_run)
    check(ok and bound and refused and out["state"] == QUEUED and key is None and retried,
          "reconcile releases exact REFUSED/EXPIRED nonces without touching uncertain ones")
    svc.close()


def test_status_never_releases_an_executed_action_key():
    print("test_status_never_releases_an_executed_action_key")
    svc = _svc([P])
    st = svc.start("publish exactly once")
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    svc.actions.confirm(nonce)
    svc.actions.execute(
        nonce, side_effect_fn=lambda _r: {"submitted": True},
        donecheck_fn=lambda _r, _x: Verdict(VERIFIED, "done"))
    raced = svc.status(mid)
    row = svc.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, nonce)).fetchone()
    check(raced["action_in_flight"] and raced["controls"] == ["cancel"],
          "a stale NEEDS_YOU view surfaces an uncertain/in-flight action conservatively")
    check(row and row["state"] == "executed",
          "status preserves and hardens the semantic key after ActionStore execution")
    svc.close()


def test_reconcile_waits_for_old_execution_latch():
    print("test_reconcile_waits_for_old_execution_latch")
    svc = _svc([P])
    st = svc.start("publish after crash")
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    svc.actions.confirm(nonce)
    old_run = svc.store.claim_run(mid, expected=(NEEDS_YOU,))
    resource, execution_token = svc.store.claim_execution(nonce, mid, old_run)
    entered, release = __import__("threading").Event(), __import__("threading").Event()

    def old_side_effect(_rec):
        entered.set(); release.wait(2); return {"submitted": True}

    thread = __import__("threading").Thread(target=lambda: svc.actions.execute(
        nonce, side_effect_fn=old_side_effect,
        donecheck_fn=lambda _r, _x: Verdict(VERIFIED, "done")))
    thread.start(); check(entered.wait(1), "old worker reached EXECUTING")
    with svc.store._lock:
        svc.store.db.execute(
            "UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
            (RECOVERY_REQUIRED, mid))
        svc.store.db.commit()
    blocked = svc.reconcile(mid, "site inspected while old worker was still live")
    row = svc.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, nonce)).fetchone()
    check(blocked["state"] == RECONCILING and "still executing" in blocked["error"],
          "reconcile remains fenced while an old side effect is live")
    check(svc.actions.get(nonce).state == "executing" and row is not None and
          svc.store.active_resources(mid),
          "live action key and execution/resource latch are preserved")
    release.set(); thread.join(2)
    svc.store.release_resource(resource, mid, execution_token)
    done = svc.reconcile(mid, "old worker settled; receipt inspected")
    row2 = svc.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, nonce)).fetchone()
    check(done["state"] == QUEUED and row2 is not None,
          "retry completes only after the old action settles, without deleting its key")
    svc.close()


def main():
    test_start_gate_confirm_handoff()
    test_bad_confirm_is_soft_error()
    test_missions_listing()
    test_pause_resume_check_and_cancel()
    test_wrong_mission_nonce_and_cancelled_nonce_are_refused()
    test_read_surfaces_do_not_require_a_provider()
    test_human_assist_can_continue_without_ending_the_mission()
    test_mock_provider_never_fakes_durable_progress()
    test_refused_parked_action_does_not_deadlock_the_mission()
    test_reconcile_wrong_state_has_no_side_effects()
    test_reconcile_fences_cleanup_before_requeue()
    test_stale_reconciler_cannot_revoke_a_fresh_action()
    test_reconcile_resolves_old_inbox_but_preserves_executed_key()
    test_reconcile_clears_only_unmaterialized_reserved_keys()
    test_reconcile_releases_a_previously_refused_materialized_key()
    test_status_never_releases_an_executed_action_key()
    test_reconcile_waits_for_old_execution_latch()
    if _fails:
        print(f"\n{len(_fails)} FAILED")
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
