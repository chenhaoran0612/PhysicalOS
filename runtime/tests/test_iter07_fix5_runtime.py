"""Round-07 fifth fix pass (docs/evolution/reviews/2026-11-10-iter07-release-review.md).

* Q7e-01 (P1): ``LatchedMotionGuard._precaution`` took "the last guard ``stop()`` has the same
  robot time as this sample" to mean "the guard thread already stopped in this sample". With a
  coarse adapter clock (>= the monitor's 20 ms period) the monitor thread's read-hang re-stop and
  the first sample after the hang share a time stamp, so the replica's reference moved to the
  position *after* the hang (v0.13.0: before it), its later re-stops came later, and the body
  went 2.6-4.6 % further than under v0.13.0. Now the guard records which thread issued the latest
  ``stop()``; a replica re-stop after a read-hang stop is always merged into it, without evidence,
  with the reference where the body was at that stop.
* Q7e-02 (P3): the events of a merged counted re-stop (no ``stop()`` call of its own) say
  ``adapter_stopped: false`` and ``merged: true``.

Reference values: the same drivers run on v0.13.0 (``dbab711``). The Q7e-01 tests fail on the
runtime of 6d8af00 (review(iter07-release)); "(kept)" marks guard rails that already held.
"""
import time
import unittest
from unittest import mock

import _support  # noqa: F401

from neuxmind_runtime import latch_guard as LG
from neuxmind_runtime.clock import ManualClock

from test_iter05_runtime import make_runtime
from test_iter06_runtime import tick_for
from test_iter07_fix4_runtime import INTERVAL, EPS, Adversary, max_in_window

P = 0.1


class _RobotTime:
    """``latch_guard.time`` with ``monotonic`` on the robot clock (the read-hang detector measures
    the hang with it); everything else is the real ``time`` module."""

    def __init__(self, clock):
        self._clock = clock

    def monotonic(self):
        return self._clock.now()

    def __getattr__(self, name):
        return getattr(time, name)


class Hang(Adversary):
    """Every ``every`` robot s the odometry read blocks for ``dur`` robot s while the body keeps
    going; meanwhile the monitor thread's ``check_stalled_read()`` runs every 0.1 s. ``eps``: the
    read returns that long after the monitor's last check (0: a coarse clock -- the same robot
    time; 0.001: as with a real monotonic clock)."""

    def __init__(self, every=20.0, dur=1.5, eps=0.0, **kw):
        super().__init__(**kw)
        self.every, self.dur, self.eps = every, dur, eps
        self.next_hang = every
        self.guard = None
        self.hangs = []              # (robot time of the monitor's last check, replica's view before the hang,
                                     #  replica "displaced" before the hang)

    def read(self, cap_id):
        if cap_id == "base.odometry" and self.guard is not None and self._t >= self.next_hang - EPS:
            self.next_hang = self._t + self.every
            g = self.guard
            replica = getattr(g, "_v13", None)          # (v0.13.0 has no replica)
            before = dict(replica.last_parsed) if replica is not None else {}
            displaced = replica.displaced if replica is not None else None
            for _ in range(int(round(self.dur / P))):
                self.clock.advance(P)
                self.advance()
                g.check_stalled_read()
            self.hangs.append((self.clock.now(), before, displaced))
            if self.eps:
                self.clock.advance(self.eps)
        return super().read(cap_id)


def run_hang(test, seconds, **kw):
    clock = ManualClock()
    ad = Hang(clock=clock, **kw)
    with mock.patch.object(LG, "time", _RobotTime(clock)):
        runtime = make_runtime(test, ad, start_monitor=False)
        runtime.emergency_stop("operator")
        ad.calls.clear()
        ad.guard = runtime.latch_guard
        x0 = ad.x
        peak = [0.0]
        refs = []                   # the replica's reference right after each post-hang sample
        seen = [0]

        def on_tick(i):
            peak[0] = max(peak[0], abs(ad.x - x0))
            if len(ad.hangs) > seen[0]:
                seen[0] = len(ad.hangs)
                replica = getattr(runtime.latch_guard, "_v13", None)
                if replica is not None:
                    refs.append((replica.last_restop, dict(replica.ref), replica.displaced))
        tick_for(runtime, ad, clock, seconds, on_tick)
    return runtime, ad, peak[0], refs


# ====================================================================== Q7e-01
class ReadHangRestopVsReplica(unittest.TestCase):
    def test_coarse_clock_replica_reference_stays_before_the_hang(self):
        # The monitor's read-hang re-stop and the first sample after the hang share a robot time
        # stamp. v0.13.0's read-hang re-stop: reference = the sample before the hang, no evidence.
        runtime, ad, _, refs = run_hang(self, 125.0, dur=1.5, vx=0.019, walk=0.0015, pause=3.0)
        self.assertGreaterEqual(len(refs), 5)
        stalls = 0
        for (t_check, before, displaced_before), (replica_at, ref, displaced) in zip(ad.hangs, refs):
            if replica_at is None or abs(replica_at - t_check) > EPS:
                continue              # the replica did not re-stop at the hang's last stall stop
            self.assertTrue(any(abs(t - t_check) <= EPS for t in ad.calls))
            stalls += 1
            self.assertEqual(ref.get("base.odometry"), before.get("base.odometry"), f"hang ending {t_check}")
            self.assertEqual(displaced, displaced_before)   # merged without evidence
        self.assertGreater(stalls, 0)
        gaps = [b - a for a, b in zip(ad.calls, ad.calls[1:])]
        self.assertGreaterEqual(min(gaps), INTERVAL - EPS)
        self.assertLessEqual(max_in_window(ad.calls, 1.0), 2)

    def test_held_as_v013_coarse_and_real_clock(self):
        # 10 min, obey each stop for `pause` s then 1.9 cm/s, odometry read blocking `dur` s every
        # 20 s; eps 0: coarse clock (the review's WORSE case: dur 1.5/3/10, pause 3, fake walk
        # 1.5 mm/sqrt(s); 6d8af00 here 1.1894 m / 408 stop() calls, 1.2377 / 514, 2.0657 / 1386 --
        # a different stop() series), eps 1 ms: real clock. Peak real displacement and stop()
        # count are v0.13.0's.
        for key, (v013, calls) in V013_HANG.items():
            dur, pause, walk, eps = key
            with self.subTest(dur=dur, pause=pause, walk=walk, eps=eps):
                _, ad, peak, _ = run_hang(self, 600.0, dur=dur, vx=0.019, walk=walk, pause=pause, eps=eps)
                self.assertAlmostEqual(peak, v013, delta=0.0005)
                self.assertLessEqual(abs(len(ad.calls) - calls), 2)
                self.assertGreaterEqual(min(b - a for a, b in zip(ad.calls, ad.calls[1:])), INTERVAL - EPS)


# (dur, pause, walk, eps) -> (peak |real x| in 600 s, guard stop() calls) on v0.13.0 (dbab711),
# same driver (scratchpad fix5ab/refhang.py)
V013_HANG = {
    (1.5, 3.0, 0.0015, 0.0): (1.1932, 425),
    (3.0, 3.0, 0.0015, 0.0): (1.23576, 539),
    (10.0, 3.0, 0.0015, 0.0): (2.12306, 1424),
    (1.5, 0.6, 0.0015, 0.0): (3.48764, 810),
    (10.0, 0.6, 0.0015, 0.0): (7.40962, 1797),
    (3.0, 0.0, 0.0, 0.0): (13.395, 1271),
    (1.5, 3.0, 0.0, 0.0): (1.1894, 248),
    (1.5, 3.0, 0.0015, 0.001): (1.19662, 423),
    (3.0, 3.0, 0.0015, 0.001): (1.24602, 535),
    (10.0, 3.0, 0.0015, 0.001): (2.20212, 1425),
    (1.5, 0.6, 0.0015, 0.001): (3.54198, 811),
    (10.0, 0.6, 0.0015, 0.001): (7.45864, 1802),
    (3.0, 0.0, 0.0, 0.001): (13.39566, 1271),
    (1.5, 3.0, 0.0, 0.001): (1.19852, 248),
}


# ====================================================================== Q7e-02
class MergedRestopEvents(unittest.TestCase):
    def test_merged_restop_event_says_no_stop_call(self):
        clock = ManualClock()
        ad = Adversary(vx=0.003, walk=0.0015, seed=3, clock=clock)
        runtime = make_runtime(self, ad, start_monitor=False)
        events = []
        orig = runtime.latch_guard._events.emit

        def emit(event_type, **kw):
            events.append((event_type, kw))
            return orig(event_type, **kw)
        runtime.latch_guard._events = mock.Mock(wraps=runtime.latch_guard._events, emit=emit)
        runtime.emergency_stop("operator")
        tick_for(runtime, ad, clock, 600.0)
        lm = runtime.latch_guard.describe()
        self.assertGreater(lm["merged_stops"], 0)
        motion = [kw for t, kw in events if t == "safety.motion_while_latched"]
        self.assertTrue(motion)
        for kw in motion:
            self.assertIn("merged", kw)
            if kw["merged"]:
                self.assertIs(kw["adapter_stopped"], False)
        self.assertTrue(any(kw["merged"] for kw in motion))
        self.assertTrue(any(not kw["merged"] and kw["adapter_stopped"] for kw in motion))
        esc = [kw for t, kw in events if t == "safety.stop_not_obeyed"]
        self.assertEqual(len(esc), 1)
        self.assertIn("merged", esc[0])
        if esc[0]["merged"]:
            self.assertIs(esc[0]["adapter_stopped"], False)


if __name__ == "__main__":
    unittest.main()
