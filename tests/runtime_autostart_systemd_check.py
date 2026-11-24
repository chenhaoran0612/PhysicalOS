"""Issue #20 for real: the console turns autostart on in a Linux "robot" that runs systemd, sudo and an SSH
service, the robot loses power, and the management layer comes back by itself. Manual: not part of any suite.

What runs for real: systemd as pid 1, sudo with a password, sshd, the real install package (the real
``device_install.sh``, the two wheels, the runtime they install) and the console's own code (``Platform``,
on this computer, reaching the container's SSH port on 127.0.0.1). A power cut is ``docker kill`` (no
shutdown) followed by ``docker start`` after a pause.

    PYTHONPATH=.:spec:runtime:tests NEUXMIND_MAIL_DEV_ECHO=1 python tests/runtime_autostart_systemd_check.py \\
        [--build [--apt-mirror URL]] [--image nx-autostart-robot:local] [--package DIR] [--keep]

``--build`` builds the image first from ``DOCKERFILE`` below (Ubuntu 22.04 with ROS 2 Humble, the robot's
system, plus systemd, sshd, sudo and python3-venv; it needs the network once). The login and sudo password
is made up for each run and never printed. Checks, in order:

1. install with autostart: the plan lists the service file, the person's confirmation with the sudo password,
   the service enabled and running the runtime as the login account, the robot linked and online;
2. a power cut: the service starts the runtime at boot, the console's heartbeat sees the robot offline and then
   online again by itself, the link comes back;
3. a boot at which the runtime cannot start at first (another program holds its port for 40 s): systemd
   tries again until it runs, and the console is online again;
4. "取消开机自启": the service file goes, the runtime keeps running detached; after a power cut it stays down;
5. turning it on again from the card while the runtime is down: the service starts it;
6. removing the install by hand (``device_install.sh remove``): the service file stays but starts nothing at boot;
   "取消开机自启" then takes the file away.
"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'spec'), str(ROOT / 'runtime'), str(ROOT / 'tests')]

from brainos import runtime_autostart_device as auto  # noqa: E402
from brainos import runtime_package  # noqa: E402
from brainos.service import Platform  # noqa: E402

IMAGE = 'nx-autostart-robot:local'
BODIES = {'quadruped': 'sim_quadruped', 'manipulator': 'sim_arm'}
USER = 'robot'
DOCKERFILE = r'''# Throwaway "robot" for tests/runtime_autostart_systemd_check.py (issue #20). Test only, never pushed.
FROM ros:humble-ros-base-jammy
ARG APT_MIRROR=http://ports.ubuntu.com/ubuntu-ports
RUN sed -i "s|http://ports.ubuntu.com/ubuntu-ports|${APT_MIRROR}|g" /etc/apt/sources.list \
 && mv /etc/apt/sources.list.d/ros2.sources /root/ros2.sources.off \
 && apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv dbus openssh-server python3-venv iproute2 procps >/dev/null \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && systemctl enable ssh \
 && systemctl mask systemd-logind.service getty.target console-getty.service \
 && useradd -m -s /bin/bash robot && usermod -aG sudo robot \
 && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/sbin/init"]
'''
BLOCKER = '''[Unit]
Description=test only: holds the runtime's port for 40 s at boot
Before=neuxmind-runtime.service
[Service]
Type=simple
ExecStart=/usr/bin/python3 -c "import socket, time; s = socket.socket(); s.bind(('127.0.0.1', 8790)); s.listen(); time.sleep(40)"
[Install]
WantedBy=multi-user.target
'''
RESULTS = []


def check(condition, what, **facts):
    RESULTS.append({'check': what, 'ok': bool(condition), **facts})
    print(('PASS ' if condition else 'FAIL ') + what + (' ' + json.dumps(facts, ensure_ascii=False) if facts else ''), flush=True)
    if not condition:
        raise SystemExit(1)


def docker(*args, stdin=None, check_exit=True, timeout=600):
    done = subprocess.run(['docker', *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    if check_exit and done.returncode:
        raise RuntimeError('docker %s: %s' % (args[0], done.stderr.strip()[-500:]))
    return done.stdout.strip()


class Robot:
    def __init__(self, image, keep):
        self.name, self.keep, self.image = 'nx-autostart-check-' + secrets.token_hex(4), keep, image
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.password = secrets.token_urlsafe(18)

    def run(self):
        docker('run', '-d', '--name', self.name, '--privileged', '--cgroupns=private', '--tmpfs', '/run', '--tmpfs', '/run/lock',
               '-p', '127.0.0.1:%d:22' % self.port, self.image)
        self.booted()
        docker('exec', '-i', self.name, 'chpasswd', stdin='%s:%s\n' % (USER, self.password))

    def booted(self, seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = docker('exec', self.name, 'systemctl', 'is-system-running', check_exit=False)
            if state in ('running', 'degraded') and self.ssh_up():
                return
            time.sleep(0.5)
        raise RuntimeError('the container did not boot')

    def ssh_up(self):
        try:
            with socket.create_connection(('127.0.0.1', self.port), timeout=2) as sock:
                sock.settimeout(3)
                return sock.recv(64).startswith(b'SSH-')
        except OSError:
            return False

    def sh(self, command, user=None):
        return docker('exec', *(['-u', user] if user else []), self.name, 'sh', '-c', command, check_exit=False)

    def service(self, *properties):
        text = self.sh('systemctl show neuxmind-runtime.service ' + ' '.join('-p ' + p for p in properties))
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)

    def active(self, since, seconds=180):
        """Seconds from ``since`` until systemd reports the service running (it may be retrying until then)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.service('ActiveState', 'SubState') == {'ActiveState': 'active', 'SubState': 'running'}:
                return round(time.monotonic() - since, 1)
            time.sleep(0.5)
        raise RuntimeError('the service did not run: %s' % self.service('ActiveState', 'SubState', 'NRestarts', 'Result'))

    def runtime_pid(self):
        return self.sh('cat /home/%s/neuxmind/runtime.pid 2>/dev/null' % USER)

    def power_cut(self, off_seconds):
        """(when the power went, when it came back)"""
        docker('kill', self.name)
        cut = time.monotonic()
        time.sleep(off_seconds)
        docker('start', self.name)
        started = time.monotonic()
        self.booted()
        return cut, started

    def remove(self):
        if not self.keep:
            docker('rm', '-f', self.name, check_exit=False)


class Console:
    def __init__(self, directory, robot, family='quadruped'):
        self.platform, self.robot, self.family = Platform(directory), robot, family
        self.platform.heartbeat.interval, self.platform.heartbeat.offline_after = 5, 15
        self.installs = self.platform.runtime_install
        self.engine = self.platform.engineering
        self.samples, self._sampling = [], threading.Event()      # (monotonic time, heartbeat state, link state)
        self.pid = self.platform.add_project({'name': 'Autostart', 'objective': 'The management layer comes back after a power cut'})['id']

    def connect(self):
        check_ = self.engine.scan({'project_id': self.pid, 'host': '127.0.0.1', 'port': self.robot.port, 'username': USER})
        self.connection = self.engine.connect({'project_id': self.pid, 'host_check_id': check_['id'], 'trust_fingerprint': check_['fingerprint'],
                                               'auth': 'password', 'password': self.robot.password, 'name': 'Robot with systemd',
                                               'family': self.family, 'read_roots': ['/opt/ros']})
        self.platform.heartbeat.start_timer()
        self.platform.runtime_link.start_timer()
        self.sample()

    def request(self, **fields):
        return {'project_id': self.pid, 'robot_id': self.connection['robot_id'], **fields}

    def view(self):
        return self.installs.status({})['installs'].get(self.connection['robot_id'])

    def wait(self, until, seconds=900):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            view = self.view()
            if view is not None and until(view):
                return view
            time.sleep(0.5)
        raise RuntimeError('waited too long: %s' % json.dumps(self.view(), ensure_ascii=False)[:2000])

    def confirm(self, kind):
        view = self.wait(lambda v: v['state'] != 'running' or (v.get('autostart') or {}).get('state') == 'waiting')
        plan = (view.get('autostart') or {}).get('plan') or {}
        check(plan.get('kind') == kind and plan.get('needs_password') is True, 'the job waits for the person (%s)' % kind,
              commands=[step['command'] for step in plan.get('commands', [])])
        self.installs.answer(self.request(job_id=view['job_id'], plan=plan['id'], decision='confirm', sudo_password=self.robot.password))
        return plan

    def robot_record(self):
        return self.platform.store.get('robots', self.connection['robot_id'])

    def heartbeat(self):
        return self.platform.heartbeat.overview({})['links'][self.connection['robot_id']]['state']

    def link(self):
        return (self.platform.runtime_link.overview({})['links'].get(self.connection['robot_id']) or {}).get('state')

    def sample(self):
        """From now on, what the console shows is written down every half second (in the background)."""
        def run():
            while not self._sampling.wait(0.5):
                try:
                    self.samples.append((time.monotonic(), self.heartbeat(), self.link()))
                except Exception:                   # a store closed under the last look at the end
                    return
        threading.Thread(target=run, daemon=True).start()

    def back_online(self, cut, since, seconds=300):
        """After a power cut at ``cut`` and power on at ``since``: (seconds from power on until the console shows
        the robot online with its link up, by itself; whether it showed it offline in between)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            after = [sample for sample in self.samples if sample[0] >= cut]
            offline = [sample[0] for sample in after if sample[1] == 'offline']
            if offline:
                back = [sample[0] for sample in after if sample[0] > offline[0] and sample[1] == 'online' and sample[2] == 'up']
                if back:
                    return round(back[0] - since, 1), True
            time.sleep(0.5)
        raise RuntimeError('not online again: heartbeat %s, link %s, seen %s' % (
            self.heartbeat(), self.link(), sorted({sample[1] for sample in self.samples if sample[0] >= cut})))

    def connected(self):
        result = self.platform.connect_robot({'robot_id': self.connection['robot_id']})
        return result.get('status') == 'connected', self.robot_record().get('error')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--image', default=IMAGE)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--apt-mirror', default='http://ports.ubuntu.com/ubuntu-ports')
    parser.add_argument('--package')
    parser.add_argument('--keep', action='store_true', help='leave the container for a look afterwards')
    parser.add_argument('--family', default='quadruped', choices=sorted(BODIES), help='the robot\'s body type (its simulated body)')
    args = parser.parse_args()
    tmp = tempfile.TemporaryDirectory()
    if args.build:
        Path(tmp.name, 'Dockerfile').write_text(DOCKERFILE)
        docker('build', '-q', '--build-arg', 'APT_MIRROR=' + args.apt_mirror, '-t', args.image, tmp.name, timeout=3600)
    package = runtime_package.load(args.package) if args.package else runtime_package.build(os.path.join(tmp.name, 'package'))
    os.environ[runtime_package.ENV] = str(package.directory)
    robot = Robot(args.image, args.keep)
    robot.run()
    console = None
    try:
        console = Console(os.path.join(tmp.name, 'state'), robot, args.family)
        console.connect()
        scenario(console, robot, package, BODIES[args.family])
    finally:
        if console is not None:
            console.platform.close()
        robot.remove()
        tmp.cleanup()
    print(json.dumps({'ok': all(result.get('ok', True) for result in RESULTS), 'checks': sum('check' in r for r in RESULTS), 'container': robot.name}, ensure_ascii=False))


def scenario(console, robot, package, body='sim_quadruped'):
    home = '/home/' + USER
    # 1. Install with autostart.
    started = time.monotonic()
    console.installs.start(console.request(autostart=True))
    plan = console.confirm('on')
    check(plan['unit'] == auto.render_unit(USER, home, package.id, 8790, body), 'the plan shows the service file the robot gets', body=body)
    view = console.wait(lambda v: v['state'] != 'running')
    check(view['state'] == 'succeeded', 'the install with autostart succeeded', seconds=round(time.monotonic() - started, 1),
          failure=view.get('failure'))
    check(robot.sh('cat ' + auto.UNIT_PATH) + '\n' == plan['unit'], 'the robot has exactly the confirmed service file')
    state = robot.service('UnitFileState', 'ActiveState', 'SubState', 'MainPID')
    check(state == {'UnitFileState': 'enabled', 'ActiveState': 'active', 'SubState': 'running', 'MainPID': robot.runtime_pid()},
          'enabled, running, and the runtime is the service\'s main process', **state)
    check(robot.sh('ps -o user= -p ' + state['MainPID']).strip() == USER, 'the runtime runs as the login account, not root')
    check(robot.sh('stat -c %U ' + auto.UNIT_PATH) == 'root' and robot.sh('stat -c %a ' + auto.UNIT_PATH) == '644', 'the service file is root\'s, 0644')
    record = console.robot_record()
    check(record['status'] == 'connected' and record['runtime_autostart']['state'] == 'on' and console.heartbeat() == 'online',
          'the console: linked, online, autostart on')
    sandbox = robot.sh('bash %s/neuxmind-pkg/console/%s/device_install.sh status | tail -n 1' % (home, package.id), user=USER)
    check('"runtime_version"' in sandbox, 'the runtime answers its status', status=sandbox[:400])
    manifest = robot.sh('bash %s/neuxmind-pkg/console/%s/device_install.sh manifest' % (home, package.id), user=USER)
    try:
        family = json.loads(manifest).get('family')
    except ValueError:
        family = None
    check(family == console.family, 'the service started the body that was installed', family=family)

    # 2. A power cut; everything comes back by itself.
    cut, since = robot.power_cut(30)
    seconds, offline = console.back_online(cut, since)
    robot.active(since)
    state = robot.service('ActiveState', 'SubState', 'NRestarts')
    check(state['ActiveState'] == 'active', 'after the power cut the service runs the runtime again', **state)
    check(offline, 'the console saw the robot offline, then online and linked again by itself', seconds_after_power_on=seconds)
    check(console.connected()[0], 'the connection check reaches the management layer after the power cut')
    status = robot.sh('bash %s/neuxmind-pkg/console/%s/device_install.sh status | tail -n 1' % (home, package.id), user=USER)
    RESULTS.append({'fact': 'e-stop latch after a power cut (the runtime latches after an unclean end, by design)',
                    'estop_latched': '"estop_latched": true' in status})
    print('FACT e-stop latched after the power cut: %s' % ('"estop_latched": true' in status), flush=True)

    # 3. A boot at which the runtime cannot start at first: systemd tries again.
    robot.sh("cat > /etc/systemd/system/nx-check-blocker.service <<'EOF'\n%sEOF\nsystemctl daemon-reload && systemctl enable nx-check-blocker.service" % BLOCKER)
    cut, since = robot.power_cut(30)
    seconds, _ = console.back_online(cut, since)
    running = robot.active(since)
    state = robot.service('ActiveState', 'NRestarts')
    check(state['ActiveState'] == 'active' and int(state['NRestarts']) >= 1,
          'the port was busy at boot: systemd tried again until the runtime ran', heartbeat_online_after=seconds,
          service_running_after=running, **state)
    check(console.connected()[0], 'and the console reaches the management layer again')
    robot.sh('systemctl disable --now nx-check-blocker.service; rm -f /etc/systemd/system/nx-check-blocker.service; systemctl daemon-reload')

    # 4. Turning it off: the file goes, the runtime keeps running detached; after a power cut it stays down.
    console.installs.autostart_off(console.request())
    console.confirm('off')
    view = console.wait(lambda v: v['state'] != 'running')
    check(view['state'] == 'succeeded', 'autostart turned off', failure=view.get('failure'))
    check(robot.sh('test -e %s && echo yes || echo no' % auto.UNIT_PATH) == 'no' and robot.service('UnitFileState')['UnitFileState'] == '',
          'the service file is gone')
    check(robot.sh('kill -0 $(cat %s/neuxmind/runtime.pid) && echo up' % home) == 'up' and console.connected()[0],
          'the runtime runs detached and is linked')
    robot.power_cut(5)
    time.sleep(5)
    check(robot.sh('pgrep -u %s -f neuxmind-runtime >/dev/null && echo up || echo down' % USER) == 'down',
          'without autostart the runtime stays down after a power cut')

    # 5. On again from the card while the runtime is down: the service starts it.
    console.installs.autostart(console.request())
    console.confirm('on')
    view = console.wait(lambda v: v['state'] != 'running')
    check(view['state'] == 'succeeded' and robot.service('ActiveState')['ActiveState'] == 'active' and console.connected()[0],
          'turned on from the card, the service started the runtime and it is linked', failure=view.get('failure'))

    # 6. The install removed by hand: the service file stays but starts nothing; "取消开机自启" takes it away.
    robot.sh('bash %s/neuxmind-pkg/console/%s/device_install.sh remove' % (home, package.id), user=USER)
    robot.power_cut(5)
    time.sleep(5)
    state = robot.service('ActiveState', 'ConditionResult')
    check(state == {'ActiveState': 'inactive', 'ConditionResult': 'no'}, 'with the install gone the service starts nothing at boot', **state)
    console.installs.autostart_off(console.request())
    console.confirm('off')
    view = console.wait(lambda v: v['state'] != 'running')
    check(view['state'] == 'succeeded' and robot.sh('test -e %s && echo yes || echo no' % auto.UNIT_PATH) == 'no',
          '"取消开机自启" removed the leftover service file', steps=[stage['id'] for stage in view['stages']], failure=view.get('failure'))


if __name__ == '__main__':
    main()
