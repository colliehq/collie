"""Several agents on one task must not write into each other.

The bug this pins was measured, not imagined: two harnesses built the way run_pack builds them,
run in sequence, and the second one's system prompt arrived carrying
`RELEVANT MEMORY (auto-recalled): - Task 'pack0' -> <the first one's answer>`.
Best-of-N selection is meaningless if attempt k has already read attempts 0..k-1.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import cli
from harness.memory import SqliteMemory
from harness.providers import Completion, ModelProvider, Usage
from harness.scratch import ScratchMemory, isolate_harness


class StubProvider(ModelProvider):
    """Answers in one turn. NOT named "mock" — the loop deliberately never consolidates mock runs,
    which would hide the very write path under test."""
    name = "stub"
    model = "stub-1"

    def __init__(self, answer):
        self.answer = answer
        self.systems = []

    def complete(self, system, messages, tool_schemas, on_text=None):
        self.systems.append(system)
        return Completion(text=self.answer, usage=Usage(input_tokens=10, output_tokens=10),
                          stop_reason="end_turn")


def test_scratch_writes_never_reach_the_shared_store():
    with tempfile.TemporaryDirectory() as root:
        base = SqliteMemory(os.path.join(root, "memory.db"))
        before = base.count()
        mem = ScratchMemory(base, read_project="repo")
        mem.remember("a note only this agent should have", keys="note", project="agent-3")
        assert mem.recall("note only this agent", project="agent-3"), "the agent can read it back"
        assert base.count() == before, "nothing was written to the shared store"
        assert not base.recall("note only this agent", project="repo")
        assert not base.recall("note only this agent", project="agent-3")
        mem.close()


def test_scratch_reads_still_see_the_shared_baseline():
    """Isolation must not mean starting dumb: every agent keeps the team's common knowledge."""
    with tempfile.TemporaryDirectory() as root:
        base = SqliteMemory(os.path.join(root, "memory.db"))
        base.remember("the widget cache is invalidated in cache_util.py", project="repo")
        # The agent is given its OWN project (that is what isolates its undo stack) and must still
        # see the shared fact despite asking under that name.
        mem = ScratchMemory(base, read_project="repo")
        hits = mem.recall("where is the widget cache invalidated", project="agent-3")
        assert any("cache_util.py" in h.get("text", "") for h in hits), hits
        mem.close()


def test_two_agents_on_one_task_do_not_see_each_other(monkeypatch):
    with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as cwd:
        monkeypatch.setattr(cli, "DATA", data)
        shared = SqliteMemory(cli._paths()[0])
        shared.remember("this repo builds with `make all`", project="repo")
        shared.close()

        secret = "the crash comes from widget_factory.py line 42, a stale cache"
        stubs = []
        for i, answer in enumerate((secret, "an unrelated second opinion")):
            h = cli.make_harness(cwd, provider="mock", model=None, project="packrun-%d" % i)
            isolate_harness(h, read_project="repo")
            stub = StubProvider(answer)
            h.provider = stub
            stubs.append(stub)
            h.run("pack%d" % i, "why does the widget crash")
            h.memory.close()
            h.recorder.close()      # Windows will not delete a sqlite file that is still open

        second = stubs[1].systems[0]
        assert secret[:40] not in second, "agent 2 read agent 1's conclusion"
        assert "make all" in second, "agent 2 lost the shared baseline it should still have"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        if test.__code__.co_argcount == 0:
            test()
    print("== PACK ISOLATION: %d test groups passed ==" % len(tests))
