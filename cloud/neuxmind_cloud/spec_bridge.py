"""Import the shared capability standard (``spec/neuxmind_spec``).

The cloud never re-implements capability or manifest rules; it calls the spec.
If the package is not installed, we look for it at ``$NEUXMIND_SPEC_PATH`` or in
the repository checkout (``<repo>/spec``).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from neuxmind_spec import capabilities, manifest  # type: ignore
except ImportError:  # pragma: no cover - depends on deployment layout
    candidates = [os.environ.get("NEUXMIND_SPEC_PATH", ""), str(Path(__file__).resolve().parents[2] / "spec")]
    for candidate in candidates:
        if candidate and (Path(candidate) / "neuxmind_spec").is_dir():
            sys.path.insert(0, candidate)
            break
    from neuxmind_spec import capabilities, manifest  # type: ignore  # noqa: E402

__all__ = ["capabilities", "manifest"]
