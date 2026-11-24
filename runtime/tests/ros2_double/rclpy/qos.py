"""TEST DOUBLE of rclpy.qos (the names the bridge uses, as in ROS 2 Humble). Profiles are only
recorded: the double's graph delivers every message whatever the profile says."""
from __future__ import annotations

from enum import IntEnum


class ReliabilityPolicy(IntEnum):
    SYSTEM_DEFAULT = 0
    RELIABLE = 1
    BEST_EFFORT = 2


class DurabilityPolicy(IntEnum):
    SYSTEM_DEFAULT = 0
    TRANSIENT_LOCAL = 1
    VOLATILE = 2


class HistoryPolicy(IntEnum):
    SYSTEM_DEFAULT = 0
    KEEP_LAST = 1
    KEEP_ALL = 2


class QoSProfile:
    def __init__(self, *, history=HistoryPolicy.KEEP_LAST, depth=10, reliability=ReliabilityPolicy.RELIABLE,
                 durability=DurabilityPolicy.VOLATILE):
        self.history, self.depth, self.reliability, self.durability = history, depth, reliability, durability
