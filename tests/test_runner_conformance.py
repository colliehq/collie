"""The conformance matrix, run against itself.

`harness/runner_compat.py` is the thing that decides whether a declared
capability is believed, so the failure that matters here is not "a column went
red" — it is a matrix that *cannot* go red: a check that silently skips, a report
that swallows an exception, a cell that says PASS because nothing ran.

So these tests assert three properties, in this order of importance:

1. **Every offline column produces a real, three-state answer on this host** for
   every key in the registry, without a model, without a credential and without
   either CLI installed.  A cell may be PASS or SKIP-with-a-reason; FAIL is the
   test failing, and a SKIP whose reason is empty is treated as a failure too.
2. **The report is publishable and machine-readable**: no prompt text, no token,
   no email address, and a shape `runner_registry.apply_compat_report` reads back
   into probe capabilities.
3. **One broken cell cannot take the matrix down**, which is asserted by breaking
   one on purpose.

The columns that spend real tokens (`one_turn`, `resume`, `usage`) run only with
`COLLIE_CONFORMANCE_LIVE` set, exactly as `tests/smoke_codex_oauth.py` gates its
own manual smoke run.  Without it they must appear as UNVERIFIED — that is a
property worth asserting rather than a state worth hiding, because an unrun
column is what the registry downgrades a capability for.
"""
import json
import os

import pytest

from harness import runner_compat, runner_registry
from harness.runner_compat import (
    CHECK_NAMES,
    CHECKS,
    FAIL,
    LIVE_CHECK_NAMES,
    OFFLINE_CHECK_NAMES,
    PASS,
    SKIP,
    UNVERIFIED,
)
from harness.runner_specs import CURRENT_PHASE


LIVE_ENV = "COLLIE_CONFORMANCE_LIVE"

# The prefixes `runner_env.assert_no_billing_override` refuses to start a worker
# against.  CODEX_HOME is kept: it selects which login file the CLI reads, not
# who pays, and Collie's own probe honours the same variable.
_BILLING_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "OPENAI_", "CODEX_", "AZURE_OPENAI_")
_KEEP = ("CODEX_HOME",)

PHASE_1_EXTERNAL = sorted(key for key, spec in runner_registry.SPECS.items()
                          if spec.kind == "external" and spec.phase <= CURRENT_PHASE)


@pytest.fixture(autouse=True)
def _shell_without_billing_route(monkeypatch):
    """Run every case as if the developer's shell exported no vendor overrides.

    Collie is very often started from inside Claude Code or a Codex terminal, and
    those parents export `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_EXECPATH` and
    friends.  A worker refuses to start against them — correctly, that is what
    `env_hygiene` asserts head-on — but leaving them ambient here would turn
    `framing`, `double_control` and `cancel` into permanent SKIPs on exactly the
    machines this layer is developed on, which is the same as not testing them.

    Only this process's environment is touched, and only for the duration of a
    test (`monkeypatch`); `os.environ` is never mutated directly.
    """
    for name in list(os.environ):
        upper = name.upper()
        if upper in _KEEP:
            continue
        if upper.startswith(_BILLING_PREFIXES):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def _isolated_compat_state():
    """`apply_compat_report` writes module-level state; put it back afterwards."""
    saved = dict(runner_registry._COMPAT)
    try:
        yield
    finally:
        runner_registry._COMPAT.clear()
        runner_registry._COMPAT.update(saved)
        runner_registry.reset_cache()


def _cells(report, key):
    return report["runners"][key]["checks"]


def _assert_answered(cell, where):
    """A cell must be a real answer: PASS, or a SKIP that says why."""
    status = cell["status"]
    assert status != FAIL, "%s FAILED: %s" % (where, cell["detail"])
    assert status in (PASS, SKIP, UNVERIFIED), "%s has status %r" % (where, status)
    if status in (SKIP, UNVERIFIED):
        assert cell["detail"].strip(), "%s is %s without a reason" % (where, status)
    assert cell["duration_ms"] >= 0


# --- the table itself -------------------------------------------------------
def test_checks_table_is_closed_and_partitioned():
    assert set(CHECKS) == set(CHECK_NAMES)
    assert set(LIVE_CHECK_NAMES) == {"one_turn", "resume", "usage"}
    assert set(OFFLINE_CHECK_NAMES) | set(LIVE_CHECK_NAMES) == set(CHECK_NAMES)
    assert not set(OFFLINE_CHECK_NAMES) & set(LIVE_CHECK_NAMES)
    # Every column states what a PASS means, in a sentence the .md renders.
    for name, check in CHECKS.items():
        assert check.name == name
        assert check.asserts.strip()
        assert callable(check.run)


def test_unknown_check_name_is_refused():
    with pytest.raises(ValueError):
        runner_compat.run_matrix(["collie"], checks=["definitely-not-a-check"])


# --- the offline matrix, for every registered runner ------------------------
@pytest.mark.parametrize("key", sorted(runner_registry.SPECS))
def test_offline_matrix_answers_every_column(key):
    """No model, no credential, no CLI required — and no column left unanswered."""
    report = runner_compat.run_matrix([key], checks=OFFLINE_CHECK_NAMES)
    cells = _cells(report, key)

    assert set(cells) == set(OFFLINE_CHECK_NAMES)
    spec = runner_registry.SPECS[key]
    for name, cell in cells.items():
        if spec.phase > CURRENT_PHASE and name == "admission":
            continue
        _assert_answered(cell, "%s.%s" % (key, name))

    if spec.phase > CURRENT_PHASE:
        # A future key gets one read-only vendor/protocol admission fingerprint.
        # Every implementation/isolation/live column remains behind the phase
        # gate, even if the binary happens to be installed on this host.
        admission = cells["admission"]
        assert admission["status"] in (PASS, FAIL, SKIP)
        if admission["status"] in (FAIL, SKIP):
            assert admission["detail"].strip()
        assert all(cell["status"] == SKIP for name, cell in cells.items()
                   if name != "admission")
        assert all("phase" in cell["detail"] for name, cell in cells.items()
                   if name != "admission")
        return

    # Every phase-1 runner, installed or not, has an answerable probe and an
    # environment policy: those two need nothing from the host at all.
    assert cells["probe"]["status"] == PASS
    assert cells["env_hygiene"]["status"] == PASS
    assert cells["billing"]["status"] == PASS
    assert cells["admission"]["status"] in (PASS, SKIP)

    if spec.kind == "native":
        assert cells["framing"]["status"] == SKIP
        assert cells["cancel"]["status"] == SKIP
        assert cells["double_control"]["status"] == PASS
        return

    if not runner_registry.probe(key).installed:
        assert cells["handshake"]["status"] == SKIP
        assert "not installed" in cells["handshake"]["detail"]
    # The three columns that drive the real runner objects through a scripted
    # transport need no CLI on PATH, which is the property that keeps them
    # meaningful in CI.
    for name in ("framing", "double_control", "cancel"):
        assert cells[name]["status"] == PASS, cells[name]["detail"]


@pytest.mark.parametrize("key", PHASE_1_EXTERNAL)
def test_framing_reports_a_runner_error_for_every_chaotic_frame(key):
    """Named separately from the sweep above: this is the column with teeth."""
    report = runner_compat.run_matrix([key], checks=["framing"])
    cell = _cells(report, key)["framing"]
    assert cell["status"] == PASS, cell["detail"]
    for case in ("invalid_json", "not_an_object", "nul_and_noise", "empty"):
        assert case in cell["detail"]
    assert "crlf=settled" in cell["detail"]
    if key in ("codex-app-server", "pi-rpc"):
        assert "no_trailing_lf=error_event" in cell["detail"]
    else:
        assert "no_trailing_lf=settled" in cell["detail"]
    assert "chunked=settled" in cell["detail"]


@pytest.mark.parametrize("key", PHASE_1_EXTERNAL)
def test_double_control_checks_both_the_start_and_the_resume_line(key):
    report = runner_compat.run_matrix([key], checks=["double_control"])
    cell = _cells(report, key)["double_control"]
    assert cell["status"] == PASS, cell["detail"]
    assert "resume argv checked" in cell["detail"]


@pytest.mark.parametrize("key", PHASE_1_EXTERNAL)
def test_cancel_column_kills_a_real_process_tree(key):
    """No CLI and no tokens, but a real gate, a real Job/process group and a real kill."""
    report = runner_compat.run_matrix([key], checks=["cancel"])
    cell = _cells(report, key)["cancel"]
    assert cell["status"] == PASS, cell["detail"]
    assert "tree extinction confirmed" in cell["detail"]
    assert "cancelled in" in cell["detail"]


def test_unknown_runner_is_a_row_of_skips_not_a_crash():
    report = runner_compat.run_matrix(["not-a-runner"], checks=OFFLINE_CHECK_NAMES)
    row = report["runners"]["not-a-runner"]
    assert row["error"] == "unknown runner"
    assert all(cell["status"] == SKIP for cell in row["checks"].values())
    assert all("unknown runner" in cell["detail"] for cell in row["checks"].values())


def test_a_broken_check_cannot_take_down_the_matrix(monkeypatch):
    """One cell explodes; the other nine still report."""
    def explode(_ctx):
        raise ZeroDivisionError("the check itself is broken")

    broken = dict(CHECKS)
    broken["billing"] = runner_compat.Check("billing", False, "boom", explode)
    monkeypatch.setattr(runner_compat, "CHECKS", broken)

    report = runner_compat.run_matrix(["collie"], checks=OFFLINE_CHECK_NAMES)
    cells = _cells(report, "collie")
    assert cells["billing"]["status"] == FAIL
    assert "ZeroDivisionError" in cells["billing"]["detail"]
    assert cells["probe"]["status"] == PASS
    assert report["totals"][FAIL] == 1


def test_docker_skips_every_external_row_until_phase_3():
    report = runner_compat.run_matrix(["collie", "codex-exec"], docker=True,
                                      checks=["billing"])
    assert report["docker"] is True
    assert _cells(report, "collie")["billing"]["status"] == PASS
    external = _cells(report, "codex-exec")["billing"]
    assert external["status"] == SKIP
    assert "phase 3" in external["detail"]


# --- live columns -----------------------------------------------------------
def test_live_columns_are_unverified_not_skipped_when_live_is_off():
    """UNVERIFIED and SKIP are different claims, and the registry treats them so."""
    report = runner_compat.run_matrix(["codex-exec"], checks=LIVE_CHECK_NAMES)
    for name in LIVE_CHECK_NAMES:
        cell = _cells(report, "codex-exec")[name]
        assert cell["status"] == UNVERIFIED
        assert "--live" in cell["detail"]
    assert report["live"] is False


def test_the_native_control_row_never_downgrades_collies_own_capabilities():
    """A compat report must not be able to tell the selector Collie cannot resume."""
    report = runner_compat.run_matrix(["collie"], checks=LIVE_CHECK_NAMES)
    for name in LIVE_CHECK_NAMES:
        cell = _cells(report, "collie")[name]
        assert cell["status"] == SKIP, cell
        assert "control row" in cell["detail"]


@pytest.mark.parametrize("key", PHASE_1_EXTERNAL)
def test_live_columns(key, tmp_path):
    """Real turns against a real CLI.  Off by default: this one spends tokens."""
    if not os.environ.get(LIVE_ENV):
        pytest.skip("live disabled")
    if not runner_registry.probe(key).installed:
        pytest.skip("not installed: %s" % key)

    report = runner_compat.run_matrix([key], live=True, checks=LIVE_CHECK_NAMES,
                                      workspace=str(tmp_path))
    cells = _cells(report, key)
    for name in LIVE_CHECK_NAMES:
        _assert_answered(cells[name], "%s.%s" % (key, name))
    assert cells["one_turn"]["status"] == PASS, cells["one_turn"]["detail"]


# --- the report -------------------------------------------------------------
def test_report_carries_the_host_facts_the_registry_reads():
    report = runner_compat.run_matrix(["collie"], checks=["billing"])
    assert report["schema"] == runner_compat.SCHEMA
    # `apply_compat_report` looks for exactly these three.
    assert report["os_name"] == os.name
    assert report["date"] and report["generated_at_utc"]
    assert isinstance(report["runners"]["collie"]["checks"]["billing"], dict)
    assert report["python_version"] and report["machine"] is not None
    # A hostname is often a person's name and explains nothing.
    assert "node" not in report and "hostname" not in report


def test_report_contains_no_prompt_no_token_no_address():
    report = runner_compat.run_matrix(None, checks=OFFLINE_CHECK_NAMES)
    encoded = json.dumps(report) + runner_compat.render_markdown(report)
    assert runner_compat._ONE_TURN_PROMPT not in encoded
    assert runner_compat._RESUME_PROMPT not in encoded
    assert runner_compat._ONE_TURN_MARKER not in encoded
    assert not runner_compat._SECRET_SCAN.search(encoded)
    assert not runner_compat._EMAIL.search(encoded)
    # The one path that would carry the operator's user name.
    assert os.path.expanduser("~") not in encoded


def test_write_report_writes_both_halves(tmp_path):
    report = runner_compat.run_matrix(["collie", "codex-exec"],
                                      checks=OFFLINE_CHECK_NAMES)
    target = tmp_path / "reports" / "runner-compat-2026-08-22.json"
    json_path, md_path = runner_compat.write_report(report, str(target))

    assert json_path.endswith(".json") and md_path.endswith(".md")
    assert os.path.dirname(json_path) == os.path.dirname(md_path)
    with open(json_path, encoding="utf-8") as handle:
        assert json.load(handle)["schema"] == runner_compat.SCHEMA

    markdown = open(md_path, encoding="utf-8").read()
    assert "| runner |" in markdown
    assert "`codex-exec`" in markdown
    for name in OFFLINE_CHECK_NAMES:
        assert name in markdown
    # The .md is what goes into docs/runners.md, so it states the limits the spec
    # declares rather than only the columns that happened to pass, and it names
    # what the run did *not* establish instead of leaving a reader to infer it
    # from an absent row.
    assert "Known limits declared by the spec" in markdown
    assert "Not established by this run" in markdown
    for where in report["unverified_reasons"]:
        assert where in markdown


def test_write_report_accepts_a_path_that_already_names_the_md(tmp_path):
    report = runner_compat.run_matrix(["collie"], checks=["billing"])
    json_path, md_path = runner_compat.write_report(report, str(tmp_path / "r.md"))
    assert os.path.basename(json_path) == "r.json"
    assert os.path.basename(md_path) == "r.md"


def test_write_report_refuses_to_publish_something_credential_shaped(tmp_path):
    report = runner_compat.run_matrix(["collie"], checks=["billing"])
    report["runners"]["collie"]["checks"]["billing"]["detail"] = (
        "Authorization: Bearer ya29.abcdefghijklmnop")
    with pytest.raises(runner_compat.CheckFailure):
        runner_compat.write_report(report, str(tmp_path / "leak.json"))
    assert not os.path.exists(str(tmp_path / "leak.json"))


def test_report_round_trips_into_probe_capabilities(tmp_path, _isolated_compat_state):
    """The closing half of the loop: an unverified column takes a capability away."""
    report = runner_compat.run_matrix(["codex-exec"], checks=CHECK_NAMES)
    json_path, _md = runner_compat.write_report(report, str(tmp_path / "compat.json"))

    applied = runner_registry.apply_compat_report(json_path)
    assert "codex-exec" in applied
    # `resume` was never run here, so the capability it speaks for is withdrawn
    # until a live report says otherwise.
    assert "session_resume" in applied["codex-exec"]
    assert runner_registry.probe("codex-exec").capabilities["session_resume"] is False
    assert runner_registry.compat_status("codex-exec").startswith("verified")
