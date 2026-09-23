"""Wire-behaviour tests for the IMAP/SMTP adapter.

The failures worth a test here are the ones that cost something real: a password
put on a cleartext socket, a mailbox modified by a read, one unparseable message
hiding every message behind it, an ambiguous send retried into two deliveries,
and a dropped ``QUIT`` recorded as a failure after the server already took the
mail.

No socket is opened anywhere in this file.  Most tests inject fakes that answer
like ``imaplib``/``smtplib`` and record the order they were driven in, which is
the only way to assert "TLS happened *before* the credential moved".  The DATA
tests are different: they run the *real* ``smtplib.SMTP`` over a scripted socket
object, because which side of the message body CPython raises ``SMTPDataError``
on is the whole question there, and a fake client cannot answer it.
"""
import email.message
import smtplib

import pytest

from harness import mail_transport as mt
from harness.mail_messages import MAX_MAIL_BYTES

CONFIG = {"imap_host": "imap.example.com", "smtp_host": "smtp.example.com",
          "address": "dog@example.com", "folder": "INBOX"}
CREDS = {"password": "hunter2-correct-horse"}


def mime(uid=1, sender="owner@example.com", subject="hello", body="please look at this",
         extra=""):
    return ("From: %s\r\nTo: dog@example.com\r\nSubject: %s\r\n"
            "Message-ID: <m%d@example.com>\r\n%s"
            "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n%s\r\n"
            % (sender, subject, uid, extra, body)).encode("utf-8")


class FakeIMAP:
    """Answers the handful of imaplib calls the adapter makes, and logs them."""

    def __init__(self, messages=None, uidvalidity=1000, *, tls=True, sizes=None,
                 hide_meta=(), vanish=(), uidnext=None):
        self.messages = dict(messages or {})
        self.sizes = dict(sizes or {})
        self.uidvalidity = uidvalidity
        self.tls_active = tls
        self.calls = []
        self.bodies_fetched = []
        # ``hide_meta``: present in SEARCH, never described by the metadata FETCH.
        # ``vanish``: in the first SEARCH and gone from the mailbox afterwards.
        self.hide_meta = set(hide_meta)
        self.vanish = set(vanish)
        self.uidnext = uidnext

    def login(self, username, password):
        assert self.tls_active, "credentials offered on a cleartext connection"
        self.calls.append(("login", username))
        return ("OK", [b"logged in"])

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return ("OK", [b"3"])

    def response(self, key):
        if key == "UIDVALIDITY":
            return (key, [str(self.uidvalidity).encode("ascii")])
        if key == "UIDNEXT" and self.uidnext is not None:
            return (key, [str(self.uidnext).encode("ascii")])
        return (key, [None])

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            low, _, high = args[-1].partition(":")
            low = max(self.messages or [0]) if low == "*" else int(low)
            high = None if high in ("*", "") else int(high)
            hits = b" ".join(str(u).encode() for u in sorted(self.messages)
                             if u >= low and (high is None or u <= high))
            for uid in self.vanish:                 # expunged after we looked
                self.messages.pop(uid, None)
            return ("OK", [hits])
        if command == "FETCH" and args[1] == "(UID RFC822.SIZE INTERNALDATE)":
            wanted = [int(u) for u in args[0].split(",") if u]
            return ("OK", [b'%d (UID %d RFC822.SIZE %d INTERNALDATE "23-Sep-2026 '
                           b'10:0%d:00 +0000")' % (i + 1, uid, self.size(uid), uid % 10)
                           for i, uid in enumerate(wanted)
                           if uid in self.messages and uid not in self.hide_meta])
        if command == "FETCH" and args[1] == "(BODY.PEEK[])":
            uid = int(args[0])
            self.bodies_fetched.append(uid)
            raw = self.messages[uid]
            return ("OK", [(b"1 (UID %d BODY[] {%d}" % (uid, len(raw)), raw), b")"])
        raise AssertionError("unexpected UID command %r" % (command,))

    def size(self, uid):
        return self.sizes.get(uid, len(self.messages.get(uid, b"")))

    def logout(self):
        self.calls.append(("logout",))
        return ("BYE", [b"bye"])


class FakeSMTP:
    def __init__(self, *, starttls_offered=True, tls=False, send_error=None,
                 quit_error=None, refused=None):
        self.ops = []
        self.tls_active = tls
        self.starttls_offered = starttls_offered
        self.send_error = send_error
        self.quit_error = quit_error
        self.refused = refused or {}
        self.sent = []
        self.tls_at_login = None

    def ehlo(self):
        self.ops.append("ehlo")

    def has_extn(self, name):
        self.ops.append("has_extn:" + name)
        return self.starttls_offered

    def starttls(self, context=None):
        assert context is not None, "STARTTLS without a verifying context"
        self.ops.append("starttls")
        self.tls_active = True

    def login(self, username, password):
        self.ops.append("login")
        self.tls_at_login = self.tls_active

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.ops.append("send")
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((message, from_addr, tuple(to_addrs or ())))
        return dict(self.refused)

    def quit(self):
        self.ops.append("quit")
        if self.quit_error is not None:
            raise self.quit_error


def imap_factory(client):
    def factory(host, port, timeout):
        client.opened = (host, port, timeout)
        return client
    return factory


def smtp_factory(client):
    def factory(host, port, timeout, security):
        client.opened = (host, port, timeout, security)
        return client
    return factory


def result(**kw):
    row = {"id": "r-1", "destination": "owner@example.com", "subject": "done",
           "text": "the task finished", "in_reply_to": "", "thread_key": "", "metadata": None}
    row.update(kw)
    return row


# ----------------------------------------------------------------- configuration

def test_config_normalises_and_refuses_guesswork():
    cfg = mt.validate_config(dict(CONFIG, smtp_security="starttls"))
    assert (cfg["imap_port"], cfg["smtp_port"]) == (993, 587)
    assert cfg["username"] == "dog@example.com" and cfg["timeout"] == mt.DEFAULT_TIMEOUT
    with pytest.raises(mt.MailConfigError):
        mt.validate_config({k: v for k, v in CONFIG.items() if k != "imap_host"})
    with pytest.raises(mt.MailConfigError):
        mt.validate_config(dict(CONFIG, imap_port=70000))
    with pytest.raises(mt.MailConfigError):
        mt.validate_config(dict(CONFIG, folder='INBOX"; DELETE'))
    with pytest.raises(mt.MailConfigError):
        mt.validate_config(dict(CONFIG, password="oops"))   # secrets are not config


def test_missing_credentials_never_reach_a_connection():
    called = []
    with pytest.raises(mt.MailConfigError):
        mt.poll(CONFIG, {}, imap_factory=lambda *a: called.append(a))
    assert called == []


# ------------------------------------------------------------- TLS before auth

def test_starttls_precedes_login_and_send():
    smtp = FakeSMTP(starttls_offered=True)
    out = mt.send(dict(CONFIG, smtp_security="starttls"), CREDS, result(),
                  smtp_factory=smtp_factory(smtp))
    assert smtp.ops == ["ehlo", "has_extn:starttls", "starttls", "ehlo", "login",
                        "send", "quit"]
    assert smtp.tls_at_login is True
    assert out["status"] == "submitted"


def test_server_without_starttls_gets_no_credentials():
    smtp = FakeSMTP(starttls_offered=False)
    with pytest.raises(mt.MailConnectionError) as exc:
        mt.send(dict(CONFIG, smtp_security="starttls"), CREDS, result(),
                smtp_factory=smtp_factory(smtp))
    assert "login" not in smtp.ops and "send" not in smtp.ops
    assert exc.value.delivery_unknown is False
    assert "no credentials were sent" in str(exc.value)


def test_implicit_tls_connection_that_is_not_encrypted_is_refused():
    smtp = FakeSMTP(tls=False)          # SMTP_SSL that somehow is not TLS
    with pytest.raises(mt.MailConnectionError):
        mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(smtp))
    assert "login" not in smtp.ops

    imap = FakeIMAP({1: mime(1)}, tls=False)
    with pytest.raises(mt.MailConnectionError):
        mt.poll(CONFIG, CREDS, imap_factory=imap_factory(imap))
    assert not [c for c in imap.calls if c[0] == "login"]


# -------------------------------------------------------------------- polling

def test_poll_reads_by_uid_without_modifying_the_mailbox():
    imap = FakeIMAP({4: mime(4), 5: mime(5, subject="second")})
    out = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(imap))
    assert [m["event_id"] for m in out["messages"]] == [mt.event_id(1000, 4),
                                                        mt.event_id(1000, 5)]
    assert out["cursor"] == {"uidvalidity": 1000, "uid": 5} and out["more"] is False
    first = out["messages"][0]
    # The parser's fields are handed on flat and unedited, ready for the store.
    assert first["sender"] == "owner@example.com" and first["recipient"] == "dog@example.com"
    assert first["message_id"] == "<m4@example.com>" and first["text"] == "please look at this"
    assert first["status"] == "ok" and first["automatic"] is False
    assert first["received_at"] > 0

    select = [c for c in imap.calls if c[0] == "select"][0]
    assert select[1] == '"INBOX"' and select[2] is True          # read-only
    fetches = [c for c in imap.calls if c[0:2] == ("uid", "FETCH")]
    assert all("BODY.PEEK[]" in c[-1] or "RFC822.SIZE" in c[-1] for c in fetches)
    assert not [c for c in imap.calls if c[1:2] and c[1] in ("STORE", "COPY", "EXPUNGE")]
    assert ("logout",) in imap.calls


def test_cursor_resumes_after_the_last_uid():
    imap = FakeIMAP({4: mime(4), 5: mime(5)})
    out = mt.poll(CONFIG, CREDS, cursor={"uidvalidity": 1000, "uid": 4},
                  imap_factory=imap_factory(imap))
    assert [m["uid"] for m in out["messages"]] == [5]
    search = [c for c in imap.calls if c[0:2] == ("uid", "SEARCH")][0]
    assert search[-1] == "5:*"


def test_limit_reports_more_without_losing_the_remainder():
    imap = FakeIMAP({1: mime(1), 2: mime(2), 3: mime(3)})
    out = mt.poll(CONFIG, CREDS, limit=2, imap_factory=imap_factory(imap))
    assert [m["uid"] for m in out["messages"]] == [1, 2]
    assert out["more"] is True and out["cursor"]["uid"] == 2


def test_changed_uidvalidity_restarts_safely_and_says_so():
    imap = FakeIMAP({1: mime(1), 2: mime(2)}, uidvalidity=2000)
    out = mt.poll(CONFIG, CREDS, cursor={"uidvalidity": 1000, "uid": 7},
                  imap_factory=imap_factory(imap))
    assert out["uidvalidity_changed"] is True
    assert [m["uid"] for m in out["messages"]] == [1, 2]
    search = [c for c in imap.calls if c[0:2] == ("uid", "SEARCH")][0]
    assert search[-1] == "1:*"
    # Ids are scoped to the generation, so old records cannot be overwritten by
    # a different message that happens to reuse UID 1.
    assert out["messages"][0]["event_id"] == mt.event_id(2000, 1)
    assert out["messages"][0]["event_id"] != mt.event_id(1000, 1)
    assert out["cursor"] == {"uidvalidity": 2000, "uid": 2}


def test_same_uidvalidity_keeps_ids_stable_across_polls():
    imap = FakeIMAP({9: mime(9)})
    first = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(imap))
    second = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(FakeIMAP({9: mime(9)})))
    assert first["messages"][0]["event_id"] == second["messages"][0]["event_id"]


def test_oversize_message_is_rejected_without_being_downloaded():
    imap = FakeIMAP({1: mime(1), 2: mime(2), 3: mime(3)},
                    sizes={2: MAX_MAIL_BYTES + 1})
    out = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(imap))
    states = [(m["uid"], m["status"]) for m in out["messages"]]
    assert states == [(1, "ok"), (2, "rejected"), (3, "ok")]
    assert imap.bodies_fetched == [1, 3]                 # the huge body was never fetched
    poison = out["messages"][1]
    assert poison["event_id"] == mt.event_id(1000, 2)
    assert "nothing was truncated" in poison["error"]
    assert poison["sender"] == mt.UNREADABLE_SENDER and poison["text"] == ""
    assert out["cursor"]["uid"] == 3                      # progress past the poison


def test_unparseable_message_is_a_rejection_with_a_stable_id():
    broken = b"From: nobody\r\nSubject: no address\r\n\r\nbody"
    imap = FakeIMAP({1: broken, 2: mime(2)})
    out = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(imap))
    assert out["messages"][0]["status"] == "rejected"
    assert out["messages"][0]["event_id"] == mt.event_id(1000, 1)
    assert out["messages"][0]["error"]
    assert out["messages"][1]["status"] == "ok"          # the next message still arrives


def test_attachment_bytes_survive_the_transport_exactly():
    raw = (b"From: owner@example.com\r\nTo: dog@example.com\r\nSubject: file\r\n"
           b"MIME-Version: 1.0\r\n"
           b'Content-Type: multipart/mixed; boundary="b1"\r\n\r\n'
           b"--b1\r\nContent-Type: text/plain\r\n\r\nsee attached\r\n"
           b"--b1\r\nContent-Type: application/octet-stream\r\n"
           b'Content-Disposition: attachment; filename="d.bin"\r\n'
           b"Content-Transfer-Encoding: base64\r\n\r\nAAECAw==\r\n--b1--\r\n")
    out = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(FakeIMAP({1: raw})))
    attachment = out["messages"][0]["attachments"][0]
    assert attachment["data"] == "AAECAw==" and attachment["bytes"] == 4


def test_a_uid_with_unreadable_metadata_is_recorded_not_stepped_over():
    """The message is still in the mailbox; only our FETCH line was unusable.

    Skipping it would move the cursor past real mail that nobody ever sees
    again, which is the exact failure the module's second rule forbids.
    """
    imap = FakeIMAP({1: mime(1), 2: mime(2), 3: mime(3)}, hide_meta={2})
    out = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(imap))
    states = [(m["uid"], m["status"]) for m in out["messages"]]
    assert states == [(1, "ok"), (2, "rejected"), (3, "ok")]
    poison = out["messages"][1]
    assert poison["event_id"] == mt.event_id(1000, 2)
    assert "still in the mailbox" in poison["error"] and "left unread" in poison["error"]
    assert poison["sender"] == mt.UNREADABLE_SENDER and poison["text"] == ""
    assert imap.bodies_fetched == [1, 3]        # no body was fetched for the bad row
    assert out["expunged"] == [] and out["cursor"]["uid"] == 3
    # It re-checked existence before deciding, and asked only about that one UID.
    assert ("uid", "SEARCH", None, "UID", "2:2") in imap.calls


def test_a_uid_expunged_between_search_and_fetch_is_reported_as_gone():
    imap = FakeIMAP({1: mime(1), 2: mime(2), 3: mime(3)}, vanish={2})
    out = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(imap))
    assert [(m["uid"], m["status"]) for m in out["messages"]] == [(1, "ok"), (3, "ok")]
    # Truly gone: no invented record, but the skip is stated rather than silent.
    assert out["expunged"] == [2] and out["cursor"]["uid"] == 3


def test_anchor_offers_a_from_now_cursor_without_reading_any_mail():
    imap = FakeIMAP({1: mime(1), 2: mime(2), 7: mime(7)}, uidnext=8)
    out = mt.anchor(CONFIG, CREDS, imap_factory=imap_factory(imap))
    assert out["cursor"] == {"uidvalidity": 1000, "uid": 7}
    assert imap.bodies_fetched == []
    assert [c for c in imap.calls if c[0:2] == ("uid", "FETCH")] == []
    assert [c for c in imap.calls if c[0] == "select"][0][2] is True     # read-only
    assert "nothing was read" in out["note"]

    # A server that does not volunteer UIDNEXT is asked for the last message only.
    quiet = FakeIMAP({1: mime(1), 7: mime(7)})
    assert mt.anchor(CONFIG, CREDS, imap_factory=imap_factory(quiet))["uid"] == 7
    assert ("uid", "SEARCH", None, "UID", "*:*") in quiet.calls

    # And that cursor is exactly "nothing from before": the next poll is empty.
    after = mt.poll(CONFIG, CREDS, cursor=out["cursor"],
                    imap_factory=imap_factory(FakeIMAP({1: mime(1), 7: mime(7)})))
    assert after["messages"] == [] and after["first_sync"] is False


def test_first_sync_reads_the_whole_mailbox_forward_and_says_so():
    imap = FakeIMAP({1: mime(1), 2: mime(2), 3: mime(3)})
    first = mt.poll(CONFIG, CREDS, limit=2, imap_factory=imap_factory(imap))
    assert first["first_sync"] is True and first["more"] is True
    assert [m["uid"] for m in first["messages"]] == [1, 2]
    second = mt.poll(CONFIG, CREDS, cursor=first["cursor"], limit=2,
                     imap_factory=imap_factory(FakeIMAP({1: mime(1), 2: mime(2),
                                                         3: mime(3)})))
    assert [m["uid"] for m in second["messages"]] == [3]
    assert second["first_sync"] is False and second["more"] is False


def test_imap_failure_is_retry_safe_and_leaks_no_server_text():
    class Rude(FakeIMAP):
        def uid(self, command, *args):
            if command == "SEARCH":
                raise OSError("login failed for dog@example.com with hunter2-correct-horse")
            return super().uid(command, *args)

    with pytest.raises(mt.MailConnectionError) as exc:
        mt.poll(CONFIG, CREDS, imap_factory=imap_factory(Rude({1: mime(1)})))
    assert exc.value.delivery_unknown is False
    assert "hunter2" not in str(exc.value) and "nothing was marked read" in str(exc.value)


# --------------------------------------------------------------------- probe

def test_probe_is_not_evidence_of_delivery():
    imap, smtp = FakeIMAP({}), FakeSMTP(tls=True)
    out = mt.probe(CONFIG, CREDS, imap_factory=imap_factory(imap),
                   smtp_factory=smtp_factory(smtp))
    assert out["delivery_proof"] is False and "not evidence" in out["note"]
    assert out["imap"]["uidvalidity"] == 1000
    assert "send" not in smtp.ops and smtp.ops[-1] == "quit"


# -------------------------------------------------------------------- sending

def test_send_uses_one_recipient_and_a_derived_stable_message_id():
    smtp = FakeSMTP(tls=True)
    out = mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(smtp))
    message, from_addr, to_addrs = smtp.sent[0]
    assert to_addrs == ("owner@example.com",) and from_addr == "dog@example.com"
    assert message["Cc"] is None and message["Bcc"] is None
    assert out["provider_message_id"] == message["Message-ID"]
    assert out["provider_message_id"] == mt.derive_message_id("dog@example.com", "r-1")
    # The same stored result always composes the same Message-ID, so a corrected
    # retry is recognisable as a repeat rather than a second letter.
    again = FakeSMTP(tls=True)
    assert mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(again)) == out


def test_send_preserves_threading_headers():
    smtp = FakeSMTP(tls=True)
    mt.send(CONFIG, CREDS,
            result(in_reply_to="<a@example.com>",
                   metadata={"references": ["<root@example.com>", "<a@example.com>"]}),
            smtp_factory=smtp_factory(smtp))
    message = smtp.sent[0][0]
    assert message["In-Reply-To"] == "<a@example.com>"
    assert message["References"] == "<root@example.com> <a@example.com>"


def test_malformed_threading_or_destination_is_refused_before_connecting():
    opened = []
    for bad in (result(in_reply_to="not-a-message-id"),
                result(destination="owner@example.com, second@example.com"),
                result(text="")):
        with pytest.raises(mt.MailConfigError):
            mt.send(CONFIG, CREDS, bad, smtp_factory=lambda *a: opened.append(a))
    assert opened == []


def test_server_refusal_is_a_definite_failure():
    refusal = smtplib.SMTPDataError(550, b"5.7.1 rejected for <owner@example.com>")
    smtp = FakeSMTP(tls=True, send_error=refusal)
    with pytest.raises(mt.MailRefused) as exc:
        mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(smtp))
    assert exc.value.delivery_unknown is False
    assert "server code 550" in str(exc.value)
    assert "5.7.1" not in str(exc.value)          # no server text, ever
    assert smtp.ops[-1] == "quit"


def test_broken_connection_during_data_is_unknown_not_failed():
    smtp = FakeSMTP(tls=True, send_error=TimeoutError("timed out"))
    with pytest.raises(mt.MailDeliveryUnknown) as exc:
        mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(smtp))
    assert exc.value.delivery_unknown is True
    assert "must not be resent automatically" in str(exc.value)

    disconnected = FakeSMTP(tls=True, send_error=smtplib.SMTPServerDisconnected("gone"))
    with pytest.raises(mt.MailDeliveryUnknown):
        mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(disconnected))


def test_failure_before_transmission_is_retry_safe():
    smtp = FakeSMTP(tls=True)
    smtp.login = lambda *a: (_ for _ in ()).throw(
        smtplib.SMTPAuthenticationError(535, b"5.7.8 bad password hunter2"))
    with pytest.raises(mt.MailConnectionError) as exc:
        mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(smtp))
    assert exc.value.delivery_unknown is False
    assert "hunter2" not in str(exc.value) and "nothing was sent" in str(exc.value)
    assert "send" not in smtp.ops


def test_quit_failure_after_acceptance_stays_submitted():
    smtp = FakeSMTP(tls=True, quit_error=smtplib.SMTPServerDisconnected("connection reset"))
    out = mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(smtp))
    assert out == {"provider_message_id": mt.derive_message_id("dog@example.com", "r-1"),
                   "status": "submitted", "delivery_unknown": False, "recipients": 1}
    assert len(smtp.sent) == 1


def test_recipient_refused_after_data_is_unknown():
    # Defensive only: with one recipient CPython raises SMTPRecipientsRefused
    # instead of returning a dict, so this asserts our handling of the shape
    # smtplib documents, not behaviour a one-recipient server can produce.
    smtp = FakeSMTP(tls=True, refused={"owner@example.com": (450, b"try later")})
    with pytest.raises(mt.MailDeliveryUnknown):
        mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(smtp))


class ScriptedSocket:
    """One SMTP conversation, replayed under a real ``smtplib.SMTP``.

    A fake client object cannot reproduce what this is about: which side of the
    message body CPython raises ``SMTPDataError`` on.  So the stdlib runs for
    real and only the socket is ours.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.written = b""

    def makefile(self, mode="rb", *args, **kw):
        return self

    def readline(self, limit=-1):
        return self.replies.pop(0) if self.replies else b""

    def sendall(self, data):
        self.written += data

    def send(self, data):
        self.sendall(data)
        return len(data)

    def close(self):
        pass

    def settimeout(self, value):
        pass

    def gettimeout(self):
        return None

    def shutdown(self, how=0):
        pass


class GenuineSMTP(smtplib.SMTP):
    """Real smtplib on a scripted socket.  Only ``login`` is stubbed out."""

    def __init__(self, socket_):
        self.scripted = socket_
        # A fixed local_hostname: no resolver lookup, no socket, anywhere.
        super().__init__("smtp.example.com", 25, local_hostname="collie.test",
                         timeout=5)
        self.tls_active = True      # the TLS gate has its own tests; this is DATA

    def _get_socket(self, host, port, timeout):
        return self.scripted

    def login(self, username, password, **kw):
        return (235, b"authenticated")


def conversation(final_reply):
    return ScriptedSocket(
        b"220 smtp.example.com ESMTP ready\r\n",
        b"250-smtp.example.com\r\n", b"250 HELP\r\n",    # EHLO
        b"250 2.1.0 sender ok\r\n",                      # MAIL FROM
        b"250 2.1.5 recipient ok\r\n",                   # RCPT TO
        b"354 end data with <CR><LF>.<CR><LF>\r\n",      # DATA
        final_reply)                                     # ...after the body


def genuine_factory(socket_, seen):
    def factory(host, port, timeout, security):
        client = GenuineSMTP(socket_)
        seen.append(client)
        return client
    return factory


def test_a_final_data_reply_without_a_code_is_unknown_not_a_refusal():
    """CPython raises SMTPDataError *after* the body has gone out.

    ``getreply`` substitutes ``-1`` when the last line carries no parsable code
    (Lib/smtplib.py), and sendmail turns that into SMTPDataError.  The message
    may be queued at that point, so calling it a clean refusal is how a retry
    sends a person's reply twice.  This is stdlib behaviour, not a fake's: the
    real client wrote the real body to the socket below.
    """
    socket_ = conversation(b"I am a teapot, not a reply code\r\n")
    with pytest.raises(mt.MailDeliveryUnknown) as exc:
        mt.send(CONFIG, CREDS, result(), smtp_factory=genuine_factory(socket_, []))
    assert exc.value.delivery_unknown is True
    assert "must not be resent automatically" in str(exc.value)
    # The evidence that it is ambiguous: the body was fully transmitted first.
    assert b"the task finished" in socket_.written
    assert socket_.written.endswith(b"\r\n.\r\n") or b"\r\n.\r\n" in socket_.written
    assert "teapot" not in str(exc.value)           # no server text, ever


def test_a_numeric_data_refusal_from_the_real_client_is_still_definite():
    socket_ = conversation(b"552 5.3.4 message too big for this mailbox\r\n")
    with pytest.raises(mt.MailRefused) as exc:
        mt.send(CONFIG, CREDS, result(), smtp_factory=genuine_factory(socket_, []))
    assert exc.value.delivery_unknown is False and "server code 552" in str(exc.value)
    assert "too big" not in str(exc.value)


def test_a_real_accepted_send_reports_submitted():
    socket_ = conversation(b"250 2.0.0 Ok: queued as ABC123\r\n")
    out = mt.send(CONFIG, CREDS, result(), smtp_factory=genuine_factory(socket_, []))
    assert out["status"] == "submitted" and out["recipients"] == 1
    # SMTP commands are case-insensitive; Python versions choose different case.
    assert b"rcpt to:<owner@example.com>" in socket_.written.lower()
    assert socket_.written.lower().count(b"rcpt to:") == 1  # one envelope recipient


def test_module_never_enables_client_debug_logging():
    source = open(mt.__file__, encoding="utf-8").read()
    assert "set_debuglevel" not in source and "debuglevel" not in source
    assert "print(" not in source


def test_polled_messages_are_storable_by_the_communication_store(tmp_path, monkeypatch):
    """The ids and addresses we emit must survive the store's own validators."""
    from harness import communications as comms

    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    comms.create_connection("mailbox", channel="email", address="dog@example.com",
                            policy={"owner_reply_target": "owner@example.com"},
                            directory=str(directory))
    imap = FakeIMAP({1: mime(1), 2: mime(2)}, sizes={2: MAX_MAIL_BYTES + 1})
    out = mt.poll(CONFIG, CREDS, imap_factory=imap_factory(imap))
    for message in out["messages"]:
        event = comms.record_received(
            "mailbox", message["event_id"], sender=message["sender"],
            recipient=message["recipient"], subject=message["subject"],
            text=message["text"], thread_key=message["message_id"] or message["event_id"],
            metadata={"input_error": message.get("error", "")},
            received_at=message["received_at"], directory=str(directory))
        assert event["state"] == "pending"
    assert comms.connection_status("mailbox", directory=str(directory))["events"]["pending"] == 2


def test_composed_message_never_carries_the_password():
    smtp = FakeSMTP(tls=True)
    mt.send(CONFIG, CREDS, result(), smtp_factory=smtp_factory(smtp))
    blob = smtp.sent[0][0].as_bytes()
    assert isinstance(smtp.sent[0][0], email.message.Message)
    assert CREDS["password"].encode() not in blob
