#!/usr/bin/env node
/**
 * langfuse-check.mts — coverage check and run scores for an uploaded eval session.
 *
 *   coverage --upload upload.json [--timeout-s 90] [--out coverage.json]
 *       Reads transcript-otlp.mts's summary and polls Langfuse until every uploaded
 *       observation is readable, per trace and per name category ("Tool: *",
 *       "Subagent: *"). Exit 0 = complete, 1 = incomplete after the timeout.
 *
 *   export --upload upload.json --out observations.json
 *       Writes the uploaded traces' observations (only those: native OTel spans of the
 *       same session are left out so tools are not counted twice) in the list shape
 *       parity-eval.py reads: id, traceId, parentObservationId, type, name, startTime,
 *       endTime, latency (s), input, output, metadata, usageDetails, model.
 *
 *   scores --run run.json --session-id SID [--environment ENV] [--dry-run] [--out scores.json]
 *       Posts deterministic, session-level scores derived from the runner's run.json
 *       (oracle results, wall time, tokens, model calls, commits, exit reason,
 *       reference eligibility). Score ids derive from the session id and score name,
 *       so a re-post updates the score instead of adding a duplicate.
 *
 * Read and write the Langfuse REST API only (Node, no SDK). Credentials:
 * LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL.
 */

import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { nameCounts } from "./transcript-otlp.mts";

type Json = Record<string, any>;

function api(env: NodeJS.ProcessEnv = process.env) {
  const { LANGFUSE_PUBLIC_KEY: pk, LANGFUSE_SECRET_KEY: sk, LANGFUSE_BASE_URL: base } = env;
  if (!pk || !sk || !base) throw new Error("LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL are required");
  const headers = { Authorization: `Basic ${Buffer.from(`${pk}:${sk}`).toString("base64")}`, "Content-Type": "application/json" };
  const root = `${base.replace(/\/+$/, "")}/api/public`;
  return {
    async get(path: string): Promise<Json> {
      const res = await fetch(`${root}${path}`, { headers });
      if (!res.ok) throw new Error(`GET ${path} -> HTTP ${res.status}`);
      return res.json() as Promise<Json>;
    },
    async post(path: string, body: Json): Promise<number> {
      return (await fetch(`${root}${path}`, { method: "POST", headers, body: JSON.stringify(body) })).status;
    },
  };
}

// --- coverage -------------------------------------------------------------------------------- //

/** Per-category received/expected; complete when nothing expected is missing. */
export function compareCoverage(expected: Record<string, number>, received: Record<string, number>) {
  const missing: Record<string, number> = {};
  let want = 0, got = 0;
  for (const [name, n] of Object.entries(expected)) {
    const r = Math.min(received[name] ?? 0, n);
    want += n; got += r;
    if (r < n) missing[name] = n - r;
  }
  return { expected, received, missing, ratio: want ? Number((got / want).toFixed(4)) : 1, complete: got === want };
}

async function observationNames(client: ReturnType<typeof api>, traceId: string): Promise<string[]> {
  const names: string[] = [];
  let cursor = "";
  for (let page = 0; page < 50; page++) {
    const q = `/v2/observations?traceId=${traceId}&limit=1000&fields=core,basic${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
    const body = await client.get(q);
    for (const o of body.data ?? []) if (typeof o.name === "string") names.push(o.name);   // name is in "basic"
    cursor = body.meta?.cursor ?? body.meta?.nextCursor ?? "";
    if (!cursor) break;
  }
  return names;
}

export async function coverage(upload: Json, timeoutS: number) {
  const client = api();
  const deadline = Date.now() + timeoutS * 1000;
  let result = compareCoverage(upload.expected_names ?? {}, {});
  for (let attempt = 1; ; attempt++) {
    const names: string[] = [];
    for (const t of upload.trace_ids ?? []) names.push(...(await observationNames(client, t)));
    result = compareCoverage(upload.expected_names ?? {}, nameCounts(names));
    if (result.complete || Date.now() > deadline) {
      return { schema: "geeky-eval.langfuse-coverage/1", session_id: upload.session_id, traces: (upload.trace_ids ?? []).length, attempts: attempt, ...result };
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
}

// --- export --------------------------------------------------------------------------------- //

const EXPORT_FIELDS = ["id", "traceId", "parentObservationId", "type", "name", "startTime", "endTime", "latency", "level",
  "statusMessage", "input", "output", "metadata", "usageDetails", "costDetails", "model", "providedModelName"];

export async function exportObservations(upload: Json): Promise<Json[]> {
  const client = api();
  const all: Json[] = [];
  for (const traceId of upload.trace_ids ?? []) {
    let cursor = "";
    for (let page = 0; page < 50; page++) {
      const body = await client.get(`/v2/observations?traceId=${traceId}&limit=1000&fields=core,basic,time,io,metadata,model,usage${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      for (const o of body.data ?? []) all.push(Object.fromEntries(EXPORT_FIELDS.filter((k) => k in o).map((k) => [k, o[k]])));
      cursor = body.meta?.cursor ?? body.meta?.nextCursor ?? "";
      if (!cursor) break;
    }
  }
  return all.sort((a, b) => String(a.startTime).localeCompare(String(b.startTime)));
}

// --- scores --------------------------------------------------------------------------------- //

/** A stable UUID-shaped id, so re-posting a score updates it. */
const scoreId = (sessionId: string, name: string) => {
  const h = createHash("sha256").update(`${sessionId}:${name}`).digest("hex");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
};

export function buildScores(run: Json, sessionId: string, environment?: string): Json[] {
  const o = run.oracle ?? {};
  const collected = Number(o.collected ?? 0);
  const metadata = {
    case: run.case, harness: run.harness, model: run.model, plugin_sha: run.plugin_sha, stamp: run.execution?.attempt_id ?? run.started_utc,
    workspace_seed: run.seed_sha, reference_eligible: run.reference_eligible === true,
    platform: process.platform === "darwin" ? "macos-mxc-seatbelt" : process.platform,
  };
  const scores: [string, "NUMERIC" | "BOOLEAN" | "CATEGORICAL", unknown, string][] = [
    ["geeky.oracle.passed", "NUMERIC", o.passed, "hidden-oracle tests passed (first of 3 runs)"],
    ["geeky.oracle.failed", "NUMERIC", o.failed, "hidden-oracle tests failed"],
    ["geeky.oracle.errors", "NUMERIC", o.errors, "hidden-oracle tests erroring"],
    ["geeky.oracle.pass_rate", "NUMERIC", collected ? Number((Number(o.passed ?? 0) / collected).toFixed(4)) : 0, "passed / collected"],
    ["geeky.oracle.stable", "BOOLEAN", o.stable_over_3 === true ? 1 : 0, "same oracle result on all 3 runs"],
    ["geeky.wall_time_s", "NUMERIC", run.wall_time_s, "contestant wall time"],
    ["geeky.tokens.total", "NUMERIC", run.tokens?.total, "all models, input incl. cache + output"],
    ["geeky.model_calls", "NUMERIC", run.model_calls, "API calls (all agents)"],
    ["geeky.commits", "NUMERIC", run.commits, "commits on top of the seed"],
    ["geeky.exit_reason", "CATEGORICAL", run.exit_reason, "runner exit classification"],
    ["geeky.reference_eligible", "BOOLEAN", run.reference_eligible === true ? 1 : 0, "eligible as a reference run"],
  ];
  return scores
    .filter(([, , value]) => value !== undefined && value !== null && !(typeof value === "number" && !Number.isFinite(value)))
    .map(([name, dataType, value, comment]) => ({
      id: scoreId(sessionId, name), sessionId, name, dataType,
      value: dataType === "CATEGORICAL" ? String(value) : Number(value),
      comment, metadata, ...(environment ? { environment } : {}),
    }));
}

// --- CLI ------------------------------------------------------------------------------------ //

function flag(argv: string[], name: string, fallback = ""): string {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1]! : fallback;
}

async function main() {
  const [command, ...argv] = process.argv.slice(2);
  const out = flag(argv, "--out");
  let report: Json;
  if (command === "coverage") {
    const upload = JSON.parse(readFileSync(resolve(flag(argv, "--upload")), "utf8"));
    report = await coverage(upload, Number(flag(argv, "--timeout-s", "90")));
    process.exitCode = report.complete ? 0 : 1;
  } else if (command === "export") {
    const upload = JSON.parse(readFileSync(resolve(flag(argv, "--upload")), "utf8"));
    const observations = await exportObservations(upload);
    report = { schema: "geeky-eval.langfuse-observations/1", session_id: upload.session_id, count: observations.length, observations };
    process.exitCode = observations.length ? 0 : 1;
  } else if (command === "scores") {
    const run = JSON.parse(readFileSync(resolve(flag(argv, "--run")), "utf8"));
    const sessionId = flag(argv, "--session-id") || run.session_id;
    if (!sessionId) throw new Error("--session-id is required (none in run.json)");
    const scores = buildScores(run, sessionId, flag(argv, "--environment", process.env.LANGFUSE_TRACING_ENVIRONMENT ?? "") || undefined);
    const dryRun = argv.includes("--dry-run");
    const statuses: number[] = [];
    if (!dryRun) { const client = api(); for (const s of scores) statuses.push(await client.post("/scores", s)); }
    const ok = dryRun || statuses.every((s) => s >= 200 && s < 300);
    report = { schema: "geeky-eval.langfuse-scores/1", session_id: sessionId, dry_run: dryRun, ok, statuses,
      scores: scores.map((s) => ({ name: s.name, dataType: s.dataType, value: s.value })) };
    process.exitCode = ok ? 0 : 1;
  } else {
    throw new Error("usage: langfuse-check.mts coverage --upload upload.json | export --upload upload.json --out obs.json | scores --run run.json [--session-id SID]");
  }
  const text = JSON.stringify(report, null, 2) + "\n";
  if (out) writeFileSync(out, text);
  process.stdout.write(text);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch((e) => { process.stderr.write(`langfuse-check: ${e instanceof Error ? e.message : e}\n`); process.exitCode = 2; });
}
