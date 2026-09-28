"""Headlines from the RSS and Atom feeds a person chose, as the Daily Brief's news.

Off until the person adds a feed.  With no feed saved nothing is fetched, no thread
runs, and the brief is built with ``news=None`` exactly as before.

This is the only part of the brief that reaches the internet, so every fetch goes
through the same guards, and each one is a guard rather than an optimisation:

* **Only what was asked for.**  A feed address is ``https`` on port 443 with a host
  name and no user name or password in it.  Nothing else is ever requested -- not the
  item links, not images, not a favicon.
* **Only the public internet.**  Every address the host name resolves to must be a
  public one (``is_global``; an IPv4 address carried inside IPv6 is checked as IPv4).
  The connection is then pinned to the address that was checked, so a second DNS
  answer cannot swap in a private address between the check and the connect.  TLS
  still names the original host and verifies its certificate.  Each redirect (at most
  four) is checked again, from the start, by the same rules.
* **Bounded.**  No proxy is read from the environment.  The answer must be
  uncompressed and at most 2 MiB -- a bigger one is refused, not cut.  One fetch has
  a 20-second deadline covering every redirect, the TLS handshake, the headers, the
  chunk sizes and the body; when it passes, the connection is shut down from outside,
  so a server that drips a byte at a time cannot hold a worker past it.  Only name
  resolution is left to the operating system's own resolver timeout.
* **Plain XML, read in linear time.**  A document that declares a DTD or an entity is
  refused, which closes entity expansion and external entities together.  One
  streaming pass reads it: deeper than 32 levels or more than 50,000 elements is
  refused, only an item's own fields are read, each is cut before any pattern runs,
  and reading stops after 100 items.  No feed can make parsing slow.
* **Rarely.**  In the background a feed is fetched at most once per the chosen
  interval, never more often than every 15 minutes, and only while Collie runs and
  after the brief has been opened.  Saving a feed, or pressing *Check now*, fetches
  at once, but never the same feed twice within a minute.

Remote text is display-only.  A title and a summary are collapsed to one plain line
here, bounded again by the brief builder, and drawn as text by the page.  A headline
is never an instruction, never a suggestion's prompt, and never ranks above anything
in the person's day: it can only be read and followed as a link.

Settings carry a revision, like the to-do list: a save from a window that is behind is
refused rather than overwriting, and a fetch that finishes after its feed was removed
cannot bring that feed's headlines back.
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from xml.parsers import expat

from . import daily_brief

SCHEMA = "collie.daily_brief.news/1"
USER_AGENT = "Collie-DailyBrief/1.0 (+https://github.com/colliehq/collie)"
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEEDS = 8
MAX_TOPICS = 20
TOPIC_LIMIT = 80
URL_LIMIT = 2048
MAX_REDIRECTS = 4
SOCKET_TIMEOUT = 7.0
FETCH_DEADLINE = 20.0
#: Items read from one feed, with or without a title; parsing stops after the last.
ITEMS_PER_FEED = 100
#: A feed nested deeper, or holding more elements, is refused as it is read.  Real
#: feeds sit near depth 5 and a few thousand elements.
MAX_DEPTH = 32
MAX_ELEMENTS = 50_000
#: Characters kept from one field of one item before it is reduced to a plain line.
FIELD_CHARS = 4096
REFRESH_RANGE = (15, 1440)
HEADLINE_RANGE = (1, 20)
#: A feed the person asked for by hand is still not fetched twice in this many seconds.
MANUAL_FLOOR_SECONDS = 60
#: How often the background worker looks for a feed that has come due.
WORKER_TICK_SECONDS = 60


class NewsError(ValueError):
    """A feed or a setting this module refuses by name.  Its text is ours, not the feed's."""


class NewsConflict(NewsError):
    """The feed settings changed in another window first.  Nothing was saved."""


def defaults():
    return {"revision": 0, "feeds": [], "topics": [], "refresh_minutes": 60, "max_items": 8}


def path_for(root):
    return os.path.join(os.path.abspath(str(root)), "daily-brief", "news.db")


# ---------------------------------------------------------------- fetching


def feed_url(value):
    """The one kind of address a feed may have, normalised, or :class:`NewsError`."""
    if not isinstance(value, str) or not value.strip() or len(value) > URL_LIMIT:
        raise NewsError("Use an https:// feed address")
    value = value.strip()
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise NewsError("Use an https:// feed address") from None
    if (parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or port not in (None, 443)
            or any(ch.isspace() or ord(ch) < 32 for ch in value)):
        raise NewsError("Use an https:// feed address on the standard port, with nothing "
                        "private in it")
    return urllib.parse.urlunsplit(("https", parts.netloc.lower(), parts.path or "/",
                                    parts.query, ""))


def _public(address):
    """Is this resolved address somewhere on the public internet (never this network)?"""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in ipaddress.ip_network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not embedded.is_global:
            return False
    return ip.is_global


def _resolve(host):
    return socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)


def _checked_address(host, resolve):
    """``(family, sockaddr)`` to connect to -- only if *every* answer is public."""
    try:
        answers = list(resolve(host))
    except (OSError, UnicodeError):
        raise NewsError("The feed's address could not be found") from None
    if not answers:
        raise NewsError("The feed's address could not be found")
    if not all(_public(answer[4][0]) for answer in answers):
        raise NewsError("The feed's address is not on the public internet")
    return answers[0][0], answers[0][4]


def _pinned_https(host, family, sockaddr, timeout):
    """An HTTPS connection to ``host`` that can only reach the address already checked."""
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout,
                                       context=ssl.create_default_context())

    def create_connection(_address, timeout=timeout, source_address=None, **_ignored):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
        except BaseException:
            sock.close()
            raise
        return sock

    conn._create_connection = create_connection
    return conn


_TOO_SLOW = "The feed took too long to answer"


def _read_bounded(response, stop):
    chunks, size = [], 0
    while True:
        if time.monotonic() > stop:
            raise NewsError(_TOO_SLOW)
        chunk = response.read1(65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_FEED_BYTES:
            raise NewsError("The feed is larger than 2 MiB")
        chunks.append(chunk)


class _Watchdog:
    """Ends a fetch's connections when the fetch's own deadline passes.

    A socket timeout bounds one read, not a fetch: a server that sends one byte just
    before each read would time out could hold the TLS handshake, the status line, the
    headers or a chunk-size line for hours, and none of those return to our code
    between bytes.  At the deadline this timer shuts every connection the fetch opened
    down from outside -- through a duplicate of the socket taken as it was created, so
    it still works after TLS has wrapped the original -- and the blocked read fails at
    once, whatever it was reading.
    """

    def __init__(self, stop):
        self.fired = False
        self._guard = threading.Lock()
        self._copies = []
        self._timer = threading.Timer(max(0.0, stop - time.monotonic()), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock):
        copy = sock.dup()
        with self._guard:
            self._copies.append(copy)
            late = self.fired
        if late:
            self._end(copy)

    @staticmethod
    def _end(copy):
        try:
            copy.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _fire(self):
        with self._guard:
            self.fired = True
            copies = list(self._copies)
        for copy in copies:
            self._end(copy)

    def close(self):
        self._timer.cancel()
        with self._guard:
            copies, self._copies = self._copies, []
        for copy in copies:
            copy.close()


def _watched(conn, watchdog):
    """Hand every socket ``conn`` opens to the watchdog as it is created."""
    create = getattr(conn, "_create_connection", None)
    if create is None:
        return conn

    def create_watched(*args, **kwargs):
        sock = create(*args, **kwargs)
        watchdog.watch(sock)
        return sock

    conn._create_connection = create_watched
    return conn


def fetch_feed(url, *, resolve=None, connect=None, deadline=FETCH_DEADLINE):
    """The feed document at ``url``, fetched under every rule in the module docstring.

    ``resolve(host)`` and ``connect(host, family, sockaddr, timeout)`` exist so tests
    can stand in for DNS and for the TLS socket; the address check, the redirect
    rules and the size and time limits are the same code either way.

    ``deadline`` covers the whole fetch, every redirect included, from the first
    connect to the last byte: past it the connection is shut down (see ``_Watchdog``)
    and the fetch fails.  Name resolution is the one step it cannot interrupt; that is
    bounded by the operating system's resolver, and nothing is sent until it answers.
    """
    resolve, connect = resolve or _resolve, connect or _pinned_https
    stop = time.monotonic() + deadline
    watchdog = _Watchdog(stop)
    try:
        return _fetch(url, resolve, connect, stop, watchdog)
    except NewsError:
        raise
    except Exception:
        if watchdog.fired:
            raise NewsError(_TOO_SLOW) from None
        raise
    finally:
        watchdog.close()


def _fetch(url, resolve, connect, stop, watchdog):
    for _hop in range(MAX_REDIRECTS + 1):
        url = feed_url(url)
        parts = urllib.parse.urlsplit(url)
        family, sockaddr = _checked_address(parts.hostname, resolve)
        remaining = stop - time.monotonic()
        if remaining <= 0 or watchdog.fired:
            raise NewsError(_TOO_SLOW)
        conn = _watched(connect(parts.hostname, family, sockaddr,
                                min(SOCKET_TIMEOUT, remaining)), watchdog)
        try:
            conn.request("GET", urllib.parse.urlunsplit(("", "", parts.path or "/",
                                                          parts.query, "")),
                         headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                  "Accept": "application/rss+xml, application/atom+xml, "
                                            "application/xml;q=0.9, text/xml;q=0.8"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader("Location") or ""
                if not target:
                    raise NewsError("The feed redirected without saying where")
                url = urllib.parse.urljoin(url, target)
                continue
            if response.status != 200:
                raise NewsError("The feed answered HTTP %d" % response.status)
            encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
            if encoding not in ("", "identity"):
                raise NewsError("The feed answered compressed, which Collie does not accept")
            declared = (response.getheader("Content-Length") or "").strip()
            if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
                raise NewsError("The feed is larger than 2 MiB")
            data = _read_bounded(response, stop)
            if watchdog.fired:
                # A connection shut down mid-body can read as a short, "complete" one.
                raise NewsError(_TOO_SLOW)
            return data
        finally:
            conn.close()
    raise NewsError("The feed redirected too many times")


# ---------------------------------------------------------------- parsing


def _plain(value, limit):
    """One line of plain text: tags dropped, entities decoded, whitespace collapsed.

    The input is cut *before* any pattern runs, and the patterns cannot backtrack: a tag
    is ``<`` and ``>`` with neither between them.  The old ``<[^>]*>`` over a whole
    field was quadratic on a title of ``<`` with no ``>`` -- 15 s for 200 KB, all of it
    holding the interpreter lock, so every request thread in Collie stood still.
    """
    text = (value or "")[:limit * 8]
    text = re.sub(r"<[^<>]*>", " ", text)
    text = re.sub(r"<[^<>]*$", " ", text)             # a tag the cut left half-open
    text = html.unescape(text)
    return " ".join("".join(ch if ch >= " " else " " for ch in text).split())[:limit]


def _stamp(value):
    value = (value or "").strip()
    if not value:
        return 0.0
    try:
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            moment = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.replace(tzinfo=moment.tzinfo or _dt.timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


class _Enough(Exception):
    """Every item this feed may contribute has been read; parsing stops here."""


def _declares_dtd(data):
    """The byte scan: a DTD or entity in any ASCII-compatible or UTF-16/32 encoding."""
    return bool(re.search(br"<!\s*(DOCTYPE|ENTITY)", data.replace(b"\x00", b""), re.I))


_NOT_A_FEED = "The address did not return an RSS or Atom feed"
_ROOTS = ("rss", "feed", "RDF")
_ITEMS = ("item", "entry")
_FIELDS = ("title", "link", "description", "summary", "pubDate", "published", "updated",
           "date")


def parse_feed(data, source):
    """``(rows, skipped)`` from an RSS 2.0, RSS 1.0 or Atom document.

    A row is ``{id, title, url, published_at, summary}``.  Only items whose link the
    brief itself would show (``https``, a host, nothing private, see
    :func:`daily_brief.safe_href`) are kept, and ``skipped`` says how many were left
    out for that, so the page can say so.

    One streaming pass, linear in the document whatever it holds.  A document nested
    deeper than ``MAX_DEPTH`` or holding more than ``MAX_ELEMENTS`` elements is refused
    as it is read.  Only an item's own children are read -- never an item inside an
    item -- and each field keeps at most ``FIELD_CHARS`` characters.  Parsing stops
    after ``ITEMS_PER_FEED`` items, with or without a title.  (Walking every child's
    whole subtree made a 2 MiB feed of nested items take five minutes.)  A DTD or an
    entity declaration is refused twice: by a byte scan before parsing, and by the
    parser itself whatever the document's encoding.
    """
    if not isinstance(data, bytes):
        raise NewsError(_NOT_A_FEED)
    if len(data) > MAX_FEED_BYTES:
        raise NewsError("The feed is larger than 2 MiB")
    if _declares_dtd(data):
        raise NewsError("The feed declares a DTD or entities, which Collie does not read")
    rows, counts = [], {"skipped": 0, "examined": 0, "elements": 0, "depth": 0}
    open_item, field = None, None

    def finish(item):
        counts["examined"] += 1
        values = item["fields"]
        title = _plain(values.get("title"), 300)
        link = item["link"].strip()
        target = urllib.parse.urljoin(source, link) if link else ""
        if title and not daily_brief.safe_href(target, external=True):
            counts["skipped"] += 1    # the brief's own link rule: kept here means shown
        elif title:
            rows.append({"id": hashlib.sha256(target.encode("utf-8")).hexdigest()[:24],
                         "title": title, "url": target,
                         "published_at": _stamp(values.get("pubDate") or values.get("published")
                                                or values.get("updated") or values.get("date")),
                         "summary": _plain(values.get("description") or values.get("summary"),
                                           240)})
        if counts["examined"] >= ITEMS_PER_FEED:
            raise _Enough()

    def start(name, attrs):
        nonlocal open_item, field
        counts["depth"] += 1
        counts["elements"] += 1
        depth = counts["depth"]
        if depth > MAX_DEPTH:
            raise NewsError("The feed nests elements more than %d deep, which Collie does "
                            "not read" % MAX_DEPTH)
        if counts["elements"] > MAX_ELEMENTS:
            raise NewsError("The feed holds more than %d elements, which Collie does not "
                            "read" % MAX_ELEMENTS)
        local = name.rsplit("}", 1)[-1]
        if depth == 1:
            if local not in _ROOTS:
                raise NewsError(_NOT_A_FEED)
        elif open_item is None:
            if local in _ITEMS:
                open_item = {"depth": depth, "fields": {}, "link": ""}
        elif field is None and depth == open_item["depth"] + 1:
            # Only the item's own children.  Anything else inside it -- including an
            # item nested in an item -- is passed over, never read as another item.
            if local == "link" and "href" in attrs:
                if not open_item["link"] and attrs.get("rel", "alternate") == "alternate":
                    open_item["link"] = attrs.get("href") or ""
            elif local in _FIELDS and local not in open_item["fields"]:
                field = {"name": local, "depth": depth, "parts": [], "size": 0}

    def text(value):
        if field is not None and field["size"] < FIELD_CHARS:
            piece = value[:FIELD_CHARS - field["size"]]
            field["parts"].append(piece)
            field["size"] += len(piece)

    def end(_name):
        nonlocal open_item, field
        depth = counts["depth"]
        counts["depth"] -= 1
        if field is not None and depth == field["depth"]:
            value = "".join(field["parts"])
            open_item["fields"][field["name"]] = value
            if field["name"] == "link" and not open_item["link"]:
                open_item["link"] = value
            field = None
        elif open_item is not None and depth == open_item["depth"]:
            item, open_item = open_item, None
            finish(item)

    def refuse(*_args):
        raise NewsError("The feed declares a DTD or entities, which Collie does not read")

    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    try:
        parser.Parse(data, True)
    except _Enough:
        pass
    except NewsError:
        raise
    except (expat.ExpatError, ValueError, LookupError):
        # Malformed XML, or an encoding the parser refuses (UTF-7 raises ValueError).
        raise NewsError(_NOT_A_FEED) from None
    return rows, counts["skipped"]


def matches_topic(text, topic):
    """Whole-word for Latin topics, so "AI" does not also pick "daily" or "chair"."""
    text, topic = text.casefold(), topic.casefold()
    if re.fullmatch(r"[a-z0-9][a-z0-9 .+#/-]*", topic):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(topic) + r"(?![a-z0-9])", text))
    return topic in text


# ---------------------------------------------------------------- the store


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _refresh_lock(path):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def _clean_settings(value):
    if not isinstance(value, dict):
        raise NewsError("Expected feed settings")
    clean = defaults()
    feeds, topics = value.get("feeds", []), value.get("topics", [])
    if not isinstance(feeds, list) or not isinstance(topics, list):
        raise NewsError("Feeds and topics must be lists")
    if len(feeds) > MAX_FEEDS:
        raise NewsError("Add at most %d feeds" % MAX_FEEDS)
    if len(topics) > MAX_TOPICS:
        raise NewsError("Use at most %d topics" % MAX_TOPICS)
    clean["feeds"] = list(dict.fromkeys(feed_url(url) for url in feeds))
    words = []
    for topic in topics:
        if not isinstance(topic, str) or len(topic) > TOPIC_LIMIT:
            raise NewsError("A topic can be at most %d characters" % TOPIC_LIMIT)
        word = " ".join(topic.split())
        if word and word.casefold() not in [w.casefold() for w in words]:
            words.append(word)
    clean["topics"] = words
    for key, (low, high) in (("refresh_minutes", REFRESH_RANGE), ("max_items", HEADLINE_RANGE)):
        number = value.get(key, clean[key])
        if type(number) is not int or not low <= number <= high:
            raise NewsError("%s must be a whole number from %d to %d" % (
                "The check interval" if key == "refresh_minutes" else "Headlines", low, high))
        clean[key] = number
    return clean


def _reason(exc):
    """What the page may say about a failed fetch.  Never a raw socket or path detail."""
    if isinstance(exc, NewsError):
        return str(exc)
    if isinstance(exc, ssl.SSLError):
        return "The feed's secure connection could not be verified"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "The feed did not answer in time"
    return "The feed could not be reached; Collie will try again later"


class NewsStore:
    """Feed settings and the last good answer from each feed, under one state directory."""

    def __init__(self, root):
        self.path = path_for(root)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY, "
                       "value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS feeds(url TEXT PRIMARY KEY, "
                       "items TEXT NOT NULL, checked_at REAL NOT NULL, "
                       "success_at REAL NOT NULL, error TEXT NOT NULL, "
                       "skipped INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO settings VALUES(1, ?)", (json.dumps(defaults()),))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _settings(db):
        return json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])

    def settings(self):
        with self._connect() as db:
            return self._settings(db)

    def save_settings(self, value):
        """Replace the settings made at ``value["revision"]``, or :class:`NewsConflict`."""
        clean = _clean_settings(value)
        revision = value.get("revision")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._settings(db)
            if type(revision) is not int or revision != current["revision"]:
                raise NewsConflict("Feeds were changed in another window. Nothing was saved.")
            clean["revision"] = current["revision"] + 1
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(clean),))
            for row in db.execute("SELECT url FROM feeds").fetchall():
                if row[0] not in clean["feeds"]:
                    db.execute("DELETE FROM feeds WHERE url=?", (row[0],))
        return clean

    def refresh(self, *, fetch=None, force=False, now=None):
        """Fetch every subscribed feed that is due.  Returns how many were fetched.

        One refresh at a time per store.  A failure keeps that feed's last good
        headlines and records why; a feed removed while its fetch was running is not
        written back, so it cannot reappear.
        """
        fetch = fetch or fetch_feed
        with _refresh_lock(self.path):
            wall = time.time() if now is None else float(now)
            prefs = self.settings()
            with self._connect() as db:
                checked = {row["url"]: row["checked_at"]
                           for row in db.execute("SELECT url, checked_at FROM feeds")}
            floor = MANUAL_FLOOR_SECONDS if force else prefs["refresh_minutes"] * 60
            due = [url for url in prefs["feeds"] if wall - checked.get(url, 0.0) >= floor]
            if not due:
                return 0

            def one(url):
                try:
                    rows, skipped = parse_feed(fetch(url), url)
                    return url, rows, skipped, ""
                except Exception as exc:      # noqa: BLE001 - recorded per feed, not raised
                    return url, None, 0, _reason(exc)

            with ThreadPoolExecutor(max_workers=min(4, len(due))) as pool:
                results = list(pool.map(one, due))
            stamp = time.time() if now is None else float(now)
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self._settings(db)["feeds"]
                for url, rows, skipped, error in results:
                    if url not in current:
                        continue              # removed meanwhile: never written back
                    old = db.execute("SELECT * FROM feeds WHERE url=?", (url,)).fetchone()
                    if rows is None:
                        db.execute("INSERT OR REPLACE INTO feeds VALUES(?,?,?,?,?,?)", (
                            url, old["items"] if old else "[]", stamp,
                            old["success_at"] if old else 0.0, error,
                            old["skipped"] if old else 0))
                    else:
                        db.execute("INSERT OR REPLACE INTO feeds VALUES(?,?,?,?,?,?)",
                                   (url, json.dumps(rows), stamp, stamp, "", skipped))
            return len(due)

    def panel(self):
        """What the page shows about the feeds: settings, and each feed's last answer."""
        with self._connect() as db:
            db.execute("BEGIN")
            prefs = self._settings(db)
            cache = {row["url"]: dict(row) for row in db.execute("SELECT * FROM feeds")}
        feeds = []
        for url in prefs["feeds"]:
            row = cache.get(url) or {}
            feeds.append({"url": url, "host": urllib.parse.urlsplit(url).hostname or "",
                          "checked_at": row.get("checked_at") or 0.0,
                          "success_at": row.get("success_at") or 0.0,
                          "error": row.get("error") or "",
                          "headlines": len(json.loads(row.get("items") or "[]")),
                          "skipped": row.get("skipped") or 0})
        return {"settings": prefs, "feeds": feeds, "refreshing": refreshing(self.path)}

    def headlines(self):
        """The rows for ``daily_brief.build(news=...)``, or ``None`` when news is off.

        Each row names its publisher by the host it was fetched from -- where Collie
        actually got it -- rather than by whatever name the feed gives itself.
        """
        with self._connect() as db:
            db.execute("BEGIN")
            prefs = self._settings(db)
            cache = {row["url"]: row["items"] for row in db.execute("SELECT url, items FROM feeds")}
        if not prefs["feeds"]:
            return None
        topics = prefs["topics"]
        rows = []
        for url in prefs["feeds"]:
            host = urllib.parse.urlsplit(url).hostname or ""
            for item in json.loads(cache.get(url) or "[]"):
                text = "%s %s" % (item.get("title", ""), item.get("summary", ""))
                if topics and not any(matches_topic(text, topic) for topic in topics):
                    continue
                rows.append({"title": item.get("title", ""), "url": item.get("url", ""),
                             "source": host, "published_at": item.get("published_at") or 0,
                             "summary": item.get("summary", "")})
        rows.sort(key=lambda row: (-float(row["published_at"] or 0), row["url"]))
        seen, unique = set(), []
        for row in rows:
            if row["url"] not in seen:
                seen.add(row["url"])
                unique.append(row)
        return unique[:prefs["max_items"]]


def configured(root):
    """Whether any feed is saved -- answered without creating anything."""
    if not os.path.isfile(path_for(root)):
        return False
    return bool(NewsStore(root).settings()["feeds"])


# ---------------------------------------------------------------- background refresh


_WORKERS = {}
_WORKERS_GUARD = threading.Lock()


def refreshing(path):
    with _WORKERS_GUARD:
        worker = _WORKERS.get(path)
        return bool(worker and worker["busy"])


def start_refresh(root, *, fetch=None, tick=WORKER_TICK_SECONDS):
    """Keep this root's feeds fresh while Collie runs.  Lazy, single, and self-ending.

    Starts one daemon thread per store if a feed is saved and none is running.  Each
    tick it fetches only the feeds whose interval has passed, and it exits once no
    feed is saved.  Returns whether a worker is running afterwards.
    """
    if not configured(root):
        return False
    path = path_for(root)
    with _WORKERS_GUARD:
        if path in _WORKERS:
            return True
        worker = {"busy": False, "stop": threading.Event()}
        _WORKERS[path] = worker

    def loop():
        try:
            while not worker["stop"].is_set():
                with _WORKERS_GUARD:
                    worker["busy"] = True
                try:
                    store = NewsStore(root)
                    store.refresh(fetch=fetch)
                    still = bool(store.settings()["feeds"])
                except Exception:             # noqa: BLE001 - a bad tick waits for the next
                    still = True
                finally:
                    with _WORKERS_GUARD:
                        worker["busy"] = False
                if not still:
                    return
                worker["stop"].wait(tick)
        finally:
            with _WORKERS_GUARD:
                if _WORKERS.get(path) is worker:
                    del _WORKERS[path]

    threading.Thread(target=loop, name="collie-brief-news", daemon=True).start()
    return True


def stop_refresh(root):
    """Ask this root's worker to finish its tick and exit (tests, and shutdown)."""
    with _WORKERS_GUARD:
        worker = _WORKERS.get(path_for(root))
    if worker:
        worker["stop"].set()
