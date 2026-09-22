"""Dynamic MCP tools backed by reviewed Collie Online connection definitions.

Only a reviewed, digest-pinned public manifest is cached on a device. Device-direct
credentials are E2E ciphertext in Online and open only at the calling endpoint; the
explicit cloud-proxy compatibility transport keeps credentials in the cloud Vault.
Authority v2 evaluates every call locally from the exact reviewed effect/action pair.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Optional

from .authority import ActionIntent, Effect
from .online import ConnectionBrokerClient, OnlineClient, OnlineStore
from .tools import Tool


_EFFECTS = {item.value: item for item in Effect}


def _alias(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return value[:48] or "online"


def _tool_name(connection: dict, remote: str) -> str:
    # The id suffix keeps two same-named shared connections from silently replacing one another.
    suffix = re.sub(r"[^a-z0-9]", "", str(connection.get("id") or "").casefold())[:8]
    server = "online_%s_%s" % (_alias(connection.get("name")), suffix or "shared")
    return "mcp__%s__%s" % (server, remote)


def _format_result(value, ctx) -> str:
    # The broker envelope contains the upstream JSON-RPC response in `result`.
    payload = value.get("result") if isinstance(value, dict) else value
    if isinstance(payload, dict) and payload.get("jsonrpc") and "result" in payload:
        payload = payload.get("result")
    try:
        from .mcpclient import _fmt_result
        if isinstance(payload, dict):
            return _fmt_result(payload, ctx)
    except Exception:
        pass
    return json.dumps(payload, ensure_ascii=False)[:16000] if not isinstance(payload, str) else payload[:16000]


class BrokerMCPTool(Tool):
    """One exact tool from a reviewed Collie Online connection manifest."""

    tier = "deferred"
    _authority_manifest_approved = True

    def __init__(self, connection: dict, manifest_tool: dict, *, store_path: Optional[str] = None):
        self._connection = dict(connection)
        self._manifest = dict(manifest_tool)
        self._server = str(connection.get("id") or "")
        self._remote = str(manifest_tool.get("name") or "")
        self._annotations = dict(manifest_tool.get("annotations") or {})
        self._store_path = store_path
        self.name = _tool_name(connection, self._remote)
        self.description = str(manifest_tool.get("description") or
                               ("MCP tool %s through Collie Online" % self._remote))[:1000]
        schema = manifest_tool.get("input_schema")
        self.schema = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}

    def _collie_intent(self, args) -> ActionIntent:
        effect = _EFFECTS.get(str(self._manifest.get("effect") or ""), Effect.RESTRICTED)
        action = str(self._manifest.get("action") or "external_change")[:100]
        return ActionIntent(
            action=action, effect=effect, target="connection:%s" % self._server,
            connection_id=self._server, reversible=effect in (Effect.OBSERVE, Effect.PREPARE, Effect.ACT),
            reason="reviewed Collie Online connection manifest")

    def run(self, args, ctx):
        call_args = args if isinstance(args, dict) else {}
        store = OnlineStore(self._store_path)
        try:
            if not store.connected():
                return "ERROR: this shared MCP tool needs Collie Connected Mode"
            broker = ConnectionBrokerClient(OnlineClient(store))
            policy = self._connection.get("policy") if isinstance(self._connection.get("policy"), dict) else {}
            allowed = set(str(x) for x in (policy.get("allowed_effects") or []))
            effect = str(self._manifest.get("effect") or "restricted")
            grant_id = ""
            # Reaching run() means the endpoint Gate already allowed this exact intent. Cloud-routed
            # calls add a server-side one-use grant; direct calls stay usable during cloud invoke
            # outages and rely on the endpoint Gate plus the local idempotency ledger.
            if effect not in allowed and self._connection.get("transport", "cloud_proxy") == "cloud_proxy":
                grant_id = str(broker.grant(self._server, self._remote, call_args).get("grant_id") or "")
                if not grant_id:
                    return "ERROR: Connection Broker did not issue the required action grant"
            raw_call_id = str(getattr(ctx, "tool_call_id", "") or "missing-call-id")
            profile = store.profile()
            idempotency = hashlib.sha256(("collie-online-v1\0%s\0%s\0%s\0%s" % (
                profile.device_id, raw_call_id, self._server, self._remote)).encode()).hexdigest()
            result = broker.invoke(self._server, self._remote, call_args,
                                   grant_id=grant_id, idempotency_key=idempotency)
            if result.get("reconciliation_required"):
                return ("ERROR: the broker cannot prove whether this action completed; inspect "
                        "the destination before retrying. receipt=%s" %
                        json.dumps(result, ensure_ascii=False)[:2000])
            if result.get("already_settled"):
                return ("Already completed earlier; Collie did not repeat the external action. "
                        "receipt=%s" % json.dumps(result, ensure_ascii=False)[:2000])
            return _format_result(result, ctx)
        except Exception as exc:
            return "ERROR: Collie Online MCP call failed: %s: %s" % (type(exc).__name__, exc)
        finally:
            store.close()


def register_broker_connections(registry, *, store_path: Optional[str] = None) -> list[str]:
    """Register only active manifests whose reviewed digest still matches exactly."""
    path = store_path or os.path.join(
        os.path.abspath(os.path.expanduser(os.environ.get("COLLIE_STATE_DIR") or "~/.collie")),
        "online.db")
    if not os.path.exists(path):
        return []
    store = OnlineStore(path)
    names = []
    try:
        if not store.connected():
            return []
        broker = ConnectionBrokerClient(OnlineClient(store))
        for connection in store.connections():
            if (connection.get("status") != "active" or not connection.get("manifest_digest") or
                    connection.get("reviewed_digest") != connection.get("manifest_digest")):
                continue
            manifest = connection.get("manifest")
            if not isinstance(manifest, list):
                continue
            if connection.get("transport") == "device_direct":
                try:
                    # Prove the ciphertext is bound to this exact endpoint and manifest before
                    # any cloud-supplied description enters the model-facing registry.
                    broker.get_credential(str(connection.get("id") or ""), connection=connection)
                except Exception:
                    continue
            for item in manifest:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                tool = BrokerMCPTool(connection, item, store_path=path)
                registry.register(tool)
                names.append(tool.name)
    finally:
        store.close()
    return names
