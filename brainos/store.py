"""Transactional local registry. Payloads are JSON; weight and dataset digests are immutable."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'registry.sqlite3', check_same_thread=False)
        (self.root / 'registry.sqlite3').chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,id))')
        self.db.commit()
        # Issue #32: kind -> fields of a saved record that only their owner changes (``put(..., owner=True)``).
        # Everybody else's ``put`` keeps the saved value: code that read a record, waited for a device and
        # saves its copy must not undo what the owner changed meanwhile (a robot's body type).
        self.owned = {}

    def list(self, kind):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM objects WHERE kind=? ORDER BY rowid DESC', (kind,))]

    def kinds(self):
        """Every kind that has a record (issue #44: a deleted task takes its records of any kind)."""
        with self.lock:
            return [row[0] for row in self.db.execute('SELECT DISTINCT kind FROM objects ORDER BY kind')]

    def get(self, kind, key):
        with self.lock:
            row = self.db.execute('SELECT payload FROM objects WHERE kind=? AND id=?', (kind, key)).fetchone()
            if not row:
                raise ValueError(f'找不到 {kind}: {key}')
            return json.loads(row[0])

    def put(self, kind, obj, owner=False):
        with self.lock:
            fields = () if owner else self.owned.get(kind, ())
            if fields:
                row = self.db.execute('SELECT payload FROM objects WHERE kind=? AND id=?', (kind, obj['id'])).fetchone()
                saved = json.loads(row[0]) if row else {}
                obj.update({field: saved[field] for field in fields if field in saved})    # the caller's copy says what was saved
            self.db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)', (kind, obj['id'], json.dumps(obj, ensure_ascii=False, allow_nan=False)))
            self.db.commit()
        return obj

    def delete(self, kind, key):
        with self.lock:
            self.db.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, key))
            self.db.commit()

    @contextmanager
    def atomic(self):
        # Serialize read-modify-write operations across HTTP and worker threads.
        with self.lock:
            yield

    def close(self):
        self.db.close()
