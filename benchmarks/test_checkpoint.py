import json
import tempfile
import unittest
from pathlib import Path
from checkpoint import Journal


class CheckpointTests(unittest.TestCase):
    def test_resume_preserves_completed_and_recovers_partial_tail(self):
        with tempfile.TemporaryDirectory() as d:
            row=dict(engine='test',variant='raw',id='a',raw='Призма')
            with Journal(d, {'dataset':'sha'}) as j:
                j.append(row)
            with (Path(d)/'predictions.jsonl').open('ab') as f:
                f.write(b'{"incomplete":')
            with Journal(d, {'dataset':'sha'}, resume=True) as j:
                self.assertEqual(j.rows,[row])
                self.assertTrue(j.contains('test','raw','a'))
                j.append(dict(row,id='b'))
            self.assertEqual(len((Path(d)/'predictions.jsonl').read_text(encoding='utf-8').splitlines()),2)
            self.assertTrue((Path(d)/'interrupted_tail.bin').exists())

    def test_mismatch_or_existing_run_does_not_modify_records(self):
        with tempfile.TemporaryDirectory() as d:
            with Journal(d, {'version':1}) as j:
                j.append(dict(engine='t',variant='v',id='a'))
            before=(Path(d)/'predictions.jsonl').read_bytes()
            with self.assertRaises(ValueError):
                Journal(d, {'version':2}, resume=True)
            with self.assertRaises(FileExistsError):
                Journal(d, {'version':1})
            self.assertEqual(before,(Path(d)/'predictions.jsonl').read_bytes())

    def test_complete_corrupt_line_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            with Journal(d, {}) as j:
                pass
            (Path(d)/'predictions.jsonl').write_bytes(b'corruption\n')
            with self.assertRaises(json.JSONDecodeError):
                Journal(d, {}, resume=True)
