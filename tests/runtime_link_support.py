"""Helpers of the tests of issue #17: a robot with a management layer on its own loopback address.

Everything here is a local SSH protocol fixture and a local HTTP stand-in. Nothing reaches a real machine.
"""
import threading

from brainos import connector, runtime_link
from heartbeat_support import FlakyDevice, until
from management_fixture import ManagementLayer

LOOPBACK = '127.0.0.1'
PORT = runtime_link.DEFAULT_PORT


class LoopbackDevice(FlakyDevice):
    """An SSH fixture of a robot that has services on its own loopback address.

    A robot's loopback is not this computer's, so a channel the robot is asked to open to
    ``127.0.0.1:<port>`` ends at the stand-in registered for that port, whatever port the stand-in
    really listens on here. Two robots can each have "their" port 8790 this way. A port nothing was
    registered for is closed: the robot answers that it could not connect."""

    def __init__(self, **options):
        super().__init__(**options)
        self.loopback = {}                # the port a service has on the robot -> the port of its stand-in here
        self.loopback_channels = []       # every channel opened to the robot's own loopback, as it was asked for

    def serve_on_loopback(self, port, stand_in_port):
        self.loopback[port] = stand_in_port
        self.forward_to.add((LOOPBACK, port))

    def stop_serving(self, port):
        self.loopback.pop(port, None)
        self.forward_to.discard((LOOPBACK, port))

    def pump(self, channel, destination):
        host, port = destination
        if host == LOOPBACK and port in self.loopback:
            self.loopback_channels.append(destination)
            destination = (host, self.loopback[port])
        super().pump(channel, destination)

    def open_sessions(self):
        return sum(1 for transport in self.transports if transport.is_active())


class LinkMixin:
    """For a test case built on ``test_ssh_jump.SshCase``: its fixtures become robots with a loopback,
    and the link's own timer is stopped (a round runs when the test calls ``links.tick()``)."""

    def setUp(self):
        super().setUp()
        self.links = self.platform.runtime_link
        self.links.stop_timer()
        if hasattr(self, 'clock'):                # a heartbeat case: both tell the time by the clock the test moves
            self.links.clock = self.clock

    def fixture(self, **options):
        fixture = LoopbackDevice(**options)
        self.addCleanup(fixture.close)
        return fixture

    def install_layer(self, target=None, port=PORT, token_file=True):
        """A management layer on the robot's loopback, as the install script leaves it: listening on
        ``port``, its token in the token file of the standard installation."""
        target = target or self.target
        layer = ManagementLayer()
        self.addCleanup(layer.close)
        target.serve_on_loopback(port, layer.port)
        if token_file:
            target.command_outputs[runtime_link.TOKEN_COMMAND] = (layer.token + '\n').encode()
        return layer

    def attach(self, connection, **fields):
        return self.links.attach({'project_id': self.pid, 'robot_id': connection['robot_id'], **fields})

    def robot(self, connection):
        return self.store.get('robots', connection['robot_id'])

    def call(self, connection, path='/v1/status', payload=None, timeout=8):
        """What every part of the console does to reach a robot's management layer."""
        robot = self.robot(connection)
        return connector.call(robot['endpoint'], path, robot['token'], payload, timeout=timeout)

    def view(self, connection):
        return self.links.overview({})['links'][connection['robot_id']]

    def refused(self, call):
        """The DeviceError a call ends with."""
        with self.assertRaises(connector.DeviceError) as caught:
            call()
        return caught.exception

    def in_background(self, call):
        """Run ``call`` on a thread. Returns (thread, box): ``box`` gets 'result' or 'error'."""
        box = {}

        def run():
            try:
                box['result'] = call()
            except Exception as exc:              # handed to the test, which asserts on it
                box['error'] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, box

    def wait_for_state(self, connection, state, seconds=10):
        return until(lambda: self.view(connection)['state'] == state, seconds)
