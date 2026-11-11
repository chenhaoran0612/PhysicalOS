"""TEST DOUBLE of sensor_msgs.msg."""
from geometry_msgs.msg import Header, Quaternion, Vector3


class BatteryState:
    POWER_SUPPLY_STATUS_CHARGING = 1
    POWER_SUPPLY_STATUS_DISCHARGING = 2

    def __init__(self):
        self.header = Header()
        self.voltage = float("nan")
        self.percentage = float("nan")
        self.power_supply_status = 0


class Imu:
    def __init__(self):
        self.header = Header()
        self.orientation = Quaternion()
        self.angular_velocity = Vector3()
        self.linear_acceleration = Vector3()


class JointState:
    def __init__(self):
        self.header = Header()
        self.name, self.position, self.velocity, self.effort = [], [], [], []
