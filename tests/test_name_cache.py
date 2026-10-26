"""Production NAME cache integration, using fake native OCR and no game input."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import numpy as np

from stalbot.domain import Catalog, Identity, Number, Rule
from stalbot.name_cache import CachedNameReader, validate_capacity
from stalbot.ocr_session import OcrSession
from stalbot.scanner import Scanner
from stalbot.vision import BOXES, STEP


def image(value=24, shape=(19, 213, 3)):
    return np.full(shape, value, dtype=np.uint8)


class FakeReader:
    def __init__(self, prefix='Item'):
        self.owner = threading.get_ident()
        self.prefix = prefix
        self.name_calls = []
        self.number_calls = []
        self.warmups = []
        self.closed = False
        self.fail_warmup = False
        self.result_override = None

    def check_thread(self):
        if self.owner != threading.get_ident():
            raise RuntimeError('wrong owner')

    def names(self, images, batch_size):
        self.check_thread()
        if self.closed:
            raise RuntimeError('reader closed')
        values = [int(im[0, 0, 0]) for im in images]
        self.name_calls.append((values, batch_size))
        if self.result_override is not None:
            return self.result_override
        return [Identity(f'{self.prefix} {value}', 'white', 0) for value in values]

    def number(self, im, field):
        self.check_thread()
        value = int(im[0, 0, 0])
        self.number_calls.append((value, field))
        return Number('AMBIGUOUS', None, '') if not value else Number('VALUE', value, str(value))

    def warmup(self, batch_size):
        self.check_thread()
        self.warmups.append(batch_size)
        if self.fail_warmup:
            raise RuntimeError('warmup failed')
        self.names([image() for _ in range(batch_size)], batch_size)
        return 1.0

    def close(self):
        self.check_thread()
        self.closed = True


class NameCacheTests(unittest.TestCase):
    def test_mixed_hits_only_infer_misses_in_original_batch_and_order(self):
        native = FakeReader()
        reader = CachedNameReader(native, 9)
        first = reader.names([image(1)], 1)[0]
        values = reader.names([image(2), image(1), image(3)], 3)
        self.assertEqual([value.name for value in values], ['Item 2', 'Item 1', 'Item 3'])
        self.assertIs(values[1], first)
        self.assertEqual(native.name_calls, [([1], 1), ([2, 3], 3)])
        self.assertEqual(reader.names([image(2), image(1), image(3)], 3), values)
        self.assertEqual(len(native.name_calls), 2)
        self.assertEqual((reader.stats()['hits'], reader.stats()['misses']), (4, 3))
        with self.assertRaises(FrozenInstanceError):
            first.name = 'changed'

    def test_original_rgb_shape_and_snapshot_determine_exact_equality(self):
        native = FakeReader()
        reader = CachedNameReader(native, 9)
        source = image()
        original = reader.names([source], 1)
        source[0, 0, 1] += 1  # A color-channel change outside the fake text decoder.
        reader.names([source], 1)
        self.assertEqual(reader.names([image()], 1), original)
        reader.names([image(shape=(213, 19, 3))], 1)
        self.assertEqual(len(native.name_calls), 3)
        self.assertEqual(reader.stats()['entries'], 3)
        self.assertEqual(reader.stats()['pixel_bytes'], 3 * 19 * 213 * 3)

    def test_noncontiguous_crop_hits_its_contiguous_copy(self):
        native = FakeReader()
        reader = CachedNameReader(native)
        source = np.arange(19 * 426 * 3, dtype=np.uint8).reshape(19, 426, 3)[:, ::2]
        self.assertFalse(source.flags.c_contiguous)
        self.assertEqual(reader.names([source], 1), reader.names([source.copy()], 1))
        self.assertEqual(len(native.name_calls), 1)
        for invalid in (image().astype(np.float32), np.zeros((19, 213), np.uint8),
                        np.zeros((19, 213, 4), np.uint8)):
            with self.subTest(shape=invalid.shape, dtype=invalid.dtype):
                with self.assertRaises(ValueError):
                    reader.names([invalid], 1)

    def test_lru_eviction_resize_and_long_overflow_stay_bounded(self):
        native = FakeReader()
        reader = CachedNameReader(native, 2)
        for value in (1, 2, 1, 3, 2):
            reader.names([image(value)], 1)
        self.assertEqual(native.name_calls, [([1], 1), ([2], 1), ([3], 1), ([2], 1)])
        self.assertEqual(reader.stats()['evictions'], 2)
        reader.configure(1)
        reader.names([image(2)], 1)  # Most recently used observation survives shrinking.
        self.assertEqual(len(native.name_calls), 4)
        for value in range(4, 100):
            reader.names([image(value)], 1)
            self.assertEqual(reader.stats()['entries'], 1)
            self.assertEqual(reader.stats()['pixel_bytes'], 19 * 213 * 3)
        reader.configure(0)
        self.assertEqual(reader.stats()['entries'], 0)
        self.assertEqual(reader.stats()['pixel_bytes'], 0)
        reader.reset_stats()
        with patch.object(reader, '_key', side_effect=AssertionError('disabled cache made key')):
            reader.names([image(99)], 1)
            reader.names([image(99)], 1)
        self.assertEqual(reader.stats()['hits'], 0)
        self.assertEqual(reader.stats()['misses'], 0)
        reader.configure(2)
        reader.names([image(99)], 1)
        self.assertEqual(reader.stats()['misses'], 1)

    def test_capacities_reject_bool_float_negative_and_excessive_values(self):
        for capacity in (-1, True, 1.5, 100_001):
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError):
                    validate_capacity(capacity)
                with self.assertRaises(ValueError):
                    CachedNameReader(FakeReader(), capacity)
        for capacity in (0, 1, 1000, 100_000):
            validate_capacity(capacity)

    def test_invalid_native_result_never_partially_populates_cache(self):
        native = FakeReader()
        reader = CachedNameReader(native, 9)
        reader.names([image(1)], 1)
        for result, error in (([], RuntimeError), ([Identity('valid'), {}], TypeError)):
            native.result_override = result
            with self.assertRaises(error):
                reader.names([image(2), image(3)], 3)
            self.assertEqual(reader.stats()['entries'], 1)
        native.result_override = None
        reader.names([image(2), image(3)], 3)
        self.assertEqual(native.name_calls[-1], ([2, 3], 3))

    def test_hits_still_validate_owner_and_batch(self):
        reader = CachedNameReader(FakeReader())
        reader.names([image()], 1)
        for images, batch in (([], 1), ([image(), image()], 1), ([image()], 2)):
            with self.assertRaises(ValueError):
                reader.names(images, batch)
        with ThreadPoolExecutor(max_workers=1) as executor:
            operations = (lambda: reader.names([image()], 1), reader.stats,
                          lambda: reader.configure(2), reader.clear, reader.reset_stats,
                          lambda: reader.warmup(1), lambda: reader.number(image(), 'quantity'), reader.close)
            for operation in operations:
                with self.assertRaisesRegex(RuntimeError, 'wrong owner'):
                    executor.submit(operation).result(timeout=5)

    def test_warmup_bypasses_cache_and_counters_for_all_batch_sizes(self):
        native = FakeReader()
        reader = CachedNameReader(native)
        reader.names([image()], 1)
        before = reader.stats()
        for batch in (1, 3, 9):
            reader.warmup(batch)
        self.assertEqual(native.warmups, [1, 3, 9])
        self.assertEqual([batch for _, batch in native.name_calls], [1, 1, 3, 9])
        self.assertEqual(reader.stats(), before)

    def test_cache_is_scoped_to_reader_and_close_releases_observations(self):
        first = CachedNameReader(FakeReader('First'))
        second = CachedNameReader(FakeReader('Second'))
        self.assertNotEqual(first.names([image()], 1), second.names([image()], 1))
        first.close()
        self.assertTrue(first.reader.closed)
        self.assertFalse(first._entries)
        self.assertEqual(first._pixel_bytes, 0)
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            first.names([image()], 1)


class NameCacheSessionTests(unittest.TestCase):
    def make_session(self):
        self.readers = []

        def factory():
            reader = CachedNameReader(FakeReader(), 9)
            self.readers.append(reader)
            return reader

        session = OcrSession(factory)
        self.addCleanup(lambda: session.close_async().result(timeout=5))
        return session

    def test_session_reuses_cache_and_reconfigures_without_native_reload(self):
        session = self.make_session()

        def run(current, batch, capacity):
            reader, metadata = current.get_reader(batch)
            reader.configure(capacity)
            value = reader.names([image(1)], batch)
            return reader, metadata, value, reader.stats()

        first, initial, value, _ = session.submit(lambda current: run(current, 1, 9)).result(timeout=5)
        reused, metadata, same, stats = session.submit(lambda current: run(current, 3, 2)).result(timeout=5)
        self.assertIs(first, reused)
        self.assertTrue(initial.created)
        self.assertFalse(metadata.created)
        self.assertTrue(metadata.warmed)
        self.assertEqual(value, same)
        self.assertEqual((stats['capacity'], stats['hits']), (2, 1))
        _, _, _, disabled = session.submit(lambda current: run(current, 1, 0)).result(timeout=5)
        self.assertEqual(disabled['entries'], 0)
        self.assertEqual(len(self.readers), 1)
        self.assertEqual(first.reader.warmups, [1, 3])

    def test_failed_new_batch_discards_native_reader_and_cached_names(self):
        session = self.make_session()

        def first_run(current):
            reader, _ = current.get_reader(1)
            reader.names([image(1)], 1)
            reader.reader.fail_warmup = True
            return reader

        old = session.submit(first_run).result(timeout=5)
        with self.assertRaisesRegex(RuntimeError, 'warmup failed'):
            session.submit(lambda current: current.get_reader(3)).result(timeout=5)
        self.assertTrue(old.reader.closed)
        self.assertFalse(old._entries)

        def retry(current):
            reader, metadata = current.get_reader(1)
            reader.names([image(1)], 1)
            return reader, metadata, reader.stats()

        replacement, metadata, stats = session.submit(retry).result(timeout=5)
        self.assertIsNot(old, replacement)
        self.assertTrue(metadata.created)
        self.assertEqual((stats['hits'], stats['misses']), (0, 1))


class NameCacheScannerTests(unittest.TestCase):
    @staticmethod
    def frame():
        frame = np.zeros((1080, 1920, 3), np.uint8)
        for row in range(9):
            NameCacheScannerTests.fill(frame, 'name', row, row + 1)
            NameCacheScannerTests.fill(frame, 'buyout', row, 10)
        return frame

    @staticmethod
    def fill(frame, field, row, value):
        x1, y1, x2, y2 = BOXES[field]
        frame[y1 + STEP * row:y2 + STEP * row, x1:x2] = value

    def test_group_selection_and_ambiguous_quantity_survive_cache_hits(self):
        frame = self.frame()
        native = FakeReader()
        reader = CachedNameReader(native, 9)
        scanner = Scanner(reader)
        catalog = Catalog([Rule(f'Item {row+1}', 100, (row+1)*100, 'white', 0) for row in range(9)])
        for group in (1, 3, 9):
            for metric in ('profit', 'roi'):
                chosen, _ = scanner.scan(frame, catalog, 3, 100, group_size=group, selection_metric=metric)
                self.assertEqual(chosen.row, group - 1)
                self.assertEqual(chosen.quantity.state, 'AMBIGUOUS')
                self.assertIsNone(chosen.quantity.value)
                self.assertEqual(chosen.effective_quantity, 1)
        self.assertEqual(sum(len(images) for images, _ in native.name_calls), 9)

    def test_cached_identity_keeps_prices_quantity_rules_balance_and_metric_fresh(self):
        frame = self.frame()
        self.fill(frame, 'buyout', 0, 100)
        self.fill(frame, 'buyout', 1, 20)
        native = FakeReader()
        reader = CachedNameReader(native, 9)
        scanner = Scanner(reader)
        catalog = Catalog([Rule('Item 1', 200, 2000), Rule('Item 2', 200, 1000)])

        def scan(rules=catalog, **kwargs):
            return scanner.scan(frame, rules, 9, kwargs.pop('max_lot_price', 5000),
                                group_size=3, **kwargs)

        self.assertEqual(scan()[0].row, 0)
        name_calls = len(native.name_calls)
        self.assertEqual(scan(selection_metric='roi')[0].row, 1)
        self.fill(frame, 'buyout', 0, 210)
        self.fill(frame, 'quantity', 1, 2)
        chosen, rows = scan()
        self.assertEqual(rows[0].reason, 'unit_price_limit')
        self.assertEqual(chosen.row, 1)
        self.assertEqual(chosen.quantity.value, 2)
        self.assertEqual(chosen.buyout.value, 20)
        self.assertIsNone(scan(balance=19)[0])
        self.assertIsNone(scan(max_lot_price=10)[0])
        numbers = len(native.number_calls)
        self.assertIsNone(scan(Catalog([]))[0])
        self.assertEqual(len(native.number_calls), numbers)
        self.assertEqual(len(native.name_calls), name_calls)
        # The menu verification path uses Scanner.row and must read both numbers again.
        before = len(native.number_calls)
        verified = scanner.row(frame, 1, catalog, 5000)
        self.assertEqual(verified.quantity.value, 2)
        self.assertEqual(len(native.number_calls), before + 2)
        self.assertEqual(len(native.name_calls), name_calls)


if __name__ == '__main__':
    unittest.main()
