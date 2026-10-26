"""Offline production-path check. Never captures the desktop or sends input."""
from pathlib import Path
import hashlib
import json
import sys
from time import perf_counter
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from stalbot.balance import balance_crop, read_balance
from stalbot.name_cache import CachedNameReader
from stalbot.vision import Reader


def main():
    output = Path(__file__).parent
    paths = sorted((ROOT/'dataset/templates').glob('*.png'))
    paths += sorted((ROOT/'dataset/hard_cases').glob('*.png'))
    paths += sorted((ROOT/'dataset/screenshots/processed').glob('*.png'))
    sheet = Image.new('RGB', (1000, len(paths)*60), 'white')
    draw = ImageDraw.Draw(sheet)
    rows=[]
    reader = CachedNameReader(Reader(ROOT/'benchmarks/models'))
    try:
        for index, path in enumerate(paths):
            frame = np.array(Image.open(path).convert('RGB'))
            started = perf_counter()
            result = read_balance(reader, frame)
            elapsed = (perf_counter()-started)*1000
            rows.append(dict(path=str(path.relative_to(ROOT)),
                             sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                             state=result.state, value=result.value, raw=result.raw,
                             elapsed_ms=elapsed))
            draw.text((2,index*60+20),f'{index+1:02} {result.value}', fill='black')
            sheet.paste(Image.fromarray(balance_crop(frame)).resize((776,44)), (200,index*60+8))
        # Resetting the prior QTY whitelist is essential when sharing native API.
        reader.reader.api.SetVariable('tessedit_char_whitelist', '0123456789')
        reset_result = read_balance(reader, np.array(Image.open(paths[0]).convert('RGB')))
        assert reset_result == read_balance(reader, np.array(Image.open(paths[0]).convert('RGB')))
        assert reset_result.value == rows[0]['value']
        assert reader.stats()['entries'] == 0
    finally:
        reader.close()
    sheet.save(output/'contact-sheet.png')
    (output/'production-results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'frames':len(rows),'accepted':sum(r['state']=='VALUE' for r in rows),
                      'values':sorted({r['value'] for r in rows if r['state']=='VALUE'}),
                      'min_ms':min(r['elapsed_ms'] for r in rows),
                      'max_ms':max(r['elapsed_ms'] for r in rows),
                      'mean_ms':sum(r['elapsed_ms'] for r in rows)/len(rows)}))


if __name__ == '__main__':
    main()
