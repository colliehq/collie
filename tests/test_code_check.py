"""The coding loop's one execution hand, and the rules that keep it narrow.

A Mission code slice could read and edit but never run anything, so it could not
see its own test failures until the slice was over and the host ran the check.
Every repair therefore cost a whole extra slice.  These tests pin the hand it
now has: exactly the pre-authorized command, exactly the bound workspace, no
arguments, real output back, and no way for the model to turn its own prose into
evidence.
"""
from __future__ import annotations

import os
import time

from harness.code_check import (VerificationCommandTool, agent_mutated_outside_checks,
                                check_window_mutated, reusable_receipt)
from harness.tools import ToolCtx


def _ctx(workspace, cancelled=None):
    return ToolCtx(cwd=str(workspace), project="mission-code", memory=None,
                   cancelled=cancelled)


def _tool(workspace, command, **kwargs):
    return VerificationCommandTool(command, str(workspace), **kwargs)


def _script(workspace, name, body):
    (workspace / name).write_text(body, encoding="utf-8")
    return "python " + name


# ── what the loop can actually do with it ───────────────────────────────────
def test_a_failing_check_hands_back_the_real_output_so_the_run_can_repair(tmp_path):
    command = _script(tmp_path, "check.py",
                      "import sys\nprint('FAIL: category filter is case-insensitive')\n"
                      "sys.exit(1)\n")
    tool = _tool(tmp_path, command)

    out = tool.run({}, _ctx(tmp_path))

    assert "VERIFICATION FAILED" in out
    assert "exited 1" in out
    # The actual failure text, not a summary of it: this is what makes the
    # difference between repairing now and buying another slice to find out.
    assert "category filter is case-insensitive" in out
    assert len(tool.receipts) == 1
    assert tool.receipts[0]["exit_code"] == 1
    assert tool.receipts[0]["passed"] is False


def test_a_passing_check_says_so_and_records_a_real_receipt(tmp_path):
    command = _script(tmp_path, "check.py", "print('Ran 12 tests\\nOK')\n")
    tool = _tool(tmp_path, command)

    out = tool.run(None, _ctx(tmp_path))

    assert "VERIFICATION PASSED" in out
    assert "Ran 12 tests" in out
    receipt = tool.receipts[0]
    assert receipt["passed"] is True and receipt["executed"] is True
    assert receipt["source"] == "mission_code_profile"
    assert receipt["invoked_by"] == "code_agent_tool"


# ── the ways it must refuse ─────────────────────────────────────────────────
def test_no_argument_can_choose_the_command_or_the_directory(tmp_path):
    marker = tmp_path / "ran.txt"
    command = _script(tmp_path, "check.py", "print('ok')\n")
    (tmp_path / "evil.py").write_text(
        "import pathlib\npathlib.Path('ran.txt').write_text('1')\n", encoding="utf-8")
    tool = _tool(tmp_path, command)

    for args in ({"command": "python evil.py"}, {"cwd": "/"},
                 {"args": ["--x"]}, {"": ""}, {"timeout": 1}):
        out = tool.run(args, _ctx(tmp_path))
        assert out.startswith("ERROR(verification): this tool takes no arguments")

    assert not marker.exists()
    assert tool.receipts == []


def test_a_loop_pointed_at_another_directory_never_starts_the_check(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    command = _script(workspace, "check.py",
                      "import pathlib\npathlib.Path('ran.txt').write_text('1')\n")
    tool = _tool(workspace, command)

    out = tool.run({}, _ctx(other))

    assert "not running in this Mission's bound workspace" in out
    assert not (workspace / "ran.txt").exists()
    assert tool.receipts == []


def test_a_mission_without_a_configured_command_has_nothing_to_run(tmp_path):
    tool = _tool(tmp_path, "   ")
    assert "no configured verification command" in tool.run({}, _ctx(tmp_path))
    assert tool.receipts == []


def test_the_number_of_host_command_runs_in_one_slice_is_bounded(tmp_path):
    command = _script(tmp_path, "check.py", "print('ok')\n")
    tool = _tool(tmp_path, command, max_runs=2)

    assert "VERIFICATION PASSED" in tool.run({}, _ctx(tmp_path))
    assert "VERIFICATION PASSED" in tool.run({}, _ctx(tmp_path))
    refused = tool.run({}, _ctx(tmp_path))

    assert "already run the check 2 times" in refused
    assert len(tool.receipts) == 2


def test_stop_ends_the_check_and_is_never_reported_as_a_verdict(tmp_path):
    command = _script(tmp_path, "check.py",
                      "import time, pathlib\ntime.sleep(6)\n"
                      "pathlib.Path('late.txt').write_text('too late')\n")
    tool = _tool(tmp_path, command, timeout_seconds=60)
    started = time.monotonic()

    out = tool.run({}, _ctx(tmp_path, cancelled=lambda: time.monotonic() - started > 1.0))

    assert "VERIFICATION STOPPED" in out
    assert "not a verdict about the code" in out
    receipt = tool.receipts[0]
    assert receipt["cancelled"] is True and receipt["passed"] is False
    assert receipt["process_tree_terminated"] is True
    time.sleep(7)
    assert not (tmp_path / "late.txt").exists()


def test_a_command_whose_tree_outlived_the_call_fences_the_run(tmp_path, monkeypatch):
    """The loop retires its journal fence when a tool returns; say so if unsure."""
    command = _script(tmp_path, "check.py", "print('ok')\n")
    monkeypatch.setattr(
        "harness.verification.run_verification_command",
        lambda *_a, **_k: {"command": command, "executed": True, "passed": True,
                           "command_passed": True, "exit_code": 0, "output": "ok",
                           "process_tree_terminated": False, "duration_ms": 1})
    tool = _tool(tmp_path, command)
    ctx = _ctx(tmp_path)

    tool.run({}, ctx)

    assert ctx.tool_effect_uncertain is True


def test_a_green_exit_on_bytes_that_moved_is_reported_as_inconclusive(tmp_path):
    """Exit zero on a tree that changed under the check certifies nothing."""
    command = _script(tmp_path, "check.py",
                      "import pathlib\npathlib.Path('grown.txt').write_text('x')\n")
    tool = _tool(tmp_path, command)

    out = tool.run({}, _ctx(tmp_path))

    assert "VERIFICATION INCONCLUSIVE" in out
    assert "Do not treat this as a pass" in out
    assert tool.receipts[0]["command_passed"] is True
    assert tool.receipts[0]["passed"] is False


# ── whose bytes are whose ───────────────────────────────────────────────────
def _window(before, after):
    return {"tree_digest": before, "post_tree_digest": after,
            "snapshot_complete": True, "post_snapshot_complete": True}


def test_a_checks_own_build_output_is_never_read_as_the_agents_patch():
    # The agent changed nothing; the check it ran wrote a coverage file.
    assert agent_mutated_outside_checks("A", [_window("A", "B")], "B") is False
    assert check_window_mutated([_window("A", "B")]) is True


def test_an_edit_after_a_clean_check_is_still_the_agents_patch():
    assert agent_mutated_outside_checks(
        "A", [_window("A", "A")], "B") is True


def test_an_edit_before_the_check_is_the_agents_patch():
    assert agent_mutated_outside_checks("A", [_window("B", "B")], "B") is True


def test_a_slice_that_only_ran_a_clean_check_changed_nothing():
    assert agent_mutated_outside_checks("A", [_window("A", "A")], "A") is False
    assert check_window_mutated([_window("A", "A")]) is False


def test_an_incomplete_snapshot_is_never_read_as_nothing_happened():
    partial = {"tree_digest": "A", "post_tree_digest": "A",
               "snapshot_complete": True, "post_snapshot_complete": False}
    assert agent_mutated_outside_checks("A", [partial], "A") is None
    assert check_window_mutated([partial]) is True
    assert agent_mutated_outside_checks("", [_window("A", "A")], "A") is None


# ── when the host may stand on a receipt it already has ─────────────────────
def _receipt(**kwargs):
    base = {"source": "mission_code_profile", "invoked_by": "code_agent_tool",
            "command": "python -m unittest -q", "executed": True, "cancelled": False,
            "ran_after_last_edit": True, "snapshot_complete": True,
            "post_snapshot_complete": True, "tree_digest": "now",
            "post_tree_digest": "now", "passed": True}
    base.update(kwargs)
    return base


def test_a_fresh_in_slice_receipt_for_exactly_these_bytes_can_stand_in():
    found = reusable_receipt([_receipt()], "python -m unittest -q", "now")
    assert found is not None and found["passed"] is True


def test_a_receipt_from_before_the_last_edit_never_stands_in():
    # The agent edited after the check: the tree is no longer the one it saw.
    assert reusable_receipt([_receipt()], "python -m unittest -q", "later") is None
    assert reusable_receipt(
        [_receipt(post_tree_digest="later")], "python -m unittest -q", "later") is None


def test_a_failing_receipt_is_confirmed_by_the_host_rather_than_inherited():
    """Reuse saves a duplicate answer; a red closes nothing, so it is re-run."""
    assert reusable_receipt([_receipt(passed=False, exit_code=1)],
                            "python -m unittest -q", "now") is None


def test_only_this_hosts_own_check_of_this_exact_command_stands_in():
    for bad in (_receipt(source="user"), _receipt(invoked_by=""),
                _receipt(command="python -m unittest"), _receipt(executed=False),
                _receipt(cancelled=True), _receipt(ran_after_last_edit=False),
                _receipt(passed=False), _receipt(snapshot_complete=False),
                _receipt(post_snapshot_complete=False)):
        assert reusable_receipt([bad], "python -m unittest -q", "now") is None
    assert reusable_receipt([], "python -m unittest -q", "now") is None
    assert reusable_receipt([_receipt()], "", "now") is None
    assert reusable_receipt([_receipt()], "python -m unittest -q", "") is None


def test_the_latest_matching_receipt_is_the_one_that_stands_in():
    stale = _receipt(exit_code=1, passed=False, tree_digest="old",
                     post_tree_digest="old")
    fresh = _receipt(exit_code=0, passed=True)
    assert reusable_receipt([stale, fresh], "python -m unittest -q",
                            "now")["exit_code"] == 0


def test_the_bound_workspace_is_resolved_once_and_not_taken_from_the_call(tmp_path):
    tool = _tool(tmp_path, "python check.py")
    assert tool.workspace == os.path.realpath(os.path.abspath(str(tmp_path)))
    assert tool.schema["properties"] == {}
    assert tool.schema["additionalProperties"] is False
    assert "python check.py" in tool.description
