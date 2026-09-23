"""Wire-behaviour tests for the Twilio SMS/voice adapter.

What these protect: the request always going to this account's namespace on
``api.twilio.com`` and nowhere a response can redirect it, the sender always
being the configured number, an ambiguous POST never being reported as a clean
failure (which is how one text becomes two), a message body never becoming TwiML
verbs, and "the provider accepted it" never being printed as "they got it".

No socket is opened anywhere in this file: every request goes through an
injected opener that records the exact URL, method, headers and body.
"""
import io
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from harness import phone_transport as pt

ACCOUNT = "AC" + "0" * 32
OTHER_ACCOUNT = "AC" + "1" * 32
NUMBER = "+15550001111"
PEER = "+15550002222"
CONFIG = {"account_sid": ACCOUNT, "phone_number": NUMBER}
CREDS = {"auth_token": "s3cr3t-auth-token"}
BASE = "https://api.twilio.com/2010-04-01/Accounts/%s/" % ACCOUNT


def sid(prefix, n):
    return prefix + "%032x" % n


class Response:
    def __init__(self, payload):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, size=-1):
        return self.body[:size] if size and size > 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Stands in for ``urlopen``; answers in order and keeps every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeout = timeout
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)


class Provider:
    """A newest-first pager that answers like Twilio's Messages list.

    Pages are anchored by ``PageToken=PA<sid>`` the way the real API anchors
    them, so a message arriving mid-sweep does not shift the pages behind it —
    which is exactly the situation the cursor has to survive.
    """

    def __init__(self, rows, page_size=pt.PAGE_SIZE):
        self.rows = list(rows)              # newest first, as Twilio returns them
        self.page_size = page_size
        self.requests = []

    def arrives(self, row):
        self.rows.insert(0, row)            # a new message, newest-first

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        token = query.get("PageToken", [""])[0]
        start = 0
        if token.startswith("PA"):
            at = next((i for i, row in enumerate(self.rows)
                       if row.get("sid") == token[2:]), None)
            start = at + 1 if at is not None else 0
        page = self.rows[start:start + self.page_size]
        payload = {"messages": page, "next_page_uri": None}
        if page and start + self.page_size < len(self.rows):
            payload["next_page_uri"] = (
                "/2010-04-01/Accounts/%s/Messages.json?PageSize=%d&PageToken=PA%s"
                % (ACCOUNT, self.page_size, page[-1]["sid"]))
        return Response(payload)


def drain(provider, cursor=None, *, limit=pt.DEFAULT_LIMIT, rounds=60):
    """Poll to completion the way a host would, persisting only what it is given."""
    seen, polls = [], 0
    while polls < rounds:
        out = pt.poll(CONFIG, CREDS, cursor=cursor, limit=limit, opener=provider)
        assert len(out["messages"]) <= limit
        seen.extend(m["sid"] for m in out["messages"])
        cursor, polls = out["cursor"], polls + 1
        if not out["more"]:
            return seen, cursor, polls
    raise AssertionError("the poll never finished draining the backlog")


def http_error(code, payload=None, url=BASE):
    body = io.BytesIO(json.dumps(payload or {}).encode())
    return urllib.error.HTTPError(url, code, "error", {}, body)


def inbound(n, *, body="help please", direction="inbound", to=NUMBER, account=ACCOUNT, **kw):
    row = {"sid": sid("SM", n), "account_sid": account, "from": PEER, "to": to,
           "body": body, "direction": direction, "status": "received", "num_media": "0",
           "date_created": "Tue, 23 Sep 2026 10:0%d:00 +0000" % (n % 10)}
    row.update(kw)
    return row


def result(**kw):
    row = {"id": "r-1", "destination": PEER, "text": "the task finished"}
    row.update(kw)
    return row


# ----------------------------------------------------------------- configuration

def test_config_shape_is_enforced():
    cfg = pt.validate_config(CONFIG)
    assert cfg["timeout"] == pt.DEFAULT_TIMEOUT and pt.MIN_TIMEOUT <= cfg["timeout"] <= pt.MAX_TIMEOUT
    for bad in ({"account_sid": "AC123", "phone_number": NUMBER},
                {"account_sid": ACCOUNT, "phone_number": "5550001111"},
                {"account_sid": ACCOUNT, "phone_number": NUMBER, "timeout": 300},
                {"account_sid": ACCOUNT, "phone_number": NUMBER, "auth_token": "x"},
                {"account_sid": ACCOUNT, "phone_number": NUMBER, "base_url": "http://evil"}):
        with pytest.raises(pt.PhoneConfigError):
            pt.validate_config(bad)


def test_missing_token_never_reaches_a_request():
    opener = Opener()
    with pytest.raises(pt.PhoneConfigError):
        pt.send(CONFIG, {}, result(), opener=opener)
    assert opener.requests == []


# ------------------------------------------------------------- request encoding

def test_send_encodes_exactly_one_twilio_message_request():
    opener = Opener({"sid": sid("SM", 1), "account_sid": ACCOUNT, "status": "queued"})
    out = pt.send(CONFIG, CREDS, result(text="hello there"), opener=opener)
    request = opener.requests[0]
    assert request.full_url == BASE + "Messages.json"
    assert request.get_method() == "POST"
    assert request.data == b"From=%2B15550001111&To=%2B15550002222&Body=hello+there"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert request.get_header("Authorization") == (      # b64("<account sid>:<token>")
        "Basic QUMwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDpzM2NyM3QtYXV0aC10b2tlbg==")
    assert opener.timeout == pt.DEFAULT_TIMEOUT
    assert out["provider_message_id"] == sid("SM", 1) and out["status"] == "submitted"


def test_sender_is_always_the_configured_number():
    opener = Opener({"sid": sid("SM", 2), "status": "queued"})
    pt.send(CONFIG, CREDS, result(**{"from": "+19998887777", "sender": "+19998887777"}),
            opener=opener)
    assert b"From=%2B15550001111" in opener.requests[0].data


def test_destination_must_be_e164_and_is_checked_before_the_request():
    opener = Opener()
    for bad in ("5550002222", "+1555000222 2", "", None, "+15550002222; DROP"):
        with pytest.raises(pt.PhoneConfigError):
            pt.send(CONFIG, CREDS, result(destination=bad), opener=opener)
    assert opener.requests == []


def test_oversize_sms_fails_visibly_instead_of_truncating():
    opener = Opener()
    with pytest.raises(pt.PhoneConfigError) as exc:
        pt.send(CONFIG, CREDS, result(text="x" * (pt.MAX_SMS_CHARS + 1)), opener=opener)
    assert "nothing was truncated" in str(exc.value)
    assert opener.requests == []


# ---------------------------------------------------------------- probe & status

def test_probe_reads_capability_metadata_only():
    opener = Opener({"incoming_phone_numbers": [
        {"phone_number": NUMBER, "friendly_name": "collie", "account_sid": ACCOUNT,
         "capabilities": {"sms": True, "mms": False, "voice": True}}]})
    out = pt.probe(CONFIG, CREDS, opener=opener)
    assert opener.requests[0].get_method() == "GET"
    assert opener.requests[0].full_url.startswith(BASE + "IncomingPhoneNumbers.json?")
    assert "PhoneNumber=%2B15550001111" in opener.requests[0].full_url
    assert out["owned"] is True and out["sms"] is True and out["voice"] is True
    assert out["delivery_proof"] is False


def test_probe_refuses_a_number_this_account_does_not_own():
    opener = Opener({"incoming_phone_numbers": [{"phone_number": "+19998887777"}]})
    with pytest.raises(pt.PhoneRefused):
        pt.probe(CONFIG, CREDS, opener=opener)


def test_fetch_status_reports_delivery_only_when_the_provider_does():
    for status, delivered in (("queued", False), ("sent", False), ("delivered", True),
                              ("undelivered", False), ("failed", False)):
        opener = Opener({"sid": sid("SM", 3), "account_sid": ACCOUNT, "status": status,
                         "direction": "outbound-api", "error_code": None})
        out = pt.fetch_status(CONFIG, CREDS, sid("SM", 3), opener=opener)
        assert out["delivered"] is delivered and out["status"] == status
        assert opener.requests[0].full_url == BASE + "Messages/%s.json" % sid("SM", 3)


def test_a_completed_call_is_not_a_claim_that_anyone_listened():
    opener = Opener({"sid": sid("CA", 4), "account_sid": ACCOUNT, "status": "completed"})
    out = pt.fetch_status(CONFIG, CREDS, sid("CA", 4), opener=opener)
    assert opener.requests[0].full_url == BASE + "Calls/%s.json" % sid("CA", 4)
    assert out["kind"] == "call" and out["final"] is True and out["delivered"] is False
    with pytest.raises(pt.PhoneConfigError):
        pt.fetch_status(CONFIG, CREDS, "not-a-sid", opener=Opener())


# ---------------------------------------------------------- account & url safety

def test_records_from_another_account_are_discarded():
    opener = Opener({"sid": sid("SM", 5), "account_sid": OTHER_ACCOUNT, "status": "delivered"})
    with pytest.raises(pt.PhoneTransportError) as exc:
        pt.fetch_status(CONFIG, CREDS, sid("SM", 5), opener=opener)
    assert "another account" in str(exc.value)


def test_pagination_links_outside_this_account_are_refused():
    good = "/2010-04-01/Accounts/%s/Messages.json?Page=1" % ACCOUNT
    assert pt.next_page_url(pt.validate_config(CONFIG), good).startswith(BASE + "Messages.json")
    for bad in ("/2010-04-01/Accounts/%s/Messages.json?Page=1" % OTHER_ACCOUNT,
                "https://evil.example.com/2010-04-01/Accounts/%s/Messages.json" % ACCOUNT,
                "http://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % ACCOUNT,
                "/2010-04-01/Accounts/%s/../../Messages.json" % ACCOUNT,
                "/v1/Anything.json"):
        with pytest.raises(pt.PhoneTransportError):
            pt.next_page_url(pt.validate_config(CONFIG), bad)


def test_a_poisoned_next_page_link_stops_the_poll_loudly():
    page = {"messages": [inbound(1)],
            "next_page_uri": "/2010-04-01/Accounts/%s/Messages.json?Page=1" % OTHER_ACCOUNT}
    with pytest.raises(pt.PhoneTransportError):
        pt.poll(CONFIG, CREDS, opener=Opener(page))


def test_redirects_are_never_followed():
    assert pt._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil") is None
    assert any(isinstance(h, pt._NoRedirect) for h in pt._OPENER.handlers)
    with pytest.raises(pt.PhoneRefused):
        pt.poll(CONFIG, CREDS, opener=Opener(http_error(302)))


# -------------------------------------------------------------------- polling

def test_poll_returns_inbound_messages_oldest_first():
    page = {"messages": [inbound(3), inbound(2), inbound(1)], "next_page_uri": None}
    out = pt.poll(CONFIG, CREDS, opener=Opener(page))
    assert [m["sid"] for m in out["messages"]] == [sid("SM", 1), sid("SM", 2), sid("SM", 3)]
    first = out["messages"][0]
    assert first["event_id"] == first["sid"] and first["status"] == "ok"
    assert first["sender"] == PEER and first["recipient"] == NUMBER
    assert first["text"] == "help please" and first["received_at"] > 0
    assert out["cursor"] == {"last_sid": sid("SM", 3)} and out["more"] is False


def test_poll_requests_only_this_number_and_drops_outbound_and_foreign_rows():
    page = {"messages": [inbound(4, direction="outbound-api"),
                         inbound(3, to="+19998887777"),
                         inbound(2, account=OTHER_ACCOUNT),
                         inbound(1)],
            "next_page_uri": None}
    opener = Opener(page)
    out = pt.poll(CONFIG, CREDS, opener=opener)
    assert "To=%2B15550001111" in opener.requests[0].full_url
    assert [m["sid"] for m in out["messages"]] == [sid("SM", 1)]


def test_poll_stops_at_the_cursor_sid():
    page = {"messages": [inbound(3), inbound(2), inbound(1)],
            "next_page_uri": "/2010-04-01/Accounts/%s/Messages.json?Page=1" % ACCOUNT}
    out = pt.poll(CONFIG, CREDS, cursor={"last_sid": sid("SM", 1)}, opener=Opener(page))
    assert [m["sid"] for m in out["messages"]] == [sid("SM", 2), sid("SM", 3)]
    assert out["pages_read"] == 1 and out["more"] is False


def test_pagination_is_bounded_and_reports_more():
    link = "/2010-04-01/Accounts/%s/Messages.json?Page=%%d" % ACCOUNT
    pages = [{"messages": [inbound(n)], "next_page_uri": link % n} for n in range(1, 9)]
    opener = Opener(*pages)
    out = pt.poll(CONFIG, CREDS, opener=opener)
    assert out["pages_read"] == pt.MAX_PAGES == len(opener.requests)
    assert out["more"] is True


def test_limit_stops_the_run_and_the_cursor_resumes_inside_the_same_page():
    """A page longer than ``limit`` is finished by the next poll, not dropped.

    The watermark stays where it was until the sweep actually completes; what
    moves in the meantime is the resume point inside the page.
    """
    rows = [inbound(n) for n in (5, 4, 3, 2, 1)]
    provider = Provider(rows, page_size=5)
    first = pt.poll(CONFIG, CREDS, limit=2, opener=provider)
    assert [m["sid"] for m in first["messages"]] == [sid("SM", 4), sid("SM", 5)]
    assert first["more"] is True and first["complete"] is False
    assert first["cursor"]["last_sid"] == ""            # nothing is claimed as done
    assert first["cursor"]["head_sid"] == sid("SM", 5)
    assert first["cursor"]["after_sid"] == sid("SM", 4)

    second = pt.poll(CONFIG, CREDS, cursor=first["cursor"], limit=2, opener=provider)
    assert [m["sid"] for m in second["messages"]] == [sid("SM", 2), sid("SM", 3)]
    third = pt.poll(CONFIG, CREDS, cursor=second["cursor"], limit=2, opener=provider)
    assert [m["sid"] for m in third["messages"]] == [sid("SM", 1)]
    # Only now, with the whole run read, does the watermark jump to the head.
    assert third["complete"] is True and third["more"] is False
    assert third["cursor"] == {"last_sid": sid("SM", 5)}
    assert pt.poll(CONFIG, CREDS, cursor=third["cursor"], opener=provider)["messages"] == []


def test_a_backlog_deeper_than_one_polls_page_budget_is_never_skipped():
    """260 messages, five pages a poll: every one arrives, exactly once.

    The old failure was silent and permanent — the cursor advanced to the newest
    message it happened to reach, and everything below the page budget was gone.
    """
    rows = [inbound(n) for n in range(260, 0, -1)]      # newest first
    provider = Provider(rows)
    seen, cursor, polls = drain(provider)
    assert len(seen) == 260 and len(set(seen)) == 260   # none lost, none twice
    assert set(seen) == {row["sid"] for row in rows}
    assert polls > 1 and cursor == {"last_sid": sid("SM", 260)}
    # Settled: a further poll returns nothing and stays complete.
    again = pt.poll(CONFIG, CREDS, cursor=cursor, opener=provider)
    assert again["messages"] == [] and again["more"] is False
    assert again["reached_cursor"] is True


def test_messages_arriving_during_a_catch_up_are_delivered_after_it():
    provider = Provider([inbound(n) for n in range(120, 0, -1)])
    first = pt.poll(CONFIG, CREDS, opener=provider)
    assert first["more"] is True and first["first_sync"] is True
    provider.arrives(inbound(500))                      # a text lands mid-backlog
    provider.arrives(inbound(501))
    caught_up, cursor, _ = drain(provider, first["cursor"])
    seen = [m["sid"] for m in first["messages"]] + caught_up
    # The backlog is finished first, and the watermark stops at the head that
    # sweep was anchored to — not at whatever is newest now.
    assert len(set(seen)) == 120 and cursor == {"last_sid": sid("SM", 120)}
    assert sid("SM", 500) not in seen

    # The next sweep starts from the newest message and picks both of them up.
    fresh, cursor, _ = drain(provider, cursor)
    assert fresh == [sid("SM", 500), sid("SM", 501)]
    assert cursor == {"last_sid": sid("SM", 501)}


def test_a_restart_continues_from_the_returned_cursor_alone():
    rows = [inbound(n) for n in range(80, 0, -1)]
    provider = Provider(rows)
    first = pt.poll(CONFIG, CREDS, opener=provider)
    carried = json.loads(json.dumps(first["cursor"]))   # as a host would persist it
    # A fresh process, a fresh provider connection, nothing but the cursor.
    seen, cursor, _ = drain(Provider(rows), carried)
    assert set(seen) | {m["sid"] for m in first["messages"]} == {r["sid"] for r in rows}
    assert not set(seen) & {m["sid"] for m in first["messages"]}
    assert cursor == {"last_sid": sid("SM", 80)}


def test_a_cursor_cannot_point_the_poll_at_another_account_or_host():
    for bad in ({"last_sid": sid("SM", 1), "head_sid": sid("SM", 9),
                 "page": "/2010-04-01/Accounts/%s/Messages.json?Page=2" % OTHER_ACCOUNT},
                {"last_sid": sid("SM", 1), "head_sid": sid("SM", 9),
                 "page": "https://evil.example.com/2010-04-01/Accounts/%s/Messages.json"
                         % ACCOUNT},
                {"last_sid": sid("SM", 1), "head_sid": sid("SM", 9),
                 "page": "/2010-04-01/Accounts/%s/../Messages.json" % ACCOUNT}):
        opener = Opener()
        with pytest.raises(pt.PhoneTransportError):
            pt.poll(CONFIG, CREDS, cursor=bad, opener=opener)
        assert opener.requests == []                    # refused before any fetch
    for malformed in ({"last_sid": "not-a-sid"}, {"last_sid": sid("SM", 1), "page": 7},
                      {"next": "surprise"}, "cursor"):
        with pytest.raises(pt.PhoneConfigError):
            pt.poll(CONFIG, CREDS, cursor=malformed, opener=Opener())
    # A resume point with no head is dropped rather than half-honoured.
    provider = Provider([inbound(2), inbound(1)], page_size=5)
    out = pt.poll(CONFIG, CREDS, cursor={"last_sid": sid("SM", 1), "page": BASE +
                                         "Messages.json?PageSize=5"}, opener=provider)
    assert [m["sid"] for m in out["messages"]] == [sid("SM", 2)]
    assert out["cursor"] == {"last_sid": sid("SM", 2)}


def test_unusable_inbound_rows_become_rejections_with_their_own_sid():
    page = {"messages": [inbound(3), inbound(2, body="", num_media="2"),
                         inbound(1, body="x" * (pt.MAX_SMS_CHARS + 1))],
            "next_page_uri": None}
    out = pt.poll(CONFIG, CREDS, opener=Opener(page))
    states = [(m["sid"], m["status"]) for m in out["messages"]]
    assert states == [(sid("SM", 1), "rejected"), (sid("SM", 2), "rejected"),
                      (sid("SM", 3), "ok")]
    assert "nothing was truncated" in out["messages"][0]["error"]
    assert "media is not downloaded" in out["messages"][1]["error"]
    assert all(m["sender"] == PEER and m["text"] == "" for m in out["messages"][:2])
    assert out["cursor"] == {"last_sid": sid("SM", 3)}      # progress past both


def test_media_alongside_text_never_looks_like_a_complete_message():
    """An MMS whose picture we did not download is not a finished request."""
    page = {"messages": [inbound(1, body="is this the right one?", num_media="2")],
            "next_page_uri": None}
    out = pt.poll(CONFIG, CREDS, opener=Opener(page))
    row = out["messages"][0]
    assert row["status"] == "ok" and row["text"] == "is this the right one?"
    assert row["media"] == 2                       # counted, so a host can show it
    assert "does not download" in row["error"] and "needs review" in row["error"]
    # Text-free MMS stays a rejection, as before.
    empty = pt.poll(CONFIG, CREDS, opener=Opener(
        {"messages": [inbound(2, body="", num_media="1")], "next_page_uri": None}))
    assert empty["messages"][0]["status"] == "rejected"
    assert empty["messages"][0]["media"] == 1


def test_rows_with_no_usable_sender_are_dropped_not_invented():
    page = {"messages": [inbound(1, **{"from": "unknown"})], "next_page_uri": None}
    out = pt.poll(CONFIG, CREDS, opener=Opener(page))
    assert out["messages"] == [] and out["skipped"] == 1   # counted, never invented
    assert out["skipped_detail"] == [{"sid": sid("SM", 1),
                                      "reason": "the sender is not an E.164 number, so "
                                                "nothing can be attributed to it"}]


def test_every_skipped_row_says_why_and_still_lets_the_cursor_move():
    page = {"messages": [inbound(5),
                         inbound(4, direction="outbound-api"),
                         inbound(3, to="+19998887777"),
                         inbound(2, account=OTHER_ACCOUNT),
                         {"sid": "not-a-sid", "from": PEER, "to": NUMBER},
                         inbound(1)],
            "next_page_uri": None}
    out = pt.poll(CONFIG, CREDS, opener=Opener(page))
    assert [m["sid"] for m in out["messages"]] == [sid("SM", 1), sid("SM", 5)]
    assert out["skipped"] == 4
    reasons = {row["sid"]: row["reason"] for row in out["skipped_detail"]}
    assert reasons[sid("SM", 4)].startswith("not an inbound message")
    assert "not addressed to the configured number" in reasons[sid("SM", 3)]
    assert "another account" in reasons[sid("SM", 2)]
    assert reasons[""] == "the provider row carries no usable message sid"
    # No skipped row is given a number it does not have.
    assert not any(pt._E164.fullmatch(row["sid"]) for row in out["skipped_detail"])
    assert out["complete"] is True and out["cursor"] == {"last_sid": sid("SM", 5)}


def test_anchor_offers_a_from_now_cursor_and_creates_nothing():
    opener = Opener({"messages": [inbound(9)], "next_page_uri": None})
    out = pt.anchor(CONFIG, CREDS, opener=opener)
    assert out["cursor"] == {"last_sid": sid("SM", 9)}
    assert len(opener.requests) == 1 and opener.requests[0].get_method() == "GET"
    assert "PageSize=1" in opener.requests[0].full_url
    assert opener.requests[0].data is None          # a read, not a write
    assert "will not be polled" in out["note"]

    # Used as a cursor, it means exactly "nothing from before now".
    provider = Provider([inbound(9), inbound(8)], page_size=5)
    after = pt.poll(CONFIG, CREDS, cursor=out["cursor"], opener=provider)
    assert after["messages"] == [] and after["first_sync"] is False
    # An empty account anchors to nothing, and says so rather than guessing.
    assert pt.anchor(CONFIG, CREDS, opener=Opener({"messages": []}))["cursor"] == {
        "last_sid": ""}


def test_read_failures_are_retry_safe_and_sanitised():
    with pytest.raises(pt.PhoneConnectionError) as exc:
        pt.poll(CONFIG, CREDS, opener=Opener(urllib.error.URLError("token s3cr3t rejected")))
    assert exc.value.delivery_unknown is False and "s3cr3t" not in str(exc.value)


# ------------------------------------------------------- ambiguous writes

def test_provider_refusal_of_a_post_is_definite():
    opener = Opener(http_error(400, {"code": 21211, "message": "The 'To' number +1555 is bad"}))
    with pytest.raises(pt.PhoneRefused) as exc:
        pt.send(CONFIG, CREDS, result(), opener=opener)
    assert exc.value.delivery_unknown is False
    assert "not a valid phone number" in str(exc.value)
    assert "+1555" not in str(exc.value)                 # provider prose is not echoed


def test_a_broken_post_is_unknown_and_never_silently_retried():
    for failure in (TimeoutError("timed out"), urllib.error.URLError("reset"),
                    http_error(503), b"<html>not json</html>"):
        opener = Opener(failure)
        with pytest.raises(pt.PhoneDeliveryUnknown) as exc:
            pt.send(CONFIG, CREDS, result(), opener=opener)
        assert exc.value.delivery_unknown is True
        assert len(opener.requests) == 1                  # one attempt, never two
    assert "must not be resent automatically" in str(exc.value)


def test_a_post_answered_without_a_sid_is_unknown():
    with pytest.raises(pt.PhoneDeliveryUnknown):
        pt.send(CONFIG, CREDS, result(), opener=Opener({"status": "queued"}))


def test_auth_failure_is_reported_without_the_token():
    opener = Opener(http_error(401, {"code": 20003, "message": "Authenticate"}))
    with pytest.raises(pt.PhoneRefused) as exc:
        pt.send(CONFIG, CREDS, result(), opener=opener)
    assert CREDS["auth_token"] not in str(exc.value)
    assert "credentials" in str(exc.value)


# ---------------------------------------------------------------- spoken calls

def test_speak_posts_escaped_inline_twiml_and_claims_only_submission():
    opener = Opener({"sid": sid("CA", 7), "account_sid": ACCOUNT, "status": "queued"})
    out = pt.speak(CONFIG, CREDS, result(text='pay <Hangup/> & "now"'), opener=opener)
    request = opener.requests[0]
    assert request.full_url == BASE + "Calls.json" and request.get_method() == "POST"
    body = urllib.parse.unquote_plus(request.data.decode())
    assert body == ("From=+15550001111&To=+15550002222&Twiml=<Response><Say>pay "
                    "&lt;Hangup/&gt; &amp; \"now\"</Say><Hangup/></Response>")
    assert out["status"] == "submitted" and out["provider_message_id"] == sid("CA", 7)
    assert "not been answered" in out["note"]


def test_say_twiml_escapes_and_refuses_oversize_text():
    assert pt.say_twiml("a & b") == "<Response><Say>a &amp; b</Say><Hangup/></Response>"
    assert "<Say></Say>" not in pt.say_twiml("</Say><Dial>+1900</Dial>")
    assert "&lt;/Say&gt;" in pt.say_twiml("</Say><Dial>+1900</Dial>")
    with pytest.raises(pt.PhoneConfigError):
        pt.say_twiml("x" * (pt.MAX_SAY_CHARS + 1))
    with pytest.raises(pt.PhoneConfigError):
        pt.say_twiml("   ")


def test_no_call_is_placed_without_an_explicit_speak():
    opener = Opener({"sid": sid("SM", 8), "account_sid": ACCOUNT, "status": "queued"})
    pt.send(CONFIG, CREDS, result(), opener=opener)
    assert all("Calls.json" not in r.full_url for r in opener.requests)


# ------------------------------------------------------------------ voicemail

def test_voicemail_announces_recording_before_it_records():
    twiml = pt.voicemail_twiml("Hi, this is Rowan's line.", max_seconds=90)
    assert twiml.index("<Say>") < twiml.index("<Record")
    assert "being recorded" in twiml and 'maxLength="90"' in twiml
    assert 'transcribe="false"' in twiml
    assert 'transcribe="true"' in pt.voicemail_twiml("Hi.", transcribe=True)
    for bad in (("", {}), ("Hi.", {"max_seconds": 0}),
                ("Hi.", {"max_seconds": pt.MAX_RECORD_SECONDS + 1})):
        with pytest.raises(pt.PhoneConfigError):
            pt.voicemail_twiml(bad[0], **bad[1])


def test_only_a_completed_transcription_is_usable():
    done = pt.transcription_receipt({"sid": sid("TR", 9), "status": "completed",
                                     "transcription_text": " call me back ",
                                     "recording_sid": sid("RE", 9), "duration": "12"})
    assert done["usable"] is True and done["text"] == "call me back"
    for payload in ({"status": "failed"}, {"status": "in-progress"},
                    {"status": "completed", "transcription_text": "  "}):
        out = pt.transcription_receipt(payload)
        assert out["usable"] is False and out["text"] == "" and out["reason"]
    with pytest.raises(pt.PhoneConfigError):
        pt.transcription_receipt("completed")


def test_polled_messages_are_storable_by_the_communication_store(tmp_path, monkeypatch):
    """The sids and numbers we emit must survive the store's own validators."""
    from harness import communications as comms

    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    comms.create_connection("line", channel="sms", address=NUMBER,
                            policy={"owner_reply_target": PEER}, directory=str(directory))
    page = {"messages": [inbound(2, body=""), inbound(1)], "next_page_uri": None}
    out = pt.poll(CONFIG, CREDS, opener=Opener(page))
    for message in out["messages"]:
        event = comms.record_received(
            "line", message["event_id"], sender=message["sender"],
            recipient=message["recipient"], text=message["text"],
            thread_key=message["sender"], received_at=message["received_at"],
            metadata={"input_error": message.get("error", "")}, directory=str(directory))
        assert event["state"] == "pending"
    assert comms.connection_status("line", directory=str(directory))["events"]["pending"] == 2


def test_module_holds_no_alternate_base_url():
    source = open(pt.__file__, encoding="utf-8").read()
    assert source.count("https://api.twilio.com") == 2      # the constant and its docs
    assert "print(" not in source
