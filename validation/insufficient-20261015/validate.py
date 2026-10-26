"""Reproducible read-only image checks; writes a new report, never replaces one."""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from stalbot.interface import Interface

folder = Path(__file__).resolve().parent
ui = Interface(ROOT / 'assets/ui')
source = folder / 'source.png'
asset = ROOT / 'assets/ui/insufficient.png'
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
report = {'source': {'file': 'source.png', 'size': list(Image.open(source).size),
                     'sha256': digest(source)},
          'template': {'file': 'assets/ui/insufficient.png',
                       'box': [802, 506, 1120, 530], 'sha256': digest(asset)},
          'threshold': .95, 'roi': [730, 430, 1190, 650],
          'screenshots': {}}
for path in [source, *sorted((ROOT / 'dataset/templates').glob('*.png'))]:
    frame = np.array(Image.open(path).convert('RGB'))
    report['screenshots'][path.name] = {
        'sha256': digest(path), 'outcome': ui.outcome(frame),
        'scores': {name: ui.score(frame, name, (730, 430, 1190, 650))
                   for name in ('success', 'unavailable', 'insufficient')}}
with (folder / 'report.json').open('x', encoding='utf-8') as output:
    json.dump(report, output, ensure_ascii=False, indent=2)
print(json.dumps(report, ensure_ascii=False, indent=2))
