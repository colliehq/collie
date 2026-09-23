"""One Twilio number over stdlib HTTPS: inbound SMS, one send, one spoken call.

The wire half of ``communications`` for the ``sms`` channel, and nothing more:
no store, no policy, no retry loop, no saved credential.  The auth token is
passed in per call by the host (from ``channel_secrets``), used for one request,
and never returned or put in an error.

What the shape of this module is protecting against:

* **Talking to the wrong place.**  Every URL is built here from
  ``https://api.twilio.com/2010-04-01/Accounts/<configured sid>/``.  Nothing the
  provider returns can move the request to another host, another account or an
  arbitrary path — including ``next_page_uri``, which is validated against that
  same prefix, and redirects, which are refused outright rather than followed.
* **Sending from somewhere else.**  ``From`` is always the configured number.  A
  caller can choose the destination (and the host's policy decides whether that
  destination is allowed), never the sender.
* **Claiming more than happened.**  Creating a message or a call means the
  provider accepted it — not that it was delivered, answered or heard.  An
  ambiguous POST raises ``PhoneDeliveryUnknown`` and must never be auto-retried,
  because the duplicate it would create is a real second message to a person.

No audio is streamed and no conversation is held.  ``speak`` plays one line and
hangs up; ``voicemail_twiml`` describes an inbound greeting for the host to
configure.  Real-time voice is not implemented here.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from xml.sax.saxutils import escape as xml_escape

API_HOST = "api.twilio.com"
API_ROOT = "https://api.twilio.com/2010-04-01"
DEFAULT_TIMEOUT = 20
MIN_TIMEOUT, MAX_TIMEOUT = 15, 30
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
PAGE_SIZE = 50
MAX_PAGES = 5                      # one poll reads at most this many pages
MAX_POLL_ITEMS = 250
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SMS_CHARS = 1600               # Twilio's own limit; we refuse rather than trim
MAX_SAY_CHARS = 1000
MAX_RECORD_SECONDS = 600
USER_AGENT = "collie-phone-transport/1"
CONFIG_KEYS = ("account_sid", "phone_number", "timeout")

_ACCOUNT_SID = re.compile(r"AC[0-9a-fA-F]{32}\Z")
_SID = re.compile(r"[A-Z]{2}[0-9a-fA-F]{32}\Z")
_E164 = re.compile(r"\+[1-9]\d{6,14}\Z")
_MESSAGE_FINAL = frozenset({"delivered", "undelivered", "failed", "canceled"})
_CALL_FINAL = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})
_CODES = {20003: "the provider rejected the credentials",
          20404: "the provider has no such record for this account",
          21211: "the destination number is not a valid phone number",
          21606: "the configured number cannot send from this account",
          21610: "the destination has unsubscribed from this number",
          21614: "the destination number cannot receive SMS"}


class PhoneTransportError(Exception):
    """Base class.  ``delivery_unknown`` is the only bit a caller must branch on."""

    delivery_unknown = False


class PhoneConfigError(PhoneTransportError, ValueError):
    """The request could not be attempted as written.  Nothing was created."""


class PhoneConnectionError(PhoneTransportError):
    """A read failed before anything was created — safe to retry as-is."""


class PhoneRefused(PhoneTransportError):
    """The provider answered with a refusal.  Nothing was created."""


class PhoneDeliveryUnknown(PhoneTransportError):
    """A write may or may not have been accepted.  Never auto-retry this."""

    delivery_unknown = True


# ----------------------------------------------------------------- validation

def validate_config(config):
    """Check and normalise the number configuration.  No secret belongs here."""
    if not isinstance(config, dict):
        raise PhoneConfigError("phone configuration must be a dict")
    unknown = sorted(set(config) - set(CONFIG_KEYS))
    if unknown:
        raise PhoneConfigError("unknown phone configuration key(s): %s (known: %s)"
                               % (", ".join(unknown), ", ".join(CONFIG_KEYS)))
    account_sid = config.get("account_sid")
    if not isinstance(account_sid, str) or not _ACCOUNT_SID.fullmatch(account_sid):
        raise PhoneConfigError("account_sid must be 'AC' followed by 32 hex characters")
    timeout = config.get("timeout")
    if timeout in (None, ""):
        timeout = DEFAULT_TIMEOUT
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise PhoneConfigError("timeout must be %d-%d seconds" % (MIN_TIMEOUT, MAX_TIMEOUT))
    return {"account_sid": account_sid,
            "phone_number": destination(config.get("phone_number"), "phone_number"),
            "timeout": float(timeout)}


def destination(value, field="destination"):
    """An E.164 number, or a refusal.  Shape only — never proof of who answers."""
    if not isinstance(value, str) or not _E164.fullmatch(value.strip()):
        raise PhoneConfigError("%s must be an E.164 number such as +15551234567" % field)
    return value.strip()


def _token(credentials):
    if not isinstance(credentials, dict):
        raise PhoneConfigError("credentials must be a dict supplied by the host")
    token = credentials.get("auth_token")
    if not isinstance(token, str) or not token.strip() or len(token) > 4096:
        raise PhoneConfigError("credentials['auth_token'] is missing or unusable")
    return token.strip()


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PhoneConfigError("limit must be a positive integer")
    return min(value, MAX_LIMIT)


def _sid(value, field="sid"):
    if not isinstance(value, str) or not _SID.fullmatch(value.strip()):
        raise PhoneConfigError("%s must be a Twilio sid (two letters and 32 hex characters)"
                               % field)
    return value.strip()


# --------------------------------------------------------------------- wire

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.  A 3xx becomes an error instead of a new request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _account_url(cfg, path, query=None):
    """The only way a URL is built here: fixed host, fixed account, known path."""
    url = "%s/Accounts/%s/%s" % (API_ROOT, cfg["account_sid"], path)
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def _check_url(cfg, url):
    parts = urllib.parse.urlsplit(url)
    prefix = "/2010-04-01/Accounts/%s/" % cfg["account_sid"]
    if parts.scheme != "https" or parts.netloc != API_HOST or not parts.path.startswith(prefix) \
            or ".." in parts.path:
        raise PhoneTransportError("refused a provider link outside this account's API "
                                  "namespace; nothing was fetched")
    return url


def next_page_url(cfg, next_page_uri):
    """Validate the provider's pagination link, or refuse to follow it.

    Twilio returns a path such as ``/2010-04-01/Accounts/AC.../Messages.json?...``.
    Anything else — another account, another host, an absolute URL — is refused
    loudly instead of followed, because "keep reading wherever the response says"
    is how a single bad response becomes a request somewhere else entirely.
    """
    if not next_page_uri:
        return None
    if not isinstance(next_page_uri, str) or len(next_page_uri) > 2048:
        raise PhoneTransportError("the provider returned an unusable pagination link")
    if next_page_uri.startswith("/"):
        next_page_uri = "https://%s%s" % (API_HOST, next_page_uri)
    return _check_url(cfg, next_page_uri)


def _provider_error(exc):
    """A sanitised description of a provider refusal: our words, their code."""
    code = None
    try:
        payload = json.loads(exc.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", "replace"))
        if isinstance(payload, dict) and isinstance(payload.get("code"), int):
            code = payload["code"]
    except Exception:
        code = None
    known = _CODES.get(code)
    if known:
        return "%s (provider code %d)" % (known, code)
    if code is not None:
        return "the provider refused the request (HTTP %s, provider code %d)" % (exc.code, code)
    return "the provider refused the request (HTTP %s)" % exc.code


def _call(cfg, token, method, url, params=None, *, opener=None, ambiguous=False):
    """One request.  ``ambiguous`` marks writes, where a broken answer is unknown."""
    _check_url(cfg, url)
    data = urllib.parse.urlencode(params).encode("utf-8") if params is not None else None
    auth = base64.b64encode(("%s:%s" % (cfg["account_sid"], token)).encode("utf-8")).decode("ascii")
    headers = {"Authorization": "Basic " + auth, "Accept": "application/json",
               "User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with (opener or _OPENER.open)(request, timeout=cfg["timeout"]) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = _provider_error(exc)
        if ambiguous and exc.code >= 500:
            raise PhoneDeliveryUnknown("%s; whether it was accepted is unknown and it "
                                       "must not be resent automatically" % detail) from None
        raise PhoneRefused(detail) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if ambiguous:
            raise PhoneDeliveryUnknown(
                "the request failed in flight (%s); whether it was accepted is unknown "
                "and it must not be resent automatically" % type(exc).__name__) from None
        raise PhoneConnectionError("the provider could not be reached (%s)"
                                   % type(exc).__name__) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise PhoneTransportError("the provider response exceeded %d bytes" % MAX_RESPONSE_BYTES)
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        if ambiguous:
            raise PhoneDeliveryUnknown(
                "the provider answered with something other than JSON; whether it was "
                "accepted is unknown and it must not be resent automatically") from None
        raise PhoneTransportError("the provider answered with something other than JSON") from None
    if not isinstance(payload, dict):
        raise PhoneTransportError("the provider answered with an unexpected document")
    owner = payload.get("account_sid")
    if isinstance(owner, str) and owner != cfg["account_sid"]:
        raise PhoneTransportError("the provider returned a record belonging to another "
                                  "account; it was discarded")
    return payload


# --------------------------------------------------------------------- probe

def probe(config, credentials, *, opener=None):
    """Read what the configured number can do.  No message, no call, no effect.

    Confirms the number is owned by this account and reports its capabilities.
    It is not evidence that any particular message will be delivered.
    """
    cfg = validate_config(config)
    token = _token(credentials)
    url = _account_url(cfg, "IncomingPhoneNumbers.json",
                       {"PhoneNumber": cfg["phone_number"], "PageSize": "1"})
    payload = _call(cfg, token, "GET", url, opener=opener)
    rows = payload.get("incoming_phone_numbers")
    if not isinstance(rows, list):
        raise PhoneTransportError("the provider did not return a number list")
    for row in rows:
        if isinstance(row, dict) and row.get("phone_number") == cfg["phone_number"]:
            caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
            return {"account_sid": cfg["account_sid"], "phone_number": cfg["phone_number"],
                    "owned": True, "sms": bool(caps.get("sms")), "mms": bool(caps.get("mms")),
                    "voice": bool(caps.get("voice")),
                    "friendly_name": str(row.get("friendly_name") or "")[:120],
                    "delivery_proof": False,
                    "note": "capability metadata only; nothing was sent and no call was made"}
    raise PhoneRefused("the configured number is not one of this account's incoming "
                       "numbers; nothing was sent")


# -------------------------------------------------------------------- polling

CURSOR_KEYS = ("last_sid", "head_sid", "page", "after_sid")


def _cursor(cfg, cursor):
    """Validate the cursor a previous poll returned, including its resume point.

    Two parts.  ``last_sid`` is the watermark: every inbound message older than
    or equal to it has already been handed over.  The other three describe a
    sweep that was still in flight — the head it snapshotted, the provider page
    it stopped on, and the row on that page it stopped after.  The page is put
    through the same account/host check as any other provider link, so a cursor
    carried over from another account or another number cannot send a request
    somewhere else.
    """
    if cursor in (None, "", {}):
        return {"last_sid": "", "head_sid": "", "page": "", "after_sid": ""}
    if not isinstance(cursor, dict):
        raise PhoneConfigError("cursor must be the dict a previous poll returned")
    unknown = sorted(set(cursor) - set(CURSOR_KEYS))
    if unknown:
        raise PhoneConfigError("unknown cursor key(s): %s (known: %s)"
                               % (", ".join(unknown), ", ".join(CURSOR_KEYS)))
    state = {}
    for field in ("last_sid", "head_sid", "after_sid"):
        value = cursor.get(field) or ""
        state[field] = _sid(value, "cursor['%s']" % field) if value else ""
    page = cursor.get("page") or ""
    if page and not isinstance(page, str):
        raise PhoneConfigError("cursor['page'] must be the link a previous poll returned")
    state["page"] = next_page_url(cfg, page) or "" if page else ""
    if not (state["page"] and state["head_sid"]):
        # Half a sweep is not a sweep: without both a resume page and the head it
        # was anchored to, finishing it could advance the watermark across a gap.
        # Starting over re-reads messages the store already holds; it loses none.
        state["head_sid"] = state["page"] = state["after_sid"] = ""
    return state


def _received_at(row):
    """The provider's own timestamp, or None.  Never a guess of "now"."""
    stamp = row.get("date_sent") or row.get("date_created")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return parsedate_to_datetime(stamp).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _unusable(cfg, row):
    """Why this row cannot be attributed to this number, or ``""`` if it can.

    Every answer here is a *skip*, and a skip has to be visible: there is no
    address to record the message against, and inventing a plausible-looking
    number so the row "fits" would put a message in the person's inbox from
    somebody who does not exist.
    """
    if not isinstance(row, dict) or not isinstance(row.get("sid"), str) \
            or not _SID.fullmatch(row["sid"]):
        return "the provider row carries no usable message sid"
    owner = row.get("account_sid")
    if isinstance(owner, str) and owner != cfg["account_sid"]:
        return "the row belongs to another account"
    if not str(row.get("direction") or "").startswith("inbound"):
        return "not an inbound message; this is an inbox, not a log"
    if row.get("to") != cfg["phone_number"]:
        return "the row is not addressed to the configured number"
    sender = row.get("from")
    if not isinstance(sender, str) or not _E164.fullmatch(sender or ""):
        return "the sender is not an E.164 number, so nothing can be attributed to it"
    return ""


def _inbound(cfg, row):
    """Normalise one inbound message, or ``None`` when it is not ours to record.

    A row that *is* ours but unusable becomes a rejection under its own sid, so
    the host can record it and the cursor can move past it.  Media is the third
    case: the text is intact and usable, but part of what the person sent has not
    been downloaded, so ``media`` counts it and ``error`` says so.  An MMS must
    not turn into a task that silently looks complete.
    """
    if _unusable(cfg, row):
        return None
    sender, recipient, text = row.get("from"), row.get("to"), row.get("body")
    try:
        media = int(row.get("num_media") or 0)
    except (TypeError, ValueError):
        media = 0
    media = max(0, media)
    base = {"event_id": row["sid"], "sid": row["sid"], "sender": sender,
            "recipient": recipient, "subject": "", "message_id": row["sid"],
            "references": [], "attachments": [], "automatic": False, "media": media,
            "received_at": _received_at(row),
            "provider_status": str(row.get("status") or "")[:32]}
    if not isinstance(text, str) or not text.strip():
        return dict(base, status="rejected", text="",
                    error="this message carries no text%s"
                          % (" (media is not downloaded by this adapter)" if media else ""))
    if len(text) > MAX_SMS_CHARS:
        return dict(base, status="rejected", text="",
                    error="this message is %d characters; the limit is %d and nothing "
                          "was truncated" % (len(text), MAX_SMS_CHARS))
    if media:
        return dict(base, status="ok", text=text,
                    error="this message also carries %d media attachment(s) that this "
                          "adapter does not download; the text is complete but the "
                          "message is not, so it needs review in the provider inbox"
                          % media)
    return dict(base, status="ok", text=text)


MAX_SKIP_DETAIL = 20


def poll(config, credentials, *, cursor=None, limit=DEFAULT_LIMIT, opener=None):
    """Read inbound SMS addressed to the configured number.  Never skips a gap.

    Returns ``{"messages": [...], "cursor": {...}, "more": bool}`` plus status
    fields.  At most ``limit`` messages come back, oldest-first within the run.

    Twilio only pages newest-first, so reading "everything since last time" means
    walking backwards until the sid the last poll ended on comes into view.  A
    backlog deeper than one poll's page budget is the case that has to be got
    right: the watermark (``cursor["last_sid"]``) **only** moves when that walk
    actually met the previous cursor or ran off the end of the account's history.
    Until then the cursor carries the sweep itself — the head it was anchored to,
    the provider page it stopped on (re-validated against this account before it
    is ever fetched), and the row it stopped after — and the next poll continues
    from exactly there.  So a 5000-message backlog is delivered ``limit`` at a
    time over as many polls as it takes, and nothing between the head and the
    watermark is ever stepped over.

    Two consequences worth stating plainly:

    * While a sweep is in progress, messages arrive newest-run-first (each run is
      itself oldest-first).  The alternative — holding everything back until the
      whole backlog is contiguous — is unbounded, and this store de-duplicates by
      sid anyway, so order of arrival costs nothing and loss would cost a lot.
    * Messages that arrive *during* a catch-up are picked up as soon as the sweep
      finishes: the watermark then jumps to the head that sweep snapshotted, and
      the next poll starts a fresh walk from the newest message at that time.
      So ``more=False`` means "this sweep is finished", never "nothing new will
      arrive" — the host keeps polling on its own schedule either way.

    **First sync**: with no cursor there is no watermark, so the walk ends only
    when the provider reports no further page — the whole inbound history, a poll
    at a time, with ``first_sync`` true throughout.  ``anchor`` is the way to say
    "only what arrives from now on" instead.

    The cursor is returned, never saved here: the caller persists it only after
    the messages are durably recorded, so a crash re-reads rather than loses.
    """
    cfg = validate_config(config)
    token = _token(credentials)
    limit = _limit(limit)
    state = _cursor(cfg, cursor)
    target, head = state["last_sid"], state["head_sid"]
    resuming = bool(state["page"])
    url = state["page"] or _account_url(cfg, "Messages.json",
                                        {"To": cfg["phone_number"],
                                         "PageSize": str(PAGE_SIZE)})
    skip_to = state["after_sid"]

    collected, skipped, pages, scanned = [], [], 0, 0
    reached = exhausted = False
    resume_page, resume_after = "", ""
    while url and pages < MAX_PAGES and len(collected) < limit and scanned < MAX_POLL_ITEMS:
        payload = _call(cfg, token, "GET", url, opener=opener)
        rows = payload.get("messages")
        if not isinstance(rows, list):
            raise PhoneTransportError("the provider did not return a message list")
        pages += 1
        following = next_page_url(cfg, payload.get("next_page_uri"))
        if skip_to:
            at = next((i for i, row in enumerate(rows)
                       if isinstance(row, dict) and row.get("sid") == skip_to), None)
            # Not finding it means the page shifted under us; re-reading it whole
            # costs duplicates the store already knows how to answer.
            rows = rows[at + 1:] if at is not None else rows
            skip_to = ""
        stopped_after = ""
        for row in rows:
            sid_value = row.get("sid") if isinstance(row, dict) else None
            # Only a sid that passes our own check is ever repeated back to the
            # caller or used as a resume point; provider text is not an id.
            if not (isinstance(sid_value, str) and _SID.fullmatch(sid_value)):
                sid_value = ""
            if sid_value:
                if not head and not resuming:
                    head = sid_value        # the newest message this sweep covers
                if target and sid_value == target:
                    reached = True
                    break
            scanned += 1
            normalized = _inbound(cfg, row)
            if normalized is None:
                reason = _unusable(cfg, row)
                if len(skipped) < MAX_SKIP_DETAIL:
                    skipped.append({"sid": sid_value, "reason": reason})
                else:
                    skipped.append(None)
            else:
                collected.append(normalized)
            stopped_after = sid_value or stopped_after
            if len(collected) >= limit:
                break
        if reached:
            break
        if len(collected) >= limit:
            resume_page, resume_after = url, stopped_after
            break
        url = following
        if not url:
            exhausted = True                # the oldest message the account has
    if not reached and not exhausted and not resume_page:
        resume_page, resume_after = url, ""  # budget ran out; carry on from here

    complete = reached or exhausted
    if complete:
        forward = {"last_sid": head or target}
    elif head and resume_page:
        forward = {"last_sid": target, "head_sid": head, "page": resume_page,
                   "after_sid": resume_after}
    else:
        # Nothing worth resuming from (no readable sid yet).  Leaving the
        # watermark where it is costs a re-read, which is the cheap mistake.
        forward = {"last_sid": target}
    return {"messages": list(reversed(collected)), "more": not complete,
            "cursor": forward, "pages_read": pages, "complete": complete,
            "skipped": len(skipped),
            "skipped_detail": [row for row in skipped if row is not None],
            "first_sync": not target, "reached_cursor": reached}


def anchor(config, credentials, *, opener=None):
    """A cursor meaning "whatever arrives after now".  Creates nothing.

    Connecting a number that has been in use for years should not mean pulling
    that history into somebody's task list.  This reads the single newest inbound
    message and hands back the cursor a poll would have had if it had just
    finished reading up to it.  No message is sent, no call is made and nothing
    on the provider is modified.

    The trade is explicit and the caller owns it: messages already in the
    provider's inbox will never be polled under this cursor.
    """
    cfg = validate_config(config)
    token = _token(credentials)
    url = _account_url(cfg, "Messages.json",
                       {"To": cfg["phone_number"], "PageSize": "1"})
    payload = _call(cfg, token, "GET", url, opener=opener)
    rows = payload.get("messages")
    if not isinstance(rows, list):
        raise PhoneTransportError("the provider did not return a message list")
    newest = ""
    for row in rows:
        sid_value = row.get("sid") if isinstance(row, dict) else None
        if isinstance(sid_value, str) and _SID.fullmatch(sid_value):
            newest = sid_value
            break
    return {"cursor": {"last_sid": newest}, "last_sid": newest,
            "phone_number": cfg["phone_number"],
            "note": "a starting point only; nothing was sent, created or read beyond "
                    "one message id, and messages already at the provider will not "
                    "be polled under this cursor"}


# -------------------------------------------------------------------- sending

def _result_text(result, cap, field):
    if not isinstance(result, dict):
        raise PhoneConfigError("result must be the dict communications.claim_send returned")
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PhoneConfigError("result['text'] is empty; nothing was sent")
    if len(text) > cap:
        raise PhoneConfigError("result['text'] is %d characters; the %s limit is %d. "
                               "Nothing was sent and nothing was truncated — send a "
                               "shorter message" % (len(text), field, cap))
    return text


def send(config, credentials, result, *, opener=None):
    """Create one SMS from the configured number.  Accepted is not delivered.

    ``status`` is ``"submitted"``: Twilio has queued the message.  Use
    ``fetch_status`` with the returned sid to learn what became of it.
    """
    cfg = validate_config(config)
    token = _token(credentials)
    text = _result_text(result, MAX_SMS_CHARS, "SMS")
    to = destination(result.get("destination"))
    payload = _call(cfg, token, "POST", _account_url(cfg, "Messages.json"),
                    {"From": cfg["phone_number"], "To": to, "Body": text},
                    opener=opener, ambiguous=True)
    sid = payload.get("sid")
    if not isinstance(sid, str) or not _SID.fullmatch(sid):
        raise PhoneDeliveryUnknown("the provider accepted the request but returned no "
                                   "message sid; delivery is unknown")
    return {"provider_message_id": sid, "status": "submitted", "delivery_unknown": False,
            "provider_status": str(payload.get("status") or "")[:32],
            "note": "queued with the provider; not yet delivered"}


def say_twiml(text):
    """One spoken line then hang up, with the text escaped as XML content.

    Escaping is the whole point: a message body is untrusted text, and an
    unescaped ``<`` would let it add TwiML verbs of its own.
    """
    if not isinstance(text, str) or not text.strip():
        raise PhoneConfigError("there is nothing to say")
    cleaned = "".join(c if c >= " " or c == "\n" else " " for c in text).strip()
    if len(cleaned) > MAX_SAY_CHARS:
        raise PhoneConfigError("spoken text is %d characters; the limit is %d. Nothing "
                               "was said and nothing was truncated" % (len(cleaned), MAX_SAY_CHARS))
    return "<Response><Say>%s</Say><Hangup/></Response>" % xml_escape(cleaned)


def speak(config, credentials, result, *, opener=None):
    """Place one call that speaks this result and hangs up.  Explicit only.

    Nothing in this module calls anybody on its own: a call happens because a
    caller asked for one, here.  A created call is ``submitted`` — the provider
    accepted it.  It has not rung, been answered, or been heard by a person, and
    ``fetch_status`` is the only way to learn more.
    """
    cfg = validate_config(config)
    token = _token(credentials)
    twiml = say_twiml(_result_text(result, MAX_SAY_CHARS, "spoken call"))
    to = destination(result.get("destination"))
    payload = _call(cfg, token, "POST", _account_url(cfg, "Calls.json"),
                    {"From": cfg["phone_number"], "To": to, "Twiml": twiml},
                    opener=opener, ambiguous=True)
    sid = payload.get("sid")
    if not isinstance(sid, str) or not _SID.fullmatch(sid):
        raise PhoneDeliveryUnknown("the provider accepted the request but returned no "
                                   "call sid; whether a call was placed is unknown")
    return {"provider_message_id": sid, "status": "submitted", "delivery_unknown": False,
            "provider_status": str(payload.get("status") or "")[:32],
            "note": "the call was created; it has not been answered or heard"}


def fetch_status(config, credentials, sid, *, opener=None):
    """What the provider now says about one message or call we created.

    ``delivered`` is true only for a message the provider reports as delivered.
    A completed call is a call that ended, which is not the same as a person
    having listened to it, and this never claims otherwise.
    """
    cfg = validate_config(config)
    token = _token(credentials)
    sid = _sid(sid)
    kind = "call" if sid.startswith("CA") else "message"
    path = ("Calls/%s.json" if kind == "call" else "Messages/%s.json") % sid
    if kind == "message" and not sid.startswith(("SM", "MM")):
        raise PhoneConfigError("sid must be a message (SM/MM) or call (CA) sid")
    payload = _call(cfg, token, "GET", _account_url(cfg, path), opener=opener)
    status = str(payload.get("status") or "")[:32]
    error_code = payload.get("error_code")
    return {"sid": sid, "kind": kind, "status": status,
            "final": status in (_CALL_FINAL if kind == "call" else _MESSAGE_FINAL),
            "delivered": kind == "message" and status == "delivered",
            "answered": kind == "call" and status in ("in-progress", "completed"),
            "error_code": error_code if isinstance(error_code, int) else None,
            "direction": str(payload.get("direction") or "")[:32]}


# ------------------------------------------------------- inbound voicemail

def voicemail_twiml(greeting, *, max_seconds=120, transcribe=False):
    """TwiML for an inbound number that takes a message, announcing it first.

    The announcement is not optional and comes before ``<Record>``: recording
    someone who was not told is the kind of thing you cannot undo afterwards.
    This is a document for the host to serve — it opens no connection and starts
    no recording by itself.
    """
    if not isinstance(greeting, str) or not greeting.strip():
        raise PhoneConfigError("a voicemail greeting is required")
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, int) \
            or not 5 <= max_seconds <= MAX_RECORD_SECONDS:
        raise PhoneConfigError("max_seconds must be 5-%d" % MAX_RECORD_SECONDS)
    announced = "%s This call is being recorded so a message can be left. Please speak " \
                "after the tone." % greeting.strip()
    if len(announced) > MAX_SAY_CHARS:
        raise PhoneConfigError("the greeting is too long once the recording notice is added")
    return ("<Response><Say>%s</Say><Record maxLength=\"%d\" playBeep=\"true\" "
            "transcribe=\"%s\"/><Hangup/></Response>"
            % (xml_escape(announced), max_seconds, "true" if transcribe else "false"))


def transcription_receipt(payload):
    """Normalise one transcription record, and only trust a completed one.

    A failed or still-running transcription has no text, and presenting it as a
    message ("") would invent silence the caller never left.  ``usable`` is the
    caller's gate: false means there is nothing here to act on yet.
    """
    if not isinstance(payload, dict):
        raise PhoneConfigError("a transcription record must be a dict")
    status = str(payload.get("status") or "")[:32]
    text = payload.get("transcription_text")
    sid = payload.get("sid") if isinstance(payload.get("sid"), str) else ""
    out = {"sid": sid[:40], "recording_sid": str(payload.get("recording_sid") or "")[:40],
           "status": status, "usable": False, "text": "",
           "duration": str(payload.get("duration") or "")[:16]}
    if status != "completed":
        out["reason"] = "the provider reports this transcription as %r; there is no text " \
                        "to act on" % (status or "unknown")
        return out
    if not isinstance(text, str) or not text.strip():
        out["reason"] = "the transcription completed with no text"
        return out
    out.update(usable=True, text=text.strip()[:MAX_SMS_CHARS])
    return out
