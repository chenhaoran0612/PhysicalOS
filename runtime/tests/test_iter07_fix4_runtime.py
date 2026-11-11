"""Round-07 fourth fix pass (docs/evolution/reviews/2026-11-09-iter07-final-rereview.md).

* Q7d-01 (P1) / Q7d-02 (P2): the guard's counted re-stops and the ``V013Replica`` precautionary
  stops kept their own 0.5 s cadences and interleaved, so ``stop()`` went out 0.1-0.4 s apart (up
  to 4/s); a driver that ignores a ``stop()`` arriving < 0.5 s after the previous one swallowed
  the whole series and kept going (1.5-8.9x v0.13.0's real displacement). Now every ``stop()`` the
  guard issues (counted, precautionary, invalid feedback, stalled read from the monitor thread)
  shares ONE cadence, at least ``restop_interval_s`` apart (<= 2/s); a re-stop that falls inside
  it is merged into the stop() that just went out.

Reference values: the same drivers and seeds run on v0.13.0 (``dbab711``,
``scripts`` in the fix report). Every test here fails on 643549a's runtime except the guard rails
marked "(kept)".
"""
import bisect
import math
import random
import time
import unittest

import _support  # noqa: F401

from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.clock import ManualClock

from test_iter05_runtime import make_runtime
from test_iter06_runtime import tick_for

INTERVAL = 0.5
EPS = 1e-9


class Adversary(SimQuadruped):
    """A configurable liar / misbehaving driver. Real body: creeps ``vx`` m/s and turns ``wz``
    rad/s (reported velocity truthful), obeys a stop() for ``pause`` s (``fixed``: a stop() while
    holding does not extend the hold), and ignores a stop() < ``debounce`` s after the previous
    stop() call (sliding). Reported odometry: + random walk ``walk`` m/sqrt(s), + white noise
    ``noise`` m, + a ``jump`` m step every ``jump_every`` s, + yaw noise ``yaw_noise`` rad,
    NaN pose with probability ``nan_p``, ``velocity: null`` with probability ``vnull_p``.
    Every stop() call is recorded (robot time) in ``calls``."""

    def __init__(self, vx=0.0, wz=0.0, pause=0.0, fixed=True, debounce=0.0, walk=0.0, noise=0.0,
                 jump=0.0, jump_every=0.0, yaw_noise=0.0, nan_p=0.0, vnull_p=0.0, seed=1, **kw):
        super().__init__(**kw)
        self.cvx, self.cwz, self.pause, self.fixed, self.debounce = vx, wz, pause, fixed, debounce
        self.walk, self.noise, self.jump, self.jump_every = walk, noise, jump, jump_every
        self.yaw_noise, self.nan_p, self.vnull_p = yaw_noise, nan_p, vnull_p
        self.rng = random.Random(seed)
        self.hold_until = -1.0
        self.last_call = -1e9
        self.bx = self.by = 0.0
        self.calls = []
        self.honoured = 0

    def stop(self):
        t = self._t
        self.calls.append(t)
        if self.debounce and t - self.last_call < self.debounce - 1e-6:
            self.last_call = t
            return
        self.last_call = t
        super().stop()
        self.honoured += 1
        if self.pause and (not self.fixed or t >= self.hold_until):
            self.hold_until = t + self.pause

    def _step(self, dt):
        super()._step(dt)
        if self._t < self.hold_until or (not self.cvx and not self.cwz):
            self.velocity = (0.0, 0.0, 0.0)
            return
        self.x += self.cvx * dt
        self.yaw = math.remainder(self.yaw + self.cwz * dt, 2 * math.pi)
        self.velocity = (self.cvx, 0.0, self.cwz)

    def read(self, cap_id):
        if cap_id != "base.odometry":
            return super().read(cap_id)
        yaw = self.yaw
        if self.yaw_noise:
            self.yaw = yaw + self.rng.gauss(0, self.yaw_noise)
        try:
            out = super().read(cap_id)
        finally:
            self.yaw = yaw
        g = self.rng.gauss
        if self.walk:
            self.bx += g(0, self.walk * math.sqrt(0.1))
            self.by += g(0, self.walk * math.sqrt(0.1))
        pose = list(out["pose"])
        pose[0] += self.bx + (g(0, self.noise) if self.noise else 0.0)
        pose[1] += self.by + (g(0, self.noise) if self.noise else 0.0)
        if self.jump and self.jump_every and int(self._t / self.jump_every) % 2:
            pose[0] += self.jump
        if self.nan_p and self.rng.random() < self.nan_p:
            pose[0] = float("nan")
        velocity = list(out["velocity"])
        if self.vnull_p and self.rng.random() < self.vnull_p:
            velocity = None
        return {"pose": pose, "velocity": velocity}


def run_latched(test, ad, clock, seconds, between=None, **kw):
    """Latch (operator stop), then tick the guard every 0.1 s robot time. Returns (runtime, guard
    stop() call times, peak |real x displacement|)."""
    runtime = make_runtime(test, ad, start_monitor=False, **kw)
    runtime.emergency_stop("operator")
    ad.calls.clear()                       # the operator's stop is outside the guard's cadence
    x0 = ad.x
    peak = [0.0]

    def on_tick(i):
        peak[0] = max(peak[0], abs(ad.x - x0))
        if between is not None:
            between(runtime, i)
    tick_for(runtime, ad, clock, seconds, on_tick)
    return runtime, list(ad.calls), peak[0]


def max_in_window(ts, w):
    best = 0
    for i, t in enumerate(ts):
        best = max(best, bisect.bisect_left(ts, t + w - EPS) - i)
    return best


def random_adversary(rng, clock, seed):
    kind = rng.choice(("resume", "resume", "jump", "noise", "yaw", "invalid", "mixed"))
    kw = {"seed": seed, "debounce": rng.choice((0.0, 0.0, 0.3, 0.5, 0.6))}
    if kind in ("resume", "mixed"):
        kw.update(vx=rng.choice((0.003, 0.0007, 0.019, 0.05, 0.3)), pause=rng.choice((0.0, 0.6, 1.5, 3.0, 10.0)),
                  fixed=rng.random() < 0.7, walk=rng.choice((0.0, 0.001, 0.0015, 0.003)))
    if kind in ("jump", "mixed"):
        kw.update(jump=rng.choice((0.004, 0.01, 0.06)), jump_every=rng.choice((0.3, 0.7, 2.0)))
    if kind in ("noise", "mixed"):
        kw.update(noise=rng.choice((0.001, 0.002, 0.004)), vx=kw.get("vx", rng.choice((0.0, 0.0007, 0.003))),
                  walk=kw.get("walk", rng.choice((0.0, 0.0015))))
    if kind == "yaw":
        kw.update(wz=rng.choice((0.0, 0.01, 0.1)), yaw_noise=rng.choice((0.001, 0.003, 0.01)),
                  pause=rng.choice((0.0, 0.6, 3.0)))
    if kind in ("invalid", "mixed"):
        kw.update(nan_p=rng.choice((0.0, 0.1, 0.5)), vnull_p=rng.choice((0.0, 0.1, 0.4)),
                  vx=kw.get("vx", 0.01), pause=kw.get("pause", 1.0))
    return kind, kw, Adversary(clock=clock, **kw)


# ====================================================================== Q7d-01/02
class OneStopCadence(unittest.TestCase):
    def assert_cadence(self, ts, what):
        gap = min((b - a for a, b in zip(ts, ts[1:])), default=INTERVAL)
        self.assertGreaterEqual(gap, INTERVAL - EPS, f"{what}: stop() gap {gap:.3f} s")
        self.assertLessEqual(max_in_window(ts, 1.0), 2, what)

    def test_randomized_adversaries_never_two_stops_within_interval(self):
        rng = random.Random(7)
        total = issued = merged = 0
        for n in range(48):
            clock = ManualClock()
            kind, kw, ad = random_adversary(rng, clock, seed=n)
            with self.subTest(n=n, kind=kind, **kw):
                runtime, ts, _ = run_latched(self, ad, clock, 240.0)
                self.assert_cadence(ts, f"{kind} {kw}")
                lm = runtime.latch_guard.describe()
                self.assertEqual(lm["stop_calls"], len(ts))
                total += len(ts)
                issued += lm["restops"] + lm["precautionary_stops"]
                merged += lm["merged_stops"]
        self.assertGreater(total, 1000)      # the property was exercised: many stops, many merges
        self.assertGreater(merged, 50)

    def test_stalled_read_from_monitor_thread_shares_the_cadence(self):
        # the monitor thread's "read blocked" re-stop goes through the same cadence as the
        # guard thread's counted / precautionary stops
        clock = ManualClock()
        ad = Adversary(vx=0.019, pause=0.6, walk=0.0015, debounce=0.5, clock=clock)
        stalls = []

        def between(runtime, i):
            g = runtime.latch_guard
            if i % 3 == 0 and i > 20:
                g._reading = ("base.odometry", time.monotonic() - 5.0)
                stalls.append(g.check_stalled_read())
                g._reading = None
        runtime, ts, _ = run_latched(self, ad, clock, 120.0, between=between)
        self.assertTrue(any(stalls))
        self.assert_cadence(ts, "stall")
        self.assertEqual(runtime.latch_guard.describe()["stop_calls"], len(ts))

    def test_debouncing_driver_held_no_worse_than_v013(self):
        # Q7d-01 review table (1 h there; 10 min here). The driver obeys stop() (fixed hold of
        # `pause` s, then 1.9 cm/s) but ignores a stop() < 0.5 s after the previous call. 643549a
        # interleaved counted and precautionary stops 0.1-0.4 s apart and the debounce swallowed
        # them: real walk / pause 0.6 s 9.92 m, fake walk / pause 3 s 9.17 m and 6.64 m.
        V013 = {  # (pause, walk m/sqrt(s), seed) -> v0.13.0 (dbab711) peak real x displacement in 600 s
            (0.6, 0.001, 1): 2.8458, (10.0, 0.001, 1): 0.2709, (30.0, 0.001, 1): 0.1163,
            (3.0, 0.0015, 1): 0.8307, (3.0, 0.0015, 2): 0.8193, (61.0, 0.0015, 1): 0.0794,
        }
        for (pause, walk, seed), v013 in V013.items():
            with self.subTest(pause=pause, walk=walk, seed=seed):
                clock = ManualClock()
                ad = Adversary(vx=0.019, pause=pause, walk=walk, debounce=0.5, seed=seed, clock=clock)
                runtime, ts, peak = run_latched(self, ad, clock, 600.0)
                self.assertLessEqual(peak, v013 + 0.0015)
                self.assert_cadence(ts, "debounce")
                self.assertEqual(ad.honoured, len(ts) + 1)   # + the operator stop: no guard stop() swallowed

    def test_merged_restop_still_counts_and_escalates(self):
        # a driver that ignores every stop: counted re-stops merged into a precautionary stop()
        # still count, are reported and escalate
        clock = ManualClock()
        ad = Adversary(vx=0.003, walk=0.0015, seed=3, clock=clock)
        runtime, ts, _ = run_latched(self, ad, clock, 600.0)
        lm = runtime.latch_guard.describe()
        self.assertGreater(lm["restops"], 10)
        self.assertTrue(lm["stop_not_obeyed"])
        self.assert_cadence(ts, "ignore")

    def test_operator_stop_outside_the_cadence(self):                          # (kept)
        # /v1/stop goes through at once even right after a guard stop()
        clock = ManualClock()
        ad = Adversary(vx=0.05, clock=clock)
        runtime = make_runtime(self, ad, start_monitor=False)
        runtime.emergency_stop("operator")
        tick_for(runtime, ad, clock, 3.0)
        n = len(ad.calls)
        last = ad.calls[-1]
        self.assertGreater(n, 1)
        clock.advance(0.1)
        runtime.emergency_stop("operator again")
        self.assertEqual(len(ad.calls), n + 1)
        self.assertLess(ad.calls[-1] - last, INTERVAL + 0.11)


if __name__ == "__main__":
    unittest.main()
