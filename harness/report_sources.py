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
* **gmail**, **calendar** -- through ``harness.google_connect``, Collie's own Google
  connection.  It is imported lazily and every call is wrapped.  ``status()`` decides first:
  not set up, not connected, "reconnect Google", or a permission the person did not tick
  (``status()["can"]``) makes that one source unavailable, and a call that fails is mapped by
  the connector's own error types (``NeedsReconnect``, ``MissingScope``, ``GoogleAPIError``
  with its HTTP status...), never by its message.  Mail is recent inbox threads (the last 36
  hours, promotions, social and drafts left out).  Unread mail needs the person only when it
  is in Gmail's Primary tab or Gmail marked it IMPORTANT; unread Updates and Forums mail is
  status (``fyi``), so a morning of notifications is not a morning of chores.  Every word of it
  is ``untrusted``: the sender wrote it.  The sender's address and the thread id ride along in ``meta`` for the
  reply-draft step and go no further.  Calendar is today and the next seven days;
  invitations are written by whoever sent them, so they are ``untrusted`` too.
* **github** -- through the ``gh`` CLI when it is installed and signed in: exactly two GraphQL
  calls, each with a timeout.  The person's repositories are the ones they can push to, their
  own, their organisations' and ones they collaborate on.  Wins since the report's window
  opened: releases (with downloads so far, whoever pressed the button), new stars, and pull
  requests the person authored that were merged (searched by merge date, in any repository).
  New downloads are new only against the counts the previous report saved.  An open pull
  request, a review request or a failing default branch that has not moved in two weeks says
  nothing: there is nobody to nudge.  The person's own open pull request is theirs to act on
  where they can merge it (admin or maintain): failed checks need them, green checks with no
  reviewers requested make it ready to merge (one line per repository), anything else is
  status.  Only where somebody else merges is a review nudge fair, and only for a pull request
  at least a day old that has not moved for two days.  Review requests are other people's words and are
  ``untrusted``.  ``activity`` is when the person last did something of their own in each
  repository: merged or opened a pull request, or published a release.
* **local** -- git repositories on this computer, found at most two folders below a set of
  project roots (the ``REPORT_PROJECT_ROOTS`` setting, else the folders holding Collie's recent
  workspaces plus ~/workspace, ~/code, ~/projects and ~/src).  Linked worktrees, hidden and
  vendored folders are skipped.  A disk full of clones is not a list of projects, so checkouts
  are grouped by remote: every clone of one remote, and every machine-made copy that reaches it
  through a local-path remote, is one project, and only its most recently active checkout
  speaks.  A project is *active* only when the person worked on it lately -- a commit they
  authored in the last 30 days (matched against the repository's own git identity) or files
  edited in the last 14; a repository with no remote needs their commit in the last 14.  An
  inactive project says nothing at all.  What an active one says is status, never a to-do
  (``fyi``): changes uncommitted for more than a week, commits unpushed for more than a day, a
  branch far behind its upstream as of the last fetch.  Projects come back with ``activity``
  (when the person last worked on each) and ``aliases`` (a checkout's other remotes), so a
  local clone and its GitHub repository are one project with one name.  Every git call is
  read-only (optional locks off, so ``git status`` does not even refresh the index), has its
  own timeout, and a repository that hangs costs only itself.
"""
from __future__ import annotations

import concurrent.futures
import datetime as _dt
import email.utils
import importlib
import json
import os
import re
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
_SKIP_LABELS = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM", "TRASH", "DRAFT"})
#: Gmail's own tabs for mail machines send.  Unread there is not a thing to do unless Gmail
#: itself marked the message IMPORTANT (bills, security alerts, ...).
_AUTOMATED = {"CATEGORY_UPDATES": "Updates", "CATEGORY_FORUMS": "Forums"}
CALENDAR_DAYS = 7
CALENDAR_LIMIT = 50

#: ``google_connect.status()["state"]`` -> what the report says instead of reading that source.
#: "reconnect Google" is short on purpose: it is what the email's footer says after "Not read".
_GOOGLE_STATES = {
    "not_configured": "Google isn't set up on this computer",
    "not_connected": "Google isn't connected yet",
    "needs_reconnect": "reconnect Google",
}
#: ``status()["can"]`` capability -> what Google did not allow.
_CAN = {"gmail_read": "read Gmail", "gmail_drafts": "write Gmail drafts",
        "calendar_read": "read your calendar"}


def _google_module():
    try:
        return importlib.import_module(__package__ + ".google_connect")
    except ImportError:
        raise rs.Unavailable("Google isn't available in this version of Collie") from None


def google_reason(module, exc, label):
    """What the report says about one failed Google call, by the connector's own error types.

    Only the type (and an HTTP status) is used: the connector's messages are written for
    ``collie google`` and would repeat its advice in every line of the report.
    """
    def isa(name):
        cls = getattr(module, name, None)
        return isinstance(cls, type) and isinstance(exc, cls)

    if isa("NeedsReconnect"):
        return _GOOGLE_STATES["needs_reconnect"]
    if isa("NotConnected"):
        return _GOOGLE_STATES["not_connected"]
    if isa("NotConfigured"):
        return _GOOGLE_STATES["not_configured"]
    if isa("MissingScope"):
        scope = str(getattr(exc, "scope", "") or "")
        what = next((words for key, words in (("compose", _CAN["gmail_drafts"]),
                                              ("gmail", _CAN["gmail_read"]),
                                              ("calendar", _CAN["calendar_read"]))
                     if key in scope), "do that")
        return "Google didn't allow Collie to %s" % what
    if isa("GoogleAPIError"):
        status = getattr(exc, "status", 0)
        return "%s answered with an error%s" % (label, " (HTTP %s)" % status
                                               if isinstance(status, int) and status else "")
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "%s could not be reached" % label
    return "%s could not be read (%s)" % (label, type(exc).__name__)


def google(capability, label, state_dir=None):
    """``(module, status)`` for a Google connection that allows ``capability``, or
    :class:`report_signals.Unavailable` with the reason.  ``capability`` is a key of
    ``status()["can"]``: gmail_read, gmail_drafts or calendar_read."""
    module = _google_module()
    try:
        status = module.status(state_dir=state_dir or None)
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        raise rs.Unavailable(google_reason(module, exc, "Google")) from None
    status = status if isinstance(status, dict) else {}
    state = str(status.get("state") or "")
    if state not in ("connected", "missing_scope"):
        raise rs.Unavailable(_GOOGLE_STATES.get(state, _GOOGLE_STATES["not_connected"]))
    can = status.get("can") if isinstance(status.get("can"), dict) else {}
    if not can.get(capability):
        raise rs.Unavailable("Google didn't allow Collie to %s" % _CAN.get(capability, capability))
    return module, status


def _call(module, label, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        raise rs.Unavailable(google_reason(module, exc, label)) from None


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


def gmail_thread_link(account, thread):
    """The Gmail web address of a thread, for the account it lives in (as the connector
    builds it: ``?authuser=`` picks the account; ``/u/<address>/`` does not)."""
    base = ("https://mail.google.com/mail/?authuser=%s" % urllib.parse.quote(account, safe="@.+-_")
            if account and "@" in account else "https://mail.google.com/mail/u/0/")
    return base + "#all/" + urllib.parse.quote(thread, safe="")


def gmail(ctx):
    """Recent inbox threads.  The newest message of each thread speaks for it."""
    module, status = google("gmail_read", "Gmail", ctx.state_dir)
    account = str(status.get("account") or "").strip()
    rows = _call(module, "Gmail", module.gmail_search, GMAIL_QUERY, GMAIL_LIMIT,
                 state_dir=ctx.state_dir or None)
    rows = rows if isinstance(rows, list) else []
    threads = {}
    for row in rows[:GMAIL_LIMIT * 2]:
        if not isinstance(row, dict):
            continue
        when = _epoch(row.get("timestamp")) or _epoch(row.get("date"))
        if when is None or when < ctx.now - GMAIL_WINDOW_S or when > ctx.now + 3600:
            continue
        labels = {str(label) for label in (row.get("labels") or []) if label}
        if labels & _SKIP_LABELS:
            continue
        name, address = email.utils.parseaddr(str(row.get("from") or ""))
        if address and account and address.casefold() == account.casefold():
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
        labels = {str(label) for label in (row.get("labels") or []) if label}
        tab = next((words for label, words in _AUTOMATED.items() if label in labels), "")
        needs = bool(row.get("unread")) and (not tab or "IMPORTANT" in labels)
        signals.append(rs.make(
            "gmail", thread, kind="needs_you" if needs else "fyi",
            title=row.get("subject") or ("(没有主题)" if _zh(ctx) else "(no subject)"),
            detail=row.get("snippet"), when=when, link=gmail_thread_link(account, thread),
            project="", evidence=" · ".join(bit for bit in (
                "Gmail", tab, "from %s" % who if who else "") if bit), untrusted=True,
            meta={"thread_id": thread, "sender": address, "sender_name": name,
                  "subject": daily_brief._text(row.get("subject"), 300)}))
    return {"signals": signals, "stats": {"threads": len(signals)}}


def _day_start(ctx):
    zone = ctx.zone or _dt.timezone.utc
    local = _dt.datetime.fromtimestamp(ctx.now, zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def calendar(ctx):
    """Today and the next seven days of the person's primary calendar."""
    module, _status = google("calendar_read", "Calendar", ctx.state_dir)
    zone = ctx.zone or _dt.timezone.utc
    start = _day_start(ctx)
    end = (start + _dt.timedelta(days=CALENDAR_DAYS + 1)).replace(hour=0)
    tomorrow = (start + _dt.timedelta(days=1)).timestamp()
    rows = _call(module, "Calendar", module.calendar_events, _rfc3339(start.timestamp()),
                 _rfc3339(end.timestamp()), CALENDAR_LIMIT, state_dir=ctx.state_dir or None)
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
GH_REPOS = 30
GH_PRS = 20
#: An open pull request, a review request or a failing branch that has not moved for this long
#: is not something to nudge anyone about.
QUIET_DAYS = 14
#: The person's own GitHub activity older than this does not make a project active.
ACTIVE_DAYS = 30
_PUSH = ("ADMIN", "MAINTAIN", "WRITE")
#: Who merges their own pull requests: nobody else is holding those up.
_MERGERS = ("ADMIN", "MAINTAIN")
#: A pull request younger than this, or one that moved more recently than NUDGE_AFTER_DAYS,
#: is not a reason to nudge a reviewer.
FRESH_PR_S = 24 * 3600
NUDGE_AFTER_DAYS = 2
_AUTH_WORDS = ("gh auth login", "not logged", "authentication", "bad credentials", "401")

_REPOS_QUERY = """query {
  viewer {
    login
    repositories(first: %d, ownerAffiliations: [OWNER, ORGANIZATION_MEMBER, COLLABORATOR],
                 orderBy: {field: PUSHED_AT, direction: DESC}, isFork: false) {
      nodes { nameWithOwner url isArchived viewerPermission stargazerCount pushedAt
        defaultBranchRef { name target { ... on Commit { oid committedDate
          statusCheckRollup { state } } } }
        releases(first: 3, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { tagName url createdAt publishedAt isDraft author { login }
                  releaseAssets(first: 20) { nodes { name downloadCount } } } }
        stargazers(last: 10) { edges { starredAt } } }
    }
  }
}""" % GH_REPOS


def _prs_query(since):
    """The person's open pull requests, what of theirs was merged since ``since``, and the
    reviews they were asked for."""
    return """query {
  viewer {
    login
    openPrs: pullRequests(states: OPEN, first: %d, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes { number title url createdAt updatedAt isDraft reviewDecision
              repository { nameWithOwner viewerPermission }
              reviewRequests(first: 1) { totalCount }
              commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }
    }
  }
  merged: search(query: "is:pr author:@me is:merged merged:>=%s", type: ISSUE, first: 50) {
    issueCount
    nodes { ... on PullRequest { number title url mergedAt repository { nameWithOwner } } }
  }
  reviewRequests: search(query: "is:open is:pr review-requested:@me archived:false",
                         type: ISSUE, first: 10) {
    issueCount
    nodes { ... on PullRequest { number title url createdAt updatedAt
                                 repository { nameWithOwner } author { login } } }
  }
}""" % (GH_PRS, _rfc3339(since))


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


def _active(ctx, activity, slug, when):
    """Record the person's own activity in ``slug`` when it is recent enough to count."""
    if slug and when is not None and ctx.now - ACTIVE_DAYS * 86400 <= when <= ctx.now:
        activity[slug] = max(activity.get(slug, 0.0), when)


def _days(ctx, when):
    days = max(0, int((ctx.now - when) // 86400))
    return "%d day%s" % (days, "" if days == 1 else "s")


def _repo_signals(ctx, repos, previous, counters, login, activity):
    out = []
    for repo in repos:
        slug = daily_brief._text(repo.get("nameWithOwner"), 120)
        if not slug or repo.get("isArchived") or repo.get("viewerPermission") not in _PUSH:
            continue                     # only repositories the person can push to are theirs
        url = repo.get("url") if isinstance(repo.get("url"), str) else ""
        for release in _nodes(repo, "releases", "nodes"):
            tag = daily_brief._text(release.get("tagName"), 80)
            if not tag or release.get("isDraft"):
                continue
            downloads = sum(int(asset.get("downloadCount") or 0)
                            for asset in _nodes(release, "releaseAssets", "nodes"))
            key = "github.downloads:%s@%s" % (slug, tag)
            counters[key] = downloads
            published = _epoch(release.get("publishedAt")) or _epoch(release.get("createdAt"))
            author = str(((release.get("author") or {}).get("login")) or "")
            if login and author.casefold() == login.casefold():
                _active(ctx, activity, slug, published)
            if published is not None and ctx.since <= published <= ctx.now:
                out.append(rs.make("github", "release:%s@%s" % (slug, tag), kind="done",
                                   title="%s %s is out" % (slug, tag),
                                   detail=("%d download%s so far" % (
                                       downloads, "" if downloads == 1 else "s"))
                                   if downloads else "", when=published,
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
        committed = _epoch(target.get("committedDate"))
        if rollup in ("FAILURE", "ERROR") and committed is not None and \
                committed >= ctx.now - QUIET_DAYS * 86400:
            name = daily_brief._text(branch.get("name"), 60) or "the default branch"
            day = _dt.datetime.fromtimestamp(committed, ctx.zone or _dt.timezone.utc).strftime(
                "%Y-%m-%d")
            out.append(rs.make("github", "ci:%s" % slug, kind="needs_you",
                               title="CI is failing on %s of %s" % (name, slug),
                               detail="latest commit %s from %s" % (
                                   daily_brief._text(target.get("oid"), 40)[:7], day),
                               when=committed, link=(url + "/actions") if url else "",
                               project=slug, evidence="GitHub checks: %s" % rollup))
    return out


def _checks(pr):
    """The state of the checks on a pull request's latest commit, or ``""`` when it has none."""
    nodes = _nodes(pr, "commits", "nodes")
    rollup = (nodes[0].get("commit") or {}).get("statusCheckRollup") if nodes else None
    return str(rollup.get("state") or "") if isinstance(rollup, dict) else ""


def _own_pr_state(ctx, pr, created, updated):
    """``(kind, words)`` for one of the person's own open pull requests that moved lately.

    Where the person can merge (admin or maintain), nobody else is holding the pull request
    up: failed checks are theirs to fix, green checks with no reviewers requested make it
    ready to merge, and anything else (checks running, a reviewer they asked) is status.
    Elsewhere a review nudge is fair only for a pull request at least a day old that has not
    moved for ``NUDGE_AFTER_DAYS``; one that moved since is status.
    """
    checks = _checks(pr)
    decision = str(pr.get("reviewDecision") or "")
    can_merge = ((pr.get("repository") or {}).get("viewerPermission")) in _MERGERS
    reviewers = int(((pr.get("reviewRequests") or {}).get("totalCount")) or 0)
    if pr.get("isDraft"):
        return "fyi", "draft"
    if checks in ("FAILURE", "ERROR"):
        return "needs_you", "checks failed"
    if decision == "CHANGES_REQUESTED":
        return "needs_you", "changes requested"
    if can_merge:
        if checks in ("SUCCESS", "") and not reviewers:
            return "ready", "ready to merge"
        if checks in ("PENDING", "EXPECTED"):
            return "fyi", "checks running"
        return "fyi", "waiting for the reviewers you asked"
    if decision == "APPROVED":
        return "fyi", "approved, waiting for a maintainer to merge"
    if created > ctx.now - FRESH_PR_S or updated > ctx.now - NUDGE_AFTER_DAYS * 86400:
        return "fyi", "waiting for a review"
    return "waiting_on_others", "waiting for a review"


def _pr_signals(ctx, data, activity):
    out = []
    viewer = data.get("viewer") or {}
    merged = {}
    for pr in _nodes(data, "merged", "nodes"):
        when = _epoch(pr.get("mergedAt"))
        slug = _slug(pr)
        _active(ctx, activity, slug, when)
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
    quiet = ctx.now - QUIET_DAYS * 86400
    mergeable = {}
    for pr in _nodes(viewer, "openPrs", "nodes"):
        slug, created = _slug(pr), _epoch(pr.get("createdAt"))
        updated = _epoch(pr.get("updatedAt")) or created
        if not slug or created is None:
            continue
        _active(ctx, activity, slug, created)
        if updated is None or updated < quiet:
            continue                     # nothing has moved in two weeks: nobody to nudge
        kind, state = _own_pr_state(ctx, pr, created, updated)
        if kind == "ready":
            mergeable.setdefault(slug, []).append(pr)
            continue
        out.append(rs.make("github", "pr:%s#%s" % (slug, pr.get("number")), kind=kind,
                           title=pr.get("title"),
                           detail="#%s in %s · %s · open %s, last activity %s ago" % (
                               pr.get("number"), slug, state, _days(ctx, created),
                               _days(ctx, updated)),
                           when=created, link=pr.get("url"), project=slug,
                           evidence="GitHub pull request, review decision: %s" % (
                               str(pr.get("reviewDecision") or "none").lower())))
    for slug, prs in mergeable.items():          # one line per repository
        prs.sort(key=lambda pr: pr.get("number") or 0)
        numbers = ", ".join("#%s" % pr.get("number") for pr in prs)
        one = prs[0] if len(prs) == 1 else None
        out.append(rs.make("github", "ready:%s" % slug, kind="ready",
                           title=one.get("title") if one else
                           "%d pull requests ready to merge in %s" % (len(prs), slug),
                           detail="%s in %s · checks passed, no reviewers requested · "
                                  "ready to merge" % (numbers, slug),
                           when=max(_epoch(pr.get("updatedAt")) or 0.0 for pr in prs) or None,
                           link=one.get("url") if one else
                           "https://github.com/%s/pulls?q=is%%3Apr+is%%3Aopen+author%%3A%%40me"
                           % slug,
                           project=slug, evidence="GitHub pull requests you can merge"))
    for pr in _nodes(data, "reviewRequests", "nodes"):
        slug, created = _slug(pr), _epoch(pr.get("createdAt"))
        updated = _epoch(pr.get("updatedAt")) or created
        if not slug or updated is None or updated < quiet:
            continue
        author = daily_brief._text((pr.get("author") or {}).get("login"), 40)
        out.append(rs.make("github", "review:%s#%s" % (slug, pr.get("number")), kind="needs_you",
                           title=pr.get("title"),
                           detail="#%s in %s%s asks for your review · open %s" % (
                               pr.get("number"), slug, " from @%s" % author if author else "",
                               _days(ctx, created or updated)),
                           when=created, link=pr.get("url"), project=slug,
                           evidence="GitHub review request", untrusted=True))
    return out, viewer


def github(ctx):
    """The person's GitHub, in two GraphQL calls through ``gh``."""
    run = _gh_runner(ctx)
    repos_data, repos_problem = _graphql(run, _REPOS_QUERY)
    prs_data, prs_problem = _graphql(run, _prs_query(ctx.since))
    problems = [problem for problem in (repos_problem, prs_problem) if problem]
    if repos_data is None and prs_data is None:
        if "auth" in problems:
            raise rs.Unavailable("gh isn't signed in to GitHub")
        if "slow" in problems:
            raise rs.Unavailable("GitHub took too long to answer")
        raise rs.Unavailable("GitHub could not be read (%s)" % problems[0])
    counters, signals, activity = {}, [], {}
    login = str((((repos_data or {}).get("viewer") or {}).get("login"))
                or (((prs_data or {}).get("viewer") or {}).get("login")) or "")
    repos = _nodes(repos_data or {}, "viewer", "repositories", "nodes")
    signals += _repo_signals(ctx, repos, ctx.previous or {}, counters, login, activity)
    viewer = {}
    if prs_data is not None:
        more, viewer = _pr_signals(ctx, prs_data, activity)
        signals += more
    missing = [name for name, problem in (("repositories", repos_problem),
                                          ("pull requests", prs_problem)) if problem]
    pushable = [repo for repo in repos if repo.get("viewerPermission") in _PUSH
                and not repo.get("isArchived")]
    return {"signals": signals, "counters": counters, "activity": activity,
            "state": "partial" if missing else "ok",
            "reason": ("%s could not be read" % " and ".join(missing)) if missing else "",
            "stats": {"repos": len(pushable),
                      "open_prs": int(((viewer.get("openPrs") or {}).get("totalCount")) or 0)},
            "detail": "2 GitHub calls"}


# ---------------------------------------------------------------- projects on this computer

DEFAULT_HOME_ROOTS = ("workspace", "code", "projects", "src")
SCAN_DEPTH = 2
MAX_REPOS = 80
REPO_TIMEOUT_S = 8
SCAN_BUDGET_S = 45
SCAN_WORKERS = 6
STALE_CHANGES_DAYS = 7
UNPUSHED_AFTER_S = 24 * 3600
BEHIND_THRESHOLD = 20
ACTIVE_COMMIT_DAYS = 30
ACTIVE_FILES_DAYS = 14
LOCAL_ONLY_COMMIT_DAYS = 14
_REMOTE_HOPS = 3
_SKIP_DIRS = frozenset({"node_modules", "venv", "env", "vendor", "third_party", "site-packages",
                        "dist", "build", "target", "__pycache__", "Pods", "bower_components"})
_WALK_UP = 4


# The scan runs git inside repositories the person did not create -- a downloaded archive, a
# clone from anyone, a take-home exercise dropped under a project root.  git config in such a
# repository can name a program to run during ordinary read commands: core.fsmonitor runs during
# status, a filter driver runs while status hashes a changed tracked file, gpg.program runs when
# log shows a signature, a hook runs when the index is written.  So every git call the scan makes
# is neutralized.  ``-c`` on the command line overrides the repository's own config, and reading
# config never runs anything, so the filter drivers (which have arbitrary names) are found with
# ``git config --get-regexp`` and blanked per call.
_GIT_HARDEN = ("core.fsmonitor=false", "core.untrackedCache=false", "core.hooksPath=" +
               (os.devnull.replace("\\", "/") or "/dev/null"), "core.pager=cat", "core.editor=true",
               "diff.external=", "diff.textconv=", "protocol.allow=never", "log.showSignature=false",
               "gpg.program=", "safe.directory=")
_FILTER_RE = re.compile(r"^(filter\.[^\n]*\.(?:clean|smudge|process))\b")
#: A config value safe to echo back on the command line: a token, never a path or a program.
_SAFE_CONFIG_VAL = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
#: git env we drop, so the process's own GIT_DIR / GIT_CONFIG / an fsmonitor override cannot
#: redirect the scan or slip a program back in.  The user's HOME stays, for their git identity.
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_GLOBAL",
                 "GIT_CONFIG_SYSTEM", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_FSMONITOR", "GIT_PROXY_COMMAND", "GIT_SSH", "GIT_SSH_COMMAND",
                 "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EDITOR", "GIT_ATTR_SYSTEM",
                 "GIT_ALLOW_PROTOCOL", "GIT_HOOKS_PATH")


def _git_env():
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_COUNT="0", GIT_FLUSH="1", LC_ALL="C")
    return env


def _user_text_config(path):
    """The user's own ``core.autocrlf`` / ``core.eol``, echoed back as ``-c``.

    ``GIT_CONFIG_NOSYSTEM=1`` (below) disables the system config, where Git for Windows sets
    ``core.autocrlf=true``; without it every CRLF-normalized file reads as modified and the scan
    would report changes that are not there.  These values are data (a token, matched against
    ``_SAFE_CONFIG_VAL``), never a program, so re-passing them is safe.  Read outside any
    repository so a scanned repo cannot change what is carried.
    """
    out, env = [], {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C")            # deliberately no NOSYSTEM here
    for key in ("core.autocrlf", "core.eol"):
        try:
            proc = subprocess.run([path, "config", "--get", key], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=REPO_TIMEOUT_S,
                                  env=env, stdin=subprocess.DEVNULL,
                                  cwd=os.path.expanduser("~"), **plat.no_window_kwargs())
        except (OSError, subprocess.SubprocessError):
            continue
        val = (proc.stdout or "").strip()
        if proc.returncode == 0 and _SAFE_CONFIG_VAL.match(val):
            out.append("%s=%s" % (key, val))
    return out


def _repo_filters(path, repo):
    """The ``filter.<name>.{clean,smudge,process}`` keys a repository's config defines, blanked.

    Only ``git config`` is run, which reads and never executes.  Its own ``-c`` neutralizers are
    passed so this first call is safe too.
    """
    try:
        proc = subprocess.run([path, "--no-optional-locks"]
                              + [arg for key in _GIT_HARDEN for arg in ("-c", key)]
                              + ["-C", repo, "config", "--get-regexp",
                                 r"^filter\..*\.(clean|smudge|process)$"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=REPO_TIMEOUT_S, env=_git_env(), stdin=subprocess.DEVNULL,
                              **plat.no_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return []
    keys = []
    for line in (proc.stdout or "").splitlines():
        match = _FILTER_RE.match(line)
        if match:
            keys.append(match.group(1) + "=")
    return keys


def _git_runner(ctx):
    """``run(args, timeout) -> (returncode, stdout, stderr)`` for git, read-only and hardened.

    ``args`` starts with ``-C <repo>``; the repository is read from it so this repository's own
    filter drivers can be blanked for the call.  A test may inject its own runner through
    ``ctx.options["git"]``.
    """
    injected = ctx.options.get("git")
    if callable(injected):
        return injected
    path = shutil.which("git")
    if not path:
        raise rs.Unavailable("git isn't installed")
    env = _git_env()
    carry = [arg for key in _user_text_config(path) for arg in ("-c", key)]

    def run(args, timeout):
        args = list(args)
        repo = args[args.index("-C") + 1] if "-C" in args else None
        extra = [arg for key in _GIT_HARDEN for arg in ("-c", key)] + carry
        if repo:
            extra += [arg for key in _repo_filters(path, repo) for arg in ("-c", key)]
        proc = subprocess.run([path, "--no-optional-locks", "--no-pager"] + extra + args,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                              **plat.no_window_kwargs())
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    return run


def _repo_of(path):
    """The main repository a folder belongs to (a worktree answers for its main one), or ``""``."""
    here = os.path.abspath(path)
    for _ in range(_WALK_UP):
        marker = os.path.join(here, ".git")
        if os.path.isdir(marker):
            return here
        if os.path.isfile(marker):
            try:
                with open(marker, encoding="utf-8", errors="replace") as handle:
                    line = handle.read(4096).strip()
            except OSError:
                return here
            target = line[len("gitdir:"):].strip() if line.startswith("gitdir:") else ""
            target = os.path.normpath(os.path.join(here, target)) if target else ""
            parts = target.replace("\\", "/").split("/.git/worktrees/")
            return os.path.normpath(parts[0]) if len(parts) == 2 else here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return ""


def _recent_workspaces(ctx):
    """Folders Collie worked in lately: recent sessions and the code map's last repository."""
    if "workspaces" in ctx.options:
        return list(ctx.options.get("workspaces") or [])
    found = []
    try:
        from . import sessions
        found += [row.get("cwd") for row in sessions.recent(80) if row.get("cwd")]
    except Exception:                                 # noqa: BLE001 - a hint, never a blocker
        pass
    try:
        with open(os.path.join(ctx.state_dir or os.path.expanduser("~/.collie"),
                               "map-last-repo.txt"), encoding="utf-8") as handle:
            found.append(handle.read(1024).strip())
    except OSError:
        pass
    return found


def project_roots(ctx):
    """The folders the local scan starts from, existing ones only, each named once."""
    if ctx.options.get("roots"):
        wanted = list(ctx.options["roots"])
    else:
        wanted = []
        for workspace in _recent_workspaces(ctx):
            if not workspace or not os.path.isdir(str(workspace)):
                continue
            repo = _repo_of(str(workspace))
            wanted.append(os.path.dirname(repo) if repo else os.path.dirname(
                os.path.abspath(str(workspace))))
        home = ctx.options.get("home") or os.path.expanduser("~")
        wanted += [os.path.join(home, name) for name in DEFAULT_HOME_ROOTS]
    roots, seen = [], set()
    for path in wanted:
        path = os.path.abspath(os.path.expanduser(str(path)))
        key = os.path.normcase(os.path.realpath(path))
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _find_repos(roots):
    """``(repos, more)``: git repositories at most ``SCAN_DEPTH`` folders below each root."""
    repos, seen, more = [], set(), 0
    for root in roots:
        queue = [(root, 0)]
        while queue:
            path, depth = queue.pop(0)
            marker = os.path.join(path, ".git")
            if os.path.isdir(marker):
                key = os.path.normcase(os.path.realpath(path))
                if key not in seen:
                    seen.add(key)
                    if len(repos) < MAX_REPOS:
                        repos.append(path)
                    else:
                        more += 1
                continue                     # a repository's own folders are its business
            if os.path.exists(marker) or depth >= SCAN_DEPTH:
                continue                     # a linked worktree or submodule speaks through its main repo
            try:
                entries = sorted(os.scandir(path), key=lambda entry: entry.name.casefold())
            except OSError:
                continue
            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir and not entry.name.startswith(".") and entry.name not in _SKIP_DIRS:
                    queue.append((entry.path, depth + 1))
    return repos, more


def _status(text):
    """What ``git status --porcelain=v2 --branch -z`` says: branch facts and changed paths."""
    info = {"head": "", "upstream": "", "ahead": 0, "behind": 0, "paths": [], "changes": 0}
    records = text.split("\0")
    skip = False
    for record in records:
        if skip:
            skip = False
            continue
        if not record:
            continue
        if record.startswith("# branch.head "):
            info["head"] = record[len("# branch.head "):]
        elif record.startswith("# branch.upstream "):
            info["upstream"] = record[len("# branch.upstream "):]
        elif record.startswith("# branch.ab "):
            match = re.match(r"# branch\.ab \+(\d+) -(\d+)", record)
            if match:
                info["ahead"], info["behind"] = int(match.group(1)), int(match.group(2))
        elif record[:2] in ("1 ", "2 ", "u ", "? "):
            info["changes"] += 1
            fields = {"1": 8, "2": 9, "u": 10, "?": 1}[record[0]]
            parts = record.split(" ", fields)
            if len(parts) > fields and len(info["paths"]) < 200:
                info["paths"].append(parts[fields])
            skip = record[0] == "2"          # a rename's original path follows as its own record
    return info


def _read_config(path):
    """``{remote name: url}`` from one git config file, in file order."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(256 * 1024)
    except OSError:
        return {}
    urls, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = re.match(r'^\[remote\s+"([^"]+)"\]$', line)
            current = section.group(1) if section else None
            continue
        if current and re.match(r"^url\s*=", line):
            urls.setdefault(current, line.split("=", 1)[1].strip())
    return urls


def _ordered(urls):
    return (["origin"] if "origin" in urls else []) + [name for name in urls if name != "origin"]


def _config_path(path):
    """The config file of the repository at ``path`` (a checkout or a bare one), or ``""``."""
    if os.path.isdir(os.path.join(path, ".git")):
        return os.path.join(path, ".git", "config")
    if os.path.isfile(os.path.join(path, "HEAD")) and os.path.isfile(os.path.join(path, "config")):
        return os.path.join(path, "config")
    return ""


def _remote_project(url, base, hops=0, seen=None):
    """``(key, label, local_only, hops)`` for the project a remote URL belongs to.

    A GitHub URL is its ``owner/repo``; any other network URL is its ``host/path``.  A local
    path -- a clone of a clone, a bare repository on disk -- is followed to that repository's
    own remote, up to ``_REMOTE_HOPS`` times, so a machine-made copy is filed under the project
    it was copied from.  A chain that ends in a repository with no remote names that repository
    and is ``local_only``.  ``hops`` counts the local copies on the way.
    """
    url = str(url or "").strip()
    slug = rs.github_slug(url)
    if slug:
        return "github.com/" + slug.casefold(), slug, False, hops
    lowered = url.lower()
    network = None
    if not lowered.startswith("file://"):
        network = (re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$",
                            url, re.IGNORECASE)
                   or re.match(r"^[^@/\s]+@([^:/\s]+):(.+?)(?:\.git)?/?$", url))
    if network:
        return ("%s/%s" % (network.group(1), network.group(2))).casefold(), network.group(2), \
            False, hops
    path = url[len("file://"):] if lowered.startswith("file://") else url
    if re.match(r"^/[A-Za-z]:[/\\]", path):
        path = path[1:]                              # file:///C:/x on Windows
    if not path:
        return "", "", True, hops
    path = os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))
    real = os.path.normcase(os.path.realpath(path))
    seen = set() if seen is None else seen
    config = _config_path(path)
    if config and hops < _REMOTE_HOPS and real not in seen:
        seen.add(real)
        urls = _read_config(config)
        for name in _ordered(urls):
            found = _remote_project(urls[name], path, hops + 1, seen)
            if found[0]:
                return found
    name = os.path.basename(path.rstrip("\\/"))
    name = name[:-4] if name.endswith(".git") else name
    return "local:" + real, name or path, True, hops + 1


def _ere(text):
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text)


def _identity(run, repo):
    """``(names, emails)`` the repository's effective git config says the person commits as."""
    code, out, _ = run(["-C", repo, "config", "--get-regexp", r"^user\.(name|email)$"],
                       REPO_TIMEOUT_S)
    found = {}
    for line in (out or "").splitlines() if code == 0 else ():
        key, _, value = line.partition(" ")
        if value.strip():
            found[key.strip().lower()] = value.strip()      # the last scope wins, as in git
    return ([found["user.name"]] if found.get("user.name") else [],
            [found["user.email"]] if found.get("user.email") else [])


def _mine(run, repo, identity, since):
    """When the person last authored a commit here (any branch), since ``since``, or ``None``."""
    names, emails = identity
    parts = []
    if names:
        parts.append("(^(%s) <)" % "|".join(_ere(name) for name in names))
    if emails:
        parts.append("(<(%s)>$)" % "|".join(_ere(email) for email in emails))
    if not parts:
        return None
    code, out, _ = run(["-C", repo, "log", "--all", "-n", "50", "--format=%at", "-i", "-E",
                        "--author=" + "|".join(parts), "--since=@%d" % int(since)],
                       REPO_TIMEOUT_S)
    stamps = [int(line) for line in (out or "").split() if code == 0 and line.isdigit()]
    stamps = [stamp for stamp in stamps if stamp >= since]
    return float(max(stamps)) if stamps else None


def _span(seconds, zh):
    days = max(1, int(seconds // 86400))
    if days < 14:
        return ("%d 天" if zh else "%d day%s") % ((days,) if zh else (days, "" if days == 1 else "s"))
    weeks = days // 7
    return ("%d 周" if zh else "%d weeks") % weeks


class _NotOwned(RuntimeError):
    """git refused a repository the current user does not own (dubious ownership).  Skipped,
    never opened, and never counted as a read failure."""


def _owned(path):
    """Best-effort: does the current user own this repository?  Unknown answers ``True`` and
    let git's own dubious-ownership check (which we do not defeat) be the backstop."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return True                      # Windows: git compares owner SIDs itself
    try:
        return os.stat(path).st_uid == getuid()
    except OSError:
        return True


def _inspect(run, repo, now, zh):
    """What one checkout is: its project, the person's activity in it, and its status facts.

    Raises on a git call that hangs or fails, or :class:`_NotOwned` for a repository the person
    does not own.  Facts are made here but only the most recently active checkout of an active
    project gets to say them (see :func:`local_projects`).
    """
    code, out, err = run(["-C", repo, "status", "--porcelain=v2", "--branch", "-z",
                          "--untracked-files=normal", "--ignore-submodules=all"], REPO_TIMEOUT_S)
    if code != 0:
        if "dubious ownership" in (err or "").lower():
            raise _NotOwned(repo)
        raise RuntimeError("git status failed")
    info = _status(out)
    urls = _read_config(os.path.join(repo, ".git", "config"))
    remotes = [found for found in (_remote_project(urls[name], repo) for name in _ordered(urls))
               if found[0]]
    folder = os.path.basename(os.path.normpath(repo))
    if remotes:
        key, label, local_only, hops = remotes[0]
    else:
        key, label, local_only, hops = ("local:" + os.path.normcase(os.path.realpath(repo)),
                                        folder, True, 0)
    touched = None
    for path in info["paths"]:
        try:
            stamp = os.stat(os.path.join(repo, path)).st_mtime
        except OSError:
            continue
        touched = stamp if touched is None else max(touched, stamp)
    commit = _mine(run, repo, _identity(run, repo), now - ACTIVE_COMMIT_DAYS * 86400)
    branch = info["head"] if info["head"] and info["head"] != "(detached)" else ""
    facts = []
    if info["changes"] and touched is not None and touched < now - STALE_CHANGES_DAYS * 86400:
        count = info["changes"]
        facts.append(rs.make(
            "local", "changes:%s" % os.path.normcase(os.path.realpath(repo)), kind="fyi",
            title=("%s 分支上有 %d 处改动还没提交" % (branch or "当前", count)) if zh else
            "%d uncommitted change%s on %s" % (count, "" if count == 1 else "s",
                                                branch or "a detached HEAD"),
            detail=("已经 %s 没动了 · %s" if zh else "untouched for %s · %s") % (
                _span(now - touched, zh), repo),
            when=touched, project=label, evidence="git status in %s" % folder))
    ahead = info["ahead"] if info["upstream"] else 0
    if branch and not info["upstream"] and urls:
        code, count, _ = run(["-C", repo, "rev-list", "--count", "HEAD", "--not", "--remotes"],
                             REPO_TIMEOUT_S)
        ahead = int(count.strip()) if code == 0 and count.strip().isdigit() else 0
    if branch and ahead:
        code, stamp, _ = run(["-C", repo, "log", "-1", "--format=%ct"], REPO_TIMEOUT_S)
        last = int(stamp.strip()) if code == 0 and stamp.strip().isdigit() else None
        if last is not None and last < now - UNPUSHED_AFTER_S:
            facts.append(rs.make(
                "local", "unpushed:%s" % os.path.normcase(os.path.realpath(repo)), kind="fyi",
                title=("%s 上有 %d 个提交还没推送" % (branch, ahead)) if zh else
                "%d commit%s not pushed yet on %s" % (ahead, "" if ahead == 1 else "s", branch),
                detail=("最近一个是 %s 前 · %s" if zh else "the latest is %s old · %s") % (
                    _span(now - last, zh), repo),
                when=last, project=label, evidence="git in %s" % folder))
    if branch and info["upstream"] and info["behind"] >= BEHIND_THRESHOLD:
        facts.append(rs.make(
            "local", "behind:%s" % os.path.normcase(os.path.realpath(repo)), kind="fyi",
            title=("%s 落后 %s %d 个提交" % (branch, info["upstream"], info["behind"])) if zh else
            "%s is %d commits behind %s" % (branch, info["behind"], info["upstream"]),
            detail=("以上次 fetch 为准 · %s" if zh else "as of the last fetch · %s") % repo,
            when=now, project=label, evidence="git status in %s" % folder))
    return {"repo": repo, "keys": [found[0] for found in remotes] or [key],
            "labels": [found[1] for found in remotes if not found[2]], "label": label,
            "network": not local_only, "hops": hops,
            "github": key.startswith("github.com/"), "commit": commit, "touched": touched,
            "facts": facts}


def _projects(records, now):
    """``[(label, activity, chosen record, aliases)]`` for the active projects only.

    Checkouts that share any remote are one project (a machine-made copy reaches its project
    through its local-path remote).  A project with a network remote is active when the person
    authored a commit in it in the last ``ACTIVE_COMMIT_DAYS`` or edited its files in the last
    ``ACTIVE_FILES_DAYS``; a local-only one only when they authored a commit in the last
    ``LOCAL_ONLY_COMMIT_DAYS``.  The most recently active checkout speaks for the project: the
    person's own latest commit first, then the fewest local copies away from the real remote,
    then the freshest edits.
    """
    parent = {}

    def find(key):
        while parent.setdefault(key, key) != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for record in records:
        for key in record["keys"][1:]:
            parent[find(key)] = find(record["keys"][0])
    groups = {}
    for record in records:
        groups.setdefault(find(record["keys"][0]), []).append(record)
    out = []
    for members in groups.values():
        network = any(record["network"] for record in members)
        for record in members:
            commit, touched = record["commit"], record["touched"]
            if network:
                edited = touched if touched is not None and \
                    touched >= now - ACTIVE_FILES_DAYS * 86400 else None
                record["activity"] = max([when for when in (commit, edited) if when] or [0.0])
            else:
                record["activity"] = commit if commit and \
                    commit >= now - LOCAL_ONLY_COMMIT_DAYS * 86400 else 0.0
        active = [record for record in members if record["activity"]]
        if not active:
            continue                     # nobody worked on it lately: it says nothing at all
        chosen = max(active, key=lambda r: (r["commit"] or 0.0, -r["hops"], r["touched"] or 0.0,
                                            r["repo"]))
        aliases = sorted({label for record in members for label in record["labels"]})
        out.append((chosen["label"], max(r["activity"] for r in active), chosen, aliases))
    return sorted(out, key=lambda row: -row[1])


def local_projects(ctx):
    """The person's active projects on this computer, and what is worth saying about each."""
    run = _git_runner(ctx)
    roots = project_roots(ctx)
    if not roots:
        raise rs.Unavailable("none of the project folders exist on this computer")
    found, more = _find_repos(roots)
    # A repository owned by another user is somebody else's; do not open it (git config there
    # could name a program to run), and do not count it as a read failure either.
    repos = [repo for repo in found if _owned(repo)]
    not_owned = len(found) - len(repos)
    zh = _zh(ctx)
    records, failed, unfinished = [], 0, 0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS,
                                                 thread_name_prefix="collie-report-git")
    futures = {pool.submit(_inspect, run, repo, ctx.now, zh): repo for repo in repos}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=SCAN_BUDGET_S):
            try:
                records.append(future.result())
            except _NotOwned:
                not_owned += 1
            except Exception:                         # noqa: BLE001 - counted, not raised
                failed += 1
    except concurrent.futures.TimeoutError:
        unfinished = len([future for future in futures if not future.done()])
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    signals, activity, aliases = [], {}, []
    for label, when, chosen, names in _projects(records, ctx.now):
        activity[label] = when
        if len(names) > 1:
            aliases.append(names)
        link = "https://github.com/%s" % label if chosen["github"] else ""
        for fact in chosen["facts"]:
            fact.update(project=label, link=link)
            signals.append(fact)
    notes = []
    if more:
        notes.append("only the first %d repositories were checked (%d more were found)"
                     % (MAX_REPOS, more))
    if failed:
        notes.append("%d repositor%s could not be read" % (failed, "y" if failed == 1 else "ies"))
    if unfinished:
        notes.append("%d repositor%s did not answer in time" % (
            unfinished, "y" if unfinished == 1 else "ies"))
    signals.sort(key=lambda signal: (-activity.get(signal["project"], 0.0), signal["id"]))
    return {"signals": signals, "activity": activity, "aliases": aliases,
            "state": "partial" if notes else "ok", "reason": "; ".join(notes),
            "stats": {"repos": len(repos), "projects": len(activity), "roots": len(roots),
                      "not_owned": not_owned},
            "detail": "%d repositories, %d active project%s, in %d folder%s" % (
                len(repos), len(activity), "" if len(activity) == 1 else "s", len(roots),
                "" if len(roots) == 1 else "s")}


# ---------------------------------------------------------------- the registry


#: name -> (label, reader, time limit in seconds).  Order is the order sources are listed.
ADAPTERS = {
    "collie": ("Collie", collie, 30.0),
    "gmail": ("Gmail", gmail, 30.0),
    "calendar": ("Google Calendar", calendar, 20.0),
    "github": ("GitHub", github, 60.0),
    "local": ("Projects on this computer", local_projects, 60.0),
    "news": ("News", news, 10.0),
}


def adapter(name):
    label, read, timeout = ADAPTERS[name]
    return rs.Adapter(name=name, label=label, read=read, timeout=timeout)


def default_adapters():
    return [adapter(name) for name in ADAPTERS]
