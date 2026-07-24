# Collie

**A lean coding-agent harness that proves its work.** Collie writes a reproduction, runs it, and
finishes only when an executed assertion passes — at roughly a sixth the tokens of comparable
agents. Terminal-first, model-agnostic, and honestly measured.

<div class="grid cards" markdown>

- :material-rocket-launch: **[Install](install.md)** — one-click Windows installer, or `pip` from a release
- :material-play: **[Quickstart](quickstart.md)** — your first verified fix in a minute
- :material-brain: **[Providers](providers.md)** — connect Claude, Codex, Gemini, a local model, or an API key
- :material-console: **[CLI reference](cli.md)** — every `collie` command
- :material-monitor: **[The desktop app](desktop.md)** — native window, live star-map, real-browser bridge

</div>

## The signature: the verification gate

When Collie fixes something it writes a reproduction that **must fail on the broken code**, makes the
smallest edit that flips it, and re-runs the assertion. A run isn't "done" — it's **verified ✓**.

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

Other agents check "did the test not error." Collie's gate is stronger: the reproduction carries an
`assert actual == expected` derived from the issue, so a plausible-but-wrong edit fails *loudly* and
drives another repair round.

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
