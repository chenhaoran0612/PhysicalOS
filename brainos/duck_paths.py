"""Where the Microduck kit is on this computer (issue #63). Nothing else: both the console and the
simulated body's own process ask here, so neither stores a path that moves with the app."""
import os
import sys
from pathlib import Path

KIT_ENV = 'NEUXMIND_DUCK_KIT_DIR'
PACKAGED_DIR = 'duck_kit'
ENGINE_DIR = 'engine'


def kit_dir():
    """The folder with ``kit.json``: named by the environment, inside the packaged app, or the build folder."""
    named = os.environ.get(KIT_ENV)
    if named:
        return Path(named)
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) / PACKAGED_DIR
    return Path(__file__).resolve().parents[1] / '.build' / 'duck-kit'


def engine_dir(kit=None):
    """The folder with the physics engine the show packages were accepted on."""
    return (Path(kit) if kit else kit_dir()) / ENGINE_DIR
