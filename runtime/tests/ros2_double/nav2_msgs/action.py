"""TEST DOUBLE of nav2_msgs.action.NavigateToPose."""
from geometry_msgs.msg import PoseStamped


class NavigateToPose:
    class Goal:
        def __init__(self):
            self.pose = PoseStamped()
            self.behavior_tree = ""

    class Result:
        def __init__(self):
            self.error_code = 0

    class Feedback:
        def __init__(self):
            self.distance_remaining = 0.0
