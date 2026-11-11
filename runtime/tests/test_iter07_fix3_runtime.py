"""Round-07 third fix pass (docs/evolution/reviews/2026-11-09-iter07-final-review.md).

* Q7c-01 (P1): where the walk / drift allowance is credited, the silent precautionary stops follow
  v0.13.0's re-stop rule and cadence exactly (``V013Replica``): same raw samples, 3 mm / 5 mrad
  from v0.13.0's last re-stop, no persistence, 0.5 s. A driver that obeys each stop() for a while
  (a repeated stop() during the hold does not extend it: the HAL says stop() is idempotent) and
  then resumes 1.9 cm/s under a random walk in its odometry goes no further than under v0.13.0.
  Reference values: the same drivers run on v0.13.0 (``dbab711``) with the same seeds.
* Q7c-02 (P2): the drift notice belongs to one latch: cleared on every successful reset (also when
  the body is re-stopped within one guard period, never sampled unlatched), reported only while
  latched; the new latch gets its own notice.
* Q7c-07: ``safety.reset_expected_s`` in (0, 25] -- see test_iter07_fix2_runtime.ResetCapabilities
  and spec/tests ResetExpected.

Every test here fails on d498d79's runtime except the guard rails marked "(kept)".
"""
import math
import random
import unittest

import _support  # noqa: F401

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.clock import ManualClock

from test_iter05_runtime import events_of, make_runtime
from test_iter06_runtime import tick_for

HOUR = 3600.0


class Resumer(SimQuadruped):
    """Real body x += vx (truthful reported velocity, below the 2 cm/s threshold). Obeys a
    stop() for ``pause`` s and then resumes; ``fixed``: a stop() while already holding does not
    extend the hold (idempotent stop, final review ``FixedPause``). ``walk``: m/sqrt(s) random walk
    added to the *reported* x/y (a legged odometry's wander, or a liar's fake one)."""

    def __init__(self, vx=0.019, pause=0.0, fixed=True, walk=0.0, seed=1, **kw):
        super().__init__(**kw)
        self.cvx, self.pause, self.fixed, self.walk = vx, pause, fixed, walk
        self.hold_until = -1.0
        self.rng = random.Random(seed)
        self.bx = self.by = 0.0
        self.stops = 0

    def stop(self):
        super().stop()
        self.stops += 1
        if self.pause and (not self.fixed or self._t >= self.hold_until):
            self.hold_until = self._t + self.pause

    def _step(self, dt):
        super()._step(dt)
        if self._t < self.hold_until:
            self.velocity = (0.0, 0.0, 0.0)
            return
        self.x += self.cvx * dt
        self.velocity = (self.cvx, 0.0, 0.0)

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "base.odometry" and self.walk:
            g = self.rng.gauss
            self.bx += g(0, self.walk * math.sqrt(0.1))
            self.by += g(0, self.walk * math.sqrt(0.1))
            pose = list(out["pose"])
            pose[0] += self.bx
            pose[1] += self.by
            out = {"pose": pose, "velocity": list(out["velocity"])}
        return out


def latched(test, adapter, clock, seconds, **kw):
    runtime = make_runtime(test, adapter, start_monitor=False, **kw)
    runtime.emergency_stop("operator")
    x0 = adapter.x
    peak = [0.0]

    def on_tick(_i):
        peak[0] = max(peak[0], abs(adapter.x - x0))
    tick_for(runtime, adapter, clock, seconds, on_tick)
    return runtime, peak[0]


# ====================================================================== Q7c-01
class PrecautionAsV013(unittest.TestCase):
    # (pause s, fixed hold, walk m/sqrt(s), seed, seconds) -> real displacement on v0.13.0 (dbab711)
    # and on d498d79, same driver and seed (see the module docstring)
    CASES = (
        ((61.0, True, 0.0015, 1, HOUR), 0.2869, 0.4218),     # faked 1.5 mm/sqrt(s), fixed 61 s
        ((30.0, True, 0.001, 2, HOUR), 0.5115, 0.5537),      # legged odometry 1 mm/sqrt(s), fixed 30 s
        ((10.0, False, 0.0015, 1, HOUR), 0.0631, 0.0790),    # every stop() restarts a 10 s hold
        ((1.5, False, 0.0015, 1, 600.0), 1.0283, 1.3558),    # every stop() restarts a 1.5 s hold, 10 min
    )

    def test_stop_then_resume_no_further_than_v013(self):
        for (pause, fixed, walk, seed, seconds), v013, _head in self.CASES:
            with self.subTest(pause=pause, fixed=fixed, walk=walk, seed=seed):
                clock = ManualClock()
                ad = Resumer(pause=pause, fixed=fixed, walk=walk, seed=seed, clock=clock)
                runtime, peak = latched(self, ad, clock, seconds)
                self.assertLessEqual(peak, v013 + 0.0015)
                lm = runtime.latch_guard.describe()
                self.assertGreater(lm["precautionary_stops"], 0)
                # silent: the precautionary stops are no re-stops, no events
                runtime.latch_guard.flush()       # aggregated counts still pending (Q7-08)
                motion = sum(e.get("count", 1) for e in events_of(runtime, "safety.motion_while_latched"))
                self.assertEqual(motion, lm["restops"])

    def test_cadence_is_v013s_restop_interval(self):
        # a 1.9 cm/s resumption 0.6 s after a stop (hold not extended) is stopped again at the
        # first raw sample 3 mm from v0.13.0's last re-stop, at most every 0.5 s -- d498d79 waited
        # for 2 samples and 1 s between precautionary stops (+40 % distance)
        # (v0.13.0 0.6228 m in 2 min, d498d79 0.8447 m)
        clock = ManualClock()
        ad = Resumer(pause=0.6, fixed=True, walk=0.0015, seed=1, clock=clock)
        runtime, _peak = latched(self, ad, clock, 120.0)
        self.assertEqual(runtime.latch_guard.config.precaution_interval_s, 0.5)
        self.assertLessEqual(ad.x, 0.6228 + 0.0015)

    def test_body_at_rest_never_counted(self):                       # (kept)
        # a legged odometry wandering 1 mm/sqrt(s) on a body at rest: precautionary stops only,
        # no counted re-stop storm, no escalation (v0.13.0: ~700 re-stops per hour)
        clock = ManualClock()
        ad = Resumer(vx=0.0, walk=0.001, seed=3, clock=clock)
        runtime, peak = latched(self, ad, clock, HOUR)
        self.assertEqual(peak, 0.0)
        lm = runtime.latch_guard.describe()
        self.assertLessEqual(lm["restops"], 5)
        self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))

    def test_disabled_with_zero_interval(self):                         # (kept)
        clock = ManualClock()
        ad = Resumer(vx=0.0, walk=0.001, seed=3, clock=clock)
        runtime, _ = latched(self, ad, clock, 300.0, latch_guard=_without_precaution())
        self.assertEqual(runtime.latch_guard.describe()["precautionary_stops"], 0)


def _without_precaution():
    from dataclasses import replace
    from neuxmind_runtime.latch_guard import DEFAULT_CONFIG
    return replace(DEFAULT_CONFIG, precaution_interval_s=0.0)


# ====================================================================== Q7c-02
class SlideAfterLatch(SimQuadruped):
    """Obeys stop; 2 s after every latch it slides 9 cm along x over 1 s (not stoppable), then rests."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.slide_at = None

    def stop(self):
        super().stop()
        if self.slide_at is None:
            self.slide_at = self._t + 2.0

    def reset(self):
        super().reset()
        self.slide_at = None

    def _step(self, dt):
        super()._step(dt)
        if self.slide_at is not None and self.slide_at <= self._t < self.slide_at + 1.0:
            self.x += 0.09 * dt
            self.velocity = (0.0, 0.0, 0.0)   # a slide the reported velocity does not show
        else:
            self.velocity = (0.0, 0.0, 0.0)


class DriftNoticePerLatch(unittest.TestCase):
    def _notices(self, runtime):
        return events_of(runtime, "safety.latched_drift_notice")

    def test_restop_right_after_reset_gets_its_own_notice(self):
        clock = ManualClock()
        ad = SlideAfterLatch(clock=clock)
        runtime = make_runtime(self, ad, start_monitor=False)
        runtime.emergency_stop("operator")
        tick_for(runtime, ad, clock, 8.0)
        first = runtime.latch_guard.describe()["drift_notice_detail"]
        self.assertIsNotNone(first)
        self.assertEqual(len(self._notices(runtime)), 1)
        for gap in (0, 0, 1):            # re-stopped in the same guard period as the reset
            with self.subTest(gap=gap):
                before = len(self._notices(runtime))
                runtime.reset({"hardware_authorized": True})
                tick_for(runtime, ad, clock, 0.1 * gap)
                runtime.emergency_stop("operator")
                # the previous latch's notice is not this latch's (d498d79: still reported)
                lm = runtime.latch_guard.describe()
                self.assertIs(lm["drift_notice"], False)
                self.assertIsNone(lm["drift_notice_detail"])
                tick_for(runtime, ad, clock, 0.1)
                self.assertIs(runtime.latch_guard.describe()["drift_notice"], False)
                tick_for(runtime, ad, clock, 8.0)
                # ... and the new latch, whose slide crosses 5 cm, gets its own (d498d79: none)
                self.assertEqual(len(self._notices(runtime)), before + 1)
                lm = runtime.latch_guard.describe()
                self.assertIs(lm["drift_notice"], True)
                self.assertNotEqual(lm["drift_notice_detail"], first)
                first = lm["drift_notice_detail"]

    def test_not_reported_once_unlatched(self):
        clock = ManualClock()
        ad = SlideAfterLatch(clock=clock)
        runtime = make_runtime(self, ad, start_monitor=False)
        runtime.emergency_stop("operator")
        tick_for(runtime, ad, clock, 8.0)
        self.assertIs(runtime.latch_guard.describe()["drift_notice"], True)
        runtime.reset({"hardware_authorized": True})
        # no guard tick yet (d498d79: estop_latched false with drift_notice true until it ticked)
        status = runtime.status()
        self.assertIs(status["estop_latched"], False)
        self.assertIs(status["latched_motion"]["drift_notice"], False)
        self.assertIsNone(status["latched_motion"]["drift_notice_at"])

    def test_once_per_latch(self):                                       # (kept)
        clock = ManualClock()
        ad = SlideAfterLatch(clock=clock)
        runtime = make_runtime(self, ad, start_monitor=False)
        runtime.emergency_stop("operator")
        tick_for(runtime, ad, clock, 8.0)
        runtime.emergency_stop("operator")      # a second stop while latched: same latch
        tick_for(runtime, ad, clock, 8.0)
        self.assertEqual(len(self._notices(runtime)), 1)


if __name__ == "__main__":
    unittest.main()
