"""Temporary local protocol fixture for the R-07/R-08 browser check. No real robot or model calls.

Serves the desktop app plus two test-only devices through brainos.edge:
a simulated body and a "real device" double whose stop latches the e-stop.
Usage: python3 tests/sim_reset_browser_fixture.py [config.json]
"""
import json
import os
import sys
import tempfile
import time

# Runs from any working directory (like ux01_browser_fixture.py): the worktree root on the import path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainos.edge import Gateway, make_handler as edge_handler
from brainos.server import Application, make_handler
from test_product import Server
from test_sim_evidence_and_reset import LatchingDevice, SimulatedDevice

if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-sim-reset-qa.json'
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name + '/app')
    token = 'local-fixture-token-123456789'
    sim, real = SimulatedDevice(), LatchingDevice()
    sim_gateway = Gateway(sim, token, tmp.name + '/sim-edge')
    real_gateway = Gateway(real, token, tmp.name + '/real-edge')
    sim_edge, real_edge = Server(edge_handler(sim_gateway)), Server(edge_handler(real_gateway))

    class Handler(make_handler(app)):
        def do_GET(self):
            if self.path == '/fixture/calls':
                return self.respond({'sim': sim.calls, 'real': real.calls, 'latched': real.latched})
            return super().do_GET()

    server = Server(Handler)
    config = {'url': server.url, 'sim_device': sim_edge.url, 'real_device': real_edge.url, 'token': token}
    with open(config_path, 'w') as f:
        json.dump(config, f)
    print(json.dumps(config), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        sim_edge.close()
        real_edge.close()
        sim_gateway.store.close()
        real_gateway.store.close()
        app.close()
        tmp.cleanup()
