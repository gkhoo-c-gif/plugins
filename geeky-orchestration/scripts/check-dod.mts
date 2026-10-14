#!/usr/bin/env node
/**
 * check-dod.mts — Node mirror of check-dod.py.
 *
 * Definition-of-Done checker for a single geeky-implement task. Embodies "verify,
 * don't trust": instead of believing the agent that a task is finished, assert the
 * durable signals on disk before the task may move to Done.
 *
 * Checks for task <ID> in <folder> (missing => ERROR unless noted):
 *   - a per-task notes file tasks/<ID>-*.notes.md exists
 *   - <ID> is placed in the Done lane of kanban.md
 *   - handoff.md mentions <ID>
 *   - WARNING if <ID> still appears in a non-Done lane (In Progress / Blocked / ...)
 *
 * It also extracts and prints the task's "Tests/Validation Before Next Task" block
 * so the orchestrator can re-run those commands itself (this script does not execute
 * them — running is the caller's job, kept framework- and shell-agnostic).
 *
 * Contract: argv in, exit 0 = pass / exit 1 = fail, human summary on stdout.
 *   --json emits a machine-readable object.
 *
 * Usage:
 *   node check-dod.mts --path "/abs/docs/feature" --task T3
 *   node check-dod.mts "/abs/docs/feature" T3 --json
 */

import {
  isDir, isFile, isMain, listdir, parseArgs, print, pyDumps, pyJoin, pyPath, pyRe, pyStrip, readText, reEscape, runMain, splitlines,
} from "./lib/py-compat.mts";
import { HEADING_RE, laneForHeading, type Lane } from "./validate-kanban.mts";

export function lanesForTask(kanbanText: string, taskId: string): Lane[] {
  const found: Lane[] = [];
  let current: Lane | null = null;
  const tidRe = pyRe(String.raw`\b${reEscape(taskId)}\b`, "i");
  for (const line of splitlines(kanbanText)) {
    const hm = HEADING_RE.exec(line);
    if (hm) {
      current = laneForHeading(hm[1]!);
      continue;
    }
    if (current && tidRe.test(line) && !found.includes(current)) found.push(current);
  }
  return found;
}

const VALIDATION_LABEL_RE = pyRe(String.raw`Tests\s*/\s*Validation`, "i");
const NEXT_LABEL_RE = pyRe(String.raw`^\s*[#*\s]*(Estimate|Priority)\b`, "i");
const NEXT_HEADING_RE = pyRe(String.raw`^\s{0,3}#{1,6}\s+\S`);

export function extractValidationBlock(taskText: string): string {
  const lines = splitlines(taskText);
  const start = lines.findIndex((line) => VALIDATION_LABEL_RE.test(line));
  if (start === -1) return "";
  const out: string[] = [];
  for (const line of lines.slice(start + 1)) {
    // Stop at the next section label (Estimate:/Priority:/heading).
    if (NEXT_LABEL_RE.test(line)) break;
    if (NEXT_HEADING_RE.test(line)) break;
    out.push(line);
  }
  return pyStrip(out.join("\n"));
}

export interface DodReport {
  ok: boolean;
  task: string;
  errors: string[];
  warnings: string[];
  lanes: Lane[];
  validation_block: string;
}

export function checkDod(folder: string, taskId: string): DodReport {
  const errors: string[] = [];
  const warnings: string[] = [];
  const idPrefixRe = pyRe(String.raw`^${reEscape(taskId)}\b`, "i");

  // 1. notes file
  const tasksDir = pyJoin(folder, "tasks");
  const taskEntries = isDir(tasksDir) ? listdir(tasksDir).filter((name) => isFile(pyJoin(tasksDir, name))) : [];
  const notes = taskEntries.filter((name) => idPrefixRe.test(name) && name.toLowerCase().endsWith(".notes.md"));
  if (notes.length === 0) errors.push(`no per-task notes file tasks/${taskId}-*.notes.md`);

  // 2/4. kanban placement
  const kanban = pyJoin(folder, "kanban.md");
  let lanes: Lane[] = [];
  if (!isFile(kanban)) {
    errors.push("kanban.md not found");
  } else {
    lanes = lanesForTask(readText(kanban), taskId);
    if (!lanes.includes("Done")) {
      errors.push(`${taskId} is not in the Done lane (found in: ${lanes.join(", ") || "no lane"})`);
    }
    const nonDone = lanes.filter((l) => l !== "Done");
    if (nonDone.length) warnings.push(`${taskId} also appears in: ${nonDone.join(", ")}`);
  }

  // 3. handoff mention
  const handoff = pyJoin(folder, "handoff.md");
  if (!isFile(handoff)) {
    warnings.push("handoff.md not found");
  } else if (!pyRe(String.raw`\b${reEscape(taskId)}\b`, "i").test(readText(handoff))) {
    warnings.push(`handoff.md does not mention ${taskId}`);
  }

  // validation block (informational)
  let validation = "";
  const taskFile = taskEntries.find((name) => idPrefixRe.test(name)
    && name.toLowerCase().endsWith(".md") && !name.toLowerCase().endsWith(".notes.md"));
  if (taskFile !== undefined) validation = extractValidationBlock(readText(pyJoin(tasksDir, taskFile)));

  return { ok: errors.length === 0, task: taskId, errors, warnings, lanes, validation_block: validation };
}

function main(): number {
  const args = parseArgs("check-dod.mts", [
    { flag: "--path", dest: "path", kind: "string" },
    { flag: "--task", dest: "task", kind: "string" },
    { flag: "--json", dest: "as_json", kind: "bool" },
  ], ["pos_path", "pos_task"]);
  const asJson = args.as_json === true;
  const pathStr = (args.path || args.pos_path) as string | null;
  const taskId = ((args.task || args.pos_task || "") as string).toUpperCase();
  if (!pathStr || !taskId) {
    print("USAGE: check-dod.mts --path <folder> --task <ID> [--json]");
    return 1;
  }

  if (!isDir(pathStr)) {
    const msg = `MISSING_FOLDER: ${pyPath(pathStr)}`;
    print(asJson ? pyDumps({ ok: false, errors: [msg] }) : msg);
    return 1;
  }

  const report = checkDod(pathStr, taskId);
  const summary = `${report.ok ? "DOD_OK" : "DOD_INCOMPLETE"}: ${taskId}`;

  if (asJson) {
    print(pyDumps(report, 2));
  } else {
    print(summary);
    for (const e of report.errors) print(`  ERROR:   ${e}`);
    for (const w of report.warnings) print(`  WARNING: ${w}`);
    if (report.validation_block) {
      print("  Re-run this task's validation block (verify, don't trust):");
      for (const line of splitlines(report.validation_block)) print(`    | ${line}`);
    }
  }

  return report.ok ? 0 : 1;
}

if (isMain(import.meta.url)) runMain(main);
