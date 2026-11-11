"""Adapter scaffold (INTERFACES_V1 §6.2): capability mapping → installable adapter package.

Input (``neuxmind.adapter_scaffold.v1``, e.g. produced by the desktop
"本体适配" workflow from a body profile)::

    {"name": "acme_rover", "vendor": "Acme", "model": "R1", "family": "mobile",
     "instance_id": "acme-r1-01", "simulated": false,
     "safety": {"hardware_estop": true},
     "capabilities": [
        {"id": "base.velocity", "params": {"max_vx": 0.8, "max_vy": 0.0, "max_wz": 1.0, "holonomic": false},
         "implementation": "ros2:/cmd_vel", "evidence_id": "ev-12"},
        {"id": "safety.stop", "params": {"hardware_estop": true}, "implementation": "ros2"},
        {"id": "system.health", "implementation": "ros2"},
        {"id": "sensor.camera.rgb", "params": {...}, "implementation": "sdk:acme_cam"}]}

Output (``generate``)::

    <out>/pyproject.toml            installable (pip install -e <out>)
    <out>/README.md                 capability table, TODOs, how to test and serve
    <out>/<name>/__init__.py
    <out>/<name>/adapter.py         class <Name>Adapter(RobotAdapter)
    <out>/<name>/adapter.json       identity, limits, ROS 2 names, mapping + evidence
    <out>/tests/test_adapter.py     conformance static level (+ live static when ROS 2 is there)

Rules:

* Implementation hints: ``ros2[:<topic|action>]``, ``sdk[:<name>]``,
  ``serial[:<device>]``, ``http[:<url>]``, ``todo``. ``ros2`` hints for the
  capabilities in :data:`ROS2_TEMPLATES` get a **working** implementation built
  on :class:`neuxmind_runtime.adapters.ros2.Ros2Bridge`; everything else gets a
  method that raises ``NotImplementedError`` and is **not** declared in the
  manifest (``IMPLEMENTED`` in ``adapter.py`` is the single switch).
* Parameters are validated against the spec; required ones must be given
  (limits come from the body's datasheet or profile, never guessed).
* ``safety.stop`` and ``system.health`` are always part of the package. If
  either is not implemented, ``manifest()`` raises ``NotImplementedError``: the
  runtime refuses to load the adapter and the generated static test fails
  until someone implements them.
* The generated adapter can be loaded with
  ``neuxmind-runtime serve --adapter <name>.adapter:<Class>``.
"""
from __future__ import annotations

import json
import keyword
import re
import sys
import time
from pathlib import Path
from typing import Any

from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

from . import __version__
from .safety import validate_safety_params

SCHEMA = "neuxmind.adapter_scaffold.v1"
HINT_KINDS = ("ros2", "sdk", "serial", "http", "todo")
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
# Q4-09: every external string is whitelisted *and* embedded with repr()/json, never raw.
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")                       # evidence_id, instance_id
_XCAP_RE = re.compile(r"^x\.[a-z0-9_]+(\.[a-z0-9_]+)+$")               # vendor extension ids
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")   # PEP 440 subset
_TARGET_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,~?&#-]{1,200}$")          # hint targets (no quotes/space/backslash)
_ROS_NAME_RE = re.compile(r"^/?[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*$")
_NODE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_FRAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_/]{0,62}$")
_TEXT_FORBIDDEN = set('"\'`\\<>|$[]')        # R4-07: no Markdown link syntax in the generated README
# R4-06: a package named like a standard-library (or common) module shadows it on PYTHONPATH.
_SHADOWED = frozenset(getattr(sys, "stdlib_module_names", ())) | frozenset({
    "json", "yaml", "numpy", "rclpy", "requests", "setuptools", "pip", "pytest", "typing_extensions",
    "std_msgs", "sensor_msgs", "geometry_msgs", "nav_msgs", "tests", "test"})


def _safe_text(value: Any, label: str, max_len: int = 100) -> str:
    """Single-line display text (vendor, model, name): printable, no quotes/backslash/backtick/<>|$."""
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ScaffoldError(f"{label} 需要 1–{max_len} 个字符")
    value = value.strip()
    bad = sorted({ch for ch in value if not ch.isprintable() or ch in _TEXT_FORBIDDEN})
    if bad:
        raise ScaffoldError(f"{label} 含不允许的字符 {bad!r}（只允许单行文本，不含引号、反斜杠、反引号、<>|$[]）")
    return value
_CLASS = re.compile(r"^[A-Z][A-Za-z0-9]{1,79}$")

# capability -> (role, message type, default ROS 2 name)
ROS2_TEMPLATES: dict[str, tuple[str, str | None, str | None]] = {
    "base.velocity": ("publish", "geometry_msgs.msg:Twist", "/cmd_vel"),
    "base.odometry": ("subscribe", "nav_msgs.msg:Odometry", "/odom"),
    "sensor.battery": ("subscribe", "sensor_msgs.msg:BatteryState", "/battery_state"),
    "sensor.imu": ("subscribe", "sensor_msgs.msg:Imu", "/imu"),
    "joint.state": ("subscribe", "sensor_msgs.msg:JointState", "/joint_states"),
    "localization.pose": ("subscribe", "geometry_msgs.msg:PoseWithCovarianceStamped", "/amcl_pose"),
    "base.navigate_to": ("action", "nav2_msgs.action:NavigateToPose", "navigate_to_pose"),
    "safety.stop": ("stop", None, None),
    "system.health": ("health", None, None),
}
_CONVERTERS = {
    "base.odometry": "odometry_outputs", "sensor.battery": "battery_outputs", "sensor.imu": "imu_outputs",
    "joint.state": "joint_state_outputs", "localization.pose": "pose_with_covariance_outputs",
}
# ROS 2 interfaces suggested in the TODO stub of capabilities without a template.
_ROS2_SUGGESTIONS = {
    "sensor.camera.rgb": "sensor_msgs/Image（或 CompressedImage）+ 帧缓存，输出 blob_ref",
    "sensor.camera.depth": "sensor_msgs/Image（16UC1/32FC1）",
    "sensor.lidar": "sensor_msgs/LaserScan 或 PointCloud2",
    "arm.joint_position": "control_msgs/action/FollowJointTrajectory",
    "arm.ee_pose": "MoveIt 2 move_group 动作",
    "gripper.position": "control_msgs/action/GripperCommand",
    "legged.posture": "厂商服务（如 /sport/request）",
    "audio.speak": "厂商 TTS 话题/服务",
}


class ScaffoldError(ValueError):
    pass


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part) + "Adapter"


def parse_hint(hint: Any) -> tuple[str, str | None]:
    if hint is None:
        return "todo", None
    if not isinstance(hint, str) or not hint.strip():
        raise ScaffoldError(f"实现提示须为字符串，收到 {hint!r}")
    kind, _, target = hint.strip().partition(":")
    kind = kind.lower()
    if kind not in HINT_KINDS:
        raise ScaffoldError(f"未知实现提示 {hint!r}；可用 {', '.join(k + '[:目标]' for k in HINT_KINDS)}")
    target = target.strip() or None
    if target is not None and not _TARGET_RE.match(target):
        raise ScaffoldError(f"实现提示目标无效：{target!r}（只允许 [A-Za-z0-9_./:@%+=,~?&#-]，不含引号、空白、反斜杠）")
    if kind == "ros2" and target is not None and not _ROS_NAME_RE.match(target):
        raise ScaffoldError(f"ROS 2 名称无效：{target!r}")
    return kind, target


def normalize_spec(spec: dict) -> dict:
    """Validate the scaffold input and return the plan written to ``adapter.json``."""
    if not isinstance(spec, dict):
        raise ScaffoldError("脚手架输入须为 JSON 对象")
    name = spec.get("name")
    if not isinstance(name, str) or not _NAME.match(name) or keyword.iskeyword(name) or name.startswith("neuxmind"):
        raise ScaffoldError("name 须为小写 Python 包名（字母开头，2–50 位字母/数字/下划线，不以 neuxmind 开头）")
    if name in _SHADOWED:
        raise ScaffoldError(f"name {name!r} 与标准库或常用包同名，会在 PYTHONPATH 中遮蔽它；请换一个包名")
    class_name = spec.get("class_name") or _camel(name)
    if not isinstance(class_name, str) or not _CLASS.match(class_name):
        raise ScaffoldError("class_name 须为 CamelCase 标识符")
    family = spec.get("family")
    if family not in spec_manifest.FAMILIES:
        raise ScaffoldError(f"family 须为 {spec_manifest.FAMILIES} 之一")
    texts = {key: _safe_text(spec.get(key), key) for key in ("vendor", "model")}
    display = _safe_text(spec["display_name"], "display_name") if spec.get("display_name") is not None \
        else f"{texts['vendor']} {texts['model']}"
    version = spec.get("adapter_version") or "0.1.0"
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ScaffoldError("adapter_version 须为版本号，如 0.1.0（PEP 440 子集）")
    instance_id = spec.get("instance_id") or name.replace("_", "-") + "-01"
    if not isinstance(instance_id, str) or not _ID_RE.match(instance_id):
        raise ScaffoldError("instance_id 只能包含 [A-Za-z0-9_.:-]，1–100 个字符")
    simulated = spec.get("simulated", False)
    if not isinstance(simulated, bool):
        raise ScaffoldError("simulated 须为 true/false")
    caps_in = spec.get("capabilities")
    if not isinstance(caps_in, list) or not caps_in:
        raise ScaffoldError("capabilities 须为非空列表")
    errors: list[str] = []
    caps: dict[str, dict] = {}
    for item in caps_in:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append(f"能力项无效：{item!r}")
            continue
        try:
            cid = spec_caps.normalize(item["id"])
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if cid.startswith("x.") and not _XCAP_RE.match(cid):
            errors.append(f"厂商扩展能力 ID 无效：{cid!r}（须为 x.<厂商>.<名称>，小写字母、数字、下划线）")
            continue
        if cid in caps:
            errors.append(f"能力重复：{cid}")
            continue
        try:
            kind, target = parse_hint(item.get("implementation"))
        except ScaffoldError as exc:
            errors.append(f"{cid}: {exc}")
            continue
        params = item.get("params") or {}
        if not isinstance(params, dict):
            errors.append(f"{cid}.params 须为对象")
            continue
        if not cid.startswith("x."):
            cap = spec_caps.get(cid)
            known = {f.name: f for f in cap.params}
            unknown = sorted(set(params) - set(known))
            if unknown:
                errors.append(f"{cid} 未定义参数 {unknown}")
            missing = [f.name for f in cap.params if f.required and f.name not in params]
            if missing:
                errors.append(f"{cid} 缺少必填参数 {missing}（来自本体规格/档案，脚手架不猜测限值）")
            for pname, value in params.items():
                if pname in known:
                    try:
                        spec_caps.check_field(known[pname], value, f"{cid}.params")
                    except ValueError as exc:
                        errors.append(str(exc))
        evidence = item.get("evidence_id")
        if evidence is not None and (not isinstance(evidence, str) or not _ID_RE.match(evidence)):
            errors.append(f"{cid}.evidence_id 只能包含 [A-Za-z0-9_.:-]，1–100 个字符")
            evidence = None
        template = kind == "ros2" and cid in ROS2_TEMPLATES
        entry = {"id": cid, "params": params, "implementation": f"{kind}:{target}" if target else kind,
                 "scaffold_status": "implemented" if template else "todo"}
        if evidence:
            entry["evidence_id"] = evidence
        if isinstance(item.get("notes"), str):
            entry["notes"] = "".join(ch for ch in item["notes"][:1000] if ch.isprintable())   # JSON only
        if template and ROS2_TEMPLATES[cid][2] is not None:
            entry["ros2_name"] = target or ROS2_TEMPLATES[cid][2]
        caps[cid] = entry
    for cid in spec_caps.MANDATORY:
        if cid not in caps:
            params = {"hardware_estop": False} if cid == "safety.stop" else {}
            caps[cid] = {"id": cid, "params": params, "implementation": "todo", "scaffold_status": "todo",
                         "notes": "必备能力：输入未提供，已加入待实现"}
    safety = spec.get("safety") or {}
    if not isinstance(safety, dict):
        errors.append("safety 须为对象")
        safety = {}
    if "hardware_estop" not in safety:
        safety = {**safety, "hardware_estop": bool(caps["safety.stop"]["params"].get("hardware_estop", False))}
    if "base.velocity" in caps and "max_linear_speed" not in safety and caps["base.velocity"]["params"].get("max_vx"):
        safety["max_linear_speed"] = caps["base.velocity"]["params"]["max_vx"]
    if not errors:
        try:
            validate_safety_params({c: dict(e["params"]) for c, e in caps.items()}, safety)
        except ValueError as exc:
            errors.append(str(exc))
        if "arm.joint_position" in caps:
            p = caps["arm.joint_position"]["params"]
            if len(p.get("lower", [])) != p.get("dof") or len(p.get("upper", [])) != p.get("dof"):
                errors.append("arm.joint_position lower/upper 长度须等于 dof")
    ros_caps = [c for c, e in caps.items() if e["scaffold_status"] == "implemented"]
    foreign_motion: list[str] = []
    if not errors and caps["safety.stop"]["scaffold_status"] == "implemented":
        # Motion capabilities the generated stop() cannot halt yet (still TODO): listed in the code.
        foreign_motion = [c for c, e in caps.items() if e["scaffold_status"] != "implemented"
                          and (c.startswith("x.") or spec_caps.get(c).safety == "motion")]
        caps["safety.stop"].setdefault("notes", "停止 = 零速度 Twist + 取消全部 Nav2 目标；不是硬件急停")
    if errors:
        raise ScaffoldError("脚手架输入有误：\n- " + "\n- ".join(errors))
    plan = {
        "schema": SCHEMA, "generated_by": f"neuxmind_runtime.scaffold {__version__}",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "module": name, "class_name": class_name,
        "instance_id": instance_id, "name": display,
        "vendor": texts["vendor"], "model": texts["model"], "family": family,
        "adapter_version": version, "simulated": simulated,
        "safety": safety,
        "capabilities": [caps[c] for c in _ordered(caps)],
        "stop_does_not_cover": foreign_motion,
    }
    if ros_caps:
        ros = spec.get("ros2") or {}
        if not isinstance(ros, dict):
            raise ScaffoldError("ros2 须为对象")
        node = ros.get("node_name") or f"neuxmind_{name}"
        frame = ros.get("map_frame") or "map"
        stale = ros.get("stale_after_s", 1.0)
        if not isinstance(node, str) or not _NODE_RE.match(node):
            raise ScaffoldError("ros2.node_name 须为 ROS 2 节点名（字母/下划线开头，字母数字下划线）")
        if not isinstance(frame, str) or not _FRAME_RE.match(frame):
            raise ScaffoldError("ros2.map_frame 须为坐标系名")
        if isinstance(stale, bool) or not isinstance(stale, (int, float)) or not 0.05 <= stale <= 10.0:
            raise ScaffoldError("ros2.stale_after_s 须在 0.05–10 s 之间（Q4-21）")
        plan["ros2"] = {"node_name": node, "stale_after_s": float(stale), "map_frame": frame}
    return plan


def _ordered(caps: dict) -> list[str]:
    order = list(spec_caps.CAPABILITIES)
    return sorted(caps, key=lambda c: (order.index(c) if c in order else len(order), c))


# ======================================================================= code generation
def _method_name(prefix: str, cid: str) -> str:
    return prefix + cid.replace(".", "_")


def _todo_body(entry: dict, what: str) -> str:
    cid = entry["id"]
    hint = entry["implementation"]
    lines = [f"        # TODO 实现提示 {hint!r}"
             + (f"，证据 {entry['evidence_id']!r}" if entry.get("evidence_id") else "") + "（详见 adapter.json）"]
    if hint.startswith("ros2") and cid in _ROS2_SUGGESTIONS:
        lines.append(f"        # ROS 2 模板未覆盖该能力；建议接口：{_ROS2_SUGGESTIONS[cid]}")
    lines.append(f"        # 实现后：把 {cid!r} 加入 IMPLEMENTED，并跑 tests/test_adapter.py 与 conformance --level dry。")
    lines.append(f"        raise NotImplementedError({(cid + ' 尚未实现（' + what + '；实现提示：' + hint + '）')!r})")
    return "\n".join(lines)


def _render_adapter(plan: dict) -> str:
    caps = {c["id"]: c for c in plan["capabilities"]}
    implemented = [c for c, e in caps.items() if e["scaffold_status"] == "implemented"]
    todo = [c for c, e in caps.items() if e["scaffold_status"] != "implemented"]
    ros = "ros2" in plan
    cls = plan["class_name"]
    subscribed = [c for c in implemented if ROS2_TEMPLATES.get(c, ("",))[0] == "subscribe"]
    has_vel = "base.velocity" in implemented
    has_nav = "base.navigate_to" in implemented
    out: list[str] = []
    w = out.append
    w(f'"""NeuxMind adapter -- generated by neuxmind_runtime.scaffold {__version__}. DRAFT.')
    w("")
    w("Identity (vendor, model, name), limits, ROS 2 names and evidence: see adapter.json.")
    w(f"Family: {plan['family']}")
    w(f"Implemented ({len(implemented)}): {', '.join(map(repr, implemented)) or '-'}")
    w(f"TODO ({len(todo)}): {', '.join(map(repr, todo)) or '-'}")
    w("")
    w("A TODO capability raises NotImplementedError and is NOT declared in manifest(); IMPLEMENTED")
    w("below is the single switch. After implementing one: add it to IMPLEMENTED, run")
    w("tests/test_adapter.py (conformance static) and")
    w(f"``neuxmind-runtime conformance --adapter {plan['module']}.adapter:{cls} --level dry``.")
    w("Before any deployment: the motion level on the real robot, with on-site confirmation.")
    if ros:
        w("ROS 2 parts use neuxmind_runtime.adapters.ros2.Ros2Bridge; tested only against the rclpy test")
        w("double, never on a real ROS 2 graph: check QoS, frames and the controller's zero-Twist handling.")
    w('"""')
    w("from __future__ import annotations")
    w("")
    w("import copy")
    w("import json")
    w("import os")
    w("from pathlib import Path")
    w("")
    w("from neuxmind_runtime.clock import RealClock")
    w("from neuxmind_runtime.hal import ActionHandle, RobotAdapter")
    if ros:
        names = ["Ros2Bridge"] + sorted({_CONVERTERS[c] for c in subscribed})
        if has_vel:
            names.append("publish_twist")
        if has_nav:
            names.append("send_nav_goal")
        w(f"from neuxmind_runtime.adapters.ros2 import {', '.join(names)}")
    w("")
    w('CONFIG_PATH = Path(__file__).with_name("adapter.json")')
    env = "NEUXMIND_" + plan["module"].upper() + "_CONFIG"
    w(f'CONFIG_ENV = "{env}"     # optional JSON file merged over adapter.json (site limits, topic names)')
    w("")
    w("# Capabilities with a working implementation -- the only ones declared in manifest().")
    w("IMPLEMENTED = (" + "".join(f"\n    {c!r}," for c in implemented) + ("\n)" if implemented else ")"))
    w("MANDATORY = ('safety.stop', 'system.health')")
    arm = caps.get("arm.joint_position")
    if arm is not None:
        dof = arm["params"].get("dof")
        declared = arm["params"].get("joint_names")
        w("")
        w("# arm.joint_position.params.joint_names (capability spec v1.1, R4c-03): the joint names in")
        w("# COMMAND order -- the order of `position` in arm.joint_position and of every row of a")
        w("# joint_position policy.action_chunk. Take them from the controller configuration: the")
        w("# `joints:` list of the ros2_control controller (e.g. joint_trajectory_controller in")
        w("# controllers.yaml), in that order. Do NOT copy the order of /joint_states: it is often")
        w("# alphabetical or URDF order, and a wrong order drives the wrong joints (only the motion-level")
        w("# conformance envelope catches it, after ~0.02 rad). Names must match joint.state `names`.")
        if declared:
            w("# Already declared in adapter.json (arm.joint_position.params.joint_names): check the order there.")
            w("ARM_JOINT_NAMES = None")
        else:
            w(f"# Example (dof={dof}): ARM_JOINT_NAMES = ['shoulder_pan_joint', 'shoulder_lift_joint', ...]")
            w("# While None: the runtime refuses joint_position action chunks (400 joint_names_required) and")
            w("# the conformance suite marks every arm case `blocked`.")
            w("ARM_JOINT_NAMES = None   # TODO(joint_names): fill in from ros2_control `joints:`")
    w("")
    w("")
    w("def load_config() -> dict:")
    w('    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))')
    w("    path = os.environ.get(CONFIG_ENV)")
    w("    if path:")
    w('        with open(path, encoding="utf-8") as fh:')
    w("            override = json.load(fh)")
    w("        for key, value in override.items():")
    w("            if isinstance(value, dict) and isinstance(config.get(key), dict):")
    w("                config[key].update(value)")
    w("            else:")
    w("                config[key] = value")
    w("    return config")
    w("")
    w("")
    w("def build_manifest(config: dict, simulated_hint: bool = False) -> dict:")
    w('    """robot.adapter.v2 with only the IMPLEMENTED capabilities (no hardware access)."""')
    w("    missing = [c for c in MANDATORY if c not in IMPLEMENTED]")
    w("    if missing:")
    w('        raise NotImplementedError(f"必备能力 {missing} 尚未实现：运行时不能加载该适配器；"')
    w('                                  "请实现后加入 IMPLEMENTED（见 README）")')
    w('    caps = [{"id": c["id"], "major": 1, "params": copy.deepcopy(c.get("params") or {})}')
    w('            for c in config["capabilities"] if c["id"] in IMPLEMENTED]')
    if arm is not None:
        w("    for c in caps:")
        w('        if c["id"] == "arm.joint_position" and ARM_JOINT_NAMES is not None:')
        w('            c["params"]["joint_names"] = list(ARM_JOINT_NAMES)')
    w("    return {")
    w('        "schema": "robot.adapter.v2", "spec_version": "1.1",')
    w('        "instance_id": config["instance_id"], "name": config["name"], "vendor": config["vendor"],')
    w('        "model": config["model"], "family": config["family"], "adapter_version": config["adapter_version"],')
    w('        "simulated": bool(config.get("simulated") is True or simulated_hint),')
    w('        "capabilities": caps, "model_runtimes": [],')
    w('        "safety": copy.deepcopy(config.get("safety") or {}),')
    w("    }")
    w("")
    w("")
    w(f"class {cls}(RobotAdapter):")
    w('    """Generated adapter; identity in adapter.json. See the module docstring for what is implemented."""')
    w("")
    w("    def __init__(self, config: dict | None = None):")
    w("        self.clock = RealClock()")
    w("        self.config = config or load_config()")
    w("        build_manifest(self.config)          # fail fast (e.g. a mandatory capability is still TODO)")
    if ros:
        w('        ros = self.config["ros2"]')
        w('        names = {c["id"]: c.get("ros2_name") for c in self.config["capabilities"]}')
        w('        self._ros = Ros2Bridge(ros["node_name"], ros.get("stale_after_s", 1.0))')
        w("        try:")
        if has_vel:
            w('            self._cmd_vel, self._Twist = self._ros.publisher("geometry_msgs.msg:Twist", names["base.velocity"])')
        for c in subscribed:
            w(f'            self._ros.subscribe({c!r}, {ROS2_TEMPLATES[c][1]!r}, names[{c!r}])')
        if has_nav:
            w("            self._nav, self._NavigateToPose, self._cancel_all, self._CancelGoal = \\")
            w('                self._ros.nav2_client(names["base.navigate_to"])')
            w("            self._nav_handles: set = set()")
        w("        except BaseException:")
        w("            self._ros.close()")
        w("            raise")
        w("        simulated = self._ros.use_sim_time")
    else:
        w("        # TODO: open the vendor connection here (SDK / serial / HTTP).")
        w("        simulated = False")
    w("        self._manifest = build_manifest(self.config, simulated)")
    w("")
    w("    @classmethod")
    w("    def offline_manifest(cls, config: dict | None = None) -> dict:")
    w('        """The manifest without touching hardware (static conformance in tests/)."""')
    w("        return build_manifest(config or load_config())")
    w("")
    w("    def manifest(self) -> dict:")
    w("        return copy.deepcopy(self._manifest)")
    w("")
    w("    def _handler(self, prefix: str, cap_id: str):")
    w("        if cap_id not in IMPLEMENTED:")
    w('            raise ValueError(f"{cap_id} 未在清单中声明")')
    w('        return getattr(self, prefix + cap_id.replace(".", "_"))')
    w("")
    w("    def read(self, cap_id: str) -> dict:")
    w('        return self._handler("_read_", cap_id)()')
    w("")
    w("    def invoke(self, cap_id: str, args: dict) -> dict:")
    w('        if cap_id == "safety.stop":')
    w("            self.stop()")
    w('            return {"stopped": True}')
    w('        return self._handler("_invoke_", cap_id)(args)')
    w("")
    w("    def start_action(self, cap_id: str, args: dict) -> ActionHandle:")
    w('        return self._handler("_start_", cap_id)(args)')
    w("")
    # stop
    w("    def stop(self) -> None:")
    w('        """Halt all motion. Fast, idempotent, thread-safe, never waits on a lock a command holds."""')
    if caps["safety.stop"]["scaffold_status"] == "implemented":
        w("        errors = []")
        if has_vel:
            w("        try:")
            w('            publish_twist(self._cmd_vel, self._Twist, {"vx": 0.0, "vy": 0.0, "wz": 0.0})')
            w("        except Exception as exc:")
            w("            errors.append(exc)")
        if has_nav:
            w("        for handle in list(self._nav_handles):")
            w("            try:")
            w("                handle.cancel()")
            w("            except Exception as exc:")
            w("                errors.append(exc)")
            w("        if self._cancel_all is not None:")
            w("            try:")
            w("                self._cancel_all.call_async(self._CancelGoal.Request())   # Nav2 cancel-all goals")
            w("            except Exception as exc:")
            w("                errors.append(exc)")
        if not (has_vel or has_nav):
            w("        # No ROS 2 motion interface is mapped: nothing this adapter commands can move.")
        for c in plan.get("stop_does_not_cover", []):
            w(f"        # TODO: {c!r} is not implemented yet; when it is, stop it here as well.")
        w("        # TODO(field): this is NOT a hardware e-stop; wire the vendor e-stop / brake service if one exists.")
        w("        if errors:")
        w('            raise RuntimeError(f"停止部分失败：{errors}")')
    else:
        w(_todo_body(caps["safety.stop"], "停止必须先实现：零速度 + 取消动作 + 厂商急停"))
    w("")
    for cid in _ordered(caps):
        entry = caps[cid]
        if cid == "safety.stop" or cid.startswith("x."):
            continue
        spec = spec_caps.get(cid)
        if spec.kind in ("state", "stream"):
            sig, what = f"    def {_method_name('_read_', cid)}(self) -> dict:", "读取"
        elif spec.kind == "command":
            sig, what = f"    def {_method_name('_invoke_', cid)}(self, args: dict) -> dict:", "命令"
        else:
            sig, what = f"    def {_method_name('_start_', cid)}(self, args: dict) -> ActionHandle:", "动作"
        w(sig)
        outputs = ", ".join(f"{f.name}{'' if f.required else '?'}" for f in spec.outputs)
        w(f'        """{cid} ({spec.title}) -> {outputs}"""')
        if entry["scaffold_status"] != "implemented":
            w(_todo_body(entry, what))
        elif cid == "system.health":
            keys = ", ".join(repr(c) for c in subscribed)
            w(f"        errors = self._ros.freshness_errors(({keys}{',' if len(subscribed) == 1 else ''}))")
            w('        return {"ok": not errors, "mode": "ros2", "errors": errors}')
        elif cid in _CONVERTERS:
            w(f"        return {_CONVERTERS[cid]}(self._ros.fresh({cid!r}))")
        elif cid == "base.velocity":
            w("        publish_twist(self._cmd_vel, self._Twist, args)     # already limited by the safety supervisor")
            w('        return {"accepted": True}')
        elif cid == "base.navigate_to":
            w('        handle = send_nav_goal(self._ros, self._nav, self._NavigateToPose, args,')
            w('                               self.config["ros2"].get("map_frame", "map"), on_done=self._nav_handles.discard)')
            w("        self._nav_handles.add(handle)")
            w("        return handle")
        w("")
    w("    def close(self) -> None:")
    w("        self._ros.close()" if ros else "        pass   # TODO: close the vendor connection")
    return "\n".join(out) + "\n"


def _render_test(plan: dict) -> str:
    module, cls = plan["module"], plan["class_name"]
    return f'''"""Generated by {plan["generated_by"]}: conformance static level for {cls}.

Run: python3 -m unittest discover -s tests   (neuxmind-runtime must be importable)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from neuxmind_runtime import conformance  # noqa: E402
from neuxmind_spec import capabilities as spec_caps  # noqa: E402

from {module} import adapter as mod  # noqa: E402

PREFIX = {{"state": "_read_", "stream": "_read_", "command": "_invoke_", "action": "_start_"}}


def todo_calls():
    """(method, args) of every capability in adapter.json that is not in IMPLEMENTED."""
    calls = []
    for entry in mod.load_config()["capabilities"]:
        cap = entry["id"]
        if cap in mod.IMPLEMENTED or cap.startswith("x."):
            continue
        if cap == "safety.stop":
            calls.append((cap, "stop", ()))
            continue
        kind = spec_caps.get(cap).kind
        calls.append((cap, PREFIX[kind] + cap.replace(".", "_"), () if kind in ("state", "stream") else ({{}},)))
    return calls


class StaticConformanceTests(unittest.TestCase):
    def test_static_level_passes(self):
        report = conformance.check_manifest(mod.{cls}.offline_manifest)
        self.assertTrue(report["static_ok"], conformance.summarize(report))

    def test_todo_capabilities_are_not_declared_and_raise(self):
        declared = {{c["id"] for c in mod.{cls}.offline_manifest()["capabilities"]}}
        self.assertEqual(declared, set(mod.IMPLEMENTED))
        stub = object.__new__(mod.{cls})        # no hardware: TODO methods never touch self
        for cap, method, args in todo_calls():
            self.assertNotIn(cap, declared)
            with self.assertRaises(NotImplementedError, msg=method):
                getattr(stub, method)(*args)

    def test_static_level_on_a_live_instance(self):
        try:
            adapter = mod.{cls}()
        except RuntimeError as exc:            # e.g. ROS 2 not sourced on this machine
            self.skipTest(str(exc))
        report = conformance.run_adapter(adapter, "static")
        self.assertTrue(report["static_ok"], conformance.summarize(report))


if __name__ == "__main__":
    unittest.main()
'''


def _render_pyproject(plan: dict) -> str:
    module = plan["module"]
    return f'''[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "neuxmind-adapter-{module.replace("_", "-")}"
version = {json.dumps(plan["adapter_version"])}
description = {json.dumps("NeuxMind RobotAdapter for " + plan["vendor"] + " " + plan["model"] + " (generated draft)")}
requires-python = ">=3.10"
dependencies = ["neuxmind-runtime"]

[tool.setuptools]
packages = ["{module}"]

[tool.setuptools.package-data]
{module} = ["adapter.json"]
'''


def _render_readme(plan: dict) -> str:
    module, cls = plan["module"], plan["class_name"]
    rows = []
    for c in plan["capabilities"]:
        status = "已实现（模板）" if c["scaffold_status"] == "implemented" else "待实现（NotImplementedError，未声明）"
        rows.append(f"| `{c['id']}` | {status} | `{c['implementation']}` | {c.get('ros2_name', '') or ''} | "
                    f"{c.get('evidence_id', '') or ''} |")
    mandatory_todo = [c["id"] for c in plan["capabilities"]
                      if c["id"] in spec_caps.MANDATORY and c["scaffold_status"] != "implemented"]
    warn = ""
    if mandatory_todo:
        warn = (f"\n> **必备能力 {', '.join(mandatory_todo)} 尚未实现**：`manifest()` 会抛 `NotImplementedError`，"
                "运行时拒绝加载，`tests/test_adapter.py` 的 static 测试失败——这是有意的，先实现它们。\n")
    arm_note = ""
    arm_caps = [c for c in plan["capabilities"] if c["id"] == "arm.joint_position"]
    if arm_caps and not arm_caps[0]["params"].get("joint_names"):
        arm_note = ("\n> **机械臂 `joint_names` 未声明**：在 `adapter.py` 的 `ARM_JOINT_NAMES`（或 `adapter.json` 的 "
                    "`arm.joint_position.params.joint_names`）按**指令顺序**填写关节名——取自 `ros2_control` 控制器配置的 "
                    "`joints:` 列表，**不要照抄 `/joint_states` 的顺序**（常按字母序）。未声明时运行时拒绝 joint_position "
                    "动作块（400 `joint_names_required`），一致性测试的机械臂用例 `blocked`；声明错误要到 motion 级才会被发现"
                    "（dry 不向机械臂发目标）。\n")
    ros_note = ""
    if "ros2" in plan:
        ros_note = ("\nROS 2 部分基于 `neuxmind_runtime.adapters.ros2.Ros2Bridge`，只在 rclpy 替身上测试过："
                    "上真机前确认 QoS、坐标系（Nav2 目标在 `map`，里程计在 `odom`）、底盘控制器对零 Twist 的处理；"
                    "`safety.stop` = 零速度 + 取消 Nav2 目标，**不是硬件急停**。\n")
    return f"""# {plan['name']} 适配器（脚手架生成的初稿）

由 `{plan['generated_by']}` 于 {plan['generated_at']} 生成。厂商/型号：{plan['vendor']} / {plan['model']}，家族 `{plan['family']}`，`simulated: {str(plan['simulated']).lower()}`。
{warn}{arm_note}{ros_note}
## 能力映射

| 能力 | 状态 | 实现提示 | ROS 2 名称 | 证据 |
|---|---|---|---|---|
{chr(10).join(rows)}

`adapter.py` 中的 `IMPLEMENTED` 是唯一开关：只有其中的能力出现在 manifest 里。`adapter.json` 的
`scaffold_status` 只是生成时的快照。限值参数来自本体档案，修改时同时更新 `adapter.json`
（或用环境变量 `NEUXMIND_{module.upper()}_CONFIG` 指向覆盖用的 JSON）。

## 实现一个待实现能力

1. 在 `adapter.py` 中实现对应方法（`_read_*` / `_invoke_*` / `_start_*`；动作返回 `ActionHandle`，`poll()`/`cancel()` 必须快）。
2. 若是运动能力，在 `stop()` 中一并停止它；`stop()` 与零速度不得等待普通指令持有的锁。
3. 把能力 ID 加入 `IMPLEMENTED`。
4. `python3 -m unittest discover -s tests`（一致性 static 级）。
5. 连接本体（或替身/仿真）后：`neuxmind-runtime conformance --adapter {module}.adapter:{cls} --level dry --out report.json`。
6. 部署前在真机上、现场确认安全后：`--level motion --authorize`。一致性报告不等于现场验收证据。

## 安装与运行

```
pip install -e .                       # 依赖 neuxmind-runtime
BRAIN_EDGE_TOKEN=... neuxmind-runtime serve --adapter {module}.adapter:{cls}
```
未安装时可用 `PYTHONPATH=<本目录>`。
"""


def generate(spec: dict, out_dir: str | Path, force: bool = False) -> dict:
    """Write the adapter package; returns a summary (paths, implemented, todo, load string)."""
    plan = normalize_spec(spec)
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()) and not force:
        raise ScaffoldError(f"输出目录 {out} 非空（--force 覆盖）")
    module = plan["module"]
    rel = {
        "pyproject.toml": _render_pyproject(plan),
        "README.md": _render_readme(plan),
        f"{module}/__init__.py": '"""NeuxMind adapter package (generated); identity in adapter.json."""\n',
        f"{module}/adapter.py": _render_adapter(plan),
        f"{module}/adapter.json": json.dumps({k: v for k, v in plan.items() if k not in ("module", "class_name",
                                                                                          "stop_does_not_cover")},
                                             ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        "tests/test_adapter.py": _render_test(plan),
    }
    _validate_generated(rel, plan)          # everything checked in memory before anything is written
    _write_atomically(out, rel)
    files = {out / name: text for name, text in rel.items()}
    caps = plan["capabilities"]
    return {"path": str(out), "module": module, "class_name": plan["class_name"],
            "adapter": f"{module}.adapter:{plan['class_name']}",
            "implemented": [c["id"] for c in caps if c["scaffold_status"] == "implemented"],
            "todo": [c["id"] for c in caps if c["scaffold_status"] != "implemented"],
            "mandatory_todo": [c["id"] for c in caps
                               if c["id"] in spec_caps.MANDATORY and c["scaffold_status"] != "implemented"],
            "files": sorted(str(p.relative_to(out)) for p in files)}


_ALLOWED_IMPORTS = {"__future__", "copy", "json", "os", "pathlib", "neuxmind_runtime.clock",
                    "neuxmind_runtime.hal", "neuxmind_runtime.adapters.ros2"}
_ALLOWED_ASSIGN = {"CONFIG_PATH", "CONFIG_ENV", "IMPLEMENTED", "MANDATORY", "ARM_JOINT_NAMES"}


def _validate_generated(files: dict, plan: dict) -> None:
    """Q4-09: parse every generated file; adapter.py may contain only the expected structure."""
    import ast
    for name, text in files.items():
        if name.endswith(".py"):
            try:
                compile(text, name, "exec")
            except SyntaxError as exc:
                raise ScaffoldError(f"生成的 {name} 无法编译（输入含非法内容？）：{exc}") from None
    json.loads(files[f"{plan['module']}/adapter.json"])
    _check_toml(files["pyproject.toml"])
    tree = ast.parse(files[f"{plan['module']}/adapter.py"])
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.Expr) and index == 0 and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.ImportFrom) and node.module in _ALLOWED_IMPORTS:
            continue
        if isinstance(node, ast.Import) and all(a.name in _ALLOWED_IMPORTS for a in node.names):
            continue
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id in _ALLOWED_ASSIGN for t in node.targets):
            continue
        if isinstance(node, ast.FunctionDef) and node.name in ("load_config", "build_manifest") and not node.decorator_list:
            continue
        if isinstance(node, ast.ClassDef) and node.name == plan["class_name"]:
            for item in node.body:
                if isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                    continue
                if isinstance(item, ast.FunctionDef) and all(
                        isinstance(d, ast.Name) and d.id == "classmethod" for d in item.decorator_list):
                    continue
                raise ScaffoldError(f"生成的 adapter.py 类体含意外结构（第 {item.lineno} 行）")
            continue
        raise ScaffoldError(f"生成的 adapter.py 含意外的顶层语句（第 {getattr(node, 'lineno', '?')} 行）")
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if len(classes) != 1:
        raise ScaffoldError("生成的 adapter.py 必须恰好含一个适配器类")


def _check_toml(text: str) -> None:
    try:
        import tomllib                       # Python 3.11+
        tomllib.loads(text)
        return
    except ImportError:
        pass
    tables = [line for line in text.splitlines() if line.startswith("[")]
    if tables != ["[build-system]", "[project]", "[tool.setuptools]", "[tool.setuptools.package-data]"]:
        raise ScaffoldError(f"生成的 pyproject.toml 表结构异常：{tables}")


def _write_atomically(out: Path, files: dict) -> None:
    """Write into a sibling temp dir, then move into place: a failure leaves nothing half-written."""
    import os
    import shutil
    import tempfile
    out = out.absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{out.name}.scaffold-", dir=str(out.parent)))
    try:
        for name, text in files.items():
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if not out.exists() or not any(out.iterdir()):
            if out.exists():
                out.rmdir()
            os.rename(tmp, out)
            tmp = None
        else:                                   # --force into an existing directory: file by file
            for name in files:
                target = out / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp / name, target)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def spec_from_args(name: str, vendor: str, model: str, family: str, caps: list[str], params: list[str],
                   impls: list[str], **extra) -> dict:
    """Build the scaffold input from CLI flags: ``--cap id``, ``--param cap.name=JSON``, ``--impl cap=hint``."""
    entries: dict[str, dict] = {}
    for raw in caps:
        for cid in (c.strip() for c in raw.split(",")):
            if cid:
                entries.setdefault(cid, {"id": cid, "params": {}})
    for raw in params:
        key, sep, value = raw.partition("=")
        cid, dot, pname = key.rpartition(".")
        if not sep or not dot:
            raise ScaffoldError(f"--param 须为 <能力>.<参数>=<JSON 值>：{raw!r}")
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
        entries.setdefault(cid, {"id": cid, "params": {}})["params"][pname] = parsed
    for raw in impls:
        cid, sep, hint = raw.partition("=")
        if not sep:
            raise ScaffoldError(f"--impl 须为 <能力>=<实现提示>：{raw!r}")
        entries.setdefault(cid, {"id": cid, "params": {}})["implementation"] = hint
    return {"name": name, "vendor": vendor, "model": model, "family": family,
            "capabilities": list(entries.values()), **{k: v for k, v in extra.items() if v is not None}}
