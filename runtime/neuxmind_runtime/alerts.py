"""Alerts raised on the device, kept there until the cloud has them (issue #26, requirement R7-08).

An alert carries what R7-08 asks for -- level, time, device, reason, advice -- plus an id made on
the device (the cloud stores an alert once per id, so sending it again is harmless) and a machine
``kind``::

    {"schema": "neuxmind.alert.v1", "id": "...", "kind": "estop_latched", "level": "critical",
     "ts": 1763500000.0, "device": {"instance_id", "name", "family", "simulated"},
     "reason": "...", "advice": "...", "detail": {...}}

Its text comes from people and from drivers (the reason an operator sends with a stop, for one), so
it is made sendable when the alert is made: what UTF-8 cannot encode and control characters are
replaced, lengths are bounded. One alert that cannot be serialised would otherwise block every
report after it.

Two parts live here; sending is ``alert_report.py``:

* :class:`AlertBook` -- the alerts of this device. Memory is the working copy; the reporter's thread
  writes it behind to ``<state-dir>/alerts.sqlite3`` so that what was not sent survives a restart.
  Adding an alert, settling one and reading the counts never touch the disk and hold a lock only
  for a few dictionary operations; a disk that stalls or refuses a write stalls only the reporter.
* :class:`LatchWatch` -- the two conditions the runtime can judge today, the e-stop latch and "the
  body does not obey the stop". It *looks* at the supervisor and the latch guard from the reporter's
  thread; nothing on the stop path calls into this module, takes one of its locks or waits for its
  disk. Every other source (a failed capability instance, a failed upgrade, a full disk ...) goes
  through ``EdgeRuntime.raise_alert`` when the feature behind it exists.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = "neuxmind.alert.v1"
CRITICAL, WARNING, INFO = "critical", "warning", "info"     # critical = R7-08's "最高级别"
LEVELS = (CRITICAL, WARNING, INFO)
ESTOP_LATCHED = "estop_latched"
STOP_NOT_OBEYED = "stop_not_obeyed"

MAX_TEXT = 500                  # characters of reason / advice and of any string in detail
MAX_DETAIL_BYTES = 2000         # JSON size of detail; larger is replaced by a marker
MAX_DETAIL_DEPTH = 4
MAX_DETAIL_ITEMS = 50           # keys of an object / items of a list in detail
MAX_KEY_CHARS = 100
MAX_INSTANCE_ID = 200           # the cloud's limits for the device's identity
MAX_NAME = 200
MAX_FAMILY = 30
MAX_ROW_BYTES = 65_536          # a stored row above this is not an alert (one is a few kilobytes)
MAX_TS = 1e11                   # seconds; what the cloud takes as a time
CAPACITY = 2000                 # alerts kept on the device (sent ones included)
BOOK_NAME = "alerts.sqlite3"
BUSY_TIMEOUT_S = 0.2            # the longest one statement waits for a lock on the file
IO_WAIT_S = 0.5                 # ... and one caller for another caller's disk work
CLOSE_WAIT_S = 1.0              # close() gives up on a disk that does not answer
UNSENT, SENT, REJECTED = "unsent", "sent", "rejected"
EPISODE_FLAG = "estop_episode_open"
KNOWN_FLAGS = ("dropped", EPISODE_FLAG)     # the only flags read back from the file
OPEN_RETRY_S = 60.0             # a book file that cannot be reached right now is tried again this often
LOOK_RETRIES = 4                # looks at the safety state before one that may be torn is used

ESTOP_ADVICE = "到现场确认机器人周围安全、排除触发原因后，在控制台解除急停"
ESTOP_PASSED_REASON = "急停被触发过，上报时已解除（停止和复位发生在两次检查之间）"
ESTOP_PASSED_ADVICE = "确认是谁、为什么停止和复位了这台设备"
NOT_OBEYED_REASON = "停止未生效：急停锁定期间本体在多次再停之后仍在运动，软件停止已无法让它停下"
NOT_OBEYED_ADVICE = "请立即使用硬件急停或切断动力电，在现场检查驱动和电机控制器后再解除急停"
NOT_OBEYED_DETAIL = ("trigger", "motion_restops", "restops", "since_s", "displacement_m")

_KIND = re.compile(r"[a-z0-9][a-z0-9_.-]{0,59}")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_KEEP_CONTROL = ("\n", "\t")


# ---------------------------------------------------------------- making an alert sendable
def clean_text(value: str, limit: int) -> str:
    """``value`` as text that every JSON library, SQLite and PostgreSQL take: lone surrogates
    become ``?``, control characters a space; trimmed and cut to ``limit`` characters."""
    text = value.encode("utf-8", "replace").decode("utf-8")
    text = "".join(ch if ch in _KEEP_CONTROL or not (ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0) else " " for ch in text)
    return text.strip()[:limit]


def _text(value, what: str) -> str:
    text = clean_text(value, MAX_TEXT) if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"告警的{what}须为非空文字")
    return text


def _clean_value(value, depth: int = 0):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return clean_text(value, MAX_TEXT)
    if depth >= MAX_DETAIL_DEPTH:
        return None
    if isinstance(value, dict):
        return {clean_text(str(key), MAX_KEY_CHARS): _clean_value(item, depth + 1)
                for key, item in list(value.items())[:MAX_DETAIL_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [_clean_value(item, depth + 1) for item in list(value)[:MAX_DETAIL_ITEMS]]
    return clean_text(str(value), MAX_TEXT)


def _detail(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("告警的 detail 须为对象")
    cleaned = _clean_value(value)
    try:
        size = len(json.dumps(cleaned, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        return {"dropped": "not_json"}
    return cleaned if size <= MAX_DETAIL_BYTES else {"dropped": "too_large"}


def _timestamp(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < MAX_TS:    # NaN fails it too
        raise ValueError("告警的时间须为时间戳（秒）")
    return float(value)


def device_view(manifest: dict) -> dict:
    """The "device" of an alert: who it is, from the body's own manifest (text made sendable)."""
    def text(key: str, limit: int) -> str:
        value = manifest.get(key)
        return clean_text(value, limit) if isinstance(value, str) else ""
    return {"instance_id": text("instance_id", MAX_INSTANCE_ID + 1), "name": text("name", MAX_NAME),
            "family": text("family", MAX_FAMILY), "simulated": manifest.get("simulated") is True}


def device_problem(manifest: dict) -> str | None:
    """Why this body cannot report (the cloud files alerts under its ``instance_id``), or ``None``."""
    raw = manifest.get("instance_id")
    if (not isinstance(raw, str) or not raw or len(raw) > MAX_INSTANCE_ID or clean_text(raw, MAX_INSTANCE_ID) != raw
            or any(ch in raw for ch in _KEEP_CONTROL)):
        return "本体清单里的 instance_id 不能用来上报（须为 1–200 个字符，不含控制字符）"
    return None


def make_alert(kind: str, level: str, reason: str, advice: str, device: dict, *, ts: float,
               detail: dict | None = None) -> dict:
    """A new alert (``ValueError`` for a kind, level, time or text that is not one)."""
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise ValueError("告警类型须为 1–60 个小写字母、数字、下划线、点或连字符")
    if level not in LEVELS:
        raise ValueError(f"告警级别须为 {' / '.join(LEVELS)}")
    return {"schema": SCHEMA, "id": uuid.uuid4().hex, "kind": kind, "level": level, "ts": _timestamp(ts),
            "device": dict(device), "reason": _text(reason, "原因"), "advice": _text(advice, "建议"),
            "detail": _detail(detail)}


def restore_alert(alert_id: str, stored) -> dict | None:
    """An alert read back from the file, put through the same checks as a new one; ``None`` if it is
    not an alert. The file sits in the state directory, which in the non-root mode a skill can write."""
    if not isinstance(stored, dict) or stored.get("id") != alert_id or not _ID.fullmatch(alert_id):
        return None
    device = stored.get("device")
    try:
        fresh = make_alert(stored.get("kind"), stored.get("level"), stored.get("reason"), stored.get("advice"),
                           device_view(device if isinstance(device, dict) else {}), ts=stored.get("ts"),
                           detail=stored.get("detail") if isinstance(stored.get("detail"), dict) else None)
    except ValueError:
        return None
    return {**fresh, "id": alert_id}


class _TryLater(Exception):
    """The book file cannot be reached right now; that says nothing about what it holds."""


def _reason(exc: Exception, path: Path) -> str:
    """An error as text for ``/v1/status``, without the state directory in it."""
    text = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
    return f"{type(exc).__name__}: {text.replace(str(path), '…').replace(str(path.parent), '…')[:200]}"


# ---------------------------------------------------------------- the book
class AlertBook:
    """The device's alerts and whether the cloud has each one.

    ``add``, ``settle``, ``set_flag``, ``unsent`` and ``counts`` work on memory under a lock that is
    never held across disk work (``counts`` takes none), so they can be called from any thread.
    ``open``, ``sync`` and ``close`` do the disk work and belong to the reporter's thread; they
    serialise on their own lock. No method raises for a storage problem: a write the disk did not
    take is tried again on the next ``sync``.
    """

    def __init__(self, path: str | Path | None, capacity: int = CAPACITY, clock=time.monotonic):
        self.capacity = max(1, int(capacity))
        self._clock = clock                          # paces the retries of open()
        self._retry_at = 0.0
        self.storage = "memory" if path is None else "pending"      # pending -> disk | memory
        self.note: str | None = None
        self.dropped = 0
        self.write_errors = 0
        self._path = None if path is None else Path(path)
        self._opened = path is None
        self._lock = threading.Lock()                # memory only; never held across disk work
        self._io = threading.Lock()                  # the SQLite connection
        self._records: dict[str, dict] = {}          # id -> {"seq", "state", "alert"}, oldest first
        self._flags: dict[str, str] = {}
        self._changed: dict[str, int] = {}           # id -> revision of its latest change
        self._removed: set[str] = set()
        self._flags_revision = 0
        self._flags_written = 0
        self._revision = 0
        self._seq = 0
        self._counts = {UNSENT: 0, SENT: 0, REJECTED: 0}
        self._db: sqlite3.Connection | None = None

    # ------------------------------------------------------------ opening (disk)
    def open(self) -> None:
        """Open or create the file and take in what it holds. Called on the reporter's rounds, not at
        start-up: a state disk that does not answer then holds up the reporter only. Never raises.

        A file that is not a database is set aside (renamed, never deleted) and a new one started. A
        file that cannot be reached right now -- locked by another process, a read error, no
        descriptor left -- is left exactly as it is, with the unsent alerts it holds: the book works
        in memory and tries again every ``OPEN_RETRY_S``."""
        if self._opened or self._clock() < self._retry_at:
            return
        with self._io:
            if self._opened:
                return
            try:
                rows, flags = self._open_file(self._path)
                restored = self._restore(rows)               # checked outside the memory lock: add() never waits for it
                with self._lock:
                    self._merge(restored, flags)
            except _TryLater as later:
                self._close_file()
                self._memory_only(f"告警库现在打不开，告警先只保存在内存里，稍后再试：{later}")
                self._retry_at = self._clock() + OPEN_RETRY_S
                return
            except Exception as exc:                         # a file like no other: memory only, said in the status
                self._close_file()
                self._memory_only(f"告警库不可用，本次运行的告警只保存在内存里：{_reason(exc, self._path)}")
            self._opened = True

    def _open_file(self, path: Path) -> tuple[list, dict]:
        for attempt in (1, 2):
            db = None
            try:
                # private from the first byte, and never through a link someone left in its place
                os.close(os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
                os.chmod(path, 0o600)
                db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, check_same_thread=False)
                db.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
                db.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, seq INTEGER NOT NULL,"
                           " state TEXT NOT NULL, payload TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
                rows = self._read_rows(db)
                flags = dict(db.execute("SELECT name, value FROM flags WHERE name IN (?, ?)", KNOWN_FLAGS).fetchall())
            except (sqlite3.OperationalError, OSError) as exc:
                # not a verdict on the file: it may be perfectly good and hold unsent alerts
                if db is not None:
                    db.close()
                raise _TryLater(_reason(exc, path)) from None
            except sqlite3.DatabaseError as exc:             # "file is not a database", "malformed": damaged
                if db is not None:
                    db.close()
                if attempt == 2 or not self._set_aside(path):
                    self._memory_only(f"告警库已损坏且无法改名隔离，本次运行的告警只保存在内存里：{_reason(exc, path)}")
                    return [], {}
                continue
            self._db = db
            self.storage = "disk"
            self.note = None
            return rows, flags
        return [], {}

    def _read_rows(self, db: sqlite3.Connection) -> list:
        """``(id, state, payload)`` of the newest rows a book can hold, oldest first. Whatever else is
        in the file -- rows that are not text, a row far beyond the size of an alert, rows beyond
        twice the capacity -- is not ours and goes. The stored sequence only orders the rows, and
        only where it is an integer."""
        order = "CASE WHEN typeof(seq) = 'integer' THEN seq ELSE 0 END, rowid"
        db.execute("DELETE FROM alerts WHERE typeof(id) != 'text' OR typeof(payload) != 'text'"
                   " OR LENGTH(CAST(payload AS BLOB)) > ?", (MAX_ROW_BYTES,))
        excess = db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] - self.capacity * 2
        if excess > 0:
            db.execute(f"DELETE FROM alerts WHERE rowid IN (SELECT rowid FROM alerts ORDER BY {order} LIMIT ?)",
                       (excess,))
        db.commit()
        return db.execute(f"SELECT id, state, payload FROM alerts ORDER BY {order}").fetchall()

    @staticmethod
    def _restore(rows: list) -> list[tuple]:
        """``(id, state, alert)`` for every row; ``alert`` is ``None`` for a row that is not one.
        The order of the rows is kept; the sequence numbers the file gives are not trusted."""
        restored = []
        for alert_id, state, payload in rows:
            alert = None
            if isinstance(alert_id, str) and state in (UNSENT, SENT, REJECTED) and isinstance(payload, str):
                try:
                    alert = restore_alert(alert_id, json.loads(payload))
                except (ValueError, TypeError, RecursionError):
                    alert = None
            restored.append((alert_id, state, alert))
        return restored

    def _memory_only(self, note: str) -> None:
        self.storage, self.note = "memory", note

    def _close_file(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None

    @staticmethod
    def _set_aside(path: Path) -> bool:
        """Rename what cannot be used as the book (a damaged file, a directory); never delete it."""
        target = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
        try:
            os.rename(path, target)
        except OSError:
            return False
        return True

    def _merge(self, restored: list[tuple], flags: dict) -> None:
        """What the file held comes first, renumbered; alerts raised before it was open follow (and are
        new to it). Built on the side and put in place at the end: nothing is half taken in."""
        records: dict[str, dict] = {}
        counts = {UNSENT: 0, SENT: 0, REJECTED: 0}
        removed = set()
        for alert_id, state, alert in restored:
            if alert is None or alert_id in records:
                if isinstance(alert_id, str) and alert_id not in records:
                    removed.add(alert_id)                    # not an alert: it goes with the next sync
                continue
            records[alert_id] = {"seq": len(records) + 1, "state": state, "alert": alert}
            counts[state] += 1
        for record in self._records.values():                # raised before open(): after what the file held
            alert_id = record["alert"]["id"]
            if alert_id not in records:
                counts[record["state"]] += 1
            records[alert_id] = {**record, "seq": len(records) + 1}
        stored = {str(k): str(v) for k, v in flags.items()}
        try:
            dropped = self.dropped + max(0, int(stored.get("dropped", 0)))
        except ValueError:
            dropped = self.dropped
        self._records, self._counts, self._seq, self.dropped = records, counts, len(records), dropped
        self._removed |= removed
        for alert_id in records:                             # every row is written again, under its new number
            self._touch(alert_id)
        self._flags = {**stored, **self._flags, "dropped": str(dropped)}
        self._flags_revision += 1
        self._trim()

    # ------------------------------------------------------------ writing behind (disk)
    def sync(self) -> bool:
        """Write what changed since the last successful write. True when nothing is left to write.
        Waits at most ``IO_WAIT_S`` for another caller's disk work."""
        if self._db is None:
            return True
        if not self._io.acquire(timeout=IO_WAIT_S):
            return False
        try:
            return self._sync()
        finally:
            self._io.release()

    def _sync(self) -> bool:
        db = self._db
        if db is None:
            return True
        with self._lock:
            changed = {i: (rev, dict(self._records[i])) for i, rev in self._changed.items() if i in self._records}
            removed = set(self._removed)
            flags_revision = self._flags_revision
            flags = dict(self._flags) if flags_revision != self._flags_written else None
        if not (changed or removed or flags is not None):
            return True
        rows, unusable = [], []
        for alert_id, (_revision, record) in changed.items():
            try:
                payload = json.dumps(record["alert"], ensure_ascii=False, allow_nan=False)
                payload.encode("utf-8")
            except (ValueError, TypeError, RecursionError):
                unusable.append(alert_id)                    # cannot be stored, so it could not be sent either
                continue
            rows.append((alert_id, record["seq"], record["state"], payload))
        try:
            self._write(db, rows, removed, flags)
        except Exception as exc:                             # sqlite3.Error, and whatever a strange value raises
            try:
                db.rollback()
            except Exception:
                pass
            self.write_errors += 1
            self.note = f"告警库暂时写不进去（告警留在内存里，稍后重试）：{_reason(exc, self._path)}"
            return False
        with self._lock:
            for alert_id, (revision, _record) in changed.items():
                if self._changed.get(alert_id) == revision:
                    del self._changed[alert_id]
            self._removed -= removed
            if flags is not None:
                self._flags_written = flags_revision
            for alert_id in unusable:
                self._discard(alert_id)
        self.note = None
        return True

    @staticmethod
    def _write(db: sqlite3.Connection, rows: list, removed: set, flags: dict | None) -> None:
        db.executemany("DELETE FROM alerts WHERE id=?", [(alert_id,) for alert_id in removed])
        db.executemany("INSERT OR REPLACE INTO alerts (id, seq, state, payload) VALUES (?, ?, ?, ?)", rows)
        if flags is not None:
            db.executemany("INSERT OR REPLACE INTO flags (name, value) VALUES (?, ?)", list(flags.items()))
        db.commit()

    def close(self) -> None:
        """Write what is left and close the file. Gives up after ``CLOSE_WAIT_S`` when another thread
        is still inside a disk call (the file is then left to the operating system)."""
        if not self._io.acquire(timeout=CLOSE_WAIT_S):
            return
        try:
            self._sync()
            self._close_file()
        finally:
            self._io.release()

    # ------------------------------------------------------------ alerts (memory)
    def _touch(self, alert_id: str) -> None:
        self._revision += 1
        self._changed[alert_id] = self._revision

    def _discard(self, alert_id: str) -> None:
        record = self._records.pop(alert_id, None)
        if record is None:
            return
        self._counts[record["state"]] -= 1
        self._changed.pop(alert_id, None)
        if self._db is not None:                     # without a file there is nothing to take out of it
            self._removed.add(alert_id)
        if record["state"] == UNSENT:
            self.dropped += 1
            self._flags["dropped"] = str(self.dropped)
            self._flags_revision += 1

    def _trim(self) -> None:
        """Over capacity: alerts the cloud already has go first, then the oldest ordinary unsent
        one, the highest level last. An unsent alert that goes is counted (``dropped``)."""
        while len(self._records) > self.capacity:
            records = self._records.values()
            victim = (next((r for r in records if r["state"] != UNSENT), None)
                      or next((r for r in records if r["alert"].get("level") != CRITICAL), None)
                      or next(iter(records)))
            self._discard(victim["alert"]["id"])

    def add(self, alert: dict) -> dict:
        with self._lock:
            self._seq += 1
            self._records[alert["id"]] = {"seq": self._seq, "state": UNSENT, "alert": alert}
            self._counts[UNSENT] += 1
            self._touch(alert["id"])
            self._trim()
        return alert

    def unsent(self, max_alerts: int, max_bytes: int, skip=frozenset()) -> list[dict]:
        """The next alerts to send: the highest level first, then oldest first; at least one, unless
        all are in ``skip`` (ids to leave out this time)."""
        with self._lock:
            waiting = [r for r in self._records.values() if r["state"] == UNSENT and r["alert"]["id"] not in skip]
        waiting.sort(key=lambda r: (r["alert"].get("level") != CRITICAL, r["seq"]))
        out, size = [], 0
        for record in waiting:
            try:
                weight = len(json.dumps(record["alert"], ensure_ascii=False, allow_nan=False).encode("utf-8")) + 2
            except (ValueError, TypeError, RecursionError):
                continue                                     # dropped by the next sync
            if out and (len(out) >= max_alerts or size + weight > max_bytes):
                break
            out.append(record["alert"])
            size += weight
        return out

    def unsent_count(self, skip=frozenset()) -> int:
        """How many alerts are still to be sent, not counting the ids in ``skip``."""
        with self._lock:
            return sum(1 for r in self._records.values() if r["state"] == UNSENT and r["alert"]["id"] not in skip)

    def settle(self, sent, rejected) -> None:
        """The cloud has ``sent``; ``rejected`` will not be sent again (kept, marked)."""
        with self._lock:
            for ids, state in ((sent, SENT), (rejected, REJECTED)):
                for alert_id in ids:
                    record = self._records.get(alert_id)
                    if record is None or record["state"] != UNSENT:
                        continue
                    self._records[alert_id] = {**record, "state": state}
                    self._counts[UNSENT] -= 1
                    self._counts[state] += 1
                    self._touch(alert_id)

    def counts(self) -> dict:
        """``{unsent, sent, rejected, dropped}``; no lock, no disk."""
        return {**self._counts, "dropped": self.dropped}

    # ------------------------------------------------------------ flags (memory)
    def flag(self, name: str) -> str | None:
        return self._flags.get(name)

    def set_flag(self, name: str, value: str) -> None:
        with self._lock:
            if self._flags.get(name) != value:
                self._flags[name] = value
                self._flags_revision += 1


# ---------------------------------------------------------------- the two conditions of this round
class LatchWatch:
    """The e-stop latch and "the body does not obey the stop", noticed by looking.

    ``poll()`` reads a handful of attributes of the safety supervisor and the latch guard; it makes
    no adapter call and is never called from the stop path.

    A latch lasts from a stop to the reset that clears it, so the supervisor's count of resets
    numbers the latches. One alert per latch, however many stops it holds. The watch acts only on
    what two looks in a row agree on: the supervisor changes its counters and its latch one after the
    other (a stop counts first and latches next, a reset counts first and unlatches next), and a look
    that lands in between sees a state that never was. That second look is also why the reason an
    operator sends with the stop is the one reported. A latch that came and went between looks
    raises one alert ("... already cleared"); so does one the runtime started with that is reset
    before it could be reported. A latch the runtime started with is reported unless an earlier run
    already did (remembered in the book).
    """

    def __init__(self, runtime, book: AlertBook, emit):
        from .core import ABNORMAL_SHUTDOWN_REASON     # local: core imports nothing from here at load
        self._abnormal = ABNORMAL_SHUTDOWN_REASON
        self._sup = runtime.supervisor
        self._guard = runtime.latch_guard
        self._book = book
        self._emit = emit
        self._covered = self._sup.stop_count           # stops that are reported or belong to a reported latch
        self._reported: int | None = None              # the latch (reset count) already reported
        self._last: tuple | None = None                # (latched, stops, resets) of the look before
        self._first = True
        self._not_obeyed_at = None

    def poll(self) -> None:
        self._estop()
        self._not_obeyed()

    def _look(self) -> tuple:
        """``(latched, reason, stops, resets)``; taken again (a few times at most) when a counter
        changed while it was being read."""
        sup = self._sup
        for _ in range(LOOK_RETRIES):
            before = (sup.stop_count, sup.reset_count)
            latched, reason = sup.latched, sup.latch_reason
            if (sup.stop_count, sup.reset_count) == before:
                break
        return latched, reason, before[0], before[1]

    def _carried_over(self, latched: bool, reason, stops: int, resets: int) -> None:
        """First look: is the latch this runtime started with one an earlier run reported?"""
        reported = self._book.flag(EPISODE_FLAG) == "1"
        if latched and reported and resets == 0 and reason != self._abnormal:
            self._reported = resets
        elif latched:
            self._covered = stops - 1                  # owed an alert, though a restored latch counts no stop
        elif reported:
            self._book.set_flag(EPISODE_FLAG, "0")

    def _estop(self) -> None:
        latched, reason, stops, resets = self._look()
        if self._first:
            self._first = False
            self._carried_over(latched, reason, stops, resets)
        steady = (latched, stops, resets) == self._last
        self._last = (latched, stops, resets)
        if not steady:
            return                                     # wait for a second look that says the same
        if self._reported is not None and (not latched or resets != self._reported):
            # the reported latch is over; with one reset since and no latch now, every stop so far was part of it
            if not latched and resets == self._reported + 1:
                self._covered = stops
            self._reported = None
            self._book.set_flag(EPISODE_FLAG, "0")
        if not latched:
            if stops > self._covered:                  # a whole latch between looks
                self._emit(ESTOP_LATCHED, CRITICAL, ESTOP_PASSED_REASON, ESTOP_PASSED_ADVICE, {"stop_count": stops})
            self._covered = stops
            return
        if self._reported is None:
            self._emit(ESTOP_LATCHED, CRITICAL, f"急停已锁定：{reason or '未说明原因'}", ESTOP_ADVICE,
                       {"stop_count": stops})
            self._reported = resets
            self._book.set_flag(EPISODE_FLAG, "1")
        self._covered = stops

    def _not_obeyed(self) -> None:
        guard = self._guard
        info = guard.stop_not_obeyed if guard is not None else None
        if not isinstance(info, dict):
            self._not_obeyed_at = None
            return
        at = info.get("at")
        if at == self._not_obeyed_at:
            return
        self._not_obeyed_at = at
        detail = {key: info[key] for key in NOT_OBEYED_DETAIL if isinstance(info.get(key), (int, float, str))}
        usable = isinstance(at, (int, float)) and not isinstance(at, bool) and 0 <= at < MAX_TS
        self._emit(STOP_NOT_OBEYED, CRITICAL, NOT_OBEYED_REASON, NOT_OBEYED_ADVICE, detail, at if usable else None)
