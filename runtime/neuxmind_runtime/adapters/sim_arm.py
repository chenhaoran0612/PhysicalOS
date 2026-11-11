"""SimArm — a simulated 6-DoF arm with a parallel gripper (``simulated=true``).

Joint-space linear interpolation at a bounded joint speed, gripper opening
interpolated at a fixed rate. No dynamics, collisions or objects: the
gripper never reports a grasp.
"""
from __future__ import annotations

from ..clock import Clock
from ._sim import CANCELLED, SimAdapterBase, SimGoal, SimHandle

DOF = 6
LOWER = [-3.05, -2.0, -2.6, -3.05, -2.0, -3.05]
UPPER = [3.05, 2.0, 2.6, 3.05, 2.0, 3.05]
MAX_JOINT_VELOCITY = 1.0     # rad/s
GRIPPER_RATE = 1.0           # opening ratio per second
JOINT_TOLERANCE = 1e-4
JOINT_NAMES = [f"joint{i + 1}" for i in range(DOF)]     # arm.joint_position order (= joint.state names)


class SimArm(SimAdapterBase):
    def __init__(self, time_scale: float = 1.0, clock: Clock | None = None,
                 instance_id: str = "sim-arm-01"):
        super().__init__(time_scale, clock)
        self.instance_id = instance_id
        self.q = [0.0] * DOF
        self.qd = [0.0] * DOF
        self.opening = 1.0
        self.arm_goal: SimGoal | None = None
        self.grip_goal: SimGoal | None = None

    def manifest(self) -> dict:
        return {
            "schema": "robot.adapter.v2", "spec_version": "1.1",
            "instance_id": self.instance_id, "name": "SimArm（仿真六轴机械臂）",
            "vendor": "neuxmind-sim", "model": "sim-arm-6dof", "family": "manipulator",
            "adapter_version": "0.1.0", "simulated": True,
            "capabilities": [
                {"id": "safety.stop", "params": {"hardware_estop": False}},
                {"id": "system.health"},
                {"id": "joint.state", "params": {"dof": DOF}},
                {"id": "arm.joint_position", "params": {"dof": DOF, "lower": LOWER, "upper": UPPER,
                                                        "max_velocity": MAX_JOINT_VELOCITY,
                                                        "joint_names": list(JOINT_NAMES)}},
                {"id": "gripper.position", "params": {"max_width": 0.08, "max_force": 40.0}},
                {"id": "gripper.state"},
            ],
            "model_runtimes": [],
            "safety": {"hardware_estop": False},
        }

    # ------------------------------------------------------------ physics
    def _step(self, dt: float) -> None:
        goal = self.arm_goal
        self.qd = [0.0] * DOF
        if goal is not None and goal.state == "running":
            target, speed = goal.target, goal.extra["speed"]
            remaining = max(abs(t - q) for t, q in zip(target, self.q))
            if remaining <= JOINT_TOLERANCE:
                self.q = list(target)
                goal.finish({"reached": True, "position": list(self.q)})
            else:
                fraction = min(1.0, speed * dt / remaining)   # synchronised joint motion
                new_q = [q + (t - q) * fraction for t, q in zip(target, self.q)]
                self.qd = [(n - q) / dt for n, q in zip(new_q, self.q)]
                self.q = new_q
                start = goal.extra["start_remaining"]
                goal.progress = 1.0 if start <= 0 else 1.0 - remaining / start
        grip = self.grip_goal
        if grip is not None and grip.state == "running":
            delta = grip.target - self.opening
            step = GRIPPER_RATE * dt
            if abs(delta) <= step:
                self.opening = grip.target
                grip.finish({"opening": self.opening})
            else:
                self.opening += step if delta > 0 else -step

    # ------------------------------------------------------------ adapter API
    def read(self, cap_id: str) -> dict:
        self.advance()
        if cap_id == "system.health":
            moving = (self.arm_goal is not None and self.arm_goal.state == "running") or \
                     (self.grip_goal is not None and self.grip_goal.state == "running")
            return {"ok": True, "mode": "sim:moving" if moving else "sim:idle", "errors": {}}
        if cap_id == "gripper.state":
            return {"opening": self.opening, "grasped": False}
        if cap_id == "joint.state":
            return {"names": list(JOINT_NAMES), "position": list(self.q),
                    "velocity": list(self.qd)}
        raise ValueError(f"SimArm 不能读取 {cap_id}")

    def invoke(self, cap_id: str, args: dict) -> dict:
        if cap_id == "safety.stop":
            self.stop()
            return {"stopped": True}
        raise ValueError(f"SimArm 不支持命令 {cap_id}")

    def start_action(self, cap_id: str, args: dict):
        with self._lock:
            self._advance_locked()
            if cap_id == "arm.joint_position":
                if self.arm_goal is not None:
                    self.arm_goal.abort(CANCELLED, "preempted by new goal")
                target = list(args["position"])
                speed = min(args.get("max_velocity", MAX_JOINT_VELOCITY), MAX_JOINT_VELOCITY)
                remaining = max(abs(t - q) for t, q in zip(target, self.q))
                self.arm_goal = SimGoal(cap_id, target, speed=speed, start_remaining=remaining)
                return SimHandle(self, self.arm_goal)
            if cap_id == "gripper.position":
                if self.grip_goal is not None:
                    self.grip_goal.abort(CANCELLED, "preempted by new goal")
                self.grip_goal = SimGoal(cap_id, float(args["opening"]))
                return SimHandle(self, self.grip_goal)
        raise ValueError(f"SimArm 不支持动作 {cap_id}")

    def stop(self) -> None:
        self._try_advance()
        for goal in (self.arm_goal, self.grip_goal):
            if goal is not None:
                goal.abort(CANCELLED, "stopped")
        self.arm_goal = self.grip_goal = None
        self.qd = [0.0] * DOF

    def status(self) -> dict:
        return {"simulated": True, "sim_time_s": round(self._t, 3),
                "time_scale": getattr(self.clock, "time_scale", 1.0)}
