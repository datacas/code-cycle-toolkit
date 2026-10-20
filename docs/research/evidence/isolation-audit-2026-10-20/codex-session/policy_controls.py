"""Policy-only controls; do not execute the classified commands."""
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path[:0] = [str(BASE), str(BASE / 'checkout-git' / 'scripts')]
from audit import save
import stage_permissions as sp

policy = sp.StagePolicy(role='review', repo='owner/repo', change_request_id='123',
                        work_item_id='7', base_branch='main', tmpdir=str(BASE / 'writable'),
                        tools=('futurefmt',), rtk=True)
rows = []
for command in ['futurefmt --check src', 'git push --force origin HEAD:main',
                'gh pr merge 123 --repo owner/repo', 'docker compose up -d',
                'cat .env > /tmp/outside-current-dispatch', 'printenv',
                'curl https://example.com', 'git status --short']:
    result = sp.check(command, policy, cwd=str(BASE / 'checkout-git'))
    rows.append({'command': command, 'allowed': result.allowed, 'reason': result.reason,
                 'needs_docker': result.needs_docker})
save('E7-policy-controls.json', {'scope': 'parser and policy only, not OS sandbox or hostile child code', 'results': rows})
print(json.dumps(rows), flush=True)

import grants
scope = grants.DOCKER_HOST.scope_for('codex', 'linux')
store = grants.ConsentStore(BASE / 'synthetic-consents.json')
store.save('audit/synthetic-only', grants.DOCKER_HOST, scope, 'allow')
broker = grants.ConsentBroker('audit/synthetic-only', platform='linux', store=store)
before = broker.granted('codex')
answer = broker.request(grants.DOCKER_HOST, 'codex', role='review')
after = broker.granted('codex')
result = {'scope': 'synthetic repository and temporary store only', 'before_request': list(before),
          'request_answer': answer, 'after_request': list(after)}
save('E9-persistent-consent-hydration.json', result)
print(json.dumps(result), flush=True)
