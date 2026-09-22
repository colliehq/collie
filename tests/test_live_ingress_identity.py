"""Accepted clip identity and consent must survive retries and broken state."""
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harness import live_copilot as live


@pytest.fixture
def staged(tmp_path, monkeypatch):
    store = live.LiveSessionStore(tmp_path)
    state = store.start(listen=True, consent=True, understand=False,
                        observe_apps=False, observe_ui=False, observe_input=False,
                        observe_screen=False)
    jobs = []
    ingress = store._ingress()
    monkeypatch.setattr(ingress, "submit", jobs.append)
    yield store, state, jobs
    for job in jobs:
        job.abandon()
        ingress.release(job.ticket)


def test_identical_retry_deduplicates_but_different_clip_cannot_steal_sequence(staged):
    store, state, jobs = staged
    args = dict(session_id=state["session_id"], source="microphone", seq=0,
                mime_type="audio/webm", data=b"first microphone clip")
    store.ingest_audio(**args)
    assert store.ingest_audio(**args)["duplicate"] is True
    with pytest.raises(live.LiveCopilotError, match="different or unavailable clip identity"):
        store.ingest_audio(**dict(args, data=b"another browser microphone clip"))
    assert len(jobs) == 1
    assert Path(jobs[0].path).read_bytes() == args["data"]
    assert store.snapshot()["audio"]["pending"] == 1
    assert "recent_chunks" not in store.snapshot()["audio"]


def test_old_capture_permission_cannot_be_resurrected_at_upload(staged):
    store, state, jobs = staged
    old_epoch = state["audio"]["listen_epoch"]
    store.update_permissions(listen=False)
    store.update_permissions(listen=True, consent=True)
    with pytest.raises(live.LiveCopilotError, match="earlier listening permission"):
        store.ingest_audio(session_id=state["session_id"], source="microphone", seq=0,
                           mime_type="audio/webm", data=b"old recording", listen_epoch=old_epoch)
    assert jobs == [] and store.snapshot()["audio"]["microphone_seq"] == -1


def test_corrupt_consent_record_prevents_decoder_and_discards_clip(staged):
    store, state, jobs = staged
    decoded = []
    store.ingest_audio(session_id=state["session_id"], source="microphone", seq=0,
                       mime_type="audio/webm", data=b"private", transcriber=lambda *a, **k: decoded.append(True))
    path = Path(store.path)
    original = path.read_bytes()
    try:
        path.write_text("{broken", "utf-8")
        jobs[0].run()
        assert decoded == []
        assert not Path(jobs[0].path).exists()
        assert path.read_text("utf-8") == "{broken"
    finally:
        path.write_bytes(original)


def test_dead_audio_owner_is_reported_as_a_gap(staged):
    store, _, _ = staged
    with store._transaction():
        value = store._read()
        value["audio"]["pending_by_owner"] = {"dead-test-owner": {"chunks": 2, "bytes": 40}}
        store._write(value)
    audio = store.snapshot()["audio"]
    assert audio["pending"] == 0
    assert "2 accepted clip(s) have no transcript" in audio["last_error"]


def test_busy_audio_http_has_retry_status_header_and_same_identity(tmp_path, monkeypatch):
    from harness import webapp

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))

    def reject(self, **kwargs):
        assert kwargs["listen_epoch"] == "4"
        raise live.LiveCopilotBusyError("speech queue busy", seq=7, source="microphone", retry_after_ms=1500)

    monkeypatch.setattr(live.LiveSessionStore, "ingest_audio", reject)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = ("http://127.0.0.1:%d/api/live-copilot/audio?token=%s&session=fixture&source=microphone&seq=7&listen_epoch=4"
               % (server.server_port, webapp.TOKEN))
        request = urllib.request.Request(url, data=b"synthetic", headers={"Content-Type": "audio/webm"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        error = caught.value
        assert error.code == 429 and error.headers["Retry-After"] == "2"
        body = json.loads(error.read())
        assert body["code"] == "live_audio_busy" and body["seq"] == 7 and body["retryable"] is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
