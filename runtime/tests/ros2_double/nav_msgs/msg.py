"""TEST DOUBLE of nav_msgs.msg."""
from geometry_msgs.msg import Header, PoseWithCovariance, TwistWithCovariance


class Odometry:
    def __init__(self):
        self.header = Header()
        self.child_frame_id = "base_link"
        self.pose, self.twist = PoseWithCovariance(), TwistWithCovariance()
