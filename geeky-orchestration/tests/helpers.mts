// Shared fixtures for the Node mirror tests.

import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

export interface RunResult { status: number | null; stdout: string; stderr: string }

/** Run a plugin script (path relative to the plugin root) the way a host would. */
export function runScript(rel: string, args: string[] = [], input?: string, env?: NodeJS.ProcessEnv): RunResult {
  const res = spawnSync(process.execPath, ["--disable-warning=ExperimentalWarning", join(ROOT, rel), ...args], {
    input: input ?? "",
    encoding: "utf8",
    env: env ?? process.env,
  });
  return { status: res.status, stdout: res.stdout, stderr: res.stderr };
}

/** A temp folder holding `files` (relative path -> content). */
export function makeFolder(files: Record<string, string>): string {
  const root = mkdtempSync(join(tmpdir(), "geekytest"));
  for (const [rel, body] of Object.entries(files)) {
    mkdirSync(dirname(join(root, rel)), { recursive: true });
    writeFileSync(join(root, rel), body, "utf8");
  }
  return root;
}

export const LANES = ["Backlog", "Ready", "In Progress", "In Review", "Blocked", "Done"];

/** kanban.md with every lane heading; `placements` maps lane -> bullet lines. */
export function kanban(placements: Record<string, string[]> = {}): string {
  return ["# Kanban", ...LANES.flatMap((lane) => [`## ${lane}`, ...(placements[lane] ?? [])])].join("\n") + "\n";
}

export const TASK_BODY = [
  "# T1 - Example task", "", "## Task Name", "Example", "", "## Context", "x", "", "## Module/System", "x", "",
  "## In scope", "- x", "", "## Dependencies", "None.", "", "## Acceptance Criteria", "- x", "",
  "## Technical Notes", "x", "", "## Tests/Validation Before Next Task", "- npm test", "- npm run lint", "",
  "## Definition of Done", "- x", "", "## Estimate", "S", "", "## Priority", "P2", "",
].join("\n");

/** A planning folder that passes every gate for T1. */
export function completeFolder(extra: Record<string, string> = {}): string {
  return makeFolder({
    "tasks/T1-example.md": TASK_BODY,
    "tasks/T1-example.notes.md": "# T1 notes\n",
    "kanban.md": kanban({ Done: ["- T1 example"] }),
    "implementation-plan.md": "# Plan\n",
    "references.md": "# Refs\n",
    "handoff.md": "# Handoff\n\nT1 is done.\n",
    "feature-specification.md": "# Spec\n",
    "draft.md": "# Draft\n",
    ...extra,
  });
}
