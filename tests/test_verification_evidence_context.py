"""What the host knows about verification, and what it may say about it.

Observed on real Python coding benchmark instances: the workspace is one top-level
``test_solution.py`` importing ``unittest`` — no ``pyproject.toml``, no ``tests/`` directory.
The model ran ``python -m unittest -q``, the host watched it exit 0, the model then edited
``README.md``, and the post-edit reminder said the host had detected *no supported check* and
suggested parsing "the JSON or CSV".  Both halves were wrong about the same workspace:

  * discovery had no rule for a standard-library unittest layout, and
  * the fallback wording described a deliverable this task did not have, while the host had
    already watched a real check succeed in this very run.

These tests pin both halves, plus the boundary that matters most: naming a command is wording,
never evidence.  A remembered success must not make a stale finish acceptable, and a check that
was denied, killed, masked or told to collect nothing must never be remembered at all.
"""
import json
import os
import sys
import unittest

import pytest

from harness import cli, loop, verification
from harness.providers import Completion, ToolCall
from _util import _ScriptProvider


_UNITTEST_SUITE = """\
import unittest

from solution import add


class AddTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)


if __name__ == "__main__":
    unittest.main()
"""


def _benchmark_workspace(tmp_path, name="bench"):
    """The observed shape: top-level suite + solution module + prose, no project metadata."""
    ws = tmp_path / name
    ws.mkdir()
    (ws / "solution.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "test_solution.py").write_text(_UNITTEST_SUITE, encoding="utf-8")
    (ws / "README.md").write_text("# task\n", encoding="utf-8")
    return ws


def _commands(cwd):
    return [c["command"] for c in verification.detect_verification_commands(str(cwd))]


# ── discovery: a unittest layout is a check, a filename is not ───────────────

def test_toplevel_unittest_suite_is_discovered(tmp_path):
    ws = _benchmark_workspace(tmp_path)

    candidates = verification.detect_verification_commands(str(ws))

    assert candidates and candidates[0]["command"] == "python -m unittest -q"
    assert candidates[0]["kind"] == "test"
    assert "test_solution.py" in candidates[0]["source"], "say which file the evidence came from"


def test_a_test_shaped_filename_alone_is_not_a_suite(tmp_path):
    """The rule is "this module defines a TestCase", not "this file is called test_*"."""
    ws = tmp_path / "data-ws"
    ws.mkdir()
    (ws / "test_fixtures.py").write_text(
        "# fixtures for the unittest suite we do not have yet\n"
        'ROWS = [{"id": 1}]\n', encoding="utf-8")

    assert _commands(ws) == []


def test_importing_unittest_without_a_testcase_is_not_a_suite(tmp_path):
    ws = tmp_path / "helper-ws"
    ws.mkdir()
    (ws / "test_helpers.py").write_text(
        "import unittest\n\n"
        "def make_loader():\n    return unittest.TestLoader()\n", encoding="utf-8")

    assert _commands(ws) == []


def test_unparseable_module_is_not_a_suite(tmp_path):
    """Static parsing fails closed; a half-written file must not propose a command."""
    ws = tmp_path / "broken-ws"
    ws.mkdir()
    (ws / "test_broken.py").write_text(
        "import unittest\nclass T(unittest.TestCase:\n", encoding="utf-8")

    assert _commands(ws) == []


def test_discovery_does_not_execute_the_suite_it_reads(tmp_path):
    """Detection reads and parses. It must never import project code to decide."""
    ws = tmp_path / "effect-ws"
    ws.mkdir()
    sentinel = ws / "executed.txt"
    (ws / "test_effect.py").write_text(
        "import pathlib\nimport unittest\n\n"
        "pathlib.Path(__file__).with_name('executed.txt').write_text('ran')\n\n"
        "class T(unittest.TestCase):\n    def test_x(self):\n        pass\n",
        encoding="utf-8")

    assert _commands(ws) == ["python -m unittest -q"]
    assert not sentinel.exists(), "the module body ran; discovery must be static"


def test_from_import_and_aliased_forms_are_recognized(tmp_path):
    ws = tmp_path / "alias-ws"
    ws.mkdir()
    (ws / "test_alias.py").write_text(
        "import unittest as ut\n\n"
        "class A(ut.IsolatedAsyncioTestCase):\n    async def test_x(self):\n        pass\n",
        encoding="utf-8")
    other = tmp_path / "from-ws"
    other.mkdir()
    (other / "test_from.py").write_text(
        "from unittest import TestCase\n\n"
        "class B(TestCase):\n    def test_x(self):\n        pass\n", encoding="utf-8")

    assert _commands(ws) == ["python -m unittest -q"]
    assert _commands(other) == ["python -m unittest -q"]


def test_a_symlink_out_of_the_workspace_is_not_this_project_s_check(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_foreign.py").write_text(_UNITTEST_SUITE, encoding="utf-8")
    ws = tmp_path / "linked-ws"
    ws.mkdir()
    try:
        os.symlink(str(outside / "test_foreign.py"), str(ws / "test_foreign.py"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this OS/account cannot create symlinks")

    assert _commands(ws) == [], "a link out of the tree is somebody else's suite"


def test_a_symlink_inside_the_workspace_still_counts(tmp_path):
    ws = _benchmark_workspace(tmp_path, "inner-link")
    (ws / "test_solution.py").rename(ws / "suite_impl.py")
    try:
        os.symlink(str(ws / "suite_impl.py"), str(ws / "test_solution.py"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this OS/account cannot create symlinks")

    assert _commands(ws) == ["python -m unittest -q"]


# ── discovery answers to the real loader, not to a shape that looks like one ──
#
# The detector's claim is "``python -m unittest -q`` will collect something here".  The only
# authority on that is ``unittest`` itself, so these cases are checked against the standard
# library's own loader rather than against a second opinion written next to the first.  Two of
# them are the regressions: an empty ``TestCase`` and a ``TestCase`` defined inside a function
# both READ as suites and both make the real loader collect zero tests.

def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _loader_test_count(directory):
    """How many tests ``python -m unittest`` discovery really collects in ``directory``.

    Imports the fixture modules — that is the point of the comparison — so every fixture below
    is inert module text.  ``sys.path``/``sys.modules`` are restored so one fixture cannot
    shadow the next.
    """
    saved_path, saved_modules = list(sys.path), set(sys.modules)
    try:
        suite = unittest.TestLoader().discover(str(directory))
        return len([t for t in _flatten(suite)
                    if type(t).__name__ not in ("_FailedTest", "ModuleImportFailure")])
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - saved_modules:
            sys.modules.pop(name, None)


# (filename, source, how many tests unittest really collects)
_LOADER_CASES = [
    ("a_real_case", "test_real.py",
     "import unittest\n\n"
     "class T(unittest.TestCase):\n    def test_x(self):\n        pass\n", 1),
    ("an_empty_case", "test_empty.py",
     "import unittest\n\nclass T(unittest.TestCase):\n    pass\n", 0),
    ("a_case_nested_in_a_function", "test_nested.py",
     "import unittest\n\n"
     "def build():\n"
     "    class T(unittest.TestCase):\n        def test_x(self):\n            pass\n"
     "    return T\n", 0),
    ("a_case_nested_in_a_class", "test_inner.py",
     "import unittest\n\n"
     "class Outer:\n"
     "    class T(unittest.TestCase):\n        def test_x(self):\n            pass\n", 0),
    ("a_non_callable_test_attribute", "test_attr.py",
     "import unittest\n\nclass T(unittest.TestCase):\n    test_x = 5\n", 0),
    ("a_local_base_class_with_the_tests", "test_inherit.py",
     "import unittest\n\n"
     "class Base(unittest.TestCase):\n    def test_x(self):\n        pass\n\n"
     "class Child(Base):\n    pass\n", 2),
    ("a_local_mixin_with_the_tests", "test_mixin.py",
     "import unittest\n\n"
     "class Mixin:\n    def test_x(self):\n        pass\n\n"
     "class T(Mixin, unittest.TestCase):\n    pass\n", 1),
    ("an_async_case", "test_async.py",
     "import unittest as ut\n\n"
     "class T(ut.IsolatedAsyncioTestCase):\n    async def test_x(self):\n        pass\n", 1),
    ("an_aliased_from_import", "test_from.py",
     "from unittest import TestCase as TC\n\n"
     "class T(TC):\n    def test_x(self):\n        pass\n", 1),
    ("a_module_unittest_cannot_import", "test-solution.py",
     "import unittest\n\n"
     "class T(unittest.TestCase):\n    def test_x(self):\n        pass\n", 0),
    ("a_fixture_module_that_is_not_a_suite", "test_data.py",
     "# fixtures for a unittest suite\nROWS = [1, 2]\n", 0),
]


@pytest.mark.parametrize("label,filename,source,expected",
                         _LOADER_CASES, ids=[c[0] for c in _LOADER_CASES])
def test_detection_agrees_with_the_standard_library_loader(tmp_path, label, filename,
                                                           source, expected):
    ws = tmp_path / label
    ws.mkdir()
    (ws / filename).write_text(source, encoding="utf-8")

    collected = _loader_test_count(ws)
    assert collected == expected, "fixture assumption about unittest itself is wrong"

    detected = _commands(ws)
    assert detected == (["python -m unittest -q"] if expected else []), (
        "detector and loader disagree about %s" % label)


def test_a_zero_test_module_beside_a_real_one_still_proposes_the_command(tmp_path):
    """Being conservative per module must not lose a suite that is genuinely there."""
    ws = tmp_path / "mixed"
    ws.mkdir()
    (ws / "test_empty.py").write_text(
        "import unittest\n\nclass E(unittest.TestCase):\n    pass\n", encoding="utf-8")
    (ws / "test_real.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_x(self):\n        pass\n", encoding="utf-8")

    assert _loader_test_count(ws) == 1
    assert _commands(ws) == ["python -m unittest -q"]


def test_a_base_class_this_module_cannot_see_is_not_guessed_at(tmp_path):
    """A dynamic/imported base may or may not be a TestCase; silence beats a wrong command."""
    ws = tmp_path / "foreign-base"
    ws.mkdir()
    (ws / "test_foreign_base.py").write_text(
        "import unittest\n\nfrom base_helpers import MyBase\n\n"
        "class T(MyBase):\n    def test_x(self):\n        pass\n", encoding="utf-8")

    assert _commands(ws) == []


def test_the_candidate_scan_is_bounded_before_sorting(tmp_path, monkeypatch):
    """The cap must apply while scanning, not to a sorted copy of every entry."""
    ws = tmp_path / "many"
    ws.mkdir()
    (ws / "test_real.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_x(self):\n        pass\n", encoding="utf-8")
    for i in range(40):
        (ws / ("test_pad_%03d.py" % i)).write_text("PAD = 1\n", encoding="utf-8")
    monkeypatch.setattr(verification, "_UNITTEST_CANDIDATE_FILES", 5)
    seen = []
    real_parse = verification._defines_unittest_case
    monkeypatch.setattr(verification, "_defines_unittest_case",
                        lambda src: seen.append(src) or real_parse(src))

    verification.detect_verification_commands(str(ws))

    assert len(seen) <= 5, "more files were read than the candidate cap allows"


# ── discovery: nothing that already worked may change ────────────────────────

def test_pytest_layout_still_wins_over_the_unittest_fallback(tmp_path):
    """pytest collects unittest suites; a project that declares a layout owns its runner."""
    ws = _benchmark_workspace(tmp_path, "pytest-ws")
    (ws / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    assert _commands(ws) == ["python -m pytest -q"]


def test_tests_directory_still_wins_over_the_unittest_fallback(tmp_path):
    ws = _benchmark_workspace(tmp_path, "tests-dir-ws")
    (ws / "tests").mkdir()

    assert _commands(ws) == ["python -m pytest -q"]


def test_other_ecosystems_are_untouched(tmp_path):
    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."}}), encoding="utf-8")
    rust = tmp_path / "rust"
    rust.mkdir()
    (rust / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    go = tmp_path / "go"
    go.mkdir()
    (go / "go.mod").write_text("module x\n", encoding="utf-8")
    make = tmp_path / "make"
    make.mkdir()
    (make / "Makefile").write_text("test:\n\techo hi\n", encoding="utf-8")

    assert _commands(node) == ["npm run test", "npm run lint"]
    assert _commands(rust) == ["cargo test"]
    assert _commands(go) == ["go test ./..."]
    assert _commands(make) == ["make test"]


# ── which command the host may claim it watched succeed ─────────────────────

def _observed(command, out="OK\n.\n----\nRan 1 test\n", name="bash"):
    return loop._host_observed_check(name, {"command": command}, out)


def test_a_successful_host_run_is_remembered():
    assert _observed("python -m unittest -q") == "python -m unittest -q"


def test_a_failed_denied_or_interrupted_check_is_never_remembered():
    """Exit status and the host's own prefixes decide. Nothing else may."""
    assert _observed("python -m unittest -q", "[exit 1]\nFAILED (failures=1)") == ""
    assert _observed("python -m unittest -q", "DENIED: command not allowed") == ""
    assert _observed("python -m unittest -q",
                     "ERROR: timed out after 120s (killed)") == ""
    assert _observed("python -m unittest -q",
                     "ERROR: canceled before the command started") == ""


def test_an_exit_masked_or_collect_only_check_is_never_remembered():
    for masked in ("python -m unittest -q || true",
                   "python -m unittest -q | tail -5",
                   "python -m unittest -q; echo done",
                   "python -m unittest -q &",
                   "python -m pytest -q --collect-only"):
        assert _observed(masked) == "", masked


def test_a_result_that_is_not_host_text_is_not_an_observation():
    """``None`` is what a call that produced no host text looks like. It is not a success.

    bash mints no ``ExecReceipt``, so the ONLY outcome information it supplies is the text the
    host prepends. A result that is not that text carries no outcome at all.
    """
    for empty in (None, b"OK\n", 0, ["OK"], {"out": "OK"}, ""):
        assert loop._host_observed_check("bash", {"command": "python -m unittest -q"},
                                         empty) == "", repr(empty)


def test_a_canceled_run_is_not_a_check_that_worked():
    """The cancellation path fills unrun calls in with this exact text."""
    assert _observed("python -m unittest -q", "CANCELED: run stopped before execution") == ""
    assert _observed("python -m unittest -q",
                     "ERROR: command canceled by the user after 3.2s — the owned process tree "
                     "was stopped.\npartial output") == ""
    assert _observed("python -m unittest -q",
                     "[WARNING: this command finished, but a child could not be handed over.]\n"
                     "Ran 3 tests\nOK") == "", "an unaccountable effect is not a clean check"


def test_a_green_run_that_collected_nothing_is_not_a_check_that_worked():
    """`Ran 0 tests ... OK` exits zero and proves nothing; quoting it back is worse than
    falling back to detection, because it names a command known to collect nothing."""
    zero = ("\n----------------------------------------------------------------------\n"
            "Ran 0 tests in 0.000s\n\nOK\n")
    assert _observed("python -m unittest -q", zero) == ""
    assert _observed("python -m pytest -q", "no tests ran in 0.01s") == ""
    assert _observed("python -m pytest -q", "collected 0 items\n\nno tests ran") == ""
    assert _observed("python -m unittest -q", "(no output)") == ""
    # The same runner with real tests is still remembered.
    assert _observed("python -m unittest -q",
                     "...\n----\nRan 3 tests in 0.01s\n\nOK\n") == "python -m unittest -q"


def test_only_the_bash_tool_s_host_minted_status_counts():
    assert _observed("python -m unittest -q", name="run_in_env") == ""
    assert _observed("echo python -m unittest -q") == ""
    assert _observed("python solution.py") == "", "a script run is not a check command"


# ── the reminder ────────────────────────────────────────────────────────────

def test_unittest_workspace_reminder_names_the_unittest_command(tmp_path):
    nudge = loop.verify_nudge_for(str(_benchmark_workspace(tmp_path)), ["solution.py"])

    assert "`python -m unittest -q`" in nudge
    assert "pytest" not in nudge
    assert "did not detect a supported test" not in nudge


def test_fallback_reminder_is_task_neutral(tmp_path):
    """A README task must not be answered with a JSON/CSV recipe."""
    ws = tmp_path / "prose"
    ws.mkdir()
    (ws / "README.md").write_text("# notes\n", encoding="utf-8")

    nudge = loop.verify_nudge_for(str(ws), ["README.md"])

    for irrelevant in ("JSON", "CSV", "fields, values and ordering"):
        assert irrelevant not in nudge, "the reminder invented a deliverable shape"
    assert "README.md" in nudge
    assert "writes nothing into this workspace" in nudge
    assert "does not prove the request was satisfied" in nudge and "remain unverified" in nudge
    assert "hiding its exit status" in nudge and "in the user's requested format" in nudge


def test_an_executed_check_outranks_static_discovery(tmp_path):
    """Discovery guesses from markers; an execution happened. The execution names the command."""
    ws = _benchmark_workspace(tmp_path, "both")
    (ws / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    nudge = loop.verify_nudge_for(str(ws), ["README.md"], ["python -m unittest -q"])

    assert "`python -m unittest -q`" in nudge
    assert "pytest" not in nudge
    assert "edited files since that run" in nudge, "say why it must run again"
    assert "hiding its exit status" in nudge and "in the user's requested format" in nudge


def test_a_check_from_another_directory_is_not_quoted_here(tmp_path):
    """A success is bound to where it ran; a run that moved on has no observation to reuse."""
    ws = _benchmark_workspace(tmp_path, "here")
    elsewhere = str(tmp_path / "somewhere-else")

    moved = loop.verify_nudge_for(str(ws), ["README.md"],
                                  [(elsewhere, "python -m pytest -q")])
    same = loop.verify_nudge_for(str(ws), ["README.md"],
                                 [(str(ws), "python -m pytest -q")])

    assert "already ran successfully" not in moved
    assert "`python -m unittest -q`" in moved, "detection still describes this workspace"
    assert "already ran successfully" in same and "`python -m pytest -q`" in same


def test_an_unquotable_remembered_command_falls_back_to_detection(tmp_path):
    ws = _benchmark_workspace(tmp_path, "long")

    nudge = loop.verify_nudge_for(str(ws), ["solution.py"], ["python -m unittest " + "-k x" * 200])

    assert "`python -m unittest -q`" in nudge


# ── through the loop ────────────────────────────────────────────────────────

class _FakeBash:
    """The bash tool's host-minted result shape, without running a shell in a unit test."""
    name = "bash"
    description = "fake"
    tier = "always"
    schema = {"type": "object", "properties": {"command": {"type": "string"}},
              "required": ["command"]}

    def __init__(self, rc=0, body="Ran 1 test in 0.001s\n\nOK\n"):
        self.rc = rc
        self.body = body
        self.commands = []

    def provider_schema(self):
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}

    def run(self, args, ctx):
        self.commands.append(args.get("command"))
        body = self.body
        return body if self.rc == 0 else "[exit %d]\n%s" % (self.rc, body)


def _harness(tmp_path, monkeypatch, cwd, script, rc=0, body="Ran 1 test in 0.001s\n\nOK\n"):
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    h = cli.make_harness(str(cwd), provider="mock", project="verify-evidence", embed="hash")
    h.provider = _ScriptProvider(script)
    h.registry.register(_FakeBash(rc, body))
    h.max_turns = 10
    return h


def _reminders(result):
    return [m["content"] for m in result.messages
            if m.get("kind") == "verification_reminder"]


def _observed_run_script():
    """Edit the solution, run the suite, edit the README, then try to finish."""
    return [
        Completion(tool_calls=[ToolCall("e1", "edit_file", {
            "path": "solution.py", "old_string": "a + b", "new_string": "a + b  # fixed"})],
            stop_reason="tool_use"),
        Completion(tool_calls=[ToolCall("b1", "bash", {
            "command": "python -m unittest -q"})], stop_reason="tool_use"),
        Completion(tool_calls=[ToolCall("e2", "edit_file", {
            "path": "README.md", "old_string": "# task", "new_string": "# task\n\nDone."})],
            stop_reason="tool_use"),
        Completion(text="Fixed the solution and documented it.", stop_reason="end_turn"),
        Completion(text="Fixed, documented, and the suite is green.", stop_reason="end_turn"),
    ]


def test_loop_reminder_after_a_later_edit_names_the_check_that_already_ran(tmp_path, monkeypatch):
    """The observed sequence, end to end: no 'no supported checks', no JSON/CSV."""
    ws = _benchmark_workspace(tmp_path)
    h = _harness(tmp_path, monkeypatch, ws, _observed_run_script())
    try:
        result = h.run("bench", "Fix add() and note it in the README.")
    finally:
        h.memory.close()
        h.recorder.close()

    reminders = _reminders(result)
    assert len(reminders) == 1, "still one advisory nudge, not a new loop"
    assert "`python -m unittest -q`" in reminders[0]
    assert "already ran successfully" in reminders[0], "it was executed, not merely detected"
    assert "did not detect a supported test" not in reminders[0]
    assert "JSON" not in reminders[0] and "CSV" not in reminders[0]
    assert not result.error


def test_a_failing_check_is_not_offered_back_as_one_that_worked(tmp_path, monkeypatch):
    ws = _benchmark_workspace(tmp_path, "red")
    h = _harness(tmp_path, monkeypatch, ws, _observed_run_script(), rc=1)
    try:
        result = h.run("bench-red", "Fix add() and note it in the README.")
    finally:
        h.memory.close()
        h.recorder.close()

    reminders = _reminders(result)
    assert reminders, "an unverified finish is still pushed back on"
    assert "already ran successfully" not in reminders[0]
    # Detection still has something true to say about this workspace.
    assert "`python -m unittest -q`" in reminders[0]


def test_loop_does_not_offer_back_a_run_that_collected_no_tests(tmp_path, monkeypatch):
    """End to end: a zero-exit run that collected nothing falls back to detection wording."""
    ws = _benchmark_workspace(tmp_path, "zero")
    h = _harness(tmp_path, monkeypatch, ws, _observed_run_script(),
                 body="\n----\nRan 0 tests in 0.000s\n\nOK\n")
    try:
        result = h.run("bench-zero", "Fix add() and note it in the README.")
    finally:
        h.memory.close()
        h.recorder.close()

    reminders = _reminders(result)
    assert reminders
    assert "already ran successfully" not in reminders[0]
    assert "`python -m unittest -q`" in reminders[0], "detection still describes this workspace"


def test_remembering_a_check_does_not_satisfy_required_verification(tmp_path, monkeypatch):
    """The load-bearing boundary: wording moved, the gate did not.

    The suite really did pass here — before the README edit.  Required still refuses the finish,
    because nothing has run against the bytes that exist now.
    """
    ws = _benchmark_workspace(tmp_path, "required")
    h = _harness(tmp_path, monkeypatch, ws, _observed_run_script())
    cli.configure_run_options(h, verification="required")
    h.max_turns = 6
    try:
        result = h.run("bench-required", "Fix add() and note it in the README.")
    finally:
        h.memory.close()
        h.recorder.close()

    assert h.verify_gate is True and h.self_verify is True
    assert result.verified is False
    assert "verification required" in result.error
    assert _reminders(result), "Required still pushes back before giving up"


def test_caller_supplied_verify_nudge_still_wins(tmp_path, monkeypatch):
    ws = _benchmark_workspace(tmp_path, "pinned")
    h = _harness(tmp_path, monkeypatch, ws, _observed_run_script())
    h.verify_nudge = "RUN THE PINNED REPRO."
    try:
        result = h.run("bench-pinned", "Fix add() and note it in the README.")
    finally:
        h.memory.close()
        h.recorder.close()

    assert _reminders(result) == ["RUN THE PINNED REPRO."]


if __name__ == "__main__":  # pragma: no cover - convenience for the pytest-less path
    raise SystemExit(pytest.main([__file__, "-q"]))
