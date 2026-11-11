"""NeuxMind Robot OS edge runtime.

Layers (bottom-up): adapter (HAL) → safety supervisor → capability bus →
skill runtime → HTTP gateway (v1 compatible + v2). See ``runtime/README.md``.
"""
from . import _paths  # noqa: F401  (makes neuxmind_spec importable)

__version__ = "0.4.0"
