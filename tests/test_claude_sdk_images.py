"""Canonical image transport for the native Claude Agent SDK provider.

The Web UI accepts attachments, so this lane has to carry real pixels: text-only
serialization silently answered questions about images it never saw.  These
tests pin the worker protocol (structured blocks, never stringified base64),
the association between an image and the message it was attached to, the
refusal paths, and that none of the existing ownership/cancellation guarantees
moved.
"""
import asyncio
import base64
import json
import struct
import threading
import time
import zlib

import pytest

from harness import claude_agent_sdk as transport
from harness import claude_agent_worker as sdk_worker
from harness.claude_agent_sdk import ClaudeAgentSdkProvider
from harness.claude_agent_worker import _anthropic_content, _query, _read_request
from harness.providers import ClaudeCliProvider


def test_text_only_cli_refuses_images_before_spending_a_request(monkeypatch):
    provider = ClaudeCliProvider("opus")
    monkeypatch.setattr(provider, "_call", lambda *args: pytest.fail("must not spawn CLI"))
    result = provider.complete("", [user("Inspect this", image_block())], [])
    assert result.stop_reason == "error" and result.request_count == 0
    assert "claude-agent-sdk" in result.error_detail
    assert image_block()["data"] not in result.error_detail


def test_sdk_default_is_a_concrete_model_that_can_match_init_attestation():
    from harness.providers import provider_default_model
    assert ClaudeAgentSdkProvider()._model == provider_default_model("claude-agent-sdk")
    assert ClaudeAgentSdkProvider()._model.startswith("claude-")


# --------------------------------------------------------------------------- #
#  Fixtures: real, in-memory image bytes.  Nothing here touches the filesystem.
# --------------------------------------------------------------------------- #
def png_bytes(width: int = 4, height: int = 4, rgb=(0, 0, 255)) -> bytes:
    """A valid solid-colour PNG built with the stdlib only."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload +
                struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    scanlines = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


_SUPPORTED = {
    "image/png": png_bytes(),
    "image/jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9",
    "image/gif": b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
    "image/webp": b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\x10\x00\x00\x00\x00\x9d\x01*\x01\x00\x01\x00",
}


def image_block(media_type: str = "image/png", raw: bytes | None = None) -> dict:
    data = base64.b64encode(_SUPPORTED[media_type] if raw is None else raw)
    return {"type": "image", "media_type": media_type, "data": data.decode("ascii")}


def user(text: str, *images) -> dict:
    return {"role": "user",
            "content": [{"type": "text", "text": text}] + list(images)}


TOOLS = [{"name": "grep", "description": "search",
          "input_schema": {"type": "object",
                           "properties": {"pattern": {"type": "string"}}}}]


class _Provider(ClaudeAgentSdkProvider):
    """Captures the worker request instead of spawning the physical worker."""

    def __init__(self, text='{"answer":"done"}', **kwargs):
        super().__init__(**kwargs)
        self.response = {"ok": True, "text": text, "usage": {},
                         "api_key_source": "none"}
        self.request = None
        self.spawned = 0

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.spawned += 1
        self.request = request
        response = dict(self.response)
        if request.get("response_format"):
            # A legitimate worker echoes the structured capability it enforced.
            response["response_format"] = request["response_format"]
            response["response_tools"] = list(request["response_tools"])
        return response


def texts(request) -> list:
    return [block["text"] for block in request["content"]
            if block["type"] == "text"]


def images(request) -> list:
    return [block for block in request["content"] if block["type"] == "image"]


def assert_no_base64_in_prose(request, *blocks):
    """No attachment payload — nor any recognisable slice of one — in the text."""
    prose = "\n".join(texts(request))
    for block in blocks:
        data = block["data"]
        assert data not in prose
        assert data[:24] not in prose
    assert "collie-attachment-" not in prose, "placeholder token leaked into prose"
    assert "[+image]" not in prose, "the text-only CLI marker replaced real pixels"


# --------------------------------------------------------------------------- #
#  Text-only regression: the string protocol must not move at all.
# --------------------------------------------------------------------------- #
def test_text_only_conversation_keeps_the_byte_identical_string_protocol():
    provider = _Provider()
    messages = [{"role": "user", "content": "fix it"}]

    completion = provider.complete("COLLIE SYSTEM", messages, TOOLS)

    assert completion.stop_reason == "end_turn"
    # Text stays a prompt string; only the response format moved (1 -> 3).
    assert provider.request["protocol"] == 3
    assert provider.request["response_tools"] == ["grep"]
    assert "content" not in provider.request
    assert provider.request["prompt"] == ClaudeCliProvider._prompt(
        provider, messages, TOOLS)


def test_plain_text_only_prompt_is_unchanged_and_never_stringifies_blocks():
    provider = _Provider()
    provider.complete("PLANNER", [{"role": "user", "content": "choose"}], [])
    plain = provider.request["prompt"]
    assert plain.startswith("# Conversation so far:")
    assert "User: choose" in plain

    # A block list must render as its text, never as a Python repr.
    rendered = ClaudeAgentSdkProvider._plain_prompt(
        [user("look at this", image_block())])
    assert "User: look at this" in rendered
    assert "'type':" not in rendered and "base64" not in rendered
    assert _SUPPORTED["image/png"][:4].hex() not in rendered


# --------------------------------------------------------------------------- #
#  The image actually reaches the worker, attached to its own message.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("media_type", sorted(_SUPPORTED))
def test_every_supported_media_type_crosses_as_a_structured_block(media_type):
    provider = _Provider()
    block = image_block(media_type)

    provider.complete("S", [user("what is this?", block)], [])

    assert provider.request["protocol"] == 2
    assert "prompt" not in provider.request
    assert images(provider.request) == [
        {"type": "image", "media_type": media_type, "data": block["data"]}]
    assert_no_base64_in_prose(provider.request, block)


def test_plain_caller_keeps_the_image_next_to_the_text_it_belongs_to():
    provider = _Provider()
    block = image_block()

    provider.complete("PLANNER", [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        user("what colour is the rectangle?", block),
    ], [])

    blocks = provider.request["content"]
    index = blocks.index(images(provider.request)[0])
    lead = blocks[index - 1]["text"]
    assert lead.endswith("[attachment 1 of 1 (image/png) follows]\n")
    assert "User: what colour is the rectangle?" in lead
    assert "earlier answer" in blocks[0]["text"]
    # The response instruction still closes the request, after the pixels.
    assert "Respond to the latest user message" in blocks[index + 1]["text"]
    assert_no_base64_in_prose(provider.request, block)


def test_harness_tool_protocol_survives_multimodal_transport():
    provider = _Provider(text='{"tool":"grep","args":{"pattern":"x"}}')
    block = image_block()

    completion = provider.complete("COLLIE SYSTEM", [
        user("find the code that draws this", block)], TOOLS)

    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].name == "grep"
    assert completion.request_count == 1
    # Image + structured response: protocol 4, with the pixels still in blocks.
    assert provider.request["protocol"] == 4
    assert provider.request["response_tools"] == ["grep"]
    assert images(provider.request) == [block]
    assert provider.request["system_prompt"] == "COLLIE SYSTEM"
    trailer = provider.request["content"][-1]["text"]
    assert "# Tools the executor can run:" in trailer
    assert "# RESPONSE FORMAT (strict):" in trailer
    assert images(provider.request)[0]["data"] == block["data"]
    assert_no_base64_in_prose(provider.request, block)


def test_history_with_several_images_stays_in_chronological_order():
    older = image_block("image/png", png_bytes(rgb=(255, 0, 0)))
    second = image_block("image/gif")
    latest = image_block("image/png", png_bytes(rgb=(0, 255, 0)))
    provider = _Provider()

    provider.complete("S", [
        user("the first screenshot", older),
        {"role": "assistant", "content": "seen"},
        user("two more now", second, latest),
    ], [])

    ordered = [block.get("data") or block["text"] for block in provider.request["content"]]
    assert [value for value in ordered if value in
            (older["data"], second["data"], latest["data"])] == [
        older["data"], second["data"], latest["data"]]

    blocks = provider.request["content"]
    first_index = next(i for i, b in enumerate(blocks)
                       if b.get("data") == older["data"])
    latest_index = next(i for i, b in enumerate(blocks)
                        if b.get("data") == latest["data"])
    assert "User: the first screenshot" in blocks[first_index - 1]["text"]
    assert "attachment 1 of 1" in blocks[first_index - 1]["text"]
    # The newest turn labels its own two attachments; the old screenshot is not
    # re-appended next to the latest question.
    assert "User: two more now" in blocks[first_index + 1]["text"]
    assert "attachment 2 of 2" in blocks[latest_index - 1]["text"]
    assert "the first screenshot" not in blocks[latest_index - 1]["text"]
    assert_no_base64_in_prose(provider.request, older, second, latest)


def test_transport_never_mutates_the_caller_owned_conversation():
    block = image_block()
    messages = [user("look", block), {"role": "assistant", "content": "ok"},
                user("and now?", image_block("image/gif"))]
    snapshot = json.dumps(messages)

    _Provider().complete("S", messages, TOOLS)

    assert json.dumps(messages) == snapshot


def test_answer_envelope_parsing_is_unaffected_by_attachments():
    provider = _Provider(text='{"answer":"a blue rectangle"}')
    streamed = []

    completion = provider.complete(
        "COLLIE SYSTEM", [user("describe it", image_block())], TOOLS,
        on_text=streamed.append)

    assert completion.stop_reason == "end_turn"
    assert completion.text == "a blue rectangle"
    assert streamed == ["a blue rectangle"]


# --------------------------------------------------------------------------- #
#  Refusals: malformed, unsupported, or over-budget attachments never spawn.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("content,match", [
    ([{"type": "image", "media_type": "image/tiff", "data": "AAAA"}],
     "unsupported image media type"),
    ([{"type": "image", "media_type": "image/png"}],
     "missing base64 data"),
    ([{"type": "image", "media_type": "image/png", "data": "not base64!!"}],
     "invalid base64 image data"),
    ([{"type": "image", "media_type": "image/png",
       "data": base64.b64encode(b"GIF89a-not-a-png").decode("ascii")}],
     "does not match its declared"),
    ([{"type": "video", "data": "AAAA"}],
     "unsupported conversation content block type"),
    (["a bare string block"], "content block is not an object"),
])
def test_malformed_or_incompatible_blocks_refuse_before_any_request(content, match):
    provider = _Provider(subscription_only=True)
    events = []
    provider.request_gate = lambda kind: events.append(("reserve", kind)) or "req-1"
    provider.request_complete = lambda request_id, status: events.append(
        ("complete", request_id, status))

    completion = provider.complete("S", [{"role": "user", "content": content}], [])

    assert completion.stop_reason == "error"
    assert "attachment refused" in completion.error_detail
    assert match in completion.error_detail
    assert completion.request_count == 0, "a refusal must not consume quota"
    assert provider.spawned == 0
    # The reservation is released rather than leaked.
    assert events == [("reserve", "claude_agent_sdk"),
                      ("complete", "req-1", "error")]
    assert provider._active_runs == {}


def test_an_image_on_a_non_user_message_fails_instead_of_being_dropped():
    provider = _Provider()
    completion = provider.complete("S", [
        {"role": "assistant", "content": [image_block()]}], [])

    assert completion.stop_reason == "error"
    assert "only supported on user messages" in completion.error_detail
    assert provider.spawned == 0


def test_oversized_latest_attachment_is_refused_and_never_truncated():
    provider = _Provider()
    huge = {"type": "image", "media_type": "image/png",
            "data": "A" * (transport._MAX_IMAGE_B64 + 4)}

    completion = provider.complete("S", [user("look", huge)], [])

    assert completion.stop_reason == "error"
    assert "nothing was truncated" in completion.error_detail
    assert provider.spawned == 0
    assert huge["data"] not in completion.error_detail


def test_request_byte_budget_refuses_instead_of_dropping_the_latest_image(monkeypatch):
    monkeypatch.setattr(transport, "_MAX_MULTIMODAL_REQUEST_BYTES", 128)
    provider = _Provider()
    block = image_block()

    completion = provider.complete("S", [user("look", block)], [])

    assert completion.stop_reason == "error"
    assert "exceeds the" in completion.error_detail
    assert "nothing was truncated" in completion.error_detail
    assert provider.spawned == 0


def test_history_beyond_the_budget_is_marked_while_the_latest_turn_is_intact(
        monkeypatch):
    monkeypatch.setattr(transport, "_MAX_REQUEST_IMAGES", 1)
    older = image_block("image/png", png_bytes(rgb=(255, 0, 0)))
    latest = image_block("image/png", png_bytes(rgb=(0, 255, 0)))
    provider = _Provider()

    provider.complete("S", [user("first", older),
                            {"role": "assistant", "content": "seen"},
                            user("second", latest)], [])

    assert [block["data"] for block in images(provider.request)] == [latest["data"]]
    prose = "\n".join(texts(provider.request))
    assert "attachment 1 of 1 (image/png) was omitted" in prose
    assert "budget was reached" in prose
    assert_no_base64_in_prose(provider.request, older, latest)


def test_text_only_followup_degrades_to_the_string_protocol_with_markers(
        monkeypatch):
    monkeypatch.setattr(transport, "_MAX_REQUEST_IMAGES", 0)
    provider = _Provider()

    provider.complete("S", [user("first", image_block()),
                            {"role": "assistant", "content": "seen"},
                            {"role": "user", "content": "and now in words?"}], [])

    assert provider.request["protocol"] == 1
    assert "was omitted" in provider.request["prompt"]
    assert "and now in words?" in provider.request["prompt"]
    assert provider.spawned == 1


def test_latest_turn_over_the_image_count_budget_is_refused(monkeypatch):
    monkeypatch.setattr(transport, "_MAX_REQUEST_IMAGES", 1)
    provider = _Provider()

    completion = provider.complete(
        "S", [user("look", image_block(), image_block("image/gif"))], [])

    assert completion.stop_reason == "error"
    assert "nothing was truncated" in completion.error_detail
    assert provider.spawned == 0


def test_attachments_never_reach_a_temporary_file(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("an attachment was staged on disk")

    monkeypatch.setattr(transport.tempfile, "mkstemp", forbidden)
    monkeypatch.setattr(transport.tempfile, "mkdtemp", forbidden)
    monkeypatch.setattr(transport.tempfile, "NamedTemporaryFile", forbidden)
    provider = _Provider()

    provider.complete("S", [user("look", image_block())], TOOLS)

    assert provider.request["protocol"] == 4


# --------------------------------------------------------------------------- #
#  Cancellation and ownership are unchanged for an image-bearing call.
# --------------------------------------------------------------------------- #
def test_scoped_cancel_fences_a_multimodal_call_before_it_reaches_the_worker(
        monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    popen_calls = []
    completions = []
    provider = ClaudeAgentSdkProvider(subscription_only=True)
    block = image_block()

    original_marker = transport._marked_messages

    def blocking_marker(messages, entries, nonce):
        entered.set()
        assert release.wait(2)
        return original_marker(messages, entries, nonce)

    def forbidden_spawn(*_args, **_kwargs):
        popen_calls.append(True)
        raise AssertionError("a cancelled call crossed the worker boundary")

    monkeypatch.setattr(transport, "_marked_messages", blocking_marker)
    monkeypatch.setattr(transport.subprocess, "Popen", forbidden_spawn)

    def invoke():
        with provider.request_authority(lambda _purpose: "req-img",
                                        lambda *_args: None,
                                        request_scope="msn_img"):
            completions.append(provider.complete(
                "S", [user("what is this?", block)], []))

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    assert entered.wait(1)
    assert provider.cancel_for("msn_other")() is False
    started = time.monotonic()
    assert provider.cancel_for("msn_img")() is True
    assert time.monotonic() - started < 0.5

    release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert popen_calls == []
    assert completions[0].stop_reason == "error"
    assert "cancelled" in completions[0].error_detail
    assert block["data"] not in completions[0].error_detail
    assert block["data"] not in completions[0].text
    assert provider._active_runs == {}


# --------------------------------------------------------------------------- #
#  Worker side: input protocol validation and SDK stream shape.
# --------------------------------------------------------------------------- #
class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _StreamSdk:
    ClaudeAgentOptions = _Options

    def __init__(self):
        self.streamed = None
        self.options = None
        self.prompt = None

    async def query(self, *, prompt, options):
        self.prompt, self.options = prompt, options
        if not isinstance(prompt, str):
            self.streamed = [message async for message in prompt]
        yield {"type": "system", "subtype": "init", "data": {
            "tools": [], "skills": [], "plugins": [], "agents": {},
            "slash_commands": [], "mcp_servers": [], "model": "opus",
            "apiKeySource": "none"}}
        yield {"type": "assistant", "id": "msg-1", "content": [
            {"type": "text", "text": '{"answer":"blue"}'}]}
        yield {"type": "result", "num_turns": 1, "is_error": False,
               "usage": {"input_tokens": 9, "output_tokens": 2}}


def run_worker_query(request):
    sdk = _StreamSdk()
    result = asyncio.run(_query(dict(
        {"model": "opus", "system_prompt": "collie", "effort": "default"},
        **request), sdk))
    return result, sdk


def test_worker_sends_one_user_turn_whose_content_carries_the_image_source():
    block = image_block()
    result, sdk = run_worker_query({"content": [
        {"type": "text", "text": "User: what colour is it?"}, block]})

    assert result["ok"] and result["text"] == '{"answer":"blue"}'
    assert result["api_key_source"] == "none"
    assert sdk.streamed == [{
        "type": "user", "session_id": "", "parent_tool_use_id": None,
        "message": {"role": "user", "content": [
            {"type": "text", "text": "User: what colour is it?"},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": block["data"]}},
        ]}}]
    # Foreign-surface isolation is untouched on the multimodal path.
    assert sdk.options.tools == [] and sdk.options.allowed_tools == []
    assert sdk.options.mcp_servers == {} and sdk.options.agents == {}
    assert sdk.options.max_turns == 1 and sdk.options.setting_sources == []


def test_worker_text_only_path_still_passes_a_plain_string_prompt():
    result, sdk = run_worker_query({"prompt": "next"})
    assert result["ok"]
    assert sdk.prompt == "next" and sdk.streamed is None


@pytest.mark.parametrize("content,match", [
    ([], "non-empty list"),
    ("blob", "non-empty list"),
    ([{"type": "text", "text": "only text"}], "carries no image block"),
    ([{"type": "text", "text": "  "}, image_block()], "text block is empty"),
    ([{"type": "text", "text": "hi", "extra": 1}, image_block()],
     "unexpected fields"),
    ([{"type": "image", "media_type": "image/png", "data": "AA", "x": 1}],
     "unexpected fields"),
    ([{"type": "image", "media_type": "image/heic", "data": "AAAA"}],
     "unsupported media type"),
    ([{"type": "image", "media_type": "image/png", "data": "!!!!"}],
     "not valid base64"),
    ([{"type": "image", "media_type": "image/png",
       "data": base64.b64encode(b"GIF89a-nope").decode("ascii")}],
     "does not match its declared media type"),
    ([{"type": "audio", "data": "AAAA"}], "unsupported content block type"),
    (["bare"], "content block is not an object"),
])
def test_worker_rejects_malformed_input_protocol(content, match):
    with pytest.raises(RuntimeError, match=match):
        _anthropic_content(content)


def test_worker_image_errors_never_quote_the_attachment():
    block = image_block()
    broken = dict(block, media_type="image/gif")
    with pytest.raises(RuntimeError) as excinfo:
        _anthropic_content([broken])
    assert block["data"] not in str(excinfo.value)


def read_request(payload, monkeypatch):
    class _Stdin:
        buffer = None

    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    class _Buffer:
        def read(self, size):
            return raw[:size]

    stdin = _Stdin()
    stdin.buffer = _Buffer()
    monkeypatch.setattr(sdk_worker.sys, "stdin", stdin)
    return _read_request()


def test_worker_request_protocol_separates_text_and_multimodal(monkeypatch):
    block = image_block()
    base = {"model": "opus", "system_prompt": "collie", "effort": "default"}

    request = read_request(dict(base, protocol=2, content=[
        {"type": "text", "text": "User: hi"}, block]), monkeypatch)
    assert request["protocol"] == 2 and request["content"][1] == block

    request = read_request(dict(base, protocol=1, prompt="hi"), monkeypatch)
    assert request["prompt"] == "hi"

    with pytest.raises(RuntimeError, match="must not carry content blocks"):
        read_request(dict(base, protocol=1, prompt="hi", content=[block]),
                     monkeypatch)
    with pytest.raises(RuntimeError, match="must not carry a prompt string"):
        read_request(dict(base, protocol=2, prompt="hi", content=[block]),
                     monkeypatch)
    with pytest.raises(RuntimeError, match="invalid worker protocol"):
        read_request(dict(base, protocol=5, prompt="hi"), monkeypatch)
    with pytest.raises(RuntimeError, match="missing prompt"):
        read_request(dict(base, protocol=1), monkeypatch)
    with pytest.raises(RuntimeError, match="carries no image block"):
        read_request(dict(base, protocol=2, content=[
            {"type": "text", "text": "hi"}]), monkeypatch)


def test_worker_keeps_the_smaller_stdin_budget_for_text_requests(monkeypatch):
    base = {"model": "opus", "system_prompt": "collie", "effort": "default"}
    oversized = dict(base, protocol=1,
                     prompt="x" * (sdk_worker._MAX_TEXT_REQUEST_BYTES + 16))
    with pytest.raises(RuntimeError, match="exceeded the safety limit"):
        read_request(oversized, monkeypatch)

    with pytest.raises(RuntimeError, match="exceeded the safety limit"):
        read_request(b"x" * (sdk_worker._MAX_MULTIMODAL_REQUEST_BYTES + 1),
                     monkeypatch)


def test_end_to_end_worker_protocol_from_provider_payload_to_sdk_stream():
    """The exact bytes the provider would write are what the worker accepts."""
    provider = _Provider()
    block = image_block()
    provider.complete("COLLIE SYSTEM", [user("what colour is it?", block)], TOOLS)

    wire = json.dumps(provider.request, ensure_ascii=False, allow_nan=False)
    result, sdk = run_worker_query(
        {"content": json.loads(wire)["content"]})

    assert result["ok"]
    content = sdk.streamed[0]["message"]["content"]
    assert [part["type"] for part in content].count("image") == 1
    image = next(part for part in content if part["type"] == "image")
    assert image["source"] == {"type": "base64", "media_type": "image/png",
                              "data": block["data"]}
    prose = " ".join(part["text"] for part in content if part["type"] == "text")
    assert "what colour is it?" in prose
    assert block["data"] not in prose
