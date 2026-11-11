"""Regression tests for the round-04 review (docs/evolution/reviews/2026-11-09-iter04-review.md).

The "lying adapters" and fault scenarios of the review's scripts (scratchpad/review04/) are
re-created here so every finding stays fixed:

* Q4-01 an operator's stop at any phase aborts the suite, stays latched, is never reset;
* Q4-02 dry never moves an arm whose joint.state does not map 1:1 to the arm; a dry-level
  adapter motion guard refuses anything that is not provably zero-displacement;
* Q4-03 any motion failure (adapter error included) stops, latches and aborts;
* Q4-04 no navigation goal without localization.pose;
* Q4-05 a time scale is only honoured for the built-in simulations, which then run at 1x;
* Q4-06 speed magnitude and direction are checked;
* Q4-07 stillness is held after stop/cancel/reset and at the end;
* Q4-09 scaffold inputs cannot inject code and a failed generation leaves nothing behind;
* Q4-10/11/15 all samples checked, gripper.state / arm.ee_state used, motion lease.
Each scenario was first reproduced against the unfixed code with the review's script.
"""
import copy
import json
import math
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import _support

from neuxmind_runtime import conformance, scaffold
from neuxmind_runtime.adapters._sim import SimGoal, SimHandle
from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.clock import RealClock
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import RuntimeApiError
from neuxmind_runtime.hal import ActionStatus


def cases(report, status=None):
    return [c for c in report["cases"] if status is None or c["status"] == status]


def case(report, cid):
    return next(c for c in report["cases"] if c["id"] == cid)


class Recording:
    """Mixin: records invoke/start_action/stop/reset calls reaching the adapter."""

    def _rec(self, *item):
        self.__dict__.setdefault("calls", []).append(item)

    def invoke(self, cap_id, args):
        self._rec("invoke", cap_id, dict(args))
        return super().invoke(cap_id, args)

    def start_action(self, cap_id, args):
        self._rec("start_action", cap_id, copy.deepcopy(args))
        return super().start_action(cap_id, args)


# ------------------------------------------------------------------ review liars (Q4-06/07)
class VelocityX5(SimQuadruped):
    def invoke(self, cap_id, args):
        if cap_id == "base.velocity":
            args = {k: (v * 5.0 if k in ("vx", "vy", "wz") else v) for k, v in args.items()}
        return super().invoke(cap_id, args)


class VelocityReversed(SimQuadruped):
    def invoke(self, cap_id, args):
        if cap_id == "base.velocity":
            args = dict(args, vx=-args["vx"])
        return super().invoke(cap_id, args)


class NavCancelResume(SimQuadruped):
    """Cancel honoured, the goal silently resumes 1.3 s later."""

    def start_action(self, cap_id, args):
        handle = super().start_action(cap_id, args)
        adapter, goal = self, handle._goal

        class H(SimHandle):
            def cancel(inner):
                super().cancel()
                saved = SimGoal(goal.kind, goal.target, **goal.extra)

                def resume():
                    time.sleep(1.3)
                    with adapter._lock:
                        if adapter.goal is None:
                            adapter.goal = saved
                threading.Thread(target=resume, daemon=True).start()
        return H(self, goal)


class NavStopResume(SimQuadruped):
    """stop() honoured, but the navigator silently resumes its goal 1.3 s later (while latched)."""

    def stop(self):
        goal = self.goal
        super().stop()
        if goal is not None:
            self._goal_stops = getattr(self, "_goal_stops", 0) + 1
        if goal is not None and self._goal_stops == 2 and not getattr(self, "_resumed", False):
            # the first stop with a goal is the cancel case's halt; the second is the stop case
            self._resumed = True
            saved = SimGoal(goal.kind, goal.target, **goal.extra)

            def resume():
                time.sleep(1.3)
                with self._lock:
                    if self.goal is None:
                        self.goal = saved
            threading.Thread(target=resume, daemon=True).start()


class ArmCancelResume(SimArm):
    def start_action(self, cap_id, args):
        handle = super().start_action(cap_id, args)
        if cap_id != "arm.joint_position":
            return handle
        adapter, goal = self, handle._goal

        class H(SimHandle):
            def cancel(inner):
                super().cancel()
                saved = SimGoal(goal.kind, list(goal.target), **goal.extra)

                def resume():
                    time.sleep(1.3)
                    with adapter._lock:
                        if adapter.arm_goal is None or adapter.arm_goal.state != "running":
                            adapter.arm_goal = saved
                threading.Thread(target=resume, daemon=True).start()
        return H(self, goal)


class ResetReplaysVelocity(SimQuadruped):
    """reset() re-applies the last non-zero setpoint (a driver replaying a stale command)."""

    def invoke(self, cap_id, args):
        out = super().invoke(cap_id, args)
        if cap_id == "base.velocity" and any(abs(args.get(k, 0.0)) > 0 for k in ("vx", "wz")):
            self._last_cmd = (args["vx"], args.get("vy", 0.0), args["wz"])
        return out

    def reset(self):
        if getattr(self, "_last_cmd", None):
            self.cmd = self._last_cmd


class CoastLying(SimQuadruped):
    """Keeps driving 1.5 s after stop() while odometry is frozen at the stop."""

    def stop(self):
        last = self.velocity
        super().stop()
        if any(abs(v) > 1e-6 for v in last):
            self._coast = (last, time.monotonic() + 1.5, {"pose": self._pose(), "velocity": [0.0, 0.0, 0.0]})

    def _step(self, dt):
        coast = getattr(self, "_coast", None)
        if coast and time.monotonic() < coast[1]:
            self.cmd = coast[0]
            super()._step(dt)
            self.cmd = (0.0, 0.0, 0.0)
            return
        super()._step(dt)

    def read(self, cap_id):
        out = super().read(cap_id)
        coast = getattr(self, "_coast", None)
        if cap_id == "base.odometry" and coast and time.monotonic() < coast[1]:
            return copy.deepcopy(coast[2])
        return out


class NoLocalization(Recording, SimQuadruped):
    def manifest(self):
        m = super().manifest()
        m["capabilities"] = [c for c in m["capabilities"] if c["id"] != "localization.pose"]
        m["simulated"] = False
        return m


class FakeTimeScaleRealBody(Recording, SimQuadruped):
    """Not simulated, but its clock claims 20x."""

    def __init__(self):
        super().__init__()
        self.clock.time_scale = 20.0

    def manifest(self):
        return {**super().manifest(), "simulated": False}


class GripperFakeResult(SimArm):
    """Gripper goals report the requested opening; the jaw never moves (gripper.state is honest)."""

    def start_action(self, cap_id, args):
        if cap_id != "gripper.position":
            return super().start_action(cap_id, args)
        goal, target, adapter = SimGoal(cap_id, float(args["opening"])), float(args["opening"]), self

        class H(SimHandle):
            def poll(inner):
                if goal.state == "running":
                    goal.finish({"opening": target})
                return ActionStatus(state=goal.state, progress=goal.progress, result=goal.result, error=goal.error)
        return H(self, goal)


class HealthJitterArm(SimArm):
    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "system.health":
            self._n = getattr(self, "_n", 0) + 1
            if self._n % 3 != 0:
                return {"ok": False, "mode": "fault", "errors": {"motor": "overcurrent"}}
        return out


class ImuNotNormalized(SimQuadruped):
    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "sensor.imu":
            out["orientation"] = [0.0, 0.0, 0.0, 0.5]
        return out


# ------------------------------------------------------------------ Q4-02 / Q4-03 bodies
class ArmJointStateExtra(Recording, SimArm):
    """ROS-style joint_states: a finger joint listed first (7 joints), arm dof 6, q != 0."""

    def __init__(self):
        super().__init__()
        self.q = [0.3, -0.4, 0.5, -0.2, 0.1, 0.0]

    def manifest(self):
        m = super().manifest()
        for c in m["capabilities"]:
            if c["id"] == "joint.state":
                c["params"]["dof"] = 7
            if c["id"] == "arm.joint_position" and not getattr(self, "declare_names", False):
                c["params"].pop("joint_names", None)     # adapter without the v1.1 declaration (R4-01)
        return m

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "joint.state":
            return {"names": ["finger_joint"] + out["names"], "position": [0.04] + out["position"],
                    "velocity": [0.0] + out["velocity"]}
        return out


class ArmJointStatePermutedNames(SimArm):
    """Same count, but the names change between reads (unstable mapping)."""

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "joint.state":
            self._flip = not getattr(self, "_flip", False)
            if self._flip:
                out["names"] = list(reversed(out["names"]))
        return out


class ArmReadRaisesMidMotion(SimArm):
    def start_action(self, cap_id, args):
        handle = super().start_action(cap_id, args)
        if cap_id == "arm.joint_position" and abs(args["position"][-1] - self.q[-1]) > 0.08 and not hasattr(self, "_armed_at"):
            self._armed_at = time.monotonic()
        return handle

    def read(self, cap_id):
        t = getattr(self, "_armed_at", None)
        if cap_id == "joint.state" and t is not None and 0.1 < time.monotonic() - t < 4.0:
            raise RuntimeError("joint_states topic lost")
        return super().read(cap_id)

    def _step(self, dt):
        super()._step(dt)
        self.max_wrist = max(getattr(self, "max_wrist", 0.0), abs(self.q[-1]))


class OperatorStops(conformance.AdapterTarget):
    """Operator presses stop just before the suite's own stop with ``label`` (or before a reset)."""

    def __init__(self, adapter, label=None, before_reset=None):
        super().__init__(adapter)
        self.label, self.before_reset, self.resets_after = label, before_reset, 0
        self._resets = 0
        self.fired = False

    def _operator(self):
        self.fired = True
        self.runtime.emergency_stop("operator: person in path", source="api")

    def stop(self, reason):
        if not self.fired and self.label and self.label in reason:
            self._operator()
        return super().stop(reason)

    def reset(self, authorized=True, expect_stop_count=None):
        self._resets += 1
        if self.fired:
            self.resets_after += 1
        if not self.fired and self.before_reset is not None and self._resets == self.before_reset:
            self._operator()
        return super().reset(authorized, expect_stop_count)


def run_motion(adapter, **kw):
    return conformance.run_adapter(adapter, "motion", True, **kw)


class OperatorStopIsNeverCleared(unittest.TestCase):
    """Q4-01: at every phase the operator's latch wins."""

    def assert_aborted_by_operator(self, target, report):
        self.assertTrue(target.fired, "scenario did not trigger")
        self.assertTrue(report["aborted"])
        self.assertTrue(report["final_latched"])
        self.assertTrue(target.runtime.supervisor.latched)
        self.assertEqual(target.resets_after, 0)
        resets = [e for e in target.runtime.events.since(0, 10 ** 6) if e["type"] == "safety.reset"]
        estops = [e for e in target.runtime.events.since(0, 10 ** 6) if e["type"] == "safety.estop"
                  and e["reason"] == "operator: person in path"]
        self.assertTrue(estops)
        self.assertFalse([e for e in resets if e["seq"] > estops[0]["seq"]], "reset after the operator's stop")
        later = [c for c in report["cases"] if c["level"] != "static"][-1]
        self.assertEqual((later["status"], later.get("skip_kind")), ("skipped", "blocked"))

    def test_dry_phases(self):
        for kw in ({"label": "dry-stop-0"}, {"label": "dry-stop-1"}, {"label": "stale-probe"},
                   {"label": "dry-posture"}, {"before_reset": 1}, {"before_reset": 3}):
            with self.subTest(**kw):
                target = OperatorStops(SimQuadruped(), **kw)
                try:
                    report = conformance.run(target, "dry")
                    self.assert_aborted_by_operator(target, report)
                    self.assertIs(report["dry_ok"], False)
                finally:
                    target.close()

    def test_motion_phases(self):
        for adapter, kw in ((SimQuadruped(), {"label": "velocity-stop"}), (SimQuadruped(), {"label": "nav-stop"}),
                            (SimArm(), {"label": "arm-stop"}), (SimArm(), {"label": "gripper-stop"})):
            with self.subTest(**kw):
                target = OperatorStops(adapter, **kw)
                try:
                    report = conformance.run(target, "motion", True)
                    self.assert_aborted_by_operator(target, report)
                    self.assertIs(report["motion_ok"], False)
                finally:
                    target.close()

    def test_operator_stop_between_cases(self):
        """A stop the suite never sees in its own calls is caught by the stop counter."""
        class Between(conformance.AdapterTarget):
            fired = False

            def read(self, cap):
                if cap == "base.odometry" and not self.fired and self.runtime is not None:
                    self.fired = True
                    self.runtime.emergency_stop("operator: person in path", source="api")
                return super().read(cap)
        target = Between(SimQuadruped())
        try:
            report = conformance.run(target, "dry")
            self.assertTrue(report["aborted"])
            self.assertIn("非套件来源的停止", report["aborted"])
            self.assertTrue(target.runtime.supervisor.latched)
            self.assertEqual(target.runtime.supervisor.latch_reason, "operator: person in path")
        finally:
            target.close()

    def test_conditional_reset(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(SimQuadruped(), tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        runtime.emergency_stop("suite")
        mine = runtime.supervisor.stop_count
        runtime.emergency_stop("operator")
        with self.assertRaises(RuntimeApiError) as ctx:
            runtime.reset({"hardware_authorized": True, "expect_stop_count": mine})
        self.assertEqual(ctx.exception.code, "stop_count_mismatch")
        self.assertTrue(runtime.supervisor.latched)
        status = runtime.status()
        self.assertEqual((status["stop_count"], status["stop_epoch"]), (mine + 1, runtime.supervisor.epoch))
        runtime.reset({"hardware_authorized": True, "expect_stop_count": mine + 1})
        self.assertFalse(runtime.supervisor.latched)
        with self.assertRaises(RuntimeApiError):
            runtime.reset({"hardware_authorized": True, "expect_stop_count": "1"})


class DryNeverMoves(unittest.TestCase):
    """Q4-02: joint mapping by names and the dry-level adapter motion guard."""

    def test_extra_joint_first_is_blocked_and_nothing_is_sent(self):
        adapter = ArmJointStateExtra()
        report = conformance.run_adapter(adapter, "dry")
        self.assertEqual(adapter.q, [0.3, -0.4, 0.5, -0.2, 0.1, 0.0])
        self.assertFalse([c for c in getattr(adapter, "calls", []) if c[1] == "arm.joint_position"])
        for cid in ("dry.arm.joint_position.zero_displacement", "dry.arm.joint_position.rejects_out_of_limits"):
            c = case(report, cid)
            self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "blocked"), c)
        self.assertIs(report["dry_ok"], False)
        self.assertEqual(report["dry_guard"]["violations"], [])

    def test_extra_joint_first_with_declared_names_maps_by_name(self):      # R4-01
        adapter = ArmJointStateExtra()
        adapter.declare_names = True
        report = conformance.run_adapter(adapter, "dry")
        self.assertIs(report["dry_ok"], True)
        # R4b-02: dry never sends arm targets, even with correctly declared names
        self.assertEqual([c for c in getattr(adapter, "calls", []) if c[1] == "arm.joint_position"], [])
        self.assertEqual(case(report, "dry.arm.joint_position.zero_displacement")["skip_kind"], "not_applicable")
        self.assertEqual(adapter.q, [0.3, -0.4, 0.5, -0.2, 0.1, 0.0])

    def test_unstable_names_are_blocked(self):
        report = conformance.run_adapter(ArmJointStatePermutedNames(), "dry")
        c = case(report, "dry.arm.joint_position.zero_displacement")
        self.assertEqual(c["skip_kind"], "blocked", c)

    def test_guard_refuses_a_suite_defect(self):
        """A (simulated) suite bug that sends a non-zero velocity or a perturbed arm target in dry
        is refused before the adapter, recorded as a suite defect, and nothing moves."""
        class BuggySuite(conformance.Suite):
            def _dry_zero_velocity(self, ev):
                self._ensure_unlatched()
                self.target.command("base.velocity", {"vx": 0.05, "wz": 0.0}, True)

            def _dry_arm_zero(self, ev):
                self._ensure_unlatched()
                q = self._arm_current()
                q[-1] += 0.01
                run = self.target.start_action("arm.joint_position", {"position": q}, True)
                self._wait_run(run, 5.0)
        for adapter, cid in ((SimQuadruped(), "dry.base.velocity.zero_setpoint"),
                             (SimArm(), "dry.arm.joint_position.zero_displacement")):
            with self.subTest(case=cid):
                target = conformance.AdapterTarget(adapter)
                try:
                    report = BuggySuite(target, "dry").run()
                    c = case(report, cid)
                    self.assertEqual(c["status"], "failed", c)
                    self.assertIn("套件缺陷", c["reason"])
                    self.assertTrue(report["dry_guard"]["violations"])
                    self.assertIs(report["dry_ok"], False)
                finally:
                    target.close()
                if isinstance(adapter, SimQuadruped):
                    self.assertEqual((adapter.x, adapter.distance), (0.0, 0.0))
                else:
                    self.assertEqual(adapter.q, [0.0] * 6)

    def test_guard_is_removed_for_motion_and_after_the_suite(self):
        adapter = SimQuadruped()
        target = conformance.AdapterTarget(adapter)
        try:
            conformance.run(target, "dry")
            self.assertNotIn("invoke", adapter.__dict__)
            self.assertNotIn("start_action", adapter.__dict__)
        finally:
            target.close()


class MotionFailuresStopAndLatch(unittest.TestCase):
    def assert_aborted(self, report, cid, needle):
        c = case(report, cid)
        self.assertEqual(c["status"], "failed", c)
        self.assertIn(needle, c["reason"])
        self.assertTrue(report["aborted"])
        self.assertTrue(report["final_latched"])
        self.assertIs(report["motion_ok"], False)

    def test_adapter_error_mid_motion(self):              # Q4-03
        adapter = ArmReadRaisesMidMotion()
        report = run_motion(adapter)
        self.assert_aborted(report, "motion.arm.joint_position.cancel", "joint_states topic lost")
        time.sleep(0.3)
        adapter.advance()
        self.assertLess(adapter.max_wrist, 0.1 + 1e-6)     # never beyond the interrupt goal
        self.assertTrue(adapter.arm_goal is None or adapter.arm_goal.state != "running")

    def test_speed_magnitude_and_direction(self):          # Q4-06
        self.assert_aborted(run_motion(VelocityX5()), "motion.base.velocity.watchdog", "峰值速度")
        self.assert_aborted(run_motion(VelocityReversed()), "motion.base.velocity.watchdog", "反向")

    def test_delayed_resume_after_cancel(self):            # Q4-07
        self.assert_aborted(run_motion(NavCancelResume()), "motion.base.navigate_to.cancel", "又动了")
        self.assert_aborted(run_motion(ArmCancelResume()), "motion.arm.joint_position.cancel", "又动了")

    def test_delayed_resume_after_stop(self):              # Q4-07
        self.assert_aborted(run_motion(NavStopResume()), "motion.base.navigate_to.stop_latency", "停止后")

    def test_reset_replays_old_velocity(self):             # Q4-07
        adapter = ResetReplaysVelocity()
        report = run_motion(adapter)
        self.assertTrue(report["aborted"])
        self.assertIn("复位后", report["aborted"])
        self.assertTrue(report["final_latched"])
        time.sleep(0.3)
        x0 = adapter.read("base.odometry")["pose"][0]
        time.sleep(0.5)
        self.assertEqual(adapter.read("base.odometry")["pose"][0], x0)       # not driving after the suite

    def test_coasting_with_frozen_odometry_is_caught_by_the_hold(self):
        report = run_motion(CoastLying())
        self.assertTrue(report["aborted"])
        self.assertIs(report["motion_ok"], False)

    def test_gripper_state_is_the_feedback(self):         # Q4-11
        self.assert_aborted(run_motion(GripperFakeResult()), "motion.gripper.position.min_move", "gripper.state")

    def test_gripper_interrupt_blocked_without_gripper_state(self):
        class NoGripperState(SimArm):
            def manifest(self):
                m = super().manifest()
                m["capabilities"] = [c for c in m["capabilities"] if c["id"] != "gripper.state"]
                return m
        report = run_motion(NoGripperState())
        for name in ("cancel", "stop_latency"):
            c = case(report, f"motion.gripper.position.{name}")
            self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "blocked"))
        self.assertIs(report["motion_ok"], False)


class NavigationAndTimeScale(unittest.TestCase):
    def test_no_localization_no_goal(self):               # Q4-04
        adapter = NoLocalization()
        report = run_motion(adapter)
        for name in ("min_goal", "cancel", "stop_latency"):
            c = case(report, f"motion.base.navigate_to.{name}")
            self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "blocked"))
            self.assertIn("localization.pose", c["reason"])
        self.assertFalse([c for c in adapter.calls if c[1] == "base.navigate_to"])
        self.assertIs(report["motion_ok"], False)

    def test_lying_localization_is_capped(self):
        """map = odom + (4, 3) m but localization.pose claims odom coordinates: the suite stops
        the body within 0.25 m of travel and aborts (the cap of the minimal goal)."""
        class Offset(SimQuadruped):
            def manifest(self):
                return {**super().manifest(), "simulated": False}

            def start_action(self, cap_id, args):
                g = list(args["goal"])
                g[0] -= 4.0
                g[1] -= 3.0
                return super().start_action(cap_id, dict(args, goal=g))
        adapter = Offset()
        report = run_motion(adapter)
        self.assertTrue(report["aborted"])
        self.assertLess(math.hypot(adapter.x, adapter.y), 0.6)          # watchdog + velocity cases ~0.3 m

    def test_untrusted_time_scale_is_ignored(self):       # Q4-05
        adapter = FakeTimeScaleRealBody()
        report = run_motion(adapter)
        self.assertEqual((report["time_scale"], report["executed_time_scale"]), (20.0, 1.0))
        self.assertIs(report["time_scale_rebased"], False)                # not a trusted simulation
        self.assertEqual(adapter.clock.time_scale, 20.0)                 # never touched
        start_x = 0.0
        for kind, cap, args in adapter.calls:
            if cap == "base.navigate_to":
                self.assertLess(abs(args["goal"][0] - start_x), 1.0)
        nav = [c for c in report["cases"] if c["id"].startswith("motion.base.navigate_to.")]
        for c in nav:
            if c["status"] == "passed" and "goal_distance_m" in c["evidence"]:
                self.assertLessEqual(c["evidence"]["goal_distance_m"], 0.2)

    def test_clock_rebase_is_continuous(self):
        clock = RealClock(20.0)
        time.sleep(0.05)
        before = clock.now()
        clock.set_time_scale(1.0)
        after = clock.now()
        self.assertGreaterEqual(after, before)
        self.assertLess(after - before, 0.05)
        time.sleep(0.1)
        self.assertAlmostEqual(clock.now() - after, 0.1, delta=0.05)
        with self.assertRaises(ValueError):
            clock.set_time_scale(0)


class SamplesAndReports(unittest.TestCase):
    def test_every_sample_is_checked(self):                # Q4-10
        report = conformance.run_adapter(HealthJitterArm(), "dry")
        c = case(report, "dry.system.health.read")
        self.assertEqual(c["status"], "failed")
        self.assertIn("第 1/3 次", c["reason"])

    def test_imu_quaternion_must_be_normalized(self):      # review R6
        c = case(conformance.run_adapter(ImuNotNormalized(), "dry"), "dry.sensor.imu.read")
        self.assertEqual(c["status"], "failed")
        self.assertIn("四元数", c["reason"])

    def test_trust_boundary_is_in_the_report(self):        # Q4-13
        report = conformance.run_adapter(SimArm(), "static")
        self.assertIs(report["trust"]["self_reported"], True)
        self.assertIs(report["trust"]["same_process_as_adapter"], True)
        self.assertIn("同一进程", report["trust"]["note"])
        self.assertIn("自报", report["disclaimer"])

    def test_final_stillness_case(self):
        report = conformance.run_adapter(SimQuadruped(), "dry")
        c = case(report, "dry.safety.stop.final_still")
        self.assertEqual(c["status"], "passed")
        self.assertFalse(report["final_latched"])

    def test_motion_lease_on_a_live_runtime(self):         # Q4-15
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        adapter = SimQuadruped()
        runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        refused, accepted = [], []
        done = threading.Event()

        def client():
            while not done.is_set():
                try:
                    runtime.invoke("base.velocity", {"request_id": uuid.uuid4().hex, "hardware_authorized": True,
                                                     "args": {"vx": 0.3, "wz": 0.0, "timeout_s": 0.3}})
                    accepted.append(1)
                except RuntimeApiError as exc:
                    refused.append(exc.code)
                time.sleep(0.05)
        started = runtime.start_conformance({"request_id": "c1", "level": "dry"})
        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        record = runtime.conformance.wait(started["conformance_id"], 120)
        done.set()
        thread.join(2)
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["report"]["dry_ok"], conformance.summarize(record["report"]))
        self.assertTrue(refused and set(refused) <= {"conformance_running"})
        self.assertEqual(record["report"]["external_motion_refused"], len(refused))
        self.assertIsNone(runtime.conformance_lease)
        self.assertEqual(runtime.invoke("base.velocity", {"request_id": "after", "hardware_authorized": True,
                                                          "args": {"vx": 0.0, "wz": 0.0}}), {"accepted": True})


class ScaffoldInjection(unittest.TestCase):              # Q4-09 (payloads of the review script)
    BASE = {"name": "acme_bot", "vendor": "Acme", "model": "B1", "family": "mobile",
            "capabilities": [
                {"id": "safety.stop", "params": {"hardware_estop": False}, "implementation": "ros2"},
                {"id": "system.health", "implementation": "ros2"},
                {"id": "base.velocity", "params": {"max_vx": 0.5, "max_vy": 0.0, "max_wz": 0.7, "holonomic": False},
                 "implementation": "ros2:/cmd_vel"},
                {"id": "sensor.battery", "implementation": "serial:/dev/ttyUSB0"}]}

    @staticmethod
    def payload(tag):
        return f'x"""\nimport pathlib; pathlib.Path("/tmp/nx_pwned_{tag}").write_text("injected")\n"""'

    def probes(self):
        b = self.BASE
        return {
            "display_name": dict(b, display_name=self.payload("display_name")),
            "vendor": dict(b, vendor=self.payload("vendor")),
            "model": dict(b, model=self.payload("model")),
            "evidence_id": dict(b, capabilities=b["capabilities"][:3] + [
                {"id": "sensor.battery", "implementation": "todo",
                 "evidence_id": 'ev-1\n        import pathlib; pathlib.Path("/tmp/nx_pwned_evidence").write_text("1")'}]),
            "adapter_version": dict(b, adapter_version='0.1"\n[tool.injected]\nx = "1'),
            "hint_quote": dict(b, capabilities=b["capabilities"][:3] + [
                {"id": "sensor.battery", "implementation": "http:http://h/');__import__('os').system('true');('"}]),
            "hint_newline": dict(b, capabilities=b["capabilities"][:3] + [
                {"id": "sensor.battery", "implementation": "ros2:/battery\nimport os"}]),
            "ros2_node": dict(b, ros2={"node_name": 'n"\nimport os\n"', "stale_after_s": 1.0}),
            "ros2_stale": dict(b, ros2={"stale_after_s": 1e9}),
            "ros_topic": dict(b, capabilities=b["capabilities"][:2] + [
                dict(b["capabilities"][2], implementation="ros2:/cmd_vel'+__import__('os').getcwd()+'")]),
            "x_cap": dict(b, capabilities=b["capabilities"] + [{"id": "x.acme.dance\"\nimport os", "implementation": "todo"}]),
            "instance_id": dict(b, instance_id="a b\nimport os"),
        }

    def test_every_injection_is_refused_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, spec in self.probes().items():
                with self.subTest(field=name):
                    out = Path(tmp) / name
                    with self.assertRaises(scaffold.ScaffoldError):
                        scaffold.generate(spec, out)
                    self.assertFalse(out.exists())
                    self.assertFalse([p for p in Path(tmp).iterdir() if ".scaffold-" in p.name])
        self.assertFalse([f for f in os.listdir("/tmp") if f.startswith("nx_pwned_")])

    def test_external_strings_are_never_raw_in_code(self):
        spec = dict(self.BASE, vendor="Acme Robotics (CN)", model="B-1 Mk2", display_name="Acme B1 #3",
                    capabilities=self.BASE["capabilities"][:3] + [
                        {"id": "sensor.battery", "implementation": "http:http://h/api?x=1&y=2", "evidence_id": "ev:12.a"}])
        with tempfile.TemporaryDirectory() as tmp:
            info = scaffold.generate(spec, Path(tmp) / "pkg")
            code = (Path(info["path"]) / "acme_bot" / "adapter.py").read_text(encoding="utf-8")
            self.assertNotIn("Acme Robotics", code)
            self.assertIn("'http:http://h/api?x=1&y=2'", code)      # repr only
            self.assertIn("'ev:12.a'", code)
            init = (Path(info["path"]) / "acme_bot" / "__init__.py").read_text(encoding="utf-8")
            self.assertNotIn("Acme", init)
            meta = _support.load_toml((Path(info["path"]) / "pyproject.toml").read_text(encoding="utf-8"))
            self.assertEqual(sorted(meta), ["build-system", "project", "tool"])
            self.assertIn("Acme Robotics (CN)", meta["project"]["description"])

    def test_structure_check_rejects_unexpected_code(self):
        plan = scaffold.normalize_spec(self.BASE)
        files = {"pyproject.toml": scaffold._render_pyproject(plan), "acme_bot/adapter.json": "{}",
                 "acme_bot/adapter.py": scaffold._render_adapter(plan) + "\nimport os\nos.getcwd()\n"}
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold._validate_generated(files, plan)
        self.assertIn("意外", str(ctx.exception))

    def test_failed_render_leaves_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pkg"
            original = scaffold._render_test
            scaffold._render_test = lambda plan: "def broken(:\n"
            try:
                with self.assertRaises(scaffold.ScaffoldError):
                    scaffold.generate(self.BASE, out)
            finally:
                scaffold._render_test = original
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
