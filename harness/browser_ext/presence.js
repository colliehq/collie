// Visible, page-isolated agent presence and the physical user-takeover sensor.
// This runs in Chrome's isolated extension world: the page cannot call these
// handlers or forge a trusted Resume. Resume exists only in extension UI.
(() => {
  if (document.getElementById("__colliePresenceHost")) return;
  let current = null;
  let suppressUntil = 0;
  let lastTakeover = 0;
  let host = null;
  let statusEl = null;
  let detailEl = null;

  const labels = {
    acting: "Collie · Acting",
    observing: "Collie · Observing",
    waiting: "Collie · Waiting for you",
    paused: "Collie · Paused",
    idle: "Collie · Ready",
    disconnected: "Collie · Disconnected",
  };

  function mount() {
    if (host && host.isConnected) return true;
    const parent = document.documentElement || document.body;
    if (!parent) return false;
    host = document.createElement("div");
    host.id = "__colliePresenceHost";
    host.style.cssText = "all:initial;position:fixed;right:14px;top:14px;z-index:2147483647;pointer-events:none";
    const root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = `
      *{box-sizing:border-box}.pill{pointer-events:auto;display:none;align-items:center;gap:8px;
      max-width:360px;padding:7px 8px 7px 10px;border:1px solid rgba(255,255,255,.2);
      border-radius:999px;background:rgba(25,29,38,.94);color:#f7f8fb;
      box-shadow:0 8px 28px rgba(0,0,0,.28);font:12px/1.25 system-ui,-apple-system,"Segoe UI",sans-serif;
      backdrop-filter:blur(12px)}.pill.show{display:flex}.dot{width:8px;height:8px;border-radius:50%;
      background:#5fc997;box-shadow:0 0 0 3px rgba(95,201,151,.16);flex:none}.pill.observing .dot{background:#8fa0e0}
      .pill.waiting .dot{background:#e7b657}.pill.paused .dot,.pill.disconnected .dot{background:#e58074}
      .copy{min-width:0}.status{font-weight:650;white-space:nowrap}.detail{display:block;max-width:200px;color:#b8becd;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10.5px;margin-top:1px}
      button{all:unset;cursor:pointer;border-left:1px solid rgba(255,255,255,.16);padding:3px 5px 3px 10px;
      color:#ffaaa1;font-weight:650}button:hover{color:#fff}.paused button{display:none}`;
    const pill = document.createElement("div");
    pill.className = "pill";
    pill.innerHTML = '<span class="dot"></span><span class="copy"><span class="status"></span>' +
      '<span class="detail"></span></span><button type="button" title="Stop Collie and take over">Stop</button>';
    root.append(style, pill);
    statusEl = pill.querySelector(".status");
    detailEl = pill.querySelector(".detail");
    pill.querySelector("button").addEventListener("click", (event) => {
      if (!event.isTrusted) return;
      chrome.runtime.sendMessage({ type: "collie:pause", reason: "Stop pressed on page" });
    });
    parent.appendChild(host);
    return true;
  }

  function render(state) {
    current = state && state.attached ? state : null;
    if (!mount()) return setTimeout(() => render(state), 20);
    const pill = statusEl.closest(".pill");
    if (!current) {
      pill.className = "pill";
      return;
    }
    const kind = current.state || "idle";
    pill.className = "pill show " + kind;
    statusEl.textContent = labels[kind] || labels.idle;
    detailEl.textContent = current.reason || current.action || current.space || "";
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || typeof message !== "object") return;
    if (message.type === "collie:presence") render(message.state);
    if (message.type === "collie:agent-input") suppressUntil = Math.max(
      suppressUntil, Number(message.until) || (Date.now() + 1200));
  });

  function physicalTakeover(event) {
    if (!event.isTrusted || !current || current.state === "paused" || Date.now() < suppressUntil) return;
    if (Date.now() - lastTakeover < 700) return;
    lastTakeover = Date.now();
    chrome.runtime.sendMessage({ type: "collie:user-takeover", reason: "You used the page" });
  }
  addEventListener("pointerdown", physicalTakeover, true);
  addEventListener("keydown", physicalTakeover, true);

  chrome.runtime.sendMessage({ type: "collie:presence-ready" }, (response) => {
    void chrome.runtime.lastError;
    if (response && response.state) render(response.state);
  });
})();
