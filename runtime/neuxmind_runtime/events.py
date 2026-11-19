"""Telemetry event stream (``/v2/telemetry``, ``/v2/runs/{id}/events``).

Emitting must never block the stop path, so :meth:`EventBus.emit` takes no
lock and does no IO: ``itertools.count`` and ``deque.append`` are atomic under
the GIL. Recent events live in an in-memory ring.

Persistence (INTERFACES_V1 §5.2, :meth:`EventBus.persist`) is *optional*:
a long-lived writer thread drains a pending queue into a bounded SQLite ring
(``telemetry.sqlite3``, default 100k events, oldest trimmed) every
``FLUSH_PERIOD_S``.

Telemetry is state and run data: besides the count limit it has an age limit
(``retention_s``, one hour in the runtime -- R1-09 / R7-12). An event older than that is
removed by the writer (``PRUNE_PERIOD_S``) and is not served by any read even before it is
removed; :meth:`EventBus.window` returns what is left by time range. Safety events, task
results and operation records are *not* kept here: ``emit`` hands every event to its taps
(``add_tap``; lock-free consumers such as ``records.DeviceRecords.offer_event``), and those
records have logs of their own that are never trimmed by count or age.

Telemetry never decides whether the robot can start (review Q3-01): any
problem opening the ring -- a corrupt file, a directory of that name, an
out-of-range reservation, a database locked by another process, a second
runtime on the same state directory -- degrades to memory-only telemetry,
reported in ``/v1/status.telemetry`` (``degraded``, ``degraded_reason``) and a
``telemetry.persist_failed`` event. A corrupt file is renamed aside
(``telemetry.sqlite3.corrupt-<ts>``), never deleted. Every SQLite wait is
short (``BUSY_TIMEOUT_S``), so start-up and ``/v1/status`` never wait seconds
for a lock (Q3-08); status reads only cached values.

``seq`` across restarts (Q3-02):

* clean shutdown: everything is flushed and the exact last seq recorded --
  the next process continues at ``last + 1``, no gap;
* any other start (crash, power loss, degraded telemetry, a ring that could
  not be read): the next process starts at
  ``max(last stored seq, reservation, wall-clock microseconds) + 1``. The
  writer keeps a reservation ``RESERVE_AHEAD`` beyond what it has seen, but a
  failing disk cannot advance it; the microsecond floor makes the start
  exceed every seq a previous process can have handed out (that would need
  more than one event per microsecond since 1970). The jump leaves a gap and
  a ``telemetry.seq_floor`` event; a seq is never handed out twice. The only
  residual case is a wall clock set back *and* a ring that could not be read.
* a batch the writer could not store is kept and retried (bounded; if it
  has to be dropped, the dropped range is counted and reported as a gap).
"""
from __future__ import annotations

import itertools
import json
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

FLUSH_PERIOD_S = 0.1
RESERVE_AHEAD = 10_000            # seqs reserved on disk beyond the last emitted one
PENDING_MAX = 200_000             # unflushed (incl. retried) events kept if the disk stalls
BUSY_TIMEOUT_S = 0.5              # never wait longer than this for a SQLite lock
OPEN_TIMEOUT_S = 0.1              # ... and at start-up even less: a locked ring degrades at once
PRUNE_PERIOD_S = 5.0              # how often the writer removes events older than retention_s
FOREVER_TS = 1e18                 # "no upper bound" for a time-range query
MAX_SANE_SEQ = 1 << 62            # a stored seq/reservation above this is treated as corruption
LOCK_NAME = "telemetry.lock"


def _now_us() -> int:
    return time.time_ns() // 1000


class TelemetryUnavailable(Exception):
    """The ring cannot be used; telemetry stays in memory."""


class RingBusy(Exception):
    """A time-range read could not reach the ring right now; the caller must try again
    (answering from memory alone would look like "there is nothing more")."""


class EventBus:
    def __init__(self, maxlen: int = 5000, wall=None):
        """``wall``: the wall clock events are stamped with (``time.time``; tests inject one)."""
        self._wall = wall or time.time
        self.retention_s: float | None = None    # age limit of state and run data; None = count limit only
        self._taps: tuple = ()                   # lock-free consumers called by emit()
        self._next_prune = 0.0                   # monotonic; writer thread only
        self._oldest_ts_cache: float | None = None
        self.evicted_by_age = 0
        self.evicted_by_capacity = 0
        self._seq = itertools.count(1)
        self._events: deque[dict] = deque(maxlen=maxlen)
        self._pending: deque[dict] | None = None
        self._retry: list[dict] = []            # writer-thread only: a batch that failed to store
        self._db: sqlite3.Connection | None = None
        self._read_db: sqlite3.Connection | None = None
        self._db_lock = threading.Lock()        # writer connection; never taken by emit() or describe()
        self._read_lock = threading.Lock()      # reader connection (since/run_events), short timeouts
        self._capacity = 0
        self._writer: threading.Thread | None = None
        self._closing = threading.Event()
        self._reserved = 0
        self._lock_fd: int | None = None
        self._oldest_cache: int | None = None
        self.dropped_unpersisted = 0
        self.first_seq = 1
        self.path: Path | None = None
        self.write_errors = 0
        self.last_write_error: str | None = None
        self.degraded_reason: str | None = None
        self.quarantined: str | None = None
        self.start_kind = "memory"               # memory | clean | after_unclean | degraded

    # ------------------------------------------------------------ emit (stop-path safe)
    def emit(self, type_: str, **data: Any) -> dict:
        event = {"seq": next(self._seq), "ts": self._wall(), "type": type_, **data}
        self._events.append(event)
        pending = self._pending
        if pending is not None:
            if len(pending) >= PENDING_MAX:
                self.dropped_unpersisted += 1      # the oldest unflushed event is pushed out
            pending.append(event)
        for tap in self._taps:
            try:
                tap(event)
            except Exception:                      # a consumer can never break emit (stop path)
                pass
        return event

    def add_tap(self, tap) -> None:
        """Call ``tap(event)`` for every emitted event. A tap runs inside :meth:`emit`, so it
        must take no lock and do no IO (e.g. append to a ``deque``); what it raises is ignored."""
        self._taps = (*self._taps, tap)

    def _cutoff(self) -> float | None:
        """Events stamped before this are too old to keep or serve (``None``: no age limit)."""
        retention = self.retention_s
        return None if retention is None else self._wall() - retention

    # ------------------------------------------------------------ persistence
    def persist(self, path: str | Path, capacity: int = 100_000) -> bool:
        """Back this bus with a bounded SQLite ring and continue ``seq`` from it.

        Never raises for a problem with the ring (Q3-01): returns False and stays
        memory-only (``degraded_reason`` says why). Only an invalid ``capacity``
        (a programming error) raises.
        """
        if capacity < 1:
            raise ValueError("telemetry capacity 须为正整数")
        self.path = Path(path)
        self._capacity = int(capacity)
        try:
            start = self._open()
        except TelemetryUnavailable as exc:
            self._degrade(str(exc))
            return False
        except Exception as exc:                        # anything else: still never block start-up
            if isinstance(exc, sqlite3.OperationalError) and ("locked" in str(exc) or "busy" in str(exc)):
                self._degrade("遥测库被其他进程锁住（如 sqlite3 命令行正在查看）")
            else:
                self._degrade(f"遥测库不可用：{type(exc).__name__}: {exc}")
            return False
        self._seq = itertools.count(start)
        self.first_seq = start
        self._pending = deque(maxlen=PENDING_MAX)
        self._writer = threading.Thread(target=self._write_loop, name="nx-telemetry", daemon=True)
        self._writer.start()
        if self.start_kind == "after_unclean":
            self.emit("telemetry.seq_floor", first_seq=start,
                      reason="上次未正常关闭：seq 从不小于墙钟微秒数的值继续，可能留下缺口，但绝不重复")
        if self.quarantined:
            self.emit("telemetry.quarantined", path=self.quarantined,
                      reason="遥测库损坏，已改名隔离（未删除）并新建")
        return True

    def _degrade(self, reason: str) -> None:
        """Memory-only telemetry; seq starts above anything a previous process handed out."""
        for conn in (self._db, self._read_db):
            try:
                if conn is not None:
                    conn.close()
            except sqlite3.Error:
                pass
        self._db = self._read_db = None
        self._release_lock()
        self.degraded_reason = reason
        self.start_kind = "degraded"
        start = _now_us() + 1
        self._seq = itertools.count(start)
        self.first_seq = start
        self.emit("telemetry.persist_failed", reason=reason,
                  note="遥测只保存在内存中；安全监督与停止不受影响")

    def _acquire_lock(self) -> None:
        """One runtime per state directory may own the ring (Q3-17)."""
        import fcntl
        lock_path = self.path.parent / LOCK_NAME
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise TelemetryUnavailable(f"无法创建遥测锁文件：{exc.strerror}") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise TelemetryUnavailable("另一个运行时实例正在使用该状态目录的遥测库") from None
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)          # closing releases the flock
            except OSError:
                pass
            self._lock_fd = None

    def _quarantine(self, why: str) -> None:
        """Rename a corrupt ring (and its WAL/SHM) aside; never delete it."""
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.rename(self.path, target)
        except OSError as exc:
            raise TelemetryUnavailable(f"遥测库损坏（{why}），且无法改名隔离：{exc.strerror}") from None
        for suffix in ("-wal", "-shm"):
            side = self.path.with_name(self.path.name + suffix)
            try:
                os.rename(side, target.with_name(target.name + suffix))
            except OSError:
                pass
        self.quarantined = str(target)

    def _connect(self, timeout: float = BUSY_TIMEOUT_S) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=timeout, check_same_thread=False)
        db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        return db

    def _open(self) -> int:
        """Open (or recreate) the ring; return the first seq of this process."""
        self._acquire_lock()
        if self.path.exists() and not self.path.is_file():
            self._quarantine("同名路径不是普通文件")
        for attempt in (1, 2):
            db = None
            try:
                db = self._connect(OPEN_TIMEOUT_S)
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, run_id TEXT, type TEXT,"
                           " payload TEXT NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS events_run ON events (run_id, seq)")
                db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
                self._ensure_ts_column(db)
                db.commit()
                last = db.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
                meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
                reserved, clean = meta.get("reserved_seq", 0), meta.get("clean_shutdown", 0)
                for value in (last, reserved):
                    if not isinstance(value, int) or not 0 <= value <= MAX_SANE_SEQ:
                        raise sqlite3.DatabaseError(f"seq/预留值超出范围：{value!r}")
                oldest, self._oldest_ts_cache = self._oldest(db)
            except sqlite3.OperationalError as exc:
                if db is not None:
                    db.close()
                if "locked" in str(exc) or "busy" in str(exc):
                    raise TelemetryUnavailable("遥测库被其他进程锁住（如 sqlite3 命令行正在查看）") from None
                if attempt == 2:
                    raise TelemetryUnavailable(f"遥测库无法打开：{exc}") from None
                self._quarantine(str(exc))
                continue
            except sqlite3.DatabaseError as exc:          # "file is not a database", malformed, bad values
                if db is not None:
                    db.close()
                if attempt == 2:
                    raise TelemetryUnavailable(f"遥测库重建后仍不可用：{exc}") from None
                self._quarantine(str(exc))
                continue
            break
        if clean and reserved == last:
            start = last + 1
            self.start_kind = "clean"
        elif last == 0 and reserved == 0 and not self.quarantined and not clean:
            start = 1                                      # brand-new ring
            self.start_kind = "clean"
        else:
            start = max(last, reserved, _now_us()) + 1
            self.start_kind = "after_unclean"
        self._oldest_cache = oldest
        self._db = db
        try:
            self._read_db = self._connect()
        except sqlite3.Error:
            self._read_db = None
        # From now on the previous run's "clean" marker no longer describes us.
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('clean_shutdown', 0)")
        db.commit()
        self._reserved = 0
        self._reserve(start - 1)
        # Start-up writes above waited at most OPEN_TIMEOUT_S; the writer may wait a little longer.
        db.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        return start

    @staticmethod
    def _ensure_ts_column(db: sqlite3.Connection) -> None:
        """Every row carries its event time (age limit, time-range reads). A ring written before
        this column existed is upgraded in place: the time is read from each stored event; a row
        that cannot be read gets 0 (it counts as expired)."""
        if "ts" not in [row[1] for row in db.execute("PRAGMA table_info(events)")]:
            db.execute("ALTER TABLE events ADD COLUMN ts REAL")
        db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events (ts)")
        stamps = []
        for seq, payload in db.execute("SELECT seq, payload FROM events WHERE ts IS NULL").fetchall():
            try:
                ts = json.loads(payload)["ts"]
                ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0
            except (ValueError, TypeError, KeyError):
                ts = 0.0
            stamps.append((ts if ts == ts and abs(ts) != float("inf") else 0.0, seq))
        if stamps:
            db.executemany("UPDATE events SET ts = ? WHERE seq = ?", stamps)

    def _reserve(self, upto: int) -> None:
        """Keep the on-disk reservation ``RESERVE_AHEAD`` beyond ``upto`` (writer thread / init)."""
        if upto + RESERVE_AHEAD // 2 <= self._reserved:
            return
        target = upto + RESERVE_AHEAD
        with self._db_lock:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('reserved_seq', ?)", (target,))
            self._db.commit()
        self._reserved = target

    def _write_loop(self) -> None:
        while not self._closing.wait(FLUSH_PERIOD_S):
            self.flush()
            self._prune_if_due()
        self.flush()

    def _oldest(self, db: sqlite3.Connection) -> tuple:
        """``(seq, ts)`` of the oldest event that is still young enough to be served."""
        cutoff = self._cutoff()
        return db.execute("SELECT MIN(seq), MIN(ts) FROM events WHERE ts >= ?",
                          (-FOREVER_TS if cutoff is None else cutoff,)).fetchone()

    def _prune_if_due(self) -> None:
        if self.retention_s is None or time.monotonic() < self._next_prune:
            return
        self._next_prune = time.monotonic() + PRUNE_PERIOD_S
        self.prune()

    def prune(self) -> int:
        """Remove events older than ``retention_s`` from the ring. Returns how many went.
        Writer thread (and tests); a busy or failing disk is tried again next period."""
        db, cutoff = self._db, self._cutoff()
        if db is None or cutoff is None:
            return 0
        try:
            with self._db_lock:
                # The newest row is never removed: start-up compares it with the recorded last seq
                # to recognise a clean shutdown (no seq gap). Reads filter by time, so it is not served.
                removed = db.execute("DELETE FROM events WHERE ts < ? AND seq < (SELECT MAX(seq) FROM events)",
                                     (cutoff,)).rowcount
                db.commit()
                oldest, oldest_ts = self._oldest(db)
        except sqlite3.Error as exc:
            try:
                with self._db_lock:
                    db.rollback()
            except sqlite3.Error:
                pass
            self.write_errors += 1
            self.last_write_error = f"{type(exc).__name__}: {exc}"
            return 0
        self._oldest_cache, self._oldest_ts_cache = oldest, oldest_ts
        self.evicted_by_age += max(0, removed)
        return max(0, removed)

    def flush(self) -> int:
        """Write pending (and previously failed) events to the ring now. Returns how many were written.

        Writer thread (and close) only. A batch that cannot be stored is kept for
        the next attempt (Q3-02); beyond ``PENDING_MAX`` the oldest are dropped and
        counted (``dropped_unpersisted``) -- a gap in the ring, never a reused seq.
        """
        pending, db = self._pending, self._db
        if pending is None or db is None:
            return 0
        batch = self._retry
        self._retry = []
        while pending:
            try:
                batch.append(pending.popleft())
            except IndexError:
                break
        if not batch:
            return 0
        if len(batch) > PENDING_MAX:
            self.dropped_unpersisted += len(batch) - PENDING_MAX
            batch = batch[-PENDING_MAX:]
        rows = []
        for event in batch:
            run_id = event.get("run_id")
            try:
                payload = json.dumps(event, ensure_ascii=False, allow_nan=False, default=str)
            except ValueError:
                payload = json.dumps({**event, "data_dropped": "non-finite"}, default=str, allow_nan=True)
            ts = event.get("ts")
            ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts == ts else 0.0
            rows.append((event["seq"], run_id if isinstance(run_id, str) else None, event["type"], payload, ts))
        newest = max(r[0] for r in rows)
        try:
            # Reservation first: even if storing the rows fails afterwards, the next
            # process starts beyond everything handed out so far.
            self._reserve(newest)
            with self._db_lock:
                db.executemany("INSERT OR REPLACE INTO events (seq, run_id, type, payload, ts)"
                               " VALUES (?, ?, ?, ?, ?)", rows)
                trimmed = db.execute("DELETE FROM events WHERE seq <= ?", (newest - self._capacity,)).rowcount
                db.commit()
                oldest, oldest_ts = self._oldest(db)
            self._oldest_cache, self._oldest_ts_cache = oldest, oldest_ts
            self.evicted_by_capacity += max(0, trimmed)
        except sqlite3.Error as exc:
            try:
                with self._db_lock:
                    db.rollback()
            except sqlite3.Error:
                pass
            self.write_errors += 1
            self.last_write_error = f"{type(exc).__name__}: {exc}"
            self._retry = batch                       # keep it; retried next period
            return 0
        return len(rows)

    def close(self) -> None:
        """Flush and stop the writer; mark a clean shutdown with the exact last seq (no gap)."""
        if self._writer is None:
            self._release_lock()
            return
        self._closing.set()
        self._writer.join(5)
        self.flush()
        try:
            if not self._retry:                       # everything stored: the next start can follow on
                last = self.last_seq
                with self._db_lock:
                    self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('reserved_seq', ?)",
                                     (last,))
                    self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('clean_shutdown', 1)")
                    self._db.commit()
        except sqlite3.Error:
            pass
        for conn in (self._db, self._read_db):
            try:
                if conn is not None:
                    conn.close()
            except sqlite3.Error:
                pass
        self._db = self._read_db = None
        self._pending = None
        self._writer = None
        self._release_lock()

    # ------------------------------------------------------------ reading
    def _db_rows(self, sql: str, params: tuple) -> list[dict] | None:
        """Rows from the reader connection, or None if it is unavailable / busy (never waits long)."""
        db = self._read_db
        if db is None:
            return None
        if not self._read_lock.acquire(timeout=BUSY_TIMEOUT_S):
            return None
        try:
            rows = db.execute(sql, params).fetchall()
        except sqlite3.Error:
            return None
        finally:
            self._read_lock.release()
        return [json.loads(r[0]) for r in rows]

    def since(self, seq: int, limit: int = 500) -> list[dict]:
        # Copy first: iterating a deque while another thread appends raises.
        snapshot = list(self._events)
        cutoff = self._cutoff()
        if cutoff is not None:
            snapshot = [e for e in snapshot if e["ts"] >= cutoff]
        if self._read_db is None or (snapshot and seq >= snapshot[0]["seq"] - 1):
            return [e for e in snapshot if e["seq"] > seq][:limit]
        out = self._db_rows("SELECT payload FROM events WHERE seq > ? AND ts >= ? ORDER BY seq LIMIT ?",
                            (seq, -FOREVER_TS if cutoff is None else cutoff, limit))
        if out is None:                              # ring busy: what memory has
            return [e for e in snapshot if e["seq"] > seq][:limit]
        last = out[-1]["seq"] if out else seq
        if len(out) < limit:                         # newest events may not be flushed yet
            out += [e for e in snapshot if e["seq"] > last][:limit - len(out)]
        return out

    def run_events(self, run_id: str, since: int = 0, limit: int = 1000) -> list[dict]:
        """Every event of one run with ``seq > since`` (ring + not yet flushed)."""
        cutoff = self._cutoff()
        floor = -FOREVER_TS if cutoff is None else cutoff
        out = self._db_rows("SELECT payload FROM events WHERE run_id = ? AND seq > ? AND ts >= ?"
                            " ORDER BY seq LIMIT ?", (run_id, since, floor, limit)) or []
        last = out[-1]["seq"] if out else since
        if len(out) < limit:
            pending = list(self._retry) + list(self._pending or ()) + list(self._events)
            seen = set()
            extra = []
            for e in sorted(pending, key=lambda e: e["seq"]):
                if e["ts"] < floor:
                    continue
                if e.get("run_id") == run_id and e["seq"] > last and e["seq"] not in seen:
                    seen.add(e["seq"])
                    extra.append(e)
            out += extra[:limit - len(out)]
        return out

    def window(self, since_ts: float, until_ts: float, after: int = 0, limit: int = 500) -> list[dict]:
        """Events with ``since_ts <= ts < until_ts`` and ``seq > after``, oldest first: what a
        client fetches after a reconnect. Nothing older than ``retention_s`` is returned; fewer
        than ``limit`` events means there are no more."""
        cutoff = self._cutoff()
        low = since_ts if cutoff is None else max(since_ts, cutoff)
        out = self._db_rows("SELECT payload FROM events WHERE ts >= ? AND ts < ? AND seq > ? ORDER BY seq LIMIT ?",
                            (low, until_ts, after, limit))
        if out is None:
            if self._read_db is not None:
                raise RingBusy("telemetry ring busy")
            out = []                                 # a memory-only bus: memory is all there is
        last = out[-1]["seq"] if out else after
        if len(out) < limit:                         # not flushed yet, or a memory-only bus
            waiting = {}
            for e in list(self._retry) + list(self._pending or ()) + list(self._events):
                if e["seq"] > last and low <= e["ts"] < until_ts:
                    waiting[e["seq"]] = e
            out += [waiting[seq] for seq in sorted(waiting)][:limit - len(out)]
        return out

    def retention(self) -> dict:
        """Oldest seq and time still stored (cached by the writer; never touches the database)."""
        if self._oldest_cache is not None:
            return {"oldest_seq": self._oldest_cache, "oldest_ts": self._oldest_ts_cache}
        cutoff = self._cutoff()
        snapshot = [e for e in list(self._events) if cutoff is None or e["ts"] >= cutoff]
        return {"oldest_seq": snapshot[0]["seq"] if snapshot else None,
                "oldest_ts": snapshot[0]["ts"] if snapshot else None}

    def describe(self) -> dict:
        """For ``/v1/status``: cached values only, no lock, no IO (Q3-08)."""
        out = {"persistent": self._db is not None, "capacity": self._capacity or None,
               "path": str(self.path) if self.path else None, "last_seq": self.last_seq,
               "first_seq_this_boot": self.first_seq, "start": self.start_kind,
               "unflushed": len(self._pending or ()) + len(self._retry),
               "write_errors": self.write_errors, "dropped_unpersisted": self.dropped_unpersisted,
               "degraded": self.degraded_reason is not None, **self.retention(),
               "retention_s": self.retention_s, "evicted_by_age": self.evicted_by_age,
               "evicted_by_capacity": self.evicted_by_capacity}
        if self.degraded_reason:
            out["degraded_reason"] = self.degraded_reason
        if self.quarantined:
            out["quarantined"] = self.quarantined
        if self.last_write_error:
            out["last_write_error"] = self.last_write_error
        return out

    @property
    def last_seq(self) -> int:
        snapshot = list(self._events)
        return snapshot[-1]["seq"] if snapshot else self.first_seq - 1
