#!/usr/bin/env node
/**
 * workflow-budget.mts — Node mirror of workflow-budget.py.
 *
 * Read-only workflow clock; no process control, network, or acceptance decisions.
 *
 * Stage targets are cumulative advisory allocations, not measured completion
 * estimates. Required gates remain required when a target or deadline expires.
 *
 * Same arguments, stdout and exit codes as the .py (0 ok, 2 invalid, 3 exhausted).
 * Deadlines parse with Python 3.11+ datetime.fromisoformat rules, so a 7-digit
 * fraction (.NET's round-trip format) is truncated to microseconds rather than
 * rejected as it is on Python 3.9/3.10.
 *
 * Usage:
 *   node workflow-budget.mts --stage spec-research [--json]
 */

import { isMain, parseArgs, print, pyDumps, PyFloat, pyRepr, runMain, SystemExit } from "./lib/py-compat.mts";

export const STAGE_TARGETS: Record<string, number> = {
  "spec-research": 0.15, "geeky-plan": 0.30, "plan-review": 0.40,
  "geeky-implement": 0.90, "impl-review": 1.0,
};

class ValueError extends Error {}

// --- datetime, in integer microseconds since the Unix epoch (Python's resolution) ---------- //

const US = 1_000_000;
// datetime.min / datetime.max. (Date.UTC maps years 0-99 to 1900-1999, hence setUTCFullYear.)
const MIN_US = new Date(0).setUTCFullYear(1, 0, 1) * 1000;
const MAX_US = Date.UTC(9999, 11, 31, 23, 59, 59) * 1000 + 999_999;

function checkRange(us: number): number {
  if (!Number.isFinite(us) || us < MIN_US || us > MAX_US) throw new ValueError("date value out of range");
  return us;
}

// datetime.fromisoformat (3.11+), for the forms a deadline takes: extended or basic date,
// "T" or " " separator, optional time with any number of fractional digits (truncated to
// microseconds), optional ±HH[:MM[:SS[.ffffff]]] offset.
const ISO_RE = /^(\d{4})-?(\d{2})-?(\d{2})(?:[T ](\d{2})(?::?(\d{2})(?::?(\d{2})(?:[.,](\d+))?)?)?(?:([+-])(\d{2})(?::?(\d{2})(?::?(\d{2})(?:\.(\d{1,6}))?)?)?)?)?$/;

function fromIsoformat(value: string): { us: number; offsetUs: number | null } {
  const m = ISO_RE.exec(value);
  const invalid = () => new ValueError(`Invalid isoformat string: ${pyRepr(value)}`);
  if (!m) throw invalid();
  const [, y, mo, d, hh = "0", mi = "0", ss = "0", frac = "", sign, oh, om = "0", os = "0", ofrac = ""] = m;
  const n = (t: string) => Number.parseInt(t, 10);
  const year = n(y!), month = n(mo!), day = n(d!), hour = n(hh), minute = n(mi), second = n(ss);
  // A well-formed string with an impossible field fails in the datetime constructor, with its messages.
  if (year < 1) throw new ValueError(`year ${year} is out of range`);
  if (month < 1 || month > 12) throw new ValueError("month must be in 1..12");
  if (day < 1 || day > monthDays(year, month)) throw new ValueError("day is out of range for month");
  if (hour > 23) throw new ValueError("hour must be in 0..23");
  if (minute > 59) throw new ValueError("minute must be in 0..59");
  if (second > 59) throw new ValueError("second must be in 0..59");
  const micro = frac ? n(frac.slice(0, 6).padEnd(6, "0")) : 0;
  const fixed = new Date(0).setUTCFullYear(year, month - 1, day) + ((hour * 60 + minute) * 60 + second) * 1000;
  let offsetUs: number | null = null;
  if (sign) {
    const ho = n(oh!), mo2 = n(om), so = n(os);
    if (ho > 23 || mo2 > 59 || so > 59) throw invalid();
    offsetUs = (sign === "-" ? -1 : 1) * ((ho * 3600 + mo2 * 60 + so) * US + (ofrac ? n(ofrac.padEnd(6, "0")) : 0));
  }
  return { us: fixed * 1000 + micro, offsetUs };
}

const isLeap = (y: number) => (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
const monthDays = (y: number, m: number) => [31, isLeap(y) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]!;

function utc(value: string): number {
  const parsed = fromIsoformat(value.split("Z").join("+00:00"));
  if (parsed.offsetUs === null || parsed.offsetUs !== 0) throw new ValueError("Deadline must have an explicit UTC offset");
  return parsed.us;
}

/** datetime.isoformat() of an aware UTC datetime. */
function isoformat(us: number): string {
  const wholeSeconds = Math.floor(us / US);
  const micro = us - wholeSeconds * US;
  const d = new Date(wholeSeconds * 1000);
  const p = (v: number, w = 2) => String(v).padStart(w, "0");
  const date = `${p(d.getUTCFullYear(), 4)}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
  const time = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
  return `${date}T${time}${micro ? "." + p(micro, 6) : ""}+00:00`;
}

/** timedelta(seconds=x) in microseconds: rounded half to even, like CPython. */
function timedeltaUs(seconds: number): number {
  const us = seconds * US;
  if (!Number.isFinite(us) || Math.abs(us) / US / 86400 > 999_999_999) {
    throw new ValueError(`days=${Math.trunc(seconds / 86400)}; must have magnitude <= 999999999`);
  }
  return roundHalfEven(us);
}

function roundHalfEven(x: number): number {
  const r = Math.round(x);
  return Math.abs(x % 1) === 0.5 ? 2 * Math.round(x / 2) : r;
}

// --- Python numbers -------------------------------------------------------------------------- //

/** float(str): surrounding whitespace, underscores between digits, inf/nan words. */
function pyFloat(text: string): number {
  const t = text.trim();
  if (/^[+-]?(inf|infinity)$/i.test(t)) return t.startsWith("-") ? -Infinity : Infinity;
  if (/^[+-]?nan$/i.test(t)) return NaN;
  if (!/^[+-]?(\d(_?\d)*(\.(\d(_?\d)*)?)?|\.\d(_?\d)*)([eE][+-]?\d(_?\d)*)?$/.test(t)) {
    throw new ValueError(`could not convert string to float: ${pyRepr(text)}`);
  }
  return Number(t.replace(/_/g, ""));
}

function number(value: string, name: string): number {
  const result = pyFloat(value);
  if (!Number.isFinite(result) || result < 0) throw new ValueError(`${name} must be finite and nonnegative`);
  return result;
}

/** round(x, 3) on a float: correctly rounded, ties to even. */
function round3(x: number): number {
  const fixed = Number(x.toFixed(3));
  // toFixed breaks exact ties upward; Python breaks them to even. Exact ties at 3 places
  // are the binary-exact sixteenths (x * 2000 is an integer but x * 1000 is not).
  if (Number.isInteger(x * 2000) && !Number.isInteger(x * 1000)) {
    const lo = Math.floor(x * 1000), hi = lo + 1;
    return (lo % 2 === 0 ? lo : hi) / 1000;
  }
  return fixed;
}

/** round(max(0, x), 3): max() returns the int 0 when x is not positive, else the float. */
const roundedNonnegative = (x: number): number | PyFloat => (x > 0 ? new PyFloat(round3(x)) : 0);

// --- evaluate ---------------------------------------------------------------------------------- //

export type Report = Record<string, string | number | boolean | null | PyFloat>;

export function evaluate(stage: string, env: Record<string, string | undefined>, nowUs?: number): Report {
  const now = nowUs ?? Date.now() * 1000;
  if (!(stage in STAGE_TARGETS)) throw new ValueError("Unknown workflow stage");
  const deadline = env.GEEKY_DEADLINE_UTC ?? "";
  const reserve = env.GEEKY_CLOSEOUT_RESERVE_S ?? "";
  const budget = env.GEEKY_HARD_WALL_BUDGET_S ?? "";
  const report: Report = {
    schema: "geeky.workflow-budget/1", stage,
    now_utc: isoformat(now), status: "unbounded",
    hard_deadline_utc: null, closeout_reserve_s: null,
    hard_wall_budget_s: null, remaining_s: null,
    usable_s: null, stage_target_utc: null,
    stage_target_remaining_s: null, stage_target_exceeded: false,
    stage_target_policy: "cumulative-advisory-v1",
    action: "ordinary-workflow", acceptance_established: false,
  };
  if (!(deadline || reserve || budget)) return report;
  try {
    if (!deadline || reserve === "") throw new ValueError("Deadline and reserve must both be supplied");
    const end = utc(deadline);
    const reserveS = number(reserve, "Reserve");
    const total = budget !== "" ? number(budget, "Budget") : null;
    if (total !== null && (total <= 0 || reserveS >= total)) throw new ValueError("Budget must be positive and larger than reserve");
    const remaining = (end - now) / US;
    Object.assign(report, {
      hard_deadline_utc: isoformat(end), closeout_reserve_s: new PyFloat(reserveS),
      hard_wall_budget_s: total === null ? null : new PyFloat(total),
      remaining_s: roundedNonnegative(remaining), usable_s: roundedNonnegative(remaining - reserveS),
    });
    if (total !== null) {
      const start = checkRange(end - timedeltaUs(total));
      if (start > now + US) throw new ValueError("Budget and deadline imply a future start");
      const target = checkRange(start + timedeltaUs((total - reserveS) * STAGE_TARGETS[stage]!));
      const delta = (target - now) / US;
      Object.assign(report, {
        stage_target_utc: isoformat(target),
        stage_target_remaining_s: new PyFloat(round3(delta)),
        stage_target_exceeded: delta <= 0,
      });
    }
    if (remaining <= 0) Object.assign(report, { status: "exhausted", action: "checkpoint-and-stop" });
    else if (remaining <= reserveS) Object.assign(report, { status: "reserve", action: "checkpoint-no-new-stage-or-dispatch" });
    else {
      Object.assign(report, {
        status: "active",
        action: report.stage_target_exceeded ? "reduce-optional-scope-complete-required-gates" : "bounded-required-work",
      });
    }
  } catch (error) {
    if (!(error instanceof ValueError)) throw error;
    Object.assign(report, { status: "invalid", action: "checkpoint-and-stop", error: error.message });
  }
  return report;
}

const show = (v: unknown) => (v === null ? "None" : String(v));

function main(): number {
  const args = parseArgs("workflow-budget.mts", [
    { flag: "--stage", dest: "stage", kind: "string", choices: Object.keys(STAGE_TARGETS) },
    { flag: "--json", dest: "as_json", kind: "bool" },
  ], []);
  if (args.stage === null) {
    process.stderr.write("usage: workflow-budget.mts [-h] --stage STAGE [--json]\n"
      + "workflow-budget.mts: error: the following arguments are required: --stage\n");
    throw new SystemExit(2);
  }
  const report = evaluate(args.stage as string, process.env);
  if (args.as_json) print(pyDumps(report, undefined, true));
  else print(`${report.stage}: ${report.status}; ${report.action}; remaining=${show(report.remaining_s)}s; usable=${show(report.usable_s)}s`);
  return ({ invalid: 2, exhausted: 3 } as Record<string, number>)[report.status as string] ?? 0;
}

if (isMain(import.meta.url)) runMain(main);
