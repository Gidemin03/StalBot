"""Exercise observation and grouped selection routing without Windows input or OCR weights."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

import numpy as np

from stalbot.config import Config
from stalbot.domain import Catalog,Identity,Number,Rule
from stalbot.runner import Runner
from stalbot.scanner import Scanner
from stalbot.storage import Store


class Reader:
    def names(self,images,batch_size):return [Identity('Mock',upgrade=0) for _ in images]
    def number(self,image,field):
        row=int(image[0,0,0])
        return Number('VALUE',1 if field=='quantity' else 90-10*row)


class RunnerCycleTests(unittest.TestCase):
    def test_gpu_routes_batch9_in_observation_and_live_without_changing_group(self):
        from dataclasses import replace
        self.runner.config=replace(self.runner.config,ocr_device='dml',batch_size=1)
        for live in (False,True):
            with self.subTest(live=live):
                self.stop.clear()
                self.runner.scanner=Mock(wraps=Scanner(Reader()))
                self.runner.fresh=Mock(return_value=self.frame)
                self.runner.purchase=Mock(side_effect=lambda *args:self.stop.set())
                self.runner.run(live,self.stop,self.pause)
                call=self.runner.scanner.scan.call_args
                self.assertEqual(call.args[2],9)
                self.assertEqual(call.kwargs['group_size'],3)
                self.assertEqual(call.kwargs['selection_metric'],'roi')
                self.desktop.click.assert_not_called()

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store(Path(temporary.name)/'test.sqlite3')
        self.store.save_rule(Rule('Mock',100,200))
        self.frame=np.zeros((1080,1920,3),np.uint8)
        for row in range(9):self.frame[393+37*row:430+37*row]=row
        self.desktop=Mock();self.desktop.capture.return_value=self.frame
        self.interface=Mock();self.interface.auction.return_value=True
        self.interface.any_menu.return_value=False;self.interface.outcome.return_value=None
        self.events=[]
        self.runner=Runner(Scanner(Reader()),self.store,self.desktop,self.interface,
            Config(group_size=3,selection_metric='roi'),Path(temporary.name)/'diagnostics',self.events.append)
        self.stop=Mock(wraps=threading.Event());self.stop.wait.return_value=False
        self.pause=threading.Event()

    def test_observation_selects_group_winner_logs_timing_without_input(self):
        self.runner.run(False,self.stop,self.pause)
        self.desktop.capture.assert_called_once();self.desktop.click.assert_not_called()
        self.desktop.exclusive.assert_not_called();self.assertFalse(self.store.history())
        event=self.events[-1]
        self.assertEqual(event['chosen_row'],3)
        self.assertEqual([r['row'] for r in event['rows']],[0,1,2])
        self.assertEqual((event['group_size'],event['selection_metric']),(3,'roi'))
        t=event['timings']
        self.assertEqual(event['elapsed_ms'],t['scan']['total_ms'])
        self.assertGreaterEqual(t['decision_ms'],t['scan']['total_ms']+t['snapshot_ms']+t['interface_ms']+t['budget_ms'])
        with self.store.connect() as db:
            logged=json.loads(db.execute("SELECT payload FROM events WHERE mode='observe'").fetchone()[0])
        self.assertEqual(logged,event)

    def test_live_routes_only_group_winner_to_purchase(self):
        self.runner.fresh=Mock(return_value=self.frame)
        self.runner.purchase=Mock(side_effect=lambda *args:self.stop.set())
        self.runner.run(True,self.stop,self.pause)
        self.desktop.exclusive.assert_called_once()
        self.runner.fresh.assert_called_once();self.runner.purchase.assert_called_once()
        self.assertEqual(self.runner.purchase.call_args.args[0].row,2)
        self.assertEqual(self.events[-1]['mode'],'live')
        self.desktop.click.assert_not_called()

    def test_live_after_restart_selects_same_price_despite_recovered_history(self):
        from dataclasses import replace
        self.runner.config=replace(self.runner.config,group_size=1)
        catalog=Catalog(self.store.rules())
        decision,_=self.runner.scanner.scan(self.frame,catalog,1,5000)
        self.store.begin_buy('old',decision);self.store.finish_buy('old','UNKNOWN')
        self.store.recover_unknown('old')
        self.store=Store(self.store.path);self.runner.store=self.store
        self.runner.fresh=Mock(return_value=self.frame)
        self.runner.purchase=Mock(side_effect=lambda *args:self.stop.set())
        self.runner.run(True,self.stop,self.pause)
        self.runner.purchase.assert_called_once()
        chosen=self.runner.purchase.call_args.args[0]
        self.assertEqual((chosen.row,chosen.buyout.value),(0,decision.buyout.value))

    def test_low_budget_stops_before_input_or_scan(self):
        for balance in (0,49999):
            with self.subTest(balance=balance):
                self.store.set_balance(balance)
                self.runner.scanner=Mock();self.runner.fresh=Mock();self.runner.purchase=Mock()
                self.runner.run(True,self.stop,self.pause)
                self.desktop.exclusive.assert_not_called();self.desktop.capture.assert_not_called()
                self.runner.fresh.assert_not_called();self.runner.scanner.scan.assert_not_called()
                self.runner.purchase.assert_not_called()
                self.assertEqual(self.events[-1]['result'],'BUDGET_LIMIT')

    def test_exact_threshold_allows_purchase_then_stops_without_next_search(self):
        self.store.set_balance(50000)
        self.runner.fresh=Mock(return_value=self.frame)
        self.runner.purchase=Mock(side_effect=lambda *args:self.store.set_balance(49930))
        self.runner.run(True,self.stop,self.pause)
        self.runner.fresh.assert_called_once();self.runner.purchase.assert_called_once()
        self.assertEqual(self.events[-1]['result'],'BUDGET_LIMIT')

    def test_recovered_unknown_reservation_can_trigger_budget_stop(self):
        from stalbot.domain import evaluate
        rule=self.store.rules()[0]
        d=evaluate(0,Identity('Mock',upgrade=0),rule,Number('VALUE',1),Number('VALUE',80),5000)
        self.store.set_balance(50000)
        self.store.begin_buy('old',d);self.store.finish_buy('old','UNKNOWN')
        self.runner.recover_auction=Mock();self.runner.fresh=Mock();self.runner.purchase=Mock()
        self.runner.run(True,self.stop,self.pause)
        self.assertEqual(self.store.balance(),49920)
        self.runner.fresh.assert_not_called();self.runner.purchase.assert_not_called()
        self.assertEqual(self.runner.session_purchases,0)
        self.assertEqual(self.events[-1]['result'],'BUDGET_LIMIT')

    def test_new_run_resets_profit_and_observation_still_works_with_low_budget(self):
        from decimal import Decimal
        self.runner.session_profit=Decimal('123');self.runner.session_purchases=1
        self.store.set_balance(0)
        self.runner.run(False,self.stop,self.pause)
        self.assertEqual(self.events[0],{'session_profit':'0','session_purchases':0})
        self.assertEqual(self.events[-1]['mode'],'observe')

    def test_observation_rejects_open_menu_before_scan(self):
        self.interface.any_menu.return_value=True
        self.runner.scanner=Mock()
        with self.assertRaisesRegex(RuntimeError,'чистый список'):self.runner.run(False,self.stop,self.pause)
        self.runner.scanner.scan.assert_not_called();self.desktop.click.assert_not_called()

    def test_unchanged_searches_retry_without_ocr_purchase_or_cycle_wait(self):
        self.runner.maintenance=Mock()
        calls=[]
        def unchanged(cancel):
            calls.append(cancel())
            if len(calls)==3:self.stop.set()
            return None
        self.runner.fresh=Mock(side_effect=unchanged)
        self.runner.scanner=Mock();self.runner.purchase=Mock()
        self.runner.run(True,self.stop,self.pause)
        self.assertEqual(len(calls),3)
        self.assertEqual(self.runner.maintenance.call_count,3)
        self.runner.scanner.scan.assert_not_called();self.runner.purchase.assert_not_called()
        self.stop.wait.assert_called_once_with(3)
        self.assertEqual(self.store.history(),[])

    def test_maintenance_runs_during_pause_without_capture_or_input(self):
        self.pause.set()
        calls=[]
        def maintain():
            calls.append(1)
            if len(calls)==3:
                self.pause.clear();self.stop.set()
        self.runner.maintenance=maintain
        self.runner.run(False,self.stop,self.pause)
        self.assertEqual(len(calls),3)
        self.desktop.capture.assert_not_called();self.desktop.click.assert_not_called()


if __name__=='__main__':unittest.main()
