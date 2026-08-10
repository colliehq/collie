# Privacy policy

Collie is a local, open-source developer tool. It runs on your own computer, under your control, and
is built to keep your data with you.

## App telemetry

The Collie app has **no Collie account, sign-up, advertising, usage analytics, telemetry, or crash
reporting.** It does not send usage events home. Data leaves your machine only when a feature you
choose inherently needs a network destination, as described below. The collie.run website and the
optional hosted phone relay are separate services with limited data flows described here.

## Where your data goes when you use a feature

Collie only sends data off your machine for features you turn on, and only to the destination that
feature inherently requires:

- **Your chosen model provider.** When you run the agent, your prompts and the code/context it needs
  are sent to the model provider *you* configured (e.g. your own Anthropic/OpenAI/DeepSeek API key,
  your Claude subscription, or a fully **local** model via Ollama — in which case nothing leaves the
  machine at all). This is the same data flow as any AI coding tool, to a provider you pick.
- **Web search / fetch (opt-in).** If you enable it, Collie fetches public web pages you or the task
  reference (a keyless DuckDuckGo/SearXNG query, or pages via your own browser). No account.
- **Phone remote (opt-in).** If you enable `collie web --remote`, your phone can reach your desktop
  through the collie.run relay. Hosted remote request and response contents are **end-to-end
  encrypted**; the relay handles necessary routing metadata such as room or device identifiers,
  request timing, and approximate message sizes. Pairing requires a code shown on your own screen
  plus your approval on the desktop.
- **"Ask Collie" chat on collie.run (optional).** Nothing is sent until you submit the website form.
  Your question and up to six recent messages from that demo go to Cloudflare Workers AI. For abuse
  prevention, the service processes your network address through a secret-keyed one-way function to
  derive a new identifier each day. The raw address is not used as a Durable Object name or stored by
  the site code; the object stores only the counter and expiry, which are deleted within 48 hours.
  Cloudflare still processes ordinary request data to deliver and secure the service. This is
  unrelated to the app and does not attach your product files or local sessions. The static site
  loads no analytics or tracking beacon.

Local features — driving your logged-in browser, arranging your desktop, controlling other apps,
recording your screen — run **entirely on your own computer**. Their output stays local unless you
send it somewhere yourself.

## Your machine, your control

Every capability that touches your real environment is **opt-in and user-initiated**: the browser
bridge requires you to install and enable an extension; remote access requires you to turn it on and
pair a device; screen recording only runs when you start it. Collie automates your *own* computer at
your request — the way tools like Playwright, AutoHotkey, or an RPA runner do — and never acts on
anyone else's system.

## Data you can delete

Collie's local state (settings, memory, sessions, paired-device list) lives under `~/.collie` on your
machine; delete that folder to remove it. Uninstalling Collie removes the program.

## Changes

This policy applies to the open-source Collie project. Material changes will be noted in the
repository. Questions: [github.com/colliehq/collie/issues](https://github.com/colliehq/collie/issues).
