"""Offline reproductions and component measurements; all input uses test fakes."""
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
from dataclasses import replace
from unittest.mock import Mock, patch

ROOT=pathlib.Path(__file__).resolve().parents[2]
OUT=pathlib.Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import cv2
import numpy as np
from PIL import Image
from stalbot.config import Config
from stalbot.runner import Runner
from stalbot.interface import Interface
from stalbot.search import FieldMonitor
from stalbot.storage import Store
from stalbot.domain import Catalog
import test_search_refresh as refresh
import test_purchase_options as options
from analyze import stats

def measure(fn,n=25):
    fn()
    values=[]
    for _ in range(n):
        start=time.perf_counter();fn();values.append((time.perf_counter()-start)*1000)
    return stats(values)

def reproductions():
    result={}
    with tempfile.TemporaryDirectory() as tmp:
        clock=refresh.Clock();desktop=refresh.Desktop(clock)
        runner=Runner(Mock(),Mock(),desktop,refresh.Interface(),
                      Config(search_mode='static',search_wait_ms=20,search_stable_ms=20),tmp)
        runner.needs_fresh_search=True
        with patch('stalbot.runner.time.monotonic',clock.monotonic),patch('stalbot.runner.time.sleep',clock.sleep):
            frame=runner.fresh(lambda:False)
        result['static_releases_unknown_barrier_without_change']={
            'returned_frame':frame is not None,'barrier_after':runner.needs_fresh_search,
            'result':runner.refresh_trace['result'],'input_events':desktop.clicked}
        assert frame is not None and not runner.needs_fresh_search
        for enabled in (False,True):
            store=Store(pathlib.Path(tmp)/f'{enabled}.sqlite');store.set_balance(1000)
            original=options.candidate(price=80);changed=options.candidate(price=90)
            scanner=options.Scanner([changed]);desktop=options.Desktop(store,original.row)
            clock=refresh.Clock()
            # A replacement row is already stable before opening its menu.
            # The initial Decision belongs to the previous screen (80); fresh OCR sees 90.
            capture=desktop.capture
            def changed_frame():
                f=capture();refresh.mark(f,'buyout',original.row,90);return f
            desktop.capture=changed_frame
            runner=Runner(scanner,store,desktop,options.Interface(),
                          Config(revalidate_lot=enabled),pathlib.Path(tmp)/str(enabled))
            with patch('stalbot.runner.time.monotonic',clock.monotonic),patch('stalbot.runner.time.sleep',clock.sleep):
                status=runner.purchase(original,Catalog([original.rule]),lambda:False)
            result['replacement_row_revalidate_'+str(enabled)]={
                'result':status,'buy_sent':desktop.sent,'rereads':len(scanner.calls),
                'original_price':80,'current_price':90}
            assert desktop.sent is (not enabled)
    return result

def benchmark():
    cv2.setNumThreads(2)
    frame=np.array(Image.open(ROOT/'dataset/templates/Обычный скриншот.png').convert('RGB'))
    ui=Interface(ROOT/'assets/ui')
    runner=Runner(Mock(),Mock(),Mock(),ui,Config(),OUT)
    assert runner.clean_auction(frame)
    timing={}
    for name,fn in [('auction',lambda:ui.auction(frame)),('any_menu',lambda:ui.any_menu(frame)),
                    ('blocking_modal',lambda:ui.blocking_modal(frame)),('outcome',lambda:ui.outcome(frame)),
                    ('clean_auction',lambda:runner.clean_auction(frame)),
                    ('field_pixels_9',lambda:FieldMonitor(9).pixels(frame))]:
        timing[name]=measure(fn)
    # Fixed saved real purchase frame; lossless PNG work is outside the game.
    index=json.loads((OUT/'timing-index.json').read_text(encoding='utf-8'))
    recent=next(r for r in reversed(index) if r.get('observed_status')=='SUCCESS')
    source=ROOT/'data/diagnostics'/recent['attempt']/'screen.png'
    image=Image.open(source).convert('RGB')
    for level in (6,1):
        buffer=io.BytesIO()
        def encode():
            buffer.seek(0);buffer.truncate();image.save(buffer,format='PNG',compress_level=level)
        timing['png_level_'+str(level)]=dict(measure(encode,12),bytes=len(buffer.getvalue()))
        assert np.array_equal(np.array(image),np.array(Image.open(io.BytesIO(buffer.getvalue()))))
    with tempfile.TemporaryDirectory() as tmp:
        store=Store(pathlib.Path(tmp)/'benchmark.sqlite');store.set_balance(100000)
        counter=iter(range(100000));decision=options.candidate()
        timing['sqlite_balance']=measure(store.balance)
        def begin_finish():
            key=str(next(counter));store.begin_buy(key,decision);store.finish_buy(key,'UNAVAILABLE')
        timing['sqlite_begin_plus_finish_empty_db']=measure(begin_finish)
        timing['sqlite_event']=measure(lambda:store.event('audit',{'sample':True}))
    return {'source':str(source.relative_to(ROOT)), 'timings_ms':timing,
            'note':'Offline components; no screen capture, input, OCR inference, or game-load control. PNG encoding excludes disk writes; temp SQLite has a short history.'}

result={'reproductions':reproductions(),'benchmark':benchmark()}
(OUT/'probes.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=True,indent=2))
