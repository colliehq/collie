// collie browser bridge — background service worker.
// Long-polls the collie bridge for commands and runs them in the active tab using the user's
// real, logged-in session. The continuous /poll fetch keeps the MV3 worker alive between commands.
const BRIDGE = "http://127.0.0.1:8677";

// The dedicated "collie tab" — all browser_* actions run here so we never hijack the tab the
// user is actually looking at. Created on the first `open` and reused; auto-recreated if closed.
// Keep its id in session storage too: MV3 service workers are suspended and restarted regularly,
// so an in-memory id alone would make the next command fall back to the user's active tab again.
let collieTabId = null;

async function rememberedTabId() {
  if (collieTabId != null) return collieTabId;
  try {
    const saved = await chrome.storage.session.get("collieTabId");
    collieTabId = saved.collieTabId == null ? null : saved.collieTabId;
  } catch (e) {}
  return collieTabId;
}

async function rememberTabId(id) {
  collieTabId = id;
  try { await chrome.storage.session.set({ collieTabId: id }); } catch (e) {}
}

async function forgetTabId() {
  collieTabId = null;
  try { await chrome.storage.session.remove("collieTabId"); } catch (e) {}
}

async function tabExists(id) {
  if (id == null) return false;
  try { await chrome.tabs.get(id); return true; } catch (e) { return false; }
}

// Resolve the dedicated tab that commands should target. `create` = allowed to open a fresh
// background tab (used by `open`); every other command fails closed until it exists.
async function targetTab(create) {
  const savedId = await rememberedTabId();
  if (await tabExists(savedId)) {
    return await chrome.tabs.get(savedId);
  }
  if (savedId != null) await forgetTabId();
  if (create) {
    // Start at about:blank so the navigation listener is installed before the target page loads.
    // active:false keeps the user's Collie UI in the foreground.
    const t = await chrome.tabs.create({ url: "about:blank", active: false });
    await rememberTabId(t.id);
    return t;
  }
  return null;
}

async function activeTab() {
  return await targetTab(false);
}

// Forget the collie tab if the user closes it, so the next `open` makes a fresh one.
chrome.tabs.onRemoved.addListener((id) => {
  rememberedTabId().then((savedId) => { if (id === savedId) return forgetTabId(); });
});

function waitComplete(tabId, timeoutMs = 20000) {
  return new Promise((resolve) => {
    const finish = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(t); resolve(); };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    chrome.tabs.onUpdated.addListener(listener);
    const t = setTimeout(finish, timeoutMs);
  });
}

async function navigateCollieTab(tabId, url) {
  const complete = waitComplete(tabId);
  await chrome.tabs.update(tabId, { url });
  await complete;
  return await chrome.tabs.get(tabId);
}

function httpUrl(raw) {
  try {
    const url = new URL(raw);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch (e) {
    return null;
  }
}

// --- functions injected into the page (must be self-contained) ---
function pageRead() { return document.body ? document.body.innerText : ""; }

function pageLinks(filter) {
  const f = (filter || "").toLowerCase();
  return [...document.querySelectorAll("a[href]")]
    .map((a) => ({ text: (a.innerText || "").trim().slice(0, 80), href: a.href }))
    .filter((l) => l.text && (!f || (l.text + l.href).toLowerCase().includes(f)))
    .slice(0, 100);
}

function pageClick(text, selector) {
  let el = null;
  if (selector) el = document.querySelector(selector);
  else if (text) {
    const t = text.toLowerCase();
    el = [...document.querySelectorAll("a,button,[role=button],input[type=submit],input[type=button]")]
      .find((e) => ((e.innerText || e.value || "").trim().toLowerCase()).includes(t));
  }
  if (!el) return { error: "no element for " + (selector || text) };
  el.scrollIntoView(); el.click();
  return { clicked: (el.innerText || el.value || selector || text).trim().slice(0, 80) };
}

function pageType(selector, text, submit) {
  const el = document.querySelector(selector);
  if (!el) return { error: "no field " + selector };
  el.focus();
  // React-controlled inputs (Facebook, most SPAs) ignore a plain `el.value = text` —
  // set through the NATIVE prototype setter so React's tracker registers it. Inlined
  // (not a shared helper): this function is injected into the PAGE via
  // chrome.scripting.executeScript and cannot reference other extension-scope functions.
  {
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
                                            : window.HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, "value");
    if (d && d.set) d.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (submit) {
    const form = el.form;
    if (form) form.submit();
    else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  }
  return { typed: (text || "").slice(0, 40), submit: !!submit };
}

// Type into the input/textarea whose enclosing <label> matches `labelText` — robust on
// obfuscated forms (Facebook Marketplace, etc.) where inputs have no stable selector.
// Self-contained: this runs injected in the PAGE, so it can't call other extension fns.
function pageTypeLabel(labelText, text) {
  const t = (labelText || "").toLowerCase();
  const el = [...document.querySelectorAll("input,textarea")].find((e) => {
    const l = e.closest("label"); return l && (l.innerText || "").toLowerCase().includes(t);
  });
  if (!el) return { error: "no field labeled " + labelText };
  el.focus();
  const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
                                          : window.HTMLInputElement.prototype;
  const d = Object.getOwnPropertyDescriptor(proto, "value");
  if (d && d.set) d.set.call(el, text); else el.value = text;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return { typed: (text || "").slice(0, 40), value: (el.value || "").slice(0, 40), label: labelText };
}

// Pick an option from a labelled dropdown/combobox: click the combobox, wait for its
// listbox to render, click the option matching `optionText`. Generic (role=combobox +
// role=option), not site-specific.
async function pagePick(labelText, optionText) {
  const t = (labelText || "").toLowerCase();
  const trig = [...document.querySelectorAll("[role=combobox]")].find((c) => {
    const l = c.closest("label") || c; return (l.innerText || "").toLowerCase().includes(t);
  });
  if (!trig) return { error: "no dropdown labeled " + labelText };
  trig.click();
  await new Promise((r) => setTimeout(r, 700));
  const opts = [...document.querySelectorAll("[role=option]")];
  const o = (optionText || "").toLowerCase();
  const opt = opts.find((e) => (e.innerText || "").trim().toLowerCase() === o)
           || opts.find((e) => (e.innerText || "").toLowerCase().includes(o));
  if (!opt) return { error: "no option " + optionText + " under " + labelText,
                     options: opts.slice(0, 8).map((e) => (e.innerText || "").trim()) };
  opt.scrollIntoView(); opt.click();
  await new Promise((r) => setTimeout(r, 200));
  return { picked: optionText, label: labelText };
}

// List the labelled form controls on the page (label, kind, current value) so the agent
// can see what to fill without guessing selectors.
function pageFields() {
  return [...document.querySelectorAll("input,textarea,[role=combobox]")].map((e) => {
    const l = e.closest("label"); const lt = l ? (l.innerText || "").trim().split("\n")[0] : "";
    const role = e.getAttribute("role");
    return { label: lt || e.getAttribute("aria-label") || "",
             kind: role === "combobox" ? "dropdown" : (e.tagName === "TEXTAREA" ? "text" : (e.getAttribute("type") || "text")),
             value: (e.value || "").slice(0, 40) };
  }).filter((x) => x.label && x.kind !== "hidden");
}

function pageUpload(selector, files) {
  const input = document.querySelector(selector);
  if (!input) return { error: "no file input " + selector };
  if (!(input instanceof HTMLInputElement) || input.type !== "file")
    return { error: "selector is not an input[type=file]: " + selector };
  if (!Array.isArray(files) || !files.length) return { error: "no images supplied" };
  if (files.length > 10) return { error: "at most 10 images can be uploaded at once" };
  const transfer = new DataTransfer();
  for (const item of files) {
    if (!item || typeof item.data !== "string" || !/^image\/(avif|gif|heic|heif|jpeg|png|webp)$/.test(item.media_type || ""))
      return { error: "unsupported image data" };
    try {
      const decoded = atob(item.data);
      const bytes = Uint8Array.from(decoded, (character) => character.charCodeAt(0));
      const blob = new Blob([bytes], { type: item.media_type });
      transfer.items.add(new File([blob], item.name || "collie-chat-image", { type: item.media_type }));
    } catch (error) {
      return { error: "could not decode image: " + error };
    }
  }
  input.files = transfer.files;
  input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  return { uploaded: transfer.files.length, names: [...transfer.files].map((file) => file.name) };
}

async function exec(func, args) {
  const tab = await activeTab();
  if (!tab) return { error: "no dedicated Collie tab — call browser_open first" };
  const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
  return res.result;
}

// --- DevTools console / debugger capture (chrome.debugger = real console + exceptions + eval) ---
const consoleBuf = {};     // tabId -> [ "level: text" ]
const attached = {};       // tabId -> true

function debuggee(tabId) { return { tabId }; }

async function ensureDebugger(tabId) {
  if (attached[tabId]) return;
  await chrome.debugger.attach(debuggee(tabId), "1.3");
  consoleBuf[tabId] = consoleBuf[tabId] || [];
  await chrome.debugger.sendCommand(debuggee(tabId), "Runtime.enable");
  await chrome.debugger.sendCommand(debuggee(tabId), "Log.enable");
  attached[tabId] = true;   // set ONLY after enable succeeds — else a failed enable leaves the flag
}                           // true and every later call short-circuits with capture silently broken

chrome.debugger.onEvent.addListener((source, method, params) => {
  const b = consoleBuf[source.tabId] || (consoleBuf[source.tabId] = []);
  if (method === "Runtime.consoleAPICalled") {
    const txt = (params.args || []).map((a) => a.value !== undefined ? a.value :
      (a.description || a.preview && JSON.stringify(a.preview) || a.type)).join(" ");
    b.push(params.type + ": " + txt);
  } else if (method === "Runtime.exceptionThrown") {
    const d = params.exceptionDetails || {};
    b.push("exception: " + (d.exception && (d.exception.description || d.exception.value) || d.text));
  } else if (method === "Log.entryAdded") {
    const e = params.entry || {};
    b.push(e.level + "(" + (e.source || "") + "): " + e.text);
  }
  if (b.length > 500) b.splice(0, b.length - 500);
});

chrome.debugger.onDetach.addListener((source) => {
  delete attached[source.tabId];       // free per-tab state so it doesn't grow unbounded across tabs
  delete consoleBuf[source.tabId];
});

async function getConsole(clear) {
  const tab = await activeTab();
  if (!tab) return { error: "no active tab" };
  try { await ensureDebugger(tab.id); } catch (e) { return { error: "debugger attach failed: " + e }; }
  const b = consoleBuf[tab.id] || [];
  const out = b.slice(-200);
  if (clear) consoleBuf[tab.id] = [];
  return out.length ? out : ["(console empty — logs are captured from when the debugger attached; " +
    "reload the page after the first browser_console call to capture load-time logs)"];
}

async function evalExpr(expr) {
  const tab = await activeTab();
  if (!tab) return { error: "no active tab" };
  try { await ensureDebugger(tab.id); } catch (e) { return { error: "debugger attach failed: " + e }; }
  const r = await chrome.debugger.sendCommand(debuggee(tab.id), "Runtime.evaluate",
    { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) return { error: r.exceptionDetails.text || "eval error" };
  const v = r.result;
  return { value: v.value !== undefined ? v.value : (v.description || v.type) };
}

async function handle(cmd) {
  try {
    if (cmd.action === "open") {
      const url = httpUrl(cmd.url);
      if (!url) return { error: "browser_open only accepts http(s) URLs" };
      const tab = await targetTab(true);            // opens/reuses a background collie tab
      await navigateCollieTab(tab.id, url);
      return await exec(pageRead, []);
    }
    if (cmd.action === "show") {
      const tab = await targetTab(false);
      if (!tab) return { error: "no dedicated Collie tab yet — open a page first" };
      const shown = await chrome.tabs.update(tab.id, { active: true });
      return { shown: true, title: shown.title || "", url: shown.url || "" };
    }
    if (cmd.action === "read") return await exec(pageRead, []);
    if (cmd.action === "links") return await exec(pageLinks, [cmd.filter || ""]);
    if (cmd.action === "click") {
      const r = await exec(pageClick, [cmd.text || "", cmd.selector || ""]);
      await new Promise((z) => setTimeout(z, 800));
      return { click: r, page: await exec(pageRead, []) };
    }
    if (cmd.action === "type") return cmd.label
      ? await exec(pageTypeLabel, [cmd.label, cmd.text])
      : await exec(pageType, [cmd.selector, cmd.text, !!cmd.submit]);
    if (cmd.action === "pick") return await exec(pagePick, [cmd.label, cmd.option]);
    if (cmd.action === "fields") return await exec(pageFields, []);
    if (cmd.action === "upload") return await exec(pageUpload, [cmd.selector, cmd.files || []]);
    if (cmd.action === "console") return await getConsole(!!cmd.clear);
    if (cmd.action === "eval") return await evalExpr(cmd.expr || "");
    return { error: "unknown action " + cmd.action };
  } catch (e) {
    return { error: String(e) };
  }
}

// --- MV3-hardened poll loop (pattern proven in the user's auto-apply / forum-autopost bridges) ---
// A plain for-loop of fetches dies when the service worker is suspended (~30s idle) and is NEVER
// re-armed -> the bridge silently stalls. So: (a) keep the worker alive with a no-op API ping
// WHILE a request/command is in flight (any API call resets the idle timer), and (b) use
// chrome.alarms + onStartup as the survive-suspension backstop that re-arms polling after the
// worker revives.
let __alive = 0, __aliveTimer = null, __polling = false;
function keepAlive(on) {
  if (on) {
    __alive++;
    if (!__aliveTimer) __aliveTimer = setInterval(function () {
      try { chrome.runtime.getPlatformInfo(function () {}); } catch (e) {}
    }, 20000);
  } else {
    __alive = Math.max(0, __alive - 1);
    if (__alive === 0 && __aliveTimer) { clearInterval(__aliveTimer); __aliveTimer = null; }
  }
}

async function pollOnce() {
  if (__polling) return;             // one loop at a time
  __polling = true;
  try {
    for (;;) {
      let cmd = null;
      keepAlive(true);
      try {
        // X-Collie-Bridge marks this as the extension (not a drive-by page); the bridge's CSRF
        // gate rejects any request missing it. host_permissions let the extension set it freely.
        const r = await fetch(BRIDGE + "/poll", { headers: { "X-Collie-Bridge": "1" } });
        cmd = await r.json();
      } catch (e) {
        return;                      // bridge down / worker resuming — the alarm re-arms us
      } finally { keepAlive(false); }
      if (cmd && cmd.id) {
        keepAlive(true);
        let data;
        try { data = await handle(cmd); }
        finally { keepAlive(false); }
        try {
          await fetch(BRIDGE + "/result", {
            method: "POST",
            headers: { "content-type": "application/json", "X-Collie-Bridge": "1" },
            body: JSON.stringify({ id: cmd.id, data }),
          });
        } catch (e) { /* result dropped; the tool times out and reports it */ }
      }
    }
  } finally {
    __polling = false;
  }
}

chrome.alarms.create("colliePoll", { periodInMinutes: 0.5 });  // survive-suspension backstop
chrome.alarms.onAlarm.addListener(function (a) { if (a.name === "colliePoll") pollOnce(); });
chrome.runtime.onStartup.addListener(function () { pollOnce(); });  // restart when the SW revives
pollOnce();
