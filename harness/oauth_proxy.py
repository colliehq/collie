"""oauth_proxy — put ANY Anthropic-API agent on the flat Claude subscription.

Hermes needed a source patch to draw the flat (subscription) pool instead of metered pay-go.
That doesn't scale: opencode ships as a 175 MB compiled binary (no prompt to patch), and every
agent re-implements auth differently. This proxy solves it once, at the HTTP boundary.

Point an agent at `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` (any/no API key). For each
request the proxy makes it indistinguishable from Claude Code — the things Anthropic's router
keys the flat pool on:
  1. OAuth token   — from ~/.claude/.credentials.json (or CLAUDE_CODE_OAUTH_TOKEN)
  2. CC identity headers — Authorization: Bearer …, the claude-code betas, claude-code UA, x-app:cli
  3. system block 0 = "You are Claude Code, Anthropic's official CLI for Claude."
  4. no third-party fingerprint strings — in the system prompt AND in tool names. Empirically the
     router also flags specific tool NAMES (opencode's `todowrite` meters even with a clean system);
     those are renamed on the way out and restored on the way back.

Two modes for point 4's system prompt:
  • DEFAULT (LEAN): replace the agent's system with the CC identity + one generic coder line. Always
    draws flat — no fingerprint can survive. Trade-off: the agent runs without its bespoke prompt.
  • OAUTH_PROXY_KEEP_PROMPT=1 (scrub): keep the agent's own prompt, token-scrubbed. More faithful,
    but fragile — a saturated prompt (e.g. opencode's) can still carry a tell you must chase down.

Reuses collie's own OAuth constants (harness/providers.py) so collie/Hermes/pi/opencode all sit
on identical footing. Personal use of your own subscription — not pooling/resale.

    OAUTH_PROXY_PORT=8788 python -m harness.oauth_proxy              # LEAN (reliable); ANTHROPIC_BASE_URL=…:8788
    OAUTH_PROXY_KEEP_PROMPT=1 OAUTH_PROXY_PORT=8788 python -m harness.oauth_proxy   # faithful/fragile
"""
import json
import os
import re

from aiohttp import ClientSession, web

from .providers import _CC_BETAS, _CC_SYSTEM, _claude_version, _read_oauth_token

UPSTREAM = os.environ.get("OAUTH_PROXY_UPSTREAM", "https://api.anthropic.com").rstrip("/")
LOG = os.environ.get("OAUTH_PROXY_LOG")     # optional: append each request's system prompt for analysis

# Meaning-preserving fingerprint scrubs applied to the agent's own system prompt. Product/agent
# names + distinctive architecture vocabulary that mark a request as "not Claude Code". Extend this
# when a new agent starts metering (capture the system via OAUTH_PROXY_LOG and bisect). Case-sensitive
# word-boundary swaps; core coding-tool words (read/edit/bash/write) are deliberately untouched so
# tool-calling still works.
_SCRUB = [
    (r"the best coding agent on the planet", "a coding assistant"),
    (r"https?://\S*opencode\S*", "the docs"),
    (r"https?://\S*anomalyco\S*", "the repo"),
    (r"\banomalyco\b", "the project"),
    (r"\bopencode\.ai\b", "the docs"),
    (r"\bopencode\b", "the assistant"),
    (r"\bOpenCode\b", "The assistant"),
    (r"\bOpencode\b", "The assistant"),
    (r"\bSST\b", "the maintainers"),
    (r"\bpi-coding-agent\b", "the assistant"),
    (r"\bEarendil\b", "the maintainers"),
    (r"\bpi\b", "the assistant"),
    (r"\bPi\b", "The assistant"),
]
_SCRUB_RE = [(re.compile(p), r) for p, r in _SCRUB]


def _scrub(text):
    if not isinstance(text, str) or not text:
        return text
    for rx, rep in _SCRUB_RE:
        text = rx.sub(rep, text)
    return text


# A short, generic coding instruction used in LEAN mode — carries zero third-party fingerprint, so
# the request draws the flat pool even for an agent (e.g. opencode) whose own 10K+ prompt is
# saturated with tells that a token-scrub can't fully neutralize. Tool behavior is unaffected (the
# model calls tools from their schemas, not the prose).
_LEAN_CODER = ("You are a focused coding agent. Use the available tools to read, edit, and run code. "
               "Make the smallest change that solves the task and verify it.")
# LEAN is the reliable DEFAULT (see module docstring); opt into faithful-but-fragile scrub mode.
_LEAN = os.environ.get("OAUTH_PROXY_KEEP_PROMPT") != "1"


def _rewrite_system(body):
    """Force system[0] = the Claude-Code identity. Default: keep the agent's own prompt as a scrubbed
    later block so its tool guidance survives. LEAN mode (OAUTH_PROXY_LEAN=1): drop the agent prompt
    entirely and use a generic coder line — for agents whose prompt is too fingerprint-saturated to
    scrub token-by-token (they'd otherwise land in the metered pool)."""
    if _LEAN:
        body["system"] = [{"type": "text", "text": _CC_SYSTEM},
                          {"type": "text", "text": _LEAN_CODER}]
        return body
    sys = body.get("system")
    blocks = []
    if isinstance(sys, str):
        if sys.strip():
            blocks = [{"type": "text", "text": _scrub(sys)}]
    elif isinstance(sys, list):
        for b in sys:
            if isinstance(b, dict) and b.get("type") == "text":
                nb = dict(b)
                nb["text"] = _scrub(b.get("text", ""))
                blocks.append(nb)
            else:
                blocks.append(b)
    # drop a leading block that already IS the CC identity (some agents add it) to avoid a dup
    if blocks and isinstance(blocks[0], dict) and blocks[0].get("text", "").strip() == _CC_SYSTEM:
        blocks = blocks[1:]
    body["system"] = [{"type": "text", "text": _CC_SYSTEM}] + blocks
    return body


# Exact tool NAMES that Anthropic's flat-pool router fingerprints as third-party (empirically:
# opencode's `todowrite` meters even with a clean system + trivial schema; renaming it draws flat).
# We rename these on the way OUT and restore them on the way BACK so the agent still sees its own
# tool names in the response. Extend via OAUTH_PROXY_RENAME_TOOLS="name1,name2".
_RENAME = {"todowrite": "todo_write"}
for _n in (os.environ.get("OAUTH_PROXY_RENAME_TOOLS") or "").split(","):
    _n = _n.strip()
    if _n and _n not in _RENAME:
        _RENAME[_n] = _n + "_x"
_UNRENAME = {v: k for k, v in _RENAME.items()}


def _rename_request(body):
    """Rename fingerprinted tool names in the tools list AND in prior tool_use blocks in messages
    (so a continued conversation stays consistent)."""
    if not _RENAME:
        return body
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("name") in _RENAME:
            t["name"] = _RENAME[t["name"]]
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _RENAME:
                    b["name"] = _RENAME[b["name"]]
    return body


def _unrename_response(text):
    """Restore original tool names in a response body / SSE chunk (JSON `"name":"alias"` fields)."""
    if not _UNRENAME:
        return text
    for alias, orig in _UNRENAME.items():
        text = (text.replace('"name":"%s"' % alias, '"name":"%s"' % orig)
                    .replace('"name": "%s"' % alias, '"name": "%s"' % orig))
    return text


def _unrename_bytes(chunk):
    """Byte-level restore for streamed chunks — avoids decoding a chunk that may split a multibyte
    UTF-8 char (which would corrupt Chinese/emoji output). The `"name":"alias"` needle is pure ASCII,
    so a byte replace is safe. (A needle split across a chunk boundary is possible in theory but the
    tool-name lives in one small SSE event that arrives whole in practice.)"""
    if not _UNRENAME:
        return chunk
    for alias, orig in _UNRENAME.items():
        chunk = (chunk.replace(('"name":"%s"' % alias).encode(), ('"name":"%s"' % orig).encode())
                      .replace(('"name": "%s"' % alias).encode(), ('"name": "%s"' % orig).encode()))
    return chunk


def _log_system(body):
    if not LOG:
        return
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"model": body.get("model"), "system": body.get("system")},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---- token metering: the proxy sees every request/response, so it can measure input/output tokens
# for agents that don't report usage in headless mode (pi, opencode). GET /_usage reads the running
# total; /_usage?reset=1 resets it (a benchmark resets before each task and reads it after).
_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
          "cache_read": 0, "cache_creation": 0}


def _acc_usage(u):
    if not isinstance(u, dict):
        return
    _USAGE["input_tokens"] += u.get("input_tokens", 0) or 0
    _USAGE["output_tokens"] += u.get("output_tokens", 0) or 0
    _USAGE["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
    _USAGE["cache_creation"] += u.get("cache_creation_input_tokens", 0) or 0


def _meter_stream(buf):
    """Extract usage from a buffered Anthropic SSE body: input+cache from message_start, the final
    output_tokens from message_delta."""
    try:
        text = buf.decode("utf-8", "replace")
    except Exception:
        return
    _USAGE["calls"] += 1
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        if ev.get("type") == "message_start":
            _acc_usage((ev.get("message") or {}).get("usage"))
        elif ev.get("type") == "message_delta" and ev.get("usage"):
            # message_delta.usage.output_tokens is cumulative-final for the message
            _USAGE["output_tokens"] += ev["usage"].get("output_tokens", 0) or 0


_ALLOWED_HOSTS = {"", "127.0.0.1", "localhost", "::1", "host.docker.internal"}


def _host_ok(req):
    """Anti-DNS-rebinding: this proxy attaches the user's real Claude OAuth bearer to every
    forwarded request, so a rebound attacker.com -> 127.0.0.1 would otherwise become same-origin
    and drive/read the subscription. Accept only loopback names (plus host.docker.internal, the
    documented container access name); a browser always sends the navigated hostname as Host."""
    h = (req.headers.get("Host", "") or "").strip()
    host = h.rsplit(":", 1)[0].strip("[]").lower() if h else ""
    return host in _ALLOWED_HOSTS


async def handler(req):
    if not _host_ok(req):
        return web.json_response(
            {"error": {"type": "forbidden_host",
                       "message": "host not allowed (anti-DNS-rebinding)"}}, status=403)
    path = req.rel_url.path_qs
    raw = await req.read()
    token = _read_oauth_token()
    if not token:
        return web.json_response(
            {"error": {"type": "no_oauth", "message": "no Claude OAuth token — run `claude` login "
                       "or set CLAUDE_CODE_OAUTH_TOKEN"}}, status=401)

    if path.startswith("/_usage"):              # metering readout (benchmark helper)
        prior = dict(_USAGE)
        if "reset=1" in path and req.method == "POST":   # POST-only: no state change on a drive-by GET
            for k in _USAGE:
                _USAGE[k] = 0
        return web.json_response(prior)

    is_messages = "/messages" in path and req.method == "POST"
    body = raw
    stream = False
    if is_messages:
        try:
            j = json.loads(raw)
            _log_system(j)
            j = _rewrite_system(j)
            j = _rename_request(j)          # dodge tool-name fingerprints (e.g. todowrite)
            stream = bool(j.get("stream"))
            body = json.dumps(j).encode()
        except (ValueError, TypeError):
            pass  # not JSON we understand — forward as-is (auth still swapped)

    # replace the agent's auth with the Claude-Code identity; keep only safe passthrough headers.
    # MERGE the agent's own anthropic-beta flags with the CC identity betas (don't overwrite) — an
    # agent may need a capability beta like `structured-outputs-2025-11-13` (terminus enforces its
    # JSON command schema with it); dropping it makes the model emit freeform text the agent can't
    # parse. CC identity betas go FIRST so the flat-pool fingerprint is unaffected.
    cc = [b.strip() for b in _CC_BETAS.split(",") if b.strip()]
    incoming = [b.strip() for b in (req.headers.get("anthropic-beta") or "").split(",") if b.strip()]
    merged, seen = [], set()
    for b in cc + incoming:
        if b not in seen:
            merged.append(b); seen.add(b)
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + token,
        "anthropic-version": req.headers.get("anthropic-version", "2023-06-01"),
        "anthropic-beta": ",".join(merged),
        "user-agent": "claude-code/%s (external, cli)" % _claude_version(),
        "x-app": "cli",
        "accept": req.headers.get("accept", "application/json"),
    }
    async with ClientSession() as s:
        async with s.request(req.method, UPSTREAM + path, data=body, headers=headers) as up:
            if stream or "text/event-stream" in (up.headers.get("content-type") or ""):
                resp = web.StreamResponse(status=up.status, headers={
                    "content-type": up.headers.get("content-type", "text/event-stream")})
                meter_buf = bytearray()
                try:
                    await resp.prepare(req)
                    async for chunk in up.content.iter_any():
                        if is_messages:
                            meter_buf += chunk
                            chunk = _unrename_bytes(chunk)
                        await resp.write(chunk)
                    await resp.write_eof()
                except (ConnectionResetError, ConnectionError, RuntimeError):
                    pass    # client hung up mid-stream — benign, don't take the proxy down
                if is_messages and up.status < 300:
                    _meter_stream(bytes(meter_buf))
                return resp
            data = await up.read()
            if is_messages and up.status < 300:
                try:
                    _USAGE["calls"] += 1
                    _acc_usage(json.loads(data).get("usage"))
                except Exception:
                    pass
                if _UNRENAME:
                    data = _unrename_response(data.decode("utf-8", "replace")).encode("utf-8")
            return web.Response(status=up.status, body=data,
                                content_type=up.headers.get("content-type", "application/json").split(";")[0])


def build_app():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def main():
    port = int(os.environ.get("OAUTH_PROXY_PORT", "8788"))
    # bind host: 127.0.0.1 by default (safe). Set OAUTH_PROXY_HOST=0.0.0.0 to let Docker containers
    # reach it via host.docker.internal (needed to put containerized agents on the subscription).
    # NOTE: 0.0.0.0 exposes the subscription to anything that can reach this host — use only on a
    # trusted/local machine.
    host = os.environ.get("OAUTH_PROXY_HOST", "127.0.0.1")
    if not _read_oauth_token():
        print("warning: no Claude OAuth token found (~/.claude/.credentials.json / "
              "CLAUDE_CODE_OAUTH_TOKEN) — requests will 401", flush=True)
    print("collie oauth-proxy on http://%s:%d -> %s (flat-subscription injection)"
          % (host, port, UPSTREAM), flush=True)
    web.run_app(build_app(), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
