"""Authenticated desktop communication actions. Provider secrets are write-only."""
from __future__ import annotations

from . import communications as comms, dogmail
from .channel_service import ChannelError, ChannelService


def mail_identity(root):
    data = dogmail.load(root)
    handle = data.get("handle") or {}
    return {"handle": handle.get("name") or "", "verified": bool(handle.get("verified")),
            "email": handle.get("email") or "",
            "mailboxes": [{"name": name, "address": row.get("address") or ""}
                          for name, row in (data.get("dogs") or {}).items() if not row.get("pending")]}


def read(root, section="", connection=""):
    host = ChannelService(root)
    if not section:
        return dict(host.overview(), mail_identity=mail_identity(root))
    if section == "events":
        return {"events": host.events(connection)}
    if section == "results":
        return {"results": host.results(connection)}
    raise ChannelError("unknown communication view")


def perform(root, body):
    host = ChannelService(root)
    action = body.get("action")
    connection = body.get("connection")
    if action == "configure":
        return host.configure(connection, kind=body.get("kind"), config=body.get("config"),
                              owner=body.get("owner"), credentials=body.get("credentials"),
                              workspace=body.get("workspace", ""), mode=body.get("mode", "manual"),
                              enabled=body.get("enabled", True), auto_reply=body.get("auto_reply", False),
                              history=body.get("history", "new"))
    if action == "enable":
        return host.set_enabled(connection, body.get("enabled"))
    if action == "disconnect":
        return host.disconnect(connection)
    if action == "probe":
        return host.probe(connection)
    if action == "poll":
        return host.poll(connection)
    if action in {"draft", "task"}:
        return host.accept(connection, body.get("event"), draft=action == "draft", approved=True)
    if action == "reject":
        return host.reject(connection, body.get("event"))
    if action == "prepare":
        if type(body.get("speak", False)) is not bool:
            raise ChannelError("speak must be true or false")
        return host.prepare_reply(connection, body.get("id"), text=body.get("text"),
                                  event_id=body.get("event", ""), speak=body.get("speak", False))
    if action == "send":
        return host.send(connection, body.get("id"))
    if action == "revise":
        host._row(connection)
        return comms.revise_result(connection, body.get("id"), body.get("new_id"), text=body.get("text"),
                                   actor="desktop-user", expected_digest=body.get("digest"), directory=host.directory)
    if action == "discard":
        host._row(connection)
        return comms.cancel_result(connection, body.get("id"), actor="desktop-user",
                                   expected_digest=body.get("digest"), directory=host.directory)
    if action == "retry":
        host._row(connection)
        return comms.retry(connection, body.get("id"), actor="desktop-user",
                           reason="Retry requested in the desktop inbox", directory=host.directory)
    if action == "resolve":
        host._row(connection)
        return comms.resolve_unknown(connection, body.get("id"), actor="desktop-user",
                                     outcome=body.get("outcome"), reason=body.get("reason", ""),
                                     directory=host.directory)
    if action == "provider_status":
        row = host._row(connection)
        if row["kind"] == "collie_mail":
            return host.check_receipt(connection, body.get("id"))
        if row["kind"] != "twilio":
            raise ChannelError("check this message in the provider's sent folder")
        result = comms.get_result(connection, body.get("id"), include_private=True, directory=host.directory)
        provider_id = ((result or {}).get("outcome_detail") or {}).get("provider_message_id")
        if not provider_id:
            raise ChannelError("no provider receipt is available for this message")
        from . import channel_secrets, phone_transport
        return phone_transport.fetch_status(row["config"], channel_secrets.get(connection, state_dir=root), provider_id)
    if action == "mail_claim":
        return dogmail.claim_handle(body.get("handle", ""), body.get("email", ""), state_dir=root)
    if action == "mail_verify":
        return dogmail.verify_handle(body.get("code", ""), state_dir=root)
    if action == "mail_create":
        return dogmail.claim_dog(body.get("name", ""), state_dir=root)
    raise ChannelError("unknown communication action")
