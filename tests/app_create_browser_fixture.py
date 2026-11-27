"""Browser fixture for issue #68: an application is created for a task and a brand and model.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/app_create_browser_fixture.py [info.json]``, then
``node tests/app_create_browser.cjs info.json [screenshot dir]``.

The console on a temporary state directory, and two devices the browser test adds by their service
addresses: the real management layer (``neuxmind_runtime serve``) twice, once with the simulated
quadruped and once with the simulated arm. Nothing is installed on either: the skills an application
can call come from the skill library of the app. Nothing here reaches a real machine. The access value
is made up for every run and written to the info file.
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

DEVICES = (('dog', 'sim_quadruped'), ('arm', 'sim_arm'))


def serve(handler):
    server = FastBindHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    access = secrets.token_urlsafe(24)
    devices = [(name, sim_robots.SimProcess('fixture-' + name, adapter, os.path.join(tmp.name, name), access)) for name, adapter in DEVICES]
    app = api = None
    try:
        urls = {name: device.start() for name, device in devices}
        app = Application(tmp.name + '/app')
        api = serve(make_handler(app))
        info = {'url': f'http://127.0.0.1:{api.server_port}', 'access': access, 'devices': urls}
        with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-app-create-ui.json', 'w') as f:
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
        for _name, device in devices:
            device.stop()
        tmp.cleanup()
