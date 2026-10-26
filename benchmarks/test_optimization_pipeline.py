"""Fast fake-native pipeline contract tests; no models/input or working catalog."""
import threading
import unittest

import numpy as np

from benchmarks.optimization_cache import decision_signature
from benchmarks.optimization_pipeline import PipelineScanner, compare_strategy
from benchmarks.optimization_devices import Spec
from stalbot.domain import Catalog, Identity, Number, Rule
from stalbot.scanner import Scanner, ScanCancelled
from stalbot.vision import BOXES, STEP


def make_frame():
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    for row in range(9):
        for field, (x1, y1, x2, y2) in BOXES.items():
            frame[y1+STEP*row:y2+STEP*row, x1:x2] = row+1
    return frame


class FakeNative:
    """Facade allows dispatch threads; numeric API enforces its actual owner."""

    def __init__(self):
        self.owner = threading.get_ident()
        self.calls = []
        self.number_calls = []
        self.next_seen = threading.Event()
        self.overlap_required = False
        self.fail_row = None
        self.cancelled = False
        self.cancel_during_number = False

    def names(self, images, batch_size):
        indices = [int(image[0, 0, 0])-1 for image in images]
        self.calls.append(indices)
        if 1 in indices:
            self.next_seen.set()
        if self.fail_row is not None and self.fail_row in indices:
            raise RuntimeError('fake NAME worker failed')
        return [Identity(str(index+1), 'white', 0) for index in indices]

    def number(self, image, field):
        if threading.get_ident() != self.owner:
            raise RuntimeError('numeric owner violated')
        row = int(image[0, 0, 0])-1
        self.number_calls.append((row, field))
        if self.overlap_required and len(self.number_calls) == 1:
            if not self.next_seen.wait(2):
                raise AssertionError('next NAME did not overlap first numeric OCR')
        if self.cancel_during_number:
            self.cancelled = True
        return Number('AMBIGUOUS', None, '') if field == 'quantity' else Number('VALUE', 10, '10')


def catalog(rows):
    return Catalog([Rule(str(row+1), 100, 100*(row+1), 'white', 0) for row in rows])


class PipelineTests(unittest.TestCase):
    def test_actual_device_adapter_baseline_and_thread_safe_strategy_pipeline(self):
        # Regression: passing DeviceStrategyReader to PipelineScanner sends the
        # main-owner adapter into prefetch executor and fails its owner guard.
        native = FakeNative()

        class FakeStrategy:
            spec = Spec('fake_cpu', ('cpu',), batch=1)

            def __init__(self):
                self.dispatch_threads = set()

            def read(self, images):
                self.dispatch_threads.add(threading.get_ident())
                return native.names(images, self.spec.batch)

            def names(self, images, batch_size):
                return self.read(images)

        strategy = FakeStrategy()
        values = compare_strategy(native, strategy, [('fake_source', make_frame())], 1, [1])
        self.assertEqual(len(values), 4*3*2*2)
        self.assertTrue(all(value['semantic_agreement'] for value in values))
        self.assertIn(threading.get_ident(), strategy.dispatch_threads)
        self.assertGreaterEqual(len(strategy.dispatch_threads), 2)

    def test_matches_production_all_groups_packets_metrics_scenarios(self):
        frame = make_frame()
        for group in (1, 3, 9):
            for batch in (1, 3, 9):
                for metric in ('profit', 'roi'):
                    for rows in ([], [0], [8], list(range(9)), [1, 2, 7]):
                        with self.subTest(group=group, batch=batch, metric=metric, rows=rows):
                            native = FakeNative()
                            rules = catalog(rows)
                            expected = Scanner(native).scan(frame, rules, batch, 100,
                                                            group_size=group, selection_metric=metric)
                            pipeline = PipelineScanner(native, native)
                            try:
                                observed = pipeline.scan(frame, rules, batch, 100,
                                                         group_size=group, selection_metric=metric)
                                self.assertEqual(decision_signature(*observed), decision_signature(*expected))
                                self.assertEqual(pipeline.last_stats['excess_rows'], [])
                                self.assertEqual(pipeline.last_stats['max_inflight_packets'], 1)
                                if observed[0]:
                                    last_group_end = (observed[0].row//group+1)*group
                                    self.assertTrue(all(row < last_group_end for row in pipeline.last_stats['name_submitted_rows']))
                            finally:
                                pipeline.close()

    def test_next_name_really_overlaps_numeric_current_group(self):
        native = FakeNative()
        native.overlap_required = True
        pipeline = PipelineScanner(native, native)
        timings = {}
        try:
            chosen, _ = pipeline.scan(make_frame(), catalog(range(9)), 1, 100, group_size=3, timings=timings)
            self.assertEqual(chosen.row, 2)
            self.assertEqual(pipeline.last_stats['name_submitted_rows'], [0, 1, 2])
            self.assertEqual(native.number_calls, [(row, field) for row in range(3) for field in ('quantity', 'buyout')])
            self.assertGreater(timings['total_ms'], 0)
        finally:
            pipeline.close()

    def test_cancel_after_numeric_drains_discards_prefetch_and_no_next_group(self):
        native = FakeNative()
        native.cancel_during_number = True
        pipeline = PipelineScanner(native, native)
        try:
            with self.assertRaises(ScanCancelled):
                pipeline.scan(make_frame(), catalog(range(9)), 1, 100,
                              cancel=lambda: native.cancelled, group_size=3)
            self.assertTrue(pipeline.last_stats['cancelled'])
            self.assertEqual(pipeline.last_stats['name_submitted_rows'], [0, 1])
            self.assertEqual(pipeline.last_stats['name_consumed_rows'], [0])
            self.assertEqual(pipeline.last_stats['excess_rows'], [1])
            self.assertIn(1, pipeline.last_stats['name_completed_rows'])
            native.cancelled = native.cancel_during_number = False
            chosen, _ = pipeline.scan(make_frame(), catalog(range(9)), 1, 100, group_size=1)
            self.assertEqual(chosen.row, 0)
            self.assertEqual(pipeline.last_stats['name_submitted_rows'], [0])
        finally:
            pipeline.close()

    def test_cancel_before_dispatch_submits_no_work(self):
        native = FakeNative()
        pipeline = PipelineScanner(native, native)
        try:
            with self.assertRaises(ScanCancelled):
                pipeline.scan(make_frame(), catalog(range(9)), 1, 100, cancel=lambda: True)
            self.assertEqual(native.calls, [])
        finally:
            pipeline.close()

    def test_name_worker_error_propagates_and_pipeline_reusable(self):
        native = FakeNative()
        native.fail_row = 1
        pipeline = PipelineScanner(native, native)
        try:
            with self.assertRaisesRegex(RuntimeError, 'NAME worker failed'):
                pipeline.scan(make_frame(), catalog(range(9)), 1, 100, group_size=3)
            native.fail_row = None
            self.assertIsNotNone(pipeline.scan(make_frame(), catalog(range(9)), 1, 100)[0])
        finally:
            pipeline.close()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            pipeline.scan(make_frame(), catalog(range(9)), 1, 100)

    def test_ties_exclusion_and_budget_keep_production_behavior(self):
        frame = make_frame()
        rules = Catalog([Rule(str(row+1), 100, 100, 'white', 0) for row in range(9)])
        native = FakeNative()
        pipeline = PipelineScanner(native, native)
        try:
            chosen, _ = pipeline.scan(frame, rules, 1, 100, group_size=9)
            self.assertEqual(chosen.row, 0)
            chosen, _ = pipeline.scan(frame, rules, 1, 100, group_size=9,
                                      exclude=lambda decision: decision.row == 0)
            self.assertEqual(chosen.row, 1)
            self.assertIsNone(pipeline.scan(frame, rules, 1, 100, balance=9, group_size=9)[0])
        finally:
            pipeline.close()


if __name__ == '__main__':
    unittest.main()
