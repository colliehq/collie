/* Offline contract checks for the VS Code entrance. No Extension Host or Collie process starts. */
"use strict";
const assert = require("assert");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") {
    return {
      workspace: { isTrusted: true, getConfiguration: () => ({}) },
      window: {}, commands: {}, env: {}, Uri: { parse: (v) => v }
    };
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
  const error = provider.errorHtml('\"><script>alert(1)</script>');
  assert(error.includes("Content-Security-Policy"));
  assert(!error.includes("onclick="));
  assert(!error.includes('><script>alert(1)</script>'));
});

test("package settings that can spawn code are machine scoped", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "vscode-collie", "package.json"), "utf8"));
  const props = pkg.contributes.configuration.properties;
  assert.equal(props["collie.command"].scope, "machine");
  assert.equal(props["collie.provider"].scope, "machine");
  assert.equal(props["collie.extraArgs"].scope, "machine");
});

test("restart discards a cancelled startup and reports only a real replacement", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "vscode-collie", "extension.js"), "utf8");
  const stop = source.match(/function stopServer\(\) \{([\s\S]*?)\n\}/);
  assert(stop && stop[1].includes("generation += 1") && stop[1].includes("starting = null"));
  assert(source.includes("const restarted = await provider.render()"));
  assert(source.includes("if (restarted) vscode.window.showInformationMessage"));
  assert(source.includes("return false;"));
});

if (!process.exitCode) console.log("\n== VS Code entrance: " + passed + " passed ==");
