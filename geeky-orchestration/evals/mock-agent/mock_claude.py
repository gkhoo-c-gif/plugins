"""Mock Claude Code CLI for dry-running the geeky eval pipeline without model calls.

Accepts the arguments run-baseline.ps1 passes to `claude` (unknown flags are tolerated):
  -p <prompt> --session-id <id> --plugin-dir <dir> --settings <f> --strict-mcp-config
  --mcp-config <f> --permission-mode <m> --output-format stream-json --verbose [--model m]
plus its own:
  --mock-scenario <name[,name...]>   (or env MOCK_SCENARIO; default: happy)
  --mock-case <case id>              (or env MOCK_CASE; optional, names the stub package)
  --mock-summary <path>              (or env MOCK_SUMMARY_PATH; default: <cwd>/.git/mock-summary.json,
                                     or <cwd>/mock-summary.json outside a repo)

It writes claude-style stream-json to stdout and a session transcript with timestamps to
$CLAUDE_CONFIG_DIR/projects/<cwd, non-alphanumerics -> '-'>/<session-id>.jsonl, so the runner
and analyze-run.py work unchanged. Every action is real (files, gate scripts, pytest, git, pwsh,
MCP handshake); each check is a `{"type":"system","subtype":"mock_check"}` line, and a
`mock_summary` line plus the summary file close the run. Stdlib only.

Scenarios (composable, comma-separated; run in the order given):
  env-probe  PATH, python/pytest versions, git identity, TEMP/workspace writability
  grants     workspace rw, plugin dir scandir/read, config dir rw, TEMP write + exec
  pwsh       exit-code fidelity, profile/config warnings on stderr
  python     python and pytest from PATH, a real pytest run in TEMP
  git        identity, safe.directory (status in cwd), a real commit in a TEMP repo
  gates      every plugin gate script, python and pwsh, on a fixture package (+ a negative)
  mcp        start each configured MCP server, initialize + tools/list over stdio
  network    unauthenticated HTTPS reachability (MOCK_NETWORK_URL)
  happy      full staged workflow: spec-research > geeky-plan > plan-review > geeky-implement > impl-review
  gate-fail  plans with a kanban error that it does not fix, then stops
  crash      exits 3 mid-run with no result event
  refusal    ends with stop_reason content_filter, as a provider content filter does; the same
             session resumed with --resume then completes (exercises run-baseline -ResumeOnRefusal)
  slow       sleeps MOCK_SLOW_SECONDS (default 600) without output
  bg-wait    ends its turn with a background subagent running; like claude -p, waits for it when
             CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0, otherwise prints the stop message on stderr
  telemetry  runs the --settings Stop hooks on this transcript, as claude does after a turn, and checks
             the Langfuse hook log reports processed turns; added after the other scenarios whenever
             the settings declare a Stop hook (the run's final Stop and SessionEnd hooks always fire)
  all        env-probe,grants,pwsh,python,git,gates,mcp
Env knobs: MOCK_LATENCY_MS (simulated model latency per call, default 40).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path

VERSION = "0.0.0-mock"
DEFAULT_MODEL = "claude-mock-5-5"
SUB_MODEL = "claude-mock-haiku"
ALL = ["env-probe", "grants", "pwsh", "python", "git", "gates", "mcp"]
KNOWN = set(ALL) | {"network", "happy", "c0-full", "gate-fail", "crash", "slow", "bg-wait", "telemetry", "refusal"}
STAGES = ["spec-research", "geeky-plan", "plan-review", "geeky-implement", "impl-review"]
GATES = ["validate-planning-folder", "validate-task-schema", "validate-kanban", "check-dod", "check-commit"]
BUILTIN_SKILLS = ["debug", "simplify", "code-review", "verify"]
BUILTIN_AGENTS = ["general-purpose", "Explore", "Plan"]
TOOLS = ["Task", "Bash", "Edit", "Glob", "Grep", "PowerShell", "Read", "Skill", "Write", "Agent"]


class Crash(Exception):
    pass


# ----------------------------------------------------------------------------- args

def parse_args(argv: list[str]) -> dict:
    opts = {"prompt": None, "session_id": None, "resume": None, "plugin_dirs": [], "settings": None,
            "mcp_configs": [], "permission_mode": "default", "model": None,
            "scenario": None, "case": None, "summary": None, "disallowed": [], "unknown": []}
    takes = {"-p": "prompt", "--print": "prompt", "--session-id": "session_id", "--resume": "resume",
             "--plugin-dir": "plugin_dirs", "--settings": "settings", "--mcp-config": "mcp_configs",
             "--permission-mode": "permission_mode", "--model": "model",
             "--disallowedTools": "disallowed", "--disallowed-tools": "disallowed",
             "--mock-scenario": "scenario", "--mock-case": "case", "--mock-summary": "summary",
             "--output-format": None, "--input-format": None, "--max-turns": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        key, val = (a.split("=", 1) + [None])[:2] if a.startswith("--") and "=" in a else (a, None)
        if key in takes:
            if val is None:
                i += 1
                val = argv[i] if i < len(argv) else ""
            dest = takes[key]
            if dest in ("plugin_dirs", "mcp_configs"):
                opts[dest].append(val)
            elif dest == "disallowed":
                opts[dest] += re.split(r"[,\s]+", val.strip())
            elif dest:
                opts[dest] = val
        else:
            opts["unknown"].append(a)
        i += 1
    opts["session_id"] = opts["session_id"] or opts["resume"]
    return opts


def expand_scenarios(spec: str) -> list[str]:
    out: list[str] = []
    for name in re.split(r"[,\s]+", spec.strip()):
        if not name:
            continue
        out.extend(ALL if name == "all" else [name])
    return out or ["happy"]


# ----------------------------------------------------------------------------- helpers

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def project_slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_exe(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if name == "pwsh":
        for c in (r"C:\Program Files\PowerShell\7\pwsh.exe",):
            if os.path.exists(c):
                return c
    return None


def run(argv: list[str], cwd: str | None = None, timeout: int = 300, env: dict | None = None,
        stdin: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, env=env,
                           input=stdin.encode() if stdin is not None else None,
                           stdin=None if stdin is not None else subprocess.DEVNULL)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except FileNotFoundError as e:
        return 127, "", f"not found: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as e:
        return 126, "", f"OSError: {e}"


def display(argv: list[str]) -> str:
    def q(a: str) -> str:
        return f'"{a}"' if (not a or re.search(r"[\s;&|'`]", a)) else a
    exe = os.path.basename(argv[0])
    exe = re.sub(r"\.exe$", "", exe, flags=re.I)
    return " ".join([exe] + [q(a) for a in argv[1:]])


def rmtree(path) -> None:
    """Best-effort delete, including git's read-only object files."""
    def unlock(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if path and os.path.exists(path):
        shutil.rmtree(path, onerror=unlock)


def scratch_dir(prefix: str) -> Path:
    """A TEMP subfolder made with a plain mkdir. tempfile.mkdtemp() is avoided on purpose: on
    Python 3.13+ for Windows its mode 0o700 becomes a user-only ACL, which an MXC AppContainer
    cannot then write into (reported by the grants.temp_mkdtemp check)."""
    d = Path(tempfile.gettempdir()) / f"{prefix}{uuid.uuid4().hex[:12]}"
    d.mkdir(parents=True)
    return d


def pkg_name(case: str | None) -> str:
    if not case:
        return "mockpkg"
    name = re.sub(r"^c\d+-", "", case)
    name = re.sub(r"\W", "_", name).strip("_").lower()
    return name or "mockpkg"


# ----------------------------------------------------------------------------- transcript

class Transcript:
    """Writes stream-json to stdout and the session transcript, and keeps check results."""

    def __init__(self, opts: dict, cwd: Path, scenarios: list[str]):
        self.opts = opts
        self.cwd = cwd
        self.sid = opts["session_id"] or str(uuid.uuid4())
        self.model = opts["model"] or DEFAULT_MODEL
        self.scenarios = scenarios
        self.latency = int(os.environ.get("MOCK_LATENCY_MS", "40")) / 1000
        self.calls = 0
        self.sub_calls = 0
        self.checks: list[dict] = []
        self.current = None
        self.parent_uuid = None
        self.started = time.time()
        cfg = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        self.session_path = Path(cfg) / "projects" / project_slug(str(cwd)) / f"{self.sid}.jsonl"
        self.session = None
        try:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            self.session = open(self.session_path, "a", encoding="utf-8", newline="\n")
        except OSError as e:
            self.session_error = str(e)
        else:
            self.session_error = None
        self.out = sys.stdout.buffer

    # raw writers
    def stream(self, event: dict) -> None:
        self.out.write((dumps(event) + "\n").encode("ascii"))
        self.out.flush()

    def record(self, entry: dict) -> None:
        if not self.session:
            return
        base = {"parentUuid": self.parent_uuid, "isSidechain": False, "userType": "external",
                "entrypoint": "sdk-cli", "cwd": str(self.cwd), "sessionId": self.sid,
                "version": VERSION, "gitBranch": "", "timestamp": now_iso()}
        base.update(entry)
        base.setdefault("uuid", str(uuid.uuid4()))
        if not base["isSidechain"]:
            self.parent_uuid = base["uuid"]
        self.session.write(dumps(base) + "\n")
        self.session.flush()

    # conversation
    def user_prompt(self, text: str) -> None:
        self.record({"type": "queue-operation", "operation": "enqueue", "content": text[:2000]})
        self.record({"type": "user", "message": {"role": "user", "content": text}})

    def assistant(self, blocks: list[dict], parent: str | None = None, agent: dict | None = None) -> None:
        time.sleep(self.latency)
        sub = parent is not None
        if sub:
            self.sub_calls += 1
        else:
            self.calls += 1
        n = self.calls + self.sub_calls
        msg_id = f"msg_mock_{n:04d}"
        model = SUB_MODEL if sub else self.model
        usage = {"input_tokens": 2, "cache_creation_input_tokens": 100 + n, "cache_read_input_tokens": 2000 + 10 * n,
                 "output_tokens": 20 + n % 7, "service_tier": "standard"}
        if "c0-full" in self.scenarios:
            usage = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0, "output_tokens": 0}
        for block in blocks:
            msg = {"model": model, "id": msg_id, "type": "message", "role": "assistant", "content": [block],
                   "stop_reason": None, "stop_sequence": None, "usage": usage}
            u = str(uuid.uuid4())
            ev = {"type": "assistant", "message": msg, "parent_tool_use_id": parent, "session_id": self.sid,
                  "uuid": u, "timestamp": now_iso()}
            if agent:
                ev.update(agent)
            self.stream(ev)
            entry = {"type": "assistant", "message": msg, "isSidechain": sub, "uuid": u}
            if agent:
                entry["agentId"] = agent.get("agent_id")
            self.record(entry)

    def say(self, text: str, parent: str | None = None, agent: dict | None = None) -> None:
        self.assistant([{"type": "text", "text": text}], parent, agent)

    def tool(self, name: str, inp: dict, execute, parent: str | None = None, agent: dict | None = None):
        """Emit tool_use, run execute() -> (content, is_error, extra), emit tool_result."""
        tid = self.tool_use(name, inp, parent, agent)
        try:
            content, is_error, extra = execute(tid)
        except Crash:
            raise
        except Exception as e:  # a tool failure is reported, never fatal
            content, is_error, extra = f"{type(e).__name__}: {e}", True, None
        self.tool_result(tid, content, is_error, parent)
        return extra if extra is not None else (content, is_error)

    def tool_use(self, name: str, inp: dict, parent: str | None = None, agent: dict | None = None) -> str:
        tid = "toolu_mock_" + uuid.uuid4().hex[:20]
        self.assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp}], parent, agent)
        return tid

    def tool_result(self, tid: str, content: str, is_error: bool, parent: str | None = None) -> None:
        res = {"type": "tool_result", "tool_use_id": tid, "content": content}
        if is_error:
            res["is_error"] = True
        u = str(uuid.uuid4())
        summary = ("Error: " + content) if is_error else content
        self.stream({"type": "user", "message": {"role": "user", "content": [res]}, "parent_tool_use_id": parent,
                     "session_id": self.sid, "uuid": u, "timestamp": now_iso(), "tool_use_result": summary})
        self.record({"type": "user", "message": {"role": "user", "content": [res]}, "isSidechain": parent is not None,
                     "uuid": u, "toolUseResult": summary})

    # checks
    def check(self, name: str, status: str, detail: str = "") -> bool:
        item = {"name": name, "status": status, "detail": detail[:2000], "scenario": self.current}
        self.checks.append(item)
        self.stream({"type": "system", "subtype": "mock_check", "session_id": self.sid,
                     "uuid": str(uuid.uuid4()), **item})
        line = detail.splitlines()[0] if detail else ""
        print(f"MOCK CHECK {status.upper():4} {name}: {line}", file=sys.stderr, flush=True)
        return status != "fail"

    def summary(self) -> dict:
        counts = {s: sum(1 for c in self.checks if c["status"] == s) for s in ("pass", "fail", "warn", "info")}
        return {"schema": "geeky-eval.mock-summary/1", "session_id": self.sid, "cwd": str(self.cwd),
                "scenarios": self.scenarios, "ok": counts["fail"] == 0, "counts": counts,
                "failed": [c["name"] for c in self.checks if c["status"] == "fail"],
                "session_file": str(self.session_path), "checks": self.checks,
                **({"c0_full": self.c0_full, "model_calls": 0, "label": "SCRIPTED MOCK"}
                   if hasattr(self, "c0_full") else {})}


# ----------------------------------------------------------------------------- agent context

class Agent:
    def __init__(self, t: Transcript, opts: dict, cwd: Path):
        self.t = t
        self.opts = opts
        self.cwd = cwd
        self.plugin = Path(opts["plugin_dirs"][0]) if opts["plugin_dirs"] else None
        self.case = opts["case"] or os.environ.get("MOCK_CASE") or None
        m = re.search(r"docs/spec-[\w.-]+/?", opts["prompt"] or "")
        self.folder = (m.group(0).rstrip("/") if m else "docs/spec-001-feature").replace("/", os.sep)
        self.python = find_exe("python") or sys.executable
        self.pwsh = find_exe("pwsh")
        self.git = find_exe("git") or "git"
        self.temp = Path(tempfile.gettempdir())

    # tool wrappers --------------------------------------------------------
    def say(self, text: str, parent=None, agent=None) -> None:
        self.t.say(text, parent, agent)

    def sh(self, argv: list[str], cwd: str | None = None, desc: str = "", parent=None, agent=None,
           timeout: int = 300, env: dict | None = None):
        """A shell step, reported as a PowerShell tool call; returns (rc, combined output)."""
        cwd = cwd or str(self.cwd)

        def go(_tid=None):
            rc, out, err = run(argv, cwd=cwd, timeout=timeout, env=env)
            text = (out + (("\n" + err) if err.strip() else "")).strip()
            if rc:
                text = (text + f"\nExit code {rc}").strip()
            return text or "(no output)", rc != 0, (rc, out, err)
        rc, out, err = self.t.tool("PowerShell", {"command": display(argv), "description": desc}, go, parent, agent)
        return rc, (out + err)

    def write(self, rel: str, content: str, parent=None, agent=None) -> bool:
        path = self.cwd / rel

        def go(_tid=None):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))  # == write_text(newline="\n"); 3.9 has no newline=
            return f"File written successfully at: {path}", False, None
        _, err = self.t.tool("Write", {"file_path": str(path), "content": content}, go, parent, agent)
        return not err

    def edit(self, rel: str, old: str, new: str, parent=None, agent=None) -> bool:
        path = self.cwd / rel

        def go(_tid=None):
            text = path.read_text(encoding="utf-8")
            if old not in text:
                return "<tool_use_error>String to replace not found in file.</tool_use_error>", True, None
            path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            return f"The file {path} has been updated successfully.", False, None
        _, err = self.t.tool("Edit", {"file_path": str(path), "old_string": old, "new_string": new}, go, parent, agent)
        return not err

    def read(self, path: Path, parent=None, agent=None) -> str | None:
        def go(_tid=None):
            text = path.read_text(encoding="utf-8", errors="replace")
            body = "\n".join(f"{i:6}\t{line}" for i, line in enumerate(text.splitlines()[:60], 1))
            return body or "(empty file)", False, (text, False)
        text, err = self.t.tool("Read", {"file_path": str(path)}, go, parent, agent)
        return None if err else text

    def skill(self, name: str) -> bool:
        full = f"geeky-orchestration:{name}"

        def go(_tid=None):
            if not (self.plugin and (self.plugin / "skills" / name / "SKILL.md").is_file()):
                return f"<tool_use_error>Unknown skill: {full}</tool_use_error>", True, None
            return f"Launching skill: {full}", False, None
        _, err = self.t.tool("Skill", {"skill": full, "args": self.folder.replace(os.sep, "/") + "/"}, go)
        self.t.check(f"skill.{name}", "fail" if err else "pass",
                     "skill not found in plugin dir" if err else "SKILL.md present and launched")
        return not err

    def subagent(self, subagent_type: str, description: str, body) -> str:
        """Agent tool call whose body runs as a sidechain; body(parent, agent_meta) -> summary text."""
        def go(tid):
            meta = {"agent_id": "a" + uuid.uuid4().hex[:16], "subagent_type": subagent_type,
                    "task_description": description}
            return body(tid, meta), False, None
        content, _ = self.t.tool("Agent", {"description": description, "subagent_type": subagent_type,
                                           "prompt": f"{description} for {self.folder}"}, go)
        return content

    # gate helpers -----------------------------------------------------------
    def gate_argv(self, gate: str, folder: Path, extra: list[str], ps: bool) -> list[str] | None:
        if not self.plugin:
            return None
        if ps:
            if not self.pwsh:
                return None
            script = self.plugin / "scripts" / f"{gate}.ps1"
            args = {"check-commit": extra, "check-dod": ["-Path", str(folder), "-Task", extra[0]] if extra else []}
            return [self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)] + \
                args.get(gate, ["-Path", str(folder)])
        script = self.plugin / "scripts" / f"{gate}.py"
        args = {"check-commit": extra, "check-dod": ["--path", str(folder), "--task", extra[0]] if extra else []}
        return [self.python, str(script)] + args.get(gate, ["--path", str(folder)])

    def gate(self, gate: str, folder: Path, extra: list[str] | None = None, ps: bool = False, expect: int = 0,
             name: str | None = None, parent=None, agent=None) -> bool:
        argv = self.gate_argv(gate, folder, extra or [], ps)
        label = name or f"gate.{gate}{'.ps1' if ps else '.py'}"
        if not argv:
            return self.t.check(label, "fail", "no plugin dir" if not self.plugin else "pwsh not found")
        rc, out = self.sh(argv, desc=f"Run {gate}", parent=parent, agent=agent)
        first = out.strip().splitlines()[0] if out.strip() else ""
        return self.t.check(label, "pass" if rc == expect else "fail", f"exit {rc} (expected {expect}): {first}")


# ----------------------------------------------------------------------------- planning package

FALLBACK_TEMPLATE = """Task Name:
[Clear action-based title]

Context:
[Why this task exists, what feature/user flow it supports]

In scope:
- [Bullet what is being changed and where, with file paths/modules]

## Dependencies:
* [Tx-task-name if any, otherwise None]

## Acceptance Criteria:
* [Behavioral / observable outcome 1]

Tests/Validation Before Next Task:
- [Exact command(s) that must pass before the next task is started]

Priority:
[P0/P1/P2]
"""


def task_body(template: str, title: str, pkg: str, deps: str, criteria: str, command: str) -> str:
    fills = {
        "[Clear action-based title]": title,
        "[Why this task exists, what feature/user flow it supports]": f"Mock-agent dry run: {title.lower()}.",
        "[Bullet what is being changed and where, with file paths/modules]": f"`{pkg}/` and `tests/`",
        "[Bullet what is intentionally NOT being changed in this task]": "The real feature logic (mock stub only).",
        "[Where this belongs — package, layer, or subsystem]": f"`{pkg}` package",
        "[Tx-task-name if any, otherwise None]": deps,
        "[Any constraints, invariants, gotchas the implementer must know]": "Standard library only.",
        "[Links to relevant sections of implementation-plan.md / feature-specification.md by heading]":
            "implementation-plan.md, Tasks",
        "[Behavioral / observable outcome 1]": criteria,
        "[Behavioral / observable outcome 2]": "pytest exits 0.",
        "[Deployment evidence, only when required by the approved task scope]": "No deployment.",
        "[Exact command(s) that must pass before the next task is started]": f"`{command}`",
        "[E.g. `python -m compileall app`, `pytest tests/test_x.py -q`]": f"`python -m compileall {pkg}`",
        "[S/M/L]": "S",
        "[P0/P1/P2]": "P0",
    }
    for k, v in fills.items():
        template = template.replace(k, v)
    return template


def kanban_text(lanes: dict[str, list[str]], titles: dict[str, str]) -> str:
    out = ["# Kanban", ""]
    for lane in ("Backlog", "Ready", "In Progress", "In Review", "Blocked", "Done"):
        out.append(f"## {lane}")
        out.extend(f"- {tid} {titles[tid]}" for tid in lanes.get(lane, []))
        out.append("")
    out += ["## Validation", "- [ ] pytest passes", ""]
    return "\n".join(out)


def package_files(plugin: Path | None, pkg: str, lanes: dict[str, list[str]]) -> dict[str, str]:
    tpl_path = plugin / "templates" / "task_template.md" if plugin else None
    template = tpl_path.read_text(encoding="utf-8") if tpl_path and tpl_path.is_file() else FALLBACK_TEMPLATE
    test_cmd = "python -m pytest -q -p no:cacheprovider tests"
    titles = {"T1": "Scaffold the package", "T2": "Add smoke tests"}
    return {
        "implementation-plan.md": f"# Implementation Plan\n\nMock plan for `{pkg}`.\n\n## Phase 1\n\n- T1 {titles['T1']}\n- T2 {titles['T2']}\n",
        "draft.md": "# Draft\n\nMock draft.\n",
        "references.md": "# References\n\n- brief.md\n- contract.md\n",
        "handoff.md": "# Handoff\n\n- geeky-plan: package created by the mock agent.\n",
        "kanban.md": kanban_text(lanes, titles),
        "tasks/T1-scaffold-package.md": task_body(template, titles["T1"], pkg, "None",
                                                  f"`python -m {pkg} --help` exits 0.", f"python -m {pkg} --help"),
        "tasks/T2-smoke-tests.md": task_body(template, titles["T2"], pkg, "T1-scaffold-package",
                                             "Smoke tests pass.", test_cmd),
    }


STUB_INIT = '"""{pkg}: mock-agent stub package."""\n__version__ = "0.0.0"\n'
STUB_MAIN = '''import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog="{pkg}", description="mock-agent stub")
    parser.add_argument("args", nargs="*")
    parser.parse_args(argv)
    print("{pkg}: mock stub, not implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''
STUB_TEST = '''import subprocess
import sys


def test_help_exits_zero():
    assert subprocess.run([sys.executable, "-m", "{pkg}", "--help"], capture_output=True).returncode == 0


def test_import():
    import {pkg}
    assert {pkg}.__version__
'''


# ----------------------------------------------------------------------------- scenarios

def sc_env_probe(a: Agent) -> None:
    t = a.t
    if a.pwsh:
        rc, out = a.sh([a.pwsh, "-NoProfile", "-NonInteractive", "-Command",
                        "$env:PATH; 'PSVersion=' + $PSVersionTable.PSVersion; 'USER=' + [Environment]::UserName"],
                       desc="Report PATH and PowerShell version")
        t.check("env.pwsh", "pass" if rc == 0 else "fail",
                " | ".join(out.strip().splitlines()[-2:]) if rc == 0 else out.strip()[-1500:])
    else:
        t.check("env.pwsh", "fail", "pwsh not found on PATH")
    rc, out = a.sh([a.python, "-c", "import sys; print(sys.version.split()[0], sys.executable)"],
                   desc="Report python version")
    t.check("env.python", "pass" if rc == 0 else "fail", f"{a.python}: {out.strip()}")
    rc, out = a.sh([a.python, "-m", "pytest", "--version"], desc="Report pytest version")
    t.check("env.pytest", "pass" if rc == 0 else "fail", out.strip())
    rc, out = a.sh([a.git, "var", "GIT_AUTHOR_IDENT"], desc="Report git identity")
    t.check("env.git_identity", "pass" if rc == 0 else "fail", out.strip())
    # Tools write caches under HOME; a HOME inside the workspace puts .cache/ in the contestant's repo.
    home = Path(os.path.expanduser("~")).resolve()
    inside = home == a.cwd.resolve() or a.cwd.resolve() in home.parents
    t.check("env.home_outside_workspace", "fail" if inside else "pass", str(home))
    for label, d in (("TEMP", a.temp), ("workspace", a.cwd)):
        p = Path(d) / f".mock-probe-{os.getpid()}.tmp"
        try:
            p.write_text("x")
            p.unlink()
            t.check(f"env.writable.{label}", "pass", str(d))
        except OSError as e:
            t.check(f"env.writable.{label}", "fail", f"{d}: {e}")
    # Claude Code's Bash tool runs zsh, which writes here-documents to $TMPPREFIX (default /tmp/zsh). A sandbox that
    # cannot write there fails every heredoc ("can't create temp file for here document"), and the shell's exit
    # status can hide it, so compare the output rather than the status.
    zsh = "/bin/zsh"
    if os.name == "posix" and Path(zsh).exists():
        rc, out = a.sh([zsh, "-c", "x=$(cat <<'EOF'\nheredoc-ok\nEOF\n); echo \"[$x]\""], desc="Run a zsh here-document")
        t.check("env.heredoc", "pass" if "[heredoc-ok]" in out else "fail",
                f"TMPPREFIX={os.environ.get('TMPPREFIX', '<unset>')}; " + out.strip()[-300:])
    ceiling = os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS")
    t.check("env.bg_wait_ceiling", "pass" if ceiling == "0" else "fail",
            f"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS={ceiling}" + ("" if ceiling == "0" else
            " (claude -p stops background subagents 10 min after the main turn ends)"))
    keys = ["CLAUDE_CONFIG_DIR", "TEMP", "VIRTUAL_ENV", "XDG_CONFIG_HOME", "CLAUDE_CODE_DISABLE_WINDOWS_SHELL_LAUNCHER",
            "GIT_CONFIG_COUNT", "GIT_CONFIG_VALUE_0", "GIT_AUTHOR_NAME", "LOCALAPPDATA", "USERPROFILE"]
    t.check("env.vars", "info", "; ".join(f"{k}={os.environ.get(k, '')}" for k in keys))
    t.check("env.path", "info", os.environ.get("PATH", ""))


def sc_grants(a: Agent) -> None:
    t = a.t
    # workspace read/write
    p = a.cwd / f".mock-grant-{os.getpid()}.tmp"
    try:
        p.write_text("probe", encoding="utf-8")
        ok = p.read_text(encoding="utf-8") == "probe"
        p.unlink()
        t.check("grants.workspace_rw", "pass" if ok else "fail", str(a.cwd))
    except OSError as e:
        t.check("grants.workspace_rw", "fail", f"{a.cwd}: {e}")
    # plugin dir: the scandir Claude does for skills/agents/commands
    if not a.plugin:
        t.check("grants.plugin_dir", "fail", "no --plugin-dir given")
    else:
        for sub, required in (("skills", True), ("agents", True), ("commands", False)):
            d = a.plugin / sub
            if not d.exists() and not required:
                t.check(f"grants.plugin_scandir.{sub}", "info", "absent")
                continue
            try:
                names = sorted(e.name for e in os.scandir(d))
                t.check(f"grants.plugin_scandir.{sub}", "pass", f"{len(names)} entries: {', '.join(names)}")
            except OSError as e:
                t.check(f"grants.plugin_scandir.{sub}", "fail", f"{d}: {e}")
        try:
            n = 0
            for s in os.scandir(a.plugin / "skills"):
                (Path(s.path) / "SKILL.md").read_text(encoding="utf-8")
                n += 1
            t.check("grants.plugin_read", "pass", f"read {n} SKILL.md files")
        except OSError as e:
            t.check("grants.plugin_read", "fail", str(e))
    # config dir (where the session transcript goes)
    t.check("grants.config_dir_rw", "fail" if t.session_error else "pass",
            t.session_error or str(t.session_path.parent))
    for flag in ("settings",):
        f = a.opts.get(flag)
        if f and not f.lstrip().startswith("{"):
            try:
                Path(f).read_text(encoding="utf-8")
                t.check(f"grants.{flag}_readable", "pass", f)
            except OSError as e:
                t.check(f"grants.{flag}_readable", "fail", f"{f}: {e}")
    for f in a.opts["mcp_configs"]:
        if not f.lstrip().startswith("{"):
            try:
                Path(f).read_text(encoding="utf-8")
                t.check("grants.mcp_config_readable", "pass", f)
            except OSError as e:
                t.check("grants.mcp_config_readable", "fail", f"{f}: {e}")
    # TEMP write + exec (uv's trampoline build failed in the container's own TEMP)
    tmp = a.temp / f"mock-exec-{os.getpid()}"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        t.check("grants.temp_write", "pass", str(tmp))
        if os.name == "nt":
            script = tmp / "probe.cmd"
            script.write_text("@echo tempexec-ok\r\n", encoding="ascii")
            cmd = ["cmd.exe", "/d", "/c", str(script)]
            src = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "hostname.exe"
            exe = tmp / "probe-hostname.exe"
        else:
            script = tmp / "probe.sh"
            script.write_text("#!/bin/sh\necho tempexec-ok\n", encoding="ascii")
            script.chmod(0o755)
            cmd = [str(script)]
            src = Path(shutil.which("true") or "/bin/true")
            exe = tmp / "probe-true"
        rc, out, err = run(cmd)
        t.check("grants.temp_exec_script", "pass" if rc == 0 and "tempexec-ok" in out else "fail",
                f"exit {rc}: {(out + err).strip()}")
        shutil.copy2(src, exe)
        rc, out, err = run([str(exe)])
        t.check("grants.temp_exec_binary", "pass" if rc == 0 else "fail", f"exit {rc}: {(out + err).strip()}")
    except OSError as e:
        t.check("grants.temp_write", "fail", f"{tmp}: {e}")
    finally:
        rmtree(tmp)
    # tempfile.mkdtemp (used by pytest's tmp_path and many libraries) sets mode 0o700
    md = None
    try:
        md = tempfile.mkdtemp(prefix="mock-mkdtemp-")
        (Path(md) / "x.txt").write_text("x", encoding="utf-8")
        t.check("grants.temp_mkdtemp", "pass", md)
    except OSError as e:
        t.check("grants.temp_mkdtemp", "fail",
                f"mkdtemp() dir is not writable (Python {sys.version.split()[0]} mode-0o700 ACL vs AppContainer?): {e}")
    finally:
        if md:
            rmtree(md)


def sc_pwsh(a: Agent) -> None:
    t = a.t
    if not a.pwsh:
        t.check("pwsh.found", "fail", "pwsh not found on PATH")
        return
    t.check("pwsh.found", "pass", a.pwsh)
    base = [a.pwsh, "-NoProfile", "-NonInteractive", "-Command"]
    for label, cmd, want in (("exit_0", "Write-Output ok", 0),
                             ("exit_7", "exit 7", 7),
                             ("native_exit_3", ("& cmd.exe /d /c exit 3" if os.name == "nt" else "& sh -c 'exit 3'")
                              + "; exit $LASTEXITCODE", 3),
                             ("cmdlet_error", "$ErrorActionPreference='Stop'; Get-Item "
                              + ("C:\\no\\such\\path" if os.name == "nt" else "/no/such/path"), 1)):
        rc, out = a.sh(base + [cmd], desc=f"pwsh exit-code check {label}")
        t.check(f"pwsh.{label}", "pass" if rc == want else "fail", f"exit {rc} (expected {want})")
    # Profile and config: run the way a user shell would, with the profile, and look at stderr.
    rc, out, err = run([a.pwsh, "-NonInteractive", "-Command", "Write-Output profile-ok"])
    a.t.tool("PowerShell", {"command": "pwsh -NonInteractive -Command 'Write-Output profile-ok'",
                            "description": "pwsh with profile"},
             lambda _tid: ((out + err).strip() or "(no output)", rc != 0, None))
    status = "fail" if rc else ("warn" if err.strip() else "pass")
    t.check("pwsh.profile_stderr", status, f"exit {rc}; stderr: {err.strip()[:800] or '(empty)'}")
    rc, out, err = run(base + ["$PROFILE; Test-Path $PROFILE; [Environment]::GetFolderPath('MyDocuments')"])
    t.check("pwsh.profile_path", "info", (out + err).strip())


def sc_python(a: Agent) -> None:
    t = a.t
    t.check("python.which", "pass" if find_exe("python") else "fail", find_exe("python") or "python not on PATH")
    rc, out = a.sh([a.python, "--version"], desc="python --version")
    t.check("python.version", "pass" if rc == 0 else "fail", out.strip())
    rc, out = a.sh([a.python, "-m", "pytest", "--version"], desc="pytest --version")
    t.check("python.pytest_version", "pass" if rc == 0 else "fail", out.strip())
    # A contestant checking the contract's minimum Python calls python3.X directly.
    if os.name != "nt":
        names = sorted({f.name for d in os.environ.get("PATH", "").split(os.pathsep) if os.path.isdir(d)
                        for f in Path(d).iterdir() if re.fullmatch(r"python3\.\d+", f.name)})
        missing = [n for n in names if run([n, "-m", "pytest", "--version"])[0] != 0]
        t.check("python.versioned_pytest", "fail" if missing else "pass",
                ("pytest missing for " + ", ".join(missing) + " (run scripts/install-agent-pythons.ps1)") if missing else ", ".join(names))
    d = scratch_dir("mock-pytest-")
    try:
        (d / "test_probe.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
        rc, out = a.sh([a.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_probe.py"], cwd=str(d),
                       desc="Run a probe pytest")
        t.check("python.pytest_run", "pass" if rc == 0 and "1 passed" in out else "fail",
                f"exit {rc}: {out.strip().splitlines()[-1] if out.strip() else ''}")
        (d / "test_tmp_path.py").write_text(
            "def test_tmp_path(tmp_path):\n    (tmp_path / 'f.txt').write_text('x')\n", encoding="utf-8")
        rc, out = a.sh([a.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_tmp_path.py"], cwd=str(d),
                       desc="Run a pytest that uses tmp_path")
        err = next((ln for ln in out.splitlines() if "Error" in ln), "")
        t.check("python.pytest_tmp_path", "pass" if rc == 0 else "fail",
                f"exit {rc}: {out.strip().splitlines()[-1] if out.strip() else ''} {err.strip()[:300]}")
    finally:
        rmtree(d)


def sc_git(a: Agent) -> None:
    t = a.t
    rc, out = a.sh([a.git, "--version"], desc="git --version")
    t.check("git.version", "pass" if rc == 0 else "fail", out.strip())
    rc, out = a.sh([a.git, "var", "GIT_AUTHOR_IDENT"], desc="git author identity")
    t.check("git.identity", "pass" if rc == 0 else "fail", out.strip())
    rc, out = a.sh([a.git, "status", "--short", "--branch"], desc="git status in workspace")
    dubious = "dubious ownership" in out
    t.check("git.workspace_status", "fail" if rc or dubious else "pass",
            ("safe.directory not trusted: " if dubious else "") + out.strip()[:500])
    # A throwaway repo in TEMP and one under the workspace's .git/ (inside the workspace grant,
    # invisible to `git status`): tells a TEMP-specific failure from a general one.
    places = [("temp", lambda: scratch_dir("mock-git-"))]
    if (a.cwd / ".git").is_dir():
        places.append(("workspace", lambda: _mk(a.cwd / ".git" / f"mock-git-probe-{uuid.uuid4().hex[:8]}")))
    for label, make in places:
        d = None
        try:
            d = make()
            env = dict(os.environ)
            env["GIT_CONFIG_COUNT"] = str(int(env.get("GIT_CONFIG_COUNT", "0")) + 1)
            n = int(env["GIT_CONFIG_COUNT"]) - 1
            env[f"GIT_CONFIG_KEY_{n}"] = "safe.directory"
            env[f"GIT_CONFIG_VALUE_{n}"] = str(d).replace("\\", "/")
            steps = [[a.git, "init", "-q"], [a.git, "add", "-A"],
                     [a.git, "commit", "-q", "-m", "chore(mock): probe commit\n\nTasks: T0"],
                     [a.git, "log", "--format=%H %an <%ae>", "-1"]]
            (d / "probe.txt").write_text("probe\n", encoding="utf-8")
            for argv in steps:
                rc, out = a.sh(argv, cwd=str(d), desc=f"git commit probe ({label})", env=env)
                if rc:
                    break
            t.check(f"git.commit_{label}", "pass" if rc == 0 else "fail", f"{d}: {out.strip()[:500]}")
        except OSError as e:
            t.check(f"git.commit_{label}", "fail", f"{d}: {e}")
        finally:
            if d:
                rmtree(d)


def _mk(d: Path) -> Path:
    d.mkdir(parents=True)
    return d


def sc_gates(a: Agent) -> None:
    t = a.t
    if not a.plugin:
        t.check("gates", "fail", "no --plugin-dir given")
        return
    root = scratch_dir("mock-gates-")
    try:
        good = root / "good"
        files = package_files(a.plugin, "mockpkg", {"Done": ["T1", "T2"]})
        files["handoff.md"] += "- T1 done.\n- T2 done.\n"
        files["feature-specification.md"] = "# Feature Specification\n\nFixture.\n"
        files["tasks/T1-scaffold-package.notes.md"] = "# T1 notes\n"
        files["tasks/T2-smoke-tests.notes.md"] = "# T2 notes\n"
        for rel, body in files.items():
            (good / rel).parent.mkdir(parents=True, exist_ok=True)
            (good / rel).write_text(body, encoding="utf-8")
        bad = root / "bad"
        shutil.copytree(good, bad)
        (bad / "kanban.md").write_text(kanban_text({"Ready": ["T2"], "Done": ["T1", "T2"]},
                                                   {"T1": "Scaffold", "T2": "Smoke"}), encoding="utf-8")
        msg = root / "COMMIT_EDITMSG"
        msg.write_text("feat(mock): add stub\n\nTasks: T1\n", encoding="utf-8")
        for ps in (False, True):
            for gate in GATES:
                extra = [str(msg)] if gate == "check-commit" else (["T1"] if gate == "check-dod" else [])
                a.gate(gate, good, extra, ps=ps)
            a.gate("validate-kanban", bad, ps=ps, expect=1,
                   name=f"gate.validate-kanban{'.ps1' if ps else '.py'}.rejects_bad")
    finally:
        rmtree(root)


def mcp_servers(a: Agent) -> dict:
    servers = {}
    sources = a.opts["mcp_configs"] or ([str(a.plugin / ".mcp.json")] if a.plugin and (a.plugin / ".mcp.json").is_file() else [])
    for src in sources:
        try:
            text = src if src.lstrip().startswith("{") else Path(src).read_text(encoding="utf-8-sig")
            servers.update(json.loads(text).get("mcpServers", {}))
        except (OSError, ValueError) as e:
            servers[f"<unreadable {src}>"] = {"error": str(e)}
    root = str(a.plugin) if a.plugin else ""
    return json.loads(json.dumps(servers).replace("${CLAUDE_PLUGIN_ROOT}", root.replace("\\", "\\\\")))


def mcp_handshake(cfg: dict, timeout: float = 90) -> tuple[bool, str]:
    if "error" in cfg:
        return False, cfg["error"]
    cmd = shutil.which(cfg.get("command", "")) or cfg.get("command", "")
    env = dict(os.environ)
    env.update(cfg.get("env") or {})
    try:
        p = subprocess.Popen([cmd] + list(cfg.get("args") or []), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=cfg.get("cwd") or None, env=env)
    except OSError as e:
        return False, f"spawn failed: {e}"
    lines: queue.Queue = queue.Queue()
    errbuf: list[bytes] = []
    threading.Thread(target=lambda: [lines.put(ln) for ln in p.stdout], daemon=True).start()
    threading.Thread(target=lambda: [errbuf.append(ln) for ln in p.stderr], daemon=True).start()
    deadline = time.time() + timeout

    def send(obj):
        p.stdin.write((json.dumps(obj) + "\n").encode())
        p.stdin.flush()

    def wait(mid):
        while time.time() < deadline:
            try:
                ln = lines.get(timeout=0.5)
            except queue.Empty:
                if p.poll() is not None:
                    return None
                continue
            try:
                msg = json.loads(ln)
            except ValueError:
                continue
            if msg.get("id") == mid:
                return msg
        return None
    try:
        t0 = time.time()
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "mock-claude", "version": VERSION}}})
        init = wait(1)
        if not init or "result" not in init:
            err = b"".join(errbuf).decode("utf-8", "replace")[-800:]
            return False, f"no initialize response (exit {p.poll()}); stderr: {err}"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tl = wait(2)
        if not tl or "result" not in tl:
            return False, "no tools/list response"
        names = [x.get("name") for x in tl["result"].get("tools", [])]
        server = init["result"].get("serverInfo", {})
        return True, f"{server.get('name')} {server.get('version')}: {len(names)} tools in {time.time() - t0:.1f}s: {', '.join(names)}"
    except OSError as e:
        return False, f"pipe error: {e}"
    finally:
        try:
            p.stdin.close()
            p.wait(timeout=5)
        except Exception:
            p.kill()


def sc_mcp(a: Agent) -> None:
    servers = mcp_servers(a)
    if not servers:
        a.t.check("mcp.config", "fail", "no MCP servers configured (--mcp-config or plugin .mcp.json)")
        return
    for name, cfg in servers.items():
        holder = {}

        def go(_tid=None, cfg=cfg):
            ok, detail = mcp_handshake(cfg)
            holder["r"] = (ok, detail)
            return detail, not ok, None
        a.t.tool("ToolSearch", {"query": f"mcp server {name}", "max_results": 10}, go)
        ok, detail = holder.get("r", (False, "not run"))
        a.t.check(f"mcp.{name}.handshake", "pass" if ok else "fail", detail)


def sc_network(a: Agent) -> None:
    url = os.environ.get("MOCK_NETWORK_URL", "https://api.anthropic.com/")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            a.t.check("network.https", "pass", f"{url}: HTTP {r.status}")
    except urllib.error.HTTPError as e:
        a.t.check("network.https", "pass", f"{url}: HTTP {e.code} (reachable)")
    except Exception as e:
        a.t.check("network.https", "fail", f"{url}: {e}")


def sc_crash(a: Agent) -> None:
    a.say("Starting work.")
    a.sh([a.python, "--version"], desc="python --version")
    a.t.check("crash.simulated", "info", "exiting 3 without a result event")
    raise Crash()


def sc_slow(a: Agent) -> None:
    secs = float(os.environ.get("MOCK_SLOW_SECONDS", "600"))
    a.say(f"Working (silent for {secs:g}s).")
    time.sleep(secs)
    a.t.check("slow.completed", "info", f"slept {secs:g}s without being stopped")


def sc_bg_wait(a: Agent) -> None:
    # claude -p stops background tasks still running CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS (default 10 min)
    # after the main agent's last turn; 0 waits indefinitely and resumes the main agent when they finish.
    a.say("The implementation run is in the background. Waiting for its completion report.")
    if os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS") == "0":
        a.subagent("general-purpose", "Run geeky-implement full", lambda parent, meta: "Background run done.")
        a.say("Background run finished; continuing.")
        a.t.check("bg-wait.resumed", "pass", "waited for the background subagent")
    else:
        print('Background tasks still running 10m after the last turn (subagent "Run geeky-implement full"); '
              "stopping them. Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely.",
              file=sys.stderr, flush=True)
        a.t.check("bg-wait.stopped", "info", "ceiling not 0: background subagent stopped")


def plan_stage(a: Agent, folder: str, pkg: str, lanes: dict) -> Path:
    for rel, body in package_files(a.plugin, pkg, lanes).items():
        a.write(os.path.join(folder, rel), body)
    return a.cwd / folder


def commit(a: Agent, paths: list[str], message: str, name: str) -> bool:
    msg = a.temp / f"mock-commit-{os.getpid()}.txt"
    msg.write_text(message + "\n", encoding="utf-8")
    try:
        a.gate("check-commit", a.cwd, [str(msg)], name=f"{name}.check-commit")
        rc, out = a.sh([a.git, "add", "--"] + paths, desc="Stage files")
        if rc == 0:
            rc, out = a.sh([a.git, "commit", "-q", "-F", str(msg)], desc="Commit")
        return a.t.check(f"{name}.commit", "pass" if rc == 0 else "fail", f"exit {rc}: {out.strip()[:300]}")
    finally:
        msg.unlink(missing_ok=True)


def sc_happy(a: Agent, broken_kanban: bool = False) -> None:
    t = a.t
    pkg = pkg_name(a.case)
    folder = a.folder
    if broken_kanban and (a.cwd / folder).exists():
        folder = os.path.join("docs", "spec-002-gate-fail")
    tag = "gate-fail" if broken_kanban else "happy"
    fdir = a.cwd / folder
    a.say(f"Running the geeky workflow end to end on {folder} (package `{pkg}`).")
    a.sh([a.git, "status", "--short"], desc="Inspect the workspace")
    seed = [p for p in ("brief.md", "contract.md") if (a.cwd / p).is_file()]
    for p in seed:
        a.read(a.cwd / p)

    # 1. spec-research
    a.skill("spec-research")

    def research(parent, meta):
        for p in seed:
            a.read(a.cwd / p, parent, meta)
        a.say("Findings: the contract is authoritative; a stub package is enough for a dry run.", parent, meta)
        return "Research report: 1 finding, no blockers."
    a.subagent("geeky-orchestration:code-explorer", "Codebase analyst", research)
    a.write(os.path.join(folder, "feature-specification.md"),
            f"# Feature Specification\n\nMock specification for `{pkg}`.\n\nSources: {', '.join(seed) or 'none'}.\n")
    a.write(os.path.join(folder, "README.md"), f"# {os.path.basename(folder)}\n\nMock feature folder.\n")

    # 2. geeky-plan
    a.skill("geeky-plan")
    lanes = {"Ready": ["T1", "T2"]}
    if broken_kanban:
        lanes = {"Ready": ["T1", "T2"], "Done": ["T2"]}
    plan_stage(a, folder, pkg, lanes)
    a.gate("validate-planning-folder", fdir, name=f"{tag}.validate-planning-folder")
    a.gate("validate-task-schema", fdir, name=f"{tag}.validate-task-schema")
    if broken_kanban:
        ok = a.gate("validate-kanban", fdir, expect=1, name="gate-fail.validate-kanban.rejects")
        a.say("validate-kanban failed: T2 is in two lanes. Stopping; the kanban is left as is.")
        t.check("gate-fail.stopped", "pass" if ok else "fail", "workflow stopped at the kanban gate")
        return
    a.gate("validate-kanban", fdir, name="happy.validate-kanban.plan")
    commit(a, [folder], "docs(plan): add mock planning package\n\nTasks: T1, T2", "happy.plan")

    # 3. plan-review
    a.skill("plan-review")

    def plan_review(parent, meta):
        a.read(fdir / "implementation-plan.md", parent, meta)
        a.gate("validate-task-schema", fdir, ps=True, name="happy.plan-review.validate-task-schema.ps1",
               parent=parent, agent=meta)
        return "Plan review: PASS, no findings."
    a.subagent("geeky-orchestration:code-reviewer", "Plan review", plan_review)
    a.edit(os.path.join(folder, "handoff.md"), "by the mock agent.\n",
           "by the mock agent.\n- plan-review: PASS.\n")

    # 4. geeky-implement
    a.skill("geeky-implement")
    a.gate("validate-planning-folder", fdir, name="happy.implement.validate-planning-folder")
    kb = os.path.join(folder, "kanban.md")
    a.edit(kb, "## Ready\n- T1 Scaffold the package\n- T2 Add smoke tests\n", "## Ready\n")
    a.edit(kb, "## In Progress\n", "## In Progress\n- T1 Scaffold the package\n- T2 Add smoke tests\n")
    a.gate("validate-kanban", fdir, name="happy.validate-kanban.in-progress")
    test_cmd = [a.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]

    def coder(parent, meta):
        a.write(f"{pkg}/__init__.py", STUB_INIT.format(pkg=pkg), parent, meta)
        a.write(f"{pkg}/__main__.py", STUB_MAIN.format(pkg=pkg), parent, meta)
        a.write(f"tests/test_{pkg}_smoke.py", STUB_TEST.format(pkg=pkg), parent, meta)
        rc, out = a.sh(test_cmd, desc="Run tests", parent=parent, agent=meta)
        return f"T1, T2 implemented. pytest exit {rc}."
    a.subagent("geeky-orchestration:geeky-coder", "Implement T1-T2", coder)
    rc, out = a.sh(test_cmd, desc="Re-run validation")
    last = out.strip().splitlines()[-1] if out.strip() else ""
    t.check("happy.pytest", "pass" if rc == 0 else "fail", f"exit {rc}: {last}")
    a.write(os.path.join(folder, "tasks", "T1-scaffold-package.notes.md"), "# T1 notes\n\nStub package.\n")
    a.write(os.path.join(folder, "tasks", "T2-smoke-tests.notes.md"), f"# T2 notes\n\npytest exit {rc}.\n")
    a.edit(kb, "## In Progress\n- T1 Scaffold the package\n- T2 Add smoke tests\n", "## In Progress\n")
    a.edit(kb, "## Done\n", "## Done\n- T1 Scaffold the package\n- T2 Add smoke tests\n")
    a.edit(os.path.join(folder, "handoff.md"), "- plan-review: PASS.\n",
           "- plan-review: PASS.\n- T1 done: stub package.\n- T2 done: smoke tests.\n")
    a.gate("validate-kanban", fdir, name="happy.validate-kanban.done")
    for tid in ("T1", "T2"):
        a.gate("check-dod", fdir, [tid], name=f"happy.check-dod.{tid}")
    commit(a, [pkg, "tests", folder], f"feat({pkg}): add mock stub package and smoke tests\n\nTasks: T1, T2",
           "happy.implement")

    # 5. impl-review
    a.skill("impl-review")

    def impl_review(parent, meta):
        a.read(a.cwd / pkg / "__main__.py", parent, meta)
        a.sh(test_cmd, desc="Reviewer test run", parent=parent, agent=meta)
        return "Implementation review: PASS."
    a.subagent("geeky-orchestration:code-reviewer", "Implementation review", impl_review)
    a.edit(os.path.join(folder, "handoff.md"), "- T2 done: smoke tests.\n",
           "- T2 done: smoke tests.\n- impl-review: PASS.\n")
    commit(a, [folder], "docs(handoff): record impl-review result\n\nTasks: T1, T2", "happy.impl-review")
    a.say("All stages ran. impl-review: PASS.")


def sc_c0_full(a: Agent) -> None:
    from c0_fixture import execute

    def report(name, item):
        if "file_path" in item:
            path = Path(item["file_path"]).resolve()
            rel = path.relative_to(a.cwd.resolve()).as_posix()
            return a.write(rel, item["content"])
        if "skill" in item:
            path = a.plugin / "skills" / item["skill"] / "SKILL.md"

            def read_skill(_tid=None):
                content = path.read_text(encoding="utf-8")
                return f"SCRIPTED MOCK read {path} ({len(content)} characters)", False, None

            _, is_error = a.t.tool("Skill", {"skill": item["skill"], "args": item["args"]}, read_skill)
            return not is_error
        if "argv" in item:
            a.t.tool("PowerShell", {"command": display(item["argv"]),
                     "description": "SCRIPTED MOCK " + name},
                     lambda _tid: (item["stdout"] + item["stderr"], item["exit_code"] != 0, None))
        else:
            a.say("SCRIPTED MOCK " + name + ": " + item["detail"])

    def write_kanban(path, content):
        rel = path.resolve().relative_to(a.cwd.resolve()).as_posix()
        return a.write(rel, content)

    a.t.c0_full = execute(a.cwd, a.plugin, a.opts["prompt"] or "", report, write_kanban)
    a.t.check("c0-full.complete", "pass", a.t.c0_full["completion_report"])


def settings_hooks(opts: dict) -> dict:
    try:
        return json.loads(Path(opts["settings"]).read_text(encoding="utf-8-sig")).get("hooks") or {}
    except (TypeError, OSError, ValueError):
        return {}


def run_hooks(a: Agent, event: str) -> list[tuple[int, str]]:
    """Run the --settings hooks for one event the way claude does: payload on stdin, the hook's shell."""
    payload = json.dumps({"session_id": a.t.sid, "transcript_path": str(a.t.session_path), "cwd": str(a.cwd),
                          "hook_event_name": event, "stop_hook_active": False})
    results = []
    for group in settings_hooks(a.opts).get(event) or []:
        for hook in group.get("hooks") or []:
            if hook.get("type") != "command":
                continue
            if hook.get("shell") == "powershell":
                argv = [a.pwsh or "powershell", "-NoProfile", "-NonInteractive", "-Command", hook["command"]]
            else:
                argv = ["bash", "-c", hook["command"]]
            try:
                r = subprocess.run(argv, input=payload, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=hook.get("timeout", 600))
                results.append((r.returncode, (r.stdout + r.stderr).strip()))
            except (OSError, subprocess.TimeoutExpired) as e:
                results.append((-1, f"{type(e).__name__}: {e}"))
    return results


def sc_telemetry(a: Agent) -> None:
    t = a.t
    if not settings_hooks(a.opts).get("Stop"):
        t.check("telemetry.configured", "info", "no Stop hook in --settings; telemetry off")
        return
    t.check("telemetry.thinking_summaries", "pass" if json.loads(Path(a.opts["settings"]).read_text(encoding="utf-8-sig"))
            .get("showThinkingSummaries") is True else "fail", "showThinkingSummaries in --settings")
    state = os.environ.get("CC_LANGFUSE_STATE_DIR")
    log = Path(state) / "langfuse_hook.log" if state else None
    offset = log.stat().st_size if log and log.exists() else 0
    # The turn so far has every scenario's tool calls; close it the way a model reply would.
    t.assistant([{"type": "thinking", "thinking": "Mock thinking summary for the telemetry probe.", "signature": "mock"},
                 {"type": "text", "text": "Telemetry probe: this turn ends here so the Stop hook uploads it."}])
    # The hook exits 0 even when an upload fails, so its output decides. Delivery itself is confirmed on
    # the host, which reads the session back from Langfuse after the run (the turn may only flush at
    # SessionEnd while subagent results are pending).
    for rc, out in run_hooks(a, "Stop"):
        ok = rc == 0 and not re.search(r"(?i)\berror\b|traceback", out)
        t.check("telemetry.stop_hook", "pass" if ok else "fail", f"exit {rc}" + (f": {out[-1500:]}" if out else ""))
    if not log:
        t.check("telemetry.hook_log", "fail", "CC_LANGFUSE_STATE_DIR is not set")
        return
    text = log.read_text(encoding="utf-8", errors="replace")[offset:] if log.exists() else ""
    ok = "Processed " in text and not re.search(r"(?i)failed|traceback|disabled|\berror\b", text)
    t.check("telemetry.hook_log", "pass" if ok else "fail", text.strip()[-1500:] or f"no new lines in {log}")


def sc_refusal(a: Agent) -> None:
    resumed = bool(a.opts["resume"])
    a.t.refused = not resumed
    a.t.check("refusal.resumed" if resumed else "refusal.filtered", "pass",
              "resumed session completes" if resumed else "first invocation ends with stop_reason content_filter")


SCENARIOS = {"env-probe": sc_env_probe, "grants": sc_grants, "pwsh": sc_pwsh, "python": sc_python,
             "git": sc_git, "gates": sc_gates, "mcp": sc_mcp, "network": sc_network, "crash": sc_crash,
             "slow": sc_slow, "bg-wait": sc_bg_wait, "happy": sc_happy, "gate-fail": lambda a: sc_happy(a, broken_kanban=True),
             "telemetry": sc_telemetry, "refusal": sc_refusal, "c0-full": sc_c0_full}


# ----------------------------------------------------------------------------- init / result

def plugin_meta(plugin: Path | None) -> tuple[list, list, list]:
    if not plugin:
        return [], [], []
    name, version = plugin.name, None
    try:
        meta = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        name, version = meta.get("name", name), meta.get("version")
    except (OSError, ValueError):
        pass
    skills, agents = [], []
    try:
        skills = sorted(f"{name}:{e.name}" for e in os.scandir(plugin / "skills")
                        if (Path(e.path) / "SKILL.md").is_file())
    except OSError:
        pass  # like claude: an unreadable plugin dir loads no skills
    try:
        agents = sorted(f"{name}:{Path(e.name).stem}" for e in os.scandir(plugin / "agents") if e.name.endswith(".md"))
    except OSError:
        pass
    plugins = [{"name": name, "path": str(plugin), "source": f"{name}@inline", "version": version}]
    return skills, agents, plugins


def token_usage(scenarios: list[str], model: str) -> dict:
    s = zlib.crc32(",".join(scenarios).encode()) % 1000
    main = {"inputTokens": 1000 + s, "outputTokens": 20000 + 10 * s, "cacheReadInputTokens": 500000 + 100 * s,
            "cacheCreationInputTokens": 40000 + 10 * s}
    sub = {"inputTokens": 30000 + s, "outputTokens": 2000 + s, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}

    def cost(u, rate_in, rate_out):
        return round((u["inputTokens"] + u["cacheCreationInputTokens"] * 1.25 + u["cacheReadInputTokens"] * 0.1) * rate_in / 1e6
                     + u["outputTokens"] * rate_out / 1e6, 6)
    main["costUSD"] = cost(main, 3, 15)
    sub["costUSD"] = cost(sub, 1, 5)
    for u in (main, sub):
        u.update({"webSearchRequests": 0, "contextWindow": 1000000, "maxOutputTokens": 128000})
    return {model: main, SUB_MODEL: sub}


def main(argv: list[str]) -> int:
    opts = parse_args(argv)
    spec = opts["scenario"] or os.environ.get("MOCK_SCENARIO") or "happy"
    scenarios = expand_scenarios(spec)
    if "c0-full" in scenarios and (scenarios != ["c0-full"] or settings_hooks(opts)):
        raise ValueError("c0-full must run alone with no telemetry/hooks")
    if settings_hooks(opts).get("Stop") and "telemetry" not in scenarios:
        scenarios.append("telemetry")
    cwd = Path(os.getcwd())
    t = Transcript(opts, cwd, scenarios)
    a = Agent(t, opts, cwd)
    skills, agents, plugins = plugin_meta(a.plugin)
    servers = mcp_servers(a)
    t.stream({"type": "system", "subtype": "init", "cwd": str(cwd), "session_id": t.sid,
              "tools": [x for x in TOOLS if x not in opts["disallowed"]] + [f"mcp__{n}__probe" for n in servers],
              "mcp_servers": [{"name": n, "status": "pending", "source": "dynamic"} for n in servers],
              "model": t.model, "permissionMode": opts["permission_mode"],
              "slash_commands": skills + BUILTIN_SKILLS, "apiKeySource": "none", "claude_code_version": VERSION,
              "output_style": "default", "agents": BUILTIN_AGENTS + agents, "skills": BUILTIN_SKILLS + skills,
              "plugins": plugins, "uuid": str(uuid.uuid4()), "mock_scenarios": scenarios,
              "mock_unknown_args": opts["unknown"]})
    t.user_prompt(opts["prompt"] or "(no prompt)")
    if t.session_error:
        t.check("session.write", "fail", t.session_error)
    t.check("init.plugin_skills", "pass" if skills else "fail",
            f"{len(skills)} plugin skills: {', '.join(skills)}" if skills else "no plugin skills loaded")

    for name in scenarios:
        t.current = name
        fn = SCENARIOS.get(name)
        if not fn:
            t.check(f"scenario.{name}", "fail", f"unknown scenario (known: {', '.join(sorted(KNOWN | {'all'}))})")
            continue
        try:
            fn(a)
        except Crash:
            t.stream({"type": "system", "subtype": "mock_summary", "session_id": t.sid, **t.summary()})
            print("mock-claude: simulated crash", file=sys.stderr, flush=True)
            sys.stdout.flush()
            os._exit(3)
        except Exception as e:  # a bug in a scenario is a failed check, not a crash
            t.check(f"scenario.{name}.error", "fail", f"{type(e).__name__}: {e}")
    t.current = None

    summary = t.summary()
    default = cwd / ".git" / "mock-summary.json" if (cwd / ".git").is_dir() else cwd / "mock-summary.json"
    target = Path(opts["summary"] or os.environ.get("MOCK_SUMMARY_PATH") or default)
    try:
        target.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        summary["summary_file"] = str(target)
    except OSError as e:
        summary["summary_file_error"] = str(e)
    t.stream({"type": "system", "subtype": "mock_summary", "session_id": t.sid, **summary})
    failed = summary["failed"]
    text = (f"Mock run ({', '.join(scenarios)}): {summary['counts']['pass']} passed, {len(failed)} failed"
            + (f": {', '.join(failed)}" if failed else "."))
    if hasattr(t, "c0_full"):
        text = t.c0_full["completion_report"]
    t.say(text)
    # claude fires Stop after the final reply and SessionEnd on exit; the final turn uploads here.
    for event in ("Stop", "SessionEnd"):
        for rc, out in run_hooks(a, event):
            if rc:
                print(f"mock-claude: {event} hook exit {rc}: {out[-500:]}", file=sys.stderr, flush=True)
    model_usage = token_usage(scenarios, t.model)
    last = {"input_tokens": 6, "cache_creation_input_tokens": 1200, "cache_read_input_tokens": 40000,
            "output_tokens": 800, "output_tokens_details": {"thinking_tokens": 50}, "service_tier": "standard"}
    if "c0-full" in scenarios:
        model_usage = {}
        last = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0}
    t.stream({"type": "result", "subtype": "success", "is_error": bool(failed),
              "duration_ms": int((time.time() - t.started) * 1000),
              "duration_api_ms": int((t.calls + t.sub_calls) * t.latency * 1000),
              "num_turns": t.calls, "session_id": t.sid,
              **({"result": "The request was rejected because it was considered high risk", "stop_reason": "content_filter"}
                 if getattr(t, "refused", False) else {"result": text, "stop_reason": "end_turn"}),
              "total_cost_usd": round(sum(u["costUSD"] for u in model_usage.values()), 6),
              "usage": last, "modelUsage": model_usage, "permission_denials": [],
              "terminal_reason": "completed", "uuid": str(uuid.uuid4())})
    if t.session:
        t.session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
