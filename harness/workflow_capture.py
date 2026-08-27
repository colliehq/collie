"""Record Collie workflows and turn them into reviewable, evaluated Skills.

This is intentionally not a macro recorder that blindly replays mouse clicks.  Structural run
events are captured, secrets are redacted before persistence, and replay initially produces a dry
run plan.  A generated Skill stays a draft until an explicit approval writes its exact bytes into a
normal Collie skill directory.  Evaluation is repeatable and stored beside the draft.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid

from .runner_specs import redact_text


_LOCK = threading.RLock()
_ID = re.compile(r"^wf_[0-9a-f]{24}$")
_EVENT_KINDS = {"start", "tool", "edit", "repro", "verification_evidence", "done",
                "browser", "desktop", "instruction", "check"}
_MAX_EVENTS = 500


def _root():
    value = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    return os.path.abspath(value)


def _path():
    return os.path.join(_root(), "workflow-capture.json")


def _text(value, limit=1000):
    return redact_text(str(value or "").replace("\x00", "")[:limit], limit)


def _slug(value):
    value = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    return (value or "recorded-workflow")[:64]


def _clean(value, depth=0):
    if depth > 4:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k)[:80]: _clean(v, depth + 1) for k, v in list(value.items())[:50]
                if str(k).lower() not in {"token", "authorization", "cookie", "secret", "password"}}
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(value)[:50]]
    if isinstance(value, (str, bytes)):
        return _text(value.decode("utf-8", "replace") if isinstance(value, bytes) else value, 2000)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _text(value, 500)


def _clean_event(kind, data):
    """Project a live event onto procedure fields; never persist answer/token payloads."""
    data = data if isinstance(data, dict) else {}
    if kind == "start":
        keys = ("intent", "workspace", "strategy", "provider", "model", "cwd")
    elif kind == "done":
        keys = ("error", "canceled", "verified", "turns", "tool_calls", "model")
    elif kind in {"verification_evidence", "check", "repro"}:
        keys = ("command", "check", "passed", "exit_code", "evidence", "path")
    elif kind in {"tool", "edit", "browser", "desktop"}:
        keys = ("name", "tool", "path", "file", "target", "command", "action", "summary", "args")
    else:
        keys = ("text", "summary", "path", "command")
    return _clean({key: data[key] for key in keys if key in data})


def _defaults():
    return {"version": 1, "active": {}, "workflows": {}}


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            return _defaults()
        value.setdefault("active", {}); value.setdefault("workflows", {})
        return value
    except FileNotFoundError:
        return _defaults()
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("workflow capture state is unreadable: %s" % exc)


def _write(value):
    path = _path(); os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
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
            os.remove(tmp)
        except FileNotFoundError:
            pass


def _public(row):
    return {key: row.get(key) for key in ("id", "name", "description", "session", "status",
            "created_at", "updated_at", "event_count", "version", "digest", "evaluation",
            "installed_path")}


def _step(event, index):
    kind = event.get("kind") or "instruction"
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if kind == "tool":
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        name = data.get("name") or data.get("tool") or "tool"
        target = (data.get("path") or data.get("target") or data.get("command") or
                  args.get("path") or args.get("file_path") or args.get("command") or
                  "the selected target")
        return "%d. Use `%s` on `%s`; inspect its result before continuing." % (
            index, _text(name, 80), _text(target, 180))
    if kind == "edit":
        return "%d. Apply the demonstrated edit to `%s`, preserving surrounding project conventions." % (
            index, _text(data.get("path") or data.get("file") or "the target file", 180))
    if kind in {"verification_evidence", "check", "repro"}:
        evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
        command = (data.get("command") or data.get("check") or evidence.get("command") or
                   "the demonstrated verification")
        return "%d. Run `%s` and require evidence of success." % (index, _text(command, 220))
    if kind in {"browser", "desktop"}:
        return "%d. Perform the demonstrated %s action: %s." % (
            index, kind, _text(data.get("action") or data.get("summary") or "continue", 240))
    if kind == "instruction":
        return "%d. %s" % (index, _text(data.get("text") or data.get("summary"), 400))
    return ""


def _draft(row):
    steps, seen = [], set()
    for event in row.get("events") or []:
        line = _step(event, len(steps) + 1)
        if line and line not in seen:
            seen.add(line); steps.append(line)
    if not steps:
        steps = ["1. Follow the demonstrated workflow and stop if required context is missing."]
    description = row.get("description") or ("Use when the user asks to repeat %s." % row["name"])
    checks = [x for x in steps if "require evidence" in x]
    body = ["---", "name: %s" % _slug(row["name"]),
            "description: %s" % json.dumps(_text(description, 400).replace("\n", " "),
                                             ensure_ascii=False), "---", "",
            "# %s" % _text(row["name"], 120), "", "## Inputs", "",
            "- Confirm the target project, account, and values that differ from the demonstration.",
            "- Never reuse credentials or irreversible approvals from the recording.", "",
            "## Workflow", ""] + steps + ["", "## Verification", ""]
    body += (checks or ["- Verify the requested outcome against the current target before reporting completion."])
    body += ["", "## Safety", "",
             "- Treat this Skill as procedure, not authority; normal Collie permission gates still apply.",
             "- Pause when the current UI or project differs materially from the demonstration.", ""]
    return "\n".join(body)


class WorkflowStore:
    def start(self, name, *, description="", session=""):
        name = _text(name, 120).strip()
        if not name:
            raise ValueError("workflow name is required")
        workflow_id = "wf_" + uuid.uuid4().hex[:24]
        now = time.time()
        row = {"id": workflow_id, "name": name, "description": _text(description, 1000),
               "session": _text(session, 128), "status": "recording", "created_at": now,
               "updated_at": now, "events": [], "event_count": 0, "version": 1,
               "evaluation": {}, "digest": "", "installed_path": ""}
        with _LOCK:
            state = _load()
            if row["session"]:
                prior = state["active"].get(row["session"])
                if prior:
                    raise ValueError("this session is already recording workflow %s" % prior)
                state["active"][row["session"]] = workflow_id
            state["workflows"][workflow_id] = row; _write(state)
        return _public(row)

    def event(self, workflow_id, kind, data=None):
        if not _ID.fullmatch(str(workflow_id or "")):
            raise ValueError("invalid workflow id")
        kind = str(kind or "").strip().lower()
        if kind not in _EVENT_KINDS:
            raise ValueError("unsupported workflow event kind")
        if kind == "token":
            return None
        with _LOCK:
            state = _load(); row = state["workflows"].get(workflow_id)
            if not row or row.get("status") != "recording":
                raise ValueError("workflow is not recording")
            events = list(row.get("events") or [])
            if len(events) >= _MAX_EVENTS:
                raise ValueError("workflow recording reached its event limit")
            events.append({"kind": kind, "at": time.time(), "data": _clean_event(kind, data or {})})
            row["events"] = events; row["event_count"] = len(events); row["updated_at"] = time.time()
            _write(state)
            return {"ok": True, "event_count": len(events)}

    def capture_session_event(self, session, kind, data=None):
        if kind == "token":
            return False
        with _LOCK:
            state = _load(); workflow_id = state["active"].get(str(session or ""))
        if not workflow_id:
            return False
        try:
            self.event(workflow_id, kind, data)
            return True
        except ValueError:
            return False

    def stop(self, workflow_id):
        if not _ID.fullmatch(str(workflow_id or "")):
            raise ValueError("invalid workflow id")
        with _LOCK:
            state = _load(); row = state["workflows"].get(workflow_id)
            if not row:
                raise ValueError("workflow not found")
            if row.get("status") == "recording":
                row["status"] = "draft"; row["updated_at"] = time.time()
                if row.get("session"):
                    state["active"].pop(row["session"], None)
            row["draft"] = _draft(row)
            row["digest"] = hashlib.sha256(row["draft"].encode("utf-8")).hexdigest()
            _write(state)
            return dict(_public(row), draft=row["draft"])

    def evaluate(self, workflow_id, cases=None):
        cases = cases if isinstance(cases, list) else []
        with _LOCK:
            state = _load(); row = state["workflows"].get(str(workflow_id or ""))
            if not row:
                raise ValueError("workflow not found")
            required = [e["kind"] for e in row.get("events") or []
                        if e.get("kind") not in {"start", "done"}]
            required = list(dict.fromkeys(required))
            results = []
            for index, case in enumerate(cases[:50]):
                case = case if isinstance(case, dict) else {}
                observed = {str(x) for x in (case.get("event_kinds") or [])}
                missing = [kind for kind in required if kind not in observed]
                explicit = case.get("passed")
                passed = (bool(explicit) and not missing) if isinstance(explicit, bool) else not missing
                results.append({"name": _text(case.get("name") or "case-%d" % (index + 1), 100),
                                "passed": passed, "missing": missing})
            has_verifier = any(kind in required for kind in ("verification_evidence", "check", "repro"))
            evaluation = {"at": time.time(), "cases": results,
                          "passed": sum(1 for x in results if x["passed"]), "total": len(results),
                          "structural_ready": bool(required), "has_verifier": has_verifier,
                          "eligible": bool(required and has_verifier and results and
                                           all(x["passed"] for x in results))}
            row["evaluation"] = evaluation; row["updated_at"] = time.time(); _write(state)
            return evaluation

    def replay_plan(self, workflow_id, variables=None):
        with _LOCK:
            row = _load()["workflows"].get(str(workflow_id or ""))
            if not row:
                raise ValueError("workflow not found")
            return {"workflow": _public(row), "dry_run": True,
                    "variables": _clean(variables or {}),
                    "events": list(row.get("events") or []),
                    "warning": "review this plan; replay does not inherit recorded approvals"}

    def approve(self, workflow_id, *, cwd, confirm=False):
        if confirm is not True:
            raise ValueError("explicit confirm=true is required")
        with _LOCK:
            state = _load(); row = state["workflows"].get(str(workflow_id or ""))
            if not row:
                raise ValueError("workflow not found")
            evaluation = row.get("evaluation") or {}
            if not evaluation.get("eligible"):
                raise ValueError("workflow must pass at least one replay evaluation with verification")
            draft = row.get("draft") or _draft(row)
            digest = hashlib.sha256(draft.encode("utf-8")).hexdigest()
            if row.get("digest") and row["digest"] != digest:
                raise ValueError("workflow draft changed after review")
            base = os.path.abspath(os.path.join(cwd, ".collie", "skills"))
            target = os.path.abspath(os.path.join(base, _slug(row["name"])))
            if os.path.commonpath([base, target]) != base:
                raise ValueError("invalid skill target")
            os.makedirs(target, exist_ok=True)
            skill_path = os.path.join(target, "SKILL.md")
            if os.path.exists(skill_path):
                with open(skill_path, encoding="utf-8") as handle:
                    if hashlib.sha256(handle.read().encode("utf-8")).hexdigest() != digest:
                        raise ValueError("a different skill already exists at the target path")
            tmp = skill_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(draft); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp, skill_path)
            row.update(status="approved", digest=digest, installed_path=skill_path,
                       updated_at=time.time())
            _write(state)
            return _public(row)

    def list(self):
        with _LOCK:
            rows = [_public(x) for x in _load()["workflows"].values()]
        return sorted(rows, key=lambda x: float(x.get("updated_at") or 0), reverse=True)

    def get(self, workflow_id):
        with _LOCK:
            row = _load()["workflows"].get(str(workflow_id or ""))
            if not row:
                raise ValueError("workflow not found")
            return dict(_public(row), draft=row.get("draft") or "", events=row.get("events") or [])
