"""Catalog ranges, legacy migrations and history; only temporary SQLite databases."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path

from stalbot.domain import Catalog,Identity,Number,Rule,evaluate
from stalbot.storage import Store
from stalbot.runner import same_lot


def decision(name='Янтарник',price=80,sell=200,grade=12):
    rule=Rule(name,1000,sell,color='red',upgrade=10,upgrade_max=13)
    return evaluate(0,Identity(name,'red',grade),rule,Number('VALUE',1),Number('VALUE',price),5000)


class RangeTests(unittest.TestCase):
    def test_lot_revalidation_rejects_grade_change_inside_same_range(self):
        before=decision(grade=11);after=decision(grade=12)
        self.assertTrue(before.buy);self.assertTrue(after.buy)
        self.assertEqual(Catalog([before.rule]).match(after.identity)[0],before.rule)
        self.assertFalse(same_lot(before,after))

    def test_adjacent_ranges_select_their_own_prices_at_every_level(self):
        rules=[Rule('Янтарник',100+i,200+i,color='red',upgrade=start,upgrade_max=end)
               for i,(start,end) in enumerate(((0,9),(10,13),(14,15)))]
        catalog=Catalog(rules)
        for level in range(16):
            with self.subTest(level=level):
                self.assertEqual(catalog.match(Identity('Янтарник','red',level)),
                    (rules[0 if level<10 else 1 if level<14 else 2],'matched'))
        for level in (None,-1,16):
            self.assertEqual(catalog.match(Identity('Янтарник','red',level))[1],'unknown_variant')
        self.assertEqual(catalog.match(Identity('Янтарник','blue',12))[1],'unknown_variant')

    def test_overlapping_exact_range_and_wildcard_are_ambiguous(self):
        rule=Rule('Янтарник',100,200,upgrade=10,upgrade_max=13)
        for other in (replace(rule,upgrade=13,upgrade_max=15),replace(rule,upgrade=12,upgrade_max=None),
                      replace(rule,upgrade=None,upgrade_max=None)):
            self.assertEqual(Catalog([rule,other]).match(Identity('Янтарник',upgrade=13 if other.upgrade==13 else 12))[1],
                             'ambiguous_variant')
        self.assertEqual(Catalog([rule,replace(rule,enabled=False)]).match(Identity('Янтарник',upgrade=12))[0],rule)

    def test_invalid_ranges_and_legacy_exact(self):
        rule=Rule('Янтарник',100,200,upgrade=10)
        for start,end in ((None,10),(14,13),(0,16),(-1,9),(0,True),(0,9.5)):
            with self.subTest(start=start,end=end),self.assertRaises(ValueError):
                replace(rule,upgrade=start,upgrade_max=end).validate()
        self.assertTrue(rule.matches_upgrade(10));self.assertFalse(rule.matches_upgrade(11))
        self.assertEqual(rule.upgrade_label,'10')
        self.assertEqual(replace(rule,upgrade_max=13).upgrade_label,'10-13')


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.sqlite3';self.store=Store(self.path)

    def tearDown(self):self.tmp.cleanup()

    def add(self,key,*,name='Янтарник',price=80,sell=200,status='SUCCESS',created='2026-10-18T10:00:00+00:00'):
        self.store.begin_buy(key,decision(name,price,sell))
        self.store.finish_buy(key,status)
        with self.store.connect() as db:db.execute('UPDATE attempts SET created=? WHERE id=?',(created,key))

    def ids(self,**kwargs):return [row['id'] for row in self.store.history(**kwargs)]

    def test_range_roundtrip_edit_and_unique_exact(self):
        key=self.store.save_rule(decision().rule)
        rule=self.store.rules()[0]
        self.assertEqual((rule.id,rule.upgrade,rule.upgrade_max),(key,10,13))
        self.store.save_rule(replace(rule,upgrade=14,upgrade_max=15))
        self.assertEqual(Store(self.path).rules()[0].upgrade_label,'14-15')
        self.store.save_rule(replace(rule,id=None,upgrade=0,upgrade_max=None))
        with self.assertRaises(sqlite3.IntegrityError):self.store.save_rule(replace(rule,id=None,upgrade=0,upgrade_max=0))

    def test_combined_unicode_status_inclusive_date_and_roi_filters(self):
        self.add('a',created='2026-10-17T00:00:00+00:00')
        self.add('b',price=100,created='2026-10-18T23:59:59+00:00')
        self.add('c',name='Броня',status='UNAVAILABLE')
        self.assertEqual(set(self.ids(name='ЯНТАРНИК',status='SUCCESS',date_from='2026-10-17',date_to='2026-10-18')),{'a','b'})
        self.assertEqual(self.ids(min_roi='100',status='SUCCESS'),['a'])
        self.assertEqual(self.ids(name='ЯНТ',min_roi='90',date_from='2026-10-18',date_to='2026-10-18'),['b'])
        self.assertEqual(self.ids(name="%' OR 1=1 --"),[])
        for filters in ({'date_from':'2026-02-30'},{'date_from':'2026-10-19','date_to':'2026-10-18'},
                        {'min_roi':'NaN'},{'sort_by':'roi; DROP TABLE attempts'}):
            with self.subTest(filters=filters),self.assertRaises(ValueError):self.store.history(**filters)

    def test_numeric_sort_filter_and_sort_precede_limit(self):
        self.add('low',price=100,sell=200)  # 90%
        self.add('high',price=80,sell=200)  # 137.5%; lexical order would be wrong.
        self.assertEqual(self.ids(sort_by='roi'),['high','low'])
        self.assertEqual(self.ids(sort_by='roi',descending=False),['low','high'])
        self.assertEqual(self.ids(sort_by='price',descending=False),['high','low'])
        self.assertEqual(self.ids(sort_by='profit'),['high','low'])
        # An optional API page must not change the default: all records are returned.
        with self.store.connect() as db:
            payload=json.dumps(decision('Другой',price=100).to_dict())
            db.executemany('INSERT INTO attempts(id,created,status,decision,updated) VALUES(?,?,?,?,?)',
                [(f'new-{i}','2026-10-19','SUCCESS',payload,'2026-10-19') for i in range(205)])
        self.assertEqual(self.ids(name='ЯНТАРНИК',sort_by='roi',limit=1),['high'])
        self.assertEqual(self.ids(sort_by='roi',limit=1),['high'])
        self.assertEqual(len(self.ids()),207)
        self.assertEqual(len(self.ids(limit=200)),200)
        self.assertEqual(self.store.attempt('high')['id'],'high')

    def test_local_date_filters_cover_local_midnight_boundaries(self):
        local_start=datetime(2026,10,17).astimezone()
        utc_start=local_start.astimezone(timezone.utc)
        self.add('before',created=(utc_start-timedelta(microseconds=1)).isoformat())
        self.add('start',created=utc_start.isoformat())
        self.add('end',created=(utc_start+timedelta(days=1,microseconds=-1)).isoformat())
        self.add('after',created=(utc_start+timedelta(days=1)).isoformat())
        self.assertEqual(set(self.ids(date_from='2026-10-17',date_to='2026-10-17',local_dates=True)),{'start','end'})

    def test_clear_keeps_budget_audit_and_all_buy_guards_after_restart(self):
        self.store.set_balance(1000)
        self.add('success');self.add('unavailable',status='UNAVAILABLE');self.add('cancelled',status='CANCELLED')
        self.add('reviewed',status='UNKNOWN');self.store.acknowledge('reviewed')
        self.add('uncertain',status='UNKNOWN');self.store.recover_unknown('uncertain')
        self.add('pending',name='Броня',status='INSUFFICIENT')
        balance=self.store.balance()
        self.assertEqual(self.store.clear_history(),4)
        other=Store(self.path)
        self.assertEqual(set(row['id'] for row in other.history()),{'pending','uncertain'})
        self.assertEqual(other.balance(),balance)
        self.assertEqual(other.attempt('success')['status'],'SUCCESS')
        with self.assertRaises(RuntimeError):other.begin_buy('repeat',decision())
        other.acknowledge('pending')
        other.begin_buy('repeat',decision())
        other.finish_buy('repeat','UNAVAILABLE')
        self.assertEqual(other.clear_history(),2)
        self.assertEqual(other.history()[0]['id'],'uncertain')

    def test_clear_does_not_hide_unknown_or_buy_sent(self):
        self.store.begin_buy('pending',decision())
        self.assertEqual(self.store.clear_history(),0)
        self.assertEqual(self.ids(),['pending'])
        self.store.finish_buy('pending','UNKNOWN')
        self.assertEqual(self.store.clear_history(),0)
        self.assertEqual(self.store.unresolved()[0]['id'],'pending')

    def test_legacy_database_migrates_repeatedly_without_changing_data(self):
        legacy=Path(self.tmp.name)/'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db,db:
            db.executescript('''CREATE TABLE rules(id INTEGER PRIMARY KEY,name TEXT NOT NULL,buy_unit INTEGER NOT NULL,
                sell_unit INTEGER NOT NULL,color TEXT NOT NULL,upgrade INTEGER,max_stack INTEGER,enabled INTEGER NOT NULL);
                CREATE UNIQUE INDEX rule_variant ON rules(name,color,ifnull(upgrade,-1));
                INSERT INTO rules VALUES(1,'Янтарник',100,200,'red',15,NULL,1);
                INSERT INTO rules VALUES(2,'Броня',100,200,'*',NULL,NULL,1);
                CREATE TABLE attempts(id TEXT PRIMARY KEY,created TEXT NOT NULL,status TEXT NOT NULL,
                decision TEXT NOT NULL,diagnostic TEXT,updated TEXT NOT NULL);''')
            old=decision().to_dict();old['rule'].pop('upgrade_max')
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',('old','2026-10-17','UNKNOWN_RECOVERED',json.dumps(old),None,'2026-10-17'))
        for _ in range(2):
            store=Store(legacy)
            self.assertEqual([(r.upgrade,r.upgrade_max) for r in store.rules()],[(None,None),(15,None)])
            self.assertEqual(store.clear_history(),0)
            self.assertNotIn('upgrade_max',json.loads(store.history()[0]['decision'])['rule'])
        store.begin_buy('same-price',decision());store.finish_buy('same-price','CANCELLED')
        store.save_rule(Rule('Янтарник',100,200,color='red',upgrade=0,upgrade_max=9))
        store.begin_buy('new',decision('Броня'));store.finish_buy('new','SUCCESS')


if __name__=='__main__':unittest.main()
