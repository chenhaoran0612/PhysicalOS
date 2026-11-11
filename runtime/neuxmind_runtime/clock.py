"""Clocks used by adapters, the safety supervisor and skills.

All "robot time" in the runtime (watchdog deadlines, skill timeouts,
``ctx.sleep``) goes through a clock owned by the adapter:

* ``RealClock(time_scale=1.0)`` — monotonic wall time. Simulated bodies may
  run faster than real time (``time_scale > 1``) so tests finish quickly.
  Real hardware adapters must use ``time_scale=1``.
* ``ManualClock`` — fully deterministic, advanced explicitly by tests.
"""
from __future__ import annotations

import threading
import time


class Clock:
    """Interface: ``now()`` in seconds, ``sleep()`` interruptible by an event."""

    time_scale: float = 1.0

    def now(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float, interrupt: threading.Event | None = None) -> bool:
        """Sleep ``seconds`` of clock time. Returns True if interrupted."""
        raise NotImplementedError

    def real_seconds(self, seconds: float) -> float:
        """Convert clock seconds to the wall seconds a waiter should block."""
        return seconds / self.time_scale


class RealClock(Clock):
    def __init__(self, time_scale: float = 1.0):
        if not time_scale > 0:
            raise ValueError("time_scale 必须大于 0")
        # (robot time at base, monotonic at base, scale): one tuple, replaced atomically.
        self._base = (0.0, time.monotonic(), float(time_scale))

    @property
    def time_scale(self) -> float:
        return self._base[2]

    @time_scale.setter
    def time_scale(self, value: float) -> None:
        self.set_time_scale(value)

    def set_time_scale(self, time_scale: float) -> None:
        """Change the speed-up without a jump in robot time (used by the conformance suite to run
        motion cases of the built-in simulations at 1x). Robot time stays continuous and monotonic."""
        if not time_scale > 0:
            raise ValueError("time_scale 必须大于 0")
        mono = time.monotonic()
        self._base = (self._now(mono), mono, float(time_scale))

    def _now(self, mono: float) -> float:
        robot, base, scale = self._base
        return robot + (mono - base) * scale

    def now(self) -> float:
        return self._now(time.monotonic())

    def sleep(self, seconds: float, interrupt: threading.Event | None = None) -> bool:
        wall = max(0.0, seconds) / self.time_scale
        if interrupt is None:
            time.sleep(wall)
            return False
        return interrupt.wait(wall)


class ManualClock(Clock):
    """Deterministic clock for unit tests. ``sleep`` advances time itself."""

    def __init__(self, start: float = 0.0):
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds

    def sleep(self, seconds: float, interrupt: threading.Event | None = None) -> bool:
        if interrupt is not None and interrupt.is_set():
            return True
        self.advance(max(0.0, seconds))
        return bool(interrupt and interrupt.is_set())

    def real_seconds(self, seconds: float) -> float:
        return 0.0
