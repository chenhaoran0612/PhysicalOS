"""ROS 2 binding of the standard capabilities (spec 1.1, ROS 2 Humble).

On a body that is driven through ROS 2, a standard capability is carried by one ROS 2 interface:
a topic or an action with a message type. This table says which, so that every component judges
"is this the right kind of topic for that capability" the same way: the desktop's motion driver
check and device interface table, the runtime's ROS 2 bridge and scaffold, the SDK.

Reading an entry
----------------
* ``kind``: ``topic`` or ``action``.
* ``role``: what the *body's own driver* does with the interface. ``subscribes``: commands go in
  (the platform publishes); ``publishes``: state comes out; ``serves``: the driver is the action
  server. A component that talks to the driver has the opposite role.
* ``types``: the message types that can carry the capability, spelled the way ``ros2 topic list -t``
  prints them (``package/msg/Type``, ``package/action/Type``). The first is the conventional one.
* ``name``: the name the interface conventionally has. A hint only: robots namespace and remap
  their topics, so a device is matched by type, and which topic it really uses is settled per device.

Not every capability is in the table. ``safety.stop`` has no interface of its own: a ROS 2 body
is stopped through its motion interfaces (zero velocity, cancelled goals). ``legged.posture`` and
``arm.ee_pose`` are served by vendor-specific or stack-specific interfaces the standard does not
fix. :func:`get` returns ``None`` for them, and callers must treat that as "cannot be checked
from the ROS 2 graph", never as "missing".

The entries the runtime's bridge implements today are the ones of ``neuxmind_runtime.scaffold``
(``ROS2_TEMPLATES``); a test in the desktop suite keeps the two in step. The others name the
conventional ROS 2 type for the capability and are not implemented by the bridge yet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .capabilities import SPEC_VERSION

DISTRO = "humble"
KINDS = ("topic", "action")
ROLES = ("subscribes", "publishes", "serves")
_TYPE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)/(msg|action)/([A-Za-z][A-Za-z0-9_]*)")
# Other spellings of the same type: "pkg.msg:Type" (a Python import path), "pkg/Type" (the ROS 1 habit).
_IMPORT_PATH = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\.(msg|action):([A-Za-z][A-Za-z0-9_]*)")
_SHORT = re.compile(r"([A-Za-z][A-Za-z0-9_]*)/([A-Za-z][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Ros2Interface:
    capability: str
    kind: str
    role: str
    types: tuple[str, ...]
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "kind": self.kind, "role": self.role,
                "types": list(self.types), "name": self.name}


def _topic(capability: str, role: str, name: str, *types: str) -> Ros2Interface:
    return Ros2Interface(capability, "topic", role, types, name)


def _action(capability: str, name: str, *types: str) -> Ros2Interface:
    return Ros2Interface(capability, "action", "serves", types, name)


INTERFACES = MappingProxyType({interface.capability: interface for interface in [
    # ---- motion ----------------------------------------------------------
    _topic("base.velocity", "subscribes", "/cmd_vel", "geometry_msgs/msg/Twist"),
    _topic("base.odometry", "publishes", "/odom", "nav_msgs/msg/Odometry"),
    _action("base.navigate_to", "/navigate_to_pose", "nav2_msgs/action/NavigateToPose"),
    _topic("joint.state", "publishes", "/joint_states", "sensor_msgs/msg/JointState"),
    _action("arm.joint_position", "/joint_trajectory_controller/follow_joint_trajectory",
            "control_msgs/action/FollowJointTrajectory"),
    _action("gripper.position", "/gripper_controller/gripper_cmd", "control_msgs/action/GripperCommand"),
    # ---- sensing ---------------------------------------------------------
    _topic("sensor.battery", "publishes", "/battery_state", "sensor_msgs/msg/BatteryState"),
    _topic("sensor.imu", "publishes", "/imu", "sensor_msgs/msg/Imu"),
    _topic("sensor.camera.rgb", "publishes", "/camera/image_raw", "sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage"),
    _topic("sensor.camera.depth", "publishes", "/camera/depth/image_raw", "sensor_msgs/msg/Image"),
    _topic("sensor.lidar", "publishes", "/scan", "sensor_msgs/msg/LaserScan", "sensor_msgs/msg/PointCloud2"),
    _topic("localization.pose", "publishes", "/amcl_pose", "geometry_msgs/msg/PoseWithCovarianceStamped"),
]})


def get(cap_id: Any) -> Ros2Interface | None:
    """The ROS 2 interface of a standard capability, or None when the standard binds it to none."""
    return INTERFACES.get(cap_id) if isinstance(cap_id, str) else None


def canonical_type(text: Any) -> str:
    """One spelling for a ROS 2 type: ``package/msg/Type`` or ``package/action/Type``.

    Accepts that spelling, ``package.msg:Type`` and the short ``package/Type`` (a message).
    """
    value = text.strip() if isinstance(text, str) else ""
    for pattern, build in ((_TYPE, lambda m: m.group(0)), (_IMPORT_PATH, lambda m: "/".join(m.groups())),
                           (_SHORT, lambda m: f"{m.group(1)}/msg/{m.group(2)}")):
        match = pattern.fullmatch(value)
        if match:
            return build(match)
    raise ValueError(f"不是 ROS 2 消息类型的写法：{str(text)[:100]}（应为 包/msg/类型 或 包/action/类型）")


def accepts(cap_id: Any, ros_type: Any) -> bool:
    """Whether an interface of this type can carry the capability. False when there is no binding."""
    interface = get(cap_id)
    if interface is None:
        return False
    try:
        return canonical_type(ros_type) in interface.types
    except ValueError:
        return False


def registry() -> dict:
    """JSON-serialisable table, for desktop, cloud and docs."""
    return {"schema": "neuxmind.ros2_interfaces.v1", "spec_version": SPEC_VERSION, "distro": DISTRO,
            "interfaces": [interface.to_dict() for interface in INTERFACES.values()]}
