"""Ownership tests that actually contend: real threads, real processes, real kills.

The claim under test is narrow and load-bearing — "exactly one executor per
session id, and the lease dies with its holder" — so these tests spend real
subprocesses instead of asserting against a mock lock.
"""
import gc
import os
import subprocess
import sys
import threading
import time
import weakref

import pytest

from harness import plat, session_owner, sessions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    return str(directory)


def test_busy_probe_checks_the_os_during_identity_publication(store):
    lease = session_owner.acquire("publishing")
    lease.release()
    assert session_owner.describe("publishing")["released"]
    path = session_owner.lock_path("publishing")
    with open(path, "rb") as source:
        before = source.read()
    # A different executor took the OS lock and has not replaced the previous
    # released identity yet. Advisory metadata alone gives the wrong answer.
    handle = session_owner._open(path)
    session_owner._lock(handle)
    try:
        assert session_owner.probe_busy("publishing") is True
    finally:
        session_owner._unlock(handle)
        handle.close()
    assert session_owner.probe_busy("publishing") is False
    with open(path, "rb") as source:
        assert source.read() == before, "a display probe must not publish an owner"


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _child_env(store):
    env = dict(os.environ)
    env["COLLIE_SESSIONS_DIR"] = store
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _spawn(script, store, *args):
    return subprocess.Popen([sys.executable, script, *args], cwd=ROOT,
                            env=_child_env(store), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            **plat.no_window_kwargs())


def _run(script, store, *args, timeout=90):
    return subprocess.run([sys.executable, script, *args], cwd=ROOT,
                          env=_child_env(store), capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout, **plat.no_window_kwargs())


def _link_dir(target, link):
    """Point `link` at `target`, however this host lets an unprivileged user.

    A symlink needs SeCreateSymbolicLinkPrivilege on Windows, but a *directory
    junction* needs nothing at all and `os.path.realpath` follows it exactly the
    same way — so the escape this guards against is reachable on a stock Windows
    account, and so is the test for it.
    """
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


def _wait_for_lease(session, timeout=20.0):
    """Poll try_acquire until the OS has published the previous holder's death."""
    deadline = time.monotonic() + timeout
    while True:
        lease = session_owner.try_acquire(session)
        if lease is not None:
            return lease
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


HOLDER = """
import os, sys, time
from harness import session_owner

lease = session_owner.acquire(sys.argv[1], label=sys.argv[2])
print("HELD %s %d" % (lease.owner_id, os.getpid()), flush=True)
if sys.argv[3] == "hold":
    time.sleep(300)
elif sys.argv[3] == "release":
    lease.release()
    print("RELEASED", flush=True)
"""

CONTENDER = """
import sys
from harness import session_owner

lease = session_owner.try_acquire(sys.argv[1])
print("WON" if lease is not None else "BUSY", flush=True)
"""


def test_one_owner_per_session_in_process(store):
    first = session_owner.acquire("run-1", label="cli")
    try:
        assert session_owner.try_acquire("run-1") is None
        with pytest.raises(session_owner.SessionBusy) as info:
            session_owner.acquire("run-1", label="web")
        assert info.value.session == "run-1"
        assert info.value.owner["pid"] == os.getpid()
        assert info.value.owner["label"] == "cli"
        assert info.value.owner["owner"] == first.owner_id
        # A different session is unaffected: the lease is per id, not global.
        other = session_owner.acquire("run-2")
        other.release()
    finally:
        first.release()
    reacquired = session_owner.acquire("run-1")
    assert reacquired.owner_id != first.owner_id
    reacquired.release()


def test_release_is_idempotent_and_reported(store):
    lease = session_owner.acquire("run-idem", label="web")
    assert lease.held
    lease.release()
    lease.release()
    assert not lease.held
    with pytest.raises(session_owner.OwnershipRequired):
        lease.assert_held()
    record = session_owner.describe("run-idem")
    assert record["present"] and record["released"]
    assert record["owner"]["owner"] == lease.owner_id


def test_context_manager_releases_on_error(store):
    with pytest.raises(RuntimeError):
        with session_owner.own("run-cm") as lease:
            assert lease.held
            raise RuntimeError("run failed")
    assert session_owner.try_acquire("run-cm") is not None


def test_threads_produce_exactly_one_owner(store):
    winners, ready = [], threading.Barrier(12)
    lock = threading.Lock()

    def attempt():
        ready.wait()
        lease = session_owner.try_acquire("run-threads")
        if lease is not None:
            with lock:
                winners.append(lease)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(winners) == 1
    winners[0].release()


def test_second_process_cannot_execute_the_same_session(tmp_path, store):
    holder = _script(tmp_path, "holder.py", HOLDER)
    proc = _spawn(holder, store, "run-mp", "child", "hold")
    try:
        line = proc.stdout.readline().strip()
        assert line.startswith("HELD"), (line, proc.stderr.read())
        child_owner, child_pid = line.split()[1], int(line.split()[2])

        assert session_owner.try_acquire("run-mp") is None
        with pytest.raises(session_owner.SessionBusy) as info:
            session_owner.acquire("run-mp")
        assert info.value.owner["pid"] == child_pid
        assert info.value.owner["owner"] == child_owner
        assert info.value.owner["label"] == "child"

        # A third process gets the same answer, from the OS rather than from us.
        contender = _script(tmp_path, "contender.py", CONTENDER)
        assert _run(contender, store, "run-mp").stdout.strip() == "BUSY"
    finally:
        proc.kill()
        proc.wait(timeout=30)
    # Killed, not asked to clean up: the kernel drops the lock, no timeout needed.
    lease = _wait_for_lease("run-mp")
    assert lease is not None
    lease.release()


def test_clean_child_release_frees_the_session(tmp_path, store):
    holder = _script(tmp_path, "holder.py", HOLDER)
    result = _run(holder, store, "run-clean", "child", "release")
    assert "RELEASED" in result.stdout, result.stderr
    lease = session_owner.acquire("run-clean")
    assert lease.held
    lease.release()


def test_lock_file_is_stable_and_never_recreated(store):
    path = session_owner.lock_path("run-stable")
    with session_owner.own("run-stable"):
        first = os.stat(path)
    assert os.path.exists(path), "releasing must not unlink the lock path"
    with session_owner.own("run-stable"):
        second = os.stat(path)
    if first.st_ino:
        assert (first.st_ino, first.st_dev) == (second.st_ino, second.st_dev)
    else:                                   # filesystem without inode numbers
        assert first.st_ctime == second.st_ctime


def test_dropped_lease_leaves_no_global_reference(store):
    lease = session_owner.try_acquire("run-gc")
    assert lease is not None
    ref = weakref.ref(lease)
    del lease
    gc.collect()
    assert ref() is None, "a module-level registry is retaining the lease"
    # Both the OS lock and the in-process guard must have gone with it.
    regained = session_owner.try_acquire("run-gc")
    assert regained is not None
    regained.release()


def test_invalid_and_escaping_ids_are_refused(store):
    for bad in ("", ".", "..", "../evil", "a/b", "a\\b", "a:b", "a\x00b", "x" * 129,
                None, 17, b"run"):
        with pytest.raises(ValueError):
            session_owner.try_acquire(bad)


def test_symlinked_lock_path_cannot_escape(tmp_path, store):
    runtime = os.path.join(store, session_owner.RUNTIME_SUBDIR)
    os.makedirs(runtime, exist_ok=True)
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"\0")
    link = os.path.join(runtime, "run-link" + session_owner.OWNER_SUFFIX)
    try:
        os.symlink(str(outside), link)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip("file symlinks unavailable here: %s "
                    "(the junction case is covered separately)" % exc)
    with pytest.raises(ValueError):
        session_owner.try_acquire("run-link")
    assert outside.read_bytes() == b"\0", "the escaped file was written to"


def test_lease_does_not_require_a_journal_but_reports_its_state(store):
    lease = session_owner.acquire("run-new")
    try:
        assert lease.identity["journal_present"] is False
        assert lease.journal() == "missing"      # a run that is just starting
        sessions.save("run-new", [{"role": "user", "content": "hello"}])
        assert lease.journal() == "ok"
        with open(sessions._path("run-new"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert lease.journal() == "invalid"      # bookkeeping, not executable work
    finally:
        lease.release()


def test_lease_does_not_block_journal_or_short_transactions(store):
    """The whole reason this is not sessions._locked: writes stay responsive."""
    sessions.save("run-live", [{"role": "user", "content": "start"}])
    with session_owner.own("run-live"):
        done = []

        def writer():
            sessions.append_run_receipt("run-live", {"note": "while owned"})
            done.append(sessions.load_checked("run-live")["status"])

        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(10)
        assert done == ["ok"], "holding the run lease blocked a journal transaction"


def test_lease_meta_must_be_bounded_json(store):
    with pytest.raises(ValueError):
        session_owner.try_acquire("run-meta", meta={"run": object()})
    with pytest.raises(ValueError):
        session_owner.try_acquire("run-meta", meta={"pad": "x" * 4096})
    lease = session_owner.acquire("run-meta", meta={"run": "abc", "turn": 3})
    try:
        assert lease.info()["meta"] == {"run": "abc", "turn": 3}
        assert lease.info()["held"] is True
    finally:
        lease.release()
    # The identity survives in the sidecar for the next caller's error message.
    assert session_owner.describe("run-meta")["owner"]["meta"]["run"] == "abc"


def test_assert_held_rejects_a_lease_for_another_session(store):
    lease = session_owner.acquire("run-a")
    try:
        with pytest.raises(session_owner.OwnershipRequired):
            lease.assert_held("run-b")
        assert lease.assert_held("run-a") is lease
    finally:
        lease.release()


# --------------------------------------------------------- root binding


def test_a_lease_is_authority_over_one_sessions_root_only(tmp_path, store):
    """A session id is unique inside a directory, not across them."""
    other = tmp_path / "other-sessions"
    other.mkdir()
    lease = session_owner.acquire("run-7", label="store-a")
    try:
        assert lease.root == os.path.realpath(store)
        assert lease.assert_held("run-7", directory=store) is lease
        with pytest.raises(session_owner.OwnershipRequired) as info:
            lease.assert_held("run-7", directory=str(other))
        assert str(other) in str(info.value)
        # ...and the identically named session in the other store is genuinely
        # free: the lease says nothing about it, so a second executor may run it.
        elsewhere = session_owner.acquire("run-7", directory=str(other))
        assert elsewhere.owner_id != lease.owner_id
        elsewhere.release()
    finally:
        lease.release()


def test_the_lease_does_not_follow_the_environment_after_acquire(tmp_path, store,
                                                                 monkeypatch):
    """COLLIE_SESSIONS_DIR can change under a run; the lease must not move."""
    sessions.save("run-env", [{"role": "user", "content": "hello"}])
    lease = session_owner.acquire("run-env")
    try:
        assert lease.journal() == "ok"
        moved = tmp_path / "moved-sessions"
        moved.mkdir()
        monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(moved))
        # The new store has no journal at all. Reading "missing" here would be a
        # report about a different session that happens to share an id.
        assert sessions.load_checked("run-env")["status"] == "missing"
        assert lease.journal() == "ok"
        assert lease.assert_held("run-env") is lease
        with pytest.raises(session_owner.OwnershipRequired):
            lease.assert_held("run-env", directory=str(moved))
        with pytest.raises(session_owner.OwnershipRequired):
            lease.journal(directory=str(moved))
        # The lock file is still the one in the original store.
        assert os.path.realpath(lease.path).startswith(os.path.realpath(store))
    finally:
        lease.release()


def test_a_linked_runtime_directory_cannot_redirect_the_lock(tmp_path, store):
    """The final file name is not the only thing that can be a link.

    If <sessions>/runtime is itself a link to somewhere else, every lock path
    still "resolves correctly" relative to that directory while every lock lands
    outside the store — two stores would then share one lock file, or an
    attacker-writable directory would hold the file that decides who executes.
    """
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    kind = _link_dir(str(outside), os.path.join(store, session_owner.RUNTIME_SUBDIR))
    if not kind:
        pytest.skip("this host allows neither symlinks nor junctions")
    with pytest.raises(ValueError) as info:
        session_owner.try_acquire("run-escape")
    assert "outside" in str(info.value)
    assert os.listdir(outside) == [], "the escaped directory was written to"
    # And the same check protects a plain read.
    with pytest.raises(ValueError):
        session_owner.describe("run-escape")


def test_a_link_planted_at_the_lock_name_is_refused(tmp_path, store):
    """The other half of the same attack: our own directory, our exact name."""
    runtime = os.path.join(store, session_owner.RUNTIME_SUBDIR)
    os.makedirs(runtime, exist_ok=True)
    outside = tmp_path / "outside-lock"
    outside.mkdir()
    name = os.path.join(runtime, "run-namelink" + session_owner.OWNER_SUFFIX)
    if not _link_dir(str(outside), name):
        pytest.skip("this host allows neither symlinks nor junctions")
    with pytest.raises(ValueError):
        session_owner.try_acquire("run-namelink")
    assert os.listdir(outside) == []
    # A sibling in the same directory is unaffected: this is one name, not a
    # blanket refusal to use the store.
    lease = session_owner.acquire("run-namelink-ok")
    lease.release()


def test_a_normal_custom_directory_still_works(tmp_path):
    """The containment check must not break the supported `directory=` argument."""
    custom = tmp_path / "custom sessions"        # spaces and all
    custom.mkdir()
    lease = session_owner.acquire("run-custom", directory=str(custom))
    try:
        assert lease.held and lease.root == os.path.realpath(str(custom))
        assert os.path.exists(session_owner.lock_path("run-custom",
                                                      directory=str(custom)))
        assert session_owner.try_acquire("run-custom", directory=str(custom)) is None
    finally:
        lease.release()
    assert session_owner.describe("run-custom", directory=str(custom))["released"]


# ------------------------------------------------------------------ fork


def test_a_lease_can_be_released_from_another_thread(store):
    """Ownership is per process, not per thread: a run's threads share it."""
    lease = session_owner.acquire("run-thread-life")
    failures = []

    def finish():
        try:
            lease.assert_held("run-thread-life")
            lease.release()
        except Exception as exc:                 # pragma: no cover - failure path
            failures.append(exc)

    thread = threading.Thread(target=finish)
    thread.start()
    thread.join(30)
    assert not failures and not lease.held
    regained = session_owner.acquire("run-thread-life")
    regained.release()


def test_an_inherited_lease_never_unlocks_the_owning_process(store, monkeypatch):
    """The fork hazard, pinned where fork is not available.

    The test below proves a real child cannot drop its parent's lock; this one
    pins the mechanism that makes that true — an inherited lease must not issue
    LOCK_UN from any path, including the destructor — so a regression is caught
    on every platform rather than only on POSIX CI.
    """
    lease = session_owner.acquire("run-inherit", label="parent")
    unlocked = []
    monkeypatch.setattr(session_owner, "_unlock", lambda handle: unlocked.append(handle))
    monkeypatch.setattr(lease, "pid", os.getpid() + 1000)     # as if inherited by fork

    assert lease.held is False, "an inherited handle is not ownership"
    with pytest.raises(session_owner.OwnershipRequired) as info:
        lease.assert_held("run-inherit")
    assert "inherited" in str(info.value)
    lease.release()                       # explicit release
    lease.__exit__(None, None, None)      # `with` exit
    lease.__del__()                       # and the destructor
    assert unlocked == [], "an inherited lease unlocked the owner's file"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() is POSIX-only")
def test_a_forked_child_neither_owns_nor_can_release_the_parents_lease(tmp_path, store):
    """The one case where an inherited handle is worse than no handle at all.

    After fork the child holds a descriptor for the *same* open file description,
    so `flock(LOCK_UN)` there — from an explicit release, a `with` block, or the
    destructor during interpreter shutdown — unlocks the parent's live run and
    lets a second executor in.  The child must therefore refuse to act as owner
    and refuse to unlock, while the parent keeps running unaffected.
    """
    lease = session_owner.acquire("run-fork", label="parent")
    contender = _script(tmp_path, "contender.py", CONTENDER)
    try:
        pid = os.fork()
        if pid == 0:                             # ---- child
            code = 0
            try:
                if lease.held:
                    code = 11                    # inherited handle is not ownership
                else:
                    try:
                        lease.assert_held("run-fork")
                        code = 12                # must have raised
                    except session_owner.OwnershipRequired:
                        pass
                lease.release()                  # must not touch the parent's lock
                lease.__exit__(None, None, None)
                del lease
                gc.collect()                     # destructor path, same rule
            except BaseException:
                code = 13
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (
            "child mis-handled the inherited lease (code %s)" % status)
        # The parent's run is untouched, as seen from a wholly separate process.
        assert lease.held
        assert _run(contender, store, "run-fork").stdout.strip() == "BUSY"
    finally:
        lease.release()
    assert _run(contender, store, "run-fork").stdout.strip() == "WON"
