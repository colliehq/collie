/** Hosted v2 fixed sealed endpoint and opaque response sequencing. */
import { webcrypto } from "node:crypto";
import { RelayRoom } from "../relay/worker.js";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const failures = [];
function check(value, message) {
  console.log((value ? "  PASS " : "  FAIL ") + message);
  if (!value) failures.push(message);
}
async function hash(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((x) => x.toString(16).padStart(2, "0")).join("");
}
function storage() {
  const m = new Map();
  return {
    async get(k) { return m.get(k); }, async put(k, v) { m.set(k, v); },
    async delete(k) { for (const x of Array.isArray(k) ? k : [k]) m.delete(x); },
    async list({ prefix }) { return new Map([...m].filter(([k]) => k.startsWith(prefix))); },
  };
}
async function fixture() {
  const token = "device-session-token";
  const sent = [];
  let attachment = { protocol: 2, e2eRequired: true, approve: true, devices: [await hash(token)] };
  const agent = {
    sent, send: (value) => sent.push(JSON.parse(value)),
    deserializeAttachment: () => attachment,
    serializeAttachment: (value) => { attachment = value; },
  };
  const room = new RelayRoom({ storage: storage(), getWebSockets: () => [agent], acceptWebSocket: () => {} }, {});
  return { room, agent, token };
}
function request(token, extra = {}, body = null) {
  const headers = { authorization: `Bearer ${token}`, ...extra };
  return new Request("https://relay/r/room/sealed", { method: "POST", headers, body });
}
const envelope = JSON.stringify({ n: "bm9uY2U=", ct: "Y2lwaGVydGV4dA==" });
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

async function open(room, agent, token, id = crypto.randomUUID()) {
  const responsePromise = room.fetch(request(token, {
    "content-type": "application/octet-stream",
    "X-Collie-Rid": id, "X-Collie-Session": "opaque-session",
  }, envelope));
  for (let attempt = 0; attempt < 50 && agent.sent.length === 0; attempt++) await tick();
  return { responsePromise };
}

async function main() {
  {
    const { room, agent, token } = await fixture();
    const leaked = await room.fetch(new Request(
      "https://relay/r/room/api/stream?q=TOP-SECRET-PROMPT",
      { headers: { authorization: `Bearer ${token}` } }));
    check(leaked.status === 404 && agent.sent.length === 0,
          "real API paths and prompt-bearing query strings are never proxied");
    const anonymous = await room.fetch(request("wrong", {
      "X-Collie-Rid": "x", "X-Collie-Session": "s" }, envelope));
    check(anonymous.status === 401, "the fixed endpoint still requires a paired bearer token");
    const plaintext = await room.fetch(request(token, {
      "X-Collie-Enc": envelope, "X-Collie-Rid": "legacy", "X-Collie-Session": "s",
    }));
    check(plaintext.status === 415 && agent.sent.length === 0,
          "a legacy ciphertext header is not accepted as a body-less downgrade");
    const noSession = await room.fetch(request(token, {
      "content-type": "application/octet-stream", "X-Collie-Rid": "no-session",
    }, envelope));
    check(noSession.status === 400 && agent.sent.length === 0,
          "a sealed request without explicit session binding fails closed");
  }

  {
    const { room, agent, token } = await fixture();
    agent.send = () => { throw new Error("socket closed"); };
    const response = await room.fetch(request(token, {
      "content-type": "application/octet-stream",
      "X-Collie-Rid": crypto.randomUUID(), "X-Collie-Session": "s",
    }, envelope));
    check(response.status === 502 && room.pending.size === 0,
          "a dispatch-time desktop disconnect fails immediately instead of hanging");
  }

  {
    const { room, agent, token } = await fixture();
    const rid = crypto.randomUUID();
    const { responsePromise } = await open(room, agent, token, rid);
    const forwarded = agent.sent.at(-1);
    check(forwarded.t === "req" && forwarded.cid === rid && forwarded.enc === envelope,
          "a sealed envelope is forwarded intact");
    check(!("path" in forwarded) && !("method" in forwarded) && !("headers" in forwarded) &&
          !("body" in forwarded) && !JSON.stringify(forwarded).includes("prompt"),
          "the relay-to-desktop frame exposes no inner path, query, headers, body or prompt");

    await room.webSocketMessage(agent, JSON.stringify({
      t: "res", id: forwarded.id, status: 200, headers: { "content-type": "application/octet-stream" },
      enc: "sealed-head", seq: 0,
    }));
    const response = await responsePromise;
    await room.webSocketMessage(agent, JSON.stringify({ t: "chunk", id: forwarded.id, enc: "sealed-data", seq: 1 }));
    await room.webSocketMessage(agent, JSON.stringify({ t: "chunk", id: forwarded.id, enc: "sealed-terminal", seq: 2 }));
    await room.webSocketMessage(agent, JSON.stringify({ t: "end", id: forwarded.id }));
    const records = (await response.text()).trim().split("\n").map(JSON.parse);
    check(response.status === 200 && records.map((x) => x.seq).join(",") === "0,1,2",
          "opaque records stream through the fixed endpoint in exact contiguous order");
  }

  {
    const { room, agent, token } = await fixture();
    const { responsePromise } = await open(room, agent, token);
    const id = agent.sent.at(-1).id;
    await room.webSocketMessage(agent, JSON.stringify({ t: "res", id, enc: "head", seq: 0 }));
    const response = await responsePromise;
    await room.webSocketMessage(agent, JSON.stringify({ t: "chunk", id, enc: "gap", seq: 2 }));
    let rejected = false;
    try { await response.text(); } catch (error) { rejected = true; }
    check(rejected, "a response sequence gap errors the stream instead of hiding dropped records");
  }

  {
    const { room, agent, token } = await fixture();
    const { responsePromise } = await open(room, agent, token);
    const id = agent.sent.at(-1).id;
    await room.webSocketMessage(agent, JSON.stringify({ t: "res", id, enc: "head", seq: 0 }));
    const response = await responsePromise;
    await room.webSocketMessage(agent, JSON.stringify({ t: "chunk", id, enc: "one", seq: 1 }));
    await room.webSocketMessage(agent, JSON.stringify({ t: "chunk", id, enc: "duplicate", seq: 1 }));
    let rejected = false;
    try { await response.text(); } catch (error) { rejected = true; }
    check(rejected, "a duplicate response record errors the stream");
  }

  {
    const { room, agent, token } = await fixture();
    room.responseHeadMs = 10;
    const { responsePromise } = await open(room, agent, token);
    const response = await responsePromise;
    check(response.status === 502 && room.pending.size === 0 &&
          (await response.json()).error.includes("timed out"),
          "a desktop that never returns a response head releases the relay slot");
  }

  {
    const { room, agent, token } = await fixture();
    room.maxInflight = 1;
    const first = await open(room, agent, token);
    const overloaded = await room.fetch(request(token, {
      "content-type": "application/octet-stream",
      "X-Collie-Rid": crypto.randomUUID(), "X-Collie-Session": "opaque-session",
    }, envelope));
    check(overloaded.status === 429 && agent.sent.length === 1,
          "a room cannot allocate more than its bounded number of in-flight streams");
    const id = agent.sent[0].id;
    await room.webSocketMessage(agent, JSON.stringify({ t: "res", id, enc: "head", seq: 0 }));
    const response = await first.responsePromise;
    await response.body.cancel();
    check(room.pending.size === 0, "phone stream cancellation releases its relay slot");
  }

  console.log(failures.length ? `\n  ${failures.length} FAILED` : "\n  relay sealed transport: all green");
  process.exit(failures.length ? 1 : 0);
}

main().catch((error) => { console.error(error); process.exit(1); });
