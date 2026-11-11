"""TEST DOUBLE of action_msgs.srv.CancelGoal."""
from types import SimpleNamespace


class CancelGoal:
    class Request:
        def __init__(self):
            self.goal_info = SimpleNamespace(goal_id=SimpleNamespace(uuid=[0] * 16),
                                             stamp=SimpleNamespace(sec=0, nanosec=0))

    class Response:
        def __init__(self):
            self.return_code = 0
            self.goals_canceling = []
