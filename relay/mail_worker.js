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
 *   KV  MAIL      — sealed messages, TTL'd
 *   KV  DIRECTORY — handles and dogs
 *   secret RELAY_PRIVATE_B64 — the relay's X25519 private key (its public half is served at /pubkey)
 */

const SKEW = 120;                     // seconds a request stamp may be off
const TTL = 60 * 60 * 24 * 7;         // a week: long enough to be away, short enough not to hoard
const MAX_BYTES = 512 * 1024;         // a verification mail is small; anything vast is not our job

const enc = new TextEncoder();

const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const ub64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

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

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

/** Verify the stamp on a read. Refuses a stale one, a replayed one, and one made for another path. */
async function stamped(request, env, url) {
  const address = request.headers.get("x-collie-addr") || "";
  const ts = request.headers.get("x-collie-ts") || "";
  const nonce = request.headers.get("x-collie-nonce") || "";
  const mac = request.headers.get("x-collie-mac") || "";
  const row = await env.DIRECTORY.get("dog:" + address, "json");
  if (!row) return { error: json({ ok: false, error: "unknown address" }, 404) };
  if (Math.abs(Math.floor(Date.now() / 1000) - parseInt(ts || "0", 10)) > SKEW)
    return { error: json({ ok: false, error: "stale stamp" }, 401) };
  // The nonce is remembered for twice the allowed skew: long enough that no accepted stamp can be
  // replayed, short enough that the store does not grow without bound.
  if (await env.DIRECTORY.get("nonce:" + nonce))
    return { error: json({ ok: false, error: "replay" }, 401) };
  const want = await hmac(await authKey(env, ub64(row.pub), address),
                          cat(lp(request.method), lp(url.pathname + url.search), lp(ts), lp(nonce)));
  if (!sameBytes(want, ub64(mac || "")))
    return { error: json({ ok: false, error: "bad stamp" }, 401) };
  await env.DIRECTORY.put("nonce:" + nonce, "1", { expirationTtl: SKEW * 2 });
  return { address, row };
}

// Exported for the cross-implementation test. Two halves of one protocol written in two languages
// agree only if something checks — "it looked right in both" is how a wire format silently forks.
export const _crypto = { lp, cat, x25519, hkdf, hmac, sameBytes, sealToDog, b64, ub64 };

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
    const raw = new Uint8Array(await new Response(message.raw).arrayBuffer());
    const body = raw.length > MAX_BYTES ? raw.slice(0, MAX_BYTES) : raw;
    const payload = enc.encode(JSON.stringify({
      from: message.from,
      to,
      subject: message.headers.get("subject") || "",
      date: message.headers.get("date") || "",
      // The whole RFC822 message, so the dog can find a link, a code, or an attachment reference
      // without the relay having decided in advance which part mattered.
      raw: b64(body),
      truncated: raw.length > MAX_BYTES,
    }));
    const at = Math.floor(Date.now() / 1000);
    await env.MAIL.put(
      `m:${to}:${at}:${crypto.randomUUID().slice(0, 8)}`,
      JSON.stringify({ at, env: await sealToDog(ub64(row.pub), payload) }),
      { expirationTtl: TTL });
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/pubkey")
      return json({ pub: b64(await relayPublic(env)) });

    if (url.pathname === "/handle/claim" && request.method === "POST") {
      const d = await request.json();
      const handle = String(d.handle || "").toLowerCase();
      if (!/^[a-z0-9][a-z0-9-]{1,30}$/.test(handle))
        return json({ ok: false, error: "a handle is 2-31 chars of a-z, 0-9 and -" }, 400);
      const existing = await env.DIRECTORY.get("handle:" + handle, "json");
      if (existing && existing.verified)
        return json({ ok: false, error: "that handle is taken" }, 409);
      const code = String(Math.floor(100000 + Math.random() * 900000));
      await env.DIRECTORY.put("handle:" + handle,
        JSON.stringify({ pub: d.pub, email: d.email, code, verified: false }),
        { expirationTtl: 1800 });        // an unverified claim expires, so a squatter cannot park one
      await env.MAILER.send({
        to: d.email,
        subject: `collie: your code is ${code}`,
        text: `${code} is the code that binds the handle "${handle}" to this machine's key.\n\n`
            + `If you did not ask for this, ignore it — the claim expires in 30 minutes.\n`,
      });
      return json({ ok: true, sent: true });
    }

    if (url.pathname === "/handle/verify" && request.method === "POST") {
      const d = await request.json();
      const handle = String(d.handle || "").toLowerCase();
      const row = await env.DIRECTORY.get("handle:" + handle, "json");
      if (!row || row.code !== String(d.code || "") || row.pub !== d.pub)
        return json({ ok: false, error: "that code does not match this claim" }, 401);
      // No TTL now: a verified handle is permanent, which is the whole point of it being a name.
      await env.DIRECTORY.put("handle:" + handle,
        JSON.stringify({ pub: row.pub, email: row.email, verified: true }));
      return json({ ok: true });
    }

    if (url.pathname === "/dog/claim" && request.method === "POST") {
      const d = await request.json();
      const address = String(d.address || "").toLowerCase();
      const handle = String(d.handle || "").toLowerCase();
      const row = await env.DIRECTORY.get("handle:" + handle, "json");
      if (!row || !row.verified)
        return json({ ok: false, error: "verify the handle first" }, 403);
      if (!address.endsWith("." + handle + "@" + (env.MAIL_DOMAIN || "collie.run")))
        return json({ ok: false, error: "that address is not under your handle" }, 403);
      const want = await hmac(await certKey(env, ub64(row.pub)),
                              cat(lp(address), lp(ub64(d.pub))));
      if (!sameBytes(want, ub64(d.cert || "")))
        return json({ ok: false, error: "that claim is not signed by this handle" }, 403);
      const taken = await env.DIRECTORY.get("dog:" + address, "json");
      if (taken && taken.pub !== d.pub)
        return json({ ok: false, error: "that address is already claimed" }, 409);
      await env.DIRECTORY.put("dog:" + address, JSON.stringify({ pub: d.pub, handle }));
      return json({ ok: true, address });
    }

    if (url.pathname === "/mail" && request.method === "GET") {
      const check = await stamped(request, env, url);
      if (check.error) return check.error;
      const since = parseInt(url.searchParams.get("since") || "0", 10);
      const list = await env.MAIL.list({ prefix: "m:" + check.address + ":" });
      const messages = [];
      for (const k of list.keys) {
        const at = parseInt(k.name.split(":")[2] || "0", 10);
        if (at < since) continue;
        const v = await env.MAIL.get(k.name, "json");
        if (v) messages.push(v);
      }
      messages.sort((a, b) => a.at - b.at);
      return json({ ok: true, messages });
    }

    if (url.pathname === "/mail" && request.method === "DELETE") {
      const check = await stamped(request, env, url);
      if (check.error) return check.error;
      const list = await env.MAIL.list({ prefix: "m:" + check.address + ":" });
      await Promise.all(list.keys.map((k) => env.MAIL.delete(k.name)));
      return json({ ok: true, deleted: list.keys.length });
    }

    return json({ ok: false, error: "not found" }, 404);
  },
};
