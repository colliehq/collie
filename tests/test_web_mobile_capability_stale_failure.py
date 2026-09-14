"""Observe a stale failure even if a later capability read repaints its damage.

The original mobile regression only checked the final worker state after its
barrier request. That request could make the worker available again, hiding a
brief incorrect fallback. Observe the page's existing DOM recorder across the
whole interval, using the same real phone gestures and staged HTTP fixture.
"""
from test_web_mobile_capability_sync import AFTER, phone  # noqa: F401
from test_web_ui_run_status import server, browser  # noqa: F401


def test_stale_failure_never_draws_an_unavailable_worker_after_a_newer_success(phone):
    reads = phone.reads
    phone.hold_next = True
    phone.open_run_options()
    phone.wait_for_reads(reads + 1)

    phone.answer = AFTER
    phone.close_run_options()
    phone.open_run_options()
    phone.wait_for_route("route-after", timeout=8000)

    phone.release(None)
    phone.barrier()

    drawn = phone.shown_since("route-after")
    assert drawn, "the current route must have been painted before releasing the failure"
    assert all(row["available"] == "true" for row in drawn), drawn
    assert all("Capabilities unavailable" not in row["note"] for row in drawn), drawn
