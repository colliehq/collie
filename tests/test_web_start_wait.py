"""Startup acknowledgement waits are bounded by elapsed time, not clock changes."""
import pytest

from harness import web_tasks, webapp


@pytest.mark.parametrize("wall_rate", [0, -100, 100], ids=["frozen", "backward", "forward"])
def test_startup_wait_keeps_its_elapsed_budget_when_wall_time_changes(monkeypatch, wall_rate):
    clock = {"elapsed": 0.0}
    monkeypatch.setattr(webapp.Handler, "_runs", {})
    monkeypatch.setattr(web_tasks.time, "monotonic", lambda: clock["elapsed"])
    monkeypatch.setattr(web_tasks.time, "time", lambda: 1000 + wall_rate * clock["elapsed"])

    def sleep(seconds):
        clock["elapsed"] += seconds
        # Bound the broken implementation too: this regression must fail,
        # rather than hanging the runner when its wall clock never advances.
        assert clock["elapsed"] <= 0.1, "startup wait exceeded its elapsed-time budget"

    monkeypatch.setattr(web_tasks.time, "sleep", sleep)
    assert web_tasks._await_run_id("clock-fixture", timeout=0.05) == ""
    assert 0.05 <= clock["elapsed"] <= 0.06


def test_startup_wait_returns_an_existing_run_without_sleeping(monkeypatch):
    monkeypatch.setattr(webapp.Handler, "_runs", {
        "ready-fixture": {"ended": None, "run": "ready-run"},
    })

    def unexpected_sleep(_seconds):
        pytest.fail("an available run should not wait")

    monkeypatch.setattr(web_tasks.time, "sleep", unexpected_sleep)
    assert web_tasks._await_run_id("ready-fixture", timeout=0) == "ready-run"
