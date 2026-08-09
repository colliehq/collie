/**
 * relay/worker.js — authenticated, leased dog presence.
 *
 * This drives the real PresencePack Durable Object class at its storage/WebSocket seams. It checks
 * the boundaries that matter after a listener crash: credentials fail closed, hello is required,
 * degraded is not advertised online, a replacement session fences every late frame from the old
 * socket, and an alarm removes a lease even when no graceful bye arrived.
 *
 *   node tests/relay_presence_test.js
 */
import { webcrypto } from "node:crypto";
import relayWorker, { PresencePack } from "../relay/worker.js";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const fails = [];
function check(cond, msg) {
  console.log((cond ? "  PASS " : "  FAIL ") + msg);
  if (!cond) fails.push(msg);
}

function fakeStorage() {
  const m = new Map();
  let alarm = null;
  return {
    m,
    async get(k) { return m.get(k); },
    async put(k, v) { m.set(k, v); },
    async delete(k) { for (const key of Array.isArray(k) ? k : [k]) m.delete(key); },
    async list({ prefix }) { return new Map([...m].filter(([k]) => k.startsWith(prefix))); },
    async transaction(fn) { return fn(this); },
    async getAlarm() { return alarm; },
    async setAlarm(at) { alarm = at; },
    async deleteAlarm() { alarm = null; },
    alarm: () => alarm,
  };
}

class FakeSocket {
  constructor() {
    this.sent = [];
    this.closed = [];
    this.attachment = {};
  }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  close(code, reason) { this.closed.push({ code, reason }); }
  serializeAttachment(v) { this.attachment = structuredClone(v); }
  deserializeAttachment() { return structuredClone(this.attachment); }
}

function fakeState() {
  const storage = fakeStorage();
  const sockets = [];
  return {
    storage,
    sockets,
    acceptWebSocket(ws, tags) { ws.tags = tags; sockets.push(ws); },
    getWebSockets(tag) { return sockets.filter((ws) => !tag || (ws.tags || []).includes(tag)); },
  };
}

const auth = (token) => ({ authorization: "Bearer " + token });
const url = (path, pack = "workspace.channel", dog = "Cornetto", session = "") =>
  "https://relay" + path + "?pack=" + encodeURIComponent(pack) +
  "&dog=" + encodeURIComponent(dog) + (session ? "&session=" + encodeURIComponent(session) : "");

async function enroll(room, admin, dog = "Cornetto", pack = "workspace.channel") {
  const r = await room.fetch(new Request(url("/presence/enroll", pack, dog), {
    method: "POST", headers: auth(admin),
  }));
  return { status: r.status, body: await r.json() };
}

async function unenroll(room, admin, dog = "Cornetto", pack = "workspace.channel") {
  const r = await room.fetch(new Request(url("/presence/enroll", pack, dog), {
    method: "DELETE", headers: auth(admin),
  }));
  return { status: r.status, body: await r.json() };
}

async function status(room, credential, dog = "Cornetto", pack = "workspace.channel") {
  const r = await room.fetch(new Request(url("/presence/status", pack, dog), {
    headers: auth(credential),
  }));
  return { status: r.status, body: await r.json() };
}

/** Node's WHATWG Response rejects 101; Cloudflare's Response accepts its `webSocket` extension. */
async function connect(room, state, credential, session, dog = "Cornetto",
                       pack = "workspace.channel") {
  const NativeResponse = globalThis.Response;
  const NativePair = globalThis.WebSocketPair;
  let client, server;
  globalThis.WebSocketPair = class {
    constructor() { client = new FakeSocket(); server = new FakeSocket(); this[0] = client; this[1] = server; }
  };
  globalThis.Response = class {
    constructor(body, init = {}) {
      this.body = body; this.status = init.status || 200; this.webSocket = init.webSocket;
    }
  };
  try {
    const response = await room.fetch(new Request(url("/presence/ws", pack, dog, session), {
      headers: { ...auth(credential), upgrade: "websocket" },
    }));
    return { response, client, server, accepted: state.sockets.includes(server) };
  } finally {
    globalThis.Response = NativeResponse;
    globalThis.WebSocketPair = NativePair;
  }
}

const frame = (t, session, health, seq, dog = "Cornetto", pack = "workspace.channel") => {
  const d = { t, v: 1, pack, dog, session };
  if (health !== undefined) d.health = health;
  if (seq !== undefined) d.seq = seq;
  return JSON.stringify(d);
};

async function main() {
  const ADMIN = "operator-secret-with-enough-entropy";
  const state = fakeState();
  const room = new PresencePack(state, { PRESENCE_ADMIN_TOKEN: ADMIN });

  // ---- provisioning is explicit and fails closed -----------------------------------------------
  {
    const closed = new PresencePack(fakeState(), {});
    const r = await enroll(closed, ADMIN);
    check(r.status === 503, "enrollment refuses to run when the Worker admin secret is absent");

    const bad = await enroll(room, "wrong-admin");
    check(bad.status === 401, "an invalid operator credential cannot enroll a dog");

    const one = await enroll(room, ADMIN);
    check(one.status === 200 && typeof one.body.credential === "string" &&
          one.body.credential.length >= 40, "enrollment mints a high-entropy per-dog credential");
    check(!JSON.stringify(state.storage.m.get("auth:Cornetto")).includes(one.body.credential),
          "only a credential digest is stored");

    const two = await enroll(room, ADMIN, "BigMac");
    check(two.status === 200 && two.body.credential !== one.body.credential,
          "each dog receives an independent credential");
    room._testCredential = one.body.credential;
    room._testPeerCredential = two.body.credential;
  }
  const credential = room._testCredential;

  // ---- connect authenticates before allocating a WebSocket; hello creates the lease ------------
  {
    const denied = await room.fetch(new Request(url("/presence/ws", "workspace.channel",
      "Cornetto", "session-bad"), { headers: { ...auth("wrong"), upgrade: "websocket" } }));
    check(denied.status === 401, "a bad runtime bearer is rejected before WebSocket acceptance");

    const c = await connect(room, state, credential, "session-one");
    check(c.response.status === 101 && c.accepted, "an enrolled dog can open a presence socket");
    let s = await status(room, credential);
    check(s.body.dogs.find((d) => d.dog === "Cornetto").online === false,
          "an HTTP upgrade alone is not online before the matching hello");

    await room.webSocketMessage(c.server, frame("hello", "session-one", "ok"));
    s = await status(room, credential);
    const dog = s.body.dogs.find((d) => d.dog === "Cornetto");
    check(dog.online === true && dog.connected === true && dog.health === "ok",
          "a valid hello establishes an online lease");
    check(c.server.sent.some((m) => m.t === "hello_ack" && m.lease_ms === 75000),
          "the server tells the client the lease duration");
    check(state.storage.alarm() !== null, "an active lease schedules a Durable Object alarm");

    await room.webSocketMessage(c.server, frame("heartbeat", "session-one", "degraded", 1));
    s = await status(room, credential);
    const degraded = s.body.dogs.find((d) => d.dog === "Cornetto");
    check(degraded.connected === true && degraded.online === false && degraded.health === "degraded",
          "a live-but-degraded process is not advertised as Slack-ready");
    room._oldSocket = c.server;
  }

  // ---- a newer connection fences late heartbeat and bye frames from the old owner ---------------
  {
    const newer = await connect(room, state, credential, "session-two");
    await room.webSocketMessage(newer.server, frame("hello", "session-two", "ok"));
    check(room._oldSocket.closed.some((x) => x.code === 4004),
          "a new session actively closes the previous socket");

    await room.webSocketMessage(room._oldSocket,
      frame("heartbeat", "session-one", "degraded", 2));
    await room.webSocketMessage(room._oldSocket, frame("bye", "session-one"));
    let s = await status(room, credential);
    check(s.body.dogs.find((d) => d.dog === "Cornetto").online === true,
          "late frames from the stale session cannot renew or erase the replacement lease");

    // Sequence fencing is per active owner too: replaying an acked heartbeat closes the socket and
    // does not extend its lease.
    await room.webSocketMessage(newer.server, frame("heartbeat", "session-two", "ok", 1));
    await room.webSocketMessage(newer.server, frame("heartbeat", "session-two", "ok", 1));
    check(newer.server.closed.some((x) => x.code === 4004) &&
          !state.storage.m.has("lease:Cornetto"),
          "a duplicate heartbeat sequence is fenced and drops its no-longer-trustworthy lease");

    // Drive an explicit bye through a fresh owner; the duplicate test intentionally closed newer.
    const final = await connect(room, state, credential, "session-three");
    await room.webSocketMessage(final.server, frame("hello", "session-three", "ok"));
    await room.webSocketMessage(final.server, frame("bye", "session-three"));
    s = await status(room, credential);
    check(s.body.dogs.find((d) => d.dog === "Cornetto").health === "offline",
          "a matching graceful bye removes the lease immediately");
  }

  // ---- crash/power loss: the alarm expires the durable lease without a client frame --------------
  {
    const c = await connect(room, state, credential, "session-four");
    await room.webSocketMessage(c.server, frame("hello", "session-four", "ok"));
    const lease = state.storage.m.get("lease:Cornetto");
    lease.expiresAt = Date.now() - 1;
    state.storage.m.set("lease:Cornetto", lease);
    await room.alarm();
    const s = await status(room, credential);
    const dog = s.body.dogs.find((d) => d.dog === "Cornetto");
    check(dog.online === false && dog.connected === false && dog.health === "offline",
          "alarm expiry makes a crashed or powered-off dog offline");
    check(c.server.closed.some((x) => x.code === 4001), "expiry also closes the stale edge socket");
  }

  // ---- status is authenticated and intentionally contains no operational payload ----------------
  {
    const unauth = await room.fetch(new Request(url("/presence/status")));
    check(unauth.status === 401, "pack status is not public");
    const s = await status(room, credential);
    const raw = JSON.stringify(s.body);
    check(s.body.dogs.length === 2 && raw.includes("BigMac"),
          "an authenticated dog sees the small roster for its own pack");
    check(!/credential|session|owner|token|task|machine/i.test(raw),
          "status returns no credentials, session ids, owners, machines or task data");
  }

  // ---- credential rotation fences a socket that authenticated but has not said hello yet --------
  {
    const first = await enroll(room, ADMIN, "Rotator");
    const pending = await connect(room, state, first.body.credential, "rotate-old", "Rotator");
    const second = await enroll(room, ADMIN, "Rotator");
    await room.webSocketMessage(pending.server,
      frame("hello", "rotate-old", "ok", undefined, "Rotator"));
    const oldStatus = await status(room, first.body.credential, "Rotator");
    const newStatus = await status(room, second.body.credential, "Rotator");
    check(oldStatus.status === 401 && newStatus.status === 200 &&
          newStatus.body.dogs.find((d) => d.dog === "Rotator").online === false,
          "rotation invalidates the old token and a delayed hello cannot resurrect its lease");
    check(pending.server.closed.some((x) => x.code === 4003),
          "rotation actively fences the pre-hello socket");
  }

  // ---- retirement removes both authority and the otherwise-permanent offline roster row --------
  {
    const added = await enroll(room, ADMIN, "Retired");
    const c = await connect(room, state, added.body.credential, "retired-session", "Retired");
    await room.webSocketMessage(c.server,
      frame("hello", "retired-session", "ok", undefined, "Retired"));
    const gone = await unenroll(room, ADMIN, "Retired");
    const again = await unenroll(room, ADMIN, "Retired");
    const old = await status(room, added.body.credential, "Retired");
    const roster = await status(room, credential);
    check(gone.status === 200 && again.status === 200 && gone.body.enrolled === false &&
          old.status === 401 && !roster.body.dogs.some((d) => d.dog === "Retired"),
          "admin unenroll is idempotent, revokes the token, and removes the roster row");
    check(c.server.closed.some((x) => x.code === 4003),
          "unenroll actively closes that dog's presence socket");
  }

  // ---- front door derives the DO from pack and checks admin auth before forwarding ----------------
  {
    let named = "", forwarded = 0;
    const env = {
      PRESENCE_ADMIN_TOKEN: ADMIN,
      PRESENCE: {
        idFromName(v) { named = v; return "id:" + v; },
        get() { return { fetch() { forwarded++; return new Response("ok"); } }; },
      },
    };
    const no = await relayWorker.fetch(new Request(url("/presence/enroll"), {
      method: "POST", headers: auth("wrong"),
    }), env);
    check(no.status === 401 && forwarded === 0,
          "the public front door fails admin auth before touching a Durable Object");
    const yes = await relayWorker.fetch(new Request(url("/presence/enroll"), {
      method: "POST", headers: auth(ADMIN),
    }), env);
    check(yes.status === 200 && named === "workspace.channel" && forwarded === 1,
          "the front door routes one strongly-consistent object by pack id");
    const del = await relayWorker.fetch(new Request(url("/presence/enroll"), {
      method: "DELETE", headers: auth(ADMIN),
    }), env);
    check(del.status === 200 && forwarded === 2,
          "the same authenticated front door forwards explicit unenrollment");
  }

  console.log(fails.length ? "\n  " + fails.length + " FAILED" : "\n  relay presence: all green");
  process.exitCode = fails.length ? 1 : 0;
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
