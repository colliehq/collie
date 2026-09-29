"""The morning report on the desktop wallpaper: whether it shows today, and what it offers.

``collie report build`` saves the morning report (:mod:`morning_report`).  On that report's
morning the ambient wallpaper (``webui/ambient.html``) shows it over a sky drawn from the day's
weather: the headline, the summary, one dot for each thing to do, and up to three actions.
This module is what the page asks (``GET /api/report/today``) and tells
(``POST /api/report/today``).

**When it shows.**  Today's report exists (its date is today in the report's own timezone), it
is before noon there, neither switch is off -- Settings -> Desktop -> *Show the morning report on
the desktop* (``DESKTOP_MORNING_REPORT``) and the widget panel's *Morning report*
(``widgets.morning`` in desktop.json) -- and the person has not hidden it for the day.  Any
other answer says why not (``why``) and when asking again could change it (``recheck_s``), and
carries none of the report's words.

**The things to do** are the report's "yours" and "ready" items: what its dots count.  Each has
a key made from the signals it cites, so an item keeps what happened to it when the report is
built again the same morning.  What happened lives beside the snapshot, in
``<state>/morning-report/<date>.state.json``; the report itself is never rewritten.

**The actions.**  At most ``PILLS``, the first one primary: the reply drafts together ("Review 2
drafts", which opens the first draft), then each other item in the report's order.  A pill
links to an https address the report vetted (vetted again here: a snapshot is a file anyone on
the account can edit), or to the report page (``/report``) when the item has none.  A morning
with nothing to do offers the report page.

**Progress.**  The person marks a thing done (the check beside its button) or undoes that.
While the scene shows, the page also asks for the resolve pass, which runs on a background
thread at most every ``RESOLVE_EVERY_S`` and re-checks three cheap facts: a reply draft that is
no longer a draft in Gmail (sent or deleted), a pull request that was merged, and an approval
that is no longer waiting in this Collie (answered, or its run ended).  A fact that cannot be
checked -- Google not connected, ``gh`` missing, the approvals not in this process, any error --
changes nothing: not knowing is never "done".  What the person said about an item always wins
over a later check.  The sentence cheers as things get done ("One down, four to go.") and says
"All clear for today." when all are; the page shows that moment and then hides the scene for
the day.

**The report page** is the morning email (:mod:`morning_report_email`) with the dog inlined and
every link opening a new window, which the wallpaper host sends to the default browser.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import threading
import time

from . import daily_brief
from . import morning_report as mr
from . import morning_report_email as mre
from . import report_signals as rs

STATE_SCHEMA = "collie.morning_report.state/1"
#: The scene shows until this hour, local time on the report's day.
NOON = 12
PILLS = 3
PILL_LABEL = 32
ACTIONABLE = ("yours", "ready")
RECHECK_SHOWING_S = 60
RECHECK_WAITING_S = 5 * 60
RECHECK_OFF_S = 15 * 60
#: The resolve pass runs at most this often, and only while the scene shows.
RESOLVE_EVERY_S = 15 * 60
MAX_KEYS = 50
REPORT_PAGE = "/report"
GMAIL_DRAFTS = "https://mail.google.com/mail/u/0/#drafts"
#: Who said an item is done: the person, or which fact the resolve pass found.
DONE_BY = ("person", "draft_gone", "pr_merged", "approval_answered")
_KEY_RE = re.compile(r"k[0-9a-f]{16}")
_PR_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]{1,9})")
_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_OFF = ("off", "0", "false", "no")

#: Seams for tests: the wall clock, and how a background pass is started.
_now = time.time
_LOCK = threading.Lock()


def _spawn(fn):
    threading.Thread(target=fn, name="collie-morning-desktop", daemon=True).start()


_WORDS = {
    "en": {"draft_one": "Review the draft", "drafts": "Review %d drafts",
           "open_report": "Open report", "dots": "%d of %d done", "by": "caught up at %s",
           "progress": "%s down, %s to go.", "last": "One to go. Almost there.",
           "clear": "All clear for today.",
           "clear_more": "Nice work. I'll keep an eye on things for the rest of the day."},
    "zh": {"draft_one": "看看这封草稿", "drafts": "看看 %d 封草稿", "open_report": "看完整晨报",
           "dots": "完成 %d / %d", "by": "%s 整理好",
           "progress": "完成 %d 件，还剩 %d 件。", "last": "只剩最后一件了。",
           "clear": "今天的事都搞定了。", "clear_more": "干得漂亮，今天剩下的我来盯着。"},
}
_COUNT = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def _words(report):
    return _WORDS["zh" if str(report.get("language") or "").startswith("zh") else "en"]


# ---------------------------------------------------------------- switches and the day


def switched_off():
    """``"setting_off"``, ``"widget_off"`` or ``""``: which switch keeps the scene away."""
    from . import desktop, settings
    value = str(settings.get("DESKTOP_MORNING_REPORT", "on") or "on").strip().lower()
    if value in _OFF:
        return "setting_off"
    if not desktop.morning_enabled():
        return "widget_off"
    return ""


def _local(report, now):
    """``now`` in the report's timezone (this machine's when the report names none that loads)."""
    zone = mr._zone((report or {}).get("timezone"))[0]
    return _dt.datetime.fromtimestamp(now, zone)


def _until_tomorrow(local):
    """Seconds from ``local`` to just after the next local midnight."""
    midnight = (local + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int(midnight.timestamp() - local.timestamp()) + 60)


def _waiting(local):
    """Before noon a report may still arrive; after it, nothing changes until tomorrow."""
    return RECHECK_WAITING_S if local.hour < NOON else _until_tomorrow(local)


# ---------------------------------------------------------------- the day's state


def _state_path(root, date):
    return os.path.join(mr.report_dir(root), "%s.state.json" % date)


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def load_state(root, date):
    """The day's state, cleaned: whatever is damaged or unknown in the file is left out."""
    try:
        with open(_state_path(root, date), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        value = None
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA or \
            value.get("date") != date:
        value = {}
    items = {}
    for key, row in (value.get("items") if isinstance(value.get("items"), dict) else {}).items():
        if _KEY_RE.fullmatch(str(key)) and isinstance(row, dict) and \
                row.get("state") in ("done", "open") and row.get("by") in DONE_BY:
            items[key] = {"state": row["state"], "by": row["by"], "at": _number(row.get("at"))}
    why = value.get("dismissed_why")
    return {"schema": STATE_SCHEMA, "date": date, "items": items,
            "dismissed_at": _number(value.get("dismissed_at")),
            "dismissed_why": why if why in ("person", "all_clear") else "",
            "resolved_at": _number(value.get("resolved_at"))}


def _save_state(root, state):
    os.makedirs(mr.report_dir(root), exist_ok=True)
    mr._write(_state_path(root, state["date"]), state)


# ---------------------------------------------------------------- the things to do


def item_key(item):
    """A stable key for one thing to do: the signals it cites, which a rebuild keeps."""
    ids = sorted(str(i) for i in (item.get("signal_ids") or []) if isinstance(i, str))
    basis = "\x1f".join(ids) if ids else "title\x1f%s" % daily_brief._text(item.get("title"))
    digest = hashlib.sha256(("collie.morning.item/1\x1f" + basis).encode("utf-8", "replace"))
    return "k" + digest.hexdigest()[:16]


def _actionable(report):
    """``[(section, item, key)]``: the report's things to do, in its order, each key unique."""
    sections = report.get("sections") if isinstance(report.get("sections"), dict) else {}
    out, seen = [], set()
    for name in ACTIONABLE:
        part = sections.get(name) if isinstance(sections.get(name), dict) else {}
        for item in part.get("items") or []:
            if not isinstance(item, dict):
                continue
            key, extra = item_key(item), 1
            while key in seen:                    # two items citing the same signals
                extra += 1
                key = item_key(dict(item, signal_ids=list(item.get("signal_ids") or [])
                                    + ["#%d" % extra]))
            seen.add(key)
            out.append((name, item, key))
    return out


def _short(text):
    text = daily_brief._text(text, 200)
    return text if len(text) <= PILL_LABEL else text[:PILL_LABEL - 1].rstrip() + "…"


def _view(name, item, key, row):
    draft = item.get("draft") if isinstance(item.get("draft"), dict) else None
    is_draft = name == "ready" and bool(draft) and draft.get("state") == "created"
    done = bool(row) and row.get("state") == "done"
    return {"key": key, "section": name, "title": daily_brief._text(item.get("title"), 160),
            "link": rs._https(item.get("link")), "draft": is_draft,
            "draft_url": rs._https(draft.get("open_url")) if is_draft else "",
            "state": "done" if done else "open", "done_by": row.get("by", "") if done else ""}


def _pills(views, words, total):
    """At most ``PILLS`` actions for what is still open, the first one primary."""
    if not total:
        return [{"label": words["open_report"], "href": REPORT_PAGE, "kind": "report",
                 "primary": True, "keys": []}]
    open_ = [v for v in views if v["state"] != "done"]
    pills = []
    drafts = [v for v in open_ if v["draft"]]
    if drafts:
        # The first draft opens; with no usable address for it, the account's drafts folder.
        base = next((v["draft_url"].split("#", 1)[0] for v in drafts if v["draft_url"]), "")
        pills.append({"label": words["draft_one"] if len(drafts) == 1 else
                      words["drafts"] % len(drafts),
                      "href": drafts[0]["draft_url"] or (base + "#drafts" if base else
                                                         GMAIL_DRAFTS),
                      "kind": "draft", "keys": [v["key"] for v in drafts]})
    for v in open_:
        if len(pills) >= PILLS:
            break
        if v["draft"]:
            continue
        pills.append({"label": _short(v["title"]), "href": v["link"] or REPORT_PAGE,
                      "kind": "link" if v["link"] else "report", "keys": [v["key"]]})
    for index, pill in enumerate(pills):
        pill["primary"] = index == 0
    return pills


# ---------------------------------------------------------------- what the page asks


def _count(number):
    return _COUNT[number] if 0 <= number < len(_COUNT) else str(number)


def _progress(report, views, words):
    """``(sentence, summary)`` for where the morning stands: the report's own headline until
    something is done, then a cheer that counts, then all clear."""
    total, done = len(views), len([v for v in views if v["state"] == "done"])
    summary = daily_brief._text(report.get("summary"), 400)
    if total and done == total:
        return words["clear"], words["clear_more"]
    if done:
        left = total - done
        if left == 1:
            return words["last"], summary
        if words is _WORDS["zh"]:
            return words["progress"] % (done, left), summary
        return words["progress"] % (_count(done).capitalize(), _count(left)), summary
    return (daily_brief._text(report.get("headline"), 200) or
            daily_brief._text(report.get("greeting"), 200), summary)


def _payload(report, state, local, noon):
    words = _words(report)
    rows = state.get("items") or {}
    views = [_view(name, item, key, rows.get(key)) for name, item, key in _actionable(report)]
    total = len(views)
    done = len([v for v in views if v["state"] == "done"])
    sentence, summary = _progress(report, views, words)
    generated = _number(report.get("generated_at"))
    profile = report.get("profile") if isinstance(report.get("profile"), dict) else {}
    return {
        "ok": True, "show": True, "why": "", "date": report["date"], "until": noon,
        "recheck_s": RECHECK_SHOWING_S,
        "language": "zh" if words is _WORDS["zh"] else "en",
        "sentence": sentence, "summary": summary,
        "done": done, "total": total, "all_done": bool(total) and done == total,
        "dots_label": words["dots"] % (done, total) if total else "",
        "items": [{key: v[key] for key in ("key", "section", "title", "link", "draft", "state",
                                           "done_by")} for v in views],
        "pills": _pills(views, words, total),
        "by": {"companion": daily_brief._text(profile.get("companion"), 60) or "Collie",
               "words": words["by"] % mre._clock(report, generated) if generated else ""},
    }


def _hidden(why, recheck, date=""):
    return {"ok": True, "show": False, "why": why, "date": date, "recheck_s": int(recheck)}


def today(root, *, now=None, resolve=False, approvals=None):
    """What the wallpaper's morning scene shows now, or why it shows nothing."""
    now = _now() if now is None else float(now)
    report = mr.load_latest(root)
    if not report:
        return _hidden("no_report", _waiting(_dt.datetime.fromtimestamp(now).astimezone()))
    local = _local(report, now)
    date = local.strftime("%Y-%m-%d")
    if report.get("date") != date:
        return _hidden("not_today", _waiting(local), date)
    if local.hour >= NOON:
        return _hidden("after_noon", _until_tomorrow(local), date)
    why = switched_off()
    if why:
        return _hidden(why, RECHECK_OFF_S, date)
    start = False
    with _LOCK:
        state = load_state(root, date)
        if state["dismissed_at"] is not None:
            return _hidden("dismissed", _until_tomorrow(local), date)
        last = state["resolved_at"]
        if resolve and (last is None or now - last >= RESOLVE_EVERY_S or now < last):
            # Noted before the pass runs, so a burst of requests starts one pass, not several.
            state["resolved_at"] = now
            _save_state(root, state)
            start = True
    noon = local.replace(hour=NOON, minute=0, second=0, microsecond=0).timestamp()
    payload = _payload(report, state, local, noon)
    if start:
        _spawn(lambda: resolve_pass(root, approvals))
    return payload


def _today_report(root, now):
    """``(report, date)`` when the saved report is today's, else ``(None, "")``."""
    report = mr.load_latest(root)
    date = _local(report, now).strftime("%Y-%m-%d") if report else ""
    return (report, date) if report and report.get("date") == date else (None, "")


def act(root, body, *, now=None):
    """``POST /api/report/today``: ``{"action": "done" | "undo", "keys": [...]}`` marks things
    done or open again, ``{"action": "dismiss"}`` hides the scene for the rest of the day.
    Raises ``ValueError`` (a 400) for anything else."""
    now = _now() if now is None else float(now)
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    action = body.get("action")
    if action not in ("done", "undo", "dismiss"):
        raise ValueError("unknown action")
    reason = body.get("reason", "person")
    if reason not in ("person", "all_clear"):
        raise ValueError("unknown reason")
    keys = body.get("keys", [])
    if action != "dismiss" and (not isinstance(keys, list) or len(keys) > MAX_KEYS or
                                not all(isinstance(key, str) for key in keys)):
        raise ValueError("keys must be a list of item keys")
    report, date = _today_report(root, now)
    if not report:
        raise ValueError("there is no morning report for today")
    known = {key for _name, _item, key in _actionable(report)}
    if action != "dismiss" and any(key not in known for key in keys):
        raise ValueError("that is not one of today's things to do")
    with _LOCK:
        state = load_state(root, date)
        if action == "dismiss":
            state.update(dismissed_at=now, dismissed_why=reason)
        for key in keys if action != "dismiss" else ():
            # The person's word, either way; a later check never overrides it.
            state["items"][key] = {"state": "done" if action == "done" else "open",
                                   "by": "person", "at": now}
        _save_state(root, state)
    return {"ok": True, "today": today(root, now=now)}


# ---------------------------------------------------------------- the resolve pass


def _google_ready(root):
    """Is Google connected with permission to write drafts (and so to look one up)?"""
    from . import report_sources
    try:
        report_sources.google("gmail_drafts", "Gmail", root)
    except Exception:                                 # noqa: BLE001 - not ready is the answer
        return False
    return True


def _draft_exists(draft_id, root):
    from . import google_connect
    return google_connect.gmail_draft_exists(draft_id, state_dir=root)


def _pr_merged(slug, number):
    """Was this pull request merged?  Asks ``gh`` about exactly that one pull request."""
    from . import report_sources
    slug, number = str(slug), str(number)
    if not _SLUG_RE.fullmatch(slug) or ".." in slug or not number.isdigit():
        raise ValueError("not a pull request")
    run = report_sources._gh_runner(rs.Context(now=_now()))
    code, out, _err = run(["api", "repos/%s/pulls/%s" % (slug, number)],
                          report_sources.GH_TIMEOUT_S)
    if code != 0:
        raise RuntimeError("gh exited with %d" % code)
    data = json.loads(out or "null")
    if not isinstance(data, dict):
        raise RuntimeError("gh answered with something that is not a pull request")
    return data.get("merged") is True


_APPROVAL_EVIDENCE = "%s · pending" % daily_brief._source_name("approvals", "en")


def _pending_approvals(approvals):
    """Signal ids of the approvals waiting now, or ``None`` when that cannot be known here."""
    if not callable(approvals):
        return None
    try:
        rows = approvals()
    except Exception:                                 # noqa: BLE001 - unknown, not "none"
        return None
    if not isinstance(rows, list):
        return None
    out = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        # The id the Collie source gave the approval's signal (report_sources._brief_signal).
        native = daily_brief._text(row.get("id"), 80) or daily_brief._text(row.get("tool"), 80)
        if native:
            out.add(rs.signal_id("collie", daily_brief._item_id("approval", "approvals", native)))
    return out


def resolve(root, *, now=None, approvals=None):
    """Re-check today's open things and mark done the ones a fact says are done.

    Returns ``{"checked": n, "done": {key: why}}``.  Runs the checks outside the lock and writes
    only items nobody has said anything about since.
    """
    now = _now() if now is None else float(now)
    report, date = _today_report(root, now)
    if not report:
        return {"checked": 0, "done": {}}
    with _LOCK:
        said = set(load_state(root, date)["items"])
    open_ = [(name, item, key) for name, item, key in _actionable(report) if key not in said]
    found, checked = {}, 0
    drafts = []
    for name, item, key in open_:
        draft = item.get("draft") if isinstance(item.get("draft"), dict) else None
        if name == "ready" and draft and draft.get("state") == "created" and \
                isinstance(draft.get("draft_id"), str) and draft["draft_id"]:
            drafts.append((key, draft["draft_id"]))
    if drafts and _google_ready(root):
        for key, draft_id in drafts:
            try:
                exists = _draft_exists(draft_id, root)
            except Exception:                         # noqa: BLE001 - unknown stays open
                continue
            checked += 1
            if exists is False:
                found[key] = "draft_gone"
    for _name, item, key in open_:
        match = _PR_RE.fullmatch(rs._https(item.get("link")))
        if key in found or not match:
            continue
        try:
            merged = _pr_merged(match.group(1), match.group(2))
        except Exception:                             # noqa: BLE001 - unknown stays open
            continue
        checked += 1
        if merged is True:
            found[key] = "pr_merged"
    pending = _pending_approvals(approvals)
    if pending is not None:
        signals = {s.get("id"): s for s in report.get("signals") or [] if isinstance(s, dict)}
        for _name, item, key in open_:
            ids = [i for i in item.get("signal_ids") or [] if isinstance(i, str)]
            asks = [signals.get(i) for i in ids]
            if key in found or not ids or not all(
                    s and s.get("source") == "collie" and s.get("evidence") == _APPROVAL_EVIDENCE
                    for s in asks):
                continue
            checked += 1
            if not any(i in pending for i in ids):
                found[key] = "approval_answered"
    if found:
        with _LOCK:
            state = load_state(root, date)
            for key, why in found.items():
                if key not in state["items"]:         # the person spoke meanwhile: they win
                    state["items"][key] = {"state": "done", "by": why, "at": now}
            _save_state(root, state)
    return {"checked": checked, "done": found}


def resolve_pass(root, approvals=None):
    """The background pass: never raises (a thread has nobody to tell)."""
    try:
        resolve(root, approvals=approvals)
    except Exception:                                 # noqa: BLE001 - the next pass tries again
        pass


# ---------------------------------------------------------------- the report page


_MISSING = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Morning report</title></head>"
            "<body style=\"margin:0;padding:48px 24px;background:#f6f8fc;color:#14213d;"
            "font:16px/1.6 system-ui,sans-serif\"><p>There is no morning report for that day. "
            "Collie writes one each morning; <code>collie report build</code> writes one "
            "now.</p></body></html>")


def page(root, date=""):
    """``(status, html)``: the saved report as the morning email, for a Collie window."""
    report = mr.load_day(root, date) if date else mr.load_latest(root)
    if not report:
        return 404, _MISSING
    html = mre.render(report, avatar="data")["html"]
    # A link would otherwise open inside the chromeless Collie window; a new window is sent to
    # the default browser by the wallpaper host, with the person's own sign-ins.
    return 200, html.replace('<a href="', '<a target="_blank" rel="noopener noreferrer" href="')
