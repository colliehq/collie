"""Durability tests for accepted user input.

The failures these cover are the ones a person actually experiences: an
instruction that was acknowledged and never ran, a message that arrived shorter
than it was typed, and a crash in the one-line window between "it is in the
transcript" and "the inbox knows it was delivered".  Concurrency and crash
recovery are exercised with real processes, because in-process fakes cannot fail
the way the bug did.
"""
import copy
import json
import os
import subprocess
import sys
import time

import pytest

from harness import compaction, plat, session_owner, sessions, task_inbox

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    return str(directory)


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _env(store):
    env = dict(os.environ)
    env["COLLIE_SESSIONS_DIR"] = store
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(script, store, *args, timeout=120):
    return subprocess.run([sys.executable, script, *args], cwd=ROOT, env=_env(store),
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, **plat.no_window_kwargs())


def _spawn(script, store, *args):
    return subprocess.Popen([sys.executable, script, *args], cwd=ROOT, env=_env(store),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", **plat.no_window_kwargs())


ENQUEUER = """
import json, sys
from harness import task_inbox

session, prefix, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
text = sys.argv[4]
out = []
for i in range(count):
    try:
        row = task_inbox.enqueue(session, "%s-%d" % (prefix, i), text)
        out.append({"id": row["id"], "duplicate": row["duplicate"]})
    except task_inbox.InboxError as exc:
        out.append({"error": type(exc).__name__})
print(json.dumps(out))
"""

READER = """
import json, sys
from harness import task_inbox

rows = task_inbox.list_entries(sys.argv[1])
print(json.dumps([{k: r[k] for k in ("id", "seq", "mode", "state", "text", "config")}
                  for r in rows]))
"""

# Claim, write the user message into the journal, then die BEFORE acknowledging.
CRASH_AFTER_JOURNAL = """
import os, sys
from harness import sessions, session_owner, task_inbox

session = sys.argv[1]
lease = session_owner.acquire(session, label="crasher")
claimed = task_inbox.claim(session, lease, modes=("steer",))
loaded = sessions.load_checked(session)
messages = list((loaded["session"] or {}).get("messages") or [])
for entry in claimed:
    messages.append(task_inbox.journal_message(entry))
sessions.save(session, messages)
print("JOURNALED %d" % len(claimed), flush=True)
os._exit(9)
"""

# Claim and die with nothing written: the instruction never reached the model.
CRASH_BEFORE_JOURNAL = """
import os, sys
from harness import session_owner, task_inbox

lease = session_owner.acquire(sys.argv[1], label="crasher")
print("CLAIMED %d" % len(task_inbox.claim(sys.argv[1], lease)), flush=True)
os._exit(9)
"""

ENQUEUE_WHILE_OWNED = """
import sys, time
from harness import task_inbox

start = time.monotonic()
row = task_inbox.enqueue(sys.argv[1], sys.argv[2], "typed while the run was owned")
print("OK %s %.3f" % (row["state"], time.monotonic() - start), flush=True)
"""


# Recover from a crash in a *fresh* process, the way the next run would.
RECOVER = """
import json, sys
from harness import sessions, session_owner, task_inbox

session = sys.argv[1]
lease = session_owner.acquire(session, label="recovery")
loaded = sessions.load_checked(session)
messages = (loaded["session"] or {}).get("messages") or []
outcome = task_inbox.reconcile(session, lease, task_inbox.delivered_ids(messages))
outcome["claimed_again"] = [e["id"] for e in task_inbox.claim(session, lease)]
outcome["journal"] = loaded["status"]
outcome["copies"] = sum(1 for m in messages if m.get("inbox_id"))
lease.release()
print(json.dumps(outcome))
"""


def _lease(session, label="test"):
    return session_owner.acquire(session, label=label)


def _link_dir(target, link):
    """Point `link` at `target` however this host permits (see test_session_owner)."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                                capture_output=True, text=True, **plat.no_window_kwargs())
        if result.returncode == 0:
            return "junction"
    return ""


# ------------------------------------------------------------ acceptance


def test_acknowledged_only_after_the_bytes_are_on_disk(store):
    row = task_inbox.enqueue("s1", "e1", "run the tests again")
    assert row["state"] == "pending" and row["duplicate"] is False
    on_disk = json.load(open(task_inbox.store_path("s1"), encoding="utf-8"))
    assert on_disk["entries"][0]["text"] == "run the tests again"
    assert on_disk["session"] == "s1" and on_disk["version"] == task_inbox.VERSION


def test_full_unicode_text_is_preserved_exactly(store, tmp_path):
    text = ("héllo 🌍 中文 ‏RTL‎\r\n\ttrailing spaces   "
            + "…" * 4200 + "\nfinal line")
    assert len(text) > 4000, "the regression is about text longer than the old 4000 cut"
    task_inbox.enqueue("s-uni", "e1", text)
    assert task_inbox.get("s-uni", "e1")["text"] == text
    # And after a completely separate process reads it back off disk.
    reader = _script(tmp_path, "reader.py", READER)
    result = _run(reader, store, "s-uni")
    assert json.loads(result.stdout)[0]["text"] == text, result.stderr


def test_same_id_is_idempotent_and_a_changed_payload_is_a_conflict(store):
    first = task_inbox.enqueue("s2", "e1", "same text")
    again = task_inbox.enqueue("s2", "e1", "same text")
    assert again["duplicate"] is True and again["seq"] == first["seq"]
    assert len(task_inbox.list_entries("s2")) == 1
    with pytest.raises(task_inbox.IdConflict):
        task_inbox.enqueue("s2", "e1", "different text")
    with pytest.raises(task_inbox.IdConflict):
        task_inbox.enqueue("s2", "e1", "same text", mode="follow_up")
    assert task_inbox.get("s2", "e1")["text"] == "same text"


def test_concurrent_processes_enqueue_without_losing_or_duplicating(tmp_path, store):
    script = _script(tmp_path, "enq.py", ENQUEUER)
    procs = [_spawn(script, store, "s-conc", "p%d" % n, "5", "concurrent write")
             for n in range(4)]
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        assert len(json.loads(out)) == 5
    rows = task_inbox.list_entries("s-conc")
    assert len(rows) == 20
    assert len({r["id"] for r in rows}) == 20
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == 20   # stable, unique ordering


def test_racing_processes_cannot_double_accept_one_id(tmp_path, store):
    script = _script(tmp_path, "enq.py", ENQUEUER)
    procs = [_spawn(script, store, "s-race", "same", "1", "one instruction")
             for _ in range(4)]
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        assert json.loads(out)[0]["id"] == "same-0"
    assert len(task_inbox.list_entries("s-race")) == 1


def test_caps_reject_the_new_request_and_never_evict_an_accepted_one(store):
    for index in range(task_inbox.MAX_PENDING):
        task_inbox.enqueue("s-cap", "e%d" % index, "waiting %d" % index)
    with pytest.raises(task_inbox.InboxFull):
        task_inbox.enqueue("s-cap", "overflow", "one too many")
    rows = task_inbox.list_entries("s-cap")
    assert len(rows) == task_inbox.MAX_PENDING
    assert all(r["state"] == "pending" for r in rows)
    assert task_inbox.get("s-cap", "overflow") is None
    # A retry of something already accepted still succeeds while full.
    assert task_inbox.enqueue("s-cap", "e0", "waiting 0")["duplicate"] is True


def test_byte_cap_is_enforced_on_open_entries(store):
    chunk = "x" * (100 * 1024)
    accepted = 0
    with pytest.raises(task_inbox.InboxFull):
        for index in range(20):
            task_inbox.enqueue("s-bytes", "e%d" % index, chunk)
            accepted += 1
    status = task_inbox.status("s-bytes")
    assert status["counts"]["pending"] == accepted
    assert status["open_bytes"] <= task_inbox.MAX_PENDING_BYTES


@pytest.mark.parametrize("text", [
    b"bytes", 17, None, "", "   ", "with\x00nul", "\ud800lone surrogate",
    "oversized",     # expanded below: a megabyte literal breaks the test id
])
def test_malformed_text_is_refused_without_storing_anything(store, text):
    if text == "oversized":
        text = "x" * (task_inbox.MAX_TEXT_BYTES + 1)
    with pytest.raises(task_inbox.InvalidRequest):
        task_inbox.enqueue("s-bad", "e1", text)
    assert task_inbox.list_entries("s-bad") == []


@pytest.mark.parametrize("metadata", [
    {"value": float("nan")}, {"value": float("inf")}, {"value": {1, 2}},
    {"value": b"bytes"}, {1: "int key"}, "not an object", [1, 2],
    {"deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}},
    {"pad": "x" * (task_inbox.MAX_METADATA_BYTES + 1)},
])
def test_metadata_must_be_bounded_plain_json(store, metadata):
    with pytest.raises(task_inbox.InvalidRequest):
        task_inbox.enqueue("s-meta", "e1", "text", metadata=metadata)
    assert task_inbox.list_entries("s-meta") == []


@pytest.mark.parametrize("entry_id", [
    "", ".", "..", "../evil", "a/b", "a\\b", "a b", "a:b", "аdmin",
    "x" * (task_inbox.MAX_ID_LEN + 1), None, 17,
])
def test_entry_ids_are_bounded_and_safe(store, entry_id):
    with pytest.raises(task_inbox.InvalidRequest):
        task_inbox.enqueue("s-id", entry_id, "text")


def test_attachments_and_frozen_config_round_trip(store):
    row = task_inbox.enqueue(
        "s-cfg", "e1", "keep going", mode="follow_up", client="web1",
        metadata={"images": ["img-1"], "contexts": [{"path": "a.py", "lines": [1, 9]}]},
        config={"model": "claude-opus-5", "worker": "native", "budget": 2.5})
    stored = task_inbox.get("s-cfg", "e1")
    assert stored["mode"] == "follow_up" and stored["client"] == "web1"
    assert stored["metadata"]["images"] == ["img-1"]
    assert stored["config"] == {"model": "claude-opus-5", "worker": "native",
                                "budget": 2.5}


# ------------------------------------------------------------ reading back


def test_entries_survive_a_process_restart_in_order(tmp_path, store):
    for index, text in enumerate(["first", "second", "third"]):
        task_inbox.enqueue("s-reload", "e%d" % index, text,
                           mode="follow_up" if index else "steer")
    reader = _script(tmp_path, "reader.py", READER)
    rows = json.loads(_run(reader, store, "s-reload").stdout)
    assert [r["text"] for r in rows] == ["first", "second", "third"]
    assert [r["mode"] for r in rows] == ["steer", "follow_up", "follow_up"]


def test_sessions_listing_never_sees_an_inbox_as_a_conversation(store):
    sessions.save("real", [{"role": "user", "content": "hi"}])
    task_inbox.enqueue("real", "e1", "queued")
    task_inbox.enqueue("never-started", "e1", "queued before the first run")
    assert {row["id"] for row in sessions.recent(50)} == {"real"}
    assert sessions.latest() == "real"
    assert os.path.dirname(task_inbox.store_path("real")) == os.path.join(
        store, task_inbox.INBOX_SUBDIR)


def test_status_distinguishes_a_new_session_from_an_orphan(store):
    task_inbox.enqueue("s-orphan", "e1", "first thing said in a new chat")
    status = task_inbox.status("s-orphan")
    assert status["journal"] == "missing" and status["counts"]["pending"] == 1
    sessions.save("s-orphan", [{"role": "user", "content": "hi"}])
    assert task_inbox.status("s-orphan")["journal"] == "ok"
    with open(sessions._path("s-orphan"), "w", encoding="utf-8") as fh:
        fh.write("{torn")
    assert task_inbox.status("s-orphan")["journal"] == "invalid"
    assert [r["session"] for r in task_inbox.pending_sessions()] == ["s-orphan"]


def test_sessions_are_isolated(store):
    task_inbox.enqueue("s-a", "shared-id", "for a")
    task_inbox.enqueue("s-b", "shared-id", "for b")
    assert task_inbox.get("s-a", "shared-id")["text"] == "for a"
    assert task_inbox.get("s-b", "shared-id")["text"] == "for b"
    lease = _lease("s-a")
    try:
        task_inbox.claim("s-a", lease)
        assert [r["state"] for r in task_inbox.list_entries("s-b")] == ["pending"]
        with pytest.raises(session_owner.OwnershipRequired):
            task_inbox.claim("s-b", lease)
    finally:
        lease.release()


def test_a_torn_store_is_never_silently_reset(store):
    task_inbox.enqueue("s-torn", "e1", "accepted")
    with open(task_inbox.store_path("s-torn"), "w", encoding="utf-8") as fh:
        fh.write('{"version": 1, "session": "s-torn", "entries": ')
    with pytest.raises(task_inbox.StoreCorrupt):
        task_inbox.list_entries("s-torn")
    with pytest.raises(task_inbox.StoreCorrupt):
        task_inbox.enqueue("s-torn", "e2", "another")
    assert task_inbox.pending_sessions()[0]["error"]


def test_a_store_naming_another_session_is_refused(store):
    task_inbox.enqueue("s-x", "e1", "mine")
    doc = json.load(open(task_inbox.store_path("s-x"), encoding="utf-8"))
    doc["session"] = "s-y"
    with open(task_inbox.store_path("s-x"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    with pytest.raises(task_inbox.StoreCorrupt):
        task_inbox.get("s-x", "e1")


def test_symlinked_store_path_cannot_escape(tmp_path, store):
    """A link planted at our exact file name, inside our own directory."""
    inbox = os.path.join(store, task_inbox.INBOX_SUBDIR)
    os.makedirs(inbox, exist_ok=True)
    name = os.path.join(inbox, "s-link.json")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        os.symlink(str(outside), name)
    except (OSError, NotImplementedError, AttributeError):
        # No symlink privilege (stock Windows): a directory junction at the same
        # name is the escape that *is* available there, and must be refused too.
        elsewhere = tmp_path / "outside-dir"
        elsewhere.mkdir()
        if not _link_dir(str(elsewhere), name):
            pytest.skip("this host allows neither symlinks nor junctions")
    with pytest.raises(ValueError):
        task_inbox.enqueue("s-link", "e1", "text")
    assert task_inbox.enqueue("s-other", "e1", "text")["state"] == "pending"


# ------------------------------------------------------------ cancel / edit


def test_cancel_only_before_delivery_and_others_stay_pending(store):
    for index in range(3):
        task_inbox.enqueue("s-cancel", "e%d" % index, "request %d" % index)
    canceled = task_inbox.cancel("s-cancel", "e1", reason="changed my mind")
    assert canceled["state"] == "canceled"
    assert task_inbox.cancel("s-cancel", "e1")["state"] == "canceled"   # idempotent
    remaining = task_inbox.list_entries("s-cancel", states={"pending"})
    assert [r["id"] for r in remaining] == ["e0", "e2"]
    assert [r["text"] for r in remaining] == ["request 0", "request 2"]

    lease = _lease("s-cancel")
    try:
        claimed = task_inbox.claim("s-cancel", lease)
        assert [r["id"] for r in claimed] == ["e0", "e2"]
        with pytest.raises(task_inbox.StateConflict):
            task_inbox.cancel("s-cancel", "e0")
        assert task_inbox.get("s-cancel", "e0")["state"] == "claimed"
    finally:
        lease.release()
    # A canceled id is spent: reuse with different text is a conflict, not a swap.
    with pytest.raises(task_inbox.IdConflict):
        task_inbox.enqueue("s-cancel", "e1", "sneaky replacement")


def test_edit_before_claim_only_and_the_original_cannot_return(store):
    original = task_inbox.enqueue("s-edit", "e1", "old text", metadata={"a": 1})
    edited = task_inbox.edit("s-edit", "e1", text="new text")
    assert edited["text"] == "new text" and edited["revision"] == 1
    assert edited["digest"] != original["digest"]
    assert edited["metadata"] == {"a": 1}
    assert edited["revisions"][0]["digest"] == original["digest"]
    with pytest.raises(task_inbox.StateConflict):
        task_inbox.edit("s-edit", "e1", text="stale", expected_digest=original["digest"])
    with pytest.raises(task_inbox.IdConflict):
        task_inbox.enqueue("s-edit", "e1", "old text", metadata={"a": 1})

    lease = _lease("s-edit")
    try:
        task_inbox.claim("s-edit", lease)
        with pytest.raises(task_inbox.StateConflict):
            task_inbox.edit("s-edit", "e1", text="too late")
        assert task_inbox.get("s-edit", "e1")["text"] == "new text"
    finally:
        lease.release()


def test_purge_leaves_a_tombstone_and_refuses_open_entries(store):
    task_inbox.enqueue("s-purge", "e1", "done with this")
    task_inbox.cancel("s-purge", "e1")
    assert task_inbox.purge("s-purge", "e1")["purged"] is True
    assert task_inbox.get("s-purge", "e1")["compacted"] is True
    assert task_inbox.enqueue("s-purge", "e1", "done with this")["duplicate"] is True
    with pytest.raises(task_inbox.IdConflict):
        task_inbox.enqueue("s-purge", "e1", "a different request")
    task_inbox.enqueue("s-purge", "e2", "still waiting")
    with pytest.raises(task_inbox.StateConflict):
        task_inbox.purge("s-purge", "e2")
    assert task_inbox.get("s-purge", "e2")["state"] == "pending"


def test_compaction_bounds_the_store_without_touching_open_entries(store, monkeypatch):
    monkeypatch.setattr(task_inbox, "MAX_TERMINAL_RETAINED", 3)
    task_inbox.enqueue("s-comp", "keep-me", "still waiting")
    for index in range(6):
        task_inbox.enqueue("s-comp", "old-%d" % index, "finished %d" % index)
        task_inbox.cancel("s-comp", "old-%d" % index)
    rows = task_inbox.list_entries("s-comp")
    assert [r["id"] for r in rows] == ["keep-me", "old-3", "old-4", "old-5"]
    assert task_inbox.get("s-comp", "keep-me")["state"] == "pending"
    # The dedup contract outlives the entry it came from.
    assert task_inbox.get("s-comp", "old-0")["compacted"] is True
    assert task_inbox.enqueue("s-comp", "old-0", "finished 0")["duplicate"] is True
    with pytest.raises(task_inbox.IdConflict):
        task_inbox.enqueue("s-comp", "old-0", "a different instruction")


# ------------------------------------------------------- claim / ack / crash


def test_claim_and_ack_require_the_run_lease(store):
    task_inbox.enqueue("s-own", "e1", "steer me")
    for bad in (None, "an-owner-id", object()):
        with pytest.raises(session_owner.OwnershipRequired):
            task_inbox.claim("s-own", bad)
    lease = _lease("s-own")
    claimed = task_inbox.claim("s-own", lease)
    assert [r["state"] for r in claimed] == ["claimed"]
    lease.release()
    with pytest.raises(session_owner.OwnershipRequired):
        task_inbox.ack("s-own", lease, "e1")          # released lease is not authority
    assert task_inbox.get("s-own", "e1")["state"] == "claimed"

    other = _lease("s-own")
    try:
        with pytest.raises(session_owner.OwnershipRequired):
            task_inbox.ack("s-own", other, "e1")      # claimed by the previous owner
        assert task_inbox.reconcile("s-own", other, [])["released"] == ["e1"]
        again = task_inbox.claim("s-own", other)
        assert [r["id"] for r in again] == ["e1"] and again[0]["attempts"] == 2
        acked = task_inbox.ack("s-own", other, "e1")
        assert acked["state"] == "consumed"
        assert acked["delivery"]["message_id"] == "e1"
        assert task_inbox.ack("s-own", other, "e1")["state"] == "consumed"  # idempotent
        assert task_inbox.claim("s-own", other) == []
    finally:
        other.release()


def test_follow_ups_are_not_claimed_as_mid_run_steers(store):
    task_inbox.enqueue("s-mode", "f1", "afterwards, update the docs",
                       mode="follow_up", config={"model": "claude-opus-5"})
    task_inbox.enqueue("s-mode", "s1", "actually use pytest", mode="steer")
    lease = _lease("s-mode")
    try:
        claimed = task_inbox.claim("s-mode", lease, modes=("steer",))
        assert [r["id"] for r in claimed] == ["s1"]
        assert task_inbox.get("s-mode", "f1")["state"] == "pending"
        assert task_inbox.get("s-mode", "f1")["mode"] == "follow_up"
        assert task_inbox.get("s-mode", "f1")["config"] == {"model": "claude-opus-5"}
    finally:
        lease.release()


def test_undelivered_claims_return_to_pending_when_the_run_ends(store):
    task_inbox.enqueue("s-end", "e1", "still owed")
    task_inbox.enqueue("s-end", "e2", "also owed")
    lease = _lease("s-end")
    try:
        task_inbox.claim("s-end", lease)
        task_inbox.ack("s-end", lease, "e1")
        # The run is canceled after one insertion; the other is still the user's.
        assert task_inbox.release_claims("s-end", lease, reason="canceled") == ["e2"]
    finally:
        lease.release()
    states = {r["id"]: r["state"] for r in task_inbox.list_entries("s-end")}
    assert states == {"e1": "consumed", "e2": "pending"}


def test_crash_between_journal_write_and_ack_does_not_repeat_the_user(tmp_path, store):
    sessions.save("s-crash", [{"role": "user", "content": "start"}])
    task_inbox.enqueue("s-crash", "e1", "switch to pytest")
    crasher = _script(tmp_path, "crash.py", CRASH_AFTER_JOURNAL)
    result = _run(crasher, store, "s-crash")
    assert "JOURNALED 1" in result.stdout, result.stderr
    assert result.returncode == 9                      # died before acknowledging
    assert task_inbox.get("s-crash", "e1")["state"] == "claimed"

    # Recovery: the next executor takes the lease the dead process cannot hold,
    # and asks the transcript what was actually delivered.
    lease = _lease("s-crash", label="recovery")
    try:
        messages = sessions.load_checked("s-crash")["session"]["messages"]
        delivered = task_inbox.delivered_ids(messages)
        assert delivered == ["e1"]
        outcome = task_inbox.reconcile("s-crash", lease, delivered)
        assert outcome == {"consumed": ["e1"], "released": [], "unknown": [],
                           "conflicts": []}
        assert task_inbox.claim("s-crash", lease) == []      # never inserted twice
    finally:
        lease.release()
    entry = task_inbox.get("s-crash", "e1")
    assert entry["state"] == "consumed" and entry["delivery"]["recovered"] is True
    assert sum(1 for m in messages if m.get("inbox_id") == "e1") == 1


def test_crash_before_the_journal_write_keeps_the_request_pending(tmp_path, store):
    task_inbox.enqueue("s-lost", "e1", "do not lose me")
    crasher = _script(tmp_path, "crash.py", CRASH_BEFORE_JOURNAL)
    result = _run(crasher, store, "s-lost")
    assert "CLAIMED 1" in result.stdout, result.stderr
    lease = _lease("s-lost", label="recovery")
    try:
        journal = sessions.load_checked("s-lost")
        assert journal["status"] == "missing"
        outcome = task_inbox.reconcile("s-lost", lease, task_inbox.delivered_ids(
            (journal["session"] or {}).get("messages")))
        assert outcome["released"] == ["e1"] and outcome["consumed"] == []
        assert [r["id"] for r in task_inbox.claim("s-lost", lease)] == ["e1"]
    finally:
        lease.release()


def test_reconcile_leaves_the_live_owners_own_claim_alone(store):
    task_inbox.enqueue("s-live", "e1", "in flight right now")
    lease = _lease("s-live")
    try:
        task_inbox.claim("s-live", lease)
        outcome = task_inbox.reconcile("s-live", lease, [])
        assert outcome["released"] == []
        assert task_inbox.get("s-live", "e1")["state"] == "claimed"
        assert task_inbox.reconcile("s-live", lease, ["not-ours"])["unknown"] == \
            ["not-ours"]
    finally:
        lease.release()


def test_inbox_stays_writable_while_a_run_owns_the_session(tmp_path, store):
    """A steer typed mid-run must not wait behind the run's own lease."""
    script = _script(tmp_path, "enq_owned.py", ENQUEUE_WHILE_OWNED)
    with session_owner.own("s-busy", label="executor"):
        started = time.monotonic()
        result = _run(script, store, "s-busy", "typed-1", timeout=60)
        elapsed = time.monotonic() - started
    assert result.stdout.startswith("OK pending"), (result.stdout, result.stderr)
    assert elapsed < 30
    assert [r["id"] for r in task_inbox.list_entries("s-busy")] == ["typed-1"]


# ---------------------------------------------------------- journal contract


def test_journal_message_is_recognised_as_the_persons_own_instruction(store):
    task_inbox.enqueue("s-msg", "e1", "use the other library", mode="steer")
    entry = task_inbox.get("s-msg", "e1")
    message = task_inbox.journal_message(entry)
    assert message == {"role": "user", "content": "use the other library",
                       "source": "user", "kind": "steer", "inbox_id": "e1"}
    assert compaction.is_user_message(message) is True
    # The trap this shape avoids: a kind without a source reads as harness text
    # and compaction may drop the user's actual instruction.
    assert compaction.is_user_message(
        {"role": "user", "content": "use the other library", "kind": "steer"}) is False
    assert task_inbox.delivered_ids([message, {"role": "assistant", "content": "ok"}]) \
        == ["e1"]


def test_the_drain_contract_is_stated_where_the_integrator_will_look(store):
    """Point 4: the loop's steering callback must not append.

    `loop.Harness._drain_steering` returns text and *its caller* appends it
    (`loop.py:1242`, `:2181`).  A callback that both appends `journal_message`
    and returns the text would insert the same instruction twice, so the
    contract has to be written down next to the API, not only in a report."""
    assert "append" in task_inbox.DRAIN_CONTRACT and "ack" in task_inbox.DRAIN_CONTRACT
    assert "return []" in task_inbox.DRAIN_CONTRACT
    assert "_drain_steering" in task_inbox.__doc__

    # The shape the contract describes, executed once: claim -> append -> durable
    # checkpoint -> ack, with the callback returning text only.
    sessions.save("s-drain", [{"role": "user", "content": "start"}])
    task_inbox.enqueue("s-drain", "e1", "actually use pytest")
    messages = list(sessions.load_checked("s-drain")["session"]["messages"])
    lease = _lease("s-drain")
    try:
        spoken = []
        for entry in task_inbox.claim("s-drain", lease, modes=("steer",)):
            messages.append(task_inbox.journal_message(entry))       # exactly once
            sessions.checkpoint("s-drain", messages, state="turn_boundary")
            task_inbox.ack("s-drain", lease, entry["id"])
            spoken.append(entry["text"])
        assert spoken == ["actually use pytest"]
    finally:
        task_inbox.release_claims("s-drain", lease, reason="run ended")
        lease.release()
    persisted = sessions.load_checked("s-drain")["session"]["messages"]
    assert sum(1 for m in persisted if m.get("inbox_id") == "e1") == 1
    assert task_inbox.get("s-drain", "e1")["state"] == "consumed"


# ------------------------------------------------- store validation (table)


def _rich_store(session="s-rich"):
    """Produce every persisted form this module can write, in one store.

    The validator's job is to reject what we could not have written; the first
    thing that has to hold is that it accepts everything we *do* write — a
    released-then-reclaimed entry, an edited one with a revision history, a
    canceled one, a consumed one with a delivery record, and a tombstone.
    """
    task_inbox.enqueue(session, "pending-1", "still waiting", metadata={"a": 1})
    task_inbox.enqueue(session, "edited-1", "first draft", client="web1")
    task_inbox.edit(session, "edited-1", text="second draft", config={"model": "m"})
    task_inbox.enqueue(session, "canceled-1", "never mind")
    task_inbox.cancel(session, "canceled-1", reason="changed my mind")
    task_inbox.enqueue(session, "purged-1", "done with this")
    task_inbox.cancel(session, "purged-1")
    task_inbox.purge(session, "purged-1")                    # leaves a tombstone
    task_inbox.enqueue(session, "consumed-1", "delivered")
    task_inbox.enqueue(session, "claimed-1", "in flight", mode="follow_up")
    lease = _lease(session)
    try:
        task_inbox.claim(session, lease, limit=99, modes=task_inbox.MODES)
        task_inbox.ack(session, lease, "consumed-1")
        task_inbox.release_claims(session, lease, entry_ids=["pending-1", "edited-1"],
                                  reason="run ended")
    finally:
        lease.release()
    return session


def _read(session):
    with open(task_inbox.store_path(session), encoding="utf-8") as fh:
        return json.load(fh)


def _write(session, doc):
    with open(task_inbox.store_path(session), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def _entry(doc, entry_id):
    return next(e for e in doc["entries"] if e["id"] == entry_id)


@pytest.fixture
def rich(store):
    session = _rich_store()
    return session, _read(session)


def _duplicate_id(doc):
    _entry(doc, "claimed-1")["id"] = "pending-1"


def _duplicate_seq(doc):
    _entry(doc, "claimed-1")["seq"] = _entry(doc, "pending-1")["seq"]


def _bytes_not_a_number(doc):
    _entry(doc, "pending-1")["bytes"] = "bad"


def _bytes_negative(doc):
    _entry(doc, "pending-1")["bytes"] = -1000000


def _bytes_understated(doc):
    _entry(doc, "pending-1")["bytes"] = 1


def _bytes_boolean(doc):
    _entry(doc, "pending-1")["bytes"] = True


def _digest_forged(doc):
    _entry(doc, "pending-1")["digest"] = "0" * 64


def _digest_malformed(doc):
    _entry(doc, "pending-1")["digest"] = "not a digest"


def _text_emptied(doc):
    _entry(doc, "pending-1")["text"] = "   "


def _text_wrong_type(doc):
    _entry(doc, "pending-1")["text"] = ["a", "list"]


def _text_with_nul(doc):
    _entry(doc, "pending-1")["text"] = "before\x00after"


def _text_oversized(doc):
    _entry(doc, "pending-1")["text"] = "x" * (task_inbox.MAX_TEXT_BYTES + 1)


def _metadata_not_object(doc):
    _entry(doc, "pending-1")["metadata"] = [1, 2, 3]


def _metadata_too_deep(doc):
    _entry(doc, "pending-1")["metadata"] = {
        "a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}


def _mode_unknown(doc):
    _entry(doc, "pending-1")["mode"] = "sudo"


def _state_unknown(doc):
    _entry(doc, "pending-1")["state"] = "delivered"


def _id_traversal(doc):
    _entry(doc, "pending-1")["id"] = "../evil"


def _message_id_missing(doc):
    _entry(doc, "pending-1")["message_id"] = None


def _foreign_session(doc):
    _entry(doc, "pending-1")["session"] = "someone-else"


def _claim_on_pending(doc):
    _entry(doc, "pending-1")["claim"] = {"owner": "abc", "pid": 1, "at": 1.0}


def _claim_missing(doc):
    _entry(doc, "claimed-1")["claim"] = None


def _claim_ownerless(doc):
    _entry(doc, "claimed-1")["claim"] = {"pid": 1, "at": 1.0}


def _claim_malformed_time(doc):
    _entry(doc, "claimed-1")["claim"] = {"owner": "abc", "pid": 1, "at": "yesterday"}


def _delivery_missing(doc):
    _entry(doc, "consumed-1")["delivery"] = None


def _delivery_other_id(doc):
    _entry(doc, "consumed-1")["delivery"]["message_id"] = "some-other-message"


def _delivery_on_pending(doc):
    _entry(doc, "pending-1")["delivery"] = {"message_id": "pending-1", "at": 1.0,
                                            "recovered": False}


def _delivery_recovered_flag(doc):
    _entry(doc, "consumed-1")["delivery"]["recovered"] = "yes"


def _canceled_record_on_pending(doc):
    _entry(doc, "pending-1")["canceled"] = {"at": 1.0, "reason": "no"}


def _next_seq_behind(doc):
    doc["next_seq"] = 1


def _next_seq_wrong_type(doc):
    doc["next_seq"] = "2"


def _updated_overflows_float(doc):
    doc["updated"] = 10 ** 400


def _created_negative(doc):
    _entry(doc, "pending-1")["created"] = -1


def _created_absurd(doc):
    _entry(doc, "pending-1")["created"] = 1e300


def _attempts_negative(doc):
    _entry(doc, "claimed-1")["attempts"] = -5


def _revision_digest_garbage(doc):
    _entry(doc, "edited-1")["revisions"] = [{"digest": "nope", "at": 1.0}]


def _revisions_unbounded(doc):
    _entry(doc, "edited-1")["revisions"] = [{"digest": "a" * 64, "at": 1.0}] * 50


def _tombstone_state(doc):
    doc["tombstones"][0]["state"] = "pending"


def _tombstone_digest(doc):
    doc["tombstones"][0]["digest"] = "x"


def _tombstone_shadows_entry(doc):
    doc["tombstones"][0]["id"] = "pending-1"


def _tombstone_future_seq(doc):
    doc["tombstones"][0]["seq"] = doc["next_seq"] + 5


def _entry_field_missing(doc):
    del _entry(doc, "pending-1")["metadata"]


def _tombstone_field_missing(doc):
    del doc["tombstones"][0]["at"]


def _entries_not_a_list(doc):
    doc["entries"] = {"pending-1": {}}


def _tombstones_not_a_list(doc):
    doc["tombstones"] = "none"


CORRUPTIONS = {
    "duplicate id": _duplicate_id,
    "duplicate seq": _duplicate_seq,
    "bytes not a number": _bytes_not_a_number,
    "bytes negative": _bytes_negative,
    "bytes understated": _bytes_understated,
    "bytes boolean": _bytes_boolean,
    "digest forged": _digest_forged,
    "digest malformed": _digest_malformed,
    "text emptied": _text_emptied,
    "text wrong type": _text_wrong_type,
    "text with nul": _text_with_nul,
    "text oversized": _text_oversized,
    "metadata not an object": _metadata_not_object,
    "metadata too deep": _metadata_too_deep,
    "mode unknown": _mode_unknown,
    "state unknown": _state_unknown,
    "id traversal": _id_traversal,
    "message_id missing": _message_id_missing,
    "foreign session": _foreign_session,
    "claim on a pending entry": _claim_on_pending,
    "claim missing": _claim_missing,
    "claim without an owner": _claim_ownerless,
    "claim with a malformed time": _claim_malformed_time,
    "delivery missing": _delivery_missing,
    "delivery naming another message": _delivery_other_id,
    "delivery on a pending entry": _delivery_on_pending,
    "delivery recovered flag": _delivery_recovered_flag,
    "canceled record on a pending entry": _canceled_record_on_pending,
    "next_seq behind its entries": _next_seq_behind,
    "next_seq wrong type": _next_seq_wrong_type,
    "updated overflows float()": _updated_overflows_float,
    "created negative": _created_negative,
    "created absurd": _created_absurd,
    "attempts negative": _attempts_negative,
    "revision digest garbage": _revision_digest_garbage,
    "revisions unbounded": _revisions_unbounded,
    "tombstone state": _tombstone_state,
    "tombstone digest": _tombstone_digest,
    "tombstone shadowing an entry": _tombstone_shadows_entry,
    "tombstone sequence from the future": _tombstone_future_seq,
    "entry field missing": _entry_field_missing,
    "tombstone field missing": _tombstone_field_missing,
    "entries not a list": _entries_not_a_list,
    "tombstones not a list": _tombstones_not_a_list,
}


@pytest.mark.parametrize("case", sorted(CORRUPTIONS))
def test_a_forged_or_torn_store_is_refused_by_every_operation(rich, case):
    """Validation is not a read-path nicety: the caps and the dedup contract
    rest on it.  ``bytes`` is what the byte cap sums, ``digest`` is what decides
    whether a retried id is the same request, and ``claim``/``delivery`` are what
    recovery believes about who has what.  A store that says one thing and holds
    another is refused identically by every entry point, and is left on disk
    exactly as found — rewriting it would destroy the only evidence about
    somebody's accepted instruction."""
    session, doc = rich
    doc = copy.deepcopy(doc)
    CORRUPTIONS[case](doc)
    _write(session, doc)
    lease = _lease(session)
    try:
        operations = (
            lambda: task_inbox.list_entries(session),
            lambda: task_inbox.get(session, "pending-1"),
            lambda: task_inbox.status(session),
            lambda: task_inbox.enqueue(session, "brand-new", "a new instruction"),
            lambda: task_inbox.edit(session, "pending-1", text="rewritten"),
            lambda: task_inbox.cancel(session, "pending-1"),
            lambda: task_inbox.purge(session, "canceled-1"),
            lambda: task_inbox.claim(session, lease),
            lambda: task_inbox.ack(session, lease, "claimed-1"),
            lambda: task_inbox.release_claims(session, lease),
            lambda: task_inbox.reconcile(session, lease, ["pending-1"]),
        )
        for operation in operations:
            with pytest.raises(task_inbox.StoreCorrupt):
                operation()
    finally:
        lease.release()
    assert _read(session) == doc, "a refused store was rewritten"
    assert task_inbox.pending_sessions()[0]["error"], "it must stay visible for repair"


def test_every_form_this_module_writes_survives_its_own_validator(tmp_path, store):
    session = _rich_store()
    states = {r["id"]: r["state"] for r in task_inbox.list_entries(session)}
    assert states == {"pending-1": "pending", "edited-1": "pending",
                      "canceled-1": "canceled", "consumed-1": "consumed",
                      "claimed-1": "claimed"}
    entry = task_inbox.get(session, "edited-1")
    assert entry["revision"] == 1 and entry["revisions"] and entry["released"]
    assert task_inbox.get(session, "purged-1")["compacted"] is True
    # And in a fresh interpreter that only ever sees the bytes on disk.
    rows = json.loads(_run(_script(tmp_path, "reader.py", READER), store, session).stdout)
    assert [r["id"] for r in rows] == ["pending-1", "edited-1", "canceled-1",
                                       "consumed-1", "claimed-1"]


def test_a_store_larger_than_this_module_writes_is_never_parsed(store):
    task_inbox.enqueue("s-huge", "e1", "accepted")
    with open(task_inbox.store_path("s-huge"), "w", encoding="utf-8") as fh:
        fh.write('{"version": 1, "session": "s-huge", "entries": [], "pad": "')
        fh.write("x" * (task_inbox.MAX_STORE_BYTES + 1))
        fh.write('"}')
    with pytest.raises(task_inbox.StoreCorrupt) as info:
        task_inbox.list_entries("s-huge")
    assert "bytes" in str(info.value)


def test_a_deeply_nested_store_is_a_refusal_not_a_crash(store):
    task_inbox.enqueue("s-deep", "e1", "accepted")
    with open(task_inbox.store_path("s-deep"), "w", encoding="utf-8") as fh:
        fh.write("[" * 100000 + "]" * 100000)
    with pytest.raises(task_inbox.StoreCorrupt):
        task_inbox.list_entries("s-deep")


def test_a_non_finite_number_in_the_store_is_refused(store):
    task_inbox.enqueue("s-nan", "e1", "accepted")
    blob = json.dumps(_read("s-nan")).replace('"created":', '"created": NaN, "was":', 1)
    with open(task_inbox.store_path("s-nan"), "w", encoding="utf-8") as fh:
        fh.write(blob)
    with pytest.raises(task_inbox.StoreCorrupt):
        task_inbox.list_entries("s-nan")


# --------------------------------------------------- the id delivery carries


def test_ack_cannot_record_a_message_id_the_journal_does_not_carry(store):
    """The stable id is assigned at acceptance, before anything is inserted.

    ``journal_message`` stamps ``inbox_id`` from the entry, so that is the only
    name ``reconcile`` can match after a crash.  An ack naming something else
    would record a delivery no transcript can corroborate — the inbox would say
    "delivered" while recovery calls the same id unknown."""
    row = task_inbox.enqueue("s-mid", "e1", "steer me")
    assert row["message_id"] == "e1"
    lease = _lease("s-mid")
    try:
        entry = task_inbox.claim("s-mid", lease)[0]
        assert task_inbox.journal_message(entry)["inbox_id"] == "e1"
        with pytest.raises(task_inbox.InvalidRequest) as info:
            task_inbox.ack("s-mid", lease, "e1", message_id="some-other-id")
        assert "journaled as" in str(info.value)
        assert task_inbox.get("s-mid", "e1")["state"] == "claimed"   # nothing recorded
        acked = task_inbox.ack("s-mid", lease, "e1", message_id="e1")  # confirming is ok
        assert acked["delivery"]["message_id"] == "e1"
        # ...and recovery agrees about the id, so a journal that carries it is
        # not reported as an unknown delivery.
        assert task_inbox.reconcile("s-mid", lease, ["e1"]) == {
            "consumed": [], "released": [], "unknown": [], "conflicts": []}
    finally:
        lease.release()


def test_a_retry_of_the_original_after_an_edit_is_a_named_conflict(store):
    task_inbox.enqueue("s-rev", "e1", "first draft")
    task_inbox.edit("s-rev", "e1", text="second draft")
    with pytest.raises(task_inbox.IdConflict) as info:
        task_inbox.enqueue("s-rev", "e1", "first draft")    # the original POST, retried
    assert "revision 1" in str(info.value)
    assert task_inbox.get("s-rev", "e1")["text"] == "second draft"
    # Edited back: the stored payload really is identical now, so the retry is a
    # duplicate — and `revision` is how a caller sees it is not untouched.
    task_inbox.edit("s-rev", "e1", text="first draft")
    again = task_inbox.enqueue("s-rev", "e1", "first draft")
    assert again["duplicate"] is True and again["revision"] == 2


# ------------------------------------------------------------ root binding


def test_a_lease_from_another_store_cannot_deliver_here(tmp_path, store):
    """A lease is authority over one id *in one directory*.

    Session ids are unique per store, not globally.  A lease taken over
    ``other/run-7`` must not let anything claim, ack or reconcile the different
    conversation called ``run-7`` in the real store."""
    other = str(tmp_path / "other")
    os.makedirs(other)
    task_inbox.enqueue("run-7", "e1", "typed into the real store")
    task_inbox.enqueue("run-7", "e1", "typed into the other store", directory=other)
    stray = session_owner.acquire("run-7", directory=other)
    try:
        for operation in (lambda: task_inbox.claim("run-7", stray),
                          lambda: task_inbox.ack("run-7", stray, "e1"),
                          lambda: task_inbox.release_claims("run-7", stray),
                          lambda: task_inbox.reconcile("run-7", stray, ["e1"])):
            with pytest.raises(session_owner.OwnershipRequired):
                operation()
        assert task_inbox.get("run-7", "e1")["state"] == "pending"
        assert task_inbox.get("run-7", "e1")["text"] == "typed into the real store"
        # The lease does work where it belongs, and only there.
        assert [e["id"] for e in task_inbox.claim("run-7", stray, directory=other)] == \
            ["e1"]
    finally:
        stray.release()


def test_a_lease_does_not_follow_a_changed_environment(tmp_path, store, monkeypatch):
    """COLLIE_SESSIONS_DIR is read on every call; the lease is read once."""
    moved = str(tmp_path / "moved")
    os.makedirs(moved)
    task_inbox.enqueue("s-move", "e1", "accepted here")
    lease = _lease("s-move")
    try:
        assert [e["id"] for e in task_inbox.claim("s-move", lease)] == ["e1"]
        monkeypatch.setenv("COLLIE_SESSIONS_DIR", moved)
        # Same call, same arguments, different store: refused rather than
        # silently acting on an unrelated session that happens to share the id.
        with pytest.raises(session_owner.OwnershipRequired):
            task_inbox.ack("s-move", lease, "e1")
        assert task_inbox.ack("s-move", lease, "e1", directory=lease.root)["state"] == \
            "consumed"
    finally:
        lease.release()


def test_an_inherited_lease_cannot_deliver_on_the_owners_behalf(store, monkeypatch):
    """A forked child holds the parent's file handle but not its responsibility.

    Two processes acting on one lease is exactly the double-delivery this module
    exists to prevent, and after `fork` the inherited object looks live: the
    handle is open and the owner id matches the claim."""
    task_inbox.enqueue("s-inherit", "in-flight", "do the thing")
    lease = _lease("s-inherit")
    try:
        claimed = task_inbox.claim("s-inherit", lease)
        assert [e["id"] for e in claimed] == ["in-flight"]
        monkeypatch.setattr(lease, "pid", os.getpid() + 1000)      # as if inherited
        for operation in (lambda: task_inbox.claim("s-inherit", lease),
                          lambda: task_inbox.ack("s-inherit", lease, "in-flight"),
                          lambda: task_inbox.release_claims("s-inherit", lease),
                          lambda: task_inbox.reconcile("s-inherit", lease, [])):
            with pytest.raises(session_owner.OwnershipRequired):
                operation()
        assert task_inbox.get("s-inherit", "in-flight")["state"] == "claimed"
        # The owning process is unaffected and still finishes its own delivery.
        monkeypatch.setattr(lease, "pid", os.getpid())
        assert task_inbox.ack("s-inherit", lease, "in-flight")["state"] == "consumed"
    finally:
        lease.release()


def test_a_custom_sessions_directory_is_fully_usable(tmp_path, store):
    """Containment must not cost the supported `directory=` argument."""
    custom = str(tmp_path / "custom sessions")
    os.makedirs(custom)
    task_inbox.enqueue("s-custom", "e1", "hello", directory=custom)
    lease = session_owner.acquire("s-custom", directory=custom)
    try:
        assert [e["id"] for e in task_inbox.claim("s-custom", lease, directory=custom)] \
            == ["e1"]
        task_inbox.ack("s-custom", lease, "e1", directory=custom)
    finally:
        lease.release()
    assert task_inbox.status("s-custom", directory=custom)["counts"]["consumed"] == 1
    assert os.path.dirname(task_inbox.store_path("s-custom", directory=custom)) == \
        os.path.join(os.path.realpath(custom), task_inbox.INBOX_SUBDIR)
    assert task_inbox.list_entries("s-custom") == []      # the env store is untouched
    assert task_inbox.pending_sessions(directory=custom) == []


def test_a_linked_inbox_directory_cannot_redirect_the_store(tmp_path, store):
    """Validating the file name is not enough when the directory is the link.

    Every path under a redirected inbox directory resolves consistently *with
    that directory*, so the only check that catches it is against the resolved
    sessions root."""
    outside = tmp_path / "outside-inbox"
    outside.mkdir()
    if not _link_dir(str(outside), os.path.join(store, task_inbox.INBOX_SUBDIR)):
        pytest.skip("this host allows neither symlinks nor junctions")
    for operation in (lambda: task_inbox.enqueue("s-escape", "e1", "text"),
                      lambda: task_inbox.get("s-escape", "e1"),
                      lambda: task_inbox.status("s-escape"),
                      lambda: task_inbox.pending_sessions()):
        with pytest.raises(ValueError):
            operation()
    assert os.listdir(outside) == [], "the escaped directory was written to"


# ------------------------------------------- crash recovery, whole processes


def test_a_crash_before_ack_leaves_a_store_a_fresh_process_can_recover(tmp_path, store):
    """The window the design exists for, end to end, across three processes.

    A run claims two instructions, writes both into the transcript, and dies
    before acknowledging either.  What it leaves behind must still pass
    validation — a store that failed its own checks would strand the recovery
    too — and the next executor, a different interpreter holding the lease the
    dead one cannot, must settle both from the journal and re-deliver neither."""
    sessions.save("s-recover", [{"role": "user", "content": "start"}])
    task_inbox.enqueue("s-recover", "e1", "switch to pytest")
    task_inbox.enqueue("s-recover", "e2", "and cover the crash path")
    crash = _run(_script(tmp_path, "crash.py", CRASH_AFTER_JOURNAL), store, "s-recover")
    assert "JOURNALED 2" in crash.stdout, crash.stderr
    assert crash.returncode == 9

    assert {r["id"]: r["state"] for r in task_inbox.list_entries("s-recover")} == {
        "e1": "claimed", "e2": "claimed"}
    recovery = _run(_script(tmp_path, "recover.py", RECOVER), store, "s-recover")
    outcome = json.loads(recovery.stdout or "{}")
    assert outcome["journal"] == "ok", recovery.stderr
    assert sorted(outcome["consumed"]) == ["e1", "e2"]
    assert outcome["released"] == [] and outcome["unknown"] == []
    assert outcome["claimed_again"] == [], "an already-delivered steer was re-issued"
    assert outcome["copies"] == 2, "the transcript must hold exactly one copy of each"
    for entry_id in ("e1", "e2"):
        entry = task_inbox.get("s-recover", entry_id)
        assert entry["state"] == "consumed" and entry["delivery"]["recovered"] is True
