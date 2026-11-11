"""TEST DOUBLE of rclpy (see ../README.md). Not ROS 2."""
from __future__ import annotations

import threading

from . import _graph
from .context import Context
from .node import Node

_default = Context()


def init(args=None, *, context: Context | None = None, **_kw) -> None:
    ctx = context or _default
    if ctx.ok():
        raise RuntimeError("Context.init() must only be called once")
    ctx._ok = True
    _graph.ensure_robot()


def ok(*, context: Context | None = None) -> bool:
    return (context or _default).ok()


def shutdown(*, context: Context | None = None) -> None:
    ctx = context or _default
    if not ctx.ok():
        raise RuntimeError("Context must be initialized before it can be shutdown")
    ctx._ok = False


def create_node(node_name: str, *, context: Context | None = None, **_kw) -> Node:
    ctx = context or _default
    if not ctx.ok():
        raise RuntimeError("rclpy.init() has not been called")
    return Node(node_name, ctx)


__all__ = ["init", "ok", "shutdown", "create_node", "Context", "Node", "threading"]
