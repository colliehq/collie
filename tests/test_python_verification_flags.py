"""Interpreter flags must not hide real suites or turn other Python into a suite."""
import pytest

from harness import cli, loop
from harness.providers import Completion, ToolCall
from _util import _ScriptProvider


@pytest.mark.parametrize('command', [
    'python -B -m unittest -q',
    'python -u -B -m pytest -q',
    'python3 -I -m unittest discover',
    'python3.14 -BE -m unittest -q',
    'python.exe -B -m pytest -q',
    'py -3 -B -m unittest -q',
    'py -3.14 -m unittest -q',
])
def test_interpreter_options_preserve_test_runner_identity(command):
    assert loop._is_test_runner_cmd(command)
    assert loop._is_asserting_cmd(command)


@pytest.mark.parametrize('command', [
    'python -c "print(\'python -B -m unittest\')"',
    'python -B -m unittest -q | tail -1',
    'python -B -m unittest -q; echo done',
    'python -B -m unittest -q || true',
    'python -B -m pytest --collect-only',
    'python -B -m pytest --co',
    'python -m unittest --help',
    'pytest --fixtures',
    'python -m pytest --version',
    'python -h -m unittest',
    'python -V -m unittest',
    'python -B -m json.tool result.json',
    'python -B script.py',
])
def test_non_checks_and_masked_results_stay_non_evidence(command):
    assert not loop._is_test_runner_cmd(command)
    assert not loop._is_asserting_cmd(command)


@pytest.mark.parametrize('command', [
    'python -B -m pytest --co', 'python -m unittest --help', 'pytest --fixtures',
    'python -m pytest --version', 'python -m pytest --collect-only',
])
def test_runner_listing_does_not_fall_back_to_generic_reproduction(command):
    assert not loop._is_repro_cmd('bash', {'command': command})


def test_verbose_runner_and_quoted_test_selector_remain_real_checks():
    assert loop._is_test_runner_cmd('python -B -m unittest -v')
    assert loop._is_test_runner_cmd('pytest -k "literal --co marker"')


def test_required_verification_accepts_real_unittest_without_cache_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'DATA', str(tmp_path/'data'))
    project = tmp_path/'project'
    project.mkdir()
    (project/'solution.py').write_text('VALUE = 0\n', encoding='utf-8')
    (project/'test_solution.py').write_text(
        'import unittest\nfrom solution import VALUE\n'
        'class Tests(unittest.TestCase):\n'
        '    def test_value(self): self.assertEqual(VALUE, 1)\n', encoding='utf-8')
    h = cli.make_harness(str(project), provider='mock', embed='hash')
    cli.configure_run_options(h, intent='build', quality='balanced', verification='required')
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall('edit', 'edit_file',
            {'path': 'solution.py', 'old_string': 'VALUE = 0', 'new_string': 'VALUE = 1'})]),
        Completion(tool_calls=[ToolCall('check', 'bash', {'command': 'python -B -m unittest -q'})]),
        Completion(text='Fixed VALUE; the unittest passed.'),
    ])
    h.max_turns = 3
    try:
        result = h.run('flags', 'Fix VALUE and verify without creating caches.', consolidate=False)
    finally:
        h.memory.close()
        h.recorder.close()
    assert result.success and result.verified
    assert result.model_calls == 3
    assert not (project/'__pycache__').exists()
    assert not any(m.get('kind') == 'verification_reminder' for m in result.messages)


@pytest.mark.parametrize('verification', ['auto', 'required'])
def test_zero_collected_unittests_do_not_verify_an_edit(tmp_path, monkeypatch, verification):
    monkeypatch.setattr(cli, 'DATA', str(tmp_path/'data'))
    project = tmp_path/'project'
    project.mkdir()
    (project/'test_empty.py').write_text(
        'import unittest\nclass Empty(unittest.TestCase):\n    pass\n', encoding='utf-8')
    h = cli.make_harness(str(project), provider='mock', embed='hash')
    cli.configure_run_options(h, intent='build', quality='balanced', verification=verification)
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall('write', 'write_file', {'path': 'value.txt', 'content': 'changed'})]),
        Completion(tool_calls=[ToolCall('empty-check', 'bash', {'command': 'python -m unittest -q'})]),
        Completion(text='Changed the file; no test assertion was executed.'),
    ])
    h.max_turns = 3
    try:
        result = h.run('empty-check', 'Change value.txt and verify it.', consolidate=False)
    finally:
        h.memory.close()
        h.recorder.close()
    assert any('Ran 0 tests' in str(m.get('content')) for m in result.messages if m.get('role') == 'tool')
    assert not result.verified
    if verification == 'required':
        assert not result.success and result.error


@pytest.mark.parametrize('verification', ['auto', 'required'])
def test_legacy_zero_exit_empty_suite_does_not_verify(tmp_path, monkeypatch, verification):
    """Python 3.10's unittest can return success for zero tests; 3.14 returns 5.

    Exercise the legacy successful process-output boundary without pretending
    this machine has an older Python installed.
    """
    monkeypatch.setattr(cli, 'DATA', str(tmp_path/'data'))
    h = cli.make_harness(str(tmp_path), provider='mock', embed='hash')
    cli.configure_run_options(h, intent='build', verification=verification)
    h.registry.get('bash').run = lambda args, ctx: '[stderr] -------\nRan 0 tests in 0.000s\n\nOK'
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall('write', 'write_file', {'path': 'value.txt', 'content': 'changed'})]),
        Completion(tool_calls=[ToolCall('check', 'bash', {'command': 'python -m unittest -q'})]),
        Completion(text='Changed the file; the suite contained no tests.'),
    ])
    h.max_turns = 3
    try:
        result = h.run('legacy-zero', 'Change and verify value.txt.', consolidate=False)
    finally:
        h.memory.close()
        h.recorder.close()
    assert not result.verified
    if verification == 'required':
        assert not result.success
