"""Re-entrant, cross-process transactions for one local JSON state file.

A ``threading.RLock`` only orders writers inside a single interpreter.  Collie routinely runs
more than one process against the same state directory (web server, CLI, native shell, a second
worker), and each of them does read-modify-write on the same file.  ``os.replace`` guarantees a
reader never sees half a file; it does not stop the second writer from replacing the first
writer's update with a value it read before that update existed.  Lost events, resurrected
sessions, and revoked permissions coming back are all the same bug.

``transaction(path)`` closes that hole with the idiom already used by ``sessions._locked`` and
``oauth_owner``: a process-local ``RLock`` plus an OS-released exclusive lock on a sibling
``.lock`` file.  Two details matter and are why this lives in one place:

* Re-entrancy.  Nested calls on one thread must not try to lock the same byte again on a second
  handle — Windows refuses that outright and POSIX ``flock`` on a second description deadlocks.
  The OS lock is taken once, at the outermost nesting level.
* One deadline for both locks.  ``timeout`` is a promise to the caller, and every caller here
  turns a timeout into an answer a person sees ("this bundle is in use", HTTP 409) rather than
  into a wait.  A waiting *thread* is exactly as much of a second writer as a waiting *process*,
  so it gets the same answer: the ``RLock`` is taken with the caller's own deadline, not
  unconditionally.  Waiting on it forever while the OS lock had a deadline made one operation
  behave two different ways depending on where the other writer happened to live — and in the
  threaded web server, where both writers are usually local, it wedged the request thread.
  Re-entry on the owning thread still costs nothing and can never time out.
* Crash release.  The kernel drops the lock when a holder exits, so a killed process cannot
  strand the file.  ``claim``/``unclaim`` expose the same property as a liveness probe: if you
  can take another owner's claim file, that owner is gone.  This is trustworthy process identity,
  not a timeout guess.
* Process binding.  Every piece of bookkeeping here — nesting depth, the ``RLock``, the open
  handle, a claim — records the pid that created it.  ``os.fork`` copies all of that into a
  child that holds none of it; a child that trusted the inherited depth would skip locking and
  write straight through the parent's transaction.  Inherited state is therefore discarded, and
  a child never issues ``LOCK_UN`` on a descriptor it inherited: on POSIX that descriptor shares
  the parent's open file description, so unlocking it would release the *parent's* lock.  Closing
  it does not.

Hold a transaction only for short local work.  Never across a model call, speech transcription,
subprocess, or any other network operation.
"""
from __future__ import annotations

import contextlib
import os
import random
import threading
import time


DEFAULT_TIMEOUT = 30.0


class StateLockTimeout(TimeoutError):
    """Another holder kept the state lock past this caller's deadline."""


def canonical(path) -> str:
    """Return a stable key for ``path`` that does not require the file to exist yet."""
    text = str(path)
    directory = os.path.dirname(os.path.abspath(text))
    return os.path.join(os.path.realpath(directory), os.path.basename(text))


class _PathLock:
    __slots__ = ("path", "local", "depth", "handle", "pid")

    def __init__(self, path: str, pid: int):
        self.path = path
        self.local = threading.RLock()
        self.depth = 0
        self.handle = None
        self.pid = pid


_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _abandon(lock: _PathLock) -> None:
    """Drop bookkeeping this process inherited instead of acquired.

    The handle is closed but deliberately not unlocked: a forked child shares the parent's open
    file description, so ``LOCK_UN``/``LK_UNLCK`` here would hand the parent's exclusive lock to
    an unrelated writer while the parent is still inside its transaction.
    """
    handle, lock.handle = lock.handle, None
    lock.depth = 0
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def _reset_after_fork() -> None:
    """Forget every lock the parent owned; this child holds none of them."""
    global _LOCKS_GUARD
    # The guard itself may have been copied while another (now nonexistent) thread held it.
    _LOCKS_GUARD = threading.Lock()
    stale = list(_LOCKS.values())
    _LOCKS.clear()
    for lock in stale:
        _abandon(lock)


if hasattr(os, "register_at_fork"):  # POSIX only; Windows has no fork
    os.register_at_fork(after_in_child=_reset_after_fork)


def _lock_for(path) -> _PathLock:
    key = canonical(path)
    pid = os.getpid()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is not None and lock.pid != pid:
            # Inherited across a fork on a platform without ``register_at_fork``, or after any
            # other pid change: the depth, the RLock and the OS handle all belong to the parent.
            _abandon(lock)
            lock = None
        if lock is None:
            lock = _LOCKS[key] = _PathLock(key + ".lock", pid)
        return lock


def _open(path: str):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Windows can lock beyond EOF. Initializing byte 0 here would write into
    # another process's lock during simultaneous first opens.
    return open(path, "a+b")


def _try_lock(handle) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def _acquire(path: str, timeout: float):
    """Block for the exclusive OS lock, reporting a timeout instead of a platform error.

    ``msvcrt.locking(LK_LOCK, …)`` gives up after ten one-second retries and raises a bare
    ``OSError``; polling a non-blocking lock keeps the deadline and the message ours.
    """
    handle = _open(path)
    deadline = time.monotonic() + max(0.0, float(timeout))
    delay = 0.0005
    while True:
        if _try_lock(handle):
            return handle
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            handle.close()
            raise StateLockTimeout("state lock is still held by another writer: %s" % path)
        # Jitter keeps several waiting processes from retrying in lockstep.
        time.sleep(min(remaining, delay * (0.5 + random.random())))
        delay = min(0.02, delay * 2)


def _acquire_local(lock: _PathLock, deadline: float) -> None:
    """Take the process-local ``RLock`` within the caller's own deadline.

    Re-entry on the owning thread returns immediately whichever call is used, so a nested
    transaction never spends budget here and never raises.  Anyone else pays the same deadline
    the OS lock charges a competing process: a caller that asked for ten seconds must not be
    parked for ten minutes because the other writer turned out to be a sibling thread.
    """
    remaining = deadline - time.monotonic()
    if remaining > 0:
        # ``Lock.acquire`` rejects a timeout above TIMEOUT_MAX outright, and a caller passing a
        # very large budget means "wait", not "raise OverflowError instead of waiting".
        taken = lock.local.acquire(timeout=min(remaining, threading.TIMEOUT_MAX))
    else:
        # An exhausted budget still owes the owning thread its re-entry, and owes everyone
        # else one honest attempt; ``acquire(timeout=0)`` and blocking=False agree on both.
        taken = lock.local.acquire(blocking=False)
    if not taken:
        raise StateLockTimeout("state lock is still held by another writer: %s" % lock.path)


@contextlib.contextmanager
def transaction(path, *, timeout: float = DEFAULT_TIMEOUT):
    """Serialize a whole read-modify-write of ``path`` across threads and processes.

    ``timeout`` is the total budget for becoming the writer, spent across the process-local
    lock and then the OS lock, and ``StateLockTimeout`` is raised rather than waiting past it.
    """
    lock = _lock_for(path)
    holder = os.getpid()
    deadline = time.monotonic() + max(0.0, float(timeout))
    _acquire_local(lock, deadline)
    try:
        outermost = lock.depth == 0
        if outermost:
            lock.handle = _acquire(lock.path, deadline - time.monotonic())
        lock.depth += 1
        try:
            yield
        finally:
            # A different pid here means ``os.fork`` ran inside the block and this is the child:
            # it acquired nothing, the at-fork reset has already replaced its bookkeeping, and
            # unlocking would free a lock the parent is still inside.  Leave it all alone.
            if os.getpid() == holder:
                lock.depth -= 1
                if outermost:
                    handle, lock.handle = lock.handle, None
                    if handle is not None:
                        _unlock(handle)
    finally:
        # Unconditional, exactly as the previous ``with lock.local`` was: after a fork inside
        # the block this object is already off ``_LOCKS``, and the child's copy of the RLock is
        # its own to drop.
        lock.local.release()


class Claim:
    """An exclusive, crash-released claim on one file, bound to the process that took it."""

    __slots__ = ("path", "handle", "pid")

    def __init__(self, path: str, handle):
        self.path = path
        self.handle = handle
        self.pid = os.getpid()

    @property
    def mine(self) -> bool:
        return self.pid == os.getpid()


def claim(path):
    """Take an exclusive, crash-released claim on ``path``; return a ``Claim`` or None if busy."""
    handle = _open(str(path))
    if _try_lock(handle):
        return Claim(str(path), handle)
    handle.close()
    return None


def unclaim(held) -> None:
    """Release a claim this process took.  An inherited claim is closed, never unlocked."""
    if held is None:
        return
    handle, held.handle = held.handle, None
    if handle is None:
        return
    if not held.mine:
        try:
            handle.close()
        except OSError:
            pass
        return
    _unlock(handle)
