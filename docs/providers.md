# Providers

Collie is model-agnostic. Pick a provider in the first-run onboarding, the Settings panel, per run
with `--provider`, or by setting `COLLIE_PROVIDER`. An explicit environment variable always wins.

## Connect an existing subscription

| Provider | Value | How |
|---|---|---|
| Claude direct (experimental) | `anthropic-oauth` | Reads the official Claude login store and sends Collie's own Messages request; not a documented third-party Claude-plan interface, so availability and billing must be proved at runtime. |
| ChatGPT / Codex subscription | `codex-oauth` | One-click OAuth — uses your ChatGPT plan. |
| Claude CLI | `claude-cli` | Routes through your already-logged-in official Claude CLI; this includes Claude Code's harness/system context. |

A subscription login is not itself proof of zero marginal charge. Provider policy and account
settings can change; unattended `--no-paid-overage` runs use a fail-closed preflight and never
silently switch to an API key, paid credits, or another provider.

## API-key providers

Set the matching environment variable (or paste the key in onboarding):

| Provider | Value | Key |
|---|---|---|
| Anthropic API | `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Google Gemini | `gemini` | `GEMINI_API_KEY` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` |
| Qwen / DashScope | `qwen` | `DASHSCOPE_API_KEY` |
| OpenRouter (many models) | `openrouter` | `OPENROUTER_API_KEY` |
| Moonshot / Kimi | `moonshot` | `MOONSHOT_API_KEY` |
| Zhipu GLM | `zhipu` | `ZHIPU_API_KEY` |
| Groq | `groq` | `GROQ_API_KEY` |
| Any OpenAI-compatible endpoint | `openai-compat` | endpoint + key in Settings |

## Local & offline

| Provider | Value | Notes |
|---|---|---|
| Ollama | `ollama` | Local models — nothing leaves the machine. No key. |
| Mock | `mock` | Offline, canned responses. Testing only — never real work. |

```bash
# per-run
collie run "summarize app.py" --provider ollama --model qwen2.5-coder:7b
DEEPSEEK_API_KEY=... collie -p "fix the bug"            # provider inferred from the key

# persist a choice
collie config PROVIDER anthropic-oauth
COLLIE_PROVIDER=deepseek collie                          # env override wins for this session
```

!!! warning "About `anthropic-oauth`"
    This experimental mode is **opt-in**. It does not invoke or impersonate Claude Code: its body
    contains Collie's own system/tool contract and identifies the caller as Collie. It reuses a
    credential from Claude's official login store, but Anthropic has not documented arbitrary raw
    Messages calls as a supported Claude-plan route. A failed live probe is therefore an admission
    failure, not a reason to fall back. `claude-cli` is separate and does carry Claude Code's own
    harness context.

## Picking a model

Each provider has a sensible default model; override with `--model` or in Settings. The web GUI's
model picker lists what each connected provider exposes.
