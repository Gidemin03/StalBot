"""Measured CPU candidate. Produces observations, not trading decisions."""
import argparse
import json
import re
import time
from pathlib import Path

import numpy as np

from benchmark import load_samples,parse_number,preprocess,score,sha,summarize
from checkpoint import Journal
from field_experiments import Experiments,parse_upgrade


class CandidateReader:
    def __init__(self,models,price_fallback=False):
        self.engines=Experiments(models,'quantity')
        self.price_fallback=price_fallback

    def read(self,image,field):
        if field=='name':
            raw,confidence,detail=self.engines.read(image,field,'rapid_cpu','line3')
            match=re.search(r'\|\s*\+\s*([0-9]+)\s*$',raw)
            detail['upgrade_value']=parse_upgrade(match[1]) if match else None
            # None means not read; do not infer +0 from missing OCR output.
            return raw,confidence,detail
        if field=='quantity':
            return self.engines.read(image,field,'tesseract','geometry_gate')
        import tesserocr
        im=preprocess(image,'buyout','otsu3')
        raw,confidence=self.engines.tess.read(im,'buyout')
        state,value=parse_number(raw,'buyout')
        detail=dict(primary_raw=raw,primary_state=state,fallback_used=False)
        if state!='AMBIGUOUS' or not self.price_fallback:return raw,confidence,detail
        iterator=self.engines.tess.api.GetIterator()
        boxes=[]
        if iterator:
            while True:
                word=iterator.GetUTF8Text(tesserocr.RIL.WORD)
                box=iterator.BoundingBox(tesserocr.RIL.WORD)
                if word and box and re.fullmatch(r'рублей|рубля|рубль',word.strip(),re.I):
                    boxes.append(list(box))
                if not iterator.Next(tesserocr.RIL.WORD):break
        detail['currency_candidates']=len(boxes)
        if len(boxes)!=1 or boxes[0][0]<=3:return raw,confidence,detail
        detail.update(fallback_used=True,currency_box=boxes[0])
        extra,extra_confidence=self.engines.rapid.read(im[:,:boxes[0][0]],'quantity')
        detail['fallback_raw']=extra
        if parse_number(extra,'buyout')[0]=='VALUE':return extra,extra_confidence,detail
        return raw,confidence,detail

    def close(self):self.engines.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--models',type=Path,default=Path(__file__).parent/'models')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--price-fallback',action='store_true',help='Experimental extra pass; disabled by default')
    args=p.parse_args()
    if args.output.resolve()==args.dataset.resolve() or args.dataset.resolve() in args.output.resolve().parents:
        p.error('Output must be outside dataset')
    samples,manifest=load_samples(args.dataset)
    grouped={}
    for s in samples:grouped.setdefault((s['screenshot'],s['row']),[]).append(s)
    signature=dict(labels=manifest['labels_sha256'],price_fallback=args.price_fallback,
                   code={n:sha(Path(__file__).parent/n) for n in ('candidate_pipeline.py','field_experiments.py','checkpoint.py','benchmark.py','engines.py')},
                   models={str(f.relative_to(args.models)):sha(f) for f in args.models.rglob('*') if f.suffix in ('.onnx','.pth','.traineddata')})
    variants=dict(name='line3',quantity='geometry_gate',buyout='currency_fallback' if args.price_fallback else 'otsu3')
    reader=CandidateReader(args.models,args.price_fallback)
    try:
        for field in variants:
            for s in [s for s in samples if s['field']==field][:2]:reader.read(s['image'],field)
        with Journal(args.output,signature,args.resume) as journal:
            if not (args.output/'manifest.json').exists():
                (args.output/'manifest.json').write_text(json.dumps(dict(manifest=manifest,signature=signature),ensure_ascii=False,indent=2),encoding='utf-8')
            for group in grouped.values():
                pending=[s for s in group if not journal.contains('candidate_cpu',variants[s['field']],s['id'])]
                if not pending:continue
                observations=[]
                start=time.perf_counter()
                for s in pending:
                    t=time.perf_counter()
                    raw,confidence,detail=reader.read(s['image'],s['field'])
                    parse_number(raw,s['field']) if s['field']!='name' else None
                    observations.append((s,raw,confidence,detail,(time.perf_counter()-t)*1000))
                lot_ms=(time.perf_counter()-start)*1000
                for s,raw,confidence,detail,elapsed in observations:
                    r={k:v for k,v in s.items() if k!='image'}
                    r.update(score(s,raw))
                    r.update(engine='candidate_cpu',variant=variants[s['field']],raw=raw,confidence=float(confidence),
                             elapsed_ms=elapsed,detail=detail,lot_ms=lot_ms if len(pending)==3 else None)
                    if s['field']=='quantity':
                        r['effective_quantity']=r['predicted'] if r['predicted'] is not None else 1
                        r['effective_correct']=r['effective_quantity']==s['truth']
                    journal.append(r)
            summary=summarize(journal.rows)
            (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
            timings=[r['lot_ms'] for r in journal.rows if r['field']=='name' and r['lot_ms'] is not None]
            (args.output/'lot_timings.json').write_text(json.dumps(dict(n=len(timings),p50_ms=float(np.percentile(timings,50)),p95_ms=float(np.percentile(timings,95)),
                scope='Three ROI reads sequentially; warm models; excludes capture, disk, catalog and trading actions'),indent=2),encoding='utf-8')
            print([(r['field'],r['correct'],r['accepted_n']) for r in summary if r['group']=='all'])
    finally:reader.close()
    if sha(args.dataset/'labels/labels.json')!=manifest['labels_sha256']:
        raise RuntimeError('Labels changed during run')


if __name__=='__main__':main()
