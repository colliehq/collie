"""Durable input meeting an external worker, and saying exactly what it can prove.

A phase-one worker owns its own tool loop.  Its protocol hands Collie no receipt
saying "I received this specific message" — but neither does the native path: an
acknowledgement there proves durable insertion before the provider was called,
not that the model acted on it.  So an external worker can run queued input under
exactly the same definition, and the ordering is what makes it true: the request
is written into the transcript, stamped with its inbox id, under the held lease,
*before* anything can transmit it.

That order is the whole of these tests.  A crash before the write leaves nothing
sent and nothing claimed; a crash after it finds the instruction in the
transcript and never sends it again.  What is never claimed is consumption: the
inbox says "recorded and offered", the surface can say the protocol returned no
receipt, and nothing anywhere infers that the worker acted.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import (runner_registry, runner_slice, session_owner,     # noqa: E402
                     sessions, task_inbox)
from harness.recorder import RunResult                                 # noqa: E402
from harness.router import RunDecision                                 # noqa: E402
from harness.runner_specs import RunnerProbe, RunnerReceipt            # noqa: E402

CONFIG = {"intent": "build", "quality": "balanced", "verification": "auto",
          "workspace": "current", "strategy": "single", "effort": "auto",
          "speed": "standard", "explicit_axes": "none"}


def _decision(**over):
    values = dict(provider="codex-oauth", model="gpt-5.6-sol", effort="high",
                  speed="standard", billing_multiplier=1.0, intent="build",
                  quality="balanced", verification="auto", workspace="current",
                  strategy="single", route_kind="code", complexity="simple")
    values.update(over)
    return RunDecision(**values)


def _probe(key="codex-app-server", **over):
    values = dict(key=key, installed=True, executable_path="C:/bin/codex.exe",
                  version="99.0.0", login="ok", billing_class="subscription_allowance",
                  billing_mode="subscription", probed_at=1_800_000_000.0)
    values.update(over)
    return RunnerProbe(**values)


def _receipt(runner="codex-app-server"):
    return RunnerReceipt.from_dict({
        "runner": runner, "runner_version": "99.0.0",
        "runner_protocol": "codex-appserver-jsonrpc",
        "billing_class": "subscription_allowance", "billing_mode": "subscription",
        "credential_family": "codex", "usage_known": True,
        "usage": {"input_tokens": 20, "output_tokens": 10},
        "settled": True, "recovery_required": False, "mutated": True,
        "native_session": {"runner": runner, "locator": "thread-web",
                           "workspace": "C:/workspace"}})


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """A Web server whose selected worker is an external one."""
    import threading
    from http.server import ThreadingHTTPServer

    from harness import cli, router, settings, webapp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(webapp, "_provider", lambda: "codex-oauth")
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None:
                        default if default is not None else "")
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(webapp.Handler, "_notify_done", staticmethod(lambda *a, **kw: None))
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-app-server": _probe()})
    monkeypatch.setattr(cli, "make_harness", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("an external Web run must not build the native harness")))
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bench = type("Bench", (), {})()
    bench.base = "http://127.0.0.1:%d" % server.server_address[1]
    bench.token = webapp.TOKEN
    bench.state = state
    try:
        yield bench
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        with webapp.Handler._runs_lock:
            webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()


def _post(bench, path, body):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(bench.base + path + "?token=" + bench.token, data=data,
                                     method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _events(handler_events):
    return {kind: data for kind, data in handler_events}


def _handler():
    from harness import webapp
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    return fake, events


def _result(answer="worker answer", receipt=None, **over):
    values = dict(task_id="web", harness="codex-app-server", provider="codex-oauth",
                  model="gpt-5.6-sol", turns=1, wall_ms=5, success=True, verified=False,
                  answer=answer, error="", messages=[], stop_reason="completed")
    values.update(over)
    result = RunResult(**values)
    if receipt is not None:
        setattr(result, runner_slice.RECEIPT_ATTR, receipt)
    return result


def _run_with(monkeypatch, fake_run):
    monkeypatch.setattr(runner_slice, "run_adhoc", fake_run)


def _accept(worker, session, entry_id, text, mode="follow_up", **extra):
    body = {"session": session, "id": entry_id, "text": text, "mode": mode,
            "config": dict(CONFIG, runner="codex-app-server")}
    body.update(extra)
    code, out = _post(worker, "/api/task-inbox", body)
    assert code == 200, out
    return out["entry"]


def _states(session):
    return {row["id"]: row["state"] for row in task_inbox.list_entries(session)}


# --------------------------------------------------------- queued initial input

def test_a_queued_request_is_journalled_before_the_worker_is_launched(
        worker, monkeypatch):
    """Insertion first, transmission second — and the answer saved exactly once."""
    from tests.test_web_execution_manager import _settle

    session = "external-queued"
    sessions.append_exchange(session, "earlier question", "earlier answer",
                             project="web")
    _accept(worker, session, "queued-1", "refactor the parser")
    seen = {}

    def fake_run(decision, task, workspace, **kwargs):
        # What the worker is handed is a fixed string this process chose, and by
        # the time it can be handed over the request is already durable.
        seen["task"] = task
        seen["history_note"] = kwargs.get("history_note")
        seen["journal"] = [m.get("content") for m in sessions.load(session)["messages"]]
        seen["stamped"] = [m.get("inbox_id") for m in sessions.load(session)["messages"]]
        seen["state"] = task_inbox.get(session, "queued-1")["state"]
        receipt = _receipt()
        kwargs["emit"]("receipt", receipt.to_dict())
        return _result(receipt=receipt)
    _run_with(monkeypatch, fake_run)

    code, started = _post(worker, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert seen["task"] == "refactor the parser"
    assert seen["journal"] == ["earlier question", "earlier answer", "refactor the parser"]
    assert seen["stamped"][-1] == "queued-1", "stamped with its inbox id before transport"
    # The prior conversation went with it: a queued turn resumes the thread.
    assert "earlier question" in (seen["history_note"] or "")

    stored = sessions.load(session)["messages"]
    assert [m.get("content") for m in stored] == [
        "earlier question", "earlier answer", "refactor the parser", "worker answer"]
    assert _states(session) == {"queued-1": "consumed"}, "saved once, closed out once"


def test_a_settled_completed_worker_turn_starts_exactly_one_follow_up(
        worker, monkeypatch):
    from tests.test_web_execution_manager import _settle

    session = "external-chain"
    _accept(worker, session, "queued-1", "first queued request")
    _accept(worker, session, "queued-2", "second queued request")
    _accept(worker, session, "queued-3", "third queued request")
    tasks = []

    def fake_run(decision, task, workspace, **kwargs):
        tasks.append(task)
        receipt = _receipt()
        kwargs["emit"]("receipt", receipt.to_dict())
        return _result(receipt=receipt, answer="answer to %s" % task)
    _run_with(monkeypatch, fake_run)

    code, started = _post(worker, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    # One scheduled turn per completed, settled turn: the third is still waiting.
    assert tasks == ["first queued request", "second queued request",
                     "third queued request"]
    assert _states(session) == {"queued-1": "consumed", "queued-2": "consumed",
                                "queued-3": "consumed"}
    assert [m.get("content") for m in sessions.load(session)["messages"]] == [
        "first queued request", "answer to first queued request",
        "second queued request", "answer to second queued request",
        "third queued request", "answer to third queued request"]


@pytest.mark.parametrize("ending, why", [
    ({"error": "the worker exited", "success": False}, "an error"),
    ({"canceled": True}, "a stop"),
    ({"receipt": None}, "no receipt at all"),
    ({"unsettled": True}, "an unsettled receipt"),
])
def test_nothing_is_started_after_a_worker_turn_that_was_not_settled(
        worker, monkeypatch, ending, why):
    from tests.test_web_execution_manager import _settle

    session = "external-no-next-%s" % why.replace(" ", "-")
    _accept(worker, session, "queued-1", "the first request")
    _accept(worker, session, "queued-2", "the follow-up nobody may start")
    tasks = []

    def fake_run(decision, task, workspace, **kwargs):
        tasks.append(task)
        fields = dict(ending)
        receipt = _receipt()
        if fields.pop("unsettled", False):
            receipt = RunnerReceipt.from_dict(
                dict(receipt.to_dict(), settled=False))
        if "receipt" in fields and fields["receipt"] is None:
            fields.pop("receipt")
            receipt = None
        if receipt is not None:
            kwargs["emit"]("receipt", receipt.to_dict())
        return _result(receipt=receipt, **fields)
    _run_with(monkeypatch, fake_run)

    code, started = _post(worker, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert tasks == ["the first request"], "no turn is started on %s" % why
    assert _states(session)["queued-2"] == "pending", "still waiting, still visible"


def test_a_queued_request_with_an_image_is_refused_before_it_is_journalled(worker):
    """An honest modality refusal: nothing sent, nothing written, nothing lost."""
    from tests.test_web_execution_manager import _settle

    session = "external-image"
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    code, uploaded = _post(worker, "/api/upload", {"media_type": "image/png", "data": png})
    assert code == 200
    _accept(worker, session, "queued-1", "what does this show?", images=[uploaded["id"]])

    code, started = _post(worker, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert sessions.load(session) in (None, {}) or not sessions.load(session)["messages"]
    assert _states(session) == {"queued-1": "pending"}


# ------------------------------------------------------------ mid-run steering

def _steer_during(worker, session, entry_id, text):
    """POST steering from inside the worker's own boundary callback."""
    code, out = _post(worker, "/api/steer", {"session": session, "id": entry_id, "q": text})
    assert code == 200 and out["queued"] is True
    return out


def test_steering_is_journalled_before_it_is_offered_to_the_transport(worker, monkeypatch):
    """Written down, then handed over, then closed out — consumption unproven."""
    from harness import webapp

    session = "external-steer"
    handed = []
    order = {}

    def fake_run(decision, task, workspace, **kwargs):
        # Typed while this worker is running, then drained at its own boundary,
        # exactly as the app-server protocol does today.
        _steer_during(worker, session, "mid-1", "also rename the flag")
        order["before_drain"] = [m.get("content") for m in sessions.load(session)["messages"]]
        handed.extend(kwargs["steering"]())
        order["after_drain"] = [m.get("content") for m in sessions.load(session)["messages"]]
        order["state"] = task_inbox.get(session, "mid-1")["state"]
        receipt = _receipt()
        kwargs["emit"]("receipt", receipt.to_dict())
        return _result(receipt=receipt)
    _run_with(monkeypatch, fake_run)

    handler, events = _handler()
    webapp.Handler._serve_stream(handler, {
        "q": ["fix the parser"], "session": [session], "runner": ["codex-app-server"]})

    assert handed == ["also rename the flag"], "the worker really was given the text"
    # The order that makes a crash survivable: durable, acknowledged, then sent.
    # The initial composer request is already durable before the worker starts;
    # the steer is appended later, at its own transport boundary.
    assert order["before_drain"] == ["fix the parser"]
    assert order["after_drain"] == ["fix the parser", "also rename the flag"]
    assert order["state"] == "consumed"

    done = _events(events)["done"]
    assert done["steering"] == {"recorded": ["mid-1"], "unsettled": [], "not_sent": [],
                                "consumption_unconfirmed": ["mid-1"]}

    stored = sessions.load(session)["messages"]
    assert [m["content"] for m in stored] == [
        "fix the parser", "also rename the flag", "worker answer"]
    assert stored[1]["inbox_id"] == "mid-1" and stored[1]["kind"] == "steer"
    assert stored[1]["source"] == "user"

    entry = task_inbox.get(session, "mid-1")
    assert entry["state"] == "consumed"
    assert entry["delivery"]["message_id"] == "mid-1"

    lease = session_owner.acquire(session, label="check")
    try:
        assert task_inbox.claim(session, lease, modes=("steer",)) == []
    finally:
        lease.release()


def test_steering_that_cannot_be_written_down_is_never_handed_to_the_worker(
        worker, monkeypatch):
    """The failure before transmission: refused, still waiting, said out loud."""
    from harness import webapp

    session = "external-steer-unwritable"
    handed = []
    real_checkpoint = sessions.checkpoint

    def fake_run(decision, task, workspace, **kwargs):
        _steer_during(worker, session, "mid-1", "stop touching the database")

        def _refuse(sid, messages, *a, **kw):
            if sid == session and any(m.get("inbox_id") == "mid-1" for m in messages):
                raise OSError(28, "no space left on device")
            return real_checkpoint(sid, messages, *a, **kw)
        monkeypatch.setattr(sessions, "checkpoint", _refuse)
        try:
            handed.extend(kwargs["steering"]())
        finally:
            monkeypatch.setattr(sessions, "checkpoint", real_checkpoint)
        receipt = _receipt()
        kwargs["emit"]("receipt", receipt.to_dict())
        return _result(receipt=receipt)
    _run_with(monkeypatch, fake_run)

    handler, events = _handler()
    webapp.Handler._serve_stream(handler, {
        "q": ["fix the parser"], "session": [session], "runner": ["codex-app-server"]})

    assert handed == [], "an instruction that was not written down is not transmitted"
    events_by_kind = _events(events)
    assert "mid-1" in events_by_kind["steering_error"]["entries"]
    done = events_by_kind["done"]
    assert done["steering"]["not_sent"] == ["mid-1"]
    assert "not sent" in done["error"]

    # Waiting, correctable, and never inserted into the transcript.
    assert _states(session) == {"mid-1": "pending"}
    stored = sessions.load(session)["messages"]
    assert [m["content"] for m in stored] == ["fix the parser", "worker answer"]
    # And no follow-up is started on a turn that refused part of the person's input.
    from harness import web_tasks
    assert web_tasks.scheduled(session) is None


def test_steering_lost_to_a_crash_after_transmission_is_never_sent_again(
        worker, monkeypatch):
    """The failure after transmission: journalled, so the next run does not repeat it."""
    from harness import webapp

    session = "external-steer-crash"

    def fake_run(decision, task, workspace, **kwargs):
        _steer_during(worker, session, "mid-1", "also rename the flag")
        kwargs["steering"]()                     # transmitted...
        raise RuntimeError("the worker process died")   # ...and then the run dies
    _run_with(monkeypatch, fake_run)

    handler, events = _handler()
    webapp.Handler._serve_stream(handler, {
        "q": ["fix the parser"], "session": [session], "runner": ["codex-app-server"]})

    assert "the worker process died" in _events(events)["done"]["error"]
    # It reached the worker, so it stays consumed: re-sending an instruction of
    # unknown effect is the one thing this design refuses to do.
    assert _states(session) == {"mid-1": "consumed"}
    stored = sessions.load(session)["messages"]
    assert [m["content"] for m in stored][:2] == ["fix the parser", "also rename the flag"]

    lease = session_owner.acquire(session, label="check")
    try:
        assert task_inbox.claim(session, lease, modes=("steer",)) == []
    finally:
        lease.release()


def test_steering_accepted_before_this_run_is_not_appended_to_it(worker, monkeypatch):
    """A correction typed for an earlier turn does not overtake the current one."""
    from harness import webapp

    session = "external-steer-floor"
    code, out = _post(worker, "/api/steer", {"session": session, "id": "old-1",
                                             "q": "the instruction I moved on from"})
    assert code == 200 and out["queued"] is True
    handed = []

    def fake_run(decision, task, workspace, **kwargs):
        handed.extend(kwargs["steering"]())
        receipt = _receipt()
        kwargs["emit"]("receipt", receipt.to_dict())
        return _result(receipt=receipt)
    _run_with(monkeypatch, fake_run)

    handler, _events_out = _handler()
    webapp.Handler._serve_stream(handler, {
        "q": ["the request I actually want"], "session": [session],
        "runner": ["codex-app-server"]})

    assert handed == [], "older steering belongs to the turn it was typed for"
    assert _states(session) == {"old-1": "pending"}, "visible, for an explicit Start"
    assert [m["content"] for m in sessions.load(session)["messages"]] == [
        "the request I actually want", "worker answer"]


def test_an_ordinary_external_run_is_unchanged_by_the_inbox(worker):
    from harness import webapp

    session = "external-plain"

    def fake_run(decision, task, workspace, **kwargs):
        receipt = _receipt()
        result = RunResult(task_id="web", harness="codex-app-server",
                           provider="codex-oauth", model="gpt-5.6-sol", turns=1,
                           wall_ms=5, success=True, answer="worker answer", error="",
                           messages=[], stop_reason="completed")
        setattr(result, runner_slice.RECEIPT_ATTR, receipt)
        return result

    import harness.runner_slice as slice_module
    original = slice_module.run_adhoc
    slice_module.run_adhoc = fake_run
    try:
        handler, events = _handler()
        webapp.Handler._serve_stream(handler, {
            "q": ["fix the parser"], "session": [session],
            "runner": ["codex-app-server"]})
    finally:
        slice_module.run_adhoc = original

    done = _events(events)["done"]
    assert done["error"] == "" and "steering" not in done
    assert [m["content"] for m in sessions.load(session)["messages"]] == [
        "fix the parser", "worker answer"]
    assert task_inbox.list_entries(session) == []
