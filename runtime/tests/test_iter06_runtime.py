"""Round-06 runtime regressions (docs/evolution/reviews/2026-11-09-iter05-review.md).

* Q5-01: the latch guard tracks cumulative displacement since the latch snapshot (base x/y/yaw,
  every joint by name): 0.015 / 0.019 m/s creeps (below the per-sample threshold) are re-stopped
  within bounded time and bounded displacement; a still or jittering body is never re-stopped.
* Q5-02: feedback that cannot confirm stillness (NaN, short arrays, ``velocity: null``, read errors,
  changed joint-name set, a read that hangs) re-stops at the bounded rate and emits
  ``safety.feedback_invalid_while_latched``; ``/v1/status.latched_motion`` shows it. Joint
  comparisons are by name, so rotating ``names`` neither hides motion nor causes false alarms.
* Q5-05: skill parameters are checked against skill.json (min/max, choices/options, length) on
  the device for every entry point; ``loops: 99`` with a declared 1–3 is 400 ``param_out_of_range``
  with zero adapter calls.
* Q5-03: ``/v1/status`` carries ``reset_in_progress_s`` / ``reset_hint``; the 409 body has a stable
  ``code`` and a Chinese ``message`` + ``hint`` naming the way out (restart keeps the latch).
* Q5-06: an adapter-declared ``stop_settle_s`` (bounded) stops controlled sit-downs being reported
  as motion. Q5-07: a reset aborted by the guard says so. Q5-08: close() has a bounded join and a
  guard stuck in a hung read makes no adapter call afterwards and exits when the read returns.
"""
import math
import tempfile
import threading
import time
import unittest

import _support  # noqa: F401
from _support import TOKEN, RuntimeFixture, example, http

from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import BadRequest, RuntimeApiError
from neuxmind_runtime.gateway import GatewayServer
from neuxmind_runtime.latch_guard import GuardConfig
from neuxmind_runtime.safety import SafetySupervisor
from neuxmind_runtime.skills import check_parameters

from test_iter05_runtime import events_of, make_runtime


def pending(runtime, kind):
    """Re-stops not yet reported in an (aggregated) event."""
    return runtime.latch_guard._ev[kind]["pending"]


class Creeper(SimQuadruped):
    """After every stop() the driver resumes ``speed`` m/s ``delay`` s later (r5/attack_a.py A1)."""

    def __init__(self, speed, delay=2.0, **kw):
        super().__init__(**kw)
        self.speed, self.delay = speed, delay
        self.resume_at = None
        self.motion_cmds = 0
        self.stopped = False

    def invoke(self, cap_id, args):
        if self.stopped:
            self.motion_cmds += 1
        return super().invoke(cap_id, args)

    def stop(self):
        super().stop()
        self.stopped = True
        self.resume_at = self._t + self.delay

    def _step(self, dt):
        if self.resume_at is not None and self._t >= self.resume_at:
            self.resume_at = None
            self.cmd = (self.speed, 0.0, 0.0)
        super()._step(dt)


def tick_for(runtime, adapter, clock, seconds, on_tick=None):
    for i in range(int(round(seconds / 0.1))):
        clock.advance(0.1)
        runtime.latch_guard.tick()
        adapter.advance()
        if on_tick is not None:
            on_tick(i)


# ====================================================================== Q5-01
class CumulativeDisplacement(unittest.TestCase):
    def _creep(self, speed, seconds=600.0, **guard):
        clock = ManualClock()
        adapter = Creeper(speed, clock=clock)
        kw = {"latch_guard": GuardConfig(**guard)} if guard else {}
        runtime = make_runtime(self, adapter, start_monitor=False, **kw)
        runtime.emergency_stop("operator")
        x0 = adapter.x
        first = []

        def note(i):
            if not first and runtime.latch_guard.restops:
                first.append(((i + 1) * 0.1, adapter.x - x0))
        tick_for(runtime, adapter, clock, seconds, note)
        sup = runtime.supervisor
        self.assertTrue(sup.latched)
        self.assertEqual(sup.stop_count, 1)
        self.assertEqual(sup.latch_reason, "operator")
        self.assertEqual(adapter.motion_cmds, 0)
        return runtime, adapter, first[0] if first else None, adapter.x - x0

    def test_creep_0015_and_0019_detected_and_bounded(self):
        for speed in (0.015, 0.019):
            with self.subTest(speed=speed):
                runtime, _ad, first, total = self._creep(speed)
                self.assertIsNotNone(first)
                t, disp = first
                self.assertLessEqual(t, 6.0)                 # 1 s settle + 2 s still + ≤ 3.4 s creep
                self.assertLessEqual(disp, 0.06)             # bound 0.05 m + one sample
                self.assertLess(total, 1.5)                  # round 05: 8.97 / 11.36 m
                lm = runtime.status()["latched_motion"]
                self.assertTrue(lm["displaced"])
                self.assertGreater(lm["restops"], 200)
                drift = [e for e in events_of(runtime, "safety.motion_while_latched")
                         if "base_drift_m" in e["evidence"]]
                self.assertEqual(len(drift), 1)              # first crossing; afterwards "since re-stop"

    def test_configurable_bound(self):
        _rt, _ad, first, _total = self._creep(0.015, seconds=20.0, max_linear_drift=0.02)
        self.assertLessEqual(first[1], 0.03)

    def test_still_and_jittering_body_never_restopped(self):
        class Jitter(SimQuadruped):
            def read(self, cap_id):
                out = super().read(cap_id)
                if cap_id == "base.odometry":
                    k = self._t
                    out["pose"][0] += 0.0025 * math.sin(7.3 * k)        # ±2.5 mm balance jitter
                    out["pose"][1] += 0.0025 * math.cos(5.1 * k)
                    out["velocity"] = [0.01 * math.cos(7.3 * k), 0.0, 0.02 * math.sin(3.0 * k)]
                return out
        for cls in (SimQuadruped, Jitter):
            with self.subTest(body=cls.__name__):
                clock = ManualClock()
                adapter = cls(clock=clock)
                runtime = make_runtime(self, adapter, start_monitor=False)
                runtime.emergency_stop("operator")
                tick_for(runtime, adapter, clock, 600.0)
                lm = runtime.latch_guard.describe()
                self.assertEqual(lm["restops"], 0)
                self.assertTrue(lm["feedback_ok"])
                self.assertLess(lm["max_drift_m"], 0.01)

    def test_joint_creep_below_velocity_threshold(self):
        class ArmCreep(SimArm):
            resume_at = None

            def stop(self):
                super().stop()
                self.resume_at = self._t + 2.0

            def _step(self, dt):
                super()._step(dt)
                if self.resume_at is not None and self._t >= self.resume_at:
                    self.q[2] += 0.04 * dt                 # 0.04 rad/s < 0.05 threshold
        clock = ManualClock()
        adapter = ArmCreep(clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("operator")
        q0 = adapter.q[2]
        first = []
        tick_for(runtime, adapter, clock, 60.0,
                 lambda i: first or not runtime.latch_guard.restops or first.append(adapter.q[2] - q0))
        self.assertTrue(first)
        self.assertLessEqual(first[0], 0.06)
        ev = events_of(runtime, "safety.motion_while_latched")[0]["evidence"]
        self.assertEqual(ev.get("joint"), "joint3")
        self.assertIn("joint_drift_rad", ev)

    def test_new_counted_stop_and_reset_clear_the_snapshot(self):
        clock = ManualClock()
        adapter = SimQuadruped(clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("a")
        tick_for(runtime, adapter, clock, 3.0)
        runtime.reset({"hardware_authorized": True})
        runtime.latch_guard.tick()                         # not latched: episode state cleared
        adapter.x += 1.0                                   # moved legitimately while unlatched
        runtime.emergency_stop("b")
        tick_for(runtime, adapter, clock, 10.0)
        self.assertEqual(runtime.latch_guard.restops, 0)


# ====================================================================== Q5-02
class Blind(Creeper):
    mode = "nan"

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "base.odometry" and self.stopped:
            if self.mode == "nan":
                return {"pose": [float("nan")] * 7, "velocity": [float("nan")] * 3}
            if self.mode == "short":
                return {"pose": [0.0, 0.0, 0.0], "velocity": [0.0]}
            if self.mode == "none_vel":
                return {"pose": out["pose"], "velocity": None}
            if self.mode == "raises":
                raise RuntimeError("bus timeout")
        return out


class InvalidFeedback(unittest.TestCase):
    def test_unreadable_feedback_restops_with_event_and_status(self):
        for mode in ("nan", "short", "none_vel", "raises"):
            with self.subTest(mode=mode):
                clock = ManualClock()
                adapter = Blind(0.3, clock=clock)
                adapter.mode = mode
                runtime = make_runtime(self, adapter, start_monitor=False)
                runtime.emergency_stop("operator")
                x0 = adapter.x
                tick_for(runtime, adapter, clock, 30.0)
                lm = runtime.status()["latched_motion"]
                self.assertGreaterEqual(lm["restops"], 50)          # ~every 0.5 s after settle + grace
                self.assertLessEqual(lm["restops"], 60)             # bounded rate
                self.assertEqual(lm["restops"], lm["invalid_restops"])
                self.assertFalse(lm["feedback_ok"])
                self.assertIn("base.odometry", lm["feedback_problems"])
                self.assertIn("base.odometry", lm["last_invalid"]["problems"])
                evs = events_of(runtime, "safety.feedback_invalid_while_latched")
                # round 07: aggregated, at most one event per event_interval_s (10 s robot time)
                self.assertLessEqual(len(evs), 4)
                self.assertEqual(sum(e["count"] for e in evs) + pending(runtime, "invalid"), lm["restops"])
                self.assertIn("无法确认本体静止", evs[0]["note"])
                self.assertLess(adapter.x - x0, 0.01)               # resumes 2 s after a stop: never gets there
                self.assertEqual(runtime.supervisor.stop_count, 1)
                self.assertTrue(runtime.supervisor.latched)

    def test_single_bad_sample_is_tolerated(self):
        class OneGlitch(SimQuadruped):
            n = 0

            def read(self, cap_id):
                out = super().read(cap_id)
                if cap_id == "base.odometry":
                    self.n += 1
                    if self.n == 30:
                        out = {"pose": out["pose"], "velocity": None}
                return out
        clock = ManualClock()
        adapter = OneGlitch(clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("operator")
        tick_for(runtime, adapter, clock, 10.0)
        self.assertEqual(runtime.latch_guard.restops, 0)
        self.assertTrue(runtime.latch_guard.describe()["feedback_ok"])

    def _arm(self, cls):
        clock = ManualClock()
        adapter = cls(clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("operator")
        return runtime, adapter, clock

    def test_rotating_joint_names_motion_detected(self):
        class ArmLiar(SimArm):
            k = 0
            stopped = False

            def stop(self):
                super().stop()
                self.stopped = True

            def read(self, cap_id):
                out = super().read(cap_id)
                if cap_id == "joint.state" and self.stopped:
                    self.q[0] += 0.02
                    self.k += 1
                    names, pos, sh = list(out["names"]), list(self.q), self.k % len(self.q)
                    out = {"names": names[sh:] + names[:sh], "position": pos[sh:] + pos[:sh]}
                return out
        runtime, adapter, clock = self._arm(ArmLiar)
        tick_for(runtime, adapter, clock, 30.0)
        lm = runtime.latch_guard.describe()
        self.assertGreater(lm["seen"], 250)                         # round 05: 0
        self.assertGreaterEqual(lm["restops"], 50)
        self.assertEqual(lm["invalid_restops"], 0)                  # rotated names are valid feedback
        self.assertEqual(lm["last_evidence"].get("joint"), "joint1")

    def test_rotating_joint_names_still_arm_no_false_alarm(self):
        class Rotates(SimArm):
            k = 0

            def read(self, cap_id):
                out = super().read(cap_id)
                if cap_id == "joint.state":
                    self.k += 1
                    sh = self.k % len(out["names"])
                    out = {k: (v[sh:] + v[:sh] if isinstance(v, list) else v) for k, v in out.items()}
                return out
        runtime, adapter, clock = self._arm(Rotates)
        tick_for(runtime, adapter, clock, 60.0)
        self.assertEqual(runtime.latch_guard.restops, 0)

    def test_joint_name_set_change_is_invalid(self):
        class Renames(SimArm):
            stopped = False

            def stop(self):
                super().stop()
                self.stopped = True

            def read(self, cap_id):
                out = super().read(cap_id)
                if cap_id == "joint.state" and self.stopped and self._t > 3.0:
                    out["names"] = [n + "_x" for n in out["names"]]
                return out
        runtime, adapter, clock = self._arm(Renames)
        tick_for(runtime, adapter, clock, 10.0)
        lm = runtime.latch_guard.describe()
        self.assertGreater(lm["invalid_restops"], 5)
        self.assertIn("关节名集合", lm["feedback_problems"]["joint.state"])

    def test_hung_read_detected_from_monitor_and_close_bounded(self):
        gate = threading.Event()

        class Hangs(SimQuadruped):
            hang = False
            stops_after_close = None

            def read(self, cap_id):
                if self.hang and cap_id == "base.odometry" and threading.current_thread().name == "nx-latch-guard":
                    gate.wait(20)
                return super().read(cap_id)

            def stop(self):
                if self.stops_after_close is not None:
                    self.stops_after_close += 1
                super().stop()
        adapter = Hangs(time_scale=1.0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None)
        try:
            runtime.emergency_stop("x")
            time.sleep(1.1)                                         # past settle
            adapter.hang = True
            time.sleep(2.6)                                         # stall 1 s, then re-stops every 0.5 s
            lm = runtime.status()["latched_motion"]
            self.assertGreaterEqual(lm["invalid_restops"], 2)
            self.assertFalse(lm["feedback_ok"])
            self.assertGreater(lm["read_in_progress_s"], 1.0)
            self.assertTrue(events_of(runtime, "safety.feedback_invalid_while_latched"))
        finally:
            t = time.monotonic()
            runtime.close()
            closed_in = time.monotonic() - t
            adapter.stops_after_close = 0
        self.assertLess(closed_in, 1.6)
        self.assertEqual(runtime.lingering_threads, ["nx-latch-guard"])
        gate.set()
        time.sleep(0.5)
        self.assertEqual(adapter.stops_after_close, 0)             # no adapter call after close
        self.assertNotIn("nx-latch-guard", [t.name for t in threading.enumerate()])


# ====================================================================== Q5-06 / Q5-07
class SitOnStop(SimArm):
    """stop(): joints move 0.8 rad over sit_s (a re-stop re-plans from the current pose)."""

    def __init__(self, sit_s, **kw):
        super().__init__(**kw)
        self.sit_s = sit_s
        self.plan = None

    def stop(self):
        super().stop()
        start = list(self.q)
        self.plan = (self._t, start, [s + 0.8 for s in start] if self.plan is None else self.plan[2])

    def _step(self, dt):
        super()._step(dt)
        if self.plan is not None:
            t0, a, b = self.plan
            k = min(1.0, (self._t - t0) / self.sit_s)
            self.q = [x + (y - x) * k for x, y in zip(a, b)]
            self.qd = [0.0] * len(self.q)


class SettleAndResetText(unittest.TestCase):
    def test_declared_stop_settle(self):
        for sit_s in (2.0, 3.0):
            with self.subTest(sit_s=sit_s):
                clock = ManualClock()
                adapter = SitOnStop(sit_s, clock=clock)
                adapter.stop_settle_s = sit_s + 0.5
                runtime = make_runtime(self, adapter, start_monitor=False)
                runtime.emergency_stop("operator")
                tick_for(runtime, adapter, clock, 15.0)
                lm = runtime.latch_guard.describe()
                self.assertEqual((lm["restops"], lm["seen"]), (0, 0))
                self.assertEqual(lm["settle_s"], sit_s + 0.5)

    def test_settle_is_bounded(self):
        for declared, expect in ((60.0, 5.0), (0.1, 1.0), (float("nan"), 1.0), (True, 1.0), ("3", 1.0)):
            with self.subTest(declared=declared):
                adapter = SimArm()
                adapter.stop_settle_s = declared
                runtime = make_runtime(self, adapter, start_monitor=False)
                self.assertEqual(runtime.latch_guard.settle_s, expect)

    def test_reset_aborted_by_guard_says_so(self):
        class StandsUpSlowly(SimQuadruped):
            def reset(self):
                t0 = time.monotonic()
                while time.monotonic() - t0 < 2.5:
                    with self._lock:
                        self.x += 0.004
                        self.velocity = (0.1, 0.0, 0.0)
                    time.sleep(0.02)
                self.velocity = (0.0, 0.0, 0.0)
        adapter = StandsUpSlowly(time_scale=1.0)
        runtime = make_runtime(self, adapter, latch_guard=GuardConfig(reset_grace_s=0.8))
        runtime.emergency_stop("operator")
        time.sleep(1.2)
        with self.assertRaises(RuntimeApiError) as ctx:
            runtime.reset({"hardware_authorized": True})
        body = ctx.exception.to_dict()
        self.assertEqual(body["code"], "estop_latched")
        self.assertEqual(body["cause"], "latch_guard_reset_grace")
        self.assertIn("锁存守护", body["message"])
        self.assertNotIn("又收到停止", body["message"])
        self.assertTrue(runtime.supervisor.latched)
        self.assertTrue(events_of(runtime, "safety.reset_raced")[-1]["by_latch_guard"])

    def test_real_stop_during_reset_keeps_old_text(self):
        class SlowReset(SimQuadruped):
            def reset(self):
                time.sleep(0.4)
        adapter = SlowReset()
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("operator")
        threading.Timer(0.1, lambda: runtime.emergency_stop("again")).start()
        with self.assertRaises(RuntimeApiError) as ctx:
            runtime.reset({"hardware_authorized": True})
        self.assertEqual(ctx.exception.to_dict()["cause"], "stop_during_reset")
        self.assertIn("复位期间又收到停止", str(ctx.exception))


# ====================================================================== Q5-03
class ResetInProgressView(unittest.TestCase):
    def test_status_and_409_body(self):
        release = threading.Event()

        class NeverReset(SimQuadruped):
            def reset(self):
                release.wait(20)
        adapter = NeverReset()
        runtime = make_runtime(self, adapter)
        self.addCleanup(release.set)
        st = runtime.status()
        self.assertIsNone(st["reset_in_progress_s"])
        self.assertIsNone(st["reset_hint"])
        server = GatewayServer(runtime, TOKEN, port=0).start()
        self.addCleanup(server.close)
        http(server.url, "/v2/stop", {"reason": "operator"})
        threading.Thread(target=lambda: runtime.reset({"hardware_authorized": True}), daemon=True).start()
        time.sleep(0.3)
        old = SafetySupervisor.RESET_STUCK_HINT_S
        try:
            code, body = http(server.url, "/v2/reset", {"request_id": "r2", "hardware_authorized": True})
            self.assertEqual((code, body["code"]), (409, "reset_in_progress"))
            self.assertIn("尚未返回", body["message"])
            self.assertIn("请稍候", body["hint"])
            self.assertGreater(body["reset_in_progress_s"], 0.0)
            SafetySupervisor.RESET_STUCK_HINT_S = 0.0
            code, body = http(server.url, "/v1/reset", {"hardware_authorized": True})
            self.assertEqual((code, body["code"]), (409, "reset_in_progress"))
            self.assertIn("重启机器人上的运行时服务", body["hint"])
            self.assertIn("锁存会保留", body["message"])
            code, st = http(server.url, "/v1/status")
            self.assertTrue(st["reset_in_progress"])
            self.assertGreater(st["reset_in_progress_s"], 0.0)
            self.assertIn("重启", st["reset_hint"])
            self.assertTrue(st["estop_latched"])
        finally:
            SafetySupervisor.RESET_STUCK_HINT_S = old


# ====================================================================== Q5-05
DECL = [
    {"name": "loops", "type": "int", "default": 1, "min": 1, "max": 3},
    {"name": "speed", "type": "float", "required": False, "min": 0.0, "max": 0.5},
    {"name": "mode", "type": "str", "required": False, "options": [{"value": "fast", "label": "快"}, "slow"]},
    {"name": "level", "type": "enum", "required": False, "choices": ["a", "b"]},
    {"name": "gains", "type": "floats", "required": False, "length": 3, "min": -1.0, "max": 1.0},
    {"name": "waypoints", "type": "object", "required": False, "min_length": 1, "max_length": 4},
    {"name": "label", "type": "str", "required": False, "max_length": 8},
]


class ParameterDeclarations(unittest.TestCase):
    def code_of(self, given, decl=DECL):
        with self.assertRaises(BadRequest) as ctx:
            check_parameters(decl, given)
        return ctx.exception.code, ctx.exception.extra.get("field"), str(ctx.exception)

    def test_valid(self):
        out = check_parameters(DECL, {"loops": 3, "speed": 0.5, "mode": "fast", "level": "b",
                                      "gains": [0.0, 1.0, -1.0], "waypoints": [[0, 0]], "label": "abc"})
        self.assertEqual(out["loops"], 3)
        self.assertEqual(check_parameters(DECL, {}), {"loops": 1})

    def test_violations(self):
        cases = [({"loops": 99}, "param_out_of_range", "loops"), ({"loops": 0}, "param_out_of_range", "loops"),
                 ({"speed": 0.51}, "param_out_of_range", "speed"), ({"speed": -0.1}, "param_out_of_range", "speed"),
                 ({"mode": "warp"}, "param_choice", "mode"), ({"level": "c"}, "param_choice", "level"),
                 ({"gains": [0.0, 2.0, 0.0]}, "param_out_of_range", "gains"),
                 ({"gains": [0.0, 0.0]}, "param_length", "gains"),
                 ({"waypoints": []}, "param_length", "waypoints"),
                 ({"waypoints": [[0, 0]] * 5}, "param_length", "waypoints"),
                 ({"label": "x" * 9}, "param_length", "label")]
        for given, code, field in cases:
            with self.subTest(given=given):
                got_code, got_field, message = self.code_of(given)
                self.assertEqual((got_code, got_field), (code, field))
                self.assertIn(field, message)
                self.assertNotIn("需要", message)             # not mistaken for a type error by the desktop

    def test_unknown_missing_type_codes(self):
        self.assertEqual(self.code_of({"evil": 1})[:2], ("bad_request", "evil"))   # compat (sdk CLI)
        self.assertEqual(self.code_of({"loops": 1.5})[:2], ("param_type", "loops"))
        self.assertIn("技能参数 loops 需要 int", self.code_of({"loops": 1.5})[2])      # desktop text rule
        self.assertEqual(self.code_of({}, [{"name": "w", "type": "object", "required": True}])[:2],
                         ("param_required", "w"))

    def test_desktop_body_fields(self):
        """Contract with the desktop (ux04): param, min, max, choices (null if undeclared), message."""
        with self.assertRaises(BadRequest) as ctx:
            check_parameters(DECL, {"loops": 99})
        body = ctx.exception.to_dict()
        self.assertEqual({k: body[k] for k in ("code", "param", "min", "max", "choices")},
                         {"code": "param_out_of_range", "param": "loops", "min": 1, "max": 3, "choices": None})
        self.assertEqual(body["message"], body["error"])
        with self.assertRaises(BadRequest) as ctx:
            check_parameters(DECL, {"mode": "warp"})
        body = ctx.exception.to_dict()
        self.assertEqual((body["code"], body["param"], body["min"], body["max"], body["choices"]),
                         ("param_choice", "mode", None, None, ["fast", "slow"]))
        with self.assertRaises(BadRequest) as ctx:
            check_parameters(DECL, {"level": "z"})
        self.assertEqual(ctx.exception.to_dict()["choices"], ["a", "b"])

    def test_bad_declarations_fail_closed(self):
        for decl in ({"name": "a", "type": "int", "min": "1"}, {"name": "a", "type": "int", "min": 3, "max": 1},
                     {"name": "a", "type": "int", "max": float("nan")}, {"name": "a", "type": "int", "max": True},
                     {"name": "a", "type": "object", "max_length": -1}, {"name": "a", "type": "str", "options": "x"},
                     {"name": "a", "type": "int", "default": 9, "max": 3}):
            with self.subTest(decl=decl):
                self.assertEqual(self.code_of({}, [decl])[0], "param_declaration_invalid")

    def test_install_refuses_bad_declaration(self):
        fx = RuntimeFixture("quadruped")
        self.addCleanup(fx.close)
        pkg = example("patrol")
        pkg["parameters"] = [dict(p) for p in pkg["parameters"]]
        pkg["parameters"][1].update(min=1, max=3, default=7)
        with self.assertRaises(RuntimeApiError) as ctx:
            fx.install(pkg)
        self.assertEqual(ctx.exception.code, "param_declaration_invalid")


class ParameterEntryPoints(unittest.TestCase):
    def setUp(self):
        class Counting(SimQuadruped):
            calls = 0

            def invoke(self, cap_id, args):
                Counting.calls += 1
                return super().invoke(cap_id, args)

            def start_action(self, cap_id, args):
                Counting.calls += 1
                return super().start_action(cap_id, args)
        self.adapter = Counting()
        self.runtime = make_runtime(self, self.adapter)
        pkg = example("patrol")
        pkg["parameters"] = [dict(p) for p in pkg["parameters"]]
        pkg["parameters"][1].update(min=1, max=3)
        self.runtime.install_skill({"request_id": "i", "package": pkg})
        self.server = GatewayServer(self.runtime, TOKEN, port=0).start()
        self.addCleanup(self.server.close)
        self.params = {"waypoints": [[0.2, 0.0]], "loops": 99}

    def test_direct_run_refused_no_adapter_call(self):
        before = self.adapter.calls
        code, body = http(self.server.url, "/v2/skills/patrol/run",
                          {"request_id": "r1", "parameters": self.params, "hardware_authorized": True})
        self.assertEqual((code, body["code"], body["param"]), (400, "param_out_of_range", "loops"))
        self.assertEqual((body["min"], body["max"], body["choices"]), (1, 3, None))
        self.assertIn("超出允许范围", body["message"])
        self.assertEqual(body["constraint"], {"min": 1, "max": 3})
        self.assertIn("1 – 3", body["error"])
        self.assertEqual(self.adapter.calls, before)
        self.assertEqual(self.runtime.runner.active_runs(), [])
        self.assertEqual(self.runtime.store.get("requests", "r1")["status"], "rejected")

    def test_application_paths(self):
        app = {"schema": "robot.application.v1", "id": "a1", "name": "a", "digest": "d",
               "steps": [{"entrypoint": "patrol", "parameters": self.params, "timeout_s": 30}]}
        before = self.adapter.calls
        code, body = http(self.server.url, "/v1/deploy", {"request_id": "d1", "application": app})
        self.assertEqual((code, body["code"]), (409, "validation_failed"))
        self.assertIn("超出允许范围", body["error"])
        code, body = http(self.server.url, "/v1/evaluate", {"request_id": "e1", "application": app,
                                                            "hardware_authorized": True})
        self.assertEqual((code, body["code"]), (409, "validation_failed"))
        # a release pinned before this check existed is refused at /v1/start, before admission
        ref = self.runtime.skills.resolve("patrol")["id"]
        self.runtime.store.put("releases", {"id": "rel-old", "manifest_digest": self.runtime.manifest_v1_digest(),
                                            "steps": [{"skill_ref": ref, "entrypoint": "patrol",
                                                       "parameters": self.params, "timeout_s": 30}]})
        code, body = http(self.server.url, "/v1/start", {"request_id": "s1", "release_id": "rel-old",
                                                         "hardware_authorized": True})
        self.assertEqual((code, body["code"], body["param"], body["step"]), (400, "param_out_of_range", "loops", 0))
        self.assertTrue(body["message"].startswith("第 1 步："))
        self.assertEqual(self.adapter.calls, before)
        self.assertEqual(self.runtime.runner.active_runs(), [])


if __name__ == "__main__":
    unittest.main()
