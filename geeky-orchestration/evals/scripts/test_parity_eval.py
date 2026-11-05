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


def test_run_summary_heading_is_a_markdown_heading():
    assert pe.RUN_SUMMARY.search('## Overall Run Summary') and pe.RUN_SUMMARY.search('###### Session Run Summary')
    assert not pe.RUN_SUMMARY.search('####### Run Summary') and not pe.RUN_SUMMARY.search('##\nRun Summary')
