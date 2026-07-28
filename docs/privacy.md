# Privacy policy

Collie is a local, open-source developer tool. It runs on your own computer, under your control, and
is built to keep your data with you.

## What Collie collects

**Nothing.** Collie has **no account, no sign-up, no analytics, no telemetry, and no crash
reporting.** It does not phone home, does not track usage, and does not send your files, prompts, or
code anywhere except where *you* explicitly direct it (below). There is no Collie-operated server that
receives your data.

## Where your data goes when you use a feature

Collie only sends data off your machine for features you turn on, and only to the destination that
feature inherently requires:

- **Your chosen model provider.** When you run the agent, your prompts and the code/context it needs
  are sent to the model provider *you* configured (e.g. your own Anthropic/OpenAI/DeepSeek API key,
  your Claude subscription, or a fully **local** model via Ollama — in which case nothing leaves the
  machine at all). This is the same data flow as any AI coding tool, to a provider you pick.
- **Web search / fetch (opt-in).** If you enable it, Collie fetches public web pages you or the task
  reference (a keyless DuckDuckGo/SearXNG query, or pages via your own browser). No account.
- **Phone remote (opt-in).** If you enable `collie web --remote`, your phone reaches your desktop
  through the collie.run relay. The relay is **end-to-end encrypted and zero-knowledge**: it only
  routes ciphertext between your paired devices and cannot read your commands or data. Pairing
  requires a code shown on your own screen plus your approval on the desktop.
- **"Ask Collie" chat on collie.run (optional).** The chat box on the website sends only the question
  you type to a Cloudflare Workers-AI model to answer questions about Collie. It is unrelated to the
  app on your machine.

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
