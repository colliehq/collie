import base64
import email.policy
import re
from email.message import EmailMessage

import pytest

from harness import mail_messages as mail


def _message():
    message = EmailMessage()
    message["From"] = "Owner <owner@example.test>"
    message["To"] = "Collie <collie@example.test>"
    message["Subject"] = "整理附件"
    message["Message-ID"] = "<request-1@example.test>"
    message.set_content("请把资料整理成比较表。")
    return message


def test_relay_mime_preserves_unicode_and_attachment_bytes():
    message = _message()
    message.add_attachment("名称,价格\n产品一,100\n".encode(), maintype="text", subtype="csv", filename="资料.csv")
    record = mail.from_relay({"from": "owner@example.test", "to": "collie@example.test",
                             "raw": base64.b64encode(message.as_bytes()).decode()})
    assert record["subject"] == "整理附件"
    assert record["text"] == "请把资料整理成比较表。"
    assert record["message_id"] == "<request-1@example.test>"
    attachment = record["attachments"][0]
    assert attachment["name"] == "资料.csv"
    assert base64.b64decode(attachment["data"]).decode() == "名称,价格\n产品一,100\n"


def test_body_limit_counts_utf8_bytes_and_duplicate_message_id_is_refused():
    message = _message()
    message.set_content("测" * 23000)
    with pytest.raises(mail.MailFormatError, match="size"):
        mail.parse(message.as_bytes())
    raw = _message().as_bytes().replace(b"Message-ID:", b"Message-ID: <another@example.test>\nMessage-ID:")
    with pytest.raises(mail.MailFormatError, match="ambiguous"):
        mail.parse(raw)


def test_long_unicode_subject_can_be_replied_to_and_filename_fits_durable_metadata():
    message = _message()
    message.replace_header("Subject", "测" * 650)
    message.add_attachment(b"data", maintype="text", subtype="plain", filename="测" * 170 + ".txt")
    parsed = mail.parse(message.as_bytes())
    assert len(parsed["attachments"][0]["name"].encode()) <= 240
    result = mail.compose(sender=parsed["recipient"], recipient=parsed["sender"],
                          subject="Re: " + parsed["subject"], text="Result", message_id="<reply@example.test>")
    assert result["Subject"] == "Re: " + parsed["subject"]


def test_html_is_readable_text_without_loading_remote_images():
    message = _message()
    message.set_content('<html><head><style>hidden</style></head><body><p>Keep this</p>'
                        '<script>doNotRun()</script><img src="https://example.test/tracker">'
                        '<p>Second paragraph</p></body></html>', subtype="html")
    record = mail.parse(message.as_bytes())
    assert "Keep this" in record["text"] and "Second paragraph" in record["text"]
    assert "doNotRun" not in record["text"] and "hidden" not in record["text"]
    assert "tracker" not in record["text"]


def test_truncated_and_invalid_mime_are_not_accepted_as_complete():
    with pytest.raises(mail.MailFormatError, match="truncated"):
        mail.from_relay({"truncated": True, "raw": "anything"})
    with pytest.raises(mail.MailFormatError, match="structure"):
        mail.parse(b'From: owner@example.test\nTo: collie@example.test\n'
                   b'Content-Type: multipart/mixed; boundary="missing"\n\nno boundary')


@pytest.mark.parametrize("header,value", [("Auto-Submitted", "auto-replied"),
                                          ("Precedence", "bulk"), ("Return-Path", "<>")])
def test_service_mail_does_not_start_an_auto_reply_loop(header, value):
    message = _message()
    message[header] = value
    assert mail.parse(message.as_bytes())["automatic"]


def test_follow_up_and_result_retain_the_original_thread():
    message = _message()
    message["In-Reply-To"] = "<result-0@example.test>"
    message["References"] = "<request-0@example.test> <result-0@example.test>"
    record = mail.parse(message.as_bytes())
    assert record["in_reply_to"] == ["<result-0@example.test>"]
    result = mail.compose(sender="collie@example.test", recipient="owner@example.test",
                          subject="Re: 整理附件", text="已完成。", message_id="<result-1@example.test>",
                          in_reply_to=record["message_id"], references=record["references"])
    parsed = mail.parse(result.as_bytes())
    assert parsed["in_reply_to"] == ["<request-1@example.test>"]
    assert parsed["text"] == "已完成。"
    assert parsed["automatic"]


def test_injected_recipient_header_cannot_expand_result_audience():
    with pytest.raises(mail.MailFormatError):
        mail.compose(sender="collie@example.test", recipient="owner@example.test\r\nBcc: other@example.test",
                     subject="Result", text="Private result", message_id="<result@example.test>")
    with pytest.raises(mail.MailFormatError):
        mail.address("owner@example.test, other@example.test")


def test_oversize_mail_is_rejected_before_parsing(monkeypatch):
    monkeypatch.setattr(mail, "MAX_MAIL_BYTES", 100)
    with pytest.raises(mail.MailFormatError, match="nothing was truncated"):
        mail.parse(_message().as_bytes())


# ------------------------------------------------------------ HTML results

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                       "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
HTML = ('<!doctype html><html><body><p>早上好 — two quick ones.</p>'
        '<img alt="" src="cid:collie-avatar"></body></html>')
AVATAR = {"cid": "collie-avatar", "filename": "collie.png", "content_type": "image/png",
          "data": PNG}


def _compose(**extra):
    return mail.compose(sender="collie@example.test", recipient="owner@example.test",
                        subject="Two quick ones · Tue 29 Sep", text="早上好 — two quick ones.\n",
                        message_id="<report-1@example.test>", **extra)


def _plain_only_reference():
    """The text-only message exactly as compose() built it before HTML existed."""
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"], message["To"] = "collie@example.test", "owner@example.test"
    message["Subject"], message["Message-ID"] = "Two quick ones · Tue 29 Sep", "<report-1@example.test>"
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content("早上好 — two quick ones.\n")
    return message


def test_a_text_only_result_is_byte_identical_to_before():
    reference = _plain_only_reference().as_bytes()
    assert _compose().as_bytes() == reference
    # Asking for "no HTML" in every way a caller can say it changes nothing either.
    assert _compose(html="", inline=()).as_bytes() == reference
    assert _compose(html=None, inline=[]).as_bytes() == reference
    assert _compose().get_content_type() == "text/plain"


def test_html_with_an_inline_image_is_alternative_then_related():
    message = _compose(html=HTML, inline=[AVATAR])
    assert message.get_content_type() == "multipart/alternative"
    plain, related = message.get_payload()
    # The plain text comes first: a client that shows the last alternative it can
    # render shows the HTML, and one that cannot still has the whole report.
    assert plain.get_content_type() == "text/plain"
    assert plain.get_content_charset() == "utf-8"
    assert plain.get_content() == "早上好 — two quick ones.\n"
    assert related.get_content_type() == "multipart/related"
    assert related.get_param("type") == "text/html"
    page, image = related.get_payload()
    assert page.get_content_type() == "text/html" and page.get_content_charset() == "utf-8"
    assert page.get_content() == HTML + "\n"         # a text part always ends its last line
    assert image.get_content_type() == "image/png"
    assert image["Content-ID"] == "<collie-avatar>"
    assert image.get_content_disposition() == "inline"
    assert image.get_filename() == "collie.png"
    assert image.get_payload(decode=True) == PNG
    # Every cid: the HTML names is a part of this message, and every part is named.
    named = set(re.findall(r'src="cid:([^"]+)"', page.get_content()))
    assert named == {part["Content-ID"].strip("<>") for part in related.get_payload()[1:]}
    # Threading and loop-safety headers are the same as a plain result's.
    assert message["Auto-Submitted"] == "auto-replied"
    assert message["Message-ID"] == "<report-1@example.test>"
    # Our own parser reads the plain alternative back, whole.
    parsed = mail.parse(message.as_bytes())
    assert parsed["text"] == "早上好 — two quick ones."


def test_html_without_images_is_a_plain_alternative():
    message = _compose(html="<p>No dog today.</p>")
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.get_payload()] == ["text/plain", "text/html"]


@pytest.mark.parametrize("html, inline, why", [
    ("<p>no image here</p>", [AVATAR], "not referenced"),
    ('<img src="cid:someone-else">', [AVATAR], "no inline part"),
    ('<img src="cid:collie-avatar">', [], "no inline part"),
    ("", [AVATAR], "only with an HTML body"),
    (HTML, [AVATAR, AVATAR], "twice"),
    (HTML, [dict(AVATAR, content_type="image/svg+xml")], "PNG, JPEG or GIF"),
    (HTML, [dict(AVATAR, cid="bad id")], "content id"),
    (HTML, [dict(AVATAR, filename="../../evil.png")], "file name"),
    (HTML, [dict(AVATAR, data="not bytes")], "bytes"),
    (HTML, [dict(AVATAR, data=b"")], "bytes"),
    ("<p>\x00</p>", [], "control"),
])
def test_an_html_result_that_does_not_add_up_is_refused(html, inline, why):
    with pytest.raises(mail.MailFormatError, match=why):
        _compose(html=html, inline=inline)


@pytest.mark.parametrize("words", ["Lucid: Gravity deliveries begin",
                                   "Postgres ACID: what changed in 18",
                                   "Handle cid:image001.png in replies",
                                   'Write src=&quot;cid:logo&quot; in your template'])
def test_cid_in_the_words_of_a_page_is_not_an_image_reference(words):
    # Only a cid: that is the value of a src or background attribute names an image;
    # the same letters in a headline are words, and must not turn a page into a refusal.
    page = '<p>%s</p><img alt="" src="cid:collie-avatar">' % words
    message = _compose(html=page, inline=[AVATAR])
    assert message.get_content_type() == "multipart/alternative"
    assert _compose(html="<p>%s</p>" % words).get_content_type() == "multipart/alternative"


def test_every_attribute_form_of_a_cid_reference_counts():
    for page in ('<img src="cid:collie-avatar">', "<img src='cid:collie-avatar'>",
                 '<IMG SRC = "CID:collie-avatar">', '<td background="cid:collie-avatar"></td>'):
        assert _compose(html=page, inline=[AVATAR]).is_multipart(), page
    with pytest.raises(mail.MailFormatError, match="no inline part"):
        _compose(html='<p>Lucid: fine</p><img src="cid:ghost">')


def test_html_and_images_are_bounded():
    with pytest.raises(mail.MailFormatError, match="HTML"):
        _compose(html="<p>" + "x" * mail.MAX_HTML_BYTES + "</p>")
    big = dict(AVATAR, data=b"\x89PNG" + b"\0" * mail.MAX_INLINE_BYTES)
    with pytest.raises(mail.MailFormatError, match="image"):
        _compose(html=HTML, inline=[big])
    many = [dict(AVATAR, cid="dog-%d" % n) for n in range(mail.MAX_INLINE_IMAGES + 1)]
    page = "".join('<img src="cid:dog-%d">' % n for n in range(mail.MAX_INLINE_IMAGES + 1))
    with pytest.raises(mail.MailFormatError, match="images"):
        _compose(html=page, inline=many)


def test_inline_parts_survive_a_round_trip_through_storage():
    stored = mail.encode_inline([AVATAR])
    assert stored == [{"cid": "collie-avatar", "filename": "collie.png",
                       "content_type": "image/png", "data": base64.b64encode(PNG).decode("ascii"),
                       "bytes": len(PNG)}]
    assert mail.decode_inline(stored) == [AVATAR]
    with pytest.raises(mail.MailFormatError):
        mail.decode_inline([dict(stored[0], data="%%%not-base64")])
    with pytest.raises(mail.MailFormatError):
        mail.decode_inline([dict(stored[0], bytes=len(PNG) + 1)])
