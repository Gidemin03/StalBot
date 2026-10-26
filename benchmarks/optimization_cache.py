"""Offline exact NAME LRU/shared preparation experiments; never issues game input.

All repeated corpus streams are synthetic reuse, not an estimate of live hit rate.
Production modules do not import this prototype. Native OCR stays on its owner thread.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile
import threading
from time import perf_counter
import tracemalloc

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stalbot import vision
from stalbot.domain import Catalog, Identity, Rule, identity_from_text
from stalbot.scanner import Scanner
from stalbot.storage import Store


@dataclass(frozen=True)
class ExactKey:
    namespace: str
    shape: tuple[int, ...]
    digest: bytes
    pixels: bytes


class ExactNameLRU:
    """Bounded immutable observations; byte equality protects digest collisions.

    Each operation is atomic. Inference happens outside the lock: two workers may
    compute the same miss, which is safe and avoids serializing native sessions.
    Readers themselves must never be shared across owner threads.
    """

    def __init__(self, capacity, digest=None):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self.capacity = capacity
        self.digest = digest or (lambda pixels: hashlib.sha256(pixels).digest())
        self._entries = OrderedDict()
        self._lock = threading.RLock()
        self.hits = self.misses = self.evictions = 0

    def key(self, image, namespace):
        if not isinstance(namespace, str) or not namespace:
            raise ValueError('nonempty model/preprocessing namespace required')
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError('original uint8 RGB NAME crop required')
        pixels = image.tobytes(order='C')
        return ExactKey(namespace, tuple(image.shape), self.digest(pixels), pixels)

    def get_key(self, key):
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self.misses += 1
                return None
            self.hits += 1
            self._entries.move_to_end(key)
            return value

    def put_key(self, key, value):
        if not isinstance(value, Identity):
            raise TypeError('only immutable Identity observations may be cached')
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
                self.evictions += 1

    def get(self, image, namespace):
        return self.get_key(self.key(image, namespace))

    def put(self, image, namespace, value):
        self.put_key(self.key(image, namespace), value)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self.hits = self.misses = self.evictions = 0

    def stats(self):
        with self._lock:
            count = self.hits + self.misses
            # retained Python objects; tracemalloc measurement is reported separately
            objects = [self._entries]
            for key, value in self._entries.items():
                objects.extend((key, key.__dict__, key.namespace, key.shape, key.digest, key.pixels,
                                value, value.name, value.color, value.__dict__))
            seen = set()
            retained = 0
            for obj in objects:
                if id(obj) not in seen:
                    retained += sys.getsizeof(obj)
                    seen.add(id(obj))
            return dict(entries=len(self._entries), capacity=self.capacity, hits=self.hits,
                        misses=self.misses, evictions=self.evictions,
                        hit_rate=self.hits/count if count else 0.0,
                        retained_estimate_bytes=retained,
                        pixel_bytes=sum(len(k.pixels) for k in self._entries))


class CachedNameReader:
    """Reader-compatible NAME cache; fresh numeric OCR/catalog evaluation always run."""

    def __init__(self, reader, capacity, namespace, cache=None):
        self.reader = reader
        self.cache = cache if cache is not None else ExactNameLRU(capacity)
        self.namespace = namespace

    def check_thread(self):
        self.reader.check_thread()

    def names(self, images, batch_size):
        self.check_thread()
        if batch_size not in (1, 3, 9) or not 1 <= len(images) <= batch_size:
            raise ValueError('batch: 1, 3 or 9')
        keys = [self.cache.key(image, self.namespace) for image in images]
        output = [self.cache.get_key(key) for key in keys]
        missing = [i for i, value in enumerate(output) if value is None]
        if missing:
            values = self.reader.names([images[i] for i in missing], batch_size)
            if len(values) != len(missing):
                raise RuntimeError('OCR returned an invalid batch size')
            for index, value in zip(missing, values):
                self.cache.put_key(keys[index], value)
                output[index] = value
        return output

    def number(self, image, field):
        return self.reader.number(image, field)


def prepared_name(line):
    """Exactly vision.name_image after its name_line call, without cleaning again."""
    points = np.argwhere(line.max(2) > 100)
    result = line
    if points.size:
        y1, x1 = points.min(0)
        y2, x2 = points.max(0) + 1
        result = result[max(0, y1-2):min(result.shape[0], y2+2),
                        max(0, x1-2):min(result.shape[1], x2+2)]
    return cv2.resize(result, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)


def prepared_separator(line):
    _, stats = vision.components(line.max(2) > 100)
    bars = [s for s in stats if s[2] <= 3 and s[3] >= 10 and s[1] <= 5]
    return any(6 <= w <= 10 and 6 <= h <= 10 and 3 <= y <= 7
               and 2 <= x-(bar[0]+bar[2]) <= 10
               for bar in bars for x, y, w, h, a in stats)


def prepared_color(line):
    hsv = cv2.cvtColor(line[:14], cv2.COLOR_RGB2HSV)
    bright = hsv[:, :, 2] >= 120
    if np.count_nonzero(bright) < 8:
        return 'unknown'
    colored = bright & (hsv[:, :, 1] >= 70)
    if np.count_nonzero(colored) < np.count_nonzero(bright)*0.35:
        return 'white'
    hue = hsv[:, :, 0][colored]
    masks = [(hue < 10) | (hue >= 170), (hue >= 10) & (hue < 35),
             (hue >= 35) & (hue < 85), (hue >= 85) & (hue < 130),
             (hue >= 130) & (hue < 170)]
    counts = [np.count_nonzero(mask) for mask in masks]
    winner = int(np.argmax(counts))
    return ('red', 'gold', 'green', 'blue', 'purple')[winner] if counts[winner]/len(hue) >= 0.7 else 'unknown'


class SharedNameReader:
    """Prototype Reader.names semantics with one name_line per source crop.

    infer_prepared(inputs_bgr, batch_size) -> raw strings may connect an independent
    device backend. Without a callback the actual production Reader model is used.
    """

    def __init__(self, reader, infer_prepared=None):
        self.reader = reader
        self.infer_prepared = infer_prepared

    def check_thread(self):
        self.reader.check_thread()

    def names(self, images, batch_size):
        self.check_thread()
        if batch_size not in (1, 3, 9) or not 1 <= len(images) <= batch_size:
            raise ValueError('batch: 1, 3 or 9')
        lines = [vision.name_line(image) for image in images]
        inputs = [cv2.cvtColor(prepared_name(line), cv2.COLOR_RGB2BGR) for line in lines]
        if self.infer_prepared is None:
            from rapidocr.ch_ppocr_rec.typings import TextRecInput
            model = self.reader.model
            if model.text_rec is None:
                model(inputs[0], use_det=False, use_cls=False, use_rec=True)
            model.text_rec.rec_batch_num = batch_size
            raws = model.text_rec(TextRecInput(img=inputs)).txts
        else:
            raws = self.infer_prepared(inputs, batch_size)
        if raws is None or len(raws) != len(images):
            raise RuntimeError('OCR returned an invalid batch size')
        return [identity_from_text(raw, prepared_color(line), prepared_separator(line))
                for raw, line in zip(raws, lines)]

    def number(self, image, field):
        return self.reader.number(image, field)

    def close(self):
        return self.reader.close()


def timing(values):
    return dict(n=len(values), p50_ms=float(np.percentile(values, 50)),
                p95_ms=float(np.percentile(values, 95)), min_ms=min(values),
                max_ms=max(values), total_ms=sum(values)) if values else dict(n=0)


def cache_interval(cache, before):
    after = cache.stats()
    interval = {key: after[key]-before[key] for key in ('hits', 'misses', 'evictions')}
    count = interval['hits']+interval['misses']
    interval['hit_rate'] = interval['hits']/count if count else 0.0
    return dict(cumulative=after, interval=interval)


def capacities_for(images):
    unique = len({(tuple(image.shape), image.tobytes()) for image in images})
    return unique, sorted({max(1, unique//4), max(1, unique//2), max(1, unique)})


def preparation_check(images, repeats):
    for index, image in enumerate(images):
        line = vision.name_line(image)
        if (not np.array_equal(vision.name_image(image), prepared_name(line))
                or vision.name_color(image) != prepared_color(line)
                or vision.separator_visible(image) != prepared_separator(line)):
            raise AssertionError(f'shared preparation differs on crop {index}')
    times = {'production_three_cleanings': [], 'shared_one_cleaning': []}
    for repeat in range(repeats):
        order = list(times) if repeat % 2 == 0 else list(reversed(times))
        for variant in order:
            started = perf_counter()
            for image in images:
                if variant == 'production_three_cleanings':
                    vision.name_image(image)
                    vision.name_color(image)
                    vision.separator_visible(image)
                else:
                    line = vision.name_line(image)
                    prepared_name(line)
                    prepared_color(line)
                    prepared_separator(line)
            times[variant].append((perf_counter()-started)*1000)
    return {key: timing(value) for key, value in times.items()}


def cache_microbench(images, capacities, namespace, loops=10):
    """No model inference: key/lookup, exact-collision and bounded-memory probes."""
    output = []
    for capacity in capacities:
        cache = ExactNameLRU(capacity)
        value = Identity('synthetic immutable observation')
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        # More unique original-sized RGB inputs than the capacity, with a repeat.
        stream = []
        for index in range(capacity*3+1):
            image = images[index % len(images)].copy()
            encoded = index.to_bytes(8, 'little')
            image.reshape(-1)[:8] = np.frombuffer(encoded, np.uint8)
            stream.append(image)
        memory_points = []
        for index, image in enumerate(stream):
            key = cache.key(image, namespace)
            cache.put_key(key, value)
            if (index+1) % capacity == 0:
                memory_points.append(dict(insertions=index+1, **cache.stats(),
                                          traced_delta_bytes=tracemalloc.get_traced_memory()[0]-before))
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert cache.stats()['entries'] == capacity
        assert cache.get(stream[0], namespace) is None
        assert cache.get(stream[-1], namespace) == value
        key_ms, lookup_ms, combined_ms = [], [], []
        key = cache.key(stream[-1], namespace)
        for _ in range(loops*len(images)):
            started = perf_counter()
            cache.key(stream[-1], namespace)
            key_ms.append((perf_counter()-started)*1000)
            started = perf_counter()
            cache.get_key(key)
            lookup_ms.append((perf_counter()-started)*1000)
            started = perf_counter()
            cache.get(stream[-1], namespace)
            combined_ms.append((perf_counter()-started)*1000)
        collision = ExactNameLRU(2, digest=lambda pixels: b'forced collision')
        collision.put(stream[0], namespace, value)
        assert collision.get(stream[1], namespace) is None
        assert collision.get(stream[0], namespace+'-changed-model') is None
        output.append(dict(capacity=capacity, synthetic=True, final=cache.stats(),
                           stream_unique=len(stream), memory_points=memory_points,
                           traced_delta_bytes=current-before, traced_peak_delta_bytes=peak-before,
                           key=timing(key_ms), lookup_prebuilt_key=timing(lookup_ms),
                           key_plus_lookup=timing(combined_ms),
                           forced_hash_collision='PASS', namespace_isolation='PASS',
                           memory_scope='tracemalloc includes synthetic overflow input copies; retained estimate isolates entries'))
    return output


def read_corpus(reader, images, batch):
    observations, durations = [], []
    started = perf_counter()
    for start in range(0, len(images), batch):
        tick = perf_counter()
        observations.extend(reader.names(images[start:start+batch], batch))
        durations.append((perf_counter()-tick)*1000)
    return observations, durations, (perf_counter()-started)*1000


def name_bench(reader, images, capacities, namespace, repeats):
    references = {}
    variants = [('production', reader)]
    shared = SharedNameReader(reader)
    variants.append(('shared', shared))
    for capacity in capacities:
        variants.extend([(f'cache-{capacity}', CachedNameReader(reader, capacity, namespace)),
                         (f'shared-cache-{capacity}', CachedNameReader(shared, capacity, namespace))])
    output = []
    for batch in (1, 3, 9):
        # Baseline is freshly computed per technical batch; batch equality is explicit.
        reference, _, _ = read_corpus(reader, images, batch)
        references[batch] = reference
        for repeat in range(repeats):
            order = variants if repeat % 2 == 0 else list(reversed(variants))
            for name, variant in order:
                for state in ('cold', 'synthetic_repeat'):
                    if state == 'cold' and isinstance(variant, CachedNameReader):
                        variant.cache.clear()
                    before = variant.cache.stats() if isinstance(variant, CachedNameReader) else None
                    observations, durations, total = read_corpus(variant, images, batch)
                    differences = [index for index, (a, b) in enumerate(zip(reference, observations)) if a != b]
                    output.append(dict(variant=name, batch=batch, repeat=repeat, state=state,
                                       synthetic=state == 'synthetic_repeat', corpus_ms=total,
                                       batch_timings=timing(durations), batch_elapsed_ms=durations,
                                       scope='Sequential full-corpus throughput: technical packets may cross source screenshot boundaries, not a same-frame nine-row latency measurement.',
                                       differences=differences,
                                       observations=[asdict(value) for value in observations],
                                       cache=cache_interval(variant.cache, before) if isinstance(variant, CachedNameReader) else None))
    batch_differences = {str(batch): [i for i, (a, b) in enumerate(zip(references[1], values)) if a != b]
                         for batch, values in references.items()}
    return output, batch_differences


class DeviceStrategyReader:
    """Dispatch NAME to owner-thread workers; numeric OCR remains on this owner."""

    def __init__(self, strategy, numeric_reader=None):
        self.strategy = strategy
        self.numeric_reader = numeric_reader
        self.owner = threading.get_ident()

    def check_thread(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError('strategy adapter used from a different owner')

    def names(self, images, batch_size):
        self.check_thread()
        if batch_size not in (1, 3, 9) or not 1 <= len(images) <= batch_size:
            raise ValueError('batch: 1, 3 or 9')
        return self.strategy.read(images)

    def number(self, image, field):
        if self.numeric_reader is None:
            raise RuntimeError('NAME-only adapter has no numeric OCR')
        return self.numeric_reader.number(image, field)


def device_combinations(cpu_reader, models, spec_name, images, capacities, namespace, repeats):
    """Selected device backend plus cache, then shared preparation plus cache.

    The device strategy controls technical batch independently of request size.
    Shared/nonshared phases are sequential so no owner session is reconfigured
    in a timed call. Variant order alternates within each phase.
    """
    from benchmarks.optimization_devices import Strategy, specifications
    spec = next((item for item in specifications() if item.name == spec_name), None)
    if spec is None:
        raise ValueError(f'Unknown device specification {spec_name}')
    references = {batch: read_corpus(cpu_reader, images, batch)[0] for batch in (1, 3, 9)}
    output = []
    strategy = Strategy(spec, models, images)
    try:
        initialization = dict(init_ms=strategy.init_ms, warmup_ms=strategy.warmup_ms)
        adapter = DeviceStrategyReader(strategy, cpu_reader)
        for shared in (False, True):
            if shared:
                for worker in strategy.workers:
                    def wrap(worker=worker):
                        worker.reader = SharedNameReader(worker.reader)
                    worker.submit(wrap).result()
            phase = 'shared' if shared else 'production_preparation'
            phase_namespace = namespace+':'+spec.name+':'+phase
            variants = [(spec.name+':'+phase, adapter)]
            variants += [(f'{spec.name}:{phase}:cache-{capacity}',
                          CachedNameReader(adapter, capacity, phase_namespace)) for capacity in capacities]
            for batch in (1, 3, 9):
                for repeat in range(repeats):
                    order = variants if repeat % 2 == 0 else list(reversed(variants))
                    for name, variant in order:
                        for state in ('cold', 'synthetic_repeat'):
                            if state == 'cold' and isinstance(variant, CachedNameReader):
                                variant.cache.clear()
                            before = variant.cache.stats() if isinstance(variant, CachedNameReader) else None
                            observations, durations, total = read_corpus(variant, images, batch)
                            differences = [i for i, (a, b) in enumerate(zip(references[batch], observations)) if a != b]
                            output.append(dict(variant=name, phase=phase, request_size=batch,
                                               technical_batch=spec.batch, spec=asdict(spec), repeat=repeat,
                                               state=state, synthetic=state == 'synthetic_repeat',
                                               corpus_ms=total, batch_timings=timing(durations),
                                               batch_elapsed_ms=durations,
                                               scope='Sequential full-corpus throughput: request packets may cross source screenshot boundaries; technical batch is controlled by spec.',
                                               differences=differences,
                                               observations=[asdict(value) for value in observations],
                                               cache=cache_interval(variant.cache, before) if isinstance(variant, CachedNameReader) else None))
        return dict(initialization=initialization, spec=asdict(spec), measurements=output,
                    note='Shared preparation and production preparation run in separate serial phases.')
    finally:
        strategy.close()


def decision_signature(chosen, decisions):
    def strip(decision):
        if decision is None:
            return None
        result = decision.to_dict()
        result.pop('elapsed_ms')
        return result
    return dict(chosen=strip(chosen), decisions=[strip(value) for value in decisions])


def scan_bench(reader, shared, images, frames, capacities, namespace, repeats, batches=(1, 9)):
    """Actual Scanner, production numeric OCR, temporary SQLite catalog, no input."""
    # Fit artificial pricing to observations, never to change OCR labels/results.
    variants = [('production', reader), ('shared', shared)]
    capacity = capacities[-1]
    variants.extend([(f'cache-{capacity}', CachedNameReader(reader, capacity, namespace)),
                     (f'shared-cache-{capacity}', CachedNameReader(shared, capacity, namespace))])
    output = []
    with tempfile.TemporaryDirectory(prefix='stalbot-cache-bench-') as tmp:
        store = Store(Path(tmp)/'catalog.sqlite3')
        identities, _, _ = read_corpus(reader, images, 1)
        rules = {(identity.name, identity.color, identity.upgrade):
                 Rule(identity.name, 9_999_999_999, 20_000_000_000, identity.color, identity.upgrade)
                 for identity in identities if identity.valid and not identity.truncated and identity.color != 'unknown'}
        for rule in rules.values():
            store.save_rule(rule)
        full_catalog = Catalog(store.rules())
        for sid, frame in frames:
            frame_identities = reader.names([vision.crop(frame, 'name', row) for row in range(9)], 9)
            catalogs = [('none', Catalog([])), ('all', full_catalog)]
            for label, row in [('first_identity', 0), ('last_identity', 8)]:
                rule, _ = full_catalog.match(frame_identities[row])
                catalogs.append((label, Catalog([rule] if rule else [])))
            for label, catalog in catalogs:
                for group in (1, 3, 9):
                    for metric, batch in ((metric, batch) for batch in batches for metric in ('profit', 'roi')):
                        reference = None
                        for repeat in range(repeats):
                            # Keep production first for the reference, alternate optimized variants.
                            order = variants if repeat % 2 == 0 else [variants[0], *reversed(variants[1:])]
                            for name, variant in order:
                                if isinstance(variant, CachedNameReader):
                                    variant.cache.clear()
                                for state in ('cold', 'synthetic_repeat'):
                                    before = variant.cache.stats() if isinstance(variant, CachedNameReader) else None
                                    timings = {}
                                    result = Scanner(variant).scan(frame, catalog, batch, 9_999_999_999,
                                                                  group_size=group, selection_metric=metric,
                                                                  timings=timings)
                                    signature = decision_signature(*result)
                                    if reference is None:
                                        reference = signature
                                    output.append(dict(source=sid, scenario=label, group_size=group,
                                                       selection_metric=metric, technical_batch=batch, repeat=repeat,
                                                       variant=name, state=state, synthetic=True,
                                                       semantic_agreement=signature == reference,
                                                       result=signature, timings=timings,
                                                       cache=cache_interval(variant.cache, before) if isinstance(variant, CachedNameReader) else None))
    return output


def load_corpus(dataset):
    label_path = dataset/'labels/labels.json'
    label_bytes = label_path.read_bytes()
    data = json.loads(label_bytes)
    images, frames, metadata = [], [], []
    for sid, shot in data['screenshots'].items():
        for row in shot['rows']:
            if row['status'] != 'labeled':
                continue
            info = row['crops']['name']
            path = (dataset/info['path']).resolve()
            if dataset.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
                raise ValueError(f'Invalid crop {path}')
            with Image.open(path) as image:
                images.append(np.array(image.convert('RGB')))
            metadata.append(dict(source=sid, row=row['row'], crop_sha256=info['sha256']))
        if not shot.get('menu_after'):
            path = (dataset/shot['source']).resolve()
            if dataset.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != sid:
                raise ValueError(f'Invalid source {path}')
            with Image.open(path) as image:
                frame = np.array(image.convert('RGB'))
            vision.validate_frame(frame)
            frames.append((sid, frame))
    return images, frames, metadata, hashlib.sha256(label_bytes).hexdigest()


def select_frames(frames, dataset, limit):
    """Deterministic spread; include ordinary and hard sources when limit allows."""
    hard = {hashlib.sha256(path.read_bytes()).hexdigest() for path in (dataset/'hard_cases').glob('*.png')}
    if not limit or limit >= len(frames):
        selected = frames
    else:
        ordinary = [index for index, (sid, _) in enumerate(frames) if sid not in hard]
        difficult = [index for index, (sid, _) in enumerate(frames) if sid in hard]
        candidates = []
        for category in (ordinary, difficult):
            if category:
                candidates.append(category[len(category)//2])
        candidates += [int(value) for value in np.linspace(0, len(frames)-1, min(len(frames), limit*2))]
        candidates += list(range(len(frames)))
        indices = list(dict.fromkeys(candidates))[:limit]
        selected = [frames[index] for index in indices]
    details = [dict(source=sid, category='hard' if sid in hard else 'ordinary') for sid, _ in selected]
    return selected, details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT/'dataset')
    parser.add_argument('--models', type=Path, default=ROOT/'benchmarks/models')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--device-spec', help='Also test one optimization_devices specification with cache/shared prep (e.g. dml_b1)')
    parser.add_argument('--scan-repeats', type=int, default=1)
    parser.add_argument('--scan-batches', type=int, nargs='+', choices=(1, 3, 9), default=[1, 9])
    parser.add_argument('--scan-frame-limit', type=int, default=0,
                        help='0: all eligible sources; positive: explicit partial scanner sample')
    args = parser.parse_args()
    if args.repeats < 1 or args.scan_repeats < 1 or args.scan_frame_limit < 0:
        parser.error('positive repeat counts and nonnegative frame limit required')
    output = args.output.resolve()
    if args.dataset.resolve() == output or args.dataset.resolve() in output.parents:
        parser.error('output must be outside dataset')
    output.mkdir(parents=True, exist_ok=False)
    images, frames, metadata, label_hash = load_corpus(args.dataset)
    if not images:
        parser.error('empty corpus')
    unique, capacities = capacities_for(images)
    model_names = ('rapid/cyrillic_PP-OCRv5_rec_mobile.onnx', 'tessdata/rus.traineddata', 'tessdata/eng.traineddata')
    model_hashes = {name: hashlib.sha256((args.models/name).read_bytes()).hexdigest() for name in model_names}
    code_paths = [Path(__file__), ROOT/'stalbot/vision.py', ROOT/'stalbot/domain.py', ROOT/'stalbot/scanner.py', ROOT/'stalbot/storage.py']
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in code_paths}
    namespace = hashlib.sha256(json.dumps(dict(model=model_hashes, code=hashes, config='production CPU threads2'), sort_keys=True).encode()).hexdigest()
    manifest = dict(created_utc=datetime.now(timezone.utc).isoformat(), corpus_rows=len(images),
                    unique_rgb_shapes=unique, capacities=capacities, labels_sha256=label_hash,
                    code_sha256=hashes, models_sha256=model_hashes, samples=metadata,
                    platform=platform.platform(), python=sys.version, namespace=namespace,
                    arguments={key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
                    limitations=['Offline, no game load/FPS/capture/menu/BUY timings.',
                                 'Repeated corpus and scanner cases are synthetic; hit rates are not expected live rates.',
                                 'First/last identity catalogs do not guarantee the first/last eligible row: numbers remain real.'])
    (output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    report = dict(preparation=preparation_check(images, args.repeats),
                  cache_micro=cache_microbench(images, capacities, namespace))
    print(f'preparation PASS; corpus {len(images)}, unique {unique}, capacities {capacities}', flush=True)
    vision.prepare_native_runtime()
    started = perf_counter()
    reader = vision.Reader(args.models)
    report['reader_init_ms'] = (perf_counter()-started)*1000
    try:
        report['warmup_ms'] = {str(batch): reader.warmup(batch) for batch in (1, 3, 9)}
        report['names'], report['batch_differences'] = name_bench(reader, images, capacities, namespace, args.repeats)
        (output/'names.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('NAME comparisons complete', flush=True)
        if args.device_spec:
            report['device_combinations'] = device_combinations(reader, args.models, args.device_spec,
                                                               images, capacities, namespace, args.repeats)
            (output/'device-combinations.json').write_text(json.dumps(report['device_combinations'], ensure_ascii=False, indent=2), encoding='utf-8')
        if args.scan:
            selected_frames, frame_details = select_frames(frames, args.dataset, args.scan_frame_limit)
            report['scanner'] = scan_bench(reader, SharedNameReader(reader), images, selected_frames,
                                           capacities, namespace, args.scan_repeats, args.scan_batches)
            report['scanner_sources'] = dict(eligible=len(frames), tested=len(selected_frames), sources=frame_details)
    finally:
        reader.close()
    if hashlib.sha256((args.dataset/'labels/labels.json').read_bytes()).hexdigest() != label_hash:
        raise RuntimeError('Labels changed during run')
    report['pass'] = (all(not row['differences'] for row in report['names'])
                      and all(not value for value in report['batch_differences'].values())
                      and all(not row['differences'] for row in report.get('device_combinations', {}).get('measurements', []))
                      and all(row['semantic_agreement'] for row in report.get('scanner', [])))
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"PASS={report['pass']}; {output}", flush=True)
    if not report['pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
