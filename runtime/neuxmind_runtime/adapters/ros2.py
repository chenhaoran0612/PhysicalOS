"""ROS 2 bridge adapter — NOT VERIFIED ON ANY ROBOT OR REAL ROS 2 INSTALL.

Maps NeuxMind standard capabilities onto common ROS 2 interfaces:

=====================  ==========  ===============================================
capability             direction   ROS 2 interface (default name)
=====================  ==========  ===============================================
base.velocity          publish     /cmd_vel            geometry_msgs/msg/Twist
base.odometry          subscribe   /odom               nav_msgs/msg/Odometry
sensor.battery         subscribe   /battery_state      sensor_msgs/msg/BatteryState
base.navigate_to       action      navigate_to_pose    nav2_msgs/action/NavigateToPose
safety.stop            —           zero Twist on /cmd_vel + cancel all Nav2 goals
system.health          —           node alive + freshness of subscribed topics
=====================  ==========  ===============================================

What is real here: the mapping table, the manifest builder (limits come from
the operator's config, never guessed) and the pure message-conversion
functions, which are unit-tested with stand-in objects. Since round 04 the
whole adapter (``Ros2Bridge`` plumbing, publishers, subscriptions, Nav2 goal
handling, stop) also passes the conformance suite's ``static`` and ``dry``
levels against a pure-Python **rclpy test double** (``runtime/tests/ros2_double``,
an in-memory graph with a fake robot). What is NOT verified: real rclpy / DDS,
QoS, executor threading, a real Nav2 stack and any robot. Treat it as a
starting point for an integration engineer.

``Ros2Bridge`` (node, private context, executor thread, typed publishers and
subscriptions with freshness, Nav2 clients) and the converters are reused by
adapters generated with ``neuxmind_runtime.scaffold`` for ``ros2:`` mappings.

Usage (on a machine with ROS 2 sourced)::

    BRAIN_EDGE_TOKEN=... python3 -m neuxmind_runtime serve \\
        --adapter neuxmind_runtime.adapters.ros2:Ros2Adapter

Configuration is read from ``NEUXMIND_ROS2_CONFIG`` (JSON file), see
:data:`DEFAULT_CONFIG`.

TODO (before any field use)
---------------------------
* QoS: /odom and /battery_state are often BEST_EFFORT sensor QoS; match the robot.
* Frames: Nav2 goals are in ``map``; odometry is in ``odom``. Expose
  ``localization.pose`` from TF (map→base_link) instead of guessing.
* Velocity watchdog: many base controllers have their own cmd timeout; keep
  the runtime watchdog anyway and confirm the controller honours zero Twist.
* Stop: publish zero Twist and cancel goals — this is NOT an e-stop. Wire the
  vendor's hardware e-stop / ``/emergency_stop`` service if one exists.
* Nav2 feedback: map ``distance_remaining`` to progress; handle ABORTED codes.
* Lifecycle: wait for Nav2 to be active (``bt_navigator``) before declaring
  ``base.navigate_to`` in the manifest.
* Time: use ROS time for stamps when ``use_sim_time`` is set.

Simulation flag
---------------
The manifest's ``simulated`` is ``true`` when the config sets
``"simulated": true`` or ``"use_sim_time": true``, or when the node's ROS
parameter ``use_sim_time`` is true at start-up (Gazebo / Isaac Sim). Results
from a simulated graph must never be reported as device evidence.

Stop / cancel semantics (review R-06)
-------------------------------------
* A cancel requested while ``send_goal_async`` is still pending is applied
  from the send-future's done-callback the moment Nav2 returns the goal
  handle (``cancel_goal_async``) -- it is never just a local flag.
* ``stop()`` publishes zero Twist, cancels every known goal handle, and also
  calls Nav2's cancel-*all*-goals service (``<action>/_action/cancel_goal``
  with a zero goal id and zero stamp), covering goals whose handle has not
  arrived yet or that this process does not know about.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any

from neuxmind_spec.manifest import normalize_latch_guard

from ..clock import RealClock
from ..hal import ActionHandle, ActionStatus, CANCELLED, FAILED, RUNNING, SUCCEEDED, RobotAdapter

DEFAULT_CONFIG: dict[str, Any] = {
    "instance_id": "ros2-robot",
    "name": "ROS 2 robot",
    "vendor": "unknown",
    "model": "unknown",
    "family": "mobile",
    "node_name": "neuxmind_runtime",
    "topics": {"cmd_vel": "/cmd_vel", "odom": "/odom", "battery": "/battery_state"},
    "nav2_action": "navigate_to_pose",
    "map_frame": "map",
    # Limits MUST be filled in by the integrator from the robot's datasheet.
    "limits": {"max_vx": None, "max_vy": 0.0, "max_wz": None, "holonomic": False},
    "stale_after_s": 1.0,
    "hardware_estop": False,
    # Set either to true when bridging a simulator (Gazebo, Isaac Sim, ...).
    "simulated": False,
    "use_sim_time": False,
    # Latched-state guard calibration (round 07, Q6-01/Q6-05): copied into the manifest as
    # safety.latch_guard, e.g. {"stop_settle_s": 3.0, "noise_m": 0.002, "yaw_drift_rad_s": 0.0002}.
    # Keys and bounds: neuxmind_spec.manifest.LATCH_GUARD_FIELDS (validated + clamped at start-up).
    "latch_guard": {},
}


# ---------------------------------------------------------------- converters
# Pure functions: no rclpy needed, unit-testable with stand-in objects.
def twist_fields(args: dict) -> dict:
    """base.velocity inputs → geometry_msgs/Twist field values."""
    return {"linear": {"x": float(args.get("vx", 0.0)), "y": float(args.get("vy", 0.0)), "z": 0.0},
            "angular": {"x": 0.0, "y": 0.0, "z": float(args.get("wz", 0.0))}}


def odometry_outputs(msg) -> dict:
    """nav_msgs/Odometry → base.odometry outputs (pose in the odom frame)."""
    p, q = msg.pose.pose.position, msg.pose.pose.orientation
    v, w = msg.twist.twist.linear, msg.twist.twist.angular
    return {"pose": [p.x, p.y, p.z, q.x, q.y, q.z, q.w], "velocity": [v.x, v.y, w.z]}


def battery_outputs(msg) -> dict:
    """sensor_msgs/BatteryState → sensor.battery outputs.

    ROS ``percentage`` is 0..1 (NaN when unknown); NeuxMind uses 0..100 %.
    """
    if msg.percentage is None or math.isnan(msg.percentage):
        raise ValueError("BatteryState.percentage 未知")
    out = {"percent": float(msg.percentage) * 100.0}
    # POWER_SUPPLY_STATUS_CHARGING == 1
    out["charging"] = int(getattr(msg, "power_supply_status", 0)) == 1
    if getattr(msg, "voltage", None) is not None and not math.isnan(msg.voltage):
        out["voltage"] = float(msg.voltage)
    return out


def imu_outputs(msg) -> dict:
    """sensor_msgs/Imu → sensor.imu outputs."""
    q, w, a = msg.orientation, msg.angular_velocity, msg.linear_acceleration
    return {"orientation": [float(q.x), float(q.y), float(q.z), float(q.w)],
            "angular_velocity": [float(w.x), float(w.y), float(w.z)],
            "linear_acceleration": [float(a.x), float(a.y), float(a.z)]}


def joint_state_outputs(msg) -> dict:
    """sensor_msgs/JointState → joint.state outputs (velocity/effort only when complete)."""
    names, position = [str(n) for n in msg.name], [float(v) for v in msg.position]
    out = {"names": names, "position": position}
    for key in ("velocity", "effort"):
        values = list(getattr(msg, key, []) or [])
        if len(values) == len(position):
            out[key] = [float(v) for v in values]
    return out


def pose_with_covariance_outputs(msg) -> dict:
    """geometry_msgs/PoseWithCovarianceStamped (e.g. /amcl_pose) → localization.pose outputs."""
    p, q = msg.pose.pose.position, msg.pose.pose.orientation
    out = {"pose": [float(p.x), float(p.y), float(p.z), float(q.x), float(q.y), float(q.z), float(q.w)],
           "covariance": [float(v) for v in msg.pose.covariance]}
    frame = getattr(getattr(msg, "header", None), "frame_id", "")
    if frame:
        out["map_id"] = str(frame)
    return out


def nav_goal_fields(args: dict, map_frame: str) -> dict:
    """base.navigate_to inputs → NavigateToPose.Goal.pose (PoseStamped) fields."""
    x, y, z, qx, qy, qz, qw = args["goal"]
    return {"header": {"frame_id": args.get("frame") or map_frame},
            "pose": {"position": {"x": x, "y": y, "z": z},
                     "orientation": {"x": qx, "y": qy, "z": qz, "w": qw}}}


def is_simulated(config: dict, use_sim_time_param: bool = False) -> bool:
    """``simulated`` comes from configuration (or the node's use_sim_time), never hard-coded."""
    for key in ("simulated", "use_sim_time"):
        value = config.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"ROS 2 配置 {key} 须为 true/false")
    return bool(config.get("simulated") or config.get("use_sim_time") or use_sim_time_param)


def build_manifest(config: dict, use_sim_time_param: bool = False) -> dict:
    limits = config["limits"]
    if limits.get("max_vx") is None or limits.get("max_wz") is None:
        raise ValueError("ROS 2 适配器需要在配置 limits 中填写 max_vx 与 max_wz（来自本体规格）")
    simulated = is_simulated(config, use_sim_time_param)
    safety = {"hardware_estop": bool(config["hardware_estop"]), "max_linear_speed": limits["max_vx"]}
    if config.get("latch_guard"):
        normalize_latch_guard(config["latch_guard"], "ROS 2 配置 latch_guard")   # fail fast (raises ValueError)
        safety["latch_guard"] = dict(config["latch_guard"])
    return {
        "schema": "robot.adapter.v2", "spec_version": "1.1",
        "instance_id": config["instance_id"], "name": config["name"],
        "vendor": config["vendor"], "model": config["model"], "family": config["family"],
        "adapter_version": "0.0.1-skeleton", "simulated": simulated,
        "capabilities": [
            {"id": "safety.stop", "params": {"hardware_estop": bool(config["hardware_estop"])}},
            {"id": "system.health"},
            {"id": "base.velocity", "params": {"max_vx": limits["max_vx"], "max_vy": limits.get("max_vy", 0.0),
                                               "max_wz": limits["max_wz"],
                                               "holonomic": bool(limits.get("holonomic", False))}},
            {"id": "base.odometry", "params": {"frame": "odom"}},
            {"id": "sensor.battery"},
            {"id": "base.navigate_to", "params": {"planner": "nav2"}},
        ],
        "model_runtimes": [],
        "safety": safety,
    }


def _reject_constant(name: str):
    raise ValueError(f"ROS 2 配置不允许 {name}（限幅参数必须是有限数值）")


def parse_config_text(text: str) -> dict:
    """Parse a config JSON document; NaN / Infinity are rejected (json accepts them by default)."""
    user = json.loads(text, parse_constant=_reject_constant)
    if not isinstance(user, dict):
        raise ValueError("ROS 2 配置须为 JSON 对象")
    return user


def load_config() -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.environ.get("NEUXMIND_ROS2_CONFIG")
    if path:
        with open(path, encoding="utf-8") as fh:
            user = parse_config_text(fh.read())
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


# ---------------------------------------------------------------- adapter
class Ros2NavHandle(ActionHandle):
    """Wraps a Nav2 NavigateToPose goal. UNVERIFIED against a running Nav2.

    Works with any future offering ``done()``, ``result()`` and
    ``add_done_callback(fn)`` (rclpy.task.Future does), so the cancel race
    is unit-testable without ROS 2.
    """

    def __init__(self, send_future, on_done=None):
        self._send_future = send_future
        self._goal_handle = None
        self._result_future = None
        self._cancel_requested = False
        self._cancel_sent = False
        self._cancel_lock = threading.Lock()     # only guards "send cancel once"
        self._on_done = on_done
        self._finished = False
        send_future.add_done_callback(self._on_goal_sent)

    def _on_goal_sent(self, future) -> None:
        try:
            handle = future.result()
        except Exception:
            return
        self._goal_handle = handle            # publish first, then check the flag (see cancel())
        if self._cancel_requested:
            self._send_cancel()

    def _send_cancel(self) -> None:
        handle = self._goal_handle
        if handle is None or not getattr(handle, "accepted", True):
            return
        with self._cancel_lock:
            if self._cancel_sent:
                return
            self._cancel_sent = True
        try:
            handle.cancel_goal_async()
        except Exception:
            pass

    def _finish(self, status: ActionStatus) -> ActionStatus:
        if not self._finished:
            self._finished = True
            if self._on_done is not None:
                self._on_done(self)
        return status

    def poll(self) -> ActionStatus:
        if self._cancel_requested:
            return self._finish(ActionStatus(state=CANCELLED, error="cancelled"))
        if self._goal_handle is None:
            if not self._send_future.done():
                return ActionStatus(state=RUNNING)
            self._goal_handle = self._send_future.result()
        if not self._goal_handle.accepted:
            return self._finish(ActionStatus(state=FAILED, error="Nav2 拒绝目标"))
        if self._result_future is None:
            self._result_future = self._goal_handle.get_result_async()
        if not self._result_future.done():
            return ActionStatus(state=RUNNING)
        # action_msgs/GoalStatus: 4=SUCCEEDED, 5=CANCELED, 6=ABORTED.
        status = self._result_future.result().status
        if status == 4:
            return self._finish(ActionStatus(state=SUCCEEDED, progress=1.0, result={"reached": True}))
        if status == 5:
            return self._finish(ActionStatus(state=CANCELLED, error="Nav2 目标已取消"))
        return self._finish(ActionStatus(state=FAILED, error=f"Nav2 结束状态 {status}"))

    def cancel(self) -> None:
        # Set the flag first, then look at the handle: together with
        # _on_goal_sent (handle first, then flag) at least one side sends the cancel.
        self._cancel_requested = True
        if self._goal_handle is not None:
            self._send_cancel()


def _import(path: str):
    """``"geometry_msgs.msg:Twist"`` → the class (ImportError when ROS 2 is not sourced)."""
    import importlib
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


class Ros2Bridge:
    """rclpy plumbing shared by :class:`Ros2Adapter` and generated adapters.

    One private ``rclpy`` context and node per bridge, a ``MultiThreadedExecutor``
    spinning on a daemon thread, typed publishers, subscriptions that keep only
    the latest message with its arrival time (``fresh`` refuses stale data), a
    Nav2 ``NavigateToPose`` client and the cancel-all-goals service client.
    Subscription callbacks only assign into a dict: no lock is shared with the
    stop path.
    """

    REQUIRED = ("rclpy", "rclpy.action", "rclpy.executors", "rclpy.context")

    @classmethod
    def require(cls, messages=()) -> None:
        """Raise RuntimeError (clear message) unless rclpy and the given message modules import."""
        import importlib
        try:
            for name in (*cls.REQUIRED, *messages):
                importlib.import_module(name)
        except ImportError as exc:
            raise RuntimeError(
                "ROS 2 适配器需要 ROS 2 环境（rclpy 与所用消息包）；请先 source ROS 2 安装目录。"
                f"导入失败：{exc}") from None

    def __init__(self, node_name: str, stale_after_s: float = 1.0):
        self.require()
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import MultiThreadedExecutor
        self._rclpy = rclpy
        self.stale_after_s = float(stale_after_s)
        self._latest: dict[str, tuple[float, Any]] = {}
        self._context = Context()
        rclpy.init(args=None, context=self._context)
        self.node = rclpy.create_node(node_name, context=self._context)
        try:
            self.use_sim_time = bool(self.node.get_parameter("use_sim_time").value)
        except Exception:
            self.use_sim_time = False
        self._executor = MultiThreadedExecutor(context=self._context)
        self._executor.add_node(self.node)
        self._spin = threading.Thread(target=self._executor.spin, name="nx-ros2-spin", daemon=True)
        self._spin.start()

    # ---------------------------------------------------------- topics
    def publisher(self, msg_type: str, topic: str, qos: int = 10):
        """``msg_type`` like ``"geometry_msgs.msg:Twist"``; returns ``(publisher, message class)``."""
        cls = _import(msg_type)
        return self.node.create_publisher(cls, topic, qos), cls

    def subscribe(self, key: str, msg_type: str, topic: str, qos: int = 10) -> None:
        # TODO(field): sensor topics are often BEST_EFFORT; match the robot's QoS profile.
        cls = _import(msg_type)
        self.node.create_subscription(cls, topic, lambda m, key=key: self._store(key, m), qos)

    def _store(self, key: str, msg) -> None:
        self._latest[key] = (time.monotonic(), msg)   # atomic dict assignment, no lock

    def fresh(self, key: str):
        item = self._latest.get(key)
        if item is None or time.monotonic() - item[0] > self.stale_after_s:
            raise RuntimeError(f"{key} 话题无新数据")
        return item[1]

    def freshness_errors(self, keys) -> dict:
        errors = {}
        for key in keys:
            try:
                self.fresh(key)
            except RuntimeError as exc:
                errors[key] = str(exc)
        return errors

    # ---------------------------------------------------------- Nav2
    def nav2_client(self, action_name: str):
        """``(ActionClient, NavigateToPose, cancel_all_client | None, CancelGoal | None)``."""
        from rclpy.action import ActionClient
        nav_type = _import("nav2_msgs.action:NavigateToPose")
        client = ActionClient(self.node, nav_type, action_name)
        try:
            cancel_type = _import("action_msgs.srv:CancelGoal")
            cancel = self.node.create_client(cancel_type, action_name.rstrip("/") + "/_action/cancel_goal")
        except Exception:
            cancel_type, cancel = None, None
        return client, nav_type, cancel, cancel_type

    def now_msg(self):
        return self.node.get_clock().now().to_msg()

    def close(self) -> None:
        try:
            self._executor.shutdown()
            self.node.destroy_node()
        finally:
            self._rclpy.shutdown(context=self._context)


def publish_twist(publisher, twist_cls, args: dict) -> None:
    fields = twist_fields(args)
    msg = twist_cls()
    msg.linear.x, msg.linear.y = fields["linear"]["x"], fields["linear"]["y"]
    msg.angular.z = fields["angular"]["z"]
    publisher.publish(msg)


def send_nav_goal(bridge: Ros2Bridge, client, nav_type, args: dict, map_frame: str, on_done=None) -> "Ros2NavHandle":
    if not client.server_is_ready():
        raise RuntimeError("Nav2 NavigateToPose 动作服务不可用")
    goal = nav_type.Goal()
    fields = nav_goal_fields(args, map_frame)
    goal.pose.header.frame_id = fields["header"]["frame_id"]
    goal.pose.header.stamp = bridge.now_msg()
    position, orientation = fields["pose"]["position"], fields["pose"]["orientation"]
    goal.pose.pose.position.x, goal.pose.pose.position.y = position["x"], position["y"]
    goal.pose.pose.position.z = position["z"]
    goal.pose.pose.orientation.x, goal.pose.pose.orientation.y = orientation["x"], orientation["y"]
    goal.pose.pose.orientation.z, goal.pose.pose.orientation.w = orientation["z"], orientation["w"]
    return Ros2NavHandle(client.send_goal_async(goal), on_done=on_done)


class Ros2Adapter(RobotAdapter):
    """ROS 2 bridge. Requires ``rclpy`` and the message packages listed above."""

    def __init__(self, config: dict | None = None):
        Ros2Bridge.require(("geometry_msgs.msg", "nav_msgs.msg", "sensor_msgs.msg", "nav2_msgs.action"))
        self.clock = RealClock()
        self.config = config or load_config()
        build_manifest(self.config)            # fail fast on missing limits before touching ROS
        self._bridge = Ros2Bridge(self.config["node_name"], self.config["stale_after_s"])
        try:
            self._manifest = build_manifest(self.config, self._bridge.use_sim_time)
            topics = self.config["topics"]
            self._cmd_pub, self._Twist = self._bridge.publisher("geometry_msgs.msg:Twist", topics["cmd_vel"])
            self._bridge.subscribe("odom", "nav_msgs.msg:Odometry", topics["odom"])
            self._bridge.subscribe("battery", "sensor_msgs.msg:BatteryState", topics["battery"])
            self._nav, self._NavigateToPose, self._cancel_all, self._CancelGoal = \
                self._bridge.nav2_client(self.config["nav2_action"])
        except BaseException:
            self._bridge.close()
            raise
        self._nav_handles: set[Ros2NavHandle] = set()

    def _fresh(self, key: str):
        return self._bridge.fresh(key)

    def manifest(self) -> dict:
        return self._manifest

    def read(self, cap_id: str) -> dict:
        if cap_id == "system.health":
            errors = self._bridge.freshness_errors(("odom", "battery"))
            return {"ok": not errors, "mode": "ros2", "errors": errors}
        if cap_id == "base.odometry":
            return odometry_outputs(self._fresh("odom"))
        if cap_id == "sensor.battery":
            return battery_outputs(self._fresh("battery"))
        raise ValueError(f"Ros2Adapter 不能读取 {cap_id}")

    def _publish_twist(self, args: dict) -> None:
        publish_twist(self._cmd_pub, self._Twist, args)

    def invoke(self, cap_id: str, args: dict) -> dict:
        if cap_id == "base.velocity":
            self._publish_twist(args)
            return {"accepted": True}
        raise ValueError(f"Ros2Adapter 不支持命令 {cap_id}")

    def start_action(self, cap_id: str, args: dict) -> ActionHandle:
        if cap_id != "base.navigate_to":
            raise ValueError(f"Ros2Adapter 不支持动作 {cap_id}")
        handle = send_nav_goal(self._bridge, self._nav, self._NavigateToPose, args, self.config["map_frame"],
                               on_done=self._nav_handles.discard)
        self._nav_handles.add(handle)
        return handle

    def _cancel_all_goals(self) -> None:
        """Nav2 cancel-all: CancelGoal with zero goal_id + zero stamp. Non-blocking."""
        if self._cancel_all is None:
            return
        self._cancel_all.call_async(self._CancelGoal.Request())

    def stop(self) -> None:
        stop_adapter(self)

    def close(self) -> None:
        self._bridge.close()


def stop_adapter(adapter) -> None:
    """Stop logic shared by Ros2Adapter.stop (module-level so it is testable without rclpy).

    Not an e-stop: zero velocity, cancel every known goal *and* ask Nav2 to
    cancel all goals. Each step runs even if an earlier one fails.
    """
    errors = []
    for step in (lambda: adapter._publish_twist({"vx": 0.0, "vy": 0.0, "wz": 0.0}),
                 lambda: [h.cancel() for h in list(adapter._nav_handles)],
                 adapter._cancel_all_goals):
        try:
            step()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError(f"ROS 2 停止部分失败：{errors}")
