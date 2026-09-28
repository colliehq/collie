"""Exercise the actual browser uploader with synthetic clips, never a microphone."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
HTML = (Path(__file__).parents[1] / "harness/webui/live.html").read_text("utf-8")
# JSON requests the page posted in the current test, other than audio: (path, body).
POSTS = []
# When set, every /api/ request must carry this token; /api/session-token hands it out.
AUTH = {"token": None}


@pytest.fixture
def live_page():
    requests = []
    state = {"session_id": "live-first", "active": True, "listen": True,
             "understand": False, "audio": {"microphone_seq": 4}, "capabilities": {}}
    responses = []
    POSTS.clear()
    AUTH["token"] = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(request):
            path = urlparse(request.request.url).path
            if path == "/live":
                return request.fulfill(status=200, content_type="text/html", body=HTML)
            if path == "/api/session-token":
                return request.fulfill(status=200, content_type="application/json",
                                       body=json.dumps({"token": AUTH["token"] or ""}))
            token = parse_qs(urlparse(request.request.url).query).get("token", [""])[0]
            if AUTH["token"] and path.startswith("/api/") and token != AUTH["token"]:
                return request.fulfill(status=403, content_type="application/json",
                                       body='{"error":"forbidden"}')
            if request.request.method == "POST" and path != "/api/live-copilot/audio":
                POSTS.append((path, json.loads(request.request.post_data or "{}")))
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


# --- Collie restarted, or stopped answering -------------------------------------------------------

def test_token_rotation_retries_identical_audio_without_a_new_sequence(live_page):
    page, _, _, requests, errors = live_page
    AUTH["token"] = "rotated-test-token"          # Collie restarted with a new token
    start_clip(page)
    page.wait_for_function("RECORDERS.length===2", timeout=5000)
    assert len(requests) == 1                      # the 403 never reached the audio handler
    assert requests[0]["query"]["token"] == ["rotated-test-token"]
    assert requests[0]["query"]["seq"] == ["5"] and requests[0]["body"] == b"captured-clip"
    assert page.evaluate("runningCapture") and not errors


def test_page_keeps_working_after_collie_restarts_with_a_new_token(live_page):
    page, state, _, _, errors = live_page
    AUTH["token"] = "rotated-test-token"
    state["context"] = "after restart"
    page.evaluate("load()")
    page.wait_for_function("STATE.context==='after restart'")
    assert page.locator("#connectionNotice").is_hidden()
    assert page.locator("#statusText").inner_text() == "Maintaining context"
    assert not errors


def test_connection_failure_does_not_leave_green_capture_or_dispatch_draft(live_page):
    page, _, _, requests, errors = live_page
    page.locator("#task").fill("Keep this draft")
    page.evaluate("streams.push(STREAM);beginCapture([{name:'microphone',stream:STREAM}])")
    page.route("**/api/live-copilot?*", lambda route: route.abort())
    page.evaluate("load()")
    page.wait_for_function("document.getElementById('statusText').textContent==='Connection interrupted'")
    assert page.locator("#connectionNotice").is_visible()
    assert page.locator("#doNow").is_disabled()
    assert not page.locator("#capture").evaluate("(e)=>e.classList.contains('on')")
    assert page.evaluate("TRACK.readyState") == "ended"
    page.evaluate("runNow()")
    page.wait_for_timeout(200)
    assert page.locator("#task").input_value() == "Keep this draft"
    assert requests == [] and POSTS == [] and not errors

    page.unroute("**/api/live-copilot?*")
    page.evaluate("load()")
    page.wait_for_function("document.getElementById('statusText').textContent==='Maintaining context'")
    assert page.locator("#connectionNotice").is_hidden()
    assert page.locator("#doNow").is_enabled()


def test_do_now_hands_off_once_even_when_pressed_twice(live_page):
    page, _, _, _, errors = live_page
    page.evaluate("Object.defineProperty(navigator,'clipboard',"
                  "{value:{writeText:async()=>{}},configurable:true})")
    page.locator("#task").fill("Summarize the review")
    page.evaluate("runNow();runNow()")
    page.wait_for_function("document.getElementById('activeNotice').textContent.includes('copied')")
    page.wait_for_timeout(300)
    events = [body for path, body in POSTS if path == "/api/live-copilot/event"]
    assert [event["text"] for event in events] == ["Summarize the review"]
    assert events[0]["session_id"] == "live-first"
    assert page.locator("#task").input_value() == "" and not errors


def test_do_now_does_not_hand_off_into_a_session_that_ended(live_page):
    page, state, _, _, errors = live_page
    page.locator("#task").fill("Summarize the review")
    state.update(active=False)                   # ended elsewhere; this page has not polled yet
    page.evaluate("runNow()")
    page.wait_for_timeout(500)
    assert [path for path, _ in POSTS if path == "/api/live-copilot/event"] == []
    assert page.locator("#task").input_value() == "Summarize the review"
    assert not errors


def test_conversation_label_and_docs_agree_that_only_the_microphone_is_captured():
    root = Path(__file__).parents[1]
    label = HTML.split('id="listen"', 1)[1].split("</label>", 1)[0]
    docs = (root / "docs" / "interviews.md").read_text(encoding="utf-8")
    assert "Microphone only" in label and "not captured" in label
    assert "system audio" not in label.replace("system audio is not captured", "")
    assert "meeting/system audio and retain" not in docs
    assert "requests microphone and system audio" not in docs
    assert "microphone only" in docs.casefold()
