"""Background menu settling, with fake frames, time and desktop only."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_purchase_options as options
from test_windows import FakeClock
from stalbot.scanner import ScanCancelled
from stalbot.vision import BOXES,STEP


class BackgroundPurchaseTests(unittest.TestCase):
    def setUp(self):
        options.PurchaseOptionsTests.setUp(self)
        self.runner,self.desktop,self.scanner,self.ui=options.PurchaseOptionsTests.runner(self)
        self.runner.config.click_mode='background'
        self.clock=FakeClock()
        for item in (patch('stalbot.runner.time.monotonic',lambda:self.clock.now),
                     patch('stalbot.runner.time.perf_counter',lambda:self.clock.now),
                     patch('stalbot.runner.time.sleep',self.clock.sleep)):
            item.start();self.addCleanup(item.stop)
        self.original_capture=self.desktop.capture
        self.pixel=(BOXES['buyout'][1]+STEP*self.decision.row,BOXES['buyout'][0])

    def purchase(self,cancel=lambda:False):
        return self.runner.purchase(self.decision,self.catalog,cancel)

    def test_transitional_pixels_settle_before_single_ocr_and_single_buy(self):
        def capture():
            frame=self.original_capture()
            if self.desktop.clicked and self.clock.now<.05:frame[self.pixel]=self.desktop.captures
            return frame
        self.desktop.capture=capture
        original_row=self.scanner.row
        def scan(frame,*args):
            self.assertGreaterEqual(self.clock.now,.09-1e-9)
            self.assertEqual(int(frame[self.pixel][0]),0)
            return original_row(frame,*args)
        self.scanner.row=scan
        self.assertEqual(self.purchase(),'SUCCESS')
        self.assertEqual(self.scanner.calls,[4])
        self.assertEqual(self.desktop.clicked.count((1330,596)),1)
        self.assertEqual(self.store.balance(),920)

    def test_continuously_changing_menu_times_out_without_ocr_or_buy(self):
        def capture():
            frame=self.original_capture();frame[self.pixel]=self.desktop.captures%2
            return frame
        self.desktop.capture=capture
        self.assertEqual(self.purchase(),'STALE')
        self.assertEqual(self.scanner.calls,[])
        self.assertFalse(self.desktop.sent)
        self.assertEqual(self.store.history(),[])
        self.assertGreaterEqual(self.clock.now,.5)
        self.assertLess(self.clock.now,.53)
        self.assertIn('стабилизировалось',self.runner.purchase_timings['skip_reason'])

    def test_cursor_mode_also_waits_for_transitional_pixels(self):
        self.runner.config.click_mode='cursor'
        self.test_transitional_pixels_settle_before_single_ocr_and_single_buy()

    def test_result_timeout_one_second_and_poll_40ms_never_retries_buy(self):
        from stalbot.config import Config
        self.runner.config.result_timeout_ms=Config().result_timeout_ms
        self.assertEqual(self.runner.config.result_timeout_ms,1000)
        self.desktop.result=0
        waits=[]
        def sleep(seconds):
            if self.desktop.sent:waits.append(seconds)
            self.clock.sleep(seconds)
        with patch('stalbot.runner.time.sleep',sleep):self.assertEqual(self.purchase(),'UNKNOWN')
        self.assertTrue(waits);self.assertTrue(all(x==.04 for x in waits))
        self.assertAlmostEqual(sum(waits),1,places=6)
        self.assertEqual(self.desktop.clicked.count((1330,596)),1)
        self.assertEqual(self.store.history()[0]['status'],'UNKNOWN_RECOVERED')

    def test_cancel_during_settling_never_sends_buy(self):
        with self.assertRaises(ScanCancelled):self.purchase(lambda:self.clock.now>=.03)
        self.assertFalse(self.desktop.sent);self.assertEqual(self.scanner.calls,[])
        self.assertEqual(self.store.history(),[])

    def test_settled_changed_price_still_rejected_and_diagnosed(self):
        self.scanner.decisions=iter([options.candidate(price=90)])
        self.assertEqual(self.purchase(),'STALE')
        self.assertFalse(self.desktop.sent);self.assertEqual(self.store.balance(),1000)
        path=Path(self.runner.purchase_timings['skip_diagnostic'])
        detail=json.loads((path/'detail.json').read_text(encoding='utf-8'))
        self.assertEqual(detail['decision']['buyout']['value'],80)
        self.assertEqual(detail['verified']['buyout']['value'],90)
        self.assertTrue((path/'screen.png').exists())

    def test_change_after_ocr_keeps_exact_pixel_guard_and_saves_both_frames(self):
        def capture():
            frame=self.original_capture()
            if self.scanner.calls:frame[self.pixel]=1
            return frame
        self.desktop.capture=capture
        self.assertEqual(self.purchase(),'STALE')
        self.assertFalse(self.desktop.sent);self.assertEqual(self.store.history(),[])
        path=Path(self.runner.purchase_timings['skip_diagnostic'])
        detail=json.loads((path/'detail.json').read_text(encoding='utf-8'))
        self.assertEqual(detail['changed_fields'],['buyout'])
        self.assertTrue((path/'reference.png').exists())
        with self.store.connect() as db:
            event=json.loads(db.execute("SELECT payload FROM events WHERE mode='purchase_timing'").fetchone()[0])
        self.assertEqual(event['purchase_timings']['skip_reason'],'Строка изменилась перед BUY')
        self.assertEqual(event['purchase_timings']['click_mode'],'background')

    def test_failed_optional_diagnostic_does_not_allow_buy(self):
        self.scanner.decisions=iter([options.candidate(price=90)])
        with patch.object(self.runner,'diagnostic',side_effect=OSError('disk unavailable')):
            self.assertEqual(self.purchase(),'STALE')
        self.assertFalse(self.desktop.sent)
        self.assertEqual(self.runner.purchase_timings['skip_diagnostic_error'],'disk unavailable')


if __name__=='__main__':unittest.main()
