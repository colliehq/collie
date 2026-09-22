"""The real pytest entry must reject failures the standalone runners collect."""
from pathlib import Path

pytest_plugins = ["pytester"]


def test_collected_legacy_checks_cannot_report_false_success(pytester):
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text("utf-8"))
    pytester.makepyfile(test_legacy='''
from pathlib import Path
_fails = []
def check(cond, msg):
    if not cond:
        _fails.append(msg)
def test_bad():
    check(False, "incorrect result")
    Path("cleanup-reached").write_text("yes")
def test_good_after_bad():
    check(True, "right result")
    assert Path("cleanup-reached").read_text() == "yes"
class _Skip(Exception): pass
def test_platform_unavailable():
    raise _Skip("not this OS")
''', test_reversed='''
ok = True
def check(name, cond):
    global ok
    ok = ok and cond
def test_bad():
    check(cond=False, name="model mismatch")
''')
    result = pytester.runpytest_subprocess("-q", "--tb=short")
    result.assert_outcomes(passed=1, failed=2, skipped=1)
    result.stdout.fnmatch_lines(["*incorrect result*", "*model mismatch*"])
