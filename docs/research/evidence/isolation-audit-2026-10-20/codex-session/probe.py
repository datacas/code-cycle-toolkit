"""Synthetic security probes: no real credentials or external network traffic."""
import json
import os
from pathlib import Path
import socket
import sys

writable, secret = map(Path, sys.argv[1:])
results = {}
for name, action in [
    ('allowed_write', lambda: (writable / 'allowed.txt').write_text('ok')),
    ('denied_sibling_write', lambda: (writable.parent / 'unauthorized.txt').write_text('should be denied')),
    ('denied_credential_read', lambda: secret.read_text()),
    ('unix_socket_creation', lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()),
    ('inet_socket_creation', lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()),
]:
    try:
        action()
        results[name] = {'result': 'allowed'}
    except OSError as error:
        results[name] = {'result': 'denied', 'errno': error.errno, 'error': str(error)}
daemon = Path(f'/tmp/codex-daemon-{os.getuid()}')
try:
    results['daemon_mode'] = oct(daemon.stat().st_mode & 0o777)
except OSError as error:
    results['daemon_error'] = str(error)
print(json.dumps(results))
