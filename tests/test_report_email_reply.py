"""Replying to the morning report email: the frozen report comes back, and "mute" works.

Two things a reply to the report can do.  A reply that asks about the morning ("what was
the first one?") is answered against the report that was actually sent, restored as a
quoted, untrusted snapshot -- exactly as a reply to the Daily Brief is.  And a reply whose
first line is ``mute <project>`` adds that project to ``REPORT_MUTED`` and answers with a
confirmation, the way the email's footer promises.

Everything that must *not* happen is most of this file: a stranger who copies the thread
headers, a reply to the plain brief, a "mute" on the second line, an automatic message,
the same reply delivered twice, and a setting held by an environment variable.

Real ledger, real outbox, real ChannelService intake and threading, a fake mail adapter,
a stand-in report builder and a settings file of the test's own.  No network.
"""
import pytest

from harness import communications as comms, daily_brief_reply as reply_mod, \
    daily_brief_schedule as sched, input_assets, morning_report
from test_daily_brief_schedule import (              # noqa: F401 - pytest fixtures
    OWNER, ZONE_KEY, at, host, opt_in, outbox, zones)
from test_report_email_schedule import (             # noqa: F401 - pytest fixtures
    builds, own_settings, report_on, tiny_avatar)

OTHER = "someone-else@example.test"


def sent_report(host, day=10):
    root, service, _adapter = host
    report_on(root, service)
    tick = sched.tick(root, at(2026, 9, day), service=service)
    assert tick["state"] == "submitted"
    return comms.get_result("mail", tick["job"]["result_id"], include_private=True,
                            directory=service.directory)


def arrive(service, row, *, event_id="in-1", sender=OWNER, text="mute colliehq/collie",
           refs=None, automatic=False):
    refs = [row["metadata"]["message_id"]] if refs is None else refs
    return service.ingest("mail", {
        "event_id": event_id, "sender": sender, "recipient": "collie@example.test",
        "subject": "Re: " + row["subject"], "text": text,
        "message_id": "<%s@example.test>" % event_id, "automatic": automatic,
        "in_reply_to": list(refs), "references": list(refs)})


def event(service, event_id="in-1"):
    return comms.get_event("mail", event_id, include_private=True, directory=service.directory)


def confirmations(service):
    return [row for row in outbox(service)
            if (row.get("metadata") or {}).get("source") == "morning_report_mute"]


# ------------------------------------------------------------------ the frozen report


def test_a_reply_gets_the_report_that_was_sent_back_as_an_untrusted_quote(host, zones, builds,
                                                                         own_settings):
    root, service, _adapter = host
    row = sent_report(host)
    arrive(service, row, text="what was the first one?")
    item = reply_mod.reply_context(service, event(service), "mail")
    assert item["kind"] == "morning_report_snapshot"
    assert "Morning report" in item["label"] and "untrusted" in item["label"]
    assert "2026-09-10" in item["label"]
    assert row["text"] in item["content"]                  # the frozen text, not a rebuild
    assert "morning report" in item["content"].split("-----")[0]
    assert len(builds.calls) == 1
    assert input_assets.validate_contexts([item])
    # A question is not a command: it stays with the person, and nothing was muted.
    assert event(service)["state"] == "pending" and not own_settings.get("REPORT_MUTED")


# ------------------------------------------------------------------ mute <project>


def test_mute_on_the_first_line_mutes_the_project_and_says_so(host, zones, builds, own_settings):
    root, service, adapter = host
    row = sent_report(host)
    arrive(service, row, text="Mute colliehq/collie\n\nOn Thu, Rowan wrote:\n> Two quick ones")
    assert morning_report.muted_names() == ["colliehq/collie"]
    handled = event(service)
    assert handled["state"] == "rejected"                   # never becomes a task
    assert "mute" in handled["rejection"]["reason"].lower()
    [reply] = confirmations(service)
    assert reply["state"] == "submitted" and reply["destination"] == OWNER
    assert "colliehq/collie" in reply["text"] and "muted" in reply["text"].lower()
    assert reply["in_reply_to"] == "<in-1@example.test>"
    assert reply["thread_key"] == row["thread_key"]
    assert reply["subject"].lower().startswith("re:")
    assert len(adapter.sent) == 2                           # the report, then the confirmation
    # The next report leaves it out: muted_names() is what build() filters by.
    assert "colliehq/collie" in morning_report.muted_names()


def test_the_same_reply_twice_mutes_once_and_confirms_once(host, zones, builds, own_settings):
    root, service, adapter = host
    row = sent_report(host)
    arrive(service, row)
    arrive(service, row)                                    # the transport hands it over again
    assert morning_report.muted_names() == ["colliehq/collie"]
    assert len(confirmations(service)) == 1 and len(adapter.sent) == 2

    # A second, separate reply naming it again changes nothing and says so.
    arrive(service, row, event_id="in-2", text="mute COLLIEHQ/collie")
    assert morning_report.muted_names() == ["colliehq/collie"]
    second = [r for r in confirmations(service) if r["in_reply_to"] == "<in-2@example.test>"][0]
    assert "already" in second["text"]


def test_other_projects_already_muted_are_kept(host, zones, builds, own_settings):
    root, service, _adapter = host
    own_settings.update({"REPORT_MUTED": "AgentGalaxy; old-thing"})
    row = sent_report(host)
    arrive(service, row, text="mute collie")
    assert morning_report.muted_names() == ["AgentGalaxy", "old-thing", "collie"]


def passed_on(service):
    """Messages the mute command handed on as ordinary replies (private views)."""
    return [row for row in comms.list_events("mail", include_private=True,
                                             directory=service.directory)
            if (row.get("metadata") or {}).get("derived_from")]


@pytest.mark.parametrize("text, rest", [
    ("mute collie\n\nAlso reschedule my 3pm with Jo to Friday.",
     "Also reschedule my 3pm with Jo to Friday."),
    ("Mute collie. Also can you draft a reply to Jo about Friday",
     "Also can you draft a reply to Jo about Friday"),
    ("mute colliehq/collie, and thanks for the drafts!", "and thanks for the drafts!"),
])
def test_mute_and_the_rest_of_the_reply_is_passed_on(host, zones, builds, own_settings,
                                                     text, rest):
    root, service, adapter = host
    row = sent_report(host)
    arrive(service, row, text=text)
    muted = morning_report.muted_names()
    assert len(muted) == 1 and muted[0] in ("collie", "colliehq/collie")
    assert event(service)["state"] == "rejected"             # the command itself is settled
    [handed] = passed_on(service)
    # The rest is an ordinary reply in the same thread, as if the command were not there.
    assert handed["state"] == "pending" and rest in handed["text"]
    assert "mute" not in handed["text"].lower()
    assert handed["thread_key"] == row["thread_key"] and handed["sender"] == OWNER
    assert handed["metadata"]["message_id"] == "<in-1@example.test>"
    [reply] = confirmations(service)
    assert "“%s”" % muted[0] in reply["text"] and "passed on" in reply["text"]


def test_quoted_history_and_a_signature_are_not_a_rest(host, zones, builds, own_settings):
    root, service, _adapter = host
    row = sent_report(host)
    arrive(service, row, text="mute collie\n\n-- \nDaming\nSent from my iPhone\n\n"
                              "On Thu, Rowan wrote:\n> Two quick ones\n> mute me")
    assert morning_report.muted_names() == ["collie"]
    assert passed_on(service) == []
    assert "passed on" not in confirmations(service)[0]["text"]


@pytest.mark.parametrize("text", [
    "Mute the standup reminders until Friday please",          # not a project
    "mute nothere",                                             # no such project in the report
    "mute collie‮eilloc",                                  # a bidi override in the name
    "mute colliehq/collie/extra",                               # not one project token
    "mute coll​ie",                                        # a zero-width character
])
def test_a_first_line_that_is_not_one_known_project_is_an_ordinary_reply(
        host, zones, builds, own_settings, text):
    root, service, adapter = host
    row = sent_report(host)
    arrive(service, row, text=text)
    assert not own_settings.get("REPORT_MUTED")
    assert event(service)["state"] == "pending"                 # the person's, as it came
    assert confirmations(service) == [] and passed_on(service) == []


def saved_muted(settings_module):
    raw = settings_module._read_uncached().get("REPORT_MUTED", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def test_a_mute_adds_to_the_saved_list_not_to_a_stale_copy(host, zones, builds, own_settings):
    root, service, _adapter = host
    own_settings.update({"REPORT_MUTED": "a"})
    own_settings.apply()                                   # this process last read "a"
    # The settings panel (or another process) has since saved a longer list.
    import json
    with open(own_settings._PATH, "w", encoding="utf-8") as fh:
        json.dump({"REPORT_MUTED": "a, b"}, fh)
    row = sent_report(host)
    arrive(service, row, text="mute collie")
    assert saved_muted(own_settings) == ["a", "b", "collie"]


def test_a_long_list_is_never_cut_and_a_full_one_is_refused_out_loud(host, zones, builds,
                                                                     own_settings):
    root, service, _adapter = host
    names = ["proj%03d" % i for i in range(230)]
    own_settings.update({"REPORT_MUTED": ", ".join(names)})
    own_settings.apply()
    row = sent_report(host)
    arrive(service, row, text="mute collie")
    # Nothing the person saved is dropped, and a name the report would never read is not
    # added as if it worked: the reply says why.
    assert saved_muted(own_settings) == names
    [reply] = confirmations(service)
    assert "200" in reply["text"] and "collie" in reply["text"]
    assert "Done" not in reply["text"]


@pytest.mark.parametrize("sender, text, refs, automatic, why", [
    (OTHER, "mute colliehq/collie", None, False, "a stranger who copied the thread headers"),
    (OWNER, "thanks!\nmute colliehq/collie", None, False, "mute on the second line"),
    (OWNER, "please mute colliehq/collie", None, False, "a sentence that mentions mute"),
    (OWNER, "mute colliehq/collie", [], False, "a reply to nothing"),
    (OWNER, "mute colliehq/collie", ["<not-ours@example.test>"], False, "a reply to another thread"),
    (OWNER, "mute colliehq/collie", None, True, "an automatic message"),
    (OWNER, "mute a, b; c", None, False, "a list smuggled into one name"),
    (OWNER, "mute", None, False, "no project at all"),
])
def test_everything_else_mutes_nothing(host, zones, builds, own_settings, sender, text, refs,
                                       automatic, why):
    root, service, adapter = host
    row = sent_report(host)
    arrive(service, row, sender=sender, text=text, refs=refs, automatic=automatic)
    assert not own_settings.get("REPORT_MUTED"), why
    assert confirmations(service) == [], why
    assert len(adapter.sent) == 1, why


def test_a_reply_to_the_plain_brief_is_not_a_mute_command(host, zones, own_settings):
    root, service, adapter = host
    opt_in(root, service)                                   # the brief, not the report
    tick = sched.tick(root, at(2026, 9, 10), service=service)
    row = comms.get_result("mail", tick["job"]["result_id"], include_private=True,
                           directory=service.directory)
    arrive(service, row)
    assert not own_settings.get("REPORT_MUTED") and confirmations(service) == []
    assert event(service)["state"] == "pending"


def test_a_list_held_by_the_environment_is_not_changed_and_the_reply_says_why(
        host, zones, builds, own_settings, monkeypatch):
    root, service, _adapter = host
    monkeypatch.setenv("COLLIE_REPORT_MUTED", "AgentGalaxy")
    row = sent_report(host)
    arrive(service, row)
    assert morning_report.muted_names() == ["AgentGalaxy"]
    [reply] = confirmations(service)
    assert "environment" in reply["text"] and "COLLIE_REPORT_MUTED" in reply["text"]


def test_a_chinese_report_is_answered_in_chinese(host, zones, builds, own_settings):
    root, service, _adapter = host
    report_on(root, service, language="zh")
    tick = sched.tick(root, at(2026, 9, 10), service=service)
    row = comms.get_result("mail", tick["job"]["result_id"], include_private=True,
                           directory=service.directory)
    arrive(service, row, text="mute collie")
    [reply] = confirmations(service)
    assert "collie" in reply["text"] and "晨报" in reply["text"]


def test_a_confirmation_that_cannot_be_sent_stays_visible(host, zones, builds, own_settings):
    root, service, adapter = host
    row = sent_report(host)
    adapter.fail = RuntimeError("the connection dropped mid-conversation")
    arrive(service, row)
    # The mute stands -- it is the person's instruction -- and the reply is in the outbox
    # in the state the transport left it, never quietly retried.
    assert morning_report.muted_names() == ["colliehq/collie"]
    [reply] = confirmations(service)
    assert reply["state"] == "unknown"
