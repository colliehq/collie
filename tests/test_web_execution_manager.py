"""Who owns a Web run: the session lease, not the socket that started it.

The behaviours locked here are the ones a person actually notices when they are
wrong — a second window quietly running the same conversation over the top of
the first, a queued follow-up that never starts (or starts twice), work that
stops because a laptop lid closed, a queued request executed against settings
the person changed afterwards, and a run that begins without the screenshot it
was about.

The harness is a fake, but it implements the real durable-input handshake: it is
handed the held lease and the claimed entry, inserts exactly one stamped message,
checkpoints it, and only then acknowledges the inbox.  Everything else — the
lease, the store, the HTTP server, the threads — is the real thing.
"""
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import (input_assets, session_owner, sessions,           # noqa: E402
                     task_inbox, web_tasks)

CONFIG = {"intent": "build", "quality": "balanced", "verification": "auto",
          "workspace": "current", "strategy": "single", "effort": "auto",
          "speed": "standard", "explicit_axes": "none"}


class _Result:
    """A RunResult shaped like the one the loop returns."""

    def __init__(self, answer="done", **kw):
        self.answer = answer
        self.error = ""
        self.canceled = False
        self.success = True
        self.model = "model-a"
        self.prefix_tokens = self.input_tokens = self.output_tokens = 0
        self.total_tokens = 0
        self.turns = 1
        self.tool_calls = 0
        self.wall_ms = 5
        self.cost_usd = 0.0
        self.verified = False
        self.messages = []
        self.turns_exhausted = False
        self.budget_exhausted = False
        self.edited = False
        self.model_calls = 1
        self.parent_run_id = None
        self.stop_reason = ""
        self.__dict__.update(kw)


class _Lab:
    """The controllable half of a run: what the harness does and what it returns."""

    def __init__(self):
        self.calls = []
        self.results = []
        self.during_run = None
        self.run_opts = {}
        self.gate = threading.Event()
        self.gate.set()

    def result_for(self, record):
        if self.results:
            value = self.results.pop(0)
            return value(record) if callable(value) else value
        return _Result(answer="answer for %r" % (record["message"],)[:40])


class _BaseHarness:
    """Everything a run does, minus the durable-input handshake itself."""

    def __init__(self, lab, **kw):
        self.lab = lab
        self.composer = SimpleNamespace(identity="")
        self.memory = self.recorder = SimpleNamespace(
            close=lambda: None, finish_run=lambda res: None)
        self.max_turns = 20
        self.steering = None
        self.__dict__.update(kw)

    def settle_run_memory(self, *a, **kw):
        return None

    def run(self, task_id, message, history=None, authority_msg=None, **kwargs):
        entry = getattr(self, "input_entry", None)
        owner = getattr(self, "run_owner", None)
        record = {"message": message, "authority": authority_msg,
                  "history": list(history or []), "run_opts": dict(self.lab.run_opts),
                  "entry": entry["id"] if entry else None,
                  "owner": owner.owner_id if owner else None,
                  "owner_held": bool(owner is not None and owner.held),
                  "steering": self.steering, "model": getattr(self, "model", None),
                  "effort": getattr(self, "effort", None),
                  "steering_after_seq": getattr(self, "steering_after_seq", None)}
        self.lab.calls.append(record)
        messages = list(history or [])
        if entry is not None:
            # The native contract: one stamped insertion of THIS request, made
            # durable, and only then acknowledged.  A crash between the two is
            # what `reconcile` repairs; the other order is unrepairable.
            assert owner is not None and owner.held
            session = owner.session
            messages.append({"role": "user", "content": message, "source": "user",
                             "kind": entry["mode"], "inbox_id": entry["id"]})
            sessions.checkpoint(session, messages, project="web", cwd=os.getcwd(),
                                run_id="fake", state="turn_boundary")
            task_inbox.ack(session, owner, entry["id"])
        else:
            messages.append({"role": "user", "content": message})
        self.lab.gate.wait(30)
        if self.lab.during_run is not None:
            self.lab.during_run(self, record)
        if owner is not None and getattr(self, "run_owner", None) is not None:
            # The loop's own steering boundary: claim what was accepted *for this
            # turn* — the surface supplied the floor — insert it stamped, make it
            # durable, then acknowledge.  Same order as the request itself.
            claimed = web_tasks.claim_steer(
                owner.session, owner, after_seq=getattr(self, "steering_after_seq", 0))
            record["claimed_steer"] = [row["id"] for row in claimed]
            for row in claimed:
                messages.append(task_inbox.journal_message(row))
            if claimed:
                sessions.checkpoint(owner.session, messages, project="web",
                                    cwd=os.getcwd(), run_id="fake", state="turn_boundary")
                for row in claimed:
                    task_inbox.ack(owner.session, owner, row["id"])
        result = self.lab.result_for(record)
        messages.append({"role": "assistant", "content": result.answer})
        result.messages = messages
        return result


class _Harness(_BaseHarness):
    """A harness that implements the contract's durable-input handshake.

    The three names are class attributes on purpose: the surface probes for them
    BEFORE assigning, because setting an attribute on any Python object always
    "succeeds" and would prove nothing about who reads it.  ``steering_after_seq``
    is part of the handshake for the same reason the other two are — a loop that
    claims steering without the run's chronological floor can append a stale
    instruction from a canceled turn on top of a newer one.
    """

    run_owner = None
    input_entry = None
    steering_after_seq = 0


class _LegacyHarness(_BaseHarness):
    """A harness from before durable input: none of those attributes exist."""


@pytest.fixture
def lab(monkeypatch, tmp_path):
    """A real HTTP server, a real store, a fake but contract-faithful harness."""
    from harness import cli, settings, webapp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.chdir(tmp_path)
    values = {"MODEL": "model-a", "REASONING_EFFORT": "auto", "INTERACTIVE_SPEED": "standard"}
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: values.get(
                            key, default if default is not None else ""))
    monkeypatch.setattr(webapp.Handler, "_notify_done", staticmethod(lambda *a, **kw: None))

    bench = _Lab()
    bench.settings = values
    bench.state = state
    bench.harness_class = _Harness
    bench.made = []

    def _make(*args, **kwargs):
        harness = bench.harness_class(bench, model=kwargs.get("model"),
                                      effort=kwargs.get("effort"))
        bench.made.append(harness)
        return harness

    monkeypatch.setattr(cli, "make_harness", _make)
    monkeypatch.setattr(cli, "configure_run_options",
                        lambda h, **opts: bench.run_opts.update(opts))
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bench.port = server.server_address[1]
    bench.base = "http://127.0.0.1:%d" % bench.port
    bench.token = webapp.TOKEN
    try:
        yield bench
    finally:
        bench.gate.set()
        _settle(20)
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        with webapp.Handler._runs_lock:
            webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()


# ------------------------------------------------------------------- helpers

def _settle(timeout=15):
    """Wait for every detached follow-up run this process scheduled."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [t for t in threading.enumerate()
                 if t.name.startswith("collie-web-input-") and t.is_alive()]
        if not alive:
            return
        for thread in alive:
            thread.join(timeout=0.25)
    raise AssertionError("a scheduled run never finished")


def _wait_for(predicate, timeout=20, what="a condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for " + what)


def _stream(bench, **params):
    url = bench.base + "/api/stream?token=" + bench.token + "&" + urllib.parse.urlencode(params)
    events, kind = [], None
    with urllib.request.urlopen(url, timeout=60) as response:
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                events.append((kind, json.loads(line[6:])))
    return events


def _post(bench, path, body):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(bench.base + path + "?token=" + bench.token, data=data,
                                     method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(bench, path):
    joiner = "&" if "?" in path else "?"
    with urllib.request.urlopen(bench.base + path + joiner + "token=" + bench.token,
                                timeout=20) as response:
        return response.status, json.loads(response.read())


def _accept(bench, session, entry_id, text, mode="follow_up", **extra):
    body = {"session": session, "id": entry_id, "text": text, "mode": mode,
            "config": dict(CONFIG)}
    body.update(extra)
    code, out = _post(bench, "/api/task-inbox", body)
    assert code == 200, out
    return out["entry"]


def _states(session):
    return {row["id"]: row["state"] for row in task_inbox.list_entries(session)}


def _hold_lease_in_another_process(state, session):
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


# ------------------------------------------------------------------ ownership

def test_the_lease_is_taken_before_the_journal_is_read_and_held_past_the_final_save(
        lab, monkeypatch):
    """Reading history under no lease is reading a store someone else may be writing."""
    seen = {}
    real_load, real_save = sessions.load, sessions.save

    def _load(sid, *a, **kw):
        # A plain threading guard: unavailable means this process already owns it.
        seen.setdefault("owned_at_load", session_owner.try_acquire(sid) is None)
        return real_load(sid, *a, **kw)

    def _save(sid, *a, **kw):
        seen["owned_at_save"] = session_owner.try_acquire(sid) is None
        return real_save(sid, *a, **kw)

    monkeypatch.setattr(sessions, "load", _load)
    monkeypatch.setattr(sessions, "save", _save)

    sessions.append_exchange("owned", "earlier", "answer", project="web", cwd=str(lab.state))
    events = _stream(lab, q="carry on", session="owned")

    assert events[-1][0] == "done" and not events[-1][1]["error"]
    assert seen == {"owned_at_load": True, "owned_at_save": True}
    assert lab.calls[0]["owner_held"] is True, "the run itself sees a live lease"
    # And released afterwards, so the next turn is not locked out.
    released = session_owner.try_acquire("owned")
    assert released is not None
    released.release()


def test_a_second_process_owning_the_session_blocks_the_run_before_it_starts(lab):
    child = _hold_lease_in_another_process(lab.state, "contended")
    try:
        events = _stream(lab, q="do it twice", session="contended")
        assert events[-1][0] == "done"
        assert events[-1][1]["error"] == web_tasks.BUSY_ERROR
        assert events[-1][1]["busy"] is True
        assert lab.calls == [], "no model work started for a session we do not own"
        assert not any(kind == "start" for kind, _ in events)
    finally:
        child.terminate(); child.wait(timeout=30)

    events = _stream(lab, q="now it is free", session="contended")
    assert events[-1][1]["error"] in ("", None) and len(lab.calls) == 1


def test_a_disconnected_client_owns_neither_the_run_nor_the_next_turn(lab):
    """Closing the tab is not a stop button, and never was meant to be."""
    session = "detached"
    _accept(lab, session, "next-1", "then write the changelog")
    lab.gate.clear()

    connection = http.client.HTTPConnection("127.0.0.1", lab.port, timeout=20)
    connection.request("GET", "/api/stream?token=%s&session=%s&q=%s"
                       % (lab.token, session, urllib.parse.quote("start the work")))
    response = connection.getresponse()
    assert response.status == 200
    response.fp.readline()                     # the run has begun streaming
    connection.close()                         # ...and the person walks away
    time.sleep(0.2)
    lab.gate.set()
    _wait_for(lambda: len(lab.calls) == 2, what="the queued follow-up to run")
    _settle()

    # The first run finished and saved, and the queued follow-up ran on its own
    # sink rather than being written to a dead socket.
    assert [call["message"] for call in lab.calls] == ["start the work",
                                                       "then write the changelog"]
    assert _states(session) == {"next-1": "consumed"}
    stored = sessions.load(session)["messages"]
    assert [m.get("content") for m in stored if m.get("role") == "user"] == [
        "start the work", "then write the changelog"]
    assert stored[-2]["inbox_id"] == "next-1"


# ------------------------------------------------------------------ scheduling

def test_a_normal_completion_starts_exactly_one_follow_up_and_leaves_a_steer_waiting(lab):
    session = "auto-next"
    steer = _accept(lab, session, "stray-steer", "while you are in there, check the logs",
                    mode="steer")
    _accept(lab, session, "follow-1", "then update the README")
    _accept(lab, session, "follow-2", "and bump the version")

    events = _stream(lab, q="first request", session=session)
    assert events[-1][1]["error"] in ("", None)
    _settle()

    # Exactly one scheduled turn per completed run, in acceptance order, and a
    # steer nobody delivered stays visible instead of being run as a new turn.
    assert [call["message"] for call in lab.calls] == [
        "first request", "then update the README", "and bump the version"]
    assert _states(session) == {"stray-steer": "pending", "follow-1": "consumed",
                                "follow-2": "consumed"}
    assert task_inbox.get(session, "stray-steer")["text"] == steer["text"]

    stored = sessions.load(session)["messages"]
    delivered = [m for m in stored if m.get("inbox_id")]
    assert [m["inbox_id"] for m in delivered] == ["follow-1", "follow-2"]
    assert all(m["source"] == "user" and m["kind"] == "follow_up" for m in delivered)
    # Each scheduled turn is a real turn: it sees everything before it.
    assert len(lab.calls[2]["history"]) > len(lab.calls[1]["history"])


@pytest.mark.parametrize("ending,expected", [
    ({"canceled": True}, "canceled"),
    ({"error": "the provider refused"}, "error"),
    ({"turns_exhausted": True}, "turn_limit"),
    ({"budget_exhausted": True}, "budget_limit"),
])
def test_no_turn_is_scheduled_after_a_stop_an_error_or_a_cap(lab, ending, expected):
    session = "no-auto-" + expected
    _accept(lab, session, "queued-1", "the next thing")
    lab.results.append(_Result(answer="stopped", **ending))

    events = _stream(lab, q="first request", session=session)
    _settle()

    done = events[-1][1]
    assert done.get("stop_reason", expected) == expected
    assert [call["message"] for call in lab.calls] == ["first request"]
    assert _states(session) == {"queued-1": "pending"}, (
        "accepted work waits for an explicit Start after an abnormal ending")


def test_a_recovery_fence_left_by_the_run_blocks_scheduling(lab):
    session = "fenced"
    _accept(lab, session, "queued-1", "the next thing")

    def _leave_a_fence(harness, record):
        sessions.checkpoint(session, record["history"], project="web", cwd=os.getcwd(),
                            run_id="fake", state="executing_tool",
                            detail={"tool_name": "bash"})
    lab.during_run = _leave_a_fence

    events = _stream(lab, q="first request", session=session)
    _settle()

    assert events[-1][1]["recovery_required"] is True
    assert [call["message"] for call in lab.calls] == ["first request"]
    assert _states(session) == {"queued-1": "pending"}


def test_explicit_start_runs_the_earliest_pending_including_an_undelivered_steer(lab):
    session = "explicit-start"
    code, idle = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and idle["started"] is False and idle["reason"] == "nothing_pending"

    _accept(lab, session, "steer-1", "look at the logs first", mode="steer")
    _accept(lab, session, "follow-1", "then write it up")

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    assert started["entry"]["id"] == "steer-1"
    _settle()

    # The earliest waiting request ran, as its own initial turn, with its kind intact.
    assert [call["message"] for call in lab.calls] == ["look at the logs first",
                                                       "then write it up"]
    assert lab.calls[0]["authority"] == "look at the logs first"
    stored = sessions.load(session)["messages"]
    stamped = [(m["inbox_id"], m["kind"]) for m in stored if m.get("inbox_id")]
    assert stamped == [("steer-1", "steer"), ("follow-1", "follow_up")]
    assert _states(session) == {"steer-1": "consumed", "follow-1": "consumed"}


def test_start_refuses_a_busy_or_recovery_fenced_session(lab):
    session = "start-refusals"
    _accept(lab, session, "queued-1", "run me later")

    child = _hold_lease_in_another_process(lab.state, session)
    try:
        code, busy = _post(lab, "/api/task-inbox/start", {"session": session})
        assert code == 409 and "running" in busy["error"]
    finally:
        child.terminate(); child.wait(timeout=30)
    assert _states(session) == {"queued-1": "pending"}

    sessions.checkpoint(session, [{"role": "user", "content": "earlier"}], project="web",
                        cwd=str(lab.state), run_id="r1", state="executing_tool",
                        detail={"tool_name": "bash"})
    code, fenced = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 409 and "inspect the outside world" in fenced["error"]
    assert _states(session) == {"queued-1": "pending"}
    assert lab.calls == []


def test_a_request_accepted_during_a_run_is_started_when_that_run_completes(lab):
    """Accepted near a finishing run: kept, not lost with the volatile channel."""
    session = "accept-during-run"
    lab.gate.clear()
    events = []
    worker = threading.Thread(
        target=lambda: events.extend(_stream(lab, q="first request", session=session)))
    worker.start()
    try:
        deadline = time.time() + 10
        while not lab.calls and time.time() < deadline:
            time.sleep(0.02)
        assert lab.calls, "the run did not start"
        code, out = _post(lab, "/api/steer", {"session": session, "id": "late-1",
                                              "q": "also update the docs",
                                              "mode": "follow_up"})
        assert code == 200 and out["queued"] is True and out["active"] is True
    finally:
        lab.gate.set()
        worker.join(timeout=30)
    _settle()

    assert [call["message"] for call in lab.calls] == ["first request", "also update the docs"]
    assert _states(session) == {"late-1": "consumed"}


def test_a_request_accepted_by_another_process_is_visible_when_the_run_ends(lab):
    """Scheduling reads the store, so it sees work this server never handled."""
    session = "cross-process"
    lab.gate.clear()
    events = []
    worker = threading.Thread(
        target=lambda: events.extend(_stream(lab, q="first request", session=session)))
    worker.start()
    try:
        _wait_for(lambda: bool(lab.calls), what="the run to start")
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import os; os.environ['COLLIE_SESSIONS_DIR'] = %r;"
            "from harness import task_inbox;"
            "task_inbox.enqueue(%r, 'from-elsewhere', 'finish the docs',"
            " mode='follow_up', client='cli')"
            % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               str(lab.state / "sessions"), session))
        assert subprocess.run([sys.executable, "-c", code], timeout=120).returncode == 0
    finally:
        lab.gate.set()
        worker.join(timeout=30)
    _settle()

    assert [call["message"] for call in lab.calls] == ["first request", "finish the docs"]
    assert _states(session) == {"from-elsewhere": "consumed"}


def test_a_durable_harness_is_not_also_given_the_old_volatile_steer_callback(lab):
    """Two channels into one turn is how one instruction gets inserted twice."""
    session = "one-channel"
    _stream(lab, q="first request", session=session)
    assert lab.calls[0]["steering"] is None, (
        "the loop claims accepted input itself; a second callback would double-insert it")

    lab.harness_class = _LegacyHarness
    _stream(lab, q="second request", session=session)
    assert callable(lab.calls[1]["steering"]), (
        "a harness without durable input keeps the in-memory channel it has always had")


# -------------------------------------------------------------- steering floor

def test_steering_typed_during_a_run_is_delivered_and_older_steering_is_not(lab):
    """"During this run" is decided when the run starts, not when it asks.

    A steer left pending by a turn that was canceled is older than the request
    the person is watching now.  Appending it afterwards would answer them with
    the instruction they had already moved on from — so it stays where they can
    see it, and an explicit Start is what runs it.
    """
    session = "steer-floor"
    stale = _accept(lab, session, "stale-1", "the plan I abandoned", mode="steer")

    lab.gate.clear()
    events = []
    worker = threading.Thread(
        target=lambda: events.extend(_stream(lab, q="what I actually want", session=session)))
    worker.start()
    try:
        _wait_for(lambda: bool(lab.calls), what="the run to start")
        code, out = _post(lab, "/api/steer", {"session": session, "id": "live-1",
                                              "q": "and use tabs, not spaces"})
        assert code == 200 and out["queued"] is True
    finally:
        lab.gate.set()
        worker.join(timeout=30)
    _settle()

    assert lab.calls[0]["steering_after_seq"] == stale["seq"], (
        "the floor is this turn's own starting point in the inbox")
    assert lab.calls[0]["claimed_steer"] == ["live-1"]
    stored = sessions.load(session)["messages"]
    assert [m.get("content") for m in stored] == [
        "what I actually want", "and use tabs, not spaces",
        "answer for 'what I actually want'"]
    assert _states(session) == {"stale-1": "pending", "live-1": "consumed"}

    # And the older one is still exactly what was typed, ready to be started.
    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["entry"]["id"] == "stale-1"
    _settle()
    assert [call["message"] for call in lab.calls][-1] == "the plan I abandoned"
    assert _states(session)["stale-1"] == "consumed"


def test_a_queued_turn_takes_its_own_request_as_the_floor(lab):
    """Steering accepted before a queued request belongs to an earlier turn."""
    session = "queued-floor"
    earlier = _accept(lab, session, "steer-0", "an older aside", mode="steer")
    queued = _accept(lab, session, "queued-1", "the request that was started")

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["entry"]["id"] == "steer-0", (
        "Start takes the earliest waiting request, whatever its kind")
    _settle()

    # That turn ran the older steer as its own initial request; the follow-up it
    # scheduled then used its own sequence as the floor.
    assert [call["message"] for call in lab.calls] == [
        "an older aside", "the request that was started"]
    assert lab.calls[1]["steering_after_seq"] == queued["seq"] > earlier["seq"]
    assert _states(session) == {"steer-0": "consumed", "queued-1": "consumed"}


def test_a_queued_request_run_under_pack_is_written_down_once(lab, monkeypatch):
    """Pack's candidates are not Collie's loop, so the request is journalled here.

    Without that, a strategy that never stamps the inbox id would finish, leave
    the request looking undelivered, and offer to run the whole thing again.
    """
    from harness import pack as pack_module

    session = "queued-pack"
    _accept(lab, session, "queued-1", "make the flaky test pass",
            config=dict(CONFIG, strategy="pack", check="pytest -q", n=2,
                        explicit_axes="strategy"))
    seen = {}

    def _fake_pack(task, cwd, **kwargs):
        seen["task"] = task
        seen["journal"] = [m.get("content") for m in sessions.load(session)["messages"]]
        seen["stamped"] = [m.get("inbox_id") for m in sessions.load(session)["messages"]]
        return {"winner": 0, "answer": "the winning patch", "n": 2, "attempts": [
            {"idx": 0, "verified": True, "turns": 1, "check_pass": True}],
            "reason": "check passed", "applied": False, "total_cost_usd": 0.0}
    monkeypatch.setattr(pack_module, "run_pack", _fake_pack)

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()
    assert seen["task"] == "make the flaky test pass"
    assert seen["stamped"] == ["queued-1"], "stamped before any candidate ran"
    assert [m.get("content") for m in sessions.load(session)["messages"]] == [
        "make the flaky test pass", "the winning patch"]
    assert _states(session) == {"queued-1": "consumed"}


# ------------------------------------------------- failures that must be visible

def test_nothing_is_started_when_the_transcript_cannot_be_read(lab, monkeypatch):
    """Unknown delivery state is not "nothing was delivered"."""
    session = "unreadable-journal"
    _accept(lab, session, "queued-1", "the next thing")

    # The journal cannot be read when this run tries to settle, so "was the
    # request delivered?" has no answer this turn.
    monkeypatch.setattr(web_tasks, "journal_ids", lambda sid: None)
    _stream(lab, q="first request", session=session)
    _settle()

    assert [call["message"] for call in lab.calls] == ["first request"], (
        "no work is started on top of a delivery state nobody can read")
    assert _states(session) == {"queued-1": "pending"}

    code, listing = _get(lab, "/api/task-inbox?session=" + session)
    assert code == 200
    assert "could not be read" in listing["queue_error"]["error"]
    assert listing["queue_error"]["kind"] == "settlement"


def test_a_scheduled_turn_that_fails_outright_says_so_and_keeps_the_request(
        lab, monkeypatch):
    """A detached run has no socket to fail on, so silence is the real danger."""
    session = "detached-failure"
    _accept(lab, session, "queued-1", "the thing that never ran")

    def _explode(*args, **kwargs):
        raise RuntimeError("the run could not be set up")
    monkeypatch.setattr(web_tasks, "serve_managed_stream", _explode)

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert lab.calls == []
    assert _states(session) == {"queued-1": "pending"}, "still waiting, still correctable"

    code, listing = _get(lab, "/api/task-inbox?session=" + session)
    assert code == 200
    assert listing["queue_error"]["kind"] == "scheduled_run"
    assert "the run could not be set up" in listing["queue_error"]["error"]
    assert listing["queue_error"]["entry"] == "queued-1"
    # The lease did not leak with the failure.
    assert listing["owner_busy"] is False
    lease = session_owner.try_acquire(session, label="after")
    assert lease is not None
    lease.release()


def test_busy_is_answered_by_the_lock_not_by_the_record_it_left_behind(lab):
    """A live owner in another process, and the stale note a dead one leaves."""
    session = "busy-truth"
    _accept(lab, session, "queued-1", "run me when it is free")

    child = _hold_lease_in_another_process(lab.state, session)
    try:
        code, listing = _get(lab, "/api/task-inbox?session=" + session)
        assert code == 200
        assert listing["active"] is False, "this server is not running it"
        assert listing["owner_busy"] is True, "but somebody is"
        assert listing["owner"]["label"] == "other-process"
    finally:
        child.terminate(); child.wait(timeout=30)

    # The record it left behind still names it, with no clean release — and that
    # is exactly the state a crash leaves.  It is not evidence of a live run.
    _wait_for(lambda: _get(lab, "/api/task-inbox?session=" + session)[1]["owner_busy"]
              is False, what="the lock to be released with the process")
    code, listing = _get(lab, "/api/task-inbox?session=" + session)
    assert listing["owner"] and not listing["owner"].get("released")
    assert listing["owner_busy"] is False
    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()
    assert _states(session) == {"queued-1": "consumed"}


def test_pending_work_waits_for_an_explicit_start_and_is_never_replayed(lab):
    """A server that comes up with queued work starts nothing on its own."""
    session = "no-speculative-replay"
    _accept(lab, session, "queued-1", "do this eventually")
    # A fresh reader (the state a restarted process is in) sees it waiting...
    assert _states(session) == {"queued-1": "pending"}
    time.sleep(0.3)
    assert lab.calls == [], "nothing runs until somebody asks"

    code, listing = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and listing["started"] is True
    _settle()
    assert [call["message"] for call in lab.calls] == ["do this eventually"]


# ---------------------------------------------------------------- frozen config

def test_a_queued_request_runs_under_the_settings_it_was_accepted_with(lab, monkeypatch):
    from harness import router

    session = "frozen"
    _accept(lab, session, "queued-1", "write the migration",
            config=dict(CONFIG, intent="plan", quality="thorough", effort="high",
                        explicit_axes="intent,quality,effort"))

    captured = {}
    real = router.resolve_run_decision

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(router, "resolve_run_decision", _spy)

    # The panel moves on while the request waits.
    lab.settings["MODEL"] = "model-b"
    lab.settings["REASONING_EFFORT"] = "low"

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert captured["model"] == "model-a", "the frozen model, not the one set later"
    assert captured["effort"] == "high"
    assert lab.run_opts == {"intent": "plan", "quality": "thorough", "verification": "auto"}
    assert _states(session) == {"queued-1": "consumed"}
    # Freezing is local to the run: the process-wide settings were not rewritten.
    assert lab.settings["MODEL"] == "model-b"


@pytest.mark.parametrize("key,value", [("RUNNER", "claude-code"), ("RUNNER_POOL", "claude-code")])
def test_changed_worker_settings_leave_the_accepted_request_visible(lab,key,value):
    session="worker-changed"
    _accept(lab,session,"queued-1","keep working")
    lab.settings[key]=value
    code,started=_post(lab,"/api/task-inbox/start",{"session":session})
    assert code==200 and started["started"]
    _settle()
    assert not lab.calls
    assert _states(session)=={"queued-1":"pending"}
    assert "worker settings changed" in web_tasks.queue_error(session)["error"]


def test_frozen_auto_model_is_not_replaced_by_a_later_explicit_model(lab,monkeypatch):
    from harness import router
    session="frozen-auto"
    lab.settings["MODEL"]=""
    _accept(lab,session,"queued-1","keep working")
    lab.settings["MODEL"]="model-b"
    seen={}; original=router.resolve_run_decision
    def resolve(*a,**kw):
        seen.update(kw)
        return original(*a,**kw)
    monkeypatch.setattr(router,"resolve_run_decision",resolve)
    code,started=_post(lab,"/api/task-inbox/start",{"session":session})
    assert code==200 and started["started"]
    _settle()
    assert seen["model"] is None
    assert _states(session)=={"queued-1":"consumed"}


def test_a_changed_provider_refuses_the_run_instead_of_charging_another_account(
        lab, monkeypatch):
    from harness import webapp

    session = "payer"
    _accept(lab, session, "queued-1", "keep working")
    monkeypatch.setattr(webapp, "_provider", lambda: "anthropic-oauth")

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert lab.calls == [], "no model call on a provider the person did not choose"
    assert _states(session) == {"queued-1": "pending"}, "the request is still waiting"


def test_a_harness_without_the_durable_handshake_refuses_queued_work(lab):
    session = "legacy-harness"
    lab.harness_class = _LegacyHarness
    _accept(lab, session, "queued-1", "run me")

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert lab.calls == []
    assert _states(session) == {"queued-1": "pending"}


# ----------------------------------------------------------------- attachments

def test_missing_or_corrupt_attachments_refuse_before_any_provider_work(lab):
    session = "assets"
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    code, uploaded = _post(lab, "/api/upload", {"media_type": "image/png", "data": png})
    assert code == 200
    entry = _accept(lab, session, "queued-1", "what does this show?", images=[uploaded["id"]])
    assert entry["assets"]["images"] == 1

    reference = task_inbox.get(session, "queued-1")["metadata"]["assets"]
    path = input_assets._path(session, reference["digest"])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"version": 1, "images": [], "contexts": []}')   # torn on disk

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert lab.calls == [], "nothing reached a provider without its attachment"
    assert _states(session) == {"queued-1": "pending"}, "left visible for correction"

    os.remove(path)
    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    _settle()
    assert lab.calls == [] and _states(session) == {"queued-1": "pending"}


def test_a_stream_referencing_an_evicted_upload_refuses_instead_of_running_without_it(lab):
    from harness import webapp

    code, uploaded = _post(lab, "/api/upload", {"media_type": "image/png", "data": "AAAA"})
    assert code == 200
    with webapp.Handler._img_lock:
        webapp.Handler._imgs.clear(); webapp.Handler._img_order.clear()

    events = _stream(lab, q="what is in this?", session="evicted", imgs=uploaded["id"])
    assert events[-1][0] == "done"
    assert "no longer available" in events[-1][1]["error"]
    assert lab.calls == []


def test_a_queued_request_delivers_its_snapshotted_attachments(lab):
    session = "queued-assets"
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    code, uploaded = _post(lab, "/api/upload", {"media_type": "image/png", "data": png})
    _accept(lab, session, "queued-1", "read this file and this shot",
            images=[uploaded["id"]],
            contexts=[{"kind": "file", "path": "src/app.ts", "content": "const a = 1;"}])

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200
    _settle()

    message = lab.calls[0]["message"]
    assert isinstance(message, list)
    assert message[-1] == {"type": "image", "media_type": "image/png", "data": png}
    assert "const a = 1;" in message[0]["text"]
    # Authority is compiled from the person's words only, never the file contents.
    assert lab.calls[0]["authority"] == "read this file and this shot"


# ------------------------------------------------------------------ concurrency

def test_a_finished_runs_save_is_never_overwritten_by_a_racing_second_run(lab, monkeypatch):
    session = "no-lost-update"
    real_save = sessions.save
    saving = threading.Event()
    release = threading.Event()

    def _slow_save(sid, messages, *a, **kw):
        if sid == session and not saving.is_set():
            saving.set()
            release.wait(10)
        return real_save(sid, messages, *a, **kw)
    monkeypatch.setattr(sessions, "save", _slow_save)

    first = []
    worker = threading.Thread(
        target=lambda: first.extend(_stream(lab, q="first request", session=session)))
    worker.start()
    try:
        assert saving.wait(10), "the first run never reached its save"
        # A second window tries to run the same conversation while the first is
        # still writing its transcript.
        second = _stream(lab, q="second request", session=session)
        assert second[-1][1]["error"] == web_tasks.BUSY_ERROR
    finally:
        release.set()
        worker.join(timeout=30)

    assert first[-1][1]["error"] in ("", None)
    stored = sessions.load(session)["messages"]
    assert [m["content"] for m in stored] == ["first request", "answer for 'first request'"]

    # Once the first run has really finished, the next one continues from it.
    _stream(lab, q="second request", session=session)
    stored = sessions.load(session)["messages"]
    assert [m["content"] for m in stored][:3] == [
        "first request", "answer for 'first request'", "second request"]
