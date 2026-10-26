"""Offline NAME device/scheduling experiments using the actual Reader.names method.

No desktop, storage, purchase, dataset writes, or model downloads. Each model is
created, called and destroyed by one owning thread. Profiles run after timing.
Run --help for a reproducible full-corpus experiment; output must be a new folder.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import queue
import random
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stalbot.vision import Reader, prepare_native_runtime


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def process_rss():
    """Current process resident bytes, never claim this is per-session VRAM."""
    try:
        import psutil
        p = psutil.Process()
        return {'rss_bytes': p.memory_info().rss, 'cpu_seconds': sum(p.cpu_times()[:2])}
    except ImportError:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                    (n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize',
                    'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return {'rss_bytes': counters.WorkingSetSize,
                        'process_peak_rss_bytes': counters.PeakWorkingSetSize,
                        'cpu_seconds': time.process_time()}
        return {'rss_bytes': None, 'cpu_seconds': time.process_time()}


def gpu_snapshot():
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.used,utilization.gpu',
                                 '--format=csv,noheader,nounits'], capture_output=True,
                                text=True, timeout=10, check=False)
        return {'returncode': result.returncode, 'csv': result.stdout.strip(),
                'note': 'Whole GPU snapshot; Windows WDDM per-process VRAM may be unavailable.'}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'unavailable': str(exc)}


@dataclass(frozen=True)
class Spec:
    name: str
    devices: tuple[str, ...]
    threads: int = 2
    batch: int = 1
    schedule: str = 'serial'
    production: bool = False


def specifications():
    result = [Spec(f'baseline_cpu_b{b}', ('cpu',), batch=b, production=True) for b in (1, 3, 9)]
    result += [Spec(f'cpu_w{w}_t{t}', ('cpu',) * w, threads=t,
                    schedule='dynamic') for w in (1, 2, 3) for t in (1, 2)]
    result += [Spec(f'dml_b{b}', ('dml',), batch=b) for b in (1, 3, 9)]
    result += [Spec('hybrid_fixed_alternate', ('cpu', 'dml'), schedule='alternate'),
               Spec('hybrid_fixed_3plus3', ('cpu', 'dml'), batch=3, schedule='chunks3'),
               Spec('hybrid_dynamic', ('cpu', 'dml'), schedule='dynamic')]
    result += [Spec(f'dml_sessions{w}', ('dml',) * w, schedule='dynamic') for w in (2, 3)]
    return result


class NameReader(Reader):
    """NAME-only native Reader; inherited names is exactly production code.

    This excludes unused numeric APIs from candidate memory. Baseline uses the
    unmodified full Reader, so reports disclose that distinction explicitly.
    """
    def __init__(self, models, device, threads):
        from rapidocr import RapidOCR, ModelType, OCRVersion
        import onnxruntime as ort
        self.owner = threading.get_ident()
        self.device = device
        self.threads = threads
        model_path = Path(models) / 'rapid/cyrillic_PP-OCRv5_rec_mobile.onnx'
        if not model_path.is_file():
            raise FileNotFoundError(model_path)
        self.model = RapidOCR(params={
            'Global.model_root_dir': str(Path(models) / 'rapid'),
            'Global.use_det': False, 'Global.use_cls': False,
            'Global.use_preprocess_img': False, 'Global.use_vertical_padding': False,
            'Global.text_score': 0.0, 'Global.log_level': 'warning',
            'Rec.lang_type': 'cyrillic', 'Rec.model_type': ModelType.MOBILE,
            'Rec.ocr_version': OCRVersion('PP-OCRv5'), 'Rec.model_path': str(model_path),
            'EngineConfig.onnxruntime.intra_op_num_threads': threads,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1,
            # The recognizer loads on CPU, before a safe DML session is installed.
            'EngineConfig.onnxruntime.use_dml': False})
        self.model._load_rec_model()
        if device == 'dml':
            if 'DmlExecutionProvider' not in ort.get_available_providers():
                raise RuntimeError('DmlExecutionProvider unavailable; no CPU substitute allowed')
            self.replace_session()
        providers = self.model.text_rec.session.session.get_providers()
        if device == 'cpu' and providers != ['CPUExecutionProvider']:
            raise RuntimeError(f'Unexpected CPU providers: {providers}')
        if device == 'dml' and 'DmlExecutionProvider' not in providers:
            raise RuntimeError(f'DML requested but inactive: {providers}')

    def replace_session(self, profile_prefix=None):
        import onnxruntime as ort
        self.check_thread()
        current = self.model.text_rec.session.session
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.log_severity_level = 3
        if profile_prefix is not None:
            options.enable_profiling = True
            options.profile_file_prefix = str(profile_prefix)
        self.model.text_rec.session.session = ort.InferenceSession(
            current._model_path, sess_options=options,
            providers=['DmlExecutionProvider', 'CPUExecutionProvider'])

    def close(self):
        self.check_thread()
        self.model = None


class Worker:
    def __init__(self, models, device, threads, production=False):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f'NAME-{device}')
        self.device = device
        self.reader = None
        def initialize():
            start = time.perf_counter()
            self.reader = Reader(models) if production else NameReader(models, device, threads)
            return (time.perf_counter() - start) * 1000
        try:
            self.init_ms = self.executor.submit(initialize).result()
        except BaseException:
            self.executor.shutdown(wait=True)
            raise

    def submit(self, function, *args):
        return self.executor.submit(function, *args)

    def warmup(self, batch, images):
        def run():
            start = time.perf_counter()
            # NAME shapes from real inputs, no Tesseract time included in NAME warmup.
            for size in (1, 3, 9):
                for offset in (0, len(images) // 2, len(images) - size):
                    chunk = images[max(0, offset):max(0, offset) + size]
                    for first in range(0, len(chunk), batch):
                        self.reader.names(chunk[first:first + batch], batch)
            return (time.perf_counter() - start) * 1000
        return self.submit(run).result()

    def close(self):
        try:
            if self.reader is not None:
                def release():
                    self.reader.close()
                    self.reader = None
                    gc.collect()
                self.submit(release).result()
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)


def fixed_assignments(n, count, schedule):
    """Indexed jobs; chunk3 yields exactly CPU3+GPU3 for the six-row workload."""
    if schedule == 'alternate':
        return [[i] for i in range(n)], [i % count for i in range(n)]
    if schedule == 'chunks3':
        jobs = [list(range(i, min(i + 3, n))) for i in range(0, n, 3)]
        return jobs, [i % count for i in range(len(jobs))]
    raise ValueError(schedule)


class Strategy:
    def __init__(self, spec, models, warm_images):
        self.spec = spec
        self.workers = []
        try:
            for device in spec.devices:
                self.workers.append(Worker(models, device, spec.threads, spec.production))
            self.warmup_ms = [w.warmup(spec.batch, warm_images) for w in self.workers]
            self.init_ms = [w.init_ms for w in self.workers]
        except BaseException:
            self.close()
            raise

    def read(self, images, trace=None):
        """Return original row order; workers have no side effects beyond native OCR."""
        n = len(images)
        if not 1 <= n <= 9:
            raise ValueError('NAME work unit must contain 1..9 rows')
        spec = self.spec
        started = time.perf_counter()
        def process(worker_index, indices):
            t = time.perf_counter()
            values = self.workers[worker_index].reader.names([images[i] for i in indices], spec.batch)
            if trace is not None:
                # Append is atomic under CPython; sorted before serialization.
                trace.append({'worker': worker_index, 'device': spec.devices[worker_index],
                              'indices': indices, 'start_ms': (t - started) * 1000,
                              'end_ms': (time.perf_counter() - started) * 1000})
            return list(zip(indices, values))
        futures = []
        if spec.schedule == 'serial':
            for offset in range(0, n, spec.batch):
                futures.append(self.workers[0].submit(process, 0, list(range(offset, min(n, offset + spec.batch)))))
        elif spec.schedule == 'dynamic':
            jobs = queue.Queue()
            for index in range(n):
                jobs.put([index])
            def consume(worker_index):
                output = []
                while True:
                    try:
                        indices = jobs.get_nowait()
                    except queue.Empty:
                        return output
                    output.extend(process(worker_index, indices))
            futures = [w.submit(consume, i) for i, w in enumerate(self.workers)]
        else:
            jobs, owners = fixed_assignments(n, len(self.workers), spec.schedule)
            futures = [self.workers[owner].submit(process, owner, indices)
                       for indices, owner in zip(jobs, owners)]
        indexed = []
        errors = []
        # Drain every worker before returning an error; never leave an old frame running.
        for future in futures:
            try:
                indexed.extend(future.result())
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
        if sorted(i for i, _ in indexed) != list(range(n)):
            raise RuntimeError('Worker lost or duplicated a row')
        return [value for _, value in sorted(indexed)]

    def check_thread(self):
        # This facade may be called by a pipeline coordinator. Native objects
        # remain permanently on their executor's owner thread.
        if not self.workers:
            raise RuntimeError('NAME strategy is closed')

    def names(self, images, batch_size):
        """Reader-compatible NAME facade; this spec controls technical batching."""
        self.check_thread()
        if batch_size not in (1, 3, 9) or not 1 <= len(images) <= batch_size:
            raise ValueError('batch: 1, 3 или 9')
        return self.read(images)

    def proof(self, directory, images):
        """Profile separate sessions after measurements, using every DML batch shape.

        Host spans can show simultaneous calls on independent sessions, but cannot
        establish GPU-kernel overlap. ORT DML fused events are reported as observed.
        """
        records = []
        for index, worker in enumerate(self.workers):
            if worker.device != 'dml':
                continue
            def profile():
                reader = worker.reader
                reader.replace_session(directory / f'worker{index}')
                session = reader.model.text_rec.session.session
                try:
                    for n in sorted({1, min(3, self.spec.batch), min(6, self.spec.batch), self.spec.batch}):
                        reader.names(images[:n], self.spec.batch)
                finally:
                    profile_path = Path(session.end_profiling())
                events = json.loads(profile_path.read_text(encoding='utf-8'))
                providers = Counter(e.get('args', {}).get('provider') for e in events
                                    if e.get('args', {}).get('provider'))
                return {'worker': index, 'profile': str(profile_path),
                        'providers': session.get_providers(), 'node_events': dict(providers),
                        'dml_execution_proven': providers['DmlExecutionProvider'] > 0,
                        'cpu_fallback_events': providers['CPUExecutionProvider'],
                        'note': 'Events aggregate these tested shapes only; host overlap is not proof of kernel overlap.'}
            records.append(worker.submit(profile).result())
        trace = []
        self.read(images[:9], trace=trace)
        return {'sessions': records, 'host_call_spans': sorted(trace, key=lambda x: x['start_ms']),
                'kernel_overlap_proven': False}

    def close(self):
        errors = []
        for worker in self.workers:
            try:
                worker.close()
            except Exception as exc:
                errors.append(str(exc))
        self.workers.clear()
        if errors:
            raise RuntimeError(f'Worker close errors: {errors}')


def load_corpus(dataset, expected_rows=170):
    dataset = Path(dataset).resolve()
    labels = dataset / 'labels/labels.json'
    data = json.loads(labels.read_text(encoding='utf-8'))
    corpus = []
    for sid, shot in data['screenshots'].items():
        for row in shot['rows']:
            if row['status'] != 'labeled':
                continue
            crop = row['crops']['name']
            path = (dataset / crop['path']).resolve()
            if dataset not in path.parents or sha256(path) != crop['sha256']:
                raise ValueError(f'Invalid crop: {path}')
            with Image.open(path) as im:
                image = np.array(im.convert('RGB'))
            image.setflags(write=False)
            corpus.append({'source': sid, 'row': row['row'], 'path': crop['path'],
                           'sha256': crop['sha256'], 'image': image})
    if len(corpus) != expected_rows:
        raise ValueError(f'Expected {expected_rows} labeled NAME crops; got {len(corpus)}')
    return corpus, sha256(labels)


def corpus_groups(corpus):
    """Never mix screenshots; include all labeled rows, even tenth rows, for correctness."""
    groups = []
    for sid in dict.fromkeys(c['source'] for c in corpus):
        indices = [i for i, c in enumerate(corpus) if c['source'] == sid]
        for start in range(0, len(indices), 9):
            groups.append(indices[start:start + 9])
    return groups


def latency_workloads(corpus, frames):
    candidates = []
    for sid in dict.fromkeys(c['source'] for c in corpus):
        indices = [i for i, c in enumerate(corpus) if c['source'] == sid][:9]
        if len(indices) == 9:
            candidates.append(indices)
    if not candidates:
        raise ValueError('No nine-row screenshot for paired latency workloads')
    # Spread throughout the corpus, not only its first easy screenshot.
    chosen = [candidates[int(i)] for i in np.linspace(0, len(candidates) - 1, min(frames, len(candidates)))]
    return [{'id': f'frame{f}_rows{n}', 'indices': indices[:n], 'rows': n,
             'purpose': 'six-row scheduling diagnostic, not selection group' if n == 6 else 'paired NAME workload'}
            for f, indices in enumerate(chosen) for n in (1, 3, 6, 9)]


def observe_all(strategy, corpus):
    output = [None] * len(corpus)
    elapsed = 0.0
    for indices in corpus_groups(corpus):
        start = time.perf_counter()
        values = strategy.read([corpus[i]['image'] for i in indices])
        elapsed += (time.perf_counter() - start) * 1000
        for index, value in zip(indices, values):
            output[index] = asdict(value)
    return output, elapsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT / 'dataset')
    parser.add_argument('--models', type=Path, default=ROOT / 'benchmarks/models')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variants', nargs='+', choices=[s.name for s in specifications()])
    parser.add_argument('--blocks', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=8)
    parser.add_argument('--frames', type=int, default=3)
    parser.add_argument('--seed', type=int, default=20261016)
    parser.add_argument('--expected-rows', type=int, default=170)
    parser.add_argument('--background', default='Offline; game/FPS not measured. Operator must describe actual background.')
    args = parser.parse_args(argv)
    if min(args.blocks, args.repeats, args.frames, args.expected_rows) < 1:
        parser.error('Positive blocks/repeats/frames/expected-rows required')
    output = args.output.resolve()
    dataset = args.dataset.resolve()
    if output == dataset or dataset in output.parents:
        parser.error('Output must be outside dataset')
    output.mkdir(parents=True, exist_ok=False)
    prepare_native_runtime()
    cv2.setNumThreads(2)  # Global process setting fixed for all variants.
    import onnxruntime as ort
    corpus, labels_hash = load_corpus(dataset, args.expected_rows)
    workloads = latency_workloads(corpus, args.frames)
    specs = [s for s in specifications() if not args.variants or s.name in args.variants]
    packages = {}
    for package in ('numpy', 'opencv-python', 'Pillow', 'rapidocr', 'onnxruntime',
                    'onnxruntime-directml', 'tesserocr', 'psutil'):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    manifest = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'arguments': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        'python': sys.version, 'executable': sys.executable, 'platform': platform.platform(),
        'processor': platform.processor(), 'logical_cpus': os.cpu_count(), 'packages': packages,
        'available_providers': ort.get_available_providers(), 'labels_sha256': labels_hash,
        'corpus': [{k: v for k, v in c.items() if k != 'image'} for c in corpus],
        'workloads': workloads, 'specifications': [asdict(s) for s in specs],
        'source_sha256': {str(p.relative_to(ROOT)): sha256(p) for p in
            [Path(__file__), ROOT / 'stalbot/vision.py', ROOT / 'stalbot/domain.py']},
        'models_sha256': {name: sha256(args.models / name) for name in
            ('rapid/cyrillic_PP-OCRv5_rec_mobile.onnx', 'tessdata/rus.traineddata', 'tessdata/eng.traineddata')},
        'environment': {k: os.environ.get(k) for k in ('OMP_THREAD_LIMIT', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS')},
        'measurement': {'unit': 'wall milliseconds for full ordered NAME workload, including scheduling and preprocessing',
            'cv2_threads': 2, 'candidate_reader': 'NAME-only Reader subclass; inherited Reader.names unchanged',
            'baseline_reader': 'unmodified production Reader including unused Tesseract API',
            'correctness': 'all Identity fields against actual CPU batch1; not independent accuracy labels',
            'round_order': 'seeded shuffled variants; odd blocks reverse prior order',
            'latency_order': 'same seeded shuffled workloads for every variant within block/repeat',
            'warmup': 'separate startup and real-width warmup per owner; not included in steady-state',
            'limitations': ['Offline NAME only; no decision/BUY/full-cycle timings',
                'No game FPS/frametime measured', 'RSS process-wide; per-session VRAM not claimed',
                'Host spans do not prove GPU kernel overlap',
                'Correctness full corpus once per variant, timed outputs also checked on every repeat']},
        'resource_before': process_rss(), 'gpu_before': gpu_snapshot()}
    write_json(output / 'manifest.json', manifest)
    warm_images = [c['image'] for c in corpus]
    # Native runtimes initialize on owner threads after main-thread tesserocr import.
    baseline = Strategy(specifications()[0], args.models, warm_images)
    try:
        reference, reference_ms = observe_all(baseline, corpus)
        write_json(output / 'reference.json', {'observations': reference, 'total_ms': reference_ms,
            'init_ms': baseline.init_ms, 'warmup_ms': baseline.warmup_ms})
    finally:
        baseline.close()
    rng = random.Random(args.seed)
    order = specs[:]
    rng.shuffle(order)
    correctness = {}
    runs = []
    all_rows = []
    failures = []
    with (output / 'measurements.jsonl').open('x', encoding='utf-8') as raw:
        for block in range(args.blocks):
            if block % 2:
                order = list(reversed(order))
            elif block:
                rng.shuffle(order)
            for position, spec in enumerate(order):
                run_dir = output / f'block{block}_{spec.name}'
                run_dir.mkdir()
                strategy = None
                record = {'block': block, 'position': position, 'variant': spec.name,
                          'rss_before': process_rss()}
                print(f'block={block} variant={spec.name} starting', flush=True)
                try:
                    strategy = Strategy(spec, args.models, warm_images)
                    record.update(init_ms=strategy.init_ms, warmup_ms=strategy.warmup_ms,
                                  rss_loaded=process_rss(), gpu_loaded=gpu_snapshot())
                    if spec.name not in correctness:
                        observations, total_ms = observe_all(strategy, corpus)
                        differences = [{'index': i, 'expected': a, 'actual': b}
                                       for i, (a, b) in enumerate(zip(reference, observations)) if a != b]
                        result = {'rows': len(corpus), 'total_ms': total_ms,
                                  'differences': differences, 'observations': observations}
                        correctness[spec.name] = {'differences': len(differences), 'rows': len(corpus)}
                        write_json(run_dir / 'correctness.json', result)
                    for repeat in range(args.repeats):
                        ordered = workloads[:]
                        random.Random(args.seed + block * 100003 + repeat).shuffle(ordered)
                        for work in ordered:
                            inputs = [corpus[i]['image'] for i in work['indices']]
                            start = time.perf_counter()
                            actual = strategy.read(inputs)
                            ms = (time.perf_counter() - start) * 1000
                            same = [asdict(x) for x in actual] == [reference[i] for i in work['indices']]
                            row = {'variant': spec.name, 'block': block, 'repeat': repeat,
                                   'workload': work['id'], 'rows': work['rows'], 'elapsed_ms': ms,
                                   'identity_equal': same, 'resources': process_rss()}
                            raw.write(json.dumps(row, ensure_ascii=False) + '\n')
                            raw.flush()
                            all_rows.append(row)
                    # Profiling disabled during every timed sample.
                    proof = strategy.proof(run_dir, [corpus[i]['image'] for i in workloads[-1]['indices']])
                    write_json(run_dir / 'gpu_proof.json', proof)
                    if any(not p['dml_execution_proven'] for p in proof['sessions']):
                        raise RuntimeError('DML provider requested but no DML node execution proved')
                    record['rss_end_loaded'] = process_rss()
                    record['gpu_end_loaded'] = gpu_snapshot()
                    record['status'] = 'complete'
                except Exception as exc:
                    import traceback
                    record.update(status='failed', error=repr(exc), traceback=traceback.format_exc())
                    failures.append({'variant': spec.name, 'block': block, 'error': repr(exc)})
                finally:
                    if strategy is not None:
                        strategy.close()
                    record['rss_after_close'] = process_rss()
                    write_json(run_dir / 'run.json', record)
                    runs.append(record)
                print(f'block={block} variant={spec.name} {record["status"]}', flush=True)
    summaries = []
    for spec in specs:
        for n in (1, 3, 6, 9):
            values = [r['elapsed_ms'] for r in all_rows if r['variant'] == spec.name and r['rows'] == n]
            if values:
                summaries.append({'variant': spec.name, 'rows': n, 'n': len(values),
                    'p50_ms': float(np.percentile(values, 50)), 'p95_ms': float(np.percentile(values, 95)),
                    'min_ms': min(values), 'max_ms': max(values),
                    'timed_identity_differences': sum(not r['identity_equal'] for r in all_rows
                        if r['variant'] == spec.name and r['rows'] == n)})
    ok = not failures and len(correctness) == len(specs) and all(
        r['differences'] == 0 for r in correctness.values()) and all(r['identity_equal'] for r in all_rows)
    result = {'pass': ok, 'correctness': correctness, 'summaries': summaries,
              'failures': failures, 'runs': runs, 'resource_after': process_rss(),
              'gpu_after': gpu_snapshot(), 'labels_unchanged': sha256(dataset / 'labels/labels.json') == labels_hash}
    result['pass'] = result['pass'] and result['labels_unchanged']
    write_json(output / 'summary.json', result)
    print(json.dumps({'pass': result['pass'], 'failures': failures, 'output': str(output)}), flush=True)
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
