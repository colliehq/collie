import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from test_channel_service import service, message  # noqa: F401


@pytest.fixture
def web(service):
    from harness import webapp
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN, service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def request(web, path="/api/channels", body=None, authenticated=True):
    base, token, _ = web
    if authenticated:
        path += ("&" if "?" in path else "?") + "token=" + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        content = response.read()
        return response.status, (json.loads(content) if "application/json" in response.headers.get("Content-Type", "") else content), response.headers


def test_channels_and_mail_contents_require_token(web):
    for path in ("/api/channels", "/api/channels/events?connection=mail", "/api/channels/results?connection=mail"):
        assert request(web, path, authenticated=False)[0] == 403
    assert request(web, body={"action": "poll", "connection": "mail"}, authenticated=False)[0] == 403
    status, payload, _ = request(web)
    assert status == 200 and payload["connections"][0]["has_credentials"]
    assert "private-password" not in json.dumps(payload)


def test_edit_and_discard_are_authenticated_and_do_not_send(web):
    _, _, (host, adapter) = web
    prepared = host.prepare_reply("mail", "original-draft", text="Original")
    body = {"action": "revise", "connection": "mail", "id": "original-draft", "new_id": "revised-draft",
            "text": "Corrected", "digest": prepared["digest"]}
    assert request(web, body=body, authenticated=False)[0] == 403
    status, response, _ = request(web, body=body)
    assert status == 200 and response["result"]["state"] == "pending"
    status, response, _ = request(web, body={"action": "discard", "connection": "mail", "id": "revised-draft",
                                           "digest": response["result"]["digest"]})
    assert status == 200 and response["result"]["state"] == "cancelled"
    assert not adapter.sent


def test_received_html_is_returned_as_data_not_rendered_markup(web):
    _, _, (host, adapter) = web
    adapter.messages = [message(text='<script>window.unsafe=1</script>')]
    assert request(web, body={"action": "poll", "connection": "mail"})[0] == 200
    status, payload, headers = request(web, "/api/channels/events?connection=mail")
    assert status == 200 and payload["events"][0]["text"] == '<script>window.unsafe=1</script>'
    assert "application/json" in headers["Content-Type"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert host.events("mail")[0]["state"] == "pending"


def test_unknown_send_has_explicit_state_and_repeated_post_cannot_send_again(web):
    _, _, (_, adapter) = web
    assert request(web, body={"action": "prepare", "connection": "mail", "id": "reply", "text": "Result"})[0] == 200
    adapter.fail = TimeoutError("untrusted SECRET reply body")
    status, payload, _ = request(web, body={"action": "send", "connection": "mail", "id": "reply"})
    assert status == 200 and payload["result"]["state"] == "unknown"
    assert "SECRET" not in json.dumps(payload)
    assert request(web, body={"action": "send", "connection": "mail", "id": "reply"})[0] == 400
    assert len(adapter.sent) == 1


def test_retry_prepares_one_new_attempt_without_sending_it(web):
    _, _, (host, adapter) = web
    class Refused(RuntimeError):
        delivery_unknown = False
    host.prepare_reply("mail", "refused-draft", text="Reviewed result")
    adapter.fail = Refused("provider refused")
    assert request(web, body={"action": "send", "connection": "mail", "id": "refused-draft"})[1]["result"]["state"] == "failed"
    retry = {"action": "retry", "connection": "mail", "id": "refused-draft"}
    status, payload, _ = request(web, body=retry)
    assert status == 200 and payload["result"]["state"] == "pending", payload
    attempt = payload["result"]["id"]
    assert attempt != "refused-draft"
    assert request(web, body=retry)[1]["result"]["id"] == attempt
    assert len(adapter.sent) == 1 and len(host.results("mail")) == 2


def test_attachment_download_is_private_non_executable_and_byte_exact(web):
    import base64
    import hashlib
    _, _, (host, _) = web
    raw = b"<script>doNotExecute()</script>"
    digest = hashlib.sha256(raw).hexdigest()
    host.ingest("mail", message(attachments=[{"name": "page.html", "content_type": "text/html", "bytes": len(raw),
                                            "sha256": digest, "data": base64.b64encode(raw).decode()}]))
    path = "/api/channels/attachment?connection=mail&digest=" + digest
    assert request(web, path, authenticated=False)[0] == 403
    status, content, headers = request(web, path)
    assert status == 200 and content == raw
    assert headers["Content-Type"] == "application/octet-stream"
    assert "attachment" in headers["Content-Disposition"]
    assert headers["Cache-Control"] == "no-store"
    assert request(web, "/api/channels/attachment?connection=mail&digest=..%2Fchannels")[0] == 400


def test_page_is_served_with_script_hash_csp_and_local_token(web):
    status, content, headers = request(web, "/communications", authenticated=False)
    assert status == 200
    assert b'name="collie-token"' in content
    assert "script-src 'self' 'sha256-" in headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in headers["Content-Security-Policy"]


def test_desktop_acceptance_runs_restricted_draft_and_saves_reply_without_sending(web, monkeypatch):
    """Real HTTP, scheduler, lease, loop and outbox; only the model is scripted."""
    import time
    from harness import cli, settings, webapp, web_tasks
    from harness.providers import Completion
    from _util import _ScriptProvider
    _, _, (host, adapter) = web
    original_make = cli.make_harness
    calls = []
    def make(*args, **kwargs):
        h = original_make(*args, **dict(kwargs, embed="bm25"))
        provider = _ScriptProvider([Completion(text="Draft: I received your request and can discuss next steps.")], name="mock", model="mock")
        original_complete = provider.complete
        def complete(system, messages, schemas, **options):
            calls.append({"system": system, "messages": messages, "tools": schemas,
                          "hooks": h.hooks, "composer": type(h.composer).__name__})
            return original_complete(system, messages, schemas, **options)
        provider.complete = complete
        h.provider = provider
        return h
    monkeypatch.setattr(cli, "make_harness", make)
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(webapp.Handler, "_notify_done", staticmethod(lambda *a, **kw: None))
    host.ingest("mail", message(text="Write a short acknowledgment."))
    status, answer, _ = request(web, body={"action": "draft", "connection": "mail", "event": "one"})
    assert status == 200, answer
    sid = answer["result"]["session"]
    deadline = time.monotonic() + 20
    while web_tasks.scheduled(sid) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not web_tasks.scheduled(sid), "draft execution did not finish"
    assert calls, webapp.Handler._runs.get(sid)
    assert all(row["composer"] == "DraftComposer" and not row["tools"] and row["hooks"] is None for row in calls)
    results = host.results("mail")
    assert len(results) == 1, webapp.Handler._runs.get(sid)
    assert results[0]["text"].startswith("Draft:") and results[0]["state"] == "pending"
    assert adapter.sent == []
