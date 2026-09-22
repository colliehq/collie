// POST /api/chat — the deliberately narrow "Ask Collie" website demo.
//
// Bindings (landing/wrangler.toml):
//   AI           Cloudflare Workers AI
//   RATE_LIMITER external Durable Object namespace from collie-chat-rate-limiter
//   RATE_LIMIT_SALT encrypted secret used to pseudonymize daily network-address buckets

// The Durable Object makes the daily limit atomic. If either binding is unavailable, this endpoint
// fails closed instead of quietly serving an unlimited public model proxy.

const MODEL = "@cf/meta/llama-3.1-8b-instruct-fp8";
const DAILY_LIMIT = 20;
const MAX_BODY_BYTES = 12 * 1024;
const MAX_MESSAGE_CHARS = 1000;
const MAX_HISTORY_MESSAGES = 6;
const MAX_HISTORY_CHARS = 4000;

const SYSTEM = `You are "Ask Collie", the assistant on Collie's website (collie.run). Answer ONLY
questions about Collie — what it is, what it does, how to install and use it, and how it works.

What Collie is: a personal AI operations system that runs across the user's devices. The user gives
one front-door Collie an outcome; Collie coordinates a Pack of models, specialist agents, skills,
app connections, and devices, keeps a durable Mission moving, asks when authority is needed, and
returns a Receipt with scoped evidence. Desktop is its Home/control plane; the supervisor and
durable stores are the runtime, so work can survive a closed window, waits, retries, and handoffs.

Collie's practical wedge is builders and small teams doing end-to-end work across code, a real
logged-in browser, desktop apps, files, and external services. Do not market "AI ecosystem" as a
feature by itself; explain the concrete outcome, authority, continuity, and evidence benefits.

Signature feature — verification the user controls. Auto asks for a relevant check after edits.
Verification · Required is the hard gate: after a code edit, Collie does not accept completion until
an executed assertion passes on the changed code. Do not claim that Auto has this hard-gate guarantee.
Never describe a green check as universal proof. State which named check ran, what it covers, and
what remains unverified.
This also scales up: 'collie loop' stops when a shell check exits 0, and 'collie pack' keeps the best
of N attempts by what actually passes.

The system:
- Home, Missions, Pack, Library, Activity, and a global Needs You approvals inbox
- coding execution (semantic code navigation, syntax-gated edits, evidence-backed repair loop)
- control of your REAL logged-in browser (extension + local bridge — operates sites, not scrapes)
- an optional Ambient desktop for summon, Ready/Running/Needs You status, and handoff
- a screen recorder (screen + camera + mic; Windows and macOS)
- phone supervision (pair by scanning a code; follow, approve, steer, stop, or start work)
- surfaces: terminal, browser GUI, VS Code, and any ACP editor (Zed/JetBrains/neovim)

Install: Windows has a one-click Collie-Setup.exe and macOS has a signed Apple-silicon DMG. Linux can
install from GitHub with pip. Open source, MIT, no account, no telemetry. Models: model-agnostic —
Claude, GPT/Codex, Gemini, DeepSeek, Qwen, or a fully local Ollama model; 'mock' and 'ollama' need no
API key.

Style: concise, friendly, concrete. 1–4 short sentences unless asked for detail. Never claim to have
performed work from this website, and never invent features, versions, prices, or benchmark numbers.
If asked something unrelated to Collie, briefly say you can only help with questions about Collie and
offer an example question. Point to the GitHub repo (github.com/colliehq/collie) or the docs
(colliehq.github.io/collie) for anything deeper.`;

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()",
      "x-frame-options": "DENY",
      "content-security-policy": "default-src 'none'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
    },
  });
}

function utf8Length(value) {
  return new TextEncoder().encode(value).byteLength;
}

function normalizeHistory(value) {
  if (!Array.isArray(value)) return [];
  const kept = [];
  let chars = 0;
  for (let i = value.length - 1; i >= 0 && kept.length < MAX_HISTORY_MESSAGES; i -= 1) {
    const item = value[i];
    if (!item || (item.role !== "user" && item.role !== "assistant") || typeof item.content !== "string") continue;
    const content = item.content.trim().slice(0, MAX_MESSAGE_CHARS);
    if (!content) continue;
    const remaining = MAX_HISTORY_CHARS - chars;
    if (remaining <= 0) break;
    const bounded = content.slice(Math.max(0, content.length - remaining));
    kept.unshift({ role: item.role, content: bounded });
    chars += bounded.length;
  }
  return kept;
}

async function parseRequest(request) {
  const type = (request.headers.get("content-type") || "").toLowerCase();
  if (!type.startsWith("application/json")) return { response: json({ error: "Content-Type must be application/json." }, 415) };

  const declared = Number(request.headers.get("content-length") || 0);
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) return { response: json({ error: "Request is too large." }, 413) };

  let raw;
  try { raw = await request.text(); } catch (_) { return { response: json({ error: "Could not read the request." }, 400) }; }
  if (utf8Length(raw) > MAX_BODY_BYTES) return { response: json({ error: "Request is too large." }, 413) };

  let body;
  try { body = JSON.parse(raw); } catch (_) { return { response: json({ error: "Malformed JSON." }, 400) }; }
  if (!body || typeof body !== "object" || Array.isArray(body) || typeof body.message !== "string") {
    return { response: json({ error: "Send a JSON object with a message string." }, 400) };
  }
  const message = body.message.trim();
  if (!message) return { response: json({ error: "Please type a question." }, 400) };
  if (message.length > MAX_MESSAGE_CHARS) return { response: json({ error: `Questions are limited to ${MAX_MESSAGE_CHARS} characters.` }, 413) };
  return { message, history: normalizeHistory(body.history) };
}

async function takeRateLimit(env, request) {
  if (!env.RATE_LIMITER || typeof env.RATE_LIMITER.idFromName !== "function") {
    return { error: json({ error: "The website demo is not configured safely yet." }, 503) };
  }
  const salt = typeof env.RATE_LIMIT_SALT === "string" ? env.RATE_LIMIT_SALT : "";
  const ip = (request.headers.get("CF-Connecting-IP") || "").trim();
  if (utf8Length(salt) < 32 || !ip || ip.length > 128) {
    return { error: json({ error: "The website demo is not configured safely yet." }, 503) };
  }
  const day = new Date().toISOString().slice(0, 10);
  try {
    // Never use a raw address as a globally visible Durable Object name. A secret-keyed daily HMAC
    // gives the limiter a stable bucket while preventing object ids from becoming an address index.
    const hmacKey = await crypto.subtle.importKey(
      "raw", new TextEncoder().encode(salt), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const signature = await crypto.subtle.sign(
      "HMAC", hmacKey, new TextEncoder().encode(`${day}\0${ip}`));
    const bucket = [...new Uint8Array(signature)]
      .map((value) => value.toString(16).padStart(2, "0")).join("");
    const id = env.RATE_LIMITER.idFromName(bucket);
    const stub = env.RATE_LIMITER.get(id);
    const result = await stub.fetch("https://rate-limit.internal/take", {
      method: "POST",
      headers: { "x-collie-limit": String(DAILY_LIMIT) },
    });
    if (result.status === 429) {
      return { error: json({ error: `Daily limit reached (${DAILY_LIMIT} questions/day). Try again tomorrow.` }, 429) };
    }
    if (!result.ok) throw new Error(`rate limiter returned ${result.status}`);
    return {};
  } catch (_) {
    return { error: json({ error: "The website demo is temporarily unavailable." }, 503) };
  }
}

export async function onRequestPost(context) {
  const { request, env } = context;
  const parsed = await parseRequest(request);
  if (parsed.response) return parsed.response;

  if (!env.AI || typeof env.AI.run !== "function") {
    return json({ error: "The website demo is not configured yet." }, 503);
  }
  const limited = await takeRateLimit(env, request);
  if (limited.error) return limited.error;

  let answer = "";
  try {
    const out = await env.AI.run(MODEL, {
      messages: [
        { role: "system", content: SYSTEM },
        ...parsed.history,
        { role: "user", content: parsed.message },
      ],
      max_tokens: 512,
      temperature: 0.4,
    });
    answer = String((out && (out.response || out.result || out.text)) || "").trim();
  } catch (_) {
    return json({ error: "The website demo is busy right now — please retry in a moment." }, 502);
  }
  if (!answer) return json({ error: "The website demo returned no answer — please rephrase." }, 502);
  return json({ reply: answer });
}

export async function onRequest(context) {
  if (context.request.method === "POST") return onRequestPost(context);
  return json({ error: "POST a JSON {message, history?} object to this endpoint." }, 405);
}
