"""Offline OCR experiment runner. Never writes into the source dataset."""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.metadata
import json
import platform
import re
import time
import unicodedata
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

FIELDS = ('name', 'quantity', 'buyout')
VARIANTS = ('raw', 'scale3', 'otsu3', 'tight3', 'white3', 'content3')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_name(text):
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'\s*\|\s*\+\s*', ' | +', text)


def parse_number(text, field):
    text = unicodedata.normalize('NFKC', text).strip()
    if field == 'buyout' and re.fullmatch(r'[-—–]{2,}', text):
        return 'ABSENT', None
    # Empty OCR is not proof of ABSENT. Reject leftover BID letters rather than
    # deleting arbitrary non-digits and silently joining two different prices.
    suffix = r'(?:\s*(?:рублей|рубля|рубль|руб\.?|₽))?' if field == 'buyout' else ''
    match = re.fullmatch(r'([0-9]+|[0-9]{1,3}(?:\s+[0-9]{3})+)' + suffix, text, flags=re.I)
    if not match:
        return 'AMBIGUOUS', None
    number = int(re.sub(r'\s+', '', match[1]))
    return ('VALUE', number) if number > 0 else ('AMBIGUOUS', None)


def distance(a, b):
    previous = list(range(len(b)+1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1]+1, previous[j]+1, previous[j-1]+(left != right)))
        previous = current
    return previous[-1]


def preprocess(image, field, variant):
    result = image.copy()
    if variant == 'content3':
        if field == 'quantity':
            raise ValueError('Content crop is not defined for item icons')
        mask=result.max(axis=2)>100
        points=np.argwhere(mask)
        if points.size:
            y1,x1=points.min(axis=0)
            y2,x2=points.max(axis=0)+1
            # Include antialiasing margins; never discard a leading component.
            result=result[max(0,y1-2):min(result.shape[0],y2+2),
                          max(0,x1-2):min(result.shape[1],x2+2)]
    if variant == 'white3':
        # Suppress colored icon pixels; do not infer quantity=1 from an empty mask.
        pixels=result.astype(np.int16)
        mask=(pixels.min(axis=2)>=175)&(pixels.max(axis=2)-pixels.min(axis=2)<=55)
        result=np.repeat(np.where(mask,0,255).astype(np.uint8)[:,:,None],3,axis=2)
    if variant == 'tight3':
        # Fixed, documented experimental boxes, not derived from ground truth.
        if field == 'buyout':
            result = result[:, 10:]  # absolute X=1240, preserves maximal price in supplied images
        elif field == 'name':
            result = result[:16, :]  # reduce BID fragments below the name baseline
        else:
            result = result[3:19, :]
    if variant != 'raw':
        result = cv2.resize(result, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    if variant == 'otsu3':
        gray = cv2.cvtColor(result, cv2.COLOR_RGB2GRAY)
        result = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
        result = cv2.cvtColor(result, cv2.COLOR_GRAY2RGB)
    # No padding in the raw baseline. Engines retain their own native preprocessing.
    return np.ascontiguousarray(result)


def load_samples(dataset, overrides=None):
    labels_path = dataset/'labels/labels.json'
    data = json.loads(labels_path.read_text(encoding='utf-8'))
    if data['schema_version'] != 1:
        raise ValueError('Unsupported label schema')
    hard = {sha(p) for p in (dataset/'hard_cases').glob('*.png')}
    corrections = json.loads(overrides.read_text(encoding='utf-8')) if overrides else []
    samples = []
    seen_crops = collections.Counter()
    for sid, screenshot in data['screenshots'].items():
        source = dataset/screenshot['source']
        if sha(source) != sid:
            raise ValueError(f'Source checksum mismatch: {source}')
        for row in screenshot['rows']:
            if row['status'] != 'labeled':
                continue
            for field in FIELDS:
                crop = row['crops'][field]
                path = (dataset/crop['path']).resolve()
                if dataset.resolve() not in path.parents or sha(path) != crop['sha256']:
                    raise ValueError(f'Invalid crop: {path}')
                truth = row['name_text'] if field == 'name' else row[field]
                original = truth
                for correction in corrections:
                    if (correction['source'], correction['row'], correction['field']) == (screenshot['original_name'], row['row'], field):
                        if truth != correction['before']:
                            raise ValueError('Correction no longer matches source label')
                        truth = correction['after']
                with Image.open(path) as im:
                    image = np.array(im.convert('RGB'))
                samples.append(dict(id=f'{sid}:{row["row"]}:{field}', screenshot=sid,
                                    source=screenshot['original_name'], row=row['row'], field=field,
                                    group='hard' if sid in hard else 'ordinary', truth=truth,
                                    original_truth=original, truth_state='VALUE' if field=='name' else row[field+'_state'],
                                    crop_sha256=crop['sha256'], crop_path=str(path), image=image,
                                    name_color=row['name_color'], upgrade=row['upgrade'],
                                    upgrade_state=row['upgrade_state']))
                seen_crops[field, crop['sha256']] += 1
    manifest = dict(labels_sha256=sha(labels_path), screenshots=len(data['screenshots']),
                    rows=len(samples)//3, groups=dict(collections.Counter(s['group'] for s in samples if s['field']=='name')),
                    unique_crops={f:sum(k[0]==f for k in seen_crops) for f in FIELDS},
                    overrides=corrections, source_snapshot=data)
    return samples, manifest


def score(sample, raw):
    result = {}
    truth, field = sample['truth'], sample['field']
    if field == 'name':
        expected, actual = normalize_name(truth), normalize_name(raw)
        result.update(predicted=actual, predicted_state='VALUE' if actual else 'AMBIGUOUS',
                      exact=raw.strip()==truth, correct=actual==expected,
                      original_correct=actual==normalize_name(sample['original_truth']),
                      edit_distance=distance(expected, actual), truth_chars=len(expected),
                      state_correct=bool(actual))
        if sample['upgrade_state']=='VALUE':
            suffix = re.search(r'\|\s*\+(\d+)\s*$', actual)
            result['upgrade_correct'] = bool(suffix and int(suffix[1])==sample['upgrade'])
    else:
        state, number = parse_number(raw, field)
        expected_state = sample['truth_state']
        # QTY ABSENT has semantic value 1, but retain separate state correctness.
        correct = number == truth if truth is not None else state == expected_state
        result.update(predicted=number, predicted_state=state, correct=correct,
                      original_correct=correct, state_correct=state==expected_state,
                      dangerous_overquantity=field=='quantity' and truth is not None and number is not None and number>truth,
                      dangerous_underprice=field=='buyout' and truth is not None and number is not None and number<truth,
                      unsafe_ambiguous_accept=expected_state=='AMBIGUOUS' and state=='VALUE',
                      false_price_gate_5m=field=='buyout' and truth is not None and number is not None and number<=5000000<truth,
                      false_price_gate_20m=field=='buyout' and truth is not None and number is not None and number<=20000000<truth)
    return result


def aggregate(rows):
    timing = [r['elapsed_ms'] for r in rows]
    values = [r for r in rows if r['truth_state']=='VALUE']
    accepted = [r for r in rows if r['predicted_state']=='VALUE']
    result = dict(n=len(rows), correct=sum(r['correct'] for r in rows),
                  accuracy=sum(r['correct'] for r in rows)/len(rows),
                  original_correct=sum(r['original_correct'] for r in rows),
                  value_n=len(values), value_correct=sum(r['correct'] for r in values),
                  accepted_n=len(accepted), accepted_correct=sum(r['correct'] for r in accepted),
                  state_correct=sum(r['state_correct'] for r in rows),
                  p50_ms=float(np.percentile(timing,50)), p95_ms=float(np.percentile(timing,95)))
    for flag in ('dangerous_overquantity','dangerous_underprice','unsafe_ambiguous_accept','false_price_gate_5m','false_price_gate_20m'):
        result[flag] = sum(r.get(flag,False) for r in rows)
    if rows[0]['field']=='name':
        result['cer'] = sum(r['edit_distance'] for r in rows)/max(1,sum(r['truth_chars'] for r in rows))
        upgrades = [r for r in rows if 'upgrade_correct' in r]
        result['upgrade_n'], result['upgrade_correct'] = len(upgrades), sum(r['upgrade_correct'] for r in upgrades)
    else:
        result['by_state'] = {s:dict(n=len(part), correct=sum(r['correct'] for r in part))
                              for s in ('VALUE','ABSENT','AMBIGUOUS')
                              if (part := [r for r in rows if r['truth_state']==s])}
    return result


def summarize(predictions):
    summary = []
    for key in sorted({(r['engine'],r['variant'],r['field']) for r in predictions}):
        base = [r for r in predictions if (r['engine'],r['variant'],r['field'])==key]
        for group in ('all','ordinary','hard'):
            part = [r for r in base if group=='all' or r['group']==group]
            if not part:
                continue
            unique = {r['crop_sha256']:r for r in part}
            summary.append(dict(engine=key[0],variant=key[1],field=key[2],group=group,
                                **aggregate(part), unique_n=len(unique),
                                unique_accuracy=sum(r['correct'] for r in unique.values())/len(unique)))
    return summary


def run(args):
    from engines import make_engine
    from checkpoint import Journal
    args.output.mkdir(parents=True, exist_ok=True)
    samples, manifest = load_samples(args.dataset, args.overrides)
    samples = [s for s in samples if s['field'] in args.fields]
    if args.limit:
        samples = samples[:args.limit]
    metadata = dict(platform=platform.platform(), python=platform.python_version(),
                    packages={p:importlib.metadata.version(p) for p in ('rapidocr','onnxruntime-directml','easyocr','torch','tesserocr','opencv-python-headless')},
                    threads=args.threads, repeats=args.repeats, command=vars(args).copy(), manifest=manifest)
    metadata['code_sha256']={name:sha(Path(__file__).parent/name) for name in ('benchmark.py','engines.py','checkpoint.py')}
    metadata['command'] = {k:str(v) if isinstance(v,Path) else v for k,v in metadata['command'].items()}
    signature=dict(labels=manifest['labels_sha256'],code=metadata['code_sha256'],packages=metadata['packages'],
                   settings={k:v for k,v in metadata['command'].items() if k not in ('resume','output','dataset','models','overrides')},
                   samples=[(s['id'],s['crop_sha256'],s['truth'],s['truth_state']) for s in samples],
                   models={str(p.relative_to(args.models)):sha(p) for p in args.models.rglob('*') if p.suffix in ('.onnx','.pth','.traineddata')})
    signature=json.loads(json.dumps(signature))
    with Journal(args.output,signature,resume=args.resume) as journal:
        if not (args.output/'manifest.json').exists():
            (args.output/'manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        predictions = journal.rows
        for name in args.engines:
            if all(journal.contains(name,v,s['id']) for v in args.variants for s in samples):
                continue
            start = time.perf_counter()
            engine = make_engine(name,args.models,args.threads)
            cold = []
            for field in args.fields:
                examples = [s for s in samples if s['field']==field]
                if not examples:
                    continue
                for sample in examples[:3]:
                    t = time.perf_counter()
                    engine.read(sample['image'],field)
                    cold.append((field,(time.perf_counter()-t)*1000))
            init = dict(engine=name,startup_warmup_ms=(time.perf_counter()-start)*1000,
                        cold_calls=cold,details=engine.details())
            runtime=args.output/f'{name}_runtime.json'
            if runtime.exists():
                runtime=args.output/f'{name}_runtime_resume_{time.time_ns()}.json'
            runtime.write_text(json.dumps(init,indent=2,default=str),encoding='utf-8')
            for variant in args.variants:
                for field in args.fields:
                    selected = [s for s in samples if s['field']==field]
                    for sample in selected:
                        if journal.contains(name,variant,sample['id']):
                            continue
                        elapsed, texts, confidences = [],[],[]
                        for _ in range(args.repeats):
                            t = time.perf_counter()
                            prepared = preprocess(sample['image'],field,variant)
                            raw, confidence = engine.read(prepared,field)
                            normalize_name(raw) if field=='name' else parse_number(raw,field)
                            elapsed.append((time.perf_counter()-t)*1000)
                            texts.append(raw)
                            confidences.append(confidence)
                        scored = score(sample,texts[-1])
                        record = {k:v for k,v in sample.items() if k!='image'}
                        record.update(engine=name,variant=variant,raw=texts[-1],confidence=confidences[-1],
                                      elapsed_ms=float(np.median(elapsed)),elapsed_samples_ms=elapsed,
                                      repeat_consistent=len(set(texts))==1,**scored)
                        journal.append(record)
                    group=[r for r in predictions if (r['engine'],r['variant'],r['field'])==(name,variant,field)]
                    if group:
                        print(name,variant,field,aggregate(group),flush=True)
            engine.close()
    summary = summarize(predictions)
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    if sha(args.dataset/'labels/labels.json') != manifest['labels_sha256']:
        raise RuntimeError('Dataset changed during benchmark; rerun with a stable snapshot')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,default=Path(__file__).resolve().parents[1]/'dataset')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--models',type=Path,default=Path(__file__).parent/'models')
    parser.add_argument('--overrides',type=Path)
    parser.add_argument('--engines',nargs='+',default=['rapid_cpu','tesseract','easy_cpu'])
    parser.add_argument('--fields',nargs='+',choices=FIELDS,default=list(FIELDS))
    parser.add_argument('--variants',nargs='+',choices=VARIANTS,default=list(VARIANTS[:4]))
    parser.add_argument('--threads',type=int,default=2)
    parser.add_argument('--repeats',type=int,default=1)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--resume',action='store_true',help='Continue compatible interrupted run without repeating saved records')
    args=parser.parse_args()
    if args.repeats<1 or args.threads<1:
        parser.error('repeats and threads must be positive')
    if 'content3' in args.variants and 'quantity' in args.fields:
        parser.error('content3 requires --fields name buyout (no icon background cropping)')
    if args.output.resolve()==args.dataset.resolve() or args.dataset.resolve() in args.output.resolve().parents:
        parser.error('Output must be outside source dataset')
    run(args)


if __name__=='__main__':
    main()
