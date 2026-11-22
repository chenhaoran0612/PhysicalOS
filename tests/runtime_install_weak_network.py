"""Weak-network check of issue #19 (R1-03's weak-network line). Linux only, as root with NET_ADMIN: it shapes
the loopback interface with tc netem. Not part of any suite; run it in a throwaway container (Docker Desktop's
kernel has netem), from the repository root, with an install package built by scripts/build_device_package.py:

    docker run --rm --cap-add NET_ADMIN -v "$PWD":/src:ro -v <package>:/package:ro -v <out>:/out \\
      -e NEUXMIND_TEST_REAL_PACKAGE=/package <python 3.10+ image with paramiko and iproute2> sh -c \\
      'cp -r /src /work && cd /work && PYTHONDONTWRITEBYTECODE=1 python -u tests/runtime_install_weak_network.py /out/result.json'

Without NEUXMIND_TEST_REAL_PACKAGE the robots get the stand-in package of tests/install_fixture.py.

Only the SSH connections between the console and the two robot stand-ins are impaired (a prio qdisc with a u32
filter on their SSH ports, both directions); each robot's own loopback stays clean, as on a real robot.
Delay applies per direction, so the round trip is twice the delay.

  weak     robot A, 1 Mbit/s, 300 ms each way, 30 % loss: install from scratch; when an install does not finish
           a person starts it again, until it is done or WEAK_SECONDS (3600) passed. Required: it finishes.
  bad      robot B, 60 % loss: an install runs for BAD_SECONDS (600) and is then stopped if it still runs.
           Required: no damaged file on the robot (every file with its own name is whole and verified).
  restored the network is clean again: robot B is installed once more, within RESTORE_SECONDS (1800).
           Required: it finishes, and what was already on the robot was not sent again.

Writes the result file named on the command line and prints a summary. Local stand-ins only; nothing
reaches a real machine.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / part) for part in ('spec', 'runtime', 'sdk', '.', 'tests')]
import paramiko  # noqa: E402

_init = paramiko.Transport.__init__


def _patient(self, *args, **kwargs):
    """The stand-in's server side gives a login as long as a real sshd (LoginGraceTime 120 s); the client side
    overrides these with the product's own limits when it connects."""
    _init(self, *args, **kwargs)
    self.banner_timeout = self.handshake_timeout = self.auth_timeout = 120


paramiko.Transport.__init__ = _patient

from brainos import runtime_package  # noqa: E402
from brainos.service import Platform  # noqa: E402
from install_fixture import InstallDevice, fake_package  # noqa: E402
from ssh_fixture import DEFAULT_LOGIN  # noqa: E402

WEAK = float(os.environ.get('WEAK_SECONDS', 3600))
BAD = float(os.environ.get('BAD_SECONDS', 600))
RESTORE_LIMIT = float(os.environ.get('RESTORE_SECONDS', 1800))
# With BAD_AFTER_BYTES the 60 % phase begins on a clean link and turns bad once robot B received that many bytes of
# the package, so the bad link hits an upload under way instead of the first login.
BAD_AFTER = int(os.environ.get('BAD_AFTER_BYTES', 0))
SKIP_WEAK = os.environ.get('SKIP_WEAK') == '1'
START = time.monotonic()
events = []


def log(phase, kind, **fields):
    entry = {'t': round(time.monotonic() - START, 1), 'phase': phase, 'kind': kind, **fields}
    events.append(entry)
    print(json.dumps(entry, ensure_ascii=False), flush=True)


def run(*args):
    subprocess.run(list(args), check=True)


def shaping(ports):
    run('ip', 'link', 'set', 'lo', 'mtu', '1500')
    run('tc', 'qdisc', 'add', 'dev', 'lo', 'root', 'handle', '1:', 'prio', 'bands', '4', 'priomap', *['0'] * 16)
    run('tc', 'qdisc', 'add', 'dev', 'lo', 'parent', '1:4', 'handle', '40:', 'netem', 'delay', '0ms')
    for port in ports:
        for side in ('dport', 'sport'):
            run('tc', 'filter', 'add', 'dev', 'lo', 'parent', '1:', 'protocol', 'ip', 'prio', '1', 'u32',
                'match', 'ip', side, str(port), '0xffff', 'flowid', '1:4')


def network(phase, *netem):
    run('tc', 'qdisc', 'change', 'dev', 'lo', 'parent', '1:4', 'handle', '40:', 'netem', *(netem or ('delay', '0ms')))
    log(phase, 'network', netem=' '.join(netem) or 'clean')


def whole_files(device, package):
    """Every file in the package's directory that has its own name is the one the console sent."""
    expected = {entry['name']: entry['sha256'] for entry in package.files}
    directory = 'neuxmind-pkg/console/' + package.id
    damaged, names = [], device.names(directory)
    for name in names:
        if name in expected and hashlib.sha256(device.read(directory + '/' + name)).hexdigest() != expected[name]:
            damaged.append(name)
    return {'names': names, 'damaged': damaged}


def main(out):
    tmp = tempfile.mkdtemp()
    real = bool(os.environ.get('NEUXMIND_TEST_REAL_PACKAGE'))
    package = runtime_package.load(os.environ['NEUXMIND_TEST_REAL_PACKAGE']) if real else fake_package(os.path.join(tmp, 'package'))
    os.environ[runtime_package.ENV] = str(package.directory)
    platform = Platform(os.path.join(tmp, 'state'))
    installs, engine = platform.runtime_install, platform.engineering
    installs.budgets['upload'] = max(installs.budgets['upload'], WEAK)
    pid = platform.add_project({'name': 'weak', 'objective': 'weak network'})['id']
    robots = {'A': InstallDevice(real=real), 'B': InstallDevice(real=real)}
    shaping([device.port for device in robots.values()])
    log('setup', 'package', id=package.id, bytes=package.total_bytes, real=real)

    def connect(name):                       # adding the robot is not part of this check: on a clean network
        device = robots[name]
        check = engine.scan({'project_id': pid, 'host': '127.0.0.1', 'port': device.port, 'username': 'robot'})
        return engine.connect({'project_id': pid, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint'],
                               'auth': 'password', 'password': DEFAULT_LOGIN[1], 'name': 'robot ' + name, 'family': 'quadruped'})

    def install(phase, connection, port, limit, each=None):
        started = time.monotonic()
        installs.start({'project_id': pid, 'robot_id': connection['robot_id'], 'port': port})
        last = None
        while True:
            if each:
                each()
            view = installs.status({})['installs'][connection['robot_id']]
            if (view['state'], view['stage']) != last:
                last = (view['state'], view['stage'])
                log(phase, 'install', state=view['state'], stage=view['stage'], progress=view['progress'],
                    code=(view['failure'] or {}).get('code'), message=((view['failure'] or {}).get('message') or '')[:240],
                    seconds=round(time.monotonic() - started, 1))
            if view['state'] != 'running':
                return view
            if time.monotonic() - started > limit:
                installs.stop({'project_id': pid, 'robot_id': connection['robot_id']})
                while installs.status({})['installs'][connection['robot_id']]['state'] == 'running':   # as a person waits for it
                    time.sleep(2)
                stopped = installs.status({})['installs'][connection['robot_id']]
                log(phase, 'stopped', state=stopped['state'], stage=stopped['stage'], left=stopped['left'],
                    seconds=round(time.monotonic() - started, 1))
                return stopped
            time.sleep(2)

    result = {'package': package.summary(), 'real': real}
    a, b = connect('A'), connect('B')

    # weak: 30 % loss; a person starts it again until it is done
    if not SKIP_WEAK:
        network('weak', 'delay', '300ms', 'loss', '30%', 'rate', '1mbit')
    weak_start, attempts, view = time.monotonic(), 0, None
    while not SKIP_WEAK and time.monotonic() - weak_start < WEAK:
        attempts += 1
        view = install('weak', a, 8790, WEAK - (time.monotonic() - weak_start))
        if view['state'] == 'succeeded':
            break
        if view['state'] == 'needs_choice':
            break
    robot = platform.store.get('robots', a['robot_id'])
    result['weak'] = {'finished': view is not None and view['state'] == 'succeeded', 'attempts': attempts,
                      'seconds': round(time.monotonic() - weak_start, 1), 'robot_status': robot.get('status'),
                      'files': whole_files(robots['A'], package), 'received_bytes': robots['A'].received}
    log('weak', 'result', **result['weak'])

    # bad: 60 % loss for a while, then stopped
    switched = []

    def turn_bad():
        if not switched and robots['B'].received >= BAD_AFTER:
            switched.append(robots['B'].received)
            network('bad', 'delay', '300ms', 'loss', '60%', 'rate', '1mbit')
    if not BAD_AFTER:
        turn_bad()
    else:                                     # 1 Mbit/s and 300 ms without loss until the switch, watched closely
        network('bad-start', 'delay', '300ms', 'rate', '1mbit')

        def watch():
            while not switched:
                turn_bad()
                time.sleep(0.02)
        threading.Thread(target=watch, daemon=True).start()
    view = install('bad', b, 8791, BAD)
    result['bad_switched_at_bytes'] = switched[0] if switched else None
    result['bad'] = {'state': view['state'], 'stage': view['stage'], 'code': (view['failure'] or {}).get('code'),
                     'left': view['left'], 'files': whole_files(robots['B'], package), 'received_bytes': robots['B'].received}
    log('bad', 'result', **result['bad'])

    # restored: clean again; robot B once more
    network('restored')
    received = robots['B'].received
    restore_start = time.monotonic()
    view = install('restored', b, 8791, RESTORE_LIMIT)
    robot = platform.store.get('robots', b['robot_id'])
    result['restored'] = {'finished': view['state'] == 'succeeded', 'seconds': round(time.monotonic() - restore_start, 1),
                          'robot_status': robot.get('status'), 'files': whole_files(robots['B'], package),
                          'sent_again_bytes': robots['B'].received - received, 'package_bytes': package.total_bytes}
    log('restored', 'result', **result['restored'])

    result['events'] = events
    with open(out, 'w') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    platform.close()
    for device in robots.values():
        device.close()
    ok = ((SKIP_WEAK or result['weak']['finished']) and not result['weak']['files']['damaged'] and not result['bad']['files']['damaged']
          and result['restored']['finished'] and not result['restored']['files']['damaged'])
    print(json.dumps({'ok': ok, 'weak': {k: result['weak'][k] for k in ('finished', 'attempts', 'seconds')},
                      'bad': {k: result['bad'][k] for k in ('state', 'stage', 'code')},
                      'restored': {k: result['restored'][k] for k in ('finished', 'seconds', 'sent_again_bytes')}},
                     ensure_ascii=False), flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else '/tmp/runtime-install-weak.json'))
