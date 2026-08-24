#!/usr/bin/env bun
// Thin Piyaz MCP client. Graph lives in Piyaz; this is I/O only.
// Auth: same OAuth row as piyaz_trial (`~/.omp/agent/agent.db`).
import { Database } from "bun:sqlite";

const ENDPOINT = "https://app.piyaz.ai/api/mcp";
const PROVIDER = "mcp_oauth:profile:default:https://app.piyaz.ai/api/mcp";
const DB_PATH = `${process.env.HOME}/.omp/agent/agent.db`;

function getCred() {
  const db = new Database(DB_PATH, { readonly: true });
  const row = db.query("SELECT data FROM auth_credentials WHERE provider = ?").get(PROVIDER);
  db.close();
  if (!row) throw new Error("piyaz credential missing in omp agent.db");
  return JSON.parse(row.data);
}

async function refreshCred(c) {
  const params = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: c.refresh,
    client_id: c.clientId,
  });
  const r = await fetch(c.tokenUrl, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: params,
  });
  const j = await r.json();
  if (!r.ok) throw new Error("piyaz OAuth refresh failed: " + r.status);
  c.access = j.access_token;
  if (j.refresh_token) c.refresh = j.refresh_token;
  c.expires = Date.now() + (j.expires_in ?? 3600) * 1000;
  const db = new Database(DB_PATH);
  db.query("UPDATE auth_credentials SET data = ? WHERE provider = ?").run(
    JSON.stringify(c),
    PROVIDER,
  );
  db.close();
  return c;
}

let sessionId = null;
async function rpc(method, params) {
  let c = getCred();
  if (c.expires - Date.now() < 60_000) c = await refreshCred(c);
  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    Authorization: "Bearer " + c.access,
  };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  let r = await fetch(ENDPOINT, {
    method: "POST",
    headers,
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  if (r.status === 401) {
    c = await refreshCred(c);
    headers.Authorization = "Bearer " + c.access;
    r = await fetch(ENDPOINT, {
      method: "POST",
      headers,
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    });
  }
  const sid = r.headers.get("mcp-session-id");
  if (sid) sessionId = sid;
  const text = await r.text();
  if (!r.ok) throw new Error("HTTP " + r.status + ": " + text.slice(0, 300));
  const parsed = text.trimStart().startsWith("event:")
    ? JSON.parse(
        text.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n"),
      )
    : JSON.parse(text);
  if (parsed.error) throw new Error(JSON.stringify(parsed.error).slice(0, 300));
  return parsed.result;
}

async function call(tool, args) {
  const res = await rpc("tools/call", { name: tool, arguments: args });
  const content = res?.content ?? [];
  if (res?.isError) {
    throw new Error(content.filter((c) => c.type === "text").map((c) => c.text).join("\n").slice(0, 400));
  }
  const txt = content.filter((c) => c.type === "text").map((c) => c.text).join("\n");
  try {
    return JSON.parse(txt);
  } catch {
    return { text: txt };
  }
}

async function init() {
  await rpc("initialize", {
    protocolVersion: "2025-03-26",
    capabilities: {},
    clientInfo: { name: "omp-modes", version: "0.1" },
  });
  try {
    await rpc("notifications/initialized", {});
  } catch {}
}

const [, , sub, ...rest] = process.argv;
if (!sub || sub === "help") {
  console.log("usage: piyaz.ts ready <project> | get <ref> | claim <ref> | in-review <ref> | search <project> [status] | create-batch <project> <json-path>");
  process.exit(sub ? 0 : 1);
}

let createBatchPayload = null;
if (sub === "create-batch") {
  const [project, jsonPath] = rest;
  if (!project) throw new Error("create-batch requires project id");
  if (!jsonPath) throw new Error("create-batch requires json path");
  try {
    createBatchPayload = JSON.parse(await Bun.file(jsonPath).text());
  } catch (e) {
    throw new Error("create-batch: cannot read JSON at " + jsonPath + ": " + (e?.message ?? e));
  }
  if (!createBatchPayload || typeof createBatchPayload !== "object" || Array.isArray(createBatchPayload)) {
    throw new Error("create-batch: payload must be a JSON object");
  }
  if (!Array.isArray(createBatchPayload.tasks) || createBatchPayload.tasks.length === 0) {
    throw new Error("create-batch: payload.tasks must be a non-empty array");
  }
  if (createBatchPayload.project !== undefined && createBatchPayload.project !== project) {
    throw new Error("create-batch: payload project conflicts with argument: " + JSON.stringify(createBatchPayload.project));
  }
}

await init();

if (sub === "ready") {
  const project = rest[0];
  if (!project) throw new Error("ready requires project id");
  const out = await call("piyaz_map", { view: "ready", project, limit: 50 });
  console.log(JSON.stringify(out));
} else if (sub === "get") {
  const ref = rest[0];
  if (!ref) throw new Error("get requires ref");
  const out = await call("piyaz_get", { task: ref, lens: "working" });
  console.log(JSON.stringify(out));
} else if (sub === "claim") {
  const ref = rest[0];
  if (!ref) throw new Error("claim requires ref");
  const out = await call("piyaz_edit", {
    task: ref,
    operations: [
      { op: "add", collection: "assignees", value: "me" },
      { op: "set", field: "status", value: "in_progress" },
    ],
  });
  console.log(JSON.stringify(out));
} else if (sub === "in-review") {
  const ref = rest[0];
  if (!ref) throw new Error("in-review requires ref");
  const out = await call("piyaz_edit", {
    task: ref,
    operations: [{ op: "set", field: "status", value: "in_review" }],
  });
  console.log(JSON.stringify(out));
} else if (sub === "search") {
  const [project, status] = rest;
  const out = await call("piyaz_search", {
    project,
    status: status ? [status] : ["planned"],
    limit: 50,
  });
  console.log(JSON.stringify(out));
} else if (sub === "create-batch") {
  const [project] = rest;
  const { project: _injected, ...body } = createBatchPayload;
  const out = await call("piyaz_create", { project, ...body });
  console.log(JSON.stringify(out));
} else {
  throw new Error("unknown subcommand: " + sub);
}
