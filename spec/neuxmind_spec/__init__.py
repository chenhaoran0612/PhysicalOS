"""NeuxMind Robot OS shared specification (stdlib only).

Import from here in runtime/, cloud/, sdk/ and brainos/:

    from neuxmind_spec import capabilities, manifest
"""
from . import capabilities, manifest  # noqa: F401
from .capabilities import SPEC_VERSION  # noqa: F401
