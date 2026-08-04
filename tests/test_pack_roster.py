"""A roster spreads the attempts over different backends without losing track of which is which."""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import pack


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
