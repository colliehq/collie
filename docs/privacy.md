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
- **Live Copilot (separately started for every session).** An optional task description may be empty.
  Window awareness retains foreground application names and window titles. Active-interface
  awareness retains a bounded semantic summary of accessibility control types and labels, which may
  contain visible page or document text. Optional activity pulses retain interaction timing and the
  focused control type. These modes discard field values and do not log raw keys, clipboard,
  passwords, or screenshots.
  Conversation mode requires participant consent and
  uses Collie's first-party microphone/system-audio capture; transient audio chunks are deleted after
  the configured speech service returns transcript text. Transcript and compact live understanding
  may be sent to the providers disclosed in the UI. Suggestions have no execution authority. A
  shortcut handoff is an explicit user action. At that moment Collie retains the exact foreground
  process name, PID/window handle, window title, and a bounded set of accessibility control types and
  labels so the requested action can return to the correct window. Field values, keys, clipboard
  content, and screenshots remain excluded. The shortcut's one-shot command audio is processed by
  Windows' local speech recognizer; only recognized text is sent to the configured Collie model.
  The exact capsule command grants authority only for that turn and captured target: ordinary and
  reversible actions can proceed, while a commit must be explicitly requested and purchases,
  security changes, secrets, target changes, and ambiguous irreversible actions remain gated.
  Interactive tasks and background Missions still use the ordinary recovery boundaries. Stopping
  clears live capture and surface authority.
- **Update checks (on request, or daily once you turn them on).** Pressing *Check for updates* in
  Settings, running `collie update`, or enabling *Check for updates automatically* (off by default;
  then about once a day) asks GitHub's public release API (`api.github.com`) for the latest Collie
  release. The request carries no account, identifier or information about your work; GitHub sees
  your network address as with any web request. The answer is kept in
  `~/.collie/update-status.json`. Installing always waits for you to press *Install* or run
  `collie update --yes`, and downloads only the release you were shown, from GitHub.
- **Weather on the live wallpaper (opt-in wallpaper).** While the desktop wallpaper runs with its
  clock showing, Collie's local server asks `ipapi.co` for an approximate location from your
  network address, then `api.open-meteo.com` for the current weather at that point (the forecast
  request carries only that latitude and longitude). It asks at most once every 15 minutes, however
  many desktop windows are open; after a failed attempt it waits 1 minute, then 2, 4, 8 and 16, and
  then 30 minutes between tries. Both requests identify themselves with the User-Agent
  `collie-desktop-weather (+https://github.com/colliehq/collie)`. Nothing about you or your work is
  sent. To stop both requests, untick *Weather* in the desktop's edit mode (the pencil at the
  bottom right), which saves `{"widgets": {"clock": {"weather": false}}}` into
  `~/.collie/desktop.json`, or write that setting there yourself (create the file if it does not
  exist, and merge it into what is already there). Turning the clock off stops them too. The
  server checks this before asking, and the clock stays. If that file exists but cannot be read,
  for example after a typo, the weather stays off until it is fixed.
- **News in the Daily Brief (opt-in; off until you add a feed).** When you save RSS or Atom feed
  addresses under *News feeds and topics* in the Daily Brief, Collie requests exactly those
  addresses: over HTTPS only, and only when the host name resolves to a public internet address
  (each redirect, at most four, is checked the same way). It fetches a feed when you save it or
  press *Check now*, and otherwise in the background while Collie is running, after the brief has
  been opened, at most once per the interval you chose and never more often than every 15
  minutes. Each request is a plain `GET` with the User-Agent
  `Collie-DailyBrief/1.0 (+https://github.com/colliehq/collie)`, and carries no cookie, account,
  topic or information about you or your work; the feed's host sees your network address as with
  any web request. Nothing is fetched from the headlines themselves — no linked page, image or
  icon — until you open a link in your browser. Headlines are kept in
  `~/.collie/daily-brief/news.db`, shown as text, and never given to a model as instructions; if
  you turn on the morning email, they are part of it. Remove every feed to stop all requests.
- **Gmail and Google Calendar (opt-in; off until you connect Google).** When you run
  `collie google connect` or press *Connect* in Settings → Connections, Collie asks Google for three
  permissions: read your Gmail, write Gmail drafts, and read your Google Calendar. Your computer then
  talks to Google directly; no Collie server is involved. See
  [Google (Gmail and Calendar)](#google-gmail-and-calendar) below for what is read, where the
  connection is kept, and how to remove it.
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

### Google (Gmail and Calendar)

Collie connects to Google with its own OAuth app, and only after you sign in on Google's page. You
can untick any of the three permissions there; Collie records which ones Google granted and uses
only those.

- **What is read.** Recent Gmail messages for your morning report: sender, recipients, subject,
  date, Gmail's snippet and labels, and the text of the conversations the report covers (plain text,
  or HTML reduced to text; attachments are not opened). Upcoming Google Calendar events: title,
  start and end, location, the number of attendees, and the event's link. Reads go straight from
  your computer to Google (`gmail.googleapis.com`, `www.googleapis.com`).
- **What is written.** Only Gmail drafts, as replies inside the conversation they answer. **Collie
  never sends mail.** The connection has no call that sends a message or a draft; a draft waits in
  Gmail until you open it and press Send yourself.
- **Where the connection is stored.** Only on this computer. The sign-in (Google's refresh token) is
  sealed for your Windows account with DPAPI on Windows, kept in your login Keychain on macOS, and
  kept in a file only your user can read elsewhere. `~/.collie/google-connection.json` holds the
  account address, the permissions Google granted and the sealed sign-in, never the token in plain
  text. Short-lived access tokens are held in memory only.
- **When mail content leaves your computer.** Only when Collie builds your morning report, and
  only to the model provider you chose, which receives the messages and events the report needs.
  With a local model (Ollama) nothing leaves the machine. If you turn on the morning email, the
  report, which can include what Collie wrote about your mail and events, is delivered to you by
  email.
- **Disconnecting.** `collie google disconnect`, or *Disconnect* in Settings → Connections,
  revokes Collie's access at Google and deletes the connection from this computer, including the
  Keychain item on macOS. `collie uninstall` does the same before it removes anything else, and
  connecting a different Google account revokes the one it replaces. You can also
  remove Collie at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
  While Collie's Google app is in testing, Google ends every sign-in after 7 days, and Collie then
  asks you to connect again.

Collie's use and transfer to any other app of information received from Google APIs will adhere to
the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements. Google user data is used only to provide the morning report
and reply drafts you asked for. It is not sold, not used for advertising, not read by people, and not
used to develop, improve or train generalized AI or machine-learning models.

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
scheduled meeting can be deleted individually from Meeting Notes. Deleting the whole folder removes
Collie's local state, with one exception: a Google connection. Its access stays granted at Google,
and on macOS its sign-in is a Keychain item outside the folder. Run `collie google disconnect`
first, which revokes the access and deletes both. `collie uninstall` removes the folder and does this
for you. Uninstalling Collie removes the program.

## Changes

This policy applies to the open-source Collie project. Material changes will be noted in the
repository. Questions: [github.com/colliehq/collie/issues](https://github.com/colliehq/collie/issues).
