"""NeuxMind Robot OS shared specification (stdlib only).

Import from here in runtime/, cloud/, sdk/ and brainos/:

    from neuxmind_spec import capabilities, manifest
"""
from . import capabilities, manifest  # noqa: F401
from . import ros2_interfaces  # noqa: F401  (issue #28: which ROS 2 topic or action carries a capability)
from .capabilities import SPEC_VERSION  # noqa: F401
