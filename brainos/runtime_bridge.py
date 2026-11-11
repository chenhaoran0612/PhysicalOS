"""Locate the edge runtime (``runtime/neuxmind_runtime``) and its bundled examples.

In the source tree the runtime lives next to ``brainos/``; in the packaged app
PyInstaller ships it as a regular package and the example skills as data under
the same root. ``NEUXMIND_RUNTIME_PATH`` (directory that *contains*
``neuxmind_runtime``) and ``NEUXMIND_EXAMPLES_DIR`` override the search.
"""
import os
import sys
from pathlib import Path

from . import spec_bridge  # noqa: F401  (spec first: the runtime imports it)

ROOT = Path(__file__).resolve().parents[1]


def _ensure_importable():
    try:
        import neuxmind_runtime  # noqa: F401
        return
    except ImportError:
        pass
    candidates = []
    if os.environ.get('NEUXMIND_RUNTIME_PATH'):
        candidates.append(Path(os.environ['NEUXMIND_RUNTIME_PATH']))
    candidates.append(ROOT / 'runtime')
    for path in candidates:
        if (path / 'neuxmind_runtime' / '__init__.py').is_file():
            sys.path.insert(0, str(path))
            return


_ensure_importable()


def examples_dir():
    """Real path (no symbolic links) of the bundled example skills, or None."""
    override = os.environ.get('NEUXMIND_EXAMPLES_DIR')
    for candidate in ([Path(override)] if override else []) + [ROOT / 'runtime' / 'examples' / 'skills']:
        if candidate.is_dir():
            return Path(os.path.realpath(candidate))
    return None


def child_command(args):
    """argv that starts ``neuxmind_runtime serve`` (via :mod:`brainos.sim_child`) as a child process."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--nx-sim-child', *args]
    return [sys.executable, '-u', '-m', 'brainos.sim_child', *args]


def child_env():
    env = dict(os.environ)
    if not getattr(sys, 'frozen', False):
        paths = [str(ROOT / 'runtime'), str(ROOT / 'spec'), str(ROOT)]
        if env.get('PYTHONPATH'):
            paths.append(env['PYTHONPATH'])
        env['PYTHONPATH'] = os.pathsep.join(paths)
    env['PYTHONUNBUFFERED'] = '1'
    env['NO_PROXY'] = '127.0.0.1,localhost'
    return env


def sandbox_mode():
    """Skill isolation for simulated robots.

    Separate skill processes need Linux system calls and a Python interpreter;
    the packaged macOS app has neither, so skills run inside the simulated
    robot's own process there (that process is already separate from the app).
    """
    if sys.platform.startswith('linux') and not getattr(sys, 'frozen', False):
        return 'process'
    return 'inprocess'
