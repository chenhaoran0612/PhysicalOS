"""TEST DOUBLE of rclpy.context."""


class Context:
    def __init__(self):
        self._ok = False

    def ok(self) -> bool:
        return self._ok
