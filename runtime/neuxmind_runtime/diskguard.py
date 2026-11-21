"""Clean-up of the device records when the disk is nearly full (R7-13).

"Nearly full" is a share of the filesystem that holds the state directory (default 90 % in use,
``serve --disk-full-percent``). When it is reached, :meth:`DiskGuard.check` removes records in a
fixed order -- operation records first, then task results, safety event records last -- and inside
a class the oldest first, whole segments, only as many as are needed to bring the disk back under
the line (plus one segment of slack, so the next check does not start again at once).

Records are only removed when that can work: if all three classes together hold less than what is
missing, something else has filled the disk, deleting the records would free next to nothing, and
they are all kept (``cleanup_can_help: false`` in ``storage.disk_low`` and ``/v1/status.storage``).

Every clean-up leaves one entry per class it touched in the ``cleanup`` log: which class, which
period, how many records and bytes, the disk before. The entry is written *before* the segments
are removed, so a crash in between cannot delete records without a trace (it can leave an entry
for segments that are still there; when a removal fails a ``storage.cleanup_incomplete`` entry
follows). ``storage.records_cleaned`` is emitted afterwards with the disk after. Crossing the line emits ``storage.disk_low``, coming back ``storage.disk_ok``; a
measurement that fails emits ``storage.check_failed`` once. Sending these on as alarms is not
done here.

Nothing in this module is called from the stop path: it runs in the ``nx-housekeeping`` thread.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable

from .records import CLEANUP, DEFAULT_FULL_RATIO, OPERATION, SAFETY, TASK, DeviceRecords, check_full_ratio

CLEANUP_ORDER = (OPERATION, TASK, SAFETY)         # R7-13: what goes first; safety event records go last
HISTORY_LIMIT = 50
MAX_ROUNDS_PER_CLASS = 4                          # planning rounds for one class in one check


class DiskGuard:
    def __init__(self, records: DeviceRecords, events, path: str | Path, *, full_ratio: float = DEFAULT_FULL_RATIO,
                 usage: Callable = shutil.disk_usage, wall: Callable[[], float] = time.time):
        """``usage(path)`` returns ``(total, used, free)`` in bytes like ``shutil.disk_usage``
        (replaceable: tests fake the disk). ``events`` is the runtime's ``EventBus``."""
        self.full_ratio = check_full_ratio(full_ratio)
        self.records = records
        self.path = Path(path)
        self.usage = usage
        self.margin_bytes = records.config.segment_bytes
        self._events = events
        self._wall = wall
        self._low = False
        self._low_since: float | None = None
        self._can_help: bool | None = None
        self._disk: dict | None = None
        self._error: str | None = None
        self._cleanup_error: str | None = None
        self._unsaved: list[dict] = []            # clean-up entries the disk has not taken yet
        try:
            history = records.cleanup_history(HISTORY_LIMIT)
        except Exception:
            history = []
        self._last: dict | None = history[-1] if history else None
        self._count = len(history)

    # ------------------------------------------------------------ measuring
    def _measure(self) -> tuple[int, int] | None:
        """``(total, free)`` or ``None`` when the reading failed or makes no sense (then nothing is cleaned)."""
        try:
            reading = self.usage(self.path)
            total, free = int(reading[0]), int(reading[2])
            if total <= 0 or free < 0 or free > total:
                raise ValueError(f"磁盘用量读数无效：total={total} free={free}")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if self._error is None:
                self._events.emit("storage.check_failed", error=message)
            self._error = message
            return None
        self._error = None
        self._disk = {"total_bytes": total, "free_bytes": free,
                      "used_percent": round(100.0 * (total - free) / total, 2), "checked_at": self._wall()}
        return total, free

    def _held(self) -> int:
        """Bytes the three classes hold now (cached numbers, no IO)."""
        classes = self.records.describe()["classes"]
        return sum(classes[name]["bytes"] for name in CLEANUP_ORDER)

    # ------------------------------------------------------------ cleaning
    def check(self) -> list[dict]:
        """Measure the disk and clean if it is nearly full and cleaning can bring it back under
        the line. Returns one entry per class that lost records (oldest class first), each with
        the disk before and after; ``[]`` when nothing was removed."""
        self._cleanup_error = None
        self._save_unsaved()
        measured = self._measure()
        if measured is None:
            return []
        total, free = measured
        used_line = self.full_ratio * total           # bytes in use at which the disk counts as nearly full
        if total - free < used_line:
            self._can_help = None
            self._set_low(False, total, free)
            return []
        goal = total - used_line                      # free bytes at the line: more than this is "enough"
        target = goal + self.margin_bytes             # what a clean-up aims for, so it does not restart at once
        self._can_help = free + self._held() >= target
        self._set_low(True, total, free)
        if not self._can_help:
            return []                                 # the records are not what fills the disk: keep them
        done = []
        for name in CLEANUP_ORDER:
            removed, before, blind = None, free, False
            for _round in range(MAX_ROUNDS_PER_CLASS):
                plan = self.records.plan_drop(name, target - free) if free <= goal else []
                if not plan:
                    break
                # write-ahead: what is about to go is on record before the first file is removed
                planned = _merge(plan)
                self._remember(self._entry(name, planned, before, total))
                gone = 0
                for _piece in plan:
                    try:
                        piece = self.records.drop_oldest(name)
                    except OSError as exc:
                        self._cleanup_error = f"{name}: {type(exc).__name__}: {exc}"
                        piece = None
                    if piece is None:
                        break
                    gone += piece["records"]
                    removed = piece if removed is None else _merge([removed, piece])
                if gone != planned["records"]:        # the entry above says more than what happened: correct it
                    self._remember({"type": "storage.cleanup_incomplete", "ts": self._wall(), "class": name,
                                    "planned_records": planned["records"], "removed_records": gone,
                                    "first_id": planned["first_id"], "error": self._cleanup_error}, count=False)
                measured = self._measure()
                if measured is None:
                    blind = True                      # never keep deleting without knowing the disk
                    break
                total, free = measured
                if self._cleanup_error:
                    break
            if removed is not None:
                result = {**self._entry(name, removed, before, total),
                          "free_bytes_after": None if blind else free, "enough": not blind and free > goal}
                self._events.emit("storage.records_cleaned",
                                  **{k: v for k, v in result.items() if k not in ("type", "ts")})
                done.append(result)
            if blind or free > goal:
                break
        if free > goal:
            self._set_low(False, total, free)
        return done

    def _entry(self, name: str, removed: dict, free_before: int, total: int) -> dict:
        return {"type": "storage.records_cleaned", "ts": self._wall(), "class": name, **removed,
                "free_bytes_before": free_before, "total_bytes": total,
                "full_percent": round(self.full_ratio * 100.0, 2)}

    def _set_low(self, low: bool, total: int, free: int) -> None:
        if low == self._low:
            return
        self._low = low
        self._low_since = self._wall() if low else None
        extra = {"cleanup_can_help": self._can_help, "records_bytes": self._held()} if low else {}
        self._events.emit("storage.disk_low" if low else "storage.disk_ok", total_bytes=total, free_bytes=free,
                          used_percent=round(100.0 * (total - free) / total, 2),
                          full_percent=round(self.full_ratio * 100.0, 2), **extra)

    def _remember(self, entry: dict, count: bool = True) -> None:
        if count:
            self._last = entry
            self._count += 1
        self._unsaved = [*self._unsaved, entry][-HISTORY_LIMIT:]
        self._save_unsaved()

    def _save_unsaved(self) -> None:
        """The clean-up log is written here. On a disk so full that even this fails the entry
        waits in memory (the removal goes ahead: it is what makes room) and is written next time."""
        while self._unsaved:
            try:
                self.records.log_cleanup(self._unsaved[0])
            except OSError as exc:
                self._cleanup_error = f"{CLEANUP}: {type(exc).__name__}: {exc}"
                return
            self._unsaved = self._unsaved[1:]

    # ------------------------------------------------------------ reading
    def history(self, limit: int = HISTORY_LIMIT) -> list[dict]:
        """The newest clean-up entries, oldest first (those not on disk yet included)."""
        saved = self.records.cleanup_history(limit)
        return [*saved, *self._unsaved][-limit:]

    def describe(self) -> dict:
        """For ``/v1/status.storage``: values held in memory only."""
        return {"low": self._low, "low_since": self._low_since, "cleanup_can_help": self._can_help,
                "full_percent": round(self.full_ratio * 100.0, 2), "disk": self._disk,
                "last_cleanup": self._last, "cleanups": self._count, "unsaved_cleanups": len(self._unsaved),
                "error": self._error, "cleanup_error": self._cleanup_error}


def _merge(pieces: list[dict]) -> dict:
    """One description for several removed segments of the same class (oldest first)."""
    return {"first_id": pieces[0]["first_id"], "last_id": pieces[-1]["last_id"],
            "from_ts": min(p["from_ts"] for p in pieces), "to_ts": max(p["to_ts"] for p in pieces),
            "records": sum(p["records"] for p in pieces), "bytes": sum(p["bytes"] for p in pieces)}
