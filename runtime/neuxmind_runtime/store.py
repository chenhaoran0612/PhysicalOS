"""Small transactional JSON object store on SQLite (same style as brainos/store.py).

Objects are grouped by ``kind`` and keyed by ``id``. Every operation holds
the store lock only for one statement, so it is safe to call from the HTTP
threads and from skill threads. The stop path never touches the store
synchronously.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        db_path = self.root / "runtime.sqlite3"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        try:
            db_path.chmod(0o600)
        except OSError:
            pass
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS objects ("
            " kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,"
            " PRIMARY KEY (kind, id))"
        )
        self._db.commit()

    def get(self, kind: str, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM objects WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM objects WHERE kind=? ORDER BY rowid", (kind,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def put(self, kind: str, obj: dict) -> dict:
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO objects (kind, id, payload) VALUES (?, ?, ?)",
                (kind, obj["id"], data),
            )
            self._db.commit()
        return obj

    def insert_new(self, kind: str, obj: dict) -> bool:
        """Insert only if absent. Returns False when the id already exists."""
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO objects (kind, id, payload) VALUES (?, ?, ?)",
                (kind, obj["id"], data),
            )
            self._db.commit()
            return cur.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()
