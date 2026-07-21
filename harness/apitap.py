"""apitap — a token-metering reverse proxy for OpenAI-compatible endpoints.

Point an opaque CLI agent (Hermes, etc.) at http://localhost:PORT/v1 instead of the real
API; apitap forwards to APITAP_UPSTREAM, reads the `usage` from each response (forcing
`stream_options.include_usage` on streamed calls so usage is always emitted), and
accumulates prompt/completion/total tokens to APITAP_OUT. This lets us measure the token
cost of an agent that doesn't report it — and, run collie through the same proxy, gives a
truly apples-to-apples token comparison at the HTTP boundary.

    APITAP_PORT=8899 APITAP_UPSTREAM=https://api.deepseek.com APITAP_OUT=usage.json \
        python -m harness.apitap
"""
import json
import os

from aiohttp import ClientSession, web

UPSTREAM = os.environ.get("APITAP_UPSTREAM", "https://api.deepseek.com").rstrip("/")
OUT = os.environ.get("APITAP_OUT", "apitap_usage.json")
STATE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
         "total_tokens": 0, "cached_tokens": 0}


def _acc(u):
    if not u:
        return
    STATE["calls"] += 1
    STATE["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
    STATE["completion_tokens"] += u.get("completion_tokens", 0) or 0
    STATE["total_tokens"] += u.get("total_tokens", 0) or 0
    STATE["cached_tokens"] += (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    with open(OUT, "w") as f:
        json.dump(STATE, f)


async def handler(req):
    path = req.rel_url.path_qs
    body = await req.read()
    headers = {k: v for k, v in req.headers.items()
               if k.lower() in ("authorization", "content-type")}
    key = os.environ.get("APITAP_KEY")     # override auth with a known-good key: an agent
    if key:                                 # probing a localhost endpoint may send a bad one
        headers["authorization"] = "Bearer " + key
    is_stream = False
    try:
        j = json.loads(body)
        if j.get("stream"):
            is_stream = True
            j.setdefault("stream_options", {})["include_usage"] = True  # force usage in stream
            body = json.dumps(j).encode()
    except Exception:
        pass
    async with ClientSession() as s:
        async with s.post(UPSTREAM + path, data=body, headers=headers) as up:
            if not is_stream:
                data = await up.read()
                try:
                    _acc(json.loads(data).get("usage"))
                except Exception:
                    pass
                return web.Response(body=data, status=up.status,
                                    content_type="application/json")
            resp = web.StreamResponse(
                status=up.status,
                headers={"content-type": up.headers.get("content-type", "text/event-stream")})
            await resp.prepare(req)
            # relay raw chunks unchanged, but buffer for usage-parsing: iter_any() yields arbitrary
            # TCP segments, so the final `data: {…usage…}` event can be split across two chunks.
            # Splitting each chunk independently dropped that usage (json.loads on a half line
            # fails) and silently undercounted tokens — the tap's whole purpose.
            buf = b""
            def _scan(line):
                line = line.strip()
                if line.startswith(b"data: "):
                    try:
                        obj = json.loads(line[6:])
                        if obj.get("usage"):
                            _acc(obj["usage"])
                    except Exception:
                        pass
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()               # keep trailing partial line for the next chunk
                for line in parts:
                    _scan(line)
            _scan(buf)                          # stream may end without a trailing newline
            await resp.write_eof()
            return resp


def main():
    with open(OUT, "w") as f:                   # reset the accumulator on start
        json.dump(STATE, f)
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    web.run_app(app, host="127.0.0.1",
                port=int(os.environ.get("APITAP_PORT", "8899")), print=None)


if __name__ == "__main__":
    main()
