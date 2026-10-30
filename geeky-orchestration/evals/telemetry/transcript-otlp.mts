#!/usr/bin/env node
/**
 * transcript-otlp.mts — upload a Claude Code session transcript to Langfuse as OTLP traces.
 *
 * Runs on the host after an eval run (never inside the sandbox) and needs only Node. It
 * reads <session>.jsonl and its subagent transcripts and posts OTLP/JSON to Langfuse's
 * /api/public/otel/v1/traces, so the full content (prompts, assistant text, thinking,
 * tool inputs and results, subagents, token usage) reaches Langfuse. Claude Code's own
 * OTel export cannot carry that content there: it emits responses and tool content as
 * OTel log events, and Langfuse ingests traces only.
 *
 * The trace shape follows the pinned Langfuse Claude hook (evals/telemetry/pins.json),
 * so runs stay comparable with the canonical telemetry path: one "Conversational Turn"
 * span per user turn, an "LLM Call" generation per API message ("Subagent LLM Call"
 * inside subagents), "Tool: <name>" tool observations, and "Subagent: <description>"
 * spans under the Agent call that spawned them. Usage keys match the hook's
 * get_usage_details_from_row. All trace and span ids derive from the session id, so a
 * re-upload replaces observations instead of duplicating them.
 *
 * Usage:
 *   node transcript-otlp.mts --transcript run/session.jsonl [--subagents run/subagents]
 *        [--session-id SID] [--tag T]... [--meta K=V]... [--environment ENV]
 *        [--max-chars N] [--dry-run] [--out otlp.json]
 * Credentials: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and LANGFUSE_BASE_URL or
 * OTEL_EXPORTER_OTLP_ENDPOINT (…/api/public/otel). Prints a JSON summary on stdout.
 */

import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

export const UPLOADER = { name: "geeky-eval-transcript-otlp", version: "1.0.0" };

type Row = Record<string, any>;
type AttrValue = string | number | boolean | string[];

export interface Span {
  traceId: string; spanId: string; parentSpanId?: string;
  name: string; start: string; end: string;
  attributes: Record<string, AttrValue>; error?: boolean;
}

export interface BuildOptions {
  sessionId: string;
  maxChars: number;
  tags: string[];
  traceMetadata: Record<string, string>;
  environment?: string;
  subagents: Map<string, { rows: Row[]; meta: Row; file: string }>;
}

const hex = (text: string, length: number) => createHash("sha256").update(text).digest("hex").slice(0, length);

export function readJsonl(path: string): Row[] {
  const rows: Row[] = [];
  for (const line of readFileSync(path, "utf8").split("\n")) {
    if (!line.trim()) continue;
    try { rows.push(JSON.parse(line)); } catch { /* a torn last line is not content */ }
  }
  return rows;
}

/** subagents/agent-<id>.jsonl + agent-<id>.meta.json, keyed by the spawning tool_use id. */
export function loadSubagents(dir?: string): BuildOptions["subagents"] {
  const found: BuildOptions["subagents"] = new Map();
  if (!dir || !existsSync(dir)) return found;
  for (const name of readdirSync(dir)) {
    if (!name.endsWith(".meta.json")) continue;
    try {
      const meta = JSON.parse(readFileSync(join(dir, name), "utf8"));
      const file = join(dir, name.replace(/\.meta\.json$/, ".jsonl"));
      if (meta.toolUseId && existsSync(file)) found.set(meta.toolUseId, { rows: readJsonl(file), meta, file: name.replace(/\.meta\.json$/, ".jsonl") });
    } catch { /* unreadable meta: the subagent is skipped, not the run */ }
  }
  return found;
}

// --- transcript -> turns ------------------------------------------------------------------- //

const blocksOf = (row: Row): any[] => {
  const c = row.message?.content;
  return Array.isArray(c) ? c : typeof c === "string" ? [{ type: "text", text: c }] : [];
};
const isToolResultRow = (row: Row) => row.type === "user" && blocksOf(row).length > 0 && blocksOf(row).every((b) => b?.type === "tool_result");
const isPromptRow = (row: Row) => row.type === "user" && !row.isMeta && !row.isCompactSummary && !isToolResultRow(row) && blocksOf(row).length > 0;
export const textOf = (blocks: any[]) => blocks.filter((b) => b?.type === "text").map((b) => b.text).join("\n");

interface ToolResult { content: unknown; isError: boolean; timestamp?: string }
interface LlmCall {
  id: string; model?: string; requestedModel?: string; start: string; end: string;
  blocks: { block: any; timestamp: string }[]; usage?: Row; stopReason?: string;
  input: unknown[]; effort?: string; thinkingDurationMs?: number;
}
interface Turn { index: number; prompt: Row; calls: LlmCall[]; end: string }

export function buildTurns(rows: Row[]): { turns: Turn[]; results: Map<string, ToolResult> } {
  const turns: Turn[] = [];
  const results = new Map<string, ToolResult>();
  let current: Turn | null = null;
  let pending: unknown[] = [];
  let byId = new Map<string, LlmCall>();
  for (const row of rows) {
    if (row.type !== "user" && row.type !== "assistant") continue;
    const ts: string = row.timestamp ?? current?.end ?? new Date(0).toISOString();
    if (isPromptRow(row)) {
      current = { index: turns.length + 1, prompt: row, calls: [], end: ts };
      turns.push(current);
      pending = [{ role: "user", content: blocksOf(row) }];
      byId = new Map();
      continue;
    }
    if (!current) continue;
    if (current.end < ts) current.end = ts;
    if (row.type === "user") {
      for (const b of blocksOf(row)) {
        if (b?.type === "tool_result") results.set(b.tool_use_id, { content: b.content, isError: b.is_error === true, timestamp: ts });
      }
      pending.push({ role: "user", content: blocksOf(row) });
      continue;
    }
    const id: string = row.message?.id ?? row.uuid;
    let call = byId.get(id);
    if (!call) {
      call = { id, model: row.message?.model, requestedModel: row.requestedModel, start: ts, end: ts, blocks: [], input: pending,
        effort: row.effort, thinkingDurationMs: row.thinkingDurationMs };
      pending = [];
      byId.set(id, call);
      current.calls.push(call);
    }
    for (const block of blocksOf(row)) call.blocks.push({ block, timestamp: ts });
    if (call.end < ts) call.end = ts;
    if (row.message?.usage) call.usage = row.message.usage;
    if (row.message?.stop_reason) call.stopReason = row.message.stop_reason;
  }
  return { turns, results };
}

/** The pinned hook's get_usage_details_from_row + get_cache_write_details. */
export function usageDetails(usage?: Row): Record<string, number> | undefined {
  if (!usage) return undefined;
  const d: Record<string, number> = {};
  for (const [src, dst] of [["input_tokens", "input"], ["output_tokens", "output"], ["cache_read_input_tokens", "cache_read_input_tokens"]] as const) {
    if (Number.isInteger(usage[src]) && usage[src] > 0) d[dst] = usage[src];
  }
  const split = usage.cache_creation;
  let wrote = false;
  for (const [src, dst] of [["ephemeral_5m_input_tokens", "input_cache_creation_5m"], ["ephemeral_1h_input_tokens", "input_cache_creation_1h"]] as const) {
    if (split && Number.isInteger(split[src]) && split[src] > 0) { d[dst] = split[src]; wrote = true; }
  }
  if (!wrote && Number.isInteger(usage.cache_creation_input_tokens) && usage.cache_creation_input_tokens > 0) {
    d.cache_creation_input_tokens = usage.cache_creation_input_tokens;
  }
  return Object.keys(d).length ? d : undefined;
}

/** Cap every string in a value at maxChars (the hook's CC_LANGFUSE_MAX_CHARS), keeping JSON valid. */
export function truncateDeep(value: unknown, maxChars: number): unknown {
  if (typeof value === "string") {
    return value.length > maxChars ? `${value.slice(0, maxChars)}…[truncated ${value.length - maxChars} chars]` : value;
  }
  if (Array.isArray(value)) return value.map((v) => truncateDeep(v, maxChars));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, truncateDeep(v, maxChars)]));
  return value;
}

const outputBlock = (b: any) => {
  if (b?.type === "thinking") return { type: "thinking", thinking: b.thinking ?? "" };
  if (b?.type === "redacted_thinking") return { type: "redacted_thinking" };
  if (b?.type === "tool_use") return { type: "tool_use", id: b.id, name: b.name, input: b.input };
  if (b?.type === "text") return { type: "text", text: b.text };
  return { type: b?.type ?? "unknown" };
};

export function buildSpans(rows: Row[], opt: BuildOptions): { spans: Span[]; counts: Record<string, number> } {
  const spans: Span[] = [];
  const counts = { turns: 0, llm_calls: 0, tools: 0, tool_errors: 0, subagents: 0, subagent_llm_calls: 0 };
  const json = (v: unknown) => JSON.stringify(truncateDeep(v, opt.maxChars));

  const emitCalls = (calls: LlmCall[], results: Map<string, ToolResult>, traceId: string, parentId: string,
    generationName: string, depth: number) => {
    for (const call of calls) {
      const spanId = hex(`${traceId}:${parentId}:${call.id}:gen`, 16);
      const usage = usageDetails(call.usage);
      const attributes: Record<string, AttrValue> = {
        "langfuse.observation.type": "generation",
        "langfuse.observation.input": json(call.input),
        "langfuse.observation.output": json({ role: "assistant", content: call.blocks.map((x) => outputBlock(x.block)) }),
        "langfuse.observation.metadata.message_id": call.id,
      };
      if (call.model) { attributes["gen_ai.response.model"] = call.model; attributes["langfuse.observation.model.name"] = call.model; }
      if (call.requestedModel) attributes["gen_ai.request.model"] = call.requestedModel;
      if (usage) attributes["langfuse.observation.usage_details"] = JSON.stringify(usage);
      if (call.stopReason) attributes["langfuse.observation.metadata.stop_reason"] = call.stopReason;
      if (call.effort) attributes["langfuse.observation.metadata.effort"] = call.effort;
      if (call.thinkingDurationMs !== undefined) attributes["langfuse.observation.metadata.thinking_duration_ms"] = call.thinkingDurationMs;
      spans.push({ traceId, spanId, parentSpanId: parentId, name: generationName, start: call.start, end: call.end, attributes });
      if (depth === 0) counts.llm_calls++; else counts.subagent_llm_calls++;

      for (const { block, timestamp } of call.blocks) {
        if (block?.type !== "tool_use") continue;
        const result = results.get(block.id);
        const toolSpanId = hex(`${traceId}:${block.id}:tool`, 16);
        const toolAttrs: Record<string, AttrValue> = {
          "langfuse.observation.type": "tool",
          "langfuse.observation.input": json(block.input ?? {}),
          "langfuse.observation.metadata.tool_use_id": block.id,
          "langfuse.observation.metadata.tool_name": block.name,
        };
        if (result) toolAttrs["langfuse.observation.output"] = json(result.content);
        else toolAttrs["langfuse.observation.metadata.result"] = "missing";
        if (result?.isError) { toolAttrs["langfuse.observation.level"] = "ERROR"; counts.tool_errors++; }
        const toolEnd = result?.timestamp && result.timestamp > timestamp ? result.timestamp : timestamp;
        spans.push({ traceId, spanId: toolSpanId, parentSpanId: spanId, name: `Tool: ${block.name}`, start: timestamp, end: toolEnd,
          attributes: toolAttrs, error: result?.isError });
        counts.tools++;

        const sub = opt.subagents.get(block.id);
        if (sub && depth < 8) {
          const { turns: subTurns, results: subResults } = buildTurns(sub.rows);
          const allCalls = subTurns.flatMap((t) => t.calls);
          const first = subTurns[0];
          const description: string = sub.meta.description ?? block.input?.description ?? "";
          const subSpanId = hex(`${traceId}:${block.id}:subagent`, 16);
          const last = allCalls.at(-1);
          spans.push({
            traceId, spanId: subSpanId, parentSpanId: toolSpanId,
            name: description ? `Subagent: ${description}` : "Subagent",
            start: first?.prompt.timestamp ?? timestamp, end: subTurns.at(-1)?.end ?? toolEnd,
            attributes: {
              "langfuse.observation.type": "span",
              "langfuse.observation.input": json({ role: "user", content: first ? textOf(blocksOf(first.prompt)) : "" }),
              "langfuse.observation.output": json({ role: "assistant", content: last ? textOf(last.blocks.map((x) => x.block)) : "" }),
              "langfuse.observation.metadata.agent_type": String(sub.meta.agentType ?? ""),
              "langfuse.observation.metadata.description": description,
              "langfuse.observation.metadata.transcript": sub.file,
              "langfuse.observation.metadata.spawn_depth": Number(sub.meta.spawnDepth ?? depth + 1),
            },
          });
          counts.subagents++;
          emitCalls(allCalls, subResults, traceId, subSpanId, "Subagent LLM Call", depth + 1);
        }
      }
    }
  };

  const { turns, results } = buildTurns(rows);
  for (const turn of turns) {
    const traceId = hex(`${opt.sessionId}:turn:${turn.index}`, 32);
    const rootId = hex(`${traceId}:root`, 16);
    const lastText = turn.calls.map((c) => textOf(c.blocks.map((x) => x.block))).filter(Boolean).at(-1) ?? "";
    const attributes: Record<string, AttrValue> = {
      "langfuse.observation.type": "span",
      "langfuse.trace.name": "Conversational Turn",
      "langfuse.session.id": opt.sessionId,
      "langfuse.observation.input": json({ role: "user", content: textOf(blocksOf(turn.prompt)) }),
      "langfuse.observation.output": json({ role: "assistant", content: lastText }),
      "langfuse.trace.metadata.session_id": opt.sessionId,
      "langfuse.trace.metadata.turn": turn.index,
      "langfuse.trace.metadata.uploader": `${UPLOADER.name}@${UPLOADER.version}`,
    };
    if (opt.tags.length) attributes["langfuse.trace.tags"] = opt.tags;
    if (opt.environment) attributes["langfuse.environment"] = opt.environment;
    for (const [k, v] of Object.entries(opt.traceMetadata)) attributes[`langfuse.trace.metadata.${k}`] = v;
    spans.push({ traceId, spanId: rootId, name: "Conversational Turn", start: turn.prompt.timestamp ?? turn.end, end: turn.end, attributes });
    counts.turns++;
    emitCalls(turn.calls, results, traceId, rootId, "LLM Call", 0);
  }
  return { spans, counts };
}

// --- OTLP/JSON ----------------------------------------------------------------------------- //

const otlpValue = (v: AttrValue): Record<string, unknown> =>
  Array.isArray(v) ? { arrayValue: { values: v.map((s) => ({ stringValue: s })) } }
    : typeof v === "boolean" ? { boolValue: v }
    : typeof v === "number" ? (Number.isInteger(v) ? { intValue: String(v) } : { doubleValue: v })
    : { stringValue: v };

const nanos = (iso: string) => (BigInt(Date.parse(iso)) * 1_000_000n).toString();

export function toOtlp(spans: Span[], resource: Record<string, AttrValue>): Record<string, unknown> {
  return {
    resourceSpans: [{
      resource: { attributes: Object.entries(resource).map(([key, v]) => ({ key, value: otlpValue(v) })) },
      scopeSpans: [{
        scope: UPLOADER,
        spans: spans.map((s) => {
          const start = nanos(s.start);
          const end = BigInt(nanos(s.end)) < BigInt(start) ? start : nanos(s.end);
          return {
            traceId: s.traceId, spanId: s.spanId, ...(s.parentSpanId ? { parentSpanId: s.parentSpanId } : {}),
            name: s.name, kind: 1, startTimeUnixNano: start, endTimeUnixNano: end,
            attributes: Object.entries(s.attributes).map(([key, v]) => ({ key, value: otlpValue(v) })),
            status: { code: s.error ? 2 : 1 },
          };
        }),
      }],
    }],
  };
}

export function langfuseAuth(env: NodeJS.ProcessEnv = process.env): { endpoint: string; headers: Record<string, string> } {
  const pk = env.LANGFUSE_PUBLIC_KEY, sk = env.LANGFUSE_SECRET_KEY;
  if (!pk || !sk) throw new Error("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required");
  const base = env.OTEL_EXPORTER_OTLP_ENDPOINT?.replace(/\/+$/, "")
    ?? (env.LANGFUSE_BASE_URL ? `${env.LANGFUSE_BASE_URL.replace(/\/+$/, "")}/api/public/otel` : undefined);
  if (!base) throw new Error("LANGFUSE_BASE_URL or OTEL_EXPORTER_OTLP_ENDPOINT is required");
  return {
    endpoint: `${base}/v1/traces`,
    headers: {
      Authorization: `Basic ${Buffer.from(`${pk}:${sk}`).toString("base64")}`,
      "Content-Type": "application/json",
      "x-langfuse-ingestion-version": "4",
    },
  };
}

export async function post(spans: Span[], resource: Record<string, AttrValue>, batchSize = 200): Promise<number[]> {
  const { endpoint, headers } = langfuseAuth();
  const statuses: number[] = [];
  for (let i = 0; i < spans.length; i += batchSize) {
    const body = JSON.stringify(toOtlp(spans.slice(i, i + batchSize), resource));
    let status = 0;
    for (let attempt = 0; attempt < 3 && !(status >= 200 && status < 300); attempt++) {
      if (attempt) await new Promise((r) => setTimeout(r, 1000 * attempt));
      try { status = (await fetch(endpoint, { method: "POST", headers, body })).status; } catch { status = -1; }
    }
    statuses.push(status);
  }
  return statuses;
}

/** Observation names grouped as the coverage check counts them ("Tool: *", "Subagent: *"). */
export const nameCategory = (name: string) =>
  name.startsWith("Tool: ") ? "Tool: *" : name.startsWith("Subagent: ") ? "Subagent: *" : name;

export function nameCounts(names: string[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const n of names) counts[nameCategory(n)] = (counts[nameCategory(n)] ?? 0) + 1;
  return counts;
}

// --- CLI ----------------------------------------------------------------------------------- //

function parseCli(argv: string[]) {
  const o = { transcript: "", subagents: "", sessionId: "", tags: [] as string[], meta: {} as Record<string, string>,
    environment: process.env.LANGFUSE_TRACING_ENVIRONMENT ?? "", maxChars: Number(process.env.CC_LANGFUSE_MAX_CHARS ?? 20000),
    dryRun: false, out: "" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!, v = () => { const x = argv[++i]; if (x === undefined) throw new Error(`${a} needs a value`); return x; };
    if (a === "--transcript") o.transcript = v();
    else if (a === "--subagents") o.subagents = v();
    else if (a === "--session-id") o.sessionId = v();
    else if (a === "--tag") o.tags.push(v());
    else if (a === "--meta") { const kv = v(), eq = kv.indexOf("="); if (eq < 1) throw new Error(`--meta needs K=V, got ${kv}`); o.meta[kv.slice(0, eq)] = kv.slice(eq + 1); }
    else if (a === "--environment") o.environment = v();
    else if (a === "--max-chars") o.maxChars = Number(v());
    else if (a === "--dry-run") o.dryRun = true;
    else if (a === "--out") o.out = v();
    else throw new Error(`unknown argument: ${a}`);
  }
  if (!o.transcript) throw new Error("--transcript is required");
  if (!Number.isFinite(o.maxChars) || o.maxChars < 100) throw new Error("--max-chars must be a number >= 100");
  return o;
}

async function main() {
  const o = parseCli(process.argv.slice(2));
  const rows = readJsonl(resolve(o.transcript));
  const sessionId = o.sessionId || rows.find((r) => r.sessionId)?.sessionId;
  if (!sessionId) throw new Error("no --session-id and none found in the transcript");
  const { spans, counts } = buildSpans(rows, {
    sessionId, maxChars: o.maxChars, tags: o.tags, traceMetadata: o.meta, environment: o.environment || undefined,
    subagents: loadSubagents(o.subagents ? resolve(o.subagents) : undefined),
  });
  const resource: Record<string, AttrValue> = { "service.name": "claude-code-transcript", "service.version": UPLOADER.version };
  if (o.environment) resource["deployment.environment.name"] = o.environment;
  if (o.out) writeFileSync(o.out, JSON.stringify(toOtlp(spans, resource)));
  const statuses = o.dryRun ? [] : await post(spans, resource);
  const ok = o.dryRun || statuses.every((s) => s >= 200 && s < 300);
  const traces = [...new Set(spans.map((s) => s.traceId))];
  process.stdout.write(JSON.stringify({
    schema: "geeky-eval.transcript-upload/1", uploader: UPLOADER, session_id: sessionId, dry_run: o.dryRun, ok,
    counts: { ...counts, spans: spans.length, traces: traces.length },
    expected_names: nameCounts(spans.map((s) => s.name)),
    batches: statuses, trace_ids: traces,
  }, null, 2) + "\n");
  process.exitCode = ok ? 0 : 1;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch((e) => { process.stderr.write(`transcript-otlp: ${e instanceof Error ? e.message : e}\n`); process.exitCode = 2; });
}
