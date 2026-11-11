"""Locate the shared spec and the edge runtime packages.

``neuxmind_spec`` (``spec/``) and ``neuxmind_runtime`` (``runtime/``) are not
published on PyPI yet. The SDK finds them, in order:

1. already importable (installed, or on ``PYTHONPATH``);
2. ``NEUXMIND_SPEC_PATH`` / ``NEUXMIND_RUNTIME_PATH`` — the directory that
   *contains* the package;
3. the repository this SDK lives in (``<repo>/spec``, ``<repo>/runtime``),
   which is what ``pip install -e sdk/`` gives you.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType

from .errors import NeuxMindError

SDK_DIR = Path(__file__).resolve().parents[1]     # <repo>/sdk
REPO_ROOT = SDK_DIR.parent                          # <repo>


def _candidates(env_var: str, repo_subdir: str) -> list[Path]:
    paths = []
    override = os.environ.get(env_var)
    if override:
        paths.append(Path(override).expanduser())
    paths.append(REPO_ROOT / repo_subdir)
    return paths


def _find(package: str, env_var: str, repo_subdir: str) -> Path | None:
    for path in _candidates(env_var, repo_subdir):
        if (path / package / "__init__.py").is_file():
            return path
    return None


def _ensure(package: str, env_var: str, repo_subdir: str, code: str) -> ModuleType:
    try:
        return importlib.import_module(package)
    except ImportError:
        pass
    path = _find(package, env_var, repo_subdir)
    if path is None:
        raise NeuxMindError(
            f"找不到 {package}；请设置 {env_var} 指向包含 {package} 的目录（仓库中为 <repo>/{repo_subdir}）",
            code=code)
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return importlib.import_module(package)


def spec() -> ModuleType:
    """Return the ``neuxmind_spec`` package (capabilities, manifest)."""
    return _ensure("neuxmind_spec", "NEUXMIND_SPEC_PATH", "spec", "spec_not_found")


def runtime() -> ModuleType:
    """Return the ``neuxmind_runtime`` package (needed for local tests and ``nx sim``)."""
    spec()
    return _ensure("neuxmind_runtime", "NEUXMIND_RUNTIME_PATH", "runtime", "runtime_not_found")


def runtime_dir() -> Path:
    """Directory containing ``neuxmind_runtime`` (for launching it as a subprocess)."""
    try:
        module = importlib.import_module("neuxmind_runtime")
        return Path(module.__file__).resolve().parents[1]
    except ImportError:
        pass
    path = _find("neuxmind_runtime", "NEUXMIND_RUNTIME_PATH", "runtime")
    if path is None:
        raise NeuxMindError("找不到 neuxmind_runtime；请设置 NEUXMIND_RUNTIME_PATH", code="runtime_not_found")
    return path


def spec_dir() -> Path:
    module = spec()
    return Path(module.__file__).resolve().parents[1]


def try_runtime() -> ModuleType | None:
    """Like :func:`runtime` but returns ``None`` when the runtime is unavailable."""
    try:
        return runtime()
    except NeuxMindError:
        return None
