"""Skill templates for ``nx skill new <id> --template ...``.

Each template renders ``skill.json``, ``skill.py``, ``tests/test_skill.py``
and ``README.md``. The generated tests run the skill on the runtime's
simulated bodies via :mod:`neuxmind.skill.testing` and pass as generated.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import NeuxMindError

TEMPLATES = ("blank", "patrol", "pick_place", "status")
_ID = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,99}$")

_IMPORT = '''try:
    from neuxmind.skill import Skill
except ImportError:  # robot without the SDK installed: the runtime's base class works the same
    try:
        from neuxmind_runtime.skills import Skill
    except ImportError:
        Skill = object
'''

_TEST_HEADER = '''"""Local tests: run the skill in-process on the runtime's simulated bodies.

    python3 -m unittest discover -s {skill_dir}/tests

Simulation proves logic and protocol only, never on-site behaviour.
"""
from pathlib import Path

from neuxmind.skill.testing import SkillTestCase

SKILL_DIR = Path(__file__).resolve().parents[1]
'''

# --------------------------------------------------------------------- blank
_BLANK_PY = '''"""__TITLE__: a minimal skill. Start here.

It only needs system.health, so it runs on any NeuxMind body and never moves it.
Declare every capability you use in skill.json (requires / optional).
"""
__IMPORT__

class __CLASS__(Skill):
    def run(self, ctx, message="hello"):
        ctx.check_stop()
        health = ctx.read("system.health")
        ctx.log(message, simulated=ctx.simulated)
        ctx.progress(1.0, "完成")
        return {"message": message, "health_ok": health["ok"], "family": ctx.manifest["family"],
                "simulated": ctx.simulated}
'''

_BLANK_TEST = '''

class __CLASS__Test(SkillTestCase):
    skill_dir = SKILL_DIR

    def test_runs_on_every_body(self):
        for body in ("quadruped", "arm"):
            with self.subTest(body=body):
                result = self.run_skill(body, message="hi")
                self.assertSucceeded(result)
                self.assertEqual(result.result["message"], "hi")
                self.assertTrue(result.result["simulated"])
                self.assertIn("hi", result.logs)

    def test_compatible_everywhere(self):
        self.assertCompatible("quadruped")
        self.assertCompatible("arm")
'''

# --------------------------------------------------------------------- status
_STATUS_PY = '''"""__TITLE__: report whatever standard state the body offers. Never moves.

Cross-embodiment by design: only system.health is required, everything else is
optional and read only when ctx.has() says the body provides it.
"""
__IMPORT__

OPTIONAL = ("sensor.battery", "base.odometry", "joint.state", "sensor.imu")


class __CLASS__(Skill):
    def run(self, ctx):
        report = {"health": ctx.read("system.health"), "simulated": ctx.simulated,
                  "family": ctx.manifest["family"], "available": []}
        for cap in OPTIONAL:
            ctx.check_stop()
            if ctx.has(cap):
                report[cap] = ctx.read(cap)
                report["available"].append(cap)
        ctx.log("状态报告完成", available=report["available"])
        return report
'''

_STATUS_TEST = '''

class __CLASS__Test(SkillTestCase):
    skill_dir = SKILL_DIR

    def test_quadruped_reports_battery_and_odometry(self):
        result = self.run_skill("quadruped")
        self.assertSucceeded(result)
        self.assertEqual(result.result["family"], "quadruped")
        self.assertIn("sensor.battery", result.result["available"])
        self.assertIn("base.odometry", result.result["available"])

    def test_arm_reports_joint_state(self):
        result = self.run_skill("arm")
        self.assertSucceeded(result)
        self.assertEqual(result.result["available"], ["joint.state"])
        self.assertEqual(len(result.result["joint.state"]["position"]), 6)
'''

# --------------------------------------------------------------------- patrol
_PATROL_PY = '''"""__TITLE__: visit waypoints in order with the standard base.navigate_to capability.

Portable: depends only on capability IDs. Stands up first if the body has
legged.posture. Waypoints are [x, y] in metres or full 7-element poses.
"""
__IMPORT__


def to_pose(waypoint):
    if isinstance(waypoint, list) and len(waypoint) == 2:
        return [float(waypoint[0]), float(waypoint[1]), 0.0, 0.0, 0.0, 0.0, 1.0]
    if isinstance(waypoint, list) and len(waypoint) == 7:
        return [float(v) for v in waypoint]
    raise ValueError(f"航点格式无效：{waypoint!r}（需要 [x, y] 或 7 元位姿）")


class __CLASS__(Skill):
    def run(self, ctx, waypoints, loops=1, tolerance=0.15):
        poses = [to_pose(w) for w in waypoints]
        if not poses:
            raise ValueError("至少需要一个航点")
        if ctx.has("legged.posture"):
            ctx.call("legged.posture", posture="stand")
        total, reached = len(poses) * loops, 0
        for loop in range(loops):
            for index, pose in enumerate(poses):
                ctx.check_stop()
                ctx.log("前往航点", loop=loop, index=index, goal=pose[:2])
                result = ctx.call("base.navigate_to", goal=pose, tolerance=tolerance)
                if not result.get("reached"):
                    ctx.call("safety.stop", reason="__ID__: 航点未到达")
                    raise RuntimeError(f"航点 {index} 未到达")
                reached += 1
                ctx.progress(reached / total, f"到达航点 {index}")
        final = ctx.read("base.odometry")["pose"] if ctx.has("base.odometry") else None
        return {"reached": reached, "loops": loops, "final_pose": final}
'''

_PATROL_TEST = '''

class __CLASS__Test(SkillTestCase):
    skill_dir = SKILL_DIR

    def test_patrols_on_quadruped(self):
        result = self.run_skill("quadruped", waypoints=[[1.0, 0.0], [1.0, 1.0]])
        self.assertSucceeded(result)
        self.assertEqual(result.result["reached"], 2)
        x, y = result.result["final_pose"][:2]
        self.assertAlmostEqual(x, 1.0, delta=0.2)
        self.assertAlmostEqual(y, 1.0, delta=0.2)

    def test_bad_parameters_are_rejected_before_running(self):
        from neuxmind import NeuxMindError
        with self.assertRaises(NeuxMindError):
            self.run_skill("quadruped", waypoints=[[1, 0]], speed=3)   # undeclared parameter

    def test_arm_cannot_patrol(self):
        self.assertCompatible("quadruped")
        self.assertIncompatible("arm", missing=["base.navigate_to"])
'''

# ----------------------------------------------------------------- pick_place
_PICK_PY = '''"""__TITLE__: joint-space pick and place for 6+ DoF arms with a gripper.

pick / place are joint positions (rad). The runtime's safety supervisor
rejects targets outside the arm's declared joint limits — this skill does not
clamp them silently. Grasp success is only reported if the body reports it
(gripper.position output "grasped"); simulated arms never do.
"""
__IMPORT__

DEFAULT_PICK = [0.5, -0.6, 1.2, 0.0, 0.6, 0.0]
DEFAULT_PLACE = [-0.5, -0.6, 1.2, 0.0, 0.6, 0.0]


class __CLASS__(Skill):
    def run(self, ctx, pick=None, place=None, close=0.0, max_velocity=0.8):
        dof = ctx.param("arm.joint_position", "dof")
        pick = list(pick if pick is not None else DEFAULT_PICK)
        place = list(place if place is not None else DEFAULT_PLACE)
        for name, q in (("pick", pick), ("place", place)):
            if len(q) != dof:
                raise ValueError(f"{name} 需要 {dof} 个关节角，收到 {len(q)} 个")
        home = [0.0] * dof

        ctx.call("gripper.position", opening=1.0)
        ctx.call("arm.joint_position", position=pick, max_velocity=max_velocity)
        ctx.progress(0.3, "到达抓取位")
        grip = ctx.call("gripper.position", opening=close)
        grasped = grip.get("grasped")          # None: the body does not report grasps
        if grasped is None:
            ctx.log("本体未报告抓取结果，无法确认是否抓到物体")
        elif not grasped:
            ctx.call("gripper.position", opening=1.0)
            raise RuntimeError("未抓到物体")
        ctx.check_stop()
        ctx.call("arm.joint_position", position=place, max_velocity=max_velocity)
        ctx.progress(0.7, "到达放置位")
        ctx.call("gripper.position", opening=1.0)
        final = ctx.call("arm.joint_position", position=home, max_velocity=max_velocity)
        return {"grasped": grasped, "final_position": final["position"]}
'''

_PICK_TEST = '''

class __CLASS__Test(SkillTestCase):
    skill_dir = SKILL_DIR

    def test_pick_and_place_on_arm(self):
        result = self.run_skill("arm")
        self.assertSucceeded(result)
        self.assertIsNone(result.result["grasped"])     # the simulated arm never reports a grasp
        for q in result.result["final_position"]:
            self.assertAlmostEqual(q, 0.0, places=3)

    def test_out_of_limit_target_is_rejected_by_safety(self):
        result = self.run_skill("arm", pick=[9.0, 0, 0, 0, 0, 0])
        self.assertEqual(result.status, "failed")

    def test_quadruped_cannot_pick(self):
        self.assertCompatible("arm")
        self.assertIncompatible("quadruped", missing=["arm.joint_position", "gripper.position"])
'''


def _skill_json(template: str, skill_id: str, title: str) -> dict:
    base = {"schema": "skill.package.v1", "id": skill_id, "version": "0.1.0", "title": title}
    cls = class_name(skill_id)
    if template == "blank":
        return {**base, "description": "最小技能：读取健康状态并记录一条日志。任何本体可运行，不运动。",
                "entry": f"skill.py:{cls}", "requires": [{"id": "system.health"}],
                "parameters": [{"name": "message", "type": "str", "required": False, "default": "hello"}],
                "timeout_s": 30}
    if template == "status":
        return {**base, "description": "状态报告：本体有什么读什么（电池、里程计、关节、IMU）。任何本体可运行，不运动。",
                "entry": f"skill.py:{cls}", "requires": [{"id": "system.health"}],
                "optional": [{"id": "sensor.battery"}, {"id": "base.odometry"}, {"id": "joint.state"},
                             {"id": "sensor.imu"}],
                "parameters": [], "timeout_s": 30}
    if template == "patrol":
        return {**base, "description": "依次导航到一组航点，可循环多圈；有 legged.posture 时先站立。",
                "entry": f"skill.py:{cls}",
                "requires": [{"id": "base.navigate_to"}, {"id": "safety.stop"}],
                "optional": [{"id": "legged.posture"}, {"id": "base.odometry"}],
                "parameters": [
                    {"name": "waypoints", "type": "object", "required": False,
                     "default": [[1.0, 0.0], [1.0, 1.0], [0.0, 0.0]],
                     "description": "[[x, y], ...] 或 7 元位姿列表，单位 m"},
                    {"name": "loops", "type": "int", "required": False, "default": 1},
                    {"name": "tolerance", "type": "float", "required": False, "default": 0.15}],
                "timeout_s": 600}
    if template == "pick_place":
        return {**base, "description": "关节空间抓取-放置：张开 → 抓取位 → 闭合 → 放置位 → 张开 → 回零。需要 ≥6 自由度机械臂和夹爪。",
                "entry": f"skill.py:{cls}",
                "requires": [{"id": "arm.joint_position"}, {"id": "gripper.position"}],
                "optional": [{"id": "joint.state"}],
                "constraints": {"arm.joint_position": {"dof": {"min": 6}}},
                "parameters": [
                    {"name": "pick", "type": "floats", "required": False, "description": "抓取位关节角 rad"},
                    {"name": "place", "type": "floats", "required": False, "description": "放置位关节角 rad"},
                    {"name": "close", "type": "float", "required": False, "default": 0.0,
                     "description": "抓取时的夹爪开度 0–1"},
                    {"name": "max_velocity", "type": "float", "required": False, "default": 0.8}],
                "timeout_s": 120}
    raise NeuxMindError(f"未知模板 {template}；可选 {', '.join(TEMPLATES)}", code="unknown_template")


_SOURCES = {"blank": (_BLANK_PY, _BLANK_TEST), "status": (_STATUS_PY, _STATUS_TEST),
            "patrol": (_PATROL_PY, _PATROL_TEST), "pick_place": (_PICK_PY, _PICK_TEST)}

_TITLES = {"blank": "新技能", "status": "状态报告", "patrol": "巡逻", "pick_place": "抓取放置"}

_RUN_HINT = {
    "blank": ("sim-quadruped", "--param message=你好"),
    "status": ("sim-arm", ""),
    "patrol": ("sim-quadruped", "--param 'waypoints=[[1,0],[1,1]]'"),
    "pick_place": ("sim-arm", ""),
}


def class_name(skill_id: str) -> str:
    parts = [p for p in re.split(r"[^0-9a-zA-Z]+", skill_id) if p]
    name = "".join(p[:1].upper() + p[1:] for p in parts) or "MySkill"
    return name if name[0].isalpha() else "Skill" + name


def _readme(template: str, skill_id: str, meta: dict) -> str:
    robot, params = _RUN_HINT[template]
    requires = ", ".join(r["id"] for r in meta["requires"])
    optional = ", ".join(r["id"] for r in meta.get("optional", [])) or "无"
    param_lines = "\n".join(f"| `{p['name']}` | {p['type']} | {json.dumps(p.get('default'), ensure_ascii=False)} | "
                            f"{p.get('description', '')} |" for p in meta["parameters"]) or "| （无） | | | |"
    run_line = f"nx skill run {skill_id} --robot {robot} {params} --authorize --wait".replace("  ", " ")
    return f"""# {meta['title']}（`{skill_id}`）

{meta['description']}

由 `nx skill new {skill_id} --template {template}` 生成。

- 必需能力：{requires}
- 可选能力：{optional}
- 入口：`{meta['entry']}`（类会被运行时实例化，然后调用 `run(ctx, **参数)`）

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
{param_lines}

## 本地测试（仿真本体，进程内）

```bash
python3 -m unittest discover -s {skill_id}/tests
```

## 在机器人上运行

```bash
nx skill check {skill_id} --robot {robot}
nx skill install {skill_id} --robot {robot}
{run_line}
nx stop {robot}            # 任何时候都可以
```

运行需要 `--authorize`（硬件授权）。授权前确认现场安全、急停在手。仿真结果不能作为现场证据。
"""


def render(template: str, skill_id: str, title: str | None = None) -> dict[str, str]:
    """Return ``{relative path: text}`` for a new skill."""
    if template not in TEMPLATES:
        raise NeuxMindError(f"未知模板 {template}；可选 {', '.join(TEMPLATES)}", code="unknown_template")
    if not _ID.match(skill_id):
        raise NeuxMindError("技能 ID 只能包含小写字母、数字、点、下划线和连字符，且以字母或数字开头",
                            code="invalid_skill_id")
    meta = _skill_json(template, skill_id, title or _TITLES[template])
    py, test = _SOURCES[template]
    subs = {"__TITLE__": meta["title"], "__CLASS__": class_name(skill_id), "__ID__": skill_id,
            "__IMPORT__": _IMPORT}
    for key, value in subs.items():
        py = py.replace(key, value)
        test = test.replace(key, value)
    return {
        "skill.json": json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        "skill.py": py,
        "tests/test_skill.py": _TEST_HEADER.format(skill_dir=skill_id) + test,
        "README.md": _readme(template, skill_id, meta),
    }


def create(template: str, skill_id: str, directory: str | Path | None = None, *,
           title: str | None = None, force: bool = False) -> Path:
    """Write a new skill directory (default ``./<skill_id>``) and return its path."""
    files = render(template, skill_id, title)
    target = Path(directory) if directory else Path(skill_id)
    if target.exists() and any(target.iterdir()) and not force:
        raise NeuxMindError(f"目录 {target} 已存在且不为空；换个目录或加 --force", code="exists")
    for rel, text in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return target
