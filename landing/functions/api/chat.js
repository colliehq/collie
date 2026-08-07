// POST /api/chat — "Ask Collie" on collie.run.
//
// A tiny, topic-locked assistant that answers questions about Collie, powered by Cloudflare
// Workers AI (no third-party key). Rate-limited to 20 questions per IP per day via a KV namespace,
// so a public endpoint can't be turned into a free LLM proxy.
//
// Bindings (see wrangler.toml): AI = Workers AI, RL = KV namespace.

const MODEL = "@cf/meta/llama-3.1-8b-instruct-fp8";
const DAILY_LIMIT = 20;

const SYSTEM = `You are "Ask Collie", the assistant on Collie's website (collie.run). Answer ONLY
questions about Collie — what it is, what it does, how to install and use it, and how it works.

What Collie is: a coding agent that LIVES ON THE USER'S OWN COMPUTER — local and private. Its wedge
is that it reaches the user's REAL environment (their logged-in browser via a Chrome extension, their
desktop, their screen, their files) and proves its work by RUNNING it, instead of being trapped in a
cloud tab or an editor pane.

Signature feature — the verification gate: when Collie fixes something it writes a reproduction that
must FAIL on the broken code, makes the smallest edit that flips it, then re-runs the assertion. A
run isn't "done" until an executed check passes ("verified"). This also scales up: 'collie loop'
stops when a shell check exits 0, and 'collie pack' keeps the best of N attempts by what actually
passes.

The range (all built by Collie's own coding agent — that breadth is the proof it's strong):
- coding agent (semantic code navigation, syntax-gated edits, self-verifying repair loop)
- control of your REAL logged-in browser (extension + local bridge — operates sites, not scrapes)
- an interactive ambient desktop / live wallpaper (clock, weather, app dock, music, command bar, and
  a live star-map of your code while it works)
- a screen recorder (screen + camera + mic; Windows and macOS)
- a phone remote (pair by scanning a code; run from your phone over Wi-Fi or a relay)
- surfaces: terminal, browser GUI, VS Code, and any ACP editor (Zed/JetBrains/neovim)

Install: Windows has a one-click Collie-Setup.exe; macOS/Linux install via pip ("pip install -e
'.[local]'"). Open source, MIT, no account, no telemetry. Models: model-agnostic — Claude, GPT/Codex,
Gemini, DeepSeek, Qwen, or a fully local Ollama model; 'mock' and 'ollama' need no API key.

Style: concise, friendly, concrete. 1–4 short sentences unless asked for detail. Never invent
features, versions, prices, or benchmark numbers. If asked something unrelated to Collie, briefly say
you can only help with questions about Collie and offer an example question. Point to the GitHub repo
(github.com/colliehq/collie) or the docs (colliehq.github.io/collie) for anything deeper.`;

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

export async function onRequestPost(context) {
  const { request, env } = context;

  // ---- per-IP daily rate limit (KV) ----
  const ip = request.headers.get("CF-Connecting-IP") || "anon";
  const day = new Date().toISOString().slice(0, 10); // YYYY-MM-DD (server clock; fine in a Worker)
  const key = `rl:${day}:${ip}`;
  let count = 0;
  if (env.RL) {
    try { count = parseInt((await env.RL.get(key)) || "0", 10) || 0; } catch (_) {}
    if (count >= DAILY_LIMIT) {
      return json({ error: `Daily limit reached (${DAILY_LIMIT} questions/day). Try again tomorrow.` }, 429);
    }
  }

  // ---- parse + validate ----
  let body;
  try { body = await request.json(); } catch (_) { return json({ error: "Bad request." }, 400); }
  const message = ((body && body.message) || "").toString().trim().slice(0, 1000);
  if (!message) return json({ error: "Please type a question." }, 400);

  // ---- ask Workers AI ----
  if (!env.AI) return json({ error: "The assistant is not configured yet." }, 503);
  let answer = "";
  try {
    const out = await env.AI.run(MODEL, {
      messages: [
        { role: "system", content: SYSTEM },
        { role: "user", content: message },
      ],
      max_tokens: 512,
      temperature: 0.4,
    });
    answer = (out && (out.response || out.result || out.text) || "").toString().trim();
  } catch (_) {
    return json({ error: "The assistant is busy right now — please retry in a moment." }, 502);
  }
  if (!answer) return json({ error: "No answer — please rephrase." }, 502);

  // ---- count this successful call (2-day TTL so the daily key self-cleans) ----
  if (env.RL) {
    try { await env.RL.put(key, String(count + 1), { expirationTtl: 172800 }); } catch (_) {}
  }
  // `reply` for the site's chat widget; `answer` kept as an alias for any other caller.
  return json({ reply: answer, answer });
}

// Anything other than POST
export async function onRequest(context) {
  if (context.request.method === "POST") return onRequestPost(context);
  return json({ error: "POST a JSON {message} to this endpoint." }, 405);
}
