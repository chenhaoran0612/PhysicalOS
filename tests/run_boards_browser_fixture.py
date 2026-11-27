"""Browser fixture for issue #70: the console on a temporary state directory, following runs.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/run_boards_browser_fixture.py [info.json]``, then
``node tests/run_boards_browser.cjs info.json [screenshot dir]``.

The app's entry point turns the following of runs on (``brainos.server.main``); a fixture builds the
application itself, so it turns it on here. The browser test adds a simulated quadruped, installs the
bundled 巡检 skill and runs it: nothing reaches a real machine.
"""
import json
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import run_records  # noqa: E402
from brainos.httpd import FastBindHTTPServer  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402

if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    run_records.FOLLOW = True
    app = Application(tmp.name + '/app')
    server = FastBindHTTPServer(('127.0.0.1', 0), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        info = {'url': f'http://127.0.0.1:{server.server_port}'}
        with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-run-boards-ui.json', 'w') as f:
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
        server.shutdown()
        server.server_close()
        app.close()
        tmp.cleanup()
