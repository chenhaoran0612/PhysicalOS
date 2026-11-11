"""`nx` end to end against real runtime gateways (in-process main(argv), temp NEUXMIND_HOME)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _support
from _support import SDK_DIR, SLEEPER, CliHome, RuntimeServer, make_skill

from neuxmind import NeuxMindError
from neuxmind.cli import parse_param


class ParamParsingTests(unittest.TestCase):
    def test_json_then_string(self):
        self.assertEqual(parse_param("n=3"), ("n", 3))
        self.assertEqual(parse_param("w=[[1,0],[1,1]]"), ("w", [[1, 0], [1, 1]]))
        self.assertEqual(parse_param("flag=true"), ("flag", True))
        self.assertEqual(parse_param("name=hello world"), ("name", "hello world"))
        self.assertEqual(parse_param("eq=a=b"), ("eq", "a=b"))
        with self.assertRaises(NeuxMindError):
            parse_param("novalue")


class CliRobotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dog = RuntimeServer("quadruped")
        cls.arm = RuntimeServer("arm")

    @classmethod
    def tearDownClass(cls):
        cls.dog.close()
        cls.arm.close()

    def setUp(self):
        self.home = CliHome()
        self.addCleanup(self.home.close)
        self.work = Path(tempfile.mkdtemp(prefix="nx-sdk-cli-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.home.json("robot", "add", "dog", self.dog.url, "--token", self.dog.token)
        self.home.json("robot", "add", "arm", self.arm.url, "--token", self.arm.token)

    def nx(self, *argv):
        return self.home.run(*argv)

    def test_config_file_and_robot_listing(self):
        path = self.home.path / "config.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.home.path.stat().st_mode), 0o700)
        data = json.loads(path.read_text())
        self.assertEqual(data["default_robot"], "dog")
        self.assertEqual(data["robots"]["arm"]["token"], self.arm.token)
        listing = self.home.json("robot", "list", "--check")
        self.assertEqual([(r["name"], r["simulated"], r["online"]) for r in listing["robots"]],
                         [("dog", True, True), ("arm", True, True)])
        self.assertNotIn("token", json.dumps(listing))
        shown = self.home.json("config", "show")
        self.assertNotIn(self.dog.token, json.dumps(shown))
        code, _out, err = self.nx("robot", "add", "bad", self.dog.url, "--token", "wrong-token-" + "x" * 20)
        self.assertEqual(code, 1)
        self.assertIn("unauthorized", err)

    def test_caps_table_marks_simulation(self):
        code, out, _ = self.nx("robot", "caps", "arm")
        self.assertEqual(code, 0)
        self.assertIn("simulated=true", out)
        self.assertIn("arm.joint_position", out)
        self.assertIn("dof=6", out)
        data = self.home.json("robot", "caps", "dog")
        self.assertTrue(data["simulated"])
        self.assertIn("base.navigate_to", [c["id"] for c in data["manifest"]["capabilities"]])

    def test_new_check_install_run_stop_reset(self):
        code, out, _ = self.nx("skill", "new", "cli-patrol", "--template", "patrol", "--dir", str(self.work / "cli-patrol"))
        self.assertEqual(code, 0, out)
        skill_dir = str(self.work / "cli-patrol")

        # Cross-body check: compatible with the quadruped, not with the arm.
        report = self.home.json("skill", "check", skill_dir, "--robot", "dog", "--robot", "arm", expect=1)
        dog, arm = report["results"]
        self.assertTrue(dog["compatible"])
        self.assertFalse(arm["compatible"])
        self.assertEqual(arm["missing"], ["base.navigate_to"])
        code, out, _ = self.nx("skill", "check", skill_dir, "--all")
        self.assertEqual(code, 1)
        self.assertIn("缺少必需能力 base.navigate_to", out)
        offline = self.home.json("skill", "check", skill_dir, "--body", "quadruped")
        self.assertTrue(offline["compatible"])

        self.assertEqual(self.nx("skill", "install", skill_dir, "--robot", "arm")[0], 1)
        installed = self.home.json("skill", "install", skill_dir, "--robot", "dog")
        self.assertTrue(installed["installed"])
        self.assertEqual(self.home.json("skill", "list", "--robot", "dog")["skills"][0]["id"], "cli-patrol")

        # Without --authorize nothing is sent.
        refused = self.home.json("skill", "run", "cli-patrol", "--robot", "dog", expect=1)
        self.assertEqual(refused["code"], "authorization_required")
        self.assertEqual(self.dog.runtime.runner.list(), [])

        run = self.home.json("skill", "run", "cli-patrol", "--robot", "dog", "--param", "waypoints=[[1,0],[1,1]]",
                             "--param", "loops=1", "--authorize", "--wait")
        self.assertEqual(run["status"], "succeeded", run)
        self.assertEqual(run["result"]["reached"], 2)
        self.assertTrue(run["simulated"])
        status = self.home.json("skill", "status", run["id"], "--robot", "dog")
        self.assertEqual(status["status"], "succeeded")
        code, out, _ = self.nx("skill", "status", "--robot", "dog")
        self.assertIn(run["id"], out)

        # Stop needs no authorization and latches; runs are refused until reset --authorize.
        stopped = self.home.json("stop", "dog")
        self.assertTrue(stopped["accepted"])
        latched = self.home.json("skill", "run", "cli-patrol", "--robot", "dog", "--authorize", expect=1)
        self.assertEqual(latched["code"], "estop_latched")
        self.assertEqual(self.home.json("reset", "dog", expect=1)["code"], "authorization_required")
        self.assertTrue(self.dog.runtime.supervisor.latched)
        self.assertFalse(self.home.json("reset", "dog", "--authorize")["latched"])
        code, out, _ = self.nx("skill", "run", "cli-patrol", "--robot", "dog", "--param", "waypoints=[[0,0]]",
                               "--authorize", "--wait")
        self.assertEqual(code, 0, out)
        self.assertIn("成功", out)

    def test_run_text_progress_cancel_and_failures(self):
        sleeper = make_skill(self.work, "cli-sleeper", SLEEPER, requires=("base.velocity",), timeout_s=600)
        self.home.json("skill", "install", str(sleeper), "--robot", "dog")
        started = self.home.json("skill", "run", "cli-sleeper", "--robot", "dog", "--authorize")
        cancelled = self.home.json("skill", "cancel", started["run_id"], "--robot", "dog")
        self.assertTrue(cancelled["accepted"])
        failing = make_skill(self.work, "cli-fail", "def run(ctx):\n    raise RuntimeError('boom')\n")
        self.home.json("skill", "install", str(failing), "--robot", "arm")
        code, out, _ = self.nx("skill", "run", "cli-fail", "--robot", "arm", "--authorize", "--wait")
        self.assertEqual(code, 1)
        self.assertIn("boom", out)
        bad = self.home.json("skill", "run", "cli-fail", "--robot", "arm", "--param", "x=1", "--authorize", expect=1)
        self.assertEqual(bad["code"], "bad_request")

    def test_usage_errors(self):
        self.assertEqual(self.nx("skill", "new", "x", "--template", "nope")[0], 2)
        self.assertEqual(self.nx()[0], 2)
        self.assertEqual(self.home.json("robot", "caps", "ghost", expect=1)["code"], "robot_not_found")


class CliProcessTests(unittest.TestCase):
    """Real processes: the installed entry point and `nx sim up` launching the runtime CLI."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="nx-sdk-proc-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = dict(os.environ, NEUXMIND_HOME=str(self.home), PYTHONPATH=str(SDK_DIR))

    def nx(self, *argv, check=True):
        proc = subprocess.run([sys.executable, "-m", "neuxmind", *argv], env=self.env, capture_output=True,
                              text=True, timeout=60)
        if check and proc.returncode != 0:
            raise AssertionError(proc.stdout + proc.stderr)
        return proc

    def test_sim_up_check_run_down(self):
        self.assertIn("nx 0.1.0", self.nx("--version").stdout)
        up = json.loads(self.nx("sim", "up", "--body", "quadruped", "--port", "0", "--time-scale", "20", "--json").stdout)
        self.addCleanup(lambda: self.nx("sim", "down", check=False))
        self.assertTrue(up["simulated"])
        self.assertGreaterEqual(len(up["token"]), 24)
        up_arm = json.loads(self.nx("sim", "up", "--body", "arm", "--port", "0", "--time-scale", "20", "--json").stdout)
        config = json.loads((self.home / "config.json").read_text())
        self.assertEqual(config["default_robot"], "sim-quadruped")       # the first sim stays default
        self.assertEqual(config["robots"]["sim-arm"]["url"], up_arm["url"])

        skill_dir = self.home / "proc-status"
        self.nx("skill", "new", "proc-status", "--template", "status", "--dir", str(skill_dir))
        report = json.loads(self.nx("skill", "check", str(skill_dir), "--all", "--json").stdout)
        self.assertTrue(report["compatible"])
        self.nx("skill", "install", str(skill_dir), "--robot", "sim-arm")
        run = json.loads(self.nx("skill", "run", "proc-status", "--robot", "sim-arm", "--authorize", "--wait",
                                 "--json").stdout)
        self.assertEqual(run["result"]["family"], "manipulator")
        self.assertIn("已停止", self.nx("stop", "sim-arm").stdout)
        sims = json.loads(self.nx("sim", "list", "--json").stdout)["sims"]
        self.assertEqual(sorted(s["name"] for s in sims if s["alive"]), ["sim-arm", "sim-quadruped"])
        down = json.loads(self.nx("sim", "down", "--json").stdout)
        self.assertEqual(len(down["stopped"]), 2)
        self.assertEqual(json.loads(self.nx("sim", "list", "--json").stdout)["sims"], [])


if __name__ == "__main__":
    unittest.main()
