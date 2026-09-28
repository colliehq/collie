"""The Inbox: one record, answerable from anywhere, exactly once.

The races are the point. Two surfaces can hold the same question at the same moment —
a desktop dialog and a phone — and whichever answers first has to be the one that counts,
with the loser told nothing happened rather than handed an error.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


def test_add_and_read_ignore_columns_from_a_newer_inbox_schema(store):
    store.db.execute("ALTER TABLE inbox_items ADD COLUMN future_detail TEXT NOT NULL DEFAULT ''")
    store.db.commit()

    item = store.add("s1", tool="generate_image", call_id="future-schema")

    assert item.pending
    assert store.get(item.id).tool == "generate_image"


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


# -- cancel closes the store while other threads still hold it ---------------
# Cancelling a web run calls Handler._inbox_close, which closes this store's SQLite
# connection. The web UI's approval poll (/api/approvals, the session replay) and the
# run's own parked approver may be holding the same store at that moment, on other
# threads. sqlite3 releases the GIL while it steps a query, so a close that lands
# mid-read frees the connection underneath the reader: a native access violation that
# kills the whole Collie process, not an exception anything could catch.
_POLL_RACE = r'''
import sys, threading
from harness.inbox import InboxStore
path, iterations = sys.argv[1], int(sys.argv[2])
for n in range(iterations):
    store = InboxStore(path)
    ready, stop, errors = threading.Event(), threading.Event(), []
    def poll():
        ready.set()
        while not stop.is_set():
            try:
                store.pending("cancel-test")
            except Exception as exc:
                errors.append(repr(exc))
                return
    reader = threading.Thread(target=poll)
    reader.start()
    ready.wait()
    store.close()
    stop.set()
    reader.join(5)
    if reader.is_alive():
        sys.exit("iteration %d: the poll never returned" % n)
    if errors:
        sys.exit("iteration %d: the poll raised %s" % (n, errors[0]))
'''

#: Before the fix a single iteration crashed 30 runs out of 30 on Windows; a hundred leaves
#: no realistic chance of a lucky pass while still finishing in well under a second.
POLL_RACE_ITERATIONS = 100


def test_closing_the_store_under_a_polling_thread_does_not_kill_the_process(tmp_path):
    # Out of process on purpose: the failure is a crash of the interpreter itself, which
    # would take pytest down with it rather than fail one test.
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", _POLL_RACE,
         str(tmp_path / "race.db"), str(POLL_RACE_ITERATIONS)],
        cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=120)
    assert result.returncode == 0, (
        "closing the inbox under a live poll ended the process with exit status %d "
        "(0xC0000005 / -11 is a native access violation)\n%s"
        % (result.returncode, result.stderr[-2000:]))


def test_a_poll_still_holding_a_closed_store_gets_nothing_rather_than_an_error(store):
    """The web handler looks the store up, lets go of the registry lock, then reads. A
    cancel in between leaves it holding a closed store; that must read as "nothing is
    waiting any more", not as a 500."""
    item = store.add("s1", tool="browser_click")
    store.close()
    assert store.pending("s1") == []
    assert store.list() == []
    assert store.get(item.id) is None
    assert store.resolve(item.id, R_ALLOW) is False, "an answer after cancel changes nothing"
    assert store.resolve_session("s1") == 0
    store.close()                                  # and closing twice is harmless


def test_cancel_releases_a_parked_approval_as_a_refusal(store):
    """A cancel must wake a run parked on an approval and have it refuse, even if the
    store is closed before anyone answered or orphaned the question."""
    approve = inbox_approver(store, "s1")
    out = []
    parked = threading.Thread(
        target=lambda: out.append(approve("browser_click", {"ref": "e1"}, _d(call_id="c1"))),
        daemon=True)
    parked.start()
    for _ in range(200):
        if store.pending("s1"):
            break
        time.sleep(0.01)
    [item] = store.pending("s1")
    store.close()
    parked.join(2)
    assert not parked.is_alive(), "the run stayed parked on a question nobody can answer"
    assert out == [Outcome.REJECT_ONCE]
    assert store.resolve(item.id, R_ALLOW) is False


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


# -- a script is approved whole, so its card shows all of it ----------------
def _fifteen_harmless_steps_then_a_delete():
    return {"steps": [{"action": "snapshot", "max": 100}] * 15 +
                     [{"action": "click", "text": "Delete board"}]}


def _approve_and_capture(store, tool, args, also=None):
    """Run one approval through the real approver; answer it (deny) from on_new."""
    seen = []

    def answer(item):
        seen.append(item)
        if also is not None:
            also(item)
        store.resolve(item.id, R_DENY)

    store.on_new = answer
    assert inbox_approver(store, "batch")(tool, args, _d()) is Outcome.REJECT_ONCE
    [item] = seen
    return item


@pytest.mark.parametrize("tool", ["browser_script", "desktop_script"])
def test_a_script_approval_card_shows_every_step(store, tool):
    """A script runs every step on one approval. Cutting each value at 80 characters left
    the sixteenth step (the delete) off the card the person was approving."""
    args = _fifteen_harmless_steps_then_a_delete()
    item = _approve_and_capture(store, tool, args)
    assert "Delete board" in item.body
    assert json.loads(item.body) == args, "the card must carry the proposal exactly"


def test_a_single_call_approval_card_keeps_the_compact_preview(store):
    item = _approve_and_capture(store, "browser_type", {"text": "x" * 5000, "submit": True})
    assert len(item.body) <= 240 and "submit: true" in item.body


def _phone_notices(monkeypatch):
    from harness import webapp
    sent = []

    class Remote:
        def notify(self, title, body, session="", thread=""):
            sent.append(body)
    monkeypatch.setattr(webapp, "REMOTE", Remote())
    return sent, (lambda item: webapp.Handler._notify_waiting("batch", item))


def test_the_phone_notice_for_a_script_is_bounded_and_counts_the_steps_it_leaves_out(
        store, monkeypatch):
    """The card carries every step; a push notice cannot, and must not pass for complete.
    Whatever does not fit is counted, so fifteen snapshots and a delete never read like
    fifteen snapshots."""
    sent, notify = _phone_notices(monkeypatch)
    _approve_and_capture(store, "browser_script", _fifteen_harmless_steps_then_a_delete(),
                         also=notify)
    [text] = sent
    assert len(text) <= 180, text
    assert text.startswith("browser_script — 16 steps:"), text
    more = re.search(r" … (\d+) more steps$", text)
    assert more, "the notice hid steps without saying so: %r" % text
    shown = text.count("snapshot(") + text.count("click(")
    assert shown >= 1 and shown + int(more.group(1)) == 16, text


def test_a_script_notice_that_fits_says_nothing_is_missing(store, monkeypatch):
    sent, notify = _phone_notices(monkeypatch)
    _approve_and_capture(store, "browser_script",
                         {"steps": [{"action": "click", "text": "Delete board"}]}, also=notify)
    assert sent == ["browser_script — 1 step: click(text: Delete board)"]


def test_the_phone_notice_for_a_long_call_is_bounded_and_says_how_much_is_cut(
        store, monkeypatch):
    sent, notify = _phone_notices(monkeypatch)
    item = _approve_and_capture(store, "browser_type",
                                {"label": "Billing address " * 10, "text": "x" * 5000,
                                 "submit": True}, also=notify)
    [text] = sent
    assert len("browser_type — " + item.body) > 180, "the body must be too long to fit"
    assert len(text) <= 180, text
    more = re.search(r" … (\d+) more chars$", text)
    assert more, "the notice was cut without saying so: %r" % text
    whole = "browser_type — " + item.body
    assert whole.startswith(text[:more.start()])
    assert more.start() + int(more.group(1)) == len(whole)


def test_a_short_call_notice_is_unchanged(store, monkeypatch):
    sent, notify = _phone_notices(monkeypatch)
    _approve_and_capture(store, "browser_click", {"ref": "e1"}, also=notify)
    assert sent == ["browser_click — ref: e1"]


def test_the_desktop_card_lists_every_script_step_on_its_own_line():
    """The body reaching the card is complete; the desktop card must also put it on screen.
    As one unwrapped line of JSON, the steps after the first few sat off to the right."""
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    with open(os.path.join(ROOT, "harness", "webui", "index.html"), encoding="utf-8") as fh:
        page = fh.read()
    fn = re.search(r"\n  function permBody\(d\) \{\n.*?\n  \}\n", page, re.S)
    assert fn, "permBody is missing from index.html"
    assert "esc(permBody(d))" in page
    assert re.search(r"\.ev\.ask \.detail \{[^}]*white-space: pre-wrap", page)
    steps = [{"action": "snapshot"}] * 15 + [{"action": "click", "text": "Delete account"}]
    cases = [
        {"tool": "browser_script", "body": json.dumps({"steps": steps, "space": "work"})},
        {"tool": "browser_click", "body": "ref: e1"},
        {"tool": "browser_script", "body": "not json"},
    ]
    script = fn.group(0) + "\nprocess.stdout.write(JSON.stringify(%s.map(permBody)));" % json.dumps(cases)
    done = subprocess.run([node, "-e", script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    assert done.returncode == 0, done.stderr
    script_card, single, broken = json.loads(done.stdout)
    lines = script_card.split("\n")
    assert lines[0] == 'space: "work"'
    assert len(lines) == 17 and lines[16] == '16. {"action":"click","text":"Delete account"}'
    assert single == "ref: e1" and broken == "not json"
