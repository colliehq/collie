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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const p = url.pathname;
    let room = null;
    if (p === "/relay/agent") room = url.searchParams.get("room");
    else if (p.startsWith("/r/")) room = p.split("/")[2] || null;
    if (!room) return new Response("collie relay", { status: 200 });
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
    // Pending pair approvals live in DO STORAGE ("pend:<ticket>"), not here — see pair(). A request
    // that is waiting for a human is precisely the state most likely to outlive an eviction.
    this.claimingTickets = new Set(); // fallback single-instance guard; storage transaction is authoritative
  }

  // Hibernation: an idle room is evicted from memory (→ no duration billing) while its WebSocket stays
  // open at the edge. So the agent socket + its pairing state must survive eviction: find the socket via
  // getWebSockets("agent"), and stash protocol/device hashes on it with serializeAttachment (persisted),
  // instead of in-memory fields the constructor would wipe on wake.
  _agent() { const ws = this.state.getWebSockets("agent"); return ws.length ? ws[0] : null; }
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
    const keyHash = await sha256hex(key);
    const stored = await this.state.storage.get("agentKeyHash");
    if (!stored) await this.state.storage.put("agentKeyHash", keyHash);   // first claim wins
    else if (stored !== keyHash) return new Response("bad agent key", { status: 403 });

    const pair = new WebSocketPair();
    const [client, server] = [pair[0], pair[1]];
    this.state.acceptWebSocket(server, ["agent"]);        // hibernatable — an idle room costs ~nothing
    server.serializeAttachment({ protocol: 0, e2eRequired: false, devices: [] });
    return new Response(null, { status: 101, webSocket: client });
  }

  // ---- hibernation handlers (called by the runtime, survive DO eviction) ----
  webSocketClose(ws) { this._dropPending("agent disconnected"); }
  webSocketError(ws) { this._dropPending("agent error"); }
  _dropPending(why) {
    for (const [, slot] of this.pending) {
      try { slot.opened ? slot.controller.error(new Error(why)) : slot.headReject(new Error(why)); } catch (e) {}
    }
    this.pending.clear();
    for (const [, waiter] of this.revokeWaiters) waiter.resolve(false);
    this.revokeWaiters.clear();
  }

  async webSocketMessage(ws, message) {
    let msg;
    try { msg = JSON.parse(typeof message === "string" ? message : ""); } catch (e) { return; }
    // pairing state lives on the socket attachment (survives hibernation), not in memory
    if (msg.t === "hello") {
      // Never serialize unknown hello fields: an accidental future `paircode` field must not become
      // durable relay state.  v1 agents remain visibly incompatible instead of downgrading.
      const tombstones = await this.revokeTombstones();
      const blocked = new Set(tombstones.keys());
      this._setAstate(ws, { protocol: Number(msg.v || 0),
                            devices: (msg.devices || []).filter((hash) => !blocked.has(hash)),
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
      s.devices = (msg.devices || []).filter((hash) => !blocked.has(hash));
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
      try { slot.controller.close(); } catch (e) {}
      this.pending.delete(msg.id);
    } else if (msg.t === "err") {
      if (!slot.opened) slot.headReject(new Error(msg.msg || "agent error"));
      else { try { slot.controller.error(new Error(msg.msg || "agent error")); } catch (e) {} }
      this.pending.delete(msg.id);
    }
  }

  _failSlot(id, slot, why) {
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
      const response = await this.issueToken(agent, this._astate(agent), p.body, p.e2e, request);
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

  async issueToken(agent, st, body, e2e, request) {
    const token = randToken();
    const hash = await sha256hex(token);
    st.devices = [...(st.devices || []), hash];          // optimistic; the desktop's refresh confirms it
    this._setAstate(agent, st);
    const name = String(body.name || shortUA(request.headers.get("User-Agent") || "")).slice(0, 60);
    // device_id: a client-supplied STABLE id (localStorage / Keychain) so re-pairing the same client
    // updates its device row instead of duplicating. device_added carries it + the token hash + name.
    if (!this.sendAgent(agent, {
      t: "device_added", device_id: String(body.device_id || ""), hash, name,
    })) {
      // The phone has not received the token yet. Roll the optimistic edge allowlist back and fail
      // this one-shot ticket instead of issuing a credential the desktop never learned about.
      st.devices = (st.devices || []).filter((candidate) => candidate !== hash);
      this._setAstate(agent, st);
      throw new Error("desktop disconnected before storing device");
    }
    // token in the body too → a NATIVE app (no cookie jar) stores it in the Keychain and sends it as
    // `Authorization: Bearer <token>`. A browser/WKWebView just uses the Secure cookie below.
    if (!e2e || !e2e.pub || !e2e.confirm) throw new Error("missing authenticated E2E result");
    const payload = { ok: true, token, pub: e2e.pub, confirm: e2e.confirm };
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: {
        "content-type": "application/json",
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
    const enc = await readBodyLimited(request, SEALED_ENVELOPE_MAX);
    const cid = request.headers.get("X-Collie-Rid") || "";
    const session = request.headers.get("X-Collie-Session") || "s1";
    if (!enc || !cid || cid.length > 128 || session.length > 256)
      return json({ error: "a valid sealed envelope is required" }, 400);
    try {
      const parsed = JSON.parse(enc);
      if (!parsed || typeof parsed.n !== "string" || typeof parsed.ct !== "string") throw new Error();
    } catch (e) {
      return json({ error: "malformed sealed envelope" }, 400);
    }
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
        // Only opaque routing fields cross the relay/desktop boundary.  No URL, query, method,
        // application headers or body is available here to log or inspect.
        if (!this.sendAgent(agent, { t: "req", id, cid, session, enc, seq: 0 })) {
          // `ReadableStream.start` runs synchronously, before proxySealed can await headPromise.
          // Resolve that local hand-off with an explicit error marker; rejecting here would briefly
          // create an unhandled promise and strict runtimes terminate before the 502 is returned.
          slot.dispatchError = "agent disconnected before request dispatch";
          try { controller.close(); } catch (e) {}
          slot.headResolve();
        }
      },
    });

    try { await slot.headPromise; }
    catch (e) { return json({ error: String((e && e.message) || e) }, 502); }
    if (slot.dispatchError) {
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
    const body = await request.json().catch(() => null);
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
    const stale = [];
    for (const [key, row] of rows) {
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

// ---------------------------------------------------------------- helpers
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

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}
function offlinePage() {
  return new Response(
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" +
    "<title>Collie offline</title><body style='font:16px system-ui;padding:2rem;background:#0f1220;color:#c9d1e6'>" +
    "<h2>桌面 Collie 未在线</h2><p>请在电脑上运行 <code>collie web --remote</code>，然后刷新本页。" +
    "已配对的设备会自动恢复，无需重新配对。</p>",
    { status: 503, headers: { "content-type": "text/html; charset=utf-8" } });
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
