"""§6.3 SDK: ``nx adapter new|test``, ``nx robot conformance [--upload]``, require_level, install progress.

Real: the runtime's scaffold and conformance suite (in this process), the
runtime HTTP gateway with a simulated body (``POST /v2/conformance``), the
rclpy test double from ``runtime/tests/ros2_double`` (NOT ROS 2) for the
generated ROS 2 adapter, and the cloud app (uvicorn, SQLite). Simulated bodies
produce ``evidence_kind: "simulation"`` reports; nothing here is acceptance.
"""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support
from _support import OPERATOR_EMAIL, PASSWORD, REPO, CliHome, CloudServer, PackageServer, RuntimeServer

from neuxmind import NeuxMindError, adapters
from neuxmind import robot as robot_module
from neuxmind.cloud import CloudClient
from neuxmind.models import _wait_install, install_progress
from neuxmind.robot import RobotClient

ROS2_DOUBLE = REPO / "runtime" / "tests" / "ros2_double"
_DOUBLE_MODULES = ("rclpy", "geometry_msgs", "nav_msgs", "sensor_msgs", "nav2_msgs", "action_msgs", "fake_robot")

ROVER_ARGS = [
    "--family", "mobile", "--vendor", "Acme", "--model", "R1",
    "--caps", "safety.stop,system.health,base.velocity,base.odometry",
    "--caps", "sensor.battery,sensor.camera.rgb",
    "--impl", "safety.stop=ros2", "--impl", "system.health=ros2", "--impl", "base.velocity=ros2:/cmd_vel",
    "--impl", "base.odometry=ros2:/odom", "--impl", "sensor.battery=ros2",
    "--param", "base.velocity.max_vx=0.5", "--param", "base.velocity.max_vy=0", "--param", "base.velocity.max_wz=0.7",
    "--param", "base.velocity.holonomic=false", "--param", "safety.stop.hardware_estop=true",
    "--param", "base.odometry.frame=odom", "--param", "sensor.camera.rgb.name=front",
    "--param", "sensor.camera.rgb.frame=cam", "--param", "sensor.camera.rgb.rate_hz=15",
]


@contextlib.contextmanager
def ros2_double():
    """The rclpy test double on sys.path (and its fake robot running) for the block only."""
    sys.path.insert(0, str(ROS2_DOUBLE))
    import rclpy._graph as graph
    graph.ensure_robot()
    try:
        yield graph
    finally:
        robot = graph.robot()
        if robot is not None:
            robot._stop.set()
            robot.thread.join(1.0)
        for name in list(sys.modules):
            if name.split(".")[0] in _DOUBLE_MODULES:
                del sys.modules[name]
        while str(ROS2_DOUBLE) in sys.path:
            sys.path.remove(str(ROS2_DOUBLE))


class _Home(unittest.TestCase):
    def setUp(self):
        self.home = CliHome()
        self.addCleanup(self.home.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nx-sdk-adapter-"))
        self.addCleanup(_support._rmtree, self.tmp)
        self.addCleanup(adapters._purge, "acme_rover")
        self.addCleanup(adapters._purge, "dog_twin")


# ======================================================================= nx adapter new
class AdapterNewTests(_Home):
    def test_new_from_caps(self):
        out = self.home.json("adapter", "new", "acme_rover", "--out", str(self.tmp / "rover"), *ROVER_ARGS)
        root = Path(out["path"])
        self.assertTrue(root.is_absolute())
        self.assertEqual(out["adapter"], "acme_rover.adapter:AcmeRoverAdapter")
        self.assertEqual(sorted(out["implemented"]),
                         ["base.odometry", "base.velocity", "safety.stop", "sensor.battery", "system.health"])
        self.assertEqual(out["todo"], ["sensor.camera.rgb"])
        self.assertEqual(out["mandatory_todo"], [])
        for rel in ("pyproject.toml", "README.md", "acme_rover/adapter.py", "acme_rover/adapter.json",
                    "tests/test_adapter.py"):
            self.assertTrue((root / rel).is_file(), rel)
        config = json.loads((root / "acme_rover" / "adapter.json").read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in config["capabilities"]}
        self.assertEqual(by_id["base.velocity"]["params"]["max_vx"], 0.5)
        self.assertEqual(by_id["base.velocity"]["implementation"], "ros2:/cmd_vel")
        code, text, _ = self.home.run("adapter", "new", "acme_rover", "--out", str(self.tmp / "rover"), *ROVER_ARGS)
        self.assertEqual(code, 1)                                   # non-empty output dir without --force

    def test_new_from_caps_text_and_missing_limits(self):
        code, text, err = self.home.run("adapter", "new", "acme_rover", "--out", str(self.tmp / "r2"), *ROVER_ARGS)
        self.assertEqual(code, 0, err)
        self.assertIn("待实现（抛 NotImplementedError，不出现在 manifest 中）：sensor.camera.rgb", text)
        self.assertIn("nx adapter test", text)
        bad = self.home.json("adapter", "new", "bare_bot", "--out", str(self.tmp / "bare"), "--caps",
                             "safety.stop,system.health,base.velocity", expect=1)
        self.assertEqual(bad["code"], "scaffold_invalid")
        self.assertIn("缺少必填参数", bad["error"])                    # limits are never guessed
        self.assertFalse((self.tmp / "bare").exists())
        both = self.home.json("adapter", "new", "x_bot", "--out", str(self.tmp / "x"), expect=1)
        self.assertEqual(both["code"], "bad_request")

    def test_new_from_manifest_file_and_robot(self):
        dog = RuntimeServer("quadruped")
        self.addCleanup(dog.close)
        manifest = RobotClient(dog.url, dog.token).manifest()
        path = self.tmp / "dog.json"
        path.write_text(json.dumps({"manifest": manifest}), encoding="utf-8")        # wrapped form accepted
        out = self.home.json("adapter", "new", "dog_twin", "--from-manifest", str(path), "--out",
                             str(self.tmp / "twin"), "--impl", "base.velocity=sdk:acme",
                             "--param", "base.velocity.max_vx=0.3")
        spec = out["input"]
        self.assertEqual({c["id"] for c in spec["capabilities"]}, {c["id"] for c in manifest["capabilities"]})
        self.assertEqual(spec["family"], "quadruped")
        self.assertFalse(spec["simulated"])                          # a new (real) body, not the sim
        self.assertNotIn("instance_id", spec)
        caps = {c["id"]: c for c in spec["capabilities"]}
        self.assertEqual(caps["base.velocity"]["implementation"], "sdk:acme")
        self.assertEqual(caps["base.velocity"]["params"]["max_vx"], 0.3)             # --param overrides
        sim_vel = next(c for c in manifest["capabilities"] if c["id"] == "base.velocity")["params"]
        self.assertEqual(caps["base.velocity"]["params"]["max_wz"], sim_vel["max_wz"])  # copied limits
        self.assertEqual(sorted(out["mandatory_todo"]), ["safety.stop", "system.health"])
        self.assertEqual(out["implemented"], [])
        # From a configured robot: the same capability set, read from its /v2/manifest.
        self.home.json("robot", "add", "dog", dog.url, "--token", dog.token)
        out2 = self.home.json("adapter", "new", "dog_twin", "--from-manifest", "dog", "--out", str(self.tmp / "twin2"))
        self.assertEqual({c["id"] for c in out2["input"]["capabilities"]}, {c["id"] for c in manifest["capabilities"]})
        self.assertIn("/v2/manifest", out2["source"])
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"schema": "robot.adapter.v1"}), encoding="utf-8")
        self.assertEqual(self.home.json("adapter", "new", "dog_twin", "--from-manifest", str(bad), "--out",
                                        str(self.tmp / "t3"), expect=1)["code"], "invalid_manifest")
        self.assertEqual(self.home.json("adapter", "new", "dog_twin", "--from-manifest", "nosuchrobot", "--out",
                                        str(self.tmp / "t4"), expect=1)["code"], "robot_not_found")


# Review Q4-09 payloads (scratchpad/review04/scaffold_inject.py): text that breaks out of a docstring.
def _payload(tag: str) -> str:
    return f'x"""\nimport pathlib; pathlib.Path("/tmp/nx_sdk_pwned_{tag}").write_text("injected")\n"""'


class ScaffoldInjectionTests(_Home):
    def _manifest(self, **changes) -> dict:
        from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
        manifest = SimQuadruped().manifest()
        manifest.update(changes)
        return manifest

    def _pwned(self) -> list:
        return sorted(p.name for p in Path("/tmp").glob("nx_sdk_pwned_*"))

    def setUp(self):
        super().setUp()
        for p in Path("/tmp").glob("nx_sdk_pwned_*"):
            p.unlink()

    def test_manifest_texts_from_a_robot_are_validated(self):
        self.home.json("robot", "add", "evil", "http://127.0.0.1:9", "--token", "t" * 24, "--no-verify")
        for field in ("vendor", "model"):
            bad = self._manifest(**{field: _payload(field)})
            with mock.patch.object(RobotClient, "manifest", return_value=bad):
                err = self.home.json("adapter", "new", "dog_twin", "--from-manifest", "evil", "--out",
                                     str(self.tmp / field), expect=1)
            self.assertEqual(err["code"], "unsafe_scaffold_input", err)
            self.assertIn("来自机器人 evil", err["error"])
            self.assertFalse((self.tmp / field).exists())            # nothing written
        # The same payload in a parameter string (e.g. a camera frame) and from a manifest file.
        bad = self._manifest()
        bad["capabilities"].append({"id": "sensor.camera.rgb",
                                    "params": {"name": _payload("param"), "frame": "cam", "rate_hz": 15.0}})
        path = self.tmp / "bad.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        err = self.home.json("adapter", "new", "dog_twin", "--from-manifest", str(path), "--out",
                             str(self.tmp / "param"), expect=1)
        self.assertEqual(err["code"], "unsafe_scaffold_input")
        self.assertIn("sensor.camera.rgb.params.name", err["error"])
        for value in ("Acme\nimport os", "Acme\\x", "Acme'; import os #", "A" * 101):
            with self.subTest(value=value):
                err = self.home.json("adapter", "new", "x_bot", "--out", str(self.tmp / "flags"), "--vendor", value,
                                     *ROVER_ARGS[2 + 4:], "--family", "mobile", "--model", "R1", expect=1)
                self.assertIn(err["code"], ("unsafe_scaffold_input", "scaffold_invalid"))
        self.assertEqual(self._pwned(), [])
        # Ordinary texts still work (sim manifests, CJK vendor names).
        ok = self.home.json("adapter", "new", "dog_twin", "--from-manifest", str(self._write(self._manifest(
            vendor="宇树 Unitree", model="Go2 (EDU)"))), "--out", str(self.tmp / "ok"))
        self.assertEqual(ok["input"]["vendor"], "宇树 Unitree")

    def _write(self, manifest: dict) -> Path:
        path = self.tmp / "m.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_failed_generation_leaves_nothing_behind(self):
        from neuxmind_runtime import scaffold

        real = scaffold.generate

        def broken(spec, out_dir, force=False):
            real(spec, out_dir, force=force)                         # files are on disk ...
            raise SyntaxError("generated code does not compile")    # ... then the scaffold fails

        with mock.patch.object(scaffold, "generate", broken):
            err = self.home.json("adapter", "new", "acme_rover", "--out", str(self.tmp / "gone"), *ROVER_ARGS, expect=1)
        self.assertEqual(err["code"], "scaffold_failed")
        self.assertIn("已清理", err["error"])
        self.assertFalse((self.tmp / "gone").exists())
        keep = self.tmp / "keep"
        keep.mkdir()
        (keep / "mine.txt").write_text("user file", encoding="utf-8")
        with mock.patch.object(scaffold, "generate", broken):
            self.home.json("adapter", "new", "acme_rover", "--out", str(keep), "--force", *ROVER_ARGS, expect=1)
        self.assertTrue((keep / "mine.txt").is_file())              # a directory we did not create is kept


# ======================================================================= nx adapter test
class AdapterTestTests(_Home):
    def rover(self) -> Path:
        return Path(self.home.json("adapter", "new", "acme_rover", "--out", str(self.tmp / "rover"), *ROVER_ARGS)["path"])

    def test_static_without_ros2(self):
        root = self.rover()
        report = self.home.json("adapter", "test", str(root), "--level", "static")
        self.assertEqual(report["schema"], "conformance.report.v1")
        self.assertTrue(report["static_ok"])
        self.assertIs(report["field_acceptance"], False)
        self.assertTrue(report["adapter"]["offline_manifest"])
        declared = {c["id"] for c in report["capabilities"] if c["declared"]}
        self.assertNotIn("sensor.camera.rgb", declared)              # TODO: not declared
        # The module directory itself works too.
        self.assertTrue(self.home.json("adapter", "test", str(root / "acme_rover"))["static_ok"])

    def test_dry_needs_ros2_then_passes_on_the_double(self):
        root = self.rover()
        err = self.home.json("adapter", "test", str(root), "--level", "dry", expect=1)
        self.assertEqual(err["code"], "ros2_unavailable")
        with ros2_double() as graph:
            code, text, stderr = self.home.run("adapter", "test", str(root), "--level", "dry", "--out",
                                               str(self.tmp / "dry.json"))
            self.assertEqual(code, 0, text + stderr)
            last = graph.published["/cmd_vel"][-1]
            self.assertEqual((last.linear.x, last.angular.z), (0.0, 0.0))        # nothing moved
        self.assertIn("dry（不运动）：通过", text)
        self.assertIn("不等于现场验收证据", text)
        self.assertIn("evidence_kind=simulation", text)              # the double is use_sim_time → simulated
        report = json.loads((self.tmp / "dry.json").read_text(encoding="utf-8"))
        self.assertTrue(report["dry_ok"])
        self.assertEqual(report["evidence_kind"], "simulation")

    def test_incomplete_package_fails_static(self):
        out = self.home.json("adapter", "new", "acme_rover", "--out", str(self.tmp / "todo"), "--family", "custom",
                             "--caps", "safety.stop,system.health,sensor.battery",
                             "--param", "safety.stop.hardware_estop=false")
        self.assertEqual(sorted(out["mandatory_todo"]), ["safety.stop", "system.health"])
        report = self.home.json("adapter", "test", out["path"], expect=1)
        self.assertFalse(report["static_ok"])
        err = self.home.json("adapter", "test", out["path"], "--level", "dry", expect=1)
        self.assertEqual(err["code"], "adapter_incomplete")
        code, text, _ = self.home.run("adapter", "test", out["path"])
        self.assertEqual(code, 1)
        self.assertIn("结论：static 级别未通过", text)

    def test_motion_requires_authorize_before_loading(self):
        with mock.patch.object(adapters, "load_adapter") as loader:
            err = self.home.json("adapter", "test", "sim_quadruped", "--level", "motion", expect=1)
            self.assertEqual(err["code"], "authorization_required")
            with self.assertRaises(NeuxMindError) as ctx:
                adapters.test_adapter("sim_arm", "motion")
            self.assertEqual(ctx.exception.code, "authorization_required")
            loader.assert_not_called()
        code, _, _ = self.home.run("adapter", "test", "sim_quadruped", "--level", "extreme")
        self.assertEqual(code, 2)                                      # argparse usage error
        with self.assertRaises(NeuxMindError) as ctx:
            adapters.test_adapter("sim_quadruped", "extreme")
        self.assertEqual(ctx.exception.code, "invalid_level")

    def test_builtin_sim_bodies_all_levels(self):
        report = self.home.json("adapter", "test", "sim_quadruped", "--level", "motion", "--authorize",
                                "--time-scale", "20")
        self.assertTrue(report["static_ok"] and report["dry_ok"] and report["motion_ok"],
                        [c for c in report["cases"] if c["status"] == "failed"])
        self.assertEqual(report["adapter"], {"kind": "builtin", "adapter": "sim_quadruped"})
        self.assertFalse(report["final_latched"])
        self.assertTrue(self.home.json("adapter", "test", "sim_arm", "--level", "dry", "--time-scale", "20")["dry_ok"])
        self.assertEqual(self.home.json("adapter", "test", "no/such/dir", expect=1)["code"], "adapter_not_found")


# ======================================================================= nx robot conformance
class RobotConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dog = RuntimeServer("quadruped")

    @classmethod
    def tearDownClass(cls):
        cls.dog.close()

    def setUp(self):
        self.home = CliHome()
        self.addCleanup(self.home.close)
        self.home.json("robot", "add", "dog", self.dog.url, "--token", self.dog.token)
        self.robot = RobotClient(self.dog.url, self.dog.token)
        if self.robot.status().get("estop_latched"):
            self.robot.reset(authorize=True)

    def test_dry_wait_text_and_json(self):
        code, text, err = self.home.run("robot", "conformance", "dog", "--level", "dry", "--wait")
        self.assertEqual(code, 0, text + err)
        self.assertIn("已在 dog【仿真】 上开始一致性测试 cf-", text)
        self.assertIn("dry（不运动）：通过", text)
        self.assertIn("结论：dry 级别通过", text)
        self.assertRegex(text, r"base\.velocity\s+底盘速度\s+通过\s+通过\s+-")
        # Since runtime b0e53c7 posture has a non-moving dry check (refused values, refused while latched).
        self.assertRegex(text, r"legged\.posture\s+.*通过\s+通过")
        self.assertIn("未声明（不支持或尚未实现", text)
        self.assertNotRegex(text, r"(?m)^arm\.joint_position\s")          # only declared capabilities are rows
        self.assertIn("arm.joint_position", text.split("未声明（不支持或尚未实现")[1])
        self.assertIn("不等于现场验收证据（field_acceptance=false，evidence_kind=simulation）", text)
        report = self.home.json("robot", "conformance", "dog", "--level", "static", "--wait")
        self.assertEqual(report["schema"], "conformance.report.v1")    # --json: the raw report
        self.assertEqual(report["target"]["kind"], "runtime")
        self.assertTrue(report["static_ok"])
        self.assertIsNone(report["dry_ok"])

    def test_without_wait_then_by_id(self):
        started = self.home.json("robot", "conformance", "dog", "--level", "dry")
        cid = started["conformance_id"]
        self.assertEqual(started["status"], "running")
        report = self.home.json("robot", "conformance", "dog", "--id", cid, "--wait")
        self.assertEqual(report["conformance_id"], cid)
        self.assertTrue(report["dry_ok"])
        self.assertIn(cid, [r["id"] for r in self.robot.conformance_runs()])
        self.assertEqual(self.home.json("robot", "conformance", "dog", "--id", "cf-nope", expect=1)["code"],
                         "conformance_not_found")

    def test_motion_authorization_is_local(self):
        sent = []
        real = robot_module.request_json

        def spy(method, url, **kw):
            sent.append((method, url))
            return real(method, url, **kw)

        with mock.patch.object(robot_module, "request_json", spy):
            err = self.home.json("robot", "conformance", "dog", "--level", "motion", "--wait", expect=1)
            with self.assertRaises(NeuxMindError) as ctx:
                self.robot.start_conformance("motion")
            with self.assertRaises(NeuxMindError) as bad:
                self.robot.start_conformance("extreme")
        self.assertEqual(err["code"], "authorization_required")
        self.assertEqual(ctx.exception.code, "authorization_required")
        self.assertEqual(bad.exception.code, "invalid_level")
        self.assertEqual([u for _, u in sent if "/v2/conformance" in u], [])

    def test_motion_with_authorize(self):
        report = self.robot.run_conformance("motion", authorize=True, timeout=300)
        self.assertTrue(report["motion_ok"], [c for c in report["cases"] if c["status"] == "failed"])
        self.assertTrue(report["hardware_authorized"])
        self.assertFalse(self.robot.status()["estop_latched"])          # the suite released its own latch

    def test_prelatched_body_fails_honestly(self):
        self.robot.stop("operator pressed stop")
        try:
            code, text, err = self.home.run("robot", "conformance", "dog", "--level", "dry", "--wait")
            self.assertEqual(code, 1, text + err)
            self.assertIn("测试前急停已锁存", text)
            self.assertIn("结论：dry 级别未通过", text)
            self.assertTrue(self.robot.status()["estop_latched"])        # not reset by the suite
        finally:
            self.robot.reset(authorize=True)

    def test_busy_and_old_runtime(self):
        first = self.robot.start_conformance("dry")
        with self.assertRaises(NeuxMindError) as ctx:
            self.robot.start_conformance("dry")
        self.assertEqual(ctx.exception.code, "conformance_busy")
        self.assertIn("同一时刻只允许一个", str(ctx.exception))
        self.robot.wait_conformance(first["conformance_id"], timeout=120)
        old = NeuxMindError("Not Found", code="not_found", status=404)
        with mock.patch.object(robot_module, "request_json", side_effect=old):
            with self.assertRaises(NeuxMindError) as ctx:
                self.robot.start_conformance("dry")
        self.assertEqual(ctx.exception.code, "conformance_unsupported")
        self.assertIn("--mode client", str(ctx.exception))

    def test_wait_timeout_and_failed_record(self):
        running = {"id": "cf-1", "status": "running", "progress": 0.2}
        with mock.patch.object(RobotClient, "conformance", return_value=running):
            with self.assertRaises(NeuxMindError) as ctx:
                self.robot.wait_conformance("cf-1", timeout=0.05, interval=0.01)
        self.assertEqual(ctx.exception.code, "wait_timeout")
        failed = {"id": "cf-2", "status": "unknown", "error": "运行时重启"}
        with mock.patch.object(RobotClient, "start_conformance", return_value={"conformance_id": "cf-2"}), \
                mock.patch.object(RobotClient, "conformance", return_value=failed):
            with self.assertRaises(NeuxMindError) as ctx:
                self.robot.run_conformance("dry")
            err = self.home.json("robot", "conformance", "dog", "--id", "cf-2", expect=1)
        self.assertEqual(ctx.exception.code, "conformance_unknown")
        self.assertEqual(err["code"], "conformance_unknown")


# ======================================================================= upload + require_level
class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dog = RuntimeServer("quadruped")
        cls.arm = RuntimeServer("arm")
        cls.cloud = CloudServer()
        cls.ops = CloudClient(cls.cloud.url)
        reg = cls.ops.register(OPERATOR_EMAIL, PASSWORD)
        cls.ops.verify_email(reg["dev_verification_token"])
        cards = [
            {"id": "gait", "name": "Gait (echo double)", "source": "closed", "kind": "policy", "license": "test",
             "pricing": {"type": "free"}, "requires_capabilities": ["base.velocity"], "upstream": {"provider": "echo"}},
            {"id": "posture", "name": "Posture (echo double)", "source": "closed", "kind": "policy", "license": "test",
             "pricing": {"type": "free"}, "requires_capabilities": ["legged.posture"], "upstream": {"provider": "echo"}},
        ]
        for card in cards:
            cls.ops._request("POST", "/admin/models", card)

    @classmethod
    def tearDownClass(cls):
        cls.dog.close()
        cls.arm.close()
        cls.cloud.close()

    def developer(self, email: str) -> CliHome:
        home = CliHome()
        self.addCleanup(home.close)
        home.json("config", "set", "cloud_url", self.cloud.url)
        reg = home.json("register", "--email", email, "--password", PASSWORD)
        home.json("verify-email", reg["dev_verification_token"])
        home.json("robot", "add", "dog", self.dog.url, "--token", self.dog.token)
        home.json("robot", "add", "arm", self.arm.url, "--token", self.arm.token)
        return home

    def cloud_client(self, home: CliHome) -> CloudClient:
        cfg = json.loads((home.path / "config.json").read_text(encoding="utf-8"))
        return CloudClient(self.cloud.url, cfg["cloud_token"])

    def test_upload_then_require_level(self):
        home = self.developer("conf-upload@sdk.test")
        before = home.json("models", "list", "--robot", "dog", "--require-level", "dry", expect=1)
        self.assertEqual(before["code"], "conformance_required")
        self.assertIn("nx robot conformance", before["error"])
        code, text, err = home.run("robot", "conformance", "dog", "--level", "dry", "--upload")
        self.assertEqual(code, 0, text + err)
        self.assertIn("已上传摘要到云端设备群", text)
        self.assertIn("dry_ok=True", text)
        cfg = json.loads((home.path / "config.json").read_text(encoding="utf-8"))
        robot_id = cfg["robots"]["dog"]["cloud_id"]
        view = self.cloud_client(home).robot(robot_id)
        summary = view["conformance"]
        self.assertEqual((summary["level"], summary["dry_ok"], summary["simulated"], summary["evidence_kind"]),
                         ("dry", True, True, "simulation"))
        self.assertIs(summary["field_acceptance"], False)
        self.assertEqual(summary["capabilities"]["base.velocity"]["levels"]["dry"]["status"], "passed")
        plain = {m["id"] for m in home.json("models", "list", "--robot", "dog")["models"]}
        self.assertEqual(plain, {"gait", "posture"})
        listed = home.json("models", "list", "--robot", "dog", "--require-level", "dry")
        # posture now passes dry (runtime b0e53c7), so both models stay listed;
        # the not-verified filtering itself is covered by cloud/tests.
        self.assertEqual({m["id"] for m in listed["models"]}, {"gait", "posture"})
        self.assertNotIn("legged.posture", listed.get("not_verified", []))
        code, text, _ = home.run("models", "list", "--robot", "dog", "--require-level", "dry")
        self.assertIn("只算一致性报告 cf-", text)
        # --json with --upload: the raw report plus the upload result.
        both = home.json("robot", "conformance", "dog", "--level", "static", "--upload")
        self.assertEqual(both["report"]["schema"], "conformance.report.v1")
        self.assertEqual(both["upload"]["conformance"]["level"], "static")
        self.assertEqual(home.json("models", "list", "--robot", "dog", "--require-level", "dry", expect=1)["code"],
                         "conformance_required")                            # latest report is static only

    def test_old_report_is_not_uploaded_for_a_changed_manifest(self):
        """Review Q4-12: ``--id <old> --upload`` must not bind a report to a manifest it was not run against."""
        home = self.developer("conf-digest@sdk.test")
        started = home.json("robot", "conformance", "dog", "--level", "static")
        real = RobotClient(self.dog.url, self.dog.token).manifest()
        changed = json.loads(json.dumps(real))
        changed["adapter_version"] = "9.9.9"
        with mock.patch.object(RobotClient, "manifest", return_value=changed):
            err = home.json("robot", "conformance", "dog", "--id", started["conformance_id"], "--upload", expect=1)
        self.assertEqual(err["code"], "conformance_manifest_mismatch")
        self.assertIn("没有向云端发送任何请求", err["error"])
        self.assertEqual(self.cloud_client(home).robots(), [])          # nothing registered or uploaded
        ok = home.json("robot", "conformance", "dog", "--id", started["conformance_id"], "--upload")
        self.assertTrue(ok["upload"]["conformance"]["digest_verified"])

    def test_first_upload_registers_the_robot(self):
        home = self.developer("conf-first@sdk.test")
        cfg = json.loads((home.path / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("cloud_id", cfg["robots"]["arm"])
        both = home.json("robot", "conformance", "arm", "--level", "static", "--upload")
        cfg = json.loads((home.path / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["robots"]["arm"]["cloud_id"], both["upload"]["robot_id"])
        self.assertEqual(self.cloud_client(home).robot(both["upload"]["robot_id"])["instance_id"],
                         both["report"]["robot"]["instance_id"])

    def test_client_refusals_and_skill_check(self):
        home = self.developer("conf-client@sdk.test")
        cloud = self.cloud_client(home)
        robot = RobotClient(self.arm.url, self.arm.token)
        report = robot.run_conformance("dry", timeout=120)
        robot_id = cloud.register_robot(robot.manifest())["id"]
        forged = json.loads(json.dumps(report))
        forged["evidence_kind"] = "device_run"
        with self.assertRaises(NeuxMindError) as ctx:
            cloud.upload_conformance(robot_id, forged)
        self.assertEqual(ctx.exception.code, "device_run_refused")
        self.assertIn("不是现场运行证据", str(ctx.exception))
        dog_report = RobotClient(self.dog.url, self.dog.token).run_conformance("static", timeout=120)
        with self.assertRaises(NeuxMindError) as ctx:
            cloud.upload_conformance(robot_id, dog_report)
        self.assertEqual(ctx.exception.code, "conformance_robot_mismatch")
        stored = cloud.upload_conformance(robot_id, report)["conformance"]
        self.assertTrue(stored["dry_ok"])
        skill = {"schema": "skill.package.v1", "id": "reach", "version": "0.1.0", "entry": "skill.py:run",
                 "requires": [{"id": "arm.joint_position"}, {"id": "joint.state"}], "files": {"skill.py": "x"}}
        result = cloud.check_skill(robot_id, skill, require_level="dry")
        self.assertTrue(result["compatible"], result)
        compat = cloud.compatible_models(robot_id=robot_id, require_level="dry")
        self.assertIn("gripper.position", compat["usable_capabilities"])
        with self.assertRaises(NeuxMindError):
            cloud.compatible_models()


# ======================================================================= install progress (§5.1 bypass removed)
class InstallProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkgs = PackageServer()
        cls.dog = RuntimeServer("quadruped", models=True)
        cls.robot = RobotClient(cls.dog.url, cls.dog.token)

    @classmethod
    def tearDownClass(cls):
        cls.dog.close()
        cls.pkgs.close()

    def _install(self, name: str):
        url, sha = self.pkgs.add(name, size=1 << 20, slow=0.03)
        return self.robot.install_model(url, sha, "onnxruntime", model_id=name, authorize=True)["run_id"]

    def test_progress_from_the_run_record(self):
        run_id = self._install("p1.onnx")
        updates, paths = [], []
        real = robot_module.request_json

        def spy(method, url, **kw):
            paths.append(url)
            return real(method, url, **kw)

        with mock.patch.object(robot_module, "request_json", spy):
            record = _wait_install(self.robot, run_id, 120, 0.05, updates.append)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual([u for u in paths if "/events" in u], [])           # no event-stream detour
        running = [u for u in updates if u["status"] in ("queued", "running")]
        fractions = [u["progress"] for u in running]
        self.assertGreaterEqual(len([f for f in fractions if 0 < f < 1]), 3, fractions)
        self.assertEqual(fractions, sorted(fractions))
        self.assertTrue(all(f <= 0.99 for f in fractions))
        self.assertEqual(running[-1]["total"], 1 << 20)
        self.assertEqual(updates[-1]["progress"], 1.0)
        self.assertIn("feedback", record["progress_detail"])

    def test_old_runtime_falls_back_to_events(self):
        run_id = self._install("p2.onnx")
        real_run = RobotClient.run

        def old_run(client, rid):                         # what a runtime before round 04 returned
            record = real_run(client, rid)
            record.pop("progress_detail", None)
            if record.get("status") != "succeeded":
                record["progress"] = 0.0
            return record

        updates = []
        with mock.patch.object(RobotClient, "run", old_run):
            record = _wait_install(self.robot, run_id, 120, 0.05, updates.append)
        self.assertEqual(record["status"], "succeeded")
        fractions = [u["progress"] for u in updates if u["status"] in ("queued", "running")]
        self.assertGreaterEqual(len([f for f in fractions if 0 < f < 1]), 3, fractions)
        self.assertTrue(any(isinstance(u["bytes"], int) for u in updates))

    def test_install_progress_helper(self):
        state = {"bytes": None, "total": None}
        record = {"status": "running", "progress": 0.5, "progress_detail": {"feedback": {"bytes": 5, "total": 10}}}
        self.assertEqual(install_progress(self.robot, "r", record, 0, state), (0.5, 0))
        self.assertEqual(state, {"bytes": 5, "total": 10})
        record = {"status": "running", "progress": 1.0, "progress_detail": {"capability": "model.install"}}
        self.assertEqual(install_progress(self.robot, "r", record, 0, state)[0], 0.99)   # not done until succeeded
        self.assertEqual(install_progress(self.robot, "r", {"status": "succeeded", "progress": 0.4,
                                                            "progress_detail": {}}, 0, state)[0], 1.0)


if __name__ == "__main__":
    unittest.main()
