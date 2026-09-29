"""Gmail + Calendar reads and reply drafts, against recorded-shape Google responses.

The shapes below are what the Gmail v1 and Calendar v3 REST APIs return (field names, the
base64url body encoding, labelIds, internalDate as a string of milliseconds, all-day events as
`date` rather than `dateTime`). Nothing here touches the network.
"""
import base64
import datetime as dt
import email
import email.policy
import inspect
import json
import re
import urllib.parse

import pytest

from harness import google_connect as gc
from _google_fakes import ACCESS, ALL, CAL, READ, connected, make_env, refresh_ok

THREAD = "1661b846250a5804"          # = 1612772752984004612, the published encoder vector
DRAFT = "r7186792049594056342"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    connected()
    refresh_ok(e)
    yield e
    gc._reset_cache()


def b64u(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def meta(mid, thread, labels, frm, subject, snippet, when_ms):
    return {"id": mid, "threadId": thread, "labelIds": labels, "snippet": snippet,
            "historyId": "987654", "internalDate": str(when_ms), "sizeEstimate": 5120,
            "payload": {"partId": "", "mimeType": "multipart/alternative", "filename": "",
                        "headers": [{"name": "From", "value": frm},
                                    {"name": "To", "value": "Owner <owner@example.com>"},
                                    {"name": "Subject", "value": subject},
                                    {"name": "Date", "value": "Mon, 28 Sep 2026 08:15:00 -0700"}]}}


# ------------------------------------------------------------------------ gmail_search

def test_gmail_search_lists_then_reads_metadata(env):
    fake = env["fake"]
    fake.on("GET", gc.GMAIL_API + "/users/me/messages?", {
        "messages": [{"id": "18f1a2b3c4d5e6f7", "threadId": "18f1a2b3c4d5e6f0"},
                     {"id": "18f1a2b3c4d5e6f8", "threadId": "18f1a2b3c4d5e6f8"}],
        "resultSizeEstimate": 2})
    fake.on("GET", gc.GMAIL_API + "/users/me/messages/18f1a2b3c4d5e6f7", meta(
        "18f1a2b3c4d5e6f7", "18f1a2b3c4d5e6f0", ["UNREAD", "IMPORTANT", "INBOX"],
        "Ana Díaz <ana@example.com>", "Thursday's call", "Can we move Thursday&#39;s call?",
        1790690000000))
    fake.on("GET", gc.GMAIL_API + "/users/me/messages/18f1a2b3c4d5e6f8", meta(
        "18f1a2b3c4d5e6f8", "18f1a2b3c4d5e6f8", ["INBOX"], "Bills <no-reply@bank.example>",
        "Statement ready", "Your statement &amp; balance", 1790680000000))

    rows = gc.gmail_search("in:inbox newer_than:2d", 3)

    listing = urllib.parse.urlsplit(fake.urls(gc.GMAIL_API + "/users/me/messages?")[0])
    q = dict(urllib.parse.parse_qsl(listing.query))
    assert q["q"] == "in:inbox newer_than:2d" and q["maxResults"] == "3"
    first = rows[0]
    assert set(first) >= {"id", "thread_id", "from", "to", "subject", "date", "snippet",
                          "labels", "unread"}
    assert first["id"] == "18f1a2b3c4d5e6f7" and first["thread_id"] == "18f1a2b3c4d5e6f0"
    assert first["from"] == "Ana Díaz <ana@example.com>" and first["subject"] == "Thursday's call"
    assert first["snippet"] == "Can we move Thursday's call?"        # entities decoded
    assert first["unread"] is True and "INBOX" in first["labels"]
    assert first["date"] == "Mon, 28 Sep 2026 08:15:00 -0700"
    assert rows[1]["unread"] is False and rows[1]["snippet"] == "Your statement & balance"
    detail = [u for u in fake.urls(gc.GMAIL_API + "/users/me/messages/")]
    assert all("format=metadata" in u for u in detail)
    for call in fake.calls:
        if call["url"].startswith(gc.GMAIL_API):
            assert call["headers"]["Authorization"] == "Bearer " + ACCESS
            assert call["timeout"] <= 30


def test_gmail_search_bounds_the_count_and_query(env):
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/messages?", {"resultSizeEstimate": 0})
    assert gc.gmail_search("x", 5000) == []
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(env["fake"].urls(gc.GMAIL_API)[0]).query))
    assert q["maxResults"] == str(gc.MAX_SEARCH_RESULTS)
    with pytest.raises(ValueError):
        gc.gmail_search("x" * 5000, 3)


def test_missing_scope_is_refused_before_any_request(env):
    gc._reset_cache()
    connected(scopes=CAL)
    with pytest.raises(gc.MissingScope) as info:
        gc.gmail_search("in:inbox", 3)
    assert "collie google connect" in str(info.value)
    assert not env["fake"].urls(gc.GMAIL_API)


def test_a_dead_sign_in_stops_every_call_before_any_request(env):
    env["fake"].on("POST", gc.TOKEN_URI, {"error": "invalid_grant"}, status=400)
    with pytest.raises(gc.NeedsReconnect):
        gc.gmail_search("in:inbox", 3)
    before = len(env["fake"].calls)
    for call in (lambda: gc.gmail_search("in:inbox", 3), lambda: gc.gmail_thread(THREAD),
                 lambda: gc.calendar_events(),
                 lambda: gc.gmail_create_draft(THREAD, "a@b.c", "s", "b", in_reply_to="<a@b>")):
        with pytest.raises(gc.NeedsReconnect) as info:
            call()
        assert "collie google connect" in str(info.value)
    assert len(env["fake"].calls) == before


def test_google_saying_insufficient_scope_is_missing_scope(env):
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/messages?", {
        "error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                  "status": "PERMISSION_DENIED",
                  "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}, status=403)
    with pytest.raises(gc.MissingScope):
        gc.gmail_search("in:inbox", 3)


def test_a_401_refreshes_once_and_retries(env):
    answers = iter([(401, b'{"error": {"code": 401}}'),
                    (200, json.dumps({"resultSizeEstimate": 0}).encode())])
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/messages?",
                   fn=lambda *a: next(answers))
    assert gc.gmail_search("in:inbox", 3) == []
    assert len(env["fake"].urls(gc.TOKEN_URI)) == 2


def test_api_errors_are_plain_and_carry_no_token(env):
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/messages?", {
        "error": {"code": 500, "message": "Backend Error"}}, status=500)
    with pytest.raises(gc.GoogleAPIError) as info:
        gc.gmail_search("in:inbox", 3)
    assert info.value.status == 500 and "Backend Error" in str(info.value)
    assert ACCESS not in str(info.value)


def test_ids_are_validated_so_they_cannot_rewrite_the_path(env):
    for bad in ("../drafts", "abc/def", "a b", "", "x" * 300):
        with pytest.raises(ValueError):
            gc.gmail_thread(bad)
    assert not env["fake"].urls(gc.GMAIL_API)


# ------------------------------------------------------------------------ gmail_thread

def _thread_fixture():
    plain = "Hi Daming,\r\n\r\nCan we move Thursday's call to 3pm?\r\n\r\nAna"
    html = ("<html><head><style>p{color:red}</style><script>alert(1)</script></head>"
            "<body><p>Sure &mdash; 3pm works.</p><div>Talk soon,<br>Daming</div></body></html>")
    return {"id": THREAD, "historyId": "1", "messages": [
        {"id": "m1", "threadId": THREAD, "labelIds": ["INBOX", "UNREAD"], "snippet": "Can we",
         "internalDate": "1790690000000",
         "payload": {"mimeType": "multipart/alternative", "headers": [
             {"name": "From", "value": "Ana <ana@example.com>"},
             {"name": "To", "value": "owner@example.com"},
             {"name": "Subject", "value": "Thursday's call"},
             {"name": "Date", "value": "Mon, 28 Sep 2026 08:15:00 -0700"},
             {"name": "Message-ID", "value": "<CAB1@mail.example.com>"}],
             "parts": [
                 {"partId": "0", "mimeType": "text/plain", "filename": "",
                  "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
                  "body": {"size": len(plain), "data": b64u(plain)}},
                 {"partId": "1", "mimeType": "text/html", "filename": "",
                  "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
                  "body": {"size": 10, "data": b64u("<p>ignored when plain exists</p>")}},
                 {"partId": "2", "mimeType": "application/pdf", "filename": "agenda.pdf",
                  "body": {"attachmentId": "ANGjdJ8", "size": 20480}}]}},
        {"id": "m2", "threadId": THREAD, "labelIds": ["SENT"], "snippet": "Sure",
         "internalDate": "1790693600000",
         "payload": {"mimeType": "text/html", "headers": [
             {"name": "From", "value": "Owner <owner@example.com>"},
             {"name": "To", "value": "Ana <ana@example.com>"},
             {"name": "Subject", "value": "Re: Thursday's call"},
             {"name": "Date", "value": "Mon, 28 Sep 2026 09:15:00 -0700"},
             {"name": "Message-ID", "value": "<CAB2@mail.gmail.com>"},
             {"name": "In-Reply-To", "value": "<CAB1@mail.example.com>"},
             {"name": "References", "value": "<CAB1@mail.example.com>"}],
             "body": {"size": len(html), "data": b64u(html)}}}]}


def test_gmail_thread_decodes_plain_bodies_and_strips_html(env):
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/threads/" + THREAD, _thread_fixture())
    th = gc.gmail_thread(THREAD)
    assert th["thread_id"] == THREAD and th["subject"] == "Thursday's call"
    first, second = th["messages"]
    assert first["body"] == "Hi Daming,\n\nCan we move Thursday's call to 3pm?\n\nAna"
    assert first["rfc_message_id"] == "<CAB1@mail.example.com>"
    assert first["unread"] is True and first["body_truncated"] is False
    assert "Sure — 3pm works." in second["body"] and "Talk soon,\nDaming" in second["body"]
    assert "<" not in second["body"] and "alert" not in second["body"] and "color" not in second["body"]
    assert second["references"] == "<CAB1@mail.example.com>"
    assert second["in_reply_to"] == "<CAB1@mail.example.com>"
    assert "format=full" in env["fake"].urls(gc.GMAIL_API + "/users/me/threads/")[0]


def test_gmail_thread_caps_each_body(env):
    long = "word " * 5000
    fx = _thread_fixture()
    fx["messages"][0]["payload"]["parts"][0]["body"]["data"] = b64u(long)
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/threads/" + THREAD, fx)
    first = gc.gmail_thread(THREAD, max_body_chars=1000)["messages"][0]
    assert len(first["body"]) <= 1000 and first["body_truncated"] is True


def test_html_fallback_reads_a_bounded_amount_of_markup():
    # A sender controls this HTML. Past MAX_HTML_CHARS (after dropping style/script/comments)
    # nothing more is read, and the body says it was cut.
    head = "<p>start</p><style>" + "p{color:red}" * 20000 + "</style><!--" + "c" * 50000 + "-->"
    markup = head + "<p>kept after the style block</p><div>" + "x " * 60000 + "</div><p>TAIL</p>"
    text, complete = gc._html_text(markup)
    assert "start" in text and "kept after the style block" in text
    assert "TAIL" not in text and complete is False
    body, truncated = gc._message_body({"mimeType": "text/html", "body": {"data": b64u(markup)}},
                                       100_000)
    assert truncated is True and "TAIL" not in body


def test_html_fallback_stops_at_its_time_budget():
    text, complete = gc._html_text("<p>para</p>" * 5000, budget=0)
    assert complete is False and len(text) < 100


@pytest.mark.parametrize("attack", ["<a ", "<!--", "</", "<!", "<style", "<script>x<", "<a b='",
                                    "&#", "<![CDATA["])
def test_html_fallback_is_linear_on_malformed_markup(attack):
    # CPython's own CVE-2025-6069 regression inputs take seconds in html.parser on the 3.12.10
    # that the Windows installer embeds; the fallback must not depend on that parser at all.
    import time
    start = time.perf_counter()
    gc._html_text(attack * 20000)
    assert time.perf_counter() - start < 1.0
    source = inspect.getsource(gc)
    assert not re.search(r"(?m)^\s*(import html\.parser|from html(\.parser)? import)", source)
    assert "HTMLParser" not in source


def test_body_decoding_tolerates_missing_padding_and_other_charsets():
    raw = "café".encode("latin-1")
    data = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    part = {"mimeType": "text/plain", "headers": [
        {"name": "Content-Type", "value": 'text/plain; charset="ISO-8859-1"'}],
        "body": {"data": data}}
    assert gc._message_body(part, 100)[0] == "café"


# ------------------------------------------------------------------------ drafts

def _drafts(env):
    captured = {}

    def create(method, url, headers, data):
        captured["body"] = json.loads(data.decode())
        captured["headers"] = headers
        return 200, json.dumps({"id": DRAFT, "message": {
            "id": "19a0b1c2d3e4f5a6", "threadId": THREAD, "labelIds": ["DRAFT"]}}).encode()
    env["fake"].on("POST", gc.GMAIL_API + "/users/me/drafts", fn=create)
    return captured


def _decoded(captured):
    raw = captured["body"]["message"]["raw"]
    assert re.fullmatch(r"[A-Za-z0-9_\-]+=*", raw), "raw must be base64url"
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)


def test_draft_is_an_in_thread_reply_with_rfc_2822_headers(env):
    captured = _drafts(env)
    out = gc.gmail_create_draft(THREAD, "Ana <ana@example.com>", "Thursday's call",
                                "3pm works — see you then.\n\nDaming",
                                in_reply_to="<CAB2@mail.gmail.com>",
                                references="<CAB1@mail.example.com>")
    body = captured["body"]
    assert body["message"]["threadId"] == THREAD
    msg = _decoded(captured)
    assert msg["To"] == "Ana <ana@example.com>"
    assert msg["Subject"] == "Re: Thursday's call"
    assert msg["In-Reply-To"] == "<CAB2@mail.gmail.com>"
    assert msg["References"].split() == ["<CAB1@mail.example.com>", "<CAB2@mail.gmail.com>"]
    # RFC 5322 lines end in CRLF on the wire
    assert msg.get_content().replace("\r\n", "\n").strip() == "3pm works — see you then.\n\nDaming"
    assert msg["From"] is None and msg["Bcc"] is None
    assert out["draft_id"] == DRAFT and out["message_id"] == "19a0b1c2d3e4f5a6"
    assert out["thread_id"] == THREAD
    assert out["open_url"].startswith("https://mail.google.com/mail/?authuser=owner@example.com#all?compose=")
    assert captured["headers"]["Content-Type"].startswith("application/json")


def test_draft_keeps_an_existing_re_prefix_and_dedupes_references(env):
    captured = _drafts(env)
    gc.gmail_create_draft(THREAD, "ana@example.com", "RE: Thursday's call", "ok",
                          in_reply_to="CAB2@mail.gmail.com",
                          references="<CAB1@mail.example.com> <CAB2@mail.gmail.com>")
    msg = _decoded(captured)
    assert msg["Subject"] == "RE: Thursday's call"
    assert msg["In-Reply-To"] == "<CAB2@mail.gmail.com>"
    assert msg["References"].split() == ["<CAB1@mail.example.com>", "<CAB2@mail.gmail.com>"]


def test_draft_looks_up_the_thread_when_reply_headers_are_not_given(env):
    captured = _drafts(env)
    env["fake"].on("GET", gc.GMAIL_API + "/users/me/threads/" + THREAD, _thread_fixture())
    gc.gmail_create_draft(THREAD, "ana@example.com", "Thursday's call", "ok")
    msg = _decoded(captured)
    assert msg["In-Reply-To"] == "<CAB2@mail.gmail.com>"
    assert msg["References"].split() == ["<CAB1@mail.example.com>", "<CAB2@mail.gmail.com>"]


@pytest.mark.parametrize("field", ["to", "subject", "in_reply_to", "references"])
def test_draft_refuses_header_injection_before_any_request(env, field):
    args = {"thread_id": THREAD, "to": "ana@example.com", "subject": "hi", "body": "b",
            "in_reply_to": "<a@b>", "references": ""}
    args[field] = args[field] + "\r\nBcc: victim@example.com"
    _drafts(env)
    with pytest.raises(ValueError):
        gc.gmail_create_draft(**args)
    assert not env["fake"].urls(gc.GMAIL_API + "/users/me/drafts")


def test_draft_needs_the_compose_scope(env):
    gc._reset_cache()
    connected(scopes=READ + " " + CAL)
    _drafts(env)
    with pytest.raises(gc.MissingScope):
        gc.gmail_create_draft(THREAD, "ana@example.com", "hi", "b", in_reply_to="<a@b>")
    assert not env["fake"].urls(gc.GMAIL_API + "/users/me/drafts")


def test_the_module_can_never_send_mail():
    source = inspect.getsource(gc)
    for pattern in (r"messages\s*[/.]\s*send", r"drafts\s*[/.]\s*send", r"/send\b",
                    r"\bgmail\.send\b"):
        assert not re.search(pattern, source), pattern
    assert "https://www.googleapis.com/auth/gmail.send" not in source
    assert "https://mail.google.com/\"" not in source and "https://mail.google.com/'" not in source


# ------------------------------------------------------------------------ draft open URL

def test_draft_open_url_matches_the_published_gmail_compose_encoding():
    # Vectors from GoodMeasuresLLC/gmail_compose_encoder spec/gmail_compose_encoder_spec.rb
    assert gc._compose_token("thread-f:1612772752984004612+msg-a:r7186792049594056342") == (
        "lLtBPXMfwdZJvWLlDhvxCqGbFRRnxnkhTqwPvxpBMCQbxPwdwWXVPRGQFfXwBlzFZRQLLvbX")
    assert gc._compose_token("thread-f:1612800019161438603+msg-a:r-2174453288762984534") == (
        "CqMvqmRFntSzZfVtfBmPPsWMVrdJWrfWKgnmmQnBpjMBHZDDbfzgzMVWSNMwqsjkqbXqVSvsngB")
    url = gc.draft_open_url(THREAD, DRAFT, "owner@example.com")
    assert url == ("https://mail.google.com/mail/?authuser=owner@example.com#all?compose="
                   "lLtBPXMfwdZJvWLlDhvxCqGbFRRnxnkhTqwPvxpBMCQbxPwdwWXVPRGQFfXwBlzFZRQLLvbX")
    assert gc.draft_open_url(THREAD, DRAFT, "").startswith("https://mail.google.com/mail/u/0/")


def test_draft_open_url_falls_back_to_the_thread_for_unknown_id_shapes():
    assert gc.draft_open_url(THREAD, "weird-id", "a@b.c") == (
        "https://mail.google.com/mail/?authuser=a@b.c#all/" + THREAD)


# ------------------------------------------------------------------------ calendar

def test_calendar_events_parse_timed_all_day_and_skip_cancelled(env):
    env["fake"].on("GET", gc.CALENDAR_API + "/calendars/primary/events", {
        "kind": "calendar#events", "timeZone": "America/Los_Angeles", "items": [
            {"id": "evt1", "status": "confirmed", "summary": "Standup", "location": "Zoom",
             "htmlLink": "https://www.google.com/calendar/event?eid=ZXZ0MQ",
             "start": {"dateTime": "2026-09-30T09:00:00-07:00", "timeZone": "America/Los_Angeles"},
             "end": {"dateTime": "2026-09-30T09:15:00-07:00", "timeZone": "America/Los_Angeles"},
             "attendees": [{"email": "a@example.com"}, {"email": "owner@example.com", "self": True},
                           {"email": "room@resource.calendar.google.com", "resource": True}]},
            {"id": "evt2", "status": "confirmed", "summary": "Offsite",
             "htmlLink": "https://www.google.com/calendar/event?eid=ZXZ0Mg",
             "start": {"date": "2026-10-01"}, "end": {"date": "2026-10-02"}},
            {"id": "evt3", "status": "cancelled"}]})
    start = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.timezone.utc)
    rows = gc.calendar_events(start, start + dt.timedelta(days=7), 20)
    assert [r["id"] for r in rows] == ["evt1", "evt2"]
    timed, allday = rows
    assert timed == {"id": "evt1", "summary": "Standup", "start": "2026-09-30T09:00:00-07:00",
                     "end": "2026-09-30T09:15:00-07:00", "all_day": False, "location": "Zoom",
                     "attendees_count": 2,
                     "html_link": "https://www.google.com/calendar/event?eid=ZXZ0MQ"}
    assert allday["all_day"] is True and allday["start"] == "2026-10-01"
    assert allday["attendees_count"] == 0 and allday["location"] == ""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        env["fake"].urls(gc.CALENDAR_API)[0]).query))
    assert q["singleEvents"] == "true" and q["orderBy"] == "startTime"
    assert q["timeMin"] == "2026-09-29T12:00:00+00:00"
    assert q["timeMax"] == "2026-10-06T12:00:00+00:00" and q["maxResults"] == "20"


def test_calendar_needs_its_scope_and_rejects_bad_times(env):
    with pytest.raises(ValueError):
        gc.calendar_events("yesterday", "tomorrow")
    gc._reset_cache()
    connected(scopes=READ)
    with pytest.raises(gc.MissingScope):
        gc.calendar_events()
    assert not env["fake"].urls(gc.CALENDAR_API)
