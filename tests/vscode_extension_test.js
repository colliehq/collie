/* Offline contract checks for the VS Code entrance. No Extension Host or Collie process starts. */
"use strict";
const assert = require("assert");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const originalLoad = Module._load;
const vscodeStub = {
  workspace: { isTrusted: true, getConfiguration: () => ({}) },
  window: {}, commands: {}, env: {}, Uri: { parse: (v) => v }
};
Module._load = function (request, parent, isMain) {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};
const extension = require("../vscode-collie/extension.js");
Module._load = originalLoad;
const T = extension._test;

let passed = 0;
function test(name, fn) {
  try { fn(); passed += 1; console.log("  PASS " + name); }
  catch (e) { console.error("  FAIL " + name + "\n       " + e.stack); process.exitCode = 1; }
}

test("command source rejects workspace absolute and every relative path", () => {
  const clean = { inspect: () => ({}) };
  const workspace = { inspect: () => ({ workspaceValue: process.execPath }) };
  assert.equal(T.isCommandAllowed(clean, "collie"), true);
  assert.equal(T.isCommandAllowed(clean, "./collie"), false);
  assert.equal(T.isCommandAllowed(clean, process.execPath), true);
  assert.equal(T.isCommandAllowed(workspace, process.execPath), false);
});

test("PATH resolution is absolute and ignores the current-directory entry", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "collie-vscode-"));
  try {
    const file = path.join(dir, process.platform === "win32" ? "collie.exe" : "collie");
    fs.writeFileSync(file, "fixture");
    if (process.platform !== "win32") fs.chmodSync(file, 0o755);
    const env = { PATH: path.delimiter + dir,
                  PATHEXT: process.platform === "win32" ? ".EXE;.CMD;.BAT" : "" };
    assert.equal(T.resolveCommand("collie", env), file);
    assert.equal(T.resolveCommand(process.execPath, process.env), process.execPath);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("default launch prefers the bundled isolated Collie runtime over PATH", () => {
  if (process.platform !== "win32") return;
  const local = fs.mkdtempSync(path.join(os.tmpdir(), "collie-localappdata-"));
  const python = path.join(local, "Programs", "Collie", "python", "python.exe");
  try {
    fs.mkdirSync(path.dirname(python), { recursive: true });
    fs.writeFileSync(python, "fixture");
    const launch = T.resolveLaunchCommand("collie", { LOCALAPPDATA: local, PATH: "" });
    assert.equal(launch.executable, python);
    assert.deepEqual(launch.prefixArgs, ["-I", "-m", "harness.cli"]);
    assert.equal(launch.bundled, true);
  } finally {
    fs.rmSync(local, { recursive: true, force: true });
  }
});

test("managed exposure flags cannot be smuggled through extraArgs", () => {
  assert.deepEqual(T.validateExtraArgs(["--provider", "mock"]), ["--provider", "mock"]);
  for (const bad of [["--port", "9999"], ["--port=9999"], ["--lan"], ["--remote"], ["--open"]]) {
    assert.throws(() => T.validateExtraArgs(bad), /cannot override/);
  }
  assert.throws(() => T.validateExtraArgs("--lan"), /array of strings/);
});

test("webview frames only the exact forwarded origin and HTML-escapes its token", () => {
  const provider = new T.CollieViewProvider({});
  const html = provider.frameHtml("https://abc.vscode-cdn.net/path?vscode_embed=a%26b&next=x");
  assert(html.includes("frame-src https://abc.vscode-cdn.net;"));
  assert(!html.includes("frame-src https:;"));
  assert(html.includes("vscode_embed=a%26b&amp;next=x"));
  assert(!html.includes("http://127.0.0.1:*"));
  assert(!html.includes("Project map"));
  assert(html.includes("event.origin!==origin"));
  assert(html.includes("event.source===frame.contentWindow"));
  assert(html.includes("collie:host"));
  assert(!html.includes("camera; microphone"));
  assert(!html.includes("clipboard-read"));
  const error = provider.errorHtml('\"><script>alert(1)</script>');
  assert(error.includes("Content-Security-Policy"));
  assert(!error.includes("onclick="));
  assert(!error.includes('><script>alert(1)</script>'));
});

test("map panel URL preserves a forwarded path and tunnel query", () => {
  const url = T.childUrl("https://abc.vscode-cdn.net/proxy/4312?existing=kept", "/map", "secret", { ide: 1, repo: "C:\\workspace\\collie" });
  assert.equal(url.pathname, "/proxy/4312/map");
  assert.equal(url.searchParams.get("existing"), "kept");
  assert.equal(url.searchParams.get("vscode_embed"), "secret");
  assert.equal(url.searchParams.get("ide"), "1");
  assert.equal(url.searchParams.get("repo"), "C:\\workspace\\collie");
  const shell = T.iframeShell(url.toString(), "map");
  assert(shell.includes("collie:openFile"));
  assert(!shell.includes("camera; microphone"));
});

test("map follows the workspace containing the active editor", () => {
  const active = { uri: { fsPath: "C:\\workspace\\collie\\harness\\webapp.py" } };
  const folder = { uri: { fsPath: "C:\\workspace\\collie" } };
  vscodeStub.window.activeTextEditor = { document: active };
  vscodeStub.workspace.workspaceFolders = [{ uri: { fsPath: "C:\\wrong-first-root" } }, folder];
  vscodeStub.workspace.getWorkspaceFolder = (uri) => uri === active.uri ? folder : undefined;
  assert.equal(T.mapWorkspaceRoot(), folder.uri.fsPath);
  delete vscodeStub.window.activeTextEditor;
  delete vscodeStub.workspace.getWorkspaceFolder;
});

test("map file bridge is confined to a real file in the open workspace", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "collie-map-workspace-"));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "collie-map-outside-"));
  const insideFile = path.join(root, "src", "inside.js");
  const outsideFile = path.join(outside, "outside.js");
  try {
    fs.mkdirSync(path.dirname(insideFile), { recursive: true });
    fs.writeFileSync(insideFile, "ok");
    fs.writeFileSync(outsideFile, "no");
    vscodeStub.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
    assert.equal(T.workspaceFile("src/inside.js"), fs.realpathSync(insideFile));
    assert.equal(T.workspaceFile(insideFile), fs.realpathSync(insideFile));
    assert.equal(T.workspaceFile(outsideFile), null);
    assert.equal(T.workspaceFile("../" + path.basename(outside) + "/outside.js"), null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("package settings that can spawn code are machine scoped", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "vscode-collie", "package.json"), "utf8"));
  const props = pkg.contributes.configuration.properties;
  assert.equal(props["collie.command"].scope, "machine");
  assert.equal(props["collie.provider"].scope, "machine");
  assert.equal(props["collie.extraArgs"].scope, "machine");
  assert(pkg.contributes.commands.some((command) => command.command === "collie.openMap"));
  assert(pkg.contributes.commands.some((command) => command.command === "collie.addSelectionToThread"));
  assert(pkg.contributes.commands.some((command) => command.command === "collie.reviewChanges"));
  assert(pkg.contributes.menus["view/title"].some((item) => item.command === "collie.openMap"));
  assert.equal(props["collie.openOnStartup"].default, false);
  assert.equal(props["collie.revealOnContextAdd"].default, false);
  assert.equal(props["collie.newAgentPreserveFocus"].default, true);
  assert.equal(props["collie.followUpQueueMode"].default, "queue");
});

test("restart discards a cancelled startup and reports only a real replacement", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "vscode-collie", "extension.js"), "utf8");
  const stop = source.match(/function stopServer\(\) \{([\s\S]*?)\n\}/);
  assert(stop && stop[1].includes("generation += 1") && stop[1].includes("starting = null"));
  assert(source.includes("await startServer()"));
  assert(source.includes("Promise.all(visible.map((target) => target.render()))"));
  assert(source.includes("Collie server restarted."));
  assert(source.includes("return false;"));
});

test("activation stays lazy and editor context additions do not reveal by default", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "vscode-collie", "extension.js"), "utf8");
  assert(source.includes('get("openOnStartup", false) === true'));
  assert(source.includes('get("revealOnContextAdd", false) === true'));
  assert(source.includes("rememberEditor(vscode.window.activeTextEditor)"));
  assert(source.includes("onDidChangeTextEditorSelection"));
  assert(source.includes("preserveFocus: preserveFocus"));
  assert(!source.includes("startServer().catch"));
});

test("secondary sidebar is version gated for older VS Code", () => {
  assert.equal(T.versionAtLeast("1.106.0", 1, 106), true);
  assert.equal(T.versionAtLeast("1.105.9", 1, 106), false);
  assert.equal(T.versionAtLeast("2.0.0", 1, 106), true);
});

if (!process.exitCode) console.log("\n== VS Code entrance: " + passed + " passed ==");
