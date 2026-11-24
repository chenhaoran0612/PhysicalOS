"""Issue #38: the ROS 2 bridge offers programs on the body as ``base.navigate_to_place`` and
``base.explore``.

On the rclpy test double (NOT ROS 2, see ros2_double/README.md) with stand-in programs (small
scripts in a temporary directory). Shown here: what the manifest declares, that requests go through
the capability bus and the safety supervisor to the program with a speed bound, that stopping ends
the program and zeroes the velocity topic, and that a refusal does not disturb a program that is
already running. Not shown: anything about a real robot or its navigation.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import _support
from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

from neuxmind_runtime.adapters import ros2
from neuxmind_runtime.bus import CapabilityBus
from neuxmind_runtime.errors import EStopLatched, SafetyRejected

PY = sys.executable
SCRIPT = r'''
import json, os, signal, sys, time
args = sys.argv[1:]
with open("calls.jsonl", "a") as fh:
    fh.write(json.dumps({"name": args[0], "args": args[1:], "pid": os.getpid()}) + "\n")
terms = []
def bye(*_):
    terms.append(1)
    with open("terms", "w") as fh:
        fh.write(str(len(terms)))
    if len(terms) == 1:
        time.sleep(0.5)                       # a stop that takes a moment; a second SIGTERM would land here
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
if "--sleep" in args:
    time.sleep(float(args[args.index("--sleep") + 1]))
if "--fail" in args:
    print("exit refused: blocked", flush=True)
    sys.exit(0)
print("exit route done", flush=True)
'''


def prog(name, *args, **fields):
    out = {"title": name, "argv": [PY, "prog.py", name, *args], "ok": "exit route done", "timeout_s": 20}
    out.update(fields)
    return out


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(os.path.realpath(self._tmp.name))
        (self.dir / "prog.py").write_text(SCRIPT)
        self._double = _support.ros2_double()
        self.graph = self._double.__enter__()
        self.addCleanup(self._double.__exit__, None, None, None)
        self.adapter = None

    def tearDown(self):
        if self.adapter is not None:
            self.adapter.close()

    def programs(self, **changes) -> dict:
        section = {
            "workdir": str(self.dir), "stop_grace_s": 0.3,
            "places": {
                "会议室": {"steps": [prog("goto-meeting", "--cruise", "{max_speed}", speed_range=[0.25, 0.6])]},
                "二楼": {"steps": [prog("goto-stairs"), prog("stairs-up", speed=0.3)]},
                "远处": {"steps": [prog("long-walk", "--sleep", "30")]},
                "走不通": {"steps": [prog("blocked", "--fail")]},
            },
            "explore": {"max_duration_s": 600, "max_radius": 10, "default_radius": 5,
                        "steps": [prog("wander", "--seconds", "{duration_s}", "--leash", "{radius}",
                                       "--cruise", "{max_speed}", speed_range=[0.25, 0.4],
                                       timeout_s=None, timeout_margin_s=5)]},
        }
        del section["explore"]["steps"][0]["timeout_s"]
        section.update(changes)
        return section

    def start(self, **config_changes):
        cfg = _support.ros2_config()
        cfg["programs"] = self.programs()
        cfg.update(config_changes)
        self.adapter = ros2.Ros2Adapter(cfg)
        self.twists = []
        publish = self.adapter._publish_twist

        def record(args):
            self.twists.append((args.get("vx", 0.0), args.get("vy", 0.0), args.get("wz", 0.0)))
            publish(args)
        self.adapter._publish_twist = record
        self.bus = CapabilityBus(self.adapter)
        return self.bus

    def calls(self):
        path = self.dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def names(self):
        return [c["name"] for c in self.calls()]

    def finish(self, handle, timeout=15.0):
        return _support.wait_until(lambda: handle.poll() if handle.poll().done else None, timeout)

    def wait_started(self, name):
        _support.wait_until(lambda: name in self.names(), 5.0)
        time.sleep(0.15)


class Manifest(_Case):
    def test_declares_places_and_free_walk(self):
        self.start()
        manifest = spec_manifest.validate_manifest_v2(self.adapter.manifest())
        declared = {c["id"]: c["params"] for c in manifest["capabilities"]}
        self.assertEqual(declared["base.navigate_to_place"], {"places": ["会议室", "二楼", "远处", "走不通"]})
        self.assertEqual(declared["base.explore"], {"max_duration_s": 600.0, "max_radius": 10.0})
        self.assertEqual(manifest["spec_version"], "1.2")
        self.assertEqual(self.adapter.status(), {"programs": {"running": None}})

    def test_without_the_section_nothing_is_declared(self):
        self.start(programs=None)
        ids = [c["id"] for c in self.adapter.manifest()["capabilities"]]
        self.assertNotIn("base.navigate_to_place", ids)
        self.assertNotIn("base.explore", ids)
        self.assertEqual(self.adapter.status(), {})
        with self.assertRaises(ValueError):
            self.adapter.start_action("base.explore", {"duration_s": 5.0, "max_speed": 0.3})

    def test_a_bad_section_is_refused_before_ros_is_touched(self):
        before = len(self.graph.subscriptions.get("/odom", []))
        cfg = _support.ros2_config()
        cfg["programs"] = self.programs(shell=True)
        with self.assertRaises(ValueError):
            ros2.Ros2Adapter(cfg)
        self.assertEqual(len(self.graph.subscriptions.get("/odom", [])), before)


class ThroughTheBus(_Case):
    def test_goes_to_a_place_with_the_speed_bound(self):
        bus = self.start()
        status = self.finish(bus.start_action("base.navigate_to_place", {"place": "会议室"}, authorized=True))
        self.assertEqual(status.state, "succeeded")
        self.assertEqual(status.result, {"reached": True, "place": "会议室"})
        spec_caps.validate_payload("base.navigate_to_place", status.result, "outputs")
        call = self.calls()[0]
        # 0.8 m/s is what this body declares; the program itself accepts at most 0.6.
        self.assertEqual(call["args"][call["args"].index("--cruise") + 1], "0.6")

    def test_a_lower_request_reaches_the_program(self):
        bus = self.start()
        self.finish(bus.start_action("base.navigate_to_place", {"place": "会议室", "max_speed": 0.3}, authorized=True))
        call = self.calls()[0]
        self.assertEqual(call["args"][call["args"].index("--cruise") + 1], "0.3")

    def test_a_body_limit_below_what_the_stairs_need_refuses_the_route(self):
        bus = self.start(limits={"max_vx": 0.28, "max_vy": 0.0, "max_wz": 1.0, "holonomic": False})
        status = self.finish(bus.start_action("base.navigate_to_place", {"place": "二楼"}, authorized=True))
        self.assertEqual(status.state, "failed")
        self.assertIn("固定速度", status.error)
        self.assertEqual(self.calls(), [])

    def test_unknown_place_never_reaches_the_adapter(self):
        bus = self.start()
        with self.assertRaises(SafetyRejected):
            bus.start_action("base.navigate_to_place", {"place": "天台"}, authorized=True)
        self.assertEqual(self.calls(), [])

    def test_free_walk(self):
        bus = self.start()
        status = self.finish(bus.start_action("base.explore", {"duration_s": 20.0, "radius": 3.0}, authorized=True))
        self.assertEqual(status.state, "succeeded")
        spec_caps.validate_payload("base.explore", status.result, "outputs")
        args = self.calls()[0]["args"]
        self.assertEqual([args[args.index(f) + 1] for f in ("--seconds", "--leash", "--cruise")], ["20", "3", "0.4"])

    def test_free_walk_longer_than_the_body_allows(self):
        bus = self.start()
        with self.assertRaises(SafetyRejected):
            bus.start_action("base.explore", {"duration_s": 601.0}, authorized=True)
        self.assertEqual(self.calls(), [])

    def test_a_failed_program_zeroes_the_velocity_topic(self):
        bus = self.start()
        status = self.finish(bus.start_action("base.navigate_to_place", {"place": "走不通"}, authorized=True))
        self.assertEqual(status.state, "failed")
        self.assertIn("exit refused: blocked", status.error)
        self.assertGreaterEqual(self.twists.count((0.0, 0.0, 0.0)), 3)

    def test_stop_ends_the_program_and_latches(self):
        bus = self.start()
        handle = bus.start_action("base.navigate_to_place", {"place": "远处"}, authorized=True)
        self.wait_started("long-walk")
        self.assertEqual(self.adapter.read("system.health")["mode"], "ros2:program")
        self.assertEqual(self.adapter.status()["programs"]["running"], "place:远处")
        started = time.monotonic()
        bus.supervisor.emergency_stop("test")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertIn((0.0, 0.0, 0.0), self.twists)
        status = self.finish(handle, 5.0)
        self.assertEqual(status.state, "cancelled")
        _support.wait_until(lambda: not _alive(self.calls()[0]["pid"]), 3.0)
        self.assertEqual(self.adapter.read("system.health")["mode"], "ros2")
        with self.assertRaises(EStopLatched):
            bus.start_action("base.navigate_to_place", {"place": "会议室"}, authorized=True)

    def test_a_second_request_is_refused_without_disturbing_the_first(self):
        bus = self.start()
        first = bus.start_action("base.navigate_to_place", {"place": "远处"}, authorized=True)
        self.wait_started("long-walk")
        second = bus.start_action("base.navigate_to_place", {"place": "会议室"}, authorized=True)
        status = second.poll()
        self.assertEqual(status.state, "failed")
        self.assertIn("还在运行", status.error)
        time.sleep(0.5)
        self.assertEqual(first.poll().state, "running")
        self.assertTrue(_alive(self.calls()[0]["pid"]))
        self.assertEqual(self.names(), ["long-walk"])

    def test_velocity_commands_are_not_taken_while_a_program_drives(self):
        bus = self.start()
        handle = bus.start_action("base.navigate_to_place", {"place": "远处"}, authorized=True)
        self.wait_started("long-walk")
        self.twists.clear()
        self.assertEqual(bus.invoke("base.velocity", {"vx": 0.2, "wz": 0.0}, authorized=True), {"accepted": False})
        self.assertEqual(self.twists, [])
        self.assertEqual(handle.poll().state, "running")
        self.assertEqual(bus.invoke("base.velocity", {"vx": 0.0, "wz": 0.0}, authorized=True), {"accepted": True})
        self.assertEqual(self.twists, [(0.0, 0.0, 0.0)])
        handle.cancel()
        self.finish(handle, 5.0)
        self.assertEqual(bus.invoke("base.velocity", {"vx": 0.2, "wz": 0.0}, authorized=True), {"accepted": True})

    def test_a_refused_request_leaves_the_velocity_watchdog_armed(self):
        # Review finding: the refusal came back as an (already failed) action, and "navigation supersedes
        # the velocity setpoint" then disarmed the watchdog although nothing had started.
        bus = self.start(limits={"max_vx": 0.28, "max_vy": 0.0, "max_wz": 1.0, "holonomic": False})
        self.assertEqual(bus.invoke("base.velocity", {"vx": 0.2, "wz": 0.0, "timeout_s": 0.3}, authorized=True),
                         {"accepted": True})
        self.assertTrue(bus.supervisor.velocity_active)
        refused = bus.start_action("base.navigate_to_place", {"place": "二楼"}, authorized=True)
        self.assertEqual(refused.poll().state, "failed")
        self.assertTrue(bus.supervisor.velocity_active)
        time.sleep(0.4)
        self.assertTrue(bus.supervisor.watchdog_tick())
        self.assertEqual(self.twists[-1], (0.0, 0.0, 0.0))

    def test_a_started_program_takes_over_from_a_velocity_setpoint(self):
        # Second review: with the watchdog for the old setpoint disarmed, a program that never publishes
        # left that setpoint as the last word on the topic. The bridge zeroes it when a program starts.
        bus = self.start()
        bus.invoke("base.velocity", {"vx": 0.2, "wz": 0.0, "timeout_s": 5.0}, authorized=True)
        self.assertEqual(self.twists[-1], (0.2, 0.0, 0.0))
        handle = bus.start_action("base.navigate_to_place", {"place": "远处"}, authorized=True)
        self.assertFalse(bus.supervisor.velocity_active)
        self.assertEqual(self.twists[-1], (0.0, 0.0, 0.0))
        handle.cancel()
        self.finish(handle, 5.0)

    def test_stopping_again_and_again_signals_the_program_once(self):
        bus = self.start()
        handle = bus.start_action("base.navigate_to_place", {"place": "远处"}, authorized=True)
        self.wait_started("long-walk")
        for _ in range(4):
            self.adapter.stop()
        self.assertEqual(self.finish(handle, 5.0).state, "cancelled")
        self.assertEqual((self.dir / "terms").read_text(), "1")

    def test_close_ends_a_running_program(self):
        bus = self.start()
        bus.start_action("base.navigate_to_place", {"place": "远处"}, authorized=True)
        self.wait_started("long-walk")
        pid = self.calls()[0]["pid"]
        self.adapter.close()
        self.adapter = None
        _support.wait_until(lambda: not _alive(pid), 3.0)


class StopSteps(unittest.TestCase):
    def test_programs_are_ended_even_when_an_earlier_step_fails(self):
        calls = []

        def broken(_args):
            raise RuntimeError("publisher gone")
        adapter = NS(_publish_twist=broken, _nav_handles=set(), _cancel_all_goals=lambda: calls.append("nav2"),
                     _stop_programs=lambda: calls.append("programs"))
        with self.assertRaises(RuntimeError):
            ros2.stop_adapter(adapter)
        self.assertEqual(calls, ["nav2", "programs"])

    def test_an_adapter_without_programs_still_stops(self):
        seen = []
        adapter = NS(_publish_twist=seen.append, _nav_handles=set(), _cancel_all_goals=lambda: None)
        ros2.stop_adapter(adapter)
        self.assertEqual(seen, [{"vx": 0.0, "vy": 0.0, "wz": 0.0}])


class Battery(unittest.TestCase):
    def test_a_body_that_publishes_percent(self):
        msg = NS(percentage=71.0, power_supply_status=2, voltage=54.3)
        self.assertEqual(ros2.battery_outputs(msg, ros2.battery_scale({"battery_percentage": "percent"}))["percent"], 71.0)
        self.assertEqual(ros2.battery_outputs(NS(percentage=0.71, power_supply_status=2, voltage=54.3),
                                              ros2.battery_scale({}))["percent"], 71.0)

    def test_unknown_unit_is_refused(self):
        cfg = _support.ros2_config()
        cfg["battery_percentage"] = "volts"
        with self.assertRaises(ValueError):
            ros2.build_manifest(cfg)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


if __name__ == "__main__":
    unittest.main()
