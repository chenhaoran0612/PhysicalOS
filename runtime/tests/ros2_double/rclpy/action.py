"""TEST DOUBLE of rclpy.action.ActionClient."""
from __future__ import annotations

from . import _graph


class ActionClient:
    def __init__(self, node, action_type, name: str):
        self.node, self.action_type, self.name = node, action_type, name

    def server_is_ready(self) -> bool:
        return self.name in _graph.action_servers

    def wait_for_server(self, timeout_sec=None) -> bool:
        return self.server_is_ready()

    def send_goal_async(self, goal, feedback_callback=None):
        return _graph.action_servers[self.name].send_goal(goal)
