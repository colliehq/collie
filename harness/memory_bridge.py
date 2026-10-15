"""Standalone native-consumer port for explicitly configured Collie SQLite memory.

Only recall and pending proposal are exposed. A configured stable namespace is
never derived from cwd/basename or combined with global. No model, embedder,
EchoMem, migration/export job, or user application is launched by this bridge.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import uuid

from . import memory
from .evidence_bridge import EvidenceBridge, MAX_LINE, _decode, _error

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
BACKEND = "collie-sqlite"
VERSION = "0.1.0"
_SOURCE = "native-model-proposal"
_STRING_FIELDS = {"text": memory.MAX_FACT_CHARS, "keys": memory.MAX_KEYS_CHARS,
                  "source": 200, "evidence": 8000, "provenance": 8000,
                  "review_source": 200, "review_evidence": 8000, "review_provenance": 8000}


class MemoryBridgeError(RuntimeError):
    """An unavailable/refused operation is not an empty successful memory store."""


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise MemoryBridgeError("invalid registered " + label)
    return value


def _text(value, label, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or "\x00" in value
            or (not empty and not value.strip())):
        raise MemoryBridgeError("invalid bounded " + label)
    if memory._contains_secret(value):
        raise MemoryBridgeError("credential-like content is not admitted to memory")
    return value.strip()


class LocalMemoryService:
    """Memory-owned application adapter; configuration is trusted, tool inputs are not.

    A store_id is the user's stable identity for this database. Keep it bound to
    that same database when relocating it; choosing another database is not an
    automatic import, migration, or shared-memory grant.
    """
    def __init__(self, config):
        if (not isinstance(config, dict) or type(config.get("version")) is not int
                or config["version"] != 1
                or set(config) - {"version", "store_id", "db_path", "workspaces", "max_results"}):
            raise MemoryBridgeError("invalid local memory configuration")
        self.store_id = _identifier(config.get("store_id"), "store_id")
        path = config.get("db_path")
        if (not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path
                or not os.path.isdir(os.path.dirname(path)) or os.path.islink(path)):
            raise MemoryBridgeError("configure one absolute database in an existing user-owned directory")
        self.db_path = os.path.realpath(path)
        rows = config.get("workspaces")
        if not isinstance(rows, dict) or not rows:
            raise MemoryBridgeError("register at least one workspace namespace")
        self.namespaces = {}
        for workspace_id, row in rows.items():
            _identifier(workspace_id, "workspace_id")
            if not isinstance(row, dict) or set(row) != {"namespace"}:
                raise MemoryBridgeError("workspace registration accepts only its stable namespace")
            namespace = row["namespace"]
            if not isinstance(namespace, str) or not namespace.startswith("workspace:"):
                raise MemoryBridgeError("namespace must be workspace:<UUID>")
            try:
                canonical = "workspace:" + str(uuid.UUID(namespace[len("workspace:"):]))
            except ValueError as exc:
                raise MemoryBridgeError("namespace must contain a UUID") from exc
            if namespace != canonical or namespace in self.namespaces.values():
                raise MemoryBridgeError("namespace must be canonical and unique per registered workspace")
            self.namespaces[workspace_id] = namespace
        self.max_results = config.get("max_results", 5)
        if type(self.max_results) is not int or not 1 <= self.max_results <= 8:
            raise MemoryBridgeError("max_results must be a configured integer from 1 to 8")
        self._lock = threading.RLock()
        # The caller chose this store. No default ~/.collie path, embedding model,
        # distiller, or another memory backend is discovered.
        self.memory = memory.SqliteMemory(self.db_path, embedder=None)

    @classmethod
    def from_file(cls, config_path):
        if not isinstance(config_path, str) or not os.path.isabs(config_path):
            raise MemoryBridgeError("configuration path must be absolute")
        with open(config_path, encoding="utf-8") as stream:
            config = _decode(stream.read())
        return cls(config)

    def close(self):
        with self._lock:
            self.memory.close()

    def _namespace(self, workspace_id):
        _identifier(workspace_id, "workspace_id")
        if workspace_id not in self.namespaces:
            raise MemoryBridgeError("workspace_id was not registered by the user")
        return self.namespaces[workspace_id]

    def _boundary(self, namespace):
        # SqliteMemory intentionally also permits global project rows when a
        # caller grants their scope. Our consumer contract grants no such rows.
        # Refuse inconsistent metadata before recall can touch their counters.
        bad = self.memory.db.execute(
            "SELECT 1 FROM facts WHERE scope=? AND COALESCE(project,'')<>? LIMIT 1",
            (namespace, namespace)).fetchone()
        if bad:
            raise MemoryBridgeError("registered namespace contains inconsistent project metadata")

    def _ref(self, claim_id, namespace):
        identity = "%s\0%s\0%s\0%s" % (BACKEND, self.store_id, namespace, claim_id)
        return {"backend": BACKEND, "store_id": self.store_id,
                "opaque_id": uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}

    def _row(self, claim, namespace, *, pending=False):
        if (not isinstance(claim, dict) or type(claim.get("id")) is not int
                or claim["id"] <= 0 or claim.get("project") != namespace
                or claim.get("scope") != namespace
                or claim.get("superseded_by") is not None
                or claim.get("status") not in ({"proposed"} if pending else memory.RECALLABLE_STATUSES)):
            raise MemoryBridgeError("memory row crossed its registered metadata boundary")
        fields = {}
        for field, limit in _STRING_FIELDS.items():
            value = claim.get(field) or ""
            if (not isinstance(value, str) or len(value) > limit or "\x00" in value
                    or memory._contains_secret(value)):
                return None
            fields[field] = value
        return {"ref": self._ref(claim["id"], namespace), "text": fields["text"],
                "keys": fields["keys"], "scope": namespace, "status": claim["status"],
                "source": fields["source"], "evidence": fields["evidence"],
                "provenance": fields["provenance"],
                "review": {"source": fields["review_source"], "evidence": fields["review_evidence"],
                           "provenance": fields["review_provenance"]}}

    def recall(self, workspace_id, query):
        namespace = self._namespace(workspace_id)
        query = _text(query, "query", 2000)
        with self._lock:
            self._boundary(namespace)
            rows = self.memory.recall(query, project=namespace, k=self.max_results,
                                      statuses=memory.RECALLABLE_STATUSES,
                                      allowed_scopes=(namespace,))
            result, suppressed = [], 0
            for row in rows:
                # Re-read authoritative metadata: recall's projection omits
                # project, and model-facing consumers must not guess it.
                claim = self.memory.get_claim(row["id"])
                projected = self._row(claim, namespace)
                if projected is None:
                    suppressed += 1
                else:
                    result.append(projected)
            return {"accepted": True, "backend": BACKEND, "store_id": self.store_id,
                    "workspace_id": workspace_id, "scope": namespace, "memories": result,
                    "suppressed_unsafe_metadata": suppressed,
                    "global_included": False, "pending_included": False}

    def propose(self, workspace_id, text, keys=""):
        namespace = self._namespace(workspace_id)
        text = _text(text, "text", memory.MAX_FACT_CHARS)
        keys = _text(keys, "keys", memory.MAX_KEYS_CHARS, empty=True)
        evidence = json.dumps({"kind": "unreviewed_model_proposal", "received_at": time.time()},
                              sort_keys=True, separators=(",", ":"))
        provenance = json.dumps({"backend": BACKEND, "store_id": self.store_id,
                                 "workspace_id": workspace_id, "scope": namespace,
                                 "proposal_id": uuid.uuid4().hex}, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._boundary(namespace)
            try:
                claim_id = self.memory.propose(text, keys=keys, project=namespace,
                                               scope=namespace, source=_SOURCE,
                                               evidence=evidence, provenance=provenance,
                                               consolidate=False)
                if type(claim_id) is not int or claim_id <= 0:
                    raise MemoryBridgeError("proposal was rejected by the store")
                # An independent connection proves commit visibility. A failed
                # write/commit must not turn a volatile INSERT into accepted.
                with sqlite3.connect(Path(self.db_path).as_uri() + "?mode=ro", uri=True, timeout=3) as witness:
                    witness.row_factory = sqlite3.Row
                    raw = witness.execute("SELECT * FROM facts WHERE id=?", (claim_id,)).fetchone()
                    claim = dict(raw) if raw else None
                projected = self._row(claim, namespace, pending=True)
                if (projected is None or claim["source"] != _SOURCE
                        or claim["evidence"] != evidence or claim["provenance"] != provenance
                        or claim["text"] != text or claim["keys"] != keys):
                    raise MemoryBridgeError("durable proposal metadata did not match its producer")
            except Exception:
                try:
                    self.memory.db.rollback()
                except Exception:
                    pass
                raise
            return {"accepted": True, "pending": True, "workspace_id": workspace_id,
                    "memory": projected, "recallable": False,
                    "review_required": "separate trusted local host/user review; this bridge cannot promote"}


def _definition(name, description, properties, required):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False}}


TOOLS = (
    _definition("recall", "Recall accepted Collie local memories only in a user-registered stable workspace namespace. No global scope or model-selected database/scope/status is granted.",
                {"workspace_id": {"type": "string", "minLength": 1, "maxLength": 64},
                 "query": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["workspace_id", "query"]),
    _definition("propose", "Submit a non-recallable pending model proposal. Source and producer metadata are generated by the bridge; only a separate trusted reviewer can attest or verify it.",
                {"workspace_id": {"type": "string", "minLength": 1, "maxLength": 64},
                 "text": {"type": "string", "minLength": 1, "maxLength": memory.MAX_FACT_CHARS},
                 "keys": {"type": "string", "maxLength": memory.MAX_KEYS_CHARS}}, ["workspace_id", "text"]),
)


class MemoryBridge(EvidenceBridge):
    """Reuse core envelope validation; all memory tool dispatch stays here."""
    def handle(self, request):
        if isinstance(request, dict) and request.get("method") == "tools/call":
            envelope = super().handle(dict(request, method="ping"))
            if envelope is None or "error" in envelope:
                return envelope
            request_id = request["id"]
            if not self.initialized:
                return _error(request_id, -32002, "Initialize this MCP connection first")
            params = request.get("params", {})
            name, args = params.get("name"), params.get("arguments", {})
            definition = next((tool for tool in TOOLS if tool["name"] == name), None)
            if definition is None:
                return _error(request_id, -32602, "Unknown memory tool")
            schema = definition["inputSchema"]
            if (not isinstance(args, dict) or set(args) - set(schema["properties"])
                    or not set(schema["required"]).issubset(args)
                    or any(not isinstance(value, str) or
                           not spec.get("minLength", 0) <= len(value) <= spec["maxLength"]
                           for key, value in args.items()
                           for spec in (schema["properties"][key],))):
                return _error(request_id, -32602, "Only declared workspace/query/proposal fields are accepted")
            try:
                data = getattr(self.service, name)(**args)
                result = {"isError": False, "structuredContent": data,
                          "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, allow_nan=False)}]}
            except Exception as exc:
                result = {"isError": True, "content": [{"type": "text", "text":
                    "Local memory request refused or unavailable (%s); no success was acknowledged. Inspect the registered local store before retrying." % type(exc).__name__}]}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        response = super().handle(request)
        if response and "result" in response and isinstance(request, dict):
            if request.get("method") == "initialize":
                response["result"]["serverInfo"] = {"name": "collie-local-memory", "version": VERSION}
                response["result"]["instructions"] = (
                    "Use only user-registered workspace IDs. Recall never includes pending/global. Model proposals are unreviewed and cannot be promoted here. Preserve opaque backend/store_id/ref and source/evidence; do not infer execution verification from prose.")
            elif request.get("method") == "tools/list":
                response["result"] = {"tools": list(TOOLS)}
        return response


def serve_memory(service, input_stream=None, output_stream=None):
    incoming, outgoing = input_stream or sys.stdin, output_stream or sys.stdout
    bridge = MemoryBridge(service)
    while True:
        line = incoming.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            response = _error(None, -32600, "MCP input exceeds the message bound")
        elif not line.strip():
            continue
        else:
            try:
                response = bridge.handle(_decode(line))
            except (ValueError, UnicodeError):
                response = _error(None, -32700, "Parse error")
        if response is not None:
            outgoing.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
            outgoing.flush()
        if len(line) > MAX_LINE:
            break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("COLLIE_MEMORY_CONFIG"))
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("set an explicit --config or COLLIE_MEMORY_CONFIG")
    try:
        service = LocalMemoryService.from_file(args.config)
    except Exception as exc:
        sys.stderr.write("Local memory configuration rejected (%s).\n" % type(exc).__name__)
        return 2
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    try:
        serve_memory(service)
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
