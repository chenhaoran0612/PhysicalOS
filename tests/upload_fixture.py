"""A stand-in for a device that accepts uploads (issue #18). Nothing here reaches a real machine.

SFTP is served from a private temporary directory, behind a login made up for each instance.

It runs a command only if all of this holds, and refuses it otherwise (noted in ``refused``):
the command line is ``sh -s --`` plus arguments, the script on standard input is one of the
product's own (``device_transfer.SCRIPTS``, compared byte for byte), and every argument fits what
that script takes: an absolute path of plain names that stays inside the temporary directory even
after links are followed, a number, a checksum, an encoding or a mode. The script then runs with
its paths moved under the temporary directory and only the tools a test allows on PATH.

Faults a test can switch on: ``drops`` (cut the connection once that many bytes arrived),
``offline`` (answer no new connection), ``stall_after`` (stop answering a write), ``full_after``
(refuse writes, as a full disk does), ``corrupt_at`` (store one byte wrong), ``lose_answer`` (run a
script, then cut the connection instead of answering), ``sftp = False`` (an SSH service without
SFTP), ``hide_modes`` (an SFTP service that does not say what kind of thing a path is), ``banner``
(text a login script prints before every answer), ``break_unpacking()`` (no room to unpack).
"""
import os
import posixpath
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading

import paramiko

from brainos import device_access, device_transfer

HOST_KEY = paramiko.ECDSAKey.generate()
BASE_TOOLS = ('rm', 'mv', 'chmod', 'wc', 'cut', 'cat')
# With NX_UPLOAD_FIXTURE_BUSYBOX=/path/to/busybox the device's shell and every tool are that
# busybox (what many small boards have), instead of this computer's sh and tools.
BUSYBOX = os.environ.get('NX_UPLOAD_FIXTURE_BUSYBOX')
SHELL = [BUSYBOX, 'sh'] if BUSYBOX else ['/bin/sh']
HASH_TOOLS = ('sha256sum',) if BUSYBOX else tuple(tool for tool in ('sha256sum', 'shasum', 'openssl') if shutil.which(tool))
SCRIPT_SECONDS = 30
MAX_SCRIPT_BYTES = 65536
PATH = re.compile(r'(?:/[A-Za-z0-9_+.@-]+)+')
ARGUMENTS = {'probe': (PATH, re.compile(r'[0-9]{1,12}'), PATH),
             'finalize': (PATH, PATH, re.compile(r'[0-9a-f]{64}'), re.compile(r'gzip|identity'), re.compile(r'[0-7]{1,3}'), PATH),
             'clean': (PATH,)}
_NETWORK_ERRORS = (OSError, EOFError, paramiko.SSHException)


class _Handle(paramiko.SFTPHandle):
    def __init__(self, owner, transport, number, path, fd, flags):
        super().__init__(flags)
        self.owner, self.transport, self.number, self.path, self.fd = owner, transport, number, path, fd

    def read(self, offset, length):
        return os.pread(self.fd, length, offset)

    def write(self, offset, data):
        return self.owner.wrote(self, offset, data)

    def stat(self):
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.fd))

    def chattr(self, attr):
        if attr.st_size is not None:
            os.ftruncate(self.fd, attr.st_size)
        if attr.st_mode is not None:
            os.fchmod(self.fd, attr.st_mode)
        return paramiko.SFTP_OK

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class UploadDevice:
    def __init__(self, tools=None, home='/home/robot'):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix='nx-upload-device-'))
        self.root, self.bin, self.home = os.path.join(self.base, 'fs'), os.path.join(self.base, 'bin'), home
        os.makedirs(self.local(home))
        os.makedirs(self.bin)
        for tool in (*BASE_TOOLS, *(('gzip', HASH_TOOLS[0]) if tools is None else tools)):
            os.symlink(BUSYBOX or shutil.which(tool), os.path.join(self.bin, tool))
        self.username, self.password = 'robot', secrets.token_hex(16)
        self.key = HOST_KEY
        self.offline, self.offline_after_drop, self.sftp, self.hide_modes = False, False, True, False
        self.drops, self.stall_after, self.stall_seconds = [], None, 20
        self.full_after, self.corrupt_at, self.banner = None, None, b''
        self.lose_answer, self.before_script = set(), None
        self.received, self.connections = 0, 0
        self.writes, self.scripts, self.refused = [], [], []     # (connection, path, offset, length); (kind, arguments); commands
        self.lock, self.released, self.closed, self.transports = threading.Lock(), threading.Event(), False, []
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.socket.settimeout(.2)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    # --- what a test reads and prepares -------------------------------------------------------

    def login(self):
        """(connection, saved logins) as the store hands them to ``SSHDevice``."""
        return ({'host': '127.0.0.1', 'port': self.port, 'username': self.username, 'auth': 'password',
                 'fingerprint': device_access.fingerprint(self.key), 'read_roots': ['/opt/ros']}, {'password': self.password})

    def local(self, path):
        real = os.path.normpath(os.path.join(self.root, posixpath.join(self.home, path).lstrip('/')))
        if real != self.root and not real.startswith(self.root + os.sep):
            raise ValueError('outside the fixture: ' + path)
        return real

    def read(self, path):
        try:
            with open(self.local(path), 'rb') as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def write(self, path, data):
        os.makedirs(os.path.dirname(self.local(path)), exist_ok=True)
        with open(self.local(path), 'wb') as handle:
            handle.write(data)

    def names(self, directory):
        try:
            return sorted(os.listdir(self.local(directory)))
        except FileNotFoundError:
            return []

    def mode(self, path):
        return stat.S_IMODE(os.stat(self.local(path)).st_mode)

    def link(self, path, target):
        """A symbolic link on the device; ``target`` is a device path too (made a directory if nothing is there)."""
        if not os.path.lexists(self.local(target)):
            os.makedirs(self.local(target))
        os.makedirs(os.path.dirname(self.local(path)), exist_ok=True)
        os.symlink(self.local(target), self.local(path))

    def break_unpacking(self):
        """From now on gzip can test a stream but not unpack it: a disk with no room for the result.
        Returns a function that makes room again."""
        tool = os.path.join(self.bin, 'gzip')
        linked = os.readlink(tool)
        real = (BUSYBOX + ' gzip') if BUSYBOX else os.path.realpath(tool)
        os.remove(tool)
        with open(tool, 'w') as handle:
            handle.write('#!/bin/sh\ncase "$1" in -t) exec %s -t;; esac\n'
                         'echo "gzip: write error: No space left on device" >&2\nexit 1\n' % real)
        os.chmod(tool, 0o755)

        def mend():
            os.remove(tool)
            os.symlink(linked, tool)
        return mend

    def close(self):
        self.closed = True
        self.released.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(timeout=2)
        shutil.rmtree(self.base, ignore_errors=True)

    # --- the device side ----------------------------------------------------------------------

    def canonical(self, path):
        real = os.path.realpath(self.local(path))
        if real == self.root:
            return '/'
        return '/' + os.path.relpath(real, self.root).replace(os.sep, '/') if real.startswith(self.root + os.sep) else posixpath.normpath(path)

    def wrote(self, handle, offset, data):
        with self.lock:
            if self.full_after is not None and self.received + len(data) > self.full_after:
                return paramiko.SFTP_FAILURE
            stalled = self.stall_after is not None and self.received >= self.stall_after
            if stalled:
                self.stall_after = None
        if stalled:
            self.released.wait(self.stall_seconds)      # no answer: the client has to give up on its own
            return paramiko.SFTP_FAILURE
        with self.lock:
            stored = bytearray(data)
            if self.corrupt_at is not None and offset <= self.corrupt_at < offset + len(data):
                stored[self.corrupt_at - offset] ^= 0xff
                self.corrupt_at = None
            os.pwrite(handle.fd, bytes(stored), offset)
            self.received += len(data)
            self.writes.append((handle.number, handle.path, offset, len(data)))
            cut = bool(self.drops) and self.received >= self.drops[0]
            if cut:
                self.drops.pop(0)
                self.offline = self.offline or self.offline_after_drop
        if cut:
            handle.transport.close()
            return paramiko.SFTP_FAILURE
        return paramiko.SFTP_OK

    def confined(self, path):
        """Where a device path lies in the temporary directory, or None if it could lead out of it."""
        if not PATH.fullmatch(path) or any(name in ('.', '..') for name in path.split('/')):
            return None
        local = self.local(path)
        for place in (os.path.dirname(local), local):
            real = os.path.realpath(place)
            if real != self.root and not real.startswith(self.root + os.sep):
                return None
        return local

    def accepted(self, command, script):
        """(kind, device arguments, local arguments) of a command this fixture is willing to run, else None."""
        words = command.split(' ')
        kind = next((name for name, text in device_transfer.SCRIPTS.items() if text == script), None)
        if kind is None or words[:3] != ['sh', '-s', '--'] or len(words) - 3 != len(ARGUMENTS[kind]):
            return None
        arguments, local = words[3:], []
        for word, shape in zip(arguments, ARGUMENTS[kind]):
            moved = self.confined(word) if shape is PATH else word if shape.fullmatch(word) else None
            if moved is None:
                return None
            local.append(moved)
        return kind, arguments, local

    # --- issue #19 (tests/install_fixture.py): a subclass answers more commands and forwards channels ---

    def handle_early(self, command):
        """(exit status, stdout, stderr) of a command answered before its standard input is read, or None."""
        return None

    def handle(self, command, script):
        """(exit status, stdout, stderr) of a command with its standard input, or None for the transfer scripts."""
        return None

    def forward(self, chanid, destination):
        """The answer to a direct-tcpip request; this fixture forwards nothing."""
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def forwarded(self, channel):
        """A channel ``forward`` accepted has arrived."""

    def _answer(self, channel, code, out, err):
        try:
            channel.sendall(out)
            channel.sendall_stderr(err)
            channel.send_exit_status(code)
        except _NETWORK_ERRORS:
            pass
        finally:
            channel.close()

    def run(self, transport, channel, command):
        early = self.handle_early(command)
        if early is not None:
            return self._answer(channel, *early)
        channel.settimeout(5)
        script = bytearray()
        try:
            while len(script) <= MAX_SCRIPT_BYTES:
                block = channel.recv(8192)
                if not block:
                    break
                script.extend(block)
        except (socket.timeout, *_NETWORK_ERRORS):
            pass
        handled = self.handle(command, bytes(script))
        if handled is not None:
            return self._answer(channel, *handled)
        accepted = self.accepted(command, script.decode('utf-8', 'replace'))
        if accepted is None:
            self.refused.append(command)
            code, out, err = 126, b'', b'fixture: not one of the transfer scripts, or not on its own files\n'
        else:
            kind, arguments, local = accepted
            self.scripts.append((kind, arguments))
            if self.before_script:
                self.before_script(kind, arguments)
            done = subprocess.run([*SHELL, '-s', '--', *local], input=bytes(script), capture_output=True,
                                  env={'PATH': self.bin}, cwd=self.root, timeout=SCRIPT_SECONDS)
            code, out, err = done.returncode, self.banner + done.stdout, done.stderr
            if kind in self.lose_answer:
                self.lose_answer.discard(kind)
                transport.close()
                return
        try:
            channel.sendall(out)
            channel.sendall_stderr(err)
            channel.send_exit_status(code)
        except _NETWORK_ERRORS:
            pass
        finally:
            channel.close()

    def _accept(self):
        while not self.closed:
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.offline:
                client.close()
                continue
            self.connections += 1
            threading.Thread(target=self._serve, args=(client, self.connections), daemon=True).start()

    def _serve(self, client, number):
        owner = self
        transport = paramiko.Transport(client)

        class Protocol(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                return paramiko.AUTH_SUCCESSFUL if (username, password) == (owner.username, owner.password) else paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return 'password'

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_direct_tcpip_request(self, chanid, origin, destination):
                return owner.forward(chanid, tuple(destination))

            def check_channel_exec_request(self, channel, command):
                threading.Thread(target=owner.run, args=(transport, channel, command.decode('utf-8', 'replace')), daemon=True).start()
                return True

        class Files(paramiko.SFTPServerInterface):
            def canonicalize(self, path):
                return owner.canonical(path)

            def _attributes(self, path, read):
                try:
                    attributes = paramiko.SFTPAttributes.from_stat(read(owner.local(path)))
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno)
                if owner.hide_modes:
                    attributes.st_mode = None
                return attributes

            def stat(self, path):
                return self._attributes(path, os.stat)

            def lstat(self, path):
                return self._attributes(path, os.lstat)

            def _change(self, action, path, *arguments):
                try:
                    action(owner.local(path), *arguments)
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno)
                return paramiko.SFTP_OK

            def mkdir(self, path, attr):
                return self._change(os.mkdir, path, 0o755 if attr.st_mode is None else attr.st_mode)

            def remove(self, path):
                return self._change(os.remove, path)

            def chattr(self, path, attr):
                return self._change(os.chmod, path, attr.st_mode) if attr.st_mode is not None else paramiko.SFTP_OK

            def open(self, path, flags, attr):
                try:
                    fd = os.open(owner.local(path), flags, 0o644)
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno)
                return _Handle(owner, transport, number, owner.canonical(path), fd, flags)

        self.transports.append(transport)
        transport.add_server_key(self.key)
        if self.sftp:
            transport.set_subsystem_handler('sftp', paramiko.SFTPServer, Files)
        channels = []       # paramiko keeps only weak references: a channel nobody holds closes itself
        try:
            transport.start_server(server=Protocol())
            while not self.closed and transport.is_active():
                channels.append(transport.accept(.2))
                if channels[-1] is not None:
                    self.forwarded(channels[-1])
        except _NETWORK_ERRORS:
            pass
        finally:
            transport.close()


class FlakyLink:
    """A relay in front of a device that cuts each connection once it carried a set number of bytes
    towards the device. TCP hides lost packets from a program: what a weak link leaves is waiting and
    broken connections, and the second of those is what this stands in for."""

    def __init__(self, port, cuts):
        self.upstream, self.cuts, self.closed, self.sockets = port, list(cuts), False, []
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.socket.settimeout(.2)
        self.port = self.socket.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            budget = self.cuts.pop(0) if self.cuts else None
            try:
                device = socket.create_connection(('127.0.0.1', self.upstream), timeout=5)
            except OSError:
                client.close()
                continue
            device.settimeout(None)
            self.sockets += [client, device]
            threading.Thread(target=self._copy, args=(client, device, budget), daemon=True).start()
            threading.Thread(target=self._copy, args=(device, client, None), daemon=True).start()

    @staticmethod
    def _copy(source, sink, budget):
        try:
            while budget is None or budget > 0:
                data = source.recv(4096 if budget is None else min(4096, budget))
                if not data:
                    break
                sink.sendall(data)
                budget = None if budget is None else budget - len(data)
        except OSError:
            pass
        finally:
            for end in (source, sink):
                try:
                    end.close()
                except OSError:
                    pass

    def close(self):
        self.closed = True
        self.socket.close()
        for end in self.sockets:
            try:
                end.close()
            except OSError:
                pass
