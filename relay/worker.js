/**
 * Collie Remote — relay Worker (Cloudflare) + RelayRoom Durable Object.  v1.
 *
 * One DO per room. Desktop dials in over WSS (/relay/agent) and stays connected; phone hits
 * /r/<room>/* over HTTPS; the DO multiplexes each phone request onto the agent WS, streaming the
 * response (incl. SSE) back into the phone's Response body.
 *
 * v1 over v0:
 *  - DURABLE pairing: the DESKTOP is the source of truth for paired devices. On connect it sends the
 *    set of paired session-token *hashes*; returning phones validate against that with no re-pairing,
 *    so pairing survives a desktop restart / being offline for a day. New devices pair with a code,
 *    the DO mints a session token and tells the desktop the hash to persist (device_added).
 *  - AGENTKEY claim: first agent to a room stores sha256(key) in DO storage (persists across evictions
 *    and desktop downtime); later agents must match — stops room impersonation / free-relay abuse.
 *  - /pair rate-limit + lockout (5 / 10 min) — the only brute-force defence now that there's no
 *    human-in-the-loop.
 *  - /api/remote/* is never forwarded to a phone — pairing is managed only at the desktop.
 *
 * NOT doing E2E: both legs are TLS (HTTPS + WSS), so no network middle-man can read traffic; only
 * Cloudflare can, which the owner accepts. Zero-knowledge-vs-Cloudflare is out of scope by decision.
 */

const HOP = new Set(["connection","keep-alive","transfer-encoding","upgrade","te","trailer",
                     "proxy-authenticate","proxy-authorization","content-length","host"]);
const PAIR_WINDOW_MS = 10 * 60 * 1000;
const PAIR_MAX = 5;
// How long a pairing request stays approvable. Long enough for someone to walk to the desk, short
// enough that a request abandoned on a shared screen does not stay live.
const PAIR_APPROVE_MS = 3 * 60 * 1000;

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
    // pair rate-limit timestamps (in-memory; the DO stays alive during any attack burst)
    // Pending pair approvals live in DO STORAGE ("pend:<ticket>"), not here — see pair(). A request
    // that is waiting for a human is precisely the state most likely to outlive an eviction.
    this.pendingE2E = new Map();   // in-flight E2E handshakes: id -> resolve (one request each, short-lived)
  }

  // Hibernation: an idle room is evicted from memory (→ no duration billing) while its WebSocket stays
  // open at the edge. So the agent socket + its pairing state must survive eviction: find the socket via
  // getWebSockets("agent"), and stash {paircode, devices} on it with serializeAttachment (persisted),
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
    // the desktop's X25519 PUBLIC key, so a phone can bind its confirm tag to the full transcript
    // before pairing. Public by definition; the pairing code is what authenticates it.
    if (rest === "/e2e" && request.method === "GET") {
      const agent = this._agent();
      if (!agent) return json({ error: "desktop offline" }, 503);
      return json({ pub: this._astate(agent).e2ePub || "" });
    }
    // Phase two: the phone shows the number and asks here until the desktop has decided. Short
    // polls, so nothing depends on a connection staying open while a human makes up their mind.
    if (rest === "/pair/wait" && request.method === "GET") {
      return this.pairWait(url.searchParams.get("ticket") || "", request);
    }
    // phones get the dedicated mobile client, not the desktop index — room root → desktop /m
    const fwd = (rest === "/" || rest === "") ? "/m" : rest;
    return this.proxyToAgent(request, fwd, url.search);
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
    server.serializeAttachment({ paircode: null, devices: [] });
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
  }

  webSocketMessage(ws, message) {
    let msg;
    try { msg = JSON.parse(typeof message === "string" ? message : ""); } catch (e) { return; }
    // pairing state lives on the socket attachment (survives hibernation), not in memory
    if (msg.t === "hello") {
      // `approve` is a CAPABILITY the desktop declares, not a policy the relay imposes. Desktops
      // already installed do not send it, and for them pairing keeps working exactly as before — a
      // relay that started demanding an approval nobody could give would lock every existing
      // install out of its own phone. New desktops opt in and get the confirmation step.
      this._setAstate(ws, { paircode: (msg.paircode || "").toUpperCase(), devices: msg.devices || [],
                            e2ePub: msg.e2ePub || "", approve: !!msg.approve });
      return;
    }
    if (msg.t === "e2e_pair_result") {
      const resolve = this.pendingE2E.get(msg.id);
      if (resolve) resolve({ ok: !!msg.ok, pub: msg.pub, confirm: msg.confirm, error: msg.error });
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
      (async () => {
        const ticket = await this.state.storage.get("rq:" + msg.id);
        if (!ticket) return;
        const v = await this.state.storage.get("pend:" + ticket);
        if (!v) return;
        v.state = msg.ok ? "approved" : "denied";
        v.error = msg.error || "";
        await this.state.storage.put("pend:" + ticket, v);
      })().catch(() => {});
      return;
    }
    if (msg.t === "devices") { const s = this._astate(ws); s.devices = msg.devices || []; this._setAstate(ws, s); return; }
    if (msg.t === "paircode") { const s = this._astate(ws); s.paircode = (msg.paircode || "").toUpperCase(); this._setAstate(ws, s); return; }
    if (msg.t === "e2ePub") { const s = this._astate(ws); s.e2ePub = msg.e2ePub || ""; this._setAstate(ws, s); return; }
    const slot = this.pending.get(msg.id);
    if (!slot) return;
    if (msg.t === "res") {
      slot.status = msg.status; slot.headers = msg.headers || {}; slot.opened = true;
      if (msg.enc) {
        // sealed: the real status/headers are inside, so stream the frame and answer 200 octet-stream
        try {
          slot.controller.enqueue(new TextEncoder().encode(
            JSON.stringify({ enc: msg.enc, seq: msg.seq || 0 }) + "\n"));
        } catch (e) {}
      }
      slot.headResolve();
    } else if (msg.t === "chunk") {
      // an E2E chunk is opaque: forward the sealed envelope as one length-prefixed record so the
      // phone can tell frames apart without us understanding any of them
      try {
        if (msg.enc) {
          const line = new TextEncoder().encode(JSON.stringify({ enc: msg.enc, seq: msg.seq }) + "\n");
          slot.controller.enqueue(line);
        } else {
          slot.controller.enqueue(b64ToBytes(msg.data));
        }
      } catch (e) {}
    } else if (msg.t === "end") {
      try { slot.controller.close(); } catch (e) {}
      this.pending.delete(msg.id);
    } else if (msg.t === "err") {
      if (!slot.opened) slot.headReject(new Error(msg.msg || "agent error"));
      else { try { slot.controller.error(new Error(msg.msg || "agent error")); } catch (e) {} }
      this.pending.delete(msg.id);
    }
  }

  // ---------------------------------------------------------------- phone side
  async pair(request) {
    const agent = this._agent();
    if (!agent) return json({ ok: false, error: "desktop offline" }, 503);
    const now = Date.now();
    this.pairAttempts = this.pairAttempts.filter((t) => now - t < PAIR_WINDOW_MS);
    if (this.pairAttempts.length >= PAIR_MAX)
      return json({ ok: false, error: "too many attempts — wait a few minutes" }, 429);

    let body = {};
    try { body = await request.json(); } catch (e) {}
    const st = this._astate(agent);
    const code = (body.paircode || "").toUpperCase();
    if (!st.paircode || code !== st.paircode) {
      this.pairAttempts.push(now);
      return json({ ok: false, error: "bad pairing code" }, 403);
    }
    this.pairAttempts = [];                              // reset on success
    // BURN the code the instant it matches — one-shot enforced at the relay, not dependent on the
    // desktop's async rotate. Closes the sub-second window where a leaked code redeemed twice. The
    // current pairing continues via the E2E handshake below (which uses body.pub/confirm, not the code).
    st.paircode = null;
    this._setAstate(agent, st);

    // E2E (optional, opt-in per client): the phone sends its X25519 public key and an HMAC over the
    // transcript keyed by the pairing code. We cannot check that tag — we do not know the code, which
    // is exactly the point — so we hand it to the desktop, which verifies it and answers with its own
    // key and tag. A relay that swapped either key cannot produce a matching tag.
    let e2e = null;
    if (body.pub && body.confirm) {
      const rid = ++this.seq;
      const wait = new Promise((resolve) => { this.pendingE2E.set(rid, resolve); });
      this.sendAgent(agent, { t: "e2e_pair", id: rid, device_id: String(body.device_id || ""),
                              pub: String(body.pub), confirm: String(body.confirm) });
      e2e = await Promise.race([wait, new Promise((r) => setTimeout(() => r(null), 15000))]);
      this.pendingE2E.delete(rid);
      if (!e2e || !e2e.ok) {
        this.pairAttempts.push(now);
        return json({ ok: false, error: (e2e && e2e.error) || "e2e handshake refused" }, 403);
      }
    }

    // ASK THE DESKTOP. The pairing code proves someone saw the screen at some point; it cannot
    // prove they are at the machine now. Anyone who caught it over a shoulder, in a screenshot, in
    // a screen share or in a recording holds a working credential — and one scan buys every /api/*
    // on that desktop: run commands, read and write files, drive the logged-in browser. So the
    // desktop confirms, with a number shown at BOTH ends, so the person approving knows which
    // request they are approving and not another one arriving at the same moment.
    // Two phases, not one held request.
    //
    // The number only means anything if BOTH ends can see it — that is the whole point: whoever
    // approves is confirming a specific request, not blessing whatever happened to arrive. A POST
    // that returns only after the decision cannot show the phone anything to compare. And holding
    // an HTTP request open for two minutes across a Worker, a Durable Object, a mobile network and
    // whatever proxy sits in between is a good way to have it dropped somewhere in the middle,
    // leaving the phone with an error while the desktop believes it approved.
    //
    // So: answer immediately with the number and a ticket, let the phone show the number and poll.
    // Any single step can time out and be retried without the pairing ending up half-done.
    if (st.approve) {
      await this.sweepPending();
      // RANDOM, not ++this.seq. The counter restarts at zero every time an idle room is evicted and
      // woken, so ids repeat across abandoned requests and a decision cannot tell them apart.
      const rid = randToken();
      const num = String(Math.floor(Math.random() * 9000) + 1000);
      const ticket = randToken();
      // DO STORAGE, not an in-memory Map. This file's own header warns that an idle room is
      // evicted and the constructor runs again on wake — and a pairing request is defined by
      // waiting for a human, so it is exactly the state most likely to span an eviction. Stored in
      // memory it survived the POST and was gone by the first poll seconds later.
      await this.state.storage.put("pend:" + ticket, {
        rid, num, at: now, state: "pending",
        device_id: String(body.device_id || ""), body, e2e,
      });
      await this.state.storage.put("rq:" + rid, ticket);   // decision -> ticket, without a scan
      this.sendAgent(agent, {
        t: "pair_request", id: rid, num,
        device_id: String(body.device_id || ""),
        name: String(body.name || shortUA(request.headers.get("User-Agent") || "")).slice(0, 60),
      });
      return json({ ok: false, pending: true, num, ticket }, 202);
    }

    return this.issueToken(agent, st, body, e2e, request);
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

  /** Mint the session token and tell the desktop. Shared by the approve-first and the legacy path. */
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
    if (p.state === "pending") return json({ ok: false, pending: true, num: p.num }, 202);

    await this.state.storage.delete([key, "rq:" + p.rid]);
    if (p.state !== "approved") {
      this.pairAttempts.push(Date.now());
      return json({ ok: false, error: p.error || "the desktop refused this device" }, 403);
    }
    const agent = this._agent();
    if (!agent) return json({ ok: false, error: "desktop offline" }, 503);
    return this.issueToken(agent, this._astate(agent), p.body, p.e2e, request);
  }

  async issueToken(agent, st, body, e2e, request) {
    const token = randToken();
    const hash = await sha256hex(token);
    st.devices = [...(st.devices || []), hash];          // optimistic; the desktop's refresh confirms it
    this._setAstate(agent, st);
    const name = String(body.name || shortUA(request.headers.get("User-Agent") || "")).slice(0, 60);
    // device_id: a client-supplied STABLE id (localStorage / Keychain) so re-pairing the same client
    // updates its device row instead of duplicating. device_added carries it + the token hash + name.
    this.sendAgent(agent, { t: "device_added", device_id: String(body.device_id || ""), hash, name });
    // token in the body too → a NATIVE app (no cookie jar) stores it in the Keychain and sends it as
    // `Authorization: Bearer <token>`. A browser/WKWebView just uses the Secure cookie below.
    const payload = { ok: true, token };
    if (e2e) { payload.pub = e2e.pub; payload.confirm = e2e.confirm; }
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: {
        "content-type": "application/json",
        // 1-year cookie → durable; real lifetime is bounded by the desktop still trusting this hash.
        "set-cookie": `collie_sess=${token}; HttpOnly; Secure; SameSite=Lax; Path=/r/; Max-Age=31536000`,
      },
    });
  }

  async proxyToAgent(request, path, search) {
    const agent = this._agent();
    if (!agent) return offlinePage();
    if (path.startsWith("/api/remote/")) return json({ error: "pairing is managed on the desktop" }, 403);

    const needsAuth = path.startsWith("/api/");
    if (needsAuth && !(await this.checkSession(request, agent)))
      return json({ error: "not paired" }, 401);

    const id = ++this.seq;
    let bodyB64 = null, hasBody = false;
    if (request.method !== "GET" && request.method !== "HEAD") {
      const buf = new Uint8Array(await request.arrayBuffer());
      if (buf.length) { hasBody = true; bodyB64 = bytesToB64(buf); }
    }
    const headers = {};
    for (const [k, v] of request.headers) if (!HOP.has(k.toLowerCase())) headers[k] = v;

    const stream = new ReadableStream({
      start: (controller) => {
        const slot = { controller, opened: false };
        slot.headPromise = new Promise((res, rej) => { slot.headResolve = res; slot.headReject = rej; });
        this.pending.set(id, slot);
        // An E2E client sends its whole request sealed in the `X-Collie-Enc` header: we forward the
        // ciphertext and never learn the method, path, headers or body — only what routing needs
        // (room, id, session, seq). A plaintext client is unchanged.
        const enc = request.headers.get("X-Collie-Enc");
        if (enc) {
          this.sendAgent(agent, { t: "req", id, cid: request.headers.get("X-Collie-Rid") || "1",
                                  session: sessionOf(request), enc, seq: 0 });
        } else {
          this.sendAgent(agent, { t: "req", id, session: "s1", method: request.method, path: path + search, headers, hasBody });
          if (hasBody) { this.sendAgent(agent, { t: "body", id, data: bodyB64 }); this.sendAgent(agent, { t: "body_end", id }); }
        }
      },
    });

    const slot = this.pending.get(id);
    try { await slot.headPromise; }
    catch (e) { return json({ error: String((e && e.message) || e) }, 502); }

    const respHeaders = new Headers();
    for (const [k, v] of Object.entries(slot.headers)) if (!HOP.has(k.toLowerCase())) respHeaders.set(k, v);
    const ct = slot.headers["content-type"] || slot.headers["Content-Type"] || "";
    if (ct.includes("text/html")) {
      const room = new URL(request.url).pathname.split("/")[2];
      return injectBase(stream, respHeaders, slot.status, `/r/${room}/`);
    }
    return new Response(stream, { status: slot.status, headers: respHeaders });
  }

  async checkSession(request, agent) {
    let tok = null;
    const auth = request.headers.get("Authorization") || "";
    const mb = auth.match(/^Bearer\s+([A-Za-z0-9_\-]+)$/);   // native app path
    if (mb) tok = mb[1];
    if (!tok) {
      const m = (request.headers.get("Cookie") || "").match(/collie_sess=([A-Za-z0-9_\-]+)/);  // browser/WKWebView
      if (m) tok = m[1];
    }
    if (!tok) return false;
    const devices = this._astate(agent).devices || [];
    return devices.includes(await sha256hex(tok));
  }

  sendAgent(agent, obj) { try { agent.send(JSON.stringify(obj)); } catch (e) {} }
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
// The phone names the session it is talking about so both ends derive the same K_sess. Missing means
// a plaintext client, which has no session keys at all.
function sessionOf(request) {
  return request.headers.get("X-Collie-Session") || "s1";
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
// The SPA uses ABSOLUTE same-origin URLs (fetch("/api/..."), new EventSource(...)), which ignore
// <base>. So we inject <base> + a shim wrapping fetch/EventSource/XHR to prefix "/..." with the room
// base, plus a pairing bootstrap (redeem #<paircode> → cookie → reload). webui/index.html unchanged.
function injectBase(stream, headers, status, base) {
  const reader = stream.getReader();
  const shim =
    `<base href="${base}"><script>(function(){var B=${JSON.stringify(base)};` +
    `function fix(u){try{if(typeof u==='string'&&u[0]==='/'&&u.indexOf(B)!==0)return B+u.slice(1);}catch(e){}return u;}` +
    `var of=window.fetch;window.fetch=function(u,o){return of.call(this,fix(u),o);};` +
    `var OE=window.EventSource;window.EventSource=function(u,o){return new OE(fix(u),o);};` +
    `var ox=window.XMLHttpRequest&&window.XMLHttpRequest.prototype.open;` +
    `if(ox)window.XMLHttpRequest.prototype.open=function(m,u){arguments[1]=fix(u);return ox.apply(this,arguments);};` +
    `var pc=(location.hash||'').replace(/^#/,'');` +
    `if(pc){document.documentElement.style.visibility='hidden';` +
    `var did=localStorage.getItem('collie_did');if(!did){did=(self.crypto&&crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random());localStorage.setItem('collie_did',did);}` +
    `var ua=navigator.userAgent,nm=/iPhone|iPad/.test(ua)?'iPhone':/Android/.test(ua)?'Android':/Mac/.test(ua)?'Mac':'device';` +
      `of(B+'pair',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({paircode:pc,device_id:did,name:nm})})` +
    `.then(function(r){return r.json();}).then(function(d){` +
    // Two phases. A 202 means the desktop has been asked and is holding this: show the number so
    // the person at the computer can check it matches THIS phone, then poll until they answer.
    // Short polls, so nothing depends on a connection staying open while a human decides.
    `if(d&&d.pending&&d.ticket){show(d.num);poll(d.ticket);return;}` +
    `if(d&&d.ok){location.replace(B);return;}fail(d&&d.error);})` +
    `.catch(function(){location.reload();});` +
    `function show(n){document.documentElement.style.visibility='';document.body.innerHTML=` +
    `'<div style=\\'font:16px/1.6 system-ui;padding:2.5rem 1.5rem;text-align:center\\'>' +` +
    `'<p style=\\'opacity:.7\\'>Approve this on your computer</p>' +` +
    `'<p style=\\'font-size:44px;font-weight:700;letter-spacing:.14em;margin:.4em 0\\'>'+n+'</p>' +` +
    `'<p style=\\'opacity:.7\\'>Check the same number is showing there.</p></div>';}` +
    `function fail(m){document.documentElement.style.visibility='';document.body.innerHTML=` +
    `'<p style=\\'font:16px system-ui;padding:2rem\\'>'+(m||'Pairing failed or the link expired — get a fresh one on your computer.')+'</p>';}` +
    `function poll(tk){of(B+'pair/wait?ticket='+encodeURIComponent(tk)).then(function(r){return r.json();})` +
    `.then(function(d){if(d&&d.ok){location.replace(B);return;}` +
    `if(d&&d.pending){setTimeout(function(){poll(tk);},1500);return;}fail(d&&d.error);})` +
    `.catch(function(){setTimeout(function(){poll(tk);},2500);});}}` +
    `})();</script>`;
  const out = new ReadableStream({
    async pull(controller) {
      const chunks = []; let total = 0;
      while (true) { const { done, value } = await reader.read(); if (done) break; chunks.push(value); total += value.length; }
      let html = new TextDecoder().decode(concat(chunks, total));
      // index.html references assets by ABSOLUTE path (src="/logo.svg", href="/map", url(/..)).
      // <base> only affects RELATIVE urls and the JS shim only catches fetch/XHR/EventSource, so strip
      // the leading slash → the <base href="/r/<room>/"> resolves them under the room. (Skip "//host".)
      html = html.replace(/(\s(?:src|href)=")\/(?!\/)/g, "$1").replace(/(url\()\/(?!\/)/g, "$1");
      html = html.includes("<head>") ? html.replace("<head>", "<head>" + shim) : shim + html;
      controller.enqueue(new TextEncoder().encode(html));
      controller.close();
    },
  });
  headers.delete("content-length");
  return new Response(out, { status, headers });
}
function concat(chunks, total) {
  const out = new Uint8Array(total); let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
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
