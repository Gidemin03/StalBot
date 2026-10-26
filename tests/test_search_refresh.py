"""Search state machine checks with fake time and captures, never game input."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

import numpy as np

from stalbot.config import Config
from stalbot.domain import Rule
from stalbot.runner import Runner
from stalbot.scanner import ScanCancelled
from stalbot.search import FieldMonitor
from stalbot.vision import BOXES,STEP


class Clock:
    def __init__(self):self.now=0.;self.sleeps=[]
    def monotonic(self):return self.now
    def sleep(self,seconds):self.sleeps.append(seconds);self.now+=seconds


class Desktop:
    def __init__(self,clock):
        self.clock=clock;self.clicked=[];self.captures=0
        self.frame=np.zeros((1080,1920,3),np.uint8)
        x,y,_,_=BOXES['name'];self.frame[y,x+10]=200  # visible synthetic name
        self.after_search=lambda frame,elapsed:None
        self.searched_at=None;self.capture_cost=.001
        self.moved=[]
    def move(self,position):self.moved.append(position)
    def capture(self):
        self.clock.now+=self.capture_cost;self.captures+=1
        frame=self.frame.copy()
        if self.searched_at is not None:self.after_search(frame,self.clock.now-self.searched_at)
        return frame
    def click(self,position):
        self.clicked.append(position);self.searched_at=self.clock.now


class RegionDesktop(Desktop):
    def __init__(self,clock):super().__init__(clock);self.regions=[]
    def capture_region(self,box):
        self.regions.append(box)
        frame=self.capture();x1,y1,x2,y2=box
        return frame[y1:y2,x1:x2].copy()


class Interface:
    def auction(self,frame):return frame[0,0,0]!=1
    def any_menu(self,frame):return frame[0,0,0]==2
    def outcome(self,frame):return 'UNKNOWN_MODAL' if frame[0,0,0]==3 else None


def mark(frame,field='name',row=0,value=9):
    x,y,_,_=BOXES[field];frame[y+STEP*row,x]=value


class FieldMonitorTests(unittest.TestCase):
    def test_full_and_region_views_match_all_fields_without_copying(self):
        frame=np.arange(1080*1920*3,dtype=np.uint32).astype(np.uint8).reshape(1080,1920,3)
        for rows in (4,9):
            with self.subTest(rows=rows):
                monitor=FieldMonitor(rows);x1,y1,x2,y2=monitor.box
                full=monitor.views(frame)
                region=monitor.views(frame[y1:y2,x1:x2],origin=(x1,y1))
                self.assertEqual(len(full),3*rows)
                self.assertTrue(monitor.equal(full,region))
                self.assertTrue(all(np.shares_memory(frame,view) for view in full+region))
                changed=frame.copy();mark(changed,'quantity',rows-1)
                self.assertFalse(monitor.equal(full,monitor.views(changed)))

    def test_monitor_excludes_bid_timers_and_rows_outside_selected_scope(self):
        frame=np.zeros((1080,1920,3),np.uint8);changed=frame.copy()
        changed[420,1000]=8;mark(changed,'buyout',8)
        four=FieldMonitor(4);nine=FieldMonitor(9)
        self.assertTrue(four.equal(four.views(frame),four.views(changed)))
        self.assertFalse(nine.equal(nine.views(frame),nine.views(changed)))


class SearchRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.clock=Clock();self.desktop=Desktop(self.clock);self.events=[]
        self.runner=Runner(Mock(),Mock(),self.desktop,Interface(),
            Config(search_wait_ms=700,settle_timeout_ms=120,search_capture_region=False),
            Path(temporary.name)/'diagnostics',self.events.append)
        self.addCleanup(patch.stopall)
        patch('stalbot.runner.time.monotonic',self.clock.monotonic).start()
        patch('stalbot.runner.time.perf_counter',self.clock.monotonic).start()
        patch('stalbot.runner.time.sleep',self.clock.sleep).start()

    def fresh(self):return self.runner.fresh(lambda:False)

    def test_hover_disappearance_is_baseline_not_search_refresh(self):
        self.runner.config=replace(self.runner.config,search_stable_ms=20)
        clean=self.desktop.frame.copy()
        mark(self.desktop.frame,'name',5,30)
        moved_at=[];capture=self.desktop.capture
        def move(position):
            self.desktop.moved.append(position);moved_at.append(self.clock.now)
        def delayed_hover():
            frame=capture()
            if moved_at and self.clock.now-moved_at[0]>=.015:frame[:]=clean
            return frame
        self.desktop.move=move;self.desktop.capture=delayed_hover
        self.assertIsNone(self.fresh())
        self.assertEqual(self.runner.refresh_trace['result'],'UNCHANGED')
        self.assertTrue(np.array_equal(self.runner.refresh_trace['before'],clean))
        self.assertEqual(self.desktop.moved,[(1342,345)])
        self.assertGreaterEqual(self.desktop.searched_at-moved_at[0],.035)

    def test_empty_shell_waits_for_old_list_before_search(self):
        self.runner.config=replace(self.runner.config,settle_timeout_ms=500)
        old=self.desktop.frame.copy();self.desktop.frame[:]=0
        capture=self.desktop.capture
        def draw_list():
            frame=capture()
            if self.clock.now>=.15:frame[:]=old
            return frame
        self.desktop.capture=draw_list
        self.assertIsNone(self.fresh())
        self.assertEqual(self.runner.refresh_trace['result'],'UNCHANGED')
        self.assertTrue(np.array_equal(self.runner.refresh_trace['before'],old))
        self.assertGreaterEqual(self.desktop.searched_at,.18)

    def test_preparation_waits_for_lower_rows_even_with_four_row_trigger(self):
        self.runner.config=replace(self.runner.config,search_monitor_rows=4)
        capture=self.desktop.capture
        def draw_rows():
            frame=capture()
            mark(frame,'buyout',8,10 if self.clock.now<.025 else 20)
            return frame
        self.desktop.capture=draw_rows
        self.assertIsNone(self.fresh())
        self.assertGreaterEqual(self.desktop.searched_at,.055)
        self.assertEqual(self.runner.refresh_trace['result'],'UNCHANGED')

    def test_unstable_preparation_does_not_click_search_or_scan(self):
        capture=self.desktop.capture
        def changing():
            frame=capture();mark(frame,'buyout',8,self.desktop.captures%255)
            return frame
        self.desktop.capture=changing
        self.assertIsNone(self.fresh())
        self.assertEqual(self.runner.refresh_trace['result'],'PRESEARCH_UNSTABLE')
        self.assertEqual(self.desktop.clicked,[])
        self.runner.scanner.scan.assert_not_called()
        self.assertLess(self.clock.now,.14)

    def test_genuinely_empty_list_can_still_be_searched_after_bounded_wait(self):
        self.desktop.frame[:]=0
        self.assertIsNone(self.fresh())
        self.assertGreaterEqual(self.desktop.searched_at,.12)
        self.assertLess(self.desktop.searched_at,.14)
        self.assertEqual(self.desktop.clicked,[(1342,345)])
        self.assertEqual(self.runner.refresh_trace['result'],'UNCHANGED')

    def test_cancel_during_preparation_never_clicks_search(self):
        with self.assertRaises(ScanCancelled):
            self.runner.fresh(lambda:self.clock.now>=.015)
        self.assertEqual(self.desktop.clicked,[])

    def test_quick_response_is_still_observed_without_post_search_delay(self):
        self.desktop.after_search=lambda frame,elapsed:mark(frame,'buyout',0)
        self.assertIsNotNone(self.fresh())
        self.assertLess(self.runner.search_timings['first_change_ms'],2)
        self.assertLess(self.clock.now-self.desktop.searched_at,.05)

    def test_each_field_on_rows_two_three_four_can_confirm_refresh(self):
        for row in (1,2,3):
            for field in ('name','quantity','buyout'):
                with self.subTest(row=row,field=field):
                    self.clock.now=0.;self.desktop=Desktop(self.clock);self.runner.desktop=self.desktop
                    self.desktop.after_search=lambda frame,elapsed:mark(frame,field,row)
                    result=self.fresh()
                    self.assertIsNotNone(result)
                    self.assertEqual(self.runner.refresh_trace['result'],'CHANGED_STABLE')
                    self.assertLess(self.clock.now,.12)
                    self.assertGreaterEqual(self.clock.now,.03)
                    self.assertEqual(self.desktop.clicked,[(1342,345)])

    def test_zero_poll_uses_elapsed_time_and_needs_more_than_two_fast_captures(self):
        self.runner.config=replace(self.runner.config,search_poll_ms=0)
        self.desktop.after_search=lambda frame,elapsed:mark(frame)
        self.fresh()
        self.assertGreater(self.desktop.captures,20)
        self.assertGreaterEqual(self.clock.now,.03)
        self.assertTrue(all(delay==0 for delay in self.clock.sleeps))

    def test_unchanged_returns_none_at_deadline_without_diagnostic_failure(self):
        self.runner.save_refresh_trace=Mock(side_effect=OSError('unavailable'))
        self.assertIsNone(self.fresh())
        self.assertEqual(self.runner.refresh_trace['result'],'UNCHANGED')
        self.assertEqual(self.runner.search_timings['unchanged_searches'],1)
        self.assertIsNone(self.fresh())
        self.assertEqual(self.runner.search_timings['unchanged_searches'],2)
        self.runner.save_refresh_trace.assert_not_called()
        self.runner.scanner.scan.assert_not_called()
        self.assertEqual(self.desktop.clicked,[(1342,345)]*2)

    def test_transient_then_original_is_unstable_and_saves_diagnostic(self):
        self.desktop.after_search=lambda frame,elapsed:mark(frame) if elapsed<.015 else None
        self.assertIsNone(self.fresh())
        self.assertGreaterEqual(self.clock.now,.24)
        self.assertEqual(self.runner.refresh_trace['result'],'UNSTABLE')
        self.assertTrue(list(self.runner.diagnostics.glob('search-*/detail.json')))
        self.runner.scanner.scan.assert_not_called()
        self.assertEqual(self.desktop.clicked,[(1342,345)])

    def test_four_row_trigger_waits_for_all_nine_rows_to_settle(self):
        self.runner.config=replace(self.runner.config,search_monitor_rows=4)
        def update(frame,elapsed):
            mark(frame,'quantity',1)
            mark(frame,'buyout',8,value=1+int(elapsed/.005))
        self.desktop.after_search=update
        self.assertIsNone(self.fresh())
        self.assertGreaterEqual(self.clock.now,.24)
        self.assertEqual(self.runner.refresh_trace['result'],'UNSTABLE')

    def test_four_row_detector_lower_only_change_settles_without_another_search(self):
        self.runner.config=replace(self.runner.config,search_monitor_rows=4)
        self.desktop.after_search=lambda frame,elapsed:mark(frame,'quantity',8)
        result=self.fresh()
        self.assertIsNotNone(result)
        self.assertEqual(self.runner.refresh_trace['result'],'CHANGED_STABLE')
        self.assertTrue(any(s['phase']=='fallback_to_nine' for s in self.runner.refresh_trace['samples']))
        self.assertEqual(self.desktop.clicked,[(1342,345)])
        self.runner.scanner.scan.assert_not_called()

    def test_four_row_fallback_still_rejects_unstable_lower_rows(self):
        self.runner.config=replace(self.runner.config,search_monitor_rows=4)
        self.desktop.after_search=lambda frame,elapsed:mark(frame,'buyout',4,value=1+int(elapsed/.005))
        self.assertIsNone(self.fresh())
        self.assertGreaterEqual(self.clock.now,.24)
        self.assertEqual(self.runner.refresh_trace['result'],'UNSTABLE')
        self.assertEqual(self.desktop.clicked,[(1342,345)])
        self.runner.scanner.scan.assert_not_called()

    def test_default_nine_row_detector_confirms_row_nine_only_change(self):
        self.assertEqual(self.runner.config.search_monitor_rows,9)
        self.desktop.after_search=lambda frame,elapsed:mark(frame,'quantity',8)
        result=self.fresh()
        self.assertIsNotNone(result)
        self.assertEqual(self.runner.refresh_trace['result'],'CHANGED_STABLE')
        self.assertEqual(self.desktop.clicked,[(1342,345)])
        x,y,_,_=BOXES['quantity']
        self.assertEqual(int(result[y+STEP*8,x,0]),9)

    def test_lower_rows_changing_once_reset_settling_window(self):
        self.runner.config=replace(self.runner.config,search_monitor_rows=4)
        def update(frame,elapsed):
            mark(frame,'name',0)
            if elapsed>=.025:mark(frame,'quantity',8)
        self.desktop.after_search=update
        result=self.fresh()
        self.assertIsNotNone(result)
        self.assertGreaterEqual(self.clock.now,.055)

    def test_unknown_ui_state_never_becomes_unchanged_or_reaches_ocr(self):
        for state in (1,2,3):
            with self.subTest(state=state):
                self.clock.now=0.;self.desktop=Desktop(self.clock);self.runner.desktop=self.desktop
                self.desktop.after_search=lambda frame,elapsed:frame.__setitem__((0,0,0),state)
                with self.assertRaises(RuntimeError):self.fresh()
                self.assertNotEqual(self.runner.refresh_trace['result'],'UNCHANGED')
                self.runner.scanner.scan.assert_not_called()

    def test_focus_failure_after_search_propagates_without_retry(self):
        def lose_focus(frame,elapsed):raise RuntimeError('Фокус окна потерян')
        self.desktop.after_search=lose_focus
        with self.assertRaisesRegex(RuntimeError,'Фокус окна потерян'):self.fresh()
        self.assertEqual(self.desktop.clicked,[(1342,345)])
        self.runner.scanner.scan.assert_not_called()

    def test_pause_and_stop_cancel_during_poll_without_another_input(self):
        for event_name in ('pause','stop'):
            with self.subTest(event=event_name):
                event=threading.Event();self.clock.now=0.
                self.desktop=Desktop(self.clock);self.runner.desktop=self.desktop
                self.desktop.after_search=lambda frame,elapsed:event.set()
                with self.assertRaises(ScanCancelled):self.runner.fresh(event.is_set)
                self.assertEqual(self.desktop.clicked,[(1342,345)])
                self.runner.scanner.scan.assert_not_called()

    def test_region_and_full_captures_return_the_same_full_ocr_frame(self):
        results=[]
        for region in (False,True):
            self.clock.now=0.;self.desktop=RegionDesktop(self.clock);self.runner.desktop=self.desktop
            self.runner.config=replace(self.runner.config,search_capture_region=region)
            self.desktop.after_search=lambda frame,elapsed:mark(frame,'quantity',3)
            results.append(self.fresh())
            self.assertEqual(results[-1].shape,(1080,1920,3))
            self.assertEqual(bool(self.desktop.regions),region)
        self.assertTrue(np.array_equal(*results))

    def test_completed_live_cycles_immediately_repeat_without_cycle_interval_wait(self):
        self.runner.store.rules.return_value=[Rule('Mock',100,200)]
        self.runner.store.unresolved.return_value=[]
        self.runner.store.balance.return_value=100000
        self.desktop.exclusive=Mock()
        stop=Mock(wraps=threading.Event());stop.wait.return_value=False
        self.runner.fresh=Mock(return_value=self.desktop.frame)
        self.runner.purchase=Mock(return_value='SUCCESS')
        scans=[]
        def scan(*args,**kwargs):
            scans.append(args[0]);kwargs['timings']['total_ms']=.5
            if len(scans)==2:
                stop.set();return None,[]
            return SimpleNamespace(row=0),[]
        self.runner.scanner.scan.side_effect=scan
        self.runner.run(True,stop,threading.Event())
        self.assertEqual(len(scans),2)
        self.assertEqual(self.runner.fresh.call_count,2)
        self.runner.purchase.assert_called_once()
        stop.wait.assert_called_once_with(3)
        self.assertEqual(self.clock.sleeps,[])


if __name__=='__main__':unittest.main()
