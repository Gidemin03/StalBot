"""Offline secondary optimization experiments. Never imports a desktop/input backend.

Run stages separately while no other benchmark is running. Reports compare primitive
costs, not live cycle latency. All prototypes live here, not in production.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import sqlite3
import sys
import tempfile
import threading
from time import perf_counter
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
from PIL import Image
from stalbot.domain import Decision, Identity, Number, Rule, evaluate
from stalbot.interface import Interface
from stalbot.storage import Store, lot_key
from stalbot.vision import Reader, crop, quantity_presence, prepare_native_runtime


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def timed(call):
    start = perf_counter()
    value = call()
    return value, (perf_counter() - start) * 1000


def summary(values):
    return {'n': len(values), 'p50_ms': float(np.percentile(values, 50)),
            'p95_ms': float(np.percentile(values, 95)), 'min_ms': min(values),
            'max_ms': max(values)}


class Report:
    def __init__(self, output, args):
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        self.raw = []
        self.gates = []
        self.notes = {}
        self.manifest = {'created_utc': datetime.now(timezone.utc).isoformat(),
                         'args': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                         'python': sys.version, 'platform': platform.platform(),
                         'cpu_count': os.cpu_count(), 'opencv': cv2.__version__,
                         'numpy': np.__version__, 'live_validation': False,
                         'code_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                                         [Path(__file__), *sorted((ROOT / 'stalbot').glob('*.py'))]},
                         'inputs': {}}

    def input(self, path):
        path = Path(path).resolve()
        self.manifest['inputs'][str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)] = sha(path)

    def sample(self, stage, variant, trial, ms, **extra):
        self.raw.append(dict(stage=stage, variant=variant, trial=trial, ms=ms, **extra))

    def gate(self, name, ok, details=None):
        self.gates.append({'name': name, 'passed': bool(ok), 'details': details})
        if not ok:
            self.save()
            raise AssertionError(name)

    def save(self):
        grouped = {}
        for row in self.raw:
            grouped.setdefault(f"{row['stage']}/{row['variant']}", []).append(row['ms'])
        for name, payload in [('manifest', self.manifest), ('summary', {
                'timings': {key: summary(values) for key, values in grouped.items()},
                'gates': self.gates, 'notes': self.notes})]:
            (self.output / f'{name}.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        (self.output / 'raw.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in self.raw), encoding='utf-8')


def read_frame(path, report):
    report.input(path)
    with Image.open(path) as image:
        return np.array(image.convert('RGB'))


def classify(ui, frame):
    # Evaluate every result template even after a hit: ambiguity must survive.
    return (ui.auction(frame), ui.any_menu(frame), ui.blocking_modal(frame), ui.outcome(frame))


class NarrowFallback(Interface):
    """Try calibrated sub-ROI first, full baseline ROI on misses, per template.

    Searching only the narrow ROI would miss moved/unknown dialogs. A narrow hit
    implies a baseline hit; other templates still get their full fallback search.
    """
    def __init__(self, assets):
        super().__init__(assets)
        self.boxes = json.loads((Path(assets) / 'manifest.json').read_text(encoding='utf-8'))

    def score(self, frame, name, box):
        if name not in ('success', 'unavailable', 'insufficient', 'modal_ok'):
            return super().score(frame, name, box)
        x1, y1, x2, y2 = self.boxes[name]['box']
        narrow = (max(box[0], x1 - 3), max(box[1], y1 - 3),
                  min(box[2], x2 + 3), min(box[3], y2 + 3))
        value = super().score(frame, name, narrow)
        return value if value >= .95 else super().score(frame, name, box)


def interface_cases(ui):
    blank = np.full((1080, 1920, 3), 25, np.uint8)
    unknown = blank.copy()
    t = ui.templates['modal_ok']
    unknown[556:556+t.shape[0], 877:877+t.shape[1]] = t
    conflict = unknown.copy()
    for name, y in [('success', 438), ('unavailable', 500)]:
        t = ui.templates[name]
        conflict[y:y+t.shape[0], 750:750+t.shape[1]] = t
    moved = blank.copy()
    t = ui.templates['success']
    moved[435:435+t.shape[0], 735:735+t.shape[1]] = t
    return {'synthetic_blank': blank, 'synthetic_unknown': unknown,
            'synthetic_conflict': conflict, 'synthetic_moved': moved}


def interface_suite(report, args):
    assets = ROOT / 'assets/ui'
    for p in sorted(assets.glob('*')):
        if p.is_file(): report.input(p)
    normal, narrow = Interface(assets), NarrowFallback(assets)
    frames = {p.name: read_frame(p, report) for p in sorted((ROOT / 'dataset/templates').glob('*.png'))}
    extra = ROOT / 'validation/insufficient-20261015/source.png'
    if extra.exists(): frames['insufficient_source'] = read_frame(extra, report)
    frames.update(interface_cases(normal))
    expected = {key: classify(normal, frame) for key, frame in frames.items()}
    report.gate('unknown modal blocks without outcome', expected['synthetic_unknown'][2:] == (True, None))
    report.gate('conflicting outcomes remain unknown', expected['synthetic_conflict'][3] is None)
    report.gate('moved outcome is recognized', expected['synthetic_moved'][3] == 'SUCCESS')
    for trial in range(args.repeats):
        for key, frame in frames.items():
            def twice():
                return classify(normal, frame), classify(normal, frame)
            def once():
                value = classify(normal, frame)
                return value, value
            variants = [('baseline_twice', twice), ('frame_local_once', once),
                        ('narrow_with_fallback', lambda: (classify(narrow, frame),) * 2)]
            if trial % 2: variants.reverse()
            for variant, run in variants:
                value, ms = timed(run)
                report.gate(f'interface:{key}:{variant}:{trial}', value == (expected[key],) * 2)
                report.sample('interface', variant, trial, ms, frame=key)
    report.notes['interface'] = {'scope': 'Two complete classification requests of the same immutable frame. Actual runner short-circuits and may request fewer checks.',
        'narrow': 'Includes wide fallback on each missing template, including conflicts and unknown modal. No cross-frame caching.',
        'verdict': 'Frame-local reuse is applicable only where the same predicates really repeat. Narrow-only search is rejected; fallback candidate is offline only.'}


def same_selected_fields(before, after, row):
    return all(np.array_equal(crop(before, name, row), crop(after, name, row))
               for name in ('name', 'quantity', 'buyout'))


def reuse_suite(report, args):
    rng = np.random.default_rng(712)
    before = rng.integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
    cases = [('same', before.copy(), True)]
    from stalbot.vision import BOXES
    for field, (x1, y1, _, _) in BOXES.items():
        changed = before.copy(); changed[y1, x1, 0] ^= 1
        cases.append((f'changed_{field}', changed, False))
    highlight = before.copy(); highlight[394:430, 889:1384] //= 2
    cases.append(('synthetic_highlight', highlight, False))
    for trial in range(args.repeats * 10):
        for name, after, expected in cases:
            value, ms = timed(lambda: same_selected_fields(before, after, 0))
            report.gate(f'reuse:{name}:{trial}', value == expected)
            report.sample('reuse', name, trial, ms)
    # Synthetic contract: cached observations are reevaluated with current rules/budget;
    # equality never authorizes sending BUY or removing the immediate pixel guard.
    identity, rule, quantity, price = Identity('Example', 'white', 0), Rule('Example', 200, 400), Number('AMBIGUOUS', None), Number('VALUE', 100)
    report.gate('reuse current budget reevaluated', not evaluate(0, identity, rule, quantity, price, 1000, 50).buy)
    report.gate('reuse ambiguous remains ambiguous', evaluate(0, identity, rule, quantity, price, 1000).quantity.state == 'AMBIGUOUS')
    report.notes['reuse'] = {'verdict': 'Defer production reuse until real paired pre/post-menu frames measure highlight hit rate. Synthetic comparisons only.',
        'guard': 'Final production immediate-frame guard, economy reevaluation, cancellation and transactional BUY protection remain mandatory. This suite sends no input.'}


def unconditional_price_rejection(price, max_lot_price, balance):
    if price.state != 'VALUE' or type(price.value) is not int or not 1 <= price.value <= 9_999_999_999:
        return 'unknown_price'
    if price.value > max_lot_price: return 'lot_price_limit'
    if balance is not None and price.value > balance: return 'balance_limit'
    return None


class NumericReader:
    """Uses production Reader.number verbatim, without loading the NAME model."""
    number = Reader.number
    check_thread = Reader.check_thread

    def __init__(self, models):
        import tesserocr
        self.owner = threading.get_ident()
        self.api = tesserocr.PyTessBaseAPI(path=str(models / 'tessdata'), lang='rus+eng', psm=7)
        self.api.SetVariable('classify_enable_learning', '0')

    def close(self):
        self.check_thread()
        self.api.End()


class NumberWorker:
    def __init__(self, models):
        self.pool = ThreadPoolExecutor(max_workers=1)
        try: self.reader = self.pool.submit(NumericReader, models).result()
        except BaseException:
            self.pool.shutdown(wait=True)
            raise

    def submit(self, image, field):
        return self.pool.submit(self.reader.number, image, field)

    def close(self):
        try: self.pool.submit(self.reader.close).result()
        finally: self.pool.shutdown(wait=True)


def numbers_suite(report, args):
    os.environ['OMP_THREAD_LIMIT'] = '2'
    prepare_native_runtime()
    import tesserocr
    report.manifest['tesseract'] = tesserocr.tesseract_version()
    label_path = ROOT / 'dataset/labels/labels.json'
    report.input(label_path)
    for name in ('rus', 'eng'): report.input(args.models / f'tessdata/{name}.traineddata')
    rows = []
    data = json.loads(label_path.read_text(encoding='utf-8'))
    for sid, shot in data['screenshots'].items():
        for row in shot['rows']:
            if row['status'] != 'labeled': continue
            fields = {}
            for field in ('quantity', 'buyout'):
                record = row['crops'][field]; path = ROOT / 'dataset' / record['path']
                if sha(path) != record['sha256']: raise ValueError(f'Invalid input hash: {path}')
                fields[field] = read_frame(path, report)
            rows.append((sid, row['row'], fields))
    readers = []
    try:
        baseline, ms = timed(lambda: NumericReader(args.models)); readers.append(baseline)
        report.sample('numbers_setup', 'baseline', 0, ms)
        qworker, ms = timed(lambda: NumberWorker(args.models)); readers.append(qworker)
        report.sample('numbers_setup', 'quantity_worker', 0, ms)
        pworker, ms = timed(lambda: NumberWorker(args.models)); readers.append(pworker)
        report.sample('numbers_setup', 'buyout_worker', 0, ms)
        warm_quantity = np.full((21, 37, 3), 24, dtype=np.uint8)
        warm_quantity[5:17, 31:33] = 235
        warm_quantity[5:7, 29:33] = 235
        warm_price = np.full((25, 154, 3), 24, dtype=np.uint8)
        cv2.putText(warm_price, '123 456', (2, 18), cv2.FONT_HERSHEY_SIMPLEX, .55, (235, 235, 235), 1, cv2.LINE_8)
        _, ms = timed(lambda: (baseline.number(warm_quantity, 'quantity'), baseline.number(warm_price, 'buyout'),
                               qworker.submit(warm_quantity, 'quantity').result(), pworker.submit(warm_price, 'buyout').result()))
        report.sample('numbers_setup', 'warmup_all', 0, ms)
        present = sum(quantity_presence(fields['quantity']) for _, _, fields in rows)
        prices = []
        for trial in range(args.repeats):
            for sid, row, fields in rows:
                def sequential():
                    # Return field timing as well; writing measurements happens outside timed pair.
                    q, q_ms = timed(lambda: baseline.number(fields['quantity'], 'quantity'))
                    p, p_ms = timed(lambda: baseline.number(fields['buyout'], 'buyout'))
                    return (q, p), (q_ms, p_ms)
                def parallel():
                    q = qworker.submit(fields['quantity'], 'quantity')
                    p = pworker.submit(fields['buyout'], 'buyout')
                    return q.result(), p.result()
                variants = [('sequential', sequential), ('independent_apis', parallel)]
                if trial % 2: variants.reverse()
                values = {}
                for variant, run in variants:
                    values[variant], ms = timed(run)
                    if variant == 'sequential':
                        values[variant], field_times = values[variant]
                        for field, field_ms in zip(('quantity', 'buyout'), field_times):
                            report.sample('numbers_fields', field, trial, field_ms, screenshot=sid, row=row)
                    report.sample('numbers', variant, trial, ms, screenshot=sid, row=row,
                                  quantity_presence=quantity_presence(fields['quantity']),
                                  observations=[asdict(number) for number in values[variant]])
                report.gate(f'numbers:{sid}:{row}:{trial}', values['sequential'] == values['independent_apis'])
                if trial == 0: prices.append(values['sequential'][1])
        # Enumerate the decision gate, including invalid full prices and quantities.
        rule, identity = Rule('Example', 100, 500, max_stack=2), Identity('Example')
        for p in [Number('VALUE', v) for v in (1, 100, 101, 1000, 0, 10_000_000_000)] + [Number('AMBIGUOUS', None), Number('ABSENT', None)]:
            for q in (Number('VALUE', 1), Number('VALUE', 3), Number('AMBIGUOUS', None)):
                skip = unconditional_price_rejection(p, 1000, 100)
                decision = evaluate(0, identity, rule, q, p, 1000, 100)
                report.gate(f'price-first-no-lost-buy:{p}:{q}', skip is None or not decision.buy)
        report.notes['numbers'] = {'rows': len(rows), 'quantity_requires_ocr': present,
            'matching': 'All labeled rows treated as catalog matches; actual catalog match fraction was not inferred.',
            'quality': 'Complete Number objects including raw text must match sequential production method. Label accuracy is not redefined by this comparison.',
            'buyout_first': {'synthetic_balance_100000_skip_count': sum(unconditional_price_rejection(p, 1_000_000, 100_000) is not None for p in prices),
                'verdict': 'Applicability only: savings depend on actual budget/catalog. Skipped QTY must remain unmeasured (None), not AMBIGUOUS/1; reasons can differ from stack_limit precedence.'},
            'pipeline': 'Deferred: numeric costs quantified here; NAME producer integration/group cancellation requires separate pool comparison. No full-cycle claim.'}
    finally:
        for reader in reversed(readers): reader.close()


class BoundedWriter:
    """Lossless bounded diagnostic queue with backpressure and surfaced errors."""
    def __init__(self, write, capacity=2):
        self.queue = queue.Queue(maxsize=capacity)
        self.write = write
        self.error = None
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def _run(self):
        while True:
            item = self.queue.get()
            try:
                if item is None: return
                if self.error is None:
                    try: self.write(*item)
                    except BaseException as exc: self.error = exc
            finally: self.queue.task_done()

    def submit(self, *item):
        self.queue.put(item)
        if self.error is not None: raise RuntimeError('Diagnostic writer failed') from self.error

    def close(self):
        self.queue.put(None)
        self.thread.join()
        if self.error is not None: raise RuntimeError('Diagnostic writer failed') from self.error


def write_diagnostic(path, frame, payload):
    Image.fromarray(frame).save(path.with_suffix('.png'))
    path.with_suffix('.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')


def diagnostics_suite(report, args):
    path = sorted((ROOT / 'dataset/templates').glob('*.png'))[0]
    frame = read_frame(path, report)
    payload = {'kind': 'synthetic optional diagnostic', 'numbers': list(range(100))}
    for trial in range(args.repeats):
        for variant in (('sync', 'bounded') if trial % 2 == 0 else ('bounded', 'sync')):
            with tempfile.TemporaryDirectory(prefix='diagnostic-', dir=report.output) as folder:
                started = perf_counter()
                writer = BoundedWriter(write_diagnostic) if variant == 'bounded' else None
                try:
                    for i in range(4):
                        target = Path(folder) / str(i)
                        def submit():
                            if writer: writer.submit(target, frame.copy(), dict(payload))
                            else: write_diagnostic(target, frame, payload)
                        _, ms = timed(submit)
                        report.sample('diagnostics_submit', variant, trial, ms, index=i)
                finally:
                    if writer: writer.close()
                report.sample('diagnostics_total', variant, trial, (perf_counter() - started) * 1000)
                for i in range(4):
                    with Image.open(Path(folder) / f'{i}.png') as image:
                        pixels_equal = np.array_equal(np.array(image), frame)
                    report.gate(f'diagnostic:{variant}:{trial}:{i}', pixels_equal and json.loads((Path(folder) / f'{i}.json').read_text(encoding='utf-8')) == payload)
    report.notes['diagnostics'] = {'unit': 'Four full-frame PNG+JSON writes, including queue drain in total. Submission includes snapshot copy and backpressure.',
        'bounded_memory': f'Queue 2 plus 1 in flight and possible blocked producer snapshot; <=4 frame buffers ({4 * frame.nbytes} raw bytes), excluding PNG compression buffers.',
        'scope': 'Optional diagnostics only. Durable BUY intent/outcome/budget stay synchronous and untouched. Disk cache/fsync durability not tested.'}


def synthetic_decision(price):
    return evaluate(0, Identity('Synthetic', 'white', 0), Rule('Synthetic', 2_000_000, 3_000_000),
                    Number('VALUE', 1), Number('VALUE', price), 5_000_000)


def storage_suite(report, args):
    for size in args.history_sizes:
        with tempfile.TemporaryDirectory(prefix='sqlite-', dir=report.output) as folder:
            store = Store(Path(folder) / 'temporary.sqlite3')
            with store.connect() as db:
                db.executemany('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                    ((str(i), 'synthetic', 'UNKNOWN_RECOVERED', json.dumps(synthetic_decision(i + 1).to_dict()), None, 'synthetic') for i in range(size)))
            missing = synthetic_decision(size + 2)
            last = synthetic_decision(size) if size else missing
            with store.connect() as db:
                keys, ms = timed(lambda: {lot_key(json.loads(r[0])) for r in db.execute("SELECT decision FROM attempts WHERE status='UNKNOWN_RECOVERED'")})
                report.sample('storage_snapshot_build', f'n={size}', 0, ms,
                              approximate_python_bytes=sys.getsizeof(keys) + sum(sys.getsizeof(key) + sum(sys.getsizeof(v) for v in key) for key in keys))
                for trial in range(args.repeats):
                    for name, decision in [('miss', missing), ('last', last)]:
                        variants = [('fresh_connection_scan', lambda: store.uncertain_lot(decision)),
                                    ('reused_connection_scan', lambda: Store._uncertain_lot(db, decision)),
                                    ('cycle_snapshot_lookup', lambda: lot_key(decision.to_dict()) in keys)]
                        if trial % 2: variants.reverse()
                        for variant, run in variants:
                            value, ms = timed(run)
                            report.gate(f'storage:{size}:{trial}:{variant}:{name}', value == (size > 0 and name == 'last'))
                            report.sample('storage', f'{variant}/n={size}/{name}', trial, ms)
                    _, ms = timed(store.balance)
                    report.sample('storage_connection', f'n={size}', trial, ms)
            if size:
                try: store.begin_buy('duplicate', last)
                except RuntimeError: blocked = True
                else: blocked = False
                report.gate(f'transactional-repeat-buy-block:{size}', blocked)
    report.notes['storage'] = {'scope': 'Temporary databases only. Worst-case missing/last-key scans; history size is synthetic, not actual working DB usage.',
        'snapshot': 'Build measured separately. Snapshot is cycle-local advisory exclusion only; begin_buy must retain its final transaction and current UNKNOWN check. No production index/connection change.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('interface', 'reuse', 'numbers', 'diagnostics', 'storage', 'all'), required=True)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--history-sizes', type=int, nargs='+', default=[0, 100, 1000, 10000])
    parser.add_argument('--models', type=Path, default=ROOT / 'benchmarks/models')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or any(n < 0 for n in args.history_sizes): parser.error('Positive repeats and nonnegative history sizes required')
    output = args.output or ROOT / 'benchmarks/results' / f"secondary-{args.suite}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    output = output.resolve()
    if (ROOT / 'benchmarks/results').resolve() not in output.parents: parser.error('Output must be a new directory under benchmarks/results')
    report = Report(output, args)
    suites = dict(interface=interface_suite, reuse=reuse_suite, numbers=numbers_suite,
                  diagnostics=diagnostics_suite, storage=storage_suite)
    try:
        for key, suite in suites.items():
            if args.suite in (key, 'all'):
                print(f'Running {key}', flush=True)
                suite(report, args)
                report.save()
    finally: report.save()
    print(output)


if __name__ == '__main__': main()
