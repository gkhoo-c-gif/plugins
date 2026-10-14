#!/usr/bin/env node
/**
 * geeky_mcp (Node) — MCP server exposing the geeky-orchestration deterministic quality gates.
 *
 * Node mirror of server.py, for environments without uv/Python. Same contract:
 * a thin adapter, not a reimplementation. Each tool shells out to the portable
 * validator mirror under ../scripts (or ../hooks) with its --json flag and
 * returns the parsed report, so MCP output is identical to the CLI/hook output.
 * The wire format — tool names, input schemas, descriptions, result shapes — is
 * pinned to wire_baseline.json, the transcript captured from server.py.
 *
 * All tools are read-only: they inspect a planning folder / message / path and never
 * modify anything. A non-zero exit from a validator is a *validation failure* (a
 * normal result with ok=false), not a server error.
 *
 * Zero dependencies (plugin installs never run `npm install`): line-delimited
 * JSON-RPC 2.0 over stdio, implemented here. Needs Node >= 22.18 for .mts type
 * stripping.
 *
 * Run (stdio):
 *   node server.mts
 */

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { constants } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { isDir, isFile, isMain, pyRepr, pyStrip } from "../scripts/lib/py-compat.mts";

const PLUGIN_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPTS = join(PLUGIN_ROOT, "scripts");
const HOOKS = join(PLUGIN_ROOT, "hooks");

const SERVER_NAME = "geeky_mcp";
// Newest first; an unsupported client version is answered with the newest.
const PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

// Older type-stripping Nodes (22.18 - 23.x) print an ExperimentalWarning, which
// would surface as a spurious "stderr" field on every tool result.
const CHILD_NODE_FLAGS = ["--disable-warning=ExperimentalWarning"];

const READONLY = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false,
};

/**
 * The plugin's own version, for serverInfo.version. A version string must never
 * be the reason a server fails to boot, so an unreadable manifest degrades to 0.0.0.
 */
function pluginVersion(): string {
  try {
    const manifest = join(PLUGIN_ROOT, ".claude-plugin", "plugin.json");
    const version = JSON.parse(readFileSync(manifest, "utf8")).version;
    return String(version ?? "") || "0.0.0";
  } catch {
    return "0.0.0";
  }
}

export const SERVER_VERSION = pluginVersion();

type Report = Record<string, unknown>;

// --------------------------------------------------------------------------- //
// Shared subprocess helper                                                    //
// --------------------------------------------------------------------------- //

/**
 * Run a validator script with the current Node; return its parsed report.
 *
 * Returns an object that always contains 'exit_code'. If the script emitted JSON on
 * stdout (the --json contract) those fields are merged in; otherwise the raw text
 * is returned under 'output'. A missing script or spawn failure surfaces as
 * {error: ...}.
 */
export function run(script: string, args: string[], stdinText?: string): Promise<Report> {
  if (!isFile(script)) {
    return Promise.resolve({ ok: false, error: `validator not found: ${script}`, exit_code: 127 });
  }
  return new Promise((resolve) => {
    // stdin is "ignore", never "inherit": on stdio transport this server's own
    // stdin is the host's pipe, and a validator that inherits it never exits —
    // the tool call hangs until the host gives up. Only the two validators that
    // are fed text on stdin get a pipe of their own.
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(process.execPath, [...CHILD_NODE_FLAGS, script, ...args], {
        stdio: [stdinText === undefined ? "ignore" : "pipe", "pipe", "pipe"],
      });
    } catch (e) {
      // e.g. a NUL byte in an argument: server.py reports these as a result, not a crash.
      resolve({ ok: false, error: `failed to run ${basename(script)}: ${describe(e)}`, exit_code: 1 });
      return;
    }
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    child.stdout!.on("data", (chunk: Buffer) => out.push(chunk));
    child.stderr!.on("data", (chunk: Buffer) => err.push(chunk));
    child.on("error", (e) => {
      resolve({ ok: false, error: `failed to run ${basename(script)}: ${describe(e)}`, exit_code: 1 });
    });
    child.on("close", (code, signal) => {
      // Like Popen.returncode: a signal death is the negated signal number.
      const rc = code ?? -((signal && constants.signals[signal]) || 1);
      const stdout = pyStrip(Buffer.concat(out).toString("utf8"));
      const stderr = pyStrip(Buffer.concat(err).toString("utf8"));

      const result: Report = { exit_code: rc };
      let parsed: unknown = null;
      if (stdout) {
        try { parsed = JSON.parse(stdout); } catch { parsed = null; }
      }
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) Object.assign(result, parsed);
      else if (stdout) result.output = stdout;
      if (stderr) result.stderr = stderr;
      if (!("ok" in result)) result.ok = rc === 0;
      resolve(result);
    });
    if (stdinText !== undefined) {
      // A child that exits before reading its input raises EPIPE here; like
      // communicate() swallowing BrokenPipeError, the exit code tells the story.
      child.stdin!.on("error", () => {});
      child.stdin!.end(stdinText, "utf8");
    }
  });
}

/** repr() of an exception, as server.py's {exc!r} renders it: Name('message'). */
const describe = (e: unknown) => (e instanceof Error ? `${e.name}(${pyRepr(e.message)})` : String(e));

function requireDir(folder: string): Report | null {
  if (!isDir(folder)) return { ok: false, error: `folder not found: ${folder}`, exit_code: 1 };
  return null;
}

// --------------------------------------------------------------------------- //
// Input models (pydantic-compatible schemas and validation)                   //
// --------------------------------------------------------------------------- //

interface Field {
  name: string;
  kind: "str" | "optional_str" | "int";
  description: string;
  minLength?: number;
  minimum?: number;
  maximum?: number;
  default?: unknown;
}

interface Model {
  title: string;
  fields: Field[];
  /** Model-level check after field validation (pydantic model_validator). */
  check?: (value: Record<string, unknown>) => string | null;
}

/** pydantic's default field title: "file_path" -> "File Path". */
const fieldTitle = (name: string) => name.split("_").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");

/** The JSON Schema pydantic generates for a model (keys sorted on the wire anyway). */
function modelSchema(model: Model): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  const required: string[] = [];
  for (const f of model.fields) {
    const prop: Record<string, unknown> = {};
    if (f.kind === "optional_str") prop.anyOf = [{ type: "string" }, { type: "null" }];
    if ("default" in f) prop.default = f.default;
    prop.description = f.description;
    if (f.maximum !== undefined) prop.maximum = f.maximum;
    if (f.minLength !== undefined) prop.minLength = f.minLength;
    if (f.minimum !== undefined) prop.minimum = f.minimum;
    prop.title = fieldTitle(f.name);
    if (f.kind === "str") prop.type = "string";
    if (f.kind === "int") prop.type = "integer";
    properties[f.name] = prop;
    if (!("default" in f)) required.push(f.name);
  }
  const schema: Record<string, unknown> = { additionalProperties: false, properties };
  if (required.length) schema.required = required;
  schema.title = model.title;
  schema.type = "object";
  return schema;
}

class ValidationError extends Error {}

/**
 * Validate and coerce one model, mirroring pydantic's lax mode for these field
 * types: str_strip_whitespace, extra="forbid", int from integral numbers or
 * numeric strings. Error text follows pydantic's "N validation error(s) for ...".
 */
function validateModel(toolName: string, model: Model, input: unknown): Record<string, unknown> {
  const problems: [string, string][] = [];
  let value = input;
  // FastMCP parses a JSON-encoded string argument before validating it.
  if (typeof value === "string") {
    try { value = JSON.parse(value); } catch { /* reported below */ }
  }
  const out: Record<string, unknown> = {};
  if (value === undefined) {
    problems.push(["params", "Field required"]);
  } else if (typeof value !== "object" || value === null || Array.isArray(value)) {
    problems.push(["params", `Input should be a valid dictionary or instance of ${model.title}`]);
  } else {
    const obj = value as Record<string, unknown>;
    for (const f of model.fields) {
      const loc = `params.${f.name}`;
      let v = obj[f.name];
      if (v === undefined) {
        if ("default" in f) { out[f.name] = f.default; continue; }
        problems.push([loc, "Field required"]);
        continue;
      }
      if (f.kind === "optional_str" && v === null) { out[f.name] = null; continue; }
      if (f.kind === "str" || f.kind === "optional_str") {
        if (typeof v !== "string") { problems.push([loc, "Input should be a valid string"]); continue; }
        v = (v as string).replace(/^\p{White_Space}+|\p{White_Space}+$/gu, "");
        const len = [...(v as string)].length;
        if (f.minLength !== undefined && len < f.minLength) {
          problems.push([loc, `String should have at least ${f.minLength} character${f.minLength === 1 ? "" : "s"}`]);
          continue;
        }
      } else {
        // pydantic lax int: bools, integral floats, and numeric strings (trailing ".0" allowed).
        let n: number | null = null;
        let why = "Input should be a valid integer";
        if (typeof v === "boolean") n = v ? 1 : 0;
        else if (typeof v === "number") {
          if (Number.isInteger(v)) n = v;
          else why += ", got a number with a fractional part";
        } else if (typeof v === "string") {
          const m = /^\p{White_Space}*([-+]?\d+)(?:\.0*)?\p{White_Space}*$/u.exec(v);
          if (m) n = Number.parseInt(m[1]!, 10);
          else why += ", unable to parse string as an integer";
        }
        if (n === null) { problems.push([loc, why]); continue; }
        if (f.minimum !== undefined && n < f.minimum) { problems.push([loc, `Input should be greater than or equal to ${f.minimum}`]); continue; }
        if (f.maximum !== undefined && n > f.maximum) { problems.push([loc, `Input should be less than or equal to ${f.maximum}`]); continue; }
        v = n;
      }
      out[f.name] = v;
    }
    for (const key of Object.keys(obj)) {
      if (!model.fields.some((f) => f.name === key)) problems.push([`params.${key}`, "Extra inputs are not permitted"]);
    }
    if (problems.length === 0 && model.check) {
      const message = model.check(out);
      if (message) problems.push(["params", `Value error, ${message}`]);
    }
  }
  if (problems.length) {
    const head = `${problems.length} validation error${problems.length === 1 ? "" : "s"} for ${toolName}Arguments`;
    throw new ValidationError([head, ...problems.map(([loc, msg]) => `${loc}\n  ${msg}`)].join("\n"));
  }
  return out;
}

const FolderInput: Model = {
  title: "FolderInput",
  fields: [{
    name: "folder", kind: "str", minLength: 1,
    description: "Absolute path to the geeky-plan planning folder (contains kanban.md, tasks/, etc.)",
  }],
};

const TaskSchemaInput: Model = {
  title: "TaskSchemaInput",
  fields: [
    { name: "folder", kind: "optional_str", default: null, description: "Planning folder; validates every tasks/Tx-*.md in it" },
    { name: "file", kind: "optional_str", default: null, description: "A single task .md file to validate instead of a whole folder" },
  ],
  check: (v) => (!v.folder && !v.file ? "provide either 'folder' or 'file'" : null),
};

const KanbanInput: Model = {
  title: "KanbanInput",
  fields: [
    { name: "folder", kind: "str", minLength: 1, description: "Planning folder containing kanban.md and tasks/" },
    { name: "wip", kind: "int", default: 3, minimum: 1, maximum: 50, description: "In Progress WIP cap before a warning is raised" },
  ],
};

const DodInput: Model = {
  title: "DodInput",
  fields: [
    { name: "folder", kind: "str", minLength: 1, description: "Planning folder" },
    { name: "task", kind: "str", minLength: 2, description: "Task id to check, e.g. 'T3'" },
  ],
};

const CommitInput: Model = {
  title: "CommitInput",
  fields: [{ name: "message", kind: "str", minLength: 1, description: "The full commit message text to validate" }],
};

const FrozenInput: Model = {
  title: "FrozenInput",
  fields: [{
    name: "file_path", kind: "str", minLength: 1,
    description: "Path an agent intends to edit; checks whether it is a frozen planning artifact",
  }],
};

// --------------------------------------------------------------------------- //
// Tools                                                                       //
// --------------------------------------------------------------------------- //

interface Tool {
  name: string;
  title: string;
  description: string;
  model: Model;
  handler: (params: Record<string, unknown>) => Promise<Report>;
}

export const TOOLS: Tool[] = [
  {
    name: "geeky_validate_planning_folder",
    title: "Validate planning folder",
    description: `Verify a geeky-plan folder contains the artifacts geeky-implement needs.

Checks for implementation-plan.md, kanban.md, references.md, handoff.md, and a
non-empty tasks/ folder; reports missing recommended files (feature-specification.md,
draft.md) as warnings.

Args:
    params (FolderInput): folder = absolute path to the planning folder.

Returns:
    dict: { ok: bool, task_count: int, missing_required: [str],
            missing_recommended: [str], summary: str, exit_code: int }.
    ok=false with exit_code=1 means the folder is incomplete (a validation
    failure, not a server error).
`,
    model: FolderInput,
    handler: async ({ folder }) => requireDir(folder as string)
      ?? run(join(SCRIPTS, "validate-planning-folder.mts"), ["--path", folder as string, "--json"]),
  },
  {
    name: "geeky_validate_task_schema",
    title: "Validate task-file schema",
    description: `Validate that task files carry the required template sections.

The plan->implement boundary gate. Required sections: Task Name, In scope,
Dependencies, Acceptance Criteria, Tests/Validation, Priority. Missing
recommended sections (Context, Module/System, Technical Notes, Definition of
Done, Estimate) are warnings.

Args:
    params (TaskSchemaInput): provide 'folder' (validate all tasks/Tx-*.md) OR
        'file' (validate one task .md).

Returns:
    dict: { ok: bool, results: [ { file, ok, missing_required, missing_recommended } ],
            exit_code: int }.
`,
    model: TaskSchemaInput,
    handler: async ({ folder, file }) => {
      let args: string[];
      if (file) {
        args = ["--file", file as string, "--json"];
      } else {
        const err = requireDir((folder as string | null) || "");
        if (err) return err;
        args = ["--path", folder as string, "--json"];
      }
      return run(join(SCRIPTS, "validate-task-schema.mts"), args);
    },
  },
  {
    name: "geeky_validate_kanban",
    title: "Validate kanban integrity",
    description: `Check kanban.md integrity against the tasks/ folder.

Errors (ok=false): a task file placed in no lane (untracked); a task in more
than one lane (ambiguous). Warnings: dangling references, WIP cap exceeded,
missing lane headings.

Args:
    params (KanbanInput): folder = planning folder; wip = In Progress cap (default 3).

Returns:
    dict: { ok: bool, errors: [str], warnings: [str], lane_counts: {lane:int},
            summary: str, exit_code: int }.
`,
    model: KanbanInput,
    handler: async ({ folder, wip }) => requireDir(folder as string)
      ?? run(join(SCRIPTS, "validate-kanban.mts"), ["--path", folder as string, "--wip", String(wip), "--json"]),
  },
  {
    name: "geeky_check_dod",
    title: "Check Definition of Done",
    description: `Verify a task's Definition of Done before it moves to Done.

Asserts: a per-task notes file tasks/<ID>-*.notes.md exists; <ID> is in the Done
lane; handoff.md mentions <ID>. Also returns the task's Tests/Validation block so
the caller can re-run it ("verify, don't trust").

Args:
    params (DodInput): folder = planning folder; task = task id (e.g. 'T3').

Returns:
    dict: { ok: bool, task: str, errors: [str], warnings: [str], lanes: [str],
            validation_block: str, exit_code: int }.
`,
    model: DodInput,
    handler: async ({ folder, task }) => requireDir(folder as string)
      ?? run(join(SCRIPTS, "check-dod.mts"), ["--path", folder as string, "--task", task as string, "--json"]),
  },
  {
    name: "geeky_check_commit",
    title: "Check commit message",
    description: `Validate a commit message: Conventional Commits subject + a task reference.

Errors (ok=false): subject not 'type(scope): summary'; subject > 72 chars; no
task reference. A bare T<n> without a 'Tasks:' line is a warning, not an error.

Args:
    params (CommitInput): message = full commit message text.

Returns:
    dict: { ok: bool, subject: str, errors: [str], warnings: [str], exit_code: int }.
`,
    model: CommitInput,
    handler: async ({ message }) => run(join(SCRIPTS, "check-commit.mts"), ["--json"], message as string),
  },
  {
    name: "geeky_check_frozen_artifact",
    title: "Check if a path is a frozen artifact",
    description: `Check whether a file path is a frozen geeky-plan planning artifact.

Use this as a pre-edit guardrail (the in-process equivalent of the PreToolUse
guard hook): call it before editing a file inside a planning folder. Frozen:
implementation-plan.md, feature-specification.md, draft.md, references.md, and
tasks/Tx-*.md bodies (but NOT tasks/Tx-*.notes.md).

Args:
    params (FrozenInput): file_path = the path an agent intends to edit.

Returns:
    dict: { frozen: bool, file_path: str, reason: str|None, exit_code: int }.
    frozen=true means do not edit — surface plan issues via kanban Blocked + handoff.md.
`,
    model: FrozenInput,
    handler: async ({ file_path: filePath }) => {
      const payload = JSON.stringify({ tool_name: "Edit", tool_input: { file_path: filePath } });
      const res = await run(join(HOOKS, "guard-planning-contract.mts"), ["--mode", "block", "--exit-code"], payload);
      const frozen = res.exit_code === 2;
      return {
        frozen,
        file_path: filePath,
        reason: frozen ? (res.stderr ?? null) : null,
        exit_code: res.exit_code ?? null,
      };
    },
  },
];

function toolListing(tool: Tool): Record<string, unknown> {
  return {
    name: tool.name,
    description: tool.description,
    inputSchema: {
      $defs: { [tool.model.title]: modelSchema(tool.model) },
      properties: { params: { $ref: `#/$defs/${tool.model.title}` } },
      required: ["params"],
      title: `${tool.name}Arguments`,
      type: "object",
    },
    outputSchema: { additionalProperties: true, title: `${tool.name}DictOutput`, type: "object" },
    annotations: { title: tool.title, ...READONLY },
  };
}

async function callTool(name: unknown, args: unknown): Promise<Record<string, unknown>> {
  const tool = TOOLS.find((t) => t.name === name);
  const failure = (text: string) => ({ content: [{ type: "text", text }], isError: true });
  if (!tool) return failure(`Unknown tool: ${String(name)}`);
  try {
    const argObj = (typeof args === "object" && args !== null ? args : {}) as Record<string, unknown>;
    const params = validateModel(tool.name, tool.model, argObj.params);
    const result = await tool.handler(params);
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }], structuredContent: result, isError: false };
  } catch (e) {
    return failure(`Error executing tool ${tool.name}: ${e instanceof Error ? e.message : String(e)}`);
  }
}

// --------------------------------------------------------------------------- //
// JSON-RPC 2.0 over stdio                                                     //
// --------------------------------------------------------------------------- //

type RpcMessage = { jsonrpc?: string; id?: string | number | null; method?: string; params?: Record<string, unknown> };

function send(message: Record<string, unknown>): void {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...message }) + "\n");
}

export async function handle(message: RpcMessage): Promise<Record<string, unknown> | null> {
  const { id, method, params } = message;
  if (typeof method !== "string") return null;            // a response, or junk: nothing to answer
  if (id === undefined || id === null) return null;        // notification
  switch (method) {
    case "initialize": {
      const requested = params?.protocolVersion;
      const protocolVersion = typeof requested === "string" && PROTOCOL_VERSIONS.includes(requested)
        ? requested : PROTOCOL_VERSIONS[0];
      return {
        id,
        result: {
          protocolVersion,
          capabilities: {
            experimental: {},
            prompts: { listChanged: false },
            resources: { subscribe: false, listChanged: false },
            tools: { listChanged: false },
          },
          serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        },
      };
    }
    case "ping":
      return { id, result: {} };
    case "tools/list":
      return { id, result: { tools: TOOLS.map(toolListing) } };
    case "tools/call":
      return { id, result: await callTool(params?.name, params?.arguments) };
    case "prompts/list":
      return { id, result: { prompts: [] } };
    case "resources/list":
      return { id, result: { resources: [] } };
    case "resources/templates/list":
      return { id, result: { resourceTemplates: [] } };
    default:
      return { id, error: { code: -32601, message: "Method not found" } };
  }
}

function serve(): void {
  let buffer = "";
  const onLine = (line: string) => {
    if (!line.trim()) return;
    let message: unknown;
    try {
      message = JSON.parse(line);
    } catch {
      send({ id: null, error: { code: -32700, message: "Parse error" } });
      return;
    }
    if (typeof message !== "object" || message === null || Array.isArray(message)) {
      send({ id: null, error: { code: -32600, message: "Invalid Request" } });
      return;
    }
    handle(message as RpcMessage).then(
      (reply) => { if (reply) send(reply); },
      (e) => {
        const id = (message as RpcMessage).id ?? null;
        send({ id, error: { code: -32603, message: e instanceof Error ? e.message : String(e) } });
      },
    );
  };
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk: string) => {
    buffer += chunk;
    let newline: number;
    while ((newline = buffer.indexOf("\n")) !== -1) {
      onLine(buffer.slice(0, newline));
      buffer = buffer.slice(newline + 1);
    }
  });
  process.stdin.on("end", () => onLine(buffer));
}

if (isMain(import.meta.url)) serve();
