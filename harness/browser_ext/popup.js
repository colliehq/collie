// Popup status panel for the collie bridge extension.
// Answers, at a glance, the question that cost a long debugging session: "is this thing actually
// connected, and is the collie I'm running the one it's talking to?"
const BRIDGE = "http://127.0.0.1:8677";
const $ = (id) => document.getElementById(id);

function setStatus(kind, title, sub) {
  $("dot").className = "dot " + kind;
  $("sTitle").textContent = title;
  $("sSub").textContent = sub;
}

async function currentTab() {
  try {
    const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!t) return "—";
    try { return new URL(t.url).host || t.url; } catch (e) { return t.url || "—"; }
  } catch (e) { return "—"; }
}

async function refresh() {
  const v = chrome.runtime.getManifest().version;
  $("ver").textContent = "v" + v;
  $("rTab").textContent = await currentTab();
  $("hint").textContent = "";
  setStatus("", "Checking…", "contacting the local bridge");
  try {
    const r = await fetch(BRIDGE + "/health", { cache: "no-store" });
    const d = await r.json();
    const ago = d.last_poll_secs_ago;
    $("rPoll").textContent = ago == null ? "never" : ago + "s ago";
    if (d.extension_connected) {
      setStatus("ok", "Connected", "collie can drive this browser");
      // A version mismatch means collie is serving a DIFFERENT copy of this extension than the one
      // Chrome loaded — the failure mode that makes every fix look like it did nothing.
      if (d.extension_version && d.extension_version !== v) {
        setStatus("warn", "Version mismatch",
          "bridge sees v" + d.extension_version + ", this is v" + v);
        $("hint").textContent = "Chrome loaded this extension from a different folder than the "
          + "collie you are running. Remove it and Load unpacked from that collie's "
          + "harness/browser_ext.";
      }
    } else {
      setStatus("warn", "Bridge up, not polling",
        "the extension has not reached it yet");
      $("hint").textContent = "Usually fixes itself in a few seconds. If not, reload the extension.";
    }
  } catch (e) {
    $("rPoll").textContent = "—";
    setStatus("bad", "Bridge not running", "nothing is listening on 8677");
    $("hint").textContent = "Start it with  collie browser-bridge  (or run  collie setup  to install "
      + "it at logon).";
  }
}

// --- high-fidelity (chrome.debugger) input: global default + per-site override -------------------
async function activeOrigin() {
  try {
    const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return t ? new URL(t.url).origin : "";
  } catch (e) { return ""; }
}

async function setSite(origin, scope) {
  if (!origin) return;
  const loc = (await chrome.storage.local.get("siteMode")).siteMode || {};
  const ses = (await chrome.storage.session.get("siteMode")).siteMode || {};
  delete loc[origin]; delete ses[origin];
  if (scope === "always") loc[origin] = "on";
  else if (scope === "off") loc[origin] = "off";
  else if (scope === "session") ses[origin] = "on";
  // 'default' => leave both cleared
  await chrome.storage.local.set({ siteMode: loc });
  await chrome.storage.session.set({ siteMode: ses });
}

async function refreshMode() {
  const g = (await chrome.storage.local.get("trustedInput")).trustedInput;
  $("hiFi").checked = g !== false;                    // default ON
  const origin = await activeOrigin();
  $("siteOrigin").textContent = origin ? origin.replace(/^https?:\/\//, "") : "—";
  const ses = (await chrome.storage.session.get("siteMode")).siteMode || {};
  const loc = (await chrome.storage.local.get("siteMode")).siteMode || {};
  let scope = "default";
  if (origin && ses[origin] === "on") scope = "session";
  else if (origin && loc[origin] === "on") scope = "always";
  else if (origin && loc[origin] === "off") scope = "off";
  [...document.querySelectorAll("#siteSeg button")].forEach((b) =>
    b.classList.toggle("on", b.dataset.scope === scope));
  [...document.querySelectorAll("#siteSeg button")].forEach((b) => { b.disabled = !origin; });
}

$("hiFi").addEventListener("change", async (e) => {
  await chrome.storage.local.set({ trustedInput: e.target.checked });
});
[...document.querySelectorAll("#siteSeg button")].forEach((b) =>
  b.addEventListener("click", async () => {
    const origin = await activeOrigin();
    await setSite(origin, b.dataset.scope);
    refreshMode();
  }));

$("recheck").addEventListener("click", refresh);
$("openCollie").addEventListener("click", () => {
  chrome.tabs.create({ url: "http://127.0.0.1:8787/" });
});
refresh();
refreshMode();
