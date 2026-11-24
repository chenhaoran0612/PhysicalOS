"""SimQuadruped — a simulated quadruped reference body (``simulated=true``).

Planar unicycle/holonomic kinematics for the trunk, preset postures, a
straight-line "navigator", a battery that drains with time and distance.
It exists so skills, the safety supervisor and the gateway can be exercised
without hardware. It says nothing about how a real quadruped behaves.
"""
from __future__ import annotations

import math

from ..clock import Clock
from ._sim import (CANCELLED, SimAdapterBase, SimGoal, SimHandle, failed_handle,
                   quat_to_yaw, wrap_angle, yaw_to_quat)

MAX_VX, MAX_VY, MAX_WZ = 1.0, 0.5, 1.5
NAV_SPEED = 0.6            # m/s cruise speed of the simulated navigator
NAV_HEADING_GATE = 0.35    # rad: rotate in place above this heading error
DEFAULT_TOLERANCE = 0.10   # m
POSTURES = ["stand", "lie", "sit", "recover"]
# Issue #38 (spec 1.2). Places the simulated body "knows", in its map frame (made-up coordinates), so
# skills written against base.navigate_to_place can be tried without hardware.
PLACES = {"出发点": (0.0, 0.0), "会议室": (3.0, 1.0), "二楼": (1.0, -2.5)}
EXPLORE_MAX_DURATION_S = 600.0
EXPLORE_MAX_RADIUS = 10.0
EXPLORE_DEFAULT_RADIUS = 2.0
EXPLORE_LEGS = 5           # the simulated wander visits the points of a star around where it started


class SimQuadruped(SimAdapterBase):
    def __init__(self, time_scale: float = 1.0, clock: Clock | None = None,
                 instance_id: str = "sim-quadruped-01"):
        super().__init__(time_scale, clock)
        self.instance_id = instance_id
        self.x = self.y = self.yaw = 0.0
        self.cmd = (0.0, 0.0, 0.0)          # body-frame vx, vy, wz (setpoint)
        self.velocity = (0.0, 0.0, 0.0)     # last integrated velocity
        self.posture = "stand"
        self.distance = 0.0
        self.goal: SimGoal | None = None

    # ------------------------------------------------------------ manifest
    def manifest(self) -> dict:
        return {
            "schema": "robot.adapter.v2", "spec_version": "1.2",
            "instance_id": self.instance_id, "name": "SimQuadruped（仿真四足）",
            "vendor": "neuxmind-sim", "model": "sim-quadruped", "family": "quadruped",
            "adapter_version": "0.1.0", "simulated": True,
            "capabilities": [
                {"id": "safety.stop", "params": {"hardware_estop": False}},
                {"id": "system.health"},
                {"id": "sensor.battery"},
                {"id": "sensor.imu", "params": {"rate_hz": 100.0, "frame": "base_link"}},
                {"id": "base.odometry", "params": {"frame": "odom"}},
                # The simulation's map frame coincides with odom (no drift); a real body's does not.
                {"id": "localization.pose"},
                {"id": "base.velocity", "params": {"max_vx": MAX_VX, "max_vy": MAX_VY,
                                                   "max_wz": MAX_WZ, "holonomic": True}},
                {"id": "base.navigate_to", "params": {"planner": "sim-straight-line"}},
                {"id": "base.navigate_to_place", "params": {"places": list(PLACES)}},
                {"id": "base.explore", "params": {"max_duration_s": EXPLORE_MAX_DURATION_S,
                                                  "max_radius": EXPLORE_MAX_RADIUS}},
                {"id": "legged.posture", "params": {"supported": POSTURES}},
            ],
            "model_runtimes": [],
            "safety": {"hardware_estop": False, "max_linear_speed": MAX_VX,
                       "workspace": {"x": [-200.0, 200.0], "y": [-200.0, 200.0]}},
        }

    # ------------------------------------------------------------ physics
    def _step(self, dt: float) -> None:
        goal = self.goal
        if goal is not None and goal.state != "running":
            self.goal = goal = None
            self.cmd = (0.0, 0.0, 0.0)
        if goal is not None and goal.kind == "base.explore":
            vx, vy, wz = self._explore(goal, dt)
        elif goal is not None:
            vx, vy, wz = self._navigate(goal)
        else:
            vx, vy, wz = self.cmd
        if self.posture != "stand":
            vx = vy = wz = 0.0
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        self.x += (vx * c - vy * s) * dt
        self.y += (vx * s + vy * c) * dt
        self.yaw = wrap_angle(self.yaw + wz * dt)
        self.distance += math.hypot(vx, vy) * dt
        self.velocity = (vx, vy, wz)

    def _navigate(self, goal: SimGoal) -> tuple[float, float, float]:
        gx, gy = goal.target
        dx, dy = gx - self.x, gy - self.y
        dist = math.hypot(dx, dy)
        start = goal.extra["start_dist"]
        goal.progress = 1.0 if start <= 0 else max(0.0, min(1.0, 1.0 - dist / start))
        if dist <= goal.extra["tolerance"]:
            result = {"reached": True, "final_pose": self._pose()}
            if "place" in goal.extra:
                result["place"] = goal.extra["place"]
            goal.finish(result)
            return 0.0, 0.0, 0.0
        return self._steer(dx, dy, dist, goal.extra.get("max_speed", NAV_SPEED))

    def _steer(self, dx: float, dy: float, dist: float, speed: float) -> tuple[float, float, float]:
        heading_error = wrap_angle(math.atan2(dy, dx) - self.yaw)
        wz = max(-MAX_WZ, min(MAX_WZ, 2.0 * heading_error))
        vx = 0.0 if abs(heading_error) > NAV_HEADING_GATE else min(NAV_SPEED, speed, 1.5 * dist)
        return vx, 0.0, wz

    def _explore(self, goal: SimGoal, dt: float) -> tuple[float, float, float]:
        """A stand-in for wandering: walk the points of a star around the start until time or distance
        runs out. No obstacles, no perception -- it only makes the capability's contract observable."""
        e = goal.extra
        e["elapsed"] += dt
        goal.progress = max(0.0, min(1.0, e["elapsed"] / e["duration_s"]))
        limit = e.get("max_distance")
        if e["elapsed"] >= e["duration_s"] or (limit is not None and e["travelled"] >= limit):
            goal.finish({"completed": True, "travelled": e["travelled"], "final_pose": self._pose()})
            return 0.0, 0.0, 0.0
        ox, oy = e["origin"]
        angle = e["leg"] * 2.0 * math.pi * 2.0 / EXPLORE_LEGS
        tx, ty = ox + e["radius"] * math.cos(angle), oy + e["radius"] * math.sin(angle)
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        if dist <= DEFAULT_TOLERANCE:
            e["leg"] += 1
            return 0.0, 0.0, 0.0
        vx, vy, wz = self._steer(dx, dy, dist, e["max_speed"])
        e["travelled"] += abs(vx) * dt
        return vx, vy, wz

    def _pose(self) -> list[float]:
        return [self.x, self.y, 0.0] + yaw_to_quat(self.yaw)

    # ------------------------------------------------------------ adapter API
    def read(self, cap_id: str) -> dict:
        self.advance()
        if cap_id == "system.health":
            goal = self.goal
            doing = "" if goal is None else ":exploring" if goal.kind == "base.explore" else ":navigating"
            mode = "sim:" + self.posture + doing
            return {"ok": True, "mode": mode, "errors": {}}
        if cap_id == "sensor.battery":
            percent = max(0.0, 100.0 - 0.002 * self._t - 0.05 * self.distance)
            return {"percent": percent, "charging": False, "voltage": 22.0 + 3.2 * percent / 100.0}
        if cap_id == "sensor.imu":
            return {"orientation": yaw_to_quat(self.yaw), "angular_velocity": [0.0, 0.0, self.velocity[2]],
                    "linear_acceleration": [0.0, 0.0, 9.81]}
        if cap_id == "base.odometry":
            return {"pose": self._pose(), "velocity": list(self.velocity)}
        if cap_id == "localization.pose":
            return {"pose": self._pose(), "map_id": "sim-map"}
        raise ValueError(f"SimQuadruped 不能读取 {cap_id}")

    def invoke(self, cap_id: str, args: dict) -> dict:
        if cap_id == "safety.stop":
            self.stop()
            return {"stopped": True}
        with self._lock:
            self._advance_locked()
            if cap_id == "base.velocity":
                if self.posture != "stand":
                    return {"accepted": False}
                if self.goal is not None:            # velocity preempts navigation
                    self.goal.abort(CANCELLED, "preempted by base.velocity")
                    self.goal = None
                self.cmd = (args["vx"], args.get("vy", 0.0), args["wz"])
                return {"accepted": True}
            if cap_id == "legged.posture":
                posture = "stand" if args["posture"] == "recover" else args["posture"]
                if posture != "stand":
                    self.cmd = (0.0, 0.0, 0.0)
                    if self.goal is not None:
                        self.goal.abort(CANCELLED, "posture changed")
                self.posture = posture
                return {"accepted": True}
        raise ValueError(f"SimQuadruped 不支持命令 {cap_id}")

    def start_action(self, cap_id: str, args: dict):
        if cap_id not in ("base.navigate_to", "base.navigate_to_place", "base.explore"):
            raise ValueError(f"SimQuadruped 不支持动作 {cap_id}")
        if cap_id == "base.navigate_to_place" and args["place"] not in PLACES:
            raise ValueError(f"SimQuadruped 不认识地点 {args['place']}；认识 {list(PLACES)}")
        with self._lock:
            self._advance_locked()
            if self.posture != "stand":
                return failed_handle(self, cap_id, "需要站立姿态才能导航")
            if self.goal is not None:
                self.goal.abort(CANCELLED, "preempted by new goal")
            speed = min(NAV_SPEED, float(args.get("max_speed", NAV_SPEED)))
            if cap_id == "base.explore":
                goal = SimGoal(cap_id, None, origin=(self.x, self.y), duration_s=float(args["duration_s"]),
                               radius=float(args.get("radius", EXPLORE_DEFAULT_RADIUS)),
                               max_distance=args.get("max_distance"), max_speed=speed,
                               elapsed=0.0, travelled=0.0, leg=0)
            elif cap_id == "base.navigate_to_place":
                gx, gy = PLACES[args["place"]]
                goal = SimGoal(cap_id, (gx, gy), tolerance=DEFAULT_TOLERANCE, place=args["place"], max_speed=speed,
                               start_dist=math.hypot(gx - self.x, gy - self.y))
            else:
                gx, gy = args["goal"][0], args["goal"][1]
                goal = SimGoal(cap_id, (gx, gy), tolerance=args.get("tolerance", DEFAULT_TOLERANCE),
                               start_dist=math.hypot(gx - self.x, gy - self.y),
                               goal_yaw=quat_to_yaw(*args["goal"][3:7]))
            self.cmd = (0.0, 0.0, 0.0)
            self.goal = goal
        return SimHandle(self, goal)

    def stop(self) -> None:
        # Lock-free: integrate up to now only if nobody holds the lock, then zero.
        self._try_advance()
        self.cmd = (0.0, 0.0, 0.0)
        goal = self.goal
        if goal is not None:
            goal.abort(CANCELLED, "stopped")
        self.goal = None
        self.velocity = (0.0, 0.0, 0.0)

    def status(self) -> dict:
        return {"simulated": True, "sim_time_s": round(self._t, 3),
                "time_scale": getattr(self.clock, "time_scale", 1.0)}
