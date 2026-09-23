/**
 * collie mail — the relay half.
 *
 * One zone, one MX, one catch-all: every user's dogs are served by this single Worker, and adding
 * a dog is a KV row rather than a DNS change. That is what makes an address per dog affordable at
 * more than one user.
 *
 * What this Worker deliberately cannot do: read the mail it carries. A message is sealed to the
 * receiving dog's X25519 public key the moment it arrives and only the ciphertext is stored, so
 * the operator holds bytes it has no key for. That is not a policy, it is the data model — which
 * matters for a product whose whole claim is that it runs on your own computer.
 *
 * Be precise about the edge: SMTP is a cleartext protocol, so the message exists in plaintext in
 * this Worker's memory for the moment between delivery and sealing. Never stored in the clear is
 * the promise; end-to-end is not, and saying so would be a lie.
 *
 * Crypto mirrors harness/dogmail.py exactly — X25519 · HKDF-SHA256 · AES-256-GCM · HMAC-SHA256,
 * with 4-byte big-endian length prefixes on every field that goes into a MAC or an AAD so no
 * field's contents can be mistaken for the next one's.
 *
 * Bindings expected in wrangler.toml:
 *   KV  MAIL          — sealed messages, TTL'd
 *   KV  DIRECTORY     — handles and dogs; the readable mirror, not the decision point
 *   DO  MAIL_DELIVERY — SQLite-backed send ledger, one object per mailbox (see MailDelivery below)
 *   DO  CLAIMS        — SQLite-backed identity ledger, one object per name (see DirectoryClaims)
 *   send_email MAILER — outbound transport, owner notifications only
 *   secret RELAY_PRIVATE_B64 — the relay's X25519 private key (its public half is served at /pubkey)
 */

// `cloudflare:email` is imported WHERE IT IS USED, not at the top. A static import of a
// Workers-only module makes this file unloadable by node — which silently breaks the very tests
// that prove this Worker and the Python client agree on the wire format. Caught by running them.
const VERSION = "collie-mail/2";
const SKEW = 120;                     // seconds a request stamp may be off
const TTL = 60 * 60 * 24 * 7;         // a week: long enough to be away, short enough not to hoard

/**
 * The practical ceiling on one incoming message. Not 8 MiB, not 25 MiB, and the reason is the
 * inflation this Worker applies before anything is stored: raw → base64 (×4/3) inside a JSON
 * payload → AES-GCM ciphertext → base64 again. 4 MiB of RFC822 lands in KV at roughly 7.2 MiB —
 * inside KV's 25 MiB value limit with room for the envelope, and with a transient peak near 30 MiB
 * against the Worker's 128 MiB, which a couple of concurrent deliveries still fit inside.
 *
 * Anything larger is REJECTED at SMTP, not truncated. A half-message stored as if it were whole is
 * the worse failure: the sender is told it arrived and the dog reads a body that stops mid-sentence.
 */
const MAX_MAIL_BYTES = 4 * 1024 * 1024;

const MAX_JSON = 16 * 1024;           // claim/verify bodies are tiny; a stream is not an invitation
const MAX_SEND_BODY = 96 * 1024;      // the outbound request envelope, digest-checked
const MAX_TEXT = 64 * 1024;           // the reply body itself, plain text
const MAX_SUBJECT = 512;              // bytes, after UTF-8 encoding
const MAX_HEADER = 2048;              // Cloudflare's per-custom-header limit, in UTF-8 bytes
const PAGE_ITEMS = 50;                // messages per /mail-page
const PAGE_BYTES = 4 * 1024 * 1024;   // and the response's sealed-byte ceiling, whichever hits first
const KV_PAGE = 200;                  // keys per KV list call — bounds memory, not the result set
const LEGACY_PAGES = 20;              // how far the un-paginated /mail will walk before saying "more"

const enc = new TextEncoder();

/**
 * base64 in fixed chunks. `btoa(String.fromCharCode(...bytes))` spreads every byte into an argument
 * list and throws RangeError once a message is large enough to matter — which is exactly the mail
 * worth carrying. 0x8000 bytes per apply() stays far under any engine's argument limit.
 */
const b64 = (buf) => {
  const bytes = new Uint8Array(buf);
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000)
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(out);
};
const ub64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

// A cursor travels in a query string, where `+` decodes to a space. base64url or it arrives broken.
const b64url = (bytes) => b64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const ub64url = (s) =>
  ub64(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "="));

const hex = (buf) =>
  [...new Uint8Array(buf)].map((x) => x.toString(16).padStart(2, "0")).join("");

const sha256hex = async (bytes) => hex(await crypto.subtle.digest("SHA-256", bytes));

/**
 * Read a stream with a hard ceiling, and say WHICH way it ended.
 *
 * The old code read the whole body and then sliced — the read itself was unbounded, so the limit
 * protected the store and not the Worker. Here the limit is enforced as the bytes arrive, and
 * `over` is returned rather than thrown so each caller can choose its own refusal (an SMTP reject,
 * a 413) instead of guessing from an exception.
 */
async function readBounded(stream, limit) {
  if (!stream) return { bytes: new Uint8Array(0), over: false };
  const reader = stream.getReader();
  const parts = [];
  let total = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const chunk = new Uint8Array(value);
      total += chunk.length;
      if (total > limit) return { bytes: new Uint8Array(0), over: true };
      parts.push(chunk);
    }
  } finally {
    try { reader.releaseLock(); } catch { /* already released by cancel */ }
  }
  return { bytes: cat(...parts), over: false };
}

/** 4-byte big-endian length prefix — the same `lp()` the Python side uses. */
function lp(value) {
  const raw = typeof value === "string" ? enc.encode(value) : new Uint8Array(value);
  const out = new Uint8Array(4 + raw.length);
  new DataView(out.buffer).setUint32(0, raw.length);
  out.set(raw, 4);
  return out;
}

function cat(...parts) {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let i = 0;
  for (const p of parts) { out.set(p, i); i += p.length; }
  return out;
}

// WebCrypto imports X25519 PUBLIC keys as "raw" and refuses private ones — a private key has to
// arrive as PKCS8. The wrapper is fixed except for the 32 key bytes: SEQUENCE, version 0,
// AlgorithmIdentifier{ OID 1.3.101.110 }, OCTET STRING(OCTET STRING(key)). Written out rather than
// pulled from a library because this Worker has no dependencies, and found by the cross-check
// against Python rather than by reading — raw import fails at run time, not at review.
const PKCS8_X25519 = new Uint8Array([
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e, 0x04, 0x22, 0x04, 0x20,
]);

async function importPrivate(privRaw) {
  return crypto.subtle.importKey("pkcs8", cat(PKCS8_X25519, privRaw), "X25519", false,
                                 ["deriveBits"]);
}

async function x25519(privRaw, pubRaw) {
  const priv = await importPrivate(privRaw);
  const pub = await crypto.subtle.importKey("raw", pubRaw, "X25519", false, []);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "X25519", public: pub }, priv, 256));
}

async function hkdf(ikm, salt, info, length = 32) {
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt, info }, key, length * 8);
  return new Uint8Array(bits);
}

async function hmac(keyRaw, message) {
  const key = await crypto.subtle.importKey(
    "raw", keyRaw, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, message));
}

/** Constant-time compare — a length-only check here would leak the MAC a byte at a time. */
function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

/** Same idea for two strings of the same expected shape — digests, hex, message ids. */
function sameString(a, b) {
  return sameBytes(enc.encode(String(a)), enc.encode(String(b)));
}

// ── what a value has to look like before it is allowed anywhere near a header or a key ──────────
//
// Every one of these exists because the alternative is a CRLF in a string that later becomes an
// RFC-5322 header, or an unbounded attacker-chosen KV key. Neither is theoretical.

const ADDRESS_RE = /^[a-z0-9][a-z0-9._%+-]{0,62}@[a-z0-9][a-z0-9.-]{0,62}\.[a-z]{2,24}$/;
const B64_RE = /^[A-Za-z0-9+/]+={0,2}$/;
const CURSOR_RE = /^[A-Za-z0-9+/=_-]{1,2048}$/;
const NONCE_RE = /^[A-Za-z0-9+/=_-]{8,128}$/;
const REQUEST_ID_RE = /^[A-Za-z0-9._:-]{8,128}$/;

const utf8len = (s) => enc.encode(s).length;

/** An address that cannot smuggle a header break, a comma-separated second recipient, or a name. */
function validEmail(value) {
  const s = String(value == null ? "" : value);
  return s.length <= 254 && ADDRESS_RE.test(s.toLowerCase()) && s === s.trim();
}

/** 32 bytes of X25519, or nothing. A short key silently changes what the MAC covers. */
function validPub(value) {
  const s = String(value == null ? "" : value);
  if (s.length !== 44 || !B64_RE.test(s)) return false;
  try { return ub64(s).length === 32; } catch { return false; }
}

/** Printable ASCII on one line — the only thing safe to put in a header this Worker composes. */
function headerSafe(value, limit = MAX_HEADER) {
  const s = String(value == null ? "" : value);
  return s.length > 0 && utf8len(s) <= limit && !/[\r\n\u0000]/.test(s) &&
         /^[\x20-\x7e]*$/.test(s);
}

/**
 * A six-digit code from the CSPRNG, not from Math.random.
 *
 * Math.random is not seeded to resist guessing, and this code is the ONLY thing standing between a
 * stranger and a handle bound to somebody else's address. Rejection-sampled so all 900000 values
 * are equally likely — a bare modulo would make the low codes marginally more common, which is a
 * small bias in a small space.
 */
function verificationCode() {
  const a = new Uint32Array(1);
  do { crypto.getRandomValues(a); } while (a[0] >= 4293900000);   // 4771 × 900000
  return String(100000 + (a[0] % 900000));
}

/** Bounded JSON: a claim body is a few hundred bytes, and a stream is not a promise of that. */
async function readJson(request, limit = MAX_JSON) {
  const { bytes, over } = await readBounded(request.body, limit);
  if (over) return { error: json({ ok: false, error: "request body too large" }, 413) };
  try {
    const value = JSON.parse(new TextDecoder().decode(bytes) || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value))
      return { error: json({ ok: false, error: "a JSON object is required" }, 400) };
    return { value };
  } catch {
    return { error: json({ ok: false, error: "malformed JSON" }, 400) };
  }
}

/**
 * The message's stable id.
 *
 * New rows carry it in the value; rows written before this existed do not, so it is derived from
 * the KV key — which is where it came from in the first place. Both readings agree by construction,
 * so a client can page with durable ids instead of a timestamp high-water mark, which KV's eventual
 * consistency makes unsafe: a message that lands a second late is behind the mark forever.
 */
function messageId(key, value) {
  if (value && typeof value.id === "string" && value.id) return value.id;
  const parts = String(key).split(":");
  return parts.length > 3 ? parts.slice(3).join(":") : String(key);
}

function relayPrivate(env) {
  return ub64(env.RELAY_PRIVATE_B64);
}

async function relayPublic(env) {
  // X25519 public keys are not derivable from the private half through WebCrypto's raw import, so
  // the public half is stored beside it rather than recomputed.
  return ub64(env.RELAY_PUBLIC_B64);
}

/** The dog-facing envelope: ephemeral-static, one throwaway keypair per message. */
async function sealToDog(dogPub, plaintext) {
  const pair = await crypto.subtle.generateKey({ name: "X25519" }, true, ["deriveBits"]);
  const ephPub = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  const shared = new Uint8Array(await crypto.subtle.deriveBits(
    { name: "X25519", public: await crypto.subtle.importKey("raw", dogPub, "X25519", false, []) },
    pair.privateKey, 256));
  const key = await crypto.subtle.importKey(
    "raw", await hkdf(shared, new Uint8Array(0), enc.encode("collie-mail-seal")),
    "AES-GCM", false, ["encrypt"]);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce, additionalData: lp(ephPub) }, key, plaintext);
  return { epk: b64(ephPub), n: b64(nonce), ct: b64(ct) };
}

async function authKey(env, dogPub, address) {
  const shared = await x25519(relayPrivate(env), dogPub);
  return hkdf(shared, enc.encode(address), enc.encode("collie-mail-auth"));
}

async function certKey(env, handlePub) {
  const shared = await x25519(relayPrivate(env), handlePub);
  return hkdf(shared, enc.encode("handle"), enc.encode("collie-mail-cert"));
}

/**
 * Is this name one we will not put on the domain?
 *
 * The list is DATA in KV, not code: it changes without a deploy, and a repository does not need a
 * slur list in its history. Both a handle and a dog name are checked, because both end up in the
 * address — filtering only handles would leave half the surface open.
 *
 * Substring matching, with an explicit set of words that legitimately contain a blocked one — the
 * Scunthorpe problem is not hypothetical, and refusing "assistant" or "analysis" is its own kind of
 * broken. Nothing here is complete: leetspeak, other languages and things nobody thought of get
 * through. **Revocation is the real backstop**, not the filter — which is why an address can be
 * withdrawn after the fact.
 */
const LEET = { "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g",
               "@": "a", "$": "s", "!": "i", "|": "i" };

/**
 * Two readings of a name: as typed, and as it would be heard.
 *
 * `n1gger` and `f4ck` walked straight through a plain substring check, which made the list mostly
 * decorative — anyone deliberate just types a digit. Folding lookalikes and collapsing repeated
 * letters catches that whole family (`fuuuck`, `sh1t`, `a$$hole`) for a few lines.
 *
 * The fold is also what rescues some false positives rather than causing them: "assistant" folds to
 * "asistant", which no longer contains "ass". It creates others in the opposite direction —
 * "shiitake" folds to "shitake" — which is what the allow list is for. Both forms are checked, so a
 * name is refused if EITHER reading hits, and allowed if either reading is explicitly permitted.
 */
function foldName(name) {
  const lowered = String(name).toLowerCase().replace(/[^a-z0-9@$!|]/g, "");
  const mapped = lowered.replace(/[0134578 9@$!|]/g, (c) => LEET[c] || c);
  return mapped.replace(/(.)\1+/g, "$1");         // fuuuck -> fuck
}

async function blockedName(env, name) {
  const list = await env.DIRECTORY.get("config:blocked", "json");
  if (!list) return "";
  const { words = [], allow = [] } = list;
  const flat = String(name).toLowerCase().replace(/[^a-z0-9]/g, "");
  const folded = foldName(name);
  if (allow.some((a) => flat === a || folded === foldName(a))) return "";
  const hit = words.find((w) => flat.includes(w) || folded.includes(foldName(w)));
  return hit ? "that name is not available" : "";
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

/** Verify the stamp on a read. Refuses a stale one, a replayed one, and one made for another path. */
async function stamped(request, env, url) {
  const address = request.headers.get("x-collie-addr") || "";
  const ts = request.headers.get("x-collie-ts") || "";
  const nonce = request.headers.get("x-collie-nonce") || "";
  const mac = request.headers.get("x-collie-mac") || "";
  if (!validEmail(address))
    return { error: json({ ok: false, error: "unknown address" }, 404) };
  const row = await env.DIRECTORY.get("dog:" + address, "json");
  if (!row || !validPub(row.pub)) return { error: json({ ok: false, error: "unknown address" }, 404) };
  // parseInt("garbage") is NaN, and `Math.abs(NaN) > SKEW` is FALSE — so a stamp with a nonsense
  // timestamp used to sail past the freshness check entirely and be judged on its MAC alone. The
  // number has to be a real, finite, whole second before it is compared to anything.
  const seconds = Number(ts);
  if (!Number.isSafeInteger(seconds) ||
      Math.abs(Math.floor(Date.now() / 1000) - seconds) > SKEW)
    return { error: json({ ok: false, error: "stale stamp" }, 401) };
  if (!NONCE_RE.test(nonce))
    return { error: json({ ok: false, error: "bad nonce" }, 401) };
  // The nonce is remembered for twice the allowed skew: long enough that no accepted stamp can be
  // replayed, short enough that the store does not grow without bound.
  if (await env.DIRECTORY.get("nonce:" + nonce))
    return { error: json({ ok: false, error: "replay" }, 401) };
  const want = await hmac(await authKey(env, ub64(row.pub), address),
                          cat(lp(request.method), lp(url.pathname + url.search), lp(ts), lp(nonce)));
  let given;
  try { given = ub64(mac || ""); } catch { given = new Uint8Array(0); }
  if (!sameBytes(want, given))
    return { error: json({ ok: false, error: "bad stamp" }, 401) };
  await env.DIRECTORY.put("nonce:" + nonce, "1", { expirationTtl: SKEW * 2 });
  return { address, row };
}

// ════════════════════════════════════════════════════════════════════════════════════════════════
// Sending: the dog answers its owner, at most one provider attempt per retained request id
// ════════════════════════════════════════════════════════════════════════════════════════════════
//
// This is deliberately not a mail relay. There is one permitted destination per mailbox — the email
// address that verified the handle — and the From is always the authenticated dog. No recipient
// list, no CC, no BCC, no caller-chosen From. A dog that can email anyone is a spam cannon with a
// key, and the feature people actually want ("tell me what happened") needs none of that.
//
// The hard part is not duplicating. KV cannot do it: two concurrent requests both read "no receipt"
// and both send, and the user gets the message twice. So the ledger is a SQLite Durable Object, one
// per mailbox, where a read-then-write is genuinely atomic.
//
// What that buys is exactly this and no more: while a receipt is retained (30 days), at most ONE
// call to the mail binding is made for that request id. It is not exactly-once DELIVERY — an id
// replayed after the receipt is pruned reserves again, "sent" means the provider accepted the
// submission rather than that anyone received it, and an unknown outcome stays unknown. The shape is: reserve (durably record
// `sending`) → send outside any transaction → record the outcome. If the Worker dies between the
// reservation and the outcome the row stays `sending` and is reported as `unknown`, which is the
// truth, and nothing reissues it automatically. Silently re-sending an ambiguous message is how one
// alert becomes forty.

const RECEIPT_TTL = 60 * 60 * 24 * 30;  // dedupe outlives any plausible client retry, by a month
const MAX_RECEIPTS = 2000;              // per mailbox — bounded, but never at dedupe's expense
const PER_MINUTE = 5;
const PER_HOUR = 30;

/** Provider errors that are a REFUSAL — the message was not accepted and will not be. */
const REFUSED = /E_VALIDATION_ERROR|E_SENDER_NOT_VERIFIED|E_RECIPIENT_NOT_ALLOWED|E_SENDER_DOMAIN_NOT_AVAILABLE|E_RATE_LIMIT|rate.?limit/i;

/**
 * Reduce a provider error to a code, and nothing else.
 *
 * The message body may quote the mail, the account, or a credential. Only a matched `E_*` token or
 * the word "unclassified" is ever stored or returned — the rest is dropped on the floor here rather
 * than trusted not to be sensitive later.
 */
function errorCode(e) {
  const m = String((e && e.message) || "").match(/E_[A-Z_]+/);
  if (m) return m[0];
  return REFUSED.test(String((e && e.message) || "")) ? "E_RATE_LIMIT" : "unclassified";
}

/**
 * The legacy `EmailMessage` constructor.
 *
 * Imported lazily — a static import of `cloudflare:email` makes this whole file unloadable outside
 * the Workers runtime — and overridable through `env.EMAIL_MESSAGE`, which is the only way the node
 * tests can exercise the legacy path at all: there is no `cloudflare:email` there to import.
 */
async function emailMessageClass(env) {
  if (env.EMAIL_MESSAGE) return env.EMAIL_MESSAGE;
  return (await import("cloudflare:email")).EmailMessage;
}

const sendMode = (env) =>
  String(env.MAIL_SEND_MODE || "structured").toLowerCase() === "legacy" ? "legacy" : "structured";

function ledgerStub(env, address) {
  const ns = env.MAIL_DELIVERY;
  return ns.get(ns.idFromName("mailbox:" + address));
}

async function ledger(env, address, op, payload) {
  const r = await ledgerStub(env, address).fetch("https://mail-delivery/" + op, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json();
}

/** One receipt, rendered the same way whether it was just made or looked up later. */
function receiptJson(receipt, duplicate) {
  const base = { ok: true, id: receipt.id, status: receipt.status, duplicate,
                 receipt: receipt.receipt || null, at: receipt.created, updated: receipt.updated };
  if (receipt.status === "sent") return json(base);
  if (receipt.status === "failed")
    return json({ ...base, ok: false, error: receipt.receipt || "refused by the provider" }, 502);
  // `sending` and `unknown` are the same answer to a caller: we do not know, and we will not guess.
  return json({ ...base, ok: false, status: "unknown",
                error: "the provider's answer is unknown; this will not be resent automatically. " +
                       "A retry needs a NEW request id and accepts the risk of a duplicate." }, 202);
}

/** Non-ASCII subjects need an encoded-word; anything else would put raw UTF-8 in a header. */
function encodeHeaderWord(value) {
  return /^[\x20-\x7e]*$/.test(value) ? value : `=?UTF-8?B?${b64(enc.encode(value))}?=`;
}

/**
 * Hand the message to Cloudflare, once.
 *
 * Two shapes are supported because two exist. The structured builder is the current API and returns
 * `{messageId}`; it also OWNS Message-ID, Date and the MIME headers and rejects a caller-supplied
 * Message-ID, so none is passed. The legacy `EmailMessage` carries a raw RFC-5322 message, may
 * resolve `undefined`, and is selected by `MAIL_SEND_MODE=legacy` for accounts still on it — there
 * is no automatic fallback between them, because "try the other one" after an ambiguous failure is
 * how a single message gets delivered twice.
 */
async function deliver(env, from, to, msg, stableId) {
  const headers = { "Auto-Submitted": "auto-replied", "X-Auto-Response-Suppress": "All" };
  if (msg.in_reply_to) headers["In-Reply-To"] = msg.in_reply_to;
  if (msg.references) headers["References"] = msg.references;

  if (sendMode(env) === "legacy") {
    const domain = env.MAIL_DOMAIN || "collie.run";
    const messageId = `<${stableId}@${domain}>`;
    const raw =
      `From: ${from}\r\nTo: ${to}\r\n` +
      `Subject: ${encodeHeaderWord(msg.subject)}\r\n` +
      `Message-ID: ${messageId}\r\nDate: ${new Date().toUTCString()}\r\n` +
      Object.entries(headers).map(([k, v]) => `${k}: ${v}\r\n`).join("") +
      `MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n` +
      msg.text.replace(/\r?\n/g, "\r\n");
    const EmailMessage = await emailMessageClass(env);
    const out = await env.MAILER.send(new EmailMessage(from, to, raw));
    // Our own Message-ID is a submission receipt only NOW, on the far side of the await. Recorded
    // before the promise resolves it would be a receipt for something that may not have happened.
    return (out && out.messageId) || messageId;
  }
  const out = await env.MAILER.send({ from, to, subject: msg.subject, text: msg.text, headers });
  return (out && out.messageId) || "";
}

/** Everything the body has to be before a single byte of it reaches the provider. */
function validateSend(body, owner) {
  const id = String(body.id || "");
  if (!REQUEST_ID_RE.test(id))
    return { error: "id must be 8-128 chars of A-Z a-z 0-9 . _ : -" };
  for (const banned of ["cc", "bcc", "from", "reply_to", "headers", "html"])
    if (body[banned] !== undefined)
      return { error: `"${banned}" is not accepted: this endpoint notifies the owner, `
                      + "it is not a mail relay" };
  const to = String(body.to || "").toLowerCase();
  if (!validEmail(to) || to !== owner.toLowerCase())
    return { error: "the only destination is the verified owner of this handle" };
  const subject = String(body.subject == null ? "" : body.subject);
  if (!subject.trim() || utf8len(subject) > MAX_SUBJECT || /[\r\n\u0000]/.test(subject))
    return { error: `subject must be 1-${MAX_SUBJECT} bytes on a single line` };
  const text = String(body.text == null ? "" : body.text);
  if (!text || utf8len(text) > MAX_TEXT)
    return { error: `text must be 1-${MAX_TEXT} bytes of plain text` };
  const msg = { id, to, subject, text };
  for (const field of ["in_reply_to", "references"]) {
    if (body[field] === undefined || body[field] === null || body[field] === "") continue;
    const value = String(body[field]);
    if (!headerSafe(value))
      return { error: `${field} must be printable ASCII on one line, at most ${MAX_HEADER} bytes` };
    if (!/^(<[^<>\s]{1,900}>\s*)+$/.test(value.trim()))
      return { error: `${field} must be one or more <message-id> tokens` };
    msg[field] = value.trim();
  }
  return { msg };
}

async function send(request, env, url) {
  if (!env.MAILER) return json({ ok: false, error: "sending is not configured" }, 501);
  if (!env.MAIL_DELIVERY)
    return json({ ok: false, error: "the delivery ledger is not configured" }, 501);

  const { bytes, over } = await readBounded(request.body, MAX_SEND_BODY);
  if (over) return json({ ok: false, error: "request body too large" }, 413);

  // Auth first: the stamp covers pathname + search, and search carries the digest — so checking the
  // stamp is what makes the digest itself trustworthy rather than merely present.
  const check = await stamped(request, env, url);
  if (check.error) return check.error;

  const want = (url.searchParams.get("sha256") || "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(want))
    return json({ ok: false, error: "a ?sha256= of the exact request body is required" }, 400);
  if (!sameString(await sha256hex(bytes), want))
    return json({ ok: false, error: "the body does not match the signed digest" }, 400);

  let body;
  try { body = JSON.parse(new TextDecoder().decode(bytes)); } catch { body = null; }
  if (!body || typeof body !== "object" || Array.isArray(body))
    return json({ ok: false, error: "a JSON object is required" }, 400);

  const handle = await env.DIRECTORY.get("handle:" + check.row.handle, "json");
  if (!handle || !handle.verified || !validEmail(handle.email))
    return json({ ok: false, error: "this handle has no verified owner to write to" }, 403);

  const { msg, error } = validateSend(body, handle.email);
  if (error) return json({ ok: false, error }, 400);

  // The digest is the identity of the payload. Same id with a different digest is a programming
  // error on the client's side, and answering it with the first message's receipt would quietly
  // drop the second one.
  const claim = await ledger(env, check.address, "reserve",
                             { id: msg.id, digest: want, at: Math.floor(Date.now() / 1000) });
  if (claim.state === "conflict")
    return json({ ok: false, id: msg.id, error: "that request id was already used with a "
                                                + "different body" }, 409);
  if (claim.state === "rate")
    return json({ ok: false, id: msg.id, error: "this mailbox has sent too much too fast",
                  retry_after: claim.retry_after }, 429);
  if (claim.state === "full")
    return json({ ok: false, id: msg.id, error: "the delivery ledger for this mailbox is full" }, 503);
  if (claim.state === "known") return receiptJson(claim.receipt, true);
  if (claim.state !== "reserved")
    return json({ ok: false, id: msg.id, error: "could not reserve this send" }, 500);

  // Outside the transaction, and outside any retry. From here there is at most one binding call.
  const stableId = "send." + (await sha256hex(enc.encode(check.address + "\u0000" + msg.id))).slice(0, 32);
  let outcome;
  try {
    const receipt = await deliver(env, check.address, msg.to, msg, stableId);
    outcome = { status: "sent", receipt };
  } catch (e) {
    const code = errorCode(e);
    // An explicit refusal is a fact: it did not go. Anything else — E_INTERNAL_SERVER_ERROR, a
    // timeout, a disconnect — is genuinely unknown, and pretending it failed would invite a retry
    // that duplicates a message which may well have been delivered.
    outcome = REFUSED.test(code) ? { status: "failed", receipt: code }
                                 : { status: "unknown", receipt: code };
  }
  let done;
  try {
    done = await ledger(env, check.address, "complete", { id: msg.id, ...outcome });
  } catch {
    // The attempt happened; recording its outcome did not. A 500 here would be the worst answer —
    // it hides an id whose row is still `sending`, which every later read reports as unknown. Say
    // what was observed AND that the ledger did not take it. Nothing reissues either way.
    return json({ ok: outcome.status === "sent", id: msg.id, status: outcome.status,
                  receipt: outcome.receipt || null, duplicate: false, recorded: false,
                  error: "the outcome was not recorded in the ledger; /send-status will answer "
                         + "'unknown' for this id, and it will not be reissued automatically" },
                outcome.status === "sent" ? 200 : 202);
  }
  return receiptJson(done.receipt, false);
}

/**
 * The per-mailbox send ledger.
 *
 * SQLite-backed (see `new_sqlite_classes` in mail.wrangler.toml). Written as a classic Durable
 * Object rather than `extends DurableObject` on purpose: that base class comes from
 * `cloudflare:workers`, a static import of which makes this file unloadable by node and takes the
 * cross-implementation tests down with it.
 *
 * What is stored is deliberately thin — request id, the body's digest, a status, a provider receipt
 * and two timestamps. Never the subject, never the text. A ledger that remembers what was said is a
 * copy of the mail, which is the one thing this relay promises not to keep.
 */
export class MailDelivery {
  constructor(state) {
    this.state = state;
    this.sql = state.storage.sql;
    state.blockConcurrencyWhile(async () => {
      this.sql.exec(`CREATE TABLE IF NOT EXISTS receipts (
        id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
        receipt TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL)`);
    });
  }

  row(id) {
    return this.sql.exec(
      "SELECT id,digest,status,receipt,created,updated FROM receipts WHERE id=?", id).toArray()[0];
  }

  count(since) {
    return this.sql.exec("SELECT COUNT(*) AS n FROM receipts WHERE created>?", since)
               .toArray()[0].n;
  }

  async fetch(request) {
    const op = new URL(request.url).pathname.slice(1);
    const body = await request.json();
    const now = Math.floor(Date.now() / 1000);

    if (op === "lookup") return json({ receipt: this.row(body.id) || null });

    if (op === "reserve") {
      // transactionSync, and nothing awaited inside it: the whole point is that no second request
      // can observe the gap between "is there a row" and "there is now".
      return json(this.state.storage.transactionSync(() => {
        const existing = this.row(body.id);
        if (existing) {
          // Dedupe is checked before the rate guard on purpose — a caller asking about a send it
          // already made deserves its receipt even when the mailbox is over quota.
          if (!sameString(existing.digest, body.digest)) return { state: "conflict" };
          if (existing.status === "sending")
            return { state: "known",
                     receipt: { ...existing, status: "unknown" } };   // in flight elsewhere
          return { state: "known", receipt: existing };
        }
        // Expired receipts go first, and only those: dropping a row a client could still replay
        // would turn its retry into a second delivery, which is the exact failure being prevented.
        this.sql.exec("DELETE FROM receipts WHERE created<?", now - RECEIPT_TTL);
        if (this.count(now - 60) >= PER_MINUTE) return { state: "rate", retry_after: 60 };
        if (this.count(now - 3600) >= PER_HOUR) return { state: "rate", retry_after: 3600 };
        if (this.sql.exec("SELECT COUNT(*) AS n FROM receipts").toArray()[0].n >= MAX_RECEIPTS)
          return { state: "full" };
        this.sql.exec(
          "INSERT INTO receipts (id,digest,status,receipt,created,updated) VALUES (?,?,?,?,?,?)",
          body.id, body.digest, "sending", null, now, now);
        return { state: "reserved" };
      }));
    }

    if (op === "complete") {
      this.sql.exec("UPDATE receipts SET status=?,receipt=?,updated=? WHERE id=?",
                    body.status, body.receipt || null, now, body.id);
      // The row is always there — it was reserved — but a caller that gets back nothing would throw
      // while rendering the receipt, turning a delivered message into a 500.
      return json({ receipt: this.row(body.id) ||
                             { id: body.id, digest: "", status: body.status,
                               receipt: body.receipt || null, created: now, updated: now } });
    }
    return json({ error: "unknown ledger op" }, 400);
  }
}

// ════════════════════════════════════════════════════════════════════════════════════════════════
// Identity: who owns a handle, and who owns a dog
// ════════════════════════════════════════════════════════════════════════════════════════════════
//
// This used to be a read-then-write against KV — `get("handle:x")`, decide, `put("handle:x")` — with
// an await in the middle. Two claims for one name that arrive together both read "free" and both
// write, and the second one's key and email land on top of the first one's. The loser is not told,
// and "the loser's code will not match" is not a consolation: the identity the name is bound to is
// whichever write happened to be second, which is a name takeover with a race as its only cost.
//
// So the decision moves into a SQLite Durable Object named after the claim itself — one object per
// handle, one per dog address, so the serialization is per name and there is no global hot spot.
// The decision runs inside `transactionSync` with NOTHING awaited inside it, which is the only
// construct here that genuinely serializes across concurrent calls. KV is downgraded to a mirror
// that readers (`stamped`, `/send`) keep using unchanged; the DO writes it after it has committed.
//
// Existing KV identities are adopted, not ignored: the first op for a name imports whatever KV
// already holds as the baseline, so a handle verified before this change stays verified to the same
// key, and a re-claim of it with a different key is refused rather than silently rebound.

const CLAIM_TTL = 1800;         // an unverified claim expires, so a squatter cannot park a name
const CODE_COOLDOWN = 60;       // seconds before one pending claim will send another code
const CODE_MAX_SENDS = 5;       // verification emails per pending claim, ever
const CODE_MAX_ATTEMPTS = 5;    // wrong codes before the pending claim is burned
const EMAIL_PER_HOUR = 10;      // codes to one address per hour, across every handle

const lower = (v) => String(v == null ? "" : v).toLowerCase();

const IDENTITY_UNCONFIGURED =
  "the identity ledger is not configured: bind the CLAIMS durable object (class DirectoryClaims, " +
  "migration tag v2) before claiming names. Claiming is refused rather than served by the " +
  "read-then-write path it replaced, which can bind a name to the wrong key under concurrency.";

function claimsStub(env, name) {
  const ns = env.CLAIMS;
  return ns.get(ns.idFromName(name));
}

async function claims(env, name, op, payload) {
  const r = await claimsStub(env, name).fetch("https://directory-claims/" + op, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json();
}

/**
 * The per-name identity ledger.
 *
 * One row, because there is one name per object: the object's identity IS the lock. Holds the
 * public key the name is bound to, the owner address for a handle, the pending code and the two
 * counters that bound abuse. Never a message, never anything a dog said.
 */
export class DirectoryClaims {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.sql = state.storage.sql;
    state.blockConcurrencyWhile(async () => {
      this.sql.exec(`CREATE TABLE IF NOT EXISTS claim (
        k INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, pub TEXT NOT NULL,
        email TEXT, code TEXT, handle TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL,
        code_at INTEGER NOT NULL DEFAULT 0, sends INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0)`);
    });
  }

  current() {
    return this.sql.exec("SELECT * FROM claim WHERE k=1").toArray()[0];
  }

  write(row) {
    this.sql.exec(
      "INSERT OR REPLACE INTO claim (k,kind,name,pub,email,code,handle,created,updated," +
      "code_at,sends,attempts,verified) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
      row.kind, row.name, row.pub, row.email || null, row.code || null, row.handle || null,
      row.created, row.updated, row.code_at || 0, row.sends || 0, row.attempts || 0,
      row.verified ? 1 : 0);
    return row;
  }

  /**
   * Whatever KV already says about this name, as the starting state.
   *
   * Backward compatibility is the point: a handle verified by the old code path has no row here,
   * and treating that as "free" would hand the name to the next caller. Read before the
   * transaction because it awaits; the transaction is what makes only the first import count.
   */
  async imported(kind, name, now) {
    if (kind === "email") return null;
    let kv = null;
    try {
      kv = await this.env.DIRECTORY.get((kind === "dog" ? "dog:" : "handle:") + name, "json");
    } catch { kv = null; }
    if (!kv || !validPub(kv.pub)) return null;
    if (kind === "dog")
      return { kind, name, pub: kv.pub, handle: String(kv.handle || ""), email: "", code: "",
               created: now, updated: now, code_at: 0, sends: 0, attempts: 0, verified: 1 };
    return { kind, name, pub: kv.pub, email: String(kv.email || ""), code: String(kv.code || ""),
             handle: "", created: now, updated: now, code_at: now,
             sends: kv.verified ? 0 : 1, attempts: 0, verified: kv.verified ? 1 : 0 };
  }

  /** Publish the committed decision to the KV shape every reader already understands. */
  async mirror(row) {
    const now = Math.floor(Date.now() / 1000);
    if (row.kind === "dog") {
      await this.env.DIRECTORY.put("dog:" + row.name,
        JSON.stringify({ pub: row.pub, handle: row.handle }));
      return;
    }
    if (row.verified) {
      // No TTL: a verified handle is permanent, which is the whole point of it being a name.
      await this.env.DIRECTORY.put("handle:" + row.name,
        JSON.stringify({ pub: row.pub, email: row.email, verified: true }));
      return;
    }
    await this.env.DIRECTORY.put("handle:" + row.name,
      JSON.stringify({ pub: row.pub, email: row.email, code: row.code, verified: false }),
      { expirationTtl: Math.max(60, CLAIM_TTL - (now - row.created)) });
  }

  async fetch(request) {
    const op = new URL(request.url).pathname.slice(1);
    const body = await request.json();
    const now = Math.floor(Date.now() / 1000);
    const kind = op === "dog-claim" ? "dog" : op === "email-quota" ? "email" : "handle";
    // The import is an await, so it is deliberately OUTSIDE the transaction: two concurrent calls
    // may both read KV, and the transaction below is what makes only the first of them count.
    const seed = this.current() ? null : await this.imported(kind, body.name, now);
    const decided = this.state.storage.transactionSync(() => this.decide(op, body, seed, now));
    // Committed first, mirrored second. A crash in between leaves KV stale, which the next call
    // repairs — the reverse order would publish an identity the ledger never agreed to.
    if (decided.row) await this.mirror(decided.row);
    return json(decided.out, decided.status || 200);
  }

  /** Every branch here is synchronous. An await in this function would reopen the race. */
  decide(op, body, seed, now) {
    let row = this.current();
    if (!row && seed) row = this.write(seed);

    if (op === "handle-claim") {
      const pub = String(body.pub || "");
      // Kept as the owner typed it — the local part of an address is case-sensitive to the letter
      // of the RFC — but compared case-insensitively, which is how every provider treats it.
      const email = String(body.email == null ? "" : body.email);
      if (row && row.verified) {
        // An exact repeat of a claim that already succeeded is idempotent and mails nothing. Any
        // other key or address is a takeover attempt against a name that has an owner.
        if (sameString(row.pub, pub) && sameString(lower(row.email), lower(email)))
          return { out: { state: "verified" } };
        return { out: { state: "taken" } };
      }
      if (row && now - row.created <= CLAIM_TTL) {
        if (!sameString(row.pub, pub) || !sameString(lower(row.email), lower(email)))
          return { out: { state: "conflict", retry_after: CLAIM_TTL - (now - row.created) } };
        if (row.sends >= CODE_MAX_SENDS)
          return { out: { state: "exhausted", retry_after: CLAIM_TTL - (now - row.created) } };
        if (now - row.code_at < CODE_COOLDOWN)
          return { out: { state: "cooldown", retry_after: CODE_COOLDOWN - (now - row.code_at),
                          code_valid_for: CLAIM_TTL - (now - row.created) } };
        // The code is PRESERVED across a resend. Minting a new one would invalidate the message
        // already sitting in the owner's inbox every time an impatient client asked again.
        const next = this.write({ ...row, code_at: now, sends: row.sends + 1, updated: now });
        return { out: { state: "send", code: next.code, resend: true }, row: next };
      }
      const fresh = this.write({ kind: "handle", name: String(body.name), pub, email,
                                 code: verificationCode(), handle: "", created: now, updated: now,
                                 code_at: now, sends: 1, attempts: 0, verified: 0 });
      return { out: { state: "send", code: fresh.code, resend: false }, row: fresh };
    }

    if (op === "handle-verify") {
      const pub = String(body.pub || "");
      const code = String(body.code || "");
      if (!row) return { out: { state: "no-claim" } };
      if (row.verified) {
        // The same key finishing the same verification twice gets the same answer — a retry after
        // a dropped response must not read as "that code does not match".
        if (sameString(row.pub, pub) && row.code && sameString(row.code, code))
          return { out: { state: "ok", email: row.email, repeat: true } };
        return { out: { state: "bad" } };
      }
      if (now - row.created > CLAIM_TTL) return { out: { state: "expired" } };
      if (row.attempts >= CODE_MAX_ATTEMPTS)
        return { out: { state: "locked", retry_after: CLAIM_TTL - (now - row.created) } };
      // Both comparisons are constant-time: a six-digit secret checked with `!==` leaks its prefix.
      if (!sameString(row.pub, pub) || !sameString(String(row.code || ""), code)) {
        const tried = this.write({ ...row, attempts: row.attempts + 1, updated: now });
        return { out: { state: "bad", attempts_left: CODE_MAX_ATTEMPTS - tried.attempts } };
      }
      const done = this.write({ ...row, verified: 1, attempts: 0, updated: now });
      return { out: { state: "ok", email: done.email }, row: done };
    }

    if (op === "dog-claim") {
      const pub = String(body.pub || "");
      if (row && !sameString(row.pub, pub)) return { out: { state: "taken" } };
      const next = this.write({ kind: "dog", name: String(body.name), pub,
                                handle: String(body.handle || ""), email: "", code: "",
                                created: row ? row.created : now, updated: now,
                                code_at: 0, sends: 0, attempts: 0, verified: 1 });
      return { out: { state: "ok" }, row: next };
    }

    // One address, every handle: without this a caller mails a stranger ten thousand codes by
    // claiming ten thousand free names. Not mirrored to KV — it is a counter, not an identity.
    if (op === "email-quota") {
      const live = row && now - row.created <= 3600 ? row : null;
      const used = live ? live.sends : 0;
      if (used >= EMAIL_PER_HOUR)
        return { out: { state: "exhausted", retry_after: 3600 - (now - live.created) } };
      this.write({ kind: "email", name: String(body.name), pub: "-", email: String(body.name),
                   code: "", handle: "", created: live ? live.created : now, updated: now,
                   code_at: now, sends: used + 1, attempts: 0, verified: 0 });
      return { out: { state: "ok", remaining: EMAIL_PER_HOUR - used - 1 } };
    }

    return { out: { state: "error", error: "unknown claim op" }, status: 400 };
  }
}

// Exported for the cross-implementation test. Two halves of one protocol written in two languages
// agree only if something checks — "it looked right in both" is how a wire format silently forks.
export const _crypto = { lp, cat, x25519, hkdf, hmac, sameBytes, sealToDog, b64, ub64 };
export const _names = { foldName, blockedName };
export const _limits = { VERSION, MAX_MAIL_BYTES, MAX_SEND_BODY, MAX_TEXT, MAX_SUBJECT,
                         PAGE_ITEMS, PAGE_BYTES, PER_MINUTE, PER_HOUR, SKEW, TTL,
                         CLAIM_TTL, CODE_COOLDOWN, CODE_MAX_SENDS, CODE_MAX_ATTEMPTS,
                         EMAIL_PER_HOUR };

export default {
  /** Incoming mail. Cloudflare Email Routing sends every address here via a catch-all rule. */
  async email(message, env) {
    const to = (message.to || "").toLowerCase();
    const row = await env.DIRECTORY.get("dog:" + to, "json");
    if (!row) {
      // Nothing is stored for an address nobody claimed — an open relay that hoards mail for
      // addresses that do not exist is a spam trap with extra steps.
      message.setReject("550 no such recipient");
      return;
    }
    if (!validPub(row.pub)) {
      message.setReject("451 mailbox not ready");
      return;
    }
    // rawSize where the runtime offers it: refusing before reading is the difference between
    // declining a large message and buffering it in order to decline it.
    const declared = Number(message.rawSize);
    if (Number.isFinite(declared) && declared > MAX_MAIL_BYTES) {
      message.setReject("552 message exceeds the mailbox size limit");
      return;
    }
    const { bytes: raw, over } = await readBounded(message.raw, MAX_MAIL_BYTES);
    if (over) {
      // Rejected, not trimmed. The sender learns it did not arrive, which is true, instead of the
      // dog silently receiving a message that ends mid-header.
      message.setReject("552 message exceeds the mailbox size limit");
      return;
    }
    const payload = enc.encode(JSON.stringify({
      from: message.from,
      to,
      subject: message.headers.get("subject") || "",
      date: message.headers.get("date") || "",
      // The whole RFC822 message, byte-exact, so the dog can find a link, a code, or an attachment
      // reference without the relay having decided in advance which part mattered.
      raw: b64(raw),
      truncated: false,
      size: raw.length,
    }));
    const at = Math.floor(Date.now() / 1000);
    const id = hex(crypto.getRandomValues(new Uint8Array(12)));
    await env.MAIL.put(
      `m:${to}:${at}:${id}`,
      JSON.stringify({ id, at, env: await sealToDog(ub64(row.pub), payload) }),
      { expirationTtl: TTL });
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/pubkey")
      return json({ pub: b64(await relayPublic(env)) });

    if (url.pathname === "/handle/claim" && request.method === "POST") {
      // No ledger, no claim. The old read-then-write path is not a fallback: it is the defect, and
      // running it because a binding is missing would quietly restore the takeover window.
      if (!env.CLAIMS) return json({ ok: false, error: IDENTITY_UNCONFIGURED }, 503);
      const parsed = await readJson(request);
      if (parsed.error) return parsed.error;
      const d = parsed.value;
      const handle = String(d.handle || "").toLowerCase();
      if (!/^[a-z0-9][a-z0-9-]{1,30}$/.test(handle))
        return json({ ok: false, error: "a handle is 2-31 chars of a-z, 0-9 and -" }, 400);
      // The address goes into a `To:` header this Worker writes by hand. A value with a newline in
      // it is not a bad address, it is a second header of the attacker's choosing.
      if (!validEmail(d.email))
        return json({ ok: false, error: "that is not an email address we can send to" }, 400);
      if (!validPub(d.pub))
        return json({ ok: false, error: "a public key is 32 bytes of base64 X25519" }, 400);
      const bad = await blockedName(env, handle);
      if (bad) return json({ ok: false, error: bad }, 400);

      // The decision, serialized: which key this name is bound to, whether a code is owed, and
      // whether one was already sent. Nothing below it writes an identity.
      const claim = await claims(env, "handle:" + handle, "handle-claim",
                                 { name: handle, pub: d.pub, email: d.email });
      if (claim.state === "taken")
        return json({ ok: false, error: "that handle is taken" }, 409);
      if (claim.state === "verified")
        return json({ ok: true, sent: false, verified: true,
                      note: "that handle is already verified to this key" });
      if (claim.state === "conflict")
        return json({ ok: false, retry_after: claim.retry_after,
                      error: "a different key is already waiting to verify that handle; it is "
                             + "not overwritten. Try again once that claim expires." }, 409);
      if (claim.state === "exhausted")
        return json({ ok: false, retry_after: claim.retry_after,
                      error: `that claim has already been sent ${CODE_MAX_SENDS} codes` }, 429);
      if (claim.state === "cooldown")
        // A double-clicked claim is not a reason to mail somebody twice. The code already in their
        // inbox is still the live one, which is why it was not regenerated.
        return json({ ok: true, sent: false, retry_after: claim.retry_after,
                      note: "a code was already sent to that address and is still valid" });
      if (claim.state !== "send" || !/^[0-9]{6}$/.test(String(claim.code || "")))
        return json({ ok: false, error: "could not record that claim" }, 500);
      const code = claim.code;

      // Per address, across every handle: the claim guard above bounds one name, and mailing a
      // stranger a code from ten thousand fresh names would walk straight around it.
      const quota = await claims(env, "email:" + String(d.email).toLowerCase(), "email-quota",
                                 { name: String(d.email).toLowerCase() });
      if (quota.state !== "ok")
        return json({ ok: false, retry_after: quota.retry_after,
                      error: "that address has been sent too many codes recently" }, 429);

      // send_email takes an EmailMessage carrying a raw RFC-5322 message, not a {to, subject}
      // object — the binding is a mail transport, not a mail composer. Built by hand because this
      // Worker has no dependencies; the headers below are the minimum a receiver will not junk.
      const from = `no-reply@${env.MAIL_DOMAIN || "collie.run"}`;
      const raw =
        `From: collie <${from}>\r\n` +
        `To: ${d.email}\r\n` +
        `Subject: collie: your code is ${code}\r\n` +
        `Message-ID: <${crypto.randomUUID()}@${env.MAIL_DOMAIN || "collie.run"}>\r\n` +
        `Date: ${new Date().toUTCString()}\r\n` +
        `MIME-Version: 1.0\r\n` +
        `Content-Type: text/plain; charset=utf-8\r\n\r\n` +
        `${code} is the code that binds the handle "${handle}" to a key on your machine.\r\n\r\n` +
        `If you did not ask for this, ignore it — the claim expires in 30 minutes.\r\n`;
      try {
        const EmailMessage = await emailMessageClass(env);
        await env.MAILER.send(new EmailMessage(from, d.email, raw));
      } catch (e) {
        // Say which half failed. "could not claim" with no reason sends the reader to their own
        // code, and the usual cause is on Cloudflare's side: send_email may only deliver to an
        // address VERIFIED on this account.
        // The provider's own text is not repeated here: it can quote the account or a credential.
        return json({ ok: false, error: "could not send the code: " + errorCode(e) }, 502);
      }
      return json({ ok: true, sent: true, resent: Boolean(claim.resend) });
    }

    if (url.pathname === "/handle/verify" && request.method === "POST") {
      if (!env.CLAIMS) return json({ ok: false, error: IDENTITY_UNCONFIGURED }, 503);
      const parsed = await readJson(request);
      if (parsed.error) return parsed.error;
      const d = parsed.value;
      const handle = String(d.handle || "").toLowerCase();
      // Six digits is 900000 guesses, which is nothing without a bound on how many may be tried.
      // The bound is in the ledger, beside the code, where it cannot be raced past.
      const result = await claims(env, "handle:" + handle, "handle-verify",
                                  { name: handle, pub: d.pub, code: d.code });
      if (result.state === "ok") return json({ ok: true });
      if (result.state === "locked")
        return json({ ok: false, retry_after: result.retry_after,
                      error: "too many wrong codes for that claim; claim the handle again once "
                             + "this one expires" }, 429);
      if (result.state === "expired")
        return json({ ok: false, error: "that claim has expired; claim the handle again" }, 401);
      return json({ ok: false, error: "that code does not match this claim",
                    ...(typeof result.attempts_left === "number"
                        ? { attempts_left: result.attempts_left } : {}) }, 401);
    }

    if (url.pathname === "/dog/claim" && request.method === "POST") {
      if (!env.CLAIMS) return json({ ok: false, error: IDENTITY_UNCONFIGURED }, 503);
      const parsed = await readJson(request);
      if (parsed.error) return parsed.error;
      const d = parsed.value;
      const address = String(d.address || "").toLowerCase();
      const handle = String(d.handle || "").toLowerCase();
      if (!validEmail(address))
        return json({ ok: false, error: "that is not an address we can serve" }, 400);
      if (!validPub(d.pub))
        return json({ ok: false, error: "a public key is 32 bytes of base64 X25519" }, 400);
      const row = await env.DIRECTORY.get("handle:" + handle, "json");
      if (!row || !row.verified || !validPub(row.pub))
        return json({ ok: false, error: "verify the handle first" }, 403);
      if (!address.endsWith("." + handle + "@" + (env.MAIL_DOMAIN || "collie.run")))
        return json({ ok: false, error: "that address is not under your handle" }, 403);
      // The dog's name is in the address too, so it faces the same list as the handle.
      const badDog = await blockedName(env, address.split(".")[0]);
      if (badDog) return json({ ok: false, error: badDog }, 400);
      const want = await hmac(await certKey(env, ub64(row.pub)),
                              cat(lp(address), lp(ub64(d.pub))));
      let cert;
      try { cert = ub64(String(d.cert || "")); } catch { cert = new Uint8Array(0); }
      if (!sameBytes(want, cert))
        return json({ ok: false, error: "that claim is not signed by this handle" }, 403);
      // Same shape as the handle: the address is bound to one key, and an exact repeat is allowed
      // while a different key is refused — decided in one object, not across an await on KV.
      const claimed = await claims(env, "dog:" + address, "dog-claim",
                                   { name: address, pub: d.pub, handle });
      if (claimed.state === "taken")
        return json({ ok: false, error: "that address is already claimed" }, 409);
      if (claimed.state !== "ok")
        return json({ ok: false, error: "could not record that claim" }, 500);
      return json({ ok: true, address });
    }

    /**
     * What this relay is and is not, stated as configuration rather than as a promise.
     *
     * `send.configured` means the bindings exist — nothing more. Cloudflare can still refuse every
     * message because the sender domain is not verified on the account, and there is no call this
     * Worker can make that proves otherwise without sending real mail. Saying "ready" here would be
     * the useful-sounding lie; this says what it actually knows.
     */
    if (url.pathname === "/capabilities" && request.method === "GET")
      return json({
        ok: true,
        version: VERSION,
        receive: {
          bounded: true,
          max_bytes: MAX_MAIL_BYTES,
          oversize: "rejected at SMTP (552), never truncated",
          retention_seconds: TTL,
        },
        paging: { endpoint: "/mail-page", max_items: PAGE_ITEMS, max_bytes: PAGE_BYTES },
        identity: {
          ledger: Boolean(env.CLAIMS),
          serialized: Boolean(env.CLAIMS),
          code_max_sends: CODE_MAX_SENDS,
          code_max_attempts: CODE_MAX_ATTEMPTS,
          code_cooldown_seconds: CODE_COOLDOWN,
          claim_ttl_seconds: CLAIM_TTL,
          note: "claiming is refused outright when the ledger is unbound; names are never bound "
                + "by a read-then-write against KV",
        },
        send: {
          endpoint: "/send",
          binding: Boolean(env.MAILER),
          ledger: Boolean(env.MAIL_DELIVERY),
          configured: Boolean(env.MAILER && env.MAIL_DELIVERY),
          mode: sendMode(env),
          max_body_bytes: MAX_SEND_BODY,
          max_text_bytes: MAX_TEXT,
          destination: "the verified owner of the handle, and no one else",
          note: "configured is about bindings, not about permission — the account may still refuse",
        },
      });

    if (url.pathname === "/mail" && request.method === "GET") {
      const check = await stamped(request, env, url);
      if (check.error) return check.error;
      const since = Number(url.searchParams.get("since") || "0");
      const floor = Number.isFinite(since) ? since : 0;
      // The old code read ONE KV list page and called it the mailbox: past a thousand keys, mail
      // simply stopped existing. It now follows the cursor, with a bounded walk and an honest
      // `more` when it stops — `/mail-page` is the endpoint that can actually finish the job.
      const messages = [];
      let cursor;
      let more = false;
      for (let page = 0; page < LEGACY_PAGES; page++) {
        const list = await env.MAIL.list(
          { prefix: "m:" + check.address + ":", limit: KV_PAGE, cursor });
        for (const k of list.keys) {
          const at = Number(k.name.split(":")[2] || "0");
          if (!Number.isFinite(at) || at < floor) continue;
          const v = await env.MAIL.get(k.name, "json");
          if (v) messages.push({ ...v, id: messageId(k.name, v) });
        }
        cursor = list.cursor;
        more = !list.list_complete;
        if (list.list_complete) break;
      }
      messages.sort((a, b) => a.at - b.at);
      return json({ ok: true, messages, more });
    }

    /**
     * The paginated read. Bounded by BOTH a message count and a total sealed-byte budget, because
     * fifty 4 MiB messages is a response no client asked for and no Worker can build.
     *
     * The cursor carries the KV cursor AND the last key already handed out, so stopping in the
     * middle of a KV page loses nothing: the next call re-lists from the same KV cursor and drops
     * what the caller already has. Positional skipping would have been shorter and wrong — a
     * message expiring out of the middle shifts every later index by one and a message is skipped.
     */
    if (url.pathname === "/mail-page" && request.method === "GET") {
      const check = await stamped(request, env, url);
      if (check.error) return check.error;
      const raw = url.searchParams.get("cursor") || "";
      if (raw && !CURSOR_RE.test(raw))
        return json({ ok: false, error: "malformed cursor" }, 400);
      let from = { m: check.address, c: undefined, a: "" };
      if (raw) {
        try {
          from = JSON.parse(new TextDecoder().decode(ub64url(raw)));
        } catch {
          return json({ ok: false, error: "malformed cursor" }, 400);
        }
        // A cursor is scoped to the mailbox that was issued it. Carrying one across addresses is
        // the only way this endpoint could ever read someone else's mail, so it is named and checked
        // rather than trusted to KV's prefix.
        if (!from || typeof from !== "object" || from.m !== check.address)
          return json({ ok: false, error: "that cursor belongs to another mailbox" }, 400);
        if (from.c !== undefined && typeof from.c !== "string") from.c = undefined;
        if (typeof from.a !== "string") from.a = "";
      }

      const list = await env.MAIL.list(
        { prefix: "m:" + check.address + ":", limit: KV_PAGE, cursor: from.c || undefined });
      const messages = [];
      let bytes = 0;
      let last = from.a || "";
      let held = false;                 // something in THIS KV page was left for the next call
      for (const k of list.keys) {
        if (from.a && k.name <= from.a) continue;
        if (messages.length >= PAGE_ITEMS) { held = true; break; }
        const v = await env.MAIL.get(k.name, "json");
        if (!v) { last = k.name; continue; }   // expired between list and get — nothing to preserve
        const size = JSON.stringify(v.env || {}).length;
        // Always yield at least one message, however large: a message bigger than the whole budget
        // must not become a page that can never be got past.
        if (messages.length > 0 && bytes + size > PAGE_BYTES) { held = true; break; }
        messages.push({ id: messageId(k.name, v), at: v.at, env: v.env });
        bytes += size;
        last = k.name;
      }

      const more = held || !list.list_complete;
      // Held items live in the CURRENT KV page, so the next call repeats this KV cursor; a finished
      // page advances to the one KV handed back. Reading never deletes: unread mail stays unread.
      const next = held ? { m: check.address, c: from.c, a: last }
                        : { m: check.address, c: list.cursor, a: last };
      return json({
        ok: true,
        messages,
        next_cursor: more ? b64url(enc.encode(JSON.stringify(next))) : null,
        more,
      });
    }

    if (url.pathname === "/mail" && request.method === "DELETE") {
      const check = await stamped(request, env, url);
      if (check.error) return check.error;
      let cursor;
      let deleted = 0;
      for (let page = 0; page < LEGACY_PAGES; page++) {
        const list = await env.MAIL.list(
          { prefix: "m:" + check.address + ":", limit: KV_PAGE, cursor });
        await Promise.all(list.keys.map((k) => env.MAIL.delete(k.name)));
        deleted += list.keys.length;
        cursor = list.cursor;
        if (list.list_complete) break;
      }
      return json({ ok: true, deleted });
    }

    if (url.pathname === "/send" && request.method === "POST")
      return send(request, env, url);

    if (url.pathname === "/send-status" && request.method === "GET") {
      const check = await stamped(request, env, url);
      if (check.error) return check.error;
      if (!env.MAIL_DELIVERY)
        return json({ ok: false, error: "the delivery ledger is not configured" }, 501);
      const id = url.searchParams.get("id") || "";
      if (!REQUEST_ID_RE.test(id))
        return json({ ok: false, error: "malformed request id" }, 400);
      const found = await ledger(env, check.address, "lookup", { id });
      if (!found.receipt) return json({ ok: false, error: "no such request id", id }, 404);
      return receiptJson(found.receipt, true);
    }

    return json({ ok: false, error: "not found" }, 404);
  },
};
