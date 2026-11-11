"""§6.1 capability conformance suite (conformance.report.v1).

* Both simulated bodies pass static, dry and motion with every case passed
  (or not applicable); the ROS 2 adapter passes static and dry (and motion) on
  the rclpy test double.
* Every verdict of the suite is pinned by a faulty body or a faulty runtime
  view that must be caught -- so loosening a threshold, dropping a check or
  recording a failure as a pass makes a test here fail (iter04 mutation run).
* POST/GET /v2/conformance, the black-box HTTP client mode and the CLI.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import _support
from _support import TOKEN, RuntimeFixture, http, wait_until

from neuxmind_runtime import cli, conformance
from neuxmind_runtime.adapters import ros2
from neuxmind_runtime.adapters._sim import SimHandle
from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import Conflict, NotAuthorized, RuntimeApiError

FAST = conformance.THRESHOLDS
# Contract values (INTERFACES §6.1, task): faulty bodies are built against these, never against the
# suite's own thresholds, so loosening a threshold in conformance.py is caught.
CONTRACT_STOP_S = 1.0
CONTRACT_READ_S = 1.0
CONTRACT_WATCHDOG_MARGIN_S = 0.5


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cases(report, status=None, level=None):
    return {c["id"]: c for c in report["cases"]
            if (status is None or c["status"] == status) and (level is None or c["level"] == level)}


def case(report, case_id):
    return cases(report)[case_id]


def assert_all_pass(test, report, level="motion"):
    upto = conformance.LEVELS[: conformance.LEVELS.index(level) + 1]
    bad = [c for c in report["cases"] if c["level"] in upto
           and not (c["status"] == "passed" or (c["status"] == "skipped" and c["skip_kind"] == "not_applicable"))]
    test.assertEqual(bad, [], conformance.summarize(report))
    for lv in conformance.LEVELS[: conformance.LEVELS.index(level) + 1]:
        test.assertIs(report[f"{lv}_ok"], True, lv)


# ------------------------------------------------------------------ faulty bodies
class Unfixed(SimArm):
    def manifest(self):
        m = super().manifest()
        self._n = getattr(self, "_n", 0) + 1
        m["name"] = f"arm #{self._n}"
        return m


class ImplicitSimulated(SimArm):
    def manifest(self):
        m = super().manifest()
        del m["simulated"]
        return m


class RuntimesWithoutInstall(SimArm):
    def manifest(self):
        m = super().manifest()
        m["model_runtimes"] = ["onnxruntime"]
        return m


class MissingParam(SimQuadruped):
    def manifest(self):
        m = super().manifest()
        for c in m["capabilities"]:
            if c["id"] == "base.velocity":
                del c["params"]["holonomic"]
        return m


class BadBattery(SimQuadruped):
    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "sensor.battery":
            out["percent"] = 150.0
        return out


class SlowFirstRead(SimQuadruped):
    def read(self, cap_id):
        if cap_id == "sensor.battery" and not getattr(self, "_slow_done", False):
            self._slow_done = True
            time.sleep(CONTRACT_READ_S + 0.1)
        return super().read(cap_id)


class Unhealthy(SimArm):
    def read(self, cap_id):
        if cap_id == "system.health":
            return {"ok": False, "mode": "fault", "errors": {"E12": "joint 3 overheated"}}
        return super().read(cap_id)


class StopFailsSecondTime(SimArm):
    def stop(self):
        self._stops = getattr(self, "_stops", 0) + 1
        if self._stops == 2:
            raise RuntimeError("serial timeout")
        super().stop()


class SlowStopCall(SimArm):
    def stop(self):
        time.sleep(CONTRACT_STOP_S + 0.2)
        super().stop()


class Coasting(SimQuadruped):
    """stop() does not halt: the base keeps its last setpoint for longer than the stop limit."""
    COAST_S = CONTRACT_STOP_S + 0.4

    def stop(self):
        if getattr(self, "_coast_until", None) is None and self.cmd != (0.0, 0.0, 0.0):
            self._coast_cmd, self._coast_until = self.cmd, time.monotonic() + self.COAST_S
        goal = self.goal
        if goal is not None:
            goal.abort("cancelled", "stopped")
        self.goal = None

    def _step(self, dt):
        until = getattr(self, "_coast_until", None)
        if until is not None:
            if time.monotonic() < until:
                self.cmd = self._coast_cmd
            else:
                self.cmd, self._coast_until = (0.0, 0.0, 0.0), None
        super()._step(dt)


class IgnoresZeroVelocity(SimQuadruped):
    """Accepts a zero setpoint but keeps driving: the watchdog's zero velocity is ignored."""

    def invoke(self, cap_id, args):
        if cap_id == "base.velocity" and not any(args.get(k, 0.0) for k in ("vx", "vy", "wz")):
            return {"accepted": True}
        return super().invoke(cap_id, args)


class _DeafHandle(SimHandle):
    def cancel(self):
        pass


class SlowNavIgnoresCancel(SimQuadruped):
    """Navigation at 30 % speed that keeps going for 1.5 s after cancel/stop."""

    def _navigate(self, goal):
        vx, vy, wz = super()._navigate(goal)
        return vx * 0.3, vy, wz

    def start_action(self, cap_id, args):
        handle = super().start_action(cap_id, args)
        return _DeafHandle(self, handle._goal)

    def stop(self):
        if self.goal is not None and getattr(self, "_late", None) is None:
            self._late = time.monotonic() + 1.5
        self.cmd = (0.0, 0.0, 0.0)

    def _step(self, dt):
        late = getattr(self, "_late", None)
        if late is not None and time.monotonic() >= late:
            self._late = None
            super().stop()
        super()._step(dt)


class StaleJointFeedback(SimArm):
    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "joint.state":
            out["position"] = [0.0] * 6
            out["velocity"] = [0.0] * 6
        return out


class GripperReportsWrongOpening(SimArm):
    def start_action(self, cap_id, args):
        handle = super().start_action(cap_id, args)
        if cap_id == "gripper.position":
            goal = handle._goal
            finish = goal.finish
            goal.finish = lambda result: finish({"opening": result["opening"] - 0.05})
        return handle


class LateZero(SimQuadruped):
    """Honours a zero setpoint only after 1 s (a controller that coasts): watchdog too slow."""

    def invoke(self, cap_id, args):
        if cap_id == "base.velocity" and not any(args.get(k, 0.0) for k in ("vx", "vy", "wz")):
            self._zero_at = time.monotonic() + 1.0
            return {"accepted": True}
        return super().invoke(cap_id, args)

    def _step(self, dt):
        at = getattr(self, "_zero_at", None)
        if at is not None and time.monotonic() >= at:
            self.cmd, self._zero_at = (0.0, 0.0, 0.0), None
        super()._step(dt)


class RejectsZero(SimQuadruped):
    def invoke(self, cap_id, args):
        if cap_id == "base.velocity" and not any(args.get(k, 0.0) for k in ("vx", "vy", "wz")):
            return {"accepted": False}
        return super().invoke(cap_id, args)


class FrozenBase(SimQuadruped):
    """Accepts velocity setpoints but never moves."""

    def _step(self, dt):
        self.cmd = (0.0, 0.0, 0.0)
        super()._step(dt)


class ClaimsReachedWithoutMoving(SimQuadruped):
    def start_action(self, cap_id, args):
        handle = super().start_action(cap_id, args)
        handle._goal.finish({"reached": True})
        self.goal = None
        return handle


class NotSimulated(SimArm):
    def manifest(self):
        return {**super().manifest(), "simulated": False}


# ------------------------------------------------------------------ faulty runtime views
class NoLatchView(conformance.AdapterTarget):
    def latched(self):
        return False


class NoStaleView(conformance.AdapterTarget):
    RESULT = {"rejected_code": None, "stale_event": False}

    def stale_call(self, cap, args, epoch):
        return {**super().stale_call(cap, args, epoch), **self.RESULT}


class WrongStaleCodeView(NoStaleView):
    RESULT = {"rejected_code": "estop_latched"}          # refused, but not as a stale call


class NoStaleEventView(NoStaleView):
    RESULT = {"stale_event": False}


class ArmedView(conformance.AdapterTarget):
    def watchdog_armed(self):
        return True


class AnyResetView(conformance.AdapterTarget):
    def reset(self, authorized=True):
        return super().reset(True)


class LatchIgnoredView(conformance.AdapterTarget):
    def command(self, cap, args, authorized):
        if self.runtime.supervisor.latched and cap == "base.velocity":
            return {"accepted": True}
        return super().command(cap, args, authorized)


class MissingVoltageField(SimQuadruped):
    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "sensor.battery":
            del out["percent"]
        return out


class UnvalidatedReadView(conformance.AdapterTarget):
    """A target whose transport does not validate outputs (e.g. an old or foreign runtime)."""

    def read(self, cap):
        return self.adapter.read(cap)


class PostureIgnoresLatchView(conformance.AdapterTarget):
    def command(self, cap, args, authorized):
        if cap == "legged.posture" and self.runtime.supervisor.latched:
            return {"accepted": True}
        return super().command(cap, args, authorized)


class InstallAlwaysAuthorizedView(conformance.RuntimeTarget):
    def start_action(self, cap, args, authorized, timeout_s=None):
        return super().start_action(cap, args, True, timeout_s)


class PostureWrongCodeView(conformance.AdapterTarget):
    def command(self, cap, args, authorized):
        if cap == "legged.posture" and self.runtime.supervisor.latched:
            raise conformance.CallError("refused", "safety_rejected", 409)
        return super().command(cap, args, authorized)


class InstallSucceedsView(conformance.RuntimeTarget):
    """Pretends the malformed-sha probe installed something (a runtime without input checks)."""

    def start_action(self, cap, args, authorized, timeout_s=None):
        if cap == "model.install" and args.get("sha256") == "not-a-sha":
            return "fake-run"
        return super().start_action(cap, args, authorized, timeout_s)

    def run(self, run_id):
        return {"status": "succeeded"} if run_id == "fake-run" else super().run(run_id)


class OperatorStopsDuringSuite(conformance.AdapterTarget):
    """An operator presses stop while the suite holds its own latch."""

    def stop(self, reason):
        out = super().stop(reason)
        if "dry-stop-1" in reason:
            self.runtime.emergency_stop("operator pressed stop", source="api")
        return out


class SimulatedBodiesPassEveryLevel(unittest.TestCase):
    def test_quadruped(self):
        report = conformance.run_adapter(SimQuadruped(), "motion", True)
        assert_all_pass(self, report)
        motion = cases(report, "passed", "motion")
        for cid in ("motion.base.velocity.watchdog", "motion.base.velocity.stop_latency",
                    "motion.base.navigate_to.min_goal", "motion.base.navigate_to.cancel",
                    "motion.base.navigate_to.stop_latency", "motion.legged.posture.stand_noop"):
            self.assertIn(cid, motion)
        stop = case(report, "motion.base.velocity.stop_latency")["evidence"]
        self.assertLess(stop["still_after_stop_s"], 1.0)
        self.assertLess(stop["stop_call_latency_s"], 1.0)
        watchdog = case(report, "motion.base.velocity.watchdog")["evidence"]
        self.assertLessEqual(watchdog["zeroed_after_s"], 0.3 + 0.5)
        self.assertGreater(watchdog["distance_m"], 0.0)
        self.assertEqual(case(report, "dry.safety.stop.stop_epoch")["evidence"]["rejected_code"], "stale_after_stop")
        self.assertFalse(report["final_latched"])

    def test_arm(self):
        report = conformance.run_adapter(SimArm(), "motion", True)
        assert_all_pass(self, report)
        motion = cases(report, "passed", "motion")
        for cid in ("motion.arm.joint_position.min_move", "motion.arm.joint_position.cancel",
                    "motion.arm.joint_position.stop_latency", "motion.gripper.position.min_move",
                    "motion.gripper.position.cancel", "motion.gripper.position.stop_latency"):
            self.assertIn(cid, motion)
        self.assertTrue(case(report, "motion.arm.joint_position.min_move")["evidence"]["restored"])

    def test_report_shape_and_honesty(self):
        report = conformance.run_adapter(SimArm(), "dry")
        json.dumps(report, allow_nan=False)
        self.assertEqual(report["schema"], "conformance.report.v1")
        self.assertIs(report["simulated"], True)
        self.assertEqual(report["evidence_kind"], "simulation")
        self.assertIs(report["field_acceptance"], False)
        self.assertIn("不等于现场验收证据", report["disclaimer"])
        self.assertIn("不等于现场验收证据", conformance.summarize(report))
        self.assertIsNone(report["motion_ok"])
        self.assertFalse(report["levels"]["motion"]["requested"])
        self.assertEqual(report["thresholds"]["stop_latency_s"], 1.0)
        by_id = {c["id"]: c for c in report["capabilities"]}
        self.assertEqual(set(by_id) >= set(conformance.spec_caps.CAPABILITIES), True)
        self.assertEqual(by_id["arm.joint_position"]["levels"]["dry"]["status"], "passed")
        self.assertEqual(by_id["base.velocity"]["levels"]["static"],
                         {"status": "skipped", "skip_kind": "not_applicable",
                          "reason": "本体未声明该能力（不支持或尚未实现）"})
        self.assertFalse(by_id["base.velocity"]["declared"])
        self.assertEqual(report["robot"]["instance_id"], "sim-arm-01")

    def test_real_body_report_is_still_not_field_evidence(self):
        report = conformance.run_adapter(NotSimulated(), "static")
        self.assertIs(report["simulated"], False)
        self.assertEqual(report["evidence_kind"], "conformance")
        self.assertIs(report["field_acceptance"], False)

    def test_motion_needs_authorization_and_level_is_checked(self):
        with self.assertRaises(NotAuthorized):
            conformance.run_adapter(SimArm(), "motion")
        with self.assertRaises(NotAuthorized):
            conformance.Suite(conformance.AdapterTarget(SimArm()), "motion", authorized=False)
        with self.assertRaises(RuntimeApiError):
            conformance.run_adapter(SimArm(), "fast")

    def test_static_only_never_starts_a_runtime_or_moves(self):
        adapter = SimArm()
        target = conformance.AdapterTarget(adapter)
        report = conformance.run(target, "static")
        self.assertIsNone(target.runtime)
        self.assertTrue(report["static_ok"])
        self.assertEqual(adapter.q, [0.0] * 6)
        self.assertTrue(conformance.check_manifest(SimQuadruped().manifest())["static_ok"])

    def test_time_scaled_simulations_pass_motion(self):
        """Coordinator follow-up: sim bodies must pass at 10x and 20x as well (interruptible windows)."""
        for k, body in ((10.0, SimArm), (20.0, SimArm), (20.0, SimQuadruped)):
            with self.subTest(time_scale=k, body=body.__name__):
                adapter = body(time_scale=k)
                report = conformance.run_adapter(adapter, "motion", True)
                assert_all_pass(self, report)
                # Q4-05: the built-in simulations run the dynamic levels at 1x; nothing is amplified.
                self.assertEqual((report["time_scale"], report["executed_time_scale"]), (k, 1.0))
                self.assertIs(report["time_scale_rebased"], True)
                self.assertEqual(adapter.clock.time_scale, k)                      # restored afterwards
                if body is SimQuadruped:
                    for name in ("cancel", "stop_latency"):
                        self.assertEqual(case(report, f"motion.base.navigate_to.{name}")["evidence"]["goal_distance_m"],
                                         0.2)
                if body is SimArm:
                    for name in ("cancel", "stop_latency"):
                        self.assertAlmostEqual(case(report, f"motion.arm.joint_position.{name}")["evidence"]
                                               ["max_velocity"], 0.2)
                        self.assertEqual(case(report, f"motion.gripper.position.{name}")["evidence"]["run_status"],
                                         "stopped")

    def test_manifest_digests_match_cloud_and_desktop(self):
        import hashlib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(SimQuadruped(), tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        report = conformance.run(conformance.RuntimeTarget(runtime), "static")
        cloud = hashlib.sha256(json.dumps(runtime.manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(report["manifest_digest"], cloud)            # cloud catalog.manifest_digest
        self.assertEqual(report["manifest_v1_digest"], runtime.manifest_v1_digest())   # desktop / brainos
        self.assertNotEqual(cloud, report["manifest_v1_digest"])
        other = conformance.run_adapter(SimQuadruped(instance_id="dog-2"), "static")
        self.assertNotEqual(other["manifest_digest"], cloud)
        self.assertIsNone(conformance.check_manifest({"schema": "x"})["manifest_digest"])

    def test_runtime_native_model_install_dry_refusals(self):
        from neuxmind_runtime.models import ModelConfig
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(SimArm(time_scale=5.0), tmp.name + "/state", sandbox="inprocess",
                              models=ModelConfig(models_dir=tmp.name + "/models", runtimes=("onnxruntime",)))
        self.addCleanup(runtime.close)
        report = conformance.run(conformance.RuntimeTarget(runtime), "dry")
        self.assertTrue(report["dry_ok"], conformance.summarize(report))
        self.assertEqual(case(report, "static.model.install.declaration")["status"], "passed")
        self.assertEqual(case(report, "static.manifest.model_runtimes")["status"], "passed")
        c = case(report, "dry.model.install.refuses_bad_requests")
        self.assertEqual(c["status"], "passed", c)
        self.assertEqual(set(c["evidence"]["probes"]), {"unauthorized", "bad_sha", "metadata_address", "file_scheme"})
        self.assertEqual(c["evidence"]["probes"]["unauthorized"]["status"], 403)
        self.assertEqual(runtime.models.list(), [])
        self.assertEqual(next(x for x in report["capabilities"] if x["id"] == "model.install")["levels"]["dry"]["status"],
                         "passed")
        report = conformance.run(InstallAlwaysAuthorizedView(runtime), "dry")
        c = case(report, "dry.model.install.refuses_bad_requests")
        self.assertEqual(c["status"], "failed", c)
        self.assertIn("未授权", c["reason"])
        self.assertEqual(runtime.models.list(), [])
        c = case(conformance.run(InstallSucceedsView(runtime), "dry"), "dry.model.install.refuses_bad_requests")
        self.assertEqual(c["status"], "failed", c)
        self.assertIn("bad_sha", c["reason"])


class StaticVerdicts(unittest.TestCase):
    def assert_failed(self, adapter, case_id, needle, level="static"):
        report = conformance.run_adapter(adapter, level)
        c = case(report, case_id)
        self.assertEqual(c["status"], "failed", c)
        self.assertIn(needle, c["reason"])
        self.assertIs(report["static_ok"], False)
        if level != "static":
            self.assertIs(report["dry_ok"], False)
            self.assertTrue(all(x["status"] == "skipped" and x["skip_kind"] == "blocked"
                                for x in report["cases"] if x["level"] == "dry"))
        return report

    def test_manifest_must_be_fixed(self):
        self.assert_failed(Unfixed(), "static.manifest.fixed_body", "固定")

    def test_simulated_must_be_explicit(self):
        self.assert_failed(ImplicitSimulated(), "static.manifest.simulated", "显式")

    def test_model_runtimes_need_model_install(self):
        self.assert_failed(RuntimesWithoutInstall(), "static.manifest.model_runtimes", "model_runtimes")

    def test_required_parameter_and_dry_is_blocked(self):
        report = self.assert_failed(MissingParam(), "static.base.velocity.declaration", "holonomic", level="dry")
        self.assertEqual(report["capabilities"][[c["id"] for c in report["capabilities"]].index("base.velocity")]
                         ["levels"]["static"]["status"], "failed")

    def test_invalid_manifest(self):
        class Broken(SimArm):
            def manifest(self):
                m = super().manifest()
                m["capabilities"] = [c for c in m["capabilities"] if c["id"] != "safety.stop"]
                return m
        report = self.assert_failed(Broken(), "static.manifest.schema", "robot.adapter.v2", level="dry")
        self.assertEqual(case(report, "static.manifest.mandatory")["status"], "failed")

    def test_manifest_raising(self):
        class Raises(SimArm):
            def manifest(self):
                raise NotImplementedError("safety.stop 尚未实现")
        report = conformance.check_manifest(Raises().manifest)
        self.assertEqual(case(report, "static.manifest.schema")["status"], "failed")
        self.assertIn("尚未实现", case(report, "static.manifest.schema")["reason"])
        self.assertFalse(report["static_ok"])


class DryVerdicts(unittest.TestCase):
    def run_dry(self, adapter_or_target):
        if isinstance(adapter_or_target, conformance.Target):
            try:
                return conformance.run(adapter_or_target, "dry")
            finally:
                adapter_or_target.close()
        return conformance.run_adapter(adapter_or_target, "dry")

    def assert_case_failed(self, report, case_id, needle):
        c = case(report, case_id)
        self.assertEqual(c["status"], "failed", c)
        self.assertIn(needle, c["reason"])
        self.assertIs(report["dry_ok"], False)
        self.assertEqual(report["levels"]["dry"]["failed"] >= 1, True)
        self.assertEqual(report["summary"]["failed"] >= 1, True)
        cap = next(x for x in report["capabilities"] if x["id"] == c["capability"])
        self.assertEqual(cap["levels"]["dry"]["status"], "failed")

    def test_output_sanity(self):
        self.assert_case_failed(self.run_dry(BadBattery()), "dry.sensor.battery.read", "0–100")

    def test_outputs_are_validated_by_the_suite_itself(self):
        report = self.run_dry(UnvalidatedReadView(MissingVoltageField()))
        self.assert_case_failed(report, "dry.sensor.battery.read", "输出不符合能力标准")

    def test_read_latency(self):
        report = self.run_dry(SlowFirstRead())
        self.assert_case_failed(report, "dry.sensor.battery.read", "读取时延")

    def test_health_must_be_ok(self):
        self.assert_case_failed(self.run_dry(Unhealthy()), "dry.system.health.read", "不健康")

    def test_stop_must_succeed_every_time(self):
        self.assert_case_failed(self.run_dry(StopFailsSecondTime()), "dry.safety.stop.idempotent_latched", "stopped=true")

    def test_stop_call_must_be_fast(self):
        report = self.run_dry(SlowStopCall())
        self.assert_case_failed(report, "dry.safety.stop.idempotent_latched", "停止调用时延")
        self.assertGreater(max(case(report, "dry.safety.stop.idempotent_latched")["evidence"]["stop_latency_s"]), 1.0)

    def test_stop_must_latch(self):
        self.assert_case_failed(self.run_dry(NoLatchView(SimArm())), "dry.safety.stop.idempotent_latched", "未锁存")

    def test_latch_must_refuse_motion(self):
        self.assert_case_failed(self.run_dry(LatchIgnoredView(SimQuadruped())),
                                "dry.safety.stop.latch_blocks_motion", "仍被接受")

    def test_reset_needs_authorization(self):
        self.assert_case_failed(self.run_dry(AnyResetView(SimArm())),
                                "dry.safety.stop.latch_blocks_motion", "未授权的 reset 被接受")

    def test_stop_epoch(self):
        self.assert_case_failed(self.run_dry(NoStaleView(SimQuadruped())), "dry.safety.stop.stop_epoch", "未被作废")
        self.assert_case_failed(self.run_dry(WrongStaleCodeView(SimQuadruped())), "dry.safety.stop.stop_epoch",
                                "estop_latched")
        self.assert_case_failed(self.run_dry(NoStaleEventView(SimQuadruped())), "dry.safety.stop.stop_epoch",
                                "stale_rejected")

    def test_posture_dry_check(self):
        report = self.run_dry(SimQuadruped())
        c = case(report, "dry.legged.posture.refused_without_motion")
        self.assertEqual(c["status"], "passed", c)
        self.assertEqual((c["evidence"]["invalid_value"]["status"], c["evidence"]["latched"]["code"]), (400, "estop_latched"))
        posture = next(x for x in report["capabilities"] if x["id"] == "legged.posture")
        self.assertEqual(posture["levels"]["dry"]["status"], "passed")
        self.assert_case_failed(self.run_dry(PostureIgnoresLatchView(SimQuadruped())),
                                "dry.legged.posture.refused_without_motion", "锁存期间姿态指令被接受")
        self.assert_case_failed(self.run_dry(PostureWrongCodeView(SimQuadruped())),
                                "dry.legged.posture.refused_without_motion", "safety_rejected")

    def test_zero_setpoint_must_be_accepted(self):
        self.assert_case_failed(self.run_dry(RejectsZero()), "dry.base.velocity.zero_setpoint", "未被接受")

    def test_zero_setpoint_must_not_arm_the_watchdog(self):
        self.assert_case_failed(self.run_dry(ArmedView(SimQuadruped())), "dry.base.velocity.zero_setpoint", "看门狗")

    def test_dry_never_moves(self):
        adapter = SimQuadruped()
        arm = SimArm()
        for a in (adapter, arm):
            self.assertTrue(self.run_dry(a)["dry_ok"])
        self.assertEqual((adapter.x, adapter.y, adapter.yaw, adapter.distance), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(arm.q, [0.0] * 6)
        self.assertEqual(arm.opening, 1.0)
        with self.assertRaises(AssertionError):
            conformance._assert_zero({"vx": 0.1})

    def test_prelatched_runtime_is_left_to_the_operator(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(SimQuadruped(), tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        runtime.emergency_stop("operator: person in the cell")
        stops = runtime.supervisor.stop_count
        report = conformance.run(conformance.RuntimeTarget(runtime), "dry")
        self.assertTrue(report["prelatched"])
        self.assertIs(report["dry_ok"], False)
        dry = [c for c in report["cases"] if c["level"] == "dry"]
        self.assertTrue(dry and all((c["status"], c["skip_kind"]) == ("skipped", "blocked") for c in dry))
        self.assertEqual(runtime.supervisor.stop_count, stops)        # Q4-01: not even another stop
        self.assertTrue(runtime.supervisor.latched)
        self.assertEqual(runtime.supervisor.latch_reason, "operator: person in the cell")
        self.assertTrue(report["final_latched"])

    def test_external_stop_aborts_and_stays_latched(self):
        target = OperatorStopsDuringSuite(SimQuadruped())
        try:
            report = conformance.run(target, "dry")
            self.assertTrue(report["aborted"])
            self.assertIs(report["dry_ok"], False)
            self.assertTrue(target.runtime.supervisor.latched)
            self.assertEqual(target.runtime.supervisor.latch_reason, "operator pressed stop")
            blocked = [c for c in report["cases"] if c.get("skip_kind") == "blocked"]
            self.assertTrue(blocked)
        finally:
            target.close()

    def test_refuses_while_runs_are_active(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(SimQuadruped(), tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        run = runtime.invoke("base.navigate_to", {"request_id": "nav", "hardware_authorized": True,
                                                  "args": {"goal": [5.0, 0, 0, 0, 0, 0, 1]}})
        self.addCleanup(runtime.emergency_stop, "teardown")
        with self.assertRaises(Conflict) as ctx:
            conformance.run(conformance.RuntimeTarget(runtime), "dry")
        self.assertEqual(ctx.exception.code, "runs_active")
        self.assertEqual(runtime.runner.get(run["run_id"])["status"], "running")


class MotionVerdicts(unittest.TestCase):
    def run_motion(self, adapter):
        report = conformance.run_adapter(adapter, "motion", True)
        self.assertEqual((report["thresholds"]["stop_latency_s"], report["thresholds"]["cancel_latency_s"],
                          report["thresholds"]["watchdog_margin_s"]), (CONTRACT_STOP_S, 1.0, CONTRACT_WATCHDOG_MARGIN_S))
        return report

    def assert_aborted_latched(self, report, case_id, needle):
        c = case(report, case_id)
        self.assertEqual(c["status"], "failed", c)
        self.assertIn(needle, c["reason"])
        self.assertTrue(report["aborted"])
        self.assertTrue(report["final_latched"])
        self.assertIs(report["motion_ok"], False)
        self.assertTrue(report["dry_ok"])

    def test_body_that_does_not_stop_in_time(self):
        report = self.run_motion(Coasting())
        self.assert_aborted_latched(report, "motion.base.velocity.stop_latency", "未静止")
        later = case(report, "motion.base.navigate_to.min_goal")
        self.assertEqual((later["status"], later["skip_kind"]), ("skipped", "blocked"))
        self.assertEqual(case(report, "motion.base.velocity.watchdog")["status"], "passed")

    def test_watchdog_too_slow(self):
        report = self.run_motion(LateZero())
        c = case(report, "motion.base.velocity.watchdog")
        self.assertEqual(c["status"], "failed", c)
        self.assertIn("看门狗归零用时", c["reason"])
        self.assertGreater(c["evidence"]["zeroed_after_s"], 0.3 + CONTRACT_WATCHDOG_MARGIN_S)

    def test_motion_must_be_visible_in_feedback(self):
        report = self.run_motion(FrozenBase())
        c = case(report, "motion.base.velocity.watchdog")
        self.assertEqual(c["status"], "failed", c)
        self.assertIn("未观察到运动", c["reason"])

    def test_navigation_must_really_move(self):
        report = self.run_motion(ClaimsReachedWithoutMoving())
        c = case(report, "motion.base.navigate_to.min_goal")
        self.assertEqual(c["status"], "failed", c)
        self.assertIn("里程计位移", c["reason"])

    def test_watchdog_zero_ignored(self):
        report = self.run_motion(IgnoresZeroVelocity())
        self.assert_aborted_latched(report, "motion.base.velocity.watchdog", "看门狗")

    def test_cancel_ignored(self):
        report = self.run_motion(SlowNavIgnoresCancel())
        self.assertEqual(case(report, "motion.base.navigate_to.min_goal")["status"], "passed")
        self.assert_aborted_latched(report, "motion.base.navigate_to.cancel", "取消")

    def test_joint_feedback_must_follow(self):
        report = self.run_motion(StaleJointFeedback())
        c = case(report, "motion.arm.joint_position.min_move")
        self.assertEqual(c["status"], "failed", c)
        self.assertIn("joint.state", c["reason"])
        self.assertIs(report["motion_ok"], False)

    def test_gripper_feedback_must_match(self):
        report = self.run_motion(GripperReportsWrongOpening())
        c = case(report, "motion.gripper.position.min_move")
        self.assertEqual(c["status"], "failed", c)
        self.assertIn("反馈开度", c["reason"])


class Ros2OnTestDouble(unittest.TestCase):
    """ROS 2 bridge against the rclpy test double (NOT ROS 2): static and dry must pass."""

    def test_ros2_adapter_static_dry_and_motion(self):
        with _support.ros2_double() as graph:
            adapter = ros2.Ros2Adapter(_support.ros2_config())
            self.assertTrue(adapter.manifest()["simulated"])          # the double reports use_sim_time
            report = conformance.run_adapter(adapter, "motion", True)
            assert_all_pass(self, report, "dry")
            self.assertTrue(report["simulated"])
            for cid in ("dry.base.odometry.read", "dry.sensor.battery.read", "dry.system.health.read",
                        "dry.base.velocity.zero_setpoint", "dry.safety.stop.stop_epoch",
                        "dry.base.navigate_to.rejects_invalid_goal", "motion.base.velocity.watchdog",
                        "motion.base.velocity.stop_latency", "motion.safety.stop.final_still"):
                self.assertEqual(case(report, cid)["status"], "passed", cid)
            # Q4-04: Ros2Adapter has no localization.pose -> navigation motion is blocked, not guessed.
            for name in ("min_goal", "cancel", "stop_latency"):
                c = case(report, f"motion.base.navigate_to.{name}")
                self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "blocked"))
            self.assertIs(report["motion_ok"], False)
            last = graph.published["/cmd_vel"][-1]
            self.assertEqual((last.linear.x, last.linear.y, last.angular.z), (0.0, 0.0, 0.0))
            robot = graph.robot()
            self.assertEqual(robot.goals, [])
            self.assertEqual(robot.velocity, (0.0, 0.0))

    def test_stale_topics_make_health_fail(self):
        with _support.ros2_double() as graph:
            robot = graph.robot()
            robot._stop.set()                                # the robot stops publishing
            robot.thread.join(1)
            config = _support.ros2_config()
            config["stale_after_s"] = 0.2
            adapter = ros2.Ros2Adapter(config)
            report = conformance.run_adapter(adapter, "dry", thresholds=conformance.Thresholds(warmup_s=0.3))
            self.assertEqual(case(report, "dry.system.health.read")["status"], "failed")
            self.assertEqual(case(report, "dry.base.odometry.read")["status"], "failed")
            self.assertFalse(report["dry_ok"])


class HttpConformance(unittest.TestCase):
    def setUp(self):
        self.fx = RuntimeFixture("quadruped", time_scale=1.0, http=True, sandbox="inprocess")

    def tearDown(self):
        self.fx.close()

    def start(self, level, authorized=False, rid=None):
        return http(self.fx.url, "/v2/conformance", {"request_id": rid or f"cf-{time.monotonic_ns()}",
                                                     "level": level, "hardware_authorized": authorized})

    def wait(self, cid):
        return wait_until(lambda: (lambda r: r if r["status"] != "running" else None)(
            http(self.fx.url, "/v2/conformance/" + cid)[1]), 60, 0.05)

    def test_dry_and_motion_on_the_live_runtime(self):
        status, body = self.start("dry")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["status"], body["simulated"]), ("running", True))
        record = self.wait(body["conformance_id"])
        self.assertEqual(record["status"], "completed", record)
        assert_all_pass(self, record["report"], "dry")
        self.assertEqual(record["report"]["target"]["kind"], "runtime")
        self.assertEqual(record["progress"], 1.0)
        self.assertFalse(self.fx.runtime.supervisor.latched)       # the suite released its own latch
        status, body = self.start("motion")
        self.assertEqual((status, body["code"]), (403, "hardware_not_authorized"))
        status, body = self.start("motion", True)
        self.assertEqual(status, 200, body)
        record = self.wait(body["conformance_id"])
        assert_all_pass(self, record["report"], "motion")
        listing = http(self.fx.url, "/v2/conformance")[1]["conformance"]
        self.assertEqual([r["motion_ok"] for r in listing][:1], [True])
        events = [e["type"] for e in self.fx.runtime.events.since(0, 10 ** 6)]
        self.assertIn("conformance.started", events)
        self.assertIn("conformance.finished", events)

    def test_one_at_a_time_idempotent_and_unknown(self):
        status, first = self.start("dry", rid="same")
        self.assertEqual(status, 200)
        self.assertEqual(self.start("dry", rid="same")[1]["conformance_id"], first["conformance_id"])
        status, busy = self.start("dry")
        self.assertEqual((status, busy["code"]), (409, "conformance_busy"))
        self.wait(first["conformance_id"])
        status, body = http(self.fx.url, "/v2/conformance/cf-nope")
        self.assertEqual((status, body["code"]), (404, "conformance_not_found"))
        status, body = self.start("everything")
        self.assertEqual((status, body["code"]), (400, "invalid_level"))
        status, body = http(self.fx.url, "/v2/conformance", {"level": "static"})
        self.assertEqual((status, body["code"]), (400, "request_id_required"))

    def test_black_box_client_mode(self):
        report = conformance.run(conformance.HttpTarget(self.fx.url, TOKEN), "dry")
        self.assertIs(report["dry_ok"], False)                     # cannot be certified as a black box
        blocked = {c["id"] for c in report["cases"] if c.get("skip_kind") == "blocked"}
        # No guard between a foreign runtime and its adapter: probes that rely on the runtime
        # refusing them are not sent (Q4-16), nor is the stale-epoch probe.
        self.assertEqual(blocked, {"dry.safety.stop.stop_epoch", "dry.base.navigate_to.rejects_invalid_goal",
                                   "dry.legged.posture.refused_without_motion"})
        self.assertFalse([c for c in report["cases"] if c["status"] == "failed"], conformance.summarize(report))
        self.assertFalse(report["dry_guard"]["active_during_dry"])
        self.assertFalse(self.fx.runtime.supervisor.latched)

    def test_running_record_becomes_unknown_after_restart(self):
        self.fx.runtime.store.put("conformance", {"id": "cf-old", "status": "running", "level": "dry",
                                                   "created_at": time.time(), "report": None})
        runtime = self.fx.runtime
        runtime.conformance.recover_after_restart()
        self.assertEqual(runtime.get_conformance("cf-old")["status"], "unknown")


class ConformanceCli(unittest.TestCase):
    def run_cli(self, *args, env=None, timeout=90):
        return subprocess.run([sys.executable, "-m", "neuxmind_runtime", "conformance", *args],
                              cwd=str(_support.RUNTIME_DIR), capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})

    def test_adapter_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            proc = self.run_cli("--adapter", "sim_quadruped", "--level", "dry", "--out", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = read_json(out)
            self.assertTrue(report["dry_ok"])
            self.assertIn("不等于现场验收证据", proc.stderr)
        proc = self.run_cli("--adapter", "sim_arm", "--level", "motion")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--authorize", proc.stderr)
        proc = self.run_cli("--adapter", f"{__name__}:BadBattery", "--level", "dry",
                            env={"PYTHONPATH": os.pathsep.join([str(_support.RUNTIME_DIR / "tests"),
                                                                str(_support.RUNTIME_DIR)])})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("dry.sensor.battery.read", proc.stderr)

    def test_ros2_adapter_on_the_double(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "ros2.json")
            with open(config, "w", encoding="utf-8") as fh:
                json.dump({"limits": {"max_vx": 0.8, "max_wz": 1.0}}, fh)
            proc = self.run_cli("--adapter", "ros2", "--level", "dry",
                                env={"NEUXMIND_ROS2_CONFIG": config,
                                     "PYTHONPATH": os.pathsep.join([str(_support.ROS2_DOUBLE), str(_support.RUNTIME_DIR)])})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertTrue(report["dry_ok"] and report["simulated"])

    def test_url_server_and_client_modes(self):
        fx = RuntimeFixture("arm", time_scale=1.0, http=True, sandbox="inprocess")
        self.addCleanup(fx.close)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.json")
            rc = cli.main(["conformance", "--url", fx.url, "--token", TOKEN, "--level", "motion", "--authorize",
                           "--out", out])
            self.assertEqual(rc, 0)
            report = read_json(out)
            self.assertTrue(report["motion_ok"])
            rc = cli.main(["conformance", "--url", fx.url, "--token", TOKEN, "--mode", "client", "--level", "dry",
                           "--out", out])
            self.assertEqual(rc, 1)                     # stop epoch is blocked in black-box mode
            self.assertEqual(cli.main(["conformance", "--level", "dry"]), 2)          # neither --adapter nor --url


if __name__ == "__main__":
    unittest.main()
