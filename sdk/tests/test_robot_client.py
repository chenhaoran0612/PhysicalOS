"""RobotClient against a real runtime gateway serving simulated bodies."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _support
from _support import RUNTIME_EXAMPLES, SLEEPER, RuntimeServer, make_skill

import neuxmind.robot as robot_module
from neuxmind import NeuxMindError
from neuxmind.robot import RobotClient
from neuxmind.templates import create

# Liveness bound for "the run reaches its terminal state". It is not what the
# tests verify (that is the terminal status itself), so it is generous: with
# the runtime's process sandbox every skill run starts a fresh interpreter, and
# on a loaded 2-CPU machine (several suites in parallel) start-up and the
# cancel/kill path can take seconds.
SETTLE_S = 60



class RobotClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="nx-sdk-rc-"))
        cls.patrol_dir = create("patrol", "sdk-patrol", cls.tmp / "sdk-patrol")
        cls.sleeper_dir = make_skill(cls.tmp, "sleeper", SLEEPER, requires=("base.velocity",), timeout_s=600)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.rt = RuntimeServer("quadruped")
        self.addCleanup(self.rt.close)
        self.robot = RobotClient(self.rt.url, self.rt.token)

    def record_adapter_commands(self):
        """Every command that reaches the (simulated) adapter, after the safety supervisor."""
        commands = []
        real = self.rt.adapter.invoke

        def spy(cap_id, args):
            commands.append((cap_id, dict(args)))
            return real(cap_id, args)

        patcher = mock.patch.object(self.rt.adapter, "invoke", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return commands

    def wait_until(self, predicate, what: str, timeout: float = SETTLE_S):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"{timeout} 秒内没有等到：{what}")
            time.sleep(0.02)

    def count_requests(self):
        calls = []
        real = robot_module.request_json

        def spy(method, url, **kw):
            calls.append((method, url))
            return real(method, url, **kw)

        patcher = mock.patch.object(robot_module, "request_json", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    # ------------------------------------------------------------ reads
    def test_manifest_status_registry_state(self):
        manifest = self.robot.manifest()
        self.assertEqual(manifest["schema"], "robot.adapter.v2")
        self.assertTrue(self.robot.simulated)
        self.assertEqual(self.robot.manifest_v1()["schema"], "robot.adapter.v1")
        status = self.robot.status()
        self.assertFalse(status["estop_latched"])
        self.assertTrue(status["simulated"])
        self.assertEqual(self.robot.registry()["schema"], "neuxmind.capabilities.v1")
        state = self.robot.state("system.health", "sensor.battery")
        self.assertEqual(set(state), {"system.health", "sensor.battery"})
        self.assertIn("base.odometry", self.robot.state())
        self.assertGreater(self.robot.telemetry(0)["last_seq"], 0)

    def test_errors_carry_server_codes(self):
        with self.assertRaises(NeuxMindError) as cm:
            RobotClient(self.rt.url, "wrong-token-" + "x" * 30).manifest()
        self.assertEqual((cm.exception.code, cm.exception.status), ("unauthorized", 401))
        with self.assertRaises(NeuxMindError) as cm:
            self.robot.run("run-does-not-exist")
        self.assertEqual((cm.exception.code, cm.exception.status), ("not_found", 404))
        with self.assertRaises(NeuxMindError) as cm:
            RobotClient("http://127.0.0.1:9", self.rt.token, timeout=1).manifest()
        self.assertEqual(cm.exception.code, "connection_failed")

    # ------------------------------------------------------------ authorization
    def test_motion_without_authorize_is_refused_locally(self):
        calls = self.count_requests()
        for call in (lambda: self.robot.invoke("base.velocity", {"vx": 0.2, "wz": 0.0}),
                     lambda: self.robot.invoke("legged.posture", {"posture": "sit"}),
                     lambda: self.robot.invoke("x.acme.dance", {}),
                     lambda: self.robot.run_skill("sdk-patrol"),
                     lambda: self.robot.reset()):
            with self.assertRaises(NeuxMindError) as cm:
                call()
            self.assertEqual(cm.exception.code, "authorization_required")
        self.assertEqual(calls, [])                      # nothing reached the robot
        self.assertEqual(self.rt.adapter.cmd, (0.0, 0.0, 0.0))
        self.assertEqual(self.rt.adapter.posture, "stand")

    def test_invoke_command_and_action_with_authorize(self):
        commands = self.record_adapter_commands()
        out = self.robot.invoke("base.velocity", {"vx": 5.0, "wz": 0.0}, authorize=True)
        self.assertEqual(out, {"accepted": True})
        # Clamped by the runtime's safety supervisor before it reached the body. (Reading
        # adapter.cmd afterwards raced the velocity watchdog: 0.5 s robot time = 25 ms wall
        # at time_scale 20, so under load the setpoint was already zeroed.)
        sent = [args for cap, args in commands if cap == "base.velocity" and args.get("vx")]
        self.assertEqual([a["vx"] for a in sent], [1.0])
        self.robot.stop()
        self.robot.reset(authorize=True)
        started = self.robot.invoke("base.navigate_to", {"goal": [0.5, 0, 0, 0, 0, 0, 1]}, authorize=True)
        record = self.robot.wait(started["run_id"], timeout=SETTLE_S, interval=0.05)
        self.assertEqual(record["status"], "succeeded")

    # ------------------------------------------------------------ skills
    def test_skill_install_run_wait_and_idempotency(self):
        installed = self.robot.install_skill(self.patrol_dir, request_id="install-1")
        self.assertTrue(installed["installed"])
        again = self.robot.install_skill(self.patrol_dir, request_id="install-1")
        self.assertEqual(again["digest"], installed["digest"])
        self.assertEqual([s["ref"] for s in self.robot.skills()], ["sdk-patrol@0.1.0"])

        updates = []
        started = self.robot.run_skill("sdk-patrol", {"waypoints": [[1, 0], [1, 1]]}, authorize=True,
                                       request_id="run-1")
        same = self.robot.run_skill("sdk-patrol", {"waypoints": [[1, 0], [1, 1]]}, authorize=True,
                                    request_id="run-1")
        self.assertEqual(same["run_id"], started["run_id"])          # runtime idempotency
        with self.assertRaises(NeuxMindError) as cm:
            self.robot.run_skill("sdk-patrol", {"waypoints": [[2, 0]]}, authorize=True, request_id="run-1")
        self.assertEqual(cm.exception.code, "request_id_reused")
        self.assertEqual(cm.exception.details["request_id"], "run-1")
        record = self.robot.wait(started["run_id"], timeout=SETTLE_S, interval=0.05, on_update=updates.append)
        self.assertEqual(record["status"], "succeeded", record)
        self.assertEqual(record["result"]["reached"], 2)
        self.assertTrue(updates)
        self.assertIn(started["run_id"], [r["id"] for r in self.robot.runs()])

    def test_incompatible_install_rejected_by_runtime(self):
        arm = RuntimeServer("arm")
        self.addCleanup(arm.close)
        with self.assertRaises(NeuxMindError) as cm:
            RobotClient(arm.url, arm.token).install_skill(self.patrol_dir)
        self.assertEqual(cm.exception.code, "incompatible")
        self.assertIn("base.navigate_to", cm.exception.details["compatibility"]["missing"])

    def test_cancel_and_wait_timeout(self):
        commands = self.record_adapter_commands()
        self.robot.install_skill(self.sleeper_dir)
        run_id = self.robot.run_skill("sleeper", authorize=True)["run_id"]
        # Synchronise on the skill really running (its process started and commanded the
        # body) instead of assuming it does so within a fixed wall-clock window.
        self.wait_until(lambda: any(cap == "base.velocity" for cap, _ in commands), "技能进程发出第一条运动指令")
        with self.assertRaises(NeuxMindError) as cm:
            self.robot.wait(run_id, timeout=0.3, interval=0.05)
        self.assertEqual(cm.exception.code, "wait_timeout")
        self.assertEqual(cm.exception.details["run"]["status"], "running", cm.exception.details["run"])
        # wait_timeout leaves the run alone: it keeps commanding the body.
        seen = len(commands)
        self.wait_until(lambda: len(commands) > seen, "wait 超时后运行仍在继续")
        self.assertEqual(self.robot.run(run_id)["status"], "running")
        self.assertIsNone(self.robot.run(run_id)["stop_reason"])

        cancelled = self.robot.cancel(run_id)
        self.assertTrue(cancelled["accepted"], cancelled)
        record = self.robot.wait(run_id, timeout=SETTLE_S, interval=0.05)
        self.assertEqual((record["status"], record["stop_reason"]), ("stopped", "cancelled"), record)
        self.assertFalse(self.robot.status()["estop_latched"])        # cancel does not latch

    def test_stop_latches_until_reset(self):
        self.robot.install_skill(self.sleeper_dir)
        self.robot.install_skill(RUNTIME_EXAMPLES / "status_report")
        run_id = self.robot.run_skill("sleeper", authorize=True)["run_id"]
        time.sleep(0.1)
        stopped = self.robot.stop("sdk test")
        self.assertTrue(stopped["accepted"])
        self.assertIn(run_id, stopped["stopped_runs"])
        self.assertEqual(self.robot.wait(run_id, timeout=SETTLE_S, interval=0.05)["status"], "stopped")
        self.assertTrue(self.robot.status()["estop_latched"])

        with self.assertRaises(NeuxMindError) as cm:
            self.robot.run_skill("sleeper", authorize=True)
        self.assertEqual((cm.exception.code, cm.exception.status), ("estop_latched", 409))
        with self.assertRaises(NeuxMindError) as cm:
            self.robot.invoke("base.velocity", {"vx": 0.1, "wz": 0.0}, authorize=True)
        self.assertEqual(cm.exception.code, "estop_latched")
        # Non-motion skills still run while latched.
        report = self.robot.wait(self.robot.run_skill("status_report", authorize=True)["run_id"], timeout=SETTLE_S)
        self.assertEqual(report["status"], "succeeded")

        self.assertEqual(self.robot.reset(authorize=True)["latched"], False)
        run_id = self.robot.run_skill("sleeper", authorize=True)["run_id"]
        self.assertEqual(self.robot.run(run_id)["status"] in ("queued", "running"), True)
        self.robot.stop()
        self.assertEqual(self.robot.wait(run_id, timeout=SETTLE_S, interval=0.05)["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
