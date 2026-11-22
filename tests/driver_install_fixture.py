"""Stand-ins for issue #29: a robot whose motion driver is missing, and a library that lists packages.

``DriverDevice`` is ``upload_fixture.UploadDevice`` (SFTP into a private temporary directory, the
transfer scripts of issue #18 and nothing else run for real) plus a robot that is read and repaired
from the text of the commands alone. It answers the product's fixed read-only commands from
``outputs``; its ROS 2 listing lacks the chassis's ``/cmd_vel`` and ``/odom`` until its driver runs.
Three repair steps are played, never run: installing the package that was delivered to it
(``apt-get install`` of that file), starting the driver's service, and a checksum read of a file.
Anything else is refused and noted in ``refused``; every command is kept in ``commands``. Its own
e-stop stays latched unless a command reaches the runtime, which none of the played steps does.

``ListingLibrary`` is ``package_cloud_fixture.FakeLibraryCloud`` with the listing route of the
library (``GET /api/v1/library/materials``, filtered by kind and status as the cloud does).

Names, packages and services are made up. Nothing here reaches a real machine or server.
"""
import hashlib
import posixpath
import socket
import urllib.parse

from brainos import hardware_scan, heartbeat, ros2_profile
from hardware_fixture import jetson
from motion_drivers_fixture import chassis
from package_cloud_fixture import FakeLibraryCloud
from upload_fixture import UploadDevice

UNIT = 'vendor-motion-driver'
NETWORK_ERRORS = (OSError, EOFError)


class DriverDevice(UploadDevice):
    def __init__(self):
        super().__init__()
        self.outputs = {hardware_scan.SCAN_EXEC: jetson().encode(), heartbeat.HEARTBEAT_COMMAND: b''}
        self.without_driver, self.with_driver = chassis('/cmd_vel', '/odom'), chassis()
        self.commands = []
        self.installed = self.running = False
        self.latched = True             # the body's own e-stop: nothing the played steps do releases it
        self.answer_others = None       # bytes: answer any other command with them (the console's own probes), else refuse it

    def ros(self):
        return (self.with_driver if self.running else self.without_driver).encode()

    def _answer(self, channel, code, out=b'', err=b''):
        try:
            channel.sendall(out)
            channel.sendall_stderr(err)
            channel.send_exit_status(code)
        except NETWORK_ERRORS:
            pass
        finally:
            channel.close()

    def _delivered(self, command):
        """The path of a package under ``~/neuxmind-pkg/library`` that the command names and that is on the device."""
        library = posixpath.join(self.home, 'neuxmind-pkg/library/')
        at = command.find(library)
        if at < 0:
            return None
        path = command[at:].split("'")[0].split()[0]
        return path if self.read(path) is not None else None

    def run(self, transport, channel, command):
        if command.startswith('sh -s -- '):
            return super().run(transport, channel, command)       # issue #18's transfer scripts
        with self.lock:
            self.commands.append(command)
        # Answer only after the client is done sending (it shuts its side down once the request was
        # accepted): an answer that comes first closes the channel under a request still waiting.
        channel.settimeout(3)
        try:
            while channel.recv(8192):
                pass
        except (socket.timeout, *NETWORK_ERRORS):
            pass
        if command in self.outputs:
            return self._answer(channel, 0, self.outputs[command])
        if command == ros2_profile.ROS2_EXEC:
            return self._answer(channel, 0, self.ros())
        if 'apt-get install -y' in command and 'neuxmind-pkg/library/' in command:
            path = self._delivered(command)
            if path is None:
                return self._answer(channel, 100, err=b'E: Unsupported file given on commandline\n')
            self.installed = True
            return self._answer(channel, 0, b'Setting up ros-humble-vendor-motion-driver (1.0) ...\n')
        if 'systemctl start ' + UNIT in command:
            if not self.installed:
                return self._answer(channel, 5, err=('Failed to start %s.service: Unit %s.service not found.\n' % (UNIT, UNIT)).encode())
            self.running = True
            return self._answer(channel, 0)
        if 'sha256sum ' in command and self._delivered(command):
            path = self._delivered(command)
            return self._answer(channel, 0, ('%s  %s\n' % (hashlib.sha256(self.read(path)).hexdigest(), path)).encode())
        with self.lock:
            self.refused.append(command)
        if self.answer_others is not None:
            return self._answer(channel, 0, self.answer_others)
        return self._answer(channel, 126, err=b'fixture: this command is not played here\n')


class ListingLibrary(FakeLibraryCloud):
    def answer(self, handler):
        path, _, query = handler.path.partition('?')
        if path.rstrip('/') != '/api/v1/library/materials':
            return super().answer(handler)
        if handler.headers.get('Authorization') != 'Bearer ' + self.token:
            return self._json(handler, 401, {'error': 'not signed in', 'code': 'unauthorized'})
        asked = dict(urllib.parse.parse_qsl(query))
        with self.lock:
            self.requests.append((handler.path, None))
        views = [m['view'] for m in self.materials.values()
                 if all(m['view'].get(key) == asked[key] for key in ('kind', 'status', 'body_type') if key in asked)]
        return self._json(handler, 200, {'materials': views, 'total': len(views), 'limit': int(asked.get('limit', 50)), 'offset': 0})


