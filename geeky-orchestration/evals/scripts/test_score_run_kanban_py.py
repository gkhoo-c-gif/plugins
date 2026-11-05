"""score-run recognises kanban lane moves written by inline Python (gpt-6.1-sol, C2 run 20261101T211028Z)."""
import importlib.util
from pathlib import Path


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


score = module('score-run')

# Shapes copied from the C2 GPT transcript (session.jsonl Bash calls), shortened.
IN_PROGRESS = """python3 - <<'PY'
from pathlib import Path
p=Path('docs/spec-001-feature'); b=p/'kanban.md'; s=b.read_text()
line='- [ ] [T1 \u2014 Core storage](tasks/T1-core-storage.md)'
s=s.replace(line+'\\n','').replace('## In Progress\\n','## In Progress\\n\\n'+line+' <!-- in_progress: 2026-11-01 21:27 -->\\n')
b.write_text(s)
PY
python3 plugin/scripts/validate-kanban.py --path docs/spec-001-feature"""

DONE = """python3 - <<'PY'
from pathlib import Path
p=Path('docs/spec-001-feature')
b=p/'kanban.md';s=b.read_text();line='- [ ] [T1 \u2014 Core storage](tasks/T1-core-storage.md)';s=s.replace(line+'\\n','').replace('## Done\\n','## Done\\n\\n'+line.replace('[ ]','[x]',1)+'\\n');b.write_text(s)
PY"""

BLOCKED = """python3 - <<'PY'
b=p/'kanban.md';s=b.read_text();line='- [ ] [T2 \u2014 HTTP server](tasks/T2-http-server.md)';s=s.replace(line+'\\n','').replace('## Blocked\\n','## Blocked\\n\\n'+line+' <!-- blocked -->\\n');b.write_text(s)
PY"""

READ_ONLY = """python3 - <<'PY'
from pathlib import Path
b=Path('docs/spec-001-feature')/'kanban.md'
print(b.read_text().count('[x]'))
PY"""


def test_inline_python_write_is_a_kanban_write():
    assert score.py_writes_kanban(IN_PROGRESS)
    assert score.py_writes_kanban(DONE)
    assert score.py_writes_kanban("python3 -c \"open('docs/f/kanban.md','w').write(s)\"")


def test_reading_kanban_is_not_a_lane_move():
    assert not score.py_writes_kanban(READ_ONLY)
    assert score.py_kanban_moves(READ_ONLY) == []


def test_lane_targets_are_paired_with_the_task_line():
    assert score.py_kanban_moves(IN_PROGRESS) == [('T1', 'In Progress')]
    assert score.py_kanban_moves(DONE) == [('T1', 'Done')]
    assert score.py_kanban_moves(BLOCKED) == [('T2', 'Blocked')]


LOOP = """python3 - <<'PY'
p=Path('docs/spec-001-feature');h=p/'handoff.md';h.write_text(h.read_text()+'\\nRESUME: phase2 - next eligible T2, T3.\\n')
b=p/'kanban.md';s=b.read_text()
for line in ['- [ ] [T2 — HTTP server](tasks/T2-http-server.md)','- [ ] [T3 — CLI client](tasks/T3-cli-client.md)']:
 s=s.replace(line+'\\n','').replace('## Ready\\n','## Ready\\n\\n'+line+'\\n')
b.write_text(s)
PY"""


def test_loop_over_task_lines():
    assert score.py_kanban_moves(LOOP) == [('T2', 'Ready'), ('T3', 'Ready')]


def test_inline_python_handoff_write():
    assert score.py_writes(LOOP, 'handoff.md')
    assert not score.py_writes(READ_ONLY, 'handoff.md')


def test_two_moves_in_one_snippet():
    both = IN_PROGRESS.replace("b.write_text(s)", "line='- [ ] [T4 \u2014 Acceptance](tasks/T4.md)'\n"
                               "s=s.replace(line+'\\n','').replace('## Ready\\n','## Ready\\n\\n'+line+'\\n')\nb.write_text(s)", 1)
    assert score.py_kanban_moves(both) == [('T1', 'In Progress'), ('T4', 'Ready')]


def _forbidden(cmds):
    class C:  # minimal trace call
        def __init__(self, i, cmd):
            self.idx, self.name, self.cmd = i, "Bash", cmd
    s = score.Scorer.__new__(score.Scorer)
    s.trace = type("T", (), {"calls": [C(i, c) for i, c in enumerate(cmds)]})()
    s.ws_source = "none"
    return s.forbidden_git()


def test_git_stash_push_is_not_a_push():
    # C2 20261101T192141Z call 344: a local stash for a test-before-fix check
    assert _forbidden(["git stash push linkvault/server.py -q && python3 -m pytest -q; git stash pop -q"]) == []
    assert _forbidden(["git stash push --keep-index -m wip"]) == []


def test_real_pushes_and_rewrites_are_still_forbidden():
    for cmd in ["git push origin main", "git -C /repo push", "git -c core.x=1 push --tags", "git push -f origin x",
                "git commit --amend -m x", "git commit --no-verify -m x", "git reset --hard && git rebase --force x"]:
        assert _forbidden([cmd]), cmd


def test_bash_for_loop_over_a_kanban_helper():
    # C2 20261101T225826Z (claude-sonnet-5-5 medium): helper written with cat > $TMPDIR/mv.py, Done moves in a bash loop
    raw = ('R=plugin/scripts; F=docs/spec-001-feature\n'
           'for t in T1 T2; do python3 $TMPDIR/mv.py $t Done; python3 $R/check-dod.py --path $F --task $t | tail -3; done')
    assert sorted(score.helper_moves(raw, {"mv.py"})) == [('T1', 'Done'), ('T2', 'Done')]
    assert score.helper_moves('python3 $TMPDIR/mv.py T3 "In Review"', {"mv.py"}) == [('T3', 'In Review')]


def test_check_dod_exit_code_evidence():
    # C2 20261101T225826Z: unpiped check-dod with its own exit code echoed counts; a piped one does not
    loop = 'for t in T3 T4; do python3 $R/check-dod.py --path $F --task $t >/dev/null; echo dod$t=$?; done'
    assert score.dod_exit_verdicts(loop, "dodT3=0\ndodT4=1\n") == {'T3': True, 'T4': False}
    lit = 'python3 $R/check-dod.py --path $F --task T1 >/dev/null; echo dod1=$?'
    assert score.dod_exit_verdicts(lit, "dod1=0") == {'T1': True}
    piped = 'for t in T1 T2; do python3 $R/check-dod.py --path $F --task $t | tail -3; echo dod=$?; done'
    assert score.dod_exit_verdicts(piped, "  Re-run ...\ndod=0\ndod=0") == {}


def test_python_subprocess_gate_runs_are_counted():
    # C2 20261102T053248Z (gpt-6.1-sol high via the bridge): gates run from inline Python as argument lists
    ar = module('analyze-run')
    cmd = ("python - <<'PY'\nimport subprocess\nroot='/p/scripts/'\n"
           "for script,args in [('check-dod.py',['--task','T1']),('validate-kanban.py',[])]:\n"
           " r=subprocess.run(['python',root+script,'--path','docs/f']+args)\nPY")
    runs = ar.static_gate_runs(cmd)
    assert runs['check-dod'] == 1 and runs['validate-kanban'] == 1
    outs = ar.gate_outcomes('Bash', {'command': cmd}, ("DOD_OK: T1\nOK: 4 task(s) tracked, 4 task file(s).", False))
    assert ('check-dod', True) in outs and ('validate-kanban', True) in outs
    # reading or printing a script name without running anything is not a gate run
    assert ar.static_gate_runs("python - <<'PY'\nprint('check-dod.py')\nPY")['check-dod'] == 0


def test_path_open_append_is_a_write():
    # C2 20261102T053248Z: `h=p/'handoff.md'; with h.open('a') as f: f.write(...)`
    raw = "p=Path('docs/f')\nh=p/'handoff.md'\nwith h.open('a') as f:\n f.write('T1 Done')\n"
    assert score.py_writes(raw, 'handoff.md')
    assert not score.py_writes("h=p/'handoff.md'\nwith h.open() as f:\n print(f.read())\n", 'handoff.md')


def test_fix_subject_accepts_the_skill_wording():
    # skills/impl-review/SKILL.md prescribes "required"; older versions said "critical"
    assert score.FIX_SUBJECT.match("fix(spec): Address 1 required review findings from impl-review")
    assert score.FIX_SUBJECT.match("fix(spec): Address 2 critical review findings from impl-review")
    assert not score.FIX_SUBJECT.match("fix: address findings")


def test_run_summary_heading_accepts_qualified_headings():
    import re
    for h in ("## Run Summary", "## Implementation Run Summary", "## Final Run Summary", "### run summary"):
        assert re.search(score.RUN_SUMMARY, "x\n%s\n" % h, re.M | re.I), h
    for h in ("## Summary", "Run Summary without heading", "## Running summary"):
        assert not re.search(score.RUN_SUMMARY, "x\n%s\n" % h, re.M | re.I), h


def test_run_summary_heading_stays_on_one_line():
    import re
    for text in ("## Next steps\n\nRun summary: to be written\n", "## Status\nRun Summary pending\n",
                 "## No Run Summary yet\n", "## Draft - Run Summary\n"):
        assert not re.search(score.RUN_SUMMARY, text, re.M | re.I), text
