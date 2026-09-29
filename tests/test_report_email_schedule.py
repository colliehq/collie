"""The morning report as the morning email: one a day, frozen once, never beside the brief.

The report reuses the Daily Brief email's promises rather than making new ones -- explicit
consent, a frozen destination, one email a local day, the ledger-then-outbox-then-provider
order, and "unknown is never resent".  What is new, and what these tests are about, is
that the report takes minutes to build (it asks a model and reads Gmail), so it is built
*outside* every lock, once, under a lease; that it goes out as a designed HTML email with
its plain text beside it; that it supersedes the plain brief without ever producing both
on one morning; and that "Send me one now" is a real send with its own small budget that
never touches the morning's slot.

Everything runs against ``tmp_path`` with the real ledger, the real outbox and the real
ChannelService, the scheduler suite's fake zone database and fake mail adapter, and a
stand-in for ``morning_report.build`` -- no network, no model, no Gmail, no mailbox.
"""
import base64
import datetime as dt
import json
import os

import pytest

from harness import communications as comms, daily_brief_schedule as sched, mail_messages
from harness import morning_report
from test_daily_brief_schedule import (              # noqa: F401 - pytest fixtures
    OWNER, ZONE_KEY, at, host, ledger, meanwhile, opt_in, outbox, power_cut, zones)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                       "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def tiny_avatar(monkeypatch):
    from harness import avatar
    monkeypatch.setattr(avatar, "png", lambda name, size=512, source="", plate=True: PNG)


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    """A settings file of this test's own, and the process environment put back after.

    Saving a setting calls ``settings.apply()``, which writes ``COLLIE_*`` variables into
    ``os.environ``; monkeypatch cannot see those, so the environment is restored by hand.
    """
    from harness import settings
    monkeypatch.setattr(settings, "_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    monkeypatch.setattr(settings, "_injected", {})
    saved = {k: v for k, v in os.environ.items() if k.startswith("COLLIE_")}
    for key in ("REPORT_GMAIL_DRAFTS", "REPORT_MUTED"):
        os.environ.pop("COLLIE_" + key, None)
    yield settings
    for key in [k for k in os.environ if k.startswith("COLLIE_") and k not in saved]:
        del os.environ[key]
    os.environ.update(saved)


class Builds:
    """``morning_report.build``, standing in: counts, records its arguments, can fail."""

    def __init__(self):
        self.calls, self.fail, self.date, self.hook = [], None, None, None

    def __call__(self, *, state_dir=None, now=None, drafts=True, profile=None, **_kw):
        self.calls.append({"now": now, "drafts": drafts, "profile": dict(profile or {})})
        if self.hook:
            self.hook()
        if self.fail:
            raise self.fail
        zone = (profile or {}).get("zone") or dt.timezone.utc
        local = dt.datetime.fromtimestamp(now, zone)
        return {
            "schema": morning_report.SCHEMA, "date": self.date or local.strftime("%Y-%m-%d"),
            "generated_at": now, "language": (profile or {}).get("language") or "en",
            "timezone": ZONE_KEY,
            "utc_offset_minutes": int(local.utcoffset().total_seconds() // 60),
            "profile": {"name": "Daming", "companion": "Rowan"},
            "weather": {"state": "off"}, "greeting": "Good morning, Daming!",
            "headline": "Two quick ones to start with.",
            "summary": "Collie 0.31.0 shipped overnight.", "things_today": 2, "things_total": 2,
            "sections": {
                "wins": {"items": [{"title": "Collie 0.31.0 is out.", "detail": "",
                                    "signal_ids": ["s1"], "link": "", "sources": ["github"],
                                    "when": None}], "more": 0},
                "yours": {"items": [{"title": "Reply to Jo about Friday", "detail": "",
                                     "signal_ids": ["s2"], "link": "", "sources": ["gmail"],
                                     "when": None}], "more": 0},
                "ready": {"items": [], "more": 0},
                "projects": {"items": [{"project": "colliehq/collie", "line": "3 changes waiting",
                                        "signal_ids": ["s3"], "link": "", "sources": ["local"],
                                        "when": None}], "more": 0},
                "reads": {"items": [], "more": 0}},
            "signals": [], "counters": {},
            "provenance": {"read_at": now, "sources": [], "composer": {"mode": "fallback"},
                           "drafts": {"requested": 0}}}


@pytest.fixture
def builds(monkeypatch):
    fake = Builds()
    monkeypatch.setattr(morning_report, "build", fake)
    return fake


@pytest.fixture
def no_brief(monkeypatch):
    """The plain brief must not even be rendered on a morning the report owns."""
    from harness import daily_brief_web

    def refuse(*_a, **_k):
        raise AssertionError("the plain brief was rendered on a report morning")

    monkeypatch.setattr(daily_brief_web, "read", refuse)


def report_on(root, service, **body):
    return opt_in(root, service, report=True, **body)


def job(root):
    return ledger(root)["jobs"][-1]


# ------------------------------------------------------------------ the morning email


def test_with_the_report_on_the_morning_email_is_the_designed_report(host, zones, builds,
                                                                     no_brief):
    root, service, adapter = host
    prefs = report_on(root, service)
    assert prefs["report"] is True and prefs["content"] == "morning_report"
    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["state"] == "submitted" and report["sent"] is True
    assert len(builds.calls) == 1 and builds.calls[0]["drafts"] is True
    # Built in the schedule's own zone and language, so it is dated the day it is sent.
    assert builds.calls[0]["profile"]["timezone"] == ZONE_KEY
    assert builds.calls[0]["profile"]["language"] == "en"

    sent = adapter.sent[0]
    assert sent["destination"] == OWNER
    assert sent["subject"].startswith("Two quick ones to start with.")
    assert "Two quick ones to start with." in sent["html"] and "cid:collie-avatar" in sent["html"]
    assert "Reply to Jo about Friday" in sent["text"]
    assert mail_messages.decode_inline(sent["inline"], sent["html"])[0]["data"] == PNG
    # It is the scheduler's own draft, marked as the report, threaded for replies.
    stored = outbox(service)[0]
    assert stored["metadata"]["source"] == "daily_brief"
    assert stored["metadata"]["content"] == "morning_report"
    assert stored["metadata"]["auto_eligible"] is False
    assert stored["thread_key"] == sched._thread_key(report["job"]["id"])
    assert stored["format"] == "html"
    assert report["job"]["content"] == "morning_report" and report["job"]["format"] == "html"


def test_one_report_a_day_however_often_the_pump_ticks(host, zones, builds, no_brief):
    root, service, adapter = host
    report_on(root, service)
    for minute in (31, 32, 45, 59):
        sched.tick(root, at(2026, 9, 10, 7, minute), service=service)
    assert len(builds.calls) == 1 and len(adapter.sent) == 1
    assert len(outbox(service)) == 1
    assert sched.tick(root, at(2026, 9, 11), service=service)["state"] == "submitted"
    assert len(builds.calls) == 2 and len(adapter.sent) == 2


def test_before_the_hour_nothing_is_built(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    assert sched.tick(root, at(2026, 9, 10, 7, 29), service=service)["state"] == "waiting"
    assert builds.calls == [] and adapter.sent == []


def test_switched_off_nothing_is_built_and_nothing_is_opened(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    sched.configure(root, {"enabled": False}, service=service)
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "disabled"
    assert builds.calls == [] and adapter.sent == []


# ------------------------------------------------------------------ never both


def test_the_report_supersedes_the_brief_and_a_morning_never_gets_both(host, zones, builds,
                                                                       monkeypatch):
    root, service, adapter = host
    opt_in(root, service)                                     # the plain brief
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "submitted"
    assert "html" not in adapter.sent[0]

    # Switched to the report after this morning's brief went: nothing more today.
    report_on(root, service)
    assert sched.tick(root, at(2026, 9, 10, 8, 0), service=service)["state"] == "submitted"
    assert builds.calls == [] and len(adapter.sent) == 1

    # Tomorrow the report is the morning email, and the brief is not rendered at all.
    from harness import daily_brief_web
    monkeypatch.setattr(daily_brief_web, "read", lambda *a, **k: pytest.fail("brief rendered"))
    assert sched.tick(root, at(2026, 9, 11), service=service)["state"] == "submitted"
    assert len(builds.calls) == 1 and "html" in adapter.sent[1]

    # Switched back to the brief after the report went: still one email that morning.
    opt_in(root, service, report=False)
    assert sched.tick(root, at(2026, 9, 11, 9, 0), service=service)["state"] == "submitted"
    assert len(adapter.sent) == 2
    assert [j["content"] for j in ledger(root)["jobs"]] == ["brief", "morning_report"]


def test_a_report_still_being_built_can_become_the_brief_and_is_never_both(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    # A pass died mid-build: the day has a placeholder and no payload.
    builds.fail = KeyboardInterrupt("power cut while the model was answering")
    with pytest.raises(KeyboardInterrupt):
        sched.tick(root, at(2026, 9, 10), service=service)
    assert job(root)["state"] == "preparing" and "text" not in job(root)

    # The person switches to the plain brief: that morning's one email is the brief.
    builds.fail = None
    opt_in(root, service, report=False)
    assert sched.tick(root, at(2026, 9, 10, 7, 45), service=service)["state"] == "submitted"
    assert len(adapter.sent) == 1 and "html" not in adapter.sent[0]
    assert job(root)["content"] == "brief" and len(builds.calls) == 1


# ------------------------------------------------------------------ built once, frozen once


def test_a_report_being_built_is_not_built_twice(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    seen = []

    def meanwhile_another_pass():
        seen.append(sched.tick(root, at(2026, 9, 10, 7, 32), service=service))

    builds.hook = meanwhile_another_pass
    first = sched.tick(root, at(2026, 9, 10), service=service)
    assert first["state"] == "submitted"
    # The pass that arrived while the model was writing saw the lease and left.
    assert seen[0]["state"] == "preparing" and seen[0]["sent"] is False
    assert len(builds.calls) == 1 and len(adapter.sent) == 1


def test_a_restart_sends_the_frozen_report_and_never_rebuilds_it(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    with power_cut(service):
        with pytest.raises(KeyboardInterrupt):
            sched.tick(root, at(2026, 9, 10), service=service)
    frozen = job(root)
    assert frozen["state"] == "sending" and frozen["html"] and frozen["text"]

    builds.date = "1999-01-01"                         # anything built now would differ
    resumed = sched.tick(root, at(2026, 9, 10, 7, 40), service=service)
    assert resumed["state"] == "submitted" and len(builds.calls) == 1
    assert adapter.sent[0]["html"] == frozen["html"] and adapter.sent[0]["text"] == frozen["text"]
    # Settled days keep the evidence, not the person's morning.
    assert "html" not in job(root) and "inline" not in job(root) and "text" not in job(root)


def test_a_report_that_cannot_be_built_is_tried_again_later_then_given_up(host, zones, builds):
    root, service, adapter = host
    report_on(root, service, grace_minutes=120)
    builds.fail = OSError("the snapshot could not be written")
    first = sched.tick(root, at(2026, 9, 10), service=service)
    assert first["state"] == "error" and "tried again" in first["detail"]
    # Not every minute: the next try waits out the lease.
    assert sched.tick(root, at(2026, 9, 10, 7, 33), service=service)["state"] == "preparing"
    assert len(builds.calls) == 1
    sched.tick(root, at(2026, 9, 10, 7, 45), service=service)
    last = sched.tick(root, at(2026, 9, 10, 8, 0), service=service)
    assert len(builds.calls) == sched.MAX_BUILDS
    assert last["state"] == "error" and "nothing was sent" in last["detail"]
    assert sched.tick(root, at(2026, 9, 10, 9, 0), service=service)["state"] == "error"
    assert len(builds.calls) == sched.MAX_BUILDS and adapter.sent == []


def test_a_report_built_for_another_day_is_not_sent(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    builds.date = "2026-09-09"
    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["state"] == "error" and "different day" in report["detail"]
    assert adapter.sent == [] and outbox(service) == []


def test_a_report_too_large_for_email_sends_its_plain_text_and_says_so(host, zones, builds,
                                                                      monkeypatch):
    root, service, adapter = host
    report_on(root, service)
    from harness import morning_report_email
    real = morning_report_email.render

    def enormous(report, avatar="cid"):
        out = real(report, avatar=avatar)
        return dict(out, html=out["html"] + "<!--" + "x" * mail_messages.MAX_HTML_BYTES + "-->")

    monkeypatch.setattr(morning_report_email, "render", enormous)
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "submitted"
    assert "html" not in adapter.sent[0] and "Reply to Jo" in adapter.sent[0]["text"]
    assert job(root)["format"] == "text" and "plain text" in job(root)["format_detail"]


# ------------------------------------------------------------------ same failure semantics


def test_an_unknown_report_is_never_resent_and_a_refused_one_waits_for_a_person(host, zones,
                                                                               builds):
    root, service, adapter = host
    report_on(root, service)
    adapter.fail = RuntimeError("the connection dropped mid-conversation")
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "unknown"
    adapter.fail = None
    for hour in (8, 9):
        assert sched.tick(root, at(2026, 9, 10, hour, 0), service=service)["state"] == "unknown"
    assert len(adapter.sent) == 1 and len(builds.calls) == 1

    refusal = RuntimeError("the account rejected the message")
    refusal.delivery_unknown = False
    adapter.fail = refusal
    assert sched.tick(root, at(2026, 9, 11), service=service)["state"] == "failed"
    adapter.fail = None
    assert sched.tick(root, at(2026, 9, 11, 9, 0), service=service)["state"] == "failed"
    assert len(adapter.sent) == 2 and outbox(service)[-1]["retryable"] is True


def test_switching_it_off_while_the_report_is_built_sends_nothing(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    builds.hook = lambda: sched.configure(root, {"enabled": False}, service=service)
    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["sent"] is False and adapter.sent == []
    assert "switched off" in report["detail"]


def test_a_report_prepared_for_one_account_is_never_sent_from_another(host, zones, builds):
    root, service, adapter = host
    service.configure("second", kind="imap", config={"address": "other@example.test"},
                      owner="second-owner@example.test", credentials={"password": "p"})
    report_on(root, service)
    builds.hook = lambda: sched.configure(root, {"enabled": True, "connection": "second",
                                                 "timezone": ZONE_KEY, "report": True},
                                          service=service)
    report = sched.tick(root, at(2026, 9, 10), service=service)
    assert report["sent"] is False and adapter.sent == []
    assert comms.list_results("second", directory=service.directory) == []


def test_yesterdays_unfinished_build_is_closed_not_sent(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    builds.fail = KeyboardInterrupt("power cut")
    with pytest.raises(KeyboardInterrupt):
        sched.tick(root, at(2026, 9, 10), service=service)
    builds.fail = None
    assert sched.tick(root, at(2026, 9, 11), service=service)["state"] == "submitted"
    assert [j["state"] for j in ledger(root)["jobs"]] == ["expired", "submitted"]
    assert len(adapter.sent) == 1


# ------------------------------------------------------------------ settings


def test_the_report_choice_and_the_drafts_switch_are_explicit(host, zones, own_settings):
    root, service, _adapter = host
    with pytest.raises(sched.ScheduleError, match="true or false"):
        opt_in(root, service, report="yes")
    with pytest.raises(sched.ScheduleError, match="true or false"):
        opt_in(root, service, gmail_drafts="off")
    assert sched.preferences(root)["enabled"] is False

    prefs = opt_in(root, service, report=True, gmail_drafts=False)
    assert prefs["report"] is True and prefs["gmail_drafts"] is False
    assert own_settings.get("REPORT_GMAIL_DRAFTS") == "off"
    assert opt_in(root, service, gmail_drafts=True)["gmail_drafts"] is True
    assert own_settings.get("REPORT_GMAIL_DRAFTS") == "on"
    # Settings saved before the report existed read as the plain brief, and still load.
    path = sched._path(root, "default")
    older = ledger(root)
    del older["prefs"]["report"]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(older, handle)
    prefs = sched.preferences(root)
    assert prefs["available"] is True and prefs["enabled"] is True and prefs["report"] is False


def test_a_drafts_switch_held_by_the_environment_is_refused_not_ignored(host, zones,
                                                                       monkeypatch):
    root, service, _adapter = host
    monkeypatch.setenv("COLLIE_REPORT_GMAIL_DRAFTS", "on")
    prefs = opt_in(root, service, report=True)
    assert prefs["gmail_drafts"] is True and prefs["gmail_drafts_locked"] is True
    with pytest.raises(sched.ScheduleError, match="environment"):
        opt_in(root, service, report=True, gmail_drafts=False)


def test_public_views_say_what_was_sent_never_the_report(host, zones, builds):
    root, service, _adapter = host
    report_on(root, service)
    sched.tick(root, at(2026, 9, 10), service=service)
    public = json.dumps(sched.preferences(root, service=service))
    for private in (OWNER, "Reply to Jo", "Two quick ones", "cid:collie-avatar"):
        assert private not in public
    last = sched.preferences(root)["last"]
    assert last["content"] == "morning_report" and last["format"] == "html"
    assert last["trigger"] == "schedule"


# ------------------------------------------------------------------ send me one now


def test_send_me_one_now_is_a_real_send_that_leaves_the_morning_alone(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    now = sched.send_now(root, service=service, now=at(2026, 9, 10, 6, 0))
    assert now["state"] == "submitted" and now["sent"] is True
    assert now["job"]["trigger"] == "request" and len(adapter.sent) == 1
    assert adapter.sent[0]["destination"] == OWNER and adapter.sent[0]["html"]
    # The morning's own email still goes at its time.
    assert sched.tick(root, at(2026, 9, 10), service=service)["state"] == "submitted"
    assert len(adapter.sent) == 2 and len(builds.calls) == 2
    ids = {row["id"] for row in outbox(service)}
    assert len(ids) == 2


def test_send_me_one_now_needs_saved_settings_and_is_rate_limited(host, zones, builds):
    root, service, adapter = host
    with pytest.raises(sched.ScheduleError, match="save"):
        sched.send_now(root, service=service, now=at(2026, 9, 10, 6, 0))
    report_on(root, service)
    sched.configure(root, {"enabled": False}, service=service)
    # Off is not "never set up": the saved account can still be sent one on request.
    for minute in range(sched.NOW_PER_DAY):
        assert sched.send_now(root, service=service,
                              now=at(2026, 9, 10, 6, minute))["state"] == "submitted"
    with pytest.raises(sched.ScheduleError, match="a day"):
        sched.send_now(root, service=service, now=at(2026, 9, 10, 6, 30))
    assert len(adapter.sent) == sched.NOW_PER_DAY
    assert sched.send_now(root, service=service, now=at(2026, 9, 11, 6, 0))["sent"] is True


def test_send_me_one_now_refuses_a_second_while_one_is_on_its_way(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    refused = []

    def click_again():
        try:
            sched.send_now(root, service=service, now=at(2026, 9, 10, 6, 1))
        except sched.ScheduleError as exc:
            refused.append(str(exc))

    builds.hook = click_again
    assert sched.send_now(root, service=service, now=at(2026, 9, 10, 6, 0))["sent"] is True
    assert refused and "already" in refused[0]
    assert len(adapter.sent) == 1 and len(builds.calls) == 1


def test_send_me_one_now_never_goes_to_a_changed_owner(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    service.configure("mail", kind="imap", config={"address": "collie@example.test"},
                      owner="somebody-else@example.test")
    with pytest.raises(sched.ScheduleError, match="owner address changed"):
        sched.send_now(root, service=service, now=at(2026, 9, 10, 6, 0))
    assert adapter.sent == [] and builds.calls == []


def test_a_failed_build_on_request_says_so_and_sends_nothing(host, zones, builds):
    root, service, adapter = host
    report_on(root, service)
    builds.fail = OSError("disk full")
    out = sched.send_now(root, service=service, now=at(2026, 9, 10, 6, 0))
    assert out["state"] == "error" and out["sent"] is False and adapter.sent == []
    builds.fail = None
    assert sched.send_now(root, service=service, now=at(2026, 9, 10, 6, 5))["sent"] is True
