// py-compat.mts — the slice of Python stdlib behaviour the Node mirrors depend on.
//
// The .mts gates are a byte-for-byte mirror of the .py gates: same stdout, same
// exit codes, same --json documents. Where Python and JavaScript disagree on a
// primitive (line splitting, JSON escaping, repr(), argument parsing), the
// Python behaviour is reproduced here once so every script agrees with its twin.
//
// Zero dependencies on purpose: plugin installs never run `npm install`.

import { opendirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { pathToFileURL } from "node:url";

/** True when the module at `metaUrl` is the process entry point (not imported). */
export function isMain(metaUrl: string): boolean {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return pathToFileURL(realpathSync(entry)).href === metaUrl;
  } catch {
    return false;
  }
}

/** Python's str.splitlines(): every Unicode line boundary, no trailing empty line. */
export function splitlines(text: string): string[] {
  if (text === "") return [];
  const parts = text.split(/\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u{2028}\u{2029}]/u);
  if (parts[parts.length - 1] === "") parts.pop();
  return parts;
}

/** Escape a string for literal use inside pyRe() (Python's re.escape). */
export function reEscape(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Number of code points — Python's len() on a str, not UTF-16 units. */
export function pyLen(text: string): number {
  return [...text].length;
}

/**
 * A Python float. JavaScript has one number type, but Python's repr (and so
 * json.dumps and str()) writes floats with a decimal point: 900.0, not 900.
 */
export class PyFloat {
  readonly value: number;
  constructor(value: number) { this.value = value; }
  repr(): string {
    const v = this.value;
    if (Number.isNaN(v)) return "NaN";
    if (!Number.isFinite(v)) return v > 0 ? "Infinity" : "-Infinity";
    if (Number.isInteger(v) && Math.abs(v) < 1e16) return v.toFixed(1);
    // Both languages print the shortest round-trip digits; Python pads the exponent (1e-07).
    return String(v).replace(/e([+-])(\d)$/, "e$10$2");
  }
  toString(): string { return this.repr(); }
}

type Json = null | boolean | number | string | PyFloat | Json[] | { [key: string]: Json };

/** A string as Python's json.dumps (ensure_ascii=True) writes it. */
function pyJsonString(text: string): string {
  // JSON.stringify already escapes quotes, backslashes and C0 controls the same
  // way Python does; ensure_ascii additionally escapes DEL and everything
  // non-ASCII (astral characters as surrogate pairs, exactly like Python).
  return JSON.stringify(text).replace(
    /[^\x20-\x7e]/g,
    (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"),
  );
}

/**
 * Python's json.dumps(value) / json.dumps(value, indent=2).
 *
 * Compact form uses Python's default ", " and ": " separators, which
 * JSON.stringify cannot produce; the indented form matches it except for
 * ensure_ascii escaping, which is applied here too.
 */
export function pyDumps(value: unknown, indent?: number, sortKeys = false): string {
  const step = indent === undefined ? "" : " ".repeat(indent);
  const render = (v: Json, depth: number): string => {
    if (v instanceof PyFloat) return v.repr();
    if (v === null) return "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") return String(v);
    if (typeof v === "string") return pyJsonString(v);
    const entries: string[] = Array.isArray(v)
      ? v.map((item) => render(item, depth + 1))
      : (sortKeys ? Object.entries(v).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)) : Object.entries(v))
          .map(([k, item]) => pyJsonString(k) + ": " + render(item, depth + 1));
    const [open, close] = Array.isArray(v) ? ["[", "]"] : ["{", "}"];
    if (entries.length === 0) return open + close;
    if (indent === undefined) return open + entries.join(", ") + close;
    const pad = "\n" + step.repeat(depth + 1);
    return open + pad + entries.join("," + pad) + "\n" + step.repeat(depth) + close;
  };
  return render(value as Json, 0);
}

const NON_PRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;

/** Python's repr() of a str (used by check-commit's {subject!r}). */
export function pyRepr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of text) {
    const cp = ch.codePointAt(0)!;
    if (ch === quote || ch === "\\") out += "\\" + ch;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch !== " " && NON_PRINTABLE.test(ch)) {
      if (cp < 0x100) out += "\\x" + cp.toString(16).padStart(2, "0");
      else if (cp < 0x10000) out += "\\u" + cp.toString(16).padStart(4, "0");
      else out += "\\U" + cp.toString(16).padStart(8, "0");
    } else out += ch;
  }
  return out + quote;
}

// --------------------------------------------------------------------------- //
// argparse-lite                                                               //
// --------------------------------------------------------------------------- //

export interface OptionSpec {
  flag: string;                       // e.g. "--path"
  dest: string;
  kind: "string" | "int" | "bool";    // "bool" == action="store_true"
  default?: string | number | boolean | null;
  choices?: readonly string[];
}

export type ParsedArgs = Record<string, string | number | boolean | null>;

/**
 * Python's SystemExit. Thrown instead of calling process.exit(), which can drop
 * output still queued for a pipe (pipe writes are asynchronous on macOS).
 */
export class SystemExit extends Error {
  readonly code: number;
  constructor(code: number) {
    super(`exit ${code}`);
    this.code = code;
  }
}

/** Exit like argparse does on a usage error: message on stderr, exit 2. */
function usageError(prog: string, usage: string, message: string): never {
  process.stderr.write(`${usage}\n${prog}: error: ${message}\n`);
  throw new SystemExit(2);
}

/**
 * The argparse subset these gates use: long options (with `=` or a separate
 * value), store_true flags, unique-prefix abbreviations, int conversion,
 * choices, optional positionals, `--`, and `-h/--help`. Errors exit 2 on
 * stderr, as argparse does, so exit-code contracts stay identical.
 */
export function parseArgs(
  prog: string,
  options: readonly OptionSpec[],
  positionals: readonly string[],
  argv: readonly string[] = process.argv.slice(2),
): ParsedArgs {
  const usage =
    `usage: ${prog} [-h]` +
    options.map((o) => (o.kind === "bool" ? ` [${o.flag}]` : ` [${o.flag} ${o.dest.toUpperCase()}]`)).join("") +
    positionals.map((p) => ` [${p}]`).join("");

  const result: ParsedArgs = {};
  for (const o of options) result[o.dest] = o.default ?? (o.kind === "bool" ? false : null);
  for (const p of positionals) result[p] = null;

  const HELP: OptionSpec = { flag: "--help", dest: "help", kind: "bool" };
  const known = [...options, HELP];

  /**
   * argparse's _parse_optional, in its order: exact flag, `flag=value`, unique
   * prefix (also before `=`), then negative numbers and anything containing a
   * space are positional; any other dash-word is an unrecognised option. The
   * negative-number test is CPython 3.13.1+'s prefix match `-\.?\d` (gh-123945),
   * the interpreter mcp/.python-version pins.
   */
  type Classified = { kind: "positional" } | { kind: "option"; spec: OptionSpec; explicit: string | null } | { kind: "unknown" };
  const classify = (arg: string): Classified => {
    if (!arg.startsWith("-") || arg === "-") return { kind: "positional" };
    if (arg === "-h") return { kind: "option", spec: HELP, explicit: null };
    const exact = known.find((o) => o.flag === arg);
    if (exact) return { kind: "option", spec: exact, explicit: null };
    const eq = arg.indexOf("=");
    const name = eq === -1 ? arg : arg.slice(0, eq);
    const explicit = eq === -1 ? null : arg.slice(eq + 1);
    const byName = known.find((o) => o.flag === name);
    if (byName) return { kind: "option", spec: byName, explicit };
    if (name.startsWith("--")) {
      const matches = known.filter((o) => o.flag.startsWith(name));
      if (matches.length > 1) {
        usageError(prog, usage, `ambiguous option: ${name} could match ${matches.map((m) => m.flag).join(", ")}`);
      }
      if (matches.length === 1) return { kind: "option", spec: matches[0]!, explicit };
    }
    if (/^-\.?\d/.test(arg)) return { kind: "positional" };
    if (arg.includes(" ")) return { kind: "positional" };
    return { kind: "unknown" };
  };

  const extras: string[] = [];
  let posIndex = 0;
  let onlyPositionals = false;

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]!;
    if (!onlyPositionals && arg === "--") { onlyPositionals = true; continue; }
    const c: Classified = onlyPositionals ? { kind: "positional" } : classify(arg);
    if (c.kind === "unknown") { extras.push(arg); continue; }
    if (c.kind === "option") {
      const { spec, explicit } = c;
      if (spec === HELP) {
        if (explicit !== null) usageError(prog, usage, `argument -h/--help: ignored explicit argument '${explicit}'`);
        process.stdout.write(usage + "\n");
        throw new SystemExit(0);
      }
      if (spec.kind === "bool") {
        if (explicit !== null) usageError(prog, usage, `argument ${spec.flag}: ignored explicit argument '${explicit}'`);
        result[spec.dest] = true;
        continue;
      }
      let value: string;
      if (explicit !== null) value = explicit;
      else {
        const next = argv[i + 1];
        if (next === undefined || next === "--" || classify(next).kind !== "positional") {
          usageError(prog, usage, `argument ${spec.flag}: expected one argument`);
        }
        value = next;
        i++;
      }
      if (spec.choices && !spec.choices.includes(value)) {
        usageError(prog, usage,
          `argument ${spec.flag}: invalid choice: '${value}' (choose from ${spec.choices.map((c) => `'${c}'`).join(", ")})`);
      }
      if (spec.kind === "int") {
        // int(): surrounding whitespace, a sign, and single underscores between digits.
        if (!pyRe(String.raw`^\s*[-+]?[0-9]+(?:_[0-9]+)*\s*$`).test(value)) {
          usageError(prog, usage, `argument ${spec.flag}: invalid int value: '${value}'`);
        }
        result[spec.dest] = Number.parseInt(pyStrip(value).replace(/_/g, ""), 10);
      } else {
        result[spec.dest] = value;
      }
      continue;
    }
    if (posIndex < positionals.length) result[positionals[posIndex++]!] = arg;
    else extras.push(arg);
  }
  if (extras.length) usageError(prog, usage, `unrecognized arguments: ${extras.join(" ")}`);
  return result;
}

/** Read all of stdin as UTF-8 (invalid bytes become U+FFFD, like errors="replace"). */
export async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString("utf8");
}

/**
 * `sys.exit(main())`: run a script's main and set the exit code without
 * cutting off buffered stdout. Uncaught errors exit 1, as a traceback would.
 */
export function runMain(main: () => number | Promise<number>): void {
  Promise.resolve()
    .then(main)
    .then((code) => { process.exitCode = code; })
    .catch((err: unknown) => {
      if (err instanceof SystemExit) { process.exitCode = err.code; return; }
      process.stderr.write(String(err instanceof Error ? err.stack : err) + "\n");
      process.exitCode = 1;
    });
}

/** print(): write a line to stdout. */
export function print(line: string): void {
  process.stdout.write(line + "\n");
}

const SEP = process.platform === "win32" ? "\\" : "/";

/** str(Path(text)): collapse duplicate separators and "." parts, drop the trailing slash. */
export function pyPath(text: string): string {
  if (text === "") return ".";
  const raw = process.platform === "win32" ? text.replace(/\//g, "\\") : text;
  const root = raw.startsWith(SEP + SEP) && !raw.startsWith(SEP + SEP + SEP) ? SEP + SEP : raw.startsWith(SEP) ? SEP : "";
  const parts = raw.slice(root.length).split(SEP).filter((p) => p !== "" && p !== ".");
  const joined = root + parts.join(SEP);
  return joined === "" ? "." : joined;
}

/** str(Path(base) / leaf). */
export function pyJoin(base: string, leaf: string): string {
  const b = pyPath(base);
  if (b === ".") return leaf;
  return b.endsWith(SEP) ? b + leaf : b + SEP + leaf;
}

/** Path.is_file() — follows symlinks, false on any error. */
export function isFile(path: string): boolean {
  try { return statSync(path).isFile(); } catch { return false; }
}

/** Path.is_dir(). */
export function isDir(path: string): boolean {
  try { return statSync(path).isDirectory(); } catch { return false; }
}

/**
 * Path.read_text(encoding="utf-8", errors="replace"): invalid bytes become
 * U+FFFD and, as in Python's text mode, "\r\n" and "\r" read as "\n".
 */
export function readText(path: string): string {
  return readFileSync(path, "utf8").replace(/\r\n?/g, "\n");
}

// --------------------------------------------------------------------------- //
// Python `re` semantics                                                       //
// --------------------------------------------------------------------------- //

// Python's str.isspace() set. JavaScript's \s differs: it includes U+FEFF (so a
// BOM would count as indentation) and omits \x1c-\x1f and \x85.
const PY_SPACE = String.raw`\t\n\v\f\r\x1c-\x1f \x85\xa0\u{1680}\u{2000}-\u{200a}\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}`;
// Python's \w for str patterns: letters, numbers, underscore (not combining marks).
const PY_WORD = String.raw`\p{L}\p{N}_`;
// sre_compile's _ignorecase_fixes: under re.I, i / I / dotless ı / dotted İ all match each other.
const PY_TURKISH_I = String.raw`iI\u{131}\u{130}`;
const PY_BOUNDARY = `(?:(?<![${PY_WORD}])(?=[${PY_WORD}])|(?<=[${PY_WORD}])(?![${PY_WORD}]))`;

/** str.strip() with Python's whitespace set. */
export function pyStrip(text: string): string {
  return text.replace(new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "gu"), "");
}

/**
 * Compile a Python regex source with Python semantics.
 *
 * Rewrites the tokens whose meaning differs between the engines: \s \S \d \D
 * \w \W \b; "." (Python excludes only \n, JavaScript also \r U+2028 U+2029);
 * "$" (Python's also matches before a final \n); and, for flag "m", ^ and $
 * (JavaScript's multiline anchors also fire at \r U+2028 U+2029). Compiles in "u"
 * mode so case-insensitive matching folds Unicode the way Python's does.
 * Flags: i = IGNORECASE, m = MULTILINE, g = findall-style global matching.
 */
export function pyRe(source: string, flags = ""): RegExp {
  const multiline = flags.includes("m");
  const ignoreCase = flags.includes("i");
  const isTurkishI = (ch: string) => ch === "i" || ch === "I" || ch === "\u{131}" || ch === "\u{130}";
  let out = "";
  let inClass = false;
  let classNeedsTurkishI = false;
  for (let i = 0; i < source.length; i++) {
    const ch = source[i]!;
    if (ch === "\\" && i + 1 < source.length) {
      const next = source[++i]!;
      if (next === "s") out += inClass ? PY_SPACE : `[${PY_SPACE}]`;
      else if (inClass && (next === "S" || next === "W" || next === "D")) {
        throw new Error(`pyRe: \\${next} inside a character class is not supported: ${source}`);
      }
      else if (next === "S") out += `[^${PY_SPACE}]`;
      else if (next === "d") out += String.raw`\p{Nd}`;
      else if (next === "D") out += String.raw`\P{Nd}`;
      else if (next === "w") out += inClass ? PY_WORD : `[${PY_WORD}]`;
      else if (next === "W") out += `[^${PY_WORD}]`;
      else if (next === "b" && !inClass) out += PY_BOUNDARY;
      else out += "\\" + next;
      continue;
    }
    if (inClass) {
      if (ch === "]") {
        inClass = false;
        if (classNeedsTurkishI) out += PY_TURKISH_I;
        classNeedsTurkishI = false;
      } else if (ignoreCase && isTurkishI(ch)) {
        classNeedsTurkishI = true;
      }
      out += ch;
      continue;
    }
    if (ch === "[") {
      inClass = true;
      out += ch;
      if (source[i + 1] === "^") out += source[++i];
      if (source[i + 1] === "]") out += "\\" + source[++i];   // leading ] is literal in Python
      continue;
    }
    if (ignoreCase && isTurkishI(ch)) out += `[${PY_TURKISH_I}]`;
    else if (ch === ".") out += String.raw`[^\n]`;
    else if (multiline && ch === "^") out += String.raw`(?<![^\n])`;
    else if (ch === "$") out += multiline ? String.raw`(?![^\n])` : String.raw`(?=\n?(?![\s\S]))`;
    else out += ch;
  }
  return new RegExp(out, "u" + (flags.includes("i") ? "i" : "") + (flags.includes("g") ? "g" : ""));
}

/** Python truthiness: unlike JavaScript, empty arrays and objects are falsy. */
export function pyTruthy(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object" && value !== null) return Object.keys(value).length > 0;
  return Boolean(value);
}

/** Python's `a or b`. */
export function pyOr(...values: unknown[]): unknown {
  for (const v of values) if (pyTruthy(v)) return v;
  return values[values.length - 1];
}

/** os.listdir / Path.iterdir order: raw readdir order (readdirSync sorts; this does not). */
export function listdir(path: string): string[] {
  const dir = opendirSync(path);
  const names: string[] = [];
  try {
    for (let entry = dir.readSync(); entry !== null; entry = dir.readSync()) names.push(entry.name);
  } finally {
    dir.closeSync();
  }
  return names;
}

/** sorted() on strings: code-point order (Array.prototype.sort compares UTF-16 units). */
export function pySorted(items: Iterable<string>): string[] {
  const cmp = (a: string, b: string): number => {
    const x = [...a], y = [...b];
    for (let i = 0; i < Math.min(x.length, y.length); i++) {
      const d = x[i]!.codePointAt(0)! - y[i]!.codePointAt(0)!;
      if (d !== 0) return d;
    }
    return x.length - y.length;
  };
  return [...items].sort(cmp);
}
