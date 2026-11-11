"""End to end (§6.3/§6.4): scaffold → adapter test → serve → robot conformance → upload → require_level.

Real processes: ``nx`` (in process), a separate ``neuxmind-runtime serve`` process loading the
generated ROS 2 adapter, and the cloud app (uvicorn). The ROS 2 side is the rclpy test double from
``runtime/tests/ros2_double`` (NOT ROS 2; its use_sim_time makes the body ``simulated: true``).
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import _support
from _support import OPERATOR_EMAIL, PASSWORD, REPO, CliHome, CloudServer
from test_adapters_conformance import ROS2_DOUBLE, ROVER_ARGS, ros2_double

from neuxmind import adapters
from neuxmind.cloud import CloudClient

TOKEN = "e2e-token-" + "x" * 24


def _readline_within(stream, timeout: float) -> str:
    box: list[str] = []
    thread = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    thread.start()
    thread.join(timeout)
    if not box:
        raise AssertionError(f"运行时 {timeout} s 内没有输出监听地址")
    return box[0]


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = CloudServer()
        ops = CloudClient(cls.cloud.url)
        reg = ops.register(OPERATOR_EMAIL, PASSWORD)
        ops.verify_email(reg["dev_verification_token"])
        for card in ({"id": "drive-assist", "name": "Drive assist (echo double)", "source": "closed",
                      "kind": "policy", "license": "test", "pricing": {"type": "free"},
                      "requires_capabilities": ["base.velocity", "base.odometry"], "upstream": {"provider": "echo"}},
                     {"id": "nav-coach", "name": "Nav coach (echo double)", "source": "closed", "kind": "policy",
                      "license": "test", "pricing": {"type": "free"},
                      "requires_capabilities": ["base.navigate_to"], "upstream": {"provider": "echo"}}):
            ops._request("POST", "/admin/models", card)

    @classmethod
    def tearDownClass(cls):
        cls.cloud.close()

    def setUp(self):
        self.home = CliHome()
        self.addCleanup(self.home.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="nx-sdk-e2e-"))
        self.addCleanup(_support._rmtree, self.tmp)
        self.addCleanup(adapters._purge, "acme_rover")

    def serve(self, root: Path) -> str:
        env = {**os.environ, "BRAIN_EDGE_TOKEN": TOKEN,
               "PYTHONPATH": os.pathsep.join([str(root), str(ROS2_DOUBLE), str(REPO / "runtime"), str(REPO / "spec")])}
        proc = subprocess.Popen([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter",
                                 "acme_rover.adapter:AcmeRoverAdapter", "--port", "0", "--state-dir",
                                 str(self.tmp / "state"), "--sandbox", "inprocess"],
                                cwd=str(self.tmp), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
            proc.stdout.close()
            proc.stderr.close()

        self.addCleanup(cleanup)
        line = _readline_within(proc.stdout, 30)
        url = line.strip().rsplit(" ", 1)[-1]
        self.assertTrue(url.startswith("http://"), line + proc.stderr.read() if proc.poll() is not None else line)
        return url

    def test_scaffold_test_serve_conformance_upload_require_level(self):
        h = self.home
        # ① scaffold
        new = h.json("adapter", "new", "acme_rover", "--out", str(self.tmp / "acme_rover"), *ROVER_ARGS)
        root = Path(new["path"])
        # ② local conformance, in process (static offline; dry on the rclpy double)
        self.assertTrue(h.json("adapter", "test", str(root))["static_ok"])
        with ros2_double():
            self.assertTrue(h.json("adapter", "test", str(root), "--level", "dry")["dry_ok"])
        # ③ serve it with the real runtime (separate process)
        url = self.serve(root)
        h.json("config", "set", "cloud_url", self.cloud.url)
        reg = h.json("register", "--email", "e2e@sdk.test", "--password", PASSWORD)
        h.json("verify-email", reg["dev_verification_token"])
        h.json("robot", "add", "rover", url, "--token", TOKEN)
        # ④ robot conformance through /v2/conformance, with motion (simulated double, authorized)
        code, text, err = h.run("robot", "conformance", "rover", "--level", "motion", "--authorize", "--upload")
        self.assertEqual(code, 0, text + err)
        self.assertIn("motion（最小动作）：通过", text)
        self.assertIn("已上传摘要到云端设备群", text)
        self.assertIn("不等于现场验收证据", text)
        # ⑤ the fleet record has the summary; require_level=dry only counts verified capabilities
        cfg = json.loads((h.path / "config.json").read_text(encoding="utf-8"))
        robot = CloudClient(self.cloud.url, cfg["cloud_token"]).robot(cfg["robots"]["rover"]["cloud_id"])
        summary = robot["conformance"]
        self.assertEqual((summary["level"], summary["motion_ok"], summary["evidence_kind"]),
                         ("motion", True, "simulation"))
        self.assertFalse(summary["capabilities"]["sensor.camera.rgb"]["declared"])      # TODO in the scaffold
        listed = h.json("models", "list", "--robot", "rover", "--require-level", "dry")
        self.assertEqual([m["id"] for m in listed["models"]], ["drive-assist"])     # no navigate_to on the rover
        self.assertEqual(sorted(listed["usable_capabilities"]),
                         ["base.odometry", "base.velocity", "safety.stop", "sensor.battery", "system.health"])
        self.assertEqual(listed["not_verified"], [])


if __name__ == "__main__":
    unittest.main()
