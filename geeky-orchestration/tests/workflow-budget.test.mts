// workflow-budget.mts: the cases test_workflow_budget.py covers, plus the CLI and
// the 7-digit deadline that Python 3.9/3.10 reject.

import assert from "node:assert/strict";
import { test } from "node:test";
import { evaluate } from "../scripts/workflow-budget.mts";
import { PyFloat, pyDumps } from "../scripts/lib/py-compat.mts";
import { runScript } from "./helpers.mts";

const NOW = Date.UTC(2026, 9, 27) * 1000;   // 2026-10-27T00:00:00Z, in microseconds
const iso = (us: number) => new Date(us / 1000).toISOString().replace(".000Z", "+00:00");
const context = (elapsed = 0): Record<string, string> => ({
  GEEKY_DEADLINE_UTC: iso(NOW + (6000 - elapsed) * 1e6),
  GEEKY_CLOSEOUT_RESERVE_S: "60", GEEKY_HARD_WALL_BUDGET_S: "6000",
});
const num = (v: unknown) => (v instanceof PyFloat ? v.value : v);

test("unset does not invent a budget", () => {
  const result = evaluate("spec-research", {}, NOW);
  assert.equal(result.status, "unbounded");
  assert.equal(result.remaining_s, null);
  assert.equal(result.acceptance_established, false);
});

test("cumulative targets leave implementation time", () => {
  const result = evaluate("plan-review", context(), NOW);
  assert.equal(num(result.usable_s), 5940);
  assert.equal(num(result.stage_target_remaining_s), 2376);
  assert.equal(result.status, "active");
});

test("replaying the Claude timeline surfaces the overrun before coding", () => {
  const research = evaluate("spec-research", context(1800), NOW);
  const plan = evaluate("geeky-plan", context(4500), NOW);
  assert.ok(research.stage_target_exceeded && plan.stage_target_exceeded);
  assert.equal(num(plan.remaining_s), 1500);
  assert.equal(plan.action, "reduce-optional-scope-complete-required-gates");
  assert.equal(plan.acceptance_established, false);
});

for (const [elapsed, status] of [[5940, "reserve"], [5999, "reserve"], [6000, "exhausted"], [6001, "exhausted"]] as const) {
  test(`reserve and expiry cannot authorize work (elapsed ${elapsed})`, () => {
    const result = evaluate("geeky-plan", context(elapsed), NOW);
    assert.equal(result.status, status);
    assert.ok(String(result.action).startsWith("checkpoint"));
    assert.equal(result.acceptance_established, false);
  });
}

for (const patch of [
  { GEEKY_DEADLINE_UTC: "" }, { GEEKY_DEADLINE_UTC: "2026-10-27T00:00:00" },
  { GEEKY_CLOSEOUT_RESERVE_S: "nan" }, { GEEKY_CLOSEOUT_RESERVE_S: "-1" },
  { GEEKY_HARD_WALL_BUDGET_S: "0" }, { GEEKY_HARD_WALL_BUDGET_S: "60" },
  { GEEKY_HARD_WALL_BUDGET_S: "inf" }, { GEEKY_HARD_WALL_BUDGET_S: "1e999" },
  { GEEKY_DEADLINE_UTC: "9999-12-31T23:59:59+00:00" },
]) {
  test(`invalid context fails closed: ${JSON.stringify(patch)}`, () => {
    const result = evaluate("spec-research", { ...context(), ...patch }, NOW);
    assert.equal(result.status, "invalid");
    assert.equal(result.acceptance_established, false);
  });
}

test("legacy context retains the deadline without fabricated targets", () => {
  const { GEEKY_HARD_WALL_BUDGET_S: _, ...env } = context();
  const result = evaluate("plan-review", env, NOW);
  assert.equal(result.status, "active");
  assert.equal(result.stage_target_utc, null);
});

test("CLI status codes: 0 unbounded, 2 invalid, 3 exhausted", () => {
  const clean = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith("GEEKY_")));
  assert.equal(runScript("scripts/workflow-budget.mts", ["--stage", "spec-research", "--json"], "", clean).status, 0);
  assert.equal(runScript("scripts/workflow-budget.mts", ["--stage", "spec-research", "--json"], "", { ...clean, GEEKY_DEADLINE_UTC: "bad" }).status, 2);
  assert.equal(runScript("scripts/workflow-budget.mts", ["--stage", "spec-research", "--json"], "",
    { ...clean, GEEKY_DEADLINE_UTC: "2000-01-01T00:00:00Z", GEEKY_CLOSEOUT_RESERVE_S: "0" }).status, 3);
  assert.equal(runScript("scripts/workflow-budget.mts", [], "", clean).status, 2);   // --stage is required
});

// --- beyond the Python tests -------------------------------------------------------------- //

test("a .NET round-trip deadline (7 fractional digits) parses, as on Python 3.11+", () => {
  const result = evaluate("spec-research", { ...context(), GEEKY_DEADLINE_UTC: "2026-10-27T01:40:00.5424100Z" }, NOW);
  assert.equal(result.status, "active");
  assert.equal(result.hard_deadline_utc, "2026-10-27T01:40:00.542410+00:00");   // truncated to microseconds
});

test("JSON is json.dumps(report, sort_keys=True), with Python float formatting", () => {
  const text = pyDumps(evaluate("plan-review", context(), NOW), undefined, true);
  assert.match(text, /^\{"acceptance_established": false, "action": "bounded-required-work", "closeout_reserve_s": 60\.0, /);
  assert.match(text, /"hard_wall_budget_s": 6000\.0, /);
  assert.match(text, /"remaining_s": 6000\.0, /);
  // max(0, x) is the int 0 when nothing remains, so Python writes 0, not 0.0
  assert.match(pyDumps(evaluate("geeky-plan", context(6001), NOW), undefined, true), /"remaining_s": 0, "schema"/);
});

test("out-of-range dates report the datetime constructor's message", () => {
  const result = evaluate("spec-research", { ...context(), GEEKY_DEADLINE_UTC: "2026-02-30T00:00:00Z" }, NOW);
  assert.equal(result.error, "day is out of range for month");
});
