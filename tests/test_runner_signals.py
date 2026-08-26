"""Runner health snapshots are read-only, route-specific, and fail unknown."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from harness.agent_runners import ProcessOutcome
from harness.runner_signals import (
    QuotaSnapshot,
    RateLimitWindow,
    collect,
    read_codex_quota,
    reset_cache,
)
from harness.runner_specs import RunnerProbe


class FakeTransport:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None,
            on_stdout=None):
        self.calls.append((tuple(argv), stdin_text, dict(env or {})))
        on_process(object())
        return self.outcome


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    reset_cache()
    # The quota reader rejects billing overrides before a child exists.  Unit
    # tests describe a clean parent explicitly rather than depending on which
    # agent launched pytest.
    for name in list(__import__("os").environ):
        upper = name.upper()
        if ((upper.startswith(("OPENAI_", "CODEX_", "AZURE_OPENAI_")))
                and upper not in {"CODEX_HOME", "CODEX_CI", "CODEX_SESSION_ID"}):
            monkeypatch.delenv(name, raising=False)


def _quota_response(used=42):
    return json.dumps({"id": 1, "result": {"ok": True}}) + "\n" + json.dumps({
        "id": 2,
        "result": {"rateLimits": {
            "primary": {"usedPercent": used, "resetsAt": 1_800_000_000,
                        "windowDurationMins": 300},
            "secondary": {"usedPercent": 7},
            "rateLimitReachedType": None,
            "planType": "pro",
        }},
    }) + "\n"


def test_codex_quota_parses_only_the_read_response(monkeypatch):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    transport = FakeTransport(ProcessOutcome(stdout=_quota_response(), exit_code=0))

    quota, error = read_codex_quota(
        now=1_700_000_000.0, transport=transport, timeout_s=3)

    assert error == "" and quota is not None
    assert quota.primary.used_percent == 42
    assert quota.secondary.used_percent == 7
    assert quota.plan_type == "pro"
    argv, stdin, child_env = transport.calls[0]
    assert "app-server" in argv[-2]
    assert "account/rateLimits/read" in stdin
    assert "rateLimitResetCredit/consume" not in stdin
    assert "thread/" not in stdin and "prompt" not in stdin.lower()
    assert not any(name.startswith("OPENAI_") for name in child_env)


def test_quota_failure_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    transport = FakeTransport(ProcessOutcome(stdout="not json\n", exit_code=0))

    quota, error = read_codex_quota(transport=transport)

    assert quota is None
    assert "no account/rateLimits/read response" in error


def test_quota_rejects_non_standard_json_numbers(monkeypatch):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    transport = FakeTransport(ProcessOutcome(stdout=(
        '{"id":2,"result":{"rateLimits":{"primary":'
        '{"usedPercent":NaN},"planType":"pro"}}}\n'), exit_code=0))

    quota, error = read_codex_quota(transport=transport)

    assert quota is None
    assert "no account/rateLimits/read response" in error


@pytest.mark.parametrize("used", [1.5, True, "42", -1])
def test_quota_rejects_ambiguous_or_invalid_integer_counters(monkeypatch, used):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    transport = FakeTransport(ProcessOutcome(stdout=_quota_response(used), exit_code=0))

    quota, error = read_codex_quota(transport=transport)

    assert quota is None
    assert "documented rate-limit window" in error


def test_quota_jsonl_keeps_unicode_separators_inside_a_record(monkeypatch):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    payload = json.dumps({
        "id": 2,
        "result": {"rateLimits": {
            "primary": {"usedPercent": 12},
            "planType": "pro\u2028workspace",
        }},
    }, ensure_ascii=False) + "\n"
    transport = FakeTransport(ProcessOutcome(stdout=payload, exit_code=0))

    quota, error = read_codex_quota(transport=transport)

    assert error == "" and quota is not None
    assert quota.plan_type == "pro\u2028workspace"


def test_truncated_quota_transport_is_unknown(monkeypatch):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    transport = FakeTransport(ProcessOutcome(
        stdout=_quota_response(), exit_code=0, output_truncated=True))

    quota, error = read_codex_quota(transport=transport)

    assert quota is None
    assert "capture limit" in error


def test_nonzero_quota_helper_exit_cannot_publish_a_plausible_response(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/codex")
    transport = FakeTransport(ProcessOutcome(
        stdout=_quota_response(3), exit_code=125))

    quota, error = read_codex_quota(transport=transport)

    assert quota is None
    assert "status 125" in error


def test_quota_reader_is_cached_for_one_minute(monkeypatch):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    transport = FakeTransport(ProcessOutcome(stdout=_quota_response(11), exit_code=0))

    first, _ = read_codex_quota(now=1000, transport=transport)
    second, _ = read_codex_quota(now=1059, transport=transport)

    assert first == second and len(transport.calls) == 1


def test_quota_cache_refreshes_after_wall_clock_moves_backward(monkeypatch):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    transport = FakeTransport(ProcessOutcome(stdout=_quota_response(11), exit_code=0))

    read_codex_quota(now=1000, transport=transport)
    read_codex_quota(now=900, transport=transport)

    assert len(transport.calls) == 2


def test_quota_cache_is_scoped_to_codex_home(monkeypatch, tmp_path):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    first_transport = FakeTransport(
        ProcessOutcome(stdout=_quota_response(11), exit_code=0))
    second_transport = FakeTransport(
        ProcessOutcome(stdout=_quota_response(27), exit_code=0))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "account-a"))
    first, _ = read_codex_quota(now=1000, transport=first_transport)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "account-b"))
    second, _ = read_codex_quota(now=1001, transport=second_transport)

    assert first.primary.used_percent == 11
    assert second.primary.used_percent == 27
    assert len(first_transport.calls) == len(second_transport.calls) == 1


def test_quota_cache_does_not_cross_injected_transport_instances(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/codex")
    first_transport = FakeTransport(
        ProcessOutcome(stdout=_quota_response(11), exit_code=0))
    second_transport = FakeTransport(
        ProcessOutcome(stdout=_quota_response(27), exit_code=0))

    first, _ = read_codex_quota(now=1000, transport=first_transport)
    second, _ = read_codex_quota(now=1001, transport=second_transport)

    assert first.primary.used_percent == 11
    assert second.primary.used_percent == 27
    assert len(first_transport.calls) == len(second_transport.calls) == 1


@pytest.mark.parametrize("kwargs", [
    {"now": float("nan")}, {"timeout_s": 0}, {"timeout_s": float("inf")},
])
def test_quota_reader_rejects_invalid_timing(kwargs):
    with pytest.raises(ValueError):
        read_codex_quota(**kwargs)


def test_concurrent_quota_readers_share_one_inflight_probe(monkeypatch):
    monkeypatch.setattr("harness.runner_signals.shutil.which", lambda _name: "codex.exe")
    entered = threading.Event()
    release = threading.Event()

    class BlockingTransport(FakeTransport):
        def run(self, *args, **kwargs):
            entered.set()
            assert release.wait(2)
            return super().run(*args, **kwargs)

    transport = BlockingTransport(ProcessOutcome(stdout=_quota_response(19), exit_code=0))
    results = []

    def read():
        results.append(read_codex_quota(now=1000, transport=transport))

    first = threading.Thread(target=read)
    second = threading.Thread(target=read)
    first.start()
    assert entered.wait(2)
    second.start()
    release.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert len(transport.calls) == 1
    assert len(results) == 2
    assert all(error == "" and quota.primary.used_percent == 19
               for quota, error in results)


def test_collect_reads_history_and_applies_quota_to_codex_only(tmp_path):
    db_path = tmp_path / "runs.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE runs(ts INTEGER,harness TEXT,success INTEGER,"
               "verified INTEGER,cost_usd REAL,wall_ms INTEGER,error TEXT)")
    now = time.time()
    db.executemany("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", [
        (int(now - 10), "codex-exec", 0, 0, 0.0, 100, "HTTP 429 rate limit"),
        (int(now - 20), "codex-exec", 1, 1, 0.01, 200, ""),
        (int(now - 30), "claude-code", 1, 0, 0.02, 300, ""),
    ])
    db.commit(); db.close()
    quota = QuotaSnapshot(primary=RateLimitWindow(25), observed_at=now)
    probes = {
        "codex-exec": RunnerProbe(key="codex-exec", installed=True, login="ok"),
        "claude-code": RunnerProbe(key="claude-code", installed=True, login="ok"),
    }

    signals = collect(
        ("codex-exec", "claude-code"), probes, runs_db=str(db_path),
        live_quota=True, now=now, quota_reader=lambda **_kw: (quota, ""))

    codex = signals.for_runner("codex-exec")
    claude = signals.for_runner("claude-code")
    assert codex.quota.primary.used_percent == 25
    assert claude.quota is None
    assert codex.recent_429 == 1 and codex.cooldown_until > now
    assert codex.history.runs == 2 and codex.history.verified == 1
    assert claude.history.runs == 1
    assert signals.digest() == signals.digest()


def test_collect_degrades_a_crashing_quota_reader_to_redacted_unknown():
    secret = "sk-" + "q" * 32
    probes = {"codex-exec": RunnerProbe(
        key="codex-exec", installed=True, login="ok")}

    signals = collect(
        ("codex-exec",), probes, live_quota=True,
        quota_reader=lambda **_kw: (_ for _ in ()).throw(
            RuntimeError("quota failed api_key=" + secret)))

    row = signals.for_runner("codex-exec")
    assert row.quota is None
    assert secret not in row.error and "[redacted]" in row.error


def test_collect_degrades_malformed_history_numbers_to_unknown(tmp_path):
    db_path = tmp_path / "runs.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE runs(ts,harness,success,verified,cost_usd,wall_ms,error)")
    db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", (
        "not-a-time", "codex-exec", 1, 1, "not-a-cost", "not-a-wall", ""))
    db.commit(); db.close()
    probes = {"codex-exec": RunnerProbe(
        key="codex-exec", installed=True, login="ok")}

    row = collect(("codex-exec",), probes, runs_db=str(db_path)).for_runner(
        "codex-exec")

    assert row.history is None and row.recent_429 == 0
    assert "history values unavailable" in row.error


def test_collect_degrades_non_finite_history_to_unknown(tmp_path):
    db_path = tmp_path / "runs.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE runs(ts,harness,success,verified,cost_usd,wall_ms,error)")
    db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", (
        time.time(), "codex-exec", 1, 1, float("inf"), 10, ""))
    db.commit(); db.close()
    probes = {"codex-exec": RunnerProbe(
        key="codex-exec", installed=True, login="ok")}

    row = collect(("codex-exec",), probes, runs_db=str(db_path)).for_runner(
        "codex-exec")

    assert row.history is None
    assert "history values unavailable" in row.error
