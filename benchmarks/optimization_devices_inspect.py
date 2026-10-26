"""Inspect effective native NAME settings without running inference.

Run only after the timing benchmark. Model sessions are initialized and closed
sequentially on this main owner thread; this is configuration evidence, not GPU
execution evidence. Profiles from optimization_devices provide the latter.
"""
import argparse
from datetime import datetime, timezone
import gc
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.optimization_devices import NameReader, process_rss, sha256, write_json
from stalbot.vision import Reader, prepare_native_runtime


def inspect_reader(reader, label, requested_threads):
    reader.check_thread()
    reader.model._load_rec_model()
    rec = reader.model.text_rec
    session = rec.session.session
    options = session.get_session_options()
    model_path = Path(session._model_path).resolve()
    character_json = json.dumps(rec.postprocess_op.character, ensure_ascii=False).encode('utf-8')
    return {'label': label, 'owner_thread': threading.get_ident(),
        'owner_matches': reader.owner == threading.get_ident(),
        'numeric_api_present': getattr(reader, 'api', None) is not None,
        'requested_threads': requested_threads,
        'providers': session.get_providers(), 'provider_options': session.get_provider_options(),
        'model_path': str(model_path), 'model_sha256': sha256(model_path),
        'rec_image_shape': list(rec.rec_image_shape),
        'character_count': len(rec.postprocess_op.character),
        'character_sha256': hashlib.sha256(character_json).hexdigest(),
        'intra_op_num_threads': options.intra_op_num_threads,
        'inter_op_num_threads': options.inter_op_num_threads,
        'execution_mode': str(options.execution_mode),
        'enable_mem_pattern': options.enable_mem_pattern,
        'enable_cpu_mem_arena': options.enable_cpu_mem_arena,
        'graph_optimization_level': str(options.graph_optimization_level),
        'rss_loaded': process_rss()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', type=Path, default=ROOT / 'benchmarks/models')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error('Output exists; use a new evidence file')
    if (ROOT / 'dataset').resolve() in output.parents:
        parser.error('Output must be outside dataset')
    output.parent.mkdir(parents=True, exist_ok=True)
    prepare_native_runtime()
    records = []
    for label, device, threads in [('production_cpu_t2', 'production', 2),
                                   ('candidate_cpu_t1', 'cpu', 1),
                                   ('candidate_cpu_t2', 'cpu', 2),
                                   ('candidate_dml_t2', 'dml', 2)]:
        reader = None
        started = time.perf_counter()
        try:
            reader = Reader(args.models) if device == 'production' else NameReader(args.models, device, threads)
            record = inspect_reader(reader, label, threads)
            record['initialize_and_inspect_ms'] = (time.perf_counter() - started) * 1000
            records.append(record)
        except Exception as exc:
            records.append({'label': label, 'error': repr(exc)})
        finally:
            if reader is not None:
                reader.close()
            reader = None
            gc.collect()
    valid = [r for r in records if 'error' not in r]
    checks = {'all_configurations_loaded': len(valid) == 4,
        'exact_production_names_method': NameReader.names is Reader.names,
        'same_loaded_model_bytes': len({r['model_sha256'] for r in valid}) == 1,
        'same_decoding_dictionary': len({r['character_sha256'] for r in valid}) == 1,
        'same_recognition_shape': len({tuple(r['rec_image_shape']) for r in valid}) == 1,
        'effective_intra_threads': all(r['intra_op_num_threads'] == r['requested_threads'] for r in valid),
        'effective_inter_threads': all(r['inter_op_num_threads'] == 1 for r in valid),
        'owner_threads': all(r['owner_matches'] for r in valid),
        'cpu_provider_exclusive': all(r['providers'] == ['CPUExecutionProvider']
                                      for r in valid if 'cpu' in r['label']),
        'dml_safe_options': all('DmlExecutionProvider' in r['providers']
            and r['enable_mem_pattern'] is False and r['execution_mode'] == 'ExecutionMode.ORT_SEQUENTIAL'
            for r in valid if 'dml' in r['label'])}
    result = {'created_utc': datetime.now(timezone.utc).isoformat(),
        'pass': all(checks.values()), 'checks': checks, 'sessions': records,
        'code_sha256': {str(p.relative_to(ROOT)): sha256(p) for p in
                        (Path(__file__), ROOT / 'benchmarks/optimization_devices.py', ROOT / 'stalbot/vision.py')},
        'limitations': ['No inference or GPU node execution is performed by this script.',
            'Model init and settings inspection are not steady-state timing measurements.',
            'Production Reader numeric API consumes additional startup time and resident memory.',
            'In the timing benchmark production lazy model loading is in warmup; candidates load it in init. Compare init+warmup, not init alone.']}
    write_json(output, result)
    print(json.dumps({'pass': result['pass'], 'checks': checks, 'output': str(output)}), flush=True)
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
