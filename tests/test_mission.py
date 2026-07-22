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
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.actions import ActionStore  # noqa: E402
from harness.jobs import (  # noqa: E402
    clear_registry, WAITING, NEEDS_YOU, DONE_VERIFIED, DONE_ACCEPTED, FAILED_S,
)
from harness.primitives import register_primitives  # noqa: E402
from harness.mission import (  # noqa: E402
    MissionStore, MissionDriver, create_mission, world_leash,
)

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
    create_mission(store, "c", "fix the retry bug then done", leash=world_leash(autonomous=False))
    st = drv.advance("c")
    check(st == DONE_VERIFIED, f"code step (reversible) auto-runs, then done, got {st}")
    check(store.get("c").case.get("coded") is True, "the mission ran a coding step")
    store.close()
    actions.close()


def main():
    test_confirm_gate_then_resume()
    test_autonomous_with_durable_wait()
    test_leash_denies_out_of_scope()
    test_anti_poll_spin()
    test_browse_mission_gates_publish()
    test_code_step_in_a_mission()
    if _fails:
        print(f"\n{len(_fails)} FAILED")
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
