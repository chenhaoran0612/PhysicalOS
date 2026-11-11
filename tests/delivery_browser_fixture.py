"""Temporary local protocol fixture. No real robot or model calls."""
import json
import tempfile
import time
from brainos.edge import Gateway, make_handler as edge_handler
from brainos.server import Application, make_handler
from test_product import DeviceFixture, Server


class Device(DeviceFixture):
    passed = True
    def evaluate(self, p):
        return {**super().evaluate(p), 'passed': self.passed}


if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name + '/app')
    device = Device()
    token = 'local-fixture-token-123456789'
    gateway = Gateway(device, token, tmp.name + '/edge')
    edge = Server(edge_handler(gateway))
    class Handler(make_handler(app)):
        def do_GET(self):
            if self.path == '/fixture/calls':
                return self.respond({'calls': device.calls})
            return super().do_GET()
        def do_POST(self):
            if self.path == '/fixture/control':
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                device.passed = payload['passed']
                return self.respond({'ok': True})
            return super().do_POST()
    server = Server(Handler)
    config = {'url': server.url, 'device': edge.url, 'token': token}
    with open('/tmp/neuxmind-delivery-qa.json', 'w') as f:
        json.dump(config, f)
    print(json.dumps(config), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        edge.close()
        gateway.store.close()
        app.close()
        tmp.cleanup()
