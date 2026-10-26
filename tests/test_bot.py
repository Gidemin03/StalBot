import tempfile
import threading
import unittest
from itertools import count
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
import numpy as np

from stalbot.config import Config
from stalbot.domain import Catalog,Identity,Rule,Number,evaluate,identity_from_text
from stalbot.storage import Store
from stalbot.scanner import Scanner,ScanCancelled
from stalbot.runner import Runner
from stalbot.vision import parse_number,validate_frame


def decision(row=0,price=80,qty=None):
    rule=Rule('Тестовый предмет',100,200,upgrade=0)
    return evaluate(row,Identity(rule.name,upgrade=0),rule,qty or Number('VALUE',1),Number('VALUE',price),5000)


class DomainTests(unittest.TestCase):
    def test_upgrade_is_exact(self):
        catalog=Catalog([Rule('Янтарник',100,200,color='red',upgrade=15)])
        self.assertIsNotNone(catalog.match(identity_from_text('Янтарник | +15','red'))[0])
        for text in ('Янтарник | +12','Янтарник | +152','Янтарник | +','Янтарник','Янтарник..'):
            self.assertIsNone(catalog.match(identity_from_text(text,'red'))[0],text)
        self.assertIsNone(catalog.match(identity_from_text('Янтарник | +15','blue'))[0])

    def test_uncertain_separator_does_not_become_zero(self):
        identity=identity_from_text('Янтарник','red',separator_visible=True)
        self.assertIsNone(identity.upgrade)
        self.assertIsNone(Catalog([Rule('Янтарник',100,200,upgrade=0)]).match(identity)[0])

    def test_truncated_names_have_zero_upgrade_and_match(self):
        for text in ('Композитная пластина..','Композитная пластина…'):
            for separator in (False,True):
                identity=identity_from_text(text,'white',separator_visible=separator)
                self.assertEqual(identity.upgrade,0);self.assertTrue(identity.truncated)
                self.assertEqual(Catalog([Rule('Композитная пластина',100,200,upgrade=0)]).match(identity)[1],'matched')
        for text in ('Янтарник | +','Янтарник | +152'):
            self.assertFalse(identity_from_text(text,'red').valid)

    def test_truncated_unique_and_overlaps(self):
        catalog=Catalog([Rule('Композитная пластина IV',100,200),Rule('Композитная пластина V',100,200)])
        self.assertEqual(catalog.match(identity_from_text('Композитная пластина..','white'))[1],'ambiguous_name')
        self.assertEqual(Catalog(catalog.rules[:1]).match(identity_from_text('Композитная пластина..','white'))[0],catalog.rules[0])
        self.assertEqual(Catalog(catalog.rules[:1]).match(identity_from_text('Композитная пластина','white'))[1],'unknown_name')
        duplicate=Catalog([Rule('Водка',100,200),Rule('Водка',120,200,color='green')])
        self.assertEqual(duplicate.match(Identity('Водка','green',0))[1],'ambiguous_variant')

    def test_truncated_variants_and_purchase_decision(self):
        identity=identity_from_text('Композитная пластина..','white')
        rule=Rule('Композитная пластина IV',100,200,color='white',upgrade=0)
        matched,reason=Catalog([rule]).match(identity)
        self.assertEqual(reason,'matched')
        self.assertTrue(evaluate(0,identity,matched,Number('VALUE',1),Number('VALUE',80),5000).buy)
        for variant in (replace(rule,color='green'),replace(rule,upgrade=1)):
            self.assertEqual(Catalog([variant]).match(identity)[1],'unknown_variant')
        self.assertEqual(Catalog([rule,replace(rule,color='*')]).match(identity)[1],'ambiguous_variant')
        self.assertEqual(Catalog([rule]).match(identity_from_text('Неизвестный..','white'))[1],'unknown_name')
        self.assertEqual(Catalog([rule]).match(identity_from_text('…','white'))[1],'invalid_name')

    def test_quantity_conservative_and_prices_strict(self):
        for state in ('ABSENT','AMBIGUOUS'):
            d=decision(qty=Number(state,None));self.assertTrue(d.buy);self.assertEqual(d.effective_quantity,1)
            self.assertFalse(decision(price=150,qty=Number(state,None)).buy)
        for raw in ('ей650 000 000 рублей','9 999 рублеи9 999 рублей','60 00','0','-1',''):
            self.assertEqual(parse_number(raw,'buyout').state,'AMBIGUOUS')
        self.assertEqual(parse_number('60 000 рублей','buyout').value,60000)
        self.assertEqual(parse_number('---','buyout').state,'ABSENT')

    def test_economics_and_limits(self):
        d=decision();self.assertEqual(d.profit,'110.00');self.assertEqual(d.roi_percent,'137.50')
        for limit,balance,reason in ((79,None,'lot_price_limit'),(5000,79,'balance_limit')):
            self.assertEqual(evaluate(0,d.identity,d.rule,d.quantity,d.buyout,limit,balance).reason,reason)
        self.assertEqual(evaluate(0,d.identity,replace(d.rule,max_stack=1),Number('VALUE',2),d.buyout,5000).reason,'stack_limit')
        self.assertEqual(evaluate(0,d.identity,replace(d.rule,sell_unit=80),d.quantity,d.buyout,5000).reason,'nonpositive_profit')

    def test_dimensions_and_config(self):
        validate_frame(np.zeros((1081,1920,3),np.uint8))
        with self.assertRaises(ValueError):validate_frame(np.zeros((1081,1920,3),np.uint8),live=True)
        with self.assertRaises(ValueError):Config(batch_size=2).validate()


class StoreFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.sqlite3')
    def tearDown(self):self.tmp.cleanup()


class StoreTests(StoreFixture,unittest.TestCase):
    def test_pending_survives_restart_and_balance_debits_once(self):
        self.store.set_balance(1000);self.store.begin_buy('first',decision())
        other=Store(self.store.path)
        with self.assertRaises(RuntimeError):other.begin_buy('second',decision())
        other.finish_buy('first','SUCCESS');self.assertEqual(self.store.balance(),920)
        with self.assertRaises(RuntimeError):other.finish_buy('first','SUCCESS')
        self.assertEqual(self.store.balance(),920)

    def test_unknown_requires_manual_resolution(self):
        self.store.begin_buy('first',decision());self.store.finish_buy('first','UNKNOWN')
        with self.assertRaises(RuntimeError):self.store.begin_buy('second',decision())
        self.store.acknowledge('first');self.store.begin_buy('second',decision())

    def test_catalog_roundtrip(self):
        rid=self.store.save_rule(Rule('Mock',100,200,color='red',upgrade=12,max_stack=64))
        rule=self.store.rules()[0];self.assertEqual(rule.id,rid);self.assertEqual(rule.upgrade,12)
        self.store.save_rule(replace(rule,buy_unit=90));self.assertEqual(self.store.rules()[0].buy_unit,90)
        self.store.delete_rule(rid);self.assertFalse(self.store.rules())

    def test_unavailable_does_not_debit(self):
        self.store.set_balance(1000);self.store.begin_buy('first',decision());self.store.finish_buy('first','UNAVAILABLE')
        self.assertEqual(self.store.balance(),1000);self.assertFalse(self.store.unresolved())


class FakeReader:
    def __init__(self):self.name_calls=[];self.number_calls=[]
    def names(self,images,batch_size):
        rows=[int(im[0,0,0]) for im in images];self.name_calls.append(rows)
        return [Identity('Unknown' if row<4 else 'Mock',upgrade=0) for row in rows]
    def number(self,image,field):
        self.number_calls.append((int(image[0,0,0]),field))
        return Number('VALUE',1 if field=='quantity' else 80)


class ScannerTests(unittest.TestCase):
    @staticmethod
    def frame():
        frame=np.zeros((1080,1920,3),np.uint8)
        for row in range(9):frame[393+37*row:430+37*row]=row
        return frame

    def test_batches_preserve_first_match_and_skip_unknown_numbers(self):
        frame=np.zeros((1080,1920,3),np.uint8)
        for row in range(9):frame[393+37*row:430+37*row]=row
        for batch in (1,3,9):
            reader=FakeReader();chosen,rows=Scanner(reader).scan(frame,Catalog([Rule('Mock',100,200)]),batch,5000)
            self.assertEqual(chosen.row,4);self.assertEqual(len(rows),5)
            self.assertEqual(reader.number_calls,[(4,'quantity'),(4,'buyout')])
            self.assertTrue(all(r<9 for call in reader.name_calls for r in call))

    def test_cancel_before_ocr(self):
        reader=FakeReader()
        with self.assertRaises(ScanCancelled):Scanner(reader).scan(np.zeros((1080,1920,3),np.uint8),Catalog([]),9,5000,cancel=lambda:True)
        self.assertFalse(reader.name_calls)

    def test_group_and_name_batch_are_independent(self):
        for group in (1,3,9):
            for batch in (1,3,9):
                with self.subTest(group=group,batch=batch):
                    reader=GroupReader({r:('Mock',1,100-r) for r in range(9)})
                    chosen,rows=Scanner(reader).scan(self.frame(),Catalog([Rule('Mock',100,200)]),batch,5000,group_size=group)
                    self.assertEqual(chosen.row,group-1)
                    self.assertEqual([d.row for d in rows],list(range(group)))
                    self.assertEqual(reader.number_calls,[(r,field) for r in range(group) for field in ('quantity','buyout')])
                    expected_names=list(range(((group+batch-1)//batch)*batch))
                    self.assertEqual([r for call in reader.name_calls for r in call],expected_names)

    def test_first_group_with_candidates_wins_and_unknown_numbers_are_skipped(self):
        reader=GroupReader({0:('Mock',1,120),3:('Mock',1,120),4:('Mock',1,90),5:('Mock',1,80),8:('Mock',1,1)})
        chosen,rows=Scanner(reader).scan(self.frame(),Catalog([Rule('Mock',100,200)]),9,5000,group_size=3)
        self.assertEqual(chosen.row,5)
        self.assertEqual([d.row for d in rows],list(range(6)))
        self.assertEqual(reader.number_calls,[(r,field) for r in (0,3,4,5) for field in ('quantity','buyout')])

    def test_metrics_select_different_eligible_candidates(self):
        rules=Catalog([Rule('A',1000,2000),Rule('B',100,300),Rule('C',5000,10000)])
        for metric,expected in (('profit',0),('roi',1)):
            reader=GroupReader({0:('A',1,1000),1:('B',1,100),2:('C',1,3000)})
            chosen,rows=Scanner(reader).scan(self.frame(),rules,3,5000,1000,group_size=3,selection_metric=metric)
            self.assertEqual(chosen.row,expected)
            self.assertEqual(rows[2].reason,'balance_limit')

    def test_roi_uses_exact_values_when_display_percent_is_equal(self):
        reader=GroupReader({0:('A',1,50000),1:('B',1,50000)})
        rules=Catalog([Rule('A',50000,100000),Rule('B',50000,100001)])
        chosen,rows=Scanner(reader).scan(self.frame(),rules,3,100000,group_size=3,selection_metric='roi')
        self.assertEqual(rows[0].roi_percent,rows[1].roi_percent)
        self.assertEqual(chosen.row,1)

    def test_exact_ties_keep_top_row(self):
        for metric,second_qty,second_price in (('profit',1,80),('roi',2,160)):
            reader=GroupReader({0:('Mock',1,80),1:('Mock',second_qty,second_price)})
            chosen,rows=Scanner(reader).scan(self.frame(),Catalog([Rule('Mock',100,200)]),3,5000,group_size=3,selection_metric=metric)
            self.assertEqual(chosen.row,0)
            self.assertEqual(len(rows),3)

    def test_no_candidates_stops_after_nine_rows(self):
        for group in (1,3,9):
            reader=GroupReader({r:('Mock',1,101) for r in range(9)})
            chosen,rows=Scanner(reader).scan(self.frame(),Catalog([Rule('Mock',100,200)]),3,5000,group_size=group)
            self.assertIsNone(chosen)
            self.assertEqual([d.row for d in rows],list(range(9)))
            self.assertEqual(len(reader.number_calls),18)

    def test_cancel_during_last_numeric_ocr_prevents_selection(self):
        for group in (1,3,9):
            reader=GroupReader({r:('Mock',1,80) for r in range(9)})
            cancel=lambda:bool(reader.number_calls and reader.number_calls[-1]==(group-1,'buyout'))
            with self.assertRaises(ScanCancelled):
                Scanner(reader).scan(self.frame(),Catalog([Rule('Mock',100,200)]),9,5000,None,cancel,group_size=group)
            self.assertEqual(len(reader.number_calls),group*2)

    def test_stage_timings_are_disjoint_and_reset_per_scan(self):
        timings={'stale':999,'name_ms':999}
        reader=GroupReader({0:('Mock',1,80),2:('Mock',1,70)})
        scanner=Scanner(reader)
        with patch('stalbot.scanner.perf_counter',side_effect=count(0,0.001)):
            chosen,rows=scanner.scan(self.frame(),Catalog([Rule('Mock',100,200)]),3,5000,group_size=3,timings=timings)
        self.assertEqual(chosen.row,2)
        self.assertEqual(set(timings),{'name_ms','quantity_ms','buyout_ms','evaluation_ms','total_ms'})
        for key,expected in (('name_ms',1),('quantity_ms',2),('buyout_ms',2),('evaluation_ms',6)):
            self.assertAlmostEqual(timings[key],expected)
        self.assertLessEqual(sum(value for key,value in timings.items() if key!='total_ms'),timings['total_ms'])
        self.assertTrue(all(result.elapsed_ms>0 for result in rows))
        with patch('stalbot.scanner.perf_counter',side_effect=count(0,0.001)):
            with self.assertRaises(ScanCancelled):
                scanner.scan(self.frame(),Catalog([]),9,5000,cancel=lambda:True,timings=timings)
        self.assertAlmostEqual(timings['total_ms'],1)
        self.assertTrue(all(value==0 for key,value in timings.items() if key!='total_ms'))

    def test_invalid_group_or_metric_rejected_before_ocr(self):
        for options in ({'group_size':2},{'selection_metric':'display_roi'}):
            reader=GroupReader({})
            with self.assertRaises(ValueError):
                Scanner(reader).scan(self.frame(),Catalog([]),3,5000,**options)
            self.assertFalse(reader.name_calls)


class GroupReader(FakeReader):
    def __init__(self,rows):
        super().__init__()
        self.rows=rows

    def names(self,images,batch_size):
        rows=[int(im[0,0,0]) for im in images]
        self.name_calls.append(rows)
        return [Identity(self.rows[row][0] if row in self.rows else 'Unknown',upgrade=0) for row in rows]

    def number(self,image,field):
        row=int(image[0,0,0])
        self.number_calls.append((row,field))
        return Number('VALUE',self.rows[row][1 if field=='quantity' else 2])


class FakeInterface:
    def auction(self,frame):return True
    def any_menu(self,frame):return False
    def menu(self,frame,row):return True
    def outcome(self,frame):return 'SUCCESS' if frame[0,0,0]==1 else None
    def ok_position(self,status):return (960,585)


class FakeDesktop:
    def move(self,position):pass
    def __init__(self,store,result=1,fail_buy=False):
        self.store=store;self.result=result;self.clicked=[];self.sent=False;self.closed=False;self.fail_buy=fail_buy
    def capture(self):
        frame=np.zeros((1080,1920,3),np.uint8)
        frame[394,937]=200  # visible synthetic name for Search preparation
        if self.sent and not self.closed:frame[0,0,0]=self.result
        return frame
    def click(self,position):
        self.clicked.append(position)
        if position==(1330,448):
            assert len(self.store.unresolved())==1, 'BUY must be durably recorded before input'
            self.sent=True
            if self.fail_buy:raise RuntimeError('Input failed')
        if position==(960,585):self.closed=True


class FixedScanner:
    def __init__(self,changed=False):self.changed=changed
    def row(self,*args):return decision(price=90 if self.changed else 80)


class RunnerTests(StoreFixture,unittest.TestCase):
    def make_runner(self,result=1,fail_buy=False,changed=False):
        desktop=FakeDesktop(self.store,result,fail_buy)
        runner=Runner(FixedScanner(changed),self.store,desktop,FakeInterface(),Config(result_timeout_ms=30),Path(self.tmp.name)/'diagnostics')
        return runner,desktop

    def test_single_buy_success(self):
        self.store.set_balance(1000);runner,desktop=self.make_runner()
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'SUCCESS')
        self.assertEqual(desktop.clicked.count((1330,448)),1);self.assertEqual(self.store.balance(),920)

    def test_session_profit_accumulates_success_only_even_when_stopped_after_buy(self):
        runner,desktop=self.make_runner()
        events=[];runner.emit=events.append
        for _ in range(2):
            desktop.sent=False;desktop.closed=False
            self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:desktop.sent),'SUCCESS')
        self.assertEqual(str(runner.session_profit),'220.00')
        self.assertEqual(runner.session_purchases,2)
        self.assertEqual([e['session_profit'] for e in events if 'session_profit' in e],['110.00','220.00'])
        desktop.sent=False;desktop.closed=False;desktop.result=0
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'UNKNOWN')
        self.assertEqual(str(runner.session_profit),'220.00')
        self.assertEqual(runner.session_purchases,2)

    def test_unknown_and_input_error_never_retry(self):
        for fail in (False,True):
            with self.subTest(fail=fail):
                runner,desktop=self.make_runner(result=0,fail_buy=fail)
                self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'UNKNOWN')
                self.assertEqual(desktop.clicked.count((1330,448)),1)
                self.assertFalse(self.store.unresolved())
                attempt=next(row for row in self.store.history() if row['status']=='UNKNOWN_RECOVERED')
                self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'STALE')
                self.assertEqual(desktop.clicked.count((1330,448)),1)
                self.store.acknowledge(attempt['id'])

    def test_stale_menu_never_buys(self):
        runner,desktop=self.make_runner(changed=True)
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'STALE')
        self.assertEqual(desktop.clicked,[(1030,405)]);self.assertFalse(self.store.history())

    def test_stop_after_buy_still_records_result_without_extra_click(self):
        runner,desktop=self.make_runner()
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:desktop.sent),'SUCCESS')
        self.assertEqual(desktop.clicked,[(1030,405),(1330,448)])
        self.assertEqual(self.store.history()[0]['status'],'SUCCESS')

    def test_cancel_during_result_recapture_does_not_close_notification(self):
        runner,desktop=self.make_runner()
        self.store.set_balance(1000)
        cancelled=threading.Event()
        capture=desktop.capture
        result_captures=0
        def capture_and_cancel():
            nonlocal result_captures
            frame=capture()
            if desktop.sent:
                result_captures+=1
                if result_captures==2:cancelled.set()
            return frame
        desktop.capture=capture_and_cancel
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),cancelled.is_set),'SUCCESS')
        self.assertTrue(cancelled.is_set())
        self.assertEqual(desktop.clicked,[(1030,405),(1330,448)])
        self.assertEqual(self.store.history()[0]['status'],'SUCCESS')
        self.assertEqual(self.store.balance(),920)

    def test_cancel_before_selection(self):
        runner,desktop=self.make_runner()
        with self.assertRaises(ScanCancelled):runner.purchase(decision(),Catalog([decision().rule]),lambda:True)
        self.assertFalse(desktop.clicked)

    def test_cancel_during_durable_write(self):
        runner,desktop=self.make_runner()
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:bool(self.store.unresolved())),'CANCELLED')
        self.assertNotIn((1330,448),desktop.clicked)
        self.assertEqual(self.store.history()[0]['status'],'CANCELLED')
        self.assertFalse(self.store.unresolved())

    def test_cancel_in_mouse_move_before_buy_does_not_leave_unknown(self):
        runner,desktop=self.make_runner()
        self.store.set_balance(1000)
        click=desktop.click
        def cancel_before_down(position):
            if position==(1330,448):
                self.assertEqual(self.store.unresolved()[0]['status'],'BUY_SENT')
                raise ScanCancelled()
            click(position)
        desktop.click=cancel_before_down
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'CANCELLED')
        self.assertEqual(desktop.clicked,[(1030,405)])
        self.assertFalse(desktop.sent);self.assertFalse(self.store.unresolved())
        self.assertEqual(self.store.history()[0]['status'],'CANCELLED')
        self.assertEqual(self.store.balance(),1000)


if __name__=='__main__':unittest.main()
