"""Regression locks for the 2026-08 core correctness audit."""
from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _append_worker(store, sid, prefix, count):
    os.environ["COLLIE_SESSIONS_DIR"] = store
    from harness import sessions
    for i in range(count):
        sessions.append_exchange(sid, "%s-q%d" % (prefix, i), "%s-a%d" % (prefix, i))


def test_undo_is_scoped_to_canonical_repository(monkeypatch, tmp_path):
    from harness import checkpoint as ck

    journal, a, b = tmp_path / "journal", tmp_path / "a", tmp_path / "b"
    for repo in (a, b):
        repo.mkdir(); (repo / ".git").mkdir()
    monkeypatch.setattr(ck, "_DIR", str(journal)); ck._STACKS.clear()
    pa, pb = a / "same.txt", b / "same.txt"
    pa.write_text("a0", encoding="utf-8"); pb.write_text("b0", encoding="utf-8")
    ck.record("demo", str(pa), cwd=str(a)); pa.write_text("a1", encoding="utf-8")
    ck.record("demo", str(pb), cwd=str(b)); pb.write_text("b1", encoding="utf-8")

    ctx = types.SimpleNamespace(project="demo", cwd=str(b))
    assert "restored" in ck.UndoTool().run({}, ctx)
    assert pb.read_text(encoding="utf-8") == "b0"
    assert pa.read_text(encoding="utf-8") == "a1", "repo B undo must not touch repo A"


def test_legacy_undo_journal_filters_cross_repo_entries(monkeypatch, tmp_path):
    from harness import checkpoint as ck

    journal, a, b = tmp_path / "journal", tmp_path / "a", tmp_path / "b"
    journal.mkdir(); a.mkdir(); b.mkdir(); (a / ".git").mkdir(); (b / ".git").mkdir()
    pa, pb = a / "x", b / "x"
    legacy = [{"path": str(pa), "existed": True, "prev": "a"},
              {"path": str(pb), "existed": True, "prev": "b"}]
    (journal / "demo.json").write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(ck, "_DIR", str(journal)); ck._STACKS.clear()
    listed = ck.UndoTool().run({"action": "list"}, types.SimpleNamespace(project="demo", cwd=str(a)))
    assert str(pa) in listed and str(pb) not in listed


def test_undo_can_be_narrowed_to_a_web_session(monkeypatch, tmp_path):
    from harness import checkpoint as ck

    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    journal = tmp_path / "journal"
    monkeypatch.setattr(ck, "_DIR", str(journal)); ck._STACKS.clear()
    one, two = repo / "one", repo / "two"
    one.write_text("old-one", encoding="utf-8"); two.write_text("old-two", encoding="utf-8")
    ck.record("web:s1", str(one), cwd=str(repo)); one.write_text("new-one", encoding="utf-8")
    ck.record("web:s2", str(two), cwd=str(repo)); two.write_text("new-two", encoding="utf-8")
    ctx = types.SimpleNamespace(project="web", checkpoint_scope="web:s1", cwd=str(repo))
    assert "restored" in ck.UndoTool().run({}, ctx)
    assert one.read_text(encoding="utf-8") == "old-one"
    assert two.read_text(encoding="utf-8") == "new-two"


def test_session_append_is_lossless_across_threads_and_processes(monkeypatch, tmp_path):
    from harness import sessions

    store, sid = str(tmp_path / "sessions"), "concurrent"
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", store)
    threads = [threading.Thread(target=sessions.append_exchange,
                                args=(sid, "t%d-q" % i, "t%d-a" % i)) for i in range(12)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(10)
    assert all(not thread.is_alive() for thread in threads)

    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_worker, args=(store, sid, "p%d" % i, 3))
             for i in range(3)]
    for proc in procs: proc.start()
    for proc in procs: proc.join(20)
    assert [p.exitcode for p in procs] == [0, 0, 0]
    messages = sessions.load(sid)["messages"]
    users = {m["content"] for m in messages if m.get("role") == "user"}
    assert len(messages) == 2 * (12 + 9)
    assert {"t%d-q" % i for i in range(12)} <= users
    assert {"p%d-q%d" % (p, i) for p in range(3) for i in range(3)} <= users


def test_concurrent_full_saves_merge_divergent_exchanges(monkeypatch, tmp_path):
    from harness import sessions

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sid = "merge"
    prefix = [{"role": "user", "content": "root"}, {"role": "assistant", "content": "ok"}]
    sessions.save(sid, prefix)
    barrier = threading.Barrier(2)

    def writer(name):
        barrier.wait()
        sessions.save(sid, prefix + [{"role": "user", "content": name},
                                     {"role": "assistant", "content": name + "!"}])

    ts = [threading.Thread(target=writer, args=(x,)) for x in ("left", "right")]
    for t in ts: t.start()
    for t in ts: t.join(5)
    contents = [m["content"] for m in sessions.load(sid)["messages"]]
    assert "left" in contents and "right" in contents


def test_pack_apply_removes_deleted_paths_but_preserves_skipped_trees(tmp_path):
    from harness import pack

    src, dst = tmp_path / "winner", tmp_path / "real"
    src.mkdir(); dst.mkdir(); (src / "keep.txt").write_text("new", encoding="utf-8")
    (dst / "keep.txt").write_text("old", encoding="utf-8")
    (dst / "deleted.txt").write_text("gone", encoding="utf-8")
    (dst / "deleted-dir").mkdir(); (dst / "deleted-dir" / "x").write_text("x", encoding="utf-8")
    (dst / ".git").mkdir(); (dst / ".git" / "sentinel").write_text("git", encoding="utf-8")
    pack._copy_back(str(src), str(dst))
    assert (dst / "keep.txt").read_text(encoding="utf-8") == "new"
    assert not (dst / "deleted.txt").exists() and not (dst / "deleted-dir").exists()
    assert (dst / ".git" / "sentinel").exists()


def test_pack_apply_preserves_nested_skipped_trees(tmp_path):
    from harness import pack

    src, dst = tmp_path / "winner", tmp_path / "real"
    app_src, app_dst = src / "packages" / "app", dst / "packages" / "app"
    app_src.mkdir(parents=True); app_dst.mkdir(parents=True)
    (app_src / "keep.txt").write_text("new", encoding="utf-8")
    for skipped in ("node_modules", ".venv", "build"):
        tree = app_dst / skipped / "nested"
        tree.mkdir(parents=True)
        (tree / "sentinel").write_text(skipped, encoding="utf-8")

    pack._copy_back(str(src), str(dst))

    assert (app_dst / "keep.txt").read_text(encoding="utf-8") == "new"
    for skipped in ("node_modules", ".venv", "build"):
        assert (app_dst / skipped / "nested" / "sentinel").read_text(encoding="utf-8") == skipped


def test_errored_pack_attempts_are_never_eligible():
    from harness.pack import select

    attempts = [{"idx": 0, "error": "model crashed", "verified": True, "check_pass": True},
                {"idx": 1, "error": "", "verified": False, "check_pass": True, "turns": 2}]
    assert select(attempts, True)[0] == 1
    assert select([attempts[0]], False)[0] is None


def test_pack_apply_failure_is_reported_and_json_cli_is_nonzero(monkeypatch, tmp_path, capsys):
    from harness import cli, pack

    monkeypatch.setattr(pack, "run_pack", lambda *a, **k: {
        "winner": 0, "applied": False, "apply_error": "OSError: denied", "reason": "apply failed",
        "attempts": [], "n": 1, "total_cost_usd": 0, "roster": ["mock"]})
    args = types.SimpleNamespace(task="x", cwd=str(tmp_path), provider="mock", model=None,
                                 roster="", n=1, check=None, apply=True, parallel=1, json=True)
    assert cli.cmd_pack(args) == 1
    assert json.loads(capsys.readouterr().out)["apply_error"]


def test_run_pack_propagates_apply_failure_in_result(monkeypatch, tmp_path):
    from harness import catalog, cli, pack, scratch

    class Result:
        answer, verified, turns, error, cost_usd = "ok", True, 1, "", 0.0
    class Harness:
        memory = recorder = None
        def run(self, *args, **kwargs): return Result()
    closer = types.SimpleNamespace(close=lambda: None)
    Harness.memory = Harness.recorder = closer
    monkeypatch.setattr(catalog, "preflight", lambda members: [])
    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: Harness())
    monkeypatch.setattr(scratch, "isolate_harness", lambda *a, **k: None)
    monkeypatch.setattr(pack, "_copy_back", lambda *a, **k:
                        (_ for _ in ()).throw(PermissionError("read only")))
    res = pack.run_pack("x", str(tmp_path), n=1, apply=True, provider="mock")
    assert res["winner"] == 0 and not res["applied"]
    assert "PermissionError" in res["apply_error"] and "apply failed" in res["reason"]


def test_pack_quality_presets_and_context_reach_every_candidate(monkeypatch, tmp_path):
    from harness import catalog, cli, pack, scratch

    monkeypatch.delenv("COLLIE_MAX_COST", raising=False)
    monkeypatch.delenv("COLLIE_MAX_TOTAL_TOKENS", raising=False)
    closer = types.SimpleNamespace(close=lambda: None)
    seen = []

    class Result:
        answer, verified, turns, error, cost_usd = "ok", True, 1, "", 0.0

    class Harness:
        memory = recorder = closer
        mode, force_edit, max_turns = "act", True, 50
        _max_turns_hard_cap = None
        self_verify, verify_max, verify_gate, require_assert = False, 2, False, False

        def run(self, task_id, task, history=None, **kwargs):
            seen.append((self.max_turns, task, history))
            return Result()

    monkeypatch.setattr(catalog, "preflight", lambda members: [])
    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: Harness())
    monkeypatch.setattr(scratch, "isolate_harness", lambda *a, **k: None)
    message = [{"type": "text", "text": "inspect"},
               {"type": "image", "media_type": "image/png", "data": "AAAA"}]
    history = [{"role": "user", "content": "before"},
               {"role": "assistant", "content": "prior"}]

    pack.run_pack(message, str(tmp_path), n=1, provider="mock", quality="balanced",
                  history=history)
    pack.run_pack(message, str(tmp_path), n=1, provider="mock", quality="thorough",
                  history=history)

    assert [row[0] for row in seen] == [40, 50]
    assert all(row[1] == message and row[2] == history for row in seen)


def test_pack_budget_is_one_serial_aggregate_not_n_copies(monkeypatch, tmp_path):
    from harness import catalog, cli, pack, scratch
    from harness.providers import Usage

    monkeypatch.setenv("COLLIE_MAX_TOTAL_TOKENS", "1")
    monkeypatch.setenv("COLLIE_MAX_COST", "0")
    closer = types.SimpleNamespace(close=lambda: None)
    made = []

    class Result:
        answer, verified, turns, error, cost_usd = "ok", True, 1, "", 0.0

    class Harness:
        memory = recorder = closer
        mode, force_edit, max_turns = "act", True, 50
        _max_turns_hard_cap = None
        self_verify, verify_max, verify_gate, require_assert = False, 2, False, False
        provider = types.SimpleNamespace(model="mock")

        def run(self, *args, **kwargs):
            self.shared_budget.account(self.provider.model, Usage(input_tokens=1))
            return Result()

    def make(*args, **kwargs):
        made.append(1)
        return Harness()

    monkeypatch.setattr(catalog, "preflight", lambda members: [])
    monkeypatch.setattr(cli, "make_harness", make)
    monkeypatch.setattr(scratch, "isolate_harness", lambda *a, **k: None)

    result = pack.run_pack("x", str(tmp_path), n=3, parallel=3, provider="mock")

    assert len(made) == 1
    assert result["parallel"] == 1 and result["requested_parallel"] == 3
    assert result["budget_exhausted"] and result["budget_tokens"] == 1
    assert [a.get("error") for a in result["attempts"]] == ["", "pack budget exhausted",
                                                              "pack budget exhausted"]


def test_pack_real_harness_accounts_provider_usage_once_across_candidates(monkeypatch, tmp_path):
    from harness import catalog, cli, pack, scratch
    from harness.providers import Completion, ModelProvider, Usage

    monkeypatch.setenv("COLLIE_MAX_TOTAL_TOKENS", "2")
    monkeypatch.setenv("COLLIE_MAX_COST", "0")
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "state"))
    monkeypatch.setattr(catalog, "preflight", lambda members: [])
    monkeypatch.setattr(scratch, "isolate_harness", lambda *a, **k: None)
    original_make = cli.make_harness
    made = []

    class Provider(ModelProvider):
        name, model, reports_cache = "usage-test", "mock", False

        def complete(self, *args, **kwargs):
            return Completion(text="done", stop_reason="end_turn", usage=Usage(input_tokens=1))

    def make(cwd, **kwargs):
        h = original_make(cwd, provider="mock", project=kwargs.get("project", "p"), embed="hash")
        h.provider = Provider()
        made.append(h)
        return h

    monkeypatch.setattr(cli, "make_harness", make)

    result = pack.run_pack("x", str(tmp_path), n=3, parallel=3, provider="mock")

    assert len(made) == 2
    assert result["budget_tokens"] == 2 and result["budget_exhausted"]
    assert result["attempts"][2]["error"] == "pack budget exhausted"


def test_shared_budget_blocks_retry_and_final_synthesis(monkeypatch, tmp_path):
    from harness import cli
    from harness.pack import _PackBudget
    from harness.providers import Completion, ModelProvider, ToolCall, Usage

    monkeypatch.setattr(cli, "DATA", str(tmp_path / "state"))

    class RetryProvider(ModelProvider):
        name, model, reports_cache = "retry-budget", "mock", False

        def __init__(self):
            self.calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            return Completion(text="overloaded", stop_reason="error", error_status=529,
                              error_detail="overloaded", usage=Usage(input_tokens=1))

    retry_h = cli.make_harness(str(tmp_path), provider="mock", project="retry", embed="hash")
    retry_h.provider = RetryProvider()
    retry_h.retry_base = 0
    retry_h.shared_budget = _PackBudget(max_tokens=1)
    retry_result = retry_h.run("retry", "work", consolidate=False)
    assert retry_h.provider.calls == 1 and retry_result.error
    retry_h.memory.close(); retry_h.recorder.close()

    class ToolProvider(ModelProvider):
        name, model, reports_cache = "synthesis-budget", "mock", False

        def __init__(self):
            self.calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            return Completion(tool_calls=[ToolCall("b", "bash", {"command": "echo ok"})],
                              stop_reason="tool_use", usage=Usage(input_tokens=1))

    synth_h = cli.make_harness(str(tmp_path), provider="mock", project="synth", embed="hash")
    synth_h.provider = ToolProvider()
    synth_h.max_turns = 1
    synth_h.shared_budget = _PackBudget(max_tokens=1)
    synth_result = synth_h.run("synth", "work", consolidate=False)
    assert synth_h.provider.calls == 1
    assert "budget ceiling reached" in synth_result.answer
    synth_h.memory.close(); synth_h.recorder.close()


def test_pack_budget_accounts_cost_across_models():
    from harness.pack import _PackBudget
    from harness.providers import Usage

    budget = _PackBudget(max_cost=0.01)
    budget.account("claude-opus-5", Usage(output_tokens=1000))
    snap = budget.snapshot()
    assert snap["exhausted"] and snap["cost_usd"] > 0.01


def test_loop_command_is_nonzero_when_until_never_passes(monkeypatch, tmp_path):
    from harness import cli, plat

    result = types.SimpleNamespace(answer="progress", error="")
    closer = types.SimpleNamespace(close=lambda: None, set_block=lambda *a, **k: None)
    harness = types.SimpleNamespace(run=lambda *a, **k: result, memory=closer, recorder=closer)
    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: harness)
    monkeypatch.setattr(plat, "shell_argv", lambda cmd: ([sys.executable, "-c", "raise SystemExit(1)"], False))
    args = types.SimpleNamespace(cwd=str(tmp_path), provider="mock", model=None, project="p",
                                 goal=None, task="work", max=2, until="never")
    assert cli.cmd_loop(args) == 1


def test_loop_command_accumulates_failures_across_iterations(monkeypatch, tmp_path):
    from harness import cli

    results = iter([types.SimpleNamespace(answer="", error="provider failed"),
                    types.SimpleNamespace(answer="recovered", error="")])
    closer = types.SimpleNamespace(close=lambda: None, set_block=lambda *a, **k: None)
    harness = types.SimpleNamespace(run=lambda *a, **k: next(results),
                                    memory=closer, recorder=closer)
    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: harness)
    args = types.SimpleNamespace(cwd=str(tmp_path), provider="mock", model=None, project="p",
                                 goal=None, task="work", max=2, until=None)
    assert cli.cmd_loop(args) == 1


def test_loop_passing_until_does_not_promote_a_failed_run(monkeypatch, tmp_path):
    from harness import cli, plat

    result = types.SimpleNamespace(answer="partial", error="provider failed",
                                   messages=[], verified=False)
    settlements = []
    closer = types.SimpleNamespace(close=lambda: None, set_block=lambda *a, **k: None,
                                   finish_run=lambda *_a, **_k: None)
    harness = types.SimpleNamespace(
        run=lambda *a, **k: result, memory=closer, recorder=closer,
        settle_run_memory=lambda res, passed, evidence, source="":
            settlements.append((passed, evidence, source)))
    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: harness)
    monkeypatch.setattr(
        plat, "shell_argv",
        lambda cmd: ([sys.executable, "-c", "raise SystemExit(0)"], False))
    args = types.SimpleNamespace(cwd=str(tmp_path), provider="mock", model=None, project="p",
                                 goal=None, task="work", max=1, until="passes")

    assert cli.cmd_loop(args) == 0
    assert settlements and settlements[0][0] is False
    assert result.verified is False


def test_cmd_run_returns_nonzero_and_scopes_undo_to_session(monkeypatch, tmp_path):
    from harness import cli, sessions

    seen = []
    closer = types.SimpleNamespace(close=lambda: None, set_block=lambda *a, **k: None)

    class FakeHarness:
        checkpoint_scope = ""
        memory = recorder = closer

        def run(self, *args, **kwargs):
            seen.append(self.checkpoint_scope)
            return types.SimpleNamespace(answer="", error="provider failed", messages=[])

    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: FakeHarness())
    monkeypatch.setattr(sessions, "new_id", lambda: "cli-session")
    monkeypatch.setattr(sessions, "save", lambda *a, **k: None)
    args = types.SimpleNamespace(cwd=str(tmp_path), provider="mock", model=None, project="p",
                                 mode=None, web_search=False, persona=None, goal=None, resume=None,
                                 cont=False, stream_json=False, json=False, print=True, task="work")
    assert cli.cmd_run(args) == 1
    assert seen == ["session:cli-session"]


def test_repl_updates_checkpoint_scope_when_starting_new_session(monkeypatch, tmp_path):
    import builtins
    from harness import cli, sessions

    scopes = []
    closer = types.SimpleNamespace(close=lambda: None, set_block=lambda *a, **k: None)

    class FakeHarness:
        checkpoint_scope = ""
        memory = recorder = closer

        def run(self, *args, **kwargs):
            scopes.append(self.checkpoint_scope)
            return types.SimpleNamespace(answer="ok", error="", messages=[])

    session_ids = iter(("repl-one", "repl-two"))
    inputs = iter(("first", "/new", "second", "/exit"))
    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: FakeHarness())
    monkeypatch.setattr(sessions, "new_id", lambda: next(session_ids))
    monkeypatch.setattr(sessions, "save", lambda *a, **k: None)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: next(inputs))
    args = types.SimpleNamespace(cwd=str(tmp_path), provider="mock", model=None, project="p",
                                 mode=None, resume=None, cont=False, goal=None)
    assert cli.cmd_repl(args) == 0
    assert scopes == ["session:repl-one", "session:repl-two"]


def test_web_pack_appends_history_and_finishes_registry(monkeypatch, tmp_path):
    from harness import pack, sessions, webapp

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    captured = {}
    def fake_pack(*args, **kwargs):
        captured["task"] = args[0]
        captured.update(kwargs)
        return {
            "winner": 0, "answer": "winner", "reason": "verified", "applied": False,
                "attempts": [{"idx": 0, "turns": 2, "verified": True},
                             {"idx": 1, "turns": 3, "verified": False}], "n": 2,
            "total_cost_usd": 0, "canceled": False, "apply_error": ""}
    monkeypatch.setattr(pack, "run_pack", fake_pack)
    sid = "pack-history"
    sessions.append_exchange(sid, "before", "prior")
    img_id = webapp.Handler._img_put("image/png", "AAAA")
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()
    webapp.Handler._serve_stream(fake, {"q": ["pack now"], "imgs": [img_id], "session": [sid],
                                        "strategy": ["pack"], "quality": ["thorough"],
                                        "verification": ["required"], "n": ["2"],
                                        "check": ["python -m pytest -q"]})
    contents = [m["content"] for m in sessions.load(sid)["messages"]]
    assert contents[:2] == ["before", "prior"] and contents[-1] == "winner"
    assert isinstance(contents[2], list) and contents[2][0]["text"] == "pack now"
    assert contents[2][1] == {"type": "image", "media_type": "image/png", "data": "AAAA"}
    run = webapp.Handler._runs_snapshot()[0]
    assert run["state"] == "done" and run["turns"] == 2
    assert events[-1][0] == "done" and not events[-1][1]["error"]
    assert captured["quality"] == "thorough" and captured["verification"] == "required"
    assert captured["task"] == contents[2]
    assert [m["content"] for m in captured["history"]] == ["before", "prior"]
    assert callable(captured["gate_factory"])


def test_loop_cancel_stops_before_next_tool(monkeypatch, tmp_path):
    from harness import cli
    from harness.providers import Completion, ModelProvider, ToolCall, Usage

    class Provider(ModelProvider):
        name, model = "cancel-test", "cancel-test"
        def complete(self, *args, **kwargs):
            return Completion(tool_calls=[
                ToolCall("one", "write_file", {"path": "one.txt", "content": "1"}),
                ToolCall("two", "write_file", {"path": "two.txt", "content": "2"})],
                stop_reason="tool_use", usage=Usage())

    h = cli.make_harness(str(tmp_path), provider="mock", project="cancel-test")
    h.provider = Provider(); h.cancelled = lambda: (tmp_path / "one.txt").exists()
    try:
        result = h.run("cancel", "write both", consolidate=False)
    finally:
        h.memory.close(); h.recorder.close()
    assert result.canceled and result.error == "canceled by user"
    assert (tmp_path / "one.txt").exists() and not (tmp_path / "two.txt").exists()
    assert "stopped by user" in result.answer
    tool_ids = [m.get("tool_call_id") for m in result.messages if m.get("role") == "tool"]
    assert tool_ids == ["one", "two"] and "CANCELED" in result.messages[-2]["content"]


def test_cancel_http_contract_is_authenticated_and_observable():
    from harness import webapp

    server, _ = webapp.bind_server(0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()
    run_id = webapp.Handler._run_begin("cancel-api", "work", ROOT)
    body = json.dumps({"session": "cancel-api"}).encode()

    def post(token):
        for attempt in range(10):
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/run/cancel?token=%s" % (port, token), data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)
            except (OSError, urllib.error.URLError):
                if attempt == 9:
                    raise
                time.sleep(.05)

    try:
        assert post("wrong")[0] == 403
        code, result = post(webapp.TOKEN)
        assert code == 200 and result["status"] == "cancel_requested" and result["run"] == run_id
        assert webapp.Handler._run_cancelled("cancel-api", run_id)
        webapp.Handler._run_end("cancel-api", run_id=run_id)
        assert webapp.Handler._runs_snapshot()[0]["state"] == "canceled"
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


def test_scheduler_failed_drive_is_released_for_retry(tmp_path):
    from harness.actions import ActionStore
    from harness.jobs import JobStore
    from harness.scheduler import FIRED_W, Scheduler

    actions = ActionStore(str(tmp_path / "actions.db")); jobs = JobStore(str(tmp_path / "jobs.db"))
    sched = Scheduler(actions, jobs, db_path=str(tmp_path / "waits.db"))
    wid = sched.schedule("", "nonce", fire_at=1, now=0)
    calls = []
    sched.executor.drive = lambda nonce: (_ for _ in ()).throw(RuntimeError("crash"))
    assert sched.tick(now=2) == 0
    assert sched.pending_waits()[0]["wait_id"] == wid
    sched.executor.drive = lambda nonce: calls.append(nonce)
    assert sched.tick(now=3) == 1 and calls == ["nonce"]
    assert sched.db.execute("SELECT state FROM waits WHERE wait_id=?", (wid,)).fetchone()[0] == FIRED_W
    sched.close(); actions.close(); jobs.close()


def test_scheduler_startup_reconciles_expired_lease(tmp_path):
    from harness.actions import ActionStore
    from harness.jobs import JobStore
    from harness.scheduler import CLAIMED_W, Scheduler

    actions = ActionStore(str(tmp_path / "actions.db")); jobs = JobStore(str(tmp_path / "jobs.db"))
    db = str(tmp_path / "waits.db")
    first = Scheduler(actions, jobs, db_path=db)
    wid = first.schedule("", "nonce", fire_at=1, now=0)
    first.db.execute("UPDATE waits SET state=?,lease_until=1 WHERE wait_id=?", (CLAIMED_W, wid))
    first.db.commit(); first.close()
    second = Scheduler(actions, jobs, db_path=db)
    assert [w["wait_id"] for w in second.pending_waits()] == [wid]
    second.close(); actions.close(); jobs.close()


def test_windows_process_discovery_uses_cim(monkeypatch):
    from harness import cli, plat

    rows = [{"ProcessId": 101, "CommandLine": "pythonw -m harness.webapp --port 8787"},
            {"ProcessId": 102, "CommandLine": "python -m harness.cli uninstall --yes"},
            {"ProcessId": 103, "CommandLine": "python unrelated.py"},
            {"ProcessId": 104,
             "CommandLine": "python -c \"print('harness.webapp docs')\""}]
    calls = []
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kw:
                        (calls.append(args) or types.SimpleNamespace(returncode=0,
                                                                    stdout=json.dumps(rows), stderr="")))
    assert cli._collie_procs() == [("101", rows[0]["CommandLine"])]
    assert calls[0][0].lower() == "powershell.exe" and "Get-CimInstance" in calls[0][-1]


def test_posix_process_discovery_finds_bare_collie_executable(monkeypatch):
    from harness import cli, plat

    ps = ("  PID COMMAND\n"
          "  101 /usr/local/bin/collie\n"
          "  102 /usr/bin/python other.py /tmp/collie\n"
          "  103 /usr/bin/python3 -u /usr/local/bin/collie web\n"
          "  104 /usr/bin/python app.py -m harness.webapp\n")
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(cli.os, "getpid", lambda: 999)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k:
                        types.SimpleNamespace(returncode=0, stdout=ps, stderr=""))
    assert cli._collie_procs() == [
        ("101", "/usr/local/bin/collie"),
        ("103", "/usr/bin/python3 -u /usr/local/bin/collie web"),
    ]


def test_uninstall_reports_material_removal_failure(monkeypatch, tmp_path, capsys):
    from harness import cli, plat
    import shutil

    home = tmp_path / "home"; victim = home / ".collie" / "data"
    victim.mkdir(parents=True); (victim / "keep").write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.setattr(cli, "_collie_procs", lambda: [])
    monkeypatch.setattr(plat, "is_macos", lambda: False)
    real = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda path, *a, **k:
                        (_ for _ in ()).throw(PermissionError("busy")) if str(path) == str(victim)
                        else real(path, *a, **k))
    rc = cli.cmd_uninstall(types.SimpleNamespace(yes=True, keep_config=False))
    assert rc == 1 and victim.exists()
    assert "incomplete" in capsys.readouterr().err


def test_shell_entrypoint_is_lf_and_attributes_pin_it():
    data = open(os.path.join(ROOT, "tests", "run_all.sh"), "rb").read()
    attrs = open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8").read()
    assert b"\r\n" not in data and "*.sh text eol=lf" in attrs


def test_session_ids_are_rejected_not_aliased(monkeypatch, tmp_path):
    from harness import sessions

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    sessions.append_exchange("victim", "private", "answer")
    assert sessions.load("../../victim") is None
    assert not sessions.delete("C:overshadow")
    assert sessions.load(None) is None and sessions.load(123) is None
    assert sessions.load("victim")["messages"][0]["content"] == "private"


def test_undo_restores_exact_binary_bytes_and_keeps_failed_snapshot(monkeypatch, tmp_path):
    from harness import checkpoint as ck

    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    monkeypatch.setattr(ck, "_DIR", str(tmp_path / "journal")); ck._STACKS.clear()
    p = repo / "opaque.bin"; original = b"\xff\xfe\x00OLD\r\n"
    p.write_bytes(original); ck.record("binary", str(p), cwd=str(repo)); p.write_bytes(b"NEW")
    ctx = types.SimpleNamespace(project="binary", cwd=str(repo))
    assert "restored" in ck.UndoTool().run({}, ctx) and p.read_bytes() == original

    p.write_bytes(b"before"); ck.record("retry", str(p), cwd=str(repo)); p.write_bytes(b"after")
    real_replace = ck.os.replace
    monkeypatch.setattr(ck.os, "replace", lambda src, dst:
                        (_ for _ in ()).throw(PermissionError("busy")))
    retry_ctx = types.SimpleNamespace(project="retry", cwd=str(repo))
    assert "ERROR restoring" in ck.UndoTool().run({}, retry_ctx)
    assert "opaque.bin" in ck.UndoTool().run({"action": "list"}, retry_ctx)
    monkeypatch.setattr(ck.os, "replace", real_replace)
    assert "restored" in ck.UndoTool().run({}, retry_ctx) and p.read_bytes() == b"before"


def test_scheduler_surfaces_uncertain_inflight_action_instead_of_claiming_it_dropped(tmp_path):
    from harness.actions import ActionStore, EXECUTING
    from harness.jobs import JobStore, NEEDS_YOU
    from harness.scheduler import Scheduler

    actions = ActionStore(str(tmp_path / "actions.db")); jobs = JobStore(str(tmp_path / "jobs.db"))
    jobs.create("j", "uncertain", leash={"may": ["note.*"]})
    nonce = actions.propose("note.append", {"file": "x", "text": "x"}, job_id="j")
    actions.db.execute("UPDATE pending_actions SET state=? WHERE nonce=?", (EXECUTING, nonce))
    actions.db.commit()
    sched = Scheduler(actions, jobs, db_path=str(tmp_path / "waits.db"))
    sched.schedule("j", nonce, fire_at=1, now=0)
    assert sched.tick(now=2) == 1
    assert jobs.get("j").state == NEEDS_YOU and "unknown" in jobs.get("j").result
    sched.close(); actions.close(); jobs.close()
