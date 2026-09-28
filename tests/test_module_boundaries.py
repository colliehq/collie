"""Behavior checks for optional modules missed by the normal interactive paths."""
import importlib
import io
import json

import pytest


@pytest.mark.parametrize("name", ["desktop_mac", "native_mac", "menubar_mac", "browserapple"])
def test_mac_surfaces_report_unavailable_on_other_platforms(monkeypatch, name):
    module = importlib.import_module("harness." + name)
    monkeypatch.setattr(module.plat, "is_macos", lambda: False)
    value = module.available()
    assert (value[0] if isinstance(value, tuple) else value) is False


@pytest.mark.parametrize("body", ["[]", "null", '{"usage":NaN}', "not json"])
def test_sdk_worker_rejects_invalid_requests_before_starting_a_runtime(monkeypatch, body):
    from harness import codex_sdk_worker
    monkeypatch.setattr(codex_sdk_worker.sys, "stdin", io.StringIO(body))
    with pytest.raises(ValueError):
        codex_sdk_worker._request()


def test_recording_library_cannot_delete_an_outside_file(tmp_path, monkeypatch):
    from harness import record
    library = tmp_path / "recordings"
    library.mkdir()
    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"preserve")
    clip = library / "clip.mp4"
    clip.write_bytes(b"test recording")
    (library / "notes.txt").write_text("not a recording", encoding="utf-8")
    monkeypatch.setattr(record, "_default_outdir", lambda: str(library))
    assert [item["name"] for item in record.list_recordings()] == ["clip.mp4"]
    assert not record.delete_recording(str(outside))
    assert outside.read_bytes() == b"preserve"
    assert record.delete_recording("clip.mp4") and record.list_recordings() == []


def test_recording_monitor_selection_preserves_negative_desktop_coordinates(monkeypatch):
    from harness import record
    monkeypatch.setattr(record, "_monitors", lambda: [(-1920, 0, 1920, 1080), (0, 0, 2560, 1440)])
    assert record.resolve_region(monitor=1) == (-1920, 0, 1920, 1080)
    assert record.resolve_region(region="-900,10,500,600") == (-900, 10, 500, 600)
    with pytest.raises(ValueError, match="out of range"):
        record.resolve_region(monitor=3)


@pytest.mark.parametrize("stream", [False, True])
def test_token_meter_counts_usage_through_real_local_http(tmp_path, monkeypatch, stream):
    import asyncio
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
    from harness import apitap
    usage = {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22,
             "prompt_tokens_details": {"cached_tokens": 9}}
    monkeypatch.setattr(apitap, "OUT", str(tmp_path / "usage.json"))
    monkeypatch.setattr(apitap, "STATE", dict.fromkeys(apitap.STATE, 0))
    monkeypatch.delenv("APITAP_KEY", raising=False)
    async def scenario():
        async def upstream(request):
            body = await request.json()
            if not stream:
                return web.json_response({"usage": usage})
            assert body["stream_options"]["include_usage"] is True
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            data = ("data: " + json.dumps({"usage": usage}) + "\n\ndata: [DONE]\n\n").encode()
            for chunk in (data[:13], data[13:41], data[41:]):
                await response.write(chunk)
                await asyncio.sleep(.005)
            await response.write_eof()
            return response
        upstream_app = web.Application()
        upstream_app.router.add_post("/v1/chat/completions", upstream)
        async with TestServer(upstream_app) as upstream_server:
            monkeypatch.setattr(apitap, "UPSTREAM", str(upstream_server.make_url("")))
            proxy_app = web.Application()
            proxy_app.router.add_post("/v1/chat/completions", apitap.handler)
            async with TestServer(proxy_app) as proxy, ClientSession() as client:
                async with client.post(proxy.make_url("/v1/chat/completions"), json={"stream": stream}) as response:
                    assert response.status == 200
                    data = await response.read()
                    assert (b"[DONE]" in data) if stream else json.loads(data)["usage"] == usage
    asyncio.run(scenario())
    assert json.loads((tmp_path / "usage.json").read_text()) == {
        "calls": 1, "prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22, "cached_tokens": 9}
