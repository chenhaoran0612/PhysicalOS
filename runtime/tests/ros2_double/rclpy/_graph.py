"""In-memory topic / service / action graph shared by every node (TEST DOUBLE)."""
from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.RLock()
subscriptions: dict[str, list] = defaultdict(list)     # topic -> [callback]
services: dict[str, object] = {}                         # name -> handler(request) -> response
action_servers: dict[str, object] = {}                   # name -> server object
published: dict[str, list] = defaultdict(list)           # topic -> last messages (bounded), for tests
_robot = None


def publish(topic: str, msg) -> None:
    with _lock:
        callbacks = list(subscriptions.get(topic, ()))
        log = published[topic]
        log.append(msg)
        if len(log) > 200:
            del log[:100]
    for cb in callbacks:
        cb(msg)


def subscribe(topic: str, callback) -> None:
    with _lock:
        subscriptions[topic].append(callback)


def unsubscribe(topic: str, callback) -> None:
    with _lock:
        if callback in subscriptions.get(topic, []):
            subscriptions[topic].remove(callback)


def ensure_robot():
    """Start the fake robot once per process (``FAKE_ROS2_ROBOT=0`` disables it)."""
    global _robot
    import os
    if os.environ.get("FAKE_ROS2_ROBOT", "1") == "0":
        return None
    with _lock:
        if _robot is None:
            from fake_robot import FakeRobot      # ros2_double/ is on sys.path
            _robot = FakeRobot()
            _robot.start()
        return _robot


def robot():
    return _robot
