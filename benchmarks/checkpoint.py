"""Append-only, locked experiment journal with strict resume compatibility."""
import json
import os
from pathlib import Path


class Journal:
    def __init__(self, folder, signature, resume=False):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = (self.folder/'.run.lock').open('a+b')
        if self.lock.seek(0, 2) == 0:
            self.lock.write(b'0')
            self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._open(signature, resume)
        except BaseException:
            self.lock.close()
            raise

    def _open(self, signature, resume):
        meta = self.folder/'checkpoint.json'
        path = self.folder/'predictions.jsonl'
        if meta.exists():
            if not resume:
                raise FileExistsError('Run exists; pass --resume or select another output')
            if json.loads(meta.read_text(encoding='utf-8')) != signature:
                raise ValueError('Resume mismatch: data, code, models or settings changed')
        else:
            if path.exists():
                raise ValueError('Cannot resume a legacy run without checkpoint metadata')
            temp = meta.with_suffix('.tmp')
            temp.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp, meta)
        self.rows = []
        self.keys = set()
        if path.exists():
            content = path.read_bytes()
            valid_end = 0
            for line in content.splitlines(keepends=True):
                if not line.endswith(b'\n'):
                    # A crash can interrupt the last write. Preserve it for audit.
                    with (self.folder/'interrupted_tail.bin').open('ab') as f:
                        f.write(line)
                    with path.open('r+b') as f:
                        f.truncate(valid_end)
                    break
                row = json.loads(line)
                key = self.key(row)
                if key in self.keys:
                    raise ValueError('Duplicate record in journal')
                self.keys.add(key)
                self.rows.append(row)
                valid_end += len(line)
        self.file = path.open('ab')

    @staticmethod
    def key(row):
        return row['engine'], row['variant'], row['id']

    def contains(self, engine, variant, sample_id):
        return (engine, variant, sample_id) in self.keys

    def append(self, row):
        key = self.key(row)
        if key in self.keys:
            raise ValueError('Duplicate prediction')
        self.file.write((json.dumps(row, ensure_ascii=False)+'\n').encode('utf-8'))
        self.file.flush()
        os.fsync(self.file.fileno())
        self.rows.append(row)
        self.keys.add(key)

    def close(self):
        self.file.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
