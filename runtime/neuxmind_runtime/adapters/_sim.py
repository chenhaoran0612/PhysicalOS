"""Shared pieces for the simulated reference bodies.

The simulation is deliberately simple kinematics, integrated lazily: each
``read``/``invoke``/``poll`` advances the model from the last update to
``clock.now()`` in small sub-steps. With ``RealClock(time_scale=k)`` the
body runs k times faster than real time; with ``ManualClock`` tests control
time exactly. No dynamics, contacts, sensors noise or obstacles.
"""
from __future__ import annotations

import math
import threading

from ..clock import Clock, RealClock
from ..hal import ActionHandle, ActionStatus, CANCELLED, FAILED, RUNNING, SUCCEEDED, RobotAdapter

SUBSTEP_S = 0.02


def yaw_to_quat(yaw: float) -> list[float]:
    return [0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)]


def quat_to_yaw(qx: float, qy: float, qz: float, qw: float) -> float:
    return math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))


def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


class SimGoal:
    """State of one simulated action goal."""

    def __init__(self, kind: str, target, **extra):
        self.kind = kind
        self.target = target
        self.state = RUNNING
        self.progress = 0.0
        self.result: dict | None = None
        self.error: str | None = None
        self.extra = extra

    def finish(self, result: dict) -> None:
        if self.state == RUNNING:
            self.state, self.result, self.progress = SUCCEEDED, result, 1.0

    def abort(self, state: str, error: str) -> None:
        if self.state == RUNNING:
            self.state, self.error = state, error


class SimHandle(ActionHandle):
    def __init__(self, adapter: "SimAdapterBase", goal: SimGoal):
        self._adapter = adapter
        self._goal = goal

    def poll(self) -> ActionStatus:
        self._adapter.advance()
        g = self._goal
        return ActionStatus(state=g.state, progress=g.progress, result=g.result, error=g.error)

    def cancel(self) -> None:
        self._goal.abort(CANCELLED, "cancelled")


def failed_handle(adapter: "SimAdapterBase", kind: str, error: str) -> SimHandle:
    goal = SimGoal(kind, None)
    goal.abort(FAILED, error)
    return SimHandle(adapter, goal)


class SimAdapterBase(RobotAdapter):
    """Lazy integration loop shared by the simulated bodies."""

    def __init__(self, time_scale: float = 1.0, clock: Clock | None = None):
        self.clock = clock or RealClock(time_scale)
        self._lock = threading.Lock()       # integration only; held for milliseconds
        self._last = self.clock.now()
        self._t = 0.0                        # simulated seconds since start

    def advance(self) -> None:
        with self._lock:
            self._advance_locked()

    def _advance_locked(self) -> None:
        now = self.clock.now()
        dt_total = now - self._last
        self._last = now
        # Cap catch-up so a long idle period cannot stall the caller.
        dt_total = min(max(dt_total, 0.0), 600.0)
        while dt_total > 1e-12:
            dt = min(SUBSTEP_S, dt_total)
            self._step(dt)
            self._t += dt
            dt_total -= dt

    def _step(self, dt: float) -> None:
        raise NotImplementedError

    def _try_advance(self) -> None:
        """Advance if the integration lock is free; never block (stop path)."""
        if self._lock.acquire(blocking=False):
            try:
                self._advance_locked()
            finally:
                self._lock.release()
