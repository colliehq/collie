"""Signals: the one shape every source of the morning report speaks.

The morning report is written from many places -- Collie's own stores, Gmail, a calendar,
GitHub, the git repositories on this computer, news feeds -- and more will come.  If the report
knew about each of them, every new source would be a change to the report.  So it knows about
none of them.  Each source is an *adapter* that turns what it can see into normalized
**signals**, and every signal is attached to a **project**.  The report groups by project and
reads signals; it never learns where one came from beyond the ``source`` name it carries.

A signal is::

    {"id":        stable hash of (source, the source's own id for the thing),
     "source":    "github" | "local" | "gmail" | ...,
     "project":   "owner/repo" when GitHub knows it, else the repo folder's name,
                  else "source:<source>" (a bucket, not a project),
     "kind":      done | needs_you | waiting_on_others | ready | stale | fyi | event,
     "title":     one bounded line,
     "detail":    one bounded line,
     "when":      epoch seconds or None,
     "link":      an https URL or "",
     "evidence":  a short note of what was read to say this,
     "untrusted": True when the words were written by somebody else (mail, invites, news)}

Adapters may also attach ``meta`` -- private data such as a mail thread id that a later step
needs.  ``meta`` never reaches a model prompt, a rendered page or the saved snapshot; use
:func:`public` to get the schema fields only.

The runner, :func:`collect`, reads every adapter in parallel, each inside its own time limit.
A source that fails costs only itself: it is reported ``unavailable`` with a reason, never
rendered as an empty, clear day.  A source that answers with more than
``MAX_SIGNALS_PER_SOURCE`` signals, or with malformed ones, is kept in part and reported
``partial``.  An exception's message is never carried into a reason, because store and network
errors quote paths, addresses and rows of private data; the exception's type is enough to act on.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field

from . import daily_brief

KINDS = ("done", "needs_you", "waiting_on_others", "ready", "stale", "fyi", "event")
FIELDS = ("id", "source", "project", "kind", "title", "detail", "when", "link", "evidence",
          "untrusted")
SOURCE_PREFIX = "source:"
TITLE_LIMIT = 160
DETAIL_LIMIT = 240
EVIDENCE_LIMIT = 160
PROJECT_LIMIT = 120
MAX_SIGNALS_PER_SOURCE = 60
DEFAULT_TIMEOUT_S = 30.0

_SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class Unavailable(RuntimeError):
    """Raised by an adapter that cannot read its source.  The message is shown as the reason,
    so it must be written for a person and carry no private data."""


@dataclass
class Context:
    """What every adapter is told about this report.  Adapters read it and never change it."""
    now: float
    state_dir: str = ""
    zone: object = None                    # tzinfo for local times; None means UTC
    zone_name: str = "UTC"
    language: str = "en"
    since: float = 0.0                     # start of the "while you slept" window
    previous: dict = field(default_factory=dict)   # the last report's counters
    options: dict = field(default_factory=dict)    # per-source settings and test seams

    def __post_init__(self):
        if not self.since:
            self.since = float(self.now) - 24 * 3600


@dataclass
class Adapter:
    name: str
    label: str
    read: object                           # callable(Context) -> dict
    timeout: float = DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------- the schema


def signal_id(source, native):
    digest = hashlib.sha256(("collie.report.signal/1\u001f%s\u001f%s" % (
        source, native)).encode("utf-8", "replace")).hexdigest()
    return "%s-%s" % (source, digest[:10])


def _https(link):
    href = daily_brief.safe_href(link, external=True) if isinstance(link, str) else ""
    return href if href.startswith("https://") else ""


def make(source, native, *, kind, title, project, detail="", when=None, link="", evidence="",
         untrusted=False, meta=None):
    """One normalized signal.  Raises ``ValueError`` for an unknown source name or kind."""
    source = str(source or "")
    if not _SOURCE_RE.fullmatch(source):
        raise ValueError("invalid signal source")
    if kind not in KINDS:
        raise ValueError("unknown signal kind: %r" % (kind,))
    project = daily_brief._text(project, PROJECT_LIMIT) or SOURCE_PREFIX + source
    title = daily_brief._text(title, TITLE_LIMIT) or daily_brief._text(detail, TITLE_LIMIT) \
        or kind.replace("_", " ")
    out = {"id": signal_id(source, daily_brief._text(native, 400)), "source": source,
           "project": project, "kind": kind, "title": title,
           "detail": daily_brief._text(detail, DETAIL_LIMIT),
           "when": daily_brief._stamp(when), "link": _https(link),
           "evidence": daily_brief._text(evidence, EVIDENCE_LIMIT),
           "untrusted": bool(untrusted)}
    if isinstance(meta, dict) and meta:
        out["meta"] = dict(meta)
    return out


def public(signal):
    """The schema fields only -- what a prompt, a page or a snapshot may carry."""
    return {key: signal.get(key) for key in FIELDS}


def _valid(signal):
    """Re-check a signal an adapter handed back.  Returns the cleaned signal or ``None``."""
    if not isinstance(signal, dict):
        return None
    if signal.get("kind") not in KINDS or not isinstance(signal.get("id"), str):
        return None
    if not _SOURCE_RE.fullmatch(str(signal.get("source") or "")):
        return None
    if not signal["id"].startswith(signal["source"] + "-"):
        return None
    clean = {"id": daily_brief._text(signal["id"], 80), "source": signal["source"],
             "project": daily_brief._text(signal.get("project"), PROJECT_LIMIT)
             or SOURCE_PREFIX + signal["source"],
             "kind": signal["kind"],
             "title": daily_brief._text(signal.get("title"), TITLE_LIMIT) or signal["kind"],
             "detail": daily_brief._text(signal.get("detail"), DETAIL_LIMIT),
             "when": daily_brief._stamp(signal.get("when")),
             "link": _https(signal.get("link")),
             "evidence": daily_brief._text(signal.get("evidence"), EVIDENCE_LIMIT),
             "untrusted": bool(signal.get("untrusted"))}
    if isinstance(signal.get("meta"), dict):
        clean["meta"] = dict(signal["meta"])
    return clean


def github_slug(url):
    """``owner/repo`` for a GitHub remote URL, in the URL's own spelling, else ``""``."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    match = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?github\.com[:/](.+?)(?:\.git)?/?$", raw,
                     re.IGNORECASE)
    if not match:
        return ""
    slug = match.group(1)
    return slug if _SLUG_RE.fullmatch(slug) else ""


def is_project(key):
    return bool(key) and not str(key).startswith(SOURCE_PREFIX)


def project_key(value):
    """How two spellings of one project are recognised as one: GitHub names ignore case."""
    return str(value or "").casefold()


def group_by_project(signals):
    """``{project label: [signals]}`` for real projects, in first-seen order.

    ``colliehq/collie`` from GitHub and ``ColliehQ/Collie`` from a local clone's remote are the
    same project; the first spelling seen is the label.  ``source:`` buckets are not projects.
    """
    labels, groups = {}, {}
    for signal in signals:
        name = signal.get("project") or ""
        if not is_project(name):
            continue
        key = project_key(name)
        label = labels.setdefault(key, name)
        groups.setdefault(label, []).append(signal)
    return groups


# ---------------------------------------------------------------- the runner


def _reason(exc):
    return "could not be read (%s)" % type(exc).__name__


def _read_one(adapter, context, box):
    started = time.time()
    try:
        box["value"] = adapter.read(context)
    except Unavailable as exc:
        box["error"] = daily_brief._text(str(exc), 200) or "unavailable"
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        box["error"] = _reason(exc)
    box["took"] = time.time() - started


def _settle(adapter, box, finished, started_at):
    row = {"name": adapter.name, "label": adapter.label, "state": "unavailable", "reason": "",
           "detail": "", "read_at": time.time(), "took_ms": 0, "signals": 0, "stats": {}}
    if not finished:
        row["reason"] = "took longer than %d s" % max(1, round(adapter.timeout))
        row["took_ms"] = int(adapter.timeout * 1000)
        return row, [], {}, {}, []
    row["took_ms"] = max(0, int(box.get("took", 0.0) * 1000))
    row["read_at"] = started_at + box.get("took", 0.0)
    if "error" in box:
        row["reason"] = box["error"]
        return row, [], {}, {}, []
    value = box.get("value")
    if not isinstance(value, dict):
        row["reason"] = "answered with something that is not a report"
        return row, [], {}, {}, []
    raw = value.get("signals") if isinstance(value.get("signals"), list) else []
    kept, seen, bad = [], set(), 0
    for signal in raw:
        clean = _valid(signal)
        if clean is None or clean["id"] in seen:
            bad += 1
            continue
        seen.add(clean["id"])
        kept.append(clean)
    notes = []
    state = value.get("state") if value.get("state") in ("ok", "partial") else "ok"
    if value.get("reason"):
        notes.append(daily_brief._text(value.get("reason"), 200))
    if bad:
        state = "partial"
        notes.append("%d malformed or repeated signal(s) were dropped" % bad)
    if len(kept) > MAX_SIGNALS_PER_SOURCE:
        state = "partial"
        notes.append("only the first %d of %d signals were kept (%d left out)" % (
            MAX_SIGNALS_PER_SOURCE, len(kept), len(kept) - MAX_SIGNALS_PER_SOURCE))
        kept = kept[:MAX_SIGNALS_PER_SOURCE]
    counters = value.get("counters") if isinstance(value.get("counters"), dict) else {}
    stats = value.get("stats") if isinstance(value.get("stats"), dict) else {}
    row.update(state=state, reason="; ".join(note for note in notes if note),
               detail=daily_brief._text(value.get("detail"), 200), signals=len(kept),
               stats={daily_brief._text(k, 40): v for k, v in list(stats.items())[:12]
                      if isinstance(v, (int, float, str)) and not isinstance(v, bool)})
    counters = {daily_brief._text(k, 160): v for k, v in list(counters.items())[:500]
                if isinstance(v, (int, float)) and not isinstance(v, bool)}
    activity = value.get("activity") if isinstance(value.get("activity"), dict) else {}
    activity = {daily_brief._text(k, PROJECT_LIMIT): daily_brief._stamp(v)
                for k, v in list(activity.items())[:500]
                if is_project(daily_brief._text(k, PROJECT_LIMIT))
                and not isinstance(v, bool) and daily_brief._stamp(v) is not None}
    aliases = [[daily_brief._text(name, PROJECT_LIMIT) for name in group[:10]
                if is_project(daily_brief._text(name, PROJECT_LIMIT))]
               for group in (value.get("aliases") or [])[:200] if isinstance(group, list)]
    return row, kept, counters, activity, [group for group in aliases if len(group) > 1]


class Collection(tuple):
    """``(signals, sources, counters)``, and ``activity``: project -> when the person last
    worked on it, as the sources saw it."""

    def __new__(cls, signals, sources, counters, activity):
        self = super().__new__(cls, (signals, sources, counters))
        self.activity = activity
        return self


def _canonical(signals, activity, aliases):
    """One name per project.  ``aliases`` are names sources know to be the same project (one
    checkout with two remotes, a fork and its upstream); spelling differences are the same
    project anyway.  The name most signals already use wins, then the most recently active."""
    parent = {}

    def find(key):
        while parent.setdefault(key, key) != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for group in aliases:
        for name in group[1:]:
            parent[find(project_key(name))] = find(project_key(group[0]))
    names = {}
    for signal in signals:
        if is_project(signal["project"]):
            names.setdefault(find(project_key(signal["project"])), {}).setdefault(
                signal["project"], [0, 0.0])[0] += 1
    for name, when in activity.items():
        entry = names.setdefault(find(project_key(name)), {}).setdefault(name, [0, 0.0])
        entry[1] = max(entry[1], when)
    chosen = {root: sorted(options.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))[0][0]
              for root, options in names.items()}
    for signal in signals:
        if is_project(signal["project"]):
            signal["project"] = chosen[find(project_key(signal["project"]))]
    merged = {}
    for name, when in activity.items():
        label = chosen[find(project_key(name))]
        merged[label] = max(merged.get(label, 0.0), when)
    return merged


def collect(context, adapters):
    """``(signals, sources, counters)`` from every adapter, read in parallel, with
    ``.activity`` on the result.

    ``sources`` is the provenance: one row per adapter with its ``state`` (ok, partial,
    unavailable), a ``reason`` when it is not ok, when it was read and how long it took.  A
    source that does not answer inside its own ``timeout`` is ``unavailable``; whatever it
    returns later is ignored.  Signal ids are unique across the whole result.

    An adapter may also return ``activity`` (project -> when the person last worked on it) and
    ``aliases`` (lists of project names that are one project).  Every project then has one
    name across all sources, and ``activity`` keeps the latest time for each.
    """
    runs = []
    for adapter in adapters:
        box = {}
        worker = threading.Thread(target=_read_one, args=(adapter, context, box),
                                  name="collie-report-%s" % adapter.name, daemon=True)
        runs.append((adapter, box, worker, time.time(), time.monotonic()))
        worker.start()
    signals, sources, counters, seen = [], [], {}, set()
    activity, aliases = {}, []
    for adapter, box, worker, started_at, started in runs:
        worker.join(max(0.0, float(adapter.timeout) - (time.monotonic() - started)))
        row, kept, found, active, same = _settle(adapter, dict(box), not worker.is_alive(),
                                                 started_at)
        fresh = [signal for signal in kept if signal["id"] not in seen]
        seen.update(signal["id"] for signal in fresh)
        signals += fresh
        counters.update(found)
        for name, when in active.items():
            activity[name] = max(activity.get(name, 0.0), when)
        aliases += same
        sources.append(row)
    return Collection(signals, sources, counters, _canonical(signals, activity, aliases))
