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

let server = null; // { proc, port, embedToken }
let starting = null;
let generation = 0;
let output = null;
let provider = null; // primary sidebar provider for this VS Code version
let fallbackProvider = null;
let secondaryProvider = null;
let mapPanel = null;
let contextTimer = null;
const agentPanels = new Set();
const originalDocuments = new Map();
const recentOpenTabs = [];

// VS Code exposed contributed secondary-sidebar containers to third-party extensions in 1.106.
// Keep the same compatibility split as the installed Codex extension: new VS Code gets the right
// rail; older supported builds retain the Activity Bar entrance.
function versionAtLeast(version, major, minor) {
  const match = /^(\d+)[.](\d+)/.exec(String(version || ""));
  if (!match) return false;
  const foundMajor = Number(match[1]);
  const foundMinor = Number(match[2]);
  return foundMajor > major || (foundMajor === major && foundMinor >= minor);
}

function supportsSecondarySidebar() {
  return versionAtLeast(vscode.version, 1, 106);
}

function primaryViewIds() {
  return supportsSecondarySidebar()
    ? { container: "collieSecondary", view: "collie.panel.secondary" }
    : { container: "collie", view: "collie.panel" };
}

async function focusPrimaryView() {
  const ids = primaryViewIds();
  await vscode.commands.executeCommand("workbench.view.extension." + ids.container);
  await vscode.commands.executeCommand(ids.view + ".focus");
}

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

function resolveLaunchCommand(cmd, env) {
  // Prefer the runtime installed with Collie over an unrelated `collie` found earlier on PATH.
  // The two can report the same package version while containing different source revisions; that
  // exact split left the new IDE shell framing an old Map without the IDE message bridge.
  if (cmd === "collie" && process.platform === "win32" && env && env.LOCALAPPDATA) {
    const python = path.join(env.LOCALAPPDATA, "Programs", "Collie", "python", "python.exe");
    if (executableFile(python)) {
      return { executable: python, prefixArgs: ["-I", "-m", "harness.cli"], bundled: true };
    }
  }
  return { executable: resolveCommand(cmd, env), prefixArgs: [], bundled: false };
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
  // Workspace Trust guard: never spawn a child process on behalf of an untrusted workspace. The
  // process is lazy, but an untrusted repo still must not be able to trigger it by opening a view.
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
  env.COLLIE_VSCODE_WORKSPACES = JSON.stringify((folders || []).map((folder) => folder.uri.fsPath));
  const launch = resolveLaunchCommand(cmd, env);
  const args = launch.prefixArgs.concat(["web", "--port", String(port), "--no-open"], extra);
  const executable = launch.executable;
  log("spawn: " + executable + (launch.bundled ? " -I -m harness.cli" : "") +
      " web --port " + port + " --no-open" +
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

function childUrl(base, childPath, embedToken, extra) {
  const raw = typeof base === "string" ? base : base.toString(true);
  const url = new URL(raw);
  url.pathname = (url.pathname.endsWith("/") ? url.pathname : url.pathname + "/") + childPath.replace(/^\/+/, "");
  url.searchParams.set("vscode_embed", embedToken);
  for (const [key, value] of Object.entries(extra || {})) url.searchParams.set(key, String(value));
  return url;
}

function isPathInside(root, candidate) {
  const rel = path.relative(path.resolve(root), path.resolve(candidate));
  return rel === "" || (!path.isAbsolute(rel) && rel !== ".." && !rel.startsWith(".." + path.sep));
}

function workspaceFile(rawPath) {
  if (typeof rawPath !== "string" || !rawPath || rawPath.length > 4096 || rawPath.includes("\0")) return null;
  const folders = vscode.workspace.workspaceFolders || [];
  for (const folder of folders) {
    const root = path.resolve(folder.uri.fsPath);
    const candidate = path.isAbsolute(rawPath) ? path.resolve(rawPath) : path.resolve(root, rawPath);
    if (!isPathInside(root, candidate)) continue;
    try {
      const realRoot = fs.realpathSync(root);
      const realCandidate = fs.realpathSync(candidate);
      if (isPathInside(realRoot, realCandidate) && fs.statSync(realCandidate).isFile()) return realCandidate;
    } catch (_) { /* a stale/deleted map node is not openable */ }
  }
  return null;
}

async function openWorkspaceFile(message) {
  const file = workspaceFile(message && message.path);
  if (!file) {
    vscode.window.showWarningMessage("Collie refused to open a file outside the current workspace.");
    return false;
  }
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  const line = Math.max(0, Math.min(document.lineCount - 1, Number.isInteger(message.line) ? message.line - 1 : 0));
  const position = new vscode.Position(line, 0);
  await vscode.window.showTextDocument(document, {
    viewColumn: vscode.ViewColumn.Beside,
    preview: true,
    selection: new vscode.Range(position, position),
  });
  return true;
}

function mapWorkspaceRoot() {
  const editor = vscode.window.activeTextEditor;
  if (editor && editor.document && typeof vscode.workspace.getWorkspaceFolder === "function") {
    const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
    if (folder && folder.uri && folder.uri.fsPath) return folder.uri.fsPath;
  }
  const folders = vscode.workspace.workspaceFolders || [];
  return folders.length && folders[0].uri ? folders[0].uri.fsPath : "";
}

function workspaceUriItem(uri, range, text, kind) {
  if (!uri || uri.scheme !== "file") return null;
  const folder = typeof vscode.workspace.getWorkspaceFolder === "function"
    ? vscode.workspace.getWorkspaceFolder(uri) : null;
  if (!folder) return null;
  const item = {
    kind: kind || "file",
    label: path.basename(uri.fsPath),
    path: vscode.workspace.asRelativePath(uri, false),
    fsPath: uri.fsPath,
  };
  if (range) {
    item.startLine = range.start.line + 1;
    item.endLine = range.end.line + 1;
    if (range.end.character === 0 && range.end.line > range.start.line) item.endLine -= 1;
  }
  if (typeof text === "string" && text) item.content = text;
  return item;
}

function selectionContext(editor) {
  if (!editor || !editor.document || !editor.document.uri || editor.document.uri.scheme !== "file") return null;
  const cfg = vscode.workspace.getConfiguration("collie");
  const maxChars = Math.max(1000, Math.min(100000, Number(cfg.get("maxSelectionChars", 12000)) || 12000));
  const selection = editor.selection;
  const selected = selection && !selection.isEmpty ? editor.document.getText(selection).slice(0, maxChars) : "";
  return workspaceUriItem(editor.document.uri, selection && !selection.isEmpty ? selection : null,
                          selected, selected ? "selection" : "file");
}

function rememberEditor(editor) {
  if (!editor || !editor.document || editor.document.uri.scheme !== "file") return;
  const item = workspaceUriItem(editor.document.uri, null, "", "file");
  if (!item) return;
  const key = process.platform === "win32" ? item.fsPath.toLowerCase() : item.fsPath;
  const next = [item];
  for (const prior of recentOpenTabs) {
    const priorKey = process.platform === "win32" ? prior.fsPath.toLowerCase() : prior.fsPath;
    if (priorKey !== key) next.push(prior);
    if (next.length >= 8) break;
  }
  recentOpenTabs.splice(0, recentOpenTabs.length, ...next);
}

function automaticIdeMessage() {
  const editor = vscode.window.activeTextEditor;
  if (editor) rememberEditor(editor);
  return {
    type: "collie:ide-context",
    automatic: true,
    active: selectionContext(editor),
    openTabs: recentOpenTabs.map((item) => ({
      kind: "file", label: item.label, path: item.path, fsPath: item.fsPath,
    })),
  };
}

function allWorkbenchTargets() {
  return [fallbackProvider, secondaryProvider, ...Array.from(agentPanels)].filter(Boolean);
}

function activeWorkbenchTarget() {
  for (const target of agentPanels) {
    if (target.panel && target.panel.active) return target;
  }
  if (provider && provider.view && provider.view.visible) return provider;
  return provider;
}

function publishAutomaticIdeContext() {
  const message = automaticIdeMessage();
  for (const target of allWorkbenchTargets()) target.postHostMessage(message, false);
}

function scheduleAutomaticIdeContext() {
  if (contextTimer) clearTimeout(contextTimer);
  contextTimer = setTimeout(() => {
    contextTimer = null;
    publishAutomaticIdeContext();
  }, 120);
}

async function addContextsToCurrentThread(items, options) {
  const clean = (items || []).filter(Boolean);
  if (!clean.length) {
    vscode.window.setStatusBarMessage("Collie: no workspace context to add", 2500);
    return false;
  }
  const target = activeWorkbenchTarget();
  if (!target) return false;
  target.postHostMessage({
    type: "collie:add-context",
    items: clean,
    draft: options && typeof options.draft === "string" ? options.draft : "",
  }, true);
  const reveal = vscode.workspace.getConfiguration("collie").get("revealOnContextAdd", false) === true;
  if (reveal && target === provider) await focusPrimaryView();
  vscode.window.setStatusBarMessage(
    "Collie: added " + clean.length + " context item" + (clean.length === 1 ? "" : "s") +
    (reveal ? "" : " without changing focus"), 3000);
  return true;
}

function diagnosticsContext(uri) {
  if (!uri || uri.scheme !== "file") return null;
  const diagnostics = vscode.languages.getDiagnostics(uri).slice(0, 40);
  if (!diagnostics.length) return null;
  const lines = diagnostics.map((diagnostic) => {
    const start = diagnostic.range && diagnostic.range.start;
    const line = start ? start.line + 1 : 1;
    const severity = ["error", "warning", "info", "hint"][diagnostic.severity] || "problem";
    return severity + " L" + line + ": " + diagnostic.message;
  });
  return workspaceUriItem(uri, null, lines.join("\n"), "diagnostics");
}

function execFile(command, args, options) {
  return new Promise((resolve, reject) => {
    cp.execFile(command, args, Object.assign({ windowsHide: true, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 }, options || {}),
      (error, stdout, stderr) => error ? reject(Object.assign(error, { stdout, stderr })) : resolve(stdout));
  });
}

async function gitRootForFile(file) {
  const folder = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(file));
  if (!folder) return null;
  const git = resolveCommand("git", process.env);
  const root = (await execFile(git, ["-C", folder.uri.fsPath, "rev-parse", "--show-toplevel"])).trim();
  return root && isPathInside(folder.uri.fsPath, root) ? root : null;
}

async function changedWorkspaceFiles(root) {
  const git = resolveCommand("git", process.env);
  const tracked = await execFile(git, ["-C", root, "diff", "--name-only", "-z", "HEAD", "--"])
    .catch(() => "");
  const untracked = await execFile(git, ["-C", root, "ls-files", "--others", "--exclude-standard", "-z", "--"])
    .catch(() => "");
  return Array.from(new Set((tracked + untracked).split("\0").filter(Boolean))).sort();
}

async function reviewWorkspaceFile(rawFile) {
  const file = workspaceFile(rawFile);
  if (!file) throw new Error("Collie refused to review a file outside the open workspace");
  const root = await gitRootForFile(file);
  if (!root) throw new Error("the selected file is not in a Git workspace");
  const rel = path.relative(root, file).split(path.sep).join("/");
  const git = resolveCommand("git", process.env);
  const before = await execFile(git, ["-C", root, "show", "HEAD:" + rel]).catch(() => "");
  const key = crypto.randomBytes(10).toString("hex");
  const original = vscode.Uri.from({ scheme: "collie-original", path: "/" + key + "/" + path.basename(file) });
  originalDocuments.set(original.toString(), before);
  while (originalDocuments.size > 80) originalDocuments.delete(originalDocuments.keys().next().value);
  await vscode.commands.executeCommand("vscode.diff", original, vscode.Uri.file(file),
    path.basename(file) + " (HEAD ↔ working tree)", { preview: true, preserveFocus: false });
  return true;
}

async function reviewChanges(uri) {
  let candidate = uri && uri.scheme === "file" ? uri.fsPath :
    (vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri.scheme === "file"
      ? vscode.window.activeTextEditor.document.uri.fsPath : "");
  if (!candidate) throw new Error("open a workspace file first");
  const root = await gitRootForFile(candidate);
  if (!root) throw new Error("the current file is not in a Git workspace");
  const changed = await changedWorkspaceFiles(root);
  if (!changed.length) {
    vscode.window.showInformationMessage("Collie: this workspace has no uncommitted file changes.");
    return false;
  }
  let rel = path.relative(root, candidate).split(path.sep).join("/");
  if (!changed.includes(rel)) {
    const picked = await vscode.window.showQuickPick(changed.map((item) => ({ label: path.basename(item), description: item, value: item })),
      { title: "Review a changed file", placeHolder: "Choose a file for VS Code's native diff" });
    if (!picked) return false;
    rel = picked.value;
  }
  return reviewWorkspaceFile(path.join(root, ...rel.split("/")));
}

class TodoCodeLensProvider {
  provideCodeLenses(document) {
    if (vscode.workspace.getConfiguration("collie").get("commentCodeLensEnabled", true) !== true) return [];
    const lenses = [];
    const limit = Math.min(document.lineCount, 20000);
    for (let line = 0; line < limit; line += 1) {
      const text = document.lineAt(line).text;
      if (!/\b(?:TODO|FIXME)\b/i.test(text)) continue;
      const range = new vscode.Range(line, 0, line, Math.max(0, text.length));
      lenses.push(new vscode.CodeLens(range, {
        title: "Implement with Collie",
        command: "collie.implementTodo",
        arguments: [document.uri, line, text.trim()],
      }));
    }
    return lenses;
  }
}

function iframeShell(url, mode) {
  const parsed = new URL(url);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("VS Code returned an unsupported forwarded URL");
  }
  const origin = parsed.origin;
  const nonce = crypto.randomBytes(18).toString("base64url");
  const mapMode = mode === "map";
  const csp =
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; " +
    "frame-src " + origin + "; script-src 'nonce-" + nonce + "'; object-src 'none'; base-uri 'none';";
  const script =
    "const vscode=acquireVsCodeApi(),frame=document.getElementById('collieFrame'),origin=" + JSON.stringify(origin) + ";" +
    "const childTypes=new Set(" + JSON.stringify(mapMode
      ? ["collie:openFile"]
      : ["collie:openMap", "collie:openFile", "collie:reviewFile", "collie:status"]) + ");" +
    "const hostTypes=new Set(['collie:ide-context','collie:add-context','collie:new-thread','collie:preferences','collie:focus-composer']);" +
    "addEventListener('message',(event)=>{" +
      "const m=event.data;if(!m||typeof m!=='object')return;" +
      "if(event.source===frame.contentWindow){if(event.origin!==origin||!childTypes.has(m.type))return;" +
        "if((m.type==='collie:openFile'||m.type==='collie:reviewFile')&&(typeof m.path!=='string'||m.path.length>4096))return;" +
        "vscode.postMessage(m);return;}" +
      "if(m.type!=='collie:host'||!m.payload||!hostTypes.has(m.payload.type))return;" +
      "try{if(JSON.stringify(m.payload).length>250000)return;}catch(_){return;}" +
      "frame.contentWindow.postMessage(m.payload,origin);" +
    "});" +
    "frame.addEventListener('load',()=>vscode.postMessage({type:'frameReady'}));";
  return (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
    "<meta http-equiv=\"Content-Security-Policy\" content=\"" + csp + "\">" +
    "<style>html,body{margin:0;padding:0;height:100%;overflow:hidden;background:var(--vscode-editor-background,#0b0d10);" +
    "color:var(--vscode-foreground,#ddd);font-family:var(--vscode-font-family,system-ui)}" +
    "iframe{border:0;display:block;width:100%;height:100vh}</style></head><body>" +
    "<iframe id=\"collieFrame\" src=\"" + escapeHtml(url) + "\" referrerpolicy=\"no-referrer\"" +
    (mapMode ? "" : " allow=\"clipboard-write\"") + "></iframe>" +
    "<script nonce=\"" + nonce + "\">" + script + "</script></body></html>"
  );
}

async function openMap(context) {
  if (mapPanel) {
    mapPanel.reveal(vscode.ViewColumn.Active);
    return mapPanel;
  }
  const s = await startServer();
  const ext = await vscode.env.asExternalUri(vscode.Uri.parse("http://127.0.0.1:" + s.port));
  const selectedRoot = mapWorkspaceRoot();
  const mapUrl = childUrl(ext, "map", s.embedToken,
                          selectedRoot ? { ide: "1", repo: selectedRoot } : { ide: "1" });
  const panel = vscode.window.createWebviewPanel(
    "collie.map", "Collie Project Map", vscode.ViewColumn.Active,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  mapPanel = panel;
  panel.iconPath = vscode.Uri.joinPath(context.extensionUri, "media", "galaxy.svg");
  panel.webview.html = iframeShell(mapUrl.toString(), "map");
  panel.webview.onDidReceiveMessage(async (message) => {
    if (!message || message.type !== "collie:openFile") return;
    try { await openWorkspaceFile(message); }
    catch (e) { vscode.window.showErrorMessage("Collie Map: " + ((e && e.message) || e)); }
  });
  panel.onDidDispose(() => { if (mapPanel === panel) mapPanel = null; });
  return panel;
}

function workbenchPreferences() {
  const cfg = vscode.workspace.getConfiguration("collie");
  const followUp = cfg.get("followUpQueueMode", "queue");
  return { followUpQueueMode: followUp === "steer" ? "steer" : "queue", autoFocus: false };
}

class CollieViewProvider {
  constructor(context, surface) {
    this.context = context;
    this.surface = surface || "sidebar";
    this.view = null;
    this.panel = null;
    this.ready = false;
    this.pendingMessages = [];
    this._extUri = null;
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true, localResourceRoots: [] };
    view.webview.onDidReceiveMessage((message) => this.handleMessage(message));
    this.render();
  }

  attachPanel(panel) {
    this.view = panel;
    this.panel = panel;
    panel.webview.onDidReceiveMessage((message) => this.handleMessage(message));
    return this.render();
  }

  async handleMessage(message) {
    if (!message || typeof message !== "object") return;
    try {
      if (message.type === "frameReady") {
        this.ready = true;
        this.flushHostMessages();
      } else if (message.type === "retry") {
        this.render();
      } else if (message.type === "openExternal" && this._extUri) {
        vscode.env.openExternal(this._extUri);
      } else if (message.type === "collie:openMap") {
        await openMap(this.context);
      } else if (message.type === "collie:openFile") {
        await openWorkspaceFile(message);
      } else if (message.type === "collie:reviewFile") {
        await reviewWorkspaceFile(message.path);
      } else if (message.type === "collie:status" && this.view && "badge" in this.view) {
        const count = Number(message.count);
        this.view.badge = Number.isInteger(count) && count > 0
          ? { value: Math.min(99, count), tooltip: String(message.tooltip || "Collie needs you") }
          : undefined;
      }
    } catch (error) {
      vscode.window.showErrorMessage("Collie: " + ((error && error.message) || error));
    }
  }

  postHostMessage(payload, queue) {
    if (!payload || typeof payload !== "object") return false;
    if (this.ready && this.view && this.view.webview) {
      this.view.webview.postMessage({ type: "collie:host", payload: payload });
      return true;
    }
    if (queue !== false) {
      this.pendingMessages.push(payload);
      if (this.pendingMessages.length > 60) this.pendingMessages.splice(0, this.pendingMessages.length - 60);
    }
    return false;
  }

  flushHostMessages() {
    if (!this.ready || !this.view || !this.view.webview) return;
    this.view.webview.postMessage({ type: "collie:host", payload: {
      type: "collie:preferences", preferences: workbenchPreferences(),
    }});
    this.view.webview.postMessage({ type: "collie:host", payload: automaticIdeMessage() });
    const pending = this.pendingMessages.splice(0);
    for (const payload of pending) {
      this.view.webview.postMessage({ type: "collie:host", payload: payload });
    }
  }

  async render() {
    if (!this.view) return false;
    this.ready = false;
    this.view.webview.html = this.loadingHtml("Starting Collie…");
    try {
      const s = await startServer();
      // asExternalUri keeps the same local-only server usable under WSL, Remote-SSH and Codespaces.
      const ext = await vscode.env.asExternalUri(vscode.Uri.parse("http://127.0.0.1:" + s.port));
      this._extUri = ext;
      const framed = childUrl(ext, "", s.embedToken, {
        ide: "1", surface: this.surface,
        followup: workbenchPreferences().followUpQueueMode,
      });
      this.view.webview.html = this.frameHtml(framed.toString());
      return true;
    } catch (e) {
      log("render failed: " + (e && e.message || e));
      this.view.webview.html = this.errorHtml(String((e && e.message) || e));
      return false;
    }
  }

  frameHtml(url) {
    return iframeShell(url, this.surface === "map" ? "map" : "workbench");
  }

  loadingHtml(msg) {
    return this._shell(
      "<div class=\"spin\"></div><p>" + escapeHtml(msg) + "</p>" +
      "<p class=\"dim\">launching the local Collie runtime for this workspace…</p>"
    );
  }

  errorHtml(msg) {
    return this._shell(
      "<p class=\"err\">Couldn't start Collie.</p>" +
      "<pre>" + escapeHtml(msg) + "</pre>" +
      "<p class=\"dim\">Check <b>collie.command</b> in Settings, then retry.</p>" +
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

async function createAgentPanel(context) {
  const cfg = vscode.workspace.getConfiguration("collie");
  const preserveFocus = cfg.get("newAgentPreserveFocus", true) !== false;
  const column = vscode.window.activeTextEditor && vscode.window.activeTextEditor.viewColumn
    ? vscode.window.activeTextEditor.viewColumn : vscode.ViewColumn.Active;
  const panel = vscode.window.createWebviewPanel("collie.agent", "Collie Agent", {
    viewColumn: column, preserveFocus: preserveFocus,
  }, { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [] });
  panel.iconPath = vscode.Uri.joinPath(context.extensionUri, "media", "collie.svg");
  const controller = new CollieViewProvider(context, "panel");
  agentPanels.add(controller);
  controller.postHostMessage({ type: "collie:new-thread" }, true);
  controller.postHostMessage(automaticIdeMessage(), true);
  panel.onDidDispose(() => agentPanels.delete(controller));
  await controller.attachPanel(panel);
  if (preserveFocus) vscode.window.setStatusBarMessage("Collie: background agent opened without changing focus", 3000);
  return panel;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[c]));
}

function activate(context) {
  output = vscode.window.createOutputChannel("Collie");
  fallbackProvider = new CollieViewProvider(context, "sidebar");
  secondaryProvider = new CollieViewProvider(context, "sidebar");
  provider = supportsSecondarySidebar() ? secondaryProvider : fallbackProvider;
  vscode.commands.executeCommand("setContext", "collie.doesNotSupportSecondarySidebar", !supportsSecondarySidebar());
  context.subscriptions.push(
    output,
    vscode.window.registerWebviewViewProvider("collie.panel", fallbackProvider, {
      webviewOptions: { retainContextWhenHidden: true }, // keep the collie session alive when hidden
    }),
    vscode.window.registerWebviewViewProvider("collie.panel.secondary", secondaryProvider, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.workspace.registerTextDocumentContentProvider("collie-original", {
      provideTextDocumentContent: (uri) => originalDocuments.get(uri.toString()) || "",
    }),
    vscode.languages.registerCodeLensProvider({ scheme: "file" }, new TodoCodeLensProvider())
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("collie.openSidebar", () => focusPrimaryView()),
    vscode.commands.registerCommand("collie.newAgent", () => createAgentPanel(context)),
    vscode.commands.registerCommand("collie.addSelectionToThread", () =>
      addContextsToCurrentThread([selectionContext(vscode.window.activeTextEditor)])),
    vscode.commands.registerCommand("collie.addFileToThread", (uri) => {
      const selected = uri && uri.scheme === "file" ? uri :
        (vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri);
      return addContextsToCurrentThread([workspaceUriItem(selected, null, "", "file")]);
    }),
    vscode.commands.registerCommand("collie.addProblemsToThread", (uri) => {
      const selected = uri && uri.scheme === "file" ? uri :
        (vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri);
      return addContextsToCurrentThread([diagnosticsContext(selected)]);
    }),
    vscode.commands.registerCommand("collie.implementTodo", async (uri, line, todo) => {
      if (!uri || uri.scheme !== "file") return false;
      const document = await vscode.workspace.openTextDocument(uri);
      const safeLine = Math.max(0, Math.min(document.lineCount - 1, Number(line) || 0));
      const range = document.lineAt(safeLine).range;
      const item = workspaceUriItem(uri, range, document.getText(range), "todo");
      return addContextsToCurrentThread([item], {
        draft: "Implement this TODO safely, preserve surrounding behavior, and run the relevant check:\n" + String(todo || "TODO"),
      });
    }),
    vscode.commands.registerCommand("collie.reviewChanges", (uri) => reviewChanges(uri).catch((error) => {
      vscode.window.showErrorMessage("Collie Diff: " + ((error && error.message) || error));
      return false;
    })),
    vscode.commands.registerCommand("collie.reload", async () => {
      const target = activeWorkbenchTarget() || provider;
      if (!target.view) await focusPrimaryView();
      return target.render();
    }),
    vscode.commands.registerCommand("collie.openMap", () => openMap(context)),
    vscode.commands.registerCommand("collie.restart", async () => {
      stopServer();
      try {
        await startServer();
        const visible = allWorkbenchTargets().filter((target) => target.view);
        const results = await Promise.all(visible.map((target) => target.render()));
        if (!results.some((value) => value === false)) vscode.window.showInformationMessage("Collie server restarted.");
        else vscode.window.showErrorMessage("Collie server restarted, but one IDE surface did not reload.");
      } catch (error) {
        vscode.window.showErrorMessage("Collie server did not restart: " + ((error && error.message) || error));
      }
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
    vscode.commands.registerCommand("collie.showLog", () => { if (output) output.show(); }),
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      rememberEditor(editor); scheduleAutomaticIdeContext();
    }),
    vscode.window.onDidChangeTextEditorSelection(() => scheduleAutomaticIdeContext()),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("collie.followUpQueueMode")) {
        for (const target of allWorkbenchTargets()) target.postHostMessage({
          type: "collie:preferences", preferences: workbenchPreferences(),
        }, false);
      }
    })
  );
  rememberEditor(vscode.window.activeTextEditor);
  // Activation registers lightweight editor integrations only. The local process is lazy and the
  // sidebar remains untouched unless the user opens it (or explicitly opts into openOnStartup).
  if (vscode.workspace.getConfiguration("collie").get("openOnStartup", false) === true) {
    focusPrimaryView().catch((error) => log("open on startup: " + ((error && error.message) || error)));
  }
}

function deactivate() {
  if (contextTimer) { clearTimeout(contextTimer); contextTimer = null; }
  if (mapPanel) { try { mapPanel.dispose(); } catch (_) { /* best effort */ } mapPanel = null; }
  originalDocuments.clear();
  stopServer();
}

module.exports = {
  activate,
  deactivate,
  _test: { CollieViewProvider, TodoCodeLensProvider, automaticIdeMessage, childUrl, diagnosticsContext,
           escapeHtml, iframeShell, isCommandAllowed, isPathInside, mapWorkspaceRoot, pickPort,
           primaryViewIds, resolveCommand, resolveLaunchCommand, selectionContext, supportsSecondarySidebar,
           validateExtraArgs, versionAtLeast, waitForServer, workspaceFile, workspaceUriItem }
};
