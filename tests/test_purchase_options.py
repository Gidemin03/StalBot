import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from stalbot.config import Config
from stalbot.domain import Catalog, Identity, Number, Rule, evaluate
from stalbot.runner import Runner
from stalbot.scanner import ScanCancelled
from stalbot.storage import Store
from test_windows import FakeClock


def candidate(row=4, price=80):
    return evaluate(row, Identity('Item', upgrade=0), Rule('Item', 100, 200),
                    Number('VALUE', 1), Number('VALUE', price), 1000)


class Scanner:
    def __init__(self, decisions):
        self.decisions=iter(decisions)
        self.calls=[]

    def row(self, frame, row, catalog, limit, balance):
        self.calls.append(row)
        return next(self.decisions)


class Desktop:
    def move(self,position):pass
    def __init__(self, store, row):
        self.store=store;self.row=row;self.clicked=[];self.sent=False
        self.closed=False;self.captures=0;self.change_immediate=False
        self.result=1
        self.on_capture=lambda count:None

    def capture(self):
        self.captures+=1
        self.on_capture(self.captures)
        frame=np.zeros((1080,1920,3), dtype=np.uint8)
        frame[394,937]=200  # visible synthetic name for Search preparation
        if self.change_immediate and self.captures==5:frame[:]=2
        if self.sent and not self.closed:frame[0,0,0]=self.result
        return frame

    def click(self, position):
        self.clicked.append(position)
        if position==(1330,448+37*self.row):
            assert len(self.store.unresolved())==1
            self.sent=True
        if position==(960,585):self.closed=True


class Interface:
    def __init__(self):self.menu_visible=True;self.menu_rows=[];self.dirty=False
    def auction(self, frame):return True
    def any_menu(self, frame):return self.dirty
    def menu(self, frame, row):
        self.menu_rows.append(row)
        return self.menu_visible
    def outcome(self, frame):return 'SUCCESS' if frame[0,0,0]==1 else None
    def ok_position(self, status):return (960,585)


class PurchaseOptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'test.sqlite')
        self.store.set_balance(1000)
        self.decision=candidate()
        self.catalog=Catalog([self.decision.rule])
        self.clock=FakeClock()
        for item in (patch('stalbot.runner.time.monotonic',lambda:self.clock.now),
                     patch('stalbot.runner.time.sleep',self.clock.sleep)):
            item.start();self.addCleanup(item.stop)

    def runner(self, revalidate=True, decisions=None):
        scanner=Scanner(decisions if decisions is not None else [self.decision,self.decision])
        desktop=Desktop(self.store,self.decision.row)
        interface=Interface()
        config=SimpleNamespace(**dict(asdict(Config()), revalidate_lot=revalidate,result_timeout_ms=30))
        runner=Runner(scanner,self.store,desktop,interface,config,Path(self.tmp.name)/'diagnostics')
        return runner,desktop,scanner,interface

    def test_default_option_rereads_once_after_selection_and_detects_stale(self):
        self.assertTrue(Config().revalidate_lot)
        runner,desktop,scanner,_=self.runner(decisions=[candidate(price=90)])
        row=scanner.row
        def after_menu(*args):
            self.assertEqual(desktop.clicked,[(1030,553)])
            return row(*args)
        scanner.row=after_menu
        self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False),'STALE')
        self.assertEqual(scanner.calls,[4])
        self.assertFalse(desktop.sent)
        self.assertFalse(self.store.history())

    def test_disabled_rereads_buys_chosen_row_without_ocr_and_records_option(self):
        runner,desktop,scanner,interface=self.runner(False,[])
        self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False),'SUCCESS')
        self.assertEqual(scanner.calls,[])
        self.assertEqual(desktop.clicked,[(1030,553),(1330,596),(960,585)])
        self.assertEqual(interface.menu_rows,[4,4,4,4])  # Three stable-menu captures and final guard.
        self.assertEqual(self.store.balance(),920)
        history=self.store.history()
        self.assertEqual(len(history),1)
        detail=json.loads((Path(history[0]['diagnostic'])/'detail.json').read_text(encoding='utf-8'))
        self.assertFalse(detail['revalidate_lot'])

    def test_disabled_rereads_missing_menu_never_buys(self):
        runner,desktop,scanner,interface=self.runner(False,[])
        interface.menu_visible=False
        with patch('stalbot.runner.time.monotonic',side_effect=[0,2,2]):
            self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False),'STALE')
        self.assertEqual(scanner.calls,[])
        self.assertFalse(desktop.sent)
        self.assertFalse(self.store.history())

    def test_disabled_rereads_preserves_immediate_pixel_guard(self):
        runner,desktop,scanner,_=self.runner(False,[])
        desktop.change_immediate=True
        self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False),'STALE')
        self.assertFalse(desktop.sent)
        self.assertFalse(self.store.history())
        self.assertEqual(scanner.calls,[])

    def test_disabled_rereads_rejects_dirty_auction(self):
        runner,desktop,_,interface=self.runner(False,[])
        interface.dirty=True
        with self.assertRaisesRegex(RuntimeError,'восстановить'):
            runner.purchase(self.decision,self.catalog,lambda:False)
        self.assertEqual(desktop.clicked,[(1342,345)]*3)
        self.assertFalse(desktop.sent)

    def test_disabled_rereads_rechecks_balance_before_selection(self):
        runner,desktop,scanner,_=self.runner(False,[])
        self.store.set_balance(79)
        self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False),'STALE')
        self.assertFalse(desktop.clicked)
        self.assertFalse(self.store.history())
        self.assertEqual(scanner.calls,[])

    def test_current_balance_rechecked_immediately_before_attempt_in_both_modes(self):
        for enabled in (False,True):
            with self.subTest(enabled=enabled):
                self.store.set_balance(1000)
                runner,desktop,_,_=self.runner(enabled)
                def lower_balance(count):
                    if count==5:self.store.set_balance(79)
                desktop.on_capture=lower_balance
                self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False),'STALE')
                self.assertFalse(desktop.sent)
                self.assertFalse(self.store.history())

    def test_disabled_rereads_probe_records_option_without_attempt(self):
        runner,desktop,scanner,_=self.runner(False,[])
        self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False,probe=True),'PROBE')
        self.assertEqual(desktop.clicked,[(1030,553)])
        self.assertFalse(self.store.history())
        self.assertEqual(self.store.balance(),1000)
        self.assertEqual(scanner.calls,[])
        with self.store.connect() as db:
            payload=json.loads(db.execute("SELECT payload FROM events WHERE mode='probe'").fetchone()[0])
        self.assertFalse(payload['detail']['revalidate_lot'])

    def test_disabled_rereads_cancel_after_selection_never_buys(self):
        runner,desktop,scanner,_=self.runner(False,[])
        with self.assertRaises(ScanCancelled):
            runner.purchase(self.decision,self.catalog,lambda:bool(desktop.clicked))
        self.assertFalse(desktop.sent)
        self.assertFalse(self.store.history())
        self.assertEqual(scanner.calls,[])

    def test_disabled_rereads_unknown_recovers_and_cannot_repeat_buy(self):
        runner,desktop,scanner,_=self.runner(False,[])
        desktop.result=0
        self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False),'UNKNOWN')
        self.assertFalse(self.store.unresolved())
        self.assertEqual(self.store.history()[0]['status'],'UNKNOWN_RECOVERED')
        self.assertEqual(self.store.balance(),920)
        self.assertEqual(runner.purchase(self.decision,self.catalog,lambda:False),'STALE')
        self.assertEqual(desktop.clicked.count((1330,596)),1)
        self.assertEqual(scanner.calls,[])


if __name__=='__main__':unittest.main()
