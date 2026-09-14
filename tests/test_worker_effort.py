"""The reasoning effort a person selected, followed to `claude`'s argv.

The bug this file pins was not in any one function: every layer had a plausible
`effort` in it (the accepted request froze one, the router resolved one, the
receipt recorded one) and the only place it had to arrive — the worker's command
line — never received it.  So nothing here asserts that a string was copied from
one variable to another.  Each test drives the real slice, the real factory
(``runner_registry.make_runner``) and the real ``ClaudeCodeRunner``, and reads
the argv a process transport was actually handed.

Fakes are limited to the two seams a run cannot have in a test: the process
transport (no ``claude`` is executed, no model is called, no account is billed)
and the workspace snapshotter.  The environment is an explicit mapping rather
than this machine's, which is what keeps the billing guard deterministic.
"""
from __future__ import annotations

import argparse
import json

import pytest

from harness import cli, runner_registry, runner_slice, sessions, settings, web_tasks, webapp
from harness.agent_runners import ProcessOutcome
from harness.runner_specs import HarnessDecision, RunnerProbe


SESSION = "0199a213-81c0-7800-8aa1-bbab2a035a53"

# What Collie is allowed to hand down, with nothing in it that would move the
# charge off the person's Claude subscription.
PARENT = {
    "PATH": "C:\\tools;C:\\Windows\\System32",
    "PATHEXT": ".COM;.EXE;.CMD",
    "SYSTEMROOT": "C:\\Windows",
    "USERPROFILE": "C:\\Users\\dev",
    "TEMP": "C:\\Temp",
}


class FakeProcess:
    pid = 4242


class FakeTransport:
    """Records every launch request; returns one canned `claude -p` result."""

    def __init__(self, turns: int = 4):
        self.calls: list[dict] = []
        self.turns = turns

    def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
        self.calls.append({"argv": tuple(argv), "cwd": cwd, "stdin": stdin_text,
                           "env": dict(env or {})})
        on_process(FakeProcess())
        if len(self.calls) > self.turns:
            raise AssertionError("more worker turns than the test scripted")
        return ProcessOutcome(stdout=json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "did the work", "session_id": SESSION, "num_turns": 2,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }), stderr="", exit_code=0)

    def argv(self, index: int) -> tuple[str, ...]:
        return self.calls[index]["argv"]


def _flag(argv: tuple[str, ...], name: str) -> str:
    """The value of ``name`` in an argv, or ``""`` when the flag is absent."""
    return argv[argv.index(name) + 1] if name in argv else ""


def _probe(key: str = "claude-code", **over) -> RunnerProbe:
    values = dict(key=key, installed=True, executable_path="C:/bin/claude.cmd",
                  version="2.1.221", login="ok",
                  billing_class="subscription_allowance",
                  billing_mode="subscription", probed_at=1_800_000_000.0)
    values.update(over)
    return RunnerProbe(**values)


def _decision(**over) -> HarnessDecision:
    values = dict(
        runner="claude-code", source="user-pinned", credential_family="claude",
        billing_class="subscription_allowance", billing_mode="subscription",
        reasons=("runner: claude-code (user-pinned)",), rejected={}, candidates=(),
        fallback_chain=(), probe=_probe().to_dict(), probe_digest="c" * 8)
    values.update(over)
    return HarnessDecision(**values)


def _instrument(monkeypatch, transport):
    """Point every registry-built runner at ``transport``, and record the call.

    Only the transport, snapshotter and parent environment are replaced — the
    same three seams ``runner_compat._instrument`` uses — so the runner under
    test is the one production builds, from the arguments production passes.
    """
    built: list[dict] = []
    real = runner_registry.make_runner

    def make_runner(key, **kwargs):
        built.append(dict(kwargs, key=key))
        runner = real(key, **kwargs)
        runner.process_runner = transport
        runner.snapshotter = lambda _workspace: {"tree_digest": "same",
                                                 "snapshot_complete": True}
        runner._environ = PARENT
        runner._session_ids = lambda: SESSION
        return runner

    monkeypatch.setattr(runner_registry, "make_runner", make_runner)
    return built


# --- the slice: one accepted choice, two native turns ------------------------
def test_effort_crosses_the_factory_into_native_argv_and_survives_resume(
        monkeypatch, tmp_path):
    transport = FakeTransport()
    built = _instrument(monkeypatch, transport)

    first = runner_slice.run_adhoc(_decision(), "fix the parser", str(tmp_path),
                                   model="claude-opus-5", effort="high")
    receipt = runner_slice.receipt_of(first)
    # The second turn resumes exactly the way `cli._worker_session` and the Web
    # stream do: from the locator the previous receipt minted.
    runner_slice.run_adhoc(_decision(), "now the lexer", str(tmp_path),
                           model="claude-opus-5", effort="high",
                           resume_from=receipt.to_dict()["native_session"])

    assert built[0]["effort"] == "high" and built[1]["effort"] == "high"
    start, resume = transport.argv(0), transport.argv(1)
    assert _flag(start, "--effort") == "high"
    assert _flag(start, "--session-id") == SESSION
    # A resumed turn is a new process: dropping the flag here would finish the
    # person's task at a level they did not choose, halfway through.
    assert _flag(resume, "--effort") == "high"
    assert _flag(resume, "--resume") == SESSION
    assert "--session-id" not in resume


def test_auto_effort_leaves_the_worker_command_line_untouched(monkeypatch, tmp_path):
    transport = FakeTransport()
    _instrument(monkeypatch, transport)

    runner_slice.run_adhoc(_decision(), "fix the parser", str(tmp_path),
                           model="claude-opus-5")
    # "default" is what the router says when the person left effort on Auto.
    runner_slice.run_adhoc(_decision(), "fix the parser", str(tmp_path),
                           model="claude-opus-5", effort="default")

    assert "--effort" not in transport.argv(0)
    assert transport.argv(0) == transport.argv(1)


def test_unknown_effort_refuses_the_slice_before_any_worker_is_built(monkeypatch,
                                                                    tmp_path):
    transport = FakeTransport()
    built = _instrument(monkeypatch, transport)

    with pytest.raises(ValueError, match="reasoning effort must be auto"):
        runner_slice.run_adhoc(_decision(), "fix the parser", str(tmp_path),
                               effort="ultra")

    assert built == [] and transport.calls == []


# --- the Web/mobile accepted request -----------------------------------------
def _isolate(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "anthropic-oauth")
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None: (
        "claude-opus-5" if key == "MODEL" else default))
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(webapp.Handler, "_notify_done", lambda *a, **kw: None)
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"claude-code": _probe()})
    monkeypatch.setattr(cli, "make_harness", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("an external Web run must not build Collie's native harness")))
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear()
        webapp.Handler._cancel_events.clear()


def _handler(events):
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    return fake


def test_web_accepted_request_effort_reaches_the_native_worker(monkeypatch, tmp_path):
    """From the run configuration a request was accepted under to the argv.

    The composer's choice is frozen at acceptance (``web_tasks.freeze_config``)
    and replayed as the managed stream's query (``web_tasks.stream_query``) —
    which is the only path a follow-up takes — so that is what this drives,
    rather than a hand-written query string that could agree with the UI and
    with nothing else.
    """
    _isolate(monkeypatch, tmp_path)
    transport = FakeTransport()
    built = _instrument(monkeypatch, transport)

    config = web_tasks.freeze_config(
        {"intent": "build", "quality": "balanced", "verification": "auto",
         "workspace": "current", "strategy": "single", "effort": "high",
         "speed": "standard", "runner": "claude-code",
         "explicit_axes": "effort,runner"},
        provider="anthropic-oauth", model="claude-opus-5", reasoning_effort="auto")
    assert config["effort"] == "high"          # accepted as chosen, not defaulted

    query = web_tasks.stream_query("web-effort", config)
    query["q"] = ["fix the parser"]
    events = []
    webapp.Handler._serve_stream(_handler(events), query)

    start = next(data for kind, data in events if kind == "start")
    assert start["decision"]["runner"]["runner"] == "claude-code"
    assert start["effort"] == "high"           # what the UI is told is running
    assert built and built[0]["effort"] == "high"
    assert _flag(transport.argv(0), "--effort") == "high"   # what actually ran

    receipt = sessions.load("web-effort")["run_receipts"][-1]
    assert receipt["effort"] == "high"


def _panel(monkeypatch, **values):
    """The Settings panel as one dict the test can move under a waiting request."""
    values.setdefault("MODEL", "claude-opus-5")
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: values.get(key, default))
    return values


def _web_config(**over):
    """What the composer sends with its Reasoning-effort group left untouched."""
    raw = {"intent": "build", "quality": "balanced", "verification": "auto",
           "workspace": "current", "strategy": "single", "speed": "standard",
           "runner": "claude-code", "explicit_axes": "runner"}
    raw.update(over)
    return raw


def test_the_panels_default_effort_reaches_the_worker_when_the_composer_is_untouched(
        monkeypatch, tmp_path):
    """"Default reasoning effort: High" has to mean something on the Web too.

    Every client sends ``effort=auto`` — that is the composer's UNTOUCHED state,
    not a choice, which is why the axis is absent from ``explicit_axes``.  Read as
    a literal selection it silently outranked the Settings panel, so the knob
    worked at the prompt (``cli.resolve_turn_decision``) and nowhere in the
    browser.  Driven through the real stream entry point, so the evidence is the
    worker's own command line.
    """
    _isolate(monkeypatch, tmp_path)
    _panel(monkeypatch, REASONING_EFFORT="high")
    transport = FakeTransport()
    built = _instrument(monkeypatch, transport)

    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix the parser"], "session": ["web-effort-panel"],
        "intent": ["build"], "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"], "speed": ["standard"],
        "effort": ["auto"], "runner": ["claude-code"], "explicit_axes": ["runner"]})

    start = next(data for kind, data in events if kind == "start")
    assert start["effort"] == "high"
    assert built and built[0]["effort"] == "high"
    assert _flag(transport.argv(0), "--effort") == "high"


def test_an_explicitly_chosen_auto_still_outranks_the_panel_default(monkeypatch,
                                                                   tmp_path):
    """The other direction: a person who picked "Auto by task" gets Auto.

    ``explicit_axes`` is what separates the two, so the composer's default must
    not be able to impersonate a selection and a selection must not be ignored.
    """
    _isolate(monkeypatch, tmp_path)
    _panel(monkeypatch, REASONING_EFFORT="high")
    transport = FakeTransport()
    _instrument(monkeypatch, transport)

    events = []
    webapp.Handler._serve_stream(_handler(events), {
        "q": ["fix the parser"], "session": ["web-effort-explicit-auto"],
        "intent": ["build"], "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"], "speed": ["standard"],
        "effort": ["auto"], "runner": ["claude-code"],
        "explicit_axes": ["effort,runner"]})

    start = next(data for kind, data in events if kind == "start")
    # Auto resolves per task (balanced work asks for medium); the panel's High is
    # a default, and a default does not override the thing it is a default for.
    assert start["decision"]["sources"]["effort"] == "task-policy"
    assert start["effort"] == "medium"
    assert _flag(transport.argv(0), "--effort") == "medium"


def test_a_queued_request_keeps_the_default_effort_it_was_accepted_under(monkeypatch,
                                                                         tmp_path):
    """A request that waited runs at the default frozen for it, not today's.

    ``freeze_config`` records ``reasoning_effort`` at acceptance for exactly this
    moment, and the managed stream resolves it locally — the panel is read, never
    written, so a save made while this request waited moves no other run either.

    ``_run_stream`` is entered directly with the snapshot the execution manager
    hands it, which is what ``serve_managed_stream`` does once it holds the lease
    on a claimed inbox entry; the rest of the run is the real one.
    """
    _isolate(monkeypatch, tmp_path)
    panel = _panel(monkeypatch, REASONING_EFFORT="high")
    transport = FakeTransport()
    built = _instrument(monkeypatch, transport)

    config = web_tasks.freeze_config(
        _web_config(), provider="anthropic-oauth", model="claude-opus-5",
        reasoning_effort=panel["REASONING_EFFORT"])
    assert config["effort"] == "auto"                        # composer untouched
    assert config["frozen"]["reasoning_effort"] == "high"    # the panel at acceptance

    panel["REASONING_EFFORT"] = "low"     # ... another tab lowers it while it waits

    handler = _handler(events := [])
    handler._run_config_frozen = config["frozen"]
    query = web_tasks.stream_query("web-effort-frozen", config)
    query["q"] = ["fix the parser"]
    webapp.Handler._run_stream(handler, query)

    start = next(data for kind, data in events if kind == "start")
    assert start["effort"] == "high"
    assert built and built[0]["effort"] == "high"
    assert _flag(transport.argv(0), "--effort") == "high"
    assert panel["REASONING_EFFORT"] == "low", "replay reads the panel, never writes it"


def test_next_starts_the_same_entry_at_the_same_effort_as_the_web_queue(monkeypatch,
                                                                       tmp_path):
    """``/next`` is the other door onto one durable entry; it must agree.

    The terminal claims the very same record, so an accepted default resolved one
    way in the browser and another at the prompt would make where a person pressed
    Start decide how much reasoning they paid for.
    """
    from harness import terminal_queue

    _isolate(monkeypatch, tmp_path)
    panel = _panel(monkeypatch, REASONING_EFFORT="high")
    config = web_tasks.freeze_config(
        _web_config(runner=""), provider="anthropic-oauth", model="claude-opus-5",
        reasoning_effort=panel["REASONING_EFFORT"])

    panel["REASONING_EFFORT"] = "low"     # the panel moves while the request waits

    decision = terminal_queue.decision(
        {"text": "fix the parser", "config": config},
        "anthropic-oauth", "claude-opus-5", [], [])
    assert decision.effort == "high"

    chosen = web_tasks.freeze_config(
        _web_config(runner="", effort="low", explicit_axes="effort"),
        provider="anthropic-oauth", model="claude-opus-5", reasoning_effort="high")
    assert terminal_queue.decision(
        {"text": "fix the parser", "config": chosen},
        "anthropic-oauth", "claude-opus-5", [], []).effort == "low"


# --- `collie run --runner claude-code --effort high` --------------------------
def test_cli_run_carries_the_selected_effort_into_the_worker(monkeypatch, tmp_path,
                                                             capsys):
    from harness import router, settings as settings_module

    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "data" / "memory.db"), str(state / "data" / "runs.db"),
        str(state / "data" / "dashboard.html"), str(state / "data" / "sandbox")))
    monkeypatch.setattr(settings_module, "get", lambda key, default=None: default)
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"claude-code": _probe()})
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: router.RunDecision(
        provider="anthropic-oauth", model="claude-opus-5", effort="high",
        speed="standard", billing_multiplier=1.0, intent="build", quality="balanced",
        verification="auto", workspace="current", strategy="single",
        route_kind="code", complexity="simple"))
    transport = FakeTransport()
    _instrument(monkeypatch, transport)

    args = argparse.Namespace(
        task="rename the helper", cwd=str(tmp_path), provider="anthropic-oauth",
        model=None, project="demo", mode=None, persona=None, goal=None, resume=None,
        cont=False, stream_json=False, json=True, print=False, web_search=False,
        intent="build", quality="balanced", verification="auto", effort="high",
        speed=None, verify_command=None, runner="claude-code")
    assert cli.cmd_run(args) == 0
    json.loads(capsys.readouterr().out.strip())          # a well-formed receipt

    assert _flag(transport.argv(0), "--effort") == "high"


# --- Pack: every candidate, not just the one that wins ------------------------
def test_pack_candidates_all_run_at_the_selected_effort(monkeypatch, tmp_path):
    """Pack already labels each attempt with an effort; now that label is true.

    A best-of-N that recorded ``effort: high`` while every candidate ran at the
    CLI default would make the comparison the strategy exists for meaningless.
    """
    import tempfile

    from harness import pack

    monkeypatch.setattr(pack, "_isolate",
                        lambda _cwd: tempfile.mkdtemp(prefix="effort_pack_",
                                                      dir=str(tmp_path)))
    monkeypatch.setattr(pack, "_init_external_git", lambda _root: None)
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(tmp_path / "memory.db"), str(tmp_path / "runs.db"),
        str(tmp_path / "dashboard.html"), str(tmp_path / "sandbox")))
    transport = FakeTransport()
    _instrument(monkeypatch, transport)

    result = pack.run_pack("fix it", str(tmp_path), n=2, effort="high",
                           runner_decision=_decision(), runner_model="claude-opus-5")

    assert [attempt["effort"] for attempt in result["attempts"]] == ["high", "high"]
    assert len(transport.calls) == 2
    assert all(_flag(call["argv"], "--effort") == "high" for call in transport.calls)
