// Validate SAT's frozen runtime before dependency code can execute.
import { createHash } from "node:crypto";
import { lstatSync, readFileSync } from "node:fs";
import { join } from "node:path";

const [root, version, manifestDigest, lockDigest, installed] = process.argv.slice(2);
function checkedFile(name, digest) {
  const path = join(root, name);
  if (!lstatSync(path).isFile() || lstatSync(path).isSymbolicLink()) {
    throw new Error(`runtime ${name} must be a regular file`);
  }
  const data = readFileSync(path);
  if (createHash("sha256").update(data).digest("hex") !== digest) {
    throw new Error(`runtime ${name} checksum mismatch`);
  }
  return JSON.parse(data);
}
const manifest = checkedFile("package.json", manifestDigest);
const lock = checkedFile("package-lock.json", lockDigest);
const url = `https://registry.npmjs.org/openclaw/-/openclaw-${version}.tgz`;
if (manifest.dependencies?.openclaw !== url || lock.lockfileVersion !== 3 ||
    lock.packages?.[""]?.dependencies?.openclaw !== url ||
    lock.packages?.["node_modules/openclaw"]?.version !== version ||
    JSON.stringify(manifest.satLifecyclePolicy) !== '{"allowlist":[]}' ||
    manifest.scripts !== undefined) {
  throw new Error("runtime dependency or lifecycle authority mismatch");
}
for (const [path, entry] of Object.entries(lock.packages)) {
  if (!path) continue;
  if (path.split("/").includes("..") || !path.startsWith("node_modules/") || entry.link) {
    throw new Error("runtime lock contains an unsafe package path");
  }
  // Bundled files inherit the integrity of the containing OpenClaw archive.
  const bundled = entry.inBundle === true &&
    path.startsWith("node_modules/openclaw/node_modules/");
  if (!bundled && (!/^https:\/\/registry\.npmjs\.org\/.+\.tgz$/.test(entry.resolved ?? "") ||
      !/^sha512-[A-Za-z0-9+/]{86}==$/.test(entry.integrity ?? ""))) {
    throw new Error(`runtime package ${path} lacks frozen registry integrity`);
  }
  if (installed) {
    const packagePath = join(root, path, "package.json");
    let actual;
    try { actual = JSON.parse(readFileSync(packagePath)); }
    catch (error) {
      if (error.code === "ENOENT" && entry.optional) continue;
      throw error;
    }
    if (actual.version !== entry.version) {
      throw new Error(`installed runtime package ${path} version mismatch`);
    }
  }
}
