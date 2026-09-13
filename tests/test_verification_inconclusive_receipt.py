"""A check that exits 0 but cannot certify the tree is not a check that failed.

``run_verification_command`` has always recorded both facts separately:
``command_passed`` is the exit code, and ``passed`` additionally requires that
the workspace did not move under the check.  Every surface that reports the
result, though, only ever asked ``passed`` — so the second, much more common
case came out as::

    required check failed: cargo test (exit 0)

which is self-contradictory, and sends a person looking for a broken test that
does not exist.  It is not an edge case: any check that writes its own build
output into the workspace it grades changes the tree under itself and lands
here on *every green run* — ``cargo test`` into ``target/``, ``npm run build``
into ``dist/``, ``make build``.  ``detect_verification_commands`` proposes
exactly those commands.

The check is run for real here (a script that exits 0 and writes a build
artifact beside its own source), so the trigger is the product's own freshness
rule rather than a hand-written receipt.
"""
import json
import sys
from pathlib import Path

import pytest

from harness import cli, sessions, verification

from test_verification_cancel import (  # the real CLI/Web endings, already wired
    PYTHON, _WebHarness, _cli_args, _answered, _pin_cli, _spaces, _web_isolate,
)


def _build_output_check(workspace):
    """A green check that writes its own build artifact into the tree it grades."""
    (workspace / "src.txt").write_text("source", encoding="utf-8")
    (workspace / "build_check.py").write_text(
        "import os\n"
        "os.makedirs('dist', exist_ok=True)\n"
        "with open(os.path.join('dist', 'bundle.txt'), 'w') as fh:\n"
        "    fh.write('built')\n"
        "print('1 passed')\n", encoding="utf-8")
    return '"%s" build_check.py' % PYTHON


def _failing_check(workspace):
    (workspace / "fail_check.py").write_text(
        "print('1 failed')\n"
        "raise SystemExit(1)\n", encoding="utf-8")
    return '"%s" fail_check.py' % PYTHON


# --------------------------------------------------------------------------- #
# the receipt itself
# --------------------------------------------------------------------------- #
def test_a_green_check_that_writes_build_output_reads_as_inconclusive(tmp_path):
    """The real trigger: exit 0, tree moved, so the receipt certifies nothing."""
    workspace, _ = _spaces(tmp_path, "build")
    evidence = verification.run_verification_command(
        _build_output_check(workspace), str(workspace), timeout=120)

    assert evidence["executed"] is True and evidence["cancelled"] is False
    assert evidence["exit_code"] == 0
    assert evidence["command_passed"] is True, "the command really did exit 0"
    assert evidence["passed"] is False, "...and still certifies nothing"
    assert evidence["freshness"] == "changed_during_check"
    assert (workspace / "dist" / "bundle.txt").exists()

    assert verification.check_result_state(evidence) == "inconclusive"
    reason = verification.check_result_reason(evidence, "npm run build")
    assert "did not certify this result" in reason
    assert "the workspace changed while it was running" in reason
    assert "failed" not in reason, reason
    assert "(exit 0)" not in reason, reason


def test_a_real_failure_is_still_reported_as_a_failure(tmp_path):
    """The repair must not soften a red check into an excuse."""
    workspace, _ = _spaces(tmp_path, "red")
    evidence = verification.run_verification_command(
        _failing_check(workspace), str(workspace), timeout=120)

    assert evidence["command_passed"] is False and evidence["passed"] is False
    assert verification.check_result_state(evidence) == "failed"
    assert verification.check_result_reason(evidence, "pytest -q") == (
        "required check failed: pytest -q (exit 1)")


@pytest.mark.parametrize("evidence,state", [
    ({"executed": True, "passed": True, "command_passed": True}, "passed"),
    ({"executed": False, "passed": False, "command_passed": False}, "not_run"),
    # A stop is its own answer, even when the race left an exit code behind.
    ({"executed": True, "cancelled": True, "passed": False,
      "command_passed": True}, "cancelled"),
])
def test_the_other_receipt_states_keep_their_own_names(evidence, state):
    assert verification.check_result_state(evidence) == state


# --------------------------------------------------------------------------- #
# the Web ending
# --------------------------------------------------------------------------- #
def test_web_run_says_the_check_did_not_certify_rather_than_failed(monkeypatch,
                                                                   tmp_path):
    from harness import webapp

    _web_isolate(monkeypatch, tmp_path)
    workspace, _ = _spaces(tmp_path, "web-build")
    command = _build_output_check(workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(cli, "make_harness",
                        lambda *a, **kw: _WebHarness(kw.get("gate")))

    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    webapp.Handler._serve_stream(fake, {
        "q": ["bundle the release assets"], "session": ["web-inconclusive"],
        "intent": ["build"], "quality": ["balanced"], "verification": ["required"],
        "verify_command": [command], "verify_source": ["user"],
        "workspace": ["current"], "strategy": ["single"]})

    done = next(data for kind, data in events if kind == "done")
    evidence = done["verification_evidence"]
    assert evidence["command_passed"] is True and evidence["passed"] is False
    assert evidence["freshness"] == "changed_during_check"
    # The conversation's own line about the ending.
    assert "required check failed" not in (done["error"] or "")
    assert "did not certify this result" in done["error"]
    assert "the workspace changed while it was running" in done["error"]
    # Nothing about the verdict is softened: an uncertified check is still not a pass.
    assert done["canceled"] is False
    saved = sessions.load("web-inconclusive")
    assert saved["run_receipts"][-1]["verified"] is False
    # ...and the evidence travels with the receipt, so a reopened thread can say it too.
    assert saved["run_receipts"][-1]["verification_evidence"]["command_passed"] is True


# --------------------------------------------------------------------------- #
# the CLI ending
# --------------------------------------------------------------------------- #
def test_cli_run_reports_an_uncertified_check_in_its_own_words(monkeypatch,
                                                               tmp_path, capsys):
    workspace, _ = _spaces(tmp_path, "cli-build")
    command = _build_output_check(workspace)
    _pin_cli(monkeypatch, tmp_path, _answered)

    assert cli.cmd_run(_cli_args(tmp_path, cwd=str(workspace),
                                 verify_command=command)) == 1
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["verification_evidence"]["command_passed"] is True
    assert payload["verification_evidence"]["passed"] is False
    assert "required check failed" not in payload["error"]
    assert "did not certify this result" in payload["error"]
    # The answer the run did produce is still the answer.
    assert payload["answer"].startswith("rewrote the tokenizer")
    saved = sessions.load(payload["session"])
    assert saved["run_receipts"][-1]["verified"] is False
