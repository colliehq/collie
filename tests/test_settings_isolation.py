"""One person's Settings save must not reach into a run that is already happening.

Settings are process-global by design — ``apply()`` exports them as ``COLLIE_*`` so that every
existing ``os.environ.get`` picks the panel up — and two things went wrong with that.

The first is the budget.  ``_budget_exceeded`` re-read ``COLLIE_MAX_COST`` and
``COLLIE_MAX_TOTAL_TOKENS`` at every turn boundary, which made a save retroactive: measured, a
run had already spent 5000 tokens when the user saved "stop past 1000" in another tab meaning it
for future work, and the answer they were waiting on came back "_[stopped: budget ceiling
reached]_".  The same read runs the other way for a request waiting in the durable inbox — a cap
raised while it waited would silently become the cap it ran under, and nobody authorized that
either.  So ceilings are snapshot once, per run, and carried by value.

The second is ownership.  ``apply()`` popped or overwrote every ``COLLIE_<KEY>`` it recognised,
including ones the running process had set for itself, and it is called on a timer by surfaces
that know nothing about each other.  A capability granted for one session and a Mission's
browser-bridge selection both disappeared across an unrelated ``apply()``.

Every model call below is an in-process fake.  Nothing here talks to a provider.
"""
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

from harness import cli, settings, task_inbox                          # noqa: E402
from harness.providers import Completion, ToolCall, Usage              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ the panel, as a fixture

@pytest.fixture
def panel(tmp_path, monkeypatch):
    """A real settings.json this test owns, plus a clean slate for apply()'s bookkeeping.

    ``apply()`` writes ``COLLIE_*`` into this process's environment — that is the behaviour under
    test, so it is not stubbed.  What the fixture does is restore exactly those variables
    afterwards, rather than replacing ``os.environ`` with something that would stop the test
    exercising the real thing.
    """
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_PATH", str(path))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    monkeypatch.setattr(settings, "_injected", {})
    before = {k: v for k, v in os.environ.items() if k.startswith("COLLIE_")}
    # A limit exported in whoever's shell is running this would outrank the panel — correctly,
    # and it is tested on its own below.  Here the panel is the thing under test, so start from
    # a machine where nobody has hard-set one.  Everything is put back in the finaliser.
    for key in settings.LIMIT_KEYS:
        os.environ.pop("COLLIE_" + key, None)
    monkeypatch.setattr(settings, "_HARD_ENV",
                        settings._HARD_ENV - {"COLLIE_" + k for k in settings.LIMIT_KEYS})
    try:
        yield SimpleNamespace(path=path, save=lambda **values: (settings.save(values),
                                                               settings.apply()))
    finally:
        for key in [k for k in os.environ if k.startswith("COLLIE_")]:
            if key not in before:
                os.environ.pop(key, None)
        os.environ.update(before)


class _Scripted:
    """A provider that answers from a list, and can be held mid-turn.

    ``name`` is not "mock" so the loop takes its ordinary path; ``complete`` is called on the
    thread of whichever run is asking, which is what makes two of these a concurrency test.
    """

    reports_cache = False
    subscription_only = False
    max_tokens = 8192

    def __init__(self, steps, name="deepseek", model="deepseek-chat"):
        self.steps = list(steps)
        self.name, self.model = name, model
        self.calls = 0

    def complete(self, system, messages, tool_schemas, on_text=None):
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        return step(messages) if callable(step) else step


def _tool_turn(path, tokens=(4000, 1000), call_id="c1"):
    return Completion(text="working", stop_reason="tool_use",
                      tool_calls=[ToolCall(call_id, "read_file", {"path": path})],
                      usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1]))


def _final(text="the finished answer"):
    return Completion(text=text, stop_reason="end_turn",
                      usage=Usage(input_tokens=10, output_tokens=10))


def _harness(tmp_path, monkeypatch, name, **kwargs):
    monkeypatch.setattr(cli, "DATA", str(tmp_path / ("data-" + name)))
    return cli.make_harness(str(tmp_path), provider="mock", project=name, embed="hash", **kwargs)


# ------------------------------------------------------ 1. the budget of a run in flight

def test_frozen_limits_read_one_saved_settings_revision(panel, monkeypatch):
    reads = []
    versions = [{"MAX_COST":"4", "MAX_TOTAL_TOKENS":"400", "MAX_TURNS":"40"},
                {"MAX_COST":"8", "MAX_TOTAL_TOKENS":"800", "MAX_TURNS":"80"}]
    def changing_file():
        reads.append(True)
        return versions[min(len(reads)-1, 1)]
    monkeypatch.setattr(settings, "_load", changing_file)
    limits = settings.limits_from_payload(settings.freeze_limits())
    assert (limits.max_cost, limits.max_total_tokens, limits.max_turns) == (4, 400, 40)
    assert len(reads) == 1


def test_freezing_limits_cannot_observe_a_half_applied_panel(panel, monkeypatch):
    panel.save(MAX_COST="4", MAX_TOTAL_TOKENS="400", MAX_TURNS="40")
    settings.save({"MAX_COST":"8", "MAX_TOTAL_TOKENS":"800", "MAX_TURNS":"80"})
    entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
    original = type(os.environ).__setitem__
    frozen, errors = [], []

    def held_write(env, key, value):
        original(env, key, value)
        if key == "COLLIE_MAX_COST" and value == "8":
            entered.set()
            if not release.wait(5):
                raise AssertionError("settings apply was not released")

    def capture():
        try:
            frozen.append(settings.freeze_limits())
        except BaseException as exc:
            errors.append(exc)
        finally:
            read_done.set()

    monkeypatch.setattr(type(os.environ), "__setitem__", held_write)
    writer = threading.Thread(target=settings.apply, daemon=True)
    reader = threading.Thread(target=capture, daemon=True)
    try:
        writer.start()
        assert entered.wait(2)
        reader.start()
        assert not read_done.wait(.1), "a task accepted a partly updated budget"
    finally:
        release.set()
        writer.join(3)
        if reader.ident is not None:
            reader.join(3)
    assert not errors and len(frozen) == 1
    limits = settings.limits_from_payload(frozen[0])
    assert (limits.max_cost, limits.max_total_tokens, limits.max_turns) == (8, 800, 80)


def test_a_budget_saved_mid_run_binds_the_next_run_and_never_the_one_in_flight(
        tmp_path, monkeypatch, panel):
    """Two real harnesses, two real threads, one Settings save in between.

    This is the reproducer from the audit, with the second run added: the point is not only
    that the running turn survives, but that the save genuinely took effect — a fix that simply
    ignored the panel would pass half of this and fail the other half.
    """
    (tmp_path / "fact.txt").write_text("supported finding", encoding="utf-8")
    panel.save(PROVIDER="mock")

    in_first_turn = threading.Event()
    saved = threading.Event()

    def hold(_messages):
        # Spend 5000 tokens, then stop until the panel has moved.  Under the old code the cap
        # typed here was compared against the tokens this run had ALREADY spent.
        in_first_turn.set()
        assert saved.wait(30), "the panel save never happened"
        return _tool_turn("fact.txt")

    running = _harness(tmp_path, monkeypatch, "in-flight", delegate=False)
    running.self_verify = False
    running.provider = _Scripted([hold, _final("the finished answer")])

    out = {}
    thread = threading.Thread(
        target=lambda: out.update(res=running.run("in-flight", "keep going",
                                                  consolidate=False)),
        daemon=True, name="collie-test-in-flight")
    thread.start()
    assert in_first_turn.wait(30), "the first run never reached the model"

    # The user, in another tab: "Budget: stop past 1000 tokens".  Exactly what webapp does.
    panel.save(PROVIDER="mock", MAX_TOTAL_TOKENS="1000")
    assert os.environ["COLLIE_MAX_TOTAL_TOKENS"] == "1000", "the save really did land in env"
    saved.set()
    thread.join(60)
    assert not thread.is_alive()

    first = out["res"]
    assert first.budget_exhausted is False, "a ceiling typed mid-run is not retroactive"
    assert first.answer == "the finished answer"
    assert first.total_tokens == 5020 and first.turns == 2
    assert first.budget_limits["MAX_TOTAL_TOKENS"] == "0", (
        "the receipt names the ceiling this run was actually held to")

    # ...and the very next run, started after the save, is bound by it.
    later = _harness(tmp_path, monkeypatch, "after-save", delegate=False)
    later.self_verify = False
    later.provider = _Scripted([_tool_turn("fact.txt"), _final("must not be requested")])
    second = {}
    thread2 = threading.Thread(
        target=lambda: second.update(res=later.run("after-save", "and again",
                                                   consolidate=False)),
        daemon=True, name="collie-test-after-save")
    thread2.start()
    thread2.join(60)
    assert not thread2.is_alive()

    stopped = second["res"]
    assert stopped.budget_exhausted is True
    assert "budget ceiling reached" in stopped.answer
    assert "1000 total tokens" in stopped.answer, "the receipt names the frozen ceiling"
    assert stopped.budget_limits["MAX_TOTAL_TOKENS"] == "1000"
    assert later.provider.calls == 1, "no second request past the ceiling"


def test_two_concurrent_runs_are_held_to_their_own_ceilings(tmp_path, monkeypatch, panel):
    """Two real runs alive at the same time, each bound by the panel as it was when it started.

    The old code had exactly one answer to "what is the budget?" for the whole process, so a
    capped run and an uncapped one could not have coexisted at all: whichever value the panel
    held at a turn boundary was applied to both.  Here the capped run is still inside its first
    model call when the cap is cleared and the second run starts.
    """
    (tmp_path / "fact.txt").write_text("supported finding", encoding="utf-8")
    started = {name: threading.Event() for name in ("capped", "free")}
    release = threading.Event()
    results = {}

    def hold(name, step):
        def _step(_messages):
            started[name].set()
            assert release.wait(30), "the runs were never released"
            return step
        return _step

    def spawn(name, harness):
        thread = threading.Thread(
            target=lambda: results.update({name: harness.run(name, "go", consolidate=False)}),
            daemon=True, name="collie-test-" + name)
        thread.start()
        assert started[name].wait(30), "%s never reached the model" % name
        return thread

    panel.save(PROVIDER="mock", MAX_TOTAL_TOKENS="1000")
    capped = _harness(tmp_path, monkeypatch, "capped", delegate=False)
    capped.self_verify = False
    capped.provider = _Scripted([hold("capped", _tool_turn("fact.txt")),
                                 _final("must not be requested")])
    capped_thread = spawn("capped", capped)

    # The cap is cleared while the first run is mid-turn, and a second run starts on top of it.
    panel.save(PROVIDER="mock")
    assert "COLLIE_MAX_TOTAL_TOKENS" not in os.environ
    free = _harness(tmp_path, monkeypatch, "free", delegate=False)
    free.self_verify = False
    free.provider = _Scripted([hold("free", _tool_turn("fact.txt")), _final("done anyway")])
    free_thread = spawn("free", free)

    release.set()                                    # both are past their first model call
    for thread in (capped_thread, free_thread):
        thread.join(60)
        assert not thread.is_alive()

    assert results["capped"].budget_exhausted is True, "still bound by the cap it started under"
    assert results["capped"].budget_limits["MAX_TOTAL_TOKENS"] == "1000"
    assert capped.provider.calls == 1
    assert results["free"].budget_exhausted is False, "and the run that started after it is not"
    assert results["free"].answer == "done anyway"
    assert results["free"].budget_limits["MAX_TOTAL_TOKENS"] == "0"


def test_a_reused_harness_takes_a_fresh_snapshot_for_each_run(tmp_path, monkeypatch, panel):
    """Freezing is per RUN, not per harness: a panel save still lands on the next turn.

    ``make_harness`` deliberately does not pin the budget onto the object it returns, so a
    surface that keeps one harness across turns keeps the behaviour people expect from the
    panel.  A caller that needs a pinned one — the durable queue — passes ``limits=`` instead.
    """
    (tmp_path / "fact.txt").write_text("supported finding", encoding="utf-8")
    panel.save(PROVIDER="mock")
    harness = _harness(tmp_path, monkeypatch, "reused", delegate=False)
    harness.self_verify = False

    harness.provider = _Scripted([_tool_turn("fact.txt"), _final("first answer")])
    first = harness.run("reused", "one", consolidate=False)
    assert first.budget_exhausted is False and first.answer == "first answer"

    panel.save(PROVIDER="mock", MAX_TOTAL_TOKENS="1000")
    harness.provider = _Scripted([_tool_turn("fact.txt"), _final("must not be requested")])
    second = harness.run("reused", "two", consolidate=False)
    assert second.budget_exhausted is True and harness.provider.calls == 1

    # ...whereas a harness handed an explicit snapshot keeps it, whatever the panel does next.
    pinned = _harness(tmp_path, monkeypatch, "pinned", delegate=False,
                      limits=settings.RunLimits.from_raw({"MAX_TOTAL_TOKENS": "0"},
                                                         source="frozen"))
    pinned.self_verify = False
    pinned.provider = _Scripted([_tool_turn("fact.txt"), _final("frozen answer")])
    third = pinned.run("pinned", "three", consolidate=False)
    assert third.budget_exhausted is False and third.answer == "frozen answer"
    assert third.budget_limits["source"] == "frozen"
    for h in (harness, pinned):
        h.memory.close(); h.recorder.close()


def test_a_delegated_child_is_held_to_the_ceiling_its_parent_started_under(
        tmp_path, monkeypatch, panel):
    """A child shares the parent's ledger, so it must share the parent's authorization too."""
    (tmp_path / "fact.txt").write_text("supported finding", encoding="utf-8")
    panel.save(PROVIDER="mock", MAX_TOTAL_TOKENS="200")

    parent = _harness(tmp_path, monkeypatch, "delegating", delegate=True)
    parent.self_verify = False

    def child_first_call(_messages):
        # The panel is raised to effectively unlimited while the child is mid-investigation.
        panel.save(PROVIDER="mock", MAX_TOTAL_TOKENS="10000000")
        return _tool_turn("fact.txt", tokens=(100, 20), call_id="r")

    parent.provider = _Scripted([
        Completion(text="", stop_reason="tool_use",
                   tool_calls=[ToolCall("d", "delegate", {"task": "inspect fact.txt"})],
                   usage=Usage(input_tokens=100, output_tokens=20)),
        child_first_call,
        _final("this third request must never be made"),
    ])
    result = parent.run("parent", "investigate", consolidate=False)

    assert os.environ["COLLIE_MAX_TOTAL_TOKENS"] == "10000000", "the raise did land"
    assert parent.provider.calls == result.model_calls == 2, (
        "the child stopped at the parent's 200, not at the panel's new value")
    assert result.total_tokens == 240 and result.budget_exhausted
    child = json.loads(next(m["content"] for m in result.messages
                            if m.get("role") == "tool" and m.get("name") == "delegate"))
    assert child["status"] == "budget_limit"
    parent.memory.close(); parent.recorder.close()


def test_a_frozen_snapshot_never_loosens_a_cap_the_user_set_in_the_environment():
    """``COLLIE_MAX_COST=0.10`` outranks the panel everywhere, including a replayed snapshot.

    ``_HARD_ENV`` is decided at import — that IS the definition of "the user set this before we
    started" — so this runs in a process started with the variable already set.
    """
    code = (
        "import json, sys;"
        "sys.path.insert(0, %r);"
        "from harness import settings;"
        "loose = settings.RunLimits.from_raw("
        "  {'MAX_COST': '5', 'MAX_TOTAL_TOKENS': '0', 'MAX_TURNS': '0',"
        "   'MAX_TOKENS': '', 'TEMPERATURE': ''}, source='frozen');"
        "tight = settings.RunLimits.from_raw("
        "  {'MAX_COST': '0.01', 'MAX_TOTAL_TOKENS': '0', 'MAX_TURNS': '0',"
        "   'MAX_TOKENS': '', 'TEMPERATURE': ''}, source='frozen');"
        "print(json.dumps({'pinned': settings.pinned('MAX_COST'),"
        " 'loosened': settings.enforce_pinned(loose).max_cost,"
        " 'tightened': settings.enforce_pinned(tight).max_cost}))" % ROOT)
    env = dict(os.environ, COLLIE_MAX_COST="0.10")
    env.pop("COLLIE_APPLIED_KEYS", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, env=env)
    assert out.returncode == 0, out.stderr
    answer = json.loads(out.stdout.strip().splitlines()[-1])
    assert answer["pinned"] is True
    assert answer["loosened"] == 0.10, "a snapshot cannot raise a cap the user hard-set"
    assert answer["tightened"] == 0.01, "...and a stricter snapshot is still honoured"


def test_an_unreplayable_snapshot_is_refused_rather_than_guessed_at():
    payload = settings.current_limits().payload()
    for broken, why in ((dict(payload, version=99), "version"),
                        (dict(payload, digest="0" * 32), "digest"),
                        ({"version": 1, "values": {"NOT_A_LIMIT": "1"}}, "unknown setting"),
                        ("not an object", "not an object")):
        with pytest.raises(ValueError) as caught:
            settings.limits_from_payload(broken)
        assert why in str(caught.value)
    # A legacy entry that made no claim is a different thing, and runs under today's values.
    assert settings.limits_from_payload(None) == settings.current_limits()


# ------------------------------------------------------- 2. apply() only touches what it owns

def _fresh_settings(tmp_path, name, env=""):
    """``harness.settings`` imported in a process whose environment already looks like `env`."""
    return {"path": str(tmp_path / (name + ".json")), "env": env}


def _in_process(tmp_path, name, script, env=None):
    """Run `script` against a freshly imported harness.settings, and read back its JSON."""
    path = str(tmp_path / (name + ".json"))
    code = ("import json, os, sys;"
            "sys.path.insert(0, %r);"
            "os.environ['COLLIE_SETTINGS_PATH'] = %r;\n"
            "from harness import settings\n" % (ROOT, path)) + script
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("COLLIE_")}
    child_env.update(env or {})
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, env=child_env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1]), path


def test_apply_does_not_revoke_a_value_the_running_process_set_for_itself(tmp_path):
    """The measured failure: a session grant erased by an unrelated surface's periodic apply().

    ``tools.EnableCapabilityTool`` sets ``COLLIE_SCREEN_CAPTURE`` directly when the save fails
    and tells the person it is on "for this session"; ``primitives`` selects the real logged-in
    browser with ``setdefault("COLLIE_BROWSER_BRIDGE", "1")``.  Live Copilot's ticker and the
    mission tick call ``apply()`` on a timer, and both values became ``None`` across one.
    """
    answer, _ = _in_process(tmp_path, "revoke", """
settings.save({"PROVIDER": "mock"})
settings.apply()
os.environ["COLLIE_SCREEN_CAPTURE"] = "on"            # tools.py, persist-failure grant
os.environ.setdefault("COLLIE_BROWSER_BRIDGE", "1")   # primitives.py, mission browse run
granted = (os.environ.get("COLLIE_SCREEN_CAPTURE"), os.environ.get("COLLIE_BROWSER_BRIDGE"))
settings.apply()                                      # live_copilot / mission tick / web request
settings.apply()                                      # and again, on the next tick
print(json.dumps({"granted": granted,
                  "after": (os.environ.get("COLLIE_SCREEN_CAPTURE"),
                            os.environ.get("COLLIE_BROWSER_BRIDGE")),
                  "owned": settings.owns("SCREEN_CAPTURE"),
                  "provider": os.environ.get("COLLIE_PROVIDER")}))
""")
    assert answer["granted"] == ["on", "1"]
    assert answer["after"] == ["on", "1"], "apply() may only remove what it put there"
    assert answer["owned"] is False, "and it knows the value is no longer its own"
    assert answer["provider"] == "mock", "the values it does own still apply"


def test_apply_does_not_overwrite_a_runtime_override_with_a_panel_value(tmp_path):
    """A key it injected once, replaced at runtime, is now somebody else's value."""
    answer, _ = _in_process(tmp_path, "overwrite", """
settings.save({"PROVIDER": "mock", "LANG": "en"})
settings.apply()
injected = os.environ.get("COLLIE_LANG")
os.environ["COLLIE_LANG"] = "zh"                      # a runtime decision, after import
settings.save({"PROVIDER": "mock", "LANG": "zh-tw"})  # ...and the panel moves on underneath it
settings.apply()
print(json.dumps({"injected": injected, "after": os.environ.get("COLLIE_LANG"),
                  "saved": settings._load().get("LANG"),
                  "applied_keys": os.environ.get("COLLIE_APPLIED_KEYS")}))
""")
    assert answer["injected"] == "en"
    assert answer["after"] == "zh", "the runtime override stands"
    assert answer["saved"] == "zh-tw", "and the panel value is still on disk, unharmed"
    assert "COLLIE_LANG" not in (answer["applied_keys"] or ""), (
        "a value it no longer owns is not advertised to a child as panel-injected")


def test_apply_still_updates_and_still_clears_the_values_it_owns(tmp_path):
    """The behaviour that must survive the fix: the panel keeps working."""
    answer, _ = _in_process(tmp_path, "owned", """
settings.save({"PROVIDER": "mock", "LANG": "en"})
settings.apply()
first = os.environ.get("COLLIE_LANG")
settings.save({"PROVIDER": "mock", "LANG": "zh"})
settings.apply()
changed = os.environ.get("COLLIE_LANG")
settings.save({"PROVIDER": "mock"})                   # the person clears the language
settings.apply()
print(json.dumps({"first": first, "changed": changed,
                  "cleared": os.environ.get("COLLIE_LANG", "<absent>"),
                  "reinjected_after_removal": (
                      os.environ.pop("COLLIE_PROVIDER", None),
                      settings.apply() or os.environ.get("COLLIE_PROVIDER"))}))
""")
    assert answer["first"] == "en" and answer["changed"] == "zh"
    assert answer["cleared"] == "<absent>", "clearing a panel value still reverts it"
    assert answer["reinjected_after_removal"] == ["mock", "mock"], (
        "a variable nobody holds is free to be injected again")


def test_a_user_exported_env_var_is_still_never_touched(tmp_path):
    answer, _ = _in_process(tmp_path, "hard", """
settings.save({"PROVIDER": "anthropic"})
settings.apply()
print(json.dumps({"provider": os.environ.get("COLLIE_PROVIDER"),
                  "pinned": settings.pinned("PROVIDER"),
                  "owned": settings.owns("PROVIDER")}))
""", env={"COLLIE_PROVIDER": "mock"})
    assert answer == {"provider": "mock", "pinned": True, "owned": False}


def test_a_forked_child_still_lets_the_panel_move_an_inherited_value(tmp_path):
    """``COLLIE_APPLIED_KEYS`` exists so a child can tell an export from an injection.

    Value-tracking must not break it: a child seeded from the inherited value has to keep
    owning it, or the desktop app's spawned web server goes back to answering forever with the
    values it started with — the measured bug that comment describes.
    """
    path = str(tmp_path / "forked.json")
    parent_env = {k: v for k, v in os.environ.items() if not k.startswith("COLLIE_")}
    parent_env["COLLIE_SETTINGS_PATH"] = path
    # The parent: apply LANG=en and hand the child what it injected, exactly like a fork.
    code = ("import json, os, sys;"
            "sys.path.insert(0, %r);"
            "from harness import settings;"
            "settings.save({'LANG': 'en'});"
            "settings.apply();"
            "print(json.dumps({k: v for k, v in os.environ.items()"
            " if k.startswith('COLLIE_')}))" % ROOT)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, env=parent_env)
    assert out.returncode == 0, out.stderr
    inherited = json.loads(out.stdout.strip().splitlines()[-1])
    assert inherited["COLLIE_LANG"] == "en"
    assert inherited["COLLIE_APPLIED_KEYS"] == "COLLIE_LANG"

    # The child: same environment, and the panel changes under it.
    child_env = dict(parent_env, **inherited)
    answer, _ = _in_process(tmp_path, "forked", """
before = os.environ.get("COLLIE_LANG")
settings.save({"LANG": "zh"})
settings.apply()
print(json.dumps({"before": before, "after": os.environ.get("COLLIE_LANG"),
                  "pinned": settings.pinned("LANG"), "owned": settings.owns("LANG")}))
""", env=child_env)
    assert answer["before"] == "en"
    assert answer["after"] == "zh", "an inherited injection is still the panel's to move"
    assert answer["pinned"] is False and answer["owned"] is True


# --------------------------------------------- 3. the durable queue, over real HTTP

CONFIG = {"intent": "build", "quality": "balanced", "verification": "auto",
          "workspace": "current", "strategy": "single", "effort": "auto",
          "speed": "standard", "explicit_axes": "none"}


class _QueueHarness:
    """A harness that honours the durable-input contract and records what it was given."""

    run_owner = None
    input_entry = None
    steering_after_seq = 0

    def __init__(self, lab, **kwargs):
        self.lab = lab
        self.composer = SimpleNamespace(identity="")
        self.memory = self.recorder = SimpleNamespace(close=lambda: None,
                                                      finish_run=lambda res: None)
        self.max_turns = 20
        self.steering = None
        self.__dict__.update(kwargs)

    def settle_run_memory(self, *a, **kw):
        return None

    def run(self, task_id, message, history=None, authority_msg=None, **kwargs):
        from harness import sessions
        entry = getattr(self, "input_entry", None)
        owner = getattr(self, "run_owner", None)
        self.lab.calls.append({"message": message, "limits": getattr(self, "limits", None),
                               "max_turns": self.max_turns,
                               "provider_max_tokens": getattr(self.lab.provider,
                                                              "max_tokens", None),
                               "capabilities": getattr(self, "capabilities", None),
                               "entry": entry["id"] if entry else None})
        messages = list(history or [])
        if entry is not None:
            messages.append({"role": "user", "content": message, "source": "user",
                             "kind": entry["mode"], "inbox_id": entry["id"]})
            sessions.checkpoint(owner.session, messages, project="web", cwd=os.getcwd(),
                                run_id="fake", state="turn_boundary")
            task_inbox.ack(owner.session, owner, entry["id"])
        else:
            messages.append({"role": "user", "content": message})
        messages.append({"role": "assistant", "content": "done"})
        return SimpleNamespace(
            answer="done", error="", canceled=False, success=True, model="model-a",
            prefix_tokens=0, input_tokens=0, output_tokens=0, total_tokens=0, turns=1,
            tool_calls=0, wall_ms=5, cost_usd=0.0, verified=False, messages=messages,
            turns_exhausted=False, budget_exhausted=False, edited=False, model_calls=1,
            parent_run_id=None, stop_reason="")


@pytest.fixture
def lab(monkeypatch, tmp_path):
    """A real HTTP server and a real inbox; the model is the only fake thing in it."""
    from harness import webapp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.chdir(tmp_path)
    values = {"PROVIDER": "mock", "MODEL": "model-a", "REASONING_EFFORT": "auto",
              "INTERACTIVE_SPEED": "standard"}
    monkeypatch.setattr(webapp, "_provider", lambda: values.get("PROVIDER", ""))
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: values.get(
                            key, default if default is not None else ""))
    monkeypatch.setattr(settings, "_load", lambda: dict(values))
    for key in settings.LIMIT_KEYS:
        monkeypatch.delenv("COLLIE_" + key, raising=False)
    # The panel for this suite is the dict above; a COLLIE_* variable exported in whoever's
    # shell is running it must not join in through the hard-set-env layer.
    monkeypatch.setattr(settings, "_HARD_ENV", set())
    monkeypatch.setattr(webapp.Handler, "_notify_done", staticmethod(lambda *a, **kw: None))

    bench = SimpleNamespace(calls=[], settings=values, state=state,
                            provider=SimpleNamespace(max_tokens=8192, default_max_tokens=8192), run_opts={})

    def _make(*args, **kwargs):
        harness = _QueueHarness(bench, limits=kwargs.get("limits"), capabilities=kwargs.get("capabilities"))
        # The real make_harness hands the frozen per-turn knobs to the provider object rather
        # than to os.environ; mirror that here so the assertion means something.
        cli._apply_generation_limits(bench.provider, kwargs["limits"])
        if kwargs["limits"].max_turns > 0:
            harness.max_turns = max(1, min(120, kwargs["limits"].max_turns))
        return harness

    monkeypatch.setattr(cli, "make_harness", _make)
    monkeypatch.setattr(cli, "configure_run_options",
                        lambda h, **opts: bench.run_opts.update(opts))
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bench.base = "http://127.0.0.1:%d" % server.server_address[1]
    bench.token = webapp.TOKEN
    try:
        yield bench
    finally:
        _settle(20)
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        with webapp.Handler._runs_lock:
            webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()


def _settle(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [t for t in threading.enumerate()
                 if t.name.startswith("collie-web-input-") and t.is_alive()]
        if not alive:
            return
        for t in alive:
            t.join(timeout=0.25)
    raise AssertionError("a scheduled run never finished")


def _post(bench, path, body):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(bench.base + path + "?token=" + bench.token, data=data,
                                     method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


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


def _accept(bench, session, entry_id, text, **extra):
    body = {"session": session, "id": entry_id, "text": text, "mode": "follow_up",
            "config": dict(CONFIG)}
    body.update(extra)
    code, out = _post(bench, "/api/task-inbox", body)
    assert code == 200, out
    return out["entry"]


def _states(session):
    return {row["id"]: row["state"] for row in task_inbox.list_entries(session)}


def test_a_queued_request_runs_under_the_budget_it_was_accepted_with(lab):
    """Accept over HTTP, move the panel, execute: the accepted ceiling is the one enforced."""
    session = "queued-budget"
    lab.settings.update({"MAX_TOTAL_TOKENS": "5000", "MAX_COST": "0.50", "MAX_TURNS": "7",
                         "MAX_TOKENS": "4096"})
    entry = _accept(lab, session, "queued-1", "keep working")
    accepted = entry["config"]["frozen"]["limits"]
    assert accepted["values"]["MAX_TOTAL_TOKENS"] == "5000"

    # While it waits, the person tightens the budget for their *next* piece of work.
    lab.settings.update({"MAX_TOTAL_TOKENS": "100", "MAX_COST": "0.01", "MAX_TURNS": "2",
                         "MAX_TOKENS": "512"})

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert len(lab.calls) == 1 and lab.calls[0]["entry"] == "queued-1"
    limits = lab.calls[0]["limits"]
    assert limits.source == "frozen"
    assert (limits.max_total_tokens, limits.max_cost) == (5000, 0.50)
    assert lab.calls[0]["max_turns"] == 7, "the accepted turn cap, not the one saved later"
    assert lab.calls[0]["provider_max_tokens"] == 4096, (
        "handed to the provider object, so no other run in this process moved")
    assert _states(session) == {"queued-1": "consumed"}
    assert lab.settings["MAX_TOTAL_TOKENS"] == "100", "the panel itself was never rewritten"

    # ...and a request typed now, with no snapshot behind it, gets the current ceiling.
    _stream(lab, q="a live request", session="live-budget")
    assert lab.calls[-1]["limits"].max_total_tokens == 100
    assert lab.calls[-1]["limits"].source == "current"


def test_an_unchanged_budget_is_no_reason_to_refuse_anything(lab):
    """Freezing must be invisible when nothing moved — including under Pack."""
    session = "queued-steady"
    lab.settings.update({"MAX_TOTAL_TOKENS": "5000"})
    _accept(lab, session, "queued-1", "make the flaky test pass",
            config=dict(CONFIG, strategy="pack", check="pytest -q", n=2,
                        explicit_axes="strategy"))

    from harness import pack as pack_module
    ran = {}

    def _fake_pack(task, cwd, **kwargs):
        ran["task"] = task
        return {"winner": 0, "answer": "the winning patch", "n": 2,
                "attempts": [{"idx": 0, "verified": True, "turns": 1, "check_pass": True}],
                "reason": "check passed", "applied": False, "total_cost_usd": 0.0}

    original = pack_module.run_pack
    pack_module.run_pack = _fake_pack
    try:
        code, started = _post(lab, "/api/task-inbox/start", {"session": session})
        assert code == 200 and started["started"] is True
        _settle()
    finally:
        pack_module.run_pack = original
    assert ran["task"] == "make the flaky test pass"
    assert _states(session) == {"queued-1": "consumed"}


def test_native_pack_receives_the_budget_it_was_accepted_under(lab):
    """An accepted Pack has one aggregate ceiling even after the panel raises it."""
    from harness import pack as pack_module, web_tasks

    session = "queued-pack-budget"
    lab.settings.update({"MAX_COST": "0.50"})
    _accept(lab, session, "queued-1", "make the flaky test pass",
            config=dict(CONFIG, strategy="pack", check="pytest -q", n=2,
                        explicit_axes="strategy"))
    lab.settings.update({"MAX_COST": "9.00"})       # raised while it waited

    ran = []
    original = pack_module.run_pack
    pack_module.run_pack = lambda *a, **kw: ran.append(kw) or {
        "winner":0, "answer":"done", "n":2, "attempts":[], "applied":False}
    try:
        code, started = _post(lab, "/api/task-inbox/start", {"session": session})
        assert code == 200 and started["started"] is True
        _settle()
    finally:
        pack_module.run_pack = original

    assert len(ran) == 1 and ran[0]["limits"].max_cost == 0.50
    assert _states(session) == {"queued-1": "consumed"}


def test_queued_capability_consent_is_frozen_before_another_task_enables_it(lab):
    session = "queued-consent"
    lab.settings["SCREEN_CAPTURE"] = "off"
    entry = _accept(lab, session, "queued-consent-1", "inspect the project")
    assert entry["config"]["frozen"]["capabilities"]["values"]["SCREEN_CAPTURE"] is False
    lab.settings["SCREEN_CAPTURE"] = "on"
    code, started = _post(lab, "/api/task-inbox/start", {"session":session})
    assert code == 200 and started["started"]
    _settle()
    assert lab.calls[0]["capabilities"]["SCREEN_CAPTURE"] is False
    assert _states(session) == {"queued-consent-1":"consumed"}


def test_a_snapshot_this_build_cannot_replay_keeps_the_request_and_says_so(lab):
    """An entry written by a newer build must not fall back to whatever is configured now.

    A snapshot exists precisely so the ceiling cannot move; a build that reads one it does not
    understand and then picks its own number has thrown the guarantee away and said nothing.
    """
    from harness import web_tasks, webapp

    session = "queued-unreplayable"
    intact = _accept(lab, session, "queued-1", "keep working")
    # ...and one whose snapshot names a format version this build does not know, which is what
    # an entry left behind by a newer Collie looks like from here.
    frozen = dict(intact["config"]["frozen"],
                  limits=dict(intact["config"]["frozen"]["limits"], version=99))
    task_inbox.enqueue(session, "queued-2", "and then this one", mode="follow_up",
                       config=dict(intact["config"], frozen=frozen), client="web")

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()
    assert lab.calls and lab.calls[-1]["entry"] == "queued-1", "the intact entry runs normally"

    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()

    assert [c["entry"] for c in lab.calls] == ["queued-1"], "the unreadable one never ran"
    assert _states(session) == {"queued-1": "consumed", "queued-2": "pending"}
    error = web_tasks.queue_error(session)["error"]
    assert "format version 99" in error and "kept pending" in error


def test_the_run_scoped_settings_view_freezes_only_the_limits(lab):
    """Worker selection sees this run's ceilings and the live value of everything else."""
    from harness import webapp

    lab.settings.update({"MAX_COST": "9.00", "RUNNER": "collie", "MAX_TOKENS": ""})
    view = webapp._RunSettings(settings, settings.RunLimits.from_raw(
        {"MAX_COST": "0.25", "MAX_TOTAL_TOKENS": "1000"}, source="frozen"))
    assert view.get("MAX_COST", "0") == "0.25", "the frozen ceiling, not the panel's"
    assert view.get("MAX_TOTAL_TOKENS", "0") == "1000"
    assert view.get("RUNNER", "collie") == "collie", "everything else falls through, live"
    assert view.get("MAX_TOKENS", "8192") == "8192", "an unset knob keeps the caller's default"
    assert not hasattr(view, "apply") and not hasattr(view, "save"), "read-only by construction"
