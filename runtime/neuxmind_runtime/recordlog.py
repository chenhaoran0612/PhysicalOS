"""One append-only, segmented JSON-lines log (the storage under ``records.py``).

A log is a directory of segments. The segment being written is ``seg-<first id>.open``; when it
reaches ``segment_bytes`` it is renamed to
``seg-<first id>-<last id>-<min ts ms>-<max ts ms>-<count>.jsonl``, so opening a log reads file
names, not file contents. Removing the oldest records is ``unlink`` of the oldest segment, which
works on a full disk. Record ids grow by one per record and are never reused: when the last
segment of a log is removed its last id is first saved in ``floor`` (a fixed-size file created
with the log and overwritten in place, so saving it needs no free space).

Only files with these names are ever written, renamed or removed, and only while they are regular
files. Nothing here is called from the stop path.
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DEFAULT_SEGMENT_BYTES = 256 << 10
MIN_SEGMENT_BYTES = 64
MAX_RECORD_BYTES = 256 << 10              # one record; a larger one keeps its short fields only
MAX_QUERY_LIMIT = 5000
MAX_QUERY_BYTES = 4 << 20                 # one answer
LOCK_TIMEOUT_S = 0.5                      # a reader never waits longer than this for a writer stuck in the disk
CLOSE_JOIN_S = 2.0
FLOOR_NAME = "floor"
FLOOR_DIGITS = 20                         # the floor file always holds exactly this many digits
MAX_TS = 1e11                             # seconds; a record time outside [0, MAX_TS) is filed under 0

_OPEN_NAME = re.compile(r"^seg-(\d{16})\.open$")
_CLOSED_NAME = re.compile(r"^seg-(\d{16})-(\d{16})-(\d{1,18})-(\d{1,18})-(\d{1,12})\.jsonl$")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _scrub(value):
    """A copy with NaN/Infinity replaced by ``None`` (they are not JSON)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _dumps(obj: dict) -> bytes:
    text = json.dumps(obj, ensure_ascii=False, allow_nan=False, default=str, separators=(",", ":"))
    return text.encode("utf-8", "backslashreplace")       # a lone surrogate becomes its \uXXXX escape


def _encode(record_id: int, record: dict) -> tuple[bytes, float]:
    """One line for ``record`` (never raises) and the time it is filed under."""
    ts = record.get("ts")
    ts = float(ts) if _is_number(ts) and 0 <= ts < MAX_TS else 0.0
    obj = {"id": record_id, **{k: v for k, v in record.items() if k not in ("id", "ts")}, "ts": ts}
    try:
        line = _dumps(obj)
    except Exception:                         # NaN, a value json cannot take, a broken __str__ ...
        try:
            line = _dumps(_scrub(obj))
        except Exception:
            kind = record.get("type")
            line = _dumps({"id": record_id, "ts": ts, "type": kind if isinstance(kind, str) else None,
                           "data_dropped": "unserialisable"})
    if len(line) > MAX_RECORD_BYTES:
        short = {k: v for k, v in obj.items()
                 if v is None or isinstance(v, (bool, int)) or (isinstance(v, float) and math.isfinite(v))
                 or (isinstance(v, str) and len(v) <= 500)}
        line = _dumps({**short, "truncated": True})
    return line + b"\n", ts


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


@dataclass(frozen=True)
class _Segment:
    path: Path
    first_id: int
    last_id: int
    min_ts: float
    max_ts: float
    count: int
    size: int


def _empty_stats(last_id: int = 0) -> dict:
    return {"count": 0, "bytes": 0, "segments": 0, "first_id": None, "last_id": last_id,
            "oldest_ts": None, "newest_ts": None}


class RecordLog:
    """Append-only, segmented JSON-lines log of one record class."""

    def __init__(self, directory: str | Path, segment_bytes: int = DEFAULT_SEGMENT_BYTES,
                 max_segments: int | None = None):
        self.directory = Path(directory)
        self.segment_bytes = int(segment_bytes)
        self.max_segments = max_segments
        self._lock = threading.Lock()         # segment list and the open file; never taken by offer_*/describe
        self._closed: list[_Segment] = []
        self._current: _Segment | None = None
        self._fd: int | None = None
        self._torn = False                    # a failed write left bytes that could not be removed
        self._next_id = 1
        self._closed_sums = (0, 0, None, None)  # count, bytes, oldest ts, newest ts of the closed segments
        self._stats = _empty_stats()
        self.skipped_files = 0                # files with a segment's name that could not be used
        self.opened = False

    # ------------------------------------------------------------ open / close
    def open(self) -> None:
        """Create or read the directory. Raises ``OSError`` when it cannot be used."""
        with self._lock:
            if self.opened:
                return
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            try:
                os.chmod(self.directory, 0o700)
            except OSError:
                pass
            closed, last = [], self._read_floor()
            self.skipped_files = 0
            for name in sorted(os.listdir(self.directory)):
                path = self.directory / name
                match, leftover = _CLOSED_NAME.match(name), _OPEN_NAME.match(name)
                if not match and not leftover:
                    continue                               # not ours: never touched
                try:
                    mode = os.lstat(path)
                except OSError:
                    mode = None
                if match:
                    first_id, last_id, min_ms, max_ms, count = (int(g) for g in match.groups())
                    last = max(last, last_id)              # its ids were handed out even if it cannot be used
                    if mode is None or not stat.S_ISREG(mode.st_mode):
                        self.skipped_files += 1            # vanished, or a link/directory with one of our names
                        continue
                    closed.append(_Segment(path, first_id, last_id, min_ms / 1000.0, max_ms / 1000.0, count,
                                           mode.st_size))
                    continue
                if mode is None or not stat.S_ISREG(mode.st_mode):
                    self.skipped_files += 1
                    continue
                segment = self._adopt(path, mode.st_size)  # left by a process that did not roll it
                if segment is not None:
                    closed.append(segment)
                    last = max(last, segment.last_id)
                elif mode.st_size:
                    # set aside unread: no id it may hold is handed out again (a line is > 16 bytes)
                    last = max(last, int(leftover.group(1)) + mode.st_size // 16)
            self._closed = sorted(closed, key=lambda s: s.first_id)
            self._current, self._fd, self._torn = None, None, False
            self._next_id = last + 1
            self._ensure_floor(last)
            self.opened = True
            self._sum_closed()
            self._refresh()

    def close(self, timeout: float = CLOSE_JOIN_S) -> None:
        """Close the open file (it stays ``.open`` and is adopted by the next :meth:`open`)."""
        if not self._lock.acquire(timeout=timeout):
            return                                         # a writer is stuck in the disk: leave it
        try:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
            self._fd, self._current, self.opened = None, None, False
        finally:
            self._lock.release()

    def _read_floor(self) -> int:
        try:
            with open(self.directory / FLOOR_NAME, "rb") as handle:
                value = int(handle.read(64).strip() or 0)
        except (OSError, ValueError):
            return 0
        return value if 0 <= value < 1 << 62 else 0

    def _ensure_floor(self, last_id: int) -> None:
        """Create the floor file while there is room for it; later it is only overwritten."""
        try:
            if os.lstat(self.directory / FLOOR_NAME).st_size == FLOOR_DIGITS:
                return
        except OSError:
            pass
        try:
            self._write_floor(last_id)
        except OSError:
            pass                                           # tried again when a segment has to go

    def _write_floor(self, last_id: int) -> None:
        """Remember the highest id handed out before the segment that holds it goes away."""
        fd = os.open(self.directory / FLOOR_NAME, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.pwrite(fd, str(int(last_id)).zfill(FLOOR_DIGITS).encode("ascii"), 0)
            os.ftruncate(fd, FLOOR_DIGITS)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _sync_directory(self) -> None:
        """Make a new or changed file name survive a power cut (best effort)."""
        try:
            fd = os.open(self.directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _adopt(self, path: Path, size: int) -> _Segment | None:
        """Give a leftover ``.open`` segment its final name. An empty one is removed. One that
        cannot be read, or holds no whole record, is kept under ``<name>.unreadable`` -- never
        deleted -- and ``None`` is returned."""
        if size == 0:
            os.unlink(path)
            return None
        ids, stamps = [], []
        try:
            for record, _size in self._read(path, missing_ok=False):
                ids.append(record["id"])
                stamps.append(record["ts"])
        except OSError:
            ids = []
        if not ids:
            os.rename(path, path.with_name(path.name + ".unreadable"))
            self.skipped_files += 1
            return None
        return self._rename_closed(_Segment(path, min(ids), max(ids), min(stamps), max(stamps), len(ids), size))

    def _rename_closed(self, segment: _Segment) -> _Segment:
        min_ms = max(0, math.floor(segment.min_ts * 1000))
        max_ms = max(0, math.ceil(segment.max_ts * 1000))
        name = f"seg-{segment.first_id:016d}-{segment.last_id:016d}-{min_ms}-{max_ms}-{segment.count}.jsonl"
        target = self.directory / name
        os.rename(segment.path, target)
        return _Segment(target, segment.first_id, segment.last_id, min_ms / 1000.0, max_ms / 1000.0,
                        segment.count, segment.size)

    # ------------------------------------------------------------ write
    def append(self, batch: list[dict]) -> int:
        """Store ``batch`` in order, giving each record the next id; a segment is closed as soon as
        it reaches ``segment_bytes``. Raises ``OSError`` when the disk refuses: the exception's
        ``stored`` says how many records of the batch are safely on disk (the rest can be appended
        again), and no half-written line stays behind."""
        stored = 0
        with self._lock:
            if not self.opened:
                raise OSError(errno.EBADF, "记录目录未打开")
            try:
                while stored < len(batch):
                    stored += self._append_piece(batch[stored:])
            except OSError as exc:
                exc.stored = stored
                raise
            finally:
                self._refresh()
        return stored

    def _append_piece(self, records: list[dict]) -> int:
        """Write as many of ``records`` as the open segment takes; returns how many."""
        if self._fd is None:
            self._begin_segment()
        current = self._current
        next_id, lines, stamps, size = self._next_id, [], [], current.size
        for record in records:
            line, ts = _encode(next_id, record)
            lines.append(line)
            stamps.append(ts)
            next_id += 1
            size += len(line)
            if size >= self.segment_bytes:
                break
        data = (b"\n" if self._torn else b"") + b"".join(lines)
        try:
            _write_all(self._fd, data)
            os.fsync(self._fd)
            size = os.fstat(self._fd).st_size
        except OSError:
            try:
                os.ftruncate(self._fd, current.size)       # no half-written line stays behind
                self._torn = False
            except OSError:
                self._torn = True                          # the next write starts on a fresh line
                self._next_id = next_id                    # ... and whole lines may be there: skip their ids
            raise
        self._torn = False
        if current.count == 0:
            self._sync_directory()                         # the segment's name is new
        self._current = _Segment(current.path, current.first_id, next_id - 1,
                                 min([current.min_ts, *stamps]) if current.count else min(stamps),
                                 max([current.max_ts, *stamps]) if current.count else max(stamps),
                                 current.count + len(lines), size)
        self._next_id = next_id
        if size >= self.segment_bytes:
            self._roll()
        return len(lines)

    def _begin_segment(self) -> None:
        path = self.directory / f"seg-{self._next_id:016d}.open"
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self._current = _Segment(path, self._next_id, self._next_id - 1, 0.0, 0.0, 0, 0)
        self._torn = False

    def _roll(self) -> None:
        """Give the full segment its final name. If that fails it simply stays open a while longer."""
        try:
            closed = self._rename_closed(self._current)
        except OSError:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd, self._current = None, None
        self._closed = [*self._closed, closed]
        count, size, oldest, newest = self._closed_sums
        self._closed_sums = (count + closed.count, size + closed.size,
                             closed.min_ts if oldest is None else min(oldest, closed.min_ts),
                             closed.max_ts if newest is None else max(newest, closed.max_ts))
        self._sync_directory()
        while self.max_segments is not None and len(self._closed) > self.max_segments:
            oldest = self._closed[0]
            try:
                os.unlink(oldest.path)
            except OSError:
                break
            self._closed = self._closed[1:]
            self._sum_closed()

    def plan_drop(self, need_bytes: float) -> list[dict]:
        """What :meth:`drop_oldest` would remove, oldest first, until ``need_bytes`` are covered
        (everything if the log holds less). Nothing is changed; no IO."""
        with self._lock:
            segments = [*self._closed, *([self._current] if self._current is not None and self._current.count else [])]
        plan, total = [], 0
        for segment in segments:
            if total >= need_bytes:
                break
            plan.append({"first_id": segment.first_id, "last_id": segment.last_id, "from_ts": segment.min_ts,
                         "to_ts": segment.max_ts, "records": segment.count, "bytes": segment.size})
            total += segment.size
        return plan

    def drop_oldest(self) -> dict | None:
        """Remove the oldest segment. Returns what went (ids, period, count, bytes) or ``None``
        when the log is empty. Raises ``OSError`` if the file cannot be removed."""
        with self._lock:
            if not self.opened:
                return None
            current = self._current if self._current is not None and self._current.count else None
            if self._closed:
                victim, is_current = self._closed[0], False
            elif current is not None:
                victim, is_current = current, True
            else:
                return None
            if len(self._closed) + (1 if current is not None else 0) == 1:
                self._write_floor(self._next_id - 1)       # the log becomes empty: ids must still go on
            os.unlink(victim.path)
            if is_current:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd, self._current = None, None
            else:
                self._closed = self._closed[1:]
                self._sum_closed()
            self._refresh()
            return {"first_id": victim.first_id, "last_id": victim.last_id, "from_ts": victim.min_ts,
                    "to_ts": victim.max_ts, "records": victim.count, "bytes": victim.size}

    # ------------------------------------------------------------ read
    @staticmethod
    def _read(path: Path, missing_ok: bool = True) -> Iterator[tuple[dict, int]]:
        """Whole records of one segment; a torn or foreign line is skipped. A segment removed by
        a clean-up meanwhile reads as empty; any other read error is raised (``OSError``)."""
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        for line in data.split(b"\n"):
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or not _is_number(record.get("ts")):
                continue
            record_id = record.get("id")
            if isinstance(record_id, int) and not isinstance(record_id, bool) and 0 < record_id < 1 << 62:
                yield record, len(line) + 1

    def query(self, since_ts: float | None = None, until_ts: float | None = None, after: int = 0,
              limit: int = 500, max_bytes: int = MAX_QUERY_BYTES) -> dict:
        """Records with ``since_ts <= ts < until_ts`` and ``id > after``, oldest first.

        ``more`` says there are further matches; continue with ``after=next_after``."""
        limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
        if not self._lock.acquire(timeout=LOCK_TIMEOUT_S):
            return {"records": [], "more": False, "next_after": after, "busy": True, **self._stats}
        try:
            segments = [*self._closed, *([self._current] if self._current is not None else [])]
            stats = self._stats
        finally:
            self._lock.release()
        out, size, more = [], 0, False
        for segment in segments:
            if segment.count == 0 or segment.last_id <= after:
                continue
            if since_ts is not None and segment.max_ts < since_ts:
                continue
            if until_ts is not None and segment.min_ts >= until_ts:
                continue
            for record, nbytes in self._read(segment.path):
                if record["id"] <= after:
                    continue
                if since_ts is not None and record["ts"] < since_ts:
                    continue
                if until_ts is not None and record["ts"] >= until_ts:
                    continue
                if len(out) >= limit or (out and size + nbytes > max_bytes):
                    more = True
                    break
                out.append(record)
                size += nbytes
            if more:
                break
        return {"records": out, "more": more, "next_after": out[-1]["id"] if out else after, **stats}

    def _sum_closed(self) -> None:
        """Recompute the sums of the closed segments (after opening and after a removal)."""
        closed = self._closed
        self._closed_sums = (sum(s.count for s in closed), sum(s.size for s in closed),
                             min((s.min_ts for s in closed), default=None),
                             max((s.max_ts for s in closed), default=None))

    def _refresh(self) -> None:
        """Publish the cached numbers (constant time: the closed segments are already summed)."""
        count, size, oldest, newest = self._closed_sums
        current = self._current if self._current is not None and self._current.count else None
        if current is not None:
            count, size = count + current.count, size + current.size
            oldest = current.min_ts if oldest is None else min(oldest, current.min_ts)
            newest = current.max_ts if newest is None else max(newest, current.max_ts)
        if not count:
            self._stats = _empty_stats(self._next_id - 1)
            return
        first = self._closed[0].first_id if self._closed else current.first_id
        self._stats = {"count": count, "bytes": size, "segments": len(self._closed) + (1 if current else 0),
                       "first_id": first, "last_id": self._next_id - 1, "oldest_ts": oldest, "newest_ts": newest}

    def stats(self) -> dict:
        """Cached numbers (no lock, no IO): count, bytes, ids and the period covered."""
        return dict(self._stats)
