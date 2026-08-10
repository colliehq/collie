/**
 * relay/worker.js — push registration and the APNs bearer token.
 *
 * Two things are worth testing without Apple in the loop. First, who is allowed to register a device
 * token: a phone that is not paired must not be able to attach itself to someone else's desktop and
 * receive their notifications. Second, the JWT — it is signed with WebCrypto and Apple's rules for it
 * are unforgiving (ES256, a `kid` header, an `iss` claim, and a token that must be reused rather than
 * re-minted, because APNs throttles providers that mint one per push).
 *
 * `fetch` is stubbed, so nothing here talks to Apple; what is checked is the request that WOULD go.
 *
 *   node tests/relay_push_test.js
 */
import { webcrypto } from "node:crypto";
import { generateKeyPairSync } from "node:crypto";
import { RelayRoom } from "../relay/worker.js";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const fails = [];
function check(cond, msg) {
  console.log((cond ? "  PASS " : "  FAIL ") + msg);
  if (!cond) fails.push(msg);
}

/** A real P-256 key in PKCS8 PEM, the shape Apple hands out as AuthKey_XXXX.p8. */
function p8() {
  const { privateKey, publicKey } = generateKeyPairSync("ec", {
    namedCurve: "prime256v1",
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
    publicKeyEncoding: { type: "spki", format: "pem" },
  });
  return { privateKey, publicKey };
}

/** Does this JWT actually verify? A 64-byte blob of the right shape is not the same as a signature. */
async function jwtVerifies(jwt, spkiPem) {
  const [h, p, sig] = jwt.split(".");
  const der = Uint8Array.from(
    Buffer.from(spkiPem.replace(/-----[A-Z ]+-----/g, "").replace(/\s+/g, ""), "base64"));
  const key = await crypto.subtle.importKey("spki", der.buffer,
    { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
  const raw = Uint8Array.from(
    Buffer.from(sig.replace(/-/g, "+").replace(/_/g, "/"), "base64"));
  return crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, raw,
                              new TextEncoder().encode(h + "." + p));
}

function fakeStorage() {
  const m = new Map();
  return {
    m,
    async get(k) { return m.get(k); },
    async put(k, v) { m.set(k, v); },
    async delete(k) { for (const key of Array.isArray(k) ? k : [k]) m.delete(key); },
    async list({ prefix }) { return new Map([...m].filter(([k]) => k.startsWith(prefix))); },
  };
}

async function sha256hex(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

/** A room whose desktop has already paired one device, holding session token `sess`. */
async function room(env, sess = "session-token-abc") {
  const sent = [];
  let att = { paircode: "X", devices: [await sha256hex(sess)], approve: true };
  const agent = {
    sent,
    send: (s) => sent.push(JSON.parse(s)),
    serializeAttachment: (s) => { att = s; },
    deserializeAttachment: () => att,
  };
  const storage = fakeStorage();
  const r = new RelayRoom({ storage, getWebSockets: () => [agent], acceptWebSocket: () => {} }, env);
  return { r, agent, storage, sess };
}

const registerRequest = (token, auth, extra = {}) =>
  new Request("https://relay/r/room/push/register", {
    method: "POST",
    headers: auth ? { authorization: "Bearer " + auth, "content-type": "application/json" }
                  : { "content-type": "application/json" },
    body: JSON.stringify({ token, ...extra }),
  });
const revokeRequest = (auth) => new Request("https://relay/r/room/device/revoke", {
  method: "POST", headers: { authorization: "Bearer " + auth },
});
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

const HEX = "a".repeat(64);

async function main() {
  const { privateKey, publicKey } = p8();
  const env = { APNS_KEY: privateKey, APNS_KEY_ID: "ABC1234567", APNS_TEAM_ID: "58Y98W3QQK",
                APNS_TOPIC: "com.wudaming00.collie-ios" };

  // ---- who may register -------------------------------------------------------------------------
  {
    const { r, sess } = await room(env);
    const ok = await r.registerPush(registerRequest(HEX, sess));
    check(ok.status === 200, "a paired device can register for pushes");

    const stranger = await r.registerPush(registerRequest(HEX, "not-a-session"));
    check(stranger.status === 401,
          "an unpaired device cannot attach itself to this desktop's notifications");

    const anon = await r.registerPush(registerRequest(HEX, null));
    check(anon.status === 401, "and neither can one with no session at all");

    const junk = await r.registerPush(registerRequest("not-hex", sess));
    check(junk.status === 400, "a device token that is not a device token is refused");

    const oversized = await r.registerPush(new Request("https://relay/r/room/push/register", {
      method: "POST",
      headers: { authorization: "Bearer " + sess, "content-type": "application/json" },
      body: JSON.stringify({ token: HEX, padding: "x".repeat(9 * 1024) }),
    }));
    check(oversized.status === 400, "an oversized push-registration body is bounded before parsing");
  }

  // ---- what gets sent to Apple ------------------------------------------------------------------
  {
    const { r, sess, storage } = await room(env);
    await r.registerPush(registerRequest(HEX, sess, { name: "iPhone" }));
    await storage.put("push:" + "f".repeat(64), {
      token: "b".repeat(64), sandbox: false, name: "stale", at: Date.now(),
    });
    check([...storage.m.keys()].some((k) => k.startsWith("push:")), "the token is stored");

    const seen = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = async (url, init) => { seen.push({ url, init }); return { status: 200 }; };
    await r.pushAll({ title: "Run finished", body: "all green", session: "s1" });
    globalThis.fetch = realFetch;

    check(seen.length === 1, "one registered phone gets one request");
    check(!storage.m.has("push:" + "f".repeat(64)),
          "push registrations whose bearer is no longer paired are garbage-collected");
    const req = seen[0];
    check(req.url === "https://api.push.apple.com/3/device/" + HEX,
          "production gateway and the device token in the path");
    check(req.init.headers["apns-topic"] === env.APNS_TOPIC, "apns-topic is the bundle id");
    check(req.init.headers["apns-push-type"] === "alert", "declared as an alert");
    const payload = JSON.parse(req.init.body);
    check(payload.aps.alert.title === "Run finished" && payload.aps.alert.body === "all green",
          "the alert carries the desktop's words");
    check(payload.session === "s1", "and the session, so a tap can open the right run");

    // ---- the JWT --------------------------------------------------------------------------------
    const jwt = String(req.init.headers.authorization).replace(/^bearer /, "");
    const [h, p, sig] = jwt.split(".");
    const dec = (s) => JSON.parse(Buffer.from(s.replace(/-/g, "+").replace(/_/g, "/"), "base64"));
    check(dec(h).alg === "ES256" && dec(h).kid === env.APNS_KEY_ID,
          "header names ES256 and the key id");
    check(dec(p).iss === env.APNS_TEAM_ID && typeof dec(p).iat === "number",
          "payload carries the team and an issued-at");
    check(Buffer.from(sig.replace(/-/g, "+").replace(/_/g, "/"), "base64").length === 64,
          "the signature is a raw 64-byte r‖s pair, not DER");
    check(await jwtVerifies(jwt, publicKey),
          "and it VERIFIES against the key it claims to be signed with");

    // Reused, not re-minted: APNs rejects a provider that signs a fresh token per push.
    globalThis.fetch = async (url, init) => { seen.push({ url, init }); return { status: 200 }; };
    await r.pushAll({ title: "again", body: "" });
    globalThis.fetch = realFetch;
    check(seen[1].init.headers.authorization === req.init.headers.authorization,
          "the bearer token is reused across pushes rather than re-signed each time");
  }

  // ---- a transient desktop disconnect must not erase durable registrations ----------------------
  {
    const { r, sess, storage } = await room(env);
    await r.registerPush(registerRequest(HEX, sess));
    const key = "push:" + await sha256hex(sess);
    r.state.getWebSockets = () => [];
    let called = false;
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () => { called = true; return { status: 200 }; };
    await r.pushAll({ title: "while offline", body: "keep registration" });
    globalThis.fetch = realFetch;
    check(storage.m.has(key) && !called,
          "a transient desktop disconnect keeps push registrations for the next reconnect");
  }

  // ---- revocation is a durable, acknowledged operation ------------------------------------------
  {
    const { r, agent, storage, sess } = await room(env);
    const pending = r.revokeDevice(revokeRequest(sess));
    for (let n = 0; n < 50 && !agent.sent.some((msg) => msg.t === "device_revoke"); n++) await tick();
    const command = agent.sent.find((msg) => msg.t === "device_revoke");
    await r.webSocketMessage(agent, JSON.stringify({
      t: "device_revoked", id: command.id, hash: command.hash, ok: true,
    }));
    const response = await pending;
    check(response.status === 200, "the phone sees revoke success only after the desktop ACK");
    check(storage.m.get("revoke:" + command.hash).state === "acked",
          "the ACK is retained so a lost HTTP response can be retried idempotently");
    check(!(await r.checkSession(revokeRequest(sess), agent)),
          "the bearer is blocked at the edge as soon as revocation starts");
  }

  {
    const first = await room(env);
    first.agent.send = () => { throw new Error("socket dropped"); };
    const failed = await first.r.revokeDevice(revokeRequest(first.sess));
    const sessionHash = await sha256hex(first.sess);
    check(failed.status === 503 && first.storage.m.get("revoke:" + sessionHash).state === "pending",
          "a send/ACK failure returns 503 and leaves a durable pending tombstone");

    // Simulate a reconnect whose hello still contains the old desktop hash. The pending tombstone
    // filters it and is re-sent until an idempotent desktop deletion is acknowledged.
    let attachment = { protocol: 0, e2eRequired: false, devices: [sessionHash] };
    const sent = [];
    const agent = {
      send: (value) => sent.push(JSON.parse(value)),
      serializeAttachment: (value) => { attachment = value; },
      deserializeAttachment: () => attachment,
    };
    const woken = new RelayRoom({ storage: first.storage, getWebSockets: () => [agent],
                                  acceptWebSocket: () => {} }, env);
    await woken.webSocketMessage(agent, JSON.stringify({
      t: "hello", v: 2, e2eRequired: true, approve: true, devices: [sessionHash],
    }));
    const retry = sent.find((msg) => msg.t === "device_revoke");
    check(!agent.deserializeAttachment().devices.includes(sessionHash) && !!retry,
          "a reconnect cannot resurrect a tombstoned bearer and receives a retry command");
    await woken.webSocketMessage(agent, JSON.stringify({
      t: "device_revoked", id: retry.id, hash: retry.hash, ok: true,
    }));
    const idempotent = await woken.revokeDevice(revokeRequest(first.sess));
    check(idempotent.status === 200,
          "after reconciliation, the phone can retry and safely clear its local credential");
  }

  // ---- a device Apple says is gone --------------------------------------------------------------
  {
    const { r, sess, storage } = await room(env);
    await r.registerPush(registerRequest(HEX, sess));
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ status: 410 });      // "this install is finished with"
    await r.pushAll({ title: "x", body: "y" });
    globalThis.fetch = realFetch;
    check(![...storage.m.keys()].some((k) => k.startsWith("push:")),
          "a 410 from Apple drops the token instead of signing a request for it forever");
  }

  // ---- a sandbox build ---------------------------------------------------------------------------
  {
    const { r, sess } = await room(env);
    await r.registerPush(registerRequest(HEX, sess, { sandbox: true }));
    const seen = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = async (url, init) => { seen.push(url); return { status: 200 }; };
    await r.pushAll({ title: "x", body: "y" });
    globalThis.fetch = realFetch;
    check(String(seen[0]).startsWith("https://api.sandbox.push.apple.com/"),
          "a debug build is pushed through the sandbox gateway, not production");
  }

  // ---- no key configured --------------------------------------------------------------------------
  {
    const { r, sess } = await room({});                    // relay deployed without APNs secrets
    await r.registerPush(registerRequest(HEX, sess));
    let called = false;
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () => { called = true; return { status: 200 }; };
    await r.pushAll({ title: "x", body: "y" });
    globalThis.fetch = realFetch;
    check(!called, "with no APNs key configured, nothing is sent and nothing throws");
  }

  // ---- the desktop's socket message reaches the fan-out ------------------------------------------
  {
    const { r, agent, sess } = await room(env);
    await r.registerPush(registerRequest(HEX, sess));
    const seen = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = async (url, init) => { seen.push(JSON.parse(init.body)); return { status: 200 }; };
    await r.webSocketMessage(agent, JSON.stringify({ t: "notify", title: "Run failed", body: "boom" }));
    await new Promise((r2) => setTimeout(r2, 20));         // fire-and-forget by design
    globalThis.fetch = realFetch;
    check(seen.length === 1 && seen[0].aps.alert.title === "Collie" &&
          seen[0].aps.alert.body === "A run has an update on your desktop.",
          "desktop notify contents are replaced by a generic alert the relay can safely see");
  }

  console.log(fails.length ? "\n  " + fails.length + " FAILED" : "\n  relay push: all green");
  process.exit(fails.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
