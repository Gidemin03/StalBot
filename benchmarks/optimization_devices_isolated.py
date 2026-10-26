"""Sequential per-(variant, round) subprocess isolation for NAME benchmarks.

Fresh processes avoid memory retention across configurations and preserve native
crashes as evidence. No inference runs in this supervisor. Each child invokes the
unchanged optimization_devices.py, including its actual CPU reference check.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.optimization_devices import specifications, sha256, write_json

MULTIGPU = {'dml_sessions2', 'dml_sessions3'}


def percentile(values, q):
    values = sorted(values)
    at = (len(values) - 1) * q
    low, high = int(at), min(int(at) + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (at - low)


def main():
    choices = [spec.name for spec in specifications()]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', type=Path, default=ROOT / 'benchmarks/models')
    parser.add_argument('--dataset', type=Path, default=ROOT / 'dataset')
    parser.add_argument('--variants', nargs='+', choices=choices, default=choices)
    parser.add_argument('--skip-variants', nargs='*', choices=choices, default=[])
    parser.add_argument('--prior-crash-log', type=Path, action='append', default=[],
                        help='Existing crash evidence to hash and reference; may repeat')
    parser.add_argument('--blocks', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=8)
    parser.add_argument('--frames', type=int, default=3)
    parser.add_argument('--seed', type=int, default=20261016)
    parser.add_argument('--timeout', type=int, default=600, help='Seconds per child; preserves timeout as failure')
    parser.add_argument('--background', default='Offline sequential benchmark; game status/FPS uncontrolled')
    args = parser.parse_args()
    if min(args.blocks, args.repeats, args.frames, args.timeout) < 1:
        parser.error('Positive blocks/repeats/frames/timeout required')
    output = args.output.resolve()
    dataset = args.dataset.resolve()
    if output == dataset or dataset in output.parents:
        parser.error('Output must be outside dataset')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'logs').mkdir()
    (output / 'runs').mkdir()
    source = ROOT / 'benchmarks/optimization_devices.py'
    original_hash = sha256(source)
    order = list(dict.fromkeys(args.variants))
    random.Random(args.seed).shuffle(order)
    manifest = {'created_utc': datetime.now(timezone.utc).isoformat(),
        'arguments': {k: ([str(x) for x in v] if k == 'prior_crash_log' else str(v) if isinstance(v, Path) else v)
                      for k, v in vars(args).items()},
        'executable': sys.executable, 'python': sys.version,
        'source_sha256': {str(p.relative_to(ROOT)): sha256(p) for p in (Path(__file__), source)},
        'prior_crash_logs': [{'path': str(p.resolve()), 'sha256': sha256(p)} for p in args.prior_crash_log],
        'protocol': 'One native child process per configuration per round; no concurrent benchmarks. '
                    'Multi-GPU-session variants are hazardous exploratory probes only in round 0. '
                    'Fresh actual CPU baseline runs in every child. Per-child model/environment/input hashes preserved.',
        'limitations': ['Fresh per-config process includes independent CPU reference before each candidate.',
            'CPU baseline initialization includes a numeric API; candidate sessions are NAME-only.',
            'Compare init+warmup together: production loads its NAME model lazily in warmup.',
            'Native crashes are failed candidates, never converted to overall PASS.',
            'No live-game FPS, frame time, or GPU kernel overlap proof.']}
    write_json(output / 'manifest.json', manifest)
    records = []
    samples = []
    failed_native = set()
    started = time.perf_counter()

    def checkpoint():
        stable = [name for name in args.variants if name not in MULTIGPU and name not in args.skip_variants]
        expected = {(name, block) for name in stable for block in range(args.blocks)}
        completed = {(r['variant'], r['block']) for r in records if r['status'] == 'passed'}
        summaries = []
        for name in args.variants:
            for n in (1, 3, 6, 9):
                rows = [row for row in samples if row['variant'] == name and row['rows'] == n
                        and row['child_complete']]
                if rows:
                    values = [row['elapsed_ms'] for row in rows]
                    summaries.append({'variant': name, 'rows': n, 'n': len(values),
                        'p50_ms': percentile(values, .5), 'p95_ms': percentile(values, .95),
                        'min_ms': min(values), 'max_ms': max(values),
                        'rounds': sorted({row['outer_block'] for row in rows}),
                        'identity_differences': sum(not row['identity_equal'] for row in rows)})
        conditional = bool(expected) and expected <= completed and all(row['identity_equal'] for row in samples
                          if row['variant'] in stable)
        failures = [r for r in records if r['status'] in ('native_crash', 'failed', 'timeout')]
        write_json(output / 'summary.json', {'stable_configurations_pass': conditional,
            'all_candidates_pass': conditional and not failures and not args.skip_variants
                and not any(r['status'] == 'probe_not_repeated' for r in records),
            'stable_expected_runs': len(expected), 'stable_completed_runs': len(expected & completed),
            'native_crash_variants': sorted(failed_native), 'records': records, 'summaries': summaries,
            'elapsed_seconds': time.perf_counter() - started})

    checkpoint()
    for block in range(args.blocks):
        if block % 2:
            order.reverse()
        elif block:
            random.Random(args.seed + block).shuffle(order)
        for position, variant in enumerate(order):
            if variant in args.skip_variants:
                records.append({'block': block, 'variant': variant, 'status': 'operator_excluded',
                                'reason': 'Explicit --skip-variants; see prior-crash-log evidence if supplied.'})
                checkpoint()
                continue
            if variant in MULTIGPU and block:
                records.append({'block': block, 'variant': variant,
                    'status': 'native_crash_not_repeated' if variant in failed_native else 'probe_not_repeated',
                    'reason': 'Hazardous multi-GPU-session probes run in round 0 only.'})
                checkpoint()
                continue
            if sha256(source) != original_hash:
                raise RuntimeError('Underlying benchmark source changed during isolated matrix')
            tag = f'block{block}_{variant}'
            child_output = output / 'runs' / tag
            stdout = output / 'logs' / f'{tag}.stdout.log'
            stderr = output / 'logs' / f'{tag}.stderr.log'
            command = [sys.executable, '-X', 'faulthandler', str(source), '--output', str(child_output),
                '--models', str(args.models.resolve()), '--dataset', str(dataset),
                '--variants', variant, '--blocks', '1', '--repeats', str(args.repeats),
                '--frames', str(args.frames), '--seed', str(args.seed + block * 100003),
                '--background', args.background]
            record = {'block': block, 'position': position, 'variant': variant, 'status': 'running',
                      'command': command, 'stdout': str(stdout), 'stderr': str(stderr),
                      'output': str(child_output), 'started_utc': datetime.now(timezone.utc).isoformat()}
            records.append(record)
            checkpoint()
            print(f'round={block + 1}/{args.blocks} variant={variant} starting isolated child', flush=True)
            tick = time.perf_counter()
            try:
                with stdout.open('xb') as out, stderr.open('xb') as err:
                    process = subprocess.run(command, stdout=out, stderr=err, cwd=ROOT, timeout=args.timeout, check=False)
                record['returncode'] = process.returncode
                record['returncode_hex32'] = f'0x{process.returncode & 0xffffffff:08x}'
                error_text = stderr.read_text(encoding='utf-8', errors='replace')
                native = (process.returncode & 0xffffffff) in (0xc0000005, 0xc0000409) or any(
                    text in error_text for text in ('Windows fatal exception', 'Fatal Python error'))
                child_summary_path = child_output / 'summary.json'
                child_summary = json.loads(child_summary_path.read_text(encoding='utf-8')) if child_summary_path.is_file() else None
                record['child_pass'] = child_summary.get('pass') if child_summary else None
                record['child_correctness'] = child_summary.get('correctness') if child_summary else None
                record['child_failures'] = child_summary.get('failures') if child_summary else None
                if native:
                    record['status'] = 'native_crash'
                    failed_native.add(variant)
                elif process.returncode == 0 and child_summary and child_summary['pass']:
                    record['status'] = 'passed'
                else:
                    record['status'] = 'failed'
                # Partial samples survive failed children but are never mistaken for a completed run.
                raw = child_output / 'measurements.jsonl'
                if raw.is_file():
                    for line in raw.read_text(encoding='utf-8').splitlines():
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        row['outer_block'] = block
                        row['child_complete'] = record['status'] == 'passed'
                        samples.append(row)
                record['stdout_sha256'] = sha256(stdout)
                record['stderr_sha256'] = sha256(stderr)
            except subprocess.TimeoutExpired:
                record['status'] = 'timeout'
            except Exception as exc:
                record.update(status='failed', error=repr(exc))
            finally:
                record['elapsed_seconds'] = time.perf_counter() - tick
                checkpoint()
                print(f'round={block + 1}/{args.blocks} variant={variant} {record["status"]}', flush=True)
    with (output / 'measurements.jsonl').open('x', encoding='utf-8') as raw:
        for row in samples:
            raw.write(json.dumps(row, ensure_ascii=False) + '\n')
    checkpoint()
    result = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    print(json.dumps({'stable_configurations_pass': result['stable_configurations_pass'],
        'all_candidates_pass': result['all_candidates_pass'], 'native_crash_variants': result['native_crash_variants'],
        'output': str(output)}), flush=True)
    # A known/isolated rejected candidate remains a failure in all_candidates_pass.
    # A supervisor may finish successfully when its explicitly stable subset passed.
    return 0 if result['stable_configurations_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
