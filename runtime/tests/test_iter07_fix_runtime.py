"""Round-07 fix pass (docs/evolution/reviews/2026-11-09-iter07-review.md).

* Q7-01: a real slow yaw rotation while latched is caught (hard bound from the latch snapshot that
  is never re-based; drift allowance not counted twice); IMU drift is tolerated up to the declared
  ``yaw_total_rad``.
* Q7-02: a body that settles away from the snapshot (controlled sit-down without
  ``stop_settle_s``, pushed by a person, odometry random walk) is not re-stopped every 60 s and is
  never escalated to ``safety.stop_not_obeyed``; escalation needs continued motion across re-stops.
* Q7-05: a calibration looser than the default emits ``safety.latch_guard_relaxed`` at startup and
  shows ``relaxed`` in ``/v1/status``; ``escalate_after`` is capped at 30.
* Q7-06: a large displacement escalates without waiting for the 30 s span.
* Q7-07: numeric strings / bools inside ranged ``object`` parameters are refused; ``pose`` ranges
  apply to the position only.
* Q7-08: aggregated event counts are flushed on close.
* Q7-10: a reset whose ``adapter.reset()`` returns after it expired keeps the latch.
"""
import math
import random
import time
import unittest

import _support  # noqa: F401

from neuxmind_spec import manifest as spec_manifest
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped, yaw_to_quat
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.errors import BadRequest, EStopLatched
from neuxmind_runtime.skills import check_parameters

from test_iter05_runtime import events_of, make_runtime
from test_iter06_runtime import SitOnStop, tick_for


class Turner(SimQuadruped):
    """Ignores stop(): keeps turning ``wz`` rad/s and/or creeping ``vx`` m/s (truthful feedback)."""

    def __init__(self, wz=0.0, vx=0.0, **kw):
        super().__init__(**kw)
        self.cwz, self.cvx = wz, vx

    def _step(self, dt):
        super()._step(dt)
        self.yaw += self.cwz * dt
        self.x += self.cvx * dt
        self.velocity = (self.cvx, 0.0, self.cwz)


class DriftingImu(SimQuadruped):
    """Body at rest; the reported yaw drifts ``rate`` rad/s (IMU), pose noise ``sigma``."""

    def __init__(self, rate, sigma=0.0, **kw):
        super().__init__(**kw)
        self.rate, self.sigma = rate, sigma
        self.rng = random.Random(7)

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "base.odometry":
            g = self.rng.gauss
            yaw = self.yaw + self.rate * self.clock.now() + g(0, self.sigma / 2)
            out = {"pose": [self.x + g(0, self.sigma), self.y + g(0, self.sigma), 0.0] + yaw_to_quat(yaw),
                   "velocity": [0.0, 0.0, 0.0]}
        return out


class PushedThenWanders(SimQuadruped):
    """Obeys stop; pushed ``dist`` m at t=20 s over 3 s, then rests -- while the legged odometry
    random-walks ``walk`` m/sqrt(s) (reported only)."""

    def __init__(self, dist, walk=0.0, seed=2, **kw):
        super().__init__(**kw)
        self.dist, self.walk = dist, walk
        self.rng = random.Random(seed)
        self.bx = self.by = 0.0

    def _step(self, dt):
        super()._step(dt)
        if 20.0 <= self._t < 23.0:
            self.x += self.dist / 3.0 * dt
            self.velocity = (self.dist / 3.0, 0.0, 0.0)
        else:
            self.velocity = (0.0, 0.0, 0.0)

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "base.odometry" and self.walk:
            g = self.rng.gauss
            self.bx += g(0, self.walk * math.sqrt(0.1))
            self.by += g(0, self.walk * math.sqrt(0.1))
            out = {"pose": [out["pose"][0] + self.bx, out["pose"][1] + self.by] + list(out["pose"][2:]),
                   "velocity": list(out["velocity"])}
        return out


def latched_run(test, adapter, clock, seconds, **kw):
    runtime = make_runtime(test, adapter, start_monitor=False, **kw)
    runtime.emergency_stop("operator")
    first = {}

    def on_tick(_i):
        if "t" not in first and runtime.latch_guard.restops:
            first["t"] = clock.now()
            first["yaw"] = abs(adapter.yaw)
            first["x"] = abs(adapter.x)
    tick_for(runtime, adapter, clock, seconds, on_tick)
    return runtime, first


# ====================================================================== Q7-01
class SlowYaw(unittest.TestCase):
    def test_slow_turn_caught_early(self):
        for wz, limit in ((0.0011, 0.1), (0.0008, 0.1), (0.0004, 0.21)):
            with self.subTest(wz=wz):
                clock = ManualClock()
                ad = Turner(wz=wz, clock=clock)
                runtime, first = latched_run(self, ad, clock, 900.0)
                self.assertIn("t", first, "a real slow turn must be re-stopped")
                self.assertLess(first["yaw"], limit)      # v0.14-pre: never (0 re-stops in 1 h)

    def test_yaw_hard_bound_never_rebased(self):
        # slower than the IMU drift allowance: only the hard bound catches it, at yaw_total_rad
        clock = ManualClock()
        ad = Turner(wz=0.0002, clock=clock)
        runtime, first = latched_run(self, ad, clock, 1500.0)
        self.assertIn("t", first)
        self.assertLess(first["yaw"], 0.21)
        self.assertIn("base_drift_yaw_total_rad", runtime.latch_guard.describe()["last_evidence"])

    def test_imu_drift_declared_bound(self):
        rate = math.radians(0.5) / 60.0
        # default yaw_total_rad 0.05 (Q7b-01, as v0.13.0): one re-stop per 0.05 rad of drift
        # (0.5 deg/min: every ~345 s, 10 in 1 h) -- bounded, never escalated
        clock = ManualClock()
        runtime, first = latched_run(self, DriftingImu(rate, 0.001, clock=clock), clock, 3600.0)
        lm = runtime.latch_guard.describe()
        self.assertLessEqual(lm["restops"], 11)
        self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))
        # declared for this IMU: 0 re-stops in 1 h
        clock = ManualClock()
        runtime, first = latched_run(self, DriftingImu(rate, 0.001, clock=clock), clock, 3600.0,
                                     latch_guard_overrides={"yaw_total_rad": 0.6})
        self.assertEqual(runtime.latch_guard.describe()["restops"], 0)


# ====================================================================== Q7-02
class SettledElsewhere(unittest.TestCase):
    def test_sit_down_without_settle_goes_quiet(self):
        clock = ManualClock()
        adapter = SitOnStop(3.0, clock=clock)              # re-plans on every stop()
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("operator")
        tick_for(runtime, adapter, clock, 60.0)
        early = runtime.latch_guard.restops
        tick_for(runtime, adapter, clock, 1800.0)
        self.assertEqual(runtime.latch_guard.restops, early)   # v0.14-pre: one more every 60 s
        self.assertLessEqual(early, 25)
        self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))

    def test_pushed_then_still_with_odometry_walk(self):
        clock = ManualClock()
        ad = PushedThenWanders(0.3, walk=0.001, clock=clock)
        runtime, _ = latched_run(self, ad, clock, 3600.0)
        lm = runtime.latch_guard.describe()
        self.assertLessEqual(lm["restops"], 15)              # v0.14-pre: 66 / h
        self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))

    def test_continued_creep_still_escalates(self):
        clock = ManualClock()
        ad = Turner(vx=0.001, clock=clock)                 # 1 mm/s, never obeys
        runtime, _ = latched_run(self, ad, clock, 600.0)
        nob = events_of(runtime, "safety.stop_not_obeyed")
        self.assertEqual(len(nob), 1)
        self.assertGreaterEqual(nob[0]["streak_restops"], 10)


# ====================================================================== Q7-06
class FastEscalation(unittest.TestCase):
    def test_large_displacement_escalates_without_30s(self):
        clock = ManualClock()
        ad = Turner(vx=0.05, clock=clock)
        runtime, _ = latched_run(self, ad, clock, 20.0)
        nob = events_of(runtime, "safety.stop_not_obeyed")
        self.assertEqual(len(nob), 1)                        # v0.14-pre: 31 s, 1.57 m
        self.assertEqual(nob[0]["trigger"], "displacement")
        self.assertLess(nob[0]["since_s"], 15.0)
        self.assertLess(nob[0]["displacement_m"], 0.7)


# ====================================================================== Q7-05
class Relaxed(SimQuadruped):
    def manifest(self):
        m = super().manifest()
        m["safety"] = {**m["safety"], "latch_guard": {"pos_m": 0.2, "escalate_after": 100}}
        return m


class RelaxedCalibration(unittest.TestCase):
    def test_relaxed_is_loud_and_capped(self):
        runtime = make_runtime(self, Relaxed(clock=ManualClock()), start_monitor=False)
        ev = events_of(runtime, "safety.latch_guard_relaxed")
        self.assertEqual(len(ev), 1)
        self.assertEqual([k["key"] for k in ev[0]["keys"]], ["escalate_after", "pos_m"])
        self.assertIn("硬件急停", ev[0]["message"])
        self.assertIsInstance(ev[0]["ts"], float)
        lm = runtime.status()["latched_motion"]
        self.assertIs(lm["relaxed"], True)
        pos = next(k for k in lm["relaxed_keys"] if k["key"] == "pos_m")
        self.assertEqual(pos, {"key": "pos_m", "value": 0.2, "default": 0.05, "source": "manifest"})
        self.assertEqual(lm["config"]["escalate_after"], 30)
        self.assertEqual(spec_manifest.normalize_latch_guard({"escalate_after": 100})[0]["escalate_after"], 30)

    def test_default_not_relaxed(self):
        runtime = make_runtime(self, SimQuadruped(clock=ManualClock()), start_monitor=False)
        self.assertFalse(events_of(runtime, "safety.latch_guard_relaxed"))
        self.assertIs(runtime.status()["latched_motion"]["relaxed"], False)


# ====================================================================== Q7-08
class FlushOnClose(unittest.TestCase):
    def test_counts_flushed(self):
        clock = ManualClock()
        ad = Turner(vx=0.003, clock=clock)
        runtime, _ = latched_run(self, ad, clock, 95.0)
        restops = runtime.latch_guard.restops
        runtime.close()
        ev = events_of(runtime, "safety.motion_while_latched")
        self.assertEqual(sum(e["count"] for e in ev), restops)


# ====================================================================== Q7-10
class SlowReset(SimQuadruped):
    reset_s = 0.0

    def reset(self):
        time.sleep(self.reset_s)
        return super().reset()


class ResetExpiry(unittest.TestCase):
    def test_late_reset_does_not_unlatch(self):
        ad = SlowReset(clock=ManualClock())
        runtime = make_runtime(self, ad, start_monitor=False)
        runtime.supervisor.reset_expire_s = 0.2
        runtime.emergency_stop("operator")
        ad.reset_s = 0.4
        with self.assertRaises(EStopLatched) as ctx:
            runtime.reset({"hardware_authorized": True})
        self.assertEqual(ctx.exception.to_dict().get("cause"), "reset_expired")
        self.assertTrue(runtime.supervisor.latched)
        self.assertEqual(len(events_of(runtime, "safety.reset_expired")), 1)
        ad.reset_s = 0.0                                   # a timely reset still works
        self.assertFalse(runtime.reset({"hardware_authorized": True})["latched"])

    def test_caller_window_expires_reset(self):
        ad = SlowReset(clock=ManualClock())
        runtime = make_runtime(self, ad, start_monitor=False)
        runtime.emergency_stop("operator")
        ad.reset_s = 1.3
        with self.assertRaises(EStopLatched) as ctx:      # the desktop waits 8 s; here 1 s (minimum)
            runtime.reset({"hardware_authorized": True, "expire_s": 1})
        self.assertEqual(ctx.exception.to_dict().get("expire_s"), 1.0)
        self.assertTrue(runtime.supervisor.latched)
        with self.assertRaises(BadRequest):
            runtime.reset({"hardware_authorized": True, "expire_s": "soon"})
        ad.reset_s = 0.0
        self.assertFalse(runtime.reset({"hardware_authorized": True, "expire_s": 8})["latched"])


# ====================================================================== Q7-07
def code_of(given, decl):
    try:
        check_parameters(decl, given)
    except BadRequest as exc:
        body = exc.to_dict()
        return body.get("code"), body.get("param")
    return None


class ParamGaps(unittest.TestCase):
    def test_numeric_strings_and_bools_refused_in_ranged_object(self):
        wp = [{"name": "waypoints", "type": "object", "min": -5, "max": 5}]
        for bad in ([["100", 0]], [["1e3", "-1e3"]], [[True, 0]], [[None, 0]], {"a": "3"}):
            with self.subTest(bad=bad):
                self.assertEqual(code_of({"waypoints": bad}, wp), ("param_type", "waypoints"))
        self.assertIsNone(code_of({"waypoints": [[1, 2.5], [-5, 5]]}, wp))
        free = [{"name": "o", "type": "object"}]             # no range declared: anything JSON
        self.assertIsNone(code_of({"o": [["label", True]]}, free))

    def test_pose_range_is_position_only(self):
        pose = [{"name": "p", "type": "pose", "min": 0, "max": 10}]
        qz, qw = math.sin(-math.pi / 4), math.cos(-math.pi / 4)   # yaw -90 deg: qz = -0.707
        self.assertIsNone(code_of({"p": [1, 2, 0, 0, 0, qz, qw]}, pose))
        self.assertEqual(code_of({"p": [11, 2, 0, 0, 0, 0, 1]}, pose), ("param_out_of_range", "p"))
        self.assertEqual(code_of({"p": [1, -2, 0, 0, 0, 0, 1]}, pose), ("param_out_of_range", "p"))


if __name__ == "__main__":
    unittest.main()
