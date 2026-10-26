import json
import unittest
from unittest.mock import patch
import test_bot as fixtures
from stalbot.domain import Catalog


class PurchaseMetricsTests(fixtures.StoreFixture,unittest.TestCase):
    def test_no_temporary_delay_after_notification_closes(self):
        runner,desktop=fixtures.RunnerTests.make_runner(self)
        from test_windows import FakeClock
        clock=FakeClock();after_close=[]
        def wait(seconds):
            if desktop.closed:after_close.append(seconds)
            clock.sleep(seconds)
        with patch('stalbot.runner.time.monotonic',lambda:clock.now),patch('stalbot.runner.time.sleep',wait):
            self.assertEqual(runner.purchase(fixtures.decision(),Catalog([fixtures.decision().rule]),lambda:False),'SUCCESS')
        self.assertTrue(desktop.closed)
        self.assertEqual(after_close,[])
        self.assertEqual(desktop.clicked.count((1330,448)),1)

    def test_actual_down_and_recognition_use_same_cycle_origin_and_persist(self):
        runner,desktop=fixtures.RunnerTests.make_runner(self)
        clock=[10.0];runner.cycle_started=9.0
        click=desktop.click;capture=desktop.capture
        def measured_click(position):
            start=clock[0];clock[0]+=.01;click(position);down=clock[0]
            clock[0]+=.03
            return {'started_at':start,'down_sent_at':down,'released_at':clock[0],
                    'move_ms':10,'hold_ms':30,'total_ms':40}
        def capture_frame():clock[0]+=.02;return capture()
        desktop.click=measured_click;desktop.capture=capture_frame
        with patch('stalbot.runner.time.perf_counter',lambda:clock[0]):
            runner.purchase(fixtures.decision(),Catalog([fixtures.decision().rule]),lambda:False)
        t=runner.purchase_timings
        self.assertGreater(t['buy_call_ms'],1000)
        self.assertAlmostEqual(t['buy_pressed_ms']-t['buy_call_ms'],10)
        self.assertAlmostEqual(t['buy_to_result_ms'],50)
        self.assertAlmostEqual(t['result_observed_ms']-t['buy_pressed_ms'],50)
        with self.store.connect() as db:
            event=json.loads(db.execute("SELECT payload FROM events WHERE mode='purchase_timing'").fetchone()[0])
        self.assertEqual(event['purchase_timings']['buy_pressed_ms'],t['buy_pressed_ms'])
        from pathlib import Path
        detail=json.loads((Path(self.store.history()[0]['diagnostic'])/'detail.json').read_text(encoding='utf-8'))
        self.assertEqual(detail['timings']['result_observed_ms'],t['result_observed_ms'])

    def test_insufficient_is_known_stops_without_debit_close_or_second_buy(self):
        runner,desktop=fixtures.RunnerTests.make_runner(self)
        self.store.set_balance(1000)
        runner.ui.outcome=lambda frame:'INSUFFICIENT' if desktop.sent else None
        with self.assertRaisesRegex(RuntimeError,'Недостаточно средств'):
            runner.purchase(fixtures.decision(),Catalog([fixtures.decision().rule]),lambda:False)
        self.assertEqual(self.store.history()[0]['status'],'INSUFFICIENT')
        self.assertEqual(self.store.balance(),1000)
        self.assertEqual(desktop.clicked,[(1030,405),(1330,448)])
        self.assertEqual(self.store.unresolved()[0]['status'],'INSUFFICIENT')
        self.assertIsNotNone(runner.purchase_timings['result_observed_ms'])
        self.assertIsNone(runner.purchase_timings['buy_pressed_ms']) # Fake API gives no DOWN timestamp.

    def test_probe_does_not_invent_buy_or_result_times(self):
        runner,desktop=fixtures.RunnerTests.make_runner(self)
        self.assertEqual(runner.purchase(fixtures.decision(),Catalog([fixtures.decision().rule]),lambda:False,probe=True),'PROBE')
        self.assertIsNone(runner.purchase_timings['buy_pressed_ms'])
        self.assertIsNone(runner.purchase_timings['result_observed_ms'])
        self.assertIsNotNone(runner.purchase_timings['ready_ms'])


if __name__=='__main__':unittest.main()
