"""Provider protocol and transport: what every backend must do with a request,
a tool call, a stream and a failure — before the loop ever sees it.

Split out of test_core.py — a pure move; no assertion was changed. Stdlib-only, no Opus, fast.
    python tests/test_providers.py     (exit 0 = all pass)
"""
import inspect, io, json, os, re, sys, tempfile, time, types, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ctx, _Skip, _RecordingMemory, _ScriptProvider, run_module  # noqa: E402,F401

import contextlib
import inspect, io, json, os, re, sys, tempfile, time, types, warnings

# ------------------------------------------------------------------ providers._to_anthropic
def test_to_anthropic_robustness():
    from harness.providers import AnthropicProvider, ToolCall
    ap = AnthropicProvider.__new__(AnthropicProvider)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},                                   # empty -> coerce
        {"role": "assistant", "tool_calls": [ToolCall("a", "grep", {"p": 1})]}, # ToolCall
        {"role": "assistant", "tool_calls": [{"id": "b", "name": "read", "args": {}}]},  # dict
        {"role": "assistant", "tool_calls": ["ToolCall(id='c'...)"]},           # legacy str -> skip
    ]
    out = ap._to_anthropic(msgs)
    empt = [m for m in out if m["role"] == "assistant" and isinstance(m["content"], str) and not m["content"].strip()]
    assert not empt, "no empty assistant text block allowed"
    ids = [b["id"] for m in out for b in (m["content"] if isinstance(m["content"], list) else []) if b.get("type") == "tool_use"]
    assert "a" in ids and "b" in ids, "ToolCall + dict tool_calls must both yield tool_use ids"


# ------------------------------------------------------------------ history cache breakpoint (caching fix)
def test_history_cache_breakpoint_placement():
    """The rolling history breakpoint lands on the last message of the byte-stable ELIDED prefix
    (stable_upto-1), promoting string content to a block so cache_control has somewhere to live; a
    single breakpoint total; no-op on empty."""
    from harness.providers import _apply_history_cache, _mark_cache_block

    def n_bp(msgs):
        return sum(1 for m in msgs for b in (m["content"] if isinstance(m["content"], list) else [])
                   if isinstance(b, dict) and b.get("cache_control"))

    # stable_upto=3 (elide boundary) -> breakpoint on index 2's last block
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": "x"}]}
            for i in range(6)]
    _apply_history_cache(msgs, 3)
    assert n_bp(msgs) == 1, "exactly one history breakpoint"
    assert msgs[2]["content"][-1].get("cache_control"), "breakpoint must sit at stable_upto-1"
    assert not msgs[5]["content"][-1].get("cache_control"), "the volatile tail must NOT be marked"

    # short/un-elided thread (stable_upto<=0) -> mark the final message; string content is promoted
    m2 = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "there"}]
    _apply_history_cache(m2, 0)
    assert isinstance(m2[-1]["content"], list) and m2[-1]["content"][-1]["cache_control"], "final marked"
    assert m2[-1]["content"][-1]["text"] == "there", "promoted block keeps the text"
    assert n_bp(m2) == 1

    _apply_history_cache([], 5)             # empty: must not raise
    # out-of-range stable_upto is clamped, never indexes past the end
    m3 = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
    _apply_history_cache(m3, 99)
    assert m3[0]["content"][-1]["cache_control"]

def test_history_cache_does_not_mutate_source_blocks():
    """_mark_cache_block copies — the original message dict/blocks must be untouched so the session
    thread and any retry see clean content (no leaked cache_control)."""
    from harness.providers import _mark_cache_block
    orig = {"role": "user", "content": [{"type": "text", "text": "a"}]}
    marked = _mark_cache_block(orig)
    assert "cache_control" in marked["content"][-1]
    assert "cache_control" not in orig["content"][-1], "source block must not be mutated"

# ------------------------------------------------------------------ every provider tolerates dict tool_calls
def test_all_providers_toolcall_tolerant():
    # the ToolCall-serialization crash existed in _to_anthropic AND _to_openai/_to_ollama/claude-cli.
    # A continued session (esp. deepseek/openai-compat, used by SWE+compare) must not crash on a
    # dict-shaped tool_call in history.
    from harness.providers import (AnthropicProvider, OllamaProvider, OpenAICompatProvider,
                                   ClaudeCliProvider, ToolCall)
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "d1", "name": "grep", "args": {"p": "x"}}]},  # dict
            {"role": "assistant", "tool_calls": [ToolCall("t2", "read", {"path": "/y"})]}]            # dataclass
    an = AnthropicProvider.__new__(AnthropicProvider)._to_anthropic(msgs)
    ol = OllamaProvider.__new__(OllamaProvider)._to_ollama("sys", msgs)
    oa = OpenAICompatProvider.__new__(OpenAICompatProvider)._to_openai("sys", msgs)
    # each must reference the dict tool_call's name without raising
    assert any("grep" in json.dumps(x) for x in an), "anthropic dropped dict tool_call"
    assert any("grep" in json.dumps(x) for x in ol), "ollama dropped dict tool_call"
    assert any("grep" in json.dumps(x) for x in oa), "openai dropped dict tool_call"
    assert '"id": "d1"' in json.dumps(oa) or "'id': 'd1'" in str(oa), "openai must keep dict tool_call id"

# ------------------------------------------------------------------ provider error paths degrade gracefully
def test_provider_error_paths():
    import io, urllib.error
    from unittest.mock import patch
    from harness.providers import OllamaProvider, OpenAICompatProvider, AnthropicProvider
    def he(code): return urllib.error.HTTPError("u", code, "e", {}, io.BytesIO(b'{"error":"boom"}'))
    class _R:
        def __init__(s, d): s._d = d if isinstance(d, bytes) else json.dumps(d).encode()
        def read(s): return s._d
        def __enter__(s): return s
        def __exit__(s, *a): pass
    msgs = [{"role": "user", "content": "hi"}]

    # ollama + openai-compat must return stop_reason='error' (NOT treat the error as the answer)
    ol = OllamaProvider.__new__(OllamaProvider); ol._model = "m"; ol.url = "http://x"
    with patch("urllib.request.urlopen", side_effect=he(500)):
        assert ol.complete("s", msgs, []).stop_reason == "error"
    os.environ["_COLLIE_TEST_KEY"] = "dummy"
    oa = OpenAICompatProvider("http://x", "_COLLIE_TEST_KEY", "m", name="deepseek")
    with patch("urllib.request.urlopen", side_effect=he(500)):
        assert oa.complete("s", msgs, []).stop_reason == "error"
    with patch("urllib.request.urlopen", lambda *a, **k: _R(b"not-json{{")):
        assert oa.complete("s", msgs, []).stop_reason == "error"

    # anthropic now RETURNS an error completion (point 4 contract flip: never raise for transport) —
    # carrying error_status so the host retry classifier can act, and never a normal answer.
    an = AnthropicProvider.__new__(AnthropicProvider); an.name = "anthropic"; an.model = "m"
    an.api_key = "k"; an.max_tokens = 100; an.API = "http://x"
    with patch("urllib.request.urlopen", side_effect=he(429)):
        c = an.complete("s", msgs, [])
    assert c.stop_reason == "error" and c.error_status == 429, "anthropic HTTP error -> error completion"
    assert c.text.startswith("ERROR("), "error body must not read as a normal answer"
    # a response missing `usage` must still return the answer (tokens default to 0), not crash
    with patch("urllib.request.urlopen", lambda *a, **k: _R({"content": [{"type": "text", "text": "ok"}]})):
        r = an.complete("s", msgs, [])
        assert r.text == "ok" and r.stop_reason != "error"

def test_classify_error_matrix():
    from harness.providers import classify_error
    assert classify_error("overloaded_error", 529) == "retryable"
    assert classify_error("", 503) == "retryable"
    assert classify_error("insufficient_quota", 429) == "terminal", "quota beats retryable status"
    assert classify_error("Insufficient Balance", 402) == "terminal"
    assert classify_error("prompt is too long: 213462 tokens > 200000 maximum", 400) == "overflow"
    assert classify_error("request_too_large", 413) == "overflow"
    assert classify_error("ThrottlingException: Too many tokens, please wait", 0) == "retryable", "throttle != overflow"
    assert classify_error("timed out", 0) == "retryable"
    assert classify_error("something novel", 0) == "terminal", "unknown fails fast"

def test_provider_error_contract_matrix():
    """Every provider, every transport failure -> stop_reason=='error', text startswith 'ERROR(',
    never raises (point 4). 4 providers x 4 fault kinds."""
    import io as _io, urllib.error
    from unittest.mock import patch
    from harness.providers import (AnthropicProvider, AnthropicOAuthProvider, OllamaProvider,
                                   OpenAICompatProvider)
    faults = [urllib.error.HTTPError("u", 429, "e", {}, _io.BytesIO(b'{"error":"x"}')),
              urllib.error.HTTPError("u", 500, "e", {}, _io.BytesIO(b'busy')),
              urllib.error.URLError("down"), TimeoutError("slow")]
    an = AnthropicProvider.__new__(AnthropicProvider); an.name = "anthropic"; an.model = "m"; an.api_key = "k"; an.max_tokens = 10; an.API = "http://x"
    oa = AnthropicOAuthProvider.__new__(AnthropicOAuthProvider); oa.name = "anthropic-oauth"; oa.model = "claude-opus-4-8"; oa.api_key = ""; oa.max_tokens = 10; oa.API = "http://x"
    ol = OllamaProvider.__new__(OllamaProvider); ol.name = "ollama"; ol._model = "m"; ol.url = "http://x"
    os.environ["_CT_KEY"] = "k"
    oc = OpenAICompatProvider("http://x", "_CT_KEY", "m", name="deepseek")
    provs = [an, ol, oc]
    # A real expired Claude Code credential on the developer's machine must not pre-empt this
    # transport-error matrix. Expiry has its own tests; this one owns the token state completely.
    with patch("harness.providers._read_oauth_token", lambda: "tok"), \
         patch("harness.providers.claude_oauth_expired", lambda *a, **k: False):
        provs.append(oa)
        for p in provs:
            for f in faults:
                with patch("urllib.request.urlopen", side_effect=f):
                    c = p.complete("s", [{"role": "user", "content": "hi"}], [])
                assert c.stop_reason == "error", "%s did not return error for %r" % (p.name, f)
                assert c.text.startswith("ERROR("), (p.name, c.text[:40])

def test_openai_compat_surfaces_finish_length():
    """AUDIT #7 second half: finish_reason='length' must surface as stop_reason='length' with the
    tool_calls PRESERVED (regression that would have caught the truncation-invisible DeepSeek path)."""
    from unittest.mock import patch
    from harness.providers import OpenAICompatProvider
    os.environ["_CT_KEY2"] = "k"
    oc = OpenAICompatProvider("http://x", "_CT_KEY2", "m", name="deepseek")
    class _R:
        def __init__(s, d): s._d = json.dumps(d).encode()
        def read(s): return s._d
        def __enter__(s): return s
        def __exit__(s, *a): pass
    body = {"choices": [{"finish_reason": "length", "message": {"content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "edit_file", "arguments": "{\"path\":"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    with patch("urllib.request.urlopen", lambda *a, **k: _R(body)):
        c = oc.complete("s", [{"role": "user", "content": "hi"}], [{"name": "edit_file", "description": "edit"}])
    assert c.stop_reason == "length" and c.tool_calls, "length must surface + keep tool_calls"
    body["choices"][0]["finish_reason"] = "stop"; body["choices"][0]["message"]["tool_calls"] = []
    body["choices"][0]["message"]["content"] = "hi"
    with patch("urllib.request.urlopen", lambda *a, **k: _R(body)):
        assert oc.complete("s", [{"role": "user", "content": "hi"}], []).stop_reason == "end_turn"

def test_anthropic_normalizes_max_tokens():
    from harness.providers import _norm_stop
    assert _norm_stop("max_tokens") == "length" and _norm_stop("length") == "length"
    assert _norm_stop("end_turn") == "end_turn" and _norm_stop("tool_use") == "tool_use"

def test_anthropic_max_tokens_default():
    from harness.providers import AnthropicProvider
    old = os.environ.pop("COLLIE_MAX_TOKENS", None)
    os.environ["ANTHROPIC_API_KEY"] = "dummy"
    try:
        assert AnthropicProvider(model="m").max_tokens == 8192, "default 8192 (raised from 4096: 4k truncated big edits into a retry loop)"
        os.environ["COLLIE_MAX_TOKENS"] = "16384"
        assert AnthropicProvider(model="m").max_tokens == 16384
    finally:
        os.environ.pop("COLLIE_MAX_TOKENS", None)
        if old is not None: os.environ["COLLIE_MAX_TOKENS"] = old

def test_ollama_done_reason_length():
    from unittest.mock import patch
    from harness.providers import OllamaProvider
    ol = OllamaProvider.__new__(OllamaProvider); ol.name = "ollama"; ol._model = "m"; ol.url = "http://x"
    class _R:
        def __init__(s, d): s._d = json.dumps(d).encode()
        def read(s): return s._d
        def __enter__(s): return s
        def __exit__(s, *a): pass
    with patch("urllib.request.urlopen", lambda *a, **k: _R(
            {"message": {"content": "partial"}, "done_reason": "length"})):
        assert ol.complete("s", [{"role": "user", "content": "hi"}], []).stop_reason == "length"

# ------------------------------------------------------------------ ollama synthetic tool-ids unique
def test_ollama_unique_tool_ids():
    from unittest.mock import patch
    from harness.providers import OllamaProvider
    class _R:
        def __init__(s, d): s._d = json.dumps(d).encode()
        def read(s): return s._d
        def __enter__(s): return s
        def __exit__(s, *a): pass
    oll = OllamaProvider.__new__(OllamaProvider); oll._model = "x"; oll.url = "http://x"
    resp = {"message": {"tool_calls": [{"function": {"name": "a", "arguments": {}}},
                                       {"function": {"name": "b", "arguments": {}}}]}}
    with patch("urllib.request.urlopen", lambda *a, **k: _R(resp)):
        ids = [tc.id for _ in range(2) for tc in oll.complete("s", [], []).tool_calls]  # 2 "turns"
    assert len(ids) == len(set(ids)), "ollama tool ids must be globally unique (were 'oll_0' colliding across turns): %r" % ids

# ------------------------------------------------------------------ providers._parse_anthropic_stream
def test_stream_parser():
    from harness.providers import _parse_anthropic_stream
    frames = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n',
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t1","name":"grep"}}\n',
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"pat"}}\n',
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"tern\\":\\"x\\"}"}}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}\n',
    ]
    got = []
    text, calls, usage, sr, edetail = _parse_anthropic_stream(iter(frames), lambda t: got.append(t))
    assert text == "Hello", text
    assert got == ["Hel", "lo"], got                             # on_text streamed each delta
    assert len(calls) == 1 and calls[0].name == "grep" and calls[0].args == {"pattern": "x"}, calls
    assert sr == "tool_use" and usage["input_tokens"] == 10 and usage["output_tokens"] == 5
    assert edetail == "", "clean stream carries no error_detail"

def test_stream_parser_error_event():
    from harness.providers import _parse_anthropic_stream
    frames = [b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n',
              b'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n']
    text, calls, usage, sr, edetail = _parse_anthropic_stream(iter(frames), None)
    assert sr == "error" and "ERROR" in text, "mid-stream error must surface stop_reason=error"
    assert edetail == "busy", "stream error must thread error_detail out for classify_error"

def test_unrecognised_provider_error_says_it_was_not_recognised():
    """'terminal' covers both 'we know this is fatal' and 'we have never seen this'.

    They printed identically, so a run stopped by an unknown error looked as settled as one stopped
    by a bad API key. That is how the mcp_ naming failure — reported by the API as a billing
    message — read as a quota problem for hours.
    """
    from harness.providers import classify_error, is_known_terminal
    known = "authentication_error: invalid x-api-key"
    unknown = "You're out of extra usage. Add more at claude.ai/settings/usage and keep going."
    assert classify_error(known) == "terminal" and classify_error(unknown) == "terminal", \
        "both still classify as terminal — that is the point"
    assert is_known_terminal(known), "a bad key is a recognised fatal error"
    assert not is_known_terminal(unknown), \
        "this message matches no pattern; it must NOT be reported with the confidence of one"

def test_provider_error_keeps_status_and_limit_headers():
    """A recorded failure has to be diagnosable later: 400, 429 and 529 must not look identical."""
    import urllib.error, io as _io
    from harness.providers import _error_completion
    hdrs = {"anthropic-ratelimit-unified-5h-utilization": "0.08",
            "anthropic-ratelimit-unified-status": "allowed",
            "content-type": "application/json"}
    err = urllib.error.HTTPError("https://api", 400, "Bad Request", hdrs,
                                 _io.BytesIO(b'{"error":{"message":"boom"}}'))
    comp = _error_completion("anthropic-oauth", err)
    assert comp.error_status == 400, "the status must survive into the record"
    assert "5h-utilization" in comp.error_detail, "the rate-limit headers must survive too"
    assert "content-type" not in comp.error_detail, "only limit-related headers, not every header"

def test_provider_default_is_api():
    """API key is the default; anthropic-oauth is OPT-IN (an explicit choice, never a silent
    default). Locks the round-17 decision across the panel and every hardcoded fallback."""
    from harness import settings as S
    import tempfile
    row = next(s for s in S.SCHEMA if s["key"] == "PROVIDER")
    assert row["default"] == "anthropic"
    # options may be plain strings OR {value,label} dicts (the panel renders friendly labels);
    # assert the semantic invariant on the values, not the serialization shape.
    opt_vals = [o["value"] if isinstance(o, dict) else o for o in row["options"]]
    assert opt_vals[0] == "anthropic", "panel lists the API provider first"
    assert "anthropic-oauth" in opt_vals, "oauth stays available as an explicit choice"
    # a Provider saved in the Settings panel must reach the settings.get() fallback path
    # (delegate/pack/acp read it directly — no dependence on settings.apply() timing)
    p = os.path.join(tempfile.gettempdir(), "collie_settings_prov.json")
    old = S._PATH
    env_had = os.environ.pop("COLLIE_PROVIDER", None)
    try:
        S._PATH = p; S._cache["mtime"] = -1.0
        assert S.get("PROVIDER", "anthropic") == "anthropic", "nothing saved -> API default"
        S.save({"PROVIDER": "anthropic-oauth"})
        assert S.get("PROVIDER", "anthropic") == "anthropic-oauth", "panel choice wins over default"
    finally:
        if env_had is not None:
            os.environ["COLLIE_PROVIDER"] = env_had
        S._PATH = old; S._cache["mtime"] = -1.0
        try: os.remove(p)
        except OSError: pass

if __name__ == "__main__":                 # LAST, always: a guard with definitions after it
    sys.exit(run_module(globals(), "PROVIDERS"))  # silently skips every one of them.
