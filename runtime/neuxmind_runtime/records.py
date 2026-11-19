"""Records kept on the device: safety events, task results, operation records (R1-09, R7-11..13).

Telemetry (``events.py``) is state and run data: a ring bounded by count and by age. The three
classes here are different: each has its own append-only log under ``<state-dir>/records/<class>/``
and is **never** evicted by count or age. They leave the device only when the disk is nearly full
(``diskguard.py``: operation records first, then task results, safety events last). A fourth log,
``cleanup``, holds one entry per clean-up.

Stop path
---------
:meth:`DeviceRecords.offer_event` is called from ``EventBus.emit`` and, like it, takes no lock and
does no IO: it appends to a bounded ``deque``. Everything that touches the disk runs in the
``nx-records`` thread. A full, hung or missing disk therefore costs records (counted, reported in
``/v1/status.records``), never a stop: records that cannot be written wait in memory and are
written when the disk answers again.

On disk
-------
Each class is one :class:`~neuxmind_runtime.recordlog.RecordLog`: a directory of JSON-lines
segments whose oldest segment can be removed with a single ``unlink`` (see ``recordlog.py``).
"""
from __future__ import annotations

import errno
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from .recordlog import (CLOSE_JOIN_S, DEFAULT_SEGMENT_BYTES, MAX_QUERY_BYTES, MAX_QUERY_LIMIT,  # noqa: F401
                        MAX_RECORD_BYTES, MIN_SEGMENT_BYTES, RecordLog, _is_number)

SAFETY, TASK, OPERATION = "safety", "task", "operation"
CLASSES = (SAFETY, TASK, OPERATION)
CLEANUP = "cleanup"                       # the disk guard's own log (one entry per clean-up)

CLEANUP_SEGMENT_BYTES = 64 << 10
CLEANUP_MAX_SEGMENTS = 8                  # the clean-up log is bounded: it grows while the disk is full
REOPEN_PERIOD_S = 10.0                    # how often an unusable log directory is tried again
MAX_BURST_KEYS = 256
MAX_OPERATOR_CHARS = 120
MAX_TARGET_CHARS = 200
LOCK_NAME = "records.lock"                # held by the one runtime that writes this records directory
DEFAULT_FULL_RATIO = 0.90                 # "the disk is nearly full" (R7-13): 90 % of the filesystem is used
MIN_FULL_RATIO, MAX_FULL_RATIO = 0.50, 0.99

# Safety events a client can cause many times a second (every command beyond a limit, every
# refused command). Within ``coalesce_s`` the first one of a kind is stored at once and the
# repeats are stored as one record with their number. Stops, resets, watchdog and guard events
# are always stored one by one.
COALESCED_TYPES = frozenset({"safety.limited", "safety.rejected", "safety.stale_rejected"})


_OPERATIONS = (
    ("POST", re.compile(r"^/v[12]/stop$"), "stop"),
    ("POST", re.compile(r"^/v[12]/reset$"), "reset"),
    ("POST", re.compile(r"^/v2/skills/install$"), "skill.install"),
    ("POST", re.compile(r"^/v2/skills/([^/]+)/run$"), "skill.run"),
    ("POST", re.compile(r"^/v2/runs/([^/]+)/cancel$"), "run.cancel"),
    ("POST", re.compile(r"^/v2/capabilities/([^/]+)/invoke$"), "capability.invoke"),
    ("POST", re.compile(r"^/v1/deploy$"), "app.deploy"),
    ("POST", re.compile(r"^/v1/start$"), "app.start"),
    ("POST", re.compile(r"^/v1/evaluate$"), "app.evaluate"),
    ("POST", re.compile(r"^/v1/models/install$"), "model.install"),
    ("DELETE", re.compile(r"^/v2/models/([^/]+)$"), "model.delete"),
    ("POST", re.compile(r"^/v2/conformance$"), "conformance.start"),
)


def classify_operation(method: str, path: str) -> tuple[str, str | None] | None:
    """``(operation, target)`` for a request that changes something on the device, else ``None``.

    Reads and pure checks (``/v1/validate``) are not operations."""
    for verb, pattern, name in _OPERATIONS:
        if verb != method:
            continue
        match = pattern.match(path)
        if match:
            return name, (unquote(match.group(1))[:MAX_TARGET_CHARS] if match.groups() else None)
    return None


def clean_operator(value) -> str | None:
    """The operator a caller names (``X-NeuxMind-Operator``): printable, at most 120 characters."""
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable()).strip()[:MAX_OPERATOR_CHARS]
    return text or None


def check_full_ratio(value) -> float:
    """The "nearly full" line as a share of the filesystem in use; ``ValueError`` outside [0.50, 0.99]."""
    if not _is_number(value) or not MIN_FULL_RATIO <= value <= MAX_FULL_RATIO:
        raise ValueError(f"磁盘将满的线须在 {MIN_FULL_RATIO:.0%} 到 {MAX_FULL_RATIO:.0%} 之间（当前 {value!r}）")
    return float(value)


@dataclass(frozen=True)
class RecordsConfig:
    """Settings of the device records (``serve --disk-full-percent`` sets ``disk_full_ratio``)."""

    segment_bytes: int = DEFAULT_SEGMENT_BYTES   # clean-up removes whole segments: this is its step
    flush_period_s: float = 0.2                  # records reach the disk this often
    pending_max: int = 50_000                    # per class, kept in memory while the disk does not answer
    coalesce_s: float = 10.0                     # merge window for COALESCED_TYPES; 0 stores every event
    disk_full_ratio: float = DEFAULT_FULL_RATIO  # "nearly full": this share of the filesystem is used

    def __post_init__(self):
        if isinstance(self.segment_bytes, bool) or not isinstance(self.segment_bytes, int) \
                or self.segment_bytes < MIN_SEGMENT_BYTES:
            raise ValueError(f"segment_bytes 须为不小于 {MIN_SEGMENT_BYTES} 的整数")
        if not _is_number(self.flush_period_s) or self.flush_period_s <= 0:
            raise ValueError("flush_period_s 须为正数")
        if isinstance(self.pending_max, bool) or not isinstance(self.pending_max, int) or self.pending_max < 1:
            raise ValueError("pending_max 须为正整数")
        if not _is_number(self.coalesce_s) or self.coalesce_s < 0:
            raise ValueError("coalesce_s 须为不小于 0 的数")
        check_full_ratio(self.disk_full_ratio)


class DeviceRecords:
    """The three record classes of one runtime, their queues and the thread that writes them."""

    def __init__(self, root: str | Path, config: RecordsConfig | None = None,
                 wall: Callable[[], float] = time.time):
        self.root = Path(root)
        self.config = config or RecordsConfig()
        self._wall = wall
        self._logs = {name: RecordLog(self.root / name, self.config.segment_bytes) for name in CLASSES}
        self._logs[CLEANUP] = RecordLog(self.root / CLEANUP, CLEANUP_SEGMENT_BYTES, CLEANUP_MAX_SEGMENTS)
        self._queues = {name: deque(maxlen=self.config.pending_max) for name in CLASSES}
        self._retry: dict[str, list] = {name: [] for name in CLASSES}      # writer only
        self._bursts: dict[tuple, dict] = {}                                # writer only
        self._open_failed: dict[str, tuple[float, str]] = {}                # name -> (monotonic, reason)
        self._flush_lock = threading.Lock()
        self._closing = threading.Event()
        self._writer: threading.Thread | None = None
        self.reopen_after_s = REOPEN_PERIOD_S
        self._shut = False
        self._lock_fd: int | None = None
        self.dropped = 0
        self.write_errors = 0
        self.last_write_error: str | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Open the logs and start the writer. Never raises: a log that cannot be opened is tried
        again later and its records wait in memory."""
        ok = True
        try:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
            os.chmod(self.root, 0o700)
        except OSError:
            pass                                           # each log reports its own failure below
        for name in (*CLASSES, CLEANUP):
            try:
                self._log(name)
            except OSError:
                ok = False
        self._writer = threading.Thread(target=self._write_loop, name="nx-records", daemon=True)
        self._writer.start()
        return ok

    def _write_loop(self) -> None:
        while not self._closing.wait(self.config.flush_period_s):
            try:
                self.flush()
            except Exception as exc:                       # the writer must survive anything
                self.write_errors += 1
                self.last_write_error = f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        """Write what is pending (if the disk answers) and close the files. Never raises."""
        self._closing.set()
        writer = self._writer
        if writer is not None and writer is not threading.current_thread():
            writer.join(CLOSE_JOIN_S)
        self._writer = None
        try:
            self.flush(final=True, timeout=CLOSE_JOIN_S)
        except Exception:
            pass
        self._shut = True
        for log in self._logs.values():
            try:
                log.close()
            except Exception:
                pass
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)                    # closing releases the lock
            except OSError:
                pass
            self._lock_fd = None

    def _own_directory(self) -> None:
        """One runtime per state directory writes the records (as for the telemetry ring): a second
        instance would rename the segments the first one is writing. Raises ``OSError`` if taken."""
        if self._lock_fd is not None:
            return
        import fcntl
        os.makedirs(self.root, mode=0o700, exist_ok=True)  # may have been unusable when start() tried
        fd = os.open(self.root / LOCK_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise OSError(errno.EAGAIN, "另一个运行时实例正在使用该状态目录的记录") from None
        self._lock_fd = fd

    def _log(self, name: str) -> RecordLog:
        """The open log of ``name``; raises ``OSError`` while its directory is unusable."""
        log = self._logs[name]
        if log.opened:
            return log
        if self._shut:
            raise OSError(errno.EBADF, "记录已关闭")
        failed = self._open_failed.get(name)
        if failed is not None and time.monotonic() - failed[0] < self.reopen_after_s:
            raise OSError(errno.EIO, failed[1])
        try:
            self._own_directory()
            log.open()
        except OSError as exc:
            self._open_failed[name] = (time.monotonic(), f"记录目录不可用：{exc}")
            raise
        self._open_failed.pop(name, None)
        return log

    # ------------------------------------------------------------ producers (lock-free, no IO)
    def _push(self, name: str, record: dict) -> None:
        queue = self._queues[name]
        if len(queue) >= self.config.pending_max:
            self.dropped += 1                              # the oldest waiting record is pushed out
        queue.append(record)

    def offer_event(self, event) -> None:
        """``EventBus`` tap: every ``safety.*`` event is a safety event record. Never raises."""
        try:
            if event["type"].startswith("safety."):
                self._push(SAFETY, event)
        except Exception:
            pass

    def task_result(self, run) -> None:
        """The final state of one run (``Runner`` calls this when a run ends). Never raises."""
        try:
            ended = run.get("ended_at")
            self._push(TASK, {
                "type": "task.result", "ts": ended if _is_number(ended) else self._wall(),
                "run_id": run.get("id"), "kind": run.get("kind"), "target": run.get("target"),
                "status": run.get("status"), "error": run.get("error"), "result": run.get("result"),
                "created_at": run.get("created_at"), "started_at": run.get("started_at"), "ended_at": ended,
                "stop_reason": run.get("stop_reason")})
        except Exception:
            pass

    def operation(self, record) -> None:
        """One operation on the device (who, what, outcome). Never raises."""
        try:
            ts = record.get("ts")
            self._push(OPERATION, {**record, "type": "operation", "ts": ts if _is_number(ts) else self._wall()})
        except Exception:
            pass

    # ------------------------------------------------------------ writer
    @staticmethod
    def _summary(burst: dict) -> dict:
        last = burst["last"]
        return {**last, "summary": True, "repeats": burst["repeats"], "from_ts": burst["start"], "to_ts": last["ts"]}

    def _coalesce(self, events: list[dict], now: float, final: bool) -> list[dict]:
        """Merge repeats of COALESCED_TYPES inside one window; summaries of windows that ended
        (all of them when ``final``) are added at the end."""
        window = self.config.coalesce_s
        if not window:
            return events
        bursts, out = dict(self._bursts), []
        for event in events:
            ts = event.get("ts")
            if event.get("type") not in COALESCED_TYPES or not _is_number(ts):
                out.append(event)
                continue
            key = (event["type"], str(event.get("capability")))
            burst = bursts.get(key)
            if burst is not None and 0 <= ts - burst["start"] < window:
                bursts[key] = {**burst, "repeats": burst["repeats"] + 1, "last": event}
                continue
            if burst is not None and burst["repeats"]:
                out.append(self._summary(burst))
            if burst is None and len(bursts) >= MAX_BURST_KEYS:
                out.append(event)                          # too many kinds at once: store it as it is
                continue
            bursts[key] = {"start": ts, "repeats": 0, "last": event}
            out.append(event)
        for key, burst in list(bursts.items()):
            if final or not 0 <= now - burst["start"] < window:
                if burst["repeats"]:
                    out.append(self._summary(burst))
                del bursts[key]
        self._bursts = bursts
        return out

    def flush(self, final: bool = False, timeout: float = -1) -> int:
        """Write waiting records now. Returns how many reached the disk (0 if another flush is
        still stuck in the disk and ``timeout`` ran out)."""
        if not self._flush_lock.acquire(timeout=timeout):
            return 0
        try:
            written, now = 0, self._wall()
            for name in list(self._open_failed):           # a directory that was unusable: try it again when due
                try:
                    self._log(name)
                except OSError:
                    pass
            for name in CLASSES:
                queue, drained = self._queues[name], []
                while queue:
                    try:
                        drained.append(queue.popleft())
                    except IndexError:
                        break
                if name == SAFETY:
                    drained = self._coalesce(drained, now, final)
                batch = [*self._retry[name], *drained]
                self._retry[name] = []
                if not batch:
                    continue
                try:
                    written += self._log(name).append(batch)
                except Exception as exc:                   # OSError in practice: keep the rest and try again
                    stored = getattr(exc, "stored", 0)
                    written += stored
                    batch = batch[stored:]
                    self.write_errors += 1
                    self.last_write_error = f"{type(exc).__name__}: {exc}"
                    overflow = len(batch) - self.config.pending_max
                    if overflow > 0:
                        self.dropped += overflow
                        batch = batch[overflow:]
                    self._retry[name] = batch
            return written
        finally:
            self._flush_lock.release()

    # ------------------------------------------------------------ reading
    def query(self, name: str, since_ts: float | None = None, until_ts: float | None = None, after: int = 0,
              limit: int = 500) -> dict:
        """One class by time range (``KeyError`` for an unknown class); see :meth:`RecordLog.query`."""
        log = self._logs[name]
        return {"class": name, **log.query(since_ts, until_ts, after, limit)}

    def drop_oldest(self, name: str) -> dict | None:
        """Disk guard: remove the oldest segment of one class."""
        return self._logs[name].drop_oldest()

    def plan_drop(self, name: str, need_bytes: float) -> list[dict]:
        """Disk guard: the oldest segments of one class that cover ``need_bytes`` (nothing is removed)."""
        return self._logs[name].plan_drop(need_bytes)

    def has_task_result(self, run_id: str, since_ts: float | None = None) -> bool:
        """Is there already a task result for this run (looked for from ``since_ts`` on)?"""
        after = 0
        while True:
            page = self._logs[TASK].query(since_ts=since_ts, after=after, limit=MAX_QUERY_LIMIT)
            if any(record.get("run_id") == run_id for record in page["records"]):
                return True
            if not page["more"]:
                return False
            after = page["next_after"]

    def log_cleanup(self, entry: dict) -> None:
        """Disk guard: store one clean-up entry now. Raises ``OSError`` when the disk refuses."""
        self._log(CLEANUP).append([entry])

    def cleanup_history(self, limit: int = 50) -> list[dict]:
        """The newest ``limit`` clean-up entries, oldest first."""
        entries, after = [], 0
        while True:
            page = self._logs[CLEANUP].query(after=after, limit=MAX_QUERY_LIMIT)
            entries = [*entries, *page["records"]][-limit:]
            after = page["next_after"]
            if not page["more"]:
                return entries

    def describe(self) -> dict:
        """For ``/v1/status``: values held in memory only -- no lock, no IO."""
        failed = {name: reason for name, (_at, reason) in list(self._open_failed.items())
                  if not self._logs[name].opened}
        skipped = sum(log.skipped_files for log in self._logs.values())
        out = {"path": str(self.root), "classes": {name: self._logs[name].stats() for name in CLASSES},
               "skipped_files": skipped,
               "pending": sum(len(q) for q in self._queues.values()) + sum(len(b) for b in self._retry.values()),
               "dropped": self.dropped, "write_errors": self.write_errors, "degraded": bool(failed),
               "coalesce_s": self.config.coalesce_s}
        if failed:
            out["degraded_reason"] = "；".join(f"{name}: {reason}" for name, reason in sorted(failed.items()))
        if self.last_write_error:
            out["last_write_error"] = self.last_write_error
        return out
