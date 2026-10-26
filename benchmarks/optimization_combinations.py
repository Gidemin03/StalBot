"""End-to-end offline Scanner comparison of selected NAME optimization combinations.

Two real frames, synthetic matching catalogs, fresh numeric OCR on every scan.
Batch9/group9 only. No capture, input, purchases, or production storage.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.optimization_cache import (CachedNameReader, SharedNameReader, DeviceStrategyReader,
    decision_signature, load_corpus, select_frames)
from benchmarks.optimization_devices import Strategy, specifications
from benchmarks.optimization_pipeline import make_catalog_scenarios
from stalbot.scanner import Scanner
from stalbot.vision import Reader, prepare_native_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('positive repeats required')
    output = args.output.resolve()
    if output == ROOT/'dataset' or ROOT/'dataset' in output.parents:
        parser.error('dataset output forbidden')
    output.mkdir(parents=True, exist_ok=False)
    images, frames, _, labels_hash = load_corpus(ROOT/'dataset')
    frames, sources = select_frames(frames, ROOT/'dataset', 2)
    prepare_native_runtime()
    reader = Reader(ROOT/'benchmarks/models')
    strategy = None
    records = []
    try:
        warmup = reader.warmup(9)
        spec = next(spec for spec in specifications() if spec.name == 'dml_b9')
        strategy = Strategy(spec, ROOT/'benchmarks/models', images)
        worker = strategy.workers[0]
        # Both wrappers share exactly one native model, always called on its
        # existing owner. Switching a wrapper is outside measured Scanner time.
        native = worker.reader
        shared_gpu = worker.submit(lambda: SharedNameReader(native)).result()
        gpu = DeviceStrategyReader(strategy, reader)
        variants = {
            'cpu_b9': reader,
            'cpu_shared_cache75': CachedNameReader(SharedNameReader(reader), 75, 'combo-cpu-shared-v1'),
            'dml_b9': gpu,
            'dml_shared_cache75': CachedNameReader(gpu, 75, 'combo-dml-shared-v1'),
        }
        rng = random.Random(20261016)
        for sid, frame in frames:
            catalog = dict(make_catalog_scenarios(reader, frame))['all']
            for metric in ('profit', 'roi'):
                reference = decision_signature(*Scanner(reader).scan(frame, catalog, 9, 9_999_999_999,
                                                     group_size=9, selection_metric=metric))
                for repeat in range(args.repeats):
                    order = list(variants)
                    rng.shuffle(order)
                    for name in order:
                        variant = variants[name]
                        desired = shared_gpu if name == 'dml_shared_cache75' else native
                        worker.submit(lambda desired=desired: setattr(worker, 'reader', desired)).result()
                        if isinstance(variant, CachedNameReader):
                            variant.cache.clear()
                        for state in ('cold', 'synthetic_repeat'):
                            timings = {}
                            result = Scanner(variant).scan(frame, catalog, 9, 9_999_999_999,
                                         group_size=9, selection_metric=metric, timings=timings)
                            signature = decision_signature(*result)
                            records.append(dict(source=sid, metric=metric, repeat=repeat, variant=name,
                                                state=state, batch_size=9, group_size=9, timings=timings,
                                                agreement=signature==reference, result=signature))
            print(f'finished source {sid[:8]}', flush=True)
        report = dict(created_utc=datetime.now(timezone.utc).isoformat(), sources=sources,
                      labels_sha256=labels_hash, measurements=records, passed=all(r['agreement'] for r in records),
                      initialization=dict(cpu_warmup_ms=warmup, gpu_init_ms=strategy.init_ms,
                                          gpu_warmup_ms=strategy.warmup_ms),
                      source_sha256={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in [Path(__file__), ROOT/'benchmarks/optimization_cache.py',
                                       ROOT/'benchmarks/optimization_devices.py', ROOT/'benchmarks/optimization_pipeline.py',
                                       ROOT/'stalbot/vision.py', ROOT/'stalbot/scanner.py', ROOT/'stalbot/domain.py']},
                      model_sha256={name:hashlib.sha256((ROOT/'benchmarks/models'/name).read_bytes()).hexdigest()
                          for name in ('rapid/cyrillic_PP-OCRv5_rec_mobile.onnx','tessdata/rus.traineddata','tessdata/eng.traineddata')},
                      limits=['Synthetic catalog matching all eligible identities; real numeric OCR and all nine rows.',
                              'Cold clears only the NAME cache; models are warm.',
                              'Repeat cache hits are synthetic, not a gameplay hit-rate estimate.',
                              'Scanner timing excludes capture, interface, menu, persistence and real BUY.',
                              'No game FPS/frametime or controlled external load.'])
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'PASS={report["passed"]}; scans={len(records)}; {output}', flush=True)
        if not report['passed']:
            raise SystemExit(1)
    finally:
        if strategy is not None:
            strategy.close()
        reader.close()


if __name__ == '__main__':
    main()
