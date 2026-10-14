#!/usr/bin/env node
/**
 * wire-baseline.mts — Node mirror of wire_baseline.py, the A/B wire harness.
 *
 * Drives a geeky_mcp stdio server over line-delimited JSON-RPC against a
 * throwaway planning folder and produces the same *normalised* transcript of
 * `initialize`, `tools/list`, and one `tools/call` per tool. Pointed at the Node
 * server.mts, its output must equal the committed wire_baseline.json (captured
 * from server.py) byte for byte — that equality is the parity proof between the
 * two servers, and tests/mcp-server.test.mts asserts it.
 *
 * Normalisation is identical to the Python harness: serverInfo is dropped (the
 * one field allowed to differ) and printed to stderr instead; every spelling of
 * the fixture path becomes the literal <FIXTURE>; keys are sorted recursively.
 *
 * Run (from this directory):
 *
 *   node wire-baseline.mts --out wire_baseline.node.json
 *   node wire-baseline.mts --out /dev/stdout --server server.mts
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { isMain, parseArgs, runMain } from "../scripts/lib/py-compat.mts";

const HERE = dirname(fileURLToPath(import.meta.url));
export const SERVER = join(HERE, "server.mts");
export const BASELINE = join(HERE, "wire_baseline.json");

const FIXTURE = "<FIXTURE>";
const PROTOCOL_VERSION = "2025-11-25";
const READ_TIMEOUT_MS = 120_000;

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

// Fixed order: the transcript must not depend on object iteration order.
const TOOL_CALLS: [string, Record<string, Json>][] = [
  ["geeky_check_commit", { message: "feat(mcp): capture wire baseline\n\nTasks: T1" }],
  ["geeky_check_dod", { folder: null, task: "T1" }],
  ["geeky_check_frozen_artifact", { file_path: null }],
  ["geeky_validate_kanban", { folder: null, wip: 3 }],
  ["geeky_validate_planning_folder", { folder: null }],
  ["geeky_validate_task_schema", { folder: null }],
];

// --------------------------------------------------------------------------- //
// Fixture                                                                     //
// --------------------------------------------------------------------------- //
const TASK_BODY = `# T1 - Example task

## Task Name
Example task

## Context
Fixture task used by the wire baseline harness.

## Module/System
geeky_mcp

## In scope
- Exercise the validators with a well-formed task file.

## Dependencies
None.

## Acceptance Criteria
- The validators return a clean report.

## Technical Notes
Nothing noteworthy.

## Tests/Validation Before Next Task
- python -m pytest -q

## Definition of Done
- Acceptance criteria met.

## Estimate
S

## Priority
P2
`;

const KANBAN_BODY = `# Kanban

## Backlog

## Ready

## In Progress

## In Review

## Blocked

## Done
- T1 example task
`;

/** Write the smallest planning folder that makes all six validators pass. */
export function buildFixture(root: string): void {
  const tasks = join(root, "tasks");
  mkdirSync(tasks);
  const write = (path: string, body: string) => writeFileSync(path, body, "utf8");
  write(join(tasks, "T1-example.md"), TASK_BODY);
  write(join(tasks, "T1-example.notes.md"), "# T1 notes\n\nDone.\n");
  write(join(root, "kanban.md"), KANBAN_BODY);
  write(join(root, "implementation-plan.md"), "# Plan\n\n- T1\n");
  write(join(root, "references.md"), "# References\n");
  write(join(root, "handoff.md"), "# Handoff\n\nT1 is done.\n");
  write(join(root, "feature-specification.md"), "# Spec\n");
  write(join(root, "draft.md"), "# Draft\n");
}

// --------------------------------------------------------------------------- //
// Normalisation                                                               //
// --------------------------------------------------------------------------- //

/** Every spelling of the fixture path a validator might echo back. */
export function pathVariants(root: string): string[] {
  const spellings = new Set<string>();
  // macOS hands out /var/... temp dirs whose real path is /private/var/...
  for (const base of new Set([root, realpathSync(root)])) {
    const native = base.replace(/\//g, "\\");
    spellings.add(base);
    spellings.add(base.replace(/\\/g, "/"));
    spellings.add(native);
    // content[].text carries the result re-serialised as JSON inside a string,
    // so every separator in it is a doubled backslash.
    spellings.add(native.replace(/\\/g, "\\\\"));
  }
  // Longest first: the doubled-backslash spelling must win over the single.
  return [...spellings].sort((a, b) => b.length - a.length);
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export function normalize(obj: Json, variants: string[]): Json {
  if (Array.isArray(obj)) return obj.map((v) => normalize(v, variants));
  if (obj !== null && typeof obj === "object") {
    return Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, normalize(v, variants)]));
  }
  if (typeof obj === "string") {
    let text = obj;
    for (const spelling of variants) text = text.replace(new RegExp(escapeRe(spelling), "gi"), FIXTURE);
    // Only inside rewritten paths — elsewhere a backslash may be content.
    if (text.includes(FIXTURE)) text = text.replace(/\\/g, "/");
    return text;
  }
  return obj;
}

const LEAK_PATTERNS = [/[A-Za-z]:[\\/]/, /\/tmp\//, /geekywire[A-Za-z0-9_]*/];

/** Walk the normalised document and refuse to write a leaky baseline. */
export function assertNoLeaks(obj: Json, where = "$"): void {
  if (Array.isArray(obj)) { obj.forEach((v, i) => assertNoLeaks(v, `${where}[${i}]`)); return; }
  if (obj !== null && typeof obj === "object") {
    for (const [k, v] of Object.entries(obj)) assertNoLeaks(v, `${where}.${k}`);
    return;
  }
  if (typeof obj !== "string") return;
  for (const pattern of LEAK_PATTERNS) {
    const found = pattern.exec(obj);
    if (found) {
      throw new Error(
        `wire-baseline: absolute or temporary path leaked into the baseline at ${where} near ` +
        `${JSON.stringify(found[0])}; fix normalisation rather than committing a document that cannot reproduce.`,
      );
    }
  }
}

function sortKeys(obj: Json): Json {
  if (Array.isArray(obj)) return obj.map(sortKeys);
  if (obj !== null && typeof obj === "object") {
    return Object.fromEntries(Object.keys(obj).sort().map((k) => [k, sortKeys(obj[k]!)]));
  }
  return obj;
}

/** json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n". */
export function serialize(doc: Json): string {
  return JSON.stringify(sortKeys(doc), null, 2) + "\n";
}

// --------------------------------------------------------------------------- //
// stdio client                                                                //
// --------------------------------------------------------------------------- //

/** Minimal line-delimited JSON-RPC client — no SDK, on purpose: observe the bytes. */
class StdioClient {
  readonly proc: ChildProcessWithoutNullStreams;
  readonly stderr: string[] = [];
  private buffer = "";
  private waiters = new Map<number, (message: Record<string, Json>) => void>();
  private exited = false;

  constructor(argv: string[]) {
    this.proc = spawn(argv[0]!, argv.slice(1), { cwd: HERE, stdio: ["pipe", "pipe", "pipe"] });
    this.proc.stdout.setEncoding("utf8");
    this.proc.stderr.setEncoding("utf8");
    this.proc.stdout.on("data", (chunk: string) => {
      this.buffer += chunk;
      let newline: number;
      while ((newline = this.buffer.indexOf("\n")) !== -1) {
        const line = this.buffer.slice(0, newline).trim();
        this.buffer = this.buffer.slice(newline + 1);
        if (!line) continue;
        let message: Record<string, Json>;
        try { message = JSON.parse(line); } catch { continue; }   // log noise is not our business
        const id = message.id;
        if (typeof id === "number") this.waiters.get(id)?.(message);
      }
    });
    this.proc.stderr.on("data", (chunk: string) => this.stderr.push(...chunk.split("\n").filter(Boolean)));
    this.proc.on("exit", () => { this.exited = true; for (const w of this.waiters.values()) w({}); });
  }

  send(payload: Record<string, Json>): void {
    this.proc.stdin.write(JSON.stringify(payload) + "\n");
  }

  request(id: number, method: string, params: Record<string, Json>): Promise<Record<string, Json>> {
    return new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`wire-baseline: timed out waiting for ${method}; server stderr:\n${this.stderr.join("\n")}`));
      }, READ_TIMEOUT_MS);
      this.waiters.set(id, (message) => {
        clearTimeout(timer);
        this.waiters.delete(id);
        if (this.exited && !("id" in message)) {
          reject(new Error(`wire-baseline: server exited before answering ${method}; server stderr:\n${this.stderr.join("\n")}`));
        } else if ("error" in message) {
          reject(new Error(`wire-baseline: ${method} returned an error: ${JSON.stringify(message.error)}`));
        } else {
          resolvePromise((message.result ?? {}) as Record<string, Json>);
        }
      });
      this.send({ jsonrpc: "2.0", id, method, params });
    });
  }

  close(): Promise<void> {
    return new Promise((resolveClose) => {
      if (this.exited) { resolveClose(); return; }
      const timer = setTimeout(() => this.proc.kill(), 30_000);
      this.proc.on("exit", () => { clearTimeout(timer); resolveClose(); });
      this.proc.stdin.end();
    });
  }
}

// --------------------------------------------------------------------------- //
// Transcript                                                                  //
// --------------------------------------------------------------------------- //

export interface Capture {
  /** The normalised transcript, ready to serialize. */
  document: Json;
  serverInfo: Json;
  stderr: string[];
}

/** Drive `serverArgv` once against a fresh fixture; return the normalised transcript. */
export async function capture(serverArgv: string[] = [process.execPath, SERVER]): Promise<Capture> {
  const root = mkdtempSync(join(tmpdir(), "geekywire"));
  try {
    buildFixture(root);
    const client = new StdioClient(serverArgv);
    try {
      const init = await client.request(1, "initialize", {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "wire_baseline", version: "1" },
      });
      client.send({ jsonrpc: "2.0", method: "notifications/initialized" });

      const { serverInfo, ...initRest } = init;
      const toolsCall: Record<string, Json> = {};
      const transcript: Record<string, Json> = {
        schema: "geeky_mcp.wire_baseline/1",
        initialize: initRest,
        tools_list: await client.request(2, "tools/list", {}),
        tools_call: toolsCall,
      };

      const frozenTarget = join(root, "tasks", "T1-example.md");
      for (const [offset, [tool, template]] of TOOL_CALLS.entries()) {
        const args: Record<string, Json> = { ...template };
        if ("folder" in args) args.folder = root;
        if ("file_path" in args) args.file_path = frozenTarget;
        toolsCall[tool] = await client.request(3 + offset, "tools/call", { name: tool, arguments: { params: args } });
      }
      const document = normalize(transcript, pathVariants(root));
      assertNoLeaks(document);
      return { document, serverInfo: serverInfo ?? {}, stderr: client.stderr };
    } finally {
      await client.close();
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

async function main(): Promise<number> {
  const args = parseArgs("wire-baseline.mts", [
    { flag: "--out", dest: "out", kind: "string" },
    { flag: "--server", dest: "server", kind: "string", default: SERVER },
  ], []);
  if (!args.out) {
    process.stderr.write("wire-baseline: --out is required\n");
    return 2;
  }
  const { document, serverInfo, stderr } = await capture([process.execPath, resolve(args.server as string)]);
  const out = args.out as string;
  if (out !== "/dev/stdout") mkdirSync(dirname(resolve(out)), { recursive: true });
  writeFileSync(out, serialize(document), "utf8");

  const tools = ((document as Record<string, Json>).tools_list as { tools: { name: string }[] }).tools.map((t) => t.name);
  process.stderr.write(`wire-baseline: wrote ${out}\n`);
  process.stderr.write(`wire-baseline: serverInfo = ${JSON.stringify(sortKeys(serverInfo))}\n`);
  process.stderr.write(`wire-baseline: ${tools.length} tools: ${tools.sort().join(", ")}\n`);
  if (stderr.length) {
    process.stderr.write("wire-baseline: server stderr:\n");
    for (const line of stderr) process.stderr.write(`  ${line}\n`);
  }
  return 0;
}

if (isMain(import.meta.url)) runMain(main);
