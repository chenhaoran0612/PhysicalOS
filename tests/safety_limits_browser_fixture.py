"""Browser fixture for issue #22: safety limits filled in per device.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/safety_limits_browser_fixture.py [info.json]``, then
``node tests/safety_limits_browser.cjs info.json [screenshot dir]``.

The console on a temporary state directory and three device doubles (``tests/safety_limits_fixture.py``)
that the browser test adds as robots by their service address:

* ``real``  a body that cannot move until a person has filled in its limits;
* ``old``   a device whose management layer does not know safety limits yet;
* ``down``  a device the test switches off after it was connected.

A control port changes a device behind the page's back and reports what reached it. The access
values are made up for every run and written to the info file. Nothing here reaches a real machine.
"""
import json
import signal
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainos.httpd import FastBindHTTPServer  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from safety_limits_fixture import LimitsDevice  # noqa: E402


def control_handler(devices):
    """GET /device?name=N                what reached that device and what it holds now
    GET /body-max?name=N&linear=X    the body of that device now declares X m/s at most
    GET /busy?name=N&on=1|0          a skill is running on that device, or not
    GET /off?name=N                  switch that device off"""

    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            device = devices[query['name'][0]]
            if url.path == '/body-max':
                with device.lock:
                    device.body_max['max_linear_speed'] = float(query['linear'][0])
            elif url.path == '/busy':
                device.busy = query['on'][0] == '1'
            elif url.path == '/off':
                device.close()
            with device.lock:
                body = {'limits': device.limits, 'set_by': device.set_by, 'revision': device.revision,
                        'writes': len(device.posts), 'request_ids': [p.get('request_id') for p in device.posts],
                        'set_by_sent': [p.get('set_by') for p in device.posts]}
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Control


def serve(handler):
    server = FastBindHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = serve(make_handler(app))
    devices = {'real': LimitsDevice(), 'old': LimitsDevice(supported=False), 'down': LimitsDevice()}
    control = serve(control_handler(devices))
    info = {'url': f'http://127.0.0.1:{api.server_port}', 'control': f'http://127.0.0.1:{control.server_port}',
            'devices': {name: {'url': device.url, 'access': device.token} for name, device in devices.items()}}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-safety-limits-ui.json', 'w') as f:
        json.dump(info, f)
    print(info['url'], flush=True)

    def terminate(_signum, _frame):
        raise KeyboardInterrupt           # stopped with SIGTERM: clean up as on Ctrl-C

    signal.signal(signal.SIGTERM, terminate)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for server in (control, api):
            server.shutdown()
            server.server_close()
        app.close()
        for device in devices.values():
            device.close()                # a device the test switched off stays closed
        tmp.cleanup()
