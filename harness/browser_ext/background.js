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

// Resolve the tab commands run in. NEVER fails: adopted tab -> a real tab the user already has ->
// a fresh one. Failing closed here is what produced "no active tab", which the model then reported
// to the user as "the bridge won't connect" while the bridge was perfectly healthy.
async function targetTab(create) {
  const savedId = await rememberedTabId();
  if (await tabExists(savedId)) {
    return await chrome.tabs.get(savedId);
  }
  if (savedId != null) await forgetTabId();

  // 1) the tab the user is actually looking at — it carries their logins, and "just use the tab I
  //    already have open" is the single most common ask.
  try {
    const found = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const act = found && found[0];
    if (act && /^https?:/i.test(act.url || "")) {
      await rememberTabId(act.id);
      return act;
    }
  } catch (e) {}

  // 2) any other ordinary web tab (the active one may be chrome://extensions — e.g. right after
  //    reloading this extension — which cannot be scripted).
  try {
    const web = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
    if (web && web.length) {
      const t = web.find((x) => x.active) || web[0];
      await rememberTabId(t.id);
      return t;
    }
  } catch (e) {}

  // 3) nothing usable open at all -> make one. Start at about:blank so the navigation listener is
  //    installed before the target page loads; active:false keeps the user's Collie UI in front.
  const fresh = await chrome.tabs.create({ url: "about:blank", active: false });
  await rememberTabId(fresh.id);
  return fresh;
}

async function activeTab() {
  return await targetTab(false);
}

// If the user ALREADY has that site open, adopt THAT tab instead of opening a second one. This is
// what people mean by "just use the tab I've got open" — and it lands directly on the view they are
// actually logged into. (Cookies are per-profile, so a fresh tab would be authenticated too; the
// point of adopting is to reuse their page and not litter the window with duplicates.)
async function adoptTabForUrl(url) {
  let origin;
  try { origin = new URL(url).origin; } catch (e) { return null; }
  let tabs = [];
  try { tabs = await chrome.tabs.query({ url: origin + "/*" }); } catch (e) { return null; }
  if (!tabs || !tabs.length) return null;
  const tab = tabs.find((t) => t.active) || tabs[0];
  await rememberTabId(tab.id);
  return tab;
}

// Forget the collie tab if the user closes it, so the next `open` makes a fresh one.
chrome.tabs.onRemoved.addListener((id) => {
  if (id === dbgTab) dbgTab = null;   // Chrome auto-detaches the debugger on close; drop our handle too
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
  await ensureConsoleCapture(tabId);   // arm console capture on the fresh document (load logs onward)
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

// Resolve an element (same finder as pageClick) and return the viewport-CENTER point to click, in CSS
// px relative to the viewport — exactly what CDP Input.dispatchMouseEvent consumes to place a real,
// isTrusted click. Used only by the trusted-input path. Self-contained (injected into the page).
function pagePoint(text, selector) {
  let el = null;
  if (selector) el = document.querySelector(selector);
  else if (text) {
    const t = text.toLowerCase();
    el = [...document.querySelectorAll("a,button,[role=button],input[type=submit],input[type=button]")]
      .find((e) => ((e.innerText || e.value || "").trim().toLowerCase()).includes(t));
  }
  if (!el) return { error: "no element for " + (selector || text) };
  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const inView = r.width > 0 && r.height > 0 && x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight;
  return { x, y, inView, label: (el.innerText || el.value || selector || text || "").trim().slice(0, 80) };
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

// --- ref-indexed accessibility snapshot (MAIN world) ---------------------------------------------
// A compact "[e5] button \"Add to cart\"" list of the visible, interactive elements — the view the
// model acts on instead of guessing CSS selectors. Built in injected JS (NOT CDP getFullAXTree) so
// there is no extra debugger surface and it composes with the existing trusted-click path: each kept
// element is stashed on window.__collieRefs (a real element handle), and a later click/type by ref
// pulls THAT element back and clicks its live getBoundingClientRect centre through CDP — a real,
// isTrusted click. Traverses OPEN shadow roots (el.shadowRoot); cross-origin iframes are unreachable
// from page JS (accepted limit — a CDP OOPIF path is the documented follow-up). Self-contained.
function pageSnapshot(maxN) {
  const CAP = maxN || 200;
  const out = [];
  const refs = (window.__collieRefs = new Map());   // fresh map each snapshot -> stale refs drop
  let n = 0;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href") ? "link" : "";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (["button", "submit", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "hidden") return "";
      return "textbox";
    }
    return "";
  };
  const nameOf = (el) => {
    let nm = el.getAttribute("aria-label") || "";
    if (!nm) {
      const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
      nm = ids.map((id) => { const e = document.getElementById(id); return e ? e.innerText : ""; }).join(" ").trim();
    }
    if (!nm) { const l = el.closest("label"); if (l) nm = (l.innerText || "").trim(); }
    if (!nm && el.id) {
      try { const lf = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (lf) nm = (lf.innerText || "").trim(); } catch (e) {}
    }
    if (!nm) nm = (el.innerText || el.value || el.getAttribute("placeholder") || el.getAttribute("alt") || el.getAttribute("title") || "").trim();
    return nm.replace(/\s+/g, " ").slice(0, 80);
  };
  const INTERACTIVE = ["link", "button", "textbox", "combobox", "checkbox", "radio", "switch",
                       "menuitem", "menuitemcheckbox", "tab", "option", "slider", "spinbutton"];
  const interactive = (el, role) => {
    if (INTERACTIVE.includes(role)) return true;
    if (el.getAttribute("tabindex") !== null && el.tabIndex >= 0) return true;
    return typeof el.onclick === "function";
  };
  const walk = (root) => {
    let els;
    try { els = root.querySelectorAll("*"); } catch (e) { return; }
    for (const el of els) {
      if (n >= CAP) return;
      const role = roleOf(el);
      if (role && interactive(el, role) && visible(el)) {
        const ref = "e" + (++n);
        refs.set(ref, el);
        const name = nameOf(el);
        const dis = (el.disabled || el.getAttribute("aria-disabled") === "true") ? " (disabled)" : "";
        out.push("[" + ref + "] " + role + (name ? ' "' + name + '"' : "") + dis);
      }
      if (el.shadowRoot) walk(el.shadowRoot);   // open shadow DOM only
    }
  };
  walk(document);
  return { count: n, snapshot: out.join("\n") || "(no interactive elements found)" };
}

// Resolve a ref from the last snapshot to its live element. Shared shape with pagePoint so the
// trusted-click path is identical. MAIN world (the refs Map lives on the page window).
function pagePointRef(ref) {
  const m = window.__collieRefs;
  const el = m && m.get ? m.get(ref) : null;
  if (!el || !el.isConnected) return { error: "no live element for ref " + ref + " — take a fresh browser_snapshot" };
  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const inView = r.width > 0 && r.height > 0 && x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight;
  return { x, y, inView, label: (el.innerText || el.value || ref || "").trim().slice(0, 80) };
}

function pageClickRef(ref) {
  const m = window.__collieRefs;
  const el = m && m.get ? m.get(ref) : null;
  if (!el || !el.isConnected) return { error: "no live element for ref " + ref + " — take a fresh browser_snapshot" };
  el.scrollIntoView({ block: "center" }); el.click();
  return { clicked: (el.innerText || el.value || ref).trim().slice(0, 80) };
}

function pageTypeRef(ref, text, submit) {
  const m = window.__collieRefs;
  const el = m && m.get ? m.get(ref) : null;
  if (!el || !el.isConnected) return { error: "no live element for ref " + ref + " — take a fresh browser_snapshot" };
  el.focus();
  const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const d = Object.getOwnPropertyDescriptor(proto, "value");
  if (d && d.set) d.set.call(el, text); else el.value = text;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  if (submit) {
    const form = el.form;
    if (form) form.submit();
    else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  }
  return { typed: (text || "").slice(0, 40), submit: !!submit };
}

async function exec(func, args) {
  const tab = await activeTab();
  if (!tab) return { error: "no dedicated Collie tab — call browser_open first" };
  const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
  return res.result;
}

// --- console capture + eval WITHOUT chrome.debugger ----------------------------------------------
// chrome.debugger is the single biggest Chrome Web Store rejection risk, and it paints a persistent
// "collie has started debugging this browser" banner across the top of the window. Everything the
// debugger did here runs through chrome.scripting in the page's MAIN world instead: a patch buffers
// console.* + errors on window.__collieConsole, and eval runs an injected indirect-eval. Trade-offs
// vs CDP: console captures from injection onward (re-armed on each navigation, so most load logs are
// caught), and eval obeys the page's CSP (a strict unsafe-eval site refuses) — both acceptable for
// store approvability and a far less alarming install.

// Injected (MAIN world): patch console + error handlers to buffer messages on the page. Idempotent —
// runs on every navigation but only installs once per document. Self-contained (no extension scope).
function installConsoleCapture() {
  if (window.__collieConsoleInstalled) return true;
  window.__collieConsoleInstalled = true;
  const buf = (window.__collieConsole = window.__collieConsole || []);
  const cap = (line) => { buf.push(line); if (buf.length > 500) buf.splice(0, buf.length - 500); };
  const fmt = (a) => { try { return typeof a === "string" ? a : JSON.stringify(a); } catch (e) { return String(a); } };
  ["log", "info", "warn", "error", "debug"].forEach((level) => {
    const orig = console[level] ? console[level].bind(console) : null;
    console[level] = function () {
      cap(level + ": " + Array.prototype.map.call(arguments, fmt).join(" "));
      if (orig) orig.apply(console, arguments);
    };
  });
  window.addEventListener("error", (e) => cap("exception: " + (e.message || "error") +
    (e.filename ? " @ " + e.filename + ":" + e.lineno : "")));
  window.addEventListener("unhandledrejection", (e) =>
    cap("exception: unhandled rejection: " + fmt(e.reason)));
  return true;
}

// Injected (MAIN world): read + optionally clear the captured buffer.
function readConsole(clear) {
  const buf = window.__collieConsole || [];
  const out = buf.slice(-200);
  if (clear) window.__collieConsole = [];
  return out;
}

// Injected (MAIN world): indirect eval, awaiting a promise result, coerced to a serializable value.
async function pageEval(expr) {
  try {
    let v = (0, eval)(expr);                       // indirect eval -> runs in the page global scope
    if (v && typeof v.then === "function") v = await v;
    let out;
    if (v === undefined) out = "undefined";
    else if (v === null || typeof v === "string" || typeof v === "number" || typeof v === "boolean") out = v;
    else { try { out = JSON.parse(JSON.stringify(v)); } catch (e) { out = String(v); } }
    return { value: out };
  } catch (e) {
    return { error: String((e && e.message) || e) };
  }
}

// Like exec(), but injects into the page's MAIN world — needed so the console patch and eval see the
// real page globals (the default isolated world has its own console and forbids eval under MV3 CSP).
async function execMain(func, args) {
  const tab = await activeTab();
  if (!tab) return { error: "no dedicated Collie tab — call browser_open first" };
  const [res] = await chrome.scripting.executeScript({
    target: { tabId: tab.id }, world: "MAIN", func, args });
  return res.result;
}

async function ensureConsoleCapture(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", func: installConsoleCapture });
  } catch (e) { /* chrome:// pages etc. can't be scripted; console just stays empty there */ }
}

const NO_TAB = "no collie tab yet — call browser_open(url) first. It opens in YOUR real, logged-in " +
  "browser (and adopts a tab you already have on that site), so your sessions apply.";

async function getConsole(clear) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  await ensureConsoleCapture(tab.id);                // arm capture if a navigation hasn't already
  const out = await execMain(readConsole, [!!clear]);
  return (out && out.length) ? out : ["(console empty — capture starts when the page is opened via " +
    "collie or browser_console is first called; reload the page to catch load-time logs)"];
}

async function evalExpr(expr) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  return await execMain(pageEval, [expr]);
}

// --- trusted input via chrome.debugger (CDP) -----------------------------------------------------
// el.click()/dispatchEvent produce isTrusted=false events; sites with real bot mitigation (eBay's
// add-to-cart, banking, some "prove you're human" gestures) gate sensitive actions on a genuine user
// gesture and silently ignore synthetic ones. When high-fidelity mode is on (popup toggle, persisted
// in storage) or a single command sets trusted:true, we place a REAL click through the DevTools
// Protocol — isTrusted=true, indistinguishable from hardware. Cost: Chrome shows a "collie has
// started debugging this browser" banner while attached, so we attach ONLY around the action and
// detach immediately (the banner flashes rather than persists), and never leak a session.
// Authorization model: a GLOBAL default (ON — high-fidelity input is the point of this build) plus
// optional PER-ORIGIN overrides. Resolved in order: session override -> permanent override -> global.
// - permanent overrides live in storage.local  ({ "https://ebay.com": "on"|"off" })
// - session overrides live in storage.session   (cleared when the browser closes = "just this session")
// Off only when EXPLICITLY disabled (popup, `mode` command, or dismissing the debug banner).
async function trustedGlobal() {
  try { const s = await chrome.storage.local.get("trustedInput"); return s.trustedInput !== false; }
  catch (e) { return true; }
}
function originOf(tab) { try { return new URL(tab.url).origin; } catch (e) { return ""; } }

async function trustedForOrigin(origin) {
  if (origin) {
    try { const ses = (await chrome.storage.session.get("siteMode")).siteMode || {};
      if (ses[origin]) return ses[origin] === "on"; } catch (e) {}
    try { const loc = (await chrome.storage.local.get("siteMode")).siteMode || {};
      if (loc[origin]) return loc[origin] === "on"; } catch (e) {}
  }
  return await trustedGlobal();
}

// scope: 'always'|'off' (permanent) · 'session'|'sessionoff' (this browser session) · 'default' (clear)
async function setSiteMode(origin, scope) {
  if (!origin) return;
  const loc = (await chrome.storage.local.get("siteMode")).siteMode || {};
  const ses = (await chrome.storage.session.get("siteMode")).siteMode || {};
  delete loc[origin]; delete ses[origin];
  if (scope === "always") loc[origin] = "on";
  else if (scope === "off") loc[origin] = "off";
  else if (scope === "session") ses[origin] = "on";
  else if (scope === "sessionoff") ses[origin] = "off";
  await chrome.storage.local.set({ siteMode: loc });
  await chrome.storage.session.set({ siteMode: ses });
}

function dbgAttach(tabId) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, "1.3", () => {
      const e = chrome.runtime.lastError;
      if (e && !/already attached/i.test(e.message || "")) reject(new Error(e.message)); else resolve();
    });
  });
}
function dbgSend(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (res) => {
      const e = chrome.runtime.lastError;
      if (e) reject(new Error(e.message)); else resolve(res);
    });
  });
}
function dbgDetach(tabId) {
  return new Promise((resolve) => {
    try { chrome.debugger.detach({ tabId }, () => { void chrome.runtime.lastError; resolve(); }); }
    catch (e) { resolve(); }
  });
}
// Hold ONE debugger session on the collie tab (persistent) rather than attach/detach per action — a
// steady banner instead of a flashing one, and faster. onDetach fires when the tab closes OR the user
// clicks the banner's "Cancel": we treat an explicit cancel as "turn high-fidelity off" and respect it.
let dbgTab = null;
chrome.debugger.onDetach.addListener((src, reason) => {
  if (src && src.tabId === dbgTab) dbgTab = null;
  if (reason === "canceled_by_user") { try { chrome.storage.local.set({ trustedInput: false }); } catch (e) {} }
});
async function ensureAttached(tabId) {
  if (dbgTab === tabId) return;
  if (dbgTab != null) { const old = dbgTab; dbgTab = null; await dbgDetach(old); }
  await dbgAttach(tabId);
  dbgTab = tabId;
}

async function trustedClick(text, selector) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  const pt = await exec(pagePoint, [text || "", selector || ""]);
  if (!pt || pt.error) return pt || { error: "no element for " + (selector || text) };
  if (!pt.inView) return { error: "element found but off-screen after scroll — cannot place a real click there" };
  try {
    await ensureAttached(tab.id);
    const b = { x: pt.x, y: pt.y, button: "left" };
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: b.x, y: b.y, buttons: 0 });
    await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mousePressed", buttons: 1, clickCount: 1 }, b));
    await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mouseReleased", buttons: 0, clickCount: 1 }, b));
    return { clicked: pt.label, trusted: true };
  } catch (e) {                          // devtools open / attach blocked — NEVER regress below synthetic
    if (dbgTab === tab.id) dbgTab = null;
    const r = await exec(pageClick, [text || "", selector || ""]);
    return Object.assign({ trusted: false, note: "debugger unavailable, used synthetic click: " + String((e && e.message) || e) }, r);
  }
}

async function trustedType(selector, text, submit) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  const pt = await exec(pagePoint, ["", selector]);
  if (!pt || pt.error) return pt || { error: "no field " + selector };
  try {
    await ensureAttached(tab.id);
    if (pt.inView) {   // click to focus the field first
      const b = { x: pt.x, y: pt.y, button: "left" };
      await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mousePressed", buttons: 1, clickCount: 1 }, b));
      await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mouseReleased", buttons: 0, clickCount: 1 }, b));
    }
    // select-all (Ctrl+A) so we replace rather than append, then type as real keystrokes
    await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyDown", modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
    await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
    await dbgSend(tab.id, "Input.insertText", { text: text || "" });
    if (submit) {
      await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
      await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
    }
    return { typed: (text || "").slice(0, 40), submit: !!submit, trusted: true };
  } catch (e) {
    if (dbgTab === tab.id) dbgTab = null;
    const r = await exec(pageType, [selector, text, !!submit]);
    return Object.assign({ trusted: false, note: "debugger unavailable, used synthetic type: " + String((e && e.message) || e) }, r);
  }
}

// Trusted click/type addressed by a snapshot `ref` (instead of text/selector). Same CDP mechanism
// as trustedClick/trustedType — only the element-locating step differs (pagePointRef pulls the exact
// element the snapshot handed the model, so there is no ambiguous text/selector match).
async function trustedClickRef(ref) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  const pt = await execMain(pagePointRef, [ref]);
  if (!pt || pt.error) return pt || { error: "no element for ref " + ref };
  if (!pt.inView) return { error: "element " + ref + " off-screen after scroll — cannot place a real click there" };
  try {
    await ensureAttached(tab.id);
    const b = { x: pt.x, y: pt.y, button: "left" };
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: b.x, y: b.y, buttons: 0 });
    await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mousePressed", buttons: 1, clickCount: 1 }, b));
    await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mouseReleased", buttons: 0, clickCount: 1 }, b));
    return { clicked: pt.label, trusted: true };
  } catch (e) {
    if (dbgTab === tab.id) dbgTab = null;
    const r = await execMain(pageClickRef, [ref]);
    return Object.assign({ trusted: false, note: "debugger unavailable, used synthetic click: " + String((e && e.message) || e) }, r);
  }
}

async function trustedTypeRef(ref, text, submit) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  const pt = await execMain(pagePointRef, [ref]);
  if (!pt || pt.error) return pt || { error: "no field for ref " + ref };
  try {
    await ensureAttached(tab.id);
    if (pt.inView) {   // click to focus the field first
      const b = { x: pt.x, y: pt.y, button: "left" };
      await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mousePressed", buttons: 1, clickCount: 1 }, b));
      await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mouseReleased", buttons: 0, clickCount: 1 }, b));
    }
    await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyDown", modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
    await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
    await dbgSend(tab.id, "Input.insertText", { text: text || "" });
    if (submit) {
      await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
      await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
    }
    return { typed: (text || "").slice(0, 40), submit: !!submit, trusted: true };
  } catch (e) {
    if (dbgTab === tab.id) dbgTab = null;
    const r = await execMain(pageTypeRef, [ref, text, !!submit]);
    return Object.assign({ trusted: false, note: "debugger unavailable, used synthetic type: " + String((e && e.message) || e) }, r);
  }
}

async function handle(cmd) {
  try {
    if (cmd.action === "open") {
      const url = httpUrl(cmd.url);
      if (!url) return { error: "browser_open only accepts http(s) URLs" };
      // Prefer a tab the user already has on that site (their logged-in view); otherwise open one.
      const adopted = await adoptTabForUrl(url);
      const tab = adopted || await targetTab(true);
      const already = adopted && (adopted.url || "").indexOf(url) === 0;
      if (!already) await navigateCollieTab(tab.id, url);
      return await exec(pageRead, []);
    }
    if (cmd.action === "show") {
      const tab = await targetTab(false);
      if (!tab) return { error: "no dedicated Collie tab yet — open a page first" };
      const shown = await chrome.tabs.update(tab.id, { active: true });
      return { shown: true, title: shown.title || "", url: shown.url || "" };
    }
    if (cmd.action === "read") return await exec(pageRead, []);
    if (cmd.action === "snapshot") return await execMain(pageSnapshot, [cmd.max || 200]);
    if (cmd.action === "links") return await exec(pageLinks, [cmd.filter || ""]);
    if (cmd.action === "mode") {   // read/set high-fidelity input from the bridge/CLI
      if (typeof cmd.trusted === "boolean") await chrome.storage.local.set({ trustedInput: cmd.trusted });
      if (cmd.origin && cmd.scope) await setSiteMode(cmd.origin, cmd.scope);
      const t = await targetTab(false);
      const origin = t ? originOf(t) : "";
      return { global: await trustedGlobal(), origin, effective: await trustedForOrigin(origin) };
    }
    // Decide trusted vs synthetic for THIS action: a command can force it (trusted:true/false),
    // otherwise resolve the per-origin authorization (session -> permanent -> global default ON).
    async function wantTrusted() {
      if (cmd.trusted === true) return true;
      if (cmd.trusted === false) return false;
      const t = await activeTab();
      return await trustedForOrigin(t ? originOf(t) : "");
    }
    if (cmd.action === "click") {
      let r;
      if (cmd.ref) {                                  // act on the exact element from a browser_snapshot
        r = (await wantTrusted()) ? await trustedClickRef(cmd.ref) : await execMain(pageClickRef, [cmd.ref]);
      } else {
        r = (await wantTrusted()) ? await trustedClick(cmd.text || "", cmd.selector || "")
                                  : await exec(pageClick, [cmd.text || "", cmd.selector || ""]);
      }
      await new Promise((z) => setTimeout(z, 800));
      return { click: r, page: await exec(pageRead, []) };
    }
    if (cmd.action === "type") {
      if (cmd.ref) {                                  // act on the exact field from a browser_snapshot
        return (await wantTrusted()) ? await trustedTypeRef(cmd.ref, cmd.text, !!cmd.submit)
                                     : await execMain(pageTypeRef, [cmd.ref, cmd.text, !!cmd.submit]);
      }
      if ((await wantTrusted()) && cmd.selector) return await trustedType(cmd.selector, cmd.text, !!cmd.submit);
      return cmd.label
        ? await exec(pageTypeLabel, [cmd.label, cmd.text])
        : await exec(pageType, [cmd.selector, cmd.text, !!cmd.submit]);
    }
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
        // report our version so collie can warn when the LOADED extension is a stale copy from
        // another path (that mismatch silently cost a long debugging session).
        const r = await fetch(BRIDGE + "/poll?v=" + encodeURIComponent(chrome.runtime.getManifest().version),
                              { headers: { "X-Collie-Bridge": "1" } });
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
