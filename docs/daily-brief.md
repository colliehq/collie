# Daily Brief

Open **Daily Brief** from Today, or visit `/brief` in Collie's web interface. It
brings together what needs your attention, today's appointments, ongoing work and
prepared replies. No account or nickname is required to read it.

## Read and act

The first section highlights up to three priorities. Each item names its source
and opens the relevant conversation, calendar or communication inbox. The remaining
sections show the day's actual appointments and task progress. Canceled or
unfinished work is not counted as completed.

**Hide** removes an unchanged item from the brief. **Snooze** pauses it for a day.
Neither action cancels or completes the underlying task. Hidden items retain their
names and a **Bring back** button; changed items return automatically. Decisions
that require your answer remain visible.

Source timestamps show when Collie read its local records. An unavailable or partly
read source is reported explicitly. An empty calendar does not mean you have no
appointments if no calendar is connected.

For large task histories, the brief reads a recent window instead of opening every
conversation. It tells you how many session inboxes were not checked, and never
assumes that the unread portion is clear. This does not change task scheduling or
remove older work.

## Your to-dos

**Your to-dos** is a list you keep yourself, on this computer. Add a line and,
if you like, a due date. A to-do that is late or due today also appears under
**Needs you**; one you tick off today is listed as finished today. To-dos with a
later date, or none, stay in the list without crowding your day. Hiding a to-do from
the brief does not tick it off.

If you edit the same to-do in two windows, the second save is refused and your
words stay in the form: the list shows the other window's version, and saving again
replaces it. Anything you type while a save is still on its way is kept.

## News from your feeds

News is off until you add a feed. Expand **News feeds and topics**, list RSS or
Atom addresses that start with `https://` (up to eight), and optionally some topics.
With topics, only headlines whose title or summary contains one of them as a whole
word are shown. Choose how often to check, from every 15 minutes to once a day, and
how many headlines the brief keeps.

Saving reads new feeds at once; **Check now** reads them all again. Otherwise Collie
checks in the background while it is running, after you have opened the brief. Each
feed shows when it was last read and, if the last attempt failed, why; its earlier
headlines stay until a later read succeeds.

Headlines are for reading. Each one opens the article in your browser. A headline
never becomes a task, never appears under Needs you, and is never given to a model as
something to do. If the morning email is on, the headlines are included in it. See
the [privacy policy](privacy.md) for exactly what these requests send.

## Receive a morning email

1. Connect an email account in [Email & phone](communication-channels.md).
2. In Daily Brief, expand **Email settings** and choose that connection.
3. Set your local time and language, then choose **Turn on and save**.

Daily email is off by default. It goes only to the owner address already saved
with the chosen connection. The separate automatic-reply setting is unchanged.
Use **Preview email text** to inspect the snapshot currently on screen without
sending it. Refreshing status preserves unsaved scheduling edits.

Collie must be running during the morning window. The default window extends four
hours after the selected time; a missed day is skipped instead of sent in a burst
later. Time zones include daylight-saving changes. Pausing an email connection
keeps the schedule but prevents sending until that connection is ready again.

The delivery history distinguishes **provider accepted**, **failed** and
**unconfirmed**. Provider acceptance is not proof of delivery. An unconfirmed
attempt is never automatically resent. Turning the schedule off prevents new
submissions; a submission already in progress may still finish.

## Get the morning report instead

In **Email settings**, set **What to send** to **The morning report** and save. The
morning email is then the morning report (the same one `collie report build` makes) — what went well
overnight, quick things for you, replies that are ready, your projects and a few reads —
as a designed email, with the same report as plain text inside it for mail apps that
show no HTML. Everything above still applies: the same account, time, window, one
email a morning, and the same delivery history.

- **One email a morning, never two.** The report replaces the brief. If you switch
  after that morning's email has gone, the change starts tomorrow.
- **It is built when the window opens.** Collie reads your connected sources and asks
  your model, so the email arrives a minute or two after the time you chose. A build
  that fails is tried again a few minutes later, three times at most; after that the
  day is marked as an error and nothing is sent. Once built, the email is frozen: a
  restart sends that one and never builds a second.
- **Write reply drafts in Gmail** (on by default) lets the report put suggested
  replies in your Gmail drafts, each addressed only to whoever wrote to you. Nothing is
  sent from there; you open each draft and send it yourself. If the
  `COLLIE_REPORT_GMAIL_DRAFTS` environment variable is set, the switch shows that and
  cannot be changed on the page.
- **Send me one now** builds a report straight away and emails it to the saved
  account, after you confirm. It is a real email. It does not replace the morning's
  email, only one can be on its way at a time, and at most three go out a day.

**What reaches your inbox.** With an **email account** connection, Collie's own SMTP
session sends one message: the plain text first, then the designed page, with the
dog's picture attached inside the message (a `cid:` image), so no mail app has to
fetch anything and nothing reports back when you open it. With **Collie Mail**, the
relay at `mail.collie.run` carries the same designed email once it has been updated to
a version that accepts pages; until then Collie asks the relay first, sends the plain
text alone, and the history says the report “went as plain text” and why. Links in
the report are `https://` only; there are no scripts, web fonts or remote images.

## Reply to the brief

A reply from the connection's owner can use the exact retained snapshot from that
email. For example, “What was the second appointment?” refers to that morning's
text, even if today's dashboard has changed. If the thread cannot be matched
reliably, Collie does not substitute a different day's brief. A reply to the morning
report works the same way, with the report's plain text.

Normal inbox controls still apply: prepare a reply, or explicitly choose **Run as
project task** for work requiring tools. A reply to a brief does not silently
approve project work or external actions.

**Mute a project from the report.** Reply to the morning report with
`mute <project>` as the first line — the name the report shows, such as
`colliehq/collie`, or just `collie`. Collie adds it to *Projects to leave out*
(the `REPORT_MUTED` setting), answers in the same thread to confirm, and the next
report leaves it out. Only your own reply, from the connection's owner address and in
the thread of a report Collie sent you, counts; “mute” anywhere but the first line, a
reply to the plain brief, or an automatic message does nothing. To hear about the
project again, remove it under Settings → Morning report → Projects to leave out.

## Available now

The desktop and email share one brief snapshot, with English and Chinese display.
The page supports a narrow browser; there is no standalone mobile app. The brief
reads local calendar, task, approval and communication records, and your own to-do
list; the only news is from feeds you add. It does not yet
perform a full semantic review of a Gmail mailbox.

Live email delivery requires a configured service account. The core brief remains
useful without one. Developers can read the [implementation and API details](daily-brief-design.md).
