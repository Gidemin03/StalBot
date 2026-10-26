"""Fast prototype checks. No native OCR/model/game input is constructed."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import numpy as np

from benchmarks.optimization_cache import (
    CachedNameReader, ExactNameLRU, SharedNameReader, capacities_for,
    load_corpus, prepared_color, prepared_name, prepared_separator,
)
from stalbot import vision
from stalbot.domain import Catalog, Identity, Number, Rule
from stalbot.scanner import Scanner


def image(value=24, shape=(19, 213, 3)):
    return np.full(shape, value, dtype=np.uint8)


class FakeReader:
    def __init__(self):
        self.owner = threading.get_ident()
        self.calls = []
        self.numbers = []

    def check_thread(self):
        if self.owner != threading.get_ident():
            raise RuntimeError('wrong owner')

    def names(self, images, batch_size):
        self.check_thread()
        self.calls.append((len(images), batch_size))
        return [Identity(str(int(im[0, 0, 0])), 'white', 0) for im in images]

    def number(self, im, field):
        self.check_thread()
        self.numbers.append(field)
        return Number('AMBIGUOUS', None, '') if field == 'quantity' else Number('VALUE', 10, '10')


class ExactCacheTests(unittest.TestCase):
    def test_cold_warm_immutable_and_original_pixels_snapshot(self):
        cache = ExactNameLRU(2)
        source = image()
        value = Identity('A', 'white', 0)
        self.assertIsNone(cache.get(source, 'model:v1'))
        cache.put(source, 'model:v1', value)
        self.assertEqual(cache.get(source.copy(), 'model:v1'), value)
        source[0, 0, 0] += 1
        self.assertIsNone(cache.get(source, 'model:v1'))
        self.assertEqual(cache.get(image(), 'model:v1'), value)
        with self.assertRaises(FrozenInstanceError):
            value.name = 'B'
        with self.assertRaises(TypeError):
            cache.put(source, 'model:v1', {'name': 'mutable'})

    def test_true_lru_eviction_and_long_overflow(self):
        cache = ExactNameLRU(2)
        a, b, c = image(1), image(2), image(3)
        for im in (a, b):
            cache.put(im, 'v1', Identity(str(im[0, 0, 0])))
        self.assertIsNotNone(cache.get(a, 'v1'))
        cache.put(c, 'v1', Identity('3'))
        self.assertIsNone(cache.get(b, 'v1'))
        self.assertIsNotNone(cache.get(a, 'v1'))
        for value in range(4, 200):
            cache.put(image(value), 'v1', Identity(str(value)))
            self.assertEqual(cache.stats()['entries'], 2)
        self.assertEqual(cache.stats()['evictions'], 197)
        self.assertEqual(cache.stats()['pixel_bytes'], 2*19*213*3)
        cache.clear()
        self.assertEqual(cache.stats()['entries'], 0)
        self.assertEqual(cache.stats()['hits'], 0)

    def test_hash_collision_size_namespace_color_and_pixel_are_distinct(self):
        cache = ExactNameLRU(9, digest=lambda pixels: b'collision')
        source = image()
        cache.put(source, 'model1:prep1', Identity('A'))
        for changed in (source[:, ::-1].copy(), source.copy()):
            changed[0, 0, 1] += 1
            self.assertIsNone(cache.get(changed, 'model1:prep1'))
        # Same byte count and byte content but RGB dimensions differ.
        self.assertIsNone(cache.get(image(shape=(213, 19, 3)), 'model1:prep1'))
        for namespace in ('model2:prep1', 'model1:prep2'):
            self.assertIsNone(cache.get(source, namespace))
        self.assertEqual(cache.get(source, 'model1:prep1'), Identity('A'))
        cache.put(image(25), 'model1:prep1', Identity('B'))
        self.assertEqual(cache.get(image(25), 'model1:prep1'), Identity('B'))
        self.assertEqual(cache.get(source, 'model1:prep1'), Identity('A'))

    def test_noncontiguous_rgb_same_pixels_and_validation(self):
        cache = ExactNameLRU(1)
        source = np.arange(19*426*3, dtype=np.uint8).reshape(19, 426, 3)[:, ::2]
        cache.put(source, 'v1', Identity('A'))
        self.assertEqual(cache.get(source.copy(), 'v1'), Identity('A'))
        for invalid in (image().astype(np.float32), np.zeros((19, 213), np.uint8)):
            with self.assertRaises(ValueError):
                cache.key(invalid, 'v1')
        with self.assertRaises(ValueError):
            cache.key(image(), '')
        for capacity in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                ExactNameLRU(capacity)

    def test_shared_cache_thread_safety_independent_readers_namespaces(self):
        cache = ExactNameLRU(8, digest=lambda pixels: b'collision')
        barrier = threading.Barrier(4)

        def worker(index):
            reader = FakeReader()  # session created/used on its owner thread
            wrapped = CachedNameReader(reader, 8, f'model{index}', cache)
            barrier.wait()
            for value in range(100):
                im = image(value % 2)
                result = wrapped.names([im], 1)
                if result != [Identity(str(value % 2), 'white', 0)]:
                    raise AssertionError('cache returned another observation')
                if cache.stats()['entries'] > 8:
                    raise AssertionError('cache exceeded capacity')
            return True

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(worker, range(4))))
        self.assertEqual(cache.stats()['entries'], 8)

    def test_capacity_selection_uses_unique_rgb_shapes(self):
        unique, capacities = capacities_for([image(v) for v in range(12)]*3)
        self.assertEqual(unique, 12)
        self.assertEqual(capacities, [3, 6, 12])


class ReaderTests(unittest.TestCase):
    def test_mixed_misses_preserve_batch_order_and_fresh_numbers(self):
        reader = FakeReader()
        wrapped = CachedNameReader(reader, 9, 'v1')
        wrapped.names([image(1)], 1)
        result = wrapped.names([image(2), image(1), image(3)], 3)
        self.assertEqual([item.name for item in result], ['2', '1', '3'])
        self.assertEqual(reader.calls, [(1, 1), (2, 3)])
        wrapped.names([image(2), image(1), image(3)], 3)
        self.assertEqual(len(reader.calls), 2)
        for _ in range(2):
            wrapped.number(image(), 'quantity')
            wrapped.number(image(), 'buyout')
        self.assertEqual(reader.numbers, ['quantity', 'buyout']*2)

    def test_wrong_owner_and_malformed_batch_fail(self):
        wrapped = CachedNameReader(FakeReader(), 1, 'v1')
        for batch, images in ((2, [image()]), (1, []), (1, [image(), image()])):
            with self.assertRaises(ValueError):
                wrapped.names(images, batch)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(RuntimeError):
                pool.submit(wrapped.names, [image()], 1).result()

    def test_shared_reader_raws_use_identical_semantics_and_one_cleaning(self):
        base = FakeReader()
        seen = []

        def infer(inputs, batch):
            seen.extend(inputs)
            return ['Name | +5', 'Short...'][:len(inputs)]

        shared = SharedNameReader(base, infer_prepared=infer)
        with patch.object(vision, 'name_line', wraps=vision.name_line) as cleaner:
            values = shared.names([image(), image()], 3)
            self.assertEqual(cleaner.call_count, 2)
        self.assertEqual(values, [Identity('Name', 'unknown', 5), Identity('Short', 'unknown', 0, True)])
        self.assertTrue(np.array_equal(seen[0], cvt(vision.name_image(image()))))

    def test_scanner_groups_cache_rechecks_catalog_numbers_budget_and_metric(self):
        frame = image(shape=(1080, 1920, 3))
        for row in range(9):
            x1, y1, x2, y2 = vision.BOXES['name']
            frame[y1+vision.STEP*row:y2+vision.STEP*row, x1:x2] = row+1
        reader = FakeReader()
        wrapped = CachedNameReader(reader, 9, 'v1')
        catalog = Catalog([Rule(str(row+1), 100, (row+1)*100, 'white', 0) for row in range(9)])
        for group in (1, 3, 9):
            scanner = Scanner(wrapped)
            chosen, _ = scanner.scan(frame, catalog, 1, 100, group_size=group)
            self.assertEqual(chosen.row, group-1)
            chosen_again, _ = scanner.scan(frame, catalog, 1, 100, group_size=group, selection_metric='roi')
            self.assertEqual(chosen_again.row, group-1)
            self.assertEqual(chosen.quantity.state, 'AMBIGUOUS')
            self.assertEqual(chosen.effective_quantity, 1)
        names_before = len(reader.calls)
        numbers_before = len(reader.numbers)
        self.assertIsNone(Scanner(wrapped).scan(frame, catalog, 1, 100, balance=9)[0])
        self.assertEqual(len(reader.calls), names_before)
        self.assertGreater(len(reader.numbers), numbers_before)
        self.assertIsNone(Scanner(wrapped).scan(frame, Catalog([]), 1, 100)[0])
        self.assertEqual(len(reader.calls), names_before)


def cvt(image):
    import cv2
    return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)


class CorpusPreprocessingTests(unittest.TestCase):
    def test_full_labeled_corpus_preprocessing_exact(self):
        dataset = Path(__file__).resolve().parents[1]/'dataset'
        if not (dataset/'labels/labels.json').is_file():
            self.skipTest('labeled dataset unavailable')
        images, _, _, _ = load_corpus(dataset)
        self.assertGreater(len(images), 0)
        for index, source in enumerate(images):
            with self.subTest(index=index):
                line = vision.name_line(source)
                self.assertTrue(np.array_equal(prepared_name(line), vision.name_image(source)))
                self.assertEqual(prepared_color(line), vision.name_color(source))
                self.assertEqual(prepared_separator(line), vision.separator_visible(source))


if __name__ == '__main__':
    unittest.main()
