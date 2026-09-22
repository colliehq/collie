"""A state-lock timeout must mean the same thing whoever the other writer is.

``statelock.transaction(path, timeout=T)`` is not advice: every caller in the
product turns ``StateLockTimeout`` into an answer somebody reads — "this bundle
is in use", ``code="locked"`` behind an HTTP 409, ``delete_artifact`` returning
False.  The deadline only ever covered the *OS* lock, though, so the same call
behaved two different ways depending on where the competing writer lived:

    another process   waits T, then reports "locked"
    another thread    waits forever on the process-local RLock

Collie's web server is a ``ThreadingHTTPServer`` and two applies against one
workspace are two threads of one process, so the in-process case is the common
one — and it wedged the request thread instead of answering it.  These tests
contend for real (threads, real lock files, a real artifact store) and assert the
deadline is honoured without giving up re-entrancy, which is the property that
made unconditional waiting look safe in the first place.
"""
import os
import shutil
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import pack_artifacts as pa, statelock

# Long enough that a wrongly-unbounded wait is unmistakable, short enough that a
# regression costs seconds rather than hanging the suite.
HOLD_S = 5.0
BUDGET_S = 0.25


class _Holder:
    """Hold one state lock in a background thread, with a bounded lifetime.

    Bounded on purpose: a regression must FAIL this test, never hang it.  The
    holder lets go by itself after ``HOLD_S``, so the unfixed code path ends up
    acquiring the lock late instead of never — a wrong answer the assertions
    below catch, with no watchdog to maintain.
    """

    def __init__(self, path, hold_s=HOLD_S):
        self.path = path
        self.hold_s = hold_s
        self.entered = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            with statelock.transaction(self.path):
                self.entered.set()
                self.done.wait(self.hold_s)
        except Exception as exc:                 # pragma: no cover - failure path
            self.error = exc
            self.entered.set()

    def __enter__(self):
        self.thread.start()
        assert self.entered.wait(10), "the background holder never took the lock"
        assert self.error is None, self.error
        return self

    def __exit__(self, *_exc):
        self.done.set()
        self.thread.join(10)
        return False


def test_a_waiting_thread_gets_the_timeout_it_asked_for(tmp_path):
    """The defect itself: an in-process contender ignored the caller's deadline."""
    path = str(tmp_path / "state.json")
    with _Holder(path):
        started = time.monotonic()
        with pytest.raises(statelock.StateLockTimeout):
            with statelock.transaction(path, timeout=BUDGET_S):
                pytest.fail("a lock held by another thread was handed out anyway")
        waited = time.monotonic() - started
    assert waited < HOLD_S / 2, (
        "transaction(timeout=%.2f) waited %.2fs for a sibling thread; the deadline "
        "only covered the OS lock" % (BUDGET_S, waited))


def test_the_timeout_is_one_budget_for_both_locks(tmp_path):
    """Local lock then OS lock must not each get the full deadline in turn."""
    path = str(tmp_path / "budget.json")
    slept = []

    def slow_acquire(lock_path, timeout):
        slept.append(timeout)
        raise statelock.StateLockTimeout("os lock busy: %s" % lock_path)

    with _Holder(path, hold_s=BUDGET_S * 2):
        started = time.monotonic()
        # The local lock frees partway through the budget; whatever is left is all
        # the OS lock may be asked for.
        original, statelock._acquire = statelock._acquire, slow_acquire
        try:
            with pytest.raises(statelock.StateLockTimeout):
                with statelock.transaction(path, timeout=BUDGET_S * 4):
                    pytest.fail("the OS lock was reported busy")
        finally:
            statelock._acquire = original
        elapsed = time.monotonic() - started
    assert slept and slept[0] < BUDGET_S * 4, (
        "the OS lock was handed a fresh %.2fs deadline after the local wait" % slept[0])
    assert elapsed < BUDGET_S * 4


def test_re_entry_on_the_owning_thread_never_times_out(tmp_path):
    """The property the unconditional wait was protecting stays exactly true."""
    path = str(tmp_path / "nested.json")
    with statelock.transaction(path, timeout=BUDGET_S):
        time.sleep(BUDGET_S * 2)                 # the outer budget is long gone
        with statelock.transaction(path, timeout=BUDGET_S):
            with statelock.transaction(path, timeout=0):
                pass


def test_a_timed_out_contender_leaves_the_lock_usable(tmp_path):
    """A refused writer must not keep a share of the lock it never got."""
    path = str(tmp_path / "release.json")
    with _Holder(path) as holder:
        with pytest.raises(statelock.StateLockTimeout):
            with statelock.transaction(path, timeout=BUDGET_S):
                pass
        holder.done.set()
        holder.thread.join(10)
    started = time.monotonic()
    with statelock.transaction(path, timeout=HOLD_S):
        pass
    assert time.monotonic() - started < HOLD_S / 2, "the refused attempt kept the lock"
    # And a second process-local writer can still take it afterwards.
    with statelock.transaction(path, timeout=HOLD_S):
        pass


def test_an_uncontended_transaction_is_unaffected(tmp_path):
    """Serialization is still the point: sequential writers keep taking turns."""
    path = str(tmp_path / "plain.json")
    order = []
    for index in range(3):
        with statelock.transaction(path, timeout=HOLD_S):
            order.append(index)
    assert order == [0, 1, 2]
    assert os.path.exists(path + ".lock")


# ------------------------------------------------------- the reachable caller

@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A private artifact store; never the developer's ~/.collie."""
    root = tmp_path / "state" / "pack_artifacts"
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_DIR", str(root))
    monkeypatch.delenv("COLLIE_STATE_DIR", raising=False)
    return root


def _saved_artifact(workspace, tmp_path):
    """One real saved winner bundle for ``workspace``, through the public path."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "edit.txt").write_text("before", encoding="utf-8")
    attempt = tmp_path / "attempt"
    shutil.copytree(str(workspace), str(attempt), symlinks=True)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "edit.txt").write_text("after", encoding="utf-8")
    bundle = pa.create_artifact(str(attempt), baseline, workspace=str(workspace))
    return pa.save_artifact(bundle)


def test_apply_reports_locked_instead_of_wedging_the_request_thread(store, tmp_path):
    """Two applies for one workspace are two threads of the web server process.

    ``apply_artifact`` documents ``code="locked"`` for exactly this, and produced
    it when the other apply was in another process.  In-process it produced
    nothing at all until the first apply finished — the Apply button spinning
    with no error, on a ``ThreadingHTTPServer`` connection nobody can reclaim.
    """
    workspace = tmp_path / "repo"
    record = _saved_artifact(workspace, tmp_path)
    apply_lock = pa._apply_lock(str(store), str(workspace))

    with _Holder(apply_lock):
        started = time.monotonic()
        result = pa.apply_artifact(record["id"], str(workspace), timeout=BUDGET_S)
        waited = time.monotonic() - started
    assert result["code"] == "locked", result
    assert result["applied"] is False
    assert waited < HOLD_S / 2, (
        "a concurrent in-process apply blocked for %.2fs instead of reporting "
        "locked within %.2fs" % (waited, BUDGET_S))
    # Nothing was written while the other writer held the workspace...
    assert (workspace / "edit.txt").read_text(encoding="utf-8") == "before"
    # ...and the bundle is still perfectly applicable once it is free.
    assert pa.apply_artifact(record["id"], str(workspace), timeout=HOLD_S)["applied"]
    assert (workspace / "edit.txt").read_text(encoding="utf-8") == "after"
