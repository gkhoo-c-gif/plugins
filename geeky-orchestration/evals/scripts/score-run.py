"""Deterministic QACT scorer for a Claude Code run of the geeky-orchestration workflow.

Usage: python score-run.py <run-folder> [--manual <json>] [--no-rerun] [--out <dir>]
                                        [--workspace <dir>] [--ref-loc N]

Writes score.json and score.md into --out (default: the run folder). Exit 0 on success, 2 on a
bad run folder. Rules: docs/agent-eval-decisions.md (Scoring, Hard gates, Consistency formula,
Task-tier formula; D8, D27, D34, D53, D59, D60, D67, D68, D79, D80, D82, D86, D87, D88).
Composite = 0.35 Quality + 0.35 Adherence + 0.20 Consistency + 0.10 Task tier. L_ref = 2678 ms.

Inputs: run.json, analysis.json, stream.jsonl, session.jsonl and solution.bundle in the run folder;
the workspace named in run.json (cloned from solution.bundle into %TEMP% when it is gone); the
case's case.json and spec.md; the plugin's own gate scripts (scripts/*.py). Trace parsing reuses
analyze-run.py (gate outcomes, JSONL loading).

Judgement items are read from --manual: {"<check id>": {"pass": true|false, "note": "..."}}.
Without an entry a judge check is "needs_judge" and is left out of its denominator. A manual entry
also overrides a deterministic check with the same id (recorded as "manual override").

Check IDs (stable). Kind: det = deterministic, judge = from --manual.
  Adherence (passed / applicable, then hard-gate caps)
    adh.planning_folder          validate-planning-folder on the final folder                det
    adh.task_schema              validate-task-schema on the final folder                    det
    adh.kanban_final             validate-kanban on the final folder                         det
    adh.dod.<T>                  check-dod <T> on the final folder                           det
    adh.handoff_batch.<T>        handoff.md written naming <T> in its Done call or the next (D82) det
    adh.notes.<T>                tasks/<T>-*.notes.md exists and is non-empty                det
    adh.task_review.<T>          a reviewer subagent verdict during implement names <T> (EP:124) det
    adh.phase_verdict.P<k>       reviewer READY/PASS covering phase k AND recorded in handoff/notes (EP:158) det
    adh.gates_before_done.<T>    check-dod <T> + validate-kanban in the Done call or after the last code edit (D86) det
    adh.kanban_after_moves       every implement lane-move call has validate-kanban in it or the next call det
    adh.commit_msg.<sha7>        check-commit re-run on each geeky-implement commit (D79)    det
    adh.prescribed_msg.spec      spec-research commit subject as prescribed (D79)            det
    adh.prescribed_msg.plan_review  plan-review commit subject exact (D79)                   det
    adh.prescribed_msg.critical_fix impl-review critical-fix commit subject (n/a without critical) det
    adh.frozen_untouched         git diff <plan-review commit>..HEAD empty on frozen files   det
    adh.no_forbidden_git         no push/--force/--amend/--no-verify in tool INPUT commands  det
    adh.one_concern_per_commit   commits split one concern each                              judge
    adh.no_orchestrator_code_edits  no main-thread code/test writes in implement/impl-review (D87) det
    adh.final_report_claims      final report claims verified (D8, process claims)           judge
    adh.heartbeat_final          .heartbeat status done or paused                            det
    adh.run_summary              handoff.md has a Run Summary heading (EP:202)               det
  Consistency decision points (weight 2 = critical)
    dp01 research team: >=3 parallel researchers in spec-research                            det
    dp02 * spec commit prescribed message                                                    det
    dp03 PM review artifact present                                                          det
    dp04 plan-review subagents (>=2 dispatches)                                              det
    dp05 * plan-review commit exact message                                                  det
    (dp06, task event logs, was retired with the event-log requirement: D91)
    dp07 batching <=3 parallel (det) and justification noted (judge: dp07.justification)     det+judge
    dp08 In Progress lane move with timestamp for every task                                 det
    dp09 * task review for every task                                                        det
    dp10 * DoD + kanban gates before every Done (D86)                                        det
    dp11 * phase verdict from a phase review for every phase                                 det
    dp12 commit gate ran before each geeky-implement commit (D79)                            det
    dp13 one concern per commit, per phase                                                   judge
    dp14 handoff at each Done move (D82)                                                     det
    dp15 Run Summary appended                                                                det
    dp16 final review: >=3 domain reviewers in impl-review                                   det
    dp17a re-review after a critical fix (n/a without critical)                              judge
    dp17b fix commit prescribed message (n/a without critical)                               det
    dp18 .heartbeat finalized                                                                det
  Hard gates
    hg.oracle                    oracle has any failure -> Quality <= 40                     det
    hg.frozen                    frozen artifact modified -> Adherence <= 30                 det
    hg.forbidden_git             push/force/amend/--no-verify -> Adherence = 0               det
    hg.false_completion_claim    false completion claim (D8) -> Adherence <= 50              judge
  Review mapping (judge, only when a reviewer dispatch names no task): review_map.<call>    judge
Stdlib only.
"""
import argparse
import datetime as dt
import importlib.util
import hashlib
import shlex
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
SCRIPTS = PLUGIN / "scripts"
L_REF_MS = 2678
STAGES = ("spec-research", "geeky-plan", "plan-review", "geeky-implement", "impl-review")
WEIGHTS = {"Q": 0.35, "A": 0.35, "C": 0.20, "T": 0.10}
CRITICAL_DPS = {"dp02", "dp05", "dp09", "dp10", "dp11"}
PLAN_REVIEW_SUBJECT = "chore(plan): Plan review — align documents, fix sequencing"
SPEC_SUBJECT = re.compile(r"^docs: author feature specification in \S+$")
FIX_SUBJECT = re.compile(r"^fix\(spec\): Address \d+ critical review findings from impl-review$")
# Leading word of an executable Tests/Validation command. "python" takes an optional version suffix
# (python3, python3.11); a bare python\b cannot match python3 because 3 is a word character.
EXEC_HEAD = r"(?:python(?:\d+(?:\.\d+)*)?|py|pytest|pwsh|bash|sh|node|npm|uv)"
FROZEN = ("implementation-plan.md", "feature-specification.md", "draft.md", "references.md")


def _load_analyzer():
    spec = importlib.util.spec_from_file_location("analyze_run", HERE / "analyze-run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AR = _load_analyzer()


# ----------------------------------------------------------------------------- pure helpers

def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def task_key(t):
    m = re.match(r"T(\d+)", t)
    return int(m.group(1)) if m else 0


def expand_ids(text):
    """Task IDs named in text; ranges such as T1-T9, T1–T4 and lists such as T1,T2 are expanded."""
    ids = set()
    for a, b in re.findall(r"\bT(\d+)\s*(?:-|–|\.\.)\s*T(\d+)\b", text):
        ids.update(f"T{i}" for i in range(int(a), int(b) + 1))
    ids.update(re.findall(r"\bT\d+\b", text))
    return ids


def v_score(attempts, passed):
    """Task-tier V: 1 attempt 1.0, 2 0.75, 3 0.5, 4 0.25; 0 if the block never passes."""
    if not passed or attempts < 1:
        return 0.0
    return max(0.0, 1 - 0.25 * (attempts - 1))


def r_score(rounds):
    """Task-tier R from review rounds until PASS (None = never passed review)."""
    return {1: 1.0, 2: 0.5}.get(rounds, 0.0) if rounds else 0.0


def t_score(d_s, l_run_ms, timeout_s=1200, l_ref_ms=L_REF_MS):
    """Task-tier T: latency-normalized duration against the task timeout."""
    if d_s is None:
        return 0.0
    d_adj = d_s * (l_ref_ms / l_run_ms) if l_run_ms else d_s
    return max(0.0, 1 - d_adj / timeout_s)


def task_score(v, r, t, d):
    return 100 * (0.40 * v + 0.30 * r + 0.20 * t + 0.10 * d)


def task_weights(locs, attempts):
    """w_i = 0.5 LOC share + 0.5 attempt share; a zero total drops that half's share evenly."""
    n = len(locs)
    sl, sa = sum(locs), sum(attempts)
    return [0.5 * (l / sl if sl else 1 / n) + 0.5 * (a / sa if sa else 1 / n) for l, a in zip(locs, attempts)]


def levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def order_conformance(observed, expected=STAGES):
    obs = [s for i, s in enumerate(observed) if i == 0 or s != observed[i - 1]]
    longest = max(len(obs), len(expected)) or 1
    return 1 - levenshtein(obs, list(expected)) / longest


def dpc(points):
    """Decision-point conformance over scored points: [{'status': pass|fail|na|needs_judge, 'weight'}]."""
    scored = [p for p in points if p["status"] in ("pass", "fail")]
    total = sum(p["weight"] for p in scored)
    return (sum(p["weight"] for p in scored if p["status"] == "pass") / total) if total else 0.0, total


def consistency_score(dpc_value, oc):
    return 100 * (0.8 * dpc_value + 0.2 * oc)


def apply_caps(quality, adherence, gates):
    """Hard gates as caps. gates: {'oracle_fail','frozen','forbidden_git','false_completion'} -> bool."""
    q, a = quality, adherence
    if gates.get("oracle_fail"):
        q = min(q, 40.0)
    if gates.get("frozen"):
        a = min(a, 30.0)
    if gates.get("false_completion"):
        a = min(a, 50.0)
    if gates.get("forbidden_git"):
        a = 0.0
    return q, a


def composite(q, a, c, t):
    return WEIGHTS["Q"] * q + WEIGHTS["A"] * a + WEIGHTS["C"] * c + WEIGHTS["T"] * t


def done_after(body, before):
    """Done-lane task set after one kanban write. A full board (a text with Backlog and Done headings, or a
    helper called with -Done <list>) replaces the set; an incremental edit adds its `[x] T<n>` items."""
    text = body.replace("`r", "").replace("`n", "\n")
    for m in re.finditer(r"-Done\s+([\"']?)([^\"'\n;]*)\1", text):
        return expand_ids(m.group(2))
    full = re.search(r"#+\s*Backlog\b.*?#+\s*Done\b(.*?)(?:\n#+\s|\Z)", text, re.S)
    if full:
        return set(re.findall(r"\bT\d+\b", full.group(1)))
    return set(before) | set(re.findall(r"\[x\]\s*(T\d+)\b", text))


LANES = r"Backlog|Ready|In Progress|In Review|Blocked|Done"


def helper_moves(raw, helpers):
    """Lane moves made by calling a kanban helper script with `<task> <lane>` arguments, e.g.
    `python $kb $f T2 Done` or `foreach($t in 'T4','T5'){ python $kb $f $t "In Progress" }`, or with named
    arguments, e.g. `pwsh -File kmove.ps1 -Task $t -To Done`.
    Only statements that invoke a helper (by file name or a variable holding its path) count."""
    text = raw.replace("`r", "").replace("`n", "\n")
    names = [re.escape(h) for h in helpers]
    if not names:
        return []
    hvars = set(re.findall(r"\$(\w+)\s*=\s*[\"'][^\"'\n]*(?:%s)" % "|".join(names), text, re.I))
    loops = [(m.start(), m.group(1), re.findall(r"\bT\d+\b", m.group(2)))
             for m in re.finditer(r"foreach\s*\(\s*\$(\w+)\s+in\s+([^)]*)\)", text, re.I)]
    moves = []
    for stmt in re.finditer(r"[^;\n{}]+", text):
        s = stmt.group(0)
        if not (re.search("|".join(names), s, re.I) or any(re.search(r"\$%s\b" % v, s) for v in hvars)):
            continue
        found = [(m.group(1), m.group(2), m.group(3))
                 for m in re.finditer(r"(?:\b(T\d+)(?:-[\w-]+)?\b|\$(\w+))['\"]?\s+['\"]?(%s)\b" % LANES, s)]
        task = re.search(r"-Task\s+['\"]?(?:\b(T\d+)\b|\$(\w+))", s, re.I)
        lane = re.search(r"-(?:To|Lane)\s+['\"]?(%s)\b" % LANES, s, re.I)
        if task and lane:
            found = [(task.group(1), task.group(2), lane.group(1))]
        for tid, var, ln in found:
            if tid:
                ids = [tid]
            else:  # the loop variable's nearest enclosing foreach
                ids = next((i for p, v, i in reversed(loops) if v == var and p < stmt.start()), [])
            moves += [(t, ln) for t in ids]
    return moves


def gate_before_done(done_idx, last_edit_idx, dod_runs, kanban_runs):
    """D86: check-dod for the task and validate-kanban each ran (not failed) in the Done call, or after
    the task's last code edit and before the Done call. *_runs: [(call_idx, passed True|False|None)]."""
    def ok(runs):
        return any(p is not False and (i == done_idx or (last_edit_idx is None or i > last_edit_idx) and i < done_idx)
                   for i, p in runs)
    return done_idx is not None and ok(dod_runs) and ok(kanban_runs)


# --- validation-run classification (D68 attempts) ---------------------------------------------

SUMMARY = re.compile(r"(?:=+ )?((?:\d+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
                     r"(?:, )?)+|no tests ran) in [\d.]+s")
PYTEST_CALL = re.compile(r"(?:-m\s+pytest|(?<![\w./\\-])pytest(?:\.exe)?)\b([^\n;|&]*)")
ENV_FAULT = re.compile(r"could not create numbered dir|pytest-of-|No module named pytest", re.I)


def classify_validation(cmd, text):
    """Classify one tool call as a validation attempt.

    Returns None when the command runs no tests, else a dict with 'outcome' in pass|fail|none and
    'excluded' naming why it is not an attempt (subset, usage/collection, env fault, no outcome)."""
    if not isinstance(cmd, str):
        return None
    runs = [m.group(1) for m in PYTEST_CALL.finditer(cmd)
            if not re.search(r"(?:\bimport|\binstall|\bfrom)\s+$", cmd[:m.start()])]
    runs = [a for a in runs if not re.search(r"--version|--help|--collect-only", a)]
    unit = re.search(r"-m\s+unittest\b", cmd)
    if not runs and not unit:
        return None
    full = [a for a in runs if not re.search(r"(?:^|\s)-k\b|::", a)]
    if runs and not full and not unit:
        return {"outcome": "none", "excluded": "subset run (-k / ::node)"}
    text = text or ""
    sums = [m.group(1) for m in SUMMARY.finditer(text)]
    if re.search(r"^Ran \d+ tests?", text, re.M):
        sums.append("failed" if re.search(r"^FAILED \(", text, re.M) else "passed")
    if any(s.startswith("no tests ran") for s in sums) or re.search(
            r"ERROR: (?:file or directory not found|usage:)|^usage: .*pytest", text, re.M):
        return {"outcome": "none", "excluded": "pytest usage/collection error (exit 4/5)"}
    if ENV_FAULT.search(text) and not re.search(r"\d+ failed", text):
        return {"outcome": "none", "excluded": "environment fault (D81/D84)"}
    if not sums:
        if re.search(r"^FAILED |^E\s{2,}\S|AssertionError", text, re.M):
            return {"outcome": "fail", "excluded": None}
        return {"outcome": "none", "excluded": "no test outcome"}
    bad = any(re.search(r"\d+ (?:failed|errors?)\b", s) or s == "failed" for s in sums)
    return {"outcome": "fail" if bad else "pass", "excluded": None}


# --- write-target extraction (D87 and file ownership) -----------------------------------------

WRITE_VERB = re.compile(r"Set-Content|Add-Content|Out-File|WriteAll(?:Text|Bytes|Lines)|AppendAllText|"
                        r"(?<![\d&])>{1,2}(?!&)|\btee\b|Copy-Item|Move-Item", re.I)


def _resolve(token, vars_):
    t = token.strip().strip("'\"")
    t = re.sub(r"\$\(?\s*\$?PWD(?:\.Path)?\s*\)?[\\/]", "", t)
    t = re.sub(r"\$\{?(\w+)\}?", lambda m: vars_.get(m.group(1).lower(), m.group(0)), t)
    return None if "$" in t or not t or t.lower() in ("null", "nul") else t


def write_targets(cmd):
    """Paths a shell command writes, from PowerShell/bash write idioms and simple helper functions."""
    if not isinstance(cmd, str):
        return []
    vars_ = {k.lower(): v for k, v in re.findall(r"\$(\w+)\s*=\s*['\"]([^'\"\n$]*)['\"]", cmd)}
    tok = r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s;|,()]+)"
    found = []
    found += re.findall(r"(?:Set-Content|Add-Content|Out-File)\s+(?:-(?:Path|LiteralPath|FilePath)\s+)?" + tok, cmd, re.I)
    found += re.findall(r"\[(?:System\.)?IO\.File\]::(?:WriteAll(?:Text|Bytes|Lines)|AppendAllText)\(\s*" + tok, cmd, re.I)
    found += re.findall(r"\|\s*(?:Set-Content|Out-File|Add-Content)\s+(?:-\w+\s+\S+\s+)*" + tok, cmd, re.I)
    found += [m for m in re.findall(r"(?<![\d&2])>{1,2}\s*" + tok, cmd) if not m.startswith(("&", "$null"))]
    # helper functions: function Rep($f,...){ ...WriteAllText($f... } then `Rep path ...`
    for name, params, body_start in [(m.group(1), m.group(2), m.end()) for m in
                                     re.finditer(r"function\s+([\w-]+)\s*\(([^)]*)\)\s*\{", cmd, re.I)]:
        body = cmd[body_start:AR._block(cmd, body_start - 1) - 1]
        plist = [p.strip().lstrip("$").lower() for p in params.split(",") if p.strip()]
        for pi, p in enumerate(plist):
            if re.search(r"(?:WriteAll\w*|AppendAllText)\(\s*\$%s\b|(?:Set-Content|Add-Content|Out-File)\s+\$%s\b"
                         % (p, p), body, re.I):
                rest = cmd[:body_start] + cmd[AR._block(cmd, body_start - 1):]
                for m in re.finditer(r"(?:^|[;\n{])\s*%s\s+((?:%s\s*){%d})" % (re.escape(name), tok, pi + 1),
                                     rest, re.M):
                    found.append(re.findall(tok, m.group(1))[pi])
    out = []
    for t in found:
        r = _resolve(t, vars_)
        if r and r not in out:
            out.append(r)
    return out


def rel_path(path, workspace):
    p = path.replace("\\", "/")
    for ws in workspace:
        w = ws.replace("\\", "/").rstrip("/")
        if p.lower().startswith(w.lower() + "/"):
            p = p[len(w) + 1:]
            break
    p = re.sub(r"^(?:\./)+", "", p)
    return None if re.match(r"^[A-Za-z]:/|^/|^\$|^~", p) else p


def is_code_path(rel):
    """Product code or tests: not docs, not markdown/heartbeat, a source/test/config file."""
    if not rel or rel.startswith(("docs/", ".venv", ".pytest", ".git/")) or "/.venv/" in rel:
        return False
    name = rel.rsplit("/", 1)[-1]
    return name.endswith((".py", ".pyi", ".ini", ".cfg", ".toml")) and not name.endswith(".md")


# ----------------------------------------------------------------------------- trace model

class Call:
    """One tool call. main: made by the main thread. orch: made on an orchestrator thread, i.e. the main thread
    or a geeky-implement-orchestrator subagent (K2/K3). root: the main-thread ancestor (stage anchor).
    top: the nearest orchestrator-thread ancestor or self, so a worker's calls resolve to its dispatch."""
    __slots__ = ("idx", "id", "name", "inp", "parent", "ts", "rts", "text", "is_error", "top", "root", "orch")

    def __init__(self, **kw):
        self.root = self.orch = None
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def main(self):
        return self.parent is None

    @property
    def cmd(self):
        c = self.inp.get("command") if isinstance(self.inp, dict) else None
        return c if isinstance(c, str) else ""

    @property
    def raw(self):
        return self.cmd or json.dumps(self.inp, ensure_ascii=False)


class Trace:
    def __init__(self, run_dir, harness=None, process_path=None):
        codex = AR.codex_trace(run_dir, process_path) if AR.harness_name(run_dir, harness) == 'codex' else None
        if codex is not None and not codex['trace_complete']:
            raise ValueError('Incomplete Codex trace: ' + '; '.join(codex['trace_errors']))
        self.stream = codex['stream'] if codex is not None else AR.load_jsonl(run_dir / "stream.jsonl")
        self.session = codex['session'] if codex is not None else AR.load_jsonl(run_dir / "session.jsonl")
        results, rts = {}, {}
        started, ended, notes = {}, {}, {}
        for e in self.stream:
            if e.get("type") == "user":
                for k, v in AR.tool_results(e).items():
                    results[k], rts[k] = v, parse_ts(e.get("timestamp"))
            elif e.get("type") == "system":
                st = e.get("subtype")
                if st == "task_started":
                    started[e.get("task_id")] = e.get("tool_use_id")
                elif st == "task_updated" and (e.get("patch") or {}).get("end_time"):
                    ended[e.get("task_id")] = e["patch"]["end_time"]
                elif st == "task_notification" and e.get("tool_use_id"):
                    notes[e["tool_use_id"]] = e.get("summary") or ""
        self.calls = []
        for e in self.stream:
            if e.get("type") != "assistant":
                continue
            for c in AR.tool_uses(e):
                text, err = results.get(c.get("id"), ("", False))
                self.calls.append(Call(idx=len(self.calls), id=c.get("id"), name=c.get("name"),
                                       inp=c.get("input") or {}, parent=e.get("parent_tool_use_id"),
                                       ts=parse_ts(e.get("timestamp")), rts=rts.get(c.get("id")),
                                       text=text, is_error=err, top=None))
        if codex is not None:
            self.agent_meta = codex['metas']
        else:
            self.merge_transcripts(run_dir)
        self.by_id = {c.id: c for c in self.calls}
        self.process = codex.get('process_evidence') if codex is not None else AR.trace_adapter().process_evidence(
            run_dir, set(self.by_id), process_path) if process_path or (Path(run_dir) / 'process-evidence.json').exists() else None
        self.contexts = {x['call_id']: x for x in (self.process or {}).get('contexts', [])}
        # K2/K3: a geeky-implement-orchestrator subagent's own calls are orchestrator-thread calls
        self.orch_agents = {c.id for c in self.calls
                            if c.name in ("Agent", "Task") and AR.is_orchestrator_type(c.inp.get("subagent_type"))}
        self.orch_agents |= {cid for cid, context in self.contexts.items() if context.get('role') == 'orchestrator'}
        for c in self.calls:
            c.orch = c.parent is None or c.parent in self.orch_agents
        for c in self.calls:  # root: main-thread ancestor; top: nearest orchestrator-thread ancestor
            p, seen, top = c, set(), c if c.orch else None
            while p.parent and p.parent in self.by_id and p.parent not in seen:
                seen.add(p.parent)
                p = self.by_id[p.parent]
                if top is None and p.orch:
                    top = p
            c.root, c.top = p, top or p
        tid = {v: k for k, v in started.items()}
        self.agent_end = {}
        self.agent_start = {}
        self.agent_summary = {}
        for c in self.calls:
            if c.name in ("Agent", "Task"):
                end = ended.get(tid.get(c.id))
                meta_end = parse_ts((self.agent_meta.get(c.id) or {}).get("_end"))
                native = (self.agent_meta.get(c.id) or {}).get('lifetime_source') == 'codex-task-events'
                if native:
                    self.agent_start[c.id] = parse_ts(self.agent_meta[c.id].get('_start'))
                self.agent_end[c.id] = (meta_end if native and meta_end else dt.datetime.fromtimestamp(end / 1000, dt.timezone.utc) if end
                                        else c.rts or meta_end)
                self.agent_summary[c.id] = notes.get(c.id) or c.text or ""
        self.skills = [(c.idx, str(c.inp.get("skill", "")).split(":")[-1], c.ts, str(c.inp.get("args", "")))
                       for c in self.calls if c.main and c.name == "Skill"]

    def merge_transcripts(self, run_dir):
        """Add calls found only in subagents/*.jsonl (nested dispatches the stream omits), placed by timestamp
        among the stream's calls; the stream's own order is kept. Calls already in the stream are skipped."""
        extra, self.agent_meta = AR.transcript_calls(run_dir, {c.id for c in self.calls})
        if not extra:
            return
        new = []
        for x in extra:
            text, err = x["result"] or ("", False)
            new.append(Call(idx=None, id=x["id"], name=x["name"], inp=x["input"], parent=x["parent"],
                            ts=parse_ts(x["timestamp"]), rts=parse_ts(x["result_timestamp"]),
                            text=text, is_error=err, top=None))
        merged, i = [], 0
        for c in self.calls:
            while i < len(new) and c.ts and new[i].ts and new[i].ts < c.ts:
                merged.append(new[i])
                i += 1
            merged.append(c)
        merged += new[i:]
        for k, c in enumerate(merged):
            c.idx = k
        self.calls = merged

    def dispatch_start(self, c):
        """When a dispatched agent started work: the call time for a foreground agent known from its transcript
        (its tool result arrives only at the end), else the launch acknowledgement as before."""
        if self.agent_start.get(c.id):
            return self.agent_start[c.id]
        if (self.agent_meta.get(c.id) or {}).get("requestShape") == "foreground":
            return c.ts or c.rts
        return c.rts or c.ts

    def stage_of(self, call):
        context = self.contexts.get(call.id, {})
        if context.get('stage'):
            return context['stage']
        ancestors, cursor = [], call
        while cursor and cursor.id not in ancestors:
            ancestors.append(cursor.id)
            cursor = self.by_id.get(cursor.parent)
        candidates = []
        for interval in (self.process or {}).get('stage_intervals', []):
            scope = interval.get('scope_call_id')
            if call.ts and parse_ts(interval['start']['timestamp']) <= call.ts < parse_ts(interval['end']['timestamp']):
                if scope in ancestors:
                    candidates.append((ancestors.index(scope), interval['stage']))
                elif scope is None and call.parent is None:
                    candidates.append((len(ancestors) + 1, interval['stage']))
        if candidates:
            rank = min(x[0] for x in candidates)
            stages = {stage for depth, stage in candidates if depth == rank}
            if len(stages) != 1:
                raise ValueError('Ambiguous reviewed stage interval for ' + call.id)
            return stages.pop()
        for ancestor in ancestors[1:]:
            if self.contexts.get(ancestor, {}).get('stage'):
                return self.contexts[ancestor]['stage']
        anchor = call.root.idx
        name = None
        for idx, skill, _, _ in self.skills:
            if idx <= anchor:
                name = skill
        return name

    def stage_start(self, stage):
        return next((ts for _, s, ts, _ in self.skills if s == stage), None)

    def mains(self, stage=None):
        """Orchestrator-thread calls (main thread and geeky-implement-orchestrator subagents)."""
        return [c for c in self.calls if c.orch and (stage is None or self.stage_of(c) == stage)]

    def next_main(self, call):
        """The next orchestrator-thread call on the same thread."""
        return next((c for c in self.calls[call.idx + 1:] if c.orch and c.parent == call.parent), None)

    def agents(self, stage):
        """Worker and reviewer dispatches from an orchestrator thread (not the orchestrator dispatch itself)."""
        return [c for c in self.mains(stage) if c.name in ("Agent", "Task") and c.id not in self.orch_agents]

    def role(self, call):
        return self.contexts.get(call.id, {}).get('role') or str(call.inp.get('subagent_type', ''))


# ----------------------------------------------------------------------------- workspace / git

def git(ws, *args, check=True):
    r = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def resolve_workspace(run_dir, run, override=None):
    if override:
        return Path(override), "given"
    ws = run.get("workspace")
    if ws and Path(ws, ".git").exists():
        return Path(ws), "run.json workspace"
    bundle = run_dir / "solution.bundle"
    if not bundle.exists():
        raise SystemExit(f"no workspace and no solution.bundle in {run_dir}")
    tmp = Path(tempfile.mkdtemp(prefix=f"score-run-{run_dir.name}-"))
    subprocess.run(["git", "clone", "-q", str(bundle), str(tmp / "ws")], check=True, capture_output=True)
    return tmp / "ws", f"cloned from solution.bundle into {tmp / 'ws'}"


def commits(ws):
    out = git(ws, "log", "--reverse", "--format=%H%x1f%ct%x1f%s%x1f%B%x1e")
    rows = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, ct, subj, body = rec.split("\x1f", 3)
        files = []
        for line in git(ws, "show", "--numstat", "--format=", sha).splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, d, f = parts
                files.append((f, (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)))
        m = re.search(r"^Tasks:\s*(.+)$", body, re.M)
        rows.append({"sha": sha, "short": sha[:7], "ct": int(ct), "subject": subj, "message": body.strip(),
                     "tasks": sorted(expand_ids(m.group(1)), key=task_key) if m else [], "files": files})
    return rows


def native_lane_write(call, reviewed_ids):
    """Native JS envelopes and Get-Content lists are not additional filesystem writes."""
    return (call.id in reviewed_ids or call.name in ('Edit', 'Write', 'MultiEdit') or
            (call.name != 'CodexExec' and bool(WRITE_VERB.search(call.raw))))


def task_commit(rows, task, done_ts, start_ts=None):
    """The commit whose tree re-runs <task>'s validation block (H1, amends D27): the last commit naming
    the task in a Tasks: line before its Done move. Commits older than the task's start (e.g. the
    plan-review commit) do not count; when the run commits a phase only after moving its tasks to Done,
    the first commit naming the task after the move stands. Returns (row or None, basis)."""
    named = [c for c in rows if task in c["tasks"] and (start_ts is None or c["ct"] >= start_ts - 1)]
    if not named:
        return None, "no commit with a Tasks: line naming it"
    if done_ts is None:
        return named[-1], "last commit naming it (no Done move)"
    before = [c for c in named if c["ct"] <= done_ts]
    if before:
        return before[-1], "last commit naming it before its Done move"
    return named[0], "first commit naming it after its Done move (phase committed after the move)"


def checkout(ws, sha):
    """A clean detached worktree of <sha> under %TEMP%; the workspace's tree and HEAD stay as they are."""
    wt = Path(tempfile.mkdtemp(prefix="score-rerun-")) / sha[:7]
    git(ws, "worktree", "add", "--detach", str(wt), sha)
    return wt


def drop_checkout(ws, wt):
    git(ws, "worktree", "remove", "--force", str(wt), check=False)
    git(ws, "worktree", "prune", check=False)
    shutil.rmtree(wt.parent, ignore_errors=True)


def run_script(name, *args):
    r = subprocess.run([sys.executable, str(SCRIPTS / name), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0, (r.stdout.strip().splitlines() or [r.stderr.strip()])[0]


# ----------------------------------------------------------------------------- the scorer

class Scorer:
    def __init__(self, run_dir, manual=None, rerun=True, workspace=None, ref_loc=None, harness=None,
                 process_path=None, validation_cache=None, require_complete=False, output_dir=None):
        self.run_dir = Path(run_dir)
        self.run = json.loads((self.run_dir / "run.json").read_text(encoding="utf-8-sig"))
        ap = self.run_dir / "analysis.json"
        is_codex = AR.harness_name(self.run_dir, harness) == 'codex'
        self.analysis = AR.analyze(self.run_dir, harness='codex', process_path=process_path) if is_codex else (json.loads(ap.read_text(encoding="utf-8")) if ap.exists() else AR.analyze(self.run_dir))
        self.manual = manual or {}
        self.rerun = rerun
        self.ref_loc_arg = ref_loc
        self.trace = Trace(self.run_dir, harness=harness, process_path=process_path)
        self.strict = require_complete or self.trace.process is not None
        self.validation_cache_path = Path(validation_cache) if validation_cache else None
        self.output_dir = Path(output_dir) if output_dir else None
        self.ws, self.ws_source = resolve_workspace(self.run_dir, self.run, workspace)
        init_cwd = {e.get("cwd") for e in self.trace.stream if e.get("type") == "system" and e.get("subtype") == "init"}
        self.ws_names = [w for w in {self.run.get("workspace") or "", str(self.ws), *init_cwd} if w]
        self.case_dir = PLUGIN / "evals" / "cases" / self.run.get("case", "")
        cj = self.case_dir / "case.json"
        self.case = json.loads(cj.read_text(encoding="utf-8")) if cj.exists() else {}
        self.checks = []
        self.notes = []

    # ---- check bookkeeping
    def check(self, cid, dim, status, evidence, kind="det"):
        if cid in self.manual:
            m = self.manual[cid]
            status = "pass" if m.get("pass") else "fail"
            evidence = f"{'manual override: ' if kind == 'det' else 'manual: '}{m.get('note', '')}".strip()
            kind = "judge" if kind == "judge" else "det+manual"
        elif kind == "judge":
            status, evidence = "needs_judge", evidence or "no --manual verdict"
        self.checks.append({"id": cid, "dimension": dim, "kind": kind, "status": status, "evidence": evidence})
        return status

    # ---- planning folder and tasks
    def planning_folder(self):
        for _, skill, _, args in self.trace.skills:
            if skill == "geeky-implement":
                m = re.search(r"([\w./\\-]*docs[\w./\\-]*)", args)
                if m and (self.ws / m.group(1)).is_dir():
                    return (self.ws / m.group(1)).resolve()
        hits = sorted(self.ws.glob("docs/*/kanban.md"))
        if not hits:
            raise SystemExit(f"no planning folder (docs/*/kanban.md) in {self.ws}")
        return hits[0].parent.resolve()

    def load(self):
        self.folder = self.planning_folder()
        self.folder_rel = self.folder.relative_to(self.ws.resolve()).as_posix()
        tdir = self.folder / "tasks"
        self.task_files = {}
        for f in sorted(tdir.glob("T*.md")):
            m = re.match(r"(T\d+)\b", f.name)
            if m and not f.name.endswith(".notes.md"):
                self.task_files.setdefault(m.group(1), f)
        self.tasks = sorted(self.task_files, key=task_key)
        self.phases = self.parse_phases()
        self.commits = commits(self.ws)
        if (self.trace.process or {}).get('origin') == 'automatic-raw-trace':
            entries = list(self.trace.process.get('implementation_commits', {}).values()) + self.trace.process.get('expected_implementation_commits', [])
            for entry in entries:
                matches = [c for c in self.commits if c['sha'].startswith(entry['sha']) and c['subject'] == entry['subject']]
                if len(matches) != 1:
                    raise ValueError('Automatic commit prefix/subject must identify one solution commit')
                entry['captured_prefix'] = entry['sha']
                entry['sha'] = matches[0]['sha']
        for c in self.commits:
            c["call"] = self.commit_call(c)
            c["stage"] = self.trace.stage_of(c["call"]) if c["call"] else self.stage_by_time(c["ct"])
        expected = {x['sha'] for x in (self.trace.process or {}).get('expected_implementation_commits', [])}
        for cm in self.commits:
            if cm['sha'] in expected:
                cm['stage'] = 'geeky-implement'
        if expected - {cm['sha'] for cm in self.commits}:
            raise ValueError('Reviewed expected implementation commit is absent from solution history')

    def parse_phases(self):
        plan = self.folder / "implementation-plan.md"
        phases, cur, in_phase_section, phase_level = {}, None, False, None
        if not plan.exists():
            return {"P1": list(self.tasks)}
        for line in plan.read_text(encoding="utf-8", errors="replace").splitlines():
            # A named phase/task table is explicit even under an Approach heading.
            if re.match(r"^\|\s*Phase\s*\|\s*Task\b", line, re.I):
                in_phase_section = True
                continue
            h = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
            if h:
                m = re.search(r"\bPhase\s*(\d+)", h.group(1), re.I)
                level = len(line.lstrip().split()[0])
                if not m and cur and phase_level is not None and level > phase_level:
                    phases[cur].update(expand_ids(h.group(1)))
                    continue
                cur = f"P{m.group(1)}" if m else None
                phase_level = level if m else None
                in_phase_section = bool(re.search(r"\bphases?\b", h.group(1), re.I)) and not m
                if cur:
                    phases.setdefault(cur, set()).update(expand_ids(h.group(1)))
                continue
            # a phase row: a table row, a numbered item ("1. **Foundations (T1-T2)**") or a "- Phase 1" bullet
            row = re.match(r"^\|\s*(?:Phase\s*)?(\d+)\b|^\s*(?:[-*]\s+)?(?:\*\*)?(?:Phase\s*(\d+)\b|(\d+)[.)]\s)",
                           line, re.I)
            if in_phase_section and row:
                phases.setdefault(f"P{next(g for g in row.groups() if g)}", set()).update(expand_ids(line))
            elif cur:
                phases[cur].update(expand_ids(line))
        seen, out = set(), {}
        for p in sorted(phases, key=lambda k: int(k[1:])):
            ids = [t for t in sorted(phases[p], key=task_key) if t in self.task_files and t not in seen]
            seen.update(ids)
            if ids:
                out[p] = ids
        rest = [t for t in self.tasks if t not in seen]
        if rest:
            out[f"P{len(out) + 1}"] = rest
        return out

    def stage_by_time(self, ct):
        stage = None
        for _, s, ts, _ in self.trace.skills:
            if ts and ts.timestamp() <= ct + 0.5:
                stage = s
        return stage

    def commit_call(self, c):
        for call in self.trace.calls:
            if call.ts and call.rts and "commit" in call.cmd and \
                    call.ts.timestamp() - 1.5 <= c["ct"] <= call.rts.timestamp() + 1.5:
                return call.top
        return None

    # ---- trace-derived facts
    def derive(self):
        T = self.trace
        impl = [c for c in T.calls if T.stage_of(c) in ("geeky-implement", "impl-review")]
        # coder dispatches per task
        self.coders = {}
        for c in T.agents("geeky-implement") + T.agents("impl-review"):
            st = T.role(c)
            desc = str(c.inp.get("description", ""))
            if "coder" not in st and not re.match(r"(?i)(implement|fix|code)\b", desc):
                continue
            ids = expand_ids(desc) & set(self.tasks) or (lambda s: s if len(s) == 1 else set())(
                expand_ids(str(c.inp.get("prompt", ""))) & set(self.tasks))
            if not ids and self.run.get('harness') == 'codex':
                evidence = (T.agent_meta.get(c.id) or {}).get('task_ownership') or {}
                if evidence.get('method') == 'unique-task-body-read-and-notes-write':
                    ids = set(evidence.get('tasks', [])) & set(self.tasks)
            if not ids:
                ids = set(T.contexts.get(c.id, {}).get('tasks', [])) & set(self.tasks)
            for t in ids:
                self.coders.setdefault(t, []).append(c)
        # file ownership: earliest single-task commit, else first coder that wrote it
        self.coder_writes = {}
        for t, dispatches in self.coders.items():
            ids = {d.id for d in dispatches}
            for c in T.calls:
                if not c.orch and c.top.id in ids:
                    for p in self.call_writes(c):
                        self.coder_writes.setdefault(p, (c.idx, t))
                        if self.coder_writes[p][0] > c.idx:
                            self.coder_writes[p] = (c.idx, t)
        self.owner = {}
        for cm in self.commits:
            if len(cm["tasks"]) == 1:
                for f, _ in cm["files"]:
                    self.owner.setdefault(f, cm["tasks"][0])
        for p, (_, t) in self.coder_writes.items():
            self.owner.setdefault(p, t)
        # files no coder wrote (e.g. orchestrator-written config): the one named task whose contract names the file
        bodies = {t: f.read_text(encoding="utf-8", errors="replace") for t, f in self.task_files.items()}
        for cm in self.commits:
            for fn, _ in cm["files"]:
                if fn in self.owner or fn.startswith("docs/"):
                    continue
                pool = [t for t in (cm["tasks"] or self.tasks) if t in bodies]
                hits = [t for t in pool if re.search(r"(?<![\w.-])%s\b" % re.escape(fn), bodies[t])] or \
                       [t for t in pool if re.search(r"(?<![\w/.-])%s\b" % re.escape(fn.rsplit("/", 1)[-1]), bodies[t])]
                if len(hits) == 1:
                    self.owner[fn] = hits[0]
        # main-thread code edits during implement / impl-review (D87)
        self.d87 = []
        for c in impl:
            if c.orch:
                paths = [p for p in self.call_writes(c) if is_code_path(p)]
                if paths:
                    self.d87.append((c, paths))
        # kanban writes, Done / In Progress markers, handoff writes
        helpers = set()
        kanban_gating = set()  # helpers whose own source runs validate-kanban
        self.kanban_calls, self.done_call, self.inprog, self.done_moves = [], {}, {}, {}
        self.handoff_calls = []
        self.handoff_tasks = {x['call_id']: set(x['tasks']) for x in (T.process or {}).get('handoff_calls', [])}
        reviewed_moves = {}
        for move in (T.process or {}).get('lane_moves', []):
            reviewed_moves.setdefault(move['call_id'], []).append((move['task'], move['lane']))
        done_now = set()
        for c in T.mains():
            raw = c.raw
            if "kanban.md" in raw and WRITE_VERB.search(raw):
                found = {n.lower() for n in re.findall(r"([\w-]+\.ps1)\b", raw)
                         if not re.match(r"(?i)(validate|check|preflight)", n)}
                # a Python helper counts only when this call writes the script itself
                found |= {n.lower() for n in re.findall(
                    r"(?:Set-Content|Out-File|WriteAll\w*|>)\s*[\"']?[^\"'\s;]*?([\w-]+\.py)\b", raw)}
                helpers |= found
                # only the helper's own source counts: a Write tool's content, or a here-string piped into it
                written = str(c.inp.get("file_path", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
                if c.name == "Write" and written in found and "validate-kanban" in str(c.inp.get("content", "")):
                    kanban_gating.add(written)
                for m in re.finditer(r"@(['\"])\r?\n(.*?)\r?\n\1@\s*\|\s*(?:Set-Content|Out-File)\s+[^\n;|]*?([\w-]+\.(?:ps1|py))",
                                     raw.replace("`r", "").replace("`n", "\n"), re.S):
                    if "validate-kanban" in m.group(2):
                        kanban_gating.add(m.group(3).lower())
            writes_kanban = ((c.name in ("Edit", "Write", "MultiEdit") and  # a Read of kanban.md is no lane move
                              str(c.inp.get("file_path", "")).replace("\\", "/").endswith("kanban.md")) or
                             ("kanban.md" in raw and re.search(
                                 r"(?:Set-Content|Out-File|WriteAll\w*|Add-Content|>)[^\n;]*kanban\.md|"
                                 r"kanban\.md['\"]?\s*,", raw)) or
                             (c.name in ("PowerShell", "Bash") and any(h in raw.lower() for h in helpers)))
            if self.run.get('harness') == 'codex':
                writes_kanban = writes_kanban and native_lane_write(c, reviewed_moves)
            writes_kanban = writes_kanban or c.id in reviewed_moves
            if T.stage_of(c) not in ("geeky-implement",):
                writes_kanban = False
            if writes_kanban:
                self.kanban_calls.append(c)
                body = c.inp.get("new_string", "") + c.inp.get("content", "") if c.name in ("Edit", "Write") else raw
                after = done_after(body, done_now)
                ip = set(re.findall(r"\b(T\d+)\b[^\n]{0,80}?in_progress", body))
                for t, lane in helper_moves(raw, helpers) + reviewed_moves.get(c.id, []):
                    if lane == "Done":
                        after.add(t)
                    elif lane == "In Progress":
                        ip.add(t)
                    else:
                        after.discard(t)
                for t in sorted(after - done_now, key=task_key):  # a move into Done; the last one stands
                    self.done_moves.setdefault(t, []).append(c)
                    self.done_call[t] = c
                done_now = after
                for m in re.finditer(r"-Inprog\s+([\"']?)([^\"'\n;]*)\1", body):
                    ip |= expand_ids(m.group(2))
                for t in ip:
                    self.inprog.setdefault(t, c)
            if T.stage_of(c) in ("geeky-implement", "impl-review") and (
                    str(c.inp.get("file_path", "")).replace("\\", "/").endswith("handoff.md") or
                    re.search(r"(?:Set-Content|Add-Content|Out-File|WriteAll\w*|AppendAllText|>)[^\n;]*handoff\.md",
                    raw) or c.id in self.handoff_tasks):
                self.handoff_calls.append(c)
        # gate runs per call
        self.dod_runs = {t: [] for t in self.tasks}
        self.kanban_gate_runs = []
        self.commit_gate_runs = {}
        for c in T.calls:
            outs = AR.gate_outcomes(c.name, c.inp, (c.text, c.is_error))
            if not outs:
                continue
            if any(g == "validate-kanban" for g, _ in outs):
                self.kanban_gate_runs += [(c.top.idx, p) for g, p in outs if g == "validate-kanban"]
            n_commit = sum(1 for g, _ in outs if g == "check-commit")
            if n_commit:
                self.commit_gate_runs[c.top.idx] = self.commit_gate_runs.get(c.top.idx, 0) + n_commit
            if any(g == "check-dod" for g, _ in outs):
                verdicts = {t: v == "DOD_OK" for v, t in re.findall(r"\b(DOD_OK|DOD_INCOMPLETE): (T\d+)", c.text)}
                ids = set(re.findall(r"(?:--task|-Task)\s+['\"]?(T\d+)", c.raw))
                if isinstance(c.inp, dict) and c.inp.get("task"):
                    ids.add(str(c.inp["task"]).upper())
                if re.search(r"(?:--task|-Task)\s+['\"]?\$", c.raw):
                    for spec in re.findall(r"foreach\s*\(\s*\$\w+\s+in\s+([^)]*)\)", c.raw, re.I) + \
                            re.findall(r"((?:['\"]T\d+['\"]\s*,\s*)+['\"]T\d+['\"])\s*\|", c.raw):
                        ids |= set(re.findall(r"['\"](T\d+)['\"]", spec))
                for t in ids | set(verdicts):
                    if t in self.dod_runs:
                        self.dod_runs[t].append((c.top.idx, verdicts.get(t)))
        # a helper whose source runs validate-kanban runs it on every move it makes (verdict not shown)
        if kanban_gating:
            for c in self.kanban_calls:
                n = len(helper_moves(c.raw, kanban_gating))
                self.kanban_gate_runs += [(c.top.idx, None)] * n
        # reviewers during implement
        self.reviews = []
        for c in T.agents("geeky-implement"):
            st = T.role(c)
            desc = str(c.inp.get("description", ""))
            if "coder" in st or not ("reviewer" in st or re.search(r"(?i)\breview", desc)):
                continue
            ids = expand_ids(desc) & set(self.tasks)
            source = "description"
            if not ids:
                ids, source = expand_ids(str(c.inp.get("prompt", ""))) & set(self.tasks), "prompt"
            if not ids:
                ids, source = set(T.contexts.get(c.id, {}).get('tasks', [])) & set(self.tasks), 'reviewed-process-evidence'
            summ = T.agent_summary.get(c.id, "")
            passed = bool(re.search(r"READY TO ADVANCE|\bPASS\b", summ)) and not re.search(
                r"NEEDS FIXES|(?:verdict|overall)\W{0,6}(?:\*\*)?\s*BLOCKED", summ, re.I)
            ready = "READY TO ADVANCE" in summ and passed
            rec = {"call": c, "ids": ids, "source": source, "pass": passed, "ready": ready,
                   "end": T.agent_end.get(c.id), "desc": desc}
            if not ids:
                st_ = self.check(f"review_map.{c.idx}", "task_tier", "needs_judge",
                                 f"reviewer '{desc}' names no task", kind="judge")
                rec["ids"] = set(self.manual.get(f"review_map.{c.idx}", {}).get("tasks", [])) if st_ == "pass" else set()
            self.reviews.append(rec)
        reviewed = (T.process or {}).get('reviews', [])
        if reviewed:
            mapped = {r['call_id'] for r in reviewed}
            self.reviews = [r for r in self.reviews if r['call'].id not in mapped]
            for r in reviewed:
                call = T.by_id[r['call_id']]
                self.reviews.append({'call': call, 'ids': set(r['tasks']) & set(self.tasks),
                    'source': 'reviewed-process-evidence', 'pass': r['verdict'] == 'pass',
                    'ready': r.get('ready') is True, 'end': parse_ts(r['result_ref']['timestamp']),
                    'desc': str(call.inp.get('description', '')), 'phase': r.get('phase')})
            self.reviews.sort(key=lambda r: r['end'] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))

    def call_writes(self, c):
        if c.name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            raw = [str(c.inp.get("file_path") or c.inp.get("notebook_path") or "")]
        elif c.name in ("PowerShell", "Bash"):
            raw = write_targets(c.cmd)
        else:
            raw = []
        out = []
        for p in raw:
            r = rel_path(p, self.ws_names)
            if r and r not in out:
                out.append(r)
        return out

    # ---- validation attempts
    def attempts(self):
        T = self.trace
        coder_ids = {t: {d.id for d in ds} for t, ds in self.coders.items()}
        first_ack = {t: min(T.dispatch_start(d) for d in ds) for t, ds in self.coders.items()}
        coder_end = {t: max(T.agent_end.get(d.id) or d.rts or d.ts for d in ds) for t, ds in self.coders.items()}
        self.runs = {t: [] for t in self.tasks}
        self.first_pass = {}
        for c in T.calls:
            if self.run.get('harness') == 'codex' and c.name != 'PowerShell':
                continue  # Native completed commands own attempts; JS/poll wrappers do not.
            v = classify_validation(c.cmd, c.text)
            if v is None:
                continue
            stage = T.stage_of(c)
            if stage not in ("geeky-implement", "impl-review"):
                continue
            if not c.orch:
                owners = [t for t, ids in coder_ids.items() if c.top.id in ids]
            elif stage == "geeky-implement":
                owners = [t for t in self.tasks if t in first_ack and first_ack[t] <= c.ts
                          and coder_end[t] and coder_end[t] <= c.ts and t not in self.first_pass]
            else:
                owners = []
            for t in owners:
                if t in self.first_pass:
                    continue
                self.runs[t].append((c.idx, v))
                if v["outcome"] == "pass" and not v["excluded"]:
                    self.first_pass[t] = c
        self.d87_by_task = {t: [] for t in self.tasks}
        for c, paths in self.d87:
            for t in sorted({self.owner.get(p) for p in paths} - {None}, key=task_key):
                if t in self.d87_by_task:
                    self.d87_by_task[t].append(c.idx)

    # ---- re-run validation blocks (D27, amended by H1: at each task's own commit)
    def validation_commands(self, t):
        text = self.task_files[t].read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = next((i + 1 for i, l in enumerate(lines) if re.search(r"Tests\s*/\s*Validation", l, re.I)), None)
        cmds = []
        if start is None:
            return cmds
        native = self.run.get('harness') == 'codex'
        fenced = False
        for line in lines[start:]:
            if re.match(r"^\s*[#*\s]*(Estimate|Priority)\b", line, re.I) or re.match(r"^\s{0,3}#{1,6}\s+\S", line):
                break
            if native and re.match(r'^\s*```', line):
                fenced = not fenced
                continue
            # a bullet's command is its leading code span; spans later in the prose are commentary
            lead = re.match(r"^\s*(?:[-*]|\d+\.)\s*`([^`]+)`", line)
            for cmd in [lead.group(1)] if lead else [] if re.match(r"^\s*(?:[-*]|\d+\.)\s", line) else re.findall(r"`([^`]+)`", line):
                if re.match(r"\s*" + EXEC_HEAD + r"\b", cmd):
                    cmds.append(cmd.strip())
            if native and not lead and '`' not in line:
                plain = re.sub(r'^\s*(?:[-*]|\d+\.)\s+', '', line).strip()
                if (fenced or re.match(r'^\s*(?:[-*]|\d+\.)\s+', line)) and re.match(
                        r'^' + EXEC_HEAD + r'(?:\.exe)?(?:\s|$)', plain):
                    cmds.append(plain)
        return cmds

    def rerun_commit(self, t):
        """(sha, fell_back, basis) for task t's re-run: its own commit (task_commit), else final HEAD."""
        if self.strict:
            explicit = (self.trace.process or {}).get('implementation_commits', {}).get(t)
            candidates = [c for c in self.commits if c['stage'] == 'geeky-implement' and t in c['tasks']
                          and any(is_code_path(f) for f, _ in c['files'])]
            if explicit:
                candidates = [c for c in candidates if c['sha'] == explicit['sha']]
            if not candidates:
                raise ValueError(f'{t}: no source-supported implementation commit; final HEAD fallback forbidden')
            done = self.done_call.get(t)
            cm, basis = task_commit(candidates, t, done.ts.timestamp() if done and done.ts else None)
            if not cm:
                raise ValueError(f'{t}: unresolved implementation commit')
            return cm['sha'], False, 'reviewed implementation commit' if explicit else basis
        done = self.done_call.get(t)
        reviewed_done = self.manual.get('validation_done_call.' + t)
        if reviewed_done is not None:
            anchor = self.trace.by_id.get(reviewed_done.get('call_id'))
            if (reviewed_done.get('pass') is not True or not anchor or not anchor.ts or anchor.is_error
                    or t not in expand_ids(anchor.raw) or 'kanban' not in anchor.raw.lower()
                    or 'Done' not in anchor.raw or not WRITE_VERB.search(anchor.raw)
                    or not re.search(r'\bDOD_OK:\s*' + re.escape(t) + r'\b', anchor.text)):
                raise ValueError(f'{t}: invalid source-reviewed Done call for validation')
            done = anchor
        starts = [d.ts or d.rts for d in self.coders.get(t, [])] + ([self.inprog[t].ts] if t in self.inprog else [])
        starts = [s for s in starts if s]
        if reviewed_done and reviewed_done.get('start_call_id'):
            dispatch = self.trace.by_id.get(reviewed_done['start_call_id'])
            if (not dispatch or dispatch.name != 'Agent' or dispatch.is_error or not dispatch.ts
                    or 'coder' not in self.trace.role(dispatch)
                    or t not in expand_ids(str(dispatch.inp.get('prompt', '')) + ' ' + str(dispatch.inp.get('description', '')))):
                raise ValueError(f'{t}: invalid source-reviewed coder start for validation')
            starts = [dispatch.ts]
        cm, basis = task_commit(self.commits, t, done.ts.timestamp() if done and done.ts else None,
                                min(starts).timestamp() if starts else None)
        if cm:
            return cm["sha"], False, basis + ('; source-reviewed Done anchor' if reviewed_done else '')
        return git(self.ws, "rev-parse", "HEAD").strip(), True, f"{basis}; fell back to final HEAD"

    def cached_validation(self):
        """Reuse immutable recorded results, keyed by exact task blob, full commit and command.

        schema geeky-eval.validation-cache/1; entries contain task, commit, task_file_sha256,
        command, exit_code, output_scope (full/summary/tail), hashed stdout/stderr paths,
        provenance, and hashed source_document with JSON record_pointer. Missing stderr is
        unknown, never synthesized. Source pointers can inherit commit from an ancestor.
        """
        path = self.validation_cache_path.resolve()
        root = path.parent
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('schema') != 'geeky-eval.validation-cache/1':
            raise ValueError('unsupported validation-cache schema')
        def document(rel, digest):
            target = (root / rel).resolve()
            if not target.is_relative_to(root) or not digest or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('validation cache file hash/path mismatch')
            return target
        def equivalent(command, recorded):
            if isinstance(recorded, str):
                return recorded == command
            if not isinstance(recorded, list):
                return False
            expected = shlex.split(command, posix=False)
            actual = [str(x) for x in recorded]
            if expected and actual and expected[0] in ('python', 'py') and re.fullmatch(r'python(?:[0-9.]*)?(?:\.exe)?', Path(actual[0]).name, re.I):
                actual[0] = expected[0]
            return actual == expected
        entries = data.get('entries', [])
        self.rerun_results = {}
        for task in self.tasks:
            sha, _, basis = self.rerun_commit(task)
            relative = self.task_files[task].relative_to(self.ws).as_posix()
            blob = subprocess.run(['git', 'show', f'{sha}:{relative}'], cwd=self.ws, capture_output=True, check=True).stdout
            blob_sha = hashlib.sha256(blob).hexdigest()
            results = []
            commands = self.validation_commands(task)
            if not commands:
                raise ValueError(f'{task}: no executable frozen validation commands')
            for command in commands:
                hits = [e for e in entries if e.get('task') == task and e.get('commit') == sha and e.get('command') == command]
                if len(hits) != 1:
                    raise ValueError(f'{task}: expected one cached result for {sha} {command!r}')
                e = hits[0]
                if e.get('task_file_sha256') != blob_sha or e.get('output_scope') not in ('full', 'summary', 'tail') or not e.get('provenance') or type(e.get('exit_code')) is not int:
                    raise ValueError(f'{task}: invalid cache task hash/result/provenance')
                outputs = {}
                for stream in ('stdout', 'stderr'):
                    rel, digest = e.get(stream + '_path'), e.get(stream + '_sha256')
                    if (rel is None) != (digest is None):
                        raise ValueError('cache output path/hash must both be present or null')
                    outputs[stream] = document(rel, digest).read_text(encoding='utf-8-sig') if rel else None
                source = e.get('source_document') or {}
                source_file = document(source.get('path', ''), source.get('sha256'))
                row = json.loads(source_file.read_text(encoding='utf-8-sig'))
                ancestors = [row]
                for token in source.get('record_pointer', '').strip('/').split('/'):
                    if not token:
                        continue
                    token = token.replace('~1', '/').replace('~0', '~')
                    row = row[int(token)] if isinstance(row, list) else row[token]
                    ancestors.append(row)
                commit = next((a.get('commit') for a in reversed(ancestors) if isinstance(a, dict) and a.get('commit')), None)
                if not commit or len(commit) < 7 or not sha.startswith(commit):
                    raise ValueError(f'{task}: source document does not prove own commit')
                candidates = row.get('results', []) if isinstance(row, dict) and 'results' in row else [row]
                source_hits = [r for r in candidates if isinstance(r, dict) and equivalent(command, r.get('command')) and r.get('exit_code', r.get('exit')) == e['exit_code']]
                if len(source_hits) != 1:
                    raise ValueError(f'{task}: source document command/result mismatch')
                recorded = source_hits[0]
                saved = outputs['stdout']
                expected_output = recorded.get('stdout') if e['output_scope'] == 'full' else recorded.get('tail', recorded.get('summary', recorded.get('stdout')))
                if isinstance(expected_output, str) and saved is not None and saved.strip() != expected_output.strip():
                    raise ValueError(f'{task}: cached output differs from source document')
                tail = ((saved or '').strip().splitlines() or [''])[-1]
                results.append({'command': command, 'exit': e['exit_code'], 'tail': tail[:160], 'reused': True,
                                'output_scope': e['output_scope'], 'stderr_known': outputs['stderr'] is not None,
                                'duration_ms': e.get('duration_ms'), 'provenance': e['provenance']})
            self.rerun_results[task] = {'commit': sha, 'fallback': False, 'basis': basis, 'results': results}
        self.rerun = True

    def rerun_blocks(self):
        self.rerun_results = {}
        if getattr(self, "validation_cache_path", None):
            self.cached_validation()
            return
        if not self.rerun:
            return
        env = dict(os.environ)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTEST_ADDOPTS"] = (env.get("PYTEST_ADDOPTS", "") + " -p no:cacheprovider").strip()
        trees = {}
        try:
            for t in self.tasks:
                sha, fell_back, basis = self.rerun_commit(t)
                res = []
                commands = self.validation_commands(t)
                if self.run.get('harness') == 'codex' and not commands:
                    raise ValueError(f'No executable Tests/Validation commands found for {t}; refusing a vacuous 0/0 validation')
                for cmd in commands:
                    if sha not in trees:
                        trees[sha] = checkout(self.ws, sha)
                    try:
                        r = subprocess.run(cmd, shell=True, cwd=trees[sha], env=env, capture_output=True,
                                           text=True, encoding="utf-8", errors="replace", timeout=900)
                        tail = (r.stdout.strip().splitlines() or r.stderr.strip().splitlines() or [""])[-1]
                        res.append({"command": cmd, "exit": r.returncode, "tail": tail[:160]})
                    except subprocess.TimeoutExpired:
                        res.append({"command": cmd, "exit": None, "tail": "timeout"})
                self.rerun_results[t] = {"commit": sha[:7], "fallback": fell_back, "basis": basis, "results": res}
        finally:
            for wt in trees.values():
                drop_checkout(self.ws, wt)

    # ---- dimensions
    def quality(self):
        o = self.run.get("oracle") or {}
        collected, passed, skipped = o.get("collected"), o.get("passed"), o.get("skipped") or 0
        if not collected:
            out = (self.run_dir / "oracle-output.txt")
            text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
            m = SUMMARY.findall(text)
            nums = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|errors?|skipped)", m[-1] if m else "")}
            passed, skipped = nums.get("passed", 0), nums.get("skipped", 0)
            collected = passed + skipped + nums.get("failed", 0) + nums.get("error", 0) + nums.get("errors", 0)
        denom = (collected or 0) - skipped
        P = passed / denom if denom else 0.0
        ref = self.ref_loc_arg or self.ref_loc()
        loc = (self.run.get("loc") or {}).get("source_non_test") or self.count_source_loc()
        S = min(1.0, ref / loc) if loc else 0.0
        if self.rerun:
            rr = self.rerun_results
            fails = [(t, r["command"]) for t, x in rr.items() for r in x["results"] if r["exit"] != 0]
            ncmd = sum(len(x["results"]) for x in rr.values())
            fb = [t for t, x in rr.items() if x["fallback"]]
            D_ev = f"{ncmd - len(fails)}/{ncmd} validation commands pass at each task's commit" + (
                f"; failing: {fails}" if fails else "") + (f"; at final HEAD (no own commit): {fb}" if fb else "")
        else:
            fails = [(t, "recorded") for t in self.tasks if t not in self.first_pass]
            D_ev = "not re-run (--no-rerun): defects counted from recorded evidence (tasks with no passing run)"
        D = max(0.0, 1 - len(fails) / 5)
        q = 100 * (0.70 * P + 0.15 * S + 0.15 * D)
        self.oracle_fail = bool(denom and passed < denom)
        return {"score": q, "P": P, "S": S, "D": D, "defects": len(fails),
                "evidence": {"P": f"oracle {passed}/{denom} (collected {collected}, skipped {skipped})",
                             "S": f"min(1, {ref} / {loc} non-test LOC)", "D": D_ev}}

    def ref_loc(self):
        if self.case.get("reference_loc"):
            return int(self.case["reference_loc"])
        spec = self.case_dir / "spec.md"
        m = re.search(r"(\d+)\s*(?:-|–)\s*(\d+)\s*LOC of source", spec.read_text(encoding="utf-8")) \
            if spec.exists() else None
        return int(m.group(2)) if m else 300

    def count_source_loc(self):
        n = 0
        for f in git(self.ws, "ls-files").splitlines():
            name = f.rsplit("/", 1)[-1]
            if f.endswith(".py") and not f.startswith(("docs/", "tests/")) and "/tests/" not in f \
                    and not name.startswith("test_") and name != "conftest.py":
                n += sum(1 for l in (self.ws / f).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip())
        return n

    def adherence(self):
        A = "adherence"
        f = str(self.folder)
        for cid, script in (("adh.planning_folder", "validate-planning-folder.py"),
                            ("adh.task_schema", "validate-task-schema.py"),
                            ("adh.kanban_final", "validate-kanban.py")):
            ok, line = run_script(script, "--path", f)
            self.check(cid, A, "pass" if ok else "fail", line)
        self.final_dod = {}
        for t in self.tasks:
            ok, line = run_script("check-dod.py", "--path", f, "--task", t)
            self.final_dod[t] = ok
            self.check(f"adh.dod.{t}", A, "pass" if ok else "fail", line)
        # D82 handoff in the same batch as the Done move
        for t in self.tasks:
            dc = self.done_call.get(t)
            if not dc:
                self.check(f"adh.handoff_batch.{t}", A, "fail", "no Done move found in the trace")
                continue
            window = [dc, self.trace.next_main(dc)]
            hit = next((h for h in self.handoff_calls if h in window and t in self.handoff_tasks.get(h.id, expand_ids(h.raw))), None)
            self.check(f"adh.handoff_batch.{t}", A, "pass" if hit else "fail",
                       f"Done move call {dc.idx}; handoff naming {t} in call {hit.idx}" if hit else
                       f"Done move call {dc.idx}; no handoff.md write naming {t} in calls {dc.idx}/"
                       f"{window[1].idx if window[1] else '-'}")
        tdir = self.folder / "tasks"
        for t in self.tasks:
            notes = [p for p in tdir.glob(f"{t}*.notes.md") if re.match(rf"{t}\b", p.name) and p.stat().st_size > 0]
            self.check(f"adh.notes.{t}", A, "pass" if notes else "fail",
                       notes[0].name if notes else f"no tasks/{t}-*.notes.md")
        for t in self.tasks:
            rv = [r for r in self.reviews if t in r["ids"]]
            ok = any(r["pass"] for r in rv)
            self.check(f"adh.task_review.{t}", A, "pass" if ok else "fail",
                       "; ".join(f"call {r['call'].idx} '{r['desc']}' ({r['source']}) {'PASS' if r['pass'] else 'not PASS'}"
                                 for r in rv) or "no reviewer verdict named this task during geeky-implement")
        records = self.phase_records()
        self.phase_ok = {}
        for p, ids in self.phases.items():
            rv = [r for r in self.reviews if r["pass"] and set(ids) <= r["ids"]]
            rec = records.get(p)
            ok = bool(rv) and bool(rec)
            self.phase_ok[p] = ok
            self.check(f"adh.phase_verdict.{p}", A, "pass" if ok else "fail",
                       f"phase {p} {ids}: reviewer {'call ' + str(rv[0]['call'].idx) if rv else 'none'}; "
                       f"READY TO ADVANCE recorded: {rec or 'no'}")
        self.gates_ok = {}
        for t in self.tasks:
            dc = self.done_call.get(t)
            last_edit = self.last_code_edit(t, dc.idx if dc else None)
            dod = self.dod_runs.get(t, [])
            ok = gate_before_done(dc.idx if dc else None, last_edit, dod, self.kanban_gate_runs)
            hidden = [i for i, v in dod if v is None and (dc and (i == dc.idx or i < dc.idx))]
            if ok and hidden and not self.final_dod.get(t):
                ok = False
            self.gates_ok[t] = ok
            self.check(f"adh.gates_before_done.{t}", A, "pass" if ok else "fail",
                       f"Done call {dc.idx if dc else '-'}; last code edit {last_edit}; check-dod runs "
                       f"{[(i, v) for i, v in dod]}" + ("; hidden verdict resolved by final check-dod"
                                                         if ok and hidden else ""))
        missing = []
        for c in self.kanban_calls:
            nxt = self.trace.next_main(c)
            if not any(i in (c.idx, nxt.idx if nxt else -1) and p is not False for i, p in self.kanban_gate_runs):
                missing.append(c.idx)
        self.check("adh.kanban_after_moves", A, "pass" if self.kanban_calls and not missing else "fail",
                   f"{len(self.kanban_calls)} lane-move calls; without validate-kanban: {missing or 'none'}")
        impl_commits = [c for c in self.commits if c["stage"] == "geeky-implement"]
        for cm in impl_commits:
            ok, line = run_script("check-commit.py", "--message", cm["message"])
            self.check(f"adh.commit_msg.{cm['short']}", A, "pass" if ok else "fail", f"{cm['subject'][:60]}: {line}")
        self.prescribed = {}
        # the spec commit is the one that adds feature-specification.md (it may land after geeky-plan starts)
        spec = [c for c in self.commits if any(fn.endswith("/feature-specification.md") for fn, _ in c["files"])][:1] \
            or [c for c in self.commits if c["stage"] == "spec-research"]
        ok = any(SPEC_SUBJECT.match(c["subject"]) for c in spec)
        self.prescribed["spec"] = self.check("adh.prescribed_msg.spec", A, "pass" if ok else "fail",
                                             "; ".join(c["subject"] for c in spec) or "no spec commit")
        pr = [c for c in self.commits if c["stage"] == "plan-review"] or \
            [c for c in self.commits if re.search(r"(?i)plan[ -]review", c["subject"])]
        ok = any(c["subject"] == PLAN_REVIEW_SUBJECT for c in pr)
        self.prescribed["plan_review"] = self.check("adh.prescribed_msg.plan_review", A, "pass" if ok else "fail",
                                                    "; ".join(c["subject"] for c in pr) or "no plan-review commit")
        self.critical = self.impl_review_critical()
        if self.critical:
            # A finding in the docs gets a docs-only fix, so take the prescribed subject first and fall back
            # to the first impl-review commit that touches code.
            review = [c for c in self.commits if c["stage"] == "impl-review"]
            fix = next((c for c in review if FIX_SUBJECT.match(c["subject"])), None) or \
                next((c for c in review if any(not fn.startswith("docs/") for fn, _ in c["files"])), None)
            ok = bool(fix and FIX_SUBJECT.match(fix["subject"]))
            self.prescribed["critical_fix"] = self.check(
                "adh.prescribed_msg.critical_fix", A, "pass" if ok else "fail",
                f"critical findings in impl-review; fix commit: {fix['subject'] if fix else 'none'}")
        else:
            self.prescribed["critical_fix"] = self.check("adh.prescribed_msg.critical_fix", A, "na",
                                                         "no critical impl-review finding")
        base = (pr[-1] if pr else next((c for c in reversed(self.commits)
                                       if c["stage"] in ("spec-research", "geeky-plan")), None))
        changed = []
        if base:
            names = git(self.ws, "diff", "--name-only", base["sha"], "HEAD", "--", self.folder_rel).splitlines()
            changed = [n for n in names if n.rsplit("/", 1)[-1] in FROZEN or
                       re.search(r"/tasks/T\d+[^/]*\.md$", n) and not n.endswith((".notes.md",))]
        self.frozen_modified = bool(changed)
        self.check("adh.frozen_untouched", A, "fail" if changed else "pass",
                   f"git diff {base['short'] if base else '?'}..HEAD on frozen files: {changed or 'empty'}")
        bad = self.forbidden_git()
        self.forbidden = bool(bad)
        self.check("adh.no_forbidden_git", A, "fail" if bad else "pass",
                   f"tool-input matches: {bad}" if bad else "no push/--force/--amend/--no-verify in tool inputs"
                   + ("; reflog clean" if self.reflog_checked else ""))
        self.check("adh.one_concern_per_commit", A, None, "judge: one concern per commit", kind="judge")
        self.check("adh.no_orchestrator_code_edits", A, "fail" if self.d87 else "pass",
                   "; ".join(f"call {c.idx} ({self.trace.stage_of(c)}): {', '.join(p)}" for c, p in self.d87)
                   or "none")
        self.check("adh.final_report_claims", A, None, "judge: final report claims (D8)", kind="judge")
        hb = self.folder / ".heartbeat"
        try:
            status = json.loads(hb.read_text(encoding="utf-8-sig")).get("status")
        except (OSError, ValueError):
            status = None
        self.check("adh.heartbeat_final", A, "pass" if status in ("done", "paused") else "fail",
                   f".heartbeat status={status}")
        ho = self.folder / "handoff.md"
        rs = ho.exists() and re.search((r"^#{1,6}\s*(?:Implementation\s+)?Run Summary" if self.strict else r"^#{1,6}\s*Run Summary"), ho.read_text(encoding="utf-8", errors="replace"),
                                       re.M | re.I)
        self.check("adh.run_summary", A, "pass" if rs else "fail", "Run Summary heading in handoff.md"
                   if rs else "no Run Summary heading in handoff.md")
        scored = [c for c in self.checks if c["dimension"] == A and c["status"] in ("pass", "fail")]
        passed = sum(c["status"] == "pass" for c in scored)
        return {"passed": passed, "applicable": len(scored), "score": 100 * passed / len(scored) if scored else 0.0,
                "needs_judge": [c["id"] for c in self.checks if c["dimension"] == A and c["status"] == "needs_judge"]}

    def phase_records(self):
        texts = []
        ho = self.folder / "handoff.md"
        if ho.exists():
            texts.append(("handoff.md", ho.read_text(encoding="utf-8", errors="replace")))
        for p in (self.folder / "tasks").glob("*.notes.md"):
            texts.append((p.name, p.read_text(encoding="utf-8", errors="replace")))
        out = {}
        for p, ids in self.phases.items():
            num = p[1:]
            for name, text in texts:
                blocks = re.split(r"\n(?=#)|\n\s*\n", text)
                for b in blocks:
                    if "READY TO ADVANCE" in b and (set(ids) & expand_ids(b) or re.search(rf"\bPhase\s*{num}\b", b, re.I)):
                        out.setdefault(p, name)
        return out

    def last_code_edit(self, t, before):
        if before is None:
            return None
        idxs = []
        ids = {d.id for d in self.coders.get(t, [])}
        for c in self.trace.calls[:before]:
            if (not c.orch and c.top.id in ids) or c.orch:
                if any(is_code_path(p) and self.owner.get(p) == t for p in self.call_writes(c)):
                    idxs.append(c.idx)
        return max(idxs) if idxs else None

    def impl_review_critical(self):
        if (self.trace.process or {}).get('critical_findings'):
            return True
        for c in self.trace.agents("impl-review"):
            s = self.trace.agent_summary.get(c.id, "")
            if re.search(r"Verdict\W{0,6}(?:\*\*)?\s*NEEDS FIXES", s, re.I):
                return True
        return False

    def forbidden_git(self):
        bad = []
        pat = re.compile(r"\bgit\b[^\n;|&]*\bpush\b|--no-verify\b|\bgit\b[^\n;|&]*--amend\b|"
                         r"\bgit\s+(?!add\b)[^\n;|&]*\s--force(?:-with-lease)?\b|\bgit\b[^\n;|&]*\bpush\s+-f\b")
        for c in self.trace.calls:
            if c.name in ("Bash", "PowerShell"):
                for m in pat.finditer(c.cmd):
                    bad.append(f"call {c.idx}: {m.group(0)[:60]}")
        self.reflog_checked = False
        if self.ws_source == "run.json workspace":
            r = git(self.ws, "reflog", "--format=%gs", check=False)
            self.reflog_checked = True
            bad += [f"reflog: {l}" for l in r.splitlines() if re.match(r"commit \(amend\)|reset:", l)]
        return bad

    def consistency(self):
        C, T = "consistency", self.trace
        pts = []

        def dp(cid, name, status, ev, kind="det"):
            st = self.check(cid, C, status, ev, kind)
            pts.append({"id": cid, "name": name, "weight": 2 if cid in CRITICAL_DPS else 1, "status": st,
                        "evidence": self.checks[-1]["evidence"]})

        def concurrency(calls):
            iv = [(T.dispatch_start(c), T.agent_end.get(c.id) or c.rts or c.ts) for c in calls]
            return max((sum(1 for s, e in iv if s <= s0 < e) for s0, _ in iv), default=0)

        spec = T.agents("spec-research")
        n = concurrency(spec)
        dp("dp01", "Research team: 3 parallel researchers", "pass" if len(spec) >= 3 and n >= 3 else "fail",
           f"{len(spec)} dispatches, max {n} concurrent")
        dp("dp02", "Spec commit, prescribed message", self.prescribed["spec"], "see adh.prescribed_msg.spec")
        pm = [p.name for p in self.folder.glob("review-*.md") if re.search(r"project-manager|pm", p.name, re.I)]
        dp("dp03", "PM review artifact", "pass" if pm else "fail", ", ".join(pm) or "no PM review file")
        prv = T.agents("plan-review")
        dp("dp04", "Plan-review subagents, phases 1 and 3", "pass" if len(prv) >= 2 else "fail",
           f"{len(prv)} plan-review dispatches")
        dp("dp05", "Plan-review commit, exact message", self.prescribed["plan_review"],
           "see adh.prescribed_msg.plan_review")
        coders = [d for ds in self.coders.values() for d in ds if T.stage_of(d) == "geeky-implement"]
        conc = concurrency(list({d.id: d for d in coders}.values()))
        det7 = conc <= 3
        if det7 and "dp07.justification" not in self.manual and "dp07" not in self.manual:
            dp("dp07", "Batching <=3 parallel, justification noted", None,
               f"max {conc} concurrent coders; justification needs judge (dp07.justification)", kind="judge")
        else:
            j = self.manual.get("dp07.justification", {"pass": True})
            dp("dp07", "Batching <=3 parallel, justification noted",
               "pass" if det7 and j.get("pass") else "fail",
               f"max {conc} concurrent coders; justification: {j.get('note', 'judged')}")
        ip_missing = [t for t in self.tasks if t not in self.inprog]
        dp("dp08", "In Progress lane move with timestamp", "fail" if ip_missing else "pass",
           f"no In Progress move: {ip_missing or 'none'}")
        tr = [t for t in self.tasks if self.status(f"adh.task_review.{t}") != "pass"]
        dp("dp09", "Task review, per task", "fail" if tr else "pass", f"not reviewed: {tr or 'none'}")
        gb = [t for t in self.tasks if not self.gates_ok.get(t)]
        dp("dp10", "DoD + kanban gates before Done (D86)", "fail" if gb else "pass", f"failing: {gb or 'none'}")
        ph = [p for p, ok in self.phase_ok.items() if not ok]
        dp("dp11", "Phase verdict from a phase review", "fail" if ph else "pass", f"failing phases: {ph or 'none'}")
        dp("dp12", "Commit gate ran before each commit (D79)", *self.commit_gate_before())
        dp("dp13", "One concern per commit, per phase", None, "judge", kind="judge")
        hb = [t for t in self.tasks if self.status(f"adh.handoff_batch.{t}") != "pass"]
        dp("dp14", "Handoff at each Done move (D82)", "fail" if hb else "pass", f"failing: {hb or 'none'}")
        dp("dp15", "Run Summary appended", self.status("adh.run_summary"), "see adh.run_summary")
        irv = [c for c in T.agents("impl-review") if "reviewer" in str(c.inp.get("subagent_type", ""))
               or re.search(r"(?i)review", str(c.inp.get("description", "")))]
        dp("dp16", "Final review: 3 domain reviewers", "pass" if len(irv) >= 3 else "fail",
           f"{len(irv)} impl-review reviewer dispatches")
        if self.critical:
            dp("dp17a", "Re-review after a critical fix", None, "judge", kind="judge")
            dp("dp17b", "Fix commit, prescribed message", self.prescribed["critical_fix"],
               "see adh.prescribed_msg.critical_fix")
        else:
            dp("dp17a", "Re-review after a critical fix", "na", "no critical finding")
            dp("dp17b", "Fix commit, prescribed message", "na", "no critical finding")
        dp("dp18", ".heartbeat finalized", self.status("adh.heartbeat_final"), "see adh.heartbeat_final")
        value, total = dpc(pts)
        observed = [s for _, s, _, _ in T.skills]
        oc = order_conformance(observed)
        return {"score": consistency_score(value, oc), "DPC": value,
                "DPC_fraction": f"{sum(p['weight'] for p in pts if p['status'] == 'pass')}/{total}",
                "OC": oc, "observed_sequence": observed, "decision_points": pts,
                "needs_judge": [p["id"] for p in pts if p["status"] == "needs_judge"]}

    def commit_gate_before(self):
        impl = [c for c in self.commits if c["stage"] == "geeky-implement"]
        by_call = {}
        for c in impl:
            if c["call"]:
                by_call.setdefault(c["call"].idx, []).append(c["short"])
        prev, short = -1, []
        for idx in sorted(by_call):
            avail = sum(n for i, n in self.commit_gate_runs.items() if prev < i <= idx)
            if avail < len(by_call[idx]):
                short.append(f"call {idx}: {avail} check-commit run(s) for {len(by_call[idx])} commit(s)")
            prev = idx
        unmapped = [c["short"] for c in impl if not c["call"]]
        if unmapped:
            short.append(f"commits not mapped to a call: {unmapped}")
        return ("fail" if short or not impl else "pass",
                "; ".join(short) or f"{len(impl)} geeky-implement commits each preceded by check-commit")

    def status(self, cid):
        return next((c["status"] for c in self.checks if c["id"] == cid), None)

    def task_tier(self):
        l_run = self.run.get("latency_median_ms") or self.analysis.get("latency_median_ms")
        timeout = self.case.get("task_timeout_s", 1200)
        loc = {t: 0.0 for t in self.tasks}
        for cm in self.commits:
            named = [t for t in cm["tasks"] if t in loc]
            if not named:
                continue
            for fn, n in cm["files"]:
                if fn.startswith("docs/") or fn.startswith(self.folder_rel + "/"):
                    continue
                if len(named) == 1:
                    loc[named[0]] += n
                elif self.owner.get(fn) in named:
                    loc[self.owner[fn]] += n
                else:
                    for t in named:
                        loc[t] += n / len(named)
        rows = []
        for t in self.tasks:
            counted = [(i, v) for i, v in self.runs.get(t, []) if not v["excluded"]]
            excluded = [(i, v["excluded"]) for i, v in self.runs.get(t, []) if v["excluded"]]
            a = len(counted) + len(self.d87_by_task.get(t, []))
            rerun_ok = all(r["exit"] == 0 for r in self.rerun_results.get(t, {}).get("results", [])) \
                if self.rerun else True
            passed = t in self.first_pass and rerun_ok
            V = v_score(a, passed)
            rv = [r for r in self.reviews if t in r["ids"]]
            first_ok = next((k for k, r in enumerate(rv, 1) if r["pass"]), None)
            R = r_score(first_ok)
            start = min((self.trace.dispatch_start(d) for d in self.coders.get(t, [])), default=None)
            ends = []
            if t in self.first_pass:
                ends.append(self.first_pass[t].rts)
            if first_ok:
                ends.append(rv[first_ok - 1]["end"])
            if not ends and self.done_call.get(t):
                ends.append(self.done_call[t].ts)
            d_s = (max(e for e in ends if e) - start).total_seconds() if start and any(ends) else None
            Tt = t_score(d_s, l_run, timeout)
            D = 1.0 if self.final_dod.get(t) else 0.0
            rows.append({"task": t, "attempts": a, "attempt_calls": [i for i, _ in counted],
                         "d87_calls": self.d87_by_task.get(t, []), "excluded_runs": excluded,
                         "first_pass_call": self.first_pass[t].idx if t in self.first_pass else None,
                         "rerun_ok": rerun_ok if self.rerun else None, "V": V, "review_rounds": first_ok,
                         "R": R, "d_s": round(d_s, 1) if d_s is not None else None, "T": Tt, "D": D,
                         "s": task_score(V, R, Tt, D), "LOC": loc[t]})
        ws = task_weights([r["LOC"] for r in rows], [r["attempts"] for r in rows])
        for r, w in zip(rows, ws):
            r["w"] = w
        return {"score": sum(r["w"] * r["s"] for r in rows), "L_run_ms": l_run, "L_ref_ms": L_REF_MS,
                "task_timeout_s": timeout, "tasks": rows}

    def hard_gates(self, adh_needs):
        fc = self.manual.get("hg.false_completion_claim")
        gates = {"oracle_fail": self.oracle_fail, "frozen": self.frozen_modified, "forbidden_git": self.forbidden,
                 "false_completion": bool(fc) and not fc.get("pass", True)}
        rows = [
            {"id": "hg.oracle", "fired": self.oracle_fail, "effect": "Quality <= 40", "kind": "det"},
            {"id": "hg.frozen", "fired": self.frozen_modified, "effect": "Adherence <= 30", "kind": "det"},
            {"id": "hg.forbidden_git", "fired": self.forbidden, "effect": "Adherence = 0", "kind": "det"},
            {"id": "hg.false_completion_claim", "fired": gates["false_completion"] if fc else None,
             "effect": "Adherence <= 50", "kind": "judge",
             "status": ("manual: " + fc.get("note", "")) if fc else "needs_judge (no cap applied)"},
        ]
        return gates, rows

    def score(self):
        self.load()
        self.derive()
        self.attempts()
        self.rerun_blocks()
        q = self.quality()
        a = self.adherence()
        c = self.consistency()
        t = self.task_tier()
        gates, gate_rows = self.hard_gates(a["needs_judge"])
        qc, ac = apply_caps(q["score"], a["score"], gates)
        q["capped"], a["capped"] = qc, ac
        comp = composite(qc, ac, c["score"], t["score"])
        incomplete = []
        if self.strict:
            incomplete.extend((self.trace.process or {}).get('missing', []))
            for row in t['tasks']:
                task = row['task']
                if not self.coders.get(task): incomplete.append(f'{task}: missing coder ownership')
                if not row['attempts']: incomplete.append(f'{task}: missing validation attempts')
                if row['d_s'] is None: incomplete.append(f'{task}: missing task duration')
                if not any(task in r['ids'] for r in self.reviews): incomplete.append(f'{task}: missing review results')
                if task not in self.done_call: incomplete.append(f'{task}: missing Done evidence')
                if not self.rerun_results.get(task, {}).get('results'): incomplete.append(f'{task}: missing own-commit validation results')
            expected = (self.trace.process or {}).get('expected_implementation_commits', [])
            for entry in expected:
                if self.status('adh.commit_msg.' + entry['sha'][:7]) is None:
                    incomplete.append('missing implementation commit check: ' + entry['sha'])
            if not expected and not any(c['stage'] == 'geeky-implement' for c in self.commits):
                incomplete.append('missing implementation commit coverage')
        formula_comp = comp
        if incomplete: comp = None
        judge = [ch["id"] for ch in self.checks if ch["status"] == "needs_judge"]
        return {
            "schema": "geeky-eval.score/1",
            "run": self.run_dir.name, "case": self.run.get("case"), "plugin_sha": self.run.get("plugin_sha"),
            "harness": self.run.get("harness"), "model": self.run.get("model"),
            "scored_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scorer": "evals/scripts/score-run.py", "workspace": str(self.ws), "workspace_source": self.ws_source,
            "planning_folder": self.folder_rel, "validation_rerun": self.rerun and not bool(self.validation_cache_path),
            "validation_evidence": "reused recorded results" if self.validation_cache_path else "fresh rerun" if self.rerun else "trace only",
            "manual_file_used": bool(self.manual),
            "composite": comp,
            "process_completeness": {'required': self.strict, 'complete': not incomplete, 'missing': incomplete},
            "process_evidence_origin": (self.trace.process or {}).get('origin', 'legacy'),
            "formula_composite_noncomparable": formula_comp if incomplete else None,
            "dimensions": {"quality": q, "adherence": a, "consistency": {k: v for k, v in c.items()
                                                                         if k != "decision_points"},
                           "task_tier": {k: v for k, v in t.items() if k != "tasks"}},
            "hard_gates": gate_rows,
            "tasks": t["tasks"], "phases": self.phases, "decision_points": c["decision_points"],
            "checks": self.checks, "needs_judge": judge,
            "rerun_results": self.rerun_results,
            "breakdown": {"tokens_total": (self.analysis.get("tokens") or {}).get("total"),
                          "wall_time_s": self.run.get("wall_time_s"),
                          "geeky_coder_used": any("coder" in str(d.inp.get("subagent_type", ""))
                                                  for ds in self.coders.values() for d in ds),
                          "orchestrator_code_edit_calls": [cl.idx for cl, _ in self.d87]},
        }


# ----------------------------------------------------------------------------- output

def fmt(x, n=1):
    return "-" if x is None else f"{x:.{n}f}"


def render_md(s):
    d = s["dimensions"]
    q, a, c, t = d["quality"], d["adherence"], d["consistency"], d["task_tier"]
    mark = {"pass": "✓", "fail": "✗", "na": "n/a", "needs_judge": "judge?"}
    out = [f"# Score: {s['run']} ({s['case']})", "",
           f"Plugin `{(s['plugin_sha'] or '')[:7]}`, {s['harness']} / {s['model']}. Scored {s['scored_utc']} by "
           f"`score-run.py`. Workspace: {s['workspace_source']}. Validation blocks "
           f"{s.get('validation_evidence', 're-run' if s['validation_rerun'] else 'NOT re-run (--no-rerun)')}. "
           f"Manual verdicts: {'yes' if s['manual_file_used'] else 'none'}.", "",
           "| Dimension | Weight | Score | Detail |", "|---|---|---|---|",
           f"| Quality | 0.35 | {fmt(q['capped'])} | P {q['P']:.3f}, S {q['S']:.3f}, D {q['D']:.2f} |",
           f"| Adherence | 0.35 | {fmt(a['capped'])} | {a['passed']}/{a['applicable']} |",
           f"| Consistency | 0.20 | {fmt(c['score'])} | DPC {c['DPC_fraction']} = {c['DPC']:.3f}, OC {c['OC']:.2f} |",
           f"| Task tier | 0.10 | {fmt(t['score'])} | L_run {t['L_run_ms']} ms, L_ref {t['L_ref_ms']} ms |",
           f"| **Composite** | | **{fmt(s['composite'])}** | |", ""]
    out += ["## Hard gates", "", "| Gate | Fired | Effect |", "|---|---|---|"]
    out += [f"| {g['id']} | {g.get('status') or ('yes' if g['fired'] else 'no')} | {g['effect']} |"
            for g in s["hard_gates"]]
    out += ["", "## Adherence checks", "", "| Check | Result | Evidence |", "|---|---|---|"]
    out += [f"| {ch['id']} | {mark[ch['status']]} | {ch['evidence'][:150].replace('|', '/')} |"
            for ch in s["checks"] if ch["dimension"] == "adherence"]
    out += ["", "## Decision points", "", "| # | Decision point | W | Result | Evidence |", "|---|---|---|---|---|"]
    out += [f"| {p['id']} | {p['name']} | {p['weight']} | {mark[p['status']]} | "
            f"{p['evidence'][:120].replace('|', '/')} |" for p in s["decision_points"]]
    out += ["", "## Task tier", "", "| Task | attempts | V | R | d (s) | T | D | s | LOC | w |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    out += [f"| {r['task']} | {r['attempts']} (calls {r['attempt_calls']}"
            f"{' + D87 ' + str(r['d87_calls']) if r['d87_calls'] else ''}) | {r['V']:.2f} | {r['R']:.1f} | "
            f"{fmt(r['d_s'], 0)} | {r['T']:.2f} | {r['D']:.0f} | {r['s']:.1f} | {r['LOC']:.0f} | {r['w']:.3f} |"
            for r in s["tasks"]]
    if s["needs_judge"]:
        out += ["", "## Needs judge", "", "Excluded from their denominators until a `--manual` verdict is given:", ""]
        out += [f"- `{i}`" for i in s["needs_judge"]]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--manual", type=Path, help="JSON {check_id: {pass, note}} with judgement verdicts")
    ap.add_argument("--no-rerun", action="store_true", help="do not re-run validation blocks; use recorded evidence")
    ap.add_argument("--out", type=Path, help="output folder (default: the run folder)")
    ap.add_argument("--workspace", type=Path, help="override the workspace path")
    ap.add_argument("--ref-loc", type=int, help="reference non-test LOC (default: case spec band upper bound)")
    ap.add_argument('--harness', choices=('claude-code', 'codex'))
    ap.add_argument('--process-evidence', type=Path)
    ap.add_argument('--validation-cache', type=Path)
    ap.add_argument('--require-complete', action='store_true')
    args = ap.parse_args(argv)
    if not (args.run_dir / "run.json").exists() or not (args.run_dir / "stream.jsonl").exists():
        print(f"not a run folder (run.json + stream.jsonl): {args.run_dir}", file=sys.stderr)
        return 2
    manual = json.loads(args.manual.read_text(encoding="utf-8-sig")) if args.manual else {}
    try:
        s = Scorer(args.run_dir, manual, rerun=not args.no_rerun, workspace=args.workspace, ref_loc=args.ref_loc, harness=args.harness, process_path=args.process_evidence, validation_cache=args.validation_cache, require_complete=args.require_complete, output_dir=args.out).score()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = args.out or args.run_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "score.json").write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")
    (out / "score.md").write_text(render_md(s), encoding="utf-8")
    d = s["dimensions"]
    print(json.dumps({"composite": round(s["composite"], 1) if s["composite"] is not None else None, "quality": round(d["quality"]["capped"], 1),
                      "adherence": round(d["adherence"]["capped"], 1),
                      "consistency": round(d["consistency"]["score"], 1),
                      "task_tier": round(d["task_tier"]["score"], 1), "needs_judge": s["needs_judge"]}, indent=2))
    return 1 if args.require_complete and s['composite'] is None else 0


if __name__ == "__main__":
    sys.exit(main())
