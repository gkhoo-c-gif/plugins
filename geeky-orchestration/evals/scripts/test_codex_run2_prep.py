"""Bounded model-free regressions for run-two trace and validation preparation."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path

import pytest


def module(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


BASE = module('test_codex_trace.py', 'codex_run2_fixtures')
CT, SR = BASE.CT, BASE.SR
trace_folder = BASE.trace_folder


def scorer_with_task(tmp_path, body, harness='codex'):
    task = tmp_path / 'T1-fixture.md'
    task.write_text(body, encoding='utf-8')
    scorer = SR.Scorer.__new__(SR.Scorer)
    scorer.run = {'harness': harness}
    scorer.task_files = {'T1': task}
    return scorer


def test_plain_bullets_and_fenced_commands_preserve_contract_order(tmp_path):
    body = ('Tests/Validation Before Next Task:\n'
            '- python -m pytest tests/test_core.py -q\n'
            '- `python -m csvledger --help` produces help.\n'
            '```powershell\npython -m pytest -q\n```\n'
            'Estimate:\nM\n- python must_not_run.py\n')
    scorer = scorer_with_task(tmp_path, body)
    assert scorer.validation_commands('T1') == [
        'python -m pytest tests/test_core.py -q', 'python -m csvledger --help', 'python -m pytest -q']
    scorer.run['harness'] = 'claude-code'
    assert scorer.validation_commands('T1') == ['python -m csvledger --help']


def test_prose_spans_do_not_become_plain_commands(tmp_path):
    scorer = scorer_with_task(tmp_path, 'Tests/Validation:\n- Check that `python -m pytest` passes.\n- Review the logs.\n')
    assert scorer.validation_commands('T1') == []


def test_empty_codex_validation_is_explicit_error_not_zero_of_zero(tmp_path):
    scorer = scorer_with_task(tmp_path, 'Tests/Validation:\n- Review the logs.\n')
    scorer.rerun, scorer.tasks = True, ['T1']
    scorer.rerun_commit = lambda task: ('fixture-sha', False, 'fixture')
    with pytest.raises(ValueError, match='refusing a vacuous 0/0'):
        scorer.rerun_blocks()


@pytest.mark.parametrize('harness', ['claude-code', 'codex'])
def test_versioned_python_commands_are_validation_commands_but_prose_words_are_not(tmp_path, harness):
    body = ('Tests/Validation Before Next Task:\n'
            '- `python3 -m pytest tests -q`\n'
            '- `python3.11 -m wordstat --help` (exit 0)\n'
            '- `python -m pytest -q`\n'
            '- `py -3 -m pytest -q`\n'
            '- `pythonic --help`\n'
            '- `python3x -V`\n'
            'Estimate:\nM\n')
    scorer = scorer_with_task(tmp_path, body, harness)
    assert scorer.validation_commands('T1') == [
        'python3 -m pytest tests -q', 'python3.11 -m wordstat --help', 'python -m pytest -q', 'py -3 -m pytest -q']


def test_native_lifetimes_flow_from_rollout_to_trace_not_ack(trace_folder):
    folder, rows = trace_folder
    BASE.add_child(folder, rows)
    child_path = folder / 'subagents/rollout-child.jsonl'
    child = [json.loads(line) for line in child_path.read_text(encoding='utf-8').splitlines()]
    child.append(BASE.row('event_msg', {'type': 'task_started'}, 9, ordinal=14))
    BASE.write_jsonl(child_path, child)
    normalized = CT.load(folder)
    meta = normalized['metas']['main:spawn']
    assert meta['lifetime_source'] == 'codex-task-events'
    assert meta['_start'].endswith('09Z') and meta['_end'].endswith('12Z')
    assert [r['event'] for r in meta['lifetime_records']] == ['task_complete', 'task_started']
    trace = SR.Trace(folder)
    dispatch = trace.by_id['main:spawn']
    assert trace.dispatch_start(dispatch).second == 9
    assert trace.agent_end[dispatch.id].second == 12
    assert trace.agent_end[dispatch.id] > dispatch.rts


def ownership_thread(extra_read=None, notes_task='T1', product=True):
    rows = [BASE.row('event_msg', {'type': 'item_completed', 'item': {'id': 'read',
        'type': 'CommandExecution', 'status': 'completed', 'exit_code': 0,
        'command': ['pwsh', '-Command', 'Get-Content docs/p/tasks/T1-core.md; Get-Content docs/p/tasks/T2-cli.notes.md']}}, 1, ordinal=10),
        BASE.row('event_msg', {'type': 'item_completed', 'item': {'id': 'notes', 'type': 'FileChange',
            'status': 'completed', 'changes': {'docs/p/tasks/' + notes_task + '-core.notes.md': {'type': 'add', 'content': 'work evidence'}}}}, 2, ordinal=11)]
    if product:
        rows.append(BASE.row('event_msg', {'type': 'item_completed', 'item': {'id': 'code',
            'type': 'FileChange', 'status': 'completed', 'changes': {'csvledger/core.py': {'type': 'add', 'content': 'implementation'}}}}, 3, ordinal=12))
    if extra_read:
        rows.append(BASE.row('event_msg', {'type': 'item_completed', 'item': {'id': 'read2',
            'type': 'CommandExecution', 'status': 'completed', 'exit_code': 0,
            'command': ['pwsh', '-Command', 'Get-Content docs/p/tasks/' + extra_read + '-cli.md']}}, 4, ordinal=13))
    return {'role': 'geeky-coder', 'rows': rows}


def test_unique_own_task_read_notes_write_and_product_write_supply_provenance():
    evidence = CT.task_ownership_evidence(ownership_thread())
    assert evidence['tasks'] == ['T1']
    assert evidence['task_reads'][0]['ordinal'] == 10
    assert evidence['notes_writes'][0]['path'].endswith('T1-core.notes.md')
    assert evidence['product_writes'][0]['path'] == 'csvledger/core.py'


@pytest.mark.parametrize('options', [{'extra_read': 'T2'}, {'notes_task': 'T2'}, {'product': False}])
def test_general_prereads_or_conflicting_work_never_assign_owner(options):
    assert CT.task_ownership_evidence(ownership_thread(**options)) is None


def test_actual_saved_trace_read_only_lifetimes_owners_and_commands():
    supplied = os.environ.get('CODEX_RUN2_SOURCE')
    if not supplied:
        pytest.skip('Set CODEX_RUN2_SOURCE to inspect the retained paid run read-only')
    folder = Path(supplied)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}
    trace = SR.Trace(folder)
    researchers = trace.agents('spec-research')
    intervals = [(trace.dispatch_start(c), trace.agent_end[c.id]) for c in researchers]
    concurrency = max(sum(start <= point < end for start, end in intervals) for point, _ in intervals)
    assert len(researchers) >= 3 and concurrency >= 3
    ownership = [meta.get('task_ownership', {}).get('tasks') for meta in trace.agent_meta.values()]
    assert ['T1'] in ownership and ['T2'] in ownership
    derived = SR.Scorer(folder, rerun=False)
    derived.load()
    derived.derive()
    assert set(derived.coders) == {'T1', 'T2'}
    assert all(derived.coders[task] for task in ('T1', 'T2'))
    run = json.loads((folder / 'run.json').read_text(encoding='utf-8-sig'))
    tasks = Path(run['workspace']) / 'docs/spec-001-feature/tasks'
    scorer = SR.Scorer.__new__(SR.Scorer)
    scorer.run = run
    scorer.task_files = {name: next(p for p in tasks.glob(name + '-*.md') if not p.name.endswith('.notes.md')) for name in ('T1', 'T2')}
    assert scorer.validation_commands('T1') == ['python -m pytest tests/test_core.py -q']
    assert scorer.validation_commands('T2') == ['python -m pytest -q', 'python -m csvledger --help']
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}
