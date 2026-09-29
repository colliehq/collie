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
* **gmail**, **calendar** -- through ``harness.google_connect`` when it exists and is
  connected.  It is imported lazily and every call is wrapped: a missing module, a connection
  that was never made or needs making again, a scope that was not granted, or a failed call
  each make that one source unavailable with a reason.  Mail is recent inbox threads (the last
  36 hours, promotions and social left out), and every word of it is ``untrusted``: the sender
  wrote it.  The sender's address and the thread id ride along in ``meta`` for the reply draft
  step and go no further.  Calendar is today and the next seven days; invitations are written
  by whoever sent them, so they are ``untrusted`` too.
* **github** -- through the ``gh`` CLI when it is installed and signed in: exactly two GraphQL
  calls, each with a timeout.  Wins are releases, new stars, merged pull requests and new
  downloads from the last 24 hours (downloads are new only against the counts the previous
  report saved).  The person's open pull requests carry their age and review state; failing CI
  on the default branch of a repository they administer is a thing for them when the failing
  commit is recent and a tidy-up when it is not; review requests are other people's words and
  are ``untrusted``.
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import importlib
import json
import os
import shutil
import subprocess
import urllib.parse

from . import daily_brief, plat
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


# ---------------------------------------------------------------- Google: Gmail and Calendar

GMAIL_WINDOW_S = 36 * 3600
GMAIL_LIMIT = 25
GMAIL_QUERY = "in:inbox newer_than:2d -category:promotions -category:social -in:chats"
_SKIP_LABELS = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM", "TRASH"})
CALENDAR_DAYS = 7
CALENDAR_LIMIT = 50

_GOOGLE_STATES = {
    "not_connected": "Google isn't connected yet",
    "needs_reconnect": "Google needs you to reconnect it",
    "not_configured": "Google isn't set up on this computer",
}


def _google(scope_word, label):
    """``(module, status)`` for a connected Google account that granted ``scope_word``."""
    try:
        module = importlib.import_module(__package__ + ".google_connect")
    except ImportError:
        raise rs.Unavailable("Google isn't available in this version of Collie") from None
    try:
        status = module.status()
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        raise rs.Unavailable("Google's connection could not be checked (%s)"
                             % type(exc).__name__) from None
    status = status if isinstance(status, dict) else {}
    state = str(status.get("state") or "")
    if state != "connected":
        raise rs.Unavailable(_GOOGLE_STATES.get(state, "Google isn't connected yet"))
    scopes = status.get("scopes")
    if isinstance(scopes, (list, tuple)) and scopes and \
            not any(scope_word in str(scope) for scope in scopes):
        raise rs.Unavailable("Google is connected without %s access" % label)
    return module, status


def _call(label, fn, *args):
    try:
        return fn(*args)
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        raise rs.Unavailable("%s could not be read (%s)" % (label, type(exc).__name__)) from None


def _epoch(value, zone=None):
    """Seconds since the epoch from the shapes mail and calendar APIs use, or ``None``.

    Numbers are seconds or milliseconds; strings are ISO 8601 (``Z`` or an offset), a bare
    date (an all-day event: local midnight in ``zone``), or an RFC 2822 mail date.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return daily_brief._stamp(number / 1000.0 if number > 1e11 else number)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            day = _dt.date.fromisoformat(text)
            return _dt.datetime(day.year, day.month, day.day,
                                tzinfo=zone or _dt.timezone.utc).timestamp()
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone or _dt.timezone.utc)
        return daily_brief._stamp(parsed.timestamp())
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return daily_brief._stamp(parsed.timestamp())


def _rfc3339(when):
    return _dt.datetime.fromtimestamp(float(when), _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def gmail(ctx):
    """Recent inbox threads.  The last message of each thread speaks for it."""
    module, status = _google("gmail", "Gmail")
    account = str(status.get("account") or "").strip().casefold()
    rows = _call("Gmail", module.gmail_search, GMAIL_QUERY, GMAIL_LIMIT)
    rows = rows if isinstance(rows, list) else []
    threads = {}
    for row in rows[:GMAIL_LIMIT * 2]:
        if not isinstance(row, dict):
            continue
        when = _epoch(row.get("date"))
        if when is None or when < ctx.now - GMAIL_WINDOW_S or when > ctx.now + 3600:
            continue
        labels = {str(label) for label in (row.get("labels") or []) if label}
        if labels & _SKIP_LABELS:
            continue
        name, address = email.utils.parseaddr(str(row.get("from") or ""))
        if address and account and address.casefold() == account:
            continue                           # the person's own message, not one for them
        thread = daily_brief._text(row.get("thread_id") or row.get("id"), 120)
        if not thread:
            continue
        if thread in threads and threads[thread][0] >= when:
            continue
        threads[thread] = (when, row, name, address)
    signals = []
    for thread, (when, row, name, address) in sorted(threads.items(),
                                                     key=lambda kv: -kv[1][0]):
        who = daily_brief._text(name, 60) or (address.split("@")[-1] if "@" in address else "")
        link = "https://mail.google.com/mail/%s#all/%s" % (
            ("?authuser=" + urllib.parse.quote(account, safe="")) if account else "",
            urllib.parse.quote(thread, safe=""))
        signals.append(rs.make(
            "gmail", thread, kind="needs_you" if row.get("unread") else "fyi",
            title=row.get("subject") or ("(没有主题)" if _zh(ctx) else "(no subject)"),
            detail=row.get("snippet"), when=when, link=link, project="",
            evidence="Gmail · from %s" % who if who else "Gmail", untrusted=True,
            meta={"thread_id": thread, "sender": address, "sender_name": name,
                  "subject": daily_brief._text(row.get("subject"), 300)}))
    return {"signals": signals, "stats": {"threads": len(signals)}}


def _day_start(ctx):
    zone = ctx.zone or _dt.timezone.utc
    local = _dt.datetime.fromtimestamp(ctx.now, zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def calendar(ctx):
    """Today and the next seven days of the person's primary calendar."""
    module, _status = _google("calendar", "Calendar")
    zone = ctx.zone or _dt.timezone.utc
    start = _day_start(ctx)
    end = (start + _dt.timedelta(days=CALENDAR_DAYS + 1)).replace(hour=0)
    tomorrow = (start + _dt.timedelta(days=1)).timestamp()
    rows = _call("Calendar", module.calendar_events, _rfc3339(start.timestamp()),
                 _rfc3339(end.timestamp()), CALENDAR_LIMIT)
    rows = rows if isinstance(rows, list) else []
    signals, today = [], 0
    for row in rows[:CALENDAR_LIMIT]:
        if not isinstance(row, dict):
            continue
        all_day = bool(row.get("all_day"))
        began = _epoch(row.get("start"), zone)
        ended = _epoch(row.get("end"), zone) or began
        if began is None or ended is None or ended < ctx.now:
            continue
        bits = []
        if not all_day:
            bits.append(_dt.datetime.fromtimestamp(began, zone).strftime("%a %H:%M"))
        else:
            bits.append(_dt.datetime.fromtimestamp(began, zone).strftime("%a") +
                        (" 全天" if _zh(ctx) else " all day"))
        people = row.get("attendees_count")
        if isinstance(people, int) and not isinstance(people, bool) and people > 1:
            bits.append(("%d 人" if _zh(ctx) else "with %d people") % people)
        if daily_brief._text(row.get("location"), 80):
            bits.append(daily_brief._text(row.get("location"), 80))
        today += began < tomorrow
        signals.append(rs.make("calendar", daily_brief._text(row.get("id"), 200) or
                               "%s|%s" % (began, row.get("summary")),
                               kind="event", title=row.get("summary") or
                               ("(忙碌)" if _zh(ctx) else "(busy)"),
                               detail=" · ".join(bits), when=began, link=row.get("html_link"),
                               project="", evidence="Google Calendar", untrusted=True))
    return {"signals": signals, "stats": {"events_today": today, "events_week": len(signals)}}


# ---------------------------------------------------------------- GitHub (the gh CLI)

GH_TIMEOUT_S = 25
GH_REPOS = 20
GH_PRS = 20
CI_FRESH_DAYS = 14
PR_STALE_DAYS = 60
_AUTH_WORDS = ("gh auth login", "not logged", "authentication", "bad credentials", "401")

_REPOS_QUERY = """query {
  viewer {
    login
    repositories(first: %d, ownerAffiliations: [OWNER, ORGANIZATION_MEMBER],
                 orderBy: {field: PUSHED_AT, direction: DESC}, isFork: false) {
      nodes { nameWithOwner url isArchived viewerPermission stargazerCount pushedAt
        defaultBranchRef { name target { ... on Commit { oid committedDate
          statusCheckRollup { state } } } }
        releases(first: 2, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { tagName url createdAt isDraft
                  releaseAssets(first: 20) { nodes { name downloadCount } } } }
        stargazers(last: 10) { edges { starredAt } } }
    }
  }
}""" % GH_REPOS

_PRS_QUERY = """query {
  viewer {
    login
    openPrs: pullRequests(states: OPEN, first: %d, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes { number title url createdAt isDraft reviewDecision repository { nameWithOwner } }
    }
    mergedPrs: pullRequests(states: MERGED, first: 30, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number title url mergedAt repository { nameWithOwner } }
    }
  }
  reviewRequests: search(query: "is:open is:pr review-requested:@me archived:false",
                         type: ISSUE, first: 10) {
    issueCount
    nodes { ... on PullRequest { number title url createdAt
                                 repository { nameWithOwner } author { login } } }
  }
}""" % GH_PRS


def _gh_runner(ctx):
    """``run(args, timeout) -> (returncode, stdout, stderr)``: the injected one, or real ``gh``."""
    injected = ctx.options.get("gh")
    if callable(injected):
        return injected
    path = shutil.which("gh")
    if not path:
        raise rs.Unavailable("the GitHub CLI (gh) isn't installed")
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1",
               GH_SPINNER_DISABLED="1")

    def run(args, timeout):
        proc = subprocess.run([path] + list(args), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL, **plat.no_window_kwargs())
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    return run


def _graphql(run, query):
    """``(data, problem)``.  ``problem`` is ``"auth"``, ``"slow"`` or a short reason."""
    try:
        code, out, err = run(["api", "graphql", "-f", "query=" + query], GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, "slow"
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        return None, type(exc).__name__
    if code != 0:
        text = str(err or "").casefold()
        if any(word in text for word in _AUTH_WORDS):
            return None, "auth"
        return None, "gh exited with %d" % code
    try:
        value = json.loads(out or "null")
    except ValueError:
        return None, "gh answered with something that is not JSON"
    data = value.get("data") if isinstance(value, dict) else None
    return (data, "") if isinstance(data, dict) else (None, "GitHub returned no data")


def _nodes(value, *path):
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _slug(row):
    return daily_brief._text(((row or {}).get("repository") or {}).get("nameWithOwner"), 120)


def _repo_signals(ctx, repos, previous, counters):
    out = []
    for repo in repos:
        slug = daily_brief._text(repo.get("nameWithOwner"), 120)
        if not slug or repo.get("isArchived") or repo.get("viewerPermission") != "ADMIN":
            continue
        url = repo.get("url") if isinstance(repo.get("url"), str) else ""
        for release in _nodes(repo, "releases", "nodes"):
            tag = daily_brief._text(release.get("tagName"), 80)
            if not tag or release.get("isDraft"):
                continue
            downloads = sum(int(asset.get("downloadCount") or 0)
                            for asset in _nodes(release, "releaseAssets", "nodes"))
            key = "github.downloads:%s@%s" % (slug, tag)
            counters[key] = downloads
            created = _epoch(release.get("createdAt"))
            if created is not None and ctx.since <= created <= ctx.now:
                out.append(rs.make("github", "release:%s@%s" % (slug, tag), kind="done",
                                   title="%s %s is out" % (slug, tag),
                                   detail=("%d download%s so far" % (
                                       downloads, "" if downloads == 1 else "s"))
                                   if downloads else "", when=created,
                                   link=release.get("url"), project=slug,
                                   evidence="GitHub release %s" % tag))
                continue
            before = previous.get(key)
            if isinstance(before, (int, float)) and not isinstance(before, bool) and \
                    downloads > before:
                gained = int(downloads - before)
                out.append(rs.make("github", "downloads:%s@%s" % (slug, tag), kind="done",
                                   title="%d new download%s of %s %s" % (
                                       gained, "" if gained == 1 else "s", slug, tag),
                                   detail="%d in total" % downloads, when=ctx.now,
                                   link=release.get("url"), project=slug,
                                   evidence="GitHub release downloads, against the last report"))
        stars = repo.get("stargazerCount")
        if isinstance(stars, int) and not isinstance(stars, bool):
            counters["github.stars:%s" % slug] = stars
        fresh = [when for when in (_epoch(edge.get("starredAt"))
                                   for edge in _nodes(repo, "stargazers", "edges"))
                 if when is not None and ctx.since <= when <= ctx.now]
        if fresh:
            out.append(rs.make("github", "stars:%s" % slug, kind="done",
                               title="%d new star%s on %s" % (len(fresh),
                                                              "" if len(fresh) == 1 else "s", slug),
                               detail="%d in total" % stars if isinstance(stars, int) else "",
                               when=max(fresh), link=url, project=slug,
                               evidence="GitHub stargazers"))
        branch = repo.get("defaultBranchRef") or {}
        target = branch.get("target") or {}
        rollup = str(((target.get("statusCheckRollup") or {}).get("state")) or "")
        if rollup in ("FAILURE", "ERROR"):
            committed = _epoch(target.get("committedDate"))
            name = daily_brief._text(branch.get("name"), 60) or "the default branch"
            fresh_ci = committed is not None and committed >= ctx.now - CI_FRESH_DAYS * 86400
            day = _dt.datetime.fromtimestamp(committed, ctx.zone or _dt.timezone.utc).strftime(
                "%Y-%m-%d") if committed else ""
            out.append(rs.make("github", "ci:%s" % slug,
                               kind="needs_you" if fresh_ci else "stale",
                               title="CI is failing on %s of %s" % (name, slug),
                               detail="latest commit %s%s" % (
                                   daily_brief._text(target.get("oid"), 40)[:7],
                                   " from %s" % day if day else ""),
                               when=committed, link=(url + "/actions") if url else "",
                               project=slug, evidence="GitHub checks: %s" % rollup))
    return out


def _pr_signals(ctx, data):
    out = []
    viewer = data.get("viewer") or {}
    merged = {}
    for pr in _nodes(viewer, "mergedPrs", "nodes"):
        when = _epoch(pr.get("mergedAt"))
        slug = _slug(pr)
        if slug and when is not None and ctx.since <= when <= ctx.now:
            merged.setdefault(slug, []).append((when, pr))
    for slug, rows in merged.items():
        rows.sort(key=lambda row: row[1].get("number") or 0)
        numbers = ", ".join("#%s" % row[1].get("number") for row in rows[:10])
        one = rows[0][1] if len(rows) == 1 else None
        out.append(rs.make("github", "merged:%s:%s" % (slug, numbers), kind="done",
                           title="%d pull request%s merged into %s" % (
                               len(rows), "" if len(rows) == 1 else "s", slug),
                           detail=numbers + (" · %s" % daily_brief._text(one.get("title"))
                                             if one else ""),
                           when=max(row[0] for row in rows),
                           link=one.get("url") if one else
                           "https://github.com/%s/pulls?q=is%%3Apr+is%%3Amerged" % slug,
                           project=slug, evidence="GitHub merged pull requests"))
    for pr in _nodes(viewer, "openPrs", "nodes"):
        slug, created = _slug(pr), _epoch(pr.get("createdAt"))
        if not slug or created is None:
            continue
        age = max(0, int((ctx.now - created) // 86400))
        decision = str(pr.get("reviewDecision") or "")
        if pr.get("isDraft"):
            kind, state = "fyi", "draft"
        elif decision == "APPROVED":
            kind, state = "needs_you", "approved, ready to merge"
        elif decision == "CHANGES_REQUESTED":
            kind, state = "needs_you", "changes requested"
        else:
            kind = "stale" if age >= PR_STALE_DAYS else "waiting_on_others"
            state = "waiting for a review"
        out.append(rs.make("github", "pr:%s#%s" % (slug, pr.get("number")), kind=kind,
                           title=pr.get("title"),
                           detail="#%s in %s · %s · open %d day%s" % (
                               pr.get("number"), slug, state, age, "" if age == 1 else "s"),
                           when=created, link=pr.get("url"), project=slug,
                           evidence="GitHub pull request, review decision: %s" % (
                               decision.lower() or "none")))
    for pr in _nodes(data, "reviewRequests", "nodes"):
        slug, created = _slug(pr), _epoch(pr.get("createdAt"))
        if not slug:
            continue
        age = max(0, int((ctx.now - created) // 86400)) if created else 0
        author = daily_brief._text((pr.get("author") or {}).get("login"), 40)
        out.append(rs.make("github", "review:%s#%s" % (slug, pr.get("number")), kind="needs_you",
                           title=pr.get("title"),
                           detail="#%s in %s%s asks for your review · open %d day%s" % (
                               pr.get("number"), slug, " from @%s" % author if author else "",
                               age, "" if age == 1 else "s"),
                           when=created, link=pr.get("url"), project=slug,
                           evidence="GitHub review request", untrusted=True))
    return out, viewer


def github(ctx):
    """The person's GitHub, in two GraphQL calls through ``gh``."""
    run = _gh_runner(ctx)
    repos_data, repos_problem = _graphql(run, _REPOS_QUERY)
    prs_data, prs_problem = _graphql(run, _PRS_QUERY)
    problems = [problem for problem in (repos_problem, prs_problem) if problem]
    if repos_data is None and prs_data is None:
        if "auth" in problems:
            raise rs.Unavailable("gh isn't signed in to GitHub")
        if "slow" in problems:
            raise rs.Unavailable("GitHub took too long to answer")
        raise rs.Unavailable("GitHub could not be read (%s)" % problems[0])
    counters, signals = {}, []
    repos = _nodes(repos_data or {}, "viewer", "repositories", "nodes")
    signals += _repo_signals(ctx, repos, ctx.previous or {}, counters)
    viewer = {}
    if prs_data is not None:
        more, viewer = _pr_signals(ctx, prs_data)
        signals += more
    missing = [name for name, problem in (("repositories", repos_problem),
                                          ("pull requests", prs_problem)) if problem]
    admin = [repo for repo in repos if repo.get("viewerPermission") == "ADMIN"
             and not repo.get("isArchived")]
    return {"signals": signals, "counters": counters,
            "state": "partial" if missing else "ok",
            "reason": ("%s could not be read" % " and ".join(missing)) if missing else "",
            "stats": {"repos": len(admin),
                      "open_prs": int(((viewer.get("openPrs") or {}).get("totalCount")) or 0)},
            "detail": "2 GitHub calls"}


# ---------------------------------------------------------------- the registry


#: name -> (label, reader, time limit in seconds).  Order is the order sources are listed.
ADAPTERS = {
    "collie": ("Collie", collie, 30.0),
    "gmail": ("Gmail", gmail, 30.0),
    "calendar": ("Google Calendar", calendar, 20.0),
    "github": ("GitHub", github, 60.0),
    "news": ("News", news, 10.0),
}


def adapter(name):
    label, read, timeout = ADAPTERS[name]
    return rs.Adapter(name=name, label=label, read=read, timeout=timeout)


def default_adapters():
    return [adapter(name) for name in ADAPTERS]
