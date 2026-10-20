import hashlib
import json
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent
report = BASE / 'informe.md'
content = report.read_text()
headings = re.findall(r'^## ([A-M])\. ', content, re.M)
assert headings == list('ABCDEFGHIJKLM'), headings
summary = content.split('## A. Resumen ejecutivo\n', 1)[1].split('## B.', 1)[0]
summary_words = len(summary.split())
assert summary_words <= 500, summary_words
for number in [141, 239, 261, 266, 268, 274, 279, 288, 289]:
    assert f'#{number}' in content
for number in range(1, 8):
    assert f'**E{number} ' in content
local_links = re.findall(r'\]\((/[^)]+)\)', content)
missing = []
bad_lines = []
for link in local_links:
    match = re.match(r'^(.*?)(?::([0-9]+))?$', link)
    filename, line = match.groups()
    path = Path(filename)
    if not path.exists():
        missing.append(link)
    elif line and int(line) > len(path.read_text().splitlines()):
        bad_lines.append(link)
assert not missing, missing
assert not bad_lines, bad_lines
manifest = []
for path in sorted(BASE.rglob('*')):
    if path.is_file() and path.name != 'evidence-manifest.json' and not any(part in {'checkout', 'checkout-git'} for part in path.relative_to(BASE).parts):
        manifest.append({'file': str(path.relative_to(BASE)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'bytes': path.stat().st_size})
(BASE / 'evidence-manifest.json').write_text(json.dumps(manifest, indent=2))
print(json.dumps({'sections': headings, 'summary_words': summary_words, 'report_words': len(content.split()),
                  'local_links': len(local_links), 'missing_links': missing, 'bad_line_anchors': bad_lines,
                  'evidence_files': len(manifest), 'report_sha256': hashlib.sha256(report.read_bytes()).hexdigest()}))
