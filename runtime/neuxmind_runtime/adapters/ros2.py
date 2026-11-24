"""ROS 2 bridge adapter for a device that runs ROS 2 Humble (only Humble is accepted).

Maps NeuxMind standard capabilities onto common ROS 2 interfaces:

=====================  ==========  ===============================================
capability             direction   ROS 2 interface (default name)
=====================  ==========  ===============================================
base.velocity          publish     /cmd_vel            geometry_msgs/msg/Twist
base.odometry          subscribe   /odom               nav_msgs/msg/Odometry
sensor.battery         subscribe   /battery_state      sensor_msgs/msg/BatteryState
base.navigate_to       action      navigate_to_pose    nav2_msgs/action/NavigateToPose
safety.stop            —           zero Twist on /cmd_vel + cancel all Nav2 goals
system.health          —           freshness of the subscribed topics
=====================  ==========  ===============================================

``sensor.battery`` is declared only when ``topics.battery`` is a name (null: the
device has no battery topic), ``base.navigate_to`` only when Nav2 is there
(see "Nav2"). Topic names come from :data:`DEFAULT_CONFIG` and the JSON file
that ``NEUXMIND_ROS2_CONFIG`` names; there is no per-device interface table.

What has been run (issue #21): the pure converters and the manifest builder
with stand-in objects; the whole adapter through the conformance suite on a
pure-Python **rclpy test double** (``runtime/tests/ros2_double``); and the
adapter in a separate runtime process against **real rclpy nodes on ROS 2
Humble** (Fast DDS, ``runtime/tests/ros2_humble``: reads, the QoS seen on the
DDS graph, refusal without safety limits, stop, clamped velocity, watchdog,
an incompatible QoS). On one real robot (a Jetson Orin NX running ROS 2
Humble, 2026-11-21, read-only): installed with ``device_install.sh``, odometry
read from the robot's own odometry topic with health ok, motion refused for
want of filled limits; the velocity topic was pointed at a name nothing
subscribes to and Nav2 was off, so nothing reached its controller. What has
not been run: a real Nav2 stack, and any motion on a robot.

``Ros2Bridge`` (node, private context, executor thread, typed publishers and
subscriptions with freshness, Nav2 clients) and the converters are reused by
adapters generated with ``neuxmind_runtime.scaffold`` for ``ros2:`` mappings;
the Humble check is the adapter's, not the bridge's.

Usage (on a machine with ROS 2 Humble sourced; ``runtime/deploy/device_install.sh``
with ``NX_ADAPTER=ros2`` does this for a device)::

    BRAIN_EDGE_TOKEN=... python3 -m neuxmind_runtime serve --adapter ros2

Configuration is read from ``NEUXMIND_ROS2_CONFIG`` (JSON file), see
:data:`DEFAULT_CONFIG`.

QoS
---
``qos`` holds, per topic key of ``topics``, any of ``reliability``
(reliable / best_effort), ``durability`` (volatile / transient_local),
``history`` (keep_last / keep_all) and ``depth`` (1–100); the rest comes
from :data:`DEFAULT_QOS`. Subscriptions default to best effort, which receives
from reliable and best-effort publishers alike; a reliable subscription gets
nothing from a best-effort publisher (ROS logs "incompatible QoS" and health
reports the topic as stale). The velocity publisher is fixed: reliable,
volatile, keep last 1, because the stop is a zero Twist on it (see
``COMMAND_QOS_FIXED``).

Nav2
----
``nav2`` is ``"auto"`` (default: navigation is declared when ``nav2_msgs``
imports), ``false`` (never) or ``true`` (refuse to start without it). Without
Nav2 nothing else changes; ``stop()`` then only publishes the zero Twist. With
``false`` the bridge does not talk to Nav2 at all, so ``stop()`` also does not
cancel goals that other software on the device gave to a Nav2 running there;
keep ``"auto"`` when NeuxMind's stop should end those too.

TODO (before any field use)
---------------------------
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

Limits
------
``limits.max_vx`` / ``limits.max_wz`` in the configuration are what the body
itself allows (its datasheet). They may be left out (issue #22, R1-06): the
bridge then starts all the same, its manifest declares no speed limits, and
the runtime refuses every motion command (``safety_limits_missing``) until a
person has filled in this device's safety limits (``POST /v2/safety/limits``,
the console's "安全限值"). When both exist the lower one is enforced. This
adapter never invents a value. The filled limits are kept for the body that
``instance_id`` (with vendor, model and the declared limits) names: give every
robot its own ``instance_id`` in the configuration, not the default.

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
    # Issue #21: per topic (same keys as "topics"), any of reliability / durability / history / depth;
    # what is not given comes from DEFAULT_QOS. See "QoS" above.
    "qos": {},
    # Issue #21: "auto" declares base.navigate_to when nav2_msgs is installed, false never, true
    # refuses to start without it. See "Nav2" above.
    "nav2": "auto",
    "nav2_action": "navigate_to_pose",
    "map_frame": "map",
    # What the body itself allows, from its datasheet; never guessed. May stay null: nothing moves
    # then until a person has filled in this device's safety limits (see "Limits" above).
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

SUPPORTED_DISTRO = "humble"          # R1: the device runs ROS 2 Humble; nothing else is accepted
ADAPTER_VERSION = "0.1.0"            # 0.0.1-skeleton until issue #21

# Issue #21. Subscriptions default to best effort: that receives from reliable and from best-effort
# (sensor QoS) publishers alike. The command publisher is reliable, which reaches both kinds of readers.
DEFAULT_QOS: dict[str, dict] = {
    "cmd_vel": {"reliability": "reliable", "durability": "volatile", "history": "keep_last", "depth": 1},
    "odom": {"reliability": "best_effort", "durability": "volatile", "history": "keep_last", "depth": 10},
    "battery": {"reliability": "best_effort", "durability": "volatile", "history": "keep_last", "depth": 10},
}
QOS_CHOICES = {"reliability": ("reliable", "best_effort"), "durability": ("volatile", "transient_local"),
               "history": ("keep_last", "keep_all")}
QOS_MAX_DEPTH = 100                  # only the newest message of a subscription is ever used
# The stop is a zero Twist on the command publisher. It must arrive (reliable), must not be replayed
# to a controller that starts later (volatile: transient local would hand it the last command, which
# may not be the zero one), publishing must never wait for a slow reader (keep last), and a reader that
# lags behind must get the newest command first, not a queue of older ones ahead of the zero (depth 1;
# found in review, the depth used to be 10).
COMMAND_QOS_FIXED = {"reliability": "reliable", "durability": "volatile", "history": "keep_last", "depth": 1}


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


def check_distro(environ) -> None:
    """Raise RuntimeError (with what was found) unless ``environ`` is a sourced ROS 2 Humble."""
    distro, version = environ.get("ROS_DISTRO"), environ.get("ROS_VERSION")
    if not distro:
        raise RuntimeError("没有 ROS 环境（ROS_DISTRO 未设置）：ROS 2 适配器只支持 ROS 2 Humble，"
                           "请先 source /opt/ros/humble/setup.bash")
    if distro != SUPPORTED_DISTRO or (version not in (None, "", "2")):
        raise RuntimeError(f"这台设备的 ROS 是 {distro}（ROS {version or '?'}），ROS 2 适配器只支持 ROS 2 Humble")


def nav2_mode(config: dict):
    """``"auto"``, ``True`` or ``False`` (ValueError otherwise)."""
    value = config.get("nav2", "auto")
    if value == "auto" or value is True or value is False:
        return value
    raise ValueError(f"ROS 2 配置 nav2 须为 \"auto\"、true 或 false，收到 {value!r}")


def qos_settings(config: dict) -> dict[str, dict]:
    """The QoS of every topic: :data:`DEFAULT_QOS` with the configuration's ``qos`` laid over it, checked."""
    given = config.get("qos") or {}
    if not isinstance(given, dict):
        raise ValueError("ROS 2 配置 qos 须为对象：{话题: {reliability, durability, history, depth}}")
    unknown = sorted(set(given) - set(DEFAULT_QOS))
    if unknown:
        raise ValueError(f"ROS 2 配置 qos 里有不认识的话题 {unknown}；可用 {sorted(DEFAULT_QOS)}")
    settings = {}
    for topic, defaults in DEFAULT_QOS.items():
        fields = given.get(topic, {})
        if not isinstance(fields, dict):
            raise ValueError(f"ROS 2 配置 qos.{topic} 须为对象")
        extra = sorted(set(fields) - set(defaults))
        if extra:
            raise ValueError(f"ROS 2 配置 qos.{topic} 里有不支持的项 {extra}；可用 {sorted(defaults)}")
        merged = {**defaults, **fields}
        for name, choices in QOS_CHOICES.items():
            if merged[name] not in choices:
                raise ValueError(f"ROS 2 配置 qos.{topic}.{name} 须为 {' / '.join(choices)}，收到 {merged[name]!r}")
        depth = merged["depth"]
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= QOS_MAX_DEPTH:
            raise ValueError(f"ROS 2 配置 qos.{topic}.depth 须为 1–{QOS_MAX_DEPTH} 的整数，收到 {depth!r}")
        settings[topic] = merged
    for name, required in COMMAND_QOS_FIXED.items():
        if settings["cmd_vel"][name] != required:
            raise ValueError(f"ROS 2 配置 qos.cmd_vel.{name} 只能是 {required}：停止是这个话题上的一条零速度，"
                             "它必须送达、不能被重放给之后才启动的控制器、不能排在旧指令后面，发布也不能等待读者")
    return settings


def topic_names(config: dict) -> dict:
    """``topics`` checked: ``cmd_vel`` and ``odom`` are required, ``battery`` may be null (no battery topic)."""
    topics = config.get("topics")
    if not isinstance(topics, dict):
        raise ValueError("ROS 2 配置 topics 须为对象")
    for key in ("cmd_vel", "odom", "battery"):
        name = topics.get(key)
        if key == "battery" and name is None:
            continue                     # issue #21: a device without a battery topic has no sensor.battery
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"ROS 2 配置 topics.{key} 须为话题名" + ("（可为 null：没有电池话题）" if key == "battery" else ""))
    return topics


def build_manifest(config: dict, use_sim_time_param: bool = False, navigation: bool | None = None) -> dict:
    """``navigation``: whether Nav2 is there (the adapter probes it); ``None`` follows the configuration."""
    topics = topic_names(config)
    limits = config["limits"]
    # Issue #22: a limit that is not configured is not declared (and not replaced by a default).
    # The supervisor refuses all motion on a chassis without declared or filled limits.
    velocity = {name: limits[name] for name in ("max_vx", "max_wz") if limits.get(name) is not None}
    velocity.update(max_vy=limits.get("max_vy", 0.0), holonomic=bool(limits.get("holonomic", False)))
    simulated = is_simulated(config, use_sim_time_param)
    safety = {"hardware_estop": bool(config["hardware_estop"])}
    if "max_vx" in velocity:
        safety["max_linear_speed"] = velocity["max_vx"]
    if config.get("latch_guard"):
        normalize_latch_guard(config["latch_guard"], "ROS 2 配置 latch_guard")   # fail fast (raises ValueError)
        safety["latch_guard"] = dict(config["latch_guard"])
    qos_settings(config)                                                        # fail fast (raises ValueError)
    if navigation is None:
        navigation = nav2_mode(config) is not False
    capabilities = [
        {"id": "safety.stop", "params": {"hardware_estop": bool(config["hardware_estop"])}},
        {"id": "system.health"},
        {"id": "base.velocity", "params": velocity},
        {"id": "base.odometry", "params": {"frame": "odom"}},
    ]
    if topics.get("battery") is not None:
        capabilities.append({"id": "sensor.battery"})
    if navigation:                       # issue #21: without Nav2 there is no navigation, nothing else changes
        capabilities.append({"id": "base.navigate_to", "params": {"planner": "nav2"}})
    return {
        "schema": "robot.adapter.v2", "spec_version": "1.1",
        "instance_id": config["instance_id"], "name": config["name"],
        "vendor": config["vendor"], "model": config["model"], "family": config["family"],
        "adapter_version": ADAPTER_VERSION, "simulated": simulated,
        "capabilities": capabilities,
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

    @staticmethod
    def importable(module: str) -> bool:
        import importlib
        try:
            importlib.import_module(module)
        except ImportError:
            return False
        return True

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
    @staticmethod
    def qos_profile(qos):
        """An int (history depth, rclpy's shorthand) as it is; a :func:`qos_settings` entry as a QoSProfile."""
        if not isinstance(qos, dict):
            return qos
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        return QoSProfile(history=getattr(HistoryPolicy, qos["history"].upper()), depth=qos["depth"],
                          reliability=getattr(ReliabilityPolicy, qos["reliability"].upper()),
                          durability=getattr(DurabilityPolicy, qos["durability"].upper()))

    def publisher(self, msg_type: str, topic: str, qos=10):
        """``msg_type`` like ``"geometry_msgs.msg:Twist"``; returns ``(publisher, message class)``."""
        cls = _import(msg_type)
        return self.node.create_publisher(cls, topic, self.qos_profile(qos)), cls

    def subscribe(self, key: str, msg_type: str, topic: str, qos=10) -> None:
        cls = _import(msg_type)
        self.node.create_subscription(cls, topic, lambda m, key=key: self._store(key, m), self.qos_profile(qos))

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
        Ros2Bridge.require(("geometry_msgs.msg", "nav_msgs.msg", "sensor_msgs.msg"))
        check_distro(os.environ)             # issue #21: only ROS 2 Humble, before any node exists
        self.clock = RealClock()
        self.config = config or load_config()
        build_manifest(self.config)            # fail fast on a bad configuration before touching ROS
        qos = qos_settings(self.config)
        # Issue #21: Nav2 is optional. Without it there is no base.navigate_to; stop() then only
        # publishes the zero Twist (it has no goal to cancel), so the stop path gains no dependency.
        mode = nav2_mode(self.config)
        self.navigation = mode is not False and Ros2Bridge.importable("nav2_msgs.action")
        if mode is True and not self.navigation:
            raise RuntimeError("ROS 2 配置 nav2=true，但这台设备没有装 Nav2（nav2_msgs 不能导入）")
        self._nav = self._NavigateToPose = self._cancel_all = self._CancelGoal = None
        self._nav_handles: set[Ros2NavHandle] = set()
        self._fresh_keys: tuple[str, ...] = ("odom",)    # topics whose freshness makes system.health
        self._bridge = Ros2Bridge(self.config["node_name"], self.config["stale_after_s"])
        try:
            self._manifest = build_manifest(self.config, self._bridge.use_sim_time, navigation=self.navigation)
            topics = self.config["topics"]
            self._cmd_pub, self._Twist = self._bridge.publisher("geometry_msgs.msg:Twist", topics["cmd_vel"],
                                                                qos["cmd_vel"])
            self._bridge.subscribe("odom", "nav_msgs.msg:Odometry", topics["odom"], qos["odom"])
            if topics.get("battery") is not None:
                self._bridge.subscribe("battery", "sensor_msgs.msg:BatteryState", topics["battery"], qos["battery"])
                self._fresh_keys = ("odom", "battery")
            if self.navigation:
                self._nav, self._NavigateToPose, self._cancel_all, self._CancelGoal = \
                    self._bridge.nav2_client(self.config["nav2_action"])
        except BaseException:
            self._bridge.close()
            raise

    def _fresh(self, key: str):
        return self._bridge.fresh(key)

    def manifest(self) -> dict:
        return self._manifest

    def read(self, cap_id: str) -> dict:
        if cap_id == "system.health":
            errors = self._bridge.freshness_errors(self._fresh_keys)
            return {"ok": not errors, "mode": "ros2", "errors": errors}
        if cap_id == "base.odometry":
            return odometry_outputs(self._fresh("odom"))
        if cap_id == "sensor.battery" and "battery" in self._fresh_keys:
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
        if self._nav is None:
            raise ValueError("Ros2Adapter 不支持动作 base.navigate_to：这台设备没有 Nav2，或配置里关掉了（nav2=false）")
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
