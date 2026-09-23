"""The Windows Codex CLI version split around ``windows.sandbox_private_desktop``.

Codex 0.156.0 removed the field from the config schema (verified 2026-09-22
against the released binary): ``codex-rs/config/src/types.rs`` -> ``WindowsToml``
keeps only ``sandbox`` and is ``#[schemars(deny_unknown_fields)]``, and
``codex-rs/config/src/strict_config.rs`` classes the old name as a removed
setting.  Under ``--strict-config`` an unknown ``-c`` field is
``io::ErrorKind::InvalidData``, so a launch that still passes it dies before
``initialize``.

The field cannot simply be deleted.  Measured on Windows 11 / Codex 0.149.0 on
2026-08-22, a private-desktop sandbox under Collie's ``CREATE_NO_WINDOW`` start
gate refused every write *while the process exited 0*, and turning the desktop
off was what made the same prompt write its file.  Hosts on 0.149-0.155 still
need that override, so the key is version-gated against the executable Collie
actually resolved, never deleted outright and never inferred from the SDK pin.

Every case here mocks the version probe.  Nothing in this module may launch the
installed Codex CLI.
"""
from __future__ import annotations

import json
import os

import pytest

from harness import agent_runners, runner_env
from harness.agent_runners import CodexExecRunner, ProcessOutcome, RunnerSnapshot
from harness.codex_app_server_runner import CodexAppServerRunner


THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"
REMOVED = "windows.sandbox_private_desktop=false"
LEVEL = 'windows.sandbox="unelevated"'


@pytest.fixture(autouse=True)
def _clean_version_cache():
    agent_runners._VERSION_CACHE.clear()
    yield
    agent_runners._VERSION_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_billing_override(monkeypatch):
    for name in list(os.environ):
        if name.upper().startswith(("OPENAI_", "AZURE_OPENAI_")):
            monkeypatch.delenv(name, raising=False)


def _probe(version, error=""):
    """A stand-in for ``<cli> --version`` that records who it was asked about."""
    calls = []

    def probe(executable):
        calls.append(executable)
        return (version, error)

    probe.calls = calls
    return probe


def _complete():
    events = [
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    return ProcessOutcome(
        stdout="\n".join(json.dumps(event) for event in events) + "\n", exit_code=0)


class FakeProcess:
    pid = 4321


class FakeProcessRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, cwd, stdin_text, timeout_s, on_process, env=None):
        self.calls.append(tuple(argv))
        on_process(FakeProcess())
        return _complete()


def _snapshotter(_root):
    return {"tree_digest": "same", "snapshot_complete": True}


def _exec_argv(monkeypatch, tmp_path, version, *, resume=False, error=""):
    monkeypatch.setattr(agent_runners.plat, "is_windows", lambda: True)
    process = FakeProcessRunner()
    runner = CodexExecRunner(process_runner=process, snapshotter=_snapshotter,
                             cli_version_probe=_probe(version, error))
    if resume:
        prior = RunnerSnapshot(runner=runner.key,
                               workspace=os.path.realpath(str(tmp_path)),
                               thread_id=THREAD)
        runner.resume(prior, "go")
    else:
        runner.start("go", str(tmp_path))
    return process.calls[0]


def _app_server_argv(monkeypatch, version, *, error=""):
    monkeypatch.setattr(agent_runners.plat, "is_windows", lambda: True)
    runner = CodexAppServerRunner(transport_factory=lambda *a, **k: None,
                                  cli_version_probe=_probe(version, error))
    return tuple(runner._argv())


# --------------------------------------------------------------------------
# 0.156+: the removed key must be gone, the sandbox level must not be
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version", ["codex-cli 0.156.0", "codex-cli 0.157.2",
                                     "codex-cli 1.0.0"])
def test_exec_start_omits_the_removed_key_on_0156_and_later(monkeypatch, tmp_path,
                                                            version):
    argv = _exec_argv(monkeypatch, tmp_path, version)

    assert REMOVED not in argv
    # Dropping the level too would let `config_toml.rs` -> derive_permission_profile
    # rewrite workspace-write to read-only, which is the failure this override
    # exists to prevent.
    assert LEVEL in argv
    assert "--strict-config" in argv


def test_exec_resume_omits_the_removed_key_on_0156(monkeypatch, tmp_path):
    argv = _exec_argv(monkeypatch, tmp_path, "codex-cli 0.156.0", resume=True)

    assert REMOVED not in argv
    assert LEVEL in argv
    assert "--strict-config" in argv


def test_app_server_argv_omits_the_removed_key_on_0156(monkeypatch):
    argv = _app_server_argv(monkeypatch, "codex-cli 0.156.0")

    assert REMOVED not in argv
    assert LEVEL in argv
    assert "--strict-config" in argv


# --------------------------------------------------------------------------
# 0.149-0.155: the measured workaround stays
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version", ["codex-cli 0.149.0", "codex-cli 0.155.1",
                                     "codex-cli 0.155.99"])
def test_exec_start_keeps_the_measured_override_on_older_clis(monkeypatch, tmp_path,
                                                              version):
    argv = _exec_argv(monkeypatch, tmp_path, version)

    assert REMOVED in argv
    assert LEVEL in argv


def test_exec_resume_keeps_the_measured_override_on_older_clis(monkeypatch, tmp_path):
    argv = _exec_argv(monkeypatch, tmp_path, "codex-cli 0.155.1", resume=True)

    assert REMOVED in argv
    assert LEVEL in argv


def test_app_server_argv_keeps_the_measured_override_on_older_clis(monkeypatch):
    argv = _app_server_argv(monkeypatch, "codex-cli 0.155.1")

    assert REMOVED in argv
    assert LEVEL in argv


# --------------------------------------------------------------------------
# Unknown version: fail closed on the measured control, not on the new schema
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version,error", [
    ("", "could not run codex --version: TimeoutExpired: timed out"),
    ("codex-cli", ""),
    ("some other tool", ""),
    ("0.156", ""),
])
def test_unknown_version_keeps_the_override_collie_measured(monkeypatch, tmp_path,
                                                            version, error):
    """An unidentified CLI keeps the control a real measurement said it needs.

    The two ways to be wrong are not symmetric.  Keeping the key against a
    0.156 host fails loudly at startup with "unknown configuration field
    `windows.sandbox_private_desktop`" and changes nothing.  Dropping it against
    the 0.149-era host that was actually measured reinstates a sandbox that
    refuses every write *and still exits 0* -- a turn that silently accomplishes
    nothing.  Fail closed means keeping the measured control.
    """
    argv = _exec_argv(monkeypatch, tmp_path, version, error=error)

    assert REMOVED in argv
    assert LEVEL in argv

    app_argv = _app_server_argv(monkeypatch, version, error=error)
    assert REMOVED in app_argv
    assert LEVEL in app_argv


def test_the_gate_never_relaxes_or_elevates_the_sandbox(monkeypatch, tmp_path):
    for version in ("codex-cli 0.155.1", "codex-cli 0.156.0", ""):
        for argv in (_exec_argv(monkeypatch, tmp_path, version),
                     _exec_argv(monkeypatch, tmp_path, version, resume=True),
                     _app_server_argv(monkeypatch, version)):
            joined = " ".join(argv)
            assert LEVEL in argv
            # 0.156 adds an `mxc` mode (types.rs -> WindowsSandboxModeToml) that
            # maps to Disabled, and `elevated` needs a one-time admin install.
            assert "mxc" not in joined
            assert "elevated" not in joined.replace('"unelevated"', "")
            assert "danger-full-access" not in joined
            assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_posix_adds_no_windows_keys_at_any_version(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_runners.plat, "is_windows", lambda: False)
    probe = _probe("codex-cli 0.155.1")
    process = FakeProcessRunner()
    CodexExecRunner(process_runner=process, snapshotter=_snapshotter,
                    cli_version_probe=probe).start("go", str(tmp_path))

    assert not [item for item in process.calls[0] if "windows." in item]
    # Seatbelt/bwrap are unconditional on POSIX, so the version is never even
    # asked for: no `--version` subprocess on the hot path of every turn.
    assert probe.calls == []


# --------------------------------------------------------------------------
# Version parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("codex-cli 0.156.0", (0, 156, 0)),
    ("codex-cli 0.155.1", (0, 155, 1)),
    ("0.149.0", (0, 149, 0)),
    ("codex-cli 1.2.3 (abcdef)", (1, 2, 3)),
    ("codex-cli 0.156.0-alpha.1", (0, 156, 0)),
    ("0.156.0-alpha.1", (0, 156, 0)),
    ("codex-cli v0.156.0", (0, 156, 0)),
    ("", None),
    ("codex-cli", None),
    ("codex-cli 0.156", None),
    ("codex-cli vNEXT", None),
    ("codex-cli abc1.2.3", None),
    # Not Codex's banner and not a bare version token: unknown, which keeps the
    # override.  Reading the first triple in any line would call a Node wrapper
    # banner "0.156 or later" and drop the key on an old CLI.
    ("node v22.1.0", None),
    ("Codex CLI 0.156.0", None),
    # A line that is *nothing but* a version is still taken at face value: some
    # builds print only that, and `_cli_version` has always reported it.  The
    # residual edge is a wrapper whose whole output is another tool's version.
    ("v22.1.0", (22, 1, 0)),
    ("v22.1.0 (node) codex-cli 0.155.1", (0, 155, 1)),
])
def test_version_parsing_is_explicit_about_what_it_cannot_read(text, expected):
    assert agent_runners._parse_cli_version(text) == expected


def test_an_unrelated_banner_is_unknown_rather_than_a_codex_version(monkeypatch,
                                                                    tmp_path):
    """A triple that is not Codex's must not decide Codex's config schema.

    A shim that prints its own runtime's version is the dangerous case: read as
    22.1.0 it would answer ">= 0.156" for a 0.155 CLI and drop the key, putting
    that host back on the sandbox that refuses every write and still exits 0.
    """
    argv = _exec_argv(monkeypatch, tmp_path, "node v22.1.0")

    assert REMOVED in argv
    assert LEVEL in argv


def test_a_codex_banner_is_read_past_an_unrelated_triple(monkeypatch, tmp_path):
    argv = _exec_argv(monkeypatch, tmp_path, "v22.1.0 (node) codex-cli 0.156.0")

    assert REMOVED not in argv
    assert LEVEL in argv


def test_removed_key_boundary_is_exactly_0_156_0():
    assert agent_runners._needs_private_desktop_override("codex-cli 0.155.999")
    assert not agent_runners._needs_private_desktop_override("codex-cli 0.156.0")
    # Unparseable is not "new".
    assert agent_runners._needs_private_desktop_override("codex-cli unknown")


# --------------------------------------------------------------------------
# Caching: one `--version` per binary, invalidated by identity
# --------------------------------------------------------------------------

def _binary(tmp_path, name="codex.exe", body=b"x"):
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


def test_version_is_probed_once_per_executable(tmp_path):
    path = _binary(tmp_path)
    probe = _probe("codex-cli 0.156.0")
    ttl = agent_runners._VERSION_CACHE_TTL_S

    first = agent_runners._resolved_cli_version(path, probe=probe, now=100.0)
    second = agent_runners._resolved_cli_version(path, probe=probe, now=100.5)
    third = agent_runners._resolved_cli_version(path, probe=probe, now=100.0 + ttl - 1)

    assert first == second == third == ("codex-cli 0.156.0", "")
    # A `--version` on every turn would add a process launch to the latency of
    # each start/resume for an answer that rarely changes.
    assert len(probe.calls) == 1


def test_a_good_answer_expires_so_a_replaced_dispatch_target_is_seen(tmp_path):
    """The shim's identity is not the identity of what it dispatches to.

    ``codex.cmd`` is a stable npm shim in front of a versioned package binary,
    so an upgrade can repoint it at a 0.156 build without changing the shim's
    own size or timestamps.  Caching a good answer for the life of the process
    would then pin "0.155" on a 0.156 host -- a launch that fails on every turn
    until a restart, which the one-shot "loud beats silent" argument for the
    unknown case does not cover.  Staleness has to be bounded.
    """
    path = _binary(tmp_path, name="codex.cmd")
    ttl = agent_runners._VERSION_CACHE_TTL_S
    old = _probe("codex-cli 0.155.1")
    assert agent_runners._resolved_cli_version(path, probe=old, now=0.0)[0] == (
        "codex-cli 0.155.1")

    # The shim itself never changes, so nothing invalidates the entry early...
    new = _probe("codex-cli 0.156.0")
    assert agent_runners._resolved_cli_version(path, probe=new, now=ttl - 1.0)[0] == (
        "codex-cli 0.155.1")
    assert new.calls == []

    # ...and the window in which that can be wrong ends.
    assert agent_runners._resolved_cli_version(path, probe=new, now=ttl + 1.0) == (
        "codex-cli 0.156.0", "")
    assert len(new.calls) == 1


def test_a_banner_nobody_could_parse_is_re_asked_on_the_short_cooldown(tmp_path):
    """Unknown keeps the override, so it must not be cached like a good answer.

    Unknown on a 0.156 host is a loud startup failure.  Holding it for the full
    TTL because one build printed something unexpected would extend that
    outage; the cooldown is the same short one a timed-out probe gets.
    """
    path = _binary(tmp_path)
    odd = _probe("some wrapper build 2026.09")
    cooldown = agent_runners._VERSION_RETRY_AFTER_S
    assert cooldown < agent_runners._VERSION_CACHE_TTL_S

    assert agent_runners._resolved_cli_version(path, probe=odd, now=0.0)[1] == ""
    agent_runners._resolved_cli_version(path, probe=odd, now=cooldown - 1.0)
    assert len(odd.calls) == 1

    later = _probe("codex-cli 0.156.0")
    assert agent_runners._resolved_cli_version(
        path, probe=later, now=cooldown + 1.0) == ("codex-cli 0.156.0", "")


def test_cache_invalidates_when_the_executable_is_replaced_in_place(tmp_path):
    path = _binary(tmp_path, body=b"old-build")
    old = _probe("codex-cli 0.155.1")
    assert agent_runners._resolved_cli_version(path, probe=old, now=10.0)[0] == (
        "codex-cli 0.155.1")

    # `npm -g install` and the Codex installer both overwrite the same shim
    # path, so the cache key has to be the file's identity, not its name.
    with open(path, "wb") as handle:
        handle.write(b"a much newer build")
    os.utime(path, (2_000_000_000, 2_000_000_000))
    new = _probe("codex-cli 0.156.0")

    assert agent_runners._resolved_cli_version(path, probe=new, now=10.1) == (
        "codex-cli 0.156.0", "")
    assert len(new.calls) == 1


def test_cache_is_keyed_per_path(tmp_path):
    first = _binary(tmp_path, name="a.exe", body=b"aa")
    second = _binary(tmp_path, name="b.exe", body=b"bbb")

    assert agent_runners._resolved_cli_version(
        first, probe=_probe("codex-cli 0.155.1"), now=1.0)[0] == "codex-cli 0.155.1"
    assert agent_runners._resolved_cli_version(
        second, probe=_probe("codex-cli 0.156.0"), now=1.0)[0] == "codex-cli 0.156.0"


def test_a_failed_probe_is_retried_after_a_cooldown_not_every_turn(tmp_path):
    path = _binary(tmp_path)
    failing = _probe("", "could not run codex --version: TimeoutExpired: timed out")

    assert agent_runners._resolved_cli_version(path, probe=failing, now=0.0)[0] == ""
    agent_runners._resolved_cli_version(path, probe=failing, now=1.0)
    # A CLI that will not answer must not add its timeout to every turn...
    assert len(failing.calls) == 1

    later = _probe("codex-cli 0.156.0")
    version, error = agent_runners._resolved_cli_version(
        path, probe=later, now=agent_runners._VERSION_RETRY_AFTER_S + 1.0)
    # ...but a transient failure must not pin "unknown" forever either.
    assert (version, error) == ("codex-cli 0.156.0", "")


def test_an_unstattable_executable_never_launches_a_process(tmp_path):
    probe = _probe("codex-cli 0.156.0")

    version, error = agent_runners._resolved_cli_version(
        str(tmp_path / "missing.exe"), probe=probe)

    assert version == ""
    assert "identify" in error
    # A bare name or a test double must never become a real CLI launch, and a
    # file we cannot stat cannot be the file that will run.
    assert probe.calls == []


def test_the_default_probe_is_not_reached_for_a_bare_name(monkeypatch, tmp_path):
    def explode(_executable):  # pragma: no cover - must not be called
        raise AssertionError("the installed Codex CLI must never be run from tests")

    # A bare name is only unstattable relative to somewhere: pin the directory
    # rather than depend on pytest's.
    monkeypatch.chdir(tmp_path)
    assert agent_runners._resolved_cli_version("codex", probe=explode)[0] == ""


def test_the_version_describes_the_resolved_executable_not_a_global(monkeypatch,
                                                                    tmp_path):
    """The gate must ask the binary that will run, not whatever is on PATH."""
    monkeypatch.setattr(agent_runners.plat, "is_windows", lambda: True)
    path = _binary(tmp_path, name="pinned-codex.exe")
    probe = _probe("codex-cli 0.156.0")
    process = FakeProcessRunner()

    CodexExecRunner(executable=path, process_runner=process,
                    snapshotter=_snapshotter,
                    cli_version_probe=probe).start("go", str(tmp_path))

    assert probe.calls == [path]
    assert process.calls[0][0] == path
    assert REMOVED not in process.calls[0]


def test_the_default_wiring_asks_only_about_the_resolved_file(monkeypatch, tmp_path):
    """With no probe injected, the gate still goes through stat and the cache.

    Every other case here passes ``cli_version_probe``, which would leave the
    production wiring -- ``version_probe or _resolved_cli_version`` and the
    resolved-path -> stat -> cache chain -- untested.  ``_cli_version`` is the
    only thing mocked, and it stands in for the one process this module is
    never allowed to start.
    """
    monkeypatch.setattr(agent_runners.plat, "is_windows", lambda: True)
    path = _binary(tmp_path, name="codex-0156.exe")
    asked = []

    def fake_cli_version(executable, timeout_s=10.0):
        asked.append(executable)
        return ("codex-cli 0.156.0", "")

    monkeypatch.setattr(agent_runners, "_cli_version", fake_cli_version)

    override = agent_runners._windows_sandbox_override(path)
    again = agent_runners._windows_sandbox_override(path)

    assert asked == [path]           # the resolved file, once, and nothing else
    assert override == again
    assert REMOVED not in override
    assert LEVEL in override


@pytest.mark.parametrize("resume", [False, True])
def test_start_refuses_a_billing_override_before_probing_the_cli(monkeypatch,
                                                                 tmp_path, resume):
    """`--version` is a child process, so the billing refusal comes first.

    It is offline and unbilled, but the invariant Collie states is that no
    process is created while the parent environment would re-route the account.
    The probe is also outside the start gate's Job Object, so it is not a child
    ``cancel_current()`` could reach.
    """
    monkeypatch.setattr(agent_runners.plat, "is_windows", lambda: True)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.invalid/v1")
    probe = _probe("codex-cli 0.156.0")
    process = FakeProcessRunner()
    runner = CodexExecRunner(process_runner=process, snapshotter=_snapshotter,
                             cli_version_probe=probe)

    with pytest.raises(runner_env.BillingOverrideError):
        if resume:
            runner.resume(RunnerSnapshot(runner=runner.key,
                                         workspace=os.path.realpath(str(tmp_path)),
                                         thread_id=THREAD), "go")
        else:
            runner.start("go", str(tmp_path))

    assert probe.calls == []
    assert process.calls == []
