// evals/telemetry: transcript -> Langfuse OTLP, coverage and scores (no network).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { buildScores, compareCoverage } from "../evals/telemetry/langfuse-check.mts";
import { buildSpans, buildTurns, loadSubagents, nameCounts, toOtlp, truncateDeep, usageDetails } from "../evals/telemetry/transcript-otlp.mts";
import { ROOT } from "./helpers.mts";

const t = (s: number) => new Date(Date.UTC(2026, 9, 30, 4, 0, s)).toISOString();
const asst = (id: string, s: number, content: unknown[], usage?: object, extra: object = {}) =>
  ({ type: "assistant", timestamp: t(s), requestedModel: "claude-sonnet-5-5", message: { id, role: "assistant", model: "claude-sonnet-5-5", content, ...(usage ? { usage } : {}), stop_reason: "tool_use" }, ...extra });
const user = (s: number, content: unknown, extra: object = {}) => ({ type: "user", timestamp: t(s), message: { role: "user", content }, ...extra });

/** Two turns; the first spawns a subagent through Agent and the subagent's Bash fails. */
function fixture() {
  const dir = mkdtempSync(join(tmpdir(), "geekytel"));
  const usage = { input_tokens: 5, output_tokens: 40, cache_read_input_tokens: 100, cache_creation: { ephemeral_5m_input_tokens: 7 } };
  const main = [
    { type: "queue-operation", timestamp: t(0) },
    user(1, "Build wordstat."),
    { type: "attachment", timestamp: t(1) },
    asst("m1", 2, [{ type: "thinking", thinking: "plan first" }], usage, { effort: "high", thinkingDurationMs: 900 }),
    asst("m1", 3, [{ type: "tool_use", id: "tu-agent", name: "Agent", input: { description: "write tests", prompt: "go" } }], usage),
    user(9, [{ type: "tool_result", tool_use_id: "tu-agent", content: [{ type: "text", text: "tests written" }] }]),
    asst("m2", 10, [{ type: "text", text: "Done with turn one." }], { input_tokens: 1, output_tokens: 3, cache_creation_input_tokens: 11 }),
    user(20, "Now review it."),
    asst("m3", 21, [{ type: "text", text: "Reviewed." }], { input_tokens: 2, output_tokens: 2 }),
  ];
  const sub = [
    user(4, "go", { isSidechain: true }),
    asst("s1", 5, [{ type: "tool_use", id: "tu-bash", name: "Bash", input: { command: "pytest" } }], { input_tokens: 3, output_tokens: 4 }, { isSidechain: true }),
    user(6, [{ type: "tool_result", tool_use_id: "tu-bash", is_error: true, content: "1 failed" }], { isSidechain: true }),
    asst("s2", 7, [{ type: "text", text: "tests written" }], { input_tokens: 3, output_tokens: 5 }, { isSidechain: true }),
  ];
  writeFileSync(join(dir, "session.jsonl"), main.map((r) => JSON.stringify(r)).join("\n") + "\n{torn");
  mkdirSync(join(dir, "subagents"));
  writeFileSync(join(dir, "subagents", "agent-a1.jsonl"), sub.map((r) => JSON.stringify(r)).join("\n"));
  writeFileSync(join(dir, "subagents", "agent-a1.meta.json"), JSON.stringify({ agentType: "geeky-coder", description: "write tests", toolUseId: "tu-agent", spawnDepth: 1 }));
  return { dir, rows: main as Record<string, unknown>[] };
}

const build = (rows: Record<string, unknown>[], dir: string, maxChars = 20000) =>
  buildSpans(rows, { sessionId: "sess-1", maxChars, tags: ["geeky-eval", "c0-wordstat"], traceMetadata: { case: "c0-wordstat" },
    environment: "development", subagents: loadSubagents(join(dir, "subagents")) });

test("turns, calls and bookkeeping rows: one LLM call per API message id", () => {
  const { rows } = fixture();
  const { turns, results } = buildTurns(rows);
  assert.equal(turns.length, 2);
  assert.deepEqual(turns[0]!.calls.map((c) => c.id), ["m1", "m2"]);
  assert.equal(turns[0]!.calls[0]!.blocks.length, 2, "thinking + tool_use rows share message m1");
  assert.ok(results.has("tu-agent"));
});

test("span tree mirrors the pinned hook: Turn > LLM Call > Tool > Subagent > Subagent LLM Call > Tool", () => {
  const { dir, rows } = fixture();
  const { spans, counts } = build(rows, dir);
  assert.deepEqual(counts, { turns: 2, llm_calls: 3, tools: 2, tool_errors: 1, subagents: 1, subagent_llm_calls: 2 });
  const byId = new Map(spans.map((s) => [s.spanId, s]));
  const chain = (name: string) => { const out: string[] = []; let s = spans.find((x) => x.name === name); while (s) { out.unshift(s.name); s = s.parentSpanId ? byId.get(s.parentSpanId) : undefined; } return out; };
  assert.deepEqual(chain("Tool: Bash"), ["Conversational Turn", "LLM Call", "Tool: Agent", "Subagent: write tests", "Subagent LLM Call", "Tool: Bash"]);
  const bash = spans.find((s) => s.name === "Tool: Bash")!;
  assert.equal(bash.attributes["langfuse.observation.level"], "ERROR");
  assert.equal(bash.attributes["langfuse.observation.type"], "tool");
  assert.deepEqual(nameCounts(spans.map((s) => s.name)),
    { "Conversational Turn": 2, "LLM Call": 3, "Tool: *": 2, "Subagent: *": 1, "Subagent LLM Call": 2 });
});

test("generations carry model, hook-compatible usage, thinking and full content", () => {
  const { dir, rows } = fixture();
  const gen = build(rows, dir).spans.find((s) => s.name === "LLM Call")!;
  assert.equal(gen.attributes["langfuse.observation.type"], "generation");
  assert.equal(gen.attributes["gen_ai.response.model"], "claude-sonnet-5-5");
  assert.deepEqual(JSON.parse(String(gen.attributes["langfuse.observation.usage_details"])),
    { input: 5, output: 40, cache_read_input_tokens: 100, input_cache_creation_5m: 7 });
  const output = JSON.parse(String(gen.attributes["langfuse.observation.output"]));
  assert.deepEqual(output.content.map((b: { type: string }) => b.type), ["thinking", "tool_use"]);
  assert.equal(output.content[0].thinking, "plan first");
  assert.equal(gen.attributes["langfuse.observation.metadata.thinking_duration_ms"], 900);
});

test("usage mapping matches the hook, including the flat cache-write fallback", () => {
  assert.deepEqual(usageDetails({ input_tokens: 1, output_tokens: 3, cache_creation_input_tokens: 11 }),
    { input: 1, output: 3, cache_creation_input_tokens: 11 });
  assert.deepEqual(usageDetails({ input_tokens: 0, output_tokens: 0 }), undefined);
});

test("root spans set the session, trace name, tags and metadata; ids are deterministic", () => {
  const { dir, rows } = fixture();
  const a = build(rows, dir).spans, b = build(rows, dir).spans;
  assert.deepEqual(a.map((s) => s.spanId), b.map((s) => s.spanId));
  const root = a.find((s) => s.name === "Conversational Turn")!;
  assert.equal(root.parentSpanId, undefined);
  assert.equal(root.attributes["langfuse.session.id"], "sess-1");
  assert.deepEqual(root.attributes["langfuse.trace.tags"], ["geeky-eval", "c0-wordstat"]);
  assert.equal(root.attributes["langfuse.trace.metadata.case"], "c0-wordstat");
  assert.match(root.traceId, /^[0-9a-f]{32}$/);
  assert.match(root.spanId, /^[0-9a-f]{16}$/);
});

test("truncation caps strings but keeps JSON valid", () => {
  assert.deepEqual(truncateDeep({ a: "x".repeat(250), b: [1, "ok"] }, 200), { a: "x".repeat(200) + "…[truncated 50 chars]", b: [1, "ok"] });
  const { dir, rows } = fixture();
  for (const s of build(rows, dir, 100).spans) {
    for (const k of ["langfuse.observation.input", "langfuse.observation.output"]) if (k in s.attributes) JSON.parse(String(s.attributes[k]));
  }
});

test("OTLP/JSON shape: hex ids, nanosecond strings, typed attribute values, error status", () => {
  const { dir, rows } = fixture();
  const doc = toOtlp(build(rows, dir).spans, { "service.name": "x" }) as any;
  const spans = doc.resourceSpans[0].scopeSpans[0].spans;
  const bash = spans.find((s: any) => s.name === "Tool: Bash");
  assert.match(bash.startTimeUnixNano, /^\d{19}$/);
  assert.equal(bash.status.code, 2);
  const root = spans.find((s: any) => s.name === "Conversational Turn");
  assert.deepEqual(root.attributes.find((a: any) => a.key === "langfuse.trace.tags").value,
    { arrayValue: { values: [{ stringValue: "geeky-eval" }, { stringValue: "c0-wordstat" }] } });
  assert.deepEqual(root.attributes.find((a: any) => a.key === "langfuse.trace.metadata.turn").value, { intValue: "1" });
});

test("CLI dry-run writes the OTLP document and a summary with expected names", () => {
  const { dir } = fixture();
  const res = spawnSync(process.execPath, ["--disable-warning=ExperimentalWarning", join(ROOT, "evals/telemetry/transcript-otlp.mts"),
    "--transcript", join(dir, "session.jsonl"), "--subagents", join(dir, "subagents"), "--dry-run", "--out", join(dir, "otlp.json"),
    "--session-id", "sess-1"], { encoding: "utf8", env: {} as NodeJS.ProcessEnv });
  assert.equal(res.status, 0, res.stderr);
  const summary = JSON.parse(res.stdout);
  assert.equal(summary.counts.spans, 10);
  assert.equal(summary.expected_names["Subagent LLM Call"], 2);
  assert.equal(JSON.parse(readFileSync(join(dir, "otlp.json"), "utf8")).resourceSpans[0].scopeSpans[0].spans.length, 10);
});

test("coverage compares per category and caps over-delivery", () => {
  assert.deepEqual(compareCoverage({ "LLM Call": 3, "Tool: *": 2 }, { "LLM Call": 3, "Tool: *": 1, "claude_code.tool": 9 }),
    { expected: { "LLM Call": 3, "Tool: *": 2 }, received: { "LLM Call": 3, "Tool: *": 1, "claude_code.tool": 9 },
      missing: { "Tool: *": 1 }, ratio: 0.8, complete: false });
  assert.equal(compareCoverage({ "LLM Call": 2 }, { "LLM Call": 5 }).complete, true);
});

test("scores: typed, session-level, deterministic ids, non-finite values skipped", () => {
  const run = { case: "c0-wordstat", harness: "claude-code", model: "claude-sonnet-5-5", wall_time_s: 120.5, model_calls: 9, commits: 4,
    exit_reason: "completed", reference_eligible: false, tokens: { total: 1000 },
    oracle: { collected: 13, passed: 12, failed: 1, errors: 0, stable_over_3: true } };
  const a = buildScores(run, "sess-1", "development"), b = buildScores(run, "sess-1", "development");
  assert.deepEqual(a.map((s) => s.id), b.map((s) => s.id));
  const byName = Object.fromEntries(a.map((s) => [s.name, s]));
  assert.equal(byName["geeky.oracle.pass_rate"]!.value, 0.9231);
  assert.equal(byName["geeky.oracle.stable"]!.dataType, "BOOLEAN");
  assert.equal(byName["geeky.exit_reason"]!.value, "completed");
  assert.equal(byName["geeky.exit_reason"]!.dataType, "CATEGORICAL");
  assert.ok(a.every((s) => s.sessionId === "sess-1" && s.environment === "development"));
  assert.match(a[0]!.id, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  assert.ok(!buildScores({ ...run, wall_time_s: null }, "s").some((s) => s.name === "geeky.wall_time_s"));
});
