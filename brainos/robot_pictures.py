"""Issue #53: the pictures of robot models the console shows on a robot's card.

They are files in ``web/robot_images/`` (``SOURCES.md`` there says where each is from);
``web/robot_catalog.js`` names them by brand and model. Only files of that directory are served, by a
name of lower-case letters, digits and hyphens: a request can never name a path.
"""
import re
from pathlib import Path

PREFIX = '/robot_images/'
DIRECTORY = 'robot_images'
NAME = re.compile(r'[a-z0-9][a-z0-9\-]{0,79}\.(webp|svg)')
MIME = {'webp': 'image/webp', 'svg': 'image/svg+xml'}
ROOT = Path(__file__).resolve().parents[1] / 'web'


def static(path, root=ROOT):
    """``(file name under web/, MIME type)`` for the request path of a picture that exists, else None."""
    if not isinstance(path, str) or not path.startswith(PREFIX):
        return None
    match = NAME.fullmatch(path[len(PREFIX):])
    if not match or not (root / DIRECTORY / match.group(0)).is_file():
        return None
    return f'{DIRECTORY}/{match.group(0)}', MIME[match.group(1)]
