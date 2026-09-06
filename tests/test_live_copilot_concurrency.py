"""Live Copilot cross-process state transactions and bounded speech ingress.

Two classes of failure are reproduced here with real concurrency rather than mocks:

* Two Collie processes doing read-modify-write on the same Live state file.  Atomic replace
  stops torn bytes; it does not stop the second writer from erasing the first writer's events,
  a stop, or a permission revocation.  Helper processes are launched hidden on Windows.
* One accepted audio chunk per thread.  The ingress must decide admission synchronously, keep a
  fixed number of decoders, and clean up every staged file on completion, failure, stop, a new
  session, or a permission change.

No microphone, no SenseVoice model, no provider, and no external service is exercised: the
audio is synthetic bytes and every transcriber is a local callable.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from harness import live_copilot as live
from harness import plat
from harness import statelock


REPO_ROOT = str(Path(__file__).resolve().parents[1])
DEADLINE = 30.0


def _junction_maker():
    """Return the real Windows junction constructor, or None where junctions do not exist."""
    if os.name != "nt":
        return None
    try:
        import _winapi
    except ImportError:  # pragma: no cover - non-CPython
        return None
    return getattr(_winapi, "CreateJunction", None)


CREATE_JUNCTION = _junction_maker()

WORKER_SOURCE = '''\
"""Hidden helper process for Live Copilot cross-process regressions."""
import os
import sys
import time

sys.path.insert(0, sys.argv[1])
from harness import live_copilot as live


def wait_for(path, timeout=60.0):
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() >= deadline:
            raise SystemExit("timed out waiting for " + path)
        time.sleep(0.002)


def touch(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("1")


def main():
    mode, root = sys.argv[2], sys.argv[3]
    store = live.LiveSessionStore(root)
    if mode == "append":
        count, prefix, ready, go = int(sys.argv[4]), sys.argv[5], sys.argv[6], sys.argv[7]
        touch(ready)
        wait_for(go)
        for index in range(count):
            store.add_event(source="typed", text="%s-%d" % (prefix, index))
        return
    if mode == "hold-append":
        text, held, release, result = sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7]
        with store._transaction():
            value = store._read()
            with open(store.path, "rb") as handle:
                before = handle.read()
            touch(held)
            wait_for(release)
            with open(store.path, "rb") as handle:
                after = handle.read()
            now = live._now_ms()
            value["events"] = (value.get("events") or []) + [{
                "id": "evt-child", "at_ms": now, "received_at_ms": now,
                "source": "typed", "speaker": "", "kind": "context", "app": "",
                "title": "", "text": text}]
            store._write(value)
        touch(result if before == after else result + ".clobbered")
        return
    if mode == "claim-pending":
        session_id, claimed = sys.argv[4], sys.argv[5]

        def transcribe(path, **_kwargs):
            while True:
                time.sleep(0.05)

        store.ingest_audio(session_id=session_id, source="microphone", seq=0,
                           mime_type="audio/webm", data=b"child-chunk",
                           transcriber=transcribe)
        touch(claimed)
        while True:
            time.sleep(0.05)
    if mode == "hold-chunk":
        session_id, source, seq = sys.argv[4], sys.argv[5], int(sys.argv[6])
        accepted, release, nbytes = sys.argv[7], sys.argv[8], int(sys.argv[9])

        def transcribe(path, **_kwargs):
            wait_for(release)
            return {"text": "child speech", "segments": []}

        store.ingest_audio(session_id=session_id, source=source, seq=seq,
                           mime_type="audio/webm", data=b"c" * nbytes,
                           transcriber=transcribe)
        touch(accepted)
        # Exit only once this process has finished with the chunk, so the test can tell
        # "the holder released its claim" apart from "the holder died".
        deadline = time.monotonic() + 60.0
        while store._ingress().stats()["outstanding"]:
            if time.monotonic() >= deadline:
                raise SystemExit("child chunk never drained")
            time.sleep(0.002)
        return
    raise SystemExit("unknown worker mode " + mode)


main()
'''


def _wait_until(predicate, message, timeout=DEADLINE):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.002)


def _spawn(tmp_path, mode, root, *args):
    """Start a helper process with no console window on Windows."""
    script = tmp_path / "live_worker.py"
    if not script.exists():
        script.write_text(WORKER_SOURCE, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(script), REPO_ROOT, mode, str(root)] + [str(a) for a in args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **plat.no_window_kwargs())


def _finish(process, expect_exit=0):
    out, err = process.communicate(timeout=DEADLINE)
    assert process.returncode == expect_exit, "helper failed: %s%s" % (out, err)
    return out, err


@pytest.fixture
def limits(monkeypatch):
    """Configure the bounded ingress explicitly; a fresh object picks the new limits up."""

    def configure(workers=1, chunks=4, queue_bytes=live.AUDIO_QUEUE_BYTES):
        monkeypatch.setenv("COLLIE_LIVE_AUDIO_WORKERS", str(workers))
        monkeypatch.setenv("COLLIE_LIVE_AUDIO_QUEUE", str(chunks))
        monkeypatch.setenv("COLLIE_LIVE_AUDIO_QUEUE_BYTES", str(queue_bytes))
        live.reset_audio_ingress()

    yield configure
    live.reset_audio_ingress()


class _Gate:
    """A transcriber that blocks until released, recording real decode concurrency."""

    def __init__(self, text="chunk transcript"):
        self.text = text
        self.open = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.seen = []
        self.started = threading.Event()

    def __call__(self, path, **_kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.seen.append(os.path.basename(path))
            self.started.set()
        try:
            assert self.open.wait(DEADLINE), "gate never opened"
        finally:
            with self.lock:
                self.active -= 1
        return {"text": self.text, "segments": []}


def _audio_files(root):
    return sorted(path.name for path in Path(root, "live-audio").rglob("*.webm"))


def _idle(store):
    _wait_until(lambda: store.snapshot()["audio"]["outstanding"] == 0,
                "speech ingress never drained")


# --- cross-process state transactions -------------------------------------------------

def test_four_processes_append_live_events_without_losing_any(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False)
    go = tmp_path / "go"
    workers, per_process = [], 15
    for index in range(4):
        ready = tmp_path / ("ready-%d" % index)
        workers.append((_spawn(tmp_path, "append", tmp_path, per_process,
                               "proc%d" % index, ready, go), ready))
    for _process, ready in workers:
        _wait_until(ready.exists, "helper process never reached the barrier")
    go.write_text("1", encoding="utf-8")
    for process, _ready in workers:
        _finish(process)

    texts = {row.get("text") for row in store.snapshot()["events"]}
    expected = {"proc%d-%d" % (index, item)
                for index in range(4) for item in range(per_process)}
    assert expected <= texts
    assert len(store.snapshot()["events"]) == 4 * per_process + 1


def test_stop_is_not_overwritten_by_a_late_writer_in_another_process(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(listen=True, consent=True, understand=False)
    held, release, result = (tmp_path / "held", tmp_path / "release", tmp_path / "result")
    child = _spawn(tmp_path, "hold-append", tmp_path, "child context", held, release, result)
    _wait_until(held.exists, "helper never took the state transaction")

    entered, stopped = threading.Event(), {}

    def stop_now():
        entered.set()
        stopped["value"] = store.stop(stopped_from="test")

    thread = threading.Thread(target=stop_now)
    thread.start()
    entered.wait(DEADLINE)
    release.write_text("1", encoding="utf-8")
    thread.join(DEADLINE)
    _finish(child)

    # The child observed the state file byte-for-byte unchanged for the whole transaction.
    assert result.exists() and not Path(str(result) + ".clobbered").exists()
    current = store.snapshot()
    assert current["active"] is False and current["listen"] is False
    assert current["stop_reason"] == "user_requested"
    assert any(row.get("text") == "child context" for row in current["events"])


def test_permission_revocation_is_not_overwritten_by_a_late_writer(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(listen=True, consent=True, understand=False)
    held, release, result = (tmp_path / "held", tmp_path / "release", tmp_path / "result")
    child = _spawn(tmp_path, "hold-append", tmp_path, "late speech", held, release, result)
    _wait_until(held.exists, "helper never took the state transaction")

    entered = threading.Event()

    def revoke():
        entered.set()
        store.update_permissions(listen=False, understand=False)

    thread = threading.Thread(target=revoke)
    thread.start()
    entered.wait(DEADLINE)
    release.write_text("1", encoding="utf-8")
    thread.join(DEADLINE)
    _finish(child)

    assert result.exists() and not Path(str(result) + ".clobbered").exists()
    current = store.snapshot()
    assert current["listen"] is False and current["understand"] is False
    assert current["active"] is True
    assert any(row.get("text") == "late speech" for row in current["events"])


def test_pending_claims_of_another_process_survive_and_dead_owners_are_reclaimed(tmp_path,
                                                                                 limits):
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    claimed = tmp_path / "claimed"
    child = _spawn(tmp_path, "claim-pending", tmp_path, started["session_id"], claimed)
    try:
        _wait_until(claimed.exists, "helper never claimed a pending chunk")
        assert store.snapshot()["audio"]["pending"] == 1

        # This process completes its own chunk.  A single shared counter would decrement the
        # other process's claim to zero; per-owner claims leave it exactly where it was.
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=1,
                           mime_type="audio/webm", data=b"local-chunk",
                           transcriber=lambda path, **_kwargs: {"text": "local", "segments": []})
        _idle(store)
        assert store.snapshot()["audio"]["pending"] == 1
        owners = store._read()["audio"]["pending_by_owner"]
        assert len(owners) == 1
        assert sum(row["chunks"] for row in owners.values()) == 1
        assert sum(row["bytes"] for row in owners.values()) == len(b"child-chunk")
    finally:
        child.kill()
        child.wait(timeout=DEADLINE)

    # The OS released the dead owner's claim file, so its pending work is reclaimed without
    # any timeout guess.
    _wait_until(lambda: store.snapshot()["audio"]["pending"] == 0,
                "a dead owner's pending claim was never reclaimed")
    store.add_note(text="state is still writable")
    assert store.snapshot()["notes"][-1]["text"] == "state is still writable"


def test_nested_transactions_never_relock_the_same_byte(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False, observe_apps=False)
    with store._transaction():
        assert store.snapshot()["active"] is True
        store.add_note(text="written inside an outer transaction")
    assert store.snapshot()["notes"][-1]["text"] == "written inside an outer transaction"

    # The ticker stops an expired session from inside its own transaction.
    with store._transaction():
        value = store._read()
        value["expires_at_ms"] = live._now_ms() - 1
        store._write(value)
    runtime = live.LiveCopilotRuntime(tmp_path, analyzer=lambda _payload: {})
    runtime.activity_source = None
    assert runtime.tick() is False
    assert store.snapshot()["stop_reason"] == "max_duration"


# --- bounded speech ingress -----------------------------------------------------------

def test_busy_rejection_is_distinguishable_and_actionable(tmp_path, limits):
    limits(workers=1, chunks=1)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate()
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"first", transcriber=gate)
    gate.started.wait(DEADLINE)

    with pytest.raises(live.LiveCopilotBusyError) as caught:
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=1,
                           mime_type="audio/webm", data=b"second", transcriber=gate)
    error = caught.value
    assert isinstance(error, live.LiveCopilotError)
    body = error.payload()
    assert body["code"] == "live_audio_busy" and body["retryable"] is True
    assert body["seq"] == 1 and body["source"] == "microphone"
    assert body["retry_after_ms"] > 0 and error.retry_after_seconds > 0
    assert "queue is full" in body["error"]
    gate.open.set()


def test_full_queue_never_consumes_a_sequence_or_stages_audio(tmp_path, limits):
    limits(workers=1, chunks=1)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate("accepted transcript")
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"first", transcriber=gate)
    gate.started.wait(DEADLINE)
    before = Path(store.path).read_bytes()

    for _attempt in range(3):
        with pytest.raises(live.LiveCopilotBusyError):
            store.ingest_audio(session_id=started["session_id"], source="microphone", seq=1,
                               mime_type="audio/webm", data=b"second", transcriber=gate)
    assert Path(store.path).read_bytes() == before
    assert store.snapshot()["audio"]["microphone_seq"] == 0
    assert _audio_files(tmp_path) == ["microphone-00000000.webm"]

    gate.open.set()
    _idle(store)
    accepted = store.ingest_audio(session_id=started["session_id"], source="microphone", seq=1,
                                  mime_type="audio/webm", data=b"second", transcriber=gate)
    assert accepted["queued"] is True and accepted["seq"] == 1
    _idle(store)
    # A retry of an already accepted sequence stays idempotent: no second decode, no event.
    again = store.ingest_audio(session_id=started["session_id"], source="microphone", seq=1,
                               mime_type="audio/webm", data=b"second", transcriber=gate)
    assert again == {"ok": True, "duplicate": True, "seq": 1}
    _idle(store)
    assert gate.seen == ["microphone-00000000.webm", "microphone-00000001.webm"]
    assert store.snapshot()["audio"]["pending"] == 0
    assert _audio_files(tmp_path) == []


def test_concurrent_admission_under_a_full_queue_is_exact(tmp_path, limits):
    limits(workers=1, chunks=3)
    ingress = live.LiveSessionStore(tmp_path)._ingress()
    barrier = threading.Barrier(12)
    admitted, rejected = [], []
    guard = threading.Lock()

    def attempt():
        barrier.wait(DEADLINE)
        try:
            ticket = ingress.reserve(1_024)
        except live.LiveCopilotBusyError:
            with guard:
                rejected.append(1)
            return
        with guard:
            admitted.append(ticket)

    threads = [threading.Thread(target=attempt) for _index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(DEADLINE)

    assert len(admitted) == 3 and len(rejected) == 9
    for ticket in admitted:
        ingress.release(ticket)
    assert ingress.stats()["outstanding"] == 0


def test_queued_bytes_are_bounded_independently_of_chunk_count(tmp_path, limits):
    limits(workers=1, chunks=8, queue_bytes=4_096)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate()
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"x" * 4_096, transcriber=gate)
    gate.started.wait(DEADLINE)
    with pytest.raises(live.LiveCopilotBusyError, match="bytes"):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=1,
                           mime_type="audio/webm", data=b"y" * 4_096, transcriber=gate)
    assert store.snapshot()["audio"]["microphone_seq"] == 0
    gate.open.set()


def test_decoders_never_exceed_the_configured_worker_count(tmp_path, limits):
    limits(workers=2, chunks=8)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate()
    for seq in range(8):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=seq,
                           mime_type="audio/webm", data=b"chunk-%d" % seq, transcriber=gate)
    _wait_until(lambda: gate.active == 2, "the worker pool never reached its limit")
    assert store.snapshot()["audio"]["queue_depth"] == 6
    gate.open.set()
    _idle(store)

    assert gate.peak == 2
    assert store._ingress()._peak_active == 2
    assert len(gate.seen) == 8
    assert threading.active_count() < 40
    assert _audio_files(tmp_path) == []


def test_one_ingress_bounds_every_store_object_on_the_same_root(tmp_path, limits):
    limits(workers=1, chunks=1)
    first, second = live.LiveSessionStore(tmp_path), live.LiveSessionStore(tmp_path)
    assert first._ingress() is second._ingress()
    started = first.start(listen=True, consent=True, understand=False)
    gate = _Gate()
    first.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"first", transcriber=gate)
    gate.started.wait(DEADLINE)
    with pytest.raises(live.LiveCopilotBusyError):
        second.ingest_audio(session_id=started["session_id"], source="system", seq=0,
                            mime_type="audio/webm", data=b"second", transcriber=gate)
    gate.open.set()


# --- deterministic cleanup across authority boundaries --------------------------------

def test_stop_cancels_queued_audio_and_removes_every_temp_file(tmp_path, limits):
    limits(workers=1, chunks=6)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate("must not be transcribed")
    for seq in range(4):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=seq,
                           mime_type="audio/webm", data=b"chunk", transcriber=gate)
    gate.started.wait(DEADLINE)
    assert len(_audio_files(tmp_path)) == 4

    stopped = store.stop()
    assert stopped["active"] is False
    # Only the chunk already inside the decoder survives; its own worker deletes it.
    assert _audio_files(tmp_path) == ["microphone-00000000.webm"]
    assert stopped["audio"]["pending"] == 1
    assert stopped["audio"]["queue_depth"] == 0

    gate.open.set()
    _idle(store)
    current = store.snapshot()
    assert _audio_files(tmp_path) == []
    assert current["audio"]["pending"] == 0
    assert all(row.get("kind") != "speech" for row in current["events"])
    assert len(gate.seen) == 1


def test_revoking_listening_cancels_queued_capture_but_not_the_capsule(tmp_path, limits):
    limits(workers=1, chunks=6)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate("held chunk")
    queued = _Gate("queued transcript")
    store.ingest_audio(session_id=started["session_id"], source="capsule", seq=0,
                       mime_type="audio/webm", data=b"held", transcriber=gate)
    gate.started.wait(DEADLINE)
    for seq in range(2):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=seq,
                           mime_type="audio/webm", data=b"mic", transcriber=queued)
    store.ingest_audio(session_id=started["session_id"], source="capsule", seq=1,
                       mime_type="audio/webm", data=b"push to talk", transcriber=queued)
    assert len(_audio_files(tmp_path)) == 4

    store.update_permissions(listen=False)
    assert _audio_files(tmp_path) == ["capsule-00000000.webm", "capsule-00000001.webm"]

    gate.open.set()
    queued.open.set()
    _idle(store)
    current = store.snapshot()
    assert queued.seen == ["capsule-00000001.webm"]
    assert [row["text"] for row in current["events"] if row["kind"] == "speech"] == [
        "held chunk", "queued transcript"]
    assert current["audio"]["pending"] == 0
    assert _audio_files(tmp_path) == []


def test_late_transcript_is_dropped_after_listening_is_revoked(tmp_path, limits):
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate("recorded before consent was withdrawn")
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"mic", transcriber=gate)
    gate.started.wait(DEADLINE)

    store.update_permissions(listen=False)
    gate.open.set()
    _idle(store)
    current = store.snapshot()
    assert all(row.get("kind") != "speech" for row in current["events"])
    assert current["audio"]["pending"] == 0
    assert _audio_files(tmp_path) == []


def test_late_transcript_is_dropped_after_a_new_session_starts(tmp_path, limits):
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    old = store.start(listen=True, consent=True, understand=False)
    gate = _Gate("previous session speech")
    store.ingest_audio(session_id=old["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"mic", transcriber=gate)
    store.ingest_audio(session_id=old["session_id"], source="microphone", seq=1,
                       mime_type="audio/webm", data=b"mic", transcriber=gate)
    gate.started.wait(DEADLINE)

    store.stop()
    current = store.start(listen=True, consent=True, understand=False)
    assert current["session_id"] != old["session_id"]
    # Starting a session purges every staging directory the new session cannot claim.  A file a
    # decoder currently holds open is skipped on Windows and removed by its own worker instead.
    assert _audio_files(tmp_path) == []

    gate.open.set()
    _idle(store)
    latest = store.snapshot()
    assert latest["session_id"] == current["session_id"]
    assert all(row.get("kind") != "speech" for row in latest["events"])
    assert latest["audio"]["pending"] == 0 and latest["audio"]["microphone_seq"] == -1
    assert _audio_files(tmp_path) == []


# --- the admission bound is shared by every process on one state root -----------------

def _accepted(store):
    audio = store.snapshot()["audio"]
    return audio["accepted"], audio["accepted_bytes"]


def test_a_second_process_cannot_multiply_accepted_chunks_on_one_state_root(tmp_path, limits):
    """One chunk is the whole root's budget, not one budget per Collie process."""
    limits(workers=1, chunks=1)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    accepted, release = tmp_path / "accepted", tmp_path / "release"
    child = _spawn(tmp_path, "hold-chunk", tmp_path, started["session_id"], "microphone", 0,
                   accepted, release, 64)
    try:
        _wait_until(accepted.exists, "helper never had its chunk accepted")
        assert _accepted(store) == (1, 64)
        before = Path(store.path).read_bytes()

        # This process has an entirely empty pool of its own, and must still be refused.
        for _attempt in range(3):
            with pytest.raises(live.LiveCopilotBusyError) as caught:
                store.ingest_audio(session_id=started["session_id"], source="system", seq=0,
                                   mime_type="audio/webm", data=b"second server chunk",
                                   transcriber=lambda path, **_kwargs: {"text": "no"})
            assert caught.value.payload()["seq"] == 0
            assert "state root" in str(caught.value)
        # Rejection consumed no sequence number, no receipt, no file and no ticket.
        assert Path(store.path).read_bytes() == before
        assert store.snapshot()["audio"]["system_seq"] == -1
        assert store._ingress().stats()["outstanding"] == 0
        assert _audio_files(tmp_path) == ["microphone-00000000.webm"]

        release.write_text("1", encoding="utf-8")
        _finish(child)
    finally:
        child.kill()
    _wait_until(lambda: _accepted(store) == (0, 0), "the holder's claim was never released")

    # The browser retries the very same seq once the root has room again.
    result = store.ingest_audio(session_id=started["session_id"], source="system", seq=0,
                                mime_type="audio/webm", data=b"second server chunk",
                                transcriber=lambda path, **_kwargs: {"text": "later chunk"})
    assert result["queued"] is True and result["seq"] == 0
    _idle(store)
    speech = [row["text"] for row in store.snapshot()["events"] if row["kind"] == "speech"]
    assert speech == ["child speech", "later chunk"]


def test_a_killed_process_does_not_hold_the_shared_admission_bound(tmp_path, limits):
    limits(workers=1, chunks=1)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    accepted, release = tmp_path / "accepted", tmp_path / "never-released"
    child = _spawn(tmp_path, "hold-chunk", tmp_path, started["session_id"], "microphone", 0,
                   accepted, release, 64)
    _wait_until(accepted.exists, "helper never had its chunk accepted")
    with pytest.raises(live.LiveCopilotBusyError):
        store.ingest_audio(session_id=started["session_id"], source="system", seq=0,
                           mime_type="audio/webm", data=b"blocked",
                           transcriber=lambda path, **_kwargs: {"text": "no"})

    child.kill()
    child.wait(timeout=DEADLINE)
    # No TTL and no pid guess: the OS released the dead owner's claim file, so its share of the
    # bound is reclaimed and the unchanged sequence number is accepted.
    _wait_until(lambda: _accepted(store)[0] == 0, "a dead owner kept the admission bound")
    result = store.ingest_audio(session_id=started["session_id"], source="system", seq=0,
                                mime_type="audio/webm", data=b"blocked",
                                transcriber=lambda path, **_kwargs: {"text": "after the crash"})
    assert result["queued"] is True and result["seq"] == 0
    _idle(store)
    assert store.snapshot()["events"][-1]["text"] == "after the crash"


def test_queued_bytes_are_bounded_across_processes_too(tmp_path, limits):
    limits(workers=1, chunks=8, queue_bytes=4_096)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    accepted, release = tmp_path / "accepted", tmp_path / "release"
    child = _spawn(tmp_path, "hold-chunk", tmp_path, started["session_id"], "microphone", 0,
                   accepted, release, 4_000)
    try:
        _wait_until(accepted.exists, "helper never had its chunk accepted")
        assert _accepted(store) == (1, 4_000)
        # Chunk room is free (1 of 8); the root's byte budget is not.
        with pytest.raises(live.LiveCopilotBusyError, match="bytes"):
            store.ingest_audio(session_id=started["session_id"], source="system", seq=0,
                               mime_type="audio/webm", data=b"y" * 4_000,
                               transcriber=lambda path, **_kwargs: {"text": "no"})
        assert store.snapshot()["audio"]["system_seq"] == -1
        release.write_text("1", encoding="utf-8")
        _finish(child)
    finally:
        child.kill()
    _wait_until(lambda: _accepted(store) == (0, 0), "the holder's bytes were never released")
    assert store.ingest_audio(session_id=started["session_id"], source="system", seq=0,
                              mime_type="audio/webm", data=b"y" * 4_000,
                              transcriber=lambda path, **_kwargs: {"text": "ok"})["queued"]
    _idle(store)


# --- consent is checked and the transcript inserted in one transaction ----------------

def test_no_speech_is_inserted_after_a_revocation_commits(tmp_path, limits):
    """Close the check-to-add window: the permission check and the insert are one transaction.

    The instrumentation puts a revoking caller in flight at the exact moment the decoder is
    about to append, which is the interleaving that used to let already-checked speech land
    after ``update_permissions(listen=False)`` had returned.
    """
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    at_insert_point, revoking = threading.Event(), threading.Event()
    observed = {}
    real_add_event = store.add_event

    def add_event_with_a_revocation_in_flight(**kwargs):
        # Reached only once the decoder has already passed its permission check, which is
        # exactly the instant the revocation has to be ordered against.
        at_insert_point.set()
        assert revoking.wait(DEADLINE), "the revoking caller never started"
        time.sleep(0.05)  # let it reach the state lock it must be serialized behind
        return real_add_event(**kwargs)

    store.add_event = add_event_with_a_revocation_in_flight
    gate = _Gate("speech authorized a moment ago")
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"mic", transcriber=gate)
    gate.started.wait(DEADLINE)

    def revoke():
        assert at_insert_point.wait(DEADLINE), "the decoder never reached its insert"
        revoking.set()
        store.update_permissions(listen=False)
        # Whatever the outcome of the race, this is the session the user was left with.
        observed["events"] = [row.get("id") for row in
                              live.LiveSessionStore(tmp_path).snapshot()["events"]]

    thread = threading.Thread(target=revoke)
    thread.start()
    gate.open.set()
    thread.join(DEADLINE)
    _idle(store)
    assert at_insert_point.is_set() and "events" in observed

    current = store.snapshot()
    assert current["listen"] is False
    speech = [row for row in current["events"] if row["kind"] == "speech"]
    # Either the transcript committed before the revocation or it never landed at all.  What
    # must never happen is speech appearing in the session *after* the user revoked consent.
    assert all(row["id"] in observed["events"] for row in speech)
    assert current["audio"]["pending"] == 0
    assert _audio_files(tmp_path) == []


def test_speech_from_a_revoked_epoch_is_dropped_even_after_listening_returns(tmp_path, limits):
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate("captured under the old authorization")
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"mic", transcriber=gate)
    gate.started.wait(DEADLINE)

    # Off, then on again, while the decode is still outstanding.  A plain ``listen`` check
    # would see True and write speech the user revoked in between.
    store.update_permissions(listen=False)
    store.update_permissions(listen=True)
    gate.open.set()
    _idle(store)

    current = store.snapshot()
    assert current["listen"] is True
    assert all(row["kind"] != "speech" for row in current["events"])
    assert current["audio"]["pending"] == 0
    assert _audio_files(tmp_path) == []

    # Capture accepted under the new epoch is written normally.
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=1,
                       mime_type="audio/webm", data=b"mic",
                       transcriber=lambda path, **_kwargs: {"text": "current authorization"})
    _idle(store)
    assert store.snapshot()["events"][-1]["text"] == "current authorization"


def test_a_capsule_chunk_survives_a_listen_toggle_but_never_a_stop(tmp_path, limits):
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate("push to talk speech")
    store.ingest_audio(session_id=started["session_id"], source="capsule", seq=0,
                       mime_type="audio/webm", data=b"hold x2", transcriber=gate)
    gate.started.wait(DEADLINE)
    store.update_permissions(listen=False)
    store.update_permissions(listen=True)
    gate.open.set()
    _idle(store)
    # The capsule gesture is its own authorization; continuous-listen epochs do not revoke it.
    assert store.snapshot()["events"][-1]["text"] == "push to talk speech"

    second = _Gate("must never be transcribed")
    store.ingest_audio(session_id=started["session_id"], source="capsule", seq=1,
                       mime_type="audio/webm", data=b"hold x2", transcriber=second)
    second.started.wait(DEADLINE)
    store.stop()
    second.open.set()
    _idle(store)
    current = store.snapshot()
    assert [row["text"] for row in current["events"] if row["kind"] == "speech"] == [
        "push to talk speech"]
    assert current["audio"]["pending"] == 0
    assert _audio_files(tmp_path) == []


# --- staging, sequence and submission commit together ---------------------------------

def test_failed_staging_cannot_leave_a_hole_behind_a_later_sequence(tmp_path, limits,
                                                                    monkeypatch):
    """Inject a disk failure on seq 0 while another writer is taking seq 1."""
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    real_stage = live.LiveSessionStore._stage_audio_bytes
    second_attempted = threading.Event()

    def flaky_stage(self, path, data):
        if path.endswith("-00000000.webm"):
            assert second_attempted.wait(DEADLINE), "the second writer never started"
            time.sleep(0.05)  # let it reach the state lock it must not get past
            raise live.LiveCopilotError("could not stage live audio: injected disk failure")
        return real_stage(self, path, data)

    monkeypatch.setattr(live.LiveSessionStore, "_stage_audio_bytes", flaky_stage)
    outcome = {}

    def take_the_next_sequence():
        second_attempted.set()
        try:
            outcome["result"] = store.ingest_audio(
                session_id=started["session_id"], source="microphone", seq=1,
                mime_type="audio/webm", data=b"second", transcriber=lambda p, **k: {"text": "b"})
        except live.LiveCopilotError as exc:
            outcome["error"] = str(exc)

    thread = threading.Thread(target=take_the_next_sequence)
    thread.start()
    with pytest.raises(live.LiveCopilotError, match="injected disk failure"):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                           mime_type="audio/webm", data=b"first",
                           transcriber=lambda p, **k: {"text": "a"})
    thread.join(DEADLINE)

    # seq 1 could not be accepted on top of a sequence that never committed, so no hole exists.
    assert "result" not in outcome
    assert "gap: expected 0" in outcome.get("error", "")
    current = store.snapshot()
    assert current["audio"]["microphone_seq"] == -1
    assert current["audio"]["pending"] == 0 and current["audio"]["accepted_bytes"] == 0
    assert store._ingress().stats()["outstanding"] == 0
    assert _audio_files(tmp_path) == []

    monkeypatch.undo()
    for seq, text in ((0, "first"), (1, "second")):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=seq,
                           mime_type="audio/webm", data=text.encode(),
                           transcriber=lambda path, **_kwargs: {"text": os.path.basename(path)})
    _idle(store)
    assert [row["text"] for row in store.snapshot()["events"] if row["kind"] == "speech"] == [
        "microphone-00000000.webm", "microphone-00000001.webm"]


def test_a_failed_submit_strands_no_sequence_file_or_claim(tmp_path, limits, monkeypatch):
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)

    def refuse(_job):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(store._ingress(), "submit", refuse)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                           mime_type="audio/webm", data=b"mic",
                           transcriber=lambda p, **k: {"text": "never"})
    current = store.snapshot()
    assert current["audio"]["microphone_seq"] == -1
    assert current["audio"]["pending"] == 0 and current["audio"]["accepted_bytes"] == 0
    assert store._ingress().stats()["outstanding"] == 0
    assert _audio_files(tmp_path) == []

    monkeypatch.undo()
    # The sender retries the same seq and nothing has been skipped.
    assert store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                              mime_type="audio/webm", data=b"mic",
                              transcriber=lambda p, **k: {"text": "retried"})["seq"] == 0
    _idle(store)
    assert store.snapshot()["events"][-1]["text"] == "retried"


def test_a_stop_between_staging_and_submit_prevents_the_decode(tmp_path, limits, monkeypatch):
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    ingress = store._ingress()
    real_submit = ingress.submit
    decoded = []

    def stop_first_then_submit(job):
        # The user ends the session in the window after the chunk was accepted and before any
        # worker can see it: the queue scan cannot cancel a job that is not queued yet.
        store.stop(stopped_from="test")
        real_submit(job)

    monkeypatch.setattr(ingress, "submit", stop_first_then_submit)
    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"mic",
                       transcriber=lambda path, **_kwargs: decoded.append(path) or {"text": "x"})
    _wait_until(lambda: ingress.stats()["outstanding"] == 0, "the abandoned chunk never drained")

    assert decoded == [], "expensive decoding started after the session was stopped"
    current = store.snapshot()
    assert current["active"] is False
    assert all(row["kind"] != "speech" for row in current["events"])
    assert current["audio"]["pending"] == 0
    assert _audio_files(tmp_path) == []


def test_failed_decode_still_frees_the_slot_and_the_staged_file(tmp_path, limits):
    limits(workers=1, chunks=2)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)

    def broken(_path, **_kwargs):
        raise RuntimeError("ffmpeg is missing")

    store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"mic", transcriber=broken)
    _idle(store)
    current = store.snapshot()
    assert "ffmpeg is missing" in current["audio"]["last_error"]
    assert current["audio"]["pending"] == 0
    assert _audio_files(tmp_path) == []
    assert store.ingest_audio(
        session_id=started["session_id"], source="microphone", seq=1, mime_type="audio/webm",
        data=b"mic", transcriber=lambda path, **_kwargs: {"text": "recovered"})["queued"]
    _idle(store)
    assert store.snapshot()["events"][-1]["text"] == "recovered"


# --- the staging purge never deletes outside its own resolved store -------------------

@pytest.mark.skipif(CREATE_JUNCTION is None, reason="Windows directory junctions only")
def test_the_stale_purge_never_deletes_through_a_directory_junction(tmp_path):
    """``os.path.islink`` says False for a junction; deleting through one leaves the store."""
    store = live.LiveSessionStore(tmp_path)
    outside = tmp_path / "someone-elses-documents"
    outside.mkdir()
    (outside / "important.webm").write_bytes(b"not Collie's file")
    audio_root = Path(store.audio_root)
    audio_root.mkdir(parents=True, exist_ok=True)
    redirected = audio_root / "live-redirected-session"
    CREATE_JUNCTION(str(outside), str(redirected))
    assert not os.path.islink(redirected), "the junction is exactly what islink misses"
    assert [path.name for path in redirected.iterdir()] == ["important.webm"]
    genuine = audio_root / "live-old-session"
    genuine.mkdir()
    (genuine / "microphone-00000000.webm").write_bytes(b"Collie's own stale chunk")

    store._purge_stale_audio("live-current-session")

    assert (outside / "important.webm").read_bytes() == b"not Collie's file"
    assert redirected.exists(), "the junction itself must be left alone, not followed"
    assert not genuine.exists(), "a genuine stale directory inside the store is still purged"


@pytest.mark.skipif(CREATE_JUNCTION is None, reason="Windows directory junctions only")
def test_a_redirected_audio_root_is_refused_rather_than_used(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    CREATE_JUNCTION(str(elsewhere), store.audio_root)
    started = store.start(listen=True, consent=True, understand=False)

    with pytest.raises(live.LiveCopilotError, match="junction"):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=0,
                           mime_type="audio/webm", data=b"mic",
                           transcriber=lambda p, **k: {"text": "no"})
    assert list(elsewhere.iterdir()) == [], "nothing was staged into the redirected directory"
    assert store.snapshot()["audio"]["microphone_seq"] == -1
    assert store._ingress().stats()["outstanding"] == 0


def test_a_symlinked_staging_entry_is_never_unlinked(tmp_path):
    """The same containment rule without needing junction support."""
    store = live.LiveSessionStore(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "important.webm"
    target.write_bytes(b"not Collie's file")
    stale = Path(store.audio_root) / "live-old-session"
    stale.mkdir(parents=True)
    try:
        os.symlink(str(target), str(stale / "microphone-00000000.webm"))
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip("symlink creation is not permitted here: %s" % exc)

    store._purge_stale_audio("live-current-session")
    assert target.read_bytes() == b"not Collie's file"


# --- lock and owner bookkeeping belongs to the process that acquired it ----------------

def test_state_lock_bookkeeping_is_rebuilt_when_the_pid_changes(tmp_path, monkeypatch):
    """Simulate the inheritance a fork produces: depth and handle from another process."""
    store = live.LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False)
    parent_lock = statelock._lock_for(store.path)
    assert parent_lock.pid == os.getpid() and parent_lock.depth == 0
    # What a child inherits mid-transaction: a nonzero depth it never acquired.
    parent_lock.depth = 1

    monkeypatch.setattr(os, "getpid", lambda: 1_234_567)
    child_lock = statelock._lock_for(store.path)
    assert child_lock is not parent_lock
    assert child_lock.depth == 0 and child_lock.handle is None
    # Believing the inherited depth would have skipped locking entirely; this really locks.
    with statelock.transaction(store.path, timeout=DEADLINE):
        assert statelock.claim(child_lock.path) is None
    monkeypatch.undo()
    statelock._lock_for(store.path).depth = 0
    store.add_note(text="state is still writable")
    assert store.snapshot()["notes"][-1]["text"] == "state is still writable"


def test_a_claim_taken_by_another_process_is_closed_but_never_unlocked(tmp_path, monkeypatch):
    path = tmp_path / "owner.claim"
    held = statelock.claim(path)
    assert held is not None and held.mine
    assert statelock.claim(path) is None, "the claim is genuinely exclusive"

    monkeypatch.setattr(os, "getpid", lambda: 1_234_567)
    assert held.mine is False
    unlocked = []
    monkeypatch.setattr(statelock, "_unlock", lambda handle: unlocked.append(handle))
    statelock.unclaim(held)
    # On POSIX the inherited descriptor shares the parent's open file description, so an
    # unlock here would release a lock the parent is still relying on.
    assert unlocked == []
    monkeypatch.undo()


def test_fork_reset_drops_inherited_caches_without_touching_the_parents_files(tmp_path, limits):
    limits(workers=1, chunks=4)
    store = live.LiveSessionStore(tmp_path)
    started = store.start(listen=True, consent=True, understand=False)
    gate = _Gate()
    for seq in range(2):
        store.ingest_audio(session_id=started["session_id"], source="microphone", seq=seq,
                           mime_type="audio/webm", data=b"mic", transcriber=gate)
    gate.started.wait(DEADLINE)
    parent_ingress = store._ingress()
    parent_owner = live._owner_key(store.audio_root)
    assert parent_ingress.stats()["outstanding"] == 2
    assert len(_audio_files(tmp_path)) == 2

    live._reset_live_after_fork()

    # A child owns no worker threads, no queued jobs and no share of the parent's bound.
    assert store._ingress() is not parent_ingress
    assert store._ingress().stats() == {"queue_depth": 0, "queue_limit": 4,
                                        "queue_limit_bytes": live.AUDIO_QUEUE_BYTES,
                                        "workers": 1, "decoding": 0, "outstanding": 0}
    assert live._this_owner(store.audio_root) == ""
    assert live._owner_key(store.audio_root) != parent_owner
    # And it must not have deleted files the parent is still going to decode.
    assert len(_audio_files(tmp_path)) == 2

    gate.open.set()
    _wait_until(lambda: _audio_files(tmp_path) == [], "the parent's own workers never finished")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork only; Windows has no fork")
def test_a_forked_child_never_inherits_the_parent_state_transaction(tmp_path):
    store = live.LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False)
    with store._transaction():
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child process
            code = 0
            try:
                with statelock.transaction(store.path, timeout=0.5):
                    code = 3  # the child wrongly believed it held the parent's lock
            except statelock.StateLockTimeout:
                code = 0
            except BaseException:
                code = 4
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        # The child exiting must not have released the lock this process still holds.
        assert statelock.claim(statelock.canonical(store.path) + ".lock") is None
    store.add_note(text="the parent transaction survived the child")
    assert store.snapshot()["notes"][-1]["text"] == "the parent transaction survived the child"
