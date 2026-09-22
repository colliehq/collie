"""Optional Collie Online client and local-first synchronization store.

This module is deliberately stdlib-only. Local Collie never imports it merely to run
an agent. Connected surfaces opt in, and cryptographic device keys are created by the
separate enrollment flow on platforms where the ``online`` extra is installed.

The local database is authoritative for pending work. Sync is an idempotent cursor
protocol: push local operations, record server versions, then pull later operations.
Conflicts are preserved as siblings and surfaced for reconciliation; user content is
never silently overwritten by last-writer-wins.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class OnlineError(RuntimeError):
    pass


class AuthenticationRequired(OnlineError):
    pass


class SyncConflict(OnlineError):
    pass


class DataClass(str, Enum):
    CLOUD_INDEXED = "cloud_indexed"
    SEALED = "sealed"
    DEVICE_ONLY = "device_only"
    SECRET = "secret"


SYNCABLE_DATA_CLASSES = frozenset({DataClass.CLOUD_INDEXED, DataClass.SEALED})


def _state_dir(path: Optional[str] = None) -> str:
    root = os.path.abspath(os.path.expanduser(
        path or os.environ.get("COLLIE_STATE_DIR") or "~/.collie"))
    os.makedirs(root, exist_ok=True)
    return root


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_json(value: str, fallback):
    try:
        out = json.loads(value or "")
        return out
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    raw = str(value or "").encode()
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def _seal_key_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "online-seal-key.json")


def _credential_path(db_path: str) -> str:
    """Device-local session store, deliberately separate from the sync mirror.

    Keeping bearer/refresh tokens out of ``online.db`` means a copied mirror or a
    database support bundle cannot accidentally carry a reusable login.  The file is
    never part of sync and is written atomically with owner-only permissions where the
    platform exposes them.
    """
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "online-credentials.json")


def _read_credentials(db_path: str) -> dict:
    try:
        with open(_credential_path(db_path), encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict) and value.get("version") == 1:
            return {key: value.get(key) for key in (
                "access_token", "refresh_token", "access_expires_at", "refresh_expires_at")}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def _write_credentials(db_path: str, values: dict) -> None:
    path = _credential_path(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"version": 1,
               "access_token": str(values.get("access_token") or ""),
               "refresh_token": str(values.get("refresh_token") or ""),
               "access_expires_at": int(values.get("access_expires_at") or 0),
               "refresh_expires_at": int(values.get("refresh_expires_at") or 0)}
    tmp = path + ".%s.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
    try:
        from . import plat
        plat.chmod_private(tmp)
    except Exception:
        pass
    os.replace(tmp, path)


def _delete_credentials(db_path: str) -> None:
    try:
        os.remove(_credential_path(db_path))
    except FileNotFoundError:
        pass


def _load_seal_key(db_path: str, *, create: bool = False) -> bytes:
    path = _seal_key_path(db_path)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        key = _unb64(value.get("key") or "")
        if value.get("version") == 1 and len(key) == 32:
            return key
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    if not create:
        raise OnlineError("this device needs the Collie sealed-sync recovery key")
    key = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".%s.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "key": _b64(key), "created_at": int(time.time())}, handle)
    try:
        from . import plat
        plat.chmod_private(tmp)
    except Exception:
        pass
    os.replace(tmp, path)
    return key


def export_seal_key(db_path: Optional[str] = None) -> str:
    path = db_path or os.path.join(_state_dir(), "online.db")
    return "collie-seal-v1:" + _b64(_load_seal_key(path, create=True))


def import_seal_key(recovery_key: str, db_path: Optional[str] = None) -> None:
    prefix = "collie-seal-v1:"
    if not str(recovery_key or "").startswith(prefix):
        raise ValueError("invalid Collie sealed-sync recovery key")
    key = _unb64(str(recovery_key)[len(prefix):])
    if len(key) != 32:
        raise ValueError("invalid Collie sealed-sync recovery key")
    path = _seal_key_path(db_path or os.path.join(_state_dir(), "online.db"))
    if os.path.exists(path):
        existing = _load_seal_key(db_path or os.path.join(_state_dir(), "online.db"))
        if existing != key:
            raise ValueError("this device already has a different sealed-sync key")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".%s.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "key": _b64(key), "imported_at": int(time.time())}, handle)
    try:
        from . import plat
        plat.chmod_private(tmp)
    except Exception:
        pass
    os.replace(tmp, path)


def _seal_value(db_path: str, object_id: str, project_id: str, content: Any) -> dict:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise OnlineError("sealed sync needs: pip install 'collie-harness[online]'") from exc
    nonce = secrets.token_bytes(12)
    aad = ("collie-sealed-v1\0%s\0%s" % (project_id, object_id)).encode()
    clear = _json(content).encode("utf-8")
    ciphertext = AESGCM(_load_seal_key(db_path, create=True)).encrypt(nonce, clear, aad)
    return {"v": 1, "alg": "A256GCM", "nonce": _b64(nonce), "ciphertext": _b64(ciphertext)}


def _open_value(db_path: str, object_id: str, project_id: str, envelope: Any) -> Any:
    if not isinstance(envelope, dict) or envelope.get("v") != 1 or envelope.get("alg") != "A256GCM":
        raise OnlineError("sealed sync object is not a supported ciphertext envelope")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aad = ("collie-sealed-v1\0%s\0%s" % (project_id, object_id)).encode()
        clear = AESGCM(_load_seal_key(db_path)).decrypt(
            _unb64(envelope.get("nonce") or ""), _unb64(envelope.get("ciphertext") or ""), aad)
        return json.loads(clear.decode("utf-8"))
    except OnlineError:
        raise
    except Exception as exc:
        raise OnlineError("sealed sync object could not be decrypted on this device") from exc


@dataclass(frozen=True)
class OnlineProfile:
    base_url: str
    user_id: str
    workspace_id: str
    device_id: str
    device_name: str
    connected_at: int


@dataclass(frozen=True)
class SyncObject:
    object_id: str
    object_type: str
    project_id: str
    data_class: DataClass
    content: Any
    version: int
    updated_at: int
    tombstone: bool = False
    conflict_of: str = ""


class OnlineStore:
    """Durable local mirror, outbox, cursor, and non-secret account metadata."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(_state_dir(), "online.db")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
            self.db.execute("PRAGMA foreign_keys=ON")
        except sqlite3.OperationalError:
            pass
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS online_profile(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), base_url TEXT NOT NULL,
          user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, device_id TEXT NOT NULL,
          device_name TEXT NOT NULL, connected_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS online_tokens(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), access_token TEXT NOT NULL,
          refresh_token TEXT NOT NULL, access_expires_at INTEGER NOT NULL,
          refresh_expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sync_state(
          workspace_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
          last_sync_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sync_objects(
          object_id TEXT PRIMARY KEY, object_type TEXT NOT NULL, project_id TEXT NOT NULL,
          data_class TEXT NOT NULL, content_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL, tombstone INTEGER NOT NULL DEFAULT 0,
          dirty INTEGER NOT NULL DEFAULT 0, conflict_of TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS sync_objects_project ON sync_objects(project_id, object_type);
        CREATE TABLE IF NOT EXISTS sync_outbox(
          operation_id TEXT PRIMARY KEY, object_id TEXT NOT NULL, base_version INTEGER NOT NULL,
          payload_json TEXT NOT NULL, created_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS online_projects(
          project_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'owner', updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS online_connections(
          connection_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS online_catalog(
          kind TEXT NOT NULL, item_id TEXT NOT NULL, payload_json TEXT NOT NULL,
          updated_at INTEGER NOT NULL, PRIMARY KEY(kind,item_id));
        CREATE TABLE IF NOT EXISTS online_project_bindings(
          project_id TEXT PRIMARY KEY, local_project TEXT NOT NULL, memory_db TEXT NOT NULL,
          cwd TEXT NOT NULL DEFAULT '', memory_data_class TEXT NOT NULL DEFAULT 'sealed',
          updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS online_mission_authorizations(
          mission_id TEXT PRIMARY KEY, authorization_digest TEXT NOT NULL,
          accepted_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS online_connection_invocations(
          connection_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
          tool_name TEXT NOT NULL, args_hash TEXT NOT NULL, state TEXT NOT NULL,
          result_hash TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
          completed_at INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(connection_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS online_connection_credentials(
          connection_id TEXT PRIMARY KEY, envelope_json TEXT NOT NULL,
          updated_at INTEGER NOT NULL);
        """)
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def connected(self) -> bool:
        return self.profile() is not None

    def profile(self) -> Optional[OnlineProfile]:
        row = self.db.execute("SELECT * FROM online_profile WHERE singleton=1").fetchone()
        if row is None:
            return None
        return OnlineProfile(base_url=row["base_url"], user_id=row["user_id"],
                             workspace_id=row["workspace_id"], device_id=row["device_id"],
                             device_name=row["device_name"], connected_at=row["connected_at"])

    def connect(self, *, base_url: str, user_id: str, workspace_id: str, device_id: str,
                device_name: str, access_token: str, refresh_token: str,
                access_expires_at: int, refresh_expires_at: int) -> OnlineProfile:
        parsed = urllib.parse.urlsplit(str(base_url or "").rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc:
            # Local integration tests may explicitly opt into loopback HTTP.
            if not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
                    and os.environ.get("COLLIE_ONLINE_ALLOW_HTTP") == "1"):
                raise ValueError("Collie Online base URL must be HTTPS")
        values = [str(x or "").strip() for x in
                  (user_id, workspace_id, device_id, device_name, access_token, refresh_token)]
        if not all(values):
            raise ValueError("account, workspace, device, and token fields are required")
        now = int(time.time())
        with self._lock:
            self.db.execute("""INSERT INTO online_profile(singleton,base_url,user_id,workspace_id,
                device_id,device_name,connected_at) VALUES(1,?,?,?,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET base_url=excluded.base_url,
                user_id=excluded.user_id,workspace_id=excluded.workspace_id,
                device_id=excluded.device_id,device_name=excluded.device_name,
                connected_at=excluded.connected_at""",
                (str(base_url).rstrip("/"), values[0], values[1], values[2], values[3], now))
            _write_credentials(self.path, {
                "access_token": values[4], "refresh_token": values[5],
                "access_expires_at": int(access_expires_at),
                "refresh_expires_at": int(refresh_expires_at)})
            # The table remains only as a one-release migration source. New writes
            # never put credentials in the sync mirror database.
            self.db.execute("DELETE FROM online_tokens")
            self.db.execute("INSERT OR IGNORE INTO sync_state(workspace_id,cursor,last_sync_at) VALUES(?,0,0)",
                            (values[1],))
            self.db.commit()
        try:
            from . import plat
            plat.chmod_private(self.path)
        except Exception:
            pass
        return self.profile()

    def disconnect(self, *, forget_mirror: bool = False) -> None:
        with self._lock:
            self.db.execute("DELETE FROM online_tokens")
            _delete_credentials(self.path)
            self.db.execute("DELETE FROM online_profile")
            if forget_mirror:
                self.db.execute("DELETE FROM sync_state")
                self.db.execute("DELETE FROM sync_objects")
                self.db.execute("DELETE FROM sync_outbox")
                self.db.execute("DELETE FROM online_projects")
                self.db.execute("DELETE FROM online_connections")
                self.db.execute("DELETE FROM online_catalog")
                self.db.execute("DELETE FROM online_project_bindings")
                self.db.execute("DELETE FROM online_connection_credentials")
            self.db.commit()

    def tokens(self) -> dict:
        value = _read_credentials(self.path)
        if value:
            return value
        # Migrate an older Connected Mode install without forcing the user through
        # device pairing again, then erase the legacy plaintext row.
        with self._lock:
            row = self.db.execute("SELECT * FROM online_tokens WHERE singleton=1").fetchone()
            if row is None:
                return {}
            value = dict(row)
            _write_credentials(self.path, value)
            self.db.execute("DELETE FROM online_tokens")
            self.db.commit()
            return {key: value[key] for key in (
                "access_token", "refresh_token", "access_expires_at", "refresh_expires_at")}

    def update_tokens(self, access_token: str, refresh_token: str,
                      access_expires_at: int, refresh_expires_at: int) -> None:
        if not self.connected():
            raise AuthenticationRequired("Collie is in Local mode")
        with self._lock:
            _write_credentials(self.path, {
                "access_token": str(access_token), "refresh_token": str(refresh_token),
                "access_expires_at": int(access_expires_at),
                "refresh_expires_at": int(refresh_expires_at)})

    def switch_workspace(self, workspace_id: str, *, access_token: str, refresh_token: str,
                         access_expires_at: int, refresh_expires_at: int) -> OnlineProfile:
        """Move this device session to an already-authorized workspace.

        Workspace-scoped public caches are cleared before the next sync. Durable
        object mirrors and cursors stay partitioned by their globally unique project
        and workspace ids, so switching never destroys offline state.
        """
        profile = self.profile()
        workspace_id = str(workspace_id or "").strip()
        if profile is None:
            raise AuthenticationRequired("Sign in to Collie Online first")
        if not workspace_id:
            raise ValueError("workspace id is required")
        with self._lock:
            self.db.execute("UPDATE online_profile SET workspace_id=? WHERE singleton=1",
                            (workspace_id,))
            self.db.execute("INSERT OR IGNORE INTO sync_state(workspace_id,cursor,last_sync_at) VALUES(?,0,0)",
                            (workspace_id,))
            self.db.execute("DELETE FROM online_projects")
            self.db.execute("DELETE FROM online_connections")
            self.db.execute("DELETE FROM online_catalog")
            self.db.commit()
            _write_credentials(self.path, {
                "access_token": access_token, "refresh_token": refresh_token,
                "access_expires_at": int(access_expires_at),
                "refresh_expires_at": int(refresh_expires_at)})
        return self.profile()

    def upsert_project(self, project_id: str, name: str, *, role: str = "owner",
                       workspace_id: str = "") -> dict:
        profile = self.profile()
        workspace_id = workspace_id or (profile.workspace_id if profile else "local")
        project_id, name = str(project_id or "").strip(), str(name or "").strip()
        if not project_id or not name:
            raise ValueError("project id and name are required")
        now = int(time.time())
        with self._lock:
            self.db.execute("""INSERT INTO online_projects(project_id,workspace_id,name,role,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET name=excluded.name,
                role=excluded.role,updated_at=excluded.updated_at""",
                (project_id, workspace_id, name[:200], str(role or "member")[:40], now))
            self.db.commit()
        return {"project_id": project_id, "workspace_id": workspace_id,
                "name": name[:200], "role": str(role or "member")[:40], "updated_at": now}

    def projects(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM online_projects ORDER BY updated_at DESC").fetchall()]

    def cache_projects(self, rows: list[dict]) -> None:
        profile = self.profile()
        workspace_id = profile.workspace_id if profile else ""
        now = int(time.time())
        with self._lock:
            seen = set()
            for row in rows or []:
                project_id = str(row.get("id") or row.get("project_id") or "")
                name = str(row.get("name") or "")
                if not project_id or not name:
                    continue
                seen.add(project_id)
                self.db.execute("""INSERT INTO online_projects(project_id,workspace_id,name,role,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                    workspace_id=excluded.workspace_id,name=excluded.name,role=excluded.role,
                    updated_at=excluded.updated_at""",
                    (project_id, workspace_id, name[:200], str(row.get("role") or "member")[:40],
                     int(row.get("updated_at") or now)))
            if seen:
                marks = ",".join("?" for _ in seen)
                self.db.execute("DELETE FROM online_projects WHERE project_id NOT IN (%s)" % marks,
                                tuple(sorted(seen)))
            else:
                self.db.execute("DELETE FROM online_projects")
            self.db.commit()

    def cache_connections(self, rows: list[dict]) -> None:
        now = int(time.time())
        with self._lock:
            seen = set()
            for row in rows or []:
                cid = str(row.get("id") or "")
                if not cid:
                    continue
                seen.add(cid)
                self.db.execute("""INSERT INTO online_connections(connection_id,payload_json,updated_at)
                    VALUES(?,?,?) ON CONFLICT(connection_id) DO UPDATE SET
                    payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                    (cid, _json(row), now))
            if seen:
                marks = ",".join("?" for _ in seen)
                self.db.execute("DELETE FROM online_connections WHERE connection_id NOT IN (%s)" % marks,
                                tuple(sorted(seen)))
            else:
                self.db.execute("DELETE FROM online_connections")
            self.db.commit()

    def connections(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload_json FROM online_connections ORDER BY updated_at DESC").fetchall()
        return [_decode_json(row["payload_json"], {}) for row in rows]

    def bind_project(self, project_id: str, local_project: str, memory_db: str, *,
                     cwd: str = "", memory_data_class: DataClass = DataClass.SEALED) -> dict:
        data_class = DataClass(memory_data_class)
        if data_class not in SYNCABLE_DATA_CLASSES:
            raise ValueError("project memory must be cloud-indexed or sealed")
        project_id, local_project = str(project_id or "").strip(), str(local_project or "").strip()
        if not project_id or not local_project or not memory_db:
            raise ValueError("remote project, local project, and memory database are required")
        now = int(time.time())
        with self._lock:
            self.db.execute("""INSERT INTO online_project_bindings(project_id,local_project,memory_db,
                cwd,memory_data_class,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE
                SET local_project=excluded.local_project,memory_db=excluded.memory_db,cwd=excluded.cwd,
                memory_data_class=excluded.memory_data_class,updated_at=excluded.updated_at""",
                (project_id, local_project, os.path.abspath(memory_db), os.path.abspath(cwd) if cwd else "",
                 data_class.value, now))
            self.db.commit()
        return {"project_id": project_id, "local_project": local_project,
                "memory_db": os.path.abspath(memory_db), "cwd": os.path.abspath(cwd) if cwd else "",
                "memory_data_class": data_class.value, "updated_at": now}

    def project_bindings(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM online_project_bindings ORDER BY updated_at DESC").fetchall()]

    def accept_mission_authorization(self, mission_id: str, authorization_digest: str) -> bool:
        """Consume one end-to-end mission authorization exactly once on this device.

        This local replay ledger is deliberately not cleared by logout or mirror cleanup. A
        compromised coordinator must not be able to make an old signed instruction executable
        again by deleting and recreating its cloud row.
        """
        mission_id = str(mission_id or "").strip()
        authorization_digest = str(authorization_digest or "").strip()
        if not mission_id or not authorization_digest:
            raise ValueError("mission id and authorization digest are required")
        with self._lock:
            cursor = self.db.execute("""INSERT OR IGNORE INTO online_mission_authorizations(
                mission_id,authorization_digest,accepted_at) VALUES(?,?,?)""",
                (mission_id, authorization_digest, int(time.time())))
            self.db.commit()
            return cursor.rowcount == 1

    def begin_connection_invocation(self, connection_id: str, idempotency_key: str,
                                    tool_name: str, args_hash: str) -> dict:
        """Reserve one local direct-MCP effect before the external uncertainty boundary."""
        values = tuple(str(x or "").strip() for x in
                       (connection_id, idempotency_key, tool_name, args_hash))
        if not all(values):
            raise ValueError("connection invocation identity is incomplete")
        now = int(time.time())
        with self._lock:
            row = self.db.execute("""SELECT * FROM online_connection_invocations
                WHERE connection_id=? AND idempotency_key=?""", values[:2]).fetchone()
            if row is not None:
                return dict(row)
            self.db.execute("""INSERT INTO online_connection_invocations(
                connection_id,idempotency_key,tool_name,args_hash,state,result_hash,
                created_at,completed_at) VALUES(?,?,?,?,'executing','',?,0)""",
                (*values, now))
            self.db.commit()
            return {"connection_id": values[0], "idempotency_key": values[1],
                    "tool_name": values[2], "args_hash": values[3], "state": "new",
                    "result_hash": "", "created_at": now, "completed_at": 0}

    def settle_connection_invocation(self, connection_id: str, idempotency_key: str,
                                     state: str, result_hash: str = "") -> None:
        state = str(state or "")
        if state not in ("completed", "uncertain"):
            raise ValueError("invalid direct connection invocation state")
        with self._lock:
            cursor = self.db.execute("""UPDATE online_connection_invocations SET
                state=?,result_hash=?,completed_at=? WHERE connection_id=? AND idempotency_key=?
                AND state='executing'""", (state, str(result_hash or ""), int(time.time()),
                                            str(connection_id), str(idempotency_key)))
            self.db.commit()
            if cursor.rowcount != 1:
                raise OnlineError("direct connection invocation was not in the executing state")

    def cache_connection_credential(self, connection_id: str, envelope: dict,
                                    updated_at: int) -> None:
        connection_id, updated_at = str(connection_id or ""), int(updated_at or 0)
        if not connection_id or not isinstance(envelope, dict) or updated_at <= 0:
            raise ValueError("sealed connection credential cache entry is invalid")
        with self._lock:
            self.db.execute("""INSERT INTO online_connection_credentials(
                connection_id,envelope_json,updated_at) VALUES(?,?,?) ON CONFLICT(connection_id)
                DO UPDATE SET envelope_json=excluded.envelope_json,updated_at=excluded.updated_at
                WHERE excluded.updated_at>=online_connection_credentials.updated_at""",
                (connection_id, _json(envelope), updated_at))
            self.db.commit()

    def connection_credential(self, connection_id: str) -> dict:
        row = self.db.execute("""SELECT envelope_json,updated_at FROM
            online_connection_credentials WHERE connection_id=?""",
            (str(connection_id or ""),)).fetchone()
        if row is None:
            return {}
        return {"sealed_credential": _decode_json(row["envelope_json"], {}),
                "updated_at": int(row["updated_at"])}

    def cache_catalog(self, kind: str, rows: list[dict]) -> None:
        kind = str(kind or "")
        if kind not in ("nodes", "missions", "schedules"):
            raise ValueError("invalid Online catalog")
        now = int(time.time()); seen = set()
        with self._lock:
            for row in rows or []:
                item_id = str(row.get("id") or "")
                if not item_id:
                    continue
                seen.add(item_id)
                self.db.execute("""INSERT INTO online_catalog(kind,item_id,payload_json,updated_at)
                    VALUES(?,?,?,?) ON CONFLICT(kind,item_id) DO UPDATE SET
                    payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                    (kind, item_id, _json(row), now))
            if seen:
                marks = ",".join("?" for _ in seen)
                self.db.execute("DELETE FROM online_catalog WHERE kind=? AND item_id NOT IN (%s)" % marks,
                                (kind, *tuple(sorted(seen))))
            else:
                self.db.execute("DELETE FROM online_catalog WHERE kind=?", (kind,))
            self.db.commit()

    def catalog(self, kind: str) -> list[dict]:
        rows = self.db.execute("SELECT payload_json FROM online_catalog WHERE kind=? ORDER BY updated_at DESC",
                               (str(kind),)).fetchall()
        return [_decode_json(row["payload_json"], {}) for row in rows]

    def put(self, *, object_id: str, object_type: str, project_id: str,
            data_class: DataClass, content: Any, tombstone: bool = False) -> SyncObject:
        data_class = DataClass(data_class)
        if data_class is DataClass.SECRET:
            raise ValueError("secret values belong in the Connection Vault, not sync objects")
        now = int(time.time())
        object_id = str(object_id or uuid.uuid4().hex)
        with self._lock:
            prior = self.db.execute("SELECT version FROM sync_objects WHERE object_id=?",
                                    (object_id,)).fetchone()
            base_version = int(prior["version"]) if prior else 0
            payload = {
                "operation_id": uuid.uuid4().hex, "object_id": object_id,
                "object_type": str(object_type or "")[:80], "project_id": str(project_id or "")[:200],
                "data_class": data_class.value,
                "content": (_seal_value(self.path, object_id, str(project_id or "")[:200], content)
                            if data_class is DataClass.SEALED else content),
                "base_version": base_version, "updated_at": now, "tombstone": bool(tombstone),
            }
            if not payload["object_type"] or not payload["project_id"]:
                raise ValueError("object type and project id are required")
            content_json = _json(content)
            self.db.execute("""INSERT INTO sync_objects(object_id,object_type,project_id,data_class,
                content_json,version,updated_at,tombstone,dirty,conflict_of) VALUES(?,?,?,?,?,?,?,?,1,'')
                ON CONFLICT(object_id) DO UPDATE SET object_type=excluded.object_type,
                project_id=excluded.project_id,data_class=excluded.data_class,
                content_json=excluded.content_json,updated_at=excluded.updated_at,
                tombstone=excluded.tombstone,dirty=1""",
                (object_id, payload["object_type"], payload["project_id"], data_class.value,
                 content_json, base_version, now, int(bool(tombstone))))
            if data_class in SYNCABLE_DATA_CLASSES:
                self.db.execute("""INSERT INTO sync_outbox(operation_id,object_id,base_version,
                    payload_json,created_at) VALUES(?,?,?,?,?)""",
                    (payload["operation_id"], object_id, base_version, _json(payload), now))
            self.db.commit()
        return self.get(object_id)

    def get(self, object_id: str) -> Optional[SyncObject]:
        row = self.db.execute("SELECT * FROM sync_objects WHERE object_id=?", (object_id,)).fetchone()
        return self._object(row)

    @staticmethod
    def _object(row) -> Optional[SyncObject]:
        if row is None:
            return None
        return SyncObject(
            object_id=row["object_id"], object_type=row["object_type"],
            project_id=row["project_id"], data_class=DataClass(row["data_class"]),
            content=_decode_json(row["content_json"], None), version=int(row["version"]),
            updated_at=int(row["updated_at"]), tombstone=bool(row["tombstone"]),
            conflict_of=row["conflict_of"])

    def list_objects(self, *, project_id: str = "", object_type: str = "",
                     include_deleted: bool = False) -> list[SyncObject]:
        where, params = [], []
        if project_id:
            where.append("project_id=?"); params.append(project_id)
        if object_type:
            where.append("object_type=?"); params.append(object_type)
        if not include_deleted:
            where.append("tombstone=0")
        sql = "SELECT * FROM sync_objects" + ((" WHERE " + " AND ".join(where)) if where else "")
        sql += " ORDER BY updated_at DESC, object_id"
        return [self._object(row) for row in self.db.execute(sql, params).fetchall()]

    def pending(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT * FROM sync_outbox ORDER BY created_at,rowid LIMIT ?",
                               (max(1, min(int(limit), 500)),)).fetchall()
        return [_decode_json(row["payload_json"], {}) for row in rows]

    def mark_pushed(self, accepted: list[dict], conflicts: list[dict]) -> None:
        """Apply a push response without discarding either side of a conflict."""
        now = int(time.time())
        with self._lock:
            for item in accepted or []:
                op = str(item.get("operation_id") or "")
                oid = str(item.get("object_id") or "")
                version = int(item.get("version") or 0)
                if not op or not oid or version <= 0:
                    continue
                self.db.execute("DELETE FROM sync_outbox WHERE operation_id=?", (op,))
                remaining = self.db.execute(
                    "SELECT 1 FROM sync_outbox WHERE object_id=? LIMIT 1", (oid,)).fetchone()
                self.db.execute("UPDATE sync_objects SET version=?,dirty=? WHERE object_id=?",
                                (version, 1 if remaining else 0, oid))
            for conflict in conflicts or []:
                op = str(conflict.get("operation_id") or "")
                if not op:
                    continue
                row = self.db.execute("SELECT * FROM sync_outbox WHERE operation_id=?", (op,)).fetchone()
                if row is None:
                    continue
                payload = _decode_json(row["payload_json"], {})
                sibling = "%s~conflict~%s" % (payload.get("object_id") or "object", op[:8])
                sibling_content = payload.get("content")
                if payload.get("data_class") == DataClass.SEALED.value:
                    sibling_content = _open_value(
                        self.path, str(payload.get("object_id") or ""),
                        str(payload.get("project_id") or ""), sibling_content)
                self.db.execute("""INSERT OR IGNORE INTO sync_objects(object_id,object_type,project_id,
                    data_class,content_json,version,updated_at,tombstone,dirty,conflict_of)
                    VALUES(?,?,?,?,?,?,?,?,0,?)""",
                    (sibling, payload.get("object_type") or "unknown",
                     payload.get("project_id") or "", payload.get("data_class") or DataClass.SEALED.value,
                     _json(sibling_content), 0, now, int(bool(payload.get("tombstone"))),
                     payload.get("object_id") or ""))
                self.db.execute("DELETE FROM sync_outbox WHERE operation_id=?", (op,))
                self.db.execute("UPDATE sync_objects SET dirty=0 WHERE object_id=?",
                                (payload.get("object_id") or "",))
                server = conflict.get("server")
                if isinstance(server, dict):
                    self._apply_remote_locked(server)
            self.db.commit()

    def _apply_remote_locked(self, event: dict) -> bool:
        oid = str(event.get("object_id") or "")
        version = int(event.get("version") or 0)
        if not oid or version <= 0:
            return False
        current = self.db.execute("SELECT version,dirty FROM sync_objects WHERE object_id=?",
                                  (oid,)).fetchone()
        if current and (int(current["dirty"]) or int(current["version"]) >= version):
            return False
        data_class = DataClass(str(event.get("data_class") or DataClass.SEALED.value))
        if data_class not in SYNCABLE_DATA_CLASSES:
            return False
        project_id = str(event.get("project_id") or "")[:200]
        content = event.get("content")
        if data_class is DataClass.SEALED:
            content = _open_value(self.path, oid, project_id, content)
        self.db.execute("""INSERT INTO sync_objects(object_id,object_type,project_id,data_class,
            content_json,version,updated_at,tombstone,dirty,conflict_of) VALUES(?,?,?,?,?,?,?,?,0,'')
            ON CONFLICT(object_id) DO UPDATE SET object_type=excluded.object_type,
            project_id=excluded.project_id,data_class=excluded.data_class,
            content_json=excluded.content_json,version=excluded.version,
            updated_at=excluded.updated_at,tombstone=excluded.tombstone,dirty=0,conflict_of=''""",
            (oid, str(event.get("object_type") or "unknown")[:80],
             project_id, data_class.value,
             _json(content), version, int(event.get("updated_at") or time.time()),
             int(bool(event.get("tombstone")))))
        return True

    def apply_pull(self, events: list[dict], cursor: int) -> dict:
        profile = self.profile()
        if profile is None:
            raise AuthenticationRequired("Collie is in Local mode")
        applied = ignored = 0
        with self._lock:
            for event in events or []:
                if self._apply_remote_locked(event):
                    applied += 1
                else:
                    ignored += 1
            old = self.cursor(profile.workspace_id)
            if int(cursor) < old:
                raise ValueError("sync cursor cannot move backwards")
            self.db.execute("""INSERT INTO sync_state(workspace_id,cursor,last_sync_at) VALUES(?,?,?)
                ON CONFLICT(workspace_id) DO UPDATE SET cursor=excluded.cursor,
                last_sync_at=excluded.last_sync_at""",
                (profile.workspace_id, int(cursor), int(time.time())))
            self.db.commit()
        return {"applied": applied, "ignored": ignored, "cursor": int(cursor)}

    def cursor(self, workspace_id: str) -> int:
        row = self.db.execute("SELECT cursor FROM sync_state WHERE workspace_id=?",
                              (str(workspace_id),)).fetchone()
        return int(row["cursor"]) if row else 0


class OnlineClient:
    def __init__(self, store: OnlineStore, *, timeout: float = 30):
        self.store = store
        self.timeout = max(1.0, float(timeout))

    def _request(self, method: str, path: str, body: Any = None,
                 *, authenticated: bool = True) -> dict:
        profile = self.store.profile()
        if profile is None:
            raise AuthenticationRequired("Sign in to Collie Online first")
        headers = {"accept": "application/json", "user-agent": "collie-online/1"}
        if authenticated:
            tokens = self.store.tokens()
            if not tokens.get("access_token"):
                raise AuthenticationRequired("Collie Online token is missing")
            headers["authorization"] = "Bearer " + tokens["access_token"]
        data = None
        if body is not None:
            data = _json(body).encode("utf-8")
            headers["content-type"] = "application/json"
        # Bearer theft alone is insufficient: every paired-device request is also Ed25519 signed.
        # The signature covers the exact path/query and request bytes, with a short-lived nonce.
        key = generate_device_key(os.path.join(os.path.dirname(self.store.path),
                                               "online-device-key.json"))
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            timestamp, nonce = str(int(time.time())), _b64(secrets.token_bytes(18))
            body_hash = _b64(hashlib.sha256(data or b"").digest())
            message = "%s\n%s\n%s\n%s\n%s" % (
                method.upper(), path, body_hash, timestamp, nonce)
            signature = Ed25519PrivateKey.from_private_bytes(
                _unb64(key["private_key"])).sign(message.encode())
            headers.update({"x-collie-device-timestamp": timestamp,
                            "x-collie-device-nonce": nonce,
                            "x-collie-device-signature": _b64(signature)})
        except (ImportError, ValueError) as exc:
            raise OnlineError("Connected Mode device signing is unavailable") from exc
        req = urllib.request.Request(profile.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise OnlineError("Collie Online response is too large")
                result = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(result, dict):
                    raise OnlineError("Collie Online returned a non-object response")
                return result
        except urllib.error.HTTPError as exc:
            raw = exc.read(65536).decode("utf-8", "replace")
            detail = _decode_json(raw, {})
            message = detail.get("error") if isinstance(detail, dict) else ""
            if exc.code == 401:
                raise AuthenticationRequired(message or "Collie Online session expired") from exc
            raise OnlineError(message or "Collie Online HTTP %d" % exc.code) from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise OnlineError("Collie Online is unavailable: %s" % exc) from exc

    def sync_once(self, limit: int = 100) -> dict:
        profile = self.store.profile()
        if profile is None:
            return {"mode": "local", "pushed": 0, "pulled": 0, "conflicts": 0}
        pending = self.store.pending(limit)
        accepted, conflicts = [], []
        if pending:
            pushed = self._request("POST", "/v1/sync/push", {"operations": pending})
            accepted = pushed.get("accepted") or []
            conflicts = pushed.get("conflicts") or []
            self.store.mark_pushed(accepted, conflicts)
        cursor = self.store.cursor(profile.workspace_id)
        pulled = self._request("GET", "/v1/sync/pull?cursor=%d&limit=%d" % (
            cursor, max(1, min(int(limit), 500))))
        events = pulled.get("events") or []
        result = self.store.apply_pull(events, int(pulled.get("cursor") or cursor))
        projects = self._request("GET", "/v1/projects").get("projects") or []
        self.store.cache_projects(projects)
        connections = self._request("GET", "/v1/connections").get("connections") or []
        self.store.cache_connections(connections)
        credential_ciphertexts = 0
        for row in connections:
            if row.get("transport") != "device_direct":
                continue
            try:
                ConnectionBrokerClient(self).get_credential(
                    str(row.get("id") or ""), connection=row, refresh=True)
                credential_ciphertexts += 1
            except OnlineError:
                # The ciphertext may already be cached while this endpoint still needs the user's
                # sealed-sync recovery key. Public sync should remain available and surface that
                # missing key only when the connection is invoked.
                pass
        catalogs = {}
        for kind in ("nodes", "missions", "schedules"):
            rows = self._request("GET", "/v1/%s" % kind).get(kind) or []
            self.store.cache_catalog(kind, rows); catalogs[kind] = len(rows)
        return {"mode": "connected", "pushed": len(accepted), "pulled": result["applied"],
                "conflicts": len(conflicts), "cursor": result["cursor"],
                "projects": len(projects), "connections": len(connections),
                "connection_credentials": credential_ciphertexts, **catalogs}

    def devices(self) -> list[dict]:
        return self._request("GET", "/v1/devices").get("devices") or []

    def create_project(self, name: str) -> dict:
        name = str(name or "").strip()
        if not name or len(name) > 200:
            raise ValueError("project name is required")
        project = self._request("POST", "/v1/projects", {"name": name}).get("project") or {}
        project_id = str(project.get("id") or project.get("project_id") or "").strip()
        if not project_id:
            raise OnlineError("Collie Online returned no project id")
        return self.store.upsert_project(
            project_id, str(project.get("name") or name),
            role=str(project.get("role") or "owner"))

    def revoke_device(self, device_id: str) -> dict:
        return self._request("POST", "/v1/devices/revoke", {"device_id": device_id})

    def workspaces(self) -> list[dict]:
        return self._request("GET", "/v1/workspaces").get("workspaces") or []

    def switch_workspace(self, workspace_id: str) -> OnlineProfile:
        result = self._request("POST", "/v1/workspaces/select",
                               {"workspace_id": str(workspace_id or "")})
        return self.store.switch_workspace(
            result.get("workspace_id") or workspace_id,
            access_token=result.get("access_token") or "",
            refresh_token=result.get("refresh_token") or "",
            access_expires_at=int(result.get("access_expires_at") or 0),
            refresh_expires_at=int(result.get("refresh_expires_at") or 0))

    def refresh(self) -> dict:
        profile = self.store.profile()
        if profile is None:
            raise AuthenticationRequired("Sign in to Collie Online first")
        tokens = self.store.tokens()
        refresh = str(tokens.get("refresh_token") or "")
        if not refresh:
            raise AuthenticationRequired("Collie Online refresh token is missing")
        result = self._request("POST", "/v1/auth/refresh",
                               {"refresh_token": refresh}, authenticated=False)
        self.store.update_tokens(
            result.get("access_token") or "", result.get("refresh_token") or "",
            int(result.get("access_expires_at") or 0),
            int(result.get("refresh_expires_at") or 0))
        return result


def _checked_base_url(value: str) -> str:
    value = str(value or "").rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") \
            and os.environ.get("COLLIE_ONLINE_ALLOW_HTTP") == "1":
        return value
    raise ValueError("Collie Online base URL must be HTTPS")


class DeviceEnrollmentClient:
    """Pre-account device-code client; no existing profile or bearer is required."""

    def __init__(self, base_url: str, *, timeout: float = 30):
        self.base_url = _checked_base_url(base_url)
        self.timeout = max(1.0, float(timeout))

    def _post(self, path: str, value: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base_url + path, data=_json(value).encode("utf-8"), method="POST",
            headers={"content-type": "application/json", "accept": "application/json",
                     "user-agent": "collie-online/1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise OnlineError("Collie Online enrollment response is too large")
                result = _decode_json(raw.decode("utf-8"), {})
                return int(response.status), result if isinstance(result, dict) else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read(65536).decode("utf-8", "replace")
            result = _decode_json(raw, {})
            return int(exc.code), result if isinstance(result, dict) else {}
        except (urllib.error.URLError, TimeoutError) as exc:
            raise OnlineError("Collie Online is unavailable: %s" % exc) from exc

    def start(self, device_name: str, *, public_key: str = "") -> dict:
        local = new_device_enrollment(device_name)
        status, remote = self._post("/v1/devices/start", {
            "device_name": local["device_name"], "challenge": local["challenge"],
            "public_key": str(public_key or "")})
        if status != 201:
            raise OnlineError(remote.get("error") or "could not start device enrollment")
        return dict(remote, verifier=local["verifier"], nonce=local["nonce"])

    def poll(self, enrollment: dict) -> Optional[dict]:
        status, result = self._post("/v1/devices/token", {
            "device_code": enrollment.get("device_code"),
            "verifier": enrollment.get("verifier")})
        if status == 428 and result.get("error") == "authorization_pending":
            return None
        if status != 200:
            raise OnlineError(result.get("error") or "device enrollment failed")
        return result

    def finish(self, store: OnlineStore, enrollment: dict, result: dict) -> OnlineProfile:
        return store.connect(
            base_url=self.base_url, user_id=result.get("user_id"),
            workspace_id=result.get("workspace_id"), device_id=result.get("device_id"),
            device_name=result.get("device_name") or enrollment.get("device_name") or "This device",
            access_token=result.get("access_token"), refresh_token=result.get("refresh_token"),
            access_expires_at=int(result.get("access_expires_at") or 0),
            refresh_expires_at=int(result.get("refresh_expires_at") or 0))


def generate_device_key(path: Optional[str] = None) -> dict:
    """Create or load one Ed25519 device key; never puts the private key in online.db."""
    path = path or os.path.join(_state_dir(), "online-device-key.json")
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if value.get("algorithm") == "Ed25519" and value.get("private_key") and value.get("public_key"):
            return value
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
    except ImportError as exc:
        raise OnlineError("Connected device identity needs: pip install 'collie-harness[online]'") from exc
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    value = {"algorithm": "Ed25519", "private_key": _b64(private), "public_key": _b64(public),
             "created_at": int(time.time())}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".%s.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
    try:
        from . import plat
        plat.chmod_private(tmp)
    except Exception:
        pass
    os.replace(tmp, path)
    return value


class ConnectionBrokerClient:
    """Reviewed MCP connection client with E2E device-direct as the secure default."""

    def __init__(self, online: OnlineClient):
        self.online = online

    def list(self, *, refresh_cache: bool = True) -> list[dict]:
        result = self.online._request("GET", "/v1/connections")
        rows = result.get("connections") or []
        if refresh_cache:
            self.online.store.cache_connections(rows)
        return rows

    def create(self, *, name: str, endpoint: str, manifest: list[dict],
               scope: str = "personal", project_id: str = "",
               transport: str = "cloud_proxy") -> dict:
        result = self.online._request("POST", "/v1/connections", {
            "name": name, "endpoint": endpoint, "manifest": manifest,
            "scope": scope, "project_id": project_id, "transport": transport})
        return result["connection"]

    def _connection(self, connection_id: str, *, refresh: bool = False) -> dict:
        connection_id = str(connection_id or "")
        rows = self.list(refresh_cache=True) if refresh else self.online.store.connections()
        row = next((item for item in rows if str(item.get("id") or "") == connection_id), None)
        if row is None and not refresh:
            return self._connection(connection_id, refresh=True)
        if row is None:
            raise OnlineError("reviewed connection is not available on this device")
        return row

    @staticmethod
    def _credential_scope(connection: dict, profile: OnlineProfile) -> str:
        if connection.get("scope") == "project":
            return "connection-project:%s" % str(connection.get("project_id") or "")
        return "connection-personal:%s:%s" % (profile.workspace_id, profile.user_id)

    @staticmethod
    def _clean_credential(credential: dict) -> dict:
        if not isinstance(credential, dict):
            raise ValueError("credential object is required")
        allowed = ("access_token", "refresh_token", "token_type", "expires_at",
                   "token_endpoint", "client_id", "client_secret", "headers")
        clean = {key: credential[key] for key in allowed if key in credential}
        if not clean.get("access_token") and not isinstance(clean.get("headers"), dict):
            raise ValueError("credential needs an access token or headers")
        _json(clean)
        return clean

    @staticmethod
    def _binding(connection: dict) -> dict:
        return {key: connection.get(key) for key in
                ("id", "name", "scope", "project_id", "transport", "endpoint",
                 "manifest_digest", "reviewed_digest")}

    @staticmethod
    def _validate_device_direct_definition(connection: dict) -> None:
        if connection.get("transport") != "device_direct":
            return
        manifest = connection.get("manifest")
        if not isinstance(manifest, list):
            raise OnlineError("device-direct connection manifest is missing")
        ordered = sorted(manifest, key=lambda row: str(row.get("name") or "")
                         if isinstance(row, dict) else "")
        digest = _b64(hashlib.sha256(_json(ordered).encode()).digest())
        if not secrets.compare_digest(digest, str(connection.get("manifest_digest") or "")):
            raise OnlineError("device-direct connection manifest digest is invalid")
        if connection.get("reviewed_digest") != connection.get("manifest_digest"):
            raise OnlineError("device-direct connection manifest has not been reviewed")

    def review(self, connection_id: str, digest: str,
               allowed_effects: tuple[str, ...] = ("observe", "prepare", "act")) -> dict:
        result = self.online._request("POST", "/v1/connections/%s/review" %
                                      urllib.parse.quote(connection_id, safe=""), {
            "digest": digest, "allowed_effects": list(allowed_effects)})
        return result["connection"]

    def put_credential(self, connection_id: str, credential: dict,
                       *, connection: Optional[dict] = None) -> dict:
        clean = self._clean_credential(credential)
        if connection is None:
            connection = next((row for row in self.online.store.connections()
                               if str(row.get("id") or "") == str(connection_id)), None)
        # Compatibility for callers that intentionally use the legacy cloud Vault and have not
        # refreshed the local public connection cache yet.
        connection = connection or {"id": connection_id, "transport": "cloud_proxy"}
        path = "/v1/connections/%s/credential" % urllib.parse.quote(connection_id, safe="")
        if connection.get("transport") == "device_direct":
            self._validate_device_direct_definition(connection)
            profile = self.online.store.profile()
            if profile is None:
                raise AuthenticationRequired("Sign in to Collie Online first")
            content = {"version": 1, "binding": self._binding(connection),
                       "credential": clean, "sealed_at": int(time.time())}
            envelope = _seal_value(
                self.online.store.path, "connection-credential:%s" % connection_id,
                self._credential_scope(connection, profile), content)
            result = self.online._request("POST", path, {"sealed_credential": envelope})
            self.online.store.cache_connection_credential(
                connection_id, envelope, int(result.get("updated_at") or time.time()))
            return result
        # Explicit compatibility mode. The server runtime can decrypt this Vault entry.
        return self.online._request("POST", path, {"credential": clean})

    def get_credential(self, connection_id: str, *, connection: Optional[dict] = None,
                       refresh: bool = False) -> dict:
        connection = connection or self._connection(connection_id)
        if connection.get("transport") != "device_direct":
            raise OnlineError("only device-direct credentials are opened on endpoints")
        self._validate_device_direct_definition(connection)
        cached = self.online.store.connection_credential(connection_id)
        if not cached or refresh:
            try:
                remote = self.online._request("GET", "/v1/connections/%s/credential" %
                                              urllib.parse.quote(connection_id, safe=""))
                remote_at = int(remote.get("updated_at") or 0)
                if isinstance(remote.get("sealed_credential"), dict) and \
                        remote_at >= int(cached.get("updated_at") or 0):
                    self.online.store.cache_connection_credential(
                        connection_id, remote["sealed_credential"], remote_at)
                    cached = remote
            except OnlineError:
                if not cached:
                    raise
        envelope = cached.get("sealed_credential")
        if not isinstance(envelope, dict):
            raise OnlineError("device-direct credential is unavailable")
        profile = self.online.store.profile()
        content = _open_value(
            self.online.store.path, "connection-credential:%s" % connection_id,
            self._credential_scope(connection, profile), envelope)
        if not isinstance(content, dict) or content.get("version") != 1 or \
                _json(content.get("binding") or {}) != _json(self._binding(connection)):
            raise OnlineError("device-direct credential is not bound to this reviewed connection")
        credential = content.get("credential")
        return self._clean_credential(credential)

    def _refresh_device_credential(self, connection: dict, credential: dict) -> dict:
        now = int(time.time())
        if not credential.get("refresh_token") or not credential.get("token_endpoint") or \
                int(credential.get("expires_at") or 0) > now + 30:
            return credential
        from . import mcpclient
        if not mcpclient._safe_oauth_url(str(credential["token_endpoint"])):
            raise OnlineError("device-direct token endpoint is not safe public HTTPS")
        form = {"grant_type": "refresh_token", "refresh_token": str(credential["refresh_token"])}
        if credential.get("client_id"):
            form["client_id"] = str(credential["client_id"])
        if credential.get("client_secret"):
            form["client_secret"] = str(credential["client_secret"])
        token = mcpclient._http_json(
            str(credential["token_endpoint"]), form, method="POST", form=True)
        if not isinstance(token, dict) or not token.get("access_token"):
            raise OnlineError("device-direct credential refresh returned no access token")
        value = dict(credential)
        value.update(access_token=token["access_token"],
                     refresh_token=token.get("refresh_token") or credential["refresh_token"],
                     token_type=token.get("token_type") or credential.get("token_type") or "Bearer",
                     expires_at=now + int(token.get("expires_in") or 3600))
        self.put_credential(connection["id"], value, connection=connection)
        return value

    def grant(self, connection_id: str, tool: str, arguments: dict,
              *, bounds: Optional[dict] = None) -> dict:
        return self.online._request("POST", "/v1/connections/%s/grant" %
                                    urllib.parse.quote(connection_id, safe=""), {
            "tool": tool, "arguments": arguments, "bounds": bounds or {}})

    def invoke(self, connection_id: str, tool: str, arguments: dict, *,
               grant_id: str = "", idempotency_key: str = "") -> dict:
        key = idempotency_key or uuid.uuid4().hex
        connection = self._connection(connection_id)
        if connection.get("transport") == "device_direct":
            from . import mcpclient
            if (connection.get("status") != "active" or
                    connection.get("reviewed_digest") != connection.get("manifest_digest")):
                raise OnlineError("device-direct connection manifest is not reviewed")
            if not mcpclient._safe_oauth_url(str(connection.get("endpoint") or "")):
                raise OnlineError("device-direct MCP endpoint is not safe public HTTPS")
            manifest = connection.get("manifest") if isinstance(connection.get("manifest"), list) else []
            if not any(str(row.get("name") or "") == str(tool) for row in manifest if isinstance(row, dict)):
                raise OnlineError("tool is not in the reviewed connection manifest")
            args = arguments if isinstance(arguments, dict) else {}
            args_hash = hashlib.sha256(_json(args).encode()).hexdigest()
            prior = self.online.store.begin_connection_invocation(
                connection_id, key, str(tool), args_hash)
            if prior.get("state") != "new":
                if prior.get("tool_name") != str(tool) or prior.get("args_hash") != args_hash:
                    raise OnlineError("idempotency key was used for another direct action")
                return {"already_settled": prior.get("state") == "completed",
                        "state": prior.get("state"), "result_hash": prior.get("result_hash") or "",
                        "reconciliation_required": prior.get("state") != "completed"}
            client = None
            try:
                credential = self._refresh_device_credential(
                    connection, self.get_credential(connection_id, connection=connection))
                headers = dict(credential.get("headers") or {})
                if credential.get("access_token") and not any(
                        str(name).casefold() == "authorization" for name in headers):
                    headers["Authorization"] = "%s %s" % (
                        credential.get("token_type") or "Bearer", credential["access_token"])
                client = mcpclient._HTTPConnection(
                    "online-%s" % connection_id,
                    {"url": connection["endpoint"], "headers": headers})
                result = client.call_tool(str(tool), args)
                result_hash = hashlib.sha256(_json(result).encode()).hexdigest()
                self.online.store.settle_connection_invocation(
                    connection_id, key, "completed", result_hash)
                return {"state": "completed", "result": result, "result_hash": result_hash,
                        "transport": "device_direct"}
            except Exception as exc:
                self.online.store.settle_connection_invocation(connection_id, key, "uncertain")
                raise OnlineError("device-direct MCP result is uncertain; inspect before retrying: %s" % exc) from exc
            finally:
                if client is not None:
                    client.close()
        return self.online._request("POST", "/v1/connections/%s/invoke" %
                                    urllib.parse.quote(connection_id, safe=""), {
            "tool": tool, "arguments": arguments, "grant_id": grant_id,
            "idempotency_key": key})

    def revoke(self, connection_id: str) -> dict:
        return self.online._request("POST", "/v1/connections/%s/revoke" %
                                    urllib.parse.quote(connection_id, safe=""), {})

    def publish_local_mcp(self, name: str, *, scope: str = "personal",
                          project_id: str = "", effect_overrides: Optional[dict] = None,
                          allowed_effects: tuple[str, ...] = ("observe", "prepare", "act"),
                          transport: str = "device_direct") -> dict:
        definition, credential = export_local_mcp_connection(
            name, effect_overrides=effect_overrides)
        definition["manifest"] = sorted(definition["manifest"], key=lambda row: row["name"])
        created = self.create(name=definition["name"], endpoint=definition["endpoint"],
                              manifest=definition["manifest"], scope=scope,
                              project_id=project_id, transport=transport)
        expected_digest = _b64(hashlib.sha256(_json(definition["manifest"]).encode()).digest())
        if (created.get("endpoint") != definition["endpoint"] or
                _json(created.get("manifest") or []) != _json(definition["manifest"]) or
                created.get("manifest_digest") != expected_digest or
                created.get("transport") != transport):
            raise OnlineError("Collie Online changed the connection definition during creation")
        reviewed = self.review(created["id"], created["manifest_digest"], allowed_effects)
        if (reviewed.get("endpoint") != definition["endpoint"] or
                _json(reviewed.get("manifest") or []) != _json(definition["manifest"]) or
                reviewed.get("manifest_digest") != expected_digest or
                reviewed.get("transport") != transport):
            raise OnlineError("Collie Online changed the connection definition during review")
        self.put_credential(created["id"], credential, connection=reviewed)
        self.list(refresh_cache=True)
        return reviewed


def _mcp_action(name: str) -> str:
    text = str(name or "").casefold().replace("_", " ").replace("-", " ")
    for action, words in (
        ("send", ("send", "message", "email")),
        ("publish", ("publish", "post", "release", "deploy")),
        ("delete", ("delete", "remove", "unsubscribe")),
        ("invite", ("invite",)), ("merge", ("merge",)),
        ("upload", ("upload", "attach")),
        ("purchase", ("purchase", "buy", "pay", "checkout")),
        ("submit", ("submit", "create", "update", "write", "set")),
    ):
        if any(re.search(r"\b%s\b" % re.escape(word), text) for word in words):
            return action
    return "external_change"


def export_local_mcp_connection(name: str, *,
                                effect_overrides: Optional[dict] = None) -> tuple[dict, dict]:
    """Split one explicitly named remote MCP into public definition and secret credential."""
    from . import mcpclient
    name = str(name or "").strip()
    cfg = (mcpclient._load_config().get(name) or {})
    endpoint = str(cfg.get("url") or "")
    if not endpoint:
        raise ValueError("only a configured remote HTTP MCP connection can be published")
    cache = mcpclient._read_cache().get(name) or {}
    if cache.get("hash") != mcpclient._cfg_hash(cfg) or not isinstance(cache.get("tools"), list):
        raise ValueError("refresh this MCP connection before publishing so its exact manifest is pinned")
    overrides = {str(k): str(v) for k, v in (effect_overrides or {}).items()}
    manifest = []
    for tool in cache["tools"]:
        tool_name = str(tool.get("name") or "")
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        effect = overrides.get(tool_name)
        if effect is None:
            effect = ("observe" if annotations.get("readOnlyHint") is True else
                      "restricted" if annotations.get("destructiveHint") is True else "commit")
        if effect not in ("observe", "prepare", "act", "commit", "restricted"):
            raise ValueError("invalid effect override for %s" % tool_name)
        manifest.append({
            "name": tool_name, "effect": effect, "action": _mcp_action(tool_name),
            "description": str(tool.get("description") or "")[:1000],
            "input_schema": tool.get("inputSchema") or {}, "annotations": annotations,
        })
    token = mcpclient._get_token(name) or {}
    headers = dict(cfg.get("headers") or {}) if isinstance(cfg.get("headers"), dict) else {}
    credential = dict(token)
    auth_key = next((key for key in headers if key.casefold() == "authorization"), "")
    if auth_key:
        auth = str(headers.pop(auth_key))
        if auth.casefold().startswith("bearer "):
            credential.setdefault("access_token", auth[7:].strip())
            credential.setdefault("token_type", "Bearer")
        else:
            headers[auth_key] = auth
    if headers:
        credential["headers"] = headers
    for key in ("client_id", "client_secret"):
        if cfg.get(key) and not credential.get(key):
            credential[key] = cfg[key]
    if not credential.get("access_token") and not credential.get("headers"):
        raise ValueError("the MCP connection has no credential to place in the vault")
    return ({"name": name, "endpoint": endpoint, "manifest": manifest}, credential)


def new_device_enrollment(device_name: str) -> dict:
    """Create the local half of a PKCE-bound device-code enrollment.

    Device signing keys are created by the platform enrollment command; this helper is
    stdlib and produces only the one-time verifier/challenge pair sent to the API.
    """
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    return {"device_name": str(device_name or "This device")[:120],
            "verifier": verifier, "challenge": challenge,
            "nonce": uuid.uuid4().hex}
