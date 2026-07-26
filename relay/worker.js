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
    this.agent = null;
    this.paircode = null;          // current code for adding a NEW device (from agent hello)
    this.valid = new Set();        // valid session-token hashes (hello set + this-session pairings)
    this.seq = 0;
    this.pending = new Map();
    this.pairAttempts = [];        // timestamps, for rate-limit
  }

  async fetch(request) {
    const url = new URL(request.url);
    const p = url.pathname;
    if (p === "/relay/agent") return this.acceptAgent(request);
    const rest = p.replace(/^\/r\/[^/]+/, "") || "/";
    if (rest === "/pair" && request.method === "POST") return this.pair(request);
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
    server.accept();
    this.agent = server;
    server.addEventListener("message", (ev) => this.onAgentMessage(ev.data));
    server.addEventListener("close", () => this.onAgentClose());
    server.addEventListener("error", () => this.onAgentClose());
    return new Response(null, { status: 101, webSocket: client });
  }

  onAgentClose() {
    this.agent = null;
    for (const [, slot] of this.pending) {
      try { slot.opened ? slot.controller.error(new Error("agent disconnected"))
                        : slot.headReject(new Error("agent disconnected")); } catch (e) {}
    }
    this.pending.clear();
  }

  onAgentMessage(data) {
    let msg;
    try { msg = JSON.parse(typeof data === "string" ? data : ""); } catch (e) { return; }
    if (msg.t === "hello") {
      this.paircode = (msg.paircode || "").toUpperCase();
      this.valid = new Set(msg.devices || []);      // returning devices validate against this
      return;
    }
    if (msg.t === "devices") { this.valid = new Set(msg.devices || []); return; }  // live refresh (kick)
    if (msg.t === "paircode") { this.paircode = (msg.paircode || "").toUpperCase(); return; } // rotated
    const slot = this.pending.get(msg.id);
    if (!slot) return;
    if (msg.t === "res") {
      slot.status = msg.status; slot.headers = msg.headers || {}; slot.opened = true;
      slot.headResolve();
    } else if (msg.t === "chunk") {
      try { slot.controller.enqueue(b64ToBytes(msg.data)); } catch (e) {}
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
    if (!this.agent) return json({ ok: false, error: "desktop offline" }, 503);
    const now = Date.now();
    this.pairAttempts = this.pairAttempts.filter((t) => now - t < PAIR_WINDOW_MS);
    if (this.pairAttempts.length >= PAIR_MAX)
      return json({ ok: false, error: "too many attempts — wait a few minutes" }, 429);

    let body = {};
    try { body = await request.json(); } catch (e) {}
    const code = (body.paircode || "").toUpperCase();
    if (!this.paircode || code !== this.paircode) {
      this.pairAttempts.push(now);
      return json({ ok: false, error: "bad pairing code" }, 403);
    }
    this.pairAttempts = [];                              // reset on success
    const token = randToken();
    const hash = await sha256hex(token);
    this.valid.add(hash);
    const name = String(body.name || shortUA(request.headers.get("User-Agent") || "")).slice(0, 60);
    // device_id: a client-supplied STABLE id (localStorage / Keychain) so re-pairing the same client
    // updates its device row instead of duplicating. device_added carries it + the token hash + name.
    this.sendAgent({ t: "device_added", device_id: String(body.device_id || ""), hash, name });
    // token in the body too → a NATIVE app (no cookie jar) stores it in the Keychain and sends it as
    // `Authorization: Bearer <token>`. A browser/WKWebView just uses the Secure cookie below.
    return new Response(JSON.stringify({ ok: true, token }), {
      status: 200,
      headers: {
        "content-type": "application/json",
        // 1-year cookie → durable; real lifetime is bounded by the desktop still trusting this hash.
        "set-cookie": `collie_sess=${token}; HttpOnly; Secure; SameSite=Lax; Path=/r/; Max-Age=31536000`,
      },
    });
  }

  async proxyToAgent(request, path, search) {
    if (!this.agent) return offlinePage();
    if (path.startsWith("/api/remote/")) return json({ error: "pairing is managed on the desktop" }, 403);

    const needsAuth = path.startsWith("/api/");
    if (needsAuth && !(await this.checkSession(request)))
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
        this.sendAgent({ t: "req", id, session: "s1", method: request.method, path: path + search, headers, hasBody });
        if (hasBody) { this.sendAgent({ t: "body", id, data: bodyB64 }); this.sendAgent({ t: "body_end", id }); }
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

  async checkSession(request) {
    let tok = null;
    const auth = request.headers.get("Authorization") || "";
    const mb = auth.match(/^Bearer\s+([A-Za-z0-9_\-]+)$/);   // native app path
    if (mb) tok = mb[1];
    if (!tok) {
      const m = (request.headers.get("Cookie") || "").match(/collie_sess=([A-Za-z0-9_\-]+)/);  // browser/WKWebView
      if (m) tok = m[1];
    }
    if (!tok) return false;
    return this.valid.has(await sha256hex(tok));
  }

  sendAgent(obj) { try { this.agent.send(JSON.stringify(obj)); } catch (e) {} }
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
    `.then(function(r){return r.json();}).then(function(d){if(d&&d.ok){location.replace(B);}` +
    `else{document.documentElement.style.visibility='';document.body.innerHTML='<p style=\\'font:16px system-ui;padding:2rem\\'>配对失败或链接已过期，请在电脑上重新运行 collie web --remote 获取新链接。</p>';}})` +
    `.catch(function(){location.reload();});}` +
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
