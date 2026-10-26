"""Integration regression on the labeled dataset; no game input, no dataset edits."""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
from datetime import datetime,timezone
from fractions import Fraction

import numpy as np
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stalbot.domain import identity_from_text,Catalog,Rule,normalize
from stalbot.vision import Reader,name_color,crop
from stalbot.interface import Interface
from stalbot.scanner import Scanner


def check_numeric_coverage(output,baseline):
    """Compare against a reviewed baseline for this exact label snapshot."""
    if baseline.get('schema_version')!=1:raise ValueError('Unsupported baseline schema')
    if output['labels_sha256']!=baseline['labels_sha256'] or output['rows']!=baseline['rows']:
        raise ValueError('Baseline belongs to another dataset; select a reviewed baseline explicitly')
    for field,expected in baseline['numeric'].items():
        actual=output['numeric'][field]
        if actual['value_n']!=expected['value_n']:raise AssertionError(f'{field}: VALUE denominator changed')
        for metric in ('value_correct','correct','semantic_correct'):
            if actual[metric]<expected['minimum_'+metric]:
                raise AssertionError(f'{field}: {metric} coverage regression ({actual[metric]} < {expected["minimum_"+metric]})')


class CachedReader:
    """Replay actual OCR observations, without invoking either OCR engine again."""
    def __init__(self,frame,observations):
        self.names_by_crop={crop(frame,'name',r).tobytes():o['identity'] for r,o in enumerate(observations)}
        self.numbers_by_crop={(field,crop(frame,field,r).tobytes()):o[field]
                              for r,o in enumerate(observations) for field in ('quantity','buyout')}

    def names(self,images,batch_size):
        if batch_size not in (1,3,9) or not 1<=len(images)<=batch_size:raise ValueError('Invalid cached OCR batch')
        return [self.names_by_crop[image.tobytes()] for image in images]

    def number(self,image,field):return self.numbers_by_crop[field,image.tobytes()]


def selection_oracle(observations,catalog,max_lot_price,group_size,metric,balance=None):
    """Independent eligibility and exact ranking from recorded OCR fields, not Decisions."""
    if group_size not in (1,3,9) or metric not in ('profit','roi'):raise ValueError('Invalid oracle strategy')
    eligible={}
    for row,o in enumerate(observations):
        identity=o['identity']
        if not identity.valid:continue
        names=set()
        for rule in catalog.rules:
            name=normalize(rule.name)
            if identity.truncated:
                if name[:len(identity.name)]==identity.name:names.add(name)
            elif name==identity.name:names.add(name)
        if len(names)!=1:continue
        matches=[rule for rule in catalog.rules if normalize(rule.name) in names
                 and rule.color in ('*',identity.color)
                 and (rule.upgrade is None or (identity.upgrade is not None and
                     rule.upgrade<=identity.upgrade<=(rule.upgrade if rule.upgrade_max is None else rule.upgrade_max)))]
        if len(matches)!=1:continue
        rule=matches[0];qty=o['quantity'];buyout=o['buyout']
        if qty.state=='VALUE' and type(qty.value) is int and qty.value>=1:q=qty.value
        elif qty.state in ('AMBIGUOUS','ABSENT'):q=1
        else:continue
        if buyout.state!='VALUE' or type(buyout.value) is not int or not 1<=buyout.value<=9_999_999_999:continue
        price=buyout.value
        if (rule.max_stack is not None and q>rule.max_stack) or price>max_lot_price:continue
        if (balance is not None and price>balance) or price>rule.buy_unit*q:continue
        profit=Fraction(rule.sell_unit*q*95,100)-price
        if profit>0:eligible[row]={'profit':profit,'roi':profit/price,'quantity':q}
    for start in range(0,9,group_size):
        end=start+group_size
        candidates=[row for row in range(start,end) if row in eligible]
        if candidates:
            # max keeps the first (upper) row when exact scores are equal.
            return max(candidates,key=lambda row:eligible[row][metric]),end,eligible
    return None,9,eligible


def grouped_replay(frame,observations,catalog,max_lot_price):
    scanner=Scanner(CachedReader(frame,observations));strategies=[]
    for group_size in (1,3,9):
        for metric in ('profit','roi'):
            expected,end,eligible=selection_oracle(observations,catalog,max_lot_price,group_size,metric)
            choices=[];checks=[]
            for batch in (1,3,9):
                chosen,decisions=scanner.scan(frame,catalog,batch,max_lot_price,
                                              group_size=group_size,selection_metric=metric)
                choice=chosen.to_dict() if chosen else None
                if choice:choice.pop('elapsed_ms',None)
                choices.append(choice)
                checks.append({'batch':batch,'chosen_row':chosen.row if chosen else None,
                    'oracle_choice':(chosen.row if chosen else None)==expected,
                    'group_boundary':[d.row for d in decisions]==list(range(end)),
                    'eligibility':all(d.buy==(d.row in eligible) for d in decisions),
                    'eligible_fields':all(d.quantity==observations[d.row]['quantity']
                        and d.buyout==observations[d.row]['buyout']
                        and d.identity==observations[d.row]['identity']
                        and d.effective_quantity==eligible[d.row]['quantity']
                        and Fraction(d.profit)==eligible[d.row]['profit'] for d in decisions if d.buy)})
            strategies.append({'group_size':group_size,'selection_metric':metric,
                'expected_row':expected,'expected_evaluated_rows':end,'checks':checks,
                'batch_agreement':all(choice==choices[0] for choice in choices)})
    return strategies


def check_grouped_replays(replays):
    for replay in replays:
        assert replay['live_group1_agreement'],('Cached observations change live group1 choice',replay['source'])
        assert len(replay['strategies'])==6,'Missing grouped replay strategies'
        assert {(s['group_size'],s['selection_metric']) for s in replay['strategies']}=={
            (g,m) for g in (1,3,9) for m in ('profit','roi')},'Missing grouped replay strategy'
        for strategy in replay['strategies']:
            assert strategy['batch_agreement'],('Grouped OCR batch disagreement',replay['source'],strategy)
            assert [c['batch'] for c in strategy['checks']]==[1,3,9],'Missing grouped OCR batches'
            assert all(c[key] for c in strategy['checks'] for key in
                       ('oracle_choice','group_boundary','eligibility','eligible_fields')),('Grouped selection regression',replay['source'],strategy)


def main():
    p=argparse.ArgumentParser();p.add_argument('--dataset',type=Path,required=True);p.add_argument('--models',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--ocr-device',choices=('cpu','dml'),default='cpu')
    p.add_argument('--baseline',type=Path,default=Path(__file__).resolve().parents[1]/'validation/baseline.json')
    args=p.parse_args()
    output_path=args.output.resolve();dataset_path=args.dataset.resolve()
    if output_path==dataset_path or dataset_path in output_path.parents:p.error('Output must be outside dataset')
    if output_path.exists():p.error('Output already exists; choose a new report filename')
    baseline=json.loads(args.baseline.read_text(encoding='utf-8'))
    label_path=args.dataset/'labels/labels.json';before=hashlib.sha256(label_path.read_bytes()).hexdigest()
    if before!=baseline['labels_sha256']:p.error('Labels differ from baseline; choose a reviewed --baseline for this dataset')
    data=json.loads(label_path.read_text(encoding='utf-8'));rows=[]
    for sid,screenshot in data['screenshots'].items():
        for row in screenshot['rows']:
            if row['status']!='labeled':continue
            images={}
            for field,c in row['crops'].items():
                path=(args.dataset/c['path']).resolve()
                if dataset_path not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest()!=c['sha256']:
                    raise ValueError(f'Invalid crop: {path}')
                with Image.open(path) as im:images[field]=np.array(im.convert('RGB'))
            rows.append((sid,row,images))
    if len(rows)!=baseline['rows']:raise ValueError('Baseline row count mismatch')
    reader=Reader(args.models,device=args.ocr_device);output={'labels_sha256':before,'rows':len(rows),'batches':{},'color':{},'numeric':{},'templates':{}}
    root=Path(__file__).resolve().parents[1]
    output['provenance']={'created_utc':datetime.now(timezone.utc).isoformat(),'baseline':baseline,
        'code_sha256':{str(f.relative_to(root)):hashlib.sha256(f.read_bytes()).hexdigest()
                       for f in [root/'main.py',Path(__file__).resolve(),*sorted((root/'stalbot').glob('*.py'))]},
        'models_sha256':{name:hashlib.sha256((args.models/name).read_bytes()).hexdigest()
                         for name in ('rapid/cyrillic_PP-OCRv5_rec_mobile.onnx','tessdata/rus.traineddata','tessdata/eng.traineddata')}}
    try:
        output['warmup_ms']={}
        from stalbot.name_cache import CachedNameReader
        cached=CachedNameReader(reader,1000)
        output['name_cache']={'capacity':1000,'corpus':{},'scanner':[]}
        name_reference=None
        for batch in (1,3,9):
            output['warmup_ms'][str(batch)]=reader.warmup(batch)
            output['ocr_backend']=reader.backend_info()
            predictions=[];durations=[]
            for start in range(0,len(rows),batch):
                group=rows[start:start+batch];t=time.perf_counter()
                predictions.extend(reader.names([im['name'] for sid,r,im in group],batch))
                durations.append((time.perf_counter()-t)*1000)
            if name_reference is None:name_reference=predictions
            errors=[];grades=[]
            for (sid,row,_),prediction in zip(rows,predictions):
                expected=identity_from_text(row['name_text'],row['name_color'])
                if (prediction.name,prediction.truncated)!=(expected.name,expected.truncated):
                    errors.append({'source':sid,'row':row['row'],'expected':asdict(expected),'actual':asdict(prediction)})
                if row['upgrade_state']=='VALUE' and prediction.upgrade!=row['upgrade']:
                    grades.append({'source':sid,'row':row['row'],'expected':row['upgrade'],'actual':prediction.upgrade})
            output['batches'][str(batch)]={'names_correct':len(rows)-len(errors),'upgrade_errors':grades,'name_errors':errors,
                    'differences_from_batch1':sum(a!=b for a,b in zip(name_reference,predictions)),
                    'total_ms':sum(durations),'p50_batch_ms':float(np.median(durations)),'p95_batch_ms':float(np.percentile(durations,95))}
            print('batch',batch,output['batches'][str(batch)],flush=True)
            cached.clear()
            phases={}
            for phase in ('cold','repeat'):
                actual=[];started=time.perf_counter()
                for start in range(0,len(rows),batch):
                    actual.extend(cached.names([im['name'] for sid,r,im in rows[start:start+batch]],batch))
                phases[phase]={'differences':sum(a!=b for a,b in zip(predictions,actual)),
                               'total_ms':(time.perf_counter()-started)*1000,'stats':cached.stats()}
            output['name_cache']['corpus'][str(batch)]=phases
        colors=Counter();color_errors=[]
        for sid,row,images in rows:
            color=name_color(images['name']);colors[row['name_color']]+=1
            if color!=row['name_color']:color_errors.append({'source':sid,'row':row['row'],'expected':row['name_color'],'actual':color})
        output['color']={'counts':dict(colors),'correct':len(rows)-len(color_errors),'errors':color_errors}
        for field in ('quantity','buyout'):
            accepted=correct=semantic=unsafe=value_n=value_correct=0;errors=[]
            for sid,row,images in rows:
                number=reader.number(images[field],field)
                accepted+=number.state=='VALUE'
                good=(number.value==row[field] if row[field] is not None else number.state==row[field+'_state'])
                correct+=good
                if row[field+'_state']=='VALUE':
                    value_n+=1;value_correct+=good and number.state=='VALUE'
                semantic+=(number.conservative_quantity==row[field]) if field=='quantity' else good
                dangerous=(number.state=='VALUE' and (row[field] is None or (number.value>row[field] if field=='quantity' else number.value<row[field])))
                unsafe+=dangerous
                if not good:errors.append({'source':sid,'row':row['row'],'expected':row[field],'actual':asdict(number)})
            output['numeric'][field]={'accepted':accepted,'correct':correct,'semantic_correct':semantic,
                                     'value_n':value_n,'value_correct':value_correct,'dangerous':unsafe,'errors':errors}
        ui=Interface(Path(__file__).resolve().parents[1]/'assets/ui')
        for path in (args.dataset/'templates').glob('*.png'):
            frame=np.array(Image.open(path).convert('RGB'))
            output['templates'][path.name]={'auction':ui.auction(frame),'menus':[r+1 for r in range(9) if ui.menu(frame,r)],'outcome':ui.outcome(frame)}
        # Replay every supplied source with explicitly artificial pricing. Nothing is saved to the real catalog.
        rules={}
        for _,row,_ in rows:
            identity=identity_from_text(row['name_text'],row['name_color'])
            if identity.valid and not identity.truncated:
                key=(identity.name,identity.color,identity.upgrade)
                rules[key]=Rule(identity.name,5_000_000,10_000_000,identity.color,identity.upgrade)
        catalog=Catalog(rules.values());scanner=Scanner(reader);replays=[];grouped_replays=[]
        for sid,shot in data['screenshots'].items():
            if shot.get('menu_after'):continue
            if hashlib.sha256((args.dataset/shot['source']).read_bytes()).hexdigest()!=sid:
                raise ValueError('Screenshot checksum mismatch')
            frame=np.array(Image.open(args.dataset/shot['source']).convert('RGB'))
            choices=[]
            for batch in (1,3,9):
                chosen,decisions=scanner.scan(frame,catalog,batch,5_000_000)
                choices.append(chosen.to_dict() if chosen else None)
            def comparison(d):
                if d:d=dict(d);d.pop('elapsed_ms',None)
                return d
            replays.append({'source':sid,'chosen_rows':[d['row']+1 if d else None for d in choices],
                            'batch_agreement':all(comparison(d)==comparison(choices[0]) for d in choices)})
            identities=reader.names([crop(frame,'name',r) for r in range(9)],9)
            observations=[{'identity':identity,
                           'quantity':reader.number(crop(frame,'quantity',r),'quantity'),
                           'buyout':reader.number(crop(frame,'buyout',r),'buyout')}
                          for r,identity in enumerate(identities)]
            strategies=grouped_replay(frame,observations,catalog,5_000_000)
            grouped_replays.append({'source':sid,'row_index_base':0,
                'live_group1_agreement':all(s['expected_row']==(choices[0]['row'] if choices[0] else None)
                                            for s in strategies if s['group_size']==1),
                'observations':[{field:asdict(value) for field,value in o.items()} for o in observations],
                'strategies':strategies})
            # Actual production scanner: both readers reread numeric fields.
            # The existing native-only baseline above remains independent of cache.
            for group_size in (1,3,9):
                for metric in ('profit','roi'):
                    for batch in (1,3,9):
                        options=dict(group_size=group_size,selection_metric=metric)
                        expected,expected_rows=scanner.scan(frame,catalog,batch,5_000_000,**options)
                        cached.clear()
                        for phase in ('cold','repeat'):
                            actual,actual_rows=Scanner(cached).scan(frame,catalog,batch,5_000_000,**options)
                            same=(comparison(expected.to_dict() if expected else None)==comparison(actual.to_dict() if actual else None)
                                  and [comparison(d.to_dict()) for d in expected_rows]==[comparison(d.to_dict()) for d in actual_rows])
                            output['name_cache']['scanner'].append(dict(source=sid,group=group_size,metric=metric,
                                batch=batch,phase=phase,equal=same,stats=cached.stats()))
            print('cache scanner checked',sid,flush=True)
        output['mock_replay']=replays
        output['grouped_mock_replay']=grouped_replays
    finally:reader.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    insufficient_source=root/'validation/insufficient-20261015/source.png'
    with Image.open(insufficient_source) as image:
        output['insufficient_template']={'outcome':ui.outcome(np.array(image.convert('RGB'))),
            'source_sha256':hashlib.sha256(insufficient_source.read_bytes()).hexdigest()}
    args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    assert hashlib.sha256(label_path.read_bytes()).hexdigest()==before,'Dataset labels changed'
    check_numeric_coverage(output,baseline)
    assert all(not phase['differences'] for phases in output['name_cache']['corpus'].values() for phase in phases.values()),'Cached NAME differs'
    assert all(check['equal'] for check in output['name_cache']['scanner']),'Cached scanner decision differs'
    assert output['insufficient_template']['outcome']=='INSUFFICIENT','Insufficient funds template regression'
    assert all(b['names_correct']==len(rows) and not b['upgrade_errors'] and not b['differences_from_batch1'] for b in output['batches'].values()),'Name/batch regression'
    assert not output['color']['errors'],'Color errors'
    assert all(not n['dangerous'] for n in output['numeric'].values()),'Dangerous numeric error'
    assert all(r['batch_agreement'] for r in output['mock_replay']),'Batch changes purchase choice'
    check_grouped_replays(output['grouped_mock_replay'])
    expected={
        'Обычный скриншот.png':([],None),'Скриншот с меню покупки.png':([1],None),
        '2 Скриншот с меню покупки.png':([7],None),'3 скриншот с меню покупки.png':([9],None),
        'Скриншот успешной покупки.png':([],'SUCCESS'),
        'Скриншот уведомления о том что предмет выкуплен.png':([],'UNAVAILABLE')}
    for name,(menus,outcome) in expected.items():
        actual=output['templates'][name]
        assert actual['auction'] and actual['menus']==menus and actual['outcome']==outcome,(name,actual)
    print('Integration regression passed:',args.output,flush=True)


if __name__=='__main__':main()
