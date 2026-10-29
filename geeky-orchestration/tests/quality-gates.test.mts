// The five validator mirrors: the cases test_quality_gates.py covers, plus the
// CLI contract (exit codes, exact stdout) the .py and .ps1 twins share.

import assert from "node:assert/strict";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { check } from "../scripts/check-commit.mts";
import { extractValidationBlock } from "../scripts/check-dod.mts";
import { buildReport } from "../scripts/validate-kanban.mts";
import { completeFolder, kanban, makeFolder, runScript, TASK_BODY } from "./helpers.mts";

// --- ported from test_quality_gates.py -------------------------------------- //

test("check-commit accepts a conventional message with a Tasks line", () => {
  const report = check("feat(plan): add packet validator\n\nTasks: T3");
  assert.equal(report.ok, true);
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.warnings, []);
});

test("check-commit rejects a missing task reference", () => {
  const report = check("fix(plan): repair validator");
  assert.equal(report.ok, false);
  assert.match(report.errors[0]!, /no task reference/);
});

test("validate-kanban reports an untracked task file", () => {
  const folder = makeFolder({ "tasks/T1-example.md": "# T1\n", "kanban.md": kanban() });
  const report = buildReport(folder, 3);
  assert.equal(report.ok, false);
  assert.match(report.errors[0]!, /T1 has a task file but is not placed/);
});

test("validate-kanban accepts a single tracked task", () => {
  const folder = makeFolder({ "tasks/T1-example.md": "# T1\n", "kanban.md": kanban({ Backlog: ["- T1 example"] }) });
  const report = buildReport(folder, 3);
  assert.equal(report.ok, true);
  assert.deepEqual(report.errors, []);
});

// --- check-commit CLI -------------------------------------------------------- //

test("check-commit reads --message, a file, or stdin", () => {
  const msg = "feat(x): y\n\nTasks: T1\n";
  assert.equal(runScript("scripts/check-commit.mts", ["--message", msg]).status, 0);
  const folder = makeFolder({ COMMIT_EDITMSG: msg });
  assert.equal(runScript("scripts/check-commit.mts", [join(folder, "COMMIT_EDITMSG")]).status, 0);
  const res = runScript("scripts/check-commit.mts", [], msg);
  assert.equal(res.status, 0);
  assert.equal(res.stdout, "OK: commit message valid.\n");
});

test("check-commit human output, comments, bare task warning, repr'd subject", () => {
  const res = runScript("scripts/check-commit.mts", [], "# comment\n\nupdate stuff T4\n");
  assert.equal(res.status, 1);
  assert.equal(res.stdout, [
    "INVALID_COMMIT_MESSAGE:",
    `  ERROR:   subject is not Conventional Commits "type(scope): summary": 'update stuff T4'`,
    `  WARNING: task referenced but no "Tasks: T<n>" line (recommended)`,
    "",
  ].join("\n"));
});

test("check-commit counts subject length in code points", () => {
  const subject = "feat(x): " + "\u{1f600}".repeat(63);       // 72 code points, 135 UTF-16 units
  assert.equal(check(subject + "\n\nTasks: T1").ok, true);
  assert.match(check(subject + "!\n\nTasks: T1").errors[0]!, /exceeds 72 chars \(73\)/);
});

test("check-commit: empty stdin is NO_MESSAGE, compact JSON like json.dumps", () => {
  const res = runScript("scripts/check-commit.mts", ["--json"], "");
  assert.equal(res.status, 1);
  assert.equal(res.stdout, `{"ok": false, "errors": ["NO_MESSAGE: provide a file path, --message, or pipe via stdin"]}\n`);
});

// --- validate-kanban CLI ---------------------------------------------------- //

test("validate-kanban: ambiguous, dangling, WIP and missing-lane findings", () => {
  const folder = makeFolder({
    "tasks/T1-a.md": "x", "tasks/T2-b.md": "x", "tasks/T3-c.md": "x", "tasks/T4-d.md": "x", "tasks/T1-a.notes.md": "x",
    "kanban.md": [
      "# Kanban", "## Backlog", "- T9 ghost", "## In Progress", "- T1", "- T2", "- T3", "- t4",
      "### Done (validation checklist)", "- T1 should not count here", "## Done", "- T1",
    ].join("\n"),
  });
  const res = runScript("scripts/validate-kanban.mts", ["--path", folder, "--json"]);
  assert.equal(res.status, 1);
  const report = JSON.parse(res.stdout);
  assert.deepEqual(report.errors, ["T1 appears in multiple lanes: Done, In Progress"]);
  assert.deepEqual(report.warnings, [
    "T9 is referenced in the kanban but has no tasks/T9-*.md file",
    'lane "Ready" heading not found in kanban.md',
    'lane "In Review" heading not found in kanban.md',
    'lane "Blocked" heading not found in kanban.md',
    "In Progress has 4 tasks (WIP cap 3)",
  ]);
  assert.deepEqual(report.lane_counts, { Backlog: 1, Ready: 0, "In Progress": 4, "In Review": 0, Blocked: 0, Done: 1 });
  assert.equal(report.summary,
    "INVALID_KANBAN: 5 task(s) tracked, 4 task file(s). Backlog=1, Ready=0, In Progress=4, In Review=0, Blocked=0, Done=1");
});

test("validate-kanban: --wip raises the cap; human output", () => {
  const folder = makeFolder({ "tasks/T1-a.md": "x", "tasks/T2-b.md": "x", "kanban.md": kanban({ "In Progress": ["- T1", "- T2"] }) });
  const res = runScript("scripts/validate-kanban.mts", [folder, "--wip", "1"]);
  assert.equal(res.status, 0);
  assert.equal(res.stdout, [
    "OK: 2 task(s) tracked, 2 task file(s). Backlog=0, Ready=0, In Progress=2, In Review=0, Blocked=0, Done=0",
    "  WARNING: In Progress has 2 tasks (WIP cap 1)",
    "",
  ].join("\n"));
});

test("validate-kanban: missing kanban.md and missing folder", () => {
  const folder = makeFolder({ "tasks/T1-a.md": "x" });
  const report = JSON.parse(runScript("scripts/validate-kanban.mts", ["--path", folder, "--json"]).stdout);
  assert.deepEqual(report.errors, [`MISSING: ${folder}/kanban.md`]);
  const res = runScript("scripts/validate-kanban.mts", ["--path", folder + "/nope/", "--json"]);
  assert.equal(res.status, 1);
  assert.equal(res.stdout, `{"ok": false, "errors": ["MISSING_FOLDER: ${folder}/nope"]}\n`);
});

test("argparse-style usage errors exit 2 on stderr", () => {
  const bad = runScript("scripts/validate-kanban.mts", ["--path", "x", "--wip", "lots"]);
  assert.equal(bad.status, 2);
  assert.equal(bad.stdout, "");
  assert.match(bad.stderr, /argument --wip: invalid int value: 'lots'/);
  assert.equal(runScript("scripts/validate-kanban.mts", ["--bogus"]).status, 2);
  assert.equal(runScript("scripts/validate-kanban.mts", []).stdout, "USAGE: validate-kanban.mts --path <folder> [--wip N] [--json]\n");
  // unique prefixes are accepted, as argparse's allow_abbrev does
  const folder = completeFolder();
  assert.equal(runScript("scripts/validate-kanban.mts", ["--pa", folder, "--js"]).status, 0);
});

// --- validate-task-schema CLI ----------------------------------------------- //

test("validate-task-schema: complete task passes; gaps are errors and warnings", () => {
  const folder = completeFolder({ "tasks/T2-thin.md": "# T2\n\n**Task Name:** thin\nPriority: P3\n" });
  const res = runScript("scripts/validate-task-schema.mts", ["--path", folder, "--json"]);
  assert.equal(res.status, 1);
  const { results } = JSON.parse(res.stdout);
  assert.deepEqual(results.map((r: { file: string }) => r.file), ["T1-example.md", "T2-thin.md"]);
  assert.equal(results[0].ok, true);
  assert.deepEqual(results[1].missing_required, ["In scope", "Dependencies", "Acceptance Criteria", "Tests/Validation"]);
  assert.deepEqual(results[1].missing_recommended, ["Context", "Module/System", "Technical Notes", "Definition of Done", "Estimate"]);
});

test("validate-task-schema: a BOM is not whitespace, exactly as in Python", () => {
  const folder = makeFolder({ "t.md": "\u{feff}## Task Name\n" + TASK_BODY.replace("## Task Name\n", "") });
  const report = JSON.parse(runScript("scripts/validate-task-schema.mts", ["--file", join(folder, "t.md"), "--json"]).stdout);
  assert.deepEqual(report.results[0].missing_required, ["Task Name"]);
});

test("validate-task-schema: --file not found, no task files, human output", () => {
  const folder = makeFolder({ "x.txt": "" });
  const missing = JSON.parse(runScript("scripts/validate-task-schema.mts", ["--file", folder + "//T9.md", "--json"]).stdout);
  assert.deepEqual(missing.results, [{ file: `${folder}/T9.md`, ok: false, missing_required: ["<file not found>"], missing_recommended: [] }]);
  assert.equal(runScript("scripts/validate-task-schema.mts", [folder]).stdout, "NO_TASK_FILES: nothing to validate (expected tasks/Tx-*.md)\n");

  const thin = makeFolder({ "tasks/T1-a.md": "Task Name\nIn scope\nDependencies\nAcceptance Criteria\nTests / Validation\nPriority\n" });
  const res = runScript("scripts/validate-task-schema.mts", [thin]);
  assert.equal(res.status, 0);
  assert.equal(res.stdout.split("\n")[0], "OK: 1 task file(s) checked, 0 failing.");
  assert.equal(res.stdout.split("\n")[1], "    WARNING: T1-a.md missing recommended section: Context");
});

// --- validate-planning-folder CLI ------------------------------------------- //

test("validate-planning-folder: complete, missing, and recommended-only", () => {
  const ok = runScript("scripts/validate-planning-folder.mts", ["--path", completeFolder(), "--json"]);
  assert.equal(ok.status, 0);
  assert.deepEqual(JSON.parse(ok.stdout), {
    ok: true, task_count: 1, missing_required: [], missing_recommended: [], summary: "planning folder valid, 1 task file(s)",
  });

  const bare = makeFolder({ "kanban.md": "", "tasks/T1-a.notes.md": "" });
  const res = runScript("scripts/validate-planning-folder.mts", [bare]);
  assert.equal(res.status, 1);
  assert.equal(res.stdout, [
    `INVALID_PLANNING_FOLDER: ${bare}`,
    "Missing required artifacts:",
    "  - implementation-plan.md",
    "  - references.md",
    "  - handoff.md",
    "  - tasks/Tx-*.md (no task files found)",
    "Also missing (recommended):",
    "  - feature-specification.md",
    "  - draft.md",
    "",
  ].join("\n"));

  const noSpec = completeFolder();
  rmSync(join(noSpec, "feature-specification.md"));
  rmSync(join(noSpec, "draft.md"));
  const warn = runScript("scripts/validate-planning-folder.mts", [noSpec]);
  assert.equal(warn.status, 0);
  assert.equal(warn.stdout,
    "OK: planning folder valid. 1 task file(s) found. (warnings: missing feature-specification.md, draft.md)\n");
});

// --- check-dod CLI ---------------------------------------------------------- //

test("check-dod: a finished task passes and returns its validation block", () => {
  const res = runScript("scripts/check-dod.mts", ["--path", completeFolder(), "--task", "t1", "--json"]);
  assert.equal(res.status, 0);
  assert.deepEqual(JSON.parse(res.stdout), {
    ok: true, task: "T1", errors: [], warnings: [], lanes: ["Done"], validation_block: "- npm test\n- npm run lint",
  });
});

test("check-dod: unfinished task, human output", () => {
  const folder = completeFolder({
    "kanban.md": kanban({ "In Progress": ["- T1 still going"] }),
    "handoff.md": "# Handoff\n\nnothing yet (T10 is unrelated)\n",
    "tasks/T1-example.notes.md": "",
  });
  const res = runScript("scripts/check-dod.mts", [folder, "T1"]);
  assert.equal(res.status, 1);
  assert.equal(res.stdout, [
    "DOD_INCOMPLETE: T1",
    "  ERROR:   T1 is not in the Done lane (found in: In Progress)",
    "  WARNING: T1 also appears in: In Progress",
    "  WARNING: handoff.md does not mention T1",
    "  Re-run this task's validation block (verify, don't trust):",
    "    | - npm test",
    "    | - npm run lint",
    "",
  ].join("\n"));
});

test("check-dod: validation block stops at Estimate/Priority labels", () => {
  assert.equal(extractValidationBlock("Tests/Validation:\n- a\n\n**Estimate:** S\n- not this"), "- a");
  assert.equal(extractValidationBlock("no block here"), "");
});

// --- regressions from the parity review ------------------------------------- //

test("argparse: --opt=value keeps values with spaces; prefixes work before '='", () => {
  const folder = completeFolder();
  const spaced = makeFolder({});
  const res = runScript("scripts/check-commit.mts", ["--message=feat: add x\n\nTasks: T1"]);
  assert.deepEqual([res.status, res.stdout], [0, "OK: commit message valid.\n"]);
  assert.equal(runScript("scripts/validate-planning-folder.mts", [`--pa=${folder}`]).status, 0);
  assert.equal(runScript("scripts/validate-planning-folder.mts", [`--path=${spaced}/no such dir`]).stdout,
    `MISSING_FOLDER: ${spaced}/no such dir\n`);
});

test("argparse: negative numbers, --h help abbreviation", () => {
  // CPython 3.14: anything starting "-<digit>" or "-.<digit>" is a value, not an option
  for (const value of ["-5", "-1x", "-.5x"]) assert.equal(runScript("scripts/check-commit.mts", ["--message", value]).status, 1, value);
  assert.equal(runScript("scripts/validate-kanban.mts", ["-1x"]).stdout, "MISSING_FOLDER: -1x\n");
  assert.match(runScript("scripts/check-commit.mts", ["--message", "-x"]).stderr, /argument --message: expected one argument/);
  assert.equal(runScript("scripts/check-commit.mts", ["--help=x"]).status, 2);
  const help = runScript("scripts/validate-kanban.mts", ["--h"]);
  assert.equal(help.status, 0);
  assert.match(help.stdout, /^usage: validate-kanban\.mts/);
  assert.equal(runScript("scripts/validate-kanban.mts", ["--path", completeFolder(), "--wip", " 1_0 ", "--json"]).status, 0);
});

test("paths keep '..' like pathlib, so a symlinked parent is followed", async () => {
  const { mkdirSync, symlinkSync } = await import("node:fs");
  const base = makeFolder({});
  const real = completeFolder({ "tasks/T2-b.md": "x" });     // T2 is untracked -> kanban must fail
  mkdirSync(join(real, "sub"));
  symlinkSync(join(real, "sub"), join(base, "link"));
  const via = join(base, "link") + "/..";
  assert.equal(runScript("scripts/validate-planning-folder.mts", ["--path", via]).status, 0);
  const kanbanRes = runScript("scripts/validate-kanban.mts", ["--path", via, "--json"]);
  assert.equal(kanbanRes.status, 1);
  assert.deepEqual(JSON.parse(kanbanRes.stdout).errors, ["T2 has a task file but is not placed in any kanban lane"]);
});

test("IGNORECASE treats i / I / dotless i / dotted I alike, as Python's re does", () => {
  const body = TASK_BODY.replace("## In scope", "## \u{130}n scope").replace("## Priority", "## Pr\u{131}ority");
  const folder = makeFolder({ "t.md": body });
  const report = JSON.parse(runScript("scripts/validate-task-schema.mts", ["--file", join(folder, "t.md"), "--json"]).stdout);
  assert.deepEqual(report.results[0].missing_required, []);
});

test("check-commit: 'Tasks: none' is accepted only for docs commits (upstream 629ae3f)", () => {
  assert.equal(check("docs(spec): draft brief\n\nTasks: none").ok, true);
  assert.equal(check("docs: x\n\ntasks: NONE").ok, true);
  assert.deepEqual(check("feat(x): y\n\nTasks: none").errors, ["Tasks: none is allowed only for docs commits"]);
  assert.deepEqual(check("not conventional\n\nTasks: none").errors.at(-1), "Tasks: none is allowed only for docs commits");
});
