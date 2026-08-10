// Collie for VS Code — a sidebar panel that embeds collie's web GUI and manages the `collie web`
// server for you. The extension spawns one server (workspace folder as cwd, a free port), waits for
// it to come up, then loads its GUI into a WebviewView via vscode.env.asExternalUri — which makes the
// localhost server reachable from the webview even over WSL / Remote-SSH / Codespaces port forwarding.
//
// No build step: plain CommonJS, on brand with collie's stdlib-only ethos.
"use strict";
const vscode = require("vscode");
const cp = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const net = require("net");
const http = require("http");
const os = require("os");
const path = require("path");

let server = null; // { proc, port }
let starting = null;
let generation = 0;
let output = null;
let provider = null;

function log(msg) {
  if (output) output.appendLine("[collie] " + msg);
}

// Pick the configured port, or ask the OS for a free one (bind :0, read it back, release).
function pickPort(preferred) {
  return new Promise((resolve, reject) => {
    if (preferred !== undefined && preferred !== null && Number(preferred) !== 0) {
      const value = Number(preferred);
      if (!Number.isInteger(value) || value < 1 || value > 65535) {
        return reject(new Error("collie.port must be 0 or an integer from 1 to 65535"));
      }
      return resolve(value);
    }
    const srv = net.createServer();
    srv.once("error", (e) => reject(new Error("could not reserve a local port: " + e.message)));
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

function isBareCommand(cmd) {
  return typeof cmd === "string" && cmd.length > 0 &&
    cmd.indexOf("/") === -1 && cmd.indexOf("\\") === -1;
}

function executableFile(candidate) {
  try {
    const stat = fs.statSync(candidate);
    if (!stat.isFile()) return false;
    if (process.platform === "win32") {
      return /[.](?:exe|com)$/i.test(candidate); // scripts require a shell, which reintroduces injection
    }
    fs.accessSync(candidate, fs.constants.X_OK);
    return true;
  } catch (_) {
    return false;
  }
}

// Resolve a bare command against PATH before changing cwd to the workspace. Both CreateProcess and
// cmd.exe search the current directory before PATH; spawning `collie` from a cloned repository could
// otherwise execute that repository's collie.exe/collie.cmd. Empty PATH entries are skipped for the
// same reason. Windows pip installs a real collie.exe, so no shell wrapper is needed.
function resolveCommand(cmd, env) {
  if (!isBareCommand(cmd)) {
    if (!path.isAbsolute(cmd)) throw new Error("collie.command must be a bare PATH name or absolute path");
    if (!executableFile(cmd)) throw new Error("collie.command is not an executable file: " + cmd);
    return cmd;
  }
  const source = (env && (env.PATH || env.Path)) || "";
  const suffixes = process.platform === "win32" ? [".exe", ".com", ""] : [""];
  const seen = new Set();
  for (const rawDir of source.split(path.delimiter)) {
    const dir = rawDir.replace(/^"|"$/g, "").trim();
    if (!dir) continue;
    for (const suffix of suffixes) {
      const candidate = path.resolve(dir, cmd + suffix);
      const key = process.platform === "win32" ? candidate.toLowerCase() : candidate;
      if (seen.has(key)) continue;
      seen.add(key);
      if (executableFile(candidate)) return candidate;
    }
  }
  throw new Error("could not find executable '" + cmd + "' on PATH");
}

function validateExtraArgs(value) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || item.indexOf("\0") !== -1)) {
    throw new Error("collie.extraArgs must be an array of strings");
  }
  const reserved = /^(?:--port(?:=|$)|--open$|--no-open$|--lan$|--remote$)/;
  if (value.some((item) => reserved.test(item))) {
    throw new Error("collie.extraArgs cannot override the managed server address or exposure mode");
  }
  return value.slice();
}

// Poll GET / until the server answers or we time out — the iframe must not load before it's up
// (a premature load shows "connection refused" and never retries).
function waitForServer(port, timeoutMs, proc) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      if (proc && (proc.exitCode !== null || proc.killed)) {
        return reject(new Error("collie web exited before becoming ready"));
      }
      const req = http.get({ host: "127.0.0.1", port: port, path: "/", timeout: 1000 }, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => { if (body.length < 131072) body += chunk; });
        res.on("end", () => {
          // A free-port reservation is released before Collie binds, so another process can win the
          // race. Never frame whichever service happens to answer: require Collie's loopback index
          // marker while the child we launched is still alive.
          if (res.statusCode === 200 && body.includes('meta name="collie-token"') &&
              (!proc || (proc.exitCode === null && !proc.killed))) resolve();
          else retry();
        });
      });
      let retried = false;
      const retry = () => {
        if (retried) return;
        retried = true;
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
  if (isBareCommand(cmd)) return true;
  if (typeof cmd !== "string" || !path.isAbsolute(cmd)) return false;
  const info = cfg.inspect("command") || {};
  return info.workspaceValue === undefined && info.workspaceFolderValue === undefined;
}

async function startServerOnce(epoch) {
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
  if (epoch !== generation) throw new Error("Collie server start was cancelled");
  const extra = validateExtraArgs(cfg.get("extraArgs", []) || []);
  const folders = vscode.workspace.workspaceFolders;
  const cwd = folders && folders.length ? folders[0].uri.fsPath : os.homedir();
  const env = Object.assign({}, process.env);
  const prov = cfg.get("provider", "");
  if (prov) env.COLLIE_PROVIDER = prov;
  const embedToken = crypto.randomBytes(32).toString("hex");
  env.COLLIE_VSCODE_EMBED_TOKEN = embedToken;
  const args = ["web", "--port", String(port), "--no-open"].concat(extra);
  const executable = resolveCommand(cmd, env);
  log("spawn: " + executable + " web --port " + port + " --no-open" +
      (extra.length ? " (" + extra.length + " extra args)" : "") + "  (cwd=" + cwd + ")");
  const proc = cp.spawn(executable, args, { cwd: cwd, env: env, shell: false, windowsHide: true });
  proc.stdout.on("data", (d) => log(String(d).trimEnd()));
  proc.stderr.on("data", (d) => log("stderr: " + String(d).trimEnd()));
  proc.on("error", (e) => log("spawn error: " + (e && e.message)));
  proc.on("exit", (code) => {
    log("server exited (" + code + ")");
    if (server && server.proc === proc) server = null;
  });
  server = { proc: proc, port: port, embedToken: embedToken };
  try {
    await waitForServer(port, 25000, proc);
    if (epoch !== generation) throw new Error("Collie server start was cancelled");
  } catch (e) {
    try { proc.kill("SIGTERM"); } catch (_) { /* best effort */ }
    if (server && server.proc === proc) server = null;
    throw e;
  }
  log("server ready on 127.0.0.1:" + port);
  return server;
}

function startServer() {
  if (server && server.proc && server.proc.exitCode === null && !server.proc.killed) {
    return Promise.resolve(server);
  }
  if (starting) return starting;
  const epoch = generation;
  const pending = startServerOnce(epoch);
  starting = pending;
  pending.finally(() => { if (starting === pending) starting = null; }).catch(() => {});
  return pending;
}

function stopServer() {
  generation += 1;
  // A restart must not inherit the cancelled startup promise. Its finalizer is identity-guarded,
  // so clearing this now lets the replacement launch immediately without the old launch erasing it.
  starting = null;
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
      const framed = new URL(ext.toString(true));
      framed.searchParams.set("vscode_embed", s.embedToken);
      this.view.webview.html = this.frameHtml(framed.toString());
      return true;
    } catch (e) {
      log("render failed: " + (e && e.message || e));
      this.view.webview.html = this.errorHtml(String((e && e.message) || e));
      return false;
    }
  }

  frameHtml(url) {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error("VS Code returned an unsupported forwarded URL");
    }
    const origin = parsed.origin;
    // Allow only the one forwarded localhost origin. The old `https:` source let any HTTPS frame
    // load if future markup was compromised, which was much wider than this panel needs.
    const csp =
      "default-src 'none'; style-src 'unsafe-inline'; img-src data:; " +
      "frame-src " + origin + "; object-src 'none'; base-uri 'none';";
    return (
      "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
      "<meta http-equiv=\"Content-Security-Policy\" content=\"" + csp + "\">" +
      "<style>html,body{margin:0;padding:0;height:100%;background:#0b0d10}" +
      "iframe{border:0;display:block;width:100%;height:100vh}</style></head><body>" +
      "<iframe src=\"" + escapeHtml(url) + "\" allow=\"clipboard-read; clipboard-write; camera; microphone\"></iframe>" +
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
      "<button id=\"retry\">Retry</button> " +
      "<button id=\"external\">Open in browser</button>",
      "const vscode=acquireVsCodeApi();" +
      "document.getElementById('retry').addEventListener('click',()=>vscode.postMessage({type:'retry'}));" +
      "document.getElementById('external').addEventListener('click',()=>vscode.postMessage({type:'openExternal'}));"
    );
  }

  _shell(inner, script) {
    const nonce = crypto.randomBytes(18).toString("base64url");
    return (
      "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
      "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; " +
      "style-src 'unsafe-inline'; script-src 'nonce-" + nonce + "';\"><style>" +
      "body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);" +
      "background:var(--vscode-editor-background);padding:22px;text-align:center}" +
      ".dim{opacity:.65;font-size:12px}.err{color:var(--vscode-errorForeground);font-weight:600}" +
      "pre{white-space:pre-wrap;text-align:left;background:var(--vscode-textBlockQuote-background);" +
      "padding:8px;border-radius:6px;font-size:12px}" +
      "button{margin-top:8px;padding:5px 10px;border:0;border-radius:5px;cursor:pointer;" +
      "background:var(--vscode-button-background);color:var(--vscode-button-foreground)}" +
      ".spin{width:22px;height:22px;margin:18px auto;border:2px solid var(--vscode-foreground);" +
      "border-top-color:transparent;border-radius:50%;animation:s 0.8s linear infinite}" +
      "@keyframes s{to{transform:rotate(360deg)}}</style></head><body>" + inner +
      (script ? "<script nonce=\"" + nonce + "\">" + script + "</script>" : "") +
      "</body></html>"
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
      const restarted = await provider.render();
      if (restarted) vscode.window.showInformationMessage("Collie server restarted.");
      else vscode.window.showErrorMessage("Collie server did not restart. Open the Collie log for details.");
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

module.exports = {
  activate,
  deactivate,
  _test: { CollieViewProvider, escapeHtml, isCommandAllowed, pickPort, resolveCommand,
           validateExtraArgs, waitForServer }
};
