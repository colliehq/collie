"""A checkpoint's list of untracked files must be read exactly, or not at all.

Restore runs `git clean -fd` whenever the snapshot carries its untracked-files parent, because an
empty list means "there were none". So a listing that could not be read must fail the checkpoint:
recorded as empty, it made restore delete files that existed when the snapshot was taken. It was
read as text in the locale's code page, and on Windows a name that did not decode (a UTF-8 name
under cp1252 or cp936) made stdout None with returncode 0.
"""
import os
import subprocess

import pytest

from harness import checkpoints


def _repo(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t",
                               "-c", "user.email=t@t", *args],
                              check=True, capture_output=True).stdout
    git("init", "-q")
    git("config", "core.autocrlf", "false")    # the machine's own setting would rewrite endings
    (tmp_path / "tracked.txt").write_bytes(b"base\n")
    git("add", "tracked.txt")
    git("commit", "-qm", "base")
    return git


def test_untracked_names_that_are_not_ascii_come_back(tmp_path):
    _repo(tmp_path)
    kept = tmp_path / "笔记-été.txt"
    kept.write_bytes("草稿 draft\n".encode("utf-8"))
    cp = checkpoints.capture(str(tmp_path), "names", 1)

    kept.unlink()
    (tmp_path / "later.txt").write_bytes(b"made after the checkpoint\n")
    done = checkpoints.restore(str(tmp_path), cp)

    assert done["untracked_rewound"] is True
    assert kept.read_bytes() == "草稿 draft\n".encode("utf-8")
    assert not (tmp_path / "later.txt").exists()


@pytest.mark.parametrize("shape", [
    {"returncode": 0, "stdout": None, "stderr": None},        # Windows, a name that did not decode
    {"returncode": 128, "stdout": b"", "stderr": b"fatal: detected dubious ownership"},
], ids=["stdout-none", "git-failed"])
def test_an_unreadable_listing_fails_the_checkpoint(tmp_path, monkeypatch, shape):
    _repo(tmp_path)
    (tmp_path / "keep-me.txt").write_bytes(b"precious\n")
    real_run = subprocess.run

    def run(argv, *a, **kw):
        if "ls-files" in argv and "--others" in argv:
            return subprocess.CompletedProcess(argv, **shape)
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(checkpoints.subprocess, "run", run)
    with pytest.raises(checkpoints.CheckpointError):
        checkpoints.capture(str(tmp_path), "unreadable", 1)
    monkeypatch.setattr(checkpoints.subprocess, "run", real_run)
    assert checkpoints.history(str(tmp_path), "unreadable") == []
    assert (tmp_path / "keep-me.txt").read_bytes() == b"precious\n"
