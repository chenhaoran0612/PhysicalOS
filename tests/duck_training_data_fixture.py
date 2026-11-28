"""Desktop backend on a temporary state directory for the browser check of issue #79 (a duck model's
training and acceptance records, the recordings of its accepted episodes, its versions).

By default the kit is made up (``test_duck_training_data.made_shows``: one model with records and two
recordings, an older version, a model that is no longer delivered), so nothing outside the repository is
needed; its videos are a few bytes, not playable. With ``NX_DUCK_REAL_KIT=1`` the kit is the one
``scripts/duck_kit.py build`` made (``NEUXMIND_DUCK_KIT_DIR`` or ``.build/duck-kit``): the delivered
packages, their records and the 120 real recordings.

Usage: python3 tests/duck_training_data_fixture.py [config.json]
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TRAINING = {'required_successes': 10, 'generated_at': 1795770000.0, 'rounds': [{}] * 9, 'source': {'last_update': 1795769000.0},
            'tasks': [{'id': 'nod', 'title': '点头', 'active': True, 'qualified': True,
                       'best': {'round_number': 1, 'iteration': 2, 'successes': 10, 'episodes': 10, 'total_steps': 64, 'training_backend': 'cuda'},
                       'current': {'round_number': 9, 'iteration': 1, 'successes': 10, 'episodes': 10, 'total_steps': 4096, 'training_backend': 'cpu'},
                       'release': {'round_number': 9, 'iteration': 1, 'training_device': 'Mac CPU', 'fixed': {'successes': 10, 'episodes': 10},
                                   'confirmation': {'successes': 10, 'episodes': 10, 'seeds': [2_000_000_000]}, 'zero': {'successes': 9, 'episodes': 10},
                                   'duration': {'qualified': True, 'fixed': {'min_s': 18.0, 'max_s': 18.09}, 'confirmation': {'min_s': 18.5, 'max_s': 18.5}}},
                       'rounds': [{'number': n, 'state': 'completed', 'iteration': u, 'total_steps': u * 2048, 'accelerator': 'CPU'} for n, u in ((1, 2), (4, 6), (9, 1))],
                       'events': [{'round_number': r, 'iteration': i, 'successes': s, 'episodes': 10, 'report_mtime': 1795760000.0 + n}
                                  for n, (r, i, s) in enumerate([(1, 1, 3), (1, 2, 10), (4, 6, 8), (9, 1, 10)])]}]}


def main():
    keep = tempfile.TemporaryDirectory(prefix='nx-duck-records-')
    if os.environ.get('NX_DUCK_REAL_KIT') != '1':
        import mujoco
        import test_duck_training_data as made
        from brainos import duck_kit
        kit, shows, recordings = made.made_shows(Path(keep.name) / 'kit', str(mujoco.__version__))
        (kit / 'training.json').write_text(json.dumps(duck_kit.trim_training(TRAINING)), encoding='utf-8')
        duck_kit.SHOWS, duck_kit.PACKAGED, duck_kit.BY_ID = shows, shows[:1], {s['id']: s for s in shows}
        duck_kit.RECORDINGS = {**duck_kit.RECORDINGS, 'sha256': recordings}
        os.environ['NEUXMIND_DUCK_KIT_DIR'] = str(kit)
    else:
        os.environ.setdefault('NEUXMIND_DUCK_KIT_DIR', str(Path(__file__).resolve().parents[1] / '.build' / 'duck-kit'))
    os.environ.setdefault('NEUXMIND_DUCK_DASHBOARD', 'http://127.0.0.1:9')       # nothing listens there: the kit's copy is shown
    import ux01_browser_fixture
    try:
        ux01_browser_fixture.main()
    finally:
        keep.cleanup()


if __name__ == '__main__':
    main()
