"""Fake differential-drive robot living on the double's graph (TEST DOUBLE, not a simulator of any real robot).

Integrates the latest ``/cmd_vel`` Twist (no controller-side timeout: the
runtime watchdog is what stops it), publishes ``/odom``, ``/battery_state``,
``/imu``, ``/joint_states`` and ``/amcl_pose`` (map = odom) at 25 Hz, and serves ``navigate_to_pose``:
straight-line driving at 0.4 m/s to the goal (rotate first), status 4 on
arrival, 5 when cancelled. ``navigate_to_pose/_action/cancel_goal`` with a zero
goal id cancels every goal.
"""
from __future__ import annotations

import math
import threading
import time
from types import SimpleNamespace

from rclpy import _graph
from rclpy.task import Future

PERIOD_S = 0.02
PUBLISH_EVERY = 2          # 50 Hz integration, 25 Hz publishing
NAV_SPEED, NAV_TURN = 0.4, 1.0


class _GoalHandle:
    def __init__(self, robot, goal):
        self.robot = robot
        self.accepted = True
        self.goal = goal
        self.status = 2                     # EXECUTING
        self.result_future = Future()

    def get_result_async(self):
        return self.result_future

    def cancel_goal_async(self):
        self.robot.finish(self, 5)
        future = Future()
        future.set_result(SimpleNamespace(return_code=0))
        return future


class _NavServer:
    def __init__(self, robot):
        self.robot = robot

    def send_goal(self, goal):
        future = Future()
        handle = _GoalHandle(self.robot, goal)
        with self.robot.lock:
            for other in list(self.robot.goals):
                self.robot.finish_locked(other, 5)       # Nav2 preempts the previous goal
            self.robot.goals.append(handle)
        future.set_result(handle)
        return future


class FakeRobot:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="fake-ros2-robot", daemon=True)

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.x = self.y = self.yaw = 0.0
            self.cmd = (0.0, 0.0)
            self.velocity = (0.0, 0.0)
            self.goals: list[_GoalHandle] = []
            self.battery = 0.87
            self.cmd_count = 0
            self.wheels = [0.0, 0.0]

    def start(self):
        _graph.subscribe("/cmd_vel", self._on_cmd)
        _graph.services["navigate_to_pose/_action/cancel_goal"] = self._cancel_all
        _graph.action_servers["navigate_to_pose"] = _NavServer(self)
        self.thread.start()

    def _on_cmd(self, msg):
        with self.lock:
            self.cmd = (float(msg.linear.x), float(msg.angular.z))
            self.cmd_count += 1

    def _cancel_all(self, request):
        with self.lock:
            for goal in list(self.goals):
                self.finish_locked(goal, 5)
        return SimpleNamespace(return_code=0, goals_canceling=[])

    def finish(self, handle, status):
        with self.lock:
            self.finish_locked(handle, status)

    def finish_locked(self, handle, status):
        if handle in self.goals:
            self.goals.remove(handle)
            handle.status = status
            handle.result_future.set_result(SimpleNamespace(status=status, result=SimpleNamespace(error_code=0)))

    def _loop(self):
        tick = 0
        last = time.monotonic()
        while not self._stop.wait(PERIOD_S):
            now = time.monotonic()
            dt, last = now - last, now
            with self.lock:
                self._step(dt)
            tick += 1
            if tick % PUBLISH_EVERY == 0:
                self._publish()

    def _step(self, dt):
        if self.goals:
            goal = self.goals[-1]
            p = goal.goal.pose.pose.position
            dx, dy = p.x - self.x, p.y - self.y
            dist = math.hypot(dx, dy)
            if dist < 0.03:
                vx, wz = 0.0, 0.0
                self.finish_locked(goal, 4)
            else:
                err = math.remainder(math.atan2(dy, dx) - self.yaw, 2 * math.pi)
                wz = max(-NAV_TURN, min(NAV_TURN, 2.0 * err))
                vx = 0.0 if abs(err) > 0.3 else min(NAV_SPEED, 1.5 * dist)
        else:
            vx, wz = self.cmd
        self.x += vx * math.cos(self.yaw) * dt
        self.y += vx * math.sin(self.yaw) * dt
        self.yaw = math.remainder(self.yaw + wz * dt, 2 * math.pi)
        self.velocity = (vx, wz)
        self.wheels = [w + (vx + s * 0.2 * wz) / 0.1 * dt for w, s in zip(self.wheels, (-1, 1))]
        self.battery = max(0.0, self.battery - 1e-6)

    def _publish(self):
        from geometry_msgs.msg import Quaternion
        from nav_msgs.msg import Odometry
        from sensor_msgs.msg import BatteryState, Imu, JointState
        with self.lock:
            x, y, yaw, (vx, wz), battery, wheels = self.x, self.y, self.yaw, self.velocity, self.battery, list(self.wheels)
        q = Quaternion(0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
        odom = Odometry()
        odom.header.frame_id = "odom"
        odom.pose.pose.position.x, odom.pose.pose.position.y = x, y
        odom.pose.pose.orientation = q
        odom.twist.twist.linear.x, odom.twist.twist.angular.z = vx, wz
        _graph.publish("/odom", odom)
        bat = BatteryState()
        bat.percentage, bat.voltage, bat.power_supply_status = battery, 24.0, 2
        _graph.publish("/battery_state", bat)
        imu = Imu()
        imu.orientation = q
        imu.angular_velocity.z = wz
        imu.linear_acceleration.z = 9.81
        _graph.publish("/imu", imu)
        js = JointState()
        js.name, js.position, js.velocity = ["wheel_left", "wheel_right"], wheels, [0.0, 0.0]
        _graph.publish("/joint_states", js)
        from geometry_msgs.msg import PoseWithCovarianceStamped
        amcl = PoseWithCovarianceStamped()            # the double's map frame coincides with odom
        amcl.header.frame_id = "map"
        amcl.pose.pose.position.x, amcl.pose.pose.position.y = x, y
        amcl.pose.pose.orientation = q
        _graph.publish("/amcl_pose", amcl)
