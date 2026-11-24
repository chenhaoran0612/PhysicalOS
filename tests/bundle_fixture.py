"""A stand-in for a robot whose programs and scene data are collected and installed (issue #43).
Test only; nothing here reaches a real machine.

``BundleDevice`` is the upload fixture of issue #18 (SFTP into a private temporary directory, the
transfer scripts run for real) plus the two scripts of ``brainos.robot_bundle_device``. It runs one
of them only if the script on standard input is the product's own text, byte for byte, and every
argument has the shape that script takes; then it runs for real with ``sh``, with ``HOME`` moved
into the temporary directory and only the tools of ``TOOLS`` on ``PATH``. A directory given as an
absolute path is moved under the temporary directory first and refused if it could lead out of it.

Reading a file over SFTP can be made to fail: ``read_cuts`` (cut the connection once that many
bytes were read, for each entry) and ``corrupt_read_at`` (hand out one byte wrong, once).
``refuse_next`` connections are closed without an answer (a robot that is unreachable for a moment).
"""
import os
import re
import shutil
import subprocess

import paramiko

import upload_fixture
from brainos import robot_bundle_device as on_device
from upload_fixture import SHELL, UploadDevice

TOOLS = ('tar', 'find', 'head', 'grep', 'sort', 'tr', 'mkdir', 'ln', 'readlink', 'dirname', 'sh', 'diff', 'date')
SCRIPT_SECONDS = 60
_HEX = re.compile(r'(?:[0-9a-f]{2})+')
_NUMBER = re.compile(r'[0-9]{1,12}')
SHAPES = {'pack': (_HEX, on_device.TOKEN, _NUMBER), 'extract': (on_device.NAME, on_device.TOKEN, _NUMBER)}
_SCRIPTS = {text: kind for kind, text in on_device.SCRIPTS.items()}


class _ReadHandle(upload_fixture._Handle):
    """The upload fixture's file handle; a device that wants to disturb reads says so with ``reading``."""

    def read(self, offset, length):
        data = super().read(offset, length)
        disturb = getattr(self.owner, 'reading', None)
        return disturb(self, offset, data) if disturb else data


upload_fixture._Handle = _ReadHandle        # the same handle for every device; only a BundleDevice has ``reading``


class BundleDevice(UploadDevice):
    refuse_next, _offline = 0, False

    def __init__(self, **options):
        super().__init__(**options)
        for tool in TOOLS:
            os.symlink(shutil.which(tool), os.path.join(self.bin, tool))
        self.bundle_scripts = []            # (kind, arguments) of every bundle script that ran
        self.before_bundle_script = None
        self.read_cuts, self.corrupt_read_at, self.read_bytes = [], None, 0

    @property
    def offline(self):
        """As in the upload fixture, and besides: the next ``refuse_next`` connections are not answered."""
        if self.refuse_next > 0:
            self.refuse_next -= 1
            return True
        return self._offline

    @offline.setter
    def offline(self, value):
        self._offline = value

    def reading(self, handle, offset, data):
        with self.lock:
            self.read_bytes += len(data)
            cut = bool(self.read_cuts) and self.read_bytes >= self.read_cuts[0]
            if cut:
                self.read_cuts.pop(0)
            if self.corrupt_read_at is not None and offset <= self.corrupt_read_at < offset + len(data):
                changed = bytearray(data)
                changed[self.corrupt_read_at - offset] ^= 0xff
                data, self.corrupt_read_at = bytes(changed), None
        if cut:
            self.cut(handle.transport)
            return paramiko.SFTP_FAILURE
        return data

    def _moved(self, kind, arguments):
        """The arguments with an absolute directory moved under the fixture, or None to refuse."""
        if len(arguments) != len(SHAPES[kind]) or not all(shape.fullmatch(word) for shape, word in zip(SHAPES[kind], arguments)):
            return None
        if kind != 'pack':
            return arguments
        directory = bytes.fromhex(arguments[0]).decode('utf-8', 'replace')
        if not directory.startswith('/'):
            return arguments
        local = os.path.normpath(os.path.join(self.root, directory.lstrip('/')))
        real = os.path.realpath(local)
        if real != self.root and not real.startswith(self.root + os.sep):
            return None
        return [on_device.hex_argument(local), *arguments[1:]]

    def handle(self, command, script):
        kind = _SCRIPTS.get(script.decode('utf-8', 'replace'))
        if kind is None:
            return super().handle(command, script)
        words = command.split(' ')
        moved = self._moved(kind, words[3:]) if words[:3] == ['sh', '-s', '--'] else None
        if moved is None:
            self.refused.append(command)
            return 126, b'', b'fixture: not a bundle script on its own files\n'
        self.bundle_scripts.append((kind, words[3:]))
        if self.before_bundle_script:
            self.before_bundle_script(kind, words[3:])
        done = subprocess.run([*SHELL, '-s', '--', *moved], input=script, capture_output=True,
                              env={'PATH': self.bin, 'HOME': self.local('')}, cwd=self.root, timeout=SCRIPT_SECONDS)
        return done.returncode, self.banner + done.stdout, done.stderr
