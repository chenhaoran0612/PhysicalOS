"""The ROS 2 bridge against real rclpy nodes on ROS 2 Humble (issue #21). Run by ``run.sh``.

Inside ``ros:humble-ros-base-jammy`` (ROS sourced, Python 3.10): a real rclpy node stands in for a
robot's base driver -- it publishes ``/odom`` with the sensor-data QoS (best effort, as many drivers
do), ``/battery_state`` reliably, and records every Twist on ``/cmd_vel``. The runtime is started as
its own process with ``--adapter ros2`` and talks to that node over DDS. Nothing here is a robot: the
"base" only echoes. Prints one line per check and writes ``results.json`` to the output directory.

    python3 check.py <copy of the repository> <output directory>
"""
from __future__ import annotations

import json
import math
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import BatteryState

REPO, OUT = Path(sys.argv[1]), Path(sys.argv[2])
TOKEN = secrets.token_urlsafe(36)
POSE = (1.5, -0.5, 0.3)                 # x, y, yaw the base reports
BATTERY = (0.73, 24.5)                  # fraction, volts; charging
FILLED = {"base": {"max_linear_speed": 0.3, "max_angular_speed": 0.5, "command_timeout_s": 0.5}}
RUNTIME_NODE = "neuxmind_runtime"
results: list[dict] = []


def check(name: str, ok: bool, detail="") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f"  -- {detail}" if detail != "" else ""), flush=True)
    return ok


class Base(Node):
    """Stands in for a base driver (NOT a robot)."""

    def __init__(self):
        super().__init__("fake_base")
        self.twists: list[tuple[float, float, float, float]] = []
        self._odom = self.create_publisher(Odometry, "/odom", qos_profile_sensor_data)
        self._battery = self.create_publisher(BatteryState, "/battery_state", 10)
        self.create_subscription(Twist, "/cmd_vel", self._on_twist, 10)
        self.create_timer(0.05, self._tick)

    def _on_twist(self, msg: Twist) -> None:
        self.twists.append((time.monotonic(), msg.linear.x, msg.linear.y, msg.angular.z))

    def _tick(self) -> None:
        odom = Odometry()
        odom.header.frame_id, odom.child_frame_id = "odom", "base_link"
        odom.header.stamp = self.get_clock().now().to_msg()
        odom.pose.pose.position.x, odom.pose.pose.position.y = POSE[0], POSE[1]
        odom.pose.pose.orientation.z, odom.pose.pose.orientation.w = math.sin(POSE[2] / 2), math.cos(POSE[2] / 2)
        odom.twist.twist.linear.x = 0.0
        self._odom.publish(odom)
        battery = BatteryState()
        battery.percentage, battery.voltage = BATTERY
        battery.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_CHARGING
        self._battery.publish(battery)

    def endpoint_qos(self, topic: str, publishers: bool) -> list:
        infos = (self.get_publishers_info_by_topic if publishers else self.get_subscriptions_info_by_topic)(topic)
        return [i.qos_profile for i in infos if i.node_name == RUNTIME_NODE]


class Runtime:
    """``python3 -m neuxmind_runtime serve --adapter ros2`` as its own process."""

    def __init__(self, config: dict, name: str):
        self.dir = Path(tempfile.mkdtemp(prefix=f"nx-{name}-"))
        (self.dir / "ros2.json").write_text(json.dumps(config))
        env = {**os.environ, "BRAIN_EDGE_TOKEN": TOKEN, "NEUXMIND_ROS2_CONFIG": str(self.dir / "ros2.json"),
               "PYTHONPATH": os.pathsep.join([str(REPO / "runtime"), str(REPO / "spec"), os.environ["PYTHONPATH"]])}
        self.log = open(self.dir / "runtime.log", "w+")
        self.proc = subprocess.Popen([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter", "ros2",
                                      "--host", "127.0.0.1", "--port", "0", "--state-dir", str(self.dir / "state")],
                                     cwd=REPO / "runtime", env=env, stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.url = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and self.proc.poll() is None:
            line = self.proc.stdout.readline()
            if "监听" in line:
                self.url = line.strip().split()[-1]
                break
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        for line in self.proc.stdout:
            self.log.write(line)

    def call(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def state(self, cap: str):
        return self.call("GET", f"/v2/state?ids={cap}")

    def stop_process(self) -> int | None:
        self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return None

    def log_text(self) -> str:
        self.log.flush()
        return (self.dir / "runtime.log").read_text()


def wait_for(predicate, timeout: float, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def config(**changes) -> dict:
    # The datasheet limits a vendor would give; nav2 left at "auto" (ros-base has no Nav2).
    return {"instance_id": "humble-check-01", "name": "Humble check", "limits": {"max_vx": 0.8, "max_wz": 1.0},
            **changes}


def environment() -> None:
    check("ROS_DISTRO is humble", os.environ.get("ROS_DISTRO") == "humble", os.environ.get("ROS_DISTRO"))
    check("Python is the system python3 of Humble", sys.version_info[:2] == (3, 10), sys.version.split()[0])
    from rclpy.utilities import get_rmw_implementation_identifier
    check("RMW", True, get_rmw_implementation_identifier())
    try:
        import nav2_msgs  # noqa: F401
        has_nav2 = True
    except ImportError:
        has_nav2 = False
    check("nav2_msgs is not installed (ros-base)", not has_nav2)


def distro_refused() -> None:
    env = {**os.environ, "ROS_DISTRO": "jazzy",
           "PYTHONPATH": os.pathsep.join([str(REPO / "runtime"), str(REPO / "spec"), os.environ["PYTHONPATH"]])}
    done = subprocess.run([sys.executable, "-c", "from neuxmind_runtime.adapters.ros2 import Ros2Adapter; Ros2Adapter()"],
                          env=env, capture_output=True, text=True, timeout=60)
    check("another ROS_DISTRO is refused on real rclpy", done.returncode != 0 and "jazzy" in done.stderr,
          done.stderr.strip().splitlines()[-1] if done.stderr.strip() else done.returncode)
    env["ROS_DISTRO"] = "humble"
    env["NEUXMIND_ROS2_CONFIG"] = str(Path(tempfile.mkdtemp()) / "nav2.json")
    Path(env["NEUXMIND_ROS2_CONFIG"]).write_text(json.dumps(config(nav2=True)))
    done = subprocess.run([sys.executable, "-c", "from neuxmind_runtime.adapters.ros2 import Ros2Adapter; Ros2Adapter()"],
                          env=env, capture_output=True, text=True, timeout=60)
    check("nav2=true without Nav2 is refused with the reason", done.returncode != 0 and "nav2" in done.stderr,
          done.stderr.strip().splitlines()[-1] if done.stderr.strip() else done.returncode)


def installer_on_real_humble() -> None:
    """device_install.sh's Humble check on the image's real /opt/ros/humble/setup.bash. The image has no
    ensurepip (python3-venv), so the install itself stops at the venv: what matters here is that the
    real setup file passes the check and the settings are read, and that the failed install cleans up."""
    base = Path(tempfile.mkdtemp(prefix="nx-installer-"))
    home, package = base / "home", base / "pkg"
    home.mkdir()
    package.mkdir()
    script = package / "device_install.sh"
    script.write_bytes((REPO / "runtime" / "deploy" / "device_install.sh").read_bytes())
    for name in ("neuxmind_spec-0-py3-none-any.whl", "neuxmind_runtime-0-py3-none-any.whl"):
        (package / name).write_bytes(b"")                 # never installed: the venv step fails first
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("ROS_", "AMENT_", "COLCON_", "PYTHONPATH"))}
    env = {**clean, "HOME": str(home), "NX_ADAPTER": "ros2", "ROS_DOMAIN_ID": "77"}
    done = subprocess.run(["bash", str(script), "install"], env=env, capture_output=True, text=True, timeout=120)
    check("installer: the real /opt/ros/humble/setup.bash passes the Humble check",
          "body: ros2, ROS 2 humble (/opt/ros/humble/setup.bash)" in done.stdout
          and "ROS settings: ROS_DOMAIN_ID=77" in done.stdout, done.stdout.strip())
    check("installer: without ensurepip the install fails and leaves nothing",
          done.returncode == 1 and not (home / "neuxmind").exists() and "removed again" in done.stderr,
          done.stderr.strip().splitlines()[-1] if done.stderr.strip() else done.returncode)
    jazzy = base / "opt" / "ros" / "jazzy"
    jazzy.mkdir(parents=True)
    (jazzy / "setup.bash").write_text("export ROS_VERSION=2\nexport ROS_DISTRO=jazzy\n")
    done = subprocess.run(["bash", str(script), "install"], env={**env, "NX_ROS_SETUP": str(jazzy / "setup.bash")},
                          capture_output=True, text=True, timeout=60)
    check("installer: another distribution is refused before anything is created",
          done.returncode == 2 and "only ROS 2 Humble" in done.stderr and not (home / "neuxmind").exists(),
          done.stderr.strip())


def main_runtime(base: Base) -> None:
    rt = Runtime(config(), "main")
    try:
        if not check("runtime with --adapter ros2 started", rt.url is not None, rt.log_text()[-400:] if rt.url is None else rt.url):
            return
        status, body = rt.call("GET", "/v1/status")
        limits = body.get("safety_limits") or {}
        check("status: not simulated, mode ros2, motion blocked (no limits filled, issue #22)",
              status == 200 and body.get("simulated") is False and body.get("mode") == "ros2"
              and limits.get("motion_blocked") is True, {k: body.get(k) for k in ("simulated", "mode")})
        _, manifest = rt.call("GET", "/v2/manifest")
        ids = [c["id"] for c in manifest.get("capabilities", [])]
        check("manifest without Nav2: no base.navigate_to, the rest declared",
              ids == ["safety.stop", "system.health", "base.velocity", "base.odometry", "sensor.battery"], ids)
        check("adapter_version", manifest.get("adapter_version") == "0.1.0", manifest.get("adapter_version"))

        odom = wait_for(lambda: (lambda r: r[1].get("base.odometry") if r[0] == 200 else None)(rt.state("base.odometry")), 15)
        check("odometry read from the real /odom (best-effort publisher)",
              bool(odom) and abs(odom["pose"][0] - POSE[0]) < 1e-6 and abs(odom["pose"][1] - POSE[1]) < 1e-6
              and abs(odom["pose"][5] - math.sin(POSE[2] / 2)) < 1e-6, odom)
        status, battery = rt.state("sensor.battery")
        battery = battery.get("sensor.battery") if status == 200 else battery
        check("battery read from the real /battery_state",
              status == 200 and abs(battery["percent"] - BATTERY[0] * 100) < 1e-4 and battery["charging"] is True
              and abs(battery["voltage"] - BATTERY[1]) < 1e-4, battery)
        status, health = rt.state("system.health")
        check("health ok while both topics are fresh", status == 200 and health["system.health"]["ok"] is True, health)

        sub = base.endpoint_qos("/odom", publishers=False)
        check("the runtime's /odom subscription is best effort (default)",
              len(sub) == 1 and sub[0].reliability == ReliabilityPolicy.BEST_EFFORT, [str(q.reliability) for q in sub])
        # DDS discovery carries reliability and durability, not history or depth (a remote endpoint shows
        # depth 0); the depth handed to rclpy is checked in test_ros2_humble.
        pub = base.endpoint_qos("/cmd_vel", publishers=True)
        check("the runtime's /cmd_vel publisher is reliable and volatile",
              len(pub) == 1 and pub[0].reliability == ReliabilityPolicy.RELIABLE
              and pub[0].durability == DurabilityPolicy.VOLATILE, [(str(q.reliability), str(q.durability)) for q in pub])

        before = len(base.twists)
        status, body = rt.call("POST", "/v2/capabilities/base.velocity/invoke",
                               {"args": {"vx": 0.2, "wz": 0.0}, "hardware_authorized": True, "request_id": "hc-1"})
        time.sleep(0.5)
        check("velocity refused before limits are filled (409 safety_limits_missing), nothing published",
              status == 409 and body.get("code") == "safety_limits_missing" and len(base.twists) == before,
              {"status": status, "code": body.get("code"), "twists": len(base.twists) - before})

        before = len(base.twists)
        status, body = rt.call("POST", "/v1/stop", {"reason": "humble check"})
        zero = wait_for(lambda: [t for t in base.twists[before:] if t[1:] == (0.0, 0.0, 0.0)], 2.0)
        check("stop without limits: answered, latched, a zero Twist reached the base",
              status == 200 and body.get("stopped") is True and bool(zero), {"status": status, "zero_twists": len(zero or [])})

        status, body = rt.call("POST", "/v2/safety/limits", {"limits": FILLED, "set_by": "ros2_humble check"})
        check("limits filled", status == 200, body if status != 200 else "")
        status, body = rt.call("POST", "/v2/reset", {"hardware_authorized": True})
        check("reset after the stop", status == 200 and body.get("latched") is False, body)

        before = len(base.twists)
        status, body = rt.call("POST", "/v2/capabilities/base.velocity/invoke",
                               {"args": {"vx": 1.0, "wz": -2.0}, "hardware_authorized": True, "request_id": "hc-2"})
        moved = wait_for(lambda: [t for t in base.twists[before:] if t[1] != 0.0], 2.0)
        check("velocity published, clamped to the filled limits (0.3 m/s, 0.5 rad/s)",
              status == 200 and bool(moved) and abs(moved[0][1] - 0.3) < 1e-9 and abs(moved[0][3] + 0.5) < 1e-9,
              {"status": status, "first": moved[0][1:] if moved else None})
        if moved:
            sent = moved[0][0]
            zero = wait_for(lambda: [t for t in base.twists if t[0] > sent and t[1:] == (0.0, 0.0, 0.0)], 3.0)
            check("watchdog: a zero Twist follows within the command timeout",
                  bool(zero) and zero[0][0] - sent < FILLED["base"]["command_timeout_s"] + 0.5,
                  round(zero[0][0] - sent, 3) if zero else None)
        status, _ = rt.call("POST", "/v1/stop", {"reason": "humble check end"})
        check("final stop", status == 200)
    finally:
        code = rt.stop_process()
        check("runtime exits cleanly on SIGTERM", code == 0, code)
        (OUT / "runtime-main.log").write_text(rt.log_text())


def qos_mismatch_runtime(base: Base) -> None:
    rt = Runtime(config(qos={"odom": {"reliability": "reliable"}}), "qos")
    try:
        if not check("runtime with a reliable /odom subscription started", rt.url is not None):
            return
        sub = wait_for(lambda: base.endpoint_qos("/odom", publishers=False), 10)
        check("the configured QoS reaches DDS: /odom subscription is reliable",
              bool(sub) and sub[0].reliability == ReliabilityPolicy.RELIABLE, [str(q.reliability) for q in sub or []])
        battery = wait_for(lambda: rt.state("sensor.battery")[0] == 200, 15)
        time.sleep(2.0)
        status, health = rt.state("system.health")
        errors = health.get("system.health", {}).get("errors", {})
        check("a reliable reader gets nothing from the best-effort /odom: health names odom, battery still fresh",
              bool(battery) and status == 200 and "odom" in errors and "battery" not in errors, errors)
        check("ROS reports the incompatible QoS in the runtime log", "incompatible" in rt.log_text().lower(),
              "")
    finally:
        rt.stop_process()
        (OUT / "runtime-qos.log").write_text(rt.log_text())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    environment()
    distro_refused()
    installer_on_real_humble()
    rclpy.init()
    base = Base()
    executor = SingleThreadedExecutor()
    executor.add_node(base)
    threading.Thread(target=executor.spin, daemon=True).start()
    try:
        main_runtime(base)
        qos_mismatch_runtime(base)
    finally:
        executor.shutdown()
        base.destroy_node()
        rclpy.shutdown()
    left = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    check("no runtime process left", "neuxmind_runtime serve" not in left)
    failed = [r for r in results if not r["ok"]]
    (OUT / "results.json").write_text(json.dumps({"passed": len(results) - len(failed), "failed": len(failed),
                                                  "results": results}, ensure_ascii=False, indent=2, default=str))
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
