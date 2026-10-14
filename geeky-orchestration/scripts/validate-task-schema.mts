#!/usr/bin/env node
/**
 * validate-task-schema.mts — Node mirror of validate-task-schema.py.
 *
 * Validate that geeky-plan task files (tasks/Tx-*.md) carry the sections the
 * template defines and that geeky-implement relies on. This is the plan->implement
 * boundary gate: run it at the end of geeky-plan and the start of geeky-implement.
 *
 * Required sections (missing => ERROR, file fails):
 *   Task Name, Scope / In scope, Dependencies, Acceptance Criteria,
 *   Tests/Validation Before Next Task, Priority
 * Recommended sections (missing => WARNING):
 *   Context, Module/System, Technical Notes, Definition of Done, Estimate
 *
 * Matching is tolerant: a label counts as present if any line, after stripping
 * leading '#'/'*' markers and whitespace, begins with the label (case-insensitive).
 *
 * Contract (matches the other validators):
 *   argv in, exit 0 = pass / exit 1 = fail, human summary on stdout.
 *   --json emits a machine-readable object.
 *
 * Usage:
 *   node validate-task-schema.mts --path "/abs/path/to/docs/feature-folder"
 *   node validate-task-schema.mts --file "/abs/path/to/tasks/T1-foo.md" --json
 */

import { basename } from "node:path";
import {
  isDir, isFile, isMain, listdir, parseArgs, print, pyDumps, pyJoin, pyPath, pyRe, pySorted, readText, runMain,
} from "./lib/py-compat.mts";

const REQUIRED: readonly (readonly [string, string])[] = [
  ["Task Name", String.raw`Task\s*Name`],
  ["In scope", String.raw`In\s*scope`],
  ["Dependencies", String.raw`Dependencies`],
  ["Acceptance Criteria", String.raw`Acceptance\s*Criteria`],
  ["Tests/Validation", String.raw`Tests\s*/\s*Validation`],
  ["Priority", String.raw`Priority`],
];
const RECOMMENDED: readonly (readonly [string, string])[] = [
  ["Context", String.raw`Context`],
  ["Module/System", String.raw`Module\s*/\s*System`],
  ["Technical Notes", String.raw`Technical\s*Notes`],
  ["Definition of Done", String.raw`Definition\s+of\s+Done`],
  ["Estimate", String.raw`Estimate`],
];

const NOTES_RE = pyRe(String.raw`\.notes\.md$`, "i");

export interface TaskFileResult {
  file: string;
  ok: boolean;
  missing_required: string[];
  missing_recommended: string[];
}

export function labelPresent(text: string, pattern: string): boolean {
  return pyRe(String.raw`^\s*[#*\s]*${pattern}\b`, "im").test(text);
}

export function checkFile(path: string): TaskFileResult {
  const text = readText(path);
  const missingRequired = REQUIRED.filter(([, pat]) => !labelPresent(text, pat)).map(([name]) => name);
  const missingRecommended = RECOMMENDED.filter(([, pat]) => !labelPresent(text, pat)).map(([name]) => name);
  return {
    file: basename(path),
    ok: missingRequired.length === 0,
    missing_required: missingRequired,
    missing_recommended: missingRecommended,
  };
}

export function collectTaskFiles(folder: string): string[] {
  const tasksDir = pyJoin(pyPath(folder), "tasks");
  if (!isDir(tasksDir)) return [];
  const names = listdir(tasksDir).filter((name) => isFile(pyJoin(tasksDir, name))
    && name.toLowerCase().endsWith(".md") && !NOTES_RE.test(name) && name.slice(0, 1).toUpperCase() === "T");
  return pySorted(names).map((name) => pyJoin(tasksDir, name));
}

function main(): number {
  const args = parseArgs("validate-task-schema.mts", [
    { flag: "--path", dest: "path", kind: "string" },
    { flag: "--file", dest: "file", kind: "string" },
    { flag: "--json", dest: "as_json", kind: "bool" },
  ], ["positional"]);
  const asJson = args.as_json === true;

  let files: string[];
  if (args.file) {
    files = [pyPath(args.file as string)];
  } else {
    const pathStr = (args.path || args.positional) as string | null;
    if (!pathStr) {
      print("USAGE: validate-task-schema.mts (--path <folder> | --file <task.md>) [--json]");
      return 1;
    }
    if (!isDir(pathStr)) {
      const msg = `MISSING_FOLDER: ${pyPath(pathStr)}`;
      print(asJson ? pyDumps({ ok: false, errors: [msg] }) : msg);
      return 1;
    }
    files = collectTaskFiles(pathStr);
  }

  if (files.length === 0) {
    const msg = "NO_TASK_FILES: nothing to validate (expected tasks/Tx-*.md)";
    print(asJson ? pyDumps({ ok: false, errors: [msg] }) : msg);
    return 1;
  }

  const results: TaskFileResult[] = files.map((f) => isFile(f)
    ? checkFile(f)
    : { file: f, ok: false, missing_required: ["<file not found>"], missing_recommended: [] });

  const ok = results.every((r) => r.ok);
  const failed = results.filter((r) => !r.ok).length;

  if (asJson) {
    print(pyDumps({ ok, results }, 2));
  } else {
    const prefix = ok ? "OK" : "INVALID_TASK_SCHEMA";
    print(`${prefix}: ${results.length} task file(s) checked, ${failed} failing.`);
    for (const r of results) {
      if (r.missing_required.length) {
        print(`  ${r.file}:`);
        for (const m of r.missing_required) print(`    ERROR:   missing required section: ${m}`);
      }
      for (const m of r.missing_recommended) print(`    WARNING: ${r.file} missing recommended section: ${m}`);
    }
  }

  return ok ? 0 : 1;
}

if (isMain(import.meta.url)) runMain(main);
