"""Pin MissionService (harness.missionweb) — the NL-front-door service behind
`collie web`'s mission commands. Deterministic ($0): a scripted decider stands in
for the model, so this tests the goal-in / status-out marshalling and the
confirm/resume plumbing, not the model.

Run: python tests/test_missionweb.py   (exit 0 = all green)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.jobs import clear_registry, NEEDS_YOU, WAITING, DONE_ACCEPTED  # noqa: E402
from harness.missionweb import MissionService  # noqa: E402

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

    st3 = svc.resume(mid)
    check(st3["state"] == DONE_ACCEPTED, "resuming the hand-off accepts it")
    svc.close()


def test_bad_confirm_is_soft_error():
    print("test_bad_confirm_is_soft_error")
    svc = _svc([R, C, P, H])
    st = svc.start("sell my car", autonomous=False)
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


def main():
    test_start_gate_confirm_handoff()
    test_bad_confirm_is_soft_error()
    test_missions_listing()
    if _fails:
        print(f"\n{len(_fails)} FAILED")
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
