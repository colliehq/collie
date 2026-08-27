# Meeting notes

Collie can record meeting audio, keep rough notes while you talk, transcribe the recording, and
produce an evidence-backed meeting note. Open `collie web` and choose the notebook icon in the top
bar.

## Meeting reminders

The **Upcoming meetings** panel can import an iCalendar (`.ics`) export or keep a meeting you add
manually. The import is parsed by the authenticated local Collie server; Collie does not fetch the
calendar URL, retain attendee addresses, or send calendar metadata to a model provider. Common
daily, weekly, and monthly recurring events, exclusions, moved occurrences, meeting links, and UTC
or platform-known IANA time zones are supported. Floating or unknown zones use the computer's local
time, including its date-specific daylight-saving offset. Re-import an export to refresh its
occurrences.

Choose **Enable alerts** to grant this browser system-notification permission. Enable **Background
system reminders** when you also want the long-lived Collie host to check locally after the Meeting
Notes page closes. The native notifier is opt-in, keeps a bounded local delivery receipt, and never
starts recording. Collie can prompt:

- before the event at the lead time you choose;
- when the event starts;
- after the scheduled end, but only if you prepared or recorded that event;
- when a shared tab/window stops providing meeting audio; and
- when an opt-in AI note is ready for review.

System notifications hide meeting titles by default; the private in-app prompt can show the title.
You can snooze, mute the rest of the day, mute one recurring series, or turn reminders off. In-app
actions require the Meeting Notes page; background OS delivery continues while the Collie host is
running. If browser notification permission is blocked, visible in-app prompts and an enabled native
notifier still work independently.

Reminder settings include a local sensitive-term list. Matching titles, agendas, or locations are
shown in the upcoming list but never produce a recording suggestion. The defaults cover common
medical, HR, legal, and banking language and can be edited. This is a conservative local text rule,
not a claim that Collie can reliably classify every sensitive conversation.

Choosing **Prepare note** only prefills the title, agenda, template, and language. A series can
remember its template, language, and reminder preference. It never remembers recording consent or
the AI-processing switch. You must still select audio sources and provide a fresh participant-
consent confirmation before every recording. Collie never starts capture from a calendar event.

## What is captured

- **Microphone** is suitable for in-person meetings and captures your side of an online call.
- **Meeting/system audio** asks the browser to share a tab or window. In the browser chooser, select
  the meeting source and enable **Share audio**. Browser and operating-system support varies.
- Collie records audio only. The display-selection prompt is required by browser security rules for
  system audio; its video track is not written to the meeting recording.
- Rough notes are saved locally during the meeting. They guide the final note without being treated
  as transcript evidence.

Use headphones to avoid echo. If the chosen shared source supplies no audio, Collie says so and can
continue with the microphone instead of silently producing a one-sided transcript.

## Consent and privacy

Every meeting requires a fresh confirmation that participants were informed and consented. Collie
does not start recording from calendar detection, a background microphone event, or a previous
blanket permission.

Recordings and notes are private local files under `~/.collie/meetings/`. AI processing is off by
default. If you explicitly enable **Transcribe and create AI notes**:

1. the recording is sent to OpenAI's audio transcription endpoint using your `OPENAI_API_KEY`;
2. the resulting transcript is sent to the model provider currently configured in Collie to create
   the meeting note;
3. API-provider usage and data policies apply.

Collie never auto-shares the result with attendees. Export is a local Markdown download. Deleting a
meeting note removes that meeting's local recording, transcript, summary, and ingest journal.

## What the note contains

The default note distinguishes:

- summary and key points;
- decisions versus proposals;
- action items with owner, due date, and transcript timestamp;
- open questions.

Unknown owners and dates remain `Unassigned` and `No date`. Speaker diarization uses anonymous
labels unless the transcription service has reliable identity evidence. The transcript remains the
source of truth and is shown alongside the generated note.

Templates adjust what gets emphasized for stand-ups, 1:1s, planning sessions, customer calls, and
interviews. Interview notes deliberately stop short of making a hiring decision.

## Failure and recovery

Audio is uploaded to the local Collie server in ordered chunks. A retried chunk is idempotent; a
gap or changed retry is rejected. Collie does not mark a meeting saved until every chunk is present
and the final recording has been durably written. If transcription or summarization fails, the
original local recording and rough notes remain available and AI processing can be retried.

Long transcripts are summarized hierarchically in bounded pieces before the final note, while
preserving timestamp citations. A server restart can resume a meeting whose AI-processing state was
durably queued.

## Deliberate next steps

The first version does not silently turn extracted action items into calendar events, tickets,
emails, or CRM updates. Those are consequential external writes and should be separate Connections
with previews, exact destinations, and Collie's normal approval receipts. Natural follow-ons are:

- local/offline Whisper-compatible transcription;
- optional calendar-provider sync with narrowly scoped read-only permissions and a clear refresh
  indicator (the current release uses explicit local `.ics` import);
- editable speaker names and organization-specific vocabulary;
- search and chat across selected meetings;
- reviewed action-item export to Linear, GitHub, Asana, or a calendar;
- retention policies that can delete audio or transcripts while keeping the final note.
