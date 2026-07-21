"""CodexOAuthProvider — run collie's OWN loop on the ChatGPT Codex subscription.

The OpenAI-side twin of AnthropicOAuthProvider. Instead of a metered OPENAI_API_KEY it
uses the OAuth token minted by `codex login` (~/.codex/auth.json) and talks to
chatgpt.com/backend-api/codex/responses — the same endpoint + `originator: codex_cli_rs`
the codex-rs CLI uses, so requests draw on your ChatGPT Plus/Pro Codex quota at $0
marginal. Personal use of your own subscription — identical footing to the Claude
flat-pool path collie already ships.

Wire difference that makes this a real adapter, not a preset: the Codex backend speaks
the RESPONSES API (top-level `instructions`, a list of typed `input` items, SSE `output`
items), NOT /chat/completions. So this translates collie's chat-shaped messages both
ways. Kept in its own module so providers.py only gains a two-line dispatch.

Prereq: `codex login` (ChatGPT account) must have populated ~/.codex/auth.json. If it
hasn't, construction raises with that instruction.
"""
from __future__ import annotations
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from .providers import (Completion, ModelProvider, ToolCall, Usage,
                         _error_completion, _norm_stop, content_text, _tc_fields)

# codex-rs OAuth app + endpoints (mirrors the upstream CLI so the token is interchangeable
# with a `codex login` session — we refresh through the SAME client_id the CLI registered).
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
BASE_URL = os.environ.get("CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex")
REFRESH_SKEW = 120                       # refresh this many seconds before the JWT `exp`


def _auth_path() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(home, "auth.json")


def _jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying the signature (we only read public claims:
    `exp` for refresh timing, `chatgpt_account_id` for the account header)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _load_auth() -> dict:
    """~/.codex/auth.json -> its `tokens` block. codex writes:
        {"OPENAI_API_KEY": null|str, "tokens": {id_token, access_token, refresh_token,
         account_id}, "last_refresh": "..."}"""
    with open(_auth_path(), encoding="utf-8") as f:
        return json.load(f)


def _save_auth(doc: dict) -> None:
    """Write auth.json atomically, preserving codex's structure so the CLI keeps working
    off the same file (same contract hermes uses: refresh writes BOTH stores)."""
    p = _auth_path()
    # auth.json holds long-lived OAuth refresh/access tokens — treat it as a secret. Keep the
    # containing ~/.codex dir owner-only (0700) and create the temp file 0600 (O_CREAT mode +
    # explicit chmod, in case the temp path pre-existed with looser perms) so os.replace never
    # leaves world/group-readable tokens behind.
    d = os.path.dirname(p)
    try:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        pass
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def _refresh(refresh_token: str) -> dict:
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token,
                       "client_id": CLIENT_ID}).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fresh_access_token() -> tuple[str, str]:
    """Return (access_token, account_id), refreshing + persisting if the JWT is near expiry.
    Refresh failure is non-fatal when the current token still has life — we fall through to
    it and let a 401 on the real call trigger a forced refresh."""
    doc = _load_auth()
    tokens = doc.get("tokens") or {}
    access = (tokens.get("access_token") or "").strip()
    refresh = (tokens.get("refresh_token") or "").strip()
    claims = _jwt_claims(access)
    exp = claims.get("exp", 0)
    if refresh and exp and time.time() > (exp - REFRESH_SKEW):
        try:
            got = _refresh(refresh)
            access = (got.get("access_token") or access).strip()
            tokens["access_token"] = access
            if got.get("refresh_token"):
                tokens["refresh_token"] = got["refresh_token"]
            doc["tokens"] = tokens
            doc["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_auth(doc)
            claims = _jwt_claims(access)
        except Exception:
            pass                          # keep the old token; the call path retries on 401
    acct = (claims.get("https://api.openai.com/auth", {}) or {}).get("chatgpt_account_id") \
        or tokens.get("account_id") or ""
    return access, acct


class CodexOAuthProvider(ModelProvider):
    name = "codex-oauth"
    reports_cache = True                  # Responses usage carries input_tokens_details.cached_tokens
    URL = BASE_URL.rstrip("/") + "/responses"

    def __init__(self, model: str = "gpt-5.6-terra", max_tokens: int = 16384):
        self.model = model
        self.max_tokens = int(os.environ.get("COLLIE_MAX_TOKENS", str(max_tokens)))
        # Codex reasoning effort — low/medium/high. Higher = deeper (more output/reasoning
        # tokens); collie's assert-verify loop wants at least medium.
        self.effort = os.environ.get("COLLIE_REASONING_EFFORT", "medium")
        self.timeout = float(os.environ.get("COLLIE_HTTP_TIMEOUT", "600"))
        self._session_id = str(uuid.uuid4())
        if not os.path.exists(_auth_path()):
            raise RuntimeError(
                "no ~/.codex/auth.json — run `codex login` (ChatGPT account) first; "
                "needed for --provider codex-oauth")

    # ---- collie chat messages -> Responses `input` items --------------------------------
    def _to_input(self, messages: list) -> list:
        items = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                items.append({"type": "function_call_output",
                              "call_id": m.get("tool_call_id", ""),
                              "output": content_text(m.get("content", ""))})
            elif role == "assistant" and m.get("tool_calls"):
                # a text preamble alongside tool calls is legal — emit it first
                txt = content_text(m.get("content") or "")
                if txt:
                    items.append({"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": txt}]})
                for tc in m["tool_calls"]:
                    tid, tname, targs = _tc_fields(tc)
                    items.append({"type": "function_call", "name": tname,
                                  "call_id": tid or ("call_" + uuid.uuid4().hex[:24]),
                                  "arguments": json.dumps(targs, ensure_ascii=False)})
            else:
                text_type = "output_text" if role == "assistant" else "input_text"
                items.append({"type": "message", "role": role or "user",
                              "content": [{"type": text_type,
                                           "text": content_text(m.get("content", ""))}]})
        return items

    def _headers(self, token: str, account_id: str) -> dict:
        # originator + a codex_cli_rs-shaped UA are what the Cloudflare layer whitelists;
        # ChatGPT-Account-Id scopes the request to the subscription. Same set codex-rs sends.
        h = {"content-type": "application/json",
             "accept": "text/event-stream",
             "authorization": "Bearer " + token,
             "openai-beta": "responses=experimental",
             "originator": "codex_cli_rs",
             "user-agent": "codex_cli_rs/0.0.0 (collie)",
             "session_id": self._session_id}
        if account_id:
            h["ChatGPT-Account-Id"] = account_id
        return h

    def _body(self, system: str, messages: list, tool_schemas: list, stream: bool) -> dict:
        # The ChatGPT-account Codex backend rejects API-key-only params: no max_output_tokens
        # (it caps output itself) and no temperature (see the gpt-5* branch in providers). Keep
        # the body to what codex-rs actually sends.
        body = {
            "model": self.model,
            "instructions": system,
            "input": self._to_input(messages),
            "store": False,
            "stream": stream,
            "reasoning": {"effort": self.effort},
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if tool_schemas:
            # Responses function shape is FLAT (name/description/parameters at top level),
            # unlike chat/completions' nested {"function": {...}}.
            body["tools"] = [{"type": "function", "name": t["name"],
                              "description": t.get("description", ""),
                              "parameters": t.get("input_schema",
                                                  {"type": "object", "properties": {}}),
                              "strict": False}
                             for t in tool_schemas]
        return body

    def complete(self, system: str, messages: list, tool_schemas: list, on_text=None) -> Completion:
        access, acct = _fresh_access_token()
        body = self._body(system, messages, tool_schemas, stream=True)
        req = urllib.request.Request(self.URL, data=json.dumps(body).encode(),
                                     headers=self._headers(access, acct), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return self._consume(r, on_text)
        except urllib.error.HTTPError as e:
            # One forced refresh + retry on 401 (token expired between our skew check and the
            # call, or another client rotated it).
            if e.code == 401:
                try:
                    doc = _load_auth()
                    got = _refresh((doc.get("tokens") or {}).get("refresh_token", ""))
                    doc.setdefault("tokens", {})["access_token"] = got["access_token"]
                    if got.get("refresh_token"):
                        doc["tokens"]["refresh_token"] = got["refresh_token"]
                    _save_auth(doc)
                    access, acct = _fresh_access_token()
                    req = urllib.request.Request(self.URL, data=json.dumps(body).encode(),
                                                 headers=self._headers(access, acct), method="POST")
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        return self._consume(r, on_text)
                except Exception as e2:
                    return _error_completion(self.name, e2)
            return _error_completion(self.name, e)
        except Exception as e:
            return _error_completion(self.name, e)

    # ---- Responses SSE stream -> Completion ---------------------------------------------
    def _consume(self, r, on_text) -> Completion:
        text_parts, calls = [], []
        usage_raw, status, err_detail = {}, "completed", ""
        data_buf = []
        for raw in r:
            line = raw.decode("utf-8", "ignore").rstrip("\n").rstrip("\r")
            if line.startswith("data:"):
                data_buf.append(line[5:].lstrip())
                continue
            if line:                      # non-blank, non-data (e.g. `event:`) — ignore
                continue
            if not data_buf:              # blank separator with nothing buffered
                continue
            chunk = "\n".join(data_buf)
            data_buf = []
            if chunk == "[DONE]":
                break
            try:
                ev = json.loads(chunk)
            except Exception:
                continue
            etype = ev.get("type", "")
            if etype == "response.output_text.delta":
                delta = ev.get("delta", "")
                if delta:
                    text_parts.append(delta)
                    if on_text:
                        on_text(delta)
            elif etype == "response.output_item.done":
                item = ev.get("item", {}) or {}
                it = item.get("type")
                if it == "function_call":
                    args = item.get("arguments", "{}")
                    try:
                        args = json.loads(args) if isinstance(args, str) else (args or {})
                    except Exception:
                        args = {"_malformed_args": str(args)[:500]}
                    calls.append(ToolCall(item.get("call_id") or item.get("id")
                                          or ("call_" + uuid.uuid4().hex[:8]),
                                          item.get("name", ""), args))
                elif it == "message" and not text_parts:
                    # no deltas were streamed (some turns emit only the final item) — recover text
                    for blk in item.get("content", []) or []:
                        if blk.get("type") in ("output_text", "text"):
                            text_parts.append(blk.get("text", ""))
            elif etype in ("response.completed", "response.incomplete", "response.failed"):
                resp = ev.get("response", {}) or {}
                usage_raw = resp.get("usage", {}) or {}
                status = etype.split(".")[1]
                if etype == "response.failed":
                    err_detail = json.dumps(resp.get("error", {}))[:300]
            elif etype == "error":
                err_detail = json.dumps(ev)[:300]
                status = "failed"
        if status == "failed":
            return Completion(text="ERROR(%s): %s" % (self.name, err_detail),
                              stop_reason="error", error_detail=err_detail)
        # Responses usage: input_tokens INCLUDES cached; output_tokens INCLUDES reasoning.
        cached = (usage_raw.get("input_tokens_details", {}) or {}).get("cached_tokens", 0) or 0
        usage = Usage(input_tokens=max(0, (usage_raw.get("input_tokens", 0) or 0) - cached),
                      output_tokens=usage_raw.get("output_tokens", 0) or 0,
                      cache_read=cached)
        stop = "tool_use" if calls else ("length" if status == "incomplete" else "end_turn")
        return Completion(text="".join(text_parts), tool_calls=calls, usage=usage,
                          stop_reason=_norm_stop(stop))
