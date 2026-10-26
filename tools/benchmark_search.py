"""Bounded, read-only Search benchmark. No OCR, input, purchases or dataset writes.

Run with .venv-benchmark/Scripts/python.exe tools/benchmark_search.py --capture.
Capture is optional: it measures the current desktop, not game acceptance.
All outputs go into a new directory; source screenshots are never copied/changed.
"""
import argparse
import hashlib
import json
import platform
import random
import sys
import time
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageGrab, __version__ as pillow_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stalbot.vision import BOXES, STEP, crop
from stalbot.interface import Interface

FIELDS = ('name', 'quantity', 'buyout')
REGION4 = (889, 394, 1384, 541)
REGION9 = (889, 394, 1384, 726)


def stats(values):
    return {'n': len(values), 'p50_ms': float(np.percentile(values, 50)),
            'p95_ms': float(np.percentile(values, 95)), 'max_ms': max(values)}


def roi_views(frame, rows, fields=FIELDS, origin=(0, 0)):
    ox, oy = origin
    return tuple(frame[y1 + STEP*r - oy:y2 + STEP*r - oy, x1-ox:x2-ox]
                 for r in range(rows) for field in fields
                 for x1, y1, x2, y2 in (BOXES[field],))


def packed(frame, rows, fields=FIELDS):
    # The historical lot_pixels baseline deliberately retains crop().copy().
    return np.concatenate([crop(frame, f, r).ravel() for r in range(rows) for f in fields])


def equal(left, right):
    if isinstance(left, tuple):
        return all(np.array_equal(a, b) for a, b in zip(left, right))
    return np.array_equal(left, right)


@lru_cache(maxsize=1)
def variants():
    result = {
        'A_copy9': (9, FIELDS, lambda f: packed(f, 9), 28),
        'B_copy4_no_qty': (4, ('name', 'buyout'), lambda f: packed(f, 4, ('name', 'buyout')), 9),
        'C_copy4': (4, FIELDS, lambda f: packed(f, 4), 13),
        'D_views4': (4, FIELDS, lambda f: roi_views(f, 4), 0),
        'E_views9': (9, FIELDS, lambda f: roi_views(f, 9), 0),
        'F_compact9': (9, FIELDS, lambda f: np.concatenate(roi_views(f, 9), axis=None), 1),
        'G_compact4': (4, FIELDS, lambda f: np.concatenate(roi_views(f, 4), axis=None), 1),
    }
    try:
        from stalbot.search import FieldMonitor
        for rows, label in ((9, 'H_runtime9'), (4, 'I_runtime4')):
            monitor = FieldMonitor(rows)
            # This records the runtime implementation as it exists at execution time.
            prepare = getattr(monitor, 'pixels', monitor.views)
            probe = prepare(np.zeros((1080, 1920, 3), dtype=np.uint8))
            result[label] = (rows, FIELDS, prepare, int(isinstance(probe, np.ndarray)))
    except ImportError:
        pass
    return result


def spread(items, maximum):
    if len(items) <= maximum:
        return items
    return [items[i] for i in np.linspace(0, len(items)-1, maximum, dtype=int)]


def load_corpus(corpus_report=None):
    paths = sorted((ROOT/'dataset/screenshots/processed').glob('*.png'))
    diagnostic_dirs = sorted((ROOT/'data/diagnostics').glob('search-*'))
    diagnostic_dirs = [p for p in diagnostic_dirs if (p/'before_search.png').is_file()
                       and (p/'screen.png').is_file() and (p/'detail.json').is_file()]
    expected = {}
    if corpus_report:
        old_manifest = json.loads(corpus_report.read_text(encoding='utf-8'))['manifest']
        expected = {m['path']: m['sha256'] for m in old_manifest}
        paths = [ROOT/m['path'] for m in old_manifest if 'dataset' in Path(m['path']).parts]
        diagnostic_dirs = [(ROOT/m['path']).parent for m in old_manifest if Path(m['path']).name == 'before_search.png']
    manifest, frames, pairs, traces = [], [], [], []
    def load(path):
        raw = path.read_bytes()
        if expected and hashlib.sha256(raw).hexdigest() != expected[str(path.relative_to(ROOT))]:
            raise ValueError(f'Corpus changed since manifest: {path}')
        result = np.array(Image.open(path).convert('RGB'))
        if result.shape[1] != 1920 or result.shape[0] not in (1080, 1081):
            raise ValueError(f'Unsupported screenshot dimensions: {path}: {result.shape}')
        manifest.append({'path': str(path.relative_to(ROOT)), 'sha256': hashlib.sha256(raw).hexdigest(),
                         'shape': list(result.shape)})
        frames.append(result)
        return result
    for path in spread(paths, 4):
        frame = load(path)
        pairs.append((str(path.relative_to(ROOT)), frame, frame))
    for directory in spread(diagnostic_dirs, 6):
        before, after = load(directory/'before_search.png'), load(directory/'screen.png')
        pairs.append((str(directory.relative_to(ROOT)), before, after))
        detail = json.loads((directory/'detail.json').read_text(encoding='utf-8'))
        traces.append({'path': str(directory.relative_to(ROOT)), 'result': detail.get('result'),
                       'samples': len(detail.get('samples', [])), 'timings': detail.get('timings'),
                       'changed_samples': sum(bool(s.get('changed')) for s in detail.get('samples', []))})
    if not frames:
        raise RuntimeError('No bounded screenshot corpus found')
    return manifest, pairs, frames, traces


def compare_benchmark(pairs, iterations):
    output = {}
    for name, (rows, fields, prepare, copies) in variants().items():
        times, prepare_times, compare_times = [], [], []
        references = [(prepare(before), prepare(after), after) for _, before, after in pairs]
        for _ in range(10):
            for baseline, previous, frame in references:
                current = prepare(frame)
                equal(baseline, current); equal(previous, current)
        cpu_started = time.process_time()
        for _ in range(iterations):
            for baseline, previous, frame in references:
                start = time.perf_counter_ns()
                current = prepare(frame)
                prepared = time.perf_counter_ns()
                equal(baseline, current); equal(previous, current)
                done = time.perf_counter_ns()
                times.append((done-start)/1e6)
                prepare_times.append((prepared-start)/1e6)
                compare_times.append((done-prepared)/1e6)
        pixels = sum((BOXES[f][2]-BOXES[f][0])*(BOXES[f][3]-BOXES[f][1]) for f in fields)*rows
        output[name] = {'total': stats(times), 'prepare': stats(prepare_times),
                        'two_comparisons': stats(compare_times), 'cpu_ms': (time.process_time()-cpu_started)*1000,
                        'selected_pixels': pixels, 'rgb_bytes': pixels*3,
                        'pixel_copy_arrays_per_sample': copies,
                        'pixel_bytes_copied_per_sample': pixels*(3 if copies == 1 else 6) if copies else 0,
                        'comparison_bool_arrays_max': rows*len(fields)*2 if not copies else 2,
                        'detections': {label: not equal(prepare(before), prepare(after)) for label, before, after in pairs}}
    return output


def synthetic_checks(frame):
    cases = {}
    changes = [('row2_name', 1, 'name'), ('row3_buyout', 2, 'buyout'), ('row4_name', 3, 'name'),
               ('row4_qty_only', 3, 'quantity'), ('row5_only', 4, 'name'), ('row9_only', 8, 'buyout')]
    for label, row, field in changes:
        changed = frame.copy()
        x1, y1, _, _ = BOXES[field]
        changed[y1+STEP*row+3:y1+STEP*row+5, x1+3:x1+5] ^= np.uint8(255)
        cases[label] = changed
    timer = frame.copy()
    # A deliberately excluded timer-column rectangle, not a real timer animation replay.
    timer[399:405, 1170:1190] ^= np.uint8(255)
    cases['timer_column_only'] = timer
    cases['identical'] = frame
    result = {label: {name: not equal(prepare(frame), prepare(changed))
                      for name, (_, _, prepare, _) in variants().items()} for label, changed in cases.items()}
    for label in ('row2_name', 'row3_buyout', 'row4_name'):
        assert all(result[label].values())
    assert result['row4_qty_only']['A_copy9'] and not result['row4_qty_only']['B_copy4_no_qty']
    assert all(not detected for detected in result['timer_column_only'].values())
    assert result['row9_only']['E_views9'] and not result['row9_only']['D_views4']
    # Regional coordinate mapping is exact on saved images, independent of live capture timing.
    for box, rows in ((REGION4, 4), (REGION9, 9)):
        x1, y1, x2, y2 = box
        assert equal(roi_views(frame, rows), roi_views(frame[y1:y2, x1:x2], rows, origin=(x1, y1)))
    return result


def component_benchmark(frames, iterations):
    ui = Interface(ROOT/'assets/ui')
    sample = frames[:4]
    output = {}
    for name, call in [('auction', ui.auction), ('any_menu', ui.any_menu), ('outcome', ui.outcome),
                       ('baseline_sha256_tobytes', lambda f: hashlib.sha256(packed(f, 9).tobytes()).hexdigest()),
                       ('baseline_check_bundle', lambda f: (packed(f, 9), ui.auction(f), ui.any_menu(f), ui.outcome(f)))]:
        values = []
        for frame in sample:
            call(frame)
        for _ in range(iterations):
            for frame in sample:
                start = time.perf_counter_ns(); call(frame)
                values.append((time.perf_counter_ns()-start)/1e6)
        output[name] = stats(values)
    return output


def capture_benchmark(count):
    # No Desktop adapter: it enforces game focus. This explicit opt-in reads desktop only.
    if sys.platform != 'win32':
        return {'available': False, 'reason': 'Windows-only desktop benchmark'}
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    methods = {'full': None, 'region4': REGION4, 'region9': REGION9}
    values = {name: [] for name in methods}
    cpu = {name: 0.0 for name in methods}
    shapes = {}
    try:
        for bbox in methods.values():
            ImageGrab.grab(bbox=bbox, all_screens=False)
        for i in range(count):
            names = list(methods)
            random.Random(i).shuffle(names)
            for name in names:
                cpu_start, start = time.process_time(), time.perf_counter_ns()
                frame = np.array(ImageGrab.grab(bbox=methods[name], all_screens=False).convert('RGB'))
                values[name].append((time.perf_counter_ns()-start)/1e6)
                cpu[name] += (time.process_time()-cpu_start)*1000
                shapes[name] = list(frame.shape)
        return {'available': True, 'context': 'Current Windows desktop; game activity/focus not established; no images saved',
                'methods': {name: {**stats(times), 'cpu_ms': cpu[name], 'shape': shapes[name]}
                            for name, times in values.items()}}
    except (OSError, RuntimeError) as exc:
        return {'available': False, 'reason': str(exc)}


def fake_clock_models(capture_ms, comparison_ms):
    """Discrete deterministic clock; no sleeps/input. Game update timing is assumed."""
    result = []
    cost = max(0.01, capture_ms + comparison_ms)
    for polling in (0, 5, 30):
        runs = []
        for update_at in np.linspace(40, 240, 101):
            now, first, captures, sleeps, stable_since = 0., None, 0, 0., None
            while now < 800:
                now += cost; captures += 1
                if now >= update_at:
                    if first is None:
                        first = now; stable_since = now
                    elif now-stable_since >= 30:
                        break
                now += polling; sleeps += polling
            runs.append((now, first-update_at, captures, sleeps))
        result.append({'poll_ms': polling, 'ready': stats([r[0] for r in runs]),
                       'detection_lag': stats([r[1] for r in runs]),
                       'mean_captures': float(np.mean([r[2] for r in runs])),
                       'mean_sleep_ms': float(np.mean([r[3] for r in runs]))})
    rng = random.Random(7301)
    mouse = {'40+40_jitter10': stats([rng.uniform(30, 50)+rng.uniform(30, 50) for _ in range(10000)]),
             '20+20_jitter0': stats([40.]*10000), '10+10_jitter0': stats([20.]*10000)}
    return {'assumptions': {'capture_ms': capture_ms, 'comparison_ms': comparison_ms,
            'update_ms': '101 equally spaced hypothetical updates 40..240ms', 'stable_elapsed_ms': 30,
            'excludes': 'real scheduling, game GPU/CPU contention, OCR and game click acceptance'},
            'polling': result, 'mouse_commanded_delay_model': mouse}


def reported_game_trace():
    path = ROOT/'data/diagnostics/search-a68f617b4da1412a9b7377a2fe6b7e2b/detail.json'
    if not path.exists():
        return None
    raw = path.read_bytes()
    trace = json.loads(raw)
    metrics = trace['timings']
    return {'path': str(path.relative_to(ROOT)), 'sha256': hashlib.sha256(raw).hexdigest(),
            'timings': metrics, 'samples': trace['samples'],
            'average_check_ms': metrics['checks_ms']/metrics['captures'],
            'average_capture_ms': metrics['captures_ms']/metrics['captures']}


def markdown(report):
    lines = ['# Search performance: bounded offline benchmark', '',
             'No clicks, purchases, OCR or dataset writes. Numbers below are local measurements unless explicitly labelled models.', '',
             'Run context: '+report['context_note'], '',
             '## Monitoring variants', '',
             '| Variant | p50 / p95 ms per sample + two comparisons | Pixels | Pixel-copy arrays |',
             '|---|---:|---:|---:|']
    for name, data in report['monitoring'].items():
        t = data['total']
        lines.append(f"| {name} | {t['p50_ms']:.4f} / {t['p95_ms']:.4f} | {data['selected_pixels']} | {data['pixel_copy_arrays_per_sample']} |")
    lines += ['', 'A reproduces historical crop().copy()+concatenate on nine rows. B omits QTY. C keeps QTY on four rows. D retains exact field views on four rows; E retains exact views on nine rows. F/G compact the nine/four row field views using concatenate(axis=None), producing one RGB buffer without intermediate crop copies. H/I use the current production FieldMonitor.pixels (or views if pixels is unavailable), including validation overhead, with the same exact comparison. Views retain the immutable source frame; RGB pixels are not copied. np.array_equal still allocates temporary boolean arrays. Copy counts describe pixel-bearing preparation arrays, excluding views/Python objects/comparison temporaries. Each run processes the same saved pairs. There is no reduced-resolution or glyph mask approximation.', '',
              '## Coverage', '', '| Synthetic change | ' + ' | '.join(report['monitoring']) + ' |',
              '|---|' + '---|'*len(report['monitoring'])]
    for label, results in report['synthetic'].items():
        lines.append('| '+label+' | '+' | '.join('detected' if v else 'ignored' for v in results.values())+' |')
    real = [p for p in report['manifest'] if 'before_search' in p['path']]
    lines += ['', f"Corpus: {len(report['manifest'])} saved images, including {len(real)} before/after diagnostic pairs, plus four dataset screenshots. Exact paths and SHA256 in report.json. Selection is evenly spaced lexicographic paths, bounded before image loading (or a verified prior report manifest). Diagnostics are a convenience sample with both success and timeout cases; they do not contain intermediate screenshots or labels proving server response. False-positive/false-negative rates and actual detection latency therefore cannot be inferred. Synthetic timer test covers only its explicitly excluded rectangle; it is not proof all game animations are excluded.", '',
              '## Component costs (separate calls)', '', '| Component | p50 ms | p95 ms |', '|---|---:|---:|']
    for name, t in report['components'].items():
        lines.append(f"| {name} | {t['p50_ms']:.3f} | {t['p95_ms']:.3f} |")
    lines += ['', '| Historical diagnostic | Frames | Mean complete check ms | Field change in saved pair |', '|---|---:|---:|---|']
    for trace in report['historical_traces']:
        metrics = trace.get('timings') or {}
        average = f"{metrics['checks_ms']/metrics['captures']:.3f}" if metrics.get('captures') and 'checks_ms' in metrics else 'not recorded'
        detected = report['monitoring']['A_copy9']['detections'][trace['path']]
        lines.append(f"| {Path(trace['path']).name} | {trace['samples']} | {average} | {detected} |")
    lines += ['', 'Historical checks_ms included extraction, comparison, UI template matching and SHA256/tobytes. The bundle measures all UI calls even if a production boolean expression would short-circuit. Historical source logs are not current performance measurements.']
    trace = report['reported_game_trace']
    if trace:
        metrics = trace['timings']
        lines += ['', f"The exact user-reported run was found in `{trace['path']}`: Search {metrics['total_ms']:.3f}ms; {metrics['captures']} captures; comparisons/checks {metrics['checks_ms']:.3f}ms total = {trace['average_check_ms']:.3f}ms per check; capture mean {trace['average_capture_ms']:.3f}ms. This is a complete check cost, not pure array comparison. First changed sample at 234ms after click, another different hash at 328ms, stable at 406ms. It demonstrates a multi-stage field transition, but the saved log does not identify which rows changed. No current optimized in-game before/after claim is possible."]
    lines += ['', '## Read-only capture', '']
    capture = report['capture']
    if capture.get('available'):
        lines += [capture['context'], '', '| Pillow RGB capture | p50 ms | p95 ms |', '|---|---:|---:|']
        for name, t in capture['methods'].items():
            lines.append(f"| {name} | {t['p50_ms']:.3f} | {t['p95_ms']:.3f} |")
    else:
        lines.append('Unavailable/not requested: '+capture.get('reason', ''))
    lines += ['', 'Installed Pillow 12.3.0 ImageGrab.py was inspected: Windows calls grabscreen_win32 without bbox, creates a full RGB image, then crops it. Thus reduced output pixels do not imply reduced native capture work. Samples alternate full/region ordering. Full and regional captures are sequential, so live pixel equality is not asserted. Saved-frame coordinate mapping checks passed for both 4 and 9 rows.', '', '## Fake-clock polling and mouse models', '',
              'The following is a deterministic scheduling model, not a Stalzone before/after measurement. Input capture/comparison costs and assumptions are in JSON. The model requires two samples and at least 30ms of observed stability; it omits initial validation, final full-frame guard, OCR and clicks.', '',
              '| Poll ms | Ready p50 / p95 ms | Detection lag p50 / p95 ms | Mean captures | Mean sleep ms |', '|---:|---:|---:|---:|---:|']
    for r in report['models']['polling']:
        lines.append(f"| {r['poll_ms']} | {r['ready']['p50_ms']:.2f} / {r['ready']['p95_ms']:.2f} | {r['detection_lag']['p50_ms']:.2f} / {r['detection_lag']['p95_ms']:.2f} | {r['mean_captures']:.2f} | {r['mean_sleep_ms']:.2f} |")
    lines += ['', '| Mouse delay model | p50 ms | p95 ms |', '|---|---:|---:|']
    for name, t in report['models']['mouse_commanded_delay_model'].items():
        lines.append(f"| {name} | {t['p50_ms']:.2f} | {t['p95_ms']:.2f} |")
    lines += ['', 'Mouse values are requested delays, not SendInput or game-accepted click measurements. Faster settings require Search/menu probe validation in the game without BUY.', '', '## Recommendation and limits', '',
              'Keep NAME+QTY+BUYOUT on nine rows as the conservative default. Use measured compact exact packing (F) if faster; zero-copy views alone were slower in the first local run, despite fewer pixel copies. Four-row monitoring is experimental: this corpus cannot prove every update touches the top four rows. B is unsuitable because QTY-only changes are missed. Compare/stabilize all nine rows before OCR, even if four rows trigger the wakeup. Two equal samples do not prove a server transaction is complete; an elapsed stability guard limits immediate same-render-frame acceptance but cannot exclude an arbitrarily delayed lower-row update. Keep existing pre-purchase protections.', '',
              'Choose 0ms polling for maximum responsiveness only after checking game/OCR p95 under actual load; 5ms offers a small scheduling gap. CPU process time is measured for local benchmark work, not game impact. No claim is made about actual 561ms Search or 713ms decision latency after changes: a new game run is required. This benchmark does not replace runtime fake-desktop regression tests.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--iterations', type=int, default=100)
    parser.add_argument('--capture', action='store_true')
    parser.add_argument('--capture-count', type=int, default=30)
    parser.add_argument('--corpus-report', type=Path, help='Replay exact paths/hashes from an earlier report.json')
    parser.add_argument('--context-note', default='No controlled game workload; other processes may affect timings')
    args = parser.parse_args()
    if args.iterations < 1 or args.capture_count < 1:
        parser.error('iteration counts must be positive')
    out = args.output or ROOT/'validation'/('search-performance-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    out = out.resolve()
    if out == ROOT/'dataset' or ROOT/'dataset' in out.parents:
        parser.error('output inside dataset is forbidden')
    out.mkdir(parents=True, exist_ok=False)
    manifest, pairs, frames, traces = load_corpus(args.corpus_report)
    report = {'utc': datetime.now(timezone.utc).isoformat(), 'python': sys.version, 'platform': platform.platform(),
              'numpy': np.__version__, 'pillow': pillow_version,
              'context_note': args.context_note,
              'runtime_search_sha256': hashlib.sha256((ROOT/'stalbot/search.py').read_bytes()).hexdigest() if (ROOT/'stalbot/search.py').exists() else None,
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'iterations': args.iterations,
              'manifest': manifest, 'historical_traces': traces, 'monitoring': compare_benchmark(pairs, args.iterations),
              'reported_game_trace': reported_game_trace(),
              'synthetic': synthetic_checks(frames[0]), 'components': component_benchmark(frames, min(args.iterations, 20)),
              'capture': capture_benchmark(args.capture_count) if args.capture else {'available': False, 'reason': '--capture omitted'}}
    capture = report['capture']
    capture_cost = capture['methods']['region9']['p50_ms'] if capture.get('available') else 20.0
    report['models'] = fake_clock_models(capture_cost, report['monitoring']['F_compact9']['total']['p50_ms'])
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (out/'REPORT.md').write_text(markdown(report), encoding='utf-8')
    print(out)
    print('Synthetic coverage and regional-coordinate assertions passed. No real input sent.')


if __name__ == '__main__':
    main()
