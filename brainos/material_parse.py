"""Parsing results of library materials (issue #34): the desktop's side of the cloud's parse API.

The page shows what the cloud parsed out of a material, lets a person change it
and confirm it. As with the rest of the library (material_library.py), the
page never talks to the cloud: this module adds the session token, checks the
material id before it goes into a URL, and passes on only plain JSON values
from the cloud's answer, with bounded depth, length and count.
"""
import math

from .material_library import _material_id

MAX_DEPTH, MAX_TEXT, MAX_ITEMS = 6, 5000, 5000
VIEW_KEYS = ('material', 'kind', 'fields', 'note', 'stage', 'status', 'status_detail', 'values', 'not_applicable',
             'missing', 'warnings', 'error', 'retrying', 'parser', 'revision', 'edited_by', 'edited_at', 'confirmed_by',
             'confirmed_at', 'editable')


def plain(value, depth=0):
    """``value`` with only JSON's plain types left (strings cut, lists and objects bounded)."""
    if depth > MAX_DEPTH:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, list):
        return [plain(item, depth + 1) for item in value[:MAX_ITEMS]]
    if isinstance(value, dict):
        return {str(k)[:200]: plain(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS] if isinstance(k, str)}
    return None


def _view(result):
    if not isinstance(result, dict):
        return {}
    return {key: plain(result.get(key)) for key in VIEW_KEYS if key in result}


class MaterialParse:
    def __init__(self, library):
        self.library = library

    def _path(self, p, tail=''):
        return f"/library/materials/{_material_id((p or {}).get('material_id'))}/parse{tail}"

    @staticmethod
    def _edit_body(p):
        revision = p.get('revision')
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError('revision 须为整数（解析结果的版本）')
        body = {'revision': revision, 'values': plain(p.get('values') if isinstance(p.get('values'), dict) else {})}
        if p.get('not_applicable') is not None:
            body['not_applicable'] = [k for k in p['not_applicable'] if isinstance(k, str)][:100] \
                if isinstance(p['not_applicable'], list) else []
        return body

    def get(self, p):
        return _view(self.library._call('GET', self._path(p)))

    def save(self, p):
        return _view(self.library._call('PUT', self._path(p), self._edit_body(p)))

    def confirm(self, p):
        result = _view(self.library._call('POST', self._path(p, '/confirm'), self._edit_body(p)))
        self.library.platform.audit('library.parse_confirmed', {'id': (result.get('material') or {}).get('id'),
                                                                'revision': result.get('revision')})
        return result

    def retry(self, p):
        return _view(self.library._call('POST', self._path(p, '/retry'), {}))
