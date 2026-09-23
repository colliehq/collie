/**
 * The mail relay, run for real.
 *
 * Not a mock of the Worker — the Worker. `relay/mail_worker.js` is imported and its exported
 * `fetch`, `email` and `MailDelivery` are executed against in-memory stand-ins for KV, the Durable
 * Object namespace and the send binding, with genuine X25519/HKDF/HMAC stamps on every request. The
 * things this file is here to prove cannot be proven any other way: that two concurrent sends of
 * one request id produce exactly ONE call to the mail binding, that an ambiguous provider failure
 * is never quietly retried, and that paging a mailbox under a byte cap loses nothing.
 *
 *   node tests/mail_relay_test.mjs
 */
import { webcrypto } from "node:crypto";
import { Buffer } from "node:buffer";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

// Imported the same way tests/mail_crossimpl_test.js does it, so Node 20 in CI loads the Worker
// module without a Workers runtime: nothing Workers-only is imported at the top of that file.
const mod = await import("../relay/mail_worker.js");
const worker = mod.default;
const { MailDelivery, DirectoryClaims, _crypto, _limits } = mod;
const { lp, cat, x25519, hkdf, hmac, b64, ub64 } = _crypto;

const enc = new TextEncoder();
const dec = new TextDecoder();
const failures = [];
function check(value, message) {
  console.log((value ? "  PASS " : "  FAIL ") + message);
  if (!value) failures.push(message);
}

const hex = (buf) => [...new Uint8Array(buf)].map((x) => x.toString(16).padStart(2, "0")).join("");
const sha = async (bytes) => hex(await crypto.subtle.digest("SHA-256", bytes));
const b64url = (bytes) => b64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

// ── stand-ins ───────────────────────────────────────────────────────────────────────────────────

/** KV, including the part the Worker used to ignore: list() pages, with a cursor. */
function fakeKV() {
  const m = new Map();
  return {
    m,
    async get(k, type) {
      const v = m.get(k);
      if (v === undefined) return null;
      return type === "json" ? JSON.parse(v) : v;
    },
    async put(k, v) { m.set(k, String(v)); },
    async delete(k) { m.delete(k); },
    async list({ prefix = "", limit = 1000, cursor } = {}) {
      const all = [...m.keys()].filter((k) => k.startsWith(prefix)).sort();
      const start = cursor ? Number(cursor) : 0;
      const slice = all.slice(start, start + limit);
      const end = start + slice.length;
      const complete = end >= all.length;
      return { keys: slice.map((name) => ({ name })), list_complete: complete,
               cursor: complete ? undefined : String(end) };
    },
  };
}

/**
 * Just enough SQLite to run the ledger's own statements — matched by their leading clause, so a
 * query this fake has not been taught throws instead of silently returning nothing.
 */
function fakeSql() {
  const rows = new Map();
  const cols = ["id", "digest", "status", "receipt", "created", "updated"];
  const result = (list) => ({ toArray: () => list });
  return {
    rows,
    exec(query, ...args) {
      const q = query.replace(/\s+/g, " ").trim();
      if (q.startsWith("CREATE TABLE")) return result([]);
      if (q.startsWith("SELECT id,digest")) {
        const row = rows.get(args[0]);
        return result(row ? [{ ...row }] : []);
      }
      if (q.startsWith("SELECT COUNT(*) AS n FROM receipts WHERE created>"))
        return result([{ n: [...rows.values()].filter((r) => r.created > args[0]).length }]);
      if (q.startsWith("SELECT COUNT(*) AS n FROM receipts")) return result([{ n: rows.size }]);
      if (q.startsWith("DELETE FROM receipts WHERE created<")) {
        for (const [k, r] of [...rows]) if (r.created < args[0]) rows.delete(k);
        return result([]);
      }
      if (q.startsWith("INSERT INTO receipts")) {
        const row = {};
        cols.forEach((c, i) => { row[c] = args[i]; });
        rows.set(row.id, row);
        return result([]);
      }
      if (q.startsWith("UPDATE receipts SET")) {
        const row = rows.get(args[3]);
        if (row) { row.status = args[0]; row.receipt = args[1]; row.updated = args[2]; }
        return result([]);
      }
      throw new Error("fake sql does not know this statement: " + q);
    },
  };
}

/**
 * The identity ledger's storage: one row per object, because there is one name per object.
 * Same rule as above — an unrecognised statement throws rather than returning a helpful nothing.
 */
function fakeClaimSql() {
  const cols = ["kind", "name", "pub", "email", "code", "handle", "created", "updated",
                "code_at", "sends", "attempts", "verified"];
  const held = { row: null };
  const result = (list) => ({ toArray: () => list });
  return {
    held,
    exec(query, ...args) {
      const q = query.replace(/\s+/g, " ").trim();
      if (q.startsWith("CREATE TABLE")) return result([]);
      if (q.startsWith("SELECT * FROM claim")) return result(held.row ? [{ ...held.row }] : []);
      if (q.startsWith("INSERT OR REPLACE INTO claim")) {
        const row = {};
        cols.forEach((c, i) => { row[c] = args[i]; });
        held.row = row;
        return result([]);
      }
      throw new Error("fake sql does not know this statement: " + q);
    },
  };
}

/**
 * One durable object per name, as the real namespace does — the identity IS the guarantee.
 *
 * `transactionSync` is synchronous here exactly as it is in the runtime: that is what the ledgers
 * rely on, and a fake that awaited inside it would prove nothing about the real one.
 */
function fakeDurableObjects(build) {
  const objects = new Map();
  const ns = {
    objects,
    env: null,
    idFromName(name) { return name; },
    get(id) {
      if (!objects.has(id)) {
        const sql = build === "claims" ? fakeClaimSql() : fakeSql();
        const state = { storage: { sql, transactionSync: (fn) => fn() },
                        blockConcurrencyWhile: (fn) => fn() };
        objects.set(id, build === "claims" ? new DirectoryClaims(state, ns.env)
                                           : new MailDelivery(state));
      }
      const object = objects.get(id);
      return { fetch: (url, init) => object.fetch(new Request(url, init)) };
    },
  };
  return ns;
}

/** The send binding. `behaviour` decides what Cloudflare "says"; every call is recorded. */
function fakeMailer(behaviour) {
  const calls = [];
  return {
    calls,
    async send(message) {
      calls.push(message);
      if (behaviour) return behaviour(message, calls.length);
      return { messageId: "cf-" + calls.length };
    },
  };
}

/** The legacy raw-message constructor, injected because `cloudflare:email` does not exist here. */
class FakeEmailMessage {
  constructor(from, to, raw) { Object.assign(this, { from, to, raw }); }
}

// ── keys and stamps ─────────────────────────────────────────────────────────────────────────────

async function keypair() {
  const pair = await crypto.subtle.generateKey({ name: "X25519" }, true, ["deriveBits"]);
  const pub = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  const pkcs8 = new Uint8Array(await crypto.subtle.exportKey("pkcs8", pair.privateKey));
  return { pub, priv: pkcs8.slice(pkcs8.length - 32) };
}

async function fixture({ mailer = fakeMailer(), mode, mailerBinding = true,
                         claimsBinding = true } = {}) {
  const [relay, dog, handle] = [await keypair(), await keypair(), await keypair()];
  const MAIL = fakeKV();
  const DIRECTORY = fakeKV();
  const address = "rowan.daming@collie.run";
  await DIRECTORY.put("dog:" + address, JSON.stringify({ pub: b64(dog.pub), handle: "daming" }));
  await DIRECTORY.put("handle:daming", JSON.stringify(
    { pub: b64(handle.pub), email: "owner@example.com", verified: true }));
  const CLAIMS = fakeDurableObjects("claims");
  const env = {
    MAIL, DIRECTORY, MAIL_DELIVERY: fakeDurableObjects(), MAIL_DOMAIN: "collie.run",
    RELAY_PRIVATE_B64: b64(relay.priv), RELAY_PUBLIC_B64: b64(relay.pub),
    EMAIL_MESSAGE: FakeEmailMessage,
  };
  if (claimsBinding) env.CLAIMS = CLAIMS;
  CLAIMS.env = env;
  if (mailerBinding) env.MAILER = mailer;
  if (mode) env.MAIL_SEND_MODE = mode;
  const authKey = await hkdf(await x25519(dog.priv, relay.pub),
                             enc.encode(address), enc.encode("collie-mail-auth"));
  return { env, address, dog, relay, handle, authKey, mailer, MAIL, DIRECTORY, CLAIMS };
}

async function stamp(ctx, method, path, { ts } = {}) {
  const at = ts === undefined ? String(Math.floor(Date.now() / 1000)) : ts;
  const nonce = b64(crypto.getRandomValues(new Uint8Array(12)));
  const mac = await hmac(ctx.authKey, cat(lp(method), lp(path), lp(at), lp(nonce)));
  return { "x-collie-addr": ctx.address, "x-collie-ts": at,
           "x-collie-nonce": nonce, "x-collie-mac": b64(mac) };
}

const call = (ctx, method, path, headers, body) =>
  worker.fetch(new Request("https://mail.collie.run" + path, { method, headers, body }), ctx.env);

async function get(ctx, path, options) {
  return call(ctx, "GET", path, await stamp(ctx, "GET", path, options));
}

/** POST /send the way a client must: digest in the query, and the stamp over that whole query. */
async function postSend(ctx, body, { wire } = {}) {
  const raw = JSON.stringify(body);
  const digest = await sha(enc.encode(raw));
  const path = `/send?sha256=${digest}`;
  const headers = await stamp(ctx, "POST", path);
  headers["content-type"] = "application/json";
  return call(ctx, "POST", path, headers, wire === undefined ? raw : wire);
}

/** The unauthenticated claim endpoints: a JSON POST, exactly as the Python client makes it. */
const post = (ctx, path, body) =>
  call(ctx, "POST", path, { "content-type": "application/json" }, JSON.stringify(body));

/** The tag a handle signs a dog's address with — `certKey` from the relay's side of the X25519. */
async function cert(ctx, address, dogPub) {
  const key = await hkdf(await x25519(ctx.handle.priv, ctx.relay.pub),
                         enc.encode("handle"), enc.encode("collie-mail-cert"));
  return b64(await hmac(key, cat(lp(address), lp(dogPub))));
}

/**
 * Run `fn` with the clock moved forward. Cooldowns, claim expiry and the send budget are all
 * measured in wall-clock seconds, and a test that cannot move the clock can only assert the first
 * second of any of them.
 */
const realNow = Date.now;
async function travel(seconds, fn) {
  Date.now = () => realNow() + seconds * 1000;
  try { return await fn(); } finally { Date.now = realNow; }
}

const note = (extra = {}) => ({
  id: "req-" + "abcdefgh", to: "owner@example.com",
  subject: "rowan finished the release check", text: "All 412 tests are green.\n", ...extra,
});

// ── incoming stand-in ───────────────────────────────────────────────────────────────────────────

function incoming(to, bytes, { rawSize } = {}) {
  const body = typeof bytes === "string" ? enc.encode(bytes) : bytes;
  const rejected = [];
  return {
    to, from: "stripe@example.com", rejected,
    rawSize: rawSize === undefined ? body.length : rawSize,
    headers: new Map([["subject", "Verify your email"], ["date", "Tue, 23 Sep 2026 09:00:00 GMT"]]),
    get raw() { return new Response(body).body; },
    setReject(reason) { rejected.push(reason); },
  };
}

/** Open an envelope the way the dog does, to prove the stored bytes are the delivered bytes. */
async function openSealed(dogPriv, sealed) {
  const shared = await x25519(dogPriv, ub64(sealed.epk));
  const key = await crypto.subtle.importKey(
    "raw", await hkdf(shared, new Uint8Array(0), enc.encode("collie-mail-seal")),
    "AES-GCM", false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: ub64(sealed.n), additionalData: lp(ub64(sealed.epk)) },
    key, ub64(sealed.ct));
  return JSON.parse(dec.decode(plain));
}

// ════════════════════════════════════════════════════════════════════════════════════════════════

async function main() {
  console.log("── outbound: exactly one side effect ──");
  {
    // Two requests, same id, in flight together. The whole reason the ledger is a Durable Object
    // and not KV: with KV both read "nothing sent yet" and the owner is mailed twice.
    const ctx = await fixture();
    const [a, b] = await Promise.all([postSend(ctx, note()), postSend(ctx, note())]);
    const codes = [a.status, b.status].sort();
    check(ctx.mailer.calls.length === 1,
          "two concurrent sends of one request id call the mail binding exactly once");
    check(codes[0] === 200 && codes[1] === 202,
          "one of them is the send, the other is told the outcome is not yet known (202)");
    const winner = a.status === 200 ? await a.json() : await b.json();
    check(winner.status === "sent" && winner.receipt === "cf-1" && winner.duplicate === false,
          "the winner carries the provider's messageId as its receipt");

    const replay = await postSend(ctx, note());
    const body = await replay.json();
    check(ctx.mailer.calls.length === 1 && replay.status === 200 && body.duplicate === true,
          "and replaying the same id afterwards returns the receipt without sending again");

    const status = await get(ctx, "/send-status?id=req-abcdefgh");
    const seen = await status.json();
    check(status.status === 200 && seen.status === "sent" && seen.receipt === "cf-1",
          "GET /send-status looks the completed receipt up by id");
    check(!("subject" in seen) && !("text" in seen) &&
          ![...ctx.env.MAIL_DELIVERY.objects.values()]
            .some((o) => [...o.sql.rows.values()].some((r) =>
              JSON.stringify(r).includes("release check") || JSON.stringify(r).includes("412"))),
          "the ledger stores digests and statuses — never the subject or the body");
  }

  {
    const ctx = await fixture();
    const conflict = await postSend(ctx, note({ text: "a completely different message\n" }));
    check(conflict.status === 200, "a first send with a fresh id goes through");
    const second = await postSend(ctx, note({ text: "changed my mind\n" }));
    check(second.status === 409 && ctx.mailer.calls.length === 1,
          "the same id with a different body is 409, not a silent second delivery");
  }

  console.log("── outbound: ambiguity is never retried ──");
  {
    // E_INTERNAL_SERVER_ERROR is the one Cloudflare error that means "we do not know". Treating it
    // as a failure and retrying is how one notification becomes two.
    const mailer = fakeMailer(() => { throw new Error("E_INTERNAL_SERVER_ERROR"); });
    const ctx = await fixture({ mailer });
    const r = await postSend(ctx, note());
    const body = await r.json();
    check(r.status === 202 && body.status === "unknown" && body.ok === false,
          "an internal provider error leaves the send in 'unknown', not 'failed'");
    const again = await postSend(ctx, note());
    const againBody = await again.json();
    check(mailer.calls.length === 1 && again.status === 202 && againBody.status === "unknown",
          "replaying an unknown send does NOT reissue it — the ledger answers, the binding is idle");
    const status = await get(ctx, "/send-status?id=req-abcdefgh");
    check((await status.json()).status === "unknown",
          "and /send-status keeps saying unknown rather than inventing an outcome");
  }

  {
    const mailer = fakeMailer(() => { throw new Error("E_RECIPIENT_NOT_ALLOWED: nope"); });
    const ctx = await fixture({ mailer });
    const r = await postSend(ctx, note());
    const body = await r.json();
    check(r.status === 502 && body.status === "failed" && body.error === "E_RECIPIENT_NOT_ALLOWED",
          "an explicit provider refusal IS a failure, recorded as its code alone");
    check(!JSON.stringify(body).includes("nope"),
          "and the provider's message body never leaves the Worker");
  }

  {
    const ctx = await fixture({ mailer: fakeMailer(() => undefined), mode: "legacy" });
    const r = await postSend(ctx, note());
    const body = await r.json();
    const sent = ctx.mailer.calls[0];
    check(r.status === 200 && /^<send\.[0-9a-f]{32}@collie\.run>$/.test(body.receipt),
          "legacy mode resolves undefined, so our own stable Message-ID is the submission receipt");
    check(sent instanceof FakeEmailMessage && sent.raw.includes("Auto-Submitted: auto-replied") &&
          sent.raw.includes("X-Auto-Response-Suppress: All") &&
          sent.raw.includes("Content-Type: text/plain"),
          "the legacy raw message is a plain-text auto-reply that will not start a loop");
  }

  {
    const ctx = await fixture();
    await postSend(ctx, note({ in_reply_to: "<abc@example.com>",
                               references: "<a@x.com> <b@x.com>" }));
    const sent = ctx.mailer.calls[0];
    check(!("messageId" in sent.headers) && !("Message-ID" in sent.headers) &&
          sent.headers["Auto-Submitted"] === "auto-replied" &&
          sent.headers["In-Reply-To"] === "<abc@example.com>" &&
          sent.headers.References === "<a@x.com> <b@x.com>",
          "the structured builder gets threading headers and no Message-ID (the platform owns it)");
    check(sent.from === ctx.address && sent.to === "owner@example.com" &&
          typeof sent.text === "string" && sent.html === undefined,
          "From is always the authenticated dog, To is the owner, and the body is plain text");
  }

  {
    // The send succeeds and the ledger then cannot be told. A 500 here would hide an id whose row
    // is stuck at `sending` — the caller needs to know a message went out under that id.
    const ctx = await fixture();
    const ns = ctx.env.MAIL_DELIVERY;
    const real = ns.get.bind(ns);
    ns.get = (id) => {
      const stub = real(id);
      return { fetch: (url, init) => String(url).endsWith("/complete")
                 ? Promise.reject(new Error("durable object unreachable"))
                 : stub.fetch(url, init) };
    };
    const r = await postSend(ctx, note({ id: "req-lost-receipt" }));
    const body = await r.json();
    ns.get = real;
    check(r.status === 200 && body.status === "sent" && body.recorded === false &&
          ctx.mailer.calls.length === 1,
          "a send whose outcome cannot be recorded reports what happened instead of a bare 500");
    const after = await get(ctx, "/send-status?id=req-lost-receipt");
    check((await after.json()).status === "unknown",
          "the id then reads as unknown — the reservation stands, so nothing reissues it");
    const replay = await postSend(ctx, note({ id: "req-lost-receipt" }));
    check(replay.status === 202 && ctx.mailer.calls.length === 1,
          "and replaying it never reaches the binding a second time");
  }

  console.log("── outbound: the destination is not negotiable ──");
  {
    const ctx = await fixture();
    for (const [body, what] of [
      [note({ to: "someone.else@example.com" }), "an address that is not the handle's owner"],
      [note({ to: "owner@example.com\r\nBcc: evil@x.com" }), "a destination with a header break"],
      [note({ cc: "evil@x.com" }), "a cc field"],
      [note({ bcc: "evil@x.com" }), "a bcc field"],
      [note({ from: "ceo@bank.example" }), "a caller-chosen From"],
      [note({ id: "short" }), "a request id below the minimum length"],
      [note({ subject: "two\nlines" }), "a subject carrying a newline"],
      [note({ text: "" }), "an empty body"],
      [note({ text: "x".repeat(_limits.MAX_TEXT + 1) }), "a body over the text limit"],
      [note({ in_reply_to: "not-a-message-id" }), "an In-Reply-To that is not a <message-id>"],
      [note({ references: "<" + "x".repeat(_limits.MAX_SUBJECT * 8) + "@x>" }),
       "a References header over the 2048-byte header limit"],
    ]) {
      const r = await postSend(ctx, body);
      check(r.status === 400, `/send refuses ${what}`);
    }
    check(ctx.mailer.calls.length === 0, "and none of those reached the mail binding");
  }

  console.log("── outbound: the request has to be the request that was signed ──");
  {
    const ctx = await fixture();
    const raw = JSON.stringify(note());
    const digest = await sha(enc.encode(raw));
    const path = `/send?sha256=${digest}`;
    const headers = await stamp(ctx, "POST", path);
    const tampered = await call(ctx, "POST", path, headers,
                                JSON.stringify(note({ to: "owner@example.com", text: "changed" })));
    check(tampered.status === 400 && ctx.mailer.calls.length === 0,
          "a body swapped under a valid stamp fails the digest check");

    const noDigest = await call(ctx, "POST", "/send",
                                await stamp(ctx, "POST", "/send"), raw);
    check(noDigest.status === 400, "and a /send with no ?sha256= at all is refused");

    // The stamp covers pathname+search, so moving the digest moves the MAC too.
    const moved = await call(ctx, "POST", `/send?sha256=${"0".repeat(64)}`, headers, raw);
    check(moved.status === 401, "changing the digest in the query invalidates the stamp itself");

    const big = "x".repeat(_limits.MAX_SEND_BODY + 1024);
    const oversize = await call(ctx, "POST", `/send?sha256=${"a".repeat(64)}`,
                                await stamp(ctx, "POST", `/send?sha256=${"a".repeat(64)}`), big);
    check(oversize.status === 413, "a request body past the send limit is refused before parsing");
  }

  console.log("── auth: a timestamp has to be a number ──");
  {
    const ctx = await fixture();
    // parseInt("garbage") is NaN and `Math.abs(NaN) > SKEW` is false, so these used to skip the
    // freshness check entirely and be judged on the MAC alone.
    for (const ts of ["abc", "NaN", "", "1e99", "1770000000xyz", "Infinity", "12.5"]) {
      const path = "/mail?since=0";
      const r = await call(ctx, "GET", path, await stamp(ctx, "GET", path, { ts }));
      check(r.status === 401, `a stamp timestamped "${ts}" is refused`);
    }
    const old = String(Math.floor(Date.now() / 1000) - _limits.SKEW - 5);
    const stale = await call(ctx, "GET", "/mail", await stamp(ctx, "GET", "/mail", { ts: old }));
    check(stale.status === 401, "and one that is merely too old is still refused");
    const good = await get(ctx, "/mail");
    check(good.status === 200, "while an honest stamp is accepted");
    const nonce = await stamp(ctx, "GET", "/mail");
    check((await call(ctx, "GET", "/mail", nonce)).status === 200 &&
          (await call(ctx, "GET", "/mail", nonce)).status === 401,
          "a replayed nonce is refused the second time");
  }

  console.log("── incoming: bounded, exact, or rejected ──");
  {
    const big = new Uint8Array(200 * 1024);
    crypto.getRandomValues(big.subarray(0, 65536));
    check(_crypto.b64(big) === Buffer.from(big).toString("base64"),
          "base64 of 200 KiB matches the reference encoder (the spread form threw RangeError)");

    const ctx = await fixture();
    const mime = "From: stripe@example.com\r\nSubject: Verify your email\r\n\r\n" +
                 "code 123456\r\n" + "-".repeat(100000);
    await worker.email(incoming(ctx.address, mime), ctx.env);
    const key = [...ctx.MAIL.m.keys()][0];
    const row = JSON.parse(ctx.MAIL.m.get(key));
    const opened = await openSealed(ctx.dog.priv, row.env);
    check(dec.decode(ub64(opened.raw)) === mime && opened.truncated === false,
          "a message inside the cap is stored byte-exact, with nothing truncated");
    check(typeof row.id === "string" && row.id.length === 24 && key.endsWith(":" + row.id),
          "and carries a stable opaque id that the KV key agrees with");

    const declared = incoming(ctx.address, "small", { rawSize: _limits.MAX_MAIL_BYTES + 1 });
    await worker.email(declared, ctx.env);
    check(declared.rejected.length === 1 && /^552 /.test(declared.rejected[0]) &&
          ctx.MAIL.m.size === 1,
          "a message whose declared rawSize is over the cap is rejected before it is read");

    // rawSize under-reported: the stream bound is what actually holds, and it holds.
    const lying = incoming(ctx.address, new Uint8Array(_limits.MAX_MAIL_BYTES + 4096),
                           { rawSize: 10 });
    await worker.email(lying, ctx.env);
    check(lying.rejected.length === 1 && ctx.MAIL.m.size === 1,
          "an under-reported size is caught by the bounded read, not by trusting the sender");

    const stranger = incoming("nobody@collie.run", "hello");
    await worker.email(stranger, ctx.env);
    check(stranger.rejected.length === 1 && /^550 /.test(stranger.rejected[0]),
          "mail for an address nobody claimed is still refused rather than hoarded");
  }

  console.log("── reading: pages that finish, and lose nothing ──");
  {
    const ctx = await fixture();
    const wanted = new Set();
    for (let i = 0; i < 205; i++) {
      const id = String(i).padStart(24, "0");
      wanted.add(id);
      await ctx.MAIL.put(`m:${ctx.address}:${1700000000 + i}:${id}`,
                         JSON.stringify({ id, at: 1700000000 + i, env: { ct: "tiny" } }));
    }
    const seen = [];
    let cursor = "";
    let pages = 0;
    for (;;) {
      const path = "/mail-page" + (cursor ? "?cursor=" + cursor : "");
      const r = await get(ctx, path);
      const body = await r.json();
      if (r.status !== 200) { check(false, "paging stayed 200: " + JSON.stringify(body)); break; }
      pages += 1;
      for (const m of body.messages) seen.push(m.id);
      if (body.messages.length > _limits.PAGE_ITEMS)
        check(false, "a page stayed within the item cap");
      if (!body.more) { check(body.next_cursor === null, "the last page reports no cursor"); break; }
      cursor = body.next_cursor;
      if (pages > 30) { check(false, "paging terminated"); break; }
    }
    const unique = new Set(seen);
    check(pages >= 5 && seen.length === 205 && unique.size === 205,
          `205 messages come back exactly once each across ${pages} pages (KV list pages crossed)`);
    check([...wanted].every((id) => unique.has(id)),
          "every durable id that went in comes back out — no timestamp high-water loss");
    check(ctx.MAIL.m.size === 205, "and reading never deleted a single unread message");
  }

  {
    // The other bound: a handful of large messages must not be assembled into one vast response.
    const ctx = await fixture();
    const ct = "A".repeat(1_500_000);
    for (let i = 0; i < 5; i++)
      await ctx.MAIL.put(`m:${ctx.address}:${1700000000 + i}:${String(i).repeat(24)}`,
                         JSON.stringify({ id: String(i).repeat(24), at: 1700000000 + i,
                                          env: { ct } }));
    const seen = [];
    let cursor = "";
    let pages = 0;
    for (;;) {
      const r = await get(ctx, "/mail-page" + (cursor ? "?cursor=" + cursor : ""));
      const body = await r.json();
      pages += 1;
      for (const m of body.messages) seen.push(m.id);
      const bytes = body.messages.reduce((n, m) => n + JSON.stringify(m.env).length, 0);
      check(bytes <= _limits.PAGE_BYTES || body.messages.length === 1,
            `page ${pages} stays inside the ${_limits.PAGE_BYTES}-byte budget`);
      if (!body.more) break;
      cursor = body.next_cursor;
      if (pages > 10) { check(false, "byte-capped paging terminated"); break; }
    }
    check(pages > 1 && new Set(seen).size === 5,
          "the byte cap splits one KV list page into several responses without dropping a message");
  }

  {
    const ctx = await fixture();
    const forged = b64url(enc.encode(JSON.stringify({ m: "someone.else@collie.run", a: "" })));
    const r = await get(ctx, "/mail-page?cursor=" + forged);
    check(r.status === 400, "a cursor minted for another mailbox is refused, not followed");
    for (const bad of ["not!base64", "a".repeat(3000), b64url(enc.encode("{broken"))]) {
      const res = await get(ctx, "/mail-page?cursor=" + encodeURIComponent(bad));
      check(res.status === 400, `a malformed cursor (${bad.slice(0, 12)}…) is refused`);
    }
  }

  {
    // The old endpoint keeps working, including for rows written before ids existed.
    const ctx = await fixture();
    await ctx.MAIL.put(`m:${ctx.address}:1700000001:legacy01`,
                       JSON.stringify({ at: 1700000001, env: { ct: "old" } }));
    const r = await get(ctx, "/mail?since=0");
    const body = await r.json();
    check(r.status === 200 && body.messages.length === 1 && body.messages[0].env.ct === "old",
          "GET /mail still answers in its original shape");
    check(body.messages[0].id === "legacy01",
          "and a row written before ids existed derives the same id from its KV key");
  }

  console.log("── capabilities, rate guards and the claim path ──");
  {
    const ctx = await fixture();
    const caps = await (await worker.fetch(
      new Request("https://mail.collie.run/capabilities"), ctx.env)).json();
    check(caps.version === _limits.VERSION && caps.send.configured === true &&
          caps.send.binding === true && caps.send.ledger === true &&
          caps.receive.max_bytes === _limits.MAX_MAIL_BYTES,
          "/capabilities reports the bindings that are actually configured");
    check(/not about permission|may still refuse/.test(caps.send.note),
          "and says plainly that configured is not the same as allowed to send");

    const bare = await fixture({ mailerBinding: false });
    const off = await (await worker.fetch(
      new Request("https://mail.collie.run/capabilities"), bare.env)).json();
    check(off.send.binding === false && off.send.configured === false,
          "with no send binding it says so instead of claiming a capability it lacks");
    check((await postSend(bare, note())).status === 501,
          "and /send answers 501 rather than pretending to have queued anything");
  }

  {
    const ctx = await fixture();
    const codes = [];
    for (let i = 0; i < 5; i++) {
      const r = await postSend(ctx, note({ id: "req-rate-" + i }));
      codes.push(r.status);
    }
    const limited = await postSend(ctx, note({ id: "req-rate-over" }));
    check(codes.every((c) => c === 200) && limited.status === 429 &&
          ctx.mailer.calls.length === _limits.PER_MINUTE,
          `a mailbox is capped at ${_limits.PER_MINUTE} sends a minute, enforced in the ledger`);
    const known = await postSend(ctx, note({ id: "req-rate-0" }));
    check(known.status === 200 && ctx.mailer.calls.length === _limits.PER_MINUTE,
          "but an already-known id still gets its receipt while the mailbox is over quota");
  }

  {
    const ctx = await fixture();
    const claim = (body) => worker.fetch(new Request("https://mail.collie.run/handle/claim", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(body) }), ctx.env);
    const injected = await claim({ handle: "newname", email: "a@b.com\r\nBcc: evil@x.com",
                                   pub: b64(ctx.handle.pub) });
    check(injected.status === 400 && ctx.mailer.calls.length === 0,
          "an email address carrying a header break never reaches the composer");
    check((await claim({ handle: "newname", email: "a@b.com", pub: "short" })).status === 400,
          "and a public key that is not 32 bytes of X25519 is refused");
    const huge = await worker.fetch(new Request("https://mail.collie.run/handle/claim", {
      method: "POST", body: "{" + " ".repeat(64 * 1024) + "}" }), ctx.env);
    check(huge.status === 413, "a claim body past the JSON bound is refused before it is parsed");

    const seen = new Set();
    for (let i = 0; i < 24; i++) {
      // A distinct address each time: one address is now capped per hour, which is the point of
      // the per-recipient guard and would otherwise cut this loop short at ten.
      await claim({ handle: "name" + i, email: `a${i}@b.com`, pub: b64(ctx.handle.pub) });
      seen.add(JSON.parse(ctx.DIRECTORY.m.get("handle:name" + i)).code);
    }
    check([...seen].every((c) => /^[0-9]{6}$/.test(c)) && seen.size >= 22,
          "verification codes are six crypto-random digits, not Math.random");
  }

  console.log("── identity: a name is bound once, and not by whoever wrote last ──");
  {
    // The race the old code lost: both callers read "free" from KV, both wrote, and the name ended
    // up bound to whichever write arrived second. Here the two claims are genuinely concurrent —
    // the ledger's KV import awaits, so both are inside the object before either decides.
    const ctx = await fixture();
    const [k1, k2] = [await keypair(), await keypair()];
    const [r1, r2] = await Promise.all([
      post(ctx, "/handle/claim", { handle: "contested", email: "first@example.com",
                                   pub: b64(k1.pub) }),
      post(ctx, "/handle/claim", { handle: "contested", email: "second@example.com",
                                   pub: b64(k2.pub) }),
    ]);
    const statuses = [r1.status, r2.status].sort();
    check(statuses[0] === 200 && statuses[1] === 409,
          "two concurrent claims for one handle: one wins, the other is told it conflicts");
    const won = r1.status === 200 ? k1 : k2;
    const lost = r1.status === 200 ? k2 : k1;
    const stored = JSON.parse(ctx.DIRECTORY.m.get("handle:contested"));
    check(stored.pub === b64(won.pub) && stored.pub !== b64(lost.pub),
          "the name stays bound to the winner's key — the loser never overwrites the identity");
    check(ctx.mailer.calls.length === 1,
          "and exactly one verification code left the Worker, to one address");
    const hijack = await post(ctx, "/handle/verify",
                              { handle: "contested", pub: b64(lost.pub), code: stored.code });
    check(hijack.status === 401 &&
          JSON.parse(ctx.DIRECTORY.m.get("handle:contested")).verified !== true,
          "the loser cannot verify the winner's claim even holding the code that was mailed");
  }

  {
    const ctx = await fixture();
    const address = "bramble.daming@collie.run";
    const [d1, d2] = [await keypair(), await keypair()];
    const [r1, r2] = await Promise.all([
      post(ctx, "/dog/claim", { address, handle: "daming", pub: b64(d1.pub),
                                cert: await cert(ctx, address, d1.pub) }),
      post(ctx, "/dog/claim", { address, handle: "daming", pub: b64(d2.pub),
                                cert: await cert(ctx, address, d2.pub) }),
    ]);
    const statuses = [r1.status, r2.status].sort();
    const won = r1.status === 200 ? d1 : d2;
    check(statuses[0] === 200 && statuses[1] === 409,
          "two concurrent claims for one dog address: exactly one of them takes it");
    check(JSON.parse(ctx.DIRECTORY.m.get("dog:" + address)).pub === b64(won.pub),
          "and the address answers to the winner's key, not to the last writer's");
    const again = await post(ctx, "/dog/claim", { address, handle: "daming", pub: b64(won.pub),
                                                  cert: await cert(ctx, address, won.pub) });
    check(again.status === 200 &&
          JSON.parse(ctx.DIRECTORY.m.get("dog:" + address)).pub === b64(won.pub),
          "re-claiming with the SAME key is idempotent rather than a conflict");
  }

  {
    // Migration: identities that exist in KV from before the ledger are adopted, not treated as
    // free. This fixture's handle and dog rows were written straight to KV, as the old path did.
    const ctx = await fixture();
    const intruder = await keypair();
    const steal = await post(ctx, "/handle/claim", { handle: "daming", email: "thief@example.com",
                                                     pub: b64(intruder.pub) });
    const row = JSON.parse(ctx.DIRECTORY.m.get("handle:daming"));
    check(steal.status === 409 && row.pub === b64(ctx.handle.pub) &&
          row.email === "owner@example.com" && ctx.mailer.calls.length === 0,
          "a pre-existing verified handle cannot be re-claimed by another key, and mails nothing");
    const repeat = await post(ctx, "/handle/claim", { handle: "daming", email: "owner@example.com",
                                                      pub: b64(ctx.handle.pub) });
    const repeatBody = await repeat.json();
    check(repeat.status === 200 && repeatBody.sent === false && repeatBody.verified === true &&
          ctx.mailer.calls.length === 0,
          "while the owner's own exact re-claim is idempotent and sends no second code");
    const takeover = await post(ctx, "/dog/claim",
      { address: ctx.address, handle: "daming", pub: b64(intruder.pub),
        cert: await cert(ctx, ctx.address, intruder.pub) });
    check(takeover.status === 409 &&
          JSON.parse(ctx.DIRECTORY.m.get("dog:" + ctx.address)).pub === b64(ctx.dog.pub),
          "a dog address claimed before the ledger existed keeps its key too");
    check((await get(ctx, "/mail")).status === 200,
          "and the original dog's stamp still authenticates afterwards");
  }

  console.log("── identity: the code is a secret with a budget ──");
  {
    const ctx = await fixture();
    const key = await keypair();
    await post(ctx, "/handle/claim", { handle: "guessme", email: "who@example.com",
                                       pub: b64(key.pub) });
    const real = JSON.parse(ctx.DIRECTORY.m.get("handle:guessme")).code;
    const tries = [];
    for (let i = 0; i < _limits.CODE_MAX_ATTEMPTS; i++)
      tries.push((await post(ctx, "/handle/verify",
                             { handle: "guessme", pub: b64(key.pub), code: "00000" + i })).status);
    check(tries.every((s) => s === 401),
          `${_limits.CODE_MAX_ATTEMPTS} wrong codes are each refused`);
    const locked = await post(ctx, "/handle/verify",
                              { handle: "guessme", pub: b64(key.pub), code: "999999" });
    check(locked.status === 429, "and the next guess is locked out rather than answered");
    const withReal = await post(ctx, "/handle/verify",
                                { handle: "guessme", pub: b64(key.pub), code: real });
    check(withReal.status === 429 &&
          JSON.parse(ctx.DIRECTORY.m.get("handle:guessme")).verified !== true,
          "the burnt claim stays burnt even for the code that was actually mailed");
  }

  {
    const ctx = await fixture();
    const key = await keypair();
    await post(ctx, "/handle/claim", { handle: "steady", email: "who@example.com",
                                       pub: b64(key.pub) });
    const code = JSON.parse(ctx.DIRECTORY.m.get("handle:steady")).code;
    const wrongKey = await post(ctx, "/handle/verify",
                                { handle: "steady", pub: b64((await keypair()).pub), code });
    check(wrongKey.status === 401, "a correct code presented by a different key is refused");
    const ok = await post(ctx, "/handle/verify", { handle: "steady", pub: b64(key.pub), code });
    const stored = JSON.parse(ctx.DIRECTORY.m.get("handle:steady"));
    check(ok.status === 200 && stored.verified === true && stored.code === undefined,
          "the right key verifies, and the verified row keeps its old shape with no code in it");
    const replay = await post(ctx, "/handle/verify", { handle: "steady", pub: b64(key.pub), code });
    check(replay.status === 200 && (await replay.json()).ok === true,
          "repeating the verify that already succeeded is idempotent, not a failure");
    const other = await post(ctx, "/handle/verify",
                             { handle: "steady", pub: b64((await keypair()).pub), code });
    check(other.status === 401, "but a different key replaying that code gets nothing");
  }

  {
    // A duplicate claim storm must not become a mail storm, and a resend must not invalidate the
    // code already sitting in the owner's inbox.
    const ctx = await fixture();
    const key = await keypair();
    const body = { handle: "patient", email: "who@example.com", pub: b64(key.pub) };
    const burst = await Promise.all([...Array(8)].map(() => post(ctx, "/handle/claim", body)));
    check(ctx.mailer.calls.length === 1 && burst.every((r) => r.status === 200),
          "eight simultaneous claims for one handle send exactly one code and no errors");
    const first = JSON.parse(ctx.DIRECTORY.m.get("handle:patient")).code;
    const cooled = await post(ctx, "/handle/claim", body);
    const cooledBody = await cooled.json();
    check(cooled.status === 200 && cooledBody.sent === false && cooledBody.retry_after > 0 &&
          ctx.mailer.calls.length === 1,
          "a claim inside the cooldown is answered without sending a second code");

    await travel(_limits.CODE_COOLDOWN + 1, async () => {
      const resent = await post(ctx, "/handle/claim", body);
      check(resent.status === 200 && (await resent.json()).sent === true &&
            ctx.mailer.calls.length === 2,
            "past the cooldown the same claim may ask again");
      check(JSON.parse(ctx.DIRECTORY.m.get("handle:patient")).code === first,
            "and the resent code is the SAME one — the message already delivered still works");
    });

    let sends = 2;
    for (let i = 2; i < _limits.CODE_MAX_SENDS + 2; i++) {
      await travel((_limits.CODE_COOLDOWN + 1) * i, async () => {
        const r = await post(ctx, "/handle/claim", body);
        if (r.status === 200 && (await r.json()).sent === true) sends += 1;
        else check(r.status === 429, "an exhausted claim is refused with 429, not mailed");
      });
    }
    check(sends === _limits.CODE_MAX_SENDS && ctx.mailer.calls.length === _limits.CODE_MAX_SENDS,
          `one pending claim is worth at most ${_limits.CODE_MAX_SENDS} codes, ever`);

    await travel(_limits.CLAIM_TTL + 5, async () => {
      const late = await post(ctx, "/handle/verify",
                              { handle: "patient", pub: b64(key.pub), code: first });
      check(late.status === 401, "and a code presented after the claim window is refused");
    });
  }

  {
    // The other direction: one address, many free names. Without a per-recipient budget a stranger
    // is mailed a code for every name the attacker can think of.
    const ctx = await fixture();
    const key = await keypair();
    const victim = "victim@example.com";
    const statuses = [];
    for (let i = 0; i < _limits.EMAIL_PER_HOUR + 3; i++)
      statuses.push((await post(ctx, "/handle/claim",
                                { handle: "spam" + i, email: victim, pub: b64(key.pub) })).status);
    check(ctx.mailer.calls.length === _limits.EMAIL_PER_HOUR &&
          statuses.filter((s) => s === 429).length === 3,
          `one address receives at most ${_limits.EMAIL_PER_HOUR} codes an hour across all handles`);
    check(ctx.mailer.calls.every((m) => m.to === victim),
          "and every one of them went to that address and nowhere else");
  }

  {
    const ctx = await fixture({ claimsBinding: false });
    const key = await keypair();
    const claim = await post(ctx, "/handle/claim", { handle: "orphan", email: "a@b.com",
                                                     pub: b64(key.pub) });
    const verify = await post(ctx, "/handle/verify", { handle: "orphan", pub: b64(key.pub),
                                                       code: "123456" });
    const dog = await post(ctx, "/dog/claim", { address: "x.daming@collie.run", handle: "daming",
                                                pub: b64(key.pub), cert: "" });
    check([claim.status, verify.status, dog.status].every((s) => s === 503),
          "with no identity ledger bound, claiming is refused outright");
    check(/CLAIMS/.test((await claim.json()).error) && ctx.mailer.calls.length === 0 &&
          !ctx.DIRECTORY.m.has("handle:orphan"),
          "the error names the missing binding, and nothing was written or mailed on the old path");
    const caps = await (await worker.fetch(
      new Request("https://mail.collie.run/capabilities"), ctx.env)).json();
    check(caps.identity.ledger === false,
          "and /capabilities says the identity ledger is absent instead of implying it is safe");
  }

  console.log("── an old request id cannot be pointed at a new owner ──");
  {
    const ctx = await fixture();
    const first = await postSend(ctx, note({ id: "req-owner-1" }));
    check(first.status === 200 && ctx.mailer.calls.length === 1, "a send to the owner goes out");
    // The owner changes. The receipt for the old id is still in the ledger under its digest.
    await ctx.DIRECTORY.put("handle:daming", JSON.stringify(
      { pub: b64(ctx.handle.pub), email: "new.owner@example.com", verified: true }));
    const stale = await postSend(ctx, note({ id: "req-owner-1" }));
    check(stale.status === 400 && ctx.mailer.calls.length === 1,
          "replaying the old body afterwards is refused: its destination is no longer the owner");
    const repointed = await postSend(ctx, note({ id: "req-owner-1", to: "new.owner@example.com" }));
    const body = await repointed.json();
    check(repointed.status === 409 && ctx.mailer.calls.length === 1 &&
          !JSON.stringify(body).includes("owner@example.com"),
          "and reusing that id with a new destination is a conflict that sends and reveals nothing");
  }

  {
    const ctx = await fixture();
    const pub = await (await worker.fetch(new Request("https://mail.collie.run/pubkey"),
                                          ctx.env)).json();
    check(Object.keys(pub).length === 1 && pub.pub === b64(ctx.relay.pub),
          "/pubkey still answers in its original one-field shape");
  }

  console.log();
  console.log(failures.length
    ? `  ${failures.length} FAILED:\n    ` + failures.join("\n    ")
    : "  mail relay: all green");
  return failures.length ? 1 : 0;
}

process.exit(await main());
