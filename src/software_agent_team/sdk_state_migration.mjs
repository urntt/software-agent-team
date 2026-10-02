// The pinned SDK owns all schema and media transformations. This helper only
// admits its reviewed migration export and emits a secret-free result.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { DatabaseSync } from "node:sqlite";

const prefix = "SAT_SDK_STATE_MIGRATION_V1 ";
const state = "/sat-state";
const dist = "/opt/sat-sdk/runtime/node_modules/openclaw/dist";
const targets = JSON.parse(process.argv[2]);
let activeTarget = null;

function version(target) {
  activeTarget = target;
  const database = new DatabaseSync(path.join(state, target), { readOnly: true });
  try {
    if (database.prepare("PRAGMA quick_check").get().quick_check !== "ok") {
      throw new Error("integrity");
    }
    return database.prepare("PRAGMA user_version").get().user_version;
  } finally {
    database.close();
  }
}

try {
  async function sdkExport(pattern, exported) {
    let selected;
    for (const name of fs.readdirSync(dist).filter((name) => pattern.test(name))) {
      const module = await import(pathToFileURL(path.join(dist, name)).href);
      if (typeof module[exported] === "function") {
        if (selected) throw new Error("ambiguous SDK migration export");
        selected = module[exported];
      }
    }
    if (!selected) throw new Error("missing SDK migration export");
    return selected;
  }
  const prepareState = await sdkExport(
    /^state-migrations[.]doctor-[A-Za-z0-9_-]+[.]mjs$/,
    "prepareLegacyStateDatabaseSchema",
  );
  const migrate = await sdkExport(
    /^state-migrations[.]media-persistence-[A-Za-z0-9_-]+[.]mjs$/,
    "migrateLegacyMediaPersistence",
  );
  const preparation = await prepareState(process.env);
  if (preparation.outcome !== "completed" && preparation.outcome !== "skipped") {
    throw new Error("SDK refused shared-state migration");
  }
  const configuredAgentDatabaseTargets = targets.map((target) => {
    if (!/^agents\/[a-z0-9_-]+\/agent\/openclaw-agent[.]sqlite$/.test(target)) {
      throw new Error("invalid target");
    }
    version(target);
    return { agentId: target.split("/")[1], path: path.join(state, target) };
  });
  activeTarget = null;
  const result = await migrate({ configuredAgentDatabaseTargets, env: process.env });
  if (result.warnings.length && result.warningDisposition !== "recoverable") {
    throw new Error("SDK refused migration");
  }
  const versions = targets.map((target) => ({ database: target, version: version(target) }));
  // Do not emit SDK changes/warnings, auth rows, or exception messages. Any of
  // those can contain private data. The caller verifies every resulting version.
  console.log(prefix + JSON.stringify({ ok: true, versions }));
} catch {
  console.log(prefix + JSON.stringify({ ok: false, database: activeTarget }));
  process.exitCode = 1;
}
