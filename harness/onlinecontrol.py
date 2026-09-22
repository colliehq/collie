"""Public, token-free local control surface for optional Collie Connected Mode."""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from .online import (DataClass, DeviceEnrollmentClient, OnlineClient, OnlineStore,
                     generate_device_key)


def _root(path: Optional[str] = None) -> str:
    value = os.path.abspath(os.path.expanduser(
        path or os.environ.get("COLLIE_STATE_DIR") or "~/.collie"))
    os.makedirs(value, exist_ok=True)
    return value


def _paths(path=None) -> tuple[str, str]:
    root = _root(path)
    return os.path.join(root, "online.db"), os.path.join(root, "online-pairing.json")


def _write_pending(path: str, value: dict) -> None:
    tmp = path + ".%s.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
    try:
        from . import plat
        plat.chmod_private(tmp)
    except Exception:
        pass
    os.replace(tmp, path)


def _read_pending(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def snapshot(path=None) -> dict:
    db_path, pending_path = _paths(path)
    store = OnlineStore(db_path)
    try:
        profile = store.profile()
        pending = _read_pending(pending_path)
        public_pending = {}
        if pending and int(pending.get("expires_at") or 0) > int(time.time()):
            public_pending = {key: pending.get(key) for key in
                              ("user_code", "verification_uri", "verification_uri_complete",
                               "expires_at")}
        trusted_devices = []
        if profile:
            from .online_node import trusted_device_pins
            trusted_devices = trusted_device_pins(store)
        return {
            "mode": "connected" if profile else "local",
            "account": ({"user_id": profile.user_id, "workspace_id": profile.workspace_id,
                         "base_url": profile.base_url} if profile else {}),
            "device": ({"id": profile.device_id, "name": profile.device_name,
                        "connected_at": profile.connected_at} if profile else {}),
            "projects": store.projects(), "connections": store.connections(),
            "project_bindings": store.project_bindings(),
            "nodes": store.catalog("nodes"), "missions": store.catalog("missions"),
            "schedules": store.catalog("schedules"),
            "trusted_devices": trusted_devices,
            "pending_sync": len(store.pending(500)) if profile else 0,
            "pairing": public_pending,
            "cloud_llm_default": "off",
        }
    finally:
        store.close()


def start_pairing(path=None, *, base_url: str, device_name: str) -> dict:
    db_path, pending_path = _paths(path)
    key = generate_device_key(os.path.join(_root(path), "online-device-key.json"))
    client = DeviceEnrollmentClient(base_url)
    enrollment = client.start(device_name, public_key=key["public_key"])
    enrollment["base_url"] = client.base_url
    enrollment["created_at"] = int(time.time())
    enrollment["expires_at"] = enrollment["created_at"] + int(enrollment.get("expires_in") or 600)
    _write_pending(pending_path, enrollment)
    return snapshot(path)["pairing"]


def poll_pairing(path=None) -> dict:
    db_path, pending_path = _paths(path)
    enrollment = _read_pending(pending_path)
    if not enrollment:
        raise ValueError("no device pairing is pending")
    if int(enrollment.get("expires_at") or 0) <= int(time.time()):
        try: os.remove(pending_path)
        except OSError: pass
        raise ValueError("device pairing expired")
    client = DeviceEnrollmentClient(enrollment["base_url"])
    result = client.poll(enrollment)
    if result is None:
        return {"pending": True, "pairing": snapshot(path)["pairing"]}
    store = OnlineStore(db_path)
    try:
        client.finish(store, enrollment, result)
        sync = OnlineClient(store).sync_once()
    finally:
        store.close()
    try: os.remove(pending_path)
    except OSError: pass
    return {"pending": False, "connected": True, "sync": sync,
            "online": snapshot(path)}


def sync(path=None) -> dict:
    db_path, _ = _paths(path)
    store = OnlineStore(db_path)
    try:
        from .onlinesync import sync_all
        return sync_all(store)
    finally:
        store.close()


def create_project(path=None, *, name: str, local_project: str = "",
                   memory_data_class: str = "sealed", cwd: str = "") -> dict:
    """Create an Online project and bind it to this device's local memory in one explicit step."""
    name = str(name or "").strip()
    local_project = str(local_project or name).strip()
    data_class = DataClass(str(memory_data_class or "sealed"))
    if data_class not in (DataClass.SEALED, DataClass.CLOUD_INDEXED):
        raise ValueError("project memory must be cloud-indexed or sealed")
    db_path, _ = _paths(path)
    store = OnlineStore(db_path)
    try:
        project = OnlineClient(store).create_project(name)
        binding = store.bind_project(
            project["project_id"], local_project,
            os.path.join(_root(path), "data", "memory.db"),
            cwd=str(cwd or ""), memory_data_class=data_class)
    finally:
        store.close()
    return {"ok": True, "project": project, "binding": binding, "online": snapshot(path)}


def logout(path=None, *, forget_mirror: bool = False, force: bool = False) -> dict:
    db_path, _ = _paths(path)
    store = OnlineStore(db_path)
    try:
        profile = store.profile()
        if profile is None:
            return {"ok": True, "mode": "local"}
        try:
            OnlineClient(store).revoke_device(profile.device_id)
        except Exception:
            if not force:
                raise
        store.disconnect(forget_mirror=bool(forget_mirror))
        return {"ok": True, "mode": "local", "mirror_kept": not forget_mirror}
    finally:
        store.close()
