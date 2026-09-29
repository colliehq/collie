"""A saved morning report for the desktop tests: five things to do, two of them reply drafts.

The shape is what ``morning_report.build`` writes (schema, date, sections with grounded items,
the public signals they cite).  Nothing here reads a source or asks a model.
"""
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

from harness import daily_brief
from harness import morning_report as mr
from harness import report_signals as rs

ZONE = "America/Los_Angeles"
DAY = "2026-09-29"
PR_LINK = "https://github.com/colliehq/collie/pull/31"
AZURE_LINK = "https://portal.azure.com/#view/Microsoft_Azure_Billing"
DRAFT_ANA = "https://mail.google.com/mail/?authuser=owner@example.com#all?compose=CllgCJfA"
DRAFT_BEN = "https://mail.google.com/mail/?authuser=owner@example.com#all?compose=CllgCJfB"
APPROVAL = "perm-7f3a"


def at(hour, minute=0, day=DAY):
    """Epoch seconds for ``hour:minute`` on ``day`` in the fixture's timezone."""
    year, month, date = (int(part) for part in day.split("-"))
    return dt.datetime(year, month, date, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


def approval_signal(native=APPROVAL):
    """The signal the Collie source makes for a pending approval (report_sources._brief_signal)."""
    item_id = daily_brief._item_id("approval", "approvals", native)
    return rs.make("collie", item_id, kind="needs_you", title="Allow Collie to run the tests",
                   detail="Collie is paused until you answer.", project="",
                   evidence="%s · pending" % daily_brief._source_name("approvals", "en"))


def _signals():
    return {
        "pr": rs.make("github", "review:colliehq/collie#31", kind="needs_you",
                      title="Speed up the report build", project="colliehq/collie",
                      detail="#31 in colliehq/collie from @ana asks for your review · open 3 days",
                      link=PR_LINK, evidence="GitHub review request", untrusted=True),
        "approval": approval_signal(),
        "azure": rs.make("gmail", "thread-azure", kind="needs_you", title="Azure invoice is due",
                         project="", link=AZURE_LINK, evidence="Gmail · from Azure",
                         untrusted=True),
        "ana": rs.make("gmail", "18f1a2b3c4d5e6f0", kind="needs_you", title="Thursday's call",
                       project="", link="https://mail.google.com/mail/u/0/#all/18f1a2b3c4d5e6f0",
                       evidence="Gmail · from Ana", untrusted=True),
        "ben": rs.make("gmail", "18f1a2b3c4d5e6f1", kind="needs_you", title="Invoice question",
                       project="", link="https://mail.google.com/mail/u/0/#all/18f1a2b3c4d5e6f1",
                       evidence="Gmail · from Ben", untrusted=True),
        "win": rs.make("github", "release:colliehq/collie@v0.31.0", kind="done",
                       title="colliehq/collie v0.31.0 is out", project="colliehq/collie",
                       link="https://github.com/colliehq/collie/releases/tag/v0.31.0",
                       evidence="GitHub release v0.31.0"),
    }


def _item(signal, title, detail="", **extra):
    item = {"title": title, "detail": detail, "signal_ids": [signal["id"]],
            "link": signal["link"], "sources": [signal["source"]], "when": signal.get("when")}
    item.update(extra)
    return item


def _draft(draft_id, url, thread):
    return {"thread_id": thread, "to": "ana@example.com", "subject": "Re: Thursday's call",
            "body": "Thursday works for me.", "state": "created", "draft_id": draft_id,
            "open_url": url}


def report(day=DAY, *, generated=None, weather=None, language="en"):
    s = _signals()
    yours = [_item(s["pr"], "Review Ana's pull request", "It has waited three days."),
             _item(s["approval"], "Answer Collie's question", "One step is waiting on you."),
             _item(s["azure"], "Pay the Azure invoice", "It is due this week.")]
    ready = [_item(s["ana"], "Reply to Ana about Thursday", "",
                   draft=_draft("r111", DRAFT_ANA, "18f1a2b3c4d5e6f0")),
             _item(s["ben"], "Reply to Ben about the invoice", "",
                   draft=_draft("r222", DRAFT_BEN, "18f1a2b3c4d5e6f1"))]
    wins = [_item(s["win"], "Collie 0.31.0 shipped overnight")]
    return {
        "schema": mr.SCHEMA, "date": day,
        "generated_at": generated if generated is not None else at(6, 38, day),
        "since": at(6, 38, day) - 86400, "language": language, "timezone": ZONE,
        "utc_offset_minutes": -420, "profile": {"name": "Daming", "companion": "Rowan"},
        "weather": weather or {"state": "ok", "temp_c": 11.0, "code": 0, "is_day": 1,
                               "stale": False},
        "greeting": "Good morning, Daming!", "headline": "Five quick ones and you're clear.",
        "summary": "Collie 0.31.0 shipped overnight, and two replies are in your Gmail drafts.",
        "things_today": 5, "things_total": 5,
        "sections": {"wins": {"items": wins, "more": 0}, "yours": {"items": yours, "more": 0},
                     "ready": {"items": ready, "more": 0}, "projects": {"items": [], "more": 0},
                     "reads": {"items": [], "more": 0}},
        "signals": [rs.public(signal) for signal in s.values()],
        "counters": {},
        "provenance": {"read_at": at(6, 38, day), "sources": [
            {"name": "github", "label": "GitHub", "state": "ok"},
            {"name": "gmail", "label": "Gmail", "state": "ok"}],
            "composer": {"mode": "model", "dropped": []}, "drafts": {"requested": 2}},
    }


def empty_report(day=DAY):
    """A morning with nothing to do: the headline says so and no item is actionable."""
    rep = report(day)
    rep["sections"]["yours"] = {"items": [], "more": 0}
    rep["sections"]["ready"] = {"items": [], "more": 0}
    rep.update(things_today=0, things_total=0, headline="Nothing needs you this morning.")
    return rep


def save(root, rep):
    """Write the report the way ``morning_report.build`` does."""
    return mr.write_snapshot(rep, str(root))


def rewrite(root, rep):
    """Replace the saved snapshot bypassing ``write_snapshot`` (a hand-edited or tampered file)."""
    folder = mr.report_dir(str(root))
    os.makedirs(folder, exist_ok=True)
    for name in ("%s.json" % rep["date"], "latest.json"):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            json.dump(rep, handle)
