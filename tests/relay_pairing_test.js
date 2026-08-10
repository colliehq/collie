/**
 * Hosted protocol v2 pairing contract. Run with:
 *
 *   node tests/relay_pairing_test.js
 */
import { webcrypto } from "node:crypto";
import { RelayRoom } from "../relay/worker.js";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const fails = [];
function check(condition, message) {
  console.log((condition ? "  PASS " : "  FAIL ") + message);
  if (!condition) fails.push(message);
}

function fakeStorage() {
  const m = new Map();
  let transactions = Promise.resolve();
  const api = {
    m,
    async get(key) { return m.get(key); },
    async put(key, value) { m.set(key, value); },
    async delete(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) m.delete(key); },
    async list({ prefix }) { return new Map([...m].filter(([key]) => key.startsWith(prefix))); },
  };
  api.transaction = (fn) => {
    const run = transactions.then(() => fn(api));
    transactions = run.catch(() => {});
    return run;
  };
  return api;
}

function fakeRoom(storage = fakeStorage()) {
  const sent = [];
  let attachment = { protocol: 2, e2eRequired: true, approve: true, devices: [], e2ePub: "desktop" };
  const agent = {
    sent,
    send: (value) => sent.push(JSON.parse(value)),
    serializeAttachment: (value) => { attachment = value; },
    deserializeAttachment: () => attachment,
  };
  const room = new RelayRoom({ storage, getWebSockets: () => [agent], acceptWebSocket: () => {} }, {});
  return { room, agent, storage };
}

const pairRequest = (body) => new Request("https://relay/r/room/pair", {
  method: "POST",
  headers: { "content-type": "application/json", "User-Agent": "iPhone" },
  body: JSON.stringify(body),
});
const waitRequest = () => new Request("https://relay/r/room/pair/wait?ticket=x");
const proof = (id = "phone-1") => ({
  device_id: id,
  name: "iPhone",
  pub: "A".repeat(44),
  confirm: "B".repeat(44),
});

async function begin(room, body = proof()) {
  const response = await room.pair(pairRequest(body));
  return { response, body: await response.json() };
}

async function ready(room, agent, pending, num = "0427") {
  const forwarded = agent.sent.at(-1);
  await room.webSocketMessage(agent, JSON.stringify({
    t: "pair_ready", id: forwarded.id, num, pub: "desktop-public", confirm: "desktop-confirm",
  }));
  return forwarded;
}

async function main() {
  // The socket attachment is an allowlist. Even a buggy/old desktop cannot make the relay persist
  // a pairing secret by placing it in hello.
  {
    const { room, agent } = fakeRoom();
    await room.webSocketMessage(agent, JSON.stringify({
      t: "hello", v: 2, e2eRequired: true, approve: true, devices: [], e2ePub: "pub",
      paircode: "THIS-MUST-NOT-BE-STORED",
    }));
    const stored = agent.deserializeAttachment();
    check(!Object.prototype.hasOwnProperty.call(stored, "paircode"),
          "the relay never stores a QR/pairing secret from hello");
  }

  // Old clients fail visibly rather than sending their credential into a downgraded flow.
  {
    const { room, agent } = fakeRoom();
    const result = await begin(room, { ...proof(), paircode: "secret" });
    check(result.response.status === 400, "a v1 body containing a pairing code is rejected");
    check(agent.sent.length === 0, "the rejected credential is never forwarded or persisted");
  }

  // Pairing is unauthenticated input. Bound it before JSON parsing so a few rate-limited requests
  // cannot each force the Durable Object to buffer an arbitrarily large body.
  {
    const { room, agent } = fakeRoom();
    const oversized = await room.pair(new Request("https://relay/r/room/pair", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...proof("oversized"), padding: "x".repeat(20 * 1024) }),
    }));
    check(oversized.status === 400 && agent.sent.length === 0,
          "an oversized pairing body is rejected before desktop forwarding");
  }

  // Full validating -> approval -> one-shot issue contract.
  {
    const { room, agent } = fakeRoom();
    const pending = await begin(room);
    check(pending.response.status === 202 && pending.body.phase === "validating" && pending.body.ticket,
          "POST /pair immediately returns a durable validating ticket");
    check(pending.body.num === undefined, "no comparison number is trusted before desktop validation");

    const forwarded = agent.sent.at(-1);
    check(forwarded.t === "pair_request" && forwarded.pub && forwarded.confirm,
          "the relay forwards only the public key and transcript proof");
    check(!("paircode" in forwarded) && !JSON.stringify(forwarded).includes("secret"),
          "the desktop pairing message contains no QR secret");

    let poll = await room.pairWait(pending.body.ticket, waitRequest());
    let body = await poll.json();
    check(poll.status === 202 && body.phase === "validating" && !body.num,
          "polling before proof validation stays in validating phase");

    await ready(room, agent, pending);
    poll = await room.pairWait(pending.body.ticket, waitRequest());
    body = await poll.json();
    check(poll.status === 202 && body.phase === "approval" && body.num === "0427" &&
          body.pub === "desktop-public" && body.confirm === "desktop-confirm",
          "after validation the phone receives the authenticated approval transcript");

    await room.webSocketMessage(agent, JSON.stringify({ t: "pair_decision", id: forwarded.id, ok: true }));
    const issued = await room.pairWait(pending.body.ticket, waitRequest());
    const issuedBody = await issued.json();
    check(issued.status === 200 && issuedBody.token && issuedBody.pub === "desktop-public",
          "approval issues one bearer token with the authenticated desktop proof");
    check(agent.sent.some((message) => message.t === "device_added"),
          "the desktop is told the new token hash for durable authorization");
    const again = await room.pairWait(pending.body.ticket, waitRequest());
    check(again.status === 404, "a collected ticket cannot issue a second token");
  }

  // Proof refusal and human refusal are both terminal and never mint a token.
  {
    const { room, agent } = fakeRoom();
    const pending = await begin(room, proof("bad-proof"));
    const rid = agent.sent.at(-1).id;
    await room.webSocketMessage(agent, JSON.stringify({ t: "pair_invalid", id: rid }));
    const refused = await room.pairWait(pending.body.ticket, waitRequest());
    check(refused.status === 403 && !(await refused.json()).token,
          "a desktop-rejected proof is terminal and tokenless");
  }
  {
    const { room, agent } = fakeRoom();
    const pending = await begin(room, proof("human-denied"));
    const forwarded = await ready(room, agent, pending, "9981");
    await room.webSocketMessage(agent, JSON.stringify({ t: "pair_decision", id: forwarded.id, ok: false }));
    const refused = await room.pairWait(pending.body.ticket, waitRequest());
    check(refused.status === 403 && !(await refused.json()).token,
          "human denial is terminal and tokenless");
  }

  // Ticket state survives a Durable Object eviction because both directions are indexed in storage.
  {
    const first = fakeRoom();
    const pending = await begin(first.room, proof("eviction"));
    const rid = first.agent.sent.at(-1).id;
    const woken = fakeRoom(first.storage);
    await woken.room.webSocketMessage(woken.agent, JSON.stringify({
      t: "pair_ready", id: rid, num: "1203", pub: "desktop-public", confirm: "desktop-confirm",
    }));
    const poll = await woken.room.pairWait(pending.body.ticket, waitRequest());
    check(poll.status === 202 && (await poll.json()).num === "1203",
          "a validating ticket survives hibernation and receives the correct desktop reply");
  }

  // Atomic claim: simultaneous polls cannot each mint a bearer token.
  {
    const { room, agent } = fakeRoom();
    const pending = await begin(room, proof("racing-polls"));
    const forwarded = await ready(room, agent, pending);
    await room.webSocketMessage(agent, JSON.stringify({ t: "pair_decision", id: forwarded.id, ok: true }));
    const responses = await Promise.all([
      room.pairWait(pending.body.ticket, waitRequest()),
      room.pairWait(pending.body.ticket, waitRequest()),
    ]);
    check(responses.filter((r) => r.status === 200).length === 1,
          "concurrent approval polls mint exactly one token");
  }

  // If the socket dies between approval and device_added, do not hand the phone a credential that
  // only exists in an optimistic edge attachment and will disappear on reconnect.
  {
    const { room, agent } = fakeRoom();
    const pending = await begin(room, proof("disconnect-at-issue"));
    const forwarded = await ready(room, agent, pending);
    await room.webSocketMessage(agent, JSON.stringify({ t: "pair_decision", id: forwarded.id, ok: true }));
    agent.send = (value) => {
      const message = JSON.parse(value);
      if (message.t === "device_added") throw new Error("socket closed");
      agent.sent.push(message);
    };
    const issued = await room.pairWait(pending.body.ticket, waitRequest());
    check(issued.status === 500 && !(await issued.json()).token &&
          agent.deserializeAttachment().devices.length === 0,
          "token issuance fails closed when device_added cannot reach the desktop");
  }

  // The admission limit is charged atomically when POST /pair is accepted, not later when a denied
  // ticket happens to be polled. A burst of valid-shaped proofs therefore cannot all reach desktop.
  {
    const { room, agent } = fakeRoom();
    const responses = await Promise.all(
      Array.from({ length: 6 }, (_, index) => room.pair(pairRequest(proof("burst-" + index)))));
    check(responses.filter((response) => response.status === 202).length === 5 &&
          responses.filter((response) => response.status === 429).length === 1,
          "concurrent valid-shaped pairing POSTs share one atomic five-attempt budget");
    check(agent.sent.filter((message) => message.t === "pair_request").length === 5,
          "the over-limit proof is never forwarded to the desktop");
  }

  // Expiry is fail closed.
  {
    const { room, storage } = fakeRoom();
    const pending = await begin(room, proof("expired"));
    const row = storage.m.get("pend:" + pending.body.ticket);
    row.at = Date.now() - 10 * 60 * 1000;
    const response = await room.pairWait(pending.body.ticket, waitRequest());
    check(response.status === 408, "an expired ticket cannot be approved or redeemed");
  }

  console.log(fails.length ? `\n  ${fails.length} FAILED` : "\n  relay pairing: all green");
  process.exit(fails.length ? 1 : 0);
}

main().catch((error) => { console.error(error); process.exit(1); });
