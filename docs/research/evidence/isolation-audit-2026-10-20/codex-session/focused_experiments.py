"""Synthetic experiments against current source; no real publication/grant."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

BASE = Path(__file__).resolve().parent
ROOT = BASE / 'checkout-git'
sys.path[:0] = [str(BASE), str(ROOT / 'scripts'), str(ROOT / 'tests')]
from audit import command, save


def masked_copy():
    import test_validate_package as tests
    location = ROOT / '.claude' / 'settings.local.json'
    if location.exists():
        raise RuntimeError('Refusing to replace existing fixture file')
    location.parent.mkdir(exist_ok=True)
    location.write_text('{}')
    location.chmod(0)
    case = tests.ValidatePackageTests()
    result = {'kind': 'synthetic unreadable placeholder, not an actual Claude launch',
              'symbol': 'ValidatePackageTests.copy_package', 'placeholder': str(location)}
    try:
        case.copy_package()
        result['observed'] = 'copy unexpectedly succeeded'
    except OSError as error:
        result.update(observed=type(error).__name__, details=str(error))
    finally:
        location.chmod(0o600)
        location.unlink()
        case.doCleanups()
    save('E4-synthetic-masked-copy.json', result)
    print(json.dumps(result), flush=True)


def consent_replay():
    import test_environment_recovery as fixture
    import grants
    case = fixture.SandboxRerouteTests()
    case.setUp()
    try:
        consent = grants.ConsentBroker('owner/repo', platform='linux', answers={'docker_host': 'cycle'})
        primary = fixture.RestrictedAdapter()
        recorder = case.recorder([primary], consent=consent)
        before = dict(fixture.CLEAN)
        after = {**before, 'pulls': ['simulated publication after stage started']}
        with patch('cycle.effect_snapshot', side_effect=[before, after, after]) as observed:
            outcome = recorder.stage('implement', 'Audit fixture only: no real publication')
        result = {'kind': 'scripted adapter only; no host access or GitHub side effects',
                  'simulated_effect': 'pulls changed', 'snapshot_call_count': observed.call_count,
                  'dispatch_count': len(primary.dispatched),
                  'grants_per_dispatch': [list(k.get('sandbox_grants', ())) for k in primary.dispatch_kwargs],
                  'row_ids': list(outcome.rows), 'detail': outcome.result.detail}
        save('E8-consent-after-publication.json', result)
        print(json.dumps(result), flush=True)
    finally:
        case.tearDown()
        case.doCleanups()


def preparation():
    import dependency_setup as ds
    fixture = ROOT / 'audit-node-fixture'
    fixture.mkdir(exist_ok=False)
    (fixture / 'package.json').write_text(json.dumps({'name': 'audit-fixture', 'version': '1.0.0', 'private': True}))
    (fixture / 'package-lock.json').write_text(json.dumps({'name': 'audit-fixture', 'version': '1.0.0',
        'lockfileVersion': 3, 'requires': True, 'packages': {'': {'name': 'audit-fixture', 'version': '1.0.0'}}}))
    for executor in ['codex', 'claude']:
        evidence = BASE / ('E5-' + executor)
        report = ds.prepare(str(fixture), executor, executor, evidence_root=evidence)
        save('E5-' + executor + '-preparation.json', report)
        print(json.dumps({'experiment': 'E5', 'executor': executor, 'status': report['status'],
                          'items': [{'status': item['status'], 'error': item.get('error'),
                                     'attempt_count': len(item['attempts'])} for item in report['items']]}), flush=True)
    command('E5-fixture-status', ['git', 'status', '--porcelain=v1'], cwd=ROOT)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'consent-only':
        consent_replay()
    else:
        masked_copy()
        consent_replay()
        preparation()
