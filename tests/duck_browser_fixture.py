"""Desktop backend on a temporary state directory for the Microduck browser check (issue #63).

By default the kit is made up (``duck_support``: a box, shows that count steps), so nothing that is not
in the repository is needed. With ``NX_DUCK_REAL_KIT=1`` the kit is the one ``scripts/duck_kit.py build``
made under ``.build/duck-kit`` (the delivered packages on the engine they were accepted on).

Usage: python3 tests/duck_browser_fixture.py [config.json]
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TRAINING = {'required_successes': 10, 'generated_at': 1795770000.0, 'rounds': [{}] * 12,
            'tasks': [{'id': 'nod', 'title': '节目 nod', 'active': True, 'qualified': True,
                       'best': {'round_number': 3, 'iteration': 8, 'successes': 10, 'episodes': 10, 'total_steps': 4096, 'training_backend': 'cpu'},
                       'current': {'round_number': 4, 'iteration': 4, 'successes': 7, 'episodes': 10, 'total_steps': 2048, 'training_backend': 'cuda'},
                       'events': [{'round_number': r, 'iteration': i, 'successes': s, 'episodes': 10, 'report_mtime': 1795760000.0 + n}
                                  for n, (r, i, s) in enumerate([(1, 4, 0), (2, 4, 3), (3, 4, 6), (3, 8, 10), (4, 4, 7)])]}],
            'source': {'last_update': 1795769000.0}}


def main():
    keep = tempfile.TemporaryDirectory(prefix='nx-duck-kit-')
    if os.environ.get('NX_DUCK_REAL_KIT') != '1':
        import mujoco
        import duck_support
        from brainos import duck_kit
        kit, shows = duck_support.kit(Path(keep.name) / 'kit', ('nod', 'long', 'fall'), str(mujoco.__version__))
        (kit / 'training.json').write_text(json.dumps(duck_kit.trim_training(TRAINING)), encoding='utf-8')
        duck_kit.SHOWS, duck_kit.PACKAGED, duck_kit.BY_ID = shows, shows, {s['id']: s for s in shows}
        os.environ['NEUXMIND_DUCK_KIT_DIR'] = str(kit)
        os.environ.setdefault('NEUXMIND_DUCK_DASHBOARD', 'http://127.0.0.1:9')       # nothing listens there
    import ux01_browser_fixture
    try:
        ux01_browser_fixture.main()
    finally:
        keep.cleanup()


if __name__ == '__main__':
    main()
