"""TEST DOUBLE of rclpy.task.Future."""
from __future__ import annotations

import threading


class Future:
    def __init__(self):
        self._done = False
        self._result = None
        self._callbacks = []
        self._lock = threading.Lock()

    def done(self) -> bool:
        return self._done

    def result(self):
        return self._result

    def set_result(self, value) -> None:
        with self._lock:
            self._result, self._done = value, True
            callbacks, self._callbacks = self._callbacks, []
        for fn in callbacks:
            fn(self)

    def add_done_callback(self, fn) -> None:
        with self._lock:
            if not self._done:
                self._callbacks.append(fn)
                return
        fn(self)
