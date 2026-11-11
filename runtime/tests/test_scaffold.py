"""§6.2 adapter scaffold: capability mapping → installable adapter package.

The generated package must load with ``serve --adapter module:Class``, pass the
conformance static level (its own generated test), declare only implemented
capabilities, raise NotImplementedError for the rest, and -- for ROS 2
mappings -- work: static, dry and motion on the rclpy test double (NOT ROS 2).
"""
import importlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _support
from _support import TOKEN, http, readline_within

from neuxmind_runtime import cli, conformance, scaffold
from neuxmind_runtime.adapters import load_adapter

ROVER = {
    "name": "acme_rover", "vendor": "Acme", "model": "R1", "family": "mobile", "instance_id": "acme-r1-01",
    "safety": {"hardware_estop": True},
    "capabilities": [
        {"id": "safety.stop", "params": {"hardware_estop": True}, "implementation": "ros2"},
        {"id": "system.health", "implementation": "ros2"},
        {"id": "base.velocity", "params": {"max_vx": 0.8, "max_vy": 0.0, "max_wz": 1.0, "holonomic": False},
         "implementation": "ros2:/cmd_vel", "evidence_id": "ev-12"},
        {"id": "base.odometry", "params": {"frame": "odom"}, "implementation": "ros2:/odom"},
        {"id": "sensor.battery", "implementation": "ros2:/battery_state"},
        {"id": "sensor.imu", "params": {"rate_hz": 50.0, "frame": "imu_link"}, "implementation": "ros2:/imu"},
        {"id": "joint.state", "params": {"dof": 2}, "implementation": "ros2:/joint_states"},
        {"id": "localization.pose", "implementation": "ros2:/amcl_pose"},
        {"id": "base.navigate_to", "params": {"planner": "nav2"}, "implementation": "ros2"},
        {"id": "sensor.camera.rgb", "params": {"name": "front", "frame": "cam", "rate_hz": 15.0},
         "implementation": "ros2:/camera/image_raw", "evidence_id": "ev-3"},
        {"id": "audio.speak", "implementation": "sdk:acme_tts"},
    ]}

SDK_ARM = {
    "name": "orbit_arm", "vendor": "Orbit", "model": "OA-6", "family": "manipulator",
    "capabilities": [
        {"id": "joint.state", "params": {"dof": 6}, "implementation": "serial:/dev/ttyUSB0"},
        {"id": "arm.joint_position", "implementation": "sdk:orbit",
         "params": {"dof": 6, "lower": [-3.0] * 6, "upper": [3.0] * 6, "max_velocity": 1.0}},
        {"id": "x.orbit.teach_mode", "implementation": "sdk:orbit"},
    ]}


class _Generated:
    """Generate into a temp dir and import it; undo sys.path / sys.modules afterwards."""

    def __init__(self, test, spec):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.close)
        self.root = Path(self.tmp.name) / spec["name"]
        self.summary = scaffold.generate(spec, self.root)
        self.module_name = spec["name"]

    def module(self):
        sys.path.insert(0, str(self.root))
        return importlib.import_module(self.module_name + ".adapter")

    def run_tests(self, extra_path=()):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([*extra_path, str(_support.RUNTIME_DIR)])}
        return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                              cwd=str(self.root), capture_output=True, text=True, timeout=120, env=env)

    def close(self):
        for name in list(sys.modules):
            if name.split(".")[0] == self.module_name:
                del sys.modules[name]
        while str(self.root) in sys.path:
            sys.path.remove(str(self.root))
        self.tmp.cleanup()


class Ros2Mapping(unittest.TestCase):
    def test_package_layout_and_mapping(self):
        gen = _Generated(self, ROVER)
        s = gen.summary
        self.assertEqual(s["adapter"], "acme_rover.adapter:AcmeRoverAdapter")
        self.assertEqual(s["files"], ["README.md", "acme_rover/__init__.py", "acme_rover/adapter.json",
                                      "acme_rover/adapter.py", "pyproject.toml", "tests/test_adapter.py"])
        self.assertEqual(sorted(s["todo"]), ["audio.speak", "sensor.camera.rgb"])
        self.assertEqual(s["mandatory_todo"], [])
        self.assertIn("base.navigate_to", s["implemented"])
        config = json.loads((gen.root / "acme_rover" / "adapter.json").read_text(encoding="utf-8"))
        self.assertEqual(config["schema"], "neuxmind.adapter_scaffold.v1")
        by_id = {c["id"]: c for c in config["capabilities"]}
        self.assertEqual(by_id["base.velocity"]["evidence_id"], "ev-12")
        self.assertEqual(by_id["base.navigate_to"]["ros2_name"], "navigate_to_pose")
        self.assertEqual(by_id["sensor.camera.rgb"]["scaffold_status"], "todo")
        readme = (gen.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("不是硬件急停", readme)
        self.assertIn("ev-3", readme)
        pyproject = _support.load_toml((gen.root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["dependencies"], ["neuxmind-runtime"])

        mod = gen.module()
        manifest = mod.AcmeRoverAdapter.offline_manifest()
        declared = {c["id"] for c in manifest["capabilities"]}
        self.assertNotIn("sensor.camera.rgb", declared)
        self.assertNotIn("audio.speak", declared)
        self.assertEqual(declared, set(mod.IMPLEMENTED))
        stub = object.__new__(mod.AcmeRoverAdapter)
        with self.assertRaises(NotImplementedError):
            stub._read_sensor_camera_rgb()
        with self.assertRaises(NotImplementedError):
            stub._invoke_audio_speak({})
        self.assertTrue(conformance.check_manifest(mod.AcmeRoverAdapter.offline_manifest)["static_ok"])

    def test_generated_tests_pass_with_and_without_ros2(self):
        gen = _Generated(self, ROVER)
        proc = gen.run_tests()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skipped", proc.stderr)                      # live instance needs ROS 2
        proc = gen.run_tests([str(_support.ROS2_DOUBLE)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("skipped", proc.stderr)

    def test_generated_ros2_adapter_works_on_the_double(self):
        gen = _Generated(self, ROVER)
        with _support.ros2_double() as graph:
            mod = gen.module()
            adapter = mod.AcmeRoverAdapter()
            self.assertTrue(adapter.manifest()["simulated"])          # use_sim_time on the double
            report = conformance.run_adapter(adapter, "motion", True)
            bad = [c for c in report["cases"] if c["status"] == "failed" or c.get("skip_kind") == "blocked"]
            self.assertEqual(bad, [], conformance.summarize(report))
            self.assertTrue(report["static_ok"] and report["dry_ok"] and report["motion_ok"])
            for cid in ("dry.sensor.imu.read", "dry.joint.state.read", "motion.base.velocity.watchdog",
                        "motion.base.navigate_to.stop_latency"):
                self.assertEqual(next(c for c in report["cases"] if c["id"] == cid)["status"], "passed", cid)
            last = graph.published["/cmd_vel"][-1]
            self.assertEqual((last.linear.x, last.angular.z), (0.0, 0.0))

    def test_serve_loads_the_generated_adapter(self):
        gen = _Generated(self, ROVER)
        with tempfile.TemporaryDirectory() as state:
            env = {**os.environ, "BRAIN_EDGE_TOKEN": TOKEN,
                   "PYTHONPATH": os.pathsep.join([str(gen.root), str(_support.ROS2_DOUBLE), str(_support.RUNTIME_DIR)])}
            proc = subprocess.Popen([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter",
                                     gen.summary["adapter"], "--port", "0", "--state-dir", state,
                                     "--sandbox", "inprocess"],
                                    cwd=str(_support.RUNTIME_DIR), env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            try:
                line = readline_within(proc.stdout, 20)
                url = line.strip().rsplit(" ", 1)[-1]
                self.assertTrue(url.startswith("http://"), line)
                status, manifest = http(url, "/v2/manifest")
                self.assertEqual(status, 200)
                self.assertEqual(manifest["instance_id"], "acme-r1-01")
                self.assertTrue(manifest["simulated"])
                self.assertNotIn("sensor.camera.rgb", {c["id"] for c in manifest["capabilities"]})
                rc = cli.main(["conformance", "--url", url, "--token", TOKEN, "--level", "dry",
                               "--out", os.path.join(state, "report.json")])
                self.assertEqual(rc, 0)
            finally:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
                proc.stdout.close()
                proc.stderr.close()

    def test_sensor_only_ros2_body(self):
        spec = {"name": "probe_box", "vendor": "Lab", "model": "P1", "family": "custom",
                "capabilities": [{"id": "safety.stop", "params": {"hardware_estop": False}, "implementation": "ros2"},
                                 {"id": "system.health", "implementation": "ros2"},
                                 {"id": "sensor.battery", "implementation": "ros2"}]}
        gen = _Generated(self, spec)
        text = (gen.root / "probe_box" / "adapter.py").read_text(encoding="utf-8")
        self.assertIn("nothing this adapter commands can move", text)
        with _support.ros2_double():
            report = conformance.run_adapter(gen.module().ProbeBoxAdapter(), "dry")
            self.assertTrue(report["dry_ok"], conformance.summarize(report))


class NonRos2Mapping(unittest.TestCase):
    def test_todo_mandatory_capabilities_block_loading(self):
        gen = _Generated(self, SDK_ARM)
        s = gen.summary
        self.assertEqual(s["implemented"], [])
        self.assertEqual(sorted(s["mandatory_todo"]), ["safety.stop", "system.health"])
        mod = gen.module()
        with self.assertRaises(NotImplementedError) as ctx:
            mod.OrbitArmAdapter.offline_manifest()
        self.assertIn("必备能力", str(ctx.exception))
        with self.assertRaises(NotImplementedError):
            load_adapter("orbit_arm.adapter:OrbitArmAdapter")
        stub = object.__new__(mod.OrbitArmAdapter)
        for call in (lambda: stub.stop(), lambda: stub._read_joint_state(), lambda: stub._start_arm_joint_position({})):
            with self.assertRaises(NotImplementedError):
                call()
        report = conformance.check_manifest(mod.OrbitArmAdapter.offline_manifest)
        self.assertFalse(report["static_ok"])
        proc = gen.run_tests()
        self.assertNotEqual(proc.returncode, 0)                      # honest: static fails until implemented
        self.assertIn("必备能力", proc.stderr)
        readme = (gen.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("必备能力 safety.stop, system.health 尚未实现", readme)

    def test_implementing_the_todos_makes_static_and_dry_pass(self):
        """The workflow an engineer (or the desktop AI) follows: implement, add to IMPLEMENTED, re-test."""
        gen = _Generated(self, SDK_ARM)
        path = gen.root / "orbit_arm" / "adapter.py"
        text = path.read_text(encoding="utf-8")
        text = text.replace("IMPLEMENTED = ()", "IMPLEMENTED = ('safety.stop', 'system.health', 'joint.state')")
        text = text.replace("        simulated = False\n",
                            "        simulated = False\n        self.q = [0.0] * 6\n")
        stop_todo = text[text.index("    def stop(self)"):text.index("    def _read_system_health")]
        text = text.replace(stop_todo, '    def stop(self) -> None:\n        """Nothing moves yet."""\n\n')
        health_todo = text[text.index("    def _read_system_health"):text.index("    def _read_joint_state")]
        text = text.replace(health_todo, '    def _read_system_health(self) -> dict:\n'
                                         '        return {"ok": True, "mode": "bench", "errors": {}}\n\n')
        joint_todo = text[text.index("    def _read_joint_state"):text.index("    def _start_arm_joint_position")]
        text = text.replace(joint_todo, '    def _read_joint_state(self) -> dict:\n'
                                        '        return {"names": [f"j{i}" for i in range(6)], "position": list(self.q)}\n\n')
        path.write_text(text, encoding="utf-8")
        mod = gen.module()
        self.assertTrue(conformance.check_manifest(mod.OrbitArmAdapter.offline_manifest)["static_ok"])
        report = conformance.run_adapter(mod.OrbitArmAdapter(), "dry")
        self.assertTrue(report["dry_ok"], conformance.summarize(report))
        self.assertNotIn("arm.joint_position", {c["id"] for c in mod.OrbitArmAdapter.offline_manifest()["capabilities"]})
        self.assertEqual(gen.run_tests().returncode, 0)


class Validation(unittest.TestCase):
    def assert_error(self, spec, needle):
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.normalize_spec(spec)
        self.assertIn(needle, str(ctx.exception))

    def test_inputs_are_checked(self):
        base = json.loads(json.dumps(ROVER))
        self.assert_error({**base, "name": "Bad-Name"}, "name")
        self.assert_error({**base, "name": "neuxmind_x"}, "name")
        self.assert_error({**base, "family": "boat"}, "family")
        self.assert_error({**base, "vendor": ""}, "vendor")
        self.assert_error({**base, "capabilities": [{"id": "base.teleport"}]}, "未知能力")
        self.assert_error({**base, "capabilities": [{"id": "base.velocity", "params": {"max_vx": 1.0},
                                                     "implementation": "ros2"}]}, "缺少必填参数")
        self.assert_error({**base, "capabilities": [{"id": "sensor.battery", "params": {"cells": 6}}]}, "未定义参数")
        self.assert_error({**base, "capabilities": [{"id": "base.velocity", "implementation": "ros2",
                                                     "params": {"max_vx": -1.0, "max_vy": 0.0, "max_wz": 1.0,
                                                                "holonomic": False}}]}, "max_vx")
        self.assert_error({**base, "capabilities": [{"id": "sensor.battery", "implementation": "telepathy:x"}]},
                          "未知实现提示")
        self.assert_error({**base, "capabilities": [{"id": "sensor.battery", "implementation": 'ros2:/a"b'}]},
                          "目标无效")
        self.assert_error({**base, "capabilities": ["sensor.battery", "battery"]}, "能力重复")

    def test_mandatory_capabilities_are_always_added(self):
        plan = scaffold.normalize_spec({"name": "tiny", "vendor": "V", "model": "M", "family": "custom",
                                        "capabilities": ["sensor.battery"]})
        ids = [c["id"] for c in plan["capabilities"]]
        self.assertIn("safety.stop", ids)
        self.assertIn("system.health", ids)

    def test_output_directory_must_be_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "keep.txt").write_text("x")
            with self.assertRaises(scaffold.ScaffoldError):
                scaffold.generate(ROVER, tmp)
            scaffold.generate(ROVER, tmp, force=True)
            self.assertTrue((Path(tmp) / "keep.txt").exists())

    def test_cli_flags_and_spec_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.main(["scaffold", "--out", os.path.join(tmp, "a"), "--name", "flag_bot", "--vendor", "V",
                           "--model", "M", "--family", "mobile", "--cap", "safety.stop,system.health",
                           "--cap", "base.velocity", "--param", "base.velocity.max_vx=0.5",
                           "--param", "base.velocity.max_vy=0", "--param", "base.velocity.max_wz=0.7",
                           "--param", "base.velocity.holonomic=false", "--param", "safety.stop.hardware_estop=true",
                           "--impl", "safety.stop=ros2", "--impl", "system.health=ros2",
                           "--impl", "base.velocity=ros2:/robot/cmd_vel"])
            self.assertEqual(rc, 0)
            config = json.loads((Path(tmp) / "a" / "flag_bot" / "adapter.json").read_text(encoding="utf-8"))
            by_id = {c["id"]: c for c in config["capabilities"]}
            self.assertEqual(by_id["base.velocity"]["ros2_name"], "/robot/cmd_vel")
            self.assertEqual(by_id["base.velocity"]["params"]["max_vx"], 0.5)
            self.assertIs(by_id["safety.stop"]["params"]["hardware_estop"], True)
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(SDK_ARM), encoding="utf-8")
            self.assertEqual(cli.main(["scaffold", "--out", os.path.join(tmp, "b"), "--spec", str(spec_path)]), 0)
            self.assertEqual(cli.main(["scaffold", "--out", os.path.join(tmp, "b"), "--spec", str(spec_path)]), 2)
            self.assertEqual(cli.main(["scaffold", "--out", os.path.join(tmp, "c"), "--name", "x_y"]), 2)


if __name__ == "__main__":
    unittest.main()
