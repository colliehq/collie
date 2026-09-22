"""Manual, quota-spending check that the SDK image lane really carries pixels.

Not collected by pytest (the module name is deliberately outside ``test_*``):
it makes real Claude Agent SDK requests on the developer's Claude Code login.
It generates its own deterministic PNG in memory — no camera, screenshot, or
user data — and asks a question whose answer exists only in the pixels.

    python tests/manual_sdk_image.py [--model claude-opus-4-8]
"""
from __future__ import annotations

import argparse
import base64
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.claude_agent_sdk import ClaudeAgentSdkProvider  # noqa: E402

WIDTH, HEIGHT = 320, 200
CIRCLES = 3                      # unguessable from the text prompt alone
RECTANGLE = (0, 0, 255)          # blue
CIRCLE = (255, 140, 0)           # orange


def fixture_png(circles: int = CIRCLES) -> bytes:
    """A solid blue rectangle plus N separate orange circles, on white."""
    rows = [bytearray(b"\xff" * (WIDTH * 3)) for _ in range(HEIGHT)]

    def put(x, y, rgb):
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            rows[y][x * 3:x * 3 + 3] = bytes(rgb)

    for y in range(40, 165):
        for x in range(25, 150):
            put(x, y, RECTANGLE)
    for index in range(circles):
        cx, cy, radius = 240, 30 + index * 38, 16
        for y in range(cy - radius, cy + radius + 1):
            for x in range(cx - radius, cx + radius + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                    put(x, y, CIRCLE)

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload +
                struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


QUESTION = ("Look at the attached image. Reply with exactly two words separated "
            "by one space: first the number of circles you can see written as a "
            "digit, then the colour of the large rectangle. Nothing else.")

TOOLS = [{"name": "read_file", "description": "read a file from the workspace",
          "input_schema": {"type": "object",
                           "properties": {"path": {"type": "string"}}}}]


def run(provider, label, messages, tool_schemas):
    events = []
    started = time.monotonic()
    with provider.request_authority(
            lambda purpose: events.append(purpose) or "manual-%s" % label,
            lambda request_id, status: events.append(status)):
        completion = provider.complete(
            "You are Collie's reasoning engine. Answer from the attached image.",
            messages, tool_schemas)
    elapsed = time.monotonic() - started
    print("\n=== %s ===" % label)
    print("elapsed: %.1fs   authority: %s" % (elapsed, events))
    print("stop_reason: %s   requests: %s" % (
        completion.stop_reason, completion.request_count))
    print("usage: in=%s out=%s cache_read=%s" % (
        completion.usage.input_tokens, completion.usage.output_tokens,
        completion.usage.cache_read))
    print("text: %r" % (completion.text,))
    if completion.tool_calls:
        print("tool_calls: %r" % ([(call.name, call.args)
                                   for call in completion.tool_calls],))
    if completion.error_detail:
        print("error_detail: %s" % completion.error_detail)
    return completion


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="claude-opus-4-8")
    parser.add_argument("--timeout", type=int, default=240)
    # Re-run with a different count to prove the answer tracks the pixels
    # rather than a lucky constant.
    parser.add_argument("--circles", type=int, choices=(1, 2, 3), default=CIRCLES)
    args = parser.parse_args()

    png = fixture_png(args.circles)
    image = {"type": "image", "media_type": "image/png",
             "data": base64.b64encode(png).decode("ascii")}
    print("fixture: %dx%d PNG, %d bytes raw / %d base64, %d circles"
          % (WIDTH, HEIGHT, len(png), len(image["data"]), args.circles))

    provider = ClaudeAgentSdkProvider(model=args.model, timeout=args.timeout,
                                      subscription_only=True)
    plain = run(provider, "plain-no-tools",
                [{"role": "user", "content": [
                    {"type": "text", "text": QUESTION}, image]}], [])
    tooled = run(provider, "harness-tool-protocol",
                 [{"role": "user", "content": [
                     {"type": "text", "text": QUESTION + " Do not call any tool."},
                     image]}], TOOLS)

    # Two turns, two different images: the model must attribute each count to
    # the turn it was attached to instead of collapsing them or reusing the
    # older screenshot as the current one.
    older_count = max(1, args.circles + 2)
    older = {"type": "image", "media_type": "image/png",
             "data": base64.b64encode(fixture_png(older_count)).decode("ascii")}
    history = run(provider, "history-two-images", [
        {"role": "user", "content": [
            {"type": "text", "text": "Here is the first image."}, older]},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": [
            {"type": "text", "text":
                "Here is the second image. Reply with exactly two numbers "
                "separated by one space: how many circles are in the FIRST "
                "image, then how many are in the SECOND image."}, image]},
    ], [])

    expected = "%d blue" % args.circles
    passed = True
    for label, completion in (("plain", plain), ("tooled", tooled)):
        answer = (completion.text or "").lower()
        matches = not completion.error_detail and answer.strip() == expected
        passed = passed and matches
        verdict = "MATCHES PIXELS" if matches \
            else "DOES NOT MATCH"
        print("%s: %s (expected %s)" % (label, verdict, expected))
    order = "%d %d" % (older_count, args.circles)
    matches = not history.error_detail and (history.text or "").strip() == order
    print("history: %s (expected %s)" % (
        "MATCHES PIXELS" if matches else "DOES NOT MATCH",
        order))
    return 0 if passed and matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
