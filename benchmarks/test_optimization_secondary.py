import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np
from optimization_secondary import (BoundedWriter, NarrowFallback, ROOT, classify,
    interface_cases, same_selected_fields, unconditional_price_rejection, summary)
from stalbot.domain import Number
from stalbot.interface import Interface
from stalbot.vision import BOXES
from stalbot.storage import Store, lot_key
from optimization_secondary import synthetic_decision


class SecondaryTests(unittest.TestCase):
    def test_interface_unknown_conflict_and_moved(self):
        normal, candidate = Interface(ROOT / 'assets/ui'), NarrowFallback(ROOT / 'assets/ui')
        cases = interface_cases(normal)
        for name, frame in cases.items():
            self.assertEqual(classify(normal, frame), classify(candidate, frame), name)
        self.assertEqual(classify(candidate, cases['synthetic_unknown'])[2:], (True, None))
        self.assertIsNone(classify(candidate, cases['synthetic_conflict'])[3])
        self.assertEqual(classify(candidate, cases['synthetic_moved'])[3], 'SUCCESS')

    def test_each_selected_field_invalidates_reuse(self):
        before = np.zeros((1080, 1920, 3), dtype=np.uint8)
        self.assertTrue(same_selected_fields(before, before.copy(), 0))
        for x, y, _, _ in BOXES.values():
            after = before.copy(); after[y, x] = 1
            self.assertFalse(same_selected_fields(before, after, 0))
        self.assertTrue(same_selected_fields(before, after, 1))

    def test_price_gate_never_uses_unit_limit(self):
        self.assertIsNone(unconditional_price_rejection(Number('VALUE', 999), 1000, None))
        self.assertIsNone(unconditional_price_rejection(Number('VALUE', 1000), 1000, 1000))
        self.assertEqual(unconditional_price_rejection(Number('VALUE', 1001), 1000, None), 'lot_price_limit')
        self.assertEqual(unconditional_price_rejection(Number('VALUE', 1000), 1000, 999), 'balance_limit')
        for number in (Number('AMBIGUOUS', None), Number('ABSENT', None), Number('VALUE', True), Number('VALUE', 10_000_000_000)):
            self.assertEqual(unconditional_price_rejection(number, 1000, None), 'unknown_price')

    def test_writer_drains_and_owns_work(self):
        calls = []
        owner = threading.get_ident()
        writer = BoundedWriter(lambda x: calls.append((x, threading.get_ident())), capacity=1)
        for i in range(10): writer.submit(i)
        writer.close()
        self.assertEqual([c[0] for c in calls], list(range(10)))
        self.assertTrue(all(c[1] != owner for c in calls))
        self.assertFalse(writer.thread.is_alive())

    def test_writer_error_does_not_hang_close(self):
        writer = BoundedWriter(Mock(side_effect=OSError('full')), capacity=1)
        try:
            for i in range(3): writer.submit(i)
        except RuntimeError: pass
        with self.assertRaises(RuntimeError): writer.close()
        self.assertFalse(writer.thread.is_alive())

    def test_summary_keeps_tail(self):
        result = summary([1, 2, 3, 4, 100])
        self.assertEqual(result['n'], 5)
        self.assertEqual(result['p50_ms'], 3)
        self.assertGreater(result['p95_ms'], 4)

    def test_cycle_snapshot_never_replaces_final_buy_transaction(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.sqlite3')
            decision = synthetic_decision(100)
            stale_keys = set()
            with store.connect() as db:
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                           ('unknown', 'now', 'UNKNOWN_RECOVERED', json.dumps(decision.to_dict()), None, 'now'))
            self.assertNotIn(lot_key(decision.to_dict()), stale_keys)
            with self.assertRaises(RuntimeError): store.begin_buy('repeat', decision)
            self.assertEqual(len(store.history()), 1)


if __name__ == '__main__': unittest.main()
