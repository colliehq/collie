// collie browser bridge — background service worker.
// Long-polls the collie bridge for commands and runs them in the active tab using the user's
// real, logged-in session. The continuous /poll fetch keeps the MV3 worker alive between commands.
const BRIDGE = "http://127.0.0.1:8677";

// --- spaces: one lane of work, one tab -----------------------------------------------------------
// Every browser_* command names a SPACE (default "default") and each space owns its own tab, so two
// tasks driving this browser at the same time cannot end up fighting over one page.
//
// The other half is OWNERSHIP, and it is why this replaced a single remembered tab id. The previous
// resolver, when it had no tab of its own, ADOPTED whatever tab the user was looking at — which is
// how a run once walked into the middle of a half-filled job application in the user's own window
// and navigated it away. A tab collie did not open is never taken silently now: `open` creates its
// own tab unless the caller passes adopt:true, and `attach` is the explicit "use the tab I am
// looking at". Cookies are per-profile, so collie's own tab is logged in exactly like the user's —
// adopting was only ever about reusing their view, never about the session.
//
// What a space is NOT: a cookie jar. A Chrome extension cannot partition storage within a profile
// (that needs a separate profile, which would not be logged in), so spaces isolate TABS, not
// identity. Two spaces on the same site share one login. Say so rather than implying containers.
//
// Ids live in session storage as well as memory: MV3 suspends this worker regularly, and an
// in-memory map alone would forget every space on the very next command.
const DEFAULT_SPACE = "default";

// The space the command in flight belongs to, so the injected helpers below resolve the right tab
// without every one of them taking an extra argument. Safe because the poll loop runs commands
// strictly one at a time (one fetched, `handle` awaited, only then the next fetch).
let curSpace = DEFAULT_SPACE;

let spaces = null;                       // {name: {tabId, owned, opened}}

function spaceOf(cmd) {
  const s = cmd && typeof cmd.space === "string" ? cmd.space.trim() : "";
  return s ? s.slice(0, 40) : DEFAULT_SPACE;
}

async function saveSpaces() {
  try { await chrome.storage.session.set({ collieSpaces: spaces || {} }); } catch (e) {}
}

async function loadSpaces() {
  if (spaces) return spaces;
  let saved = {};
  try { saved = await chrome.storage.session.get(["collieSpaces", "collieTabId"]); } catch (e) {}
  spaces = (saved.collieSpaces && typeof saved.collieSpaces === "object") ? saved.collieSpaces : {};
  // Upgrade in place: a bridge that was already driving a tab keeps driving THAT tab after the
  // extension reloads into this version, instead of quietly opening a second one beside it.
  if (!spaces[DEFAULT_SPACE] && saved.collieTabId != null) {
    spaces[DEFAULT_SPACE] = { tabId: saved.collieTabId, owned: false, adopted: true };
    await saveSpaces();
  }
  return spaces;
}

async function getSpace(name) {
  const all = await loadSpaces();
  return all[name] || null;
}

async function setSpace(name, rec) {
  const all = await loadSpaces();
  all[name] = rec;
  await saveSpaces();
}

async function dropSpace(name) {
  const all = await loadSpaces();
  delete all[name];
  await saveSpaces();
}

async function tabExists(id) {
  if (id == null) return false;
  try { await chrome.tabs.get(id); return true; } catch (e) { return false; }
}

// Is this tab already spoken for by ANOTHER space? Adopting one twice would recreate the collision
// spaces exist to prevent, so the caller is told rather than quietly given a shared tab.
async function spaceHolding(tabId, except) {
  const all = await loadSpaces();
  for (const name of Object.keys(all)) {
    if (name !== except && all[name] && all[name].tabId === tabId) return name;
  }
  return null;
}

// Resolve the tab this space works in. Returns null when the space has no tab and create is false —
// deliberately: the alternative (fall back to whatever the user has in front of them) is the exact
// behaviour that made collie type into someone else's page. Callers turn null into NO_TAB, which
// tells the model to open a page or attach a tab explicitly.
async function targetTab(create, opts) {
  const name = (opts && opts.space) || curSpace;
  const rec = await getSpace(name);
  if (rec && await tabExists(rec.tabId)) return await chrome.tabs.get(rec.tabId);
  if (rec) await dropSpace(name);
  if (!create) return null;
  // A space's own tab, opened in the background: about:blank first so the navigation listener is
  // installed before the target page loads, active:false so the user keeps looking at what they
  // were looking at. A named space that asked for its own window gets one, unfocused.
  let fresh;
  if (opts && opts.window) {
    const win = await chrome.windows.create({ url: "about:blank", focused: false });
    fresh = win.tabs && win.tabs[0];
    if (!fresh) fresh = await chrome.tabs.create({ url: "about:blank", active: false });
  } else {
    fresh = await chrome.tabs.create({ url: "about:blank", active: false });
  }
  await setSpace(name, { tabId: fresh.id, owned: true });
  return fresh;
}

async function activeTab() {
  return await targetTab(false);
}

// Adopt a tab the user already has on that site — ONLY when the caller explicitly asked for it
// (browser_open adopt:true / browser_attach). Lands directly on the view they are looking at, which
// is sometimes exactly the point ("finish what I started in this tab") and is never the default.
async function adoptTabForUrl(url, space) {
  let origin;
  try { origin = new URL(url).origin; } catch (e) { return null; }
  let tabs = [];
  try { tabs = await chrome.tabs.query({ url: origin + "/*" }); } catch (e) { return null; }
  if (!tabs || !tabs.length) return null;
  const tab = tabs.find((t) => t.active) || tabs[0];
  const held = await spaceHolding(tab.id, space);
  if (held) return { error: "that tab is already space '" + held + "'s — use space:'" + held +
                            "' to work in it, or let this space open its own" };
  await setSpace(space, { tabId: tab.id, owned: false, adopted: true });
  return tab;
}

// Forget a space's tab when it closes, so the next command in that space opens a fresh one.
chrome.tabs.onRemoved.addListener((id) => {
  if (id === dbgTab) dbgTab = null;   // Chrome auto-detaches the debugger on close; drop our handle too
  loadSpaces().then((all) => {
    let changed = false;
    for (const name of Object.keys(all)) {
      if (all[name] && all[name].tabId === id) { delete all[name]; changed = true; }
    }
    if (changed) return saveSpaces();
  });
});

// Wait for THIS navigation to arrive — not merely for the next "complete" event. A tab collie just
// created is still finishing about:blank, so its complete fires immediately after the update call,
// and scripting the tab right then fails with `Cannot access contents of url "about:blank"`: the
// page the caller asked for has not loaded yet, and the very first read of it comes back an error.
// (That was invisible while collie mostly took over a tab the user already had on the site; opening
// its own tab every time made it the normal path.)
async function navigateCollieTab(tabId, url) {
  await chrome.tabs.update(tabId, { url });
  const deadline = Date.now() + 20000;
  for (;;) {
    let t = null;
    try { t = await chrome.tabs.get(tabId); } catch (e) { break; }   // tab closed under us
    if (t && t.status === "complete" && t.url && t.url !== "about:blank") break;
    if (Date.now() > deadline) break;
    await sleep(120);
  }
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
  if (all.length > 1) return {
    error: "ambiguous click target (" + all.length + " matches) — take a browser_snapshot and use one exact ref",
    matches: all.length,
    candidates: all.slice(0, 5).map((e) => (e.innerText || e.value || e.tagName || "").trim().slice(0, 40))
  };
  el.scrollIntoView(); el.click();
  const out = { clicked: (el.innerText || el.value || selector || text).trim().slice(0, 80) };
  return out;
}

// Resolve an element (same finder as pageClick) and return the viewport-CENTER point to click, in CSS
// px relative to the viewport — exactly what CDP Input.dispatchMouseEvent consumes to place a real,
// isTrusted click. Used only by the trusted-input path. Self-contained (injected into the page).
function pagePoint(text, selector, broad) {
  let all = [];
  if (selector) { try { all = [...document.querySelectorAll(selector)]; } catch (e) { return { error: "bad selector " + selector }; } }
  else if (text) {
    const t = text.toLowerCase();
    // `broad` widens the search from clickable things to ANY element carrying that text. Hovering
    // needs it: a site's navigation is made of divs and list items, not buttons, so the narrow
    // search answered "no element for Menu" about a menu that was plainly there.
    all = [...document.querySelectorAll(broad ? "*" : "a,button,[role=button],input[type=submit],input[type=button]")]
      .filter((e) => ((e.innerText || e.value || "").trim().toLowerCase()).includes(t));
    if (broad) {
      // Order matters, and getting it wrong is subtle. Drop the INVISIBLE matches first, then keep
      // the deepest of what is left. The other way round, a hidden descendant eliminates its own
      // visible ancestor and the answer is "no element" about something plainly on screen — a
      // closed submenu labelled "Submenu item" knocking out the "Menu" that opens it.
      all = all.filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
      // Every ancestor contains its descendant's text, so without this the best match is <body>.
      all = all.filter((e) => !all.some((other) => other !== e && e.contains(other)));
    }
  }
  const el = all[0];
  if (!el) return { error: "no element for " + (selector || text) };
  if (all.length > 1) return {
    error: "ambiguous click target (" + all.length + " matches) — take a browser_snapshot and use one exact ref",
    matches: all.length,
    candidates: all.slice(0, 5).map((e) => (e.innerText || e.value || e.tagName || "").trim().slice(0, 40))
  };
  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const inView = r.width > 0 && r.height > 0 && x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight;
  const out = { x, y, inView, label: (el.innerText || el.value || selector || text || "").trim().slice(0, 80) };
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
  if (el.isContentEditable || el.getAttribute("contenteditable") !== null) {
    el.textContent = text;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true,
                                               inputType: "insertText", data: text }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
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
  const el = [...document.querySelectorAll(
    "input,textarea,[contenteditable=true],[role=textbox]")].find((e) => {
    const l = e.closest("label");
    const names = [l ? (l.innerText || "") : "", e.getAttribute("aria-label") || "",
                   e.getAttribute("data-testid") || "", e.getAttribute("name") || ""];
    return names.join(" ").toLowerCase().includes(t);
  });
  if (!el) return { error: "no field labeled " + labelText };
  el.focus();
  if (el.isContentEditable || el.getAttribute("contenteditable") !== null) {
    el.textContent = text;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true,
                                               inputType: "insertText", data: text }));
  } else {
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
                                            : window.HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, "value");
    if (d && d.set) d.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  el.dispatchEvent(new Event("change", { bubbles: true }));
  const landed = el.isContentEditable ? el.innerText : el.value;
  return { typed: (text || "").slice(0, 40), value: (landed || "").slice(0, 40), label: labelText };
}

// Pick an option from a labelled dropdown/combobox: click the combobox, wait for its
// listbox to render, click the option matching `optionText`. Generic (role=combobox +
// role=option), not site-specific.
async function pagePick(labelText, optionText) {
  const t = (labelText || "").toLowerCase();
  // Native <select> FIRST, and by more than one name. `[role=combobox]` matches only an EXPLICIT
  // role attribute, which a <select> does not carry — so this used to miss every plain HTML
  // dropdown on the web while the snapshot cheerfully listed it as a combobox. Its label is also
  // often a sibling <label for=…> or an aria-label rather than an ancestor, which is why the
  // ancestor-only lookup below is not enough on its own.
  {
    const named = (e) => {
      const parts = [];
      const anc = e.closest("label"); if (anc) parts.push(anc.innerText || "");
      if (e.id) { const f = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (f) parts.push(f.innerText || ""); }
      parts.push(e.getAttribute("aria-label") || "", e.getAttribute("name") || "", e.getAttribute("id") || "");
      const lbl = e.getAttribute("aria-labelledby");
      if (lbl) lbl.split(/\s+/).forEach((id) => { const n = document.getElementById(id); if (n) parts.push(n.innerText || ""); });
      return parts.join(" ").toLowerCase();
    };
    const sels = [...document.querySelectorAll("select")];
    const sel = (!t && sels.length === 1) ? sels[0] : sels.find((e) => named(e).includes(t));
    if (sel) {
      const want = (optionText || "").trim().toLowerCase();
      const opts = [...sel.options];
      const hit = opts.find((o) => (o.text || "").trim().toLowerCase() === want)
               || opts.find((o) => (o.value || "").trim().toLowerCase() === want)
               || opts.find((o) => (o.text || "").toLowerCase().includes(want));
      if (!hit) return { error: "no option " + optionText + " under " + labelText,
                         options: opts.map((o) => (o.text || "").trim()).slice(0, 12) };
      sel.focus();
      const sd = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value");
      if (sd && sd.set) sd.set.call(sel, hit.value); else sel.value = hit.value;
      sel.dispatchEvent(new Event("input", { bubbles: true }));
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      return { picked: (hit.text || "").trim(), label: labelText, value: sel.value,
               landed: sel.value === hit.value };
    }
  }
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
  // `select` is listed explicitly: `[role=combobox]` only matches an EXPLICIT role attribute, so
  // native dropdowns were invisible here — the agent could not even see that the field existed,
  // let alone that it had to be set before the form would submit. Their options are returned too,
  // because "there is a dropdown" is useless without knowing what may be chosen.
  return [...document.querySelectorAll(
    "input,textarea,select,[role=combobox],[contenteditable=true],[role=textbox]")].map((e) => {
    const anc = e.closest("label");
    let lt = anc ? (anc.innerText || "").trim().split("\n")[0] : "";
    if (!lt && e.id) { const f = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (f) lt = (f.innerText || "").trim().split("\n")[0]; }
    const role = e.getAttribute("role");
    const isSelect = e.tagName === "SELECT";
    const rich = e.isContentEditable || e.getAttribute("contenteditable") !== null;
    const out = { label: lt || e.getAttribute("aria-label") || e.getAttribute("name") || "",
                  kind: (isSelect || role === "combobox") ? "dropdown"
                        : (rich ? "richtext"
                           : (e.tagName === "TEXTAREA" ? "text" : (e.getAttribute("type") || "text"))),
                  value: ((rich ? e.innerText : e.value) || "").slice(0, 40) };
    if (isSelect) out.options = [...e.options].map((o) => (o.text || "").trim()).slice(0, 20);
    return out;
  }).filter((x) => x.label && x.kind !== "hidden");
}

// Independent verification snapshot. Unlike pageEval this is injected as a
// real function by chrome.scripting, so strict sites such as X can be reread
// without requiring CSP `unsafe-eval`. Keep full rich-editor text for exact
// done-checks; Python applies the durable redaction and size bounds.
function pageFormSnapshot() {
  const fields = [...document.querySelectorAll(
    "input,textarea,select,[role=combobox],[contenteditable],[role=textbox]")].map((e) => {
    const anc = e.closest("label");
    let label = anc ? (anc.innerText || "").trim().split("\n")[0] : "";
    if (!label && e.id) {
      const f = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
      if (f) label = (f.innerText || "").trim().split("\n")[0];
    }
    label = label || e.getAttribute("aria-label") || e.getAttribute("data-testid") ||
      e.getAttribute("name") || e.getAttribute("role") || e.tagName;
    const role = e.getAttribute("role");
    const rich = e.isContentEditable || e.getAttribute("contenteditable") !== null;
    const value = role === "combobox"
      ? (anc ? (anc.innerText || "").replace(/\n/g, " ").trim() : "")
      : ((rich ? e.innerText : e.value) || "");
    const meta = [label, e.type, e.name, e.id, e.autocomplete,
                  e.getAttribute("aria-label")].join(" ");
    const sensitive = e.type === "password" || e.type === "email" || e.type === "tel" ||
      /(pass(word|code)?|secret|token|api.?key|captcha|recaptcha|otp|one.?time|verification.?code|cvv|cvc|card.?number|ssn|social.?security|e.?mail|phone|mobile|street.?address|postal|zip.?code|birth|dob|user.?name)/i.test(meta);
    return { label, value: sensitive ? "[redacted]" : String(value).slice(0, 4000),
             sensitive: !!sensitive, filled: !!value };
  }).filter((x) => x.label && x.filled);
  const actions = [...document.querySelectorAll("button,input[type=submit],[role=button]")].map((e) => {
    const label = (e.getAttribute("aria-label") || e.innerText || e.value || "").trim();
    const disabled = !!e.disabled || e.getAttribute("aria-disabled") === "true";
    const visible = !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
    return { label, disabled, visible };
  }).filter((x) => x.visible && /^(post|publish|send|submit|save|next|continue)$/i.test(x.label));
  return { fields, actions };
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
  //
  // Compare against what was ASKED for, not against the DataTransfer. `landed === transfer.length`
  // reports attached:true when BOTH are zero — so a DataTransfer that accepted nothing (the browser
  // can refuse `items.add` without throwing) came back as a success with an empty file list, and the
  // caller's only error check is `attached === false`. That is the same "looks identical to a
  // successful one" failure this read-back exists to prevent, one level up.
  const landed = input.files ? input.files.length : 0;
  const attached = landed > 0 && landed === files.length;
  const out = { uploaded: landed, attached: attached,
                names: [...(input.files || [])].map((file) => file.name),
                accept: input.getAttribute("accept") || "" };
  if (!attached)
    out.error = "the input holds " + landed + " file(s) after attaching " + files.length
              + (transfer.files.length !== files.length
                 ? " — the browser refused " + (files.length - transfer.files.length) + " of them before the input was touched"
                 : " — the page refused the assignment");
  return out;
}

// --- ref-indexed accessibility snapshot (MAIN world) ---------------------------------------------
// A compact "[e5] button \"Add to cart\"" view of the page — what the model acts on instead of
// guessing CSS selectors. Built in injected JS (NOT CDP getFullAXTree) so there is no extra debugger
// surface and it composes with the existing trusted-click path: each kept element is stashed on
// window.__collieRefs (a real element handle), and a later click/type by ref pulls THAT element back
// and clicks its live getBoundingClientRect centre through CDP — a real, isTrusted click.
//
// It is a TREE, not a flat list, and that is what makes it worth reading instead of the page text:
// headings and landmarks are kept as unnumbered context lines around the controls nested under them,
// so one snapshot answers both "what is this page" and "what can I press" — the pair of calls
// (snapshot + read) it used to take, at a fraction of the tokens. Three more economies: runs of
// identical siblings (a feed's 30 "Reply" links) collapse to one line while every ref stays
// addressable; nothing off-screen-but-rendered is dropped, but when the cap bites, what survives is
// chosen by IMPORTANCE (open dialog first, then in-viewport) rather than by document order, so a
// just-opened modal can no longer be the part that falls off the end; and same-origin iframes are
// walked, which they never were.
//
// Traverses shadow roots: open ones off el.shadowRoot, closed ones via the WeakMap shadow.js records
// at document_start. CROSS-origin iframes are unreachable from page JS by construction — they are
// listed, not silently skipped, and `frames:true` fetches them over CDP (see snapshotFrames).
// Self-contained.
function pageSnapshot(maxN, opts) {
  const CAP = Math.max(1, maxN || 200);
  const O = opts || {};
  const refs = (window.__collieRefs = new Map());   // fresh map each snapshot -> stale refs drop
  const items = [];            // every candidate, before the cap is applied
  const frames = [];           // cross-origin iframes: reachable only over CDP
  let visited = 0, overflowed = false;
  const VISIT_LIMIT = 30000;   // a runaway page must not hang the worker

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  };
  const inViewport = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
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
    // A drag handle usually has no role and no tabindex — a bare div the page marked draggable. It
    // still needs a line in the snapshot, or a drag-and-drop board has nothing to grab.
    if (el.draggable === true || el.getAttribute("draggable") === "true") return "draggable";
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
    // Something the page marked as draggable is something a person can act on, so it needs a ref to
    // be dragged BY. Without this a drag-and-drop board had no handles in the snapshot at all.
    if (el.draggable === true || el.getAttribute("draggable") === "true") return true;
    return typeof el.onclick === "function";
  };
  const LANDMARK = { dialog: "dialog", alertdialog: "dialog", form: "form", navigation: "nav",
                     main: "main", table: "table", list: "list", listbox: "listbox", menu: "menu",
                     tablist: "tablist", article: "article", region: "region", search: "search",
                     banner: "banner", contentinfo: "contentinfo" };
  const TAG_LANDMARK = { DIALOG: "dialog", FORM: "form", NAV: "nav", MAIN: "main", TABLE: "table",
                         UL: "list", OL: "list", ARTICLE: "article", SECTION: "region",
                         HEADER: "banner", FOOTER: "contentinfo", ASIDE: "complementary" };
  const landmarkOf = (el) => {
    const r = (el.getAttribute("role") || "").toLowerCase();
    if (LANDMARK[r]) return LANDMARK[r];
    return TAG_LANDMARK[el.tagName] || "";
  };
  const modalOf = (el) => {
    const r = (el.getAttribute("role") || "").toLowerCase();
    if (el.getAttribute("aria-modal") === "true") return true;
    if (r === "dialog" || r === "alertdialog") return true;
    return el.tagName === "DIALOG" && el.hasAttribute("open");
  };
  const headingOf = (el) => {
    if ((el.getAttribute("role") || "").toLowerCase() === "heading") return true;
    return /^H[1-6]$/.test(el.tagName);
  };
  // Text worth carrying: the element's OWN text, not its descendants' (or every ancestor would
  // repeat the whole page). Only collected when the caller asks for text.
  const ownText = (el) => {
    let s = "";
    for (const node of el.childNodes) if (node.nodeType === 3) s += node.nodeValue;
    return s.replace(/\s+/g, " ").trim();
  };
  // Any element that carries its OWN text counts, not a list of "text tags": the modern web writes
  // its prose in divs and spans, and a tag whitelist quietly lost most of what a page actually says
  // — including the status line that tells you whether the last action worked. Because only DIRECT
  // text children count, an ancestor never repeats what its children already said.
  const NOT_TEXT = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, TITLE: 1, HEAD: 1, SVG: 1 };

  const push = (entry) => { items.push(entry); };

  const walk = (node, depth, modal) => {
    let kids;
    try { kids = node.children; } catch (e) { return; }
    if (!kids) return;
    for (const el of kids) {
      if (visited++ > VISIT_LIMIT) { overflowed = true; return; }
      let childDepth = depth;
      const shown = visible(el);
      const isModal = modal || (shown && modalOf(el));

      if (shown) {
        const role = roleOf(el);
        if (role && interactive(el, role)) {
          const dis = (el.disabled || el.getAttribute("aria-disabled") === "true") ? " (disabled)" : "";
          push({ kind: "control", el, depth, modal: isModal, view: inViewport(el),
                 role, name: nameOf(el), suffix: dis });
        } else if (headingOf(el)) {
          const t = (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 90);
          if (t) push({ kind: "heading", el, depth, modal: isModal, view: inViewport(el),
                        role: /^H[1-6]$/.test(el.tagName) ? el.tagName.toLowerCase() : "heading",
                        name: t });
        } else {
          const mark = landmarkOf(el);
          if (mark) {
            const label = (el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().slice(0, 60);
            push({ kind: "landmark", el, depth, modal: isModal, view: inViewport(el),
                   role: isModal && mark !== "dialog" ? "dialog" : mark, name: label });
            childDepth = depth + 1;
          } else if (O.text && !NOT_TEXT[el.tagName]) {
            const t = ownText(el);
            if (t.length > 1) push({ kind: "text", el, depth, modal: isModal, view: inViewport(el),
                                     role: "", name: t.slice(0, 200) });
          }
        }
      }

      // An <iframe> is a document boundary. Same-origin content is walked inline (it was invisible
      // to every previous snapshot); cross-origin content is NOT reachable from page JS at all, so
      // it is REPORTED — a control that is genuinely there but unreachable must never read as absent.
      if (el.tagName === "IFRAME") {
        let doc = null;
        try { doc = el.contentDocument; } catch (e) { doc = null; }
        if (doc && doc.documentElement) {
          if (shown) walk(doc.documentElement, childDepth + 1, isModal);
        } else if (shown) {
          frames.push({ src: (el.getAttribute("src") || "").slice(0, 200),
                        name: el.getAttribute("name") || el.getAttribute("title") ||
                              el.getAttribute("aria-label") || "" });
        }
        continue;       // an iframe's own children are its fallback content, never rendered
      }

      // Open roots come off the element; closed ones are recovered from the WeakMap shadow.js
      // filled in at document_start (el.shadowRoot stays null for those, by design).
      const sub = el.shadowRoot || (window.__collieClosedRoots ? window.__collieClosedRoots.get(el) : null);
      if (sub) walk(sub, childDepth, isModal);
      walk(el, childDepth, isModal);
    }
  };
  walk(document.documentElement || document, 0, false);

  // Collapse runs of identical siblings — a feed's 30 "Reply" links cost 30 lines and say one thing.
  // Every one of them still gets a ref, so any single item stays addressable; only the repetition is
  // dropped, and the line says which refs it covers.
  const merged = [];
  for (const it of items) {
    const prev = merged[merged.length - 1];
    if (prev && it.kind === "control" && prev.kind === "control" && prev.depth === it.depth &&
        prev.role === it.role && prev.name === it.name && prev.suffix === it.suffix) {
      (prev.run = prev.run || [prev.el]).push(it.el);
      continue;
    }
    merged.push(Object.assign({}, it));
  }

  // The cap decides what SURVIVES, and importance beats document order. Cutting in document order is
  // what once dropped a just-opened modal — it is appended last in the body — and reported the page
  // as if the dialog were not there.
  const score = (it) => (it.modal ? 0 : 2) + (it.view ? 0 : 1) +
                        (it.kind === "control" ? 0 : it.kind === "heading" ? 0.5 : it.kind === "landmark" ? 0.6 : 1);
  let keep = merged;
  let dropped = 0;
  if (merged.length > CAP) {
    const order = merged.map((it, i) => [score(it), i]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const chosen = new Set(order.slice(0, CAP).map((p) => p[1]));
    dropped = merged.length - chosen.size;
    keep = merged.filter((_, i) => chosen.has(i));
  }

  // Landmarks earn their line only if something was kept inside them; a page of empty "region" and
  // "list" headers is noise the model pays for.
  const out = [];
  let n = 0;
  for (let i = 0; i < keep.length; i++) {
    const it = keep[i];
    if (it.kind === "landmark") {
      let hasChild = false;
      for (let j = i + 1; j < keep.length && keep[j].depth > it.depth; j++) {
        if (keep[j].kind !== "landmark") { hasChild = true; break; }
      }
      if (!hasChild) continue;
    }
    const pad = "  ".repeat(Math.min(it.depth, 8));
    if (it.kind === "control") {
      const first = "e" + (++n);
      refs.set(first, it.el);
      let line = pad + "[" + first + "] " + it.role + (it.name ? ' "' + it.name + '"' : "") + it.suffix;
      if (it.run) {
        const ids = [first];
        for (let k = 1; k < it.run.length; k++) { const r = "e" + (++n); refs.set(r, it.run[k]); ids.push(r); }
        line += " ×" + it.run.length + " (identical siblings: " + ids[0] + "–" + ids[ids.length - 1] + ")";
      }
      out.push(line);
    } else if (it.kind === "heading") {
      out.push(pad + it.role + ' "' + it.name + '"');
    } else if (it.kind === "landmark") {
      out.push(pad + it.role + (it.name ? ' "' + it.name + '"' : ""));
    } else {
      out.push(pad + '"' + it.name + '"');
    }
  }
  for (const f of frames) {
    out.push("iframe (cross-origin — its contents are NOT in this list) " +
             (f.name ? '"' + f.name + '" ' : "") + (f.src || ""));
  }
  return { count: n, truncated: dropped > 0 || overflowed, dropped,
           frames: frames.length, url: location.href,
           snapshot: out.join("\n") || "(no interactive elements found)" };
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

// Classify a snapshot ref before the restricted Mission browser may click it. This is an
// enforcement boundary, not a model prompt: navigation/menu/focus steps may proceed, while a
// final external write, consent, purchase, destructive action or human-verification control stays
// behind the outer Mission gate. Only exact refs are accepted, never fuzzy text/coordinates.
function pageAdvanceInfo(ref) {
  const m = window.__collieRefs;
  const el = m && m.get ? m.get(ref) : null;
  if (!el || !el.isConnected) return { error: "no live element for ref " + ref + " — take a fresh browser_snapshot" };
  const label = (el.getAttribute("aria-label") || el.innerText || el.value ||
                 el.getAttribute("title") || ref || "").trim().slice(0, 160);
  const role = (el.getAttribute("role") || "").toLowerCase();
  const tag = (el.tagName || "").toLowerCase();
  const type = (el.getAttribute("type") || "").toLowerCase();
  const link = tag === "a" ? el : (el.closest ? el.closest("a") : null);
  const href = link ? String(link.getAttribute("href") || link.href || "") : "";
  const meta = [label, role, tag, type, href, el.id || "", el.getAttribute("name") || "",
                el.getAttribute("data-testid") || ""].join(" ");
  if (el.disabled || el.getAttribute("aria-disabled") === "true")
    return { error: "ref " + ref + " is disabled" };
  if (type === "file") return { error: "file controls require the gated upload path" };
  if (/(captcha|recaptcha|hcaptcha|human.?verification|verify.?you.?are.?human|security.?challenge)/i.test(meta))
    return { error: "CAPTCHA or human verification requires Needs You" };
  if (/(?:^|\b)(post|publish|send|submit|save|create\s+(?:account|page)|sign\s*up|register|authorize|grant\s+access|allow\s+access|approve|pay|buy|purchase|checkout|place\s+order|delete|remove|deactivate|unsubscribe|log\s*out|sign\s*out)(?:\b|$)/i.test(label))
    return { error: "consequential control '" + label + "' requires the outer Mission gate" };
  if (href && /(?:^|[/?&=])(?:logout|signout|unsubscribe|delete|remove|deactivate|activate|verify|confirm)(?:[/?&=]|$)/i.test(href))
    return { error: "consequential navigation requires the outer Mission gate" };
  const editable = !!el.isContentEditable || el.getAttribute("contenteditable") !== null ||
                   role === "textbox" || tag === "input" || tag === "textarea";
  return { allowed: true, label, role, tag, href: href.slice(0, 300), editable };
}

function pageTypeRef(ref, text, submit) {
  const m = window.__collieRefs;
  const el = m && m.get ? m.get(ref) : null;
  if (!el || !el.isConnected) return { error: "no live element for ref " + ref + " — take a fresh browser_snapshot" };
  el.focus();
  // A native <select> is reported as `combobox` by the snapshot, so the model is told it can act on
  // it — but the input-value setter below writes nothing to one, and every attempt came back
  // "typed" with landed:false and the old value still selected. Choosing the option is the only
  // thing that moves a <select>; typing into it never will.
  if (el.tagName === "SELECT") {
    const want = (text || "").trim().toLowerCase();
    const opts = [...el.options];
    const hit = opts.find((o) => (o.text || "").trim().toLowerCase() === want)
             || opts.find((o) => (o.value || "").trim().toLowerCase() === want)
             || opts.find((o) => (o.text || "").toLowerCase().includes(want));
    if (!hit) return { error: "no option matching " + text,
                       options: opts.map((o) => (o.text || "").trim()).slice(0, 12) };
    const sd = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value");
    if (sd && sd.set) sd.set.call(el, hit.value); else el.value = hit.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { picked: (hit.text || "").trim(), value: el.value, landed: el.value === hit.value };
  }
  if (el.isContentEditable || el.getAttribute("contenteditable") !== null) {
    el.textContent = text;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true,
                                               inputType: "insertText", data: text }));
  } else {
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, "value");
    if (d && d.set) d.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
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

// Is it there yet? The post-condition a multi-step script waits on between steps, so a page that
// renders asynchronously (every SPA) does not need a blind sleep long enough to cover the worst case.
// Self-contained (injected into the PAGE).
function pageHas(text, selector) {
  if (selector) {
    try { const el = document.querySelector(selector); return { found: !!el }; }
    catch (e) { return { error: "bad selector " + selector }; }
  }
  const t = String(text || "").toLowerCase();
  if (!t) return { error: "wait_for needs text or selector" };
  const body = (document.body && document.body.innerText) || "";
  return { found: body.toLowerCase().indexOf(t) >= 0 };
}

// Scroll the page (or an element from the last snapshot) — the step a long page needs before its
// controls are even laid out. Self-contained (injected into the PAGE, MAIN world for refs).
function pageScroll(to, by, ref) {
  if (ref) {
    const m = window.__collieRefs;
    const el = m && m.get ? m.get(ref) : null;
    if (!el || !el.isConnected) return { error: "no live element for ref " + ref + " — take a fresh browser_snapshot" };
    el.scrollIntoView({ block: "center", inline: "center" });
    return { scrolled: "to " + ref, y: window.scrollY };
  }
  if (to === "top") window.scrollTo(0, 0);
  else if (to === "bottom") window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
  else window.scrollBy(0, Number(by) || Math.round(innerHeight * 0.9));
  return { scrolled: to || ("by " + (Number(by) || Math.round(innerHeight * 0.9))), y: window.scrollY,
           bottom: Math.abs((window.scrollY + innerHeight) - (document.body ? document.body.scrollHeight : 0)) < 4 };
}

async function exec(func, args) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
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
  if (!tab) return { error: NO_TAB };
  const [res] = await chrome.scripting.executeScript({
    target: { tabId: tab.id }, world: "MAIN", func, args });
  return res.result;
}

async function ensureConsoleCapture(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", func: installConsoleCapture });
  } catch (e) { /* chrome:// pages etc. can't be scripted; console just stays empty there */ }
}

const NO_TAB = "this space has no tab yet — call browser_open(url) first. It opens a tab of collie's " +
  "own in YOUR real browser, so your logins apply without touching the tabs you are using. To work " +
  "in a page you already have open, hand it over deliberately with browser_tabs(action='attach').";

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
  // Every child session died with the attachment; keeping their ids would hand out dead handles.
  if (src && src.tabId != null) frameSessions.delete(src.tabId);
  if (reason === "canceled_by_user") { try { chrome.storage.local.set({ trustedInput: false }); } catch (e) {} }
});
async function ensureAttached(tabId) {
  if (dbgTab === tabId) return;
  if (dbgTab != null) { const old = dbgTab; dbgTab = null; await dbgDetach(old); }
  await dbgAttach(tabId);
  dbgTab = tabId;
}

// --- cross-origin iframes (OOPIF) over CDP -------------------------------------------------------
// Page JS cannot see inside a cross-origin iframe — same-origin policy, and no extension permission
// changes that. So an embedded checkout, a payment field, a booking widget, a Stripe/Recaptcha frame
// were invisible to every snapshot collie has ever taken, and the model was told there was no such
// control: indistinguishable from the control not existing. CDP is the way in — each out-of-process
// frame is its own TARGET, and evaluating in that target's session is the only route.
//
// Cost is the debugger banner, so this is OPT-IN per call (frames:true) and detaches afterwards
// unless the trusted-input path was already holding the session. Refs from a frame are tagged
// `f1e7`, and clicking one is translated back into top-level viewport coordinates so it can still be
// a real trusted click; when the translation is not available it falls back to a synthetic click IN
// the frame and says which of the two it did — the same "never regress below synthetic, never lie
// about it" rule the rest of this file follows.
// Sessions are remembered for as long as the attachment that owns them lives, NOT only while a
// collector happens to be listening. `Target.setAutoAttach` announces a frame ONCE per connection;
// asking a second time is silent because the child is already attached — so a collector that only
// counts freshly-fired events sees the frames on the first call and an empty page on every call
// after it ("frame f1 is no longer on the page", while the frame sat there the whole time). The map
// is emptied when the debugger detaches, which is exactly when those session ids stop being valid.
const frameSessions = new Map();       // tabId -> Map(targetId -> {sessionId, targetId, url})
let frameIndex = null;                 // { tabId, frames: [{tag, sessionId, targetId, url}] }

chrome.debugger.onEvent.addListener((src, method, params) => {
  if (!src || src.tabId == null || !params) return;
  if (method === "Target.attachedToTarget" && params.sessionId) {
    const info = params.targetInfo || {};
    if (info.type !== "iframe") return;
    let m = frameSessions.get(src.tabId);
    if (!m) { m = new Map(); frameSessions.set(src.tabId, m); }
    // Keyed by TARGET, so a frame that re-attaches replaces its dead session instead of sitting
    // beside it and being found first.
    m.set(info.targetId, { sessionId: params.sessionId, targetId: info.targetId, url: info.url || "" });
  } else if (method === "Target.detachedFromTarget" && params.sessionId) {
    const m = frameSessions.get(src.tabId);
    if (!m) return;
    for (const [tid, s] of m) if (s.sessionId === params.sessionId) m.delete(tid);
  }
});

// Send a command to a CHILD session (an OOPIF) rather than the tab's root session. `sessionId` on
// the debuggee is what makes the flat protocol usable from an extension; a Chrome too old to know
// the field rejects the call, and we report that rather than pretending the frame is empty.
function dbgSendSession(tabId, sessionId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId, sessionId }, method, params || {}, (res) => {
      const e = chrome.runtime.lastError;
      if (e) reject(new Error(e.message)); else resolve(res);
    });
  });
}

async function frameEval(tabId, sessionId, expression) {
  const r = await dbgSendSession(tabId, sessionId, "Runtime.evaluate",
                                 { expression, returnByValue: true, awaitPromise: true });
  if (r && r.exceptionDetails)
    throw new Error((r.exceptionDetails.exception && r.exceptionDetails.exception.description) ||
                    r.exceptionDetails.text || "frame eval failed");
  return r && r.result ? r.result.value : undefined;
}

// Turn a self-contained page function into an expression CDP can evaluate inside a frame. Same trick
// the injected paths use — these functions may not reference anything in extension scope.
function asCall(fn, args) {
  return "(" + fn.toString() + ").apply(null," + JSON.stringify(args || []) + ")";
}

async function collectFrameSessions(tabId, waitMs) {
  try {
    // flatten:true is what delivers child sessions on this connection instead of a separate socket.
    await dbgSend(tabId, "Target.setAutoAttach",
                  { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
    await new Promise((r) => setTimeout(r, waitMs || 400));   // attachedToTarget arrives async
  } catch (e) { /* report through the empty list: the caller says "no frames" rather than throwing */ }
  return [...(frameSessions.get(tabId) || new Map()).values()];
}

// Where the frame sits in the TOP-level viewport, so a click inside it can be placed by the same
// CDP Input path as everything else. The owning <iframe> element lives in the parent document, which
// page JS can see even when the content is off-limits — but going through CDP keeps it in one place.
// Injected into the PARENT document (MAIN world): where does that <iframe> sit on screen? The parent
// can see the iframe ELEMENT perfectly well — same-origin policy hides the frame's CONTENT, not the
// box it occupies — so this is all it takes to turn a point inside the frame into a page coordinate.
// Scrolls the frame into view first, because a click can only be placed inside the viewport.
// Self-contained (injected).
function pageFrameBox(src, nth) {
  const all = [...document.querySelectorAll("iframe")];
  let list = src ? all.filter((f) => f.src === src || f.getAttribute("src") === src) : all;
  if (!list.length) list = all;
  const el = list[Math.min(nth || 0, list.length - 1)];
  if (!el) return { error: "no <iframe> for " + (src || "(any)") };
  let r = el.getBoundingClientRect();
  const onScreen = () => r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  if (!onScreen()) { el.scrollIntoView({ block: "center", inline: "center" }); r = el.getBoundingClientRect(); }
  // Frame coordinates start INSIDE the border and padding; skipping that shifts every click by the
  // border width, which on a 1px-bordered payment frame is enough to miss a small control.
  const cs = getComputedStyle(el);
  const num = (v) => parseFloat(v) || 0;
  return { x: r.left + num(cs.borderLeftWidth) + num(cs.paddingLeft),
           y: r.top + num(cs.borderTopWidth) + num(cs.paddingTop),
           inView: onScreen() };
}

// Where the frame sits in the TOP-level viewport, so a click inside it can be placed by the same CDP
// Input path as everything else.
//
// NOT via CDP: `Page.getFrameOwner` is one of the methods chrome.debugger does not expose to
// extensions at all ("'Page.getFrameOwner' wasn't found", -32601), and asking for it fails in a way
// that looks exactly like "this frame has no position" — which silently downgraded every click
// inside a cross-origin frame to a synthetic one. The parent document knows the answer anyway.
async function frameOffset(tabId, url, nth) {
  try {
    const box = await execMain(pageFrameBox, [url || "", nth || 0]);
    if (!box || box.error) return { error: (box && box.error) || "could not measure the frame" };
    if (!box.inView) return { error: "the frame is off-screen even after scrolling to it" };
    return box;
  } catch (e) { return { error: String((e && e.message) || e) }; }
}

// Run `fn(sessions)` with the tab's OOPIFs attached, then put the debugger back the way we found it.
async function withFrames(tabId, fn) {
  const held = dbgTab === tabId;                 // the trusted-input path owns a persistent session
  let attached = held;
  if (!held) {
    try {
      const targets = await new Promise((r) => chrome.debugger.getTargets((t) => r(t || [])));
      attached = targets.some((t) => t.tabId === tabId && t.attached);
    } catch (e) {}
    if (!attached) { await dbgAttach(tabId); }
  }
  try {
    return await fn(await collectFrameSessions(tabId));
  } finally {
    if (!held && !attached) {
      // Drop the handle too if the work inside took a persistent session out on this tab: leaving
      // dbgTab pointing at a detached tab makes the next ensureAttached a no-op, and every trusted
      // click after that fails on a session that is not there.
      if (dbgTab === tabId) dbgTab = null;
      try { await dbgDetach(tabId); } catch (e) {}
    }
  }
}

function splitFrameRef(ref) {
  const m = /^(f\d+)(e\d+)$/.exec(String(ref || ""));
  return m ? { tag: m[1], ref: m[2] } : null;
}

function lookupFrame(tabId, tag) {
  if (!frameIndex || frameIndex.tabId !== tabId) return null;
  return frameIndex.frames.find((f) => f.tag === tag) || null;
}

// Snapshot every cross-origin frame in the tab, tagging each frame's refs so they stay addressable
// (`f2e5` = the fifth control of the second frame).
async function snapshotFrames(tabId, max, opts) {
  return await withFrames(tabId, async (sessions) => {
    const frames = [];
    const blocks = [];
    for (let i = 0; i < sessions.length; i++) {
      const s = sessions[i];
      const tag = "f" + (i + 1);
      let data = null, err = "";
      try {
        // Wait for the FRAME to be ready, not just the page that hosts it. A cross-origin iframe
        // loads on its own schedule and the parent's load event says nothing about it, so a snapshot
        // taken too early captures a half-laid-out document — and the coordinates it hands out are
        // stale by the time anyone clicks them. (Seen as an intermittent "the click did not land".)
        for (let w = 0; w < 20; w++) {
          const ready = await frameEval(tabId, s.sessionId, "document.readyState");
          if (ready === "complete") break;
          await sleep(100);
        }
        data = await frameEval(tabId, s.sessionId, asCall(pageSnapshot, [max || 200, opts || {}]));
      } catch (e) {
        err = String((e && e.message) || e);
      }
      // How many earlier frames share this url — that is which <iframe> element it is in the parent
      // when a page embeds the same widget twice.
      const nth = frames.filter((f) => f.url === s.url).length;
      frames.push({ tag, sessionId: s.sessionId, targetId: s.targetId, url: s.url, nth });
      if (data && data.snapshot) {
        const body = String(data.snapshot)
          .replace(/\[e(\d+)\]/g, "[" + tag + "e$1]")
          .replace(/siblings: e(\d+)–e(\d+)/g, "siblings: " + tag + "e$1–" + tag + "e$2");
        blocks.push("── " + tag + " (cross-origin iframe) " + (s.url || "") + "\n" + body);
      } else {
        blocks.push("── " + tag + " (cross-origin iframe) " + (s.url || "") + "\n" +
                    "  (unreadable: " + (err || "no content") + ")");
      }
    }
    frameIndex = { tabId, frames };
    return { frames: frames.length, snapshot: blocks.join("\n") };
  });
}

// Click/type a `f1e7` ref: resolve the element inside its frame, then place a REAL click at the
// frame's offset when the geometry is available, else act synthetically inside the frame.
async function frameActRef(tabId, tag, ref, kind, text, submit) {
  const fr = lookupFrame(tabId, tag);
  if (!fr) return { error: "no frame " + tag + " on this tab — take a browser_snapshot with frames:true first" };
  const tab = await activeTab();
  return await withFrames(tabId, async (sessions) => {
    // Re-resolve the session EVERY time. A session id dies with the debugger attachment, and this
    // path deliberately detaches after each use, so the id the snapshot recorded is already stale by
    // the time anyone clicks something ("Session with given id not found"). The frame's TARGET id
    // survives, so that is what identifies it across attachments; its url is the fallback.
    const live = sessions.find((s) => s.targetId === fr.targetId) ||
                 sessions.find((s) => s.url && s.url === fr.url);
    if (!live)
      return { error: "frame " + tag + " is no longer on the page (it navigated or was removed) — " +
                      "re-run browser_snapshot with frames:true" };
    const sid = live.sessionId;
    let pt = null;
    try { pt = await frameEval(tabId, sid, asCall(pagePointRef, [ref])); }
    catch (e) { return { error: "frame " + tag + " is unreachable: " + String((e && e.message) || e) }; }
    if (!pt || pt.error) return pt || { error: "no element for ref " + tag + ref };
    // Same rule as the top-level paths: CDP input never reaches a background tab, so a "real" click
    // into a frame is only real if the tab is in front.
    const focused = tab ? await focusForTrusted(tab) : false;
    const geom = (pt.inView && focused) ? await frameOffset(tabId, live.url, fr.nth || 0) : null;
    const off = geom && !geom.error ? geom : null;
    if (off) {
      // Re-read the element's position with the frame box already measured, so the two halves of the
      // coordinate come from the same moment. Measuring them far apart is how a click lands where
      // the button USED to be when a page is still settling.
      let pt2 = null;
      try { pt2 = await frameEval(tabId, sid, asCall(pagePointRef, [ref])); } catch (e) {}
      if (pt2 && !pt2.error && pt2.inView) pt = pt2;
      const x = off.x + pt.x, y = off.y + pt.y;
      try {
        await ensureAttached(tabId);
        const b = { x, y, button: "left" };
        await dbgSend(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, buttons: 0 });
        await dbgSend(tabId, "Input.dispatchMouseEvent", Object.assign({ type: "mousePressed", buttons: 1, clickCount: 1 }, b));
        await dbgSend(tabId, "Input.dispatchMouseEvent", Object.assign({ type: "mouseReleased", buttons: 0, clickCount: 1 }, b));
        if (kind === "type") {
          await dbgSend(tabId, "Input.dispatchKeyEvent", { type: "keyDown", modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
          await dbgSend(tabId, "Input.dispatchKeyEvent", { type: "keyUp", modifiers: 2, key: "a", code: "KeyA", windowsVirtualKeyCode: 65 });
          await dbgSend(tabId, "Input.insertText", { text: text || "" });
          if (submit) {
            await dbgSend(tabId, "Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
            await dbgSend(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
          }
          const back = await frameEval(tabId, sid, asCall(pageValue, [ref, ""]));
          const want = String(text || "").trim().slice(0, 60);
          return { typed: String(text || "").slice(0, 40), trusted: true, frame: tag,
                   value: String((back && back.value) || "").slice(0, 120),
                   landed: !want || String((back && back.value) || "").indexOf(want) >= 0 };
        }
        return { clicked: pt.label, trusted: true, frame: tag };
      } catch (e) {
        if (dbgTab === tabId) dbgTab = null;   // fall through to the synthetic path below
      }
    }
    // No usable geometry (frame scrolled out of view, or getFrameOwner refused): act inside the
    // frame instead. Synthetic events, and the result says so — a site that gates on isTrusted will
    // ignore this, and the caller needs to know that is what happened.
    const why = off ? "the trusted click failed"
                    : !focused ? NO_FOCUS
                    : !pt.inView ? "the element is off-screen inside the frame"
                    : "the frame's position on the page could not be read (" +
                      ((geom && geom.error) || "unknown") + ")";
    try {
      if (kind === "type") {
        const r = await frameEval(tabId, sid, asCall(pageTypeRef, [ref, text, !!submit]));
        const back = await frameEval(tabId, sid, asCall(pageValue, [ref, ""]));
        const want = String(text || "").trim().slice(0, 60);
        return Object.assign({ trusted: false, frame: tag,
                               note: why + "; typed synthetically inside the frame",
                               value: String((back && back.value) || "").slice(0, 120),
                               landed: !want || String((back && back.value) || "").indexOf(want) >= 0 }, r || {});
      }
      const r = await frameEval(tabId, sid, asCall(pageClickRef, [ref]));
      return Object.assign({ trusted: false, frame: tag,
                             note: why + "; clicked synthetically inside the frame" }, r || {});
    } catch (e) {
      return { error: "frame " + tag + ": " + String((e && e.message) || e) };
    }
  });
}

// CDP input is delivered to the tab the browser is SHOWING. A background tab swallows it silently:
// Input.insertText writes nothing and Input.dispatchMouseEvent clicks nothing, and BOTH still return
// success — so the caller is told a real click happened when the page never saw one. (Measured, not
// assumed: with the tab in the background a trusted type read back "" and a trusted click left the
// page untouched.) That became reachable the day collie stopped typing into whatever tab the user
// had in front of them and started opening its own, which is the right thing to do — so the fix
// belongs here.
//
// The fix is NOT to steal the user's view. `Emulation.setFocusEmulationEnabled` makes the renderer
// treat the tab as focused, and with it on, input reaches a background tab exactly as it would a
// foreground one: measured on a tab that was never activated, the click arrived with
// **isTrusted true**, the text landed, and it kept working across a navigation. As a bonus the page
// reads `visibilityState: "visible"`, so sites that pause rendering, timers or lazy-loading while
// hidden behave normally instead of quietly doing nothing.
//
// Bringing the tab forward is kept only as the fallback for a browser that will not emulate, and
// synthetic input as the fallback to that — never a trusted claim we cannot back.
async function focusForTrusted(tab) {
  try {
    await ensureAttached(tab.id);
    await dbgSend(tab.id, "Emulation.setFocusEmulationEnabled", { enabled: true });
    return true;                            // the tab stays where it is; the user is not disturbed
  } catch (e) {
    if (dbgTab === tab.id) dbgTab = null;
  }
  if (tab.active) return true;
  try {                                     // fallback: a tab switch inside Chrome, as pageShot does
    await chrome.tabs.update(tab.id, { active: true });
    await sleep(120);                       // let the switch commit before input is dispatched
    const t = await chrome.tabs.get(tab.id);
    return !!t.active;
  } catch (e) { return false; }
}

const NO_FOCUS = "collie's tab could be neither focus-emulated nor brought to the front, so a REAL " +
                 "click was impossible (CDP input never reaches a background tab); acted " +
                 "synthetically instead — a site that checks isTrusted will ignore it";

async function trustedClick(text, selector) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  if (!(await focusForTrusted(tab)))
    return Object.assign({ trusted: false, note: NO_FOCUS },
                         await exec(pageClick, [text || "", selector || ""]));
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
  if (!(await focusForTrusted(tab)))
    return Object.assign({ trusted: false, note: NO_FOCUS },
                         await exec(pageType, [selector, text, !!submit]));
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
  if (!(await focusForTrusted(tab)))
    return Object.assign({ trusted: false, note: NO_FOCUS }, await execMain(pageClickRef, [ref]));
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

// Report the tag behind a ref, so the trusted path can tell a <select> from a text field before it
// commits to keystrokes. Self-contained (injected into the PAGE, MAIN world).
function pageTagRef(ref) {
  const m = window.__collieRefs;
  const el = m && m.get ? m.get(ref) : null;
  return el && el.isConnected ? el.tagName : "";
}

async function trustedTypeRef(ref, text, submit) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  // Real keystrokes cannot drive a native <select>: CDP Input.insertText goes nowhere on one, and
  // the OS dropdown a genuine click opens is not something we can pick from. Choosing the option in
  // the DOM is the only path that lands, so route selects there before touching the debugger.
  try {
    if ((await execMain(pageTagRef, [ref])) === "SELECT")
      return Object.assign({ trusted: false, note: "native <select>: option chosen in the DOM" },
                           await execMain(pageTypeRef, [ref, text, !!submit]));
  } catch (e) {}
  if (!(await focusForTrusted(tab)))
    return Object.assign({ trusted: false, note: NO_FOCUS },
                         await execMain(pageTypeRef, [ref, text, !!submit]));
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

// --- the rest of a hand: keys, hover, drag, and a point on the screen ----------------------------
// Clicking and typing covered most of the web, and the gaps were all the same shape: a control that
// answers to something OTHER than "click this element / put text in this field". A menu that only
// opens on hover, a dialog that only closes on Escape, a list that is reordered by dragging, a slider
// puzzle, a chart where the thing you want has no element of its own. All four are the same CDP
// primitives the trusted click already uses — they were simply never exposed.

// CDP wants a virtual-key code, not just a name. Only the keys worth pressing are listed; a single
// character falls through to the generic branch.
const KEYS = {
  enter: { key: "Enter", code: "Enter", vk: 13, text: "\r" },
  tab: { key: "Tab", code: "Tab", vk: 9, text: "\t" },
  escape: { key: "Escape", code: "Escape", vk: 27 },
  esc: { key: "Escape", code: "Escape", vk: 27 },
  backspace: { key: "Backspace", code: "Backspace", vk: 8 },
  delete: { key: "Delete", code: "Delete", vk: 46 },
  arrowup: { key: "ArrowUp", code: "ArrowUp", vk: 38 },
  arrowdown: { key: "ArrowDown", code: "ArrowDown", vk: 40 },
  arrowleft: { key: "ArrowLeft", code: "ArrowLeft", vk: 37 },
  arrowright: { key: "ArrowRight", code: "ArrowRight", vk: 39 },
  up: { key: "ArrowUp", code: "ArrowUp", vk: 38 },
  down: { key: "ArrowDown", code: "ArrowDown", vk: 40 },
  left: { key: "ArrowLeft", code: "ArrowLeft", vk: 37 },
  right: { key: "ArrowRight", code: "ArrowRight", vk: 39 },
  home: { key: "Home", code: "Home", vk: 36 },
  end: { key: "End", code: "End", vk: 35 },
  pageup: { key: "PageUp", code: "PageUp", vk: 33 },
  pagedown: { key: "PageDown", code: "PageDown", vk: 34 },
  space: { key: " ", code: "Space", vk: 32, text: " " },
};
const MODBIT = { alt: 1, ctrl: 2, control: 2, meta: 4, cmd: 4, command: 4, shift: 8 };

function keySpec(name) {
  const raw = String(name || "").trim();
  if (!raw) return null;
  const known = KEYS[raw.toLowerCase()];
  if (known) return known;
  if (raw.length === 1) {
    const upper = raw.toUpperCase();
    const code = /[a-z]/i.test(raw) ? "Key" + upper : (/[0-9]/.test(raw) ? "Digit" + raw : "");
    return { key: raw, code, vk: upper.charCodeAt(0), text: raw };
  }
  return null;
}

function modMask(mods) {
  let m = 0;
  for (const name of (Array.isArray(mods) ? mods : [])) {
    m |= MODBIT[String(name).toLowerCase()] || 0;
  }
  return m;
}

// Injected: the synthetic fallback for a key. It reaches listeners bound to keydown/keyup, which is
// most of them, and reaches nothing that depends on a real edit — hence `trusted:false` in the reply.
function pageKey(key, code, mods) {
  const el = document.activeElement || document.body;
  if (!el) return { error: "nothing focused to send a key to" };
  const init = { key: key, code: code, bubbles: true, cancelable: true,
                 altKey: !!(mods & 1), ctrlKey: !!(mods & 2), metaKey: !!(mods & 4),
                 shiftKey: !!(mods & 8) };
  el.dispatchEvent(new KeyboardEvent("keydown", init));
  el.dispatchEvent(new KeyboardEvent("keyup", init));
  return { pressed: key, on: (el.tagName || "").toLowerCase() };
}

// Injected: what is actually at this point? A coordinate click is the one addressing mode with no
// element behind it, so the reply says what it landed on — otherwise "I clicked (400,300)" is a
// claim nobody can check.
function pageElementAt(x, y) {
  const el = document.elementFromPoint(x, y);
  if (!el) return { at: null };
  return { at: (el.tagName || "").toLowerCase(),
           name: (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().slice(0, 60),
           id: el.id || "", cls: (typeof el.className === "string" ? el.className : "").slice(0, 60) };
}

// Injected: the synthetic hover — the pointer/mouse event sequence a menu listens for.
function pageHover(ref, selector, text) {
  let el = null;
  if (ref) { const m = window.__collieRefs; el = m && m.get ? m.get(ref) : null; }
  else if (selector) { try { el = document.querySelector(selector); } catch (e) { return { error: "bad selector " + selector }; } }
  else if (text) {
    const t = String(text).toLowerCase();
    el = [...document.querySelectorAll("a,button,[role=button],li,div,span")]
      .find((e) => ((e.innerText || "").trim().toLowerCase()).includes(t));
  }
  if (!el) return { error: "no element to hover for " + (ref || selector || text) };
  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
  for (const type of ["pointerover", "pointerenter", "mouseover", "mouseenter", "pointermove", "mousemove"]) {
    try { el.dispatchEvent(new (type.startsWith("pointer") ? PointerEvent : MouseEvent)(type, opts)); }
    catch (e) { el.dispatchEvent(new MouseEvent(type.replace("pointer", "mouse"), opts)); }
  }
  return { hovered: (el.innerText || el.getAttribute("aria-label") || "").trim().slice(0, 60) || (el.tagName || "").toLowerCase() };
}

// Injected: HTML5 drag-and-drop. Plain mouse movement does NOT drive it — the browser only fires
// dragstart/drop for a real drag, so a page built on the HTML5 API sits there doing nothing while
// the pointer sails across it. Handing both elements a shared DataTransfer is the way in, and it is
// what every automation library ends up doing.
function pageDragHtml5(from, to) {
  const find = (t) => {
    t = t || {};
    if (t.ref) { const m = window.__collieRefs; return m && m.get ? m.get(t.ref) : null; }
    if (t.selector) { try { return document.querySelector(t.selector); } catch (e) { return null; } }
    if (t.text) {
      const s = String(t.text).toLowerCase();
      const all = [...document.querySelectorAll("*")]
        .filter((e) => ((e.innerText || "").trim().toLowerCase()).includes(s));
      return all.filter((e) => !all.some((o) => o !== e && e.contains(o)))[0] || null;
    }
    return null;
  };
  const src = find(from), dst = find(to);
  if (!src || !dst) return { error: "drag needs a source and a target that both resolve — "
                                    + "take a fresh browser_snapshot, or name them by selector" };
  const dt = new DataTransfer();
  const fire = (el, type) => {
    const r = el.getBoundingClientRect();
    const ev = new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt,
                                     clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 });
    el.dispatchEvent(ev);
    return ev;
  };
  fire(src, "dragstart");
  fire(dst, "dragenter");
  fire(dst, "dragover");
  fire(dst, "drop");
  fire(src, "dragend");
  return { dragged: "html5", from: (src.innerText || "").trim().slice(0, 40),
           to: (dst.innerText || "").trim().slice(0, 40) };
}

// Is this element one the HTML5 drag API owns? Injected.
function pageIsDraggable(target) {
  const t = target || {};
  let el = null;
  if (t.ref) { const m = window.__collieRefs; el = m && m.get ? m.get(t.ref) : null; }
  else if (t.selector) { try { el = document.querySelector(t.selector); } catch (e) { el = null; } }
  if (!el) return { draggable: false };
  return { draggable: el.draggable === true || el.getAttribute("draggable") === "true" };
}

// Resolve any of the three ways to name an element to a viewport point.
async function resolvePoint(target) {
  const t = target || {};
  if (t.ref) return await execMain(pagePointRef, [t.ref]);
  if (t.selector || t.text) return await exec(pagePoint, [t.text || "", t.selector || "", !!t.broad]);
  if (typeof t.x === "number" && typeof t.y === "number")
    return { x: t.x, y: t.y, inView: true, label: "(" + t.x + "," + t.y + ")" };
  return { error: "say which element: ref, selector, text — or x and y" };
}

async function doPress(key, mods, repeat) {
  const spec = keySpec(key);
  if (!spec) return { error: "unknown key " + key + " — use a single character or a name like Escape, Tab, ArrowDown" };
  const mask = modMask(mods);
  const times = Math.max(1, Math.min(20, Number(repeat) || 1));
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  if (await focusForTrusted(tab)) {
    try {
      await ensureAttached(tab.id);
      for (let i = 0; i < times; i++) {
        const down = { type: "keyDown", modifiers: mask, key: spec.key, code: spec.code,
                       windowsVirtualKeyCode: spec.vk, nativeVirtualKeyCode: spec.vk };
        // Text belongs to a PLAIN keystroke only. Sending it with a modifier held is how Ctrl+A
        // ends up typing the letter a into the field it was supposed to select.
        if (spec.text && !mask) down.text = spec.text;
        await dbgSend(tab.id, "Input.dispatchKeyEvent", down);
        await dbgSend(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", modifiers: mask,
                                                          key: spec.key, code: spec.code,
                                                          windowsVirtualKeyCode: spec.vk,
                                                          nativeVirtualKeyCode: spec.vk });
      }
      return { pressed: spec.key, modifiers: mods || [], times: times, trusted: true };
    } catch (e) {
      if (dbgTab === tab.id) dbgTab = null;
    }
  }
  const r = await execMain(pageKey, [spec.key, spec.code, mask]);
  return Object.assign({ trusted: false, times: 1,
                         note: "sent a synthetic key; a page that checks isTrusted will ignore it" },
                       r || {});
}

async function doHover(target) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  const pt = await resolvePoint(Object.assign({ broad: true }, target || {}));
  if (!pt || pt.error) return pt || { error: "nothing to hover" };
  if (await focusForTrusted(tab)) {
    try {
      await ensureAttached(tab.id);
      await execMain(pageCursor, [pt.x, pt.y]);
      await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: pt.x, y: pt.y, buttons: 0 });
      await sleep(350);                       // menus open on a timer; give it one
      return { hovered: pt.label, trusted: true };
    } catch (e) {
      if (dbgTab === tab.id) dbgTab = null;
    }
  }
  const t = target || {};
  const r = await execMain(pageHover, [t.ref || "", t.selector || "", t.text || ""]);
  return Object.assign({ trusted: false, note: "synthetic hover" }, r || {});
}

async function doDrag(from, to, steps) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  // An HTML5-draggable source needs the DataTransfer path; mouse movement alone does nothing there.
  const d = await execMain(pageIsDraggable, [from || {}]);
  if (d && d.draggable) {
    const r = await execMain(pageDragHtml5, [from || {}, to || {}]);
    if (r && !r.error) return r;
  }
  const a = await resolvePoint(from);
  if (!a || a.error) return a || { error: "no drag source" };
  const b = await resolvePoint(to);
  if (!b || b.error) return b || { error: "no drag target" };
  if (!(await focusForTrusted(tab))) return { error: NO_FOCUS };
  const n = Math.max(2, Math.min(60, Number(steps) || 12));
  try {
    await ensureAttached(tab.id);
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: a.x, y: a.y, buttons: 0 });
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mousePressed", x: a.x, y: a.y,
                                                        button: "left", buttons: 1, clickCount: 1 });
    for (let i = 1; i <= n; i++) {
      // Move in steps, not one jump: a sortable list or a slider tracks mousemove, and a single
      // teleport from A to B reads as no movement at all.
      const x = a.x + ((b.x - a.x) * i) / n, y = a.y + ((b.y - a.y) * i) / n;
      await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: x, y: y, button: "left", buttons: 1 });
      await sleep(16);
    }
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseReleased", x: b.x, y: b.y,
                                                        button: "left", buttons: 0, clickCount: 1 });
    return { dragged: "pointer", from: a.label, to: b.label, trusted: true };
  } catch (e) {
    if (dbgTab === tab.id) dbgTab = null;
    return { error: "drag failed: " + String((e && e.message) || e) };
  }
}

async function doClickAt(x, y) {
  const tab = await activeTab();
  if (!tab) return { error: NO_TAB };
  const before = await execMain(pageElementAt, [x, y]);
  if (!(await focusForTrusted(tab))) {
    return Object.assign({ trusted: false, note: NO_FOCUS },
                         await execMain(pageClickAtSynthetic, [x, y]));
  }
  try {
    await ensureAttached(tab.id);
    await execMain(pageCursor, [x, y]);
    await sleep(320);
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: x, y: y, buttons: 0 });
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mousePressed", x: x, y: y, button: "left", buttons: 1, clickCount: 1 });
    await dbgSend(tab.id, "Input.dispatchMouseEvent", { type: "mouseReleased", x: x, y: y, button: "left", buttons: 0, clickCount: 1 });
    return { clicked_at: [x, y], hit: before, trusted: true };
  } catch (e) {
    if (dbgTab === tab.id) dbgTab = null;
    return Object.assign({ trusted: false, note: "debugger unavailable, clicked synthetically" },
                         await execMain(pageClickAtSynthetic, [x, y]));
  }
}

// Injected: the synthetic coordinate click — find what is there and click it.
function pageClickAtSynthetic(x, y) {
  const el = document.elementFromPoint(x, y);
  if (!el) return { error: "nothing at (" + x + "," + y + ")" };
  el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, clientX: x, clientY: y }));
  el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, clientX: x, clientY: y }));
  el.click();
  return { clicked_at: [x, y], hit: { at: (el.tagName || "").toLowerCase() } };
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
  // A screenshot is measured in DEVICE pixels and every click takes CSS pixels, and on a scaled
  // display those are not the same number. Without the ratio the caller is guessing: on a 129%
  // display a click read off the image lands ~30% away and still returns ok, which is the worst
  // kind of wrong. So the viewport is asked for its own CSS size and the conversion is handed back
  // with the picture: css_x = image_x / scale.
  let view = null;
  try {
    view = await exec(pageViewport, []);
  } catch (e) { /* a page that refuses injection still gets a picture, just without the ratio */ }
  const out = { data: small.data, width: small.width, height: small.height, how,
                title: tab.title || "", url: tab.url || "" };
  if (view && view.w) {
    out.css_width = view.w;
    out.css_height = view.h;
    out.device_pixel_ratio = view.dpr;
    out.scale = +(small.width / view.w).toFixed(4);      // image pixels per CSS pixel
  }
  return out;
}

// Self-contained (injected into the PAGE): what the page thinks its own size is.
function pageViewport() {
  return { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio || 1 };
}

// Decide trusted vs synthetic for THIS step: a command can force it (trusted:true/false), otherwise
// resolve the per-origin authorization (session -> permanent -> global default ON).
async function wantTrusted(cmd) {
  if (cmd.trusted === true) return true;
  if (cmd.trusted === false) return false;
  const t = await activeTab();
  return await trustedForOrigin(t ? originOf(t) : "");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function runStep(cmd) {
    if (cmd.action === "open") {
      const url = httpUrl(cmd.url);
      if (!url) return { error: "browser_open only accepts http(s) URLs" };
      // A tab the user already has on this site is taken ONLY when the caller asked for it. The old
      // default — adopt whatever was open — is what let a run walk into a half-filled form the user
      // (or another collie job) had going. Cookies are per-profile, so collie's own tab is logged in
      // just the same; adopting buys their scroll position, not their session.
      let adopted = null;
      if (cmd.adopt) {
        adopted = await adoptTabForUrl(url, curSpace);
        if (adopted && adopted.error) return adopted;
      }
      const tab = adopted || await targetTab(true, { window: !!cmd.window });
      const already = adopted && (adopted.url || "").indexOf(url) === 0;
      if (!already) await navigateCollieTab(tab.id, url);
      return await exec(pageRead, []);
    }
    if (cmd.action === "show") {
      const tab = await targetTab(false);
      if (!tab) return { error: "no tab in space '" + curSpace + "' yet — open a page first" };
      const shown = await chrome.tabs.update(tab.id, { active: true });
      return { shown: true, title: shown.title || "", url: shown.url || "" };
    }
    // Explicitly hand collie the tab you are looking at ("finish what I started here"). This is the
    // ONLY way a tab collie did not open becomes collie's, and it is a deliberate user act.
    if (cmd.action === "attach") {
      let tab = null;
      if (cmd.tab_id != null) {
        try { tab = await chrome.tabs.get(cmd.tab_id); } catch (e) { return { error: "no tab with id " + cmd.tab_id }; }
      } else {
        try { const found = await chrome.tabs.query({ active: true, lastFocusedWindow: true }); tab = found && found[0]; }
        catch (e) {}
      }
      if (!tab) return { error: "could not tell which tab you are looking at" };
      if (!/^https?:/i.test(tab.url || ""))
        return { error: "that tab is not an ordinary web page (" + (tab.url || "").slice(0, 60) + ") — collie cannot script it" };
      const held = await spaceHolding(tab.id, curSpace);
      if (held) return { error: "that tab is already space '" + held + "'s" };
      await setSpace(curSpace, { tabId: tab.id, owned: false, adopted: true });
      return { attached: true, space: curSpace, title: tab.title || "", url: tab.url || "" };
    }
    if (cmd.action === "spaces") {
      const all = await loadSpaces();
      const out = [];
      for (const name of Object.keys(all)) {
        const rec = all[name] || {};
        let tab = null;
        try { tab = await chrome.tabs.get(rec.tabId); } catch (e) {}
        if (!tab) { await dropSpace(name); continue; }
        out.push({ space: name, tab_id: rec.tabId, owned: !!rec.owned, active: !!tab.active,
                   title: (tab.title || "").slice(0, 60), url: (tab.url || "").slice(0, 120) });
      }
      return { spaces: out, current: curSpace };
    }
    // Give a space's tab back. A tab collie did not open is never CLOSED — dropping the claim is the
    // most collie is entitled to do with someone else's window.
    if (cmd.action === "release") {
      const rec = await getSpace(curSpace);
      if (!rec) return { released: false, note: "space '" + curSpace + "' has no tab" };
      await dropSpace(curSpace);
      if (cmd.close) {
        if (!rec.owned) return { released: true, closed: false,
                                 note: "the claim on that tab is dropped, but it was YOUR tab, not one collie opened, so it was left open" };
        try { await chrome.tabs.remove(rec.tabId); } catch (e) {}
        return { released: true, closed: true };
      }
      return { released: true, closed: false };
    }
    if (cmd.action === "read") return await exec(pageRead, []);
    if (cmd.action === "snapshot") {
      const top = await execMain(pageSnapshot, [cmd.max || 200, { text: !!cmd.text }]);
      if (!cmd.frames || !top || top.error) return top;
      const tab = await activeTab();
      if (!tab) return top;
      try {
        const fr = await snapshotFrames(tab.id, cmd.max || 200, { text: !!cmd.text });
        return Object.assign({}, top, { frames: fr.frames,
                                        snapshot: top.snapshot + (fr.snapshot ? "\n" + fr.snapshot : "") });
      } catch (e) {
        // Say the reach failed rather than returning the top document as if it were the whole page.
        return Object.assign({}, top, { frames_error: String((e && e.message) || e) });
      }
    }
    if (cmd.action === "links") return await exec(pageLinks, [cmd.filter || ""]);
    if (cmd.action === "screenshot") return await pageShot(cmd.full_page === true, cmd.max_dim || 1568);
    if (cmd.action === "wait") { await sleep(Math.min(30000, Math.max(0, Number(cmd.ms) || 500))); return { waited_ms: Number(cmd.ms) || 500 }; }
    if (cmd.action === "wait_for") {
      const budget = Math.min(60000, Math.max(500, Number(cmd.timeout_ms) || 10000));
      const started = Date.now();
      for (;;) {
        const r = await exec(pageHas, [cmd.text || "", cmd.selector || ""]);
        if (r && r.error) return r;
        if (r && r.found) return { found: true, waited_ms: Date.now() - started };
        if (Date.now() - started >= budget)
          return { error: "waited " + budget + "ms and " + (cmd.selector ? "selector " + cmd.selector : '"' + (cmd.text || "") + '"') +
                          " never appeared — the page may not have got there, or the wording differs" };
        await sleep(250);
      }
    }
    if (cmd.action === "scroll") return await execMain(pageScroll, [cmd.to || "", cmd.by || 0, cmd.ref || ""]);
    if (cmd.action === "mode") {   // read/set high-fidelity input from the bridge/CLI
      if (typeof cmd.trusted === "boolean") await chrome.storage.local.set({ trustedInput: cmd.trusted });
      if (cmd.origin && cmd.scope) await setSiteMode(cmd.origin, cmd.scope);
      const t = await targetTab(false);
      const origin = t ? originOf(t) : "";
      return { global: await trustedGlobal(), origin, effective: await trustedForOrigin(origin) };
    }
    if (cmd.action === "press")
      return await doPress(cmd.key, cmd.modifiers, cmd.repeat);
    if (cmd.action === "hover")
      return await doHover({ ref: cmd.ref, selector: cmd.selector, text: cmd.text,
                             x: cmd.x, y: cmd.y });
    if (cmd.action === "drag")
      return await doDrag(cmd.from || {}, cmd.to || {}, cmd.steps);
    if (cmd.action === "click") {
      // A point on the screen is its own addressing mode — for a canvas, a map, a chart, anything
      // whose target is not an element. The reply says what was under the point, because otherwise
      // "clicked (400,300)" is a claim with nothing behind it.
      if (typeof cmd.x === "number" && typeof cmd.y === "number" && !cmd.ref && !cmd.text && !cmd.selector) {
        const r = await doClickAt(cmd.x, cmd.y);
        await sleep(600);
        return { click: r, page: await exec(pageRead, []) };
      }
      let r;
      const fref = splitFrameRef(cmd.ref);
      if (fref) {                                     // a ref from inside a cross-origin iframe
        const tab = await activeTab();
        if (!tab) return { error: NO_TAB };
        r = await frameActRef(tab.id, fref.tag, fref.ref, "click");
      } else if (cmd.ref) {                           // act on the exact element from a browser_snapshot
        r = (await wantTrusted(cmd)) ? await trustedClickRef(cmd.ref) : await execMain(pageClickRef, [cmd.ref]);
      } else {
        r = (await wantTrusted(cmd)) ? await trustedClick(cmd.text || "", cmd.selector || "")
                                     : await exec(pageClick, [cmd.text || "", cmd.selector || ""]);
      }
      await sleep(800);
      return { click: r, page: await exec(pageRead, []) };
    }
    if (cmd.action === "advance") {
      if (!cmd.ref || splitFrameRef(cmd.ref))
        return { advance: { error: "browser_advance currently requires a top-page snapshot ref" } };
      const info = await execMain(pageAdvanceInfo, [cmd.ref]);
      if (!info || info.error || !info.allowed)
        return { advance: info || { error: "could not classify the target" } };
      const clicked = (await wantTrusted(cmd)) ? await trustedClickRef(cmd.ref)
                                               : await execMain(pageClickRef, [cmd.ref]);
      await sleep(500);
      if (clicked && clicked.error) return { advance: clicked };
      return { advance: Object.assign({}, info, clicked || {}), page: await exec(pageRead, []) };
    }
    if (cmd.action === "type") {
      let r;
      const fref = splitFrameRef(cmd.ref);
      if (fref) {
        const tab = await activeTab();
        if (!tab) return { error: NO_TAB };
        return await frameActRef(tab.id, fref.tag, fref.ref, "type", cmd.text, !!cmd.submit);
      }
      if (cmd.ref) {                                  // act on the exact field from a browser_snapshot
        r = (await wantTrusted(cmd)) ? await trustedTypeRef(cmd.ref, cmd.text, !!cmd.submit)
                                     : await execMain(pageTypeRef, [cmd.ref, cmd.text, !!cmd.submit]);
      } else if ((await wantTrusted(cmd)) && cmd.selector) {
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
    if (cmd.action === "form_snapshot") return await exec(pageFormSnapshot, []);
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
}

// --- many steps, one round trip ------------------------------------------------------------------
// A bridge round trip is a MODEL TURN: the tool result goes back through the loop, the model reads
// it and decides the next call. So filling a six-field form cost six turns of latency and six copies
// of the page in context, and the model spent most of them re-deciding things it already knew. A
// script says the whole sequence up front and pays for one turn.
//
// Two rules make it safe to give up that per-step supervision:
//   · it STOPS at the first failure and reports which step stopped it, with the same hard-failure
//     signals a single call would have raised (a type that did not land is a failure, not a note);
//   · only the LAST step returns its full payload. Intermediate page reads are summarised, because
//     a script whose every step returned the whole page would cost more context than the calls it
//     replaced — which would defeat the entire point.
function stepSummary(r) {
  if (r == null) return { ok: true };
  if (typeof r === "string") return { ok: true, text: r.length > 200 ? r.slice(0, 200) + "…" : r };
  if (Array.isArray(r)) return { ok: true, items: r.length };
  const out = { ok: !r.error };
  if (r.error) out.error = String(r.error).slice(0, 300);
  for (const k of ["clicked", "typed", "picked", "landed", "value", "trusted", "found", "waited_ms",
                   "uploaded", "attached", "count", "truncated", "scrolled", "frame", "note", "matches",
                   "pressed", "hovered", "dragged", "clicked_at", "times"]) {
    if (r[k] !== undefined) out[k] = typeof r[k] === "string" ? r[k].slice(0, 120) : r[k];
  }
  if (r.click && typeof r.click === "object") {           // click returns {click, page}
    if (r.click.error) { out.ok = false; out.error = String(r.click.error).slice(0, 300); }
    if (r.click.clicked) out.clicked = String(r.click.clicked).slice(0, 120);
    if (r.click.trusted !== undefined) out.trusted = r.click.trusted;
    if (r.click.matches) out.matches = r.click.matches;
  }
  return out;
}

// What counts as a failure worth stopping for. `landed:false` is here deliberately: a write that
// silently went nowhere is the failure this codebase has been burned by most, and a script must not
// keep going (and eventually submit) on top of one.
function stepFailed(r) {
  if (r == null) return false;
  if (typeof r === "string") return false;
  if (r.error) return true;
  if (r.landed === false) return true;
  if (r.attached === false) return true;
  if (r.click && r.click.error) return true;
  return false;
}

async function runScript(cmd) {
  const steps = Array.isArray(cmd.steps) ? cmd.steps : [];
  if (!steps.length) return { error: "browser_script needs a non-empty `steps` list" };
  if (steps.length > 40) return { error: "at most 40 steps in one script (got " + steps.length + ")" };
  const done = [];
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i] && typeof steps[i] === "object" ? steps[i] : {};
    const action = typeof step.action === "string" ? step.action : "";
    if (!action) {
      done.push({ step: i + 1, action: "", ok: false, error: "step has no `action`" });
      return { ok: false, ran: done.length, of: steps.length, stopped_at: i + 1, steps: done };
    }
    if (action === "script")
      return { ok: false, ran: done.length, of: steps.length, stopped_at: i + 1,
               steps: done.concat([{ step: i + 1, action, ok: false, error: "scripts do not nest" }]) };
    let r;
    try { r = await runStep(step); }
    catch (e) { r = { error: String((e && e.message) || e) }; }
    const failed = stepFailed(r);
    const last = i === steps.length - 1;
    done.push(Object.assign({ step: i + 1, action }, stepSummary(r)));
    if (failed)
      return { ok: false, ran: done.length, of: steps.length, stopped_at: i + 1, steps: done,
               // The failing step's own payload in full — that is the one worth reading.
               result: r };
    if (last) return { ok: true, ran: done.length, of: steps.length, steps: done, result: r };
  }
  return { ok: true, ran: done.length, of: steps.length, steps: done };
}

async function handle(cmd) {
  curSpace = spaceOf(cmd);
  try {
    if (cmd.action === "script") return await runScript(cmd);
    return await runStep(cmd);
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
// --- the shared secret ---------------------------------------------------------------------------
// The bridge only takes commands from a caller holding this machine's token, so the extension has to
// present it too. It is pasted in once through the popup (`collie browser-bridge --print-token`).
// A wrong or missing token gets a 401, and the badge says so — otherwise the extension would simply
// stop working against a bridge that looks perfectly healthy, which is the worst kind of silence.
let __token = null;
let __authFailed = false;

async function bridgeToken() {
  if (__token !== null) return __token;
  try {
    const s = await chrome.storage.local.get("collieToken");
    __token = typeof s.collieToken === "string" ? s.collieToken : "";
  } catch (e) { __token = ""; }
  if (!__token) __token = await tokenFromDisk();
  return __token;
}

// The bridge leaves the token in this extension's own directory, which only this extension can read
// (it is not web_accessible, so no page can fetch it). That is what makes the token invisible in
// normal use: nothing to copy, nothing to paste. A packed/store build has no such file, and then the
// popup's paste box is the way in.
async function tokenFromDisk() {
  try {
    const r = await fetch(chrome.runtime.getURL("token.txt"), { cache: "no-store" });
    if (!r.ok) return "";
    const t = (await r.text()).trim();
    if (t) { try { await chrome.storage.local.set({ collieToken: t }); } catch (e) {} }
    return t;
  } catch (e) { return ""; }
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.collieToken) __token = changes.collieToken.newValue || "";
});

async function bridgeHeaders(extra) {
  const t = await bridgeToken();
  const h = Object.assign({ "X-Collie-Bridge": "1" }, extra || {});
  if (t) h["Authorization"] = "Bearer " + t;
  return h;
}

async function noteAuthFailure(failed) {
  try {
    await chrome.storage.local.set({ collieAuthFailed: !!failed });
    await chrome.action.setBadgeText({ text: failed ? "!" : "" });
    if (failed) await chrome.action.setBadgeBackgroundColor({ color: "#b3261e" });
  } catch (e) {}
}

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
                              { headers: await bridgeHeaders() });
        if (r.status === 401) {
          // A rotated token is the likely cause, so re-read the file once before giving up; only a
          // build with no file (or a genuinely wrong token) gets as far as the badge. Then stop
          // hammering — the alarm retries in 30s, by which time the user may have pasted one in.
          const fresh = await tokenFromDisk();
          if (fresh && fresh !== __token) { __token = fresh; continue; }
          __authFailed = true;
          await noteAuthFailure(true);
          return;
        }
        if (__authFailed) { __authFailed = false; await noteAuthFailure(false); }
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
            headers: await bridgeHeaders({ "content-type": "application/json" }),
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
