// Popup status panel for the collie bridge extension.
// Answers, at a glance, the question that cost a long debugging session: "is this thing actually
// connected, and is the collie I'm running the one it's talking to?"
const BRIDGE = "http://127.0.0.1:8677";
let WEB = "http://127.0.0.1:8787";
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
  await refreshPresence();
  $("hint").textContent = "";
  setStatus("", "Checking…", "contacting the local bridge");
  try {
    const r = await fetch(BRIDGE + "/health", { cache: "no-store" });
    const d = await r.json();
    const ago = d.last_poll_secs_ago;
    $("rPoll").textContent = ago == null ? "never" : ago + "s ago";
    // A bridge that requires a token and is turning this extension away is HEALTHY and useless at
    // the same time — the one state that looks fine from every other angle, so it is checked first.
    const authFailed = (await chrome.storage.local.get("collieAuthFailed")).collieAuthFailed;
    if (d.auth_required && authFailed) {
      setStatus("bad", "Token rejected", "the bridge will not accept this extension");
      $("hint").textContent = "Run  collie browser-bridge --print-token  and paste it above.";
      return;
    }
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

async function refreshPresence() {
  try {
    const reply = await chrome.runtime.sendMessage({ type: "collie:get-status" });
    const state = reply && reply.state;
    if (!state || !state.attached) {
      $("rAgent").textContent = "Not attached";
      $("takeover").textContent = "Pause tab";
      $("takeover").disabled = true;
      return;
    }
    $("rAgent").textContent = (state.state || "idle") + " · " + (state.space || "default");
    const paused = state.state === "paused";
    $("takeover").textContent = paused ? "Resume tab" : "Pause tab";
    $("takeover").disabled = false;
  } catch (e) { $("rAgent").textContent = "Unavailable"; }
}

async function getWebToken() {
  const remembered = await chrome.storage.local.get("collieWebPort");
  if (Number(remembered.collieWebPort)) WEB = "http://127.0.0.1:" + Number(remembered.collieWebPort);
  const reply = await chrome.runtime.sendMessage({ type: "collie:get-bridge-token" });
  if (!reply || !reply.token) throw new Error("bridge token missing");
  const requestAuth = () => fetch(WEB + "/api/browser/bridge-auth", {
    headers: { Authorization: "Bearer " + reply.token }, cache: "no-store"
  });
  let response;
  let needsStart = false;
  try {
    response = await requestAuth();
    needsStart = !response.ok && response.status !== 403;
  } catch (e) { needsStart = true; }
  if (needsStart) {
    for (let port = 8787; port < 8799 && needsStart; port++) {
      if (WEB.endsWith(":" + port)) continue;
      const candidate = "http://127.0.0.1:" + port;
      try {
        const found = await fetch(candidate + "/api/browser/bridge-auth", {
          headers: { Authorization: "Bearer " + reply.token }, cache: "no-store"
        });
        if (found.ok) {
          WEB = candidate; response = found; needsStart = false;
          await chrome.storage.local.set({ collieWebPort: port });
        }
      } catch (e) {}
    }
  }
  if (needsStart) {
    const started = await fetch(BRIDGE + "/web/start", { method: "POST", headers: {
      Authorization: "Bearer " + reply.token, "X-Collie-Bridge": "1", "content-type": "application/json"
    }, body: "{}" });
    const detail = await started.json().catch(() => ({}));
    if (!started.ok || !detail.ok) throw new Error(detail.error || "could not start Collie web");
    WEB = "http://127.0.0.1:" + Number(detail.port || 8787);
    await chrome.storage.local.set({ collieWebPort: Number(detail.port || 8787) });
    response = await requestAuth();
  }
  if (!response.ok) throw new Error("Collie web unavailable");
  return await response.json();
}

async function refreshSiteAccess() {
  try {
    const auth = await getWebToken();
    $("siteAccess").value = auth.site_access || "all_except_sensitive";
    $("siteAccess").disabled = false;
    $("policyNote").textContent = "Clicks, typing, sends, payments and deletes keep separate approval.";
  } catch (e) {
    $("siteAccess").disabled = true;
    $("policyNote").textContent = "Start `collie web` to change the persistent policy.";
  }
}

// --- high-fidelity (chrome.debugger) input: global default + per-site override -------------------
const HAS_DEBUGGER_PERMISSION = (chrome.runtime.getManifest().permissions || []).includes("debugger");
const OPTIONAL_WEB_ORIGINS = ["http://*/*", "https://*/*"];

async function refreshReach() {
  let granted = false;
  try { granted = await chrome.permissions.contains({ origins: OPTIONAL_WEB_ORIGINS }); } catch (e) {}
  $("reachGrant").hidden = granted;
  $("reachNote").textContent = granted
    ? "Enabled. Collie-created tabs can continue across site navigations."
    : "Optional. The current tab works after you invoke Collie; grant this only for autonomous multi-site runs.";
}

$("reachGrant").addEventListener("click", async () => {
  let granted = false;
  try { granted = await chrome.permissions.request({ origins: OPTIONAL_WEB_ORIGINS }); } catch (e) {}
  $("reachNote").textContent = granted ? "Enabled for websites." : "Not enabled; current-tab mode remains available.";
  await refreshReach();
});

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
  if (!HAS_DEBUGGER_PERMISSION) {
    $("hiFi").checked = false; $("hiFi").disabled = true;
    $("hiFiTitle").textContent = "High-fidelity input · local Power build only";
    $("hiFiNote").textContent = "The Web Store build uses normal browser scripting and does not request debugger access.";
    document.querySelectorAll("#siteSeg button").forEach((b) => { b.disabled = true; });
    return;
  }
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
  if (!HAS_DEBUGGER_PERMISSION) return;
  await chrome.storage.local.set({ trustedInput: e.target.checked });
});
[...document.querySelectorAll("#siteSeg button")].forEach((b) =>
  b.addEventListener("click", async () => {
    const origin = await activeOrigin();
    await setSite(origin, b.dataset.scope);
    refreshMode();
  }));

// --- the token ------------------------------------------------------------------------------------
async function refreshToken() {
  const t = (await chrome.storage.local.get("collieToken")).collieToken;
  const failed = (await chrome.storage.local.get("collieAuthFailed")).collieAuthFailed;
  $("tokState").textContent = !t ? "not set" : (failed ? "rejected" : "set");
}

$("tokSave").addEventListener("click", async () => {
  const v = ($("tokIn").value || "").trim();
  // Clearing the field on purpose is how you revoke it here; saving a new one clears the failure
  // flag so the next poll decides afresh rather than staying red on old news.
  await chrome.storage.local.set({ collieToken: v, collieAuthFailed: false });
  $("tokIn").value = "";
  try { await chrome.action.setBadgeText({ text: "" }); } catch (e) {}
  await refreshToken();
  refresh();
});

$("recheck").addEventListener("click", refresh);
$("takeover").addEventListener("click", async () => {
  const paused = $("takeover").textContent.startsWith("Resume");
  await chrome.runtime.sendMessage({ type: paused ? "collie:resume-active" : "collie:pause-active" });
  await refreshPresence();
});
$("siteAccess").addEventListener("change", async () => {
  try {
    const auth = await getWebToken();
    const response = await fetch(WEB + "/api/settings?token=" + encodeURIComponent(auth.token), {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ BROWSER_SITE_ACCESS: $("siteAccess").value })
    });
    if (!response.ok) throw new Error("save failed");
    $("policyNote").textContent = "Saved. It controls navigation only; high-impact actions still ask.";
  } catch (e) {
    $("policyNote").textContent = "Could not save: " + e.message;
    refreshSiteAccess();
  }
});
$("openSide").addEventListener("click", async () => {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs[0]) await chrome.sidePanel.open({ windowId: tabs[0].windowId });
  window.close();
});
$("openCollie").addEventListener("click", () => {
  chrome.tabs.create({ url: WEB + "/" });
});
refresh();
refreshMode();
refreshToken();
refreshSiteAccess();
refreshReach();
