#!/usr/bin/env node
/**
 * guard-planning-contract.mts — Node mirror of guard-planning-contract.py.
 *
 * PreToolUse-style guard for Edit/Write/NotebookEdit. Reads tool-call JSON from
 * stdin. If file_path points at a frozen geeky-plan artifact, it surfaces a message
 * through a PORTABLE channel so it is visible across agent frameworks, not just on
 * Claude Code stderr.
 *
 * Frozen patterns (inside a planning folder under docs/<feature>/):
 *   - implementation-plan.md
 *   - feature-specification.md
 *   - draft.md
 *   - references.md
 *   - tasks/Tx-*.md           (but NOT tasks/Tx-*.notes.md — those are writable)
 *
 * Modes (default warn) via --mode or $GEEKY_GUARD_MODE:
 *   warn   -> advisory only. Writes the message to BOTH stdout (plain text) and
 *             stderr, then exit 0. On exit 0, Claude Code treats non-JSON stdout as
 *             added context; OpenAI Codex treats stdout as developer context. So the
 *             warning actually reaches the model, unlike stderr-on-exit-0 alone.
 *   block  -> emits Claude/Codex-style deny JSON on stdout and exits 0 so the host
 *             parses the decision and blocks the edit. (Frameworks that gate purely
 *             on exit code: re-run with --exit-code so a block returns exit 2.)
 *
 * Exit codes:
 *   0  -> normal (warn, or block-via-JSON which the host parses)
 *   2  -> hard block via exit code (only when --exit-code is passed in block mode)
 *
 * Unlike the .py, a payload that is valid JSON but not the expected shape (an
 * array, a non-string file_path) is ignored with exit 0 rather than a traceback:
 * a guard must never be the reason an edit errors out.
 */

import { isMain, parseArgs, pyDumps, pyOr, pyRe, pyTruthy, readStdin, runMain } from "../scripts/lib/py-compat.mts";

const FROZEN_LEAVES = new Set([
  "implementation-plan.md",
  "feature-specification.md",
  "draft.md",
  "references.md",
]);

const TASK_FILE_RE = pyRe(String.raw`/tasks/T[^/]+\.md$`, "i");
const NOTES_FILE_RE = pyRe(String.raw`\.notes\.md$`, "i");

export function message(filePath: string, reason: string): string {
  return (
    `[geeky-orchestration] ${filePath} ${reason}. ` +
    "Planning artifacts are the frozen contract between geeky-plan and " +
    "geeky-implement. If the plan is genuinely wrong, surface it via kanban " +
    "Blocked + handoff.md rather than editing the contract mid-run."
  );
}

/** Why `filePath` is frozen, or null when it may be edited. */
export function frozenReason(filePath: string): string | null {
  const normalized = filePath.replace(/\\/g, "/");
  const leaf = normalized.slice(normalized.lastIndexOf("/") + 1);
  if (FROZEN_LEAVES.has(leaf)) return "is a frozen geeky-plan planning artifact";
  if (TASK_FILE_RE.test(normalized) && !NOTES_FILE_RE.test(normalized)) {
    return "is a frozen task file (per-task notes belong in tasks/Tx-*.notes.md instead)";
  }
  return null;
}

function emitWarn(msg: string): number {
  // stdout: portable context channel (Claude adds non-JSON stdout as context;
  // Codex treats stdout as developer context). stderr: log visibility.
  process.stdout.write(msg + "\n");
  process.stderr.write(msg + "\n");
  return 0;
}

function emitBlock(msg: string, exitCode: boolean): number {
  if (exitCode) {
    process.stderr.write(msg + "\n");
    return 2;
  }
  const decision = {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: msg,
    },
  };
  process.stdout.write(pyDumps(decision) + "\n");
  return 0;
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

async function main(): Promise<number> {
  const args = parseArgs("guard-planning-contract.mts", [
    // argparse does not check a default against choices, so an unexpected
    // $GEEKY_GUARD_MODE behaves as warn — mirrored here.
    { flag: "--mode", dest: "mode", kind: "string", choices: ["warn", "block"], default: process.env.GEEKY_GUARD_MODE ?? "warn" },
    { flag: "--exit-code", dest: "exit_code", kind: "bool" },
  ], []);

  let raw: string;
  try {
    raw = await readStdin();
  } catch {
    return 0;
  }
  if (!raw) return 0;
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return 0;
  }
  if (!isObject(payload)) return 0;

  const tool = pyOr(payload.tool_name, payload.tool);
  if (!pyTruthy(tool)) return 0;

  const toolInput = pyOr(payload.tool_input, payload.input, {});
  if (!isObject(toolInput)) return 0;
  const filePath = pyOr(toolInput.file_path, toolInput.notebook_path);
  if (!pyTruthy(filePath) || typeof filePath !== "string") return 0;

  const reason = frozenReason(filePath);
  if (reason === null) return 0;

  const msg = message(filePath, reason);
  if (args.mode === "block") return emitBlock(msg, args.exit_code === true);
  return emitWarn(msg);
}

if (isMain(import.meta.url)) runMain(main);
