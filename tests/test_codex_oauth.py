"""Offline self-test for CodexOAuthProvider — no network, no real login required.

Validates the two things that are pure logic and thus the likely bug sites:
  1. collie chat messages  -> Responses `input` items
  2. Responses SSE stream   -> Completion (text, tool_calls, usage)
plus JWT account-id extraction and the construction gate. The live network path is
exercised separately by smoke_codex_oauth.py after `codex login`.
"""
import base64
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# fake CODEX_HOME so construction doesn't require a real login
_home = tempfile.mkdtemp(prefix="codextest_")


def _fake_jwt(claims):
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    pl = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "%s.%s.sig" % (hdr, pl)


# a token that won't be near expiry (exp far in the future) so construction/refresh is a no-op
_tok = _fake_jwt({"exp": 9999999999,
                  "https://api.openai.com/auth": {"chatgpt_account_id": "acct_ABC123"}})
os.makedirs(_home, exist_ok=True)
json.dump({"OPENAI_API_KEY": None,
           "tokens": {"access_token": _tok, "refresh_token": "rt_x", "account_id": "acct_ABC123"},
           "last_refresh": "2026-07-18T00:00:00Z"},
          open(os.path.join(_home, "auth.json"), "w"))
os.environ["CODEX_HOME"] = _home

from harness.codex_oauth import CodexOAuthProvider, _jwt_claims, _fresh_access_token

ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# ---- 1. construction gate + account id ------------------------------------------------
p = CodexOAuthProvider(model="gpt-5-codex")
access, acct = _fresh_access_token()
check("account id from JWT claim", acct == "acct_ABC123")
check("access token loaded", access == _tok)

# ---- 2. message translation -----------------------------------------------------------
msgs = [
    {"role": "user", "content": "fix the bug"},
    {"role": "assistant", "content": "looking", "tool_calls": [
        {"id": "call_1", "name": "read_file", "args": {"path": "a.py"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "line1\nline2"},
    {"role": "assistant", "content": "done"},
]
items = p._to_input(msgs)
kinds = [(it["type"], it.get("role")) for it in items]
check("user -> input_text message", items[0] == {
    "type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]})
check("assistant preamble kept before call",
      kinds[1] == ("message", "assistant") and items[1]["content"][0]["type"] == "output_text")
check("tool_call -> function_call w/ call_id", items[2]["type"] == "function_call"
      and items[2]["call_id"] == "call_1" and json.loads(items[2]["arguments"]) == {"path": "a.py"})
check("tool result -> function_call_output", items[3] == {
    "type": "function_call_output", "call_id": "call_1", "output": "line1\nline2"})
check("final assistant -> output_text", items[4]["content"][0]["type"] == "output_text")

# ---- 3. request body: tools flat, store false, reasoning ------------------------------
body = p._body("SYS", msgs, [{"name": "read_file", "description": "d",
                              "input_schema": {"type": "object"}}], stream=True)
check("instructions = system", body["instructions"] == "SYS")
check("store is False", body["store"] is False)
check("tools are FLAT (name at top level)", body["tools"][0]["name"] == "read_file"
      and "function" not in body["tools"][0])
check("reasoning effort present", body["reasoning"]["effort"] in ("low", "medium", "high"))

# ---- 4. SSE parse: text + tool call + usage ------------------------------------------
def sse(*events):
    return io.BytesIO(("".join("data: %s\n\n" % json.dumps(e) for e in events)
                       + "data: [DONE]\n\n").encode())

# 4a. a plain text turn with usage (incl. cached + reasoning)
stream_txt = sse(
    {"type": "response.output_text.delta", "delta": "Hel"},
    {"type": "response.output_text.delta", "delta": "lo"},
    {"type": "response.completed", "response": {"usage": {
        "input_tokens": 1000, "output_tokens": 200,
        "input_tokens_details": {"cached_tokens": 900},
        "output_tokens_details": {"reasoning_tokens": 150}}}},
)
seen = []
c = p._consume(stream_txt, on_text=seen.append)
check("text assembled from deltas", c.text == "Hello")
check("on_text streamed", "".join(seen) == "Hello")
check("usage input uncached (1000-900)", c.usage.input_tokens == 100)
check("usage cache_read", c.usage.cache_read == 900)
check("usage output incl reasoning", c.usage.output_tokens == 200)
check("stop end_turn on plain text", c.stop_reason == "end_turn")

# 4b. a tool-call turn (function_call item, no deltas)
stream_tool = sse(
    {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "call_9", "name": "edit_file",
        "arguments": json.dumps({"path": "x.py", "old": "a", "new": "b"})}},
    {"type": "response.completed", "response": {"usage": {"input_tokens": 50, "output_tokens": 20}}},
)
c2 = p._consume(stream_tool, on_text=None)
check("tool call parsed", len(c2.tool_calls) == 1 and c2.tool_calls[0].name == "edit_file")
check("tool call id + args", c2.tool_calls[0].id == "call_9"
      and c2.tool_calls[0].args["path"] == "x.py")
check("stop tool_use", c2.stop_reason == "tool_use")

# 4c. message item without deltas (final-item-only turn)
stream_msg = sse(
    {"type": "response.output_item.done", "item": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "recovered"}]}},
    {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 2}}},
)
check("text recovered from final message item", p._consume(stream_msg, None).text == "recovered")

# 4d. failure frame -> error completion
stream_fail = sse({"type": "response.failed",
                   "response": {"error": {"message": "boom", "code": "server_error"}}})
cf = p._consume(stream_fail, None)
check("failed stream -> error stop", cf.stop_reason == "error" and "boom" in cf.error_detail)

print("\n%s" % ("ALL PASS" if ok else "SOME FAILED"))


def test_codex_oauth_checks_pass():
    """Gate for a bare `pytest` run — see the note in test_catalog.py."""
    assert ok, "see the FAIL lines in captured stdout"


if __name__ == "__main__":                 # script mode; a bare SystemExit here aborts collection
    raise SystemExit(0 if ok else 1)
