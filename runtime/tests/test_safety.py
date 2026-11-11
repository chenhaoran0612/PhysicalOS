"""Safety supervisor: limits, rejections, watchdog, latch/reset, action chunks.

Uses ManualClock so simulated time is deterministic.
"""
import unittest

import _support  # noqa: F401

from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.bus import CapabilityBus
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.errors import BadRequest, EStopLatched, NotAuthorized, SafetyRejected
from neuxmind_runtime.events import EventBus
from neuxmind_runtime.safety import SafetySupervisor


def events_of(bus, type_):
    return [e for e in bus.events.since(0) if e["type"] == type_]


class QuadrupedSafetyTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.robot = SimQuadruped(clock=self.clock)
        self.bus = CapabilityBus(self.robot)
        self.sup = self.bus.supervisor

    def test_velocity_is_clamped_and_recorded(self):
        out = self.bus.invoke("base.velocity", {"vx": 5.0, "wz": -9.0}, authorized=True)
        self.assertEqual(out, {"accepted": True})
        self.assertEqual(self.robot.cmd[0], 1.0)       # max_vx
        self.assertEqual(self.robot.cmd[2], -1.5)      # max_wz
        limited = events_of(self.bus, "safety.limited")
        self.assertEqual({e["field"] for e in limited}, {"vx", "wz"})
        self.assertEqual(limited[0]["requested"], 5.0)

    def test_motion_requires_authorization(self):
        with self.assertRaises(NotAuthorized):
            self.bus.invoke("base.velocity", {"vx": 0.1, "wz": 0.0}, authorized=False)
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))

    def test_spec_validation_rejects_bad_payloads(self):
        with self.assertRaises(BadRequest):
            self.bus.invoke("base.velocity", {"vx": "fast", "wz": 0.0}, authorized=True)
        with self.assertRaises(BadRequest):
            self.bus.invoke("base.velocity", {"vx": 0.1, "wz": 0.0, "turbo": True}, authorized=True)

    def test_nan_in_pose_rejected(self):
        # Since iter01-fix the spec payload validator rejects non-finite values
        # (BadRequest); the supervisor's own NaN check (SafetyRejected) is the
        # second line of defence. Either way: rejected, nothing reaches the robot.
        goal = [float("nan"), 0, 0, 0, 0, 0, 1]
        with self.assertRaises((BadRequest, SafetyRejected)):
            self.bus.start_action("base.navigate_to", {"goal": goal}, authorized=True)
        self.assertIsNone(self.robot.goal)
        with self.assertRaises((BadRequest, SafetyRejected)):
            self.bus.invoke("base.velocity", {"vx": float("inf"), "wz": 0.0}, authorized=True)
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))

    def test_navigation_goal_outside_workspace_rejected(self):
        with self.assertRaises(SafetyRejected):
            self.bus.start_action("base.navigate_to", {"goal": [500.0, 0, 0, 0, 0, 0, 1]}, authorized=True)

    def test_watchdog_zeroes_stale_velocity(self):
        self.bus.invoke("base.velocity", {"vx": 0.5, "wz": 0.0, "timeout_s": 0.3}, authorized=True)
        self.clock.advance(0.2)
        self.assertFalse(self.sup.watchdog_tick())
        self.assertEqual(self.robot.cmd[0], 0.5)
        self.clock.advance(0.2)
        self.assertTrue(self.sup.watchdog_tick())
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))
        self.assertTrue(events_of(self.bus, "safety.watchdog"))
        x = self.bus.read("base.odometry")["pose"][0]
        self.clock.advance(1.0)
        self.assertAlmostEqual(self.bus.read("base.odometry")["pose"][0], x)

    def test_watchdog_refresh_keeps_moving(self):
        for _ in range(5):
            self.bus.invoke("base.velocity", {"vx": 0.5, "wz": 0.0, "timeout_s": 0.3}, authorized=True)
            self.clock.advance(0.2)
            self.assertFalse(self.sup.watchdog_tick())
        self.assertAlmostEqual(self.bus.read("base.odometry")["pose"][0], 0.5, places=6)

    def test_default_and_maximum_velocity_timeout(self):
        self.bus.invoke("base.velocity", {"vx": 0.5, "wz": 0.0, "timeout_s": 60.0}, authorized=True)
        self.clock.advance(2.01)        # clamped to MAX_VELOCITY_TIMEOUT_S = 2.0
        self.assertTrue(self.sup.watchdog_tick())

    def test_estop_latch_and_reset(self):
        self.bus.invoke("base.velocity", {"vx": 0.5, "wz": 0.0}, authorized=True)
        result = self.sup.emergency_stop("test")
        self.assertTrue(result["latched"])
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))
        with self.assertRaises(EStopLatched):
            self.bus.invoke("base.velocity", {"vx": 0.1, "wz": 0.0}, authorized=True)
        with self.assertRaises(EStopLatched):
            self.bus.start_action("base.navigate_to", {"goal": [1.0, 0, 0, 0, 0, 0, 1]}, authorized=True)
        # Non-motion capabilities stay available while latched.
        self.assertTrue(self.bus.read("system.health")["ok"])
        with self.assertRaises(NotAuthorized):
            self.sup.reset(authorized=False)
        self.assertTrue(self.sup.latched)
        self.sup.reset(authorized=True)
        self.assertFalse(self.sup.latched)
        self.bus.invoke("base.velocity", {"vx": 0.1, "wz": 0.0}, authorized=True)
        self.assertEqual(self.robot.cmd[0], 0.1)

    def test_stop_racing_with_command_is_reapplied(self):
        """If the latch is set while a command is in flight, the robot is stopped again."""
        original_invoke = self.robot.invoke

        def invoke_then_estop(cap_id, args):
            out = original_invoke(cap_id, args)
            self.sup.emergency_stop("race")   # stop lands after the check, before return
            return out

        self.robot.invoke = invoke_then_estop
        with self.assertRaises(EStopLatched):
            self.bus.invoke("base.velocity", {"vx": 0.8, "wz": 0.0}, authorized=True)
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))

    def test_posture_must_be_supported(self):
        self.bus.invoke("legged.posture", {"posture": "lie"}, authorized=True)
        self.assertEqual(self.robot.posture, "lie")
        # Lying down: the sim refuses velocity (accepted=false), the robot stays put.
        self.assertEqual(self.bus.invoke("base.velocity", {"vx": 0.5, "wz": 0.0}, authorized=True),
                         {"accepted": False})


class NonHolonomicTests(unittest.TestCase):
    def test_lateral_velocity_zeroed_on_non_holonomic_base(self):
        robot = SimQuadruped(clock=ManualClock())
        manifest = robot.manifest()
        for cap in manifest["capabilities"]:
            if cap["id"] == "base.velocity":
                cap["params"]["holonomic"] = False
        robot.manifest = lambda: manifest
        bus = CapabilityBus(robot)
        bus.invoke("base.velocity", {"vx": 0.2, "vy": 0.4, "wz": 0.0}, authorized=True)
        self.assertEqual(robot.cmd[1], 0.0)


class ArmSafetyTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.arm = SimArm(clock=self.clock)
        self.bus = CapabilityBus(self.arm)

    def test_joint_limits_reject(self):
        with self.assertRaises(SafetyRejected):
            self.bus.start_action("arm.joint_position", {"position": [0, 2.5, 0, 0, 0, 0]}, authorized=True)
        self.assertIsNone(self.arm.arm_goal)
        rejected = [e for e in self.bus.events.since(0) if e["type"] == "safety.rejected"]
        self.assertIn("关节 1", rejected[0]["reason"])

    def test_dof_mismatch_rejected(self):
        with self.assertRaises(SafetyRejected):
            self.bus.start_action("arm.joint_position", {"position": [0.1, 0.1]}, authorized=True)

    def test_joint_speed_clamped_and_motion_interpolated(self):
        handle = self.bus.start_action("arm.joint_position",
                                       {"position": [1.0, 0, 0, 0, 0, 0], "max_velocity": 10.0}, authorized=True)
        self.assertEqual(self.arm.arm_goal.extra["speed"], 1.0)
        self.clock.advance(0.5)
        status = handle.poll()
        self.assertEqual(status.state, "running")
        self.assertAlmostEqual(self.bus.read("joint.state")["position"][0], 0.5, places=6)
        self.clock.advance(0.6)
        status = handle.poll()
        self.assertEqual(status.state, "succeeded")
        self.assertEqual(status.result["position"][0], 1.0)

    def test_gripper_opening_range(self):
        with self.assertRaises(SafetyRejected):
            self.bus.start_action("gripper.position", {"opening": 1.5}, authorized=True)

    def test_stop_cancels_arm_motion(self):
        handle = self.bus.start_action("arm.joint_position", {"position": [2.0, 0, 0, 0, 0, 0]}, authorized=True)
        self.clock.advance(0.3)
        handle.poll()
        self.bus.supervisor.emergency_stop("test")
        self.clock.advance(1.0)
        self.assertEqual(handle.poll().state, "cancelled")
        self.assertAlmostEqual(self.bus.read("joint.state")["position"][0], 0.3, places=6)


class ActionChunkTests(unittest.TestCase):
    """policy.action_chunk step-by-step limiting (supervisor level; no sim adapter executes chunks)."""

    def _bus(self, space, dim, extra_caps=()):
        arm = SimArm(clock=ManualClock())
        manifest = arm.manifest()
        manifest["capabilities"].append({"id": "policy.action_chunk",
                                         "params": {"action_space": space, "dim": dim, "rate_hz": 10.0}})
        manifest["capabilities"].extend(extra_caps)
        arm.manifest = lambda: manifest
        return CapabilityBus(arm)

    def test_joint_position_chunk_limited_per_step(self):
        bus = self._bus("joint_position", 6)
        actions = [[0.5, 0, 0, 0, 0, 0], [5.0, 0, 0, 0, 0, 0], [1.0, 0, 0, 0, 0, 3.5]]
        limited, notes = bus.supervisor.check("policy.action_chunk", {"actions": actions, "dt": 0.1},
                                              True, {"joint_position": [0.0] * 6})
        steps = limited["actions"]
        self.assertAlmostEqual(steps[0][0], 0.1)          # 1.0 rad/s * 0.1 s from 0.0
        self.assertAlmostEqual(steps[1][0], 0.2)
        self.assertAlmostEqual(steps[2][0], 0.3)
        self.assertLessEqual(steps[2][5], 0.1 + 1e-12)
        self.assertTrue(notes and notes[0]["clamped_values"] >= 3)

    def test_base_velocity_chunk_limited(self):
        bus = self._bus("base_velocity", 3, [{"id": "base.velocity", "params": {
            "max_vx": 0.5, "max_vy": 0.0, "max_wz": 1.0, "holonomic": False}}])
        limited, _ = bus.supervisor.check("policy.action_chunk", {"actions": [[2.0, 1.0, -3.0]], "dt": 0.1}, True)
        self.assertEqual(limited["actions"], [[0.5, 0.0, -1.0]])

    def test_unknown_action_space_and_bad_shape_rejected(self):
        bus = self._bus("ee_delta", 6)
        with self.assertRaises(SafetyRejected):
            bus.supervisor.check("policy.action_chunk", {"actions": [[0.0] * 6], "dt": 0.1}, True)
        bus = self._bus("joint_position", 6)
        with self.assertRaises(SafetyRejected):
            bus.supervisor.check("policy.action_chunk", {"actions": [[0.0] * 5], "dt": 0.1}, True)
        with self.assertRaises(SafetyRejected):     # faster than the 10 Hz control period
            bus.supervisor.check("policy.action_chunk", {"actions": [[0.0] * 6], "dt": 0.01}, True)


class RecordingQuadruped(SimQuadruped):
    """Records every motion call that reaches the adapter."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.dispatched = []

    def invoke(self, cap_id, args):
        self.dispatched.append(("invoke", cap_id, dict(args)))
        return super().invoke(cap_id, args)

    def start_action(self, cap_id, args):
        self.dispatched.append(("start_action", cap_id, dict(args)))
        return super().start_action(cap_id, args)


class LatchGateTests(unittest.TestCase):
    """The latch and the stop epoch must stop commands *before* the adapter (review mutant R1)."""

    def setUp(self):
        self.robot = RecordingQuadruped(clock=ManualClock())
        self.bus = CapabilityBus(self.robot)
        self.sup = self.bus.supervisor

    def test_latched_motion_never_reaches_adapter(self):
        self.sup.emergency_stop("test")
        self.robot.dispatched.clear()
        with self.assertRaises(EStopLatched):
            self.bus.invoke("base.velocity", {"vx": 0.3, "wz": 0.0}, authorized=True)
        with self.assertRaises(EStopLatched):
            self.bus.start_action("base.navigate_to", {"goal": [1.0, 0, 0, 0, 0, 0, 1]}, authorized=True)
        with self.assertRaises(EStopLatched):
            self.bus.invoke("legged.posture", {"posture": "sit"}, authorized=True)
        self.assertEqual(self.robot.dispatched, [])        # zero motion dispatched while latched
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))
        self.assertIsNone(self.robot.goal)

    def test_call_from_before_stop_is_refused_after_reset(self):
        epoch = self.sup.epoch                 # recorded when the request/run was admitted
        self.sup.emergency_stop("operator")
        self.sup.reset(authorized=True)
        self.robot.dispatched.clear()
        with self.assertRaises(EStopLatched) as ctx:
            self.bus.invoke("base.velocity", {"vx": 0.3, "wz": 0.0}, authorized=True, epoch=epoch)
        self.assertEqual(ctx.exception.code, "stale_after_stop")
        with self.assertRaises(EStopLatched):
            self.bus.start_action("base.navigate_to", {"goal": [1.0, 0, 0, 0, 0, 0, 1]},
                                  authorized=True, epoch=epoch)
        self.assertEqual(self.robot.dispatched, [])
        # A fresh call (current epoch) works.
        self.bus.invoke("base.velocity", {"vx": 0.3, "wz": 0.0}, authorized=True, epoch=self.sup.epoch)
        self.assertEqual(self.robot.cmd[0], 0.3)

    def test_stop_and_reset_while_command_in_flight_is_undone(self):
        """Stop *and* reset both land while the adapter call is in progress: stop again."""
        original = self.robot.invoke

        def invoke_with_stop_and_reset(cap_id, args):
            out = original(cap_id, args)
            if cap_id == "base.velocity" and args.get("vx"):
                self.sup.emergency_stop("race")
                self.sup.reset(authorized=True)
            return out

        self.robot.invoke = invoke_with_stop_and_reset
        with self.assertRaises(EStopLatched):
            self.bus.invoke("base.velocity", {"vx": 0.8, "wz": 0.0}, authorized=True)
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))

    def test_reset_racing_with_stop_stays_latched(self):
        self.sup.emergency_stop("first")
        writes = []

        def persist(latched, reason, epoch):
            writes.append((latched, epoch))
            self.sup.emergency_stop("second, during reset")     # lands after the disk write

        with self.assertRaises(EStopLatched):
            self.sup.reset(authorized=True, persist=persist)
        self.assertTrue(self.sup.latched)
        self.assertEqual(writes[0][0], False)


def _supervisor(caps, safety=None):
    manifest = {"capabilities": [{"id": "safety.stop"}, {"id": "system.health"}] + caps, "safety": safety or {}}
    return SafetySupervisor(manifest, None, EventBus(), ManualClock())


class DefensiveManifestTests(unittest.TestCase):
    """R-02/R-03: even if spec validation is bypassed, bad limits refuse to start or fail closed."""

    def test_bad_limits_refuse_to_start(self):
        bad = [
            [{"id": "base.velocity", "params": {"max_vx": -1.0, "max_wz": 1.0}}],
            [{"id": "base.velocity", "params": {"max_vx": float("nan"), "max_wz": 1.0}}],
            [{"id": "base.velocity", "params": {"max_vx": "fast", "max_wz": 1.0}}],
            [{"id": "base.velocity", "params": {"max_vx": 1.0, "max_vy": -0.1, "max_wz": 1.0}}],
            [{"id": "arm.joint_position", "params": {"dof": 6, "lower": [-1.0], "upper": [1.0], "max_velocity": 1.0}}],
            [{"id": "arm.joint_position", "params": {"dof": 2, "lower": [-1, 1], "upper": [1, 0], "max_velocity": 1.0}}],
            [{"id": "arm.joint_position", "params": {"dof": 2, "lower": [-1, -1], "max_velocity": 1.0}}],
            [{"id": "arm.joint_position", "params": {"dof": 2, "lower": [-1, -1], "upper": [1, 1], "max_velocity": 0}}],
            [{"id": "arm.joint_position", "params": {"dof": 2, "lower": [-1, -1], "upper": [1, 1], "max_velocity": 1}},
             {"id": "policy.action_chunk", "params": {"action_space": "joint_position", "dim": 3}}],
            [{"id": "arm.ee_pose", "params": {"reach": float("inf")}}],
        ]
        for caps in bad:
            with self.subTest(caps=caps), self.assertRaises(ValueError):
                _supervisor(caps)
        for safety in ({"max_linear_speed": -1.0}, {"workspace": {"x": [5, -5]}}, {"workspace": {"x": [0, float("nan")]}}):
            with self.subTest(safety=safety), self.assertRaises(ValueError):
                _supervisor([], safety)

    def test_bad_adapter_manifest_refuses_bus(self):
        robot = SimQuadruped(clock=ManualClock())
        manifest = robot.manifest()
        for cap in manifest["capabilities"]:
            if cap["id"] == "base.velocity":
                cap["params"]["max_vx"] = -1.0
        robot.manifest = lambda: manifest
        with self.assertRaises(ValueError):
            CapabilityBus(robot)

    def test_limits_corrupted_after_start_fail_closed(self):
        sup = _supervisor([{"id": "base.velocity", "params": {"max_vx": 1.0, "max_wz": 1.0}},
                           {"id": "arm.joint_position", "params": {"dof": 6, "lower": [-1.0] * 6,
                                                                   "upper": [1.0] * 6, "max_velocity": 1.0}}])
        # The limits are frozen (N-04); simulate a corrupted supervisor state by
        # replacing whole entries, to exercise the fail-closed limiter.
        sup.params["base.velocity"] = {"max_vx": -1.0, "max_wz": 1.0}   # _clamp(v, 1, -1) used to give +1.0
        for vx in (-0.2, 0.05):
            with self.assertRaises(SafetyRejected):
                sup.check("base.velocity", {"vx": vx, "wz": 0.0}, True)
        sup.params["base.velocity"] = {"max_vx": float("nan"), "max_wz": 1.0}
        with self.assertRaises(SafetyRejected):
            sup.check("base.velocity", {"vx": 0.3, "wz": 0.0}, True)
        sup.params["base.velocity"] = {"max_vx": "fast", "max_wz": 1.0}
        with self.assertRaises(SafetyRejected):
            sup.check("base.velocity", {"vx": 0.3, "wz": 0.0}, True)
        sup.params["arm.joint_position"] = {"dof": 6, "lower": [-1.0], "upper": [1.0],   # shorter than dof
                                            "max_velocity": 1.0}
        with self.assertRaises(SafetyRejected):
            sup.check("arm.joint_position", {"position": [0, 0, 0, 0, 0, 100.0]}, True)

    def test_huge_integer_is_rejected_not_unknown(self):
        bus = CapabilityBus(SimQuadruped(clock=ManualClock()))
        with self.assertRaises((BadRequest, SafetyRejected)):
            bus.invoke("base.velocity", {"vx": 10 ** 400, "wz": 0}, authorized=True)
        sup = _supervisor([{"id": "base.velocity", "params": {"max_vx": 1.0, "max_wz": 1.0}}])
        with self.assertRaises(SafetyRejected):
            sup.check("base.velocity", {"vx": 10 ** 400, "wz": 0}, True)


class ChunkFailClosedTests(unittest.TestCase):
    """R-04: joint_position chunks need a successfully read joint.state of length dim."""

    def _caps(self):
        return [{"id": "arm.joint_position", "params": {"dof": 2, "lower": [-3, -3], "upper": [3, 3], "max_velocity": 1.0}},
                {"id": "policy.action_chunk", "params": {"action_space": "joint_position", "dim": 2, "rate_hz": 10.0}}]

    def test_without_joint_state_rejected(self):
        sup = _supervisor(self._caps())
        args = {"actions": [[3.0, -3.0]], "dt": 0.1}
        with self.assertRaises(SafetyRejected):
            sup.check("policy.action_chunk", args, True, {})
        with self.assertRaises(SafetyRejected):
            sup.check("policy.action_chunk", args, True, {"joint_position": [0.0]})          # shorter than dim
        with self.assertRaises(SafetyRejected):
            sup.check("policy.action_chunk", args, True, {"joint_position": [0.0, float("nan")]})
        limited, _ = sup.check("policy.action_chunk", args, True, {"joint_position": [0.0, 0.0]})
        self.assertEqual(limited["actions"], [[0.1, -0.1]])

    def test_bus_rejects_when_joint_state_unreadable(self):
        arm = SimArm(clock=ManualClock())
        manifest = arm.manifest()
        manifest["capabilities"].append({"id": "policy.action_chunk",
                                         "params": {"action_space": "joint_position", "dim": 6, "rate_hz": 10.0}})
        arm.manifest = lambda: manifest
        started = []
        arm.start_action = lambda cap, args: started.append(cap)

        def broken_read(cap_id):
            raise RuntimeError("serial timeout")
        arm.read = broken_read
        bus = CapabilityBus(arm)
        with self.assertRaises(SafetyRejected) as ctx:
            bus.start_action("policy.action_chunk", {"actions": [[0.5] * 6], "dt": 0.1}, authorized=True)
        self.assertIn("joint.state", str(ctx.exception))
        self.assertEqual(started, [])

    def test_bus_rejects_without_declared_joint_state(self):
        arm = SimArm(clock=ManualClock())
        manifest = arm.manifest()
        manifest["capabilities"] = [c for c in manifest["capabilities"] if c["id"] != "joint.state"]
        manifest["capabilities"].append({"id": "policy.action_chunk",
                                         "params": {"action_space": "joint_position", "dim": 6, "rate_hz": 10.0}})
        arm.manifest = lambda: manifest
        bus = CapabilityBus(arm)
        with self.assertRaises(SafetyRejected):
            bus.start_action("policy.action_chunk", {"actions": [[0.5] * 6], "dt": 0.1}, authorized=True)


if __name__ == "__main__":
    unittest.main()
