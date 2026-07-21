"""Live smoke for CodexOAuthProvider — run AFTER `codex login` (ChatGPT account).

Hits the real chatgpt.com/backend-api/codex/responses on your ChatGPT Codex quota
($0 marginal). Two tiny turns: a text reply and a forced tool call. Prints usage so
you can see cached/reasoning token behavior before committing a full rebench run.

    codex login                                  # once, populates ~/.codex/auth.json
    MODEL=gpt-5-codex ~/collie/.venv/bin/python smoke_codex_oauth.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness.codex_oauth import CodexOAuthProvider, _auth_path, _fresh_access_token

if not os.path.exists(_auth_path()):
    sys.exit("no %s — run `codex login` first" % _auth_path())

# valid ChatGPT-account Codex slugs: gpt-5.6-sol | gpt-5.6-terra | gpt-5.6-luna | gpt-5.5 | gpt-5.4
model = os.environ.get("MODEL", "gpt-5.6-terra")
p = CodexOAuthProvider(model=model)
access, acct = _fresh_access_token()
print("auth: token %s… account=%s model=%s\n" % (access[:12], acct or "(none)", model))

# ---- turn 1: plain text --------------------------------------------------------------
print("== turn 1: text ==")
c1 = p.complete("You are a terse assistant.",
                [{"role": "user", "content": "Reply with exactly: OK"}], [])
if c1.stop_reason == "error":
    sys.exit("FAILED text turn: %s" % c1.error_detail)
print("text: %r" % c1.text)
print("usage: in=%d cache=%d out=%d | stop=%s"
      % (c1.usage.input_tokens, c1.usage.cache_read, c1.usage.output_tokens, c1.stop_reason))

# ---- turn 2: forced tool call --------------------------------------------------------
print("\n== turn 2: tool call ==")
tools = [{"name": "get_weather", "description": "Get weather for a city",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}]
c2 = p.complete("You are a helpful assistant. Use tools when asked about weather.",
                [{"role": "user", "content": "What's the weather in Tokyo? Use the tool."}], tools)
if c2.stop_reason == "error":
    sys.exit("FAILED tool turn: %s" % c2.error_detail)
if c2.tool_calls:
    tc = c2.tool_calls[0]
    print("tool_call: %s(%s)" % (tc.name, tc.args))
    print("usage: in=%d cache=%d out=%d | stop=%s"
          % (c2.usage.input_tokens, c2.usage.cache_read, c2.usage.output_tokens, c2.stop_reason))
    print("\nSMOKE OK — provider drives the Codex subscription end-to-end.")
else:
    print("no tool call (model answered directly): %r" % c2.text)
    print("\nSMOKE PARTIAL — text works, model just chose not to call the tool. Try again or a "
          "blunter prompt; the wire path is fine.")
