"""Balance accounting uses temporary databases, fake frames/input and virtual time."""
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import Mock,patch

from stalbot.config import Config
from stalbot.domain import Catalog,Number
from stalbot.scanner import ScanCancelled
from stalbot.storage import Store
import test_bot as fixtures
from test_search_refresh import Clock


class AutoBalanceTests(fixtures.StoreFixture,unittest.TestCase):
    def setup_runner(self,result=1):
        runner,desktop=fixtures.RunnerTests.make_runner(self,result=result)
        runner.config=replace(runner.config,auto_balance=True)
        runner.scanner.reader=Mock()
        self.clock=Clock()
        for target,value in (('time.monotonic',self.clock.monotonic),('time.sleep',self.clock.sleep)):
            p=patch('stalbot.runner.'+target,value);p.start();self.addCleanup(p.stop)
        self.store.set_balance(100000)
        return runner,desktop

    def purchase(self,runner,cancel=lambda:False):
        return runner.purchase(fixtures.decision(),Catalog([fixtures.decision().rule]),cancel)

    def test_success_updates_authoritative_balance_once_after_ok(self):
        runner,desktop=self.setup_runner();events=[];runner.emit=events.append
        def read(*args):
            self.assertTrue(desktop.closed)
            return Number('VALUE',99920)
        with patch('stalbot.balance.read_balance',side_effect=read) as ocr:
            self.assertEqual(self.purchase(runner),'SUCCESS')
        self.assertEqual(ocr.call_count,2)
        self.assertEqual(self.store.balance(),99920)
        self.assertEqual(runner.session_purchases,1)
        self.assertFalse(self.store.unresolved())
        self.assertTrue(any(e.get('account_changed') and e['balance']==99920 for e in events))
        self.assertEqual(desktop.clicked.count((1330,448)),1)

    def test_unknown_waits_out_transient_debit_and_no_reservation(self):
        runner,desktop=self.setup_runner(result=0)
        def read(*args):
            return Number('VALUE',99920 if self.clock.now<.6 else 100000)
        with patch('stalbot.balance.read_balance',side_effect=read):
            self.assertEqual(self.purchase(runner),'UNAVAILABLE')
        self.assertEqual(self.store.balance(),100000)
        self.assertEqual(runner.session_purchases,0)
        self.assertTrue(runner.needs_fresh_search)
        self.assertFalse(self.store.unresolved())

    def test_unknown_exact_debit_resolves_success(self):
        runner,desktop=self.setup_runner(result=0)
        with patch('stalbot.balance.read_balance',return_value=Number('VALUE',99920)):
            self.assertEqual(self.purchase(runner),'SUCCESS')
        check=json.loads(self.store.history()[0]['balance_check'])
        self.assertEqual(check['observed_status'],'UNKNOWN')
        self.assertEqual(check['actual_spend'],80)
        self.assertEqual(runner.session_purchases,1)

    def test_unavailable_waits_for_refund_after_ok(self):
        runner,desktop=self.setup_runner()
        runner.ui.outcome=lambda frame:'UNAVAILABLE' if desktop.sent and not desktop.closed else None
        closed_at=[];click=desktop.click
        def close(position):
            click(position)
            if desktop.closed:closed_at.append(self.clock.now)
        desktop.click=close
        def read(*args):
            self.assertTrue(closed_at)
            return Number('VALUE',99920 if self.clock.now-closed_at[0]<.5 else 100000)
        with patch('stalbot.balance.read_balance',side_effect=read):
            self.assertEqual(self.purchase(runner),'UNAVAILABLE')
        self.assertEqual(self.store.balance(),100000)
        self.assertFalse(self.store.unresolved())
        self.assertEqual(desktop.clicked.count((1330,448)),1)

    def test_mode_switch_cannot_bypass_pending_success(self):
        runner,desktop=self.setup_runner()
        self.assertEqual(self.purchase(runner,lambda:desktop.sent),'SUCCESS')
        row=self.store.unresolved()[0]
        self.assertTrue(row['balance_pending'])
        self.assertEqual(self.store.clear_history(),0)
        self.store.save_rule(fixtures.decision().rule)
        runner.config=replace(runner.config,auto_balance=False)
        stop=Mock(wraps=threading.Event());stop.wait.return_value=False
        with self.assertRaisesRegex(RuntimeError,'незавершённую'):runner.run(True,stop,threading.Event())
        self.assertEqual(desktop.clicked.count((1330,448)),1)
        self.store.acknowledge(row['id'])
        self.assertFalse(self.store.unresolved())
        self.assertEqual(self.store.balance(),100000)

    def test_old_unknown_without_baseline_requires_manual_review_in_auto_mode(self):
        runner,desktop=self.setup_runner()
        self.store.save_rule(fixtures.decision().rule)
        self.store.begin_buy('legacy',fixtures.decision());self.store.finish_buy('legacy','UNKNOWN')
        stop=Mock(wraps=threading.Event());stop.wait.return_value=False
        with self.assertRaisesRegex(RuntimeError,'незавершённую'):runner.run(True,stop,threading.Event())
        self.assertEqual(self.store.balance(),100000)
        self.assertFalse(desktop.clicked)

    def test_mismatched_debit_or_increase_is_logged_and_allows_new_buy_after_restart(self):
        for value in (99900,100000,100100):
            runner,desktop=self.setup_runner()
            with patch('stalbot.balance.read_balance',return_value=Number('VALUE',value)):
                self.assertEqual(self.purchase(runner),'BALANCE_MISMATCH')
            self.assertFalse(self.store.unresolved())
            row=self.store.history()[0]
            self.assertEqual(row['status'],'BALANCE_MISMATCH')
            self.assertFalse(row['balance_pending'])
            check=json.loads(row['balance_check'])
            self.assertEqual(check['expected_spend'],80)
            self.assertEqual(check['actual_spend'],100000-value)
            with self.store.connect() as db:
                event=json.loads(db.execute("SELECT payload FROM events WHERE mode='balance_check' ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual(event,dict(check,attempt=row['id']))
            self.assertEqual(self.store.balance(),value)
            self.assertEqual(runner.session_purchases,0)
            self.assertEqual(self.store.clear_history(),0)
            restarted=Store(self.store.path)
            self.assertFalse(restarted.unresolved())
            next_id='again-'+str(value)
            restarted.begin_buy(next_id,fixtures.decision(),auto_balance=True)
            self.assertEqual(restarted.attempt(next_id)['balance_before'],value)
            restarted.finish_buy(next_id,'CANCELLED')
            self.store.acknowledge(row['id'])
            self.store.clear_history()

    def test_live_continues_to_next_search_after_mismatch(self):
        runner,desktop=self.setup_runner()
        self.store.save_rule(fixtures.decision().rule);desktop.exclusive=Mock()
        stop=Mock(wraps=threading.Event());stop.wait.return_value=False
        searches=[]
        def fresh(cancel):
            searches.append(self.store.balance())
            if len(searches)==2:
                stop.set();return None
            return desktop.capture()
        def scan(*args,**kwargs):
            kwargs['timings']['total_ms']=0
            return fixtures.decision(),[]
        runner.fresh=fresh;runner.scanner.scan=scan
        with patch('stalbot.balance.read_balance',side_effect=[Number('VALUE',100000)]*2+[Number('VALUE',99900)]*4):
            runner.run(True,stop,threading.Event())
        self.assertEqual(searches,[100000,99900])
        self.assertEqual(desktop.clicked.count((1330,448)),1)
        self.assertEqual(self.store.history()[0]['status'],'BALANCE_MISMATCH')
        self.assertFalse(self.store.unresolved())

    def test_unavailable_and_unknown_mismatches_are_nonblocking(self):
        for outcome in ('UNAVAILABLE','UNKNOWN'):
            with self.subTest(outcome=outcome):
                runner,desktop=self.setup_runner(result=0)
                if outcome=='UNAVAILABLE':
                    runner.ui.outcome=lambda frame:'UNAVAILABLE' if desktop.sent and not desktop.closed else None
                with patch('stalbot.balance.read_balance',return_value=Number('VALUE',99900)):
                    self.assertEqual(self.purchase(runner),'BALANCE_MISMATCH')
                row=self.store.history()[0]
                self.assertEqual(json.loads(row['balance_check'])['observed_status'],outcome)
                self.assertFalse(self.store.unresolved())
                self.assertEqual(runner.session_purchases,0)
                self.assertEqual(desktop.clicked.count((1330,448)),1)
                self.assertEqual(runner.needs_fresh_search,outcome=='UNKNOWN')
                self.store.acknowledge(row['id']);self.store.clear_history()

    def test_cancel_after_buy_persists_pending_and_restart_reconciles(self):
        runner,desktop=self.setup_runner(result=0)
        self.assertEqual(self.purchase(runner,lambda:desktop.sent),'UNKNOWN')
        row=Store(self.store.path).unresolved()[0]
        self.assertEqual(row['balance_before'],100000)
        self.assertEqual(self.store.balance(),100000)
        with self.assertRaises(RuntimeError):self.store.recover_unknown(row['id'])
        runner.session_attempts.clear()
        with patch('stalbot.balance.read_balance',return_value=Number('VALUE',99920)):
            self.assertEqual(runner.reconcile_purchase(row['id'],lambda:False),'SUCCESS')
        self.assertEqual(runner.session_purchases,0)  # Old session is not current profit.
        with self.assertRaises(RuntimeError):self.store.reconcile_balance(row['id'],99920)

    def test_unreadable_or_changing_balance_leaves_attempt_pending(self):
        runner,desktop=self.setup_runner()
        with patch('stalbot.balance.read_balance',return_value=Number('AMBIGUOUS',None)):
            with self.assertRaisesRegex(RuntimeError,'Баланс не прочитан'):self.purchase(runner)
        self.assertTrue(self.store.unresolved()[0]['balance_pending'])
        self.assertEqual(self.store.balance(),100000)
        self.assertEqual(runner.session_purchases,0)

    def test_stable_read_resets_on_ambiguous_sample_and_cancels(self):
        runner,desktop=self.setup_runner()
        with patch('stalbot.balance.read_balance',side_effect=[Number('VALUE',1),Number('AMBIGUOUS',None),Number('VALUE',2),Number('VALUE',2)]) as read:
            self.assertEqual(runner.read_stable_balance(lambda:False),2)
        self.assertEqual(read.call_count,4)
        with self.assertRaises(ScanCancelled):runner.sync_balance(lambda:True,'test')
        self.assertEqual(self.store.balance(),100000)

    def test_start_sync_happens_before_low_budget_guard_and_periodic_is_not_every_search(self):
        runner,desktop=self.setup_runner()
        self.store.save_rule(fixtures.decision().rule);self.store.set_balance(0)
        desktop.exclusive=Mock()
        stop=Mock(wraps=threading.Event());stop.wait.return_value=False
        runner.scanner.scan=Mock(return_value=(None,[]))
        searches=[]
        def fresh(cancel):
            searches.append(self.clock.now)
            if len(searches)==3:self.clock.now+=61
            if len(searches)==5:stop.set()
            return None
        runner.fresh=fresh
        with patch('stalbot.balance.read_balance',return_value=Number('VALUE',100000)) as read:
            runner.run(True,stop,threading.Event())
        self.assertEqual(len(searches),5)
        self.assertEqual(read.call_count,4)  # Start + one periodic check; two frames each.

    def test_resume_sync_before_next_search(self):
        runner,desktop=self.setup_runner()
        self.store.save_rule(fixtures.decision().rule);desktop.exclusive=Mock()
        pause=threading.Event();stop=Mock(wraps=threading.Event());stop.wait.return_value=False
        searches=[]
        def fresh(cancel):
            searches.append(self.store.balance())
            if len(searches)==1:pause.set()
            else:stop.set()
            return None
        def wait(seconds):
            if seconds==.1:pause.clear()
            return False
        stop.wait.side_effect=wait;runner.fresh=fresh
        with patch('stalbot.balance.read_balance',side_effect=[Number('VALUE',100000)]*2+[Number('VALUE',90000)]*2):
            runner.run(True,stop,pause)
        self.assertEqual(searches,[100000,90000])

    def test_auto_balance_config_roundtrip_and_strict_bool(self):
        path=self.store.path.with_suffix('.json')
        Config(auto_balance=True).save(path)
        self.assertTrue(Config.load(path).auto_balance)
        for value in (1,'yes',None):
            with self.assertRaises(ValueError):Config(auto_balance=value).validate()
