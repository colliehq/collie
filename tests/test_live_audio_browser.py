"""Exercise the actual browser uploader with synthetic clips, never a microphone."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
HTML = (Path(__file__).parents[1] / "harness/webui/live.html").read_text("utf-8")


@pytest.fixture
def live_page():
    requests = []
    state = {"session_id": "live-first", "active": True, "listen": True,
             "understand": False, "audio": {"microphone_seq": 4}, "capabilities": {}}
    responses = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(request):
            path = urlparse(request.request.url).path
            if path == "/live":
                return request.fulfill(status=200, content_type="text/html", body=HTML)
            if path == "/api/live-copilot/audio":
                requests.append({"query": parse_qs(urlparse(request.request.url).query),
                                 "body": request.request.post_data_buffer})
                status, body = responses.pop(0) if responses else (202, {"ok": True})
                return request.fulfill(status=status, content_type="application/json",
                                       body=json.dumps(body))
            body = (state if path == "/api/live-copilot" else
                    {"values": {"LANG": "en"}} if path == "/api/settings" else {})
            request.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        page.route("**/*", route)
        page.add_init_script("""
            window.RECORDERS=[];
            window.MediaRecorder=class {
              static isTypeSupported(){return true}
              constructor(stream,options){this.state='inactive';this.options=options;RECORDERS.push(this)}
              start(){this.state='recording'}
              stop(){
                if(this.state==='inactive')return;
                this.state='inactive';
                if(this.ondataavailable)this.ondataavailable({data:new Blob(['captured-clip'],{type:this.options.mimeType})});
                if(this.onstop)this.onstop();
              }
            };
            window.TRACK={readyState:'live',stop(){this.readyState='ended'}};
            window.STREAM={getTracks(){return[TRACK]},getAudioTracks(){return[TRACK]}};
        """)
        page.goto("http://collie.test/live")
        page.wait_for_function("STATE.session_id==='live-first'")
        yield page, state, responses, requests, errors
        page.evaluate("endCapture()")
        browser.close()


def start_clip(page):
    page.evaluate("streams.push(STREAM);beginCapture([{name:'microphone',stream:STREAM}])")
    page.wait_for_function("RECORDERS.length===1")
    page.evaluate("RECORDERS[0].stop()")


def test_busy_upload_retries_same_bytes_and_sequence_without_recording_ahead(live_page):
    page, _, responses, requests, errors = live_page
    responses.extend([(429, {"code": "live_audio_busy", "retry_after_ms": 750})] * 2)
    start_clip(page)
    page.wait_for_function("document.getElementById('audioNotice').textContent.includes('paused')")
    assert page.evaluate("RECORDERS.length") == 1
    assert requests[0]["query"]["seq"] == ["5"]
    page.wait_for_function("RECORDERS.length===2", timeout=5000)
    assert len(requests) == 3
    assert all(request == requests[0] for request in requests)
    assert requests[0]["body"] == b"captured-clip"
    assert page.locator("#audioNotice").inner_text() == ""
    page.evaluate("RECORDERS[1].stop()")
    page.wait_for_function("RECORDERS.length===3")
    assert requests[-1]["query"]["seq"] == ["6"]
    assert not errors


def test_stop_aborts_retry_and_does_not_upload_final_stopped_clip(live_page):
    page, _, responses, requests, errors = live_page
    responses.extend([(429, {"retry_after_ms": 750})] * 5)
    start_clip(page)
    page.wait_for_function("document.getElementById('audioNotice').textContent.includes('paused')")
    page.evaluate("endCapture()")
    count = len(requests)
    page.wait_for_timeout(1100)
    assert len(requests) == count == 1
    assert page.evaluate("TRACK.readyState") == "ended"
    assert page.evaluate("captureRequests.size") == 0
    assert page.evaluate("RECORDERS.length") == 1
    assert not errors


def test_permission_revocation_or_new_session_stops_old_capture(live_page):
    page, state, _, requests, errors = live_page
    page.evaluate("streams.push(STREAM);beginCapture([{name:'microphone',stream:STREAM}])")
    state.update(session_id="live-second", listen=False)
    page.evaluate("value => render(value)", state)
    page.wait_for_timeout(150)
    assert requests == []
    assert page.evaluate("runningCapture") is False
    assert page.evaluate("TRACK.readyState") == "ended"
    assert not errors


def test_permission_sheet_return_cannot_start_capture_for_replaced_session(live_page):
    page, state, _, requests, errors = live_page
    page.evaluate("""captureSources=()=>new Promise(resolve=>window.resolveAudio=resolve);
                     void beginCapture();""")
    state.update(session_id="live-second")
    page.evaluate("value => render(value)", state)
    page.evaluate("resolveAudio([{name:'microphone',stream:STREAM}])")
    page.wait_for_timeout(150)
    assert page.evaluate("RECORDERS.length") == 0
    assert page.evaluate("TRACK.readyState") == "ended"
    assert requests == [] and not errors


def test_permanent_upload_error_stops_capture_with_visible_reason(live_page):
    page, _, responses, requests, errors = live_page
    responses.append((409, {"error": "Listening permission ended"}))
    start_clip(page)
    page.wait_for_function("!runningCapture")
    assert "Listening permission ended" in page.locator("#audioNotice").inner_text()
    assert page.evaluate("TRACK.readyState") == "ended"
    assert len(requests) == 1 and not errors
