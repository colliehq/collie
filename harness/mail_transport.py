"""One mailbox over stdlib IMAP/SMTP: bounded read-only polling, one send.

This is the wire half of ``communications``.  It owns exactly four things —
validate, probe, poll, send — and deliberately owns nothing else: no store, no
policy, no retry loop, no scheduler, and no copy of a password.  Credentials are
passed in by the host on every call (from ``channel_secrets``), used once, and
never returned, logged or attached to an error.

Three rules shape the code:

* **Polling never changes the mailbox.**  ``SELECT`` is read-only and bodies are
  fetched with ``BODY.PEEK[]``, so nothing is marked ``\\Seen`` and nothing is
  deleted.  Messages are addressed by UID, never by sequence number, because
  sequence numbers shift under an expunge and would silently skip mail.
* **A message we cannot parse must not hide the ones behind it.**  Oversize or
  malformed mail comes back as a ``rejected`` row carrying the same stable id a
  good message would have had, so the caller can record the refusal durably and
  move the cursor past it.
* **"Submitted" means the server accepted DATA, and nothing weaker.**  A probe
  is not a delivery.  A failure before transmission is reported separately from
  an ambiguous one during it, because only the first is safe to retry: see
  ``MailRefused`` versus ``MailDeliveryUnknown``.

The cursor is returned, never stored here.  The caller persists it *after* the
messages are durably recorded, so a crash re-polls rather than loses mail.
"""
from __future__ import annotations

import hashlib
import imaplib
import re
import smtplib
import ssl
import time

from . import mail_messages
from .mail_messages import MAX_MAIL_BYTES, MailFormatError

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORTS = {"ssl": 465, "starttls": 587}
DEFAULT_TIMEOUT = 30
MIN_TIMEOUT, MAX_TIMEOUT = 5, 120
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_POLL_BYTES = 32 * 1024 * 1024   # one poll's total body budget, not one message's
UNREADABLE_SENDER = "unreadable@invalid.test"
SECURITIES = ("ssl", "starttls")
CONFIG_KEYS = ("imap_host", "imap_port", "smtp_host", "smtp_port", "smtp_security",
               "username", "address", "folder", "timeout")

_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?\Z")
_FOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,127}\Z")
_MESSAGE_ID = re.compile(r"<[^<>\s\x00-\x1f]{1,250}>\Z")
_UID = re.compile(rb"\bUID (\d+)")
_SIZE = re.compile(rb"\bRFC822\.SIZE (\d+)")


class MailTransportError(Exception):
    """Base class.  ``delivery_unknown`` is the only bit a caller must branch on."""

    delivery_unknown = False


class MailConfigError(MailTransportError, ValueError):
    """The request could not be attempted as written.  Nothing was transmitted."""


class MailConnectionError(MailTransportError):
    """Failed before anything could be transmitted — safe to retry as-is."""


class MailRefused(MailTransportError):
    """The server answered with a refusal.  The message was definitely not sent."""


class MailDeliveryUnknown(MailTransportError):
    """The connection broke while sending.  It may or may not have been delivered."""

    delivery_unknown = True


# ------------------------------------------------------------------ sanitising

def _why(exc):
    """What went wrong, with no server text in it.

    Provider responses echo the envelope and sometimes the credential that was
    just refused, so an error that quotes them ends up in a log or a UI.  A class
    name plus the numeric code is enough to act on and safe to show.
    """
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int) and 100 <= code <= 599:
        return "server code %d" % code
    if isinstance(exc, TimeoutError):
        return "timed out"
    return type(exc).__name__


# ----------------------------------------------------------------- validation

def validate_config(config):
    """Check and normalise the mailbox configuration.  No secret belongs here."""
    if not isinstance(config, dict):
        raise MailConfigError("mail configuration must be a dict")
    unknown = sorted(set(config) - set(CONFIG_KEYS))
    if unknown:
        raise MailConfigError("unknown mail configuration key(s): %s (known: %s)"
                              % (", ".join(unknown), ", ".join(CONFIG_KEYS)))
    security = config.get("smtp_security") or "ssl"
    if security not in SECURITIES:
        raise MailConfigError("smtp_security must be one of %s" % ", ".join(SECURITIES))
    try:
        address = mail_messages.address(config.get("address") or "")
    except MailFormatError as exc:
        raise MailConfigError("address: %s" % exc) from None
    username = config.get("username") or address
    if not isinstance(username, str) or not username.strip() or len(username) > 320 \
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in username):
        raise MailConfigError("username must be printable text of at most 320 characters")
    folder = config.get("folder") or "INBOX"
    if not isinstance(folder, str) or not _FOLDER.fullmatch(folder):
        raise MailConfigError("folder must be a plain ASCII mailbox name such as 'INBOX'")
    timeout = config.get("timeout")
    if timeout in (None, ""):
        timeout = DEFAULT_TIMEOUT
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise MailConfigError("timeout must be %d-%d seconds" % (MIN_TIMEOUT, MAX_TIMEOUT))
    return {
        "imap_host": _host(config.get("imap_host"), "imap_host"),
        "imap_port": _port(config.get("imap_port"), "imap_port", DEFAULT_IMAP_PORT),
        "smtp_host": _host(config.get("smtp_host"), "smtp_host"),
        "smtp_port": _port(config.get("smtp_port"), "smtp_port", DEFAULT_SMTP_PORTS[security]),
        "smtp_security": security, "username": username.strip(), "address": address,
        "folder": folder, "timeout": float(timeout),
    }


def _host(value, field):
    if not isinstance(value, str) or not value.strip():
        raise MailConfigError("%s is required; this adapter does not guess a server" % field)
    value = value.strip().rstrip(".").lower()
    if not _HOST.fullmatch(value) or "." not in value:
        raise MailConfigError("%s must be a hostname such as imap.example.com" % field)
    return value


def _port(value, field, default):
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise MailConfigError("%s must be a TCP port between 1 and 65535" % field)
    return value


def _password(credentials):
    """Take the password for this one call.  It is never stored or echoed back."""
    if not isinstance(credentials, dict):
        raise MailConfigError("credentials must be a dict supplied by the host")
    password = credentials.get("password")
    if not isinstance(password, str) or not password or len(password) > 4096:
        raise MailConfigError("credentials['password'] is missing or unusable")
    return password


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MailConfigError("limit must be a positive integer")
    return min(value, MAX_LIMIT)


def _cursor(cursor):
    """``{"uidvalidity": int, "uid": int}`` as this module last returned it."""
    if cursor in (None, "", {}):
        return None, 0
    if not isinstance(cursor, dict):
        raise MailConfigError("cursor must be the dict a previous poll returned")
    uidvalidity, uid = cursor.get("uidvalidity"), cursor.get("uid", 0)
    for name, value in (("uidvalidity", uidvalidity), ("uid", uid)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << 32:
            raise MailConfigError("cursor['%s'] must be a 32-bit unsigned integer" % name)
    return uidvalidity, uid


# ----------------------------------------------------------------- connections

def _tls_context():
    return ssl.create_default_context()


def _tls_active(client):
    """Is this connection encrypted *now*?  Checked before any credential moves.

    Real clients are judged by their socket.  A test transport may instead set
    ``tls_active``, which is how the ordering tests observe that login never
    happens on a cleartext connection.
    """
    if isinstance(getattr(client, "sock", None), ssl.SSLSocket):
        return True
    return bool(getattr(client, "tls_active", False))


def _default_imap(host, port, timeout):
    return imaplib.IMAP4_SSL(host, port, ssl_context=_tls_context(), timeout=timeout)


def _default_smtp(host, port, timeout, security):
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=_tls_context())
    return smtplib.SMTP(host, port, timeout=timeout)


def _shut(client, method):
    if client is None:
        return
    try:
        getattr(client, method)()
    except Exception:       # closing is best-effort and never changes an outcome
        pass


def _imap_connect(cfg, password, factory):
    client = None
    try:
        client = (factory or _default_imap)(cfg["imap_host"], cfg["imap_port"], cfg["timeout"])
        if not _tls_active(client):
            raise MailConnectionError("the IMAP connection is not encrypted; "
                                      "no credentials were sent")
        client.login(cfg["username"], password)
    except MailTransportError:
        _shut(client, "logout")
        raise
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        _shut(client, "logout")
        raise MailConnectionError("could not open an authenticated IMAP session (%s)"
                                  % _why(exc)) from None
    return client


def _smtp_connect(cfg, password, factory):
    """Connect, reach TLS, then authenticate — in that order, always."""
    client = None
    try:
        client = (factory or _default_smtp)(cfg["smtp_host"], cfg["smtp_port"],
                                            cfg["timeout"], cfg["smtp_security"])
        if cfg["smtp_security"] == "starttls":
            client.ehlo()
            if not client.has_extn("starttls"):
                raise MailConnectionError("the SMTP server does not offer STARTTLS; "
                                          "no credentials were sent")
            client.starttls(context=_tls_context())
            client.ehlo()
        if not _tls_active(client):
            raise MailConnectionError("the SMTP connection is not encrypted; "
                                      "no credentials were sent")
        client.login(cfg["username"], password)
    except MailTransportError:
        _shut(client, "quit")
        raise
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        _shut(client, "quit")
        raise MailConnectionError("could not open an authenticated SMTP session (%s); "
                                  "nothing was sent" % _why(exc)) from None
    return client


# ----------------------------------------------------------------------- probe

def probe(config, credentials, *, imap_factory=None, smtp_factory=None):
    """Log in to both servers and stop.  Never sends, and never proves delivery.

    A mailbox that accepts our password can still refuse every outgoing message,
    so ``delivery_proof`` is ``False`` and the caller must not present a green
    probe as "mail works".
    """
    cfg = validate_config(config)
    password = _password(credentials)
    client = _imap_connect(cfg, password, imap_factory)
    try:
        uidvalidity = _select(client, cfg["folder"])
    finally:
        _shut(client, "logout")
    _shut(_smtp_connect(cfg, password, smtp_factory), "quit")
    return {"imap": {"host": cfg["imap_host"], "port": cfg["imap_port"],
                     "folder": cfg["folder"], "uidvalidity": uidvalidity, "tls": True},
            "smtp": {"host": cfg["smtp_host"], "port": cfg["smtp_port"],
                     "security": cfg["smtp_security"], "tls": True},
            "address": cfg["address"], "delivery_proof": False,
            "note": "login and TLS only; no message was sent, so this is not evidence "
                    "that mail can be delivered"}


# ------------------------------------------------------------------ polling

def _ok(typ, what):
    if typ != "OK":
        raise MailConnectionError("the IMAP server refused %s" % what)


def _select(client, folder):
    typ, _ = client.select('"%s"' % folder, readonly=True)
    _ok(typ, "SELECT of the configured folder")
    typ, data = client.response("UIDVALIDITY")
    try:
        uidvalidity = int((data or [b"0"])[0])
    except (TypeError, ValueError):
        raise MailConnectionError("the IMAP server did not report UIDVALIDITY") from None
    if uidvalidity <= 0:
        raise MailConnectionError("the IMAP server did not report UIDVALIDITY")
    return uidvalidity


def _search(client, start):
    typ, data = client.uid("SEARCH", None, "UID", "%d:*" % start)
    _ok(typ, "UID SEARCH")
    uids = set()
    for chunk in data or []:
        if isinstance(chunk, (bytes, bytearray)):
            for token in chunk.split():
                try:
                    uids.add(int(token))
                except ValueError:
                    continue
    # ``n:*`` answers with the last message when nothing is that new, so the
    # lower bound has to be re-applied here or every poll re-reads one message.
    return sorted(uid for uid in uids if uid >= start)


def _meta(client, uids):
    """Size and arrival time for the batch, so an oversize body is never downloaded."""
    if not uids:
        return {}
    typ, data = client.uid("FETCH", ",".join(str(u) for u in uids),
                           "(UID RFC822.SIZE INTERNALDATE)")
    _ok(typ, "UID FETCH of message sizes")
    out = {}
    for item in data or []:
        line = item[0] if isinstance(item, tuple) else item
        if not isinstance(line, (bytes, bytearray)):
            continue
        uid, size = _UID.search(line), _SIZE.search(line)
        if uid and size:
            out[int(uid.group(1))] = (int(size.group(1)), _internaldate(line))
    return out


def _still_present(client, uid):
    """Is this UID still in the mailbox?  A second, narrow SEARCH — no body.

    The question matters because the two reasons a UID has no metadata are
    opposite: it was expunged between SEARCH and FETCH (nothing to record, and
    stepping over it is right), or it is still there and the server's FETCH line
    was not one we could parse (stepping over it would lose real mail silently).
    """
    try:
        typ, data = client.uid("SEARCH", None, "UID", "%d:%d" % (uid, uid))
    except (imaplib.IMAP4.error, OSError):
        return True         # cannot tell: assume it is there and record a refusal
    if typ != "OK":
        return True
    for chunk in data or []:
        if isinstance(chunk, (bytes, bytearray)):
            for token in chunk.split():
                try:
                    if int(token) == uid:
                        return True
                except ValueError:
                    continue
    return False


def _internaldate(line):
    """When the server received it, or None.  Never a guess of "now"."""
    try:
        stamp = imaplib.Internaldate2tuple(bytes(line))
        return time.mktime(stamp) if stamp else None
    except (ValueError, OverflowError, TypeError):
        return None


def _fetch(client, uid):
    typ, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
    _ok(typ, "UID FETCH of a message body")
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def event_id(uidvalidity, uid, folder="INBOX"):
    """A stable id for one delivery, scoped to the mailbox generation it came from.

    UIDs are only unique within a UIDVALIDITY, so both belong in the id; the
    folder is folded in as a short digest because the same UIDVALIDITY may be
    handed out for a different folder on the same account.
    """
    tag = hashlib.sha256(str(folder).encode("utf-8")).hexdigest()[:8]
    return "imap:%s:%d:%d" % (tag, int(uidvalidity), int(uid))


def poll(config, credentials, *, cursor=None, limit=DEFAULT_LIMIT, imap_factory=None):
    """Read up to ``limit`` new messages without changing the mailbox.

    Returns ``{"messages": [...], "cursor": {...}, "more": bool}``.  Each message
    is one flat record: ``status="ok"`` carries the exact ``mail_messages.parse``
    fields (MIME and attachment bytes preserved verbatim), while
    ``status="rejected"`` carries an ``error`` a person can read and a placeholder
    sender, so the host can still record it.  Both wear the same ``event_id``, and
    that is what stops one unreadable message hiding every message behind it.

    A changed UIDVALIDITY invalidates the old UID space entirely, so the scan
    restarts at UID 1 in the new one and says so.  Ids are scoped by UIDVALIDITY,
    so re-seen mail arrives under new ids rather than colliding with old records
    — duplicate work, never a lost or silently merged message.

    **First sync**: with no cursor this starts at UID 1 and works forward
    ``limit`` messages at a time, so a mailbox with years of history is ingested
    in full, a page per poll, until ``more`` is false.  That is the honest
    default (nothing is skipped), but it is rarely what someone connecting an old
    inbox wants: call ``anchor`` first and persist its cursor to mean "from now
    on".  ``first_sync`` in the answer says which of the two happened.

    ``expunged`` lists UIDs that vanished between SEARCH and FETCH and therefore
    have no record at all.  A UID that is still in the mailbox but whose metadata
    could not be read comes back as a ``rejected`` message instead, never as a
    silent step over it.
    """
    cfg = validate_config(config)
    password = _password(credentials)
    limit = _limit(limit)
    want_uidvalidity, want_uid = _cursor(cursor)
    client = _imap_connect(cfg, password, imap_factory)
    try:
        uidvalidity = _select(client, cfg["folder"])
        rebased = want_uidvalidity is not None and want_uidvalidity != uidvalidity
        start = want_uid + 1 if (not rebased and want_uidvalidity is not None) else 1
        found = _search(client, start)
        more = len(found) > limit
        batch = found[:limit]
        meta = _meta(client, batch)
        messages, budget, last, expunged = [], MAX_POLL_BYTES, start - 1, []
        for uid in batch:
            if uid not in meta:
                retry = _meta(client, [uid])        # one targeted re-FETCH, no body
                if uid in retry:
                    meta[uid] = retry[uid]
                elif not _still_present(client, uid):
                    expunged.append(uid)            # really gone; nothing to record
                    last = uid
                    continue
                else:
                    # Still in the mailbox, but we cannot read its metadata.  A
                    # rejected row under the same id keeps it visible and lets the
                    # cursor move; skipping it would lose the message in silence.
                    messages.append(_rejected(
                        {"event_id": event_id(uidvalidity, uid, cfg["folder"]), "uid": uid,
                         "uidvalidity": uidvalidity, "size": 0, "received_at": None},
                        cfg, "the server did not report a usable size or date for this "
                             "message, and it is still in the mailbox; it was left "
                             "unread and nothing was fetched"))
                    last = uid
                    continue
            size, received_at = meta[uid]
            if size > budget and messages:
                more = True         # keep the poll bounded; the rest wait for next time
                break
            row = _one(client, cfg, uidvalidity, uid, size, received_at)
            budget -= max(0, size)
            last = uid
            messages.append(row)
        return {"messages": messages, "more": more,
                "cursor": {"uidvalidity": uidvalidity, "uid": max(last, 0)},
                "uidvalidity_changed": rebased, "folder": cfg["folder"],
                "expunged": expunged, "first_sync": want_uidvalidity is None}
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailConnectionError("the mailbox could not be read (%s); "
                                  "nothing was marked read" % _why(exc)) from None
    finally:
        _shut(client, "logout")


def anchor(config, credentials, *, imap_factory=None):
    """A cursor meaning "whatever arrives after now".  Reads nothing else.

    Onboarding an inbox that has existed for years should not mean sweeping
    years of it into someone's task list.  This opens the folder read-only, asks
    the server where the UID space currently ends, and hands back the cursor a
    poll would have had if it had just finished reading everything.

    No message is fetched, no flag is set and nothing is marked ``\\Seen``.  The
    trade is explicit and the caller owns it: mail already in the folder will
    never be polled under this cursor.
    """
    cfg = validate_config(config)
    password = _password(credentials)
    client = _imap_connect(cfg, password, imap_factory)
    try:
        uidvalidity = _select(client, cfg["folder"])
        uid = _highest_uid(client)
        return {"cursor": {"uidvalidity": uidvalidity, "uid": uid},
                "uidvalidity": uidvalidity, "uid": uid, "folder": cfg["folder"],
                "note": "a starting point only; nothing was read, fetched or marked "
                        "read, and mail already in this folder will not be polled"}
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailConnectionError("the mailbox could not be read (%s); "
                                  "nothing was marked read" % _why(exc)) from None
    finally:
        _shut(client, "logout")


def _highest_uid(client):
    """Where the UID space ends right now, from UIDNEXT or one ``*`` search.

    UIDNEXT comes free with SELECT on most servers.  The fallback asks for the
    last message only (``UID SEARCH UID *:*``) rather than listing every UID in
    a mailbox that may hold a hundred thousand of them.
    """
    try:
        typ, data = client.response("UIDNEXT")
        if typ == "UIDNEXT" or data:
            value = int((data or [b"0"])[0])
            if value > 0:
                return value - 1
    except (TypeError, ValueError, IndexError, imaplib.IMAP4.error):
        pass
    typ, data = client.uid("SEARCH", None, "UID", "*:*")
    _ok(typ, "UID SEARCH")
    highest = 0
    for chunk in data or []:
        if isinstance(chunk, (bytes, bytearray)):
            for token in chunk.split():
                try:
                    highest = max(highest, int(token))
                except ValueError:
                    continue
    return highest


def _rejected(base, cfg, reason):
    """A refusal the host can still record: same id, a reason, no invented content.

    The sender is an explicit unusable placeholder rather than a guess, because a
    plausible-looking address on a message we could not read is worse than an
    obviously empty one.
    """
    return dict(base, status="rejected", error=reason, sender=UNREADABLE_SENDER,
                recipient=cfg["address"], subject="Unreadable email", text="",
                message_id="", in_reply_to=[], references=[], attachments=[],
                automatic=False)


def _one(client, cfg, uidvalidity, uid, size, received_at):
    """One message, or a describable refusal wearing the same id."""
    base = {"event_id": event_id(uidvalidity, uid, cfg["folder"]), "uid": uid,
            "uidvalidity": uidvalidity, "size": size, "received_at": received_at}
    if size > MAX_MAIL_BYTES:
        return _rejected(base, cfg, "this message is %d bytes; the limit is %d and "
                                    "nothing was truncated" % (size, MAX_MAIL_BYTES))
    raw = _fetch(client, uid)
    if not raw:
        return _rejected(base, cfg, "the server returned no body for this message")
    try:        # parse() re-checks the real byte count, so a lying SIZE cannot slip past.
                # The recipient is the mailbox we polled, not whatever To: claimed.
        mail = mail_messages.parse(raw, envelope_to=cfg["address"])
    except MailFormatError as exc:
        return _rejected(base, cfg, str(exc))
    return dict(base, status="ok", **mail)


# -------------------------------------------------------------------- sending

def derive_message_id(address, result_id):
    """The same result id always yields the same Message-ID.

    That is what makes a retry recognisable as a repeat rather than a second
    letter: the recipient's server sees the id it already has.
    """
    domain = address.rsplit("@", 1)[1]
    seed = "collie-mail/v1|%s|%s" % (address, result_id)
    return "<collie-%s@%s>" % (hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32], domain)


def _thread_ids(result):
    in_reply_to = result.get("in_reply_to") or ""
    if in_reply_to and not _MESSAGE_ID.fullmatch(in_reply_to):
        raise MailConfigError("in_reply_to must be a Message-ID in angle brackets; "
                              "nothing was sent")
    references = result.get("references")
    if references is None:
        references = (result.get("metadata") or {}).get("references")
    if references is None:
        references = [in_reply_to] if in_reply_to else []
    if not isinstance(references, (list, tuple)):
        raise MailConfigError("references must be a list of Message-IDs")
    refs = [str(v) for v in references]
    if any(not _MESSAGE_ID.fullmatch(v) for v in refs):
        raise MailConfigError("references must all be Message-IDs in angle brackets")
    return in_reply_to, refs


def _compose(cfg, result):
    if not isinstance(result, dict):
        raise MailConfigError("result must be the dict communications.claim_send returned")
    result_id = result.get("id")
    if not isinstance(result_id, str) or not result_id.strip() or len(result_id) > 96:
        raise MailConfigError("result['id'] must be the stored result id")
    message_id = result.get("message_id") or derive_message_id(cfg["address"], result_id)
    in_reply_to, refs = _thread_ids(result)
    try:
        recipient = mail_messages.address(result.get("destination") or "")
        message = mail_messages.compose(
            sender=cfg["address"], recipient=recipient,
            subject=result.get("subject") or "", text=result.get("text") or "",
            message_id=message_id, in_reply_to=in_reply_to, references=refs)
    except MailFormatError as exc:
        raise MailConfigError("%s; nothing was sent" % exc) from None
    return message, message_id, recipient


def send(config, credentials, result, *, smtp_factory=None):
    """Send one stored result to its one destination.

    Returns ``{"provider_message_id": <Message-ID>, "status": "submitted"}`` only
    after the server accepted DATA.  Everything else raises, and the class of the
    exception is the caller's answer to "may this be retried?":

    * ``MailConfigError`` / ``MailConnectionError`` — nothing was transmitted;
      fix and retry.
    * ``MailRefused`` — the server said no, with a number; definitely not
      delivered.  Only an explicit 4xx/5xx SMTP reply earns this.
    * ``MailDeliveryUnknown`` — it may have gone out.  Never auto-retry this.
      This includes a final DATA reply we could not read a code out of: the body
      was already transmitted by then.

    A failing ``QUIT`` after an accepted DATA is not a failure: the message is
    already the server's problem, and treating a dropped goodbye as a failure is
    how one reply becomes two.
    """
    cfg = validate_config(config)
    password = _password(credentials)
    message, message_id, recipient = _compose(cfg, result)
    if len(message.as_bytes()) > MAX_MAIL_BYTES:
        raise MailConfigError("the composed message exceeds %d bytes; nothing was sent"
                              % MAX_MAIL_BYTES)
    client = _smtp_connect(cfg, password, smtp_factory)
    try:
        try:
            # Recipients are passed explicitly, so no header can widen delivery.
            refused = client.send_message(message, from_addr=cfg["address"],
                                          to_addrs=[recipient])
        except smtplib.SMTPDataError as exc:
            # smtplib raises this class on both sides of the body: for a refused
            # DATA *command* (nothing transmitted) and for the final reply, which
            # arrives after the whole message has gone down the wire
            # (Lib/smtplib.py sendmail).  A numeric 4xx/5xx there is the server
            # saying it did not take the message.  Anything else — notably the -1
            # getreply() substitutes when the final line has no parsable code — is
            # a message that may well be queued, and calling that a clean refusal
            # is how one reply gets sent twice.
            code = getattr(exc, "smtp_code", None)
            if isinstance(code, int) and 400 <= code <= 599:
                raise MailRefused("the server refused this message (%s); it was not sent"
                                  % _why(exc)) from None
            raise MailDeliveryUnknown(
                "the server did not answer the message data with a usable reply code "
                "(%s); whether it was delivered is unknown and it must not be resent "
                "automatically" % _why(exc)) from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                smtplib.SMTPHeloError, smtplib.SMTPNotSupportedError) as exc:
            raise MailRefused("the server refused this message (%s); it was not sent"
                              % _why(exc)) from None
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise MailDeliveryUnknown(
                "the connection failed while sending (%s); whether the message was "
                "delivered is unknown and it must not be resent automatically"
                % _why(exc)) from None
        if refused:
            raise MailDeliveryUnknown(
                "the server accepted the message but reported a refused recipient; "
                "delivery is unknown")
    finally:
        _shut(client, "quit")
    return {"provider_message_id": message_id, "status": "submitted",
            "delivery_unknown": False, "recipients": 1}
