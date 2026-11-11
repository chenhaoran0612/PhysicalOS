"""Round-07 second fix pass (docs/evolution/reviews/2026-11-09-iter07-rereview.md).

Release rule: the latched-state guard is at least as safe as v0.13.0 in every scenario, while
keeping the round-07 false-alarm fixes. Every test here fails on aab8a26's runtime except the
guard rails marked "(kept)" (false-alarm fixes / strictness that must not regress).

* Q7b-01: after a yaw re-stop the next bound is capped (``r + min(max(yaw_total_rad, yaw_rad),
  yaw_rad + drift x dt)``); default ``yaw_total_rad`` 0.05 rad: a real slow turn is re-stopped
  every 0.05 rad, an IMU drift a bounded number of times per hour, 0 with the field calibration.
* Q7b-02: "displaced" no longer ends after 60 s quiet -- a driver that obeys each stop for a
  while and then resumes gets no fresh 5 cm per cycle; a stop-and-go creep is not a random walk.
* Q7b-03: displacement since the latch escalates whatever the gaps between re-stops; a faked
  random walk is not believed once the body goes somewhere.
* Q7b-04: a ``float`` parameter accepts a JSON integer in ``choices``/``options`` (1 == 1.0).
* Q7b-05/06: ``/v1/status.capabilities.reset_expire`` and the manifest's ``safety.reset_expected_s``.
"""
import math
import random
import unittest

import _support  # noqa: F401

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped, yaw_to_quat
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.errors import BadRequest
from neuxmind_runtime.skills import check_parameters

from test_iter05_runtime import events_of, make_runtime
from test_iter06_runtime import tick_for

HOUR = 3600.0


class Driver(SimQuadruped):
    """Real body x += vx, yaw += wz (truthful velocity), ignoring stop() -- or, with ``pause``,
    obeying each stop() for ``pause`` s and then resuming (a driver that re-sends its last
    command). ``fake_walk``: m/sqrt(s) random walk added to the *reported* x/y only (a liar)."""

    def __init__(self, vx=0.0, wz=0.0, pause=0.0, fake_walk=0.0, seed=1, **kw):
        super().__init__(**kw)
        self.cvx, self.cwz, self.pause, self.fw = vx, wz, pause, fake_walk
        self.hold_until = -1.0
        self.rng = random.Random(seed)
        self.bx = self.by = 0.0

    def stop(self):
        super().stop()
        if self.pause:
            self.hold_until = self._t + self.pause

    def _step(self, dt):
        super()._step(dt)
        if self._t < self.hold_until:
            self.velocity = (0.0, 0.0, 0.0)
            return
        vx = self.cvx
        self.x += vx * dt
        self.yaw += self.cwz * dt
        self.velocity = (vx, 0.0, self.cwz)

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "base.odometry" and self.fw:
            g = self.rng.gauss
            self.bx += g(0, self.fw * math.sqrt(0.1))
            self.by += g(0, self.fw * math.sqrt(0.1))
            pose = list(out["pose"])
            pose[0] += self.bx
            pose[1] += self.by
            out = {"pose": pose, "velocity": list(out["velocity"])}
        return out


class DriftingImu(SimQuadruped):
    """Body at rest; the reported yaw drifts ``rate`` rad/s."""

    def __init__(self, rate, **kw):
        super().__init__(**kw)
        self.rate = rate

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "base.odometry":
            out = {"pose": [self.x, self.y, 0.0] + yaw_to_quat(self.yaw + self.rate * self.clock.now()),
                   "velocity": [0.0, 0.0, 0.0]}
        return out


def run(test, adapter, clock, seconds, **kw):
    """Latched for ``seconds``; returns (runtime, restop times)."""
    runtime = make_runtime(test, adapter, start_monitor=False, **kw)
    runtime.emergency_stop("operator")
    times = []

    def on_tick(_i):
        if runtime.latch_guard.restops != len(times):
            times.append(clock.now())
    tick_for(runtime, adapter, clock, seconds, on_tick)
    return runtime, times


# ====================================================================== Q7b-01
class YawRatchetCapped(unittest.TestCase):
    def test_slow_turn_keeps_being_caught(self):
        # aab8a26: 1 re-stop in 1 h (the bound grew with yaw_drift_rad_s x t after the first)
        for wz, at_least, step in ((0.00029, 15, 0.06), (0.0001, 5, 0.06)):
            with self.subTest(wz=wz):
                clock = ManualClock()
                ad = Driver(wz=wz, clock=clock)
                runtime, times = run(self, ad, clock, HOUR)
                self.assertGreaterEqual(len(times), at_least)
                gaps = [b - a for a, b in zip(times, times[1:])]
                self.assertLess(max(gaps) * wz, step)            # never more than ~0.05 rad unnoticed
                self.assertLess(times[0] * wz, step)             # first one at 0.05 rad (as v0.13.0)

    def test_imu_drift_bounded_and_calibrated_to_zero(self):        # (kept)
        rate = math.radians(0.5) / 60.0                          # 0.5 deg/min
        clock = ManualClock()
        runtime, times = run(self, DriftingImu(rate, clock=clock), clock, HOUR)
        self.assertLessEqual(len(times), 11)                     # every ~345 s
        self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))
        for calibration in ({"yaw_total_rad": 1.0}, {"yaw_total_rad": 1.0, "yaw_drift_rad_s": 0.0002}):
            with self.subTest(calibration=calibration):
                clock = ManualClock()
                runtime, times = run(self, DriftingImu(rate, clock=clock), clock, HOUR,
                                     latch_guard_overrides=calibration)
                self.assertEqual(times, [])
                self.assertIn("yaw_total_rad", [k["key"] for k in runtime.latch_guard.describe()["relaxed_keys"]])

    def test_calibrated_step_is_capped_too(self):
        # yaw_total_rad 0.3 declared: a turn below the tolerated drift is re-stopped every 0.3 rad,
        # not once (aab8a26: the step after the first grew without bound)
        clock = ManualClock()
        ad = Driver(wz=0.00029, clock=clock)
        runtime, times = run(self, ad, clock, 2 * HOUR, latch_guard_overrides={"yaw_total_rad": 0.3})
        self.assertGreaterEqual(len(times), 5)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertLess(max(gaps) * 0.00029, 0.32)


# ====================================================================== Q7b-02
class NoFreshAllowancePerCycle(unittest.TestCase):
    def test_resume_after_pause_is_as_tight_as_v013(self):
        # v0.13.0: 0.237 / 0.113 m in 1 h; aab8a26: 2.70 / 0.97 m
        for pause, limit in ((65.0, 0.237), (185.0, 0.113)):
            with self.subTest(pause=pause):
                clock = ManualClock()
                ad = Driver(vx=0.019, pause=pause, clock=clock)
                runtime, times = run(self, ad, clock, HOUR)
                self.assertLessEqual(ad.x, limit)
                self.assertTrue(runtime.latch_guard.describe()["displaced"])

    def test_stop_and_go_creep_is_not_a_walk(self):
        # 0.8 mm/s after each 185 s pause: v0.13.0 0.102 m in 1 h; aab8a26 0.81 m (the stop/go
        # pattern was measured as a 0.8 mm/sqrt(s) random walk)
        clock = ManualClock()
        ad = Driver(vx=0.0008, pause=185.0, clock=clock)
        runtime, times = run(self, ad, clock, HOUR)
        self.assertLessEqual(ad.x, 0.102)
        self.assertEqual(runtime.latch_guard.describe()["walk_used"]["m_per_sqrt_s"], 0.0)


# ====================================================================== Q7b-03
class EscalationNotDodged(unittest.TestCase):
    def test_displacement_since_latch_counts_across_long_gaps(self):
        # every 200 s (> escalate_gap_s) the driver lurches 0.2 m in 0.4 s whatever stop() says:
        # one re-stop per lurch, so no streak ever forms -- aab8a26 never escalates
        clock = ManualClock()

        class Lurch(SimQuadruped):
            def _step(self, dt):
                super()._step(dt)
                v = 0.5 if (self._t % 200.0) >= 199.6 else 0.0
                self.x += v * dt
                self.velocity = (v, 0.0, 0.0)

        ad = Lurch(clock=clock)
        runtime, times = run(self, ad, clock, 1000.0)
        self.assertLessEqual(len(times), 10)
        detail = runtime.latch_guard.describe()["stop_not_obeyed_detail"]
        self.assertIsNotNone(detail)
        self.assertEqual(detail["trigger"], "displacement")
        self.assertEqual(len(events_of(runtime, "safety.stop_not_obeyed")), 1)

    def test_fake_walk_does_not_buy_allowance(self):
        # real 0.7 mm/s creep after each 61 s pause + a reported fake 1.5 mm/sqrt(s) walk:
        # aab8a26 1.36 m in 1 h
        clock = ManualClock()
        ad = Driver(vx=0.0007, pause=61.0, fake_walk=0.0015, clock=clock)
        runtime, times = run(self, ad, clock, HOUR)
        self.assertLessEqual(ad.x, 0.05)                                  # v0.13.0: 0.049 m
        self.assertGreater(runtime.latch_guard.describe()["precautionary_stops"], 0)
        # never obeying + fake walk: displacement since the latch escalates (aab8a26: 387 s)
        clock = ManualClock()
        ad = Driver(vx=0.0007, fake_walk=0.0015, clock=clock)
        runtime, times = run(self, ad, clock, 600.0)
        self.assertTrue(events_of(runtime, "safety.stop_not_obeyed"))

    def test_pushed_then_wandering_still_not_escalated(self):        # (kept)
        # the round-07 false-alarm fix stays: pushed 0.3 m, then the odometry wanders 1 mm/sqrt(s)
        from test_iter07_fix_runtime import PushedThenWanders
        for seed in (2, 5):
            with self.subTest(seed=seed):
                clock = ManualClock()
                ad = PushedThenWanders(0.3, walk=0.001, seed=seed, clock=clock)
                runtime, times = run(self, ad, clock, HOUR)
                self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))
                self.assertLessEqual(len(times), 15)          # v0.13.0: ~700


# ====================================================================== drift notice (commander)
class LatchedDriftNotice(unittest.TestCase):
    def test_hidden_by_walk_still_notified_once(self):
        # obeys each stop for 61 s and resumes 0.7 mm/s under a faked 1.5 mm/sqrt(s) walk: only
        # precautionary stops (no motion event) -- but one notice when it passes 5 cm
        clock = ManualClock()
        ad = Driver(vx=0.0007, pause=61.0, fake_walk=0.0015, clock=clock)
        runtime, times = run(self, ad, clock, HOUR)
        notices = events_of(runtime, "safety.latched_drift_notice")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["channel"], "base_position")
        self.assertGreater(notices[0]["displacement"], 0.05)
        self.assertIn("急停期间机器人离停下位置已超过", notices[0]["message"])
        self.assertIn("ts", notices[0])
        lm = runtime.latch_guard.describe()
        self.assertIs(lm["drift_notice"], True)
        self.assertIsNotNone(lm["drift_notice_at"])
        self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))

    def test_once_per_latch_and_again_after_a_new_latch(self):
        # a walking odometry: exactly one notice per latch, never escalated
        from test_iter07_fix_runtime import PushedThenWanders
        clock = ManualClock()
        ad = PushedThenWanders(0.3, walk=0.001, seed=2, clock=clock)
        runtime, times = run(self, ad, clock, HOUR)
        self.assertEqual(len(events_of(runtime, "safety.latched_drift_notice")), 1)
        self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))
        runtime.reset({"hardware_authorized": True})
        tick_for(runtime, ad, clock, 1.0)
        self.assertIs(runtime.latch_guard.describe()["drift_notice"], False)

    def test_turn_channel(self):
        clock = ManualClock()
        runtime, times = run(self, Driver(wz=0.0005, clock=clock), clock, 300.0)
        notices = events_of(runtime, "safety.latched_drift_notice")
        self.assertEqual([n["channel"] for n in notices], ["base_yaw"])
        self.assertIn("rad", notices[0]["message"])

    def test_still_body_never_notified(self):
        clock = ManualClock()
        runtime, times = run(self, SimQuadruped(clock=clock), clock, 600.0)
        self.assertFalse(events_of(runtime, "safety.latched_drift_notice"))
        self.assertIs(runtime.latch_guard.describe()["drift_notice"], False)


# ====================================================================== Q7b-04
def code_of(given, decl):
    try:
        check_parameters(decl, given)
    except BadRequest as exc:
        body = exc.to_dict()
        return body.get("code"), body.get("param")
    return None


class FloatChoices(unittest.TestCase):
    def test_integer_json_is_a_float(self):
        declared = [{"name": "speed", "type": "float", "options": [{"value": 0.5}, {"value": 1.0}]}]
        out = check_parameters(declared, {"speed": 1})
        self.assertEqual(out, {"speed": 1.0})
        self.assertIsInstance(out["speed"], float)
        browser = [{"name": "speed", "type": "float", "options": [{"value": 0.5}, {"value": 1}]}]  # JSON.parse
        self.assertEqual(check_parameters(browser, {"speed": 1.0}), {"speed": 1.0})
        self.assertIsNone(code_of({"s": 2}, [{"name": "s", "type": "float", "choices": [1.0, 2.0], "default": 1}]))
        # desktop 1bc90f2 always sends floats, also for a hand-registered skill declaring [1, 2]
        out = check_parameters([{"name": "s", "type": "float", "choices": [1, 2]}], {"s": 1.0})
        self.assertEqual(out, {"s": 1.0})

    def test_strict_for_bool_and_str_and_other_types(self):          # (kept)
        declared = [{"name": "speed", "type": "float", "choices": [0.5, 1.0]}]
        for bad in (True, "1", "1.0", 2):
            with self.subTest(bad=bad):
                self.assertIn(code_of({"speed": bad}, declared)[0], ("param_choice", "param_type"))
        self.assertEqual(code_of({"n": 1.0}, [{"name": "n", "type": "int", "choices": [1, 2]}])[0], "param_type")
        self.assertEqual(code_of({"m": 1.0}, [{"name": "m", "type": "enum", "choices": [1, 2]}]),
                         ("param_choice", "m"))

    def test_float_range_with_integer(self):
        declared = [{"name": "speed", "type": "float", "min": 0, "max": 1}]
        self.assertIsInstance(check_parameters(declared, {"speed": 1})["speed"], float)   # the skill gets 1.0
        self.assertEqual(code_of({"speed": 2}, declared), ("param_out_of_range", "speed"))


# ====================================================================== Q7b-05/06
class ResetCapabilities(unittest.TestCase):
    def _runtime(self, safety=None):
        class Body(SimQuadruped):
            def manifest(self):
                m = super().manifest()
                if safety is not None:
                    m = dict(m)
                    m["safety"] = {**m.get("safety", {}), **safety}
                return m
        return make_runtime(self, Body(), start_monitor=False)

    def test_status_says_late_resets_are_void(self):
        status = self._runtime().status()
        caps = status["capabilities"]
        self.assertIs(caps["reset_expire"], True)
        self.assertEqual(caps["reset_expire_max_s"], 30.0)
        self.assertEqual(caps["reset_expire_min_s"], 1.0)
        self.assertIsNone(status["reset_expected_s"])

    def test_manifest_reset_expected_s(self):
        for declared, expected in ((10, 10.0), (2.5, 2.5), (25, 25.0)):
            with self.subTest(declared=declared):
                status = self._runtime({"reset_expected_s": declared}).status()
                self.assertEqual(status["reset_expected_s"], expected)
                self.assertEqual(status["capabilities"]["reset_expected_s"], expected)
                self.assertIsNone(status["capabilities"]["reset_expected_note"])
        # validated by the spec manifest validation (fail closed): the runtime does not start
        for bad in (45, 30, 25.5, "10", True, -1, 0, float("nan")):     # Q7c-07: (0, 25]
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._runtime({"reset_expected_s": bad})


if __name__ == "__main__":
    unittest.main()
