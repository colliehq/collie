"""Bounded human review of saved Pack changes; never calls a model."""
from __future__ import annotations

import difflib
import hashlib
import os
import stat

from . import pack_artifacts as artifacts

MAX_TEXT_BYTES = 64 * 1024
MAX_PREVIEW_FILES = 50
MAX_DIFF_CHARS = 16 * 1024
MAX_REVIEW_CHARS = 256 * 1024


def _read_text(root, rel, expected_hash=None):
    path = artifacts.resolve_within(root, rel)
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or artifacts._link_like(path, info):
        raise ValueError("Not a regular file")
    if info.st_size > MAX_TEXT_BYTES:
        raise ValueError("File exceeds the 64 KiB text preview limit")
    with open(path, "rb") as handle:
        raw = handle.read(MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("File exceeds the 64 KiB text preview limit")
    if expected_hash and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("File changed since the saved change was captured")
    if b"\0" in raw:
        raise ValueError("Binary file; content preview unavailable")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Non-UTF-8 file; content preview unavailable") from exc


def review(artifact_id, cwd=None, *, root=None):
    record = artifacts.inspect_artifact(artifact_id, root=root)
    target = cwd or record["workspace"]
    check = artifacts.apply_artifact(artifact_id, target, root=root, dry_run=True)
    result = {"artifact": artifacts.summarize_artifact(record, preview=MAX_PREVIEW_FILES),
              "check": check, "files": [], "preview_truncated": False}
    # A conflict is reviewable against the current workspace. Corrupt or unsafe
    # records must never turn into file reads through the preview path.
    if not check["ok"] and check["code"] != "conflict":
        return result
    used = 0
    for change in record["changes"][:MAX_PREVIEW_FILES]:
        item = {"path": change["path"], "action": change["action"], "diff": "", "note": ""}
        try:
            try:
                before = _read_text(target, change["path"])
            except FileNotFoundError:
                before = ""
            after = ""
            if change["action"] != "delete":
                sha = change["blob"]
                after = _read_text(record["path"], "blobs/" + sha, sha)
            diff = "".join(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile="current/" + change["path"], tofile="saved/" + change["path"]))
            remaining = max(0, min(MAX_DIFF_CHARS, MAX_REVIEW_CHARS - used))
            item["diff"] = diff[:remaining]
            used += len(item["diff"])
            if len(diff) > remaining:
                item["note"] = "Diff preview shortened; applying uses the complete saved file"
                result["preview_truncated"] = True
            elif not diff:
                item["note"] = "The saved contents are already present"
        except (OSError, ValueError, artifacts.PackArtifactError) as exc:
            item["note"] = str(exc)
        result["files"].append(item)
    if len(record["changes"]) > MAX_PREVIEW_FILES:
        result["preview_truncated"] = True
    return result


def saved_command(args):
    import json
    import sys

    ident = args.saved
    if args.task:
        print("Use either a new task or --saved, not both.", file=sys.stderr)
        return 2
    if not ident:
        if args.apply:
            print("Choose a saved artifact ID before using --apply.", file=sys.stderr)
            return 2
        rows = artifacts.list_artifacts(workspace=args.cwd or os.getcwd())
        if args.json:
            print(json.dumps({"artifacts": rows}, ensure_ascii=False))
        elif not rows:
            print("No saved Pack changes for this workspace.")
        else:
            for row in rows:
                print("%s  %s  %s" % (row["id"], row.get("created_iso", ""),
                                        row.get("metadata", {}).get("task", "")))
            print("Review: collie pack --saved ID\nApply:  collie pack --saved ID --apply")
        return 0
    try:
        data = (artifacts.apply_artifact(ident, args.cwd) if args.apply
                else review(ident, args.cwd))
    except artifacts.PackArtifactError as exc:
        data = {"ok": False, "error": str(exc)}
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    elif args.apply:
        print("Saved changes applied." if data.get("applied") else data.get("error", "Apply failed"))
        for row in data.get("conflicts", []):
            print("  %s: %s" % (row.get("path"), row.get("reason")))
        if data.get("backup_dir"):
            print("Original files: %s" % data["backup_dir"])
    elif data.get("artifact"):
        print("Saved Pack %s\nWorkspace: %s" % (ident, data["artifact"]["workspace"]))
        for item in data["files"]:
            print("\n%s %s\n%s" % (item["action"], item["path"], item["diff"] or item["note"]))
        if not data["check"]["ok"]:
            print(data["check"]["error"])
        else:
            print("\nApply without another model run: collie pack --saved %s --apply" % ident)
    else:
        print(data.get("error", "Saved changes unavailable"), file=sys.stderr)
    return 0 if data.get("ok", data.get("check", {}).get("ok", False)) else 1
