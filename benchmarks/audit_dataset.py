import argparse
import collections
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

def main():
    parser = argparse.ArgumentParser(description='Read-only crop and label audit')
    parser.add_argument('--dataset', type=Path, default=Path(__file__).resolve().parents[1]/'dataset')
    parser.add_argument('--output', type=Path, default=Path(__file__).parent/'audit')
    args = parser.parse_args()
    ROOT, OUT = args.dataset, args.output
    if ROOT.resolve() == OUT.resolve() or ROOT.resolve() in OUT.resolve().parents:
        parser.error('Audit output must be outside source dataset')
    OUT.mkdir(parents=True, exist_ok=True)
    data = json.loads((ROOT / 'labels/labels.json').read_text(encoding='utf-8'))
    rows = [(sid, s['original_name'], r) for sid, s in data['screenshots'].items()
            for r in s['rows'] if r['status'] == 'labeled']
    seen = collections.defaultdict(list)
    failures = []
    for sid, name, row in rows:
        for field in ('name', 'quantity', 'buyout'):
            label = row['name_text'] if field == 'name' else (row[field], row[field+'_state'])
            seen[field, row['crops'][field]['sha256']].append((name, row['row'], label))
        for crop in row['crops'].values():
            if hashlib.sha256((ROOT / crop['path']).read_bytes()).hexdigest() != crop['sha256']:
                failures.append(crop['path'])
    conflicts = [v for v in seen.values() if len(set(x[2] for x in v)) > 1]
    print('conflicting identical crops:', json.dumps(conflicts, ensure_ascii=True))
    print('hash failures:', failures)
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 16)
    for start in range(0, len(rows), 40):
        sheet = Image.new('RGB', (1440, 1560), '#151515')
        draw = ImageDraw.Draw(sheet)
        for j, (_, name, row) in enumerate(rows[start:start+40]):
            x, y = (j % 2)*720, (j // 2)*78
            with Image.open(ROOT / row['crops']['name']['path']) as im:
                sheet.paste(im.resize((426, 38)), (x, y+18))
            draw.text((x, y), f'{start+j+1}: {name} r{row["row"]}', fill='white', font=font)
            draw.text((x, y+57), row['name_text'], fill='#99ccff', font=font)
        sheet.save(OUT / f'names_{start//40}.png')
        numeric = Image.new('RGB', (1440, 1560), '#151515')
        draw = ImageDraw.Draw(numeric)
        for j, (_, name, row) in enumerate(rows[start:start+40]):
            x, y = (j % 2)*720, (j // 2)*78
            for field, dx in [('quantity', 0), ('buyout', 90)]:
                with Image.open(ROOT / row['crops'][field]['path']) as im:
                    numeric.paste(im.resize((im.width*2, im.height*2)), (x+dx, y+18))
            draw.text((x,y), f'{start+j+1}: {name} r{row["row"]}', fill='white', font=font)
            draw.text((x,y+57), f'QTY {row["quantity_state"]}={row["quantity"]}   BUYOUT {row["buyout_state"]}={row["buyout"]}', fill='#99ccff', font=font)
        numeric.save(OUT/f'numeric_{start//40}.png')
    (OUT/'rows.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')

if __name__ == '__main__':
    main()
