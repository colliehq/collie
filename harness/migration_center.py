"""Dry-run-first migration bridge for local agent setups.

The migration center inventories only documented, local configuration roots.  It never mutates a
source agent and never overwrites a different destination.  Skills can be installed into Collie's
global skill library after explicit approval; instructions, settings, MCP files, hooks, and raw
session artifacts are copied into a provenance-labelled import archive for review.  A saved sync
policy re-runs the same digest-checked operation without granting new item types.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path


_LOCK = threading.RLock()
_PLAN_RE = re.compile(r"^mig_[0-9a-f]{24}$")
_MAX_FILE = 8 * 1024 * 1024
_MAX_ITEMS = 5000


def _state_root():
    return Path(os.path.abspath(os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")))


def _registry_path():
    return _state_root() / "migration-center.json"


def _sources():
    home = Path.home()
    return {
        "claude": {"root": home / ".claude", "instructions": ["CLAUDE.md"],
                   "settings": ["settings.json"], "skills": ["skills"],
                   "sessions": ["projects"], "hooks": ["hooks"]},
        "cursor": {"root": home / ".cursor", "instructions": ["rules", "mcp.json"],
                   "settings": ["settings.json", "mcp.json"], "skills": ["skills"],
                   "sessions": ["chats"], "hooks": ["hooks"]},
        "codex": {"root": home / ".codex", "instructions": ["AGENTS.md"],
                  "settings": ["config.toml"], "skills": ["skills"],
                  "sessions": ["sessions"], "hooks": ["hooks"]},
        "pi": {"root": home / ".pi" / "agent", "instructions": ["AGENTS.md", "SYSTEM.md"],
               "settings": ["settings.json", "models.json"], "skills": ["skills"],
               "sessions": ["sessions"], "hooks": ["extensions"]},
        "hermes": {"root": home / ".hermes", "instructions": ["AGENTS.md", "SOUL.md"],
                   "settings": ["config.yaml", "config.json"], "skills": ["skills"],
                   "sessions": ["sessions"], "hooks": ["hooks"]},
    }


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _load():
    try:
        with open(_registry_path(), encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("expected object")
    except FileNotFoundError:
        value = {"version": 1, "plans": {}, "history": [], "sync": {}}
    except (OSError, ValueError) as exc:
        raise RuntimeError("migration registry is unreadable: %s" % exc)
    value.setdefault("plans", {}); value.setdefault("history", []); value.setdefault("sync", {})
    return value


def _write(value):
    path = _registry_path(); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _walk_files(base):
    base = Path(base)
    if base.is_file() and not base.is_symlink():
        yield base
        return
    if not base.is_dir() or base.is_symlink():
        return
    for root, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            path = Path(root) / name
            if not path.is_symlink():
                yield path


def _kind_for(rel, spec):
    first = rel.parts[0] if rel.parts else ""
    for kind in ("skills", "sessions", "hooks", "settings", "instructions"):
        if first in spec.get(kind, []):
            return kind
    return "other"


def scan():
    out = []
    for source, spec in _sources().items():
        root = spec["root"]
        counts = {k: 0 for k in ("instructions", "settings", "skills", "sessions", "hooks", "other")}
        total = 0
        if root.exists():
            for path in _walk_files(root):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                kind = _kind_for(path.relative_to(root), spec)
                counts[kind] += 1; total += size
                if sum(counts.values()) >= _MAX_ITEMS:
                    break
        out.append({"source": source, "available": root.is_dir(), "root": str(root),
                    "counts": counts, "bytes": total})
    return out


def _destination(source, kind, rel):
    if kind == "skills" and len(rel.parts) >= 3 and rel.name == "SKILL.md":
        skill_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", rel.parts[-2]).strip("-.") or "skill"
        return _state_root() / "skills" / ("imported-%s-%s" % (source, skill_name)) / "SKILL.md"
    return _state_root() / "imports" / source / kind / Path(*rel.parts)


class MigrationCenter:
    def scan(self):
        return scan()

    def plan(self, source, *, kinds=None, keep_synced=False):
        source = str(source or "").lower()
        spec = _sources().get(source)
        if not spec:
            raise ValueError("unsupported migration source")
        root = spec["root"]
        if not root.is_dir():
            raise ValueError("%s setup was not found" % source)
        wanted = set(kinds or ("instructions", "settings", "skills", "sessions", "hooks"))
        allowed = {"instructions", "settings", "skills", "sessions", "hooks"}
        if not wanted or not wanted <= allowed:
            raise ValueError("invalid migration item type")
        items, skipped = [], []
        for path in _walk_files(root):
            rel = path.relative_to(root); kind = _kind_for(rel, spec)
            if kind not in wanted:
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                skipped.append({"path": str(rel), "reason": str(exc)[:200]}); continue
            if size > _MAX_FILE:
                skipped.append({"path": str(rel), "reason": "file exceeds 8 MiB safety limit"}); continue
            dest = _destination(source, kind, rel)
            digest = _digest(path)
            conflict = False; identical = False
            if dest.exists():
                try:
                    identical = _digest(dest) == digest
                    conflict = not identical
                except OSError:
                    conflict = True
            items.append({"kind": kind, "relative": rel.as_posix(), "source_path": str(path),
                          "destination": str(dest), "bytes": size, "sha256": digest,
                          "conflict": conflict, "identical": identical})
            if len(items) >= _MAX_ITEMS:
                skipped.append({"path": "*", "reason": "item limit reached"}); break
        plan_id = "mig_" + uuid.uuid4().hex[:24]
        canonical = json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8")
        plan = {"id": plan_id, "source": source, "root": str(root), "kinds": sorted(wanted),
                "items": items, "skipped": skipped, "created_at": time.time(),
                "digest": hashlib.sha256(canonical).hexdigest(), "status": "planned",
                "keep_synced": keep_synced is True}
        with _LOCK:
            state = _load(); state["plans"][plan_id] = plan; _write(state)
        return plan

    def apply(self, plan_id, *, confirm=False, keep_synced=None):
        if confirm is not True:
            raise ValueError("explicit confirm=true is required")
        if not _PLAN_RE.fullmatch(str(plan_id or "")):
            raise ValueError("invalid migration plan id")
        with _LOCK:
            state = _load(); plan = state["plans"].get(plan_id)
            if not plan:
                raise ValueError("migration plan not found")
            copied, identical, conflicts, changed = [], [], [], []
            for item in plan.get("items") or []:
                src, dst = Path(item["source_path"]), Path(item["destination"])
                if not src.is_file() or src.is_symlink() or _digest(src) != item["sha256"]:
                    changed.append(item["relative"]); continue
                if dst.exists():
                    try:
                        if _digest(dst) == item["sha256"]:
                            identical.append(item["relative"]); continue
                    except OSError:
                        pass
                    conflicts.append(item["relative"]); continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".tmp")
                with open(src, "rb") as inp, open(tmp, "wb") as out:
                    shutil.copyfileobj(inp, out, 1024 * 1024); out.flush(); os.fsync(out.fileno())
                os.replace(tmp, dst)
                copied.append(item["relative"])
            status = "applied" if not conflicts and not changed else "needs_review"
            result = {"plan_id": plan_id, "source": plan["source"], "at": time.time(),
                      "status": status, "copied": copied, "identical": identical,
                      "conflicts": conflicts, "source_changed": changed}
            plan["status"] = status; plan["last_result"] = result
            sync = plan.get("keep_synced") if keep_synced is None else keep_synced is True
            if sync:
                state["sync"][plan["source"]] = {"kinds": plan["kinds"], "enabled": True,
                                                  "last_plan": plan_id, "updated_at": time.time()}
            state["history"] = (state.get("history") or [])[-99:] + [result]
            _write(state)
            return result

    def sync(self, source, *, confirm=False):
        if confirm is not True:
            raise ValueError("explicit confirm=true is required")
        with _LOCK:
            policy = dict(_load().get("sync", {}).get(str(source or "").lower()) or {})
        if not policy.get("enabled"):
            raise ValueError("no enabled sync policy for this source")
        plan = self.plan(source, kinds=policy.get("kinds"), keep_synced=True)
        return self.apply(plan["id"], confirm=True, keep_synced=True)

    def snapshot(self):
        with _LOCK:
            state = _load()
            plans = [{k: v.get(k) for k in ("id", "source", "kinds", "created_at", "digest",
                                            "status", "keep_synced", "last_result")}
                     for v in state["plans"].values()]
            return {"sources": scan(), "plans": sorted(plans,
                    key=lambda x: float(x.get("created_at") or 0), reverse=True)[:50],
                    "history": list(state.get("history") or [])[-50:], "sync": state.get("sync") or {}}
