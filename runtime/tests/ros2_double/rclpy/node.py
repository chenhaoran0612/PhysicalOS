"""TEST DOUBLE of rclpy.node.Node."""
from __future__ import annotations

import time
from types import SimpleNamespace

from . import _graph
from .task import Future


class _Publisher:
    def __init__(self, topic: str):
        self.topic = topic

    def publish(self, msg) -> None:
        _graph.publish(self.topic, msg)


class _Client:
    def __init__(self, name: str):
        self.name = name

    def service_is_ready(self) -> bool:
        return self.name in _graph.services

    def call_async(self, request) -> Future:
        future = Future()
        handler = _graph.services.get(self.name)
        future.set_result(handler(request) if handler is not None else None)
        return future


class _Time:
    def __init__(self, t: float):
        self.t = t

    def to_msg(self):
        return SimpleNamespace(sec=int(self.t), nanosec=int((self.t % 1) * 1e9))


class _Clock:
    def now(self):
        return _Time(time.time())


class Node:
    def __init__(self, name: str, context):
        self.name = name
        self.context = context
        self._subs = []
        self.parameters = {"use_sim_time": True}     # the double is a simulator

    def get_parameter(self, name: str):
        return SimpleNamespace(value=self.parameters.get(name))

    def create_publisher(self, msg_type, topic: str, qos):
        return _Publisher(topic)

    def create_subscription(self, msg_type, topic: str, callback, qos):
        _graph.subscribe(topic, callback)
        self._subs.append((topic, callback))
        return SimpleNamespace(topic=topic)

    def create_client(self, srv_type, name: str):
        return _Client(name)

    def get_clock(self):
        return _Clock()

    def destroy_node(self) -> None:
        for topic, cb in self._subs:
            _graph.unsubscribe(topic, cb)
        self._subs.clear()
