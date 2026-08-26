"""A roster spreads the attempts over different backends without losing track of which is which."""
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import pack
from harness import runner_slice
from harness.recorder import RunResult
from harness.runner_specs import HarnessDecision, RunnerReceipt


def test_roster_entries_parse_without_mangling_ollama_tags():
    assert pack.normalize_roster(None, "anthropic", "claude-opus-5") == \
        [("anthropic", "claude-opus-5")]
    # A model belongs to ITS provider: a bare backend takes that backend's default, not "m".
    assert pack.normalize_roster(["codex-oauth"], "anthropic", "m") == [("codex-oauth", None)]
    assert pack.normalize_roster(["deepseek:deepseek-reasoner"], "anthropic", "m") == \
        [("deepseek", "deepseek-reasoner")]
    # An ollama tag is itself colon-separated — split once, or the model becomes "qwen2.5-coder".
    assert pack.normalize_roster(["ollama:qwen2.5-coder:7b"], "x", None) == \
        [("ollama", "qwen2.5-coder:7b")]
    assert pack.normalize_roster([("groq", None), ["openai", "gpt-4o-mini"]], "x", None) == \
        [("groq", None), ("openai", "gpt-4o-mini")]


def _stub_backends(monkeypatch, seen, delay_first=0.0):
    """Replace the harness so the roster wiring is testable without spending a model call."""
    import time

    class Res:
        def __init__(self, idx):
            self.answer, self.verified, self.turns = "answer %d" % idx, False, 1
            self.error, self.cost_usd = "", 0.0

    class FakeHarness:
        def __init__(self, provider, model):
            self.provider_name, self.model_name = provider, model
            self.memory = self.recorder = self

        def close(self):
            pass

        def run(self, task_id, task, **kw):
            idx = int(task_id.replace("pack", ""))
            if delay_first and idx == 0:
                time.sleep(delay_first)          # submitted first, finishes last
            seen.append((idx, self.provider_name, self.model_name))
            return Res(idx)

    import harness.catalog as catalog
    import harness.cli as cli
    import harness.scratch as scratch
    monkeypatch.setattr(pack, "_isolate", lambda cwd: tempfile.mkdtemp(prefix="fakepack_"))
    monkeypatch.setattr(cli, "make_harness",
                        lambda iso, provider=None, model=None, **kw: FakeHarness(provider, model))
    monkeypatch.setattr(scratch, "isolate_harness", lambda h, read_project: None)
    monkeypatch.setattr(catalog, "preflight", lambda members: [])


def test_roster_is_assigned_round_robin_and_recorded_per_attempt(monkeypatch):
    _stub_backends(monkeypatch, [])
    res = pack.run_pack("t", tempfile.mkdtemp(), n=4,
                        roster=["groq", "deepseek:deepseek-reasoner"])
    assert [a["provider"] for a in res["attempts"]] == ["groq", "deepseek", "groq", "deepseek"]
    assert [a["model"] for a in res["attempts"]] == [None, "deepseek-reasoner",
                                                     None, "deepseek-reasoner"]
    assert res["roster"] == ["groq", "deepseek:deepseek-reasoner"]
    # The winner has to be attributable, or a mixed roster answers nothing.
    assert res["winner_provider"] in ("groq", "deepseek")


def test_a_roster_longer_than_n_is_never_silently_truncated(monkeypatch):
    _stub_backends(monkeypatch, [])
    res = pack.run_pack("t", tempfile.mkdtemp(), n=2,
                        roster=["groq", "openai", "deepseek", "ollama"])
    assert res["n"] == 4, "every named backend must actually run"
    assert sorted(a["provider"] for a in res["attempts"]) == \
        ["deepseek", "groq", "ollama", "openai"]


def test_parallel_attempts_keep_their_order_and_their_backend(monkeypatch):
    seen = []
    _stub_backends(monkeypatch, seen, delay_first=0.4)      # attempt 0 finishes last
    res = pack.run_pack("t", tempfile.mkdtemp(), n=3, parallel=3,
                        roster=["groq", "openai", "deepseek"])
    assert [a["idx"] for a in res["attempts"]] == [0, 1, 2], "reported in attempt order"
    assert [a["provider"] for a in res["attempts"]] == ["groq", "openai", "deepseek"]
    assert res["parallel"] == 3
    assert seen and seen[0][0] != 0, "attempt 0 was delayed, so it must not have finished first"


def test_emit_is_serialized_across_workers(monkeypatch):
    _stub_backends(monkeypatch, [])
    overlaps, active, lock = [], [], threading.Lock()

    def emit(i, rec):
        with lock:
            active.append(i)
            overlaps.append(len(active))
        active.pop()

    pack.run_pack("t", tempfile.mkdtemp(), n=4, parallel=4, emit=emit, roster=["groq", "openai"])
    assert max(overlaps) == 1, "the caller's emit was written against a sequential loop"


def test_cleanup_deletes_only_the_owned_attempt_directory(monkeypatch, tmp_path):
    """A test double may return any attempt root; cleanup must never derive its parent."""
    _stub_backends(monkeypatch, [])
    parent = tmp_path / "parent"
    attempt = parent / "attempt"
    attempt.mkdir(parents=True)
    sentinel = parent / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(pack, "_isolate", lambda _cwd: str(attempt))
    real_rmtree = pack.shutil.rmtree
    removed = []

    def guarded_rmtree(path, *args, **kwargs):
        removed.append(os.path.abspath(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pack.shutil, "rmtree", guarded_rmtree)
    pack.run_pack("t", str(tmp_path), n=1, roster=["groq"])
    assert removed == [os.path.abspath(attempt)]
    assert sentinel.exists(), "the attempt's parent and sibling data must survive cleanup"


def test_cleanup_failure_is_returned_as_durable_outcome_evidence(monkeypatch, tmp_path):
    _stub_backends(monkeypatch, [])
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    monkeypatch.setattr(pack, "_isolate", lambda _cwd: str(attempt))
    monkeypatch.setattr(
        pack.shutil, "rmtree",
        lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("directory busy")))

    result = pack.run_pack("t", str(tmp_path), n=1, roster=["groq"])

    assert result["cleanup_errors"][0]["idx"] == 0
    assert "directory busy" in result["cleanup_errors"][0]["error"]
    assert "could not be removed" in result["reason"]


def test_isolation_copy_failure_reports_retained_partial_workspace(monkeypatch, tmp_path):
    partial = tmp_path / "partial-attempt"

    def make_partial(**_kwargs):
        partial.mkdir()
        return str(partial)

    monkeypatch.setattr(pack.tempfile, "mkdtemp", make_partial)
    monkeypatch.setattr(
        pack.shutil, "copytree",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("copy failed")))
    monkeypatch.setattr(
        pack.shutil, "rmtree",
        lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("directory busy")))

    try:
        pack._isolate(str(tmp_path))
    except RuntimeError as exc:
        detail = str(exc)
    else:  # pragma: no cover - makes a silent orphan an explicit test failure
        raise AssertionError("retained partial workspace was not reported")

    assert str(partial) in detail
    assert "directory busy" in detail


def test_pack_budget_rejects_nonfinite_configuration_and_usage(monkeypatch):
    monkeypatch.setenv("COLLIE_MAX_COST", "NaN")
    try:
        pack._PackBudget.from_env()
    except ValueError as exc:
        assert "finite" in str(exc)
    else:
        raise AssertionError("NaN cost ceiling silently disabled the Pack budget")

    budget = pack._PackBudget(max_cost=1, max_tokens=100)
    assert budget.account_values(tokens=1.5, cost_usd=float("inf")) == (
        "cost_usd", "tokens")
    assert budget.exceeded() is True


def test_a_tree_is_copied_only_when_its_attempt_starts(monkeypatch):
    """Copying all N up front makes a sequential pack wait through N copytrees of the whole repo
    before the first model call."""
    order = []
    _stub_backends(monkeypatch, [])
    real_isolate = pack._isolate

    def watched(cwd):
        order.append("copy")
        return real_isolate(cwd)

    monkeypatch.setattr(pack, "_isolate", watched)

    import harness.cli as cli
    base = cli.make_harness

    def make(iso, provider=None, model=None, **kw):
        h = base(iso, provider=provider, model=model, **kw)
        inner = h.run

        def run(task_id, task, **kwargs):
            order.append("run")
            return inner(task_id, task, **kwargs)
        h.run = run
        return h

    monkeypatch.setattr(cli, "make_harness", make)
    pack.run_pack("t", tempfile.mkdtemp(), n=3)
    assert order == ["copy", "run"] * 3, order


def test_one_failed_copy_costs_one_candidate_not_the_run(monkeypatch):
    _stub_backends(monkeypatch, [])
    real_isolate = pack._isolate
    calls = {"n": 0}

    def flaky(cwd):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("no space left on device")
        return real_isolate(cwd)

    monkeypatch.setattr(pack, "_isolate", flaky)
    res = pack.run_pack("t", tempfile.mkdtemp(), n=3)
    assert len(res["attempts"]) == 3
    assert "isolation failed" in res["attempts"][1]["error"]
    assert [a["error"] for a in res["attempts"]][::2] == ["", ""], "the others still ran"


def test_nothing_is_applied_when_no_attempt_has_a_tree(monkeypatch):
    _stub_backends(monkeypatch, [])
    monkeypatch.setattr(pack, "_isolate", lambda cwd: (_ for _ in ()).throw(OSError("nope")))
    res = pack.run_pack("t", tempfile.mkdtemp(), n=2, apply=True)
    assert res["applied"] is False, "there was no tree to copy back"


def test_preflight_still_refuses_before_spending_attempts(monkeypatch):
    import harness.catalog as catalog
    _stub_backends(monkeypatch, [])
    monkeypatch.setattr(catalog, "preflight", lambda members: ["openai: set OPENAI_API_KEY"])
    res = pack.run_pack("t", tempfile.mkdtemp(), n=3, roster=["openai", "groq"])
    assert res["winner"] is None and res["attempts"] == []
    assert "OPENAI_API_KEY" in res["reason"]


def _external_decision(key="codex-exec"):
    return HarnessDecision(
        runner=key, source="user", credential_family="codex",
        billing_class="subscription_allowance", billing_mode="subscription",
        reasons=("pinned by user",), rejected={}, candidates=(), fallback_chain=(),
        probe={"key": key}, probe_digest="d" * 8)


def test_external_worker_runs_every_isolated_candidate_and_records_receipt(monkeypatch,
                                                                           tmp_path):
    """Pack owns isolation/checking; the selected worker owns candidate generation."""
    roots, calls = [], []

    def isolate(_cwd):
        root = tempfile.mkdtemp(prefix="external_pack_")
        roots.append(root)
        return root

    monkeypatch.setattr(pack, "_isolate", isolate)
    monkeypatch.setattr(pack, "_init_external_git", lambda root: calls.append(("git", root)))

    receipt = RunnerReceipt.from_dict({
        "runner": "codex-exec", "settled": True, "usage_known": True,
        "billing_class": "subscription_allowance", "billing_mode": "subscription",
    })

    def run_adhoc(decision, task, workspace, **kwargs):
        idx = len([row for row in calls if row[0] == "run"])
        calls.append(("run", workspace, kwargs.get("model"), kwargs.get("task_id"),
                      kwargs.get("provider")))
        result = RunResult(task_id="pack%d" % idx, harness="codex-exec",
                           model="gpt-worker", provider="codex", turns=1,
                           total_tokens=100 + idx, cost_usd=0.01, success=True,
                           answer="candidate %d" % idx, error="", messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, receipt)
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", run_adhoc)
    res = pack.run_pack("fix it", str(tmp_path), n=2,
                        runner_decision=_external_decision(), runner_model="gpt-worker")

    assert [row[0] for row in calls] == ["git", "run", "git", "run"]
    assert all(row[4] == "codex" for row in calls if row[0] == "run")
    assert [a["runner"] for a in res["attempts"]] == ["codex-exec", "codex-exec"]
    assert all(a["runner_receipt"]["settled"] for a in res["attempts"])
    assert res["winner_runner"] == "codex-exec"
    assert res["roster"] == ["codex-exec"]
    assert res["answer"] == "candidate 0"


def test_external_pack_attributes_a_pre_prompt_fallback_to_actual_runner(monkeypatch,
                                                                         tmp_path):
    import harness.cli as cli

    monkeypatch.setattr(pack, "_isolate", lambda _cwd: tempfile.mkdtemp(prefix="pack_fallback_"))
    monkeypatch.setattr(pack, "_init_external_git", lambda _root: None)
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(tmp_path / "memory.db"), str(tmp_path / "runs.db"),
        str(tmp_path / "dashboard.html"), str(tmp_path / "sandbox")))
    receipt = RunnerReceipt.from_dict({
        "runner": "codex-appserver", "settled": True, "usage_known": True,
        "billing_class": "subscription_allowance", "billing_mode": "subscription",
    })

    def fell_back(*args, **kwargs):
        result = RunResult(
            task_id="pack0", harness="codex-appserver", provider="codex",
            total_tokens=10, input_tokens=8, output_tokens=2, turns=1,
            cost_usd=0.01, success=True, answer="fallback candidate", messages=[])
        setattr(result, runner_slice.RECEIPT_ATTR, receipt)
        return result

    monkeypatch.setattr(runner_slice, "run_adhoc", fell_back)
    result = pack.run_pack(
        "fix it", str(tmp_path), n=1,
        runner_decision=_external_decision("codex-exec"))

    assert result["attempts"][0]["runner"] == "codex-appserver"
    assert result["winner_runner"] == "codex-appserver"
    assert result["attempts"][0]["runner_receipt"]["runner"] == "codex-appserver"


def test_external_parallel_emit_failure_does_not_fail_work_or_leak_trees(monkeypatch,
                                                                         tmp_path):
    """A disconnected observer cannot rewrite real worker/check outcomes."""
    roots = []

    def isolate(_cwd):
        root = tempfile.mkdtemp(prefix="pack_emit_")
        roots.append(root)
        return root

    monkeypatch.setattr(pack, "_isolate", isolate)
    monkeypatch.setattr(pack, "_init_external_git", lambda _root: None)
    monkeypatch.setattr(runner_slice, "run_adhoc", lambda *a, **kw: RunResult(
        harness="codex-exec", success=True, answer="done", turns=1, messages=[]))

    def bad_emit(_idx, _rec):
        raise RuntimeError("consumer disconnected sk-super-secret")

    res = pack.run_pack("fix it", str(tmp_path), n=2, parallel=2, emit=bad_emit,
                        runner_decision=_external_decision())
    assert res["winner"] == 0
    assert all(a["runner"] == "codex-exec" for a in res["attempts"])
    assert all(not a["error"] for a in res["attempts"])
    assert "sk-super-secret" not in json.dumps(res)
    assert all(not os.path.exists(root) for root in roots)


def test_verifier_start_failure_becomes_attempt_evidence_and_cleans_trees(monkeypatch,
                                                                          tmp_path):
    """A broken host checker must not abort Pack before exact-root cleanup."""
    _stub_backends(monkeypatch, [])
    roots = []

    def isolate(_cwd):
        root = tempfile.mkdtemp(prefix="pack_check_error_")
        roots.append(root)
        return root

    monkeypatch.setattr(pack, "_isolate", isolate)
    monkeypatch.setattr(
        pack, "_run_check_evidence",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            RuntimeError("checker leaked api_key=abcdefghijklmnop")))

    res = pack.run_pack("fix it", str(tmp_path), n=2, check="broken-check")

    assert res["winner"] is None
    assert all(row["check_pass"] is False for row in res["attempts"])
    assert all("verification failed" in row["error"] for row in res["attempts"])
    assert "abcdefghijklmnop" not in json.dumps(res)
    assert all(not os.path.exists(root) for root in roots)


def test_budgeted_external_pack_stops_when_worker_usage_is_unknown(monkeypatch,
                                                                    tmp_path):
    """An absent meter is not zero; no later candidate may spend past the blind spot."""
    import harness.cli as cli

    monkeypatch.setenv("COLLIE_MAX_TOTAL_TOKENS", "1000")
    monkeypatch.delenv("COLLIE_MAX_COST", raising=False)
    monkeypatch.setattr(pack, "_isolate", lambda _cwd: tempfile.mkdtemp(prefix="pack_budget_"))
    monkeypatch.setattr(pack, "_init_external_git", lambda _root: None)
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(tmp_path / "memory.db"), str(tmp_path / "runs.db"),
        str(tmp_path / "dashboard.html"), str(tmp_path / "sandbox")))
    calls = []

    def unknown_usage(*args, **kwargs):
        calls.append(kwargs.get("task_id"))
        return RunResult(
            harness="codex-exec", success=True, answer="done", turns=1,
            total_tokens=None, cost_usd=None, messages=[])

    monkeypatch.setattr(runner_slice, "run_adhoc", unknown_usage)

    result = pack.run_pack(
        "fix it", str(tmp_path), n=3,
        runner_decision=_external_decision())

    assert calls == ["pack0"]
    assert result["winner"] is None
    assert result["budget_exhausted"] is True
    assert result["budget_usage_unknown"] is True
    assert result["budget_unknown_fields"] == ["tokens"]
    assert result["total_cost_usd"] is None
    assert "did not report tokens" in result["attempts"][0]["error"]
    assert all("budget exhausted" in row["error"]
               for row in result["attempts"][1:])
