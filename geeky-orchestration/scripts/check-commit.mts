#!/usr/bin/env node
/**
 * check-commit.mts — Node mirror of check-commit.py.
 *
 * Validate a commit message against the geeky-implement format: a Conventional
 * Commits subject plus a task reference (Tasks: T<n> ...). The most natively
 * portable gate — wire it as a git commit-msg hook, a pre-commit check, or a
 * Claude PreToolUse matcher on `Bash(git commit *)`.
 *
 * Rules (violations => ERROR, exit 1):
 *   - subject matches  type(scope)!: summary  where type is one of
 *     feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert
 *     (scope optional; '!' optional)
 *   - subject length <= 72 chars
 *   - a task reference exists somewhere in the message: a "Tasks:" line, or a
 *     bare T<number> token (warn-only if only a bare token, error if neither).
 *
 * Message source (first that is provided):
 *   --message "..."
 *   positional FILE  (git commit-msg passes the path to .git/COMMIT_EDITMSG)
 *   stdin
 *
 * Contract: exit 0 = pass / exit 1 = fail, human summary on stdout. --json optional.
 *
 * Usage:
 *   node check-commit.mts .git/COMMIT_EDITMSG
 *   node check-commit.mts --message "feat(plan): add linter\n\nTasks: T3"
 *   echo "$msg" | node check-commit.mts --json
 */

import {
  isFile, isMain, parseArgs, print, pyDumps, pyLen, pyRe, pyRepr, pyStrip, readStdin, readText, runMain, splitlines,
} from "./lib/py-compat.mts";

const TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert";
const SUBJECT_RE = pyRe(String.raw`^(?:${TYPES})(?:\([^)]+\))?!?: .+`);
const TASKS_LINE_RE = pyRe(String.raw`^\s*Tasks:\s*.*\bT\d+\b`, "im");
const BARE_TASK_RE = pyRe(String.raw`\bT\d+\b`);
const COMMENT_RE = pyRe(String.raw`^\s*#`);
const MAX_SUBJECT = 72;

export interface CommitReport {
  ok: boolean;
  subject: string;
  errors: string[];
  warnings: string[];
}

export function check(message: string): CommitReport {
  // Ignore comment lines (git strips '#' lines) and blank leading lines.
  const lines = splitlines(message).filter((ln) => !COMMENT_RE.test(ln));
  const subject = lines.find((ln) => pyStrip(ln) !== "") ?? "";
  const body = lines.join("\n");

  const errors: string[] = [];
  const warnings: string[] = [];

  if (!SUBJECT_RE.test(subject)) {
    errors.push(`subject is not Conventional Commits "type(scope): summary": ${pyRepr(subject)}`);
  }
  if (pyLen(subject) > MAX_SUBJECT) errors.push(`subject exceeds ${MAX_SUBJECT} chars (${pyLen(subject)})`);

  if (TASKS_LINE_RE.test(body)) {
    // a Tasks: line is the recommended form
  } else if (BARE_TASK_RE.test(body)) {
    warnings.push('task referenced but no "Tasks: T<n>" line (recommended)');
  } else {
    errors.push("no task reference (expected a Tasks: line with T<n>)");
  }

  return { ok: errors.length === 0, subject, errors, warnings };
}

async function readMessage(args: Record<string, unknown>): Promise<string | null> {
  if (args.message !== null) return args.message as string;
  if (args.file) {
    const path = args.file as string;
    return isFile(path) ? readText(path) : null;
  }
  const data = await readStdin();
  return data ? data : null;
}

async function main(): Promise<number> {
  const args = parseArgs("check-commit.mts", [
    { flag: "--message", dest: "message", kind: "string", default: null },
    { flag: "--json", dest: "as_json", kind: "bool" },
  ], ["file"]);
  const asJson = args.as_json === true;
  const message = await readMessage(args);
  if (message === null) {
    const msg = "NO_MESSAGE: provide a file path, --message, or pipe via stdin";
    print(asJson ? pyDumps({ ok: false, errors: [msg] }) : msg);
    return 1;
  }

  const report = check(message);
  if (asJson) {
    print(pyDumps(report, 2));
  } else {
    print(report.ok ? "OK: commit message valid." : "INVALID_COMMIT_MESSAGE:");
    for (const e of report.errors) print(`  ERROR:   ${e}`);
    for (const w of report.warnings) print(`  WARNING: ${w}`);
  }
  return report.ok ? 0 : 1;
}

if (isMain(import.meta.url)) runMain(main);
