from types import SimpleNamespace

from harness import cli, swe


def test_investigative_critic_reaches_the_read_only_harness(monkeypatch, tmp_path):
    """The critic must run; a missing cli import used to fail open as a clean review."""
    closed = []

    class Closeable:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    class FakeHarness:
        def __init__(self):
            self.registry = SimpleNamespace(_tools={
                "edit_file": object(), "write_file": object(), "undo": object(),
                "grep": object(),
            })
            self.memory = Closeable("memory")
            self.recorder = Closeable("recorder")

        def run(self, session, prompt, consolidate=False):
            assert session == "critic"
            assert "ISSUE:\nmissing edge case" in prompt
            assert "CANDIDATE DIFF" in prompt
            assert consolidate is False
            assert set(self.registry._tools) == {"grep"}
            assert self.max_turns == 14
            assert self.self_verify is False
            assert self.force_edit is False
            assert self.critic is False
            return SimpleNamespace(answer="CONCERN: untested empty input")

    seen = {}

    def make_harness(cwd, **kwargs):
        seen.update(cwd=cwd, **kwargs)
        return FakeHarness()

    monkeypatch.setattr(cli, "make_harness", make_harness)
    critic = swe._spawn_investigative_critic("mock", "mock-model")

    ok, objection = critic("missing edge case", "+ candidate", str(tmp_path))

    assert ok is False
    assert objection == "untested empty input"
    assert seen == {
        "cwd": str(tmp_path), "provider": "mock", "model": "mock-model",
        "project": "critic", "code_search": True,
    }
    assert closed == ["memory", "recorder"]
