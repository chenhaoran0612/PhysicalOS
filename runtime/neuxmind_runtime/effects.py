"""Per-thread "a side effect may have happened" marker, used by the ledger.

The ledger records a failed request as ``unknown`` (never replayed) only if
something may already have reached the robot or the disk; failures before
any side effect are recorded as rejections, so the same ``request_id`` can be
answered consistently instead of being burnt as "outcome unknown".

Code that is about to cause an effect calls :func:`mark` first (the
capability bus right before an adapter command/action, the run manager
before starting a run thread, the skill store before writing files).
"""
from __future__ import annotations

import threading

_local = threading.local()


def reset() -> None:
    _local.happened = False


def mark() -> None:
    _local.happened = True


def happened() -> bool:
    return getattr(_local, "happened", False)
