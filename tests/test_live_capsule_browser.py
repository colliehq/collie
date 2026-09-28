"""Drive the push-to-talk capsule in Chromium against a fake Collie server.

The fake server follows the real capsule contract: audio sequence numbers must continue from the
session's ``capsule_seq`` (a gap is refused), and each accepted clip gets a receipt for its exact
``seq`` in ``audio.capsule_results``. No microphone, model, or real Collie state is involved.
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
HTML = (Path(__file__).parents[1] / "harness/webui/live_capsule.html").read_text("utf-8")


@pytest.fixture
def capsule_page():
    state = {"session_id": "live-capsule-test", "active": True, "audio": {}, "events": []}
    previews, finals, commands = [], [], []
    control = {"token": "capsule-boot-one", "refreshes": 0, "rejected": [], "deny_refresh": False,
               "state": state, "final_responses": [], "receipt": True, "receipt_text":
               "帮我清空一下画板。", "streams": [], "stream_disconnect": False, "intents": [],
               "held_intents": [], "hold_intent": False,
               "intent": {"mode": "command", "confidence": 0.99}}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def reply(request, status, payload):
            return request.fulfill(status=status, content_type="application/json",
                                   body=json.dumps(payload))

        def route(request):
            parsed = urlparse(request.request.url)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/live-capsule":
                return request.fulfill(status=200, content_type="text/html", body=HTML.replace(
                    '<meta charset="utf-8">', '<meta charset="utf-8">'
                    '<meta name="collie-token" content="capsule-boot-one">'))
            if path == "/api/session-token":
                control["refreshes"] += 1
                if control["deny_refresh"]:
                    return reply(request, 403, {"error": "forbidden"})
                return reply(request, 200, {"token": control["token"], "boot": control["token"]})
            if path.startswith("/api/") and query.get("token") != [control["token"]]:
                control["rejected"].append({"path": path, "query": query,
                                            "body": request.request.post_data_buffer})
                return reply(request, 403, {"error": "forbidden"})
            if path == "/api/settings":
                return reply(request, 200, {"values": {"LANG": "zh"}})
            if path == "/api/live-copilot/audio-preview":
                previews.append(request.request.post_data_buffer)
                return reply(request, 200, {"ok": True, "text": "帮我清空一下画板。", "final": False,
                                            "language": "auto", "engine": "SenseVoice"})
            if path == "/api/live-copilot/audio":
                body = request.request.post_data_buffer
                finals.append({"query": query, "body": body})
                if control["final_responses"]:
                    status, payload = control["final_responses"].pop(0)
                    if status != 202:
                        return reply(request, status, payload)
                seq = int(query["seq"][0])
                audio = state["audio"]
                previous = int(audio.get("capsule_seq", -1))
                if seq <= previous:
                    if control.get("last_body") == body:
                        return reply(request, 200, {"ok": True, "duplicate": True, "seq": seq})
                    return reply(request, 409, {"error": "audio sequence already accepted"})
                if seq != previous + 1:
                    return reply(request, 409, {"error": "audio sequence gap: expected %d" %
                                                (previous + 1)})
                audio["capsule_seq"], control["last_body"] = seq, body
                if control["receipt"]:
                    audio.setdefault("capsule_results", []).append(
                        {"seq": seq, "text": control["receipt_text"], "error": ""})
                return reply(request, 202, {"ok": True, "queued": True, "seq": seq})
            if path == "/api/live-copilot/intent":
                control["intents"].append(json.loads(request.request.post_data))
                if control["hold_intent"]:
                    control["held_intents"].append(request)
                    return None
                return reply(request, 200, control["intent"])
            if path == "/api/live-copilot/handoff":
                return reply(request, 201, {"id": "handoff-1", "pending": True})
            if path == "/api/live-copilot/event":
                commands.append(json.loads(request.request.post_data or "{}"))
                return reply(request, 201, {"ok": True})
            if path == "/api/live-copilot/handoff/resolve":
                return reply(request, 200, {"ok": True})
            if path == "/api/stream":
                control["streams"].append(query)
                body = 'event: done\ndata: {"answer":"Board command received."}\n\n'
                if control["stream_disconnect"]:
                    body = 'event: start\ndata: {}\n\n'
                return request.fulfill(status=200, content_type="text/event-stream", body=body)
            if path == "/api/runs":
                runs = []
                if control["streams"] and control["streams"][-1].get("session"):
                    runs = [{"session": control["streams"][-1]["session"][0],
                             "ended": 1, "state": "done", "error": ""}]
                return reply(request, 200, {"runs": runs})
            if path.startswith("/api/session/"):
                return reply(request, 200, {"messages": [
                    {"role": "assistant", "content": "Recovered existing result."}]})
            if path == "/api/live-copilot":
                return reply(request, 200, state)
            return reply(request, 200, {})

        page.route("**/*", route)
        page.add_init_script("""
          window.RECORDERS=[];
          window.HOST_MESSAGES=[]; window.HOST_LISTENERS=[];
          window.chrome=window.chrome||{};
          window.chrome.webview={postMessage(d){HOST_MESSAGES.push(d)},
            addEventListener(kind,handler){if(kind==='message')HOST_LISTENERS.push(handler)}};
          window.sendHostMessage=(data)=>HOST_LISTENERS.forEach(handler=>handler({data}));
          window.MediaRecorder=class {
            static isTypeSupported(){return true}
            constructor(stream,options){this.state='inactive';this.options=options;RECORDERS.push(this)}
            start(timeslice){this.timeslice=timeslice;this.state='recording'}
            emit(value){if(this.ondataavailable)this.ondataavailable({data:new Blob([value],{type:this.options.mimeType})})}
            stop(){if(this.state==='inactive')return;this.state='inactive';if(this.onstop)this.onstop()}
          };
          window.TRACK={readyState:'live',stop(){this.readyState='ended'}};
          window.STREAM={getTracks(){return[TRACK]},getAudioTracks(){return[TRACK]}};
          Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async()=>STREAM}});
          window.AudioContext=class {
            createMediaStreamSource(){return{connect(){}}}
            createAnalyser(){return{frequencyBinCount:8,fftSize:0,getByteTimeDomainData(a){a.fill(128)}}}
            close(){return Promise.resolve()}
          };
        """)
        page.goto("http://collie.test/live-capsule")
        page.evaluate("load().then(beginHandoff)")
        page.wait_for_function("STATE.session_id==='live-capsule-test'")
        yield page, previews, finals, commands, errors, control
        browser.close()


def _until(page, predicate, timeout_ms=10000):
    """Let the page and the fake server run until a server-side condition holds."""
    waited = 0
    while not predicate() and waited < timeout_ms:
        page.wait_for_timeout(50)
        waited += 50
    return predicate()


def _record(page, *chunks):
    """Hold X2, speak, release — through the same host messages the native shell sends."""
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true})")
    page.wait_for_function("RECORDERS.length && RECORDERS[RECORDERS.length-1].state==='recording'")
    for chunk in chunks:
        page.evaluate("(c)=>RECORDERS[RECORDERS.length-1].emit(c)", chunk)
    page.evaluate("sendHostMessage({type:'capsule-record-stop'})")


def test_first_clip_continues_the_session_sequence_and_runs_its_own_transcript(capsule_page):
    page, _previews, finals, commands, errors, _control = capsule_page
    _record(page, "first-", "second")
    assert _until(page, lambda: finals)
    # The session has accepted no capsule clip yet, so the first one is sequence 0.
    assert [final["query"]["seq"] for final in finals] == [["0"]]
    page.wait_for_function("document.getElementById('answer').textContent.includes('Board command')",
                           timeout=10000)
    assert finals[0]["body"] == b"first-second"
    assert commands[-1]["kind"] == "command" and commands[-1]["text"] == "帮我清空一下画板。"
    assert not errors


def test_live_text_appears_while_talking_then_the_final_clip_decides(capsule_page):
    page, previews, finals, commands, errors, _control = capsule_page
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true})")
    page.wait_for_function("RECORDERS.length===1 && RECORDERS[0].state==='recording'")
    # Short slices, so the page has audio to preview while the key is still held.
    assert page.evaluate("RECORDERS[0].timeslice") == 250
    page.evaluate("RECORDERS[0].emit('first-');RECORDERS[0].emit('second')")
    page.wait_for_function("document.getElementById('command').value==='帮我清空一下画板。'",
                           timeout=5000)
    assert previews and previews[0] == b"first-second"   # the whole recording so far
    assert "live text" in page.locator("#status").inner_text()
    assert not finals and not commands                   # a preview never runs anything
    page.evaluate("sendHostMessage({type:'capsule-record-stop'})")
    page.wait_for_function("document.getElementById('answer').textContent.includes('Board command')",
                           timeout=10000)
    assert len(finals) == 1 and finals[0]["body"] == b"first-second"
    assert [command["text"] for command in commands] == ["帮我清空一下画板。"]
    assert not errors


def test_busy_speech_queue_retries_the_same_clip_and_sequence(capsule_page):
    page, _previews, finals, _commands, errors, control = capsule_page
    control["final_responses"][:] = [(429, {"code": "live_audio_busy", "retry_after_ms": 250})]
    _record(page, "same-", "utterance")
    page.wait_for_function("document.getElementById('answer').textContent.includes('Board command')",
                           timeout=10000)
    assert len(finals) == 2 and finals[0] == finals[1]
    assert finals[0]["query"]["seq"] == ["0"] and finals[0]["body"] == b"same-utterance"
    assert not errors


def test_nearby_event_is_never_used_as_the_final_transcript(capsule_page):
    page, _previews, finals, commands, errors, control = capsule_page
    control["receipt"] = False
    # One earlier clip was accepted, so both old and new pages send a sequence the server takes.
    control["state"]["audio"]["capsule_seq"] = 0
    # A continuous-listening transcript that happens to land right after the capsule clip.
    control["state"]["events"].append({"id": "evt-near", "source": "you", "kind": "speech",
                                       "text": "delete the old branch", "at_ms": 10 ** 15})
    _record(page, "new speech")
    assert _until(page, lambda: finals) and finals[0]["query"]["seq"] == ["1"]
    page.wait_for_timeout(2500)
    assert "delete the old branch" not in [command.get("text") for command in commands]
    assert not commands and not control["streams"] and not errors


def test_capsule_recovers_rotated_token_without_losing_typed_draft(capsule_page):
    page, _previews, _finals, _commands, errors, control = capsule_page
    page.locator("#command").fill("Explain my existing architecture")
    page.evaluate("window.sameDocumentMarker=42")
    control["token"] = "capsule-boot-two"

    page.evaluate("Promise.all([load(),api('/api/settings')])")

    assert control["refreshes"] == 1
    assert len(control["rejected"]) == 2
    assert page.locator("#command").input_value() == "Explain my existing architecture"
    assert page.evaluate("sameDocumentMarker") == 42
    assert page.evaluate("STATE.active")
    assert not page.locator("#status").evaluate("(e)=>e.classList.contains('bad')")
    assert not errors


def test_audio_retry_after_token_rotation_preserves_same_clip_and_sequence(capsule_page):
    page, _previews, finals, commands, errors, control = capsule_page
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true})")
    page.wait_for_function("REC && REC.state === 'recording'")
    page.evaluate("REC.emit('same-');REC.emit('utterance')")
    control["token"] = "capsule-boot-two"
    page.evaluate("sendHostMessage({type:'capsule-record-stop'})")
    page.wait_for_function("document.getElementById('answer').textContent.includes('Board command')",
                           timeout=10000)

    denied = [r for r in control["rejected"] if r["path"] == "/api/live-copilot/audio"]
    assert len(denied) == 1 and control["refreshes"] == 1 and len(finals) == 1
    assert denied[0]["body"] == finals[0]["body"] == b"same-utterance"
    assert denied[0]["query"]["seq"] == finals[0]["query"]["seq"] == ["0"]
    assert len(commands) == 1 and not errors


def test_failed_reconnection_does_not_fall_through_to_the_agent(capsule_page):
    page, _previews, _finals, commands, errors, control = capsule_page
    control["token"] = "capsule-boot-two"
    control["deny_refresh"] = True
    page.locator("#command").fill("clear the board")
    page.locator("#command").press("Enter")
    page.wait_for_timeout(500)

    assert not commands and not control["streams"]
    assert page.evaluate("STATE.session_id") == "live-capsule-test"
    assert page.locator("#status").evaluate("(e)=>e.classList.contains('bad')")
    assert not errors


def test_eventsource_disconnect_queries_existing_run_without_relaunch(capsule_page):
    page, _previews, _finals, commands, errors, control = capsule_page
    control["stream_disconnect"] = True
    page.locator("#command").fill("Explain the existing design")
    page.locator("#command").press("Enter")
    assert _until(page, lambda: control["streams"])
    page.wait_for_timeout(3500)  # past EventSource's own reconnect delay, which reruns the URL
    assert len(control["streams"]) == len(commands) == 1
    page.wait_for_function("document.getElementById('answer').textContent.includes('Recovered existing')",
                           timeout=10000)
    assert control["streams"][0]["session"][0].startswith("capsule-")
    assert control["streams"][0]["authority_text"] == ["Explain the existing design"]
    assert not errors


def test_capsule_context_does_not_ask_the_host_for_a_second_recording(capsule_page):
    page, _previews, _finals, _commands, errors, _control = capsule_page
    # The native shell sends the target and starts the recording itself on capsule-ready.
    page.evaluate("HOST_MESSAGES.length=0;sendHostMessage({type:'capsule-context',"
                  "target:{process:'code',title:'notes.md',pid:4,hwnd:9}})")
    page.wait_for_function("document.getElementById('target').textContent.includes('notes.md')")
    page.wait_for_timeout(300)
    assert page.evaluate("HOST_MESSAGES.map(x=>x.type)").count("capsule-listen") == 0
    assert not errors


def test_untagged_native_speech_never_authorizes_a_command(capsule_page):
    page, _previews, _finals, commands, errors, control = capsule_page
    page.evaluate("sendHostMessage({type:'capsule-speech-final',text:'clear the board'})")
    page.evaluate("sendHostMessage({type:'capsule-speech-partial',text:'The.'})")
    page.wait_for_timeout(300)
    assert page.locator("#command").input_value() == ""
    assert not commands and not control["streams"] and not errors


def test_escape_closes_while_input_disabled_and_discards_recording(capsule_page):
    page, _previews, finals, commands, errors, _control = capsule_page
    page.evaluate("startVoice(true)")
    page.wait_for_function("REC && REC.state === 'recording'")
    page.evaluate("REC.emit('do not submit this');document.getElementById('command').disabled=true")
    page.locator("#lang").focus()
    page.keyboard.press("Escape")

    assert page.evaluate("CLOSED && REC === null && TRACK.readyState === 'ended'")
    assert page.evaluate("HOST_MESSAGES.filter(x=>x.type==='capsule-close').length") == 1
    assert not finals and not commands and not errors


def test_session_rotation_resets_audio_sequence_and_requires_fresh_utterance(capsule_page):
    page, _previews, _finals, commands, errors, control = capsule_page
    page.evaluate("CAPSULE_SEQ=8")
    control["state"]["session_id"] = "live-replacement"
    page.evaluate("run('clear the board', 'voice')")

    assert page.evaluate("CAPSULE_SEQ") == -1
    assert "Session changed" in page.locator("#status").inner_text()
    assert not commands and not errors


@pytest.mark.parametrize("text", [".", "。", "…?!", "🎵", "The.", "Uh, um."])
def test_punctuation_or_filler_never_routes_as_a_voice_command(capsule_page, text):
    page, _, _, commands, errors, control = capsule_page
    page.evaluate("(text)=>run(text,'voice')", text)
    page.wait_for_timeout(200)
    assert not control["intents"] and not control["streams"] and not commands
    assert page.locator("#command").input_value() == ""
    assert not errors


def test_upload_failure_keeps_the_heard_text_without_executing(capsule_page):
    page, _, finals, commands, errors, control = capsule_page
    control["final_responses"][:] = [(500, {"error": "decoder unavailable"})]
    page.evaluate("startVoice(true)")
    page.wait_for_function("REC && REC.state === 'recording'")
    page.evaluate("REC.emit('audio');REC_CLIP.preview='clear the board';stopVoice()")
    page.wait_for_function("REC_CLIP.consumed")
    assert page.locator("#command").input_value() == "clear the board"
    assert "Review the text" in page.locator("#status").inner_text()
    assert len(finals) == 1 and not commands and not errors


def test_missing_receipt_reports_instead_of_guessing(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    control["receipt"] = False
    page.evaluate("startVoice(true)")
    page.wait_for_function("REC && REC.state === 'recording'")
    page.evaluate("REC.emit('new speech');stopVoice()")
    page.wait_for_function("document.getElementById('status').textContent.includes('Finalizing')")
    page.evaluate("clearTimeout(REC_CLIP.timer);waitForVoice(REC_CLIP,48)")
    page.wait_for_function("REC_CLIP.consumed")
    assert "did not arrive" in page.locator("#status").inner_text()
    assert not commands and not control["streams"] and not errors


@pytest.mark.parametrize("boundary", ["close", "session", "recording"])
def test_pending_retry_is_discarded_at_operation_boundary(capsule_page, boundary):
    page, _, finals, commands, errors, control = capsule_page
    control["final_responses"][:] = [(429, {"retry_after_ms": 1200})]
    page.evaluate("startVoice(true)")
    page.wait_for_function("REC && REC.state === 'recording'")
    page.evaluate("REC.emit('old audio');stopVoice()")
    page.wait_for_function("document.getElementById('status').textContent.includes('retrying')")
    if boundary == "close":
        page.evaluate("closeCapsule()")
    elif boundary == "session":
        control["state"]["session_id"] = "replacement"
        page.evaluate("load()")
    else:
        page.evaluate("startVoice(true)")
    page.wait_for_timeout(1300)  # Cross the scheduled retry deadline, then check side effects.
    assert len(finals) == 1 and not commands and not errors


def test_push_to_talk_release_before_microphone_ready_discards_stream(capsule_page):
    page, _, finals, commands, errors, _ = capsule_page
    page.evaluate("navigator.mediaDevices.getUserMedia=()=>new Promise(r=>window.grantMic=r);"
                  "void startVoice(true)")
    page.wait_for_function("typeof grantMic === 'function'")
    page.evaluate("stopVoice();grantMic(STREAM)")
    page.wait_for_function("!VOICE_STARTING")
    assert page.evaluate("REC===null && TRACK.readyState==='ended'")
    assert not finals and not commands and not errors


def test_ptt_release_during_initial_session_load_never_opens_microphone(capsule_page):
    page, _, finals, commands, errors, _ = capsule_page
    page.evaluate("""() => {
      window.MIC_CALLS=0;
      navigator.mediaDevices.getUserMedia=async()=>{MIC_CALLS++;return STREAM};
      STATE={};window.realApi=api;
      api=(path,...args)=>path==='/api/live-copilot'?new Promise(r=>window.resolveState=r):realApi(path,...args);
      void startVoice(true);stopVoice();
      resolveState({session_id:'live-capsule-test',active:true,audio:{}});
    }""")
    page.wait_for_function("!VOICE_STARTING")
    assert page.evaluate("MIC_CALLS") == 0
    assert not finals and not commands and not errors


def test_late_final_poll_cannot_overwrite_new_recording(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    control["receipt"] = False
    page.evaluate("startVoice(true)")
    page.wait_for_function("REC && REC.state === 'recording'")
    page.evaluate("REC.emit('old audio');stopVoice()")
    page.wait_for_function("document.getElementById('status').textContent.includes('Finalizing')")
    page.evaluate("window.oldClip=REC_CLIP;startVoice(true)")
    control["state"]["audio"]["capsule_results"] = [{"seq": 0, "text": "clear the board",
                                                     "error": ""}]
    page.evaluate("waitForVoice(oldClip)")
    page.wait_for_timeout(500)
    assert page.locator("#command").input_value() == ""
    assert not commands and not errors


def test_hands_free_recording_stops_if_nothing_is_heard(capsule_page):
    page, _, finals, _commands, errors, _ = capsule_page
    page.evaluate("startVoice(false)")
    page.wait_for_function("REC && REC.state === 'recording'")
    page.evaluate("REC.emit('room tone');REC_STARTED-=11000;REC_LAST_SOUND-=11000")
    page.wait_for_function("REC === null", timeout=5000)
    page.wait_for_function("window.RECORDERS[0].state === 'inactive'")
    assert len(finals) == 1 and not errors
