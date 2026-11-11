"""Safety supervisor: the software safety layer between skills and the adapter.

Responsibilities
----------------
1. **Authorization** — every ``motion`` capability needs
   ``hardware_authorized=true``.
2. **Limits** — commands are limited or rejected using the parameters the
   body declares in its manifest (``max_vx``, joint ``lower``/``upper``, ...).
   Limiting a value is recorded as a ``safety.limited`` event; a command
   that cannot be made safe by limiting is rejected.
3. **Watchdog** — a ``base.velocity`` setpoint expires after ``timeout_s``;
   if it is not refreshed the supervisor sends zero velocity.
4. **E-stop latch** — after a stop, all motion is refused until an operator
   calls ``reset(hardware_authorized=True)``.
5. **Stop epoch** — every stop increments a monotonic epoch. A caller records
   the epoch when its request/run was admitted and passes it with every
   call; a call whose epoch is no longer current is refused before it can
   reach the adapter (and stopped again if it raced into the adapter). So a
   command issued before a stop can never move the robot after a reset.
   A reset advances the epoch too, so a request that *arrived* before the
   reset (e.g. while latched, then delayed by a slow disk) is stale after it.
6. **Defensive limits** — the manifest's limit parameters are deep-copied,
   validated again here (finite, positive, joint-limit lengths equal
   ``dof``) and frozen (lists become tuples); a bad manifest refuses to
   start, and later changes to the adapter's own objects have no effect.
   Any limiter error fails closed (rejection), comparisons are NaN-safe,
   and every value sent to the adapter is checked to be finite.
7. **Halt channel** — the watchdog normally sends zero velocity under the
   velocity lock; if a velocity command is stuck inside the adapter holding
   that lock, the watchdog does not wait: it calls ``adapter.stop()`` and
   sends zero velocity *without* the lock. Adapters must therefore allow
   ``stop()`` and a zero ``base.velocity`` to be called concurrently with an
   in-flight ``invoke`` (see hal.RobotAdapter).

The stop path (:meth:`emergency_stop`) sets a ``threading.Event`` and calls
``adapter.stop()`` directly. It takes no lock that a command or skill could
hold, touches no queue, database, model or network.

This is a *software* layer. It does not replace a hardware e-stop, and it
can only enforce limits the manifest declares.
"""
from __future__ import annotations

import copy
import itertools
import math
import threading
import time
from types import MappingProxyType
from typing import Any

from neuxmind_spec import capabilities as spec_caps

from .errors import Conflict, EStopLatched, NotAuthorized, SafetyRejected
from .events import EventBus

DEFAULT_VELOCITY_TIMEOUT_S = 0.5
MIN_VELOCITY_TIMEOUT_S = 0.05
MAX_VELOCITY_TIMEOUT_S = 2.0
_EPS = 1e-9

# policy.action_chunk timing limits (N-06): the per-step velocity limit is
# max_velocity * dt, so dt must be bounded or the limit can be inflated.
CHUNK_MAX_DT_PERIODS = 2.0       # dt <= 2 / rate_hz when the body declares rate_hz
CHUNK_MAX_DT_DEFAULT_S = 0.5     # dt <= 0.5 s when it does not
CHUNK_MAX_DURATION_S = 10.0      # len(actions) * dt
CHUNK_MAX_STEPS = 1000


def freeze(value: Any) -> Any:
    """Deep, immutable copy: lists → tuples, dicts → read-only mappings."""
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """Plain JSON-style copy of a frozen value (for skills)."""
    if isinstance(value, (dict, MappingProxyType)):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


def is_motion(cap_id: str) -> bool:
    if cap_id.startswith("x."):
        # Vendor extensions are treated as motion unless proven otherwise.
        return True
    return spec_caps.get(cap_id).safety == "motion"


def _all_finite(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True          # Python ints are exact; huge ones are rejected by float() in the limiters
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_all_finite(v) for v in value)
    if isinstance(value, dict):
        return all(_all_finite(v) for v in value.values())
    return True


class _LimitError(Exception):
    """Internal: a limiter met an impossible bound or value. Always becomes a rejection."""


def _clamp(value: float, low: float, high: float) -> float:
    if not (math.isfinite(value) and math.isfinite(low) and math.isfinite(high)):
        raise _LimitError(f"限幅输入非有限值（value={value}, low={low}, high={high}）")
    if not low <= high:
        raise _LimitError(f"限幅区间无效：low={low} > high={high}")
    return max(low, min(high, value))


def _positive(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


# Limit parameters that the supervisor relies on. (cap, param) -> "positive" | "nonnegative".
_NUMERIC_LIMITS = {
    ("base.velocity", "max_vx"): "positive", ("base.velocity", "max_vy"): "nonnegative",
    ("base.velocity", "max_wz"): "positive",
    ("arm.joint_position", "max_velocity"): "positive", ("arm.ee_pose", "reach"): "positive",
    ("gripper.position", "max_force"): "positive", ("policy.action_chunk", "rate_hz"): "positive",
}


def validate_safety_params(params: dict[str, dict], limits: dict) -> None:
    """Re-check the manifest values the supervisor enforces. Raises ValueError.

    ``spec.validate_manifest_v2`` already checks these, but an adapter (or a
    caller constructing the supervisor directly) can bypass it; a wrong sign
    or NaN here would be amplified into dangerous motion, so refuse to start.
    """
    for (cap, name), kind in _NUMERIC_LIMITS.items():
        if cap not in params or params[cap].get(name) is None:
            continue
        value = params[cap][name]
        ok = _positive(value) if kind == "positive" else (_finite_number(value) and value >= 0)
        if not ok:
            raise ValueError(f"清单安全参数无效：{cap}.{name}={value!r}（须为{'大于' if kind == 'positive' else '不小于'} 0 的有限数值）")
    for cap in ("arm.joint_position", "joint.state"):
        dof = params.get(cap, {}).get("dof")
        if dof is not None and (isinstance(dof, bool) or not isinstance(dof, int) or dof <= 0):
            raise ValueError(f"清单安全参数无效：{cap}.dof={dof!r}（须为正整数）")
    arm = params.get("arm.joint_position")
    if arm is not None:
        lower, upper, dof = arm.get("lower"), arm.get("upper"), arm.get("dof")
        if (lower is None) != (upper is None):
            raise ValueError("清单安全参数无效：arm.joint_position 须同时声明 lower 与 upper")
        if lower is not None:
            for label, seq in (("lower", lower), ("upper", upper)):
                if not isinstance(seq, list) or not all(_finite_number(v) for v in seq):
                    raise ValueError(f"清单安全参数无效：arm.joint_position.{label} 须为有限数值列表")
            if dof is None or len(lower) != dof or len(upper) != dof:
                raise ValueError(f"清单安全参数无效：arm.joint_position lower/upper 长度"
                                 f"（{len(lower)}/{len(upper)}）须等于 dof={dof}")
            if any(lo >= hi for lo, hi in zip(lower, upper)):
                raise ValueError("清单安全参数无效：arm.joint_position 每个关节须满足 lower < upper")
    chunk = params.get("policy.action_chunk")
    if chunk is not None:
        dim = chunk.get("dim")
        if dim is not None and (isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0):
            raise ValueError(f"清单安全参数无效：policy.action_chunk.dim={dim!r}（须为正整数）")
        if chunk.get("action_space") == "joint_position" and arm is not None and arm.get("lower") is not None:
            if dim != len(arm["lower"]):
                raise ValueError(f"清单安全参数无效：policy.action_chunk.dim={dim} 与机械臂自由度 "
                                 f"{len(arm['lower'])} 不一致")
    max_speed = limits.get("max_linear_speed")
    if max_speed is not None and not _positive(max_speed):
        raise ValueError(f"清单安全参数无效：safety.max_linear_speed={max_speed!r}（须为大于 0 的有限数值）")
    workspace = limits.get("workspace")
    if workspace is not None:
        if not isinstance(workspace, dict):
            raise ValueError("清单安全参数无效：safety.workspace 须为对象")
        for axis, bounds in workspace.items():
            if (not isinstance(bounds, list) or len(bounds) != 2 or not all(_finite_number(b) for b in bounds)
                    or bounds[0] >= bounds[1]):
                raise ValueError(f"清单安全参数无效：safety.workspace.{axis}={bounds!r}（须为 [min, max] 有限数值）")


class SafetySupervisor:
    def __init__(self, manifest: dict, adapter, events: EventBus, clock):
        self._adapter = adapter
        self._events = events
        self._clock = clock
        # Deep copy -> validate -> freeze. Nothing the adapter does to its own
        # objects afterwards can change the limits enforced here (N-04).
        params = {c["id"]: copy.deepcopy(dict(c.get("params", {}))) for c in manifest["capabilities"]}
        limits = copy.deepcopy(dict(manifest.get("safety", {})))
        validate_safety_params(params, limits)    # refuse to start on a bad manifest
        self.params = {cap: freeze(p) for cap, p in params.items()}
        self.limits = freeze(limits)
        self.manifest_drift: str | None = None    # set when the adapter's manifest changes at runtime

        self._latched = threading.Event()
        self.latch_reason: str | None = None
        # Stop epoch: incremented (atomically, via itertools.count under the GIL)
        # on every emergency stop. Never decreases, never reset.
        self._epoch_counter = itertools.count(1)
        self._epoch = 0
        # Number of stops so far (only stops; the epoch also moves on reset).
        self._stop_counter = itertools.count(1)
        self._stop_seq = 0
        # R4b-01: every adapter stop (latching or not) bumps this; a reset compares it across
        # adapter.reset() so a non-latching halt that lands while reset() runs is a race too.
        self._halt_counter = itertools.count(1)
        self._halt_seq = 0
        # Q7c-02 (round 07 third fix pass): successful resets so far -- a latch after a reset is a
        # new latch (the latch guard's drift notice belongs to one latch), even if nobody sampled
        # the unlatched state in between.
        self._reset_seq = 0
        # R4c-01: at most one reset at a time. Held for the whole of reset() -- including an
        # adapter.reset() that a caller (e.g. the conformance suite) has given up waiting for --
        # so a second reset can never enter adapter.reset() concurrently, and memory/disk latch
        # state are only ever written by one reset. Never taken by the stop path.
        self._reset_lock = threading.Lock()
        self._reset_started: float | None = None     # time.monotonic() when the in-flight reset began
        self._guard_restop_in_reset = False           # Q5-07: the latch guard re-stopped during this reset
        # Q7-10: a reset whose adapter.reset() returns later than this (wall s) is expired: the
        # operator was told the outcome is unknown long ago and may have walked up to the body,
        # so it must not unlatch by itself. 0/None disables (tests only).
        self.reset_expire_s: float | None = self.RESET_EXPIRE_S
        self._stop_lock = threading.Lock()            # R4c-02: compare-and-set of the latch reason only
        self._stop_record: tuple[int, str | None] = (0, None)

        # Serialises velocity setpoints with the watchdog so the watchdog
        # never overwrites a fresher command. The stop path does NOT use it.
        self.velocity_lock = threading.Lock()
        self._velocity_deadline: float | None = None

    # ------------------------------------------------------------ state
    @property
    def latched(self) -> bool:
        return self._latched.is_set()

    @property
    def reset_in_progress(self) -> bool:
        """True while a reset (in particular ``adapter.reset()``) is running (R4c-01)."""
        return self._reset_started is not None

    @property
    def reset_in_flight_s(self) -> float | None:
        started = self._reset_started
        return None if started is None else max(0.0, time.monotonic() - started)

    @property
    def stop_count(self) -> int:
        """Number of emergency stops since start (never decreases). Lets a caller tell its own
        stop from anyone else's (conformance suite, Q4-01)."""
        return self._stop_seq

    @property
    def reset_count(self) -> int:
        """Number of successful resets since start (never decreases; Q7c-02)."""
        return self._reset_seq

    @property
    def epoch(self) -> int:
        """Current stop epoch. Record it when admitting a request or run."""
        return self._epoch

    def ensure_epoch(self, epoch: int | None, cap_id: str = "") -> None:
        """Raise if a stop happened since ``epoch`` was recorded (``None`` = no check)."""
        if epoch is not None and epoch != self._epoch:
            self._events.emit("safety.stale_rejected", capability=cap_id, call_epoch=epoch,
                              current_epoch=self._epoch)
            raise EStopLatched("该调用发起后发生过停止；停止之前发起的指令永远不会再下发，请重新发起",
                               code="stale_after_stop")

    # --------------------------------------------------------- checking
    def check(self, cap_id: str, args: dict, authorized: bool, context: dict | None = None,
              epoch: int | None = None) -> tuple[dict, list[dict]]:
        """Return ``(limited_args, notes)`` or raise.

        ``context`` may carry extra robot state needed for limiting
        (e.g. ``{"joint_position": [...]}`` for action chunks). ``epoch`` is
        the stop epoch recorded when the call (or its run) was admitted.
        """
        if not is_motion(cap_id):
            return args, []
        if self.latched:
            raise EStopLatched(f"急停已锁存（{self.latch_reason or 'stop'}）；需经现场授权 reset 后才能运动")
        self.ensure_epoch(epoch, cap_id)
        if self.manifest_drift:
            raise SafetyRejected(f"本体能力清单在运行中发生变化（{self.manifest_drift}）；"
                                 "为安全起见拒绝一切运动，请检查本体后重启运行时", code="manifest_changed")
        if authorized is not True:
            raise NotAuthorized(f"{cap_id} 会使机器人运动，需要 hardware_authorized=true")
        if not _all_finite(args):
            self._reject(cap_id, "参数包含 NaN 或无穷大")
        limiter = getattr(self, "_limit_" + cap_id.replace(".", "_"), None)
        if limiter is None:
            if cap_id.startswith("x."):
                self._reject(cap_id, "运行时无法为厂商扩展运动能力限幅，拒绝执行")
            return dict(args), []
        notes: list[dict] = []
        try:
            limited = limiter(dict(args), notes, context or {})
        except (_LimitError, ValueError, TypeError, IndexError, KeyError, OverflowError) as exc:
            # Fail closed: a limiter that cannot compute a safe value rejects.
            self._reject(cap_id, f"无法安全限幅：{exc}")
        if not _all_finite(limited):
            self._reject(cap_id, "限幅结果包含 NaN 或无穷大")
        for note in notes:
            self._events.emit("safety.limited", capability=cap_id, **note)
        return limited, notes

    def _reject(self, cap_id: str, reason: str):
        self._events.emit("safety.rejected", capability=cap_id, reason=reason)
        raise SafetyRejected(f"{cap_id} 被安全监督器拒绝：{reason}")

    def _param(self, cap_id: str, name: str, required: bool = True):
        value = self.params.get(cap_id, {}).get(name)
        if value is None and required:
            self._reject(cap_id, f"清单未声明 {name}，拒绝无限制运动")
        return value

    # ----------------------------------------------------------- limiters
    def _limit_base_velocity(self, args: dict, notes: list, _ctx: dict) -> dict:
        cap = "base.velocity"
        holonomic = self.params.get(cap, {}).get("holonomic", False)
        for name, pname in (("vx", "max_vx"), ("vy", "max_vy"), ("wz", "max_wz")):
            if name not in args:
                continue
            value = float(args[name])
            if name == "vy" and not holonomic:
                if value != 0.0:
                    notes.append({"field": "vy", "requested": value, "applied": 0.0, "reason": "非全向底盘"})
                args["vy"] = 0.0
                continue
            if value == 0.0:
                args[name] = 0.0
                continue
            limit = float(self._param(cap, pname))
            applied = _clamp(value, -limit, limit)
            if applied != value:
                notes.append({"field": name, "requested": value, "applied": applied, "limit": limit})
            args[name] = applied
        # Optional overall linear speed cap from manifest.safety.
        max_speed = self.limits.get("max_linear_speed")
        if isinstance(max_speed, (int, float)) and max_speed > 0:
            vx, vy = args.get("vx", 0.0), args.get("vy", 0.0)
            speed = math.hypot(vx, vy)
            if speed > max_speed:
                scale = max_speed / speed
                notes.append({"field": "speed", "requested": speed, "applied": max_speed, "limit": max_speed})
                args["vx"] = vx * scale
                if "vy" in args:
                    args["vy"] = vy * scale
        timeout = float(args.get("timeout_s", DEFAULT_VELOCITY_TIMEOUT_S))
        applied = _clamp(timeout, MIN_VELOCITY_TIMEOUT_S, MAX_VELOCITY_TIMEOUT_S)
        if "timeout_s" in args and applied != timeout:
            notes.append({"field": "timeout_s", "requested": timeout, "applied": applied})
        args["timeout_s"] = applied
        return args

    def _limit_arm_joint_position(self, args: dict, notes: list, _ctx: dict) -> dict:
        cap = "arm.joint_position"
        dof = self._param(cap, "dof")
        lower = self._param(cap, "lower")
        upper = self._param(cap, "upper")
        position = args["position"]
        if len(position) != dof:
            self._reject(cap, f"需要 {dof} 个关节角，收到 {len(position)} 个")
        if len(lower) != dof or len(upper) != dof:
            # Never zip-truncate: every joint must have a limit.
            self._reject(cap, f"关节限位长度（{len(lower)}/{len(upper)}）与自由度 {dof} 不一致")
        for i, (q, lo, hi) in enumerate(zip(position, lower, upper)):
            if not (lo - _EPS <= q <= hi + _EPS):        # NaN-safe: NaN anywhere → reject
                self._reject(cap, f"关节 {i} 目标 {q:.4f} rad 超出限位 [{lo}, {hi}]")
        vmax = float(self._param(cap, "max_velocity"))
        requested = float(args.get("max_velocity", vmax))
        if requested <= 0:
            self._reject(cap, "max_velocity 必须大于 0")
        applied = min(requested, vmax)
        if applied != requested:
            notes.append({"field": "max_velocity", "requested": requested, "applied": applied, "limit": vmax})
        args["max_velocity"] = applied
        return args

    def _limit_gripper_position(self, args: dict, notes: list, _ctx: dict) -> dict:
        cap = "gripper.position"
        opening = args["opening"]
        if not 0.0 <= opening <= 1.0:
            self._reject(cap, f"opening={opening} 须在 0–1 之间")
        fmax = self.params.get(cap, {}).get("max_force")
        if fmax is not None and "max_force" in args:
            requested = float(args["max_force"])
            applied = _clamp(requested, 0.0, float(fmax))
            if applied != requested:
                notes.append({"field": "max_force", "requested": requested, "applied": applied, "limit": fmax})
            args["max_force"] = applied
        return args

    def _limit_legged_posture(self, args: dict, notes: list, _ctx: dict) -> dict:
        supported = self.params.get("legged.posture", {}).get("supported")
        if isinstance(supported, (list, tuple)) and args["posture"] not in supported:
            self._reject("legged.posture", f"本体不支持姿态 {args['posture']}，支持 {supported}")
        return args

    def _limit_base_navigate_to(self, args: dict, notes: list, _ctx: dict) -> dict:
        cap = "base.navigate_to"
        if "tolerance" in args and args["tolerance"] <= 0:
            self._reject(cap, "tolerance 必须大于 0")
        qx, qy, qz, qw = args["goal"][3:7]
        if abs(math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw) - 1.0) > 1e-3:
            self._reject(cap, "goal 四元数未归一化")
        workspace = self.limits.get("workspace")
        if isinstance(workspace, (dict, MappingProxyType)):
            for axis, index in (("x", 0), ("y", 1)):
                bounds = workspace.get(axis)
                if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                    value = args["goal"][index]
                    if not bounds[0] <= value <= bounds[1]:
                        self._reject(cap, f"目标 {axis}={value} 超出允许工作区 {bounds}")
        return args

    def _limit_arm_ee_pose(self, args: dict, notes: list, _ctx: dict) -> dict:
        cap = "arm.ee_pose"
        reach = self._param(cap, "reach")
        x, y, z = args["pose"][0:3]
        if not (math.sqrt(x * x + y * y + z * z) <= reach + _EPS):
            self._reject(cap, f"目标超出工作半径 {reach} m")
        return args

    def _limit_policy_action_chunk(self, args: dict, notes: list, ctx: dict) -> dict:
        """Limit every step of a policy action chunk (e.g. from a VLA model)."""
        cap = "policy.action_chunk"
        space = self._param(cap, "action_space")
        dim = self._param(cap, "dim")
        rate = self.params.get(cap, {}).get("rate_hz")
        actions = args["actions"]
        dt = float(args["dt"])
        if dt <= 0:
            self._reject(cap, "dt 必须大于 0")
        if not math.isfinite(dt):
            self._reject(cap, "dt 须为有限数值")
        has_rate = isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0
        if has_rate and dt < 1.0 / rate - _EPS:
            self._reject(cap, f"dt={dt} 小于本体控制周期 {1.0 / rate:.4f} s")
        # N-06: an unbounded dt inflates the per-step limit max_velocity·dt.
        max_dt = CHUNK_MAX_DT_PERIODS / rate if has_rate else CHUNK_MAX_DT_DEFAULT_S
        if dt > max_dt + _EPS:
            self._reject(cap, f"dt={dt} 超过上限 {max_dt:.4f} s"
                              f"（{'2 个控制周期' if has_rate else '本体未声明 rate_hz 时的保守上限'}）")
        if not isinstance(actions, list) or not actions:
            self._reject(cap, "actions 须为非空二维数组")
        if len(actions) > CHUNK_MAX_STEPS:
            self._reject(cap, f"动作块最多 {CHUNK_MAX_STEPS} 步，收到 {len(actions)} 步")
        if len(actions) * dt > CHUNK_MAX_DURATION_S + _EPS:
            self._reject(cap, f"动作块总时长 {len(actions) * dt:.2f} s 超过上限 {CHUNK_MAX_DURATION_S} s")
        for row in actions:
            if (not isinstance(row, list) or len(row) != dim
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in row)):
                self._reject(cap, f"actions 每一步须为 {dim} 个数值")
        clamped = 0
        limited: list[list[float]] = []
        if space == "joint_position":
            arm = "arm.joint_position"
            lower = self._param(arm, "lower")
            upper = self._param(arm, "upper")
            vmax = float(self._param(arm, "max_velocity"))
            if len(lower) != dim or len(upper) != dim:
                self._reject(cap, "动作维度与机械臂关节限位长度不一致")
            max_step = vmax * dt
            previous = ctx.get("joint_position")
            # Fail closed: the per-step velocity limit needs the current joint
            # positions as the starting point (README "以当前 joint.state 为起点").
            if previous is None:
                reason = ctx.get("joint_state_error") or "本体未提供 joint.state"
                self._reject(cap, f"无法读取当前关节位置（{reason}），不能对动作块逐步限速")
            if (not isinstance(previous, (list, tuple)) or len(previous) != dim
                    or not all(_finite_number(v) for v in previous)):
                self._reject(cap, f"joint.state 位置数组须为 {dim} 个有限数值，"
                                  f"收到 {len(previous) if isinstance(previous, (list, tuple)) else type(previous).__name__}")
            for row in actions:
                out = []
                for i, q in enumerate(row):
                    target = _clamp(float(q), lower[i], upper[i])
                    target = _clamp(target, previous[i] - max_step, previous[i] + max_step)
                    if target != q:
                        clamped += 1
                    out.append(target)
                limited.append(out)
                previous = out
        elif space == "base_velocity":
            if dim != 3:
                self._reject(cap, "base_velocity 动作空间须为 3 维 (vx, vy, wz)")
            for row in actions:
                step = self._limit_base_velocity(
                    {"vx": float(row[0]), "vy": float(row[1]), "wz": float(row[2])}, [], {})
                out = [step["vx"], step["vy"], step["wz"]]
                clamped += sum(1 for a, b in zip(row, out) if a != b)
                limited.append(out)
        else:
            self._reject(cap, f"运行时无法为动作空间 {space} 逐步限幅，拒绝执行")
        if clamped:
            notes.append({"field": "actions", "clamped_values": clamped, "steps": len(actions)})
        args["actions"] = limited
        return args

    # ------------------------------------------------- after dispatch
    def after_dispatch(self, cap_id: str, args: dict, handle=None, epoch: int | None = None) -> None:
        """Run right after a motion command reached the adapter.

        Closes the check-then-act race with the stop path: if a stop latched
        while this command was in flight -- or a stop *and a reset* both
        happened (the epoch moved) -- stop the adapter again.
        """
        if not is_motion(cap_id):
            return
        if self.latched or (epoch is not None and epoch != self._epoch):
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:
                    pass
            self._adapter_stop()
            raise EStopLatched("下发指令时急停已触发；已再次停止")
        if cap_id == "base.velocity":
            moving = any(abs(args.get(k, 0.0)) > 0.0 for k in ("vx", "vy", "wz"))
            self._velocity_deadline = (self._clock.now() + args["timeout_s"]) if moving else None
        elif cap_id == "base.navigate_to":
            # Navigation supersedes any streamed velocity setpoint.
            self._velocity_deadline = None

    # --------------------------------------------------------- watchdog
    def watchdog_tick(self) -> bool:
        """Send zero velocity if the last setpoint expired. Returns True if fired.

        Called periodically by the runtime monitor thread (and by tests).
        """
        deadline = self._velocity_deadline
        if deadline is None or self._clock.now() < deadline:
            return False
        if not self.velocity_lock.acquire(timeout=0.01):
            # A velocity command is inside the adapter holding the lock -- possibly
            # stuck (serial retransmit, hung driver). Never wait for it (N-03):
            # use the halt channel, which by contract may run concurrently.
            return self._halt_unlocked(deadline)
        try:
            deadline = self._velocity_deadline
            if deadline is None or self._clock.now() < deadline:
                return False
            self._velocity_deadline = None
            try:
                self._adapter.invoke("base.velocity", {"vx": 0.0, "vy": 0.0, "wz": 0.0})
                ok = True
            except Exception:
                ok = self._adapter_stop()
            self._events.emit("safety.watchdog", capability="base.velocity", zeroed=ok,
                              reason="速度指令超时未刷新，已发送零速度")
            return True
        finally:
            self.velocity_lock.release()

    def _halt_unlocked(self, deadline: float) -> bool:
        if self._velocity_deadline != deadline:
            return False          # a fresh setpoint just replaced the expired one
        self._velocity_deadline = None
        ok = self._adapter_stop()
        sent = self._send_zero_velocity_async()
        self._events.emit("safety.watchdog", capability="base.velocity", zeroed=ok or sent, unlocked=True,
                          reason="速度指令超时未刷新，且有速度指令卡在适配器中；已不等锁直接停车并发送零速度")
        return True

    def _send_zero_velocity_async(self) -> bool:
        """Zero velocity on a short-lived thread so a misbehaving adapter cannot freeze the watchdog."""
        def send():
            try:
                self._adapter.invoke("base.velocity", {"vx": 0.0, "vy": 0.0, "wz": 0.0})
            except Exception:
                pass
        try:
            threading.Thread(target=send, name="nx-halt-zero", daemon=True).start()
            return True
        except RuntimeError:
            return False

    # ------------------------------------------------------------- stop
    def _adapter_stop(self) -> bool:
        self._halt_seq = next(self._halt_counter)
        try:
            self._adapter.stop()
            return True
        except Exception:
            return False

    def emergency_stop(self, reason: str = "stop", source: str = "api", keep_reason: bool = False) -> dict:
        """Latch and stop. Never blocks on locks, queues, storage or network.

        ``keep_reason`` (R4b-01/R4b-08): when already latched, the existing latch reason (e.g. the
        operator's) is kept; the stop is still counted and the adapter still told to stop."""
        previous = self.latch_reason if self._latched.is_set() else None
        self._stop_seq = next(self._stop_counter)
        self._epoch = next(self._epoch_counter)   # invalidates every call admitted before now
        self._latched.set()
        self.latch_reason = previous if (keep_reason and previous) else reason
        self._stop_record = (self._stop_seq, self.latch_reason)   # one tuple: read by set_reason_if
        self._velocity_deadline = None
        count = self._stop_seq
        ok = self._adapter_stop()
        self._events.emit("safety.estop", reason=reason, source=source, adapter_stopped=ok)
        return {"stopped": ok, "latched": True, "stop_count": count}

    def set_reason_if(self, reason: str, *, stop_count: int, expect_reason: str | None) -> bool:
        """R4c-02: compare-and-set of the latch reason. Writes ``reason`` only if still latched, no
        stop happened after stop number ``stop_count`` and the current reason equals
        ``expect_reason``. Returns whether it wrote. (The stop path does not take ``_stop_lock``; a
        stop landing inside this method is detected by the stop-count re-check and wins.)"""
        with self._stop_lock:
            if (not self._latched.is_set() or self._stop_seq != stop_count
                    or self.latch_reason != expect_reason):
                return False
            self.latch_reason = reason
            if self._stop_seq != stop_count:
                # A stop landed between the check and the write: its reason wins (restore it if it
                # had already been written; otherwise its own write comes after ours).
                seq, theirs = self._stop_record
                if seq == self._stop_seq:
                    self.latch_reason = theirs
                return False
            return True

    def latched_restop(self) -> bool:
        """R4b-04 latched-state guard: tell the adapter to stop again while latched. Not counted,
        never latches/unlatches, never sends a motion command, never touches the latch reason."""
        if not self._latched.is_set():
            return False
        if self._reset_started is not None:
            self._guard_restop_in_reset = True     # Q5-07: so the failed reset can say who stopped it
        self._velocity_deadline = None
        return self._adapter_stop()

    def halt_motion(self, reason: str) -> bool:
        """Non-latching motion halt, used when a run is cancelled or times out."""
        self._velocity_deadline = None
        ok = self._adapter_stop()
        self._events.emit("safety.halt", reason=reason, adapter_stopped=ok)
        return ok

    def reset(self, authorized: bool, source: str = "api", persist=None, expect_stop_count: int | None = None,
              expire_s: float | None = None) -> dict:
        """Clear the latch.

        ``persist(latched, reason, epoch)`` is called *before* the in-memory
        flag is cleared, so disk never says "unlatched" while memory says
        latched. The epoch is advanced first, so every request or run that
        arrived before this reset (in particular while latched) is stale
        afterwards (N-02). A stop racing with the reset wins.

        Q7-10: an ``adapter.reset()`` that returns after ``expire_s`` (the caller's own waiting
        window, clamped to [1, :attr:`reset_expire_s`]; default :attr:`reset_expire_s`) never
        unlatches -- see :meth:`_expire_reset`.
        """
        if authorized is not True:
            raise NotAuthorized("解除急停锁存需要 hardware_authorized=true（确认现场安全）")
        if not self._reset_lock.acquire(blocking=False):
            raise self.reset_in_progress_error()
        self._guard_restop_in_reset = False
        self._reset_started = time.monotonic()
        try:
            return self._reset_locked(source, persist, expect_stop_count, expire_s)
        finally:
            self._reset_started = None
            self._reset_lock.release()

    RESET_STUCK_HINT_S = 10.0     # after this long /v1/status and the 409 body carry the operator hint
    RESET_EXPIRE_S = 30.0         # Q7-10: an adapter.reset() returning later than this never unlatches

    def reset_hint(self, age: float | None) -> str:
        """Q5-03: what an operator can do about an ``adapter.reset()`` that does not return."""
        if age is None:
            return ""
        if age < self.RESET_STUCK_HINT_S:
            return "复位正在执行（例如机器人正在起立），请稍候；停止按钮始终可用"
        return ("适配器复位长时间未返回，可能卡在驱动里。急停仍保持锁存，停止始终可用。"
                "出路：确认现场安全后重启机器人上的运行时服务（锁存会保留），然后重新“解除急停”；"
                "仿真机器人可直接停止再启动仿真")

    def reset_progress_view(self) -> dict:
        """Fields merged into ``/v1/status`` (Q5-03): ``reset_in_progress_s`` and ``reset_hint``."""
        age = self.reset_in_flight_s
        if age is None:
            return {"reset_in_progress_s": None, "reset_hint": None}
        return {"reset_in_progress_s": round(age, 1), "reset_hint": self.reset_hint(age)}

    def reset_in_progress_error(self) -> Conflict:
        age = self.reset_in_flight_s
        message = ("适配器上一次复位（adapter.reset()）尚未返回"
                   + (f"（已 {age:.1f} s）" if age is not None else "")
                   + "；在它返回之前拒绝新的复位，急停保持锁存")
        hint = self.reset_hint(age)
        return Conflict(message, code="reset_in_progress",
                        extra={"message": message + ("。" + hint if hint else ""),
                               "reset_in_progress_s": None if age is None else round(age, 1),
                               "hint": hint})

    def _reset_locked(self, source, persist, expect_stop_count, expire_s=None) -> dict:
        stops = self._stop_seq
        if expect_stop_count is not None and stops != expect_stop_count:
            # Conditional reset (Q4-01): the latch is not the one the caller set -- someone else
            # stopped since. A stop arriving after this check is caught below (re-latch).
            raise EStopLatched(f"停止次数为 {stops}，与预期 {expect_stop_count} 不符：有其他来源的停止，保持锁存",
                               code="stop_count_mismatch")
        halts = self._halt_seq
        started = self._reset_started if self._reset_started is not None else time.monotonic()
        try:
            self._adapter.reset()
        except Exception as exc:
            if self._stop_seq != stops or self._halt_seq != halts:
                self._restop_after_raced_reset()
            raise SafetyRejected(f"适配器拒绝复位：{exc}") from None
        took = time.monotonic() - started
        limit = self.reset_expire_s
        if limit and expire_s is not None:
            limit = min(limit, max(1.0, float(expire_s)))
        if limit and took > limit and self._stop_seq == stops:
            self._expire_reset(persist, took, limit)
        if self._halt_seq != halts:
            # R4b-01: a non-latching halt (or any adapter stop) arrived while adapter.reset() was
            # running -- e.g. a conformance re-stop after the suite gave up on this very reset.
            # Treated exactly like a raced stop: keep the latch, stop again (the driver may have
            # replayed its last setpoint on reset).
            self._abort_reset(persist, stops)
        self._epoch = next(self._epoch_counter)
        epoch = self._epoch
        if persist is not None:
            persist(False, None, epoch)
        if self._stop_seq != stops:
            self._abort_reset(persist, stops)
        self._reset_seq += 1      # Q7c-02: before the latch clears -- the old latch's notice is gone
        self._latched.clear()
        if self._stop_seq != stops:
            # A stop landed between the check and the clear: re-latch.
            self._latched.set()
            self._abort_reset(persist, stops)
        self.latch_reason = None
        self._events.emit("safety.reset", source=source, epoch=epoch)
        return {"latched": False}

    def _restop_after_raced_reset(self) -> bool:
        """R4-03: a stop arrived while ``adapter.reset()`` was running. The adapter's reset finished
        *after* that stop (a driver may re-enable motors, stand up or replay the last setpoint on
        reset), so the adapter is told to stop again. The latch is kept by the caller."""
        self._velocity_deadline = None
        ok = self._adapter_stop()
        latched = self._latched.is_set()
        by_guard = self._guard_restop_in_reset
        self._events.emit("safety.reset_raced", adapter_stopped=ok, reason=self.latch_reason, latched=latched,
                          by_latch_guard=by_guard,
                          note=("复位动作超过允许时间仍在运动，锁存守护已再次停止：保持锁存" if by_guard and latched else
                                "复位执行期间收到停止：复位返回后已再次停止适配器，保持锁存" if latched else
                                "复位执行期间收到停止：复位返回后已再次停止适配器（注意：当前未锁存）"))
        return ok

    def _abort_reset(self, persist, stops: int) -> None:
        # R4c-01: only the reset holding _reset_lock gets here, and it has not cleared the latch (or
        # has just re-set it), so memory is latched; make that explicit before disk is written so the
        # two always agree.
        self._latched.set()
        self._restop_after_raced_reset()
        # Make sure disk ends up "latched" with a key newer than the reset's write.
        self._epoch = next(self._epoch_counter)
        if persist is not None:
            persist(True, self.latch_reason, self._epoch)
        if self._guard_restop_in_reset and self._stop_seq == stops:
            # Q5-07: nobody pressed stop -- the latch guard did, because adapter.reset() was still
            # moving the body after reset_grace_s.
            message = ("复位动作（adapter.reset()）超过允许时间仍在进行且本体仍在运动，锁存守护已再次停止；"
                       "保持锁存。若起立/复位本身需要更久，请调大锁存守护的 reset_grace_s，或检查驱动的复位")
            raise EStopLatched(message, extra={"message": message, "cause": "latch_guard_reset_grace"})
        message = "复位期间又收到停止；保持锁存"
        raise EStopLatched(message, extra={"message": message, "cause": "stop_during_reset"})

    def _expire_reset(self, persist, took: float, limit: float) -> None:
        """Q7-10: ``adapter.reset()`` returned after ``reset_expire_s``. Keep the latch, stop the
        adapter again (the driver may have re-enabled motors / replayed a setpoint on reset),
        emit ``safety.reset_expired`` and fail the reset. A new reset can be requested."""
        self._latched.set()
        self._velocity_deadline = None
        ok = self._adapter_stop()
        self._epoch = next(self._epoch_counter)
        if persist is not None:
            persist(True, self.latch_reason, self._epoch)
        message = (f"复位动作（adapter.reset()）用了 {took:.1f} s 才返回，超过 {limit:g} s 的有效期，"
                   f"已作废：保持锁存并再次停止。确认现场安全后请重新“解除急停”")
        self._events.emit("safety.reset_expired", took_s=round(took, 1), expire_s=limit,
                          adapter_stopped=ok, reason=self.latch_reason, latched=True, note=message)
        raise EStopLatched(message, extra={"message": message, "cause": "reset_expired",
                                           "took_s": round(took, 1), "expire_s": limit})

    def restore_latch(self, reason: str) -> None:
        """Re-apply a latch persisted before a restart (no adapter call)."""
        self._epoch = next(self._epoch_counter)
        self._latched.set()
        self.latch_reason = reason
