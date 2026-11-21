"""How far an upload got, kept on this computer so that it can be continued (issue #18).

One small JSON file per device and destination, in a folder per device. It holds the checksum
and size of the local file, the name of the temporary file on the device, and how much of it the
device has confirmed: no login, no content. A record stays until its transfer finishes, is
rejected or is given up, so what a transfer left on a device can always be found again
(``unfinished``). It is read back from disk, so it is checked like any outside data; one that
does not fit is treated as absent, and the transfer starts again.
"""
import hashlib
import json
import os
import re
from pathlib import Path

from .device_transfer_target import path_names, place_on_device, refuse, shown
from .service import now

RECORD_VERSION = 1
KEPT_BOUNDARIES = 64                    # chunk ends remembered for a compressed transfer
MAX_SHOWN = 1500                        # characters of a saved text handed back to a caller
_STATUSES = ('sending', 'interrupted', 'cancelled')
_ENCODINGS = ('gzip', 'identity')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_TEXTS = ('remote_path', 'sha256', 'encoding', 'path', 'part', 'status', 'error', 'updated_at')
_COUNTS = ('size', 'sent', 'wire')
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def _key(text, length):
    return hashlib.sha256(text.encode('utf-8', 'backslashreplace')).hexdigest()[:length]


def device_folder(record_dir, device_key):
    if not isinstance(device_key, str) or not 1 <= len(device_key) <= 200:
        raise refuse('device', '设备标识无效')
    try:
        return Path(record_dir) / _key(device_key, 16)
    except TypeError:
        raise refuse('records', '保存传输记录的目录无效') from None


def record_file(record_dir, device_key, remote_path):
    return device_folder(record_dir, device_key) / (_key(remote_path, 32) + '.json')


def new(source, remote_path, encoding, dest, part):
    return {'version': RECORD_VERSION, 'remote_path': remote_path, 'sha256': source['sha256'], 'size': source['size'],
            'encoding': encoding, 'path': dest, 'part': part, 'sent': 0, 'wire': 0, 'boundaries': [],
            'status': 'sending', 'error': '', 'updated_at': now()}


def advanced(record, sent, wire, boundaries, **changes):
    """A copy of ``record`` at a new position. ``boundaries``: [bytes of the file, bytes of the
    temporary file] at the end of each chunk, of which only the latest are kept."""
    return {**record, 'sent': sent, 'wire': wire, 'boundaries': boundaries[-KEPT_BOUNDARIES:], 'updated_at': now(), **changes}


def ended(record, status, message):
    """A copy of ``record`` that says how a transfer which can be continued came to a halt."""
    return {**record, 'status': status, 'error': message, 'updated_at': now()}


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _well_formed(record):
    if not isinstance(record, dict) or record.get('version') != RECORD_VERSION:
        return False
    ends = record.get('boundaries')
    if not all(isinstance(record.get(key), str) for key in _TEXTS) or not all(_count(record.get(key)) for key in _COUNTS):
        return False
    if not isinstance(ends, list) or not all(isinstance(end, list) and len(end) == 2 and all(_count(v) for v in end) for end in ends):
        return False
    if record['status'] not in _STATUSES or record['encoding'] not in _ENCODINGS or not _SHA256.fullmatch(record['sha256']):
        return False
    try:
        path_names(record['remote_path'])
    except ValueError:
        return False
    if not place_on_device(record['path']) or not place_on_device(record['part']):
        return False
    return all(a[0] < b[0] and a[1] < b[1] for a, b in zip(ends, ends[1:])) and record['sent'] <= record['size']


def load(path):
    """The saved record, or None: none, unreadable, or not what this version writes."""
    try:
        record = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None
    return record if _well_formed(record) else None


def save(path, record):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = path.with_suffix('.tmp')
    descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(record, handle, ensure_ascii=False)
    os.replace(scratch, path)


def forget(path):
    path.unlink(missing_ok=True)


def summary(record, running):
    """What a caller is told about an unfinished transfer. A record still marked as sending while
    nothing is sending was left by a process that ended: that transfer is interrupted."""
    status = 'interrupted' if record['status'] == 'sending' and not running else record['status']
    return {'remote_path': record['remote_path'], 'status': status, 'sent': record['sent'], 'total': record['size'],
            'sha256': record['sha256'], 'path': record['path'], 'remote_temp': record['part'],
            'error': shown(record['error'], MAX_SHOWN), 'updated_at': shown(record['updated_at'], 40)}


def of_device(record_dir, device_key):
    """(file, record) of every unfinished transfer to one device."""
    folder = device_folder(record_dir, device_key)
    if not folder.is_dir():
        return []
    loaded = ((entry, load(entry)) for entry in sorted(folder.glob('*.json')))
    return [(entry, record) for entry, record in loaded if record is not None]


def forget_device(record_dir, device_key):
    """Drop every record of one device, for when the device itself is removed from the product.
    Nothing is contacted: what its transfers left on the device stays there. Returns how many."""
    folder = device_folder(record_dir, device_key)
    if not folder.is_dir():
        return 0
    entries = [entry for entry in folder.iterdir() if entry.suffix in ('.json', '.tmp')]
    for entry in entries:
        entry.unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass        # something that is not ours is in it: leave the folder
    return sum(1 for entry in entries if entry.suffix == '.json')
