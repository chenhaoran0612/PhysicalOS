"""Capability conformance suite (INTERFACES_V1 §6.1): ``conformance.report.v1``.

Checks that a body really provides the standard capabilities its manifest
declares, at three levels:

``static``
    The manifest alone: ``robot.adapter.v2`` validation, every declared
    capability known to the spec with the right major version, parameters
    known / well-typed / required ones present, limits the safety supervisor
    accepts, mandatory capabilities, ``simulated`` declared explicitly,
    ``model_runtimes`` consistent with ``model.install``, and a fixed body
    (two ``manifest()`` calls return the same thing).

``dry``
    Nothing moves. Every ``state``/``stream`` capability is read three times
    and checked against the spec and simple physical sanity rules; the
    e-stop is triggered twice (idempotent, latency < 1 s, latched, motion
    refused while latched, reset needs authorization), the stop epoch is
    probed (a call admitted before a stop is refused after the reset), a
    zero ``base.velocity`` setpoint is accepted without arming the watchdog,
    an arm is commanded to its *current* joint position, and goals the safety
    supervisor must refuse (out of limits, bad quaternion, unsupported
    posture) are checked to be refused. The only commands that can reach the
    adapter are zero-valued (``_assert_zero``); the zero setpoints and the
    reset of the suite's own latch carry ``hardware_authorized=true`` because
    the runtime requires it for any motion-class call.

``motion``
    Requires ``hardware_authorized=true``. Every motion capability makes a
    minimal movement (≈0.1 m, 0.05 rad, 10 % gripper opening) and the
    feedback is checked; action capabilities are cancelled and stopped
    mid-motion, ``base.velocity`` is left to the watchdog, and after every
    movement the body must be still within 1 s of the stop.

The suite always runs *through* an :class:`~neuxmind_runtime.core.EdgeRuntime`
(an adapter instance is wrapped in a throw-away runtime), so payloads are
spec-validated and motion goes through the safety supervisor, exactly as in
production. Targets:

* :class:`AdapterTarget` -- any ``RobotAdapter`` instance (``--adapter``);
* :class:`RuntimeTarget` -- a running runtime in this process
  (``POST /v2/conformance``);
* :class:`HttpTarget` -- a runtime over HTTP, black box (``--url --mode client``);
  checks that need the runtime's internals (stop epoch) are *blocked* there.

Every case is ``passed``, ``failed`` or ``skipped`` with a reason; a skip is
``not_applicable`` (nothing to test) or ``blocked`` (could not be tested,
e.g. the e-stop was latched before the suite started). A level is ``ok`` only
when it ran with no failed and no blocked case.

The report is **not** on-site acceptance evidence (ADR-011): it is marked
``field_acceptance: false``; a simulated body's report is
``evidence_kind: "simulation"``, never ``device_run``.

Safety of the suite itself: it refuses to start while runs are active; it
never resets a latch it did not set (an e-stop pressed by someone else
during the suite aborts it and stays latched); when a body fails to stop
in a motion case the suite aborts and leaves the e-stop latched.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import quote

from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

from . import __version__
from .errors import BadRequest, Conflict, NotAuthorized, NotFound, RuntimeApiError

SCHEMA = "conformance.report.v1"
LEVELS = ("static", "dry", "motion")
PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
NOT_APPLICABLE, BLOCKED = "not_applicable", "blocked"
TERMINAL_RUN = ("succeeded", "failed", "stopped", "unknown")
TRUST_NOTE = ("报告是自报的：在 --adapter 与 POST /v2/conformance（server）模式下，套件与适配器代码在同一进程，"
              "适配器代码可以篡改判定；任何基于适配器自身反馈的测试都无法发现“读数与动作同时伪造”或“识别出正在被测而表现不同”；"
              "hardware_estop 等声明无法验证。只测试可信的适配器代码，并以现场人工验收为准。")
DISCLAIMER = ("本报告是能力一致性测试结果，用于说明适配器实现了哪些标准能力；它不等于现场验收证据（ADR-011）。"
              "真机部署前仍须在现场确认安全后运行 motion 级别并由操作员验收。报告由适配器所在进程自报，"
              "适配器代码可以影响它，也可能识别出正在被测（见 trust）。")


@dataclass(frozen=True)
class Thresholds:
    """Pass/fail limits (wall-clock seconds unless noted). Part of the report."""

    stop_latency_s: float = 1.0          # stop call returns AND the body is still
    cancel_latency_s: float = 1.0        # action cancel → body still
    read_latency_s: float = 1.0          # one state/stream read
    command_latency_s: float = 1.0       # one command (setpoint) round trip
    watchdog_margin_s: float = 0.5       # watchdog zeroes within timeout_s + margin
    run_settle_s: float = 2.0            # a stopped/cancelled run becomes terminal
    move_detect_s: float = 3.0           # motion must be visible in feedback within this
    action_timeout_s: float = 30.0       # a minimal action goal must finish within this
    warmup_s: float = 5.0                # wait for system.health ok before dynamic levels
    still_hold_s: float = 2.0            # after every stop/cancel: stay still this long (Q4-07)
    reset_hold_s: float = 1.0            # after every reset of the suite's own latch
    final_hold_s: float = 2.0            # at the end of the suite
    velocity_tolerance: float = 0.5      # measured peak speed within ±50 % of the command (Q4-06)
    nav_interrupt_m: float = 0.2         # navigation goal of the cancel/stop cases (absolute cap)
    joint_interrupt_rad: float = 0.1     # joint goal of the cancel/stop cases (absolute cap)
    still_velocity: float = 0.01         # m/s, rad/s: "still"
    still_position: float = 1e-3         # m / rad between two reads 30 ms apart
    base_step_m: float = 0.1             # minimal translation
    base_speed: float = 0.1              # m/s cap for the minimal velocity move
    joint_step_rad: float = 0.05         # minimal joint move
    joint_tolerance_rad: float = 2e-3    # feedback vs. target
    gripper_step: float = 0.1            # minimal opening change (ratio)
    gripper_tolerance: float = 0.02
    turn_rate: float = 0.2               # rad/s cap for the minimal wz move (R4-04)
    cross_axis_tolerance: float = 0.2    # off-axis displacement/speed ≤ 0.2 × commanded axis (R4-04)
    adapter_call_timeout_s: float = 5.0  # any single target call by the suite (R4-05)
    reset_call_timeout_s: float = 15.0   # reset (a legged body may stand up)
    restop_hold_s: float = 5.0           # after an abort: still this long, re-stopping on motion (R4-02, R4b-04)
    restop_deadline_s: float = 12.0      # ... but at most this long in total
    restop_interval_s: float = 0.3       # minimum spacing of repeated stops
    restop_max: int = 20                 # and at most this many
    dry_displacement_m: float = 0.01     # dry: base displacement vs. the start of dry that aborts (R4b-02)
    dry_displacement_rad: float = 0.01   # dry: joint / yaw displacement that aborts (R4b-02)
    arm_envelope_rad: float = 0.02       # motion: any joint outside [start, target] ± this -> stop + abort


THRESHOLDS = Thresholds()


GUARD_POLL_S = 0.25          # how often the post-abort check reads the runtime guard's counters


class CallError(Exception):
    """A target call was refused (HTTP error / RuntimeApiError)."""

    def __init__(self, message: str, code: str = "error", status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


class CallTimeout(Exception):
    """A target call did not return within its timeout (R4-05). The call's thread is abandoned;
    ``done`` is set when it finally returns (R4b-01: the suite reports calls still in flight)."""

    def __init__(self, message: str, label: str = "", done: threading.Event | None = None):
        super().__init__(message)
        self.label, self.done = label, done


# Target calls that may reach the adapter (or the network) and are therefore bounded (R4-05).
_TIMED_CALLS = frozenset({"read", "command", "start_action", "run", "cancel", "reset", "stop", "restop",
                          "stale_call", "active_runs", "installed_models"})


def _call_with_timeout(fn: Callable, args: tuple, kwargs: dict, limit: float, label: str) -> Any:
    box: dict = {}
    done = threading.Event()

    def work():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:          # re-raised in the caller's thread
            box["error"] = exc
        finally:
            done.set()
    threading.Thread(target=work, name="nx-cf-call", daemon=True).start()
    if not done.wait(limit):
        raise CallTimeout(f"调用 {label} 超过 {limit:g} s 未返回", label, done)
    if "error" in box:
        raise box["error"]
    return box["value"]


class _TimedTarget:
    """Delegates to a :class:`Target`; every call in ``_TIMED_CALLS`` runs on its own thread and
    raises :class:`CallTimeout` after the limit, so a hung adapter cannot hang the suite (and, in
    server mode, hold the motion lease) indefinitely. A hung call never blocks a later ``stop``:
    each call has its own thread, and the runtime's stop path takes no lock."""

    def __init__(self, target: "Target", limit: float, reset_limit: float, on_timeout: Callable[[CallTimeout], None]):
        self._target, self._limit, self._reset_limit, self._on_timeout = target, limit, reset_limit, on_timeout

    @property
    def wrapped(self) -> "Target":
        return self._target

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if name not in _TIMED_CALLS or not callable(attr):
            return attr
        limit = self._reset_limit if name == "reset" else self._limit

        def call(*args, **kwargs):
            label = f"{name}({args[0]!r})" if args and isinstance(args[0], str) else f"{name}()"
            try:
                return _call_with_timeout(attr, args, kwargs, limit, label)
            except CallTimeout as exc:
                self._on_timeout(exc)
                raise
        return call


class _Fail(Exception):
    pass


class _Skip(Exception):
    def __init__(self, reason: str, kind: str = NOT_APPLICABLE):
        super().__init__(reason)
        self.kind = kind


class _Abort(Exception):
    """Stop the suite: every remaining case is blocked with this reason."""

    def __init__(self, reason: str, keep_latched: bool = True):
        super().__init__(reason)
        self.keep_latched = keep_latched


def _check(condition: bool, reason: str) -> None:
    if not condition:
        raise _Fail(reason)


def _assert_zero(args: dict) -> dict:
    """Dry level: a velocity setpoint must be exactly zero before it is sent."""
    for key in ("vx", "vy", "wz"):
        if key in args and args[key] != 0.0:
            raise AssertionError(f"dry 级别只允许零速度，{key}={args[key]}")
    return args


def manifest_digest(manifest_v2: dict) -> str:
    """Digest binding a report to one manifest: sha256 of the *validated* robot.adapter.v2
    (``validate_manifest_v2``) serialised as ``json.dumps(m, sort_keys=True, ensure_ascii=False)``,
    UTF-8. Identical to the cloud's ``catalog.manifest_digest`` (fleet ``robots.manifest_digest``)."""
    data = json.dumps(manifest_v2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def manifest_v1_digest(manifest_v2: dict) -> str:
    """The desktop's digest (brainos ``digest`` of ``manifest_v1_view``, as /v1/evaluate's
    ``robot_manifest_digest``): sha256 of ``json.dumps(v1, sort_keys=True)`` (ASCII-escaped)."""
    v1 = spec_manifest.manifest_v1_view(manifest_v2)
    return hashlib.sha256(json.dumps(v1, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _round(value: Any, digits: int = 4) -> Any:
    if isinstance(value, float):
        return round(value, digits) if math.isfinite(value) else None
    if isinstance(value, list):
        return [_round(v, digits) for v in value[:16]]
    if isinstance(value, dict):
        return {k: _round(v, digits) for k, v in list(value.items())[:32]}
    if isinstance(value, str):
        return value[:300]
    return value


def _yaw(pose: list) -> float:
    qx, qy, qz, qw = pose[3:7]
    return math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))


def _quat_norm(q: list) -> float:
    return math.sqrt(sum(v * v for v in q))


# ======================================================================= targets
class Target:
    """What the suite drives. Subclasses map these calls onto a runtime."""

    kind = "abstract"
    in_process = False

    def manifest(self) -> dict:
        raise NotImplementedError

    def raw_manifests(self) -> list:
        """Two consecutive manifest reads (fixed-body check); items may be exceptions."""
        out = []
        for _ in range(2):
            try:
                out.append(self.manifest())
            except Exception as exc:          # reported by the static level
                out.append(exc)
        return out

    def describe(self) -> dict:
        return {"kind": self.kind}

    # dynamic calls (dry / motion)
    def read(self, cap: str) -> dict: raise NotImplementedError
    def command(self, cap: str, args: dict, authorized: bool) -> dict: raise NotImplementedError
    def start_action(self, cap: str, args: dict, authorized: bool, timeout_s: float | None = None) -> str:
        raise NotImplementedError
    def run(self, run_id: str) -> dict: raise NotImplementedError
    def cancel(self, run_id: str) -> dict: raise NotImplementedError
    def stop(self, reason: str) -> dict: raise NotImplementedError

    def restop(self, reason: str, counted: bool = False) -> dict:
        """Stop again after motion was observed while (possibly) already latched (R4-02). Never
        resets anything. ``counted`` (R4b-01): the stop must be a counted emergency stop (it may
        race an adapter call still in flight, e.g. a timed-out reset). Default: the ordinary stop."""
        return self.stop(reason)
    def reset(self, authorized: bool = True) -> dict: raise NotImplementedError
    def latched(self) -> bool: raise NotImplementedError
    def latch_reason(self) -> str | None: raise NotImplementedError
    def active_runs(self) -> list: raise NotImplementedError

    def stop_count(self) -> int | None:
        """The runtime's emergency-stop counter (never decreases); None = not reported.
        The suite tells its own stops from anyone else's with it (Q4-01)."""
        return None

    def epoch(self) -> int | None:
        return None

    def latched_motion(self) -> dict | None:
        """The runtime's latched-state guard counters (``/v1/status.latched_motion``, round 05);
        ``None`` = not reported. The guard may re-stop a body faster than the suite samples it, so
        the suite counts the guard's observations as motion too."""
        return None

    def stale_call(self, cap: str, args: dict, epoch: int) -> dict | None:
        """Send ``cap`` carrying a stop epoch recorded *before* a stop + reset (in-process only).
        ``None`` = not possible on this target (black box)."""
        return None

    # dry-level adapter motion guard (in-process targets only)
    guard_supported = False

    def install_dry_guard(self, check: Callable[[str, dict], str | None]) -> None:
        raise NotImplementedError

    def remove_dry_guard(self) -> None:
        pass

    def trusted_simulation(self):
        """The runtime's own simulation adapter (exact built-in class) or None."""
        return None

    def watchdog_armed(self) -> bool | None:
        return None

    def time_scale(self) -> float:
        """Robot-clock speed-up of a simulated body (1.0 for real hardware)."""
        return 1.0

    def installed_models(self) -> list | None:
        """sha256 of runtime-installed models, or None when the target cannot tell."""
        return None

    def emit(self, type_: str, **data: Any) -> None:
        pass

    def close(self) -> None:
        pass


class RuntimeTarget(Target):
    """A live :class:`EdgeRuntime` in this process (also used for ``POST /v2/conformance``)."""

    kind = "runtime"
    in_process = True

    guard_supported = True

    def __init__(self, runtime, kind: str | None = None, lease: str | None = None):
        self.runtime = runtime
        self.lease = lease            # motion lease token while the suite runs on a live runtime (Q4-15)
        if kind:
            self.kind = kind
        self._guarded = None

    def _call(self, fn):
        try:
            return fn()
        except RuntimeApiError as exc:
            raise CallError(str(exc), exc.code, exc.status) from None

    @staticmethod
    def _rid() -> str:
        return "conformance-" + uuid.uuid4().hex

    def manifest(self) -> dict:
        return copy.deepcopy(self.runtime.manifest)

    def raw_manifests(self) -> list:
        """The adapter's own manifest (twice) plus what the runtime adds natively (model.install)."""
        out = []
        for _ in range(2):
            try:
                raw = copy.deepcopy(self.runtime.adapter.manifest())
                native = [c for c in self.runtime.manifest["capabilities"]
                          if c["id"] in self.runtime.bus.native]
                if native:
                    raw["capabilities"] = list(raw.get("capabilities", [])) + copy.deepcopy(native)
                    raw["model_runtimes"] = list(self.runtime.manifest.get("model_runtimes", []))
                out.append(raw)
            except Exception as exc:
                out.append(exc)
        return out

    def describe(self) -> dict:
        m = self.runtime.manifest
        return {"kind": self.kind, "instance_id": m["instance_id"], "name": m["name"],
                "adapter": f"{type(self.runtime.adapter).__module__}:{type(self.runtime.adapter).__name__}"}

    def read(self, cap):
        return self._call(lambda: self.runtime.bus.read(cap))

    def _payload(self, args, authorized, timeout_s=None) -> dict:
        payload = {"request_id": self._rid(), "args": args, "hardware_authorized": authorized}
        if timeout_s is not None:
            payload["timeout_s"] = timeout_s
        if self.lease is not None:
            payload["conformance_lease"] = self.lease
        return payload

    def command(self, cap, args, authorized):
        return self._call(lambda: self.runtime.invoke(cap, self._payload(args, authorized)))

    def start_action(self, cap, args, authorized, timeout_s=None):
        return self._call(lambda: self.runtime.invoke(cap, self._payload(args, authorized, timeout_s)))["run_id"]

    def run(self, run_id):
        return self._call(lambda: self.runtime.runner.get(run_id))

    def cancel(self, run_id):
        return self._call(lambda: self.runtime.runner.cancel(run_id))

    def stop(self, reason):
        result = self.runtime.emergency_stop(reason, source="conformance")
        return {"stopped": result.get("stopped"), "latched": result.get("latched")}

    def restop(self, reason, counted=False):
        """R4-02: while latched (by anyone) the adapter is told to stop again without touching the
        latch or its reason (an operator's latch stays the operator's); when not latched this is an
        ordinary latching stop. ``counted`` (R4b-01: abort, or an adapter call may still be in
        flight): a counted emergency stop that keeps the existing latch reason, so a reset still
        running inside the adapter sees the stop and keeps the latch when it returns."""
        sup = self.runtime.supervisor
        if sup.latched and counted:
            result = self.runtime.emergency_stop(reason, source="conformance", keep_reason=True)
            return {"stopped": result.get("stopped"), "latched": sup.latched, "latch_kept": True, "counted": True}
        if sup.latched:
            ok = sup.halt_motion(reason)
            try:
                self.runtime.runner.stop_all(reason)
            except Exception:
                pass
            return {"stopped": ok, "latched": sup.latched, "latch_kept": True}
        return self.stop(reason)

    def reset(self, authorized=True, expect_stop_count=None):
        payload = {"hardware_authorized": authorized}
        if expect_stop_count is not None:
            payload["expect_stop_count"] = expect_stop_count
        if self.lease is not None:
            payload["conformance_lease"] = self.lease
        return self._call(lambda: self.runtime.reset(payload))

    def stop_count(self):
        return self.runtime.supervisor.stop_count

    def epoch(self):
        return self.runtime.supervisor.epoch

    def latched_motion(self):
        guard = getattr(self.runtime, "latch_guard", None)
        return guard.describe() if guard is not None else None

    def latched(self):
        return self.runtime.supervisor.latched

    def latch_reason(self):
        return self.runtime.supervisor.latch_reason

    def active_runs(self):
        return [r.id for r in self.runtime.runner.active_runs()]

    def stale_call(self, cap, args, epoch):
        marker = self.runtime.events.emit("conformance.stale_probe", capability=cap, epoch=epoch)["seq"]
        try:
            if spec_caps.get(cap).kind == "command":
                self.runtime.bus.invoke(cap, args, True, epoch=epoch)
            else:
                handle = self.runtime.bus.start_action(cap, args, True, epoch=epoch)
                handle.cancel()
            code = None
        except RuntimeApiError as exc:
            code = exc.code
        stale = any(e.get("type") == "safety.stale_rejected" for e in self.runtime.events.since(marker))
        return {"rejected_code": code, "stale_event": stale, "epoch_before": epoch,
                "epoch_after": self.runtime.supervisor.epoch}

    def install_dry_guard(self, check):
        """Shadow the adapter's ``invoke``/``start_action`` with a check that refuses (raises) any
        motion call that is not provably zero-displacement. ``stop()`` and ``read()`` are untouched,
        so the stop path and the watchdog's zero velocity keep working."""
        adapter = self.runtime.adapter
        if self._guarded is not None:
            return
        orig_invoke, orig_start = adapter.invoke, adapter.start_action

        def invoke(cap_id, args):
            problem = check(cap_id, args)
            if problem:
                raise RuntimeError("dry 级适配器运动调用守卫拒绝下发：" + problem)
            return orig_invoke(cap_id, args)

        def start_action(cap_id, args):
            problem = check(cap_id, args)
            if problem:
                raise RuntimeError("dry 级适配器运动调用守卫拒绝下发：" + problem)
            return orig_start(cap_id, args)

        adapter.invoke, adapter.start_action = invoke, start_action
        self._guarded = adapter

    def remove_dry_guard(self):
        adapter, self._guarded = self._guarded, None
        if adapter is not None:
            for name in ("invoke", "start_action"):
                adapter.__dict__.pop(name, None)

    def trusted_simulation(self):
        from .adapters.sim_arm import SimArm
        from .adapters.sim_quadruped import SimQuadruped
        adapter = self.runtime.adapter
        if type(adapter) in (SimArm, SimQuadruped) and self.runtime.manifest.get("simulated") is True:
            return adapter
        return None

    def watchdog_armed(self):
        return self.runtime.supervisor._velocity_deadline is not None

    def time_scale(self):
        scale = getattr(self.runtime.bus.clock, "time_scale", 1.0)
        return float(scale) if isinstance(scale, (int, float)) and scale > 0 else 1.0

    def installed_models(self):
        if self.runtime.models is None:
            return None
        return [m["sha256"] for m in self.runtime.models.list()]

    def emit(self, type_, **data):
        if self.runtime is not None:
            self.runtime.events.emit(type_, **data)


class AdapterTarget(RuntimeTarget):
    """Any ``RobotAdapter`` instance, wrapped in a throw-away runtime (temp state, no skills).

    The static level only calls ``adapter.manifest()``; the runtime is built
    when a dynamic level needs it (an invalid manifest cannot start one).
    ``close()`` closes the runtime and therefore the adapter."""

    kind = "adapter"

    def __init__(self, adapter):
        self.adapter = adapter
        self.runtime = None
        self.lease = None
        self._guarded = None
        self._tmp: tempfile.TemporaryDirectory | None = None

    def manifest(self):
        if self.runtime is not None:
            return copy.deepcopy(self.runtime.manifest)
        return copy.deepcopy(self.adapter.manifest())

    def raw_manifests(self):
        out = []
        for _ in range(2):
            try:
                out.append(copy.deepcopy(self.adapter.manifest()))
            except Exception as exc:
                out.append(exc)
        return out

    def describe(self):
        out = {"kind": self.kind, "adapter": f"{type(self.adapter).__module__}:{type(self.adapter).__name__}"}
        try:
            m = self.adapter.manifest()
            out.update(instance_id=m.get("instance_id"), name=m.get("name"))
        except Exception:
            pass
        return out

    def start_runtime(self) -> None:
        if self.runtime is not None:
            return
        from .core import EdgeRuntime
        self._tmp = tempfile.TemporaryDirectory(prefix="nx-rt-")
        # No skills run here, so no skill sandbox is needed; telemetry stays in memory.
        self.runtime = EdgeRuntime(self.adapter, self._tmp.name, sandbox="inprocess", telemetry_capacity=None)

    def close(self):
        try:
            if self.runtime is not None:
                self.runtime.runner.stop_all("conformance finished")
                self.runtime.close()
            else:
                self.adapter.close()
        finally:
            if self._tmp is not None:
                self._tmp.cleanup()


class HttpTarget(Target):
    """A runtime reached over its public HTTP API (black box). No proxy is used."""

    kind = "http"

    def __init__(self, url: str, token: str, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read() or b"{}")
            except ValueError:
                body = {}
            raise CallError(body.get("error") or f"HTTP {exc.code}", body.get("code") or "http_error",
                            exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise CallError(f"无法连接运行时：{exc}", "unreachable") from None

    @staticmethod
    def _rid() -> str:
        return "conformance-" + uuid.uuid4().hex

    def manifest(self):
        return self.request("GET", "/v2/manifest")

    def describe(self):
        out = {"kind": self.kind, "url": self.url}
        try:
            m = self.manifest()
            out.update(instance_id=m.get("instance_id"), name=m.get("name"))
        except CallError:
            pass
        return out

    def read(self, cap):
        out = self.request("GET", "/v2/state?ids=" + quote(cap))
        if cap not in out:
            raise CallError(f"/v2/state 未返回 {cap}", "missing")
        return out[cap]

    def command(self, cap, args, authorized):
        return self.request("POST", f"/v2/capabilities/{quote(cap)}/invoke",
                            {"request_id": self._rid(), "args": args, "hardware_authorized": authorized})

    def start_action(self, cap, args, authorized, timeout_s=None):
        payload = {"request_id": self._rid(), "args": args, "hardware_authorized": authorized}
        if timeout_s is not None:
            payload["timeout_s"] = timeout_s
        return self.request("POST", f"/v2/capabilities/{quote(cap)}/invoke", payload)["run_id"]

    def run(self, run_id):
        return self.request("GET", "/v2/runs/" + quote(run_id))

    def cancel(self, run_id):
        return self.request("POST", f"/v2/runs/{quote(run_id)}/cancel", {})

    def stop(self, reason):
        return self.request("POST", "/v2/stop", {"reason": reason})

    def restop(self, reason, counted=False):
        """R4b-08: black box -- every stop is counted; ``keep_reason`` keeps an existing latch reason
        (the operator's). A runtime that ignores the field overwrites it (old behaviour)."""
        return self.request("POST", "/v2/stop", {"reason": reason, "keep_reason": True})

    def reset(self, authorized=True, expect_stop_count=None):
        payload = {"request_id": self._rid(), "hardware_authorized": authorized}
        if expect_stop_count is not None:
            payload["expect_stop_count"] = expect_stop_count
        return self.request("POST", "/v2/reset", payload)

    def stop_count(self):
        value = self._status().get("stop_count")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def epoch(self):
        return self._status().get("stop_epoch")

    def latched_motion(self):
        value = self._status().get("latched_motion")
        return value if isinstance(value, dict) else None

    def _status(self):
        return self.request("GET", "/v1/status")

    def latched(self):
        return bool(self._status().get("estop_latched"))

    def latch_reason(self):
        return self._status().get("latch_reason")

    def active_runs(self):
        return list(self._status().get("active_runs") or [])

    def time_scale(self):
        # Reported by the adapter itself: recorded, never trusted (Q4-05).
        scale = (self._status().get("adapter") or {}).get("time_scale", 1.0)
        return float(scale) if isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale > 0 else 1.0

    def installed_models(self):
        try:
            return [m["sha256"] for m in self.request("GET", "/v2/models").get("models", [])]
        except CallError:
            return None


# ======================================================================= suite
@dataclass
class _Case:
    capability: str
    level: str
    name: str
    title: str
    status: str = SKIPPED
    reason: str = ""
    skip_kind: str | None = None
    evidence: dict | None = None
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        out = {"id": f"{self.level}.{self.capability}.{self.name}", "capability": self.capability,
               "level": self.level, "name": self.name, "title": self.title, "status": self.status,
               "reason": self.reason, "evidence": _round(self.evidence or {}), "duration_s": round(self.duration_s, 4)}
        if self.status == SKIPPED:
            out["skip_kind"] = self.skip_kind
        return out


def _declared_joint_names(manifest: dict) -> list | None:
    """``arm.joint_position.params.joint_names`` when validly declared (unique strings, len = dof)."""
    for c in manifest.get("capabilities", []):
        if isinstance(c, dict) and c.get("id") == "arm.joint_position":
            params = c.get("params") or {}
            names, dof = params.get("joint_names"), params.get("dof")
            if (isinstance(names, list) and names and len(names) == dof and len(set(names)) == len(names)
                    and all(isinstance(n, str) and n for n in names)):
                return list(names)
    return None


def _map_joint_state(state: dict, names: list) -> list:
    """Positions of ``names`` (command order) taken from a ``joint.state`` sample by name."""
    got = state.get("names")
    pos = state.get("position")
    if not isinstance(got, list) or not isinstance(pos, list) or len(got) != len(pos) or len(set(got)) != len(got):
        raise ValueError(f"joint.state 关节名/位置不一致：{got}")
    index = {n: i for i, n in enumerate(got)}
    missing = [n for n in names if n not in index]
    if missing:
        raise ValueError(f"缺少关节 {missing}（收到 {got}）")
    return [float(pos[index[n]]) for n in names]


def _level_index(level: str) -> int:
    if level not in LEVELS:
        raise BadRequest(f"level 须为 {'/'.join(LEVELS)} 之一", code="invalid_level")
    return LEVELS.index(level)


class Suite:
    """One conformance run against one target."""

    def __init__(self, target: Target, level: str = "static", authorized: bool = False,
                 thresholds: Thresholds = THRESHOLDS, progress: Callable[[float], None] | None = None,
                 run_id: str | None = None):
        self.level_index = _level_index(level)
        self.level = level
        if self.level_index == 2 and authorized is not True:
            raise NotAuthorized("motion 级别会让机器人运动，需要 hardware_authorized=true（现场确认）")
        self.t = thresholds
        self._raw_target = target
        self._hung: str | None = None             # first call that timed out (R4-05)
        self._inflight: list[tuple[str, threading.Event]] = []   # abandoned calls (R4b-01)
        self._dry_ref: dict | None = None         # body position at the start of dry (R4b-02)
        self.target = _TimedTarget(target, thresholds.adapter_call_timeout_s, thresholds.reset_call_timeout_s,
                                   self._on_call_timeout)
        self.authorized = authorized is True
        self.progress = progress or (lambda _f: None)
        self.run_id = run_id or "cf-" + uuid.uuid4().hex[:12]
        self.cases: list[_Case] = []
        self.manifest: dict | None = None
        self.caps: dict[str, dict] = {}
        self.prelatched = False
        self.aborted: str | None = None
        self.final_latched: bool | None = None
        # Latch ownership (Q4-01): the runtime's stop counter is the only proof. ``_expected_stops``
        # is the count the suite expects (start + its own stops); any other value at any check =
        # a stop from someone else -> abort, stay latched, never reset again.
        self._expected_stops: int | None = None
        self._own_reason: str | None = None
        self._own_latch = False
        self._planned = 1
        self.warmup_s: float | None = None
        self.claimed_time_scale = 1.0
        self.executed_time_scale = 1.0
        self.time_scale_rebased = False      # a trusted built-in simulation was run at 1x
        self.guard_violations: list[str] = []
        self.guard_active = False
        self.restops = 0                          # stops issued after motion was observed (R4-02)
        self.final_motion: dict | None = None     # what was observed at the end (R4-02)
        self._probe_block: _Skip | None = None

    # ------------------------------------------------------------ recording
    def _record(self, capability: str, level: str, name: str, title: str, fn: Callable[[dict], Any]) -> _Case:
        case = _Case(capability, level, name, title)
        evidence: dict = {}
        started = time.monotonic()
        violations_before = len(self.guard_violations)
        if self.aborted:
            case.status, case.reason, case.skip_kind = SKIPPED, self.aborted, BLOCKED
        else:
            try:
                if level != "static":
                    self._check_external()
                try:
                    fn(evidence)
                finally:
                    if level == "dry" and self._dry_ref is not None and not self.aborted:
                        self._dry_displacement_check(evidence)
                if level != "static":
                    self._check_external()
                case.status = PASSED
            except _Fail as exc:
                case.status, case.reason = FAILED, str(exc)
            except _Skip as exc:
                case.status, case.reason, case.skip_kind = SKIPPED, str(exc), exc.kind
            except _Abort as exc:
                case.status, case.reason = FAILED, str(exc)
                self._abort(str(exc))
            except CallTimeout as exc:            # R4-05: a hung adapter call fails the case and aborts
                case.status, case.reason = FAILED, f"适配器调用超时：{exc}"
                self._abort(f"用例 {name} 中{exc}；已发出停止并保持锁存，不再调用该适配器做后续测试")
            except CallError as exc:
                case.status, case.reason = FAILED, f"调用被拒绝（{exc.code}）：{exc}"
            except Exception as exc:
                case.status, case.reason = FAILED, f"测试异常：{type(exc).__name__}: {exc}"
                evidence["traceback"] = traceback.format_exc(limit=4)[-1500:]
            if len(self.guard_violations) > violations_before:
                case.status = FAILED
                case.reason = "套件缺陷：dry 级试图向适配器下发非零位移的运动调用（已被守卫拒绝）：" + \
                    "；".join(self.guard_violations[violations_before:])[:800]
            if case.status == FAILED and not self.aborted and level != "static":
                self._after_failure(case, level)
        case.evidence = evidence
        case.duration_s = time.monotonic() - started
        self.cases.append(case)
        self.progress(min(0.99, len(self.cases) / max(1, self._planned)))
        return case

    def _after_failure(self, case: _Case, level: str) -> None:
        """Q4-03: in motion every failure (judgement, adapter error, timeout) stops the body, keeps
        the latch and aborts; in dry an error with something still running does the same."""
        if level == "motion":
            self._abort(f"motion 用例 {case.name} 未通过（{case.reason[:200]}）；已发出停止并保持锁存")
            return
        try:
            active = self.target.active_runs()
        except Exception:
            active = ["unknown"]
        if active:
            self._abort(f"dry 用例 {case.name} 失败时仍有运行未结束（{active}）；已发出停止并保持锁存")

    def _abort(self, reason: str) -> None:
        """Stop -- always, latched or not (R4-02: a latch is not proof the body stopped) --, keep the
        latch, block every later case. Never resets afterwards. Whether the body really ended up
        still is verified (with repeated stops) at the end of the run: ``final_motion``."""
        if self.aborted:
            return
        self.aborted = f"一致性测试已中止：{reason}"
        self._restop("abort")

    def _on_call_timeout(self, exc: CallTimeout) -> None:
        if self._hung is None:
            self._hung = str(exc)
        if exc.done is not None:
            self._inflight.append((exc.label, exc.done))

    def _calls_in_flight(self, mutating_only: bool = False) -> list[str]:
        """Timed-out calls that have not returned yet. ``mutating_only``: only those whose late
        return could move the body (reset / command / start_action ...), not feedback reads."""
        return [label for label, done in self._inflight if not done.is_set()
                and not (mutating_only and label.split("(")[0] in ("read", "run", "active_runs", "installed_models"))]

    def _read_hung(self) -> bool:
        return any(label.split("(")[0] == "read" for label, _ in self._inflight)

    def _restop(self, label: str, counted: bool = False) -> bool:
        """One stop after motion was observed or on abort; never raises. On abort, and whenever an
        adapter call timed out (it may still be running inside the adapter, e.g. ``reset()``), the
        stop is a counted emergency stop that keeps the existing latch reason (R4b-01)."""
        self.restops += 1
        counted = counted or label == "abort" or self._hung is not None
        try:
            result = self.target.restop(f"conformance:{self.run_id}:{label}", counted=counted)
            return result.get("stopped") is True
        except Exception:
            return False

    def _settle_after_abort(self) -> dict:
        """R4-02: after an abort, the body must be seen still for ``restop_hold_s``; any motion
        seen meanwhile is answered with another stop (spaced, bounded in count and time). The
        result is reported as-is -- never "stopped" when motion was the last thing observed."""
        out = {"checked": False, "still": None, "restops": 0, "motion_seen": 0, "held_still_s": 0.0}
        if self._hung and self._read_hung():
            out["note"] = f"适配器调用曾超时（{self._hung}），无法可靠读取反馈：未能核对本体是否静止"
            for _ in range(2):
                out["restops"] += 1
                self._restop("final-unverified")
            return out
        if self._hung:
            # R4b-01: the abort stop was counted, so a timed-out reset still inside the adapter keeps
            # the latch when it returns. Feedback still works and is observed, but the report keeps
            # the warning: an adapter call already hung.
            out["note"] = f"适配器调用曾超时（{self._hung}）"
        if not any(c in self.caps for c in ("base.odometry", "joint.state", "gripper.state")):
            out["note"] = "；".join(filter(None, [out.get("note"),
                                    "本体没有可读的运动反馈（base.odometry / joint.state / gripper.state）：无法核对是否静止"]))
            for _ in range(2 if self._hung else 0):
                out["restops"] += 1
                self._restop("final-unverified")
            return out
        out["checked"] = True
        t0 = time.monotonic()
        deadline = t0 + self.t.restop_deadline_s
        last_stop = t0
        still_since = None
        last_motion = None
        guard0 = self._guard_seen()
        guard_last, guard_checked = guard0, t0
        while True:
            now = time.monotonic()
            try:
                still = self._body_still()
                if guard_last is not None and now - guard_checked >= GUARD_POLL_S:
                    # Round 05: the runtime's latched-state guard re-stops within ~0.1 s; with lazy
                    # or fast feedback the suite may never see that motion itself.
                    guard_checked = now
                    seen = self._guard_seen()
                    if seen is not None and seen > guard_last:
                        still = False
                        out["guard_motion_seen"] = out.get("guard_motion_seen", 0) + (seen - guard_last)
                    if seen is not None:
                        guard_last = seen
            except Exception as exc:          # a read failed: cannot prove anything
                out.update(still=None, note="；".join(filter(None, [out.get("note"),
                                                       f"读取反馈失败：{type(exc).__name__}: {exc}"])))
                self._restop("final-unverified")
                out["restops"] += 1
                break
            now = time.monotonic()
            if still:
                still_since = still_since if still_since is not None else now
                # R4b-01: while an abandoned call (reset) is still in flight, keep watching (bounded
                # by the deadline): its return may restart the drive.
                if now - still_since >= self.t.restop_hold_s and not self._calls_in_flight(mutating_only=True):
                    out["still"] = True
                    break
            else:
                still_since, last_motion = None, now
                out["motion_seen"] += 1
                if now - last_stop >= self.t.restop_interval_s or out["restops"] == 0:
                    if out["restops"] < self.t.restop_max:
                        self._restop("motion-after-stop")
                        out["restops"] += 1
                        last_stop = time.monotonic()
            if now >= deadline:
                # Only "not still" when motion was the last thing seen; still for the whole hold
                # window but an abandoned call still in flight -> still, with the call reported.
                out["still"] = bool(still_since is not None and now - still_since >= self.t.restop_hold_s)
                break
            time.sleep(0.05)
        out["held_still_s"] = round(time.monotonic() - still_since, 3) if still_since is not None else 0.0
        out["observed_s"] = round(time.monotonic() - t0, 3)
        self._guard_mark = self._guard_seen()
        if last_motion is not None:
            out["last_motion_before_end_s"] = round(time.monotonic() - last_motion, 3)
        return out

    def _guard_seen(self) -> int | None:
        """``seen`` counter of the runtime's latched-state guard, or None (not reported / failed)."""
        try:
            lm = self.target.latched_motion()
        except Exception:
            return None
        value = (lm or {}).get("seen")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _final_recheck(self) -> None:
        """R4b-04: one more look right before the report is finalized. Motion -> stop again
        (counted, keeps the reason) and report it; never "still" when motion was seen last."""
        fm = self.final_motion
        if fm is None or not fm.get("checked"):
            return
        try:
            still = self._body_still()
        except Exception as exc:
            fm["final_recheck"] = f"读取失败：{type(exc).__name__}: {exc}"
            if fm.get("still") is True:
                fm["still"] = None
                fm["note"] = "；".join(filter(None, [fm.get("note"), "报告定稿前的最终核对读取失败"]))
            return
        mark, seen = getattr(self, "_guard_mark", None), self._guard_seen()
        if still and mark is not None and seen is not None and seen > mark:
            still = False                     # the runtime guard saw (and stopped) motion since the window
            fm["guard_motion_seen"] = fm.get("guard_motion_seen", 0) + (seen - mark)
        if still:
            fm["final_recheck"] = "still"
            return
        fm["final_recheck"] = "motion"
        fm["motion_seen"] = fm.get("motion_seen", 0) + 1
        self._restop("motion-before-report", counted=True)
        fm["restops"] = fm.get("restops", 0) + 1
        time.sleep(0.5)
        try:
            again = self._body_still()
        except Exception:
            again = None
        fm["still"] = False if again is not True else None
        fm["note"] = "；".join(filter(None, [fm.get("note"),
                                            "报告定稿前再次观察到运动，已再次停止" +
                                            ("，0.5 s 后静止，但无法确认不会再次运动" if again is True else "，0.5 s 后仍不能确认静止")]))

    def _blocked(self, capability: str, level: str, name: str, title: str, reason: str) -> None:
        case = _Case(capability, level, name, title, SKIPPED, reason, BLOCKED, {})
        self.cases.append(case)

    # ------------------------------------------------------------ latch handling
    def _count(self) -> int | None:
        return self.target.stop_count()

    def _check_external(self) -> None:
        """Abort when the stop counter is not what the suite's own stops explain (Q4-01)."""
        if self._expected_stops is None:
            return
        count = self._count()
        if count != self._expected_stops:
            raise _Abort(f"检测到非套件来源的停止（停止计数 {count}，套件预期 {self._expected_stops}）；"
                         "立即中止、保持锁存，不再复位")

    def _stop(self, label: str) -> tuple[dict, float]:
        """Trigger the e-stop as the suite; returns (result, call latency).

        Refuses (aborts) when the body is latched by someone else or the counter does not match;
        afterwards the counter must have moved by exactly one, otherwise another stop raced it."""
        self._check_external()
        if self.target.latched() and not self._own_latch:
            raise _Abort("急停已被其他来源锁存；一致性测试不再发出停止，也不会复位")
        reason = f"conformance:{self.run_id}:{label}"
        t0 = time.monotonic()
        result = self.target.stop(reason)
        latency = time.monotonic() - t0
        count = self._count()
        if self._expected_stops is not None:
            if count != self._expected_stops + 1:
                self._own_latch = False
                raise _Abort(f"停止计数 {count}（预期 {self._expected_stops + 1}）：与其他来源的停止交错，保持锁存")
            self._expected_stops = count
        self._own_reason, self._own_latch = reason, True
        return result, latency

    def _ensure_unlatched(self) -> None:
        """Before a case that needs motion admitted: clear only the suite's own latch, by a
        conditional reset (``expect_stop_count``), then verify the body stays still (Q4-07)."""
        self._check_external()
        if not self.target.latched():
            self._own_latch = False
            return
        if self._expected_stops is None:
            raise _Skip("运行时不报告停止计数（stop_count），无法证明锁存是套件自己造成的；不复位", BLOCKED)
        if not self._own_latch or self.target.latch_reason() != self._own_reason:
            raise _Abort(f"急停被其他来源锁存（{self.target.latch_reason()}）；一致性测试不会替操作员解除急停")
        try:
            self.target.reset(True, expect_stop_count=self._expected_stops)
        except CallError as exc:
            raise _Abort(f"条件复位被拒绝（{exc.code}）：可能有其他来源的停止；保持锁存") from None
        self._own_latch = False
        self._check_external()
        if self.target.latched():
            raise _Abort("复位后仍处于锁存状态")
        self._hold_still(self.t.reset_hold_s, "复位后")

    def _release_latch(self) -> None:
        """End of the suite: clear the suite's own latch (never anyone else's)."""
        if self.aborted:
            return
        self._ensure_unlatched()

    # ------------------------------------------------------------ stillness (Q4-07)
    def _body_still(self) -> bool:
        """Every available feedback says still: base odometry, joints, gripper opening."""
        checks = []
        if "base.odometry" in self.caps:
            checks.append(self._base_still)
        if "joint.state" in self.caps:
            checks.append(self._joints_still)
        if "gripper.state" in self.caps:
            checks.append(self._gripper_still)
        return all(fn() for fn in checks)

    def _hold_still(self, duration: float, when: str, fn: Callable[[], bool] | None = None) -> None:
        """The body must stay still for ``duration`` s (covers watchdog periods and delayed
        resumes). Motion during the window: stop again -- latched or not (R4-02) -- and abort."""
        fn = fn or self._body_still
        t0 = time.monotonic()
        # Round 06: while latched, the runtime's guard may see (and re-stop) a delayed resume
        # before this loop's own sample does -- its ``seen`` counter going up is motion too.
        guard_last, guard_checked = self._guard_seen(), t0
        while time.monotonic() - t0 < duration:
            still = fn()
            by_guard = False
            if still and guard_last is not None and time.monotonic() - guard_checked >= GUARD_POLL_S:
                guard_checked = time.monotonic()
                seen = self._guard_seen()
                if seen is not None and seen > guard_last:
                    still, by_guard = False, True
                if seen is not None:
                    guard_last = seen
            if not still:
                moved_after = time.monotonic() - t0
                self._restop("hold-violated")
                raise _Abort(f"{when} {moved_after:.2f} s 本体又动了（要求持续静止 {duration:g} s"
                             + ("；由运行时锁存守护发现并再停" if by_guard else "")
                             + "）；已再次发出停止并保持锁存")
            time.sleep(0.05)

    # ------------------------------------------------------------ dry displacement (R4b-02)
    def _position_snapshot(self) -> dict:
        """Where the body is now, from every available feedback (joints by name when names are usable)."""
        snap: dict = {}
        if "base.odometry" in self.caps:
            p = self._odom()["pose"]
            snap["base"] = (float(p[0]), float(p[1]), _yaw(p))
        if "joint.state" in self.caps:
            js = self._joints()
            names, pos = js.get("names") or [], list(js["position"])
            if isinstance(names, list) and len(set(names)) == len(names) == len(pos):
                snap["joints"] = dict(zip(names, pos))
            else:
                snap["joint_list"] = pos
        if "gripper.state" in self.caps:
            snap["gripper"] = float(self._read("gripper.state")["opening"])
        return snap

    def _displacement(self, a: dict, b: dict) -> list[str]:
        moved = []
        if "base" in a and "base" in b:
            d = math.hypot(b["base"][0] - a["base"][0], b["base"][1] - a["base"][1])
            yaw = abs(math.remainder(b["base"][2] - a["base"][2], 2 * math.pi))
            if d > self.t.dry_displacement_m or yaw > self.t.dry_displacement_rad:
                moved.append(f"底盘平移 {d:.3f} m、转向 {yaw:.3f} rad")
        ja, jb = a.get("joints"), b.get("joints")
        if ja is not None and jb is not None:
            common = [n for n in ja if n in jb]
            if len(common) != len(ja) or len(common) != len(jb):
                moved.append(f"joint.state 关节名变化（{sorted(ja)} → {sorted(jb)}），无法证明未移动")
            far = {n: round(jb[n] - ja[n], 4) for n in common if abs(jb[n] - ja[n]) > self.t.dry_displacement_rad}
            if far:
                moved.append(f"关节移动 {far} rad")
        elif "joint_list" in a or "joint_list" in b:
            la = a.get("joint_list") or list((a.get("joints") or {}).values())
            lb = b.get("joint_list") or list((b.get("joints") or {}).values())
            if len(la) != len(lb) or any(abs(x - y) > self.t.dry_displacement_rad for x, y in zip(la, lb)):
                moved.append("关节读数变化（关节名不可用，按位置比较）")
        if "gripper" in a and "gripper" in b and abs(b["gripper"] - a["gripper"]) > self.t.gripper_tolerance:
            moved.append(f"夹爪开度变化 {b['gripper'] - a['gripper']:+.3f}")
        return moved

    def _dry_displacement_check(self, ev: dict) -> None:
        """After every dry case: the body must still be where dry started. Any displacement ->
        stop, keep the latch, abort the suite (no further dry case is sent)."""
        try:
            now = self._position_snapshot()
        except CallTimeout:
            raise
        except Exception as exc:
            ev["dry_displacement_check"] = f"读取失败：{type(exc).__name__}: {exc}"
            return
        moved = self._displacement(self._dry_ref, now)
        if moved:
            ev["dry_displacement"] = moved
            raise _Abort("dry 级观察到本体位移（" + "；".join(moved)[:300] + "）：dry 级不允许任何位移；"
                         "已停止并保持锁存，不再执行后续用例")

    def _gripper_still(self) -> bool:
        a = self._read("gripper.state")["opening"]
        time.sleep(0.03)
        b = self._read("gripper.state")["opening"]
        return abs(a - b) < 1e-3

    # ------------------------------------------------------------ helpers
    def _declared(self, cap: str) -> bool:
        return cap in self.caps

    def _param(self, cap: str, name: str, default=None):
        return self.caps.get(cap, {}).get("params", {}).get(name, default)

    def _read(self, cap: str) -> dict:
        return self.target.read(cap)

    def _wait(self, predicate: Callable[[], Any], timeout: float, interval: float = 0.02):
        """Poll until ``predicate()`` is truthy. Returns (value, elapsed) or (None, elapsed)."""
        t0 = time.monotonic()
        while True:
            value = predicate()
            elapsed = time.monotonic() - t0
            if value:
                return value, elapsed
            if elapsed >= timeout:
                return None, elapsed
            time.sleep(interval)

    def _wait_run(self, run_id: str, timeout: float) -> dict:
        record, _ = self._wait(lambda: (lambda r: r if r.get("status") in TERMINAL_RUN else None)(
            self.target.run(run_id)), timeout, 0.02)
        return record or self.target.run(run_id)

    # base feedback
    def _odom(self) -> dict:
        return self._read("base.odometry")

    def _base_still(self) -> bool:
        a = self._odom()
        time.sleep(0.03)
        b = self._odom()
        speed = max(abs(v) for v in b["velocity"])
        moved = math.hypot(b["pose"][0] - a["pose"][0], b["pose"][1] - a["pose"][1])
        turned = abs(math.remainder(_yaw(b["pose"]) - _yaw(a["pose"]), 2 * math.pi))
        return speed < self.t.still_velocity and moved < self.t.still_position and turned < self.t.still_position

    def _base_moving(self, start: dict) -> bool:
        now = self._odom()
        speed = max(abs(v) for v in now["velocity"])
        moved = math.hypot(now["pose"][0] - start["pose"][0], now["pose"][1] - start["pose"][1])
        turned = abs(math.remainder(_yaw(now["pose"]) - _yaw(start["pose"]), 2 * math.pi))
        return speed > self.t.still_velocity or moved > 0.005 or turned > 0.005

    # joint feedback
    def _joints(self) -> dict:
        return self._read("joint.state")

    def _joints_still(self) -> bool:
        a = self._joints()
        time.sleep(0.03)
        b = self._joints()
        pa = dict(zip(a.get("names") or [], a["position"]))
        pb = dict(zip(b.get("names") or [], b["position"]))
        if len(pa) != len(a["position"]) or set(pa) != set(pb):
            return False                      # cannot compare joint by joint: not provably still
        delta = max((abs(pa[n] - pb[n]) for n in pa), default=0.0)   # by name, never by order (R4-01)
        speed = max((abs(v) for v in b.get("velocity") or []), default=0.0)
        return delta < self.t.still_position * 0.1 and speed < self.t.still_velocity

    # ------------------------------------------------------------ main
    def run(self) -> dict:
        started = time.time()
        t0 = time.monotonic()
        self.target.emit("conformance.started", conformance_id=self.run_id, level=self.level,
                         hardware_authorized=self.authorized)
        try:
            raw = self.target.raw_manifests()
            self._planned = 12 + 6 * len((raw[0] if isinstance(raw[0], dict) else {}).get("capabilities", []))
            static_ok = self._static(raw)
            if self.level_index >= 1:
                self._dynamic(static_ok)
        finally:
            try:
                if self.aborted and self.manifest is not None:
                    self.final_motion = self._settle_after_abort()
            except Exception as exc:
                self.final_motion = {"checked": False, "still": None, "note": f"核对失败：{type(exc).__name__}: {exc}"}
            try:
                if self.level_index >= 1 and self.manifest is not None:
                    self._final_recheck()
            except Exception as exc:
                if self.final_motion is not None:
                    self.final_motion["final_recheck"] = f"核对失败：{type(exc).__name__}: {exc}"
            inflight = self._calls_in_flight()
            if inflight and self.final_motion is not None:
                self.final_motion["calls_in_flight"] = inflight
            try:                              # read back, never assumed (R4b-01)
                self.final_latched = self.target.latched() if self.manifest is not None else None
            except Exception:
                self.final_latched = None
        report = self._report(started, time.monotonic() - t0)
        self.target.emit("conformance.finished", conformance_id=self.run_id, level=self.level,
                         static_ok=report["static_ok"], dry_ok=report["dry_ok"], motion_ok=report["motion_ok"])
        self.progress(1.0)
        return report

    # ============================================================ static
    def _static(self, raw: list) -> bool:
        first = raw[0]

        def manifest_valid(ev):
            if isinstance(first, Exception):
                raise _Fail(f"manifest() 抛出异常：{type(first).__name__}: {first}")
            _check(isinstance(first, dict), "manifest() 须返回对象")
            try:
                normalized = spec_manifest.validate_manifest_v2(copy.deepcopy(first))
            except (ValueError, TypeError, KeyError) as exc:
                raise _Fail(f"robot.adapter.v2 校验失败：{exc}") from None
            ev.update(instance_id=normalized["instance_id"], family=normalized["family"],
                      capabilities=[c["id"] for c in normalized["capabilities"]])
            self.manifest = normalized

        valid = self._record("manifest", "static", "schema", "清单通过 robot.adapter.v2 校验", manifest_valid)
        if isinstance(first, dict) and self.manifest is None:
            # Keep going on a best-effort view so every capability gets its own verdict.
            self.caps = {c.get("id"): c for c in first.get("capabilities", []) if isinstance(c, dict)}
        elif self.manifest is not None:
            self.caps = {c["id"]: c for c in self.manifest["capabilities"]}

        def json_clean(ev):
            _check(isinstance(first, dict), "清单不可用")
            try:
                json.dumps(first, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise _Fail(f"清单不能无损序列化为 JSON（不允许 NaN/Infinity/非 JSON 类型）：{exc}") from None

        def fixed_body(ev):
            second = raw[1]
            _check(not isinstance(second, Exception), f"第二次调用 manifest() 抛出异常：{second}")
            a = json.dumps(first, sort_keys=True, default=repr)
            b = json.dumps(second, sort_keys=True, default=repr)
            _check(a == b, "两次 manifest() 返回不同内容；本体清单必须固定（运行中变化会被运行时判为 manifest_changed）")

        def mandatory(ev):
            missing = [c for c in spec_caps.MANDATORY if c not in self.caps]
            ev["mandatory"] = list(spec_caps.MANDATORY)
            _check(not missing, f"缺少必备能力：{missing}")

        def simulated_flag(ev):
            _check(isinstance(first, dict), "清单不可用")
            _check("simulated" in first, "清单必须显式声明 simulated（true/false），不能靠默认值")
            _check(isinstance(first["simulated"], bool), f"simulated 须为布尔值，收到 {first['simulated']!r}")
            ev["simulated"] = first["simulated"]

        def model_runtimes(ev):
            _check(isinstance(first, dict), "清单不可用")
            top = first.get("model_runtimes", [])
            ev["model_runtimes"] = top
            if "model.install" in self.caps:
                runtimes = self._param("model.install", "runtimes")
                ev["model_install_runtimes"] = runtimes
                _check(isinstance(runtimes, list) and runtimes, "model.install 须声明非空 params.runtimes")
                _check(list(top) == list(runtimes),
                       f"顶层 model_runtimes={top} 与 model.install.params.runtimes={runtimes} 不一致")
            else:
                _check(not top, f"未声明 model.install，但顶层 model_runtimes={top} 非空（云端会据此下发端侧模型）")

        def safety_limits(ev):
            from .safety import validate_safety_params
            _check(isinstance(first, dict), "清单不可用")
            params = {cid: dict(c.get("params") or {}) for cid, c in self.caps.items()}
            try:
                validate_safety_params(params, dict(first.get("safety") or {}))
            except ValueError as exc:
                raise _Fail(f"安全监督器不接受该清单：{exc}") from None

        for name, title, fn in (("json", "清单可序列化为严格 JSON", json_clean),
                                ("fixed_body", "两次读取清单结果相同（固定本体）", fixed_body),
                                ("mandatory", "必备能力存在", mandatory),
                                ("simulated", "simulated 显式声明", simulated_flag),
                                ("model_runtimes", "model_runtimes 与 model.install 一致", model_runtimes),
                                ("safety_limits", "安全监督器接受清单限值", safety_limits)):
            self._record("manifest", "static", name, title, fn)

        for cid in self._capability_ids():
            if cid in self.caps:
                self._record(cid, "static", "declaration", "声明与能力标准一致",
                             lambda ev, cid=cid: self._static_capability(cid, ev))
            else:
                self._record(cid, "static", "declaration", "声明与能力标准一致",
                             lambda ev: (_ for _ in ()).throw(_Skip("本体未声明该能力（不支持或尚未实现）")))
        return valid.status == PASSED and not any(
            c.status == FAILED for c in self.cases if c.level == "static")

    def _capability_ids(self) -> list[str]:
        ids = list(spec_caps.CAPABILITIES)
        ids += sorted(c for c in self.caps if isinstance(c, str) and c.startswith("x."))
        ids += sorted(c for c in self.caps if isinstance(c, str) and c not in ids)   # unknown ids: failed below
        return ids

    def _static_capability(self, cid: str, ev: dict) -> None:
        entry = self.caps[cid]
        params = entry.get("params", {})
        ev["params"] = params
        if cid.startswith("x."):
            _check(cid.count(".") >= 2, "厂商扩展须命名为 x.<厂商>.<名称>")
            ev["note"] = "厂商扩展能力：运行时一律视为运动类，需现场授权"
            return
        try:
            spec = spec_caps.get(cid)
        except ValueError as exc:
            raise _Fail(str(exc)) from None
        _check(entry.get("major", 1) == spec.major, f"主版本 {entry.get('major')} 与标准 v{spec.major} 不符")
        _check(isinstance(params, dict), "params 须为对象")
        known = {f.name: f for f in spec.params}
        unknown = sorted(set(params) - set(known))
        _check(not unknown, f"未定义参数：{unknown}")
        missing = [f.name for f in spec.params if f.required and f.name not in params]
        _check(not missing, f"缺少必填参数：{missing}")
        for name, value in params.items():
            try:
                spec_caps.check_field(known[name], value, f"{cid}.params")
            except ValueError as exc:
                raise _Fail(str(exc)) from None
        if cid == "arm.joint_position":
            dof, lower, upper = params["dof"], params["lower"], params["upper"]
            _check(len(lower) == dof and len(upper) == dof, f"lower/upper 长度须等于 dof={dof}")
            _check(all(lo < hi for lo, hi in zip(lower, upper)), "每个关节须满足 lower < upper")
            js = self._param("joint.state", "dof")
            if js is not None:
                _check(js >= dof, f"joint.state.dof={js} 小于机械臂自由度 {dof}")
            if "joint_names" in params:
                names = params["joint_names"]
                _check(isinstance(names, list) and len(names) == dof and len(set(names)) == dof
                       and all(isinstance(n, str) and n for n in names),
                       f"joint_names 须为 {dof} 个互不相同的非空字符串")
                ev["joint_names"] = list(names)
            else:
                ev["note"] = "未声明 joint_names（v1.1 可选）：dry/motion 级全部机械臂与关节动作块用例将被阻断（R4-01）"
        if cid == "legged.posture":
            supported = params["supported"]
            choices = next(f for f in spec.inputs if f.name == "posture").choices
            _check(isinstance(supported, list) and supported and all(p in choices for p in supported),
                   f"supported 须为 {list(choices)} 的非空子集")
        if cid == "policy.action_chunk" and params.get("action_space") == "joint_position":
            _check("arm.joint_position" in self.caps and "joint.state" in self.caps,
                   "joint_position 动作空间需要同时声明 arm.joint_position（限位）与 joint.state（逐步限速起点）")
        if cid == "model.install":
            runtimes = params["runtimes"]
            _check(isinstance(runtimes, list) and runtimes and all(isinstance(r, str) and r for r in runtimes),
                   "runtimes 须为非空字符串列表")
        ev.update(kind=spec.kind, safety=spec.safety)

    # ============================================================ dynamic
    def _dynamic(self, static_ok: bool) -> None:
        plan = self._plan()
        if not static_ok:
            self._block_all(plan, "static 级未通过，未执行（清单无效时运行时拒绝启动）")
            return
        if isinstance(self._raw_target, AdapterTarget):
            try:
                self.target.start_runtime()
            except Exception as exc:
                self._block_all(plan, f"无法以该适配器启动运行时：{type(exc).__name__}: {exc}")
                return
        try:
            active = self.target.active_runs()
        except CallError as exc:
            self._block_all(plan, f"无法读取运行状态：{exc}")
            return
        if active:
            raise Conflict(f"运行时有运行未结束（{active}）；一致性测试需要空闲的本体", code="runs_active")
        self.warmup_s = self._warm_up()
        self.prelatched = self.target.latched()
        if self.prelatched:
            # Q4-01: a latch the suite did not set is never touched -- not even with another stop.
            self._block_all(plan, f"一致性测试开始前急停已锁存（{self.target.latch_reason()}）；"
                                  "测试不发出停止、不复位，请操作员确认现场并解锁后重跑")
            return
        self._expected_stops = self._count()
        # Q4-05: a time scale is only honoured for the runtime's own simulation classes; they are run
        # at 1x for the dynamic levels (same wall-clock behaviour as hardware). Anything else is 1x.
        self.claimed_time_scale = self.target.time_scale()
        sim = self.target.trusted_simulation()
        restore = None
        if sim is not None and abs(getattr(sim.clock, "time_scale", 1.0) - 1.0) > 1e-9 \
                and hasattr(sim.clock, "set_time_scale"):
            restore = sim.clock.time_scale
            sim.clock.set_time_scale(1.0)
            self.time_scale_rebased = True
        try:
            if self.target.guard_supported:
                self.target.install_dry_guard(self._guard_check)
                self.guard_active = True
            try:
                self._dry_ref = self._position_snapshot()
            except CallTimeout:
                raise
            except Exception:
                self._dry_ref = None          # each case's own checks still apply
            for level, cap, name, title, fn in plan:
                if level == "motion" and self.level_index < 2:
                    continue
                if level == "motion":
                    self._dry_ref = None
                if level == "motion" and self.guard_active:
                    self.target.remove_dry_guard()
                    self.guard_active = False
                if level == "motion" and any(c.status == FAILED or (c.status == SKIPPED and c.skip_kind == BLOCKED)
                                             for c in self.cases if c.level in ("static", "dry")):
                    self._blocked(cap, level, name, title, "dry 级未通过，不执行运动测试")
                    continue
                self._record(cap, level, name, title, fn)
            if self.guard_active:
                self.target.remove_dry_guard()
                self.guard_active = False
            # Q4-07: leave the body as found -- release only the suite's own latch, then prove stillness.
            final_level = "motion" if self.level_index >= 2 else "dry"
            self._record("safety.stop", final_level, "final_still", "测试结束：解除套件自己的锁存后持续静止",
                         self._final_still)
        finally:
            if self.guard_active:
                self.target.remove_dry_guard()
                self.guard_active = False
            if restore is not None:
                sim.clock.set_time_scale(restore)
        self.executed_time_scale = 1.0

    def _final_still(self, ev: dict) -> None:
        self._release_latch()
        self._hold_still(self.t.final_hold_s, "测试结束时")
        ev.update(hold_s=self.t.final_hold_s, latched=self.target.latched())
        checked = any(c in self.caps for c in ("base.odometry", "joint.state", "gripper.state"))
        self.final_motion = {"checked": checked, "still": True if checked else None, "restops": 0, "motion_seen": 0,
                             "held_still_s": self.t.final_hold_s}

    def _end_state(self) -> str | None:
        """One honest sentence about the body at the end (R4-02)."""
        fm = self.final_motion
        if fm is None:
            return None
        latched = ("急停保持锁存" if self.final_latched else
                   "急停未锁存" if self.final_latched is False else "未能读取锁存状态")
        inflight = fm.get("calls_in_flight") or []
        tail = (f"；适配器调用 {'、'.join(inflight)} 超时后仍未返回（运行时会在其返回后保持锁存并再次停止）"
                if inflight else "")
        window = "（观察窗之后才恢复的运动无法由本报告发现）"
        if not self.aborted:
            if fm.get("still") is True:
                return f"测试结束：本体在 {fm['held_still_s']:g} s 观察窗内持续静止{window}"
            if fm.get("still") is False:
                return (f"警告：测试结束时观察到运动（{fm.get('note') or ''}）：不能确认本体已停下，"
                        f"请立即使用硬件急停并检查驱动；{latched}")
            return "测试结束：" + (fm.get("note") or "本体没有可读的运动反馈，未能核对是否静止")
        if self._hung and fm.get("still") is not False:
            seen = (f"中止后在 {fm['held_still_s']:g} s 观察窗内静止{window}" if fm.get("still") is True
                    else "未能核对本体是否静止")
            return (f"警告：适配器调用曾超时（{self._hung}）；{seen}；已发出停止；请现场确认并使用硬件急停；"
                    f"{latched}{tail}")
        if fm.get("still") is True:
            extra = f"，期间观察到运动并再次发出停止 {fm['restops']} 次" if fm.get("motion_seen") else ""
            return f"中止后核对：本体在 {fm['held_still_s']:g} s 观察窗内持续静止{window}{extra}；{latched}{tail}"
        if fm.get("still") is False:
            last = fm.get("last_motion_before_end_s")
            when = f"最近一次在核对结束前 {last:.2f} s" if last is not None else "直到核对结束"
            if fm.get("guard_motion_seen"):
                when += f"；其中运行时锁存守护发现并再停 {fm['guard_motion_seen']} 次"
            return (f"警告：中止后已发出停止 {fm['restops']} 次，仍观察到运动（{when}）：不能确认本体已停下，"
                    f"请立即使用硬件急停并检查驱动；{latched}{tail}")
        return f"警告：{fm.get('note') or '未能核对本体是否静止'}；已发出停止；请现场确认并使用硬件急停；{latched}{tail}"

    def _warm_up(self) -> float:
        """A freshly started adapter may not have data yet (ROS 2 topics): wait until
        ``system.health`` reports ok, at most ``warmup_s``. Not a verdict -- the health
        case reports what it finds."""
        def healthy():
            try:
                return self._read("system.health").get("ok") is True
            except CallError:
                return False
        _ok, waited = self._wait(healthy, self.t.warmup_s, 0.05)
        return round(waited, 3)

    def _block_all(self, plan, reason: str) -> None:
        for level, cap, name, title, _fn in plan:
            if level == "motion" and self.level_index < 2:
                continue
            self._blocked(cap, level, name, title, reason)

    def _plan(self) -> list[tuple[str, str, str, str, Callable[[dict], Any]]]:
        """(level, capability, case name, title, fn) in execution order: dry first, then motion."""
        dry, motion = [], []
        for cid in self._capability_ids():
            if cid not in self.caps:
                continue
            if cid.startswith("x."):
                dry.append(("dry", cid, "vendor", "厂商扩展", lambda ev: self._na("厂商扩展能力没有标准测试；需厂商提供验证方法")))
                continue
            try:
                spec = spec_caps.get(cid)
            except ValueError:
                continue
            for name, title, fn in self._dry_cases(cid, spec):
                dry.append(("dry", cid, name, title, fn))
            for name, title, fn in self._motion_cases(cid, spec):
                motion.append(("motion", cid, name, title, fn))
        return dry + motion

    @staticmethod
    def _na(reason: str):
        raise _Skip(reason, NOT_APPLICABLE)

    def _require_unlatched_or_block(self) -> None:
        self._ensure_unlatched()

    def _require_guard(self) -> None:
        """Probes whose safety rests on the runtime refusing them are only sent when the dry guard
        stands between the runtime and the adapter (in-process). A black-box runtime that wrongly
        admitted one could move the body (Q4-16)."""
        if not self.guard_active:
            raise _Skip("黑盒模式没有 dry 级适配器运动调用守卫：不发送依赖运行时拒绝的探测目标；"
                        "请用 POST /v2/conformance（--mode server）", BLOCKED)

    # ------------------------------------------------------------ dry guard (Q4-02)
    def _guard_check(self, cap: str, args: dict) -> str | None:
        """Called (in the adapter's place) for every invoke/start_action while dry cases run.
        Returns a reason to refuse, or None. A refusal is a defect of the suite (or of the runtime
        that admitted the call), never something the body is expected to do."""
        from .safety import is_motion
        try:
            if cap.startswith("x.") or (cap in spec_caps.CAPABILITIES and is_motion(cap)):
                problem = self._guard_problem(cap, args)
            else:
                problem = None
        except Exception as exc:          # cannot prove zero displacement -> refuse
            problem = f"{cap}：无法证明零位移（{type(exc).__name__}: {exc}）"
        if problem:
            self.guard_violations.append(problem)
        return problem

    def _guard_problem(self, cap: str, args: dict) -> str | None:
        if cap == "base.velocity":
            nonzero = {k: args.get(k) for k in ("vx", "vy", "wz") if args.get(k, 0.0) != 0.0}
            return f"base.velocity 非零 {nonzero}" if nonzero else None
        if cap == "arm.joint_position":
            # R4b-02: dry never sends arm targets (a wrong joint_names declaration would turn a
            # "zero displacement" target into a real move); refusal probes must be refused upstream.
            return "arm.joint_position 在 dry 级不得到达适配器"
        if cap == "policy.action_chunk":
            space = self._param("policy.action_chunk", "action_space")
            rows = args.get("actions") or []
            if space == "base_velocity":
                bad = [r for r in rows if any(v != 0.0 for v in r)]
                return f"base_velocity 动作块含非零行 {bad[:2]}" if bad else None
            if space == "joint_position":
                return "joint_position 动作块在 dry 级不得到达适配器"
            return f"动作空间 {space} 无法证明零位移"
        return f"{cap} 在 dry 级不得到达适配器"

    # ------------------------------------------------------------ arm joint mapping (Q4-02 / R4-01)
    _arm_names: list | None = None
    NO_JOINT_NAMES = ("arm.joint_position 未声明 params.joint_names（能力标准 v1.1）：无法证明 joint.state 的关节顺序"
                      "与机械臂指令顺序一致（ROS 的 /joint_states 常按字母序或 URDF 序发布）；不发送任何机械臂/关节动作块指令。"
                      "请在清单中按指令顺序声明 joint_names")

    def _arm_joint_map(self) -> list:
        """Names of the arm's joints in ``arm.joint_position`` order: the manifest's declared
        ``joint_names`` (R4-01), verified once against ``joint.state`` -- every declared name
        present, names unique, same order in two reads. The suite never infers the command order
        from ``joint.state``; without ``joint_names`` every arm case is blocked and nothing is sent."""
        if self._arm_names is not None:
            return self._arm_names
        if "joint.state" not in self.caps:
            raise _Skip("未声明 joint.state，无法读取当前关节角：不发送任何机械臂指令", BLOCKED)
        names = _declared_joint_names(self.manifest or {"capabilities": list(self.caps.values())})
        if names is None:
            raise _Skip(self.NO_JOINT_NAMES, BLOCKED)
        a, b = self._joints(), self._joints()
        for sample in (a, b):
            got = sample.get("names")
            if (not isinstance(got, list) or len(set(got)) != len(got)
                    or len(sample.get("position") or []) != len(got)):
                raise _Skip(f"joint.state 关节名重复或与 position 长度不一致（{got}）；不发送任何机械臂指令", BLOCKED)
            missing = [n for n in names if n not in got]
            if missing:
                raise _Skip(f"joint.state 缺少 joint_names 声明的关节 {missing}（收到 {got}）；不发送任何机械臂指令", BLOCKED)
        if a["names"] != b["names"]:
            raise _Skip("两次读取 joint.state 的关节名顺序不同；不发送任何机械臂指令", BLOCKED)
        self._arm_names = list(names)
        return self._arm_names

    def _arm_current(self) -> list:
        names = self._arm_joint_map()
        try:
            return _map_joint_state(self._joints(), names)
        except ValueError as exc:
            raise _Abort(f"joint.state 在测试中不再包含声明的关节（{exc}）") from None

    # ------------------------------------------------------------ dry cases
    def _dry_cases(self, cid: str, spec) -> list:
        if spec.kind in ("state", "stream"):
            return [("read", "可读取且输出符合标准", lambda ev: self._dry_read(cid, ev))]
        table = {
            "safety.stop": [("idempotent_latched", "停止可调用、幂等、<1 s 且锁存", self._dry_stop),
                            ("latch_blocks_motion", "锁存期间运动被拒、复位需授权", self._dry_latch),
                            ("stop_epoch", "停止前发起的指令在复位后作废", self._dry_epoch)],
            "base.velocity": [("zero_setpoint", "零速度指令被接受且不武装看门狗", self._dry_zero_velocity)],
            "base.navigate_to": [("rejects_invalid_goal", "非法目标被安全监督器拒绝", self._dry_nav_reject)],
            "legged.posture": [("refused_without_motion", "非法值与锁存期间的姿态指令被拒（不运动）", self._dry_posture_refused),
                               ("rejects_unsupported", "不支持的姿态被拒绝", self._dry_posture_reject)],
            "arm.joint_position": [("zero_displacement", "目标=当前关节角（按关节名对应）：成功且不移动", self._dry_arm_zero),
                                   ("rejects_out_of_limits", "超限目标被拒绝", self._dry_arm_reject)],
            "arm.ee_pose": [("rejects_out_of_reach", "超出工作半径被拒绝", self._dry_ee_reject)],
            "gripper.position": [("rejects_out_of_range", "开度超出 0–1 被拒绝", self._dry_gripper_reject)],
            "policy.action_chunk": [("zero_chunk", "零位移动作块执行且不移动", self._dry_chunk_zero),
                                    ("rejects_bad_dt", "超长 dt 被拒绝", self._dry_chunk_reject)],
            "audio.speak": [("empty_text", "空文本播报被接受", self._dry_speak)],
            "model.install": [("refuses_bad_requests", "未授权、非法 sha、被禁地址被拒且不产生安装", self._dry_model_install)],
        }
        return table.get(cid, [("none", "无 dry 测试", lambda ev: self._na("该能力没有 dry 级测试"))])

    def _dry_read(self, cid: str, ev: dict) -> None:
        latencies, samples = [], []
        for _ in range(3):
            t0 = time.monotonic()
            out = self._read(cid)
            latencies.append(time.monotonic() - t0)
            try:
                spec_caps.validate_payload(cid, out, "outputs")
            except ValueError as exc:
                raise _Fail(f"输出不符合能力标准：{exc}") from None
            samples.append(out)
            time.sleep(0.02)
        ev.update(sample=samples[-1], read_latency_s=latencies, max_read_latency_s=max(latencies))
        _check(max(latencies) < self.t.read_latency_s,
               f"读取时延 {max(latencies):.3f} s 超过 {self.t.read_latency_s} s")
        for index, sample in enumerate(samples):          # Q4-10: every sample, not only the last
            try:
                self._sanity(cid, sample, ev)
            except _Fail as exc:
                raise _Fail(f"第 {index + 1}/{len(samples)} 次读数：{exc}") from None
        self._sanity_series(cid, samples)

    def _sanity(self, cid: str, last: dict, ev: dict) -> None:
        if cid == "system.health":
            _check(last["ok"] is True, f"本体报告不健康：mode={last.get('mode')} errors={last.get('errors')}")
        elif cid == "sensor.battery":
            _check(0.0 <= last["percent"] <= 100.0, f"percent={last['percent']} 不在 0–100")
            if "voltage" in last:
                _check(last["voltage"] >= 0.0, f"voltage={last['voltage']} 为负")
        elif cid == "sensor.imu":
            n = _quat_norm(last["orientation"])
            _check(abs(n - 1.0) < 1e-2, f"orientation 四元数模长 {n:.4f} ≠ 1")
        elif cid in ("base.odometry", "localization.pose", "arm.ee_state"):
            n = _quat_norm(last["pose"][3:7])
            _check(abs(n - 1.0) < 1e-2, f"pose 四元数模长 {n:.4f} ≠ 1")
            if cid == "base.odometry":
                ev["at_rest_speed"] = max(abs(v) for v in last["velocity"])
        elif cid == "gripper.state":
            _check(0.0 <= last["opening"] <= 1.0, f"opening={last['opening']} 不在 0–1")
        elif cid == "joint.state":
            dof = self._param("joint.state", "dof")
            names, pos = last["names"], last["position"]
            _check(isinstance(names, list) and all(isinstance(n, str) for n in names), "names 须为字符串列表")
            _check(len(names) == len(pos), f"names({len(names)}) 与 position({len(pos)}) 长度不同")
            _check(len(set(names)) == len(names), f"关节名重复：{names}")
            if dof is not None:
                _check(len(pos) == dof, f"position 长度 {len(pos)} ≠ dof {dof}")
            for key in ("velocity", "effort"):
                if key in last:
                    _check(len(last[key]) == len(pos), f"{key} 长度与 position 不同")
            arm_dof = self._param("arm.joint_position", "dof")
            lower, upper = self._param("arm.joint_position", "lower"), self._param("arm.joint_position", "upper")
            if lower and arm_dof == len(pos):              # limits apply only when the mapping is unambiguous
                bad = [i for i, (q, lo, hi) in enumerate(zip(pos, lower, upper)) if not lo - 1e-3 <= q <= hi + 1e-3]
                _check(not bad, f"关节 {bad} 当前位置超出声明限位")
        elif cid in ("sensor.camera.rgb", "sensor.camera.depth"):
            key = "image" if cid.endswith("rgb") else "depth"
            _check(bool(last[key]), f"{key} 引用为空")
            _check(last["width"] > 0 and last["height"] > 0, "width/height 须大于 0")
        elif cid == "sensor.lidar":
            _check(bool(last["points"]), "points 引用为空")

    @staticmethod
    def _sanity_series(cid: str, samples: list) -> None:
        if cid in ("sensor.camera.rgb", "sensor.camera.depth", "sensor.lidar"):
            stamps = [s["stamp"] for s in samples]
            _check(all(b >= a for a, b in zip(stamps, stamps[1:])), f"stamp 倒退：{stamps}")
        if cid == "joint.state":
            names = [s["names"] for s in samples]
            _check(all(n == names[0] for n in names), "多次读取的关节名或顺序不同")

    def _probe(self) -> tuple[str, dict] | None:
        """A motion call that cannot move the body even if it were admitted."""
        if "base.velocity" in self.caps:
            return "base.velocity", _assert_zero({"vx": 0.0, "wz": 0.0})
        if "arm.joint_position" in self.caps:
            try:
                return "arm.joint_position", {"position": self._arm_current()}
            except _Skip as exc:
                self._probe_block = exc
                return None
        return None

    def _dry_stop(self, ev: dict) -> None:
        latencies, results = [], []
        for i in range(2):
            result, latency = self._stop(f"dry-stop-{i}")
            latencies.append(latency)
            results.append(result)
        ev.update(stop_latency_s=latencies, results=results)
        _check(all(r.get("stopped") is True for r in results),
               f"stop 未报告 stopped=true（适配器 stop() 失败或抛异常）：{results}")
        _check(max(latencies) < self.t.stop_latency_s,
               f"停止调用时延 {max(latencies):.3f} s 超过 {self.t.stop_latency_s} s")
        _check(self.target.latched(), "停止后未锁存")
        if "base.odometry" in self.caps:
            still, waited = self._wait(self._base_still, self.t.stop_latency_s)
            ev["still_after_s"] = waited
            _check(bool(still), "停止后底盘未静止")

    def _dry_latch(self, ev: dict) -> None:
        if not self.target.latched():
            self._stop("dry-latch")
        probe = self._probe()
        if probe is None:
            ev["note"] = "无可安全探测的运动能力，只验证复位授权"
        else:
            cap, args = probe
            try:
                if spec_caps.get(cap).kind == "command":
                    self.target.command(cap, args, True)
                    raise _Fail(f"锁存期间 {cap} 仍被接受")
                run_id = self.target.start_action(cap, args, True)
                record = self._wait_run(run_id, 5.0)
                raise _Fail(f"锁存期间 {cap} 仍被接受（run {record.get('status')}）")
            except CallError as exc:
                ev["latched_rejection"] = {"capability": cap, "code": exc.code, "status": exc.status}
                _check(exc.code == "estop_latched", f"锁存期间 {cap} 被拒绝，但错误码为 {exc.code}（应为 estop_latched）")
        try:
            self.target.reset(False)
            raise _Fail("未授权的 reset 被接受")
        except CallError as exc:
            ev["unauthorized_reset"] = {"code": exc.code, "status": exc.status}
            _check(exc.status == 403 or exc.code == "hardware_not_authorized",
                   f"未授权 reset 应返回 403 hardware_not_authorized，收到 {exc.status} {exc.code}")
        _check(self.target.latched(), "未授权的 reset 解除了锁存")
        self._ensure_unlatched()
        _check(not self.target.latched(), "授权 reset 后仍锁存")
        ev["reset"] = "ok"

    def _dry_epoch(self, ev: dict) -> None:
        self._ensure_unlatched()
        probe = self._probe()
        if probe is None:
            if self._probe_block is not None:
                raise _Skip(f"唯一可探测的运动能力是机械臂：{self._probe_block}", BLOCKED)
            self._na("无可安全探测的运动能力")
        cap, args = probe
        epoch = self.target.epoch()
        if not self.target.in_process or epoch is None:
            raise _Skip("黑盒 HTTP 无法构造“停止前到达、复位后才下发”的指令；请用 POST /v2/conformance "
                        "（--mode server）在运行时进程内执行此项", BLOCKED)
        self._stop("stale-probe")
        self._ensure_unlatched()
        if cap == "arm.joint_position":
            args = {"position": self._arm_current()}
        result = self.target.stale_call(cap, args, epoch)
        ev.update(result, capability=cap)
        _check(result["rejected_code"] == "stale_after_stop",
               f"停止前发起的 {cap} 在复位后未被作废（错误码 {result['rejected_code']}）")
        _check(result["stale_event"], "未产生 safety.stale_rejected 事件")

    def _dry_zero_velocity(self, ev: dict) -> None:
        self._ensure_unlatched()
        args = _assert_zero({"vx": 0.0, "wz": 0.0, "timeout_s": 0.1})
        if self._param("base.velocity", "holonomic"):
            args["vy"] = 0.0
        t0 = time.monotonic()
        out = self.target.command("base.velocity", args, True)
        latency = time.monotonic() - t0
        ev.update(outputs=out, command_latency_s=latency)
        _check(out.get("accepted") is True, f"零速度指令未被接受：{out}")
        _check(latency < self.t.command_latency_s, f"指令时延 {latency:.3f} s 超过 {self.t.command_latency_s} s")
        armed = self.target.watchdog_armed()
        ev["watchdog_armed"] = armed
        _check(armed is not True, "零速度指令不应武装看门狗")
        if "base.odometry" in self.caps:
            time.sleep(0.2)
            still, _ = self._wait(self._base_still, 1.0)
            _check(bool(still), "零速度指令后底盘未静止")
            ev["speed_after"] = max(abs(v) for v in self._odom()["velocity"])

    def _expect_rejected_action(self, cap: str, args: dict, needle: str, ev: dict) -> None:
        self._require_guard()
        self._ensure_unlatched()
        try:
            run_id = self.target.start_action(cap, args, True)
        except CallError as exc:      # rejected synchronously (spec validation)
            ev["rejected"] = {"code": exc.code, "error": str(exc)}
            return
        record = self._wait_run(run_id, 5.0)
        ev["run"] = {"status": record.get("status"), "error": record.get("error")}
        _check(record.get("status") == "failed", f"{cap} 非法目标未被拒绝（run {record.get('status')}）")
        _check(needle in (record.get("error") or ""), f"{cap} 失败原因不是安全监督器拒绝：{record.get('error')}")

    def _map_pose(self) -> list | None:
        """Pose in the map frame (navigation goals): only ``localization.pose`` (Q4-04)."""
        if "localization.pose" in self.caps:
            return self._read("localization.pose")["pose"]
        return None

    def _dry_nav_reject(self, ev: dict) -> None:
        pose = self._map_pose()
        x, y = (pose[0], pose[1]) if pose else (0.0, 0.0)
        # Only reaches the adapter if the runtime wrongly admits it -- and then the dry guard refuses it.
        self._expect_rejected_action("base.navigate_to", {"goal": [x, y, 0.0, 0.0, 0.0, 0.0, 2.0]}, "四元数", ev)
        workspace = (self.manifest or {}).get("safety", {}).get("workspace") or {}
        if isinstance(workspace.get("x"), list):
            sub: dict = {}
            just_out = workspace["x"][1] + 0.01          # 1 cm beyond the bound (Q4-16)
            self._expect_rejected_action("base.navigate_to", {"goal": [just_out, y, 0.0, 0.0, 0.0, 0.0, 1.0]},
                                         "工作区", sub)
            ev["workspace"] = sub

    def _dry_posture_reject(self, ev: dict) -> None:
        supported = self._param("legged.posture", "supported") or []
        choices = [p for p in ("lie", "sit", "stand", "recover") if p not in supported]
        if not choices:
            self._na("本体支持全部标准姿态，没有可拒绝的姿态；姿态变化在 motion 级由现场人工验证")
        self._require_guard()
        self._ensure_unlatched()
        try:
            out = self.target.command("legged.posture", {"posture": choices[0]}, True)
            raise _Fail(f"不支持的姿态 {choices[0]} 被接受：{out}")
        except CallError as exc:
            ev["rejected"] = {"posture": choices[0], "code": exc.code}
            _check(exc.code == "safety_rejected", f"错误码 {exc.code}（应为 safety_rejected）")

    def _dry_posture_refused(self, ev: dict) -> None:
        """The spec has no posture read-out, so "re-send the current posture" cannot be verified
        and could stand a lying robot up. Instead: an out-of-spec value must be refused by input
        validation, and a valid posture must be refused while the suite holds the e-stop latch --
        neither can reach the adapter (the dry guard refuses any posture anyway), and health must
        be unchanged afterwards."""
        self._require_guard()
        try:
            out = self.target.command("legged.posture", {"posture": "dance"}, True)
            raise _Fail(f"非标准姿态值被接受：{out}")
        except CallError as exc:
            ev["invalid_value"] = {"code": exc.code, "status": exc.status}
            _check(exc.status == 400 or exc.code == "bad_request", f"非标准姿态值应 400，收到 {exc.status} {exc.code}")
        if not self.target.latched():
            self._stop("dry-posture")
        supported = self._param("legged.posture", "supported") or ["stand"]
        try:
            out = self.target.command("legged.posture", {"posture": supported[0]}, True)
            raise _Fail(f"锁存期间姿态指令被接受：{out}")
        except CallError as exc:
            ev["latched"] = {"posture": supported[0], "code": exc.code}
            _check(exc.code == "estop_latched", f"锁存期间姿态指令错误码 {exc.code}（应为 estop_latched）")
        health = self._read("system.health")
        ev["health_after"] = health
        _check(health["ok"] is True, f"姿态检查后本体不健康：{health}")
        ev["note"] = "规范没有姿态读取能力：不发送“当前姿态”的空操作（无法验证且可能让趴下的机器人站起）"
        self._ensure_unlatched()

    def _dry_model_install(self, ev: dict) -> None:
        """Nothing may be installed: unauthorized, malformed sha and refused addresses all fail."""
        runtimes = self._param("model.install", "runtimes") or ["onnxruntime"]
        before = self.target.installed_models()
        bogus = uuid.uuid4().hex * 2                 # well-formed sha256 that no package has
        probes = [
            ("unauthorized", {"package_ref": "https://models.invalid/m.onnx", "sha256": bogus, "runtime": runtimes[0]}, False),
            ("bad_sha", {"package_ref": "https://models.invalid/m.onnx", "sha256": "not-a-sha", "runtime": runtimes[0]}, True),
            ("metadata_address", {"package_ref": "https://169.254.169.254/latest/meta-data/m.onnx", "sha256": bogus,
                                  "runtime": runtimes[0]}, True),
            ("file_scheme", {"package_ref": "file:///etc/passwd", "sha256": bogus, "runtime": runtimes[0]}, True),
        ]
        results = {}
        for name, args, authorized in probes:
            try:
                run_id = self.target.start_action("model.install", args, authorized, timeout_s=30.0)
            except CallError as exc:
                results[name] = {"refused": True, "code": exc.code, "status": exc.status}
                continue
            record = self._wait_run(run_id, 35.0)
            results[name] = {"refused": record.get("status") == "failed", "run_status": record.get("status"),
                             "error": record.get("error")}
        ev["probes"] = results
        ev["scope"] = "只证明非法安装请求被拒绝且不产生安装；不证明能成功安装（Q4-22）"
        bad = [n for n, r in results.items() if not r["refused"]]
        _check(not bad, f"这些非法安装请求未被拒绝：{bad}")
        _check(results["unauthorized"].get("status") == 403 or results["unauthorized"].get("code") == "hardware_not_authorized",
               f"未授权安装应 403，收到 {results['unauthorized']}")
        after = self.target.installed_models()
        ev["installed_before"], ev["installed_after"] = before, after
        if after is not None:
            _check(bogus not in after and sorted(after) == sorted(before or []), "拒绝的安装请求改变了已安装模型")
        else:
            ev["note"] = "模型由适配器提供，无法列出已安装模型：只验证拒绝"

    DRY_NO_ARM = ("dry 级不向适配器下发机械臂目标或 joint_position 动作块（R4b-02：joint_names 声明错误时，"
                  "“目标=当前关节角”会变成真实转动）；dry 只验证请求校验与拒绝，执行由 motion 级 {case} 验证")

    def _dry_arm_zero(self, ev: dict) -> None:
        self._arm_joint_map()                  # undeclared joint_names / bad joint.state -> blocked (R4-01)
        ev.update(dry_skipped=True, joint_names=list(self._arm_names))
        raise _Skip(self.DRY_NO_ARM.format(case="arm.joint_position.min_move"), NOT_APPLICABLE)

    def _dry_arm_reject(self, ev: dict) -> None:
        target = self._arm_current()
        target[-1] = self._param("arm.joint_position", "upper")[-1] + 0.01
        self._expect_rejected_action("arm.joint_position", {"position": target}, "超出限位", ev)

    def _dry_ee_reject(self, ev: dict) -> None:
        reach = self._param("arm.ee_pose", "reach")
        self._expect_rejected_action("arm.ee_pose", {"pose": [reach + 0.01, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]},
                                     "工作半径", ev)

    def _dry_gripper_reject(self, ev: dict) -> None:
        self._expect_rejected_action("gripper.position", {"opening": 1.5}, "0–1", ev)

    def _chunk_dt(self) -> float:
        rate = self._param("policy.action_chunk", "rate_hz")
        return 1.0 / rate if rate else 0.1

    def _dry_chunk_zero(self, ev: dict) -> None:
        space = self._param("policy.action_chunk", "action_space")
        if space == "joint_position":
            self._arm_joint_map()              # undeclared joint_names -> blocked (R4-01)
            ev["dry_skipped"] = True
            raise _Skip(self.DRY_NO_ARM.format(case="policy.action_chunk.min_chunk"), NOT_APPLICABLE)
        self._ensure_unlatched()
        if space == "base_velocity":
            row = [0.0, 0.0, 0.0]
        else:
            self._na(f"运行时无法为动作空间 {space} 限幅（会被拒绝），没有零位移测试")
        run_id = self.target.start_action("policy.action_chunk", {"actions": [row, row], "dt": self._chunk_dt()}, True)
        record = self._wait_run(run_id, self.t.action_timeout_s)
        ev.update(status=record.get("status"), result=record.get("result"))
        _check(record.get("status") == "succeeded", f"零位移动作块未成功：{record.get('status')} {record.get('error')}")
        _check((record.get("result") or {}).get("executed") == 2, f"executed 应为 2：{record.get('result')}")

    def _dry_chunk_reject(self, ev: dict) -> None:
        dim = self._param("policy.action_chunk", "dim")
        space = self._param("policy.action_chunk", "action_space")
        row = self._arm_current() if space == "joint_position" else [0.0] * dim
        self._expect_rejected_action("policy.action_chunk", {"actions": [row], "dt": 3.0 * self._chunk_dt() + 1.0},
                                     "dt", ev)

    def _dry_speak(self, ev: dict) -> None:
        out = self.target.command("audio.speak", {"text": ""}, False)
        ev["outputs"] = out
        _check(isinstance(out.get("accepted"), bool), f"输出缺少 accepted：{out}")

    # ------------------------------------------------------------ motion cases
    def _motion_cases(self, cid: str, spec) -> list:
        if spec.safety != "motion":
            return []
        table = {
            "base.velocity": [("watchdog", "小速度指令：方向/幅度一致，不刷新时看门狗归零并持续静止", self._motion_velocity_watchdog),
                              ("turn", "小角速度 wz：转向符号/幅度一致、无明显平移，看门狗归零", lambda ev: self._motion_velocity_axis(ev, 2)),
                              ("lateral", "全向底盘小侧向速度 vy：方向/幅度一致，看门狗归零", lambda ev: self._motion_velocity_axis(ev, 1)),
                              ("stop_latency", "运动中停止 <1 s 静止并持续静止", self._motion_velocity_stop)],
            "base.navigate_to": [("min_goal", "地图坐标系最小目标到达且位移方向一致", self._motion_nav_min),
                                 ("cancel", "运动中取消 <1 s 静止并持续静止", lambda ev: self._motion_nav_interrupt(ev, "cancel")),
                                 ("stop_latency", "运动中停止 <1 s 静止并持续静止", lambda ev: self._motion_nav_interrupt(ev, "stop"))],
            "legged.posture": [("stand_noop", "站立姿态指令（不改变姿态）", self._motion_posture)],
            "arm.joint_position": [("min_move", "最小幅度关节运动且反馈一致", self._motion_arm_min),
                                   ("cancel", "运动中取消 <1 s 静止并持续静止", lambda ev: self._motion_arm_interrupt(ev, "cancel")),
                                   ("stop_latency", "运动中停止 <1 s 静止并持续静止", lambda ev: self._motion_arm_interrupt(ev, "stop"))],
            "gripper.position": [("min_move", "最小幅度开度变化且反馈一致", self._motion_gripper_min),
                                 ("cancel", "动作中取消 <1 s 结束并持续静止", lambda ev: self._motion_gripper_interrupt(ev, "cancel")),
                                 ("stop_latency", "动作中停止 <1 s 结束并持续静止", lambda ev: self._motion_gripper_interrupt(ev, "stop"))],
            "policy.action_chunk": [("min_chunk", "最小幅度动作块执行且反馈一致", self._motion_chunk)],
            "arm.ee_pose": [("min_move", "最小幅度末端位移且 arm.ee_state 反馈一致", self._motion_ee_min)],
        }
        return table.get(cid, [])

    def _after_stop(self, ev: dict, still_fn: Callable[[], bool], label: str) -> None:
        """Stop, measure call latency and time until still, then require stillness for
        ``still_hold_s`` (delayed resumes, Q4-07). Any failure aborts with the latch kept."""
        t0 = time.monotonic()
        result, call_latency = self._stop(label)
        still, _ = self._wait(still_fn, self.t.stop_latency_s * 2)
        still_after = time.monotonic() - t0
        ev.update(stop_call_latency_s=call_latency, still_after_stop_s=still_after, stop_result=result)
        if not (still and still_after < self.t.stop_latency_s and call_latency < self.t.stop_latency_s
                and result.get("stopped") is True):
            raise _Abort(f"停止后 {self.t.stop_latency_s} s 内未静止或停止调用失败（调用 {call_latency:.3f} s，"
                         f"静止 {'未观察到' if not still else f'{still_after:.3f} s'}）；已保持急停锁存")
        _check(self.target.latched(), "停止后未锁存")
        self._hold_still(self.t.still_hold_s, "停止后", still_fn)
        ev["still_hold_s"] = self.t.still_hold_s

    def _after_cancel(self, ev: dict, run_id: str, still_fn: Callable[[], bool]) -> None:
        t0 = time.monotonic()
        self.target.cancel(run_id)
        still, _ = self._wait(still_fn, self.t.cancel_latency_s * 2)
        after = time.monotonic() - t0
        ev["still_after_cancel_s"] = after
        if not still:
            raise _Abort("取消后本体未静止；已急停并保持锁存")
        _check(after < self.t.cancel_latency_s, f"取消后 {after:.3f} s 才静止（上限 {self.t.cancel_latency_s} s）")
        self._hold_still(self.t.still_hold_s, "取消后", still_fn)
        ev["still_hold_s"] = self.t.still_hold_s

    # base.velocity
    def _velocity_args(self, timeout_s: float) -> tuple[dict, float]:
        if "base.odometry" not in self.caps:
            raise _Skip("未声明 base.odometry，无法读取运动反馈", BLOCKED)
        v = min(self.t.base_speed, 0.25 * float(self._param("base.velocity", "max_vx")))
        return {"vx": v, "wz": 0.0, "timeout_s": timeout_s}, v

    def _wait_moving(self, start: dict, ev: dict) -> None:
        moving, waited = self._wait(lambda: self._base_moving(start), self.t.move_detect_s)
        ev["move_detected_after_s"] = waited
        if not moving:
            raise _Fail(f"指令后 {self.t.move_detect_s} s 内反馈中未观察到运动")

    def _check_base_motion(self, ev: dict, v: float, start: dict, end: dict, samples: list, axis: int = 0) -> None:
        """Q4-06 / R4-04: direction and magnitude of the commanded axis (0 vx, 1 vy, 2 wz) must match
        the command (body frame of the start pose); the other axes must stay small."""
        names = ("vx", "vy", "wz")
        unit = "rad/s" if axis == 2 else "m/s"
        yaw0 = _yaw(start["pose"])
        dx, dy = end["pose"][0] - start["pose"][0], end["pose"][1] - start["pose"][1]
        forward = math.cos(yaw0) * dx + math.sin(yaw0) * dy
        lateral = -math.sin(yaw0) * dx + math.cos(yaw0) * dy
        turned = math.remainder(_yaw(end["pose"]) - yaw0, 2 * math.pi)
        moving = [s for s in samples if max(abs(c) for c in s) > self.t.still_velocity]
        peaks = [max((s[i] for s in moving), key=abs, default=None) for i in range(3)]
        ev.update(forward_m=forward, lateral_m=lateral, turned_rad=turned, velocity_samples=len(moving),
                  peak_vx=peaks[0], peak_vy=peaks[1], peak_wz=peaks[2], axis=names[axis])
        _check(moving, "运动中没有采到非零速度读数，无法核对速度")
        tol, cross = self.t.velocity_tolerance, self.t.cross_axis_tolerance
        peak = peaks[axis]
        _check(all(s[axis] >= -self.t.still_velocity for s in moving),
               f"里程计 {names[axis]} 出现反向（指令 {names[axis]}=+{v} {unit}）")
        _check(v * (1 - tol) <= peak <= v * (1 + tol),
               f"里程计 {names[axis]} 峰值速度 {peak:.3f} {unit} 与指令 {v} {unit} 不符（容差 ±{tol:.0%}）")
        if axis == 0:
            _check(forward > 0.0, f"位移方向与指令相反（前向位移 {forward:.3f} m）")
            _check(abs(lateral) <= cross * abs(forward) + 0.005,
                   f"侧向位移 {lateral:.3f} m 过大（前向 {forward:.3f} m，上限 {cross:.0%}）")
            _check(abs(peaks[1]) <= cross * v + self.t.still_velocity,
                   f"指令只有 vx，里程计 vy 峰值 {peaks[1]:.3f} m/s 过大")
            self._check_no_yaw(peaks[2], turned, v, "vx")
        elif axis == 1:
            _check(lateral > 0.0, f"侧向位移方向与指令相反（侧向位移 {lateral:.3f} m）")
            _check(abs(forward) <= cross * abs(lateral) + 0.005,
                   f"前向位移 {forward:.3f} m 过大（侧向 {lateral:.3f} m，上限 {cross:.0%}）")
            _check(abs(peaks[0]) <= cross * v + self.t.still_velocity,
                   f"指令只有 vy，里程计 vx 峰值 {peaks[0]:.3f} m/s 过大")
            self._check_no_yaw(peaks[2], turned, v, "vy")
        else:
            _check(turned > 0.0, f"转向与指令相反（偏航变化 {turned:.4f} rad，指令 wz=+{v}）")
            moved = math.hypot(dx, dy)
            _check(moved <= 0.02, f"指令只有 wz，底盘却平移了 {moved:.3f} m")
            _check(max(abs(peaks[0]), abs(peaks[1])) <= 0.05,
                   f"指令只有 wz，里程计线速度峰值 {max(abs(peaks[0]), abs(peaks[1])):.3f} m/s 过大")

    def _check_no_yaw(self, peak_wz, turned: float, v: float, axis: str) -> None:
        """R4b-05: a translation command must not turn the base."""
        limit = self.t.turn_rate * (v / self.t.base_speed) if self.t.base_speed > 0 else self.t.turn_rate
        _check(abs(peak_wz or 0.0) <= limit + self.t.still_velocity,
               f"指令只有 {axis}，里程计 wz 峰值 {abs(peak_wz or 0.0):.3f} rad/s 过大（上限 {limit:.3f} rad/s）")
        _check(abs(turned) <= 0.05, f"指令只有 {axis}，底盘却转向 {turned:.3f} rad（上限 0.05 rad）")

    def _sample_until(self, samples: list, predicate: Callable[[], bool], timeout: float) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            odom = self._odom()
            samples.append(list(odom["velocity"]))
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _motion_velocity_watchdog(self, ev: dict) -> None:
        self._ensure_unlatched()
        timeout_s = 0.3
        args, v = self._velocity_args(timeout_s)
        start = self._odom()
        t0 = time.monotonic()
        out = self.target.command("base.velocity", args, True)
        _check(out.get("accepted") is True, f"速度指令未被接受：{out}")
        samples: list = []
        moving = self._sample_until(samples, lambda: self._base_moving(start), self.t.move_detect_s)
        ev["move_detected_after_s"] = time.monotonic() - t0
        _check(moving, f"指令后 {self.t.move_detect_s} s 内反馈中未观察到运动")
        still = self._sample_until(samples, self._base_still, timeout_s + self.t.watchdog_margin_s + 1.0)
        zeroed_after = time.monotonic() - t0
        end = self._odom()
        ev.update(command=args, watchdog_timeout_s=timeout_s, zeroed_after_s=zeroed_after,
                  distance_m=math.hypot(end["pose"][0] - start["pose"][0], end["pose"][1] - start["pose"][1]))
        if not still:
            raise _Abort("看门狗未让底盘停下；已急停并保持锁存")
        _check(zeroed_after <= timeout_s + self.t.watchdog_margin_s,
               f"看门狗归零用时 {zeroed_after:.3f} s，超过 timeout_s {timeout_s} + {self.t.watchdog_margin_s} s")
        self._check_base_motion(ev, v, start, end, samples)
        self._hold_still(self.t.still_hold_s, "看门狗归零后", self._base_still)

    def _motion_velocity_axis(self, ev: dict, axis: int) -> None:
        """R4-04: a small wz (axis 2) or, on a holonomic base, vy (axis 1) setpoint, left to the
        watchdog; sign and magnitude checked against odometry like vx."""
        if "base.odometry" not in self.caps:
            raise _Skip("未声明 base.odometry，无法读取运动反馈", BLOCKED)
        if axis == 1:
            max_vy = float(self._param("base.velocity", "max_vy", 0.0) or 0.0)
            if self._param("base.velocity", "holonomic") is not True or max_vy <= 0.0:
                self._na("非全向底盘（holonomic≠true 或 max_vy=0）：没有侧向速度")
            v = min(self.t.base_speed, 0.25 * max_vy)
            args = {"vx": 0.0, "vy": v, "wz": 0.0}
        else:
            v = min(self.t.turn_rate, 0.25 * float(self._param("base.velocity", "max_wz")))
            args = {"vx": 0.0, "wz": v}
            if self._param("base.velocity", "holonomic"):
                args["vy"] = 0.0
        self._ensure_unlatched()
        timeout_s = 0.3
        args["timeout_s"] = timeout_s
        start = self._odom()
        t0 = time.monotonic()
        out = self.target.command("base.velocity", args, True)
        _check(out.get("accepted") is True, f"速度指令未被接受：{out}")
        samples: list = []
        moving = self._sample_until(samples, lambda: self._base_moving(start), self.t.move_detect_s)
        ev["move_detected_after_s"] = time.monotonic() - t0
        _check(moving, f"指令后 {self.t.move_detect_s} s 内反馈中未观察到运动")
        still = self._sample_until(samples, self._base_still, timeout_s + self.t.watchdog_margin_s + 1.0)
        ev.update(command=args, zeroed_after_s=time.monotonic() - t0)
        if not still:
            raise _Abort("看门狗未让底盘停下；已急停并保持锁存")
        self._check_base_motion(ev, v, start, self._odom(), samples, axis)
        self._hold_still(self.t.still_hold_s, "看门狗归零后", self._base_still)

    def _motion_velocity_stop(self, ev: dict) -> None:
        self._ensure_unlatched()
        args, v = self._velocity_args(2.0)
        start = self._odom()
        out = self.target.command("base.velocity", args, True)
        _check(out.get("accepted") is True, f"速度指令未被接受：{out}")
        samples: list = []
        _check(self._sample_until(samples, lambda: self._base_moving(start), self.t.move_detect_s),
               f"指令后 {self.t.move_detect_s} s 内反馈中未观察到运动")
        self._sample_until(samples, lambda: False, 0.15)
        self._check_base_motion(ev, v, start, self._odom(), samples)
        self.target.command("base.velocity", args, True)       # the stop must meet a moving base
        self._after_stop(ev, self._base_still, "velocity-stop")
        self._ensure_unlatched()

    # base.navigate_to (goals in the map frame: only with localization.pose, Q4-04)
    def _goal(self, pose: list, distance: float) -> list:
        yaw = _yaw(pose)
        return [pose[0] + distance * math.cos(yaw), pose[1] + distance * math.sin(yaw), 0.0] + list(pose[3:7])

    def _nav_feedback_or_block(self) -> list:
        if "base.odometry" not in self.caps:
            raise _Skip("未声明 base.odometry，无法读取运动反馈", BLOCKED)
        pose = self._map_pose()
        if pose is None:
            raise _Skip("未声明 localization.pose：没有地图坐标系位姿，无法计算安全的最小导航目标"
                        "（odom 与 map 可能相差数米）；不发送导航目标", BLOCKED)
        return pose

    def _motion_nav_min(self, ev: dict) -> None:
        self._ensure_unlatched()
        map_pose = self._nav_feedback_or_block()
        start = self._odom()["pose"]
        tolerance = 0.03
        goal = self._goal(map_pose, self.t.base_step_m)
        run_id = self.target.start_action("base.navigate_to", {"goal": goal, "tolerance": tolerance}, True,
                                          timeout_s=self.t.action_timeout_s)
        moved_max = [0.0]

        def done_or_too_far():
            pose = self._odom()["pose"]
            moved_max[0] = max(moved_max[0], math.hypot(pose[0] - start[0], pose[1] - start[1]))
            if moved_max[0] > self.t.base_step_m + 0.15:
                return True
            return self.target.run(run_id).get("status") in TERMINAL_RUN
        self._wait(done_or_too_far, self.t.action_timeout_s + 5.0)
        _check(moved_max[0] <= self.t.base_step_m + 0.15,
               f"最小目标 {self.t.base_step_m} m，本体已行驶 {moved_max[0]:.2f} m：坐标系或规划异常")
        record = self._wait_run(run_id, 2.0)
        end = self._odom()["pose"]
        yaw0 = _yaw(start)
        forward = math.cos(yaw0) * (end[0] - start[0]) + math.sin(yaw0) * (end[1] - start[1])
        moved = math.hypot(end[0] - start[0], end[1] - start[1])
        ev.update(goal=goal, status=record.get("status"), result=record.get("result"), moved_m=moved, forward_m=forward)
        _check(record.get("status") == "succeeded", f"最小目标未到达：{record.get('status')} {record.get('error')}")
        _check((record.get("result") or {}).get("reached") is True, f"结果 reached≠true：{record.get('result')}")
        _check(0.3 * self.t.base_step_m <= moved <= self.t.base_step_m + tolerance + 0.05,
               f"里程计位移 {moved:.3f} m 与最小目标 {self.t.base_step_m} m 不符")
        _check(forward > 0.0, f"位移方向与目标相反（前向位移 {forward:.3f} m）")
        self._hold_still(self.t.still_hold_s, "到达后", self._base_still)

    def _motion_nav_interrupt(self, ev: dict, how: str) -> None:
        self._ensure_unlatched()
        map_pose = self._nav_feedback_or_block()
        start_odom = self._odom()
        distance = self.t.nav_interrupt_m           # forward for both cases (Q4-19), absolute cap
        goal = self._goal(map_pose, distance)
        ev["goal_distance_m"] = distance
        run_id = self.target.start_action("base.navigate_to", {"goal": goal, "tolerance": 0.02}, True,
                                          timeout_s=self.t.action_timeout_s)
        self._wait_moving(start_odom, ev)
        if how == "cancel":
            self._after_cancel(ev, run_id, self._base_still)
        else:
            self._after_stop(ev, self._base_still, "nav-stop")
        record = self._wait_run(run_id, self.t.run_settle_s)
        ev["run_status"] = record.get("status")
        _check(record.get("status") == "stopped", f"被{'取消' if how == 'cancel' else '停止'}的导航运行状态为 "
                                                   f"{record.get('status')}（应为 stopped）")
        self._ensure_unlatched()

    def _motion_posture(self, ev: dict) -> None:
        self._ensure_unlatched()
        if "stand" not in (self._param("legged.posture", "supported") or []):
            self._na("本体不支持 stand；姿态变化需现场人工验证")
        out = self.target.command("legged.posture", {"posture": "stand"}, True)
        ev["outputs"] = out
        _check(out.get("accepted") is True, f"stand 指令未被接受：{out}")
        health = self._read("system.health")
        ev["health"] = health
        _check(health["ok"] is True, f"指令后本体不健康：{health}")
        ev["note"] = "只验证 stand（已站立时不改变姿态）；lie/sit 等大幅姿态变化需现场人工验证"

    # arm (joint angles by verified joint names, Q4-02)
    def _arm_joint_target(self, q: list, step: float) -> tuple[int, list]:
        lower = self._param("arm.joint_position", "lower")
        upper = self._param("arm.joint_position", "upper")
        i = len(q) - 1                                  # last joint: the wrist, smallest swept volume
        direction = 1.0 if upper[i] - q[i] >= q[i] - lower[i] else -1.0
        target = list(q)
        target[i] = q[i] + direction * step
        _check(lower[i] < target[i] < upper[i], f"关节 {i} 当前位置离限位太近，无法安全测试")
        return i, target

    def _arm_speed(self) -> float:
        return min(0.2, float(self._param("arm.joint_position", "max_velocity")))

    def _move_arm(self, target: list, timeout: float, start: list | None = None) -> dict:
        start = start if start is not None else self._arm_current()
        run_id = self.target.start_action("arm.joint_position", {"position": target, "max_velocity": self._arm_speed()},
                                          True, timeout_s=timeout)
        return self._wait_run_arm(run_id, timeout + 5.0, start, target)

    def _arm_envelope_check(self, start: list, target: list) -> None:
        """Every joint (by declared name) must stay between its start and target (± a margin) while
        an arm command runs. A wrong ``joint_names`` declaration moves other joints than intended:
        caught within one poll period, stopped and aborted (R4b-02, motion)."""
        now = self._arm_current()
        m = self.t.arm_envelope_rad
        out = [(k, round(q - s0, 4)) for k, (q, s0, t1) in enumerate(zip(now, start, target))
               if not (min(s0, t1) - m <= q <= max(s0, t1) + m)]
        if out:
            self._restop("arm-envelope", counted=True)
            raise _Abort(f"机械臂运动超出预期范围（关节序号与偏移 {out}，允许 ±{m} rad）：可能 joint_names 声明与控制器"
                         "关节顺序不一致；已停止并保持锁存")

    def _wait_run_arm(self, run_id: str, timeout: float, start: list, target: list) -> dict:
        def poll():
            self._arm_envelope_check(start, target)
            record = self.target.run(run_id)
            return record if record.get("status") in TERMINAL_RUN else None
        record, _ = self._wait(poll, timeout, 0.02)
        self._arm_envelope_check(start, target)
        return record or self.target.run(run_id)

    def _motion_arm_min(self, ev: dict) -> None:
        self._ensure_unlatched()
        start = self._arm_current()
        joint, target = self._arm_joint_target(start, self.t.joint_step_rad)
        record = self._move_arm(target, self.t.action_timeout_s)
        after = self._arm_current()
        error = max(abs(a - b) for a, b in zip(after, target))
        ev.update(joint=joint, joint_names=list(self._arm_names), start=start, target=target,
                  status=record.get("status"), feedback=after, error_rad=error)
        _check(record.get("status") == "succeeded", f"最小关节运动未完成：{record.get('status')} {record.get('error')}")
        reported = (record.get("result") or {}).get("position") or []
        _check(len(reported) == len(target) and
               max(abs(a - b) for a, b in zip(reported, target)) <= self.t.joint_tolerance_rad,
               f"结果 position 与目标不符：{reported}")
        _check(error <= self.t.joint_tolerance_rad, f"joint.state 与目标相差 {error:.4f} rad")
        restore = self._move_arm(start, self.t.action_timeout_s, start=after)
        ev["restored"] = restore.get("status") == "succeeded"
        self._hold_still(self.t.still_hold_s, "复原后", self._joints_still)

    def _motion_arm_interrupt(self, ev: dict, how: str) -> None:
        self._ensure_unlatched()
        start = self._arm_current()
        joint, target = self._arm_joint_target(start, self.t.joint_interrupt_rad)
        ev.update(max_velocity=self._arm_speed(), goal_delta_rad=self.t.joint_interrupt_rad)
        run_id = self.target.start_action("arm.joint_position", {"position": target, "max_velocity": self._arm_speed()},
                                          True, timeout_s=self.t.action_timeout_s)
        def moved():
            self._arm_envelope_check(start, target)
            return abs(self._arm_current()[joint] - start[joint]) > 0.005
        moving, waited = self._wait(moved, self.t.move_detect_s)
        ev["move_detected_after_s"] = waited
        _check(bool(moving), "指令后未观察到关节运动")
        if how == "cancel":
            self._after_cancel(ev, run_id, self._joints_still)
        else:
            self._after_stop(ev, self._joints_still, "arm-stop")
        record = self._wait_run(run_id, self.t.run_settle_s)
        ev["run_status"] = record.get("status")
        stopped_at = self._arm_current()[joint]
        ev["swept_rad"] = abs(stopped_at - start[joint])
        _check(record.get("status") == "stopped", f"运行状态 {record.get('status')}（应为 stopped）")
        _check(abs(stopped_at - target[joint]) > 1e-3, "中断无效：关节仍到达了目标")
        self._ensure_unlatched()
        ev["restored"] = self._move_arm(start, self.t.action_timeout_s).get("status") == "succeeded"

    # gripper (gripper.state when declared, Q4-11)
    def _grip(self, opening: float) -> dict:
        run_id = self.target.start_action("gripper.position", {"opening": opening}, True, timeout_s=self.t.action_timeout_s)
        return self._wait_run(run_id, self.t.action_timeout_s + 5.0)

    def _gripper_opening(self) -> float | None:
        return self._read("gripper.state")["opening"] if "gripper.state" in self.caps else None

    def _motion_gripper_min(self, ev: dict) -> None:
        self._ensure_unlatched()
        first = self._grip(1.0)
        ev["open_first"] = {"status": first.get("status"), "result": first.get("result"),
                            "note": "先张开到 1.0 作为已知起点（若原本闭合，这一步幅度较大）"}
        _check(first.get("status") == "succeeded", f"张开夹爪失败：{first.get('status')} {first.get('error')}")
        target = 1.0 - self.t.gripper_step
        record = self._grip(target)
        opening = (record.get("result") or {}).get("opening")
        measured = self._gripper_opening()
        ev.update(target=target, status=record.get("status"), opening=opening, gripper_state=measured)
        _check(record.get("status") == "succeeded", f"最小开度变化未完成：{record.get('status')} {record.get('error')}")
        _check(isinstance(opening, (int, float)) and abs(opening - target) <= self.t.gripper_tolerance,
               f"反馈开度 {opening} 与目标 {target} 不符")
        if measured is None:
            ev["note"] = "未声明 gripper.state：只能核对动作自报的开度"
        else:
            _check(abs(measured - target) <= self.t.gripper_tolerance,
                   f"gripper.state 开度 {measured} 与目标 {target} 不符（动作自报 {opening}）")
        ev["restored"] = self._grip(1.0).get("status") == "succeeded"

    def _motion_gripper_interrupt(self, ev: dict, how: str) -> None:
        self._ensure_unlatched()
        if "gripper.state" not in self.caps:
            raise _Skip("未声明 gripper.state：无法验证夹爪停止后是否静止", BLOCKED)
        _check(self._grip(1.0).get("status") == "succeeded", "张开夹爪失败")
        target = 0.4
        run_id = self.target.start_action("gripper.position", {"opening": target}, True,
                                          timeout_s=self.t.action_timeout_s)
        moving, _ = self._wait(lambda: self._gripper_opening() < 1.0 - 0.005, self.t.move_detect_s, 0.005)
        _check(bool(moving), "指令后 gripper.state 未变化")
        t0 = time.monotonic()
        if how == "cancel":
            self._after_cancel(ev, run_id, self._gripper_still)
        else:
            self._after_stop(ev, self._gripper_still, "gripper-stop")
        record = self._wait_run(run_id, self.t.run_settle_s)
        stopped_at = self._gripper_opening()
        ev.update(run_status=record.get("status"), terminal_after_s=time.monotonic() - t0, stopped_at=stopped_at)
        _check(record.get("status") == "stopped", f"运行状态 {record.get('status')}（应为 stopped）")
        _check(stopped_at > target + self.t.gripper_tolerance, f"中断无效：开度 {stopped_at} 已到目标 {target}")
        self._ensure_unlatched()
        ev["restored"] = self._grip(1.0).get("status") == "succeeded"

    # arm.ee_pose (arm.ee_state when declared, Q4-11)
    def _motion_ee_min(self, ev: dict) -> None:
        self._ensure_unlatched()
        if "arm.ee_state" not in self.caps:
            raise _Skip("未声明 arm.ee_state：无法读取末端位姿，不能计算安全的最小位移", BLOCKED)
        start = self._read("arm.ee_state")["pose"]
        target = list(start)
        target[2] += 0.02
        reach = self._param("arm.ee_pose", "reach")
        _check(math.sqrt(sum(v * v for v in target[:3])) < reach, "末端离工作半径边界太近，无法安全测试")
        run_id = self.target.start_action("arm.ee_pose", {"pose": target, "max_velocity": 0.05}, True,
                                          timeout_s=self.t.action_timeout_s)
        record = self._wait_run(run_id, self.t.action_timeout_s + 5.0)
        after = self._read("arm.ee_state")["pose"]
        error = math.sqrt(sum((a - b) ** 2 for a, b in zip(after[:3], target[:3])))
        ev.update(start=start, target=target, feedback=after, error_m=error, status=record.get("status"))
        _check(record.get("status") == "succeeded", f"末端最小位移未完成：{record.get('status')} {record.get('error')}")
        _check(error <= 0.005, f"arm.ee_state 与目标相差 {error:.4f} m")
        back = self.target.start_action("arm.ee_pose", {"pose": start, "max_velocity": 0.05}, True,
                                        timeout_s=self.t.action_timeout_s)
        ev["restored"] = self._wait_run(back, self.t.action_timeout_s + 5.0).get("status") == "succeeded"

    # policy.action_chunk
    def _motion_chunk(self, ev: dict) -> None:
        self._ensure_unlatched()
        space = self._param("policy.action_chunk", "action_space")
        dim = self._param("policy.action_chunk", "dim")
        dt = self._chunk_dt()
        if space == "joint_position":
            start = self._arm_current()
            joint, final = self._arm_joint_target(start, self.t.joint_step_rad)
            steps = 5
            rows = [[s + (f - s) * (k + 1) / steps for s, f in zip(start, final)] for k in range(steps)]
        elif space == "base_velocity":
            if "base.odometry" not in self.caps:
                raise _Skip("需要 base.odometry", BLOCKED)
            v = min(self.t.base_speed, 0.25 * float(self._param("base.velocity", "max_vx", 0.4)))
            rows = [[v, 0.0, 0.0]] * 5
        else:
            self._na(f"运行时无法为动作空间 {space} 限幅")
        before = self._odom()["pose"] if space == "base_velocity" else None
        run_id = self.target.start_action("policy.action_chunk", {"actions": rows, "dt": dt}, True)
        if space == "joint_position":
            record = self._wait_run_arm(run_id, self.t.action_timeout_s, start, final)
        else:
            record = self._wait_run(run_id, self.t.action_timeout_s)
        ev.update(status=record.get("status"), result=record.get("result"), steps=len(rows), dt=dt, dim=dim)
        _check(record.get("status") == "succeeded", f"动作块未完成：{record.get('status')} {record.get('error')}")
        _check((record.get("result") or {}).get("executed") == len(rows), f"executed≠{len(rows)}：{record.get('result')}")
        if space == "joint_position":
            after = self._arm_current()
            error = max(abs(a - b) for a, b in zip(after, final))
            ev["error_rad"] = error
            _check(error <= self.t.joint_tolerance_rad * 5, f"动作块结束后关节与最后一步相差 {error:.4f} rad")
            self._move_arm(start, self.t.action_timeout_s, start=after)
        else:
            still, _ = self._wait(self._base_still, 2.0)
            after = self._odom()["pose"]
            yaw0 = _yaw(before)
            forward = math.cos(yaw0) * (after[0] - before[0]) + math.sin(yaw0) * (after[1] - before[1])
            ev["forward_m"] = forward
            _check(forward > 0.001, "动作块执行后底盘没有向前位移")
            _check(bool(still), "动作块结束后底盘未静止")

    # ============================================================ report
    def _report(self, started: float, duration: float) -> dict:
        manifest = self.manifest or {}
        simulated = manifest.get("simulated") is True
        levels: dict[str, dict] = {}
        for index, level in enumerate(LEVELS):
            cases = [c for c in self.cases if c.level == level]
            requested = index <= self.level_index
            counts = {s: sum(1 for c in cases if c.status == s) for s in (PASSED, FAILED, SKIPPED)}
            blocked = [c for c in cases if c.status == SKIPPED and c.skip_kind == BLOCKED]
            ran = requested and any(c.status != SKIPPED or c.skip_kind != BLOCKED for c in cases)
            ok = (requested and counts[FAILED] == 0 and not blocked and bool(cases)) if requested else None
            levels[level] = {"requested": requested, "ran": ran, "ok": ok, **counts, "blocked": len(blocked),
                             "blocked_reasons": sorted({c.reason for c in blocked})[:5]}
        # A level can only be ok when every lower level is ok.
        if levels["static"]["ok"] is not True:
            for level in ("dry", "motion"):
                if levels[level]["ok"] is not None:
                    levels[level]["ok"] = False
        if levels["dry"]["ok"] is False and levels["motion"]["ok"] is not None:
            levels["motion"]["ok"] = False
        capabilities = []
        for cid in self._capability_ids():
            try:
                spec = spec_caps.get(cid)
                kind, safety, title = spec.kind, spec.safety, spec.title
            except ValueError:
                kind, safety, title = ("command", "motion", "厂商扩展") if cid.startswith("x.") else (None, None, "未知能力")
            entry = {"id": cid, "title": title, "kind": kind, "safety": safety, "declared": cid in self.caps,
                     "levels": {}}
            for level in LEVELS[: self.level_index + 1]:
                cases = [c for c in self.cases if c.capability == cid and c.level == level]
                entry["levels"][level] = _verdict(cases)
            capabilities.append(entry)
        target = self.target.describe()
        end_state = self._end_state()
        return {
            "schema": SCHEMA, "conformance_id": self.run_id, "runtime_version": __version__,
            "spec_version": spec_caps.SPEC_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), "duration_s": round(duration, 3),
            "target": target,
            "robot": {k: manifest.get(k) for k in ("instance_id", "name", "vendor", "model", "family", "adapter_version")},
            "manifest_digest": manifest_digest(manifest) if self.manifest is not None else None,
            "manifest_v1_digest": manifest_v1_digest(manifest) if self.manifest is not None else None,
            "time_scale": self.claimed_time_scale,
            "executed_time_scale": self.executed_time_scale,
            "time_scale_rebased": self.time_scale_rebased,
            "trust": {
                "self_reported": True,
                "same_process_as_adapter": self.target.kind in ("adapter", "runtime"),
                "note": TRUST_NOTE,
            },
            "dry_guard": {"active_during_dry": self.target.guard_supported, "violations": list(self.guard_violations)},
            "simulated": simulated,
            "evidence_kind": "simulation" if simulated else "conformance",
            "field_acceptance": False, "disclaimer": DISCLAIMER,
            "level": self.level, "hardware_authorized": self.authorized,
            "static_ok": levels["static"]["ok"], "dry_ok": levels["dry"]["ok"], "motion_ok": levels["motion"]["ok"],
            "levels": levels,
            "summary": {s: sum(1 for c in self.cases if c.status == s) for s in (PASSED, FAILED, SKIPPED)},
            "warmup_s": self.warmup_s, "prelatched": self.prelatched,
            "aborted": (f"{self.aborted}；{end_state}" if self.aborted and end_state else self.aborted),
            "final_latched": self.final_latched,
            "final_motion": self.final_motion, "end_state": end_state, "restops": self.restops,
            "call_timeout": self._hung,
            "thresholds": asdict(self.t),
            "capabilities": capabilities,
            "cases": [c.to_dict() for c in self.cases],
        }


def _verdict(cases: list[_Case]) -> dict:
    if not cases:
        return {"status": SKIPPED, "reason": "该级别没有此能力的测试", "skip_kind": NOT_APPLICABLE}
    failed = [c for c in cases if c.status == FAILED]
    if failed:
        return {"status": FAILED, "reason": "；".join(f"{c.name}: {c.reason}" for c in failed)[:1000]}
    blocked = [c for c in cases if c.status == SKIPPED and c.skip_kind == BLOCKED]
    if any(c.status == PASSED for c in cases) and not blocked:
        return {"status": PASSED, "reason": ""}
    if blocked:
        return {"status": SKIPPED, "skip_kind": BLOCKED, "reason": "；".join(sorted({c.reason for c in blocked}))[:1000]}
    return {"status": SKIPPED, "skip_kind": NOT_APPLICABLE,
            "reason": "；".join(sorted({c.reason for c in cases}))[:1000]}


# ======================================================================= entry points
def run(target: Target, level: str = "static", authorized: bool = False, *,
        thresholds: Thresholds = THRESHOLDS, progress: Callable[[float], None] | None = None,
        run_id: str | None = None) -> dict:
    """Run the suite on ``target`` and return a ``conformance.report.v1`` dict."""
    return Suite(target, level, authorized, thresholds, progress, run_id).run()


def run_adapter(adapter, level: str = "static", authorized: bool = False, **kwargs) -> dict:
    """Conformance of a ``RobotAdapter`` instance (closes it afterwards)."""
    _level_index(level)
    if level == "motion" and authorized is not True:
        raise NotAuthorized("motion 级别会让机器人运动，需要 hardware_authorized=true（现场确认）")
    target = AdapterTarget(adapter)
    try:
        return run(target, level, authorized, **kwargs)
    finally:
        target.close()


class _ManifestOnly(Target):
    kind = "manifest"

    def __init__(self, fn: Callable[[], dict]):
        self._fn = fn

    def manifest(self):
        return copy.deepcopy(self._fn())


def check_manifest(manifest_fn: Callable[[], dict] | dict) -> dict:
    """Static level only, without any hardware (used by generated adapter tests)."""
    fn = manifest_fn if callable(manifest_fn) else (lambda: manifest_fn)
    return run(_ManifestOnly(fn), "static")


def summarize(report: dict) -> str:
    """Short human-readable summary (CLI)."""
    lines = [f"一致性测试 {report['robot'].get('name')}（{report['target'].get('kind')}）级别 {report['level']}"
             + ("  [仿真本体]" if report["simulated"] else "")]
    for level in LEVELS:
        info = report["levels"][level]
        if not info["requested"]:
            continue
        mark = "通过" if info["ok"] else "未通过"
        lines.append(f"  {level}: {mark}（passed {info['passed']} / failed {info['failed']} / skipped {info['skipped']}"
                     f"，其中 blocked {info['blocked']}）")
    for case in report["cases"]:
        if case["status"] == FAILED or (case["status"] == SKIPPED and case.get("skip_kind") == BLOCKED):
            lines.append(f"  - {case['status']} {case['id']}：{case['reason']}")
    if report.get("aborted"):
        lines.append(f"  中止：{report['aborted']}")
    fm = report.get("final_motion") or {}
    if report.get("aborted") and fm.get("still") is not True:
        lines.append("  !!! 未能确认本体已静止：请立即使用硬件急停 !!!")
    lines.append("  注意：" + DISCLAIMER)
    return "\n".join(lines)


# ======================================================================= runtime-side runs (/v2/conformance)
class ConformanceRuns:
    """Asynchronous runs started by ``POST /v2/conformance`` (one at a time)."""

    KIND = "conformance"

    def __init__(self, runtime):
        self.runtime = runtime
        self._lock = threading.Lock()
        self._live: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}

    def recover_after_restart(self) -> list[str]:
        marked = []
        for record in self.runtime.store.list(self.KIND):
            if record.get("status") == "running":
                record.update(status="unknown", error="运行时重启时一致性测试未结束；结果未知（不会自动重跑）")
                self.runtime.store.put(self.KIND, record)
                marked.append(record["id"])
        return marked

    def start(self, payload: dict) -> dict:
        level = payload.get("level", "static")
        if not isinstance(level, str):
            raise BadRequest("level 须为字符串")
        _level_index(level)
        authorized = payload.get("hardware_authorized") is True
        if level == "motion" and not authorized:
            raise NotAuthorized("motion 级别会让机器人运动，需要 hardware_authorized=true（现场确认）")
        with self._lock:
            busy = [r for r in self._live.values() if r["status"] == "running"]
            if busy:
                raise Conflict(f"一致性测试 {busy[0]['id']} 正在执行", code="conformance_busy")
            if level != "static" and self.runtime.runner.active_runs():
                raise Conflict("有运行未结束；一致性测试需要空闲的本体", code="runs_active")
            cid = "cf-" + uuid.uuid4().hex[:12]
            record = {"id": cid, "conformance_id": cid, "status": "running", "level": level,
                      "hardware_authorized": authorized, "progress": 0.0, "created_at": time.time(),
                      "simulated": self.runtime.manifest["simulated"], "report": None, "error": None}
            self._live[cid] = record
            self.runtime.store.put(self.KIND, record)
        thread = threading.Thread(target=self._work, args=(record,), name=f"nx-{cid}", daemon=True)
        self._threads[cid] = thread
        thread.start()
        return {"accepted": True, "conformance_id": cid, "run_id": cid, "status": "running", "level": level,
                "simulated": record["simulated"]}

    def _work(self, record: dict) -> None:
        def progress(fraction: float) -> None:
            record["progress"] = round(fraction, 3)
        import secrets
        lease = secrets.token_hex(16)
        self.runtime.conformance_lease = lease           # other motion sources get 409 meanwhile (Q4-15)
        self.runtime.conformance_refused = 0
        try:
            report = run(RuntimeTarget(self.runtime, lease=lease), record["level"], record["hardware_authorized"],
                         progress=progress, run_id=record["id"])
            report["external_motion_refused"] = self.runtime.conformance_refused
            record.update(status="completed", report=report, progress=1.0)
        except RuntimeApiError as exc:
            record.update(status="failed", error=exc.to_dict())
        except Exception as exc:
            record.update(status="failed", error={"error": f"{type(exc).__name__}: {exc}", "code": "internal_error"})
        finally:
            self.runtime.conformance_lease = None
        record["ended_at"] = time.time()
        try:
            self.runtime.store.put(self.KIND, record)
        except Exception as exc:
            self.runtime.events.emit("runtime.monitor_error", error=f"conformance persist: {exc}")

    def get(self, cid: str) -> dict:
        record = self._live.get(cid) or self.runtime.store.get(self.KIND, cid)
        if record is None:
            raise NotFound(f"找不到一致性测试 {cid}", code="conformance_not_found")
        return dict(record)

    def list(self) -> list[dict]:
        records = {r["id"]: r for r in self.runtime.store.list(self.KIND)}
        records.update(self._live)
        out = []
        for r in sorted(records.values(), key=lambda r: r["created_at"], reverse=True)[:50]:
            report = r.get("report") or {}
            out.append({"id": r["id"], "status": r["status"], "level": r["level"], "created_at": r["created_at"],
                        "static_ok": report.get("static_ok"), "dry_ok": report.get("dry_ok"),
                        "motion_ok": report.get("motion_ok"), "simulated": r.get("simulated")})
        return out

    def wait(self, cid: str, timeout: float) -> dict:
        thread = self._threads.get(cid)
        if thread is not None:
            thread.join(timeout)
        return self.get(cid)
