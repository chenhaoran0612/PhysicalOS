"""TEST DOUBLE of geometry_msgs.msg (field names as in ROS 2)."""
from __future__ import annotations

from types import SimpleNamespace


class Vector3:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z


class Point(Vector3):
    pass


class Quaternion:
    def __init__(self, x=0.0, y=0.0, z=0.0, w=1.0):
        self.x, self.y, self.z, self.w = x, y, z, w


class Twist:
    def __init__(self):
        self.linear, self.angular = Vector3(), Vector3()


class Pose:
    def __init__(self):
        self.position, self.orientation = Point(), Quaternion()


class PoseWithCovariance:
    def __init__(self):
        self.pose, self.covariance = Pose(), [0.0] * 36


class TwistWithCovariance:
    def __init__(self):
        self.twist, self.covariance = Twist(), [0.0] * 36


def Header():
    return SimpleNamespace(frame_id="", stamp=SimpleNamespace(sec=0, nanosec=0))


class PoseStamped:
    def __init__(self):
        self.header, self.pose = Header(), Pose()


class PoseWithCovarianceStamped:
    def __init__(self):
        self.header, self.pose = Header(), PoseWithCovariance()
