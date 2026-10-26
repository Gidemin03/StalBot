"""Read-only production-code experiment: fake desktop/clock, no real input."""
import sys, json, tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np

ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT.parents[1]),str(ROOT.parents[1]/'tests')]
from test_search_refresh import Clock, Desktop, Interface, mark
from stalbot.runner import Runner
from stalbot.config import Config
from stalbot.vision import BOXES, STEP

results=[]
for scenario in ('hover_cleared', 'empty_then_old_list'):
    clock=Clock();desktop=Desktop(clock)
    # Synthetic list: one text pixel is the old lot; row six has hover background.
    old=np.zeros_like(desktop.frame);mark(old,'name',1,80)
    if scenario=='hover_cleared':
        desktop.frame=old.copy()
        x1,y1,x2,y2=BOXES['name']
        desktop.frame[y1+5*STEP:y2+5*STEP,x1:x2]=20
    def update(frame,elapsed):
        frame[:]=old
        if elapsed>=.300:mark(frame,'name',1,160)
    desktop.after_search=update
    with tempfile.TemporaryDirectory() as temp, \
         patch('stalbot.runner.time.monotonic',clock.monotonic), \
         patch('stalbot.runner.time.perf_counter',clock.monotonic), \
         patch('stalbot.runner.time.sleep',clock.sleep):
        runner=Runner(Mock(),Mock(),desktop,Interface(),Config(search_stable_ms=20,search_poll_ms=10),temp)
        frame=runner.fresh(lambda:False)
        result=dict(scenario=scenario,result=runner.refresh_trace['result'],
                    returned_after_ms=round((clock.now-desktop.searched_at)*1000,3),
                    actual_list_update_ms=300,returned_old_list=bool(np.array_equal(frame,old)))
        assert result['returned_old_list'] and result['returned_after_ms']<300
        results.append(result)
(ROOT/'reproduction.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps(results,indent=2))
