"""Issue #21 (R1-03): the ROS 2 bridge for a device that runs ROS 2 Humble.

What these tests cover, on the rclpy test double (NOT ROS 2, see ros2_double/README.md): Nav2 is
optional, topic QoS comes from the configuration, only Humble is accepted, and on a body that is not
simulated the safety limits of issue #22 still hold, with a stop path that needs nothing new. The same
adapter is run against real rclpy nodes on ROS 2 Humble by ``tests/ros2_humble/run.sh`` (a container,
see its README); the installer's choice of body is in ``test_device_install_ros2.py``.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import _support
from neuxmind_spec import manifest as spec_manifest

from neuxmind_runtime import conformance
from neuxmind_runtime.adapters import ros2
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import SafetyRejected

DEFAULT_QOS = {
    "cmd_vel": {"reliability": "reliable", "durability": "volatile", "history": "keep_last", "depth": 1},
    "odom": {"reliability": "best_effort", "durability": "volatile", "history": "keep_last", "depth": 10},
    "battery": {"reliability": "best_effort", "durability": "volatile", "history": "keep_last", "depth": 10},
}
MISSING = "safety_limits_missing"


def config(**changes) -> dict:
    """The default configuration with the body's datasheet limits filled in, plus ``changes``."""
    cfg = _support.ros2_config()
    cfg.update(json.loads(json.dumps(changes)))
    return cfg


def capability_ids(adapter) -> list[str]:
    return [c["id"] for c in adapter.manifest()["capabilities"]]


def wait_for_odometry(adapter) -> dict:
    def read():
        try:
            return adapter.read("base.odometry")
        except RuntimeError:
            return None
    found = _support.wait_until(read, 5.0)
    if not found:
        raise AssertionError("no odometry from the test double within 5 s")
    return found


class _NotSimulated:
    """The double reports ``use_sim_time: true``; a real robot's node says false."""

    def __enter__(self):
        import rclpy.node
        self._patch = mock.patch.object(rclpy.node.Node, "get_parameter", lambda _self, _name: NS(value=False))
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


# ------------------------------------------------------------------------------------ QoS
class QosConfigurationTests(unittest.TestCase):
    def test_defaults_publish_reliably_and_subscribe_to_any_publisher(self):
        # A best-effort subscription receives from reliable and from best-effort (sensor QoS)
        # publishers alike; a reliable publisher reaches both kinds of subscribers.
        self.assertEqual(ros2.qos_settings(ros2.DEFAULT_CONFIG), DEFAULT_QOS)

    def test_a_device_sets_its_own_profile_per_topic_and_field(self):
        cfg = config(qos={"odom": {"reliability": "reliable", "depth": 1},
                          "battery": {"durability": "transient_local", "history": "keep_all"},
                          "cmd_vel": {"depth": 1}})                  # the fixed value may be repeated

        got = ros2.qos_settings(cfg)

        self.assertEqual(got["odom"], {**DEFAULT_QOS["odom"], "reliability": "reliable", "depth": 1})
        self.assertEqual(got["battery"], {**DEFAULT_QOS["battery"], "durability": "transient_local",
                                          "history": "keep_all"})
        self.assertEqual(got["cmd_vel"], {**DEFAULT_QOS["cmd_vel"], "depth": 1})

    def test_anything_the_bridge_would_not_understand_is_refused_with_its_name(self):
        for bad, named in (({"odom": {"reliability": "sometimes"}}, "reliability"),
                           ({"odom": {"deadline": 1.0}}, "deadline"),
                           ({"lidar": {"depth": 5}}, "lidar"),
                           ({"odom": {"depth": 0}}, "depth"),
                           ({"odom": {"depth": 101}}, "depth"),
                           ({"odom": {"depth": True}}, "depth"),
                           ({"odom": {"depth": 2.5}}, "depth"),
                           ({"odom": "best_effort"}, "odom"),
                           (["odom"], "qos")):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                ros2.qos_settings(config(qos=bad))
            self.assertIn(named, str(ctx.exception))

    def test_the_velocity_topic_keeps_what_the_stop_relies_on(self):
        # The stop is a zero Twist on this publisher: it must not be dropped (best effort), handed to a
        # controller that starts later (transient local: the last command would be replayed), or wait
        # for a slow reader (keep all).
        # Depth 1: a reader that lags behind gets the newest command (the zero) first, not a queue of old
        # ones ahead of it.
        for field, value in (("reliability", "best_effort"), ("durability", "transient_local"),
                             ("history", "keep_all"), ("depth", 10)):
            with self.subTest(field=field), self.assertRaises(ValueError) as ctx:
                ros2.qos_settings(config(qos={"cmd_vel": {field: value}}))
            self.assertIn("cmd_vel", str(ctx.exception))
            self.assertIn("停止", str(ctx.exception))

    def test_a_bad_profile_is_refused_before_ros_is_touched(self):
        with _support.ros2_double() as graph:
            nodes_before = len(graph.subscriptions.get("/odom", []))
            with self.assertRaises(ValueError):
                ros2.Ros2Adapter(config(qos={"odom": {"reliability": "sometimes"}}))
            self.assertEqual(len(graph.subscriptions.get("/odom", [])), nodes_before)


class QosReachesRclpyTests(unittest.TestCase):
    def test_publisher_and_subscriptions_are_created_with_the_configured_profiles(self):
        with _support.ros2_double():
            import rclpy.node
            from rclpy.qos import DurabilityPolicy, HistoryPolicy, ReliabilityPolicy
            made = {}
            create_publisher, create_subscription = rclpy.node.Node.create_publisher, rclpy.node.Node.create_subscription

            def publisher(node, msg_type, topic, qos):
                made[topic] = qos
                return create_publisher(node, msg_type, topic, qos)

            def subscription(node, msg_type, topic, callback, qos):
                made[topic] = qos
                return create_subscription(node, msg_type, topic, callback, qos)

            with mock.patch.object(rclpy.node.Node, "create_publisher", publisher), \
                    mock.patch.object(rclpy.node.Node, "create_subscription", subscription):
                adapter = ros2.Ros2Adapter(config(qos={"odom": {"reliability": "reliable", "depth": 3}}))
            try:
                odom, battery, cmd = made["/odom"], made["/battery_state"], made["/cmd_vel"]
                self.assertEqual((odom.reliability, odom.durability, odom.history, odom.depth),
                                 (ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE, HistoryPolicy.KEEP_LAST, 3))
                self.assertEqual(battery.reliability, ReliabilityPolicy.BEST_EFFORT)
                self.assertEqual((cmd.reliability, cmd.durability, cmd.history, cmd.depth),
                                 (ReliabilityPolicy.RELIABLE, DurabilityPolicy.VOLATILE, HistoryPolicy.KEEP_LAST, 1))
                wait_for_odometry(adapter)                  # the subscriptions are live
            finally:
                adapter.close()


# ----------------------------------------------------------------------------------- Nav2
class Nav2OptionalTests(unittest.TestCase):
    def test_without_nav2_the_bridge_starts_and_only_navigation_is_missing(self):
        with _support.ros2_double(nav2=False) as graph:
            adapter = ros2.Ros2Adapter(config())
            try:
                ids = capability_ids(adapter)
                spec_manifest.validate_manifest_v2(adapter.manifest())
                self.assertNotIn("base.navigate_to", ids)
                self.assertEqual(ids, ["safety.stop", "system.health", "base.velocity", "base.odometry",
                                       "sensor.battery"])
                wait_for_odometry(adapter)
                self.assertTrue(adapter.read("system.health")["ok"])
                with self.assertRaises(ValueError):
                    adapter.start_action("base.navigate_to", {"goal": [1, 0, 0, 0, 0, 0, 1]})
                adapter.invoke("base.velocity", {"vx": 0.2, "wz": 0.0})
                adapter.stop()                              # zero Twist; there is no goal to cancel
                last = graph.published["/cmd_vel"][-1]
                self.assertEqual((last.linear.x, last.linear.y, last.angular.z), (0.0, 0.0, 0.0))
                self.assertIsNone(adapter._cancel_all)
            finally:
                adapter.close()

    def test_installed_nav2_is_declared_by_default(self):
        with _support.ros2_double():
            adapter = ros2.Ros2Adapter(config())
            try:
                self.assertIn("base.navigate_to", capability_ids(adapter))
            finally:
                adapter.close()

    def test_nav2_can_be_turned_off_for_a_device(self):
        with _support.ros2_double() as graph:
            adapter = ros2.Ros2Adapter(config(nav2=False))
            try:
                self.assertNotIn("base.navigate_to", capability_ids(adapter))
                adapter.stop()
                self.assertIsNone(adapter._nav)
                self.assertEqual(graph.robot().goals, [])
            finally:
                adapter.close()

    def test_nav2_required_but_not_installed_is_refused_with_the_reason(self):
        with _support.ros2_double(nav2=False):
            with self.assertRaises(RuntimeError) as ctx:
                ros2.Ros2Adapter(config(nav2=True))
        self.assertIn("nav2_msgs", str(ctx.exception))

    def test_the_nav2_setting_is_auto_true_or_false(self):
        for bad in ("yes", 1, None, "on"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                ros2.nav2_mode(config(nav2=bad))
            self.assertIn("nav2", str(ctx.exception))
        self.assertEqual([ros2.nav2_mode(config(nav2=v)) for v in ("auto", True, False)], ["auto", True, False])
        self.assertEqual(ros2.nav2_mode(ros2.DEFAULT_CONFIG), "auto")

    def test_a_static_manifest_follows_the_setting(self):
        self.assertIn("base.navigate_to", [c["id"] for c in ros2.build_manifest(config())["capabilities"]])
        off = ros2.build_manifest(config(nav2=False))
        self.assertNotIn("base.navigate_to", [c["id"] for c in off["capabilities"]])
        self.assertNotIn("base.navigate_to",
                         [c["id"] for c in ros2.build_manifest(config(), navigation=False)["capabilities"]])

    def test_dry_conformance_passes_without_nav2(self):
        with _support.ros2_double(nav2=False):
            adapter = ros2.Ros2Adapter(config())
            report = conformance.run_adapter(adapter, "dry")
        self.assertTrue(report["dry_ok"], [c for c in report["cases"] if c["status"] == "failed"])
        # every standard capability gets a static declaration case; an undeclared one is skipped
        self.assertEqual([(c["id"], c["status"]) for c in report["cases"] if "navigate_to" in c["id"]],
                         [("static.base.navigate_to.declaration", "skipped")])


# ------------------------------------------------------------------------- optional battery
class NoBatteryTopicTests(unittest.TestCase):
    """A device whose ROS graph has no battery topic: ``"topics": {"battery": null}``."""

    def test_no_battery_topic_means_no_battery_and_a_health_that_can_be_ok(self):
        with _support.ros2_double() as graph:
            cfg = config()
            cfg["topics"] = {**cfg["topics"], "battery": None}
            adapter = ros2.Ros2Adapter(cfg)
            try:
                self.assertNotIn("sensor.battery", capability_ids(adapter))
                spec_manifest.validate_manifest_v2(adapter.manifest())
                self.assertEqual(graph.subscriptions.get("/battery_state", []), [])
                wait_for_odometry(adapter)
                self.assertEqual(adapter.read("system.health"), {"ok": True, "mode": "ros2", "errors": {}})
                with self.assertRaises(ValueError):
                    adapter.read("sensor.battery")
            finally:
                adapter.close()

    def test_odometry_and_the_velocity_topic_cannot_be_left_out(self):
        for topic in ("odom", "cmd_vel"):
            cfg = config()
            cfg["topics"] = {**cfg["topics"], topic: None}
            with self.subTest(topic=topic), self.assertRaises(ValueError) as ctx:
                ros2.build_manifest(cfg)
            self.assertIn(topic, str(ctx.exception))

    def test_a_static_manifest_leaves_the_battery_out_too(self):
        cfg = config()
        cfg["topics"] = {**cfg["topics"], "battery": None}
        self.assertNotIn("sensor.battery", [c["id"] for c in ros2.build_manifest(cfg)["capabilities"]])


# --------------------------------------------------------------------------------- Humble
class HumbleOnlyTests(unittest.TestCase):
    def test_the_distribution_check_names_what_it_found(self):
        self.assertIsNone(ros2.check_distro({"ROS_DISTRO": "humble", "ROS_VERSION": "2"}))
        for env, named in (({"ROS_DISTRO": "jazzy", "ROS_VERSION": "2"}, "jazzy"),
                           ({"ROS_DISTRO": "noetic", "ROS_VERSION": "1"}, "noetic"),
                           ({}, "source")):
            with self.subTest(env=env), self.assertRaises(RuntimeError) as ctx:
                ros2.check_distro(env)
            self.assertIn(named, str(ctx.exception))
            self.assertIn("Humble", str(ctx.exception))

    def test_another_distribution_is_refused_before_a_node_is_made(self):
        with _support.ros2_double() as graph, mock.patch.dict(os.environ, {"ROS_DISTRO": "jazzy"}):
            before = len(graph.subscriptions.get("/odom", []))
            with self.assertRaises(RuntimeError) as ctx:
                ros2.Ros2Adapter(config())
            self.assertEqual(len(graph.subscriptions.get("/odom", [])), before)
        self.assertIn("jazzy", str(ctx.exception))

    def test_a_shell_without_ros_sourced_is_told_what_to_do(self):
        with _support.ros2_double():
            env = {k: v for k, v in os.environ.items() if k != "ROS_DISTRO"}
            with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(RuntimeError) as ctx:
                ros2.Ros2Adapter(config())
        self.assertIn("/opt/ros/humble/setup.bash", str(ctx.exception))

    def test_the_manifest_says_it_is_no_longer_a_skeleton(self):
        self.assertNotIn("skeleton", ros2.build_manifest(config())["adapter_version"])


# ------------------------------------------------------------------- issue #22 still holds
class SafetyLimitsOnRos2Tests(unittest.TestCase):
    """A body that is not simulated, reached through ROS 2 without Nav2 and without filled limits."""

    def test_it_starts_reads_and_stops_but_does_not_move(self):
        with _support.ros2_double(nav2=False) as graph, _NotSimulated(), tempfile.TemporaryDirectory() as state:
            adapter = ros2.Ros2Adapter(_support.ros2_config())
            self.assertFalse(adapter.manifest()["simulated"])
            runtime = EdgeRuntime(adapter, state, sandbox="inprocess", start_monitor=False)
            try:
                status = runtime.status()
                self.assertEqual((status["simulated"], status["mode"], status["safety_limits"]["motion_blocked"]),
                                 (False, "ros2", True))
                wait_for_odometry(adapter)
                self.assertIn("pose", runtime.read_state(["base.odometry"])["base.odometry"])
                self.assertIn("percent", runtime.read_state(["sensor.battery"])["sensor.battery"])
                for args in ({"vx": 0.2, "wz": 0.0}, {"vx": 0.0, "wz": 0.0}):
                    payload = {"args": args, "hardware_authorized": True, "request_id": f"r-{time.monotonic_ns()}"}
                    with self.assertRaises(SafetyRejected) as ctx:
                        runtime.invoke("base.velocity", payload)
                    self.assertEqual(ctx.exception.code, MISSING)
                self.assertEqual(list(graph.published["/cmd_vel"]), [])          # nothing reached the body

                stopped = runtime.emergency_stop("test", source="api")

                self.assertTrue(stopped["stopped"])
                last = graph.published["/cmd_vel"][-1]
                self.assertEqual((last.linear.x, last.linear.y, last.angular.z), (0.0, 0.0, 0.0))
                self.assertTrue(runtime.status()["estop_latched"])
            finally:
                runtime.supervisor.halt_motion("test teardown")
                runtime.close()

    def test_the_stop_path_is_the_same_with_and_without_nav2(self):
        # stop_adapter: zero Twist, cancel known goals, Nav2 cancel-all -- the last is skipped when there
        # is no Nav2, and nothing was added in front of the Twist.
        calls = []
        adapter = NS(_publish_twist=lambda args: calls.append(("twist", args)), _nav_handles=set(),
                     _cancel_all_goals=lambda: calls.append("cancel_all"))
        ros2.stop_adapter(adapter)
        self.assertEqual(calls[0], ("twist", {"vx": 0.0, "vy": 0.0, "wz": 0.0}))
        with _support.ros2_double(nav2=False):
            real = ros2.Ros2Adapter(config())
            try:
                with mock.patch.object(real, "_cancel_all_goals", wraps=real._cancel_all_goals) as cancel_all:
                    real.stop()
                cancel_all.assert_called_once_with()           # still called; it returns at once without Nav2
            finally:
                real.close()


if __name__ == "__main__":
    unittest.main()
