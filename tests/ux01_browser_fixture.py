"""Desktop backend on a fresh, temporary state directory for the ux01 browser check.

Nothing is pre-registered: the browser test creates the workspace and adds the
simulated robots through the UI, which starts real ``neuxmind_runtime serve``
child processes (simulated bodies only). No real robot, cloud or model is used.

Usage: python3 tests/ux01_browser_fixture.py [config.json]

ux04: with ``NX_FIXTURE_HANG_RESET=1`` simulated quadrupeds use the test-only
``tests/ux04_hang_adapter.py`` body, whose ``reset()`` hangs while
``GET /fixture/hang?on=1`` is in effect (``?on=0`` lets it return).

ux05-fix2: with ``NX_FIXTURE_IGNORE_STOP=1 NX_FIXTURE_FIX2=1`` two more real runtimes: ``mixed`` (latch-guard
calibration partly stricter, partly looser) and ``hang`` (its reset hangs while ``/fixture/hang?on=1``; its
manifest declares ``safety.reset_expected_s = 7``). ux05-fix3 adds ``creep``: while ``/fixture/creep?on=1`` each
latch is followed by the body sliding 9 cm (the runtime's one-shot ``safety.latched_drift_notice``).

ux05-fix (Q7-03): with ``NX_FIXTURE_IGNORE_STOP=1`` simulated quadrupeds use
``tests/ignore_stop_adapter.py:IgnoreStopQuadruped`` (real runtime; while
``GET /fixture/ignore?on=1`` is in effect the body resumes walking 0.8 s after every
stop), and the fixture also starts two more real ``neuxmind_runtime serve`` processes
the browser adds as *real* robots by address and token (``config.real``): ``ignore``
(same stop-ignoring body, manifest says real with a hardware e-stop) and ``relaxed``
(obeys stops, latch-guard calibration loosened in its manifest). Simulated bodies only.
"""
import json
import os
import signal
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import socket  # noqa: E402

# "No cloud reachable" is the first-time user's situation: point the default cloud
# address at a port nothing listens on (unless the caller chose one).
if 'NEUXMIND_DEFAULT_CLOUD_URL' not in os.environ:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        os.environ['NEUXMIND_DEFAULT_CLOUD_URL'] = f'http://127.0.0.1:{s.getsockname()[1]}'

from brainos.server import Application, make_handler  # noqa: E402
from test_product import Server  # noqa: E402


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-ux01-qa.json'
    tmp = tempfile.TemporaryDirectory(prefix='nx-ux01-')
    hang_flag = os.path.join(tmp.name, 'hang-reset')
    if os.environ.get('NX_FIXTURE_HANG_RESET') == '1':
        from brainos import sim_robots
        here = os.path.dirname(os.path.abspath(__file__))
        os.environ['PYTHONPATH'] = os.pathsep.join(x for x in (here, os.environ.get('PYTHONPATH', '')) if x)
        os.environ['NX_FIXTURE_HANG_FLAG'] = hang_flag
        sim_robots.KINDS['quadruped'] = {**sim_robots.KINDS['quadruped'], 'adapter': 'ux04_hang_adapter:HangResetQuadruped'}
    ignore_flag = os.path.join(tmp.name, 'ignore-stop')
    creep_flag = os.path.join(tmp.name, 'creep')
    real = {}
    procs = []
    if os.environ.get('NX_FIXTURE_IGNORE_STOP') == '1':
        import secrets
        from brainos import sim_robots
        here = os.path.dirname(os.path.abspath(__file__))
        os.environ['PYTHONPATH'] = os.pathsep.join(x for x in (here, os.environ.get('PYTHONPATH', '')) if x)
        os.environ['NX_FIXTURE_IGNORE_FLAG'] = ignore_flag
        sim_robots.KINDS['quadruped'] = {**sim_robots.KINDS['quadruped'], 'adapter': 'ignore_stop_adapter:IgnoreStopQuadruped'}
        robots = [('ignore', 'IgnoreStopRealQuadruped'), ('relaxed', 'RelaxedRealQuadruped')]
        if os.environ.get('NX_FIXTURE_FIX2') == '1':
            # ux05-fix2: mixed calibration (Q7b-07) and a real robot whose reset hangs (Q7b-05/08, /fixture/hang)
            os.environ['NX_FIXTURE_HANG_FLAG'] = hang_flag
            robots += [('mixed', 'MixedRealQuadruped'), ('hang', 'HangResetRealQuadruped')]
            # ux05-fix3: a real robot that slides >5 cm while latched (/fixture/creep), for the drift notice
            os.environ['NX_FIXTURE_CREEP_FLAG'] = creep_flag
            robots.append(('creep', 'CreepRealQuadruped'))
        for key, adapter in robots:
            token = secrets.token_urlsafe(24)
            proc = sim_robots.SimProcess('fixture-' + key, 'ignore_stop_adapter:' + adapter,
                                         os.path.join(tmp.name, 'real-' + key), token)
            procs.append(proc)
            real[key] = {'endpoint': proc.start(), 'token': token, 'pid': proc.pid}
    app = Application(tmp.name + '/app')

    class Handler(make_handler(app)):
        def do_GET(self):
            if self.path == '/fixture/sims':
                procs = []
                for ws in app.workspaces.values():
                    procs += [{'robot_id': k, 'pid': p.pid, 'alive': p.alive} for k, p in ws.sims.procs.items()]
                return self.respond({'sims': procs})
            if self.path in ('/fixture/ignore?on=1', '/fixture/ignore?on=0'):
                if self.path.endswith('1'):
                    open(ignore_flag, 'w').close()
                elif os.path.exists(ignore_flag):
                    os.remove(ignore_flag)
                return self.respond({'ignore': os.path.exists(ignore_flag)})
            if self.path in ('/fixture/creep?on=1', '/fixture/creep?on=0'):
                if self.path.endswith('1'):
                    open(creep_flag, 'w').close()
                elif os.path.exists(creep_flag):
                    os.remove(creep_flag)
                return self.respond({'creep': os.path.exists(creep_flag)})
            if self.path in ('/fixture/hang?on=1', '/fixture/hang?on=0'):
                if self.path.endswith('1'):
                    open(hang_flag, 'w').close()
                elif os.path.exists(hang_flag):
                    os.remove(hang_flag)
                return self.respond({'hang': os.path.exists(hang_flag)})
            return super().do_GET()

    server = Server(Handler)
    with open(config_path, 'w') as f:
        json.dump({'url': server.url, 'state': tmp.name, 'real': real}, f)
    print(json.dumps({'url': server.url}), flush=True)

    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        app.close()            # stops every simulated robot child process
        for proc in procs:
            proc.stop()
        tmp.cleanup()


if __name__ == '__main__':
    main()
