# Privacy policy

Collie is a local, open-source developer tool. It runs on your own computer, under your control, and
is built to keep your data with you.

## App telemetry

The open-source local core needs **no Collie account or sign-up** and has no advertising, usage
analytics, telemetry, or crash reporting. It does not send usage events home. The optional Collie
Online preview uses an account only when you choose Connected Mode. Data leaves your machine only
when a feature you choose inherently needs a network destination, as described below. The
collie.run website, Collie Online, and the optional hosted phone relay are separate services with
limited data flows described here.

## Where your data goes when you use a feature

Collie only sends data off your machine for features you turn on, and only to the destination that
feature inherently requires:

- **Your chosen model provider.** When you run the agent, your prompts and the code/context it needs
  are sent to the model provider *you* configured (e.g. your own Anthropic/OpenAI/DeepSeek API key,
  your Claude subscription, or a fully **local** model via Ollama — in which case nothing leaves the
  machine at all). This is the same data flow as any AI coding tool, to a provider you pick.
- **Web search / fetch (opt-in).** If you enable it, Collie fetches public web pages you or the task
  reference (a keyless DuckDuckGo/SearXNG query, or pages via your own browser). No account.
- **AI meeting notes (separately opt-in for every meeting).** Meeting audio and rough notes stay
  under `~/.collie/meetings/` by default. If you enable AI processing before recording, audio is
  sent to OpenAI's transcription endpoint using your `OPENAI_API_KEY`; the transcript is then sent
  to your configured Collie model provider to create the note. Collie displays both destinations
  before recording, never auto-shares the result, and retains the original transcript as evidence.
- **Meeting schedules and reminders (local).** An `.ics` file is read only after you select it and
  is parsed on the authenticated loopback server. Upcoming event metadata and reminder preferences
  stay in `~/.collie/meeting-reminders.json`; attendee fields are not retained. Collie does not fetch
  calendar feeds or send schedule metadata to a model. Reminder detection can prefill a note but
  cannot start recording or carry consent from one meeting to another.
- **System-design interview assistance (separately opt-in for every session).** VocalCode owns
  local capture and transcription; Collie does not read its audio. After you confirm everyone has
  agreed to recording and AI assistance, you may separately share a bounded tail of the current
  session's transcript with your configured model provider and grant Collie permission to add
  editable shapes to one Chrome board tab you explicitly attach. Stopping the session clears both
  permissions. Collie does not hide itself, impersonate the candidate, bypass proctoring rules, or
  delete existing board content.
- **Phone remote (opt-in).** If you enable `collie web --remote`, your phone can reach your desktop
  through the collie.run relay. Hosted remote request and response contents are **end-to-end
  encrypted**; the relay handles necessary routing metadata such as room or device identifiers,
  request timing, and approximate message sizes. Pairing requires a code shown on your own screen
  plus your approval on the desktop.
- **Collie Online / Connected Mode (opt-in).** If you sign in and pair devices, selected sealed
  project memory, restrictive policy, learned-workflow derivatives, shared-connection envelopes,
  and Mission payloads are end-to-end encrypted between your devices. The service necessarily
  processes account, workspace/project, device, timing, presence, lease, and approximate-size
  metadata to route and coordinate them. Raw outside-AI observations, browser-history summaries,
  and typed personal events are not uploaded by this build. Data explicitly classified
  `cloud_indexed` is server-readable and outside the sealed-content guarantee; choosing that class
  or optional Cloud Light processing is a separate, visible decision. Your devices remain the
  execution authority and cloud delivery never grants a Mission permission to act locally.
- **"Ask Collie" chat on collie.run (optional).** Nothing is sent until you submit the website form.
  Your question and up to six recent messages from that demo go to Cloudflare Workers AI. For abuse
  prevention, the service processes your network address through a secret-keyed one-way function to
  derive a new identifier each day. The raw address is not used as a Durable Object name or stored by
  the site code; the object stores only the counter and expiry, which are deleted within 48 hours.
  Cloudflare still processes ordinary request data to deliver and secure the service. This is
  unrelated to the app and does not attach your product files or local sessions. The static site
  loads no analytics or tracking beacon.

Local features — driving your logged-in browser, arranging your desktop, controlling other apps,
processing personal intelligence and meeting reminders, recording your screen, and recording a
meeting with AI processing disabled — run **entirely on your own computer**. Their output stays local
unless you send it somewhere yourself.

### Outside-AI learning and Personal intelligence

Outside-AI learning is **off by default** and has three visible modes: Off, Activity only, and
Personal intelligence. Enabling it requires one affirmative, versioned consent. Collie records the
consent version and time locally so the choice is auditable. It then runs quietly in the background;
it does not ask again for each sample. Turning the mode off stops collection, records withdrawal,
and requires fresh consent before it can be enabled again.

Activity-only mode stores foreground executable identity, coarse duration, idle/session boundaries,
and an allowlist of operating-system sleep/resume/start/stop event IDs. It does not store window
titles, keystrokes, clipboard contents, screenshots, document paths, command lines, or system-log
messages. Raw activity observations expire locally (seven days by default) and never sync.

Browser-history learning is a separate optional source inside Personal intelligence. One click shows
this disclosure and requests Chrome's native optional `history` permission. After that, the extension
can refresh in the background without repeated prompts. Each refresh reads at most the most recent
14 days and reduces records in memory to web origin, local day/hour bucket, visit count, and typed
count. Page titles, URL paths, query strings, searches, and raw history records are discarded before
the extension sends anything to Collie's authenticated loopback service. Collie retains no more than
40 top origins per day for 14 days; those local summaries are not uploaded. Disconnecting the source
revokes the optional Chrome permission and deletes its summaries.

Browsing is only a habit signal. Visiting a store or tracking page is never treated as proof of a
purchase. Delivery, reservation, bill, appointment, renewal, and follow-up reminders require a typed
event with source evidence and confidence (or an explicitly confirmed manual entry), and reminders
have notification-only authority. This build does not upload personal-history summaries or these
personal events. Any future cross-device derivative sync is a separate setting and must preserve
end-to-end encryption; changing that data practice will require a new prominent disclosure.

The browser extension's current-page side chat sends the question plus the displayed page title,
URL, and any text you explicitly selected to the model provider configured in your local Collie.
It does so only after you press Send. The bridge token, Web UI token, cookies, and raw browser profile
are not sent to the page or to Collie's servers. Browser tab ownership, pause state, and per-site
input preferences stay in Chrome's extension storage; Collie's persistent navigation preference
stays in local `~/.collie/settings.json`.

## Your machine, your control

Every capability that touches your real environment is **opt-in and user-initiated**: the browser
bridge requires you to install and enable an extension; remote access requires you to turn it on and
pair a device; screen recording only runs when you start it; meeting recording requires a fresh
confirmation that participants were informed and consented. Collie automates your *own* computer at
your request — the way tools like Playwright, AutoHotkey, or an RPA runner do — and never acts on
anyone else's system.

MCP recommendations first classify the requested outcome entirely on the device. Collie's reviewed
catalog needs no discovery request. Public MCP Registry search is off until one disclosed consent;
after that, only allowlisted generic labels such as `calendar`, `github`, or `music` are sent. The
original request, project names, paths, URLs, searches, and conversation are not included. Public
metadata is cached under `~/.collie` and can be deleted with the rest of local state. Connecting a
recommended provider is a separate exact-endpoint action and sends data later only when one of that
provider's tools is actually used.

## Data you can delete

Collie's local state (settings, memory, sessions, meeting schedules, meeting recordings/notes,
paired-device list) lives under `~/.collie` on your machine. A meeting recording or manually added
scheduled meeting can be deleted individually from Meeting Notes; delete the whole folder to remove
all Collie state. Uninstalling Collie removes the program.

## Changes

This policy applies to the open-source Collie project. Material changes will be noted in the
repository. Questions: [github.com/colliehq/collie/issues](https://github.com/colliehq/collie/issues).
