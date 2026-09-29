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
#: The receiving server's authentication verdict, kept only as long as it is useful.
MAX_AUTH_RESULTS = 2000
#: An HTML result is a designed page, not a document: Gmail clips a body past ~102 KB, so
#: anything near this ceiling is already a page nobody reads whole.
MAX_HTML_BYTES = 128 * 1024
#: Inline images are decoration (a dog's face), referenced from the HTML as ``cid:``.
MAX_INLINE_IMAGES = 4
MAX_INLINE_BYTES = 64 * 1024
MAX_INLINE_TOTAL = 128 * 1024
INLINE_TYPES = ("image/png", "image/jpeg", "image/gif")
_MESSAGE_ID = re.compile(r"<[^<>\s\x00-\x1f]{1,250}>")
_CID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_INLINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
#: A ``cid:`` reference is the quoted value of a ``src`` or ``background`` attribute and
#: nothing else: "Lucid:" or "ACID:" in a headline are words.  Escaped text cannot match,
#: because the renderer writes a quote inside text as ``&quot;``.  relay/mail_worker.js
#: applies the same pattern, so both halves agree on what a page references.
_CID_REF = re.compile(r"""\s(?:src|background)\s*=\s*(?:"\s*cid:([^"]*)"|'\s*cid:([^']*)')""",
                      re.IGNORECASE)
_HTML_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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
    # Only the topmost Authentication-Results: the receiving server prepends its own
    # verdict, and any such header a sender wrote into the message sits below it.
    verdicts = message.get_all("Authentication-Results", [])
    verdict = " ".join(str(verdicts[0]).split())[:MAX_AUTH_RESULTS] if verdicts else ""
    return {
        "sender": address(sender), "recipient": address(recipient),
        "authentication_results": verdict,
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
            "recipient": address(str(row.get("to") or "")), "authentication_results": "",
            "subject": str(row.get("subject") or ""), "text": text,
            "message_id": "", "in_reply_to": [], "references": [],
            "attachments": [], "automatic": False}


def check_html(html):
    """An HTML body as a result may carry it: text, bounded, no control characters.

    The HTML is not sanitised here -- the caller renders it from its own templates and
    escapes what it puts in.  What this refuses is what no template should produce: a
    body past the size a mail client will show, and bytes that are not text.
    """
    if not isinstance(html, str) or not html.strip():
        raise MailFormatError("the HTML body is empty")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise MailFormatError("the HTML body exceeds %d bytes" % MAX_HTML_BYTES)
    if _HTML_CONTROL.search(html):
        raise MailFormatError("the HTML body contains control characters")
    return html


def cid_references(html):
    """The content ids a page shows, from its ``src``/``background`` attributes only."""
    return {first or second for first, second in _CID_REF.findall(html or "")}


def check_inline(parts, html):
    """The inline images of an HTML result, validated against the HTML that shows them.

    Each part is ``{"cid", "filename", "content_type", "data": bytes}``.  Every image must
    be named by a ``cid:`` in the HTML and every ``cid:`` in the HTML must be one of these
    parts: an unnamed image turns into an attachment in some clients, and a name with no
    image is a broken picture in all of them.
    """
    parts = list(parts or ())
    if parts and not html:
        raise MailFormatError("inline images are allowed only with an HTML body")
    if len(parts) > MAX_INLINE_IMAGES:
        raise MailFormatError("a result carries at most %d inline images" % MAX_INLINE_IMAGES)
    clean, seen, total = [], set(), 0
    for part in parts:
        if not isinstance(part, dict):
            raise MailFormatError("an inline image must be an object")
        cid, name = part.get("cid"), part.get("filename")
        kind, data = part.get("content_type"), part.get("data")
        if not isinstance(cid, str) or not _CID.fullmatch(cid):
            raise MailFormatError("an inline image needs a plain content id")
        if cid in seen:
            raise MailFormatError("the inline image %s is given twice" % cid)
        if not isinstance(name, str) or not _INLINE_NAME.fullmatch(name):
            raise MailFormatError("an inline image needs a plain file name")
        if kind not in INLINE_TYPES:
            raise MailFormatError("an inline image must be PNG, JPEG or GIF")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise MailFormatError("an inline image must carry its bytes")
        if len(data) > MAX_INLINE_BYTES:
            raise MailFormatError("an inline image exceeds %d bytes" % MAX_INLINE_BYTES)
        total += len(data)
        if total > MAX_INLINE_TOTAL:
            raise MailFormatError("the inline images exceed %d bytes together" % MAX_INLINE_TOTAL)
        seen.add(cid)
        clean.append({"cid": cid, "filename": name, "content_type": kind, "data": bytes(data)})
    named = cid_references(html)
    dangling = sorted(named - seen)
    if dangling:
        raise MailFormatError("the HTML shows cid:%s, and there is no inline part by that name"
                              % dangling[0][:64])
    unnamed = sorted(seen - named)
    if unnamed:
        raise MailFormatError("the inline image %s is not referenced by the HTML" % unnamed[0])
    return clean


def encode_inline(parts, html=None):
    """Inline images as a JSON store keeps them: base64 text plus the decoded size."""
    checked = check_inline(parts, html if html is not None else _showing(
        part.get("cid") for part in parts or () if isinstance(part, dict)))
    return [{"cid": part["cid"], "filename": part["filename"],
             "content_type": part["content_type"],
             "data": base64.b64encode(part["data"]).decode("ascii"), "bytes": len(part["data"])}
            for part in checked]


def decode_inline(stored, html=None):
    """The stored form back to bytes, refusing anything that does not decode to what it says."""
    parts = []
    for row in stored or ():
        if not isinstance(row, dict) or not isinstance(row.get("data"), str):
            raise MailFormatError("a stored inline image is malformed")
        try:
            data = base64.b64decode(row["data"].encode("ascii"), validate=True)
        except (ValueError, binascii.Error, UnicodeEncodeError):
            raise MailFormatError("a stored inline image is not valid base64") from None
        if row.get("bytes") != len(data):
            raise MailFormatError("a stored inline image does not match its recorded size")
        parts.append({"cid": row.get("cid"), "filename": row.get("filename"),
                      "content_type": row.get("content_type"), "data": data})
    return check_inline(parts, html if html is not None else _showing(
        part["cid"] for part in parts))


def _showing(cids):
    """A stand-in page that shows exactly these images, for checking parts on their own."""
    return "".join('<img src="cid:%s">' % cid for cid in cids)


def compose(*, sender, recipient, subject, text, message_id, in_reply_to="", references=(),
            html="", inline=()):
    """Build one result email with a stable id and exactly one recipient.

    With no ``html`` this is the plain-text message it has always been, byte for byte.
    With ``html`` it is ``multipart/alternative``: the plain text first, then the HTML --
    wrapped with its ``inline`` images (``cid:`` parts) in a ``multipart/related`` when
    there are any.  The plain text is never optional: it is what a client that shows no
    HTML reads, and what a reply to the message is answered against.
    """
    html = html or ""
    inline = list(inline or ())
    if html:
        html = check_html(html)
    parts = check_inline(inline, html) if (inline or html) else []
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
    if html:
        message.add_alternative(html, subtype="html")
        if parts:
            page = message.get_payload()[1]
            for part in parts:
                maintype, subtype = part["content_type"].split("/", 1)
                page.add_related(part["data"], maintype=maintype, subtype=subtype,
                                 cid="<%s>" % part["cid"], filename=part["filename"],
                                 disposition="inline")
            # RFC 2387: a related body names the type of its root part.
            page.set_param("type", "text/html")
    return message
