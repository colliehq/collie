"""Stopping a run must not rewrite what already happened.

Ctrl-C, a Web stop button and a crashed turn all end in the same place: a
transcript, a durable journal and a result that three surfaces read. The rules
these tests hold to are the same on all of them — a completed action stays in the
conversation, an action whose outcome is unknown stays fenced until a human says
otherwise, every unanswered tool call gets an honest closure, and nothing that
runs after the stop can turn the attempt into a success.
"""
import json

import pytest

from harness import cli, sessions
from harness.providers import Completion, ToolCall
from _util import _ScriptProvider


def _harness(tmp_path, monkeypatch, sid="", **kw):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    h = cli.make_harness(str(tmp_path), provider="mock", project="lifecycle",
                         embed="hash", **kw)
    if sid:
        h.durable_session_id = sid
    return h


def _paired(messages):
    """Every tool_use in the thread that never received a result."""
    pending = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                if cid:
                    pending[cid] = msg
        elif msg.get("role") == "tool":
            pending.pop(msg.get("tool_call_id"), None)
    return pending


def _results(messages):
    return {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}


def _batch(*names_and_paths):
    return Completion(tool_calls=[
        ToolCall(cid, "write_file", {"path": path, "content": "written by " + cid})
        for cid, path in names_and_paths], stop_reason="tool_use")


# --------------------------------------------------------------------------- #
# the loop: an interrupted batch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name,args", [
    ("bash", {"command": "echo partial"}), ("grep", {"pattern": "anything"}),
])
def test_unconfirmed_process_tree_fences_batch_and_survives_final_save(tmp_path, monkeypatch, name, args):
    from harness import tool_process
    h = _harness(tmp_path, monkeypatch, sid="uncertain-tree")
    monkeypatch.setattr(tool_process, "run_owned", lambda *a, **kw:
                        tool_process.Outcome(tool_process.CANCELED, stdout="partial", tree_terminated=False))
    h.provider = _ScriptProvider([Completion(tool_calls=[
        ToolCall("running", name, args),
        ToolCall("not-started", "write_file", {"path": "must-not-exist", "content": "bad"}),
    ])])
    try:
        res = h.run("uncertain", "run the requested steps")
        assert "recovery inspection" in res.error
        assert not (tmp_path / "must-not-exist").exists()
        assert not _paired(res.messages)
        sessions.save("uncertain-tree", res.messages, answer=res.answer)
        state = sessions.recovery_state("uncertain-tree")
        assert state["recovery_required"]
        assert state["detail"]["tool_call_id"] == "running"
        assert state["state"] == "external_action", "even a grep process must carry its unknown lifetime"
    finally:
        h.memory.close(); h.recorder.close()


def test_cancel_nested_python_command_stops_descendants_and_resumes(tmp_path, monkeypatch):
    import time
    h = _harness(tmp_path, monkeypatch, sid="nested-stop", exec_code=True)
    delayed = "import time; time.sleep(2); open('late', 'w').write('bad')"
    (tmp_path / "probe.py").write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c',%r], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, "
        "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))\n"
        "print('Probe has started',flush=True)\n"
        "open('ready','w').write('ready')\n"
        "time.sleep(30)\n" % delayed, encoding="utf-8")
    h.cancelled = lambda: (tmp_path / "ready").exists()
    h.provider = _ScriptProvider([Completion(tool_calls=[
        ToolCall("script", "execute_code", {"code": "print(bash('python probe.py'))", "timeout": 60}),
    ])])
    try:
        res = h.run("nested", "run the probe")
        assert res.canceled
        assert not _paired(res.messages)
        assert "Probe has started" in _results(res.messages)["script"]
        assert sessions.recovery_state("nested-stop") is None
        time.sleep(2.2)
        assert not (tmp_path / "late").exists()
    finally:
        h.memory.close(); h.recorder.close()
def test_interrupted_batch_keeps_done_work_and_closes_the_rest(tmp_path, monkeypatch):
    """One killed tool must not erase the tool before it or forgive the one after."""
    from harness.tools import WriteFileTool

    h = _harness(tmp_path, monkeypatch, sid="batch")
    h.provider = _ScriptProvider([_batch(("w1", "one.txt"), ("w2", "two.txt"),
                                         ("w3", "three.txt"))])
    original = WriteFileTool.run

    def run(self, args, ctx):
        if args.get("path") == "two.txt":
            raise KeyboardInterrupt()
        return original(self, args, ctx)
    monkeypatch.setattr(WriteFileTool, "run", run)
    try:
        res = h.run("test", "write three files", consolidate=False)
    finally:
        h.memory.close(); h.recorder.close()

    assert res.canceled and res.stop_reason == "canceled" and not res.success
    assert "interrupted by user" in res.error
    # 1. the completed action is still in the conversation, with its real result
    assert (tmp_path / "one.txt").exists()
    results = _results(res.messages)
    assert "one.txt" in results["w1"] and not results["w1"].startswith("ERROR")
    # 2. the interrupted action is neither claimed done nor claimed untouched
    assert "UNKNOWN" in results["w2"] and "stopped while it was running" in results["w2"]
    assert not (tmp_path / "two.txt").exists()
    # 3. the queued action says exactly that it never started
    assert results["w3"] == "CANCELED: run stopped before execution"
    assert not (tmp_path / "three.txt").exists()
    # 4. the thread a provider would see next is protocol-valid
    assert _paired(res.messages) == {}

    # 5. the durable journal fences the unknown effect on the interrupted call
    state = sessions.recovery_state("batch")
    assert state["state"] == "external_action"
    assert state["recovery_required"] is True and state["auto_resumable"] is False
    assert state["detail"]["tool_call_id"] == "w2"
    assert _results(sessions.load("batch")["messages"]) == results


def test_saving_the_interrupted_transcript_cannot_clear_the_fence(tmp_path, monkeypatch):
    """The surface save that follows every run is not evidence about the world."""
    from harness.tools import WriteFileTool

    h = _harness(tmp_path, monkeypatch, sid="fenced")
    h.provider = _ScriptProvider([_batch(("w1", "kept.txt"), ("w2", "unknown.txt"))])

    def run(self, args, ctx):
        raise KeyboardInterrupt()
    monkeypatch.setattr(WriteFileTool, "run", run)
    try:
        res = h.run("test", "write two files", consolidate=False)
    finally:
        h.memory.close(); h.recorder.close()

    # exactly what cli/tui/repl/webapp do after a run, with no special flag
    sessions.save("fenced", res.messages, project="lifecycle",
                  cwd=str(tmp_path), answer=res.answer or "")

    state = sessions.recovery_state("fenced")
    assert state["recovery_required"] is True
    assert state["detail"]["tool_call_id"] == "w1"
    assert sessions.load("fenced")["last_answer"] == res.answer
    # and the CLI's own resume guard still refuses to continue this thread
    assert cli.recovery_notice("fenced", state).count("collie recovery") == 2


def test_save_still_retires_a_settled_checkpoint(tmp_path, monkeypatch):
    """Only an UNCERTAIN boundary survives a transcript save."""
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.checkpoint("settled", [{"role": "user", "content": "go"}],
                        run_id="r1", state="tool_complete",
                        detail={"tool_name": "read_file", "tool_call_id": "c1"})
    sessions.save("settled", [{"role": "user", "content": "go"},
                              {"role": "assistant", "content": "done"}],
                  answer="done")
    assert sessions.recovery_state("settled") is None


def test_interrupted_builtin_read_stays_auto_resumable_and_backfills(tmp_path,
                                                                     monkeypatch):
    """A host-attested read cannot have changed anything: do not fence the thread."""
    from harness.tools import ReadFileTool

    (tmp_path / "a.txt").write_text("evidence", encoding="utf-8")
    h = _harness(tmp_path, monkeypatch, sid="read")
    h.provider = _ScriptProvider([Completion(tool_calls=[
        ToolCall("r1", "read_file", {"path": "a.txt"}),
        ToolCall("r2", "read_file", {"path": "a.txt"})], stop_reason="tool_use")])
    monkeypatch.setattr(ReadFileTool, "run",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        res = h.run("test", "read a.txt twice", consolidate=False)
    finally:
        h.memory.close(); h.recorder.close()

    state = sessions.recovery_state("read")
    assert state["state"] == "executing_tool"
    assert state["recovery_required"] is False and state["auto_resumable"] is True
    results = _results(res.messages)
    assert "stopped before returning a result" in results["r1"]
    assert results["r2"] == "CANCELED: run stopped before execution"
    # a resumed load neither replays the read nor duplicates its closure
    reloaded = sessions.load("read")["messages"]
    assert _paired(reloaded) == {}
    assert len([m for m in reloaded if m.get("tool_call_id") == "r1"]) == 1


def test_the_next_turn_in_the_same_process_continues_from_real_progress(tmp_path,
                                                                        monkeypatch):
    """A surface holding the canceled result may run again without repairing it."""
    from harness.tools import ReadFileTool

    (tmp_path / "a.txt").write_text("evidence", encoding="utf-8")
    h = _harness(tmp_path, monkeypatch, sid="continue")
    h.provider = _ScriptProvider([Completion(tool_calls=[
        ToolCall("r1", "read_file", {"path": "a.txt"})], stop_reason="tool_use")])
    monkeypatch.setattr(ReadFileTool, "run",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        stopped = h.run("test", "read a.txt", consolidate=False)
        monkeypatch.undo()
        seen = {}

        def answer(messages):
            seen["messages"] = messages
            return Completion(text="I re-read the file and it says evidence.")
        h.provider = _ScriptProvider([answer])
        second = h.run("test", "now finish", history=stopped.messages,
                       consolidate=False)
    finally:
        h.memory.close(); h.recorder.close()

    assert second.answer == "I re-read the file and it says evidence."
    assert not second.canceled and not second.error
    # the model was handed a valid thread that still contains the stop
    assert _paired(seen["messages"]) == {}
    assert "stopped before returning a result" in json.dumps(
        _results(seen["messages"]))
    # and the journal moved on rather than staying fenced on the killed read
    assert sessions.recovery_state("continue") is None


def test_partial_streamed_text_survives_an_interrupt_during_generation(tmp_path,
                                                                       monkeypatch):
    """Ctrl-C while tokens are arriving keeps what the user already watched."""
    shown = []
    h = _harness(tmp_path, monkeypatch, sid="stream")
    h.stream_cb = shown.append

    class Interrupting(_ScriptProvider):
        def complete(self, system, messages, schemas, on_text=None):
            self.calls += 1
            for piece in ("Found the bug in ", "parser.py line 40"):
                on_text(piece)
            raise KeyboardInterrupt()

    h.provider = Interrupting([])
    try:
        res = h.run("test", "find the bug", consolidate=False)
    finally:
        h.memory.close(); h.recorder.close()

    assert res.canceled and "interrupted by user" in res.error
    assert res.answer == "Found the bug in parser.py line 40\n\n_[stopped by user]_"
    assert shown == ["Found the bug in ", "parser.py line 40"]
    # nothing ran, so nothing needs reconciling: the thread stays resumable
    assert sessions.recovery_state("stream") is None
    assert sessions.load("stream")["messages"][-1]["content"] == res.answer


def test_a_completed_tool_result_is_never_replaced_by_a_closure(tmp_path, monkeypatch):
    """The closure pass may only touch calls that have no result at all."""
    from harness.tools import WriteFileTool

    h = _harness(tmp_path, monkeypatch, sid="once")
    h.provider = _ScriptProvider([_batch(("w1", "one.txt")),
                                  _batch(("w2", "two.txt"))])
    original = WriteFileTool.run

    def run(self, args, ctx):
        if args.get("path") == "two.txt":
            raise KeyboardInterrupt()
        return original(self, args, ctx)
    monkeypatch.setattr(WriteFileTool, "run", run)
    try:
        res = h.run("test", "write both files", consolidate=False)
    finally:
        h.memory.close(); h.recorder.close()

    closures = [m for m in res.messages if m.get("tool_call_id") == "w1"]
    assert len(closures) == 1 and "one.txt" in closures[0]["content"]
    assert res.tool_calls == 1        # the killed call is not counted as executed


def test_an_interrupted_delegated_run_stops_the_run_the_user_is_watching(tmp_path,
                                                                         monkeypatch):
    """Ctrl-C inside a sub-agent must not be absorbed as 'the subtask was canceled'."""
    from harness.tools import ReadFileTool

    (tmp_path / "a.txt").write_text("evidence", encoding="utf-8")
    h = _harness(tmp_path, monkeypatch, sid="delegated", delegate=True)
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall("d1", "delegate", {
            "task": "inspect a.txt"})], stop_reason="tool_use"),
        Completion(tool_calls=[ToolCall("r1", "read_file", {"path": "a.txt"})],
                   stop_reason="tool_use"),
        Completion(text="the parent kept going after the child was stopped"),
    ])
    monkeypatch.setattr(ReadFileTool, "run",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        res = h.run("test", "delegate the inspection", consolidate=False)
    finally:
        h.memory.close(); h.recorder.close()

    assert res.canceled and "interrupted by user" in res.error
    assert "kept going" not in (res.answer or "")
    results = _results(res.messages)
    assert "UNKNOWN" not in results["d1"]      # delegate is host-attested effect-free
    assert "stopped before returning a result" in results["d1"]
    assert _paired(res.messages) == {}


# --------------------------------------------------------------------------- #
# interactive surfaces: REPL and TUI
# --------------------------------------------------------------------------- #
class _FakeHarness:
    """A harness whose turn does durable work and then loses the process."""

    def __init__(self, sid, cwd, effect=None):
        self.sid = sid
        self.cwd = cwd
        self.project = "lifecycle"
        self.effect = effect
        self.checkpoint_scope = ""
        self.approve = None
        self.steering = None
        self.emit = None
        self.stream_cb = None
        self.provider = type("P", (), {"name": "mock", "model": "mock-1"})()
        self.memory = type("M", (), {"close": lambda self: None,
                                     "set_block": lambda self, *a, **k: None})()
        self.recorder = type("R", (), {"close": lambda self: None})()
        self.calls = 0

    def run(self, task_id, line, **kw):
        self.calls += 1
        return self.effect(self, line, kw)


def _write_progress(h, state, detail):
    """Persist the same journal shape a real interrupted turn leaves behind."""
    sessions.checkpoint(
        h.sid, [{"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "name": "write_file", "args": {"path": "x.txt"}}]}],
        project="lifecycle", cwd=h.cwd, run_id="r1", turn=0,
        state=state, detail=detail)


def _repl_args(tmp_path, **over):
    values = dict(resume=None, cont=False, cwd=str(tmp_path), provider="mock",
                  model=None, project="lifecycle", mode=None, goal=None)
    values.update(over)
    return type("Args", (), values)()


def _drive_repl(monkeypatch, tmp_path, lines, effect):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    typed = iter(lines)
    monkeypatch.setattr("builtins.input", lambda *a: next(typed))
    holder = {}

    def make(cwd, **kw):
        holder["h"] = _FakeHarness(sessions.latest() or "", cwd, effect)
        return holder["h"]
    monkeypatch.setattr(cli, "make_harness", make)
    monkeypatch.setattr(cli, "default_gate", lambda *a, **k: None)
    monkeypatch.setattr(cli, "resolve_turn_decision", lambda *a, **k: type(
        "D", (), {"model": "mock-1", "effort": "low", "intent": "build",
                  "quality": "balanced", "verification": "auto"})())
    monkeypatch.setattr(cli, "apply_turn_decision", lambda *a, **k: None)
    monkeypatch.setattr(cli, "turn_decision_receipt", lambda *a, **k: {"ok": True})
    from harness import approve
    monkeypatch.setattr(approve, "tty_approver", lambda **k: None)
    rc = cli.cmd_repl(_repl_args(tmp_path))
    return rc, holder.get("h")


def test_repl_interrupt_keeps_durable_progress_and_stops_at_the_fence(
        monkeypatch, tmp_path, capsys):
    """An interrupt outside run() must not roll the conversation back to before it."""
    def effect(h, line, kw):
        h.sid = h.checkpoint_scope.split(":", 1)[1]
        _write_progress(h, "executing_tool",
                        {"tool_name": "write_file", "tool_call_id": "c1"})
        raise KeyboardInterrupt()

    rc, h = _drive_repl(monkeypatch, tmp_path,
                        ["publish the release", "and now do the next bit", "/exit"],
                        effect)
    out = capsys.readouterr().out
    assert rc == 0
    # the second prompt was refused rather than run over an unknown effect
    assert h.calls == 1
    assert "turn interrupted — kept the 2 messages already recorded" in out
    assert "collie recovery reconcile" in out
    assert sessions.recovery_state(h.sid)["recovery_required"] is True
    # the exit line does not invite a resume the resume guard would refuse
    assert "cannot be resumed yet" in out and "collie repl --resume" not in out


def test_repl_interrupt_over_a_safe_boundary_keeps_going(monkeypatch, tmp_path, capsys):
    """A replay-safe stop returns the user to a usable prompt, not a dead session."""
    def effect(h, line, kw):
        h.sid = h.checkpoint_scope.split(":", 1)[1]
        if h.calls == 1:
            _write_progress(h, "calling_model", {"attempt": 1})
            raise KeyboardInterrupt()
        result = type("R", (), {"messages": kw.get("history") or [],
                                "answer": "finished", "error": ""})()
        return result

    rc, h = _drive_repl(monkeypatch, tmp_path, ["start", "continue", "/exit"], effect)
    out = capsys.readouterr().out
    assert rc == 0 and h.calls == 2
    assert "turn interrupted" in out and "collie recovery reconcile" not in out
    assert "finished" in out


def test_repl_reports_a_failed_transcript_save_and_stops(monkeypatch, tmp_path, capsys):
    """A journal that cannot be written is not a turn that quietly continues."""
    def effect(h, line, kw):
        h.sid = h.checkpoint_scope.split(":", 1)[1]
        return type("R", (), {"messages": [{"role": "user", "content": line}],
                              "answer": "did it", "error": ""})()

    def refuse(*a, **kw):
        raise ValueError("session journal is unreadable")
    monkeypatch.setattr(sessions, "save", refuse)
    rc, h = _drive_repl(monkeypatch, tmp_path, ["do it", "do more", "/exit"], effect)
    out = capsys.readouterr().out
    assert rc == 0 and h.calls == 1
    assert "session transcript could not be persisted" in out


def _drive_tui(monkeypatch, tmp_path, lines, effect):
    from harness import tui
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(tui, "_HAVE_RICH", False)
    typed = iter(lines)
    monkeypatch.setattr("builtins.input", lambda *a: next(typed))
    holder = {}

    def make(cwd, **kw):
        holder["h"] = _FakeHarness("", cwd, effect)
        return holder["h"]
    monkeypatch.setattr(cli, "make_harness", make)
    monkeypatch.setattr(cli, "default_gate", lambda *a, **k: None)
    monkeypatch.setattr(cli, "resolve_turn_decision", lambda *a, **k: type(
        "D", (), {"model": "mock-1", "effort": "low", "intent": "build",
                  "quality": "balanced", "verification": "auto"})())
    monkeypatch.setattr(cli, "apply_turn_decision", lambda *a, **k: None)
    monkeypatch.setattr(cli, "turn_decision_receipt", lambda *a, **k: {"ok": True})
    rc = tui.run_tui(str(tmp_path), "mock", None, project="lifecycle")
    return rc, holder.get("h")


def test_tui_interrupt_reloads_progress_instead_of_replaying_the_old_history(
        monkeypatch, tmp_path, capsys):
    """The TUI used to continue from its pre-turn history, losing completed edits."""
    def effect(h, line, kw):
        h.sid = h.checkpoint_scope.split(":", 1)[1]
        _write_progress(h, "executing_tool",
                        {"tool_name": "write_file", "tool_call_id": "c1"})
        raise KeyboardInterrupt()

    rc, h = _drive_tui(monkeypatch, tmp_path,
                       ["ship the release", "keep going", "/exit"], effect)
    out = capsys.readouterr().out
    assert rc == 0 and h.calls == 1
    assert "kept the 2 messages already recorded" in out
    assert "collie recovery reconcile" in out
    assert "cannot be resumed yet" in out and "collie tui --resume" not in out


def test_tui_interrupt_at_a_safe_boundary_returns_to_a_working_prompt(
        monkeypatch, tmp_path, capsys):
    def effect(h, line, kw):
        h.sid = h.checkpoint_scope.split(":", 1)[1]
        if h.calls == 1:
            _write_progress(h, "model_complete", {"stop_reason": "tool_use"})
            raise KeyboardInterrupt()
        return type("R", (), {"messages": kw.get("history") or [],
                              "answer": "finished", "error": ""})()

    rc, h = _drive_tui(monkeypatch, tmp_path, ["start", "continue", "/exit"], effect)
    out = capsys.readouterr().out
    assert rc == 0 and h.calls == 2
    assert "collie recovery reconcile" not in out


# --------------------------------------------------------------------------- #
# verification after a stop
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# `collie run`: the same endings on the terminal
# --------------------------------------------------------------------------- #
def _cli_run_args(tmp_path, **over):
    import argparse

    base = dict(task="fix the parser", cwd=str(tmp_path), provider="mock", model=None,
                project="lifecycle", mode=None, persona=None, goal=None, resume=None,
                cont=False, stream_json=False, json=True, print=False,
                web_search=False, intent="build", quality="balanced",
                verification="required", effort=None, speed=None,
                verify_command="pytest -q", runner=None)
    base.update(over)
    return argparse.Namespace(**base)


def _pin_native_cli(monkeypatch, tmp_path, run):
    """Route a `collie run` to a fake native harness without probing anything."""
    import argparse

    from harness import router, runner_registry, runner_slice
    from harness.router import RunDecision

    state = tmp_path / "state"
    (state / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "data" / "memory.db"), str(state / "data" / "runs.db"),
        str(state / "data" / "dashboard.html"), str(state / "data" / "sandbox")))
    from harness import settings
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(router, "resolve_run_decision", lambda *a, **kw: RunDecision(
        provider="mock", model="mock-coder-v1", effort="default", speed="standard",
        billing_multiplier=1.0, intent="build", quality="balanced",
        verification="required", workspace="current", strategy="single",
        route_kind="code", complexity="simple"))
    from harness.runner_specs import RunnerProbe
    monkeypatch.setattr(runner_registry, "probe_all", lambda keys=None, **kw: {
        "collie": RunnerProbe(
            key="collie", installed=True, executable_path="", version="0.test",
            login="n/a", billing_class="local", billing_mode="local",
            probed_at=1_800_000_000.0,
            capabilities=runner_registry.COLLIE_CAPABILITIES.to_dict())})
    monkeypatch.setattr(runner_slice, "run_adhoc", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("the native path must not build a worker")))
    monkeypatch.setattr(cli, "default_gate", lambda *a, **k: None)
    monkeypatch.setattr(cli, "configure_run_options", lambda h, **k: None)
    monkeypatch.setattr(cli, "dash", type("D", (), {"build": staticmethod(
        lambda *a, **k: None)})())

    class FakeHarness:
        def __init__(self):
            self.memory = self.recorder = type(
                "Closer", (), {"close": lambda self: None,
                               "finish_run": lambda self, res: None,
                               "set_block": lambda self, *a, **k: None})()
            self.provider = argparse.Namespace(actual_speed="standard")
            self.checkpoint_scope = ""
            self.settled = []

        def settle_run_memory(self, res, passed, evidence, source=""):
            self.settled.append((passed, source))
            return {"promoted": 0, "rejected": 0}

        def run(self, task_id, task, history=None, **kw):
            return run(self, task_id, task, history)

    built = []
    monkeypatch.setattr(cli, "make_harness",
                        lambda *a, **kw: built.append(FakeHarness()) or built[-1])
    return built


def test_cli_stopped_run_reports_the_stop_without_starting_a_check(monkeypatch,
                                                                   tmp_path, capsys):
    """The terminal reaches the same ending as the Web surface, on the same evidence."""
    from harness import verification
    from harness.recorder import RunResult

    monkeypatch.setattr(verification, "run_verification_command",
                        lambda *a, **k: pytest.fail("a stopped run started the check"))

    def run(h, task_id, task, history):
        sid = h.checkpoint_scope.split(":", 1)[1]
        sessions.checkpoint(
            sid, [{"role": "user", "content": task},
                  {"role": "assistant", "content": "", "tool_calls": [
                      {"id": "c1", "name": "browser_click", "args": {}}]},
                  {"role": "tool", "tool_call_id": "c1", "name": "browser_click",
                   "content": "INTERRUPTED: this call stopped while it was running."}],
            project="lifecycle", cwd=str(tmp_path), run_id="r1", state="external_action",
            detail={"tool_name": "browser_click", "tool_call_id": "c1"})
        return RunResult(task_id=task_id, harness="collie", model="mock-coder-v1",
                         canceled=True, error="interrupted by user",
                         answer="submitted the form\n\n_[stopped by user]_",
                         messages=[{"role": "user", "content": task}])

    _pin_native_cli(monkeypatch, tmp_path, run)
    code = cli.cmd_run(_cli_run_args(tmp_path))
    payload = json.loads(capsys.readouterr().out.strip())

    assert code == 1
    assert payload["canceled"] is True and payload["stop_reason"] == "canceled"
    assert payload["completed"] is False
    assert payload["verification_evidence"]["executed"] is False
    assert payload["answer"].startswith("submitted the form")
    # the fence the run left behind survived the transcript save and is reported
    assert payload["recovery_required"] is True
    assert payload["recovery"]["detail"]["tool_name"] == "browser_click"
    assert sessions.recovery_state(payload["session"])["recovery_required"] is True

    # ...and the next `--continue` refuses this thread before routing anything
    monkeypatch.setattr(cli, "make_harness", lambda *a, **kw: pytest.fail(
        "a fenced thread started another run"))
    assert cli.cmd_run(_cli_run_args(tmp_path, cont=True)) == 2
    refusal = json.loads(capsys.readouterr().out.strip())
    assert refusal["recovery_required"] is True


def test_cli_clean_run_still_runs_and_settles_its_required_check(monkeypatch,
                                                                 tmp_path, capsys):
    """Blocking the check after a stop must not weaken the ordinary Required path."""
    from harness import verification
    from harness.recorder import RunResult

    calls = []
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda command, cwd, **kw: calls.append(command) or {
                            "command": command, "exit_code": 0, "passed": True,
                            "command_passed": True, "output": "2 passed",
                            "freshness": "fresh", "source": "user"})

    def run(h, task_id, task, history):
        return RunResult(task_id=task_id, harness="collie", model="mock-coder-v1",
                         answer="fixed it", messages=[
                             {"role": "user", "content": task},
                             {"role": "assistant", "content": "fixed it"}])

    built = _pin_native_cli(monkeypatch, tmp_path, run)
    assert cli.cmd_run(_cli_run_args(tmp_path)) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    assert calls == ["pytest -q"]
    assert payload["verification_evidence"]["passed"] is True
    assert payload["recovery_required"] is False
    assert built[0].settled == [(True, "cli_verification")]


def _web_isolate(monkeypatch, tmp_path):
    from harness import settings, webapp

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "memory.db"), str(state / "runs.db"),
        str(state / "dashboard.html"), str(state / "sandbox")))
    monkeypatch.setattr(webapp.Handler, "_notify_done", lambda *a, **kw: None)
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear()
        webapp.Handler._cancel_events.clear()


class _WebHarness:
    """Just enough native harness for the Web run path to reach its endings."""

    def __init__(self, gate, outcome, before_return=None):
        from types import SimpleNamespace

        closer = SimpleNamespace(close=lambda: None,
                                 finish_run=lambda res: None,
                                 set_block=lambda *a, **k: None)
        self.gate = gate
        self.composer = SimpleNamespace(identity="")
        self.memory = self.recorder = closer
        self.provider = SimpleNamespace(name="mock", model="mock-1")
        self.mode, self.force_edit, self.max_turns = "act", True, 20
        self._max_turns_hard_cap = None
        self.self_verify, self.verify_max = False, 2
        self.verify_gate, self.require_assert = False, False
        self.checkpoint_scope = ""
        self.outcome = outcome
        self.before_return = before_return
        self.settled = []

    def settle_run_memory(self, res, passed, evidence, source=""):
        self.settled.append((passed, source))
        return {"promoted": 0, "rejected": 0}

    def run(self, task_id, message, history=None, **kwargs):
        if self.before_return is not None:
            self.before_return(self)
        return self.outcome(message)


def _web_result(message, **over):
    from types import SimpleNamespace

    values = dict(
        answer="partial work", error="", model="mock-1", prefix_tokens=0,
        input_tokens=0, output_tokens=0, total_tokens=0, turns=1, tool_calls=1,
        wall_ms=1, cost_usd=0.0, verified=False, canceled=False,
        turns_exhausted=False, budget_exhausted=False, stop_reason="completed",
        edited=True, model_calls=1, parent_run_id=None, success=True,
        messages=[{"role": "user", "content": message},
                  {"role": "assistant", "content": "partial work"}])
    values.update(over)
    return SimpleNamespace(**values)


def _run_web(monkeypatch, tmp_path, harness, session="web-stop", **extra_qs):
    from harness import webapp

    monkeypatch.setattr(cli, "make_harness",
                        lambda *args, **kwargs: harness(kwargs.get("gate")))
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    qs = {"q": ["fix the parser"], "session": [session], "intent": ["build"],
          "quality": ["balanced"], "verification": ["required"],
          "verify_command": ["pytest -q"], "verify_source": ["user"],
          "workspace": ["current"], "strategy": ["single"]}
    qs.update(extra_qs)
    webapp.Handler._serve_stream(fake, qs)
    return events, next(data for kind, data in events if kind == "done")


@pytest.mark.parametrize("over,expected", [
    ({"canceled": True, "error": "canceled by user", "stop_reason": "canceled",
      "success": False}, "canceled"),
    ({"error": "provider exploded", "stop_reason": "error", "success": False}, "error"),
])
def test_web_required_check_is_not_launched_after_a_stop(monkeypatch, tmp_path,
                                                          over, expected):
    """A stopped run must not start a fresh host command it cannot interpret."""
    from harness import verification

    _web_isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda *a, **k: pytest.fail("a stopped run started the check"))
    events, done = _run_web(
        monkeypatch, tmp_path,
        lambda gate: _WebHarness(gate, lambda msg: _web_result(msg, **over)))

    assert done["stop_reason"] == expected and done["completed"] is False
    assert done["verification_evidence"]["executed"] is False
    assert done["verification_evidence"]["passed"] is False
    assert done["verification_evidence"]["command"] == "pytest -q"
    assert done["verification_evidence"]["freshness"] == "not_run"
    assert done["verification_evidence"]["output"] == \
        done["verification_evidence"]["skipped_reason"]
    # the partial answer is preserved rather than replaced by the stop
    assert done["answer"] == "partial work"
    evidence_event = next(data for kind, data in events
                          if kind == "verification_evidence")
    assert evidence_event["evidence"]["executed"] is False
    saved = sessions.load("web-stop")
    assert saved["run_receipts"][-1]["verified"] is False
    assert saved["run_receipts"][-1]["stop_reason"] == expected


def test_web_passing_check_cannot_turn_a_canceled_run_into_a_success(monkeypatch,
                                                                     tmp_path):
    """Even if a check is somehow run, the recorded outcome stays the stop."""
    from harness import verification

    _web_isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(verification, "run_verification_command", lambda *a, **k: {
        "command": "pytest -q", "passed": True, "command_passed": True,
        "exit_code": 0, "executed": True, "ran_after_last_edit": True})
    events, done = _run_web(
        monkeypatch, tmp_path,
        lambda gate: _WebHarness(gate, lambda msg: _web_result(
            msg, canceled=True, error="canceled by user", stop_reason="canceled",
            success=False)))

    assert done["canceled"] is True and done["stop_reason"] == "canceled"
    assert done["completed"] is False
    assert sessions.load("web-stop")["run_receipts"][-1]["verified"] is False


def test_web_clean_run_still_runs_its_required_check(monkeypatch, tmp_path):
    """The stop guard must not quietly disable Required verification."""
    from harness import verification

    _web_isolate(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda command, cwd, **k: calls.append(command) or {
                            "command": command, "passed": True, "command_passed": True,
                            "exit_code": 0, "executed": True,
                            "ran_after_last_edit": True})
    harnesses = []

    def build(gate):
        harnesses.append(_WebHarness(gate, _web_result))
        return harnesses[-1]
    events, done = _run_web(monkeypatch, tmp_path, build)

    assert calls == ["pytest -q"]
    assert done["stop_reason"] == "completed" and done["completed"] is True
    assert harnesses[0].settled == [(True, "web_verification")]
    assert sessions.load("web-stop")["run_receipts"][-1]["verified"] is True


def test_web_native_stop_keeps_its_fence_across_save_and_the_next_request(
        monkeypatch, tmp_path):
    """The Web save must not make an unknown effect look replay-safe."""
    from harness import verification, webapp

    _web_isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda *a, **k: pytest.fail("a stopped run started the check"))

    def fence(h):
        sessions.checkpoint(
            "web-fence", [{"role": "user", "content": "fix the parser"}],
            project="web", cwd=str(tmp_path), run_id="r1", turn=0,
            state="external_action",
            detail={"tool_name": "browser_click", "tool_call_id": "c1"})

    events, done = _run_web(
        monkeypatch, tmp_path,
        lambda gate: _WebHarness(gate, lambda msg: _web_result(
            msg, canceled=True, error="canceled by user", stop_reason="canceled",
            success=False), before_return=fence),
        session="web-fence")

    assert done["recovery_required"] is True
    assert done["recovery"]["detail"]["tool_name"] == "browser_click"
    state = sessions.recovery_state("web-fence")
    assert state["recovery_required"] is True
    assert sessions.load("web-fence")["last_answer"] == "partial work"

    # the next request on this thread is refused before any model or tool runs
    second = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: second.append((kind, data))
    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: pytest.fail(
        "a fenced thread started another run"))
    webapp.Handler._serve_stream(fake, {"q": ["carry on"], "session": ["web-fence"]})
    assert second[-1][0] == "done" and second[-1][1]["recovery_required"] is True


def test_skipped_verification_evidence_is_receipt_shaped_and_honest():
    evidence = cli.skipped_verification_evidence("pytest -q", "detected", "run was stopped")
    assert evidence["executed"] is False and evidence["passed"] is False
    assert evidence["command_passed"] is False and evidence["exit_code"] is None
    assert evidence["freshness"] == "not_run"
    assert evidence["command"] == "pytest -q" and evidence["source"] == "detected"
    assert "run was stopped" in evidence["output"]


@pytest.mark.parametrize("canceled,error,expected", [
    (True, "", "stopped before it finished"),
    (False, "provider exploded", "ended with an error"),
    (False, "", ""),
])
def test_only_a_clean_result_may_start_a_host_check(canceled, error, expected):
    result = type("R", (), {"canceled": canceled, "error": error})()
    assert expected in cli.stopped_before_verification(result)
    assert bool(cli.stopped_before_verification(result)) == bool(expected)
