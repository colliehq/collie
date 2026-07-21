// Collie for VS Code — a sidebar panel that embeds collie's web GUI and manages the `collie web`
// server for you. The extension spawns one server (workspace folder as cwd, a free port), waits for
// it to come up, then loads its GUI into a WebviewView via vscode.env.asExternalUri — which makes the
// localhost server reachable from the webview even over WSL / Remote-SSH / Codespaces port forwarding.
//
// No build step: plain CommonJS, on brand with collie's stdlib-only ethos.
"use strict";
const vscode = require("vscode");
const cp = require("child_process");
const net = require("net");
const http = require("http");

let server = null; // { proc, port }
let output = null;
let provider = null;

function log(msg) {
  if (output) output.appendLine("[collie] " + msg);
}

// Pick the configured port, or ask the OS for a free one (bind :0, read it back, release).
function pickPort(preferred) {
  return new Promise((resolve) => {
    if (preferred && preferred > 0) return resolve(preferred);
    const srv = net.createServer();
    srv.once("error", () => resolve(8790));
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

// Poll GET / until the server answers or we time out — the iframe must not load before it's up
// (a premature load shows "connection refused" and never retries).
function waitForServer(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const req = http.get({ host: "127.0.0.1", port: port, path: "/", timeout: 1000 }, (res) => {
        res.resume();
        resolve();
      });
      const retry = () => {
        if (Date.now() > deadline) reject(new Error("collie web did not come up on port " + port));
        else setTimeout(tryOnce, 300);
      };
      req.on("error", retry);
      req.on("timeout", () => { req.destroy(); retry(); });
    };
    tryOnce();
  });
}

// Security guard: the collie.command value must be a bare PATH name (e.g. "collie") unless it was
// set in the user's/machine's own settings. A path-containing command (relative or absolute) coming
// from a workspace-level settings file could point at an attacker-controlled binary inside the repo,
// which we would then spawn — an RCE. collie.command is machine-scoped in package.json, so workspace
// values are already ignored by VS Code; this is defense-in-depth in case that scope is bypassed.
function isCommandAllowed(cfg, cmd) {
  if (cmd && cmd.indexOf("/") === -1 && cmd.indexOf("\\") === -1) return true; // bare name: always OK
  const info = cfg.inspect("command") || {};
  return info.workspaceValue === undefined && info.workspaceFolderValue === undefined;
}

async function startServer() {
  if (server && server.proc && server.proc.exitCode === null && !server.proc.killed) return server;
  // Workspace Trust guard: never spawn a child process on behalf of an untrusted workspace. This
  // extension auto-starts at activation, so an untrusted repo must not be able to trigger a spawn.
  if (vscode.workspace.isTrusted === false) {
    vscode.window.showWarningMessage("Collie: this workspace is not trusted. Trust the workspace to start the Collie server.");
    throw new Error("workspace is not trusted");
  }
  const cfg = vscode.workspace.getConfiguration("collie");
  const cmd = cfg.get("command", "collie");
  if (!isCommandAllowed(cfg, cmd)) {
    vscode.window.showErrorMessage("Collie: refusing to run '" + cmd + "'. Set collie.command to a bare PATH name, or configure it in your user/machine settings.");
    throw new Error("collie.command is not allowed from this source");
  }
  const port = await pickPort(cfg.get("port", 0));
  const extra = cfg.get("extraArgs", []) || [];
  const folders = vscode.workspace.workspaceFolders;
  const cwd = folders && folders.length ? folders[0].uri.fsPath : (process.env.HOME || undefined);
  const env = Object.assign({}, process.env);
  const prov = cfg.get("provider", "");
  if (prov) env.COLLIE_PROVIDER = prov;
  const args = ["web", "--port", String(port), "--no-open"].concat(extra);
  log("spawn: " + cmd + " " + args.join(" ") + "  (cwd=" + cwd + ")");
  const proc = cp.spawn(cmd, args, { cwd: cwd, env: env, shell: process.platform === "win32" });
  proc.stdout.on("data", (d) => log(String(d).trimEnd()));
  proc.stderr.on("data", (d) => log("stderr: " + String(d).trimEnd()));
  proc.on("error", (e) => log("spawn error: " + (e && e.message)));
  proc.on("exit", (code) => {
    log("server exited (" + code + ")");
    if (server && server.proc === proc) server = null;
  });
  server = { proc: proc, port: port };
  await waitForServer(port, 25000);
  log("server ready on 127.0.0.1:" + port);
  return server;
}

function stopServer() {
  if (server && server.proc && !server.proc.killed) {
    try { server.proc.kill("SIGTERM"); } catch (e) { /* ignore */ }
  }
  server = null;
}

class CollieViewProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
    this._extUri = null;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage((m) => {
      if (!m) return;
      if (m.type === "retry") this.render();
      else if (m.type === "openExternal" && this._extUri) vscode.env.openExternal(this._extUri);
    });
    this.render();
  }

  async render() {
    if (!this.view) return;
    this.view.webview.html = this.loadingHtml("Starting Collie…");
    try {
      const s = await startServer();
      // asExternalUri: turns http://127.0.0.1:PORT into a URI the webview can actually reach through
      // whatever forwarding is in play (WSL localhost, Remote-SSH, Codespaces tunnel).
      const ext = await vscode.env.asExternalUri(vscode.Uri.parse("http://127.0.0.1:" + s.port));
      this._extUri = ext;
      this.view.webview.html = this.frameHtml(ext.toString(true));
    } catch (e) {
      log("render failed: " + (e && e.message || e));
      this.view.webview.html = this.errorHtml(String((e && e.message) || e));
    }
  }

  frameHtml(url) {
    const origin = url.replace(/\/+$/, "");
    // Allow the iframe (frame-src) to load the forwarded localhost origin. Everything else is denied.
    const csp =
      "default-src 'none'; style-src 'unsafe-inline'; img-src data:; " +
      "frame-src " + origin + " http://127.0.0.1:* http://localhost:* https:;";
    return (
      "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
      "<meta http-equiv=\"Content-Security-Policy\" content=\"" + csp + "\">" +
      "<style>html,body{margin:0;padding:0;height:100%;background:#0b0d10}" +
      "iframe{border:0;display:block;width:100%;height:100vh}</style></head><body>" +
      "<iframe src=\"" + url + "\" allow=\"clipboard-read; clipboard-write; camera; microphone\"></iframe>" +
      "</body></html>"
    );
  }

  loadingHtml(msg) {
    return this._shell(
      "<div class=\"spin\"></div><p>" + escapeHtml(msg) + "</p>" +
      "<p class=\"dim\">launching the collie web server on your workspace…</p>"
    );
  }

  errorHtml(msg) {
    return this._shell(
      "<p class=\"err\">Couldn't start Collie.</p>" +
      "<pre>" + escapeHtml(msg) + "</pre>" +
      "<p class=\"dim\">Check <b>collie.command</b> in Settings points at the collie CLI, then:</p>" +
      "<button onclick=\"vscode.postMessage({type:'retry'})\">Retry</button> " +
      "<button onclick=\"vscode.postMessage({type:'openExternal'})\">Open in browser</button>"
    );
  }

  _shell(inner) {
    return (
      "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>" +
      "body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);" +
      "background:var(--vscode-editor-background);padding:22px;text-align:center}" +
      ".dim{opacity:.65;font-size:12px}.err{color:var(--vscode-errorForeground);font-weight:600}" +
      "pre{white-space:pre-wrap;text-align:left;background:var(--vscode-textBlockQuote-background);" +
      "padding:8px;border-radius:6px;font-size:12px}" +
      "button{margin-top:8px;padding:5px 10px;border:0;border-radius:5px;cursor:pointer;" +
      "background:var(--vscode-button-background);color:var(--vscode-button-foreground)}" +
      ".spin{width:22px;height:22px;margin:18px auto;border:2px solid var(--vscode-foreground);" +
      "border-top-color:transparent;border-radius:50%;animation:s 0.8s linear infinite}" +
      "@keyframes s{to{transform:rotate(360deg)}}</style></head><body>" +
      "<script>const vscode=acquireVsCodeApi();</script>" + inner + "</body></html>"
    );
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[c]));
}

function activate(context) {
  output = vscode.window.createOutputChannel("Collie");
  provider = new CollieViewProvider(context);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("collie.panel", provider, {
      webviewOptions: { retainContextWhenHidden: true }, // keep the collie session alive when hidden
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("collie.reload", () => provider.render()),
    vscode.commands.registerCommand("collie.restart", async () => {
      stopServer();
      await provider.render();
      vscode.window.showInformationMessage("Collie server restarted.");
    }),
    vscode.commands.registerCommand("collie.openInBrowser", async () => {
      try {
        const s = await startServer();
        const ext = await vscode.env.asExternalUri(vscode.Uri.parse("http://127.0.0.1:" + s.port));
        vscode.env.openExternal(ext);
      } catch (e) {
        vscode.window.showErrorMessage("Collie: " + ((e && e.message) || e));
      }
    }),
    vscode.commands.registerCommand("collie.showLog", () => { if (output) output.show(); })
  );
  // warm the server at startup so the panel paints instantly when first opened
  startServer().catch((e) => log("startup warm-up: " + ((e && e.message) || e)));
}

function deactivate() {
  stopServer();
}

module.exports = { activate, deactivate };
