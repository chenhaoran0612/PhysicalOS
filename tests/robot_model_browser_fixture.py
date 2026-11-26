"""Browser fixture for issue #48: robots of two brands and models, none of them real.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/robot_model_browser_fixture.py [info.json]``, then
``node tests/robot_model_browser.cjs info.json [screenshot dir]``.

The console on a temporary state directory and three devices of the HTTP contract fixture
(tests/test_product.py): one whose manifest names a brand and model, two that name none. The access
value is made up for every run and written to the info file. ``/fixture/calls`` lists what each device
was asked to do.
"""
import json
import secrets
import signal
import sys
import tempfile
import time

from brainos.edge import Gateway, make_handler as edge_handler
from brainos.server import Application, make_handler
from test_product import Server
from test_robot_model import Reporting

DEVICES = {'reporting': ('Acme Robotics', 'X1'), 'silent-1': (None, None), 'silent-2': (None, None)}

if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name + '/app')
    access = secrets.token_urlsafe(24)
    devices, gateways, servers = {}, [], []
    for name, (vendor, model) in DEVICES.items():
        devices[name] = Reporting(vendor, model, 'fixture-' + name)
        gateways.append(Gateway(devices[name], access, f'{tmp.name}/edge-{name}'))
        servers.append(Server(edge_handler(gateways[-1])))

    class Handler(make_handler(app)):
        def do_GET(self):
            if self.path == '/fixture/calls':
                return self.respond({name: device.calls for name, device in devices.items()})
            return super().do_GET()

    server = Server(Handler)
    info = {'url': server.url, 'access': access, 'devices': {name: s.url for name, s in zip(DEVICES, servers)}}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-robot-model-ui.json', 'w') as f:
        json.dump(info, f)
    print(info['url'], flush=True)

    def terminate(_signum, _frame):
        raise KeyboardInterrupt               # stopped with SIGTERM: clean up as on Ctrl-C

    signal.signal(signal.SIGTERM, terminate)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        for s in servers:
            s.close()
        for g in gateways:
            g.store.close()
        app.close()
        tmp.cleanup()
