"""Browser fixture for issue #42: an installed skill is run at once, on a robot that is not a simulation.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/skill_run_browser_fixture.py [info.json]``, then
``node tests/skill_run_browser.cjs info.json [screenshot dir]``.

The console on a temporary state directory, and one device the browser test adds by its service
address: the real management layer (``neuxmind_runtime serve``) in its own process, with the simulated
quadruped whose manifest does not call itself simulated (``runtime/tests/_limit_bodies.py``). The
console and the management layer therefore treat it as a real body: its safety limits have to be filled
in and a run needs the on-site confirmation. Nothing here reaches a real machine. The access value is
made up for every run and written to the info file.
"""
import json
import os
import secrets
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from brainos import sim_robots  # noqa: E402
from brainos.httpd import FastBindHTTPServer  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402

ADAPTER = '_limit_bodies:RealQuadruped'


def serve(handler):
    server = FastBindHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    # the child finds the stand-in body next to the runtime's own tests
    os.environ['PYTHONPATH'] = os.pathsep.join(x for x in (str(ROOT / 'runtime' / 'tests'), os.environ.get('PYTHONPATH', '')) if x)
    access = secrets.token_urlsafe(24)
    device = sim_robots.SimProcess('fixture-real', ADAPTER, os.path.join(tmp.name, 'device'), access)
    app = api = None
    try:
        device_url = device.start()
        app = Application(tmp.name + '/app')
        api = serve(make_handler(app))
        info = {'url': f'http://127.0.0.1:{api.server_port}', 'device': {'url': device_url, 'access': access}}
        with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-skill-run-ui.json', 'w') as f:
            json.dump(info, f)
        print(info['url'], flush=True)

        def terminate(_signum, _frame):
            raise KeyboardInterrupt           # stopped with SIGTERM: clean up as on Ctrl-C

        signal.signal(signal.SIGTERM, terminate)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if api:
            api.shutdown()
            api.server_close()
        if app:
            app.close()
        device.stop()
        tmp.cleanup()
