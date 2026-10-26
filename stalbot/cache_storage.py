"""Fail-closed, bounded NAME snapshots. No executable serialization or OCR objects.

The file contains a small JSON header, length-prefixed JSON Identity records and
raw RGB bytes in oldest-to-newest LRU order, followed by a SHA-256 digest. The
digest detects accidental damage; this is a local cache, not an authentication
boundary. A single writer keeps only the newest pending immutable snapshot.
"""
from collections import deque
from dataclasses import asdict
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import struct
import tempfile
import threading

from .domain import Identity


FORMAT = 1
MAGIC = b'SBNAMES1'
# Includes all 100000 configured entries at the production 19x213 RGB geometry.
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_JSON_BYTES = 32_768
MAX_PIXEL_BYTES = 4 * 1024 * 1024
SAVE_INTERVAL = 60.0


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _identity(value):
    if type(value) is not dict or set(value) != {'name', 'color', 'upgrade', 'truncated', 'valid'}:
        raise ValueError('invalid Identity schema')
    if type(value['name']) is not str or len(value['name']) > 4096:
        raise ValueError('invalid Identity name')
    if value['color'] not in ('unknown', 'white', 'green', 'blue', 'purple', 'red', 'gold'):
        raise ValueError('invalid Identity color')
    upgrade = value['upgrade']
    # Invalid OCR suffixes up to 99 are legitimate *invalid* observations too.
    if upgrade is not None and (type(upgrade) is not int or not 0 <= upgrade <= 99):
        raise ValueError('invalid Identity upgrade')
    if type(value['truncated']) is not bool or type(value['valid']) is not bool:
        raise ValueError('invalid Identity flags')
    return Identity(**value)


def _record(key, identity):
    shape, pixels = key
    if (type(shape) not in (tuple, list) or len(shape) != 3 or shape[2] != 3
            or any(type(n) is not int or not 1 <= n <= 8192 for n in shape)
            or type(pixels) is not bytes or len(pixels) > MAX_PIXEL_BYTES
            or len(pixels) != shape[0] * shape[1] * 3):
        raise ValueError('invalid RGB key')
    if not isinstance(identity, Identity):
        raise ValueError('only Identity can be persisted')
    data = asdict(identity)
    _identity(data)
    raw = _json({'shape': list(shape), 'identity': data})
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('record too large')
    return raw, pixels


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def compatibility_fingerprint(models, device):
    """Hash actual model/dictionary bytes, implementation, config and runtime.

    The selected Cyrillic ONNX embeds its character dictionary. Package sources
    and configs also cover RapidOCR decoding/preprocessing and external defaults.
    Reading package metadata does not initialize any native OCR session.
    """
    if device not in ('cpu', 'dml'):
        raise ValueError('OCR device: cpu or dml')
    rapid = Path(models) / 'rapid'
    hashes = {'model': _hash_file(rapid / 'cyrillic_PP-OCRv5_rec_mobile.onnx')}
    for path in sorted(rapid.rglob('*')):
        if path.is_file() and path.suffix.lower() in ('.txt', '.yaml', '.yml', '.json'):
            hashes['models/' + path.relative_to(rapid).as_posix()] = _hash_file(path)
    root = Path(__file__).parent
    for name in ('vision.py', 'domain.py', 'name_cache.py', 'cache_storage.py'):
        hashes['source/' + name] = _hash_file(root / name)
    spec = importlib.util.find_spec('rapidocr')
    if spec is None or spec.origin is None:
        raise ValueError('RapidOCR package source unavailable')
    package = Path(spec.origin).parent
    for path in sorted(package.rglob('*')):
        if path.is_file() and path.suffix.lower() in ('.py', '.txt', '.yaml', '.yml', '.json'):
            hashes['rapidocr/' + path.relative_to(package).as_posix()] = _hash_file(path)
    versions = {}
    for name in ('rapidocr', 'numpy', 'opencv-python', 'opencv-python-headless',
                 'onnxruntime', 'onnxruntime-directml'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    if (not versions['rapidocr'] or not versions['numpy'] or not any(
            versions[name] for name in ('onnxruntime', 'onnxruntime-directml'))
            or not any(versions[name] for name in ('opencv-python', 'opencv-python-headless'))):
        raise ValueError('OCR runtime version unavailable')
    description = dict(format=FORMAT, device=device, files=hashes, versions=versions,
                       python=platform.python_version(), machine=platform.machine())
    return hashlib.sha256(_json(description)).hexdigest()


def create_storage(models, device='cpu', directory=None):
    """Fingerprint failure disables disk reuse without affecting native OCR."""
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parents[1] / 'cache' / 'names'
    try:
        namespace = compatibility_fingerprint(models, device)
        return CacheStorage(directory / f'{device}.names', namespace, device)
    except Exception as exc:
        return CacheStorage(None, '', device, error=f'fingerprint: {type(exc).__name__}: {exc}')


class CacheStorage:
    """One wrapper owns one storage. Disk errors are JSON-safe diagnostics."""
    def __init__(self, path, namespace, device='cpu', *, error=None):
        self.path = Path(path) if path is not None else None
        self.namespace = namespace
        self.device = device
        self._condition = threading.Condition()
        self._thread = None
        self._pending = None
        self._closing = False
        self._submitted = self._completed = 0
        self._diagnostics = dict(enabled=self.path is not None,
                                 path=str(self.path) if self.path is not None else None,
                                 namespace=namespace or None, device=device,
                                 load_status='disabled' if self.path is None else 'not_loaded',
                                 loaded_entries=0, source_entries=0, saved_entries=0, saves=0,
                                 save_failures=0, last_error=error)

    def stats(self):
        with self._condition:
            return dict(self._diagnostics, writer_pending=self._completed < self._submitted)

    def _error(self, operation, exc):
        with self._condition:
            self._diagnostics['last_error'] = f'{operation}: {type(exc).__name__}: {exc}'

    def load(self, capacity):
        if self.path is None or capacity == 0:
            return []
        try:
            entries, source_entries = self._load(capacity)
        except FileNotFoundError:
            with self._condition:
                self._diagnostics.update(load_status='missing', loaded_entries=0, source_entries=0)
            return []
        except Exception as exc:
            self._error('load', exc)
            with self._condition:
                self._diagnostics.update(load_status='rejected', loaded_entries=0, source_entries=0)
            return []
        with self._condition:
            self._diagnostics.update(load_status='loaded', loaded_entries=len(entries),
                                     source_entries=source_entries)
        return entries

    def _load(self, capacity):
        with self.path.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_FILE_BYTES or size < len(MAGIC) + 4 + 32:
                raise ValueError('snapshot size out of bounds')
            remaining = size - 32
            digest = hashlib.sha256()

            def read(length):
                nonlocal remaining
                if length < 0 or length > remaining:
                    raise ValueError('truncated snapshot')
                raw = stream.read(length)
                if len(raw) != length:
                    raise ValueError('truncated snapshot')
                digest.update(raw)
                remaining -= length
                return raw

            def metadata():
                length = struct.unpack('>I', read(4))[0]
                if not 1 <= length <= MAX_JSON_BYTES:
                    raise ValueError('JSON length out of bounds')
                return _decode(read(length))

            if read(len(MAGIC)) != MAGIC:
                raise ValueError('unknown snapshot format')
            header = metadata()
            if type(header) is not dict or set(header) != {'format', 'namespace', 'device', 'entries'}:
                raise ValueError('invalid header schema')
            if (type(header['format']) is not int or header['format'] != FORMAT
                    or header['namespace'] != self.namespace or header['device'] != self.device):
                raise ValueError('incompatible snapshot')
            count = header['entries']
            if type(count) is not int or not 0 <= count <= MAX_ENTRIES:
                raise ValueError('entry count out of bounds')
            entries = deque(maxlen=capacity)
            seen = set()
            for _ in range(count):
                record = metadata()
                if type(record) is not dict or set(record) != {'shape', 'identity'}:
                    raise ValueError('invalid record schema')
                length = struct.unpack('>I', read(4))[0]
                if not 1 <= length <= MAX_PIXEL_BYTES:
                    raise ValueError('RGB length out of bounds')
                pixels = read(length)
                shape = record['shape']
                identity = _identity(record['identity'])
                _record((shape, pixels), identity)
                key = (tuple(shape), pixels)
                # Store digests for duplicate detection without retaining evicted pixels.
                signature = hashlib.sha256(_json(shape) + pixels).digest()
                if signature in seen:
                    raise ValueError('duplicate RGB key')
                seen.add(signature)
                entries.append((key, identity))
            if remaining or stream.read(32) != digest.digest() or stream.read(1):
                raise ValueError('snapshot checksum/length mismatch')
            return list(entries), count

    def _write(self, entries):
        if len(entries) > MAX_ENTRIES:
            raise ValueError('too many entries')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=self.path.name + '.',
                                             suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                digest = hashlib.sha256()
                written = 0

                def write(raw):
                    nonlocal written
                    written += len(raw)
                    if written + 32 > MAX_FILE_BYTES:
                        raise ValueError('snapshot exceeds disk size bound')
                    stream.write(raw)
                    digest.update(raw)

                def metadata(raw):
                    write(struct.pack('>I', len(raw)))
                    write(raw)

                write(MAGIC)
                metadata(_json(dict(format=FORMAT, namespace=self.namespace, device=self.device,
                                    entries=len(entries))))
                seen = set()
                for key, identity in entries:
                    if key in seen:
                        raise ValueError('duplicate RGB key')
                    seen.add(key)
                    raw, pixels = _record(key, identity)
                    metadata(raw)
                    write(struct.pack('>I', len(pixels)))
                    write(pixels)
                stream.write(digest.digest())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def submit(self, entries):
        """Receive an owner-thread tuple of immutable (RGB key, Identity) pairs."""
        if self.path is None:
            return False
        try:
            with self._condition:
                if self._closing:
                    return False
                if self._thread is None:
                    thread = threading.Thread(target=self._run, name='NAME-cache-writer', daemon=True)
                    thread.start()
                    self._thread = thread
                self._submitted += 1
                self._pending = (self._submitted, entries)
                self._condition.notify_all()
            return True
        except Exception as exc:
            self._error('schedule save', exc)
            return False

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closing:
                    self._condition.wait()
                if self._pending is None:
                    return
                generation, entries = self._pending
                self._pending = None
            try:
                self._write(entries)
            except Exception as exc:
                self._error('save', exc)
                with self._condition:
                    self._diagnostics['save_failures'] += 1
            else:
                with self._condition:
                    self._diagnostics['saved_entries'] = len(entries)
                    self._diagnostics['saves'] += 1
                    self._diagnostics['last_error'] = None
            finally:
                with self._condition:
                    self._completed = generation
                    self._condition.notify_all()

    def wait(self):
        with self._condition:
            target = self._submitted
            while self._completed < target:
                self._condition.wait()

    def close(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join()
