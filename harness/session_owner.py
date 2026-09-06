"""Exactly one executor per saved session, across threads and processes.

``webapp.Handler._run_begin`` is a class dictionary: it makes a second run
impossible *inside one server process* and says nothing at all about the CLI, a
second `collie web`, or a mission worker started from a terminal.  Two executors
on one session id is not a race that shows up as a lost message — it is two
providers appending to ``data/sessions/<id>.json`` through
``sessions._merge_messages``, interleaving two model conversations into one
transcript that neither of them would have produced.

The rule this module implements is deliberately narrow:

    Executing a session requires a lease.  Everything else — enqueueing a steer,
    reading the journal, listing runs — requires nothing.

That separation is why the lease is NOT ``sessions._locked``.  That primitive is
the right tool for a complete read/modify/write of one JSON file and the wrong
one for a twenty-minute run: holding it for the run's duration would block every
checkpoint, every inbox write and every sidebar read behind the agent's slowest
tool call.  A lease is a *sidecar* file that is locked for the whole run while
the journal stays free for short transactions.

Ownership is an OS byte-range lock (``msvcrt.locking`` / ``fcntl.flock``), the
same primitive ``supervisor.InstanceLock`` and ``oauth_owner.RefreshOwner``
already rely on, chosen for one property no bookkeeping scheme has: when the
holder dies — cleanly, killed, or blue-screened — the kernel drops the lock.
There is therefore no stale-timeout reclaim anywhere in this module, because a
timeout cannot distinguish "crashed" from "still thinking about a 90-second
test run", and guessing wrong is how you get two executors.

The lock path is stable and never unlinked.  Deleting and recreating a lock file
is the classic way to end up with two holders: the process that opened the old
inode still holds a lock on a file nobody else can see.  A leftover
``<id>.owner.lock`` after a session is deleted is harmless; removing it is not.

Not provided on purpose: blocking acquisition, queues, and heartbeats.  A second
surface that cannot get the lease should attach to the running session and
enqueue durable input (``task_inbox``) — never start a second executor, and
never wait on a lock while holding an HTTP request thread.

A lease is bound to three things, and all three are checked before it is accepted
as authority: the session id, the *resolved sessions root* it was acquired for,
and the process that took it.  A session id is only unique within a directory —
``COLLIE_SESSIONS_DIR`` can change inside one process, and a caller may pass
``directory=`` explicitly — so a lease over ``A/run-7`` must never be usable to
mutate ``B/run-7``.  And an OS file lock is inherited by ``fork``: a child that
"releases" an inherited lease would unlock the *parent's* run, so a lease is only
ever released by the process that acquired it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import platform
import threading
import time
import uuid

from . import sessions

RUNTIME_SUBDIR = "runtime"      # sibling of the *.json journals, invisible to sessions.recent()
OWNER_SUFFIX = ".owner.lock"
_IDENTITY_OFFSET = 1            # byte 0 is the locked byte; the record lives after it
_MAX_IDENTITY = 4096

# One lock object per lock path, so two threads in this process get a definite
# answer without depending on the OS lock's same-process semantics.  These are
# `threading.Lock` objects only: no lease, file handle or session state is
# reachable from module scope, so nothing here can keep a lease alive.
_GUARDS: dict = {}
_GUARDS_LOCK = threading.Lock()


class SessionBusy(RuntimeError):
    """Another executor holds this session's run lease.

    ``owner`` is the identity record read from the sidecar *after* our own
    acquisition failed, which is the only moment it is known to be current.  It
    may be empty if the holder crashed between taking the lock and describing
    itself; busy is still busy.
    """

    def __init__(self, session, owner=None):
        self.session = session
        self.owner = dict(owner or {})
        who = self.owner.get("label") or self.owner.get("owner") or "another process"
        pid = self.owner.get("pid")
        super().__init__("session %s is already being executed by %s%s"
                         % (session, who, " (pid %s)" % pid if pid else ""))


class OwnershipRequired(RuntimeError):
    """An operation that may only run under a live, matching run lease."""


def _guard(path):
    key = os.path.normcase(os.path.realpath(path))
    with _GUARDS_LOCK:
        guard = _GUARDS.get(key)
        if guard is None:
            guard = _GUARDS[key] = threading.Lock()
    return guard


def sessions_root(directory=None):
    """The sessions directory this call addresses, fully resolved.

    Everything else in this module and in ``task_inbox`` is expressed relative to
    this one string.  It is resolved (``realpath``) because it is the trust
    anchor: containment checks against an unresolved root can be satisfied by a
    path that resolves somewhere else entirely.  Resolving *here* also pins the
    answer — ``sessions._dir`` reads ``COLLIE_SESSIONS_DIR`` on every call, so a
    lease that kept only the id would follow the environment to another store.

    What this deliberately does not claim: that the configured root is itself
    trustworthy.  If ``COLLIE_SESSIONS_DIR`` points at a directory an attacker
    controls, they own the sessions, and no amount of checking below that point
    changes it.  The guarantee is narrower and real: *given* the root, nothing
    this module writes lands outside it.
    """
    return os.path.realpath(sessions._dir(directory))


def _key(path):
    """Compare paths the way the host filesystem does."""
    return os.path.normcase(os.path.abspath(path))


def _within(child, root):
    """True if the resolved ``child`` is ``root`` itself or lives under it.

    Prefix comparison on normalised paths, not ``commonpath``: that raises on
    two different Windows drives, which is a plain "no" here, not an error.
    """
    child_key, root_key = _key(child), _key(root)
    if child_key == root_key:
        return True
    if not root_key.endswith(os.sep):        # a drive or filesystem root already does
        root_key += os.sep
    return child_key.startswith(root_key)


def sidecar_dir(subdir, directory=None, *, root=None):
    """``<sessions>/<subdir>``, proven to still be inside the sessions root.

    Validating only the final file name is not enough.  If ``<sessions>/inbox``
    is itself a symlink (POSIX) or a directory junction (Windows, which needs no
    privilege at all) pointing outside the store, then every read and write is
    redirected while each individual file name still resolves "correctly" —
    ``realpath(<sessions>/inbox/x.json)`` and ``realpath(<sessions>/inbox)`` agree
    perfectly with each other, and both are somewhere else.  The check that
    catches it has to be against the resolved *root*, which is what this does.
    """
    root = root or sessions_root(directory)
    base = os.path.join(root, str(subdir))
    # Check before creating anything: makedirs through a dangling link would
    # materialise the attacker's directory for them.
    if os.path.lexists(base) and not _within(os.path.realpath(base), root):
        raise ValueError("sessions subdirectory %r resolves outside %s" % (str(subdir), root))
    os.makedirs(base, exist_ok=True)
    if not _within(os.path.realpath(base), root):
        raise ValueError("sessions subdirectory %r resolves outside %s" % (str(subdir), root))
    return base


def sidecar_path(session, subdir, suffix, directory=None, *, root=None):
    """Resolve ``<sessions>/<subdir>/<session><suffix>`` with sessions.py's own rules.

    ``sessions._path`` is the project's single answer to "is this id safe?" — it
    rejects traversal instead of normalising it (``../../victim`` must not become
    authority over ``victim``) and refuses an id whose real path leaves the
    directory.  Reusing it against the sidecar directory keeps one validator for
    the journal, the inbox and the lease, so a name that cannot address a journal
    cannot address our files either.  Raises ValueError rather than returning
    None: a caller here is about to execute or accept user input, and a silent
    skip is the wrong failure.

    Three separate things are checked, because each one alone is bypassable: the
    id (``sessions._path``), the sidecar directory (``sidecar_dir`` — it must
    still be inside the root), and the final file name (a link planted at exactly
    our name inside our own directory).
    """
    root = root or sessions_root(directory)
    base = sidecar_dir(subdir, root=root)
    probe = sessions._path(session, directory=base)
    if not probe:
        raise ValueError("invalid session id: %r" % (session,))
    path = probe[: -len(".json")] + suffix
    # Re-check the *final* name: the id is clean, but a symlink planted at
    # exactly this file name inside our own directory would still redirect the
    # write. os.path.realpath resolves it; the parent must be our directory.
    if _key(os.path.dirname(os.path.realpath(path))) != _key(os.path.realpath(base)):
        raise ValueError("session sidecar escapes its directory: %r" % (session,))
    return path


def lock_path(session, directory=None, *, root=None):
    """The stable lock file for one session's execution lease."""
    return sidecar_path(session, RUNTIME_SUBDIR, OWNER_SUFFIX, directory, root=root)


def _lock(handle):
    """Take the exclusive byte lock, or fail immediately. There is no wait path."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _same_file(fd, path):
    """Is the handle we just locked the file that this name refers to?

    ``lstat`` does not follow a symlink or a Windows reparse point; ``fstat``
    describes what we actually opened.  If they disagree, the name was a link (or
    was swapped between the path check and the open) and the lock we are about to
    take would be on a different file than the one we validated.  Filesystems
    that report no inode number cannot answer, and a non-answer is not evidence
    of an attack: the path checks above still stand.
    """
    try:
        opened, named = os.fstat(fd), os.lstat(path)
    except OSError:
        return True
    if not opened.st_ino or not named.st_ino:
        return True
    return (opened.st_ino, opened.st_dev) == (named.st_ino, named.st_dev)


def _open(path):
    """Open the lock file without ever truncating, recreating or following it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # O_NOFOLLOW (POSIX) refuses a symlink at the final component outright; on
    # Windows there is no such flag, so the inode/device comparison below is what
    # rejects a reparse point planted at our name.
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        # Linux reports ELOOP for O_NOFOLLOW on a symlink; the BSDs report EMLINK.
        if getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
            raise ValueError("lock path is a symbolic link: %s" % path) from None
        raise
    if not _same_file(fd, path):
        os.close(fd)
        raise ValueError("lock path is a link or was replaced while opening: %s" % path)
    handle = os.fdopen(fd, "r+b")
    try:
        # Windows locks a byte range; keep one real byte so the file a human
        # inspects is never zero-length. Beyond EOF locking works too, so a
        # failure here (another process already holds byte 0) is not fatal.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
    except OSError:
        pass
    return handle


def _read_identity(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(_IDENTITY_OFFSET)
            line = fh.read(_MAX_IDENTITY).split(b"\n", 1)[0]
        record = json.loads(line.decode("utf-8"))
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


class SessionOwner:
    """A held execution lease.  Release is idempotent and also happens on GC.

    The lease is authority over exactly one (session id, sessions root) pair, in
    exactly one process.  ``assert_held`` is the single gate for all three facts;
    every mutating ``task_inbox`` call goes through it.
    """

    def __init__(self, session, path, handle, guard, identity, root=None):
        self.session = session
        self.path = path
        self.identity = identity
        self.owner_id = identity["owner"]
        # The root this lease is authority over, resolved once at acquisition.
        # Not folded into `identity`, which is published to HTTP callers.
        self.root = root or os.path.dirname(os.path.dirname(path))
        self.pid = os.getpid()
        self._root_key = _key(self.root)
        self._handle = handle
        self._guard = guard

    # -- identity ---------------------------------------------------------
    @property
    def held(self):
        """Held *by this process*.

        A forked child inherits the object and the open file, but not ownership:
        the run belongs to the parent, and two processes acting on one lease is
        the exact outcome this module exists to prevent.
        """
        return self._handle is not None and self.pid == os.getpid()

    def info(self):
        """A JSON-safe description for /api/runs, logs and SessionBusy replies."""
        out = dict(self.identity)
        out["held"] = self.held
        return out

    def journal(self, directory=None):
        """"missing" / "ok" / "invalid" for the session's transcript, read now.

        Owning the id is not evidence that a conversation exists.  A run that is
        just starting is legitimately "missing"; a lease over an "invalid"
        journal is bookkeeping about a session that must be inspected, not
        executed.  Kept off the acquire path because validating a long transcript
        is far more work than taking a lock.

        Reads the root this lease was acquired for.  ``COLLIE_SESSIONS_DIR`` may
        have changed since — in a test, in a subcommand, in a thread that set it
        for another store — and reporting *that* directory's journal under this
        lease would describe a session the lease says nothing about.  Passing
        ``directory`` is allowed for call-site compatibility, but it must name
        the same root.
        """
        if directory is not None:
            self._assert_root(directory)
        return sessions.load_checked(self.session, self.root)["status"]

    def _assert_root(self, directory=None, root=None):
        """Reject a call aimed at a store this lease is not authority over."""
        if root is None:
            root = sessions_root(directory)
        if _key(root) != self._root_key:
            raise OwnershipRequired(
                "run lease for %s is bound to %s, not %s"
                % (self.session, self.root, root))
        return self

    def assert_held(self, session=None, *, directory=None, root=None):
        """Raise unless this is a live lease, in this process, for this session
        *in this sessions root*.

        ``root`` is an already-resolved ``sessions_root`` value (the internal
        fast path); ``directory`` is the raw argument a public API was called
        with and is resolved here.
        """
        if self.pid != os.getpid():
            raise OwnershipRequired(
                "run lease for %s belongs to pid %s; this is pid %s and inherited it"
                % (self.session, self.pid, os.getpid()))
        if not self.held:
            raise OwnershipRequired("run lease for %s has been released" % self.session)
        if session is not None and session != self.session:
            raise OwnershipRequired("run lease is for session %s, not %s"
                                    % (self.session, session))
        if directory is not None or root is not None:
            self._assert_root(directory, root)
        return self

    # -- lifecycle --------------------------------------------------------
    def _describe(self, handle, released=False):
        record = dict(self.identity)
        if released:
            record["released"] = time.time()
        try:
            blob = json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(blob) > _MAX_IDENTITY - 1:
                blob = json.dumps({"owner": self.owner_id, "pid": os.getpid()}).encode("utf-8")
            handle.seek(_IDENTITY_OFFSET)
            handle.write(blob + b"\n")
            handle.flush()
            with contextlib.suppress(OSError):
                handle.truncate()
        except (OSError, ValueError):
            # Describing ourselves is a courtesy to the next caller's error
            # message. The lock is what confers ownership, and we hold it.
            pass

    def release(self):
        if self.pid != os.getpid():
            # Inherited through fork.  On POSIX the child's descriptor refers to
            # the *same* open file description as the parent's, so LOCK_UN here
            # would drop the lock out from under a run that is still going — the
            # two-executor bug, caused by the cleanup meant to prevent it.  Let
            # go of the object only: dropping the last reference to a duplicated
            # descriptor does not release a flock while the parent still holds
            # one.  The guard is a private copy of the parent's, so releasing it
            # affects nothing outside this process.
            self._handle = None
            guard, self._guard = self._guard, None
            if guard is not None:
                with contextlib.suppress(RuntimeError):
                    guard.release()
            return self
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                self._describe(handle, released=True)   # while still locked; never after
            finally:
                try:
                    _unlock(handle)
                finally:
                    handle.close()
        guard, self._guard = self._guard, None
        if guard is not None:
            guard.release()
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()
        return False

    def __del__(self):
        # A dropped lease must not strand the session for the rest of the
        # process's life: the OS frees the file lock when the handle is
        # collected, and the in-process guard has to go with it.
        try:
            self.release()
        except Exception:
            pass

    def __repr__(self):
        return "<SessionOwner %s %s%s>" % (
            self.session, self.owner_id[:8], "" if self.held else " released")


def try_acquire(session, *, label="", meta=None, directory=None):
    """Take the execution lease, or return None if someone else already has it.

    Never blocks.  A journal for ``session`` need not exist yet: a run that is
    just starting owns its id before its first message is written.  The lease
    records ``journal_present`` and offers ``lease.journal()`` so a caller can
    tell a brand-new session from a torn one, and refuse to treat an "invalid"
    journal as executable work.

    The returned lease is authority over this session id *in the sessions root
    resolved now* (``lease.root``), and only in this process.  Later changes to
    ``COLLIE_SESSIONS_DIR`` do not move it, and a fork does not copy it.
    """
    root = sessions_root(directory)
    path = lock_path(session, root=root)
    guard = _guard(path)
    if not guard.acquire(blocking=False):
        return None                       # another thread here; same answer, cheaper
    handle = None
    try:
        handle = _open(path)
        try:
            _lock(handle)
        except (OSError, IOError):
            handle.close()
            handle = None
            guard.release()
            guard = None
            return None
        identity = {
            "owner": uuid.uuid4().hex,
            "session": session,
            "pid": os.getpid(),
            "host": platform.node()[:64],
            "label": str(label or "")[:120],
            "acquired": time.time(),
            "journal_present": os.path.exists(sessions._path(session, root) or ""),
        }
        if meta is not None:
            identity["meta"] = _small_json(meta)
        lease = SessionOwner(session, path, handle, guard, identity, root)
        lease._describe(handle)
        return lease
    except BaseException:
        if handle is not None:
            with contextlib.suppress(Exception):
                _unlock(handle)
            handle.close()
        if guard is not None:
            guard.release()
        raise


def acquire(session, *, label="", meta=None, directory=None):
    """try_acquire, but a taken lease is an explicit SessionBusy with identity."""
    lease = try_acquire(session, label=label, meta=meta, directory=directory)
    if lease is None:
        raise SessionBusy(session, _read_identity(lock_path(session, directory)))
    return lease


@contextlib.contextmanager
def own(session, *, label="", meta=None, directory=None):
    """`with own(sid, label="web") as lease:` — the normal way to run a session."""
    lease = acquire(session, label=label, meta=meta, directory=directory)
    try:
        yield lease
    finally:
        lease.release()


def describe(session, directory=None):
    """Advisory: what the sidecar says, without touching the lock.

    Deliberately does not answer "is it held?".  The only honest test is trying
    to acquire, and probing by locking would make a concurrent acquire fail for
    no reason.  ``released`` present means the last holder shut down cleanly;
    absent means it either still holds the lease or died with it.
    """
    path = lock_path(session, directory)
    record = _read_identity(path)
    return {"session": session, "present": os.path.exists(path), "owner": record,
            "released": record.get("released"), "advisory": True}


def probe_busy(session, directory=None):
    """Snapshot of OS ownership for display: True, False, or None if unreadable.

    Never grants execution authority or rewrites the identity record. A probe
    briefly holds the lock, so callers must still use try_acquire before work.
    Released metadata cannot prove idleness: a new owner may already hold the
    OS lock while its identity write is still pending.
    """
    try:
        path = lock_path(session, directory)
        if not os.path.exists(path):
            return False
        guard = _guard(path)
        if not guard.acquire(blocking=False):
            return True
    except (ValueError, OSError):
        return None
    handle = None
    try:
        handle = _open(path)
        try:
            _lock(handle)
        except (OSError, IOError):
            return True
        _unlock(handle)
        return False
    except (ValueError, OSError):
        return None
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()
        guard.release()


def _small_json(value, limit=1024):
    """Keep caller metadata to plain JSON values inside a hard byte budget."""
    try:
        blob = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("lease meta must be JSON values: %s" % exc) from None
    if len(blob.encode("utf-8")) > limit:
        raise ValueError("lease meta is larger than %d bytes" % limit)
    return json.loads(blob)
