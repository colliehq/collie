"""A continued thread must execute in the directory shown by its saved session."""
import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import cli, sessions, tui
from harness.recorder import RunResult


@pytest.fixture
def workspace_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    roots = [tmp_path / "original", tmp_path / "elsewhere"]
    for root in roots:
        root.mkdir()
        (root / "fact.txt").write_text(root.name, encoding="utf-8")
    for root, sid in zip(roots, ("original-thread", "other-thread")):
        sessions.save(sid, [{"role": "user", "content": "remember this project"}],
                      cwd=str(root), project="demo")
    monkeypatch.chdir(roots[1])
    monkeypatch.setattr(tui, "_HAVE_RICH", False)
    monkeypatch.setattr(cli, "apply_turn_decision", lambda *a, **kw: None)
    made, reads = [], []

    class Harness:
        def __init__(self, cwd):
            self.cwd = cwd
            self.provider = SimpleNamespace(name="mock", model="mock", actual_speed="standard")
            self.memory = self.recorder = SimpleNamespace(close=lambda: None)
            self.emit = None

        def run(self, _task, text, history=None, **kwargs):
            # Exercise the tool's path resolution instead of asserting a copied field.
            from harness.tools import ReadFileTool, ToolCtx
            answer = ReadFileTool().run(
                {"path": "fact.txt"}, ToolCtx(cwd=self.cwd, project="demo", memory=None))
            reads.append(answer)
            return RunResult(answer=answer, model="mock", messages=list(history or []) + [
                {"role": "user", "content": text}, {"role": "assistant", "content": answer}])

    def make(cwd, **kwargs):
        made.append(cwd)
        return Harness(cwd)

    monkeypatch.setattr(cli, "make_harness", make)
    return roots, made, reads


def args(**overrides):
    data = dict(cwd=None, provider="mock", model=None, project="demo", mode=None,
                persona=None, goal=None, resume="original-thread", cont=False,
                task="read fact.txt", stream_json=False, json=True, print=False,
                web_search=False, intent="build", quality="balanced", verification="auto",
                effort=None, speed=None, verify_command=None, runner=None)
    return SimpleNamespace(**dict(data, **overrides))


@pytest.mark.parametrize("surface", ["run", "repl", "tui"])
def test_resume_from_another_terminal_directory_reads_saved_project(
        workspace_flow, monkeypatch, surface):
    roots, made, reads = workspace_flow
    inputs = iter(("read fact.txt", "/exit"))
    monkeypatch.setattr(builtins, "input", lambda *a, **kw: next(inputs))
    monkeypatch.setattr(tui, "_read_line", lambda *a, **kw: next(inputs))
    if surface == "run":
        assert cli.cmd_run(args()) == 0
    elif surface == "repl":
        assert cli.cmd_repl(args()) == 0
    else:
        assert tui.run_tui(str(roots[1]), "mock", "mock", resume="original-thread") == 0
    assert made == [str(roots[0])]
    assert len(reads) == 1 and "original" in reads[0] and "elsewhere" not in reads[0]


def test_tui_resume_switches_tool_registry_and_gate_to_new_workspace(workspace_flow, monkeypatch):
    roots, made, reads = workspace_flow
    inputs = iter(("read fact.txt", "/resume original-thread", "read fact.txt", "/exit"))
    monkeypatch.setattr(tui, "_read_line", lambda *a, **kw: next(inputs))
    assert tui.run_tui(str(roots[1]), "mock", "mock", resume="other-thread") == 0
    assert made == [str(roots[1]), str(roots[0])]
    assert "elsewhere" in reads[0] and "original" in reads[1]


def test_explicit_relocation_survives_a_later_resume(workspace_flow):
    roots, made, reads = workspace_flow
    assert cli.cmd_run(args(cwd=str(roots[1]))) == 0
    assert cli.cmd_run(args()) == 0
    assert made == [str(roots[1]), str(roots[1])]
    saved = sessions.load("original-thread")
    assert saved["handoffs"][-1]["previous_cwd"] == str(roots[0])
    assert saved["messages"][0]["content"] == "remember this project"


@pytest.mark.parametrize("surface", ["run", "repl", "tui"])
def test_missing_saved_workspace_never_falls_back_to_current_project(
        workspace_flow, monkeypatch, surface):
    roots, made, reads = workspace_flow
    missing = roots[0] / "no-longer-present"
    sessions.save("missing-project", [], cwd=str(missing))
    if surface == "run":
        code = cli.cmd_run(args(resume="missing-project"))
    elif surface == "repl":
        code = cli.cmd_repl(args(resume="missing-project"))
    else:
        code = tui.run_tui(str(roots[1]), "mock", "mock", resume="missing-project")
    assert code == 2 and not made and not reads


@pytest.mark.parametrize("surface", ["run", "repl", "tui"])
def test_typo_in_session_id_is_an_error_not_an_empty_thread(workspace_flow, surface):
    roots, made, reads = workspace_flow
    if surface == "run":
        code = cli.cmd_run(args(resume="typo"))
    elif surface == "repl":
        code = cli.cmd_repl(args(resume="typo"))
    else:
        code = tui.run_tui(str(roots[1]), "mock", "mock", resume="typo")
    assert code == 2 and not made and not reads
