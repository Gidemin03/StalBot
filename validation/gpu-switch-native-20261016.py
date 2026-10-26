"""Offline native device switches, provider proof, CPU regression and CLI smoke."""
from collections import Counter
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
import onnxruntime as ort
from stalbot.vision import Reader,prepare_native_runtime
from stalbot.name_cache import CachedNameReader
from stalbot.ocr_session import OcrSession
from stalbot.domain import identity_from_text

out=ROOT/'validation/gpu-switch-native-20261016-b'
out.mkdir(exist_ok=False)
gpu=json.loads((ROOT/'validation/gpu-switch-dml-integration-20261016.json').read_text(encoding='utf-8'))
cpu=json.loads((ROOT/'validation/name-cache-integration-20261016.json').read_text(encoding='utf-8'))
assert gpu['grouped_mock_replay']==cpu['grouped_mock_replay'],'GPU changes CPU observations/strategies'
assert gpu['mock_replay']==cpu['mock_replay'],'GPU changes CPU replay decisions'
for path,digest in gpu['provenance']['code_sha256'].items():
    assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
labels=ROOT/'dataset/labels/labels.json'
assert hashlib.sha256(labels.read_bytes()).hexdigest()==gpu['labels_sha256']
data=json.loads(labels.read_text(encoding='utf-8'))
images=[];expected=[];grades=[]
for shot in data['screenshots'].values():
    for row in shot['rows']:
        if row['status']!='labeled':continue
        crop=row['crops']['name'];path=ROOT/'dataset'/crop['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==crop['sha256']
        with Image.open(path) as im:images.append(np.array(im.convert('RGB')))
        expected.append(identity_from_text(row['name_text'],row['name_color']))
        grades.append((row['upgrade_state'],row['upgrade']))
prepare_native_runtime()
session=OcrSession(lambda:CachedNameReader(Reader(ROOT/'benchmarks/models')))
native_session=ort.InferenceSession
def profiled(*args,**kwargs):
    if kwargs.get('providers',[None])[0]=='DmlExecutionProvider':
        options=kwargs['sess_options']
        options.enable_profiling=True
        options.profile_file_prefix=str(out/'dml-profile')
    return native_session(*args,**kwargs)

reference=None
switches=[]
try:
    for device in ('cpu','dml','cpu'):
        def check(current):
            batch=9 if device=='dml' else 1
            reader,metadata=current.get_reader(batch,key=device,
                reader_factory=lambda:CachedNameReader(Reader(ROOT/'benchmarks/models',device=device)))
            assert metadata.created and reader.stats()['entries']==0
            info=reader.backend_info()
            assert info['requested_device']==device
            predictions={}
            for size in (1,3,9):
                reader.warmup(size)
                values=[]
                for start in range(0,len(images),size):
                    values.extend(reader.reader.names(images[start:start+size],size))
                # Match the established label contract: NOT_VISIBLE is not an
                # independent oracle for separator-derived upgrade/valid flags.
                assert len(values)==len(expected)
                assert all((a.name,a.truncated,a.color)==(b.name,b.truncated,b.color)
                           and (state!='VALUE' or a.upgrade==grade)
                           for a,b,(state,grade) in zip(values,expected,grades)),(device,size,'baseline label fields differ')
                if reference is not None:assert values==reference[size],(device,size,'CPU/GPU differs')
                predictions[size]=values
            reader.names(images[:9],9)
            entries=reader.stats()['entries']
            again,prepared=current.get_reader(batch,key=device)
            assert again is reader and not prepared.created
            assert again.stats()['entries']==entries
            again.names(images[:9],9)
            result=dict(device=device,preparation=asdict(metadata),backend=info,
                        names_correct={size:len(values) for size,values in predictions.items()},
                        label_non_oracle_differences=[dict(index=i,actual=asdict(a),text_only=asdict(b))
                            for i,(a,b) in enumerate(zip(predictions[1],expected)) if a!=b],
                        reused_cache=again.stats())
            if device=='dml':
                profile=reader.reader.model.text_rec.session.session.end_profiling()
                events=json.loads(Path(profile).read_text(encoding='utf-8'))
                providers=Counter(event.get('args',{}).get('provider') for event in events
                                  if event.get('args',{}).get('provider'))
                assert providers['DmlExecutionProvider']>0,'No DML node execution'
                result['node_events']=dict(providers)
                result['profile']=profile
            return result,predictions
        with patch.object(ort,'InferenceSession',side_effect=profiled):
            result,values=session.submit(check).result(timeout=240)
        if reference is None:reference=values
        switches.append(result)
        print('Native switch checked:',device,flush=True)
finally:
    session.close_async().result(timeout=30)

cli=[]
with tempfile.TemporaryDirectory(prefix='stalbot-gpu-cli-') as directory:
    for device in ('cpu','dml'):
        result=subprocess.run([sys.executable,str(ROOT/'main.py'),
            '--replay',str(ROOT/'dataset/templates/Обычный скриншот.png'),
            '--db',str(Path(directory)/f'{device}.sqlite3'),'--ocr-device',device,
            '--batch','1','--name-cache-capacity','1000'],cwd=ROOT,capture_output=True,
            encoding='utf-8',env={**os.environ,'PYTHONIOENCODING':'utf-8'},timeout=120)
        assert result.returncode==0,result.stderr
        payload=json.loads(result.stdout)
        assert payload['ocr_backend']['requested_device']==device
        assert payload['batch']==(9 if device=='dml' else 1)
        assert len(payload['rows'])==9 and payload['chosen_row'] is None
        cli.append(payload)
clean=lambda rows:[{k:v for k,v in row.items() if k!='elapsed_ms'} for row in rows]
assert clean(cli[0]['rows'])==clean(cli[1]['rows'])
(out/'report.json').write_text(json.dumps(dict(passed=True,cross_device_replays=len(gpu['mock_replay']),
    switches=switches,cli=cli),ensure_ascii=False,indent=2),encoding='utf-8')
print('Native CPU/DML/CPU, DML execution proof and CLI smoke passed',flush=True)
