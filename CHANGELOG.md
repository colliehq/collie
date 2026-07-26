# Changelog

## v0.18.0 — first public release

- **One-click Windows installer** (`Collie-Setup.exe`) — bundles a self-contained runtime
  (Python + Collie + semantic memory), the native desktop window, and the browser bridge.
  No Python, no terminal, no configuration.
- **Verification gate (`assert-verify`)** — Collie writes a reproduction that must fail on
  the broken code, makes the smallest edit that flips it, and re-runs the assertion before a
  task is called done.
- **Terminal-first, editor-anywhere** — `collie` (TUI), `collie web` (browser GUI with the
  live gate, diffs, and settings), and `collie acp` for Zed / JetBrains / neovim / VS Code
  over the Agent Client Protocol.
- **Local-first & model-agnostic** — bring your own subscription or API key (Anthropic,
  OpenAI-compatible presets, Ollama), or run fully local. No account, no telemetry.
- **Built in** — hybrid semantic memory, `code_search`, keyless web search, MCP support, a
  best-of-N `pack` mode, an autonomous `loop` that stops on a real green check, and a
  real-browser bridge that drives your logged-in Chrome/Edge.

MIT-licensed · runs locally · <https://collie.run>
