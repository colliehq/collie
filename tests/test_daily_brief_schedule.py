"""What the opt-in daily brief email must never get wrong.

The failures this file exists to prevent are the ones a person cannot undo: a
private morning summary sent to an address they did not choose, a second copy of
the same day, a brief that silently stops arriving, and a surface that reports a
send that never happened.

Everything runs against ``tmp_path`` with the real durable stores -- the real
ledger file, the real :mod:`harness.communications` outbox, the real
:class:`~harness.channel_service.ChannelService` -- and a fake mail adapter in
place of a provider.  No network, no credentials, no mailbox, no personal data.

Time zones are faked too: this machine has no ``tzdata``, and the DST rules being
tested (a 23-hour and a 25-hour day) must be exercised anyway, so a small
US-style zone is installed in place of :mod:`zoneinfo` for both this module and
:mod:`harness.daily_brief`.
"""
import contextlib
import datetime as dt
import json
import os
import sys
import threading
import types

import pytest

from harness import communications as comms, daily_brief_schedule as sched
from harness.channel_service import ChannelService

HOUR = dt.timedelta(hours=1)
ZERO = dt.timedelta(0)
OWNER = "owner@example.test"
ZONE_KEY = "Test/Eastern"


class Eastern(dt.tzinfo):
    """-05:00, with DST from 2026-03-08 02:00 to 2026-11-01 02:00 local."""

    key = ZONE_KEY
    START, END = dt.datetime(2026, 3, 8, 2), dt.datetime(2026, 11, 1, 2)

    def utcoffset(self, when):
        return -5 * HOUR + self.dst(when)

    def dst(self, when):
        if when is None:
            return ZERO
        naive = when.replace(tzinfo=None)
        if self.START + HOUR <= naive < self.END - HOUR:
            return HOUR
        if self.END - HOUR <= naive < self.END:                  # the repeated hour
            return ZERO if when.fold else HOUR
        if self.START <= naive < self.START + HOUR:              # the hour that is not
            return HOUR if when.fold else ZERO
        return ZERO

    def tzname(self, when):
        return "EDT" if self.dst(when) else "EST"


class ZoneInfoNotFoundError(KeyError):
    pass


def fake_zoneinfo(keys=(ZONE_KEY,)):
    """A stand-in tz database.  ``keys=()`` is a machine with no tzdata at all."""
    def ZoneInfo(key):
        if key in keys:
            return Eastern()
        if key == "UTC" and keys:
            return dt.timezone.utc
        raise ZoneInfoNotFoundError(key)
    return types.SimpleNamespace(ZoneInfo=ZoneInfo,
                                 ZoneInfoNotFoundError=ZoneInfoNotFoundError)


@pytest.fixture
def zones(monkeypatch):
    monkeypatch.setitem(sys.modules, "zoneinfo", fake_zoneinfo())
    return Eastern()


class Adapter:
    """A mail provider that records, and fails exactly when told to."""

    def __init__(self):
        self.sent, self.fail = [], None

    def validate_config(self, config):
        return dict(config)

    def probe(self, config, credentials):
        return {"receive": True, "send": True}

    def anchor(self, config, credentials):
        return {"cursor": {"uid": 1}}

    def poll(self, config, credentials, *, cursor=None, limit=25):
        return {"messages": [], "cursor": {"uid": 1}, "more": False}

    def send(self, config, credentials, result):
        self.sent.append(result)
        if self.fail is not None:
            raise self.fail
        return {"status": "submitted", "provider_message_id": "p-%d" % len(self.sent)}


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = str(tmp_path / "state")
    monkeypatch.setenv("COLLIE_STATE_DIR", root)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", os.path.join(root, "sessions"))
    adapter = Adapter()
    monkeypatch.setattr(ChannelService, "_adapter", staticmethod(lambda kind: adapter))
    service = ChannelService(root)
    service.configure("mail", kind="imap", config={"address": "collie@example.test"},
                      owner=OWNER, credentials={"password": "private-password"},
                      workspace=str(tmp_path))
    return root, service, adapter


def at(year, month, day, hour=7, minute=31, fold=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=Eastern(), fold=fold).timestamp()


def opt_in(root, service, **body):
    return sched.configure(root, dict({"enabled": True, "connection": "mail",
                                       "timezone": ZONE_KEY, "at": "07:30"}, **body),
                           service=service, now=at(2026, 9, 1))


def test_desktop_pump_keeps_the_brief_lane_when_mail_intake_fails(tmp_path, monkeypatch):
    from harness import channel_service
    root = str(tmp_path / "pump")
    checked, reports = threading.Event(), []
    real_tick = sched.tick

    def broken_intake(self):
        raise RuntimeError("mailbox unavailable")

    def observe(state_dir):
        reports.append(real_tick(state_dir))
        checked.set()

    def no_service(*args, **kwargs):
        raise AssertionError("disabled briefing must not open an account")

    monkeypatch.setattr(ChannelService, "tick", broken_intake)
    monkeypatch.setattr(sched, "tick", observe)
    monkeypatch.setattr(sched, "_service", no_service)
    stop = channel_service.start_pump(root)
    thread = channel_service._PUMPS[root][0]
    try:
        assert checked.wait(5), "mail failure prevented the independent brief lane"
        assert reports[0]["state"] == "disabled"
    finally:
        stop.set()
        thread.join(5)
        channel_service._PUMPS.pop(root, None)
    assert not thread.is_alive()


def ledger(root, profile="default"):
    with open(sched._path(root, profile), encoding="utf-8") as handle:
        return json.load(handle)


@contextlib.contextmanager
def power_cut(service):
    """The process dies inside the provider call, after the outbox was written."""
    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt("power cut")
    service.send = boom
    try:
        yield
    finally:
        del service.send


def outbox(service, states=None):
    return comms.list_results("mail", states=states, include_private=True,
                              directory=service.directory)


@contextlib.contextmanager
def meanwhile(monkeypatch, change):
    """Somebody changes the settings in the gap between the render and the provider.

    That gap is real and deliberate -- no lock is held across a provider call -- so
    it is where a withdrawn consent has to be caught.  ``_outbox`` is the first thing
    the pass does after letting the ledger lock go.
    """
    real = sched._outbox

    def hooked(*args, **kwargs):
        change()
        return real(*args, **kwargs)

    monkeypatch.setattr(sched, "_outbox", hooked)
    try:
        yield
    finally:
        monkeypatch.setattr(sched, "_outbox", real)


# --------------------------------------------------------------- opt-in


def test_off_by_default_and_a_tick_sends_nothing(host, zones):
    root, service, adapter = host
    prefs = sched.preferences(root)
    assert prefs["enabled"] is False and prefs["connection"] == ""
    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["state"] == "disabled" and report["sent"] is False
    assert adapter.sent == [] and outbox(service) == []
    # Seeing the brief in the app is a desktop default; being emailed it is not.
    assert prefs["available"] is True and prefs["last"] is None


def test_opt_in_freezes_the_destination_and_takes_no_recipient(host, zones):
    root, service, _adapter = host
    with pytest.raises(sched.ScheduleError) as bad:
        sched.configure(root, {"enabled": True, "connection": "mail", "timezone": ZONE_KEY,
                               "destination": "somebody-else@example.test"}, service=service)
    assert "unknown setting" in str(bad.value) and "destination" in str(bad.value)

    prefs = opt_in(root, service)
    assert prefs["enabled"] and prefs["destination_masked"] == "o***@example.test"
    # Neither the stored file nor the public view keeps a second copy of the address.
    assert OWNER not in json.dumps(ledger(root)) and OWNER not in json.dumps(prefs)
    assert prefs["kind"] == "imap" and prefs["connection"] == "mail"


def test_only_mail_connections_can_carry_a_brief(host, zones):
    root, _service, _adapter = host

    class Phone:
        directory = ""

        def connection(self, connection):
            return {"id": connection, "kind": "twilio", "owner": "+15551230000",
                    "enabled": True}

        def overview(self):
            return {"connections": []}

    with pytest.raises(sched.ScheduleError) as bad:
        sched.configure(root, {"enabled": True, "connection": "phone",
                               "timezone": ZONE_KEY}, service=Phone())
    assert "email connection" in str(bad.value)
    assert sched.preferences(root)["enabled"] is False


def test_opting_in_enables_nothing_else(host, zones):
    root, service, _adapter = host
    service.set_enabled("mail", False)
    prefs = opt_in(root, service)
    row = service.connection("mail")
    # No connection was switched on, and the general auto-reply switch was neither
    # required nor changed: this preference is its own consent.
    assert row["enabled"] is False and row["auto_reply"] is False
    assert prefs["enabled"] is True
    assert any("paused" in notice for notice in prefs["notices"])


def test_a_bad_schedule_is_refused_field_by_field(host, zones):
    root, service, _adapter = host
    for body, expected in (({"enabled": True, "connection": "mail", "timezone": "Nowhere/Fake"},
                            "unknown time zone"),
                           ({"enabled": True, "connection": "mail", "timezone": ZONE_KEY,
                             "at": "7:30"}, "24-hour clock"),
                           ({"enabled": True, "connection": "mail", "timezone": ZONE_KEY,
                             "language": "fr"}, "language must be"),
                           ({"enabled": True, "connection": "mail", "timezone": ZONE_KEY,
                             "grace_minutes": 5}, "cutoff must be between"),
                           ({"enabled": "yes"}, "true or false")):
        with pytest.raises(sched.ScheduleError) as bad:
            sched.configure(root, body, service=service)
        assert expected in str(bad.value)
    assert sched.preferences(root)["enabled"] is False


def test_without_a_time_zone_database_nothing_is_scheduled(host, monkeypatch):
    root, service, adapter = host
    monkeypatch.setitem(sys.modules, "zoneinfo", fake_zoneinfo(keys=()))
    with pytest.raises(sched.ScheduleError) as bad:
        opt_in(root, service)
    assert "time zone database" in str(bad.value)
    assert adapter.sent == []


# --------------------------------------------------------------- one a day


def test_one_email_a_day_however_often_the_pump_ticks(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    first = sched.tick(root, at(2026, 9, 10), service=service)
    assert first["state"] == "submitted" and first["sent"] is True
    assert first["delivery_known"] is False and first["job"]["submission_known"] is True

    for minute in (32, 35, 59):
        again = sched.tick(root, at(2026, 9, 10, 7, minute), service=service)
        assert again["sent"] is False and again["state"] == "submitted"
    assert len(adapter.sent) == 1
    assert [row["id"] for row in outbox(service)] == [first["job"]["result_id"]]

    # The job key is the day, not the content: it never collides with the brief id.
    job = ledger(root)["jobs"][-1]
    assert job["id"] == "daily-brief-email:default:mail:2026-09-10"
    assert job["brief_id"] != job["id"] and job["brief_id"].startswith("brief-2026-09-10-")


def test_two_concurrent_ticks_send_one_email(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    reports, when = [], at(2026, 9, 10)

    threads = [threading.Thread(target=lambda: reports.append(
        sched.tick(root, when, service=service))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(adapter.sent) == 1
    assert len(outbox(service)) == 1
    assert sum(report["sent"] for report in reports) == 1
    assert all(report["state"] in ("submitted", "sending", "queued") for report in reports)


def test_the_brief_arrives_as_the_scheduler_s_own_draft(host, zones):
    root, service, adapter = host
    # Even with the connection's general auto-reply on, the channel pump must not
    # send this draft: two pumps sending one message is the duplicate we cannot undo.
    service.configure("mail", kind="imap", config={"address": "collie@example.test"},
                      owner=OWNER, auto_reply=True)
    opt_in(root, service)
    with open(sched._path(root, "default")) as handle:
        prepared = json.load(handle)
    assert prepared["jobs"] == []

    sched.tick(root, at(2026, 9, 10), service=service)
    stored = outbox(service)[0]
    assert stored["metadata"]["auto_eligible"] is False
    assert stored["metadata"]["brief_date"] == "2026-09-10"
    assert stored["metadata"]["brief_id"] == ledger(root)["jobs"][-1]["brief_id"]
    assert stored["metadata"]["message_id"].startswith("<collie-brief-")
    assert stored["destination"] == OWNER
    assert len(stored["subject"].encode()) <= sched.SUBJECT_BYTES
    assert len(stored["text"].encode()) <= sched.TEXT_BYTES

    service.tick()                                    # the channel pump's own pass
    assert len(adapter.sent) == 1


def test_an_oversized_brief_is_clipped_not_refused(host, zones, monkeypatch):
    root, service, adapter = host
    opt_in(root, service)
    from harness import daily_brief_web
    real = daily_brief_web.read

    def huge(*args, **kwargs):
        payload = real(*args, **kwargs)
        payload["email"] = {"available": True, "subject": "S" * 900, "text": "x" * 90000}
        return payload

    monkeypatch.setattr(daily_brief_web, "read", huge)
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "submitted"
    stored = outbox(service)[0]
    assert len(stored["subject"].encode()) <= sched.SUBJECT_BYTES
    assert len(stored["text"].encode()) <= sched.TEXT_BYTES
    assert stored["text"].endswith("(truncated)")


# --------------------------------------------------------------- the window


def test_before_the_hour_nothing_is_prepared(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    report = sched.tick(root, at(2026, 9, 10, 7, 29), service=service)
    assert report["state"] == "waiting" and report["sent"] is False
    assert outbox(service) == [] and adapter.sent == []
    assert report["next_at"] == at(2026, 9, 10, 7, 30)


def test_a_missed_morning_is_skipped_and_never_backfilled(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    late = sched.tick(root, at(2026, 9, 10, 18, 0), service=service)
    assert late["state"] == "skipped" and late["sent"] is False
    assert "not running" in late["detail"]
    assert outbox(service) == [] and adapter.sent == []

    assert sched.tick(root, at(2026, 9, 10, 19, 0), service=service)["state"] == "skipped"
    # Two days missed, then the app comes back: today's brief, and only today's.
    assert sched.tick(root, at(2026, 9, 12), service=service)["state"] == "submitted"
    assert len(adapter.sent) == 1
    assert [job["date"] for job in ledger(root)["jobs"]] == ["2026-09-10", "2026-09-12"]


def test_a_paused_connection_is_transient_and_settles_nothing(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    service.set_enabled("mail", False)
    blocked = sched.tick(root, at(2026, 9, 10), service=service)
    assert blocked["state"] == "blocked" and blocked["sent"] is False
    assert "paused" in blocked["detail"] and adapter.sent == []
    assert ledger(root)["jobs"] == []                 # nothing claims to have happened

    service.set_enabled("mail", True)
    assert sched.tick(root, at(2026, 9, 10, 9, 0), service=service)["state"] == "submitted"
    assert len(adapter.sent) == 1


def test_a_draft_that_outlives_its_window_is_not_sent_later(host, zones):
    root, service, adapter = host
    opt_in(root, service, grace_minutes=60)
    with power_cut(service):
        with pytest.raises(KeyboardInterrupt):
            sched.tick(root, at(2026, 9, 10), service=service)

    expired = sched.tick(root, at(2026, 9, 10, 12, 0), service=service)
    assert expired["state"] == "expired" and adapter.sent == []
    # The morning is over, so the draft goes with it: left pending it is a brief the
    # person is shown forever and an open outbox row that will never be spent.
    assert outbox(service, states=["pending"]) == []
    assert [row["state"] for row in outbox(service)] == ["cancelled"]
    assert "discarded" in expired["detail"]
    assert sched.tick(root, at(2026, 9, 10, 13, 0), service=service)["state"] == "expired"
    # And the reason the day ended is what the record keeps, not the consequence.
    assert ledger(root)["jobs"][-1]["detail"].startswith("the morning window closed")


# --------------------------------------------------------------- local days


def test_a_23_hour_day_and_a_25_hour_day_are_each_one_email(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    # Spring forward (2026-03-08) and fall back (2026-11-01), ticked every hour.
    for month, day in ((3, 7), (11, 1)):
        adapter.sent.clear()
        start = at(2026, month, day, 7, 31)
        for step in range(26):
            sched.tick(root, start + step * 3600, service=service)
        dates = sorted({row["metadata"]["brief_date"] for row in outbox(service)})
        assert len(adapter.sent) == 2, dates          # this day and the next, no more
    assert [job["date"] for job in ledger(root)["jobs"]] == [
        "2026-03-07", "2026-03-08", "2026-11-01", "2026-11-02"]


def test_the_day_boundary_is_local_not_utc(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    # 2026-09-11 03:00 UTC is still the evening of the 10th in this zone, and the
    # morning window for the 11th has not opened.
    assert sched.tick(root, at(2026, 9, 10, 23, 0), service=service)["state"] == "skipped"
    assert sched.tick(root, at(2026, 9, 11, 7, 31), service=service)["date"] == "2026-09-11"
    assert len(adapter.sent) == 1


# --------------------------------------------------------------- the owner


def test_a_changed_owner_pauses_instead_of_redirecting(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    sched.tick(root, at(2026, 9, 10), service=service)
    service.configure("mail", kind="imap", config={"address": "collie@example.test"},
                      owner="somebody-else@example.test")

    report = sched.tick(root, at(2026, 9, 11), service=service)
    assert report["state"] == "needs_reconfigure" and report["needs_reconfigure"]
    assert "owner address changed" in report["detail"]
    assert len(adapter.sent) == 1                     # yesterday's, and nothing since
    assert sched.preferences(root)["needs_reconfigure"] is True
    # It stays paused: a silent resume would be the redirect we refused a tick ago.
    assert sched.tick(root, at(2026, 9, 11, 9, 0), service=service)["state"] == "needs_reconfigure"

    prefs = opt_in(root, service)
    assert prefs["destination_masked"] == "s***@example.test"
    assert sched.tick(root, at(2026, 9, 11, 9, 30), service=service)["state"] == "submitted"
    assert adapter.sent[-1]["destination"] == "somebody-else@example.test"


def test_a_connection_that_is_gone_pauses_the_schedule(host, zones):
    root, service, adapter = host
    opt_in(root, service)

    class Missing:
        directory = service.directory

        def connection(self, connection):
            raise KeyError(connection)

        def overview(self):
            return {"connections": []}

    report = sched.tick(root, at(2026, 9, 10), service=Missing())
    assert report["state"] == "needs_reconfigure" and adapter.sent == []
    assert "no longer there" in report["detail"]


# --------------------------------------------------------------- crashes


def test_a_death_between_the_ledger_and_the_provider_is_repaired_once(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    with power_cut(service):
        with pytest.raises(KeyboardInterrupt):
            sched.tick(root, at(2026, 9, 10), service=service)

    prepared = ledger(root)["jobs"][-1]
    assert prepared["state"] == "sending" and prepared["attempts"] == 1
    assert outbox(service, states=["pending"])        # durable before the provider

    resumed = sched.tick(root, at(2026, 9, 10, 7, 40), service=service)
    assert resumed["state"] == "submitted" and len(adapter.sent) == 1
    assert len(outbox(service)) == 1


def test_a_death_after_the_provider_never_sends_a_second_copy(host, zones, monkeypatch):
    root, service, adapter = host
    opt_in(root, service)
    settled = sched._settle
    monkeypatch.setattr(sched, "_settle", lambda *a, **k: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        sched.tick(root, at(2026, 9, 10), service=service)
    monkeypatch.setattr(sched, "_settle", settled)

    assert len(adapter.sent) == 1
    assert ledger(root)["jobs"][-1]["state"] == "sending"   # the ledger never learned
    report = sched.tick(root, at(2026, 9, 10, 7, 45), service=service)
    assert report["state"] == "submitted" and report["sent"] is False
    assert len(adapter.sent) == 1                     # read back, not re-sent
    assert ledger(root)["jobs"][-1]["state"] == "submitted"


def test_a_restart_reuses_the_frozen_payload(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    with power_cut(service):
        with pytest.raises(KeyboardInterrupt):
            sched.tick(root, at(2026, 9, 10), service=service)
    frozen = ledger(root)["jobs"][-1]

    # The day moves on underneath the prepared draft; the stored text still wins.
    from harness.meeting_reminders import ReminderStore
    ReminderStore(os.path.join(root, "meeting-reminders.json")).save_event(
        title="A meeting that did not exist when the brief was frozen",
        start_at=at(2026, 9, 10, 15, 0), end_at=at(2026, 9, 10, 16, 0))

    sched.tick(root, at(2026, 9, 10, 8, 0), service=service)
    stored = outbox(service)[0]
    assert stored["text"] == frozen["text"] and stored["subject"] == frozen["subject"]
    assert "did not exist" not in stored["text"]
    assert ledger(root)["jobs"][-1]["brief_id"] == frozen["brief_id"]


# --------------------------------------------------------------- the provider


def test_an_unknown_delivery_is_never_retried(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    adapter.fail = RuntimeError("the connection dropped mid-conversation")

    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["state"] == "unknown" and report["sent"] is False
    assert report["delivery_known"] is False and report["job"]["submission_known"] is False
    assert len(adapter.sent) == 1

    adapter.fail = None
    for hour in (8, 9, 10):
        again = sched.tick(root, at(2026, 9, 10, hour, 0), service=service)
        assert again["state"] == "unknown" and again["sent"] is False
    assert len(adapter.sent) == 1
    assert outbox(service)[0]["state"] == "unknown"


def test_a_refused_send_stays_visible_for_a_person(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    refusal = RuntimeError("the account rejected the message")
    refusal.delivery_unknown = False
    adapter.fail = refusal

    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["state"] == "failed" and "manual retry" in report["detail"]
    adapter.fail = None
    for hour in (8, 9):                               # no busy loop, ever
        assert sched.tick(root, at(2026, 9, 10, hour, 0), service=service)["state"] == "failed"
    assert len(adapter.sent) == 1
    stored = outbox(service)[0]
    assert stored["state"] == "failed" and stored["retryable"] is True


# --------------------------------------------------------------- corruption


def test_unreadable_settings_fail_closed_and_are_preserved(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    path = sched._path(root, "default")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"schema": "collie.daily_brief.schedule/1", "prefs": ')

    prefs = sched.preferences(root)
    assert prefs["available"] is False and prefs["enabled"] is False
    assert prefs["needs_reconfigure"] is True
    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["state"] == "error" and report["sent"] is False
    assert adapter.sent == [] and outbox(service) == []
    # A read never overwrites the evidence; only an explicit write repairs it.
    assert open(path, encoding="utf-8").read().endswith('"prefs": ')

    repaired = sched.configure(root, {"enabled": True, "connection": "mail",
                                      "timezone": ZONE_KEY}, service=service)
    assert repaired["enabled"] is True
    assert any("set aside" in notice for notice in repaired["notices"])
    kept = [name for name in os.listdir(os.path.dirname(path)) if ".corrupt-" in name]
    assert len(kept) == 1
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "submitted"


def test_settings_are_per_profile_and_public_safe(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    assert sched.preferences(root, profile="work")["enabled"] is False
    assert sched.tick(root, at(2026, 9, 10), profile="work", service=service)["state"] == "disabled"

    sched.tick(root, at(2026, 9, 10), service=service)
    public = json.dumps(sched.preferences(root, service=service))
    assert OWNER not in public and "private-password" not in public
    # A job is evidence of a send, not a copy of the person's morning.
    assert "Daily brief ·" not in public
    assert sched.preferences(root)["last"]["state"] == "submitted"


# ------------------------------------------------- consent, at the moment of sending


def test_switching_it_off_mid_pass_sends_nothing(host, zones, monkeypatch):
    root, service, adapter = host
    opt_in(root, service)
    with meanwhile(monkeypatch, lambda: sched.configure(root, {"enabled": False},
                                                        service=service)):
        report = sched.tick(root, at(2026, 9, 10), service=service)
    # The settings read at the top of the pass was a snapshot; consent is what is on
    # disk when the attempt is spent, and it had been withdrawn by then.
    assert report["sent"] is False and adapter.sent == []
    assert "switched off" in report["detail"]
    assert ledger(root)["jobs"][-1]["attempts"] == 0

    # Back on inside the same window: the one frozen draft goes, never a second.
    opt_in(root, service)
    resumed = sched.tick(root, at(2026, 9, 10, 8, 0), service=service)
    assert resumed["state"] == "submitted" and len(adapter.sent) == 1
    assert len(outbox(service)) == 1


def test_a_brief_prepared_for_one_account_is_never_sent_from_another(host, zones, monkeypatch):
    root, service, adapter = host
    service.configure("second", kind="imap", config={"address": "other@example.test"},
                      owner="second-owner@example.test", credentials={"password": "p"})
    opt_in(root, service)

    def repoint():
        sched.configure(root, {"enabled": True, "connection": "second",
                               "timezone": ZONE_KEY}, service=service)

    with meanwhile(monkeypatch, repoint):
        report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["sent"] is False and adapter.sent == []
    assert "settings changed" in report["detail"]
    assert comms.list_results("second", include_private=True,
                              directory=service.directory) == []


def test_moving_the_send_time_out_of_the_window_stops_the_send(host, zones, monkeypatch):
    root, service, adapter = host
    opt_in(root, service)
    with meanwhile(monkeypatch, lambda: sched.configure(
            root, {"enabled": True, "connection": "mail", "timezone": ZONE_KEY,
                   "at": "19:00"}, service=service)):
        report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["sent"] is False and adapter.sent == []
    assert "window" in report["detail"]


# --------------------------------------------------------------- the outbox is the truth


def test_a_send_that_landed_as_the_window_closed_is_not_called_expired(host, zones, monkeypatch):
    root, service, adapter = host
    opt_in(root, service, grace_minutes=60)
    # Accepted by the provider, then the process dies before the ledger learns of it.
    settled = sched._settle
    monkeypatch.setattr(sched, "_settle", lambda *a, **k: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        sched.tick(root, at(2026, 9, 10), service=service)
    monkeypatch.setattr(sched, "_settle", settled)
    assert len(adapter.sent) == 1 and ledger(root)["jobs"][-1]["state"] == "sending"

    # The clock closes the window; it does not decide what happened.  Saying
    # "expired" here would tell a person their brief never went out, permanently.
    late = sched.tick(root, at(2026, 9, 10, 12, 0), service=service)
    assert late["state"] == "submitted" and late["sent"] is False
    assert ledger(root)["jobs"][-1]["state"] == "submitted"
    assert len(adapter.sent) == 1


def test_a_person_resolving_an_unknown_send_corrects_the_day(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    adapter.fail = RuntimeError("the connection dropped mid-conversation")
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "unknown"
    adapter.fail = None

    result_id = ledger(root)["jobs"][-1]["result_id"]
    comms.resolve_unknown("mail", result_id, actor="desktop-user", outcome="submitted",
                          reason="found in the account's sent folder",
                          directory=service.directory)
    report = sched.tick(root, at(2026, 9, 10, 9, 0), service=service)
    assert report["state"] == "submitted" and report["sent"] is False
    assert sched.preferences(root)["last"]["submission_known"] is True
    assert len(adapter.sent) == 1                     # corrected, never re-sent


def test_a_manual_retry_is_the_persons_to_send_not_the_schedulers(host, zones):
    root, service, adapter = host
    refusal = RuntimeError("the account rejected the message")
    refusal.delivery_unknown = False
    opt_in(root, service)
    adapter.fail = refusal
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "failed"
    adapter.fail = None

    result_id = ledger(root)["jobs"][-1]["result_id"]
    comms.retry("mail", result_id, actor="desktop-user", reason="the account is back",
                directory=service.directory)
    # Pending again, but a failed day stays closed here: re-sending on a schedule is
    # how one refused message becomes an unattended loop.
    for hour in (8, 9):
        assert sched.tick(root, at(2026, 9, 10, hour, 0), service=service)["state"] == "failed"
    service.tick()                                    # nor is it the channel pump's
    assert adapter.sent == [] or len(adapter.sent) == 1
    assert comms.get_result("mail", result_id, directory=service.directory)["state"] == "pending"


def test_yesterdays_unsent_draft_is_never_sent_and_does_not_outlive_the_day(host, zones):
    root, service, adapter = host
    opt_in(root, service, grace_minutes=60)
    with power_cut(service):
        with pytest.raises(KeyboardInterrupt):
            sched.tick(root, at(2026, 9, 10), service=service)
    yesterday = ledger(root)["jobs"][-1]["result_id"]

    today = sched.tick(root, at(2026, 9, 11), service=service)
    assert today["state"] == "submitted" and len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"]["brief_date"] == "2026-09-11"
    # Yesterday's morning cannot come back, so yesterday's draft is withdrawn -- and
    # withdrawn is not sent: the provider was asked for exactly one message.
    assert comms.get_result("mail", yesterday, directory=service.directory)["state"] == "cancelled"
    assert [job["state"] for job in ledger(root)["jobs"]] == ["expired", "submitted"]
    service.tick()
    assert len(adapter.sent) == 1


def test_somebody_elses_draft_under_todays_id_is_never_sent(host, zones):
    root, service, adapter = host
    opt_in(root, service)
    stolen = sched._result_id("default", "mail", "2026-09-10")
    comms.create_result("mail", stolen, destination=OWNER, subject="Re: lunch",
                        text="a reply a person wrote, which is not a daily brief",
                        metadata={"auto_eligible": True}, directory=service.directory)

    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["state"] == "error" and report["sent"] is False
    assert "different message" in report["detail"] and adapter.sent == []
    assert comms.get_result("mail", stolen, directory=service.directory)["state"] == "pending"


def test_a_repaired_ledger_sends_the_draft_that_was_already_stored(host, zones, monkeypatch):
    root, service, adapter = host
    opt_in(root, service)
    with power_cut(service):
        with pytest.raises(KeyboardInterrupt):
            sched.tick(root, at(2026, 9, 10), service=service)
    prepared = outbox(service)[0]

    # The ledger is lost and rebuilt blank, so the same day derives the same outbox
    # id -- while the brief itself has moved on underneath it.
    with open(sched._path(root, "default"), "w", encoding="utf-8") as handle:
        handle.write("{")
    from harness import daily_brief_web
    real = daily_brief_web.read

    def moved_on(*args, **kwargs):
        payload = real(*args, **kwargs)
        payload["brief"] = dict(payload["brief"], id="brief-2026-09-10-elsewhere")
        payload["email"] = {"available": True, "subject": "Later", "text": "later text"}
        return payload

    monkeypatch.setattr(daily_brief_web, "read", moved_on)
    sched.configure(root, {"enabled": True, "connection": "mail", "timezone": ZONE_KEY},
                    service=service)
    report = sched.tick(root, at(2026, 9, 10, 8, 0), service=service)

    assert report["state"] == "submitted" and len(adapter.sent) == 1
    assert adapter.sent[0]["text"] == prepared["text"]        # stored, not re-rendered
    assert len(outbox(service)) == 1
    # And the record says what was actually sent, not what this pass happened to build.
    job = ledger(root)["jobs"][-1]
    assert job["brief_id"] == prepared["metadata"]["brief_id"] != "brief-2026-09-10-elsewhere"
    assert job["text_bytes"] == len(prepared["text"].encode())


# --------------------------------------------------------------- abandoned days


def test_an_abandoned_day_takes_its_draft_with_it_and_nothing_else(host, zones):
    root, service, adapter = host
    # Somebody else's draft, pending in the same outbox for the whole story.
    comms.create_result("mail", "a-persons-own-draft", destination=OWNER,
                        text="see you at six", metadata={"auto_eligible": True},
                        directory=service.directory)
    opt_in(root, service, grace_minutes=60)
    with power_cut(service):
        with pytest.raises(KeyboardInterrupt):
            sched.tick(root, at(2026, 9, 10), service=service)
    mine = ledger(root)["jobs"][-1]["result_id"]
    assert comms.get_result("mail", mine, directory=service.directory)["state"] == "pending"

    # Consent withdrawn while the draft is prepared.  The morning it belongs to is
    # still today, so it is still a message the person could choose to send.
    sched.configure(root, {"enabled": False}, service=service, now=at(2026, 9, 10, 8, 0))
    assert sched.tick(root, at(2026, 9, 10, 12, 0), service=service)["state"] == "disabled"
    assert comms.get_result("mail", mine, directory=service.directory)["state"] == "pending"

    # The next day it is a brief for a morning that cannot come back.  Nothing will
    # ever send it, and a pending row nothing will send is one a person is shown for
    # ever and one the outbox cannot spend.
    assert sched.tick(root, at(2026, 9, 11, 9, 0), service=service)["state"] == "disabled"
    assert comms.get_result("mail", mine, directory=service.directory)["state"] == "cancelled"
    job = ledger(root)["jobs"][-1]
    assert job["state"] == "expired" and "discarded" in job["detail"]
    assert adapter.sent == []                          # withdrawn is not sent

    # Whatever else was in that outbox is none of this module's business.
    other = comms.get_result("mail", "a-persons-own-draft", include_private=True,
                             directory=service.directory)
    assert other["state"] == "pending" and other["text"] == "see you at six"
    assert [row["state"] for row in outbox(service)] == ["pending", "cancelled"]


def test_a_brief_that_may_be_in_flight_is_never_withdrawn(host, zones):
    root, service, adapter = host
    opt_in(root, service, grace_minutes=60)
    with power_cut(service):
        with pytest.raises(KeyboardInterrupt):
            sched.tick(root, at(2026, 9, 10), service=service)
    mine = ledger(root)["jobs"][-1]["result_id"]
    # Another sender holds it: whether it reached the provider is not knowable here,
    # so the day may not claim to have taken it back.
    comms.claim_send("mail", mine, transport="imap", directory=service.directory)

    expired = sched.tick(root, at(2026, 9, 10, 12, 0), service=service)
    assert expired["state"] == "expired" and "discarded" not in expired["detail"]
    assert comms.get_result("mail", mine, directory=service.directory)["state"] == "sending"
    assert ledger(root)["jobs"][-1].get("discarded") is None and adapter.sent == []


def test_an_unknown_or_settled_brief_is_evidence_and_is_left_alone(host, zones):
    root, service, adapter = host
    opt_in(root, service, grace_minutes=60)
    adapter.fail = RuntimeError("the connection dropped mid-conversation")
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "unknown"
    adapter.fail = None
    mine = ledger(root)["jobs"][-1]["result_id"]

    # Days later, with the schedule long since off: an `unknown` is the only record
    # of a message that may have gone out, and it is never cancelled or re-sent.
    sched.configure(root, {"enabled": False}, service=service, now=at(2026, 9, 10, 9, 0))
    assert sched.tick(root, at(2026, 9, 14, 9, 0), service=service)["state"] == "disabled"
    assert comms.get_result("mail", mine, directory=service.directory)["state"] == "unknown"
    assert ledger(root)["jobs"][-1]["state"] == "unknown" and len(adapter.sent) == 1


def test_a_foreign_draft_under_a_past_days_id_is_not_the_schedulers_to_discard(host, zones):
    root, service, adapter = host
    opt_in(root, service, grace_minutes=60)
    stolen = sched._result_id("default", "mail", "2026-09-10")
    comms.create_result("mail", stolen, destination=OWNER, text="not a brief",
                        metadata={"auto_eligible": True}, directory=service.directory)
    # The ledger believes it prepared that day; the row under the id is not its own.
    blocked = sched.tick(root, at(2026, 9, 10), service=service)
    assert blocked["state"] == "error" and "different message" in blocked["detail"]
    # Nor would clearing up an abandoned day ever reach for it: the row has to prove
    # it is the scheduler's own, and this job's, before anything withdraws it.
    assert sched._discard(service, {"id": sched._job_id("default", "mail", "2026-09-10"),
                                    "connection": "mail", "result_id": stolen}) == ""

    assert sched.tick(root, at(2026, 9, 12), service=service)["state"] == "submitted"
    assert comms.get_result("mail", stolen, directory=service.directory)["state"] == "pending"
    assert len(adapter.sent) == 1


# --------------------------------------------------------------- profiles


def test_a_request_cannot_save_the_settings_to_another_profile(host, zones):
    root, service, _adapter = host
    body = {"enabled": True, "connection": "mail", "timezone": ZONE_KEY, "profile": "work"}
    with pytest.raises(sched.ScheduleError) as bad:
        sched.configure(root, dict(body), service=service, now=at(2026, 9, 1))
    assert "profile" in str(bad.value)

    # Refused, not quietly redirected: neither the profile the caller is reading nor
    # the one the body named came on, and nothing was written for either.
    assert sched.preferences(root, service=service)["enabled"] is False
    assert sched.preferences(root, profile="work", service=service)["enabled"] is False
    assert not os.path.exists(sched._path(root, "work"))

    # Naming the profile the caller is already writing is not a redirection.
    named = sched.configure(root, dict(body), profile="work", service=service,
                            now=at(2026, 9, 1))
    assert named["enabled"] is True and named["profile"] == "work"
    assert sched.preferences(root, service=service)["enabled"] is False


# --------------------------------------------------------------- real time zones


def test_the_real_tz_database_handles_both_clock_changes(host):
    """No fake zone here: the machine's own tzdata, on the two days it moves."""
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("America/Los_Angeles")
    root, service, adapter = host

    def when(month, day, hour=7, minute=31):
        return dt.datetime(2026, month, day, hour, minute, tzinfo=zone).timestamp()

    sched.configure(root, {"enabled": True, "connection": "mail", "at": "07:30",
                           "timezone": "America/Los_Angeles"},
                    service=service, now=when(3, 1))
    for month, day in ((3, 8), (11, 1)):              # 23 hours long, then 25
        adapter.sent.clear()
        for step in range(26):                        # every hour, across midnight
            sched.tick(root, when(month, day) + step * 3600, service=service)
        assert len(adapter.sent) == 2                 # this day and the next, no more
    assert [job["date"] for job in ledger(root)["jobs"]] == [
        "2026-03-08", "2026-03-09", "2026-11-01", "2026-11-02"]
    assert sched.preferences(root)["timezone"] == "America/Los_Angeles"
