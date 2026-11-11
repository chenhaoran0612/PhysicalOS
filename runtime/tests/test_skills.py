"""Skill install/compatibility, cross-embodiment demo and run lifecycle."""
import tempfile
import threading
import time
import unittest
from unittest import mock

import _support
from _support import RuntimeFixture, custom_package, example, wait_until

from neuxmind_runtime import runner as runner_module
from neuxmind_runtime.clock import RealClock
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import (BadRequest, Conflict, EStopLatched, Incompatible, NotAuthorized,
                                     RuntimeApiError)
from neuxmind_runtime.hal import ActionHandle, ActionStatus, RobotAdapter
from neuxmind_runtime.skills import SkillContext, check_parameters

LONG_SLEEP = """
def run(ctx, seconds=1000.0):
    ctx.log("sleeping")
    ctx.sleep(seconds)
    return {"slept": seconds}
"""

FAILS = """
def run(ctx):
    raise ValueError("boom")
"""

IGNORES_STOP = """
import time
def run(ctx):
    while True:
        try:
            time.sleep(0.01)
        except BaseException:
            pass
"""

UNDECLARED = """
def run(ctx):
    return ctx.read("sensor.battery")
"""


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.quad = RuntimeFixture("quadruped")
        self.arm = RuntimeFixture("arm")

    def tearDown(self):
        self.quad.close()
        self.arm.close()

    def test_cross_embodiment(self):
        # status_report needs only system.health: installs and runs on both bodies.
        for fx in (self.quad, self.arm):
            info = fx.install(example("status_report"))
            self.assertTrue(info["compatibility"]["compatible"])
            record = fx.wait(fx.run("status_report"))
            self.assertEqual(record["status"], "succeeded", record["error"])
            self.assertTrue(record["result"]["simulated"])
        quad_report = self.quad.wait(self.quad.run("status_report"))["result"]
        arm_report = self.arm.wait(self.arm.run("status_report"))["result"]
        self.assertIn("base.odometry", quad_report["available"])
        self.assertIn("joint.state", arm_report["available"])

        # patrol needs base.navigate_to: accepted on the quadruped, rejected on the arm.
        self.assertTrue(self.quad.install(example("patrol"))["compatibility"]["compatible"])
        with self.assertRaises(Incompatible) as ctx:
            self.arm.install(example("patrol"))
        self.assertIn("base.navigate_to", ctx.exception.extra["compatibility"]["missing"])
        self.assertEqual(self.arm.runtime.skills.list(), [s for s in self.arm.runtime.skills.list()
                                                          if s["id"] != "patrol"])
        # wave is the reverse.
        self.assertTrue(self.arm.install(example("wave"))["compatibility"]["compatible"])
        with self.assertRaises(Incompatible):
            self.quad.install(example("wave"))

    def test_constraint_violation_rejected(self):
        package = example("wave")
        package["id"] = "wave_big"
        package["constraints"] = {"arm.joint_position": {"dof": {"min": 7}}}
        with self.assertRaises(Incompatible) as ctx:
            self.arm.install(package)
        self.assertTrue(ctx.exception.extra["compatibility"]["violations"])

    def test_versions_are_immutable(self):
        self.quad.install(custom_package("demo", "def run(ctx):\n    return {'a': 1}\n"))
        again = self.quad.install(custom_package("demo", "def run(ctx):\n    return {'a': 1}\n"))
        self.assertTrue(again["already_installed"])
        with self.assertRaises(Conflict):
            self.quad.install(custom_package("demo", "def run(ctx):\n    return {'a': 2}\n"))

    def test_bad_packages_rejected(self):
        bad_path = custom_package("evil", "def run(ctx): pass\n")
        bad_path["files"] = {"../escape.py": "x", "skill.py": "def run(ctx): pass\n"}
        with self.assertRaises(BadRequest):
            self.quad.install(bad_path)
        no_entry = custom_package("noentry", "def run(ctx): pass\n")
        no_entry["entry"] = "missing.py:run"
        with self.assertRaises(BadRequest):
            self.quad.install(no_entry)
        unknown_cap = custom_package("unknowncap", "def run(ctx): pass\n", requires=("teleport.now",))
        with self.assertRaises(BadRequest):
            self.quad.install(unknown_cap)


class RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fx = RuntimeFixture("quadruped", time_scale=20.0)
        self.fx.install(example("patrol"))

    def tearDown(self):
        self.fx.close()

    def test_patrol_succeeds_and_reaches_waypoints(self):
        record = self.fx.wait(self.fx.run("patrol", {"waypoints": [[2, 0], [2, 2]], "loops": 2}), 20)
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual(record["result"]["reached"], 4)
        x, y = record["result"]["final_pose"][:2]
        self.assertLess(abs(x - 2) + abs(y - 2), 0.4)
        self.assertEqual(record["progress"], 1.0)
        self.assertTrue(any(e["type"] == "log" for e in record["events"]))

    def test_run_requires_authorization_and_valid_parameters(self):
        with self.assertRaises(NotAuthorized):
            self.fx.runtime.run_skill("patrol", {"request_id": "na", "parameters": {"waypoints": [[1, 0]]}})
        with self.assertRaises(BadRequest):
            self.fx.run("patrol", {"waypoints": [[1, 0]], "speed": 9})
        with self.assertRaises(BadRequest):
            self.fx.run("patrol", {})               # waypoints missing

    def test_cancel(self):
        run_id = self.fx.run("patrol", {"waypoints": [[150, 0]]})
        wait_until(lambda: self.fx.runtime.bus.read("base.odometry")["pose"][0] > 0.2)
        self.assertTrue(self.fx.runtime.runner.cancel(run_id)["accepted"])
        record = self.fx.wait(run_id, 3)
        self.assertEqual(record["status"], "stopped")
        self.assertEqual(record["stop_reason"], "cancelled")
        self.assertEqual(self.fx.adapter.cmd, (0.0, 0.0, 0.0))
        self.assertIsNone(self.fx.adapter.goal)
        self.assertFalse(self.fx.runtime.supervisor.latched)     # cancel does not latch

    def test_timeout(self):
        self.fx.install(custom_package("sleeper", LONG_SLEEP, parameters=[
            {"name": "seconds", "type": "float", "default": 1000.0}], timeout_s=1.0))
        started = time.monotonic()
        record = self.fx.wait(self.fx.run("sleeper"), 5)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "运行超时")
        self.assertLess(time.monotonic() - started, 2.0)   # 1 s sim at 20x

    def test_timeout_override_cannot_extend_package_limit(self):
        self.fx.install(custom_package("sleeper2", LONG_SLEEP, parameters=[
            {"name": "seconds", "type": "float", "default": 1000.0}], timeout_s=2.0))
        record = self.fx.wait(self.fx.run("sleeper2", timeout_s=1e6), 5)
        self.assertEqual(record["error"], "运行超时")

    def test_failure_is_reported(self):
        self.fx.install(custom_package("fails", FAILS))
        record = self.fx.wait(self.fx.run("fails"))
        self.assertEqual(record["status"], "failed")
        self.assertIn("boom", record["error"])
        self.assertTrue(any(e["type"] == "exception" for e in record["events"]))

    def test_undeclared_capability_refused(self):
        self.fx.install(custom_package("sneaky", UNDECLARED))
        record = self.fx.wait(self.fx.run("sneaky"))
        self.assertEqual(record["status"], "failed")
        self.assertIn("未在 requires/optional 中声明", record["error"])

    def test_one_motion_owner_at_a_time(self):
        first = self.fx.run("patrol", {"waypoints": [[150, 0]]})
        with self.assertRaises(Conflict):
            self.fx.run("patrol", {"waypoints": [[1, 0]]})
        # Non-motion skills may run alongside.
        self.fx.install(example("status_report"))
        self.assertEqual(self.fx.wait(self.fx.run("status_report"))["status"], "succeeded")
        self.fx.runtime.runner.cancel(first)
        self.assertEqual(self.fx.wait(first, 3)["status"], "stopped")

    def test_global_stop_latches_and_blocks_motion_skills(self):
        run_id = self.fx.run("patrol", {"waypoints": [[150, 0]]})
        wait_until(lambda: self.fx.runtime.runner.get(run_id)["status"] == "running")
        self.fx.runtime.emergency_stop("test")
        self.assertEqual(self.fx.wait(run_id, 3)["status"], "stopped")
        with self.assertRaises(EStopLatched):
            self.fx.run("patrol", {"waypoints": [[1, 0]]})
        self.fx.install(example("status_report"))      # non-motion still allowed
        self.assertEqual(self.fx.wait(self.fx.run("status_report"))["status"], "succeeded")
        self.fx.runtime.reset({"hardware_authorized": True})
        self.assertEqual(self.fx.wait(self.fx.run("patrol", {"waypoints": [[1, 0]]}), 10)["status"], "succeeded")

    def test_skill_calling_safety_stop_latches(self):
        self.fx.install(custom_package("panic", "def run(ctx):\n    ctx.call('safety.stop', reason='x')\n"
                                       "    ctx.check_stop()\n"))
        record = self.fx.wait(self.fx.run("panic"))
        self.assertEqual(record["status"], "stopped")
        self.assertTrue(self.fx.runtime.supervisor.latched)

    def test_uncooperative_skill_is_revoked_after_grace(self):
        self.fx.install(custom_package("stubborn", IGNORES_STOP))
        with mock.patch.object(runner_module, "STOP_GRACE_S", 0.2):
            run_id = self.fx.run("stubborn")
            wait_until(lambda: self.fx.runtime.runner.get(run_id)["status"] == "running")
            self.fx.runtime.runner.cancel(run_id)
            record = wait_until(lambda: (r := self.fx.runtime.runner.get(run_id))["status"] != "running" and r, 3)
        self.assertEqual(record["status"], "stopped")
        self.assertIn("能力访问已撤销", record["events"][-1]["note"])


STUBBORN_CALLER = """
import time
def run(ctx):
    while True:
        try:
            ctx.log("tick")
            ctx.progress(0.5, "still here")
            ctx.call("legged.posture", posture="sit")
        except BaseException:
            pass
        time.sleep(0.01)
"""


class RevocationTests(unittest.TestCase):
    """After the grace period a stubborn run is revoked: no capability access, record frozen (mutant R7)."""

    def test_revoked_run_cannot_call_or_mutate_its_record(self):
        fx = RuntimeFixture("quadruped", time_scale=20.0)
        try:
            fx.install(custom_package("stubborn_caller", STUBBORN_CALLER, requires=("legged.posture",)))
            with mock.patch.object(runner_module, "STOP_GRACE_S", 0.2):
                run_id = fx.run("stubborn_caller")
                wait_until(lambda: fx.runtime.runner.get(run_id)["status"] == "running")
                fx.runtime.runner.cancel(run_id)
                record = wait_until(lambda: (r := fx.runtime.runner.get(run_id))["status"] != "running" and r, 3)
            self.assertEqual(record["status"], "stopped")
            self.assertTrue(record["capability_access_revoked"])
            frozen = len(record["events"])
            calls = []
            original = fx.adapter.invoke
            fx.adapter.invoke = lambda cap, args: (calls.append(cap), original(cap, args))[1]
            time.sleep(0.3)                     # the thread is still looping, calling ctx.*
            after = fx.runtime.runner.get(run_id)
            self.assertEqual(len(after["events"]), frozen, after["events"][-3:])
            self.assertEqual(calls, [])
        finally:
            fx.close()

    def test_revoked_context_refuses_calls_directly(self):
        fx = RuntimeFixture("quadruped")
        try:
            run = runner_module.RunState("skill", "x", True, True, {})
            run.epoch = fx.runtime.supervisor.epoch
            ctx = SkillContext(run, fx.runtime.bus, {"legged.posture"}, {}, fx.runtime.emergency_stop)
            run.revoked = True
            with self.assertRaises(BaseException) as raised:
                ctx.call("legged.posture", posture="sit")
            self.assertEqual(type(raised.exception).__name__, "SkillStopped")
            self.assertEqual(fx.adapter.posture, "stand")
        finally:
            fx.close()


class _BlockingReadArm(RobotAdapter):
    """joint.state reads block until released (stale ROS topic / serial timeout)."""

    def __init__(self):
        self.clock = RealClock()
        self.release = threading.Event()
        self.reading = threading.Event()
        self.started = []

    def manifest(self):
        return {"schema": "robot.adapter.v2", "instance_id": "slow-arm", "name": "slow", "adapter_version": "0.1.0",
                "family": "manipulator", "simulated": True,
                "capabilities": [{"id": "safety.stop"}, {"id": "system.health"},
                                 {"id": "joint.state", "params": {"dof": 2}},
                                 {"id": "arm.joint_position", "params": {"dof": 2, "lower": [-3, -3],
                                                                         "upper": [3, 3], "max_velocity": 1.0,
                                                                         "joint_names": ["a", "b"]}},
                                 {"id": "policy.action_chunk", "params": {"action_space": "joint_position",
                                                                          "dim": 2, "rate_hz": 10.0}}]}

    def read(self, cap):
        if cap == "joint.state":
            self.reading.set()
            self.release.wait(10)
            return {"names": ["a", "b"], "position": [0.0, 0.0]}
        return {"ok": True, "mode": "x", "errors": {}}

    def invoke(self, cap, args):
        self.started.append(("invoke", cap))
        return {"accepted": True}

    def start_action(self, cap, args):
        self.started.append(("start_action", cap))

        class H(ActionHandle):
            def poll(self_inner):
                return ActionStatus(state="running")

            def cancel(self_inner):
                pass
        return H()

    def stop(self):
        pass


class StaleAfterResetTests(unittest.TestCase):
    """R-01: an in-flight call of a stopped+revoked run must never reach the adapter, even after reset."""

    def test_in_flight_call_dropped_after_stop_and_reset(self):
        adapter = _BlockingReadArm()
        with tempfile.TemporaryDirectory() as state:
            rt = EdgeRuntime(adapter, state)
            try:
                code = "def run(ctx):\n    ctx.call('policy.action_chunk', actions=[[0.1, 0.1]], dt=0.1)\n"
                rt.install_skill({"request_id": "i", "package": custom_package(
                    "stale", code, requires=("policy.action_chunk", "joint.state", "arm.joint_position"))})
                with mock.patch.object(runner_module, "STOP_GRACE_S", 0.2):
                    run_id = rt.run_skill("stale", {"request_id": "r", "hardware_authorized": True})["run_id"]
                    self.assertTrue(adapter.reading.wait(5))
                    self.assertTrue(rt.emergency_stop("operator pressed stop")["latched"])
                    wait_until(lambda: rt.runner.get(run_id)["status"] == "stopped", 5)
                self.assertEqual(rt.runner.active_runs(), [])
                self.assertFalse(rt.reset({"hardware_authorized": True})["latched"])
                adapter.release.set()               # the blocked read returns now
                run = rt.runner._runs[run_id]
                run.thread.join(5)
                self.assertFalse(run.thread.is_alive())
                self.assertEqual(adapter.started, [], "被撤销运行的在途指令在 reset 后到达了本体")
                rejected = [e for e in rt.events.since(0) if e["type"] == "safety.stale_rejected"]
                self.assertTrue(rejected)
            finally:
                adapter.release.set()
                rt.close()


class ParameterValidationTests(unittest.TestCase):
    """R-12: type first, non-finite rejected, and pre-effect failures are rejections (not unknown)."""

    DECL = [{"name": "speed", "type": "float", "default": 0.2}, {"name": "goal", "type": "vector", "length": 2}]

    def test_check_parameters(self):
        for bad in ("ab", [["speed", 0.2]], 5):
            with self.subTest(bad=bad), self.assertRaises(BadRequest):
                check_parameters(self.DECL, bad)
        for bad in ({"speed": float("nan")}, {"speed": float("inf")}, {"goal": [0.0, float("nan")]}):
            with self.subTest(bad=bad), self.assertRaises(BadRequest):
                check_parameters(self.DECL, bad)
        self.assertEqual(check_parameters(self.DECL, None), {"speed": 0.2})

    def test_bad_parameters_are_recorded_as_rejected_not_unknown(self):
        fx = RuntimeFixture("quadruped")
        try:
            fx.install(example("patrol"))
            payload = {"request_id": "bad-params", "parameters": "ab", "hardware_authorized": True}
            for _ in range(2):            # the retry gets the same answer, not outcome_unknown
                with self.assertRaises(RuntimeApiError) as ctx:
                    fx.runtime.run_skill("patrol", payload)
                self.assertEqual((ctx.exception.status, ctx.exception.code), (400, "bad_request"))
            self.assertEqual(fx.runtime.store.get("requests", "bad-params")["status"], "rejected")
        finally:
            fx.close()

    def test_conflicting_file_and_directory_paths_rejected(self):
        fx = RuntimeFixture("quadruped")
        try:
            package = custom_package("conflict", "def run(ctx): pass\n")
            package["files"]["skill.py/b.py"] = "x = 1\n"
            with self.assertRaises(BadRequest):
                fx.install(package)
        finally:
            fx.close()


class ActionTimeoutTests(unittest.TestCase):
    """R-11: actions invoked directly over v2 get a capability-level default timeout."""

    def test_default_and_shortened_timeout(self):
        fx = RuntimeFixture("quadruped", time_scale=20.0)
        try:
            out = fx.runtime.invoke("base.navigate_to", {"request_id": "nav-far", "hardware_authorized": True,
                                                         "args": {"goal": [190.0, 0, 0, 0, 0, 0, 1]}})
            run = fx.runtime.runner._runs[out["run_id"]]
            wait_until(lambda: run.deadline is not None)
            self.assertAlmostEqual(run.deadline - fx.runtime.bus.clock.now(),
                                   runner_module.ACTION_DEFAULT_TIMEOUT_S["base.navigate_to"], delta=5)
            fx.runtime.runner.cancel(out["run_id"])
            fx.wait(out["run_id"], 3)
            out = fx.runtime.invoke("base.navigate_to", {"request_id": "nav-short", "hardware_authorized": True,
                                                         "timeout_s": 1.0,
                                                         "args": {"goal": [190.0, 0, 0, 0, 0, 0, 1]}})
            record = fx.wait(out["run_id"], 5)
            self.assertEqual((record["status"], record["error"]), ("failed", "运行超时"))
            self.assertEqual(fx.adapter.cmd, (0.0, 0.0, 0.0))
            # A request can shorten, never extend.
            self.assertEqual(runner_module.Runner.action_timeout("base.navigate_to", {}, 1e9),
                             runner_module.ACTION_DEFAULT_TIMEOUT_S["base.navigate_to"])
            with self.assertRaises(BadRequest):
                runner_module.Runner.action_timeout("base.navigate_to", {}, float("nan"))
        finally:
            fx.close()


class ArmSkillTests(unittest.TestCase):
    def test_wave_runs_on_sim_arm(self):
        fx = RuntimeFixture("arm", time_scale=50.0)
        try:
            fx.install(example("wave"))
            record = fx.wait(fx.run("wave", {"cycles": 1}), 20)
            self.assertEqual(record["status"], "succeeded", record["error"])
            self.assertEqual(record["result"]["final_position"], [0.0] * 6)
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
