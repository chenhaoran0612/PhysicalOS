"""A stand-in for a robot on which the console installs its management layer (issue #19). Test only;
nothing here reaches a real machine.

``InstallDevice`` is the upload fixture of issue #18 (SFTP into a private temporary directory, the
transfer scripts run for real) plus what an install needs:

* the product's install scripts (``runtime_install_device.SCRIPTS``, compared byte for byte, plain
  arguments only) run for real with ``sh``, with ``HOME`` moved into the temporary directory and only
  the tools of ``TOOLS`` on ``PATH``; ``uname`` says Linux (a robot is one);
* the heartbeat's ``true`` and issue #17's command that reads the token file;
* direct-tcpip channels to the robot's own 127.0.0.1: a port where the install's runtime runs is
  answered by a ``ManagementLayer`` with the token of the install; ``listen_elsewhere(port)`` puts some
  other program on a port; ``forwarding = False`` is an SSH service that does not forward.

With ``real=False`` (the default, any OS) the package is ``fake_package``: its ``device_install.sh``
is a short POSIX sh stand-in with the same commands and directory rules, whose "runtime" is a shell
that waits while ``<install>/.fake-running`` exists and has the runtime's entry and ``--port`` among
its arguments, which is what the probe looks for. Files under ``~/.fixture`` steer it (``hold-install``,
``fail-install``, ``hold-start``, ``fail-start``); every command it runs is appended to ``~/.fixture/runs``.

With ``real=True`` (Linux, for ``tests/test_runtime_install_real.py``) the package is the real one,
the scripts get this machine's ``PATH``, and the real runtime the real install script starts listens
on this machine's loopback, which the channels then reach.
"""
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import paramiko

from brainos import runtime_install_device, runtime_link, runtime_package
from management_fixture import ManagementLayer
from ssh_fixture import DEFAULT_LOGIN
from upload_fixture import SHELL, UploadDevice

TOOLS = ('sh', 'bash', 'cat', 'ls', 'mkdir', 'rmdir', 'rm', 'mv', 'chmod', 'head', 'tail', 'cut', 'sed', 'tr', 'grep',
         'df', 'ps', 'nohup', 'sleep', 'od', 'dirname', 'wc', 'env')
SCRIPT_SECONDS = 30
EARLY_ANSWER_DELAY = 0.3
_INSTALL_SCRIPTS = {text: kind for kind, text in runtime_install_device.SCRIPTS.items()}

FAKE_INSTALL_SCRIPT = r'''#!/bin/sh
# Test stand-in for runtime/deploy/device_install.sh (tests/install_fixture.py). Not the product.
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${NX_ROOT:-$HOME/neuxmind}
PORT=${NX_PORT:-8790}
ADAPTER=${NX_ADAPTER:-sim_quadruped}
MARKER=$ROOT/.neuxmind-device-install
CONTROL=$HOME/.fixture
mkdir -p "$CONTROL"
echo "$1 $PORT $ADAPTER" >> "$CONTROL/runs"
wait_while() { while [ -e "$CONTROL/$1" ]; do sleep 0.05; done; }
one_wheel() {
  set -- "$HERE"/"$1"-*.whl
  [ $# -eq 1 ] && [ -f "$1" ] || { echo "need exactly one wheel" >&2; exit 2; }
}
ours() { [ -f "$MARKER" ]; }
installed() { ours && [ "$(cat "$MARKER")" = state=installed ] && [ -f "$ROOT/edge-token" ]; }
running() {
  [ -f "$ROOT/runtime.pid" ] || return 1
  pid=$(cat "$ROOT/runtime.pid")
  case $pid in ''|*[!0-9]*) return 1;; esac
  kill -0 "$pid" 2>/dev/null && [ -e "$ROOT/.fake-running" ]
}
wipe() { rm -rf -- "$ROOT"; }
install() {
  one_wheel neuxmind_spec
  one_wheel neuxmind_runtime
  if [ -e "$ROOT" ]; then
    if ours && ! installed; then echo "replacing an unfinished install in $ROOT"; wipe
    elif [ -d "$ROOT" ] && [ -z "$(ls -A "$ROOT")" ]; then rmdir -- "$ROOT"
    else echo "refusing: $ROOT already exists" >&2; exit 2; fi
  fi
  mkdir -m 700 "$ROOT" || exit 2
  echo state=installing > "$MARKER"
  wait_while hold-install
  if [ -e "$CONTROL/fail-install" ]; then
    echo "ERROR: Could not install packages due to an OSError"
    wipe
    echo "install failed; $ROOT was removed again" >&2
    exit 1
  fi
  wheel=$(ls "$HERE"/neuxmind_runtime-*.whl); version=${wheel##*/neuxmind_runtime-}; version=${version%%-*}
  mkdir -p "$ROOT/venv/bin" "$ROOT/venv/lib/python3.10/site-packages/neuxmind_runtime-$version.dist-info" "$ROOT/state"
  : > "$ROOT/venv/bin/neuxmind-runtime"
  (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$ROOT/edge-token")
  echo "$ADAPTER" > "$ROOT/adapter"
  # Issue #38: like the product, a real body records the ROS settings install was given; the test reads both files.
  env | grep -E '^(NX_ROS_SETUP|ROS_[A-Z_]*|RMW_IMPLEMENTATION|FASTRTPS_DEFAULT_PROFILES_FILE|CYCLONEDDS_URI|ZENOH_SESSION_CONFIG_URI)=' > "$CONTROL/install-env"
  if [ "$ADAPTER" = ros2 ]; then
    (umask 077; sed 's/^NX_ROS_SETUP=/ROS_SETUP=/' "$CONTROL/install-env" > "$ROOT/ros2.env")
  fi
  echo state=installed > "$MARKER"
  echo "token file: $ROOT/edge-token (mode 600)"
}
start() {
  if running; then echo "already running: pid $(cat "$ROOT/runtime.pid")"; return 0; fi
  installed || { echo "not installed: run install first" >&2; exit 2; }
  wait_while hold-start
  if [ -e "$CONTROL/fail-start-once" ]; then        # issue #38: the next start fails, the one after it does not
    rm -f -- "$CONTROL/fail-start-once"
    echo "runtime did not come up; last log lines:" >&2
    exit 1
  fi
  if [ -e "$CONTROL/fail-start" ]; then
    echo "runtime did not come up; last log lines:" >&2
    echo "neuxmind-runtime: error: the adapter did not start" >&2
    exit 1
  fi
  echo "$PORT $ADAPTER" > "$ROOT/.fake-running"
  entry=$(cd "$ROOT" && pwd -P)/venv/bin/neuxmind-runtime
  nohup sh -c 'while [ -e "$1" ]; do sleep 0.2; done' fake-runtime "$ROOT/.fake-running" "$entry" --port "$PORT" >/dev/null 2>&1 </dev/null &
  echo "$!" > "$ROOT/runtime.pid"
  echo "started: pid $!, 127.0.0.1:$PORT, adapter $ADAPTER"
}
stop() {
  if running; then
    pid=$(cat "$ROOT/runtime.pid")
    rm -f -- "$ROOT/.fake-running"
    while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done
    echo "stopped pid $pid"
  else
    echo "not running"
  fi
  if ours; then rm -f -- "$ROOT/runtime.pid"; fi
}
remove() {
  [ -e "$ROOT" ] || { echo "not installed: $ROOT does not exist"; return 0; }
  ours || { echo "refusing: $ROOT was not made by this script" >&2; exit 2; }
  stop
  wipe
  echo "removed $ROOT"
}
case $1 in
  install) install;;
  start) start;;
  stop) stop;;
  remove) remove;;
  status) if running; then echo "running: pid $(cat "$ROOT/runtime.pid")"; else echo "not running"; exit 1; fi;;
  *) echo "usage: $0 install|start|status|manifest|stop|remove" >&2; exit 2;;
esac
'''


def fake_package(directory, runtime_version='0.4.0', wheel_bytes=48 * 1024):
    """A package with the stand-in install script and two made-up wheels. Returns it, loaded."""
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, 'device_install.sh'), 'w') as handle:
        handle.write(FAKE_INSTALL_SCRIPT)
    for name in ('neuxmind_spec-1.1.0-py3-none-any.whl', 'neuxmind_runtime-%s-py3-none-any.whl' % runtime_version):
        with open(os.path.join(directory, name), 'wb') as handle:
            handle.write(b'PK\x03\x04' + secrets.token_bytes(wheel_bytes))
    return runtime_package.write_manifest(directory, runtime_version, '1.1.0', 'fixture')


def _shim(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, 'w') as handle:
        handle.write('#!/bin/sh\n' + text)
    os.chmod(path, 0o755)


class InstallDevice(UploadDevice):
    def __init__(self, real=False, home='/home/robot'):
        super().__init__(home=home)
        self.real = real
        self.username, self.password = DEFAULT_LOGIN
        self.forwarding, self.elsewhere, self.layers = True, {}, {}
        self.pending, self.install_runs, self.forwards, self.early = {}, [], [], []
        self.canned = {}                    # command -> output: read-only probes of other features (scan, status)
        self.host_tools = os.environ.get('PATH', '/usr/bin:/bin')
        for tool in TOOLS:
            found = shutil.which(tool)
            link = os.path.join(self.bin, tool)
            if found and not os.path.lexists(link):
                os.symlink(found, link)
        _shim(self.bin, 'uname', 'case "$1" in -m) echo aarch64;; *) echo Linux;; esac\n')
        # The probe only asks whether these exist; the stand-in install script does not use them.
        for tool, stand_in in (('setsid', 'exec "$@"\n'), ('flock', 'exit 0\n'), ('realpath', 'exit 0\n')):
            found = shutil.which(tool)
            if found:
                os.symlink(found, os.path.join(self.bin, tool))
            else:
                _shim(self.bin, tool, stand_in)
        python = shutil.which('python3') if real else sys.executable
        os.symlink(python, os.path.join(self.bin, 'python3'))
        os.makedirs(self.local('.fixture'), exist_ok=True)

    # --- what a test reads and steers ---------------------------------------------------------

    def environment(self):
        return {'PATH': self.host_tools if self.real else self.bin, 'HOME': self.local(''), 'LANG': 'C', 'LC_ALL': 'C'}

    def control(self, name, on=True):
        path = self.local('.fixture/' + name)
        if on:
            open(path, 'w').close()
        elif os.path.exists(path):
            os.remove(path)

    def runs(self):
        """Every command the stand-in install script ran: (command, port, adapter)."""
        text = self.read('.fixture/runs') or b''
        return [tuple(line.split()) for line in text.decode().splitlines() if line.strip()]

    def token(self):
        data = self.read('neuxmind/edge-token')
        return data.decode().strip() if data else None

    def replace_tool(self, name, text=None):
        """The robot's ``name`` becomes a shell script with ``text``, or goes away (``None``)."""
        path = os.path.join(self.bin, name)
        if os.path.lexists(path):
            os.remove(path)
        if text is not None:
            _shim(self.bin, name, text)

    def by_hand(self, start=True, version='0.4.0', port=8790):
        """A management layer someone installed by hand, as on a robot set up before issue #19: the stand-in
        install script and two wheels straight in ~/neuxmind-pkg, ``install`` (and ``start``) run there."""
        directory = self.local('neuxmind-pkg')
        if not self.real:
            fake_package(directory, runtime_version=version)
            os.remove(os.path.join(directory, runtime_package.MANIFEST))
        for command in ('install', 'start') if start else ('install',):
            code, out, err = self._run(['bash', os.path.join(directory, 'device_install.sh'), command],
                                       seconds=600 if self.real else SCRIPT_SECONDS)
            if code:
                raise AssertionError('installing by hand failed: %r %r' % (out, err))
        return {name: self.read('neuxmind-pkg/' + name) for name in self.names('neuxmind-pkg') if name != 'console'}

    def listen_elsewhere(self, port):
        """Some other program listens on the robot's own 127.0.0.1:<port>."""
        layer = ManagementLayer()
        self.elsewhere[port] = layer
        return layer

    def runtime_port(self):
        """The port of the stand-in runtime of ~/neuxmind while it runs, else None."""
        marker = self.read('neuxmind/.fake-running')
        pid = self.read('neuxmind/runtime.pid')
        if not marker or not pid or not pid.strip().isdigit():
            return None
        try:
            os.kill(int(pid.strip()), 0)
        except OSError:
            return None
        return int(marker.split()[0])

    def layer(self, port):
        """The management layer stand-in that answers for the install's runtime on ``port``."""
        token = self.token()
        kept = self.layers.get(port)
        if kept is None or kept[0] != token:
            if kept is not None:
                kept[1].close()
            kept = (token, ManagementLayer())
            kept[1].token = token
            self.layers[port] = kept
        return kept[1]

    # --- the device side ----------------------------------------------------------------------

    def _run(self, argv, script=None, seconds=SCRIPT_SECONDS, env=None):
        done = subprocess.run(argv, input=script, capture_output=True, env={**self.environment(), **(env or {})},
                              cwd=self.local(''), timeout=seconds)
        return done.returncode, self.banner + done.stdout, done.stderr

    def handle_early(self, command):
        answer = self._early(command)
        if answer is not None:
            # paramiko confirms the request to run a command only after this handler's thread has started:
            # an answer and a close sent before that confirmation read as "Channel closed" to the client.
            time.sleep(EARLY_ANSWER_DELAY)
        return answer

    def _early(self, command):
        if command == 'true':
            self.early.append(command)
            return 0, b'', b''
        if command == runtime_link.TOKEN_COMMAND:
            self.early.append(command)
            return self._run([*SHELL, '-c', command])
        if command in self.canned:
            self.early.append(command)
            return 0, self.canned[command], b''
        return None

    def handle(self, command, script):
        kind = _INSTALL_SCRIPTS.get(script.decode('utf-8', 'replace'))
        if kind is None:
            return None
        words = command.split(' ')
        try:
            ok = ' '.join(words[:3]) == runtime_install_device.COMMAND and \
                runtime_install_device.command(kind, *words[3:]) == command
        except ValueError:
            ok = False
        if not ok:
            self.refused.append(command)
            return 126, b'', b'fixture: not an install script command\n'
        self.install_runs.append((kind, tuple(words[3:])))
        return self._run([*SHELL, '-s', '--', *words[3:]], script)

    def _target(self, port):
        """The port on this computer that stands for the robot's own 127.0.0.1:<port>, or None."""
        if port in self.elsewhere:
            return self.elsewhere[port].port
        if self.real:
            with socket.socket() as probe:
                probe.settimeout(1)
                return port if probe.connect_ex(('127.0.0.1', port)) == 0 else None
        return self.layer(port).port if self.runtime_port() == port else None

    def forward(self, chanid, destination):
        if not self.forwarding:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        host, port = destination
        target = self._target(port) if host == '127.0.0.1' else None
        if target is None:
            return paramiko.OPEN_FAILED_CONNECT_FAILED
        self.pending[chanid] = target
        self.forwards.append(destination)
        return paramiko.OPEN_SUCCEEDED

    def forwarded(self, channel):
        target = self.pending.pop(channel.get_id(), None)
        if target is None:
            return
        try:
            upstream = socket.create_connection(('127.0.0.1', target), timeout=5)
        except OSError:
            channel.close()
            return
        upstream.settimeout(None)

        def copy(read, write):
            try:
                while True:
                    data = read(32768)
                    if not data:
                        break
                    write(data)
            except (OSError, EOFError, paramiko.SSHException):
                pass
            finally:
                for end in (channel, upstream):
                    try:
                        end.close()
                    except (OSError, EOFError, paramiko.SSHException):
                        pass
        threading.Thread(target=copy, args=(channel.recv, upstream.sendall), daemon=True).start()
        threading.Thread(target=copy, args=(upstream.recv, channel.sendall), daemon=True).start()

    def drop_sessions(self):
        """Every open SSH session ends, as when the robot's network drops for a moment."""
        for transport in self.transports:
            transport.close()

    def close(self):
        marker = self.local('neuxmind/.fake-running')
        if os.path.exists(marker):
            os.remove(marker)               # the stand-in runtime ends by itself within 0.2 s
        if self.real:
            self._end_processes()
        for _, layer in self.layers.values():
            layer.close()
        for layer in self.elsewhere.values():
            layer.close()
        super().close()

    def _end_processes(self):
        """In real mode: end what the real install script started from this fixture's directory."""
        for entry in os.listdir('/proc'):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue
            try:
                with open('/proc/%s/cmdline' % entry, 'rb') as handle:
                    if self.base.encode() in handle.read():
                        os.kill(int(entry), signal.SIGKILL)
            except OSError:
                continue
