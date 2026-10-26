"""Persistent NAME observations: temporary files and fake OCR, no game/native API."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from stalbot.cache_storage import (CacheStorage, MAGIC, MAX_FILE_BYTES,
                                   compatibility_fingerprint, create_storage)
from stalbot.domain import Identity, Number
from stalbot.name_cache import CachedNameReader


def image(value):
    return np.full((19, 213, 3), value, np.uint8)


class Native:
    def __init__(self):
        self.owner = threading.get_ident()
        self.calls = []
        self.number_calls = 0
        self.closed = False

    def check_thread(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError('wrong owner')

    def names(self, images, batch_size):
        self.check_thread()
        self.calls.append(len(images))
        return [Identity(f'Item {int(im[0, 0, 0])}', 'white', 0) for im in images]

    def number(self, image, field):
        self.number_calls += 1
        return Number('AMBIGUOUS', None)

    def warmup(self, batch_size):
        self.names([image(24)], batch_size)

    def close(self):
        self.check_thread()
        self.closed = True


class CacheStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'names.snapshot'

    def storage(self, namespace='test', device='cpu'):
        storage = CacheStorage(self.path, namespace, device)
        self.addCleanup(storage.close)
        return storage

    def reader(self, capacity=3, storage=None):
        reader = CachedNameReader(Native(), capacity, persistence=storage or self.storage())
        # Explicit closes remain observable in tests; cleanup is idempotent here.
        self.addCleanup(lambda: None if reader._closed else reader.close())
        return reader

    @staticmethod
    def populate(reader, values):
        for value in values:
            reader.names([image(value)], 1)

    def test_restart_retains_exact_rgb_identities_and_recent_lru_at_lower_capacity(self):
        first = self.reader()
        self.populate(first, (1, 2, 3, 1))  # oldest -> newest: 2, 3, 1
        first.close()
        restarted = self.reader(2)
        self.assertEqual(restarted.stats()['persistence']['loaded_entries'], 2)
        self.assertEqual(restarted.stats()['hits'], 0)
        self.populate(restarted, (3, 1))
        self.assertEqual(restarted.reader.calls, [])
        # A different color channel must miss even with identical OCR text.
        changed = image(1)
        changed[0, 0, 1] += 1
        restarted.names([changed], 1)
        self.assertEqual(restarted.reader.calls, [1])
        self.assertEqual(restarted.stats()['entries'], 2)
        restarted.number(image(1), 'quantity')
        restarted.number(image(1), 'quantity')
        self.assertEqual(restarted.reader.number_calls, 2)

    def test_hit_only_lru_reordering_is_saved_and_controls_next_restart_eviction(self):
        first = self.reader(2)
        self.populate(first, (1, 2))
        first.flush_cache(force=True, wait=True)
        self.populate(first, (1,))
        first.close()
        restarted = self.reader(2)
        self.populate(restarted, (3, 1))  # evicts 2, keeps 1
        self.assertEqual(restarted.reader.calls, [1])
        self.populate(restarted, (2,))
        self.assertEqual(restarted.reader.calls, [1, 1])

    def test_loading_smaller_capacity_saves_only_newest_entries_even_without_names(self):
        reader = self.reader()
        self.populate(reader, (1, 2, 3))
        reader.close()
        smaller = self.reader(1)
        self.assertEqual(smaller.stats()['persistence']['source_entries'], 3)
        smaller.close()
        entries = self.storage().load(3)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][1].name, 'Item 3')

    def test_disabled_capacity_never_overwrites_disk_and_enable_reloads(self):
        first = self.reader()
        self.populate(first, (1, 2))
        first.close()
        before = self.path.read_bytes()
        disabled = self.reader(0)
        self.populate(disabled, (3, 3))
        disabled.flush_cache(force=True, wait=True)
        self.assertEqual(before, self.path.read_bytes())
        disabled.configure(2)
        self.populate(disabled, (1,))
        self.assertEqual(disabled.reader.calls, [1, 1])
        disabled.configure(0)
        disabled.close()
        self.assertEqual(before, self.path.read_bytes())

    def test_warmup_and_clean_close_never_save_synthetic_observations(self):
        reader = self.reader()
        for batch in (1, 3, 9):
            reader.warmup(batch)
        reader.close()
        self.assertFalse(self.path.exists())
        self.assertEqual(reader.persistence.stats()['saves'], 0)
        self.assertTrue(reader.reader.closed)

    def test_autosave_is_dirty_only_and_throttled_while_force_flush_bypasses_time(self):
        with patch('stalbot.name_cache.monotonic', return_value=0) as timer:
            reader = self.reader()
            timer.return_value = 59
            self.populate(reader, (1,))
            reader.flush_cache(wait=True)
            self.assertFalse(self.path.exists())
            timer.return_value = 60
            self.populate(reader, (1,))
            reader.flush_cache(wait=True)
            self.assertEqual(reader.persistence.stats()['saves'], 1)
            timer.return_value = 120
            self.populate(reader, (1,))
            reader.flush_cache(wait=True)
            self.assertEqual(reader.persistence.stats()['saves'], 1)  # same last item, no LRU change
            self.populate(reader, (2,))
            reader.flush_cache(wait=True)
            self.assertEqual(reader.persistence.stats()['saves'], 2)
            timer.return_value = 121
            self.populate(reader, (3,))
            reader.flush_cache(force=True, wait=True)
            self.assertEqual(reader.persistence.stats()['saves'], 3)

    def test_slow_writer_never_runs_ocr_and_keeps_only_latest_pending_snapshot(self):
        storage = self.storage()
        reader = self.reader(storage=storage)
        started, release = threading.Event(), threading.Event()
        original = storage._write
        threads = []
        snapshots = []

        def slow(entries):
            threads.append(threading.get_ident())
            snapshots.append(entries)
            started.set()
            if not release.wait(5):
                raise RuntimeError('test writer timed out')
            original(entries)

        with patch.object(storage, '_write', side_effect=slow):
            self.populate(reader, (1,))
            reader.flush_cache(force=True)
            self.assertTrue(started.wait(3))
            try:
                self.populate(reader, (2,))  # returns while disk writer is blocked
                reader.flush_cache(force=True)
                self.populate(reader, (3,))
                reader.flush_cache(force=True)
                self.assertEqual(len(storage._pending[1]), 3)
                self.assertEqual(len(snapshots[0]), 1)  # immutable original snapshot
            finally:
                release.set()
            reader.flush_cache(wait=True)
        self.assertEqual([len(items) for items in snapshots], [1, 3])
        self.assertTrue(all(owner != threading.get_ident() for owner in threads))
        reader.close()
        self.assertFalse(storage._thread.is_alive())
        self.assertEqual(len(self.storage().load(3)), 3)

    def test_failed_atomic_replace_preserves_previous_file_retries_and_releases_native(self):
        reader = self.reader()
        self.populate(reader, (1,))
        reader.flush_cache(force=True, wait=True)
        previous = self.path.read_bytes()
        self.populate(reader, (2,))
        with patch('stalbot.cache_storage.os.replace', side_effect=OSError('disk unavailable')):
            reader.flush_cache(force=True, wait=True)
            state = reader.stats()['persistence']
            self.assertIn('disk unavailable', state['last_error'])
            self.assertEqual(state['save_failures'], 2)  # final forced wait retries once
            self.assertEqual(self.path.read_bytes(), previous)
            self.assertEqual(list(self.root.glob('*.tmp')), [])
            self.populate(reader, (1,))  # OCR remains usable after disk error
        reader.flush_cache(force=True, wait=True)
        self.assertEqual(reader.stats()['persistence']['saves'], 2)
        self.assertIsNone(reader.stats()['persistence']['last_error'])
        self.populate(reader, (3,))
        with patch.object(reader.persistence, '_write', side_effect=OSError('close save failure')):
            reader.close()
        self.assertTrue(reader.reader.closed)
        self.assertFalse(reader.persistence._thread.is_alive())
        self.assertFalse(reader._entries)

    def test_thread_start_failure_is_diagnostic_and_a_later_flush_retries(self):
        reader = self.reader()
        self.populate(reader, (1,))
        with patch('stalbot.cache_storage.threading.Thread.start', side_effect=RuntimeError('no thread')):
            reader.flush_cache(force=True, wait=True)
        self.assertIn('no thread', reader.stats()['persistence']['last_error'])
        self.populate(reader, (1,))
        reader.flush_cache(force=True, wait=True)
        self.assertEqual(reader.stats()['persistence']['saves'], 1)

    def test_close_retries_once_when_inherited_pending_write_fails_during_wait(self):
        reader = self.reader()
        storage = reader.persistence
        started, release = threading.Event(), threading.Event()
        original_write, original_wait = storage._write, storage.wait
        attempts = []

        def write(entries):
            attempts.append(entries)
            if len(attempts) == 1:
                started.set()
                if not release.wait(5):
                    raise RuntimeError('test writer timed out')
                raise OSError('pending write failed during close')
            original_write(entries)

        def wait():
            # The owner is now inside final flush waiting for its existing save.
            self.assertEqual(storage.stats()['save_failures'], 0)
            release.set()
            original_wait()

        with patch.object(storage, '_write', side_effect=write):
            self.populate(reader, (1, 2))
            reader.flush_cache(force=True)
            self.assertTrue(started.wait(3))
            # Only the first wait needs to release the blocked writer; the retry
            # has a second wait after the recorded failure count has advanced.
            first_wait = True

            def final_wait():
                nonlocal first_wait
                if first_wait:
                    first_wait = False
                    wait()
                else:
                    original_wait()

            with patch.object(storage, 'wait', side_effect=final_wait):
                reader.close()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(storage.stats()['save_failures'], 1)
        self.assertEqual(storage.stats()['saves'], 1)
        self.assertIsNone(storage.stats()['last_error'])
        self.assertTrue(reader.reader.closed)
        self.assertFalse(storage._thread.is_alive())
        self.assertEqual([value.name for _, value in self.storage().load(3)], ['Item 1', 'Item 2'])

    def test_close_retry_is_bounded_when_disk_keeps_failing(self):
        reader = self.reader()
        self.populate(reader, (1,))
        with patch.object(reader.persistence, '_write', side_effect=OSError('still unavailable')) as write:
            reader.close()
        self.assertEqual(write.call_count, 2)
        self.assertEqual(reader.persistence.stats()['save_failures'], 2)
        self.assertTrue(reader.reader.closed)
        self.assertFalse(reader.persistence._thread.is_alive())

    def test_corruption_anywhere_discards_whole_file_even_entries_outside_capacity(self):
        reader = self.reader()
        self.populate(reader, (1, 2, 3))
        reader.close()
        valid = self.path.read_bytes()
        damaged = bytearray(valid)
        damaged[-40] ^= 1
        for raw in (valid[:40], bytes(damaged), valid + b'junk', b'not a snapshot'):
            with self.subTest(size=len(raw)):
                self.path.write_bytes(raw)
                storage = self.storage()
                self.assertEqual(storage.load(1), [])
                self.assertEqual(storage.stats()['load_status'], 'rejected')
                json.dumps(storage.stats())  # diagnostics stay serializable

    def test_schema_and_resource_bounds_reject_even_valid_checksum(self):
        def pack(value):
            raw = json.dumps(value).encode()
            return struct.pack('>I', len(raw)) + raw

        header = dict(format=1, namespace='test', device='cpu', entries=1)
        good = dict(shape=[1, 1, 3], identity=dict(name='x', color='unknown', upgrade=None,
                                               truncated=False, valid=True))
        malformed = [dict(header, entries=True), dict(header, entries=100_001),
                     dict(header, extra=1), dict(header, format=True)]
        payloads = [MAGIC + pack(value) for value in malformed]
        payloads += [MAGIC + pack(header) + pack(dict(good, shape=[True, 1, 3]))
                     + struct.pack('>I', 3) + b'abc',
                     MAGIC + pack(header) + pack(dict(good, decision='BUY'))
                     + struct.pack('>I', 3) + b'abc',
                     MAGIC + pack(header) + struct.pack('>I', 2**31),
                     MAGIC + pack(header) + pack(good) + struct.pack('>I', 2**31)]
        duplicate = pack(good) + struct.pack('>I', 3) + b'abc'
        payloads.append(MAGIC + pack(dict(header, entries=2)) + duplicate * 2)
        for payload in payloads:
            with self.subTest(payload=payload[:80]):
                self.path.write_bytes(payload + hashlib.sha256(payload).digest())
                self.assertEqual(self.storage().load(3), [])
        with patch('stalbot.cache_storage.os.fstat', return_value=SimpleNamespace(st_size=MAX_FILE_BYTES + 1)):
            self.assertEqual(self.storage().load(3), [])

    def test_compatible_invalid_identity_roundtrips_without_becoming_valid(self):
        storage = self.storage()
        observations = [Identity('', 'unknown', None, False, False),
                        Identity('X', 'red', 99, False, False),
                        Identity('Partial', 'white', 0, True, True)]
        entries = tuple((((1, 1, 3), bytes([n] * 3)), value)
                        for n, value in enumerate(observations))
        storage.submit(entries)
        storage.wait()
        self.assertEqual(storage.load(3), list(entries))

    def test_namespace_and_device_mismatch_never_reuse_disk(self):
        reader = self.reader()
        self.populate(reader, (1,))
        reader.close()
        self.assertEqual(self.storage(namespace='changed-model').load(3), [])
        self.assertEqual(self.storage(device='dml').load(3), [])
        self.assertEqual(len(self.storage().load(3)), 1)

    def test_fingerprint_covers_model_dictionary_preprocessing_runtime_and_device(self):
        rapid = self.root / 'models' / 'rapid'
        rapid.mkdir(parents=True)
        model = rapid / 'cyrillic_PP-OCRv5_rec_mobile.onnx'
        model.write_bytes(b'model-with-embedded-dictionary')
        package = self.root / 'rapidocr'
        package.mkdir()
        (package / '__init__.py').write_text('# OCR')
        dictionary = package / 'dictionary.txt'
        dictionary.write_text('abc')
        with patch('stalbot.cache_storage.importlib.util.find_spec',
                   return_value=SimpleNamespace(origin=str(package / '__init__.py'))), patch(
                       'stalbot.cache_storage.importlib.metadata.version', return_value='1.0') as version:
            original = compatibility_fingerprint(rapid.parent, 'cpu')
            self.assertEqual(original, compatibility_fingerprint(rapid.parent, 'cpu'))
            self.assertNotEqual(original, compatibility_fingerprint(rapid.parent, 'dml'))
            model.write_bytes(b'other-model')
            self.assertNotEqual(original, compatibility_fingerprint(rapid.parent, 'cpu'))
            model.write_bytes(b'model-with-embedded-dictionary')
            dictionary.write_text('cba')
            self.assertNotEqual(original, compatibility_fingerprint(rapid.parent, 'cpu'))
            dictionary.write_text('abc')
            version.return_value = '2.0'
            self.assertNotEqual(original, compatibility_fingerprint(rapid.parent, 'cpu'))
            version.return_value = '1.0'
            real_hash = __import__('stalbot.cache_storage', fromlist=['_hash_file'])._hash_file
            with patch('stalbot.cache_storage._hash_file', side_effect=lambda path:
                       'changed' if Path(path).name == 'vision.py' else real_hash(path)):
                self.assertNotEqual(original, compatibility_fingerprint(rapid.parent, 'cpu'))

    def test_missing_fingerprint_or_runtime_disables_disk_without_breaking_ocr(self):
        storage = create_storage(self.root / 'missing-models', directory=self.root)
        reader = self.reader(storage=storage)
        self.populate(reader, (1, 1))
        self.assertEqual(reader.reader.calls, [1])
        self.assertFalse(reader.stats()['persistence']['enabled'])
        self.assertIn('fingerprint:', reader.stats()['persistence']['last_error'])
        reader.close()
        self.assertTrue(reader.reader.closed)


if __name__ == '__main__':
    unittest.main()
