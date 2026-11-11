"""TEST DOUBLE of rclpy.executors (delivery is synchronous in the double; spin just waits)."""
from __future__ import annotations

import threading


class MultiThreadedExecutor:
    def __init__(self, num_threads=None, *, context=None):
        self._stop = threading.Event()
        self.nodes = []

    def add_node(self, node) -> None:
        self.nodes.append(node)

    def spin(self) -> None:
        self._stop.wait()

    def shutdown(self, timeout_sec=None) -> bool:
        self._stop.set()
        return True


SingleThreadedExecutor = MultiThreadedExecutor
