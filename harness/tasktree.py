"""Durable background run tree and scoped-specialist mailbox.

This is deliberately a backend primitive, not a UI or an agent implementation.
Web/CLI/Harness code can create runs, provision worktrees, claim leases, stream
progress, steer/cancel, and dispatch notifications without keeping correctness in
one process.  Specialist authority is always a deterministic subset of its parent.
"""
from __future__ import annotations

import fnmatch
import json
import os
import secrets
import sqlite3
import threading
import time


QUEUED = "queued"
RUNNING = "running"
BLOCKED = "blocked"
WAITING = "waiting"
NEEDS_YOU = "needs_you"
PAUSED = "paused"
COMPLETED = "completed"
FAILED = "failed"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"
RECOVERY_REQUIRED = "recovery_required"
WORKSPACE_REQUIRED = "workspace_required"
_TERMINAL = {COMPLETED, FAILED, CANCELLED}


def _js(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False,
                      sort_keys=True, default=str)


def _jl(value, default=None):
    try:
        return json.loads(value) if value else ({} if default is None else default)
    except (TypeError, ValueError):
        return {} if default is None else default


def _covered_capability(name, parent_patterns):
    return any(fnmatch.fnmatchcase(str(name), str(pattern))
               for pattern in (parent_patterns or ()))


def narrow_leash(parent, requested=None):
    """Return a child leash or raise when any requested authority expands parent."""
    parent = dict(parent or {})
    if requested is None:
        child = dict(parent)
        # A specialist gets an isolated filesystem even when the interactive
        # parent was allowed to use cwd. Equal tool/budget limits are ceilings;
        # cumulative usage is charged through every ancestor below.
        if child.get("workspace_mode") == "current":
            child["workspace_mode"] = "isolated"
        return child
    requested = dict(requested or {})
    child = dict(parent)
    unknown = set(requested) - set(parent)
    if unknown:
        raise ValueError("specialist leash introduces parent-unknown authority: %s" %
                         ", ".join(sorted(unknown)))

    if "may" in requested:
        may = requested.get("may")
        if not isinstance(may, (list, tuple)) or not all(
                isinstance(item, str) and _covered_capability(item, parent.get("may"))
                for item in may):
            raise ValueError("specialist capabilities must be covered by parent leash.may")
        child["may"] = sorted(set(may))

    numeric_caps = {
        "spend_max_usd", "max_total_steps", "max_irreversible_actions",
        "actions_per_hour", "max_model_tokens", "max_model_cost_usd",
        "max_active_wall_seconds", "max_elapsed_seconds", "max_step_seconds",
        "max_retries", "max_storage_bytes", "checkpoint_keep",
        "human_escalate_seconds", "human_timeout_seconds", "max_specialists",
        "max_specialist_depth",
    }
    for key, value in requested.items():
        if key == "may":
            continue
        if key in numeric_caps:
            try:
                if float(value) > float(parent[key]):
                    raise ValueError("specialist %s cannot exceed parent (%s > %s)" %
                                     (key, value, parent[key]))
            except (TypeError, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("specialist"):
                    raise
                raise ValueError("specialist %s must be numeric" % key)
            child[key] = value
            continue
        if key == "allowed_domains":
            domains = value or []
            parent_domains = parent.get(key) or []
            if not all(any(fnmatch.fnmatchcase(str(domain), str(pattern))
                           for pattern in parent_domains) for domain in domains):
                raise ValueError("specialist domains must be covered by parent domains")
            child[key] = list(domains)
            continue
        if key == "irreversible":
            order = {"deny": 0, "confirm": 1, "allow": 2}
            if value not in order or order[value] > order.get(parent.get(key, "confirm"), 1):
                raise ValueError("specialist irreversible authority expands parent")
            child[key] = value
            continue
        if key == "workspace_mode":
            # isolated is a restriction of current; the inverse expands scope.
            if value not in ("current", "isolated") or (
                    parent.get(key) == "isolated" and value != "isolated"):
                raise ValueError("specialist workspace mode expands parent")
            child[key] = value
            continue
        if key == "expires":
            if parent.get(key) and str(value) > str(parent[key]):
                raise ValueError("specialist expiry cannot outlive parent")
            child[key] = value
            continue
        if value != parent.get(key):
            raise ValueError("specialist field %s must equal parent" % key)
    return child


def _normalize_resource(item):
    if isinstance(item, str):
        if ":" not in item:
            raise ValueError("resource strings use kind:id")
        kind, ident = item.split(":", 1)
        item = {"kind": kind, "id": ident, "mode": "write"}
    if not isinstance(item, dict):
        raise ValueError("resource must be a mapping")
    kind = str(item.get("kind") or "").strip().lower()
    ident = str(item.get("id") or item.get("path") or "").strip()
    mode = str(item.get("mode") or "write").strip().lower()
    if not kind or not ident or mode not in ("read", "write"):
        raise ValueError("resource needs kind/id and read|write mode")
    if kind == "file":
        ident = os.path.normcase(os.path.realpath(os.path.abspath(ident)))
    return {"kind": kind, "id": ident, "mode": mode}


def normalize_resources(resources):
    out = []
    seen = set()
    for item in resources or ():
        resource = _normalize_resource(item)
        key = (resource["kind"], resource["id"], resource["mode"])
        if key not in seen:
            out.append(resource)
            seen.add(key)
    return out


def _resource_contains(parent, child):
    if parent["kind"] != child["kind"]:
        return False
    if parent["mode"] == "read" and child["mode"] == "write":
        return False
    if parent["kind"] != "file":
        return parent["id"] == child["id"]
    try:
        return os.path.commonpath([parent["id"], child["id"]]) == parent["id"]
    except ValueError:
        return False


class TaskTreeStore:
    """SQLite run tree with durable progress, mailbox and notification outbox."""

    def __init__(self, path=None, hooks=None):
        path = path or os.path.join(
            os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie"),
            "tasktree.db")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.hooks = hooks
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_runs(
            run_id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL DEFAULT '',
            root_run_id TEXT NOT NULL, mission_id TEXT NOT NULL DEFAULT '',
            depth INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'general',
            task TEXT NOT NULL, status TEXT NOT NULL, background INTEGER NOT NULL DEFAULT 0,
            leash_json TEXT NOT NULL, resources_json TEXT NOT NULL DEFAULT '[]',
            workspace_mode TEXT NOT NULL DEFAULT 'worktree', workspace TEXT NOT NULL DEFAULT '',
            owns_workspace INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '',
            owner_token TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0,
            progress_seq INTEGER NOT NULL DEFAULT 0, progress_at INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            model_cost_microusd INTEGER NOT NULL DEFAULT 0,
            active_wall_ms INTEGER NOT NULL DEFAULT 0, retry_count INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            cancel_ack_at INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS agent_runs_parent ON agent_runs(parent_run_id,created_at);
        CREATE TABLE IF NOT EXISTS agent_events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS agent_events_run ON agent_events(run_id,event_id);
        CREATE TABLE IF NOT EXISTS agent_mailbox(
            message_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            sender_run_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'queued',
            created_at INTEGER NOT NULL, delivered_at INTEGER NOT NULL DEFAULT 0,
            acked_at INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS agent_mailbox_run ON agent_mailbox(run_id,state,message_id);
        CREATE TABLE IF NOT EXISTS agent_notifications(
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'queued', created_at INTEGER NOT NULL,
            acked_at INTEGER NOT NULL DEFAULT 0);
        """)
        self.db.commit()

    def _hook(self, event, payload, subject=""):
        if self.hooks is None:
            return None
        try:
            return self.hooks.dispatch(event, payload, subject=subject)
        except Exception as exc:
            run_id = str((payload or {}).get("run_id") or "")
            if run_id:
                with self.lock:
                    self._event_locked(run_id, "hook_error",
                                       {"event": event, "error": "%s: %s" %
                                        (type(exc).__name__, exc)})
                    self.db.commit()
            return None

    def _event_locked(self, run_id, kind, payload=None, now=None):
        self.db.execute(
            "INSERT INTO agent_events(run_id,kind,payload_json,at) VALUES(?,?,?,?)",
            (run_id, kind, _js(payload or {}), int(now if now is not None else time.time())))

    def _notify_locked(self, run_id, kind, payload=None, now=None):
        self.db.execute(
            "INSERT INTO agent_notifications(run_id,kind,payload_json,state,created_at) "
            "VALUES(?,?,?,'queued',?)",
            (run_id, kind, _js(payload or {}), int(now if now is not None else time.time())))

    def _row(self, run_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row):
        out = dict(row)
        out["leash"] = _jl(out.pop("leash_json"))
        out["resources"] = _jl(out.pop("resources_json"), [])
        out["background"] = bool(out["background"])
        out["owns_workspace"] = bool(out["owns_workspace"])
        out["cancel_requested"] = bool(out["cancel_requested"])
        out["model_cost_usd"] = out["model_cost_microusd"] / 1_000_000.0
        return out

    def create_root(self, task, leash, resources, *, run_id=None, mission_id="",
                    workspace="", workspace_mode="worktree"):
        run_id = run_id or "run_" + secrets.token_hex(8)
        now = int(time.time())
        resources = normalize_resources(resources)
        workspace = os.path.realpath(os.path.abspath(workspace)) if workspace else ""
        status = QUEUED if workspace or workspace_mode != "worktree" else WORKSPACE_REQUIRED
        with self.lock:
            self.db.execute(
                "INSERT INTO agent_runs(run_id,parent_run_id,root_run_id,mission_id,depth,role,"
                "task,status,leash_json,resources_json,workspace_mode,workspace,created_at,updated_at,"
                "progress_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, "", run_id, mission_id, 0, "orchestrator", str(task)[:4000], status,
                 _js(leash or {}), _js(resources), workspace_mode, workspace, now, now, now))
            self._event_locked(run_id, "created", {"status": status}, now)
            self.db.commit()
        run = self.get(run_id)
        self._hook("TaskCreated", {"run_id": run_id, "parent_run_id": "",
                                    "task": run["task"], "role": run["role"],
                                    "resources": run["resources"]}, subject=run["role"])
        return run

    def get(self, run_id):
        return self._row(run_id)

    def spawn_specialist(self, parent_run_id, role, task, *, leash=None, resources=None,
                         run_id=None, workspace="", workspace_mode="worktree"):
        parent = self.get(parent_run_id)
        if not parent or parent["status"] in _TERMINAL:
            raise ValueError("specialist parent is missing or terminal")
        max_depth = int(parent["leash"].get("max_specialist_depth", 2))
        if parent["depth"] + 1 > max_depth:
            raise ValueError("specialist depth exceeds parent leash")
        child_leash = narrow_leash(parent["leash"], leash)
        child_resources = normalize_resources(parent["resources"] if resources is None else resources)
        for resource in child_resources:
            if not any(_resource_contains(owned, resource) for owned in parent["resources"]):
                raise ValueError("specialist resource expands parent ownership: %s:%s" %
                                 (resource["kind"], resource["id"]))
        run_id = run_id or "run_" + secrets.token_hex(8)
        now = int(time.time())
        workspace = os.path.realpath(os.path.abspath(workspace)) if workspace else ""
        status = QUEUED if workspace or workspace_mode != "worktree" else WORKSPACE_REQUIRED
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            count = self.db.execute(
                "SELECT COUNT(*) n FROM agent_runs WHERE parent_run_id=? AND status NOT IN (?,?,?)",
                (parent_run_id, COMPLETED, FAILED, CANCELLED)).fetchone()["n"]
            if count >= int(parent["leash"].get("max_specialists", 4)):
                self.db.rollback()
                raise ValueError("parent specialist concurrency budget exhausted")
            # Siblings may read the same scope; write ownership is exclusive. The
            # parent can query can_access() and must stop touching delegated files.
            siblings = self.db.execute(
                "SELECT resources_json FROM agent_runs WHERE parent_run_id=? "
                "AND status NOT IN (?,?,?)", (parent_run_id, COMPLETED, FAILED, CANCELLED)).fetchall()
            for sibling in siblings:
                for old in _jl(sibling["resources_json"], []):
                    for new in child_resources:
                        overlap = _resource_contains(old, new) or _resource_contains(new, old)
                        if overlap and "write" in (old["mode"], new["mode"]):
                            self.db.rollback()
                            raise ValueError("specialist write resource already owned: %s:%s" %
                                             (new["kind"], new["id"]))
            self.db.execute(
                "INSERT INTO agent_runs(run_id,parent_run_id,root_run_id,mission_id,depth,role,"
                "task,status,leash_json,resources_json,workspace_mode,workspace,created_at,updated_at,"
                "progress_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, parent_run_id, parent["root_run_id"], "",
                 parent["depth"] + 1, str(role or "specialist")[:80], str(task)[:4000], status,
                 _js(child_leash), _js(child_resources), workspace_mode, workspace, now, now, now))
            self._event_locked(run_id, "created", {"parent_run_id": parent_run_id,
                                                     "role": role, "status": status}, now)
            self._event_locked(parent_run_id, "child_created", {"run_id": run_id,
                                                                  "role": role}, now)
            self.db.commit()
        child = self.get(run_id)
        self._hook("TaskCreated", {"run_id": run_id, "parent_run_id": parent_run_id,
                                    "task": child["task"], "role": child["role"],
                                    "resources": child["resources"]}, subject=child["role"])
        return child

    def bind_workspace(self, run_id, path, *, owns_workspace=False):
        canonical = os.path.realpath(os.path.abspath(str(path or "")))
        if not path or not os.path.isdir(canonical):
            raise ValueError("provisioned worktree does not exist")
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET workspace=?,owns_workspace=?,status=CASE WHEN status=? "
                "THEN ? ELSE status END,updated_at=? WHERE run_id=? AND status NOT IN (?,?,?) "
                "AND owner_token=''",
                (canonical, int(bool(owns_workspace)), WORKSPACE_REQUIRED, QUEUED, now, run_id,
                 COMPLETED, FAILED, CANCELLED))
            if cur.rowcount:
                self._event_locked(run_id, "workspace_bound",
                                   {"workspace": canonical, "owned": bool(owns_workspace)}, now)
            self.db.commit()
        return self.get(run_id) if cur.rowcount else None

    def bind_mission(self, run_id, mission_id):
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET mission_id=?,updated_at=? WHERE run_id=? "
                "AND status NOT IN (?,?,?) AND owner_token=''",
                (str(mission_id)[:100], int(time.time()), run_id,
                 COMPLETED, FAILED, CANCELLED))
            self.db.commit()
        return cur.rowcount == 1

    def provision_worktree(self, run_id, parent_cwd, *, prepare_fn=None):
        """Provision the default isolated checkout and bind it to the durable run.

        Cleanup is intentionally not automatic: a worktree may hold the user's
        completed changes and ``worktree.release`` already refuses to remove such
        work.  Callers own review/merge/release as an explicit later workflow.
        """
        run = self.get(run_id)
        if not run or run["workspace_mode"] != "worktree" or run["workspace"]:
            return {"ok": False, "error": "run does not need a worktree", "run": run}
        if prepare_fn is None:
            from .worktree import prepare as prepare_fn
        prepared = prepare_fn(parent_cwd, run_id, "%s-%s" % (run["role"], run_id[-6:]))
        if not prepared.get("ok") or prepared.get("kind") != "worktree":
            return {**prepared, "run": self.get(run_id)}
        bound = self.bind_workspace(run_id, prepared["dir"], owns_workspace=True)
        return {**prepared, "run": bound}

    def claim(self, run_id, lease_s=300):
        token, now = secrets.token_hex(16), int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,owner_token=?,lease_until=?,updated_at=? "
                "WHERE run_id=? AND status=? AND owner_token='' AND cancel_requested=0",
                (RUNNING, token, now + int(lease_s), now, run_id, QUEUED))
            if cur.rowcount:
                self._event_locked(run_id, "claimed", {"lease_until": now + int(lease_s)}, now)
            self.db.commit()
        return token if cur.rowcount else None

    def renew(self, run_id, token, lease_s=300):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET lease_until=?,updated_at=? WHERE run_id=? "
                "AND status IN (?,?) AND owner_token=?",
                (now + int(lease_s), now, run_id, RUNNING, CANCEL_REQUESTED, token))
            self.db.commit()
        return cur.rowcount == 1

    def progress(self, run_id, token, summary, *, percent=None, detail=None):
        now = int(time.time())
        payload = {"summary": str(summary)[:1000]}
        if percent is not None:
            payload["percent"] = max(0, min(100, float(percent)))
        if detail is not None:
            payload["detail"] = detail
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET progress_seq=progress_seq+1,progress_at=?,updated_at=? "
                "WHERE run_id=? AND status IN (?,?) AND owner_token=?",
                (now, now, run_id, RUNNING, CANCEL_REQUESTED, token))
            if cur.rowcount:
                self._event_locked(run_id, "progress", payload, now)
            self.db.commit()
        return cur.rowcount == 1

    def set_background(self, run_id, background=True, token=""):
        with self.lock:
            suffix, args = "", [int(bool(background)), int(time.time()), run_id]
            if token:
                suffix = " AND owner_token=?"
                args.append(token)
            cur = self.db.execute(
                "UPDATE agent_runs SET background=?,updated_at=? WHERE run_id=? "
                "AND status NOT IN (?,?,?)" + suffix,
                (*args[:3], COMPLETED, FAILED, CANCELLED, *args[3:]))
            self.db.commit()
        return cur.rowcount == 1

    def _stop_owned(self, run_id, token, state, result, notify):
        if state in (COMPLETED, FAILED):
            hook = self._hook(
                "TaskCompleted", {"run_id": run_id, "state": state,
                                  "result": str(result or "")[:4000]}, subject=state)
            if (state == COMPLETED and hook is not None and
                    not getattr(hook, "allowed", True)):
                with self.lock:
                    self._event_locked(
                        run_id, "completion_hook_blocked",
                        {"reason": getattr(hook, "reason", "") or "policy check did not pass"})
                    self.db.commit()
                return False
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,updated_at=? "
                "WHERE run_id=? AND status=? AND owner_token=?",
                (state, str(result or "")[:4000], now, run_id, RUNNING, token))
            if cur.rowcount:
                self._event_locked(run_id, state, {"result": str(result or "")[:1000]}, now)
                if notify:
                    self._notify_locked(run_id, notify,
                                        {"state": state, "result": str(result or "")[:1000]}, now)
                row = self.db.execute(
                    "SELECT parent_run_id FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
                if row and row["parent_run_id"] and state in _TERMINAL:
                    self.db.execute(
                        "INSERT INTO agent_mailbox(run_id,sender_run_id,kind,payload_json,state,created_at) "
                        "VALUES(?,?, 'child_result',?,'queued',?)",
                        (row["parent_run_id"], run_id,
                         _js({"run_id": run_id, "state": state,
                              "result": str(result or "")[:4000]}), now))
            self.db.commit()
        if cur.rowcount and notify:
            self._hook("Notification", {"run_id": run_id, "kind": notify,
                                         "state": state}, subject=notify)
        return cur.rowcount == 1

    def block(self, run_id, token, reason, *, needs_you=False):
        return self._stop_owned(run_id, token, NEEDS_YOU if needs_you else BLOCKED,
                                reason, "needs_you" if needs_you else "blocked")

    def complete(self, run_id, token, result=""):
        return self._stop_owned(run_id, token, COMPLETED, result, "completed")

    def fail(self, run_id, token, result=""):
        return self._stop_owned(run_id, token, FAILED, result, "failed")

    def cancel_owned(self, run_id, token, result="cancelled"):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,"
                "cancel_requested=1,cancel_ack_at=?,updated_at=? WHERE run_id=? "
                "AND status IN (?,?) AND owner_token=?",
                (CANCELLED, str(result)[:4000], now, now, run_id,
                 RUNNING, CANCEL_REQUESTED, token))
            if cur.rowcount:
                self._event_locked(run_id, "cancel_acknowledged", {}, now)
                self._notify_locked(run_id, "cancelled", {"acknowledged": True}, now)
            self.db.commit()
        return cur.rowcount == 1

    def resume(self, run_id):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result='',updated_at=? WHERE run_id=? "
                "AND status IN (?,?,?) AND owner_token='' AND cancel_requested=0 "
                "AND (workspace_mode<>'worktree' OR workspace<>'')",
                (QUEUED, now, run_id, BLOCKED, NEEDS_YOU, PAUSED))
            if cur.rowcount:
                self._event_locked(run_id, "resumed", {}, now)
            self.db.commit()
        return cur.rowcount == 1

    def park_waiting(self, run_id, token, reason="waiting"):
        return self._stop_owned(run_id, token, WAITING, reason, "")

    def requeue_waiting(self, run_id):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result='',updated_at=? WHERE run_id=? "
                "AND status=? AND owner_token='' AND cancel_requested=0",
                (QUEUED, now, run_id, WAITING))
            if cur.rowcount:
                self._event_locked(run_id, "wake_due", {}, now)
            self.db.commit()
        return cur.rowcount == 1

    def mark_recovery(self, run_id, token, reason):
        return self._stop_owned(run_id, token, RECOVERY_REQUIRED, reason,
                                "recovery_required")

    def reconcile(self, run_id, note=""):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,updated_at=? WHERE run_id=? "
                "AND status=? AND owner_token='' AND cancel_requested=0",
                (QUEUED, str(note or "explicitly reconciled")[:4000], now,
                 run_id, RECOVERY_REQUIRED))
            if cur.rowcount:
                self._event_locked(run_id, "reconciled", {"note": note}, now)
            self.db.commit()
        return cur.rowcount == 1

    def steer(self, run_id, text, sender_run_id=""):
        if not str(text or "").strip():
            raise ValueError("steer text is empty")
        now = int(time.time())
        with self.lock:
            row = self.db.execute("SELECT status FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["status"] in _TERMINAL:
                return None
            cur = self.db.execute(
                "INSERT INTO agent_mailbox(run_id,sender_run_id,kind,payload_json,state,created_at) "
                "VALUES(?,?,'steer',?,'queued',?)",
                (run_id, sender_run_id, _js({"text": str(text)[:4000]}), now))
            self._event_locked(run_id, "steer_queued", {"message_id": cur.lastrowid}, now)
            self.db.commit()
        return cur.lastrowid

    def request_cancel(self, run_id, sender_run_id=""):
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT status FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["status"] in _TERMINAL:
                self.db.commit()
                return False
            if row["status"] == CANCEL_REQUESTED:
                self.db.commit()
                return True
            if row["status"] == RUNNING:
                self.db.execute(
                    "UPDATE agent_runs SET status=?,cancel_requested=1,updated_at=? WHERE run_id=?",
                    (CANCEL_REQUESTED, now, run_id))
                self.db.execute(
                    "INSERT INTO agent_mailbox(run_id,sender_run_id,kind,payload_json,state,created_at) "
                    "VALUES(?,?,'cancel','{}','queued',?)", (run_id, sender_run_id, now))
                self._event_locked(run_id, "cancel_requested", {}, now)
            else:
                self.db.execute(
                    "UPDATE agent_runs SET status=?,cancel_requested=1,cancel_ack_at=?,updated_at=? "
                    "WHERE run_id=?", (CANCELLED, now, now, run_id))
                self._event_locked(run_id, "cancel_acknowledged", {"without_worker": True}, now)
                self._notify_locked(run_id, "cancelled", {"acknowledged": True}, now)
            self.db.commit()
        return True

    def claim_messages(self, run_id, token, limit=20):
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute(
                "SELECT 1 FROM agent_runs WHERE run_id=? AND owner_token=? "
                "AND status IN (?,?)", (run_id, token, RUNNING, CANCEL_REQUESTED)).fetchone()
            if not owner:
                self.db.rollback()
                return []
            rows = self.db.execute(
                "SELECT * FROM agent_mailbox WHERE run_id=? AND state='queued' "
                "ORDER BY message_id LIMIT ?", (run_id, max(1, int(limit)))).fetchall()
            ids = [row["message_id"] for row in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                self.db.execute(
                    "UPDATE agent_mailbox SET state='delivered',delivered_at=? "
                    "WHERE message_id IN (%s) AND state='queued'" % marks, (now, *ids))
            self.db.commit()
        return [{**dict(row), "payload": _jl(row["payload_json"])} for row in rows]

    def ack_message(self, run_id, token, message_id):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_mailbox SET state='acked',acked_at=? WHERE message_id=? "
                "AND run_id=? AND state='delivered' AND EXISTS (SELECT 1 FROM agent_runs "
                "WHERE run_id=? AND owner_token=? AND status IN (?,?))",
                (now, int(message_id), run_id, run_id, token, RUNNING, CANCEL_REQUESTED))
            if cur.rowcount:
                self._event_locked(run_id, "message_acknowledged", {"message_id": message_id}, now)
            self.db.commit()
        return cur.rowcount == 1

    def ack_cancel(self, run_id, token, result="cancelled by worker"):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,"
                "cancel_ack_at=?,updated_at=? WHERE run_id=? AND status=? AND owner_token=?",
                (CANCELLED, str(result)[:4000], now, now, run_id, CANCEL_REQUESTED, token))
            if cur.rowcount:
                self.db.execute(
                    "UPDATE agent_mailbox SET state='acked',acked_at=? WHERE run_id=? "
                    "AND kind='cancel' AND state IN ('queued','delivered')", (now, run_id))
                self._event_locked(run_id, "cancel_acknowledged", {}, now)
                self._notify_locked(run_id, "cancelled", {"acknowledged": True}, now)
            self.db.commit()
        return cur.rowcount == 1

    def account_usage(self, run_id, token, *, input_tokens=0, output_tokens=0,
                      cost_usd=0.0, wall_ms=0, retries=0):
        """Charge a specialist and every ancestor, preventing fan-out budget escape."""
        run = self.get(run_id)
        if not run:
            return []
        ancestry = []
        cursor = run
        while cursor:
            ancestry.append(cursor["run_id"])
            cursor = self.get(cursor["parent_run_id"]) if cursor["parent_run_id"] else None
        with self.lock:
            owner = self.db.execute(
                "SELECT 1 FROM agent_runs WHERE run_id=? AND owner_token=? "
                "AND status IN (?,?)", (run_id, token, RUNNING, CANCEL_REQUESTED)).fetchone()
            if not owner:
                return ["run ownership lost"]
            marks = ",".join("?" for _ in ancestry)
            self.db.execute(
                "UPDATE agent_runs SET input_tokens=input_tokens+?,output_tokens=output_tokens+?,"
                "model_cost_microusd=model_cost_microusd+?,active_wall_ms=active_wall_ms+?,"
                "retry_count=retry_count+? WHERE run_id IN (%s)" % marks,
                (max(0, int(input_tokens)), max(0, int(output_tokens)),
                 max(0, int(round(float(cost_usd) * 1_000_000))), max(0, int(wall_ms)),
                 max(0, int(retries)), *ancestry))
            self.db.commit()
        return [(rid, self.budget_reason(rid)) for rid in ancestry if self.budget_reason(rid)]

    def budget_reason(self, run_id):
        run = self.get(run_id)
        if not run:
            return "run missing"
        leash = run["leash"]
        checks = (
            (run["input_tokens"] + run["output_tokens"] >=
             int(leash.get("max_model_tokens", 2_000_000)), "model-token budget exhausted"),
            (run["model_cost_usd"] >= float(leash.get("max_model_cost_usd", 25)),
             "model-cost budget exhausted"),
            (run["active_wall_ms"] >= int(leash.get("max_active_wall_seconds", 21600)) * 1000,
             "active wall-time budget exhausted"),
            (run["retry_count"] >= int(leash.get("max_retries", 32)),
             "retry budget exhausted"),
            (int(time.time()) - int(run["created_at"]) >=
             int(leash.get("max_elapsed_seconds", 2_592_000)),
             "elapsed-time budget exhausted"),
            (self.storage_bytes(run_id) >= int(leash.get("max_storage_bytes", 5_000_000)),
             "durable-storage budget exhausted"),
        )
        return next((reason for hit, reason in checks if hit), "")

    def storage_bytes(self, run_id):
        with self.lock:
            row = self.db.execute(
                "SELECT LENGTH(CAST(task AS BLOB))+LENGTH(CAST(leash_json AS BLOB))+"
                "LENGTH(CAST(resources_json AS BLOB))+LENGTH(CAST(result AS BLOB)) n "
                "FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            events = self.db.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) n "
                "FROM agent_events WHERE run_id=?", (run_id,)).fetchone()
            mailbox = self.db.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) n "
                "FROM agent_mailbox WHERE run_id=? OR sender_run_id=?",
                (run_id, run_id)).fetchone()
        return int(row["n"] or 0) + int(events["n"] or 0) + int(mailbox["n"] or 0) \
            if row else 0

    def recover_stale(self, now=None):
        now = int(now if now is not None else time.time())
        with self.lock:
            rows = self.db.execute(
                "SELECT run_id FROM agent_runs WHERE status IN (?,?) AND owner_token<>'' "
                "AND lease_until>0 AND lease_until<=?", (RUNNING, CANCEL_REQUESTED, now)).fetchall()
            for row in rows:
                self.db.execute(
                    "UPDATE agent_runs SET status=?,owner_token='',lease_until=0,result=?,updated_at=? "
                    "WHERE run_id=? AND lease_until<=?",
                    (RECOVERY_REQUIRED,
                     "worker lease expired; inspect its worktree/resources before resume",
                     now, row["run_id"], now))
                self._event_locked(row["run_id"], "recovery_required", {}, now)
                self._notify_locked(row["run_id"], "recovery_required", {}, now)
            self.db.commit()
        return len(rows)

    def can_access(self, run_id, resource, mode="write"):
        """Check declared scope and active descendant ownership before a tool call."""
        run = self.get(run_id)
        if not run:
            return False, "run missing"
        wanted = _normalize_resource({**(resource if isinstance(resource, dict) else
                                         {"kind": "file", "id": resource}), "mode": mode})
        if not any(_resource_contains(owned, wanted) for owned in run["resources"]):
            return False, "resource is outside run ownership"
        if mode == "write":
            with self.lock:
                rows = self.db.execute(
                    "SELECT run_id,resources_json FROM agent_runs WHERE root_run_id=? "
                    "AND run_id<>? AND status NOT IN (?,?,?)",
                    (run["root_run_id"], run_id, COMPLETED, FAILED, CANCELLED)).fetchall()
            descendants = {child["run_id"] for child in self.tree(run_id)["flat"]
                           if child["run_id"] != run_id}
            for row in rows:
                if row["run_id"] not in descendants:
                    continue
                for delegated in _jl(row["resources_json"], []):
                    if delegated["mode"] == "write" and (
                            _resource_contains(delegated, wanted) or
                            _resource_contains(wanted, delegated)):
                        return False, "write ownership delegated to %s" % row["run_id"]
        return True, "owned"

    def events(self, run_id, limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT event_id,kind,payload_json,at FROM agent_events WHERE run_id=? "
                "ORDER BY event_id DESC LIMIT ?", (run_id, max(1, int(limit)))).fetchall()
        return [{"event_id": row["event_id"], "kind": row["kind"],
                 "payload": _jl(row["payload_json"]), "at": row["at"]}
                for row in reversed(rows)]

    def list_runs(self, status=None, *, specialists_only=False):
        where, args = [], []
        if status:
            states = (status,) if isinstance(status, str) else tuple(status)
            where.append("status IN (%s)" % ",".join("?" for _ in states))
            args.extend(states)
        if specialists_only:
            where.append("parent_run_id<>''")
        query = "SELECT * FROM agent_runs"
        if where:
            query += " WHERE " + " AND ".join(where)
        with self.lock:
            rows = self.db.execute(query + " ORDER BY created_at,run_id", args).fetchall()
        return [self._decode(row) for row in rows]

    def notifications(self, state="queued", limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM agent_notifications WHERE state=? "
                "ORDER BY notification_id LIMIT ?", (state, max(1, int(limit)))).fetchall()
        return [{**dict(row), "payload": _jl(row["payload_json"])} for row in rows]

    def ack_notification(self, notification_id):
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_notifications SET state='acked',acked_at=? "
                "WHERE notification_id=? AND state='queued'",
                (int(time.time()), int(notification_id)))
            self.db.commit()
        return cur.rowcount == 1

    def tree(self, run_id):
        root = self.get(run_id)
        if not root:
            return {"root": None, "flat": []}
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM agent_runs WHERE root_run_id=? ORDER BY depth,created_at,run_id",
                (root["root_run_id"],)).fetchall()
        decoded = [self._decode(row) for row in rows]
        wanted = {run_id}
        changed = True
        while changed:
            changed = False
            for row in decoded:
                if row["parent_run_id"] in wanted and row["run_id"] not in wanted:
                    wanted.add(row["run_id"])
                    changed = True
        flat = [row for row in decoded if row["run_id"] in wanted]
        return {"root": self.get(run_id), "flat": flat}

    def close(self):
        with self.lock:
            self.db.close()
