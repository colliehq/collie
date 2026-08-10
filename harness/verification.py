"""Repository check discovery and durable, structured execution evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import subprocess
import time


_KIND_ORDER = {"test": 0, "typecheck": 1, "lint": 2, "build": 3}


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


def _git_snapshot(cwd: str) -> dict:
    from . import plat
    out = {"commit": "", "working_tree": "unversioned", "dirty_files": []}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
            timeout=10, **plat.no_window_kwargs())
        if commit.returncode != 0:
            return out
        out["commit"] = (commit.stdout or "").strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True,
            timeout=10, **plat.no_window_kwargs())
        lines = [line for line in (status.stdout or "").splitlines() if line]
        out["dirty_files"] = [line[3:] if len(line) > 3 else line for line in lines[:200]]
        out["working_tree"] = "dirty" if lines else "clean"
    except Exception:
        pass
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
        "passed": False,
        "timestamp": started,
        "duration_ms": 0,
        "output": "",
        "cwd": os.path.abspath(cwd),
        "commit": before["commit"],
        "working_tree": before["working_tree"],
        "dirty_files": before["dirty_files"],
        "ran_after_last_edit": bool(after_last_edit),
        "source": source,
    }
    if not command:
        evidence["output"] = "no verification command"
        return evidence
    args, use_shell = plat.shell_argv(command)
    try:
        proc = subprocess.run(
            args, shell=use_shell, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            **plat.no_window_kwargs())
        evidence["exit_code"] = int(proc.returncode)
        evidence["passed"] = proc.returncode == 0
        evidence["output"] = (proc.stdout or "")[-4000:]
    except subprocess.TimeoutExpired as e:
        evidence["output"] = ((e.stdout or "") if isinstance(e.stdout, str) else "")[-3500:] + \
            "\n(check timed out after %ds)" % timeout
    except Exception as e:
        evidence["output"] = "check failed to run: %s: %s" % (type(e).__name__, e)
    evidence["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return evidence
