#!/usr/bin/env bun
// piyaz MCP-over-HTTP client for archon workers.
// Reads the stored OAuth credential from the omp agent.db, refreshes when
// needed, and speaks JSON-RPC to the hosted piyaz MCP endpoint.
//
// Usage:
//   bun .archon/scripts/piyaz.ts get <ref> <lens>      -> piyaz_get task (prints JSON text)
//   bun .archon/scripts/piyaz.ts working <ref>         -> piyaz_get lens=working (AC ids)
//   bun .archon/scripts/piyaz.ts edit <ref> '<json-ops>' -> raw piyaz_edit passthrough
//   bun .archon/scripts/piyaz.ts complete <ref> '<record>' '<files csv>' ['<decision>' ...]
//        -> one piyaz_edit: set executionRecord + files, add decisions,
//           check ALL acceptance criteria, set status in_review.
import { Database } from "bun:sqlite";

const ENDPOINT = "https://app.piyaz.ai/api/mcp";
const PROVIDER = "mcp_oauth:profile:default:https://app.piyaz.ai/api/mcp";
const DB_PATH = `${process.env.HOME}/.omp/agent/agent.db`;

function getCred() {
  const db = new Database(DB_PATH, { readonly: true });
  const row = db.query("SELECT data FROM auth_credentials WHERE provider = ?").get(PROVIDER);
  db.close();
  if (!row) throw new Error("piyaz credential not found in omp agent.db");
  return JSON.parse(row.data);
}

async function refreshCred(c) {
  const params = new URLSearchParams({ grant_type: "refresh_token", refresh_token: c.refresh, client_id: c.clientId });
  const r = await fetch(c.tokenUrl, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: params });
  const j = await r.json();
  if (!r.ok) throw new Error("piyaz OAuth refresh failed: " + r.status + " " + JSON.stringify(j).slice(0, 200));
  c.access = j.access_token;
  if (j.refresh_token) c.refresh = j.refresh_token;
  c.expires = Date.now() + (j.expires_in ?? 3600) * 1000;
  const db = new Database(DB_PATH);
  db.query("UPDATE auth_credentials SET data = ? WHERE provider = ?").run(JSON.stringify(c), PROVIDER);
  db.close();
  return c;
}

let sessionId = null;
async function rpc(method, params) {
  let c = getCred();
  if (c.expires - Date.now() < 60_000) c = await refreshCred(c);
  const headers = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + c.access };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  let r = await fetch(ENDPOINT, { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  if (r.status === 401) {
    c = await refreshCred(c);
    headers["Authorization"] = "Bearer " + c.access;
    r = await fetch(ENDPOINT, { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  }
  const sid = r.headers.get("mcp-session-id");
  if (sid) sessionId = sid;
  const text = await r.text();
  if (!r.ok) throw new Error("HTTP " + r.status + ": " + text.slice(0, 300));
  const parsed = text.trimStart().startsWith("event:")
    ? JSON.parse(text.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n"))
    : JSON.parse(text);
  if (parsed.error) throw new Error(JSON.stringify(parsed.error).slice(0, 300));
  return parsed.result;
}

async function call(tool, args) {
  const res = await rpc("tools/call", { name: tool, arguments: args });
  const content = res?.content ?? [];
  if (res?.isError) {
    const txt = content.filter((c) => c.type === "text").map((c) => c.text).join("\n");
    throw new Error("tool error: " + txt.slice(0, 400));
  }
  const txt = content.filter((c) => c.type === "text").map((c) => c.text).join("\n");
  try { return JSON.parse(txt); } catch { return { text: txt }; }
}

async function init() {
  await rpc("initialize", { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "piyaz-archon", version: "0.1" } });
  try { await rpc("notifications/initialized", {}); } catch {}
}

const [, , sub, ...rest] = process.argv;

if (!sub || sub === "help") {
  console.log("usage: piyaz.ts get <ref> <lens> | working <ref> | edit <ref> <json-ops> | complete <ref> '<record>' '<files csv>' ['<decision>'...]");
  process.exit(sub ? 0 : 1);
}

await init();

if (sub === "get" || sub === "working") {
  const [ref, lens] = sub === "working" ? [rest[0], "working"] : rest;
  if (!ref) throw new Error("get requires a task ref");
  const out = await call("piyaz_get", { task: ref, lens: lens || "working" });
  console.log(typeof out === "string" ? out : JSON.stringify(out));
} else if (sub === "edit") {
  const [ref, opsJson] = rest;
  const ops = JSON.parse(opsJson);
  const out = await call("piyaz_edit", { task: ref, operations: ops });
  console.log(JSON.stringify(out));
} else if (sub === "complete") {
  const [ref, record, filesCsv, ...decisions] = rest;
  if (!ref || !record) throw new Error("complete requires <ref> '<record>'");
  const working = await call("piyaz_get", { task: ref, lens: "working" });
  const text = typeof working === "string" ? working : (working.text || JSON.stringify(working));
  const acIds = [...text.matchAll(/- \[ \] `([0-9a-f-]+)`/g)].map((m) => m[1]);
  if (acIds.length === 0) throw new Error("no unchecked acceptance criteria found for " + ref);
  const ops = [
    { op: "set", field: "executionRecord", value: record },
    { op: "set", field: "files", value: (filesCsv || "").split(",").map((s) => s.trim()).filter(Boolean) },
    ...decisions.filter(Boolean).map((d) => ({ op: "add", collection: "decisions", text: d })),
    ...acIds.map((id) => ({ op: "check", collection: "acceptanceCriteria", id })),
    { op: "set", field: "status", value: "in_review" },
  ];
  const out = await call("piyaz_edit", { task: ref, operations: ops });
  console.log(JSON.stringify({ task: ref, status: out.status, checked: acIds.length, applied: out.applied }));
} else if (sub === "token") {
  let c = getCred();
  if (c.expires - Date.now() < 60_000) c = await refreshCred(c);
  console.log(c.access);
} else {
  throw new Error("unknown subcommand: " + sub);
}
