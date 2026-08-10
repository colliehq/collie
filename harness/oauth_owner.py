"""Cross-process ownership for OAuth refresh writers.

OAuth refresh tokens are commonly rotated.  Two Collie workers refreshing the same
credential file at once can therefore invalidate the winner with the loser's stale
refresh token.  ``RefreshOwner`` is a small OS-released file lock: a crashed owner
cannot strand it and callers must re-read the credential *after* acquiring it.

Claude Code remains the sole writer of its own credential store.  Collie only uses
this lock for stores it is explicitly responsible for updating (currently Codex).
"""

from __future__ import annotations

import os
import time


class RefreshBusyError(TimeoutError):
    """Another process retained refresh ownership past the caller's deadline."""


class RefreshOwner:
    """Exclusive, crash-released refresh ownership for one credential store."""

    def __init__(self, credential_path: str, *, timeout: float = 35.0,
                 poll_interval: float = 0.05, clock=time.monotonic, sleep=time.sleep):
        self.path = os.path.abspath(credential_path) + ".refresh.lock"
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self._clock = clock
        self._sleep = sleep
        self._file = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = self._clock() + self.timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._file = handle
                return self
            except (OSError, IOError):
                if self._clock() >= deadline:
                    handle.close()
                    raise RefreshBusyError(
                        "OAuth refresh owner did not release %s" % self.path)
                self._sleep(min(self.poll_interval, max(0.0, deadline - self._clock())))

    def close(self):
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()
