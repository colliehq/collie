"""A worker CLI that exits before reading its request must report its own reason.

Real child processes. On Windows, writing to the stdin of a child that has already exited raises
OSError(EINVAL) rather than BrokenPipeError (CPython bpo-19612); ``subprocess.communicate``
ignores both, and the bounded streaming path has to as well, so the child's stderr and exit code
reach the caller instead of a bare "[Errno 22] Invalid argument".
"""
import errno
import subprocess
import sys

import pytest

from harness.agent_runners import SubprocessRunner


def _exited_child():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('unknown flag --frobnicate'); sys.exit(2)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.wait(timeout=30)
    return proc


def test_a_child_that_exited_first_reports_its_stderr_and_exit_code():
    runner = SubprocessRunner()
    outcome = runner._streaming_communicate(_exited_child(), "x" * 200_000, 30.0, lambda _r: None)
    assert outcome.exit_code == 2
    assert "unknown flag --frobnicate" in outcome.stderr
    assert outcome.timed_out is False


class _Stdin:
    def __init__(self, exc):
        self.exc = exc

    def write(self, _text):
        raise self.exc

    def flush(self):
        pass

    def close(self):
        pass


class _Proc:
    """A child whose stdin write fails with a chosen error and that has already exited."""

    def __init__(self, exc):
        self.stdin = _Stdin(exc)
        real = _exited_child()
        self.stdout, self.stderr, self.returncode = real.stdout, real.stderr, real.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.mark.parametrize("exc", [BrokenPipeError(errno.EPIPE, "broken pipe"),
                                 OSError(errno.EINVAL, "Invalid argument")])
def test_both_shapes_of_a_gone_reader_are_tolerated(exc):
    outcome = SubprocessRunner()._streaming_communicate(_Proc(exc), "request", 30.0, lambda _r: None)
    assert outcome.exit_code == 2 and "unknown flag" in outcome.stderr


def test_any_other_write_error_still_surfaces():
    with pytest.raises(OSError) as raised:
        SubprocessRunner()._streaming_communicate(
            _Proc(OSError(errno.EACCES, "denied")), "request", 30.0, lambda _r: None)
    assert raised.value.errno == errno.EACCES

