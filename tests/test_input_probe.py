"""Probe actual runner checks using temporary state and a desktop that forbids BUY."""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

import numpy as np
from PIL import Image

from stalbot.config import Config
from stalbot.domain import Catalog,Identity,Number,Rule
from stalbot.runner import Runner
from stalbot.scanner import ScanCancelled,Scanner
from stalbot.storage import Store
from stalbot.vision import BOXES,STEP


class FakeClock:
    def __init__(self):self.now=0;self.after_sleep=lambda:None
    def monotonic(self):return self.now
    def sleep(self,seconds):self.now+=seconds;self.after_sleep()


class Reader:
    def names(self,images,batch_size):return [Identity('Mock',upgrade=0) for image in images]
    def number(self,image,field):
        return Number('VALUE',1 if field=='quantity' else 90-10*int(image[0,0,0]))


class ProbeDesktop:
    """The only permitted input is Search, followed by opening the chosen row."""
    def __init__(self):
        self.frame=np.zeros((1080,1920,3),np.uint8)
        for row in range(9):self.frame[393+STEP*row:430+STEP*row]=row
        self.clicked=[];self.captures=0;self.menu_row=None;self.exclusive_calls=0
        self.scanned=False;self.menu_captures=0
        self.on_capture=lambda frame:None
    def exclusive(self):self.exclusive_calls+=1
    def move(self,position):pass
    def capture(self):
        self.captures+=1
        if self.menu_row is not None:self.menu_captures+=1
        frame=self.frame.copy()
        x,y,_,_=BOXES['name'];frame[y,x+10]=200  # visible synthetic name
        frame[0,0,0]=self.menu_row+1 if self.menu_row is not None else 0
        self.on_capture(frame)
        return frame
    def click(self,position):
        if not self.clicked:
            assert position==(1342,345),'First input must be Search'
            # Simulate the server replacing the first name after Search.
            x,y,_,_=BOXES['name'];self.frame[y,x]=7
        elif position==(1342,345):
            self.menu_row=None
        else:
            assert len(self.clicked)==1,'No input is permitted after opening the menu'
            assert position[0]==1030 and (position[1]-405)%STEP==0,'BUY is forbidden'
            self.menu_row=(position[1]-405)//STEP
        self.clicked.append(position)


class ProbeInterface:
    def auction(self,frame):return True
    def any_menu(self,frame):return bool(frame[0,0,0])
    def outcome(self,frame):return None
    def menu(self,frame,row):return frame[0,0,0]==row+1


class InputProbeTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.store=Store(self.root/'test.sqlite3')
        self.store.save_rule(Rule('Mock',100,200));self.store.set_balance(1000)
        self.desktop=ProbeDesktop();self.events=[]
        self.runner=Runner(Scanner(Reader()),self.store,self.desktop,ProbeInterface(),
            Config(group_size=3,selection_metric='roi',search_wait_ms=20,settle_timeout_ms=200),
            self.root/'diagnostics',self.events.append)
        scan=self.runner.scanner.scan
        def mark_scanned(*args,**kwargs):
            result=scan(*args,**kwargs);self.desktop.scanned=True;return result
        self.runner.scanner.scan=mark_scanned
        self.stop=Mock(wraps=threading.Event());self.stop.wait.return_value=False
        self.pause=threading.Event();self.clock=FakeClock()
        self.addCleanup(patch.stopall)
        patch('stalbot.runner.time.monotonic',self.clock.monotonic).start()
        patch('stalbot.runner.time.sleep',self.clock.sleep).start()

    def run_probe(self):self.runner.run(False,self.stop,self.pause,probe=True)

    def assert_no_purchase(self):
        self.assertEqual(self.store.history(),[])
        self.assertEqual(self.store.balance(),1000)

    def logged(self):
        with self.store.connect() as db:
            return [(row['mode'],json.loads(row['payload'])) for row in db.execute("SELECT * FROM events WHERE mode!='purchase_timing' ORDER BY id")]

    def change_buyout(self,frame,row=2):
        x,y,_,_=BOXES['buyout'];frame[y+STEP*row,x]=9

    def test_one_search_one_group_winner_menu_and_no_buy_or_budget_write(self):
        self.store.begin_buy=Mock(wraps=self.store.begin_buy)
        self.store.finish_buy=Mock(wraps=self.store.finish_buy)
        self.run_probe()
        self.assertEqual(self.desktop.clicked,[(1342,345),(1030,405+2*STEP)])
        self.assertEqual(self.desktop.exclusive_calls,1)
        self.assertEqual(self.desktop.menu_row,2)
        self.store.begin_buy.assert_not_called();self.store.finish_buy.assert_not_called()
        self.assert_no_purchase()
        selection=next(event for event in self.events if 'timings' in event)
        self.assertEqual((selection['mode'],selection['chosen_row']),('probe',3))
        self.assertEqual([row['row'] for row in selection['rows']],[0,1,2])
        final=next(event for event in self.events if event.get('result')=='PROBE')
        self.assertEqual(final['result'],'PROBE');self.assertIn('BUY не отправлен',final['status'])
        directory=Path(final['diagnostic'])
        self.assertTrue(directory.name.startswith('probe-'))
        self.assertEqual(directory.parent,self.root/'diagnostics')
        detail=json.loads((directory/'detail.json').read_text(encoding='utf-8'))
        self.assertEqual((detail['status'],detail['decision']['row']),('PROBE',2))
        with Image.open(directory/'screen.png') as screen:
            self.assertEqual(screen.size,(1920,1080));self.assertEqual(screen.getpixel((0,0)),(3,0,0))
        self.assertEqual(self.logged(),[('probe',final)])

    def test_no_candidate_finishes_after_one_search(self):
        rule=self.store.rules()[0]
        self.store.save_rule(Rule('Mock',1,200,id=rule.id))
        self.run_probe()
        self.assertEqual(self.desktop.clicked,[(1342,345)])
        self.assert_no_purchase()
        self.assertEqual(self.logged()[0][1]['result'],'NO_CANDIDATE')

    def test_live_probe_combination_rejected_before_input(self):
        with self.assertRaisesRegex(ValueError,'несовместима'):
            self.runner.run(True,self.stop,self.pause,probe=True)
        self.assertEqual((self.desktop.clicked,self.desktop.captures,self.desktop.exclusive_calls),([],0,0))

    def test_empty_catalog_rejected_before_input(self):
        self.store.delete_rule(self.store.rules()[0].id)
        with self.assertRaisesRegex(ValueError,'правила'):self.run_probe()
        self.assertEqual(self.desktop.clicked,[]);self.assertEqual(self.desktop.captures,0)

    def test_unresolved_purchase_blocks_probe_before_input(self):
        catalog=Catalog(self.store.rules())
        decision=self.runner.scanner.row(self.desktop.frame,0,catalog,10000,1000)
        self.store.begin_buy('prior-attempt',decision)
        with self.assertRaisesRegex(RuntimeError,'незавершённую'):self.run_probe()
        self.assertEqual(self.desktop.clicked,[]);self.assertEqual(self.desktop.captures,0)
        self.assertEqual(self.store.history()[0]['status'],'BUY_SENT')

    def test_preselection_frame_is_not_reread(self):
        self.desktop.on_capture=lambda frame:self.change_buyout(frame) if self.desktop.scanned and self.desktop.menu_row is None else None
        self.run_probe()
        self.assertEqual(self.desktop.clicked,[(1342,345),(1030,479)])
        self.assert_no_purchase();self.assertEqual(self.logged()[0][1]['result'],'PROBE')

    def test_changed_row_in_open_menu_fails_without_buy(self):
        self.desktop.on_capture=lambda frame:self.change_buyout(frame) if self.desktop.menu_captures>=1 else None
        self.run_probe()
        self.assertEqual(self.desktop.clicked,[(1342,345),(1030,479),(1342,345)]);self.assert_no_purchase()
        self.assertEqual(self.logged()[0][1]['result'],'STALE')

    def test_changed_immediate_frame_fails_without_buy(self):
        original=self.runner.scanner.row;ocr_done=[]
        def row(*args,**kwargs):
            result=original(*args,**kwargs)
            if self.desktop.menu_row is not None:ocr_done.append(True)
            return result
        self.runner.scanner.row=row
        self.desktop.on_capture=lambda frame:self.change_buyout(frame) if ocr_done else None
        self.run_probe()
        self.assertEqual(self.desktop.clicked,[(1342,345),(1030,479),(1342,345)]);self.assert_no_purchase()
        self.assertEqual(self.logged()[0][1]['result'],'STALE')

    def test_pause_after_last_capture_finishes_without_probe_write_or_more_input(self):
        self.desktop.on_capture=lambda frame:self.pause.set() if self.desktop.menu_captures==2 else None
        self.run_probe()
        self.assertTrue(self.pause.is_set());self.assertEqual(self.desktop.menu_captures,2)
        self.assertEqual(len(self.desktop.clicked),2);self.assert_no_purchase()
        self.assertEqual(self.logged(),[]);self.assertFalse(list((self.root/'diagnostics').glob('probe-*')))

    def test_cancel_before_search_sends_no_input(self):
        with self.assertRaises(ScanCancelled):self.runner.fresh(lambda:True)
        self.assertEqual(self.desktop.clicked,[])

    def test_cancel_while_waiting_after_search_does_not_retry(self):
        cancelled_at=[]
        def pause_on_poll():
            if self.desktop.clicked:
                cancelled_at.append(self.desktop.captures);self.pause.set()
        self.clock.after_sleep=pause_on_poll
        self.run_probe()
        self.assertEqual(self.desktop.clicked,[(1342,345)])
        self.assertEqual(self.desktop.captures,cancelled_at[0]);self.assert_no_purchase()

    def test_diagnostic_failure_cannot_reach_buy(self):
        self.runner.diagnostic=Mock(side_effect=OSError('disk unavailable'))
        with self.assertRaisesRegex(OSError,'disk unavailable'):self.run_probe()
        self.assertEqual(len(self.desktop.clicked),2);self.assert_no_purchase()

    def test_fresh_stability_ignores_pixels_outside_lot_fields(self):
        self.desktop.on_capture=lambda frame:frame.__setitem__((200,200),self.desktop.captures)
        frame=self.runner.fresh(lambda:False)
        self.assertGreaterEqual(self.desktop.captures,3)
        self.assertEqual(frame[200,200,0],self.desktop.captures)
        self.assertGreaterEqual(self.clock.now,self.runner.config.search_stable_ms/1000)
        self.assertEqual(self.desktop.clicked,[(1342,345)])

    def test_fresh_changing_lot_times_out_without_another_search(self):
        x,y,_,_=BOXES['buyout']
        self.desktop.on_capture=lambda frame:frame.__setitem__((y,x),self.desktop.captures) if self.desktop.clicked else None
        self.assertIsNone(self.runner.fresh(lambda:False))
        self.assertGreaterEqual(self.clock.now,.4)
        self.assertEqual(self.desktop.clicked,[(1342,345)]);self.assert_no_purchase()

    def test_delayed_refresh_old_stable_frames_are_not_freshness_proof(self):
        # Even when old frames stay stable beyond the initial wait, do not
        # accept them. Test several response latencies, not a fixed sleep.
        x,y,_,_=BOXES['name']
        for wait_ms,latency in ((100,.18),(200,.6),(200,1.2)):
            with self.subTest(wait_ms=wait_ms,latency=latency):
                self.clock.now=0;self.desktop=ProbeDesktop()
                self.runner.desktop=self.desktop
                self.runner.config=replace(self.runner.config,search_wait_ms=wait_ms,settle_timeout_ms=1800)
                clicked_at=[];captures=[]
                click=self.desktop.click
                def delayed_click(position):
                    click(position);self.clock.sleep(.08)
                    clicked_at.append(self.clock.now)
                self.desktop.click=delayed_click
                def delayed_frame(frame):
                    if clicked_at:
                        since_click=self.clock.now-clicked_at[0]
                        captures.append(since_click)
                        frame[y,x]=7 if since_click>=latency else 0
                self.desktop.on_capture=delayed_frame
                frame=self.runner.fresh(lambda:False)
                self.assertEqual(int(frame[y,x,0]),7)
                self.assertGreaterEqual(captures[-1],latency)
                # Changed mode starts polling immediately; search_wait_ms belongs
                # to static mode and must not delay a fast server response.
                self.assertLess(captures[0],wait_ms/1000)
                self.assertEqual(self.desktop.clicked,[(1342,345)])
                self.assert_no_purchase()

    def test_unchanged_or_transient_then_original_list_never_reaches_ocr(self):
        x,y,_,_=BOXES['name']
        for transient in (False,True):
            with self.subTest(transient=transient):
                self.clock.now=0;self.desktop=ProbeDesktop();self.runner.desktop=self.desktop
                after_click=[]
                def original(frame):
                    if self.desktop.clicked:after_click.append(True)
                    frame[y,x]=9 if transient and len(after_click)==1 else 0
                self.desktop.on_capture=original
                self.runner.scanner=Mock()
                self.run_probe()
                self.runner.scanner.scan.assert_not_called()
                self.assertEqual(self.desktop.clicked,[(1342,345)]);self.assert_no_purchase()
                self.assertEqual(self.runner.refresh_trace['result'],'UNSTABLE' if transient else 'UNCHANGED')
                if not transient:self.assertEqual(self.logged()[-1][1]['result'],'NO_CHANGE')

    def test_probe_saves_exact_ocr_frame_even_without_candidate(self):
        rule=self.store.rules()[0];self.store.save_rule(Rule('Mock',1,200,id=rule.id))
        original=self.runner.scanner.scan;seen=[]
        def scan(frame,*args,**kwargs):
            seen.append(frame.copy());return original(frame,*args,**kwargs)
        self.runner.scanner.scan=scan
        self.run_probe()
        path=Path(self.logged()[0][1]['search_diagnostic'])
        with Image.open(path/'screen.png') as image:self.assertTrue(np.array_equal(np.array(image),seen[0]))
        with Image.open(path/'before_search.png') as image:self.assertFalse(np.array_equal(np.array(image),seen[0]))
        detail=json.loads((path/'detail.json').read_text(encoding='utf-8'))
        self.assertEqual(detail['result'],'CHANGED_STABLE')
        self.assertTrue(detail['samples'][-1]['changed']);self.assertTrue(detail['samples'][-1]['stable'])

    def test_static_wait_captures_once_after_delay_even_if_list_is_unchanged(self):
        self.runner.config=replace(self.runner.config,search_mode='static',search_wait_ms=350)
        x,y,_,_=BOXES['name']
        self.desktop.on_capture=lambda frame:frame.__setitem__((y,x),0)
        self.runner.fresh(lambda:False)
        self.assertEqual(self.runner.search_timings['captures'],1)
        self.assertAlmostEqual(self.clock.now,.350+self.runner.search_timings['prepare_ms']/1000)
        self.assertEqual(self.runner.refresh_trace['result'],'STATIC_DELAY')
        self.assertEqual(self.desktop.clicked,[(1342,345)])

    def test_static_wait_rejects_menu_and_does_not_scan(self):
        self.runner.config=replace(self.runner.config,search_mode='static')
        def menu_after_search(frame):
            if self.desktop.clicked:frame[0,0,0]=1
        self.desktop.on_capture=menu_after_search
        self.runner.scanner=Mock()
        with self.assertRaisesRegex(RuntimeError,'восстановить'):self.run_probe()
        self.runner.scanner.scan.assert_not_called();self.assert_no_purchase()

    def test_static_wait_is_cancellable_before_post_search_capture(self):
        self.runner.config=replace(self.runner.config,search_mode='static',search_wait_ms=2000)
        captures_at_search=[]
        def pause_after_search():
            if self.desktop.clicked:
                captures_at_search.append(self.desktop.captures);self.pause.set()
        self.clock.after_sleep=pause_after_search
        self.run_probe()
        self.assertEqual(self.desktop.captures,captures_at_search[0])
        self.assertLess(self.clock.now,.1)
        self.assertEqual(self.desktop.clicked,[(1342,345)]);self.assert_no_purchase()

    def test_search_components_measure_actual_wait_and_capture_without_double_counting(self):
        self.runner.config=replace(self.runner.config,search_mode='static',search_wait_ms=200)
        capture=self.desktop.capture
        def slow_capture():
            self.clock.sleep(.01);return capture()
        self.desktop.capture=slow_capture
        with patch('stalbot.runner.time.perf_counter',self.clock.monotonic):
            self.runner.fresh(lambda:False)
        t=self.runner.search_timings
        self.assertAlmostEqual(t['initial_capture_ms'],10*(self.desktop.captures-1))
        self.assertAlmostEqual(t['captures_ms'],10)
        self.assertAlmostEqual(t['wait_ms'],200)
        self.assertEqual(t['captures'],1)
        self.assertAlmostEqual(t['total_ms'],sum(t[key] for key in
            ('initial_capture_ms','initial_check_ms','click_ms','wait_ms','captures_ms','checks_ms','poll_sleep_ms','prepare_sleep_ms')))


if __name__=='__main__':unittest.main()
