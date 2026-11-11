"""NeuxMind standard capability registry, version 1.

This is the cross-embodiment contract. Skills and models are written against
capability IDs defined here; each robot body implements whichever subset it
physically supports through a RobotAdapter (see ``runtime/``).

Design rules
------------
* A capability ID is ``<domain>.<name>`` and never changes meaning. Breaking
  changes create a new ``major`` version of that capability.
* Every field declares a type and, for physical quantities, an SI unit.
  Angles are radians, distances metres, time seconds, poses are
  (x, y, z, qx, qy, qz, qw) in the frame named by ``frame``.
* ``kind`` tells the runtime how the capability is used:
    - ``state``   : read-only snapshot, polled (``read``)
    - ``stream``  : sensor data, latest sample or a reference to a buffer
    - ``command`` : short, non-blocking setpoint (``invoke``)
    - ``action``  : long-running goal with progress, result and cancel
* ``safety`` classifies physical risk:
    - ``none``    : cannot move the robot
    - ``motion``  : can move the robot; requires hardware authorization and
                    is always subject to the safety supervisor
    - ``system``  : changes software/firmware on the robot
* ``safety.stop`` and ``system.health`` are mandatory for every body.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

SPEC_VERSION = "1.1"

# Field type vocabulary --------------------------------------------------
# float, int, bool, str, enum, vector (fixed-length float list),
# floats (variable-length float list), pose, blob_ref (URI to binary data)
FIELD_TYPES = {"float", "int", "bool", "str", "enum", "vector", "floats", "pose", "blob_ref", "object"}
KINDS = {"state", "stream", "command", "action"}
SAFETY = {"none", "motion", "system"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    unit: str = ""
    description: str = ""
    required: bool = True
    length: int | None = None          # for ``vector``
    choices: tuple[str, ...] = ()      # for ``enum``
    positive: bool = False             # numeric value must be finite and > 0
    nonnegative: bool = False          # numeric value must be finite and >= 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required}
        if self.unit:
            out["unit"] = self.unit
        if self.description:
            out["description"] = self.description
        if self.length is not None:
            out["length"] = self.length
        if self.choices:
            out["choices"] = list(self.choices)
        if self.positive:
            out["positive"] = True
        if self.nonnegative:
            out["nonnegative"] = True
        return out


@dataclass(frozen=True)
class Capability:
    id: str
    major: int
    kind: str
    safety: str
    title: str
    description: str
    inputs: tuple[Field, ...] = ()
    outputs: tuple[Field, ...] = ()
    # Parameters the adapter declares in its manifest for this capability,
    # e.g. joint count, max speed. Skills may constrain these.
    params: tuple[Field, ...] = ()
    mandatory: bool = False
    tags: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "major": self.major, "kind": self.kind, "safety": self.safety,
            "title": self.title, "description": self.description, "mandatory": self.mandatory,
            "inputs": [f.to_dict() for f in self.inputs],
            "outputs": [f.to_dict() for f in self.outputs],
            "params": [f.to_dict() for f in self.params],
            "tags": list(self.tags),
        }


F = Field
_POSE = "x,y,z,qx,qy,qz,qw"

CAPABILITIES: dict[str, Capability] = {c.id: c for c in [
    # ---- mandatory -------------------------------------------------------
    Capability("safety.stop", 1, "command", "none", "停止", "立即停止所有运动并保持安全状态。必须始终可调用，不受任务队列、模型或网络状态阻塞。",
               inputs=(F("reason", "str", required=False),),
               outputs=(F("stopped", "bool"),),
               params=(F("hardware_estop", "bool", description="本体是否有独立硬件急停"),),
               mandatory=True, tags=("safety",)),
    Capability("system.health", 1, "state", "none", "系统健康", "本体在线状态、错误与运行模式。",
               outputs=(F("ok", "bool"), F("mode", "str", description="厂商运行模式"),
                        F("errors", "object", required=False, description="错误码 → 描述")),
               mandatory=True, tags=("system",)),
    # ---- state / sensing -------------------------------------------------
    Capability("sensor.battery", 1, "state", "none", "电池", "电量与充电状态。",
               outputs=(F("percent", "float", "%"), F("charging", "bool", required=False), F("voltage", "float", "V", required=False))),
    Capability("sensor.imu", 1, "stream", "none", "IMU", "姿态、角速度、线加速度。",
               outputs=(F("orientation", "vector", length=4, description="qx,qy,qz,qw"),
                        F("angular_velocity", "vector", "rad/s", length=3), F("linear_acceleration", "vector", "m/s^2", length=3)),
               params=(F("rate_hz", "float", "Hz", positive=True), F("frame", "str"))),
    Capability("sensor.camera.rgb", 1, "stream", "none", "彩色相机", "最新图像帧引用。",
               outputs=(F("image", "blob_ref"), F("width", "int", "px"), F("height", "int", "px"), F("stamp", "float", "s")),
               params=(F("name", "str"), F("frame", "str"), F("rate_hz", "float", "Hz", positive=True),
                       F("intrinsics", "vector", length=4, required=False, description="fx,fy,cx,cy"))),
    Capability("sensor.camera.depth", 1, "stream", "none", "深度相机", "最新深度帧引用。",
               outputs=(F("depth", "blob_ref"), F("width", "int", "px"), F("height", "int", "px"), F("stamp", "float", "s")),
               params=(F("name", "str"), F("frame", "str"), F("range_max", "float", "m", positive=True))),
    Capability("sensor.lidar", 1, "stream", "none", "激光雷达", "最新点云或扫描引用。",
               outputs=(F("points", "blob_ref"), F("stamp", "float", "s")),
               params=(F("frame", "str"), F("range_max", "float", "m", positive=True), F("dimensions", "enum", choices=("2d", "3d")))),
    Capability("base.odometry", 1, "state", "none", "里程计", "移动底盘的位姿与速度估计。",
               outputs=(F("pose", "pose", description=_POSE), F("velocity", "vector", length=3, description="vx m/s, vy m/s, wz rad/s")),
               params=(F("frame", "str"),)),
    Capability("localization.pose", 1, "state", "none", "定位", "地图坐标系下的位姿。",
               outputs=(F("pose", "pose", description=_POSE), F("covariance", "floats", required=False), F("map_id", "str", required=False))),
    Capability("gripper.state", 1, "state", "none", "夹爪状态", "当前夹爪开度与是否夹持物体（v1.1 新增，用于一致性测试验证夹爪停止与动作）。",
               outputs=(F("opening", "float", "ratio"), F("grasped", "bool", required=False), F("force", "float", "N", required=False))),
    Capability("arm.ee_state", 1, "state", "none", "末端位姿状态", "末端执行器当前位姿（v1.1 新增，用于一致性测试验证 arm.ee_pose）。",
               outputs=(F("pose", "pose", description=_POSE), F("frame", "str", required=False))),
    Capability("joint.state", 1, "state", "none", "关节状态", "所有关节位置、速度、力矩。",
               outputs=(F("names", "object", description="关节名列表"), F("position", "floats", "rad"),
                        F("velocity", "floats", "rad/s", required=False), F("effort", "floats", "N·m", required=False)),
               params=(F("dof", "int", positive=True),)),
    # ---- motion commands -------------------------------------------------
    Capability("base.velocity", 1, "command", "motion", "底盘速度", "设置底盘速度指令；超过 timeout_s 未刷新时运行时自动停止。",
               inputs=(F("vx", "float", "m/s"), F("vy", "float", "m/s", required=False), F("wz", "float", "rad/s"),
                       F("timeout_s", "float", "s", required=False)),
               outputs=(F("accepted", "bool"),),
               params=(F("max_vx", "float", "m/s", positive=True), F("max_vy", "float", "m/s", nonnegative=True), F("max_wz", "float", "rad/s", positive=True),
                       F("holonomic", "bool"))),
    Capability("base.navigate_to", 1, "action", "motion", "导航到目标", "在地图中自主导航到目标位姿。",
               inputs=(F("goal", "pose", description=_POSE), F("frame", "str", required=False), F("tolerance", "float", "m", required=False)),
               outputs=(F("reached", "bool"), F("final_pose", "pose", required=False)),
               params=(F("planner", "str", required=False),)),
    Capability("legged.posture", 1, "command", "motion", "足式姿态", "站立、趴下、坐下等预设姿态。",
               inputs=(F("posture", "enum", choices=("stand", "lie", "sit", "recover")),),
               outputs=(F("accepted", "bool"),),
               params=(F("supported", "object", description="支持的姿态列表"),)),
    Capability("arm.joint_position", 1, "action", "motion", "机械臂关节位置", "移动到目标关节角。",
               inputs=(F("position", "floats", "rad"), F("max_velocity", "float", "rad/s", required=False)),
               outputs=(F("reached", "bool"), F("position", "floats", "rad")),
               params=(F("dof", "int", positive=True), F("lower", "floats", "rad"), F("upper", "floats", "rad"), F("max_velocity", "float", "rad/s", positive=True),
                       F("joint_names", "object", required=False,
                         description="可选（v1.1）：position 各分量对应的关节名（字符串列表，长度 = dof，互不相同），"
                                     "须与 joint.state 的 names 使用同一套名称；声明后按名称对应，"
                                     "未声明时不得假定 joint.state 的顺序就是指令顺序"))),
    Capability("arm.ee_pose", 1, "action", "motion", "末端位姿", "笛卡尔空间末端执行器目标位姿。",
               inputs=(F("pose", "pose", description=_POSE), F("frame", "str", required=False), F("max_velocity", "float", "m/s", required=False)),
               outputs=(F("reached", "bool"), F("pose", "pose")),
               params=(F("frame", "str"), F("reach", "float", "m", positive=True), F("payload", "float", "kg"))),
    Capability("gripper.position", 1, "action", "motion", "夹爪", "设置夹爪开度。0=闭合，1=全开。",
               inputs=(F("opening", "float", "ratio"), F("max_force", "float", "N", required=False)),
               outputs=(F("opening", "float", "ratio"), F("grasped", "bool", required=False)),
               params=(F("max_width", "float", "m", positive=True), F("max_force", "float", "N", positive=True))),
    # ---- policy / model interface -----------------------------------------
    Capability("policy.action_chunk", 1, "action", "motion", "策略动作块",
               "执行由端到端策略（如 VLA）输出的一段动作序列；动作空间由 action_space 参数声明，运行时逐步限幅。",
               inputs=(F("actions", "object", description="二维数组 [T][D]"), F("dt", "float", "s")),
               outputs=(F("executed", "int", description="实际执行步数"),),
               params=(F("action_space", "str", description="如 joint_position / ee_delta / base_velocity"),
                       F("dim", "int", positive=True), F("rate_hz", "float", "Hz", positive=True))),
    # ---- system ----------------------------------------------------------
    Capability("model.install", 1, "action", "system", "端侧模型安装", "下载并校验模型包，安装到端侧运行时，不触发任何动作。",
               inputs=(F("package_ref", "str"), F("sha256", "str"), F("runtime", "str"),
                       F("model_id", "str", required=False, description="目录中的模型 ID（可选，便于展示与查找）"),
                       F("name", "str", required=False)),
               outputs=(F("installed", "bool"), F("model_id", "str")),
               params=(F("runtimes", "object", description="支持的推理运行时，如 onnxruntime / tensorrt"),
                       F("accelerator", "str", required=False))),
    Capability("audio.speak", 1, "command", "none", "语音播报", "文本转语音播放。",
               inputs=(F("text", "str"), F("language", "str", required=False)),
               outputs=(F("accepted", "bool"),)),
]}

MANDATORY = tuple(c.id for c in CAPABILITIES.values() if c.mandatory)

# Mapping from legacy free-text v1 capability strings to standard IDs.
LEGACY_ALIASES = {
    "stop": "safety.stop", "estop": "safety.stop", "status": "system.health", "health": "system.health",
    "move_base": "base.velocity", "cmd_vel": "base.velocity", "navigate": "base.navigate_to", "navigation": "base.navigate_to",
    "camera": "sensor.camera.rgb", "rgb": "sensor.camera.rgb", "depth": "sensor.camera.depth", "lidar": "sensor.lidar",
    "imu": "sensor.imu", "battery": "sensor.battery", "odom": "base.odometry", "odometry": "base.odometry",
    "joints": "joint.state", "arm": "arm.joint_position", "gripper": "gripper.position", "posture": "legged.posture",
    "speak": "audio.speak", "tts": "audio.speak",
}


def get(cap_id: str) -> Capability:
    try:
        return CAPABILITIES[cap_id]
    except KeyError:
        raise ValueError(f"未知能力：{cap_id}（不在 NeuxMind 能力标准 v{SPEC_VERSION} 中）") from None


def normalize(cap: str) -> str:
    """Return the standard ID for a capability string (standard or legacy alias)."""
    if cap in CAPABILITIES:
        return cap
    alias = LEGACY_ALIASES.get(cap.strip().lower())
    if alias:
        return alias
    if cap.startswith("x."):   # vendor extension namespace, e.g. x.unitree.dance
        return cap
    raise ValueError(f"未知能力：{cap}；请使用标准能力 ID 或 x.<vendor>.<name> 扩展命名")


def _check_value(f: Field, value: Any, where: str) -> None:
    bad = ValueError(f"{where}.{f.name} 需要 {f.type}{(' ' + f.unit) if f.unit else ''}")
    num = (int, float)
    if f.type == "float":
        if isinstance(value, bool) or not isinstance(value, num) or not math.isfinite(value):
            raise bad
    elif f.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad
    elif f.type == "bool":
        if not isinstance(value, bool):
            raise bad
    elif f.type in ("str", "blob_ref"):
        if not isinstance(value, str):
            raise bad
    elif f.type == "enum":
        if value not in f.choices:
            raise ValueError(f"{where}.{f.name} 须为 {list(f.choices)} 之一")
    elif f.type in ("vector", "floats", "pose"):
        n = 7 if f.type == "pose" else f.length
        if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, num) or not math.isfinite(v) for v in value):
            raise bad
        if n is not None and len(value) != n:
            raise ValueError(f"{where}.{f.name} 需要 {n} 个数值")
    if f.positive and not (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0):
        raise ValueError(f"{where}.{f.name} 须为大于 0 的有限数值")
    if f.nonnegative and not (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0):
        raise ValueError(f"{where}.{f.name} 须为不小于 0 的有限数值")


def check_field(f: Field, value: Any, where: str = "value") -> None:
    """Public field validator (used by runtime/SDK for skill parameters)."""
    _check_value(f, value, where)


def validate_payload(cap_id: str, payload: dict, direction: str = "inputs") -> dict:
    """Validate a payload against a capability's inputs or outputs. Unknown keys are rejected."""
    if cap_id.startswith("x."):
        if not isinstance(payload, dict):
            raise ValueError("扩展能力参数须为对象")
        return payload
    cap = get(cap_id)
    fields = cap.inputs if direction == "inputs" else cap.outputs
    if not isinstance(payload, dict):
        raise ValueError(f"{cap_id} 的{direction}须为对象")
    names = {f.name for f in fields}
    extra = set(payload) - names
    if extra:
        raise ValueError(f"{cap_id} 不接受字段：{sorted(extra)}")
    for f in fields:
        if f.name not in payload:
            if f.required:
                raise ValueError(f"{cap_id} 缺少字段 {f.name}")
            continue
        _check_value(f, payload[f.name], cap_id)
    return payload


def registry() -> dict:
    """JSON-serialisable registry, used by desktop, cloud and docs."""
    return {"schema": "neuxmind.capabilities.v1", "spec_version": SPEC_VERSION,
            "capabilities": [c.to_dict() for c in CAPABILITIES.values()]}
