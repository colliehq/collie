"""Pack's winning edits survive the run, and applying them cannot eat somebody's work.

These tests use the real filesystem and the real store: a bundle is created from an isolated
attempt directory, saved, read back in a FRESH process state, and applied to a live workspace that
has moved on in the meantime.  They assert the promises a user is given ("your winner is saved",
"an unrelated edit of mine is untouched", "a conflicting edit is refused, not overwritten"), not
the shape of the helpers that keep them.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import pack, pack_artifacts as pa


# --------------------------------------------------------------------------- helpers

@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A private artifact store; never the developer's ~/.collie."""
    root = tmp_path / "state" / "pack_artifacts"
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_DIR", str(root))
    monkeypatch.delenv("COLLIE_STATE_DIR", raising=False)
    return root


def _write(path, text, *, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable and os.name != "nt":
        path.chmod(path.stat().st_mode | 0o111)


def _isolate(workspace, tmp_path, name="attempt"):
    """The same copy Pack makes: excluded trees left behind, links preserved."""
    attempt = tmp_path / name
    shutil.copytree(str(workspace), str(attempt), symlinks=True,
                    ignore=lambda _d, names: [n for n in names if n in pa.SKIP_DIRS])
    return attempt


def _bundle(workspace, attempt, baseline, **meta):
    return pa.create_artifact(str(attempt), baseline, workspace=str(workspace), metadata=meta)


# --------------------------------------------------------------------------- create / save

def test_winner_bundle_stores_only_the_changes_and_survives_the_attempt_tree(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "keep.txt", "untouched" * 100)
    _write(workspace / "edit.txt", "before")
    _write(workspace / "gone.txt", "delete me")
    _write(workspace / ".git" / "config", "[core]")
    _write(workspace / "node_modules" / "dep" / "index.js", "module")

    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))

    (attempt / "edit.txt").write_text("after", encoding="utf-8")
    (attempt / "gone.txt").unlink()
    (attempt / "sub").mkdir()
    (attempt / "sub" / "new.bin").write_bytes(bytes(range(256)))

    bundle = _bundle(workspace, attempt, baseline, task="do it")
    assert bundle.summary["added"] == 1 and bundle.summary["modified"] == 1
    assert bundle.summary["deleted"] == 1 and bundle.summary["files"] == 3
    assert {c["path"] for c in bundle.changes} == {"edit.txt", "gone.txt", "sub/new.bin"}

    record = pa.save_artifact(bundle)
    shutil.rmtree(str(attempt))                     # Pack deletes the candidate right after this

    stored = [p for p in (store / record["id"] / "blobs").iterdir()]
    assert len(stored) == 2, "only added/replaced contents are stored, never the whole tree"
    # keep.txt is 900 bytes and unchanged: it must not have been duplicated into the bundle.
    assert sum(p.stat().st_size for p in stored) < 900

    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["applied"] and result["code"] == ""
    assert (workspace / "edit.txt").read_text(encoding="utf-8") == "after"
    assert not (workspace / "gone.txt").exists()
    assert (workspace / "sub" / "new.bin").read_bytes() == bytes(range(256))
    assert (workspace / ".git" / "config").exists()
    assert (workspace / "node_modules" / "dep" / "index.js").exists()


def test_unchanged_winner_is_not_persisted_as_an_empty_bundle(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "same")
    attempt = _isolate(workspace, tmp_path)
    bundle = _bundle(workspace, attempt, pa.capture_baseline(str(attempt)))
    assert bundle.empty and bundle.summary["files"] == 0
    with pytest.raises(pa.PackArtifactError):
        pa.save_artifact(bundle)
    assert not store.exists() or not list(store.glob("2*")), "no artifact directory was created"


def test_quota_refuses_an_oversized_winner_with_an_actionable_message(store, tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_MAX_FILES", "1")
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "a")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    _write(attempt / "b.txt", "b")
    _write(attempt / "c.txt", "c")
    with pytest.raises(pa.ArtifactLimitError) as excinfo:
        _bundle(workspace, attempt, baseline)
    assert "COLLIE_PACK_ARTIFACT_MAX_FILES" in str(excinfo.value)


def test_store_failure_is_reported_and_leaves_no_half_artifact(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "one")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("two", encoding="utf-8")
    bundle = _bundle(workspace, attempt, baseline)

    def boom(*_a, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pa, "_copy_blob", boom)
    with pytest.raises(pa.ArtifactStorageError) as excinfo:
        pa.save_artifact(bundle)
    assert "a.txt" in str(excinfo.value) and "No space left" in str(excinfo.value)
    assert not list(store.glob("2*")), "a failed save leaves no artifact claiming to hold content"


# --------------------------------------------------------------------------- retention

def _save_edit(workspace, tmp_path, name, path, text):
    """One more saved winner for ``workspace``, editing ``path`` to ``text``."""
    attempt = _isolate(workspace, tmp_path, name=name)
    baseline = pa.capture_baseline(str(attempt))
    _write(attempt / path, text)
    try:
        return pa.save_artifact(_bundle(workspace, attempt, baseline))
    finally:
        shutil.rmtree(str(attempt), ignore_errors=True)


def test_unapplied_bundles_are_never_pruned_to_make_room(store, tmp_path, monkeypatch):
    """Two winners, one slot.  Each holds DIFFERENT edits, so neither may be dropped for the
    other."""
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_KEEP", "1")
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "base")
    _write(workspace / "b.txt", "base")

    first = _save_edit(workspace, tmp_path, "attempt0", "a.txt", "winner one")
    with pytest.raises(pa.ArtifactQuotaError) as excinfo:
        _save_edit(workspace, tmp_path, "attempt1", "b.txt", "winner two")

    message = str(excinfo.value)
    assert first["id"] in message and "never applied" in message
    assert "COLLIE_PACK_ARTIFACT_KEEP" in message and "NOT saved" in message
    # The older winner is still there, still complete, still appliable.
    assert [row["id"] for row in pa.list_artifacts(workspace=str(workspace))] == [first["id"]]
    assert pa.apply_artifact(first["id"], str(workspace))["applied"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "winner one"


def test_a_delivered_bundle_is_pruned_but_a_reverted_one_is_not(store, tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_KEEP", "1")
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "base")

    first = _save_edit(workspace, tmp_path, "attempt0", "a.txt", "one")
    assert pa.apply_artifact(first["id"], str(workspace))["applied"]
    assert pa.delivery_state(pa.inspect_artifact(first["id"])) == "delivered"

    # Delivered: its edits live in the workspace, so the slot may be reused.
    second = _save_edit(workspace, tmp_path, "attempt1", "a.txt", "two")
    assert second["pruned"] == [first["id"]]
    assert [row["id"] for row in pa.list_artifacts(workspace=str(workspace))] == [second["id"]]

    # Now apply the second and then REVERT the workspace by hand. The applied journal line is
    # still there, but the work is not: an old journal entry must not license deletion.
    assert pa.apply_artifact(second["id"], str(workspace))["applied"]
    (workspace / "a.txt").write_text("one", encoding="utf-8")     # the user undid it by hand
    assert pa.delivery_state(pa.inspect_artifact(second["id"])) == "diverged"
    with pytest.raises(pa.ArtifactQuotaError) as excinfo:
        _save_edit(workspace, tmp_path, "attempt2", "a.txt", "three")
    assert "no longer matches" in str(excinfo.value)
    assert pa.apply_artifact(second["id"], str(workspace))["applied"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "two", "still recoverable"


def test_a_bundle_whose_workspace_vanished_is_protected_not_pruned(store, tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_KEEP", "1")
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "base")
    first = _save_edit(workspace, tmp_path, "attempt0", "a.txt", "one")
    assert pa.apply_artifact(first["id"], str(workspace))["applied"]

    moved = tmp_path / "moved"
    os.rename(str(workspace), str(moved))
    workspace.mkdir()
    _write(workspace / "a.txt", "base")
    with pytest.raises(pa.ArtifactQuotaError) as excinfo:
        _save_edit(workspace, tmp_path, "attempt1", "a.txt", "two")
    assert "cannot be read" in str(excinfo.value) or "no longer matches" in str(excinfo.value)
    assert pa.inspect_artifact(first["id"])["id"] == first["id"]


def test_the_byte_quota_refuses_instead_of_dropping_unapplied_work(store, tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_STORE_MB", "1")
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "base")
    _write(workspace / "b.txt", "base")

    first = _save_edit(workspace, tmp_path, "attempt0", "a.txt", "x" * 700_000)
    with pytest.raises(pa.ArtifactQuotaError) as excinfo:
        _save_edit(workspace, tmp_path, "attempt1", "b.txt", "y" * 700_000)
    assert "COLLIE_PACK_ARTIFACT_STORE_MB" in str(excinfo.value)
    assert len(pa.inspect_artifact(first["id"])["changes"]) == 1
    assert pa.apply_artifact(first["id"], str(workspace))["applied"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "x" * 700_000


def test_another_workspaces_bundles_do_not_consume_this_workspaces_quota(store, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_KEEP", "1")
    one, two = tmp_path / "one", tmp_path / "two"
    _write(one / "a.txt", "base")
    _write(two / "a.txt", "base")
    kept = _save_edit(one, tmp_path, "attempt0", "a.txt", "one")
    other = _save_edit(two, tmp_path, "attempt1", "a.txt", "two")
    assert other["pruned"] == []
    ids = {row["id"] for row in pa.list_artifacts()}
    assert ids == {kept["id"], other["id"]}


def test_a_bundle_an_apply_is_using_is_not_deleted_underneath_it(store, tmp_path):
    """Real cross-process coordination: while apply holds the bundle, nothing may remove it."""
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "base")
    record = _save_edit(workspace, tmp_path, "attempt0", "a.txt", "winner")
    ready, release = tmp_path / "ready", tmp_path / "release"

    script = (
        "import os, sys, time;"
        "sys.path.insert(0, %r);"
        "from harness import pack_artifacts as pa, statelock;"
        "lock = pa._artifact_lock(%r, %r);"
        "handle = statelock.transaction(lock, timeout=30);"
        "handle.__enter__();"
        "open(%r, 'w').close();"
        "[time.sleep(0.05) for _ in range(200) if not os.path.exists(%r)];"
        "handle.__exit__(None, None, None)"
        % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
           str(store), record["id"], str(ready), str(release)))
    holder = subprocess.Popen([sys.executable, "-c", script],
                              env=dict(os.environ, COLLIE_PACK_ARTIFACT_DIR=str(store)))
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "the holder process never took the lock"

        assert not pa.delete_artifact(record["id"], timeout=0.5), "refused while in use"
        assert (store / record["id"] / "artifact.json").exists()
        # A concurrent save may not prune it either, even though it is only a quota away.
        os.environ["COLLIE_PACK_ARTIFACT_KEEP"] = "1"
        try:
            with pytest.raises(pa.ArtifactQuotaError) as excinfo:
                _save_edit(workspace, tmp_path, "attempt1", "b.txt", "second")
        finally:
            os.environ.pop("COLLIE_PACK_ARTIFACT_KEEP", None)
        assert "being applied right now" in str(excinfo.value)
    finally:
        release.write_text("go", encoding="utf-8")
        holder.wait(timeout=60)

    # Once the holder is gone the bundle is intact and still works.
    assert pa.apply_artifact(record["id"], str(workspace))["applied"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "winner"


# --------------------------------------------------------------------------- conflicts

def test_unrelated_live_edits_survive_an_apply(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "target.txt", "before")
    _write(workspace / "other.txt", "mine")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "target.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    # While the pack ran the user kept working on files the winner never touched.
    (workspace / "other.txt").write_text("mine, edited", encoding="utf-8")
    _write(workspace / "brand_new.txt", "written during the run")

    assert pa.apply_artifact(record["id"], str(workspace))["applied"]
    assert (workspace / "target.txt").read_text(encoding="utf-8") == "after"
    assert (workspace / "other.txt").read_text(encoding="utf-8") == "mine, edited"
    assert (workspace / "brand_new.txt").exists()


def test_a_touched_file_edited_during_the_run_is_refused_with_no_partial_edit(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "one.txt", "before")
    _write(workspace / "two.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "one.txt").write_text("after", encoding="utf-8")
    (attempt / "two.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    (workspace / "two.txt").write_text("the user got there first", encoding="utf-8")
    result = pa.apply_artifact(record["id"], str(workspace))

    assert not result["applied"] and result["code"] == "conflict"
    assert [c["path"] for c in result["conflicts"]] == ["two.txt"]
    assert "changed since the pack attempt started" in result["conflicts"][0]["reason"]
    # NOTHING was written: the non-conflicting file is untouched too.
    assert (workspace / "one.txt").read_text(encoding="utf-8") == "before"
    assert (workspace / "two.txt").read_text(encoding="utf-8") == "the user got there first"
    # And the bundle is still there to review or apply after the conflict is resolved.
    assert pa.inspect_artifact(record["id"])["id"] == record["id"]


def test_a_deleted_target_is_a_conflict_not_a_resurrection(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "doomed.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "doomed.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    (workspace / "doomed.txt").unlink()
    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["code"] == "conflict" and not (workspace / "doomed.txt").exists()


def test_reapplying_is_a_no_op_and_never_reverts_newer_edits(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    _write(workspace / "b.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    (attempt / "b.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    assert pa.apply_artifact(record["id"], str(workspace))["applied"]
    second = pa.apply_artifact(record["id"], str(workspace))
    assert second["applied"] and second["changed"] == []
    assert sorted(second["already_applied"]) == ["a.txt", "b.txt"]

    # Now the user edits an applied file further. Applying again must refuse, not roll them back.
    (workspace / "a.txt").write_text("after, then improved", encoding="utf-8")
    third = pa.apply_artifact(record["id"], str(workspace))
    assert third["code"] == "conflict"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "after, then improved"


def test_dry_run_reports_the_plan_without_touching_the_workspace(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    result = pa.apply_artifact(record["id"], str(workspace), dry_run=True)
    assert result["ok"] and not result["applied"]
    assert [c["path"] for c in result["changed"]] == ["a.txt"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"


def _snapshot(directory):
    """Every file under ``directory`` with its bytes and mtime — a zero-mutation witness."""
    rows = {}
    for root, _dirs, files in os.walk(str(directory)):
        for name in files:
            path = os.path.join(root, name)
            rows[os.path.relpath(path, str(directory))] = (
                open(path, "rb").read(), os.stat(path).st_mtime_ns)
    return rows


def test_a_dry_run_conflict_writes_nothing_at_all(store, tmp_path):
    """A preview that journals is not a preview: the artifact was mutated by looking at it."""
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))
    (workspace / "a.txt").write_text("the user got there first", encoding="utf-8")

    before = _snapshot(store / record["id"])
    result = pa.apply_artifact(record["id"], str(workspace), dry_run=True)

    assert result["code"] == "conflict" and not result["applied"]
    assert [c["path"] for c in result["conflicts"]] == ["a.txt"]
    assert _snapshot(store / record["id"]) == before, "the bundle's bytes are unchanged"
    assert sorted(os.listdir(str(store / record["id"]))) == ["artifact.json", "blobs"]
    assert not (store / record["id"] / "backups").exists()
    assert pa.inspect_artifact(record["id"])["applies"] == [], "no journal line for a preview"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "the user got there first"

    # The same conflict through a real apply DOES record the attempt.
    assert pa.apply_artifact(record["id"], str(workspace))["code"] == "conflict"
    assert [row["status"] for row in pa.inspect_artifact(record["id"])["applies"]] == ["conflict"]


def test_a_successful_dry_run_leaves_the_bundle_and_workspace_untouched(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    _write(attempt / "new.txt", "added")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    before = _snapshot(store / record["id"])
    result = pa.apply_artifact(record["id"], str(workspace), dry_run=True)
    assert result["ok"] and not result["applied"]
    assert sorted(c["path"] for c in result["changed"]) == ["a.txt", "new.txt"]
    assert _snapshot(store / record["id"]) == before
    assert pa.inspect_artifact(record["id"])["applies"] == []
    assert not (workspace / "new.txt").exists()
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"


def test_a_bundle_is_bound_to_the_workspace_it_came_from(store, tmp_path):
    workspace = tmp_path / "repo"
    other = tmp_path / "elsewhere"
    other.mkdir()
    _write(workspace / "a.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    result = pa.apply_artifact(record["id"], str(other))
    assert result["code"] == "foreign_root" and str(workspace) in result["error"]
    assert not list(other.iterdir()), "nothing was written into the wrong root"


# --------------------------------------------------------------------------- type changes

def test_file_becomes_a_directory_and_back_without_deleting_unexpected_work(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "thing", "i am a file")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "thing").unlink()
    _write(attempt / "thing" / "inner.txt", "i am a directory now")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    assert pa.apply_artifact(record["id"], str(workspace))["applied"]
    assert (workspace / "thing" / "inner.txt").read_text(encoding="utf-8") == "i am a directory now"

    # The reverse: directory -> file, but the user dropped an extra file inside meanwhile.
    attempt2 = _isolate(workspace, tmp_path, name="attempt2")
    baseline2 = pa.capture_baseline(str(attempt2))
    shutil.rmtree(str(attempt2 / "thing"))
    _write(attempt2 / "thing", "a file again")
    record2 = pa.save_artifact(_bundle(workspace, attempt2, baseline2))

    _write(workspace / "thing" / "mine.txt", "do not delete me")
    blocked = pa.apply_artifact(record2["id"], str(workspace))
    assert blocked["code"] == "conflict"
    assert (workspace / "thing" / "mine.txt").exists(), "no recursive delete of unexpected files"

    (workspace / "thing" / "mine.txt").unlink()
    assert pa.apply_artifact(record2["id"], str(workspace))["applied"]
    assert (workspace / "thing").read_text(encoding="utf-8") == "a file again"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_executable_bit_is_part_of_the_bundle(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "run.sh", "#!/bin/sh\necho old\n")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    _write(attempt / "run.sh", "#!/bin/sh\necho new\n", executable=True)
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))
    assert pa.apply_artifact(record["id"], str(workspace))["applied"]
    assert os.stat(workspace / "run.sh").st_mode & stat.S_IXUSR


# --------------------------------------------------------------------------- hostile paths

@pytest.mark.parametrize("bad", [
    "../escape.txt", "/etc/passwd", "a/../../b.txt", ".git/config",
    "sub/node_modules/x.js", "C:evil.txt", "a/./b.txt"])
def test_traversal_and_excluded_paths_are_refused(bad):
    with pytest.raises(pa.UnsafePath):
        pa.check_relpath(bad)


def test_a_tampered_bundle_path_is_refused_before_anything_is_written(store, tmp_path):
    import json

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside.txt"
    _write(outside, "precious")
    _write(workspace / "a.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    manifest = store / record["id"] / "artifact.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["changes"][0]["path"] = "../outside.txt"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["code"] == "unsafe_path" and not result["applied"]
    assert outside.read_text(encoding="utf-8") == "precious"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"


def _tamper(store, artifact_id, mutate):
    """Rewrite a stored manifest the way a hand-edit, a bad merge or a bit flip would."""
    manifest = store / artifact_id / "artifact.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    mutate(data)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return manifest


def _one_change_record(store, tmp_path, workspace):
    _write(workspace / "a.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))
    shutil.rmtree(str(attempt))
    return record


def _dup(data, path):
    clone = dict(data["changes"][0])
    clone["path"] = path
    data["changes"].append(clone)


@pytest.mark.parametrize("name,mutate", [
    ("changes not a list", lambda d: d.update(changes={"a.txt": 1})),
    ("change not an object", lambda d: d.update(changes=["a.txt"])),
    ("baseline not an object", lambda d: d["changes"][0].update(baseline="file")),
    ("target missing", lambda d: d["changes"][0].pop("target")),
    ("state type unusable", lambda d: d["changes"][0]["target"].update(type="link")),
    ("sha not a digest", lambda d: d["changes"][0]["target"].update(sha256="zz")),
    ("sha not a string", lambda d: d["changes"][0]["target"].update(sha256=1)),
    ("size negative", lambda d: d["changes"][0]["target"].update(size=-1)),
    ("size not a number", lambda d: d["changes"][0]["target"].update(size="huge")),
    ("size is a bool", lambda d: d["changes"][0]["target"].update(size=True)),
    ("size absurd", lambda d: d["changes"][0]["target"].update(size=2 ** 62)),
    ("unknown action", lambda d: d["changes"][0].update(action="chmod")),
    ("action contradicts state", lambda d: d["changes"][0].update(action="delete")),
    ("blob is not its digest", lambda d: d["changes"][0].update(blob="f" * 64)),
    ("blob not a string", lambda d: d["changes"][0].update(blob=["x"])),
    ("duplicate path", lambda d: _dup(d, "a.txt")),
    ("case-colliding path", lambda d: _dup(d, "A.txt")),
    ("path not a string", lambda d: d["changes"][0].update(path=42)),
    ("id does not match dir", lambda d: d.update(id="20200101T000000Z-abcdef12")),
    ("schema is a string", lambda d: d.update(schema="1")),
    ("schema is a bool", lambda d: d.update(schema=True)),
    ("workspace not a string", lambda d: d.update(workspace=None)),
    ("workspace key missing", lambda d: d.pop("workspace_key")),
    ("summary not an object", lambda d: d.update(summary="two files")),
])
def test_a_malformed_manifest_is_corrupt_not_a_crash(store, tmp_path, name, mutate):
    """Every one of these used to reach _plan as a ValueError/TypeError/AttributeError."""
    workspace = tmp_path / "repo"
    record = _one_change_record(store, tmp_path, workspace)
    _tamper(store, record["id"], mutate)

    with pytest.raises(pa.PackArtifactError):
        pa.inspect_artifact(record["id"])

    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["code"] == "corrupt" and not result["applied"], name
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"
    assert not (store / record["id"] / "backups").exists(), "corruption is detected before writing"
    # A corrupt bundle must not take the review list down with it.
    assert pa.list_artifacts(workspace=str(workspace)) == []


def test_a_manifest_larger_than_the_read_bound_is_corrupt(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    record = _one_change_record(store, tmp_path, workspace)
    monkeypatch.setattr(pa, "_MAX_MANIFEST_BYTES", 200)
    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["code"] == "corrupt" and "artifact.json is" in result["error"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"


@pytest.mark.parametrize("bad", [".GiT/config", "sub/.Git/hooks/x", "NODE_modules/a.js",
                                 "sub/__PYCACHE__/x.pyc"])
def test_excluded_trees_are_refused_whatever_the_case(bad):
    """.GiT IS .git on Windows and macOS; an exact-case test would be a way into the repo."""
    with pytest.raises(pa.UnsafePath):
        pa.check_relpath(bad)


def test_a_case_variant_excluded_tree_is_skipped_not_refused(store, tmp_path):
    """Refusing ".GiT" must not turn a repo with a "Build/" directory into an unappliable bundle.

    The diff skips those trees exactly as apply refuses them, so they never enter a bundle at
    all — the winner's real changes still apply, and the directory is left alone.
    """
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    _write(workspace / "Build" / "out.o", "old")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    _write(attempt / "Build" / "out.o", "new")

    bundle = _bundle(workspace, attempt, baseline)
    assert [c["path"] for c in bundle.changes] == ["a.txt"] and bundle.unsupported == []
    record = pa.save_artifact(bundle)
    assert pa.apply_artifact(record["id"], str(workspace))["applied"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "after"
    assert (workspace / "Build" / "out.o").read_text(encoding="utf-8") == "old"


def test_a_case_bypassed_excluded_path_is_refused_at_apply(store, tmp_path):
    workspace = tmp_path / "repo"
    record = _one_change_record(store, tmp_path, workspace)
    _write(workspace / ".git" / "config", "[core]\n")
    _tamper(store, record["id"], lambda d: d["changes"][0].update(path=".GiT/config"))

    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["code"] == "unsafe_path" and not result["applied"]
    assert (workspace / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"


def _link_dir(link, target):
    """Make ``link`` a junction (Windows) or symlink (POSIX) to ``target``.  False if refused."""
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        return done.returncode == 0
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        return False
    return True


def test_store_internals_cannot_redirect_through_a_link(store, tmp_path):
    """artifact.json / blobs are ours; if one has become a link, the bundle is corrupt."""
    workspace = tmp_path / "repo"
    record = _one_change_record(store, tmp_path, workspace)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    blobs = store / record["id"] / "blobs"
    shutil.rmtree(str(blobs))
    if not _link_dir(blobs, elsewhere):
        pytest.skip("this platform/account cannot create junctions or symlinks")
    try:
        result = pa.apply_artifact(record["id"], str(workspace))
        assert result["code"] == "corrupt" and "symlink or junction" in result["error"]
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"
    finally:
        os.rmdir(str(blobs))


def test_a_linked_backup_directory_stops_the_apply_before_it_writes(store, tmp_path):
    """Backups are the user's originals; writing them through a link would scatter them."""
    workspace = tmp_path / "repo"
    record = _one_change_record(store, tmp_path, workspace)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if not _link_dir(store / record["id"] / "backups", elsewhere):
        pytest.skip("this platform/account cannot create junctions or symlinks")
    try:
        result = pa.apply_artifact(record["id"], str(workspace))
        assert result["code"] == "corrupt" and "symlink or junction" in result["error"]
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"
        assert not list(elsewhere.iterdir()), "no originals were written through the link"
    finally:
        os.rmdir(str(store / record["id"] / "backups"))


def test_a_linked_artifact_directory_is_never_read(store, tmp_path):
    workspace = tmp_path / "repo"
    record = _one_change_record(store, tmp_path, workspace)
    victim = store / record["id"]
    moved = tmp_path / "moved-bundle"
    os.rename(str(victim), str(moved))
    if not _link_dir(victim, moved):
        pytest.skip("this platform/account cannot create junctions or symlinks")
    try:
        result = pa.apply_artifact(record["id"], str(workspace))
        assert result["code"] == "corrupt" and "symlink or junction" in result["error"]
        assert pa.list_artifacts(workspace=str(workspace)) == []
    finally:
        os.rmdir(str(victim))


def test_corrupt_blob_is_detected_before_the_workspace_is_touched(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    _write(workspace / "b.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    (attempt / "b.txt").write_text("after too", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    blobs = sorted((store / record["id"] / "blobs").iterdir())
    blobs[0].write_bytes(b"corrupted")
    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["code"] == "corrupt" and not result["applied"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "before"


def test_unknown_or_malformed_ids_are_reported_not_raised(store, tmp_path):
    for bad in ("../../etc", "nope", "20260101T000000Z-deadbeef"):
        result = pa.apply_artifact(bad, str(tmp_path))
        assert result["code"] == "not_found" and not result["applied"]


@pytest.mark.skipif(not hasattr(os, "symlink") and os.name != "nt",
                    reason="platform has no links")
def test_a_changed_symlink_makes_the_bundle_refuse_to_apply(store, tmp_path):
    workspace = tmp_path / "repo"
    _write(workspace / "real.txt", "content")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    try:
        os.symlink(str(attempt / "real.txt"), str(attempt / "link.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("this platform/account cannot create symlinks")

    bundle = _bundle(workspace, attempt, baseline)
    assert [u["path"] for u in bundle.unsupported] == ["link.txt"]
    record = pa.save_artifact(bundle)
    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["code"] == "unsupported" and not result["applied"]
    assert not (workspace / "link.txt").exists()


def test_an_unsupported_bundle_is_refused_before_editing_on_every_platform(store, tmp_path):
    """Same guarantee as the symlink case, without needing link privileges to prove it."""
    import json

    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    manifest = store / record["id"] / "artifact.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["unsupported"] = [{"path": "link.txt", "reason": "symlink or junction changed"}]
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = pa.apply_artifact(record["id"], str(workspace))
    assert result["code"] == "unsupported" and not result["applied"]
    assert result["unsupported"][0]["path"] == "link.txt"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"


def test_a_junction_in_the_path_is_never_followed(store, tmp_path):
    """A Windows junction (or POSIX symlink) standing in for a directory is a refusal."""
    workspace = tmp_path / "repo"
    real_target = tmp_path / "target"
    _write(real_target / "victim.txt", "precious")
    _write(workspace / "sub" / "file.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "sub" / "file.txt").write_text("after", encoding="utf-8")
    _write(attempt / "sub" / "victim.txt", "written by the model")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    shutil.rmtree(str(workspace / "sub"))
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(workspace / "sub"),
                               str(real_target)], capture_output=True, text=True)
        if done.returncode:
            pytest.skip("could not create a junction: %s" % (done.stderr or done.stdout))
    else:
        os.symlink(str(real_target), str(workspace / "sub"))

    result = pa.apply_artifact(record["id"], str(workspace))
    assert not result["applied"] and result["code"] in ("conflict", "unsafe_path")
    assert (real_target / "victim.txt").read_text(encoding="utf-8") == "precious"
    assert not (real_target / "file.txt").exists(), "nothing was written through the link"


# --------------------------------------------------------------------------- failure honesty

def test_partial_filesystem_failure_rolls_back_and_says_so(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before-a")
    _write(workspace / "b.txt", "before-b")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after-a", encoding="utf-8")
    (attempt / "b.txt").write_text("after-b", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    real_replace = os.replace
    seen = []

    def flaky(src, dst, *args, **kwargs):
        if str(dst).endswith("b.txt"):
            raise PermissionError(13, "Permission denied")
        seen.append(dst)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    result = pa.apply_artifact(record["id"], str(workspace))
    monkeypatch.undo()

    assert not result["applied"] and result["code"] == "io_error"
    assert "b.txt" in result["error"] and "Permission denied" in result["error"]
    assert result["rolled_back"] and result["backup_dir"]
    assert result["changed"] == [] and result["unrestored"] == [], "clean rollback: no changes"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before-a", "rolled back"
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "before-b"
    assert os.path.isdir(result["backup_dir"]), "originals stay reviewable"
    assert not list(workspace.glob("**/.collie-pack-*.tmp")), "no staged temp file left behind"


def test_a_parent_that_becomes_a_junction_mid_apply_is_refused_and_reported(store, tmp_path,
                                                                           monkeypatch):
    """The leaf still looks right through the link; only re-resolving the PARENT catches it."""
    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(workspace / "sub" / "a.txt", "before-a")
    _write(workspace / "sub" / "b.txt", "shared bytes")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "sub" / "a.txt").unlink()
    (attempt / "sub" / "b.txt").unlink()
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))
    # Same content, somewhere the bundle has no business touching: a leaf-only check passes here.
    _write(outside / "b.txt", "shared bytes")

    real_remove, swapped = os.remove, []

    def swap(path, *args, **kwargs):
        real_remove(path, *args, **kwargs)
        if not swapped:                       # right after sub/a.txt is deleted
            swapped.append(True)
            os.rename(str(workspace / "sub"), str(tmp_path / "sub-moved"))
            if not _link_dir(workspace / "sub", outside):
                os.rename(str(tmp_path / "sub-moved"), str(workspace / "sub"))
                swapped.append("skip")

    monkeypatch.setattr(os, "remove", swap)
    result = pa.apply_artifact(record["id"], str(workspace))
    monkeypatch.undo()
    try:
        if "skip" in swapped:
            pytest.skip("this platform/account cannot create junctions or symlinks")
        assert not result["applied"] and result["code"] == "io_error"
        assert "symlink or junction" in result["error"]
        assert (outside / "b.txt").read_text(encoding="utf-8") == "shared bytes", \
            "nothing was deleted through the link"
        # Rollback could not put sub/a.txt back either (its parent is the link now), and the
        # result says so instead of reporting an empty change list.
        assert not result["rolled_back"]
        assert [c["path"] for c in result["changed"]] == ["sub/a.txt"]
        assert [c["path"] for c in result["unrestored"]] == ["sub/a.txt"]
        backup = result["changed"][0]["backup"]
        assert backup and open(backup, encoding="utf-8").read() == "before-a"
        assert "PARTIALLY changed" in result["error"] and result["backup_dir"] in result["error"]
    finally:
        if os.path.exists(str(workspace / "sub")):
            os.rmdir(str(workspace / "sub"))


def test_a_write_whose_parent_becomes_a_junction_mid_apply_is_refused(store, tmp_path,
                                                                     monkeypatch):
    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(workspace / "sub" / "one.txt", "before-1")
    _write(workspace / "sub" / "two.txt", "before-2")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "sub" / "one.txt").write_text("after-1", encoding="utf-8")
    (attempt / "sub" / "two.txt").write_text("after-2", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    real_replace, swapped = os.replace, []

    def swap(src, dst, *args, **kwargs):
        result = real_replace(src, dst, *args, **kwargs)
        if not swapped:
            swapped.append(True)
            os.rename(str(workspace / "sub"), str(tmp_path / "sub-moved"))
            if not _link_dir(workspace / "sub", outside):
                os.rename(str(tmp_path / "sub-moved"), str(workspace / "sub"))
                swapped.append("skip")
        return result

    monkeypatch.setattr(os, "replace", swap)
    out = pa.apply_artifact(record["id"], str(workspace))
    monkeypatch.undo()
    try:
        if "skip" in swapped:
            pytest.skip("this platform/account cannot create junctions or symlinks")
        assert not out["applied"] and "symlink or junction" in out["error"]
        assert not list(outside.iterdir()), "nothing was written through the link"
        assert not list(outside.glob(".collie-pack-*.tmp"))
    finally:
        if os.path.exists(str(workspace / "sub")):
            os.rmdir(str(workspace / "sub"))


def test_a_leaf_that_becomes_a_link_mid_apply_is_never_replaced(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    secret = tmp_path / "secret.txt"
    _write(secret, "precious")
    _write(workspace / "a.txt", "before-a")
    _write(workspace / "b.txt", "before-b")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after-a", encoding="utf-8")
    (attempt / "b.txt").write_text("after-b", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline))

    real_replace, swapped = os.replace, []

    def swap(src, dst, *args, **kwargs):
        outcome = real_replace(src, dst, *args, **kwargs)
        if not swapped:
            swapped.append(True)
            os.remove(str(workspace / "b.txt"))
            try:
                os.symlink(str(secret), str(workspace / "b.txt"))
            except (OSError, NotImplementedError, AttributeError):
                swapped.append("skip")
        return outcome

    monkeypatch.setattr(os, "replace", swap)
    result = pa.apply_artifact(record["id"], str(workspace))
    monkeypatch.undo()
    if "skip" in swapped:
        pytest.skip("this platform/account cannot create symlinks")
    assert not result["applied"] and "symlink or junction" in result["error"]
    assert secret.read_text(encoding="utf-8") == "precious", "we did not write through the link"


def test_restart_can_review_and_apply_without_rerunning_a_model(store, tmp_path):
    """The whole point: a saved winner is applied later by a different process, no model call."""
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    attempt = _isolate(workspace, tmp_path)
    baseline = pa.capture_baseline(str(attempt))
    (attempt / "a.txt").write_text("after", encoding="utf-8")
    record = pa.save_artifact(_bundle(workspace, attempt, baseline, task="fix a"))
    shutil.rmtree(str(attempt))

    script = (
        "import json, os, sys;"
        "sys.path.insert(0, %r);"
        "from harness import pack_artifacts as pa;"
        "found = pa.inspect_artifact(%r);"
        "out = pa.apply_artifact(%r, %r);"
        "print(json.dumps({'task': found['metadata']['task'], 'applied': out['applied'],"
        " 'changed': out['changed']}))"
        % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
           record["id"], record["id"], str(workspace)))
    env = dict(os.environ, COLLIE_PACK_ARTIFACT_DIR=str(store))
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                          timeout=120)
    assert done.returncode == 0, done.stderr
    payload = __import__("json").loads(done.stdout.strip().splitlines()[-1])
    assert payload["task"] == "fix a" and payload["applied"]
    assert [c["path"] for c in payload["changed"]] == ["a.txt"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "after"


# --------------------------------------------------------------------------- run_pack wiring

def _fake_candidates(monkeypatch, edit):
    """A Pack whose 'model' is a function editing the isolated tree.  No provider is contacted."""
    from harness import catalog, cli, scratch

    class Result:
        answer, verified, turns, error, cost_usd = "done", True, 1, "", 0.0

    class Harness:
        memory = recorder = types.SimpleNamespace(close=lambda: None)

        def __init__(self, cwd):
            self.cwd = cwd

        def run(self, task_id, task, **kwargs):
            edit(self.cwd, int(task_id.replace("pack", "")))
            return Result()

    monkeypatch.setattr(catalog, "preflight", lambda members: [])
    monkeypatch.setattr(cli, "make_harness", lambda cwd, **kw: Harness(cwd))
    monkeypatch.setattr(cli, "configure_run_options", lambda *a, **k: None)
    monkeypatch.setattr(scratch, "isolate_harness", lambda *a, **k: None)


def test_run_pack_saves_the_winner_by_default_and_applies_nothing(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")

    def edit(cwd, _idx):
        with open(os.path.join(cwd, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("after")

    _fake_candidates(monkeypatch, edit)
    result = pack.run_pack("fix a", str(workspace), n=1, provider="mock")

    assert result["winner"] == 0 and not result["applied"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before", "review, not apply"
    artifact = result["artifact"]
    assert artifact and artifact["summary"]["modified"] == 1
    assert [c["path"] for c in artifact["changed"]] == ["a.txt"]
    assert "changes" not in artifact, "the SSE payload carries a summary, not the bundle"
    assert result["artifact_error"] == "" and result["retained_attempt_dir"] == ""
    assert not result["cleanup_errors"]

    # The reviewed winner is applied later, with no second pack.
    applied = pa.apply_artifact(artifact["id"], str(workspace))
    assert applied["applied"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "after"


def test_run_pack_apply_uses_the_same_conflict_rules(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    _write(workspace / "shared.txt", "before")

    def edit(cwd, _idx):
        with open(os.path.join(cwd, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("after")
        with open(os.path.join(cwd, "shared.txt"), "w", encoding="utf-8") as handle:
            handle.write("model version")
        # The user edits a file Pack is about to apply, while the candidate is still running.
        (workspace / "shared.txt").write_text("human version", encoding="utf-8")

    _fake_candidates(monkeypatch, edit)
    result = pack.run_pack("fix a", str(workspace), n=1, apply=True, provider="mock")

    assert result["winner"] == 0 and not result["applied"]
    assert "apply failed" in result["reason"] and result["apply_error"]
    assert [c["path"] for c in result["apply_conflicts"]] == ["shared.txt"]
    assert (workspace / "shared.txt").read_text(encoding="utf-8") == "human version"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before", "no partial apply"
    # The rejected winner is still reviewable rather than lost.
    assert pa.inspect_artifact(result["artifact"]["id"])["summary"]["modified"] == 2


def test_run_pack_apply_writes_the_winner_when_nothing_conflicts(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    _write(workspace / "stale.txt", "remove me")

    def edit(cwd, _idx):
        with open(os.path.join(cwd, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("after")
        os.remove(os.path.join(cwd, "stale.txt"))
        (workspace / "unrelated.txt").write_text("typed during the run", encoding="utf-8")

    _fake_candidates(monkeypatch, edit)
    result = pack.run_pack("fix a", str(workspace), n=1, apply=True, provider="mock")

    assert result["applied"] and not result["apply_error"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "after"
    assert not (workspace / "stale.txt").exists()
    assert (workspace / "unrelated.txt").read_text(encoding="utf-8") == "typed during the run"


def test_an_unsaved_winner_keeps_its_attempt_directory(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")

    def edit(cwd, _idx):
        with open(os.path.join(cwd, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("after")

    _fake_candidates(monkeypatch, edit)
    monkeypatch.setattr(pa, "save_artifact", lambda *a, **kw: (_ for _ in ()).throw(
        pa.ArtifactStorageError("disk is full")))

    result = pack.run_pack("fix a", str(workspace), n=1, provider="mock")
    retained = result["retained_attempt_dir"]
    try:
        assert result["artifact"] is None and "disk is full" in result["artifact_error"]
        assert retained and os.path.isdir(retained), "the paid-for winner is not deleted silently"
        assert "winner kept at" in result["reason"]
        with open(os.path.join(retained, "a.txt"), encoding="utf-8") as handle:
            assert handle.read() == "after"
    finally:
        shutil.rmtree(retained, ignore_errors=True)


def test_run_pack_keeps_the_winner_when_the_store_cannot_make_room(store, tmp_path, monkeypatch):
    """A full store never costs anybody a winner: the refusal keeps BOTH the old and new edits."""
    monkeypatch.setenv("COLLIE_PACK_ARTIFACT_KEEP", "1")
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")
    _write(workspace / "old.txt", "before")
    earlier = _save_edit(workspace, tmp_path, "earlier", "old.txt", "an earlier unapplied winner")

    def edit(cwd, _idx):
        with open(os.path.join(cwd, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("after")

    _fake_candidates(monkeypatch, edit)
    result = pack.run_pack("fix a", str(workspace), n=1, apply=True, provider="mock")

    retained = result["retained_attempt_dir"]
    try:
        assert result["artifact"] is None and not result["applied"]
        assert earlier["id"] in result["artifact_error"] and "NOT saved" in result["artifact_error"]
        assert "apply failed" in result["reason"] and "winner kept at" in result["reason"]
        assert retained and os.path.isdir(retained)
        with open(os.path.join(retained, "a.txt"), encoding="utf-8") as handle:
            assert handle.read() == "after", "the new winner is still on disk"
        # And the older winner was not sacrificed to make room for it.
        assert pa.inspect_artifact(earlier["id"])["id"] == earlier["id"]
        assert pa.apply_artifact(earlier["id"], str(workspace))["applied"]
        assert (workspace / "old.txt").read_text(encoding="utf-8") == "an earlier unapplied winner"
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "before", "nothing was applied"
    finally:
        shutil.rmtree(retained, ignore_errors=True)


def test_a_stopped_pack_saves_and_applies_nothing(store, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    _write(workspace / "a.txt", "before")

    def edit(cwd, _idx):
        with open(os.path.join(cwd, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("after")

    _fake_candidates(monkeypatch, edit)
    result = pack.run_pack("fix a", str(workspace), n=1, apply=True, provider="mock",
                           cancel=lambda: True)

    assert result["canceled"] and result["winner"] is None
    assert not result["applied"] and result["artifact"] is None
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "before"
    assert not list(store.glob("2*")) if store.exists() else True
