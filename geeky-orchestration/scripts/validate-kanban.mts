#!/usr/bin/env node
/**
 * validate-kanban.mts — Node mirror of validate-kanban.py.
 *
 * Deterministic integrity check for a geeky-plan kanban.md against the tasks/ folder.
 * Replaces LLM self-report ("I updated the board") with a fail-stop check.
 *
 * Checks (errors fail the run; warnings do not):
 *   ERROR   - a task file in tasks/ is not placed in any lane (untracked)
 *   ERROR   - a task id appears in more than one lane (ambiguous placement)
 *   WARNING - a task id referenced in the kanban has no matching task file (dangling)
 *   WARNING - In Progress count exceeds the WIP cap (default 3)
 *   WARNING - a known lane heading is entirely missing
 *
 * Recognised lanes (case-insensitive, any heading depth): Backlog, Ready,
 * In Progress, In Review, Blocked, Done. A heading containing "validation" is
 * treated as the release-gate checklist, not a task lane.
 *
 * Contract (matches the other validators):
 *   argv in, exit 0 = pass / exit 1 = fail, human summary on stdout.
 *   --json emits a machine-readable object instead of the human summary.
 *
 * Usage:
 *   node validate-kanban.mts --path "/abs/path/to/docs/feature-folder"
 *   node validate-kanban.mts "/abs/path/to/docs/feature-folder" --wip 3 --json
 */

import {
  isDir, isFile, isMain, listdir, parseArgs, print, pyDumps, pyJoin, pyPath, pyRe, pySorted, pyStrip, readText, runMain, splitlines,
} from "./lib/py-compat.mts";

// Ordered so that more specific "In ..." lanes are tested before short names.
export const KNOWN_LANES = ["Backlog", "Ready", "In Progress", "In Review", "Blocked", "Done"] as const;
export type Lane = (typeof KNOWN_LANES)[number];

export const HEADING_RE = pyRe(String.raw`^\s{0,3}#{1,6}\s+(.*\S)\s*$`);
const TASK_ID_RE = pyRe(String.raw`\bT\d+\b`, "ig");
const TASK_FILE_RE = pyRe(String.raw`^T\d+`, "i");
const NOTES_RE = pyRe(String.raw`\.notes\.md$`, "i");

/** Return the canonical lane name for a heading, or null. */
export function laneForHeading(heading: string): Lane | null {
  const norm = pyStrip(heading).toLowerCase();
  if (norm.includes("validation")) return null;
  for (const lane of KNOWN_LANES) {
    if (norm === lane.toLowerCase() || norm.startsWith(lane.toLowerCase())) return lane;
  }
  return null;
}

function taskIdsFromFiles(tasksDir: string): Set<string> {
  const ids = new Set<string>();
  if (!isDir(tasksDir)) return ids;
  for (const name of listdir(tasksDir)) {
    if (!isFile(pyJoin(tasksDir, name))) continue;
    if (NOTES_RE.test(name) || !name.toLowerCase().endsWith(".md")) continue;
    const m = TASK_FILE_RE.exec(name);
    if (m) ids.add(m[0].toUpperCase());
  }
  return ids;
}

/**
 * Map canonical lane -> set of task ids referenced under it, and the set of
 * lane headings actually present in the document.
 */
export function parseKanban(text: string): [Map<Lane, Set<string>>, Set<Lane>] {
  const placements = new Map<Lane, Set<string>>(KNOWN_LANES.map((lane) => [lane, new Set<string>()]));
  const present = new Set<Lane>();
  let current: Lane | null = null;
  for (const line of splitlines(text)) {
    const hm = HEADING_RE.exec(line);
    if (hm) {
      current = laneForHeading(hm[1]!);
      if (current) present.add(current);
      continue;
    }
    if (current === null) continue;
    for (const tid of line.match(TASK_ID_RE) ?? []) placements.get(current)!.add(tid.toUpperCase());
  }
  return [placements, present];
}

export interface KanbanReport {
  ok: boolean;
  errors: string[];
  warnings: string[];
  lane_counts: Record<string, number>;
  summary: string;
}

export function buildReport(folder: string, wipCap: number): KanbanReport {
  const errors: string[] = [];
  const warnings: string[] = [];

  const kanban = pyJoin(folder, "kanban.md");
  if (!isFile(kanban)) {
    return {
      ok: false,
      errors: [`MISSING: ${kanban}`],
      warnings: [],
      lane_counts: {},
      summary: `INVALID_KANBAN: ${kanban} not found.`,
    };
  }

  const fileIds = taskIdsFromFiles(pyJoin(folder, "tasks"));
  const [placements, presentLanes] = parseKanban(readText(kanban));

  // Where does each id live?
  const idLanes = new Map<string, Lane[]>();
  for (const [lane, ids] of placements) {
    for (const tid of ids) {
      if (!idLanes.has(tid)) idLanes.set(tid, []);
      idLanes.get(tid)!.push(lane);
    }
  }
  const sortedIds = pySorted(idLanes.keys());

  // Untracked: a real task file not placed anywhere.
  for (const tid of pySorted(fileIds)) {
    if (!idLanes.has(tid)) errors.push(`${tid} has a task file but is not placed in any kanban lane`);
  }

  // Ambiguous: placed in more than one lane.
  for (const tid of sortedIds) {
    const lanes = idLanes.get(tid)!;
    if (lanes.length > 1) errors.push(`${tid} appears in multiple lanes: ${pySorted(lanes).join(", ")}`);
  }

  // Dangling: referenced in kanban but no task file.
  for (const tid of sortedIds) {
    if (fileIds.size && !fileIds.has(tid)) {
      warnings.push(`${tid} is referenced in the kanban but has no tasks/${tid}-*.md file`);
    }
  }

  // Missing lanes.
  for (const lane of KNOWN_LANES) {
    if (!presentLanes.has(lane)) warnings.push(`lane "${lane}" heading not found in kanban.md`);
  }

  // WIP cap.
  const inProgress = placements.get("In Progress")!.size;
  if (inProgress > wipCap) warnings.push(`In Progress has ${inProgress} tasks (WIP cap ${wipCap})`);

  const laneCounts: Record<string, number> = {};
  for (const [lane, ids] of placements) laneCounts[lane] = ids.size;
  const tracked = new Set([...placements.values()].flatMap((ids) => [...ids])).size;

  const ok = errors.length === 0;
  const summary =
    `${ok ? "OK" : "INVALID_KANBAN"}: ${tracked} task(s) tracked, ` +
    `${fileIds.size} task file(s). ` +
    Object.entries(laneCounts).map(([k, v]) => `${k}=${v}`).join(", ");
  return { ok, errors, warnings, lane_counts: laneCounts, summary };
}

function main(): number {
  const args = parseArgs("validate-kanban.mts", [
    { flag: "--path", dest: "path", kind: "string" },
    { flag: "--wip", dest: "wip", kind: "int", default: 3 },
    { flag: "--json", dest: "as_json", kind: "bool" },
  ], ["positional"]);
  const asJson = args.as_json === true;
  const pathStr = (args.path || args.positional) as string | null;
  if (!pathStr) {
    print("USAGE: validate-kanban.mts --path <folder> [--wip N] [--json]");
    return 1;
  }

  if (!isDir(pathStr)) {
    const msg = `MISSING_FOLDER: ${pyPath(pathStr)}`;
    print(asJson ? pyDumps({ ok: false, errors: [msg] }) : msg);
    return 1;
  }

  const report = buildReport(pathStr, args.wip as number);

  if (asJson) {
    print(pyDumps(report, 2));
  } else {
    print(report.summary);
    for (const e of report.errors) print(`  ERROR:   ${e}`);
    for (const w of report.warnings) print(`  WARNING: ${w}`);
  }

  return report.ok ? 0 : 1;
}

if (isMain(import.meta.url)) runMain(main);
