"""Local execution node for Collie Online missions.

The cloud owns queueing and short leases; this process owns models, provider logins,
browser/desktop sessions, files, and complex execution. A running lease is renewed in
the background. If renewal is lost, completion is refused and the cloud moves the work
to Needs You instead of replaying a potentially completed external effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from .online import (OnlineClient, OnlineError, OnlineStore, _b64, _json, _unb64,
                     generate_device_key)


_AUTH_VERSION = 1
_AUTH_PREFIX = b"collie-online-authorization-v1\0"
_CLOCK_SKEW_SECONDS = 300
_MISSION_LIFETIME_SECONDS = 7 * 86400
_SCHEDULE_LIFETIME_SECONDS = 366 * 86400
_MAX_AUTH_LIFETIME_SECONDS = 370 * 86400
_DEFAULT_OCCURRENCE_GRACE_SECONDS = 86400
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def _device_key_path(store: OnlineStore) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(store.path)), "online-device-key.json")


def _trust_path(store: OnlineStore) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(store.path)), "online-trusted-devices.json")


def _valid_public_key(value: str) -> str:
    value = str(value or "").strip()
    try:
        raw = _unb64(value)
        if len(raw) != 32:
            raise ValueError
    except Exception as exc:
        raise ValueError("trusted device needs a valid Ed25519 public key") from exc
    return _b64(raw)


def _read_trust(store: OnlineStore) -> dict:
    try:
        with open(_trust_path(store), encoding="utf-8") as handle:
            value = json.load(handle)
        if value.get("version") == 1 and isinstance(value.get("devices"), dict):
            return value
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"version": 1, "devices": {}}


def _write_trust(store: OnlineStore, value: dict) -> None:
    path = _trust_path(store)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".%s.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        from . import plat
        plat.chmod_private(tmp)
    except Exception:
        pass
    os.replace(tmp, path)


def _authorization_message(intent: dict) -> bytes:
    return _AUTH_PREFIX + _json(intent).encode("utf-8")


def _mission_values(*, goal: str, payload: Optional[dict], required_capabilities,
                    fallback: str, data_class: str, cloud_budget_tokens: int) -> dict:
    goal = str(goal or "").strip()
    payload = payload if payload is not None else {}
    if not goal or len(goal) > 4000:
        raise ValueError("mission goal is required and must be at most 4000 characters")
    if not isinstance(payload, dict):
        raise ValueError("mission payload must be an object")
    _json(payload)  # Reject values that cannot be represented in the signed request.
    required = sorted(set(str(x or "").strip() for x in required_capabilities if str(x or "").strip()))
    if len(required) > 64 or any(not re.match(r"^[A-Za-z0-9_.:-]{1,100}$", x) for x in required):
        raise ValueError("mission capabilities must be a bounded list of names")
    fallback = str(fallback or "wait")
    data_class = str(data_class or "cloud_indexed")
    if fallback not in ("wait", "home_node", "cloud_light"):
        raise ValueError("invalid mission fallback")
    if data_class not in ("cloud_indexed", "sealed"):
        raise ValueError("invalid mission data class")
    budget = int(cloud_budget_tokens)
    if budget < 0 or budget > 4096:
        raise ValueError("cloud token budget must be between 0 and 4096")
    if fallback == "cloud_light" and (data_class != "cloud_indexed" or not budget):
        raise ValueError("Cloud Light needs cloud-indexed input and an explicit token budget")
    return {"goal": goal, "payload": payload, "required_capabilities": required,
            "fallback": fallback, "data_class": data_class, "cloud_budget_tokens": budget}


def trusted_device_pins(store: OnlineStore) -> list[dict]:
    """Return this endpoint's trust roots without consulting the cloud directory."""
    profile = store.profile()
    if profile is None:
        return []
    local = generate_device_key(_device_key_path(store))
    rows = [{"device_id": profile.device_id, "name": profile.device_name,
             "public_key": local["public_key"], "source": "this_device"}]
    for device_id, value in sorted((_read_trust(store).get("devices") or {}).items()):
        if device_id == profile.device_id or not isinstance(value, dict):
            continue
        rows.append({"device_id": device_id, "name": str(value.get("name") or device_id),
                     "public_key": str(value.get("public_key") or ""),
                     "trusted_at": int(value.get("trusted_at") or 0), "source": "local_pin"})
    return rows


def detect_capabilities(extra=()) -> list[str]:
    values = {"collie", "code", "os:%s" % platform.system().casefold()}
    try:
        from .browserbridge import _bridge_live
        if _bridge_live():
            values.add("browser")
    except Exception:
        pass
    try:
        from .native import backend
        if backend() is not None:
            values.add("desktop")
    except Exception:
        pass
    try:
        from .mcpclient import status
        for item in status():
            if item.get("enabled"):
                values.add("mcp:%s" % item.get("name"))
    except Exception:
        pass
    values.update(str(x).strip() for x in extra if str(x).strip())
    return sorted(values)


@dataclass
class MissionLease:
    node: "OnlineNode"
    mission: dict
    lost: bool = False
    last_error: str = ""

    @property
    def mission_id(self) -> str:
        return str(self.mission["id"])

    def _body(self, value: Optional[dict] = None) -> dict:
        return {"lease_id": self.mission["lease_id"],
                "fencing_token": self.mission["fencing_token"], **(value or {})}

    def checkpoint(self, value: Optional[dict] = None) -> dict:
        if self.lost:
            raise OnlineError("mission lease was lost: %s" % self.last_error)
        try:
            result = self.node.client._request(
                "POST", "/v1/missions/%s/checkpoint" % self.mission_id,
                self._body({"checkpoint": value or {}}))
            self.mission = result.get("mission") or self.mission
            return self.mission
        except Exception as exc:
            self.lost, self.last_error = True, "%s: %s" % (type(exc).__name__, exc)
            raise

    def commit_key(self, step_id: str) -> str:
        """Stable across fencing/lease replacement so an external commit deduplicates globally."""
        raw = "collie-mission-v1\0%s\0%s" % (self.mission_id, str(step_id or "default"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def finish(self, result: dict, *, success: bool = True) -> dict:
        if self.lost:
            raise OnlineError("refusing completion after lease loss: %s" % self.last_error)
        return self.node.client._request(
            "POST", "/v1/missions/%s/complete" % self.mission_id,
            self._body({"result": result or {}, "success": bool(success)}))

    def needs_you(self, detail: dict) -> dict:
        return self.node.client._request(
            "POST", "/v1/missions/%s/needs-you" % self.mission_id,
            self._body({"detail": detail or {}}))

    def wait(self, checkpoint: dict) -> dict:
        return self.node.client._request(
            "POST", "/v1/missions/%s/wait" % self.mission_id,
            self._body({"checkpoint": checkpoint or {}}))


class OnlineNode:
    def __init__(self, store: OnlineStore, *, name: str = "This device",
                 kind: str = "device", capabilities=(), lease_seconds: int = 90):
        if not store.connected():
            raise OnlineError("Online node needs Connected Mode")
        self.store = store
        self.client = OnlineClient(store)
        self.name = str(name or "This device")[:120]
        self.kind = str(kind or "device")
        self.capabilities = detect_capabilities(capabilities)
        self.lease_seconds = max(30, min(int(lease_seconds), 300))

    def trusted_devices(self) -> list[dict]:
        """Return locally pinned task issuers; the cloud directory is not a trust root."""
        return trusted_device_pins(self.store)

    def trust_device(self, device_id: str, public_key: str, *, name: str = "") -> dict:
        """Pin another issuer after an out-of-band/device-to-device verification ceremony."""
        device_id, public_key = str(device_id or "").strip(), _valid_public_key(public_key)
        if not _ID.match(device_id):
            raise ValueError("invalid trusted device id")
        profile = self.store.profile()
        local = generate_device_key(_device_key_path(self.store))
        if device_id == profile.device_id and not secrets.compare_digest(public_key, local["public_key"]):
            raise ValueError("this device id cannot be pinned to another key")
        value = _read_trust(self.store)
        prior = (value.get("devices") or {}).get(device_id) or {}
        if prior.get("public_key") and not secrets.compare_digest(str(prior["public_key"]), public_key):
            raise ValueError("trusted device key changed; remove the old pin before replacing it")
        row = {"public_key": public_key, "name": str(name or device_id)[:120],
               "trusted_at": int(time.time())}
        value.setdefault("devices", {})[device_id] = row
        _write_trust(self.store, value)
        return {"device_id": device_id, **row, "source": "local_pin"}

    def untrust_device(self, device_id: str) -> bool:
        device_id = str(device_id or "").strip()
        profile = self.store.profile()
        if device_id == profile.device_id:
            raise ValueError("this device's own execution key cannot be untrusted")
        value = _read_trust(self.store)
        removed = value.setdefault("devices", {}).pop(device_id, None) is not None
        if removed:
            _write_trust(self.store, value)
        return removed

    def _sign(self, intent: dict) -> dict:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            key = generate_device_key(_device_key_path(self.store))
            signature = Ed25519PrivateKey.from_private_bytes(
                _unb64(key["private_key"])).sign(_authorization_message(intent))
        except (ImportError, ValueError) as exc:
            raise OnlineError("mission authorization signing is unavailable") from exc
        return {"intent": intent, "signature": _b64(signature), "public_key": key["public_key"]}

    def _trusted_public_key(self, issuer_device_id: str) -> str:
        profile = self.store.profile()
        if issuer_device_id == profile.device_id:
            return generate_device_key(_device_key_path(self.store))["public_key"]
        row = (_read_trust(self.store).get("devices") or {}).get(issuer_device_id)
        return str(row.get("public_key") or "") if isinstance(row, dict) else ""

    def _verify_authorization(self, mission: dict) -> None:
        """Verify cloud-returned work against a locally trusted endpoint signature."""
        authorization = mission.get("authorization")
        if not isinstance(authorization, dict) or not isinstance(authorization.get("intent"), dict):
            raise OnlineError("mission has no endpoint authorization")
        intent = authorization["intent"]
        issuer = str(intent.get("issuer_device_id") or "")
        public_key = str(authorization.get("public_key") or "")
        trusted_key = self._trusted_public_key(issuer)
        if not trusted_key or not secrets.compare_digest(public_key, trusted_key):
            raise OnlineError("mission issuer is not trusted by this device")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(_unb64(public_key)).verify(
                _unb64(authorization.get("signature") or ""), _authorization_message(intent))
        except Exception as exc:
            raise OnlineError("mission endpoint signature is invalid") from exc
        now = int(time.time())
        issued_at, expires_at = int(intent.get("issued_at") or 0), int(intent.get("expires_at") or 0)
        if (intent.get("version") != _AUTH_VERSION or issued_at <= 0 or
                issued_at > now + _CLOCK_SKEW_SECONDS or expires_at < now or
                expires_at < issued_at or
                expires_at - issued_at > _MAX_AUTH_LIFETIME_SECONDS):
            raise OnlineError("mission authorization is expired or outside its validity window")
        if not _ID.match(str(intent.get("authorization_id") or "")):
            raise OnlineError("mission authorization id is invalid")
        profile = self.store.profile()
        if str(intent.get("workspace_id") or "") != profile.workspace_id:
            raise OnlineError("mission authorization belongs to another workspace")
        if str(intent.get("target_device_id") or "") != profile.device_id:
            raise OnlineError("mission authorization targets another execution endpoint")
        kind = str(intent.get("kind") or "")
        actual = _mission_values(
            goal=mission.get("goal"), payload=mission.get("payload"),
            required_capabilities=mission.get("required_capabilities") or (),
            fallback=mission.get("fallback") or "wait",
            data_class=mission.get("data_class") or "cloud_indexed",
            cloud_budget_tokens=int(mission.get("cloud_budget_tokens") or 0))
        if kind == "mission":
            expected = {"version": _AUTH_VERSION, "kind": "mission",
                        "authorization_id": intent.get("authorization_id"),
                        "mission_id": str(mission.get("id") or ""),
                        "workspace_id": profile.workspace_id,
                        "project_id": str(mission.get("project_id") or ""),
                        "target_device_id": profile.device_id, **actual,
                        "issuer_device_id": issuer, "issued_at": issued_at, "expires_at": expires_at}
        elif kind == "schedule":
            schedule_id, scheduled_for = str(mission.get("schedule_id") or ""), int(mission.get("scheduled_for") or 0)
            schedule = intent.get("schedule") if isinstance(intent.get("schedule"), dict) else {}
            template = intent.get("mission") if isinstance(intent.get("mission"), dict) else {}
            step = (86400 if schedule.get("cadence") == "daily" else
                    7 * 86400 if schedule.get("cadence") == "weekly" else 0)
            start = int(schedule.get("next_run_at") or 0)
            interval = int(schedule.get("interval") or 1)
            grace = int(schedule.get("occurrence_grace_seconds") or 0)
            if (schedule.get("cadence") not in ("once", "daily", "weekly") or
                    interval < 1 or interval > 365 or grace < 60 or grace > 7 * 86400):
                raise OnlineError("scheduled mission policy is invalid")
            aligned = (scheduled_for == start if not step else
                       scheduled_for >= start and (scheduled_for - start) % (step * interval) == 0)
            expected_id = "%s:%s" % (schedule_id, scheduled_for)
            if (schedule_id != str(intent.get("schedule_id") or "") or
                    str(intent.get("project_id") or "") != str(mission.get("project_id") or "") or
                    str(mission.get("id") or "") != expected_id or not aligned or
                    scheduled_for > now + _CLOCK_SKEW_SECONDS or
                    scheduled_for < now - grace or scheduled_for > expires_at):
                raise OnlineError("scheduled mission occurrence is outside its signed bounds")
            if _json(actual) != _json(template):
                raise OnlineError("scheduled mission was changed after endpoint authorization")
            expected = intent
        else:
            raise OnlineError("unsupported mission authorization kind")
        if kind == "mission" and _json(intent) != _json(expected):
            raise OnlineError("mission was changed after endpoint authorization")
        digest = hashlib.sha256(_authorization_message(intent) + _unb64(
            authorization.get("signature") or "")).hexdigest()
        if not self.store.accept_mission_authorization(str(mission.get("id") or ""), digest):
            raise OnlineError("mission authorization was already consumed on this device")

    @property
    def node_id(self) -> str:
        profile = self.store.profile()
        return "%s:%s" % (profile.workspace_id, profile.device_id)

    def heartbeat(self) -> dict:
        return self.client._request("POST", "/v1/nodes/heartbeat", {
            "name": self.name, "kind": self.kind, "capabilities": self.capabilities})["node"]

    def claim(self) -> Optional[MissionLease]:
        result = self.client._request("POST", "/v1/nodes/claim", {
            "node_id": self.node_id, "lease_seconds": self.lease_seconds})
        mission = result.get("mission")
        if not isinstance(mission, dict):
            return None
        lease = MissionLease(self, mission)
        try:
            self._verify_authorization(mission)
        except Exception as exc:
            # Marking Needs You is diagnostic only. Even if a hostile coordinator rejects this
            # state change, the local executor has already failed closed.
            try:
                lease.needs_you({"reason": "mission_authorization_invalid",
                                 "detail": str(exc)[:300]})
            except Exception:
                pass
            raise OnlineError("refusing cloud mission: %s" % exc) from exc
        return lease

    def submit(self, *, project_id: str, goal: str, payload: Optional[dict] = None,
               required_capabilities=(), fallback: str = "wait",
               data_class: str = "cloud_indexed", cloud_budget_tokens: int = 0,
               target_device_id: str = "") -> dict:
        profile = self.store.profile()
        project_id = str(project_id or "").strip()
        if not _ID.match(project_id):
            raise ValueError("valid project id is required")
        target_device_id = str(target_device_id or profile.device_id).strip()
        if not _ID.match(target_device_id):
            raise ValueError("valid target device id is required")
        mission_id, now = uuid.uuid4().hex, int(time.time())
        values = _mission_values(
            goal=goal, payload=payload, required_capabilities=required_capabilities,
            fallback=fallback, data_class=data_class, cloud_budget_tokens=cloud_budget_tokens)
        intent = {"version": _AUTH_VERSION, "kind": "mission",
                  "authorization_id": uuid.uuid4().hex, "mission_id": mission_id,
                  "workspace_id": profile.workspace_id, "project_id": project_id,
                  "target_device_id": target_device_id, **values,
                  "issuer_device_id": profile.device_id, "issued_at": now,
                  "expires_at": now + _MISSION_LIFETIME_SECONDS}
        return self.client._request("POST", "/v1/missions", {
            "mission_id": mission_id, "project_id": project_id,
            "target_device_id": target_device_id, **values,
            "authorization": self._sign(intent)})["mission"]

    def schedule(self, *, project_id: str, name: str, next_run_at: int,
                 cadence: str = "once", interval: int = 1, timezone: str = "UTC",
                 goal: str, payload: Optional[dict] = None, required_capabilities=(),
                 fallback: str = "wait", data_class: str = "cloud_indexed",
                 cloud_budget_tokens: int = 0,
                 occurrence_grace_seconds: int = _DEFAULT_OCCURRENCE_GRACE_SECONDS,
                 target_device_id: str = "") -> dict:
        """Create a recurring signed template; the coordinator may time it but cannot edit it."""
        profile = self.store.profile()
        project_id, cadence = str(project_id or "").strip(), str(cadence or "once")
        if not _ID.match(project_id):
            raise ValueError("valid project id is required")
        target_device_id = str(target_device_id or profile.device_id).strip()
        if not _ID.match(target_device_id):
            raise ValueError("valid target device id is required")
        if cadence not in ("once", "daily", "weekly"):
            raise ValueError("invalid schedule cadence")
        next_run_at, interval = int(next_run_at), int(interval)
        if next_run_at <= 0 or interval < 1 or interval > 365:
            raise ValueError("invalid schedule timing")
        grace = max(60, min(int(occurrence_grace_seconds), 7 * 86400))
        schedule_id, now = uuid.uuid4().hex, int(time.time())
        mission = _mission_values(
            goal=goal, payload=payload, required_capabilities=required_capabilities,
            fallback=fallback, data_class=data_class, cloud_budget_tokens=cloud_budget_tokens)
        schedule = {"name": str(name or goal)[:160], "cadence": cadence,
                    "interval": interval, "timezone": str(timezone or "UTC")[:80],
                    "next_run_at": next_run_at, "occurrence_grace_seconds": grace}
        intent = {"version": _AUTH_VERSION, "kind": "schedule",
                  "authorization_id": uuid.uuid4().hex, "schedule_id": schedule_id,
                  "workspace_id": profile.workspace_id, "project_id": project_id,
                  "target_device_id": target_device_id, "schedule": schedule, "mission": mission,
                  "issuer_device_id": profile.device_id, "issued_at": now,
                  "expires_at": now + _SCHEDULE_LIFETIME_SECONDS}
        return self.client._request("POST", "/v1/schedules", {
            "schedule_id": schedule_id, "project_id": project_id, **schedule,
            "target_device_id": target_device_id, "mission": mission,
            "authorization": self._sign(intent)})["schedule"]

    def run_once(self, executor: Callable[[dict, MissionLease], dict]) -> Optional[dict]:
        self.heartbeat()
        lease = self.claim()
        if lease is None:
            return None
        # Crossing leased -> running before user code is the uncertainty boundary. The Worker
        # never automatically re-leases an expired running mission.
        lease.checkpoint({"phase": "starting", "node": self.node_id})
        stop = threading.Event()

        def renew():
            interval = max(10, min(30, self.lease_seconds // 3))
            while not stop.wait(interval):
                try:
                    lease.checkpoint({"phase": "running", "heartbeat_at": int(time.time())})
                except Exception:
                    return

        thread = threading.Thread(target=renew, name="collie-online-lease", daemon=True)
        thread.start()
        try:
            result = executor(lease.mission, lease)
            if not isinstance(result, dict):
                result = {"output": str(result)}
            if lease.lost:
                return {"mission_id": lease.mission_id, "state": "uncertain",
                        "error": lease.last_error, "reconciliation_required": True}
            if result.pop("needs_user", False):
                return lease.needs_you(result).get("mission")
            if result.pop("waiting", False):
                return lease.wait(result).get("mission")
            return lease.finish(result, success=not bool(result.get("error"))).get("mission")
        except Exception as exc:
            if lease.lost:
                return {"mission_id": lease.mission_id, "state": "uncertain",
                        "error": lease.last_error, "reconciliation_required": True}
            return lease.finish({"error": "%s: %s" % (type(exc).__name__, exc)}, success=False).get("mission")
        finally:
            stop.set()
            thread.join(timeout=2)

    def serve(self, executor: Callable[[dict, MissionLease], dict], *, interval: float = 5,
              once: bool = False, on_result: Optional[Callable[[dict], None]] = None) -> None:
        interval = max(1.0, min(float(interval), 60.0))
        while True:
            result = self.run_once(executor)
            if result is not None and on_result is not None:
                on_result(result)
            if once:
                return
            time.sleep(interval)


def local_mission_executor(state_dir: str = ""):
    """Bridge a cloud Mission into Collie's existing durable local Mission runtime."""
    state_dir = state_dir or os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")

    def execute(mission: dict, lease: MissionLease) -> dict:
        from . import settings
        from .missionweb import MissionService
        settings.apply()
        payload = mission.get("payload") if isinstance(mission.get("payload"), dict) else {}
        svc = MissionService(state_dir=state_dir)
        try:
            started = svc.start(
                mission.get("goal") or "Online Mission", autonomous=payload.get("autonomous"),
                code=bool(payload.get("code")), workspace=str(payload.get("workspace") or ""),
                verify_command=str(payload.get("verify_command") or ""),
                provider=str(payload.get("provider") or ""), model=str(payload.get("model") or ""),
                runner=str(payload.get("runner") or ""))
            if started.get("error"):
                return {"error": started["error"]}
            lease.checkpoint({"phase": "local_mission", "local_mission_id": started["mission_id"]})
            out = svc.run(started["mission_id"])
            state = out.get("state")
            if state == "needs_user":
                return {"needs_user": True, "local_mission_id": started["mission_id"],
                        "reason": out.get("error") or out.get("result") or "local Mission needs you"}
            if state not in ("completed", "accepted"):
                return {"error": out.get("error") or "local Mission ended in %s" % state,
                        "local_mission_id": started["mission_id"]}
            return {"local_mission_id": started["mission_id"], "state": state,
                    "result": out.get("result") or ""}
        finally:
            svc.close()

    return execute
