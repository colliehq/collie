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


def test_file_names_are_not_patterns(tmp_path):
    # As pathspecs, an untracked "secret[1].env" also matched an IGNORED "secret1.env", and
    # `add --force` committed that ignored file into the checkpoint ref.
    git = _repo(tmp_path)
    (tmp_path / ".gitignore").write_bytes(b"secret1.env\n")
    git("add", ".gitignore")
    git("commit", "-qm", "ignore")
    (tmp_path / "secret1.env").write_bytes(b"TOKEN=do-not-snapshot\n")
    (tmp_path / "secret[1].env").write_bytes(b"not a secret\n")
    cp = checkpoints.capture(str(tmp_path), "globs", 1)
    names = git("ls-tree", "--name-only", cp.ref + "^3").decode("utf-8").split()
    assert names == ["secret[1].env"], names


def test_a_chinese_code_page_does_not_empty_the_list(tmp_path, monkeypatch):
    # The shipped failure end to end: under cp936 one Chinese file name made the text-mode
    # listing unreadable, it was recorded as "none", and restore deleted the files.
    import locale
    monkeypatch.setattr(locale, "getencoding", lambda: "cp936")
    _repo(tmp_path)
    kept = tmp_path / "会议纪要-0924.txt"
    other = tmp_path / "plain.txt"
    kept.write_bytes(b"notes\n")
    other.write_bytes(b"plain\n")
    cp = checkpoints.capture(str(tmp_path), "cp936", 1)
    kept.unlink()
    other.unlink()
    checkpoints.restore(str(tmp_path), cp)
    assert kept.read_bytes() == b"notes\n" and other.read_bytes() == b"plain\n"
