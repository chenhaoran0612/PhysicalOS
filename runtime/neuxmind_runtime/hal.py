"""Hardware abstraction layer: the contract every robot adapter implements.

An adapter maps one physical (or simulated) body onto NeuxMind standard
capabilities (``spec/neuxmind_spec/capabilities.py``). It is the *only* code
that knows vendor SDKs, topics or serial protocols.

The runtime never calls the adapter without going through
:class:`neuxmind_runtime.bus.CapabilityBus`, which validates payloads against
the spec and routes motion through the safety supervisor. Adapters therefore
receive already-validated, already-limited arguments, and must return
outputs that match the capability's ``outputs`` fields.

Required behaviour
------------------
* ``manifest()``  returns a ``robot.adapter.v2`` dict (validated at startup).
* ``read(cap)``   for ``state`` / ``stream`` capabilities, returns outputs.
* ``invoke(cap, args)`` for ``command`` capabilities, returns outputs quickly
  (no long blocking — it is a setpoint).
* ``start_action(cap, args)`` for ``action`` capabilities, returns an
  :class:`ActionHandle` immediately; the work proceeds asynchronously.
* ``stop()`` halts all motion and cancels all actions. It must be fast,
  idempotent, thread-safe and must not wait on any lock that a long-running
  call could hold, on a network round trip, or on a model. If the body has
  a hardware e-stop, that remains the primary safety mechanism.
  **Repeated stop() calls** (round 07, Q7e-04): while the e-stop is latched the latched-state
  guard calls ``stop()`` again whenever the body may be moving; its repeated calls are at least
  0.5 s apart (robot clock, ``GuardConfig.restop_interval_s``; at most 2/s). Operator stops
  (``/v1/stop``, ``/v2/stop``) come at any time. A driver must treat **every** call as a stop:
  it must not drop / debounce ``stop()`` calls over a window longer than 0.5 s (a driver that
  ignores a ``stop()`` arriving within, say, 0.8 s of the previous one swallows the whole series
  and is not held at all -- see ``docs/specs/INTERFACES_V1.md`` §9.4). Debouncing within 0.5 s or
  less is harmless.
* **Halt channel (concurrency)**: ``stop()`` and ``invoke("base.velocity",
  {"vx": 0, "vy": 0, "wz": 0})`` must be callable from another thread *while
  another ``invoke`` is still in progress* (e.g. stuck in a serial
  retransmit), and must not wait for it. The velocity watchdog and the stop
  path use them without any lock held by ordinary commands. If the vendor
  SDK is single-threaded, route these through the vendor's independent
  e-stop / brake channel.
* ``manifest()`` must describe a fixed body. The runtime deep-copies and
  freezes the limits at start-up; if ``manifest()`` later returns something
  different, the runtime reports ``manifest_changed`` and refuses all motion
  until it is restarted.
* ``clock`` attribute: a :class:`neuxmind_runtime.clock.Clock`. It should be a high-resolution
  monotonic clock (the default :class:`RealClock`, or robot / simulation time with a resolution
  well below the guard's 20 ms monitor period and 0.1 s sampling period). A coarse clock (e.g. a
  control-cycle counter ticking every >= 20 ms) still works -- the guard never infers anything
  from two equal time stamps (round 07, Q7e-01) -- but the 0.5 s ``stop()`` spacing and the
  motion thresholds are measured on it and lose precision.
* ``stop_settle_s`` attribute (optional, round 06): if ``stop()`` is itself a controlled motion
  (legged damping / sit-down / StandDown), how many robot seconds it takes to come to rest. The
  latched-state guard ignores motion for that long after a stop (bounded to
  ``[GuardConfig.settle_s, GuardConfig.max_settle_s]``, i.e. 1–5 s by default). ``None``: 1 s.
  Round 07: preferably declared in the manifest as ``safety.latch_guard.stop_settle_s`` (with the
  other guard calibration keys, see ``neuxmind_spec.manifest.LATCH_GUARD_FIELDS``); the manifest
  and the runtime configuration (``serve --latch-guard``) take precedence over this attribute.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .clock import Clock, RealClock

RUNNING, SUCCEEDED, FAILED, CANCELLED = "running", "succeeded", "failed", "cancelled"
ACTION_STATES = (RUNNING, SUCCEEDED, FAILED, CANCELLED)


@dataclass
class ActionStatus:
    """Snapshot of an action's progress."""

    state: str = RUNNING
    progress: float = 0.0                 # 0..1, best effort
    result: dict | None = None            # outputs when state == succeeded
    error: str | None = None              # reason when failed / cancelled
    feedback: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.state != RUNNING


class ActionHandle(ABC):
    """A running long-horizon goal (navigation, arm motion, policy chunk)."""

    @abstractmethod
    def poll(self) -> ActionStatus:
        """Return the current status. Must be cheap and non-blocking."""

    @abstractmethod
    def cancel(self) -> None:
        """Request cancellation. Must be fast and idempotent."""


class RobotAdapter(ABC):
    """Base class for robot adapters. Subclass and implement the abstract methods."""

    clock: Clock = RealClock()
    stop_settle_s: float | None = None    # see "stop_settle_s" above (latched-state guard settle)

    @abstractmethod
    def manifest(self) -> dict:
        """Return the robot.adapter.v2 manifest describing this body."""

    @abstractmethod
    def read(self, cap_id: str) -> dict:
        """Return outputs of a state/stream capability."""

    @abstractmethod
    def invoke(self, cap_id: str, args: dict) -> dict:
        """Execute a command capability (non-blocking setpoint)."""

    @abstractmethod
    def start_action(self, cap_id: str, args: dict) -> ActionHandle:
        """Start an action capability and return its handle immediately."""

    @abstractmethod
    def stop(self) -> None:
        """Stop all motion immediately. Fast, idempotent, thread-safe; every call is a stop (no
        debouncing over windows > 0.5 s: the guard's repeated calls are >= 0.5 s apart). See the
        module docstring for the rules."""

    # ---- optional hooks ---------------------------------------------------
    def reset(self) -> None:
        """Called when an operator clears the latched e-stop. Default: no-op."""

    def status(self) -> dict[str, Any]:
        """Extra vendor status merged into ``/v1/status``. Default: empty."""
        return {}

    def install_model(self, request: dict) -> dict:
        """Install an on-device model package (``model.install``)."""
        raise NotImplementedError("该适配器未实现端侧模型安装")

    def close(self) -> None:
        """Release resources on shutdown."""
