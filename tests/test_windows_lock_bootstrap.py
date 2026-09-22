"""Cold-start lock contenders must coordinate even while the lock file is still empty."""
import concurrent.futures
import os
from pathlib import Path
import threading

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows mandatory byte-range locks")


@pytest.fixture
def held_empty_lock(tmp_path):
    import msvcrt
    opened = []
    def hold(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b", buffering=0)
        assert os.fstat(handle.fileno()).st_size == 0
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        opened.append(handle)
        return handle
    yield hold
    for handle in opened:
        if not handle.closed:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()


@pytest.mark.parametrize("kind", ["sessions", "plan", "state"])
def test_waiting_writer_does_not_write_into_another_owners_lock(tmp_path, held_empty_lock, kind):
    import msvcrt
    import importlib
    path = str(tmp_path / "data.json")
    held = held_empty_lock(path + ".lock")
    module_name, function_name = {"sessions": ("sessions", "_locked"),
                                  "plan": ("plantool", "_locked"),
                                  "state": ("statelock", "transaction")}[kind]
    lock = getattr(importlib.import_module("harness." + module_name), function_name)
    entered = threading.Event()
    def writer():
        entered.set()
        with lock(path):
            Path(path).write_text("writer ran after release")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        try:
            assert entered.wait(3)
            with pytest.raises(concurrent.futures.TimeoutError):
                future.result(timeout=.2)
            assert not Path(path).exists()
        finally:
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
            held.close()
        future.result(timeout=5)
    assert Path(path).read_text() == "writer ran after release"


@pytest.mark.parametrize("kind", ["oauth", "session_owner", "supervisor", "slack"])
def test_nonblocking_owner_reports_busy_and_can_reacquire_after_release(tmp_path, held_empty_lock, monkeypatch, kind):
    import msvcrt
    if kind == "oauth":
        from harness import oauth_owner
        acquire = lambda: oauth_owner.RefreshOwner(str(tmp_path / "credential.json"), timeout=0).acquire()
        path = str(tmp_path / "credential.json.refresh.lock")
        error = oauth_owner.RefreshBusyError
    elif kind == "session_owner":
        from harness import session_owner
        path = session_owner.lock_path("bootstrap", directory=str(tmp_path))
        acquire = lambda: session_owner.try_acquire("bootstrap", directory=str(tmp_path))
        error = None
    elif kind == "supervisor":
        from harness import supervisor
        path = str(tmp_path / "supervisor.lock")
        acquire = lambda: supervisor.InstanceLock(path)
        error = RuntimeError
    else:
        from harness import slackbot
        monkeypatch.setattr(slackbot, "QUEUE_DIR", str(tmp_path))
        path = str(tmp_path / "slack-bootstrap.lock")
        acquire = lambda: slackbot.SlackInstanceLock("bootstrap")
        error = RuntimeError
    held = held_empty_lock(path)
    if error:
        with pytest.raises(error):
            acquire()
    else:
        assert acquire() is None
    msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
    held.close()
    owner = acquire()
    assert owner is not None
    if kind == "session_owner":
        owner.release()
    else:
        owner.close()
