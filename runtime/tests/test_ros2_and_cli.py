"""ROS 2 skeleton (pure converters only — no ROS 2 here) and the CLI entry points."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS

import _support
from _support import EXAMPLES, REPO_ROOT, RUNTIME_DIR, TOKEN, http
from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

from neuxmind_runtime.adapters import ros2


def _vec(x=0.0, y=0.0, z=0.0, w=None):
    return NS(x=x, y=y, z=z) if w is None else NS(x=x, y=y, z=z, w=w)


class Ros2ConverterTests(unittest.TestCase):
    """Verifies message field mapping only; rclpy integration is NOT verified."""

    def test_twist(self):
        self.assertEqual(ros2.twist_fields({"vx": 0.3, "wz": -0.2}),
                         {"linear": {"x": 0.3, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": -0.2}})

    def test_odometry_matches_capability_outputs(self):
        msg = NS(pose=NS(pose=NS(position=_vec(1, 2, 0), orientation=_vec(0, 0, 0, 1))),
                 twist=NS(twist=NS(linear=_vec(0.5, 0.1), angular=_vec(z=0.2))))
        out = ros2.odometry_outputs(msg)
        spec_caps.validate_payload("base.odometry", out, "outputs")
        self.assertEqual(out["velocity"], [0.5, 0.1, 0.2])

    def test_battery_percentage_scaled_and_nan_rejected(self):
        out = ros2.battery_outputs(NS(percentage=0.42, power_supply_status=1, voltage=24.1))
        spec_caps.validate_payload("sensor.battery", out, "outputs")
        self.assertAlmostEqual(out["percent"], 42.0)
        self.assertTrue(out["charging"])
        with self.assertRaises(ValueError):
            ros2.battery_outputs(NS(percentage=float("nan"), power_supply_status=0, voltage=24.0))

    def test_nav_goal_and_manifest(self):
        fields = ros2.nav_goal_fields({"goal": [1, 2, 0, 0, 0, 0, 1]}, "map")
        self.assertEqual(fields["header"]["frame_id"], "map")
        with self.assertRaises(ValueError):          # limits must come from the integrator
            ros2.build_manifest(ros2.DEFAULT_CONFIG)
        config = json.loads(json.dumps(ros2.DEFAULT_CONFIG))
        config["limits"].update(max_vx=0.8, max_wz=1.0)
        manifest = spec_manifest.validate_manifest_v2(ros2.build_manifest(config))
        self.assertFalse(manifest["simulated"])

    def test_clear_error_without_rclpy(self):
        try:
            import rclpy  # noqa: F401
            self.skipTest("rclpy 可用；本测试只验证缺少 ROS 2 时的报错")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as ctx:
            ros2.Ros2Adapter()
        self.assertIn("ROS 2", str(ctx.exception))


class _Future:
    """Stand-in for rclpy.task.Future."""

    def __init__(self):
        self._done, self._result, self._callbacks = False, None, []

    def done(self):
        return self._done

    def result(self):
        return self._result

    def add_done_callback(self, fn):
        if self._done:
            fn(self)
        else:
            self._callbacks.append(fn)

    def complete(self, value):
        self._result, self._done = value, True
        for fn in self._callbacks:
            fn(self)


class _GoalHandle:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.cancels = 0

    def cancel_goal_async(self):
        self.cancels += 1

    def get_result_async(self):
        return _Future()


class Ros2StopSemanticsTests(unittest.TestCase):
    """R-06 (stand-in objects; rclpy itself is NOT verified)."""

    def test_cancel_before_goal_accepted_is_sent_when_goal_arrives(self):
        send = _Future()
        handle = ros2.Ros2NavHandle(send)
        handle.cancel()                         # stop lands while send_goal_async is pending
        self.assertEqual(handle.poll().state, "cancelled")
        goal = _GoalHandle()
        send.complete(goal)                     # Nav2 accepts the goal afterwards
        self.assertEqual(goal.cancels, 1)       # ... and is cancelled immediately
        handle.cancel()
        self.assertEqual(goal.cancels, 1)       # sent once

    def test_cancel_after_goal_accepted(self):
        send = _Future()
        handle = ros2.Ros2NavHandle(send)
        goal = _GoalHandle()
        send.complete(goal)
        self.assertEqual(handle.poll().state, "running")
        handle.cancel()
        self.assertEqual(goal.cancels, 1)

    def test_finished_handles_are_pruned(self):
        handles = set()
        send = _Future()
        handle = ros2.Ros2NavHandle(send, on_done=handles.discard)
        handles.add(handle)
        send.complete(_GoalHandle(accepted=False))
        self.assertEqual(handle.poll().state, "failed")
        self.assertEqual(handles, set())

    def test_stop_cancels_known_goals_and_all_goals_even_if_publish_fails(self):
        calls = []
        send = _Future()
        handle = ros2.Ros2NavHandle(send)

        def publish(_args):
            calls.append("twist")
            raise RuntimeError("publisher gone")
        adapter = NS(_publish_twist=publish, _nav_handles={handle},
                     _cancel_all_goals=lambda: calls.append("cancel_all"))
        with self.assertRaises(RuntimeError):
            ros2.stop_adapter(adapter)
        self.assertEqual(calls, ["twist", "cancel_all"])
        goal = _GoalHandle()
        send.complete(goal)
        self.assertEqual(goal.cancels, 1)

    def test_simulated_comes_from_config(self):
        config = json.loads(json.dumps(ros2.DEFAULT_CONFIG))
        config["limits"].update(max_vx=0.8, max_wz=1.0)
        self.assertFalse(ros2.build_manifest(config)["simulated"])
        self.assertTrue(ros2.build_manifest(config, use_sim_time_param=True)["simulated"])
        for key in ("simulated", "use_sim_time"):
            cfg = json.loads(json.dumps(config))
            cfg[key] = True
            manifest = spec_manifest.validate_manifest_v2(ros2.build_manifest(cfg))
            self.assertTrue(manifest["simulated"])
            self.assertTrue(spec_manifest.manifest_v1_view(manifest)["simulated"])
        cfg = json.loads(json.dumps(config))
        cfg["simulated"] = "yes"
        with self.assertRaises(ValueError):
            ros2.build_manifest(cfg)

    def test_config_rejects_nan_and_negative_limits(self):
        with self.assertRaises(ValueError):
            ros2.parse_config_text('{"limits": {"max_vx": NaN, "max_wz": 1.0}}')
        with self.assertRaises(ValueError):
            ros2.parse_config_text('{"limits": {"max_vx": Infinity, "max_wz": 1.0}}')
        config = json.loads(json.dumps(ros2.DEFAULT_CONFIG))
        config["limits"].update(max_vx=-0.8, max_wz=1.0)
        with self.assertRaises(ValueError):              # spec (and the supervisor) refuse it
            spec_manifest.validate_manifest_v2(ros2.build_manifest(config))


class CliTests(unittest.TestCase):
    def _run(self, args, cwd, module="neuxmind_runtime"):
        return subprocess.run([sys.executable, "-m", module, *args], cwd=cwd, capture_output=True,
                              text=True, timeout=30, env={**os.environ, "PYTHONPATH": ""})

    def test_check_from_runtime_dir_and_repo_root(self):
        ok = self._run(["check", str(EXAMPLES / "status_report"), "--adapter", "sim_arm"], RUNTIME_DIR)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(json.loads(ok.stdout)["compatible"])
        bad = self._run(["check", str(EXAMPLES / "patrol"), "--adapter", "sim_arm"], REPO_ROOT,
                        module="runtime.neuxmind_runtime")
        self.assertEqual(bad.returncode, 1, bad.stderr)
        self.assertIn("base.navigate_to", json.loads(bad.stdout)["missing"])

    def test_serve_requires_long_token(self):
        result = subprocess.run([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter", "sim_arm"],
                                cwd=RUNTIME_DIR, capture_output=True, text=True, timeout=30,
                                env={**os.environ, "BRAIN_EDGE_TOKEN": "short"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("24", result.stderr)

    def test_serve_sim_arm(self):
        with tempfile.TemporaryDirectory() as state:
            proc = subprocess.Popen([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter", "sim_arm",
                                     "--port", "0", "--state-dir", state, "--time-scale", "10"],
                                    cwd=RUNTIME_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env={**os.environ, "BRAIN_EDGE_TOKEN": TOKEN})
            try:
                line = _support.readline_within(proc.stdout, 30)
                self.assertIn("simulated=true", line)
                url = line.strip().split()[-1]
                status, body = http(url, "/v1/status")
                self.assertEqual((status, body["instance_id"], body["simulated"]), (200, "sim-arm-01", True))
            finally:
                proc.terminate()
                proc.wait(10)
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
