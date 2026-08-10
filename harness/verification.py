"""Repository check discovery and durable, structured execution evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import time


_KIND_ORDER = {"test": 0, "typecheck": 1, "lint": 2, "build": 3}
_SNAPSHOT_FILE_CAP = 20_000
_SNAPSHOT_BYTE_CAP = 64 * 1024 * 1024


def _candidate(kind: str, command: str, source: str, confidence: str = "high") -> dict:
    return {"kind": kind, "command": command, "source": source, "confidence": confidence}


def detect_verification_commands(cwd: str) -> list[dict]:
    """Detect likely repo-owned checks without executing project code.

    Results are proposals, not permission.  The UI shows the first one and lets
    the user edit it; Test mode allowlists only that exact command.
    """
    cwd = os.path.abspath(cwd)
    found = []

    package = os.path.join(cwd, "package.json")
    if os.path.isfile(package) and os.path.getsize(package) <= 2_000_000:
        try:
            with open(package, encoding="utf-8") as f:
                scripts = (json.load(f) or {}).get("scripts") or {}
            pm = ("pnpm" if os.path.exists(os.path.join(cwd, "pnpm-lock.yaml")) else
                  "yarn" if os.path.exists(os.path.join(cwd, "yarn.lock")) else "npm")
            aliases = (
                ("test", ("test", "test:unit", "test:ci")),
                ("typecheck", ("typecheck", "type-check", "check:types")),
                ("lint", ("lint",)),
                ("build", ("build",)),
            )
            for kind, names in aliases:
                name = next((n for n in names if n in scripts), None)
                if name:
                    cmd = "%s %s%s" % (pm, "run " if pm == "npm" or name != "test" else "", name)
                    found.append(_candidate(kind, cmd, "package.json#scripts.%s" % name))
        except (OSError, ValueError, TypeError):
            pass

    python_markers = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
    if os.path.isdir(os.path.join(cwd, "tests")) or any(
            os.path.isfile(os.path.join(cwd, p)) for p in python_markers):
        found.append(_candidate("test", "python -m pytest -q", "Python test layout"))

    if os.path.isfile(os.path.join(cwd, "Cargo.toml")):
        found.append(_candidate("test", "cargo test", "Cargo.toml"))
    if os.path.isfile(os.path.join(cwd, "go.mod")):
        found.append(_candidate("test", "go test ./...", "go.mod"))

    makefile = next((os.path.join(cwd, n) for n in ("Makefile", "makefile")
                     if os.path.isfile(os.path.join(cwd, n))), None)
    if makefile:
        try:
            with open(makefile, encoding="utf-8", errors="replace") as f:
                text = f.read(512_000)
            for kind, target in (("test", "test"), ("typecheck", "typecheck"),
                                 ("lint", "lint"), ("build", "build")):
                if re.search(r"(?m)^%s\s*:" % re.escape(target), text):
                    found.append(_candidate(kind, "make " + target, os.path.basename(makefile)))
        except OSError:
            pass

    # De-duplicate while keeping the strongest/useful ordering stable.
    unique = {}
    for item in found:
        unique.setdefault(item["command"], item)
    return sorted(unique.values(), key=lambda x: (_KIND_ORDER.get(x["kind"], 99), x["command"]))


def _filesystem_snapshot(cwd: str) -> dict:
    """Best-effort freshness fingerprint for workspaces without usable Git metadata."""
    digest = hashlib.sha256()
    count = 0
    complete = True
    try:
        for root, dirs, files in os.walk(cwd):
            dirs[:] = sorted(name for name in dirs if name != ".git")
            for name in sorted(files):
                count += 1
                if count > _SNAPSHOT_FILE_CAP:
                    complete = False
                    break
                path = os.path.join(root, name)
                rel = os.path.relpath(path, cwd).replace(os.sep, "/")
                stat = os.lstat(path)
                digest.update(("%s\0%d\0%d\0%d\0" % (
                    rel, stat.st_mode, stat.st_size, stat.st_mtime_ns)).encode("utf-8", "surrogatepass"))
                if os.path.islink(path):
                    digest.update(os.readlink(path).encode("utf-8", "surrogatepass"))
            if not complete:
                break
    except OSError:
        complete = False
    return {"tree_digest": digest.hexdigest(), "snapshot_complete": complete,
            "snapshot_kind": "filesystem"}


def _git_snapshot(cwd: str) -> dict:
    from . import plat
    out = {"commit": "", "working_tree": "unversioned", "dirty_files": [],
           "tree_digest": "", "snapshot_complete": False,
           "snapshot_kind": "filesystem"}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
            timeout=10, **plat.no_window_kwargs())
        if commit.returncode != 0:
            out.update(_filesystem_snapshot(cwd))
            return out
        out["commit"] = (commit.stdout or "").strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=cwd, capture_output=True,
            timeout=10, **plat.no_window_kwargs())
        if status.returncode != 0:
            out["working_tree"] = "unknown"
            out.update(_filesystem_snapshot(cwd))
            return out
        raw_status = status.stdout or b""
        entries = [entry for entry in raw_status.split(b"\0") if entry]
        dirty = []
        untracked = []
        for entry in entries:
            if len(entry) < 3 or entry[2:3] != b" ":
                continue
            path = entry[3:].decode("utf-8", "replace")
            dirty.append(path)
            if entry[:2] == b"??":
                untracked.append(path)
        out["dirty_files"] = dirty[:200]
        out["working_tree"] = "dirty" if entries else "clean"

        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD", "--"], cwd=cwd,
            capture_output=True, timeout=30, **plat.no_window_kwargs())
        if diff.returncode != 0:
            out["working_tree"] = "unknown"
            out.update(_filesystem_snapshot(cwd))
            return out
        digest = hashlib.sha256()
        digest.update(out["commit"].encode("ascii", "replace"))
        digest.update(raw_status)
        digest.update(diff.stdout or b"")
        remaining = _SNAPSHOT_BYTE_CAP
        complete = True
        root = os.path.realpath(os.path.abspath(cwd))
        for rel in untracked:
            path = os.path.realpath(os.path.abspath(os.path.join(root, rel)))
            try:
                if os.path.commonpath((path, root)) != root or not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                digest.update(rel.encode("utf-8", "surrogatepass"))
                if size > remaining:
                    complete = False
                    digest.update(("oversize:%d" % size).encode("ascii"))
                    continue
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(min(1024 * 1024, remaining + 1))
                        if not chunk:
                            break
                        if len(chunk) > remaining:
                            complete = False
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
            except (OSError, ValueError):
                complete = False
        out.update({"tree_digest": digest.hexdigest(), "snapshot_complete": complete,
                    "snapshot_kind": "git"})
    except Exception:
        out.update(_filesystem_snapshot(cwd))
    return out


def run_verification_command(command: str, cwd: str, timeout: int = 300,
                             source: str = "user", after_last_edit: bool = True) -> dict:
    """Execute a proposed check and return receipt-ready evidence."""
    from . import plat
    command = (command or "").strip()
    started = datetime.now(timezone.utc).isoformat()
    before = _git_snapshot(cwd)
    t0 = time.monotonic()
    evidence = {
        "command": command,
        "exit_code": None,
        "command_passed": False,
        "passed": False,
        "timestamp": started,
        "duration_ms": 0,
        "output": "",
        "cwd": os.path.abspath(cwd),
        "commit": before["commit"],
        "working_tree": before["working_tree"],
        "dirty_files": before["dirty_files"],
        "ran_after_last_edit": False,
        "freshness": "not_run",
        "source": source,
    }
    if not command:
        evidence["output"] = "no verification command"
        return evidence
    args, use_shell = plat.shell_argv(command)
    executed = False
    try:
        proc = subprocess.run(
            args, shell=use_shell, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            **plat.no_window_kwargs())
        executed = True
        evidence["exit_code"] = int(proc.returncode)
        evidence["command_passed"] = proc.returncode == 0
        evidence["output"] = (proc.stdout or "")[-4000:]
    except subprocess.TimeoutExpired as e:
        executed = True
        evidence["output"] = ((e.stdout or "") if isinstance(e.stdout, str) else "")[-3500:] + \
            "\n(check timed out after %ds)" % timeout
    except Exception as e:
        evidence["output"] = "check failed to run: %s: %s" % (type(e).__name__, e)
    evidence["duration_ms"] = int((time.monotonic() - t0) * 1000)
    after = _git_snapshot(cwd)
    comparable = bool(before.get("tree_digest") and after.get("tree_digest") and
                      before.get("snapshot_complete") and after.get("snapshot_complete"))
    unchanged = bool(comparable and before["tree_digest"] == after["tree_digest"] and
                     before.get("commit") == after.get("commit"))
    evidence.update({
        "post_commit": after.get("commit", ""),
        "post_working_tree": after.get("working_tree", "unknown"),
        "post_dirty_files": after.get("dirty_files", []),
        "working_tree_changed_during_check": (not unchanged) if comparable else None,
        "ran_after_last_edit": bool(executed and after_last_edit and unchanged),
        "freshness": ("not_run" if not executed else
                      "caller_marked_stale" if not after_last_edit else "fresh" if unchanged else
                      "changed_during_check" if comparable else "unknown"),
        "snapshot_kind": before.get("snapshot_kind", "unknown"),
        "executed": executed,
    })
    # ``passed`` is the completion-grade verdict consumed by CLI/Web/Pack. Exit zero remains
    # separately visible as ``command_passed``, but it cannot certify bytes that changed during
    # the check or whose freshness snapshot was incomplete.
    evidence["passed"] = bool(
        evidence["command_passed"] and evidence["ran_after_last_edit"])
    return evidence
