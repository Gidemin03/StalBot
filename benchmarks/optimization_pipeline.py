"""Offline bounded NAME/numeric pipeline versus the production Scanner.

Only the next NAME packet of the current selection group may be prefetched.
Native NAME sessions belong to optimization_devices workers; numeric Reader is
created and used on the main owner thread. No desktop/input/purchase code runs.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.optimization_cache import DeviceStrategyReader, decision_signature, load_corpus, select_frames
from benchmarks.optimization_devices import Strategy, specifications, process_rss, gpu_snapshot
from stalbot.domain import Catalog, Rule
from stalbot.scanner import Scanner, ScanCancelled
from stalbot.vision import Reader, crop, prepare_native_runtime, validate_frame


class PipelineScanner:
    """Benchmark prototype: at most one NAME packet in flight; no cross-group lookahead."""

    def __init__(self, name_reader, numeric_reader):
        self.names = name_reader
        self.rows = Scanner(numeric_reader)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='NAME-prefetch-dispatch')
        self.closed = False
        self.last_stats = {}

    def scan(self, frame, catalog, batch_size, max_lot_price, balance=None,
             cancel=lambda: False, *, group_size=1, selection_metric='profit',
             timings=None, exclude=lambda decision: False):
        started = perf_counter()
        if self.closed:
            raise RuntimeError('pipeline closed')
        validate_frame(frame)
        if batch_size not in (1, 3, 9) or group_size not in (1, 3, 9):
            raise ValueError('batch and group: 1, 3 or 9')
        if selection_metric not in ('profit', 'roi'):
            raise ValueError('selection_metric: profit or roi')
        metrics = dict(name_ms=0.0, name_wait_ms=0.0, quantity_ms=0.0,
                       buyout_ms=0.0, evaluation_ms=0.0, total_ms=0.0)
        stats = dict(name_submitted_rows=[], name_consumed_rows=[], name_completed_rows=[],
                     excess_rows=[], max_inflight_packets=0, drain_ms=0.0, cancelled=False)
        self.last_stats = stats
        pending = None
        results = []

        def check_cancel():
            if cancel():
                stats['cancelled'] = True
                raise ScanCancelled()

        def submit(indices):
            check_cancel()
            # Copy from the immutable input frame before dispatch; workers never
            # capture fresh frames or mutate scanner state.
            images = [crop(frame, 'name', row) for row in indices]
            stats['name_submitted_rows'].extend(indices)
            stats['max_inflight_packets'] = max(stats['max_inflight_packets'], 1)

            def read():
                tick = perf_counter()
                values = self.names.names(images, batch_size)
                if len(values) != len(indices):
                    raise RuntimeError('NAME worker returned invalid packet size')
                return indices, values, (perf_counter()-tick)*1000
            return self.executor.submit(read)

        def receive(future):
            tick = perf_counter()
            indices, values, elapsed = future.result()
            metrics['name_wait_ms'] += (perf_counter()-tick)*1000
            metrics['name_ms'] += elapsed
            stats['name_completed_rows'].extend(indices)
            return indices, values, elapsed

        try:
            for start in range(0, 9, group_size):
                check_cancel()
                group_end = min(start+group_size, 9)
                packets = [list(range(first, min(first+batch_size, group_end)))
                           for first in range(start, group_end, batch_size)]
                pending = submit(packets[0])
                group = []
                for packet_index in range(len(packets)):
                    current = pending
                    pending = None
                    indices, identities, elapsed = receive(current)
                    check_cancel()
                    # Exactly the next required packet overlaps the numeric work
                    # below, and never enters a later selection group.
                    if packet_index+1 < len(packets):
                        pending = submit(packets[packet_index+1])
                    for row, identity in zip(indices, identities):
                        check_cancel()
                        result = self.rows.row(frame, row, catalog, max_lot_price,
                                               balance, identity, timings=metrics)
                        if result.buy and exclude(result):
                            result.reason = 'uncertain_lot'
                        result.elapsed_ms += elapsed/len(indices)
                        results.append(result)
                        group.append(result)
                        stats['name_consumed_rows'].append(row)
                        check_cancel()
                tick = perf_counter()
                candidates = [result for result in group if result.buy]

                def score(result):
                    profit = Decimal(result.profit)
                    return profit if selection_metric == 'profit' else Fraction(profit)/result.buyout.value

                chosen = max(candidates, key=score, default=None)
                metrics['evaluation_ms'] += (perf_counter()-tick)*1000
                check_cancel()
                if chosen is not None:
                    return chosen, results
            return None, results
        finally:
            # Native inference cannot be safely interrupted: finish the bounded
            # packet before reuse/close, and discard its observations.
            if pending is not None:
                tick = perf_counter()
                try:
                    receive(pending)
                except BaseException:
                    # A primary cancellation/error is already propagating.
                    pass
                stats['drain_ms'] = (perf_counter()-tick)*1000
            stats['excess_rows'] = sorted(set(stats['name_submitted_rows'])-set(stats['name_consumed_rows']))
            metrics['total_ms'] = (perf_counter()-started)*1000
            if timings is not None:
                timings.clear()
                timings.update(metrics)

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)


def make_catalog_scenarios(numeric_reader, frame):
    """Synthetic rule sets against real OCR; precise early/late candidate targets.

    Prices/quantities remain untouched. Target rows with unusable price cannot
    become candidates; actual expected rows are recorded, never asserted by name.
    Duplicate identities can also make an earlier row eligible.
    """
    identities = numeric_reader.names([crop(frame, 'name', row) for row in range(9)], 9)
    rules = {}
    for identity in identities:
        if identity.valid and not identity.truncated and identity.color != 'unknown':
            key = identity.name, identity.color, identity.upgrade
            rules[key] = Rule(identity.name, 9_999_999_999, 20_000_000_000,
                              identity.color, identity.upgrade)
    all_catalog = Catalog(rules.values())
    output = [('none', Catalog([])), ('all', all_catalog)]
    for name, indices in [('early', range(9)), ('late', reversed(range(9)))]:
        selected = None
        for row in indices:
            rule, _ = all_catalog.match(identities[row])
            if rule is None:
                continue
            decision = Scanner(numeric_reader).row(frame, row, all_catalog,
                                                  9_999_999_999, identity=identities[row])
            if decision.buy:
                selected = rule
                break
        output.append((name, Catalog([selected] if selected else [])))
    return output


def compare_strategy(numeric_reader, strategy, frames, repeats, request_sizes):
    adapter = DeviceStrategyReader(strategy, numeric_reader)
    baseline = Scanner(adapter)
    # DeviceStrategyReader deliberately belongs to this main owner (numbers and
    # production Scanner). Strategy is the thread-safe NAME dispatch facade;
    # its native sessions stay inside their own worker executors.
    pipeline = PipelineScanner(strategy, numeric_reader)
    output = []
    try:
        for sid, frame in frames:
            scenarios = make_catalog_scenarios(numeric_reader, frame)
            for scenario, catalog in scenarios:
                for group in (1, 3, 9):
                    for metric in ('profit', 'roi'):
                        for request in request_sizes:
                            reference = None
                            for repeat in range(repeats):
                                variants = [('production_scanner', baseline), ('pipeline', pipeline)]
                                if repeat % 2:
                                    variants.reverse()
                                # Establish semantic reference independently before alternating timing.
                                if reference is None:
                                    reference = decision_signature(*baseline.scan(frame, catalog, request,
                                                                  9_999_999_999, group_size=group,
                                                                  selection_metric=metric))
                                for name, scanner in variants:
                                    timings = {}
                                    result = scanner.scan(frame, catalog, request, 9_999_999_999,
                                                          group_size=group, selection_metric=metric,
                                                          timings=timings)
                                    signature = decision_signature(*result)
                                    output.append(dict(source=sid, scenario=scenario, group_size=group,
                                                       selection_metric=metric, request_size=request,
                                                       technical_batch=strategy.spec.batch,
                                                       spec=asdict(strategy.spec), repeat=repeat, variant=name,
                                                       synthetic_catalog=True, semantic_agreement=signature == reference,
                                                       result=signature, timings=timings,
                                                       pipeline_stats=dict(pipeline.last_stats) if name == 'pipeline' else None))
    finally:
        pipeline.close()
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT/'dataset')
    parser.add_argument('--models', type=Path, default=ROOT/'benchmarks/models')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--specs', nargs='+', default=['cpu_w3_t1', 'dml_b1', 'dml_b9'])
    parser.add_argument('--request-sizes', type=int, nargs='+', choices=(1, 3, 9), default=[1, 9])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--frame-limit', type=int, default=0, help='0: all sources; positive limit is explicitly partial')
    args = parser.parse_args()
    if args.repeats < 1 or args.frame_limit < 0:
        parser.error('positive repeats and nonnegative frame limit required')
    output = args.output.resolve()
    if output == args.dataset.resolve() or args.dataset.resolve() in output.parents:
        parser.error('output must be outside dataset')
    specs = {spec.name: spec for spec in specifications()}
    if any(name not in specs for name in args.specs):
        parser.error('unknown device specification')
    output.mkdir(parents=True, exist_ok=False)
    images, frames, metadata, labels_hash = load_corpus(args.dataset)
    selected, frame_details = select_frames(frames, args.dataset, args.frame_limit)
    manifest = dict(created_utc=datetime.now(timezone.utc).isoformat(), labels_sha256=labels_hash,
                    corpus_rows=len(images), eligible_sources=len(frames), tested_sources=len(selected),
                    sources=frame_details, specs=[asdict(specs[name]) for name in args.specs],
                    code_sha256={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in [Path(__file__), ROOT/'benchmarks/optimization_cache.py',
                                              ROOT/'benchmarks/optimization_devices.py', ROOT/'stalbot/scanner.py',
                                              ROOT/'stalbot/vision.py', ROOT/'stalbot/domain.py']},
                    model_sha256={str(path.relative_to(args.models)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in sorted(args.models.rglob('*')) if path.suffix in ('.onnx', '.traineddata')},
                    arguments={key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
                    limitations=['Offline synthetic catalogs; no game load, capture, menu or BUY.',
                                 'Pipeline restricts requests to current selection group; production may prefetch beyond group.',
                                 'NAME compute time overlaps numbers and is not additive with total_ms.',
                                 'Identities repeated in a frame may make early/late catalog scenarios select other rows.'])
    (output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    prepare_native_runtime()
    reader = Reader(args.models)
    report = dict(warmup_ms={})
    try:
        report['warmup_ms'] = {str(batch): reader.warmup(batch) for batch in (1, 3, 9)}
        for name in args.specs:
            resources_before = dict(process=process_rss(), gpu=gpu_snapshot())
            strategy = Strategy(specs[name], args.models, images)
            try:
                values = compare_strategy(reader, strategy, selected, args.repeats, args.request_sizes)
                report[name] = dict(init_ms=strategy.init_ms, warmup_ms=strategy.warmup_ms,
                                    measurements=values,
                                    resources_before=resources_before,
                                    resources_after=dict(process=process_rss(), gpu=gpu_snapshot()),
                                    semantic_agreement=all(row['semantic_agreement'] for row in values))
                (output/(name+'.json')).write_text(json.dumps(report[name], ensure_ascii=False, indent=2), encoding='utf-8')
                print(f"{name}: agreement={report[name]['semantic_agreement']}, scans={len(values)}", flush=True)
            finally:
                strategy.close()
    finally:
        reader.close()
    if hashlib.sha256((args.dataset/'labels/labels.json').read_bytes()).hexdigest() != labels_hash:
        raise RuntimeError('labels changed during benchmark')
    report['pass'] = all(report[name]['semantic_agreement'] for name in args.specs)
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"PASS={report['pass']}; {output}", flush=True)
    if not report['pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
