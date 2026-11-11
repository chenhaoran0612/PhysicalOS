"""Import the shared NeuxMind spec (``spec/neuxmind_spec``).

The spec is the single authority for capability names, kinds, safety levels,
skill packages and compatibility. It is normally installed (``pip install -e
spec``); inside this repository it is also found next to ``brainos/``.
``NEUXMIND_SPEC_PATH`` overrides the search (the directory that *contains*
``neuxmind_spec``).
"""
import os
import sys
from pathlib import Path


def _ensure_importable():
    try:
        import neuxmind_spec  # noqa: F401
        return
    except ImportError:
        pass
    candidates = []
    if os.environ.get('NEUXMIND_SPEC_PATH'):
        candidates.append(Path(os.environ['NEUXMIND_SPEC_PATH']))
    candidates.append(Path(__file__).resolve().parents[1] / 'spec')
    for path in candidates:
        if (path / 'neuxmind_spec' / '__init__.py').is_file():
            sys.path.insert(0, str(path))
            return


_ensure_importable()

from neuxmind_spec import capabilities, manifest  # noqa: E402

__all__ = ['capabilities', 'manifest']
