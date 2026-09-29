"""The morning report's sources.  Each adapter turns what it can see into signals.

Every function named in :data:`ADAPTERS` takes a :class:`report_signals.Context` and returns
``{"signals": [...], "state": "ok"|"partial", "reason": str, "counters": {...},
"stats": {...}, "detail": str}``, or raises :class:`report_signals.Unavailable` with a reason a
person can read.  The report never imports anything from here except :func:`default_adapters`,
so a new source is a new function and one more row in ``ADAPTERS`` -- nothing else changes.

What each source may say, and what it must not:

* **collie** -- Collie's own stores, read through the Daily Brief's collector and builder, so
  every rule the brief already enforces (carried waits, unfinished runs, quiet mail, the
  person's own hide/snooze choices) holds here too.  Failed missions older than a week are
  not "needs you" any more: they collapse into one tidy-up signal.  Wins come from the last
  24 hours, not from "today", because a report read at seven would otherwise only ever
  celebrate what finished after midnight.
* **news** -- the headlines the feeds' background refresh already stored.  Never a fetch.
  Headlines are other people's words, so they are ``untrusted``.
"""
from __future__ import annotations

import os

from . import daily_brief
from . import report_signals as rs

WINDOW_DAYS_TIDY = 7
NEWS_LIMIT = 12

#: Daily Brief item kind -> signal kind for rows the brief says need the person.
_DONE_MISSION = ("done", "completed", "succeeded")


def _zh(ctx):
    return str(ctx.language or "").lower().startswith("zh")


# ---------------------------------------------------------------- collie


def _brief_signal(item, kind):
    """One Daily Brief row as a signal.  The row's words are kept exactly as stored."""
    source = daily_brief._source_name(item.get("source") or "", "en")
    status = str(item.get("status") or "").replace("_", " ")
    return rs.make("collie", item["id"], kind=kind, title=item.get("title"),
                   detail=item.get("detail"), when=item.get("when"), project="",
                   evidence=" · ".join(bit for bit in (source, status) if bit),
                   untrusted=item.get("source") == "communications")


def _tidy_signal(ctx, rows):
    """One signal for every failed mission that has sat for more than a week."""
    ages = sorted(int((ctx.now - row["when"]) // 86400) for row in rows)
    count, low, high = len(rows), ages[0], ages[-1]
    if _zh(ctx):
        title = "%d 个旧任务可以清理了" % count
        detail = ("它在 %d 天前停下，在“任务”里清掉就好。" % low if count == 1 else
                  "它们在 %d 到 %d 天前停下，在“任务”里清一遍就好。" % (low, high))
    else:
        title = "%d older mission%s can be tidied away" % (count, "" if count == 1 else "s")
        detail = ("It stopped %d days ago; clearing it takes a moment in Missions." % low
                  if count == 1 else
                  "They stopped %d to %d days ago; one pass in Missions clears them." % (low, high))
    return rs.make("collie", "tidy:failed-missions", kind="stale", title=title, detail=detail,
                   when=max(row["when"] for row in rows), project="",
                   evidence="missions: %d failed, oldest %d days" % (count, high))


def _recent_wins(ctx, payloads):
    """Work that finished inside the report's window, straight from the stored rows.

    The brief's own "done today" starts at local midnight, and some finished states
    (``done_verified``, ``done_accepted``) are not in its list at all, so a mission that
    shipped at eleven last night never reached a morning read.  Ids are the brief's own, so a
    row the brief also listed is one signal, not two.
    """
    out = []
    for row in daily_brief._rows(payloads.get("missions"), "missions"):
        state = daily_brief._text(row.get("state"), 32)
        when = daily_brief._stamp(row.get("updated_at"))
        if when is None or not (ctx.since <= when <= ctx.now):
            continue
        if state in _DONE_MISSION or state.startswith("done"):
            native = daily_brief._text(row.get("mission_id"), 80)
            item_id = daily_brief._item_id("mission", "missions", native)
            out.append(rs.make("collie", item_id, kind="done", title=row.get("title"),
                               when=when, project="", evidence="Missions · %s" % state))
    for row in daily_brief._rows(payloads.get("todos"), "todos", daily_brief.TODO_LIMIT):
        when = daily_brief._stamp(row.get("done_at"))
        if row.get("done") is not True or when is None or not (ctx.since <= when <= ctx.now):
            continue
        item_id = daily_brief._item_id("todo", "todos", daily_brief._text(row.get("id"), 80))
        out.append(rs.make("collie", item_id, kind="done", title=row.get("title"), when=when,
                           project="", evidence="To-dos · done"))
    for row in daily_brief._rows(payloads.get("runs"), "runs"):
        when = daily_brief._stamp(row.get("ended"))
        state = daily_brief._text(row.get("state"), 32)
        stop = daily_brief._text(row.get("stop_reason"), 32)
        if when is None or not (ctx.since <= when <= ctx.now):
            continue
        if state in ("failed", "canceled", "cancelled") or stop in (
                daily_brief._UNFINISHED_STOP_REASONS | {"canceled", "cancelled"}):
            continue
        item_id = daily_brief._item_id("run", "runs", daily_brief._text(row.get("session"), 80))
        out.append(rs.make("collie", item_id, kind="done", title=row.get("ask"),
                           detail="" if row.get("ask") else "A task finished", when=when,
                           project="", evidence="Runs · %s" % (state or "done")))
    return out


def collie(ctx):
    """Collie's missions, decisions, runs, routines, sessions, messages and to-dos."""
    from . import daily_brief_web
    payloads, report = daily_brief_web.collect(ctx.state_dir, now=ctx.now)
    brief = daily_brief.build(payloads, now=ctx.now, timezone=ctx.zone or "UTC",
                              language=ctx.language, state_dir=ctx.state_dir or None,
                              remember=False)
    found, old_failed = {}, []
    tidy_before = ctx.now - WINDOW_DAYS_TIDY * 86400

    def add(signal):
        found.setdefault(signal["id"], signal)

    for item in brief["attention"]:
        if item["kind"] == "mission" and item.get("status") == "failed" and \
                item.get("when") is not None and item["when"] < tidy_before:
            old_failed.append(item)
            continue
        add(_brief_signal(item, "ready" if item.get("status") == "ready_to_send" else "needs_you"))
    if old_failed:
        add(_tidy_signal(ctx, old_failed))
    for item in brief["agenda"]:
        add(_brief_signal(item, "event"))
    for item in brief["completed"]:
        add(_brief_signal(item, "done"))
    for signal in _recent_wins(ctx, payloads):
        add(signal)
    for item in brief["progress"]:
        add(_brief_signal(item, "fyi"))
    unread = sorted(row["name"] for row in report if row.get("state") != "ok")
    missions = daily_brief._rows(payloads.get("missions"), "missions")
    return {"signals": list(found.values()), "state": "partial" if unread else "ok",
            "reason": ("not readable from here: %s" % ", ".join(unread)) if unread else "",
            "stats": {"missions": len(missions)}}


# ---------------------------------------------------------------- news


def news(ctx):
    """Headlines the news feeds' background refresh already stored.  Never fetches."""
    from . import daily_brief_news
    if not os.path.isfile(daily_brief_news.path_for(ctx.state_dir)):
        raise rs.Unavailable("no news feeds are set up")
    store = daily_brief_news.NewsStore(ctx.state_dir)
    rows = store.headlines()
    if rows is None:
        raise rs.Unavailable("no news feeds are set up")
    feeds = store.panel().get("feeds") or []
    failing = [feed for feed in feeds if feed.get("error")]
    signals = []
    for row in rows:
        link = row.get("url") if isinstance(row.get("url"), str) else ""
        if not rs._https(link) or not daily_brief._text(row.get("title")):
            continue                  # unsourced or unlinkable: not something to recommend
        signals.append(rs.make("news", link, kind="fyi", title=row.get("title"),
                               detail=row.get("summary"), when=row.get("published_at"),
                               link=link, evidence=row.get("source"), project="",
                               untrusted=True))
        if len(signals) >= NEWS_LIMIT:
            break
    return {"signals": signals, "state": "partial" if failing else "ok",
            "reason": ("%d of %d feeds could not be refreshed" % (len(failing), len(feeds))
                       if failing else ""),
            "stats": {"feeds": len(feeds)}}


# ---------------------------------------------------------------- the registry


#: name -> (label, reader, time limit in seconds).  Order is the order sources are listed.
ADAPTERS = {
    "collie": ("Collie", collie, 30.0),
    "news": ("News", news, 10.0),
}


def adapter(name):
    label, read, timeout = ADAPTERS[name]
    return rs.Adapter(name=name, label=label, read=read, timeout=timeout)


def default_adapters():
    return [adapter(name) for name in ADAPTERS]
