// mcp/server.mts: wire parity with server.py, input validation, JSON-RPC edges,
// and the stdio-transport rule.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { BASELINE, capture, serialize } from "../mcp/wire-baseline.mts";
import { completeFolder, ROOT } from "./helpers.mts";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

/**
 * The committed baseline was captured on Windows, where the frozen-artifact
 * path inside content[].text was a JSON-escaped "\\" separator that
 * normalisation turned into "//". That is platform noise, not server output:
 * collapse it on both sides before comparing.
 */
function canonical(doc: Json): Json {
  if (Array.isArray(doc)) return doc.map(canonical);
  if (doc !== null && typeof doc === "object") return Object.fromEntries(Object.entries(doc).map(([k, v]) => [k, canonical(v)]));
  if (typeof doc === "string" && doc.includes("<FIXTURE>")) return doc.replace(/<FIXTURE>[^"\s]*/g, (p) => p.replace(/\/{2,}/g, "/"));
  return doc;
}

test("wire transcript equals server.py's committed wire_baseline.json", async () => {
  const { document, serverInfo, stderr } = await capture();
  const baseline = readFileSync(BASELINE, "utf8");
  assert.equal(serialize(canonical(document)), serialize(canonical(JSON.parse(baseline))));
  const version = JSON.parse(readFileSync(join(ROOT, ".claude-plugin", "plugin.json"), "utf8")).version;
  assert.deepEqual(serverInfo, { name: "geeky_mcp", version });
  assert.deepEqual(stderr, []);
});

/** A live server session; `call` sends one request and awaits its reply. */
function session() {
  const proc = spawn(process.execPath, ["--disable-warning=ExperimentalWarning", join(ROOT, "mcp", "server.mts")], { stdio: ["pipe", "pipe", "pipe"] });
  const waiters = new Map<unknown, (m: Record<string, Json>) => void>();
  const unsolicited: Record<string, Json>[] = [];
  let buffer = "";
  proc.stdout.setEncoding("utf8");
  proc.stdout.on("data", (chunk: string) => {
    buffer += chunk;
    let nl: number;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      const message = JSON.parse(buffer.slice(0, nl));
      buffer = buffer.slice(nl + 1);
      const waiter = waiters.get(message.id);
      if (waiter) { waiters.delete(message.id); waiter(message); } else unsolicited.push(message);
    }
  });
  let next = 100;
  return {
    unsolicited,
    raw: (line: string) => proc.stdin.write(line + "\n"),
    call(method: string, params?: Record<string, Json>): Promise<Record<string, Json>> {
      const id = next++;
      return new Promise((resolve) => {
        waiters.set(id, resolve);
        proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
      });
    },
    tool(name: string, args: Record<string, Json>): Promise<Record<string, Json>> {
      return this.call("tools/call", { name, arguments: args }).then((m) => m.result as Record<string, Json>);
    },
    close: () => new Promise<void>((resolve) => { proc.on("exit", () => resolve()); proc.stdin.end(); }),
  };
}

const text = (result: Record<string, Json>) => ((result.content as { text: string }[])[0]!).text;

test("input validation mirrors pydantic: errors are tool results with isError", async () => {
  const s = session();
  try {
    const cases: [string, Record<string, Json>, RegExp][] = [
      ["geeky_check_dod", {}, /params\n {2}Field required/],
      ["geeky_check_dod", { params: { folder: "/x", task: " T " } }, /params\.task\n {2}String should have at least 2 characters/],
      ["geeky_check_commit", { params: { message: "x", extra: 1 } }, /params\.extra\n {2}Extra inputs are not permitted/],
      ["geeky_validate_kanban", { params: { folder: "/x", wip: 51 } }, /less than or equal to 50/],
      ["geeky_validate_kanban", { params: { folder: "/x", wip: 2.5 } }, /valid integer/],
      ["geeky_validate_task_schema", { params: {} }, /Value error, provide either 'folder' or 'file'/],
    ];
    for (const [name, args, pattern] of cases) {
      const result = await s.tool(name, args);
      assert.equal(result.isError, true, name);
      assert.match(text(result), new RegExp(`^Error executing tool ${name}: 1 validation error for ${name}Arguments\\n`));
      assert.match(text(result), pattern);
    }
    const unknown = await s.tool("geeky_nope", {});
    assert.deepEqual(unknown, { content: [{ type: "text", text: "Unknown tool: geeky_nope" }], isError: true });
  } finally {
    await s.close();
  }
});

test("tools coerce like pydantic: stripped strings, numeric-string ints, JSON-string params", async () => {
  const folder = completeFolder();
  const s = session();
  try {
    const kanban = await s.tool("geeky_validate_kanban", { params: JSON.stringify({ folder: `  ${folder}  `, wip: "2" }) });
    assert.equal(kanban.isError, false, text(kanban));
    assert.deepEqual((kanban.structuredContent as Record<string, Json>).ok, true);

    const missing = await s.tool("geeky_validate_planning_folder", { params: { folder: folder + "/nope" } });
    assert.deepEqual(missing.structuredContent, { ok: false, error: `folder not found: ${folder}/nope`, exit_code: 1 });

    const failing = await s.tool("geeky_check_commit", { params: { message: "wip" } });
    const report = failing.structuredContent as Record<string, Json>;
    assert.deepEqual([report.exit_code, report.ok], [1, false]);
    assert.equal(Object.keys(report)[0], "exit_code");   // server.py's key order: exit_code, then the report

    const notes = await s.tool("geeky_check_frozen_artifact", { params: { file_path: `${folder}/tasks/T1-example.notes.md` } });
    assert.deepEqual(notes.structuredContent, { frozen: false, file_path: `${folder}/tasks/T1-example.notes.md`, reason: null, exit_code: 0 });
  } finally {
    await s.close();
  }
});

test("JSON-RPC edges: ping, unknown method, notifications, parse errors", async () => {
  const s = session();
  try {
    assert.deepEqual((await s.call("ping")).result, {});
    assert.deepEqual((await s.call("resources/list")).result, { resources: [] });
    assert.deepEqual((await s.call("bogus/method")).error, { code: -32601, message: "Method not found" });
    s.raw(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }));
    s.raw("{not json");
    await s.call("ping");                                  // flushes everything sent before it
    assert.deepEqual(s.unsolicited, [{ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }]);
    const init = await s.call("initialize", { protocolVersion: "1999-01-01", capabilities: {}, clientInfo: { name: "t", version: "1" } });
    assert.equal((init.result as Record<string, Json>).protocolVersion, "2025-11-25");
  } finally {
    await s.close();
  }
});

test("no validator spawn can inherit the server's stdio transport", () => {
  // On stdio, the server's stdin is the host's pipe; a child that inherits it
  // never sees EOF and the tool call hangs (server.py's 0.2.15 bug). The wire
  // test above exercises this live (its client keeps stdin open throughout);
  // this pins the source so a refactor cannot quietly reintroduce it.
  const source = readFileSync(join(ROOT, "mcp", "server.mts"), "utf8");
  const spawns = [...source.matchAll(/\bspawn\(([\s\S]*?)\);/g)];
  assert.equal(spawns.length, 1, "expected exactly one spawn site — this guard has gone stale");
  const call = spawns[0]![1]!;
  assert.match(call, /stdio:\s*\[stdinText === undefined \? "ignore" : "pipe", "pipe", "pipe"\]/);
  assert.doesNotMatch(call, /inherit/);
});

test("pydantic lax coercion: bools and '3.0' are ints; fractional and Unicode-space cases", async () => {
  const folder = completeFolder();
  const s = session();
  try {
    for (const wip of [true, "3.0", 2.0] as Json[]) {
      const r = await s.tool("geeky_validate_kanban", { params: { folder, wip } });
      assert.equal(r.isError, false, `wip=${JSON.stringify(wip)}: ${text(r)}`);
    }
    assert.match(text(await s.tool("geeky_validate_kanban", { params: { folder, wip: 2.5 } })), /got a number with a fractional part/);
    assert.match(text(await s.tool("geeky_validate_kanban", { params: { folder, wip: "x" } })), /unable to parse string as an integer/);
    // \x1c is not Unicode White_Space, so pydantic keeps it: a normal "folder not found" result
    const odd = await s.tool("geeky_validate_planning_folder", { params: { folder: "\x1c" } });
    assert.deepEqual(odd.structuredContent, { ok: false, error: "folder not found: \x1c", exit_code: 1 });
  } finally {
    await s.close();
  }
});

test("spawn failures are reports, and a child that never reads stdin cannot crash the server", async () => {
  const { run } = await import("../mcp/server.mts");
  const { writeFileSync } = await import("node:fs");
  const folder = completeFolder();
  const nul = await run(join(ROOT, "scripts", "check-dod.mts"), ["--path", "a\0b"]);
  assert.equal(nul.ok, false);
  assert.match(String(nul.error), /^failed to run check-dod\.mts: TypeError\(["']/);
  const quitter = join(folder, "quit.mts");
  writeFileSync(quitter, "process.exit(3);\n");
  const res = await run(quitter, [], "x".repeat(5_000_000));
  assert.deepEqual([res.exit_code, res.ok], [3, false]);
});
