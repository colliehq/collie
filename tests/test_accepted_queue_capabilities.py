"""A queued request keeps the sensitive grants it was accepted under, wherever it starts.

The Web composer freezes DESKTOP_CONTROL / SCREEN_CAPTURE / MCP_MANAGE / MCP_DISCOVERY
at acceptance (``capability_policy.freeze``) and the managed stream replays that payload
with ``capability_policy.from_payload`` when the request's turn comes.  ``/next`` in
``collie repl`` / ``collie tui`` claims the very same durable entry, so it has to replay
the same payload: a toggle switched on while the request waited must not arm a task that
was never accepted with it, and the next line the person types must be measured against
the settings as they are then.

Live revocation is unchanged and still wins: a capability the panel has since turned off
stays off for a replayed grant, because that is the standing global gate.

Where a check needs a real gate rather than a reported one, the turn also calls the
shipped ``screenshot`` tool, which decides for itself whether SCREEN_CAPTURE lets it
reach the capture backend.

Offline throughout: mock provider, temp sessions root, temp settings file, and a
stubbed capture backend — nothing on the host is ever photographed.
"""
import json

import pytest

from harness import (capability_policy, cli, run_ownership, screenshot, sessions,
                     settings, task_inbox, terminal_queue, tui, web_tasks)
from harness.providers import Completion, MockProvider, ToolCall, Usage
from harness.tools import Tool


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated sessions root, data dir and settings file, like one installation."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    monkeypatch.setattr(settings, "_HARD_ENV", set())
    for key in tuple(settings.LIMIT_KEYS) + capability_policy.KEYS:
        monkeypatch.delenv("COLLIE_" + key, raising=False)
    return str(directory)


def _panel(**values):
    """Write the Settings panel file, as a save from another tab would."""
    with open(settings._PATH, "w", encoding="utf-8") as f:
        json.dump(values, f)
    settings._cache["mtime"] = -1.0


def _accept(sid, entry_id, text, *, cwd, capabilities=True):
    """Accept one request exactly as the web surface does: frozen settings and all."""
    frozen = web_tasks.freeze_config(
        {}, provider="mock", model="", limits=settings.freeze_limits(),
        capabilities=capability_policy.freeze() if capabilities else None)
    sessions.save(sid, [{"role": "user", "content": "earlier turn"}], cwd=cwd)
    return task_inbox.enqueue(sid, entry_id, text, mode="follow_up", config=frozen)


def _args(sid, cwd):
    from types import SimpleNamespace
    return SimpleNamespace(cwd=cwd, provider="mock", model=None, project="caps",
                           mode=None, persona=None, goal=None, resume=sid, cont=False,
                           task=None, json=False, print=False, stream_json=False)


class _ProbeProvider(MockProvider):
    """Spends the first call of every run on the tools below, then answers.

    Keyed off the last message rather than "is there any tool result in the thread",
    so a second turn that carries the first turn's transcript still probes.
    """
    default_max_tokens = max_tokens = 4096
    default_temperature = temperature = 0.2
    tools_to_call = ("policy_probe",)

    def complete(self, system, messages, tool_schemas, on_text=None):
        if messages and messages[-1].get("role") == "tool":
            return Completion(text="policy reported", usage=Usage(10, 10))
        return Completion(
            tool_calls=[ToolCall("probe-%d" % i, name,
                                 {"max_dim": 256} if name == "screenshot" else {})
                        for i, name in enumerate(self.tools_to_call)],
            usage=Usage(10, 10))


class _ScreenProbeProvider(_ProbeProvider):
    """Also calls the SHIPPED screenshot tool, which gates itself on SCREEN_CAPTURE.

    Its own gate (``screenshot._enabled`` -> ``capability_policy.allowed``) decides
    whether the capture backend is reached at all, so "did the backend run" is the
    real tool's decision about this turn's policy rather than anything a test asserts.
    """
    tools_to_call = ("policy_probe", "screenshot")


def _drive(monkeypatch, tmp_path, surface, lines, *, resume, seen, captures=None):
    """Run one interactive surface over scripted input with a capability-probing tool.

    Pass ``captures`` to also call the real ``screenshot`` tool each turn with its
    capture backend stubbed out: every element is one attempt that got PAST the tool's
    own capability gate. Nothing is captured on the host either way.
    """
    typed = iter(lines)
    real_make = cli.make_harness

    class Probe(Tool):
        name, tier, description = "policy_probe", "always", "Report this task's policy"
        schema = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            seen.append(capability_policy.allowed("SCREEN_CAPTURE", ctx))
            return "policy inspected"

    def make(cwd, **kw):
        h = real_make(cwd, **dict(kw, embed="bm25"))
        h.registry.register(Probe())
        if captures is not None:
            assert h.registry.get("screenshot") is not None, "no screenshot tool to gate"
        return h

    if captures is not None:
        def _no_display(**kw):
            captures.append(kw)
            return {"ok": False, "error": "no display in this test"}

        monkeypatch.setattr(screenshot, "capture", _no_display)

    provider = _ScreenProbeProvider if captures is not None else _ProbeProvider
    monkeypatch.setattr(cli, "make_provider", lambda *a, **kw: provider())
    monkeypatch.setattr(cli, "make_harness", make)
    monkeypatch.setattr(cli, "default_gate", lambda *a, **k: None)
    if surface == "tui":
        monkeypatch.setattr(tui, "_HAVE_RICH", False)
        monkeypatch.setattr(tui, "make_harness", make, raising=False)
        monkeypatch.setattr(tui, "_read_line", lambda *a, **k: next(typed))
        return tui.run_tui(str(tmp_path), "mock", None, project="caps", resume=resume)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(typed))
    return cli.cmd_repl(_args(resume, str(tmp_path)))


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_next_cannot_use_a_capability_enabled_after_the_request_was_accepted(
        store, tmp_path, monkeypatch, surface):
    """A toggle switched on while the request waited must not arm it."""
    sid = "queued-caps-" + surface
    _panel(SCREEN_CAPTURE="off")                 # accepted with screen capture off
    _accept(sid, "waiting", "inspect the policy", cwd=str(tmp_path))
    _panel(SCREEN_CAPTURE="on")                  # ... another tab enables it afterwards
    seen = []
    assert _drive(monkeypatch, tmp_path, surface, ["/next", "/exit"],
                  resume=sid, seen=seen) == 0
    assert seen == [False], "another request's grant must not arm this accepted task"
    assert task_inbox.get(sid, "waiting")["state"] == "consumed"


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_next_still_honours_a_capability_revoked_after_acceptance(
        store, tmp_path, monkeypatch, surface):
    """Replaying an accepted grant never outranks the live global gate."""
    sid = "revoked-caps-" + surface
    _panel(SCREEN_CAPTURE="on")                  # accepted while it was allowed
    _accept(sid, "waiting", "inspect the policy", cwd=str(tmp_path))
    _panel(SCREEN_CAPTURE="off")                 # ... then the person revokes it
    seen = []
    assert _drive(monkeypatch, tmp_path, surface, ["/next", "/exit"],
                  resume=sid, seen=seen) == 0
    assert seen == [False], "revocation is a standing gate, not a frozen value"


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_next_arms_a_grant_the_request_was_accepted_with_and_still_has(
        store, tmp_path, monkeypatch, surface):
    """The other direction: replaying an accepted policy must still GRANT what it holds.

    Without this the three refusals above are also satisfied by a terminal that simply
    never grants a queued request anything.
    """
    sid = "granted-caps-" + surface
    _panel(SCREEN_CAPTURE="on")                  # accepted with screen capture on ...
    _accept(sid, "waiting", "inspect the policy", cwd=str(tmp_path))
    seen = []                                    # ... and nobody touched the panel since
    assert _drive(monkeypatch, tmp_path, surface, ["/next", "/exit"],
                  resume=sid, seen=seen) == 0
    assert seen == [True], "an accepted grant that is still enabled must reach the tool"
    assert task_inbox.get(sid, "waiting")["state"] == "consumed"


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_two_queued_requests_each_run_under_their_own_accepted_policy(
        store, tmp_path, monkeypatch, surface):
    """Back-to-back ``/next`` on one live terminal: policy is per request, not per session.

    The shipped ``screenshot`` tool is called on both turns with its capture backend
    stubbed, so the recorded attempts are its own gate deciding — the first request may
    see the screen, the second was never accepted with that authority even though the
    panel says yes throughout.
    """
    sid = "caps-per-request-" + surface
    _panel(SCREEN_CAPTURE="on")                  # first request accepted with eyes
    _accept(sid, "with-eyes", "inspect the policy", cwd=str(tmp_path))
    _panel(SCREEN_CAPTURE="off")                 # second accepted without
    _accept(sid, "without-eyes", "inspect the policy", cwd=str(tmp_path))
    _panel(SCREEN_CAPTURE="on")                  # live setting allows both, all along
    seen, captures = [], []
    assert _drive(monkeypatch, tmp_path, surface, ["/next", "/next", "/exit"],
                  resume=sid, seen=seen, captures=captures) == 0
    assert seen == [True, False], "each request replays its own accepted policy"
    assert len(captures) == 1, (
        "the shipped screenshot tool ran on both turns and reached its backend only for "
        "the request accepted with screen capture: %r" % (captures,))
    assert captures[0]["max_dim"] == 256, captures
    assert [task_inbox.get(sid, e)["state"] for e in ("with-eyes", "without-eyes")] == [
        "consumed", "consumed"]


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_an_accepted_policy_does_not_leak_into_the_next_typed_turn(
        store, tmp_path, monkeypatch, surface):
    """One terminal Harness serves many turns, so a replayed policy must end with its run."""
    sid = "caps-no-leak-" + surface
    _panel(SCREEN_CAPTURE="off")
    _accept(sid, "waiting", "inspect the policy", cwd=str(tmp_path))
    _panel(SCREEN_CAPTURE="on")                  # enabled deliberately, before typing
    seen, captures = [], []
    assert _drive(monkeypatch, tmp_path, surface,
                  ["/next", "inspect the policy", "/exit"],
                  resume=sid, seen=seen, captures=captures) == 0
    assert seen == [False, True], "the typed turn runs under the current policy"
    assert len(captures) == 1, (
        "the shipped screenshot tool was refused on the queued turn and allowed on the "
        "typed one: %r" % (captures,))


def test_a_request_accepted_before_policies_were_frozen_uses_the_current_one(
        store, tmp_path, monkeypatch):
    """No claim about capabilities is an explicit absence, not an empty policy."""
    sid = "caps-legacy"
    _panel(SCREEN_CAPTURE="on")
    _accept(sid, "legacy", "inspect the policy", cwd=str(tmp_path), capabilities=False)
    seen = []
    assert _drive(monkeypatch, tmp_path, "repl", ["/next", "/exit"],
                  resume=sid, seen=seen) == 0
    assert seen == [True]


def test_a_frozen_policy_this_build_cannot_read_keeps_the_request_pending(store, tmp_path):
    """A payload that will not replay is a refusal, never a guessed permission."""
    sid = "caps-unreplayable"
    _panel(SCREEN_CAPTURE="on")
    entry = _accept(sid, "source", "do it", cwd=str(tmp_path))
    config = dict(entry["config"])
    config["frozen"] = dict(config["frozen"])
    config["frozen"]["capabilities"] = {"version": 2, "values": {"SCREEN_CAPTURE": True}}
    task_inbox.enqueue(sid, "broken", "do it", mode="follow_up", config=config)
    task_inbox.cancel(sid, "source", reason="replaced by the tampered copy")
    with run_ownership.hold(sid, label="test") as lease:
        with pytest.raises(terminal_queue.QueueError) as raised:
            with terminal_queue.claimed_next(sid, lease, requested=True):
                pass
    assert "cannot replay" in str(raised.value)
    assert task_inbox.get(sid, "broken")["state"] == "pending"
    assert task_inbox.get(sid, "broken")["attempts"] == 0
