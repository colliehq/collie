"""Undo snapshots work before a user configures Git's commit identity."""
import os
import subprocess

from harness import checkpoints


def test_checkpoint_without_git_identity_preserves_dirty_and_untracked_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for prefix in ("GIT_AUTHOR_", "GIT_COMMITTER_"):
        for key in (prefix + "NAME", prefix + "EMAIL"):
            monkeypatch.delenv(key, raising=False)

    def git(*args, check=True):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=check,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "-qm", "fixture")
    before_head = git("rev-parse", "HEAD")
    before_index = git("write-tree")
    (tmp_path / "tracked.txt").write_text("work in progress\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new work\n", encoding="utf-8")
    checkpoint = checkpoints.capture(str(tmp_path), "no-identity", 1)

    assert git("show", checkpoint.ref + ":tracked.txt") == "work in progress"
    assert git("show", checkpoint.ref + "^3:new.txt") == "new work"
    assert git("rev-parse", "HEAD") == before_head
    assert git("write-tree") == before_index
    assert git("config", "--get", "user.name", check=False) == ""
    assert git("config", "--get", "user.email", check=False) == ""
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "work in progress\n"
