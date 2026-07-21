// collie — thin VS Code extension. It spawns `collie web` (the polished web GUI) as a child
// process scoped to the open workspace, then shows it in a Webview panel via asExternalUri (which
// VS Code port-forwards, so it works locally AND over Remote/SSH/Codespaces). The Python agent
// loop is unchanged — this is the Claude-Code model: a thin client over the CLI.
const vscode = require("vscode");
const cp = require("child_process");

let server = null;   // the `collie web` child process
let panel = null;    // the Webview panel
let port = 0;
let out = "";

function cfg() { return vscode.workspace.getConfiguration("collie"); }

function workspaceDir() {
  const f = vscode.workspace.workspaceFolders;
  return (f && f[0] && f[0].uri.fsPath) || process.env.HOME || process.cwd();
}

// Security guard: the collie.command value must be a bare PATH name (e.g. "collie") unless it was
// set in the user's/machine's own settings. A path-containing command from a workspace-level
// settings file could point at an attacker-controlled binary inside the repo, which we would then
// spawn — an RCE. collie.command is machine-scoped in package.json (workspace values are ignored by
// VS Code); this is defense-in-depth in case that scope is bypassed.
function isCommandAllowed(c, cmd) {
  if (cmd && cmd.indexOf("/") === -1 && cmd.indexOf("\\") === -1) return true; // bare name: always OK
  const info = c.inspect("command") || {};
  return info.workspaceValue === undefined && info.workspaceFolderValue === undefined;
}

function startServer() {
  return new Promise((resolve, reject) => {
    // Workspace Trust guard: do not spawn a child process on behalf of an untrusted workspace (RCE guard).
    if (vscode.workspace.isTrusted === false) {
      vscode.window.showWarningMessage("Collie: this workspace is not trusted. Trust the workspace to start Collie.");
      return reject(new Error("workspace is not trusted"));
    }
    const c = cfg();
    const cmd = c.get("command", "collie");
    if (!isCommandAllowed(c, cmd)) {
      vscode.window.showErrorMessage("Collie: refusing to run '" + cmd + "'. Set collie.command to a bare PATH name, or configure it in your user/machine settings.");
      return reject(new Error("collie.command is not allowed from this source"));
    }
    const provider = cfg().get("provider", "anthropic-oauth");
    const wantPort = cfg().get("port", 8787);
    out = "";
    let proc;
    try {
      proc = cp.spawn(cmd, ["web", "--no-open", "--port", String(wantPort)], {
        cwd: workspaceDir(),
        env: Object.assign({}, process.env, { COLLIE_PROVIDER: provider, PYTHONUNBUFFERED: "1" }),
      });
    } catch (e) { return reject(e); }

    // collie web prints "collie web · http://127.0.0.1:<port>/ ..." (auto-picks a free port).
    const onData = (d) => {
      out += d.toString();
      const m = out.match(/http:\/\/127\.0\.0\.1:(\d+)\//);
      if (m && !port) { port = parseInt(m[1], 10); resolve(proc); }
    };
    proc.stdout && proc.stdout.on("data", onData);
    proc.stderr && proc.stderr.on("data", onData);
    proc.on("error", reject);
    proc.on("exit", (code) => { if (!port) reject(new Error("collie web exited (code " + code + ")\n" + out)); });
    setTimeout(() => { port ? resolve(proc) : reject(new Error("collie web did not report a port in 10s\n" + out)); }, 10000);
  });
}

async function ensureServer() {
  if (server && port) return;
  server = await startServer();
}

function frameHtml(url) {
  // The outer webview hosts a single full-bleed iframe pointing at the (port-forwarded) collie
  // web GUI. CSP allows framing the forwarded origin over http/https.
  return [
    "<!doctype html><html><head><meta charset=\"utf-8\">",
    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; frame-src http: https:; style-src 'unsafe-inline';\">",
    "<style>html,body{margin:0;padding:0;height:100%;width:100%;overflow:hidden;background:#131C18}",
    "iframe{border:0;width:100%;height:100%;display:block}</style></head>",
    "<body><iframe src=\"", url, "\" allow=\"clipboard-read; clipboard-write\"></iframe></body></html>",
  ].join("");
}

async function openChat() {
  if (panel) { panel.reveal(vscode.ViewColumn.Beside); return; }
  try {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Starting Collie…" },
      () => ensureServer());
  } catch (e) {
    vscode.window.showErrorMessage(
      "Collie: couldn't start `collie web`. Is the CLI installed and on PATH? " +
      "Set collie.command in Settings. (" + (e && e.message ? e.message : e) + ")");
    return;
  }
  const ext = await vscode.env.asExternalUri(vscode.Uri.parse("http://127.0.0.1:" + port + "/"));
  panel = vscode.window.createWebviewPanel("collieChat", "Collie", vscode.ViewColumn.Beside, {
    enableScripts: true, retainContextWhenHidden: true,
  });
  panel.iconPath = undefined;
  panel.webview.html = frameHtml(ext.toString());
  panel.onDidDispose(() => { panel = null; });
}

async function restart() {
  if (panel) { panel.dispose(); panel = null; }
  if (server) { try { server.kill(); } catch (e) {} server = null; }
  port = 0;
  await openChat();
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("collie.openChat", openChat),
    vscode.commands.registerCommand("collie.restart", restart));
}

function deactivate() {
  if (server) { try { server.kill(); } catch (e) {} }
}

module.exports = { activate, deactivate };
