"""Issue #38 (spec 1.2): ``base.navigate_to_place`` and ``base.explore`` through the capability bus.

On the simulated quadruped with a ManualClock. What this shows is the contract and the supervisor's
part in it (authorization, known places, the speed bound that travels with every request, the body's
duration and radius ceilings, stop). It says nothing about how a real body walks.
"""
import math
import unittest

import _support  # noqa: F401

from neuxmind_runtime.adapters import sim_quadruped
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.bus import CapabilityBus
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.errors import BadRequest, EStopLatched, NotAuthorized, SafetyRejected
from neuxmind_runtime.runner import Runner


def events_of(bus, type_):
    return [e for e in bus.events.since(0) if e["type"] == type_]


class _Base(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.robot = SimQuadruped(clock=self.clock)
        self.bus = CapabilityBus(self.robot)

    def run_to_end(self, handle, seconds=600.0, step=0.1):
        status = handle.poll()
        elapsed = 0.0
        while not status.done and elapsed < seconds:
            self.clock.advance(step)
            elapsed += step
            status = handle.poll()
        return status


class GoToPlace(_Base):
    def test_manifest_lists_the_places(self):
        declared = {c["id"]: c.get("params", {}) for c in self.robot.manifest()["capabilities"]}
        self.assertEqual(declared["base.navigate_to_place"]["places"], list(sim_quadruped.PLACES))
        self.assertIn("会议室", declared["base.navigate_to_place"]["places"])
        self.assertIn("二楼", declared["base.navigate_to_place"]["places"])

    def test_reaches_the_named_place(self):
        handle = self.bus.start_action("base.navigate_to_place", {"place": "会议室"}, authorized=True)
        status = self.run_to_end(handle)
        self.assertEqual(status.state, "succeeded")
        self.assertEqual(status.result["reached"], True)
        self.assertEqual(status.result["place"], "会议室")
        x, y = sim_quadruped.PLACES["会议室"]
        self.assertLess(math.hypot(self.robot.x - x, self.robot.y - y), 0.11)

    def test_needs_authorization(self):
        with self.assertRaises(NotAuthorized):
            self.bus.start_action("base.navigate_to_place", {"place": "会议室"}, authorized=False)
        self.assertIsNone(self.robot.goal)

    def test_unknown_place_is_refused_before_the_body(self):
        with self.assertRaises(SafetyRejected):
            self.bus.start_action("base.navigate_to_place", {"place": "天台"}, authorized=True)
        self.assertIsNone(self.robot.goal)
        self.assertTrue(events_of(self.bus, "safety.rejected"))

    def test_a_coordinate_is_not_a_place(self):
        with self.assertRaises(BadRequest):
            self.bus.start_action("base.navigate_to_place", {"place": "会议室", "goal": [1, 1, 0, 0, 0, 0, 1]},
                                  authorized=True)

    def test_speed_bound_always_reaches_the_body(self):
        self.bus.start_action("base.navigate_to_place", {"place": "会议室"}, authorized=True)
        self.assertEqual(self.robot.goal.extra["max_speed"], sim_quadruped.NAV_SPEED)   # min(body 1.0, sim 0.6)
        self.robot.stop()
        self.bus.start_action("base.navigate_to_place", {"place": "会议室", "max_speed": 0.2}, authorized=True)
        self.assertEqual(self.robot.goal.extra["max_speed"], 0.2)

    def test_requested_speed_is_lowered_to_the_body_limit(self):
        seen = {}
        original = self.robot.start_action

        def spy(cap_id, args):
            seen.update(args)
            return original(cap_id, args)
        self.robot.start_action = spy
        self.bus.start_action("base.navigate_to_place", {"place": "会议室", "max_speed": 5.0}, authorized=True)
        self.assertEqual(seen["max_speed"], sim_quadruped.MAX_VX)
        limited = events_of(self.bus, "safety.limited")
        self.assertEqual([(e["field"], e["requested"], e["applied"]) for e in limited],
                         [("max_speed", 5.0, sim_quadruped.MAX_VX)])

    def test_it_never_walks_faster_than_asked(self):
        handle = self.bus.start_action("base.navigate_to_place", {"place": "会议室", "max_speed": 0.2}, authorized=True)
        fastest = 0.0
        for _ in range(200):
            self.clock.advance(0.1)
            handle.poll()
            fastest = max(fastest, abs(self.robot.velocity[0]))
        self.assertLessEqual(fastest, 0.2 + 1e-9)
        self.assertGreater(fastest, 0.0)

    def test_stop_cancels_and_latch_refuses_the_next(self):
        handle = self.bus.start_action("base.navigate_to_place", {"place": "二楼"}, authorized=True)
        self.clock.advance(1.0)
        self.assertEqual(handle.poll().state, "running")
        self.bus.supervisor.emergency_stop("test")
        self.assertEqual(handle.poll().state, "cancelled")
        self.assertEqual(self.robot.velocity, (0.0, 0.0, 0.0))
        with self.assertRaises(EStopLatched):
            self.bus.start_action("base.navigate_to_place", {"place": "二楼"}, authorized=True)


class Explore(_Base):
    def test_walks_until_the_time_is_up(self):
        handle = self.bus.start_action("base.explore", {"duration_s": 30.0, "radius": 1.5}, authorized=True)
        self.clock.advance(10.0)
        status = handle.poll()
        self.assertEqual(status.state, "running")
        self.assertGreater(status.progress, 0.2)
        status = self.run_to_end(handle)
        self.assertEqual(status.state, "succeeded")
        self.assertEqual(status.result["completed"], True)
        self.assertGreater(status.result["travelled"], 1.0)
        self.assertEqual(self.robot.velocity, (0.0, 0.0, 0.0))

    def test_stays_inside_the_radius(self):
        handle = self.bus.start_action("base.explore", {"duration_s": 60.0, "radius": 1.0}, authorized=True)
        furthest = 0.0
        for _ in range(700):
            self.clock.advance(0.1)
            handle.poll()
            furthest = max(furthest, math.hypot(self.robot.x, self.robot.y))
        self.assertLessEqual(furthest, 1.0 + 0.15)

    def test_stops_at_the_distance_limit(self):
        handle = self.bus.start_action("base.explore", {"duration_s": 300.0, "max_distance": 2.0}, authorized=True)
        status = self.run_to_end(handle)
        self.assertEqual(status.state, "succeeded")
        self.assertLess(status.result["travelled"], 2.2)

    def test_longer_or_wider_than_the_body_allows_is_refused(self):
        for args in ({"duration_s": sim_quadruped.EXPLORE_MAX_DURATION_S + 1},
                     {"duration_s": 30.0, "radius": sim_quadruped.EXPLORE_MAX_RADIUS + 0.5}):
            with self.subTest(args=args):
                with self.assertRaises(SafetyRejected):
                    self.bus.start_action("base.explore", args, authorized=True)
                self.assertIsNone(self.robot.goal)

    def test_needs_authorization_and_a_stand(self):
        with self.assertRaises(NotAuthorized):
            self.bus.start_action("base.explore", {"duration_s": 10.0}, authorized=False)
        self.bus.invoke("legged.posture", {"posture": "lie"}, authorized=True)
        handle = self.bus.start_action("base.explore", {"duration_s": 10.0}, authorized=True)
        self.assertEqual(handle.poll().state, "failed")

    def test_stop_ends_it(self):
        handle = self.bus.start_action("base.explore", {"duration_s": 60.0}, authorized=True)
        self.clock.advance(3.0)
        handle.poll()
        self.bus.supervisor.emergency_stop("test")
        self.assertEqual(handle.poll().state, "cancelled")
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))


class NoSpeedBound(unittest.TestCase):
    """A body that declares no speed limit and has none filled in: the supervisor has nothing to
    bound the body's own navigation with, so it refuses."""

    def test_refused(self):
        robot = SimQuadruped(clock=ManualClock())
        manifest = robot.manifest()
        manifest["safety"] = {"hardware_estop": False}
        for cap in manifest["capabilities"]:
            if cap["id"] == "base.velocity":
                cap["params"] = {"max_vy": 0.5, "holonomic": True}
        robot.manifest = lambda: manifest
        bus = CapabilityBus(robot)
        for cap_id, args in (("base.navigate_to_place", {"place": "会议室"}), ("base.explore", {"duration_s": 5.0})):
            with self.subTest(cap_id=cap_id):
                with self.assertRaises(SafetyRejected):
                    bus.start_action(cap_id, args, authorized=True)
        self.assertIsNone(robot.goal)


class Timeouts(unittest.TestCase):
    def test_direct_invocation_defaults(self):
        self.assertEqual(Runner.action_timeout("base.navigate_to_place", {"place": "会议室"}), 1800.0)
        self.assertEqual(Runner.action_timeout("base.explore", {"duration_s": 60.0}), 4500.0)
        self.assertEqual(Runner.action_timeout("base.explore", {"duration_s": 60.0}, 90), 90.0)


if __name__ == "__main__":
    unittest.main()
