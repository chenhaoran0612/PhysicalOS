"""Built-in adapters and adapter loading.

``sim_quadruped`` and ``sim_arm`` are simulated reference bodies.
``ros2`` is an unverified bridge skeleton. Vendor adapters are loaded with
``module:Class`` (trusted code installed by the operator).
"""
from __future__ import annotations

import importlib
import inspect

BUILTIN = {
    "sim_quadruped": (".sim_quadruped", "SimQuadruped"),
    "sim_arm": (".sim_arm", "SimArm"),
    "ros2": (".ros2", "Ros2Adapter"),
}


def load_adapter(name: str, time_scale: float = 1.0):
    """Instantiate a built-in adapter by name or a ``module:Class`` adapter."""
    if name in BUILTIN:
        relative, cls_name = BUILTIN[name]
        module_name = __package__ + relative
    elif ":" in name:
        module_name, cls_name = name.split(":", 1)
    else:
        raise ValueError(f"未知适配器 {name}；可用 {sorted(BUILTIN)} 或 module:Class")
    cls = getattr(importlib.import_module(module_name), cls_name)
    kwargs = {}
    if "time_scale" in inspect.signature(cls).parameters:
        kwargs["time_scale"] = time_scale
    elif time_scale != 1.0:
        raise ValueError(f"{name} 不支持 time_scale（只有仿真本体可以加速）")
    return cls(**kwargs)
