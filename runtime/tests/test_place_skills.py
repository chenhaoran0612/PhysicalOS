"""Issue #38: the three skills (去指定地点 / 巡检 / 无图自由走) from install to result.

Twice: on the simulated quadruped (made-up places, a stand-in wander), and on the ROS 2 bridge of the
rclpy test double with stand-in programs in a temporary directory. The second shows the whole chain a
real body uses (skill → capability bus → safety supervisor → adapter → program on the body) with
nothing real at the end of it. Skills run in-process here so the file also runs on macOS; the
``Sandboxed`` class repeats the first part in skill processes (Linux).
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import _support
from _support import EXAMPLES, RuntimeFixture, wait_until

from neuxmind_runtime.adapters import ros2, sim_quadruped
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import BadRequest, Incompatible, NotAuthorized
from neuxmind_runtime.sandbox import INPROCESS
from neuxmind_runtime.skills import load_package_dir

SKILLS = ("goto_place", "patrol_places", "free_walk")


def example(name: str) -> dict:
    return load_package_dir(EXAMPLES / name)


class OnTheSimulatedBody(unittest.TestCase):
    SANDBOX = INPROCESS

    def setUp(self):
        self.fx = RuntimeFixture("quadruped", time_scale=40.0, sandbox=self.SANDBOX)
        for name in SKILLS:
            self.fx.install(example(name))

    def tearDown(self):
        self.fx.close()

    def near(self, place: str) -> bool:
        x, y = sim_quadruped.PLACES[place]
        pose = self.fx.runtime.bus.read("base.odometry")["pose"]
        return math.hypot(pose[0] - x, pose[1] - y) < 0.2

    def test_go_to_the_meeting_room(self):
        record = self.fx.wait(self.fx.run("goto_place", {"place": "会议室"}), 20)
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual(record["result"]["place"], "会议室")
        self.assertEqual(record["progress"], 1.0)
        self.assertTrue(self.near("会议室"))

    def test_go_upstairs(self):
        record = self.fx.wait(self.fx.run("goto_place", {"place": "二楼", "max_speed": 0.5}), 20)
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertTrue(self.near("二楼"))

    def test_the_place_is_one_of_the_two(self):
        for bad in ({}, {"place": "天台"}, {"place": 3}, {"place": "会议室", "max_speed": 9}):
            with self.subTest(bad=bad):
                with self.assertRaises(BadRequest):
                    self.fx.run("goto_place", bad)

    def test_every_run_needs_authorization(self):
        for skill, parameters in (("goto_place", {"place": "会议室"}), ("patrol_places", {}), ("free_walk", {})):
            with self.subTest(skill=skill):
                with self.assertRaises(NotAuthorized):
                    self.fx.runtime.run_skill(skill, {"request_id": "na-" + skill, "parameters": parameters})

    def test_inspection_round_visits_both_and_comes_back(self):
        record = self.fx.wait(self.fx.run("patrol_places"), 40)
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual(record["result"], {"visited": ["会议室", "二楼", "出发点"], "loops": 1, "returned_to": "出发点"})
        self.assertTrue(self.near("出发点"))
        arrived = [e["place"] for e in record["events"] if e["type"] == "log" and e.get("message") == "到达"]
        self.assertEqual(arrived, ["会议室", "二楼", "出发点"])

    def test_inspection_round_in_another_order_without_returning(self):
        record = self.fx.wait(self.fx.run("patrol_places", {"places": "会议室, 二楼", "return_to": "", "loops": 2}), 60)
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual(record["result"]["visited"], ["会议室", "二楼", "会议室", "二楼"])
        self.assertIsNone(record["result"]["returned_to"])
        self.assertTrue(self.near("二楼"))

    def test_a_place_the_body_does_not_know_fails_before_the_first_step(self):
        record = self.fx.wait(self.fx.run("patrol_places", {"places": "二楼、天台"}), 10)
        self.assertEqual(record["status"], "failed")
        self.assertIn("天台", record["error"])
        self.assertTrue(self.near("出发点"))

    def test_free_walk_for_a_while(self):
        record = self.fx.wait(self.fx.run("free_walk", {"duration_s": 20, "radius": 1.5}), 20)
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertTrue(record["result"]["completed"])
        self.assertGreater(record["result"]["travelled"], 1.0)

    def test_free_walk_longer_than_the_body_allows_says_so(self):
        record = self.fx.wait(self.fx.run("free_walk", {"duration_s": 900}), 10)
        self.assertEqual(record["status"], "failed")
        self.assertIn("最多自由走", record["error"])

    def test_stop_during_a_round_ends_it_and_latches(self):
        self.fx.close()
        self.fx = RuntimeFixture("quadruped", time_scale=1.0, sandbox=self.SANDBOX)
        self.fx.install(example("patrol_places"))
        run_id = self.fx.run("patrol_places")
        wait_until(lambda: abs(self.fx.runtime.bus.read("base.odometry")["pose"][1]) > 0.05, 10)
        self.fx.runtime.emergency_stop("test")
        record = self.fx.wait(run_id, 5)
        self.assertEqual(record["status"], "stopped")
        self.assertEqual(self.fx.runtime.bus.read("base.odometry")["velocity"], [0.0, 0.0, 0.0])
        self.assertTrue(self.fx.runtime.supervisor.latched)

    def test_an_arm_cannot_install_them(self):
        arm = RuntimeFixture("arm", sandbox=self.SANDBOX)
        try:
            for name in SKILLS:
                with self.subTest(name=name):
                    with self.assertRaises(Incompatible):
                        arm.install(example(name))
        finally:
            arm.close()


class Sandboxed(OnTheSimulatedBody):
    """The same, with every skill in its own process (the default on a device)."""
    SANDBOX = "process"


PROGRAM = r'''
import json, os, signal, sys, time
args = sys.argv[1:]
with open("calls.jsonl", "a") as fh:
    fh.write(json.dumps({"name": args[0], "args": args[1:]}) + "\n")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
if "--sleep" in args:
    time.sleep(float(args[args.index("--sleep") + 1]))
print("exit route done", flush=True)
'''


class OnTheRos2BridgeWithStandInPrograms(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(os.path.realpath(self._tmp.name))
        (self.dir / "walk").mkdir()
        (self.dir / "walk" / "prog.py").write_text(PROGRAM)
        self._double = _support.ros2_double()
        self._double.__enter__()
        self.addCleanup(self._double.__exit__, None, None, None)
        cfg = _support.ros2_config()
        cfg["programs"] = self.programs()
        self.adapter = ros2.Ros2Adapter(cfg)
        self.runtime = EdgeRuntime(self.adapter, str(self.dir / "state"), sandbox=INPROCESS)
        self.addCleanup(self.close)
        for name in SKILLS:
            self.runtime.install_skill({"request_id": "install-" + name, "package": example(name)})

    def close(self):
        self.runtime.runner.stop_all("test teardown")
        self.runtime.close()

    def programs(self) -> dict:
        def prog(name, *args, **fields):
            out = {"title": name, "argv": [sys.executable, "prog.py", name, *args], "ok": "exit route done",
                   "timeout_s": 20}
            out.update(fields)
            return out
        down = prog("stairs-down", speed=0.3, enters="一楼")

        def ground(name):
            return {"region": "一楼", "routes": {"一楼": [prog("goto-" + name, "--cruise", "{max_speed}",
                                                              speed_range=[0.25, 0.6])],
                                                "二楼": [down, prog("goto-" + name)]}}
        wander = prog("wander", "--seconds", "{duration_s}", "--leash", "{radius}", timeout_margin_s=5)
        del wander["timeout_s"]
        return {"workdir": str(self.dir / "walk"), "stop_grace_s": 0.3, "start_region": "一楼",
                "state_file": str(self.dir / "region.json"),
                "places": {"出发点": ground("home"), "会议室": ground("meeting"),
                           "二楼": {"region": "二楼", "routes": {
                               "一楼": [prog("goto-stairs"), prog("stairs-up", speed=0.3, enters="二楼")], "二楼": []}}},
                "explore": {"regions": ["一楼"], "max_duration_s": 600, "max_radius": 10, "default_radius": 5,
                            "steps": [wander]}}

    def run_skill(self, skill, parameters=None, timeout=30):
        run_id = self.runtime.run_skill(skill, {"request_id": f"run-{skill}-{time.monotonic_ns()}",
                                                "parameters": parameters or {}, "hardware_authorized": True})["run_id"]
        return self.runtime.runner.wait(run_id, timeout)

    def names(self):
        path = self.dir / "walk" / "calls.jsonl"
        return [json.loads(line)["name"] for line in path.read_text().splitlines()] if path.exists() else []

    def test_the_body_says_which_places_it_knows(self):
        manifest = self.runtime.bus.manifest_copy()
        declared = {c["id"]: c.get("params", {}) for c in manifest["capabilities"]}
        self.assertEqual(declared["base.navigate_to_place"]["places"], ["出发点", "会议室", "二楼"])

    def test_go_to_the_meeting_room(self):
        record = self.run_skill("goto_place", {"place": "会议室"})
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual(self.names(), ["goto-meeting"])

    def test_inspection_round_runs_the_programs_in_the_order_of_the_site(self):
        record = self.run_skill("patrol_places", timeout=60)
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual(record["result"]["visited"], ["会议室", "二楼", "出发点"])
        self.assertEqual(self.names(), ["goto-meeting", "goto-stairs", "stairs-up", "stairs-down", "goto-home"])
        self.assertEqual(json.loads((self.dir / "region.json").read_text())["region"], "一楼")

    def test_free_walk(self):
        record = self.run_skill("free_walk", {"duration_s": 30})
        self.assertEqual(record["status"], "succeeded", record["error"])
        self.assertEqual(self.names(), ["wander"])

    def test_free_walk_is_refused_upstairs_with_the_reason(self):
        self.assertEqual(self.run_skill("goto_place", {"place": "二楼"})["status"], "succeeded")
        record = self.run_skill("free_walk", {"duration_s": 30})
        self.assertEqual(record["status"], "failed")
        self.assertIn("只允许在", record["error"])
        self.assertNotIn("wander", self.names())

    def test_status_shows_the_region(self):
        self.run_skill("goto_place", {"place": "二楼"})
        status = self.runtime.status()
        self.assertEqual(status["adapter"]["programs"], {"running": None, "region": "二楼", "region_known": True})


if __name__ == "__main__":
    unittest.main()
