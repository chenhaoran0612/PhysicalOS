"""HTTP to a robot's management layer, sent over a channel of the robot's SSH connection (issue #17, R1-09).

A robot that is reached over SSH has ``ssh-forward:<robot id>`` as its endpoint instead of an address.
``connector.call`` hands such an endpoint to ``transport`` here, and everything else about the call
(the token, the time limit, how an answer or an error is read) stays what it is for any endpoint.

Nothing listens on this computer for it. The request is written into an SSH ``direct-tcpip`` channel
that the robot's own SSH service connects to a port on the robot's loopback address, and the answer is
read from that channel. So there is no local port for two robots to collide on, and no port that
another program on this computer could connect to.

Which robot, which port, and the SSH session the channel is opened on belong to
``runtime_link.RuntimeLinks``. The address is not a parameter anywhere: it is ``DEVICE_LOOPBACK``.
"""
import http.client
import io
import re
import socket
import threading
import time
import urllib.request

from . import connector

SCHEME = 'ssh-forward'
PREFIX = SCHEME + ':'
DEVICE_LOOPBACK = '127.0.0.1'          # on the robot: the only address a channel is ever opened to
ROBOT_ID = re.compile(r'[A-Za-z0-9_-]{1,80}')
DEFAULT_TIMEOUT_SECONDS = 8
# A request and its answer end within the caller's time limit plus this, plus the time the request body
# needs at MIN_SEND_RATE: whatever listens on the robot's port cannot hold a thread of the console longer
# by answering a byte at a time (or with "100 Continue" for ever). The caller's limit is still the limit
# of each single wait, as for any connection.
EXCHANGE_GRACE_SECONDS = 120
MIN_SEND_RATE = 4096                   # bytes per second
SEND_CHUNK = 8192                      # small enough that one send takes seconds even on a very slow link
# paramiko writes to the TCP connection under the session's lock and retries for as long as the kernel
# keeps the connection (about 15 minutes) when the far end stops acknowledging: no time limit reaches that
# write. A write to the session that is still blocked after this long (or after the caller's limit, if
# longer) ends by closing the whole session, which is then replaced; the request ends as a lost answer.
STALL_SECONDS = 30
STALL_MARGIN_SECONDS = 10              # beyond the caller's own limit: a wait for the far end's window ends first
NOT_CONFIGURED = ('这台机器人没有经 SSH 接入管理层（或接入已被移除）；请在机器人卡片上点“接入管理层”。'
                  '这次请求没有发出')


def endpoint_of(robot_id):
    """What is saved as the endpoint of a robot whose management layer is reached over its SSH connection."""
    return PREFIX + robot_id


def is_forwarded(endpoint):
    return isinstance(endpoint, str) and endpoint.startswith(PREFIX)


def not_configured():
    return connector.DeviceError(NOT_CONFIGURED, code='device_link_not_configured', sent=False)


def bounded(transport, seconds, action):
    """``action()`` on an SSH session (``transport``), but never blocked in a write to it longer than
    ``seconds``: the session is closed then (see STALL_SECONDS), and socket.timeout is raised."""
    stalled = threading.Event()

    def give_up():
        stalled.set()
        try:
            transport.close()
        except Exception:                             # a session that is already broken has nothing left to close
            pass
    watchdog = threading.Timer(max(seconds, 0), give_up)
    watchdog.daemon = True
    try:
        watchdog.start()
    except RuntimeError:                              # no thread to be had: the action runs without the watchdog
        watchdog = None
    try:
        return action()
    except Exception:
        if stalled.is_set():
            raise socket.timeout('the SSH session to the robot stalled and was closed') from None
        raise
    finally:
        if watchdog is not None:
            watchdog.cancel()


class _ChannelReader(io.RawIOBase):
    """The read side of a channel, as the file ``http.client`` reads a response from."""

    def __init__(self, sock):
        super().__init__()
        self._sock = sock

    def readable(self):
        return True

    def readinto(self, buffer):
        self._sock.wait_limit()
        data = self._sock.channel.recv(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        if not self.closed:
            super().close()
            self._sock.reader_closed()


class ChannelSocket:
    """What ``http.client`` uses of a socket, on an SSH channel.

    Like a socket it stays open until both it and the file made from it are closed: urllib closes the
    socket as soon as it has the head of the response, and reads the body from the file afterwards."""

    def __init__(self, channel, timeout):
        self.channel, self._readers, self._closed = channel, 0, False
        self._timeout = timeout
        self._deadline = time.monotonic() + timeout + EXCHANGE_GRACE_SECONDS

    def settimeout(self, timeout):
        self._timeout = timeout

    def wait_limit(self):
        """Before each wait: the caller's limit for one wait, or what is left of the whole exchange."""
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout('the management layer took too long to answer')
        self.channel.settimeout(left if self._timeout is None else min(self._timeout, left))

    def sendall(self, data):
        data = bytes(data)
        self._deadline += len(data) / MIN_SEND_RATE
        for start in range(0, len(data), SEND_CHUNK):
            chunk = data[start:start + SEND_CHUNK]
            while chunk:
                self.wait_limit()
                sent = self._send(chunk)
                if sent <= 0:
                    raise ConnectionResetError('the channel to the management layer was closed')
                chunk = chunk[sent:]

    def _send(self, chunk):
        """One send, never stuck in the session's write for long (see STALL_SECONDS). Whatever paramiko
        raises for a session that went away is a connection error to urllib: a lost answer, not a crash."""
        import paramiko
        transport = self.channel.get_transport()
        if transport is None:
            raise ConnectionResetError('the SSH session to the robot is gone')
        limit = max(self._timeout or DEFAULT_TIMEOUT_SECONDS, STALL_SECONDS) + STALL_MARGIN_SECONDS
        try:
            return bounded(transport, limit, lambda: self.channel.send(chunk))
        except (EOFError, paramiko.SSHException) as exc:
            raise ConnectionResetError('the SSH session to the robot went away') from exc

    def makefile(self, mode='rb', *args, **kwargs):
        self._readers += 1
        return io.BufferedReader(_ChannelReader(self))

    def reader_closed(self):
        self._readers -= 1
        self._release()

    def close(self):
        self._closed = True
        self._release()

    def _release(self):
        if self._closed and self._readers <= 0:
            self.channel.close()


class ChannelConnection(http.client.HTTPConnection):
    """An HTTP connection whose "socket" is a channel that ``open_channel(seconds)`` returns.
    The host and port of the URL are not used: the channel is the only way anywhere.

    ``open_channel`` raises ``connector.DeviceError`` itself when there is no channel to be had: that
    is not an ``OSError``, so it reaches the caller of ``connector.call`` as it is."""

    def __init__(self, host, open_channel, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        super().__init__(host, timeout=timeout)
        self._open_channel = open_channel

    def connect(self):
        seconds = self.timeout if isinstance(self.timeout, (int, float)) else DEFAULT_TIMEOUT_SECONDS
        self.sock = ChannelSocket(self._open_channel(seconds), seconds)


class ForwardHandler(urllib.request.HTTPHandler):
    """Takes the place of urllib's own HTTP handler for one call: same requests, another way there."""

    def __init__(self, open_channel):
        super().__init__()
        self._open_channel = open_channel

    def http_open(self, req):
        # The token the request presents: the link checks it is still the robot's before anything is sent.
        header = req.get_header('Authorization') or ''
        token = header[len('Bearer '):] if header.startswith('Bearer ') else ''

        def connection(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
            return ChannelConnection(host, lambda seconds: self._open_channel(seconds, token), timeout)
        return self.do_open(connection, req)


def transport(endpoint, forwarders):
    """(base URL, urllib handlers) of an endpoint that names a robot reached over SSH.

    ``forwarders``: the ``RuntimeLinks`` of the open workspaces. The one that has this robot linked
    opens the channel. An endpoint that is not exactly ``ssh-forward:<id of a linked robot>`` reaches
    nothing: nothing in it is read as a host, a port or a path."""
    robot_id = endpoint[len(PREFIX):] if is_forwarded(endpoint) else ''
    if ROBOT_ID.fullmatch(robot_id):
        for links in list(forwarders):
            port = links.port_of(robot_id)
            if port is not None:
                return f'http://{DEVICE_LOOPBACK}:{port}', (ForwardHandler(lambda seconds, token: links.open(robot_id, seconds, token)),)
    raise not_configured()
