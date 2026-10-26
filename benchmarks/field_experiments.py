"""Second-stage hypotheses. All decisions use pixels, never sample labels."""
import argparse
import importlib.metadata
import json
import re
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from benchmark import load_samples, preprocess, score, sha, summarize, parse_number
from checkpoint import Journal
from engines import make_engine


def components(mask):
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype('uint8'), 8)
    return labels, stats[1:]


def name_line(image):
    """Keep components on the upper name baseline, plus adjacent terminal dots."""
    labels, stats = components(image.max(axis=2)>100)
    primary = [i+1 for i,(x,y,w,h,a) in enumerate(stats) if y<=7 and a>=2]
    right = max((stats[i-1][0]+stats[i-1][2] for i in primary), default=0)
    dots = [i+1 for i,(x,y,w,h,a) in enumerate(stats)
            if w<=4 and h<=4 and a>=2 and y+h<=15 and x<=right+12]
    keep = np.isin(labels,primary+dots).astype('uint8')
    keep = cv2.dilate(keep,np.ones((3,3),'uint8'))
    bg = np.median(image.reshape(-1,3),axis=0).astype('uint8')
    return np.where(keep[:,:,None]>0,image,bg).astype('uint8')


def quantity_presence(image):
    pixels=image.astype('int16')
    mask=(pixels.min(2)>=175)&((pixels.max(2)-pixels.min(2))<=55)
    _,stats=components(mask)
    candidates=[list(map(int,s)) for s in stats
                if 2<=s[2]<=10 and 8<=s[3]<=14 and 3<=s[1]<=7
                and 14<=s[1]+s[3]<=19 and s[4]>=10]
    # Digits are right-aligned. No ground-truth width or numeric value is used.
    present=any(29<=s[0]+s[2]<=35 for s in candidates)
    return present,candidates


def upgrade_region(image):
    cleaned=name_line(image)
    _,stats=components(cleaned.max(2)>100)
    bars=[s for s in stats if s[2]<=3 and s[3]>=10 and s[1]<=5]
    pairs=[]
    for bar in bars:
        for plus in stats:
            x,y,w,h,a=plus
            if 6<=w<=10 and 6<=h<=10 and 3<=y<=7 and 2<=x-(bar[0]+bar[2])<=10:
                pairs.append((int(bar[0]),int(x+w)))
    if len(pairs)!=1:
        return None,dict(separator_candidates=len(pairs))
    left,right=pairs[0]
    return cleaned[:,right+1:],dict(separator_candidates=1,digits_left=right+1,separator_left=left)


def parse_upgrade(raw):
    text=raw.strip()
    # Range is a validation rule from the supplied README, never truncate +152.
    return int(text) if re.fullmatch(r'[0-9]{1,2}',text) and 1<=int(text)<=15 else None


class Experiments:
    def __init__(self,models,stage):
        self.rapid=make_engine('rapid_cpu',models,2)
        self.tess=make_engine('tesseract',models,2)
        self.eng=None
        self.craft=None
        if stage=='name':
            import tesserocr
            self.eng=tesserocr.PyTessBaseAPI(path=str(models/'tessdata'),lang='eng',psm=7)
            self.eng.SetVariable('classify_enable_learning','0')
        if stage=='detector':
            import torch,easyocr
            torch.set_num_threads(2)
            self.craft=easyocr.Reader(['en'],gpu=False,recognizer=False,
                                      model_storage_directory=str(models/'easy'),verbose=False)

    def numeric(self,image,engine):
        if engine=='rapid_en':
            return self.rapid.read(image,'quantity')
        api=self.tess.api
        api.SetPageSegMode(7)
        api.SetVariable('tessedit_char_whitelist','0123456789')
        api.SetImage(Image.fromarray(image))
        return api.GetUTF8Text().strip(),api.MeanTextConf()/100

    def read(self,image,field,engine,variant):
        detail={}
        if field=='name':
            im=name_line(image) if variant=='line3' else image[:,:200]
            im=preprocess(im,'name','content3')
            if engine=='rapid_cpu':
                raw,conf=self.rapid.read(im,'name')
            elif engine=='rapid_en':
                raw,conf=self.rapid.read(im,'quantity')
            elif engine=='tesseract':
                raw,conf=self.tess.read(im,'name')
            else:
                self.eng.SetImage(Image.fromarray(im))
                raw,conf=self.eng.GetUTF8Text().strip(),self.eng.MeanTextConf()/100
        elif field=='upgrade':
            region,detail=upgrade_region(image)
            if region is None:
                return '',0.0,detail
            im=preprocess(region,'name','content3')
            raw,conf=self.numeric(im,engine)
            detail['upgrade_value']=parse_upgrade(raw)
        elif field=='quantity':
            if variant.startswith('craft'):
                im=preprocess(image,'quantity','scale3')
                horizontal,free=self.craft.detect(im,min_size=3,text_threshold=0.7,low_text=0.4)
                present=bool(horizontal[0] or free[0])
                detail=dict(presence=present,boxes=horizontal[0],polygons=free[0])
            else:
                present,stats=quantity_presence(image)
                detail=dict(presence=present,components=stats)
            if not present:
                return '',0.0,detail
            v='white3' if engine=='tesseract' else 'scale3'
            im=preprocess(image,'quantity',v)
            raw,conf=(self.tess if engine=='tesseract' else self.rapid).read(im,'quantity')
        else:
            im=preprocess(image,'buyout','otsu3')
            if variant=='digits_full':
                raw,conf=self.numeric(im,engine)
            else:
                # OCR locates currency; do not infer a number boundary from labels.
                import tesserocr
                api=self.tess.api
                api.SetPageSegMode(7)
                api.SetVariable('tessedit_char_whitelist','')
                api.SetImage(Image.fromarray(im))
                full=api.GetUTF8Text().strip()
                iterator=api.GetIterator()
                words=[]
                if iterator:
                    while True:
                        text=iterator.GetUTF8Text(tesserocr.RIL.WORD)
                        box=iterator.BoundingBox(tesserocr.RIL.WORD)
                        if text and box:
                            words.append((text.strip(),list(box)))
                        if not iterator.Next(tesserocr.RIL.WORD):break
                currency=[box for text,box in words if re.fullmatch(r'рублей|рубля|рубль',text,re.I)]
                detail=dict(locator_raw=full,words=words,currency_candidates=len(currency))
                if len(currency)!=1 or currency[0][0]<=3:
                    return '',0.0,detail
                # Preserve every pixel to the left, including potential BID contamination.
                roi=im[:,:currency[0][0]]
                if variant=='currency_strict' and parse_number(full,'buyout')[0]!='VALUE':
                    return '',0.0,detail
                raw,conf=self.numeric(roi,engine)
        return raw,conf,detail

    def close(self):
        self.rapid.close()
        self.tess.close()
        if self.eng:self.eng.End()


CONFIGS={
    'name': [('name',e,v) for e in ('rapid_cpu','tesseract') for v in ('line3','right200')]
            + [('name',e,'line3') for e in ('rapid_en','tesseract_eng')],
    'upgrade': [('upgrade',e,'separator_digits') for e in ('rapid_en','tesseract')],
    'quantity': [('quantity',e,'geometry_gate') for e in ('rapid_cpu','tesseract')],
    'detector': [('quantity',e,'craft_gate') for e in ('rapid_cpu','tesseract')],
    'price': [('buyout',e,v) for e in ('rapid_en','tesseract')
              for v in ('digits_full','currency_crop','currency_strict')],
}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--models',type=Path,default=Path(__file__).parent/'models')
    p.add_argument('--stage',choices=CONFIGS,required=True)
    p.add_argument('--resume',action='store_true')
    args=p.parse_args()
    if args.output.resolve()==args.dataset.resolve() or args.dataset.resolve() in args.output.resolve().parents:
        p.error('Output must be outside dataset')
    all_samples,manifest=load_samples(args.dataset)
    e=Experiments(args.models,args.stage)
    signature=dict(stage=args.stage,labels=manifest['labels_sha256'],configs=CONFIGS[args.stage],
                   code={n:sha(Path(__file__).parent/n) for n in ('field_experiments.py','benchmark.py','engines.py','checkpoint.py')},
                   models={str(f.relative_to(args.models)):sha(f) for f in args.models.rglob('*') if f.suffix in ('.onnx','.pth','.traineddata')},
                   packages={n:importlib.metadata.version(n) for n in ('numpy','opencv-python-headless','rapidocr','onnxruntime-directml','tesserocr','easyocr','torch')})
    signature=json.loads(json.dumps(signature))
    try:
        with Journal(args.output,signature,args.resume) as journal:
            if not (args.output/'manifest.json').exists():
                (args.output/'manifest.json').write_text(json.dumps(dict(manifest=manifest,signature=signature),ensure_ascii=False,indent=2),encoding='utf-8')
            for field,engine,variant in CONFIGS[args.stage]:
                source_field='name' if field=='upgrade' else field
                selected=[s for s in all_samples if s['field']==source_field]
                # Warm engines separately from timed inference and disk journaling.
                for s in selected[:2]:e.read(s['image'],field,engine,variant)
                for s in selected:
                    ident=s['id'] if field!='upgrade' else s['id'].rsplit(':',1)[0]+':upgrade'
                    if journal.contains(engine,variant,ident):continue
                    start=time.perf_counter()
                    raw,confidence,detail=e.read(s['image'],field,engine,variant)
                    value=parse_upgrade(raw) if field=='upgrade' else (parse_number(raw,field) if field!='name' else raw)
                    elapsed=(time.perf_counter()-start)*1000
                    detail=json.loads(json.dumps(detail,default=lambda value:value.tolist()))
                    record={k:v for k,v in s.items() if k!='image'}
                    if field=='upgrade':
                        truth=s['upgrade'] if s['upgrade_state']=='VALUE' else None
                        scored=dict(truth=truth,truth_state=s['upgrade_state'],predicted=value,
                                    predicted_state='VALUE' if value is not None else 'AMBIGUOUS',
                                    correct=value==truth if truth is not None else value is None,
                                    false_upgrade=value is not None and value!=truth)
                    else:
                        scored=score(s,raw)
                    record.update(scored)
                    record.update(id=ident,field=field,engine=engine,variant=variant,raw=raw,
                                  confidence=float(confidence),elapsed_ms=elapsed,detail=detail)
                    if field=='quantity':
                        record['effective_quantity']=record['predicted'] if record['predicted'] is not None else 1
                        record['effective_correct']=record['effective_quantity']==s['truth']
                    journal.append(record)
                group=[r for r in journal.rows if (r['engine'],r['variant'],r['field'])==(engine,variant,field)]
                print(field,engine,variant,'correct',sum(r['correct'] for r in group),'/',len(group),flush=True)
            standard=[r for r in journal.rows if r['field']!='upgrade']
            (args.output/'summary.json').write_text(json.dumps(summarize(standard) if standard else [],ensure_ascii=False,indent=2),encoding='utf-8')
    finally:e.close()
    if sha(args.dataset/'labels/labels.json')!=manifest['labels_sha256']:
        raise RuntimeError('Labels changed during experiment')


if __name__=='__main__':main()
