// guard-planning-contract.mts: the PreToolUse frozen-artifact guard.

import assert from "node:assert/strict";
import { test } from "node:test";
import { frozenReason } from "../hooks/guard-planning-contract.mts";
import { runScript } from "./helpers.mts";

const GUARD = "hooks/guard-planning-contract.mts";
const edit = (filePath: string, key = "file_path") => JSON.stringify({ tool_name: "Edit", tool_input: { [key]: filePath } });
const MSG = (p: string, reason: string) =>
  `[geeky-orchestration] ${p} ${reason}. Planning artifacts are the frozen contract between geeky-plan and ` +
  "geeky-implement. If the plan is genuinely wrong, surface it via kanban Blocked + handoff.md rather than editing the contract mid-run.";
const TASK_REASON = "is a frozen task file (per-task notes belong in tasks/Tx-*.notes.md instead)";

test("frozen leaves and task bodies are frozen; notes and other files are not", () => {
  assert.equal(frozenReason("/r/docs/f/implementation-plan.md"), "is a frozen geeky-plan planning artifact");
  assert.equal(frozenReason("C:\\r\\docs\\f\\tasks\\T3-x.md"), TASK_REASON);
  assert.equal(frozenReason("/r/docs/f/tasks/T3-x.notes.md"), null);
  assert.equal(frozenReason("/r/docs/f/kanban.md"), null);
  assert.equal(frozenReason("/r/src/tasks.md"), null);
});

test("warn mode: message on stdout and stderr, exit 0", () => {
  const res = runScript(GUARD, ["--mode", "warn"], edit("/r/tasks/T1-a.md"));
  assert.equal(res.status, 0);
  assert.equal(res.stdout, MSG("/r/tasks/T1-a.md", TASK_REASON) + "\n");
  assert.equal(res.stderr, res.stdout);
});

test("block mode: deny JSON in json.dumps form, exit 0", () => {
  const res = runScript(GUARD, ["--mode", "block"], edit("/r/draft.md", "notebook_path"));
  assert.equal(res.status, 0);
  const reason = MSG("/r/draft.md", "is a frozen geeky-plan planning artifact");
  assert.equal(res.stdout,
    `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": ${JSON.stringify(reason)}}}\n`);
});

test("block --exit-code: exit 2 with the reason on stderr", () => {
  const res = runScript(GUARD, ["--mode", "block", "--exit-code"], edit("/r/references.md"));
  assert.equal(res.status, 2);
  assert.equal(res.stdout, "");
  assert.equal(res.stderr, MSG("/r/references.md", "is a frozen geeky-plan planning artifact") + "\n");
});

test("$GEEKY_GUARD_MODE selects the default mode", () => {
  const res = runScript(GUARD, [], edit("/r/draft.md"), { ...process.env, GEEKY_GUARD_MODE: "block" });
  assert.match(res.stdout, /"permissionDecision": "deny"/);
});

test("anything unexpected passes through silently with exit 0", () => {
  for (const input of ["", "not json", "[1,2]", '{"tool_input":{"file_path":"/r/draft.md"}}',
    '{"tool_name":"Edit","tool_input":{"file_path":7}}', edit("/r/src/app.ts")]) {
    const res = runScript(GUARD, ["--mode", "block", "--exit-code"], input);
    assert.deepEqual([res.status, res.stdout, res.stderr], [0, "", ""], `input: ${input}`);
  }
});

test("an empty tool_input falls back to input, as Python's `or` does", () => {
  const payload = JSON.stringify({ tool: "Write", tool_input: {}, input: { file_path: "/r/draft.md" } });
  assert.equal(runScript(GUARD, ["--mode", "block", "--exit-code"], payload).status, 2);
});
