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

// Report AMBIGUITY, don't hide it. A page routinely carries several elements answering to the same
// text or selector (old.reddit has a `button.save` in every comment box on the page); clicking the
// first is a guess, and a wrong guess is indistinguishable from success in the return value. So the
// caller is told how many matched — and can switch to a snapshot `ref`, which is exact.
function pageClick(text, selector) {
  let all = [];
  if (selector) { try { all = [...document.querySelectorAll(selector)]; } catch (e) { return { error: "bad selector " + selector }; } }
  else if (text) {
    const t = text.toLowerCase();
    all = [...document.querySelectorAll("a,button,[role=button],input[type=submit],input[type=button]")]
      .filter((e) => ((e.innerText || e.value || "").trim().toLowerCase()).includes(t));
  }
  const el = all[0];
  if (!el) return { error: "no element for " + (selector || text) };
  el.scrollIntoView(); el.click();
  const out = { clicked: (el.innerText || el.value || selector || text).trim().slice(0, 80) };
  if (all.length > 1) {
    out.matches = all.length;
    out.candidates = all.slice(0, 5).map((e) => (e.innerText || e.value || e.tagName || "").trim().slice(0, 40));
  }
  return out;
}

// Resolve an element (same finder as pageClick) and return the viewport-CENTER point to click, in CSS
// px relative to the viewport — exactly what CDP Input.dispatchMouseEvent consumes to place a real,
// isTrusted click. Used only by the trusted-input path. Self-contained (injected into the page).
function pagePoint(text, selector) {
  let all = [];
  if (selector) { try { all = [...document.querySelectorAll(selector)]; } catch (e) { return { error: "bad selector " + selector }; } }
  else if (text) {
    const t = text.toLowerCase();
    all = [...document.querySelectorAll("a,button,[role=button],input[type=submit],input[type=button]")]
      .filter((e) => ((e.innerText || e.value || "").trim().toLowerCase()).includes(t));
  }
  const el = all[0];
  if (!el) return { error: "no element for " + (selector || text) };
  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const inView = r.width > 0 && r.height > 0 && x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight;
  const out = { x, y, inView, label: (el.innerText || el.value || selector || text || "").trim().slice(0, 80) };
  if (all.length > 1) {   // same ambiguity warning as pageClick — the trusted path picks the first too
    out.matches = all.length;
    out.candidates = all.slice(0, 5).map((e) => (e.innerText || e.value || e.tagName || "").trim().slice(0, 40));
  }
  return out;
}

// Injected (MAIN world): show a visible pointer that GLIDES to (x,y) and pulses a ring — so you can
// watch Collie operate the page instead of things just changing on their own. Self-contained.
function pageCursor(x, y) {
  const D = document, ID = "__collieCursor";
  let c = D.getElementById(ID);
  if (!c) {
    c = D.createElement("div"); c.id = ID;
    c.style.cssText = "position:fixed;left:0;top:0;z-index:2147483647;width:26px;height:26px;margin:-3px 0 0 -3px;" +
      "pointer-events:none;opacity:0;will-change:transform,opacity;" +
      "transition:transform .32s cubic-bezier(.22,.61,.36,1),opacity .25s;" +
      "filter:drop-shadow(0 1px 3px rgba(0,0,0,.5));" +
      "background:center/contain no-repeat url(\"data:image/svg+xml;utf8," +
      "<svg xmlns='http://www.w3.org/2000/svg' width='26' height='26' viewBox='0 0 24 24'>" +
      "<path d='M4 2l6.5 17 2.4-6.8L20 9.5z' fill='%23ffffff' stroke='%23202020' stroke-width='1.4' stroke-linejoin='round'/></svg>\")";
    (D.body || D.documentElement).appendChild(c);
  }
  requestAnimationFrame(function () { c.style.opacity = "1"; c.style.transform = "translate(" + x + "px," + y + "px)"; });
  setTimeout(function () {                                   // click ring, timed to when the pointer arrives
    const r = D.createElement("div");
    r.style.cssText = "position:fixed;left:" + x + "px;top:" + y + "px;z-index:2147483646;width:16px;height:16px;" +
      "margin:-8px 0 0 -8px;border-radius:50%;pointer-events:none;border:2px solid rgba(70,200,140,.95);" +
      "transform:scale(.3);opacity:1;transition:transform .5s ease-out,opacity .5s;";
    (D.body || D.documentElement).appendChild(r);
    requestAnimationFrame(function () { r.style.transform = "scale(2.6)"; r.style.opacity = "0"; });
    setTimeout(function () { r.remove(); }, 520);
  }, 300);
  return true;
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

// Attach files by writing the <input type=file>'s FileList directly — never by clicking the page's
// "choose file" button. That button opens the OS file picker, and Chrome only opens one for a
// genuine user gesture: a synthetic or CDP-driven click produces NO dialog at all, so there is
// nothing for the desktop hand to drive either. (Collie burned a whole Reddit launch on that dead
// end.) Setting .files via DataTransfer is what Playwright/Puppeteer do and is the only path that
// works from automation. Any media type — videos and PDFs upload the same way images do.
// Self-contained (injected into the PAGE).
function pageUpload(selector, files, ref) {
  let input = null;
  const seen = [];
  if (ref) { const m = window.__collieRefs; input = m && m.get ? m.get(ref) : null; }
  else if (selector) { try { input = document.querySelector(selector); } catch (e) { return { error: "bad selector " + selector }; } }
  else {
    // No target given: find the file inputs ourselves. They are usually display:none behind a
    // styled button, so this deliberately does NOT filter by visibility. Open shadow roots are
    // walked because component-based sites (Reddit's new UI) bury the real input inside one.
    const walk = (root) => {
      let els; try { els = root.querySelectorAll("*"); } catch (e) { return; }
      for (const el of els) {
        if (el instanceof HTMLInputElement && el.type === "file") seen.push(el);
        const sub = el.shadowRoot || (window.__collieClosedRoots ? window.__collieClosedRoots.get(el) : null);
        if (sub) walk(sub);
      }
    };
    walk(document);
    if (!seen.length) return { error: "no <input type=file> on this page — the upload control may be "
                                      + "inside a cross-origin iframe, or the page may need a click to render it first" };
    if (seen.length > 1) return { error: "several file inputs (" + seen.length + ") — say which one via "
                                         + "selector or a snapshot ref",
                                  candidates: seen.slice(0, 6).map((e, i) => (e.name || e.id || e.getAttribute("aria-label") || ("#" + i))) };
    input = seen[0];
  }
  if (!input) return { error: "no file input " + (selector || ref || "") };
  if (!(input instanceof HTMLInputElement) || input.type !== "file")
    return { error: "target is not an input[type=file] (it is a <" + (input.tagName || "?").toLowerCase() + ">)" };
  if (!Array.isArray(files) || !files.length) return { error: "no files supplied" };
  if (files.length > 10) return { error: "at most 10 files can be attached at once" };
  if (files.length > 1 && !input.multiple) return { error: "this input accepts a single file" };
  const transfer = new DataTransfer();
  for (const item of files) {
    if (!item || typeof item.data !== "string" || !/^[\w.+-]+\/[\w.+-]+$/.test(item.media_type || ""))
      return { error: "unsupported file data" };
    try {
      const decoded = atob(item.data);
      const bytes = Uint8Array.from(decoded, (character) => character.charCodeAt(0));
      const blob = new Blob([bytes], { type: item.media_type });
      transfer.items.add(new File([blob], item.name || "collie-upload", { type: item.media_type }));
    } catch (error) {
      return { error: "could not decode file: " + error };
    }
  }
  input.files = transfer.files;
  input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  // Read the FileList back: assigning .files is silently refused in some contexts, and a refused
  // upload otherwise looks identical to a successful one.
  const landed = input.files ? input.files.length : 0;
  return { uploaded: landed, attached: landed === transfer.files.length,
           names: [...(input.files || [])].map((file) => file.name),
           accept: input.getAttribute("accept") || "" };
}

// --- ref-indexed accessibility snapshot (MAIN world) ---------------------------------------------
// A compact "[e5] button \"Add to cart\"" list of the visible, interactive elements — the view the
// model acts on instead of guessing CSS selectors. Built in injected JS (NOT CDP getFullAXTree) so
// there is no extra debugger surface and it composes with the existing trusted-click path: each kept
// element is stashed on window.__collieRefs (a real element handle), and a later click/type by ref
// pulls THAT element back and clicks its live getBoundingClientRect centre through CDP — a real,
// isTrusted click. Traverses shadow roots: open ones off el.shadowRoot, closed ones via the WeakMap
// shadow.js records at document_start. Cross-origin iframes are unreachable from page JS (accepted
// limit — a CDP OOPIF path is the documented follow-up). Self-contained.
function pageSnapshot(maxN) {
  const CAP = maxN || 200;
  const out = [];
  const refs = (window.__collieRefs = new Map());   // fresh map each snapshot -> stale refs drop
  let n = 0, cut = false;   // `cut`: we hit the cap, so the list below is NOT the whole page
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
      if (n >= CAP) { cut = true; return; }
      const role = roleOf(el);
      if (role && interactive(el, role) && visible(el)) {
        const ref = "e" + (++n);
        refs.set(ref, el);
        const name = nameOf(el);
        const dis = (el.disabled || el.getAttribute("aria-disabled") === "true") ? " (disabled)" : "";
        out.push("[" + ref + "] " + role + (name ? ' "' + name + '"' : "") + dis);
      }
      // Open roots come off the element; closed ones are recovered from the WeakMap shadow.js
      // filled in at document_start (el.shadowRoot stays null for those, by design).
      const sub = el.shadowRoot || (window.__collieClosedRoots ? window.__collieClosedRoots.get(el) : null);
      if (sub) walk(sub);
    }
  };
  walk(document);
  // Truncation must be visible. The walk is in document order, so what gets dropped is whatever
  // sits LAST — and a dialog or modal that just opened is appended at the end of <body>. Reporting
  // a silently-cut list as if it were the page is how a required control comes to look absent.
  return { count: n, truncated: cut, snapshot: out.join("\n") || "(no interactive elements found)" };
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

// Read back what a field ACTUALLY holds — the post-condition every write needs. Setting `.value`
// or pushing CDP keystrokes can land nowhere at all (focus went elsewhere; the field is a rich-text
// editor that ignores value writes; the element was re-rendered mid-type) and every one of those
// failures returns the same cheerful "typed" as a success. Collie once submitted three empty Reddit
// comments in a row on exactly that blind spot, then invented a theory about server-side
// anti-automation to explain it. Reading the field back is what tells the difference.
// Self-contained (injected into the PAGE) and MAIN-world (window.__collieRefs lives there).
function pageValue(ref, selector) {
  let el = null;
  if (ref) { const m = window.__collieRefs; el = m && m.get ? m.get(ref) : null; }
  else if (selector) { try { el = document.querySelector(selector); } catch (e) { el = null; } }
  if (!el) el = document.activeElement;    // the type paths all focus their target first
  if (!el || el === document.body) return { error: "no element to read back" };
  const v = (el.value !== undefined && el.value !== null) ? el.value
          : (el.isContentEditable ? el.innerText : (el.textContent || ""));
  return { value: String(v == null ? "" : v).slice(0, 500),
           tag: (el.tagName || "").toLowerCase(), editable: !!el.isContentEditable };
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
  try { await exec(pageCursor, [pt.x, pt.y]); await new Promise((r) => setTimeout(r, 320)); } catch (e) {}  // show it move
  try {
    await ensureAttached(tab.id);
    const b = { x: pt.x, y: pt.y, button: "left" };
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: b.x, y: b.y, buttons: 0 });
    await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mousePressed", buttons: 1, clickCount: 1 }, b));
    await dbgSend(tab.id, "Input.dispatchMouseEvent", Object.assign({ type: "mouseReleased", buttons: 0, clickCount: 1 }, b));
    return { clicked: pt.label, trusted: true, matches: pt.matches, candidates: pt.candidates };
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
  if (!pt.inView) return { error: "field '" + selector + "' off-screen after scroll — cannot type there" };
  try { await exec(pageCursor, [pt.x, pt.y]); await new Promise((r) => setTimeout(r, 320)); } catch (e) {}
  try {
    await ensureAttached(tab.id);
    {   // click to focus the field first
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
  try { await execMain(pageCursor, [pt.x, pt.y]); await new Promise((r) => setTimeout(r, 320)); } catch (e) {}  // show it move
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
  if (!pt.inView) return { error: "field " + ref + " off-screen after scroll — cannot type there" };
  try { await execMain(pageCursor, [pt.x, pt.y]); await new Promise((r) => setTimeout(r, 320)); } catch (e) {}
  try {
    await ensureAttached(tab.id);
    {   // click to focus the field first
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

// --- screenshots -------------------------------------------------------------------------------
// Why this exists next to pageSnapshot: a snapshot is the accessibility tree, which is exact for
// ACTING but says nothing about appearance. And the OS-level `screenshot` tool cannot cover a web
// page — PrintWindow renders a Chromium window's frame but not its GPU-composited content, so it
// comes back with the tabs and an empty page. Capturing here, inside the browser, is the only path
// that sees the page as rendered.
//
// The default path is chrome.tabs.captureVisibleTab: NO chrome.debugger, so no "started debugging
// this browser" banner — the same reason console capture and eval avoid the debugger. It captures
// the visible viewport of the active tab in its window, so the collie tab is activated first: a tab
// switch inside the browser, not an OS focus steal.
async function shrinkPng(dataUrl, maxDim) {
  const blob = await (await fetch(dataUrl)).blob();
  const bmp = await createImageBitmap(blob);
  const m = Math.max(bmp.width, bmp.height);
  const k = m > maxDim ? maxDim / m : 1;
  const w = Math.max(1, Math.round(bmp.width * k)), h = Math.max(1, Math.round(bmp.height * k));
  const c = new OffscreenCanvas(w, h);
  c.getContext("2d").drawImage(bmp, 0, 0, w, h);
  bmp.close();
  const buf = await (await c.convertToBlob({ type: "image/png" })).arrayBuffer();
  const u8 = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return { data: btoa(s), width: w, height: h };
}

async function pageShot(fullPage, maxDim) {
  const tab = await targetTab(false);
  if (!tab) return { error: "no collie tab yet — call browser_open first" };
  let dataUrl = "", how = "";
  if (!fullPage) {
    // Preferred path: no chrome.debugger, so no banner. But captureVisibleTab can only read pixels
    // that are genuinely ON SCREEN — a minimised or fully covered Chrome window fails with "image
    // readback failed" — so a failure here falls through to CDP rather than asking the caller to go
    // and rearrange their windows.
    try {
      if (!tab.active) {
        await chrome.tabs.update(tab.id, { active: true });
        await new Promise((r) => setTimeout(r, 150));      // let it paint before reading back
      }
      dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
      how = "visible viewport";
    } catch (e) {
      dataUrl = "";
      how = "";
    }
  }
  if (!dataUrl) {
    // CDP reaches content below the fold and does not need the tab active, at the cost of the
    // debugger banner. Detach ONLY if we were the ones who attached — the trusted-input path holds
    // a deliberate persistent session and tearing it down here would silently disable real clicks.
    let preAttached = false;
    try {
      const targets = await new Promise((r) => chrome.debugger.getTargets((t) => r(t || [])));
      preAttached = targets.some((t) => t.tabId === tab.id && t.attached);
    } catch (e) { /* getTargets is best-effort; worst case we detach a session we did not open */ }
    await dbgAttach(tab.id);
    try {
      const r = await dbgSend(tab.id, "Page.captureScreenshot",
                              { format: "png", captureBeyondViewport: true });
      dataUrl = "data:image/png;base64," + r.data;
      how = fullPage ? "full page (CDP)"
                     : "full page (CDP — the browser window was not on screen)";
    } finally {
      if (!preAttached) await dbgDetach(tab.id);
    }
  }
  const small = await shrinkPng(dataUrl, maxDim || 1568);
  return { data: small.data, width: small.width, height: small.height, how,
           title: tab.title || "", url: tab.url || "" };
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
    if (cmd.action === "screenshot") return await pageShot(cmd.full_page === true, cmd.max_dim || 1568);
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
      let r;
      if (cmd.ref) {                                  // act on the exact field from a browser_snapshot
        r = (await wantTrusted()) ? await trustedTypeRef(cmd.ref, cmd.text, !!cmd.submit)
                                  : await execMain(pageTypeRef, [cmd.ref, cmd.text, !!cmd.submit]);
      } else if ((await wantTrusted()) && cmd.selector) {
        r = await trustedType(cmd.selector, cmd.text, !!cmd.submit);
      } else {
        r = cmd.label ? await exec(pageTypeLabel, [cmd.label, cmd.text])
                      : await exec(pageType, [cmd.selector, cmd.text, !!cmd.submit]);
      }
      // Verify the write instead of trusting it. Skipped when submit was requested: submitting can
      // navigate or clear the field, so an empty read-back there would be a false alarm.
      if (r && !r.error && !cmd.submit) {
        const want = String(cmd.text || "");
        const back = await execMain(pageValue, [cmd.ref || "", cmd.selector || ""]);
        if (back && !back.error) {
          const got = String(back.value || "");
          const probe = want.trim().slice(0, 60);
          r = Object.assign({}, r, { value: got.slice(0, 120),
                                     landed: !probe || got.indexOf(probe) >= 0 });
        }
      }
      return r;
    }
    if (cmd.action === "pick") return await exec(pagePick, [cmd.label, cmd.option]);
    if (cmd.action === "fields") return await exec(pageFields, []);
    if (cmd.action === "upload")   // MAIN world: a snapshot ref resolves against window.__collieRefs
      return await execMain(pageUpload, [cmd.selector || "", cmd.files || [], cmd.ref || ""]);
    if (cmd.action === "reload") {
      // Pick up new extension files from disk. Chrome never re-reads an unpacked extension on its
      // own, and chrome://extensions cannot be automated (privileged page — no scripting, no
      // debugger), so reloading ourselves is the only way collie can finish its own update instead
      // of asking the user to go and click a button.
      //
      // Reload IMMEDIATELY, and accept that this command gets no reply. Deferring it with
      // setTimeout so the reply could be sent first does not work: an MV3 service worker is
      // suspended once the in-flight work finishes, and a pending timer dies with it — the reply
      // arrived and the reload silently never happened, which is the most misleading of the two
      // failure modes. Tearing the worker down here means the caller sees the request time out;
      // that IS the success signature, and the caller confirms the outcome by the version the
      // extension reports once it is answering commands again.
      chrome.runtime.reload();
      return { reloading: true };
    }
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
