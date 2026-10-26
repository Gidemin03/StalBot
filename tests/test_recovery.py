"""Recovery, uncertain purchases and cancellation with fake input and time."""
from dataclasses import replace
from unittest.mock import Mock,patch
import threading
import sqlite3
import unittest

from stalbot.domain import Catalog
from stalbot.scanner import Scanner,ScanCancelled
from stalbot.storage import Store
import test_bot as fixtures
import test_search_refresh as refresh
from test_bot import decision


class RecoveryTests(fixtures.StoreFixture,unittest.TestCase):
    def test_recovery_one_second_polls_15ms_and_retries_at_300ms(self):
        runner,desktop=self.make_runner();desktop.sent=True
        times=[];waits=[]
        desktop.click=lambda position:times.append(self.clock.now)
        original_sleep=self.clock.sleep
        def sleep(seconds):waits.append(seconds);original_sleep(seconds)
        with patch('stalbot.runner.time.sleep',sleep):
            with self.assertRaisesRegex(RuntimeError,'восстановить'):runner.recover_auction(lambda:False)
        self.assertEqual(len(times),3)
        self.assertAlmostEqual(times[0],0)
        for previous,current in zip(times,times[1:]):
            self.assertGreaterEqual(current-previous,.3-1e-9)
            self.assertLess(current-previous,.316)
        self.assertGreaterEqual(self.clock.now,1);self.assertLess(self.clock.now,1.02)
        self.assertTrue(all(seconds==.015 for seconds in waits))

    def make_runner(self):
        runner,desktop=fixtures.RunnerTests.make_runner(self)
        self.clock=refresh.Clock()
        self.addCleanup(patch.stopall)
        patch('stalbot.runner.time.monotonic',self.clock.monotonic).start()
        patch('stalbot.runner.time.sleep',self.clock.sleep).start()
        return runner,desktop

    def test_notification_click_is_retried_and_no_delay_follows_close(self):
        runner,desktop=self.make_runner();click=desktop.click;attempts=[]
        def missed_first_ok(position):
            if position==(960,585):
                attempts.append(position)
                if len(attempts)==1:
                    desktop.clicked.append(position);return
            click(position)
        desktop.click=missed_first_ok
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'SUCCESS')
        self.assertEqual(len(attempts),2)
        self.assertTrue(desktop.closed)
        self.assertEqual(desktop.clicked.count((1330,448)),1)
        self.assertLess(self.clock.now,.54)

    def test_unknown_with_remaining_buy_menu_closes_via_search_and_reserves_once(self):
        runner,desktop=self.make_runner();desktop.result=0
        runner.ui.any_menu=lambda frame:desktop.sent and not desktop.closed
        click=desktop.click
        def close_menu(position):
            click(position)
            if position==(1342,345):desktop.closed=True
        desktop.click=close_menu
        self.store.set_balance(1000)
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'UNKNOWN')
        self.assertEqual(desktop.clicked,[(1030,405),(1330,448),(1342,345)])
        self.assertEqual(self.store.balance(),920)
        self.assertFalse(self.store.unresolved())
        attempt=self.store.history()[0]
        self.assertEqual(attempt['status'],'UNKNOWN_RECOVERED')
        other=Store(self.store.path)
        with self.assertRaises(RuntimeError):other.recover_unknown(attempt['id'])
        with self.assertRaises(sqlite3.IntegrityError):other.begin_buy(attempt['id'],decision())
        self.assertEqual(other.balance(),920)
        other.begin_buy('same-price-new-cycle',replace(decision(),row=8))
        other.finish_buy('same-price-new-cycle','UNAVAILABLE')
        other.begin_buy('different',decision(price=70))
        other.finish_buy('different','UNAVAILABLE')
        other.acknowledge(attempt['id'])
        self.assertEqual(other.balance(),920)  # Manual review never guesses a refund.

    def test_stuck_notification_retries_boundedly_without_buy_retry(self):
        runner,desktop=self.make_runner();click=desktop.click
        def stuck(position):
            click(position)
            if position==(960,585):desktop.closed=False
        desktop.click=stuck
        with self.assertRaisesRegex(RuntimeError,'восстановить'):
            runner.purchase(decision(),Catalog([decision().rule]),lambda:False)
        self.assertEqual(desktop.clicked.count((960,585)),3)
        self.assertEqual(desktop.clicked.count((1330,448)),1)
        self.assertEqual(self.store.history()[0]['status'],'SUCCESS')

    def test_unknown_dirty_screen_remains_unresolved(self):
        runner,desktop=self.make_runner();desktop.result=0
        runner.ui.auction=lambda frame:not desktop.sent
        with self.assertRaisesRegex(RuntimeError,'восстановить'):
            runner.purchase(decision(),Catalog([decision().rule]),lambda:False)
        self.assertEqual(self.store.unresolved()[0]['status'],'UNKNOWN')
        self.assertEqual(desktop.clicked,[(1030,405),(1330,448)])

    def test_pause_during_recovery_prevents_retry(self):
        runner,desktop=self.make_runner();desktop.sent=True
        click=desktop.click
        def stuck(position):click(position);desktop.closed=False
        desktop.click=stuck
        with self.assertRaises(ScanCancelled):runner.recover_auction(lambda:bool(desktop.clicked))
        self.assertEqual(desktop.clicked,[(960,585)])

    def test_recognized_unknown_ok_can_be_closed_without_claiming_success(self):
        runner,desktop=self.make_runner();desktop.result=0
        base=type(runner.ui)
        class ModalInterface(base):
            def blocking_modal(self,frame):return desktop.sent and not desktop.closed
            def modal_ok_position(self,frame):return (960,585)
        runner.ui=ModalInterface()
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'UNKNOWN')
        self.assertTrue(desktop.closed)
        self.assertEqual(self.store.history()[0]['status'],'UNKNOWN_RECOVERED')

    def test_failed_recovery_never_reaches_scanner_or_new_buy(self):
        runner,desktop=self.make_runner()
        self.store.save_rule(decision().rule)
        self.store.begin_buy('old',decision());self.store.finish_buy('old','UNKNOWN')
        runner.ui.auction=lambda frame:False
        runner.scanner=Mock();desktop.exclusive=Mock()
        stop=Mock(wraps=threading.Event());stop.wait.return_value=False
        with self.assertRaisesRegex(RuntimeError,'восстановить'):runner.run(True,stop,threading.Event())
        runner.scanner.scan.assert_not_called()
        self.assertFalse(desktop.clicked)
        self.assertEqual(self.store.unresolved()[0]['status'],'UNKNOWN')

    def test_recovered_unknown_does_not_exclude_identical_profitable_candidates(self):
        runner,desktop=self.make_runner()
        self.store.begin_buy('old',decision());self.store.finish_buy('old','UNKNOWN')
        self.store.recover_unknown('old')
        name=decision().identity.name
        reader=fixtures.GroupReader({0:(name,1,80),1:(name,1,70)})
        frame=desktop.capture()
        for row in range(9):frame[393+37*row:430+37*row]=row
        chosen,rows=Scanner(reader).scan(frame,Catalog([decision().rule]),1,1000)
        self.assertEqual(chosen.row,0)
        self.assertTrue(rows[0].buy)

    def test_unknown_allows_same_price_after_real_fresh_search(self):
        runner,desktop=self.make_runner();desktop.result=0
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'UNKNOWN')
        self.assertTrue(runner.needs_fresh_search)
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'STALE')
        self.assertEqual(desktop.clicked.count((1330,448)),1)
        # Use the real Search state machine with a changed, stable list.
        search_desktop=refresh.Desktop(self.clock)
        search_desktop.after_search=lambda frame,elapsed:refresh.mark(frame)
        runner.desktop=search_desktop
        self.assertIsNotNone(runner.fresh(lambda:False))
        self.assertFalse(runner.needs_fresh_search)
        runner.desktop=desktop;desktop.sent=False;desktop.closed=False;desktop.result=1
        self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False),'SUCCESS')
        self.assertEqual(desktop.clicked.count((1330,448)),2)
        self.assertEqual({r['status'] for r in self.store.history()},{'UNKNOWN_RECOVERED','SUCCESS'})

    def test_restart_recovers_unknown_before_fresh_scan(self):
        runner,desktop=self.make_runner();self.store.save_rule(decision().rule)
        self.store.set_balance(100000)
        self.store.begin_buy('old',decision());self.store.finish_buy('old','UNKNOWN')
        desktop.exclusive=Mock()
        stop=Mock(wraps=threading.Event());stop.wait.return_value=False
        def fresh(cancel):
            self.assertFalse(self.store.unresolved())
            self.assertEqual(self.store.balance(),99920)
            stop.set();return None
        runner.fresh=Mock(side_effect=fresh)
        runner.run(True,stop,threading.Event())
        runner.fresh.assert_called_once()
        self.assertFalse(desktop.clicked)


class ExtendedWaitTests(unittest.TestCase):
    setUp=refresh.SearchRefreshTests.setUp
    fresh=refresh.SearchRefreshTests.fresh
    def test_unchanged_search_does_not_release_unknown_purchase_barrier(self):
        self.runner.needs_fresh_search=True
        self.assertIsNone(self.fresh())
        self.assertTrue(self.runner.needs_fresh_search)

    def test_late_stabilization_uses_same_search_and_all_nine_rows(self):
        def update(frame,elapsed):
            refresh.mark(frame,'buyout',8,value=min(30,1+int(elapsed/.005)))
        self.desktop.after_search=update
        self.assertIsNotNone(self.fresh())
        self.assertGreater(self.clock.now,.12)
        self.assertLess(self.clock.now,.24)
        self.assertEqual(self.desktop.clicked,[(1342,345)])
        self.assertEqual(self.runner.refresh_trace['result'],'CHANGED_STABLE')

    def test_clock_crossing_deadline_between_reads_never_sleeps_negative(self):
        def ticking():self.clock.now+=.004;return self.clock.now
        def sleep(seconds):
            self.assertGreaterEqual(seconds,0)
            self.clock.sleep(seconds)
        with patch('stalbot.runner.time.monotonic',ticking),patch('stalbot.runner.time.sleep',sleep):
            self.assertIsNone(self.fresh())
        self.assertEqual(self.runner.refresh_trace['result'],'UNCHANGED')


if __name__=='__main__':unittest.main()
