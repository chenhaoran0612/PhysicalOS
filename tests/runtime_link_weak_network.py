"""Weak-network check of issue #17's link to the management layer. Linux only, as root with NET_ADMIN:
it shapes the loopback interface with tc netem. Not part of any suite; run it in a throwaway container
(Docker Desktop's kernel has netem), from the repository root:

    docker run --rm --cap-add NET_ADMIN -v "$PWD":/src:ro -v /tmp/nx17:/out python:3.13-slim sh -c \
      'apt-get update -qq && apt-get install -y -qq iproute2 && pip install -q paramiko==4.0.0 &&
       PYTHONDONTWRITEBYTECODE=1 python -u /src/tests/runtime_link_weak_network.py /out/result.json'

Environment: WEAK_SECONDS (600), BAD_SECONDS (300), RESTORE_SECONDS (300), ECHO_BYTES (200000),
ECHO_PARALLEL=1 (the large requests on a thread of their own, next to the small ones).

Only the SSH connection between the console and the robot is impaired (a prio qdisc with a u32 filter on the
fixture's SSH port, both directions): the robot's own loopback, between its SSH service and its management
layer, stays clean, as on a real robot. Delay applies per direction, so the round trip is twice the delay.

Phases (durations in seconds from the environment):
  clean    connect over SSH and link the management layer
  weak     1 Mbit/s, 300 ms each way, 30 % loss: link again from scratch (detach + attach, retried like a
           person would), then requests for W seconds; halfway the robot's SSH sessions are killed once
  bad      60 % loss for B seconds: requests keep coming, nothing is required to finish
  restored the network is clean again: nobody does anything but ask; how long until a request goes through
Writes the result file named on the command line and prints a summary. Local fixtures only; nothing
reaches a real machine.
"""
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
    """The fixture's server side gives a login as long as a real sshd (LoginGraceTime 120 s); the client side
    overrides these with the product's own limits when it connects."""
    _init(self, *args, **kwargs)
    self.banner_timeout = self.handshake_timeout = self.auth_timeout = 120


paramiko.Transport.__init__ = _patient

from brainos import connector, runtime_link  # noqa: E402
from brainos.service import Platform  # noqa: E402
from management_fixture import ManagementLayer  # noqa: E402
from runtime_link_support import PORT, LoopbackDevice  # noqa: E402
from ssh_fixture import DEFAULT_LOGIN  # noqa: E402

WEAK = float(os.environ.get('WEAK_SECONDS', 600))
BAD = float(os.environ.get('BAD_SECONDS', 300))
RESTORE_LIMIT = float(os.environ.get('RESTORE_SECONDS', 300))
ECHO_BYTES = int(os.environ.get('ECHO_BYTES', 200_000))
ECHO_PARALLEL = os.environ.get('ECHO_PARALLEL') == '1'      # the large requests on a thread of their own
START = time.monotonic()
events = []


def log(phase, kind, **fields):
    entry = {'t': round(time.monotonic() - START, 1), 'phase': phase, 'kind': kind, **fields}
    events.append(entry)
    print(json.dumps(entry, ensure_ascii=False), flush=True)


def run(*args):
    subprocess.run(list(args), check=True)


def shaping(port):
    run('ip', 'link', 'set', 'lo', 'mtu', '1500')
    run('tc', 'qdisc', 'add', 'dev', 'lo', 'root', 'handle', '1:', 'prio', 'bands', '4', 'priomap', *['0'] * 16)
    run('tc', 'qdisc', 'add', 'dev', 'lo', 'parent', '1:4', 'handle', '40:', 'netem', 'delay', '0ms')
    for side in ('dport', 'sport'):
        run('tc', 'filter', 'add', 'dev', 'lo', 'parent', '1:', 'protocol', 'ip', 'prio', '1', 'u32',
            'match', 'ip', side, str(port), '0xffff', 'flowid', '1:4')


def network(phase, *netem):
    run('tc', 'qdisc', 'change', 'dev', 'lo', 'parent', '1:4', 'handle', '40:', 'netem', *(netem or ('delay', '0ms')))
    log(phase, 'network', netem=' '.join(netem) or 'clean')


def main():
    tmp = tempfile.mkdtemp()
    platform = Platform(tmp)
    pid = platform.add_project({'name': 'weak', 'objective': 'weak network'})['id']
    layer = ManagementLayer()
    robot = LoopbackDevice(command_outputs={runtime_link.TOKEN_COMMAND: (layer.token + '\n').encode()})
    robot.serve_on_loopback(PORT, layer.port)
    shaping(robot.port)
    links, engine, store = platform.runtime_link, platform.engineering, platform.store

    # clean: connect and link
    check = engine.scan({'project_id': pid, 'host': '127.0.0.1', 'port': robot.port, 'username': 'robot'})
    login = {'project_id': pid, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint'],
             'auth': 'password', 'name': 'weak robot', 'family': 'quadruped'}
    login['password'] = DEFAULT_LOGIN[1]
    connection = engine.connect(login)
    rid = connection['robot_id']
    ask = {'project_id': pid, 'robot_id': rid}
    links.attach(ask)
    platform.heartbeat.start()            # the heartbeat runs next to it, as in the app
    log('clean', 'linked', state=links.overview({})['links'][rid]['state'])

    def request(phase, timeout, path='/v1/status', payload=None, what='status'):
        saved = store.get('robots', rid)
        started = time.monotonic()
        try:
            answer = connector.call(saved['endpoint'], path, saved['token'], payload, timeout=timeout)
            log(phase, what, ok=True, seconds=round(time.monotonic() - started, 1),
                bytes=answer.get('bytes') if what == 'echo' else None)
            return True
        except connector.DeviceError as exc:
            log(phase, what, ok=False, seconds=round(time.monotonic() - started, 1), code=exc.code, sent=exc.sent)
        except Exception as exc:          # anything else would reach the page as an internal error
            log(phase, what, ok=False, seconds=round(time.monotonic() - started, 1), unexpected=type(exc).__name__)
        return False

    stop_sampling = threading.Event()

    def sample():
        while not stop_sampling.wait(10):
            view = links.overview({})['links'].get(rid) or {}
            log('sample', 'link', state=view.get('state'), reason=(view.get('error') or {}).get('reason'),
                retry_in=view.get('retry_in'), sessions=robot.open_sessions(), logins=robot.logins())
    threading.Thread(target=sample, daemon=True).start()

    # weak: 1 Mbit/s, 300 ms each way, 30 % loss
    network('weak', 'delay', '300ms', 'loss', '30%', 'rate', '1mbit')
    links.detach(ask)
    log('weak', 'detached')
    for attempt in range(1, 21):
        started = time.monotonic()
        try:
            result = links.attach(ask)
            log('weak', 'attach', ok=True, attempt=attempt, seconds=round(time.monotonic() - started, 1),
                status=result['status'])
            break
        except Exception as exc:
            log('weak', 'attach', ok=False, attempt=attempt, seconds=round(time.monotonic() - started, 1),
                code=getattr(exc, 'code', None), error=type(exc).__name__, message=str(exc)[:160])
            time.sleep(5)
    end, dropped, n = time.monotonic() + WEAK, None, 0
    if ECHO_PARALLEL:
        def echoes():
            while time.monotonic() < end:
                request('weak', 180, '/v1/echo', {'text': 'x' * ECHO_BYTES}, 'echo')
        threading.Thread(target=echoes, daemon=True).start()
    while time.monotonic() < end:
        n += 1
        if dropped is None and time.monotonic() > end - WEAK / 2:
            robot.drop_sessions()
            dropped = time.monotonic()
            log('weak', 'sessions_killed')
        if n % 5 == 0 and not ECHO_PARALLEL:
            ok = request('weak', 180, '/v1/echo', {'text': 'x' * ECHO_BYTES}, 'echo')
        elif n % 2:
            ok = request('weak', 8, what='status8')          # the console's own limit for most requests
        else:
            ok = request('weak', 30)
        if ok and dropped is not None and dropped is not True:
            log('weak', 'back_after_kill', seconds=round(time.monotonic() - dropped, 1))
            dropped = True
        if not ok:
            time.sleep(2)

    # bad: 60 % loss
    network('bad', 'delay', '300ms', 'loss', '60%', 'rate', '1mbit')
    end = time.monotonic() + BAD
    while time.monotonic() < end:
        if not request('bad', 15):
            time.sleep(2)

    # restored: nobody clicks anything; requests keep coming as the page's own reads would
    network('restored')
    restored, back = time.monotonic(), None
    while time.monotonic() - restored < RESTORE_LIMIT:
        if request('restored', 10):
            back = round(time.monotonic() - restored, 1)
            break
        time.sleep(2)
    log('restored', 'back', seconds=back, state=links.overview({})['links'][rid]['state'])
    stop_sampling.set()

    summary = {}
    for phase in ('weak', 'bad', 'restored'):
        mine = [e for e in events if e['phase'] == phase and e['kind'] in ('status', 'status8', 'echo')]
        summary[phase] = {'requests': len(mine), 'ok': sum(e['ok'] for e in mine),
                          'codes': sorted({e.get('code') or e.get('unexpected') for e in mine if not e['ok']} - {None}),
                          'unexpected': [e for e in mine if 'unexpected' in e],
                          'sent_true_failures': sum(1 for e in mine if not e['ok'] and e.get('sent') is True),
                          'echo_ok': sum(1 for e in mine if e['kind'] == 'echo' and e['ok']),
                          'echo_total': sum(1 for e in mine if e['kind'] == 'echo'),
                          'status8_ok': sum(1 for e in mine if e['kind'] == 'status8' and e['ok']),
                          'status8_total': sum(1 for e in mine if e['kind'] == 'status8'),
                          'ok_seconds': sorted(e['seconds'] for e in mine if e['ok'] and e['kind'] != 'echo'),
                          'echo_seconds': sorted(e['seconds'] for e in mine if e['ok'] and e['kind'] == 'echo'),
                          'slowest_ok': max([e['seconds'] for e in mine if e['ok']] or [None])}
    summary['attach'] = [e for e in events if e['kind'] == 'attach']
    summary['back_after_kill'] = [e for e in events if e['kind'] == 'back_after_kill']
    summary['restored_back_seconds'] = back
    summary['logins'] = robot.logins()
    print('SUMMARY ' + json.dumps(summary, ensure_ascii=False), flush=True)
    with open(sys.argv[1] if len(sys.argv) > 1 else 'runtime_link_weak_network.json', 'w') as f:
        json.dump({'summary': summary, 'events': events}, f, ensure_ascii=False, indent=1)
    platform.close()
    robot.close()
    layer.close()


if __name__ == '__main__':
    main()
