#!/usr/bin/env node
/**
 * validate-planning-folder.mts — Node mirror of validate-planning-folder.py.
 *
 * Verify that a folder produced by /geeky-plan contains all artifacts /geeky-implement needs.
 *
 * Exit codes (match validate-planning-folder.py and .ps1 exactly):
 *   0  -> OK summary on stdout, folder is valid
 *   1  -> missing-artifact summary on stdout, folder is invalid
 *
 * Usage:
 *   node validate-planning-folder.mts --path "/abs/path/to/docs/feature-folder"
 *   node validate-planning-folder.mts "/abs/path/to/docs/feature-folder"   # positional also supported
 */

import {
  isDir, isFile, isMain, listdir, parseArgs, print, pyDumps, pyJoin, pyPath, pyRe, runMain,
} from "./lib/py-compat.mts";

const REQUIRED = [
  "implementation-plan.md",
  "kanban.md",
  "references.md",
  "handoff.md",
] as const;

const RECOMMENDED = [
  "feature-specification.md",
  "draft.md",
] as const;

// Match the .ps1: tasks/T*-*.md but exclude the *.notes.md siblings.
const TASK_INCLUDE = pyRe(String.raw`^T.*\.md$`, "i");
const TASK_EXCLUDE = pyRe(String.raw`\.notes\.md$`, "i");

export interface PlanningFolderReport {
  ok: boolean;
  task_count: number;
  missing_required: string[];
  missing_recommended: string[];
}

export function checkFolder(folder: string): PlanningFolderReport {
  const missing: string[] = REQUIRED.filter((name) => !isFile(pyJoin(folder, name)));

  const tasksDir = pyJoin(folder, "tasks");
  let taskCount = 0;
  if (!isDir(tasksDir)) {
    missing.push("tasks/ (directory)");
  } else {
    taskCount = listdir(tasksDir).filter(
      (name) => isFile(pyJoin(tasksDir, name)) && TASK_INCLUDE.test(name) && !TASK_EXCLUDE.test(name),
    ).length;
    if (taskCount === 0) missing.push("tasks/Tx-*.md (no task files found)");
  }

  const missingRecommended = RECOMMENDED.filter((name) => !isFile(pyJoin(folder, name)));
  return { ok: missing.length === 0, task_count: taskCount, missing_required: missing, missing_recommended: missingRecommended };
}

function main(): number {
  const args = parseArgs("validate-planning-folder.mts", [
    { flag: "--path", dest: "path", kind: "string" },
    { flag: "--json", dest: "as_json", kind: "bool" },
  ], ["positional"]);
  const asJson = args.as_json === true;
  const pathStr = (args.path || args.positional) as string | null;
  if (!pathStr) {
    const msg = "USAGE: validate-planning-folder.mts --path <folder> [--json]";
    print(asJson ? pyDumps({ ok: false, errors: [msg] }) : msg);
    return 1;
  }

  const folder = pyPath(pathStr);
  if (!isDir(pathStr)) {
    const msg = `MISSING_FOLDER: ${folder}`;
    print(asJson ? pyDumps({ ok: false, errors: [msg] }) : msg);
    return 1;
  }

  const { ok, task_count: taskCount, missing_required: missing, missing_recommended: missingRecommended } = checkFolder(pathStr);
  if (asJson) {
    const summary = ok
      ? `planning folder valid, ${taskCount} task file(s)`
      : `missing ${missing.length} required artifact(s)`;
    print(pyDumps({
      ok,
      task_count: taskCount,
      missing_required: missing,
      missing_recommended: missingRecommended,
      summary,
    }, 2));
    return ok ? 0 : 1;
  }

  if (missing.length) {
    print(`INVALID_PLANNING_FOLDER: ${folder}`);
    print("Missing required artifacts:");
    for (const m of missing) print(`  - ${m}`);
    if (missingRecommended.length) {
      print("Also missing (recommended):");
      for (const m of missingRecommended) print(`  - ${m}`);
    }
    return 1;
  }

  const warningPart = missingRecommended.length ? ` (warnings: missing ${missingRecommended.join(", ")})` : "";
  print(`OK: planning folder valid. ${taskCount} task file(s) found.${warningPart}`);
  return 0;
}

if (isMain(import.meta.url)) runMain(main);
