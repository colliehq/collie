/**
 * relay/worker.js — the two-phase pairing handshake, driven directly.
 *
 * The relay had no runtime test at all: it was only ever exercised by deploying it and pointing a
 * phone at it. The bug that prompted this file survived exactly that, because it needs *history* to
 * show up — a fresh room pairs fine, and only a room that already holds abandoned requests
 * mis-routes the approval. So the cases below are about state that accumulates.
 *
 * The Durable Object is stubbed at its real seams: storage is a Map (list/get/put/delete, including
 * delete(array)), the "agent" is a fake WebSocket that records what the relay sent it and whose
 * attachment carries the pairing code. Nothing about pair()/pairWait()/webSocketMessage() is
 * reimplemented here.
 *
 *   node tests/relay_pairing_test.js
 */
import { RelayRoom } from "../relay/worker.js";

const fails = [];
function check(cond, msg) {
  console.log((cond ? "  PASS " : "  FAIL ") + msg);
  if (!cond) fails.push(msg);
}

/** Storage with the semantics pairing relies on: prefix list, and delete of a key OR a key array. */
function fakeStorage() {
  const m = new Map();
  return {
    m,
    async get(k) { return m.get(k); },
    async put(k, v) { m.set(k, v); },
    async delete(k) { for (const key of Array.isArray(k) ? k : [k]) m.delete(key); },
    async list({ prefix }) {
      return new Map([...m].filter(([k]) => k.startsWith(prefix)));
    },
  };
}

function fakeRoom(paircode) {
  const sent = [];
  let att = { paircode, devices: [], approve: true };
  const agent = {
    sent,
    send: (s) => sent.push(JSON.parse(s)),
    serializeAttachment: (s) => { att = s; },
    deserializeAttachment: () => att,
  };
  const storage = fakeStorage();
  const room = new RelayRoom({
    storage,
    getWebSockets: () => [agent],
    acceptWebSocket: () => {},
  }, {});
  return { room, agent, storage };
}

const req = (body) => new Request("https://r/r/room/pair", {
  method: "POST",
  headers: { "content-type": "application/json", "User-Agent": "iPhone" },
  body: JSON.stringify(body),
});

async function pairReq(room, code, id) {
  const r = await room.pair(req({ paircode: code, device_id: id, name: id }));
  return { status: r.status, body: await r.json() };
}

/** The code is one-shot at the relay, so each attempt needs the room's code set afresh. */
function setCode(agent, code) {
  const s = agent.deserializeAttachment();
  s.paircode = code;
  agent.serializeAttachment(s);
}

async function main() {
  // ---- the request id must not repeat across requests -------------------------------------------
  {
    const { room, agent } = fakeRoom("AAAA1111");
    const ids = [];
    for (let i = 0; i < 4; i++) {
      setCode(agent, "CODE" + i + "000");
      await pairReq(room, "CODE" + i + "000", "dev" + i);
      ids.push(agent.sent[agent.sent.length - 1].id);
    }
    check(new Set(ids).size === 4, "four pairing requests get four distinct ids");
    check(ids.every((i) => typeof i === "string" && i.length > 16),
          "the id is a random token, not a per-instance counter that restarts at zero on eviction");
  }

  // ---- the real bug: an eviction between the abandoned requests and the live one -----------------
  // This is the case that reached production. Inside ONE instance the ids never repeat, so the whole
  // thing looks fine; the collision needs the room to be evicted and woken — storage survives, the
  // instance does not — which is precisely what an idle room does while nobody is pairing.
  {
    const { room, agent, storage } = fakeRoom("X");
    for (let i = 0; i < 3; i++) {                    // three phones that scanned and walked away
      setCode(agent, "ABAND" + i + "00");
      await pairReq(room, "ABAND" + i + "00", "gone" + i);
    }

    const woken = new RelayRoom({                    // same storage, fresh instance: counters reset
      storage,
      getWebSockets: () => [agent],
      acceptWebSocket: () => {},
    }, {});

    setCode(agent, "REALCODE");
    const live = await pairReq(woken, "REALCODE", "real-phone");
    check(live.status === 202 && live.body.pending && !!live.body.ticket,
          "a pairing attempt is held, not answered with a token");

    const rid = agent.sent[agent.sent.length - 1].id;
    await woken.webSocketMessage(agent, JSON.stringify({ t: "pair_decision", id: rid, ok: true }));
    const r = await woken.pairWait(live.body.ticket, req({}));
    const body = await r.json();
    check(r.status === 200 && body.ok && !!body.token,
          "after an eviction, approving still pairs THIS phone and not an abandoned request");

    // The approval must have landed on the live request and on nothing else.
    const decided = [...(await storage.list({ prefix: "pend:" }))]
      .filter(([, v]) => v.state && v.state !== "pending");
    check(decided.length === 0, "no abandoned request was marked approved in its place");
  }

  // ---- a decision the phone never collects must not leak storage forever -------------------------
  {
    const { room, agent } = fakeRoom("X");
    setCode(agent, "OLDCODE1");
    const old = await pairReq(room, "OLDCODE1", "slow-phone");
    for (const [k, v] of room.state.storage.m) {        // age it past the approval window
      if (k.startsWith("pend:")) v.at = Date.now() - 10 * 60 * 1000;
    }
    setCode(agent, "NEWCODE1");
    await pairReq(room, "NEWCODE1", "next-phone");      // sweeps on the way in
    const left = [...room.state.storage.m.keys()].filter((k) => k.startsWith("pend:"));
    check(left.length === 1, "an expired request is swept, and only the live one remains");
    const gone = [...room.state.storage.m.keys()].filter((k) => k.startsWith("rq:"));
    check(gone.length === 1, "its decision index is swept with it, not orphaned");
    const r = await room.pairWait(old.body.ticket, req({}));
    check(r.status === 404 || r.status === 408, "the swept ticket is no longer usable");
  }

  // ---- denial ----------------------------------------------------------------------------------
  {
    const { room, agent } = fakeRoom("X");
    setCode(agent, "DENYCODE");
    const p = await pairReq(room, "DENYCODE", "stranger");
    const rid = agent.sent[agent.sent.length - 1].id;
    await room.webSocketMessage(agent, JSON.stringify({ t: "pair_decision", id: rid, ok: false }));
    const r = await room.pairWait(p.body.ticket, req({}));
    const body = await r.json();
    check(r.status === 403 && !body.token, "a refused device gets no token");
    const again = await room.pairWait(p.body.ticket, req({}));
    check(again.status === 404, "and cannot retry the same ticket");
  }

  // ---- the phone must not be able to walk in while the human has not answered -------------------
  {
    const { room, agent } = fakeRoom("X");
    setCode(agent, "WAITCODE");
    const p = await pairReq(room, "WAITCODE", "patient");
    for (let i = 0; i < 3; i++) {
      const r = await room.pairWait(p.body.ticket, req({}));
      const b = await r.json();
      if (r.status !== 202 || b.token) { check(false, "polling before a decision issued a token"); break; }
      if (i === 2) check(true, "polling before a decision only ever returns pending");
    }
  }

  // ---- the number is the whole point: both ends must be told the same one ------------------------
  {
    const { room, agent } = fakeRoom("X");
    setCode(agent, "NUMCODE1");
    const p = await pairReq(room, "NUMCODE1", "phone");
    const toDesktop = agent.sent[agent.sent.length - 1];
    check(/^\d{4}$/.test(p.body.num) && toDesktop.num === p.body.num,
          "the phone and the desktop are given the same four-digit number");
  }

  console.log(fails.length ? "\n  " + fails.length + " FAILED" : "\n  relay pairing: all green");
  process.exit(fails.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
