import { copyFile, mkdir, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
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
  "_redirects",
  "_headers",
];

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await Promise.all(publicFiles.map((name) => copyFile(join(root, name), join(output, name))));
console.log(`Built ${publicFiles.length} explicitly public files in landing/dist.`);
