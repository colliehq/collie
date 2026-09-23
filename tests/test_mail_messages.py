import base64
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
