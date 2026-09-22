"""Local calendar ingestion and privacy-preserving meeting reminders.

This module deliberately schedules *prompts*, never recordings.  Calendar metadata stays under
``COLLIE_STATE_DIR`` (``~/.collie`` by default), sensitive events are suppressed locally, and every
recording still has to pass :class:`harness.meetings.MeetingStore`'s fresh consent check.

The iCalendar reader is intentionally small and dependency-free.  It accepts the common VEVENT,
RRULE, EXDATE, and RECURRENCE-ID shapes emitted by Google, Outlook, and Apple calendar exports.  It
does not fetch remote calendar URLs: the browser reads an explicitly selected ``.ics`` file and
sends its contents to the authenticated loopback API.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
MAX_ICS_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 10_000
_LOCK = threading.RLock()
_EVENT_RE = re.compile(r"^evt_[0-9a-f]{24}$")
_SERIES_RE = re.compile(r"^series_[0-9a-f]{24}$")
_URL_RE = re.compile(r"https://[^\s<>\]\[\)\(\"']+", re.I)
_BYDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_TEMPLATES = {"general", "standup", "one_on_one", "interview", "customer", "planning"}
DEFAULT_SENSITIVE_TERMS = [
    "therapy", "therapist", "doctor", "medical", "health appointment",
    "human resources", "hr meeting", "performance review", "disciplinary", "termination",
    "attorney", "lawyer", "legal counsel", "bank", "banking", "payroll",
]


class ReminderError(RuntimeError):
    pass


def _finite(value, default=0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _text(value, limit: int) -> str:
    return str(value or "").replace("\x00", "")[:limit]


def _hash_id(prefix: str, *parts) -> str:
    raw = "\x1f".join(str(part or "") for part in parts).encode("utf-8", "replace")
    return prefix + hashlib.sha256(raw).hexdigest()[:24]


def _local_tz():
    # A datetime's ``astimezone().tzinfo`` is often only today's fixed UTC offset on Windows.  If
    # reused for a winter event it can be one hour wrong across DST.  Keeping floating/local values
    # naive lets ``datetime.timestamp()`` ask the operating system for the offset on the event date.
    # Explicit UTC and known IANA TZID values still receive a real tzinfo below.
    return None


def _safe_https_url(value) -> str:
    value = _text(value, 2_000).strip().rstrip(".,;")
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return ""
    return value


def _meeting_url(*values) -> str:
    preferred = ("meet.google.com", "zoom.us", "teams.microsoft.com", "webex.com",
                 "whereby.com", "around.co")
    fallback = ""
    for value in values:
        for match in _URL_RE.findall(str(value or "")):
            safe = _safe_https_url(match)
            if not safe:
                continue
            if any(host in urllib.parse.urlparse(safe).hostname.lower() for host in preferred):
                return safe
            fallback = fallback or safe
    return fallback


def _infer_template(title: str) -> str:
    value = title.casefold()
    if "standup" in value or "stand-up" in value or "daily sync" in value:
        return "standup"
    if "1:1" in value or "one on one" in value or "one-on-one" in value:
        return "one_on_one"
    if "interview" in value:
        return "interview"
    if "customer" in value or "client" in value:
        return "customer"
    if "planning" in value or "roadmap" in value:
        return "planning"
    return "general"


def _unescape_ical(value: str) -> str:
    return (value.replace("\\N", "\n").replace("\\n", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _unfold_ical(raw: str) -> list[str]:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _property(line: str):
    if ":" not in line:
        return "", {}, ""
    head, value = line.split(":", 1)
    bits = head.split(";")
    name = bits[0].strip().upper()
    params = {}
    for bit in bits[1:]:
        if "=" in bit:
            key, item = bit.split("=", 1)
            params[key.strip().upper()] = item.strip().strip('"')
    return name, params, _unescape_ical(value)


def _parse_datetime(value: str, params: dict, fallback_tz=None):
    value = str(value or "").strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or bool(re.fullmatch(r"\d{8}", value))
    tz = fallback_tz or _local_tz()
    tzid = params.get("TZID", "").strip()
    if tzid:
        try:
            tz = ZoneInfo(tzid)
        except ZoneInfoNotFoundError:
            # Windows Python installations do not always ship the IANA tz database.  A floating
            # local time is safer than silently treating a local calendar value as UTC.
            tz = fallback_tz or _local_tz()
    utc = value.endswith("Z")
    if utc:
        value = value[:-1]
        tz = dt.timezone.utc
    formats = ["%Y%m%d"] if is_date else ["%Y%m%dT%H%M%S", "%Y%m%dT%H%M"]
    parsed = None
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(value, fmt)
            break
        except ValueError:
            pass
    if parsed is None:
        raise ReminderError("unsupported iCalendar date: %s" % value[:80])
    return parsed.replace(tzinfo=tz), is_date


def _duration(value: str) -> float:
    match = re.fullmatch(
        r"(?P<sign>-)?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?", str(value or "").upper())
    if not match:
        return 0.0
    seconds = (int(match.group("weeks") or 0) * 604800 + int(match.group("days") or 0) * 86400 +
               int(match.group("hours") or 0) * 3600 + int(match.group("minutes") or 0) * 60 +
               int(match.group("seconds") or 0))
    return float(-seconds if match.group("sign") else seconds)


def _component(lines: list[str]) -> dict:
    props: dict[str, list[tuple[dict, str]]] = {}
    for line in lines:
        name, params, value = _property(line)
        if name:
            props.setdefault(name, []).append((params, value))
    return props


def _first(component: dict, name: str, default=""):
    rows = component.get(name, [])
    return rows[0] if rows else ({}, default)


def _rrule(value: str) -> dict:
    out = {}
    for item in str(value or "").split(";"):
        if "=" in item:
            key, val = item.split("=", 1)
            out[key.upper()] = val
    return out


def _month_delta(a: dt.datetime, b: dt.datetime) -> int:
    return (b.year - a.year) * 12 + b.month - a.month


def _recurs(candidate: dt.datetime, start: dt.datetime, rule: dict) -> bool:
    if candidate < start:
        return False
    try:
        interval = int(rule.get("INTERVAL", "1") or 1)
    except ValueError:
        raise ReminderError("invalid iCalendar recurrence interval")
    interval = max(1, min(365, interval))
    days = (candidate.date() - start.date()).days
    freq = rule.get("FREQ", "").upper()
    if freq == "DAILY":
        return days % interval == 0
    if freq == "WEEKLY":
        allowed = {_BYDAY[item[-2:]] for item in rule.get("BYDAY", "").split(",")
                   if item[-2:] in _BYDAY} or {start.weekday()}
        return (days // 7) % interval == 0 and candidate.weekday() in allowed
    if freq == "MONTHLY":
        month_days = {int(item) for item in rule.get("BYMONTHDAY", "").split(",")
                      if re.fullmatch(r"\d{1,2}", item)} or {start.day}
        return _month_delta(start, candidate) % interval == 0 and candidate.day in month_days
    return False


def _expand(component: dict, *, now: float, horizon_days: int) -> list[dict]:
    start_params, start_value = _first(component, "DTSTART")
    if not start_value:
        return []
    start, all_day = _parse_datetime(start_value, start_params)
    end_params, end_value = _first(component, "DTEND")
    duration_value = _first(component, "DURATION")[1]
    if end_value:
        end, _ = _parse_datetime(end_value, end_params, start.tzinfo)
        length = max(60.0, end.timestamp() - start.timestamp())
    else:
        length = _duration(duration_value) or (86400.0 if all_day else 3600.0)
    uid = _first(component, "UID")[1].strip() or "%s:%s" % (start_value, _first(component, "SUMMARY")[1])
    series_id = _hash_id("series_", uid)
    title = _text(_first(component, "SUMMARY", "Untitled meeting")[1], 200).strip() or "Untitled meeting"
    description = _text(_first(component, "DESCRIPTION")[1], 20_000)
    location = _text(_first(component, "LOCATION")[1], 1_000)
    raw_url = _first(component, "URL")[1]
    join_url = _meeting_url(raw_url, location, description)
    status = _first(component, "STATUS")[1].strip().upper()
    recurrence_params, recurrence_value = _first(component, "RECURRENCE-ID")
    recurrence_at = None
    if recurrence_value:
        recurrence_at = _parse_datetime(recurrence_value, recurrence_params, start.tzinfo)[0].timestamp()
    base = {
        "series_id": series_id,
        "calendar_uid": _text(uid, 500),
        "title": title,
        "agenda": description,
        "location": location,
        "join_url": join_url,
        "template": _infer_template(title),
        "all_day": all_day,
        "cancelled": status == "CANCELLED",
        "recurrence_at": recurrence_at,
    }
    rule = _rrule(_first(component, "RRULE")[1])
    if not rule or recurrence_at is not None:
        return [dict(base, start_at=start.timestamp(), end_at=start.timestamp() + length)]
    if rule.get("FREQ", "").upper() not in {"DAILY", "WEEKLY", "MONTHLY"}:
        # Keep the first occurrence for unsupported YEARLY or exotic rules instead of inventing a
        # recurrence expansion that may notify at the wrong time.
        return [dict(base, start_at=start.timestamp(), end_at=start.timestamp() + length)]
    window_start = dt.datetime.fromtimestamp(now - 31 * 86400, start.tzinfo)
    window_end = dt.datetime.fromtimestamp(now + max(30, min(730, horizon_days)) * 86400,
                                           start.tzinfo)
    until = None
    if rule.get("UNTIL"):
        until = _parse_datetime(rule["UNTIL"], {}, start.tzinfo)[0]
        if (until.tzinfo is None) != (start.tzinfo is None):
            until = dt.datetime.fromtimestamp(until.timestamp(), start.tzinfo)
    try:
        count_limit = int(rule.get("COUNT", "0") or 0)
    except ValueError:
        raise ReminderError("invalid iCalendar recurrence count")
    count_limit = max(0, min(MAX_EVENTS, count_limit))
    exdates = set()
    for params, values in component.get("EXDATE", []):
        for value in values.split(","):
            try:
                exdates.add(int(_parse_datetime(value, params, start.tzinfo)[0].timestamp()))
            except ReminderError:
                continue
    cursor = start
    if cursor < window_start and not count_limit:
        cursor = dt.datetime.combine(window_start.date(), start.timetz())
    seen = 0
    rows = []
    hard_end = min(window_end, until) if until else window_end
    iterations = 0
    while cursor <= hard_end and len(rows) < MAX_EVENTS and iterations < 100_000:
        iterations += 1
        if _recurs(cursor, start, rule):
            seen += 1
            if count_limit and seen > count_limit:
                break
            stamp = int(cursor.timestamp())
            if stamp not in exdates and cursor >= window_start:
                rows.append(dict(base, start_at=cursor.timestamp(), end_at=cursor.timestamp() + length))
        cursor += dt.timedelta(days=1)
    return rows


def parse_ics(raw, *, now=None, horizon_days=365) -> list[dict]:
    """Parse and expand a bounded iCalendar export into normalized local events."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_ICS_BYTES:
            raise ReminderError("calendar file exceeds the 2 MiB safety limit")
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raw = raw.decode("latin-1")
    raw = str(raw or "")
    if len(raw.encode("utf-8")) > MAX_ICS_BYTES:
        raise ReminderError("calendar file exceeds the 2 MiB safety limit")
    if "BEGIN:VCALENDAR" not in raw.upper():
        raise ReminderError("the selected file is not an iCalendar (.ics) export")
    blocks = []
    active = None
    for line in _unfold_ical(raw):
        marker = line.strip().upper()
        if marker == "BEGIN:VEVENT":
            active = []
        elif marker == "END:VEVENT" and active is not None:
            blocks.append(_component(active))
            active = None
        elif active is not None:
            active.append(line)
    now = _finite(now, time.time())
    expanded = []
    overrides = {}
    for block in blocks:
        rows = _expand(block, now=now, horizon_days=horizon_days)
        for row in rows:
            key = (row["series_id"], int(row.get("recurrence_at") or row["start_at"]))
            if row.get("recurrence_at") is not None:
                overrides[key] = row
            else:
                expanded.append(row)
    normalized = {}
    for row in expanded:
        original_key = (row["series_id"], int(row["start_at"]))
        row = overrides.pop(original_key, row)
        if row.get("cancelled"):
            continue
        event_id = _hash_id("evt_", row["series_id"], int(row["start_at"]))
        normalized[event_id] = dict(row, id=event_id, source="ics")
    for row in overrides.values():
        if not row.get("cancelled"):
            event_id = _hash_id("evt_", row["series_id"], int(row["start_at"]))
            normalized[event_id] = dict(row, id=event_id, source="ics")
    rows = sorted(normalized.values(), key=lambda item: item["start_at"])
    if len(rows) > MAX_EVENTS:
        raise ReminderError("calendar expands to more than %d events" % MAX_EVENTS)
    return rows


class ReminderStore:
    """Crash-safe schedule, reminder preference, and delivery-deduplication store."""

    def __init__(self, path: str | None = None):
        state = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
        self.path = os.path.abspath(path or os.path.join(state, "meeting-reminders.json"))
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    @staticmethod
    def defaults() -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "preferences": {
                "enabled": True,
                "lead_minutes": 5,
                "end_grace_minutes": 3,
                "hide_titles": True,
                # Native delivery is explicit opt-in.  Browser/in-app reminders keep working when
                # it is false; enabling it lets the long-lived Collie host deliver after this page
                # closes without widening recording consent.
                "native_enabled": False,
                "muted_until": 0.0,
                "sensitive_terms": list(DEFAULT_SENSITIVE_TERMS),
            },
            "events": {},
            "series": {},
            "notifications": {},
            "snoozes": {},
            "engaged": {},
        }

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                value = json.load(handle, parse_constant=lambda item: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number: %s" % item)))
        except FileNotFoundError:
            return self.defaults()
        except (OSError, ValueError, TypeError) as exc:
            raise ReminderError("meeting reminder state is unreadable: %s" % exc)
        default = self.defaults()
        if not isinstance(value, dict):
            raise ReminderError("meeting reminder state is invalid")
        for key in ("events", "series", "notifications", "snoozes", "engaged"):
            if isinstance(value.get(key), dict):
                default[key] = value[key]
        if isinstance(value.get("preferences"), dict):
            default["preferences"].update(value["preferences"])
        return default

    def _write(self, value: dict) -> None:
        tmp = "%s.%d.%s.tmp" % (self.path, os.getpid(), os.urandom(4).hex())
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass

    @staticmethod
    def _public_event(event: dict, state: dict) -> dict:
        row = {key: event.get(key) for key in (
            "id", "series_id", "title", "agenda", "location", "join_url", "template",
            "start_at", "end_at", "all_day", "source")}
        series = state["series"].get(event.get("series_id"), {})
        if series.get("template") in _TEMPLATES:
            row["template"] = series["template"]
        row["language"] = _text(series.get("language"), 16)
        row["series_reminders"] = series.get("remind", True) is not False
        terms = state["preferences"].get("sensitive_terms") or []
        haystack = "\n".join(str(event.get(key) or "") for key in
                             ("title", "agenda", "location")).casefold()
        row["sensitive"] = any(str(term).strip().casefold() in haystack for term in terms
                               if str(term).strip())
        return row

    def snapshot(self, *, start_at=None, end_at=None) -> dict:
        now = time.time()
        start_at = _finite(start_at, now - 86400)
        end_at = _finite(end_at, now + 31 * 86400)
        if end_at < start_at or end_at - start_at > 370 * 86400:
            raise ReminderError("invalid meeting schedule window")
        with _LOCK:
            state = self._load()
            rows = [self._public_event(event, state) for event in state["events"].values()
                    if _finite(event.get("end_at")) >= start_at and
                    _finite(event.get("start_at")) <= end_at]
            rows.sort(key=lambda row: _finite(row.get("start_at")))
            return {"preferences": dict(state["preferences"]), "events": rows}

    def get_event(self, event_id: str) -> dict:
        if not _EVENT_RE.fullmatch(str(event_id or "")):
            raise ReminderError("invalid scheduled meeting id")
        with _LOCK:
            state = self._load()
            event = state["events"].get(event_id)
            if not event:
                raise ReminderError("scheduled meeting not found")
            return self._public_event(event, state)

    def import_ics(self, raw, *, now=None, horizon_days=365) -> dict:
        rows = parse_ics(raw, now=now, horizon_days=horizon_days)
        imported_at = time.time()
        with _LOCK:
            state = self._load()
            imported_series = {row["series_id"] for row in rows}
            state["events"] = {key: value for key, value in state["events"].items()
                               if value.get("source") != "ics" or
                               value.get("series_id") not in imported_series}
            for row in rows:
                row["imported_at"] = imported_at
                state["events"][row["id"]] = row
                state["series"].setdefault(row["series_id"], {})
            self._prune(state, now=_finite(now, imported_at))
            self._write(state)
        return {"ok": True, "imported": len(rows), "series": len(imported_series)}

    def save_event(self, *, event_id="", title="", start_at=0, end_at=0, agenda="",
                   location="", join_url="", template="general") -> dict:
        start_at = _finite(start_at)
        end_at = _finite(end_at)
        if start_at <= 0:
            raise ReminderError("meeting start time is required")
        if end_at <= start_at:
            raise ReminderError("meeting end time must be after its start")
        if end_at - start_at > 7 * 86400:
            raise ReminderError("meeting duration cannot exceed seven days")
        template = str(template or "general").strip().lower()
        if template not in _TEMPLATES:
            raise ReminderError("unsupported meeting template")
        safe_join_url = _safe_https_url(join_url)
        if str(join_url or "").strip() and not safe_join_url:
            raise ReminderError("meeting link must be a valid HTTPS URL")
        with _LOCK:
            state = self._load()
            if event_id:
                if not _EVENT_RE.fullmatch(str(event_id)) or event_id not in state["events"]:
                    raise ReminderError("scheduled meeting not found")
                old = state["events"][event_id]
                series_id = old["series_id"]
            else:
                series_id = _hash_id("series_", "manual", os.urandom(16).hex())
                event_id = _hash_id("evt_", series_id, int(start_at))
            row = {
                "id": event_id,
                "series_id": series_id,
                "calendar_uid": "",
                "title": _text(title, 200).strip() or "Untitled meeting",
                "agenda": _text(agenda, 20_000),
                "location": _text(location, 1_000),
                "join_url": safe_join_url,
                "template": template,
                "start_at": start_at,
                "end_at": end_at,
                "all_day": False,
                "source": "manual",
                "updated_at": time.time(),
            }
            state["events"][event_id] = row
            state["series"].setdefault(series_id, {})
            self._write(state)
            return self._public_event(row, state)

    def delete_event(self, event_id: str) -> bool:
        if not _EVENT_RE.fullmatch(str(event_id or "")):
            raise ReminderError("invalid scheduled meeting id")
        with _LOCK:
            state = self._load()
            existed = state["events"].pop(event_id, None) is not None
            for bucket in ("notifications", "snoozes", "engaged"):
                for key in list(state[bucket]):
                    if key == event_id or key.startswith(event_id + ":"):
                        state[bucket].pop(key, None)
            self._write(state)
            return existed

    def update_preferences(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ReminderError("expected reminder preferences")
        with _LOCK:
            state = self._load()
            prefs = state["preferences"]
            if "enabled" in values:
                prefs["enabled"] = values["enabled"] is True
            if "hide_titles" in values:
                prefs["hide_titles"] = values["hide_titles"] is True
            if "native_enabled" in values:
                prefs["native_enabled"] = values["native_enabled"] is True
            if "lead_minutes" in values:
                prefs["lead_minutes"] = max(0, min(120, int(_finite(values["lead_minutes"], 5))))
            if "end_grace_minutes" in values:
                prefs["end_grace_minutes"] = max(
                    0, min(60, int(_finite(values["end_grace_minutes"], 3))))
            if "muted_until" in values:
                prefs["muted_until"] = max(0.0, _finite(values["muted_until"]))
            if "sensitive_terms" in values:
                terms = values["sensitive_terms"]
                if not isinstance(terms, list):
                    raise ReminderError("sensitive terms must be a list")
                cleaned = []
                for item in terms[:50]:
                    item = _text(item, 80).strip()
                    if item and item.casefold() not in {prior.casefold() for prior in cleaned}:
                        cleaned.append(item)
                prefs["sensitive_terms"] = cleaned
            self._write(state)
            return dict(prefs)

    def update_series(self, series_id: str, *, remind=None, template=None, language=None) -> dict:
        if not _SERIES_RE.fullmatch(str(series_id or "")):
            raise ReminderError("invalid meeting series id")
        with _LOCK:
            state = self._load()
            if not any(event.get("series_id") == series_id for event in state["events"].values()):
                raise ReminderError("meeting series not found")
            value = state["series"].setdefault(series_id, {})
            if remind is not None:
                value["remind"] = remind is True
            if template is not None:
                template = str(template).strip().lower()
                if template not in _TEMPLATES:
                    raise ReminderError("unsupported meeting template")
                value["template"] = template
            if language is not None:
                value["language"] = _text(language, 16).strip().lower()
            self._write(state)
            return dict(value)

    def mark_engaged(self, event_id: str, *, kind="recording") -> None:
        if not _EVENT_RE.fullmatch(str(event_id or "")):
            raise ReminderError("invalid scheduled meeting id")
        with _LOCK:
            state = self._load()
            if event_id not in state["events"]:
                raise ReminderError("scheduled meeting not found")
            state["engaged"][event_id] = {"kind": _text(kind, 30), "at": time.time()}
            self._write(state)

    def claim_due(self, *, now=None, limit=10, channel="browser") -> list[dict]:
        """Atomically claim due prompts so multiple Collie tabs do not duplicate notifications."""
        now = _finite(now, time.time())
        limit = max(1, min(50, int(limit or 10)))
        channel = str(channel or "browser").strip().lower()
        if channel not in {"browser", "native"}:
            raise ReminderError("invalid reminder delivery channel")
        with _LOCK:
            state = self._load()
            prefs = state["preferences"]
            if not prefs.get("enabled", True) or _finite(prefs.get("muted_until")) > now:
                return []
            lead = max(0, min(120, int(prefs.get("lead_minutes", 5)))) * 60
            grace = max(0, min(60, int(prefs.get("end_grace_minutes", 3)))) * 60
            candidates = []
            for event in state["events"].values():
                row = self._public_event(event, state)
                if row["sensitive"] or row["series_reminders"] is False or row.get("all_day"):
                    continue
                start = _finite(row.get("start_at"))
                end = _finite(row.get("end_at"))
                phase = ""
                if start - lead <= now < start and lead:
                    phase = "prepare"
                elif start <= now < min(end, start + 30 * 60):
                    phase = "start"
                elif event["id"] in state["engaged"] and end + grace <= now < end + grace + 3600:
                    phase = "end"
                if not phase:
                    continue
                public_key = "%s:%s" % (event["id"], phase)
                key = public_key if channel == "browser" else public_key + ":native"
                if (key in state["notifications"] or
                        _finite(state["snoozes"].get(public_key)) > now):
                    continue
                candidates.append((start, phase, row, key, public_key))
            candidates.sort(key=lambda item: (item[0], item[1]))
            claimed = []
            for _, phase, row, key, public_key in candidates[:limit]:
                state["notifications"][key] = now
                minutes = max(1, int(round((_finite(row["start_at"]) - now) / 60)))
                if phase == "prepare":
                    browser_title = "Meeting starts in %d minute%s" % (minutes, "" if minutes == 1 else "s")
                    message = "%s starts soon." % row["title"]
                elif phase == "start":
                    browser_title = "Meeting is starting"
                    message = "%s is scheduled now." % row["title"]
                else:
                    browser_title = "Meeting may have ended"
                    message = "Review and stop the note for %s." % row["title"]
                claimed.append({
                    "id": public_key,
                    "channel": channel,
                    "phase": phase,
                    "event": row,
                    "message": message,
                    "notification_title": browser_title,
                    "notification_body": ("Open Collie Meeting Notes to respond."
                                          if prefs.get("hide_titles", True) else message),
                })
            if claimed:
                self._prune(state, now=now)
                self._write(state)
            return claimed

    def reminder_action(self, event_id: str, phase: str, action: str, *, minutes=5) -> dict:
        if not _EVENT_RE.fullmatch(str(event_id or "")):
            raise ReminderError("invalid scheduled meeting id")
        phase = str(phase or "").lower()
        if phase not in {"prepare", "start", "end"}:
            raise ReminderError("invalid reminder phase")
        key = "%s:%s" % (event_id, phase)
        with _LOCK:
            state = self._load()
            event = state["events"].get(event_id)
            if not event:
                raise ReminderError("scheduled meeting not found")
            if action == "snooze":
                delay = max(1, min(120, int(_finite(minutes, 5))))
                state["notifications"].pop(key, None)
                state["notifications"].pop(key + ":native", None)
                state["snoozes"][key] = time.time() + delay * 60
            elif action == "mute_today":
                local = dt.datetime.now().astimezone()
                tomorrow = (local + dt.timedelta(days=1)).date()
                midnight = dt.datetime.combine(tomorrow, dt.time.min, local.tzinfo)
                state["preferences"]["muted_until"] = midnight.timestamp()
            elif action == "series_off":
                state["series"].setdefault(event["series_id"], {})["remind"] = False
            elif action == "engage":
                state["engaged"][event_id] = {"kind": "prepared", "at": time.time()}
            elif action != "dismiss":
                raise ReminderError("unknown reminder action")
            self._write(state)
            return {"ok": True, "action": action}

    @staticmethod
    def _prune(state: dict, *, now: float) -> None:
        cutoff = now - 45 * 86400
        future_limit = now + 730 * 86400
        state["events"] = {key: value for key, value in state["events"].items()
                           if _finite(value.get("end_at")) >= cutoff and
                           _finite(value.get("start_at")) <= future_limit}
        valid = set(state["events"])
        for bucket in ("notifications", "snoozes"):
            state[bucket] = {key: value for key, value in state[bucket].items()
                             if key.split(":", 1)[0] in valid and _finite(value) >= cutoff}
        state["engaged"] = {key: value for key, value in state["engaged"].items()
                            if key in valid}
