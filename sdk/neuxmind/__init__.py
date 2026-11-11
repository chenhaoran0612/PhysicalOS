"""NeuxMind Robot OS developer SDK.

* :mod:`neuxmind.robot`   — :class:`RobotClient` for the edge runtime HTTP API (v2, v1 manifest)
* :mod:`neuxmind.cloud`   — :class:`CloudClient` for the NeuxMind Cloud API
* :mod:`neuxmind.skill`   — writing skills (``Skill`` base class, context type hints)
* :mod:`neuxmind.skill.testing` — run skills on simulated bodies in-process
* :mod:`neuxmind.package` — build / validate skill packages, explain compatibility
* :mod:`neuxmind.cli`     — the ``nx`` command line

The core SDK uses only the Python standard library.
"""
from .errors import NeuxMindError

__version__ = "0.1.0"

__all__ = ["NeuxMindError", "__version__"]
