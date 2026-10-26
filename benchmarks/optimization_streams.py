"""Synthetic exact-cache policy traces; no OCR inference or gameplay hit-rate claim."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.optimization_cache import ExactNameLRU, capacities_for, load_corpus
from stalbot.domain import Identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT/'dataset' or ROOT/'dataset' in output.parents:
        parser.error('dataset output forbidden')
    output.mkdir(parents=True, exist_ok=False)
    images, _, metadata, labels_hash = load_corpus(ROOT/'dataset')
    unique = list({(image.shape, image.tobytes()): image for image in images}.values())
    _, capacities = capacities_for(images)
    # All sequences have 30 frames of nine NAME inputs. They are constructed
    # workloads, not temporal reconstructions of the dataset screenshots.
    traces = {
        'replace_all_nine': [[unique[(9*t+r) % len(unique)] for r in range(9)] for t in range(30)],
        'shift_one_new_row': [[unique[(t+r) % len(unique)] for r in range(9)] for t in range(30)],
        'repeat_same_frame': [unique[:9] for _ in range(30)],
    }
    mutations = []
    for t in range(30):
        frame = [im.copy() for im in unique[:9]]
        for row, image in enumerate(frame):
            # One channel differs from the original; keep each stream input
            # distinct by placing a bounded frame number in a single byte.
            image[0, 0, 0] = (int(unique[row][0, 0, 0])+t+1) % 256
        mutations.append(frame)
    traces['one_pixel_changes_every_frame'] = mutations
    rows = []
    summaries = []
    for scenario, frames in traces.items():
        for capacity in capacities:
            cache = ExactNameLRU(capacity)
            for frame_index, frame in enumerate(frames):
                before_hits, before_misses = cache.hits, cache.misses
                started = time.perf_counter_ns()
                for image in frame:
                    key = cache.key(image, 'policy-v1')
                    # Synthetic immutable value is a byte fingerprint, not an
                    # assertion about what OCR would read from mutated pixels.
                    expected = Identity(hashlib.sha256(key.pixels).hexdigest())
                    value = cache.get_key(key)
                    if value is None:
                        cache.put_key(key, expected)
                    else:
                        assert value == expected, 'Cache returned another image observation'
                elapsed = (time.perf_counter_ns()-started)/1e6
                rows.append(dict(scenario=scenario, capacity=capacity, frame=frame_index,
                                 hits=cache.hits-before_hits, misses=cache.misses-before_misses,
                                 policy_including_synthetic_value_ms=elapsed,
                                 entries=cache.stats()['entries']))
            stats = cache.stats()
            assert stats['entries'] <= capacity
            if scenario == 'repeat_same_frame':
                assert stats['misses'] == 9 and stats['hits'] == 261
            if scenario == 'shift_one_new_row':
                assert stats['misses'] == 38 and stats['hits'] == 232
            if scenario == 'one_pixel_changes_every_frame':
                assert stats['misses'] == 270 and stats['hits'] == 0
            summaries.append(dict(scenario=scenario, **stats))
    report = dict(created_utc=datetime.now(timezone.utc).isoformat(), labels_sha256=labels_hash,
                  unique_original_rgb=len(unique), capacities=capacities, frames_per_trace=30,
                  inputs=metadata, source_sha256={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in (Path(__file__), ROOT/'benchmarks/optimization_cache.py')},
                  scope='Synthetic cache policy only, no OCR. Timings include synthetic fingerprint generation; not inference savings.',
                  limitations=['Not recorded gameplay; hit rates cannot estimate live hit rates',
                               'Replace-all sequence wraps within 75 known unique crops',
                               'Mutated-image values are synthetic fingerprints, not OCR labels'],
                  summaries=summaries, frames=rows, passed=True)
    (output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(dict(passed=True, output=str(output), summaries=summaries), indent=2))


if __name__ == '__main__':
    main()
