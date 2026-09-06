"""MCP client — let collie consume external MCP servers (filesystem, github, sqlite, …) as tools.

This is the payoff of the two-tier registry: an MCP server may expose dozens of tools, and putting
all their schemas in the cached prefix would wreck collie's lean-prompt advantage. So MCP tools go
in the DEFERRED tier — advertised by name only — and the model pulls a schema with `load_tools`
exactly when it needs one.

Two costs to keep low:
  • startup — we must know each server's tool NAMES to advertise them, but spawning every server on
    every `collie` launch is wasteful. So tool lists are CACHED (~/.collie/mcp_cache.json, keyed by a
    hash of the server's launch config); startup reads the cache and spawns nothing. A cache miss
    does one synchronous list (and caches it).
  • per call — a server process is spawned LAZILY on the first tool call and then reused for the rest
    of the session (pooled by server name).

Transports (two, chosen per-server by config shape):
  • stdio  — JSON-RPC 2.0 over a spawned process's stdin/stdout, newline-delimited (NOT LSP framing).
  • remote — Streamable HTTP (spec 2025-03-26): one endpoint, JSON-RPC over POST, responses as JSON
    or a text/event-stream frame. Auth = a static `headers` block, or OAuth 2.1 (PKCE + dynamic client
    registration + loopback redirect) via `collie mcp login <name>`; tokens live in ~/.collie/mcp_tokens.json.

Config: ~/.collie/mcp.json (or $COLLIE_MCP_CONFIG):
    {"servers": {
       "fs":     {"command": "npx", "args": ["-y","@modelcontextprotocol/server-filesystem","/tmp"]},
       "linear": {"url": "https://mcp.linear.app/mcp"},                       # remote, OAuth
       "custom": {"url": "https://x/mcp", "headers": {"Authorization": "Bearer TOKEN"}}}}  # remote, static
No third-party deps — subprocess + json + threading + urllib only, on brand with collie.
"""
import atexit
import base64
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .tools import Tool

# SSRF guard shared with web_fetch: refuse hosts that resolve to loopback/private/link-local/CGNAT/
# reserved addresses. Reused here because OAuth discovery follows metadata URLs an untrusted remote
# MCP server steers, and a spawned/credentialed request to an internal address must not be possible.
try:
    from .webfetch import _addr_ok
except Exception:                        # keep the guard working even if webfetch is unavailable
    import ipaddress as _ipaddress
    import socket as _socket

    def _addr_ok(host):
        if not host:
            return False
        try:
            infos = _socket.getaddrinfo(host, None)
        except _socket.gaierror:
            return False
        for info in infos:
            try:
                a = _ipaddress.ip_address(info[4][0].split("%")[0])
            except ValueError:
                return False
            if (a.is_loopback or a.is_private or a.is_link_local or a.is_reserved
                    or a.is_multicast or a.is_unspecified):
                return False
        return True


def _safe_oauth_url(u):
    """True iff `u` is an https URL whose host is a normal public address. OAuth endpoints here can be
    named by an untrusted remote MCP server (protected-resource metadata points at its own auth
    server); requiring https + a public host stops that server from aiming discovery — or a later
    credentialed token POST — at loopback/internal infrastructure (SSRF)."""
    try:
        p = urllib.parse.urlsplit(u)
    except Exception:
        return False
    return p.scheme == "https" and bool(p.hostname) and _addr_ok(p.hostname)


# Minimal, non-secret env vars a spawned stdio MCP server is allowed to inherit. collie's own process
# holds every provider API key + OAuth token in os.environ; forwarding all of that to an arbitrary
# third-party server binary would hand it secrets it has no need for (see _child_env).
_ENV_ALLOW = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ",
    # Python and other native stdio servers need these benign runtime/location variables on
    # Windows.  In particular, omitting SystemRoot can make ``import _overlapped`` fail before an
    # MCP server reaches its initialize handshake.  APPDATA/LOCALAPPDATA let isolated CLIs find
    # their own user-level configuration; credentials remain excluded unless a server explicitly
    # declares one in its own ``env`` block.
    "SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
)


def _child_env(cfg):
    """Build a minimal allowlisted environment for a spawned stdio MCP server: a few benign runtime
    vars plus only the ones this server explicitly declared in its own `env` config — never the full
    os.environ (which carries collie's API keys and OAuth tokens)."""
    env = {}
    for k in _ENV_ALLOW:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    env.update({k: str(v) for k, v in (cfg.get("env") or {}).items()})
    return env


# Streamable-HTTP transport speaks the newer revision; stdio stays on the one it shipped with. The
# initialize handshake negotiates down if a server is older, so advertising 2025-03-26 is safe.
PROTOCOL_VERSION = "2024-11-05"
HTTP_PROTOCOL_VERSION = "2025-03-26"
_CONFIG = os.environ.get("COLLIE_MCP_CONFIG") or os.path.expanduser("~/.collie/mcp.json")
_CACHE = os.path.expanduser("~/.collie/mcp_cache.json")
_TOKENS = os.path.expanduser("~/.collie/mcp_tokens.json")   # OAuth tokens for remote servers (0600)
_CALL_TIMEOUT = float(os.environ.get("COLLIE_MCP_TIMEOUT", "60"))
_INIT_TIMEOUT = float(os.environ.get("COLLIE_MCP_INIT_TIMEOUT", "30"))
_MAX_CALL_TIMEOUT = float(os.environ.get("COLLIE_MCP_MAX_TIMEOUT", "900"))
_MAX_INLINE_IMAGE_B64 = int(os.environ.get("COLLIE_MCP_MAX_IMAGE_B64", str(16 * 1024 * 1024)))

# These official local tools wait longer than Collie's ordinary MCP deadline when their own
# ``timeout_seconds`` argument is omitted.  The client deadline must sit outside the server's or a
# healthy generation is reported as failed while it keeps running in the stdio child.  Explicit
# timeout_seconds values work for every MCP server and are still capped so a bad schema/argument
# cannot park a run indefinitely.
_COMFY_LOCAL_DEFAULT_TIMEOUTS = {
    "run_workflow": 125.0,
    "generate_image": 615.0,
    "partner_generate": 615.0,
    "run_template": 615.0,
    "job": 180.0,
    "launch_comfyui": 180.0,
    "restart_comfyui": 240.0,
    "update_comfyui": 615.0,
    "switch_comfyui_version": 615.0,
    "install_node": 615.0,
    "download_model": 615.0,
}

_ANNOTATION_BOOL_KEYS = (
    "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint",
)

# A readOnlyHint is still just server-provided metadata.  Pin both the endpoint and the known
# operation name before using it to relax Collie's default approval policy.  This also means a
# stale or hand-edited cache cannot turn submit_workflow into an approval-free call.
_COMFY_CLOUD_READONLY_TOOLS = frozenset({
    "get_job_status", "wait_for_job", "get_output", "get_queue",
    "get_batch_status", "get_batch_output", "wait_for_batch",
    "get_prompting_guide", "get_creative_technique",
    "search_models", "search_nodes", "get_node",
    "search_templates", "get_catalog_overview", "get_template",
    "cql", "get_template_schema", "apply_slots", "estimate_credits",
    "list_saved_workflows", "get_saved_workflow", "get_usage_report",
    "get_billing_activity",
})

# The official local server predates MCP annotations, so its advertised schemas currently carry no
# readOnlyHint/destructiveHint at all.  Keep a host-owned, name-pinned policy for the operations whose
# descriptions and contracts are unambiguously observational.  Unknown/new local tools stay external
# until reviewed, which preserves the MCP fail-closed default without asking before every status poll.
_COMFY_LOCAL_READONLY_TOOLS = frozenset({
    "server_info", "auth_status", "list_partner_models", "partner_model_schema",
    "system_stats", "get_logs", "discover", "which",
    "search_templates", "get_template", "nodes", "node_dependencies", "workflow_deps",
    "search_models", "validate_workflow", "list_workflow_slots", "list_workflow_notes",
})
_COMFY_LOCAL_WRITE_PATHS = {
    "emit_partner_workflow": "out_path",
    "fetch_template": "out_path",
    "fetch_outputs": "out_dir",
}
_COMFY_LOCAL_EXEC_TOOLS = frozenset({"generate_image", "free_memory"})

_POOL: dict = {}                 # server name -> _MCPConnection (lazy, reused within a process)
_POOL_LOCK = threading.Lock()


def _load_config():
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    servers = data.get("servers") or data.get("mcpServers") or {}
    return servers if isinstance(servers, dict) else {}


def _load_raw():
    """The whole config document and which key holds the servers, so a rewrite preserves the file's
    existing shape — both `servers` and the Claude-style `mcpServers` are accepted on read, and
    anything else in the file is left alone."""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return {}, "servers"
    if not isinstance(data, dict):
        return {}, "servers"
    key = "mcpServers" if (isinstance(data.get("mcpServers"), dict)
                           and not isinstance(data.get("servers"), dict)) else "servers"
    return data, key


def save_config(servers):
    """Write the server map back, atomically and owner-only. mcp.json can carry `Authorization`
    headers, so it is treated as a secret file the same way the token store is."""
    data, key = _load_raw()
    data[key] = servers
    os.makedirs(os.path.dirname(_CONFIG), exist_ok=True)
    tmp = _CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        from . import plat
        plat.chmod_private(tmp)
    except Exception:
        pass
    os.replace(tmp, _CONFIG)


def enabled(cfg):
    """Servers are on unless explicitly switched off. Absent means enabled, so every config written
    before this existed keeps working untouched — only `"enabled": false` disables."""
    return not (isinstance(cfg, dict) and cfg.get("enabled") is False)


def set_enabled(name, on):
    """Turn a server off without losing how it was configured — the point of a switch rather than a
    delete: 'is this server what is breaking startup' is answered by toggling, not by rebuilding the
    entry afterwards."""
    servers = _load_config()
    if name not in servers or not isinstance(servers[name], dict):
        return False
    cfg = dict(servers[name])
    if on:
        cfg.pop("enabled", None)          # back to the default rather than an explicit true
    else:
        cfg["enabled"] = False
    servers[name] = cfg
    save_config(servers)
    return True


# ---- the short list of servers you can just connect to ------------------------------------------
#
# Adding an MCP server used to mean knowing its URL, or knowing which npm package to spawn. Asked to
# "connect Slack", Collie reached for `@modelcontextprotocol/server-slack` — a stdio server that
# wants a bot token and a team id you have to go and mint in Slack's admin UI. Meanwhile Slack runs
# a remote endpoint that does OAuth: click, browser, authorize, done. The capability was already
# here (see login(), full OAuth 2.1 with PKCE and dynamic registration); what was missing was
# knowing the address.
#
# Every entry below was probed and answered 401 with a WWW-Authenticate: Bearer challenge, which is
# what an MCP endpoint that will do the browser handshake looks like. Anything that cannot be
# checked that way does not belong here — a directory that lists a URL nobody verified turns "one
# click" into "one click, then a mystery".
#
# `byo_client` marks the ones a press CANNOT finish. Answering 401 with a Bearer challenge — the
# test every entry here passed — proves the endpoint speaks OAuth. It does NOT prove a client can
# register itself, and without RFC 7591 dynamic registration there is no client_id to authorize
# with. Slack, HubSpot and GitHub advertise no registration_endpoint (checked 2026-08-04 against
# their live metadata: Slack sends you to slack.com/oauth/v2_user/authorize with nothing to
# register against), so `connect` on those dies at "no client_id" — after adding the server, which
# is exactly how a press comes to look like it did nothing.
#
# They stay listed rather than removed: the address is still the thing you would otherwise go
# hunting for, and knowing you need your own OAuth app IS the answer to "why did that not work".
CATALOG = {
    "slack":     {"url": "https://mcp.slack.com/mcp",        "label": "Slack", "byo_client": True},
    "linear":    {"url": "https://mcp.linear.app/mcp",       "label": "Linear"},
    "notion":    {"url": "https://mcp.notion.com/mcp",       "label": "Notion"},
    "sentry":    {"url": "https://mcp.sentry.dev/mcp",       "label": "Sentry"},
    "atlassian": {"url": "https://mcp.atlassian.com/v1/mcp", "label": "Jira & Confluence",
                  "aka": ("jira", "confluence")},
    "stripe":    {"url": "https://mcp.stripe.com",           "label": "Stripe"},
    "hubspot":   {"url": "https://mcp.hubspot.com/anthropic", "label": "HubSpot",
                  "byo_client": True},
    "vercel":    {"url": "https://mcp.vercel.com",           "label": "Vercel"},
    "neon":      {"url": "https://mcp.neon.tech/mcp",        "label": "Neon"},
    "github":    {"url": "https://api.githubcopilot.com/mcp/", "label": "GitHub",
                  "byo_client": True},
    # Comfy's first-party hosted server supports OAuth 2.1 dynamic client registration + PKCE,
    # so Collie can complete the entire connection from one press.  Discovery/search is free;
    # generation terms and credits remain Comfy's and are shown during its own account flow.
    "comfy-cloud": {"url": "https://cloud.comfy.org/mcp", "label": "Comfy Cloud",
                    "aka": ("comfy", "comfyui", "comfycloud")},
    # Eraser documents this remote endpoint as its first-party OAuth MCP server for creating,
    # reading and updating architecture diagrams and files.
    "eraser": {"url": "https://app.eraser.io/api/mcp", "label": "Eraser",
               "aka": ("eraserio", "diagram", "diagrams")},
}


BYO_PORT = 8898          # any free port; it only has to agree with what you register
CLIENT_ID_METADATA_URL = os.environ.get(
    "COLLIE_MCP_CLIENT_METADATA_URL", "https://collie.run/oauth-client-metadata.json")
_CIMD_REDIRECT_URIS = (
    "http://localhost:%d/callback" % BYO_PORT,
    "http://127.0.0.1:%d/callback" % BYO_PORT,
)


def _first_bindable_port(start=8890, stop=8990):
    """A loopback port this machine will actually accept, or None.

    Kept BELOW the ephemeral range (Windows hands those out at random, and a pinned port that gets
    handed to someone else breaks the pairing silently), and checked on both loopback families
    because a port can be refused on one and not the other.
    """
    import socket
    for p in range(start, stop):
        try:
            for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                s = socket.socket(fam, socket.SOCK_STREAM)
                try:
                    s.bind((addr, p))
                finally:
                    s.close()
            return p
        except OSError:
            continue
    return None


def byo_client_help(name, label, url):
    """What to do about a service that will not register a client for you.

    The sign-in itself is the ordinary one — their authorize page, their login, their Allow button.
    The ONLY missing piece is a client_id: this server's authorization metadata advertises no
    registration_endpoint, so collie cannot obtain one on the spot the way it does elsewhere. The
    clients that connect to it without this step (Claude, Cursor) are pre-registered with the
    provider — HubSpot's endpoint is literally .../anthropic — which is a relationship, not a
    protocol feature.
    """
    return ("%s will not register a client on the spot (its OAuth metadata advertises no "
            "registration_endpoint), so collie has no client_id to send and cannot open the "
            "authorize page. Everything after that is the normal flow — their page, your login, "
            "one Allow.\n"
            "  1. Create an app at the provider's developer console (%s) and copy its Client ID —\n"
            "     and its Client Secret, if the console shows one.\n"
            "  2. Register this exact redirect URL on it: http://localhost:%d/callback\n"
            "     (also add http://127.0.0.1:%d/callback if it will take both.)\n"
            "  3. Put both in ~/.collie/mcp.json:\n"
            '     "%s": {"url": "%s",\n'
            '                "client_id": "<your client id>",\n'
            '                "client_secret": "<your client secret, if it has one>",\n'
            '                "redirect_port": %d, "redirect_host": "localhost"}\n'
            "  4. `collie mcp connect %s` — the browser opens on their authorize page.\n"
            "  The port is pinned because the URL you register has to match the one collie listens "
            "on; without redirect_port it picks a fresh one every sign-in and nothing can match."
            % (label, label, BYO_PORT, BYO_PORT, name, url, BYO_PORT, name))


def known(name):
    """Look a service up by name, however it was typed. Returns its entry or None.

    Deliberately forgiving: the name arrives from a person saying "connect slack" or from a model
    that wrote "Slack MCP", and refusing on capitalisation would send both back to hunting for a URL.
    """
    key = (name or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if key in CATALOG:
        return dict(CATALOG[key], name=key)
    # People ask for the product, not the vendor: "connect jira" must not miss because the entry is
    # filed under Atlassian.
    for k, v in CATALOG.items():
        if key in tuple(v.get("aka", ())):
            return dict(v, name=k)
    for k, v in CATALOG.items():
        if key and (key in k or k in key or key == v["label"].lower().replace(" ", "")):
            return dict(v, name=k)
    return None


def catalog_connection_mode(hit, cfg=None):
    """Return how a catalog entry can obtain an OAuth client *right now*.

    ``byo_client`` records the last exercised provider behaviour, but OAuth metadata can evolve.
    Re-check only when the user actually presses Connect: a provider that has since adopted Client
    ID Metadata Documents or DCR should become one-click without waiting for a Collie release.
    """
    cfg = cfg or {}
    if cfg.get("client_id"):
        return "configured"
    if not hit or not hit.get("byo_client"):
        return "dynamic"
    try:
        meta = _discover_oauth(hit["url"])
    except Exception:
        return "manual"
    if meta.get("client_id_metadata_document_supported") is True:
        return "cimd"
    if meta.get("_metadata_discovered") and meta.get("registration_endpoint"):
        return "dynamic"
    return "manual"


def add_server(name, cfg, replace=False):
    """Add (or replace) one server. Returns an error string, or "" when it was written."""
    name = (name or "").strip()
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        return "server name must be alphanumeric (dashes and underscores allowed), got %r" % name
    if not isinstance(cfg, dict) or not (cfg.get("url") or cfg.get("command")):
        return "a server needs either `url` (remote) or `command` (stdio)"
    if cfg.get("url") and not str(cfg["url"]).startswith(("http://", "https://")):
        return "url must be http(s), got %r" % cfg.get("url")
    servers = _load_config()
    if name in servers and not replace:
        return "server %r already exists — pass replace to overwrite it" % name
    servers[name] = cfg
    save_config(servers)
    return ""


def remove_server(name):
    servers = _load_config()
    if name not in servers:
        return False
    servers.pop(name)
    save_config(servers)
    cache = _read_cache()                 # drop the advertised tool names with it
    if cache.pop(name, None) is not None:
        _write_cache(cache)
    toks = _load_tokens()                 # and the credential, so removing really removes
    if toks.pop(name, None) is not None:
        _save_tokens(toks)
    return True


def status():
    """What is configured and what state it is actually in — the one description of MCP that the CLI,
    the agent tools and the settings UI all read, so the three can never disagree."""
    servers = _load_config()
    toks = _load_tokens()
    cache = _read_cache()
    out = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        remote = _is_remote(cfg)
        static = any(str(k).lower() == "authorization" for k in (cfg.get("headers") or {}))
        if not remote:
            auth = "none"
        elif static:
            auth = "header"
        elif name in toks:
            auth = "oauth"
        else:
            auth = "login-needed"
        entry = cache.get(name) or {}
        fresh = entry.get("hash") == _cfg_hash(cfg)
        # The whole command line, not just the executable: "npx" says nothing about which server this
        # actually is, and identifying it is the entire point of the listing.
        target = cfg.get("url") or " ".join(
            [str(cfg.get("command") or "")] + [str(a) for a in (cfg.get("args") or [])]).strip()
        out.append({
            "name": name,
            "kind": "remote" if remote else "stdio",
            "target": str(target),
            "enabled": enabled(cfg),
            "auth": auth,
            # None (not 0) when the cache does not match this config: the tool count is genuinely
            # unknown until it is listed, and reporting 0 would read as "this server has no tools".
            "tools": len(entry.get("tools") or []) if fresh else None,
        })
    out.sort(key=lambda s: s["name"])
    return out


def server_configuration(name):
    """Return one enabled server's non-normalized config for a host-owned integration seam.

    Domain adapters should still invoke the server through MCP; this helper only lets a Collie
    surface read connection policy such as an allowed return-URL host list without reaching into
    the private config loader itself.
    """
    cfg = _load_config().get(str(name or ""))
    return dict(cfg) if isinstance(cfg, dict) and enabled(cfg) else {}


def server_has_tool(name, tool):
    """Check the cached contract without spawning a provider during a frequently-polled UI read."""
    cfg = server_configuration(name)
    if not cfg:
        return False
    entry = _read_cache().get(str(name or "")) or {}
    if entry.get("hash") != _cfg_hash(cfg):
        return False
    return any(str(row.get("name") or "") == str(tool or "")
               for row in (entry.get("tools") or []) if isinstance(row, dict))


def call_server_tool(name, tool, arguments=None, timeout=None):
    """Call one named MCP tool and return its raw MCP result.

    This is for host surfaces that need a narrow provider operation outside the model tool loop.
    It deliberately preserves MCP's structured result and error bit so the surface can validate
    every value before presenting it to a browser.
    """
    cfg = server_configuration(name)
    if not cfg:
        raise ValueError("no enabled MCP server named %r" % str(name or ""))
    conn = _get_conn(str(name), cfg)
    return conn.call_tool(str(tool), arguments if isinstance(arguments, dict) else {},
                          timeout=timeout or _tool_timeout(str(name), str(tool), arguments or {}))


def _cfg_hash(cfg):
    # The `enabled` switch is presentation, not identity: toggling a server off and on again must not
    # invalidate its cached tool list and force a re-spawn.
    if isinstance(cfg, dict) and "enabled" in cfg:
        cfg = {k: v for k, v in cfg.items() if k != "enabled"}
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


def _read_cache():
    try:
        with open(_CACHE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _write_cache(cache):
    try:
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        tmp = _CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, _CACHE)
    except OSError:
        pass


def _tool_record(tool):
    """The bounded part of a tools/list row Collie needs after the connection is closed.

    Tool annotations used to be discarded here.  Comfy Cloud publishes useful read-only and
    destructive hints, so preserve the standard booleans while dropping arbitrary extension data.
    They remain hints: only MCPTool._trusted_risk() decides whether a known first-party endpoint may
    use one to relax Collie's conservative default.
    """
    annotations = tool.get("annotations") if isinstance(tool, dict) else None
    safe_annotations = {}
    if isinstance(annotations, dict):
        for key in _ANNOTATION_BOOL_KEYS:
            if isinstance(annotations.get(key), bool):
                safe_annotations[key] = annotations[key]
        if isinstance(annotations.get("title"), str):
            safe_annotations["title"] = annotations["title"][:160]
    return {
        "name": tool.get("name"),
        "description": str(tool.get("description") or "")[:4000],
        "inputSchema": tool.get("inputSchema") or tool.get("input_schema"),
        **({"annotations": safe_annotations} if safe_annotations else {}),
    }


def _tool_timeout(server, tool, args):
    timeout = _CALL_TIMEOUT
    if server in ("comfy-local", "comfy-mcp"):
        timeout = max(timeout, _COMFY_LOCAL_DEFAULT_TIMEOUTS.get(tool, timeout))
    if isinstance(args, dict) and args.get("timeout_seconds") is not None:
        try:
            timeout = max(timeout, float(args["timeout_seconds"]) + 15.0)
        except (TypeError, ValueError):
            pass
    return max(1.0, min(_MAX_CALL_TIMEOUT, timeout))


def _paged_tool_list(fetch):
    """Collect a bounded tools/list cursor sequence for either MCP transport."""
    out, cursor, seen = [], None, set()
    for _ in range(100):
        result = fetch({"cursor": cursor} if cursor else {}) or {}
        rows = result.get("tools") or []
        if isinstance(rows, list):
            out.extend(row for row in rows if isinstance(row, dict))
        cursor = result.get("nextCursor") or result.get("next_cursor")
        if not cursor or cursor in seen or len(out) >= 10_000:
            break
        seen.add(cursor)
    return out[:10_000]


# ---------------------------------------------------------------- remote transport (Streamable HTTP)
def _is_remote(cfg):
    """A server config with a `url` is a remote (HTTP) server; otherwise it's a spawned stdio one."""
    return bool(isinstance(cfg, dict) and cfg.get("url"))


def _sse_extract(raw, want_id):
    """Pull the JSON-RPC response matching want_id out of an SSE body. Frames are `data: <json>` lines
    grouped by blank lines; server->client notifications (other/no id) are skipped. Falls back to the
    first response-shaped message if the id doesn't line up (some servers echo a string vs int id)."""
    fallback = None
    for block in raw.split("\n\n"):
        data = "\n".join(ln[5:].lstrip() for ln in block.splitlines() if ln.startswith("data:"))
        if not data.strip():
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("id") == want_id:
            return obj
        if fallback is None and ("result" in obj or "error" in obj):
            fallback = obj
    return fallback


# ---- OAuth 2.1 token store (PKCE + dynamic client registration; RFC 6749/7591/8414/9728) ----
def _load_tokens():
    try:
        with open(_TOKENS, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _save_tokens(toks):
    try:
        os.makedirs(os.path.dirname(_TOKENS), exist_ok=True)
        tmp = _TOKENS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(toks, f, indent=2)
        from . import plat
        plat.chmod_private(tmp)           # tokens are secrets — owner-only on POSIX, no-op on Windows
        os.replace(tmp, _TOKENS)
    except OSError:
        pass


def _get_token(name):
    return _load_tokens().get(name)


def _put_token(name, tok):
    toks = _load_tokens()
    toks[name] = tok
    _save_tokens(toks)


def _pkce():
    """(code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _http_json(url, data=None, headers=None, method=None, timeout=20, form=False):
    """One-shot JSON HTTP call (stdlib). `form=True` sends url-encoded (token endpoints), else JSON."""
    h = {"Accept": "application/json"}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if body else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8", "replace")
    return json.loads(txt) if txt.strip() else {}


def _discover_oauth(server_url):
    """Resolve a remote MCP server's OAuth endpoints per the MCP auth spec: protected-resource
    metadata (RFC 9728) points at the authorization server, whose metadata (RFC 8414) gives the
    authorization/token/registration endpoints. Conventional /.well-known fallbacks if a step 404s."""
    parts = urllib.parse.urlsplit(server_url)
    origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    as_url = origin
    scopes = []
    for prm in ("%s/.well-known/oauth-protected-resource%s" % (origin, parts.path.rstrip("/")),
                "%s/.well-known/oauth-protected-resource" % origin):
        try:
            doc = _http_json(prm) or {}
            # The RESOURCE says what may be asked for; the authorization server does not. A request
            # with no `scope` is granted nothing, and the sign-in then completes, stores a token,
            # and every call made with it is refused — which reads as a broken server rather than an
            # empty grant. Slack lists 29 of them here. Taken whether or not the authorization
            # server named below is accepted: they describe the resource either way.
            if doc.get("scopes_supported"):
                scopes = [str(s) for s in doc["scopes_supported"] if s]
            servers = doc.get("authorization_servers") or []
            cand = str(servers[0]).rstrip("/") if servers else ""
            # The authorization-server URL is chosen by the (untrusted) remote server — only accept it
            # if it's https + public, else fall back to the origin so we can't be pointed inward.
            if cand and _safe_oauth_url(cand):
                as_url = cand
                break
        except Exception:
            continue
    for meta in ("%s/.well-known/oauth-authorization-server" % as_url,
                 "%s/.well-known/openid-configuration" % as_url):
        try:
            doc = _http_json(meta) or {}
            # Validate the endpoints the metadata advertises too: the token_endpoint receives a
            # credentialed POST (auth code / refresh token), so it must not resolve to an internal host.
            if (doc.get("authorization_endpoint") and doc.get("token_endpoint")
                    and _safe_oauth_url(doc["authorization_endpoint"])
                    and _safe_oauth_url(doc["token_endpoint"])):
                doc["resource_scopes"] = scopes      # from the RESOURCE metadata, not this document
                doc["_metadata_discovered"] = True
                return doc
        except Exception:
            continue
    return {"authorization_endpoint": "%s/authorize" % as_url,      # last-ditch conventional guess
            "token_endpoint": "%s/token" % as_url,
            "registration_endpoint": "%s/register" % as_url,
            "resource_scopes": scopes, "_metadata_discovered": False}


def _client_creds(name, cfg=None):
    """The (client_id, client_secret) a byo-client server was configured with.

    A registered client is not always a public one. Slack's metadata says
    `token_endpoint_auth_methods_supported: ["client_secret_post"]`, so a client_id alone gets
    through the authorize page and its Allow button and then fails at the exchange — the one place
    where the failure can no longer be read as "I have not signed in yet".

    They live in mcp.json rather than the token store because they configure the client, not a
    session: they survive `logout` and a refresh a year from now still needs them. mcp.json is
    already written owner-only for exactly this class of secret.
    """
    cfg = cfg if cfg is not None else (_load_config().get(name) or {})
    return cfg.get("client_id") or "", cfg.get("client_secret") or ""


def _register_client(reg_endpoint, redirect_uri):
    """RFC 7591 dynamic client registration -> client_id (public/native client, no secret)."""
    return _http_json(reg_endpoint, data={
        "client_name": "collie", "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "application_type": "native"})


def _stamp_token(tok, client_id, token_endpoint):
    tok["client_id"] = client_id
    tok["token_endpoint"] = token_endpoint
    tok["obtained_at"] = int(time.time())
    return tok


def _access_token(name):
    """A currently-valid access token for a remote server, refreshing if within 60s of expiry.
    None if never authorized (caller then relies on static headers or raises a login hint)."""
    tok = _get_token(name)
    if not tok:
        return None
    exp = tok.get("obtained_at", 0) + int(tok.get("expires_in", 3600) or 3600)
    if time.time() < exp - 60:
        return tok.get("access_token")
    rt, te = tok.get("refresh_token"), tok.get("token_endpoint")
    if rt and te:
        try:
            form = {"grant_type": "refresh_token", "refresh_token": rt,
                    "client_id": tok.get("client_id", "")}
            # A confidential client authenticates on refresh with the same secret it used to get
            # the token. Leaving it out works until the first expiry and then logs the user out an
            # hour later, which reads as the server going flaky rather than as a missing field.
            _, secret = _client_creds(name)
            if secret:
                form["client_secret"] = secret
            new = _http_json(te, form=True, data=form)
            new.setdefault("refresh_token", rt)
            _put_token(name, _stamp_token(new, tok.get("client_id"), te))
            return new.get("access_token")
        except Exception:
            pass
    return tok.get("access_token")        # stale but let the server be the judge (it'll 401 if dead)


def login(name, cfg=None, timeout=300, announce=None):
    """Interactive OAuth 2.1 (auth-code + PKCE + loopback redirect). Opens a browser, catches the
    redirect on 127.0.0.1, exchanges the code, and stores the token. Returns the token dict.

    `timeout` bounds the wait for the redirect. `announce(auth_url)` is handed the address the
    moment it exists: a caller that is not a terminal — a tool call, a panel — needs somewhere to
    put it, because `webbrowser.open` can fail without raising (a headless box, a WSL shell with no
    BROWSER set) and the printed line then goes nowhere anyone is looking. Without it that failure
    is indistinguishable from a user who is simply slow to click, for five minutes."""
    import http.server
    import webbrowser
    cfg = cfg if cfg is not None else _load_config().get(name) or {}
    url = cfg.get("url")
    if not url:
        raise RuntimeError("mcp server %r is not remote (no url); OAuth applies to remote servers" % name)
    meta = _discover_oauth(url)
    holder = {}

    class _CB(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            holder["code"] = (q.get("code") or [None])[0]
            holder["state"] = (q.get("state") or [None])[0]
            holder["error"] = (q.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>collie: MCP authorized \xe2\x9c\x93 \xe2\x80\x94 you can close this tab.</h3>")

        def log_message(self, *a):
            pass

    # An EPHEMERAL port is right for a server that registers the client on the spot: it hands over
    # whatever address it just bound. It is unusable for one that does not, because that redirect
    # URI has to be typed into someone's app-management page BEFORE the first sign-in, and a port
    # that changes every time can never match. Providers match the redirect exactly (Slack: "must
    # match or be a subdirectory of a Redirect URL configured under App Management"), and the port
    # is part of the origin, not a subdirectory — so `redirect_port` pins it. `redirect_host` is
    # there because some providers accept `localhost` and not `127.0.0.1`; we always BIND loopback
    # and only vary the name in the URL.
    configured_client_id, client_secret = _client_creds(name, cfg)
    use_cimd = (not configured_client_id
                and meta.get("client_id_metadata_document_supported") is True)
    want = int(cfg.get("redirect_port") or os.environ.get("COLLIE_OAUTH_PORT")
               or (BYO_PORT if use_cimd else 0))
    host = str(cfg.get("redirect_host") or ("localhost" if use_cimd else "127.0.0.1"))
    try:
        srv = http.server.HTTPServer(("127.0.0.1", want), _CB)
    except OSError as e:
        if use_cimd:
            raise RuntimeError(
                "cannot listen on Collie's published OAuth callback port %d for %r (%s). "
                "Close another Collie sign-in or any process using that port and try again. "
                "This redirect is fixed by %s; to use another port, configure your own "
                "client_id and matching redirect_port for this server."
                % (want, name, e, CLIENT_ID_METADATA_URL))
        # "Free the port" is bad advice when nothing holds it that you can find. Windows can refuse
        # a port that `netstat` shows as empty and that is in no documented exclusion range —
        # WinNAT and WSL take blocks in the kernel — and it refuses it on ::1 too, so switching
        # family does not help. The only move left is a different port on BOTH sides, which is
        # tedious to pick by hand, so pick it here and name the exact URL to register.
        free = _first_bindable_port()
        raise RuntimeError(
            "cannot listen on the redirect port %d for %r (%s). Nothing may appear to hold it: "
            "Windows can reserve a port invisibly, on IPv4 and IPv6 alike. This port is half of a "
            "pair — the other half is registered on the provider's OAuth app — so both have to "
            "move together.%s"
            % (want, name, e,
               ("\n  %d is free right now. Set \"redirect_port\": %d in the server's config, and "
                "register http://%s:%d/callback as a Redirect URL on the app (remember to SAVE "
                "it — pasting alone does nothing)." % (free, free, host, free)) if free else ""))
    port = srv.server_address[1]
    redirect_uri = "http://%s:%d/callback" % (host, port)
    client_id = configured_client_id
    if use_cimd:
        if redirect_uri not in _CIMD_REDIRECT_URIS:
            srv.server_close()
            raise RuntimeError(
                "Client ID Metadata Documents require one of Collie's published redirects (%s); "
                "remove the custom redirect_host/redirect_port or configure your own client_id"
                % ", ".join(_CIMD_REDIRECT_URIS))
        client_id = CLIENT_ID_METADATA_URL
    if not client_id and meta.get("registration_endpoint"):
        client_id = (_register_client(meta["registration_endpoint"], redirect_uri) or {}).get("client_id")
    if not client_id:
        srv.server_close()
        raise RuntimeError("no client_id — server has no registration_endpoint; set client_id in config")
    verifier, challenge = _pkce()
    state = base64.urlsafe_b64encode(os.urandom(12)).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
              "resource": url}                                      # RFC 8707 — many MCP AS require it
    # Ask for what the resource says it has. Without this the authorize page shows "No scopes
    # requested", the user approves nothing, and the token that comes back is refused by every
    # call made with it — a sign-in that succeeds and buys nothing.
    scope = cfg.get("scope") or " ".join(meta.get("resource_scopes") or [])
    if scope:
        params["scope"] = scope
    auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
    print("Opening browser to authorize MCP server %r:\n  %s" % (name, auth_url))
    if announce:
        try:
            announce(auth_url)
        except Exception:
            pass
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    srv.timeout = max(1, int(timeout or 300))
    srv.handle_request()                                           # blocks until the redirect or timeout
    srv.server_close()
    if holder.get("error"):
        raise RuntimeError("authorization denied: %s" % holder["error"])
    code = holder.get("code")
    if not code:
        raise RuntimeError("no authorization code — nothing came back within %ds, or the browser "
                           "never opened" % srv.timeout)
    if holder.get("state") != state:
        raise RuntimeError("state mismatch — aborting (possible CSRF)")
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": verifier, "resource": url}
    if client_secret:
        form["client_secret"] = client_secret     # client_secret_post; PKCE still carries the proof
    tok = _http_json(meta["token_endpoint"], form=True, data=form)
    # Slack answers a refusal with HTTP 200 and {"ok": false, "error": …}, so without this a token
    # dict holding no token is stored and the sign-in reads as successful — the complaint arrives
    # much later, at the first tool call, as an unexplained 401.
    if tok.get("ok") is False or (not tok.get("access_token") and tok.get("error")):
        raise RuntimeError("the token endpoint refused the exchange: %s%s" % (
            tok.get("error") or tok,
            "" if client_secret else " (no client_secret was sent — some providers require one)"))
    if not tok.get("access_token") and isinstance(tok.get("authed_user"), dict):
        tok = dict(tok["authed_user"])            # Slack returns the user grant nested under this
    _put_token(name, _stamp_token(tok, client_id, meta["token_endpoint"]))
    return tok


class _HTTPConnection:
    """Streamable-HTTP MCP transport (spec 2025-03-26): one endpoint, JSON-RPC over POST; each response
    arrives as application/json OR a text/event-stream frame. Auth is a static `headers` block in the
    config, or an OAuth bearer from the token store (`collie mcp login <name>`). stdlib urllib only."""

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.url = cfg["url"]
        self.proc = None                  # duck-type parity with _MCPConnection (no process here)
        self._session_id = None
        self._id = 0
        self._initialized = False
        self._lock = threading.Lock()

    def alive(self):
        return self._initialized          # HTTP has no process to die; re-init is cheap if it lapses

    def _auth_headers(self):
        h = dict(self.cfg.get("headers") or {})
        # Never transmit bearer/Authorization credentials in cleartext to a non-loopback http:// URL —
        # they'd be exposed to anyone on the wire. Strip a static Authorization header and skip
        # attaching an OAuth token unless the transport is https (loopback http stays allowed for
        # local dev servers).
        parts = urllib.parse.urlsplit(self.url)
        insecure = parts.scheme != "https" and (parts.hostname or "") not in (
            "127.0.0.1", "::1", "localhost")
        if insecure:
            return {k: v for k, v in h.items() if k.lower() != "authorization"}
        if not any(k.lower() == "authorization" for k in h):
            tok = _access_token(self.name)
            if tok:
                h["Authorization"] = "Bearer " + tok
        return h

    def _post(self, payload, timeout):
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": HTTP_PROTOCOL_VERSION}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        headers.update(self._auth_headers())
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("mcp %s: unauthorized (401) — run `collie mcp login %s`"
                                   % (self.name, self.name))
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise RuntimeError("mcp %s: HTTP %s %s" % (self.name, e.code, detail))
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        ctype = (resp.headers.get("Content-Type") or "").lower()
        raw = resp.read().decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            return _sse_extract(raw, payload.get("id"))
        return json.loads(raw) if raw.strip() else None

    def _request(self, method, params, timeout):
        with self._lock:
            self._id += 1
            mid = self._id
        msg = self._post({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, timeout)
        if not msg:
            raise RuntimeError("mcp %s: empty response to %s" % (self.name, method))
        if msg.get("error"):
            raise RuntimeError("mcp %s error: %s" % (self.name, msg["error"].get("message", msg["error"])))
        return msg.get("result", {})

    def _notify(self, method, params=None):
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}, _CALL_TIMEOUT)

    def connect(self):
        if self._initialized:
            return
        self._request("initialize", {"protocolVersion": HTTP_PROTOCOL_VERSION, "capabilities": {},
                                     "clientInfo": {"name": "collie", "version": "1.0"}}, _INIT_TIMEOUT)
        try:
            self._notify("notifications/initialized")
        except Exception:
            pass
        self._initialized = True

    def list_tools(self):
        self.connect()
        return _paged_tool_list(
            lambda params: self._request("tools/list", params, _INIT_TIMEOUT))

    def call_tool(self, tool, arguments, timeout=None):
        self.connect()
        return self._request("tools/call", {"name": tool, "arguments": arguments or {}},
                             timeout or _CALL_TIMEOUT)

    def close(self):
        self._initialized = False


class _MCPConnection:
    """One live JSON-RPC-over-stdio session to a spawned MCP server. Thread-safe: a background
    reader thread demuxes responses by id; callers block on a per-request Event."""

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.proc = None
        self._id = 0
        self._pending = {}                 # id -> [event, result_holder]
        self._lock = threading.Lock()
        self._alive = False
        self._reader = None

    def _spawn(self):
        cmd = self.cfg.get("command")
        if not cmd:
            raise RuntimeError("mcp server %r has no 'command'" % self.name)
        argv = [cmd] + list(self.cfg.get("args") or [])
        # Minimal allowlisted env instead of the full os.environ — a third-party MCP server binary has
        # no business seeing collie's provider API keys / OAuth tokens (env-minimization).
        env = _child_env(self.cfg)
        from . import plat as _plat
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd=self.cfg.get("cwd") or None, bufsize=1, text=True,
            **_plat.no_window_kwargs())
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue                  # a notification from the server — ignore
                with self._lock:
                    slot = self._pending.get(mid)
                if slot:
                    slot[1] = msg
                    slot[0].set()
        except Exception:
            pass
        finally:
            self._alive = False
            # wake anyone still waiting so they fail fast instead of hanging to timeout
            with self._lock:
                for slot in self._pending.values():
                    if slot[1] is None:
                        slot[1] = {"error": {"message": "mcp server exited"}}
                    slot[0].set()

    def _send(self, obj):
        line = json.dumps(obj) + "\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _request(self, method, params, timeout):
        with self._lock:
            self._id += 1
            mid = self._id
            ev = threading.Event()
            self._pending[mid] = [ev, None]
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise TimeoutError("mcp %s: %s timed out after %ss" % (self.name, method, timeout))
        with self._lock:
            slot = self._pending.pop(mid, None)
        resp = slot[1] if slot else None
        if not resp:
            raise RuntimeError("mcp %s: no response to %s" % (self.name, method))
        if "error" in resp and resp["error"]:
            raise RuntimeError("mcp %s error: %s" % (self.name, resp["error"].get("message", resp["error"])))
        return resp.get("result", {})

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def connect(self):
        """Spawn + JSON-RPC handshake. Idempotent."""
        if self._alive and self.proc and self.proc.poll() is None:
            return
        self._spawn()
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "collie", "version": "1.0"},
        }, _INIT_TIMEOUT)
        self._notify("notifications/initialized")

    def list_tools(self):
        self.connect()
        return _paged_tool_list(
            lambda params: self._request("tools/list", params, _INIT_TIMEOUT))

    def call_tool(self, tool, arguments, timeout=None):
        self.connect()
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}},
                               timeout or _CALL_TIMEOUT)
        return result

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def close(self):
        self._alive = False
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try: self.proc.kill()
                except Exception: pass


def _make_conn(name, cfg):
    return _HTTPConnection(name, cfg) if _is_remote(cfg) else _MCPConnection(name, cfg)


def _get_conn(name, cfg):
    fingerprint = _cfg_hash(cfg)
    with _POOL_LOCK:
        c = _POOL.get(name)
        stale = c is not None and getattr(c, "_collie_cfg_hash", None) != fingerprint
        if c is None or stale or not c.alive():
            if stale:
                try:
                    c.close()
                except Exception:
                    pass
            c = _make_conn(name, cfg)
            c._collie_cfg_hash = fingerprint
            _POOL[name] = c
    return c


def close_all():
    with _POOL_LOCK:
        conns = list(_POOL.values()); _POOL.clear()
    for c in conns:
        c.close()


atexit.register(close_all)   # never leak a spawned MCP server past the collie process


def refresh_server(name):
    """Re-list one enabled server and atomically replace its cached tool contract."""
    cfg = _load_config().get(name)
    if not isinstance(cfg, dict):
        raise ValueError("no MCP server named %r" % name)
    if not enabled(cfg):
        raise ValueError("MCP server %r is switched off" % name)
    conn = _get_conn(name, cfg)
    tools = [_tool_record(t) for t in conn.list_tools() if t.get("name")]
    cache = _read_cache()
    cache[name] = {"hash": _cfg_hash(cfg), "tools": tools, "refreshed_at": int(time.time())}
    _write_cache(cache)
    return tools


def _fmt_result(result, ctx=None):
    """MCP tools/call result -> plain text for the model. content is a list of {type,text|...}."""
    if not isinstance(result, dict):
        return str(result)
    if result.get("isError"):
        prefix = "ERROR (from MCP tool): "
    else:
        prefix = ""
    parts = []
    attached = 0
    for block in result.get("content", []) or []:
        if not isinstance(block, dict):
            parts.append(str(block)); continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "image":
            data = block.get("data")
            media_type = str(block.get("mimeType") or block.get("mime_type") or "image/png")
            valid = (isinstance(data, str) and media_type.startswith("image/")
                     and len(data) <= _MAX_INLINE_IMAGE_B64 and attached < 8)
            if valid:
                try:
                    base64.b64decode(data, validate=True)
                except (ValueError, TypeError):
                    valid = False
            images = getattr(ctx, "images", None) if ctx is not None else None
            if valid and isinstance(images, list):
                images.append({"type": "image", "media_type": media_type, "data": data,
                               "label": "MCP output from %s" % getattr(ctx, "project", "Comfy"),
                               "source": "MCP image"})
                attached += 1
                parts.append("[image attached: %s]" % media_type)
            else:
                parts.append("[image returned but not attached: %s]" % media_type)
        elif block.get("type") == "audio":
            media_type = str(block.get("mimeType") or block.get("mime_type") or "audio")
            parts.append("[audio returned: %s; this Collie surface cannot attach audio yet]" % media_type)
        elif block.get("type") == "resource":
            r = block.get("resource", {})
            parts.append(r.get("text") or ("[resource %s]" % r.get("uri", "")))
        else:
            parts.append(json.dumps(block)[:500])
    structured = result.get("structuredContent") or result.get("structured_content")
    body = "\n".join(p for p in parts if p)
    if not body and structured is not None:
        body = json.dumps(structured, ensure_ascii=False)[:16_000]
    body = body or json.dumps(result, ensure_ascii=False)[:800]
    return prefix + body


class MCPTool(Tool):
    tier = "deferred"

    def __init__(self, server, cfg, tool_name, description, input_schema, annotations=None):
        # namespaced so two servers can expose a same-named tool without colliding
        self.name = "mcp__%s__%s" % (server, tool_name)
        self._server = server
        self._cfg = cfg
        self._remote = tool_name
        self._annotations = dict(annotations or {})
        self.description = (description or ("MCP tool %s" % tool_name))[:1000]
        self.schema = input_schema or {"type": "object", "properties": {}}

    def run(self, args, ctx):
        try:
            conn = _get_conn(self._server, self._cfg)
            call_args = args if isinstance(args, dict) else {}
            result = conn.call_tool(
                self._remote, call_args, timeout=_tool_timeout(self._server, self._remote, call_args))
        except Exception as e:
            return "ERROR: mcp call %s failed: %s: %s" % (self.name, type(e).__name__, e)
        return _fmt_result(result, ctx)

    def _official_comfy_local(self):
        if self._server not in ("comfy-local", "comfy-mcp"):
            return False
        command = str(self._cfg.get("command") or "")
        return os.path.basename(command).lower() in ("comfy-mcp", "comfy-mcp.exe")

    def _local_target_is_this_machine(self):
        remote_keys = ("COMFYUI_URL", "COMFYUI_HOST")
        cfg_env = self._cfg.get("env") if isinstance(self._cfg.get("env"), dict) else {}
        return not any(str(cfg_env.get(key) or os.environ.get(key) or "").strip()
                       for key in remote_keys)

    def _trusted_risk(self, args=None):
        """Host-owned risk policy for pinned official Comfy MCP connections."""
        args = args if isinstance(args, dict) else {}
        official = CATALOG["comfy-cloud"]["url"]
        if (self._server == "comfy-cloud" and self._cfg.get("url") == official
                and self._remote in _COMFY_CLOUD_READONLY_TOOLS
                and self._annotations.get("readOnlyHint") is True):
            return "read"
        if not self._official_comfy_local():
            return None
        if self._remote in _COMFY_LOCAL_READONLY_TOOLS:
            return "read"
        if self._remote == "job" and str(args.get("action") or "status").lower() in (
                "status", "error", "wait", "watch", "queue"):
            return "read"
        if self._remote == "download" and str(args.get("action") or "status").lower() in (
                "status", "wait"):
            return "read"
        if self._remote == "project" and str(args.get("action") or "status").lower() == "status":
            return "read"
        if self._remote == "set_workflow_slot" and args.get("stdout", True) is not False:
            return "read"
        if self._remote == "vary_workflow" and not args.get("out_dir"):
            return "read"
        if self._remote in _COMFY_LOCAL_WRITE_PATHS:
            return "write_local"
        if self._remote == "set_workflow_slot" and args.get("stdout", True) is False:
            return "write_local"
        if (self._remote in _COMFY_LOCAL_EXEC_TOOLS
                and self._local_target_is_this_machine()):
            return "exec"
        return None

    def _local_write_path(self, args):
        """The path mutated by a trusted local MCP write, for Gate root scoping."""
        if not self._official_comfy_local() or not isinstance(args, dict):
            return None
        field = _COMFY_LOCAL_WRITE_PATHS.get(self._remote)
        if self._remote == "set_workflow_slot" and args.get("stdout", True) is False:
            field = "workflow_path"
        if not field:
            return None
        return str(args.get(field) or "") or None

    def _trusted_target(self):
        """A concrete run-scoped approval target for official consequential Comfy calls."""
        if self._server == "comfy-cloud" and self._cfg.get("url") == CATALOG["comfy-cloud"]["url"]:
            return CATALOG["comfy-cloud"]["url"]
        if self._official_comfy_local():
            return "local ComfyUI"
        return None


# --------------------------------------------------------------- managing servers (agent-facing) --
# Adding an MCP server is not an ordinary edit: it hands collie a new set of tools, which is collie
# extending its OWN reach, and for a remote server it does so under the user's credentials. So the
# read is free and every write is gated behind the same just-in-time consent as desktop control —
# collie can propose a server and wire it up, but only after the user has said yes in words.
_MCP_CONSENT = (
    "REFUSED: managing MCP servers is gated off. This would %s, which changes the set of tools you "
    "yourself can call — and for a remote server it runs under the user's credentials. Ask the user "
    "in plain language whether to allow MCP management, say which server and what it grants, and "
    "ONLY if they agree call enable_capability(capability=\"mcp_manage\") and retry. Do not enable "
    "it on your own initiative.")

_MCP_DISCOVERY_CONSENT = (
    "REFUSED: public MCP Registry discovery is off. Local curated recommendations are still "
    "available and no private goal was sent anywhere. Searching the public Registry sends only "
    "generic capability labels such as 'calendar' or 'github' — never the user's raw goal, files, "
    "project names, or conversation. Ask once whether to allow this lookup and ONLY if the user "
    "agrees call enable_capability(capability=\"mcp_discovery\") and retry.")


def _mcp_manage_on(ctx=None):
    from .capability_policy import allowed
    return allowed("MCP_MANAGE", ctx)


def _mcp_discovery_on(ctx=None):
    from .capability_policy import allowed
    return allowed("MCP_DISCOVERY", ctx)


def _register_live(registry, name, cfg):
    """Put a newly added server's tools into the RUNNING registry, so it is usable this turn rather
    than after a restart. Returns a human summary, never raises: a server that cannot be listed is a
    normal outcome (not installed yet, needs OAuth) and must not undo the config change."""
    if registry is None:
        return "It will be available on the next collie run."
    try:
        tools = refresh_server(name)
    except Exception as e:
        return ("Could not list its tools yet (%s: %s) — if it is a remote server this usually means "
                "it needs `collie mcp login %s` first. The config is saved either way."
                % (type(e).__name__, e, name))
    if not tools:
        return "It connected but exposes no tools."
    for t in tools:
        registry.register(MCPTool(name, cfg, t["name"], t.get("description", ""),
                                  t.get("inputSchema"), t.get("annotations")))
    return ("%d tools are live NOW (no restart): %s. They are deferred — call load_tools with a name "
            "to get its schema." % (len(tools), ", ".join("mcp__%s__%s" % (name, t["name"])
                                                          for t in tools[:8])))


class MCPStatusTool(Tool):
    name, tier = "mcpctl_status", "always"
    description = ("List the MCP servers configured on this machine and what state each is in: "
                   "stdio or remote, its command/URL, whether it is switched on, whether it is "
                   "authenticated, and how many tools it advertises. Use this before assuming a "
                   "server is missing or broken — a server that is present but switched OFF, or "
                   "present but not logged in, looks identical to an absent one from the tool list "
                   "alone. No args.")
    schema = {"type": "object", "properties": {}}

    def run(self, args, ctx):
        rows = status()
        if not rows:
            return ("No MCP servers are configured (%s does not exist or is empty)." % _CONFIG)
        out = []
        for s in rows:
            bits = [s["kind"], s["target"][:70]]
            bits.append("ON" if s["enabled"] else "OFF (switched off — contributes no tools)")
            if s["auth"] == "login-needed":
                bits.append("NOT authenticated — call mcpctl_connect with name=%r to sign the user "
                            "in through their browser" % s["name"])
            elif s["auth"] in ("oauth", "header"):
                bits.append("authenticated (%s)" % s["auth"])
            bits.append("%s tools" % ("unknown, not listed yet" if s["tools"] is None else s["tools"]))
            out.append("%s: %s" % (s["name"], " · ".join(bits)))
        return "\n".join(out)


class MCPRefreshTool(Tool):
    name, tier = "mcpctl_refresh", "always"
    description = ("Refresh one configured MCP server's cached tool names, schemas and annotations. "
                   "Use this after the server was upgraded or its advertised tools changed. It only "
                   "reads tools/list and updates Collie's local cache; a new Collie run picks up the "
                   "replacement contract. Args: name.")
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def run(self, args, ctx):
        name = str((args if isinstance(args, dict) else {}).get("name", "")).strip()
        try:
            tools = refresh_server(name)
        except Exception as exc:
            return "ERROR: could not refresh %r: %s: %s" % (name, type(exc).__name__, exc)
        return ("Refreshed %r: %d tools cached. New and removed tools take effect on the next "
                "Collie run." % (name, len(tools)))


class MCPRecommendTool(Tool):
    name, tier = "mcpctl_recommend", "always"
    description = (
        "Find the smallest useful MCP connection set when the user's requested outcome needs an "
        "app or service that is not already connected. The goal is classified LOCALLY into generic "
        "capability labels and is never uploaded. First call with search_registry=false: this ranks "
        "Collie's host-owned catalog and reports any existing MCP tools. If no curated option fits, "
        "ask once before retrying with search_registry=true; that sends only allowlisted labels such "
        "as calendar/github/music to the public MCP Registry, whose entries are UNREVIEWED. This "
        "tool only recommends: it never installs, connects, signs in, or grants authority. Prefer "
        "an existing built-in or connection, recommend at most three choices, and explain data and "
        "effects before mcpctl_connect or mcpctl_connect_candidate. Args: goal, optional "
        "search_registry (bool), refresh (bool), max_results (1..10).")
    schema = {"type": "object", "properties": {
        "goal": {"type": "string", "maxLength": 4000},
        "search_registry": {"type": "boolean"}, "refresh": {"type": "boolean"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
    }, "required": ["goal"]}

    def run(self, args, ctx):
        a = args if isinstance(args, dict) else {}
        goal = str(a.get("goal") or "").strip()
        if not goal:
            return "ERROR: goal is required"
        public = a.get("search_registry") is True
        if public and not _mcp_discovery_on(ctx):
            return _MCP_DISCOVERY_CONSENT
        from .mcp_discovery import recommend
        try:
            result = recommend(
                goal, include_registry=public, refresh=a.get("refresh") is True,
                available_tools=(getattr(ctx, "registry", None).names()
                                 if getattr(ctx, "registry", None) is not None else []),
                max_results=max(1, min(int(a.get("max_results") or 3), 10)),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return "ERROR: MCP recommendation failed: %s: %s" % (type(exc).__name__, exc)
        for row in result.get("recommendations") or []:
            if row.get("source") == "collie_curated":
                row["next_action"] = "mcpctl_connect(name=%r) after explicit user approval" % row["name"]
            elif row.get("installability") == "review_and_connect":
                row["next_action"] = (
                    "mcpctl_connect_candidate(candidate_id=%r, confirmed=true) only after showing "
                    "the unreviewed warning and receiving explicit approval" % row["id"])
            else:
                row["next_action"] = "review only; Collie will not execute this local package"
        return json.dumps(result, ensure_ascii=False, indent=2)


class MCPAddTool(Tool):
    name, tier = "mcpctl_add", "always"
    description = ("Add an MCP server, giving yourself the tools it exposes. For a well-known "
                   "service — Slack, Linear, Notion, Sentry, Jira/Confluence, Stripe, HubSpot, "
                   "Vercel, Neon, GitHub, Comfy Cloud, Eraser — pass ONLY the name: Collie fills in the official remote "
                   "address, which signs in through the browser. Never send the user hunting for an "
                   "API token or a bot token for one of these. "
                   "Otherwise provide `url` for a "
                   "remote server (https://…) or `command` (plus optional `args`) for a stdio one. "
                   "The server's tools are registered immediately, so you can use them in this same "
                   "session. Requires the user's explicit agreement first — this expands what you "
                   "can do. Args: name, url OR command, optional args (array), optional env "
                   "(object), optional headers (object).")
    schema = {"type": "object", "properties": {
        "name": {"type": "string"}, "url": {"type": "string"}, "command": {"type": "string"},
        "args": {"type": "array", "items": {"type": "string"}},
        "env": {"type": "object"}, "headers": {"type": "object"}},
        "required": ["name"]}

    def run(self, args, ctx):
        a = args if isinstance(args, dict) else {}
        name = str(a.get("name", "")).strip()
        if not _mcp_manage_on(ctx):
            return _MCP_CONSENT % ("add the MCP server %r and register its tools for you" % name)
        cfg = {}
        for k in ("url", "command"):
            if a.get(k):
                cfg[k] = str(a[k])
        for k in ("args", "env", "headers"):
            if a.get(k):
                cfg[k] = a[k]
        catalogued = None
        if not cfg:
            # The description tells the model that a name is enough for a known service. It has to be
            # enough HERE, or the model does as it is told and gets "a server needs either url or
            # command" back — which reads as "the name was wrong" and sends it hunting for an npm
            # package, the exact behaviour the catalog was added to stop.
            catalogued = known(name)
            if not catalogued:
                return ("ERROR: %r is not a service Collie knows the address of, and no `url` or "
                        "`command` was given. Known services: %s. For anything else pass the remote "
                        "`url` (https://…) or the stdio `command`."
                        % (name, ", ".join(sorted(CATALOG))))
            name, cfg = catalogued["name"], {"url": catalogued["url"]}
        err = add_server(name, cfg, replace=False)
        if err:
            return "ERROR: %s" % err
        if catalogued:
            # Added, not authorized. Say the one thing that finishes it rather than leaving a server
            # that lists no tools and looks broken — and for the three that cannot finish at all,
            # say THAT instead of pointing at a tool which will only refuse.
            return ("Added MCP server %r (%s). Call mcpctl_connect with the same name: Collie "
                    "checks the provider's current OAuth metadata, uses browser sign-in when the "
                    "protocol permits it, and explains any provider-side client setup before "
                    "opening a dead flow." % (name, catalogued["label"]))
        return "Added MCP server %r. %s" % (name, _register_live(getattr(ctx, "registry", None), name, cfg))


class MCPConnectTool(Tool):
    name, tier = "mcpctl_connect", "always"
    description = ("Connect a well-known service in ONE step: Slack, Linear, Notion, Sentry, "
                   "Jira/Confluence, Stripe, HubSpot, Vercel, Neon, GitHub, Comfy Cloud or Eraser. Pass the name and "
                   "nothing else — Collie knows the official remote address, opens the user's "
                   "browser so they can authorize it, and registers the tools it exposes in THIS "
                   "session. This is the right tool for 'connect Slack' or 'can you use Linear': "
                   "never send the user to mint an API token or a bot token for one of these, and "
                   "never reach for an npm package that wants one. It BLOCKS while the user presses "
                   "Authorize, so tell them the browser is opening before you call it. Requires "
                   "their explicit agreement first. Args: name.")
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def run(self, args, ctx):
        a = args if isinstance(args, dict) else {}
        raw = str(a.get("name", "")).strip()
        if not _mcp_manage_on(ctx):
            return _MCP_CONSENT % ("connect the MCP server %r and sign in to it as the user" % raw)
        hit = known(raw)
        name = hit["name"] if hit else raw
        cfg = _load_config().get(name)
        if hit and catalog_connection_mode(hit, cfg) == "manual":
            # BEFORE the add, not after. Adding it first leaves a server in the config that can
            # never sign in, and the list then reads as one Sign-in press away from working.
            return "ERROR: " + byo_client_help(name, hit["label"], hit["url"])
        if not cfg and hit:
            err = add_server(name, {"url": hit["url"]}, replace=False)
            if err:
                return "ERROR: %s" % err
            cfg = {"url": hit["url"]}
        if not cfg:
            return ("ERROR: %r is not one of the services Collie knows an address for (%s), and no "
                    "server of that name is configured. Add it with mcpctl_add and its `url`."
                    % (raw, ", ".join(sorted(CATALOG))))
        if not cfg.get("url"):
            return ("ERROR: %r is a stdio server (it runs a command); there is nothing to sign in "
                    "to. It is already configured." % name)
        if {s["name"]: s for s in status()}.get(name, {}).get("auth") == "oauth":
            return ("MCP server %r is already authorized — nothing to sign in to. %s"
                    % (name, _register_live(getattr(ctx, "registry", None), name, cfg)))
        seen = {}
        try:
            # Bounded well under login()'s own 5 minutes: a tool call that hangs for five minutes is
            # indistinguishable from a hung agent, and the address is handed back either way so the
            # user can finish in their own time.
            login(name, cfg, timeout=180, announce=lambda u: seen.__setitem__("url", u))
        except Exception as e:
            return ("ERROR: the browser sign-in for %r did not finish (%s: %s).%s"
                    % (name, type(e).__name__, e,
                       (" Give the user this address and call mcpctl_connect again once they say "
                        "they have authorized it: %s" % seen["url"]) if seen.get("url") else ""))
        return ("Connected %r — the user authorized it in their browser and the token is stored. %s"
                % (name, _register_live(getattr(ctx, "registry", None), name, cfg)))


def prepare_registry_candidate(candidate_id):
    """Resolve and persist one exact cached HTTPS Registry candidate.

    The caller owns the explicit-consent UX.  The URL never comes from its request: it is recovered
    from the bounded local Registry cache by the immutable candidate id.
    """
    from .mcp_discovery import cached_candidate, candidate_config_name
    candidate = cached_candidate(candidate_id)
    if not candidate:
        raise ValueError("candidate is absent or stale; search the Registry again")
    remote = candidate.get("remote") or {}
    if candidate.get("installability") != "review_and_connect" or not remote.get("url"):
        raise ValueError(
            "this entry has no reviewable HTTPS remote; executable packages are review-only")
    if not _safe_oauth_url(remote["url"]):
        raise ValueError("remote endpoint does not resolve to a public HTTPS address")
    name = candidate_config_name(candidate)
    existing = _load_config().get(name)
    cfg = {"url": remote["url"], "_collie_registry": {
        "id": candidate["id"], "name": candidate.get("registry_name"),
        "version": candidate.get("version"), "repository": candidate.get("repository"),
        "trust": "community_unreviewed",
    }}
    if existing and existing.get("url") != cfg["url"]:
        raise ValueError("local MCP name collision for %r" % name)
    if not existing:
        err = add_server(name, cfg, replace=False)
        if err:
            raise ValueError(err)
    else:
        cfg = existing
    return candidate, name, cfg


def connect_registry_candidate(candidate_id, timeout=180):
    """Preflight an approved cached remote, OAuth if needed, and return its bounded tool list."""
    candidate, name, cfg = prepare_registry_candidate(candidate_id)
    try:
        tools = refresh_server(name)
    except Exception as first:
        message = str(first).casefold()
        if "unauthorized" not in message and "401" not in message:
            set_enabled(name, False)
            raise RuntimeError(
                "saved %r but switched it off because preflight failed (%s: %s)"
                % (name, type(first).__name__, first)) from first
        try:
            login(name, cfg, timeout=timeout)
            tools = refresh_server(name)
        except Exception:
            # A failed community connection must not stay active and look one harmless click away
            # from working.  Keep its provenance for inspection/removal, but contribute no tools.
            set_enabled(name, False)
            raise
    return candidate, name, cfg, tools


class MCPConnectCandidateTool(Tool):
    name, tier = "mcpctl_connect_candidate", "always"
    description = (
        "Connect one exact REMOTE candidate previously returned by mcpctl_recommend with "
        "search_registry=true. Public Registry entries are community metadata, not security "
        "reviews. Before calling, show the candidate's exact endpoint, publisher/repository, data, "
        "effects and warnings; obtain explicit user approval; then pass confirmed=true and the "
        "unchanged candidate_id. Collie resolves the URL from its local cache, never from model "
        "arguments, refuses executable packages, lists tools, performs OAuth if required, and keeps "
        "unknown effects behind the normal external-action gate. Args: candidate_id, confirmed.")
    schema = {"type": "object", "properties": {
        "candidate_id": {"type": "string", "maxLength": 500},
        "confirmed": {"type": "boolean"},
    }, "required": ["candidate_id", "confirmed"]}

    def run(self, args, ctx):
        a = args if isinstance(args, dict) else {}
        candidate_id = str(a.get("candidate_id") or "").strip()
        if a.get("confirmed") is not True:
            return "REFUSED: confirmed=true is required after the user reviews this exact candidate"
        if not _mcp_manage_on(ctx):
            return _MCP_CONSENT % ("connect the unreviewed Registry candidate %r" % candidate_id)
        try:
            _candidate, name, cfg, tools = connect_registry_candidate(candidate_id, timeout=180)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return ("ERROR: Registry connection did not finish (%s: %s). It has no live tools; "
                    "inspect or remove the disabled entry in Library."
                    % (type(exc).__name__, exc))
        registry = getattr(ctx, "registry", None)
        if registry is not None:
            for tool in tools:
                registry.register(MCPTool(name, cfg, tool["name"], tool.get("description", ""),
                                          tool.get("inputSchema"), tool.get("annotations")))
        return ("Connected unreviewed Registry candidate %r as %r. %d tools are live now. Unknown "
                "tool effects remain external-write by default; load only the tool needed for the "
                "user's stated Mission." % (candidate_id, name, len(tools)))


class MCPSetEnabledTool(Tool):
    name, tier = "mcpctl_set_enabled", "always"
    description = ("Switch a configured MCP server on or off without deleting how it was set up. "
                   "Switching one OFF is the safe way to test whether it is what is causing a "
                   "problem. Switching one ON expands the tools you can call, so it needs the user's "
                   "agreement the same way adding one does. Takes effect on the next collie run. "
                   "Args: name, enabled (bool).")
    schema = {"type": "object", "properties": {
        "name": {"type": "string"}, "enabled": {"type": "boolean"}},
        "required": ["name", "enabled"]}

    def run(self, args, ctx):
        a = args if isinstance(args, dict) else {}
        name, on = str(a.get("name", "")).strip(), bool(a.get("enabled"))
        # Switching OFF only ever reduces reach, so it does not need consent — being able to disable
        # a misbehaving server without a permission dance is the point of having a switch.
        if on and not _mcp_manage_on(ctx):
            return _MCP_CONSENT % ("switch the MCP server %r back on and give you its tools" % name)
        if not set_enabled(name, on):
            return "ERROR: no MCP server named %r (call mcpctl_status to see what exists)" % name
        return ("MCP server %r is now %s. This takes effect on the next collie run — the tools "
                "available in THIS session are unchanged." % (name, "ON" if on else "OFF"))


class MCPRemoveTool(Tool):
    name, tier = "mcpctl_remove", "always"
    description = ("Delete an MCP server's configuration, its cached tool list and any stored OAuth "
                   "token. This is irreversible — the user has to set the server up again. Prefer "
                   "mcpctl_set_enabled with enabled=false to switch one off temporarily. Requires the "
                   "user's explicit agreement. Args: name.")
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def run(self, args, ctx):
        name = str((args if isinstance(args, dict) else {}).get("name", "")).strip()
        if not _mcp_manage_on(ctx):
            return _MCP_CONSENT % ("delete the MCP server %r, including its stored credential" % name)
        if not remove_server(name):
            return "ERROR: no MCP server named %r (call mcpctl_status to see what exists)" % name
        return ("Removed MCP server %r — config, cached tool list and stored token. Its tools stay in "
                "this session's registry until the next run." % name)


def register_mcp_management(registry):
    """The manage-MCP tools. Registered ALWAYS, including when no server is configured — `mcpctl_add`
    with nothing set up yet is the case that matters most."""
    for t in (MCPStatusTool(), MCPRefreshTool(), MCPRecommendTool(), MCPAddTool(), MCPConnectTool(),
              MCPConnectCandidateTool(), MCPSetEnabledTool(), MCPRemoveTool()):
        registry.register(t)
    return True


def register_mcp_servers(registry):
    """Read config, advertise every server's tools in the DEFERRED tier. Uses the tool-list cache to
    avoid spawning servers at startup; a cache miss does a one-time synchronous list."""
    servers = _load_config()
    if not servers:
        return False
    cache = _read_cache()
    dirty = False
    n = 0
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or not enabled(cfg):
            continue        # switched off: contribute no tools, and never get spawned to list them
        h = _cfg_hash(cfg)
        entry = cache.get(name)
        tools = entry.get("tools") if entry and entry.get("hash") == h else None
        if tools is None:                       # cache miss / config changed -> list once, then cache
            try:
                conn = _get_conn(name, cfg)
                tools = [_tool_record(t) for t in conn.list_tools() if t.get("name")]
                cache[name] = {"hash": h, "tools": tools, "refreshed_at": int(time.time())}
                dirty = True
            except Exception:
                continue                        # a broken/unavailable server just contributes nothing
        for t in tools:
            registry.register(MCPTool(name, cfg, t["name"], t.get("description", ""),
                                      t.get("inputSchema"), t.get("annotations")))
            n += 1
    if dirty:
        _write_cache(cache)
    return n > 0
