import importlib.util
import json
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pe = module(HERE / 'parity-eval.py', 'parity_eval')
ls = module(HERE.parent / 'telemetry/langfuse-scores.py', 'langfuse_scores')


def obs(oid, name, inp, parent=None, t='2026-10-25T03:00:00Z', kind='TOOL'):
    return {'id': oid, 'type': kind, 'name': name, 'input': json.dumps(inp) if not isinstance(inp, str) else inp,
            'output': '', 'parentObservationId': parent, 'startTime': t}


def test_events_normalise_both_tracers():
    o = [obs('s1', 'Subagent: coder', '', kind='SPAN'),
         obs('a', 'Tool: Agent', {'subagent_type': 'geeky-orchestration:geeky-coder', 'prompt': 'Implement T1'}),
         obs('b', 'Tool: Write', {'file_path': '/ws/wordstat/cli.py', 'content': 'x'}, parent='s1'),
         obs('c', 'spawn_agent', {'agent_type': 'code-reviewer', 'message': 'Review phase 1'}),
         obs('d', 'exec_command', {'cmd': "cat > tests/test_cli.py <<'EOF'\nx\nEOF"}),
         obs('e', 'skill:plan-review', {'cmd': 'cat SKILL.md'})]
    ev = {e['name']: e for e in pe.events(o)}
    assert ev['Tool: Agent']['kind'] == 'spawn' and 'geeky-coder' in ev['Tool: Agent']['role']
    assert ev['spawn_agent']['role'] == 'code-reviewer'
    assert ev['Tool: Write']['thread'] == 'sub' and ev['Tool: Write']['paths'] == ['/ws/wordstat/cli.py']
    assert ev['exec_command']['thread'] == 'main' and 'tests/test_cli.py' in ev['exec_command']['paths']
    assert ev['skill:plan-review']['kind'] == 'skill'


def test_final_review_marker_matches_both_harnesses():
    assert pe.REVIEW_FINAL.search('{"task_name":"impl_cli_review","agent_type":"code-reviewer"}')
    assert pe.REVIEW_FINAL.search('{"description": "Impl review: CLI core"}')
    assert pe.REVIEW_FINAL.search("a CLI expert reviewing wordstat's implementation")
    assert not pe.REVIEW_FINAL.search('{"description": "Phase 1 review of T1"}')


def test_stage_time_comparison_normalizes_git_and_observation_offsets():
    assert pe.utc_time('2026-10-26T00:19:16+08:00') == pe.utc_time('2026-10-25T16:19:16Z')
    evaluation = pe.Evaluation.__new__(pe.Evaluation)
    evaluation.bounds = {'geeky-implement': (pe.utc_time('2026-10-26T00:19:16+08:00'),
                                          pe.utc_time('2026-10-26T00:34:00+08:00'))}
    evaluation.events = [{'t': '2026-10-25T16:25:00.123Z', 'kind': 'spawn'}]
    assert evaluation.within('geeky-implement') == evaluation.events


def test_batched_commit_helper_counts_each_recorded_gate_pass():
    event = {'text': 'function C { python check-commit.py --message $msg }; C a; C b; C c',
             'output': 'OK: commit message valid.\r\nOK: commit message valid.\r\nOK: commit message valid.\r\n'}
    assert pe.commit_gate_runs(event) == 3
    assert pe.commit_gate_runs({'text': 'python check-commit.py --message x', 'output': ''}) == 1
    assert pe.commit_gate_runs({'text': 'Get-Content x', 'output': event['output']}) == 0


def test_stage_boundaries_use_explicit_review_dispatch_and_skill():
    evaluation = pe.Evaluation.__new__(pe.Evaluation)
    times = [pe.utc_time(f'2026-10-25T03:{minute}:00Z') for minute in ('00', '05', '10', '20', '30')]
    evaluation.moves = [
        {'subject': 'docs: author feature specification in docs/spec', 'time': times[1], 'files': []},
        {'subject': pe.PLAN_REVIEW_SUBJECT, 'time': times[3], 'files': ['implementation-plan.md']}]
    evaluation.events = [
        {'t': times[0], 'kind': 'shell', 'name': 'Tool: PowerShell', 'text': 'read'},
        {'t': times[2], 'kind': 'spawn', 'name': 'Tool: Agent', 'text': 'plan-review Phase 1 document audit'},
        {'t': times[4], 'kind': 'skill', 'name': 'Tool: Skill', 'text': 'geeky-orchestration:impl-review'}]
    bounds = evaluation.stage_bounds()
    assert bounds['plan-review'] == (times[2], times[3])
    assert bounds['impl-review'][0] == times[4]


def test_deferred_impl_review_mention_does_not_start_final_review():
    evaluation = pe.Evaluation.__new__(pe.Evaluation)
    times = [pe.utc_time(f'2026-10-25T03:{minute}:00Z') for minute in ('00', '05', '10', '20', '30', '40', '50')]
    evaluation.moves = [
        {'subject': 'docs: author feature specification in docs/spec', 'time': times[1], 'files': []},
        {'subject': pe.PLAN_REVIEW_SUBJECT, 'time': times[2], 'files': ['implementation-plan.md']}]
    evaluation.events = [
        {'t': times[0], 'kind': 'shell', 'name': 'Tool: PowerShell', 'text': 'read'},
        {'t': times[3], 'kind': 'spawn', 'name': 'Tool: Agent', 'role': 'geeky-orchestration:geeky-implement-orchestrator',
         'text': 'Execute the implementation. Do not run impl-review here; the parent launches it next.'},
        {'t': times[4], 'kind': 'spawn', 'name': 'spawn_agent', 'role': '',
         'text': 'Implement the remaining tasks and commit before impl-review starts.'},
        {'t': times[5], 'kind': 'spawn', 'name': 'Tool: Agent', 'role': 'geeky-orchestration:code-reviewer',
         'text': '{"description": "Impl review: CLI core", "prompt": "Do not edit files."}'}]
    bounds = evaluation.stage_bounds()
    assert bounds['geeky-implement'] == (times[2], times[5])
    assert bounds['impl-review'][0] == times[5]


def test_final_review_dispatch_rules():
    spawn = {'kind': 'spawn', 'role': 'code-reviewer'}
    assert pe.final_review_dispatch({**spawn, 'text': 'a CLI expert reviewing wordstat\'s implementation'})
    assert not pe.final_review_dispatch({**spawn, 'text': "Don't start the impl-review yet"})
    assert not pe.final_review_dispatch({**spawn, 'text': 'Phase 1 review; implementation review follows'})
    assert not pe.final_review_dispatch({'kind': 'spawn', 'role': 'geeky-coder', 'text': 'Fix impl-review finding F1'})
    # One affirmative mention is enough even when another is deferred.
    assert pe.final_review_dispatch({**spawn, 'text': 'Not a phase gate. Impl review: run the final review now.'})


def test_named_actor_trace_does_not_turn_coder_edits_into_orchestrator_edits():
    root = obs('actor', 'codex-mxc-win-coder-worker', '', kind='AGENT')
    root['metadata'] = {'actor_scope': 'subagent', 'agent_role': 'coder'}
    edit = obs('edit', 'apply_patch', '*** Begin Patch\n*** Add File: app.py\n+x\n*** End Patch', parent='actor')
    assert pe.events([root, edit])[0]['thread'] == 'sub'


def test_scores_map_parity_results():
    parity = {'harness': 'codex', 'composite': 98.75, 'quality': 96.8, 'adherence': 0.0, 'adherence_uncapped': 96.97,
              'adherence_fraction': '32/33', 'consistency': 100.0,
              'checks': [{'id': 'a.no_forbidden_git', 'status': 'fail', 'evidence': 'reflog: reset'},
                         {'id': 'a.final_report_claims', 'status': 'pass', 'evidence': 'judge: ok'}]}
    run = {'oracle': {'passed': 13, 'collected': 13}, 'telemetry': {'coverage_complete': True}, 'interventions': 1}
    values, comments = ls.scores(run, parity)
    assert values['oracle_pass_rate'] == 1.0 and values['forbidden_git_operation'] is True
    assert values['final_report_claims_verified'] is True and values['provider_refusals'] == 1
    assert set(values) <= set(ls.CONFIGS) and comments['process_adherence'] == '32/33 checks passed'


def test_score_publication_uses_named_session_and_retains_native_id(tmp_path, capsys):
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    (run_dir / 'run.json').write_text(json.dumps({
        'session_id': 'native-thread', 'telemetry': {
            'session_id': 'codex-mxc-win-c0-wordstat-stamp', 'environment': 'mxc-win',
            'agent_family': 'codex-mxc-win', 'coverage_complete': False}}))
    parity = tmp_path / 'parity.json'
    parity.write_text(json.dumps({
        'harness': 'codex', 'composite': 100, 'quality': 100, 'adherence': 100,
        'adherence_uncapped': 100, 'adherence_fraction': '1/1', 'consistency': 100,
        'checks': [{'id': 'a.no_forbidden_git', 'status': 'pass', 'evidence': 'fixture'}]}))
    coverage = tmp_path / 'coverage.json'
    coverage.write_text(json.dumps({'session_id': 'codex-mxc-win-c0-wordstat-stamp', 'complete': True}))
    scope = tmp_path / 'evaluation-inputs.json'
    scope.write_text(json.dumps(scope_document([run_dir / 'run.json', parity, coverage])))
    assert ls.main(['--run', str(run_dir), '--parity', str(parity), '--coverage', str(coverage), '--scope', str(scope), '--dry-run']) == 0
    published = json.loads(capsys.readouterr().out)
    assert published['session'] == 'codex-mxc-win-c0-wordstat-stamp'
    assert published['metadata']['native_session_id'] == 'native-thread'
    assert published['metadata']['environment'] == 'mxc-win'
    assert published['scores']['harness_coverage_complete'] is True
    assert len(published['metadata']['coverage_report_sha256']) == 64
    assert published['metadata']['input_sha256'] == ls.sha256(scope)
    assert published['metadata']['parity_sha256'] == ls.sha256(parity)
    assert published['scores_verified'] is False and published['status'] == 'preview'
    assert json.loads((run_dir / 'run.json').read_text())['telemetry']['coverage_complete'] is False


def scope_document(paths):
    return {'schema': 'geeky-eval.inputs/1', 'scope_id': 'initial', 'kind': 'initial', 'parent_scope_id': None,
            'configuration': {'prompt_sha256': None, 'plugin_sha': None},
            'sources': [{'id': str(i), 'path': str(p), 'sha256': ls.sha256(p), 'ancestry': []} for i, p in enumerate(paths)],
            'segments': [{'segment_id': 'one', 'authorization_reference': 'fixture',
                          'start': '2026-10-25T03:00:00Z', 'end': '2026-10-25T04:00:00Z'}],
            'ownership': {'method': 'native', 'rule': 'single initial capture'},
            'metric_default': {'scope_id': 'initial', 'population': 'all captured agents',
                               'source_ids': ['0'], 'additive': False, 'complete': True}}


def test_scope_labels_block_mixed_cumulative_metrics(tmp_path):
    source = tmp_path / 'source'
    source.write_text('reviewed evidence')
    doc = scope_document([source])
    doc.update(scope_id='cumulative', kind='cumulative', parent_scope_id='initial')
    doc['ownership'] = {'method': 'source-reviewed', 'rule': 'UUID ownership from retained source review',
                        'evidence_source': '0', 'cutoff': '2026-10-25T03:00:00Z',
                        'assignments': [{'source_id': '0', 'logical_ids': ['previous:a', 'continuation:b']}]}
    doc['metric_default'].update(scope_id='cumulative')
    doc['metrics'] = {m: {'scope_id': 'continuation', 'population': 'continuation only', 'complete': False}
                      for m in ('tokens', 'backend', 'cost')}
    path = tmp_path / 'evaluation-inputs.json'
    path.write_text(json.dumps(doc))
    loaded = pe.load_scope(path, [source])
    from evaluation_scope import scoped_efficiency
    result = scoped_efficiency({'wall_time_s': 7180, 'commits': 12, 'tokens_total': 100, 'backend': 'x', 'cost': 1}, loaded)
    assert result == {'wall_time_s': 7180, 'commits': 12, 'tokens_total': None, 'backend': None, 'cost': None}
    assert loaded['metric_labels']['tokens']['population'] == 'continuation only'
    doc['metrics']['tokens']['additive'] = True
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='Unsupported addition'):
        pe.load_scope(path)
    doc['metrics']['tokens']['additive'] = False
    doc['ownership']['assignments'].append({'source_id': '0', 'logical_ids': ['previous:a']})
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='duplicate logical ownership'):
        pe.load_scope(path)


def test_scope_missing_and_changed_sources_never_establish_completeness(tmp_path):
    source = tmp_path / 'source'
    source.write_text('original')
    doc = scope_document([source])
    path = tmp_path / 'evaluation-inputs.json'
    path.write_text(json.dumps(doc))
    source.unlink()
    loaded = pe.load_scope(path)
    assert loaded['completeness'] == 'unknown' and loaded['missing_sources'] == ['0']
    assert loaded['metric_labels']['tokens']['complete'] is False
    source.write_text('changed')
    with pytest.raises(ValueError, match='hash mismatch'):
        pe.load_scope(path)


def test_duplicate_observation_identity_is_rejected():
    with pytest.raises(ValueError, match='duplicate observation IDs'):
        pe.events([obs('a', 'Tool: Read', ''), obs('a', 'Tool: Read', '')])


def test_scope_binds_future_prompt_plugin_and_execution_identity(tmp_path):
    from evaluation_scope import bind_run
    doc = scope_document([])
    run = {'prompt_sha256': 'a' * 64, 'plugin_sha': 'b' * 40,
           'execution': {'schema': 'geeky-eval.execution/1', 'segment_id': 'one',
                         'attempt_id': 'attempt', 'authorization_reference': 'fixture'}}
    doc['configuration'] = {key: run[key] for key in ('prompt_sha256', 'plugin_sha')}
    doc['segments'][0]['attempt_id'] = 'attempt'
    bind_run(doc, run)
    run['plugin_sha'] = 'c' * 40
    with pytest.raises(ValueError, match='configuration differs'):
        bind_run(doc, run)
    run['plugin_sha'] = 'b' * 40
    run['execution']['attempt_id'] = 'another-attempt'
    with pytest.raises(ValueError, match='segment/attempt/authorization'):
        bind_run(doc, run)


def test_forbidden_git_ignores_stash_push_but_catches_real_pushes():
    # C2 20261101T192141Z: JSON event text with escaped newlines and a local `git stash push`
    stash = '{"command": "mv server.fixed.tmp /tmp/x 2>&1 || echo fail; git stash push linkvault/server.py -q && pytest -q; git stash pop -q"}'
    assert not pe.forbidden_git_command(stash)
    assert not pe.forbidden_git_command('{"command": "git status\\necho push done"}')
    for cmd in ['git push origin main', 'git -C /r push', 'git commit --amend -m x', 'git commit --no-verify -m x', 'git push --force']:
        assert pe.forbidden_git_command('{"command": "%s"}' % cmd), cmd


def test_fix_subject_accepts_the_skill_wording():
    # skills/impl-review/SKILL.md:134 prescribes "required"; C3 Opus run 20261104T083537Z commit d270a2b
    assert pe.FIX_SUBJECT.match('fix(spec): Address 1 required review findings from impl-review')
    assert pe.FIX_SUBJECT.match('fix(spec): Address 2 critical review findings from impl-review')
    assert not pe.FIX_SUBJECT.match('fix(spec): Address findings')


def test_run_summary_heading_accepts_qualified_headings():
    # C3 GPT run 20261104T104418Z: handoff.md has "## Implementation Run Summary" and "## Final Run Summary"
    for h in ('## Run Summary', '## Implementation Run Summary', '## Final Run Summary'):
        assert pe.RUN_SUMMARY.search('x\n%s\n' % h), h
    for h in ('## Summary', 'Run Summary', '## Running summary'):
        assert not pe.RUN_SUMMARY.search('x\n%s\n' % h), h


def test_kanban_gate_counts_runs_inside_a_gating_lane_helper():
    # C3 Opus run 20261104T083537Z: lanes.py (bash heredoc) moves the task and runs validate-kanban itself
    helper = ("cat > $CLAUDE_CODE_TMPDIR/lanes.py <<'EOF'\nimport sys,subprocess\nF='docs/f/'\n"
              "# usage: lanes.py TASK LANE [comment]\nk=open(F+'kanban.md').read()\nopen(F+'kanban.md','w').write(k)\n"
              "subprocess.run(['python','/p/scripts/validate-kanban.py','--path',F])\nEOF\n"
              "L=$CLAUDE_CODE_TMPDIR/lanes.py\npython $L T5 Ready; python $L T5 \"In Progress\"")
    loop = 'L=$T/lanes.py; for t in T5 T6; do python $L $t Done "ok"; done'
    other = 'python lanes.py T7 "In Review"; echo T8 Done'
    shells = [{'text': helper}, {'text': loop}, {'text': other}]
    assert pe.kanban_helper_runs(shells, shells) == 2 + 2 + 1
    # a helper that does not run validate-kanban gives no credit
    plain = [{'text': helper.replace('validate-kanban', 'cat')}, {'text': loop}]
    assert pe.kanban_helper_runs(plain, plain) == 0


def test_commit_gate_counts_calls_of_a_bash_commit_function():
    # C3 Opus run 20261104T083537Z: commit() runs check-commit with its output discarded, then commits
    text = ("commit() { m=$1; shift\n  python $P/scripts/check-commit.py $m >/dev/null || { python $P/scripts/check-commit.py $m; return 1; }\n"
            "  git add \"$@\" && git commit -q -F $m; }\ncommit $T/c1.txt a.py || exit 1\ncommit $T/c2.txt b.py || exit 1\ncommit $T/c3.txt c.py")
    assert pe.commit_gate_runs({'text': text, 'output': ''}) == 3
    assert pe.commit_gate_runs({'text': 'python check-commit.py m.txt; git commit -F m.txt', 'output': ''}) == 1
    assert pe.commit_gate_runs({'text': 'git commit -m x', 'output': ''}) == 0


def test_review_cases_for_helper_and_commit_gate_counts():
    # scorer-fix review, 2026-11-05: comment-only gate mention, non-call mentions, keyword functions, readers, aborts
    src = ("cat > $T/lanes.py <<'EOF'\n# usage: python lanes.py T1 Done\nk=open('docs/f/kanban.md').read()\n"
           "# validate-kanban is run by the caller\nEOF\n")
    assert pe.kanban_helper_runs([{'text': src}], [{'text': src + 'L=$T/lanes.py; python $L T2 Done'}]) == 0
    gating = src.replace('# validate-kanban is run by the caller', "subprocess.run(['python','/p/scripts/validate-kanban.py'])")
    calls = [{'text': gating + 'L=$T/lanes.py; python $L T2 Done; cat $L T3 Done; echo "$L T4 Done"; python $L --dry-run T5 Done'}]
    assert pe.kanban_helper_runs(calls, calls) == 1
    kw = 'function commit { python $P/scripts/check-commit.py $m; git commit -F $m; }\ncommit a; commit b; commit c'
    assert pe.commit_gate_runs({'text': kw, 'output': ''}) == 3                 # agrees with analyze-run
    reader = 'show() { cat $P/scripts/check-commit.py; }\nshow; show'
    assert pe.commit_gate_runs({'text': reader, 'output': ''}) == 1             # the one-call fallback, not 2
    chain = ("commit() { python $P/scripts/check-commit.py $1 >/dev/null || { python $P/scripts/check-commit.py $1; return 1; }; "
             "git commit -F $1; }\ncommit c1.txt || exit 1\ncommit c2.txt || exit 1\ncommit c3.txt")
    assert pe.commit_gate_runs({'text': chain, 'output': 'INVALID_COMMIT_MESSAGE: subject exceeds 72 chars'}) == 0  # only a failure: no pass
    assert pe.commit_gate_runs({'text': chain, 'output': ''}) == 3


def test_review_round_two_scorers_agree_on_helper_calls():
    src = ("cat > $T/lanes.py <<'EOF'\nimport subprocess\nk=open('docs/f/kanban.md').read()\n"
           "subprocess.run(['python','/p/scripts/validate-kanban.py','--path','docs/f'])\nEOF\nL=$T/lanes.py\n")
    for call in ('python "$L" T1 Done', 'python ${L} T1 Done', 'python "$T/lanes.py" T1 Done', 'echo ok | python $L T1 Done'):
        assert pe.kanban_helper_runs([{'text': src}], [{'text': src + call}]) == 1, call
    loop = src + 'for t in T1 T2\ndo\n python $L $t Done\ndone\n'
    assert pe.kanban_helper_runs([{'text': loop}], [{'text': loop}]) == 2
    assert pe.RUN_SUMMARY.search('## Final Run Summary') and not pe.RUN_SUMMARY.search('## Status\nRun Summary pending')


def test_powershell_here_string_helper_counts_in_both_scorers():
    call = ("@'\nparam($Task,$To)\n$k='docs/f/kanban.md'; Set-Content $k (Get-Content $k)\npython /p/scripts/validate-kanban.py --path docs/f\n"
            "'@ | Set-Content $env:TEMP\\kmove.ps1\npwsh -File $env:TEMP\\kmove.ps1 -Task T2 -To Done")
    assert pe.AR.gating_helper_scripts(call) == {'kmove.ps1'}
    assert pe.kanban_helper_runs([{'text': call}], [{'text': call}]) == 1


def test_shell_readers_get_the_command_not_its_json_wrapper():
    # C3 Opus run 20261104T083537Z: in the observation text the command sits inside JSON quoting; a multi-line quote
    # masker would read the JSON value's own quotes as one string around the whole command
    cmd = ('commit() { m=$1; shift\n python $P/scripts/check-commit.py $m >/dev/null || { echo "GATE FAIL $m"; return 1; }\n'
           ' git commit -q -F $m; }\ncommit $T/c1.txt a.py || exit 1\ncommit $T/c2.txt b.py || exit 1')
    ev = pe.events([obs('o1', 'Tool: Bash', {'command': cmd})])[0]
    assert ev['cmd'] == cmd
    assert pe.commit_gate_runs({**ev, 'output': ''}) == 2


def test_codex_cmd_key_and_write_tool_helpers_match_score_run():
    cmd = ('commit() { m=$1; shift\n python $P/scripts/check-commit.py $m >/dev/null || { echo "GATE FAIL $m"; return 1; }\n'
           ' git commit -q -F $m; }\ncommit c1.txt a.py\ncommit c2.txt b.py')
    ev = pe.events([obs('d', 'exec_command', {'cmd': cmd})])[0]
    assert ev['cmd'] == cmd and pe.commit_gate_runs({**ev, 'output': ''}) == 2
    content = "F='docs/f/kanban.md'\nopen(F,'w').write(k)\nsubprocess.run(['python','/p/scripts/validate-kanban.py','--path','docs/f'])"
    py = [{'kind': 'edit', 'name': 'Tool: Write', 'text': content, 'paths': ['/tmp/t/lanes.py']}]
    assert pe.kanban_helper_runs(py, [{'text': 'python /tmp/t/lanes.py T1 Done'}]) == 0          # score-run registers no .py this way
    ps = [{'kind': 'edit', 'name': 'Tool: Write', 'text': content, 'paths': ['C:/t/kmove.ps1']}]
    assert pe.kanban_helper_runs(ps, [{'text': 'pwsh -File C:/t/kmove.ps1 -Task T1 -To Done'}]) == 1


def test_commit_gate_review_round_seven():
    # parity-path review, 2026-11-05
    kw = ('function commit { m=$1; shift\n python $P/scripts/check-commit.py $m >/dev/null || { python $P/scripts/check-commit.py $m; '
          'return 1; }\n git add "$@" && git commit -q -F $m; }\n')
    calls = 'commit c1 a || exit 1\ncommit c2 b || exit 1\ncommit c3 c || exit 1\ncommit c4 d || exit 1'
    assert pe.commit_gate_runs({'text': kw + calls, 'output': 'INVALID_COMMIT_MESSAGE: subject exceeds 72 chars'}) == 0
    assert pe.commit_gate_runs({'text': kw + calls, 'output': ''}) == 4
    # a shown failure after hidden passes: one pass per commit the call made
    out = 'abc1234 feat(x): add a\nINVALID_COMMIT_MESSAGE:\n  ERROR: subject exceeds 72 chars'
    assert pe.commit_gate_runs({'text': kw + calls, 'output': out}, commits=1) == 1
    assert pe.commit_gate_runs({'text': kw + calls, 'output': out}, commits=2) == 2
    assert pe.commit_gate_runs({'text': 'git commit -m pre\n' + kw + calls, 'output': out}, commits=2) == 0  # a literal commit: no evidence
    # a helper that prints the verdict: the shown passes are its runs (prefixed lines read as analyze-run reads them)
    loud = 'commit() { printf "%s: " "$1"; python $P/scripts/check-commit.py "$1" || return 1; git commit -q -F "$1"; }\n'
    shown = 'a: OK: commit message valid.\nb: OK: commit message valid.\nc: INVALID_COMMIT_MESSAGE: x'
    assert pe.commit_gate_runs({'text': loud + 'commit a || exit 1\ncommit b || exit 1\ncommit c', 'output': shown}) == 2
    chain = 'c(){ python3 $P/scripts/check-commit.py --message "$1" || return 1; git commit -q -m "$1"; }\nc a && c b && c c'
    assert pe.commit_gate_runs({'text': chain, 'output': 'INVALID_COMMIT_MESSAGE: x'}) == 0


def test_run_summary_heading_is_a_markdown_heading():
    assert pe.RUN_SUMMARY.search('## Overall Run Summary') and pe.RUN_SUMMARY.search('###### Session Run Summary')
    assert not pe.RUN_SUMMARY.search('####### Run Summary') and not pe.RUN_SUMMARY.search('##\nRun Summary')


def test_commit_gate_counts_implement_stage_commits_only():
    # parity-path review, 2026-11-05 (C2 20261102T053248Z): the impl-review fix commit 02cc3d0 was counted against
    # implement-stage gate runs
    evaluation = pe.Evaluation.__new__(pe.Evaluation)
    evaluation.bounds = {'geeky-implement': (pe.utc_time('2026-11-02T05:49:44Z'), pe.utc_time('2026-11-02T06:28:22.910Z'))}
    c = lambda t, files: {'time': pe.utc_time(t), 'files': files}
    after = [c('2026-11-02T06:28:10Z', ['linkvault/server.py']), c('2026-11-02T06:28:15Z', ['docs/f/handoff.md']),
             c('2026-11-02T06:47:07Z', ['linkvault/domain.py'])]
    assert evaluation.implement_code_commits(after) == after[:1]
    evaluation.bounds = {'geeky-implement': (pe.utc_time('2026-11-02T05:49:44Z'), '9999')}   # impl-review never started
    assert evaluation.implement_code_commits(after) == [after[0], after[2]]


def test_commit_gate_review_round_eight():
    # round-7 review: a shown failure of a quiet helper, whatever stopped the command (or nothing did)
    q = ('c() { python3 /p/scripts/check-commit.py --message "$1" >/dev/null || { python3 /p/scripts/check-commit.py --message "$1"; '
         '%s 1; }; git commit -q -m "$1"; }\n')
    fail = 'INVALID_COMMIT_MESSAGE: subject exceeds 72 chars'
    for cmd in (q % 'exit' + 'c a\nc b\nc c', q % 'return' + 'set -x -e\nc a\nc b', q % 'return' + 'if ! c a; then exit 1; fi\nc b',
                'function Commit-Checked($m) { python /p/scripts/check-commit.py $m *> $null; if ($LASTEXITCODE) { python '
                '/p/scripts/check-commit.py $m; throw "gate" }; git commit -q -F $m }\nCommit-Checked a\nCommit-Checked b'):
        assert pe.commit_gate_runs({'text': cmd, 'output': fail}, commits=0) == 0, cmd       # nothing committed: no pass
    # no stop: the first call fails visibly, the other two pass silently and commit
    assert pe.commit_gate_runs({'text': q % 'return' + 'c a\nc b\nc c', 'output': fail}, commits=2) == 2


def test_round_seven_parity_low_findings():
    # a shown failure with no pass credits nothing; a loud helper is counted by its shown passes
    loud = 'c() { python3 /p/scripts/check-commit.py --message "$1" || return 1; git commit -q -m "$1"; }\nc a\nc b\nc c'
    assert pe.commit_gate_runs({'text': loud, 'output': 'INVALID_COMMIT_MESSAGE: x'}) == 0
    assert pe.commit_gate_runs({'text': loud, 'output': 'OK: commit message valid.\nINVALID_COMMIT_MESSAGE: x\n'
                                                        'OK: commit message valid.'}) == 2
    assert pe.commit_gate_runs({'text': 'python check-commit.py --message x', 'output': ''}) == 1   # hidden: the fallback
    # literal kanban runs: a helper's own source or a note is not a run
    assert pe.literal_kanban_run({'text': 'python /p/scripts/validate-kanban.py --path f', 'cmd': 'python /p/scripts/validate-kanban.py --path f'})
    note = "cat >> docs/f/review-plan.md <<'EOF'\nvalidate-kanban OK\nEOF\ngit commit -q -m x"
    assert not pe.literal_kanban_run({'text': note, 'cmd': note})
    here = "@'\npython /p/scripts/validate-kanban.py\n'@ | Set-Content k.ps1"
    assert not pe.literal_kanban_run({'text': here, 'cmd': here})


def test_commit_gate_passes_over_calls():
    evaluation = pe.Evaluation.__new__(pe.Evaluation)
    t = lambda s: pe.utc_time(s)
    evaluation.commits = [{'time': t('2026-11-04T09:30:01Z'), 'subject': 'feat: a'}, {'time': t('2026-11-04T09:30:02Z'), 'subject': 'feat: b'},
                          {'time': t('2026-11-04T08:00:00Z'), 'subject': pe.PLAN_REVIEW_SUBJECT}]
    q = ('c() { python3 /p/scripts/check-commit.py --message "$1" >/dev/null || { python3 /p/scripts/check-commit.py --message "$1"; '
         'return 1; }; git commit -q -m "$1"; }\nc a || exit 1\nc b || exit 1\nc c || exit 1')
    call = {'t': t('2026-11-04T09:30:00.500Z'), 'end': t('2026-11-04T09:30:03Z'), 'text': q, 'cmd': q, 'output': 'INVALID_COMMIT_MESSAGE: x'}
    assert evaluation.commit_gate_passes([call]) == 2                                   # a and b committed, c failed
    assert evaluation.commit_gate_passes([{**call, 'end': ''}]) == 0                     # no end time: no commit evidence
    pr = {'t': t('2026-11-04T08:00:00.200Z'), 'end': t('2026-11-04T08:00:01Z'), 'text': 'python check-commit.py --message m',
          'cmd': 'python check-commit.py --message m', 'output': 'OK: commit message valid.'}
    assert evaluation.commit_gate_passes([pr]) == 0                                     # the plan-review commit's own gate
    ev = pe.events([{**obs('o1', 'Tool: Bash', {'command': 'ls'}), 'endTime': '2026-10-25T03:00:05Z'}])[0]
    assert ev['end'] == pe.utc_time('2026-10-25T03:00:05Z')


def test_commit_gate_review_round_nine():
    # round-8 review: a shown failure is a run that did not pass, and a commit made without the gate is no pass
    c1 = ('function C($msg,$files){ $m=$msg; $r=$m | python /p/scripts/check-commit.py; "gate=$LASTEXITCODE $r"; '
          'git add -- $files 2>$null; git commit -q -m $m 2>&1 | Select -First 2 }\nC m1 a\nC m2 b\nC m3 c')   # stored C1 csvledger 20261020T155628Z
    out = 'gate=1 INVALID_COMMIT_MESSAGE: subject exceeds 72 chars (75)\ngate=0 OK: commit message valid.\ngate=0 OK: commit message valid.'
    assert pe.commit_gate_runs({'text': c1, 'output': out}, commits=3) == 2
    warn = 'cc(){ python /p/scripts/check-commit.py $1 >/dev/null || python /p/scripts/check-commit.py $1; git commit -qF $1; }\n'
    assert pe.commit_gate_runs({'text': warn + 'cc c1', 'output': 'INVALID_COMMIT_MESSAGE: x'}, commits=1) == 0
    assert pe.commit_gate_runs({'text': warn + 'cc c1\ncc c2\ncc c3', 'output': 'INVALID_COMMIT_MESSAGE: x'}, commits=3) == 2
    gated = ('c() { python3 /p/scripts/check-commit.py --message "$1" >/dev/null || { python3 /p/scripts/check-commit.py --message "$1"; '
             'return 1; }; git commit -q -m "$1"; }\n')
    wip = 'wip() { git add docs && git commit -q -m "docs(tracking): x"; }\n'
    assert not pe.AR.commits_only_in_helpers(gated + wip + 'c a\nwip')
    assert pe.commit_gate_runs({'text': gated + wip + 'c a\nwip', 'output': 'INVALID_COMMIT_MESSAGE: x'}, commits=1) == 0
    tail = 'c() { python3 /p/scripts/check-commit.py --message "$1" | tail -1 && git commit -q -m "$1"; }\nc a\nc b\nc c'
    shown = 'OK: commit message valid.\nINVALID_COMMIT_MESSAGE: x\nOK: commit message valid.'
    assert pe.commit_gate_runs({'text': tail, 'output': shown}, commits=3) == 2


def test_a_commit_is_made_by_one_call():
    # round-8 review: back-to-back calls in one second; the commit belongs to the first call naming commit
    evaluation = pe.Evaluation.__new__(pe.Evaluation)
    t = pe.utc_time
    evaluation.commits = [{'time': t('2026-11-04T10:00:05Z'), 'subject': 'feat: a'}]
    a = {'t': t('2026-11-04T10:00:04.100Z'), 'end': t('2026-11-04T10:00:05.300Z'), 'cmd': 'python3 /p/scripts/check-commit.py --message "$m" && git commit -q -m "$m"',
         'output': 'OK: commit message valid.'}
    a['text'] = a['cmd']
    q = ('c() { python3 /p/scripts/check-commit.py --message "$1" >/dev/null || { python3 /p/scripts/check-commit.py --message "$1"; '
         'return 1; }; git commit -q -m "$1"; }\nc a || exit 1\nc b || exit 1')
    b = {'t': t('2026-11-04T10:00:05.600Z'), 'end': t('2026-11-04T10:00:06.100Z'), 'cmd': q, 'text': q, 'output': 'INVALID_COMMIT_MESSAGE: x'}
    assert evaluation.commit_gate_passes([a, b]) == 1
