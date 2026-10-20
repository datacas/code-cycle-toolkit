#!/usr/bin/env python3
"""Read-only repository audit; all experiment state is under this directory."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
REPO = Path('$HOME/www/code-cycle-toolkit')
ENV = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}


def save(name, value):
    (BASE / name).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def command(label, argv, cwd=None, timeout=60, env=None):
    started = time.monotonic()
    record = {'label': label, 'argv': ['rtk', 'proxy', *map(str, argv)],
              'cwd': str(cwd or BASE), 'timeout': timeout}
    try:
        result = subprocess.run(record['argv'], cwd=cwd or BASE, env=env or ENV,
                                capture_output=True, text=True, timeout=timeout)
        record.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except subprocess.TimeoutExpired as error:
        record.update(exit_code=124, stdout=str(error.stdout or ''), stderr=str(error.stderr or ''), timeout_reached=True)
    except OSError as error:
        record.update(exit_code=None, stdout='', stderr=str(error))
    record['duration_seconds'] = round(time.monotonic() - started, 3)
    save(label + '.json', record)
    print(json.dumps({'label': label, 'exit_code': record['exit_code'],
                      'seconds': record['duration_seconds'],
                      'stderr_tail': record['stderr'][-1600:],
                      'stdout_tail': record['stdout'][-1600:]}, ensure_ascii=False), flush=True)
    return record


def metadata():
    info = {'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'kernel': platform.platform(), 'uid': os.getuid(), 'euid': os.geteuid(),
            'cwd': str(Path.cwd()), 'executables': {name: shutil.which(name) for name in
                ['codex', 'claude', 'srt', 'bwrap', 'socat', 'rg', 'node', 'npm', 'orca', 'python3']},
            'selected_environment': {k: os.environ.get(k) for k in
                ['TMPDIR', 'TEMP', 'TMP', 'CODEX_SANDBOX_NETWORK_DISABLED', 'CODEX_SANDBOX']}}
    socket_dir = Path(f'/tmp/codex-daemon-{os.getuid()}')
    try:
        value = socket_dir.stat()
        info['daemon_directory'] = {'path': str(socket_dir), 'mode': oct(stat.S_IMODE(value.st_mode)), 'uid': value.st_uid}
        try:
            info['daemon_directory']['listable'] = bool(list(socket_dir.iterdir()))
        except OSError as error:
            info['daemon_directory']['list_error'] = str(error)
    except OSError as error:
        info['daemon_directory'] = {'path': str(socket_dir), 'error': str(error)}
    mountinfo = Path('/proc/self/mountinfo').read_text()
    info['relevant_mounts'] = [line for line in mountinfo.splitlines() if
        str(socket_dir) in line or line.split()[4] in ['/', '/tmp', '/var/tmp']]
    info['process_security'] = [line for line in Path('/proc/self/status').read_text().splitlines()
                                if line.startswith(('NoNewPrivs:', 'Seccomp:', 'CapEff:'))]
    ancestors = []
    pid = os.getpid()
    for _ in range(8):
        try:
            comm = Path(f'/proc/{pid}/comm').read_text().strip()
            status = Path(f'/proc/{pid}/status').read_text().splitlines()
            parent = int(next(line.split()[1] for line in status if line.startswith('PPid:')))
            ancestors.append({'pid': pid, 'comm': comm, 'ppid': parent})
            if not parent or parent == pid:
                break
            pid = parent
        except (OSError, StopIteration):
            break
    info['ancestors'] = ancestors
    save('environment.json', info)
    print(json.dumps(info, ensure_ascii=False), flush=True)
    for label, argv in [('version-codex', ['codex', '--version']), ('version-claude', ['claude', '--version']),
                        ('version-srt', ['srt', '--version']), ('version-bwrap', ['bwrap', '--version']),
                        ('version-socat', ['socat', '-V']), ('version-rg', ['rg', '--version']),
                        ('version-node', ['node', '--version']), ('version-npm', ['npm', '--version']),
                        ('repo-head', ['git', 'rev-parse', 'HEAD']), ('repo-status-before', ['git', 'status', '--porcelain=v1'])]:
        command(label, argv, cwd=REPO)


def collect():
    issues = []
    prs = {239, 266, 269, 273, 277, 283, 287}
    for number in [141, 239, 261, 266, 268, 274, 279, 288, 289]:
        record = command(f'issue-{number}', ['gh', 'issue', 'view', str(number), '--repo', 'datacas/code-cycle-toolkit',
            '--json', 'number,title,state,url,updatedAt,body,comments,closedByPullRequestsReferences'])
        if record['exit_code'] == 0:
            item = json.loads(record['stdout'])
            issues.append({key: item[key] for key in ['number', 'title', 'state', 'url', 'updatedAt']})
            prs.update(p['number'] for p in item['closedByPullRequestsReferences'])
        timeline = command(f'issue-{number}-timeline', ['gh', 'api', '--paginate', '--slurp',
            f'repos/datacas/code-cycle-toolkit/issues/{number}/timeline'])
        if timeline['exit_code'] == 0:
            for page in json.loads(timeline['stdout']):
                for event in page:
                    issue = (event.get('source') or {}).get('issue') or {}
                    if issue.get('pull_request') and issue.get('repository_url', '').endswith('/datacas/code-cycle-toolkit'):
                        prs.add(issue['number'])
    save('issue-index.json', issues)
    for number in sorted(prs):
        command(f'pr-{number}', ['gh', 'pr', 'view', str(number), '--repo', 'datacas/code-cycle-toolkit',
            '--json', 'number,title,state,url,mergedAt,mergeCommit,baseRefName,headRefOid,body,files'], timeout=60)


def snapshot():
    destination = BASE / 'checkout'
    if destination.exists():
        raise RuntimeError('Snapshot already exists; refusing to replace it')
    destination.mkdir()
    listing = subprocess.run(['rtk', 'proxy', 'git', 'ls-files', '-z'], cwd=REPO, check=True, capture_output=True).stdout
    for raw in listing.split(b'\0'):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        source = REPO / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, target)
    print('snapshot', destination, flush=True)


def native():
    writable = BASE / 'writable'
    writable.mkdir(exist_ok=True)
    secret = BASE / 'synthetic-credential.txt'
    secret.write_text('AUDIT_SYNTHETIC_SECRET_NOT_A_REAL_CREDENTIAL\n', encoding='utf-8')
    settings = {'network': {'allowedDomains': [], 'deniedDomains': [], 'allowLocalBinding': False},
                'filesystem': {'allowWrite': [str(writable)], 'denyWrite': [], 'denyRead': [str(secret)]}}
    save('srt-settings.json', settings)
    probe = ['python3', '-B', str(BASE / 'probe.py'), str(writable), str(secret)]
    managed = ['--include-managed-config'] if os.environ.get('CCT_AUDIT_INCLUDE_MANAGED') == '1' else []
    codex = ['codex', 'sandbox', *managed, '-P', 'audit_read', '-c', 'permissions.audit_read.extends=":read-only"',
             '-c', 'permissions.audit_read.network.enabled=false',
             '-c', 'permissions.audit_read.filesystem={' + json.dumps(str(writable)) + '="write",' + json.dumps(str(secret)) + '="none"}', '--']
    command('E1-E2-codex-native-corrected', [*codex, *probe], cwd=writable, timeout=45)
    command('E3-E7-srt-attempt', ['srt', '--settings', str(BASE / 'srt-settings.json'), '--', *probe], cwd=writable, timeout=45)
    command('E2-codex-minimal-readonly', ['codex', 'sandbox', *managed, '-P', ':read-only', '--', 'python3', '-B', '-c', 'print("native sandbox command reached")'], cwd=writable, timeout=45)


def metrics():
    files = ['cycle.py', 'executors.py', 'run_cycle.py', 'reroute.py', 'environment_recovery.py',
             'grants.py', 'stage_permissions.py', 'dependency_setup.py', 'user_choices.py', 'issue_review.py']
    rows = []
    for name in files:
        path = REPO / 'scripts' / name
        content = path.read_text()
        tree = ast.parse(content)
        rows.append({'file': str(path.relative_to(REPO)), 'lines': len(content.splitlines()),
                     'functions': sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(tree)),
                     'if_nodes': sum(isinstance(n, ast.If) for n in ast.walk(tree)),
                     'try_nodes': sum(isinstance(n, ast.Try) for n in ast.walk(tree))})
    save('complexity.json', rows)
    print(json.dumps(rows), flush=True)


def tests():
    command('targeted-tests-git', ['python3', '-B', str(BASE / 'test_runner.py')], cwd=BASE / 'checkout-git', timeout=300)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['metadata', 'collect', 'snapshot', 'native', 'metrics', 'tests'])
    mode = parser.parse_args().mode
    globals()[mode]()
