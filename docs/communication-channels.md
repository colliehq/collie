# Email and phone

Open **Email & phone** in the desktop app, or visit `/communications` in its web
interface. An account is optional: local tasks and the Daily Brief work without one.
The desktop starts as **Collie**; an optional display name lives in Settings and
does not determine your email address or phone number.

## Connect an account

| Connection | Required setup | Available workflow |
|---|---|---|
| Email account | IMAP and SMTP server details, your mailbox credentials, and your owner reply address | Receive messages, prepare replies, send reviewed results |
| Collie Mail | A verified handle and a mailbox on a configured Collie relay | Receive sealed mail and submit replies to the verified owner |
| Phone | Twilio account credentials, a messaging-capable number, and your owner number | Receive SMS, prepare or send SMS results, and read a reviewed result in an outbound voice call |

Credentials are saved locally and are not filled back into the settings form.
Provider account rules still apply. An IMAP/SMTP account may require an app password;
this setup screen does not implement provider-specific OAuth login.

New connections import messages **from now on** by default. Choose existing history
when connecting if you want it imported. A paused connection keeps its messages,
tasks and drafts. Disconnecting removes transport credentials; a Collie Mail
identity stays on the device so reconnecting does not lose its keys.

## Receive, prepare, review

Messages first appear in the inbox. You can prepare a reply, dismiss a message, or
choose **Run as project task** for work that needs project tools. An email's sender
header and instructions in its body do not grant those tools access.

Optional automatic drafting uses a restricted workflow with no tools, project
memory or earlier private task history. It has an hourly limit; deferred messages
remain visible. Automatic reply submission is a separate setting and is off by
default. Neither setting is enabled by connecting an account.

Text and supported image attachments can accompany a task. Unsupported or incomplete
attachments stay available for review; Collie does not claim to have read them.

## Review the outbox

Edit a pending or refused draft before sending it, or discard it. An edit produces
a new draft and preserves the earlier record. A known failed attempt can be prepared
for another try; its new request ID avoids replaying a relay's earlier refusal.

**Provider accepted** means the provider acknowledged the request. It does not
prove delivery or that somebody answered a call. An **unknown** outcome is kept
for checking and is never automatically resent. Collie Mail supports a receipt
lookup; phone connections can query the provider's recorded status.

Incoming task acceptance, queued input and outgoing results survive restarts.
Reopening the page does not authorize a second send.

Changing the result recipient does not retarget a task that was already accepted.
Its answer remains in the original conversation. Use **Open task** to review it,
then **Write reviewed reply** on the received message to prepare a reply for the
address shown. Saving that draft clears the message's outstanding-reply reminder;
it waits for **Send now** even when automatic replies are enabled.

## Morning email

Open [Daily Brief](daily-brief.md), expand **Email settings**, choose an existing
email connection and turn it on. This sends to the owner address already bound
to that connection. It does not change automatic replies or create another account.
The computer must be running during the configured morning window; missed days
are skipped. Replying to a retained brief can restore the exact snapshot that was
sent, while ordinary inbox rules still govern what the reply may do.

## Service availability

The IMAP/SMTP, Collie Mail and Twilio integrations have protocol and recovery tests
using isolated fake providers. Live delivery needs your configured provider account.
The Collie Mail relay's broader sending-domain entitlement remains unverified;
Email Routing alone sends only to Cloudflare-account-verified destinations. See
[relay setup](mail-relay.md). A phone account and number are not provisioned by Collie.

Voice currently reads a prepared result in an outbound call. It is not an
interactive, real-time phone conversation service.
