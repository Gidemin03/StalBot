"""Re-score stored OCR outputs after an explicitly authorized label correction."""
import argparse
import json
from pathlib import Path
from benchmark import load_samples,score,summarize,sha

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    samples,manifest=load_samples(args.dataset)
    by_id={s['id']:s for s in samples}
    rows=[]
    changes=[]
    for line in (args.input/'predictions.jsonl').read_text(encoding='utf-8').splitlines():
        row=json.loads(line)
        s=by_id[row['id']]
        if row['crop_sha256']!=s['crop_sha256']:
            raise ValueError('Crop changed: inference must be rerun')
        if row['truth']!=s['truth']:
            changes.append(dict(id=row['id'],before=row['truth'],after=s['truth']))
        row.update(truth=s['truth'],original_truth=s['original_truth'],truth_state=s['truth_state'],**score(s,row['raw']))
        rows.append(row)
    (args.output/'predictions.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
    (args.output/'summary.json').write_text(json.dumps(summarize(rows),ensure_ascii=False,indent=2),encoding='utf-8')
    metadata=json.loads((args.input/'manifest.json').read_text(encoding='utf-8'))
    metadata['original_manifest']=metadata['manifest']
    metadata['manifest']=manifest
    metadata['regrade_changes']={c['id']:c for c in changes}
    metadata['regrade_scoring_code_sha256']=sha(Path(__file__).parent/'benchmark.py')
    metadata['regrade_policy']='Strict thousands grouping; no arbitrary non-digit stripping; empty QTY stays AMBIGUOUS'
    (args.output/'manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    for p in args.input.glob('*_runtime.json'):
        (args.output/p.name).write_bytes(p.read_bytes())
    print('Regraded',len(rows),'predictions;',len(metadata['regrade_changes']),'corrected labels')

if __name__=='__main__':
    main()
