# Collie

**Your personal AI operations system, running across your devices.** Give Collie an outcome. It
chooses the brain, tools, skills, workers, and device; keeps the Mission moving; asks when authority
is needed; and returns a receipt with scoped evidence.

<div class="grid cards" markdown>

- :material-rocket-launch: **[Install](install.md)** — one-click Windows installer, or `pip` from a release
- :material-play: **[Quickstart](quickstart.md)** — your first evidence-backed task in a minute
- :material-brain: **[Providers](providers.md)** — connect Claude, Codex, Gemini, a local model, or an API key
- :material-console: **[CLI reference](cli.md)** — every `collie` command
- :material-monitor: **[The desktop app](desktop.md)** — Home, Missions, Pack, Library, Activity, and Needs You

</div>

## Why it's different

**Mission-first, not chat-first.** Desktop, terminal, IDE, phone, and browser enter the same durable
Mission history. Work can survive waits, retries, restarts, and handoffs; decisions that need your
authority enter one global **Needs You** inbox.

**One Collie, a Pack underneath.** The user sees a stable Collie identity. Models, specialist workers,
skills, app connections, and devices remain replaceable execution resources behind it.

**Local reach with an explicit leash.** Collie can use your real logged-in browser, desktop, screen,
files, and code. Sensitive actions are bounded by permissions and budgets; task context goes only to
the model provider and services you connect.

## Evidence you can inspect: the verification gate

For code changes, Collie can record a failing reproduction, edit the implementation, and execute the
named assertion again. Required verification blocks completion until the selected post-edit check
passes. The result is evidence for that check's stated scope—not proof of every possible property.

```text
  repro    wrote repro.py · assert parse_duration("1h30m") == 5400
           ✗ FAILING  › got 1800, want 5400              ← gate armed

  edit     utils/timeparse.py  ································· +1 −1
           43 │- total = SECONDS[unit] * int(val)
           43 │+ total += SECONDS[unit] * int(val)

  verify   python repro.py
           ✓ PASSING  › parse_duration("1h30m") == 5400   ← gate green

  ✓ verified in 12.8s · Δ +1 −1 · 3,410 tok · $0.006
```

The receipt should say what ran, whether it passed, what it covers, and what remains unverified. A
green gate without that scope is only a confidence symbol; it is not an accountable completion rule.

## What makes it lean

- **Executed verification**, not self-reported success — the one change that measurably moved
  Collie's SWE-bench resolve rate.
- **Zero third-party dependencies in the core.** `mock` and `ollama` run with no key; memory works
  out of the box on BM25 keyword recall, and upgrades to real semantic recall when you install the
  optional local embedder.
- **Model-agnostic.** Claude, GPT/Codex, Gemini, DeepSeek, Qwen, a local Ollama model — same harness,
  so any delta is the harness, not the model.
- **Runs locally, no telemetry, no account.**

## Where to go next

New here and just want it running? → **[Install](install.md)**. Already installed? →
**[Quickstart](quickstart.md)**.
