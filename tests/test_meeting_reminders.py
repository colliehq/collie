import datetime as dt
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harness import meeting_reminders, meetings


def _stamp(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc).timestamp()


def test_ics_recurring_events_apply_exdates_overrides_and_ignore_attendees():
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:weekly-product@example.com
DTSTART:20260827T100000Z
DTEND:20260827T110000Z
RRULE:FREQ=WEEKLY;COUNT=3
EXDATE:20260903T100000Z
SUMMARY:Weekly Product Sync
DESCRIPTION:Roadmap\\nJoin https://meet.google.com/abc-defg-hij
ATTENDEE;CN=Private Person:mailto:private@example.com
END:VEVENT
BEGIN:VEVENT
UID:weekly-product@example.com
RECURRENCE-ID:20260910T100000Z
DTSTART:20260910T110000Z
DTEND:20260910T120000Z
SUMMARY:Weekly Product Sync moved
END:VEVENT
END:VCALENDAR
"""
    rows = meeting_reminders.parse_ics(raw, now=_stamp("2026-08-20T00:00:00"))
    assert len(rows) == 2
    assert [row["start_at"] for row in rows] == [
        _stamp("2026-08-27T10:00:00"), _stamp("2026-09-10T11:00:00")]
    assert rows[0]["join_url"] == "https://meet.google.com/abc-defg-hij"
    assert rows[0]["template"] == "general"
    assert "attendee" not in json.dumps(rows).lower()
    assert rows[0]["series_id"] == rows[1]["series_id"]


def test_ics_rejects_non_calendar_and_unbounded_input():
    with pytest.raises(meeting_reminders.ReminderError, match="not an iCalendar"):
        meeting_reminders.parse_ics("not a calendar")
    with pytest.raises(meeting_reminders.ReminderError, match="2 MiB"):
        meeting_reminders.parse_ics("BEGIN:VCALENDAR\n" + "x" * (2 * 1024 * 1024))


def test_unknown_calendar_timezone_uses_os_local_date_rules(monkeypatch):
    def missing(_name):
        raise meeting_reminders.ZoneInfoNotFoundError

    monkeypatch.setattr(meeting_reminders, "ZoneInfo", missing)
    local, _ = meeting_reminders._parse_datetime(
        "20261215T090000", {"TZID": "Unknown/Office"})
    utc, _ = meeting_reminders._parse_datetime("20261215T090000Z", {})
    assert local.tzinfo is None  # timestamp() will apply OS DST rules for December, not August.
    assert utc.tzinfo == dt.timezone.utc


def test_reminders_are_deduplicated_snoozable_and_sensitive_by_default(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    store = meeting_reminders.ReminderStore(str(tmp_path / "reminders.json"))
    event = store.save_event(
        title="Product sync", start_at=now + 240, end_at=now + 1800,
        join_url="https://meet.google.com/abc-defg-hij")
    private = store.save_event(
        title="Therapy", start_at=now + 240, end_at=now + 1800)

    claimed = store.claim_due(now=now)
    assert len(claimed) == 1 and claimed[0]["event"]["id"] == event["id"]
    assert claimed[0]["phase"] == "prepare"
    assert "Product sync" not in claimed[0]["notification_body"]
    assert store.claim_due(now=now + 1) == []

    store.reminder_action(event["id"], "prepare", "snooze", minutes=5)
    assert store.claim_due(now=now + 60) == []
    assert store.claim_due(now=now + 301)[0]["event"]["id"] == event["id"]

    snapshot = store.snapshot(start_at=now, end_at=now + 3600)
    sensitive = next(row for row in snapshot["events"] if row["id"] == private["id"])
    assert sensitive["sensitive"] is True


def test_series_preferences_and_end_prompts_do_not_store_consent(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    store = meeting_reminders.ReminderStore(str(tmp_path / "reminders.json"))
    event = store.save_event(title="1:1", start_at=now - 1200, end_at=now - 120)
    changed = store.update_series(event["series_id"], remind=True, template="one_on_one",
                                  language="zh")
    assert changed == {"remind": True, "template": "one_on_one", "language": "zh"}
    assert "consent" not in json.dumps(changed).lower()
    assert store.claim_due(now=now) == []
    store.mark_engaged(event["id"], kind="recording")
    due = store.claim_due(now=now + 180)
    assert len(due) == 1 and due[0]["phase"] == "end"


def test_calendar_linkage_never_bypasses_fresh_recording_consent(tmp_path):
    store = meetings.MeetingStore(str(tmp_path / "meetings"))
    with pytest.raises(meetings.MeetingError, match="consent"):
        store.start(scheduled_event_id="evt_" + "a" * 24, scheduled_end_at=123,
                    consent=False)
    row = store.start(
        title="Linked", scheduled_event_id="evt_" + "a" * 24,
        scheduled_series_id="series_" + "b" * 24, scheduled_end_at=456,
        consent=True)
    assert row["consent_confirmed"] is True
    assert row["scheduled_event_id"] == "evt_" + "a" * 24
    assert row["scheduled_end_at"] == 456


@pytest.fixture
def reminder_web(monkeypatch, tmp_path):
    from harness import webapp
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(url, token, *, body=None, method="GET"):
    url += ("&" if "?" in url else "?") + "token=" + token
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_authenticated_calendar_api_prefills_but_still_requires_consent(reminder_web):
    base, token = reminder_web
    now = dt.datetime.now(dt.timezone.utc)
    start = now + dt.timedelta(minutes=4)
    end = start + dt.timedelta(hours=1)
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:api-event@example.com
DTSTART:%s
DTEND:%s
SUMMARY:API Planning
URL:https://zoom.us/j/123456
END:VEVENT
END:VCALENDAR
""" % (start.strftime("%Y%m%dT%H%M%SZ"), end.strftime("%Y%m%dT%H%M%SZ"))

    code, _ = _request(base + "/api/meetings/calendar/import", "wrong",
                       method="POST", body={"ics": ics})
    assert code == 403
    code, imported = _request(base + "/api/meetings/calendar/import", token,
                              method="POST", body={"ics": ics})
    assert code == 201 and imported["imported"] == 1
    code, schedule = _request(
        base + "/api/meetings/schedule?start=%s&end=%s" %
        (now.timestamp(), (end + dt.timedelta(days=1)).timestamp()), token)
    assert code == 200 and len(schedule["events"]) == 1
    event = schedule["events"][0]

    code, error = _request(base + "/api/meetings/start", token, method="POST", body={
        "scheduled_event_id": event["id"], "consent": False})
    assert code == 400 and "consent" in error["error"]
    code, meeting = _request(base + "/api/meetings/start", token, method="POST", body={
        "title": event["title"], "scheduled_event_id": event["id"],
        "consent": True, "mime_type": "audio/webm"})
    assert code == 201 and meeting["scheduled_event_id"] == event["id"]
    assert meeting["scheduled_end_at"] == pytest.approx(event["end_at"])


def test_meeting_page_exposes_opt_in_alerts_and_local_calendar_controls():
    page = os.path.join(os.path.dirname(meetings.__file__), "webui", "meetings.html")
    html = open(page, encoding="utf-8").read()
    assert "Enable alerts" in html and "Import .ics" in html
    assert "/api/meetings/reminders/claim" in html
    assert "only you can start a recording" in html
    assert "Recording consent and the AI switch are never remembered" in html
    assert 'id="consent" type="checkbox" checked' not in html
