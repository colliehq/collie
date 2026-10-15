"""Library/consumer adapter: minimal stdio MCP for Task Handoff and Evidence.

Start with explicit trusted local configuration and state outside workspaces.
This bridge never launches a model, a native agent session, or a paid API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .task_evidence_service import TaskEvidenceService

VERSION = "0.1.0"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_LINE = 1_048_576


def _tool(name, description, fields):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": {
                field: {"type": "string", "minLength": 1, "maxLength": 128} for field in fields},
                            "required": list(fields), "additionalProperties": False}}


TOOLS = (
    _tool("open_task", "Open evidence for a user-preconfigured workspace and check. No goal, path, command or authorization is accepted from the model.", ("workspace_id", "check_id")),
    _tool("verify_task", "Run only the task's registered check and durably record its scoped evidence. Unknown effects remain fenced; this does not run the host agent.", ("task_id",)),
    _tool("prepare_handoff", "Recompute scoped freshness and return sourced task/check evidence for another conversation. Does not migrate a native session or certify unobserved tools.", ("task_id",)),
)


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _decode(line):
    return json.loads(line, object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class EvidenceBridge:
    def __init__(self, service):
        self.service = service
        self.initialized = False

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        if "id" in request and (isinstance(request_id, bool) or not isinstance(request_id, (str, int, type(None)))):
            return _error(None, -32600, "Invalid request ID")
        method = request.get("method")
        if not isinstance(method, str):
            return _error(request_id, -32600, "Invalid method")
        if "id" not in request:
            # No effects may be smuggled into a tools/call notification.
            return None
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Params must be an object")
        if method == "initialize":
            protocol = params.get("protocolVersion")
            if not isinstance(protocol, str):
                return _error(request_id, -32602, "protocolVersion is required")
            self.initialized = True
            result = {"protocolVersion": protocol if protocol in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "collie-task-evidence", "version": VERSION},
                      "instructions": "Use registered IDs only. Native agents retain execution ownership. Handoff verified=true only describes the configured check at the recorded snapshot; stale/unknown is not approval to replay."}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return _error(request_id, -32002, "Initialize this MCP connection first")
        elif method == "tools/list":
            result = {"tools": list(TOOLS)}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            definition = next((item for item in TOOLS if item["name"] == name), None)
            if definition is None:
                return _error(request_id, -32602, "Unknown tool")
            expected = set(definition["inputSchema"]["required"])
            if (not isinstance(args, dict) or set(args) != expected or
                    any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in args.values())):
                return _error(request_id, -32602, "Only the declared registered IDs are accepted")
            try:
                data = getattr(self.service, name)(**args)
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, allow_nan=False)}],
                          "structuredContent": data, "isError": False}
            except Exception as exc:
                # Never echo arbitrary configuration, commands or paths in errors.
                result = {"content": [{"type": "text", "text": "Task evidence request failed (%s); no success was acknowledged. Inspect local state/configuration before retrying." % type(exc).__name__}],
                          "isError": True}
        else:
            return _error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(service, input_stream=None, output_stream=None):
    incoming, outgoing = input_stream or sys.stdin, output_stream or sys.stdout
    bridge = EvidenceBridge(service)
    while True:
        line = incoming.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            response = _error(None, -32600, "MCP input exceeds the message bound")
            outgoing.write(json.dumps(response) + "\n")
            outgoing.flush()
            break
        if not line.strip():
            continue
        try:
            response = bridge.handle(_decode(line))
        except (ValueError, UnicodeError):
            response = _error(None, -32700, "Parse error")
        if response is not None:
            outgoing.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
            outgoing.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("COLLIE_TASK_EVIDENCE_CONFIG"))
    parser.add_argument("--state-dir", default=os.environ.get("COLLIE_TASK_EVIDENCE_STATE"))
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("set explicit --config (or COLLIE_TASK_EVIDENCE_CONFIG); state_dir must be configured explicitly")
    try:
        service = TaskEvidenceService.from_file(args.config, args.state_dir)
    except Exception as exc:
        sys.stderr.write("Evidence bridge configuration rejected (%s).\n" % type(exc).__name__)
        return 2
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    serve(service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
