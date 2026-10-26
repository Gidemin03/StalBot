"""Fast scheduler/provenance checks. No model loads, desktop, or purchases."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading
import time
import unittest

from benchmarks.optimization_devices import (NameReader, Reader, Spec, Strategy,
    corpus_groups, fixed_assignments, latency_workloads, specifications)


@dataclass(frozen=True)
class Observation:
    value: int


class FakeReader:
    def __init__(self):
        self.owner = threading.get_ident()
        self.active = False
        self.completed = []

    def names(self, images, batch):
        if threading.get_ident() != self.owner:
            raise RuntimeError('Wrong owner')
        if self.active:
            raise RuntimeError('Concurrent session call')
        self.active = True
        try:
            time.sleep(0.001 * (10 - images[0]))
            self.completed.extend(images)
            if -1 in images:
                raise ValueError('Injected worker failure')
            return [Observation(x) for x in images]
        finally:
            self.active = False


class FakeWorker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.reader = self.executor.submit(FakeReader).result()

    def submit(self, function, *args):
        return self.executor.submit(function, *args)

    def close(self):
        self.executor.shutdown(wait=True)


def make_strategy(spec):
    result = Strategy.__new__(Strategy)
    result.spec = spec
    result.workers = [FakeWorker() for _ in spec.devices]
    return result


class SchedulingTests(unittest.TestCase):
    def test_names_is_actual_production_method(self):
        self.assertIs(NameReader.names, Reader.names)

    def test_all_requested_variants_exist(self):
        specs = specifications()
        self.assertEqual(len(specs), 17)
        self.assertEqual(len({s.name for s in specs}), len(specs))
        self.assertEqual({(len(s.devices), s.threads) for s in specs if s.name.startswith('cpu_w')},
                         {(w, t) for w in (1, 2, 3) for t in (1, 2)})

    def test_order_and_session_ownership_for_every_schedule(self):
        for spec in specifications():
            strategy = make_strategy(spec)
            try:
                for n in (1, 3, 6, 9):
                    trace = []
                    actual = strategy.read(list(range(n)), trace)
                    self.assertEqual(actual, [Observation(i) for i in range(n)], spec.name)
                    self.assertEqual(sorted(i for event in trace for i in event['indices']), list(range(n)))
                    self.assertTrue(all(len(event['indices']) <= spec.batch for event in trace))
            finally:
                strategy.close()

    def test_six_rows_are_exactly_cpu3_gpu3(self):
        jobs, owners = fixed_assignments(6, 2, 'chunks3')
        self.assertEqual(jobs, [[0, 1, 2], [3, 4, 5]])
        self.assertEqual(owners, [0, 1])

    def test_exception_drains_all_submitted_work(self):
        strategy = make_strategy(Spec('failure', ('cpu', 'dml'), schedule='alternate'))
        try:
            with self.assertRaisesRegex(ValueError, 'Injected worker failure'):
                strategy.read([-1, 1, 2])
            self.assertEqual(sorted(x for w in strategy.workers for x in w.reader.completed), [-1, 1, 2])
            # Next frame can run after the failed one is completely drained.
            self.assertEqual(strategy.read([3, 4]), [Observation(3), Observation(4)])
        finally:
            strategy.close()

    def test_corpus_groups_keep_tenth_rows_without_crossing_sources(self):
        corpus = [{'source': source, 'row': row} for source in ('a', 'b') for row in range(10)]
        groups = corpus_groups(corpus)
        self.assertEqual([len(g) for g in groups], [9, 1, 9, 1])
        self.assertEqual([i for group in groups for i in group], list(range(20)))
        self.assertTrue(all(len({corpus[i]['source'] for i in group}) == 1 for group in groups))
        work = latency_workloads(corpus, 2)
        self.assertEqual(len(work), 8)
        self.assertTrue(all(len(w['indices']) == w['rows'] for w in work))
        self.assertTrue(all(len({corpus[i]['source'] for i in w['indices']}) == 1 for w in work))


if __name__ == '__main__':
    unittest.main()
