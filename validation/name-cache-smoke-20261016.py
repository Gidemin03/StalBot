"""One-off CLI smoke: isolated DBs, no desktop input or production DB access."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / 'validation/name-cache-cli-20261016.json'
if target.exists():
    raise SystemExit('Choose a new output; previous reports must not be overwritten')
integration = json.loads((ROOT / 'validation/name-cache-integration-20261016.json').read_text(encoding='utf-8'))
for path, digest in integration['provenance']['code_sha256'].items():
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
outputs = []
with tempfile.TemporaryDirectory(prefix='stalbot-cache-smoke-') as directory:
    for capacity in (1000, 0):
        result = subprocess.run([
            sys.executable, str(ROOT / 'main.py'),
            '--replay', str(ROOT / 'dataset/templates/Обычный скриншот.png'),
            '--db', str(Path(directory) / f'empty-{capacity}.sqlite3'),
            '--batch', '9', '--name-cache-capacity', str(capacity),
        ], cwd=ROOT, capture_output=True, encoding='utf-8',
            env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'})
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload['name_cache']['capacity'] == capacity
        assert len(payload['rows']) == 9 and payload['chosen_row'] is None
        assert all(row['reason'] == 'unknown_name' for row in payload['rows'])
        if capacity:
            assert payload['name_cache']['misses'] == 9
        else:
            assert payload['name_cache']['entries'] == 0
        outputs.append(payload)
rows = [[{k: v for k, v in row.items() if k != 'elapsed_ms'} for row in payload['rows']]
        for payload in outputs]
assert rows[0] == rows[1]
target.write_text(json.dumps(dict(passed=True, cli=outputs), ensure_ascii=False, indent=2), encoding='utf-8')
print('CLI cache enabled/disabled passed; production code hashes match full OCR report')
