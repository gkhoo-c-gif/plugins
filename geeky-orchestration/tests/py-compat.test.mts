// Python-semantics primitives the mirrors rely on. Expected values are what
// CPython 3.14 produces for the same input.

import assert from "node:assert/strict";
import { test } from "node:test";
import { pyDumps, pyJoin, pyOr, pyPath, pyRe, pyRepr, pyStrip, splitlines } from "../scripts/lib/py-compat.mts";

test("pyRe: \\s is Python's set, so a BOM is not indentation", () => {
  const label = pyRe(String.raw`^\s*[#*\s]*Task\s*Name\b`, "im");
  assert.equal(label.test("\u{feff}# Task Name"), false);
  assert.equal(label.test("x\n## Task Name\n"), true);
});

test("pyRe: \\b uses Python's Unicode word set", () => {
  assert.deepEqual("\u{e9}T1 T2 _T3 T4\u{e9}".match(pyRe(String.raw`\bT\d+\b`, "ig")), ["T2"]);
});

test("pyRe: '.', '$' and MULTILINE anchors follow Python", () => {
  assert.equal(pyRe("a.b").test("a\rb"), true);
  assert.equal(pyRe(String.raw`\.md$`).test("x.md\n"), true);
  assert.equal(pyRe(String.raw`\.md$`).test("x.md\n\n"), false);
  assert.equal(pyRe("^x", "m").test("a\rx"), false);
  assert.equal(pyRe("^x", "m").test("a\nx"), true);
  assert.equal(pyRe("k", "i").test("\u{212a}"), true);   // KELVIN SIGN folds like re.IGNORECASE
});

test("pyRe: Turkish i inside a class keeps ranges valid", () => {
  const re = pyRe("^[i-k]+$", "i");
  assert.equal(re.test("\u{130}j\u{131}K"), true);
  assert.equal(re.test("l"), false);
});

test("pyRe: refuses class escapes it cannot translate faithfully", () => {
  assert.throws(() => pyRe(String.raw`[\S]`), /not supported/);
});

test("splitlines / strip", () => {
  assert.deepEqual(splitlines("a\r\nb\n\nc\u{2028}d\n"), ["a", "b", "", "c", "d"]);
  assert.deepEqual(splitlines(""), []);
  assert.equal(pyStrip("\x1c a \u{feff}"), "a \u{feff}");
});

test("pyDumps matches json.dumps (ensure_ascii, separators, indent)", () => {
  assert.equal(pyDumps({ ok: false, errors: ["\u{e9}\x7f"] }), String.raw`{"ok": false, "errors": ["\u00e9\u007f"]}`);
  assert.equal(pyDumps("\u{1f600}"), String.raw`"\ud83d\ude00"`);
  assert.equal(pyDumps({ a: [], b: {}, c: [1, "x"] }, 2), '{\n  "a": [],\n  "b": {},\n  "c": [\n    1,\n    "x"\n  ]\n}');
});

test("pyRepr matches repr()", () => {
  assert.equal(pyRepr("fix: x"), "'fix: x'");
  assert.equal(pyRepr("it's\t\u{200b}"), String.raw`"it's\t\u200b"`);
  assert.equal(pyRepr(`a'b"c`), String.raw`'a\'b"c'`);
});

test("pyPath / pyJoin match str(Path(...))", () => {
  assert.deepEqual([pyPath("a//b/./c/"), pyPath("./x"), pyPath(""), pyPath("/")], ["a/b/c", "x", ".", "/"]);
  assert.deepEqual([pyJoin(".", "k.md"), pyJoin("/", "k.md"), pyJoin("a/", "k.md")], ["k.md", "/k.md", "a/k.md"]);
});

test("pyOr uses Python truthiness", () => {
  assert.deepEqual(pyOr({}, { file_path: "x" }), { file_path: "x" });
  assert.equal(pyOr([], ""), "");
});
