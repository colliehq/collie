"""The Web durable-input API, over real HTTP.

What these lock is the difference between "we told you it was accepted" and "it
is on disk": every acknowledgement here has to survive the server process, a
page reload, a retried POST whose first answer nobody saw, and a request that
arrives while the model is already working.  The old ``/api/steer`` failed all
four — it pushed 4000 characters of a person's message onto a queue that the
finishing run threw away, and answered ``{"queued": true}``.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import task_inbox, web_tasks                            # noqa: E402

CONFIG = {"intent": "build", "quality": "balanced", "verification": "auto",
          "workspace": "current", "strategy": "single", "effort": "auto",
          "speed": "standard", "runner": "", "explicit_axes": "none",
          "verify_command": "", "verify_source": "", "n": "3", "check": "",
          "apply": False}


# The Settings panel, as a mutable thing a test can move under a waiting request.
PANEL = {}


@pytest.fixture
def web(monkeypatch, tmp_path):
    """A real server on a real socket, against a temporary session store."""
    from harness import settings, webapp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.chdir(tmp_path)
    PANEL.clear()
    PANEL.update({"PROVIDER": "mock", "MODEL": "model-a", "REASONING_EFFORT": "auto",
                  "INTERACTIVE_SPEED": "standard"})
    monkeypatch.setattr(webapp, "_provider", lambda: PANEL.get("PROVIDER", ""))
    monkeypatch.setattr(settings, "apply", lambda: (_ for _ in ()).throw(
        AssertionError("accepting a request must not rewrite this process's settings")))
    monkeypatch.setattr(settings, "get", lambda key, default=None: PANEL.get(
        key, default if default is not None else ""))
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN, state
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        with webapp.Handler._runs_lock:
            webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()


def _call(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(base, token, path, body):
    return _call(base + path + "?token=" + token, "POST", body)


def _get(base, token, path):
    joiner = "&" if "?" in path else "?"
    return _call(base + path + joiner + "token=" + token)


def _read_store_in_a_fresh_process(state, session):
    """What a restarted server sees — a different interpreter, same disk."""
    code = (
        "import json, os, sys;"
        "sys.path.insert(0, %r);"
        "os.environ['COLLIE_SESSIONS_DIR'] = %r;"
        "from harness import task_inbox;"
        "print(json.dumps([{'id': e['id'], 'state': e['state'], 'mode': e['mode'],"
        " 'text': e['text'], 'digest': e['digest']}"
        " for e in task_inbox.list_entries(%r)]))"
        % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
           str(state / "sessions"), session))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------- accepting

def test_accept_is_exact_durable_idempotent_and_survives_a_restart(web):
    base, token, state = web
    sid = "inbox-durable"
    # Longer than the old 4000-character slice, and every character matters.
    text = "step one\n" + ("x" * 6000) + "\nand finally: do not truncate me"
    body = {"session": sid, "id": "req-1", "text": text, "mode": "follow_up",
            "config": CONFIG, "client": "web"}

    code, refused = _call(base + "/api/task-inbox", "POST", body)
    assert code == 403 and refused["error"] == "forbidden"

    code, accepted = _post(base, token, "/api/task-inbox", body)
    assert code == 200 and accepted["accepted"] is True
    assert accepted["session"] == sid
    entry = accepted["entry"]
    assert entry["id"] == "req-1" and entry["state"] == "pending"
    assert entry["mode"] == "follow_up"
    assert entry["text"] == text, "accepted text is stored exactly as written"
    assert entry["config"]["frozen"]["provider"] == "mock"

    # The browser never saw that answer and retries the identical POST.
    code, retried = _post(base, token, "/api/task-inbox", body)
    assert code == 200 and retried["accepted"] is True
    assert retried["entry"]["duplicate"] is True
    assert retried["entry"]["digest"] == entry["digest"]

    # The same id carrying a different request is a conflict, never an overwrite.
    code, conflict = _post(base, token, "/api/task-inbox", dict(body, text="something else"))
    assert code == 409 and "already accepted with a different message" in conflict["error"]
    assert task_inbox.get(sid, "req-1")["text"] == text, "the stored request is untouched"

    code, second = _post(base, token, "/api/task-inbox", {
        "session": sid, "id": "req-2", "text": "then run the tests", "mode": "follow_up",
        "config": CONFIG})
    assert code == 200 and second["entry"]["state"] == "pending"

    code, listing = _get(base, token, "/api/task-inbox?session=" + sid)
    assert code == 200 and listing["active"] is False
    assert [e["id"] for e in listing["entries"]] == ["req-1", "req-2"]
    assert listing["entries"][0]["text"] == text

    # A restart is not a reason to forget what a person is waiting on.
    restarted = _read_store_in_a_fresh_process(state, sid)
    assert [row["id"] for row in restarted] == ["req-1", "req-2"]
    assert restarted[0]["text"] == text
    assert restarted[0]["digest"] == entry["digest"]


def test_canceling_one_request_leaves_the_others_exactly_as_accepted(web):
    base, token, _state = web
    sid = "inbox-cancel"
    for index, text in enumerate(("first request", "second request", "third request"), 1):
        code, _ = _post(base, token, "/api/task-inbox", {
            "session": sid, "id": "r%d" % index, "text": text, "mode": "follow_up",
            "config": CONFIG})
        assert code == 200
    before = {e["id"]: e["digest"] for e in task_inbox.list_entries(sid)}

    code, canceled = _post(base, token, "/api/task-inbox/cancel", {"session": sid, "id": "r2"})
    assert code == 200 and canceled["entry"]["state"] == "canceled"
    # Cancelling is idempotent, and a cancelled id stays spent.
    code, again = _post(base, token, "/api/task-inbox/cancel", {"session": sid, "id": "r2"})
    assert code == 200 and again["entry"]["state"] == "canceled"

    rows = {e["id"]: e for e in task_inbox.list_entries(sid)}
    assert rows["r1"]["state"] == "pending" and rows["r3"]["state"] == "pending"
    assert rows["r1"]["digest"] == before["r1"] and rows["r3"]["digest"] == before["r3"]
    assert rows["r1"]["text"] == "first request"

    code, missing = _post(base, token, "/api/task-inbox/cancel", {"session": sid, "id": "nope"})
    assert code == 404


def test_edit_revises_a_waiting_request_and_refuses_a_stale_write(web):
    base, token, _state = web
    sid = "inbox-edit"
    _post(base, token, "/api/task-inbox", {"session": sid, "id": "e1", "text": "run the linter",
                                           "mode": "follow_up", "config": CONFIG})
    original = task_inbox.get(sid, "e1")

    code, edited = _post(base, token, "/api/task-inbox/edit", {
        "session": sid, "id": "e1", "text": "run the linter, then the tests",
        "expected_digest": original["digest"]})
    assert code == 200
    assert edited["entry"]["text"] == "run the linter, then the tests"
    assert edited["entry"]["revision"] == 1
    assert edited["entry"]["config"] == original["config"], "settings survive an edit"

    code, stale = _post(base, token, "/api/task-inbox/edit", {
        "session": sid, "id": "e1", "text": "third version",
        "expected_digest": original["digest"]})
    assert code == 409 and "changed since it was read" in stale["error"]
    assert task_inbox.get(sid, "e1")["text"] == "run the linter, then the tests"

    code, empty = _post(base, token, "/api/task-inbox/edit", {"session": sid, "id": "e1",
                                                              "text": "   "})
    assert code == 400


# ------------------------------------------------------------------- validation

def test_input_is_validated_with_actionable_status_codes(web):
    base, token, _state = web
    sid = "inbox-validate"
    cases = [
        ({"session": "../escape", "id": "a", "text": "hi", "config": CONFIG}, 400),
        ({"session": sid, "id": "bad id!", "text": "hi", "config": CONFIG}, 400),
        ({"session": sid, "id": "a", "text": "   ", "config": CONFIG}, 400),
        ({"session": sid, "id": "a", "text": "hi", "mode": "whenever", "config": CONFIG}, 400),
        ({"session": sid, "id": "a", "text": "hi", "config": dict(CONFIG, intent="ship")}, 400),
        ({"session": sid, "id": "a", "text": "hi",
          "config": dict(CONFIG, surprise="value")}, 400),
        ({"session": sid, "id": "a", "text": "hi",
          "config": dict(CONFIG, verification="required")}, 400),
        ({"session": sid, "id": "a", "text": "x" * (task_inbox.MAX_TEXT_BYTES + 1),
          "config": CONFIG}, 413),
        ({"session": sid, "id": "a", "text": "hi", "config": CONFIG,
          "images": ["deadbeefdeadbeef"]}, 409),
    ]
    for body, expected in cases:
        code, out = _post(base, token, "/api/task-inbox", body)
        assert code == expected, (body.get("config"), out)
        assert out.get("accepted") is not True
        assert "error" in out and out["error"]
    assert task_inbox.list_entries(sid) == [], "nothing invalid was stored"

    # An oversize body is refused as oversize, with the limit, and not as "bad JSON".
    huge = json.dumps({"session": sid, "id": "a", "text": "x",
                       "pad": "y" * (web_tasks.MAX_BODY_BYTES + 10)}).encode("utf-8")
    request = urllib.request.Request(base + "/api/task-inbox?token=" + token, data=huge,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            code, out = response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        code, out = exc.code, json.loads(exc.read())
    assert code == 413 and "not" in out["error"] and "truncated" in out["error"]


def test_error_replies_never_echo_the_requests_content(web):
    base, token, _state = web
    secret = "AKIA-not-a-real-key-but-treat-it-as-one"
    code, out = _post(base, token, "/api/task-inbox", {
        "session": "inbox-quiet", "id": "bad id!", "text": secret, "config": CONFIG})
    assert code == 400 and secret not in json.dumps(out)
    code, out = _post(base, token, "/api/task-inbox", {
        "session": "inbox-quiet", "id": "ok", "text": secret, "config": CONFIG,
        "images": ["missingref"]})
    assert code == 409 and secret not in json.dumps(out)


# --------------------------------------------------------------- attachments

def test_attachments_are_snapshotted_at_acceptance_not_referenced(web):
    from harness import input_assets, webapp

    base, token, _state = web
    sid = "inbox-assets"
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    code, uploaded = _post(base, token, "/api/upload", {"media_type": "image/png", "data": png})
    assert code == 200
    code, context = _post(base, token, "/api/ide/context", {
        "items": [{"kind": "selection", "path": "src/app.ts", "startLine": 1, "endLine": 2,
                   "content": "const value = 1;"}]})
    assert code == 200

    code, accepted = _post(base, token, "/api/task-inbox", {
        "session": sid, "id": "with-assets", "text": "what is wrong in this screenshot?",
        "mode": "follow_up", "config": CONFIG, "images": [uploaded["id"]],
        "contexts": [context["id"], {"kind": "file", "path": "README.md", "content": "# hi"}]})
    assert code == 200
    assert accepted["entry"]["assets"] == {"images": 1, "contexts": 2,
                                           "bytes": accepted["entry"]["assets"]["bytes"]}

    # Evict the volatile upload caches entirely: the accepted request must not
    # depend on them any more.
    with webapp.Handler._img_lock:
        webapp.Handler._imgs.clear(); webapp.Handler._img_order.clear()
    with webapp.Handler._ide_context_lock:
        webapp.Handler._ide_contexts.clear(); webapp.Handler._ide_context_order.clear()

    entry = task_inbox.get(sid, "with-assets")
    bundle = input_assets.load(sid, entry["metadata"]["assets"])
    assert bundle["images"] == [{"media_type": "image/png", "data": png}]
    assert [item.get("path") for item in bundle["contexts"]] == ["src/app.ts", "README.md"]
    assert bundle["contexts"][0]["content"] == "const value = 1;"


PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
       "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
OTHER_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4"
             "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _upload(base, token, data=PNG):
    code, out = _post(base, token, "/api/upload", {"media_type": "image/png", "data": data})
    assert code == 200
    return out["id"]


def test_a_retry_after_the_settings_panel_moved_on_is_still_the_same_request(web):
    """The panel is allowed to change; a request already accepted is not.

    An acceptance that re-froze the current provider and model would give the
    identical retry a different payload — so a browser that never saw the first
    answer would be told its request conflicted with itself, and a person would
    either lose the request or end up with two.
    """
    base, token, _state = web
    sid = "inbox-settings-moved"
    body = {"session": sid, "id": "req-1", "text": "write the migration",
            "mode": "follow_up", "config": dict(CONFIG, intent="plan"), "client": "web"}

    code, accepted = _post(base, token, "/api/task-inbox", body)
    assert code == 200
    frozen = accepted["entry"]["config"]["frozen"]
    assert frozen == {"provider": "mock", "model": "model-a",
                      "interactive_speed": "standard", "reasoning_effort": "auto",
                      "runner_settings": {"RUNNER": "collie", "RUNNER_POOL": "collie"}}

    # Somebody opens Settings and picks a different model, and a different payer.
    PANEL.update({"MODEL": "model-b", "PROVIDER": "anthropic-oauth",
                  "REASONING_EFFORT": "high"})

    code, retried = _post(base, token, "/api/task-inbox", body)
    assert code == 200 and retried["accepted"] is True
    assert retried["entry"]["duplicate"] is True
    assert retried["entry"]["config"]["frozen"] == frozen, (
        "the request keeps the settings it was accepted under")
    assert retried["entry"]["digest"] == accepted["entry"]["digest"]
    assert retried["entry"]["seq"] == accepted["entry"]["seq"]

    rows = task_inbox.list_entries(sid)
    assert [(row["id"], row["state"]) for row in rows] == [("req-1", "pending")]
    assert rows[0]["config"]["frozen"] == frozen

    # The same id with a genuinely different request is still a conflict, and
    # still changes nothing.
    code, conflict = _post(base, token, "/api/task-inbox",
                           dict(body, config=dict(CONFIG, intent="build")))
    assert code == 409 and "run configuration" in conflict["error"]
    assert task_inbox.get(sid, "req-1")["config"]["intent"] == "plan"


def test_a_retry_works_after_the_upload_cache_is_gone(web):
    """The acknowledgement was lost, the server restarted — the request stands."""
    from harness import webapp

    base, token, _state = web
    sid = "inbox-retry-assets"
    body = {"session": sid, "id": "req-1", "text": "what is wrong here?",
            "mode": "follow_up", "config": CONFIG,
            "images": [_upload(base, token)],
            "contexts": [{"kind": "file", "path": "a.ts", "content": "const a = 1;"}]}
    code, accepted = _post(base, token, "/api/task-inbox", body)
    assert code == 200 and accepted["entry"]["assets"]["images"] == 1

    # Everything volatile evaporates: the browser's answer never arrived, and by
    # the time it retries, the upload id in its body names nothing at all.
    with webapp.Handler._img_lock:
        webapp.Handler._imgs.clear(); webapp.Handler._img_order.clear()
    with webapp.Handler._ide_context_lock:
        webapp.Handler._ide_contexts.clear(); webapp.Handler._ide_context_order.clear()

    code, retried = _post(base, token, "/api/task-inbox", body)
    assert code == 200 and retried["entry"]["duplicate"] is True
    assert retried["entry"]["digest"] == accepted["entry"]["digest"]
    assert len(task_inbox.list_entries(sid)) == 1

    # A *different* request under that id still cannot slip through on the same
    # fingerprint shortcut.
    code, conflict = _post(base, token, "/api/task-inbox", dict(body, text="something else"))
    assert code == 409 and "different message" in conflict["error"]


def test_the_same_screenshot_re_uploaded_is_a_retry_and_a_different_one_is_a_conflict(web):
    """Attachments are compared by content, not by the id the browser happened to get."""
    base, token, _state = web
    sid = "inbox-reupload"
    body = {"session": sid, "id": "req-1", "text": "what is wrong here?",
            "mode": "follow_up", "config": CONFIG, "images": [_upload(base, token)]}
    code, accepted = _post(base, token, "/api/task-inbox", body)
    assert code == 200
    reference = task_inbox.get(sid, "req-1")["metadata"]["assets"]

    # The retry re-attaches the same bytes, which land under a brand-new id.
    same_again = dict(body, images=[_upload(base, token, PNG)])
    code, retried = _post(base, token, "/api/task-inbox", same_again)
    assert code == 200 and retried["entry"]["duplicate"] is True
    assert task_inbox.get(sid, "req-1")["metadata"]["assets"] == reference
    assert len(task_inbox.list_entries(sid)) == 1

    # A different screenshot under the same id is a different request.
    code, conflict = _post(base, token, "/api/task-inbox",
                           dict(body, images=[_upload(base, token, OTHER_PNG)]))
    assert code == 409 and "attachment" in conflict["error"]
    assert task_inbox.get(sid, "req-1")["metadata"]["assets"] == reference

    # As is dropping the attachment entirely.
    code, conflict = _post(base, token, "/api/task-inbox", dict(body, images=[]))
    assert code == 409 and "attachment" in conflict["error"]


def test_a_retry_of_an_edited_request_is_refused_rather_than_confirmed(web):
    """The fingerprint is a shortcut, never a way to confirm a request that changed."""
    base, token, _state = web
    sid = "inbox-retry-edited"
    body = {"session": sid, "id": "req-1", "text": "the original wording",
            "mode": "follow_up", "config": CONFIG}
    code, accepted = _post(base, token, "/api/task-inbox", body)
    assert code == 200

    code, edited = _post(base, token, "/api/task-inbox/edit", {
        "session": sid, "id": "req-1", "text": "the wording I actually meant"})
    assert code == 200 and edited["entry"]["revision"] == 1

    code, conflict = _post(base, token, "/api/task-inbox", body)
    assert code == 409 and "different message" in conflict["error"]
    assert task_inbox.get(sid, "req-1")["text"] == "the wording I actually meant"


def test_ide_context_upload_refuses_oversize_instead_of_truncating(web):
    from harness import webapp

    base, token, _state = web
    code, saved = _post(base, token, "/api/ide/context", {
        "items": [{"kind": "selection", "path": "a.ts", "content": "x" * 100}]})
    assert code == 200
    # Peeking must not consume: a validation failure elsewhere used to destroy
    # the attachment the request was refused for.
    assert webapp.Handler._ide_context_peek(saved["id"])[0]["content"] == "x" * 100
    assert webapp.Handler._ide_context_peek(saved["id"]) is not None
    assert webapp.Handler._ide_context_take(saved["id"]) is not None
    assert webapp.Handler._ide_context_take(saved["id"]) is None

    code, refused = _post(base, token, "/api/ide/context", {
        "items": [{"path": "too-large.ts", "content": "x" * 70_000}]})
    assert code == 413 and "nothing was truncated" in refused["error"]
    assert "id" not in refused


# ------------------------------------------------------------- steer adapter

def test_steer_adapter_acknowledges_only_durable_acceptance(web):
    base, token, _state = web
    sid = "inbox-steer"
    text = "y" * 6000                       # the old path silently kept 4000 of these

    code, out = _post(base, token, "/api/steer", {"session": sid, "q": text})
    assert code == 200 and out["queued"] is True
    assert out["active"] is False and out["delivery"] == "pending"
    stored = task_inbox.list_entries(sid)
    assert len(stored) == 1
    assert stored[0]["text"] == text, "no truncation between acknowledgement and storage"
    assert stored[0]["mode"] == "steer" and stored[0]["state"] == "pending"

    # A caller-supplied id keeps the retry key contract for this adapter too.
    for _ in range(2):
        code, out = _post(base, token, "/api/steer", {"session": sid, "id": "s-1",
                                                      "q": "and check the logs"})
        assert code == 200 and out["queued"] is True
    assert len(task_inbox.list_entries(sid)) == 2

    code, out = _post(base, token, "/api/steer", {"session": sid, "q": "   "})
    assert code == 400 and out["queued"] is False


# ------------------------------------------------------- deletion and recovery

def _hold_lease_in_another_process(state, session):
    """A second process that really owns the session, until we stop it."""
    code = (
        "import sys, time;"
        "sys.path.insert(0, %r);"
        "import os; os.environ['COLLIE_SESSIONS_DIR'] = %r;"
        "from harness import session_owner;"
        "lease = session_owner.acquire(%r, label='other-process');"
        "print('held', flush=True);"
        "time.sleep(120)"
        % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
           str(state / "sessions"), session))
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"
    return child


def test_delete_refuses_an_owned_session_and_never_silently_drops_pending_work(web):
    from harness import sessions

    base, token, state = web
    sid = "inbox-delete"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _post(base, token, "/api/task-inbox", {"session": sid, "id": "keep-me",
                                           "text": "still waiting", "mode": "follow_up",
                                           "config": CONFIG})

    code, refused = _get(base, token, "/api/delete/" + sid)
    assert code == 409 and refused["ok"] is False and refused["pending"] == 1
    assert sessions.load(sid) is not None
    assert task_inbox.get(sid, "keep-me")["state"] == "pending"

    child = _hold_lease_in_another_process(state, sid)
    try:
        code, busy = _get(base, token, "/api/delete/" + sid + "?discard_pending=1")
        assert code == 409 and "running" in busy["error"]
        assert sessions.load(sid) is not None
    finally:
        child.terminate(); child.wait(timeout=30)

    code, gone = _get(base, token, "/api/delete/" + sid + "?discard_pending=1")
    assert code == 200 and gone["ok"] is True and gone["canceled"] == ["keep-me"]
    assert sessions.load(sid) is None
    # Discarded means cancelled and recorded, not vanished.
    assert task_inbox.get(sid, "keep-me")["state"] == "canceled"
    # The stable lock file is never removed; it is the name executors agree on.
    from harness import session_owner
    assert os.path.exists(session_owner.lock_path(sid))


def test_recovery_reconcile_refuses_while_another_process_owns_the_session(web):
    from harness import sessions

    base, token, state = web
    sid = "inbox-reconcile"
    sessions.checkpoint(sid, [{"role": "user", "content": "go"}], project="web",
                        cwd=str(state), run_id="r1", state="executing_tool",
                        detail={"tool": "bash"})
    child = _hold_lease_in_another_process(state, sid)
    try:
        code, refused = _post(base, token, "/api/recovery/reconcile", {
            "session": sid, "resolution": "cancel", "confirmed": True})
        assert code == 409 and "running" in refused["error"]
        assert sessions.recovery_state(sid)["recovery_required"] is True
    finally:
        child.terminate(); child.wait(timeout=30)

    code, done = _post(base, token, "/api/recovery/reconcile", {
        "session": sid, "resolution": "cancel", "confirmed": True})
    assert code == 200 and done["ok"] is True
