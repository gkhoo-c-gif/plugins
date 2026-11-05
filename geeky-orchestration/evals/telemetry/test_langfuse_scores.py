"""Offline publication/readback and existing telemetry recovery; no live services."""
import argparse
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('publisher', HERE / 'langfuse-scores.py')
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)


@pytest.fixture
def http(monkeypatch):
    """Exercise the current Client's HTTP request encoding and response handling."""
    state = {'scores': {}, 'requests': [], 'mode': 'ok'}
    for name in ('PUBLIC_KEY', 'SECRET_KEY', 'BASE_URL'):
        monkeypatch.setenv('LANGFUSE_' + name, 'http://fixture.invalid' if name == 'BASE_URL' else 'fixture')

    def urlopen(req, timeout):
        parsed = urllib.parse.urlparse(req.full_url)
        body = json.loads(req.data) if req.data else None
        state['requests'].append((req.method, parsed.path, body))
        if state['mode'] == 'dns':
            raise urllib.error.URLError('fake DNS failure')
        if parsed.path == '/api/public/score-configs':
            result = {'data': [{'name': name, 'id': name} for name in ls.CONFIGS]}
        elif req.method == 'POST' and parsed.path == '/api/public/scores':
            if state['mode'] == 'partial' and len(state['scores']) == 1:
                raise urllib.error.URLError('fake partial delivery')
            state['scores'][body['id']] = body
            result = {'id': body['id']}
        else:
            assert req.method == 'GET' and parsed.path == '/api/public/v3/scores'
            query = urllib.parse.parse_qs(parsed.query)
            assert query['fields'] == ['details,subject'] and query['limit'] == ['100']
            ids = query['id'][0].split(',')
            rows = [{**copy.deepcopy(row), 'subject': {'kind': 'session', 'id': row['sessionId']}}
                    for key, row in state['scores'].items() if key in ids]
            for row in rows:
                if row['dataType'] == 'BOOLEAN':
                    row['value'] = bool(row['value'])
            if state['mode'] == 'missing' and rows:
                rows.pop()
            elif state['mode'] == 'hash' and rows:
                rows[0]['metadata']['input_sha256'] = 'wrong'
            elif state['mode'] == 'value' and rows:
                rows[0]['value'] += 1
            elif state['mode'] == 'boolean-type' and rows:
                next(row for row in rows if row['dataType'] == 'BOOLEAN')['value'] = 0
            elif state['mode'] == 'type' and rows:
                rows[0]['value'] = str(rows[0]['value'])
            elif state['mode'] == 'subject' and rows:
                rows[0]['subject']['id'] = 'another-session'
            elif state['mode'] == 'unsupported':
                raise urllib.error.HTTPError(req.full_url, 404, 'unsupported v3', {}, None)
            result = {'data': rows, 'meta': {}}
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(ls.urllib.request, 'urlopen', urlopen)
    client = ls.Client(argparse.Namespace(base_url_env='LANGFUSE_BASE_URL', public_key_env='LANGFUSE_PUBLIC_KEY',
                                        secret_key_env='LANGFUSE_SECRET_KEY'))
    return client, state


def expected(scope='initial', evaluator='parity-eval/1'):
    return [{'id': ls.score_id('session', scope, evaluator, name), 'sessionId': 'session', 'name': name,
             'dataType': ls.CONFIGS[name][0], 'value': value,
             'metadata': {'scope_id': scope, 'evaluator': evaluator, 'input_sha256': 'a' * 64,
                          'parity_sha256': 'b' * 64}}
            for name, value in (('quality', 95.64), ('forbidden_git_operation', 0))]


def test_scoped_publication_is_idempotent_and_preserves_legacy_ids(http):
    client, state = http
    legacy = ls.hashlib.sha256(b'session:quality').hexdigest()[:32]
    state['scores'][legacy] = {'id': legacy, 'sessionId': 'session', 'name': 'quality', 'value': 90.33}
    assert ls.publish(client, expected())['scores_verified'] is True
    assert ls.publish(client, expected())['scores_verified'] is True
    assert len(state['scores']) == 3 and state['scores'][legacy]['value'] == 90.33
    assert ls.publish(client, expected('cumulative'))['scores_verified'] is True
    assert ls.publish(client, expected(evaluator='parity-eval/2'))['scores_verified'] is True
    assert len(state['scores']) == 7
    assert ls.score_id('a:b', 'c', 'd', 'e') != ls.score_id('a', 'b:c', 'd', 'e')


@pytest.mark.parametrize('mode', ['missing', 'hash', 'value', 'type', 'boolean-type', 'subject', 'unsupported', 'dns', 'partial'])
def test_missing_mismatched_and_failed_delivery_remains_pending(http, mode):
    client, state = http
    state['mode'] = mode
    result = ls.publish(client, expected())
    assert result['status'] == 'pending' and result['scores_verified'] is False


def test_score_readback_checks_all_identity_fields(http):
    client, state = http
    assert ls.publish(client, expected())['scores_verified']
    for field in ('scope_id', 'evaluator', 'input_sha256', 'parity_sha256'):
        rows = expected()
        state['scores'][rows[0]['id']]['metadata'][field] = 'wrong'
        assert ls.verify_scores(client, rows)[0] is False
        state['scores'][rows[0]['id']] = rows[0]


def test_reusing_scope_with_changed_input_cannot_overwrite_prior_scores(http):
    client, state = http
    rows = expected()
    assert ls.publish(client, rows)['scores_verified']
    original = copy.deepcopy(state['scores'])
    changed = copy.deepcopy(rows)
    changed[0]['metadata']['input_sha256'] = 'changed-input'
    state['requests'].clear()
    assert ls.publish(client, changed)['status'] == 'pending'
    assert state['scores'] == original
    assert all(method == 'GET' for method, _, _ in state['requests'])


def test_publication_requires_explicit_scope_before_client_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, 'Client', lambda _: pytest.fail('client must not be created'))
    with pytest.raises(SystemExit) as exc:
        ls.main(['--run', str(tmp_path), '--parity', str(tmp_path / 'parity.json')])
    assert exc.value.code == 2


def test_publication_refuses_an_evaluator_version_parity_json_did_not_record(tmp_path, monkeypatch):
    # parity-path review, 2026-11-05: published scores cannot be corrected, and their ids bind the evaluator version
    monkeypatch.setattr(ls, 'Client', lambda _: pytest.fail('client must not be created'))
    (tmp_path / 'run.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'parity.json').write_text(json.dumps({'evaluator': 'parity-eval/1.3'}), encoding='utf-8')
    with pytest.raises(ValueError, match='does not match parity.json evaluator parity-eval/1.3'):
        ls.main(['--run', str(tmp_path), '--parity', str(tmp_path / 'parity.json'), '--scope', str(tmp_path / 's.json'),
                 '--evaluator-version', 'parity-eval/1.2', '--dry-run'])

def test_publication_uses_the_recorded_evaluator_and_refuses_new_versions_for_old_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ls, 'Client', lambda _: pytest.fail('client must not be created'))
    monkeypatch.setattr(ls, 'load_scope', lambda *a: {'missing_sources': [], 'scope_id': 's', 'kind': 'initial', 'input_sha256': 'a' * 64})
    monkeypatch.setattr(ls, 'bind_run', lambda *a: None)
    monkeypatch.setattr(ls, 'scores', lambda run, parity: ({'quality': 95.0}, {}))
    (tmp_path / 'run.json').write_text(json.dumps({'session_id': 'session'}), encoding='utf-8')
    parity = tmp_path / 'parity.json'
    args = ['--run', str(tmp_path), '--parity', str(parity), '--scope', str(tmp_path / 's.json'), '--dry-run']
    parity.write_text(json.dumps({'evaluator': 'parity-eval/1.3', 'harness': 'claude-code'}), encoding='utf-8')
    assert ls.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['metadata']['evaluator'] == 'parity-eval/1.3'
    assert result['expected'][0]['id'] == ls.score_id('session', 's', 'parity-eval/1.3', 'quality')
    parity.write_text(json.dumps({'harness': 'claude-code'}), encoding='utf-8')   # made by an older evaluator
    with pytest.raises(ValueError, match='records no evaluator'):
        ls.main(args + ['--evaluator-version', 'parity-eval/1.3'])
    assert ls.main(args) == 0 and json.loads(capsys.readouterr().out)['metadata']['evaluator'] == 'parity-eval/1'

@pytest.mark.parametrize('mode', ['dns', 'zero', 'partial', 'covered'])
def test_actual_telemetry_recovery_never_launches_contestant(tmp_path, mode):
    # Only this existing function is executed; its Python boundary is a local deny-by-default fake.
    telemetry = HERE / 'telemetry.ps1'
    fake = tmp_path / 'fake-python.ps1'
    fake.write_text("""param([Parameter(ValueFromRemainingArguments)]$Arguments)
$scriptName = [IO.Path]::GetFileName($Arguments[0])
Add-Content -LiteralPath $env:T3_CALLS -Value $scriptName
if ($scriptName -notin @('langfuse-run.py','langfuse-coverage.py')) { throw 'Contestant/provider executable forbidden' }
if ($env:T3_MODE -eq 'dns') { $global:LASTEXITCODE=2; return }
$outputPath = $Arguments[[Array]::IndexOf($Arguments, '--output') + 1]
if ($scriptName -eq 'langfuse-run.py') {
    $n=if ($env:T3_MODE -eq 'zero') { 0 } else { 3 }
    @{ observations=$n; user_ids=@(); agent_roles=@(); traces=1 } | ConvertTo-Json | Set-Content -LiteralPath $outputPath
    $global:LASTEXITCODE=if ($n) { 0 } else { 3 }
} else {
    @{complete=($env:T3_MODE -eq 'covered'); missed=@('stage:impl-review')} | ConvertTo-Json | Set-Content -LiteralPath $outputPath
    $global:LASTEXITCODE=0
}
""", encoding='utf-8')
    script = tmp_path / 'check.ps1'
    script.write_text("""param($TelemetryPath,$Fake,$OutDir,$Mode)
$ErrorActionPreference='Stop'
. $TelemetryPath
$env:T3_MODE=$Mode; $env:T3_CALLS=Join-Path $OutDir 'calls.txt'
$t=[pscustomobject]@{Record=[ordered]@{host_flush='exit 0';delivered=$true;coverage_complete=$true;metadata=@{}}; StateDir=$null;KeyNames=@('A','B','C')}
# A stale summary must not convert a DNS failure into delivery.
@{observations=999;traces=1} | ConvertTo-Json | Set-Content (Join-Path $OutDir 'langfuse.json')
1..2 | ForEach-Object { Save-EvalTelemetry -Telemetry $t -SessionId 'fixture' -OutDir $OutDir -Python $Fake -WaitSec 0 }
$t.Record | ConvertTo-Json -Depth 10
""", encoding='utf-8')
    result = subprocess.run(['pwsh', '-NoProfile', '-File', str(script), str(telemetry), str(fake), str(tmp_path), mode],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)
    assert record['scores_verified'] is False and record['scores_status'] == 'pending'
    calls = (tmp_path / 'calls.txt').read_text().splitlines()
    assert calls == (['langfuse-run.py', 'langfuse-coverage.py'] * 2 if mode in ('partial', 'covered') else ['langfuse-run.py'] * 2)
    assert record['delivered'] == record['observations_present'] == (mode in ('partial', 'covered'))
    assert record['coverage_status'] == {'dns': 'unknown', 'zero': 'unknown', 'partial': 'missing', 'covered': 'met'}[mode]
    assert record['observations'] == (3 if mode in ('partial', 'covered') else None)
