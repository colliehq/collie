# Providers

Collie is model-agnostic. Pick a provider in the first-run onboarding, the Settings panel, per run
with `--provider`, or by setting `COLLIE_PROVIDER`. An explicit environment variable always wins.

## Connect an existing subscription (no per-token cost)

| Provider | Value | How |
|---|---|---|
| Claude subscription | `anthropic-oauth` | One-click OAuth in onboarding — uses your Claude plan. |
| ChatGPT / Codex subscription | `codex-oauth` | One-click OAuth — uses your ChatGPT plan. |
| Claude CLI | `claude-cli` | Routes through your already-logged-in Claude CLI. |

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
    OAuth subscription mode impersonates the Claude-Code client and is **opt-in** — you select it
    deliberately, per run or in Settings. It is never a silent default.

## Picking a model

Each provider has a sensible default model; override with `--model` or in Settings. The web GUI's
model picker lists what each connected provider exposes.
