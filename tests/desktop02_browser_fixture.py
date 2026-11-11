"""Browser acceptance fixture for round 02 (INTERFACES §4.1 client, §4.5).

Serves the desktop app together with:
* the real runtime simulated quadruped (runtime/neuxmind_runtime) over HTTP;
* ``AsyncDevice`` — a test-only protocol double of the §4.1 asynchronous
  on-site test (moves nothing, labels its evidence honestly);
* the real cloud app (test environment) with three seeded catalogue models.

Usage: python3 tests/desktop02_browser_fixture.py [config.json]
"""
import json
import os
import sys
import tempfile
import time

# Runs from any working directory (like ux01_browser_fixture.py): the worktree root on the import path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brainos.server import Application, make_handler
from round02_support import AsyncDevice, CloudServer, copy_example, runtime_support, write_skill
from test_product import Server

if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-desktop02-qa.json'
    tmp = tempfile.TemporaryDirectory()
    support = runtime_support()
    runtime = support.RuntimeFixture(http=True, time_scale=20.0)
    device = AsyncDevice()
    cloud = CloudServer()
    cloud.seed_models()
    app = Application(tmp.name + '/app')
    skills = {
        'patrol': copy_example(tmp.name + '/skills', 'patrol'),
        'wave': copy_example(tmp.name + '/skills', 'wave'),
        'fast': write_skill(tmp.name + '/skills', 'desk.fast', requires=('base.velocity',),
                            constraints={'base.velocity': {'max_vx': {'min': 2.0}}}, title='快速巡航'),
    }

    class Handler(make_handler(app)):
        def do_GET(self):
            if self.path == '/fixture/state':
                status, body = support.http(runtime.url, '/v2/skills')
                return self.respond({'runtime_skills': [s['id'] for s in body.get('skills', [])],
                                     'device_calls': device.calls, 'latched': device.latched})
            if self.path == '/fixture/finish':
                device.finish(passed=True)
                return self.respond({'ok': True})
            return super().do_GET()

    server = Server(Handler)
    config = {'url': server.url, 'runtime': runtime.url, 'runtime_token': support.TOKEN,
              'device': device.url, 'device_token': AsyncDevice.TOKEN, 'cloud': cloud.url, 'skills': skills}
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
        app.close()
        cloud.close()
        device.close()
        runtime.close()
        tmp.cleanup()
