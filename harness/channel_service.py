"""Email/phone adapters into Collie's existing durable task and result stores."""
from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
import os
import re
import threading
import time

from . import channel_secrets, communications as comms, mail_messages, sessions
from .controlplane import state_dir as active_state_dir

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_KINDS = {"imap", "collie_mail", "twilio"}
_ANCHORING = {"imap", "twilio", "collie_mail"}
_HISTORY = {"new", "all"}
_MAX_CONFIG = 128 * 1024
_MAX_ASSETS = 256 * 1024 * 1024
# A send claim older than this is treated as abandoned.  It is deliberately long:
# the only thing this buys is moving a claim to ``unknown`` (never a resend), and
# an SMTP session that is merely slow must not be declared lost while it runs.
SEND_CLAIM_TIMEOUT = 120.0
# Per-tick budgets.  One connection, or one bad event, cannot consume the pass.
MAX_RECOVER = 10
MAX_RECONCILE = 25
MAX_DRAFTS = 10
MAX_AUTO_SEND = 3
# Of one reconcile budget, the slots reserved for the *oldest* work still
# waiting.  The rest go to the newest, because that is the reply a person is
# most likely waiting on — but an old one must still get its turn every pass.
RECONCILE_OLDEST = 5
# How much of the pending queue the drafting lane looks at.  It covers the whole
# cap on purpose: pending events are already bounded, so a window this size
# cannot leave an eligible message hidden behind ineligible ones in front of it.
# (The hourly throttle does *not* use this window — see ``_draft_lane``.)
DRAFT_WINDOW = comms.MAX_PENDING_EVENTS
# Attempts at one reply.  A person who has hit this should edit the reply or
# fix the connection rather than queue an eleventh identical message.
MAX_SEND_ATTEMPTS = 10


class ChannelError(ValueError):
    pass


class PoisonMessage(ChannelError):
    """This one message cannot be stored as it arrived; the cursor may pass it.

    Only ever raised about the content of a single delivery.  A store that is
    full, unreadable or holding a conflicting record is *not* this: those mean
    the next poll should see the same messages again, so they propagate and
    leave the cursor where it is.
    """


def _key(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ChannelError("invalid connection id")
    return value


def _detail(exc, fallback):
    """Status text for a person.  Provider bodies and credentials never reach it.

    Only errors this package composed itself are quoted; anything raised by a
    transport, a provider library or the standard library is replaced by
    ``fallback``, because those messages routinely carry URLs, response bodies
    and occasionally the credential that failed.
    """
    if isinstance(exc, (ChannelError, comms.CommsError)):
        return str(exc)[:500]
    return fallback


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ChannelService:
    def __init__(self, state_dir=None):
        self.root = active_state_dir(state_dir)
        self.directory = sessions.store_root(self.root)
        self.path = os.path.join(self.root, "channels.json")
        os.makedirs(self.root, exist_ok=True)

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as stream:
                raw = stream.read(_MAX_CONFIG + 1)
        except FileNotFoundError:
            return {"version": 1, "connections": {}}
        try:
            value = json.loads(raw)
        except ValueError:
            raise ChannelError("channel settings need repair; existing settings were kept") from None
        if (len(raw) > _MAX_CONFIG or not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("connections"), dict)):
            raise ChannelError("channel settings need repair; existing settings were kept")
        return value

    def _change(self, update):
        with sessions._locked(self.path):
            data = self._load()
            result = update(data["connections"])
            if len(json.dumps(data).encode()) > _MAX_CONFIG:
                raise ChannelError("channel settings exceeded the storage limit")
            sessions._atomic_dump(data, self.path)
            return result

    def _row(self, connection):
        row = self._load()["connections"].get(_key(connection))
        if not isinstance(row, dict):
            raise ChannelError("connection was not found")
        return row

    @staticmethod
    def _adapter(kind):
        if kind == "imap":
            from . import mail_transport
            return mail_transport
        if kind == "twilio":
            from . import phone_transport
            return phone_transport
        raise ChannelError("this connection uses the Collie Mail relay")

    def _op_lock(self, connection):
        """Serializes configure/send/disconnect for one connection.

        Deliberately *not* the settings-file lock: a send holds this across a
        provider round trip, and holding ``channels.json`` for that long would
        block every unrelated read.  What it buys is the ordering that matters —
        an owner or credential change cannot land in the middle of an outgoing
        claim that was authorized against the previous owner.
        """
        return sessions._locked(os.path.join(self.root, "channel-op-" + _key(connection)))

    def configure(self, connection, *, kind, config, owner, credentials=None, workspace="",
                  mode="manual", enabled=True, auto_reply=False, history=None):
        """Create or re-save a connection.  Validates fully before it writes anything.

        ``history`` is the initial-sync policy and may be given only as ``"new"``
        (default for a connection that did not exist: the first poll anchors and
        only mail arriving afterwards becomes work) or ``"all"`` (read the
        mailbox from its beginning, a page per poll).  Re-saving an existing
        connection keeps whatever it already had, so a connection that carries a
        cursor keeps reading exactly where it stopped.
        """
        connection = _key(connection)
        if kind not in _KINDS or mode not in {"manual", "draft"}:
            raise ChannelError("unsupported channel or workflow")
        if history is not None and history not in _HISTORY:
            raise ChannelError("history must be 'new' or 'all'")
        if type(enabled) is not bool or type(auto_reply) is not bool:
            raise ChannelError("connection switches must be true or false")
        if not isinstance(config, dict):
            raise ChannelError("connection settings must be an object")
        if not isinstance(workspace, (str, os.PathLike)):
            raise ChannelError("project folder must be a path")
        # --- validation only.  Nothing below this block may touch the stores.
        try:
            if kind == "collie_mail":
                from . import dogmail
                if set(config) - {"mailbox"}:
                    raise ChannelError("unsupported Collie Mail setting")
                mailbox = str(config.get("mailbox") or "")
                dog = dogmail._dog(mailbox, state_dir=self.root)
                address = dog.get("address") or ""
                if not address:
                    raise ChannelError("create or connect a Collie Mail address first")
                clean = {"mailbox": mailbox}
            else:
                clean = self._adapter(kind).validate_config(config)
                address = clean.get("phone_number") if kind == "twilio" else (clean.get("address") or clean.get("sender"))
            channel = "sms" if kind == "twilio" else "email"
            owner = comms._check_address(owner, "owner", channel)
            if kind == "twilio":
                from .phone_transport import destination
                owner = destination(owner)
        except ChannelError:
            raise
        except (ValueError, comms.CommsError) as exc:
            # Transport and address validators speak about the field that is
            # wrong; the caller only ever needs to see a ChannelError.
            raise ChannelError(str(exc)[:500]) from None
        if credentials is not None:
            if (not isinstance(credentials, dict) or not credentials
                    or set(credentials) - ({"auth_token"} if kind == "twilio" else {"password"})
                    or any(not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value
                           for value in credentials.values())):
                raise ChannelError("invalid connection credentials")
        if workspace:
            workspace = os.path.abspath(os.path.expanduser(workspace))
            if not os.path.isdir(workspace):
                raise ChannelError("choose an existing project folder")
        else:
            workspace = os.path.join(self.root, "communication-work")
        policy = {"allowed_senders": [owner], "owner_reply_target": owner,
                  "allowed_destinations": [], "require_allowed_sender": True}
        source_keys = {"imap": ("address", "imap_host", "imap_port", "username", "folder"),
                       "twilio": ("account_sid", "phone_number"), "collie_mail": ("mailbox",)}[kind]
        with self._op_lock(connection):
            before = self._load()["connections"]
            previous = before.get(connection) or {}
            if not previous and len(before) >= 16:
                raise ChannelError("at most 16 communication connections are supported")
            if previous and (previous.get("kind") != kind or any(
                    previous.get("config", {}).get(key) != clean.get(key) for key in source_keys)):
                raise ChannelError("add a new connection for a different mailbox, server or phone number")
            if history is None:
                # An existing row keeps its policy; a legacy row (written before
                # this field existed, possibly with a cursor) keeps reading the
                # way it always has.  Only a genuinely new connection defaults to
                # "from now on".
                history = previous.get("history") or ("all" if previous else "new")
            os.makedirs(workspace, exist_ok=True)
            # From here on the write is multi-store and cannot be one transaction.
            # An existing connection is marked first so that an interruption fails
            # closed for sending instead of leaving a half-changed owner usable.
            if previous:
                self._change(lambda rows: rows[connection].update(config_pending=True, updated=time.time()))
            comms.create_connection(connection, channel=channel, address=address,
                                    display_name="Phone" if channel == "sms" else "Email",
                                    policy=policy, secret_ref="file:" + connection,
                                    directory=self.directory)
            comms.update_policy(connection, policy, actor="desktop-settings", directory=self.directory)
            if credentials is not None:
                channel_secrets.put(connection, credentials, state_dir=self.root)
            def update(rows):
                if connection not in rows and len(rows) >= 16:
                    raise ChannelError("at most 16 communication connections are supported")
                previous = rows.get(connection) or {}
                if previous and (previous.get("kind") != kind or previous.get("config") != clean):
                    # A new mailbox/server must not inherit another server's cursor.
                    previous = {k: v for k, v in previous.items()
                                if k not in {"cursor", "status", "last_checked", "anchored"}}
                rows[connection] = dict(previous, id=connection, kind=kind, config=clean, owner=owner,
                                       workspace=workspace, mode=mode, enabled=enabled, history=history,
                                       auto_reply=auto_reply, config_pending=False, updated=time.time(),
                                       revision=int(previous.get("revision", 0)) + 1)
            self._change(update)
        return self.connection(connection)

    def connection(self, connection):
        row = self._row(connection)
        public = {key: row.get(key) for key in ("id", "kind", "config", "owner", "workspace", "mode",
                                                "enabled", "auto_reply", "updated", "last_checked", "status", "error")}
        public["history"] = row.get("history") or "all"
        public["anchored"] = bool(row.get("anchored"))
        public["synced"] = bool(row.get("cursor"))
        public["warning"] = row.get("warning") or ""
        public["settings_incomplete"] = bool(row.get("config_pending"))
        public["has_credentials"] = bool(channel_secrets.present(connection, state_dir=self.root))
        public["counts"] = comms.connection_status(connection, directory=self.directory)
        return public

    def overview(self):
        rows = []
        for connection in self._load()["connections"]:
            try:
                rows.append(self.connection(connection))
            except (ValueError, comms.CommsError, OSError):
                rows.append({"id": connection, "status": "error", "error": "connection records need repair"})
        return {"connections": rows}

    def set_enabled(self, connection, enabled):
        """Pause or resume a connection, and invalidate work already in flight.

        The revision is bumped for the same reason ``configure`` bumps it: a
        lane that started before this call holds a stale view of the connection
        and its ``_status`` write must lose.  The op lock is deliberately *not*
        taken — pausing must stay instant even while a send is mid-flight, and
        the revision plus the enabled check in ``_status`` already order the
        outcome correctly.
        """
        if type(enabled) is not bool:
            raise ChannelError("enabled must be true or false")
        self._row(connection)
        def update(rows):
            row = rows[connection]
            row.update(enabled=enabled, updated=time.time(),
                       revision=int(row.get("revision", 0)) + 1)
        self._change(update)
        return self.connection(connection)

    def disconnect(self, connection):
        """Pause delivery and remove credentials; keep accepted work and results.

        The cursor is kept on purpose: reconnecting the same account resumes
        where it stopped instead of re-reading (or skipping) what arrived while
        it was paused.  Serialized with ``send`` so credentials cannot disappear
        underneath an outgoing claim.
        """
        with self._op_lock(connection):
            self.set_enabled(connection, False)
            channel_secrets.delete(connection, state_dir=self.root)
            self._change(lambda rows: rows[connection].update(status="disconnected"))
        return self.connection(connection)

    def _status(self, connection, status, error="", *, revision=None, **fields):
        """Publish what a lane observed, unless the connection has moved on.

        Two guards, and both answer the same question — is this observation
        still about the connection as it is now?

        * ``revision`` is the optimistic one: ``configure`` and ``set_enabled``
          bump it, so a lane that read the row before either lands writes
          nothing.
        * A disabled connection is never given a status at all.  A poll that
          started before ``disconnect`` finishes afterwards, and re-publishing
          ``connected`` over a connection whose credentials were just removed
          tells a person the opposite of what they asked for.
        """
        def update(rows):
            row = rows[connection]
            if revision is not None and row.get("revision") != revision:
                return False
            if not row.get("enabled"):
                return False
            row.update(status=status, error=error, last_checked=time.time(), **fields)
            return True
        return self._change(update)

    def _advance_cursor(self, connection, cursor, *, kind, config):
        """Commit only the read position of a poll whose status write was stale.

        The messages behind this cursor are already durably recorded, so moving
        it is safe and re-reading them would only produce duplicates.  It is
        still refused if the row now names a different account, because a
        cursor belongs to the mailbox it was read from.
        """
        if not cursor:
            return False
        def update(rows):
            row = rows[connection]
            if row.get("kind") != kind or row.get("config") != config:
                return False
            row.update(cursor=cursor, last_checked=time.time())
            return True
        return self._change(update)

    def probe(self, connection):
        row = self._row(connection)
        if row["kind"] == "collie_mail":
            from . import dogmail
            result = dogmail.probe(row["config"]["mailbox"], state_dir=self.root)
        else:
            result = self._adapter(row["kind"]).probe(row["config"], channel_secrets.get(connection, state_dir=self.root))
        self._status(connection, "connected")
        return result

    def events(self, connection, *, limit=100, newest=True):
        """This connection's received messages, newest window first, oldest-first within it.

        ``newest`` defaults to True because every caller here is looking at
        what is happening *now*: the desktop inbox, and thread matching for a
        reply that quotes a recent message.  A connection retains up to 500
        settled events, so a window taken from the front would freeze on the
        first hundred messages a mailbox ever received and never show the one
        that just arrived.  Pass ``newest=False`` to read from the beginning.
        """
        self._row(connection)
        return comms.list_events(connection, limit=limit, include_private=True,
                                 newest=newest, directory=self.directory)

    def results(self, connection, *, limit=100, newest=True):
        """This connection's outgoing messages, newest window; see ``events``."""
        self._row(connection)
        return comms.list_results(connection, limit=limit, include_private=True,
                                  newest=newest, directory=self.directory)

    def _store_attachments(self, connection, attachments):
        folder = os.path.join(self.root, "channel-assets", _key(connection))
        os.makedirs(folder, exist_ok=True)
        refs = []
        with sessions._locked(os.path.join(folder, "quota")):
            for index, attachment in enumerate(attachments):
                try:
                    encoded = attachment["data"]
                    raw = base64.b64decode(encoded, validate=True)
                except (KeyError, TypeError, ValueError):
                    # The delivery itself is unusable, not the store.
                    raise PoisonMessage("attachment payload could not be decoded") from None
                digest = hashlib.sha256(raw).hexdigest()
                if digest != attachment["sha256"] or len(raw) != attachment["bytes"]:
                    raise PoisonMessage("attachment integrity check failed")
                path = os.path.join(folder, digest + ".json")
                if not os.path.exists(path):
                    used = sum(item.stat().st_size for item in os.scandir(folder)
                               if item.is_file() and item.name.endswith(".json"))
                    if used + len(encoded) + 1024 > _MAX_ASSETS:
                        raise ChannelError("attachment storage is full; received work was kept")
                    sessions._atomic_dump({"data": encoded, "sha256": digest, "bytes": len(raw)}, path)
                refs.append({"id": "%d-%s" % (index, digest), "name": attachment["name"],
                             "media_type": attachment["content_type"], "bytes": len(raw), "digest": digest})
        return refs

    def attachment(self, connection, digest):
        self._row(connection)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ChannelError("invalid attachment id")
        path = os.path.join(self.root, "channel-assets", connection, digest + ".json")
        with open(path, encoding="utf-8") as stream:
            stored = stream.read(mail_messages.MAX_MAIL_BYTES * 2 + 1024)
        data = json.loads(stored)
        raw = base64.b64decode(data["data"], validate=True)
        if hashlib.sha256(raw).hexdigest() != digest or len(raw) != data["bytes"]:
            raise ChannelError("saved attachment failed its integrity check")
        return raw

    def _thread(self, connection, message):
        if self._row(connection)["kind"] == "twilio":
            return "sms-" + _hash(message["sender"])
        refs = set(message.get("in_reply_to") or []) | set(message.get("references") or [])
        if refs:
            for event in self.events(connection, limit=200):
                if (event.get("sender") == message["sender"] and
                        (event.get("metadata") or {}).get("message_id") in refs):
                    return event["thread_key"]
            for result in self.results(connection, limit=500):
                provider_id = (result.get("outcome_detail") or {}).get("provider_message_id", "")
                identifiers = {(result.get("metadata") or {}).get("message_id"), provider_id}
                if provider_id:
                    identifiers.add("<" + provider_id.strip("<>") + ">")
                if (result.get("destination") == message["sender"] and result.get("thread_key")
                        and identifiers.intersection(refs)):
                    return result["thread_key"]
        return "mail-" + _hash(message.get("message_id") or message["event_id"])

    def ingest(self, connection, message):
        """Record a normalized provider event before its transport cursor moves.

        A message the parser accepted can still be one the durable store
        refuses — a NUL in the body, a decoded subject carrying a line break.
        Those are recorded as an explicit refusal under the same event id
        instead of escaping to the caller, because an exception here is what
        stops the cursor, and a stopped cursor hides every message that arrived
        afterwards.  The content is never repaired into a task: a message
        quietly altered on the way in is not the message the person received.
        """
        row = self._row(connection)
        if not isinstance(message, dict) or not message.get("event_id"):
            raise ChannelError("received message has no delivery identity")
        existing = comms.get_event(connection, message["event_id"], include_private=True, directory=self.directory)
        if existing and existing.get("compacted"):
            return dict(existing, duplicate=True)
        # A missing sender is a message the store will refuse, not a crash.
        message = dict(message, sender=message.get("sender") or "")
        try:
            thread = existing.get("thread_key", "") if existing else self._thread(connection, message)
            refs = self._store_attachments(connection, message.get("attachments") or [])
            event = comms.record_received(connection, message["event_id"], sender=message["sender"],
                                          recipient=message.get("recipient") or "", text=message.get("text") or "(No text)",
                                          subject=message.get("subject", ""), thread_key=thread, attachments=refs,
                                          metadata={"message_id": message.get("message_id", ""),
                                                    "references": message.get("references") or [],
                                                    "automatic": bool(message.get("automatic")),
                                                    "input_error": message.get("error", "")},
                                          received_at=message.get("received_at"), directory=self.directory)
        except (comms.InvalidRequest, PoisonMessage) as exc:
            # Refused for what this delivery *is*.  StoreFull, IdConflict and
            # StoreCorrupt are not caught here: those say the next poll should
            # see these messages again, so they must reach the caller with the
            # cursor untouched.
            return self._record_unstorable(connection, row, message, exc)
        if not event.get("duplicate") and (message.get("automatic") or message.get("error")):
            event = comms.reject_event(connection, message["event_id"], actor="channel-intake",
                               reason="Automatic message" if message.get("automatic") else "Input could not be read completely",
                               directory=self.directory)
        return event

    def _record_unstorable(self, connection, row, message, reason):
        """Record "something arrived here that could not be kept" and settle it.

        The placeholder carries no content from the message, only the fact of
        it and a readable reason, so the person can find it in the account's own
        inbox.  It is rejected immediately: it can never become a task.
        """
        channel = "sms" if row["kind"] == "twilio" else "email"
        fallback = (row["config"].get("phone_number") or "+10000000000") if channel == "sms" \
            else "unreadable@invalid.test"
        sender = message.get("sender") or fallback
        try:
            comms._check_address(sender, "sender", channel, strict=True)
        except comms.CommsError:
            sender = fallback
        detail = _detail(reason, "this message could not be stored as it arrived")[:300]
        try:
            event = comms.record_received(
                connection, message["event_id"], sender=sender,
                text="(This message could not be stored as it arrived; it was not opened.)",
                subject="Unreadable message", thread_key="",
                metadata={"message_id": "", "references": [], "automatic": False,
                          "input_error": detail},
                received_at=message.get("received_at"), directory=self.directory)
        except comms.InvalidRequest as exc:
            # Not even a refusal fits under this id — the id itself is the
            # problem.  There is nothing durable to show, so the poll counts it
            # and steps over it rather than stalling on it forever.
            raise PoisonMessage(_detail(exc, "this message could not be recorded at all")) from None
        if not event.get("duplicate"):
            event = comms.reject_event(connection, message["event_id"], actor="channel-intake",
                                       reason="Message could not be stored: " + detail,
                                       directory=self.directory)
        return dict(event, unstorable=True)

    def _anchor(self, connection, row):
        """Persist a "from now on" baseline before any message is read.

        Called only for a connection whose initial-sync policy is ``new`` and
        that has no cursor yet.  Nothing is fetched or marked read, and the
        cursor is written in the same guarded update the poll path uses, so a
        failure here leaves the connection exactly as it was and the next poll
        tries again.
        """
        if row["kind"] == "collie_mail":
            from . import dogmail
            baseline = dogmail.anchor(row["config"]["mailbox"], state_dir=self.root)
            if not self._status(connection, "connected", revision=row.get("revision"),
                                cursor=baseline["cursor"], anchored=time.time(), warning=""):
                raise ChannelError("connection settings changed while the starting point was saved")
            return {"received": 0, "more": False, "status": "connected", "anchored": True,
                    "history": "new", "detail": baseline["note"]}
        adapter = self._adapter(row["kind"])
        if not hasattr(adapter, "anchor"):
            # An adapter without a baseline cannot skip history honestly, so it
            # reads everything rather than silently dropping mail.
            self._change(lambda rows: rows[connection].update(history="all"))
            return None
        try:
            baseline = adapter.anchor(row["config"], channel_secrets.get(connection, state_dir=self.root))
            cursor = baseline.get("cursor") if isinstance(baseline, dict) else None
            if not isinstance(cursor, dict) or not cursor:
                raise ChannelError("the account did not report a starting point")
        except Exception as exc:
            raise ChannelError(_detail(exc, "the account could not be opened to set a starting "
                                            "point; nothing was read and this will be tried again")) from None
        if not self._status(connection, "connected", revision=row.get("revision"),
                            cursor=cursor, anchored=time.time(), warning=""):
            raise ChannelError("connection settings changed while the starting point was saved; "
                               "nothing was read and this will be tried again")
        return {"received": 0, "more": False, "status": "connected", "anchored": True,
                "history": "new", "skipped": 0, "unreadable": 0, "warning": "",
                "detail": "Starting point saved. Only messages that arrive from now on "
                          "become tasks; mail already in this account is left alone."}

    def poll(self, connection):
        row = self._row(connection)
        if not row.get("enabled"):
            return {"received": 0, "status": "paused"}
        lock = os.path.join(self.root, "channel-poll-" + _key(connection))
        with sessions._locked(lock):
            row = self._row(connection)
            if not row.get("enabled"):
                return {"received": 0, "status": "paused"}
            if row.get("config_pending"):
                raise ChannelError("connection settings were interrupted while saving; "
                                   "save the connection again before checking it")
            if (row["kind"] in _ANCHORING and not row.get("cursor")
                    and (row.get("history") or "all") == "new"):
                anchored = self._anchor(connection, row)
                if anchored is not None:
                    return anchored
                row = self._row(connection)
            if row["kind"] == "collie_mail":
                from . import dogmail
                batch = dogmail.fetch_page(row["config"]["mailbox"], cursor=row.get("cursor"), state_dir=self.root)
                messages = []
                for item in batch["messages"]:
                    try:
                        parsed = mail_messages.from_relay(item)
                    except mail_messages.MailFormatError as exc:
                        parsed = {"sender": "unreadable@invalid.test", "recipient": "", "text": "",
                                  "subject": "Unreadable email", "error": str(exc)}
                    parsed.update(event_id=item["id"], received_at=item.get("at"))
                    messages.append(parsed)
                result = {"messages": messages, "more": batch["more"], "cursor": batch["cursor"]}
            else:
                result = self._adapter(row["kind"]).poll(row["config"], channel_secrets.get(connection, state_dir=self.root),
                                                        cursor=row.get("cursor"), limit=25)
            count = unreadable = unrecordable = 0
            for item in result["messages"]:
                message = self._normalize(row, item)
                try:
                    recorded = self.ingest(connection, message)
                except PoisonMessage:
                    # No event could be written for this delivery at all, so
                    # there is nothing to show and nothing to re-read.  It is
                    # counted with the messages the provider could not hand over.
                    unrecordable += 1
                    continue
                if recorded.get("duplicate"):
                    continue
                count += 1
                if message.get("error") or recorded.get("unstorable"):
                    unreadable += 1
            # Messages the provider could not hand over at all: gone between the
            # listing and the fetch (IMAP), or unusable rows the transport had to
            # step over.  They have no event, so the count is the only trace.
            skipped = (len(result.get("expunged") or []) + int(result.get("skipped") or 0)
                       + unrecordable)
            warning = ""
            if unreadable or skipped:
                warning = ("%d message(s) arrived unreadable and %d could not be retrieved at all; "
                           "check them in the account's own inbox" % (unreadable, skipped))
            # A failed intake leaves the cursor untouched; retry sees the same
            # events and the durable store decides which already exist.
            if not self._status(connection, "connected", revision=row.get("revision"),
                                cursor=result.get("cursor"), warning=warning):
                # Settings changed while this poll ran — a pause, a disconnect, a
                # re-save.  Everything read is durably recorded, so the read
                # position still moves (re-reading would only produce
                # duplicates), but the status this poll observed is stale and
                # must not overwrite the one the person just asked for.
                self._advance_cursor(connection, result.get("cursor"),
                                     kind=row["kind"], config=row["config"])
                return {"received": count, "more": bool(result.get("more")),
                        "status": self._row(connection).get("status") or "", "settings_changed": True,
                        "skipped": skipped, "unreadable": unreadable, "warning": warning,
                        "history": row.get("history") or "all"}
            return {"received": count, "more": bool(result.get("more")), "status": "connected",
                    "skipped": skipped, "unreadable": unreadable, "warning": warning,
                    "history": row.get("history") or "all"}

    def _normalize(self, row, item):
        """Adapters report poison messages explicitly so a cursor can pass them."""
        message = dict(item.get("mail") or item)
        message["event_id"] = item.get("event_id") or item.get("id")
        if item.get("status") == "rejected":
            message.update(text="(Message could not be read)",
                           sender=message.get("sender") or (row["config"].get("phone_number") if row["kind"] == "twilio" else "unreadable@invalid.test"),
                           error=item.get("error") or item.get("reason") or "Message could not be read completely")
        elif item.get("media"):
            message["error"] = "This SMS contains media that has not been downloaded; review it in the provider inbox"
        if len(str(message.get("text") or "").encode("utf-8")) > comms.MAX_TEXT_BYTES:
            message.update(text="(Message body exceeds the task intake limit)",
                           error="Message text exceeds 64 KiB; it was not used as a task")
        return message

    def _snapshot_attachments(self, connection, event, session):
        from . import input_assets
        images, contexts = [], []
        for ref in event.get("attachment_refs") or []:
            raw = self.attachment(connection, ref["digest"])
            kind = ref["media_type"]
            if kind in input_assets.IMAGE_TYPES:
                images.append({"media_type": kind, "data": base64.b64encode(raw).decode("ascii")})
            elif kind.startswith("text/") or kind in {"application/json", "application/xml"}:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise ChannelError("attachment text is not UTF-8; download and review it first") from None
                contexts.append({"kind": "email_attachment", "label": ref["name"], "content": text})
            else:
                raise ChannelError("this attachment type needs review; download it before accepting the message")
        if len(contexts) < input_assets.MAX_CONTEXTS:
            from . import daily_brief_reply
            room = input_assets.MAX_CONTEXT_CHARS - sum(len(item["content"]) for item in contexts)
            quoted = daily_brief_reply.reply_context(self, event, connection, max_chars=max(0, room))
            if quoted:
                contexts.append(quoted)
        return input_assets.save(session, images=images, contexts=contexts, directory=self.directory)

    def accept(self, connection, event_id, *, draft=True, start=True, approved=False):
        """A trusted local UI or the configured no-tools drafting template calls this."""
        from . import capability_policy, settings, web_tasks, webapp
        row = self._row(connection)
        event = comms.get_event(connection, event_id, include_private=True, directory=self.directory)
        if not event or event.get("compacted"):
            raise ChannelError("received message is no longer available")
        if (event.get("metadata") or {}).get("input_error"):
            raise ChannelError("this message is incomplete; correct the input before starting")
        frozen = event.get("acceptance_detail")
        if frozen:
            config = frozen["config"]
            saved_policy = config.get("frozen", {}).get("communication_policy", {})
            if saved_policy.get("scope") != ("draft" if draft else "task"):
                raise ChannelError("this message was accepted with a different work scope")
        else:
            limits = settings.current_limits()
            if draft:
                limits = replace(limits, max_total_tokens=min(limits.max_total_tokens or 20_000, 20_000),
                                 max_turns=min(limits.max_turns or 6, 6), max_tokens=min(limits.max_tokens or 4000, 4000))
            config = web_tasks.freeze_config(
                {"intent": "build", "quality": "balanced", "verification": "auto",
                 "workspace": "current", "strategy": "single", "runner": "collie",
                 "speed": "standard", "explicit_axes": "intent,quality,verification,workspace,strategy,speed",
                 "cwd": row["workspace"]},
                provider=webapp._provider(), model=settings.get("MODEL", ""),
                limits=limits.payload(), capabilities=capability_policy.freeze())
            config["frozen"]["communication_policy"] = {"version": 1, "connection": connection,
                                                          "event": event_id, "scope": "draft" if draft else "task"}
            target = comms.thread_session(connection, event["thread_key"], directory=self.directory)["session"]
            assets = self._snapshot_attachments(connection, event, target)
            if assets:
                config["frozen"]["communication_assets"] = assets
        accepted = comms.accept_event(connection, event_id, actor="local-draft-template" if draft else "desktop-user",
                                      config=config, override_sender=approved,
                                      reason="Accepted in the desktop inbox" if approved else "",
                                      directory=self.directory)
        if start:
            try:
                accepted["execution"] = self._start_pending(accepted["session"], label="communication")
            except web_tasks.WebInputError as exc:
                # Acceptance is durable even when execution is busy or fenced.
                accepted["execution"] = {"started": False, "reason": str(exc)}
        return accepted

    def reject(self, connection, event_id):
        self._row(connection)
        return comms.reject_event(connection, event_id, actor="desktop-user", reason="Dismissed by user",
                                  directory=self.directory)

    def prepare_reply(self, connection, result_id, *, text, event_id="", speak=False, automatic=False):
        row = self._row(connection)
        event = (comms.get_event(connection, event_id, include_private=True, directory=self.directory)
                 if event_id else {}) or {}
        if event_id and not event:
            raise ChannelError("received message was not found")
        if speak and row["kind"] != "twilio":
            raise ChannelError("voice calls require a phone connection")
        message_id = "<collie-%s@%s>" % (_hash(connection + ":" + result_id)[:40],
                                        "collie.local" if row["kind"] == "twilio" else
                                        (row["config"].get("address") or row["config"].get("sender") or "mail@collie.run").split("@")[-1])
        subject = event.get("subject") or "Collie result"
        subject = subject if subject.lower().startswith("re:") else "Re: " + subject
        # ``for_event`` makes storing the reply and recording that the received
        # message now has one a single durable write, so no crash can leave an
        # answered message still counted as owed an answer — or, worse, leave it
        # looking answered when the answer was never stored.
        return comms.create_result(connection, result_id, destination=row["owner"], text=text, subject=subject,
                                    thread_key=event.get("thread_key", ""),
                                    in_reply_to=(event.get("metadata") or {}).get("message_id", ""),
                                    session=(event.get("acceptance") or {}).get("session", ""),
                                    metadata={"message_id": message_id, "speak": bool(speak), "auto_eligible": bool(automatic),
                                              "references": (event.get("metadata") or {}).get("references") or []},
                                    for_event=event_id or "", directory=self.directory)

    def send(self, connection, result_id):
        """Transmit one prepared reply.  Serialized against configure/disconnect.

        The op lock is held across the provider call so that the owner address,
        credentials and transport the claim was authorized against cannot change
        while the message is in flight.  The settings file is not held: only this
        connection's other settings operations wait.
        """
        with self._op_lock(connection):
            return self._send_locked(connection, result_id)

    def _send_locked(self, connection, result_id):
        row = self._row(connection)
        if not row.get("enabled"):
            raise ChannelError("connection is paused")
        if row.get("config_pending"):
            # A settings write did not finish, so the owner and credentials on
            # disk may disagree.  Refuse rather than send to a half-changed owner.
            raise ChannelError("connection settings were interrupted while saving; "
                               "save the connection again before sending")
        adapter, credentials = None, None
        if row["kind"] != "collie_mail":
            adapter = self._adapter(row["kind"])
            credentials = channel_secrets.get(connection, state_dir=self.root)
            if not credentials:
                raise ChannelError("connect this account before sending")
        result = comms.get_result(connection, result_id, include_private=True, directory=self.directory)
        if result and result.get("destination") != row["owner"]:
            raise ChannelError("this reply targets the previous owner address; prepare a new reply for the current owner")
        claim = comms.claim_send(connection, result_id, transport=row["kind"], directory=self.directory)
        payload = dict(claim, message_id=(claim.get("metadata") or {}).get("message_id", ""),
                       references=(claim.get("metadata") or {}).get("references") or [])
        try:
            if row["kind"] == "collie_mail":
                from . import dogmail
                receipt = dogmail.send(row["config"]["mailbox"], payload, state_dir=self.root)
            else:
                sender = adapter.speak if (claim.get("metadata") or {}).get("speak") else adapter.send
                receipt = sender(row["config"], credentials, payload)
            if not isinstance(receipt, dict) or receipt.get("status") != "submitted" or not receipt.get("provider_message_id"):
                raise RuntimeError("provider did not acknowledge submission")
        except Exception as exc:
            unknown = bool(getattr(exc, "delivery_unknown", True))
            settle = comms.mark_unknown if unknown else comms.mark_failed
            return settle(connection, result_id, token=claim["token"],
                          error="Delivery status is unknown; check before retrying" if unknown else "Provider refused the request; check connection settings",
                          directory=self.directory)
        return comms.mark_submitted(connection, result_id, token=claim["token"],
                                     provider_message_id=str(receipt.get("provider_message_id") or ""),
                                     directory=self.directory)

    def retry(self, connection, result_id):
        """Queue a fresh attempt at a failed reply, under a new result id.

        A retry that reuses the result id is answered by the relay's own
        delivery ledger, which is keyed on it: once that ledger holds ``failed``
        for an id, every later submission of the same id returns that failure,
        so the reply can never leave this machine no matter what was fixed.

        The new attempt keeps the approved text, subject, destination, thread
        and the same ``Message-ID``, so the person sends the reply they approved
        and the recipient sees one message.  The failed record keeps its
        outcome and history, and names this attempt, so a second click of Retry
        answers with the attempt that already exists instead of queueing
        another one.  Serialized with ``send`` and ``disconnect``, so a new
        attempt cannot be prepared underneath a claim on the old one.
        """
        with self._op_lock(connection):
            self._row(connection)
            result = comms.get_result(connection, result_id, include_private=True,
                                      directory=self.directory)
            if not result or result.get("compacted"):
                raise ChannelError("this reply is no longer available to retry")
            attempt = int((result.get("metadata") or {}).get("attempt") or 1) + 1
            if attempt > MAX_SEND_ATTEMPTS:
                raise ChannelError("this reply has been retried too many times; edit it and "
                                   "send the revision, or check the connection first")
            base = re.sub(r"-try[0-9]+\Z", "", result_id)
            return comms.retry_as(connection, result_id, "%s-try%d" % (base, attempt),
                                  actor="desktop-user",
                                  reason="Retry requested in the desktop inbox",
                                  directory=self.directory)

    def check_receipt(self, connection, result_id):
        """Read an authenticated relay receipt; never reissue the mail request."""
        from . import dogmail
        with self._op_lock(connection):
            row = self._row(connection)
            if row["kind"] != "collie_mail":
                raise ChannelError("check this message in the provider's sent folder")
            result = comms.get_result(connection, result_id, include_private=True, directory=self.directory)
            if not result or result.get("state") not in {"unknown", "submitted"}:
                raise ChannelError("this message does not need a relay receipt check")
            receipt = dogmail.send_status(row["config"]["mailbox"], result_id, state_dir=self.root)
            if result["state"] == "unknown":
                comms.resolve_unknown(connection, result_id, actor="authenticated-relay-receipt",
                                      outcome="submitted", reason="Relay recorded provider acceptance",
                                      provider_message_id=receipt["provider_message_id"], directory=self.directory)
            return receipt

    def _start_pending(self, session, *, label):
        from . import web_tasks
        if os.path.normcase(os.path.realpath(self.directory)) != os.path.normcase(os.path.realpath(sessions._dir())):
            raise web_tasks.WebInputError("open this Collie profile before starting its saved tasks", 409)
        return web_tasks.start_pending(session, label=label)

    def capture_result(self, session, entry, outcome):
        """Save a reply from a completed, journalled task before another turn starts."""
        metadata = (entry or {}).get("metadata") or {}
        communication = metadata.get("communication") or {}
        if (not communication or not outcome.get("completed") or outcome.get("error")
                or outcome.get("canceled") or outcome.get("recovery_required")):
            return None
        connection, event_id = communication["connection"], communication["event"]
        row = self._row(connection)
        checked = sessions.load_checked(session, directory=self.directory)
        if checked["status"] != "ok":
            raise ChannelError("task result journal could not be read")
        receipts = [r for r in checked["session"].get("run_receipts", []) if r.get("input_id") == entry["id"]]
        if receipts:
            receipt = receipts[-1]
            if (not receipt.get("completed") or receipt.get("error") or receipt.get("canceled")
                    or receipt.get("recovery_required")):
                return None
            text = receipt.get("communication_answer")
            if isinstance(text, str) and text.strip():
                return self._save_answer(connection, event_id, session, text)
        messages = checked["session"].get("messages") or []
        start = next((i for i, message in enumerate(messages) if message.get("inbox_id") == entry["id"]), None)
        if start is None:
            raise ChannelError("accepted input is not in the task journal")
        answers = []
        for message in messages[start + 1:]:
            if message.get("role") == "user":
                break
            if message.get("role") == "assistant" and not message.get("tool_calls") and isinstance(message.get("content"), str):
                answers.append(message["content"])
        if not answers or not answers[-1].strip():
            return None
        return self._save_answer(connection, event_id, session, answers[-1])

    def _save_answer(self, connection, event_id, session, text):
        row = self._row(connection)
        if row["kind"] == "twilio" and len(text) > 1400:
            text = text[:1150] + "\n\nExcerpt only. The full result is saved in Collie task " + session + "."
        result_id = "result-" + _hash(connection + ":" + event_id)[:40]
        existing = comms.get_result(connection, result_id, directory=self.directory)
        if existing:
            return existing
        return self.prepare_reply(connection, result_id, text=text, event_id=event_id, automatic=True)

    def sweep(self, connection, older_than=SEND_CLAIM_TIMEOUT):
        """Abandoned send claims become ``unknown``.  Nothing is ever re-sent here.

        A claim older than ``older_than`` belonged to a process that did not come
        back to report an outcome.  The message may well have been delivered, so
        the only honest state is "we do not know": a person resolves it from the
        account's sent folder or the provider log, and only an explicit ``failed``
        resolution makes a retry available.
        """
        self._row(connection)
        return comms.sweep_sending(
            connection, older_than=float(older_than),
            reason="the sender did not report an outcome within %d seconds; delivery is unknown"
                   % int(older_than), directory=self.directory)

    def recover_acceptances(self, connection, limit=MAX_RECOVER):
        """Settle reservations and restart durable work a dead process left behind.

        Two distinct repairs, both idempotent:

        * A reservation (``acceptance.state == "enqueuing"``) is re-offered to
          ``comms.accept_event`` with *exactly* the terms frozen at reservation —
          the same mode, the stored config, the original actor and override — so
          the same entry id in the same session settles.  A fresh config is never
          built here: that would change the terms and be refused, or worse, run
          the person's message under settings they never saw.
        * A settled acceptance whose task-inbox entry is still ``pending`` is
          handed to ``web_tasks.start_pending``.  Anything already ``claimed``,
          ``consumed`` or ``canceled`` is left alone, which is what keeps
          completed work from running twice.

        One unrecoverable event is reported and stepped over, never allowed to
        starve the rest.

        Both classes are selected by the store, before any window is taken.  A
        window over "pending or accepted" would have spent this budget on
        whatever arrived most recently and never reached a reservation sitting
        behind a few hundred accepted messages — which is precisely the record
        that names a task nothing else knows exists.  Reservations are taken
        first for the same reason; what is left of the budget goes to resuming
        accepted work, oldest slots reserved so a permanently stuck task cannot
        hold the lane.
        """
        row = self._row(connection)
        out = {"settled": 0, "resumed": 0, "examined": 0, "issues": []}
        if not row.get("enabled"):
            out["issues"].append("connection is paused; acceptance recovery was skipped")
            return out
        budget = max(0, int(limit))
        # Disjoint by construction: the store only allows an ``enqueuing``
        # reservation on a pending event, and only an accepted event is owed a
        # reply, so no record is examined twice.
        reserved = comms.list_events(connection, needs="reservation", limit=budget,
                                     newest=True, reserve_oldest=budget // 2,
                                     include_private=True, directory=self.directory)
        room = max(0, budget - len(reserved))
        resumable = comms.list_events(connection, needs="reply", limit=room, newest=True,
                                      reserve_oldest=min(RECONCILE_OLDEST, room),
                                      include_private=True, directory=self.directory) if room else []
        for event in reserved + resumable:
            if not isinstance(event.get("acceptance_detail"), dict):
                continue
            out["examined"] += 1
            frozen = event["acceptance_detail"]
            try:
                if frozen.get("state") != "accepted":
                    settled = comms.accept_event(
                        connection, event["id"], actor=frozen.get("actor") or "channel-recovery",
                        mode=frozen.get("mode") or "follow_up", config=frozen.get("config"),
                        override_sender=bool(frozen.get("override_sender")),
                        directory=self.directory)
                    if settled.get("state") == "accepted":
                        out["settled"] += 1
                    session, entry_id = settled["session"], settled["entry_id"]
                else:
                    session, entry_id = frozen.get("session"), frozen.get("entry_id")
                if not session or not entry_id:
                    continue
                resumed = self._resume(session, entry_id)
                if resumed["started"]:
                    out["resumed"] += 1
                elif resumed["reason"]:
                    out["issues"].append("%s: %s" % (event["id"], resumed["reason"]))
            except Exception as exc:
                out["issues"].append("%s: %s" % (event["id"], _detail(
                    exc, "this accepted message could not be recovered; open it in the inbox")))
        return out

    def _resume(self, session, entry_id):
        """Restart a durable task only when the inbox and the session both allow it."""
        from . import task_inbox, web_tasks
        try:
            entry = task_inbox.get(session, entry_id, directory=self.directory)
        except task_inbox.InboxError as exc:
            return {"started": False, "reason": _detail(exc, "the task inbox could not be read")}
        if not entry or entry.get("compacted"):
            return {"started": False, "reason": ""}
        if entry.get("state") != "pending":
            # claimed (a runner holds it), consumed (the work happened) or
            # canceled.  None of these may be started again.
            return {"started": False, "reason": ""}
        state = sessions.recovery_state(session, directory=self.directory)
        if state and state.get("recovery_required"):
            return {"started": False,
                    "reason": "this task needs recovery in the conversation before it can continue"}
        try:
            outcome = self._start_pending(session, label="communication-recovery")
        except web_tasks.WebInputError as exc:
            # Busy or fenced.  The entry stays pending and the next tick retries.
            return {"started": False, "reason": str(exc)[:300]}
        return {"started": bool(outcome.get("started")), "reason": ""}

    def _reconcile_candidates(self, connection, limit=MAX_RECONCILE, oldest=RECONCILE_OLDEST):
        """Accepted messages still owed a reply, already fairly windowed.

        Eligibility is the store's own durable one — an accepted event with no
        settlement marker — and it is applied *before* the window, not after.
        That is the whole point: filtering afterwards meant a connection with
        more accepted messages than the window could hide every task that
        finished behind mail that had already been answered, and the lane would
        examine the same replied rows forever.  The window then reserves slots
        for the oldest of what is left, so a task that can never reconcile (its
        input was cancelled, its journal cannot be read) cannot take the whole
        budget every tick either.

        One bounded read of the document the store already loads; no secondary
        index to keep in step with it.
        """
        self._row(connection)
        return comms.list_events(connection, needs="reply", limit=limit, newest=True,
                                 reserve_oldest=oldest, include_private=True,
                                 directory=self.directory)

    def reconcile(self, connection, issues=None):
        """Recover completed replies; never infer task success from an old answer.

        ``issues`` collects per-event problems so that one session whose journal
        cannot be read does not hide every other recoverable reply.
        """
        from . import task_inbox
        recovered = 0
        for event in self._reconcile_candidates(connection):
            try:
                acceptance = event.get("acceptance") or {}
                sid, eid = acceptance.get("session"), acceptance.get("entry_id")
                if not sid or not eid:
                    continue
                result_id = "result-" + _hash(connection + ":" + event["id"])[:40]
                if comms.get_result(connection, result_id, directory=self.directory):
                    # The reply exists but the event does not say so: saved
                    # before settlement markers existed, or by a process that
                    # died in the one write between the two.  The outbox record
                    # (or its tombstone) is the evidence, and stamping the event
                    # from it is what lets the message compact later instead of
                    # being re-examined on every pass forever.
                    comms.mark_event_settled(connection, event["id"], disposition="replied",
                                             result_id=result_id, actor="channel-reconcile",
                                             reason="Reply was already saved",
                                             directory=self.directory)
                    continue
                checked = sessions.load_checked(sid, directory=self.directory)
                if checked["status"] != "ok":
                    continue
                matches = [r for r in checked["session"].get("run_receipts", []) if r.get("input_id") == eid]
                if not matches:
                    continue
                entry = task_inbox.get(sid, eid, directory=self.directory)
                if entry and self.capture_result(sid, entry, matches[-1]):
                    recovered += 1
            except Exception as exc:
                # One unreadable journal must not hide every other saved reply.
                if issues is None:
                    raise
                issues.append("%s: %s" % (event["id"], _detail(
                    exc, "this task's reply could not be recovered; open it in the inbox")))
        return recovered

    def _draft_lane(self, connection, issues=None):
        """Offer allowed, attachment-free messages to the no-tools drafting template.

        Automatic drafting is capped at ``MAX_DRAFTS`` an hour.  Two things that
        cap must not do: stop counting, and stop saying anything.

        The hour is counted by *when each acceptance was made*, across every
        acceptance the connection still retains — not by reading the newest N
        events and hoping the hour is inside them.  Those are different
        questions the moment a mailbox is synced with ``history="all"``: mail
        that arrived years ago and was accepted this morning sits at the front
        of the event list, so a window taken by arrival order would not see
        today's acceptances at all and would report a busy connection as idle.

        Whatever the cap defers is reported, with the time it resumes, so a
        connection is never left looking healthy while new mail sits untouched.
        Nothing is dropped — a deferred message stays pending and is offered
        again on the next pass.
        """
        drafted, now = 0, time.time()
        pending = comms.list_events(connection, states=["pending"], limit=DRAFT_WINDOW,
                                    include_private=True, directory=self.directory)
        eligible = [e for e in pending
                    if e.get("sender_allowed") and not e.get("attachment_refs")]
        recent = comms.count_acceptances(connection, since=now - 3600, directory=self.directory)
        budget = max(0, MAX_DRAFTS - recent["count"])
        for event in eligible[:budget]:
            self.accept(connection, event["id"], draft=True)
            drafted += 1
        deferred = len(eligible) - min(len(eligible), budget)
        if deferred and issues is not None:
            resumes = (time.strftime("%H:%M", time.localtime(recent["oldest"] + 3600))
                       if recent["count"] else "")
            issues.append(
                "%d received message(s) are waiting: automatic drafting is limited to %d an hour "
                "and resumes about %s. Nothing was discarded; open the inbox to reply now."
                % (deferred, MAX_DRAFTS, resumes or "shortly"))
        return drafted

    def _delivery_lane(self, connection, row, issues=None):
        """Sweep stale claims, then send only what was marked automatic."""
        out = {"swept": len(self.sweep(connection)), "sent": 0, "attempted": 0}
        if not row.get("auto_reply"):
            return out
        pending = comms.list_results(connection, states=["pending"], limit=comms.MAX_OPEN_OUTBOX,
                                     include_private=True, directory=self.directory)
        for result in pending:
            # Calls always require a direct invocation from the UI.
            if ((result.get("metadata") or {}).get("auto_eligible")
                    and not (result.get("metadata") or {}).get("speak")):
                outcome = self.send(connection, result["id"])
                out["attempted"] += 1
                if outcome.get("state") == "submitted":
                    out["sent"] += 1
                elif issues is not None:
                    issues.append({"lane": "delivery", "error":
                                   "A reply could not be confirmed as submitted; check the outbox"})
                if out["attempted"] >= MAX_AUTO_SEND:
                    break
        return out

    def tick(self):
        """One bounded pass in independent lanes; only enabled connections run.

        The lanes — acceptance recovery, reply reconciliation, receiving,
        drafting, delivery — fail independently on purpose.  A mailbox that
        cannot be opened must not stop a reply that is already prepared from
        going out, and a provider that refuses a send must not stop new mail
        from being received.  Each lane reports its own problem; only the
        receiving lane decides the connection's ``status``, and a connection
        with any lane problem never reports a clean all-clear.
        """
        report = []
        for connection, row in self._load()["connections"].items():
            if not row.get("enabled"):
                continue
            entry, issues = {"connection": connection}, []

            def lane(name, call, fallback):
                try:
                    return call()
                except Exception as exc:
                    issues.append({"lane": name, "error": _detail(exc, fallback)})
                    return None

            recovery = lane("recovery", lambda: self.recover_acceptances(connection),
                            "Accepted work could not be checked; review the connection")
            if recovery:
                issues.extend({"lane": "recovery", "error": text} for text in recovery["issues"])
                entry["recovered"] = {k: recovery[k] for k in ("settled", "resumed", "examined")}
            entry["reconciled"] = lane("reconcile", lambda: self.reconcile(connection, issues),
                                       "Saved replies could not be checked") or 0
            received = lane("poll", lambda: self.poll(connection),
                            "Connection check failed; review settings")
            if received is None:
                # Only the receiving lane owns connection status, and it is the
                # one that just failed.
                self._status(connection, "error", issues[-1]["error"])
            else:
                entry.update(received)
            if row.get("mode") == "draft":
                deferrals = []
                entry["drafted"] = lane("draft", lambda: self._draft_lane(connection, deferrals),
                                        "Drafting could not be started for this connection") or 0
                issues.extend({"lane": "draft", "error": text} for text in deferrals)
            delivery = lane("delivery", lambda: self._delivery_lane(connection, row, issues),
                            "Sending could not be completed; check the outbox")
            if delivery:
                entry.update(delivery)
            if issues:
                entry["issues"] = issues
                entry.setdefault("status", "attention")
                if received is not None:
                    # Received fine, but something else did not: say so rather
                    # than leaving a connected badge as the whole story.
                    self._status(connection, "connected", warning=issues[0]["error"])
            report.append(entry)
        return report


_PUMPS = {}
_PUMP_LOCK = threading.Lock()


def start_pump(state_dir=None, interval=60):
    root = active_state_dir(state_dir)
    with _PUMP_LOCK:
        existing = _PUMPS.get(root)
        if existing and existing[0].is_alive():
            return existing[1]
        stop = threading.Event()
        def run():
            while not stop.is_set():
                try:
                    ChannelService(root).tick()
                except Exception:
                    pass  # The connections API exposes a malformed settings file.
                try:
                    from . import daily_brief_schedule
                    daily_brief_schedule.tick(root)
                except Exception:
                    pass  # Daily Brief settings expose their own failure; other lanes continue.
                stop.wait(max(10, interval))
        thread = threading.Thread(target=run, name="collie-channels", daemon=True)
        _PUMPS[root] = (thread, stop)
        thread.start()
        return stop
