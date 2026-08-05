"""The Inbox: one record, answerable from anywhere, exactly once.

The races are the point. Two surfaces can hold the same question at the same moment —
a desktop dialog and a phone — and whichever answers first has to be the one that counts,
with the loser told nothing happened rather than handed an error.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from harness.gate import Decision, Outcome
from harness.inbox import (
    R_ALLOW, R_ALWAYS, R_DENY, R_NEVER, STATE_PENDING, STATE_RESOLVED,
    VIS_INBOX, VIS_INLINE, InboxStore, args_preview, inbox_approver, outcome_of,
)


@pytest.fixture()
def store(tmp_path):
    s = InboxStore(str(tmp_path / "inbox.db"))
    yield s
    s.close()


def _d(**kw):
    kw.setdefault("allowed", False)
    kw.setdefault("needs_user", True)
    return Decision(**kw)


# -- the state machine ------------------------------------------------------
def test_resolve_once_first_responder_wins(store):
    item = store.add("s1", tool="browser_click")
    assert store.resolve(item.id, R_ALLOW) is True
    assert store.resolve(item.id, R_DENY) is False, "a second answer must not overwrite"
    assert store.get(item.id).resolution == R_ALLOW
    assert store.get(item.id).state == STATE_RESOLVED


def test_resolve_unknown_item_is_false_not_an_error(store):
    assert store.resolve("nope", R_ALLOW) is False


def test_concurrent_answers_produce_exactly_one_winner(store):
    """Two surfaces racing. Exactly one True, and the stored answer is that one's."""
    item = store.add("s1", tool="browser_click")
    results, start = [], threading.Event()

    def answer(resolution):
        start.wait()
        results.append((resolution, store.resolve(item.id, resolution)))

    ts = [threading.Thread(target=answer, args=(r,))
          for r in (R_ALLOW, R_DENY, R_ALWAYS, R_NEVER)]
    for t in ts:
        t.start()
    start.set()
    for t in ts:
        t.join(5)
    winners = [r for r, won in results if won]
    assert len(winners) == 1, results
    assert store.get(item.id).resolution == winners[0]


def test_wait_returns_when_another_thread_answers(store):
    item = store.add("s1", tool="browser_click")

    def answer():
        time.sleep(0.05)
        store.resolve(item.id, R_ALLOW)

    threading.Thread(target=answer, daemon=True).start()
    assert store.wait(item.id, timeout=5) == R_ALLOW


def test_wait_returns_immediately_if_already_resolved(store):
    """The durable-resume case: a restart re-raises a prompt that was answered while the
    process was gone. It must not block for an answer that already exists."""
    item = store.add("s1", tool="browser_click")
    store.resolve(item.id, R_ALLOW)
    t0 = time.time()
    assert store.wait(item.id, timeout=5) == R_ALLOW
    assert time.time() - t0 < 1


def test_wait_times_out_to_empty(store):
    item = store.add("s1", tool="browser_click")
    assert store.wait(item.id, timeout=0.05) == ""
    assert store.get(item.id).state == STATE_PENDING     # a timeout decides nothing


# -- idempotency ------------------------------------------------------------
def test_same_call_id_reuses_the_item(store):
    a = store.add("s1", tool="browser_click", call_id="c1")
    b = store.add("s1", tool="browser_click", call_id="c1")
    assert a.id == b.id, "a reconnecting surface must not ask the same question twice"
    assert len(store.pending("s1")) == 1


def test_same_call_id_returns_the_resolved_item(store):
    a = store.add("s1", tool="browser_click", call_id="c1")
    store.resolve(a.id, R_ALLOW)
    b = store.add("s1", tool="browser_click", call_id="c1")
    assert b.id == a.id and not b.pending and b.resolution == R_ALLOW


def test_blank_call_ids_do_not_collide(store):
    """The unique index is partial — items without a call id are independent."""
    a = store.add("s1", tool="x")
    b = store.add("s1", tool="x")
    assert a.id != b.id


def test_call_ids_are_scoped_per_session(store):
    a = store.add("s1", tool="x", call_id="c1")
    b = store.add("s2", tool="x", call_id="c1")
    assert a.id != b.id


# -- persistence & orphans --------------------------------------------------
def test_survives_a_reopen(tmp_path):
    p = str(tmp_path / "i.db")
    s1 = InboxStore(p)
    item = s1.add("s1", tool="browser_click", call_id="c1")
    s1.close()
    s2 = InboxStore(p)
    try:
        got = s2.get(item.id)
        assert got is not None and got.pending and got.tool == "browser_click"
    finally:
        s2.close()


def test_orphans_are_closed_when_a_run_ends(store):
    store.add("s1", tool="a")
    store.add("s1", tool="b")
    store.add("s2", tool="c")
    assert store.resolve_session("s1") == 2
    assert not store.pending("s1")
    assert len(store.pending("s2")) == 1


def test_reconcile_on_resume_separates_waiting_from_decided(store):
    a = store.add("s1", tool="a")
    store.add("s1", tool="b")
    store.resolve(a.id, R_ALLOW)
    out = store.reconcile_on_resume("s1")
    assert [i["tool"] for i in out["pending"]] == ["b"]
    assert [i["tool"] for i in out["recap"]] == ["a"]


def test_visibility_filters(store):
    store.add("s1", tool="a", visibility=VIS_INLINE)
    store.add("s1", tool="b", visibility=VIS_INBOX)
    assert [i.tool for i in store.list(visibility=VIS_INBOX)] == ["b"]


# -- notification hook ------------------------------------------------------
def test_on_new_fires_once_per_new_item(tmp_path):
    seen = []
    s = InboxStore(str(tmp_path / "i.db"), on_new=seen.append)
    try:
        s.add("s1", tool="a", call_id="c1")
        s.add("s1", tool="a", call_id="c1")      # the same question, not a new one
        assert len(seen) == 1
    finally:
        s.close()


def test_a_broken_notifier_does_not_break_the_run(tmp_path):
    def boom(_item):
        raise RuntimeError("the phone is off")

    s = InboxStore(str(tmp_path / "i.db"), on_new=boom)
    try:
        assert s.add("s1", tool="a").pending
    finally:
        s.close()


# -- resolutions -> outcomes ------------------------------------------------
def test_known_resolutions_map():
    assert outcome_of(R_ALLOW) is Outcome.ALLOW_ONCE
    assert outcome_of(R_ALWAYS) is Outcome.ALLOW_ALWAYS
    assert outcome_of(R_NEVER) is Outcome.REJECT_ALWAYS
    assert outcome_of(R_DENY) is Outcome.REJECT_ONCE


@pytest.mark.parametrize("junk", ["", "maybe", "ALLOW", "yes", None, "orphaned"])
def test_anything_unrecognised_is_a_refusal(junk):
    """Consent is stated, never inferred. A garbled reply, a stale value, a closed run —
    none of them mean go ahead."""
    assert outcome_of(junk) is Outcome.REJECT_ONCE


# -- the approver -----------------------------------------------------------
def test_approver_parks_and_suspends_until_answered(store):
    approve = inbox_approver(store, "s1")
    out = {}

    def run():
        out["v"] = approve("browser_click", {"ref": "e1"}, _d(target="https://x.test",
                                                              call_id="c1"))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(100):                     # wait for the item to appear
        if store.pending("s1"):
            break
        time.sleep(0.01)
    items = store.pending("s1")
    assert len(items) == 1 and items[0].tool == "browser_click"
    assert items[0].target == "https://x.test"
    store.resolve(items[0].id, R_ALWAYS)
    t.join(5)
    assert out["v"] is Outcome.ALLOW_ALWAYS


def test_approver_returns_at_once_for_an_already_answered_call(store):
    """Restart semantics: the item exists and is resolved, so no new question is asked."""
    item = store.add("s1", tool="browser_click", call_id="c1")
    store.resolve(item.id, R_ALLOW)
    approve = inbox_approver(store, "s1")
    assert approve("browser_click", {}, _d(call_id="c1")) is Outcome.ALLOW_ONCE
    assert len(store.list(session="s1")) == 1, "no duplicate question was created"


def test_approver_timeout_refuses_and_closes_the_item(store):
    approve = inbox_approver(store, "s1", timeout=0.05)
    assert approve("browser_click", {}, _d(call_id="c1")) is Outcome.REJECT_ONCE
    assert not store.pending("s1"), "a dead question must not keep showing as live"


def test_approver_records_what_a_rule_would_be(store):
    approve = inbox_approver(store, "s1", timeout=0.05)
    approve("browser_click", {}, _d(target="https://x.test",
                                    rule_offer="browser_click → https://x.test",
                                    call_id="c1"))
    it = store.list(session="s1")[0]
    assert it.rule_offer == "browser_click → https://x.test"


# -- preview ----------------------------------------------------------------
def test_args_preview_shows_what_not_just_the_tool_name():
    p = args_preview({"text": "hello there", "submit": True})
    assert "text: hello there" in p
    assert "submit: true" in p          # non-strings render as JSON, so booleans lowercase


def test_args_preview_truncates():
    p = args_preview({"content": "x" * 5000})
    assert len(p) <= 240


def test_args_preview_does_not_go_looking_for_real_values():
    """The loop hands over placeholder-form args on purpose; the preview only shortens."""
    assert "{{SECRET:deadbeef}}" in args_preview({"text": "{{SECRET:deadbeef}}"})
