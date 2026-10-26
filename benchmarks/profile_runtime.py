"""Sequential vs native recognition batching; verify active GPU kernels."""
import argparse
import collections
import hashlib
import json
import time
from pathlib import Path

import cv2
import numpy as np

from benchmark import load_samples,preprocess,parse_number,normalize_name
from engines import make_engine


def batch_read(engine,images,field,batch_size):
    from rapidocr.ch_ppocr_rec.typings import TextRecInput
    model=engine.models['en' if field=='quantity' else 'cyrillic']
    rec=model.text_rec
    rec.rec_batch_num=batch_size
    result=rec(TextRecInput(img=[cv2.cvtColor(im,cv2.COLOR_RGB2BGR) for im in images]))
    return list(result.txts)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--models',type=Path,default=Path(__file__).parent/'models')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--threads',type=int,default=2)
    parser.add_argument('--engines',nargs='+',choices=['rapid_cpu','rapid_dml'],default=['rapid_cpu','rapid_dml'])
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    all_samples,_=load_samples(args.dataset)
    sources=[]
    for group in ('ordinary','hard'):
        sources.append(next(s['screenshot'] for s in all_samples if s['group']==group))
    rows=[]
    for engine_name in args.engines:
        engine=make_engine(engine_name,args.models,args.threads)
        for field in ('name','quantity','buyout'):
            samples=[s for s in all_samples if s['field']==field and s['screenshot'] in sources and s['row']<=9]
            for sample in samples[:3]:
                engine.read(sample['image'],field)
            sequential=[engine.read(preprocess(s['image'],field,'raw'),field)[0] for s in samples]
            for batch in (1,3,9):
                durations=[]
                outputs=[]
                for repeat in range(4):
                    for start in range(0,len(samples),batch):
                        chunk=samples[start:start+batch]
                        t=time.perf_counter()
                        images=[preprocess(s['image'],field,'raw') for s in chunk]
                        text=batch_read(engine,images,field,batch)
                        [normalize_name(x) if field=='name' else parse_number(x,field) for x in text]
                        elapsed=(time.perf_counter()-t)*1000
                        if repeat:
                            durations.append(elapsed)
                        if repeat==3:
                            outputs.extend(text)
                result=dict(engine=engine_name,field=field,batch_size=batch,
                            threads=args.threads,
                            images=len(samples),repeats=3,preprocess='raw',
                            p50_batch_ms=float(np.percentile(durations,50)),
                            p95_batch_ms=float(np.percentile(durations,95)),
                            p50_per_image_ms=float(np.percentile(durations,50))/batch,
                            differing_predictions=sum(a!=b for a,b in zip(sequential,outputs)),
                            sources=sources)
                rows.append(result)
                print(result,flush=True)
        if engine_name=='rapid_dml':
            import onnxruntime as ort
            model=engine.models['cyrillic']
            old=model.text_rec.session.session
            options=ort.SessionOptions()
            options.enable_profiling=True
            options.profile_file_prefix=str(args.output/'dml_nodes')
            options.enable_mem_pattern=False
            options.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL
            options.intra_op_num_threads=args.threads
            session=ort.InferenceSession(old._model_path,sess_options=options,
                                         providers=['DmlExecutionProvider','CPUExecutionProvider'])
            model.text_rec.session.session=session
            engine.read(next(s['image'] for s in all_samples if s['field']=='name'),'name')
            profile=Path(session.end_profiling())
            events=json.loads(profile.read_text(encoding='utf-8'))
            counts=collections.Counter(e.get('args',{}).get('provider') for e in events if e.get('args',{}).get('provider'))
            (args.output/'gpu_proof.json').write_text(json.dumps(dict(providers=session.get_providers(),node_events=dict(counts)),indent=2),encoding='utf-8')
            if not counts['DmlExecutionProvider']:
                raise RuntimeError('No DML node execution found')
        engine.close()
    (args.output/'batch_timings.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    hashes={str(p.relative_to(args.models)):dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),bytes=p.stat().st_size)
            for p in args.models.rglob('*') if p.suffix in ('.onnx','.pth','.traineddata')}
    (args.output/'model_hashes.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
