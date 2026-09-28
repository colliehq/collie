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
    state = {"session_id": "live-capsule-test", "active": True, "audio": {}, "events": [],
             "capabilities": {"speech_ready": True, "capsule_voice_local": True,
                              "speech_missing": []}}
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
                if control.get("intent_status"):
                    return reply(request, control["intent_status"],
                                 {"error": "intent model returned an error"})
                return reply(request, 200, control["intent"])
            if path == "/api/live-copilot/capsule-text":
                body = json.loads(request.request.post_data)
                control.setdefault("native_texts", []).append(body)
                audio = state["audio"]
                previous = int(audio.get("capsule_seq", -1))
                if body["seq"] <= previous:
                    return reply(request, 202, {"ok": True, "duplicate": True})
                if body["seq"] != previous + 1:
                    return reply(request, 409, {"error": "audio sequence gap"})
                audio["capsule_seq"] = body["seq"]
                audio.setdefault("capsule_results", []).append(
                    {"seq": body["seq"], "text": body["text"], "error": ""})
                return reply(request, 202, {"ok": True, "seq": body["seq"]})
            if path == "/api/live-copilot/dictate":
                control.setdefault("dictated", []).append(json.loads(request.request.post_data))
                if control.get("dictate_error"):
                    return reply(request, 409, {"error": control["dictate_error"]})
                body = json.loads(request.request.post_data)
                return reply(request, 201, {"ok": True, "inserted": len(body["text"]),
                                            "submitted": False, "control": "Edit"})
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
    assert page.evaluate("HOST_MESSAGES.map(x=>x.type)").count("capsule-recording-ended") == 0
    assert not errors


def _ended(page):
    return page.evaluate("HOST_MESSAGES.filter(x=>x.type==='capsule-recording-ended')"
                         ".map(x=>x.recording)")


def test_the_host_hears_when_its_recording_ends_so_live_listening_resumes(capsule_page):
    page, _previews, _finals, _commands, errors, _control = capsule_page
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true,recording:'7'})")
    page.wait_for_function("REC && REC.state==='recording'")
    assert _ended(page) == []                        # still recording: listening stays off
    page.evaluate("REC.emit('words');sendHostMessage({type:'capsule-record-stop'})")
    page.wait_for_function("HOST_MESSAGES.some(x=>x.type==='capsule-recording-ended')")
    page.wait_for_timeout(300)
    assert _ended(page) == ["7"]                     # exactly once, for exactly that recording
    assert not errors


def test_a_recording_the_page_cannot_make_is_ended_at_once(capsule_page):
    page, _previews, _finals, _commands, errors, _control = capsule_page
    page.evaluate("ROUTING=true;sendHostMessage({type:'capsule-record-start',push_to_talk:true,"
                  "recording:'8'})")
    assert _ended(page) == ["8"]
    page.evaluate("ROUTING=false")
    assert not errors


def test_a_replaced_session_ends_the_hosts_recording(capsule_page):
    page, _previews, _finals, _commands, errors, control = capsule_page
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true,recording:'9'})")
    page.wait_for_function("REC && REC.state==='recording'")
    control["state"]["session_id"] = "live-replacement"
    page.evaluate("load()")
    page.wait_for_function("HOST_MESSAGES.some(x=>x.type==='capsule-recording-ended')")
    assert _ended(page) == ["9"] and not errors


def test_a_second_press_while_recording_is_ended_once_under_its_new_id(capsule_page):
    page, _previews, _finals, _commands, errors, _control = capsule_page
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true,recording:'10'})")
    page.wait_for_function("REC && REC.state==='recording'")
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true,recording:'11'})")
    page.evaluate("REC.emit('words');sendHostMessage({type:'capsule-record-stop'})")
    page.wait_for_function("HOST_MESSAGES.some(x=>x.type==='capsule-recording-ended')")
    page.wait_for_timeout(300)
    assert _ended(page) == ["11"] and not errors


def test_native_shell_pauses_continuous_listening_while_the_capsule_records():
    native = (Path(__file__).parents[1] / "harness" / "wallpaper" / "Program.cs").read_text(
        encoding="utf-8")
    resume = native.split("static void ResumeLiveSpeech()", 1)[1].split("try", 1)[0]
    assert "_capsuleRecording" in resume          # nothing restarts it mid-recording
    opening = native.split("void OpenLiveCapsule", 1)[1].split("try", 1)[0]
    assert "_capsuleRecording = true;" in opening and "SuspendLiveSpeech();" in opening
    begin = native.split("static void BeginCapsuleRecording(", 1)[1].split("static void", 1)[0]
    assert "SuspendLiveSpeech();" in begin and '\\"recording\\"' in begin
    end = native.split("static void EndCapsuleRecording(", 1)[1].split("static void", 1)[0]
    assert "_capsuleRecordingId" in end and "ResumeLiveSpeech();" in end
    assert 'EndCapsuleRecording(JsonField(raw, "recording"))' in native
    assert native.count("BeginCapsuleRecording(") == 4   # the definition and all three starts
    closed = native.split("form.FormClosed += delegate", 1)[1].split("};", 1)[0]
    assert "_capsuleRecording = false;" in closed


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


# --- Command or dictation ------------------------------------------------------------------------

def test_dictated_speech_is_typed_into_the_captured_field_not_run(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    control["receipt_text"] = "Ship the fix on Friday after the review."
    control["intent"] = {"mode": "dictation", "confidence": 0.95}
    _record(page, "dictated words")
    page.wait_for_function("document.getElementById('answer').textContent.includes('without submitting')",
                           timeout=10000)
    assert control["intents"] == [{"handoff_id": "handoff-1",
                                   "text": "Ship the fix on Friday after the review."}]
    assert control["dictated"] == [{"handoff_id": "handoff-1",
                                    "text": "Ship the fix on Friday after the review."}]
    assert not control["streams"] and not commands and not errors


def test_dictation_that_cannot_find_a_field_keeps_the_text_and_runs_nothing(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    control["intent"] = {"mode": "dictation", "confidence": 0.95}
    control["dictate_error"] = "the captured window has no focused editable field"
    page.evaluate("run('Ship the fix on Friday', 'voice')")
    page.wait_for_function("document.getElementById('status').textContent.includes('no focused')")
    assert page.locator("#command").input_value() == "Ship the fix on Friday"
    assert not control["streams"] and not commands and not errors


def test_ambiguous_acknowledgement_asks_instead_of_launching(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    control["intent"] = {"mode": "clarify", "confidence": 0.99}
    page.evaluate("run('Yeah.', 'voice')")
    page.wait_for_function("!ROUTING")
    assert not page.evaluate("RUN")
    assert page.locator("#command").input_value() == "Yeah."
    assert "clearer instruction" in page.locator("#status").inner_text()
    assert not control["streams"] and not commands and not errors


def test_action_like_quote_is_classified_before_anything_runs(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    control["intent"] = {"mode": "clarify", "confidence": 0.9}
    page.evaluate("run('Clear the board is the phrase I am quoting, not a request', 'voice')")
    page.wait_for_function("!ROUTING")
    assert len(control["intents"]) == 1
    assert not commands and not control["streams"] and not errors


def test_typed_commands_skip_the_classifier(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    page.locator("#command").fill("Explain the design")
    page.locator("#command").press("Enter")
    page.wait_for_function("document.getElementById('answer').textContent.includes('Board command')")
    assert control["intents"] == [] and len(control["streams"]) == 1 and not errors


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_a_failing_classifier_falls_back_to_running_the_command(capsule_page, failure):
    page, _, _, commands, errors, control = capsule_page
    if failure == "error":
        control["intent_status"] = 409
    else:
        control["hold_intent"] = True
        page.evaluate("INTENT_TIMEOUT_MS=300")
    page.evaluate("run('Explain the design', 'voice')")
    page.wait_for_function("document.getElementById('answer').textContent.includes('Board command')",
                           timeout=10000)
    assert len(control["intents"]) == 1 and len(control["streams"]) == 1
    assert [command["text"] for command in commands] == ["Explain the design"]
    assert not errors


def test_late_intent_cannot_dispatch_after_target_changes(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    control["hold_intent"] = True
    page.evaluate("void run('Explain this design', 'voice')")
    page.wait_for_function("ROUTING && document.getElementById('command').disabled")
    page.wait_for_timeout(100)  # let the routing request reach the fake server
    assert len(control["held_intents"]) == 1
    page.evaluate("sendHostMessage({type:'capsule-context',target:{title:'New target'}})")
    control["held_intents"][0].fulfill(status=200, content_type="application/json",
                                       body='{"mode":"command","confidence":0.99}')
    page.wait_for_timeout(300)
    assert not commands and not control["streams"] and not errors


# --- Without local SenseVoice: the Windows recognizer hears the command ------------------------

def _no_sensevoice(control, native=True):
    control["state"]["capabilities"] = {"speech_ready": False, "capsule_voice_local": native,
                                        "speech_missing": ["SenseVoice model", "ffmpeg"]}


def _host_types(page):
    return page.evaluate("HOST_MESSAGES.map(x=>x.type)")


def test_without_sensevoice_the_windows_recognizer_hears_one_command(capsule_page):
    page, previews, finals, commands, errors, control = capsule_page
    _no_sensevoice(control)
    control["intent"] = {"mode": "command", "confidence": 0.99}
    page.evaluate("HOST_MESSAGES.length=0;"
                  "sendHostMessage({type:'capsule-record-start',push_to_talk:true,recording:'3'})")
    page.wait_for_function("HOST_MESSAGES.some(x=>x.type==='capsule-native-start')")
    start = page.evaluate("HOST_MESSAGES.find(x=>x.type==='capsule-native-start')")
    generation = start["generation"]
    assert page.evaluate("RECORDERS.length") == 0            # no second microphone reader
    # An untagged or stale message is not this recording.
    page.evaluate("sendHostMessage({type:'capsule-speech-partial',text:'someone else'})")
    page.evaluate("(g)=>sendHostMessage({type:'capsule-speech-partial',text:'Open my',"
                  "generation:g})", generation)
    assert page.locator("#command").input_value() == "Open my"
    page.evaluate("sendHostMessage({type:'capsule-record-stop'})")    # X2 released
    assert "capsule-native-stop" in _host_types(page)
    page.evaluate("(g)=>sendHostMessage({type:'capsule-speech-final',text:'Open my notes',"
                  "generation:g})", generation)
    page.evaluate("(g)=>sendHostMessage({type:'capsule-speech-final',text:'Open my notes',"
                  "generation:g})", generation)                   # delivered twice
    page.wait_for_function("document.getElementById('answer').textContent.includes('Board command')",
                           timeout=10000)
    assert control["native_texts"] == [{"session_id": "live-capsule-test", "seq": 0,
                                        "text": "Open my notes"}]
    assert [command["text"] for command in commands] == ["Open my notes"]
    assert len(control["streams"]) == 1 and not finals and not previews
    assert page.evaluate("HOST_MESSAGES.filter(x=>x.type==='capsule-recording-ended')"
                         ".map(x=>x.recording)") == ["3"]
    assert not errors


def test_windows_recognizer_silence_says_so_and_runs_nothing(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    _no_sensevoice(control)
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:false,recording:'4'})")
    page.wait_for_function("HOST_MESSAGES.some(x=>x.type==='capsule-native-start')")
    page.evaluate("sendHostMessage({type:'capsule-speech-final',text:'',"
                  "generation:HOST_MESSAGES.find(x=>x.type==='capsule-native-start').generation})")
    page.wait_for_function("document.getElementById('status').textContent.includes(\"didn't catch\")")
    assert not commands and not control["streams"] and not errors


def test_a_windows_recognizer_error_names_what_is_missing(capsule_page):
    page, _, _, commands, errors, control = capsule_page
    _no_sensevoice(control)
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true,recording:'5'})")
    page.wait_for_function("HOST_MESSAGES.some(x=>x.type==='capsule-native-start')")
    page.evaluate("sendHostMessage({type:'capsule-speech-error',message:'Windows speech "
                  "recognition unavailable: no recognizer',generation:HOST_MESSAGES.find("
                  "x=>x.type==='capsule-native-start').generation})")
    status = page.locator("#status").inner_text()
    assert "no recognizer" in status and "SenseVoice model" in status
    assert "capsule-recording-ended" in _host_types(page)
    assert not commands and not errors


def test_without_any_local_speech_the_capsule_says_so_before_recording(capsule_page):
    page, _, finals, commands, errors, control = capsule_page
    _no_sensevoice(control, native=False)
    page.evaluate("sendHostMessage({type:'capsule-record-start',push_to_talk:true,recording:'6'})")
    page.wait_for_function("document.getElementById('status').textContent.includes('missing')")
    status = page.locator("#status").inner_text()
    assert "SenseVoice model" in status and "ffmpeg" in status and "Type the command" in status
    assert page.evaluate("RECORDERS.length") == 0
    assert "capsule-native-start" not in _host_types(page)
    assert "capsule-recording-ended" in _host_types(page)
    assert not finals and not commands and not errors


def test_native_shell_runs_the_windows_recognizer_only_when_the_page_asks():
    native = (Path(__file__).parents[1] / "harness" / "wallpaper" / "Program.cs").read_text(
        encoding="utf-8")
    assert 'StartCapsuleSpeech(JsonField(raw, "language"), JsonField(raw, "generation"))' in native
    assert native.count("StartCapsuleSpeech(") == 2      # its definition and that one request
    speech = native.split("static void StartCapsuleSpeech(", 1)[1].split("void PostCapsuleTarget", 1)[0]
    for kind in ("capsule-speech-partial", "capsule-speech-final", "capsule-speech-error",
                 "capsule-speech-start"):
        line = next(row for row in speech.splitlines() if kind in row)
        assert "tag" in line or "tag" in speech.split(kind, 1)[1].split(";", 1)[0]
    assert "capsule-native-stop" in native and "capsule-native-cancel" in native
