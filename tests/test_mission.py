"""Pin the Mission container (harness.mission) — the durable, gated, verified shell
a model drives with neutral primitives. No per-errand template, no marketplace.*.

Run: python tests/test_mission.py   (exit 0 = all green)

The container is what's under test, so the decider is a SCRIPTED test double (it
stands in for the model returning a next-action each step). Production wires
mission.ModelDecider(provider). Proven here:
  - the model drives the flow: a scripted sequence of primitives runs multi-step,
    each result folded into the shared durable case
  - reversible primitives (research/compose/observe) auto-run under the leash
  - an irreversible primitive (web.submit) PARKS for confirm unless the leash
    pre-authorizes it — autonomy is the leash, not a flag
  - a 'wait' is DURABLE: it schedules and re-enters on tick (colliejobd)
  - 'needs_human' hands off; confirm+resume carries the campaign on
  - an out-of-leash action fails closed (never runs unauthorized)
"""
import os
import json
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.actions import ActionStore  # noqa: E402
from harness.webact import FakeActuator  # noqa: E402
from harness.jobs import (  # noqa: E402
    clear_registry, QUEUED, WAITING, NEEDS_YOU, DONE_VERIFIED, DONE_ACCEPTED, FAILED_S,
    PAUSED, PAUSING, CANCELLED, RUNNING, RECONCILING, RECOVERY_REQUIRED,
    Capability, register,
)
from harness.primitives import register_primitives  # noqa: E402
from harness.mission import (  # noqa: E402
    MissionStore, MissionDriver, ModelDecider, create_mission, world_leash,
    _model_case_json,
)
from harness.providers import Completion  # noqa: E402
from harness.verifier import Verdict, VERIFIED  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


class Scripted:
    """A decider test double: returns the next canned decision each call, then
    'done'. Models the model being asked 'what next?' repeatedly."""
    def __init__(self, decisions):
        self.decisions, self.i = list(decisions), 0

    def __call__(self, goal, case, primitives):
        if self.i >= len(self.decisions):
            return {"action": "done", "reason": "script exhausted"}
        d = self.decisions[self.i]
        self.i += 1
        return d


def _driver(decisions):
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    actions = ActionStore(p + ".actions")
    store = MissionStore(p + ".missions")
    return MissionDriver(store, actions, Scripted(decisions)), store, actions


R = {"action": "research", "args": {"query": "corolla price"}, "reason": "price"}
C = {"action": "compose", "args": {"facts": "2018 corolla"}, "reason": "draft listing"}
PUB = {"action": "web.submit", "args": {"what": "listing"}, "reason": "publish it"}
OBS = {"action": "observe", "args": {}, "reason": "check the inbox"}
WAIT = {"action": "wait", "args": {"seconds": 3600}, "reason": "poll later"}
HAND = {"action": "needs_human", "args": {"summary": "a local buyer at 7700 — your call"},
        "reason": "hand off"}
GOAL = "sell my 2018 Toyota Corolla"


def test_confirm_gate_then_resume():
    """autonomous=False: the reads auto-run, publish PARKS for confirm, then a
    human confirm + resume carries it to the hand-off."""
    print("test_confirm_gate_then_resume")
    clear_registry()
    register_primitives(stub=True)
    drv, store, actions = _driver([R, C, PUB, HAND])
    create_mission(store, "m1", GOAL, case={"make": "Toyota"},
                   leash=world_leash(autonomous=False))

    st = drv.advance("m1")
    check(st == NEEDS_YOU, f"publish must park for confirm, got {st}")
    c = store.get("m1").case
    check(c.get("researched") and c.get("composed"), "reads auto-ran before the gate")
    check(c.get("submitted") is not True, "publish did NOT fire — it is parked")

    inbox = actions.pending()
    check(len(inbox) == 1 and inbox[0]["capability"] == "web.submit",
          "the parked publish is in the human confirm inbox")

    name, nonce = store.last_parked("m1")
    check(name == "web.submit" and nonce == inbox[0]["nonce"], "parked action recoverable")
    actions.confirm(nonce)
    st2 = drv.resume("m1")
    check(st2 == NEEDS_YOU, f"after confirm the campaign runs publish then hands off, got {st2}")
    check(store.get("m1").case.get("submitted") is True, "publish fired after confirm")
    check(drv.resume("m1") == DONE_ACCEPTED, "resuming the hand-off accepts it")


def test_autonomous_with_durable_wait():
    """autonomous=True: publish is pre-authorized; a 'wait' is durable and the
    loop re-enters on tick before the buyer surfaces and it hands off."""
    print("test_autonomous_with_durable_wait")
    clear_registry()
    register_primitives(stub=True)
    # a real poll loop: check inbox (nothing) -> wait -> check again (buyer) -> hand off
    drv, store, actions = _driver([R, C, PUB, OBS, WAIT, OBS, HAND])
    create_mission(store, "m2", GOAL, leash=world_leash(autonomous=True))

    st = drv.advance("m2")
    check(st == WAITING, f"first pass runs to the durable wait, got {st}")
    c = store.get("m2").case
    check(c.get("submitted") is True and c.get("url"), "publish auto-ran (pre-authorized)")
    check(c.get("observe_count") == 1 and c.get("signal") is False, "first inbox check found nothing")
    check(len(store.due_waits(10**11)) == 1, "a durable re-check is scheduled")
    check(drv.tick_missions(0) == 0, "nothing fires before the wait is due")

    n = drv.tick_missions(10**11)         # wake in the future -> re-enter
    check(n == 1, f"one mission advanced on tick, got {n}")
    m = store.get("m2")
    check(m.state == NEEDS_YOU, f"after the re-check it hands off (needs_you), got {m.state}")
    check(m.case.get("observe_count") == 2, "the poll count persisted across the wait")
    check(m.case.get("signal") is True, "the second observation surfaced the signal")

    pub = [r for r in actions.receipts() if r["capability"] == "web.submit" and r["fired"]]
    check(len(pub) == 1 and pub[0]["verdict"] == "verified", "publish left a verified receipt")
    check(drv.resume("m2") == DONE_ACCEPTED, "the hand-off accepts")


def test_leash_denies_out_of_scope():
    """A primitive outside the leash `may` fails closed — never runs unauthorized."""
    print("test_leash_denies_out_of_scope")
    clear_registry()
    register_primitives(stub=True)
    # allow only reads; web.send is out of scope
    drv, store, actions = _driver([{"action": "web.send", "args": {"to": "x"}, "reason": "msg"}])
    create_mission(store, "m3", GOAL,
                   leash=world_leash(may=["research", "compose", "observe"]))
    st = drv.advance("m3")
    check(st == FAILED_S, f"an out-of-leash action must fail closed, got {st}")
    check("denied" in store.get("m3").result, "failure names the leash denial")
    check(not [r for r in actions.receipts() if r["fired"]], "nothing fired")


def test_anti_poll_spin():
    """A decider that keeps choosing a reversible read must NOT tight-loop: after
    read_streak_cap consecutive reads the driver forces a durable wait (a monitor
    reads then waits — it does not poll 40x in a row)."""
    print("test_anti_poll_spin")
    clear_registry()
    register_primitives(stub=True)
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    actions = ActionStore(p + ".actions")
    store = MissionStore(p + ".missions")
    always_read = lambda goal, case, prims: {"action": "observe", "args": {}, "reason": "poll"}
    drv = MissionDriver(store, actions, always_read)
    create_mission(store, "spin", GOAL, leash=world_leash(autonomous=True))

    st = drv.advance("spin")
    check(st == WAITING, f"consecutive reads must force a durable wait, got {st}")
    n = len([s for s in store.steps("spin") if s["name"] == "observe"])
    check(n == drv.read_streak_cap, f"reads capped at {drv.read_streak_cap}, ran {n} (no spin)")
    check(len(store.due_waits(10**11)) == 1, "a durable re-check was scheduled instead of spinning")
    store.close()
    actions.close()


def test_distinct_observe_targets_are_not_poll_backoff():
    """Reading several different platforms once is discovery, not a tight poll."""
    print("test_distinct_observe_targets_are_not_poll_backoff")
    clear_registry()
    register_primitives(stub=True)
    reads = [
        {"action": "observe", "args": {"url": f"https://site{i}.test/home", "authed": True},
         "reason": "inspect a distinct account"}
        for i in range(5)
    ]
    drv, store, actions = _driver(reads + [HAND])
    create_mission(store, "multi-site-read", "inspect several signed-in platforms",
                   leash=world_leash(autonomous=True))

    state = drv.advance("multi-site-read")
    completed = [s for s in store.steps("multi-site-read") if s["name"] == "observe"]
    check(state == NEEDS_YOU and len(completed) == 5 and
          store.next_wait("multi-site-read") is None,
          "different observe targets proceed without the one-hour polling delay")
    store.close()
    actions.close()


def test_model_context_keeps_newest_results_and_per_site_browse_facts():
    print("test_model_context_keeps_newest_results_and_per_site_browse_facts")
    recent = [{"marker": f"result-{i}", "body": "x" * 900} for i in range(8)]
    encoded = _model_case_json({"_recent_results": recent}, 1800)
    check("result-7" in encoded and "result-0" not in encoded,
          "bounded model context keeps newest timeline evidence, not the oldest prefix")

    drv, store, actions = _driver([])
    create_mission(store, "site-facts", "inspect several platforms",
                   leash=world_leash(autonomous=True))
    mission = store.get("site-facts")
    for host, summary in (("x.com", "authenticated; composer available"),
                          ("www.reddit.com", "u/nestlyze; r/SideProject available")):
        drv._fold(mission, "browse", {"result": summary, "form": [],
                                      "page": {"host": host, "title": host},
                                      "case": {"browsed": True, "browse_result": summary}})
    case = store.get("site-facts").case
    check(set(case.get("browse_sites", {})) == {"x.com", "www.reddit.com"},
          "browse facts accumulate by domain rather than overwriting the prior site")
    model_case = json.loads(_model_case_json(case, 5000))
    check("x.com" in model_case.get("browse_sites", {}) and
          "www.reddit.com" in model_case.get("browse_sites", {}),
          "per-site browser facts survive model-context compaction")
    store.close()
    actions.close()


def test_model_context_preserves_complete_latest_human_update():
    print("test_model_context_preserves_complete_latest_human_update")
    tail = " FINAL-URL=https://vocalcode.app/ NEVER-DUPLICATE"
    note = "Use this exact approved copy: " + ("x" * 340) + tail
    encoded = _model_case_json({"old": "z" * 30000,
                                "human_updates": [{"at": 1, "note": "older"},
                                                  {"at": 2, "note": note}]}, 2400)
    check(tail in encoded,
          "the newest ordinary operator note keeps its final URL/constraint after compaction")


def test_local_compose_work_is_not_treated_as_polling():
    """Several writing steps before the first external action must not inherit
    the one-hour inbox-poll backoff."""
    print("test_local_compose_work_is_not_treated_as_polling")
    clear_registry()
    register_primitives(stub=True)
    writes = [
        {"action": "compose", "args": {"facts": "fact", "instruction": f"post {i}"},
         "reason": "prepare channel copy"}
        for i in range(5)
    ]
    drv, store, actions = _driver(writes + [HAND])
    create_mission(store, "compose-burst", "prepare a multi-channel campaign",
                   leash=world_leash(autonomous=True))
    state = drv.advance("compose-burst")
    completed = [s for s in store.steps("compose-burst") if s["name"] == "compose"]
    check(state == NEEDS_YOU and len(completed) == 5 and
          store.next_wait("compose-burst") is None,
          "local composition proceeds without the durable polling delay")
    store.close()
    actions.close()


def test_browse_mission_gates_publish():
    """The FB path: a mission uses `browse` (reversible, auto) to fill the form, then
    `browse.submit` (irreversible) PARKS for confirm; confirm+resume publishes."""
    print("test_browse_mission_gates_publish")
    clear_registry()
    register_primitives(stub=True)
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    actions = ActionStore(p + ".actions")
    store = MissionStore(p + ".missions")
    BR = {"action": "browse", "args": {"goal": "fill the Corolla listing"}, "reason": "fill"}
    SUB = {"action": "browse.submit", "args": {"button": "Publish"}, "reason": "publish"}
    drv = MissionDriver(store, actions, Scripted([BR, SUB]))
    create_mission(store, "b", GOAL, leash=world_leash(autonomous=False))

    st = drv.advance("b")
    check(st == NEEDS_YOU, f"browse.submit (publish) must park for confirm, got {st}")
    check(store.get("b").case.get("browsed") is True, "browse filled (reversible, auto) before the gate")
    inbox = actions.pending()
    check(len(inbox) == 1 and inbox[0]["capability"] == "browse.submit",
          "the parked irreversible action is browse.submit (the Publish click)")

    name, nonce = store.last_parked("b")
    actions.confirm(nonce)
    drv.resume("b")
    check(store.get("b").case.get("published") is True, "publish fired only after the human confirmed")
    store.close()
    actions.close()


def test_code_step_in_a_mission():
    """Coding is one capability among many: a mission can run a `code` step (reversible,
    auto) alongside world steps — the 'one entry, does anything' picture."""
    print("test_code_step_in_a_mission")
    clear_registry()
    register_primitives(stub=True)
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    actions = ActionStore(p + ".actions")
    store = MissionStore(p + ".missions")
    CODE = {"action": "code", "args": {"goal": "add a retry", "workspace": "/repo"}, "reason": "fix"}
    drv = MissionDriver(store, actions, Scripted([CODE, {"action": "done"}]))
    create_mission(store, "c", "fix the retry bug then done",
                   leash=world_leash(may=["code"], autonomous=False))
    st = drv.advance("c")
    check(st == NEEDS_YOU,
          f"a model done self-report requires explicit human acceptance, got {st}")
    check(store.get("c").case.get("coded") is True, "the mission ran a coding step")
    check(drv.accept_handoff("c") == DONE_ACCEPTED,
          "human acceptance, not model self-report, ends the mission")
    store.close()
    actions.close()


def test_pause_preserves_due_wait_and_cancel_is_terminal():
    print("test_pause_preserves_due_wait_and_cancel_is_terminal")
    clear_registry(); register_primitives(stub=True)
    drv, store, actions = _driver([WAIT, HAND])
    create_mission(store, "life", GOAL, leash=world_leash(autonomous=True))
    check(drv.advance("life") == WAITING, "mission reached a durable wait")
    check(store.pause("life") and store.get("life").state == PAUSED,
          "waiting -> paused")
    check(drv.tick_missions(10**11) == 0 and store.next_wait("life") is not None,
          "a due wake remains pending while paused")
    check(store.resume_paused("life") == WAITING, "resume restores waiting")
    check(drv.tick_missions(10**11) == 1 and store.get("life").state == NEEDS_YOU,
          "the preserved wake advances exactly once after resume")
    check(store.cancel("life") and store.get("life").state == CANCELLED,
          "needs_you can be cancelled")
    check(drv.advance("life") == CANCELLED and drv.tick_missions(10**11) == 0,
          "cancel is terminal and cannot be woken")
    store.close(); actions.close()


def test_browse_submit_requires_latest_verified_preparation():
    print("test_browse_submit_requires_latest_verified_preparation")
    failed = [{"kind": "result", "name": "browse",
               "payload": {"verdict": "failed", "reason": "body did not match"}}]
    ok = [{"kind": "result", "name": "browse",
           "payload": {"verdict": "verified", "reason": "exact form reread"}}]
    check(MissionDriver._browse_submit_ready(failed)[0] is False,
          "a failed fill deterministically blocks the final browser click")
    check(MissionDriver._browse_submit_ready(ok)[0] is True,
          "an independently verified fill permits the final browser click")
    check(MissionDriver._browse_submit_ready([])[0] is False,
          "submit without any preparation evidence fails closed")


def test_cancel_revokes_a_parked_action():
    print("test_cancel_revokes_a_parked_action")
    clear_registry(); register_primitives(stub=True)
    drv, store, actions = _driver([PUB])
    create_mission(store, "kill", GOAL, leash=world_leash(autonomous=False))
    check(drv.advance("kill") == NEEDS_YOU, "publish parked")
    _name, nonce = store.last_parked("kill")
    store.cancel("kill"); actions.refuse_for_job("kill")
    check(actions.get(nonce).state == "refused", "cancel revokes the pending payload")
    try:
        actions.confirm(nonce)
        check(False, "a cancelled mission's nonce must never be confirmable")
    except Exception:
        check(True, "cancelled nonce refused")
    check(drv.resume("kill") == CANCELLED, "stale resume cannot revive cancellation")
    store.close(); actions.close()


def test_run_claim_prevents_two_drivers_and_cancel_beats_stale_worker():
    print("test_run_claim_prevents_two_drivers_and_cancel_beats_stale_worker")
    clear_registry(); register_primitives(stub=True)
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    ap, mp = p + ".actions", p + ".missions"
    a1, a2 = ActionStore(ap), ActionStore(ap)
    s1, s2 = MissionStore(mp), MissionStore(mp)
    started, release, calls = threading.Event(), threading.Event(), []

    def first(*_args):
        calls.append("first"); started.set(); release.wait(2)
        return {"action": "done", "reason": "one owner"}

    def second(*_args):
        calls.append("second")
        return {"action": "done"}

    create_mission(s1, "race", GOAL, leash=world_leash())
    d1, d2 = MissionDriver(s1, a1, first), MissionDriver(s2, a2, second)
    th = threading.Thread(target=lambda: d1.advance("race")); th.start()
    check(started.wait(2), "first driver acquired the mission")
    check(d2.advance("race") == RUNNING and calls == ["first"],
          "a concurrent driver loses the SQL claim without calling its model")
    s2.cancel("race"); a2.refuse_for_job("race"); release.set(); th.join(2)
    check(s1.get("race").state == CANCELLED,
          "the stale worker cannot overwrite a concurrent cancellation")
    s1.close(); s2.close(); a1.close(); a2.close()


def test_old_mission_database_migrates_in_place():
    print("test_old_mission_database_migrates_in_place")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE missions(mission_id TEXT PRIMARY KEY, goal TEXT, "
               "leash_json TEXT, case_json TEXT, state TEXT, result TEXT, "
               "created_at INTEGER, updated_at INTEGER)")
    db.execute("INSERT INTO missions VALUES(?,?,?,?,?,?,?,?)",
               ("old", "survive upgrade", "{}", "{}", "queued", "", 1, 1))
    db.commit(); db.close()
    store = MissionStore(p)
    old = store.get("old")
    check(old and old.goal == "survive upgrade" and old.run_token == "",
          "guarded migration preserves old mission rows")
    check(store.pause("old") and store.get("old").state == PAUSED,
          "new lifecycle columns work on the migrated row")
    store.close()


def test_expired_runner_heartbeat_recovers_without_blind_replay():
    print("test_expired_runner_heartbeat_recovers_without_blind_replay")
    drv, store, actions = _driver([])
    create_mission(store, "crash", GOAL, leash=world_leash())
    token = store.claim_run("crash", lease_s=-1)
    check(bool(token) and store.get("crash").state == RUNNING, "worker claimed the run")
    check(store.recover_stale_runs() == 1 and
          store.get("crash").state == RECOVERY_REQUIRED,
          "expired heartbeat becomes a distinct recovery-required checkpoint")
    check("inspect the external system" in store.get("crash").result,
          "recovery never claims an uncertain external action did not fire")
    check(not store.continue_handoff("crash", "receipts inspected"),
          "ordinary human-assist continue cannot bypass crash reconciliation")
    check(store.reconcile_recovery("crash", "receipts and target inspected") and
          store.get("crash").state == QUEUED,
          "explicit reconciliation can continue the durable mission")
    store.close(); actions.close()


def test_pause_waits_for_an_inflight_action_boundary():
    print("test_pause_waits_for_an_inflight_action_boundary")
    clear_registry()
    entered, release, calls = threading.Event(), threading.Event(), []

    def slow(_rec):
        calls.append(1); entered.set(); release.wait(3); return {"case": {"slow": True}}

    register(Capability("slow.action", execute=slow,
                        verify=lambda _r, _x: Verdict(VERIFIED, "done"),
                        reversible=True, risk="read"))
    drv, store, actions = _driver([
        {"action": "slow.action", "args": {}}, {"action": "done"}])
    create_mission(store, "slow", "one slow action",
                   leash=world_leash(may=["slow.action"], autonomous=True))
    th = threading.Thread(target=lambda: drv.advance("slow")); th.start()
    check(entered.wait(2), "primitive entered its side effect")
    check(store.pause("slow") and store.get("slow").state == PAUSING,
          "pause requests quiescence without revoking the live owner token")
    check(store.resume_paused("slow") is None and calls == [1],
          "resume is impossible while the old primitive is still running")
    release.set(); th.join(3)
    check(store.get("slow").state == PAUSED and calls == [1],
          "the owner acknowledges PAUSED exactly at the action boundary")
    check(store.resume_paused("slow") == QUEUED,
          "resume becomes available only after quiescence")
    check(store.get("slow").case.get("slow") is True,
          "a completed result is folded durably before PAUSING settles")
    check(drv.advance("slow") == NEEDS_YOU and calls == [1],
          "resume sees the folded case and never repeats the completed action")
    store.close(); actions.close()


def test_pause_during_confirmed_action_does_not_reenter():
    print("test_pause_during_confirmed_action_does_not_reenter")
    clear_registry()
    entered, release, calls = threading.Event(), threading.Event(), []

    def slow_send(_rec):
        calls.append(1); entered.set(); release.wait(3); return {"sent": True}

    register(Capability("slow.send", execute=slow_send,
                        verify=lambda _r, _x: Verdict(VERIFIED, "sent"),
                        reversible=False, risk="send", semantic_args=("to",)))
    drv, store, actions = _driver([
        {"action": "slow.send", "args": {"to": "one"}}, {"action": "done"}])
    create_mission(store, "send", "send once",
                   leash=world_leash(may=["slow.send"], autonomous=False))
    check(drv.advance("send") == NEEDS_YOU, "irreversible action parked")
    _name, nonce = store.last_parked("send")
    th = threading.Thread(target=lambda: drv.confirm_and_resume("send", nonce)); th.start()
    check(entered.wait(2), "confirmed primitive entered its side effect")
    check(store.pause("send") and store.get("send").state == PAUSING,
          "confirmed path also enters PAUSING")
    check(store.resume_paused("send") is None, "no second confirmed worker can start")
    release.set(); th.join(3)
    check(store.get("send").state == PAUSED and calls == [1],
          "confirmed side effect fired once and then settled paused")
    check(any(r["fired"] for r in actions.receipts(nonce)),
          "the in-flight action's receipt is retained")
    check(store.get("send").case.get("slow.send", {}).get("sent") is True,
          "confirmed action result is folded before the pause boundary")
    check(store.resume_paused("send") == QUEUED and
          drv.advance("send") == NEEDS_YOU and calls == [1],
          "resume after a completed send does not send it a second time")
    store.close(); actions.close()


def test_pause_after_approval_retires_unfired_key():
    print("test_pause_after_approval_retires_unfired_key")
    clear_registry(); calls = []
    register(Capability("slow.send", execute=lambda _r: calls.append(1) or {"sent": True},
                        verify=lambda _r, _x: Verdict(VERIFIED, "sent"),
                        reversible=False, risk="send", semantic_args=("to",)))
    decision = {"action": "slow.send", "args": {"to": "one"}}
    drv, store, actions = _driver([decision, decision])
    create_mission(store, "approve-pause", "send once",
                   leash=world_leash(may=["slow.send"], autonomous=False))
    check(drv.advance("approve-pause") == NEEDS_YOU, "first send parked")
    _name, nonce = store.last_parked("approve-pause")
    original_confirm = actions.confirm

    def approve_then_pause(n):
        rec = original_confirm(n)
        store.pause("approve-pause")
        return rec

    actions.confirm = approve_then_pause
    check(drv.confirm_and_resume("approve-pause", nonce) == PAUSED and not calls,
          "pause after approval but before execute fires nothing")
    key_count = store.db.execute(
        "SELECT COUNT(*) n FROM mission_action_keys WHERE mission_id=?",
        ("approve-pause",)).fetchone()["n"]
    check(store.last_parked("approve-pause")[1] is None and key_count == 0,
          "unfired approval retires awaiting row and semantic key")
    check(store.resume_paused("approve-pause") == QUEUED and
          drv.advance("approve-pause") == NEEDS_YOU,
          "a fresh proposal is allowed after the safe pause boundary")
    store.close(); actions.close()


def test_pause_before_confirm_inbox_publish_is_atomic():
    print("test_pause_before_confirm_inbox_publish_is_atomic")
    clear_registry(); register_primitives(stub=True)
    drv, store, actions = _driver([PUB, PUB])
    create_mission(store, "gate-pause", GOAL,
                   leash=world_leash(autonomous=False))
    original_park = store.park_for_confirm

    def pause_then_try_park(*args, **kwargs):
        store.pause("gate-pause")
        return original_park(*args, **kwargs)

    store.park_for_confirm = pause_then_try_park
    check(drv.advance("gate-pause") == PAUSED,
          "pause can win before the confirmation inbox is atomically published")
    count = store.db.execute(
        "SELECT COUNT(*) n FROM mission_action_keys WHERE mission_id=?",
        ("gate-pause",)).fetchone()["n"]
    check(store.last_parked("gate-pause")[1] is None and count == 0,
          "the losing proposal leaves neither an awaiting row nor a duplicate key")
    store.park_for_confirm = original_park
    check(store.resume_paused("gate-pause") == QUEUED and
          drv.advance("gate-pause") == NEEDS_YOU and
          store.last_parked("gate-pause")[1],
          "resume can publish a fresh confirmation inbox")
    store.close(); actions.close()


def test_pause_before_automatic_execution_latch_retires_action():
    print("test_pause_before_automatic_execution_latch_retires_action")
    clear_registry(); calls = []
    register(Capability("auto.send", execute=lambda _r: calls.append(1) or {"sent": True},
                        verify=lambda _r, _x: Verdict(VERIFIED, "sent"),
                        reversible=False, risk="send", semantic_args=("to",)))
    decision = {"action": "auto.send", "args": {"to": "one"}}
    drv, store, actions = _driver([decision, decision, {"action": "done"}])
    create_mission(store, "auto-pause", "send once",
                   leash=world_leash(may=["auto.send"], autonomous=True,
                                     max_irreversible_actions=1,
                                     actions_per_hour=1))
    original_claim = store.claim_execution

    def pause_then_claim(*args, **kwargs):
        store.pause("auto-pause")
        return original_claim(*args, **kwargs)

    store.claim_execution = pause_then_claim
    check(drv.advance("auto-pause") == PAUSED and not calls,
          "pause before the execution latch prevents the automatic side effect")
    count = store.db.execute(
        "SELECT COUNT(*) n FROM mission_action_keys WHERE mission_id=?",
        ("auto-pause",)).fetchone()["n"]
    check(count == 0, "the never-fired automatic proposal releases its semantic key")
    store.claim_execution = original_claim
    check(store.resume_paused("auto-pause") == QUEUED and
          drv.advance("auto-pause") == NEEDS_YOU and calls == [1],
          "resume may safely execute one fresh proposal without consuming a no-fire quota")
    store.close(); actions.close()


def test_confirmed_action_waits_when_shared_resource_is_busy():
    print("test_confirmed_action_waits_when_shared_resource_is_busy")
    clear_registry(); calls = []
    register(Capability("social.post", execute=lambda _r: calls.append(1) or {"posted": True},
                        verify=lambda _r, _x: Verdict(VERIFIED, "posted"),
                        reversible=False, risk="publish", resource="browser-profile",
                        semantic_args=("target",)))
    drv, store, actions = _driver([
        {"action": "social.post", "args": {"target": "one"}},
        {"action": "done", "reason": "posted"}])
    create_mission(store, "busy-confirm", "post once",
                   leash=world_leash(may=["social.post"], autonomous=False))
    check(drv.advance("busy-confirm") == NEEDS_YOU, "post parked for confirmation")
    _name, nonce = store.last_parked("busy-confirm")
    blocker = store.claim_resource("browser-profile", "other-mission")
    check(drv.confirm_and_resume("busy-confirm", nonce) == WAITING and not calls,
          "a busy browser becomes a durable wait, never stranded RUNNING")
    check(actions.get(nonce).state == "approved",
          "the exact confirmed payload remains approved for retry")
    store.release_resource("browser-profile", "other-mission", blocker)
    check(drv.tick_missions(10**11) == 1 and calls == [1],
          "wake retries the same approved nonce exactly once")
    store.close(); actions.close()


def test_cancel_serializes_against_a_new_run_claim():
    print("test_cancel_serializes_against_a_new_run_claim")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    s1, s2 = MissionStore(p), MissionStore(p)
    create_mission(s1, "cancel-race", "do not start", leash=world_leash())
    selected, release = threading.Event(), threading.Event()

    def trace(sql):
        if sql.startswith("UPDATE missions SET state") and not selected.is_set():
            selected.set(); release.wait(2)

    s1.db.set_trace_callback(trace)
    cancelled = threading.Thread(target=lambda: s1.cancel("cancel-race"))
    claimed = []
    cancelled.start(); check(selected.wait(1), "cancel holds its write transaction")
    contender = threading.Thread(target=lambda: claimed.append(s2.claim_run("cancel-race")))
    contender.start()
    check(not claimed, "a daemon cannot interleave a run claim inside cancellation")
    release.set(); cancelled.join(2); contender.join(2)
    check(claimed == [None] and s2.get("cancel-race").state == CANCELLED,
          "the losing daemon cannot acquire a live token after cancellation")
    s1.close(); s2.close()


def test_reconcile_has_one_cleanup_owner():
    print("test_reconcile_has_one_cleanup_owner")
    drv, store, actions = _driver([])
    create_mission(store, "reconcile-owner", "recover", leash=world_leash())
    token = store.claim_run("reconcile-owner", lease_s=-1)
    check(bool(token) and store.recover_stale_runs() == 1,
          "mission entered recovery_required")
    one = store.begin_reconcile("reconcile-owner", "inspected")
    two = store.begin_reconcile("reconcile-owner", "second caller")
    check(bool(one) and two is None,
          "only one concurrent reconciler owns cross-database cleanup")
    check(not store.finish_reconcile("reconcile-owner", "wrong-token") and
          store.get("reconcile-owner").state == RECONCILING,
          "a losing reconciler cannot publish queued or delete leases")
    check(store.finish_reconcile("reconcile-owner", one) and
          store.get("reconcile-owner").state == QUEUED,
          "the exact cleanup owner may finish reconciliation")
    store.close(); actions.close()


def test_recovery_fences_late_reservations_and_key_aba():
    print("test_recovery_fences_late_reservations_and_key_aba")
    drv, store, actions = _driver([])
    create_mission(store, "reservation-fence", "recover safely",
                   leash=world_leash(may=["social.like"], autonomous=True))
    old_run = store.claim_run("reservation-fence")
    leash = store.get("reservation-fence").leash
    old_ok, _why, _retry = store.reserve_action(
        "reservation-fence", "same-key", True, leash, "social.like",
        {"url": "https://social.test/p/1"}, old_run)
    with store._lock:
        store.db.execute(
            "UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
            (RECOVERY_REQUIRED, "reservation-fence"))
        store.db.commit()
    recovery_owner = store.begin_reconcile("reservation-fence", "inspected")
    late_ok, _why2, _retry2 = store.reserve_action(
        "reservation-fence", "late-key", True, leash, "social.like",
        {"url": "https://social.test/p/2"}, old_run)
    check(old_ok and not late_ok,
          "a stale driver cannot reserve after the recovery fence is published")
    check(store.finish_reconcile("reservation-fence", recovery_owner),
          "recovery owner publishes queued after clearing the old reservation")
    fresh_run = store.claim_run("reservation-fence")
    fresh_ok, _why3, _retry3 = store.reserve_action(
        "reservation-fence", "same-key", True, leash, "social.like",
        {"url": "https://social.test/p/1"}, fresh_run)
    hijacked = store.bind_action_key(
        "reservation-fence", "same-key", "old-nonce", old_run)
    released = store.release_action_key("reservation-fence", "same-key", old_run)
    row = store.db.execute(
        "SELECT owner_token,nonce,state FROM mission_action_keys "
        "WHERE mission_id=? AND action_key=?",
        ("reservation-fence", "same-key")).fetchone()
    check(fresh_ok and not hijacked and not released and row and
          row["owner_token"] == fresh_run and row["nonce"] == "" and
          row["state"] == "reserved",
          "an expired run cannot bind or release a fresh run's same-key reservation")
    store.close(); actions.close()


def test_legacy_running_row_requires_reconciliation():
    print("test_legacy_running_row_requires_reconciliation")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE missions(mission_id TEXT PRIMARY KEY, goal TEXT, "
               "leash_json TEXT, case_json TEXT, state TEXT, result TEXT, "
               "created_at INTEGER, updated_at INTEGER)")
    db.execute("INSERT INTO missions VALUES(?,?,?,?,?,?,?,?)",
               ("legacy-run", "uncertain", "{}", "{}", "running", "", 1, 1))
    db.commit(); db.close()
    store = MissionStore(p)
    check(store.get("legacy-run").state == RECOVERY_REQUIRED,
          "pre-token RUNNING rows do not remain permanently stuck or blindly replay")
    store.close()


def test_browser_target_change_refuses_confirmed_click():
    print("test_browser_target_change_refuses_confirmed_click")
    clear_registry()
    fake = FakeActuator(result_url="https://social.test/post/new")
    register_primitives(stub=False, actuator=fake,
                        browse_runner=lambda _g: "prepared")
    drv, store, actions = _driver([
        {"action": "browse.submit", "args": {"button": "Publish"}}])
    create_mission(store, "target", "publish one post", leash=world_leash())
    store.record_event("target", "result", "browse",
                       payload={"verdict": VERIFIED, "reason": "exact form reread"})
    check(drv.advance("target") == NEEDS_YOU, "snapshotted publish parked")
    _name, nonce = store.last_parked("target")
    rec = actions.get(nonce)
    check(rec.snapshot.get("url") == "https://social.test/post/new" and
          rec.snapshot.get("ref") == "e1", "approval binds page and exact element")
    fake._url = "https://social.test/different-account"
    check(drv.confirm_and_resume("target", nonce) == NEEDS_YOU,
          "changed target refuses rather than clicking a same-named button")
    check(not any(c[0] == "click_ref" for c in fake.calls),
          "TOCTOU refusal fired no browser click")
    check(store.last_parked("target")[1] is None,
          "changed approval is retired so the mission can re-prepare")
    store.close(); actions.close()


def test_shared_browser_resource_serializes_missions():
    print("test_shared_browser_resource_serializes_missions")
    clear_registry()
    entered, release, calls = threading.Event(), threading.Event(), []

    def browser_work(rec):
        calls.append(rec.job_id); entered.set(); release.wait(3)
        return {"ok": True}

    register(Capability("social.prepare", execute=browser_work,
                        verify=lambda _r, _x: Verdict(VERIFIED, "prepared"),
                        reversible=True, risk="read", resource="browser-profile"))
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = MissionStore(p + ".missions")
    actions = ActionStore(p + ".actions")
    decision = {"action": "social.prepare", "args": {}}
    d1 = MissionDriver(store, actions, Scripted([decision, {"action": "done"}]))
    d2 = MissionDriver(store, actions, Scripted([decision]))
    leash = world_leash(may=["social.prepare"], autonomous=True)
    create_mission(store, "one", "first", leash=leash)
    create_mission(store, "two", "second", leash=leash)
    th = threading.Thread(target=lambda: d1.advance("one")); th.start()
    check(entered.wait(2), "first mission owns the shared browser profile")
    check(d2.advance("two") == WAITING and calls == ["one"],
          "second mission backs off instead of driving the account concurrently")
    release.set(); th.join(3)
    store.close(); actions.close()


def test_irreversible_actions_are_deduplicated_and_rate_limited():
    print("test_irreversible_actions_are_deduplicated_and_rate_limited")
    clear_registry(); fired = []
    register(Capability("social.like", execute=lambda r: fired.append(r.args["url"]) or {"ok": True},
                        verify=lambda _r, _x: Verdict(VERIFIED, "liked"),
                        reversible=False, risk="publish", semantic_args=("url",)))
    same_a = {"action": "social.like", "args": {
        "url": "https://social.test/p/1", "idempotency_key": "attempt-a"}}
    same_b = {"action": "social.like", "args": {
        "url": "https://social.test/p/1", "idempotency_key": "attempt-b"}}
    drv, store, actions = _driver([same_a, same_b])
    create_mission(store, "dedupe", "like once",
                   leash=world_leash(may=["social.like"], autonomous=True))
    check(drv.advance("dedupe") == NEEDS_YOU and fired == ["https://social.test/p/1"],
          "same campaign+target+operation cannot fire twice")
    check("duplicate external action blocked" in store.get("dedupe").result,
          "duplicate prevention is explicit in Mission status")
    store.close(); actions.close()

    fired.clear()
    first = {"action": "social.like", "args": {"url": "https://social.test/p/1"}}
    second = {"action": "social.like", "args": {"url": "https://social.test/p/2"}}
    drv, store, actions = _driver([first, second])
    create_mission(store, "rate", "pace likes",
                   leash=world_leash(may=["social.like"], autonomous=True,
                                     actions_per_hour=1))
    check(drv.advance("rate") == WAITING and fired == ["https://social.test/p/1"],
          "durable hourly cap paces distinct external actions across advances")
    store.close(); actions.close()


def test_browser_dedupe_ignores_ephemeral_tab_and_element_refs():
    print("test_browser_dedupe_ignores_ephemeral_tab_and_element_refs")
    clear_registry(); fired = []; snapshots = iter([
        {"url": "https://social.test/compose", "tab_id": "tab-a",
         "button": "Publish", "target": "[e1] button Publish",
         "form_digest": "same-form"},
        {"url": "https://social.test/compose", "tab_id": "tab-b",
         "button": "Publish", "target": "[e9] button Publish",
         "form_digest": "same-form"},
    ])
    register(Capability(
        "social.publish", execute=lambda _r: fired.append(1) or {"ok": True},
        verify=lambda _r, _x: Verdict(VERIFIED, "published"),
        snapshot=lambda _args, _mid: next(snapshots),
        reversible=False, risk="publish", semantic_args=("text",)))
    decision_a = {"action": "social.publish", "args": {
        "text": "same post", "success_text": "Posted", "expect_title": "Draft"}}
    decision_b = {"action": "social.publish", "args": {
        "text": "same post", "success_url_contains": "/published",
        "expect_title": "Published", "untrusted_attempt_label": "second"}}
    drv, store, actions = _driver([decision_a, decision_b])
    create_mission(store, "stable-target", "publish once",
                   leash=world_leash(may=["social.publish"], autonomous=True))
    check(drv.advance("stable-target") == NEEDS_YOU and fired == [1] and
          "duplicate external action blocked" in store.get("stable-target").result,
          "tab/ref or verification hints cannot bypass semantic duplicate protection")
    store.close(); actions.close()


def test_registered_semantic_projection_canonicalizes_aliases():
    print("test_registered_semantic_projection_canonicalizes_aliases")
    clear_registry(); register_primitives(stub=True)
    first = {"action": "web.send", "args": {
        "to": "alice", "text": "hello", "selector": "#message",
        "send": "#go", "attempt_note": "first"}}
    second = {"action": "web.send", "args": {
        "to": "display-only-bob", "text": "hello", "message_selector": "#message",
        "send_selector": "#go", "attempt_note": "second"}}
    drv, store, actions = _driver([first, second])
    create_mission(store, "canonical-aliases", "send once",
                   leash=world_leash(may=["web.send"], autonomous=True))
    state = drv.advance("canonical-aliases")
    fired = [r for r in actions.receipts() if r.get("fired")]
    check(state == NEEDS_YOU and len(fired) == 1 and
          "duplicate external action blocked" in store.get("canonical-aliases").result,
          "ignored metadata and executor aliases cannot create a second send identity")
    store.close(); actions.close()


def test_irreversible_capability_without_semantic_projection_fails_closed():
    print("test_irreversible_capability_without_semantic_projection_fails_closed")
    clear_registry(); fired = []
    register(Capability(
        "unsafe.send", execute=lambda _r: fired.append(1) or {"sent": True},
        verify=lambda _r, _x: Verdict(VERIFIED, "sent"),
        reversible=False, risk="send"))
    drv, store, actions = _driver([
        {"action": "unsafe.send", "args": {"text": "hello", "junk": "vary-me"}}])
    create_mission(store, "missing-projection", "do not weaken dedupe",
                   leash=world_leash(may=["unsafe.send"], autonomous=True))
    check(drv.advance("missing-projection") == FAILED_S and not fired and not actions.list() and
          "semantic_args projection" in store.get("missing-projection").result,
          "an undeclared irreversible identity cannot reach ActionStore or execution")
    store.close(); actions.close()


def test_transient_model_failure_becomes_durable_backoff():
    print("test_transient_model_failure_becomes_durable_backoff")
    class Temporary:
        def complete(self, *_a, **_kw):
            return Completion(stop_reason="error", error_status=429,
                              error_detail="rate limit; retry later")

    decision = ModelDecider(Temporary())("goal", {"_recent_events": []}, [])
    check(decision["action"] == "wait" and decision["args"]["seconds"] >= 60 and
          decision["args"]["transient"],
          "temporary provider outage schedules durable exponential backoff")


def test_model_decider_exposes_unambiguous_primitive_contracts():
    print("test_model_decider_exposes_unambiguous_primitive_contracts")

    class Capture:
        def __init__(self):
            self.system = ""
            self.user = ""

        def complete(self, system, messages, _tools):
            self.system = system
            self.user = messages[0]["content"]
            return Completion(text='{"action":"needs_human","args":{"summary":"done"}}')

    provider = Capture()
    primitive = {"name": "compose", "reversible": True,
                 "description": "Create final ready-to-use copy.",
                 "args": '{"facts","instruction","text (final literal only)"}'}
    ModelDecider(provider)("prepare a campaign", {}, [primitive])
    check("args.instruction" in provider.system and "ONLY" in provider.system and
          "LITERAL substring" in provider.system and "one separate read-only 'browse'" in
          provider.system and "final literal only" in provider.user,
          "the planner sees unambiguous compose and browser-observation contracts")


def test_credentials_handoff_before_any_durable_action_payload():
    print("test_credentials_handoff_before_any_durable_action_payload")
    clear_registry(); register_primitives(stub=True)
    drv, store, actions = _driver([{
        "action": "browse",
        "args": {"goal": "fill the signup form",
                 "expect": {"Email": "owner@example.test", "Password": "hunter2"}}
    }])
    create_mission(store, "secret", "register an account",
                   leash=world_leash(autonomous=True))
    check(drv.advance("secret") == NEEDS_YOU and not actions.list(),
          "credentials trigger a human browser handoff before ActionStore persistence")
    check("without persisting" in store.get("secret").result,
          "the handoff explains the privacy boundary")
    store.close(); actions.close()


def test_missing_authorization_defers_only_its_branch():
    print("test_missing_authorization_defers_only_its_branch")
    clear_registry(); register_primitives(stub=True)
    ask = {"action": "needs_authorization", "args": {
        "kind": "profile_claim", "claim": "age_at_least_16",
        "risk": "medium", "domain": "producthunt.com",
        "summary": "Confirm that the account holder is at least 16"}}
    drv, store, actions = _driver([ask, R, {"action": "done", "reason": "other work done"}])
    authority = {
        "auto_apply_profile_claims": False,
        "defer_missing_authorizations": True,
        "max_auto_risk": "medium", "claims": {}, "never_auto": []}
    create_mission(store, "branch-auth", "prepare a launch",
                   leash=world_leash(autonomous=True))
    with patch("harness.mission._standing_authority", return_value=authority):
        state = drv.advance("branch-auth")
    case = store.get("branch-auth").case
    check(state == NEEDS_YOU and case.get("researched"),
          "an authorization wait does not stop independent research")
    check(case.get("pending_authorizations", [])[0]["claim"] == "age_at_least_16",
          "the branch-scoped authorization request remains durable")
    store.close(); actions.close()


def test_exact_saved_profile_claim_is_reused_automatically():
    print("test_exact_saved_profile_claim_is_reused_automatically")
    clear_registry(); register_primitives(stub=True)
    ask = {"action": "needs_authorization", "args": {
        "kind": "profile_claim", "claim": "age_at_least_16",
        "risk": "medium", "domain": "producthunt.com",
        "summary": "Confirm that the account holder is at least 16"}}
    drv, store, actions = _driver([ask, R, HAND])
    authority = {
        "auto_apply_profile_claims": True,
        "defer_missing_authorizations": True,
        "max_auto_risk": "medium",
        "claims": {"age_at_least_16": True}, "never_auto": []}
    create_mission(store, "saved-fact", "prepare a launch",
                   leash=world_leash(autonomous=True))
    with patch("harness.mission._standing_authority", return_value=authority):
        state = drv.advance("saved-fact")
    case = store.get("saved-fact").case
    check(state == NEEDS_YOU and case.get("researched"),
          "an exact confirmed profile fact authorizes the matching form and work continues")
    check(case.get("resolved_authorizations", [])[0]["resolution"] == "standing_authority" and
          not case.get("pending_authorizations"),
          "the reused fact has a durable non-secret authorization receipt")
    store.close(); actions.close()


def test_person_required_security_checks_never_auto_authorize():
    print("test_person_required_security_checks_never_auto_authorize")
    clear_registry(); register_primitives(stub=True)
    ask = {"action": "needs_authorization", "args": {
        "kind": "captcha", "risk": "low", "blocking": True,
        "summary": "Complete the human verification challenge"}}
    drv, store, actions = _driver([ask])
    authority = {
        "auto_apply_profile_claims": True,
        "defer_missing_authorizations": True,
        "max_auto_risk": "medium",
        "claims": {"age_at_least_16": True}, "never_auto": []}
    create_mission(store, "captcha-boundary", "finish signup",
                   leash=world_leash(autonomous=True))
    with patch("harness.mission._standing_authority", return_value=authority):
        state = drv.advance("captcha-boundary")
    check(state == NEEDS_YOU and store.get("captcha-boundary").case.get(
          "pending_authorizations", [])[0]["kind"] == "captcha",
          "CAPTCHA remains a person-required boundary regardless of the standing risk ceiling")
    store.close(); actions.close()


def test_world_leash_normalizes_epoch_expiry_for_runtime_comparison():
    print("test_world_leash_normalizes_epoch_expiry_for_runtime_comparison")
    leash = world_leash(expires=1786495837)
    check(leash["expires"] == "2026-08-12T00:50:37Z",
          "epoch deadlines normalize to the UTC string used by leash evaluation")
    check(isinstance(leash["expires"], str),
          "an epoch never survives the builder to cause a str-vs-int runtime failure")

def main():
    test_confirm_gate_then_resume()
    test_autonomous_with_durable_wait()
    test_leash_denies_out_of_scope()
    test_anti_poll_spin()
    test_distinct_observe_targets_are_not_poll_backoff()
    test_model_context_keeps_newest_results_and_per_site_browse_facts()
    test_model_context_preserves_complete_latest_human_update()
    test_local_compose_work_is_not_treated_as_polling()
    test_browse_submit_requires_latest_verified_preparation()
    test_browse_mission_gates_publish()
    test_code_step_in_a_mission()
    test_pause_preserves_due_wait_and_cancel_is_terminal()
    test_cancel_revokes_a_parked_action()
    test_run_claim_prevents_two_drivers_and_cancel_beats_stale_worker()
    test_old_mission_database_migrates_in_place()
    test_expired_runner_heartbeat_recovers_without_blind_replay()
    test_pause_waits_for_an_inflight_action_boundary()
    test_pause_during_confirmed_action_does_not_reenter()
    test_pause_after_approval_retires_unfired_key()
    test_pause_before_confirm_inbox_publish_is_atomic()
    test_pause_before_automatic_execution_latch_retires_action()
    test_confirmed_action_waits_when_shared_resource_is_busy()
    test_cancel_serializes_against_a_new_run_claim()
    test_reconcile_has_one_cleanup_owner()
    test_recovery_fences_late_reservations_and_key_aba()
    test_legacy_running_row_requires_reconciliation()
    test_browser_target_change_refuses_confirmed_click()
    test_shared_browser_resource_serializes_missions()
    test_irreversible_actions_are_deduplicated_and_rate_limited()
    test_browser_dedupe_ignores_ephemeral_tab_and_element_refs()
    test_registered_semantic_projection_canonicalizes_aliases()
    test_irreversible_capability_without_semantic_projection_fails_closed()
    test_transient_model_failure_becomes_durable_backoff()
    test_model_decider_exposes_unambiguous_primitive_contracts()
    test_credentials_handoff_before_any_durable_action_payload()
    test_world_leash_normalizes_epoch_expiry_for_runtime_comparison()
    if _fails:
        print(f"\n{len(_fails)} FAILED")
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
