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
import queue
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


def _stream(bench, on_event=None, **params):
    url = bench.base + "/api/stream?token=" + bench.token + "&" + urllib.parse.urlencode(params)
    events, kind = [], None
    with urllib.request.urlopen(url, timeout=60) as response:
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                events.append((kind, json.loads(line[6:])))
                if on_event is not None:
                    on_event(*events[-1])     # on the reader's thread, as it arrives
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


# ----------------------------------------- a terminal frame vs. admission state
def _stream_probing_on_done(lab, monkeypatch, on_done=None, **params):
    """Read one managed stream and, the instant ``done`` lands, ask what a caller
    asks next: can this conversation start another turn?  ``try_acquire`` is what
    ``serve_managed_stream`` admits with, taken in the same breath as the frame.
    The settlement hook is a barrier, not a timing guess: it holds open the work
    that runs between ``_run_stream`` and the release."""
    session = params["session"]
    probed, probe, barrier_used = threading.Event(), {}, []
    real_settle = web_tasks.settle_run_claims

    def _settle_claims(sid, owner, entry_ids, **kw):
        if sid == session and not barrier_used:
            barrier_used.append(True)
            probed.wait(0.75)
        return real_settle(sid, owner, entry_ids, **kw)

    def _on_event(kind, data):
        if kind != "done" or probe:
            return
        probe["owner"] = session_owner.describe(session)["owner"]   # sidecar first
        lease = session_owner.try_acquire(session, label="next-turn")
        probe["admitted"] = lease is not None
        if lease is not None:
            lease.release()
        if on_done is not None:
            probe["next"] = on_done()
        probed.set()

    monkeypatch.setattr(web_tasks, "settle_run_claims", _settle_claims)
    try:
        events = _stream(lab, on_event=_on_event, **params)
    finally:
        probed.set()
    assert probe, "the stream ended without a done frame"
    return events, probe


@pytest.mark.parametrize("ending", ["completed", "error", "canceled", "crashed"])
def test_done_means_the_next_turn_can_be_admitted(lab, monkeypatch, ending):
    """A terminal frame is a statement about the session, not about one socket: a
    caller doing the one thing it invites was refused for work already finished,
    and every ending owes the same promise."""
    session = "admit-after-" + ending
    def _explode(record):
        raise RuntimeError("the worker died mid-turn")
    outcome = {"error": _Result(answer="", error="the provider refused"),
               "canceled": _Result(answer="partial", canceled=True),
               "crashed": _explode}.get(ending)
    if outcome is not None:
        lab.results.append(outcome)
    # On a clean ending, do the whole thing: send again on the frame itself.
    after = (lambda: _stream(lab, q="second request", session=session)) \
        if ending == "completed" else None
    events, probe = _stream_probing_on_done(lab, monkeypatch, q="first request",
                                            session=session, on_done=after)
    assert [kind for kind, _ in events].count("done") == 1
    assert events[-1][0] == "done"
    if ending == "completed":
        assert events[-1][1]["error"] in ("", None)
    else:
        assert events[-1][1].get("error") or events[-1][1].get("canceled")
    # The lease was let go before the frame that invites the next request, and
    # says so in its own sidecar — as the field refusal did, microseconds late.
    assert probe["owner"].get("released"), "done was announced under the session lease"
    assert probe["admitted"] is True, "the next turn was refused for finished work"
    from harness import webapp
    with webapp.Handler._terminal_lock:
        assert session not in webapp.Handler._terminal_gates
    if ending != "completed":
        return
    # And end to end: the request sent on that frame ran, on the same history.
    _settle()
    assert probe["next"][-1][1]["error"] in ("", None), "the second request was refused"
    assert [call["message"] for call in lab.calls] == ["first request", "second request"]
    assert [m["content"] for m in sessions.load(session)["messages"]] == [
        "first request", "answer for 'first request'",
        "second request", "answer for 'second request'"]


# ------------------------------------- two runs of one conversation, in order
def _watch(session, make=lambda: queue.Queue(maxsize=1024)):
    """The two feeds a session's runs share: a mirroring window, and a live Map."""
    from harness import webapp
    mirror, live = make(), make()
    with webapp.Handler._mirror_lock:
        webapp.Handler._mirror_subs.setdefault(session, []).append(mirror)
    with webapp.Handler._live_lock:
        webapp.Handler._live_subs.append(live)
    return mirror, live


def _unwatch(session, mirror, live):
    """Leave the feeds, leaving no gate of this test's own behind."""
    from harness import webapp
    with webapp.Handler._mirror_lock:
        webapp.Handler._mirror_subs.get(session, []).remove(mirror)
    with webapp.Handler._live_lock:
        webapp.Handler._live_subs.remove(live)
    with webapp.Handler._terminal_lock:
        left = webapp.Handler._terminal_gates.pop(session, None)
    if left:
        left.flush()


def _received(q):
    """What one feed was handed, in order: (kind, the run it was about)."""
    with q.mutex:
        out, q.queue = list(q.queue), type(q.queue)()
    return [(kind, data.get("run")) for kind, data in out]


def test_a_hand_over_ends_the_old_run_before_the_new_one_starts(lab, monkeypatch):
    """Two runs, one conversation, one pair of feeds — so order is the contract.
    A follow-up is handed the lease and publishes while the turn that scheduled
    it is still finishing; out of order, that turn's ``done`` ends the *new* run
    on every watching window and drops the backlog of a run that is alive."""
    from harness import webapp
    session = "mirror-handover"
    _accept(lab, session, "follow-1", "second request")
    started, hold, runs = threading.Event(), threading.Event(), {}

    def _hold_the_follow_up(harness, record):
        if record["message"] == "second request":
            with webapp.Handler._runs_lock:
                runs["second"] = webapp.Handler._runs[session]["run"]
            started.set()
            hold.wait(10)
    lab.during_run = _hold_the_follow_up
    real_schedule = web_tasks.schedule

    def _schedule_and_let_it_run(sid, owner, entry):
        # One legal schedule, forced: the follow-up reaches its run before the
        # thread that started it carries on, so what the ending turn owes its
        # feeds must have happened before this returns, not after it.
        out = real_schedule(sid, owner, entry)
        assert started.wait(10), "the follow-up never started"
        return out
    monkeypatch.setattr(web_tasks, "schedule", _schedule_and_let_it_run)
    mirror, live = _watch(session)
    try:
        events, probe = _stream_probing_on_done(lab, monkeypatch, q="first request",
                                                session=session)
        assert [kind for kind, _ in events].count("done") == 1, "the frame was swallowed"
        assert probe["admitted"] is False, "a follow-up really is running on that lease"
        runs["first"] = events[-1][1]["run"]
        for feed in (_received(mirror), _received(live)):
            assert feed.count(("done", runs["first"])) == 1, feed
            assert feed.index(("done", runs["first"])) < feed.index(("start", runs["second"])), (
                "a watching window was shown the new run ending before it began: %r" % (feed,))
        # And what a window arriving now is replayed: the running run's start.
        with webapp.Handler._mirror_lock:
            backlog = [(k, d.get("run")) for k, d
                       in webapp.Handler._mirror_backlog.get(session, [])]
        assert ("start", runs["second"]) in backlog, backlog
        assert ("done", runs["first"]) not in backlog, backlog
    finally:
        hold.set()
        lab.during_run = None
        _unwatch(session, mirror, live)
    _settle()
    assert [call["message"] for call in lab.calls] == ["first request", "second request"]
    assert _states(session) == {"follow-1": "consumed"}


def test_a_follow_up_does_not_wait_for_the_ending_turns_own_socket_or_phone(
        lab, monkeypatch):
    """The ending turn owes two different things, and only one of them is shared.

    A follow-up that has been accepted, claimed and handed the lease is waiting on
    an ordering — this turn's ``done`` on the feeds they both publish to.  It is
    not waiting on this turn's own socket, which nobody may be reading, or on a
    phone that never answers; the run those belong to is over.  Behind them, an
    accepted request does not start until a write to a dead client times out.
    """
    from harness import webapp
    session = "handover-wedged-io"
    _accept(lab, session, "follow-1", "second request")
    notifying, unwedge, ran = threading.Event(), threading.Event(), threading.Event()
    runs, read = {}, []

    def _wedged_notify(sid, res, wall_ms=None, replay=False):
        # The real notifier's own seam, with a phone that never answers: held as
        # this turn's own I/O (`shared=False`), published at its release.
        if not replay and webapp.Handler._terminal_defer(
                sid, lambda gate: lambda: _wedged_notify(sid, res, wall_ms, replay=True),
                shared=False):
            return
        notifying.set()
        assert unwedge.wait(20), "the ending turn's notifier was never let go"

    def _remember(harness, record):
        with webapp.Handler._runs_lock:
            runs[record["message"]] = webapp.Handler._runs[session]["run"]
        if record["message"] == "second request":
            ran.set()

    monkeypatch.setattr(webapp.Handler, "_notify_done", staticmethod(_wedged_notify))
    lab.during_run = _remember
    mirror, live = _watch(session)
    # The first turn's own frame is held behind the wedged notifier too, so the
    # reader stays blocked and the assertions run while that turn is still writing.
    reader = threading.Thread(
        target=lambda: read.append(_stream(lab, q="first request", session=session)),
        daemon=True)
    try:
        reader.start()
        assert notifying.wait(20), "the ending turn never reached its own I/O"
        assert ran.wait(20), "the follow-up waited for the ending turn's own I/O"
        assert not read, "the ending turn's own frame is still unwritten, as intended"
        # What the follow-up did start behind: this turn's ending, in order, on
        # every feed the two of them share.
        for feed in (_received(mirror), _received(live)):
            assert ("done", runs["first request"]) in feed and (
                feed.index(("done", runs["first request"]))
                < feed.index(("start", runs["second request"]))), (
                "a watching window was shown the new run ending before it began: %r" % (feed,))
        unwedge.set()
        reader.join(20)
        assert read and read[0][-1][0] == "done", "the ending turn's own frame never landed"
        assert read[0][-1][1]["run"] == runs["first request"]
        assert [kind for kind, _ in read[0]].count("done") == 1
    finally:
        unwedge.set()
        lab.during_run = None
        reader.join(20)
        _unwatch(session, mirror, live)
    _settle()
    assert [call["message"] for call in lab.calls] == ["first request", "second request"]
    assert _states(session) == {"follow-1": "consumed"}


def test_a_hand_over_publishes_the_shared_half_and_still_owes_its_own(lab):
    """Handing the session on is not the end of the ending turn's announcement.

    What a successor must not overtake goes out first and frees the session; what
    only this turn is waiting on is still this turn's to write, exactly once, at
    the tail of the turn — and taking nothing back off the successor."""
    from harness import webapp
    session = "handover-halves"
    mirror, live = _watch(session)
    own = []
    try:
        ending = webapp.Handler._terminal_arm(session)
        webapp.Handler._mirror_pub(session, "done", {"session": session, "run": "old"})
        assert ending.defer(lambda: own.append("this turn's socket"), shared=False)
        assert not _received(mirror), "an ending turn is held until it hands over"

        webapp.Handler._terminal_handover(session, ending)
        assert _received(mirror) == [("done", "old")], "the shared half waited"
        assert own == [], "the next run was made to wait for this turn's own socket"
        with webapp.Handler._terminal_lock:
            assert session not in webapp.Handler._terminal_gates, "the session was not handed on"
        # And nothing more may be held: a `done` offered now would land behind the
        # start of the run that replaced this one.
        assert ending.defer(lambda: own.append("late"), shared=True) is False

        successor = webapp.Handler._terminal_arm(session)
        webapp.Handler._terminal_release(session, ending)     # the tail of the old turn
        assert own == ["this turn's socket"]
        with webapp.Handler._terminal_lock:
            assert webapp.Handler._terminal_gates.get(session) is successor, (
                "the ending turn's release took the successor's session")
        webapp.Handler._terminal_release(session, successor)
        assert own == ["this turn's socket"], "the ending turn's own I/O was published twice"
        assert not _received(mirror)
    finally:
        _unwatch(session, mirror, live)


def test_a_follow_up_that_cannot_be_started_stays_accepted_and_says_so(lab, monkeypatch):
    """The hand-over happens before the follow-up exists, so it can still fail.

    A thread that cannot be started leaves the lease in this turn's hands and the
    request accepted: it goes back to waiting, the session is free, and why
    nothing is running is somewhere a person can read it."""
    from harness import webapp
    session = "handover-no-thread"
    _accept(lab, session, "follow-1", "second request")

    real_schedule, refused = web_tasks.schedule, []

    def _no_thread(sid, owner, entry):
        if not refused:                    # the hand-over's one failure, then honest
            refused.append(True)
            raise RuntimeError("can't start new thread")
        return real_schedule(sid, owner, entry)
    monkeypatch.setattr(web_tasks, "schedule", _no_thread)
    mirror, live = _watch(session)
    try:
        events = _stream(lab, q="first request", session=session)
        assert events[-1][0] == "done" and events[-1][1]["error"] in ("", None)
        assert _received(mirror).count(("done", events[-1][1]["run"])) == 1, (
            "the turn that could not hand over never finished announcing either")
    finally:
        _unwatch(session, mirror, live)
    _settle()

    assert [call["message"] for call in lab.calls] == ["first request"]
    assert _states(session) == {"follow-1": "pending"}, "the accepted request is still waiting"
    with webapp.Handler._terminal_lock:
        assert session not in webapp.Handler._terminal_gates
    code, listing = _get(lab, "/api/task-inbox?session=" + session)
    assert code == 200 and listing["owner_busy"] is False, "the lease leaked"
    assert listing["queue_error"]["kind"] == "settlement"
    assert "could not be started" in listing["queue_error"]["error"]
    assert listing["queue_error"]["entry"] == "follow-1"
    # Still the same request, and an explicit Start runs it on the same history.
    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    assert started["entry"]["id"] == "follow-1" and started["entry"]["text"] == "second request"
    _settle()
    assert [call["message"] for call in lab.calls] == ["first request", "second request"]
    assert _states(session) == {"follow-1": "consumed"}


@pytest.mark.parametrize("bus", ["mirror", "live"])
@pytest.mark.parametrize("patience", ["spent", "kept", "dropped"])
def test_a_successor_cannot_publish_through_an_ending_turn(lab, monkeypatch, bus, patience):
    """The next turn arrives while the one before it is still announcing — with
    that turn's gate to take first (``kept``), or past the bounded wait, which
    supersedes it either mid-enqueue (``spent``) or before it published at all
    (``dropped``).  Publishing is the enqueue, not the decision to enqueue: an
    old ``done`` must never land behind the new run's ``start``, which is what
    tells a window which run ended, and a superseded one lands not at all."""
    from harness import webapp
    session = "feed-order-%s-%s" % (bus, patience)
    entered, release, published = (threading.Event(), threading.Event(), threading.Event())

    class _Paused(queue.Queue):
        def put_nowait(self, item):
            if item[1].get("run") == "old" and patience != "dropped":
                entered.set()
                assert release.wait(10)
            return super().put_nowait(item)

    if patience != "kept":
        monkeypatch.setattr(webapp.Handler, "_TERMINAL_HANDOVER_S", 0.05)
    mirror, live = _watch(session, _Paused)
    feed = mirror if bus == "mirror" else live
    publish = ((lambda kind, data: webapp.Handler._mirror_pub(session, kind, data))
               if bus == "mirror" else webapp.Handler._live_pub)
    ending = webapp.Handler._terminal_arm(session)
    publish("done", {"session": session, "run": "old"})
    assert not _received(feed), "an ending turn is held until its release"

    def _the_next_turn():
        webapp.Handler._terminal_arm(session)      # its first act, holding the lease
        publish("start", {"session": session, "run": "new"})
        published.set()

    flushing = threading.Thread(target=webapp.Handler._terminal_release,
                                args=(session, ending), daemon=True)
    successor = threading.Thread(target=_the_next_turn, name="next-turn", daemon=True)
    try:
        if patience == "dropped":
            successor.start()                      # takes the session unopposed
            successor.join(10)
            flushing.start()                       # too late to be heard at all
        else:
            flushing.start()
            assert entered.wait(10), "the held ending never reached the feed"
            successor.start()
            assert not published.wait(0.3), "the next run published over the ending one"
            release.set()
        flushing.join(10)
        successor.join(10)
        assert published.is_set(), "the next run never got the session"
        order = _received(feed)
        assert order[-1:] == [("start", "new")], (
            "an old ending overtook the run that replaced it: %r" % (order,))
        assert patience != "dropped" or order == [("start", "new")], (
            "a superseded ending was published anyway: %r" % (order,))
        with webapp.Handler._mirror_lock:
            backlog = [(k, d.get("run")) for k, d
                       in webapp.Handler._mirror_backlog.get(session, [])]
        assert ("done", "old") not in backlog and ending.stale == (patience != "kept"), backlog
    finally:
        release.set()
        flushing.join(10)
        successor.join(10)
        _unwatch(session, mirror, live)


def test_an_orphaned_ending_is_never_adopted_by_the_turn_after_it(lab):
    """A detached turn whose bookkeeping broke has no gate to flush.  Announcing
    its ending is one step with checking for a successor: deciding first and
    publishing after hands that ``done`` to the new turn's gate, which publishes
    it at the end of a run it never belonged to."""
    from harness import webapp
    session = "orphan-late"
    old = lambda gate: webapp.Handler._mirror_pub(
        session, "done", {"session": session, "run": "old"}, held=gate)
    mirror, live = _watch(session)
    armed, publishing, hold = threading.Event(), threading.Event(), threading.Event()

    def _announcing(gate):
        publishing.set()
        assert not armed.wait(0.3), "a successor took the session mid-announcement"
        hold.wait(10)
        old(gate)

    orphan = threading.Thread(
        target=lambda: webapp.Handler._terminal_orphan(session, _announcing), daemon=True)
    next_turn = threading.Thread(
        target=lambda: (webapp.Handler._terminal_arm(session), armed.set()), daemon=True)
    try:
        assert webapp.Handler._terminal_orphan(session, old) is True   # nobody owns it
        assert _received(mirror) == [("done", "old")]
        orphan.start()
        assert publishing.wait(10)
        next_turn.start()
        hold.set()
        orphan.join(10)
        next_turn.join(10)
        assert armed.is_set() and _received(mirror) == [("done", "old")]
        # The successor owns the session now, so a late ending is dropped where
        # it stands — not held for, and published by, that turn's own release.
        assert webapp.Handler._terminal_orphan(session, old) is False
        webapp.Handler._terminal_release(session, webapp.Handler._terminal_gates[session])
        assert not _received(mirror), "the old ending was adopted by the next turn"
    finally:
        hold.set()
        _unwatch(session, mirror, live)


def test_a_queued_turn_that_fails_late_ends_its_own_run_and_no_other(lab, monkeypatch):
    """A detached turn reports its own failure, but the registry row it ends must
    be the one it recorded when it began: by the time the failure surfaces the
    lease may be a successor's, and "end this session's run" would fail that."""
    from harness import webapp
    session = "late-failure"

    def _explode(handler, qs, owner=None, entry=None):
        raise RuntimeError("the bookkeeping broke")
    monkeypatch.setattr(web_tasks, "serve_managed_stream", _explode)
    successor_run = webapp.Handler._run_begin(session, "second request", os.getcwd())
    try:
        # No run of its own, so it ends none — and still reports the failure.
        web_tasks._run_detached(web_tasks.DetachedSink(session), session, None, {"id": "q1"})
        with webapp.Handler._runs_lock:
            row = dict(webapp.Handler._runs[session])
        assert (row["run"], row["ended"]) == (successor_run, None), row
        assert "bookkeeping" in (web_tasks.queue_error(session) or {}).get("error", "")
        sink = web_tasks.DetachedSink(session)
        sink._run_id = successor_run          # its own row, recorded as the run began
        web_tasks._run_detached(sink, session, None, {"id": "q2"})
        with webapp.Handler._runs_lock:
            assert webapp.Handler._runs[session]["ended"] is not None
    finally:
        with webapp.Handler._runs_lock:
            webapp.Handler._runs.pop(session, None)
            webapp.Handler._cancel_events.pop(session, None)
        web_tasks.clear_queue_error(session)
