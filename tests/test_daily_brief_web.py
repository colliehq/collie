"""What the Daily Brief surface must never get wrong.

:mod:`harness.daily_brief` already has its own suite for the snapshot rules.  These
tests cover the half that talks to real stores and to the browser:

* a brief built for one root never reads another root's stores, and a suppression
  saved under one profile does not hide anything for another;
* one broken source costs itself, not the brief: it is reported ``unavailable`` with
  a reason that names no path and quotes no row, and the rest of the day still renders;
* the two process-memory sources (running runs, waiting approvals) are explicitly
  unavailable when they cannot be read from here, rather than rendered as "nothing
  is running";
* hiding is a brief preference and a waiting decision cannot be hidden at all;
* every malformed POST is refused by name instead of half-applied.

Everything runs against ``tmp_path``.  No network, no mail, no credentials, no real
mailbox and no personal data of any kind appears in this file.
"""
import json
import os
import sys
import time
import types

import pytest

from harness import daily_brief as db
from harness import daily_brief_web as web

NOW = 1790000000.0                       # a fixed instant; nothing here depends on today


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "state-a")


@pytest.fixture
def other(tmp_path):
    return str(tmp_path / "state-b")


def add_meeting(root, *, title, start_at):
    """Put one real row in one root's calendar store, through its own public API."""
    from harness.meeting_reminders import ReminderStore
    os.makedirs(root, exist_ok=True)
    store = ReminderStore(os.path.join(root, "meeting-reminders.json"))
    return store.save_event(title=title, start_at=start_at, end_at=start_at + 1800)


def titles(brief, key="agenda"):
    return [row["title"] for row in brief[key]]


class _Adapter:
    """A transport that is never asked for anything. Every call here is a test failure.

    The point of the messages collector is that it reads stored rows: a mailbox is not
    opened, a provider is not called, and no credential is fetched. Wiring the adapter
    to explode is how that stays true after somebody adds a "just refresh it first".
    """

    def validate_config(self, config):
        return dict(config)

    def __getattr__(self, name):
        def refuse(*_args, **_kwargs):
            raise AssertionError("the brief reached the transport: %s" % name)
        return refuse


@pytest.fixture
def mailbox(root, monkeypatch):
    """One configured connection under ``root``, with a transport that must not be used.

    Addresses here are ``example.test`` fixtures and the password is a literal in this
    file: nothing in this suite touches a real account.
    """
    from harness.channel_service import ChannelService
    monkeypatch.setattr(ChannelService, "_adapter", staticmethod(lambda kind: _Adapter()))
    service = ChannelService(root)
    service.configure("mail", kind="imap", config={"address": "collie@example.test"},
                      owner="owner@example.test", credentials={"password": "fixture-only"},
                      workspace=root)
    return service


def arrive(service, event_id="one", *, sender="owner@example.test", subject="Invoice question",
           text="A private body that belongs to the mailbox and nowhere else."):
    return service.ingest("mail", {"event_id": event_id, "sender": sender,
                                   "recipient": "collie@example.test", "subject": subject,
                                   "text": text, "message_id": "<%s@example.test>" % event_id,
                                   "in_reply_to": [], "references": [], "attachments": []})


def message_rows(payload, key="events", connection=0):
    return payload["connections"][connection][key]


def test_an_answered_message_leaves_progress_while_its_draft_remains_visible(mailbox):
    arrive(mailbox)
    mailbox.accept("mail", "one", start=False)
    mailbox.prepare_reply("mail", "answer", text="Prepared reply", event_id="one")
    payload = web._communications(mailbox.root, False, NOW)
    assert message_rows(payload)[0]["settled"] is True
    brief = db.build({"communications": payload}, now=NOW)
    assert not brief["progress"]
    assert len(brief["attention"]) == 1
    assert brief["attention"][0]["kind"] == "reply"


# --------------------------------------------------------------- collection


def test_every_source_is_reported_and_a_failure_is_not_an_empty_day(root, monkeypatch):
    def explode(*_args):
        raise OSError("/private/path/to/somebodys-mailbox.db is locked")

    monkeypatch.setitem(web.COLLECTORS, "missions", explode)
    payloads, report = web.collect(root)

    assert sorted(payloads) == sorted(db.SOURCE_NAMES)
    assert [row["name"] for row in report] == list(db.SOURCE_NAMES)
    failed = next(row for row in report if row["name"] == "missions")
    assert failed["state"] == "unavailable"
    # A reason a person can act on, with nothing from inside the store in it.
    assert failed["reason"] == "missions could not be read (OSError)"
    assert "mailbox" not in failed["reason"] and "/private" not in failed["reason"]
    assert payloads["missions"]["__unavailable"] is True

    brief = db.build(payloads, now=NOW, state_dir=root)
    assert brief["all_clear"] is False
    assert "missions" in brief["coverage"]["unavailable"]
    assert any("Nothing missing has been assumed clear" in note for note in brief["notices"])
    # The sources that did answer still produced a day.
    assert brief["coverage"]["ok"]


def test_a_source_that_returns_a_non_dict_is_unavailable_not_trusted(root, monkeypatch):
    monkeypatch.setitem(web.COLLECTORS, "personal", lambda *_: ["not", "a", "payload"])
    payloads, report = web.collect(root)
    assert next(row for row in report if row["name"] == "personal")["state"] == "unavailable"
    assert payloads["personal"]["__unavailable"] is True


def test_roots_are_isolated(root, other):
    add_meeting(root, title="Root A planning", start_at=NOW + 3600)

    mine = web.read(root, now=NOW, utc_offset_minutes=0)["brief"]
    theirs = web.read(other, now=NOW, utc_offset_minutes=0)["brief"]

    assert "Root A planning" in titles(mine)
    assert titles(theirs) == []
    # And the empty one says so rather than claiming a clear calendar.
    assert any("No calendar is connected" in note for note in theirs["notices"])


def test_inbox_reads_the_execution_store_and_honors_profile_isolation(root, other, monkeypatch):
    from harness import sessions, task_inbox
    monkeypatch.setenv("COLLIE_STATE_DIR", root)
    custom = os.path.join(root, "custom-journals")
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", custom)
    task_inbox.enqueue("brief-queued-task", "input-1", "Continue this task", directory=custom)
    assert sessions.store_root(root) == os.path.realpath(custom)
    own = web._task_inbox(root, False, NOW)
    assert [row["session"] for row in own["sessions"]] == ["brief-queued-task"]
    assert web._task_inbox(other, False, NOW)["sessions"] == []


def test_process_memory_sources_are_unavailable_with_a_reason_when_unreadable(root):
    report = {row["name"]: row for row in web.read(root, now=NOW)["sources"]}
    for name in ("runs", "approvals"):
        assert report[name]["state"] == "unavailable"
        assert "running Collie app" in report[name]["reason"]


def test_process_memory_sources_are_read_only_for_their_own_root(root, other, monkeypatch):
    """Inside the serving process the live registries are used -- for that root only."""
    handler = types.SimpleNamespace(
        _runs_snapshot=lambda: [{"session": "s1", "state": "running", "ask": "Build the thing"}],
        _inbox_pending_all=lambda: [{"id": "a1", "tool": "shell", "session": "s1",
                                     "title": "Run a command"}])
    monkeypatch.setitem(sys.modules, "harness.webapp",
                        types.SimpleNamespace(Handler=handler, mission_ticker_status=dict))
    monkeypatch.setattr(web, "_live_root", lambda: os.path.realpath(root))

    answer = web.read(root, now=NOW)
    report = {row["name"]: row for row in answer["sources"]}
    assert report["runs"]["state"] == "ok" and report["approvals"]["state"] == "ok"
    assert "Build the thing" in titles(answer["brief"], "progress")
    assert [row["kind"] for row in answer["brief"]["top"]] == ["approval"]

    # The same live process, asked about a different root, must not hand over the
    # first root's running work.
    elsewhere = {row["name"]: row for row in web.read(other, now=NOW)["sources"]}
    assert elsewhere["runs"]["state"] == "unavailable"
    assert elsewhere["approvals"]["state"] == "unavailable"


def test_opening_the_brief_reports_freshness_per_source(root):
    answer = web.read(root, now=NOW)
    assert answer["schema"] == web.SCHEMA
    for row in answer["sources"]:
        assert row["collected_at"] == NOW
        assert row["endpoint"] == db.PAYLOAD_ENDPOINTS[row["name"]]
        assert row["took_ms"] >= 0
    assert answer["collected_at"] == NOW
    assert answer["brief"]["generated_at"] == NOW


# --------------------------------------------------------------- messages


def test_the_collector_list_is_the_source_list():
    """One list, derived: adding a source to the builder without a collector here used
    to produce a brief that silently reported it ``absent`` forever."""
    assert sorted(web.COLLECTORS) == sorted(db.SOURCE_NAMES)
    assert "communications" in db.SOURCE_NAMES
    assert db.PAYLOAD_ENDPOINTS["communications"] == "/api/channels"


def test_messages_are_read_from_the_store_and_the_transport_is_never_touched(mailbox):
    arrive(mailbox, "one")
    payload = web._communications(mailbox.root, False, NOW)      # _Adapter would raise

    row = message_rows(payload)[0]
    assert row["id"] == "one" and row["state"] == "pending"
    assert row["subject"] == "Invoice question" and row["sender_allowed"] is True
    assert payload["connections"][0]["enabled"] is True
    assert payload["truncated"] is False


def test_the_collector_carries_no_body_no_address_and_no_attachment_name(mailbox, root):
    arrive(mailbox, "one", text="A private body that nobody outside the mailbox may see.")
    mailbox.prepare_reply("mail", "r1", text="A drafted answer.", event_id="one")

    surface = json.dumps(web._communications(root, False, NOW))
    for secret in ("A private body", "A drafted answer", "owner@example.test",
                   "collie@example.test", "fixture-only", "message_id"):
        assert secret not in surface
    # And the same holds for everything the whole surface returns, rendered.
    answer = web.read(root, now=NOW)
    whole = json.dumps(answer) + answer["email"]["text"]
    for secret in ("A private body", "A drafted answer", "owner@example.test", "fixture-only"):
        assert secret not in whole


def test_a_waiting_message_and_a_failed_reply_reach_the_brief(mailbox, root):
    from harness import communications as comms

    arrive(mailbox, "one")
    mailbox.prepare_reply("mail", "r1", text="Here is the answer.", event_id="one")
    claim = comms.claim_send("mail", "r1", directory=mailbox.directory)
    comms.mark_failed("mail", "r1", token=claim["token"],
                      error="the provider refused it", directory=mailbox.directory)

    brief = web.read(root, now=NOW, utc_offset_minutes=0)["brief"]
    kinds = [(row["kind"], row["status"]) for row in brief["attention"]]
    assert ("reply", "failed") in kinds and ("message", "ready_for_review") in kinds
    assert brief["all_clear"] is False
    assert [row["source_ref"]["href"] for row in brief["attention"]] == \
        ["/communications", "/communications"]


def test_a_message_from_a_stranger_is_kept_but_makes_no_demand(mailbox, root):
    arrive(mailbox, "two", sender="stranger@example.test", subject="URGENT act now")
    brief = web.read(root, now=NOW)["brief"]
    assert brief["attention"] == []
    assert brief["counts"]["messages_quiet"] == 1
    assert "URGENT" not in json.dumps(brief)


def test_a_paused_connection_still_reports_the_work_it_holds(mailbox, root):
    arrive(mailbox, "one")
    mailbox.set_enabled("mail", False)

    payload = web._communications(root, False, NOW)
    assert payload["connections"][0]["enabled"] is False
    assert [row["id"] for row in message_rows(payload)] == ["one"]
    brief = web.read(root, now=NOW)["brief"]
    assert [row["status"] for row in brief["attention"]] == ["ready_for_review"]


def test_one_unreadable_connection_costs_itself_and_not_the_day(mailbox, root, monkeypatch):
    from harness.channel_service import ChannelService

    arrive(mailbox, "one")

    def explode(self, connection, **kwargs):
        raise OSError("/private/path/to/somebodys-mailbox.json is locked")

    monkeypatch.setattr(ChannelService, "events", explode)
    payload = web._communications(root, False, NOW)
    assert payload["connections"][0]["unreadable"] is True
    assert "mailbox.json" not in json.dumps(payload)      # no store text travels

    answer = web.read(root, now=NOW)
    report = {row["name"]: row for row in answer["sources"]}
    # The source answered -- in part. Reporting it unavailable would throw away the
    # connections that did open; reporting nothing would call an unread mailbox clear.
    assert report["communications"]["state"] == "ok"
    assert answer["brief"]["coverage"]["partial"] == ["communications"]
    assert answer["brief"]["all_clear"] is False
    assert "/private" not in json.dumps(answer)


def test_a_mailbox_larger_than_the_window_is_reported_as_partial(mailbox, root, monkeypatch):
    monkeypatch.setattr(web, "MESSAGE_LIMIT", 2)
    for index in range(3):
        arrive(mailbox, "m%d" % index)

    payload = web._communications(root, False, NOW)
    assert len(message_rows(payload)) == 2
    assert payload["connections"][0]["truncated"] is True
    brief = db.build(web.collect(root, now=NOW)[0], now=NOW)
    assert brief["coverage"]["partial"] == ["communications"]
    assert brief["all_clear"] is False


def test_message_stores_are_isolated_by_root(mailbox, root, other):
    arrive(mailbox, "one")
    assert message_rows(web._communications(root, False, NOW))
    assert web._communications(other, False, NOW) == {"connections": [], "truncated": False}
    assert web.read(other, now=NOW)["brief"]["counts"]["connections"] == 0


def test_no_connections_is_an_empty_store_and_not_a_failure(root):
    answer = web.read(root, now=NOW)
    report = {row["name"]: row for row in answer["sources"]}
    assert report["communications"]["state"] == "ok" and not report["communications"]["reason"]
    assert answer["brief"]["counts"]["connections"] == 0
    assert answer["brief"]["coverage"]["partial"] == []
    assert not [note for note in answer["brief"]["notices"] if "connection" in note]


def test_channel_settings_that_need_repair_are_unavailable_without_quoting_them(root):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "channels.json"), "w", encoding="utf-8") as handle:
        handle.write('{"version": 1, "connections": "not-a-mapping"}')

    answer = web.read(root, now=NOW)
    report = {row["name"]: row for row in answer["sources"]}
    assert report["communications"]["state"] == "unavailable"
    assert report["communications"]["reason"] == "communications could not be read (ChannelError)"
    assert answer["brief"]["all_clear"] is False
    assert "communications" in answer["brief"]["coverage"]["unavailable"]


# --------------------------------------------------------------- route contract


def test_read_carries_the_brief_email_preview_and_hidden_list(root):
    answer = web.read(root, timezone="Asia/Shanghai", utc_offset_minutes=480, language="zh",
                      now=NOW)
    assert set(answer) >= {"schema", "brief", "sources", "email", "feedback", "profile",
                           "unsuppressible", "collected_at"}
    assert answer["brief"]["schema"] == db.SCHEMA
    assert answer["brief"]["language"] == "zh"
    assert answer["email"]["available"] is True and answer["email"]["subject"]
    # The email is a preview only: nothing in this surface can send it yet, and it
    # says so rather than offering a button that quietly does nothing.
    assert answer["email"]["sendable"] is False and answer["email"]["reason"]
    assert answer["feedback"] == {"hidden": [], "count": 0}
    assert answer["unsuppressible"] == ["approval"]


def test_an_unknown_timezone_degrades_to_the_browser_offset_and_says_so(root):
    zone = web.read(root, timezone="Not/AZone", utc_offset_minutes=330, now=NOW
                    )["brief"]["timezone"]
    assert zone["degraded"] is True and zone["reason"]
    assert zone["utc_offset_minutes"] == 330


def test_preview_renders_the_same_snapshot_without_recording_it(root):
    answer = web.perform(root, {"action": "preview", "language": "en"}, now=NOW)
    assert answer["ok"] is True and answer["brief_id"].startswith("brief-")
    assert answer["email"]["text"].strip()
    assert [row["name"] for row in answer["sources"]] == list(db.SOURCE_NAMES)
    # A preview is a read: it must not age the "days_seen" bookkeeping behind the
    # user's back, so it leaves no brief in the history log.
    assert db.feedback_state(state_dir=root)["briefs"] == []


# --------------------------------------------------------------- feedback


def test_hiding_persists_is_reversible_and_is_not_completion(root):
    add_meeting(root, title="Standup", start_at=NOW + 3600)
    answer = web.read(root, now=NOW, utc_offset_minutes=0)
    item = answer["brief"]["agenda"][0]

    saved = web.perform(root, {"action": "dismiss", "item_id": item["id"],
                               "fingerprint": item["fingerprint"]}, now=NOW)
    assert saved["ok"] is True

    after = web.read(root, now=NOW, utc_offset_minutes=0)
    assert titles(after["brief"]) == []
    assert after["feedback"]["count"] == 1
    assert after["feedback"]["hidden"][0]["item_id"] == item["id"]
    assert after["feedback"]["hidden"][0]["title"] == "Standup"
    # Hiding is a rendering preference. The row is still there, and the brief says so.
    assert after["brief"]["counts"]["suppressed"] == 1
    assert any("does not cancel or complete it" in note for note in after["brief"]["notices"])

    restored = web.perform(root, {"action": "restore", "item_id": item["id"]}, now=NOW)
    assert restored["ok"] is True
    assert titles(web.read(root, now=NOW, utc_offset_minutes=0)["brief"]) == ["Standup"]


def test_a_snooze_expires_and_is_bounded(root):
    add_meeting(root, title="Standup", start_at=NOW + 3600)
    item = web.read(root, now=NOW, utc_offset_minutes=0)["brief"]["agenda"][0]
    web.perform(root, {"action": "snooze", "item_id": item["id"], "minutes": 60}, now=NOW)

    assert titles(web.read(root, now=NOW, utc_offset_minutes=0)["brief"]) == []
    assert titles(web.read(root, now=NOW + 3601, utc_offset_minutes=0)["brief"]) == ["Standup"]
    assert web.read(root, now=NOW + 3601)["feedback"] == {"hidden": [], "count": 0}

    for minutes in (0, -5, web.SNOOZE_MAX_MINUTES + 1, "60", True, None):
        with pytest.raises(db.BriefError):
            web.perform(root, {"action": "snooze", "item_id": item["id"], "minutes": minutes},
                        now=NOW)


def test_a_waiting_decision_cannot_be_hidden(root, monkeypatch):
    monkeypatch.setitem(web.COLLECTORS, "approvals", lambda *_: {"approvals": [
        {"id": "ap-1", "tool": "shell", "session": "s1", "title": "Delete a folder"}]})
    item = web.read(root, now=NOW)["brief"]["top"][0]
    assert item["kind"] == "approval"

    refused = web.perform(root, {"action": "dismiss", "item_id": item["id"]}, now=NOW)
    assert refused["ok"] is False and "cannot be hidden" in refused["error"]

    again = web.read(root, now=NOW)
    assert [row["id"] for row in again["brief"]["top"]] == [item["id"]]
    assert again["feedback"]["count"] == 0


def test_changed_items_are_not_still_listed_as_hidden(root, monkeypatch):
    mission = {"mission_id": "work", "title": "Weekly report", "state": "needs_you"}
    monkeypatch.setitem(web.COLLECTORS, "missions", lambda *_: {"missions": [mission]})
    item = web.read(root, now=NOW)["brief"]["top"][0]
    web.perform(root, {"action": "dismiss", "item_id": item["id"],
                       "fingerprint": item["fingerprint"]}, now=NOW)
    assert web.read(root, now=NOW)["feedback"]["count"] == 1
    mission["state"] = "failed"
    answer = web.read(root, now=NOW + 60)
    assert answer["brief"]["top"][0]["title"] == "Weekly report"
    assert answer["feedback"] == {"hidden": [], "count": 0}


def test_profiles_do_not_share_suppressions(root):
    add_meeting(root, title="Standup", start_at=NOW + 3600)
    item = web.read(root, now=NOW, utc_offset_minutes=0)["brief"]["agenda"][0]
    web.perform(root, {"action": "dismiss", "item_id": item["id"]}, profile="work", now=NOW)

    assert titles(web.read(root, profile="work", now=NOW, utc_offset_minutes=0)["brief"]) == []
    assert titles(web.read(root, now=NOW, utc_offset_minutes=0)["brief"]) == ["Standup"]


# --------------------------------------------------------------- bad requests


@pytest.mark.parametrize("body", [
    None, [], "dismiss", {}, {"action": ""}, {"action": "delete"}, {"action": "send"},
    {"action": "dismiss"}, {"action": "dismiss", "item_id": ""},
    {"action": "dismiss", "item_id": "   "}, {"action": "dismiss", "item_id": 7},
    {"action": "dismiss", "item_id": "../../etc/passwd"},
    {"action": "dismiss", "item_id": "meeting:not-a-digest"},
    {"action": "restore", "item_id": None},
    {"action": "snooze", "item_id": "meeting:0123456789abcdef"},
])
def test_malformed_requests_are_refused_by_name(root, body):
    with pytest.raises(db.BriefError):
        web.perform(root, body, now=NOW)


def test_a_refusal_writes_nothing(root):
    with pytest.raises(db.BriefError):
        web.perform(root, {"action": "dismiss", "item_id": "meeting:zzzz"}, now=NOW)
    assert web.read(root, now=NOW)["feedback"]["count"] == 0


def test_an_invalid_profile_name_is_refused(root):
    with pytest.raises(db.BriefError):
        web.perform(root, {"action": "dismiss", "item_id": "meeting:0123456789abcdef",
                           "profile": "../escape"}, now=NOW)


def test_a_wall_clock_read_still_works(root):
    """The default path the route takes: no injected clock anywhere."""
    answer = web.read(root)
    assert answer["brief"]["date"]
    assert abs(answer["collected_at"] - time.time()) < 60


# --------------------------------------------------------------- browser surface


def test_ui_static_checks():
    """Run the Node checks over daily_brief.html from pytest, so CI sees them."""
    import shutil
    import subprocess

    suite = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_brief_ui_test.mjs")
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    done = subprocess.run([node, suite], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
