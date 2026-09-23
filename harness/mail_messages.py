"""Bounded MIME parsing for received work; no remote content is fetched."""
from __future__ import annotations

import base64
import binascii
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
from html.parser import HTMLParser
import re

MAX_MAIL_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 128_000
MAX_TEXT_BYTES = 64 * 1024
MAX_ATTACHMENTS = 16
MAX_PARTS = 64
_MESSAGE_ID = re.compile(r"<[^<>\s\x00-\x1f]{1,250}>")


class MailFormatError(ValueError):
    pass


class _ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def address(value):
    """One mailbox only; callers decide whether this identity is authorized."""
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise MailFormatError("invalid email address")
    try:
        rows = getaddresses([value])
    except ValueError:
        raise MailFormatError("invalid email address") from None
    if len(rows) != 1:
        raise MailFormatError("provide one email address")
    result = rows[0][1]
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", result) or len(result) > 254:
        raise MailFormatError("invalid email address")
    local, domain = result.rsplit("@", 1)
    return local + "@" + domain.lower()


def message_ids(value):
    value = str(value or "")
    if len(value.encode("utf-8")) > 6000:
        raise MailFormatError("email thread headers are too large")
    ids = _MESSAGE_ID.findall(value)
    if len(ids) > 32:
        raise MailFormatError("email thread contains too many references")
    return ids


def _text(part):
    try:
        value = part.get_content()
    except (LookupError, UnicodeError, ValueError):
        raw = part.get_payload(decode=True) or b""
        value = raw.decode("utf-8", "replace")
    if not isinstance(value, str):
        return ""
    if part.get_content_type() == "text/html":
        parser = _ReadableHTML()
        parser.feed(value)
        value = "".join(parser.parts)
    if len(value) > MAX_TEXT_CHARS or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise MailFormatError("email text exceeds the supported size; nothing was truncated")
    return value.strip()


def parse(raw, *, envelope_from="", envelope_to=""):
    if not isinstance(raw, bytes) or not raw:
        raise MailFormatError("email must contain MIME bytes")
    if len(raw) > MAX_MAIL_BYTES:
        raise MailFormatError("email exceeds 8 MiB; nothing was truncated")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    parts = list(message.walk())
    if len(parts) > MAX_PARTS:
        raise MailFormatError("email contains too many MIME parts")
    if any(part.defects for part in parts):
        raise MailFormatError("email MIME structure is incomplete or invalid")
    body = message.get_body(preferencelist=("plain", "html"))
    text = _text(body) if body is not None else ""
    attachments = []
    for part in message.iter_attachments():
        if part.is_multipart():
            raise MailFormatError("attached nested emails are not supported; forward their text instead")
        data = part.get_payload(decode=True)
        if data is None:
            raise MailFormatError("email attachment could not be decoded")
        if part.defects:
            raise MailFormatError("email attachment encoding is invalid")
        name = str(part.get_filename() or "attachment")
        name = re.sub(r"[\\/\x00-\x1f\x7f]", "_", name).strip()[:180] or "attachment"
        name = name.encode("utf-8")[:240].decode("utf-8", "ignore") or "attachment"
        attachments.append({"name": name, "content_type": part.get_content_type(),
                            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "data": base64.b64encode(data).decode("ascii")})
        if len(attachments) > MAX_ATTACHMENTS:
            raise MailFormatError("email contains more than 16 attachments")
    subject = str(message.get("Subject") or "")
    if len(subject.encode("utf-8")) > 2000:
        raise MailFormatError("email subject is too long")
    ids = message_ids(message.get("Message-ID"))
    if len(ids) > 1 or len(message.get_all("Message-ID", [])) > 1:
        raise MailFormatError("email has ambiguous Message-ID headers")
    sender = envelope_from or str(message.get("From") or "")
    recipient = envelope_to or str(message.get("To") or "")
    return {
        "sender": address(sender), "recipient": address(recipient),
        "subject": subject, "text": text, "message_id": ids[0] if ids else "",
        "in_reply_to": message_ids(message.get("In-Reply-To")),
        "references": message_ids(message.get("References")), "attachments": attachments,
        "automatic": (str(message.get("Auto-Submitted") or "no").lower() != "no"
                      or str(message.get("Precedence") or "").lower() in {"bulk", "list", "junk"}
                      or bool(message.get("List-Id"))
                      or message.get("Return-Path") == "<>"),
    }


def from_relay(row):
    if not isinstance(row, dict):
        raise MailFormatError("invalid relay mail record")
    if row.get("truncated"):
        raise MailFormatError("the mail relay truncated this message; resend a smaller message")
    if row.get("error"):
        raise MailFormatError("the received mail could not be decrypted")
    raw = row.get("raw")
    if isinstance(raw, str) and raw:
        if len(raw) > (MAX_MAIL_BYTES * 4 // 3 + 8):
            raise MailFormatError("email exceeds the supported size")
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error):
            raise MailFormatError("relay MIME payload is not valid base64") from None
        return parse(decoded, envelope_from=str(row.get("from") or ""),
                     envelope_to=str(row.get("to") or ""))
    # Older fixtures and service-generated messages can carry plain text.
    text = row.get("text") or ""
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise MailFormatError("email has no supported body")
    return {"sender": address(str(row.get("from") or "")),
            "recipient": address(str(row.get("to") or "")),
            "subject": str(row.get("subject") or ""), "text": text,
            "message_id": "", "in_reply_to": [], "references": [],
            "attachments": [], "automatic": False}


def compose(*, sender, recipient, subject, text, message_id, in_reply_to="", references=()):
    """Build one result email with a stable id; no recipient expansion or HTML."""
    sender, recipient = address(sender), address(recipient)
    if not isinstance(subject, str) or len(subject.encode("utf-8")) > 2048 or "\r" in subject or "\n" in subject:
        raise MailFormatError("invalid result email subject")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise MailFormatError("result email body is empty or too large")
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        raise MailFormatError("invalid result message id")
    refs = list(references)
    if len(refs) > 32 or any(not isinstance(v, str) or not _MESSAGE_ID.fullmatch(v) for v in refs):
        raise MailFormatError("invalid email references")
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"] = sender, recipient
    message["Subject"], message["Message-ID"] = subject, message_id
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"
    if in_reply_to:
        if not isinstance(in_reply_to, str) or not _MESSAGE_ID.fullmatch(in_reply_to):
            raise MailFormatError("invalid reply message id")
        message["In-Reply-To"] = in_reply_to
    if refs:
        message["References"] = " ".join(refs)
    message.set_content(text)
    return message
