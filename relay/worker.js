/**
 * Collie Remote — zero-knowledge relay Worker + RelayRoom Durable Object.  v2.
 *
 * One DO per room. Desktop dials in over WSS (/relay/agent) and stays connected; phone hits
 * /r/<room>/* over HTTPS; the DO multiplexes each phone request onto the agent WS, streaming the
 * response (incl. SSE) back into the phone's Response body.
 *
 * Security contract:
 *  - The pairing secret and desktop public key travel in a QR URL fragment, which is never sent to
 *    this Worker.  The Worker only forwards an HMAC transcript proof to the desktop.
 *  - A human approves every new bearer token after the desktop verifies that proof.  Tickets live in
 *    Durable Object storage and are single-use, including across hibernation.
 *  - Hosted API traffic is mandatory E2E and uses only POST /r/<room>/sealed.  The real HTTP method,
 *    path, query, headers, prompt and body are ciphertext.  Plaintext downgrade attempts fail closed.
 *  - Response records are forwarded in an exact contiguous sequence.  The phone independently
 *    authenticates a terminal record and rejects EOF without it.
 *  - The DESKTOP remains the source of truth for paired session-token hashes, so revocation persists.
 *  - AGENTKEY claim: first agent to a room stores sha256(key) in DO storage (persists across evictions
 *    and desktop downtime); later agents must match — stops room impersonation / free-relay abuse.
 * The relay still sees unavoidable routing metadata: room, opaque request/session ids, sizes/timing,
 * bearer-token hashes and APNs registration metadata.  It never sees application plaintext.
 */

const PAIR_WINDOW_MS = 10 * 60 * 1000;
const PAIR_MAX = 5;
// How long a pairing request stays approvable. Long enough for someone to walk to the desk, short
// enough that a request abandoned on a shared screen does not stay live.
const PAIR_APPROVE_MS = 3 * 60 * 1000;
const REVOKE_ACK_MS = 8 * 1000;
const REVOKE_ACKED_TTL_MS = 24 * 60 * 60 * 1000;
const PAIR_ENVELOPE_MAX = 16 * 1024;
const SEALED_ENVELOPE_MAX = 256 * 1024;
const PUSH_BODY_MAX = 8 * 1024;
const DEVICE_STORE_ACK_MS = 8 * 1000;
const RESPONSE_HEAD_MS = 30 * 1000;
const MAX_INFLIGHT = 64;
const MAX_DEVICES = 64;
const ROOM_RE = /^[A-Za-z0-9_-]{16,64}$/;
const AGENT_KEY_RE = /^[A-Za-z0-9_-]{32,128}$/;
const DEVICE_HASH_RE = /^[0-9a-f]{64}$/;

function pushConfigReady(env) {
  const value = env || {};
  return typeof value.APNS_KEY === "string" &&
    /-----BEGIN PRIVATE KEY-----[\s\S]+-----END PRIVATE KEY-----/.test(value.APNS_KEY) &&
    /^[A-Z0-9]{10}$/.test(String(value.APNS_KEY_ID || "")) &&
    /^[A-Z0-9]{10}$/.test(String(value.APNS_TEAM_ID || "")) &&
    /^(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+$/.test(String(value.APNS_TOPIC || ""));
}

// Dog presence is deliberately NOT Slack presence. Slack's Events/Socket Mode green dot cannot be
// driven from a listener's socket, so this is the truthful signal Collie itself can use when choosing
// a packmate: a short, renewable lease held only while that dog's listener is healthy.
//
// Provisioning is an explicit operator action. PRESENCE_ADMIN_TOKEN is a Worker secret and is used
// only at POST/DELETE /presence/enroll; POST mints a credential and DELETE retires one (pack,dog).
// Only a domain-separated SHA-256 digest of the dog credential is stored. Runtime credentials are
// accepted in Authorization headers only — never URLs, messages, attachments, status responses or
// logs. If the admin secret/binding is missing, enrollment fails closed.
// Clients should heartbeat every 20-25s. Seventy-five seconds tolerates two missed beats while still
// bounding a hard crash/power loss to a short, explicit stale window.
const PRESENCE_LEASE_MS = 75 * 1000;
const PRESENCE_HEALTH = new Set(["ok", "degraded"]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const p = url.pathname;
    if (p.startsWith("/presence/")) return presenceFront(request, env, url);
    let room = null;
    if (p === "/relay/agent") room = url.searchParams.get("room");
    else if (p.startsWith("/r/")) room = p.split("/")[2] || null;
    // Wrangler cannot declare encrypted secrets as required bindings. Make an incomplete production
    // deployment visibly unavailable instead of silently accepting Remote sessions whose push path
    // can never notify a phone. The response deliberately does not identify which binding is absent.
    if (!room)
      return pushConfigReady(env)
        ? new Response("collie relay", { status: 200 })
        : json({ error: "relay configuration unavailable" }, 503);
    if (!ROOM_RE.test(room)) return json({ error: "invalid room" }, 400);
    if (!pushConfigReady(env)) return json({ error: "relay configuration unavailable" }, 503);
    return env.RELAY.get(env.RELAY.idFromName(room)).fetch(request);
  },
};

export class RelayRoom {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.seq = 0;                  // request-id counter (per alive instance; resets after hibernation — fine)
    this.pending = new Map();      // id -> {controller,…}; only non-empty while a request is in flight
    this.pairAttempts = [];
    // Production pairing limits live in atomic DO storage. This array is only the fallback used by
    // tiny unit-test stores that do not implement transactions.
    this.revokeWaiters = new Map(); // revoke message id -> bounded HTTP waiter for desktop ACK
    this.revokeAckMs = REVOKE_ACK_MS;
    this.deviceStoreWaiters = new Map(); // device_added id -> durable desktop-store ACK waiter
    this.deviceStoreAckMs = DEVICE_STORE_ACK_MS;
    this.responseHeadMs = RESPONSE_HEAD_MS;
    this.maxInflight = MAX_INFLIGHT;
    // Pending pair approvals live in DO STORAGE ("pend:<ticket>"), not here — see pair(). A request
    // that is waiting for a human is precisely the state most likely to outlive an eviction.
    this.claimingTickets = new Set(); // fallback single-instance guard; storage transaction is authoritative
  }

  // Hibernation: an idle room is evicted from memory (→ no duration billing) while its WebSocket stays
  // open at the edge. So the agent socket + its pairing state must survive eviction: find the socket via
  // getWebSockets("agent"), and stash protocol/device hashes on it with serializeAttachment (persisted),
  // instead of in-memory fields the constructor would wipe on wake.
  _agent() {
    const sockets = this.state.getWebSockets("agent")
      .filter((ws) => ws.readyState === undefined || ws.readyState === 1);
    return sockets.length ? sockets[sockets.length - 1] : null;
  }
  _astate(ws) { try { return ws.deserializeAttachment() || {}; } catch (e) { return {}; } }
  _setAstate(ws, s) { try { ws.serializeAttachment(s); } catch (e) {} }

  async fetch(request) {
    const url = new URL(request.url);
    const p = url.pathname;
    if (p === "/relay/agent") return this.acceptAgent(request);
    const rest = p.replace(/^\/r\/[^/]+/, "") || "/";
    if (rest === "/pair" && request.method === "POST") return this.pair(request);
    // Phase two: the phone shows the number and asks here until the desktop has decided. Short
    // polls, so nothing depends on a connection staying open while a human makes up their mind.
    if (rest === "/pair/wait" && request.method === "GET") {
      return this.pairWait(url.searchParams.get("ticket") || "", request);
    }
    // A paired phone leaves its APNs token here so the desktop can reach it when the app is closed.
    // Session-authenticated: only a device this desktop already let in can be pushed to.
    if (rest === "/push/register" && request.method === "POST") return this.registerPush(request);
    if (rest === "/device/revoke" && request.method === "POST") return this.revokeDevice(request);
    if (rest === "/sealed" && request.method === "POST") return this.proxySealed(request);
    if (rest === "/" || rest === "") {
      return json({ error: "Use the Collie mobile app and scan a fresh desktop QR code." }, 426);
    }
    // There is intentionally no general-purpose proxy route.  A URL such as /api/stream?q=prompt
    // would disclose both endpoint and prompt to the relay even if a header happened to be sealed.
    return json({ error: "hosted protocol v2 requires POST /sealed" }, 404);
  }

  // ---------------------------------------------------------------- agent (desktop) side
  async acceptAgent(request) {
    if (request.headers.get("Upgrade") !== "websocket")
      return new Response("expected websocket", { status: 426 });
    const key = new URL(request.url).searchParams.get("key") || "";
    if (!AGENT_KEY_RE.test(key)) return new Response("bad agent key", { status: 403 });
    const keyHash = await sha256hex(key);
    if (!(await this.claimAgentKeyHash(keyHash)))
      return new Response("bad agent key", { status: 403 });

    // Exactly one authenticated desktop owns a room at a time. A reconnect replaces its stale
    // socket and fails any work tied to that socket, rather than letting two agents race replies.
    const oldAgents = this.state.getWebSockets("agent");
    if (oldAgents.length) {
      this._dropPending("agent replaced");
      for (const old of oldAgents) {
        try { old.close(1012, "agent replaced"); } catch (e) {}
      }
    }

    const pair = new WebSocketPair();
    const [client, server] = [pair[0], pair[1]];
    this.state.acceptWebSocket(server, ["agent"]);        // hibernatable — an idle room costs ~nothing
    server.serializeAttachment({ protocol: 0, e2eRequired: false, devices: [] });
    return new Response(null, { status: 101, webSocket: client });
  }

  async claimAgentKeyHash(keyHash) {
    const storage = this.state.storage;
    if (storage.transaction) {
      return storage.transaction(async (txn) => {
        const stored = await txn.get("agentKeyHash");
        if (stored && stored !== keyHash) return false;
        if (!stored) await txn.put("agentKeyHash", keyHash);
        return true;
      });
    }
    // Minimal unit-test stores do not expose transactions. Production Durable Object storage does.
    const stored = await storage.get("agentKeyHash");
    if (stored && stored !== keyHash) return false;
    if (!stored) await storage.put("agentKeyHash", keyHash);
    return true;
  }

  // ---- hibernation handlers (called by the runtime, survive DO eviction) ----
  webSocketClose(ws) {
    const current = this._agent();
    if (!current || current === ws) this._dropPending("agent disconnected");
  }
  webSocketError(ws) {
    const current = this._agent();
    if (!current || current === ws) this._dropPending("agent error");
  }
  _dropPending(why) {
    for (const [, slot] of this.pending) {
      clearTimeout(slot.headTimer);
      try { slot.opened ? slot.controller.error(new Error(why)) : slot.headReject(new Error(why)); } catch (e) {}
    }
    this.pending.clear();
    for (const [, waiter] of this.revokeWaiters) waiter.resolve(false);
    this.revokeWaiters.clear();
    for (const [, waiter] of this.deviceStoreWaiters) waiter.resolve(false);
    this.deviceStoreWaiters.clear();
  }

  async webSocketMessage(ws, message) {
    if (this._agent() !== ws) return; // frames from a replaced desktop socket are never authoritative
    let msg;
    try { msg = JSON.parse(typeof message === "string" ? message : ""); } catch (e) { return; }
    // pairing state lives on the socket attachment (survives hibernation), not in memory
    if (msg.t === "hello") {
      // Never serialize unknown hello fields: an accidental future `paircode` field must not become
      // durable relay state.  v1 agents remain visibly incompatible instead of downgrading.
      const tombstones = await this.revokeTombstones();
      const blocked = new Set(tombstones.keys());
      this._setAstate(ws, { protocol: Number(msg.v || 0),
                            devices: cleanDeviceHashes(msg.devices, blocked),
                            approve: !!msg.approve, e2eRequired: msg.e2eRequired === true });
      // A pending durable tombstone outlives a dropped socket/HTTP response. Re-send it whenever the
      // authenticated desktop reconnects; deletion is idempotent and only its ACK completes revoke.
      for (const [hash, row] of tombstones) {
        if (row.state === "pending")
          this.sendAgent(ws, { t: "device_revoke", id: randToken(), hash });
      }
      return;
    }
    if (msg.t === "device_revoked") {
      const hash = String(msg.hash || "");
      const waiter = this.revokeWaiters.get(msg.id);
      let acknowledged = false;
      if (/^[0-9a-f]{64}$/.test(hash)) {
        const key = "revoke:" + hash;
        const row = await this.state.storage.get(key);
        if (row && row.state === "pending" && msg.ok === true) {
          await this.state.storage.put(key, { state: "acked", at: Date.now() });
          acknowledged = true;
        } else if (row && row.state === "acked" && msg.ok === true) {
          acknowledged = true;
        }
      }
      if (waiter && waiter.hash === hash) waiter.resolve(acknowledged);
      return;
    }
    if (msg.t === "device_stored") {
      const waiter = this.deviceStoreWaiters.get(String(msg.id || ""));
      if (waiter && waiter.agent === ws && waiter.hash === String(msg.hash || ""))
        waiter.resolve(msg.ok === true);
      return;
    }
    if (msg.t === "pair_ready" || msg.t === "pair_invalid") {
      const ticket = await this.state.storage.get("rq:" + msg.id);
      if (!ticket) return;
      const v = await this.state.storage.get("pend:" + ticket);
      if (!v || v.state !== "validating") return;
      if (msg.t === "pair_invalid") {
        v.state = "denied";
        v.error = "pairing proof refused";
      } else {
        v.state = "pending";
        v.num = String(msg.num || "");
        v.e2e = { pub: String(msg.pub || ""), confirm: String(msg.confirm || "") };
      }
      await this.state.storage.put("pend:" + ticket, v);
      return;
    }
    if (msg.t === "pair_decision") {
      // Record the verdict; the phone collects it on its next poll. Nothing is awaiting this, so a
      // desktop that answers after the phone gave up simply leaves a decided ticket that expires.
      //
      // Look the ticket up through the "rq:" index instead of scanning for a matching id. The id is
      // random, so it names exactly the request the human answered. A scan keyed on a per-instance
      // counter matched whichever ABANDONED request was stored first under the same number — it
      // marked that one approved and left the live phone polling until it expired.
      const ticket = await this.state.storage.get("rq:" + msg.id);
      if (!ticket) return;
      const v = await this.state.storage.get("pend:" + ticket);
      if (!v) return;
      if (v.state !== "pending") return; // proof validation must precede human approval
      v.state = msg.ok ? "approved" : "denied";
      v.error = msg.error || "";
      await this.state.storage.put("pend:" + ticket, v);
      return;
    }
    if (msg.t === "notify") {
      // The desktop has something worth interrupting a person for. Fire and forget: a push that
      // fails must never stall the socket the run itself is streaming over.
      this.pushAll({ title: "Collie", body: "A run has an update on your desktop.",
                     thread: msg.thread, session: msg.session })
        .catch(() => {});
      return;
    }
    if (msg.t === "devices") {
      const tombstones = await this.revokeTombstones();
      const blocked = new Set(tombstones.keys());
      const s = this._astate(ws);
      s.devices = cleanDeviceHashes(msg.devices, blocked);
      this._setAstate(ws, s);
      return;
    }
    const slot = this.pending.get(msg.id);
    if (!slot) return;
    if (msg.t === "res") {
      if (slot.opened || !msg.enc || !Number.isInteger(msg.seq) || msg.seq !== 0) {
        return this._failSlot(msg.id, slot, "invalid or duplicate sealed response head");
      }
      slot.status = msg.status; slot.headers = msg.headers || {}; slot.opened = true;
      slot.expectedSeq = 1;
      clearTimeout(slot.headTimer);
      try { slot.controller.enqueue(new TextEncoder().encode(
        JSON.stringify({ enc: msg.enc, seq: 0 }) + "\n")); } catch (e) {}
      slot.headResolve();
    } else if (msg.t === "chunk") {
      if (!slot.opened || !msg.enc || !Number.isInteger(msg.seq) || msg.seq !== slot.expectedSeq) {
        return this._failSlot(msg.id, slot, "sealed response sequence gap or duplicate");
      }
      try {
        const line = new TextEncoder().encode(JSON.stringify({ enc: msg.enc, seq: msg.seq }) + "\n");
        slot.controller.enqueue(line);
        slot.expectedSeq += 1;
      } catch (e) {}
    } else if (msg.t === "end") {
      if (!slot.opened) return this._failSlot(msg.id, slot, "response ended before its head");
      clearTimeout(slot.headTimer);
      try { slot.controller.close(); } catch (e) {}
      this.pending.delete(msg.id);
    } else if (msg.t === "err") {
      clearTimeout(slot.headTimer);
      if (!slot.opened) slot.headReject(new Error(msg.msg || "agent error"));
      else { try { slot.controller.error(new Error(msg.msg || "agent error")); } catch (e) {} }
      this.pending.delete(msg.id);
    }
  }

  _failSlot(id, slot, why) {
    clearTimeout(slot.headTimer);
    if (!slot.opened) slot.headReject(new Error(why));
    else { try { slot.controller.error(new Error(why)); } catch (e) {} }
    this.pending.delete(id);
  }

  // ---------------------------------------------------------------- phone side
  async pair(request) {
    const agent = this._agent();
    if (!agent) return json({ ok: false, error: "desktop offline" }, 503);
    const agentState = this._astate(agent);
    if (agentState.protocol !== 2 || agentState.e2eRequired !== true || !agentState.approve)
      return json({ ok: false, error: "desktop must upgrade to hosted protocol v2" }, 426);
    const now = Date.now();
    // Charge every admitted POST before parsing or forwarding it. The production transaction makes
    // simultaneous requests share one durable ledger instead of each seeing a free slot.
    if (!(await this.takePairAttempt(now)))
      return json({ ok: false, error: "too many attempts — wait a few minutes" }, 429);

    const rawBody = await readBodyLimited(request, PAIR_ENVELOPE_MAX);
    let body = null;
    try { body = rawBody === null ? null : JSON.parse(rawBody); } catch (e) {}
    if (!body || typeof body !== "object" || Array.isArray(body))
      return json({ ok: false, error: "invalid pairing request" }, 400);
    // The v2 body contains proof, never the QR secret.  Explicitly reject the old field so a client
    // cannot believe it is relay-blind while handing the credential to this process.
    if (Object.prototype.hasOwnProperty.call(body, "paircode") ||
        typeof body.pub !== "string" || typeof body.confirm !== "string" ||
        typeof body.device_id !== "string" || !body.device_id ||
        body.pub.length > 128 || body.confirm.length > 128 || body.device_id.length > 128) {
      return json({ ok: false, error: "invalid relay-blind pairing proof" }, 400);
    }
    await this.sweepPending();
    const rid = randToken();
    const ticket = randToken();
    const clean = {
      device_id: body.device_id,
      name: String(body.name || shortUA(request.headers.get("User-Agent") || "")).slice(0, 60),
      pub: body.pub,
      confirm: body.confirm,
    };
    // First state is "validating": the phone may poll, but neither a comparison number nor an
    // approval exists until the desktop has authenticated the proof.
    await this.state.storage.put("pend:" + ticket, {
      rid, at: now, state: "validating", device_id: clean.device_id, body: clean,
    });
    await this.state.storage.put("rq:" + rid, ticket);
    if (!this.sendAgent(agent, { t: "pair_request", id: rid, ...clean })) {
      await this.state.storage.delete(["pend:" + ticket, "rq:" + rid]);
      return json({ ok: false, error: "desktop disconnected" }, 503);
    }
    return json({ ok: false, pending: true, phase: "validating", ticket }, 202);
  }

  async takePairAttempt(now) {
    const key = "pair-rate";
    if (this.state.storage.transaction) {
      return this.state.storage.transaction(async (txn) => {
        const stored = await txn.get(key);
        const attempts = (Array.isArray(stored) ? stored : [])
          .filter((value) => Number.isFinite(value) && now - value < PAIR_WINDOW_MS);
        if (attempts.length >= PAIR_MAX) {
          await txn.put(key, attempts);
          return false;
        }
        attempts.push(now);
        await txn.put(key, attempts);
        return true;
      });
    }
    this.pairAttempts = this.pairAttempts.filter((value) => now - value < PAIR_WINDOW_MS);
    if (this.pairAttempts.length >= PAIR_MAX) return false;
    this.pairAttempts.push(now);
    return true;
  }

  async resetPairAttempts() {
    this.pairAttempts = [];
    await this.state.storage.delete("pair-rate");
  }

  /**
   * Drop pairing requests nobody came back for. Without this they accumulate for the life of the
   * room: a phone that is closed mid-pairing never reads its ticket again, and expiry-on-read never
   * runs. Called on each new pairing attempt, where the rate limit already bounds the work.
   */
  async sweepPending() {
    const now = Date.now();
    const dead = [];
    for (const [k, v] of await this.state.storage.list({ prefix: "pend:" })) {
      if (!v || now - v.at > PAIR_APPROVE_MS) { dead.push(k); if (v && v.rid) dead.push("rq:" + v.rid); }
    }
    if (dead.length) await this.state.storage.delete(dead);
  }

  /** Phase two: has the desktop decided about this ticket yet? */
  async pairWait(ticket, request) {
    const key = "pend:" + ticket;
    const p = ticket ? await this.state.storage.get(key) : null;
    if (!p) return json({ ok: false, error: "unknown or expired pairing request" }, 404);

    // Expire on read: an abandoned request must not stay approvable. A request nobody ever reads
    // again is cleared by sweepPending() on the next pairing attempt.
    if (Date.now() - p.at > PAIR_APPROVE_MS) {
      await this.state.storage.delete([key, "rq:" + p.rid]);
      return json({ ok: false, error: "this pairing request expired — scan again" }, 408);
    }
    if (p.state === "validating")
      return json({ ok: false, pending: true, phase: "validating" }, 202);
    if (p.state === "pending")
      return json({ ok: false, pending: true, phase: "approval", num: p.num,
                    pub: p.e2e && p.e2e.pub, confirm: p.e2e && p.e2e.confirm }, 202);
    if (p.state !== "approved") {
      await this.state.storage.delete([key, "rq:" + p.rid]);
      return json({ ok: false, error: p.error || "the desktop refused this device" }, 403);
    }
    const agent = this._agent();
    if (!agent) return json({ ok: false, error: "desktop offline" }, 503);
    const claimed = await this.claimApprovedTicket(key, p.rid);
    if (!claimed) return json({ ok: false, error: "pairing ticket already consumed" }, 409);
    try {
      const response = await this.issueToken(
        agent, p.body, p.e2e, request, p.rid);
      await this.state.storage.delete([key, "rq:" + p.rid]);
      await this.resetPairAttempts();
      return response;
    } catch (e) {
      // Do not roll an accepted ticket back to approved: a retry could mint two bearer tokens.
      return json({ ok: false, error: "could not finish this one-shot pairing" }, 500);
    }
  }

  async claimApprovedTicket(key, rid) {
    if (this.state.storage.transaction) {
      return this.state.storage.transaction(async (txn) => {
        const current = await txn.get(key);
        if (!current || current.rid !== rid || current.state !== "approved") return false;
        current.state = "issuing";
        await txn.put(key, current);
        return true;
      });
    }
    // Unit-test/miniflare fallback.  The production Durable Object storage path above is atomic.
    if (this.claimingTickets.has(key)) return false;
    this.claimingTickets.add(key);
    const current = await this.state.storage.get(key);
    if (!current || current.rid !== rid || current.state !== "approved") return false;
    current.state = "issuing";
    await this.state.storage.put(key, current);
    return true;
  }

  async issueToken(agent, body, e2e, request, pairId) {
    if (!e2e || !e2e.pub || !e2e.confirm) throw new Error("missing authenticated E2E result");
    const token = randToken();
    const hash = await sha256hex(token);
    const name = String(body.name || shortUA(request.headers.get("User-Agent") || "")).slice(0, 60);
    // device_id: a client-supplied STABLE id (localStorage / Keychain) so re-pairing the same client
    // updates its device row instead of duplicating. device_added carries it + the token hash + name.
    if (!(await this.waitForDeviceStored(
      agent, String(body.device_id || ""), hash, name, String(pairId || "")))) {
      // The phone has not received the token yet. Fail the one-shot ticket unless the desktop has
      // explicitly confirmed that both the token hash and K_dev are durably stored.
      throw new Error("desktop disconnected before storing device");
    }
    const current = this._astate(agent);
    current.devices = cleanDeviceHashes([...(current.devices || []), hash]);
    this._setAstate(agent, current);
    // token in the body too → a NATIVE app (no cookie jar) stores it in the Keychain and sends it as
    // `Authorization: Bearer <token>`. A browser/WKWebView just uses the Secure cookie below.
    const payload = { ok: true, token, pub: e2e.pub, confirm: e2e.confirm };
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        // 1-year cookie → durable; real lifetime is bounded by the desktop still trusting this hash.
        "set-cookie": `collie_sess=${token}; HttpOnly; Secure; SameSite=Lax; Path=/r/; Max-Age=31536000`,
      },
    });
  }

  async proxySealed(request) {
    const agent = this._agent();
    if (!agent) return offlinePage();
    const st = this._astate(agent);
    if (st.protocol !== 2 || st.e2eRequired !== true)
      return json({ error: "desktop must upgrade to hosted protocol v2" }, 426);
    if (!(await this.checkSession(request, agent))) return json({ error: "not paired" }, 401);
    if (this.pending.size >= this.maxInflight)
      return json({ error: "room request capacity reached; retry later" }, 429);
    const contentType = (request.headers.get("Content-Type") || "").toLowerCase();
    if (contentType !== "application/octet-stream")
      return json({ error: "sealed requests require application/octet-stream" }, 415);
    const enc = await readBodyLimited(request, SEALED_ENVELOPE_MAX);
    const cid = request.headers.get("X-Collie-Rid") || "";
    const session = request.headers.get("X-Collie-Session") || "";
    if (!enc || !cid || cid.length > 128 || !session || session.length > 256)
      return json({ error: "a valid sealed envelope is required" }, 400);
    try {
      const parsed = JSON.parse(enc);
      if (!parsed || typeof parsed.n !== "string" || typeof parsed.ct !== "string") throw new Error();
    } catch (e) {
      return json({ error: "malformed sealed envelope" }, 400);
    }
    // Recheck after the asynchronous body read. Durable Objects can interleave requests at awaits.
    if (this.pending.size >= this.maxInflight)
      return json({ error: "room request capacity reached; retry later" }, 429);
    const id = ++this.seq;
    let slot;
    const stream = new ReadableStream({
      start: (controller) => {
        slot = { controller, opened: false, expectedSeq: 0 };
        slot.headPromise = new Promise((res, rej) => { slot.headResolve = res; slot.headReject = rej; });
        // Dispatch can fail synchronously inside this stream constructor, before proxySealed reaches
        // its own await/catch. Attach a handler now so runtimes with strict unhandled-rejection rules
        // return the intended 502 instead of terminating the worker/test process.
        slot.headPromise.catch(() => {});
        this.pending.set(id, slot);
        slot.headTimer = setTimeout(
          () => this._failSlot(id, slot, "desktop response head timed out"), this.responseHeadMs);
        // Only opaque routing fields cross the relay/desktop boundary.  No URL, query, method,
        // application headers or body is available here to log or inspect.
        if (!this.sendAgent(agent, { t: "req", id, cid, session, enc, seq: 0 })) {
          // `ReadableStream.start` runs synchronously, before proxySealed can await headPromise.
          // Resolve that local hand-off with an explicit error marker; rejecting here would briefly
          // create an unhandled promise and strict runtimes terminate before the 502 is returned.
          slot.dispatchError = "agent disconnected before request dispatch";
          clearTimeout(slot.headTimer);
          try { controller.close(); } catch (e) {}
          slot.headResolve();
        }
      },
      cancel: () => {
        if (this.pending.get(id) === slot) {
          clearTimeout(slot.headTimer);
          this.pending.delete(id);
        }
      },
    });

    try { await slot.headPromise; }
    catch (e) { return json({ error: String((e && e.message) || e) }, 502); }
    if (slot.dispatchError) {
      clearTimeout(slot.headTimer);
      this.pending.delete(id);
      return json({ error: slot.dispatchError }, 502);
    }

    return new Response(stream, { status: 200,
      headers: { "content-type": "application/octet-stream", "cache-control": "no-store" } });
  }

  async revokeDevice(request) {
    const token = tokenOf(request);
    if (!token) return json({ ok: false, error: "not paired" }, 401);
    const hash = await sha256hex(token);
    const tombstoneKey = "revoke:" + hash;
    const tombstones = await this.revokeTombstones();
    const existing = tombstones.get(hash);
    // Retrying after the desktop ACKed but before the phone received HTTP 200 is successful and does
    // not require the desktop to still be online.
    if (existing && existing.state === "acked") return json({ ok: true });

    const agent = this._agent();
    if (!agent) return json({ ok: false, error: "desktop offline; retry revocation" }, 503);
    const st = this._astate(agent);
    if (!(st.devices || []).includes(hash) && !existing)
      return json({ ok: false, error: "not paired" }, 401);

    // Persist first. From this point on, hello/devices filters cannot resurrect the bearer even if
    // the desktop socket or this HTTP response disappears midway through the operation.
    if (!existing)
      await this.state.storage.put(tombstoneKey, { state: "pending", at: Date.now() });
    st.devices = (st.devices || []).filter((x) => x !== hash);
    this._setAstate(agent, st);
    await this.state.storage.delete("push:" + hash);
    const acknowledged = await this.waitForRevokeAck(agent, hash);
    if (!acknowledged)
      return json({ ok: false, error: "desktop did not confirm durable revocation; retry" }, 503);
    return json({ ok: true });
  }

  async revokeTombstones() {
    const rows = await this.state.storage.list({ prefix: "revoke:" });
    const now = Date.now();
    const live = new Map();
    const stale = [];
    for (const [key, row] of rows) {
      const hash = key.slice("revoke:".length);
      if (!row || !["pending", "acked"].includes(row.state) ||
          (row.state === "acked" && now - Number(row.at || 0) > REVOKE_ACKED_TTL_MS)) {
        stale.push(key);
      } else {
        live.set(hash, row);
      }
    }
    if (stale.length) await this.state.storage.delete(stale);
    return live;
  }

  waitForRevokeAck(agent, hash) {
    const id = randToken();
    return new Promise((resolve) => {
      let settled = false;
      const done = (ok) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.revokeWaiters.delete(id);
        resolve(!!ok);
      };
      const timer = setTimeout(() => done(false), this.revokeAckMs);
      this.revokeWaiters.set(id, { hash, resolve: done });
      if (!this.sendAgent(agent, { t: "device_revoke", id, hash })) done(false);
    });
  }

  waitForDeviceStored(agent, deviceId, hash, name, pairId) {
    const id = randToken();
    return new Promise((resolve) => {
      let settled = false;
      const done = (ok) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.deviceStoreWaiters.delete(id);
        resolve(!!ok);
      };
      const timer = setTimeout(() => done(false), this.deviceStoreAckMs);
      this.deviceStoreWaiters.set(id, { agent, hash, resolve: done });
      if (!this.sendAgent(agent, {
        t: "device_added", id, pair_id: pairId, device_id: deviceId, hash, name,
      })) done(false);
    });
  }

  async checkSession(request, agent) {
    const tok = tokenOf(request);
    if (!tok) return false;
    const devices = this._astate(agent).devices || [];
    return devices.includes(await sha256hex(tok));
  }

  // ---------------------------------------------------------------- push
  //
  // The phone is only useful away from the desk if it can be TOLD something happened. An app that
  // has to be open to find out is a worse version of walking back to the computer.
  //
  // Tokens live here rather than on the desktop because the desktop may well be the thing that is
  // busy, asleep, or on another network when the moment comes; the relay is the piece that is always
  // up. They are keyed by the hash of the session token, so forgetting a device on the desktop also
  // strands its pushes: no session, no delivery.

  async registerPush(request) {
    const agent = this._agent();
    if (!agent) return json({ ok: false, error: "desktop offline" }, 503);
    if (!(await this.checkSession(request, agent))) return json({ ok: false, error: "not paired" }, 401);
    const rawBody = await readBodyLimited(request, PUSH_BODY_MAX);
    let body = null;
    try { body = rawBody === null ? null : JSON.parse(rawBody); } catch (e) {}
    if (!body || typeof body !== "object" || Array.isArray(body))
      return json({ ok: false, error: "invalid push registration" }, 400);
    const token = String((body && body.token) || "");
    if (!/^[0-9a-fA-F]{64,200}$/.test(token)) return json({ ok: false, error: "bad device token" }, 400);
    const auth = (request.headers.get("Authorization") || "").match(/^Bearer\s+([A-Za-z0-9_\-]+)$/);
    const cookie = (request.headers.get("Cookie") || "").match(/collie_sess=([A-Za-z0-9_\-]+)/);
    const sess = await sha256hex((auth && auth[1]) || (cookie && cookie[1]) || "");
    await this.state.storage.put("push:" + sess, {
      token: token.toLowerCase(),
      // TestFlight and the App Store are both the production gateway; only a locally built debug
      // app is on sandbox. The app says which one it was built as, because the relay cannot tell.
      sandbox: !!(body && body.sandbox),
      name: String((body && body.name) || "").slice(0, 60),
      at: Date.now(),
    });
    return json({ ok: true });
  }

  /// Fan a desktop notice out to every phone paired with this room.
  async pushAll(note) {
    const rows = await this.state.storage.list({ prefix: "push:" });
    if (!rows.size) return;
    const agent = this._agent();
    // A disconnected desktop is absence of evidence, not evidence that every paired phone was
    // revoked. Keep registrations until an attached agent can authoritatively supply its devices.
    if (!agent) return;
    const allowed = new Set(this._astate(agent).devices || []);
    const stale = [];
    for (const [key, row] of rows) {
      if (!allowed.has(key.slice("push:".length))) { stale.push(key); continue; }
      const status = await this.apns(row, note);
      // 410 Gone is APNs telling us this install is finished with — deleting it is the documented
      // obligation, and keeping it would mean signing a request per notification forever.
      if (status === 410 || status === 400) stale.push(key);
    }
    if (stale.length) await this.state.storage.delete(stale);
  }

  async apns(row, note) {
    const env = this.env;
    if (!env.APNS_KEY || !env.APNS_KEY_ID || !env.APNS_TEAM_ID || !env.APNS_TOPIC) return 0;
    const host = row.sandbox ? "api.sandbox.push.apple.com" : "api.push.apple.com";
    let jwt;
    try {
      jwt = await apnsJWT(env.APNS_KEY, env.APNS_KEY_ID, env.APNS_TEAM_ID);
    } catch (e) {
      return 0;
    }
    const payload = {
      aps: {
        alert: { title: note.title || "Collie", body: note.body || "" },
        sound: "default",
        "thread-id": note.thread || "collie",
      },
    };
    if (note.session) payload.session = note.session;
    const res = await fetch("https://" + host + "/3/device/" + row.token, {
      method: "POST",
      headers: {
        authorization: "bearer " + jwt,
        "apns-topic": env.APNS_TOPIC,
        "apns-push-type": "alert",
        "apns-priority": "10",
      },
      body: JSON.stringify(payload),
    }).catch(() => null);
    return res ? res.status : 0;
  }

  sendAgent(agent, obj) {
    try { agent.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
  }
}

// ---------------------------------------------------------------- dog presence

/**
 * Route presence before the phone-remote room router.
 *
 * The Durable Object id is derived from PACK, never from a caller-supplied object id. A pack is the
 * consistency boundary: every dog in it is read and fenced by one single-threaded PresencePack.
 * `dog` on /status names the credential owner (the caller); an authenticated member may see the
 * small online/offline roster for its own pack, but no session ids, machine data, tasks or secrets.
 */
async function presenceFront(request, env, url) {
  if (!env.PRESENCE) return json({ ok: false, error: "presence unavailable" }, 503);
  const pack = url.searchParams.get("pack") || "";
  const dog = url.searchParams.get("dog") || "";
  if (!presenceID(pack, 128) || !presenceID(dog, 80))
    return json({ ok: false, error: "invalid pack or dog" }, 400);

  if (url.pathname === "/presence/enroll") {
    if (request.method !== "POST" && request.method !== "DELETE")
      return json({ ok: false, error: "method not allowed" }, 405);
    if (!env.PRESENCE_ADMIN_TOKEN)
      return json({ ok: false, error: "presence enrollment is not configured" }, 503);
    if (!(await bearerEquals(request, env.PRESENCE_ADMIN_TOKEN)))
      return json({ ok: false, error: "unauthorized" }, 401);
  } else if (url.pathname === "/presence/ws") {
    if (request.method !== "GET" ||
        (request.headers.get("Upgrade") || "").toLowerCase() !== "websocket")
      return json({ ok: false, error: "expected websocket" }, 426);
    if (!presenceID(url.searchParams.get("session") || "", 128, 8))
      return json({ ok: false, error: "invalid session" }, 400);
  } else if (url.pathname === "/presence/status") {
    if (request.method !== "GET") return json({ ok: false, error: "method not allowed" }, 405);
  } else {
    return json({ ok: false, error: "not found" }, 404);
  }

  return env.PRESENCE.get(env.PRESENCE.idFromName(pack)).fetch(request);
}

/**
 * One strongly-consistent, hibernatable presence registry per pack.
 *
 * Storage keys:
 *   auth:<dog>  -> {hash}                         credential verifier, never returned
 *   lease:<dog> -> {session,owner,seq,health,expiresAt}
 *
 * `owner` is random per accepted socket and never crosses the wire. Session ids fence an old
 * process from a new process; owner additionally fences two sockets that accidentally reuse the
 * same session. A stale heartbeat/bye therefore cannot revive or erase the replacement lease.
 */
export class PresencePack {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const pack = url.searchParams.get("pack") || "";
    const dog = url.searchParams.get("dog") || "";
    if (!presenceID(pack, 128) || !presenceID(dog, 80))
      return json({ ok: false, error: "invalid pack or dog" }, 400);

    if (url.pathname === "/presence/enroll")
      return request.method === "DELETE" ? this.unenroll(request, pack, dog)
                                         : this.enroll(request, pack, dog);
    if (url.pathname === "/presence/ws") return this.connect(request, pack, dog,
      url.searchParams.get("session") || "");
    if (url.pathname === "/presence/status") return this.status(request, pack, dog);
    return json({ ok: false, error: "not found" }, 404);
  }

  async enroll(request, pack, dog) {
    // Validate again inside the DO. Today all traffic arrives through presenceFront, but keeping the
    // authority check at the mutation boundary means a future internal caller cannot accidentally
    // turn the binding itself into an unauthenticated provisioning API.
    if (request.method !== "POST") return json({ ok: false, error: "method not allowed" }, 405);
    if (!this.env.PRESENCE_ADMIN_TOKEN)
      return json({ ok: false, error: "presence enrollment is not configured" }, 503);
    if (!(await bearerEquals(request, this.env.PRESENCE_ADMIN_TOKEN)))
      return json({ ok: false, error: "unauthorized" }, 401);

    const credential = randToken();
    const hash = await presenceTokenHash(pack, dog, credential);
    // Credential rotation and lease revocation are one commit. Without the transaction, a process
    // death between the two writes can leave either the old token live or its old lease advertised.
    await this.state.storage.transaction(async (txn) => {
      await txn.put("auth:" + dog, { hash, enrolledAt: Date.now() });
      await txn.delete("lease:" + dog);
    });
    for (const ws of this._sockets()) {
      const a = this._attachment(ws);
      if (a.dog === dog) this._close(ws, 4003, "credential rotated");
    }
    await this._rescheduleAlarm();
    return presenceJson({ ok: true, pack, dog, credential });
  }

  async unenroll(request, pack, dog) {
    if (request.method !== "DELETE") return json({ ok: false, error: "method not allowed" }, 405);
    // Keep authorization at the mutation boundary as well as at the public front door.
    if (!this.env.PRESENCE_ADMIN_TOKEN)
      return json({ ok: false, error: "presence enrollment is not configured" }, 503);
    if (!(await bearerEquals(request, this.env.PRESENCE_ADMIN_TOKEN)))
      return json({ ok: false, error: "unauthorized" }, 401);
    // Idempotent retirement: authority and liveness disappear in the same commit.
    await this.state.storage.transaction(async (txn) => {
      await txn.delete("auth:" + dog);
      await txn.delete("lease:" + dog);
    });
    for (const ws of this._sockets()) {
      const a = this._attachment(ws);
      if (a.dog === dog) this._close(ws, 4003, "dog unenrolled");
    }
    await this._rescheduleAlarm();
    return presenceJson({ ok: true, pack, dog, enrolled: false });
  }

  async connect(request, pack, dog, session) {
    if (request.method !== "GET" ||
        (request.headers.get("Upgrade") || "").toLowerCase() !== "websocket")
      return json({ ok: false, error: "expected websocket" }, 426);
    if (!presenceID(session, 128, 8)) return json({ ok: false, error: "invalid session" }, 400);
    const authHash = await this._dogAuthHash(request, pack, dog);
    if (!authHash)
      return json({ ok: false, error: "unauthorized" }, 401);

    const pair = new WebSocketPair();
    const [client, server] = [pair[0], pair[1]];
    this.state.acceptWebSocket(server, ["presence"]);
    server.serializeAttachment({ kind: "presence", phase: "pending", pack, dog, session,
                                 authHash });
    // A connection is not online yet. Its first application frame must be the matching v1 hello;
    // this prevents a successful HTTP upgrade from becoming a lease before protocol negotiation.
    return new Response(null, { status: 101, webSocket: client });
  }

  async status(request, pack, dog) {
    if (request.method !== "GET") return json({ ok: false, error: "method not allowed" }, 405);
    if (!(await this._dogAuthorized(request, pack, dog)))
      return json({ ok: false, error: "unauthorized" }, 401);

    const now = Date.now();
    await this._expireLeases(now);
    const auth = await this.state.storage.list({ prefix: "auth:" });
    const leases = await this.state.storage.list({ prefix: "lease:" });
    const dogs = [];
    for (const key of auth.keys()) {
      const name = key.slice(5);
      const lease = leases.get("lease:" + name);
      const connected = !!(lease && lease.expiresAt > now);
      // A process that can heartbeat but has lost its Slack socket is useful diagnostic evidence,
      // not an addressable dog. Only an unexpired `ok` lease is advertised online.
      const online = connected && lease.health === "ok";
      dogs.push({
        dog: name,
        connected,
        online,
        health: connected ? lease.health : "offline",
        expires_at: connected ? Math.floor(lease.expiresAt / 1000) : 0,
      });
    }
    dogs.sort((a, b) => a.dog.localeCompare(b.dog));
    return presenceJson({ ok: true, pack, lease_ms: PRESENCE_LEASE_MS, dogs });
  }

  async webSocketMessage(ws, message) {
    const a = this._attachment(ws);
    if (a.kind !== "presence") return;
    let msg;
    try {
      if (typeof message !== "string" || message.length > 2048) throw new Error("bad frame");
      msg = JSON.parse(message);
    } catch (e) {
      this._close(ws, 4002, "invalid presence frame");
      return;
    }

    try {
      if (a.phase === "pending") {
        if (!this._matches(msg, a, "hello") || !presenceHealth(msg.health)) {
          this._close(ws, 4002, "expected matching hello");
          return;
        }
        await this._activate(ws, a, msg.health);
        return;
      }

      if (msg.t === "heartbeat") {
        if (!this._matches(msg, a, "heartbeat") || !presenceHealth(msg.health) ||
            !Number.isSafeInteger(msg.seq) || msg.seq <= 0) {
          await this._dropIfOwner(a);
          this._close(ws, 4002, "invalid heartbeat");
          return;
        }
        const key = "lease:" + a.dog;
        const outcome = await this.state.storage.transaction(async (txn) => {
          const lease = await txn.get(key);
          if (!lease || lease.session !== a.session || lease.owner !== a.owner)
            return { kind: "stale" };
          if (msg.seq <= lease.seq) {
            await txn.delete(key);
            return { kind: "replay" };
          }
          lease.seq = msg.seq;
          lease.health = msg.health;
          lease.expiresAt = Date.now() + PRESENCE_LEASE_MS;
          await txn.put(key, lease);
          return { kind: "accepted", expiresAt: lease.expiresAt };
        });
        if (outcome.kind === "stale") {
          this._close(ws, 4004, "stale session");
          return;
        }
        if (outcome.kind === "replay") {
          await this._rescheduleAlarm();
          this._close(ws, 4004, "stale heartbeat");
          return;
        }
        a.seq = msg.seq;
        ws.serializeAttachment(a);
        await this._ensureAlarm(outcome.expiresAt);
        this._send(ws, { t: "heartbeat_ack", v: 1, seq: msg.seq,
                         lease_ms: PRESENCE_LEASE_MS });
        return;
      }

      if (msg.t === "bye") {
        if (!this._matches(msg, a, "bye")) {
          await this._dropIfOwner(a);
          this._close(ws, 4002, "invalid bye");
          return;
        }
        await this._dropIfOwner(a);
        this._close(ws, 1000, "bye");
        return;
      }
      await this._dropIfOwner(a);
      this._close(ws, 4002, "unknown presence frame");
    } catch (e) {
      // Storage is the authority. If it cannot be updated, stop accepting heartbeats rather than
      // displaying an online state we failed to durably fence.
      this._close(ws, 1011, "presence unavailable");
    }
  }

  async _activate(ws, a, health) {
    // Enrollment may rotate a credential after HTTP upgrade but before hello. Bind the socket to
    // the verifier generation it authenticated with and refuse to resurrect a rotated lease.
    const owner = randToken();                    // server-only connection generation
    const expiresAt = Date.now() + PRESENCE_LEASE_MS;
    const activated = await this.state.storage.transaction(async (txn) => {
      const auth = await txn.get("auth:" + a.dog);
      if (!auth || !auth.hash || !constantTextEqual(auth.hash, a.authHash)) return false;
      await txn.put("lease:" + a.dog,
        { session: a.session, owner, seq: 0, health, expiresAt });
      return true;
    });
    if (!activated) {
      this._close(ws, 4003, "credential rotated");
      return;
    }
    const active = { ...a, phase: "active", owner, seq: 0 };
    ws.serializeAttachment(active);

    // The put above is the linearization point. Any old socket that races after it reads a different
    // owner and cannot renew or delete this lease, even if close delivery is delayed.
    for (const other of this._sockets()) {
      if (other === ws) continue;
      const old = this._attachment(other);
      if (old.dog === a.dog) this._close(other, 4004, "replaced by newer session");
    }
    await this._ensureAlarm(expiresAt);
    this._send(ws, { t: "hello_ack", v: 1, lease_ms: PRESENCE_LEASE_MS });
  }

  _matches(msg, a, type) {
    return !!msg && msg.t === type && msg.v === 1 &&
      msg.pack === a.pack && msg.dog === a.dog && msg.session === a.session;
  }

  async _dogAuthHash(request, pack, dog) {
    const token = bearerToken(request);
    if (!token) return "";
    const row = await this.state.storage.get("auth:" + dog);
    if (!row || !row.hash) return "";
    const got = await presenceTokenHash(pack, dog, token);
    return constantTextEqual(row.hash, got) ? row.hash : "";
  }

  async _dogAuthorized(request, pack, dog) {
    return !!(await this._dogAuthHash(request, pack, dog));
  }

  _sockets() {
    try { return this.state.getWebSockets("presence") || []; } catch (e) { return []; }
  }

  _attachment(ws) {
    try { return ws.deserializeAttachment() || {}; } catch (e) { return {}; }
  }

  _send(ws, obj) {
    try { ws.send(JSON.stringify(obj)); } catch (e) {}
  }

  _close(ws, code, reason) {
    try { ws.close(code, reason); } catch (e) {}
  }

  async _dropIfOwner(a) {
    if (!a || a.phase !== "active") return false;
    const key = "lease:" + a.dog;
    const dropped = await this.state.storage.transaction(async (txn) => {
      const lease = await txn.get(key);
      if (!lease || lease.session !== a.session || lease.owner !== a.owner) return false;
      await txn.delete(key);
      return true;
    });
    if (!dropped) return false;
    await this._rescheduleAlarm();
    return true;
  }

  async webSocketClose(ws) {
    try { await this._dropIfOwner(this._attachment(ws)); } catch (e) {}
  }

  async webSocketError(ws) {
    try { await this._dropIfOwner(this._attachment(ws)); } catch (e) {}
  }

  async alarm() {
    await this._expireLeases(Date.now());
    await this._rescheduleAlarm();
  }

  async _expireLeases(now) {
    const leases = await this.state.storage.list({ prefix: "lease:" });
    for (const [key, seen] of leases) {
      if (!seen || seen.expiresAt > now) continue;
      // Re-read and conditionally delete in one transaction: an alarm can target an earlier
      // generation, and a fresh heartbeat/new session must survive even if it lands concurrently.
      const current = await this.state.storage.transaction(async (txn) => {
        const value = await txn.get(key);
        if (!value || value.owner !== seen.owner || value.expiresAt > now) return null;
        await txn.delete(key);
        return value;
      });
      if (!current) continue;
      const dog = key.slice(6);
      for (const ws of this._sockets()) {
        const a = this._attachment(ws);
        if (a.dog === dog && a.owner === current.owner)
          this._close(ws, 4001, "lease expired");
      }
    }
  }

  async _ensureAlarm(expiresAt) {
    const current = await this.state.storage.getAlarm();
    if (current == null || expiresAt < current) await this.state.storage.setAlarm(expiresAt);
  }

  async _rescheduleAlarm() {
    const leases = await this.state.storage.list({ prefix: "lease:" });
    let next = null;
    for (const [, lease] of leases) {
      if (lease && Number.isFinite(lease.expiresAt))
        next = next == null ? lease.expiresAt : Math.min(next, lease.expiresAt);
    }
    if (next == null) await this.state.storage.deleteAlarm();
    else await this.state.storage.setAlarm(next);
  }
}

// ---------------------------------------------------------------- helpers
function presenceID(value, max, min = 1) {
  return typeof value === "string" && value.length >= min && value.length <= max &&
    /^[A-Za-z0-9][A-Za-z0-9_.:@-]*$/.test(value);
}

function presenceHealth(value) {
  return typeof value === "string" && PRESENCE_HEALTH.has(value);
}

function presenceJson(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function bearerToken(request) {
  const m = (request.headers.get("Authorization") || "").match(/^Bearer ([\x21-\x7e]+)$/);
  return m ? m[1] : "";
}

function constantTextEqual(a, b) {
  a = String(a || ""); b = String(b || "");
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function bearerEquals(request, expected) {
  const got = bearerToken(request);
  if (!got || !expected) return false;
  // Compare fixed-width digests so the equality loop does not reveal which byte of an operator
  // credential differed. The token is still expected to be a high-entropy Worker secret.
  return constantTextEqual(await sha256hex(got), await sha256hex(String(expected)));
}

async function presenceTokenHash(pack, dog, token) {
  // Domain separation prevents a copied verifier row from authorizing another dog or another pack.
  return sha256hex("collie-presence-v1\0" + pack + "\0" + dog + "\0" + token);
}

async function sha256hex(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}
function randToken() {
  const a = new Uint8Array(32); crypto.getRandomValues(a);
  return btoa(String.fromCharCode(...a)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
/**
 * The bearer token APNs wants: an ES256 JWT signed with the team's .p8 key.
 *
 * Cached because APNs REFUSES a token minted more than once every 20 minutes (TooManyProviderTokenUpdates)
 * and rejects one older than an hour — so the window is genuinely narrow at both ends, and a fresh
 * signature per notification is a way to get throttled rather than a way to be safe.
 *
 * WebCrypto's ECDSA signature is already the raw r‖s pair a JWT wants; there is no DER to unwrap.
 */
let apnsCache = { jwt: "", at: 0, kid: "" };
async function apnsJWT(pem, keyID, teamID) {
  const now = Math.floor(Date.now() / 1000);
  if (apnsCache.jwt && apnsCache.kid === keyID && now - apnsCache.at < 1800) return apnsCache.jwt;

  const body = pem.replace(/-----[A-Z ]+-----/g, "").replace(/\s+/g, "");
  const key = await crypto.subtle.importKey(
    "pkcs8", b64ToBytes(body).buffer,
    { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);

  const enc = (obj) => b64url(new TextEncoder().encode(JSON.stringify(obj)));
  const signing = enc({ alg: "ES256", kid: keyID }) + "." + enc({ iss: teamID, iat: now });
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key,
                                       new TextEncoder().encode(signing));
  const jwt = signing + "." + b64url(new Uint8Array(sig));
  apnsCache = { jwt, at: now, kid: keyID };
  return jwt;
}

function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function shortUA(ua) {
  if (/iPhone|iPad/.test(ua)) return "iPhone/iPad";
  if (/Android/.test(ua)) return "Android";
  if (/Macintosh/.test(ua)) return "Mac";
  if (/Windows/.test(ua)) return "Windows";
  return "device";
}
function b64ToBytes(b64) {
  const bin = atob(b64); const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function bytesToB64(bytes) {
  let bin = ""; for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
async function readBodyLimited(request, limit) {
  const declared = request.headers.get("Content-Length");
  if (declared !== null) {
    const size = Number(declared);
    if (!Number.isFinite(size) || size < 0 || size > limit) return null;
  }
  if (!request.body) return "";
  const reader = request.body.getReader();
  const decoder = new TextDecoder();
  let total = 0;
  let result = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      try { await reader.cancel(); } catch (e) {}
      return null;
    }
    result += decoder.decode(value, { stream: true });
  }
  return result + decoder.decode();
}
function tokenOf(request) {
  const auth = request.headers.get("Authorization") || "";
  const bearer = auth.match(/^Bearer\s+([A-Za-z0-9_\-]+)$/);
  if (bearer) return bearer[1];
  const cookie = (request.headers.get("Cookie") || "").match(/collie_sess=([A-Za-z0-9_\-]+)/);
  return cookie ? cookie[1] : null;
}

function cleanDeviceHashes(values, blocked = new Set()) {
  const clean = [];
  const seen = new Set();
  for (const value of Array.isArray(values) ? values : []) {
    const hash = String(value || "");
    if (!DEVICE_HASH_RE.test(hash) || blocked.has(hash) || seen.has(hash)) continue;
    clean.push(hash);
    seen.add(hash);
    if (clean.length >= MAX_DEVICES) break;
  }
  return clean;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: {
    "content-type": "application/json", "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  } });
}
function offlinePage() {
  return new Response(
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" +
    "<title>Collie offline</title><body style='font:16px system-ui;padding:2rem;background:#0f1220;color:#c9d1e6'>" +
    "<h2>桌面 Collie 未在线</h2><p>请在电脑上运行 <code>collie web --remote</code>，然后刷新本页。" +
    "已配对的设备会自动恢复，无需重新配对。</p>",
    { status: 503, headers: {
      "content-type": "text/html; charset=utf-8", "cache-control": "no-store",
      "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
      "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
    } });
}
/* wrangler.toml:
 *   name = "collie-relay"
 *   main = "worker.js"
 *   compatibility_date = "2026-01-01"
 *   [[durable_objects.bindings]]
 *     name = "RELAY"; class_name = "RelayRoom"
 *   [[migrations]]
 *     tag = "v1"; new_sqlite_classes = ["RelayRoom"]
 */
