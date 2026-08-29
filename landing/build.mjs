import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const repository = resolve(root, "..");
const output = join(root, "dist");
const publicFiles = [
  "index.html",
  "privacy.html",
  "404.html",
  "collie-logo.svg",
  "collie-logo.png",
  "favicon.ico",
  "robots.txt",
  "sitemap.xml",
  "site-version.json",
  "_redirects",
];
const cspPlaceholder = "__COLLIE_INLINE_SCRIPT_HASHES__";

// The site is product source, not a separately drifting marketing project. Every product tag
// therefore freezes the matching homepage, privacy notice, and an explicit machine-readable
// content contract. Refuse to build if those three versions no longer agree.
const siteManifest = JSON.parse(await readFile(join(root, "site-version.json"), "utf8"));
const packageSource = await readFile(join(repository, "harness", "__init__.py"), "utf8");
const productVersion = packageSource.match(/^__version__\s*=\s*["']([^"']+)["']/m)?.[1];
if (!productVersion || siteManifest.source_product_version !== productVersion) {
  throw new Error("site-version.json source_product_version must match harness.__version__");
}
if (!siteManifest.content_version || !siteManifest.privacy_notice_version) {
  throw new Error("site-version.json must name content and privacy notice versions");
}
const sourceIndex = await readFile(join(root, "index.html"), "utf8");
const sourcePrivacy = await readFile(join(root, "privacy.html"), "utf8");
if (!sourceIndex.includes(`data-site-version="${siteManifest.content_version}"`)) {
  throw new Error("index.html data-site-version does not match site-version.json");
}
if (!sourcePrivacy.includes(
  `data-privacy-notice-version="${siteManifest.privacy_notice_version}"`,
)) {
  throw new Error("privacy.html notice version does not match site-version.json");
}

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await Promise.all(publicFiles.map((name) => copyFile(join(root, name), join(output, name))));

// Inline scripts are intentional (the dependency-free page and JSON-LD), but their CSP hashes must
// describe the exact bytes being shipped. Generate them from index.html so an edit cannot silently
// make the browser block navigation/chat code while a stale hand-maintained hash looks reassuring.
const index = await readFile(join(root, "index.html"), "utf8");
const inlineScripts = [];
for (const match of index.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
  if (!/\bsrc\s*=/i.test(match[1])) inlineScripts.push(match[2]);
}
if (!inlineScripts.length) throw new Error("index.html contains no inline scripts to authorize");
// The HTML tokenizer normalizes CRLF and lone CR to LF before CSP hashes the script text. Hash that
// parsed representation (not raw checkout bytes), so Windows line endings do not create false CSPs.
const hashes = inlineScripts.map((script) => {
  const browserText = script.replace(/\r\n?/g, "\n");
  return `'sha256-${createHash("sha256").update(browserText, "utf8").digest("base64")}'`;
});
const headerTemplate = await readFile(join(root, "_headers"), "utf8");
if (headerTemplate.split(cspPlaceholder).length !== 2) {
  throw new Error(`_headers must contain ${cspPlaceholder} exactly once`);
}
const headers = headerTemplate.replace(cspPlaceholder, hashes.join(" "));
if (headers.includes(cspPlaceholder)) throw new Error("unresolved CSP placeholder");
await writeFile(join(output, "_headers"), headers, "utf8");

console.log(
  `Built ${publicFiles.length + 1} explicitly public files for Collie ${productVersion} ` +
  `(${siteManifest.content_version}) in landing/dist.`,
);
