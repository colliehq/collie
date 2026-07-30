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
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ")


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
    for prm in ("%s/.well-known/oauth-protected-resource%s" % (origin, parts.path.rstrip("/")),
                "%s/.well-known/oauth-protected-resource" % origin):
        try:
            servers = (_http_json(prm) or {}).get("authorization_servers") or []
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
                return doc
        except Exception:
            continue
    return {"authorization_endpoint": "%s/authorize" % as_url,      # last-ditch conventional guess
            "token_endpoint": "%s/token" % as_url,
            "registration_endpoint": "%s/register" % as_url}


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
            new = _http_json(te, form=True, data={
                "grant_type": "refresh_token", "refresh_token": rt,
                "client_id": tok.get("client_id", "")})
            new.setdefault("refresh_token", rt)
            _put_token(name, _stamp_token(new, tok.get("client_id"), te))
            return new.get("access_token")
        except Exception:
            pass
    return tok.get("access_token")        # stale but let the server be the judge (it'll 401 if dead)


def login(name, cfg=None):
    """Interactive OAuth 2.1 (auth-code + PKCE + loopback redirect). Opens a browser, catches the
    redirect on 127.0.0.1, exchanges the code, and stores the token. Returns the token dict."""
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

    srv = http.server.HTTPServer(("127.0.0.1", 0), _CB)
    port = srv.server_address[1]
    redirect_uri = "http://127.0.0.1:%d/callback" % port
    client_id = cfg.get("client_id")
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
    if cfg.get("scope"):
        params["scope"] = cfg["scope"]
    auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
    print("Opening browser to authorize MCP server %r:\n  %s" % (name, auth_url))
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    srv.timeout = 300
    srv.handle_request()                                           # blocks until the redirect or timeout
    srv.server_close()
    if holder.get("error"):
        raise RuntimeError("authorization denied: %s" % holder["error"])
    code = holder.get("code")
    if not code:
        raise RuntimeError("no authorization code (timed out after 5 min, or the browser never returned)")
    if holder.get("state") != state:
        raise RuntimeError("state mismatch — aborting (possible CSRF)")
    tok = _http_json(meta["token_endpoint"], form=True, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier, "resource": url})
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
        return self._request("tools/list", {}, _INIT_TIMEOUT).get("tools", []) or []

    def call_tool(self, tool, arguments):
        self.connect()
        return self._request("tools/call", {"name": tool, "arguments": arguments or {}}, _CALL_TIMEOUT)

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
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd=self.cfg.get("cwd") or None, bufsize=1, text=True)
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
        result = self._request("tools/list", {}, _INIT_TIMEOUT)
        return result.get("tools", []) or []

    def call_tool(self, tool, arguments):
        self.connect()
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}}, _CALL_TIMEOUT)
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
    with _POOL_LOCK:
        c = _POOL.get(name)
        if c is None or not c.alive():
            c = _make_conn(name, cfg)
            _POOL[name] = c
    return c


def close_all():
    with _POOL_LOCK:
        conns = list(_POOL.values()); _POOL.clear()
    for c in conns:
        c.close()


atexit.register(close_all)   # never leak a spawned MCP server past the collie process


def _fmt_result(result):
    """MCP tools/call result -> plain text for the model. content is a list of {type,text|...}."""
    if not isinstance(result, dict):
        return str(result)
    if result.get("isError"):
        prefix = "ERROR (from MCP tool): "
    else:
        prefix = ""
    parts = []
    for block in result.get("content", []) or []:
        if not isinstance(block, dict):
            parts.append(str(block)); continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "resource":
            r = block.get("resource", {})
            parts.append(r.get("text") or ("[resource %s]" % r.get("uri", "")))
        else:
            parts.append(json.dumps(block)[:500])
    body = "\n".join(p for p in parts if p) or json.dumps(result)[:800]
    return prefix + body


class MCPTool(Tool):
    tier = "deferred"

    def __init__(self, server, cfg, tool_name, description, input_schema):
        # namespaced so two servers can expose a same-named tool without colliding
        self.name = "mcp__%s__%s" % (server, tool_name)
        self._server = server
        self._cfg = cfg
        self._remote = tool_name
        self.description = (description or ("MCP tool %s" % tool_name))[:1000]
        self.schema = input_schema or {"type": "object", "properties": {}}

    def run(self, args, ctx):
        try:
            conn = _get_conn(self._server, self._cfg)
            result = conn.call_tool(self._remote, args if isinstance(args, dict) else {})
        except Exception as e:
            return "ERROR: mcp call %s failed: %s: %s" % (self.name, type(e).__name__, e)
        return _fmt_result(result)


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


def _mcp_manage_on():
    return os.environ.get("COLLIE_MCP_MANAGE", "").lower() in ("1", "on", "true")


def _register_live(registry, name, cfg):
    """Put a newly added server's tools into the RUNNING registry, so it is usable this turn rather
    than after a restart. Returns a human summary, never raises: a server that cannot be listed is a
    normal outcome (not installed yet, needs OAuth) and must not undo the config change."""
    if registry is None:
        return "It will be available on the next collie run."
    try:
        conn = _get_conn(name, cfg)
        tools = [{"name": t.get("name"), "description": t.get("description", ""),
                  "inputSchema": t.get("inputSchema") or t.get("input_schema")}
                 for t in conn.list_tools() if t.get("name")]
    except Exception as e:
        return ("Could not list its tools yet (%s: %s) — if it is a remote server this usually means "
                "it needs `collie mcp login %s` first. The config is saved either way."
                % (type(e).__name__, e, name))
    if not tools:
        return "It connected but exposes no tools."
    cache = _read_cache()
    cache[name] = {"hash": _cfg_hash(cfg), "tools": tools}
    _write_cache(cache)
    for t in tools:
        registry.register(MCPTool(name, cfg, t["name"], t.get("description", ""), t.get("inputSchema")))
    return ("%d tools are live NOW (no restart): %s. They are deferred — call load_tools with a name "
            "to get its schema." % (len(tools), ", ".join("mcp__%s__%s" % (name, t["name"])
                                                          for t in tools[:8])))


class MCPStatusTool(Tool):
    name, tier = "mcp_status", "always"
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
                bits.append("NOT authenticated — needs `collie mcp login %s`" % s["name"])
            elif s["auth"] in ("oauth", "header"):
                bits.append("authenticated (%s)" % s["auth"])
            bits.append("%s tools" % ("unknown, not listed yet" if s["tools"] is None else s["tools"]))
            out.append("%s: %s" % (s["name"], " · ".join(bits)))
        return "\n".join(out)


class MCPAddTool(Tool):
    name, tier = "mcp_add", "always"
    description = ("Add an MCP server, giving yourself the tools it exposes. Provide `url` for a "
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
        if not _mcp_manage_on():
            return _MCP_CONSENT % ("add the MCP server %r and register its tools for you" % name)
        cfg = {}
        for k in ("url", "command"):
            if a.get(k):
                cfg[k] = str(a[k])
        for k in ("args", "env", "headers"):
            if a.get(k):
                cfg[k] = a[k]
        err = add_server(name, cfg, replace=False)
        if err:
            return "ERROR: %s" % err
        return "Added MCP server %r. %s" % (name, _register_live(getattr(ctx, "registry", None), name, cfg))


class MCPSetEnabledTool(Tool):
    name, tier = "mcp_set_enabled", "always"
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
        if on and not _mcp_manage_on():
            return _MCP_CONSENT % ("switch the MCP server %r back on and give you its tools" % name)
        if not set_enabled(name, on):
            return "ERROR: no MCP server named %r (call mcp_status to see what exists)" % name
        return ("MCP server %r is now %s. This takes effect on the next collie run — the tools "
                "available in THIS session are unchanged." % (name, "ON" if on else "OFF"))


class MCPRemoveTool(Tool):
    name, tier = "mcp_remove", "always"
    description = ("Delete an MCP server's configuration, its cached tool list and any stored OAuth "
                   "token. This is irreversible — the user has to set the server up again. Prefer "
                   "mcp_set_enabled with enabled=false to switch one off temporarily. Requires the "
                   "user's explicit agreement. Args: name.")
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def run(self, args, ctx):
        name = str((args if isinstance(args, dict) else {}).get("name", "")).strip()
        if not _mcp_manage_on():
            return _MCP_CONSENT % ("delete the MCP server %r, including its stored credential" % name)
        if not remove_server(name):
            return "ERROR: no MCP server named %r (call mcp_status to see what exists)" % name
        return ("Removed MCP server %r — config, cached tool list and stored token. Its tools stay in "
                "this session's registry until the next run." % name)


def register_mcp_management(registry):
    """The manage-MCP tools. Registered ALWAYS, including when no server is configured — `mcp_add`
    with nothing set up yet is the case that matters most."""
    for t in (MCPStatusTool(), MCPAddTool(), MCPSetEnabledTool(), MCPRemoveTool()):
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
                tools = [{"name": t.get("name"), "description": t.get("description", ""),
                          "inputSchema": t.get("inputSchema") or t.get("input_schema")}
                         for t in conn.list_tools() if t.get("name")]
                cache[name] = {"hash": h, "tools": tools}
                dirty = True
            except Exception:
                continue                        # a broken/unavailable server just contributes nothing
        for t in tools:
            registry.register(MCPTool(name, cfg, t["name"], t.get("description", ""),
                                      t.get("inputSchema")))
            n += 1
    if dirty:
        _write_cache(cache)
    return n > 0
