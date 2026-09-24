"""Runner probes run side by side, and say exactly what they said when they ran in a row.

The web run menu reads every runner's probe. Pi's alone started Pi four times one after another
(--version and an auth check per provider), 4.5 s on Windows, so the menu's first read after a
minute took 5.5 s.
"""
import json
import threading
import time
import types

from harness import pi_rpc_runner, runner_registry


def test_pi_probe_checks_in_parallel_and_keeps_the_first_ready_provider(monkeypatch):
    calls = []
    lock = threading.Lock()
    answers = {"openai-codex": {"status": "missing", "reason": "no_credentials"},
               "anthropic": {"status": "ready"},
               "google": {"status": "ready"}}

    def fake_run(argv, **_kw):
        with lock:
            calls.append(argv[1:])
        time.sleep(0.25)
        if argv[1] == "--version":
            return types.SimpleNamespace(returncode=0, stdout="0.84.1\n", stderr="")
        provider = argv[argv.index("--provider") + 1]
        value = answers[provider]
        return types.SimpleNamespace(returncode=0 if value["status"] == "ready" else 1,
                                     stdout=json.dumps(value), stderr="")

    monkeypatch.setattr(pi_rpc_runner.shutil, "which", lambda _exe: r"C:\fake\pi.cmd")
    monkeypatch.setattr(pi_rpc_runner.subprocess, "run", fake_run)
    t0 = time.monotonic()
    probe = pi_rpc_runner.PiRpcRunner().probe(now=100.0)
    took = time.monotonic() - t0
    assert took < 0.7, "four 0.25 s checks took %.2fs" % took
    assert len(calls) == 4
    assert probe.installed is True and probe.version == "0.84.1"
    assert probe.login == "ok"
    assert probe.billing_evidence["ready_provider"] == "anthropic", "order decides, not timing"
    assert probe.billing_evidence["providers_checked"] == ["openai-codex", "anthropic"]


def test_pi_probe_without_any_ready_provider_reports_the_first_reason(monkeypatch):
    def fake_run(argv, **_kw):
        if argv[1] == "--version":
            return types.SimpleNamespace(returncode=0, stdout="0.84.1\n", stderr="")
        provider = argv[argv.index("--provider") + 1]
        return types.SimpleNamespace(returncode=1, stderr="", stdout=json.dumps(
            {"status": "missing", "reason": "no_%s_login" % provider.replace("-", "_")}))

    monkeypatch.setattr(pi_rpc_runner.shutil, "which", lambda _exe: r"C:\fake\pi.cmd")
    monkeypatch.setattr(pi_rpc_runner.subprocess, "run", fake_run)
    probe = pi_rpc_runner.PiRpcRunner().probe(now=100.0)
    assert probe.login == "not-logged-in"
    assert probe.billing_evidence["providers_checked"] == ["openai-codex", "anthropic", "google"]
    assert probe.detail == "no openai codex login"


def test_probe_all_is_as_slow_as_its_slowest_runner_not_their_sum(monkeypatch):
    keys = list(runner_registry.SPECS)[:4]

    def slow_probe(key, **_kw):
        time.sleep(0.3)
        return types.SimpleNamespace(key=key)

    monkeypatch.setattr(runner_registry, "probe", slow_probe)
    t0 = time.monotonic()
    out = runner_registry.probe_all(keys=keys)
    took = time.monotonic() - t0
    assert list(out) == keys and all(out[k].key == k for k in keys)
    assert took < 0.8, "%d probes of 0.3 s took %.2fs" % (len(keys), took)


def test_the_host_compat_report_is_applied_before_any_parallel_probe_reads_it(monkeypatch):
    """autoload marks itself done before it applies the report, so a probe racing it on another
    thread would build a row without the host's recorded downgrades."""
    applied = []

    def slow_autoload():
        time.sleep(0.1)
        applied.append(True)
        return {}

    seen = []

    def probe(key, **_kw):
        seen.append(bool(applied))
        return types.SimpleNamespace(key=key)

    monkeypatch.setattr(runner_registry, "autoload_compat_report", slow_autoload)
    monkeypatch.setattr(runner_registry, "probe", probe)
    runner_registry.probe_all(keys=list(runner_registry.SPECS)[:4])
    assert seen and all(seen)


def test_concurrent_reads_of_one_runner_share_a_single_probe(monkeypatch):
    """A page load asks for the run options from several places at once; each used to probe every
    runner itself, so three concurrent reads took 3.1-3.7 s each against 2.1 s for one."""
    runner_registry.reset_cache()
    calls = []

    def slow(spec, now, live, provider, status_runner):
        calls.append(spec.key)
        time.sleep(0.3)
        return runner_registry.RunnerProbe(key=spec.key, installed=True, probed_at=now,
                                           ttl_s=runner_registry.PROBE_TTL_S)

    monkeypatch.setattr(runner_registry, "_probe_uncached", slow)
    got = []
    threads = [threading.Thread(target=lambda: got.append(runner_registry.probe("codex-exec")))
               for _ in range(3)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert calls == ["codex-exec"], calls
    assert len(got) == 3 and all(g is got[0] for g in got)
    assert time.monotonic() - t0 < 0.8
    runner_registry.reset_cache()
