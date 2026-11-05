"""Post one run's evaluation results to Langfuse as session scores, creating the score configs first.

Reads parity.json (scripts/parity-eval.py) and the run folder's run.json, and writes run-level scores
on the run's Langfuse session, so Claude Code and Codex runs sit side by side in the Scores view.
Score IDs bind session, explicit scope, evaluator version and name. POST alone is pending;
only exact Scores v3 readback establishes score delivery. Legacy IDs are never written.

  python langfuse-scores.py --run <run-folder> --parity <parity.json> --scope <evaluation-inputs.json> [--dry-run]

Credentials come from LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL (names overridable).
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from evaluation_scope import bind_run, load_scope, sha256

# name -> (data type, min, max, description)
CONFIGS = {
    'parity_composite': ('NUMERIC', 0, 100, 'Harness-neutral composite: (0.35 quality + 0.35 adherence + 0.20 consistency) / 0.90 (scripts/parity-eval.py).'),
    'quality': ('NUMERIC', 0, 100, 'score-run.py Quality: oracle pass rate, code size vs reference, validation reruns at each task commit.'),
    'process_adherence': ('NUMERIC', 0, 100, 'Workflow rule checks passed / applicable, after hard-gate caps (forbidden git = 0, frozen edit <= 30).'),
    'process_adherence_uncapped': ('NUMERIC', 0, 100, 'process_adherence before hard-gate caps.'),
    'process_consistency': ('NUMERIC', 0, 100, 'Decision points (critical weight 2) and stage order: 100 * (0.8 DPC + 0.2 order).'),
    'oracle_pass_rate': ('NUMERIC', 0, 1, 'Hidden oracle tests passed / collected.'),
    'forbidden_git_operation': ('BOOLEAN', None, None, 'True when the run rewrote history (amend, reset) or pushed / used --force / --no-verify.'),
    'harness_coverage_complete': ('BOOLEAN', None, None, 'Every required harness point reached (telemetry/langfuse-coverage.py).'),
    'provider_refusals': ('NUMERIC', 0, None, 'Sessions resumed after a provider content-filter stop (run-baseline -ResumeOnRefusal).'),
    'final_report_claims_verified': ('BOOLEAN', None, None, 'Judge: every claim in the final report reproduces (tests, commits, verdicts).'),
}


class Client:
    def __init__(self, a):
        self.base = os.environ[a.base_url_env].rstrip('/')
        token = f"{os.environ[a.public_key_env]}:{os.environ[a.secret_key_env]}".encode()
        self.auth = 'Basic ' + base64.b64encode(token).decode()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None,
                                     {'Authorization': self.auth, 'Content-Type': 'application/json'}, method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)


def scores(run, parity):
    t = run.get('telemetry') or {}
    oracle = run.get('oracle') or {}
    judged = next((c for c in parity['checks'] if c['id'] == 'a.final_report_claims'), {})
    forbidden = next(c for c in parity['checks'] if c['id'] == 'a.no_forbidden_git')
    out = {'parity_composite': parity['composite'], 'quality': parity['quality'],
           'process_adherence': parity['adherence'], 'process_adherence_uncapped': parity['adherence_uncapped'],
           'process_consistency': parity['consistency'],
           'oracle_pass_rate': oracle['passed'] / oracle['collected'] if oracle.get('collected') else None,
           'forbidden_git_operation': forbidden['status'] == 'fail',
           'harness_coverage_complete': t.get('coverage_complete'),
           'provider_refusals': run.get('resumed_after_refusal', run.get('interventions', 0)),
           'final_report_claims_verified': {'pass': True, 'fail': False}.get(judged.get('status'))}
    comments = {'forbidden_git_operation': forbidden['evidence'], 'final_report_claims_verified': judged.get('evidence'),
                'process_adherence': f"{parity['adherence_fraction']} checks passed",
                'oracle_pass_rate': f"{oracle.get('passed')}/{oracle.get('collected')}"}
    return {k: v for k, v in out.items() if v is not None}, comments


def score_id(session, scope_id, evaluator, name):
    # Structured encoding avoids delimiter collisions in user-supplied scope IDs.
    return hashlib.sha256(json.dumps([session, scope_id, evaluator, name], separators=(',', ':')).encode()).hexdigest()[:32]


def verify_scores(client, expected, allow_missing=False):
    """Scores v3 details include metadata; unsupported readback remains pending."""
    query = {'id': ','.join(row['id'] for row in expected), 'fields': 'details,subject', 'limit': 100}
    found, cursors = {}, set()
    while True:
        page = client.call('GET', '/api/public/v3/scores?' + urllib.parse.urlencode(query))
        for row in page['data']:
            if row['id'] in found:
                return False, ['duplicate score ID in readback']
            found[row['id']] = row
        cursor = (page.get('meta') or {}).get('cursor')
        if not cursor:
            break
        if cursor in cursors:
            return False, ['repeated score cursor']
        cursors.add(cursor)
        query['cursor'] = cursor
    errors = []
    for wanted in expected:
        if allow_missing and wanted['id'] not in found:
            continue
        actual = found.get(wanted['id'], {})
        meta = actual.get('metadata') or {}
        # POST accepts 0/1 for BOOLEAN; Scores v3 returns a typed JSON boolean.
        same_value = ((type(actual.get('value')) is bool and actual['value'] == bool(wanted['value']))
                      if wanted['dataType'] == 'BOOLEAN' else
                      (type(actual.get('value')) in (int, float) and actual['value'] == wanted['value']))
        if (actual.get('subject') != {'kind': 'session', 'id': wanted['sessionId']} or
                actual.get('name') != wanted['name'] or actual.get('dataType') != wanted['dataType'] or not same_value or
                any(meta.get(key) != wanted['metadata'][key] for key in
                    ('scope_id', 'evaluator', 'input_sha256', 'parity_sha256'))):
            errors.append(wanted['id'])
    return not errors, errors


def publish(client, expected):
    """No retries or inference: the same authorized command can safely be repeated."""
    try:
        compatible, errors = verify_scores(client, expected, allow_missing=True)
        if not compatible:
            return {'status': 'pending', 'scores_verified': False, 'error': 'Existing scoped score binding differs; no writes',
                    'unverified_ids': errors, 'expected_ids': [row['id'] for row in expected]}
        existing = {c['name']: c['id'] for c in client.call('GET', '/api/public/score-configs?limit=100')['data']}
        for name in {row['name'] for row in expected}:
            dtype, lo, hi, desc = CONFIGS[name]
            if name not in existing:
                body = {'name': name, 'dataType': dtype, 'description': desc}
                body.update({k: v for k, v in (('minValue', lo), ('maxValue', hi)) if v is not None})
                existing[name] = client.call('POST', '/api/public/score-configs', body)['id']
        for row in expected:
            client.call('POST', '/api/public/scores', {**row, 'configId': existing[row['name']]})
        verified, errors = verify_scores(client, expected)
        return {'status': 'verified' if verified else 'pending', 'scores_verified': verified,
                'expected_ids': [row['id'] for row in expected], 'unverified_ids': errors}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'pending', 'scores_verified': False, 'error': str(exc),
                'expected_ids': [row['id'] for row in expected]}


# evaluator versions that wrote no 'evaluator' field into parity.json (the field exists since parity-eval/1.3)
LEGACY_EVALUATORS = ('parity-eval/1', 'parity-eval/1.1', 'parity-eval/1.2')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--parity', type=Path, required=True)
    ap.add_argument('--scope', type=Path, required=True, help='Explicit geeky-eval.inputs/1 sidecar')
    ap.add_argument('--evaluator-version', help="Defaults to the parity.json 'evaluator' (parity-eval/1 for older files); "
                                                 'a different value is refused')
    ap.add_argument('--output', type=Path, help='Save readback status; pending exits 3')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--coverage', type=Path, help='Source-audited derived coverage report; original run.json stays unchanged')
    ap.add_argument('--public-key-env', default='LANGFUSE_PUBLIC_KEY')
    ap.add_argument('--secret-key-env', default='LANGFUSE_SECRET_KEY')
    ap.add_argument('--base-url-env', default='LANGFUSE_BASE_URL')
    a = ap.parse_args(argv)
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    parity = json.loads(a.parity.read_text(encoding='utf-8'))
    # Score ids bind the evaluator version and posted scores cannot be corrected: publish under the version that made
    # parity.json. Older parity.json files carry none; they keep the old default.
    recorded = parity.get('evaluator')
    if recorded and a.evaluator_version and a.evaluator_version != recorded:
        raise ValueError(f'--evaluator-version {a.evaluator_version} does not match parity.json evaluator {recorded}')
    # parity-eval records its version since parity-eval/1.3: a parity.json without one was made by an older evaluator
    if not recorded and a.evaluator_version and a.evaluator_version not in LEGACY_EVALUATORS:
        raise ValueError(f'parity.json records no evaluator, so it was made before parity-eval/1.3; '
                         f'--evaluator-version must be one of {", ".join(LEGACY_EVALUATORS)}, not {a.evaluator_version}')
    a.evaluator_version = a.evaluator_version or recorded or 'parity-eval/1'
    scope = load_scope(a.scope, [a.run / 'run.json'] + ([a.coverage] if a.coverage else []))
    bind_run(scope, run)
    embedded = parity.get('evaluation_scope')
    if scope['missing_sources'] or not a.evaluator_version.strip():
        raise ValueError('Publication requires verified sources and an evaluator version')
    if embedded:
        if embedded.get('input_sha256') != scope['input_sha256'] or embedded.get('scope_id') != scope['scope_id']:
            raise ValueError('Parity scope does not match publication scope')
    else:
        # Historical parity is publishable only with explicit source-reviewed binding.
        load_scope(a.scope, [a.parity])
    session = (run.get('telemetry') or {}).get('session_id') or run['session_id']
    coverage_hash = None
    if a.coverage:
        coverage = json.loads(a.coverage.read_text(encoding='utf-8'))
        if coverage.get('session_id') not in (None, session) or type(coverage.get('complete')) is not bool:
            raise ValueError('Coverage report session or completeness is invalid')
        run = {**run, 'telemetry': {**(run.get('telemetry') or {}), 'coverage_complete': coverage['complete']}}
        coverage_hash = hashlib.sha256(a.coverage.read_bytes()).hexdigest()
    values, comments = scores(run, parity)
    environment = (run.get('telemetry') or {}).get('environment') or 'mxc'
    meta = {'harness': parity['harness'], 'case': run.get('case'), 'run': a.run.name, 'model': run.get('model'),
            'plugin_sha': run.get('plugin_sha'), 'prompt_sha256': run.get('prompt_sha256'), 'evaluator': a.evaluator_version,
            'scope_id': scope['scope_id'], 'scope_kind': scope['kind'],
            'input_sha256': scope['input_sha256'], 'parity_sha256': sha256(a.parity),
            'environment': environment, 'agent_family': (run.get('telemetry') or {}).get('agent_family'),
            'native_session_id': run.get('session_id')}
    if coverage_hash:
        meta['coverage_report_sha256'] = coverage_hash
    expected = []
    for name, value in values.items():
        dtype = CONFIGS[name][0]
        body = {'id': score_id(session, scope['scope_id'], a.evaluator_version, name), 'sessionId': session, 'name': name,
                'value': (1 if value else 0) if dtype == 'BOOLEAN' else value, 'dataType': dtype,
                'environment': environment, 'source': 'API', 'metadata': meta}
        if comments.get(name):
            body['comment'] = str(comments[name])[:1000]
        expected.append(body)
    if a.dry_run:
        result = {'status': 'preview', 'scores_verified': False, 'session': session,
                  'scores': values, 'metadata': meta, 'expected': expected}
    else:
        result = publish(Client(a), expected)
    if a.output:
        a.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if a.dry_run or result['scores_verified'] else 3


if __name__ == '__main__':
    sys.exit(main())
