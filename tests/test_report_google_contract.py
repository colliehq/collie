"""The morning report's contract with the real harness.google_connect.

The report's own tests use a fake of the connector.  A fake only proves something while it looks
like the real thing, so this file imports the real module and checks the parts the report relies
on: the functions and the arguments it passes them, the error types it maps to a reason, the
fields of status() it reads, and that there is still no way to send mail.  Nothing here reaches
Google: status() is asked about an empty state directory, which it answers offline.
"""
import inspect

from harness import google_connect as gc
from harness import report_sources as src

KW = inspect.Parameter.KEYWORD_ONLY


def _params(fn):
    return inspect.signature(fn).parameters


def _positional(fn):
    return [name for name, p in _params(fn).items()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]


def test_the_calls_the_report_makes_match_the_connectors_signatures():
    assert _params(gc.status)["check"].kind is KW and _params(gc.status)["state_dir"].kind is KW
    # gmail_search(GMAIL_QUERY, GMAIL_LIMIT, state_dir=...)
    assert _positional(gc.gmail_search)[:2] == ["query", "max_results"]
    assert _params(gc.gmail_search)["state_dir"].kind is KW
    assert 1 <= src.GMAIL_LIMIT <= gc.MAX_SEARCH_RESULTS
    # gmail_thread(thread_id, state_dir=...)
    assert _positional(gc.gmail_thread) == ["thread_id"]
    assert _params(gc.gmail_thread)["state_dir"].kind is KW
    # gmail_create_draft(thread_id, to, subject, body, in_reply_to, references, state_dir=...)
    assert _positional(gc.gmail_create_draft) == ["thread_id", "to", "subject", "body",
                                                  "in_reply_to", "references"]
    assert _params(gc.gmail_create_draft)["state_dir"].kind is KW
    # calendar_events(time_min, time_max, CALENDAR_LIMIT, state_dir=...)
    assert _positional(gc.calendar_events)[:3] == ["time_min", "time_max", "max_results"]
    assert _params(gc.calendar_events)["state_dir"].kind is KW
    assert src.CALENDAR_LIMIT <= gc.MAX_EVENTS


def test_the_times_the_calendar_is_asked_for_are_ones_it_accepts():
    assert gc._RFC3339.match(src._rfc3339(1790690520.0))


def test_every_error_the_report_names_is_the_connectors_own():
    for name in ("NotConfigured", "NotConnected", "NeedsReconnect", "MissingScope",
                 "GoogleAPIError"):
        assert issubclass(getattr(gc, name), gc.GoogleError), name
    cases = ((gc.NeedsReconnect("x"), "reconnect Google"),
             (gc.NotConnected("x"), "Google isn't connected yet"),
             (gc.NotConfigured("x"), "Google isn't set up on this computer"),
             (gc.MissingScope(gc.GMAIL_READ), "Google didn't allow Collie to read Gmail"),
             (gc.MissingScope(gc.GMAIL_COMPOSE), "Google didn't allow Collie to write Gmail drafts"),
             (gc.MissingScope(gc.CALENDAR_READ), "Google didn't allow Collie to read your calendar"),
             (gc.GoogleAPIError(503, "x"), "Gmail answered with an error (HTTP 503)"))
    for exc, words in cases:
        assert src.google_reason(gc, exc, "Gmail") == words, type(exc).__name__


def test_status_has_the_fields_the_report_reads(tmp_path):
    st = gc.status(state_dir=str(tmp_path))
    assert st["state"] in {"not_configured", "not_connected", "connected", "missing_scope",
                           "needs_reconnect"}
    assert set(st["can"]) >= {"gmail_read", "gmail_drafts", "calendar_read"}
    assert "account" in st
    assert set(src._GOOGLE_STATES) | {"connected", "missing_scope"} == {
        "not_configured", "not_connected", "connected", "missing_scope", "needs_reconnect"}


def test_the_connector_still_has_no_way_to_send_mail():
    public = [name for name in dir(gc) if not name.startswith("_") and callable(getattr(gc, name))]
    assert not [name for name in public if "send" in name.lower()]
