"""Harness-neutral process evaluation of one geeky-orchestration run, for Claude Code vs Codex parity.

score-run.py reads each harness's raw transcript through its own parser, so its Adherence and
Consistency are only comparable when both parsers fit (the Codex one expects Windows PowerShell board
edits and a delegated implement orchestrator). This script reads every run the same way:

  - Langfuse observations, which both tracers emit in one shape (see telemetry/langfuse-coverage.py),
  - the git history, whose commit times mark the stage boundaries for both harnesses,
  - the final planning artifacts, checked with the plugin's own gate scripts.

Quality is taken from score-run's score.json (oracle, size, validation reruns: artifact-based, so
already neutral). Parity composite = (0.35 Q + 0.35 A + 0.20 C) / 0.90, score-run's weights without
the latency-based task tier. Judge items come from --manual, as in score-run.

  python parity-eval.py <run-folder> --observations <obs.json> --score <score.json> [--manual m.json] --out <dir>
"""
import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluation_scope import bind_run, load_scope, scoped_efficiency

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
# The version parity.json records and langfuse-scores.py publishes under (score ids bind it). Raise it with every
# change that can move a published number.
EVALUATOR = 'parity-eval/1.3'
SPEC_SUBJECT = re.compile(r'^docs: author feature specification in \S+$')
PLAN_REVIEW_SUBJECT = 'chore(plan): Plan review — align documents, fix sequencing'
# impl-review SKILL.md prescribes "Address N required review findings"; "critical" is the older wording (as score-run)
FIX_SUBJECT = re.compile(r'^fix\(spec\): Address \d+ (?:required|critical) review findings from impl-review$')
# "## Run Summary", or one qualified as Implementation / Final / Overall / Session, on one line (as score-run)
RUN_SUMMARY = re.compile(r'(?im)^#{1,6}[ \t]*(?:(?:Implementation|Final|Overall|Session)[ \t]+)?Run Summary\b')
FROZEN = ('implementation-plan.md', 'feature-specification.md', 'draft.md', 'references.md')
CRITICAL = {'dp02', 'dp05', 'dp09', 'dp10', 'dp11'}
SPAWN = {'Tool: Agent', 'Tool: Task', 'spawn_agent'}
REDISPATCH = {'followup_task', 'send_message', 'Tool: SendMessage'}
SHELL = {'Tool: Bash', 'Tool: PowerShell', 'exec', 'exec_command', 'shell', 'local_shell'}
EDIT = {'Tool: Write', 'Tool: Edit', 'Tool: MultiEdit', 'apply_patch'}
REVIEW_FINAL = re.compile(r'impl[-_ ]review|\bimpl_\w*review|final (implementation )?review|implementation review'
                          r'|expert reviewing \S+ implementation', re.I)
# Workers dispatched by the implement stage; their briefs mention impl-review only to defer it.
IMPLEMENT_ROLE = re.compile(r'implement-orchestrator|geeky-coder', re.I)
# A mention that defers or forbids the stage ("do not run impl-review", "before impl-review").
NEGATED = re.compile(r"(?i)\b(?:do not|don't|never|not|before|until|without)\b[^.\n]{0,40}$")
# A shell command that writes a file: redirection, tee, sed -i, apply_patch, or a Python write.
WRITES = re.compile(r'>>?\s*([\w./-]+)|\btee\s+(?:-a\s+)?([\w./-]+)|\bsed\s+-i\S*\s.*?([\w./-]+\.\w+)'
                    r"|\*\*\* (?:Add|Update) File: ([\w./-]+)|open\(\s*['\"]([\w./-]+)['\"]\s*,\s*['\"][wa]"
                    r"|Path\(\s*['\"]([\w./-]+)['\"]\s*\)\.write_text")


def _coverage_module():
    spec = importlib.util.spec_from_file_location('cov', HERE.parent / 'telemetry/langfuse-coverage.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


COV = _coverage_module()


def _analyze_module():
    spec = importlib.util.spec_from_file_location('analyze_run', HERE / 'analyze-run.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AR = _analyze_module()  # shared shell-reading helpers (gate runs, heredoc and quote masking), as score-run uses


def git(ws, *args):
    return subprocess.run(['git', '-C', str(ws), *args], capture_output=True, text=True, encoding='utf-8').stdout


def utc_time(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Evaluation timestamps require an explicit timezone')
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec='microseconds')


def commits(ws):
    out = []
    for line in git(ws, 'log', '--reverse', '--format=%H%x1f%cI%x1f%s').splitlines():
        sha, when, subject = line.split('\x1f')
        files = [f for f in git(ws, 'show', '--name-only', '--format=', sha).splitlines() if f]
        out.append({'sha': sha, 'time': utc_time(when), 'subject': subject, 'files': files,
                    'body': git(ws, 'log', '-1', '--format=%B', sha)})
    return out


def head_moves(ws):
    """Every commit HEAD ever pointed at, with the time it got there (survives reset/amend rewrites)."""
    out = []
    for line in git(ws, 'reflog', '--date=iso-strict', '--format=%H%x1f%gd%x1f%gs').splitlines():
        sha, ref, subject = line.split('\x1f')
        if not subject.startswith('commit'):
            continue
        when = re.search(r'\{(.+)\}', ref).group(1)
        files = [f for f in git(ws, 'show', '--name-only', '--format=', sha).splitlines() if f]
        out.append({'sha': sha, 'time': utc_time(when), 'subject': git(ws, 'log', '-1', '--format=%s', sha).strip(), 'files': files})
    return sorted(out, key=lambda c: c['time'])


def events(observations):
    """Tool calls in time order, each tagged main/subagent, with decoded input text."""
    by_id = {o['id']: o for o in observations}
    if len(by_id) != len(observations):
        raise ValueError('Ambiguous duplicate observation IDs; supply source-reviewed logical inputs')

    def thread(o):
        seen, parent = set(), by_id.get(o.get('parentObservationId'))
        while parent and parent['id'] not in seen:
            if COV.is_subagent_container(parent):
                return 'sub'
            seen.add(parent['id'])
            parent = by_id.get(parent.get('parentObservationId'))
        return 'main'

    out = []
    for o in observations:
        if o.get('type') != 'TOOL':
            continue
        name = str(o.get('name', ''))
        raw = COV.decoded(o.get('input'))
        body = re.sub(r'\\[nrt]', '\n', COV.text(raw))
        kind = ('spawn' if name in SPAWN else 'redispatch' if name in REDISPATCH else 'shell' if name in SHELL
                else 'edit' if name in EDIT else 'skill' if name.startswith('skill:') or name == 'Tool: Skill' else 'other')
        role = ''
        if kind == 'spawn' and isinstance(raw, dict):
            role = raw.get('agent_type') or raw.get('subagent_type') or ''
        paths = []
        if kind == 'edit' and isinstance(raw, dict) and raw.get('file_path'):
            paths = [raw['file_path']]
        elif kind in ('shell', 'edit'):
            paths = [p for m in WRITES.finditer(body) for p in m.groups() if p]
        # the shell command itself, for the shell readers (body is the input as text: JSON quoting wraps the command)
        cmd = next((raw[k] for k in ('command', 'cmd') if isinstance(raw, dict) and isinstance(raw.get(k), str)), body)  # Claude, Codex
        out.append({'t': utc_time(o['startTime']) if o.get('startTime') else '', 'name': name, 'kind': kind, 'thread': thread(o),
                    'end': utc_time(o['endTime']) if o.get('endTime') else '',
                    'role': role, 'text': body, 'cmd': cmd, 'paths': paths, 'output': COV.text(o.get('output'))})
    return sorted(out, key=lambda e: e['t'])


def final_review_dispatch(event):
    """A spawn that starts final implementation review: not an implement worker, not a phase
    review, and at least one review mention that is not deferred or forbidden."""
    text = event['text']
    if event['kind'] != 'spawn' or IMPLEMENT_ROLE.search(event.get('role', '')):
        return False
    if re.search(r'(?i)\bphase\s*\d', text[:400]):
        return False
    return any(not NEGATED.search(text[max(0, m.start() - 60):m.start()]) for m in REVIEW_FINAL.finditer(text))


def latency_median_ms(observations):
    values = [o['latency'] * 1000 for o in observations if o.get('type') == 'GENERATION' and o.get('latency')]
    return round(statistics.median(values)) if values else None


def literal_kanban_run(event):
    """True when a call runs validate-kanban by naming it, HEAD's literal test, except when every mention in the
    command is inside a source the call writes to a file (a heredoc lane helper, a PowerShell here-string, a note
    appended to a .md): the moves that helper makes are counted by kanban_helper_runs."""
    cmd = event.get('cmd', event['text'])
    return 'validate-kanban' in event['text'] and ('validate-kanban' not in cmd or 'validate-kanban' in AR.mask_written_sources(cmd))


def kanban_helper_runs(defs, calls):
    """validate-kanban runs made inside a lane helper the orchestrator wrote and whose own source runs the gate, so each
    lane move it makes runs the gate. Helpers are read by analyze-run's gating_helper_scripts: a bash heredoc script
    that touches kanban.md (C3 Opus run 20261104T083537Z, `cat > $T/lanes.py <<'EOF'`) or a PowerShell here-string
    piped into Set-Content / Out-File; and here, a .ps1 written with the Write tool whose content touches kanban.md
    and runs validate-kanban (score-run registers that one only from a main-thread call that writes kanban.md). The
    moves are read by analyze-run's helper_moves, the same reader score-run uses."""
    helpers = {name for e in defs for name in AR.gating_helper_scripts(e.get('cmd', e['text']))}
    for e in defs:  # a .ps1 helper written with the Write tool whose content touches kanban.md and runs validate-kanban (as score-run)
        if e.get('kind') == 'edit' and str(e.get('name', '')).endswith('Write') and 'validate-kanban' in e['text'] and 'kanban.md' in e['text']:
            helpers |= {p.replace('\\', '/').rsplit('/', 1)[-1].lower() for p in e.get('paths', []) if p.lower().endswith('.ps1')}
    return sum(len(AR.helper_moves(e.get('cmd', e['text']), helpers)) for e in calls) if helpers else 0


def commit_gate_runs(event, commits=0):
    """check-commit passes in one shell call; commits is the number of commits made while the call ran."""
    text = event['text']
    if 'check-commit' not in text:
        return 0
    # One captured shell call may invoke a checked commit helper several times. Count each recorded PASS (verdict lines
    # read as analyze-run reads them), or each call of a helper function that runs the gate, counted as analyze-run
    # counts helper functions. A helper that sends the gate's output away (C3 Opus run 20261104T083537Z:
    # `check-commit.py $m >/dev/null || …`) counts by its calls. A helper that prints the verdict counts by its calls
    # only when no verdict is shown at all (the output is missing); otherwise its shown verdicts are its runs. A call
    # whose output shows no verdict keeps the established one-call fallback (score-run counts each literal call). Only
    # passes count: a failed gate does not cover the commit made after it, so a call that shows only failures counts
    # none. When a gate failure is shown, which helper calls passed is not in the output. The hidden passes are then
    # at most the quiet helper calls less the shown failures that the helpers printing their verdict cannot account
    # for (a helper that commits despite its failed gate would otherwise turn that commit into a pass; stored C1
    # csvledger 20261020T155628Z), and, when every commit of the call is inside a function that runs the gate, at
    # most the commits the call made. Otherwise none are credited, and hidden passes of literal gate calls are not
    # credited once any verdict is shown (a failure that is itself hidden cannot be seen either). score-run's
    # gate_outcomes bounds its hidden runs by the same commits less the shown passes; it counts runs, failures too,
    # and compares them with the commits of each call. Shown and hidden passes are not added: a helper that filters
    # its output (`| tail -1`) may both show a pass and count as hiding it, so the larger of the two is credited.
    # MCP check_commit tool calls are not counted here (score-run's gate_outcomes counts them).
    out = event.get('output', '')
    seen = AR.gate_verdicts(out, 'check-commit')
    passes, fails = sum(seen), len(seen) - sum(seen)
    cmd = event.get('cmd', text)
    runs, _, _, quiet = AR.helper_function_gate_runs(cmd)
    helper = quiet['check-commit'] if seen else runs['check-commit']
    if fails:
        loud = runs['check-commit'] - quiet['check-commit']
        helper = min(max(0, helper - max(0, fails - loud)), commits if AR.commits_only_in_helpers(cmd) else 0)
    return max(passes, helper) if seen else max(1, helper)


_GIT = r'\bgit(?:\s+-[Cc]\s+\S+|\s+--[\w.-]+(?:=\S+)?)*\s+'
FORBIDDEN_GIT = re.compile(_GIT + r'push\b|\bgit\b[^\n;|&]*\s(?:--no-verify|--amend)\b|'
                           r'\bgit\s+(?!add\b|stash\b)[^\n;|&]*\s--force(?:-with-lease)?\b')


def forbidden_git_command(text):
    """A push, --force, --amend or --no-verify git command. `push` must be git's subcommand: `git stash push`
    is a local stash. Event text is JSON, so escaped newlines are decoded first to keep commands apart."""
    return bool(FORBIDDEN_GIT.search(text.replace('\\n', '\n')))


def gate(script, *args):
    r = subprocess.run([sys.executable, str(PLUGIN / 'scripts' / f'{script}.py'), *args], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[:1]


class Evaluation:
    def __init__(self, run_dir, observations, score, manual, scope=None):
        self.run = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
        if scope:
            bind_run(scope, self.run)
        self.ws = Path(self.run['workspace'])
        self.folder = self.ws / 'docs/spec-001-feature'
        self.commits = commits(self.ws)
        self.events = events(observations)
        self.latency = latency_median_ms(observations)
        self.score = score
        self.manual = manual
        self.scope = scope
        self.checks = []
        # Tasks and phases as score-run parsed them from the plan, so both evaluations agree.
        self.phases = {p: list(ts) for p, ts in score['phases'].items()} if isinstance(score.get('phases'), dict) else \
            {p['phase']: p['tasks'] for p in score['phases']}
        self.tasks = sorted({t for ts in self.phases.values() for t in ts}, key=lambda t: int(t[1:]))
        self.moves = head_moves(self.ws)
        self.bounds = self.stage_bounds()

    # ------------------------------------------------------------------ structure
    def stage_bounds(self):
        """Stage start times from the commit history; impl-review starts with its first reviewer dispatch."""
        def first(pred):
            return next((c for c in self.moves if pred(c)), None)
        spec = first(lambda c: SPEC_SUBJECT.match(c['subject']))
        plan = first(lambda c: any(f.endswith('implementation-plan.md') for f in c['files']))
        review = first(lambda c: c['subject'] == PLAN_REVIEW_SUBJECT) or first(lambda c: re.search(r'(?i)plan[ -]review', c['subject']))
        start = self.events[0]['t'] if self.events else ''
        # A stage starts at the earlier of its commit boundary and the first time its skill is invoked
        # after the previous commit boundary. Claude commits a stage late (its plan commit came after
        # plan-review); Codex reads every SKILL.md up front, so a read before that boundary is ignored.
        def stage_of(e):
            m = re.match(r'skill:([\w-]+)$', e['name']) or re.search(r'geeky-orchestration:([\w-]+)', e['text'][:200])
            return m.group(1) if e['kind'] == 'skill' and m else None
        reads = [(self.iso(e['t']), stage_of(e)) for e in self.events if stage_of(e)]

        def seconds(t):
            return dt.datetime.fromisoformat(t).timestamp()

        def skill_at(stage, after):
            # Reads of several stages' skills within 5 s are pre-reading, not entering a stage.
            for t, st in reads:
                if st != stage or not after or t <= after:
                    continue
                if not any(o != stage and abs(seconds(t) - seconds(u)) < 5 for u, o in reads):
                    return t
            return None

        def earliest(*ts):
            ts = [t for t in ts if t]
            return min(ts) if ts else None
        spec_c, plan_c, review_c = (c['time'] if c else None for c in (spec, plan, review))
        spec_t = spec_c
        plan_dispatch = next((e['t'] for e in self.events if e['kind'] == 'spawn' and spec_c
                              and e['t'] > spec_c and re.search(r'(?i)plan-review', e['text'][:400])), None)
        plan_t = earliest(plan_c, skill_at('plan-review', spec_c), plan_dispatch)
        review_t = earliest(review_c, skill_at('geeky-implement', plan_c))
        plan_end = review_t
        final = next((e for e in self.events if plan_end and self.iso(e['t']) > plan_end
                      and final_review_dispatch(e)), None)
        final_t = earliest(skill_at('impl-review', review_t), self.iso(final['t']) if final else None)
        return {'spec-research': (start, spec_t),
                'geeky-plan': (spec_t, plan_t),
                'plan-review': (plan_t, plan_end),
                'geeky-implement': (plan_end, final_t or '9999'),
                'impl-review': (final_t, '9999')}

    @staticmethod
    def iso(t):
        return utc_time(t)

    def within(self, stage):
        lo, hi = self.bounds[stage]
        return [e for e in self.events if lo and hi and lo <= self.iso(e['t']) < hi]

    def implement_code_commits(self, after):
        """Code commits (not docs-only) of `after` made before impl-review starts. The commit gate is a geeky-implement
        rule (skills/geeky-implement/SKILL.md:55, D79), and its runs are counted during geeky-implement, so an
        impl-review fix commit is in neither count (C2 20261102T053248Z: its fix commit's gate ran in impl-review).
        score-run dp12 counts geeky-implement runs too, per commit call, against every geeky-implement commit (docs
        commits included); here only code commits are counted, as before."""
        end = self.bounds['geeky-implement'][1]
        return [c for c in after if any(not f.startswith('docs/') for f in c['files']) and c['time'] < end]

    def commit_gate_passes(self, shells):
        """check-commit passes over the implement-stage shell calls (commit_gate_runs per call, with the commits each
        call made). The call that made the plan-review commit is left out: implement starts in that commit's second, so
        the call can fall inside the window, but its gate run covered the plan-review commit (C3 GPT 11:11:43).
        A commit is made by at most one call: the first shell call naming `commit` whose start-to-end seconds hold the
        commit time (docs commits too). score-run's commit_maker uses the same rule over all calls, and falls back to a
        call within 1.5 s; here only implement-stage shell calls are candidates and there is no fallback."""
        def window(e):  # commit times have whole seconds
            return e['t'][:19], (e.get('end') or e['t'])[:19]
        owner = {}
        for c in self.commits:
            for i, e in enumerate(shells):
                lo, hi = window(e)
                if 'commit' in e.get('cmd', e['text']) and lo <= c['time'][:19] <= hi:
                    owner[id(c)] = i
                    break
        total = 0
        for i, e in enumerate(shells):
            commits = [c for c in self.commits if owner.get(id(c)) == i]
            if not any(c['subject'] == PLAN_REVIEW_SUBJECT for c in commits):
                total += commit_gate_runs(e, len(commits))
        return total

    def check(self, cid, dim, status, evidence):
        if cid in self.manual:
            status, evidence = ('pass' if self.manual[cid]['pass'] else 'fail'), 'judge: ' + self.manual[cid].get('note', '')
        self.checks.append({'id': cid, 'dimension': dim, 'status': status, 'evidence': evidence})
        return status

    # ------------------------------------------------------------------ checks
    def evaluate(self):
        A, C = 'adherence', 'consistency'
        f = str(self.folder)
        for cid, script, args in (('a.planning_folder', 'validate-planning-folder', ['--path', f]),
                                  ('a.task_schema', 'validate-task-schema', ['--path', f]),
                                  ('a.kanban_final', 'validate-kanban', ['--path', f])):
            ok, out = gate(script, *args)
            self.check(cid, A, 'pass' if ok else 'fail', '; '.join(out))
        for t in self.tasks:
            ok, out = gate('check-dod', '--path', f, '--task', t)
            self.check(f'a.dod.{t}', A, 'pass' if ok else 'fail', '; '.join(out))
            notes = list(self.folder.glob(f'tasks/{t}-*.notes.md'))
            self.check(f'a.notes.{t}', A, 'pass' if notes and notes[0].stat().st_size else 'fail', notes[0].name if notes else 'missing')
        handoff = (self.folder / 'handoff.md').read_text(encoding='utf-8') if (self.folder / 'handoff.md').exists() else ''
        summary = 'pass' if RUN_SUMMARY.search(handoff) else 'fail'
        self.check('a.run_summary', A, summary, 'Run Summary heading in handoff.md')
        hb = self.folder / '.heartbeat'
        hb_text = hb.read_text(encoding='utf-8') if hb.exists() else ''
        heartbeat = 'pass' if re.search(r'(?i)status\W+(done|paused)', hb_text) else 'fail'
        self.check('a.heartbeat_final', A, heartbeat, hb_text.strip()[:120] or 'no .heartbeat')

        spec_ok = 'pass' if any(SPEC_SUBJECT.match(c['subject']) for c in self.commits) else 'fail'
        self.check('a.prescribed.spec', A, spec_ok, 'spec-research commit subject')
        pr_ok = 'pass' if any(c['subject'] == PLAN_REVIEW_SUBJECT for c in self.commits) else 'fail'
        self.check('a.prescribed.plan_review', A, pr_ok, 'plan-review commit subject exact')
        fixes = [c for c in self.commits if c['subject'].startswith('fix(spec)')]
        self.check('a.prescribed.critical_fix', A, ('pass' if all(FIX_SUBJECT.match(c['subject']) for c in fixes) else 'fail') if fixes else 'na',
                   '; '.join(c['subject'] for c in fixes) or 'no critical-fix commit')
        after = self.commits[[c['sha'] for c in self.commits].index(next(c['sha'] for c in self.commits if c['subject'] == PLAN_REVIEW_SUBJECT)) + 1:] \
            if pr_ok == 'pass' else []
        bad = []
        for c in after:
            r = subprocess.run([sys.executable, str(PLUGIN / 'scripts/check-commit.py'), '--message', c['body']], capture_output=True, text=True)
            if r.returncode:
                bad.append(c['subject'])
        self.check('a.commit_messages', A, 'fail' if bad else 'pass', f'{len(after)} post-plan-review commits; failing: {bad}')
        frozen = [fn for c in after for fn in c['files'] if fn.rsplit('/', 1)[-1] in FROZEN or re.search(r'/tasks/T\d+-[^.]+\.md$', fn)]
        frozen_status = self.check('a.frozen_untouched', A, 'fail' if frozen else 'pass', f'frozen files changed after plan-review: {sorted(set(frozen))}')
        reflog = [l for l in git(self.ws, 'reflog', '--format=%gs').splitlines() if re.match(r'commit \(amend\)|reset:', l)]
        pushes = [e['text'][:80] for e in self.events if e['kind'] == 'shell' and forbidden_git_command(e['text'])]
        forbidden = self.check('a.no_forbidden_git', A, 'fail' if reflog or pushes else 'pass', f'reflog: {reflog}; commands: {pushes}')

        impl = self.within('geeky-implement')
        shells = [e for e in impl if e['kind'] in ('shell', 'skill') or e['name'].startswith('mcp')]
        coder = [e for e in impl if e['kind'] == 'spawn' and 'coder' in (e['role'] + e['text'][:300]).lower()]
        reviewers = [e for e in impl if (e['kind'] == 'spawn' and 'review' in (e['role'] + e['text'][:300]).lower()) or e['kind'] == 'redispatch']
        for t in self.tasks:
            name = re.compile(rf'\b{t}\b')
            self.check(f'a.task_dispatch.{t}', A, 'pass' if any(name.search(e['text']) for e in coder) else 'fail', f'geeky-coder dispatch naming {t}')
            phase = next(p for p, ts in self.phases.items() if t in ts)
            self.check(f'a.task_review.{t}', A, 'pass' if any(name.search(e['text']) or re.search(rf'(?i)\bphase\s*{phase[1:]}\b', e['text']) for e in reviewers) else 'fail',
                       f'reviewer dispatch naming {t} or {phase}')
            dod = [e for e in shells if 'check-dod' in e['text'] and name.search(e['text'])]
            self.check(f'a.dod_gate.{t}', A, 'pass' if dod else 'fail', f'{len(dod)} check-dod runs naming {t} during implement')
        for p, ts in self.phases.items():
            named = [e for e in reviewers if re.search(rf'(?i)\bphase\s*{p[1:]}\b', e['text']) or any(re.search(rf'\b{t}\b', e['text']) for t in ts)]
            recorded = bool(re.search(r'(?i)READY TO ADVANCE', handoff))
            self.check(f'a.phase_verdict.{p}', A, 'pass' if named and recorded else 'fail', f'{len(named)} reviewer dispatches; verdict recorded: {recorded}')
        kanban = sum(literal_kanban_run(e) for e in shells) + \
            kanban_helper_runs([e for e in self.events if e['kind'] in ('shell', 'edit')], [e for e in shells if e['kind'] == 'shell'])
        self.check('a.kanban_gate', A, 'pass' if kanban >= len(self.tasks) else 'fail', f'{kanban} validate-kanban runs for {len(self.tasks)} tasks')
        code_commits = self.implement_code_commits(after)
        cc = self.commit_gate_passes(shells)
        self.check('a.commit_gate', A, 'pass' if cc >= len(code_commits) else 'fail', f'{cc} check-commit passes for {len(code_commits)} code commits')
        tests = sum(bool(re.search(r'\bpytest\b', e['text'])) for e in impl if e['kind'] == 'shell')
        self.check('a.tests_run', A, 'pass' if tests else 'fail', f'{tests} pytest runs during implement')
        code = re.compile(r'(^|/)(wordstat|tests)/[^/]+\.py$')
        writes = [(e['name'], p) for e in impl + self.within('impl-review') if e['thread'] == 'main' for p in e['paths'] if code.search(p)]
        self.check('a.no_orchestrator_code_edits', A, 'fail' if writes else 'pass', f'main-thread code/test writes: {writes[:5]}')
        reason = self.run.get('exit_reason')
        self.check('a.run_completed', A, 'pass' if reason == 'completed' else 'fail', f'exit_reason {reason}')
        dirty = [l for l in git(self.ws, 'status', '--porcelain').splitlines() if not l.endswith('.heartbeat')]
        self.check('a.work_committed', A, 'fail' if dirty else 'pass', f'uncommitted at the end: {dirty[:5]}')
        for jid in ('a.one_concern_per_commit', 'a.final_report_claims'):
            self.check(jid, A, 'needs_judge', 'from --manual')

        def spawns(stage):
            return [e for e in self.within(stage) if e['kind'] == 'spawn']
        dps = [('dp01', len(spawns('spec-research')) >= 3, f"{len(spawns('spec-research'))} spec-research dispatches"),
               ('dp02', spec_ok == 'pass', 'see a.prescribed.spec'),
               ('dp03', (self.folder / 'review-development-project-manager.md').exists(), 'review-development-project-manager.md'),
               ('dp04', len(spawns('plan-review')) >= 2, f"{len(spawns('plan-review'))} plan-review dispatches"),
               ('dp05', pr_ok == 'pass', 'see a.prescribed.plan_review'),
               ('dp09', all(c['status'] == 'pass' for c in self.checks if c['id'].startswith('a.task_review.')), 'every task reviewed'),
               ('dp10', all(c['status'] == 'pass' for c in self.checks if c['id'].startswith('a.dod_gate.')), 'DoD gate for every task'),
               ('dp11', all(c['status'] == 'pass' for c in self.checks if c['id'].startswith('a.phase_verdict.')), 'phase verdict for every phase'),
               ('dp12', cc >= len(code_commits), 'see a.commit_gate'),
               ('dp15', summary == 'pass', 'see a.run_summary'),
               ('dp16', len(spawns('impl-review')) >= 3, f"{len(spawns('impl-review'))} impl-review dispatches"),
               ('dp18', heartbeat == 'pass', 'see a.heartbeat_final')]
        for dp, ok, ev in dps:
            self.check(dp, C, 'pass' if ok else 'fail', ev)
        for jid in ('dp07', 'dp13'):
            self.check(jid, C, 'needs_judge', 'from --manual')
        order = [self.bounds[s][0] for s in ('geeky-plan', 'plan-review', 'geeky-implement', 'impl-review')]
        oc = 1.0 if all(order) and order == sorted(order) else 0.0

        def ratio(dim, weight=lambda c: 1):
            rows = [c for c in self.checks if c['dimension'] == dim and c['status'] in ('pass', 'fail')]
            return sum(weight(c) for c in rows if c['status'] == 'pass') / max(1, sum(weight(c) for c in rows)), rows
        a_ratio, a_rows = ratio(A)
        adherence = 100 * a_ratio
        if forbidden == 'fail':
            adherence = 0.0
        elif frozen_status == 'fail':
            adherence = min(adherence, 30.0)
        if self.manual.get('hg.false_completion_claim', {}).get('pass') is False:
            adherence = min(adherence, 50.0)
        dpc, c_rows = ratio(C, lambda c: 2 if c['id'] in CRITICAL else 1)
        consistency = 100 * (0.8 * dpc + 0.2 * oc)
        quality = self.score['dimensions']['quality']['capped']
        composite = (0.35 * quality + 0.35 * adherence + 0.20 * consistency) / 0.90
        result = {'schema': 'geeky-eval.parity/1', 'evaluator': EVALUATOR, 'harness': self.run.get('harness') or 'claude-code',
                'run': self.run.get('run_id') or Path(self.run['workspace']).name,
                'session_id': (self.run.get('telemetry') or {}).get('session_id') or self.run.get('session_id'),
                'composite': round(composite, 2), 'quality': round(quality, 2),
                'adherence': round(adherence, 2), 'adherence_uncapped': round(100 * a_ratio, 2),
                'adherence_fraction': f"{sum(c['status'] == 'pass' for c in a_rows)}/{len(a_rows)}",
                'consistency': round(consistency, 2), 'dpc': round(dpc, 4), 'order': oc,
                'efficiency': {'tokens_total': self.run.get('tokens', {}).get('total'), 'wall_time_s': self.run.get('wall_time_s'),
                               'latency_median_ms': self.latency, 'commits': len(self.commits) - 1,
                               'interventions': self.run.get('interventions', 0)},
                'stage_bounds': self.bounds, 'phases': self.phases,
                'needs_judge': [c['id'] for c in self.checks if c['status'] == 'needs_judge'], 'checks': self.checks}
        if self.scope:
            result['evaluation_scope'] = self.scope
            result['efficiency'] = scoped_efficiency(result['efficiency'], self.scope)
            result['prompt_sha256'] = self.run.get('prompt_sha256')
            result['plugin_sha'] = self.run.get('plugin_sha')
        return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_dir', type=Path)
    ap.add_argument('--observations', type=Path, required=True)
    ap.add_argument('--score', type=Path, required=True, help="score-run.py's score.json (for Quality)")
    ap.add_argument('--manual', type=Path)
    ap.add_argument('--scope', type=Path, help='Validated evaluation-inputs.json; required for new publication')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args(argv)
    inputs = [a.run_dir / 'run.json', a.observations, a.score] + ([a.manual] if a.manual else [])
    scope = load_scope(a.scope, inputs) if a.scope else None
    if scope and scope['missing_sources']:
        raise ValueError(f"Scope completeness unknown; missing sources: {scope['missing_sources']}")
    obs = json.loads(a.observations.read_text(encoding='utf-8'))
    obs = obs if isinstance(obs, list) else obs.get('observations') or obs.get('data')
    manual = json.loads(a.manual.read_text(encoding='utf-8')) if a.manual else {}
    result = Evaluation(a.run_dir, obs, json.loads(a.score.read_text(encoding='utf-8')), manual, scope).evaluate()
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'parity.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('harness', 'composite', 'quality', 'adherence', 'consistency', 'needs_judge')}))


if __name__ == '__main__':
    sys.exit(main())
