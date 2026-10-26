"""Offline restart/disk-cache regression; no game input, isolated cache and DBs."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
from PIL import Image
from stalbot.domain import Catalog,Rule,identity_from_text
from stalbot.reader_factory import create_cached_reader
from stalbot.scanner import Scanner


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True,help='New directory outside dataset')
    args=parser.parse_args()
    output=args.output.resolve();dataset=(ROOT/'dataset').resolve()
    if output==dataset or dataset in output.parents:parser.error('Output must be outside dataset')
    output.mkdir(parents=True,exist_ok=False)
    label_path=dataset/'labels/labels.json'
    label_hash=hashlib.sha256(label_path.read_bytes()).hexdigest()
    data=json.loads(label_path.read_text(encoding='utf-8'))
    baseline=json.loads((ROOT/'validation/baseline.json').read_text(encoding='utf-8'))
    assert label_hash==baseline['labels_sha256']
    rows=[];rules={}
    for shot in data['screenshots'].values():
        for row in shot['rows']:
            if row['status']!='labeled':continue
            images={}
            for field in ('name','quantity','buyout'):
                meta=row['crops'][field];path=dataset/meta['path']
                assert hashlib.sha256(path.read_bytes()).hexdigest()==meta['sha256']
                with Image.open(path) as image:images[field]=np.array(image.convert('RGB'))
            rows.append(images)
            identity=identity_from_text(row['name_text'],row['name_color'])
            if identity.valid and not identity.truncated:
                key=(identity.name,identity.color,identity.upgrade)
                rules[key]=Rule(identity.name,5_000_000,10_000_000,identity.color,identity.upgrade)
    assert len(rows)==baseline['rows']
    catalog=Catalog(rules.values())
    frames=[]
    for sid,shot in data['screenshots'].items():
        if sid.startswith(('ae497dd6','cf778c20')):
            path=dataset/shot['source']
            assert hashlib.sha256(path.read_bytes()).hexdigest()==sid
            with Image.open(path) as image:frames.append((sid,np.array(image.convert('RGB'))))
    assert len(frames)==2
    clean=lambda decision:({k:v for k,v in decision.to_dict().items() if k!='elapsed_ms'} if decision else None)
    report={'labels_sha256':label_hash,'rows':len(rows),'devices':{},'passed':False,
            'code_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in [ROOT/'main.py',Path(__file__),*sorted((ROOT/'stalbot').glob('*.py'))]}}
    reference=None
    for device in ('cpu','dml'):
        directory=output/'cache'
        reader=create_cached_reader(ROOT/'benchmarks/models',device,1000,directory)
        raw={};numbers={};result={}
        try:
            assert reader.stats()['entries']==0
            for batch in (1,3,9):
                reader.warmup(batch)
                raw[batch]=[]
                for start in range(0,len(rows),batch):
                    images=[row['name'] for row in rows[start:start+batch]]
                    raw[batch].extend(reader.reader.names(images,batch))
                cached=[]
                for start in range(0,len(rows),batch):
                    cached.extend(reader.names([row['name'] for row in rows[start:start+batch]],batch))
                assert cached==raw[batch]
                if reference is not None:assert raw[batch]==reference[batch]
            for field in ('quantity','buyout'):
                numbers[field]=[reader.number(row[field],field) for row in rows]
            order=list(reader._entries)
            reader.flush_cache(force=True,wait=True)
            result['saved']=reader.stats()
            assert result['saved']['persistence']['saves']>0
            assert result['saved']['persistence']['last_error'] is None
        finally:reader.close()
        if reference is None:reference=raw
        reader=create_cached_reader(ROOT/'benchmarks/models',device,1000,directory)
        try:
            assert list(reader._entries)==order,'LRU order changed on restart'
            result['loaded']=reader.stats()
            assert result['loaded']['persistence']['loaded_entries']==len(order)
            for batch in (1,3,9):reader.warmup(batch)
            with patch.object(reader.reader,'names',side_effect=AssertionError('Loaded exact NAME invoked OCR')):
                for batch in (1,3,9):
                    restored=[]
                    for start in range(0,len(rows),batch):
                        restored.extend(reader.names([row['name'] for row in rows[start:start+batch]],batch))
                    assert restored==raw[batch]
            with patch.object(reader.reader,'number',wraps=reader.reader.number) as numeric:
                for field in ('quantity','buyout'):
                    assert [reader.number(row[field],field) for row in rows]==numbers[field]
                assert numeric.call_count==2*len(rows)
            comparisons=0
            for sid,frame in frames:
                for group in (1,3,9):
                    for metric in ('profit','roi'):
                        for batch in (1,3,9):
                            kw=dict(group_size=group,selection_metric=metric)
                            expected,expected_rows=Scanner(reader.reader).scan(frame,catalog,batch,5_000_000,**kw)
                            actual,actual_rows=Scanner(reader).scan(frame,catalog,batch,5_000_000,**kw)
                            assert clean(actual)==clean(expected),(sid,group,metric,batch)
                            assert [clean(d) for d in actual_rows]==[clean(d) for d in expected_rows]
                            comparisons+=1
            result['scanner_comparisons']=comparisons
            result['after_restart']=reader.stats()
            result['backend']=reader.backend_info()
        finally:reader.close()
        with tempfile.TemporaryDirectory(prefix='stalbot-disk-cli-') as temporary:
            process=subprocess.run([sys.executable,str(ROOT/'main.py'),
                '--replay',str(ROOT/'dataset/templates/Обычный скриншот.png'),
                '--db',str(Path(temporary)/'empty.sqlite3'),'--ocr-device',device,
                '--name-cache-dir',str(directory),'--name-cache-capacity','1000'],
                cwd=ROOT,capture_output=True,encoding='utf-8',timeout=120,
                env={**os.environ,'PYTHONIOENCODING':'utf-8'})
            assert process.returncode==0,process.stderr
            cli=json.loads(process.stdout)
            assert cli['name_cache']['persistence']['loaded_entries']>0
            assert cli['name_cache']['hits']>0
            assert cli['chosen_row'] is None and cli['session_seconds']>0
            result['cli']=cli
        report['devices'][device]=result
        print('Persistent native restart/Scanner/CLI checked:',device,flush=True)
    assert hashlib.sha256(label_path.read_bytes()).hexdigest()==label_hash
    report['passed']=True
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Persistent cache regression passed:',output,flush=True)


if __name__=='__main__':main()
