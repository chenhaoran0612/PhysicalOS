"""Path bootstrap so the runtime finds the shared ``neuxmind_spec`` package.

The spec lives in ``<repo>/spec/neuxmind_spec`` and is the single source of
truth for capabilities and manifests. On a robot the spec is normally
installed next to the runtime; in this repository we locate it relative to
this file. ``NEUXMIND_SPEC_PATH`` overrides the search (point it at the
directory that *contains* ``neuxmind_spec``).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]   # <repo>/runtime
REPO_ROOT = RUNTIME_DIR.parent                      # <repo>


def _candidates() -> list[Path]:
    paths = []
    override = os.environ.get("NEUXMIND_SPEC_PATH")
    if override:
        paths.append(Path(override))
    paths.append(REPO_ROOT / "spec")
    return paths


def ensure_spec_importable() -> None:
    """Make ``import neuxmind_spec`` work; raise a clear error otherwise.

    ``NEUXMIND_SPEC_PATH`` or the spec next to this runtime (same checkout) wins over an installed
    copy (round 07): an editable install pointing at another checkout used to shadow the spec this
    runtime was written against (e.g. a worktree with a newer spec importing the old one)."""
    if "neuxmind_spec" in sys.modules:
        return
    for path in _candidates():
        if (path / "neuxmind_spec" / "__init__.py").is_file():
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            return
    try:
        import neuxmind_spec  # noqa: F401
        return
    except ImportError:
        pass
    raise ImportError(
        "找不到 neuxmind_spec；请设置 NEUXMIND_SPEC_PATH 指向包含 neuxmind_spec 的目录"
    )


def ensure_runtime_importable() -> None:
    """Allow ``import neuxmind_runtime`` (used by skills) even when the runtime
    was started as ``python3 -m runtime.neuxmind_runtime`` from the repo root."""
    if str(RUNTIME_DIR) not in sys.path:
        sys.path.append(str(RUNTIME_DIR))


ensure_spec_importable()
ensure_runtime_importable()
