"""Exact NAME observations, scoped to a compatible immutable OCR pipeline."""
from collections import OrderedDict
from time import monotonic

import numpy as np

from .domain import Identity
from .config import validate_capacity
from .cache_storage import SAVE_INTERVAL


class CachedNameReader:
    """Owner-thread LRU with optional compatible disk snapshots.

    Keys retain original RGB bytes and shape. Python hashes accelerate lookup,
    but full equality is always checked, including on a hash collision. A new
    native reader must get a new wrapper. Optional storage fingerprints the
    model/preprocessing/runtime; only exact RGB and Identity observations persist.
    No lossy preprocessing participates in the key, and numbers/decisions never
    enter the cache. Disk writes receive immutable snapshots, never the reader.
    """

    def __init__(self, reader, capacity=1000, *, persistence=None):
        validate_capacity(capacity)
        self.reader = reader
        self._closed = False
        self.capacity = capacity
        self._entries = OrderedDict()
        self._pixel_bytes = 0
        self.hits = self.misses = self.evictions = 0
        self.persistence = persistence
        self._revision = self._submitted_revision = 0
        self._submitted_failures = 0
        self._last_flush = monotonic()
        if persistence is not None and capacity:
            self._load()

    def _load(self):
        self.check_thread()
        entries = self.persistence.load(self.capacity)
        self._entries = OrderedDict(entries)
        self._pixel_bytes = sum(len(key[1]) for key in self._entries)
        if self.persistence.stats()['source_entries'] > self.capacity:
            self._revision += 1

    def check_thread(self):
        self.reader.check_thread()
        if self._closed:
            raise RuntimeError('OCR cache reader is closed')

    @staticmethod
    def _key(image):
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError('original uint8 RGB NAME crop required')
        return (tuple(image.shape), image.tobytes(order='C'))

    def configure(self, capacity):
        """Apply settings between runs; retain recent entries when possible."""
        self.check_thread()
        validate_capacity(capacity)
        previous = self.capacity
        self.capacity = capacity
        while len(self._entries) > capacity:
            key, _ = self._entries.popitem(last=False)
            self._pixel_bytes -= len(key[1])
            self.evictions += 1
            self._revision += 1
        if not previous and capacity and self.persistence is not None:
            self.persistence.wait()
            self._submitted_revision = self._revision
            self._load()

    def reset_stats(self):
        self.check_thread()
        self.hits = self.misses = self.evictions = 0

    def clear(self):
        self.check_thread()
        if self._entries:
            self._revision += 1
        self._entries.clear()
        self._pixel_bytes = 0
        self.reset_stats()

    def stats(self):
        self.check_thread()
        requests = self.hits + self.misses
        result = dict(capacity=self.capacity, entries=len(self._entries),
                    hits=self.hits, misses=self.misses, evictions=self.evictions,
                    hit_rate=self.hits / requests if requests else 0.0,
                    pixel_bytes=self._pixel_bytes)
        if self.persistence is not None:
            result['persistence'] = self.persistence.stats()
        return result

    def flush_cache(self, *, wait=False, force=False):
        """Submit a dirty LRU at most once per minute; force bypasses the timer.

        Called on the OCR owner thread. No serialization or disk writes run here.
        Capacity zero preserves the last valid disk snapshot. A failed save is
        retried on the next eligible flush. wait=True also waits for pending work;
        a forced wait retries a newly failed write once before returning.
        """
        self.check_thread()
        storage = self.persistence
        if storage is None:
            return
        state = storage.stats()
        dirty = (self._revision != self._submitted_revision
                 or state['save_failures'] > self._submitted_failures)
        now = monotonic()
        if (self.capacity and state['enabled'] and dirty
                and (force or now - self._last_flush >= SAVE_INTERVAL)):
            if storage.submit(tuple(self._entries.items())):
                self._submitted_revision = self._revision
                self._submitted_failures = state['save_failures']
            self._last_flush = now
        if wait:
            storage.wait()
            # A pending write may fail *during* wait, after the dirty/retry check
            # above. Final forced flushes get one bounded retry of the complete
            # current LRU, including when close inherited an in-flight write.
            completed = storage.stats()
            if (force and self.capacity and completed['enabled']
                    and completed['save_failures'] > state['save_failures']
                    and completed['last_error'] is not None):
                if storage.submit(tuple(self._entries.items())):
                    self._submitted_revision = self._revision
                    self._submitted_failures = completed['save_failures']
                self._last_flush = monotonic()
                storage.wait()

    def names(self, images, batch_size):
        self.check_thread()
        if batch_size not in (1, 3, 9) or not 1 <= len(images) <= batch_size:
            raise ValueError('batch: 1, 3 или 9')
        if not self.capacity:
            # Disabled means no key construction and no cache statistics.
            return self.reader.names(images, batch_size)
        keys = [self._key(image) for image in images]
        output = []
        for key in keys:
            value = self._entries.get(key)
            output.append(value)
            if value is None:
                self.misses += 1
            else:
                self.hits += 1
                if next(reversed(self._entries)) != key:
                    self._entries.move_to_end(key)
                    self._revision += 1
        missing = [i for i, value in enumerate(output) if value is None]
        if missing:
            values = self.reader.names([images[i] for i in missing], batch_size)
            if len(values) != len(missing):
                raise RuntimeError('OCR нарушил размер пакета')
            if not all(isinstance(value, Identity) for value in values):
                raise TypeError('Only immutable Identity observations may be cached')
            for index, value in zip(missing, values):
                key = keys[index]
                if key not in self._entries:
                    self._pixel_bytes += len(key[1])
                self._entries[key] = value
                self._entries.move_to_end(key)
                self._revision += 1
                output[index] = value
                if len(self._entries) > self.capacity:
                    old, _ = self._entries.popitem(last=False)
                    self._pixel_bytes -= len(old[1])
                    self.evictions += 1
        self.flush_cache()
        return output

    def number(self, image, field):
        self.check_thread()
        return self.reader.number(image, field)

    def warmup(self, batch_size=1):
        self.check_thread()
        return self.reader.warmup(batch_size)

    def backend_info(self):
        self.check_thread()
        return self.reader.backend_info()

    def close(self):
        self.check_thread()
        try:
            self.flush_cache(force=True, wait=True)
        finally:
            try:
                if self.persistence is not None:
                    self.persistence.close()
            finally:
                self._entries.clear()
                self._pixel_bytes = 0
                self._closed = True
                self.reader.close()
