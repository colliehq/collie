"""Trusted, deterministic lifecycle hooks for Collie's agent loop.

The contract intentionally mirrors the small, useful intersection of Codex and
Claude Code hooks: JSON goes to stdin, JSON may come back on stdout, and a hook
can fail closed at an authority boundary.  Hooks are *host policy*, never model
tools.  Project hooks are therefore ignored until that exact workspace has been
trusted with ``collie trust``.

Supported events are deliberately data, not a fixed enum.  Core currently emits
SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
PostToolUseFailure, Stop, and SessionEnd; Mission and supervisor code can use
the same dispatcher for TaskCreated/TaskCompleted/Notification events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from typing import Any

from . import plat
from .trust import TrustStore, canonical


_BLOCKING = {"UserPromptSubmit", "PreToolUse", "PermissionRequest", "Stop", "TaskCompleted"}
_MAX_OUTPUT = 64 * 1024


@dataclass
class HookResult:
    allowed: bool = True
    reason: str = ""
    additional_context: list[str] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)


def _matches(pattern: str | None, subject: str) -> bool:
    if not pattern:
        return True
    # Treat the common glob form as a glob and the common ``A|B`` form as a
    # regex.  An invalid regex is a non-match, never an accidental match-all.
    if any(ch in pattern for ch in "*?[") and "|" not in pattern:
        return fnmatch.fnmatchcase(subject, pattern)
    try:
        return re.fullmatch(pattern, subject) is not None
    except re.error:
        return False


def _config_paths(cwd: str, state_dir: str | None = None) -> list[str]:
    state = state_dir or os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    paths = [os.path.join(state, "hooks.json")]
    extra = os.environ.get("COLLIE_HOOKS", "").strip()
    if extra:
        paths.extend(os.path.abspath(os.path.expanduser(p.strip()))
                     for p in extra.split(os.pathsep) if p.strip())
    try:
        trusted = TrustStore().is_trusted(cwd)
    except Exception:
        trusted = False
    if trusted:
        paths.append(os.path.join(canonical(cwd), ".collie", "hooks.json"))
    # Extension hooks are included only while an approved package is enabled.  Library lookup
    # rechecks the package digest and revocation state; HookTrustStore independently pins the exact
    # JSON bytes, so either trust layer failing makes the hook disappear/fail closed.
    try:
        from .extensions import enabled_component_paths
        paths.extend(enabled_component_paths("hooks", state))
    except Exception:
        pass
    # Preserve precedence/order while preventing the same file from firing twice.
    out, seen = [], set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            out.append(path); seen.add(key)
    return out


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class HookTrustStore:
    """Trust the exact bytes of a hook file, not merely its directory.

    A previously reviewed project may later receive a changed hook in a pull;
    path-only trust would execute that new command silently.  Hash trust makes
    every changed definition return to pending review.
    """

    def __init__(self, path: str | None = None):
        state = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
        self.path = path or os.path.join(state, "hook_trust.json")

    def _load(self) -> dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return {}
        values = raw.get("trusted") if isinstance(raw, dict) else None
        return {os.path.normcase(os.path.abspath(k)): str(v)
                for k, v in (values or {}).items()
                if isinstance(k, str) and isinstance(v, str)} if isinstance(values, dict) else {}

    def is_trusted(self, path: str, digest: str | None = None) -> bool:
        key = os.path.normcase(os.path.abspath(path))
        try:
            digest = digest or _digest(path)
        except OSError:
            return False
        return self._load().get(key) == digest

    def set(self, path: str, trusted=True) -> str:
        # Trust writes can arrive from ``collie hooks trust`` while Library activation is adding
        # another exact hash.  Serialize the read-modify-replace across processes so neither
        # successful operation silently loses the other's entry.
        from .extensions import _Lock
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with _Lock(self.path + ".lock"):
            key = os.path.normcase(os.path.abspath(path))
            values = self._load()
            digest = _digest(path)
            if trusted: values[key] = digest
            else: values.pop(key, None)
            tmp = "%s.%d.%d.tmp" % (self.path, os.getpid(), threading.get_ident())
            try:
                with open(tmp, "x", encoding="utf-8") as fh:
                    json.dump({"trusted": values}, fh, indent=2, sort_keys=True)
                    fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
                try: os.chmod(tmp, 0o600)
                except OSError: pass
                os.replace(tmp, self.path)
            finally:
                try:
                    if os.path.exists(tmp): os.unlink(tmp)
                except OSError:
                    pass
            return digest


class HookManager:
    """Load trusted command hooks and dispatch them with bounded runtime."""

    def __init__(self, cwd: str, configs: list[dict] | None = None,
                 state_dir: str | None = None,
                 trust_store: HookTrustStore | None = None):
        self.cwd = canonical(cwd)
        self._groups: dict[str, list[dict]] = {}
        self._lock = threading.RLock()
        self.pending: list[dict[str, str]] = []
        self._dynamic = configs is None
        self._state_dir = state_dir
        self._trust_store = trust_store
        if configs is None:
            configs = []
            trust = trust_store or HookTrustStore(
                os.path.join(state_dir, "hook_trust.json") if state_dir else None)
            bypass = os.environ.get("COLLIE_HOOKS_BYPASS_TRUST") == "1"
            for path in _config_paths(cwd, state_dir):
                if not os.path.isfile(path):
                    continue
                try:
                    digest = _digest(path)
                    if not bypass and not trust.is_trusted(path, digest):
                        self.pending.append({"path": path, "sha256": digest})
                        continue
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        data = dict(data); data["_source"] = path
                        configs.append(data)
                except Exception:
                    # A malformed optional hook file cannot crash every Collie run.
                    # Dispatch receipts expose handler failures; config parse failures
                    # remain visible to ``collie hooks check`` (CLI integration).
                    continue
        for data in configs:
            source = str(data.get("_source") or "inline")
            hooks = data.get("hooks") if isinstance(data, dict) else None
            if not isinstance(hooks, dict):
                continue
            for event, groups in hooks.items():
                if not isinstance(groups, list):
                    continue
                for group in groups:
                    if not isinstance(group, dict):
                        continue
                    g = dict(group); g["_source"] = source
                    self._groups.setdefault(str(event), []).append(g)

    def _refresh_dynamic(self):
        """Observe Library disable/revoke/config changes in a long-lived agent process."""
        if not self._dynamic:
            return
        fresh = HookManager(self.cwd, state_dir=self._state_dir,
                            trust_store=self._trust_store)
        with self._lock:
            self._groups = fresh._groups
            self.pending = fresh.pending

    @property
    def active(self) -> bool:
        self._refresh_dynamic()
        with self._lock:
            return bool(self._groups)

    def events(self) -> list[str]:
        self._refresh_dynamic()
        with self._lock:
            return sorted(self._groups)

    def dispatch(self, event: str, payload: dict | None = None,
                 subject: str = "") -> HookResult:
        self._refresh_dynamic()
        result = HookResult()
        with self._lock:
            groups = list(self._groups.get(event, ()))
        if not groups:
            return result
        envelope = dict(payload or {})
        envelope.setdefault("hook_event_name", event)
        envelope.setdefault("cwd", self.cwd)
        for group in groups:
            if not _matches(group.get("matcher"), subject):
                continue
            handlers = group.get("hooks") or []
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if not isinstance(handler, dict) or handler.get("type", "command") != "command":
                    continue
                receipt = self._run_command(event, envelope, handler, group.get("_source"))
                result.receipts.append(receipt)
                ctx = receipt.get("additional_context")
                if ctx:
                    result.additional_context.append(str(ctx))
                if not receipt.get("allowed", True):
                    result.allowed = False
                    if receipt.get("reason"):
                        result.reason = str(receipt["reason"])
                    # Authority decisions are conjunctive: one deny is final.  Post
                    # hooks continue so audit/formatting handlers all get a chance.
                    if event in _BLOCKING:
                        return result
        return result

    def _run_command(self, event: str, payload: dict, handler: dict,
                     source: str | None) -> dict:
        command = str(handler.get("command") or "").strip()
        started = time.time()
        base = {"event": event, "source": source or "inline", "command": command,
                "allowed": True, "reason": "", "exit_code": None,
                "wall_ms": 0, "timed_out": False, "additional_context": ""}
        if not command:
            base.update(allowed=False, reason="hook command is empty")
            return base
        try:
            timeout = max(.1, min(600.0, float(handler.get("timeout", 30))))
        except (TypeError, ValueError):
            timeout = 30.0
        argv, use_shell = plat.shell_argv(command)
        try:
            proc = subprocess.run(
                argv, shell=use_shell, input=json.dumps(payload, ensure_ascii=False),
                text=True, capture_output=True, timeout=timeout, cwd=self.cwd,
                **plat.no_window_kwargs())
            stdout = (proc.stdout or "")[:_MAX_OUTPUT]
            stderr = (proc.stderr or "")[:_MAX_OUTPUT]
            base["exit_code"] = proc.returncode
            decision = None
            if stdout.strip():
                try:
                    decision = json.loads(stdout)
                except json.JSONDecodeError:
                    decision = None
            if isinstance(decision, dict):
                word = str(decision.get("decision") or "allow").lower()
                base["allowed"] = word not in ("deny", "block", "reject")
                base["reason"] = str(decision.get("reason") or "")[:2000]
                base["additional_context"] = str(
                    decision.get("additionalContext") or decision.get("additional_context") or "")[:8000]
            elif proc.returncode != 0:
                base["allowed"] = event not in _BLOCKING
                base["reason"] = (stderr.strip() or stdout.strip()
                                  or "hook exited %d" % proc.returncode)[:2000]
        except subprocess.TimeoutExpired:
            base.update(allowed=event not in _BLOCKING, timed_out=True,
                        reason="hook timed out after %.1fs" % timeout)
        except Exception as exc:
            base.update(allowed=event not in _BLOCKING,
                        reason="hook failed: %s: %s" % (type(exc).__name__, exc))
        base["wall_ms"] = int((time.time() - started) * 1000)
        return base


def validate_config(path: str) -> list[str]:
    """Return human-readable configuration errors without executing a hook."""
    errors = []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return ["%s: %s" % (type(exc).__name__, exc)]
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return ["top-level 'hooks' must be an object"]
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            errors.append("%s must be an array" % event); continue
        for i, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list) or not handlers:
                errors.append("%s[%d].hooks must be a non-empty array" % (event, i)); continue
            for j, handler in enumerate(handlers):
                if not isinstance(handler, dict) or handler.get("type", "command") != "command":
                    errors.append("%s[%d].hooks[%d] supports type=command only" % (event, i, j))
                elif not str(handler.get("command") or "").strip():
                    errors.append("%s[%d].hooks[%d].command is required" % (event, i, j))
    return errors
