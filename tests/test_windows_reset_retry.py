"""A connection reset on Windows is retried, as it is elsewhere.

A real run on 2026-08-01 stopped on "An existing connection was forcibly closed by the remote host"
(WinError 10054), classed "terminal: no known pattern", because the retryable patterns knew only the
POSIX wording ("connection reset").
"""
from harness.providers import classify_error


def test_windows_connection_resets_and_aborts_are_retryable():
    for text in (
            "URLError: <urlopen error [WinError 10054] An existing connection was forcibly closed "
            "by the remote host>",
            "ConnectionAbortedError: [WinError 10053] An established connection was aborted by the "
            "software in your host machine",
            "ConnectionResetError: [Errno 104] Connection reset by peer",
            "URLError: <urlopen error [WinError 10060] A connection attempt failed because the "
            "connected party did not properly respond after a period of time, or established "
            "connection failed because connected host has failed to respond>",
            "URLError: <urlopen error [WinError 10061] No connection could be made because the "
            "target machine actively refused it>",
            "IncompleteRead(0 bytes read)"):
        assert classify_error(text) == "retryable", text


def test_unrecognised_and_fatal_errors_still_are_not():
    assert classify_error("HTTP 401: invalid api key") == "terminal"
    assert classify_error("something nobody has seen") == "terminal"
    # Not speculatively widened: a machine that is offline stays offline for every retry.
    assert classify_error("URLError: <urlopen error [Errno 11001] getaddrinfo failed>") == "terminal"
