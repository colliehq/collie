# Quickstart

This assumes Collie is [installed](install.md). Every example runs locally.

## Zero-config

```bash
collie                 # terminal chat (TUI); the first run picks a provider
collie web             # browser GUI — chat, the live verification gate, diffs, the star-map
collie app             # the native desktop window (Windows)
```

## Your first verified fix

Point Collie at a real bug with a cheap model:

```bash
DEEPSEEK_API_KEY=... collie -p "fix the off-by-one in utils/timeparse.py"
```

Watch the [verification gate](index.md#it-proves-its-work-the-verification-gate): it writes a
reproduction that fails, makes the smallest edit, and re-runs it. The run ends **verified ✓**, not
"done."

## Fully local, no key

```bash
collie run "summarize app.py" --provider ollama --model qwen2.5-coder:7b
```

## Machine-readable output

```bash
collie run "fix the bug" --json          # final result object (tokens, cost, verified)
collie run "fix the bug" --stream-json   # live NDJSON: tool · edit · repro-gate · receipt
```

## Autonomy that ends on green

```bash
# iterate toward a goal; STOP the first turn a real executed check passes
collie loop --goal "get the suite passing" --until "pytest -q" --max 8
```

The loop terminates on a *real executed check* (`--until` exits 0), not on the model announcing it's
finished — the verification gate, one level up.

## Best-of-N by what actually passes

```bash
# run 3 isolated attempts, keep only what passes --check; apply nothing if none pass
collie pack "fix the failing test" -n 3 --check "pytest -q" --apply
```

## Warm a repo (optional)

```bash
collie init            # pre-download the memory model for this repo
collie init --rules    # additionally have the model write an AGENTS.md
```

`collie init` is optional — the first question pays the indexing cost otherwise. Collie reads
`AGENTS.md` / `CLAUDE.md` as project rules on every run.

## Where next

- Connect your preferred model → **[Providers](providers.md)**
- Every flag and subcommand → **[CLI reference](cli.md)**
- The native window, wallpaper, and browser bridge → **[The desktop app](desktop.md)**
