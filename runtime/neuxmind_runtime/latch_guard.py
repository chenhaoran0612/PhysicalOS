"""Latched-state guard (R4b-04 root fix, round 05; Q5-01/02/06/07/08, round 06; Q6-01/04/05, round 07;
Q7b-01/02/03/09, round 07 second fix pass: at least as safe as v0.13.0 in every reviewed scenario).

While the e-stop is latched nothing may move. A driver that ignores or "forgets" a stop (resumes
its last setpoint seconds later, re-enables motors on a timer, ...) used to be caught only inside
the conformance suite's finite observation window. The guard closes that gap for the whole life of
the runtime:

* it runs on its own thread (``nx-latch-guard``) and does nothing while not latched;
* while latched it samples the feedback the body declares -- ``base.odometry`` and ``joint.state``
  -- and, if motion is observed *or stillness cannot be confirmed*, tells the adapter to stop again
  (:meth:`SafetySupervisor.latched_restop`), at most once every ``restop_interval_s`` (robot clock);
* re-stops are reported as ``safety.motion_while_latched`` / ``safety.feedback_invalid_while_latched``
  events, aggregated (round 07): the first re-stop of a kind is reported at once, later ones at most
  once per ``event_interval_s`` with ``count`` = re-stops the event stands for, so a misbehaving
  driver cannot flood the 5000-event ring; ``describe()`` is ``latched_motion`` in ``/v1/status``.

Evidence of motion (any one is enough). Real feedback is noisy and IMUs drift (Q6-01), so the
round-07 rules work on robust statistics instead of single samples:

1. **Velocity**: one sample above ``fast_factor`` x threshold (clear motion, caught at once), or
   the *median* of the reported velocities over the last ``velocity_window_s`` above the threshold
   (``linear_velocity`` m/s, ``angular_velocity`` rad/s for base yaw rate and joints).
2. **Jump between two samples**: a pose / joint change above ``max(min_*_delta, sample_noise_k x
   noise)`` and above threshold x elapsed time. ``noise`` is the declared noise (``noise_m`` /
   ``noise_rad``) or, if larger, the noise measured on the feedback itself (robust estimate from
   second differences, so a steady creep does not inflate it; capped at ``noise_cap_*``).
3. **Displacement** (Q5-01), on median-filtered values (``filter_n`` samples) while the noise
   matters, on the raw sample while it is negligible next to ``min_*_delta`` (no filter delay, as
   tight as v0.13.0): after the stop has settled the guard takes a snapshot (base x/y/yaw, every
   joint *by name*).
   a) Position and joints from the snapshot, never re-based (Q7b-09, as v0.13.0): more than
      ``max_linear_drift`` (``pos_m``) -- widened by a measured odometry random walk
      (``walk_k`` x walk x sqrt(t)) -- or ``max_joint_drift`` (``joint_rad``) counts, whatever the
      speed. Base yaw: a reference re-based every ``rebase_s`` while nothing counts (IMU drift),
      plus b).
   b) **Hard bounds from the snapshot, never re-based** (Q7-01): position ``total_factor`` x
      ``pos_m``, each joint ``total_factor`` x ``joint_rad``, yaw ``min(max(yaw_total_rad,
      yaw_rad), yaw_rad + yaw_drift_rad_s x t)``. Default ``yaw_total_rad`` = 0.05 rad (Q7b-01):
      any turn is caught at 0.05 rad as in v0.13.0, an IMU drift too (0.5 deg/min: a re-stop every
      ~345 s, never escalated); a body with such an IMU declares ``yaw_total_rad`` (up to 1 rad,
      loud) and the drift is tolerated up to it. Ratchet (Q7-02): after a re-stop at displacement R
      on a channel the next hard bound is ``R + limit`` (yaw: ``R + min(max(yaw_total_rad,
      yaw_rad), yaw_rad + drift x dt)``, capped -- Q7b-01), so a body that came to rest away from
      the snapshot (sat down, pushed) is not re-stopped again and again; one that keeps going is.
   c) After a re-stop on displacement the body is "displaced" for the rest of the latch episode
      (Q7b-02: it no longer ends after a quiet minute, which handed a driver that obeys each stop
      for a while a fresh ``pos_m`` every cycle): moving again beyond ``max(min_*_delta,
      hypot(filtered_noise_k x noise, walk_k x random walk x sqrt(t + walk_t0_s)))`` relative to
      where it was at the last re-stop (re-based after ``rebase_s`` without motion evidence), for
      ``persist_n`` consecutive samples when the noise matters, is motion again. The random walk
      (``walk_*``, legged odometry wander) is measured on the feedback (lag-variance estimate,
      peak over ``walk_hold_s``, capped at ``walk_cap_*``) and believed only while it explains
      what is seen: not when every step does not show it (a stop-and-go creep, Q7b-02), not when
      the window shows a net trend, not within the window after a re-stop on direct motion, not
      once the base is ``walk_trend_k`` walks x sqrt(t) from its anchor (a faked walk, Q7b-03),
      and never growing while re-stops keep coming (Q7b-03).
   d) **Precautionary stop** (round 07 second fix pass; Q7c-01, third fix pass: an exact
      replica): :class:`V013Replica` runs v0.13.0's re-stop rule and cadence on the same raw
      samples (no filter, no noise / walk / drift allowance, no persistence, its own 0.5 s
      ``restop_interval_s``, reference and "displaced" moved only at its own re-stops). Whenever
      it would re-stop and this guard did not call ``stop()`` in that sample, the adapter is told
      to stop again *silently* (``stop()`` is idempotent by the HAL contract): no event, not a
      re-stop, never escalates (``precautionary_stops`` in ``/v1/status``). The ``stop()`` calls
      thus cover every re-stop v0.13.0 would have made: a driver that obeys each stop for a while
      and resumes under a (faked or real) odometry walk goes no further than under v0.13.0.
   e) **One stop() cadence** (Q7d-01/02, round 07 fourth fix pass): every ``adapter.stop()`` the
      guard issues -- counted re-stop, precautionary stop, invalid feedback, the monitor thread's
      stalled read -- is at least ``restop_interval_s`` (0.5 s) after the previous one (<= 2/s;
      the two kinds used to interleave 0.1-0.4 s apart, up to 4/s, and a driver that drops a
      stop() < 0.5 s after the previous one swallowed them all). While the precautionary stops
      are on, the guard thread's ``stop()`` calls are exactly the replica's (v0.13.0's) re-stop
      times: a counted re-stop calls ``stop()`` only in a sample where v0.13.0's rule also sees
      motion / invalid feedback, else it is *merged* (counted, reported, escalates; no call of
      its own; ``merged_stops``); a replica re-stop due < 0.5 s after a guard ``stop()`` (a
      stalled-read stop) is that stop. ``stop_calls`` counts the calls. Operator stops
      (``/v1/stop``, ``/v2/stop``) are outside this cadence and always go through at once.
      Q7e-01/02 (round 07 fifth fix pass): the guard records which thread issued the latest
      ``stop()`` (guard thread in which iteration / monitor thread's read-hang re-stop) and
      never infers "already stopped in this sample" from equal robot times (coarse adapter
      clocks); a replica re-stop after a read-hang stop is merged into it without evidence,
      reference = where the body was at that stop (as v0.13.0's read-hang re-stop). Events of a
      merged counted re-stop say ``adapter_stopped: false``, ``merged: true``.
4. **Feedback that cannot confirm stillness** (Q5-02): a read that raises, NaN/inf, arrays of the
   wrong length, ``velocity: null`` on ``base.odometry``, joint names that are missing, duplicated
   or not the same *set* as at the snapshot. Lasting ``invalid_grace_s`` (robot time) it counts:
   "cannot see = may be moving". A read that *hangs* for ``read_stall_s`` is detected from the
   monitor thread (:meth:`check_stalled_read`), since the guard thread itself is stuck.

What is bounded (Q6-04): the displacement *between two re-stops* (about the threshold plus the
filter delay); not the total displacement of a driver that resumes after every stop -- software
cannot stop a driver that does not obey ``stop()``. That is escalated instead (Q7-02/Q7-06): a
motion re-stop extends the *streak* only if the body kept moving since the previous counted one
(velocity / sample jump, or filtered displacement beyond noise, walk and drift) and within
``escalate_gap_s``; ``escalate_after`` streak re-stops spanning ``escalate_min_s`` emit
``safety.stop_not_obeyed`` once (also in ``latched_motion``): use the hardware e-stop / cut power.
Q7b-03: the base ``escalate_displacement_m`` (0.5 m) from the snapshot after ``escalate_fast_n``
(3) motion re-stops escalates too, whatever the gaps between them. A body at rest away from the
snapshot builds no streak.

Calibration (Q6-01/Q6-05): ``pos_m``, ``yaw_rad``, ``joint_rad``, ``yaw_total_rad``, ``noise_m``,
``noise_rad``, ``yaw_drift_rad_s``, ``stop_settle_s`` (alias ``settle_s``) and ``escalate_after`` (max
30) can be declared
in the manifest (``safety.latch_guard``, validated and clamped by
``neuxmind_spec.manifest.normalize_latch_guard``) and overridden by the runtime configuration
(``EdgeRuntime(latch_guard_overrides=...)``, ``serve --latch-guard key=value``), same bounds.
A calibration looser than the default (any key above its default) is loud (Q7-05): one
``safety.latch_guard_relaxed`` event at startup and ``latched_motion.relaxed`` / ``relaxed_keys``.

What it never does: unlatch, change the latch reason, count as a stop (``stop_count``), send any
motion command (not even zero velocity), take a lock the stop path takes, or call the adapter after
``close()``. A hung feedback read blocks only this (daemon) thread; ``close()`` waits for it at most
1 s and the thread exits, without touching the adapter, as soon as the read returns (Q5-08).

Settling: right after a counted stop the body is allowed ``settle_s`` (robot time) to come to rest
before anything counts and before the snapshot is taken. An adapter whose stop is a controlled
motion (legged damping / sit-down, Q5-06) declares ``stop_settle_s`` (manifest / runtime config, or
the adapter attribute); the guard uses it, bounded to ``[settle_s, max_settle_s]``.

While an operator's reset is running (``adapter.reset()`` may legitimately move the body, e.g. a
quadruped standing up) the guard only observes; if that reset is still in flight after
``reset_grace_s`` (wall time) it re-stops anyway (which, by the supervisor's race rule, keeps the
latch when the reset finally returns -- safe side; the reset then says that it was the guard,
Q5-07). After a reset attempt that left the latch on, the snapshot is retaken after a new settle.

Inherent limit: the guard relies on the feedback being true. Frozen-but-plausible feedback
(constant pose, zero velocity) cannot be told from a body at rest. The guard does not replace a
hardware e-stop.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

from neuxmind_spec.manifest import LATCH_GUARD_FIELDS, NOISE_MARGIN, normalize_latch_guard

from .clock import Clock


@dataclass(frozen=True)
class GuardConfig:
    period_s: float = 0.1               # wall seconds between samples while latched
    restop_interval_s: float = 0.5      # robot seconds between two re-stops (bounded rate)
    settle_s: float = 1.0               # robot seconds after a counted stop before motion counts
    linear_velocity: float = 0.02       # m/s
    angular_velocity: float = 0.05      # rad/s (base yaw rate and joint velocity)
    min_linear_delta: float = 0.003     # m: lowest "moved" floor (sample jump / since the last re-stop)
    min_angular_delta: float = 0.005    # rad, same
    reset_grace_s: float = 30.0         # wall seconds an in-flight reset may move the body
    max_linear_drift: float = 0.05      # m from the reference (Q5-01)            -- manifest pos_m
    max_angular_drift: float = 0.05     # rad base yaw from the reference (Q5-01) -- manifest yaw_rad
    invalid_grace_s: float = 0.25       # robot s of continuously unusable feedback before it counts (Q5-02)
    read_stall_s: float = 1.0           # wall s a single feedback read may block before it counts (Q5-02)
    max_settle_s: float = 5.0           # upper bound for a declared stop_settle_s (Q5-06)
    # ---- round 07 (Q6-01/04/05)
    max_joint_drift: float | None = None  # rad per joint from the reference; None: max_angular_drift (joint_rad)
    noise_m: float = 0.0                # declared position noise floor, 1 sigma (m); the measured noise is used if larger
    noise_rad: float = 0.0              # declared yaw / joint noise floor, 1 sigma (rad)
    yaw_drift_rad_s: float = 0.0003     # tolerated IMU yaw drift (rad/s, ~1 deg/min)
    stop_settle_s: float | None = None  # declared stop duration (manifest/config); None: adapter attribute
    escalate_after: int = 10            # motion re-stops before safety.stop_not_obeyed ...
    escalate_min_s: float = 30.0        # ... spread over at least this many robot seconds
    rebase_s: float = 60.0              # robot s: reference re-based / "displaced" ends after this quiet time
    total_factor: float = 4.0           # bound from the snapshot = total_factor x limit (position, joints)
    filter_n: int = 3                   # median filter length for displacement
    persist_n: int = 2                  # consecutive samples for "moved since the last re-stop" (when noise matters)
    fast_factor: float = 3.0            # single-sample velocity above fast_factor x threshold counts at once
    velocity_window_s: float = 1.0      # robot s: median window for the reported velocity
    velocity_noise_n: int = 10          # samples needed before the reported velocity's spread is trusted
    velocity_noise_k: float = 8.0       # the single-sample gate is the threshold while k x spread is below it
    sample_noise_k: float = 8.0         # jump between two raw samples must exceed k x noise
    filtered_noise_k: float = 5.0       # move since the last re-stop (filtered) must exceed k x noise
    noise_cap_m: float = 0.003          # measured noise is believed up to this much (a liar cannot hide behind it;
    noise_cap_rad: float = 0.002        # round 07 second fix pass: 1 cm / 10 mrad -> 3 mm / 2 mrad; more must be declared)
    noise_history: int = 200            # samples per channel kept for the noise estimate (20 s at 10 Hz)
    event_interval_s: float = 10.0      # robot s between two events of the same kind (aggregated)
    # ---- round 07 fix pass (Q7-01/02/06)
    yaw_total_rad: float = 0.05         # hard bound on base yaw from the snapshot, never re-based -- manifest yaw_total_rad (Q7b-01: 0.2 -> 0.05)
    walk_k: float = 4.0                 # "moved since the last re-stop" must also exceed walk_k x random walk x sqrt(t)
    walk_cap_m: float = 0.0015          # odometry random walk believed up to this (m/sqrt(s)); a liar cannot hide behind more
    walk_cap_rad: float = 0.0015        # yaw random walk believed up to this (rad/sqrt(s))
    walk_lag: int = 20                  # samples: lag used to tell a random walk from white noise
    escalate_gap_s: float = 180.0       # robot s: consecutive motion re-stops further apart start a new streak
    escalate_displacement_m: float = 0.5  # m from the snapshot: escalate after escalate_fast_n motion re-stops (Q7-06/Q7b-03)
    escalate_fast_n: int = 3
    # ---- round 07 second fix pass (Q7b-01/02/03)
    precaution_interval_s: float = 0.5  # > 0: silent precautionary stops on (<= 0: off). Q7c-01: their cadence is
                                        # v0.13.0's own, restop_interval_s since the replica's last re-stop (was 1.0 s)
    walk_hold_s: float = 300.0          # robot s: the walk believed is the peak estimate over this window
    walk_t0_s: float = 2.0              # s added to the time in the walk spread (a fresh re-stop reference is not exact)
    walk_trend_k: float = 5.0           # random walk not believed once the base is this many walks (x sqrt(t)) from its anchor


DEFAULT_CONFIG = GuardConfig()


# Declared calibration key (manifest safety.latch_guard / runtime config) -> GuardConfig field.
CALIBRATION_FIELDS = {"pos_m": "max_linear_drift", "yaw_rad": "max_angular_drift", "joint_rad": "max_joint_drift",
                      "yaw_total_rad": "yaw_total_rad",
                      "noise_m": "noise_m", "noise_rad": "noise_rad", "yaw_drift_rad_s": "yaw_drift_rad_s",
                      "stop_settle_s": "stop_settle_s", "escalate_after": "escalate_after"}
assert set(CALIBRATION_FIELDS) == set(LATCH_GUARD_FIELDS)
ESCALATE_AFTER_MAX = LATCH_GUARD_FIELDS["escalate_after"][2]   # Q7-05: never more than this (also for a GuardConfig)


def _reject_constant(name: str):
    raise ValueError(f"--latch-guard 不允许 {name}")


def parse_overrides(items) -> dict:
    """``serve --latch-guard``: ``["pos_m=0.08", "noise_m=0.003"]`` or a JSON object string
    (``'{"pos_m": 0.08}'``) -> dict of numbers. Raises ``ValueError``. Validation and clamping is
    :func:`neuxmind_spec.manifest.normalize_latch_guard` (same rule as the manifest)."""
    out: dict = {}
    for item in items or []:
        text = str(item).strip()
        if text.startswith("{"):
            value = json.loads(text, parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise ValueError("--latch-guard 的 JSON 须为对象")
            out.update(value)
            continue
        key, sep, raw = text.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--latch-guard 须为 key=value 或 JSON 对象，收到 {text!r}")
        try:
            number = float(raw)
        except ValueError:
            raise ValueError(f"--latch-guard {key.strip()} 须为数值，收到 {raw!r}") from None
        out[key.strip()] = int(number) if number.is_integer() and key.strip() == "escalate_after" else number
    return out


def apply_calibration(config: GuardConfig, values: dict) -> GuardConfig:
    """``values``: normalised declared keys (see :data:`CALIBRATION_FIELDS`)."""
    return replace(config, **{CALIBRATION_FIELDS[k]: v for k, v in values.items()})


def calibration_view(config: GuardConfig, settle_s: float) -> dict:
    return {"pos_m": config.max_linear_drift, "yaw_rad": config.max_angular_drift,
            "joint_rad": config.max_joint_drift if config.max_joint_drift is not None else config.max_angular_drift,
            "yaw_total_rad": config.yaw_total_rad,
            "noise_m": config.noise_m, "noise_rad": config.noise_rad, "yaw_drift_rad_s": config.yaw_drift_rad_s,
            "stop_settle_s": settle_s, "escalate_after": config.escalate_after}


def relaxed_view(view: dict, source: dict) -> dict:
    """Q7-05: calibration keys that make the guard *less* sensitive than the default (every key
    loosens the guard when larger): ``{key: {"value", "default", "source"}}``."""
    out = {}
    for key, value in view.items():
        default = LATCH_GUARD_FIELDS[key][0]
        if isinstance(value, (int, float)) and value > default + 1e-12:
            out[key] = {"value": value, "default": default, "source": source.get(key, "default")}
    return out


MOTION_NOTE = ("急停锁存期间检测到本体仍在运动，已再次停止适配器（锁存保持）；若反复出现请使用硬件急停并检查驱动。"
               "若停止本身是受控动作（阻尼趴下/坐下），请在清单 safety.latch_guard.stop_settle_s 或适配器上声明停止时长；"
               "若是传感器噪声/漂移（例如 IMU 偏航漂移每 0.05 rad 再停一次），请按现场实测在 safety.latch_guard 标定 "
               "noise_m / noise_rad / yaw_total_rad / yaw_drift_rad_s")
INVALID_NOTE = ("急停锁存期间无法读取有效的本体反馈，无法确认本体静止，已按“可能在动”再次停止适配器（锁存保持）；"
                "请检查传感器/驱动反馈，必要时使用硬件急停")
NOT_OBEYED_NOTE = ("驱动不服从停止：急停锁存期间本体在多次再停之后仍在运动，软件停止已无法让它停下。"
                   "请立即使用硬件急停或切断动力电，在现场检查驱动/电机控制器后再解除急停")
RELAXED_NOTE = ("锁存守护的标定比默认更宽松：急停锁存期间本体要移动/转动更多才会被发现、或更晚才升级为“驱动不服从停止”。"
                "请确认这些取值来自现场实测，并始终保留硬件急停")


def _finite_list(value) -> list[float] | None:
    if not isinstance(value, (list, tuple)):
        return None
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        out.append(float(v))
    return out


def _yaw(pose: list[float]) -> float:
    qx, qy, qz, qw = pose[3:7]
    return math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + math.pi) % (2.0 * math.pi) - math.pi)


def parse_odometry(sample) -> tuple[dict | None, str | None]:
    """``({"x", "y", "yaw", "velocity"}, None)`` or ``(None, problem)``."""
    if not isinstance(sample, dict):
        return None, "base.odometry 不是对象"
    pose = _finite_list(sample.get("pose"))
    if pose is None or len(pose) != 7:
        return None, "base.odometry.pose 不是 7 个有限数值"
    if math.hypot(*pose[3:7]) < 1e-6:
        return None, "base.odometry.pose 四元数为零"
    vel = _finite_list(sample.get("velocity"))
    if vel is None or len(vel) != 3:
        return None, "base.odometry.velocity 不是 3 个有限数值"
    return {"x": pose[0], "y": pose[1], "yaw": _yaw(pose), "velocity": vel}, None


def parse_joint_state(sample) -> tuple[dict | None, str | None]:
    """``({"position": {name: rad}, "velocity": {name: rad/s} | None}, None)`` or ``(None, problem)``.
    Everything is keyed by joint name (Q5-02: the order of ``names`` may change between reads)."""
    if not isinstance(sample, dict):
        return None, "joint.state 不是对象"
    names = sample.get("names")
    if (not isinstance(names, (list, tuple)) or not names
            or any(not isinstance(n, str) or not n for n in names)):
        return None, "joint.state.names 不是非空的关节名列表"
    if len(set(names)) != len(names):
        return None, "joint.state.names 有重复关节名"
    pos = _finite_list(sample.get("position"))
    if pos is None or len(pos) != len(names):
        return None, "joint.state.position 与 names 长度不符或含非有限数值"
    vel_raw = sample.get("velocity")
    velocity = None
    if vel_raw is not None:                      # optional in the standard
        vel = _finite_list(vel_raw)
        if vel is None or len(vel) != len(names):
            return None, "joint.state.velocity 与 names 长度不符或含非有限数值"
        velocity = dict(zip(names, vel))
    return {"position": dict(zip(names, pos)), "velocity": velocity}, None


WALK_STEP_RATIO = 2.0    # a walk rate whose per-sample variance exceeds this x the measured step variance is not a walk
NOISE_MIN_SAMPLES = 20   # the noise estimate (and the sample-jump rule that relies on it) needs this many


def _sigma(values: list[float]) -> float:
    """Robust 1-sigma noise of a sampled signal from its second differences (a steady drift or
    creep has none): 1.4826 x median(|x[i] - 2 x[i-1] + x[i-2]|) / sqrt(6)."""
    if len(values) < NOISE_MIN_SAMPLES:
        return 0.0
    d2 = sorted(abs(values[i] - 2.0 * values[i - 1] + values[i - 2]) for i in range(2, len(values)))
    return 1.4826 * d2[len(d2) // 2] / math.sqrt(6.0)


def _median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def _robust_var(diffs: list[float]) -> float:
    """Variance of ``diffs`` about their median, from the MAD (a steady creep only shifts them)."""
    m = _median(diffs)
    mad = _median([abs(d - m) for d in diffs])
    return (1.4826 * mad) ** 2


def _walk_rate(samples: list[tuple[float, float]], lag: int, trend_k: float = 4.0, noise_floor: float = 0.0) -> float:
    """Random-walk rate (units per sqrt(s)) of a sampled signal (Q7-02: legged odometry wanders
    while standing). White noise gives var(x[i+L]-x[i]) = 2 sigma^2 for any lag L, a random walk
    adds L x s^2, so s^2 = (var_L - var_1) / (L - 1); a steady creep only shifts the differences.
    A walk has no direction: if the net change over the window exceeds ``trend_k`` x what that
    walk explains (+ ``noise_floor``), the signal is going somewhere (stop-and-go creep) -> 0."""
    n = len(samples)
    if n < max(NOISE_MIN_SAMPLES, 3 * lag):
        return 0.0
    values = [v for _, v in samples]
    var1 = _robust_var([values[i] - values[i - 1] for i in range(1, n)])
    varl = _robust_var([values[i] - values[i - lag] for i in range(lag, n)])
    dt = (samples[-1][0] - samples[0][0]) / (n - 1)
    if dt <= 0:
        return 0.0
    rate = math.sqrt(max(0.0, varl - var1) / ((lag - 1) * dt))
    # Q7b-02: a random walk shows in every step (var1 >= rate^2 dt); a stop-and-go creep (steps
    # all equal while it moves, zero while it pauses) only between pauses -- not a walk.
    if rate * rate * dt > WALK_STEP_RATIO * var1:
        return 0.0
    k = min(5, n // 4)
    net = abs(_median(values[-k:]) - _median(values[:k]))
    if net > trend_k * rate * math.sqrt(samples[-1][0] - samples[0][0]) + noise_floor:
        return 0.0
    return rate


class V013Replica:
    """Q7c-01 (round 07 third fix pass): v0.13.0's latched-state re-stop rule and cadence,
    replicated exactly (``dbab711`` ``latch_guard.py``), so that the silent precautionary stops
    are issued wherever and whenever v0.13.0 would have re-stopped:

    * the same raw samples (no filter), the same evidence: reported velocity above
      ``linear_velocity`` / ``angular_velocity``; a jump between two samples above
      ``min_*_delta`` and threshold x dt; before "displaced", more than ``pos_m`` / ``yaw_rad`` /
      ``joint_rad`` from the raw latch snapshot; once displaced, more than ``min_*_delta``
      (3 mm / 5 mrad) from where the body was at v0.13.0's last re-stop;
    * no noise, random-walk or IMU-drift allowance, no persistence requirement;
    * its own cadence: at most once per ``restop_interval_s`` (0.5 s) since its own last
      re-stop; "displaced" set at a re-stop on a drift rule; the reference moved only at its
      own re-stops; the episode (snapshot, reference, "displaced") starts again with the guard's.

    It never calls the adapter itself: :meth:`LatchedMotionGuard._precaution` does (silently),
    unless the guard already called ``stop()`` in that sample (or < ``restop_interval_s`` ago:
    Q7d-01, then that stop is the replica's re-stop, :meth:`restopped` with ``at``)."""

    def __init__(self, config: GuardConfig, joint_limit: float):
        self.config = config
        self.joint_limit = joint_limit
        self.last_restop: float | None = None     # robot time (kept across episodes, as v0.13.0)
        self.reset_episode()

    def reset_episode(self) -> None:
        self.prev: dict[str, tuple[float, dict]] = {}
        self.anchor: dict[str, dict] = {}
        self.ref: dict[str, dict] = {}
        self.last_parsed: dict[str, dict] = {}
        self.displaced = False

    def evaluate(self, now: float, parsed: dict, problems: dict, snapshot_ok: bool, yaw: float | None) -> dict:
        """v0.13.0's motion evidence for this sample (``parsed``: the guard's parsed samples;
        ``yaw``: the unwrapped base yaw). Updates the previous sample / snapshot as v0.13.0 did."""
        cfg = self.config
        evidence: dict = {}
        current: dict[str, dict] = {}
        odo = parsed.get("base.odometry")
        if odo is not None and yaw is not None:
            o = {"x": odo["x"], "y": odo["y"], "yaw": yaw}
            current["base.odometry"] = o
            vel = odo["velocity"]
            if math.hypot(vel[0], vel[1]) > cfg.linear_velocity:
                evidence["base_speed"] = round(math.hypot(vel[0], vel[1]), 4)
            if abs(vel[2]) > cfg.angular_velocity:
                evidence["base_wz"] = round(vel[2], 4)
            prev = self.prev.get("base.odometry")
            if prev is not None and now - prev[0] > 0:
                old, dt = prev[1], now - prev[0]
                d = math.hypot(o["x"] - old["x"], o["y"] - old["y"])
                if d > cfg.min_linear_delta and d > cfg.linear_velocity * dt:
                    evidence["base_moved_m"] = round(d, 4)
                dyaw = abs(o["yaw"] - old["yaw"])
                if dyaw > cfg.min_angular_delta and dyaw > cfg.angular_velocity * dt:
                    evidence["base_yaw_rad"] = round(dyaw, 4)
            anchor = self.anchor.get("base.odometry")
            if anchor is None and snapshot_ok:
                anchor = self.anchor["base.odometry"] = o
            if anchor is not None:
                drift = math.hypot(o["x"] - anchor["x"], o["y"] - anchor["y"])
                dyaw = abs(o["yaw"] - anchor["yaw"])
                if not self.displaced and drift > cfg.max_linear_drift:
                    evidence["base_drift_m"] = round(drift, 4)
                if not self.displaced and dyaw > cfg.max_angular_drift:
                    evidence["base_drift_yaw_rad"] = round(dyaw, 4)
                if self.displaced:
                    ref = self.ref.setdefault("base.odometry", o)
                    d = math.hypot(o["x"] - ref["x"], o["y"] - ref["y"])
                    if d > cfg.min_linear_delta:
                        evidence["base_moved_since_restop_m"] = round(d, 4)
                    dy = abs(o["yaw"] - ref["yaw"])
                    if dy > cfg.min_angular_delta:
                        evidence["base_yaw_since_restop_rad"] = round(dy, 4)
        js = parsed.get("joint.state")
        if js is not None:
            anchor = self.anchor.get("joint.state")
            if anchor is not None and set(anchor["position"]) != set(js["position"]):
                js = None                          # the guard reports the name-set problem
        if js is not None:
            pos = js["position"]
            current["joint.state"] = js
            if js["velocity"]:
                name, peak = max(js["velocity"].items(), key=lambda kv: abs(kv[1]))
                if abs(peak) > cfg.angular_velocity:
                    evidence["joint_velocity"] = round(abs(peak), 4)
                    evidence["joint"] = name
            prev = self.prev.get("joint.state")
            if prev is not None and now - prev[0] > 0:
                old, dt = prev[1]["position"], now - prev[0]
                common = [n for n in pos if n in old]
                if common:
                    name = max(common, key=lambda n: abs(pos[n] - old[n]))
                    peak = abs(pos[name] - old[name])
                    if peak > cfg.min_angular_delta and peak > cfg.angular_velocity * dt:
                        evidence["joint_moved_rad"] = round(peak, 4)
                        evidence.setdefault("joint", name)
            anchor = self.anchor.get("joint.state")
            if anchor is None and snapshot_ok:
                anchor = self.anchor["joint.state"] = js
            if anchor is not None:
                apos = anchor["position"]
                name = max(pos, key=lambda n: abs(pos[n] - apos[n]))
                drift = abs(pos[name] - apos[name])
                if not self.displaced and drift > self.joint_limit:
                    evidence["joint_drift_rad"] = round(drift, 4)
                    evidence.setdefault("joint", name)
                if self.displaced:
                    rpos = self.ref.setdefault("joint.state", js)["position"]
                    name = max(pos, key=lambda n: abs(pos[n] - rpos.get(n, pos[n])))
                    d = abs(pos[name] - rpos.get(name, pos[name]))
                    if d > cfg.min_angular_delta:
                        evidence["joint_moved_since_restop_rad"] = round(d, 4)
                        evidence.setdefault("joint", name)
        for cap, value in current.items():
            if cap not in problems:
                self.prev[cap] = (now, value)
                self.last_parsed[cap] = value
        return evidence

    def due(self, now: float, evidence: dict, invalid: dict) -> bool:
        """Would v0.13.0 re-stop now (``invalid``: the guard's feedback problems past their grace,
        the same rule as v0.13.0's)?"""
        if not evidence and not invalid:
            return False
        return self.last_restop is None or now - self.last_restop >= self.config.restop_interval_s

    def restopped(self, now: float, evidence: dict, at: dict | None = None) -> None:
        """v0.13.0 re-stopped here: its cadence, "displaced" and reference (``at``: where the body
        was at that stop, default the latest sample -- Q7d-01: a guard ``stop()`` the replica's
        re-stop is merged into)."""
        self.last_restop = now
        if evidence and any(k.startswith(("base_drift", "joint_drift")) for k in evidence):
            self.displaced = True
        for cap, value in (self.last_parsed if at is None else at).items():
            self.ref[cap] = value


class LatchedMotionGuard:
    def __init__(self, bus, events, clock: Clock, config: GuardConfig = DEFAULT_CONFIG, adapter=None,
                 overrides: dict | None = None):
        """``overrides``: runtime calibration (``serve --latch-guard``), same keys and bounds as the
        manifest's ``safety.latch_guard``; raises ``ValueError`` when invalid."""
        self._bus = bus
        self._sup = bus.supervisor
        self._events = events
        self._clock = clock
        self.config_notes: list[str] = []
        self.config_source: dict[str, str] = {k: "default" for k in CALIBRATION_FIELDS}
        declared = ((bus.manifest.get("safety") or {}).get("latch_guard") or {}) if hasattr(bus, "manifest") else {}
        if declared:
            config = apply_calibration(config, declared)
            self.config_source.update({k: "manifest" for k in declared})
            try:   # the frozen manifest is already clamped; say what was clamped
                raw = (adapter.manifest().get("safety") or {}).get("latch_guard") if adapter is not None else None
                if raw is not None:
                    self.config_notes += normalize_latch_guard(raw)[1]
            except Exception:
                pass
        if overrides:
            values, notes = normalize_latch_guard(overrides, "运行时配置 latch_guard")
            config = apply_calibration(config, values)
            self.config_source.update({k: "runtime" for k in values})
            self.config_notes += notes
        if config.escalate_after > ESCALATE_AFTER_MAX:      # Q7-05: a GuardConfig cannot go past the cap either
            self.config_notes.append(f"escalate_after={config.escalate_after} 超过上限，按 {ESCALATE_AFTER_MAX} 执行")
            config = replace(config, escalate_after=ESCALATE_AFTER_MAX)
        self.config = config
        self.sources = [c for c in ("base.odometry", "joint.state") if c in bus.capability_ids]
        self.settle_s = self._effective_settle(adapter)
        if self.config_source["stop_settle_s"] == "default" and config.stop_settle_s is None \
                and getattr(adapter, "stop_settle_s", None) is not None:
            self.config_source["stop_settle_s"] = "adapter"
        # Q7-05: a calibration that loosens the guard is loud -- one event at startup + /v1/status.
        self.relaxed = relaxed_view(calibration_view(config, self.settle_s), self.config_source) if self.sources else {}
        if self.relaxed:
            # desktop contract (master 5ccbd26): {message, keys, ts}; ts is added by the event bus
            events.emit("safety.latch_guard_relaxed", message=RELAXED_NOTE, keys=self.relaxed_list(),
                        notes=list(self.config_notes))
        self._lock = threading.Lock()        # guard bookkeeping only (never taken by the stop path)
        self._restop_lock = threading.Lock()  # guard thread vs monitor thread (stalled read); not the stop path
        self.seen = 0                        # samples in which motion was observed while latched
        self.restops = 0
        self.read_errors = 0
        self.seen_during_reset = 0
        self.feedback_invalid = 0            # samples (after grace) in which stillness could not be confirmed
        self.invalid_restops = 0
        self.last_invalid: dict | None = None
        self.last_at: float | None = None    # wall time (time.time()) of the last observed motion
        self.last_restop_at: float | None = None
        self.last_evidence: dict | None = None
        self.max_drift_m = 0.0               # this latch episode, from the snapshot
        self.max_drift_rad = 0.0
        self.rebases = 0                     # reference re-based (slow drift that is not motion)
        self.events_suppressed = 0           # re-stops reported only in an aggregated event
        self.motion_restops = 0              # this latch episode
        self.stop_not_obeyed: dict | None = None
        self._first_motion_restop: float | None = None
        self._prev: dict[str, tuple[float, dict]] = {}
        self._hist: dict[str, deque] = {}    # channel -> (t, value): raw positions (filter + noise estimate)
        self._vhist: dict[str, deque] = {}   # channel -> (t, value): reported velocities
        self._snap: dict[str, dict] = {}     # cap -> filtered values at the snapshot (total bound)
        self._ref: dict[str, tuple[float, dict]] = {}          # cap -> (t, filtered values): reference
        self._restop_ref: dict[str, tuple[float, dict]] = {}   # cap -> (t, filtered, raw values) at the last re-stop (re-based after rebase_s quiet)
        self._filtered: dict[str, dict] = {}  # cap -> latest filtered values (replaced, never mutated)
        self._persist: dict[str, int] = {}
        self._displaced = False
        self._last_motion: float | None = None   # robot time of the last motion evidence (this episode)
        self._yaw_raw: float | None = None
        self._yaw_unwrapped = 0.0
        self._invalid_since: dict[str, float] = {}
        self._invalid_now: dict[str, str] = {}
        self._last_restop_robot: float | None = None
        self._episode_stop_count: int | None = None
        self._settle_until: float | None = None
        self._saw_reset = False
        self._reading: tuple[str, float] | None = None   # (capability, time.monotonic()) of a read in progress
        self._stall_reported: float | None = None
        self._noise = (self.config.noise_m, self.config.noise_rad, self.config.noise_rad)
        self._noise_ticks = 0
        self._noise_ready: set = set()
        self._ev = {kind: {"last": None, "pending": 0, "payload": None} for kind in ("motion", "invalid")}
        # round 07 fix pass
        self._snap_t: dict[str, float] = {}     # cap -> robot time of the snapshot
        self._ratchet: dict[str, tuple[float, float]] = {}   # "pos" | "yaw" | "j:<name>" -> (t, displacement re-stopped at)
        self._totals: dict[str, float] = {}     # channel -> displacement from the snapshot (latest sample)
        self._walk = (0.0, 0.0)                 # measured random walk (m/sqrt(s), rad/sqrt(s))
        self._streak: dict | None = None        # escalation: consecutive re-stops with continued motion
        self._fast_at: float | None = None      # robot time of the last re-stop on direct motion evidence
        # round 07 second fix pass
        self._walk_anchor: tuple[float, dict] | None = None   # (t, filtered base) the walk trend is measured from
        self._walk_ceiling: tuple[float, float, float] | None = None   # (t, m, rad): walk used at the last motion re-stop
        self._current: dict[str, dict] = {}    # cap -> latest raw values (replaced, never mutated)
        self._walk_eff = (0.0, 0.0)            # walk allowance actually used at the last sample
        self._walk_hist: deque = deque()       # (t, m, rad) walk estimates over walk_hold_s
        # Q7c-01 (round 07 third fix pass): v0.13.0's re-stop rule and cadence, replicated exactly
        self._v13 = V013Replica(self.config, self._joint_limit)
        self._v13_evidence: dict = {}          # what v0.13.0's rule sees in the latest sample
        self.precautionary_stops = 0           # silent stop() calls (not re-stops: no event, no escalation)
        # Q7d-01/02 (round 07 fourth fix pass): ONE cadence for every stop() this guard issues
        self._last_call: float | None = None   # robot time of the last guard-issued adapter stop() (kept across episodes)
        self._last_call_ok: bool = False       # what that call returned
        self._v13_at_call: dict = {}           # the replica's view of the body at that call
        # Q7e-01 (round 07 fifth fix pass): who issued that call -- "tick" (the guard thread, in the
        # guard iteration numbered ``_last_call_tick``) or "stall" (the monitor thread's read-hang
        # re-stop). "This sample already stopped" is never inferred from robot-time equality (a
        # coarse adapter clock can give the monitor's stop and the next sample the same time).
        self._last_call_src: str | None = None
        self._last_call_tick: int | None = None
        self._tick_seq = 0                     # guard iterations (guard thread only)
        self.stop_calls = 0                    # guard-issued stop() calls (counted + precautionary + stall)
        self.merged_stops = 0                  # re-stops merged into a guard stop() < restop_interval_s earlier
        self.drift_notice: dict | None = None  # the one safety.latched_drift_notice of this latch
        self._notice_latch: int | None = None  # Q7c-02: supervisor reset_count of the latch the notice belongs to
        self._notice_due: tuple | None = None
        self._closed = False

    def relaxed_list(self) -> list[dict]:
        """Q7-05 desktop contract: ``[{key, value, default, source}]`` (sorted by key)."""
        return [{"key": k, **v} for k, v in sorted(self.relaxed.items())]

    def _effective_settle(self, adapter) -> float:
        cfg = self.config
        declared = cfg.stop_settle_s
        if declared is None:
            declared = getattr(adapter, "stop_settle_s", None) if adapter is not None else None
        if isinstance(declared, bool) or not isinstance(declared, (int, float)) or not math.isfinite(declared):
            return cfg.settle_s
        return max(cfg.settle_s, min(float(declared), max(cfg.settle_s, cfg.max_settle_s)))

    @property
    def _joint_limit(self) -> float:
        cfg = self.config
        return cfg.max_joint_drift if cfg.max_joint_drift is not None else cfg.max_angular_drift

    # ------------------------------------------------------------ status
    def describe(self) -> dict:
        reading = self._reading
        hung = None if reading is None else round(max(0.0, time.monotonic() - reading[1]), 3)
        cfg = self.config
        notice = self._notice_view()
        with self._lock:
            invalid_now = dict(self._invalid_now)
            noise = self._noise
            return {"enabled": bool(self.sources), "sources": list(self.sources),
                    "active": self._sup.latched and bool(self.sources),
                    "seen": self.seen, "restops": self.restops, "last_at": self.last_at,
                    "last_restop_at": self.last_restop_at, "last_evidence": self.last_evidence,
                    "seen_during_reset": self.seen_during_reset, "read_errors": self.read_errors,
                    "restop_interval_s": cfg.restop_interval_s,
                    "settle_s": self.settle_s,
                    "drift_limit_m": cfg.max_linear_drift,
                    "drift_limit_rad": cfg.max_angular_drift,
                    "max_drift_m": round(self.max_drift_m, 4), "max_drift_rad": round(self.max_drift_rad, 4),
                    "displaced": self._displaced,
                    "feedback_ok": not invalid_now and (hung is None or hung < cfg.read_stall_s),
                    "feedback_problems": invalid_now,
                    "feedback_invalid": self.feedback_invalid, "invalid_restops": self.invalid_restops,
                    "last_invalid": self.last_invalid,
                    "read_in_progress_s": hung,
                    # round 07
                    "config": calibration_view(cfg, self.settle_s),
                    "config_source": dict(self.config_source),
                    "config_notes": list(self.config_notes),
                    "noise_used": {"m": round(noise[0], 5), "yaw_rad": round(noise[1], 5),
                                   "joint_rad": round(noise[2], 5)},
                    "rebases": self.rebases,
                    "motion_restops": self.motion_restops,
                    "precautionary_stops": self.precautionary_stops,
                    # Q7d-01/02: one stop() cadence for the whole guard (>= restop_interval_s apart)
                    "stop_calls": self.stop_calls, "merged_stops": self.merged_stops,
                    # Q7c-02: only while latched, and only the notice of the current latch
                    "drift_notice": notice is not None,
                    "drift_notice_at": notice["at"] if notice else None,
                    "drift_notice_detail": dict(notice) if notice else None,
                    "events_suppressed": self.events_suppressed,
                    "event_interval_s": cfg.event_interval_s,
                    # desktop contract (round 07): a bool + when; details separately
                    "stop_not_obeyed": self.stop_not_obeyed is not None,
                    "stop_not_obeyed_at": self.stop_not_obeyed["at"] if self.stop_not_obeyed else None,
                    "stop_not_obeyed_detail": dict(self.stop_not_obeyed) if self.stop_not_obeyed else None,
                    # round 07 fix pass
                    "relaxed": bool(self.relaxed), "relaxed_keys": self.relaxed_list(),
                    "hard_limits": {"pos_m": round(cfg.total_factor * cfg.max_linear_drift, 4),
                                    "yaw_rad": round(max(cfg.yaw_total_rad, cfg.max_angular_drift), 4),
                                    "joint_rad": round(cfg.total_factor * self._joint_limit, 4),
                                    # Q7b-01/02: after a re-stop at displacement R the next bound is R + step
                                    "pos_step_m": round(cfg.max_linear_drift, 4),
                                    "yaw_step_rad": round(self._yaw_step(cfg.max_angular_drift), 4),
                                    "joint_step_rad": round(self._joint_limit, 4)},
                    "walk_used": {"m_per_sqrt_s": round(self._walk_eff[0], 5),
                                  "rad_per_sqrt_s": round(self._walk_eff[1], 5)},
                    "walk_measured": {"m_per_sqrt_s": round(self._walk[0], 5), "rad_per_sqrt_s": round(self._walk[1], 5)},
                    "escalation_streak": (None if self._streak is None else
                                          {"restops": self._streak["n"],
                                           "span_s": round(self._streak["t_last"] - self._streak["t0"], 1)})}

    def _latch_id(self):
        """Q7c-02: identifies the current latch -- the supervisor's count of successful resets
        (a latch after a reset is a new latch, even if the guard never saw it unlatched)."""
        return getattr(self._sup, "reset_count", None)

    def _notice_view(self) -> dict | None:
        notice = self.drift_notice
        if notice is None or not self._sup.latched or self._notice_latch != self._latch_id():
            return None
        return notice

    # ------------------------------------------------------------ sampling
    def _sample(self) -> tuple[dict[str, dict], dict[str, str]]:
        out, errors = {}, {}
        for cap in self.sources:
            self._reading = (cap, time.monotonic())
            try:
                out[cap] = self._bus.read(cap)
            except Exception as exc:
                errors[cap] = f"读取 {cap} 失败：{str(exc)[:200]}"
                with self._lock:
                    self.read_errors += 1
            finally:
                self._reading = None
        return out, errors

    def _reset_episode(self, now: float, settle: float) -> None:
        """Forget samples, snapshot and references; ``settle`` robot seconds before anything counts."""
        self._flush_events(now, force=True)
        self._settle_until = now + settle
        self._prev.clear()
        self._hist.clear()
        self._vhist.clear()
        self._snap.clear()
        self._snap_t.clear()
        self._ratchet.clear()
        self._totals = {}
        self._walk = (0.0, 0.0)
        self._fast_at = None
        self._walk_anchor = None
        self._walk_ceiling = None
        self._walk_eff = (0.0, 0.0)
        self._walk_hist = deque()
        self._v13.reset_episode()
        self._v13_evidence = {}
        self._v13_at_call = {}
        self._current = {}
        self._ref.clear()
        self._restop_ref.clear()
        self._persist.clear()
        self._displaced = False
        self._last_motion = None
        self._yaw_raw = None
        self._noise_ticks = 0
        self._noise_ready = set()
        self._invalid_since.clear()
        with self._lock:
            self._filtered = {}
            self._invalid_now.clear()

    def _new_episode(self) -> None:
        """A new counted stop (or the latch was cleared): per-episode statistics start again."""
        with self._lock:
            self.max_drift_m = self.max_drift_rad = 0.0
            self.motion_restops = 0
            self.stop_not_obeyed = None
        self._first_motion_restop = None
        self._streak = None

    # ------------------------------------------------------------ helpers
    def _record(self, hist: dict, ch: str, now: float, value: float, maxlen: int) -> None:
        q = hist.get(ch)
        if q is None:
            q = hist[ch] = deque(maxlen=maxlen)
        q.append((now, value))

    def _filter(self, ch: str) -> float | None:
        """Median of the last ``filter_n`` samples, all taken after the settle; else None."""
        q = self._hist.get(ch)
        n = self.config.filter_n
        if q is None or len(q) < n:
            return None
        last = list(q)[-n:]
        start = self._settle_until
        if start is not None and last[0][0] < start:
            return None
        return _median([v for _, v in last])

    def _vmedian(self, ch: str, now: float) -> float | None:
        q = self._vhist.get(ch)
        if not q:
            return None
        lo = now - self.config.velocity_window_s
        start = self._settle_until if self._settle_until is not None else lo
        vals = [v for t, v in q if t > lo and t >= start]
        return _median(vals) if len(vals) >= 3 else None

    def _vgate(self, channels, threshold: float) -> float:
        """Single-sample velocity gate: ``threshold`` while every channel's reported velocity is
        quiet (robust spread x ``velocity_noise_k`` below it over the last ``velocity_noise_n``
        samples), else ``fast_factor`` x threshold (noisy velocity: the median rule decides)."""
        cfg = self.config
        for ch in channels:
            q = self._vhist.get(ch)
            if q is None or len(q) < cfg.velocity_noise_n:
                return cfg.fast_factor * threshold
            vals = [v for _, v in q]          # all kept samples (<= 64): a short window can look quiet
            m = _median(vals)
            spread = 1.4826 * _median([abs(v - m) for v in vals])
            if cfg.velocity_noise_k * spread >= threshold:
                return cfg.fast_factor * threshold
        return threshold

    def _measure_noise(self, joint_names) -> tuple[float, float, float]:
        cfg = self.config

        def est(chs, cap):
            sig = 0.0
            for ch in chs:
                q = self._hist.get(ch)
                if q is not None:
                    sig = max(sig, _sigma([v for _, v in q]))
            return min(sig, cap)
        nm = max(cfg.noise_m, est(("x", "y"), cfg.noise_cap_m))
        ny = max(cfg.noise_rad, est(("yaw",), cfg.noise_cap_rad))
        nj = max(cfg.noise_rad, est(["j:" + n for n in joint_names], cfg.noise_cap_rad))
        return nm, ny, nj

    def _cond(self, evidence: dict, key: str, flag: bool, value: float, need: int = 1) -> bool:
        count = self._persist.get(key, 0) + 1 if flag else 0
        self._persist[key] = count
        if flag and count >= need:
            evidence[key] = round(value, 4)
            return True
        return False

    def _need(self, noise_floor: float, min_floor: float) -> int:
        """Consecutive samples required: ``persist_n`` when noise sets the floor (a noise peak must
        not count), 1 when the noise is negligible next to the fixed floor (no extra delay)."""
        return self.config.persist_n if noise_floor >= 0.5 * min_floor else 1

    def _hard_bound(self, key: str, now: float, lim: float) -> float:
        """Displacement from the snapshot (never re-based) that counts as motion on channel ``key``
        (``pos``, ``yaw``, ``j:<joint>``). Before any re-stop on that channel: ``total_factor`` x
        limit (position, joints) or, for yaw, ``yaw_lim + yaw_drift_rad_s`` x time since the
        snapshot (IMU drift) but never more than ``max(yaw_total_rad, yaw_lim)`` (Q7-01). After a
        re-stop at displacement R (the ratchet): ``R + lim`` (+ the drift allowance since then,
        yaw) -- a body that came to rest elsewhere is not re-stopped again (Q7-02)."""
        cfg = self.config
        ratchet = self._ratchet.get(key)
        if key == "yaw":
            if ratchet is not None:
                # Q7b-01: capped like the first bound -- every further max(yaw_total_rad, yaw_rad)
                # is a re-stop, however slow (an IMU drift too: bounded re-stops per hour).
                t_r, r = ratchet
                return r + min(self._yaw_step(lim), lim + cfg.yaw_drift_rad_s * (now - t_r))
            t0 = self._snap_t.get("base.odometry", now)
            return min(self._yaw_step(lim), lim + cfg.yaw_drift_rad_s * (now - t0))
        if ratchet is not None:
            return ratchet[1] + lim
        return cfg.total_factor * lim

    def _yaw_step(self, lim: float) -> float:
        """Q7b-01: the largest yaw from the snapshot -- and, after a yaw re-stop at R, beyond R --
        ever tolerated as IMU drift: ``max(yaw_total_rad, yaw_rad)``. Default ``yaw_total_rad`` =
        ``yaw_rad`` = 0.05 rad (as v0.13.0); a field calibration raises it (loud, Q7-05)."""
        return max(self.config.yaw_total_rad, lim)

    def _walk_used(self, now: float) -> tuple[float, float]:
        """Random-walk allowance in use (Q7-02), believed only while it explains what is seen:

        * none while the measurement window still holds a re-stop on direct motion evidence
          (that motion would be mistaken for a walk);
        * Q7b-03: none once the base is further from its walk anchor (the snapshot, or where it
          was at the last re-stop on direct motion) than ``walk_trend_k`` walks x sqrt(t) (+ the
          tolerated yaw drift): a walk has no direction, a slow driver -- even one that adds a
          fake walk to its feedback -- goes somewhere;
        * Q7b-03: while motion re-stops keep coming (less than ``rebase_s`` apart) it never grows
          above what was measured at the last one (re-stops on slow evidence only)."""
        cfg = self.config
        fb = self._filtered.get("base.odometry")
        if self._fast_at is not None and now - self._fast_at <= cfg.noise_history * cfg.period_s:
            self._walk_anchor = None          # re-anchored where the walk is believed again
            return 0.0, 0.0
        if self._walk_anchor is None and fb is not None and "base.odometry" in self._snap:
            self._walk_anchor = (now, fb)
        wm, wy = self._walk
        anchor = self._walk_anchor
        if anchor is not None and fb is not None:
            t_a, a = anchor
            span = max(0.0, now - t_a)
            root = math.sqrt(span)
            noise_m, noise_yaw, _nj = self._noise
            if math.hypot(fb["x"] - a["x"], fb["y"] - a["y"]) > (
                    cfg.walk_trend_k * wm * root + max(cfg.min_linear_delta, cfg.filtered_noise_k * noise_m)):
                wm = 0.0
            if abs(fb["yaw"] - a["yaw"]) > (cfg.walk_trend_k * wy * root + cfg.yaw_drift_rad_s * span
                                             + max(cfg.min_angular_delta, cfg.filtered_noise_k * noise_yaw)):
                wy = 0.0
        ceiling = self._walk_ceiling
        if ceiling is not None and now - ceiling[0] < cfg.rebase_s:
            wm, wy = min(wm, ceiling[1]), min(wy, ceiling[2])
        return wm, wy

    def _ratchet_update(self, now: float) -> None:
        """After a motion re-stop: every channel that is past its first hard bound (or already
        ratcheted) records the displacement it was re-stopped at."""
        pos_lim, yaw_lim, joint_lim = getattr(self, "_limits", (0.0, 0.0, 0.0))
        for key, total in self._totals.items():
            lim = pos_lim if key == "pos" else yaw_lim if key == "yaw" else joint_lim
            old = self._ratchet.get(key)
            if old is not None:
                if total > old[1]:
                    self._ratchet[key] = (now, total)
            elif total > self._hard_bound(key, now, lim):
                # past the first hard bound: from now on R + lim
                self._ratchet[key] = (now, total)

    def _moved_since(self, old: dict, new: dict, dt: float, now: float) -> bool:
        """Escalation (Q7-02): did the body move between two motion re-stops (filtered values),
        beyond noise, odometry random walk and IMU drift?"""
        cfg = self.config
        noise_m, noise_yaw, noise_j = self._noise
        walk_m, walk_yaw = self._walk_used(now)
        root = math.sqrt(max(0.0, dt))
        ob, nb = old.get("base.odometry"), new.get("base.odometry")
        if ob is not None and nb is not None:
            if math.hypot(nb["x"] - ob["x"], nb["y"] - ob["y"]) > max(
                    cfg.min_linear_delta, math.hypot(cfg.filtered_noise_k * noise_m, cfg.walk_k * walk_m * root)):
                return True
            if abs(nb["yaw"] - ob["yaw"]) > max(cfg.min_angular_delta, math.hypot(
                    cfg.filtered_noise_k * noise_yaw, cfg.walk_k * walk_yaw * root)) + cfg.yaw_drift_rad_s * dt:
                return True
        oj, nj = old.get("joint.state"), new.get("joint.state")
        if oj is not None and nj is not None:
            floor = max(cfg.min_angular_delta, cfg.filtered_noise_k * noise_j)
            if any(abs(nj[n] - oj[n]) > floor for n in nj if n in oj):
                return True
        return False

    # ------------------------------------------------------------ evaluation
    def _evaluate(self, now: float, samples: dict, errors: dict, snapshot_ok: bool) -> tuple[dict, dict]:
        """``(motion_evidence, invalid_problems)`` for these samples. ``snapshot_ok``: the stop has
        settled and no reset is in flight, so missing snapshots may be taken now."""
        cfg = self.config
        evidence: dict = {}
        problems: dict = dict(errors)
        parsed: dict[str, dict] = {}
        for cap, sample in samples.items():
            parser = parse_odometry if cap == "base.odometry" else parse_joint_state
            value, problem = parser(sample)
            if problem is not None:
                problems[cap] = problem
            else:
                parsed[cap] = value
        self._v13_evidence = self._v13.evaluate(now, parsed, problems, snapshot_ok, self._yaw_unwrap_peek(parsed))
        js = parsed.get("joint.state")
        if js is not None:
            snap = self._snap.get("joint.state")
            if snap is not None and set(snap) != set(js["position"]):
                problems["joint.state"] = "joint.state 的关节名集合与锁存快照不同"
                del parsed["joint.state"]
                js = None

        maxlen = cfg.noise_history
        current: dict[str, dict] = {}
        odo = parsed.get("base.odometry")
        if odo is not None:
            if self._yaw_raw is None:
                self._yaw_unwrapped = odo["yaw"]
            else:
                d = (odo["yaw"] - self._yaw_raw + math.pi) % (2.0 * math.pi) - math.pi
                self._yaw_unwrapped += d
            self._yaw_raw = odo["yaw"]
            current["base.odometry"] = {"x": odo["x"], "y": odo["y"], "yaw": self._yaw_unwrapped}
            for ch, v in current["base.odometry"].items():
                self._record(self._hist, ch, now, v, maxlen)
            for ch, v in zip(("vx", "vy", "wz"), odo["velocity"]):
                self._record(self._vhist, ch, now, v, 64)
        if js is not None:
            current["joint.state"] = dict(js["position"])
            for n, q in js["position"].items():
                self._record(self._hist, "j:" + n, now, q, maxlen)
            for n, v in (js["velocity"] or {}).items():
                self._record(self._vhist, "jv:" + n, now, v, 64)
        joint_names = list(js["position"]) if js is not None else []
        self._noise_ticks += 1
        if self._noise_ticks >= 5 or self._noise_ticks == 1 or not self._noise_ready:   # every 0.5 s once known
            self._noise_ticks = 1
            self._noise = self._measure_noise(joint_names)
            measured = (min(cfg.walk_cap_m, max(_walk_rate(list(self._hist.get(c, ())), cfg.walk_lag,
                                                           cfg.walk_k, cfg.filtered_noise_k * self._noise[0])
                                                for c in ("x", "y"))),
                        min(cfg.walk_cap_rad, _walk_rate(list(self._hist.get("yaw", ())), cfg.walk_lag,
                                                         cfg.walk_k, cfg.filtered_noise_k * self._noise[1])))
            # Q7b-09: peak over walk_hold_s -- the 20 s estimate fluctuates; a dip must not turn a
            # walking odometry into "motion" now that the position is measured from the snapshot
            hold = self._walk_hist
            hold.append((now, measured[0], measured[1]))
            while hold and now - hold[0][0] > cfg.walk_hold_s:
                hold.popleft()
            self._walk = (max(w[1] for w in hold), max(w[2] for w in hold))
            self._noise_ready = {cap for cap, chs in (("base.odometry", ("x",)),
                                                      ("joint.state", ["j:" + n for n in joint_names]))
                                 if chs and all(len(self._hist.get(c, ())) >= NOISE_MIN_SAMPLES for c in chs)}
        noise_m, noise_yaw, noise_j = self._noise

        # 1. reported velocity: one clear sample, or the median over the window. "Clear": above
        #    fast_factor x threshold -- or, once the reported velocity is known to be quiet (robust
        #    spread over the last samples), above the threshold itself (as v0.13.0: no delay).
        if odo is not None:
            vx, vy, wz = odo["velocity"]
            speed = math.hypot(vx, vy)
            lin_gate = self._vgate(("vx", "vy"), cfg.linear_velocity)
            ang_gate = self._vgate(("wz",), cfg.angular_velocity)
            if speed > lin_gate:
                evidence["base_speed"] = round(speed, 4)
            if abs(wz) > ang_gate:
                evidence["base_wz"] = round(wz, 4)
            mvx, mvy, mwz = (self._vmedian(c, now) for c in ("vx", "vy", "wz"))
            if mvx is not None and mvy is not None and math.hypot(mvx, mvy) > cfg.linear_velocity:
                evidence.setdefault("base_speed", round(math.hypot(mvx, mvy), 4))
            if mwz is not None and abs(mwz) > cfg.angular_velocity:
                evidence.setdefault("base_wz", round(mwz, 4))
        if js is not None and js["velocity"]:
            name, peak = max(js["velocity"].items(), key=lambda kv: abs(kv[1]))
            if abs(peak) > self._vgate(["jv:" + n for n in js["velocity"]], cfg.angular_velocity):
                evidence["joint_velocity"] = round(abs(peak), 4)
                evidence["joint"] = name
            else:
                meds = {n: self._vmedian("jv:" + n, now) for n in js["velocity"]}
                meds = {n: m for n, m in meds.items() if m is not None}
                if meds:
                    name, peak = max(meds.items(), key=lambda kv: abs(kv[1]))
                    if abs(peak) > cfg.angular_velocity:
                        evidence["joint_velocity"] = round(abs(peak), 4)
                        evidence["joint"] = name

        # 2. jump between two raw samples, above the noise floor -- only once the noise is known
        #    (NOISE_MIN_SAMPLES samples; before that the velocity and displacement rules apply)
        #    Round 07 second fix pass: before that, with the largest noise that would be believed
        #    (noise_cap_*), so a jump no sensor noise explains counts from the first sample on.
        base = current.get("base.odometry")
        prev = self._prev.get("base.odometry")
        if base is not None and prev is not None and now - prev[0] > 0:
            known = "base.odometry" in self._noise_ready
            jn_m = noise_m if known else max(noise_m, cfg.noise_cap_m)
            jn_y = noise_yaw if known else max(noise_yaw, cfg.noise_cap_rad)
            old, dt = prev[1], now - prev[0]
            d = math.hypot(base["x"] - old["x"], base["y"] - old["y"])
            if d > max(cfg.min_linear_delta, cfg.sample_noise_k * jn_m) and d > cfg.linear_velocity * dt:
                evidence["base_moved_m"] = round(d, 4)
            dyaw = abs(base["yaw"] - old["yaw"])
            if dyaw > max(cfg.min_angular_delta, cfg.sample_noise_k * jn_y) and dyaw > cfg.angular_velocity * dt:
                evidence["base_yaw_rad"] = round(dyaw, 4)
        joints = current.get("joint.state")
        prev = self._prev.get("joint.state")
        if joints is not None and prev is not None and now - prev[0] > 0:
            if "joint.state" not in self._noise_ready:
                noise_j_jump = max(noise_j, cfg.noise_cap_rad)
            else:
                noise_j_jump = noise_j
            old, dt = prev[1], now - prev[0]
            common = [n for n in joints if n in old]
            if common:
                name = max(common, key=lambda n: abs(joints[n] - old[n]))
                peak = abs(joints[name] - old[name])
                if peak > max(cfg.min_angular_delta, cfg.sample_noise_k * noise_j_jump) and peak > cfg.angular_velocity * dt:
                    evidence["joint_moved_rad"] = round(peak, 4)
                    evidence.setdefault("joint", name)

        # 3. displacement on filtered values
        filtered: dict[str, dict] = {}
        if base is not None:
            fb = {ch: self._filter(ch) for ch in ("x", "y", "yaw")}
            if all(v is not None for v in fb.values()):
                filtered["base.odometry"] = fb
        if joints is not None:
            fj = {n: self._filter("j:" + n) for n in joints}
            if all(v is not None for v in fj.values()):
                filtered["joint.state"] = fj
        # While the noise is negligible the raw sample stands in until the filter has its first
        # filter_n samples after the settle (the snapshot is not taken two samples late).
        settled = self._settle_until is None or now >= self._settle_until
        k_noise = cfg.filtered_noise_k
        if (base is not None and "base.odometry" not in filtered and settled
                and k_noise * noise_m < 0.5 * cfg.min_linear_delta and k_noise * noise_yaw < 0.5 * cfg.min_angular_delta):
            filtered["base.odometry"] = dict(base)
        if (joints is not None and "joint.state" not in filtered and settled
                and k_noise * noise_j < 0.5 * cfg.min_angular_delta):
            filtered["joint.state"] = dict(joints)
        for cap, f in filtered.items():
            if cap not in self._snap and snapshot_ok:
                self._snap[cap] = f
                self._snap_t[cap] = now
                self._ref[cap] = (now, f)
        pos_lim = max(cfg.max_linear_drift, NOISE_MARGIN * noise_m)
        yaw_lim = max(cfg.max_angular_drift, NOISE_MARGIN * noise_yaw)
        joint_lim = max(self._joint_limit, NOISE_MARGIN * noise_j)
        walk_m, walk_yaw = self._walk_eff = self._walk_used(now)
        totals: dict[str, float] = {}
        # Values compared: the median-filtered ones while the noise matters, the raw sample while
        # it is negligible next to min_*_delta (no filter delay: as tight as v0.13.0, Q7b-02).
        raw_m = cfg.filtered_noise_k * noise_m < 0.5 * cfg.min_linear_delta
        raw_y = cfg.filtered_noise_k * noise_yaw < 0.5 * cfg.min_angular_delta
        raw_j = cfg.filtered_noise_k * noise_j < 0.5 * cfg.min_angular_delta

        def pick(f, raw):
            return {"x": (raw if raw_m else f)["x"], "y": (raw if raw_m else f)["y"],
                    "yaw": (raw if raw_y else f)["yaw"]}
        fb = filtered.get("base.odometry")
        if fb is not None and "base.odometry" in self._snap:
            pb = pick(fb, base)
            snap = self._snap["base.odometry"]
            ref_t, ref = self._ref["base.odometry"]
            total = math.hypot(pb["x"] - snap["x"], pb["y"] - snap["y"])
            yaw_total = abs(pb["yaw"] - snap["yaw"])
            totals["pos"], totals["yaw"] = total, yaw_total
            if total > pos_lim:
                self._notice_candidate("base_position", total, "m")
            elif yaw_total > yaw_lim:
                self._notice_candidate("base_yaw", yaw_total, "rad")
            with self._lock:
                self.max_drift_m = max(self.max_drift_m, total)
                self.max_drift_rad = max(self.max_drift_rad, yaw_total)
            # Hard bounds from the snapshot, never re-based (Q7-01); after a re-stop at displacement
            # R the next one is R + step (Q7-02: a body that settled elsewhere is not re-stopped
            # again and again; one that keeps going is; Q7b-01: the yaw step is capped).
            self._cond(evidence, "base_drift_total_m", total > self._hard_bound("pos", now, pos_lim), total)
            self._cond(evidence, "base_drift_yaw_total_rad", yaw_total > self._hard_bound("yaw", now, yaw_lim),
                       yaw_total)
            if not self._displaced:
                # Q7b-09: position from the snapshot, never re-based (as v0.13.0); an odometry
                # random walk measured on the feedback (and still believed) widens it.
                d = total
                spread = math.hypot(cfg.filtered_noise_k * noise_m, cfg.walk_k * walk_m * math.sqrt(
                    max(0.0, now - self._snap_t.get("base.odometry", now)) + cfg.walk_t0_s))
                self._cond(evidence, "base_drift_m", d > max(pos_lim, spread), d)
                # The reference is re-based every rebase_s, which already absorbs yaw_lim / rebase_s
                # of drift: only a declared drift rate above that is added (no double count, Q7-01).
                extra = max(0.0, cfg.yaw_drift_rad_s - yaw_lim / cfg.rebase_s) * (now - ref_t)
                dyaw = abs(pb["yaw"] - ref["yaw"])
                self._cond(evidence, "base_drift_yaw_rad", dyaw > yaw_lim + extra, dyaw)
            else:
                # Q7b-02: once displaced, the body stays displaced for the whole latch episode;
                # the place it was re-stopped at (re-based after rebase_s without motion) is the
                # reference -- resuming after a pause never gets a fresh pos_m allowance.
                rt, rr, rraw = self._restop_ref.setdefault("base.odometry", (now, fb, base))
                r0 = pick(rr, rraw)
                d = math.hypot(pb["x"] - r0["x"], pb["y"] - r0["y"])
                spread = math.hypot(cfg.filtered_noise_k * noise_m,
                                    cfg.walk_k * walk_m * math.sqrt(max(0.0, now - rt) + cfg.walk_t0_s))
                self._cond(evidence, "base_moved_since_restop_m", d > max(cfg.min_linear_delta, spread), d,
                           self._need(spread, cfg.min_linear_delta))
                dy = abs(pb["yaw"] - r0["yaw"])
                spread = math.hypot(cfg.filtered_noise_k * noise_yaw,
                                    cfg.walk_k * walk_yaw * math.sqrt(max(0.0, now - rt) + cfg.walk_t0_s))
                self._cond(evidence, "base_yaw_since_restop_rad",
                           dy > max(cfg.min_angular_delta, spread) + cfg.yaw_drift_rad_s * (now - rt), dy,
                           self._need(spread, cfg.min_angular_delta))
        fj = filtered.get("joint.state")
        if fj is not None and "joint.state" in self._snap:
            pj = joints if raw_j else fj
            snap = self._snap["joint.state"]
            over = None
            for n in pj:
                t_n = abs(pj[n] - snap[n])
                totals["j:" + n] = t_n
                if t_n > self._hard_bound("j:" + n, now, joint_lim) and (
                        over is None or t_n > totals["j:" + over]):
                    over = n
            name = max(pj, key=lambda n: abs(pj[n] - snap[n]))
            total = abs(pj[name] - snap[name])
            if total > joint_lim:
                self._notice_candidate("joint:" + name, total, "rad")
            with self._lock:
                self.max_drift_rad = max(self.max_drift_rad, total)
            if self._cond(evidence, "joint_drift_total_rad", over is not None,
                          totals["j:" + over] if over is not None else total):
                evidence.setdefault("joint", over)
            if not self._displaced:
                # Q7b-09: joints from the snapshot, never re-based (encoders do not drift; as v0.13.0)
                if self._cond(evidence, "joint_drift_rad", total > joint_lim, total):
                    evidence.setdefault("joint", name)
            else:
                _rt, rr, rraw = self._restop_ref.setdefault("joint.state", (now, fj, joints))
                ref0 = rraw if raw_j else rr
                rname = max(pj, key=lambda n: abs(pj[n] - ref0.get(n, pj[n])))
                d = abs(pj[rname] - ref0.get(rname, pj[rname]))
                if self._cond(evidence, "joint_moved_since_restop_rad",
                              d > max(cfg.min_angular_delta, cfg.filtered_noise_k * noise_j), d,
                              self._need(cfg.filtered_noise_k * noise_j, cfg.min_angular_delta)):
                    evidence.setdefault("joint", rname)
        self._totals = totals
        self._limits = (pos_lim, yaw_lim, joint_lim)
        self._current = current
        with self._lock:
            self._filtered = filtered

        for cap, value in current.items():
            self._prev[cap] = (now, value)
        for cap in list(self._invalid_since):
            if cap not in problems:
                del self._invalid_since[cap]
        due = {}
        for cap, problem in problems.items():
            since = self._invalid_since.setdefault(cap, now)
            if now - since >= cfg.invalid_grace_s:
                due[cap] = problem
        with self._lock:
            self._invalid_now = dict(problems)
        return evidence, due

    def _quiet(self, now: float, evidence: dict) -> None:
        """No motion this sample: re-base the reference after ``rebase_s`` (slow sensor drift is
        not motion); end "displaced" after ``rebase_s`` without motion evidence."""
        cfg = self.config
        filtered = self._filtered
        if self._displaced:
            # Q7b-02: "displaced" never ends within the episode (it used to end after rebase_s,
            # handing a resuming driver a fresh pos_m each cycle); only the re-stop reference is
            # re-based after rebase_s without motion evidence, so the random-walk spread restarts.
            quiet = self._last_motion is None or now - self._last_motion >= cfg.rebase_s
            current = self._current
            for cap, (rt, _rr, _raw) in list(self._restop_ref.items()):
                if quiet and now - rt >= cfg.rebase_s and cap in filtered and cap in current:
                    self._restop_ref[cap] = (now, filtered[cap], current[cap])
                    with self._lock:
                        self.rebases += 1
            return
        for cap, (ref_t, _ref) in list(self._ref.items()):
            # only the base yaw window uses the re-based reference (IMU drift); position and
            # joints are measured from the snapshot (Q7b-09)
            if cap == "base.odometry" and now - ref_t >= cfg.rebase_s and cap in filtered:
                self._ref[cap] = (now, filtered[cap])
                with self._lock:
                    self.rebases += 1

    # ------------------------------------------------------------ events
    def _report(self, kind: str, now: float, payload: dict) -> None:
        """First re-stop of a kind: event at once; then at most one per ``event_interval_s``,
        ``count`` = re-stops it stands for (Q6-01: the 5000-event ring is not flooded)."""
        event_type = "safety.motion_while_latched" if kind == "motion" else "safety.feedback_invalid_while_latched"
        with self._lock:
            st = self._ev[kind]
            due = st["last"] is None or now - st["last"] >= self.config.event_interval_s
            if due:
                count, st["last"], st["pending"], st["payload"] = st["pending"] + 1, now, 0, None
            else:
                st["pending"] += 1
                st["payload"] = payload
                self.events_suppressed += 1
        if due:
            self._events.emit(event_type, count=count, **payload)

    def _flush_events(self, now: float, force: bool = False) -> None:
        for kind in ("motion", "invalid"):
            event_type = "safety.motion_while_latched" if kind == "motion" else "safety.feedback_invalid_while_latched"
            with self._lock:
                st = self._ev[kind]
                if not st["pending"] or not (force or now - st["last"] >= self.config.event_interval_s):
                    if force:
                        st["last"] = None
                    continue
                count, payload = st["pending"], st["payload"]
                st["pending"], st["payload"] = 0, None
                st["last"] = None if force else now
            self._events.emit(event_type, count=count, summary=True, **payload)

    SLOW_KEYS = ("base_drift_total_m", "base_drift_yaw_total_rad", "joint_drift_total_rad", "joint",
                 "base_moved_since_restop_m", "base_yaw_since_restop_rad", "joint_moved_since_restop_rad")
    DIRECT_MOTION = ("base_speed", "base_wz", "joint_velocity", "base_moved_m", "base_yaw_rad", "joint_moved_rad")

    def _count_streak(self, now: float, evidence: dict, filtered: dict) -> dict:
        """Q7-02 escalation evidence: a motion re-stop extends the streak only if the body kept
        moving since the previous counted one (reported velocity / sample jump, or the filtered
        position moved beyond noise, random walk and drift) and came within ``escalate_gap_s`` of
        it. A body that rests away from the snapshot (sat down, pushed, odometry wander) does
        not build a streak, so it cannot reach ``safety.stop_not_obeyed``."""
        cfg = self.config
        st = self._streak
        if st is None or now - st["t_last"] > cfg.escalate_gap_s:
            st = self._streak = {"n": 1, "t0": now, "t_last": now, "ref": filtered}
            return st
        moved = any(k in evidence for k in self.DIRECT_MOTION) or self._moved_since(st["ref"], filtered,
                                                                                   now - st["t_last"], now)
        if moved:
            st["n"] += 1
            st["t_last"] = now
            st["ref"] = filtered
        return st

    def flush(self) -> None:
        """Q7-08: emit the aggregated counts still pending (runtime ``close()``), so the sum of
        ``count`` over the events equals the re-stops. Never calls the adapter."""
        try:
            self._flush_events(self._clock.now(), force=True)
        except Exception:
            pass

    # ------------------------------------------------------------ restop
    def _restop(self, now: float, evidence: dict, invalid: dict, replica_paced: bool = True) -> bool:
        """Re-stop at the bounded rate and report it. Returns True if it re-stopped (counted).
        Q7d-01/02: ``adapter.stop()`` is called only on the guard's single stop() cadence
        (:meth:`_slot_open`) and, while the precautionary stops are on and ``replica_paced``
        (guard thread), only where v0.13.0's rule also sees motion / invalid feedback; otherwise
        the re-stop is merged into the last stop() (``merged_stops``)."""
        sup = self._sup
        cfg = self.config
        with self._restop_lock:
            if (self._last_restop_robot is not None
                    and now - self._last_restop_robot < cfg.restop_interval_s):
                return False
            issue = self._slot_open(now)
            if (issue and replica_paced and cfg.precaution_interval_s > 0
                    and not self._v13_evidence and not invalid):
                # Q7d-01: with the precautionary stops on, the guard thread's stop() calls are
                # v0.13.0's (the replica's): a counted re-stop calls stop() only in a sample where
                # v0.13.0's rule sees motion too (the replica merges into it); otherwise it is
                # merged into the replica's stream (its stop() would shift the replica's cadence)
                issue = False
            self._last_restop_robot = now
            if issue:
                self._mark_call(now, "tick" if replica_paced else "stall")
        merged = not issue
        if issue:
            ok = self._last_call_ok = sup.latched_restop()
        else:
            # Q7d-01: merged (another guard stop() < restop_interval_s ago, or not on the
            # replica's beat): counted, reported, escalates -- no stop() call of its own.
            # Q7e-02: its events say so (``adapter_stopped: false``, ``merged: true``)
            ok = False
            with self._lock:
                self.merged_stops += 1
        if evidence and any(k.startswith(("base_drift", "joint_drift")) for k in evidence):
            self._displaced = True
        # Where the body is now: the reference for "moved again since the last re-stop".
        filtered = self._filtered            # replaced, never mutated: safe from the monitor thread
        current = self._current
        self._restop_ref = {cap: (now, f, current.get(cap, f)) for cap, f in filtered.items()}
        escalate = None
        with self._lock:
            self.restops += 1
            if invalid:
                self.invalid_restops += 1
            if evidence:
                self.motion_restops += 1
            self.last_restop_at = time.time()
            seen, restops = self.seen, self.restops
            invalid_count, invalid_restops = self.feedback_invalid, self.invalid_restops
            motion_restops = self.motion_restops
        if evidence:
            if any(k not in self.SLOW_KEYS for k in evidence):
                # velocity, a sample jump or leaving the re-based reference: real motion, not an
                # odometry random walk -- the walk measured over this history is not believed
                self._fast_at = now
            else:
                # Q7b-03: re-stops on slow evidence keep coming -- the measured walk may not grow
                # past what it was at this one (a liar cannot raise its fake walk to buy more)
                ceiling = self._walk_ceiling
                active = ceiling is not None and now - ceiling[0] < cfg.rebase_s
                wm, wy = self._walk
                cm, cy = (ceiling[1], ceiling[2]) if active else (math.inf, math.inf)
                self._walk_ceiling = (now, min(cm, wm) if wm > 0 else cm, min(cy, wy) if wy > 0 else cy)
            self._ratchet_update(now)
            if self._first_motion_restop is None:
                self._first_motion_restop = now
            streak = self._count_streak(now, evidence, filtered)
            span = streak["t_last"] - streak["t0"]
            slow = streak["n"] >= cfg.escalate_after and span >= cfg.escalate_min_s
            # Q7b-03: displacement since the latch counts across re-stops whatever the gaps between
            # them (a driver cannot dodge it by waiting out escalate_gap_s or faking a walk)
            fast = motion_restops >= cfg.escalate_fast_n and self.max_drift_m >= cfg.escalate_displacement_m
            if self.stop_not_obeyed is None and (slow or fast):
                escalate = {"at": time.time(), "motion_restops": motion_restops, "restops": restops,
                            "since_s": round(now - self._first_motion_restop, 1),
                            "streak_restops": streak["n"], "streak_s": round(span, 1),
                            "trigger": "displacement" if fast and not slow else "restops",
                            "displacement_m": round(self.max_drift_m, 4),
                            "max_drift_m": round(self.max_drift_m, 4),
                            "max_drift_rad": round(self.max_drift_rad, 4), "evidence": evidence,
                            "message": NOT_OBEYED_NOTE}
                with self._lock:
                    self.stop_not_obeyed = escalate
        if evidence:
            self._report("motion", now, {"seen": seen, "restops": restops, "motion_restops": motion_restops,
                                         "adapter_stopped": ok, "merged": merged, "evidence": evidence,
                                         "reason": sup.latch_reason, "note": MOTION_NOTE})
        if invalid:
            self._report("invalid", now, {"problems": invalid, "feedback_invalid": invalid_count,
                                          "invalid_restops": invalid_restops, "restops": restops,
                                          "adapter_stopped": ok, "merged": merged,
                                          "reason": sup.latch_reason, "note": INVALID_NOTE})
        if escalate is not None:
            # event "ts" (wall time) is added by the event bus; message/note are the same Chinese text
            self._events.emit("safety.stop_not_obeyed", adapter_stopped=ok, merged=merged, reason=sup.latch_reason,
                              message=NOT_OBEYED_NOTE, note=NOT_OBEYED_NOTE,
                              **{k: v for k, v in escalate.items() if k not in ("at", "message")})
        return True

    def _notice_candidate(self, channel: str, displacement: float, unit: str) -> None:
        if self.drift_notice is None and self._notice_due is None:
            self._notice_due = (channel, displacement, unit)

    def _emit_drift_notice(self) -> None:
        """Commander decision (round 07 second fix pass): once per latch, when the displacement
        since the latch snapshot first exceeds the first-detection bound (``pos_m`` / ``yaw_rad``
        / ``joint_rad``, noise-raised) -- whether it then counts as motion or is attributed to
        walk / drift and only precautionary stops follow -- one ``safety.latched_drift_notice``
        ``{message, channel, displacement, unit, ts}``. Never escalates, never re-stops; a body
        whose odometry wanders gets exactly one per latch."""
        channel, displacement, unit = self._notice_due
        shown = (f"{displacement * 100:.0f} 厘米" if unit == "m"
                 else f"{displacement:.2f} rad（约 {math.degrees(displacement):.0f}°）")
        what = "底盘位置" if channel == "base_position" else "底盘朝向" if channel == "base_yaw" else f"关节 {channel[6:]}"
        message = f"急停期间机器人离停下位置已超过 {shown}（{what}），可能是里程计漂移，也可能在移动，请到现场确认"
        notice = {"at": time.time(), "channel": channel, "displacement": round(displacement, 4), "unit": unit,
                  "message": message}
        with self._lock:
            self.drift_notice = notice
            self._notice_latch = self._latch_id()
        self._events.emit("safety.latched_drift_notice", message=message, channel=channel,
                          displacement=round(displacement, 4), unit=unit)

    def _yaw_unwrap_peek(self, parsed: dict) -> float | None:
        """The unwrapped base yaw this sample will get (same as in :meth:`_evaluate`)."""
        odo = parsed.get("base.odometry")
        if odo is None:
            return None
        if self._yaw_raw is None:
            return odo["yaw"]
        return self._yaw_unwrapped + (odo["yaw"] - self._yaw_raw + math.pi) % (2.0 * math.pi) - math.pi

    def _precaution(self, now: float, invalid: dict) -> bool:
        """Q7c-01 (round 07 third fix pass): wherever and whenever v0.13.0 would have re-stopped
        (:class:`V013Replica`: same raw samples, same thresholds, no persistence, its own
        ``restop_interval_s`` cadence) but this guard did not call ``stop()`` in this sample (the
        walk / drift / noise allowance explains the movement), tell the adapter to stop again
        *silently*: ``stop()`` is idempotent by the HAL contract, so a body really at rest pays
        nothing, while a driver that obeys each stop for a while and then resumes is held exactly
        as v0.13.0 held it. Not a re-stop: no event, no ``restops`` / ``motion_restops``, never
        escalates; counted in ``precautionary_stops``. ``precaution_interval_s`` <= 0 turns it off.

        Q7d-01/02 (round 07 fourth fix pass): one cadence for every guard ``stop()``. If this
        guard already called ``stop()`` in this sample, that is the replica's re-stop too; if a
        guard ``stop()`` went out less than ``restop_interval_s`` ago (only the monitor thread's
        stalled-read stop can do that: counted re-stops call ``stop()`` only on the replica's
        beat), that stop is the replica's re-stop at that time (its cadence and reference move
        there) and no second ``stop()`` is sent.

        Q7e-01 (round 07 fifth fix pass): "this guard already called ``stop()`` in this sample"
        means the guard thread issued it in this very iteration (``_last_call_src == "tick"`` and
        the same iteration number), never "the last call has the same robot time": with a coarse
        adapter clock the monitor thread's read-hang stop and the first sample after the hang can
        share a time stamp. When the latest ``stop()`` was a read-hang re-stop, the replica's
        re-stop is always merged into it, without evidence and with the reference where the body
        was at that stop (v0.13.0's read-hang re-stop: reference = the sample before the hang, no
        evidence, not "displaced")."""
        if self.config.precaution_interval_s <= 0:
            return False
        evidence = self._v13_evidence
        if not self._v13.due(now, evidence, invalid):
            return False
        with self._restop_lock:
            last = self._last_call
            stall = last is not None and self._last_call_src == "stall"
            this_sample = (last is not None and self._last_call_src == "tick"
                           and self._last_call_tick == self._tick_seq)
            issue = not this_sample and self._slot_open(now)
            if issue:
                self._mark_call(now, "tick")
            at = self._v13_at_call
        if this_sample:
            self._v13.restopped(now, evidence)
            return False
        if issue:
            self._v13.restopped(now, evidence)
            self._last_call_ok = self._sup.latched_restop()
            with self._lock:
                self.precautionary_stops += 1
            return False
        self._v13.restopped(last, {} if stall else evidence, at=at)
        with self._lock:
            self.merged_stops += 1
        return False

    def _slot_open(self, now: float) -> bool:
        """Q7d-01/02: every guard-issued ``stop()`` (counted re-stop, precautionary stop, invalid /
        stalled feedback) shares one cadence, at least ``restop_interval_s`` apart. Call with
        ``_restop_lock`` held. Operator stops are not part of it."""
        return self._last_call is None or now - self._last_call >= self.config.restop_interval_s

    def _mark_call(self, now: float, src: str) -> None:
        """A guard ``stop()`` goes out now (``_restop_lock`` held); ``src``: ``"tick"`` (guard
        thread, this iteration) or ``"stall"`` (monitor thread, read-hang re-stop)."""
        self._last_call = now
        self._last_call_src = src
        self._last_call_tick = self._tick_seq if src == "tick" else None
        self._v13_at_call = dict(self._v13.last_parsed)
        with self._lock:
            self.stop_calls += 1

    # ------------------------------------------------------------ tick
    def tick(self, closing: threading.Event | None = None) -> bool:
        """One guard iteration. Returns True if it re-stopped the adapter."""
        sup = self._sup
        self._tick_seq += 1                    # Q7e-01: identifies "this sample" for _precaution
        if not self.sources or not sup.latched:
            with self._lock:
                self.drift_notice = None           # once per latch: a new latch may notify again
                self._notice_latch = None
            if self._episode_stop_count is not None:
                self._reset_episode(self._clock.now(), 0.0)
                self._new_episode()
            self._episode_stop_count = None
            self._saw_reset = False
            return False
        now = self._clock.now()
        latch_id = self._latch_id()
        if self.drift_notice is not None and self._notice_latch != latch_id:
            # Q7c-02: a reset succeeded since the notice (e.g. re-stopped within one guard period
            # of the reset, never seen unlatched): it belonged to the previous latch
            with self._lock:
                self.drift_notice = None
                self._notice_latch = None
        count = sup.stop_count
        if count != self._episode_stop_count:
            # A (new) counted stop: give the body settle_s to brake before motion counts; the
            # snapshot is taken after that.
            self._episode_stop_count = count
            self._reset_episode(now, self.settle_s)
            self._new_episode()
        in_flight = sup.reset_in_flight_s
        if in_flight is not None:
            self._saw_reset = True
        elif self._saw_reset:
            # A reset attempt ended and the latch is still on (refused / raced): the body may have
            # moved legitimately during adapter.reset(); settle again and take a new snapshot.
            self._saw_reset = False
            self._reset_episode(now, self.settle_s)
        samples, errors = self._sample()
        if closing is not None and closing.is_set():
            return False
        now = self._clock.now()
        in_settle = self._settle_until is not None and now < self._settle_until
        in_flight = sup.reset_in_flight_s
        observing_reset = in_flight is not None and in_flight < self.config.reset_grace_s
        evidence, invalid = self._evaluate(now, samples, errors,
                                           snapshot_ok=not in_settle and in_flight is None)
        self._flush_events(now)
        if not in_settle and not evidence:
            self._quiet(now, evidence)
        if sup.latched and not in_settle and in_flight is None and self._notice_due is not None:
            self._emit_drift_notice()
        self._notice_due = None
        if not sup.latched or in_settle:
            return False
        if closing is not None and closing.is_set():
            return False
        stopped = False
        if evidence or invalid:
            if observing_reset:
                with self._lock:
                    self.seen_during_reset += 1
                return False
            with self._lock:
                if evidence:
                    self.seen += 1
                    self.last_at = time.time()
                    self.last_evidence = evidence
                if invalid:
                    self.feedback_invalid += 1
                    self.last_invalid = {"at": time.time(), "problems": invalid}
            if evidence:
                self._last_motion = now
            if closing is not None and closing.is_set():
                return False
            stopped = self._restop(now, evidence, invalid)
        if not observing_reset and not (closing is not None and closing.is_set()):
            self._precaution(now, invalid)
        return stopped

    def check_stalled_read(self) -> bool:
        """Called from the monitor thread (Q5-02): a feedback read that has been blocking for
        ``read_stall_s`` means stillness cannot be confirmed; re-stop at the bounded rate."""
        reading = self._reading
        sup = self._sup
        if reading is None or not sup.latched:
            return False
        cap, started = reading
        stalled = time.monotonic() - started
        if stalled < self.config.read_stall_s:
            return False
        now = self._clock.now()
        if self._settle_until is not None and now < self._settle_until:
            return False
        in_flight = sup.reset_in_flight_s
        if in_flight is not None and in_flight < self.config.reset_grace_s:
            return False
        if self._stall_reported != started:
            self._stall_reported = started
            with self._lock:
                self.feedback_invalid += 1
                problem = {cap: f"读取 {cap} 已阻塞 {stalled:.1f} s"}
                self.last_invalid = {"at": time.time(), "problems": problem}
        return self._restop(now, {}, {cap: f"读取 {cap} 已阻塞 {stalled:.1f} s"}, replica_paced=False)
