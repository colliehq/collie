"""Gated in-process CLI executor used only by :mod:`harness.slackguard`.

On POSIX it is the task's process-group leader.  If its guard disappears, stdin
reaches EOF and this process kills its own group with SIGKILL, including children
that ignore TERM.  Running ``harness.cli.main`` in-process keeps that watcher
alive for the whole task rather than losing it across exec().
"""
from __future__ import annotations

import os
import signal
import sys
import threading


def _read_release() -> bool:
    data = b""
    while len(data) < 4 and not data.endswith(b"\n"):
        chunk = os.read(sys.stdin.fileno(), 1)
        if not chunk:
            break
        data += chunk
    return data in (b"go\n", b"go\r\n")


def main(argv=None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if command[:1] == ["--"]:
        command = command[1:]
    if len(command) < 3:
        print("slackexec: missing Python command", file=sys.stderr)
        return 2

    if not _read_release():
        return 75

    # fd 0 used to remain the guard's lifetime pipe.  That made an otherwise
    # ordinary tool which reads stdin wait forever instead of seeing the EOF it
    # saw before guarded execution.  POSIX still needs to watch that pipe, so
    # duplicate it privately before restoring the task's stdin to DEVNULL.  The
    # private descriptor is non-inheritable: tools must not be able to keep the
    # guard relationship alive accidentally.
    life_fd = None
    if os.name != "nt":
        life_fd = os.dup(sys.stdin.fileno())
        os.set_inheritable(life_fd, False)

    null_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null_fd, sys.stdin.fileno())
    finally:
        if null_fd != sys.stdin.fileno():
            os.close(null_fd)

    if os.name != "nt":
        def watch_guard():
            try:
                os.read(life_fd, 1)
            finally:
                try:
                    os.close(life_fd)
                except OSError:
                    pass
            # This executor is a session/process-group leader. SIGKILL is
            # synchronous at the kernel boundary and cannot be ignored.
            try:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            except OSError:
                os._exit(137)

        threading.Thread(target=watch_guard, daemon=True).start()

    # Repository-owned child launchers normally create their own POSIX session
    # so an individual tool timeout can kill that tool tree.  Inside this
    # executor the outer process group is the stronger ownership boundary: if a
    # shell starts a second session, listener death would no longer reach it.
    # plat.new_group_kwargs() consumes this marker and keeps every managed child
    # in this executor's group.  Windows uses a Job Object and needs no marker.
    os.environ["COLLIE_PROCESS_OWNER"] = "slackexec"

    if command[1:3] == ["-m", "harness.cli"]:
        from . import cli
        return int(cli.main(command[3:]) or 0)
    if command[1] == "-c":                  # narrow fixture path; still the same Python process
        sys.argv = ["-c"] + command[3:]
        exec(compile(command[2], "<slackexec>", "exec"), {"__name__": "__main__"})
        return 0
    print("slackexec: expected python -m harness.cli ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
