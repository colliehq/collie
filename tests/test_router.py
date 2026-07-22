"""Pin the front-door router (harness.router) — the classifying head. Deterministic
($0): a scripted provider stands in for the model, so this tests the routing logic,
thresholds, abstain, prefix override, and the model-unavailable contract.

Run: python tests/test_router.py   (exit 0 = all green)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.router import classify, prefix_override, ModelUnavailable, MISSION_THRESHOLD  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def _ok(text):
    return type("C", (), {"text": text, "stop_reason": "end_turn",
                          "error_status": 0, "error_detail": ""})()


def _err(status, detail):
    return type("C", (), {"text": detail or "", "stop_reason": "error",
                          "error_status": status, "error_detail": detail})()


class Prov:
    """Canned completions. `fail_times` returns an error completion (status/detail)
    for the first N calls, then returns `then` — models a transient overload."""
    def __init__(self, text, error=False, status=0, detail="",
                 fail_times=0, then=None):
        self.text, self.error, self.status, self.detail = text, error, status, detail
        self.fail_times, self.then = fail_times, then
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        if self.fail_times and self.calls <= self.fail_times:
            return _err(self.status, self.detail)
        if self.then is not None:
            return _ok(self.then)
        return _err(self.status, self.detail) if self.error else _ok(self.text)


_NOSLEEP = lambda _s: None


class Boom:
    """A provider that crashes — stands in for auth/network failure."""
    def __init__(self):
        self.calls = 0

    def complete(self, *a):
        self.calls += 1
        raise RuntimeError("network down")


def test_three_kinds():
    print("test_three_kinds")
    d = classify("sell my 2018 corolla, local only", Prov('{"kind":"mission","goal":"sell corolla","confidence":0.95}'))
    check(d["kind"] == "mission" and d["goal"] == "sell corolla" and not d["abstained"],
          "a clear world errand -> mission")
    check(classify("add a --json flag", Prov('{"kind":"code","confidence":0.9}'))["kind"] == "code",
          "a workspace change -> code")
    check(classify("why is this flaky?", Prov('{"kind":"chat","confidence":0.9}'))["kind"] == "chat",
          "a question -> chat")
    check(classify("find me a cheap flight", Prov('{"kind":"chat","confidence":0.6}'))["kind"] == "chat",
          "research/find-out -> chat (epistemic, not a mission)")


def test_mission_threshold_abstains():
    print("test_mission_threshold_abstains")
    low = 0.5
    check(low < MISSION_THRESHOLD, "test assumes 0.5 is below the mission bar")
    d = classify("maybe post this somewhere?", Prov(f'{{"kind":"mission","confidence":{low}}}'))
    check(d["kind"] == "chat" and d["abstained"] is True and d.get("suggested") == "mission",
          "a low-confidence mission abstains to chat and offers to promote")


def test_unparsed_falls_back_to_chat():
    print("test_unparsed_falls_back_to_chat")
    d = classify("hello", Prov("I think this is a chat, friend."))     # model up, but not JSON
    check(d["kind"] == "chat" and d["source"] == "fallback",
          "model up + unusable label -> chat (cheapest working path), marked fallback")


def test_model_unavailable_raises():
    print("test_model_unavailable_raises")
    for prov, why in ((None, "no provider"), (Boom(), "provider crash"),
                      (Prov("", error=True, status=401, detail="unauthorized"), "terminal error")):
        try:
            classify("anything", prov, _sleep=_NOSLEEP)
            check(False, f"{why} must raise ModelUnavailable, did not")
        except ModelUnavailable:
            check(True, why)


def test_transient_overload_retries_then_succeeds():
    print("test_transient_overload_retries_then_succeeds")
    # two 529s, then a good classification -> the front door rides it out
    prov = Prov(None, status=529, detail="overloaded", fail_times=2,
                then='{"kind":"mission","goal":"sell car","confidence":0.9}')
    d = classify("sell my car", prov, retries=3, _sleep=_NOSLEEP)
    check(d["kind"] == "mission", "recovers to the real classification after transient 529s")
    check(prov.calls == 3, f"retried the 2 overloads then succeeded, calls={prov.calls}")


def test_persistent_overload_raises_after_retries():
    print("test_persistent_overload_raises_after_retries")
    prov = Prov("", error=True, status=529, detail="overloaded")   # always 529
    try:
        classify("anything", prov, retries=3, _sleep=_NOSLEEP)
        check(False, "persistent overload must raise ModelUnavailable")
    except ModelUnavailable:
        check(prov.calls == 4, f"tried once + 3 retries then gave up, calls={prov.calls}")


def test_prefix_override_skips_model():
    print("test_prefix_override_skips_model")
    boom = Boom()                                   # would raise if the model were called
    d = classify("/mission sell my car", boom)
    check(d["kind"] == "mission" and d["goal"] == "sell my car" and d["source"] == "override",
          "/mission overrides to mission without a model call")
    check(boom.calls == 0, "an explicit prefix must NOT call the model")
    check(classify("/code fix the bug", boom)["kind"] == "code", "/code -> code")
    check(classify("/chat what is X", boom)["kind"] == "chat", "/chat -> chat")
    check(classify("/delegate book a table", boom)["kind"] == "mission", "/delegate == mission")
    check(prefix_override("no prefix here") is None, "a bare message has no override")


def main():
    test_three_kinds()
    test_mission_threshold_abstains()
    test_unparsed_falls_back_to_chat()
    test_model_unavailable_raises()
    test_transient_overload_retries_then_succeeds()
    test_persistent_overload_raises_after_retries()
    test_prefix_override_skips_model()
    if _fails:
        print(f"\n{len(_fails)} FAILED")
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
