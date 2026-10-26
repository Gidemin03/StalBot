"""Supervisor recovery checks with fake subprocesses; no OCR or native sessions."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmarks import optimization_devices_isolated as supervisor


class IsolationTests(unittest.TestCase):
    def run_fake_matrix(self, output, variants, fake_run):
        arguments = ['optimization_devices_isolated.py', '--output', str(output),
                     '--blocks', '3', '--repeats', '1', '--frames', '1', '--variants', *variants]
        with patch.object(sys, 'argv', arguments), patch.object(supervisor.subprocess, 'run', side_effect=fake_run):
            code = supervisor.main()
        return code, json.loads((output / 'summary.json').read_text(encoding='utf-8'))

    @staticmethod
    def fake_child(command, *, passed=True, native=False, **kwargs):
        output = Path(command[command.index('--output') + 1])
        variant = command[command.index('--variants') + 1]
        output.mkdir(parents=True)
        if native:
            kwargs['stderr'].write(b'Windows fatal exception: access violation\n')
            return subprocess.CompletedProcess(command, 0xc0000005)
        (output / 'summary.json').write_text(json.dumps({'pass': passed,
            'correctness': {variant: {'rows': 170, 'differences': 0}}, 'failures': []}), encoding='utf-8')
        (output / 'measurements.jsonl').write_text(json.dumps({'variant': variant,
            'rows': 9, 'elapsed_ms': 1.5, 'identity_equal': True}) + '\n', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0 if passed else 1)

    def test_native_crash_is_recorded_and_not_repeated_stable_matrix_continues(self):
        calls = []
        def fake(command, **kwargs):
            variant = command[command.index('--variants') + 1]
            calls.append(variant)
            return self.fake_child(command, native=variant == 'dml_sessions2', **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'new-run'
            code, result = self.run_fake_matrix(output, ['cpu_w1_t1', 'dml_sessions2'], fake)
            self.assertEqual(code, 0)
            self.assertTrue(result['stable_configurations_pass'])
            self.assertFalse(result['all_candidates_pass'])
            self.assertEqual(result['native_crash_variants'], ['dml_sessions2'])
            self.assertEqual(calls.count('dml_sessions2'), 1)
            self.assertEqual(calls.count('cpu_w1_t1'), 3)
            crash = next(r for r in result['records'] if r['status'] == 'native_crash')
            self.assertEqual(crash['returncode_hex32'], '0xc0000005')
            self.assertIn('access violation', Path(crash['stderr']).read_text())
            self.assertEqual(result['stable_completed_runs'], 3)
            self.assertEqual(result['summaries'][0]['n'], 3)

    def test_failed_stable_child_cannot_be_hidden_by_partial_samples(self):
        calls = []
        def fake(command, **kwargs):
            calls.append(command)
            return self.fake_child(command, passed=len(calls) != 2, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'new-run'
            code, result = self.run_fake_matrix(output, ['cpu_w1_t1'], fake)
            self.assertEqual(code, 1)
            self.assertFalse(result['stable_configurations_pass'])
            self.assertEqual(result['stable_completed_runs'], 2)
            self.assertEqual(result['summaries'][0]['n'], 2)
            raw = [json.loads(line) for line in (output / 'measurements.jsonl').read_text().splitlines()]
            self.assertEqual(len(raw), 3)
            self.assertEqual(sum(not row['child_complete'] for row in raw), 1)


if __name__ == '__main__':
    unittest.main()
