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

## Reply to the brief

A reply from the connection's owner can use the exact retained snapshot from that
email. For example, “What was the second appointment?” refers to that morning's
text, even if today's dashboard has changed. If the thread cannot be matched
reliably, Collie does not substitute a different day's brief.

Normal inbox controls still apply: prepare a reply, or explicitly choose **Run as
project task** for work requiring tools. A reply to a brief does not silently
approve project work or external actions.

## Available now

The desktop and email share one brief snapshot, with English and Chinese display.
The page supports a narrow browser; there is no standalone mobile app. The brief
reads local calendar, task, approval and communication records. It does not yet
perform a full semantic review of a Gmail mailbox or fetch general news.

Live email delivery requires a configured service account. The core brief remains
useful without one. Developers can read the [implementation and API details](daily-brief-design.md).
